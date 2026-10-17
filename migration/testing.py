"""Synthetic Falcon-shaped files for tests (migration.md §8). Fake names and fake amounts only; never real data.

Never used outside tests. The files are built in memory with openpyxl (xlsx) or as CSV text.
"""
import csv
import io
from datetime import datetime
from decimal import Decimal

from .readers import FALCON_JOURNAL_COLUMNS

HEADERS = [ar for ar, _, _ in FALCON_JOURNAL_COLUMNS.values()]
KEYS = list(FALCON_JOURNAL_COLUMNS)


def line(entry_no, on, code, name, debit=None, credit=None, description="قيد اختبار", serial=1, cost_centre=None,
         source="Journal", source_no=None, journal_type="اليوميه العامه"):
    """One journal row as a dict of canonical keys (amounts as numbers, like an xlsx cell)."""
    return {
        "debit": float(Decimal(debit)) if debit is not None else None,
        "credit": float(Decimal(credit)) if credit is not None else None,
        "entry_no": int(entry_no), "entry_serial": serial, "account_name": name, "description": description,
        "journal_type": journal_type, "date": datetime(on.year, on.month, on.day), "cost_centre": cost_centre,
        "source": source, "source_no": source_no if source_no is not None else int(entry_no),
        "account_code": int(code),
    }


def falcon_xlsx(rows, *, headers=None, order=None, title_rows=(), extra_sheets=()):
    """xlsx bytes in the Falcon general-journal layout. ``headers`` overrides header texts by key (spacing and ه/ة
    variants); ``order`` reorders the columns; ``title_rows`` are written above the header row."""
    from openpyxl import Workbook

    keys = order or KEYS
    texts = {k: FALCON_JOURNAL_COLUMNS[k][0] for k in KEYS}
    texts.update(headers or {})
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for title in title_rows:
        ws.append(list(title))
    ws.append([texts[k] for k in keys])
    for row in rows:
        ws.append([row.get(k) for k in keys])
    for name in extra_sheets:
        wb.create_sheet(name).append(["ملخص"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def table_csv(header, rows, encoding="utf-8"):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for r in rows:
        writer.writerow(r)
    return out.getvalue().encode(encoding)


def table_xlsx(header, rows, title_rows=()):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for title in title_rows:
        ws.append(list(title))
    ws.append(header)
    for r in rows:
        ws.append(list(r))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# A column map for the synthetic trial balance below (the real Falcon layout is OQ-1).
TB_COLUMNS = {
    "code": "رقم الحساب",
    "name": "اسم الحساب",
    "opening_debit": "افتتاحي مدين",
    "opening_credit": "افتتاحي دائن",
    "movement_debit": "حركة مدين",
    "movement_credit": "حركة دائن",
    "closing_debit": "ختامي مدين",
    "closing_credit": "ختامي دائن",
}
TB_HEADER = list(TB_COLUMNS.values())


def tb_rows(balances):
    """TB rows from {code: (name, opening, movement_debit, movement_credit)}; opening signed (debit positive)."""
    out = []
    for code, (name, opening, md, mc) in balances.items():
        opening, md, mc = Decimal(opening), Decimal(md), Decimal(mc)
        closing = opening + md - mc
        out.append([code, name, max(opening, 0), max(-opening, 0), md, mc, max(closing, 0), max(-closing, 0)])
    return out
