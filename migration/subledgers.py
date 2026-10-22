"""The sub-ledger loaders of the migration workbook (migration.md R23, OQ-12; dry run 2026-10-21 gaps 1, 4 and 5).

``load_subledgers(entity, book, sections, user=..., apply=False)`` loads one entity's rows of a filled workbook into
the modules that own them, through **their own services and rights** (R24: the user's membership in that entity):

=================  ====================================================================  ==========================
section            what is loaded (owning module, service)                               right needed
=================  ====================================================================  ==========================
``receivables``    Customers, then Open receivables as migrated open items (invoicing,     invoicing.manage_customer,
                   ``migration_port.ensure_customer`` / ``load_open_item``)               migration.import
``payables``       Vendors, then Open payables as migrated open items (invoicing)         invoicing.manage_vendor,
                                                                                          migration.import
``banks``          Bank accounts as drafts (banking ``create_bank_account``); the second  banking.manage_bank_account
                   person approves them in banking (R7)
``employees``      Employees as draft hr records (hr ``create_employee``)                 hr.manage_employee
``eosb``           End-of-service provisions at the cut-over (payroll                     payroll.prepare_run
                   ``record_opening_eosb_provision``)
``leave``          Annual leave balances as draft opening adjustments (hr                 hr.manage_employee
                   ``record_opening_leave_balance``; approved in hr)
``bai_ajel``       Bai' ajel contracts as migrated contracts (payroll                     payroll.create_deferred_sale
                   ``load_migrated_deferred_sale``, invoicing through the port)
``fixed_assets``   The opening asset register (fixed assets                              migration.import
                   ``migration_port.load_opening_asset``; fixed-assets R40, R41)
=================  ====================================================================  ==========================

The entity's part of the workbook must check without an error first (W4 to W17). Dry run unless ``apply``: the same
service calls run inside a transaction that is rolled back, so the report is the real one. All or nothing: one
refused row loads none. A re-run skips what is loaded already (each service is idempotent) and refuses changed
values. The sections that feed the R22 posting gate (receivables, payables, eosb, bai_ajel) are refused once the
entity's opening entry is posted: the sub-ledgers and the opening balances are tied out once, before posting.

Employees are matched to hr by their ID number (hashed, never by name); the workbook's employee number links the
Employees sheet to the Bai' ajel contracts sheet. Fixed assets are loaded into the register of the fixed-assets module
(fixed-assets R40): each row's category is the entity's category of that asset class whose cost account is the ERP
account the approved map gives the row's Falcon cost code; categories are set up before the load. Messages never
repeat a personal-data value (W3).
"""
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from core.access import AccessDenied

from . import workbook_checks
from . import workbook_spec as spec
from .readers import bi

SECTIONS = ("receivables", "payables", "banks", "employees", "eosb", "leave", "bai_ajel", "fixed_assets")
GATED = frozenset({"receivables", "payables", "eosb", "bai_ajel", "fixed_assets"})  # feed the R22 posting gate
ANNUAL_LEAVE = "ANNUAL"
AR_TYPES = {"invoice": "invoice", "debit_note": "debit_note", "credit_note": "credit_note", "advance": "advance_received"}
AP_TYPES = {"bill": "bill", "vendor_credit_note": "vendor_credit_note", "advance": "advance_paid"}


@dataclass
class SectionResult:
    created: int = 0
    skipped: int = 0
    problems: list = field(default_factory=list)  # bilingual texts
    warnings: list = field(default_factory=list)
    objects: list = field(default_factory=list)  # what was created (rolled back in a dry run)


@dataclass
class SubledgerPlan:
    entity: str
    report: object  # workbook_checks.WorkbookReport for the entity
    sections: dict = field(default_factory=dict)  # name -> SectionResult
    applied: bool = False

    @property
    def problems(self):
        return [p for r in self.sections.values() for p in r.problems]

    @property
    def ok(self):
        return not self.report.errors and not self.problems


class _Rollback(Exception):
    pass


def _messages(exc):
    if isinstance(exc, AccessDenied):
        return [str(exc)]
    return list(getattr(exc, "messages", None) or [str(exc)])


def _where(sheet_key, row):
    sheet = spec.SHEET_BY_KEY[sheet_key]
    return f"{sheet.name} row {row} / {sheet.title_ar} الصف {row}"


def _call(result, sheet_key, row, fn):
    """Run one row's service call in its own savepoint. Returns (ok, value); a refusal is a problem of that row (the
    run continues so the report lists every refusal, and is rolled back as a whole)."""
    try:
        with transaction.atomic():
            return True, fn()
    except (ValidationError, AccessDenied) as exc:
        result.problems += [f"{_where(sheet_key, row)}: {m}" for m in _messages(exc)]
    except ValueError as exc:  # setup helpers raise ValueError with a bilingual text
        result.problems.append(f"{_where(sheet_key, row)}: {exc}")
    return False, None


def _attempt(result, sheet_key, row, fn):
    return _call(result, sheet_key, row, fn)[1]


def _count(result, outcome):
    obj, created = outcome if isinstance(outcome, tuple) else (outcome, outcome is not None)
    if created:
        result.created += 1
        result.objects.append(obj)
    else:
        result.skipped += 1
    return obj


def _given(record, keys):
    return {k: record[k] for k in keys if record[k] not in (None, "")}


# --- parties and open items (invoicing) ------------------------------------------------------------------------------

PARTY_KEYS = ("name_ar", "name_en", "building_no", "street_ar", "street_en", "district_ar", "district_en", "city_ar",
              "city_en", "postal_code", "country", "currency")


def _party_fields(record, vendor=False):
    fields = _given(record, PARTY_KEYS)
    vat = record["vat_number"]
    fields.update(is_vat_registered=bool(vat), group_entity=record["group_entity"])
    if vat:
        fields["vat_number"] = vat
    if vendor:
        fields["vendor_kind"] = record["kind"]
        fields["is_resident"] = record["resident"] is not False
        fields.update(_given(record, ("residence_country", "wht_category")))
        if record["wht_gross_up"] is not None:
            fields["wht_gross_up"] = bool(record["wht_gross_up"])
        if record["iban"]:
            fields["bank_iban"] = record["iban"]
    else:
        fields["customer_kind"] = record["kind"]
        if record["payment_terms_days"] is not None:
            fields["payment_terms_days"] = record["payment_terms_days"]
    return fields


def _open_items(entity, book, user, result, *, receivable, cutover, source_system):
    from invoicing import migration_port
    from invoicing import services as invoicing
    from invoicing.models import MigratedOpenItem

    party_sheet, items_sheet = ("customers", "open_receivables") if receivable else ("vendors", "open_payables")
    ensure = migration_port.ensure_customer if receivable else migration_port.ensure_vendor
    parties = {}
    for r in book.records(party_sheet, entity.code):
        if not r["code"]:
            continue
        fields = _party_fields(r, vendor=not receivable)
        ok, outcome = _call(result, party_sheet, r.row, lambda r=r, fields=fields: ensure(
            entity, user=user, code=r["code"], **fields))
        if not ok:
            continue
        parties[r["code"]] = _count(result, outcome)
        if receivable and outcome[1] and r["cr_number"]:  # a new customer's commercial registration (buyer ID, R16)
            _call(result, party_sheet, r.row, lambda p=outcome[0], cr=r["cr_number"]: invoicing.add_customer_identifier(
                p, "CRN", cr, user=user))
    side = MigratedOpenItem.Side.RECEIVABLE if receivable else MigratedOpenItem.Side.PAYABLE
    types = AR_TYPES if receivable else AP_TYPES
    party_key = "customer" if receivable else "vendor"
    for r in book.records(items_sheet, entity.code):
        party = parties.get(r[party_key])
        if party is None:
            result.problems.append(bi(f"{_where(items_sheet, r.row)}: party {r[party_key]} was not loaded.",
                                      f"{_where(items_sheet, r.row)}: لم يُحمَّل الطرف {r[party_key]}."))
            continue
        outcome = _attempt(result, items_sheet, r.row, lambda r=r, party=party: migration_port.load_open_item(
            entity, user=user, side=side, party=party, doc_type=types[r["document_type"]],
            legacy_number=r["document_no"], document_date=r["document_date"], due_date=r["due_date"],
            currency=r["currency"], amount=r["amount"], amount_sar=r["sar_amount"], cutover=cutover,
            description=r["description"] or "", file_sha256=book.sha256, source_system=source_system))
        if outcome is not None:
            _count(result, outcome)


# --- bank accounts (banking) -----------------------------------------------------------------------------------------

def _banks(entity, book, user, result, *, cutover, amap):
    from banking import services as banking

    rows = {r.legacy_code: r for r in amap.rows.select_related("account")} if amap is not None else {}
    if amap is None:
        result.problems.append(bi("Bank accounts need the entity's approved account map (Falcon code → ERP GL).",
                                  "تحتاج الحسابات البنكية إلى خريطة الحسابات المعتمدة للكيان."))
        return
    for r in book.records("bank_accounts", entity.code):
        where = _where("bank_accounts", r.row)
        if r["currency"] and r["currency"] != entity.base_currency_id:
            result.warnings.append(bi(
                f"{where}: {r['code']} is in {r['currency']}: a foreign-currency bank account needs its opening balance "
                "in that currency on a currency sub-account (OQ-13); it is not loaded here. Set it up in banking.",
                f"{where}: {r['code']} بعملة {r['currency']}: يحتاج الحساب البنكي بعملة أجنبية إلى رصيده الافتتاحي بتلك "
                "العملة على حساب فرعي بها؛ لا يُحمَّل هنا. أعدّه في وحدة البنوك."))
            continue
        if r["bank"] == "other":
            result.problems.append(bi(f"{where}: the bank is not in the ERP's bank list; add it in banking first.",
                                      f"{where}: البنك ليس في قائمة البنوك في النظام؛ أضفه في وحدة البنوك أولًا."))
            continue
        row = rows.get(r["falcon_code"])
        if row is None:
            result.problems.append(bi(f"{where}: Falcon {r['falcon_code']} is not in the approved account map.",
                                      f"{where}: الكود {r['falcon_code']} غير موجود في خريطة الحسابات المعتمدة."))
            continue
        balance = r["statement_balance"] if r["statement_balance"] is not None else r["book_balance"]
        existing = entity.bank_accounts.select_related("bank", "gl_account").filter(code=r["code"]).first()
        if existing is not None:  # R23: a re-run skips the same account and refuses changed values
            if _bank_differs(existing, r, row.account, balance, cutover):
                result.problems.append(bi(
                    f"{where}: bank account {r['code']} was loaded before with other values; change it in banking "
                    "(an approved change), not through the workbook.",
                    f"{where}: حُمّل الحساب البنكي {r['code']} سابقًا بقيم أخرى؛ عدّله في وحدة البنوك بطلب تعديل معتمد "
                    "لا من خلال الملف."))
            else:
                result.skipped += 1
            continue
        account = _attempt(result, "bank_accounts", r.row, lambda r=r, row=row, balance=balance:
                           banking.create_bank_account(
                               entity, user=user, code=r["code"], name_en=r["name_en"], name_ar=r["name_ar"],
                               bank=r["bank"], currency=r["currency"], gl_account=row.account,
                               sharia_basis=r["sharia_basis"], statement_format=r["statement_format"] or "mt940",
                               opening_statement_date=cutover, opening_statement_balance=balance, active_from=cutover,
                               iban=r["iban"] or "", account_number=r["account_number"] or "",
                               is_purification_account=bool(r["is_purification"])))
        if account is not None:
            _count(result, account)


def _bank_differs(existing, r, gl, balance, cutover):
    from banking import iban as iban_rules
    from banking.crypto import keyed_hash

    iban = iban_rules.normalise(r["iban"] or "")
    number = "".join(str(r["account_number"] or "").split())
    return ((existing.bank.code, existing.currency_id, existing.gl_account_id, existing.opening_statement_balance,
             existing.opening_statement_date, existing.is_purification_account, existing.sharia_basis)
            != (r["bank"], r["currency"], gl.pk, balance, cutover, bool(r["is_purification"]), r["sharia_basis"])
            or existing.iban_hash != (keyed_hash(iban) if iban else "")
            or existing.account_number_last4 != (number[-4:] if number else ""))


# --- employees, end of service, leave, bai' ajel (hr, payroll) ------------------------------------------------------

def _bank_name(code):
    return spec.BANKS_V1[code][0] if code in spec.BANKS_V1 else ""


def _employees(entity, book, user, result):
    from hr import services as hr

    for r in book.records("employees", entity.code):
        if not r["id_number"]:
            continue
        ok, found = _call(result, "employees", r.row, lambda r=r: hr.find_current_employee(entity, r["id_number"],
                                                                                            user=user))
        if not ok:
            continue
        if found is not None:
            result.skipped += 1
            continue
        fields = _given(r, ("full_name_en", "full_name_ar", "nationality", "id_type", "id_number", "date_of_birth",
                            "gender", "hire_date", "employment_basis", "gosi_number", "gosi_cohort", "iban"))
        if r["salary_bank"]:
            fields["bank_name"] = _bank_name(r["salary_bank"])
        ok, employee = _call(result, "employees", r.row, lambda fields=fields: hr.create_employee(entity, user=user,
                                                                                                  **fields))
        if ok:
            _count(result, (employee, True))


def _employee_of(entity, person, user, result, sheet_key, row):
    """The hr employee of a workbook Employees row, matched by ID number; a problem when not in hr yet."""
    from hr import services as hr

    ok, employee = _call(result, sheet_key, row, lambda: hr.find_current_employee(entity, person["id_number"],
                                                                                   user=user))
    if ok and employee is None:
        result.problems.append(bi(f"{_where(sheet_key, row)}: employee {person['employee_no']} is not in hr yet; load "
                                  "the employees first.", f"{_where(sheet_key, row)}: الموظف {person['employee_no']} "
                                  "غير موجود في الموارد البشرية بعد؛ حمّل الموظفين أولًا."))
    return employee


def _eosb(entity, book, user, result, *, cutover):
    from payroll import services as payroll

    for r in book.records("employees", entity.code):
        if r["eosb_provision"] is None:
            continue
        employee = _employee_of(entity, r, user, result, "employees", r.row)
        if employee is None:
            continue
        ok, outcome = _call(result, "employees", r.row, lambda employee=employee, r=r:
                            payroll.record_opening_eosb_provision(employee, user=user, amount=r["eosb_provision"],
                                                                 on=cutover))
        if ok:
            _count(result, outcome)


def _leave(entity, book, user, result, *, cutover):
    from hr import services as hr

    for r in book.records("employees", entity.code):
        if r["leave_balance_days"] is None:
            continue
        employee = _employee_of(entity, r, user, result, "employees", r.row)
        if employee is None:
            continue
        ok, existing = _call(result, "employees", r.row, lambda employee=employee: hr.opening_leave_adjustment(
            employee, ANNUAL_LEAVE, user=user))
        if not ok:
            continue
        if existing is not None:
            if existing.days != r["leave_balance_days"] or existing.date != cutover:
                result.problems.append(bi(f"{_where('employees', r.row)}: an opening leave balance with other values is "
                                          "already recorded.", f"{_where('employees', r.row)}: سُجّل رصيد إجازة افتتاحي "
                                          "بقيم أخرى سابقًا."))
            else:
                result.skipped += 1
            continue
        ok, adjustment = _call(result, "employees", r.row, lambda employee=employee, r=r:
                               hr.record_opening_leave_balance(employee, ANNUAL_LEAVE, r["leave_balance_days"], cutover,
                                                               user=user))
        if ok:
            _count(result, (adjustment, True))


def _bai_ajel(entity, book, user, result, *, cutover):
    from payroll import services as payroll

    employees = {r["employee_no"]: r for r in book.records("employees", entity.code) if r["employee_no"]}
    for r in book.records("bai_ajel_contracts", entity.code):
        person = employees.get(r["employee_no"])
        if person is None:
            result.problems.append(bi(f"{_where('bai_ajel_contracts', r.row)}: employee {r['employee_no']} is not on the "
                                      "Employees sheet.", f"{_where('bai_ajel_contracts', r.row)}: الموظف "
                                      f"{r['employee_no']} غير موجود في ورقة الموظفين."))
            continue
        employee = _employee_of(entity, person, user, result, "bai_ajel_contracts", r.row)
        if employee is None:
            continue
        known = payroll.migrated_deferred_sale(entity, r["contract_no"]) is not None
        ok, contract = _call(result, "bai_ajel_contracts", r.row, lambda r=r, employee=employee:
                             payroll.load_migrated_deferred_sale(
                                 employee, user=user, legacy_number=r["contract_no"],
                                 asset_description_ar=r["asset_description_ar"],
                                 asset_description_en=r["asset_description_en"] or "", sale_date=r["sale_date"],
                                 sale_price=r["sale_price"], profit_amount=r["profit_amount"],
                                 vat_amount=r["vat_amount"] if r["vat_amount"] is not None else Decimal("0"),
                                 instalments=r["instalments"], paid_to_date=r["paid_to_date"],
                                 next_period=r["next_instalment_period"], cutover=cutover,
                                 consent_ref=r["consent_ref"] or ""))
        if ok:
            _count(result, (contract, contract is not None and not known))


# --- fixed assets (fixed-assets R40, R41; W11) ------------------------------------------------------------------------

def _fixed_assets(entity, book, user, result, *, cutover, amap, source_system):
    from fixed_assets import migration_port

    if amap is None:
        result.problems.append(bi("Fixed assets need the entity's approved account map (Falcon code → ERP GL).",
                                  "تحتاج الأصول الثابتة إلى خريطة الحسابات المعتمدة للكيان."))
        return
    rows = {r.legacy_code: r for r in amap.rows.select_related("account")}
    for r in book.records("fixed_assets", entity.code):
        mapped = rows.get(r["cost_code"])
        if mapped is None:
            result.problems.append(bi(f"{_where('fixed_assets', r.row)}: Falcon {r['cost_code']} is not in the approved "
                                      "account map.", f"{_where('fixed_assets', r.row)}: الكود {r['cost_code']} غير "
                                      "موجود في خريطة الحسابات المعتمدة."))
            continue
        outcome = _attempt(result, "fixed_assets", r.row, lambda r=r, account=mapped.account:
                           migration_port.load_opening_asset(
                               entity, user=user, legacy_code=r["asset_code"], description_ar=r["description_ar"],
                               description_en=r["description_en"] or "", asset_class=r["asset_class"],
                               usage=r["usage"], location=r["location"], cost_account=account,
                               acquisition_date=r["acquisition_date"],
                               available_for_use_date=r["available_for_use_date"], cost=r["cost"],
                               accumulated_depreciation=r["accumulated_depreciation"],
                               residual_value=r["residual_value"], method=r["method"],
                               useful_life_months=r["useful_life_months"],
                               remaining_life_months=r["remaining_life_months"],
                               serial_or_plate=r["serial_or_plate"] or "",
                               custodian_ref=r["custodian_employee_no"] or "", cutover=cutover,
                               source_system=source_system))
        if outcome is not None:
            _count(result, outcome)


# --- the run ---------------------------------------------------------------------------------------------------------

def _opening_posted(entity, *, lock=False):
    """The entity's opening entry is posted (or reversed). With ``lock`` its rows are locked first (inside the load's
    transaction), so the posting of the opening entry and this load never interleave (R22, R25)."""
    from ledger import services as ledger

    return ledger.migration_opening_posted(entity, lock=lock)


def check_entity(entity, book):
    """The workbook check (W4 to W17) of ``entity``'s rows against its ERP chart and Sharia flag."""
    return workbook_checks.check_workbook(book, charts={entity.code: workbook_checks.entity_chart(entity)},
                                          entities=[entity.code], erp_entities={entity.code: entity.sharia_enforced})


def load_subledgers(entity, book, sections, *, user, apply=False, source_system="falcon"):
    """Load ``sections`` (see the module doc) of ``entity`` from the parsed workbook ``book``. Returns a
    :class:`SubledgerPlan`; ``applied`` is True only when everything was loaded and kept."""
    from .services import _approved_map

    unknown = set(sections) - set(SECTIONS)
    if unknown or not sections:
        raise ValueError(f"Sections are {', '.join(SECTIONS)}. / الأقسام: {', '.join(SECTIONS)}.")
    from core import access
    from core.access.models import Kind
    from core.access.services import kind_of

    if kind_of(user) != Kind.HUMAN:  # R19: legacy data is loaded by people only, never agents or system users
        access.deny(user, entity, "migration.import", "agent_forbidden")
    plan = SubledgerPlan(entity.code, check_entity(entity, book))
    record = book.entity(entity.code)
    cutover = record["cutover_date"] if record is not None else None
    ordered = [s for s in SECTIONS if s in sections]
    for name in ordered:
        plan.sections[name] = SectionResult()
    if plan.report.errors or cutover is None:
        return plan
    try:
        with transaction.atomic():
            posted = _opening_posted(entity, lock=True)  # R25: locked before anything is loaded
            for name in ordered:
                result = plan.sections[name]
                if name in GATED and posted:
                    result.problems.append(bi(
                        f"{name}: the opening entry of {entity.code} is posted; its sub-ledgers were tied out to it "
                        "(R22) and are not loaded again.", f"{name}: القيد الافتتاحي للكيان {entity.code} مرحّل؛ وقد "
                        "طوبقت سجلاته الفرعية معه ولا تُحمَّل مرة أخرى."))
                    continue
                if name in ("receivables", "payables"):
                    _open_items(entity, book, user, result, receivable=name == "receivables", cutover=cutover,
                                source_system=source_system)
                elif name == "banks":
                    _banks(entity, book, user, result, cutover=cutover, amap=_approved_map(entity, source_system))
                elif name == "employees":
                    _employees(entity, book, user, result)
                elif name == "eosb":
                    _eosb(entity, book, user, result, cutover=cutover)
                elif name == "leave":
                    _leave(entity, book, user, result, cutover=cutover)
                elif name == "fixed_assets":
                    _fixed_assets(entity, book, user, result, cutover=cutover,
                                  amap=_approved_map(entity, source_system), source_system=source_system)
                else:
                    _bai_ajel(entity, book, user, result, cutover=cutover)
            if not apply or plan.problems:
                raise _Rollback
    except _Rollback:
        return plan
    plan.applied = True
    return plan


def render(plan):
    """Text lines for the console (codes and counts; no personal value)."""
    out = workbook_checks.render(plan.report)
    for name, result in plan.sections.items():
        out.append(f"{name}: created / أُنشئ {result.created}, skipped (loaded before) / تُرك {result.skipped}, "
                   f"refused / مرفوض {len(result.problems)}")
        out += [f"  [refused] {p}" for p in result.problems[:200]]
        out += [f"  [warning] {w}" for w in result.warnings[:200]]
    return out
