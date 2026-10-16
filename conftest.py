"""Repository-wide test guards."""
from pathlib import Path

import pytest

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
