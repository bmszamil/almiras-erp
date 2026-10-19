"""Tax point and VAT SAR rate (invoicing R100 as amended by owner decisions G6 / G7, AT-138 to AT-146) and the B2G
payment order date (R117, G1, AT-147 to AT-150), which is the tax point of a B2G invoice (owner decision I3,
AT-157 to AT-160). Anonymised entities and made-up rates (R70)."""
import importlib
from datetime import date
from types import SimpleNamespace

import pytest
from django.utils import timezone
from django.apps import apps as django_apps
from django.db import connection

from invoicing import services
from invoicing.conftest import ADDRESS, D, approve, final, invoice, usd_rate
from invoicing.models import InvoicingError, SalesDocument
from ledger.models import JournalLine, NonPublicationDay

pytestmark = pytest.mark.django_db


def messages(exc):
    return " ".join(exc.value.messages)


def closed(w, *days):
    for day in days:
        NonPublicationDay.objects.create(date=day, name_en="Weekend", name_ar="عطلة نهاية الأسبوع", created_by=w.cfo,
                                         verified_by=w.cfo, verified_at=timezone.now())


def draft(w, *, issue_date, lines=(("rent_commercial", "1", "1000.00"),), doc_type="invoice", **fields):
    fields.setdefault("supply_date", issue_date)
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=fields.pop("customer", w.customer),
                                         issue_date=issue_date, doc_type=doc_type, **fields)
    for income_type, qty, price in lines:
        services.add_sales_line(doc, user=w.clerk, income_type=income_type, quantity=qty, unit_price=price,
                                description_ar="بند اختبار", description_en="Test line")
    return SalesDocument.objects.get(pk=doc.pk)


# --- G6: the tax point is computed (R100 as amended) ------------------------------------------------------------

def test_at138_tax_point_is_the_supply_date_and_drives_the_sama_rate(w):
    """ledger-fx AT-43: supply 2027-03-10, issued 2027-03-31; SAMA 3.7500 on 03-10, 3.7510 on 03-31."""
    on_supply = usd_rate(w, "3.7500", on=date(2027, 3, 10))
    usd_rate(w, "3.7510", on=date(2027, 3, 31))
    doc = approve(w, invoice(w, currency="USD", issue_date=date(2027, 3, 31), supply_date=date(2027, 3, 10)))
    assert (doc.tax_point_date, doc.tax_point_basis) == (date(2027, 3, 10), "supply")
    assert (doc.sar_rate, doc.vat_sar_exchange_rate_id) == (D("3.7500"), on_supply.pk)
    assert doc.total_vat_sar == D("562.50") and doc.vat_breakdown[0]["vat_sar"] == "562.50"
    # booking (owner decision H2, R100(d)): the whole document at the SAMA tax-point row, although it is 21 days
    # older than the issue date (ledger-fx R51), so 2200 equals total_vat_sar
    assert (doc.fx_rate, doc.fx_exchange_rate_id, doc.fx_rate_source) == (D("3.7500"), on_supply.pk, "sama_tp")
    posted = w.zatca.clear(doc)
    vat = JournalLine.objects.get(entry=posted.journal_entry, account__code="2200")
    assert vat.credit == D("562.50") == doc.total_vat_sar


def test_at139_continuous_supply_uses_the_end_of_the_supply_period(w):
    doc = draft(w, issue_date=date(2027, 3, 31), supply_date=date(2027, 3, 1), supply_end_date=date(2027, 3, 20))
    assert (doc.tax_point_date, doc.tax_point_basis) == (date(2027, 3, 20), "supply_end")
    doc = services.update_sales_document(doc, user=w.clerk, supply_end_date=date(2027, 4, 30))
    assert (doc.tax_point_date, doc.tax_point_basis) == (date(2027, 3, 31), "issue")  # billed before the end


def test_at140_an_earlier_payment_or_advance_is_the_tax_point(w):
    doc = draft(w, issue_date=date(2027, 3, 31), supply_date=date(2027, 3, 20), payment_received_date=date(2027, 3, 5))
    assert (doc.tax_point_date, doc.tax_point_basis) == (date(2027, 3, 5), "payment")
    later = draft(w, issue_date=date(2027, 3, 31), supply_date=date(2027, 3, 20),
                  payment_received_date=date(2027, 4, 15))
    assert (later.tax_point_date, later.tax_point_basis) == (date(2027, 3, 20), "supply")


def test_at141_supply_after_issue_or_missing_uses_the_issue_date(w):
    after = draft(w, issue_date=date(2027, 3, 15), supply_date=date(2027, 3, 20), currency="USD")
    assert (after.tax_point_date, after.tax_point_basis) == (date(2027, 3, 15), "issue")
    same = draft(w, issue_date=date(2027, 3, 15), supply_date=date(2027, 3, 15))
    assert same.tax_point_basis == "issue"  # equal dates: the issue date is named (G7: the two coincide)
    usd_rate(w, "3.7520", on=date(2027, 3, 15))
    issued = approve(w, services.submit_sales_document(after, user=w.clerk))
    assert issued.tax_point_date == issued.issue_date == date(2027, 3, 15) and issued.sar_rate == D("3.7520")


def test_at142_notes_are_taxed_on_their_own_date(w):
    original = final(w, invoice(w, issue_date=date(2027, 1, 10)))
    note = services.create_sales_document(
        w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 2, 10), doc_type="credit_note",
        original_document=original, supply_date=date(2027, 1, 10), reason_code="value_amendment", payment_means_code="10")
    assert (note.tax_point_date, note.tax_point_basis) == (date(2027, 2, 10), "issue")
    with pytest.raises(InvoicingError) as exc:
        services.set_tax_point(note, user=w.clerk, tax_point_date=date(2027, 1, 10), reason="the supply")
    assert exc.value.code == "tax_point_note"


def test_at143_tax_point_set_by_hand_in_draft_only_with_a_reason(w):
    doc = draft(w, issue_date=date(2027, 3, 31), supply_date=date(2027, 3, 31))
    with pytest.raises(InvoicingError) as exc:
        services.set_tax_point(doc, user=w.clerk, tax_point_date=date(2027, 3, 12), reason=" ")
    assert exc.value.code == "tax_point_reason" and "سبب" in messages(exc)
    with pytest.raises(InvoicingError) as exc:
        services.set_tax_point(doc, user=w.clerk, tax_point_date=date(2027, 4, 1), reason="later")
    assert exc.value.code == "tax_point_after_issue" and "الأسبق" in messages(exc)
    doc = services.set_tax_point(doc, user=w.clerk, tax_point_date=date(2027, 3, 12),
                                 reason="Goods delivered on 03-12 per delivery note DN-1")
    assert (doc.tax_point_date, doc.tax_point_basis) == (date(2027, 3, 12), "manual")
    doc = services.update_sales_document(doc, user=w.clerk, supply_date=date(2027, 3, 1))
    assert doc.tax_point_date == date(2027, 3, 12)  # kept until cleared
    usd = services.set_tax_point(doc, user=w.clerk, tax_point_date=None)
    assert (usd.tax_point_date, usd.tax_point_basis, usd.tax_point_reason) == (date(2027, 3, 1), "supply", "")
    services.set_tax_point(usd, user=w.clerk, tax_point_date=date(2027, 3, 12), reason="delivery note DN-1")
    issued = approve(w, services.submit_sales_document(usd, user=w.clerk))
    assert (issued.tax_point_date, issued.tax_point_basis) == (date(2027, 3, 12), "manual")
    assert issued.tax_point_reason == "delivery note DN-1"
    with pytest.raises(InvoicingError) as exc:  # issued: never changed again (R13)
        services.set_tax_point(issued, user=w.clerk, tax_point_date=date(2027, 3, 1), reason="again")
    assert exc.value.code == "not_draft"


def test_at143_hand_tax_point_after_a_moved_issue_date_is_refused_at_submission(w):
    doc = draft(w, issue_date=date(2027, 3, 31))
    services.set_tax_point(doc, user=w.clerk, tax_point_date=date(2027, 3, 20), reason="delivery")
    services.update_sales_document(doc, user=w.clerk, issue_date=date(2027, 3, 15), supply_date=date(2027, 3, 15))
    with pytest.raises(InvoicingError) as exc:
        services.submit_sales_document(doc, user=w.clerk)
    assert "2027-03-20" in messages(exc) and "تاريخ الاستحقاق الضريبي" in messages(exc)


# --- G7: SAMA rate on the tax point --------------------------------------------------------------------------

def test_at144_i2_any_day_without_a_sama_rate_falls_back_to_the_last_published_one(w):
    """AT-108 / AT-144 as amended by owner decision I2: SAMA row only on Thursday 2027-03-04; the weekend is recorded
    as non-publication days, Sunday 03-07 (a publication day) has no row: both use the 03-04 rate."""
    usd_rate(w, "3.7500", on=date(2027, 3, 4))
    closed(w, date(2027, 3, 5), date(2027, 3, 6))
    sat = approve(w, invoice(w, currency="USD", issue_date=date(2027, 3, 6)))
    assert (sat.tax_point_date, sat.sar_rate, sat.total_vat_sar) == (date(2027, 3, 6), D("3.7500"), D("562.50"))
    sunday = approve(w, invoice(w, currency="USD", issue_date=date(2027, 3, 8), supply_date=date(2027, 3, 7)))
    assert (sunday.tax_point_date, sunday.sar_rate, sunday.status) == (date(2027, 3, 7), D("3.7500"), "issued")


def test_i2_no_sama_rate_on_or_before_the_tax_point_is_refused(w):
    usd_rate(w, "3.7500", on=date(2027, 3, 8))  # only after the tax point
    early = invoice(w, currency="USD", issue_date=date(2027, 3, 8), supply_date=date(2027, 3, 7))
    with pytest.raises(InvoicingError) as exc:
        approve(w, early)
    assert exc.value.code == "sama_rate_missing"
    assert "2027-03-07" in messages(exc) and "البنك المركزي" in messages(exc)
    stored = SalesDocument.objects.get(pk=early.pk)
    assert (stored.status, stored.number) == ("submitted", "")  # nothing issued, no number used


def test_at145_only_sama_rows_count_for_vat_and_book_when_the_ledger_accepts_them(w):
    """AT-126 as amended: a manual row on the tax point is ignored; the SAMA row is also the booking rate."""
    sama = usd_rate(w, "3.7510", on=date(2027, 3, 31))
    usd_rate(w, "3.7600", on=date(2027, 3, 31), source="manual")  # the ledger would prefer it (R12)
    doc = approve(w, invoice(w, currency="USD", issue_date=date(2027, 3, 31)))
    assert (doc.sar_rate, doc.vat_sar_exchange_rate_id, doc.fx_rate, doc.fx_exchange_rate_id) == (
        D("3.7510"), sama.pk, D("3.7510"), sama.pk)
    assert doc.fx_rate_source == "sama"
    posted = w.zatca.clear(doc)
    assert JournalLine.objects.get(entry=posted.journal_entry, account__code="2200").credit == doc.total_vat_sar


def test_at145_manual_rate_alone_does_not_issue(w):
    usd_rate(w, "3.7600", on=date(2027, 3, 31), source="manual")
    doc = invoice(w, currency="USD", issue_date=date(2027, 3, 31))
    with pytest.raises(InvoicingError) as exc:
        approve(w, doc)
    assert exc.value.code == "sama_rate_missing"


def test_at146_sar_documents_need_no_rate_row(w):
    doc = approve(w, invoice(w, issue_date=date(2027, 3, 31), supply_date=date(2027, 3, 20)))
    assert (doc.tax_point_date, doc.sar_rate, doc.vat_sar_exchange_rate_id) == (date(2027, 3, 20), D("1"), None)


def test_at146_migration_sets_the_issue_date_as_a_legacy_tax_point(w):
    issued = approve(w, invoice(w, issue_date=date(2027, 1, 10), supply_date=date(2027, 1, 5)))
    open_draft = draft(w, issue_date=date(2027, 1, 12), supply_date=date(2027, 1, 2))
    if connection.vendor == "postgresql":  # as the table was before migration 0026
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("ALTER TABLE invoicing_salesdocument DISABLE TRIGGER invoicing_salesdocument_lock")
            cursor.execute("UPDATE invoicing_salesdocument SET tax_point_date = NULL, tax_point_basis = ''")
            cursor.execute("ALTER TABLE invoicing_salesdocument ENABLE TRIGGER invoicing_salesdocument_lock")
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
    else:
        SalesDocument._base_manager.update(tax_point_date=None, tax_point_basis="")
    migration = importlib.import_module("invoicing.migrations.0026_tax_point_and_payment_order")
    migration.forwards(django_apps, SimpleNamespace(connection=connection))
    stored = {d.pk: (d.tax_point_date, d.tax_point_basis) for d in SalesDocument.objects.all()}
    assert stored[issued.pk] == (date(2027, 1, 10), "legacy")
    assert stored[open_draft.pk] == (date(2027, 1, 12), "legacy")
    assert w.zatca.clear(SalesDocument.objects.get(pk=issued.pk)).status == "final"  # still posts
    edited = services.update_sales_document(open_draft, user=w.clerk, notes_en="", notes_ar="")
    assert (edited.tax_point_date, edited.tax_point_basis) == (date(2027, 1, 2), "supply")  # computed once touched


# --- G1: B2G payment order date (R117) ------------------------------------------------------------------------

def _government(w):
    return services.create_customer(
        w.alm, user=w.clerk, code="G001", customer_kind="government", name_en="Test Ministry",
        name_ar="وزارة الاختبار", is_vat_registered=True, vat_number="310000000000003", **ADDRESS)


def test_at147_standard_document_to_a_government_customer_needs_the_payment_order_date(w):
    gov = _government(w)
    doc = draft(w, issue_date=date(2027, 1, 10), customer=gov)
    with pytest.raises(InvoicingError) as exc:
        services.submit_sales_document(doc, user=w.clerk)
    assert "payment order date" in messages(exc) and "BR-KSA-92" in messages(exc) and "أمر الدفع" in messages(exc)
    assert SalesDocument.objects.get(pk=doc.pk).status == "draft"
    services.update_sales_document(doc, user=w.clerk, payment_order_date=date(2027, 1, 5))
    issued = approve(w, services.submit_sales_document(doc, user=w.clerk))
    assert (issued.status, issued.payment_order_date) == ("issued", date(2027, 1, 5))
    # R117 as amended by owner decision I3: the payment order date is also the VAT tax point (VAT IR Art. 20(5))
    assert (issued.tax_point_date, issued.tax_point_basis) == (date(2027, 1, 5), "payment_order")


def test_at148_other_buyers_and_simplified_documents_need_no_payment_order_date(w):
    assert final(w, invoice(w)).status == "final"
    gov = _government(w)
    small = invoice(w, [("rent_commercial", "1", "100.00")], customer=gov, zatca_profile="simplified")
    assert approve(w, small).status == "issued"


def test_at149_approval_refuses_a_b2g_document_without_the_date(w, monkeypatch):
    """Defence in depth: a submitted B2G document that lost its date (e.g. created before R117) is not issued."""
    gov = _government(w)
    doc = draft(w, issue_date=date(2027, 1, 10), customer=gov, payment_order_date=date(2027, 1, 5))
    doc = services.submit_sales_document(doc, user=w.clerk)
    monkeypatch.setattr(services, "_payment_order_problems",
                        lambda d: [" / ".join(services.PAYMENT_ORDER_REQUIRED)])
    with pytest.raises(InvoicingError) as exc:
        approve(w, doc)
    assert "BR-KSA-92" in messages(exc)
    assert SalesDocument.objects.get(pk=doc.pk).number == ""


# --- I3: the payment order date is the tax point of a B2G invoice (R117 as amended) ---------------------------------

def test_i3_a_b2g_invoice_is_taxed_on_its_payment_order_date_for_the_sama_rate_and_booking(w):
    """AT-157: supply 03-10, payment order 03-20, issued 03-31: tax point 03-20 (not the earlier supply date)."""
    usd_rate(w, "3.7500", on=date(2027, 3, 10))
    on_order = usd_rate(w, "3.7520", on=date(2027, 3, 20))
    usd_rate(w, "3.7510", on=date(2027, 3, 31))
    gov = _government(w)
    doc = draft(w, issue_date=date(2027, 3, 31), supply_date=date(2027, 3, 10), customer=gov, currency="USD")
    assert (doc.tax_point_date, doc.tax_point_basis) == (date(2027, 3, 10), "supply")  # no payment order date yet
    services.update_sales_document(doc, user=w.clerk, payment_order_date=date(2027, 3, 20))
    assert SalesDocument.objects.get(pk=doc.pk).tax_point_basis == "payment_order"
    issued = approve(w, services.submit_sales_document(doc, user=w.clerk))
    assert (issued.tax_point_date, issued.tax_point_basis) == (date(2027, 3, 20), "payment_order")
    assert (issued.sar_rate, issued.vat_sar_exchange_rate_id, issued.total_vat_sar) == (
        D("3.7520"), on_order.pk, D("562.80"))
    # H2 / R51: 11 days before the issue date, so the whole document books at the payment order date's SAMA row
    assert (issued.fx_rate, issued.fx_exchange_rate_id, issued.fx_rate_source) == (D("3.7520"), on_order.pk, "sama_tp")
    posted = w.zatca.clear(issued)
    assert JournalLine.objects.get(entry=posted.journal_entry, account__code="2200").credit == D("562.80")


def test_i3_a_payment_order_date_after_the_issue_date_is_still_the_tax_point(w):
    """AT-158: issued 03-15, payment order 03-25 (Art. 20(5) defers the tax point). VAT in SAR at the SAMA rate for
    03-25 (the latest on or before it); booking at the R12 rate on the issue date, never a later day's rate (R51);
    the 2200 / VAT-in-SAR gap is explained in the VAT return tie-out (tax-returns §10.7)."""
    on_issue = usd_rate(w, "3.7500", on=date(2027, 3, 15))
    on_order = usd_rate(w, "3.7600", on=date(2027, 3, 25))
    gov = _government(w)
    doc = draft(w, issue_date=date(2027, 3, 15), customer=gov, currency="USD", payment_order_date=date(2027, 3, 25))
    issued = approve(w, services.submit_sales_document(doc, user=w.clerk))
    assert (issued.issue_date, issued.tax_point_date, issued.tax_point_basis) == (
        date(2027, 3, 15), date(2027, 3, 25), "payment_order")
    assert (issued.sar_rate, issued.vat_sar_exchange_rate_id, issued.total_vat_sar) == (
        D("3.7600"), on_order.pk, D("564.00"))
    assert (issued.fx_rate, issued.fx_exchange_rate_id, issued.fx_rate_source) == (D("3.7500"), on_issue.pk, "sama")
    assert services._sama_tax_point(issued) is None  # never booked at a SAMA tax-point rate after the entry date
    posted = w.zatca.clear(issued)
    assert posted.journal_entry.date == date(2027, 3, 15)
    lines = list(posted.journal_entry.lines.exclude(currency_id="SAR"))
    assert lines and {(l.rate_source, l.fx_rate) for l in lines} == {("table", D("3.7500"))}
    assert JournalLine.objects.get(entry=posted.journal_entry, account__code="2200").credit == D("562.50")


def test_i3_a_later_payment_order_date_without_its_own_sama_row_uses_the_latest_published_rate(w):
    """I2 with I3: at issue the SAMA rate of a later payment order date is not published yet; the latest SAMA
    rate on or before it (the issue date's) is used, and the document books at that same row."""
    on_issue = usd_rate(w, "3.7500", on=date(2027, 3, 15))
    gov = _government(w)
    doc = draft(w, issue_date=date(2027, 3, 15), customer=gov, currency="USD", payment_order_date=date(2027, 3, 25))
    issued = approve(w, services.submit_sales_document(doc, user=w.clerk))
    assert (issued.tax_point_date, issued.vat_sar_exchange_rate_id, issued.fx_exchange_rate_id) == (
        date(2027, 3, 25), on_issue.pk, on_issue.pk)
    posted = w.zatca.clear(issued)
    assert JournalLine.objects.get(entry=posted.journal_entry, account__code="2200").credit == posted.total_vat_sar


def test_i3_a_sar_b2g_invoice_is_taxed_on_the_payment_order_date_too(w):
    gov = _government(w)
    doc = draft(w, issue_date=date(2027, 1, 10), customer=gov, payment_order_date=date(2027, 1, 20))
    issued = approve(w, services.submit_sales_document(doc, user=w.clerk))
    assert (issued.tax_point_date, issued.tax_point_basis, issued.sar_rate) == (
        date(2027, 1, 20), "payment_order", D("1"))
    assert w.zatca.clear(issued).status == "final"


def test_i3_no_tax_point_by_hand_on_a_b2g_invoice_and_the_payment_order_date_replaces_one(w):
    gov = _government(w)
    doc = draft(w, issue_date=date(2027, 1, 10), customer=gov)
    with pytest.raises(InvoicingError) as exc:
        services.set_tax_point(doc, user=w.clerk, tax_point_date=date(2027, 1, 8), reason="Contract")
    assert exc.value.code == "tax_point_payment_order"
    assert "payment order date" in messages(exc) and "أمر الدفع" in messages(exc)
    stored = SalesDocument.objects.get(pk=doc.pk)
    stored.tax_point_date, stored.tax_point_basis, stored.tax_point_reason = date(2027, 1, 8), "manual", "old"
    stored.payment_order_date = date(2027, 1, 5)  # in memory: e.g. a hand tax point from before the customer kind
    services._refresh_tax_point(stored)
    assert (stored.tax_point_date, stored.tax_point_basis, stored.tax_point_reason) == (
        date(2027, 1, 5), "payment_order", "")


def test_i3_b2g_notes_simplified_documents_and_other_buyers_keep_the_r100_tax_point(w):
    gov = _government(w)
    original = final(w, invoice(w, customer=gov, payment_order_date=date(2027, 1, 5)))
    assert (original.tax_point_date, original.tax_point_basis) == (date(2027, 1, 5), "payment_order")
    note = services.create_sales_document(w.alm, user=w.clerk, customer=gov, issue_date=date(2027, 2, 10),
                                          doc_type="credit_note", original_document=original,
                                          payment_order_date=date(2027, 1, 5))
    assert (note.tax_point_date, note.tax_point_basis) == (date(2027, 2, 10), "issue")  # D30(a): its own date
    small = draft(w, issue_date=date(2027, 1, 10), customer=gov, zatca_profile="simplified",
                  payment_order_date=date(2027, 1, 5), lines=(("rent_commercial", "1", "100.00"),))
    assert (small.tax_point_date, small.tax_point_basis) == (date(2027, 1, 10), "issue")
    business = draft(w, issue_date=date(2027, 1, 10), payment_order_date=date(2027, 1, 5))
    assert (business.tax_point_date, business.tax_point_basis) == (date(2027, 1, 10), "issue")


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")
def test_at146_database_keeps_an_issued_tax_point_and_payment_order_date(w):
    from django.db import DatabaseError, transaction

    issued = approve(w, invoice(w, issue_date=date(2027, 1, 10)))
    for column, value in (("tax_point_date", date(2027, 1, 1)), ("tax_point_basis", "manual"),
                          ("payment_order_date", date(2027, 1, 1)), ("sar_rate", D("2"))):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"UPDATE invoicing_salesdocument SET {column} = %s WHERE id = %s", [value, issued.pk])
    assert SalesDocument.objects.get(pk=issued.pk).tax_point_date == date(2027, 1, 10)
