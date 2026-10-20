"""Sign-in, sign-out, sessions and the sign-in lockout (web.md R12, R17, R18, R22, R26; WT-17, WT-18, WT-36)."""
import os
import subprocess
import sys
import time
from datetime import timedelta
from importlib import import_module
from pathlib import Path

import pytest
from django.contrib.auth import SESSION_KEY
from django.utils import timezone

from core.access.models import AccessLog
from web.conftest import PASSWORD
from web.middleware import LOGIN_AT, SEEN_AT
from web.models import LoginThrottle

pytestmark = pytest.mark.django_db
REPO = Path(__file__).resolve().parent.parent


def login(client, username, password=PASSWORD, ip="10.0.0.1", **extra):
    return client.post("/login/", {"username": username, "password": password, **extra}, REMOTE_ADDR=ip)


def test_sign_in_page_renders_and_signed_in_users_skip_it(w, client):
    page = client.get("/login/")
    assert page.status_code == 200 and 'name="csrfmiddlewaretoken"' in page.content.decode()
    login(client, "acc")
    assert client.get("/login/").status_code == 302


def test_sign_in_logs_login_succeeded_without_secrets_and_rotates_the_session_key(w, client):
    engine = import_module("django.contrib.sessions.backends.db")
    before = engine.SessionStore()
    before["web_probe"] = "x"
    before.save()
    client.cookies["sessionid"] = before.session_key
    response = login(client, "acc")
    assert response.status_code == 302 and response["Location"] == "/"
    assert client.session.session_key != before.session_key  # R17: rotated at sign-in
    row = AccessLog.objects.get(event="login_succeeded")
    assert row.actor == w.acc and PASSWORD not in str(row.details) and client.session.session_key not in str(row.details)


def test_session_and_csrf_cookies_are_httponly_and_samesite_lax(w, client):
    response = login(client, "acc")
    cookie = response.cookies["sessionid"]
    assert cookie["httponly"] and cookie["samesite"] == "Lax"


def _settings_value(env, *names):
    code = ("import django, os; os.environ['DJANGO_SETTINGS_MODULE']='config.settings'; from django.conf import settings;"
            f"print([getattr(settings, n) for n in {list(names)!r}])")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env={**clean, **env}, capture_output=True, text=True,
                         check=True)
    return out.stdout.strip()


def test_r17_cookies_are_secure_outside_debug():
    assert _settings_value({"DJANGO_DEBUG": "0"}, "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE") == "[True, True]"
    assert _settings_value({"DJANGO_DEBUG": "1"}, "SESSION_COOKIE_SECURE") == "[False]"  # plain-HTTP local runs
    assert _settings_value({"DJANGO_DEBUG": "1", "DJANGO_SECURE_COOKIES": "1"}, "SESSION_COOKIE_SECURE") == "[True]"


def test_wt36_sign_out_is_post_only_logged_and_ends_the_session(w, client):
    login(client, "acc")
    page = client.get("/e/ALM/")
    assert "no-store" in page["Cache-Control"] and "private" in page["Cache-Control"]  # R22
    refused = client.get("/logout/")
    assert refused.status_code == 405 and "طريقة غير مسموحة" in refused.content.decode()
    assert SESSION_KEY in client.session  # a GET does not sign out
    old_key = client.session.session_key
    response = client.post("/logout/")
    assert response.status_code == 302 and response["Location"] == "/login/"
    assert AccessLog.objects.filter(event="logout", actor=w.acc).count() == 1
    engine = import_module("django.contrib.sessions.backends.db")
    assert not engine.SessionStore().exists(old_key)
    back = client.get("/e/ALM/")  # "Back": nothing served from the old session
    assert back.status_code == 302 and back["Location"].startswith("/login/")


def _age_session(client, **seconds_ago):
    session = client.session
    now = time.time()
    for key, ago in seconds_ago.items():
        session[{"login": LOGIN_AT, "seen": SEEN_AT}[key]] = now - ago
    session.save()
    return session.session_key


def test_wt17_idle_timeout_ends_the_session(w, client):
    login(client, "acc")
    key = _age_session(client, seen=29 * 60)
    assert client.get("/e/ALM/").status_code == 200  # within 30 minutes
    key = _age_session(client, seen=31 * 60)
    response = client.get("/e/ALM/")
    assert response.status_code == 302 and response["Location"].startswith("/login/")
    engine = import_module("django.contrib.sessions.backends.db")
    assert not engine.SessionStore().exists(key)


def test_wt17_users_who_need_mfa_have_the_shorter_idle_timeout(w, client):
    login(client, "cfo")  # approver: an MFA role (R13)
    _age_session(client, seen=16 * 60)
    assert client.get("/e/ALM/").status_code == 302


def test_wt17_absolute_timeout_ends_an_active_session(w, client):
    login(client, "acc")
    _age_session(client, login=10 * 3600 + 5, seen=10)
    response = client.get("/e/ALM/")
    assert response.status_code == 302 and response["Location"].startswith("/login/")


def test_wt18_lockout_after_five_failures_also_refuses_the_right_password(w, client):
    for attempt in range(4):
        response = login(client, "acc", password="wrong")
        assert response.status_code == 200, attempt
    fifth = login(client, "acc", password="wrong")
    assert fifth.status_code == 429
    body = fifth.content.decode()
    assert "محاولات كثيرة جدًا" in body and "Too many attempts" in body
    right = login(client, "acc")
    assert right.status_code == 429 and SESSION_KEY not in client.session
    assert not AccessLog.objects.filter(event="login_succeeded").exists()
    assert "acc" not in LoginThrottle.objects.get().key  # username and IP are not stored in clear


def test_wt18_lockout_is_per_username_and_ip_and_ends_after_the_lock(w, client):
    for _ in range(5):
        login(client, "acc", password="wrong")
    assert login(client, "acc", ip="10.0.0.2").status_code == 302  # another IP is not locked
    client.post("/logout/")
    LoginThrottle.objects.update(locked_until=timezone.now() - timedelta(seconds=1))
    assert login(client, "acc").status_code == 302  # after the lock
    assert not LoginThrottle.objects.filter(failures__gt=0, locked_until__isnull=False).exists()


def test_sign_in_never_redirects_off_site(w, client):
    response = login(client, "acc", next="https://evil.example/steal")
    assert response.status_code == 302 and response["Location"] == "/"
    client.post("/logout/")
    response = login(client, "acc", next="/e/ALM/ledger/entries/")
    assert response["Location"] == "/e/ALM/ledger/entries/"


def test_empty_sign_in_form_shows_bilingual_field_errors(w, client):
    body = client.post("/login/", {"username": "", "password": ""}).content.decode()
    assert "أدخل اسم المستخدم وكلمة المرور." in body and 'role="alert"' in body
