"""Checks of a filled migration workbook (migration.md §11, W4 to W11). Read-only: nothing is written anywhere.

``check_workbook`` takes a parsed workbook (``workbook.read_workbook``) and, per entity, the ERP chart to map to
(``entity_chart`` from the database, or the group template). It returns every problem with its sheet, row and
amount, and the sub-ledger tie-outs (open items, banks, end of service, fixed assets) against the trial balance.
Messages never repeat a personal-data value (W3).
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from banking import iban as iban_rules

from . import workbook_spec as spec
from .readers import ZERO
from .services import CLASS_TYPES, OPENING_EQUITY
from .workbook import _ENTITY_CODE as ENTITY_CODE
from .workbook import problem

AR_CONTROL, AR_GROUP, AP_CONTROL, AP_GROUP = "1200", "1210", "2100", "2110"
BANK_PARENT, PURIFICATION, EOSB = "1100", "1140", "2500"
ASSET_COST_CODES, ACCUMULATED_DEPRECIATION = frozenset({"1500", "1510", "1520"}), "1590"
BAI_AJEL_RECEIVABLE, BAI_AJEL_DEFERRED, BAI_AJEL_INCOME = "1240", "1241", "4610"
AR_SIGNS = {"invoice": 1, "debit_note": 1, "credit_note": -1, "advance": -1}
AP_SIGNS = {"bill": 1, "vendor_credit_note": -1, "advance": -1}
NAMES = {s.key: (s.name, s.title_ar) for s in spec.SHEETS}


@dataclass(frozen=True)
class ErpAccount:
    code: str
    type: str
    postable: bool
    intercompany: bool = False
    prohibited: bool = False
    parent: str = ""


def template_chart():
    """The group chart template (``ledger/coa_template.py``) as {code: ErpAccount}."""
    from ledger.coa_template import TEMPLATE

    headers = {kind: code for code, _en, _ar, kind, flags in TEMPLATE if flags.get("header")}
    return {code: ErpAccount(code, kind, not flags.get("header") and not flags.get("report_only"),
                             bool(flags.get("intercompany")), bool(flags.get("prohibited")),
                             flags.get("parent") or ("" if flags.get("header") else headers.get(kind, "")))
            for code, _en, _ar, kind, flags in TEMPLATE}


def entity_chart(entity):
    """An entity's ERP chart from the database as {code: ErpAccount}. The caller checks the user's right."""
    from ledger.models import Account

    return {a.code: ErpAccount(a.code, a.type, a.is_postable and a.is_active, a.is_intercompany, a.sharia_prohibited,
                               a.parent.code if a.parent_id else "")
            for a in Account.objects.filter(entity=entity).select_related("parent")}


@dataclass
class WorkbookReport:
    name: str
    sha256: str
    problems: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)  # entity -> {key: value}
    ties: list = field(default_factory=list)  # {entity, check, account, subledger, ledger, difference}
    chart_source: dict = field(default_factory=dict)  # entity -> "erp" | "template"

    @property
    def errors(self):
        return [p for p in self.problems if p.severity == "error"]

    @property
    def warnings(self):
        return [p for p in self.problems if p.severity == "warning"]

    @property
    def ok(self):
        return not self.errors


def _decimals(amount):
    return max(0, -amount.normalize().as_tuple().exponent)


class _Checker:
    def __init__(self, book, charts, only):
        self.book = book
        self.report = WorkbookReport(book.name, book.sha256, problems=list(book.problems))
        self.codes = book.entity_codes()
        self.only = set(only or self.codes)
        self.charts = charts or {}

    # helpers ------------------------------------------------------------------------------------------------------

    def add(self, severity, code, en, ar, *, sheet="", row=None, entity="", account_code=""):
        name = NAMES.get(sheet, (sheet, sheet))
        prefix_en = f"{name[0]} row {row}: " if row else (f"{name[0]}: " if sheet else "")
        prefix_ar = f"{name[1]} الصف {row}: " if row else (f"{name[1]}: " if sheet else "")
        self.report.problems.append(problem(severity, code, prefix_en + en, prefix_ar + ar, sheet=name[0], row=row,
                                            entity=entity, account_code=account_code))

    def error(self, *a, **kw):
        self.add("error", *a, **kw)

    def warning(self, *a, **kw):
        self.add("warning", *a, **kw)

    def chart(self, entity):
        if entity not in self.charts:
            self.charts[entity] = template_chart()
            self.report.chart_source.setdefault(entity, "template")
        else:
            self.report.chart_source.setdefault(entity, "erp")
        return self.charts[entity]

    def tie(self, entity, check, account, subledger, ledger, *, sheet, en, ar):
        difference = subledger - ledger
        self.report.ties.append({"entity": entity, "check": check, "account": account, "subledger": subledger,
                                 "ledger": ledger, "difference": difference})
        if difference:
            self.error("tie_out", f"{en}: sheet {subledger}, trial balance {ledger}, difference {difference}.",
                       f"{ar}: الورقة {subledger}، ميزان المراجعة {ledger}، الفرق {difference}.", sheet=sheet,
                       entity=entity, account_code=account)

    # row checks (W4) ----------------------------------------------------------------------------------------------

    def rows(self):
        for sheet in spec.SHEETS:
            data = self.book.sheets.get(sheet.key)
            if data is None or not data.present:
                continue
            entity_key = "entity_code" if sheet.key == "entities" else "entity"
            records = [r for r in data.records if sheet.key == "entities" or r["entity"] in self.only
                       or r["entity"] not in self.codes]
            for r in records:
                for column in sheet.columns:
                    if column.required and r[column.key] is None and column.key not in r.invalid:
                        self.error("required", f"{column.en} is required.", f"{column.ar} مطلوب.", sheet=sheet.key,
                                   row=r.row, entity=r[entity_key] or "")
                    if column.kind == "entity" and r[column.key] and r[column.key] not in self.codes:
                        self.error("unknown_entity", f"{column.en} {r[column.key]} is not on the Entities sheet.",
                                   f"{column.ar} {r[column.key]} غير موجود في ورقة الكيانات.", sheet=sheet.key,
                                   row=r.row, entity=r[entity_key] or "")
            for keys in sheet.unique:
                seen = Counter(tuple(r[k] for k in keys) for r in records if all(r[k] is not None for k in keys))
                for value, n in seen.items():
                    if n > 1:
                        shown = " / ".join(str(v) for k, v in zip(keys, value) if not sheet.column(k).personal)
                        first = next(r for r in records if tuple(r[k] for k in keys) == value)
                        self.error("duplicate", f"{shown} appears {n} times.", f"{shown} مكرر {n} مرات.",
                                   sheet=sheet.key, row=first.row, entity=first[entity_key] or "")

    # W5 -----------------------------------------------------------------------------------------------------------

    def entities(self):
        records = self.book.records("entities")
        if not records:
            self.error("no_entities", "The Entities sheet has no entity.", "لا يوجد كيان في ورقة الكيانات.",
                       sheet="entities")
        for r in records:
            code = r["entity_code"]
            if code and not ENTITY_CODE.match(code):
                self.error("bad_entity_code", f"Entity code {code} is not 2 to 8 capitals or digits.",
                           f"رمز الكيان {code} ليس من 2 إلى 8 أحرف كبيرة أو أرقام.", sheet="entities", row=r.row)
            currency = r["functional_currency"]
            if currency and spec.CURRENCIES.get(currency) != 2:
                self.error("functional_currency", f"Functional currency {currency} must have 2 decimals.",
                           f"العملة الوظيفية {currency} يجب أن تكون بخانتين عشريتين.", sheet="entities", row=r.row,
                           entity=code or "")
            elif currency and currency != "SAR":
                self.warning("functional_currency", f"Functional currency {currency}: the workbook's amounts are "
                             "taken as that currency.", f"العملة الوظيفية {currency}: تُعد مبالغ الملف بهذه العملة.",
                             sheet="entities", row=r.row, entity=code or "")
            if r["parent_entity"] and r["parent_entity"] == code:
                self.error("parent_self", "An entity cannot be its own parent.", "لا يكون الكيان أمًّا لنفسه.",
                           sheet="entities", row=r.row, entity=code or "")

    # W6, W7 -------------------------------------------------------------------------------------------------------

    def ledger(self, entity):
        """Chart, TB and mapping checks of one entity. Returns (TB net by code, map by code)."""
        chart_rows = {r["code"]: r for r in self.book.records("falcon_chart", entity) if r["code"]}
        for r in chart_rows.values():
            allowed = CLASS_TYPES.get(r["code"][:1])
            if r["type"] and allowed is not None and r["type"] not in allowed:
                self.error("class_mismatch", f"Code {r['code']} is class {r['code'][0]} but type {r['type']}.",
                           f"الكود {r['code']} من الفئة {r['code'][0]} ونوعه {r['type']}.", sheet="falcon_chart",
                           row=r.row, entity=entity, account_code=r["code"])
            elif allowed is None:
                self.warning("class_unknown", f"Code {r['code']}: first digit {r['code'][0]} has no class rule.",
                             f"الكود {r['code']}: لا توجد قاعدة للفئة {r['code'][0]}.", sheet="falcon_chart",
                             row=r.row, entity=entity, account_code=r["code"])
            if r["parent"] and r["parent"] not in chart_rows:
                self.warning("parent_missing", f"Parent {r['parent']} of {r['code']} is not in the chart.",
                             f"الحساب الرئيسي {r['parent']} للكود {r['code']} غير موجود في الدليل.",
                             sheet="falcon_chart", row=r.row, entity=entity, account_code=r["code"])
        tb_rows = [r for r in self.book.records("trial_balance", entity) if r["code"]]
        if not tb_rows:
            self.warning("no_trial_balance", "No trial balance rows for this entity.", "لا توجد أرصدة لهذا الكيان.",
                         sheet="trial_balance", entity=entity)
        if not chart_rows:
            self.warning("no_chart", "No Falcon chart rows: trial balance codes are not checked against a chart.",
                         "لا يوجد دليل حسابات: لم تُفحص أكواد الميزان مقابل الدليل.", sheet="falcon_chart", entity=entity)
        net, debit, credit = {}, ZERO, ZERO
        codes = {r["code"] for r in tb_rows}
        for r in tb_rows:
            d, c = r.get("debit", ZERO), r.get("credit", ZERO)
            if d and c:
                self.error("both_sides", f"Code {r['code']} has a debit and a credit; enter one balance.",
                           f"للكود {r['code']} رصيد مدين ودائن؛ أدخل رصيدًا واحدًا.", sheet="trial_balance", row=r.row,
                           entity=entity, account_code=r["code"])
            debit, credit = debit + d, credit + c
            net[r["code"]] = net.get(r["code"], ZERO) + d - c
            if chart_rows and r["code"] not in chart_rows:
                self.error("not_in_chart", f"Code {r['code']} is not in the Falcon chart.",
                           f"الكود {r['code']} غير موجود في دليل حسابات فالكون.", sheet="trial_balance", row=r.row,
                           entity=entity, account_code=r["code"])
            elif r["code"] in chart_rows and chart_rows[r["code"]]["postable"] is False:
                self.error("not_postable", f"Code {r['code']} is a header account in the chart; give postable "
                           "accounts only.", f"الكود {r['code']} حساب رئيسي في الدليل؛ أدخل الحسابات القابلة للترحيل فقط.",
                           sheet="trial_balance", row=r.row, entity=entity, account_code=r["code"])
            if any(other != r["code"] and other.startswith(r["code"]) for other in codes):
                self.error("tb_header", f"Code {r['code']} is the start of another code in the trial balance (a "
                           "header): give postable accounts only.", f"الكود {r['code']} بداية كود آخر في الميزان (حساب "
                           "رئيسي): أدخل الحسابات القابلة للترحيل فقط.", sheet="trial_balance", row=r.row,
                           entity=entity, account_code=r["code"])
        if debit != credit:
            self.error("tb_unbalanced", f"Trial balance does not balance: debit {debit}, credit {credit}, difference "
                       f"{debit - credit}.", f"ميزان المراجعة غير متوازن: المدين {debit}، الدائن {credit}، الفرق "
                       f"{debit - credit}.", sheet="trial_balance", entity=entity)
        self.report.summary.setdefault(entity, {}).update(
            {"tb_rows": len(tb_rows), "tb_debit": debit, "tb_credit": credit, "chart_rows": len(chart_rows)})
        mapping = self.mapping(entity, chart_rows, net)
        return net, mapping

    def mapping(self, entity, chart_rows, net):
        erp = self.chart(entity)
        record = self.book.entity(entity)
        sharia = bool(record and record["sharia_enforced"])
        rows = {r["legacy_code"]: r for r in self.book.records("account_map", entity) if r["legacy_code"]}
        for code, amount in sorted(net.items()):
            if amount and code not in rows:
                self.error("unmapped", f"Code {code} has a balance of {amount} and no ERP account.",
                           f"الكود {code} له رصيد {amount} وليس له حساب في النظام.", sheet="account_map",
                           entity=entity, account_code=code)
        missing = sorted(c for c, r in chart_rows.items() if r["postable"] and c not in rows and not net.get(c))
        if missing:
            self.warning("chart_unmapped", f"{len(missing)} postable Falcon account(s) without an ERP account (needed "
                         f"for replayed journals): {', '.join(missing[:20])}.", f"{len(missing)} حساب قابل للترحيل بلا "
                         f"حساب في النظام (يلزم لإعادة ترحيل القيود): {', '.join(missing[:20])}.", sheet="account_map",
                         entity=entity)
        out = {}
        for code, r in rows.items():
            where = dict(sheet="account_map", row=r.row, entity=entity, account_code=code)
            balance = net.get(code, ZERO)
            if chart_rows and code not in chart_rows:
                self.warning("map_not_in_chart", f"Code {code} is not in the Falcon chart.",
                             f"الكود {code} غير موجود في دليل حسابات فالكون.", **where)
            account = erp.get(r["erp_code"]) if r["erp_code"] else None
            if r["erp_code"] and account is None:
                self.error("unknown_account", f"ERP account {r['erp_code']} does not exist in {entity}'s chart.",
                           f"الحساب {r['erp_code']} غير موجود في دليل حسابات {entity}.", **where)
                continue
            if account is None:
                continue
            out[code] = (account.code, r["counterparty"] or None)
            if not account.postable:
                self.error("not_postable", f"ERP account {account.code} is not open for posting (header or "
                           "report-only).", f"الحساب {account.code} غير مفتوح للترحيل (رئيسي أو للتقارير فقط).", **where)
            allowed = CLASS_TYPES.get(code[:1])
            if allowed is not None and account.type not in allowed:
                self.error("class_mismatch", f"Code {code} is class {code[0]}; ERP account {account.code} is "
                           f"{account.type}.", f"الكود {code} من الفئة {code[0]}؛ والحساب {account.code} من نوع "
                           f"{account.type}.", **where)
            cp = r["counterparty"]
            if account.intercompany and not cp:
                self.error("counterparty_missing", f"ERP account {account.code} is intercompany: give the counterparty "
                           "entity.", f"الحساب {account.code} بين شركات المجموعة: أدخل الكيان المقابل.", **where)
            if cp and cp == entity:
                self.error("counterparty_self", "The counterparty cannot be the entity itself.",
                           "لا يكون الكيان المقابل هو الكيان نفسه.", **where)
            if cp and not account.intercompany:
                self.error("counterparty_not_intercompany", f"ERP account {account.code} is not intercompany; remove the "
                           "counterparty.", f"الحساب {account.code} ليس بين شركات المجموعة؛ احذف الكيان المقابل.", **where)
            if account.prohibited and sharia:
                (self.error if balance else self.warning)(
                    "sharia_prohibited", f"ERP account {account.code} is Sharia-prohibited in {entity} (balance "
                    f"{balance}): a human decision is needed (OQ-10).", f"الحساب {account.code} محظور شرعًا في {entity} "
                    f"(الرصيد {balance}): يلزم قرار بشري.", **where)
            if account.code == OPENING_EQUITY:
                self.warning("opening_equity", f"Code {code} is mapped to {OPENING_EQUITY}, the balancing account of "
                             "the opening entry.", f"الكود {code} مربوط بالحساب {OPENING_EQUITY}، حساب موازنة القيد "
                             "الافتتاحي.", **where)
        self.report.summary[entity]["map_rows"] = len(rows)
        return out

    @staticmethod
    def on(net, mapping, erp_codes, counterparty=None, any_counterparty=False):
        return sum((amount for code, amount in net.items() if code in mapping and mapping[code][0] in erp_codes
                    and (any_counterparty or mapping[code][1] == counterparty)), ZERO)

    # W8 -----------------------------------------------------------------------------------------------------------

    def open_items(self, entity, net, mapping, *, key, party_sheet, party_key, signs, control, group, credit):
        cutover = self.book.entity(entity)["cutover_date"] if self.book.entity(entity) else None
        parties = {r["code"]: r for r in self.book.records(party_sheet, entity) if r["code"]}
        totals = defaultdict(lambda: ZERO)
        items = self.book.records(key, entity)
        for r in items:
            where = dict(sheet=key, row=r.row, entity=entity)
            party = parties.get(r[party_key])
            if r[party_key] and party is None:
                self.error("unknown_party", f"{r[party_key]} is not on the {NAMES[party_sheet][0]} sheet of {entity}.",
                           f"{r[party_key]} غير موجود في ورقة {NAMES[party_sheet][1]} للكيان {entity}.", **where)
            if cutover and r["document_date"] and r["document_date"] > cutover:
                self.error("after_cutover", f"Document date {r['document_date']} is after the cut-over {cutover}.",
                           f"تاريخ المستند {r['document_date']} بعد تاريخ التحويل {cutover}.", **where)
            if r["due_date"] and r["document_date"] and r["due_date"] < r["document_date"]:
                self.error("due_before_date", "The due date is before the document date.",
                           "تاريخ الاستحقاق قبل تاريخ المستند.", **where)
            self.currency_amount(r, "amount", where)
            if r["currency"] == "SAR" and r["amount"] is not None and r["sar_amount"] is not None \
                    and r["amount"] != r["sar_amount"]:
                self.error("sar_mismatch", f"A SAR document: amount {r['amount']} ≠ SAR amount {r['sar_amount']}.",
                           f"مستند بالريال: المبلغ {r['amount']} ≠ المبلغ بالريال {r['sar_amount']}.", **where)
            if r["sar_amount"] is not None and r["sar_amount"] == 0:
                self.warning("zero_item", "An open item of 0.00: leave it out.", "رصيد مفتوح بصفر: احذفه.", **where)
            if r["sar_amount"] is None or r["document_type"] not in signs:
                continue
            cp = party["group_entity"] if party is not None else None
            totals[cp] += signs[r["document_type"]] * r["sar_amount"]
        sign = -1 if credit else 1
        label = NAMES[key]
        counterparties = set(totals) | {cp for code, (erp, cp) in mapping.items() if erp == group and net.get(code)}
        for cp in sorted(counterparties, key=lambda c: c or ""):
            erp_code = group if cp else control
            ledger = sign * self.on(net, mapping, {erp_code}, cp)
            if cp is None and not totals[None] and not ledger:
                continue
            self.tie(entity, key, erp_code + (f"/{cp}" if cp else ""), totals[cp], ledger, sheet=key,
                     en=f"{label[0]} vs ERP {erp_code}{f' ({cp})' if cp else ''}",
                     ar=f"{label[1]} مقابل الحساب {erp_code}{f' ({cp})' if cp else ''}")
        if None not in counterparties:
            ledger = sign * self.on(net, mapping, {control})
            if ledger:
                self.tie(entity, key, control, ZERO, ledger, sheet=key, en=f"{label[0]} vs ERP {control}",
                         ar=f"{label[1]} مقابل الحساب {control}")
        self.report.summary[entity][f"{key}_rows"] = len(items)

    def currency_amount(self, r, key, where):
        if r[key] is None or not r["currency"]:
            return
        minor = spec.CURRENCIES.get(r["currency"])
        if minor is not None and _decimals(r[key]) > minor:
            self.error("currency_decimals", f"{r[key]} has more decimals than {r['currency']} allows ({minor}).",
                       f"{r[key]} بخانات عشرية أكثر مما تسمح به العملة {r['currency']} ({minor}).", **where)

    def parties(self, entity):
        for key in ("customers", "vendors"):
            for r in self.book.records(key, entity):
                where = dict(sheet=key, row=r.row, entity=entity)
                if r["group_entity"] and r["group_entity"] == entity:
                    self.error("group_self", "The group entity cannot be the entity itself.",
                               "لا يكون كيان المجموعة هو الكيان نفسه.", **where)
                if r["cr_number"] and len(r["cr_number"]) != 10:
                    self.warning("cr_length", "A Saudi commercial registration number has 10 digits.",
                                 "رقم السجل التجاري السعودي من 10 أرقام.", **where)
                if key == "vendors":
                    if r["resident"] is False and not r["residence_country"]:
                        self.error("residence_country", "A non-resident vendor needs its country of residence.",
                                   "المورد غير المقيم يحتاج إلى دولة الإقامة.", **where)
                    if r["resident"] is False and not r["wht_category"]:
                        self.warning("wht_category", "A non-resident vendor needs a withholding tax category before "
                                     "it is paid.", "المورد غير المقيم يحتاج إلى فئة ضريبة الاستقطاع قبل الدفع له.", **where)
                    if r["iban"]:
                        self.iban(r["iban"], r["iban"][:2], where)

    def iban(self, value, country, where, bank_code=None):
        for message in iban_rules.problems(value, country=country, bank_code=bank_code):
            en, _, ar = message.partition(" / ")
            severity = "warning" if bank_code and "Positions 5-6" in en else "error"
            self.add(severity, "iban", en, ar or en, **where)

    # W9 -----------------------------------------------------------------------------------------------------------

    def banks(self, entity, net, mapping):
        erp = self.chart(entity)
        by_code = defaultdict(lambda: ZERO)
        purification = []
        records = self.book.records("bank_accounts", entity)
        for r in records:
            where = dict(sheet="bank_accounts", row=r.row, entity=entity)
            bank = spec.BANKS_V1.get(r["bank"])
            if r["bank"] == "other" and not r["bank_name_other"]:
                self.error("bank_name", "Give the bank's name.", "أدخل اسم البنك.", **where)
            if r["iban"]:
                country = bank[2] if bank else r["iban"][:2]
                self.iban(r["iban"], country, where, bank_code=bank[3] if bank else None)
            elif not r["account_number"]:
                self.error("iban_or_number", "Give the IBAN or, where there is none, the account number.",
                           "أدخل الآيبان أو رقم الحساب عند عدم وجود آيبان.", **where)
            self.currency_amount(r, "book_balance", where)
            self.currency_amount(r, "statement_balance", where)
            if r["currency"] == "SAR" and None not in (r["book_balance"], r["book_balance_sar"]) \
                    and r["book_balance"] != r["book_balance_sar"]:
                self.error("sar_mismatch", f"A SAR account: book balance {r['book_balance']} ≠ SAR {r['book_balance_sar']}.",
                           f"حساب بالريال: الرصيد {r['book_balance']} ≠ بالريال {r['book_balance_sar']}.", **where)
            if r["statement_balance"] is None:
                self.warning("no_statement_balance", "No statement balance: the first reconciliation needs it.",
                             "لا يوجد رصيد كشف: تحتاجه أول تسوية بنكية.", **where)
            if r["falcon_code"]:
                by_code[r["falcon_code"]] += r.get("book_balance_sar", ZERO)
                target = mapping.get(r["falcon_code"], (None, None))[0]
                account = erp.get(target) if target else None
                if account is not None and BANK_PARENT not in (account.code, account.parent):
                    self.warning("bank_gl", f"Falcon {r['falcon_code']} maps to {account.code}, not a bank account "
                                 f"({BANK_PARENT} or under it).", f"الكود {r['falcon_code']} مربوط بالحساب {account.code} "
                                 f"وليس حساب بنك ({BANK_PARENT} أو ما تحته).", **where)
            if r["is_purification"]:
                purification.append(r)
                if r["currency"] and r["currency"] != "SAR":
                    self.error("purification_currency", "The purification account is in SAR.",
                               "حساب التطهير بالريال.", **where)
                if r["falcon_code"] and mapping.get(r["falcon_code"], (None,))[0] != PURIFICATION:
                    self.warning("purification_gl", f"The purification account's Falcon code should map to "
                                 f"{PURIFICATION}.", f"يجب ربط كود حساب التطهير بالحساب {PURIFICATION}.", **where)
        if len(purification) > 1:
            self.error("purification_twice", "More than one purification account.", "أكثر من حساب تطهير.",
                       sheet="bank_accounts", row=purification[1].row, entity=entity)
        for code in sorted(by_code):
            self.tie(entity, "bank_accounts", code, by_code[code], net.get(code, ZERO), sheet="bank_accounts",
                     en=f"Bank accounts on Falcon {code}", ar=f"الحسابات البنكية على الكود {code}")
        for code, (erp_code, _cp) in sorted(mapping.items()):
            account = erp.get(erp_code)
            if account is not None and account.parent == BANK_PARENT and net.get(code) and code not in by_code:
                self.warning("bank_missing", f"Falcon {code} ({erp_code}) has a balance and no bank account row.",
                             f"للكود {code} ({erp_code}) رصيد وليس له حساب بنكي في الورقة.", sheet="bank_accounts",
                             entity=entity, account_code=code)
        self.report.summary[entity]["bank_accounts_rows"] = len(records)

    # W10 ----------------------------------------------------------------------------------------------------------

    def employees(self, entity, net, mapping):
        records = self.book.records("employees", entity)
        ids = Counter(r["id_number"] for r in records if r["id_number"])
        for r in records:
            where = dict(sheet="employees", row=r.row, entity=entity)
            if r["id_number"] and ids[r["id_number"]] > 1:
                self.error("duplicate_id", "The same ID number is on another row.", "رقم الهوية نفسه في صف آخر.", **where)
            if r["nationality"] == "SA" and r["id_type"] and r["id_type"] != "national_id":
                self.warning("id_type", "A Saudi employee normally has a national ID.",
                             "الموظف السعودي عادة يحمل هوية وطنية.", **where)
            if r["nationality"] == "SA" and not r["gosi_cohort"]:
                self.warning("gosi_cohort", "A Saudi employee needs the GOSI cohort before payroll.",
                             "الموظف السعودي يحتاج إلى فئة التأمينات قبل الرواتب.", **where)
            if r["date_of_birth"] and r["hire_date"] and r["date_of_birth"] >= r["hire_date"]:
                self.error("dates", "The date of birth is not before the hire date.",
                           "تاريخ الميلاد ليس قبل تاريخ التعيين.", **where)
            if r["iban"]:
                self.iban(r["iban"], r["iban"][:2], where)
        ledger = -self.on(net, mapping, {EOSB}, any_counterparty=True)
        given = [r["eosb_provision"] for r in records if r["eosb_provision"] is not None]
        if given:
            self.tie(entity, "employees", EOSB, sum(given, ZERO), ledger, sheet="employees",
                     en=f"End-of-service provisions vs ERP {EOSB}", ar=f"مخصصات نهاية الخدمة مقابل الحساب {EOSB}")
        elif ledger:
            self.warning("eosb_not_given", f"The trial balance has {ledger} on {EOSB} and no employee provision is "
                         "given.", f"في الميزان {ledger} على الحساب {EOSB} ولا توجد مخصصات للموظفين.", sheet="employees",
                         entity=entity)
        self.report.summary[entity]["employees_rows"] = len(records)

    # W11 ----------------------------------------------------------------------------------------------------------

    def fixed_assets(self, entity, net, mapping):
        cutover = self.book.entity(entity)["cutover_date"] if self.book.entity(entity) else None
        cost, accum = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
        records = self.book.records("fixed_assets", entity)
        for r in records:
            where = dict(sheet="fixed_assets", row=r.row, entity=entity)
            c, a = r.get("cost", ZERO), r.get("accumulated_depreciation", ZERO)
            if a > c:
                self.error("accumulated_above_cost", f"Accumulated depreciation {a} is above the cost {c}.",
                           f"مجمع الاستهلاك {a} أكبر من التكلفة {c}.", **where)
            if a and r["asset_class"] in spec.NON_DEPRECIABLE_CLASSES:
                self.warning("not_depreciable", f"Class {r['asset_class']} is not depreciated but has {a}.",
                             f"الفئة {r['asset_class']} لا تُستهلك ولها مجمع {a}.", **where)
            if (r["asset_class"] in spec.INVESTMENT_PROPERTY_CLASSES) != (r["usage"] == "investment_property") \
                    and r["asset_class"] and r["usage"]:
                self.error("investment_property", "Investment property classes go with the investment property usage "
                           "only.", "فئات العقار الاستثماري تكون باستخدام العقار الاستثماري فقط.", **where)
            if cutover and r["acquisition_date"] and r["acquisition_date"] > cutover:
                self.error("after_cutover", "Acquired after the cut-over date.", "تم الاقتناء بعد تاريخ التحويل.", **where)
            if r["available_for_use_date"] and r["acquisition_date"] and r["available_for_use_date"] < r["acquisition_date"]:
                self.error("dates", "Available for use before it was acquired.", "جاهز للاستخدام قبل اقتنائه.", **where)
            if r["method"] == "straight_line" and not r["useful_life_months"]:
                self.error("useful_life", "Straight line needs the useful life.", "القسط الثابت يحتاج إلى العمر الإنتاجي.",
                           **where)
            if r["useful_life_months"] and r["remaining_life_months"] and r["remaining_life_months"] > r["useful_life_months"]:
                self.error("remaining_life", "The remaining life is longer than the useful life.",
                           "العمر المتبقي أطول من العمر الإنتاجي.", **where)
            if a and not r["accum_code"]:
                self.error("accum_code", "Give the Falcon accumulated depreciation code.",
                           "أدخل كود حساب مجمع الاستهلاك في فالكون.", **where)
            if r["cost_code"]:
                cost[r["cost_code"]] += c
            if r["accum_code"]:
                accum[r["accum_code"]] += a
        for code, (erp_code, _cp) in mapping.items():
            if net.get(code) and erp_code in ASSET_COST_CODES:
                cost.setdefault(code, ZERO)
            if net.get(code) and erp_code == ACCUMULATED_DEPRECIATION:
                accum.setdefault(code, ZERO)
        for code in sorted(cost):
            self.tie(entity, "fixed_assets_cost", code, cost[code], net.get(code, ZERO), sheet="fixed_assets",
                     en=f"Asset cost on Falcon {code}", ar=f"تكلفة الأصول على الكود {code}")
        for code in sorted(accum):
            self.tie(entity, "fixed_assets_accumulated", code, accum[code], -net.get(code, ZERO), sheet="fixed_assets",
                     en=f"Accumulated depreciation on Falcon {code}", ar=f"مجمع الاستهلاك على الكود {code}")
        self.report.summary[entity]["fixed_assets_rows"] = len(records)

    # bai' ajel (payroll.md slice 2 item 3, invoicing R127) ---------------------------------------------------------

    def bai_ajel(self, entity, net, mapping):
        """1240 / 1241 / 4610 load through the opening entry (the migration allowance on restricted accounts), but the
        workbook has no sheet for the contracts behind them: the balance is listed for the CFO's decision (warning),
        and a deferred profit (1241, contra 1240) that is a debit or larger than the receivable is an error."""
        receivable = self.on(net, mapping, {BAI_AJEL_RECEIVABLE}, any_counterparty=True)
        deferred = self.on(net, mapping, {BAI_AJEL_DEFERRED}, any_counterparty=True)
        income = self.on(net, mapping, {BAI_AJEL_INCOME}, any_counterparty=True)
        if not (receivable or deferred or income):
            return
        self.report.summary[entity].update({"bai_ajel_1240": receivable, "bai_ajel_1241": deferred,
                                            "bai_ajel_4610": income})
        if receivable or deferred:
            self.warning("bai_ajel_contracts", f"Bai' ajel balances {BAI_AJEL_RECEIVABLE} {receivable}, "
                         f"{BAI_AJEL_DEFERRED} {deferred}: the workbook has no contract detail, so they are not tied to "
                         "a sub-ledger; the open contracts must be set up in invoicing before payroll collects their "
                         "instalments (CFO decision).", f"أرصدة البيع الآجل {BAI_AJEL_RECEIVABLE} {receivable}، "
                         f"{BAI_AJEL_DEFERRED} {deferred}: لا يحتوي الملف على تفاصيل العقود فلا تُطابق مع سجل فرعي؛ "
                         "ويلزم إعداد العقود القائمة في الفوترة قبل أن تحصّل الرواتب أقساطها (قرار المدير المالي).",
                         sheet="account_map", entity=entity, account_code=BAI_AJEL_RECEIVABLE)
        if deferred > 0 or -deferred > max(receivable, ZERO):
            self.error("bai_ajel_contra", f"Deferred bai' ajel profit {BAI_AJEL_DEFERRED} ({deferred}) must be a credit "
                       f"no larger than the receivable {BAI_AJEL_RECEIVABLE} ({receivable}).",
                       f"أرباح البيع الآجل المؤجلة {BAI_AJEL_DEFERRED} ({deferred}) يجب أن تكون دائنة ولا تزيد على "
                       f"الذمة {BAI_AJEL_RECEIVABLE} ({receivable}).", sheet="account_map", entity=entity,
                       account_code=BAI_AJEL_DEFERRED)

    def run(self):
        self.rows()
        self.entities()
        for entity in [c for c in self.codes if c in self.only]:
            net, mapping = self.ledger(entity)
            self.parties(entity)
            self.open_items(entity, net, mapping, key="open_receivables", party_sheet="customers", party_key="customer",
                            signs=AR_SIGNS, control=AR_CONTROL, group=AR_GROUP, credit=False)
            self.open_items(entity, net, mapping, key="open_payables", party_sheet="vendors", party_key="vendor",
                            signs=AP_SIGNS, control=AP_CONTROL, group=AP_GROUP, credit=True)
            self.banks(entity, net, mapping)
            self.employees(entity, net, mapping)
            self.fixed_assets(entity, net, mapping)
            self.bai_ajel(entity, net, mapping)
        unknown = set(self.only) - set(self.codes)
        for code in sorted(unknown):
            self.error("unknown_entity", f"Entity {code} is not on the Entities sheet.",
                       f"الكيان {code} غير موجود في ورقة الكيانات.", sheet="entities", entity=code)
        return self.report


def check_workbook(book, *, charts=None, entities=None):
    """The validation report of a parsed workbook (W4 to W11). ``charts``: {entity code: {code: ErpAccount}}; an
    entity without one is checked against the group template. ``entities``: limit the checks to these codes."""
    return _Checker(book, dict(charts or {}), entities).run()


def render(report, *, limit=300):
    """Text lines for the console."""
    out = [f"workbook {report.name or '-'} sha256={report.sha256[:12]}"]
    for entity, summary in report.summary.items():
        source = report.chart_source.get(entity, "template")
        out.append(f"  {entity} (ERP chart: {source}): " + ", ".join(f"{k}={v}" for k, v in summary.items()))
    out.append(f"errors / أخطاء: {len(report.errors)}; warnings / تنبيهات: {len(report.warnings)}")
    for p in report.problems[:limit]:
        where = " ".join(x for x in (p.entity, p.sheet, f"row={p.row}" if p.row else "",
                                     f"code={p.account_code}" if p.account_code else "") if x)
        out.append(f"  [{p.severity}] {p.code} {where} {p.message}".rstrip())
    if len(report.problems) > limit:
        out.append(f"  … {len(report.problems) - limit} more (see the CSV report) / المزيد في ملف التقرير")
    differences = [t for t in report.ties if t["difference"]]
    out.append(f"tie-outs / المطابقات: {len(report.ties)}, with differences / بفروق: {len(differences)}")
    for t in report.ties:
        out.append(f"  {t['entity']} {t['check']} {t['account']}: sheet={t['subledger']} tb={t['ledger']} "
                   f"difference={t['difference']}")
    return out


CSV_FIELDS = ["section", "severity", "code", "entity", "sheet", "row", "account_code", "check", "account", "subledger",
              "ledger", "difference", "message"]


def write_csv(report, path):
    """The report as UTF-8 CSV with BOM. Holds codes and amounts, never personal-data values (W3); keep it outside
    git unless reviewed (R20)."""
    import csv
    from pathlib import Path

    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for p in report.problems:
            writer.writerow({"section": "problem", **{k: ("" if v is None else v) for k, v in p.as_dict().items()}})
        for t in report.ties:
            writer.writerow({"section": "tie_out", **{k: str(v) if isinstance(v, Decimal) else (v or "")
                                                      for k, v in t.items()}})
    return path
