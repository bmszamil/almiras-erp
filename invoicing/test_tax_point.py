"""Tax point and VAT SAR rate (invoicing R100 as amended by owner decisions G6 / G7, AT-138 to AT-146) and the B2G
payment order date (R117, G1, AT-147 to AT-150). Anonymised entities and made-up rates (R70)."""
import importlib
from datetime import date
from types import SimpleNamespace

import pytest
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
        NonPublicationDay.objects.create(date=day, name_en="Weekend", name_ar="عطلة نهاية الأسبوع", created_by=w.cfo)


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
    on_issue = usd_rate(w, "3.7510", on=date(2027, 3, 31))
    doc = approve(w, invoice(w, currency="USD", issue_date=date(2027, 3, 31), supply_date=date(2027, 3, 10)))
    assert (doc.tax_point_date, doc.tax_point_basis) == (date(2027, 3, 10), "supply")
    assert (doc.sar_rate, doc.vat_sar_exchange_rate_id) == (D("3.7500"), on_supply.pk)
    assert doc.total_vat_sar == D("562.50") and doc.vat_breakdown[0]["vat_sar"] == "562.50"
    # booking: the 03-10 row is 21 days older than the issue date, beyond the ledger's pinned-rate window (R12),
    # so the document books at the ledger rate on its issue date and the VAT return explains the difference
    assert (doc.fx_rate, doc.fx_exchange_rate_id) == (D("3.7510"), on_issue.pk)
    posted = w.zatca.clear(doc)
    vat = JournalLine.objects.get(entry=posted.journal_entry, account__code="2200")
    assert vat.credit == D("562.65")


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

def test_at144_weekend_falls_back_to_the_last_published_sama_rate(w):
    """AT-108 as amended: SAMA row only on Thursday 2027-03-04; 03-05 and 03-06 recorded as non-publication days."""
    usd_rate(w, "3.7500", on=date(2027, 3, 4))
    closed(w, date(2027, 3, 5), date(2027, 3, 6))
    sat = approve(w, invoice(w, currency="USD", issue_date=date(2027, 3, 6)))
    assert (sat.tax_point_date, sat.sar_rate, sat.total_vat_sar) == (date(2027, 3, 6), D("3.7500"), D("562.50"))
    sunday = invoice(w, currency="USD", issue_date=date(2027, 3, 8), supply_date=date(2027, 3, 7))
    with pytest.raises(InvoicingError) as exc:
        approve(w, sunday)
    assert exc.value.code == "sama_rate_missing"
    assert "2027-03-07" in messages(exc) and "البنك المركزي" in messages(exc)
    stored = SalesDocument.objects.get(pk=sunday.pk)
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
    assert issued.tax_point_date == date(2027, 1, 10)  # the payment order date fills ZATCA BT-7 only (R117)


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
