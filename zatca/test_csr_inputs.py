"""CSR input checks taken from ZATCA SDK R3.4.8 / R4.0.0 ``CsrGenerationService`` (findings 2026-10-17 §3.6, §7.3;
zatca §3.2, AT-78 to AT-80): no "=" in the EGS serial; none of ``! @ # $ % & * _ <`` in CN, OU, O, location and
industry; for a VAT group (11th digit of the VAT number 1) OU is the member's 10-digit TIN. Refused when the unit is
created and again before every CSR, with English and Arabic messages naming the field."""
import pytest
from cryptography.x509.oid import NameOID

from zatca import rules, services
from zatca.conftest import keys, onboard
from zatca.messages import ZatcaError
from zatca.models import EgsUnit, OnboardingEvent

pytestmark = pytest.mark.django_db

CLEAN = {"common_name": "EGS-EGS1-311111111100003", "org_unit": "Jeddah Branch", "org_name": "TST1 Test Company",
         "location": "RRRD2929", "industry": "Real estate"}


def _create(zw, entity=None, **over):
    fields = dict(code="EGS1", name_en="EGS", name_ar="وحدة", serial_number="1-TestERP|2-1.0|3-EGS1",
                  industry="Real estate", location="RRRD2929", branch_name_en="Jeddah Branch")
    fields.update(over)
    return services.create_egs_unit(entity or zw.tst1, user=zw.admin1, **fields)


def _ready(zw, entity=None):
    entity = entity or zw.tst1
    keys(zw, entity, zw.admin1)
    services.create_profile(entity, user=zw.admin1)


def _set_entity(entity, **fields):
    type(entity).objects.filter(pk=entity.pk).update(**fields)
    entity.refresh_from_db()


# --- serial: no "=" (AT-78) ----------------------------------------------------------------------------------

@pytest.mark.parametrize("serial", ["1-Test=ERP|2-1.0|3-1", "1-TestERP|2-1.0|3-a=b", "1-a|2-b|3-c="])
def test_at78_serial_with_equals_is_refused(zw, serial):
    with pytest.raises(ZatcaError) as exc:
        _create(zw, serial_number=serial)
    assert exc.value.code == "serial" and "'='" in exc.value.en and "'='" in exc.value.ar
    assert not EgsUnit.objects.filter(entity=zw.tst1).exists()


def test_at78_serial_regex_refuses_equals_and_keeps_the_format():
    assert rules.SERIAL_RE.match("1-TestERP|2-1.0|3-TST1-EGS1")
    assert rules.SERIAL_RE.match("1-حل|2-طراز|3-١٢٣")  # free text apart from "|" and "="
    for bad in ("1-a=|2-b|3-c", "1-a|2-b|3-c=d", "1-a|2-b", "2-a|1-b|3-c", "1-a|2-b|3-c|4-d"):
        assert not rules.SERIAL_RE.match(bad), bad


def test_at78_serial_format_error_still_reported(zw):
    with pytest.raises(ZatcaError) as exc:
        _create(zw, serial_number="TestERP-1")
    assert exc.value.code == "serial" and "1-<solution>" in exc.value.en


# --- special characters in CN, OU, O, location, industry (AT-79) ----------------------------------------------

@pytest.mark.parametrize("char", list("!@#$%&*_<"))
@pytest.mark.parametrize("field,key,label_en,label_ar", [
    ("branch_name_en", "org_unit", "branch name (OU)", "اسم الفرع"),
    ("location", "location", "location", "الموقع"),
    ("industry", "industry", "industry", "النشاط"),
    ("code", "common_name", "common name (CN", "الاسم الشائع"),
])
def test_at79_special_character_refused_when_the_unit_is_created(zw, char, field, key, label_en, label_ar):
    value = {"branch_name_en": "Jeddah{}Branch", "location": "RRRD{}2929", "industry": "Real{}estate",
             "code": "EGS{}1"}[field].format(char)
    with pytest.raises(ZatcaError) as exc:
        _create(zw, **{field: value})
    assert exc.value.code == "csr_values"
    assert [p[0] for p in exc.value.problems] == [f"csr_chars_{key}"]
    assert label_en in exc.value.en and char in exc.value.en  # names the field and the character, en
    assert label_ar in exc.value.ar and char in exc.value.ar  # and ar
    assert not EgsUnit.objects.filter(entity=zw.tst1).exists()


def test_at79_ou_falls_back_to_the_arabic_branch_then_the_unit_name(zw):
    with pytest.raises(ZatcaError) as exc:
        _create(zw, branch_name_en="", branch_name_ar="فرع_جدة")
    assert [p[0] for p in exc.value.problems] == ["csr_chars_org_unit"]
    with pytest.raises(ZatcaError) as exc:
        _create(zw, branch_name_en="", branch_name_ar="", name_en="EGS #1")
    assert [p[0] for p in exc.value.problems] == ["csr_chars_org_unit"]
    _create(zw, branch_name_en="Branch", name_en="EGS #1")  # name_en is not used when a branch name exists


def test_at79_entity_legal_name_with_ampersand_refused(zw):
    _set_entity(zw.tst1, legal_name_en="TST1 & Co")
    with pytest.raises(ZatcaError) as exc:
        _create(zw)
    assert [p[0] for p in exc.value.problems] == ["csr_chars_org_name"]
    assert "entity legal name (O)" in exc.value.en and "&" in exc.value.en and "الاسم القانوني للكيان" in exc.value.ar


def test_at79_every_bad_field_is_listed_once(zw):
    with pytest.raises(ZatcaError) as exc:
        _create(zw, branch_name_en="A&B", location="L<1", industry="I*I_I")
    assert [p[0] for p in exc.value.problems] == ["csr_chars_org_unit", "csr_chars_location", "csr_chars_industry"]
    assert "* _" in exc.value.problems[2][1]  # each character once, in order


def test_at79_arabic_and_ordinary_punctuation_accepted(zw):
    _ready(zw)
    egs = _create(zw, branch_name_en="", branch_name_ar="فرع جدة (الرئيسي)", industry="عقارات - تأجير، إدارة",
                  location="RRRD2929 / Jeddah", code="EGS-1A")  # EGS codes: [A-Za-z0-9_-] (review 2026-10-21 item 10)
    egs = services.generate_csr(egs, user=zw.admin1)
    assert egs.status == "csr_ready"


def test_at79_checked_again_before_the_csr(zw):
    """The entity's legal name (O) can change after the unit is created: no key is generated or stored."""
    _ready(zw)
    egs = _create(zw)
    _set_entity(zw.tst1, legal_name_en="TST1 Trading @ Jeddah")
    calls = list(zw.fakes.store.calls)
    with pytest.raises(ZatcaError) as exc:
        services.generate_csr(egs, user=zw.admin1)
    assert exc.value.code == "csr_values" and [p[0] for p in exc.value.problems] == ["csr_chars_org_name"]
    egs.refresh_from_db()
    assert egs.status == "draft" and egs.private_key_ref == "" and egs.csr_pem == ""
    assert zw.fakes.store.calls == calls  # nothing written to the secret store
    assert not OnboardingEvent.objects.filter(egs_unit=egs, action="csr_generated").exists()


def test_at79_checked_before_renewal_too(zw):
    egs = onboard(zw, zw.tst1, zw.admin1)
    _set_entity(zw.tst1, legal_name_en="TST1 Trading_Co")
    with pytest.raises(ZatcaError) as exc:
        services.renew_production_csid(egs, "654321", user=zw.admin1)
    assert exc.value.code == "csr_values"
    assert not [c for c in zw.fakes.client.calls if c[0] == "renew_production_csid"]  # nothing sent to ZATCA


def test_at79_rules_on_clean_values():
    assert rules.csr_value_problems(vat_number="311111111100003", **CLEAN) == []
    assert rules.csr_value_problems(vat_number="311111111100003", **{**CLEAN, "org_name": "شركة الاختبار"}) == []


# --- VAT group: OU = the member's 10-digit TIN (AT-80) ---------------------------------------------------------

GROUP_VAT = "300000000010003"  # 11th digit 1


def test_at80_vat_group_detection():
    assert rules.is_vat_group(GROUP_VAT)
    assert not rules.is_vat_group("311111111100003") and not rules.is_vat_group("") and not rules.is_vat_group("31")


@pytest.mark.parametrize("ou", ["Jeddah Branch", "123456789", "12345678901", "١٢٣٤٥٦٧٨٩٠", "12345 7890"])
def test_at80_vat_group_needs_a_10_digit_tin_as_ou(zw, ou):
    _set_entity(zw.tst1, vat_number=GROUP_VAT)
    with pytest.raises(ZatcaError) as exc:
        _create(zw, branch_name_en=ou)
    assert [p[0] for p in exc.value.problems] == ["vat_group_ou"]
    assert "10-digit TIN" in exc.value.en and "10 أرقام" in exc.value.ar


def test_at80_vat_group_tin_goes_into_the_csr_ou(zw):
    from cryptography import x509

    _set_entity(zw.tst1, vat_number=GROUP_VAT)
    _ready(zw)
    egs = services.generate_csr(_create(zw, branch_name_en="1234567890"), user=zw.admin1)
    csr = x509.load_pem_x509_csr(egs.csr_pem.encode())
    assert csr.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)[0].value == "1234567890"


def test_at80_not_a_vat_group_keeps_the_branch_name(zw):
    assert zw.tst1.vat_number[10] != "1"
    _create(zw, branch_name_en="Jeddah Branch")  # OU = branch name (§3.2; SDK findings §3.6)
