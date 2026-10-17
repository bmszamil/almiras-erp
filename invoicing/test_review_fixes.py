"""Regression tests for the slice-1 code review (review items 1 to 17; reviewer probes probe4 p1 to p5)."""
from datetime import date, time
from unittest import mock

import pytest
from django.db import connection

from core import access
from invoicing import services
from invoicing.conftest import D, approve, final, invoice, usd_rate
from invoicing.models import InvoicingError, SalesDocument
from invoicing.test_purchases import bill
from invoicing.testing import approval_day
from ledger import services as ledger
from ledger.models import FiscalPeriod

pytestmark = pytest.mark.django_db


def approve_on(w, doc, day, at=time(9, 30), user=None):
    """CFO approval on ``day`` in Riyadh, running the after-commit ZATCA hand-off like ``conftest.approve``."""
    before = len(connection.run_on_commit)
    with approval_day(day, at):
        result = services.approve_sales_document(doc, user=user or w.cfo)
    for _, callback, _ in connection.run_on_commit[before:]:
        callback()
    del connection.run_on_commit[before:]
    stored = SalesDocument.objects.get(pk=result.pk)
    stored.warnings = result.warnings
    return stored


# --- 1. over-allocation: applications are summed per target --------------------------------------

def test_p1_two_receipt_applications_cannot_overallocate_one_invoice(w):
    inv = final(w, invoice(w))  # gross 1,150.00
    r = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="2300.00")
    services.add_receipt_application(r, inv, "1150.00", user=w.clerk)
    with pytest.raises(InvoicingError) as exc:
        services.add_receipt_application(r, inv, "1150.00", user=w.clerk)
    assert exc.value.code == "application_invalid"
    with pytest.raises(InvoicingError):
        services.add_receipt_application(r, inv, "0.01", user=w.clerk)  # nothing more fits
    services.post_receipt(r, user=w.clerk2)
    assert services.open_amount(inv, user=w.clerk) == D("0.00")


def test_p1_posting_sums_applications_per_target(w):
    """Applications that each fit, but together exceed the open amount, are refused when the receipt posts."""
    inv = final(w, invoice(w))
    r = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="2300.00")
    services.add_receipt_application(r, inv, "600.00", user=w.clerk)
    services.add_receipt_application(r, inv, "550.00", user=w.clerk)
    other = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="600")
    services.add_receipt_application(other, inv, "600.00", user=w.clerk)
    services.post_receipt(other, user=w.clerk2)  # the invoice now has 550.00 open; r plans 1,150.00 on it
    with pytest.raises(InvoicingError) as exc:
        services.post_receipt(r, user=w.clerk2)
    assert exc.value.code == "document_invalid"
    r.refresh_from_db()
    assert (r.status, r.number) == ("draft", "")
    assert services.open_amount(inv, user=w.clerk) == D("550.00")


def test_p1b_two_payment_applications_cannot_overallocate_one_bill(w):
    b = services.approve_purchase_document(bill(w), user=w.cfo)
    b.refresh_from_db()
    gross = b.total_gross
    p = services.create_vendor_payment(w.alm, user=w.ap, vendor=b.vendor, date=date(2027, 1, 25), amount=str(gross * 2))
    services.add_payment_application(p, b, str(gross), user=w.ap)
    with pytest.raises(InvoicingError) as exc:
        services.add_payment_application(p, b, str(gross), user=w.ap)
    assert exc.value.code == "application_invalid"
    services.submit_vendor_payment(p, user=w.ap)
    services.approve_vendor_payment(p, user=w.cfo)
    assert services.open_amount(b, user=w.ap) == D("0.00")


def test_p1b_payment_posting_sums_applications_per_target(w):
    b = services.approve_purchase_document(bill(w), user=w.cfo)  # gross 1,150.00
    p = services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 25), amount="2000")
    services.add_payment_application(p, b, "700.00", user=w.ap)
    services.add_payment_application(p, b, "450.00", user=w.ap)
    first = services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 25), amount="100")
    services.add_payment_application(first, b, "100.00", user=w.ap)
    services.submit_vendor_payment(first, user=w.ap)
    services.approve_vendor_payment(first, user=w.cfo)
    services.submit_vendor_payment(p, user=w.ap)
    with pytest.raises(InvoicingError):
        services.approve_vendor_payment(p, user=w.cfo)
    p.refresh_from_db()
    assert (p.status, p.number) == ("submitted", "")


def test_p1_foreign_currency_applications_are_summed_per_target(w):
    usd_rate(w, "3.75", on=date(2027, 1, 10))
    inv = final(w, invoice(w, [("rent_commercial", "1", "1000.00")], currency="USD"))  # USD 1,150.00
    r = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 12), amount="2300.00",
                                currency="USD")
    services.add_receipt_application(r, inv, "1000.00", user=w.clerk)
    with pytest.raises(InvoicingError):
        services.add_receipt_application(r, inv, "151.00", user=w.clerk)
    services.add_receipt_application(r, inv, "150.00", user=w.clerk)
    services.post_receipt(r, user=w.clerk2)
    assert services.open_amount(inv, user=w.clerk) == D("0.00")


# --- 5. issue date = the approval day in Riyadh (BR-KSA-04, compliance 6b) ------------------------

def test_6b_issue_date_is_the_approval_day_and_the_draft_date_is_kept_as_requested(w):
    backdated = invoice(w, issue_date=date(2027, 1, 5))
    issued = approve_on(w, backdated, date(2027, 1, 10), time(14, 5, 7))
    assert (issued.issue_date, issued.issue_time, issued.requested_issue_date) == (
        date(2027, 1, 10), time(14, 5, 7), date(2027, 1, 5))
    assert any("2027-01-05" in warning for warning in issued.warnings)
    posted = w.zatca.clear(issued)
    assert posted.journal_entry.date == date(2027, 1, 10)


def test_6b_a_future_draft_date_is_never_the_issue_date(w):
    future = invoice(w, issue_date=date(2027, 2, 1), supply_date=date(2027, 1, 8))
    issued = approve_on(w, future, date(2027, 1, 10))
    assert issued.issue_date == date(2027, 1, 10) and issued.requested_issue_date == date(2027, 2, 1)
    assert issued.number == "ALM-INV-2027-000001"


def test_6b_issue_time_follows_the_riyadh_clock(w):
    """The real clock: issue date and time come from the same moment in Asia/Riyadh."""
    from datetime import datetime, timezone as tz

    moment = datetime(2027, 1, 10, 21, 30, tzinfo=tz.utc)  # 00:30 on 11 January in Riyadh
    with mock.patch("invoicing.services.timezone.now", return_value=moment):
        now = services._riyadh_now()
    assert (now.date(), now.time()) == (date(2027, 1, 11), time(0, 30))


# --- 3. stale approvals: a grant checked before the document last changed is refused ---------------

def _race(permission, action):
    """Patch access.require so ``action`` runs right after the ``permission`` grant is taken (before the lock)."""
    real = access.require

    def require_then_race(user, entity, perm, **kw):
        grant = real(user, entity, perm, **kw)
        if perm == permission:
            action()
        return grant

    return mock.patch.object(services.access, "require", side_effect=require_then_race)


def test_p2_approval_refused_when_the_document_changed_after_the_grant(w):
    doc = invoice(w)

    def edit():
        services.return_to_draft(doc, user=w.clerk2)
        services.update_sales_document(doc, user=w.clerk2, notes_en="changed", notes_ar="تغير")
        services.submit_sales_document(doc, user=w.clerk2)

    with _race("invoicing.approve_sales_document", edit), pytest.raises(access.AccessDenied) as exc:
        approve(w, doc)
    assert exc.value.code == "grant_document_changed"
    doc.refresh_from_db()
    assert (doc.status, doc.number) == ("submitted", "")
    posted = final(w, doc)  # a fresh approval after the change issues it and clearance posts it
    assert posted.status == "final" and posted.journal_entry.status == "posted"


def test_p2_receipt_posting_refused_when_an_application_was_added_after_the_grant(w):
    inv = final(w, invoice(w))
    r = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="1150.00")

    def add():
        services.add_receipt_application(r, inv, "1150.00", user=w.clerk)

    with _race("invoicing.post_receipt", add), pytest.raises(access.AccessDenied) as exc:
        services.post_receipt(r, user=w.clerk2)
    assert exc.value.code == "grant_document_changed"
    assert services.post_receipt(r, user=w.clerk2).status == "posted"


@pytest.mark.parametrize("kind", ["bill", "payment"])
def test_p2_bill_and_payment_approval_refused_with_a_grant_older_than_the_last_change(w, kind):
    """No service changes a submitted bill or payment (and the database refuses raw writes on PostgreSQL), so the
    race is simulated with a grant checked before the document's submission."""
    import dataclasses
    from datetime import timedelta

    if kind == "bill":
        doc, permission, approve_ = bill(w), "invoicing.approve_bill", services.approve_purchase_document
    else:
        doc = services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 25), amount="100")
        doc = services.submit_vendor_payment(doc, user=w.ap)
        permission, approve_ = "invoicing.approve_vendor_payment", services.approve_vendor_payment
    real = access.require

    def older_grant(user, entity, perm, **kw):
        grant = real(user, entity, perm, **kw)
        if perm == permission:
            return dataclasses.replace(grant, checked_at=doc.submitted_at - timedelta(seconds=1))
        return grant

    with mock.patch.object(services.access, "require", side_effect=older_grant), \
            pytest.raises(access.AccessDenied) as exc:
        approve_(doc, user=w.cfo)
    assert exc.value.code == "grant_document_changed"
    doc.refresh_from_db()
    assert (doc.status, doc.number) == ("submitted", "")


# --- 2. FX: a document books at the rate pinned at approval ----------------------------------------

def test_p3_rate_added_between_approval_and_clearance_still_posts_at_the_approved_rate(w):
    first = usd_rate(w, "3.75", on=date(2027, 1, 8))  # the latest rate on approval day is two days old
    issued = approve(w, invoice(w, [("rent_commercial", "1", "1000.00")], currency="USD"))
    assert (issued.fx_rate, issued.fx_exchange_rate_id) == (D("3.75"), first.pk)
    usd_rate(w, "3.76", on=date(2027, 1, 10))  # the rate for the issue date arrives later
    posted = w.zatca.clear(issued)
    assert posted.status == "final"
    lines = list(posted.journal_entry.lines.exclude(currency_id="SAR"))
    assert lines and all((l.fx_rate, l.exchange_rate_id) == (D("3.75"), first.pk) for l in lines)
    assert posted.journal_entry.lines.get(account__code="1200").debit == D("4312.50")  # USD 1,150 x 3.75


def test_nit15_foreign_credit_note_settles_at_clearance_without_the_approvers_rights_or_todays_rates(w):
    usd_rate(w, "3.75", on=date(2027, 1, 10))
    inv = final(w, invoice(w, [("rent_commercial", "1", "1000.00")], currency="USD"))
    usd_rate(w, "3.70", on=date(2027, 1, 20))
    cn = approve(w, invoice(w, [("rent_commercial", "1", "400.00")], doc_type="credit_note", original_document=inv,
                            issue_date=date(2027, 1, 20), payment_means_code="30", reason_ar="تخفيض"))
    ledger.create_rate(user=w.accountant, entity=w.alm, from_currency="USD", to_currency="SAR", on=date(2027, 1, 20),
                       rate=D("3.90"))  # an entity override entered later for the same date: not used
    with mock.patch.object(ledger, "get_rate", side_effect=AssertionError("no rate lookup at clearance")), \
            mock.patch.object(ledger, "fx_settlement_lines", side_effect=AssertionError("no approver rights")):
        posted = w.zatca.clear(cn)
    lines = sorted((l.account.code, l.debit, l.credit, l.line_kind) for l in posted.journal_entry.lines.all())
    assert ("1200", D("0"), D("1725.00"), "settlement") in lines  # USD 460 at the carrying 3.75
    assert ("4100", D("1480.00"), D("0"), "normal") in lines  # at the pinned 3.70, not 3.90
    assert ("5750", D("23.00"), D("0"), "realised_fx") in lines


def test_p3_the_document_line_check_names_the_pinned_rate_and_no_impossible_remedy(w):
    usd_rate(w, "3.75", on=date(2027, 1, 10))
    issued = approve(w, invoice(w, [("rent_commercial", "1", "1000.00")], currency="USD"))
    other = ledger.create_rate(user=w.accountant, entity=w.alm, from_currency="USD", to_currency="SAR",
                               on=date(2027, 1, 10), rate=D("3.80"))
    line = mock.Mock(currency_id="USD", line_kind="normal", fx_rate=D("3.80"), exchange_rate_id=other.pk)
    line.account.code = "1200"
    problems = services._check_document_rate(issued, [line])
    assert len(problems) == 1 and "approved at 3.75" in problems[0] and "draft" not in problems[0].lower()


def test_pinned_rate_rows_are_for_document_postings_only_and_must_fit_the_date(w):
    from ledger.models import LedgerError

    row = usd_rate(w, "3.75", on=date(2027, 1, 1))
    spec = [{"account": "1100", "currency": "USD", "txn_debit": D("10"), "exchange_rate": row},
            {"account": "3100", "credit": D("37.50")}]
    with pytest.raises(LedgerError) as exc:
        ledger.create_entry(w.alm, date(2027, 1, 2), "x", spec, user=w.accountant)
    assert exc.value.code == "pinned_rate_not_allowed"
    with pytest.raises(LedgerError) as exc:  # more than the age limit (5 days) after the row
        ledger._build_line(w.alm, date(2027, 1, 20), spec[0], w.accountant)
    assert exc.value.code == "pinned_rate_invalid"
    with pytest.raises(LedgerError):  # before the row's date
        ledger._build_line(w.alm, date(2026, 12, 31), spec[0], w.accountant)
    assert ledger._build_line(w.alm, date(2027, 1, 3), spec[0], w.accountant).fx_rate == D("3.75")


# --- 6. resubmitting a document whose ZATCA hand-off failed after commit ---------------------------

def _failed_hand_off(w):
    """An issued document whose after-commit hand-off raised: still not_submitted."""
    with mock.patch.object(w.zatca, "submit_document", side_effect=RuntimeError("connection reset")):
        with pytest.raises(RuntimeError):
            approve(w, invoice(w))
    doc = SalesDocument.objects.get(status="issued")
    assert doc.zatca_status == "not_submitted"
    return doc


def test_item6_queue_user_resubmits_and_it_is_logged(w):
    from core.access.models import AccessLog

    doc = _failed_hand_off(w)
    again = services.resubmit_to_zatca(doc, user=w.queue)
    assert again.zatca_status == "pending" and w.zatca.submitted[-1][0] == doc.pk
    assert AccessLog.objects.filter(actor=w.queue, details__action="zatca_resubmit").count() == 1
    with pytest.raises(InvoicingError) as exc:  # now pending: nothing to resubmit
        services.resubmit_to_zatca(doc, user=w.queue)
    assert exc.value.code == "not_resubmittable"
    assert w.zatca.clear(again).status == "final"


def test_item6_only_zatca_admins_resubmit_by_hand_never_agents(w):
    from core.access.models import AccessLog
    from core.access.testing import make_agent, make_user, member

    doc = _failed_hand_off(w)
    for user in (w.accountant, w.cfo, w.clerk):  # the accountant holds zatca.retry_failed, but not as zatca_admin
        with pytest.raises(access.AccessDenied):
            services.resubmit_to_zatca(doc, user=user)
    agent = make_agent("zatca-helper")
    with pytest.raises(access.AccessDenied) as exc:
        services.resubmit_to_zatca(doc, user=agent)
    assert exc.value.code == "agent_forbidden"
    admin = make_user("zadmin")
    member(admin, w.alm, "zatca_admin")
    assert services.resubmit_to_zatca(doc, user=admin).zatca_status == "pending"
    assert AccessLog.objects.filter(actor=admin, details__action="zatca_resubmit").exists()


# --- 7. reconcile_subledger as of a date before a later reversal -----------------------------------

def test_p5_reconcile_as_of_a_date_before_a_later_reversal_ties_out(w):
    b = services.approve_purchase_document(bill(w), user=w.cfo)  # 2027-01-15
    r = services.post_receipt(services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20),
                                                      amount="500.00"), user=w.clerk2)
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-01").update(status="closed")
    services.reverse_document(b, user=w.accountant)  # reversal dated 2027-02-01, the next open period
    services.reverse_document(r, user=w.accountant)
    for as_of, ap, ar in ((date(2027, 1, 31), D("-1150.00"), D("-500.00")), (date(2027, 2, 28), D("0"), D("0"))):
        rows = {row["account"]: row for row in services.reconcile_subledger(w.alm, as_of, user=w.accountant)}
        assert all(row["difference"] == D("0") for row in rows.values()), rows
        assert rows["2100"]["subledger"] == ap and rows["1200"]["subledger"] == ar


# --- nits 12, 13, 14, 17 ---------------------------------------------------------------------------

def test_nit12_unknown_reference_codes_and_line_prefixes_are_bilingual(w):
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10),
                                         supply_date=date(2027, 1, 10))
    for kwargs in ({"income_type": "no_such_type"}, {"income_type": "rent_commercial", "vat_category": "E",
                                                     "vat_exemption_reason": "VATEX-SA-NOPE"}):
        with pytest.raises(InvoicingError) as exc:
            services.add_sales_line(doc, user=w.clerk, quantity="1", unit_price="10", description_ar="بند", **kwargs)
        assert exc.value.code == "unknown_reference" and " / " in exc.value.messages[0]
    with pytest.raises(InvoicingError) as exc:
        services.update_sales_document(doc, user=w.clerk, reason_code="no_such_reason")
    assert exc.value.code == "unknown_reference"
    assert services._on_line(3, "Broken. / معطل.") == "Line 3: Broken. / البند 3: معطل."


def test_nit13_inactive_exemption_reasons_are_refused(w):
    from invoicing.models import VatExemptionReason

    VatExemptionReason.objects.filter(code="VATEX-SA-29").update(is_active=False)
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10),
                                         supply_date=date(2027, 1, 10))
    with pytest.raises(InvoicingError) as exc:
        services.add_sales_line(doc, user=w.clerk, income_type="other", income_account="4900", quantity="1",
                                unit_price="10", description_ar="بند", vat_category="E",
                                vat_exemption_reason="VATEX-SA-29")
    assert "no longer in use" in str(exc.value.messages)


def test_nit14_build_lines_hooks_refuse_documents_in_the_wrong_status(w):
    from invoicing.models import CustomerReceipt

    issued = approve(w, invoice(w))  # issued, ZATCA pending
    draft_receipt = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20),
                                            amount="10")
    with services._posting_as(w.cfo):
        with pytest.raises(InvoicingError) as exc:
            services._sales_document_lines(issued)
        assert exc.value.code == "not_cleared"
        with pytest.raises(InvoicingError) as exc:
            services._receipt_lines(CustomerReceipt.objects.get(pk=draft_receipt.pk))
        assert exc.value.code == "post_wrong_status"
        posted = w.zatca.clear(SalesDocument.objects.get(pk=issued.pk))
        with pytest.raises(InvoicingError) as exc:  # already booked
            services._sales_document_lines(posted)
        assert exc.value.code == "post_wrong_status"


def test_nit17_bill_with_a_posted_vendor_credit_note_cannot_be_reversed(w):
    b = services.approve_purchase_document(bill(w), user=w.cfo)
    vcn = services.approve_purchase_document(
        bill(w, [("5300", "1", "100.00")], number="VCN-1", doc_type="vendor_credit_note", original_document=b),
        user=w.cfo)
    from invoicing.models import PayableAllocation

    services.unallocate(PayableAllocation.objects.get(vendor_credit_note=vcn), user=w.ap)  # no allocations left
    with pytest.raises(InvoicingError) as exc:
        services.reverse_document(b, user=w.accountant)
    assert exc.value.code == "has_credit_notes"
    services.reverse_document(vcn, user=w.accountant)
    assert services.reverse_document(b, user=w.accountant).status == "reversed"
