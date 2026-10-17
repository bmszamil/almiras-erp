"""R-39 prepayment deduction checks (BR-KSA-73, -74, -75, -79, -80, -82; SDK R3.4.8 findings §3.4).

Validators only: documents with prepayment deductions (and 386 itself) are still refused until the invoicing
prepayment flow exists. The valid fixture is AT-52's final 388: net 4,000.00 S 15 % deducting a 386 of
1,000.00 + 150.00, so BT-113 = 1,150.00 and BT-115 = 4,600.00 - 1,150.00 = 3,450.00.
"""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from zatca import rules, ubl
from zatca.conftest import NOW, einvoice_of, held_codes, issue, mutate, onboard, run
from zatca.models import EgsUnit


def _deduction(**over):
    p = ubl.PrepaymentLine(number="TST1-386-1", uuid="8e6000cf-1a98-4174-b3e7-b5d5954bc10d", issue_date="2026-10-01",
                           issue_time="10:00:00", type_code="386", taxable=D("1000.00"), vat=D("150.00"),
                           category="S", rate=D("15.00"))
    return replace(p, **over)


def _final_388(prepayments=None, prepaid=D("1150.00"), currency="SAR", places=2):
    seller = ubl.Party(name="شركة", vat_number="311111111100003", id_scheme="CRN", id_value="1", street="ش",
                       building_no="1234", district="ح", city="الرياض", postal_code="12345", country="SA")
    line = ubl.Line(1, "بند", D("1"), "PCE", D("4000.00"), D("0"), D("4000.00"), D("600.00"), "S", D("15.00"))
    return ubl.InvoiceData(
        number="TST1-388-9", uuid="u", issue=NOW, type_code="388", category="simplified", subtype_code="0200000",
        currency=currency, currency_places=places, seller=seller, buyer=ubl.Party(), lines=[line],
        groups=[ubl.TaxGroup("S", D("15.00"), D("4000.00"), D("600.00"))], total_net=D("4000.00"),
        total_vat=D("600.00"), total_gross=D("4600.00"), total_vat_sar=D("600.00"), payment_means_code="10",
        prepaid_amount=prepaid, prepayments=[_deduction()] if prepayments is None else prepayments)


def _codes(data):
    return [p[0] for p in rules.prepayment_problems(data)]


def test_at52_valid_deduction_passes_the_validators():
    data = _final_388()
    assert _codes(data) == []
    assert data.payable == D("3450.00")  # BT-115 = BT-112 - BT-113 (BR-CO-16), QR tag 4 (R-15)


def test_at52_two_rates_one_line_each():
    """Rate-change sample (R4.0.0): a 5 % prepayment deducted on a 15 % invoice; one line per prepaid rate."""
    lines = [_deduction(), _deduction(number="TST1-386-2", taxable=D("200.00"), vat=D("10.00"), rate=D("5.00"))]
    assert _codes(_final_388(lines, prepaid=D("1360.00"))) == []


def test_zero_rated_prepayment_has_no_vat():
    zero = _deduction(category="Z", rate=D("0"), vat=D("0.00"))
    assert _codes(_final_388([zero], prepaid=D("1000.00"))) == []
    assert "prepayment_vat" in _codes(_final_388([replace(zero, vat=D("1.00"))], prepaid=D("1001.00")))
    assert "prepayment_vat" in _codes(_final_388([replace(zero, rate=D("15.00"))], prepaid=D("1000.00")))


@pytest.mark.parametrize("field", ["number", "issue_date", "issue_time", "type_code"])
def test_br_ksa_73_reference_fields_required(field):
    assert "prepayment_reference" in _codes(_final_388([_deduction(**{field: ""})]))


def test_br_ksa_73_prepaid_amount_without_deduction_lines():
    codes = _codes(_final_388([], prepaid=D("1150.00")))
    assert codes == ["prepayment_reference"]


@pytest.mark.parametrize("type_code", ["388", "381", "383", " "])
def test_br_ksa_74_type_code_must_be_386(type_code):
    assert "prepayment_type" in _codes(_final_388([_deduction(type_code=type_code)]))


@pytest.mark.parametrize("field, value", [("taxable", None), ("vat", None), ("category", ""), ("rate", None)])
def test_br_ksa_75_vat_fields_required(field, value):
    assert "prepayment_vat_fields" in _codes(_final_388([_deduction(**{field: value})]))


@pytest.mark.parametrize("vat, ok", [(D("150.00"), True), (D("150.01"), False), (D("149.99"), False)])
def test_br_ksa_79_prepaid_vat_is_rounded_taxable_times_rate(vat, ok):
    """House rule: exact round(KSA-31 x KSA-34 / 100, 2) half-up (the SDK tolerates 0.01 as a warning)."""
    codes = _codes(_final_388([_deduction(vat=vat)], prepaid=D("1000.00") + vat))
    assert ("prepayment_vat" not in codes) is ok


def test_br_ksa_79_half_up():
    p = _deduction(taxable=D("0.10"), vat=D("0.02"))  # 0.015 -> 0.02
    assert _codes(_final_388([p], prepaid=D("0.12"))) == []
    assert "prepayment_vat" in _codes(_final_388([replace(p, vat=D("0.01"))], prepaid=D("0.11")))


@pytest.mark.parametrize("prepaid", [D("1000.00"), D("1150.01"), D("0")])
def test_br_ksa_80_prepaid_equals_sum_of_taxable_and_vat(prepaid):
    assert "prepaid_amount" in _codes(_final_388(prepaid=prepaid))


@pytest.mark.parametrize("field", ["price", "net", "allowance", "charge", "line_vat", "line_amount_with_vat"])
def test_br_ksa_82_deduction_line_amounts_are_zero(field):
    assert "prepayment_line_zero" in _codes(_final_388([_deduction(**{field: D("0.01")})]))


def test_r39_valid_deduction_still_refused_by_document_problems():
    """Validators pass, but the document is refused: prepayment documents are not built yet."""
    codes = [p[0] for p in rules.document_problems(_final_388())]
    assert codes == ["prepayment_not_built"]
    bad = [p[0] for p in rules.document_problems(_final_388([_deduction(type_code="388")]))]
    assert "prepayment_not_built" in bad and "prepayment_type" in bad


def test_r51_third_decimal_in_prepayment_amounts():
    p = _deduction(taxable=D("1000.005"), vat=D("150.00"))
    codes = [c[0] for c in rules.document_problems(_final_388([p], prepaid=D("1150.005"), currency="KWD", places=3))]
    assert "third_decimal" in codes


def test_r39_deduction_lines_are_never_emitted():
    with pytest.raises(NotImplementedError):
        ubl.build_unsigned(_final_388(), 1, "x")


def test_prepayment_messages_are_bilingual():
    from zatca.messages import LOCAL

    for code in ("prepayment_not_built", "prepayment_reference", "prepayment_type", "prepayment_vat_fields",
                 "prepayment_vat", "prepaid_amount", "prepayment_line_zero"):
        en, ar = LOCAL[code]
        assert en.strip() and ar.strip() and en != ar


@pytest.mark.django_db
def test_r39_document_with_prepayment_held_without_icv(zw, monkeypatch):
    """Through the queue: refused locally (bilingual), no ICV consumed, nothing sent to ZATCA."""
    onboard(zw, zw.tst1, zw.admin1)
    mutate(monkeypatch, prepaid_amount=D("15.00"))
    doc = issue(zw, zw.tst1, simplified=True)
    run(zw)
    einv = einvoice_of(doc)
    assert einv.icv is None and einv.hold_reason == "local_validation"
    assert {"prepayment_not_built", "prepayment_reference"} <= held_codes(einv)
    assert einv.messages.get(code="prepayment_not_built").message_ar
    assert EgsUnit.objects.get(entity=zw.tst1).last_icv == 0
    assert not [c for c in zw.fakes.client.calls if c[0] in ("clearance", "reporting")]
