"""Owner decisions of 2026-10-19 (`docs/approvals/owner-answers-2026-10-18.md` section I) in the VAT slice: I3
(a B2G invoice under VAT IR Art. 20(5) is taxed on its payment order date: return period and SAMA rate follow it;
tax-returns §10.7) together with I2 (latest published SAMA rate). Anonymised data, made-up rates."""
from datetime import date

import pytest

from invoicing import services as invoicing
from invoicing.conftest import ADDRESS, final, usd_rate
from tax import services
from tax.conftest import D, box

pytestmark = pytest.mark.django_db
Q1, Q2 = date(2027, 1, 1), date(2027, 4, 1)


def _government(t):
    return invoicing.create_customer(
        t.alm, user=t.clerk, code="G001", customer_kind="government", name_en="Test Ministry",
        name_ar="وزارة الاختبار", is_vat_registered=True, vat_number="310000000000003", **ADDRESS)


def _b2g_usd_sale(t, *, issue, payment_order, lines=(("rent_commercial", "1", "1000.00"),)):
    doc = invoicing.create_sales_document(t.alm, user=t.clerk, customer=_government(t), issue_date=issue,
                                          currency="USD", supply_date=issue, payment_order_date=payment_order)
    for income_type, qty, price in lines:
        invoicing.add_sales_line(doc, user=t.clerk, income_type=income_type, quantity=qty, unit_price=price,
                                 description_ar="بند اختبار", description_en="Test line")
    return final(t, invoicing.submit_sales_document(doc, user=t.clerk))


def test_i3_a_b2g_sale_issued_in_q1_with_a_q2_payment_order_is_reported_in_q2_and_ties_out(t):
    """AT-159: issued 2027-03-30 (Q1), payment order 2027-04-05 (Q2). SAMA 3.7500 on 03-30, 3.7600 on 04-05 (test
    data). Box 1 in Q2 at 3.7600; booked on 03-30 at 3.7500; the tie-out explains the 1.50 (§10.7)."""
    usd_rate(t, "3.7500", on=date(2027, 3, 30))
    usd_rate(t, "3.7600", on=date(2027, 4, 5))
    doc = _b2g_usd_sale(t, issue=date(2027, 3, 30), payment_order=date(2027, 4, 5))
    assert (doc.tax_point_date, doc.tax_point_basis, doc.total_vat_sar) == (date(2027, 4, 5), "payment_order",
                                                                           D("564.00"))
    assert doc.journal_entry.date == date(2027, 3, 30)
    q1 = services.compute_draft(t.alm, Q1, user=t.a1)
    assert box(q1, 1) == (D("0"), D("0"), D("0"))
    tie = services.tie_out(q1, user=t.a1)
    assert (len(tie["shifted_out"]), tie["difference_output"]) == (1, D("0"))
    assert "period_shift" in {e["kind"] for e in services.exceptions(q1, user=t.a1)}
    q2 = services.compute_draft(t.alm, Q2, user=t.a1)
    assert box(q2, 1) == (D("3760.00"), D("0"), D("564.00"))
    tie = services.tie_out(q2, user=t.a1)
    assert len(tie["shifted_in"]) == 1
    assert (tie["rate_difference_output"], tie["ledger_output"], tie["difference_output"]) == (
        D("-1.50"), D("564.00"), D("0"))


def test_i3_a_b2g_sale_with_an_earlier_payment_order_ties_out_without_a_rate_difference(t):
    """AT-160: payment order 03-05, issued 03-31: Q1, booked at the 03-05 SAMA row (H2, R51), no difference."""
    usd_rate(t, "3.7500", on=date(2027, 3, 5))
    usd_rate(t, "3.7510", on=date(2027, 3, 31))
    doc = _b2g_usd_sale(t, issue=date(2027, 3, 31), payment_order=date(2027, 3, 5))
    assert (doc.tax_point_date, doc.fx_rate_source, doc.total_vat_sar) == (date(2027, 3, 5), "sama_tp", D("562.50"))
    q1 = services.compute_draft(t.alm, Q1, user=t.a1)
    assert box(q1, 1)[2] == D("562.50")
    tie = services.tie_out(q1, user=t.a1)
    assert (tie["rate_difference_output"], tie["difference_output"]) == (D("0"), D("0"))
