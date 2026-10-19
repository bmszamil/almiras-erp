"""ZATCA developer-portal sandbox run of 2026-10-19 (docs/compliance/zatca-sandbox-2026-10-19.md §7 items 4 to 7),
as built: the production CSID key check (R-8), the sample and fixture buyer VAT, BR-CUSTOM-VALIDATION-01 (R-56)
and Arabic for the ZATCA codes met in the sandbox. Fake ZATCA client only, no network.
"""
import base64
from dataclasses import replace
from datetime import date

import pytest
from cryptography import x509

from zatca import rules, services, ubl
from zatca.conftest import ADMINS1, NOW, alerts, einvoice_of, held_codes, issue, keys, mutate, onboard, run
from zatca.messages import LOCAL, ZATCA_CODES_AR, ZatcaError, arabic_for
from zatca.models import EgsCredential, EgsUnit, OnboardingEvent
from zatca.testing import FakeZatcaClient, zatca_error

pytestmark = pytest.mark.django_db
D = __import__("decimal").Decimal
ZATCA_TEST_VAT = "399999999900003"  # ZATCA's documented test VAT; the developer portal's static CSID is bound to it


@pytest.fixture
def live(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    return zw


# --- §7 item 4: the production CSID must be for the unit's key (R-8) ------------------------------------------------

def _compliance_passed(zw, entity, admin):
    keys(zw, entity, admin)
    profile = services.create_profile(entity, user=admin, go_live_date=date(2026, 1, 1))
    services.enable_profile(profile, user=admin)
    egs = services.create_egs_unit(entity, user=admin, code="EGS1", name_en="EGS", name_ar="وحدة",
                                   serial_number="1-TestERP|2-1.0|3-EGS1", industry="Real estate",
                                   supports_standard=True, supports_simplified=True, branch_name_en="Branch")
    services.generate_csr(egs, user=admin)
    services.enter_otp(egs, "123345", user=admin)
    return services.run_compliance_checks(egs, user=admin)


def test_developer_portal_static_production_certificate_is_refused(zw):
    egs = _compliance_passed(zw, zw.tst1, zw.admin1)
    zw.fakes.client.developer_portal_production_certificate = True
    with pytest.raises(ZatcaError) as exc:
        services.request_production_csid(egs, user=zw.admin1)
    assert exc.value.code == "pcsid_key_mismatch"
    assert "developer-portal sandbox" in exc.value.en and "compliance checks only" in exc.value.en
    assert "بيئة التجربة" in exc.value.ar
    egs.refresh_from_db()
    assert egs.status == "compliance_passed"
    assert not EgsCredential.objects.filter(egs_unit=egs, kind="production").exists()
    assert EgsCredential.objects.get(egs_unit=egs, kind="compliance").status == "active"
    assert not OnboardingEvent.objects.filter(egs_unit=egs, action="pcsid_issued").exists()
    # The same unit gets a certificate for its own key afterwards (e.g. ZATCA issues a proper one).
    zw.fakes.client.developer_portal_production_certificate = False
    egs = services.request_production_csid(egs, user=zw.admin1)
    assert egs.status == "active"


def test_production_certificate_for_another_key_is_refused_outside_the_sandbox_too(zw):
    egs = _compliance_passed(zw, zw.tst1, zw.admin1)
    zw.fakes.client.developer_portal_production_certificate = True
    response = zw.fakes.client._issue(None, "production")
    profile = services._profile_of(zw.tst1)
    profile.environment = "simulation"  # not saved: only the message branch differs
    with pytest.raises(ZatcaError) as exc:
        services._check_production_key(egs, profile, response)
    assert exc.value.code == "pcsid_key_mismatch" and "not for this unit's key" in exc.value.en and exc.value.ar
    assert "developer-portal" not in exc.value.en


def test_production_certificate_for_the_units_key_passes_the_check(zw):
    egs = _compliance_passed(zw, zw.tst1, zw.admin1)
    profile = services._profile_of(zw.tst1)
    csr = x509.load_pem_x509_csr(egs.csr_pem.encode())
    services._check_production_key(egs, profile, zw.fakes.client._issue(csr, "production"))  # no error


# --- §7 item 5: sample and fixture buyers are not ZATCA's test VAT --------------------------------------------------

def test_sample_and_fixture_buyer_vat_is_not_zatcas_test_vat(zw):
    assert services.SAMPLE_BUYER.vat_number != ZATCA_TEST_VAT and rules.vat_number_ok(services.SAMPLE_BUYER.vat_number)
    assert rules.vat_number_ok(services.SAMPLE_BUYER_ALTERNATE_VAT)
    assert services.SAMPLE_BUYER_ALTERNATE_VAT != services.SAMPLE_BUYER.vat_number
    for code in ("TST1", "TST2"):
        vat = zw.customers[code].vat_number
        assert vat != ZATCA_TEST_VAT and rules.vat_number_ok(vat)
        assert vat not in (zw.tst1.vat_number, zw.tst2.vat_number, zw.par.vat_number)


def test_sample_buyer_changes_when_the_seller_has_its_vat(zw):
    assert services._sample_buyer("311111111100003") is services.SAMPLE_BUYER
    other = services._sample_buyer(services.SAMPLE_BUYER.vat_number)
    assert other.vat_number == services.SAMPLE_BUYER_ALTERNATE_VAT and other.name == services.SAMPLE_BUYER.name


@pytest.mark.parametrize("seller_vat", [ZATCA_TEST_VAT, "310000000000003"])
def test_compliance_samples_never_sell_to_the_seller(zw, seller_vat):
    type(zw.tst1).objects.filter(pk=zw.tst1.pk).update(vat_number=seller_vat)
    zw.tst1.refresh_from_db()
    egs = _compliance_passed(zw, zw.tst1, zw.admin1)
    assert egs.status == "compliance_passed"
    for api, _env, body in zw.fakes.client.calls:
        if api == "compliance_check":
            xml = base64.b64decode(body["invoice"])
            buyer = ubl.find_text(xml, "/*/cac:AccountingCustomerParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID")
            assert buyer != seller_vat


def test_control_the_fake_rejects_samples_that_sell_to_the_seller(zw, monkeypatch):
    type(zw.tst1).objects.filter(pk=zw.tst1.pk).update(vat_number="310000000000003")
    zw.tst1.refresh_from_db()
    monkeypatch.setattr(services, "_sample_buyer", lambda seller_vat: services.SAMPLE_BUYER)
    with pytest.raises(ZatcaError) as exc:
        _compliance_passed(zw, zw.tst1, zw.admin1)
    assert exc.value.code == "compliance_failed" and "standard-388" in exc.value.en
    event = OnboardingEvent.objects.get(entity=zw.tst1, action="compliance_checks_failed")
    assert "BR-CUSTOM-VALIDATION-01" in event.detail["results"]["standard-388"]["errors"]
    assert event.detail["results"]["simplified-388"]["passed"]  # simplified samples have no buyer VAT


# --- §7 item 6: buyer VAT = seller VAT refused locally (R-56) -------------------------------------------------------

def _data(category="standard", buyer_vat="311111111100003"):
    seller = ubl.Party(name="شركة", vat_number="311111111100003", id_scheme="CRN", id_value="1", street="ش",
                       building_no="1234", district="ح", city="الرياض", postal_code="12345", country="SA")
    buyer = replace(seller, name="مشتري", vat_number=buyer_vat, id_scheme="", id_value="")
    line = ubl.Line(1, "بند", D("1"), "PCE", D("10.00"), D("0"), D("10.00"), D("1.50"), "S", D("15.00"))
    return ubl.InvoiceData(
        number="N-1", uuid="u", issue=NOW, type_code="388", category=category,
        subtype_code="0100000" if category == "standard" else "0200000", currency="SAR", currency_places=2,
        seller=seller, buyer=buyer, lines=[line], groups=[ubl.TaxGroup("S", D("15.00"), D("10.00"), D("1.50"))],
        total_net=D("10.00"), total_vat=D("1.50"), total_gross=D("11.50"), total_vat_sar=D("1.50"),
        supply_date=NOW.date(), payment_means_code="10")


@pytest.mark.parametrize("category", ["standard", "simplified"])
def test_r56_buyer_vat_equal_to_seller_vat_is_refused(category):
    codes = {p[0]: p for p in rules.document_problems(_data(category))}
    _, en, ar = codes["BR-CUSTOM-VALIDATION-01"]
    assert "BT-48" in en and "BT-31" in en and "للبائع" in ar and "للمشتري" in ar
    assert "BR-CUSTOM-VALIDATION-01" not in {p[0] for p in rules.document_problems(_data(category, "322222222200003"))}
    assert "BR-CUSTOM-VALIDATION-01" not in {p[0] for p in rules.document_problems(_data(category, ""))}


def test_r56_buyer_is_seller_is_held_before_any_icv(live, monkeypatch):
    mutate(monkeypatch, **{"buyer.vat_number": lambda data: data.seller.vat_number})
    calls_before = len(live.fakes.client.calls)
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    assert einv.icv is None and einv.hold_reason == "local_validation"
    assert "BR-CUSTOM-VALIDATION-01" in held_codes(einv)
    message = einv.messages.get(code="BR-CUSTOM-VALIDATION-01")
    assert message.message_en == LOCAL["BR-CUSTOM-VALIDATION-01"][0] and message.message_ar
    assert EgsUnit.objects.get(pk=live.egs1.pk).last_icv == 0
    assert len(live.fakes.client.calls) == calls_before  # nothing sent to ZATCA
    assert alerts("local_validation", einv) == ADMINS1


# --- §7 item 7: Arabic for the codes met in the sandbox -------------------------------------------------------------

SANDBOX_CODES = ["BR-CUSTOM-VALIDATION-01", "certificate-permissions", "certificate-issuer-name",
                 "invalid-signing-certificate", "signed-properties-hashing", "invoiceHash_QRCODE_INVALID",
                 "invoiceTimeStamp_QRCODE_INVALID", "timestamp_QRCODE_INVALID", "invoiceTotal_QRCODE_INVALID",
                 "sellerName_QRCODE_INVALID", "INVOICE_SIGNATURE_VALUE_QRCODE_INVALID", "QRCODE_INVALID",
                 "XML-INVOICE-ERROR", "Missing-OTP", "Incorrect-ComplianceRequestId"]


@pytest.mark.parametrize("code", SANDBOX_CODES)
def test_sandbox_codes_have_arabic(code):
    text = arabic_for(code)
    assert text and any("\u0600" <= ch <= "\u06ff" for ch in text)


def test_zatca_rejection_with_a_sandbox_code_stores_its_arabic(live):
    live.fakes.client.script["clearance"] = [FakeZatcaClient.rejected(
        zatca_error("BR-CUSTOM-VALIDATION-01", "The Seller VAT ... must not match the buyer VAT"))]
    doc = issue(live, live.tst1)
    run(live)
    message = einvoice_of(doc).messages.get(code="BR-CUSTOM-VALIDATION-01", origin="zatca")
    assert message.message_ar == ZATCA_CODES_AR["BR-CUSTOM-VALIDATION-01"]
