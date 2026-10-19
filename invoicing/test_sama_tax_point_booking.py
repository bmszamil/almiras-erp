"""Owner decision H2 (invoicing R100(d) as amended, OQ-65; ledger-fx R51, OQ-20): a foreign-currency sales document
of a SAR entity books every line at the SAMA rate on its tax point, so 2200 equals ``total_vat_sar``, also when that
row is older than the ledger's 5-day rate age limit. Anonymised entities and made-up rates (R70)."""
from datetime import date, timedelta

import pytest
from django.utils import timezone

from invoicing import services
from invoicing.conftest import D, approve, invoice, usd_rate
from invoicing.models import SalesDocument
from invoicing.test_money import je, receipt
from ledger import documents
from ledger.models import Account, JournalLine, NonPublicationDay

pytestmark = pytest.mark.django_db


def _issued(w, *, supply, issue, lines=(("rent_commercial", "1", "1000.00"),)):
    return approve(w, invoice(w, lines, currency="USD", issue_date=issue, supply_date=supply))


def _lines(doc):
    return list(JournalLine.objects.filter(entry=doc.journal_entry).select_related("account").order_by("pk"))


def test_at151_tax_point_three_weeks_before_issue_books_every_line_at_the_sama_tax_point_row(w):
    on_supply = usd_rate(w, "3.7500", on=date(2027, 3, 10))
    usd_rate(w, "3.7510", on=date(2027, 3, 31))
    doc = _issued(w, supply=date(2027, 3, 10), issue=date(2027, 3, 31),
                  lines=(("rent_commercial", "1", "1000.00"), ("rent_commercial", "3", "333.33")))
    assert (doc.fx_rate, doc.fx_exchange_rate_id, doc.fx_rate_source, doc.fx_rate_date) == (
        D("3.7500"), on_supply.pk, "sama_tp", date(2027, 3, 10))
    assert doc.vat_sar_exchange_rate_id == on_supply.pk
    posted = w.zatca.clear(doc)
    assert posted.status == "final" and posted.journal_entry.date == date(2027, 3, 31)
    for line in _lines(posted):
        assert (line.currency_id, line.rate_source, line.fx_rate, line.exchange_rate_id) == (
            "USD", "sama_tax_point", D("3.7500"), on_supply.pk)
    vat = sum((l.credit for l in _lines(posted) if l.account.code == "2200"), D("0"))
    assert vat == posted.total_vat_sar == D("1125.00")  # USD 300.00 VAT on 1,999.99 net, x 3.75


def test_a_tax_point_within_five_days_still_books_as_a_table_rate(w):
    row = usd_rate(w, "3.7500", on=date(2027, 3, 26))
    usd_rate(w, "3.7510", on=date(2027, 3, 31))
    doc = _issued(w, supply=date(2027, 3, 26), issue=date(2027, 3, 31))
    assert (doc.fx_rate_source, doc.fx_exchange_rate_id) == ("sama", row.pk)
    posted = w.zatca.clear(doc)
    assert {l.rate_source for l in _lines(posted)} == {"table"}
    assert JournalLine.objects.get(entry=posted.journal_entry, account__code="2200").credit == D("562.50")


def test_a_long_holiday_on_the_issue_date_books_at_the_last_sama_row_before_it(w):
    """Tax point = issue date; SAMA published nothing for 8 days (I2: proposed, unverified days or none at all)."""
    row = usd_rate(w, "3.7500", on=date(2027, 3, 3))
    for offset in range(1, 5):
        NonPublicationDay.objects.create(date=date(2027, 3, 3) + timedelta(days=offset), name_en="Holiday",
                                         name_ar="عطلة", created_by=w.cfo)
    doc = _issued(w, supply=date(2027, 3, 11), issue=date(2027, 3, 11))
    assert (doc.tax_point_date, doc.fx_rate_source, doc.fx_exchange_rate_id) == (date(2027, 3, 11), "sama_tp", row.pk)
    posted = w.zatca.clear(doc)
    assert JournalLine.objects.get(entry=posted.journal_entry, account__code="2200").credit == posted.total_vat_sar


def test_no_issue_date_table_rate_is_needed_any_more(w):
    usd_rate(w, "3.7500", on=date(2027, 3, 10))  # the only rate row: 21 days before the issue date
    doc = _issued(w, supply=date(2027, 3, 10), issue=date(2027, 3, 31))
    assert doc.status == "issued" and doc.fx_rate == D("3.7500")


def test_a_receipt_settles_the_receivable_at_its_sama_tax_point_carrying_rate(w):
    usd_rate(w, "3.7500", on=date(2027, 3, 10))
    doc = w.zatca.clear(_issued(w, supply=date(2027, 3, 10), issue=date(2027, 3, 31)))  # USD 1,150 = SAR 4,312.50
    usd_rate(w, "3.8000", on=date(2027, 4, 10))
    r = receipt(w, "1150.00", currency="USD", date=date(2027, 4, 10), applications=[(doc, "1150.00")])
    posted = services.post_receipt(r, user=w.clerk2)
    assert je(posted) == [("1100", D("4370.00"), D("0"), "normal"), ("1200", D("0"), D("4312.50"), "settlement"),
                          ("4700", D("0"), D("57.50"), "realised_fx")]


def _registration_with(hook):
    current = documents.get("invoicing.salesdocument")
    options = {name: getattr(current, name) for name in (
        "build_lines", "validate_lines", "pending_documents", "creator_may_post", "contract_approval",
        "reverse_guard", "entity_field", "changed_at_field", "creator_field")}
    return documents._registered_for_tests(
        SalesDocument, post_permissions=current.post_permissions, reverse_permissions=current.reverse_permissions,
        sama_tax_point=hook, **options)


@pytest.mark.parametrize("hook, why", [
    (lambda doc: None, "no tax point"),
    (lambda doc: date(2027, 4, 1), "a tax point after the entry date"),
    (lambda doc: date(2027, 3, 12), "a later SAMA row between the row and the tax point (I2: not the latest)"),
    (lambda doc: (date(2027, 3, 12), None), "the same, as (tax point, no time)"),
    (lambda doc: date(2027, 3, 9), "a tax point before the row"),
])
def test_the_ledger_checks_the_sama_row_against_the_documents_own_tax_point(w, hook, why):
    usd_rate(w, "3.7500", on=date(2027, 3, 10))
    doc = _issued(w, supply=date(2027, 3, 10), issue=date(2027, 3, 31))
    usd_rate(w, "3.7520", on=date(2027, 3, 11))  # recorded after the issue: only a tampered hook gives no time
    with _registration_with(hook):
        with pytest.raises(Exception) as exc:
            w.zatca.clear(doc)
    text = " ".join(getattr(exc.value, "messages", [str(exc.value)]))
    assert "SAMA" in text or "tax point" in text, why
    assert "البنك المركزي" in text or "استحقاق" in text
    stored = SalesDocument.objects.get(pk=doc.pk)
    assert stored.journal_entry_id is None and stored.status == "issued"


def test_lines_rated_otherwise_than_approved_are_refused(w):
    usd_rate(w, "3.7500", on=date(2027, 3, 10))
    doc = _issued(w, supply=date(2027, 3, 10), issue=date(2027, 3, 31))
    stored = SalesDocument.objects.get(pk=doc.pk)
    line = JournalLine(account=Account.objects.get(entity=w.alm, code="1200"), currency_id="USD", rate_source="table",
                       fx_rate=stored.fx_rate, exchange_rate_id=stored.fx_exchange_rate_id, line_kind="normal")
    problems = services._check_document_rate(stored, [line])
    assert problems and "SAMA" in problems[0] and "البنك المركزي" in problems[0]
    tax_point, issued_at = services._sama_tax_point(stored)
    assert tax_point == date(2027, 3, 10) and issued_at.date() == date(2027, 3, 31) and issued_at.tzinfo is not None
    stored.fx_rate_source = "sama"
    assert services._sama_tax_point(stored) is None


def test_the_ledger_refuses_a_sama_tax_point_line_with_another_rate_or_kind(w):
    from ledger import services as ledger

    usd_rate(w, "3.7500", on=date(2027, 3, 10))
    posted = w.zatca.clear(_issued(w, supply=date(2027, 3, 10), issue=date(2027, 3, 31)))
    entry = posted.journal_entry
    line = _lines(posted)[0]
    assert ledger._sama_tax_point_problems(entry, line, "USD", "SAR", "L") == []
    line.fx_rate = D("3.7600")  # in memory only: posted lines never change
    assert "3.7600" in ledger._sama_tax_point_problems(entry, line, "USD", "SAR", "L")[0]
    line.fx_rate, line.line_kind = D("3.7500"), "settlement"
    assert "normal" in ledger._sama_tax_point_problems(entry, line, "USD", "SAR", "L")[0]
    line.line_kind, line.exchange_rate_id = "normal", None
    assert "rate row" in ledger._sama_tax_point_problems(entry, line, "USD", "SAR", "L")[0]


def test_i2_a_sama_row_entered_after_the_issue_for_a_day_in_the_gap_does_not_undo_the_document(w):
    """The document chose the latest SAMA row on or before its tax point when it was issued (I2); a SAMA row for a
    day in the gap recorded afterwards (before the ZATCA clearance posts it) does not make the posting fail."""
    from datetime import datetime
    from datetime import timezone as dt_tz
    from unittest import mock

    from django.utils import timezone as tz

    row = usd_rate(w, "3.7500", on=date(2027, 3, 10))
    doc = _issued(w, supply=date(2027, 3, 10), issue=date(2027, 3, 31))
    assert (doc.fx_rate_source, doc.fx_exchange_rate_id) == ("sama_tp", row.pk)
    later = datetime(2027, 4, 1, 6, 0, tzinfo=dt_tz.utc)  # 09:00 in Riyadh
    with mock.patch.object(tz, "now", return_value=later):  # entered the day after the issue
        backfilled = usd_rate(w, "3.7520", on=date(2027, 3, 11))
    assert backfilled.created_at == later
    posted = w.zatca.clear(doc)
    assert posted.status == "final"
    assert {l.exchange_rate_id for l in _lines(posted)} == {row.pk}
    assert JournalLine.objects.get(entry=posted.journal_entry, account__code="2200").credit == posted.total_vat_sar
