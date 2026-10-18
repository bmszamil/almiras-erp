"""Regression tests for the code review and compliance findings on the VAT slice (tax-returns.md §10.4).

Each test names the finding it covers ("item n" = code review, "Cn" = compliance check). The reviewer's probes
(scratch ``test_review_probes.py`` / ``test_review_pg_probes.py``) are ported here with the corrected outcome.
"""
from datetime import date

import pytest

from core import access
from ledger import services as ledger
from ledger.models import JournalEntry
from tax import services
from tax.conftest import D, approved, bill, close_q1, filed, sale
from tax.models import VatReturn

pytestmark = pytest.mark.django_db
Q1, Q2 = date(2027, 1, 1), date(2027, 4, 1)


# --- item 5: registered tax documents are reversed only through tax.services ---------------------------------

def _paid_q1(t):
    sale(t)
    close_q1(t)
    ret = filed(t, approved(t))
    pay = services.create_vat_payment(ret, user=t.a1, amount_sar="1500.00", payment_date=date(2027, 4, 25),
                                      bank_account="1100")
    services.submit_tax_document(pay, user=t.a1)
    return ret, services.approve_tax_document(pay, user=t.c1)


def test_item5_generic_ledger_reversal_of_a_tax_payment_is_refused(t):
    ret, pay = _paid_q1(t)
    entry = pay.journal_entry
    with pytest.raises(access.AccessDenied) as exc:
        ledger.reverse_entry(entry, access.require(t.a1, t.alm, "ledger.reverse_entry", document=entry), reason="x")
    assert exc.value.code == "document_reversal_only"
    with pytest.raises(access.AccessDenied) as exc:  # its own permission, but outside reverse_tax_document
        ledger.reverse_entry(entry, access.require(t.a1, t.alm, "tax.reverse_document", document=pay), reason="x")
    assert exc.value.code == "reverse_through_owner"
    assert JournalEntry.objects.get(pk=entry.pk).status == "posted"
    assert VatReturn.objects.get(pk=ret.pk).status == "settled"
    services.reverse_tax_document(pay, user=t.a1, reason="Paid from the wrong account")
    assert VatReturn.objects.get(pk=ret.pk).status == "filed" and services.open_amount(ret, user=t.a1) == D("1500.00")


def test_item5_t1_offset_is_never_reversed(t):
    sale(t)
    bill(t, [("5300", "1", "2000.00")])
    close_q1(t)
    ret = approved(t)
    entry = ret.settlement_entry
    with pytest.raises(access.AccessDenied) as exc:
        ledger.reverse_entry(entry, access.require(t.a1, t.alm, "ledger.reverse_entry", document=entry), reason="x")
    assert exc.value.code == "document_reversal_only"
    with pytest.raises(access.AccessDenied) as exc:
        ledger.reverse_entry(entry, access.require(t.a1, t.alm, "tax.reverse_document", document=ret), reason="x")
    assert exc.value.code == "grant_wrong_permission"
    assert JournalEntry.objects.get(pk=entry.pk).status == "posted"


def test_item5_invoicing_documents_still_reverse_through_invoicing(t):
    from invoicing import services as invoicing

    doc = bill(t, [("5300", "1", "1000.00")], on=date(2027, 3, 10))
    with pytest.raises(access.AccessDenied) as exc:
        ledger.reverse_entry(doc.journal_entry, access.require(t.a1, t.alm, "ledger.reverse_entry",
                                                               document=doc.journal_entry))
    assert exc.value.code == "document_reversal_only"
    done = invoicing.reverse_document(doc, user=t.a1, date=date(2027, 3, 20), reason="wrong vendor")
    assert done.status == "reversed"
