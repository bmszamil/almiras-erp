"""VAT return computation (tax-returns.md R15 to R25, R55, R56; AT-15 to AT-24, AT-46 to AT-48)."""
from datetime import date
from types import SimpleNamespace

import pytest

from invoicing import services as invoicing
from invoicing.conftest import usd_rate
from tax import art51_port, services
from tax.conftest import (
    D, approve_sales, bill, box, close_q1, confirm_tax_points, filed, imported, invoice, journal, pdf, prepared,
    reviewed, sale,
)
from tax.models import TaxError, VatExceptionAck

pytestmark = pytest.mark.django_db
Q1, Q2 = date(2027, 1, 1), date(2027, 4, 1)


def compute(t, start=Q1):
    return services.compute_draft(t.alm, start, user=t.a1)


def test_r18_box1_standard_sales_and_tie_out(t):
    sale(t)
    ret = compute(t)
    assert box(ret, 1) == (D("10000.00"), D("0"), D("1500.00"))
    assert box(ret, 6) == box(ret, 1) and box(ret, 13)[2] == D("1500.00") and box(ret, 16)[2] == D("1500.00")
    tie = services.tie_out(ret, user=t.a1)
    assert (tie["ledger_output"], tie["difference_output"], tie["difference_input"]) == (D("1500"), 0, 0)


def test_at18_foreign_currency_invoice_at_its_sar_rate(t):
    usd_rate(t, "3.75")
    sale(t)  # SAR 10,000.00 / VAT 1,500.00
    sale(t, [("rent_commercial", "1", "1000.00")], currency="USD")
    ret = compute(t)
    assert box(ret, 1) == (D("13750.00"), D("0"), D("2062.50"))  # 1,500.00 + 562.50
    assert services.tie_out(ret, user=t.a1)["difference_output"] == 0


def test_at19_boxes_2_to_5_by_category_and_reason_out_of_scope_nowhere(t):
    sale(t, [("rent_commercial", "1", "1000.00", {"vat_category": "Z", "vat_exemption_reason": "VATEX-SA-32"}),
             ("rent_commercial", "1", "2000.00", {"vat_category": "Z", "vat_exemption_reason": "VATEX-SA-35"}),
             ("rent_residential", "1", "3000.00"),
             ("rent_commercial", "1", "4000.00", {"vat_category": "O", "vat_exemption_reason": "VATEX-SA-OOS",
                                                   "exemption_text_ar": "خارج النطاق للاختبار"})])
    ret = compute(t)
    assert box(ret, 4) == (D("1000.00"), D("0"), D("0"))
    assert box(ret, 3) == (D("2000.00"), D("0"), D("0"))
    assert box(ret, 5) == (D("3000.00"), D("0"), D("0"))  # residential rent, VATEX-SA-30 (R85, X12(b))
    assert box(ret, 6) == (D("6000.00"), D("0"), D("0"))  # O (VATEX-SA-OOS) is in no box
    assert not ret.source_lines.filter(amount_sar=D("4000.00")).exists()


def test_r19_box2_private_health_and_education_to_citizens():
    line = lambda cat, code: SimpleNamespace(vat_category=cat, vat_exemption_reason_id=1,  # noqa: E731
                                             vat_exemption_reason=SimpleNamespace(code=code))
    assert services._sales_box(line("Z", "VATEX-SA-HEA")) == 2
    assert services._sales_box(line("Z", "VATEX-SA-EDU")) == 2
    assert services._sales_box(line("Z", "VATEX-SA-33")) == 4
    assert services._sales_box(line("Z", "VATEX-SA-MLTRY")) == 3
    assert services._sales_box(line("E", "VATEX-SA-29")) == 5
    assert services._sales_box(line("O", "VATEX-SA-OOS")) is None


def note(t, original, on, doc_type="credit_note", price="4000.00"):
    return sale(t, [("rent_commercial", "1", price)], on=on, doc_type=doc_type, original_document=original,
                payment_means_code="30", reason_ar="تعديل متفق عليه")


def test_at20_credit_note_amount_or_adjustment_column_and_debit_note(t):
    original = sale(t)
    note(t, original, date(2027, 3, 30))  # before the Q1 return is filed: reduces the Q1 amount
    close_q1(t)
    q1 = services.approve_return(reviewed(t, prepared(t)), user=t.c1)
    assert box(q1, 1) == (D("6000.00"), D("0"), D("900.00"))
    filed(t, q1, on=date(2027, 4, 20))
    note(t, original, date(2027, 4, 25), price="2000.00")  # after filing: Q2 Adjustment column (R20)
    note(t, original, date(2027, 5, 5), doc_type="debit_note", price="1000.00")  # always the Amount column
    q2 = compute(t, Q2)
    assert box(q2, 1) == (D("1000.00"), D("-2000.00"), D("-150.00"))
    assert box(q2, 6) == (D("1000.00"), D("-2000.00"), D("-150.00"))


def test_at21_bills_box7_recoverable_only_unregistered_not_reported_z_e(t):
    bill(t, [("5300", "1", "10000.00")])  # resident, taxable: VAT 1,500.00 to 1310
    bill(t, [("5300", "1", "2000.00", {"vat_attribution": "exempt"})])  # VAT 300.00 to cost
    bill(t, [("5300", "1", "500.00")], vendor=t.unregistered)  # D24 / R92: no VAT, not reported (OQ-12)
    bill(t, [("5300", "1", "700.00", {"vat_category": "Z"})])
    bill(t, [("5300", "1", "400.00", {"vat_category": "E"})])
    ret = compute(t)
    assert box(ret, 7) == (D("12000.00"), D("0"), D("1500.00"))
    assert box(ret, 10) == (D("700.00"), D("0"), D("0")) and box(ret, 11) == (D("400.00"), D("0"), D("0"))
    assert box(ret, 12) == (D("13100.00"), D("0"), D("1500.00"))
    assert not ret.source_lines.filter(amount_sar=D("500.00")).exists()
    tie = services.tie_out(ret, user=t.a1)
    assert (tie["ledger_input"], tie["difference_input"]) == (D("1500"), 0)


def import_record(t, number, value, vat, attribution="taxable", debit=None, on=date(2027, 2, 10)):
    rec = services.create_import_vat_record(
        t.alm, user=t.a1, customs_declaration_no=number, declaration_date=on, customs_value_sar=value,
        vat_paid_sar=vat, attribution=attribution, bank_account="1100", declaration_file=pdf("declaration.pdf"),
        debit_account=debit)
    services.submit_tax_document(rec, user=t.a1)
    return services.approve_tax_document(rec, user=t.c1)


def lines_of(entry):
    return sorted((l.account.code, l.debit, l.credit) for l in entry.lines.select_related("account"))


def test_at22_import_vat_records_post_t3_and_fill_box8(t):
    taxable = import_record(t, "CUS-1", "15000.00", "2250.00")
    mixed = import_record(t, "CUS-2", "3000.00", "450.00", "mixed", "5300")
    assert lines_of(taxable.journal_entry) == [("1100", 0, D("2250.00")), ("1310", D("2250.00"), 0)]
    assert lines_of(mixed.journal_entry) == [("1100", 0, D("450.00")), ("5300", D("450.00"), 0)]
    assert taxable.journal_entry.posted_by == t.c1
    ret = compute(t)
    assert box(ret, 8) == (D("18000.00"), D("0"), D("2250.00"))
    assert services.tie_out(ret, user=t.a1)["difference_input"] == 0


def test_r22_import_record_needs_cost_account_when_not_recoverable_and_is_unique(t):
    with pytest.raises(TaxError) as exc:
        import_record(t, "CUS-3", "100.00", "15.00", "exempt")
    assert exc.value.code == "debit_account_invalid"
    import_record(t, "CUS-4", "100.00", "15.00")
    with pytest.raises(TaxError) as exc:
        import_record(t, "CUS-4", "100.00", "15.00")
    assert exc.value.code == "declaration_duplicate"


def test_at23_reverse_charge_services_in_box9_goods_not(t):
    bill(t, [("5300", "1", "10000.00", {"supply_kind": "services"})], vendor=t.foreign)
    bill(t, [("1500", "1", "5000.00", {"supply_kind": "goods"})], vendor=t.foreign)
    ret = compute(t)
    assert box(ret, 9) == (D("10000.00"), D("0"), D("1500.00"))
    assert ret.rc_output_vat_sar == D("1500.00") and ret.output_vat_sar == D("1500.00")
    assert box(ret, 13)[2] == D("0.00")  # RC output and input offset (PSG §4.6)
    assert not ret.source_lines.filter(amount_sar=D("5000.00")).exists()
    tie = services.tie_out(ret, user=t.a1)
    assert (tie["difference_output"], tie["difference_input"]) == (0, 0)


def test_at24_totals_with_carried_credit(t):
    imported(t, date(2026, 10, 1), {13: {"vat": "-4000.00"}, 16: {"vat": "-4000.00"}}, ref="OLD-Q4",
             filed_on=date(2027, 1, 20))
    sale(t)
    bill(t, [("5300", "1", "2000.00")])
    ret = compute(t)
    assert (box(ret, 13)[2], box(ret, 14)[2], box(ret, 15)[2], box(ret, 16)[2]) == (
        D("1200.00"), D("0"), D("4000.00"), D("-2800.00"))
    assert services._q(D("0.005")) == D("0.01") and services._q(D("-0.005")) == D("-0.01")  # ROUND_HALF_UP


def test_at17_reversal_counts_negative_in_its_own_period(t):
    doc = bill(t, [("5300", "1", "1000.00")], on=date(2027, 3, 31))
    invoicing.reverse_document(doc, user=t.a1, date=date(2027, 4, 5), reason="duplicate")
    assert box(compute(t), 7) == (D("1000.00"), D("0"), D("150.00"))
    assert box(compute(t, Q2), 7) == (D("-1000.00"), D("0"), D("-150.00"))


def test_at47_r56_deposit_and_receipts_in_no_box(t):
    original = sale(t)
    receipt = invoicing.create_receipt(t.alm, user=t.clerk, customer=t.customer, date=date(2027, 1, 20),
                                       amount="11500.00")
    invoicing.add_receipt_application(receipt, original, "11500.00", user=t.clerk)
    invoicing.post_receipt(receipt, user=t.clerk)
    ret = compute(t)
    assert box(ret, 1) == (D("10000.00"), D("0"), D("1500.00"))
    assert not ret.source_lines.exclude(source_type="sales_document").exists()


# --- exceptions and tie-out (R15, R16) ----------------------------------------------------------------

def test_at15_untagged_journal_blocks_review_until_acknowledged(t):
    sale(t)
    entry = journal(t, date(2027, 2, 5), [{"account": "1100", "debit": "100.00"}, {"account": "2200", "credit": "100.00"}])
    ret = prepared(t)
    found = {e["kind"]: e for e in services.exceptions(ret, user=t.a2)}
    assert found["untagged_vat_journal"]["reference"] == f"je:{entry.pk}"
    assert found["tie_out_difference"]["difference"] == D("100.00")
    with pytest.raises(TaxError) as exc:
        services.complete_review(ret, user=t.a2)
    assert exc.value.code == "exceptions_open"
    with pytest.raises(TaxError) as exc:  # the difference itself is never acknowledged
        services.acknowledge_exception(ret, user=t.a2, kind="tie_out_difference", reference="2200",
                                       decision="not_return_relevant", reason_ar="سبب")
    assert exc.value.code == "no_such_exception"
    services.acknowledge_exception(ret, user=t.a2, kind="untagged_vat_journal", reference=f"je:{entry.pk}",
                                   decision="not_return_relevant", reason_ar="قيد إعادة تصنيف لا يخص الإقرار",
                                   reason_en="Reclassification, not return relevant")
    assert services.tie_out(ret, user=t.a2)["difference_output"] == 0
    confirm_tax_points(t, ret)
    assert services.complete_review(ret, user=t.a2).status == "reviewed"


def test_r15_untagged_journal_included_by_a_manual_adjustment(t):
    entry = journal(t, date(2027, 2, 5), [{"account": "1100", "debit": "100.00"}, {"account": "2200", "credit": "100.00"}])
    ret = compute(t)
    with pytest.raises(TaxError) as exc:
        services.add_manual_adjustment(ret, user=t.a1, box_no=1, column="vat", amount_sar="100.00",
                                       reason_ar="ضريبة مستحقة", evidence=None)
    assert exc.value.code == "evidence_required"
    with pytest.raises(TaxError) as exc:
        services.add_manual_adjustment(ret, user=t.a1, box_no=6, column="vat", amount_sar="100.00",
                                       reason_ar="ضريبة مستحقة", evidence=pdf())
    assert exc.value.code == "adjustment_box"
    services.add_manual_adjustment(ret, user=t.a1, box_no=1, column="vat", amount_sar="100.00",
                                   reason_ar="ضريبة مستحقة عن إيراد سجل يدويًا", evidence=pdf(),
                                   journal_lines=[entry.lines.get(account__code="2200")])
    ret = services.submit_for_review(ret, user=t.a1)
    assert box(ret, 1)[2] == D("100.00") and services.tie_out(ret, user=t.a2)["difference_output"] == 0
    with pytest.raises(TaxError):
        services.complete_review(ret, user=t.a2)  # the journal still needs the reviewer's decision
    services.acknowledge_exception(ret, user=t.a2, kind="untagged_vat_journal", reference=f"je:{entry.pk}",
                                   decision="included_by_adjustment", reason_ar="مدرج بالتعديل اليدوي")
    assert services.complete_review(ret, user=t.a2).status == "reviewed"


def test_at16_rejected_document_needs_the_reviewers_decision(t):
    issued = approve_sales(t, invoice(t, [("rent_commercial", "1", "500.00")], zatca_profile="simplified"))
    t.zatca.reject(issued)
    ret = prepared(t)
    found = services.exceptions(ret, user=t.a2)
    assert [e["kind"] for e in found] == ["rejected_document"]
    with pytest.raises(TaxError) as exc:
        services.complete_review(ret, user=t.a2)
    assert exc.value.code == "exceptions_open"
    with pytest.raises(TaxError) as exc:  # no adjustment exists, so it cannot be "included by adjustment"
        services.acknowledge_exception(ret, user=t.a2, kind="rejected_document", reference=found[0]["reference"],
                                       decision="included_by_adjustment", reason_ar="مدرج")
    assert exc.value.code == "adjustment_missing"
    services.acknowledge_exception(ret, user=t.a2, kind="rejected_document", reference=found[0]["reference"],
                                   decision="tagged", reason_ar="صدرت فاتورة بديلة")
    assert services.complete_review(ret, user=t.a2).status == "reviewed"


# --- Art. 51 true-up (R55, AT-46) -----------------------------------------------------------------------

class FakeArt51:
    status = art51_port.Art51Status()

    def year_status(self, entity, year):
        return self.status


def test_at46_art51_true_up_in_the_last_return_of_the_year(t, settings):
    settings.TAX_ART51_PORT = "tax.test_vat_boxes.FakeArt51"
    from ledger.services_setup import create_monthly_periods

    create_monthly_periods(t.alm, 2028)
    t.today.set(date(2028, 1, 15))
    q4 = date(2027, 10, 1)
    services.change_tax_settings(t.alm, user=t.c1, vat_golive_date=q4)  # Q1 to Q3 filed outside the ERP (G4)
    for start in (Q1, Q2, date(2027, 7, 1)):
        imported(t, start, ref=f"Z-{start}", filed_on=date(2027, 10, 20))
    entry = journal(t, date(2027, 12, 31), [{"account": "1310", "debit": "1000.00"},
                                            {"account": "5300", "credit": "1000.00"}])
    entries = ((entry, D("1000.00")),)
    FakeArt51.status = art51_port.Art51Status(applies=True, final_ratio_approved=False, true_up_posted=True,
                                              true_up_entries=entries)
    from tax.conftest import close
    close(t, "2027-10", "2027-11", "2027-12")
    ret = reviewed(t, prepared(t, q4))
    with pytest.raises(TaxError) as exc:
        services.approve_return(ret, user=t.c1)
    assert exc.value.code == "art51_true_up_missing" and "51" in str(exc.value)
    FakeArt51.status = art51_port.Art51Status(applies=True, final_ratio_approved=True, true_up_posted=False,
                                              true_up_entries=())
    with pytest.raises(TaxError) as exc:
        services.approve_return(ret, user=t.c1)
    assert exc.value.code == "art51_true_up_missing"
    FakeArt51.status = art51_port.Art51Status(applies=True, final_ratio_approved=True, true_up_posted=True,
                                              true_up_entries=entries)
    assert box(ret, 7) == (D("0"), D("1000.00"), D("1000.00"))
    assert services.tie_out(ret, user=t.a1)["difference_input"] == 0
    assert not [e for e in services.exceptions(ret, user=t.a1) if e["kind"] == "untagged_vat_journal"]
    assert services.approve_return(ret, user=t.c1).status == "approved"
    FakeArt51.status = art51_port.Art51Status()


def test_r55_default_port_reports_no_true_up_as_built(t):
    status = art51_port.InvoicingArt51Port().year_status(t.alm, 2027)
    assert not status.applies and status.true_up_vat_sar == 0


def test_acknowledgement_decision_must_fit(t):
    entry = journal(t, date(2027, 2, 5), [{"account": "1100", "debit": "10.00"}, {"account": "2200", "credit": "10.00"}])
    ret = prepared(t)
    with pytest.raises(TaxError) as exc:
        services.acknowledge_exception(ret, user=t.a2, kind="untagged_vat_journal", reference=f"je:{entry.pk}",
                                       decision=VatExceptionAck.Decision.TAGGED, reason_ar="سبب")
    assert exc.value.code == "decision_invalid"
    with pytest.raises(TaxError) as exc:
        services.acknowledge_exception(ret, user=t.a2, kind="untagged_vat_journal", reference=f"je:{entry.pk}",
                                       decision="not_return_relevant", reason_ar="")
    assert exc.value.code == "reason_required"
