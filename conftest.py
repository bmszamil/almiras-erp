"""Repository-wide test guards."""
import os
from pathlib import Path

import pytest
from django.conf import settings

import core.services
import ledger.management.commands.setup_almiras as setup_cmd


@pytest.fixture(autouse=True)
def _never_load_real_setup_data(monkeypatch):
    """R70 / AT-49: tests never load the real Almiras setup-data file.

    Tests that exercise the setup command must pass an anonymised file
    with --setup-data.
    """
    real = Path(core.services.ALMIRAS_SETUP_DATA).resolve()
    original = core.services.load_setup_data

    def guarded(path):
        if Path(path).resolve() == real:
            raise AssertionError("Tests must not load the real Almiras setup data (R70); pass --setup-data.")
        return original(path)

    monkeypatch.setattr(core.services, "load_setup_data", guarded)
    monkeypatch.setattr(setup_cmd, "load_setup_data", guarded)



@pytest.fixture(autouse=True)
def _access_log_connection_only_when_declared(request):
    """Use the separate access-log connection (settings.ACCESS_LOG_DB) only in tests that declare it.

    In an ordinary database test everything is inside one uncommitted test
    transaction, so a second connection could not see the actor or entity
    anyway and log_denial would fall back to the main connection. Django's
    TestCase also checks constraints on every declared connection at
    teardown, which a second SQLite connection cannot do while the test
    transaction holds its locks. Tests of the rollback behaviour use
    ``@pytest.mark.django_db(transaction=True, databases=["default", "access_log"])``.
    """
    alias = getattr(settings, "ACCESS_LOG_DB", None)
    marker = request.node.get_closest_marker("django_db")
    declared = marker.kwargs.get("databases") if marker else None
    if alias and not (declared == "__all__" or (declared and alias in declared)):
        request.getfixturevalue("settings").ACCESS_LOG_DB = None


# --- CI: the PostgreSQL job must run the PostgreSQL-only tests (REQUIRE_POSTGRES=1) ---------------
# The trigger and locking tests skip on SQLite. In the PostgreSQL job a wrong database setting would
# silently skip them and still pass, so there the run fails unless the database is PostgreSQL and no
# test was skipped.

REQUIRE_POSTGRES = os.environ.get("REQUIRE_POSTGRES") == "1"
_skipped = []


def pytest_sessionstart(session):
    if REQUIRE_POSTGRES:
        from django.db import connection

        if connection.vendor != "postgresql":
            raise pytest.UsageError(f"REQUIRE_POSTGRES=1 but the test database is {connection.vendor}.")


def pytest_runtest_logreport(report):
    if REQUIRE_POSTGRES and report.skipped and not hasattr(report, "wasxfail"):
        _skipped.append(report.nodeid)


def pytest_terminal_summary(terminalreporter):
    if REQUIRE_POSTGRES and _skipped:
        terminalreporter.write_line(
            f"REQUIRE_POSTGRES=1: {len(_skipped)} test(s) skipped; none may skip on PostgreSQL:", red=True
        )
        for nodeid in _skipped:
            terminalreporter.write_line(f"  {nodeid}", red=True)


def pytest_sessionfinish(session, exitstatus):
    if REQUIRE_POSTGRES and _skipped and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
