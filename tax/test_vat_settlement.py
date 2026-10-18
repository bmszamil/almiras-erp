"""Corrections, amendments, payments and refunds (tax-returns.md R26 to R28, R33 to R36; AT-25 to AT-27, AT-31, AT-32)."""
from datetime import date

import pytest

from core import access
from core.access.testing import member
from ledger.models import FiscalPeriod
from tax import jobs, services
from tax.conftest import D, approved, bill, box, close, close_q1, filed, journal, pdf, reviewed, sale
from tax.models import TaxAlert, TaxError, VatCorrection, VatReturn

pytestmark = pytest.mark.django_db
Q1, Q2 = date(2027, 1, 1), date(2027, 4, 1)


def lines_of(entry):
    return sorted((l.account.code, l.debit, l.credit) for l in entry.lines.select_related("account"))


def filed_q1(t, with_sale=True):
    if with_sale:
        sale(t)
    close_q1(t)
    return filed(t, approved(t))


def correction(t, original, amount, entries=(), on=date(2027, 5, 1)):
    return services.record_correction(original, user=t.a1, discovered_on=on, description_en="Missed invoice",
                                      description_ar="فاتورة لم تُدرج", tax_difference_sar=amount,
                                      correcting_entries=entries)


def test_at26_treatment_by_threshold(t):
    q1 = filed_q1(t)
    small = correction(t, q1, "14999.99")
    assert (small.treatment, small.notify_by) == ("next_return", None)
    big = correction(t, q1, "15000.00")
    small.refresh_from_db()
    assert (big.treatment, big.notify_by) == ("amendment", date(2027, 5, 21))
    assert (small.treatment, small.notify_by) == ("amendment", date(2027, 5, 21))  # C6: netted, re-classified
    over = correction(t, q1, "-20000.00")  # net of the period now 9,999.99: below the threshold
    assert over.treatment == "next_return"
    small.refresh_from_db()
    assert small.treatment == "amendment"  # the net only re-classifies upwards
    alerts = jobs.deadline_alerts(today=date(2027, 5, 1))
    assert [a.kind for a in alerts if a.correction_id == big.pk] == ["correction_notify"]
    assert "إفصاح" in TaxAlert.objects.get(correction=big).message_ar
    with pytest.raises(TaxError) as exc:
        correction(t, q1, "-50000.00", on=date(2033, 1, 1))
    assert exc.value.code == "correction_time_barred"
    with pytest.raises(TaxError) as exc:
        correction(t, services.compute_draft(t.alm, Q2, user=t.a1), "10.00")
    assert exc.value.code == "original_not_filed"


def test_at26_large_overstatement_is_claimed_in_a_next_return(t):
    q1 = filed_q1(t)
    assert correction(t, q1, "-20000.00").treatment == "overstatement_next_return"


def test_at25_correcting_entry_excluded_and_reported_in_box14(t):
    q1 = filed_q1(t)
    missed = sale(t, on=date(2027, 4, 15))  # the Q1 invoice issued late: VAT 1,500.00
    correction(t, q1, "1500.00", entries=[missed.journal_entry])
    q2 = services.compute_draft(t.alm, Q2, user=t.a1)
    assert box(q2, 1) == (D("0"), D("0"), D("0"))
    assert box(q2, 14)[2] == D("1500.00") and box(q2, 16)[2] == D("1500.00")
    tie = services.tie_out(q2, user=t.a1)
    assert (tie["difference_output"], tie["difference_input"]) == (0, 0)
    corr = VatCorrection.objects.get()
    assert (corr.status, corr.target_return_id) == ("assigned", q2.pk)
    close(t, "2027-04", "2027-05", "2027-06")
    t.today.set(date(2027, 7, 10))
    q2 = services.submit_for_review(q2, user=t.a1)
    services.acknowledge_exception(q2, user=t.a2, kind="correcting_entry", reference=f"je:{missed.journal_entry_id}",
                                   decision="confirmed_correction", reason_ar="فاتورة الربع الأول المتأخرة")
    q2 = services.approve_return(reviewed(t, q2), user=t.c1)
    assert q2.settlement_offset_sar == 0
    filed(t, q2, on=date(2027, 7, 20), ref="Z-Q2")
    assert VatCorrection.objects.get().status == "filed"


def test_r26_a_correcting_entry_belongs_to_one_correction(t):
    q1 = filed_q1(t)
    missed = sale(t, on=date(2027, 4, 15))
    correction(t, q1, "1500.00", entries=[missed.journal_entry])
    with pytest.raises(TaxError) as exc:
        correction(t, q1, "1500.00", entries=[missed.journal_entry])
    assert exc.value.code == "correcting_entry_invalid"


def vat_journal(t, amount, on=date(2027, 5, 2)):
    """A correcting journal for output VAT not booked before: Dr 1100 / Cr 2200."""
    return journal(t, on, [{"account": "1100", "debit": amount}, {"account": "2200", "credit": amount}])


def test_at27_amendment_by_voluntary_disclosure(t):
    q1 = filed_q1(t)
    corr = correction(t, q1, "20000.00", entries=[vat_journal(t, "20000.00")])
    with pytest.raises(TaxError):  # nothing to amend for an unrelated period
        services.create_amendment(services.compute_draft(t.alm, Q2, user=t.a1), user=t.a1)
    v1 = services.create_amendment(q1, user=t.a1)
    assert (v1.version, v1.amends_id, v1.due_date) == (1, q1.pk, corr.notify_by)
    assert box(v1, 1) == box(q1, 1) and box(v1, 14)[2] == D("20000.00") and box(v1, 16)[2] == D("21500.00")
    v1 = services.approve_return(reviewed(t, services.submit_for_review(v1, user=t.a1)), user=t.c1)
    assert v1.settlement_entry is None
    with pytest.raises(TaxError) as exc:
        services.record_filing(v1, user=t.a1, zatca_filing_reference="Z-V1", filed_on=date(2027, 5, 10),
                               acknowledgement=pdf())
    assert exc.value.code == "disclosure_ref_required"
    v1 = services.record_filing(v1, user=t.a1, zatca_filing_reference="Z-V1", filed_on=date(2027, 5, 10),
                                acknowledgement=pdf(), voluntary_disclosure_ref="VD-2027-1")
    assert VatReturn.objects.get(pk=q1.pk).status == "amended"
    corr.refresh_from_db()
    assert (corr.status, corr.voluntary_disclosure_ref) == ("filed", "VD-2027-1")
    with pytest.raises(TaxError):
        services.create_vat_payment(q1, user=t.a1, amount_sar="1.00", payment_date=date(2027, 5, 11),
                                    bank_account="1100")
    payment = services.create_vat_payment(v1, user=t.a1, amount_sar="21500.00", payment_date=date(2027, 5, 11),
                                          bank_account="1100")
    services.submit_tax_document(payment, user=t.a1)
    assert services.approve_tax_document(payment, user=t.c1).vat_return_id == v1.pk
    assert VatReturn.objects.get(pk=v1.pk).status == "settled"
    assert services.open_amount(v1, user=t.v1) == 0


def test_cancelled_correction_leaves_box14(t):
    q1 = filed_q1(t)
    corr = correction(t, q1, "100.00")
    q2 = services.compute_draft(t.alm, Q2, user=t.a1)
    assert box(q2, 14)[2] == D("100.00")
    services.cancel_correction(corr, user=t.a1, reason="Not an error")
    assert box(services.recompute_draft(q2, user=t.a1), 14)[2] == D("0")


def pay(t, ret, amount, on=date(2027, 4, 25), creator=None):
    payment = services.create_vat_payment(ret, user=creator or t.a1, amount_sar=amount, payment_date=on,
                                          bank_account="1100", sadad_bill_number="SADAD-1")
    return services.submit_tax_document(payment, user=creator or t.a1)


def test_at31_payment_segregation_t2_and_reversal_in_open_period(t):
    q1 = filed_q1(t)
    payment = pay(t, q1, "1500.00")
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_tax_document(payment, user=t.a1)
    assert exc.value.code == "no_membership"
    member(t.a1, t.alm, "approver")
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_tax_document(payment, user=t.a1)
    assert exc.value.code == "self_approval"
    posted = services.approve_tax_document(payment, user=t.c1)
    assert lines_of(posted.journal_entry) == [("1100", 0, D("1500.00")), ("2200", D("1500.00"), 0)]
    assert (posted.journal_entry.posted_by, VatReturn.objects.get(pk=q1.pk).status) == (t.c1, "settled")
    close(t, "2027-04")
    reversed_ = services.reverse_tax_document(posted, user=t.a1, reason="Paid from the wrong account")
    assert (reversed_.status, reversed_.reversal_entry.date) == ("reversed", date(2027, 5, 1))
    assert VatReturn.objects.get(pk=q1.pk).status == "filed"
    with pytest.raises(TaxError):
        services.reverse_tax_document(reversed_, user=t.a1, reason="again")


def test_at32_overpayment_refused(t):
    q1 = filed_q1(t)
    services.approve_tax_document(pay(t, q1, "1000.00"), user=t.c1)
    with pytest.raises(TaxError) as exc:
        services.approve_tax_document(pay(t, q1, "501.00"), user=t.c1)
    assert exc.value.code == "overpayment" and "تتجاوز" in str(exc.value)
    services.approve_tax_document(pay(t, q1, "500.00"), user=t.c1)
    assert services.open_amount(q1, user=t.a1) == 0


def test_r35_payment_needs_an_approved_return_and_a_bank_account(t):
    draft = services.compute_draft(t.alm, Q1, user=t.a1)
    with pytest.raises(TaxError) as exc:
        services.create_vat_payment(draft, user=t.a1, amount_sar="1.00", payment_date=date(2027, 4, 25),
                                    bank_account="1100")
    assert exc.value.code == "return_not_payable"
    q1 = filed_q1(t)
    for bank, code in (("2200", "bank_account_invalid"), ("1140", "bank_account_invalid")):
        with pytest.raises(TaxError) as exc:
            services.create_vat_payment(q1, user=t.a1, amount_sar="1.00", payment_date=date(2027, 4, 25),
                                        bank_account=bank)
        assert exc.value.code == code
    with pytest.raises(TaxError) as exc:
        services.create_vat_payment(q1, user=t.a1, amount_sar="1.001", payment_date=date(2027, 4, 25),
                                    bank_account="1100")
    assert exc.value.code == "amount_decimals"


def test_refund_posts_t4_and_settles_the_claim(t):
    sale(t, [("rent_commercial", "1", "1000.00")])
    bill(t, [("5300", "1", "10000.00")])
    ret = services.compute_draft(t.alm, Q1, user=t.a1)
    ret = services.set_refund_requested(ret, user=t.a1, requested=True)
    close_q1(t)
    ret = services.approve_return(reviewed(t, services.submit_for_review(ret, user=t.a1)), user=t.c1)
    ret = filed(t, ret)
    assert (ret.status, ret.net_vat_sar) == ("filed", D("-1350.00"))
    refund = services.create_vat_refund(ret, user=t.a1, amount_sar="1350.00", received_on=date(2027, 5, 20),
                                        bank_account="1100", zatca_reference="REF-1")
    services.submit_tax_document(refund, user=t.a1)
    with pytest.raises(TaxError):
        services.create_vat_refund(ret, user=t.a1, amount_sar="1350.00", received_on=date(2027, 5, 20),
                                   bank_account="1100", zatca_reference="")
    posted = services.approve_tax_document(refund, user=t.c1)
    assert lines_of(posted.journal_entry) == [("1100", D("1350.00"), 0), ("1310", 0, D("1350.00"))]
    assert VatReturn.objects.get(pk=ret.pk).status == "settled"
    extra = services.create_vat_refund(ret, user=t.a1, amount_sar="0.01", received_on=date(2027, 5, 21),
                                       bank_account="1100", zatca_reference="REF-2")
    services.submit_tax_document(extra, user=t.a1)
    with pytest.raises(TaxError) as exc:
        services.approve_tax_document(extra, user=t.c1)
    assert exc.value.code == "refund_exceeds_claim"


def test_r36_import_record_reversal_counts_in_its_own_period(t):
    from tax.test_vat_boxes import import_record

    rec = import_record(t, "CUS-9", "1000.00", "150.00", on=date(2027, 3, 1))
    close_q1(t)
    reversed_ = services.reverse_tax_document(rec, user=t.a1, reason="Duplicate declaration")
    assert reversed_.reversal_entry.date == date(2027, 4, 1)
    assert box(services.compute_draft(t.alm, Q2, user=t.a1), 8) == (D("-1000.00"), D("0"), D("-150.00"))
    assert FiscalPeriod.objects.get(entity=t.alm, name="2027-03").status == "closed"


def test_r34_tax_documents_are_registered_with_their_permissions():
    from ledger import documents

    assert documents.get("tax.vatreturn").post_permissions == {"tax.approve_return"}
    assert documents.get("tax.vatreturn").reverse_permissions == frozenset()
    for label in ("tax.importvatrecord", "tax.taxpayment", "tax.taxrefund"):
        doc_type = documents.get(label)
        assert (doc_type.post_permissions, doc_type.reverse_permissions) == (
            {"tax.approve_payment"}, {"tax.reverse_document"})
