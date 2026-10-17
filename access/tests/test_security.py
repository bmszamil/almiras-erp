"""Compliance and settings items from the 17 Oct 2026 review: tokens, TOTP enrolment, sign-out audit,
lockout, trusted proxy IP, production-safe settings."""
import datetime
import os
import subprocess
import sys

import pytest
from django.conf import settings
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone

from access import services as access
from access.errors import RuleError
from access.models import AuditLog

pytestmark = pytest.mark.django_db
PASSWORD = "a-Long-test-pass-91"


def test_service_token_requires_future_expiry(agent):
    with pytest.raises(RuleError):
        access.issue_service_token(None, agent, expires_at=None)
    with pytest.raises(RuleError):
        access.issue_service_token(None, agent, expires_at=timezone.now() - datetime.timedelta(minutes=1))


def test_service_token_issue_and_revoke_are_audited(agent, mp):
    access.issue_service_token(mp, agent, expires_at=timezone.now() + datetime.timedelta(days=7), label="ci")
    token = agent.service_tokens.get()
    assert AuditLog.objects.filter(action="access.token.issue", actor=mp, object_id=str(token.pk)).exists()
    access.revoke_service_token(mp, token)
    assert AuditLog.objects.filter(action="access.token.revoke", actor=mp, object_id=str(token.pk)).exists()


def test_service_token_issue_needs_user_manage(agent, accountant):
    from access.errors import AccessDenied

    with pytest.raises(AccessDenied):
        access.issue_service_token(accountant, agent, expires_at=timezone.now() + datetime.timedelta(days=1))


def test_expired_token_is_refused(agent):
    from rest_framework.test import APIClient

    key = access.issue_service_token(None, agent, expires_at=timezone.now() + datetime.timedelta(seconds=30))
    token = agent.service_tokens.get()
    token.expires_at = timezone.now() - datetime.timedelta(seconds=1)
    token.save(update_fields=["expires_at"])
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
    assert c.get("/api/v1/me").status_code in (401, 403)


def test_no_tokens_for_humans(mp):
    with pytest.raises(RuleError):
        access.issue_service_token(None, mp, expires_at=timezone.now() + datetime.timedelta(days=1))


def test_totp_enrolment_is_audited_and_human_only(mp, agent):
    access.enroll_totp(mp, mp)
    assert AuditLog.objects.filter(action="access.totp.enroll", object_id=str(mp.pk)).exists()
    with pytest.raises(RuleError):
        access.enroll_totp(None, agent)


def test_setup_totp_command_audits(mp):
    from django.core.management import call_command

    call_command("setup_totp", "mp", stdout=open(os.devnull, "w"))
    assert AuditLog.objects.filter(action="access.totp.enroll", object_id=str(mp.pk)).exists()


def _staff(user):
    user.is_staff = True
    user.set_password(PASSWORD)
    user.save()
    return user


def test_sign_out_is_audited(mp, api):
    _staff(mp)
    c = api(mp)  # password + verified TOTP session
    assert c.post("/admin/logout/").status_code == 200
    assert AuditLog.objects.filter(action="access.sign_out", actor=mp).exists()


def test_lockout_after_five_failed_sign_ins(mp):
    from django.contrib.auth import authenticate

    _staff(mp)
    rf = RequestFactory()
    for _ in range(5):
        assert authenticate(rf.post("/admin/login/"), username="mp", password="wrong") is None
    assert AuditLog.objects.filter(action="access.sign_in_failed").count() == 5
    # Correct password is now refused too, until the window passes.
    assert authenticate(rf.post("/admin/login/"), username="mp", password=PASSWORD) is None
    from access.throttle import is_locked_out

    assert is_locked_out("mp")


def test_four_failures_do_not_lock(mp):
    from django.contrib.auth import authenticate

    _staff(mp)
    rf = RequestFactory()
    for _ in range(4):
        authenticate(rf.post("/admin/login/"), username="mp", password="wrong")
    assert authenticate(rf.post("/admin/login/"), username="mp", password=PASSWORD) == mp


def test_lockout_by_ip(make_user):
    from access.throttle import is_locked_out
    from django.contrib.auth import authenticate

    rf = RequestFactory()
    for i in range(20):
        authenticate(rf.post("/admin/login/", REMOTE_ADDR="203.0.113.9"), username=f"nobody{i}", password="x")
    assert is_locked_out("someone-else", "203.0.113.9")
    assert not is_locked_out("someone-else", "203.0.113.10")


def test_client_ip_ignores_forwarded_for_from_untrusted_peer():
    rf = RequestFactory()
    req = rf.get("/", REMOTE_ADDR="198.51.100.7", HTTP_X_FORWARDED_FOR="1.2.3.4")
    assert access.client_ip(req) == "198.51.100.7"


@override_settings(ERP_TRUSTED_PROXIES=["10.0.0.1"])
def test_client_ip_uses_forwarded_for_from_trusted_proxy():
    rf = RequestFactory()
    req = rf.get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.5, 10.0.0.1")
    assert access.client_ip(req) == "203.0.113.5"  # right-most untrusted hop; spoofed left entries ignored


def test_session_and_cookie_settings():
    assert settings.SESSION_COOKIE_AGE == 1800 and settings.SESSION_SAVE_EVERY_REQUEST is True
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.DEBUG is False and settings.SESSION_COOKIE_SECURE is True and settings.CSRF_COOKIE_SECURE is True
    assert settings.AUTHENTICATION_BACKENDS == ["access.throttle.LockoutModelBackend"]


def _import_settings(env):
    code = "import config.settings as s; print(s.DEBUG)"
    full = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")} | env
    return subprocess.run([sys.executable, "-c", code], cwd=settings.BASE_DIR, env=full, capture_output=True, text=True)


def test_debug_defaults_off_and_secret_key_required():
    r = _import_settings({})
    assert r.returncode != 0 and "DJANGO_SECRET_KEY" in r.stderr
    r = _import_settings({"DJANGO_SECRET_KEY": "x" * 50})
    assert r.returncode == 0 and r.stdout.strip() == "False"
    r = _import_settings({"DJANGO_DEBUG": "1"})
    assert r.returncode == 0 and r.stdout.strip() == "True"
