"""The opening-balance dry run fixture set (docs/migration/dry-run-2026-10-21.md). FICTIONAL data only (M3).

Two invented entities shaped like a Falcon migration at the cut-over date 2026-12-31:

- ``DRYH``, a holding company that enforces Sharia: banks (SAR, USD and the purification account), customers and
  vendors with open items (one in USD, one unapplied advance, one credit note), an intercompany receivable from DRYS,
  employees' end-of-service provisions and leave, fixed assets (loaded into the fixed-asset register after
  :func:`setup_fixed_assets`, fixed-assets R40, R41), two bai' ajel contracts behind 1240 / 1241 (and the 4610 profit
  of the year), VAT, equity and P&L.
- ``DRYS``, a subsidiary that does **not** enforce Sharia: a bank, one customer, the intercompany payable to DRYH and
  a conventional loan with its interest (2410, 5610: Sharia-prohibited accounts, allowed only because DRYS does not
  enforce Sharia).

Every name, number and amount is invented: codes are Falcon-shaped (3 to 5 digits, first digit = class), IDs and
VAT numbers are built from 9s, IBANs from zeros with valid check digits. A Falcon general-journal export of DRYH's
December 2026 is included, so that the TB's opening balance + journal movement = closing balance per code (R11).

``write_files(directory)`` writes the files a person would hand to the commands (the filled workbook, the Falcon
journal xlsx, a TB CSV with opening / movement / closing columns, its column map, and the account map CSV in the
``docs/migration/<entity>-account-map.csv`` layout). Write them to a scratch directory, never into git (R20).
"""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

D = Decimal
CUTOVER = date(2026, 12, 31)
PERIOD_OPENING = date(2026, 11, 30)  # the journal sample covers December 2026; the TB opening is at 30 November
HOLDING, SUBSIDIARY = "DRYH", "DRYS"

# Falcon chart per entity: code -> (Arabic name, English name, type, ERP code, counterparty entity or None).
CHART = {
    HOLDING: {
        "1101": ("بنك وهمي جاري", "Fictional bank current", "asset", "1100", None),
        "1102": ("بنك وهمي دولار", "Fictional bank USD", "asset", "1100", None),
        "1103": ("حساب التطهير الوهمي", "Fictional purification account", "asset", "1140", None),
        "1201": ("عملاء وهميون", "Fictional customers", "asset", "1200", None),
        "1202": ("مستحق من الشركة التابعة الوهمية", "Due from fictional subsidiary", "asset", "1210", SUBSIDIARY),
        "1251": ("ذمم البيع الآجل للموظفين", "Employee bai' ajel receivable", "asset", "1240", None),
        "1252": ("أرباح البيع الآجل المؤجلة", "Deferred bai' ajel profit", "asset", "1241", None),
        "1501": ("سيارات", "Vehicles", "asset", "1520", None),
        "1502": ("أجهزة حاسب", "IT equipment", "asset", "1500", None),
        "1591": ("مجمع الاستهلاك", "Accumulated depreciation", "asset", "1590", None),
        "2101": ("موردون وهميون", "Fictional vendors", "liability", "2100", None),
        "2201": ("ضريبة القيمة المضافة المستحقة", "VAT output", "liability", "2200", None),
        "2501": ("مخصص نهاية الخدمة", "End-of-service provision", "liability", "2500", None),
        "2601": ("رأس المال", "Share capital", "equity", "3100", None),
        "2602": ("أرباح مبقاة", "Retained earnings", "equity", "3300", None),
        "2701": ("مبالغ مستحقة للتطهير", "Charity payable", "liability", "2600", None),
        "4101": ("إيرادات إيجار", "Rental income", "income", "4100", None),
        "4611": ("إيرادات أرباح البيع الآجل", "Bai' ajel profit income", "income", "4610", None),
        "5101": ("رواتب وأجور", "Salaries", "expense", "5100", None),
        "5201": ("مصروف إيجار", "Rent expense", "expense", "5200", None),
    },
    SUBSIDIARY: {
        "1101": ("بنك الشركة التابعة", "Subsidiary bank", "asset", "1100", None),
        "1201": ("عملاء الشركة التابعة", "Subsidiary customers", "asset", "1200", None),
        "2102": ("مستحق للشركة الأم", "Due to parent", "liability", "2110", HOLDING),
        "2401": ("قرض بنكي تقليدي", "Conventional bank loan", "liability", "2410", None),
        "2601": ("رأس المال", "Share capital", "equity", "3100", None),
        "4101": ("إيرادات خدمات", "Service income", "income", "4100", None),
        "5101": ("رواتب وأجور", "Salaries", "expense", "5100", None),
        "5611": ("مصروف فوائد", "Interest expense", "expense", "5610", None),
    },
}

# Closing trial balance at the cut-over, signed (debit positive). Halalas on purpose (rounding, SAR 2 dp).
CLOSING = {
    HOLDING: {
        "1101": "182345.67", "1102": "18750.00", "1103": "1234.56", "1201": "44250.50", "1202": "12500.00",
        "1251": "34500.00", "1252": "-2875.00", "1501": "240000.00", "1502": "36000.00", "1591": "-101500.00",
        "2101": "-27000.00", "2201": "-4567.89", "2501": "-58000.00", "2601": "-300000.00", "2602": "-49278.28",
        "2701": "-1234.56", "4101": "-150000.00", "4611": "-1125.00", "5101": "96000.00", "5201": "30000.00",
    },
    SUBSIDIARY: {
        "1101": "61000.00", "1201": "9200.10", "2102": "-12500.00", "2401": "-30000.00", "2601": "-25000.00",
        "4101": "-24500.10", "5101": "20000.00", "5611": "1800.00",
    },
}

# The expected ERP trial balance after posting, per ERP account (worked out by hand from CLOSING and the map above,
# not by the code under test): the tie-out target.
EXPECTED_ERP = {
    HOLDING: {
        "1100": "201095.67", "1140": "1234.56", "1200": "44250.50", "1210": "12500.00", "1240": "34500.00",
        "1241": "-2875.00", "1500": "36000.00", "1520": "240000.00", "1590": "-101500.00", "2100": "-27000.00",
        "2200": "-4567.89", "2500": "-58000.00", "2600": "-1234.56", "3100": "-300000.00", "3300": "-49278.28",
        "4100": "-150000.00", "4610": "-1125.00", "5100": "96000.00", "5200": "30000.00",
    },
    SUBSIDIARY: {
        "1100": "61000.00", "1200": "9200.10", "2110": "-12500.00", "2410": "-30000.00", "3100": "-25000.00",
        "4100": "-24500.10", "5100": "20000.00", "5610": "1800.00",
    },
}
EXPECTED_TOTAL_DEBIT = {HOLDING: D("695580.73"), SUBSIDIARY: D("92000.10")}

# DRYH's bai' ajel contracts (W14), worked by hand: receivable = price + VAT; recognised before the cut-over = profit
# x paid / receivable (half-up); the instalment = receivable / instalments; the instalments left = open / instalment.
# contract: (receivable, paid, open on 1240, recognised, deferred on 1241, instalment, instalments left)
BAI_AJEL = {
    "BAJ-F-01": (D("41400.00"), D("17250.00"), D("24150.00"), D("1250.00"), D("1750.00"), D("1725.00"), 14),
    "BAJ-F-02": (D("11500.00"), D("1150.00"), D("10350.00"), D("125.00"), D("1125.00"), D("1150.00"), 9),
}  # open 34,500.00 = TB 1251 (1240); deferred 2,875.00 = - TB 1252 (1241)

# DRYH's Falcon general journal for December 2026 (entry number = yy + 6-digit sequence), the movement of the TB below
# from its opening at 30 November to its closing at the cut-over. Lines: (code, debit, credit, بيان).
JOURNAL = [
    ("26004101", date(2026, 12, 1), "مستخلص عميل", "501", None, [
        ("1201", "57500.00", None, "مستخلص إيجار ديسمبر"), ("4101", None, "50000.00", "مستخلص إيجار ديسمبر"),
        ("2201", None, "7500.00", "ضريبة مستخلص ديسمبر")]),
    ("26004102", date(2026, 12, 6), "Journal", None, None, [
        ("1101", "57500.00", None, "تحصيل مستخلص ديسمبر"), ("1201", None, "57500.00", "تحصيل مستخلص ديسمبر")]),
    ("26004103", date(2026, 12, 10), "Journal", None, "الإدارة", [
        ("5101", "32000.00", None, "رواتب ديسمبر"), ("1101", None, "32000.00", "رواتب ديسمبر")]),
    ("26004104", date(2026, 12, 15), "Journal", None, None, [
        ("5201", "30000.00", None, "إيجار المكتب"), ("2101", None, "30000.00", "إيجار المكتب")]),
    ("26004105", date(2026, 12, 20), "Journal", None, None, [
        ("2101", "30000.00", None, "سداد المورد"), ("1101", None, "30000.00", "سداد المورد")]),
    ("26004106", date(2026, 12, 30), "Journal", None, None, [
        ("2201", "2932.11", None, "سداد ضريبة القيمة المضافة"), ("1101", None, "2932.11", "سداد ضريبة القيمة المضافة")]),
]

TB_COLUMNS = {"code": "رقم الحساب", "name": "اسم الحساب", "opening_debit": "افتتاحي مدين",
              "opening_credit": "افتتاحي دائن", "movement_debit": "حركة مدين", "movement_credit": "حركة دائن",
              "closing_debit": "ختامي مدين", "closing_credit": "ختامي دائن"}


def _iban(bank_code, account):
    from migration.workbook_spec import _fake_sa_iban
    return _fake_sa_iban(bank_code, account)


def movements(entity=HOLDING):
    """Journal movement per Falcon code: {code: (debit, credit)}."""
    out = {}
    for _no, _on, _source, _source_no, _cc, lines in (JOURNAL if entity == HOLDING else []):
        for code, debit, credit, _text in lines:
            d, c = out.get(code, (D("0"), D("0")))
            out[code] = (d + D(debit or "0"), c + D(credit or "0"))
    return out


def tb_table(entity=HOLDING):
    """TB rows (header, rows) with opening = closing − journal movement, so R11 ties out by construction."""
    moves = movements(entity)
    rows = []
    for code, closing in CLOSING[entity].items():
        closing = D(closing)
        md, mc = moves.get(code, (D("0"), D("0")))
        opening = closing - md + mc
        rows.append([code, CHART[entity][code][0], max(opening, 0), max(-opening, 0), md, mc,
                     max(closing, 0), max(-closing, 0)])
    return list(TB_COLUMNS.values()), rows


def journal_rows():
    """The Falcon journal as rows of canonical keys (``migration.testing.line``)."""
    from migration import testing as t

    out = []
    for no, on, source, source_no, cc, lines in JOURNAL:
        for serial, (code, debit, credit, text) in enumerate(lines, start=1):
            out.append(t.line(no, on, code, CHART[HOLDING][code][0], debit=debit, credit=credit, description=text,
                              serial=serial, cost_centre=cc, source=source,
                              source_no=int(source_no) if source_no else None))
    return out


def map_rows(entity):
    """Account map rows (``docs/migration/<entity>-account-map.csv`` layout)."""
    return [{"legacy_code": code, "legacy_name": ar, "erp_code": erp, "counterparty": cp or "",
             "note": "fictional dry run"} for code, (ar, _en, _t, erp, cp) in CHART[entity].items()]


def workbook_rows():
    """{sheet key: [row dicts]} for ``migration.testing.filled_workbook``."""
    entity = lambda code, en, ar, parent, sharia: {  # noqa: E731
        "entity_code": code, "name_en": en, "name_ar": ar, "cr_number": None, "vat_number": None,
        "parent_entity": parent, "cutover_date": CUTOVER, "legacy_system": "falcon",
        "legacy_company": f"{code}-FICTIONAL", "functional_currency": "SAR", "sharia_enforced": sharia,
        "pl_to_opening_equity": "no", "replay_journals": "no", "notes": None}
    party = lambda e, code, ar, kind="business", currency="SAR", group=None, **kw: {  # noqa: E731
        "entity": e, "code": code, "name_ar": ar, "name_en": None, "kind": kind, "vat_number": None,
        "cr_number": None, "country": "SA", "currency": currency, "payment_terms_days": 30, "group_entity": group, **kw}
    item = lambda e, who, code, kind, no, on, amount, sar=None, currency="SAR": {  # noqa: E731
        "entity": e, who: code, "document_type": kind, "document_no": no, "document_date": on, "due_date": None,
        "currency": currency, "amount": D(amount), "sar_amount": D(sar or amount), "description": "بند وهمي"}
    bank = lambda e, code, ar, bank_code, bank, account, currency, falcon, book, sar, purification="no": {  # noqa: E731
        "entity": e, "code": code, "name_ar": ar, "name_en": f"Fictional {code}", "bank": bank,
        "iban": _iban(bank_code, account), "currency": currency, "falcon_code": falcon, "sharia_basis": "islamic",
        "statement_format": "mt940", "book_balance": D(book), "book_balance_sar": D(sar), "statement_balance": D(book),
        "is_purification": purification}
    employee = lambda e, no, ar, en, nationality, id_type, id_number, eosb, **kw: {  # noqa: E731
        "entity": e, "employee_no": no, "full_name_ar": ar, "full_name_en": en, "nationality": nationality,
        "id_type": id_type, "id_number": id_number, "date_of_birth": date(1988, 3, 3), "gender": "male",
        "hire_date": date(2019, 4, 1), "employment_basis": "ksa_employee", "job_title_ar": "محاسب وهمي",
        "job_title_en": "Fictional accountant", "basic_salary": D("8000.00"), "eosb_provision": D(eosb), **kw}
    asset = lambda e, code, ar, klass, cost, accum, cost_code, life, remaining: {  # noqa: E731
        "entity": e, "asset_code": code, "description_ar": ar, "asset_class": klass, "usage": "own_use",
        "location": "مقر وهمي", "acquisition_date": date(2023, 1, 1), "available_for_use_date": date(2023, 1, 1),
        "cost": D(cost), "accumulated_depreciation": D(accum), "method": "straight_line", "useful_life_months": life,
        "remaining_life_months": remaining, "cost_code": cost_code, "accum_code": "1591"}
    contract = lambda e, no, employee_no, ar, price, profit, vat, instalments, paid: {  # noqa: E731
        "entity": e, "contract_no": no, "employee_no": employee_no, "asset_description_ar": ar,
        "asset_description_en": None, "sale_date": date(2025, 12, 15), "sale_price": D(price),
        "profit_amount": D(profit), "vat_amount": D(vat), "instalments": instalments, "paid_to_date": D(paid),
        "next_instalment_period": "2027-01", "consent_ref": f"CONSENT-{no}"}
    H, S = HOLDING, SUBSIDIARY
    return {
        "entities": [entity(H, "Fictional holding (dry run)", "شركة قابضة وهمية", None, "yes"),
                     entity(S, "Fictional subsidiary (dry run)", "شركة تابعة وهمية", H, "no")],
        "falcon_chart": [{"entity": e, "code": c, "name_ar": ar, "name_en": en, "type": typ, "parent": None,
                          "postable": "yes"} for e, rows in CHART.items() for c, (ar, en, typ, _erp, _cp) in rows.items()],
        "trial_balance": [{"entity": e, "code": c, "name": CHART[e][c][0],
                           "debit": D(v) if D(v) > 0 else None, "credit": -D(v) if D(v) < 0 else None}
                          for e, rows in CLOSING.items() for c, v in rows.items()],
        "account_map": [{"entity": e, "legacy_code": r["legacy_code"], "legacy_name": r["legacy_name"],
                         "erp_code": r["erp_code"], "counterparty": r["counterparty"] or None, "note": r["note"]}
                        for e in CHART for r in map_rows(e)],
        "customers": [party(H, "C001", "عميل وهمي أول"), party(H, "C002", "عميل وهمي فرد", kind="individual",
                                                               currency="USD"),
                      party(H, "C003", "عميل وهمي ثالث"), party(H, "C-DRYS", "الشركة التابعة الوهمية", group=S),
                      party(S, "C101", "عميل وهمي للتابعة")],
        "vendors": [party(H, "V001", "مورد وهمي أول", resident="yes"), party(H, "V002", "مورد وهمي ثان", resident="yes"),
                    party(H, "V003", "مورد وهمي ثالث", resident="yes"),
                    party(S, "V-DRYH", "الشركة الأم الوهمية", group=H, resident="yes")],
        "open_receivables": [
            item(H, "customer", "C001", "invoice", "INV-26-0101", date(2026, 11, 20), "40250.75"),
            item(H, "customer", "C001", "credit_note", "CN-26-0007", date(2026, 12, 2), "1500.25"),
            item(H, "customer", "C002", "invoice", "INV-26-0102", date(2026, 12, 10), "2000.00", sar="7500.00",
                 currency="USD"),
            item(H, "customer", "C003", "advance", "ADV-26-0003", date(2026, 12, 15), "2000.00"),
            item(H, "customer", "C-DRYS", "invoice", "INV-26-0103", date(2026, 12, 20), "12500.00"),
            item(S, "customer", "C101", "invoice", "S-INV-0042", date(2026, 12, 28), "9200.10")],
        "open_payables": [
            item(H, "vendor", "V001", "bill", "B-9001", date(2026, 12, 1), "23456.78"),
            item(H, "vendor", "V002", "bill", "B-9002", date(2026, 12, 5), "5000.00"),
            item(H, "vendor", "V001", "vendor_credit_note", "VCN-17", date(2026, 12, 9), "456.78"),
            item(H, "vendor", "V003", "advance", "PADV-3", date(2026, 12, 11), "1000.00"),
            item(S, "vendor", "V-DRYH", "bill", "INV-26-0103", date(2026, 12, 20), "12500.00")],
        "bank_accounts": [
            bank(H, "DRYH-RJ-SAR", "حساب جاري وهمي", "80", "alrajhi", "700001", "SAR", "1101", "182345.67", "182345.67"),
            bank(H, "DRYH-SNB-USD", "حساب دولار وهمي", "10", "snb", "700002", "USD", "1102", "5000.00", "18750.00"),
            bank(H, "DRYH-RJ-PUR", "حساب تطهير وهمي", "80", "alrajhi", "700003", "SAR", "1103", "1234.56", "1234.56",
                 purification="yes"),
            bank(S, "DRYS-RJ-SAR", "حساب التابعة الوهمي", "80", "alrajhi", "700004", "SAR", "1101", "61000.00",
                 "61000.00")],
        "employees": [
            employee(H, "F-001", "موظف وهمي أول", "Fictional Employee One", "SA", "national_id", "1999999971",
                     "45678.90", gosi_cohort="legacy_m33", leave_balance_days=D("12.5")),
            employee(H, "F-002", "موظف وهمي ثان", "Fictional Employee Two", "EG", "iqama", "2999999972", "12321.10")],
        # W14: the two contracts behind DRYH's 1240 34,500.00 and 1241 -2,875.00 (worked by hand, BAI_AJEL below).
        "bai_ajel_contracts": [
            contract(H, "BAJ-F-01", "F-001", "سيارة وهمية مبيعة لموظف", "36000.00", "3000.00", "5400.00", 24,
                     "17250.00"),
            contract(H, "BAJ-F-02", "F-002", "أجهزة وهمية مبيعة لموظف", "10000.00", "1250.00", "1500.00", 10,
                     "1150.00")],
        "fixed_assets": [
            # fixed-assets AT-32: one vehicle at cost 100,000.00, accumulated 40,000.00, residual 0, 36 months left
            # (its first ERP month is 60,000.00 / 36 = 1,666.67); the sheet still ties to 1501 and 1591.
            asset(H, "FA-F-01", "سيارة وهمية أولى", "vehicle", "100000.00", "40000.00", "1501", 60, 36),
            asset(H, "FA-F-02", "سيارة وهمية ثانية", "vehicle", "140000.00", "51500.00", "1501", 60, 39),
            asset(H, "FA-F-03", "أجهزة حاسب وهمية", "it_equipment", "36000.00", "10000.00", "1502", 36, 26)],
    }


def setup_fixed_assets(entity, user):
    """The fixed-asset setup DRYH's register load needs (fixed-assets R31, R40), done by ``user`` (an accountant in
    the entity): the group template's categories, and a category for IT equipment on 1500, where DRYH's Falcon 1502
    maps (the IT account 1540 is still proposed, OQ-5). Returns the categories created."""
    from fixed_assets import services
    from fixed_assets.models import AssetCategory

    created, _skipped = services.setup_entity(entity, user=user)
    if not AssetCategory.objects.filter(entity=entity, code="ITF").exists():
        services.create_category(entity, user=user, code="ITF", name_en="IT equipment (on 1500 until 1540 is approved)",
                                 name_ar="أجهزة الحاسب (على 1500 حتى اعتماد 1540)", asset_class="it_equipment",
                                 cost_account="1500", accum_depr_account="1590", depr_expense_account="5400")
        created.append("ITF")
    return created


def open_items_by_control(book, entity_code):
    """What the R23 loader (not built) would load for ``entity_code``, read from the parsed workbook: the signed SAR
    open items per (control account, counterparty) — receivables 1200 / 1210 (group customer), payables 2100 / 2110
    (group vendor), credit balances negative. Written independently of ``workbook_checks`` W8 on purpose."""
    signs = {"open_receivables": {"invoice": 1, "debit_note": 1, "credit_note": -1, "advance": -1},
             "open_payables": {"bill": -1, "vendor_credit_note": 1, "advance": 1}}
    accounts = {"open_receivables": ("customers", "customer", "1200", "1210"),
                "open_payables": ("vendors", "vendor", "2100", "2110")}
    out = {}
    for key, (party_sheet, party_key, control, group) in accounts.items():
        parties = {r["code"]: r for r in book.records(party_sheet, entity_code)}
        for r in book.records(key, entity_code):
            cp = parties[r[party_key]]["group_entity"] or ""
            k = (group if cp else control, cp)
            out[k] = out.get(k, D("0")) + signs[key][r["document_type"]] * r["sar_amount"]
    return out


def write_files(directory):
    """Write the fixture files into ``directory`` (a scratch folder, never git) and return their paths."""
    from migration import testing as t

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {"workbook": directory / "dry-run-workbook.xlsx", "journal": directory / "dryh-journal-2026-12.xlsx",
             "tb": directory / "dryh-tb-2026-12.csv", "tb_columns": directory / "dryh-tb-columns.json"}
    paths["workbook"].write_bytes(t.filled_workbook(workbook_rows()))
    paths["journal"].write_bytes(t.falcon_xlsx(journal_rows(), title_rows=[["شركة قابضة وهمية - دفتر اليومية العامة"], []]))
    header, rows = tb_table(HOLDING)
    paths["tb"].write_bytes(t.table_csv(header, rows))
    paths["tb_columns"].write_text(json.dumps({"columns": TB_COLUMNS}, ensure_ascii=False), encoding="utf-8")
    for entity in CHART:
        path = directory / f"{entity.lower()}-account-map.csv"
        path.write_bytes(t.table_csv(["legacy_code", "legacy_name", "erp_code", "counterparty", "note"],
                                     [[r[k] for k in ("legacy_code", "legacy_name", "erp_code", "counterparty", "note")]
                                      for r in map_rows(entity)]))
        paths[f"map_{entity}"] = path
    return {k: str(v) for k, v in paths.items()}
