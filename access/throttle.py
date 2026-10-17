"""Sign-in lockout (review item 11): after LOCKOUT_FAILURES failed sign-ins for a username within
LOCKOUT_MINUTES (or IP_FAILURES from one address), password checks are refused until the window passes.

Failures are counted from the append-only audit log (`access.sign_in_failed`), so the evidence and
the lockout use the same records. Attempts refused while locked are logged as `access.sign_in_locked`
and do not count, so the lock ends LOCKOUT_MINUTES after the last real failure. A successful sign-in
or `access.unlock` (command `unlock_user` / admin action) resets the per-username count. TOTP tokens are additionally throttled per device by django-otp.
"""
import datetime

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from .models import AuditLog


def _limits():
    return (
        getattr(settings, "ERP_LOCKOUT_FAILURES", 5),
        getattr(settings, "ERP_LOCKOUT_IP_FAILURES", 20),
        getattr(settings, "ERP_LOCKOUT_MINUTES", 15),
    )


def is_locked_out(username, ip=""):
    per_user, per_ip, minutes = _limits()
    since = timezone.now() - datetime.timedelta(minutes=minutes)
    failed = AuditLog.objects.filter(action="access.sign_in_failed", at__gte=since)
    resets = [
        AuditLog.objects.filter(action="access.sign_in", at__gte=since, actor__username=username)
        .order_by("-at").values_list("at", flat=True).first(),
        AuditLog.objects.filter(action="access.unlock", at__gte=since, after__username=username)
        .order_by("-at").values_list("at", flat=True).first(),
    ]
    last_ok = max([r for r in resets if r is not None], default=None)
    user_failures = failed.filter(after__username=username)
    if last_ok:
        user_failures = user_failures.filter(at__gt=last_ok)
    if user_failures.count() >= per_user:
        return True
    return bool(ip) and failed.filter(ip=ip).count() >= per_ip


class LockoutModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        from .services import client_ip

        ip = client_ip(request) if request is not None else ""
        if username and is_locked_out(username, ip):
            raise PermissionDenied("Too many failed sign-ins. Try again later.")  # stops all backends
        return super().authenticate(request, username=username, password=password, **kwargs)
