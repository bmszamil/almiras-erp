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
* **Throttle (R18).** Every wrong code doubles the wait before the next try (1, 2, 4, 8 s); 5 wrong codes in a row
  lock the device for ``WEB_MFA_LOCK_SECONDS`` (15 min). A try during the wait or the lock is refused without
  checking the code. Every refusal is logged ``mfa_verify_failed`` (R26). The device row is locked for the check, so
  concurrent tries are counted one after the other.
* **Recovery codes.** 10 single-use codes shown once at enrolment (or renewal), stored as keyed hashes
  (``web.crypto``). A recovery code signs in; it is not accepted for a step-up.
* **Reset (R16, OQ-11 default).** Never by the user themselves: by a holder of ``managing_partner``, or the
  ``access_admin`` of an entity where the user holds an active membership; superusers only by the operator command
  ``manage.py web_mfa_reset``. Logged ``mfa_reset`` with the reason.

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
from core.access.models import AccessLog, Membership

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
    ``not_allowed``, ``reason_required``, ``no_user``. The web layer shows the matching ``mfa_err_<code>`` text (R28)."""

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


def pending_device(user):
    """The user's started but not yet confirmed enrolment, or None."""
    return MfaDevice.objects.filter(user=user, confirmed=False).first()


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


def _totp_matches(device, code, now):
    """Accept ``code`` for the current step or one step either way, never a step at or before the last accepted
    one (each code works once). Updates ``device.last_counter`` in memory when it matches."""
    code = normalise_code(code)
    if len(code) != DIGITS:
        return False
    totp = _totp(device)
    current = _counter(now)
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if device.last_counter is not None and step <= device.last_counter:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            device.last_counter = step
            return True
    return False


def _delay(failures):
    lock = _setting("WEB_MFA_LOCK_SECONDS", 15 * 60)
    return timedelta(seconds=min(2 ** max(failures - 1, 0), lock))


def _attempt(device_pk, check, *, purpose, kind="totp"):
    """One throttled check of ``device_pk`` (R18), in its own transaction holding the device row.

    Returns ``(device, refusal)``; ``refusal`` is None when ``check(device, now)`` accepted the code. A wrong code is
    counted and saved before the refusal is reported, so the caller raises outside this transaction."""
    now = _now()
    max_failures = _setting("WEB_MFA_MAX_FAILURES", 5)
    with transaction.atomic():
        device = MfaDevice.objects.select_for_update().get(pk=device_pk)
        if device.locked_until is not None and device.locked_until <= now:  # a lock that ran out starts again
            device.failures, device.locked_until, device.last_failure_at = 0, None, None
        refusal = None
        if device.locked_until is not None:
            refusal = "locked"
        elif device.failures and device.last_failure_at and now < device.last_failure_at + _delay(device.failures):
            refusal = "throttled"
        else:
            if check(device, now):
                device.failures, device.last_failure_at, device.locked_until = 0, None, None
                device.save(update_fields=["last_counter", "failures", "last_failure_at", "locked_until"])
                return device, None
            device.failures += 1
            device.last_failure_at = now
            if device.failures >= max_failures:
                device.locked_until = now + timedelta(seconds=_setting("WEB_MFA_LOCK_SECONDS", 15 * 60))
                refusal = "locked"
            else:
                refusal = "wrong_code"
        device.save(update_fields=["failures", "last_failure_at", "locked_until"])
        access_services.log(AccessLog.Event.MFA_VERIFY_FAILED, actor=device.user, subject_user=device.user,
                            details={"purpose": purpose, "kind": kind, "reason": refusal,
                                     "failures": device.failures})
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
    RecoveryCode.objects.bulk_create([RecoveryCode(user=user, code_hash=crypto.code_hash(_normalise_recovery(c)))
                                      for c in codes])
    return codes


def confirm_enrolment(user, code):
    """Confirm the pending device with a right code (R16): the device becomes the user's, 10 recovery codes are
    made and returned **once** (only their hashes are kept), ``mfa_enrolled`` is logged."""
    device = pending_device(user)
    if device is None:
        raise MfaError("no_device")
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

def may_reset(actor, user):
    """OQ-11 default: never oneself; a ``managing_partner``; or the ``access_admin`` of an entity where ``user`` holds an
    active membership. A superuser's device is reset only by the operator command."""
    if not web_services.is_web_user(actor) or user is None or actor.pk == user.pk or user.is_superuser:
        return False
    now = access_services.now()
    mine = Membership.objects.active(now).filter(user=actor)
    if mine.filter(role__code="managing_partner").exists():
        return True
    theirs = Membership.objects.active(now).filter(user=user).values_list("entity_id", flat=True)
    return mine.filter(role__code="access_admin", entity_id__in=list(theirs)).exists()


def _remove(user):
    with transaction.atomic():
        device = MfaDevice.objects.select_for_update().filter(user=user).first()
        had = device is not None
        if device is not None:
            device.delete()
        RecoveryCode.objects.filter(user=user).delete()
    return had


def reset_device(actor, user, *, reason):
    """Remove ``user``'s device and recovery codes so they enrol again at their next sign-in (R16). Logged
    ``mfa_reset`` with the reason; refused (``not_allowed``) for oneself or without the right (OQ-11)."""
    if not (reason or "").strip():
        raise MfaError("reason_required")
    if not may_reset(actor, user):
        raise MfaError("not_allowed")
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
