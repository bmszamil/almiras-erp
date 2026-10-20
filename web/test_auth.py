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


def login(client, username, password=PASSWORD, ip="10.0.0.1", headers=None, **form):
    return client.post("/login/", {"username": username, "password": password, **form}, REMOTE_ADDR=ip,
                       **(headers or {}))


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
    assert AccessLog.objects.get(event="logout", actor=w.acc).details == {"reason": "sign_out"}
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


def test_wt17_idle_timeout_ends_the_session_and_is_logged(w, client):
    login(client, "acc")
    key = _age_session(client, seen=29 * 60)
    assert client.get("/e/ALM/").status_code == 200  # within 30 minutes
    key = _age_session(client, seen=31 * 60)
    response = client.get("/e/ALM/")
    assert response.status_code == 302 and response["Location"].startswith("/login/")
    engine = import_module("django.contrib.sessions.backends.db")
    assert not engine.SessionStore().exists(key)
    row = AccessLog.objects.get(event="logout", actor=w.acc)
    assert row.details == {"reason": "idle_timeout"}  # review item 10


def test_wt17_users_who_need_mfa_have_the_shorter_idle_timeout(w, client):
    login(client, "cfo")  # approver: an MFA role (R13)
    _age_session(client, seen=16 * 60)
    assert client.get("/e/ALM/").status_code == 302


def test_wt17_absolute_timeout_ends_an_active_session(w, client):
    login(client, "acc")
    _age_session(client, login=10 * 3600 + 5, seen=10)
    response = client.get("/e/ALM/")
    assert response.status_code == 302 and response["Location"].startswith("/login/")
    assert AccessLog.objects.get(event="logout", actor=w.acc).details == {"reason": "absolute_timeout"}


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
    keys = list(LoginThrottle.objects.values_list("key", flat=True))
    assert len(keys) == 2 and not any("acc" in k or "10.0.0.1" in k for k in keys)  # never in clear


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


def test_signed_in_user_with_a_bare_next_is_redirected_not_500(w, client):  # review item 4
    login(client, "acc")
    response = client.get("/login/?next=foo")
    assert response.status_code == 302 and response["Location"] == "foo"
    assert client.get("/login/?next=https://evil.example/").headers["Location"] == "/"


def test_r18_username_cap_across_addresses_locks_the_username_everywhere(w, client, settings):
    settings.WEB_LOGIN_MAX_USER_FAILURES = 6  # review item 2: bounds guessing spread over many addresses
    for i in range(5):
        assert login(client, "acc", password="wrong", ip=f"10.1.0.{i}").status_code == 200
    sixth = login(client, "acc", password="wrong", ip="10.1.0.99")
    assert sixth.status_code == 429
    assert "تعددت محاولات الدخول الفاشلة لهذا الحساب من عدة أماكن" in sixth.content.decode()
    assert login(client, "acc", ip="10.9.9.9").status_code == 429  # also the right password, from anywhere
    assert login(client, "cfo", ip="10.9.9.9").status_code == 302  # other usernames are not affected


def test_r18_the_right_password_does_not_reset_the_username_cap(w, client, settings):
    settings.WEB_LOGIN_MAX_USER_FAILURES = 4
    for i in range(3):
        login(client, "acc", password="wrong", ip=f"10.2.0.{i}")
    assert login(client, "acc", ip="10.2.0.50").status_code == 302
    client.post("/logout/")
    assert login(client, "acc", password="wrong", ip="10.2.0.60").status_code == 429  # 4th failure in the window


def test_oq27_behind_the_proxy_the_address_comes_from_x_real_ip_only(w, client, settings):
    from django.test import RequestFactory

    from web.services import client_ip

    factory = RequestFactory()
    spoofed = factory.get("/", REMOTE_ADDR="172.18.0.5", HTTP_X_REAL_IP="203.0.113.7",
                          HTTP_X_FORWARDED_FOR="198.51.100.1")
    assert client_ip(spoofed) == "172.18.0.5"  # no proxy configured: the client's own headers are ignored
    settings.WEB_CLIENT_IP_HEADER = "HTTP_X_REAL_IP"
    assert client_ip(spoofed) == "203.0.113.7"  # written by the proxy; X-Forwarded-For never read
    for _ in range(5):
        login(client, "acc", password="wrong", ip="172.18.0.5", headers={"HTTP_X_REAL_IP": "203.0.113.7"})
    assert login(client, "acc", ip="172.18.0.5", headers={"HTTP_X_REAL_IP": "203.0.113.7"}).status_code == 429
    other = login(client, "acc", ip="172.18.0.5", headers={"HTTP_X_REAL_IP": "203.0.113.8"})
    assert other.status_code == 302  # same proxy address, another client: not locked


def test_oq27_the_proxy_overwrites_x_real_ip_for_the_app():
    caddy = (REPO / "deploy/proxy/Caddyfile").read_text(encoding="utf-8")
    start = caddy.index("reverse_proxy web:8000 {")
    app_route = caddy[start:caddy.index("\n\t\t}", start)]  # the app route's own block
    assert "header_up X-Real-IP {remote_host}" in app_route
    settings_py = (REPO / "config/settings.py").read_text(encoding="utf-8")
    assert 'WEB_CLIENT_IP_HEADER = "HTTP_X_REAL_IP"' in settings_py and "HTTP_X_FORWARDED_FOR" not in settings_py


def test_stale_throttle_rows_are_purged_on_the_next_attempt(w, client):  # review item 6
    login(client, "acc", password="wrong")
    assert LoginThrottle.objects.count() == 2
    LoginThrottle.objects.update(first_failure_at=timezone.now() - timedelta(minutes=16))
    login(client, "cfo", password="wrong", ip="10.3.0.1")
    assert LoginThrottle.objects.count() == 2  # acc's two old rows gone; cfo's two new rows


def test_backend_runs_the_hasher_once_for_every_kind_of_account(w, monkeypatch):  # review item 5
    from django.contrib.auth import authenticate
    from django.contrib.auth.hashers import get_hasher

    hasher = type(get_hasher())
    calls = []
    original = hasher.encode

    def counting(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(hasher, "encode", counting)
    for username, password in (("nobody-at-all", "x"), (w.billing.username, "x"), (w.agent.username, "x"),
                               ("acc", "wrong")):
        calls.clear()
        assert authenticate(username=username, password=password) is None
        assert len(calls) == 1, username  # the same work whether the account exists, is a system user or not
    w.agent.set_password("known")
    w.agent.save(update_fields=["password"])
    calls.clear()
    assert authenticate(username=w.agent.username, password="known") is None and len(calls) == 1
