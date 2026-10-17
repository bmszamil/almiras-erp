"""SWIFT MT940 customer statement (public field structure) **[CC-verify: tag details per bank, OQ-1]**.

Read: ``:20:`` statement reference (statement id), ``:25:`` account, ``:28C:`` statement number[/page] (sequence),
``:60F:``/``:60M:`` opening balance, ``:61:`` statement lines with their ``:86:`` information, ``:62F:``/``:62M:``
closing balance. ``:64:``, ``:65:`` and statement-level ``:86:`` are ignored. One statement per file.

``:61:`` = value date YYMMDD, optional entry (booking) date MMDD, mark C / D / RC / RD (RC, a reversal of a credit,
is money out; RD money in), optional funds code, amount with a decimal comma, transaction type (N/F/S + 3),
customer reference (``NONREF`` = none), optional ``//`` bank reference, optional supplementary line.
"""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from . import ParsedLine, ParsedStatement, decode_text, parse_error

_TAG = re.compile(r"^:(\d{2}[A-Z]?):(.*)$")
_BALANCE = re.compile(r"^(?P<mark>[CD])(?P<date>\d{6})(?P<ccy>[A-Z]{3})(?P<amount>\d{1,15},\d*)$")
_LINE = re.compile(
    r"^(?P<value>\d{6})(?P<entry>\d{4})?(?P<mark>RC|RD|C|D)(?P<funds>[A-Z])?(?P<amount>\d{1,15},\d*)"
    r"(?P<type>[NFS][A-Z0-9]{3})(?P<rest>.*)$"
)
_EREF = re.compile(r"/EREF/([^/\n]{1,35})")


def _amount(text, where):
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        raise parse_error(f"{where}: {text!r} is not an amount.", f"{where}: {text!r} ليس مبلغًا.") from None


def _yymmdd(text, where):
    try:
        return date(2000 + int(text[:2]), int(text[2:4]), int(text[4:6]))
    except ValueError:
        raise parse_error(f"{where}: {text!r} is not a date (YYMMDD).", f"{where}: {text!r} ليس تاريخًا (YYMMDD).") from None


def _entry_date(value_date, mmdd, where):
    month, day = int(mmdd[:2]), int(mmdd[2:])
    year = value_date.year
    if month == 1 and value_date.month == 12:
        year += 1
    elif month == 12 and value_date.month == 1:
        year -= 1
    try:
        return date(year, month, day)
    except ValueError:
        raise parse_error(f"{where}: {mmdd!r} is not a booking date (MMDD).", f"{where}: {mmdd!r} ليس تاريخ قيد.") from None


def _balance(value, tag):
    m = _BALANCE.match(value.strip())
    if not m:
        raise parse_error(f"Field :{tag}: is not a balance (mark, YYMMDD, currency, amount).",
                          f"الحقل :{tag}: ليس رصيدًا صحيحًا.")
    amount = _amount(m["amount"], f":{tag}:")
    return (-amount if m["mark"] == "D" else amount), _yymmdd(m["date"], f":{tag}:"), m["ccy"]


def _fields(text):
    """[(tag, value)] of block 4 (or the whole text), continuation lines joined with newlines."""
    start = text.find("{4:")
    if start >= 0:
        text = text[start + 3:]
        end = text.find("\n-}")
        if end < 0:
            end = text.find("-}")
        text = text[:end] if end >= 0 else text
    fields = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if not line or line == "-":
            continue
        m = _TAG.match(line)
        if m:
            fields.append([m[1], m[2]])
        elif fields:
            fields[-1][1] += "\n" + line
    return fields


def parse(data):
    text = decode_text(data)
    fields = _fields(text)
    tags = [t for t, _ in fields]
    if text.count("{4:") > 1:
        raise parse_error("The file holds more than one MT940 message; import one statement per file.",
                          "يحتوي الملف على أكثر من رسالة MT940؛ استورد كشفًا واحدًا لكل ملف.")
    if "20" not in tags or not any(t in tags for t in ("60F", "60M")) or not any(t in tags for t in ("62F", "62M")):
        raise parse_error("Not an MT940 statement: fields :20:, :60F: and :62F: are required.",
                          "ليس كشف MT940: الحقول :20: و:60F: و:62F: مطلوبة.")
    if tags.count("20") > 1:
        raise parse_error("The file holds more than one MT940 statement; import one statement per file.",
                          "يحتوي الملف على أكثر من كشف MT940؛ استورد كشفًا واحدًا لكل ملف.")
    statement = ParsedStatement(statement_id="", sequence_no=None, period_start=None, period_end=None, currency="",
                                opening_balance=None, closing_balance=None)
    current = None
    for tag, value in fields:
        if tag == "20":
            statement.statement_id = value.strip()[:35]
        elif tag == "25":
            statement.account_identifier = value.strip()
        elif tag == "28C":
            number = value.strip().split("/")[0]
            if not number.isdigit():
                raise parse_error(f":28C: {value!r} is not a statement number.", f":28C: {value!r} ليس رقم كشف.")
            statement.sequence_no = int(number)
        elif tag in ("60F", "60M"):
            statement.opening_balance, statement.period_start, statement.currency = _balance(value, tag)
        elif tag == "61":
            current = _line(value, len(statement.lines) + 1, statement.currency)
            statement.lines.append(current)
        elif tag == "86":
            if current is not None:  # the information of the :61: line just read; later :86: are statement-level
                narrative = " ".join(part.strip() for part in value.split("\n") if part.strip())
                current.narrative = f"{current.narrative} {narrative}".strip()
                eref = _EREF.search(value)
                if eref and not current.end_to_end_id:
                    current.end_to_end_id = eref[1].strip()
                current = None
        elif tag in ("62F", "62M"):
            closing, statement.period_end, ccy = _balance(value, tag)
            if ccy != statement.currency:
                raise parse_error(f"Opening balance in {statement.currency}, closing balance in {ccy}.",
                                  f"الرصيد الافتتاحي بعملة {statement.currency} والختامي بعملة {ccy}.")
            statement.closing_balance = closing
            current = None
    return statement


def _line(value, line_no, currency):
    first, _, supplementary = value.partition("\n")
    m = _LINE.match(first.strip())
    where = f":61: line {line_no}"
    if not m:
        raise parse_error(f"{where} is not a statement line.", f"{where} ليس سطر كشف صحيحًا.")
    value_date = _yymmdd(m["value"], where)
    booking = _entry_date(value_date, m["entry"], where) if m["entry"] else value_date
    mark = m["mark"]
    direction = "credit" if mark in ("C", "RD") else "debit"
    customer_ref, _, bank_ref = m["rest"].partition("//")
    customer_ref = customer_ref.strip()
    if customer_ref.upper() == "NONREF":
        customer_ref = ""
    return ParsedLine(
        line_no=line_no, booking_date=booking, value_date=value_date, direction=direction,
        amount=_amount(m["amount"], where), currency=currency, transaction_code=m["type"],
        customer_reference=customer_ref[:35], bank_reference=bank_ref.strip()[:35],
        narrative=" ".join(supplementary.split()),
    )
