"""Repository-wide test guards."""
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
