"""Web UI services (web.md §1: "``web/services.py`` for UI preferences only"), plus the session and sign-in rules
the web layer itself enforces (R10, R12, R17, R18, R26, R27).

No business data is read or written here. Business screens call the owning module's services with the user.
"""
import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from core.access import services as access_services
from core.access.models import AccessLog, Kind, Membership

from .models import LoginThrottle, UiPreference

LANGUAGES = ("ar", "en")
DEFAULT_LANGUAGE = "ar"  # caller brief: Arabic (RTL) is the default before a language is chosen
LANGUAGE_COOKIE = "alm_lang"

# R13 (W-D6, OQ-3 default): roles whose holders need MFA. Used in slice 1 for the shorter idle timeout (OQ-8).
MFA_ROLES = frozenset({"approver", "managing_partner", "chairman", "zatca_admin", "access_admin"})


def _setting(name, default):
    return getattr(settings, name, default)


# --- language (R27) -----------------------------------------------------------------------------------------------

def _accept_language(header):
    """The first of ``ar`` / ``en`` named in an Accept-Language header, by quality; None if neither."""
    best, best_q = None, -1.0
    for part in (header or "").split(","):
        tag, _, params = part.strip().partition(";")
        code = tag.strip().lower().split("-")[0]
        if code not in LANGUAGES:
            continue
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        if q > best_q:
            best, best_q = code, q
    return best


def language_for(user, cookie_value, accept_language):
    """The UI language (R27): the person's profile once signed in; before sign-in the cookie, then the browser's
    Accept-Language, then Arabic."""
    if user is not None and getattr(user, "is_authenticated", False):
        profile = access_services.profile_of(user)
        if profile is not None and profile.preferred_language in LANGUAGES:
            return profile.preferred_language
    if cookie_value in LANGUAGES:
        return cookie_value
    return _accept_language(accept_language) or DEFAULT_LANGUAGE


def set_language(user, language):
    """The language switch (R27): saved to the signed-in person's profile through the logged access service."""
    if language not in LANGUAGES:
        raise ValueError(language)
    if user is not None and getattr(user, "is_authenticated", False):
        access_services.set_preferred_language(user, language)
    return language


# --- entities (R9, R10, R11) --------------------------------------------------------------------------------------

def switcher_entities(user):
    """The entities of the switcher, as a list (R9): exactly the visible ones, ordered by code."""
    return list(access_services.visible_entities(user))


def entity_from(entities, code):
    """The entity with ``code`` among ``entities`` (already the visible ones); 404 otherwise (R4, R10)."""
    for entity in entities:
        if entity.code == code:
            return entity
    raise Http404("Not found / غير موجود")


def start_entity(user, entities):
    """Where the start page goes (R10): the remembered entity if still visible, else the first visible one, else
    None ("no access")."""
    if not entities:
        return None
    pref = UiPreference.objects.filter(user=user).only("last_entity_id").first()
    if pref is not None and pref.last_entity_id:
        for entity in entities:
            if entity.pk == pref.last_entity_id:
                return entity
    return entities[0]


@transaction.atomic
def remember_entity(user, code):
    """The entity switch (R10, R11): only a visible entity; remembered in ``UiPreference.last_entity``."""
    entity = entity_from(switcher_entities(user), code)
    pref, _ = UiPreference.objects.select_for_update().get_or_create(user=user)
    if pref.last_entity_id != entity.pk:
        pref.last_entity = entity
        pref.save(update_fields=["last_entity", "updated_at"])
    return entity


# --- sessions (R12, R17) ------------------------------------------------------------------------------------------

def is_web_user(user):
    """Only active people use the web UI (R12): never agent or system users."""
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and access_services.kind_of(user) == Kind.HUMAN)


def requires_mfa(user):
    """R13 (W-D6 list, OQ-3 default): superusers and holders of an active MFA role membership in any entity."""
    if not is_web_user(user):
        return False
    if user.is_superuser:
        return True
    return Membership.objects.active().filter(user=user, role__code__in=MFA_ROLES).exists()


def idle_timeout(user):
    """OQ-8 default: 15 minutes for users who need MFA, 30 minutes for the others."""
    if requires_mfa(user):
        return timedelta(seconds=_setting("WEB_SESSION_IDLE_MFA_SECONDS", 15 * 60))
    return timedelta(seconds=_setting("WEB_SESSION_IDLE_SECONDS", 30 * 60))


def absolute_timeout():
    """OQ-8 default: 10 hours after sign-in, whatever the activity."""
    return timedelta(seconds=_setting("WEB_SESSION_ABSOLUTE_SECONDS", 10 * 3600))


def log_login(user):
    """R26: ``login_succeeded`` in the access log. Never the password or the session key."""
    return access_services.log(AccessLog.Event.LOGIN_SUCCEEDED, actor=user, subject_user=user)


def log_logout(user):
    """R26: ``logout`` in the access log."""
    return access_services.log(AccessLog.Event.LOGOUT, actor=user, subject_user=user)


# --- sign-in throttle (R18, OQ-9 thresholds) -------------------------------------------------------------------

def _throttle_key(username, ip):
    secret = settings.SECRET_KEY.encode()
    message = f"{(username or '').strip().lower()}|{ip or ''}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def login_locked(username, ip):
    """True while (username, IP) is locked out after repeated failures (R18)."""
    row = LoginThrottle.objects.filter(key=_throttle_key(username, ip)).first()
    return bool(row and row.locked_until and row.locked_until > timezone.now())


@transaction.atomic
def record_login_failure(username, ip):
    """Count one failed sign-in; lock (username, IP) after ``WEB_LOGIN_MAX_FAILURES`` failures within the lock
    window, for ``WEB_LOGIN_LOCK_SECONDS`` (OQ-9: 5 failures, 15 minutes). Returns True when now locked."""
    now = timezone.now()
    window = timedelta(seconds=_setting("WEB_LOGIN_LOCK_SECONDS", 15 * 60))
    row, created = LoginThrottle.objects.select_for_update().get_or_create(
        key=_throttle_key(username, ip), defaults={"first_failure_at": now},
    )
    expired_lock = row.locked_until is not None and row.locked_until <= now
    stale = row.locked_until is None and now - row.first_failure_at > window
    if not created and (expired_lock or stale):
        row.failures, row.first_failure_at, row.locked_until = 0, now, None
    row.failures += 1
    if row.failures >= _setting("WEB_LOGIN_MAX_FAILURES", 5):
        row.locked_until = now + window
    row.save()
    return row.locked_until is not None


def clear_login_failures(username, ip):
    """A successful sign-in clears the failures of (username, IP)."""
    LoginThrottle.objects.filter(key=_throttle_key(username, ip)).delete()
