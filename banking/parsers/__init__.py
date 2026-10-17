"""Statement parsers (banking R10). One module per format, standard library only (no paid or closed libraries).

Each parser takes the file bytes (and, for CSV, the layout) and returns a :class:`ParsedStatement` or raises
:class:`ParseError` with an "English / العربية" message. Parsers store nothing and check nothing about the
account's history: ``banking.services.import_statement`` runs R11 to R14 on the result.

The MT940 and camt.053 parsers follow the public structures of those standards (SWIFT MT940 field tags; ISO 20022
``BkToCstmrStmt``) and are tested on synthetic fixtures. No bank-specific variant is marked supported until a
bank's anonymised sample passes (OQ-1).
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


class ParseError(ValueError):
    """The file cannot be read as the given format. ``str(exc)`` is bilingual."""


def parse_error(en, ar):
    return ParseError(f"{en} / {ar}")


@dataclass
class ParsedLine:
    line_no: int
    booking_date: date
    direction: str  # "credit" | "debit"
    amount: Decimal  # > 0, as written in the file
    value_date: date | None = None
    currency: str = ""
    original_amount: Decimal | None = None
    original_currency: str = ""
    bank_reference: str = ""
    end_to_end_id: str = ""
    customer_reference: str = ""
    transaction_code: str = ""
    counterparty_name: str = ""
    counterparty_iban: str = ""
    narrative: str = ""


@dataclass
class ParsedStatement:
    statement_id: str
    sequence_no: int | None
    period_start: date | None
    period_end: date | None
    currency: str
    opening_balance: Decimal | None  # signed: negative = overdrawn
    closing_balance: Decimal | None
    account_identifier: str = ""
    lines: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def decode_text(data, encodings=("utf-8-sig", "cp1256")):
    """Text of ``data`` in the first encoding that reads it (UTF-8, then Windows Arabic)."""
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise parse_error("The file is not in a supported text encoding (UTF-8 or Windows-1256).",
                      "ترميز الملف غير مدعوم (UTF-8 أو Windows-1256).")


def parse(fmt, data, *, layout=None):
    """Dispatch to the parser for ``fmt`` (a ``StatementFormat`` value)."""
    from . import camt053, csv_layout, mt940

    if data[:5] == b"%PDF-" or data[:4] in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"):
        raise parse_error("Unsupported format: PDF, Excel and other binary files are not statements this module reads.",
                          "صيغة غير مدعومة: ملفات PDF وExcel والملفات الثنائية ليست كشوفًا يقرؤها هذا النظام.")
    if fmt == "mt940":
        return mt940.parse(data)
    if fmt == "camt053":
        return camt053.parse(data)
    if fmt == "csv":
        if layout is None:
            raise parse_error("A CSV statement needs the bank account's CSV layout.", "كشف CSV يحتاج إلى تنسيق CSV للحساب.")
        return csv_layout.parse(data, layout)
    raise parse_error(f"Unsupported format {fmt!r}.", f"الصيغة {fmt!r} غير مدعومة.")
