"""Invoicing R97 (policy A, D32(a)) and AT-115, invoicing side: the ALM chart's control accounts take invoicing
documents (an approved bill on 2100) and refuse manual journals (Dr 1200 / Cr 4100), en/ar ``control_account``.
Dr 5900 / Cr 2310 of AT-115 is not testable: 2310 is not in the chart (R97 note 2026-10-21, OQ-81)."""
from datetime import date

import pytest

from invoicing import services
from invoicing.conftest import D
from invoicing.test_purchases import bill, je
from ledger import services as ledger
from ledger.coa_template import TEMPLATE
from ledger.models import JournalEntry, LedgerError

pytestmark = pytest.mark.django_db


def test_at115_invoicing_bill_on_2100_posts(w):
    posted = services.approve_purchase_document(bill(w), user=w.cfo)
    assert posted.status == "posted" and ("2100", D("0"), D("1150.00")) in je(posted)
    assert posted.journal_entry.document_type == "invoicing.purchasedocument"


def test_at115_manual_journal_dr_1200_cr_4100_refused_en_ar(w):
    with pytest.raises(LedgerError) as exc:
        ledger.create_entry(w.alm, date(2027, 1, 15), "Manual AR / قيد يدوي",
                            [{"account": "1200", "debit": D("100.00")}, {"account": "4100", "credit": D("100.00")}],
                            user=w.accountant)
    assert exc.value.code == "control_account"
    text = " ".join(exc.value.messages)
    assert "Account 1200 (Trade receivables) is a control account" in text
    assert "الحساب 1200 (الذمم المدينة التجارية) حساب مراقبة" in text
    assert not JournalEntry.objects.filter(entity=w.alm, description__startswith="Manual AR").exists()


def test_at115_2310_is_not_in_the_chart_yet():
    """OQ-81 team default: 2310 carries the control flag when R99 adds it; ledger.control_accounts then picks it up."""
    assert "2310" not in {code for code, *_ in TEMPLATE}
