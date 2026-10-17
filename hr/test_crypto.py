"""hr R9, R10 (review finding 8; probe P11): encryption keys, the stable ID-number hash, rotation and re-hashing,
and the bilingual checks and command (nit 22)."""
from io import StringIO

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection

from hr import checks, crypto, jobs
from hr.conftest import employee
from hr.models import Employee, HrError

TEST_KEY = "test-only-hr-personal-data-key-not-secret"


def _stored(emp, column="id_number_hash"):
    with connection.cursor() as cur:
        cur.execute(f"SELECT {column} FROM hr_employee WHERE id = %s", [emp.pk])
        return cur.fetchone()[0]


def test_no_key_means_no_encryption_unless_the_dev_key_is_opted_in(settings):
    settings.HR_PERSONAL_DATA_KEYS = []
    settings.HR_ALLOW_DEV_KEY = False
    with pytest.raises(ImproperlyConfigured) as exc:
        crypto.encrypt("x")
    assert "HR_ALLOW_DEV_KEY" in str(exc.value) and "/" in str(exc.value)
    settings.HR_ALLOW_DEV_KEY = True  # explicit opt-in only
    assert crypto.decrypt(crypto.encrypt("x")) == "x"


def test_checks_refuse_missing_keys_and_the_dev_key_in_production(settings):
    settings.HR_PERSONAL_DATA_KEYS, settings.HR_ALLOW_DEV_KEY, settings.DEBUG = [], False, False
    assert [m.id for m in checks.personal_data_key_configured()] == ["hr.E001"]
    settings.DEBUG = True
    assert [m.id for m in checks.personal_data_key_configured()] == ["hr.W001"]
    settings.DEBUG, settings.HR_ALLOW_DEV_KEY = False, True
    assert [m.id for m in checks.personal_data_key_configured()] == ["hr.E002"]
    settings.HR_ALLOW_DEV_KEY, settings.HR_PERSONAL_DATA_KEYS, settings.HR_PERSONAL_DATA_HASH_KEY = False, ["k"], ""
    found = checks.personal_data_key_configured()
    assert [m.id for m in found] == ["hr.W002"] and "/" in found[0].msg  # English and Arabic
    settings.HR_PERSONAL_DATA_HASH_KEY = "stable"
    assert checks.personal_data_key_configured() == []


def test_p11_rotation_keeps_id_uniqueness_and_rehash_moves_rows_to_the_new_key(w, settings):
    first = employee(w, id_number="1999999991")
    old_hash = _stored(first)
    settings.HR_PERSONAL_DATA_KEYS = ["new-key-after-rotation", TEST_KEY]
    assert Employee.objects.get(pk=first.pk).full_name_en == "Test Person"  # old rows still decrypt
    with pytest.raises(HrError) as exc:
        employee(w, id_number="1999999991")
    assert exc.value.code == "id_duplicate"
    out = StringIO()
    call_command("hr_rehash_personal_data", stdout=out)
    assert "/" in out.getvalue() and "employee" in out.getvalue()
    assert _stored(first) != old_hash and _stored(first) == crypto.keyed_hash("1999999991")
    settings.HR_PERSONAL_DATA_KEYS = ["new-key-after-rotation"]  # the old key is retired
    assert Employee.objects.get(pk=first.pk).full_name_en == "Test Person"
    with pytest.raises(HrError):
        employee(w, id_number="1999999991")


def test_stable_hash_key_survives_rotation(w, settings):
    settings.HR_PERSONAL_DATA_HASH_KEY = "stable-test-hash-key"
    jobs.rehash_personal_data()
    emp = employee(w, id_number="1999999992")
    stable = _stored(emp)
    settings.HR_PERSONAL_DATA_KEYS = ["rotated-key", TEST_KEY]
    jobs.rehash_personal_data()
    assert _stored(emp) == stable
    assert _stored(emp, "full_name_en").startswith(crypto.PREFIX)
    settings.HR_PERSONAL_DATA_KEYS = ["rotated-key"]
    assert Employee.objects.get(pk=emp.pk).full_name_en == "Test Person"  # re-encrypted under the new key
    with pytest.raises(HrError):
        employee(w, id_number="1999999992")
