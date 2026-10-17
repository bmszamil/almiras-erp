"""Review of 838e89b, findings 10, 11, 12, 17: BR-KSA-63/67 and BR-CO-15 (probes p6, p7), R-51 unit
prices, exemption reason text, notes on pre-go-live invoices."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from invoicing.conftest import invoice
from zatca import rules, ubl
from zatca.conftest import ADMINS1, NOW, TODAY, alerts, einvoice_of, held_codes, issue, mutate, onboard, run, xml_text
from zatca.models import EgsUnit, EInvoice

pytestmark = pytest.mark.django_db
D = __import__("decimal").Decimal


@pytest.fixture
def live(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    return zw


# --- findings 10, 11 (probes p6, p7): local checks ZATCA would otherwise reject ------------------------------------

@pytest.mark.parametrize("changes, code", [
    ({"buyer.building_no": ""}, "buyer_address_sa"),
    ({"buyer.postal_code": ""}, "buyer_address_sa"),
    ({"buyer.district": ""}, "buyer_address_sa"),
    ({"buyer.postal_code": "1234"}, "buyer_postal_code"),
    ({"total_gross": D("999.00")}, "total_gross"),
])
def test_f10_br_ksa_63_67_and_br_co_15_refused_locally(live, monkeypatch, changes, code):
    mutate(monkeypatch, **changes)
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    assert einv.icv is None and einv.hold_reason == "local_validation" and code in held_codes(einv)
    live.egs1.refresh_from_db()
    assert live.egs1.last_icv == 0


def test_f10_non_saudi_buyer_needs_no_building_number(live, monkeypatch):
    mutate(monkeypatch, **{"buyer.country": "AE", "buyer.building_no": "", "buyer.postal_code": "",
                           "buyer.district": ""})
    doc = issue(live, live.tst1)
    run(live)
    assert einvoice_of(doc).status == "cleared"


def test_f11_unit_price_with_a_third_decimal_refused_under_r51(live, monkeypatch):
    mutate(monkeypatch, currency_places=3,
           lines=lambda d: [replace(line, unit_price=D("33.335")) for line in d.lines])
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    assert einv.icv is None and "third_decimal" in held_codes(einv)
    monkeypatch.undo()
    mutate(monkeypatch, currency_places=3, lines=lambda d: [replace(line, unit_price=D("33.330")) for line in d.lines])
    ok = issue(live, live.tst1)
    run(live, now=NOW + timedelta(minutes=1))
    assert einvoice_of(ok).status == "cleared"
    assert xml_text(einvoice_of(ok).signed_xml, "//cac:Price/cbc:PriceAmount") == "33.33"


# --- finding 12: exemption reason text ------------------------------------------------------------------------

def test_f12_exemption_reason_is_the_codes_official_arabic_text(live):
    from invoicing.models import VatExemptionReason

    doc = issue(live, live.tst1, lines=[("rent_residential", "1", "1000.00")])
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "cleared"
    reason = VatExemptionReason.objects.get(code="VATEX-SA-30")
    tc = "/*/cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory"
    assert xml_text(einv.signed_xml, f"{tc}/cbc:TaxExemptionReasonCode") == "VATEX-SA-30"
    assert xml_text(einv.signed_xml, f"{tc}/cbc:TaxExemptionReason") == reason.name_ar


# --- finding 17: credit note on an invoice issued before go-live ---------------------------------------------------

def test_f17_note_on_a_pre_go_live_invoice_is_refused_with_a_clear_message(zw):
    """invoicing refuses notes on originals that are not final; a pre-go-live original is final only outside this
    module (e.g. migrated history, or a Phase 1 path, OQ-1). Stand-in: invoicing's fake port finalises it before
    TST1 has a ZATCA profile, so it has no e-invoice."""
    from invoicing.conftest import approve
    from invoicing.testing import fake_zatca

    with fake_zatca() as port:
        original = issue(zw, zw.tst1)
        original = port.clear(original)
    assert original.status == "final" and not EInvoice.objects.filter(source_document=original).exists()
    onboard(zw, zw.tst1, zw.admin1)
    w = SimpleNamespace(alm=zw.tst1, clerk=zw.clerk, customer=original.customer, cfo=zw.cfo)
    note = approve(w, invoice(w, [("management_fee", "1", "10.00")], customer=original.customer, issue_date=TODAY,
                              entity=zw.tst1, doc_type="credit_note", original_document=original,
                              payment_means_code="10", reason_ar="إرجاع البضاعة"), zw.cfo)
    run(zw)
    einv = einvoice_of(note)
    assert einv.icv is None and einv.hold_reason == "local_validation"
    message = einv.messages.get(code="original_pre_go_live")
    assert "before the entity went live" in message.message_en and message.message_ar
    assert "billing_reference" not in held_codes(einv)
    assert alerts("local_validation", einv) == ADMINS1
    assert EgsUnit.objects.get(entity=zw.tst1).last_icv == 0


def test_f17_rule_unit():
    data = SimpleNamespace(billing_reference="INV-1", billing_reference_entity_ok=True, billing_reference_pre_go_live=True)
    codes = {p[0] for p in _note_problems(data)}
    assert "original_pre_go_live" in codes and "billing_reference" not in codes


def _note_problems(overrides):
    seller = ubl.Party(name="شركة", vat_number="311111111100003", id_scheme="CRN", id_value="1", street="ش",
                       building_no="1234", district="ح", city="الرياض", postal_code="12345", country="SA")
    line = ubl.Line(1, "بند", D("1"), "PCE", D("10.00"), D("0"), D("10.00"), D("1.50"), "S", D("15.00"))
    data = ubl.InvoiceData(
        number="N-1", uuid="u", issue=NOW, type_code="381", category="simplified", subtype_code="0200000",
        currency="SAR", currency_places=2, seller=seller, buyer=ubl.Party(), lines=[line],
        groups=[ubl.TaxGroup("S", D("15.00"), D("10.00"), D("1.50"))], total_net=D("10.00"), total_vat=D("1.50"),
        total_gross=D("11.50"), total_vat_sar=D("1.50"), payment_means_code="10", reason="سبب")
    for name, value in vars(overrides).items():
        setattr(data, name, value)
    return rules.document_problems(data)
