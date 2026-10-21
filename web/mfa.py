"""Multi-factor sign-in for the web UI: TOTP (RFC 6238) and recovery codes (web.md R13 to R16, R18, R19, R26).

* **Library.** One-time codes come from ``pyotp`` (MIT); the enrolment QR code is drawn on the server as SVG by
  ``segno`` (BSD-3-Clause), never by an outside service (R16, web.md §6). Codes: 6 digits, 30-second steps, one step
  of clock drift either way, each step accepted once (no replay).
* **Who.** MFA is required (R13, OQ-3 default) for superusers and holders of an active membership in one of
  ``web.services.MFA_ROLES``, checked at every request (a membership granted mid-session counts at the next request,
  R14). Staff users also enrol, because Django admin needs a verified session from everyone (R19); other people do
  not enrol. ``settings.WEB_MFA_ENFORCED`` switches the whole gate; with DEBUG off the deploy check ``web.E001``
  refuses a release where it is off.
* **Session.** After a right code the session records the device id and its key is rotated (R17). The record is
  valid only while that device exists and is confirmed, so a reset ends it at the next request.
* **Step-up.** Approving in the inbox needs a fresh code on the approve form, on top of the verified session
  (R15; owner instruction 2026-10-21 replaces the OQ-23 session-only default).
* **Throttle (R18, OQ-30).** Every wrong code doubles the wait before the next try (1, 2, 4, 8 s); 5 wrong codes in a
  row lock the device for ``WEB_MFA_LOCK_SECONDS`` (15 min), each further lock in a row twice as long (at most 24 h),
  and the second lock alerts the person and their access admins. A try during the wait or the lock is refused without
  checking the code. Every refusal is logged ``mfa_verify_failed`` (R26). The device row is locked for the check, so
  concurrent tries are counted one after the other.
* **Recovery codes.** 10 single-use codes shown once at enrolment (or renewal), stored as keyed hashes
  (``web.crypto``). A recovery code signs in; it is not accepted for a step-up.
* **Reset (R16, OQ-11; security review 2026-10-21).** Never by the user themselves; the actor needs a verified
  session and a fresh code. The group managing partner may reset anyone else; an access admin only people without an
  MFA role whose memberships are all in the actor's entities; superusers only by ``manage.py web_mfa_reset``.
  Logged ``mfa_reset`` with the reason.

Nothing here logs or stores a code, a secret or a recovery code in clear.
"""
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

import pyotp
import segno
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.access import services as access_services
from core.access.authority_models import AccessAlert
from core.access.models import AccessLog, Membership
from core.models import Entity

from . import crypto
from . import services as web_services
from .models import MfaDevice, RecoveryCode

ISSUER = "Almiras ERP"
DIGITS = 6
INTERVAL = 30
DRIFT_STEPS = 1
RECOVERY_CODES = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: read aloud and typed safely
VERIFIED_KEY = "web_mfa_verified"  # session: the id of the device that verified this session
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


class MfaError(Exception):
    """A refused MFA step. ``code``: ``wrong_code``, ``throttled``, ``locked``, ``no_device``, ``already_enrolled``,
    ``not_allowed``, ``reason_required``, ``no_user``, ``expired``, ``session_not_verified``. The web layer shows the matching ``mfa_err_<code>`` text (R28)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _now():
    return timezone.now()


def _setting(name, default):
    return getattr(settings, name, default)


def enforced():
    return bool(_setting("WEB_MFA_ENFORCED", True))


def requires_mfa(user):
    """R13: a superuser, or a holder of an active MFA-role membership in any entity (read at each call)."""
    return web_services._requires_mfa(user)


def device_of(user):
    """The user's confirmed device, or None."""
    if user is None or not getattr(user, "pk", None):
        return None
    return MfaDevice.objects.filter(user=user, confirmed=True).first()


ENROLMENT_TTL = timedelta(minutes=15)  # security review 2026-10-21, item 7


def _unconfirmed(user):
    return MfaDevice.objects.filter(user=user, confirmed=False).first()


def _expired(device):
    return device.started_at is None or _now() - device.started_at > ENROLMENT_TTL


def pending_device(user):
    """The user's started, not yet confirmed and not expired enrolment (15 minutes), or None."""
    device = _unconfirmed(user)
    return None if device is None or _expired(device) else device


def must_verify(user, required=None):
    """True if this user's session must be OTP-verified and approvals need a step-up code: MFA is switched on and
    the user needs it (R13). ``required`` passes a value the caller already read in this request."""
    if not enforced() or not web_services.is_web_user(user):
        return False
    return requires_mfa(user) if required is None else bool(required)


def may_enrol(user):
    """Enrolment is for people who need MFA (R13) and for staff users, who need it for Django admin (R19)."""
    return web_services.is_web_user(user) and (requires_mfa(user) or user.is_staff or user.is_superuser)


def session_verified(user, session):
    """True if ``session`` was verified with the user's current confirmed device (R14, R15; read every time)."""
    device = device_of(user)
    return device is not None and session.get(VERIFIED_KEY) == device.pk


def gate(user, session, required=None):
    """What a signed-in request needs before an ordinary page (R14): ``None`` (nothing), ``"enrol"`` (no confirmed
    device yet) or ``"verify"`` (a code for this session). A user who does not need MFA costs no device query."""
    if not must_verify(user, required):
        return None
    device = device_of(user)
    if device is None:
        return "enrol"
    if session.get(VERIFIED_KEY) != device.pk:
        return "verify"
    return None


def mark_verified(session, device):
    """Record the verification in the session and rotate its key (R17)."""
    session.cycle_key()
    session[VERIFIED_KEY] = device.pk


# --- codes --------------------------------------------------------------------------------------------------

def normalise_code(value):
    """Digits only: spaces and dashes dropped; Arabic-Indic and Extended Arabic-Indic digits read as Latin (R33)."""
    return "".join(ch for ch in str(value or "").translate(_ARABIC_DIGITS) if ch.isdigit())


def _normalise_recovery(value):
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


def _counter(now):
    return int(now.timestamp()) // INTERVAL


def _totp(device):
    return pyotp.TOTP(device.secret, digits=DIGITS, interval=INTERVAL)


REPLAY = "replay"


def _totp_matches(device, code, now):
    """Accept ``code`` for the current step or one step either way, never a step at or before the last accepted
    one (each code works once). Updates ``device.last_counter`` in memory when it matches. The code of the last
    accepted step itself (a double submit, or the code just used to sign in) returns :data:`REPLAY`: refused but not
    counted as a wrong code (verification review of f95e1c8, item 7)."""
    code = normalise_code(code)
    if len(code) != DIGITS:
        return False
    totp = _totp(device)
    current = _counter(now)
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if device.last_counter is not None and step <= device.last_counter:
            if step == device.last_counter and hmac.compare_digest(totp.generate_otp(step), code):
                return REPLAY
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            device.last_counter = step
            return True
    return False


def _delay(failures):
    lock = _setting("WEB_MFA_LOCK_SECONDS", 15 * 60)
    return timedelta(seconds=min(2 ** max(failures - 1, 0), lock))


def _lock_length(lock_count):
    """OQ-30 team default (security review 2026-10-21): the first lock lasts ``WEB_MFA_LOCK_SECONDS`` (15 min);
    each further lock without a right code in between doubles it, at most ``WEB_MFA_LOCK_MAX_SECONDS`` (24 h)."""
    base = _setting("WEB_MFA_LOCK_SECONDS", 15 * 60)
    cap = _setting("WEB_MFA_LOCK_MAX_SECONDS", 24 * 3600)
    return timedelta(seconds=min(base * 2 ** max(lock_count - 1, 0), cap))


def _alert_second_lock(device, now):
    """OQ-30: on the second lock in a row the person and the access admins of every entity where they hold an active
    membership get a bilingual in-app alert (written once per lock)."""
    from core.access import engine

    user = device.user
    entities = {m.entity for m in Membership.objects.active(access_services.now()).filter(user=user)
                .select_related("entity")}
    key = f"mfa_locked:{device.pk}:{device.locked_until.isoformat()}"
    en = (f"Two-step verification of {user.get_username()} was locked again after repeated wrong codes, until "
          f"{timezone.localtime(device.locked_until):%Y-%m-%d %H:%M}. If this was not the person, reset the device "
          "and review the account.")
    ar = (f"قُفل التحقق بخطوتين للمستخدم {user.get_username()} مرة أخرى بعد رموز خاطئة متكررة، حتى "
          f"{timezone.localtime(device.locked_until):%Y-%m-%d %H:%M}. إن لم يكن ذلك صاحب الحساب فأعد تعيين الجهاز "
          "وراجع الحساب.")
    recipients = {}
    for entity in entities:
        admins = set(Membership.objects.active(access_services.now()).filter(entity=entity, role__code="access_admin")
                     .values_list("user_id", flat=True))
        recipients[entity] = admins | {user.pk}
    if not entities:
        # Verification review item 8: a person without memberships (a superuser or a staff user) has no access admin;
        # the operators (active superusers other than the person) are told, on the group parent(s), and the person.
        from django.contrib.auth import get_user_model

        operators = set(get_user_model()._default_manager.filter(is_superuser=True, is_active=True)
                        .exclude(pk=user.pk).values_list("pk", flat=True))
        for root in Entity.objects.filter(parent__isnull=True, consolidation=Entity.Consolidation.PARENT):
            recipients[root] = operators | {user.pk}
    # The alert row and its access_alert_sent log row are written in this same transaction (the code check's own,
    # committed even if the request fails, item 1): both are kept or neither.
    for entity, uids in recipients.items():
        for uid in uids:
            engine.alert(entity, uid, AccessAlert.Kind.MFA_LOCKED, en, ar, key=f"{key}:{entity.pk}:{uid}",
                         details={"subject_user": user.pk, "lock_count": device.lock_count})


def _attempt(device_pk, check, *, purpose, kind="totp"):
    """One throttled check of ``device_pk`` (R18), in its own transaction holding the device row.

    Returns ``(device, refusal)``; ``refusal`` is None when ``check(device, now)`` accepted the code. A wrong code is
    counted and saved before the refusal is reported, so the caller raises outside this transaction. **The caller
    must not run inside a transaction that can still roll back** (the web views that check codes are
    ``non_atomic_requests``, tested): otherwise a later error would undo the count, the lock, the used time step
    and the ``mfa_verify_failed`` row (security review 2026-10-21, item 1)."""
    now = _now()
    max_failures = _setting("WEB_MFA_MAX_FAILURES", 5)
    with transaction.atomic():
        device = MfaDevice.objects.select_for_update().select_related("user").get(pk=device_pk)
        if device.locked_until is not None and device.locked_until <= now:  # a lock that ran out starts again
            device.failures, device.locked_until, device.last_failure_at = 0, None, None
        refusal = None
        if device.locked_until is not None:
            refusal = "locked"
        elif device.failures and device.last_failure_at and now < device.last_failure_at + _delay(device.failures):
            refusal = "throttled"
        else:
            result = check(device, now)
            if result == REPLAY:
                refusal = "replayed"
            elif result:
                device.failures, device.last_failure_at, device.locked_until, device.lock_count = 0, None, None, 0
                device.save(update_fields=["last_counter", "failures", "last_failure_at", "locked_until",
                                           "lock_count"])
                return device, None
            else:
                device.failures += 1
                device.last_failure_at = now
                if device.failures >= max_failures:
                    device.lock_count += 1
                    device.locked_until = now + _lock_length(device.lock_count)
                    refusal = "locked"
                    if device.lock_count == 2:
                        _alert_second_lock(device, now)
                else:
                    refusal = "wrong_code"
        device.save(update_fields=["failures", "last_failure_at", "locked_until", "lock_count"])
        access_services.log(AccessLog.Event.MFA_VERIFY_FAILED, actor=device.user, subject_user=device.user,
                            details={"purpose": purpose, "kind": kind, "reason": refusal,
                                     "failures": device.failures, "lock_count": device.lock_count})
    return device, refusal


# --- enrolment (R16) -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Provisioning:
    """What the enrolment page shows: the QR code as an SVG data URI and the secret for typing by hand."""

    qr_data_uri: str
    secret_groups: tuple
    issuer: str
    account: str


def start_enrolment(user):
    """Start (or restart) enrolment: a new random secret on an unconfirmed device. Refused when the user already has
    a confirmed device: a lost or replaced device is reset by another person (R16, OQ-11)."""
    if not may_enrol(user):
        raise MfaError("not_allowed")
    with transaction.atomic():
        device = MfaDevice.objects.select_for_update().filter(user=user).first()
        if device is not None and device.confirmed:
            raise MfaError("already_enrolled")
        if device is None:
            device = MfaDevice(user=user)
        device.secret = pyotp.random_base32(length=32)  # 160 bits (RFC 4226 recommends at least 128)
        device.last_counter, device.failures, device.last_failure_at, device.locked_until = None, 0, None, None
        device.started_at = _now()
        device.save()
    return device


def provisioning(user, device):
    """The ``otpauth://`` QR code (drawn here, no outside service) and the grouped secret of a pending device."""
    account = user.get_username()
    uri = _totp(device).provisioning_uri(name=account, issuer_name=ISSUER)
    qr = segno.make(uri, error="m").svg_data_uri(scale=5, border=4, dark="#0B2A3C", light="#FFFFFF")
    groups = tuple(device.secret[i:i + 4] for i in range(0, len(device.secret), 4))
    return Provisioning(qr, groups, ISSUER, account)


def _new_recovery_codes(user, now):
    RecoveryCode.objects.filter(user=user, used_at__isnull=True).update(used_at=now)  # old codes stop working
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    current = crypto.key_id()
    RecoveryCode.objects.bulk_create([RecoveryCode(user=user, code_hash=crypto.code_hash(_normalise_recovery(c)),
                                                   key_id=current) for c in codes])
    return codes


def confirm_enrolment(user, code):
    """Confirm the pending device with a right code (R16): the device becomes the user's, 10 recovery codes are
    made and returned **once** (only their hashes are kept), ``mfa_enrolled`` is logged."""
    device = _unconfirmed(user)
    if device is None:
        raise MfaError("no_device")
    if _expired(device):
        raise MfaError("expired")  # the QR code was shown more than 15 minutes ago: start again
    device, refusal = _attempt(device.pk, lambda d, now: _totp_matches(d, code, now), purpose="enrol")
    if refusal:
        raise MfaError(refusal)
    now = _now()
    with transaction.atomic():
        device.confirmed, device.confirmed_at = True, now
        device.save(update_fields=["confirmed", "confirmed_at"])
        codes = _new_recovery_codes(user, now)
        access_services.log(AccessLog.Event.MFA_ENROLLED, actor=user, subject_user=user,
                            details={"device": device.pk, "recovery_codes": len(codes)})
    return device, codes


# --- verification (R14, R15) ---------------------------------------------------------------------------------

def verify(user, code, *, purpose="sign_in"):
    """Check a TOTP code of the user's confirmed device under the throttle; ``purpose`` is ``sign_in`` (the session)
    or ``step_up`` (an approval, R15). Logged ``mfa_verified`` or ``mfa_verify_failed``."""
    device = device_of(user)
    if device is None:
        raise MfaError("no_device")
    device, refusal = _attempt(device.pk, lambda d, now: _totp_matches(d, code, now), purpose=purpose)
    if refusal:
        raise MfaError(refusal)
    access_services.log(AccessLog.Event.MFA_VERIFIED, actor=user, subject_user=user,
                        details={"purpose": purpose, "kind": "totp", "device": device.pk})
    return device


def use_recovery_code(user, code):
    """Sign in with a recovery code instead of a TOTP code (R16): single use, under the same throttle."""
    device = device_of(user)
    if device is None:
        raise MfaError("no_device")
    hashes = crypto.code_hashes(_normalise_recovery(code))
    used = {}

    def check(dev, now):
        row = (RecoveryCode.objects.select_for_update()
               .filter(user=user, used_at__isnull=True, code_hash__in=hashes).first())
        if row is None:
            return False
        row.used_at = now
        row.save(update_fields=["used_at"])
        used["row"] = row
        return True

    device, refusal = _attempt(device.pk, check, purpose="sign_in", kind="recovery_code")
    if refusal:
        raise MfaError(refusal)
    remaining = RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()
    access_services.log(AccessLog.Event.MFA_RECOVERY_USED, actor=user, subject_user=user,
                        details={"device": device.pk, "remaining": remaining})
    return device


def recovery_codes_left(user):
    return RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()


def renew_recovery_codes(user, code):
    """New recovery codes (the old ones stop working), after a right TOTP code; returned once, logged."""
    device = verify(user, code, purpose="renew_recovery_codes")
    with transaction.atomic():
        codes = _new_recovery_codes(user, _now())
        access_services.log(AccessLog.Event.MFA_RECOVERY_RENEWED, actor=user, subject_user=user,
                            details={"device": device.pk, "recovery_codes": len(codes)})
    return codes


# --- reset by another person (R16, OQ-11) -------------------------------------------------------------------------

TOP_ROLES = frozenset({"managing_partner", "chairman"})


def may_reset(actor, user):
    """Who may reset another person's device (R16; OQ-11 default as tightened by the security review of 2026-10-21,
    item 2). Never oneself, never a superuser (operator command only), and only:

    * the **group managing partner** (``managing_partner`` on a group parent): anyone else whose memberships all lie
      in that group's tree; the only person (besides the operator command) who may reset a ``managing_partner`` or
      ``chairman``;
    * an **access admin**: a person who does not need MFA at all (no MFA role, no approval right, no active
      delegation; ``requires_mfa``) and has active memberships only in entities where the actor is ``access_admin``
      (in practice staff users who enrolled for Django admin).
    """
    if not web_services.is_web_user(actor) or user is None or actor.pk == user.pk or user.is_superuser:
        return False
    at = access_services.now()
    if access_services.group_managing_partner_covers(actor, user):  # own group tree only (verification item 5)
        return True
    if requires_mfa(user):  # verification item 2: never by an access admin (roles, approval rights, delegates)
        return False
    theirs = list(Membership.objects.active(at).filter(user=user).select_related("role"))
    if not theirs or {m.role.code for m in theirs} & (web_services.MFA_ROLES | TOP_ROLES):
        return False
    admin_of = set(Membership.objects.active(at).filter(user=actor, role__code="access_admin")
                   .values_list("entity_id", flat=True))
    return {m.entity_id for m in theirs} <= admin_of


def _remove(user):
    with transaction.atomic():
        device = MfaDevice.objects.select_for_update().filter(user=user).first()
        had = device is not None
        if device is not None:
            device.delete()
        RecoveryCode.objects.filter(user=user).delete()
    return had


def reset_device(actor, user, *, reason, session, code):
    """Remove ``user``'s device and recovery codes so they enrol again at their next sign-in (R16). Logged
    ``mfa_reset`` with the reason. The actor needs an OTP-verified session of their own and a fresh code
    (step-up, purpose ``reset``); refused ``not_allowed`` under :func:`may_reset`. Like every code check, the caller
    must not run inside a transaction that can roll back."""
    if not (reason or "").strip():
        raise MfaError("reason_required")
    if not may_reset(actor, user):
        raise MfaError("not_allowed")
    if not session_verified(actor, session):
        raise MfaError("session_not_verified")
    verify(actor, code, purpose="reset")
    had = _remove(user)
    access_services.log(AccessLog.Event.MFA_RESET, actor=actor, subject_user=user,
                        details={"reason": reason.strip()[:500], "had_device": had})
    return had


def reset_device_by_operator(username, *, reason, os_user=""):
    """``manage.py web_mfa_reset``: the operator path, also for superusers (OQ-11 default). Logged ``mfa_reset``."""
    from django.contrib.auth import get_user_model

    if not (reason or "").strip():
        raise MfaError("reason_required")
    user = get_user_model().objects.filter(username=username).first()
    if user is None:
        raise MfaError("no_user")
    had = _remove(user)
    access_services.log(AccessLog.Event.MFA_RESET, subject_user=user, source="command:web_mfa_reset",
                        details={"reason": reason.strip()[:500], "had_device": had, "os_user": os_user[:100]})
    return had


def rekey(*, expire_old_recovery_codes=False):
    """``manage.py web_mfa_rekey`` after a key rotation: re-encrypt every device secret under the current first key
    and report the unused recovery codes still hashed under an older key (one-way hashes cannot be re-hashed);
    ``expire_old_recovery_codes`` retires them (their holders renew their codes). Logged in the access log."""
    from django.db.models import TextField
    from django.db.models.functions import Cast

    current = crypto.key_id()
    reencrypted = 0
    with transaction.atomic():
        # The stored ciphertext, not the decrypted value (verification review item 3): Cast reads the column as text.
        raw = dict(MfaDevice.objects.select_for_update().annotate(raw=Cast("secret", TextField()))
                   .values_list("pk", "raw"))
        for device in MfaDevice.objects.filter(pk__in=[pk for pk, value in raw.items() if not crypto.is_current(value)]):
            device.secret = device.secret  # decrypted on read; encrypted under the first key on save
            device.save(update_fields=["secret"])
            reencrypted += 1
        unused = RecoveryCode.objects.filter(used_at__isnull=True)
        old = unused.exclude(key_id__in=[current, ""])
        unknown = unused.filter(key_id="")  # item 4: hashed before key_id existed, key not known: never auto-expired
        users = sorted(set(old.values_list("user__username", flat=True)))
        unknown_users = sorted(set(unknown.values_list("user__username", flat=True)))
        old_count, unknown_count = old.count(), unknown.count()
        expired = bool(expire_old_recovery_codes and old_count)
        if expired:
            old.update(used_at=_now())
        access_services.log(AccessLog.Event.MFA_REKEYED, source="command:web_mfa_rekey",
                            details={"rekey": True, "reencrypted": reencrypted, "old_recovery_codes": old_count,
                                     "unknown_key_recovery_codes": unknown_count, "expired": expired,
                                     "key_id": current})
    return {"reencrypted": reencrypted, "old_recovery_codes": old_count, "users": users, "expired": expired,
            "unknown_key_recovery_codes": unknown_count, "unknown_key_users": unknown_users}
