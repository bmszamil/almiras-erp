"""Tests for core.Entity registration fields, issue readiness and setup data.

All identifiers here are fake (R70). Real Almiras values live only in
core/setup_data/ and are never loaded by tests.
"""
import json
from datetime import date
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from core import services
from core.admin import EntityAdmin
from core.models import Entity, validate_vat_number
from ledger.models import Account, FiscalPeriod

pytestmark = pytest.mark.django_db

FAKE_VAT = "399999999900003"

FAKE_FIELDS = {
    "legal_name_en": "Example Trading Company",
    "legal_name_ar": "شركة المثال التجارية",
    "legal_form": "llc",
    "unified_number": "7000000001",
    "cr_number": "1010000001",
    "vat_number": FAKE_VAT,
    "vat_effective_date": "2025-01-01",
    "vat_return_period": "monthly",
    "building_no": "1234",
    "street_en": "Example St",
    "street_ar": "شارع المثال",
    "district_en": "Example District",
    "district_ar": "حي المثال",
    "additional_no": "5678",
    "postal_code": "12345",
    "city_en": "Riyadh",
    "city_ar": "الرياض",
    "country": "SA",
    "short_address": "ABCD1234",
}


def make_entity(code="TST", **kw):
    kw.setdefault("name_en", "Test")
    kw.setdefault("name_ar", "اختبار")
    kw.setdefault("consolidation", "parent")
    return Entity.objects.create(code=code, **kw)


def complete_entity(code="TST", **overrides):
    fields = dict(FAKE_FIELDS, vat_effective_date=date(2025, 1, 1))
    fields.update(overrides)
    return make_entity(code, **fields)


@pytest.fixture
def setup_file(tmp_path):
    def write(fields=None, code="ALM"):
        path = tmp_path / "entity.json"
        path.write_text(json.dumps({"code": code, "fields": fields or FAKE_FIELDS}, ensure_ascii=False), encoding="utf-8")
        return str(path)
    return write


# --- VAT number format (§3.0; pending ZATCA BR-KSA verification) ---------

def test_valid_vat_number_accepted():
    validate_vat_number(FAKE_VAT)
    validate_vat_number("")  # optional at DB level
    e = complete_entity()
    e.full_clean()


@pytest.mark.parametrize(
    "bad",
    [
        "39999999990003",     # 14 digits
        "3999999999000003",   # 16 digits
        "199999999900003",    # does not start with 3
        "399999999900001",    # does not end with 3
        "39999999990000A",    # non-digit
        "3999999 9900003",    # space
        "٣٩٩٩٩٩٩٩٩٩٠٠٠٠٣",    # Arabic-Indic digits are not accepted
    ],
)
def test_invalid_vat_number_rejected(bad):
    with pytest.raises(ValidationError):
        validate_vat_number(bad)
    e = complete_entity(vat_number=bad[:15])
    with pytest.raises(ValidationError):
        e.full_clean()
    with pytest.raises(ValidationError):
        e.clean()


@pytest.mark.parametrize(
    "field,bad",
    [("building_no", "123"), ("building_no", "12a4"), ("additional_no", "99"), ("postal_code", "1234"), ("postal_code", "ABCDE")],
)
def test_address_number_formats_rejected(field, bad):
    e = complete_entity(**{field: bad})
    with pytest.raises(ValidationError) as exc:
        e.full_clean()
    assert field in exc.value.message_dict


def test_existing_style_entity_without_new_fields_still_valid():
    e = make_entity()
    e.full_clean()
    assert e.country == "SA" and e.legal_name_en == ""


def test_bilingual_display_helpers():
    e = complete_entity(legal_form="llc", vat_return_period="quarterly")
    assert e.get_legal_form_display() == "Limited liability company"
    assert e.legal_form_ar == "شركة ذات مسؤولية محدودة"
    assert e.vat_return_period_ar == "ربع سنوي"
    assert (e.country_name_en, e.country_name_ar) == ("Saudi Arabia", "المملكة العربية السعودية")


# --- R69 / AT-48: entity_ready_to_issue ----------------------------------

def test_complete_entity_is_ready():
    e = complete_entity()
    assert services.entity_ready_to_issue(e) == []
    assert services.entity_ready_to_issue(e, standard_rated=True) == []


def test_blank_entity_lists_every_missing_field():
    e = make_entity()
    missing = services.entity_ready_to_issue(e)
    assert set(missing) == set(services.ISSUE_REQUIRED_FIELDS) - {"country"}
    assert "vat_number" not in missing  # only needed for S lines


@pytest.mark.parametrize("field", ["building_no", "street_ar"])
def test_missing_address_field_is_listed(field):
    e = complete_entity(**{field: ""})
    assert services.entity_ready_to_issue(e) == [field]


def test_standard_rated_needs_vat_registration():
    e = complete_entity(vat_number="", vat_effective_date=None, vat_return_period="")
    assert services.entity_ready_to_issue(e) == []
    assert services.entity_ready_to_issue(e, standard_rated=True) == ["vat_number", "vat_effective_date", "vat_return_period"]


def test_vat_number_needs_effective_date_and_period():
    e = complete_entity(vat_effective_date=None, vat_return_period="")
    assert services.entity_ready_to_issue(e) == ["vat_effective_date", "vat_return_period"]


def test_invalid_stored_vat_number_is_listed():
    e = complete_entity(vat_number="123")  # bypasses full_clean via create()
    assert services.entity_ready_to_issue(e) == ["vat_number"]


# --- Setup data and setup_almiras command (D10) --------------------------

def test_setup_command_sets_fields(setup_file):
    call_command("setup_almiras", setup_data=setup_file())
    e = Entity.objects.get(code="ALM")
    assert e.legal_name_ar == FAKE_FIELDS["legal_name_ar"]
    assert e.vat_number == FAKE_VAT and e.vat_effective_date == date(2025, 1, 1)
    assert e.short_address == "ABCD1234" and e.consolidation == Entity.Consolidation.PARENT
    assert services.entity_ready_to_issue(e, standard_rated=True) == []


def test_setup_command_is_idempotent(setup_file):
    path = setup_file()
    call_command("setup_almiras", setup_data=path)
    first = Entity.objects.filter(code="ALM").values().get()
    call_command("setup_almiras", setup_data=path)
    assert Entity.objects.filter(code="ALM").count() == 1
    assert Entity.objects.filter(code="ALM").values().get() == first
    e = Entity.objects.get(code="ALM")
    assert Account.objects.filter(entity=e).count() == 55
    assert FiscalPeriod.objects.filter(entity=e).count() == 12


def test_setup_command_updates_existing_entity(setup_file):
    e = make_entity("ALM", name_en="Almiras", name_ar="المراس")
    call_command("setup_almiras", setup_data=setup_file())
    e.refresh_from_db()
    assert e.legal_name_en == FAKE_FIELDS["legal_name_en"]
    assert e.name_en == "Almiras"  # display name untouched
    assert Entity.objects.count() == 1


def test_setup_command_rejects_invalid_data_and_changes_nothing(setup_file):
    with pytest.raises(ValidationError):
        call_command("setup_almiras", setup_data=setup_file(dict(FAKE_FIELDS, vat_number="123456789012345")))
    assert not Entity.objects.exists()


def test_setup_data_rejects_unknown_fields(setup_file):
    with pytest.raises(ValidationError, match="unknown"):
        call_command("setup_almiras", setup_data=setup_file(dict(FAKE_FIELDS, base_currency="USD")))
    assert not Entity.objects.exists()


def test_setup_command_rejects_invalid_date_bilingually(setup_file):
    with pytest.raises(ValidationError) as exc:
        call_command("setup_almiras", setup_data=setup_file(dict(FAKE_FIELDS, vat_effective_date="2025-13-01")))
    message = exc.value.message_dict["vat_effective_date"][0]
    assert "ISO date" in message and "تاريخ" in message
    assert not Entity.objects.exists()


def test_setup_command_reports_created_updated_unchanged(setup_file):
    out = StringIO()
    path = setup_file()
    call_command("setup_almiras", setup_data=path, stdout=out)
    assert out.getvalue().startswith("Created")
    out = StringIO()
    call_command("setup_almiras", setup_data=path, stdout=out)
    assert out.getvalue().startswith("Unchanged ALM")
    out = StringIO()
    call_command("setup_almiras", setup_data=setup_file(dict(FAKE_FIELDS, street_en="New St")), stdout=out)
    assert out.getvalue().startswith("Updated (street_en)")


def test_setup_command_help_says_file_overwrites_admin_edits():
    from ledger.management.commands.setup_almiras import Command

    assert "source of truth" in Command.help and "overwrites" in Command.help


# --- Entity separation ----------------------------------------------------

@pytest.mark.parametrize("code", ["LZM", "alm", ""])
def test_setup_file_for_another_entity_is_rejected_and_changes_nothing(setup_file, code):
    alm = make_entity("ALM", name_en="Almiras", name_ar="المراس")
    other = make_entity("LZM", name_en="Other", name_ar="أخرى", consolidation="full", parent=alm)
    before = {e["code"]: e for e in Entity.objects.values()}
    with pytest.raises(ValidationError):
        call_command("setup_almiras", setup_data=setup_file(code=code))
    assert {e["code"]: e for e in Entity.objects.values()} == before
    assert not Account.objects.filter(entity__in=[alm, other]).exists()


def test_setup_file_for_unknown_entity_creates_nothing(setup_file):
    with pytest.raises(ValidationError, match="only sets up 'ALM'"):
        call_command("setup_almiras", setup_data=setup_file(code="NEW"))
    assert not Entity.objects.exists()


def test_setup_touches_only_its_entity(setup_file):
    other = make_entity("LZM", name_en="Other", name_ar="أخرى", consolidation="full", parent=make_entity("ALM"))
    before = Entity.objects.filter(pk=other.pk).values().get()
    call_command("setup_almiras", setup_data=setup_file())
    assert Entity.objects.filter(pk=other.pk).values().get() == before
    assert services.entity_ready_to_issue(other) != []
    assert services.entity_ready_to_issue(Entity.objects.get(code="ALM")) == []


def test_readiness_is_per_entity():
    a = complete_entity("AAA")
    b = complete_entity("BBB", street_ar="")
    assert services.entity_ready_to_issue(a) == []
    assert services.entity_ready_to_issue(b) == ["street_ar"]


# --- Admin ------------------------------------------------------------------

def test_admin_fieldsets_cover_every_editable_field():
    listed = set()
    for _, opts in EntityAdmin.fieldsets:
        for f in opts["fields"]:
            listed.update(f if isinstance(f, tuple) else [f])
    editable = {f.name for f in Entity._meta.get_fields() if getattr(f, "editable", False) and f.concrete and not f.auto_created}
    assert listed == editable


def test_admin_change_page_renders(admin_client):
    e = complete_entity()
    resp = admin_client.get(f"/admin/core/entity/{e.pk}/change/")
    assert resp.status_code == 200
    assert "العنوان الوطني" in resp.content.decode()
