"""R70 / AT-49: real Almiras identifiers live only in setup data.

The identifiers are read from the setup-data file at run time, so this
file itself never contains them. The file is read as raw JSON only; it is
never applied to an entity.
"""
import json
from pathlib import Path

import pytest
from django.core.management import call_command

from core.services import ALMIRAS_SETUP_DATA, SETUP_DATA_DIR, SETUP_FIELDS

REPO = Path(__file__).resolve().parent.parent
IDENTIFIER_FIELDS = ("unified_number", "cr_number", "vat_number", "short_address")
# Allowed homes for the real values: the setup data itself and the specs that
# record the owner's decision (docs/specs/invoicing.md D10).
ALLOWED_DIRS = (SETUP_DATA_DIR.resolve(), (REPO / "docs").resolve())
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}
SKIP_SUFFIXES = {".pyc", ".sqlite3", ".mo"}


def real_identifiers():
    fields = json.loads(ALMIRAS_SETUP_DATA.read_text(encoding="utf-8"))["fields"]
    values = [fields.get(name, "") for name in IDENTIFIER_FIELDS]
    assert all(values), "Setup data is missing an identifier the scan relies on"
    return values


def scanned_files():
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix in SKIP_SUFFIXES:
            continue
        rel_parts = path.relative_to(REPO).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        resolved = path.resolve()
        if any(resolved.is_relative_to(d) for d in ALLOWED_DIRS):
            continue
        yield path


def test_no_real_identifiers_outside_setup_data():
    identifiers = real_identifiers()
    hits = []
    for path in scanned_files():
        text = path.read_bytes().decode("utf-8", errors="ignore")
        hits += [f"{path.relative_to(REPO)}: field {name}" for name, value in zip(IDENTIFIER_FIELDS, identifiers) if value in text]
    assert not hits, "Real Almiras identifiers found outside setup data (R70):\n" + "\n".join(hits)


def test_scan_covers_test_files():
    files = {p.relative_to(REPO).as_posix() for p in scanned_files()}
    assert {"core/tests.py", "ledger/tests.py", "conftest.py", "core/test_no_real_identifiers.py"} <= files
    assert not any(f.startswith("core/setup_data/") for f in files)


def test_no_other_test_references_real_setup_file():
    this = Path(__file__).resolve()
    offenders = []
    for path in scanned_files():
        name = path.name
        is_test = name in ("tests.py", "conftest.py") or (name.startswith("test_") and name.endswith(".py"))
        if not is_test or path.resolve() == this or name == "conftest.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "almiras.json" in text or "ALMIRAS_SETUP_DATA" in text:
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, f"Tests referencing the real setup data: {offenders}"


@pytest.mark.django_db
def test_setup_command_refuses_real_data_under_tests():
    with pytest.raises(AssertionError, match="R70"):
        call_command("setup_almiras")


def test_real_setup_file_is_well_formed():
    # Raw JSON read only, to catch typos in keys; never applied to an entity.
    data = json.loads(ALMIRAS_SETUP_DATA.read_text(encoding="utf-8"))
    assert data["code"] == "ALM"
    assert set(data["fields"]) == SETUP_FIELDS
