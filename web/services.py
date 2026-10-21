"""Web UI services (web.md §1: "``web/services.py`` for UI preferences only"), plus the session and sign-in rules
the web layer itself enforces (R10, R12, R17, R18, R26, R27).

No business data is read or written here. Business screens call the owning module's services with the user.
"""
import hashlib
import hmac
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from core.access import services as access_services
from core.access.models import AccessLog, Kind

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


def _requires_mfa(user):
    """R13 (W-D6 list, OQ-3 default): superusers and holders of an active MFA role membership in any entity."""
    if not is_web_user(user):
        return False
    return user.is_superuser or access_services.holds_any_role(user, MFA_ROLES)


def idle_timeout(user):
    """OQ-8 default: 15 minutes for users who need MFA, 30 minutes for the others."""
    if _requires_mfa(user):
        return timedelta(seconds=_setting("WEB_SESSION_IDLE_MFA_SECONDS", 15 * 60))
    return timedelta(seconds=_setting("WEB_SESSION_IDLE_SECONDS", 30 * 60))


def absolute_timeout():
    """OQ-8 default: 10 hours after sign-in, whatever the activity."""
    return timedelta(seconds=_setting("WEB_SESSION_ABSOLUTE_SECONDS", 10 * 3600))


def log_login(user):
    """R26: ``login_succeeded`` in the access log. Never the password or the session key."""
    return access_services.log(AccessLog.Event.LOGIN_SUCCEEDED, actor=user, subject_user=user)


def log_logout(user, reason="sign_out"):
    """R26: ``logout`` in the access log, with why the session ended (``sign_out``, ``idle_timeout``,
    ``absolute_timeout``)."""
    return access_services.log(AccessLog.Event.LOGOUT, actor=user, subject_user=user, details={"reason": reason})


# --- sign-in with the throttle (R12, R18; OQ-9 thresholds, OQ-27) ---------------------------------------------

def client_ip(request):
    """The client's address for the lockout (OQ-27).

    Behind the TLS proxy (``WEB_CLIENT_IP_HEADER``, set when ``DJANGO_BEHIND_TLS_PROXY=1``) it is the header the
    proxy itself writes (Caddy ``header_up X-Real-IP {remote_host}``, which overwrites any client value).
    ``X-Forwarded-For`` is never read: a client can send it. Without the proxy it is ``REMOTE_ADDR``.
    """
    header = _setting("WEB_CLIENT_IP_HEADER", None)
    if header:
        value = request.META.get(header, "").strip()
        if value:
            return value
    return request.META.get("REMOTE_ADDR", "")


def _key(*parts):
    message = "|".join(parts).encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def _pair_key(username, ip):
    return _key("pair", (username or "").strip().lower(), ip or "")


def _user_key(username):
    return _key("user", (username or "").strip().lower())


def _window():
    return timedelta(seconds=_setting("WEB_LOGIN_LOCK_SECONDS", 15 * 60))


def _locked_row(key, now):
    """The throttle row of ``key``, locked for this transaction (created when missing); a lock or a counting window
    that has run out starts again from zero."""
    row, _ = LoginThrottle.objects.select_for_update().get_or_create(key=key, defaults={"first_failure_at": now})
    expired_lock = row.locked_until is not None and row.locked_until <= now
    stale = row.locked_until is None and now - row.first_failure_at > _window()
    if expired_lock or stale:
        row.failures, row.first_failure_at, row.locked_until = 0, now, None
    return row


def _purge(now):
    """Throttle rows whose window and lock have both run out are of no use: removed on every sign-in attempt."""
    old = now - _window()
    LoginThrottle.objects.filter(first_failure_at__lt=old).exclude(locked_until__gt=now).delete()


@dataclass(frozen=True)
class SignIn:
    """Result of :func:`sign_in`: ``user`` when the password was right; ``locked`` = ``"pair"`` (this username from
    this address) or ``"user"`` (this username from everywhere); ``tried`` = the password was checked."""

    user: object = None
    locked: str = ""
    tried: bool = False


def sign_in(request, username, password):
    """Check a password under the lockout (R18), in one transaction holding the throttle rows.

    Both rows are locked before anything is read, so concurrent attempts are counted one after the other: the
    attempt is counted **before** the password is checked, a locked pair or username is refused without checking
    it, and a right password clears the pair's failures. Limits (OQ-9 defaults): ``WEB_LOGIN_MAX_FAILURES`` (5)
    per username + IP and ``WEB_LOGIN_MAX_USER_FAILURES`` (20) per username from all addresses, each within
    ``WEB_LOGIN_LOCK_SECONDS`` (15 min), locking for the same time. The username cap bounds guessing spread over
    many addresses; its price is that anyone can lock a username for 15 minutes (web.md, as built).
    Only active people are returned (R12); everyone else gets the same answer as a wrong password.
    """
    from django.contrib.auth import authenticate

    ip = client_ip(request)
    now = timezone.now()
    with transaction.atomic():
        _purge(now)
        pair = _locked_row(_pair_key(username, ip), now)
        user_row = _locked_row(_user_key(username), now)
        for row, scope in ((pair, "pair"), (user_row, "user")):
            if row.locked_until is not None:
                return SignIn(locked=scope)
        limits = ((pair, _setting("WEB_LOGIN_MAX_FAILURES", 5)), (user_row, _setting("WEB_LOGIN_MAX_USER_FAILURES", 20)))
        for row, limit in limits:
            row.failures += 1
            if row.failures >= limit:
                row.locked_until = now + _window()
            row.save()
        user = authenticate(request, username=username, password=password)
        if user is not None and is_web_user(user):
            pair.delete()  # the username row keeps counting: guesses from elsewhere are not forgiven
            if user_row.failures:
                user_row.failures -= 1  # the right password is not a failure
                user_row.locked_until = None
                user_row.save()
            return SignIn(user=user, tried=True)
        for row, scope in ((pair, "pair"), (user_row, "user")):
            if row.locked_until is not None:
                return SignIn(locked=scope, tried=True)
        return SignIn(tried=True)
