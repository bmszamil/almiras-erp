"""Statement import (banking R9 to R15; AT-3 to AT-10) and the parsers (R10). Synthetic files only."""
from datetime import date
from decimal import Decimal as D

import pytest
from django.contrib.admin.sites import site
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory

from banking import services
from banking.conftest import IBANS, journal, open_account, postgres_only
from banking.models import BankingError, BankingEvent, Match, StatementImport, StatementLine
from banking.parsers import ParseError, camt053, csv_layout, mt940 as mt940_parser
from banking.testing import AT3_LINES, CSV_COLUMNS, camt053 as camt_file, csv_export, mt940
from ledger.services_setup import install_bank_sub_account

JAN1, JAN31 = date(2027, 1, 1), date(2027, 1, 31)


def at3_file(**overrides):
    fields = dict(statement_id="RIYAD-2027-01", sequence=1, currency="SAR", opening="100000.00", opening_date=JAN1,
                  closing_date=JAN31, lines=AT3_LINES, account=IBANS["RIYAD-SAR"])
    fields.update(overrides)
    return mt940(**fields)


def refused(account, data, user, **kwargs):
    before = (StatementImport.objects.count(), StatementLine.objects.count())
    with pytest.raises(BankingError) as exc:
        services.import_statement(account, data, user=user, **kwargs)
    assert (StatementImport.objects.count(), StatementLine.objects.count()) == before  # nothing stored
    assert BankingEvent.objects.filter(event="import_refused", object_id=account.pk).exists()
    text = " ".join(exc.value.messages)
    assert any("؀" <= ch <= "ۿ" for ch in text)  # bilingual
    return exc.value, text


def test_at3_mt940_import_stores_header_lines_and_runs_matching(w, accounts):
    payment = journal(w, w.alm, date(2027, 1, 7), [{"account": "5300", "debit": D("4000.00")},
                                                   {"account": "1101", "credit": D("4000.00")}])
    imp = services.import_statement(accounts.riyad, at3_file(), user=w.a, file_name="riyad-jan.mt940")
    assert (imp.statement_id, imp.sequence_no, imp.period_start, imp.period_end) == ("RIYAD-2027-01", 1, JAN1, JAN31)
    assert (imp.opening_balance, imp.closing_balance, imp.line_count) == (D("100000.00"), D("112450.00"), 6)
    assert (imp.total_credits, imp.total_debits) == (D("26575.00"), D("14125.00"))
    assert len(imp.file_sha256) == 64 and imp.source == "human"
    lines = list(imp.lines.order_by("line_no"))
    assert [l.amount for l in lines] == [D(x[2]) for x in AT3_LINES]
    assert lines[3].customer_reference == "TRX88" and lines[3].bank_reference == "BR0004"
    assert lines[2].narrative == "CHARGES" and lines[2].transaction_code == "NCHG"
    assert BankingEvent.objects.filter(event="import_accepted", object_id=imp.pk, actor=w.a).count() == 1
    # R18: matching ran after the import (the 4,000.00 payment is proposed for the 01-07 debit, M2).
    match = Match.objects.get(bank_account=accounts.riyad)
    assert match.rule == "M2" and match.status == "proposed"
    assert {p.journal_line_id for p in match.parts.all() if p.journal_line_id} == {payment.lines.get(account__code="1101").pk}


def test_at4_reimport_is_idempotent(w, accounts):
    first = services.import_statement(accounts.riyad, at3_file(), user=w.a)
    counts = (StatementImport.objects.count(), StatementLine.objects.count(), Match.objects.count())
    events = BankingEvent.objects.exclude(event="import_duplicate_file").count()
    assert services.import_statement(accounts.riyad, at3_file(), user=w.a) == first
    assert services.import_statement(accounts.riyad, at3_file(), user=w.t) == first  # group treasury, same file
    assert (StatementImport.objects.count(), StatementLine.objects.count(), Match.objects.count()) == counts
    assert BankingEvent.objects.exclude(event="import_duplicate_file").count() == events
    assert BankingEvent.objects.filter(event="import_duplicate_file", object_id=first.pk).count() == 2


def test_at5_same_statement_id_with_a_changed_line_is_refused(w, accounts):
    services.import_statement(accounts.riyad, at3_file(), user=w.a)
    changed = list(AT3_LINES)
    changed[5] = (*changed[5][:2], "150.00", *changed[5][3:])
    exc, text = refused(accounts.riyad, at3_file(lines=changed), w.a)
    assert exc.code == "statement_replaced" and "already imported" in text
    event = BankingEvent.objects.filter(event="import_refused").latest("pk")
    assert event.details["reason"] == "statement_replaced"


def test_at6_discontinuity_inconsistency_and_currency_are_refused(w, accounts):
    services.import_statement(accounts.riyad, at3_file(), user=w.a)
    feb = dict(statement_id="RIYAD-2027-02", sequence=2, opening_date=date(2027, 2, 1), closing_date=date(2027, 2, 28),
               lines=[(date(2027, 2, 3), "D", "50.00", "NCHG", "", "BR0100", "CHARGES")])
    exc, text = refused(accounts.riyad, at3_file(opening="112400.00", **feb), w.a)
    assert exc.code == "discontinuity" and "112400.00" in text and "112450.00" in text
    exc, text = refused(accounts.riyad, at3_file(opening="112450.00", closing="112450.00", **feb), w.a)
    assert exc.code == "statement_inconsistent" and "112400.00" in text  # 112450 - 50 = 112400, not 112450
    exc, text = refused(accounts.riyad, at3_file(opening="112450.00", currency="USD", **feb), w.a)
    assert exc.code == "currency" and "USD" in text
    assert services.import_statement(accounts.riyad, at3_file(opening="112450.00", **feb), user=w.a).line_count == 1


def test_at7_overlapping_and_out_of_order_statements_are_refused(w, accounts):
    half = [l for l in AT3_LINES if l[0].day <= 15]
    rest = [l for l in AT3_LINES if l[0].day > 15]
    first = services.import_statement(accounts.riyad, at3_file(statement_id="S-A", lines=half,
                                                               closing_date=date(2027, 1, 15)), user=w.a)
    services.import_statement(accounts.riyad, at3_file(statement_id="S-B", sequence=2, lines=rest,
                                                       opening=str(first.closing_balance),
                                                       opening_date=date(2027, 1, 16)), user=w.a)
    exc, text = refused(accounts.riyad, at3_file(statement_id="S-C", sequence=3, lines=[], opening="112450.00",
                                                 opening_date=date(2027, 1, 10), closing_date=date(2027, 1, 20)), w.a)
    assert exc.code == "overlap" and "overlaps" in text
    exc, text = refused(accounts.riyad, at3_file(statement_id="S-D", sequence=4, lines=[], opening="112450.00",
                                                 opening_date=JAN1, closing_date=date(2027, 1, 5)), w.a)
    assert exc.code == "order" and "import in order" in text


def test_r12_same_day_needs_a_higher_sequence(w, accounts):
    services.import_statement(accounts.riyad, at3_file(statement_id="D1", lines=[], closing_date=date(2027, 1, 2)),
                              user=w.a)
    nxt = dict(lines=[], opening_date=date(2027, 1, 2), closing_date=date(2027, 1, 3))
    exc, _ = refused(accounts.riyad, at3_file(statement_id="D2", sequence=1, **nxt), w.a)
    assert exc.code == "overlap"
    assert services.import_statement(accounts.riyad, at3_file(statement_id="D2", sequence=2, **nxt), user=w.a)


def csv_account(w, code="CSV-SAR", gl_code="1106", opening="100000.00"):
    layout = services.create_csv_layout(w.alm, user=w.p, code="test-csv", name_en="Test CSV", name_ar="ملف تجريبي",
                                        bank="riyad", columns=CSV_COLUMNS, date_format="%d/%m/%Y")
    gl = install_bank_sub_account(w.alm, gl_code, "CSV bank", "بنك CSV", "SAR")
    from banking.testing import make_iban
    return open_account(w, w.alm, code, "riyad", gl, "SAR", "conventional", fmt="csv", csv_layout=layout,
                        iban=make_iban("20", "000000000000155555"), opening=opening), layout


def test_at8_duplicate_lines_refused_then_accepted_with_reason(w, accounts):
    services.import_statement(accounts.riyad, at3_file(), user=w.a)
    layout = services.create_csv_layout(w.alm, user=w.p, code="riyad-csv", name_en="Riyad CSV", name_ar="ملف الرياض",
                                        bank="riyad", columns=CSV_COLUMNS, date_format="%d/%m/%Y")
    services.update_bank_account(accounts.riyad, user=w.a, csv_layout=layout)
    feb = [(date(2027, 2, 1), "C", "500.00", "NTRF", "", "BR0200", "Deposit"),
           (date(2027, 2, 2), "C", "700.00", "NTRF", "", "BR0201", "Deposit"),
           (date(2027, 2, 3), "D", "25.00", "NCHG", "", "BR0003", "CHARGES")]  # repeats line 3 of AT-3
    data = csv_export(feb)
    kwargs = dict(format="csv", opening_balance="112450.00", closing_balance="113625.00")
    exc, text = refused(accounts.riyad, data, w.a, **kwargs)
    assert exc.code == "duplicate_lines" and "Lines 3 " in text
    with pytest.raises(BankingError, match="reason"):
        services.import_statement(accounts.riyad, data, user=w.a, accept_duplicates=[3], **kwargs)
    imp = services.import_statement(accounts.riyad, data, user=w.a, accept_duplicates=[3],
                                    duplicate_reason="Bank charged the fee twice; claim refund", **kwargs)
    line3 = imp.lines.get(line_no=3)
    original = StatementLine.objects.get(bank_reference="BR0003", statement__statement_id="RIYAD-2027-01")
    assert line3.duplicate_of == original
    assert BankingEvent.objects.filter(event="duplicates_accepted", object_id=imp.pk).exists()
    same_day = [(date(2027, 2, 5), "D", "25.00", "NCHG", "", "", "CHARGES"),
                (date(2027, 2, 5), "D", "25.00", "NCHG", "", "", "CHARGES")]
    twice = services.import_statement(accounts.riyad, csv_export(same_day), user=w.a, format="csv",
                                      opening_balance="113625.00", closing_balance="113575.00",
                                      period_start=date(2027, 2, 4), period_end=date(2027, 2, 5))
    assert twice.line_count == 2  # identical lines inside one file are not duplicates


def test_at9_camt053_and_csv_parse_to_the_same_lines_and_pdf_is_refused(w, accounts):
    mt = services.import_statement(accounts.riyad, at3_file(), user=w.a)
    camt = services.import_statement(accounts.rajhi, camt_file(
        statement_id="RAJHI-2027-01", sequence=1, currency="SAR", opening="100000.00", opening_date=JAN1,
        closing_date=JAN31, lines=AT3_LINES, account=IBANS["RAJHI-SAR"]), user=w.a, format="camt053")

    def key(imp):
        return [(l.booking_date, l.direction, l.amount, l.bank_reference, l.narrative, l.transaction_code)
                for l in imp.lines.order_by("line_no")]

    assert key(camt) == key(mt) and camt.closing_balance == mt.closing_balance
    assert camt.lines.get(line_no=4).end_to_end_id == "TRX88"
    account, _ = csv_account(w)
    data = csv_export(AT3_LINES)
    exc, text = refused(account, data, w.a)
    assert exc.code == "balances_missing"
    imp = services.import_statement(account, data, user=w.a, opening_balance="100000.00", closing_balance="112450.00",
                                    period_start=JAN1, period_end=JAN31)
    assert [k[:4] for k in key(imp)] == [k[:4] for k in key(mt)]
    assert imp.warnings and "OQ-1" in imp.warnings[0]  # layout not yet checked against a bank sample
    exc, text = refused(account, b"%PDF-1.7\n...", w.a)
    assert exc.code == "unsupported_format" and "Unsupported format" in text


def test_csv_running_balance_derives_the_balances(w, accounts):
    account, layout = csv_account(w)
    layout.columns = {**CSV_COLUMNS, "balance": "Balance"}
    layout._service_write = True
    layout.save()
    account.refresh_from_db()
    imp = services.import_statement(account, csv_export(AT3_LINES, with_balance=True, opening="100000.00"), user=w.a,
                                    period_start=JAN1, period_end=JAN31)
    assert (imp.opening_balance, imp.closing_balance) == (D("100000.00"), D("112450.00"))


def test_r11_decimals_and_dates_outside_the_period(w, accounts):
    exc, text = refused(accounts.riyad, at3_file(lines=[(date(2027, 1, 3), "C", "10.005", "NTRF", "", "X1", "")]), w.a)
    assert exc.code == "statement_inconsistent" and "decimals" in text
    exc, text = refused(accounts.riyad, at3_file(lines=[(date(2027, 2, 3), "C", "10.00", "NTRF", "", "X1", "")]), w.a)
    assert "outside the statement period" in text


def test_r2_statement_for_another_account_is_refused(w, accounts):
    exc, text = refused(accounts.riyad, at3_file(account=IBANS["RAJHI-SAR"]), w.a)
    assert exc.code == "other_account" and IBANS["RAJHI-SAR"] not in text


# --- R15 / AT-10: immutable lines ------------------------------------------------------------------------------

def test_at10_statement_lines_are_immutable(w, accounts):
    imp = services.import_statement(accounts.riyad, at3_file(), user=w.a)
    line = imp.lines.get(line_no=1)
    line.amount = D("1.00")
    with pytest.raises(BankingError):
        line.save()
    with pytest.raises(BankingError):
        StatementLine.objects.filter(pk=line.pk).update(amount=D("1.00"))
    with pytest.raises(BankingError):
        line.delete()
    with pytest.raises(BankingError):
        StatementLine.objects.filter(pk=line.pk).delete()
    with pytest.raises(BankingError):
        imp.delete()
    with pytest.raises(BankingError):
        StatementImport.objects.filter(pk=imp.pk).update(closing_balance=D("0"))
    request = RequestFactory().get("/")
    request.user = w.a
    admin = site._registry[StatementLine]
    assert not admin.has_change_permission(request, line) and not admin.has_delete_permission(request, line)
    assert StatementLine.objects.get(pk=line.pk).amount == D("15000.00")


@postgres_only
def test_at10_raw_sql_cannot_change_or_delete_lines(w, accounts):
    imp = services.import_statement(accounts.riyad, at3_file(), user=w.a)
    line = imp.lines.get(line_no=1)
    for sql in ("UPDATE banking_statementline SET amount = 1 WHERE id = %s",
                "DELETE FROM banking_statementline WHERE id = %s",
                "UPDATE banking_statementimport SET closing_balance = 0 WHERE id = %s"):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [line.pk if "line" in sql else imp.pk])
    assert StatementLine.objects.get(pk=line.pk).amount == D("15000.00")


# --- parsers (R10) ----------------------------------------------------------------------------------------------

def test_mt940_reversal_marks_entry_dates_and_one_statement_per_file():
    data = mt940(statement_id="X", sequence=7, currency="SAR", opening="10.00", opening_date=date(2027, 12, 31),
                 closing_date=date(2028, 1, 2), lines=[])
    text = data.decode().replace(":62F:", ":61:2712310102RD5,00NTRFNONREF//B1\r\n:86:Reversal\r\n:62F:")
    parsed = mt940_parser.parse(text.encode())
    line = parsed.lines[0]
    assert (line.direction, line.amount, line.booking_date, line.value_date) == ("credit", D("5.00"), date(2028, 1, 2),
                                                                               date(2027, 12, 31))
    assert parsed.sequence_no == 7 and line.customer_reference == "" and line.narrative == "Reversal"
    with pytest.raises(ParseError, match="more than one"):
        mt940_parser.parse(data + b"\r\n" + data)
    with pytest.raises(ParseError):
        mt940_parser.parse(b"hello")


def test_camt053_refuses_doctype_and_leaves_out_pending_entries():
    pending = """<Ntry><Amt Ccy="SAR">9.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts>PDNG</Sts>
                 <BookgDt><Dt>2027-01-05</Dt></BookgDt></Ntry>"""
    data = camt_file(statement_id="C1", sequence=1, currency="SAR", opening="0.00", opening_date=JAN1,
                     closing_date=JAN31, lines=[], extra_entries=pending)
    parsed = camt053.parse(data)
    assert parsed.lines == [] and parsed.warnings
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Document/>'
    with pytest.raises(ParseError, match="DOCTYPE"):
        camt053.parse(evil)


def test_csv_debit_and_credit_columns():
    class Layout:
        code, encoding, delimiter, skip_rows, date_format = "x", "utf-8", ";", 1, "%Y-%m-%d"
        decimal_separator, thousands_separator, sample_verified = ",", ".", True
        credit_values, debit_values = [], []
        columns = {"booking_date": "Datum", "debit": "Soll", "credit": "Haben", "narrative": "Text"}

    data = "Export\nDatum;Soll;Haben;Text\n2027-01-03;;1.500,25;In\n2027-01-04;10,00;;Out\n".encode()
    parsed = csv_layout.parse(data, Layout)
    assert [(l.direction, l.amount) for l in parsed.lines] == [("credit", D("1500.25")), ("debit", D("10.00"))]
