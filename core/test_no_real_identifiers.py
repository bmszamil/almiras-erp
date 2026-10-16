"""R70 / AT-49: real Almiras identifiers live only in setup data.

The identifiers are read from the setup-data file at run time, so this
file itself never contains them. The file is read as raw JSON only; it is
never applied to an entity.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.core.management import call_command

from core.services import ALMIRAS_SETUP_DATA, SETUP_DATA_DIR, SETUP_FIELDS

REPO = Path(__file__).resolve().parent.parent
ROOT_CONFTEST = (REPO / "conftest.py").resolve()
IDENTIFIER_FIELDS = ("unified_number", "cr_number", "vat_number", "short_address")
# Allowed homes for the real values: the setup data itself, the specs that
# record the owner's decision (docs/specs/invoicing.md D10) and the compliance
# reviews that check them. Nothing else under docs/ is exempt.
ALLOWED_DIRS = (
    SETUP_DATA_DIR.resolve(),
    (REPO / "docs" / "specs").resolve(),
    (REPO / "docs" / "compliance").resolve(),
)
# Only used when git is unavailable (the git listing already leaves these out).
FALLBACK_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}
# Identifiers that are plain digit strings of these lengths get a second pass
# with the non-digits removed, so e.g. "1010-000-001" or "3 9999 9999 9000 03" (fake values) match.
# Non-digits are removed inside each digit group (digits joined by up to three
# spaces, dashes, dots, slashes or underscores), not across the whole file:
# joining every digit in a source file matches long numbers by chance.
DIGIT_ONLY_LENGTHS = {10, 15}
# Arabic-Indic and Extended Arabic-Indic digits count as digits too.
_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_NON_DIGIT = re.compile(r"[^0-9]+")
_DIGIT_GROUP = re.compile(r"[0-9](?:[\s\-./_\u00a0\u200f\u200e]{0,3}[0-9])*")


def real_identifiers():
    fields = json.loads(ALMIRAS_SETUP_DATA.read_text(encoding="utf-8"))["fields"]
    values = [fields.get(name, "") for name in IDENTIFIER_FIELDS]
    assert all(values), "Setup data is missing an identifier the scan relies on"
    return values


def _git_files():
    """Tracked plus untracked, not-ignored files (what could be committed), or None without git."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        out = subprocess.run(
            [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=REPO, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [REPO / name for name in out.decode("utf-8", errors="surrogateescape").split("\0") if name]


def _fallback_files():
    for path in REPO.rglob("*"):
        if any(part in FALLBACK_SKIP_DIRS for part in path.relative_to(REPO).parts):
            continue
        yield path


def scanned_files(git_files=_git_files):
    candidates = git_files()
    if candidates is None:
        candidates = _fallback_files()
    for path in candidates:
        if path.is_symlink() or not path.is_file():  # e.g. deleted but still in the index
            continue
        resolved = path.resolve()
        if any(resolved.is_relative_to(d) for d in ALLOWED_DIRS):
            continue
        yield path


def _read_text(path):
    # Binary files (SQLite databases, .mo catalogues, ...) are scanned too:
    # decode leniently so embedded UTF-8 text is still searched.
    return path.read_bytes().decode("utf-8", errors="ignore")


def find_identifiers(text, identifiers):
    """Field names whose real value appears in ``text`` (raw, or digits-only for 10/15-digit values)."""
    digits = None
    found = []
    for name, value in zip(IDENTIFIER_FIELDS, identifiers):
        if value in text:
            found.append(name)
            continue
        if value.isdigit() and len(value) in DIGIT_ONLY_LENGTHS:
            if digits is None:
                digits = "\n".join(_NON_DIGIT.sub("", m) for m in _DIGIT_GROUP.findall(text.translate(_DIGIT_MAP)))
            if value in digits:
                found.append(name)
    return found


def test_no_real_identifiers_outside_setup_data():
    identifiers = real_identifiers()
    hits = []
    for path in scanned_files():
        hits += [f"{path.relative_to(REPO)}: field {name}" for name in find_identifiers(_read_text(path), identifiers)]
    assert not hits, "Real Almiras identifiers found outside setup data (R70):\n" + "\n".join(hits)


def test_scan_covers_test_files():
    files = {p.relative_to(REPO).as_posix() for p in scanned_files()}
    assert {"core/tests.py", "ledger/tests.py", "conftest.py", "core/test_no_real_identifiers.py"} <= files
    assert "docs/ai-agent-gateway.md" in files  # docs/ outside specs and compliance is scanned
    assert not any(f.startswith(("core/setup_data/", "docs/specs/", "docs/compliance/")) for f in files)


def test_scan_falls_back_to_walking_the_tree_without_git():
    files = {p.relative_to(REPO).as_posix() for p in scanned_files(git_files=lambda: None)}
    assert {"core/tests.py", "ledger/tests.py", "conftest.py"} <= files
    assert not any(f.startswith((".git/", "core/setup_data/")) or "__pycache__" in f for f in files)


def test_digits_in_separate_numbers_are_not_joined():
    identifiers = real_identifiers()
    for name, value in zip(IDENTIFIER_FIELDS, identifiers):
        if value.isdigit() and len(value) in DIGIT_ONLY_LENGTHS:
            text = f"a = {value[:4]}\nb = 'x{value[4:]}'\n"
            assert name not in find_identifiers(text, identifiers)


def test_scan_reads_binary_files(tmp_path):
    # .sqlite3 / .mo files are not skipped by suffix; embedded text is still found.
    identifiers = real_identifiers()
    vat = next(v for n, v in zip(IDENTIFIER_FIELDS, identifiers) if n == "vat_number")
    blob = b"SQLite format 3\x00\xff\xfe" + vat.encode() + b"\x00\x01"
    for name in ("sample.sqlite3", "django.mo"):
        path = tmp_path / name
        path.write_bytes(blob)
        assert "vat_number" in find_identifiers(_read_text(path), identifiers)


@pytest.mark.parametrize(
    "spell",
    [
        lambda v: "-".join(v[i:i + 3] for i in range(0, len(v), 3)),
        lambda v: " ".join(v),
        lambda v: v.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")),
        lambda v: f"{v[:3]} . {v[3:6]}_{v[6:]}",
    ],
)
def test_digit_identifiers_are_found_with_separators(spell):
    identifiers = real_identifiers()
    for name, value in zip(IDENTIFIER_FIELDS, identifiers):
        if value.isdigit() and len(value) in DIGIT_ONLY_LENGTHS:
            assert name in find_identifiers(f"x = {spell(value)}\n", identifiers)


def _is_test_file(name):
    return name in ("tests.py", "conftest.py") or (name.startswith("test_") and name.endswith(".py"))


def _reference_offenders(paths):
    this = Path(__file__).resolve()
    offenders = []
    for path in paths:
        resolved = path.resolve()
        # This file reads the setup data on purpose; the repo-root conftest.py
        # names it to guard against loading it. Every other test file is checked.
        if not _is_test_file(path.name) or resolved in (this, ROOT_CONFTEST):
            continue
        text = path.read_text(encoding="utf-8")
        if "almiras.json" in text or "ALMIRAS_SETUP_DATA" in text:
            offenders.append(path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path))
    return offenders


def test_no_other_test_references_real_setup_file():
    offenders = _reference_offenders(scanned_files())
    assert not offenders, f"Tests referencing the real setup data: {offenders}"


def test_nested_conftest_referencing_setup_data_is_reported(tmp_path):
    nested = tmp_path / "conftest.py"
    nested.write_text("from core.services import ALMIRAS_SETUP_DATA\n", encoding="utf-8")
    assert _reference_offenders([nested, REPO / "conftest.py"]) == [str(nested)]


@pytest.mark.django_db
def test_setup_command_refuses_real_data_under_tests():
    with pytest.raises(AssertionError, match="R70"):
        call_command("setup_almiras")


def test_real_setup_file_is_well_formed():
    # Raw JSON read only, to catch typos in keys; never applied to an entity.
    data = json.loads(ALMIRAS_SETUP_DATA.read_text(encoding="utf-8"))
    assert data["code"] == "ALM"
    assert set(data["fields"]) == SETUP_FIELDS
