"""Synthetic Falcon-shaped files for tests (migration.md §8). Fake names and fake amounts only; never real data.

Never used outside tests. The files are built in memory with openpyxl (xlsx) or as CSV text.
"""
import csv
import io
from datetime import date, datetime
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


# --- the fillable workbook (migration.md §11) ------------------------------------------------------------------------
# A coherent fake dataset for two entities, ALM (Sharia, parent) and SUB: every sub-ledger ties to its TB. Fake names,
# fake identifiers (VAT, CR, ID numbers built from 9s), IBANs with valid check digits built from zeros.

def _wb_iban(bank_code, account):
    from .workbook_spec import _fake_sa_iban
    return _fake_sa_iban(bank_code, account)


WB_CUTOVER = date(2026, 12, 31)


def workbook_rows():
    """{sheet key: [row dicts]} with codes for choices; amounts as Decimal."""
    D = Decimal
    cut = WB_CUTOVER
    chart = {
        "ALM": [("110", "النقد والبنوك", "Cash and banks", "asset", None, "no"),
                ("1101", "بنك تجريبي جاري", "Test bank current", "asset", "110", "yes"),
                ("1102", "بنك تجريبي دولار", "Test bank USD", "asset", "110", "yes"),
                ("1201", "عملاء تجريبيون", "Test customers", "asset", None, "yes"),
                ("1202", "مستحق من الشركة التابعة", "Due from subsidiary", "asset", None, "yes"),
                ("1501", "سيارات", "Vehicles", "asset", None, "yes"),
                ("1591", "مجمع استهلاك السيارات", "Accumulated depreciation vehicles", "asset", None, "yes"),
                ("2101", "موردون تجريبيون", "Test vendors", "liability", None, "yes"),
                ("2501", "مخصص نهاية الخدمة", "End-of-service provision", "liability", None, "yes"),
                ("2601", "رأس المال", "Share capital", "equity", None, "yes"),
                ("2602", "أرباح مبقاة", "Retained earnings", "equity", None, "yes"),
                ("4101", "إيرادات إيجار", "Rental income", "income", None, "yes"),
                ("5101", "رواتب وأجور", "Salaries", "expense", None, "yes"),
                ("5201", "مصروف إيجار", "Rent expense", "expense", None, "yes")],
        "SUB": [("1101", "بنك تجريبي", "Test bank", "asset", None, "yes"),
                ("2102", "مستحق للشركة الأم", "Due to parent", "liability", None, "yes"),
                ("2601", "رأس المال", "Share capital", "equity", None, "yes")],
    }
    tb = {
        "ALM": [("1101", "50000.00", None), ("1102", "3750.00", None), ("1201", "23000.00", None),
                ("1202", "5000.00", None), ("1501", "120000.00", None), ("1591", None, "30000.00"),
                ("2101", None, "11500.00"), ("2501", None, "8000.00"), ("2601", None, "100000.00"),
                ("2602", None, "20000.00"), ("4101", None, "40000.00"), ("5101", "7750.00", None)],
        "SUB": [("1101", "55000.00", None), ("2102", None, "5000.00"), ("2601", None, "50000.00")],
    }
    erp = {"ALM": {"1101": "1100", "1102": "1100", "1201": "1200", "1202": ("1210", "SUB"), "1501": "1520",
                   "1591": "1590", "2101": "2100", "2501": "2500", "2601": "3100", "2602": "3300", "4101": "4100",
                   "5101": "5100", "5201": "5200"},
           "SUB": {"1101": "1100", "2102": ("2110", "ALM"), "2601": "3100"}}
    names = {e: {code: ar for code, ar, *_ in rows} for e, rows in chart.items()}
    entity = lambda code, name_en, name_ar, parent: {  # noqa: E731
        "entity_code": code, "name_en": name_en, "name_ar": name_ar, "cr_number": None, "vat_number": None,
        "parent_entity": parent, "cutover_date": cut, "legacy_system": "falcon", "legacy_company": f"{code}-TEST",
        "functional_currency": "SAR", "sharia_enforced": "yes", "pl_to_opening_equity": "no",
        "replay_journals": "no", "notes": None}
    party = lambda e, code, ar, kind="business", currency="SAR", group=None, **kw: {  # noqa: E731
        "entity": e, "code": code, "name_ar": ar, "name_en": None, "kind": kind, "vat_number": None,
        "cr_number": None, "country": "SA", "currency": currency, "payment_terms_days": 30, "group_entity": group, **kw}
    item = lambda e, who, party_code, kind, no, on, amount, sar=None, currency="SAR": {  # noqa: E731
        "entity": e, who: party_code, "document_type": kind, "document_no": no, "document_date": on,
        "due_date": None, "currency": currency, "amount": D(amount), "sar_amount": D(sar or amount),
        "description": "بند اختبار"}
    return {
        "entities": [entity("ALM", "ALM test holding", "شركة اختبار القابضة", None),
                     entity("SUB", "SUB test services", "شركة اختبار للخدمات", "ALM")],
        "falcon_chart": [{"entity": e, "code": c, "name_ar": ar, "name_en": en, "type": t, "parent": p, "postable": po}
                         for e, rows in chart.items() for c, ar, en, t, p, po in rows],
        "trial_balance": [{"entity": e, "code": c, "name": names[e][c], "debit": D(d) if d else None,
                           "credit": D(cr) if cr else None} for e, rows in tb.items() for c, d, cr in rows],
        "account_map": [{"entity": e, "legacy_code": c, "legacy_name": names[e][c],
                         "erp_code": t[0] if isinstance(t, tuple) else t,
                         "counterparty": t[1] if isinstance(t, tuple) else None, "note": None}
                        for e, rows in erp.items() for c, t in rows.items()],
        "customers": [party("ALM", "C001", "عميل اختبار أول"),
                      party("ALM", "C002", "عميل اختبار فرد", kind="individual", currency="USD"),
                      party("ALM", "C-SUB", "الشركة التابعة", group="SUB")],
        "vendors": [party("ALM", "V001", "مورد اختبار", resident="yes"),
                    party("SUB", "V-ALM", "الشركة الأم", group="ALM", resident="yes")],
        "open_receivables": [item("ALM", "customer", "C001", "invoice", "INV-1001", date(2026, 11, 30),
                                  "15000.00"),
                             item("ALM", "customer", "C001", "credit_note", "CN-0001", date(2026, 12, 5),
                                  "1000.00"),
                             item("ALM", "customer", "C002", "invoice", "INV-1002", date(2026, 12, 10),
                                  "2400.00", sar="9000.00", currency="USD"),
                             item("ALM", "customer", "C-SUB", "invoice", "INV-1003", date(2026, 12, 20),
                                  "5000.00")],
        "open_payables": [item("ALM", "vendor", "V001", "bill", "B-77", date(2026, 12, 15), "11500.00"),
                          item("SUB", "vendor", "V-ALM", "bill", "INV-1003", date(2026, 12, 20), "5000.00")],
        "bank_accounts": [
            {"entity": "ALM", "code": "ALM-RJ-SAR", "name_ar": "حساب جاري تجريبي", "name_en": "Test current",
             "bank": "alrajhi", "iban": _wb_iban("80", "100001"), "currency": "SAR", "falcon_code": "1101",
             "sharia_basis": "islamic", "statement_format": "mt940", "book_balance": D("50000.00"),
             "book_balance_sar": D("50000.00"), "statement_balance": D("50500.00"), "is_purification": "no"},
            {"entity": "ALM", "code": "ALM-SNB-USD", "name_ar": "حساب دولار تجريبي", "name_en": "Test USD",
             "bank": "snb", "iban": _wb_iban("10", "100002"), "currency": "USD", "falcon_code": "1102",
             "sharia_basis": "islamic", "statement_format": "camt053", "book_balance": D("1000.00"),
             "book_balance_sar": D("3750.00"), "statement_balance": D("1000.00"), "is_purification": "no"},
            {"entity": "SUB", "code": "SUB-RJ-SAR", "name_ar": "حساب الشركة التابعة", "name_en": "SUB current",
             "bank": "alrajhi", "iban": _wb_iban("80", "100003"), "currency": "SAR", "falcon_code": "1101",
             "sharia_basis": "islamic", "statement_format": "csv", "book_balance": D("55000.00"),
             "book_balance_sar": D("55000.00"), "statement_balance": D("55000.00"), "is_purification": "no"}],
        "employees": [
            {"entity": "ALM", "employee_no": "T-001", "full_name_ar": "موظف اختبار", "full_name_en": "Test Employee",
             "nationality": "SA", "id_type": "national_id", "id_number": "1999999911",
             "date_of_birth": date(1990, 1, 1), "gender": "male", "hire_date": date(2020, 1, 1),
             "employment_basis": "ksa_employee", "job_title_ar": "محاسب", "job_title_en": "Accountant",
             "basic_salary": D("10000.00"), "gosi_cohort": "legacy_m33", "salary_bank": "alrajhi",
             "iban": _wb_iban("80", "200001"), "eosb_provision": D("5000.00"), "leave_balance_days": D("10")},
            {"entity": "ALM", "employee_no": "T-002", "full_name_ar": "موظفة اختبار", "full_name_en": "Test Employee 2",
             "nationality": "EG", "id_type": "iqama", "id_number": "2999999912",
             "date_of_birth": date(1992, 2, 2), "gender": "female", "hire_date": date(2022, 2, 1),
             "employment_basis": "ksa_employee", "job_title_ar": "مساعدة", "job_title_en": "Assistant",
             "basic_salary": D("6000.00"), "eosb_provision": D("3000.00")}],
        "fixed_assets": [
            {"entity": "ALM", "asset_code": "FA-0001", "description_ar": "سيارة اختبار", "asset_class": "vehicle",
             "usage": "own_use", "location": "المقر", "acquisition_date": date(2024, 1, 1),
             "available_for_use_date": date(2024, 1, 1), "cost": D("120000.00"),
             "accumulated_depreciation": D("30000.00"), "method": "straight_line", "useful_life_months": 96,
             "remaining_life_months": 72, "cost_code": "1501", "accum_code": "1591"}],
    }


def filled_workbook(rows=None, *, labels=True, keep_examples=False, drop_key_row=False, drop_sheets=(),
                    drop_columns=()):
    """xlsx bytes of the template filled with ``rows`` ({sheet key: [dicts]}, default :func:`workbook_rows`).
    Choices are written as their bilingual dropdown labels (``labels``) or as codes; ``drop_key_row`` clears the
    field-key row; ``drop_columns`` is a list of (sheet key, column key) whose header rows are cleared."""
    from openpyxl import load_workbook

    from . import workbook_spec as spec
    from .workbook import build_workbook

    rows = workbook_rows() if rows is None else rows
    wb = load_workbook(io.BytesIO(build_workbook()))
    for sheet in spec.SHEETS:
        ws = wb[sheet.name]
        if sheet.key in drop_sheets:
            wb.remove(ws)
            continue
        if not keep_examples:
            for r in range(spec.FIRST_DATA_ROW, spec.FIRST_DATA_ROW + len(sheet.examples)):
                for c in range(1, len(sheet.columns) + 1):
                    ws.cell(row=r, column=c).value = None
        start = spec.FIRST_DATA_ROW + (len(sheet.examples) if keep_examples else 0)
        for n, record in enumerate(rows.get(sheet.key, [])):
            for c, column in enumerate(sheet.columns, start=1):
                value = record.get(column.key)
                if value is not None and labels and column.kind in ("choice", "yesno"):
                    key = column.choices if column.kind == "choice" else "yes_no"
                    value = spec.label(next(ch for ch in spec.CHOICES[key] if ch[0] == value))
                ws.cell(row=start + n, column=c).value = value
        for c, column in enumerate(sheet.columns, start=1):
            if drop_key_row:
                ws.cell(row=spec.KEY_ROW, column=c).value = None
            if (sheet.key, column.key) in drop_columns:
                for r in (spec.EN_ROW, spec.AR_ROW, spec.KEY_ROW):
                    ws.cell(row=r, column=c).value = None
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
