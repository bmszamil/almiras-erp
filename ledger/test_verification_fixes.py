"""Regression tests for the verification review (round 5), ledger side: invoicing R89 (AT-91) and R90 (AT-92)."""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from core import access
from core.access.models import GrantUse
from ledger import documents, services
from ledger.models import ExchangeRate, JournalEntry, JournalLine, LedgerError
from ledger.test_posting_hardening import CASH, doc, w  # noqa: F401
from testdocs.models import Receipt, Unregistered

pytestmark = pytest.mark.django_db
D = Decimal
REPO = Path(__file__).resolve().parent.parent


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


# --- R90 / AT-92: the settlement helper is not a public service function ------------------------------

def _posted_cash_line(w, entity=None):
    entity = entity or w.alm
    receipt = doc(Receipt, entity, created_by=w.cfo)
    grant = access.require(w.clerk, entity, "invoicing.post_receipt", document=receipt)
    entry = services.create_entry(entity, date(2027, 1, 5), "Receipt", CASH, user=w.clerk, post=True, grant=grant,
                                  document=receipt)
    return JournalLine.objects.get(entry=entry, account__code="1100")


def test_r90_the_helper_is_not_public():
    """Reviewer probe p6c called ledger.services.settlement_for_document directly, with no rights."""
    assert not hasattr(services, "settlement_for_document")
    from core.test_access_scan import unchecked_functions

    _, public = unchecked_functions()
    assert not [name for name in public if "settlement_for_document" in name]


def test_r90_only_the_posting_services_name_the_helper():
    """No view, API, admin or agent code reaches it: only the ledger and the invoicing and banking posting paths
    (banking R33: a bank transaction's build_lines settles intercompany items)."""
    users = sorted(
        path.relative_to(REPO).as_posix() for path in REPO.glob("*/**/*.py")
        if "_settlement_for_document" in path.read_text(encoding="utf-8")
        and not path.name.startswith("test_") and "migrations" not in path.parts
    )
    assert users == ["banking/services.py", "invoicing/services.py", "ledger/services.py"]


def test_r90_the_helper_is_refused_outside_a_documents_build_lines(w):
    """It takes no user: whoever calls it (a view, an API, an agent's code) outside a posting is refused."""
    item = _posted_cash_line(w)
    with pytest.raises(LedgerError) as exc:
        services._settlement_for_document(item, "10")
    assert exc.value.code == "settlement_outside_posting"
    assert "تُسعَّر" in " ".join(exc.value.messages)


def test_r90_the_helper_refuses_an_item_of_another_entity_inside_build_lines(w):
    other_item = _posted_cash_line(w, w.sub)
    built = []

    def build(document):
        built.append(services._settlement_for_document(other_item, "10"))
        return CASH

    with documents._registered_for_tests(Unregistered, post_permissions={"invoicing.post_receipt"},
                                         build_lines=build):
        item = Unregistered.objects.create(entity=w.alm, created_by=w.cfo)
        grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=item)
        with pytest.raises(LedgerError) as exc:
            services.create_entry(w.alm, date(2027, 1, 5), "x", None, user=w.clerk, post=True, grant=grant,
                                  document=item)
    assert exc.value.code == "settlement_other_entity" and not built
    assert not JournalEntry.objects.filter(entity=w.alm).exists()


def test_r90_the_helper_prices_a_settlement_inside_build_lines(w):
    item = _posted_cash_line(w)
    seen = []

    def build(document):
        seen.append(services._settlement_for_document(item, "4"))
        return CASH

    with documents._registered_for_tests(Unregistered, post_permissions={"invoicing.post_receipt"},
                                         build_lines=build):
        target = Unregistered.objects.create(entity=w.alm, created_by=w.cfo)
        grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=target)
        services.create_entry(w.alm, date(2027, 1, 5), "x", None, user=w.clerk, post=True, grant=grant,
                              document=target)
    (spec, settled), = seen
    assert spec["settles_line"] == item and spec["txn_credit"] == D("4") and settled == D("4.00")
    with pytest.raises(LedgerError):  # the context ends with build_lines
        services._settlement_for_document(item, "4")
