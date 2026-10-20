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
    with pytest.raises(ValidationError) as exc:
        e.full_clean()
    # Reported once, on the field; clean() does not repeat it.
    assert exc.value.message_dict == {"vat_number": [exc.value.message_dict["vat_number"][0]]}
    assert "15" in exc.value.message_dict["vat_number"][0]
    e.clean()  # model-level rules only


def test_vat_error_is_reported_once():
    e = complete_entity(vat_number="123")
    with pytest.raises(ValidationError) as exc:
        e.full_clean()
    assert len(exc.value.messages) == 1


@pytest.mark.parametrize("field,good", [("building_no", "1234"), ("additional_no", "5678"), ("postal_code", "12345")])
def test_digit_validators_reject_trailing_newline(field, good):
    # The RegexValidator alone (not max_length) must reject "1234\n": "$" would accept it, "\\Z" does not.
    from django.core.validators import RegexValidator

    regex = [v for v in Entity._meta.get_field(field).validators if isinstance(v, RegexValidator)]
    assert len(regex) == 1
    regex[0](good)
    with pytest.raises(ValidationError):
        regex[0](good + "\n")


@pytest.mark.parametrize("bad", ["6000000001", "70000A0001", "٧٠٠٠٠٠٠٠٠١"])
def test_unified_number_must_be_digits_starting_with_7(bad):
    e = complete_entity(unified_number=bad)
    with pytest.raises(ValidationError) as exc:
        e.full_clean()
    assert "unified_number" in exc.value.message_dict
    assert "7" in exc.value.message_dict["unified_number"][0]


@pytest.mark.parametrize("bad", ["XX", "sa", "S", "ZZ"])
def test_country_must_be_iso_code(bad):
    e = complete_entity(country=bad)
    with pytest.raises(ValidationError) as exc:
        e.full_clean()
    assert "country" in exc.value.message_dict


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
    assert e.get_legal_form_display() == "Limited liability company / شركة ذات مسؤولية محدودة"
    assert e.legal_form_en == "Limited liability company"
    assert e.legal_form_ar == "شركة ذات مسؤولية محدودة"
    assert (e.vat_return_period_en, e.vat_return_period_ar) == ("Quarterly", "ربع سنوي")
    assert make_entity("BLK").legal_form_ar == ""
    assert (e.country_name_en, e.country_name_ar) == ("Saudi Arabia", "المملكة العربية السعودية")


def _has_arabic(text):
    return any("\u0600" <= ch <= "\u06ff" for ch in str(text))


def test_entity_field_labels_are_bilingual():
    for field in Entity._meta.get_fields():
        if not field.concrete or field.auto_created:
            continue
        en, sep, ar = str(field.verbose_name).partition(" / ")
        assert sep and en.strip() and _has_arabic(ar), f"{field.name}: {field.verbose_name!r}"
        for value, label in field.flatchoices:
            en, sep, ar = str(label).partition(" / ")
            assert sep and en.strip() and _has_arabic(ar), f"{field.name}={value}: {label!r}"
    assert _has_arabic(Entity._meta.verbose_name) and _has_arabic(Entity._meta.verbose_name_plural)


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


def test_legal_form_is_required_to_issue():
    assert "legal_form" in services.ISSUE_REQUIRED_FIELDS
    assert services.entity_ready_to_issue(complete_entity("AAA", legal_form="")) == ["legal_form"]
    assert services.entity_ready_to_issue(complete_entity("BBB", legal_form="corp")) == ["legal_form"]


@pytest.mark.parametrize(
    "field,bad",
    [
        ("building_no", "12a4"),
        ("building_no", "123"),
        ("building_no", "123\n"),
        ("postal_code", "1234"),
        ("additional_no", "99"),
        ("country", "XX"),
        ("country", "sa"),
    ],
)
def test_invalid_stored_field_is_listed(field, bad):
    # AT-48: objects.create() skips full_clean, so readiness runs the validators itself.
    e = complete_entity(**{field: bad})
    assert services.entity_ready_to_issue(e) == [field]


def test_secondary_number_is_optional_but_checked_when_filled():
    # §3.0: additional_no "N" since 2026-10-16 (verification Z3); unified_number optional (R72).
    assert "additional_no" not in services.ISSUE_REQUIRED_FIELDS
    assert services.entity_ready_to_issue(complete_entity("AAA", additional_no="", unified_number="")) == []
    assert services.entity_ready_to_issue(complete_entity("BBB", unified_number="6000000001")) == ["unified_number"]


@pytest.mark.parametrize("field", ["street_ar", "legal_name_en", "city_en", "cr_number"])
def test_whitespace_only_field_is_missing(field):
    e = complete_entity(**{field: "   "})
    assert services.entity_ready_to_issue(e) == [field]


def test_whitespace_vat_number_is_invalid():
    e = complete_entity(vat_number="   ")
    assert "vat_number" in services.entity_ready_to_issue(e)


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
    # 55 + 6 proposed FX (ledger-fx 5.0) + 5 income + 2 WHT (invoicing §5) + 1140 purification bank (D31b)
    # + payroll 1230, 1240, 2330, 2340, 5120 + bai' ajel 1241, 1250, 4610 (invoicing R112, ledger 0021)
    assert Account.objects.filter(entity=e).count() == 77
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


@pytest.mark.parametrize("bad", ["20250101", "2025-W01-1", "2025-1-1", "2025-01-01T00:00", 20250101, ["2025-01-01"]])
def test_setup_rejects_non_iso_dates_bilingually(setup_file, bad):
    with pytest.raises(ValidationError) as exc:
        call_command("setup_almiras", setup_data=setup_file(dict(FAKE_FIELDS, vat_effective_date=bad)))
    message = exc.value.message_dict["vat_effective_date"][0]
    assert "YYYY-MM-DD" in message and "تاريخ" in message
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
