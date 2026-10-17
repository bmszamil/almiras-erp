"""Regression tests for the verification review (round 5), ledger side: invoicing R89 (AT-91)."""
from datetime import date
from decimal import Decimal

import pytest

from core import access
from core.access.models import GrantUse
from ledger import documents, services
from ledger.models import ExchangeRate, JournalEntry, LedgerError
from ledger.test_posting_hardening import doc, w  # noqa: F401
from testdocs.models import Receipt, Unregistered

pytestmark = pytest.mark.django_db
D = Decimal


def _usd(w, rate, on):
    return ExchangeRate.objects.create(entity=w.alm, from_currency_id="USD", to_currency_id="SAR", date=on,
                                       rate=D(rate), source="manual", rate_type="spot", created_by=w.mp)


def _usd_lines(row=None):
    """USD 10 into cash against SAR 37.50 of equity: balances at 3.75."""
    first = {"account": "1100", "currency": "USD", "txn_debit": D("10"), "debit": D("37.50")}
    if row is not None:
        first["exchange_rate"] = row
    return [first, {"account": "3100", "credit": D("37.50")}]


# --- R89 / AT-91: a pinned rate row comes only from the document type's build_lines --------------------

def test_r89_caller_lines_of_a_validate_lines_type_never_carry_a_rate_row(w):
    """testdocs.Receipt registers validate_lines only: the caller passes the lines, so it may not pin a rate."""
    pinned = _usd(w, "3.75", date(2027, 1, 3))
    _usd(w, "3.80", date(2027, 1, 5))  # the rate table moved on: the pinned row would book at an older rate
    receipt = doc(Receipt, w.alm, created_by=w.cfo)
    receipt.amount = D("37.50")
    receipt.save()
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt)
    with pytest.raises(LedgerError) as exc:
        services.create_entry(w.alm, date(2027, 1, 5), "pinned by the caller", _usd_lines(pinned), user=w.clerk,
                              post=True, grant=grant, document=receipt)
    assert exc.value.code == "pinned_rate_not_allowed"
    text = " ".join(exc.value.messages)
    assert "build_lines" in text and "المستدعي" in text
    assert not JournalEntry.objects.exists() and not GrantUse.objects.filter(log_id=grant.log_id).exists()


def test_r89_even_the_current_rate_row_is_refused_from_the_caller(w):
    """Naming the row that is current for the date is refused too: the key itself is not for callers."""
    current = _usd(w, "3.75", date(2027, 1, 5))
    receipt = doc(Receipt, w.alm, created_by=w.cfo)
    receipt.amount = D("37.50")
    receipt.save()
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt)
    with pytest.raises(LedgerError) as exc:
        services.create_entry(w.alm, date(2027, 1, 5), "x", _usd_lines(current), user=w.clerk, post=True,
                              grant=grant, document=receipt)
    assert exc.value.code == "pinned_rate_not_allowed"
    # Without the key the ledger looks the rate up itself and the same posting goes through.
    entry = services.create_entry(w.alm, date(2027, 1, 5), "looked up", _usd_lines(), user=w.clerk, post=True,
                                  grant=grant, document=receipt)
    usd = entry.lines.get(currency="USD")
    assert (usd.fx_rate, usd.exchange_rate_id) == (D("3.75"), current.pk)


def test_r89_a_build_lines_type_may_book_at_its_pinned_row(w):
    pinned = _usd(w, "3.75", date(2027, 1, 3))
    _usd(w, "3.80", date(2027, 1, 5))

    def build(document):
        return _usd_lines(pinned)

    with documents._registered_for_tests(Unregistered, post_permissions={"invoicing.post_receipt"},
                                         build_lines=build):
        item = Unregistered.objects.create(entity=w.alm, created_by=w.cfo, amount=D("37.50"))
        grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=item)
        entry = services.create_entry(w.alm, date(2027, 1, 5), "Built", None, user=w.clerk, post=True, grant=grant,
                                      document=item)
    usd = entry.lines.get(currency="USD")
    assert (usd.fx_rate, usd.exchange_rate_id) == (D("3.75"), pinned.pk)
