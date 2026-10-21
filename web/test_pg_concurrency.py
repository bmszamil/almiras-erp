"""PostgreSQL only: concurrent sign-in attempts cannot get past the lockout (web.md R18; review item 1).

Ten threads guess one username from one address at the same moment, with the password check slowed down. The
throttle rows are locked before the attempt is counted and the password checked, so at most five passwords are
ever checked (WEB_LOGIN_MAX_FAILURES) and every other attempt is refused as locked.
"""
import threading
import time

import pytest
from django.db import connection, connections
from django.test import RequestFactory

from core.access.testing import make_user
from web import services
from web.models import LoginThrottle

pytestmark = [pytest.mark.skipif(connection.vendor != "postgresql", reason="row locks need PostgreSQL"),
              # Committed transactions: the threads see each other's rows.
              pytest.mark.django_db(transaction=True)]

THREADS = 10


def test_concurrent_wrong_passwords_check_at_most_the_limit(monkeypatch):
    user = make_user("racer")
    user.set_password("made-up-right-password-1!")
    user.save(update_fields=["password"])
    import django.contrib.auth

    original = django.contrib.auth.authenticate

    def slow(*args, **kwargs):
        time.sleep(0.3)  # without the row locks, all ten would pass the lock check during this pause
        return original(*args, **kwargs)

    monkeypatch.setattr(django.contrib.auth, "authenticate", slow)
    barrier = threading.Barrier(THREADS, timeout=30)
    results = [None] * THREADS

    def attempt(i):
        try:
            request = RequestFactory().post("/login/", REMOTE_ADDR="10.20.30.40")
            barrier.wait()
            results[i] = services.sign_in(request, "racer", "wrong")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert all(r is not None for r in results)
    assert sum(r.tried for r in results) == 5  # passwords checked: exactly the limit
    assert sum(bool(r.locked) for r in results) == THREADS - 4  # the fifth failure locks; the rest refused
    assert all(r.user is None for r in results)
    assert max(LoginThrottle.objects.values_list("failures", flat=True)) == 5
