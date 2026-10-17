"""Bank CSV exports read through a configurable :class:`banking.models.CsvLayout` (R10; layouts per bank, OQ-1).

The layout names the columns (see ``CsvLayout``). Amounts are read as text into ``Decimal`` (never float):
thousands separators removed, the layout's decimal separator, a leading or trailing minus or parentheses for
negative amounts. Direction comes from a direction column (``credit_values`` / ``debit_values``), from separate
debit and credit columns, or from the sign of a single amount column (negative = money out).

CSV exports often have no statement header: statement id, period and balances may be missing. A running
``balance`` column lets the opening and closing balances be derived; otherwise the importer enters them (OQ-24).
"""
import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

from . import ParsedLine, ParsedStatement, decode_text, parse_error

TEXT_FIELDS = ("bank_reference", "end_to_end_id", "customer_reference", "transaction_code", "counterparty_name",
               "counterparty_iban", "narrative")
LIMITS = {"bank_reference": 35, "end_to_end_id": 35, "customer_reference": 35, "transaction_code": 10,
          "counterparty_name": 140, "counterparty_iban": 34}


def _amount(text, layout, where):
    raw = (text or "").strip()
    if not raw:
        return None
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative, raw = True, raw[1:-1]
    if raw.endswith("-"):
        negative, raw = True, raw[:-1]
    if raw.startswith("-"):
        negative, raw = not negative, raw[1:]
    if layout.thousands_separator:
        raw = raw.replace(layout.thousands_separator, "")
    if layout.decimal_separator != ".":
        raw = raw.replace(layout.decimal_separator, ".")
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        raise parse_error(f"{where}: {text!r} is not an amount.", f"{where}: {text!r} ليس مبلغًا.") from None
    return -value if negative else value


def _date(text, layout, where):
    try:
        return datetime.strptime((text or "").strip(), layout.date_format).date()
    except ValueError:
        raise parse_error(f"{where}: {text!r} is not a date ({layout.date_format}).",
                          f"{where}: {text!r} ليس تاريخًا ({layout.date_format}).") from None


def parse(data, layout):
    text = decode_text(data, (layout.encoding, "utf-8-sig", "cp1256"))
    rows = list(csv.reader(io.StringIO(text), delimiter=layout.delimiter))[layout.skip_rows:]
    if not rows:
        raise parse_error("The CSV file is empty.", "ملف CSV فارغ.")
    header = [h.strip() for h in rows[0]]
    cols = layout.columns or {}
    needed = ["booking_date"] + (["amount"] if "amount" in cols else ["debit", "credit"])
    missing = [cols.get(n, n) for n in needed if cols.get(n) not in header]
    if missing:
        raise parse_error(f"Columns missing in the CSV file: {', '.join(missing)}.",
                          f"أعمدة ناقصة في ملف CSV: {', '.join(missing)}.")
    index = {name: header.index(col) for name, col in cols.items() if col in header}

    def cell(row, name):
        i = index.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    statement = ParsedStatement(statement_id="", sequence_no=None, period_start=None, period_end=None,
                                currency="", opening_balance=None, closing_balance=None)
    balances = []
    for n, row in enumerate(rows[1:], start=1):
        if not any(c.strip() for c in row):
            continue
        where = f"CSV row {n + layout.skip_rows + 1}"
        line_no = len(statement.lines) + 1
        if "amount" in index:
            signed = _amount(cell(row, "amount"), layout, where)
        else:
            debit, credit = _amount(cell(row, "debit"), layout, where), _amount(cell(row, "credit"), layout, where)
            if debit and credit:
                raise parse_error(f"{where}: both debit and credit are filled.", f"{where}: الخانتان المدينة والدائنة معبأتان.")
            signed = credit if credit else (-abs(debit) if debit else None)
        if signed is None or signed == 0:
            raise parse_error(f"{where}: no amount.", f"{where}: لا يوجد مبلغ.")
        direction = "credit" if signed > 0 else "debit"
        if "direction" in index:
            flag = cell(row, "direction").upper()
            if flag in [v.upper() for v in layout.credit_values]:
                direction = "credit"
            elif flag in [v.upper() for v in layout.debit_values]:
                direction = "debit"
            else:
                raise parse_error(f"{where}: direction {flag!r} is not in the layout.",
                                  f"{where}: الاتجاه {flag!r} غير معرّف في التنسيق.")
        line = ParsedLine(line_no=line_no, booking_date=_date(cell(row, "booking_date"), layout, where),
                          direction=direction, amount=abs(signed), currency=cell(row, "currency").upper())
        if cell(row, "value_date"):
            line.value_date = _date(cell(row, "value_date"), layout, where)
        for name in TEXT_FIELDS:
            value = cell(row, name)
            limit = LIMITS.get(name)
            if limit and len(value) > limit:
                raise parse_error(f"{where}: {name} is longer than {limit} characters.",
                                  f"{where}: الحقل {name} أطول من {limit} حرفًا.")
            setattr(line, name, value)
        statement.lines.append(line)
        if "balance" in index and cell(row, "balance"):
            balances.append((line, _amount(cell(row, "balance"), layout, where)))
    if not statement.lines:
        raise parse_error("The CSV file has no statement lines.", "لا يحتوي ملف CSV على أسطر.")
    statement.currency = statement.lines[0].currency
    if balances and len(balances) == len(statement.lines):
        first_line, first_balance = balances[0]
        statement.opening_balance = first_balance - first_line.amount if first_line.direction == "credit" \
            else first_balance + first_line.amount
        statement.closing_balance = balances[-1][1]
    dates = [l.booking_date for l in statement.lines]
    statement.period_start, statement.period_end = min(dates), max(dates)
    if not layout.sample_verified:
        statement.warnings.append(f"CSV layout {layout.code} is not yet checked against a bank sample (OQ-1). / "
                                  f"لم يُتحقق بعد من تنسيق CSV {layout.code} مقابل عينة من البنك.")
    return statement
