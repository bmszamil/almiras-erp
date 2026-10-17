"""Banking services (docs/specs/banking.md). The only supported way to change banking data.

Every public function checks rights with ``core.access.require`` for the entity that owns the record (R1);
records of another entity are refused (R2). Matching and reconciliation never create, change or post journal
entries (R16, R24, R41): only an approved ``BankTransaction`` posts, through ``ledger.services.create_entry``
with the document's registered line hooks (``build_lines`` / ``validate_lines``, access R34 to R36).

Sections: settings and layouts; bank accounts (R3 to R8); statement import (R9 to R15); matching (R16 to R24);
bank transactions (R25 to R35); reconciliation (R36 to R41); reports and views (R4, R46).
"""
import base64
import hashlib
import itertools
import json
import re
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connections, router, transaction
from django.db.models import Sum
from django.utils import timezone

from core import access
from core.access import services as access_services
from core.access.models import AccessLog, Kind
from core.models import Currency, Entity
from ledger import fx
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine, LedgerError

from . import iban as iban_rules
from .crypto import keyed_hash
from .models import (
    Bank,
    BankAccount,
    BankAccountChange,
    BankingError,
    BankingEvent,
    BankingSequence,
    BankingSettings,
    BankTransaction,
    CsvLayout,
    Match,
    MatchPart,
    Reconciliation,
    ReconciliationNote,
    StatementFormat,
    StatementImport,
    StatementLine,
    error,
)
from .parsers import ParseError
from .parsers import parse as parse_statement_file

ZERO = Decimal("0")
BOOKED = (JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED)
BANK_GL_ROOT = "1100"
BANK_CHARGES, ISLAMIC_PROFIT, CHARITY_PAYABLE = "5700", "4600", "2600"
# R28, R29: words by which a bank labels interest or penalties (English and Arabic). A credit so labelled is
# conventional interest (never income); a debit so labelled is a Sharia exception (OQ-7).
INTEREST_WORDS = re.compile(r"interest|penalt|usury|riba|فائدة|فوائد|الفائدة|غرامة|ربا", re.IGNORECASE)
Event = BankingEvent.Event
Kind_ = BankTransaction.Kind
TStatus = BankTransaction.Status


# --- small helpers ------------------------------------------------------------------------------------------

def _problems(problems, code):
    exc = BankingError(list(problems), code=code)
    exc.code = code
    return exc


def _from_ledger(exc, code=None):
    return _problems(exc.messages, code or getattr(exc, "code", None) or "ledger_refused")


def _json(value):
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, (Decimal, date_type)):
        return str(value)
    return value


def _source(user):
    return access_services.source_of(user) or "human"


def _event(entity, actor, event, obj=None, details=None, *, survive=False):
    """One BankingEvent (R45). ``survive``: written outside the caller's transaction when possible (refusals)."""
    actor = actor if getattr(actor, "pk", None) else None
    fields = dict(
        entity=entity, at=timezone.now(), actor=actor, actor_kind=access_services.kind_of(actor) if actor else Kind.HUMAN,
        event=event, object_type=obj._meta.label_lower if obj is not None else "",
        object_id=obj.pk if obj is not None else None, details=_json(details or {}),
        source=_source(actor) if actor else "",
    )
    main = router.db_for_write(BankingEvent)
    alias = getattr(settings, "ACCESS_LOG_DB", None)
    if survive and alias and alias in settings.DATABASES and alias != main and connections[main].in_atomic_block:
        try:
            with transaction.atomic(using=alias):
                return BankingEvent.objects.using(alias).create(**fields)
        except DatabaseError:
            pass
    return BankingEvent.objects.create(**fields)


def _agent_read(user, entity, what):
    """R43: every agent action is logged, reads included."""
    if access_services.kind_of(user) == Kind.AGENT:
        _event(entity, user, Event.AGENT_READ, details={"read": what})


def _save(obj, **kwargs):
    obj._service_write = True
    obj.save(**kwargs)
    return obj


def _currency(value):
    code = value.code if isinstance(value, Currency) else str(value or "").strip().upper()
    cur = Currency.objects.filter(code=code).first()
    if cur is None or not cur.is_active:
        raise error(f"Currency {code} is unknown or inactive.", f"العملة {code} غير معروفة أو غير نشطة.",
                    code="currency")
    if cur.minor_units > 3:
        raise error(f"Currency {code} cannot be used (more than 3 decimals).",
                    f"لا يمكن استخدام العملة {code} (أكثر من 3 خانات عشرية).", code="currency")
    return cur


def _money(value, currency, what, *, positive=True):
    """A Decimal in ``currency``'s own decimals (R42); floats are refused, never converted."""
    try:
        amount = fx.to_decimal(value, what)
        fx.check_txn_decimals(abs(amount), currency)
    except LedgerError as exc:
        raise _from_ledger(exc, "amount") from None
    if positive and amount <= ZERO:
        raise error(f"The {what} must be above zero.", f"يجب أن يكون المبلغ ({what}) أكبر من صفر.", code="amount")
    if abs(amount) >= Decimal("1e18"):
        raise error(f"The {what} is too large.", f"المبلغ ({what}) كبير جدًا.", code="amount")
    return amount


def _same_entity(entity, obj, en, ar):
    if obj is not None and getattr(obj, "entity_id", entity.pk) != entity.pk:
        raise error(f"The {en} belongs to another entity (R2).", f"{ar} يخص كيانًا آخر.", code="other_entity")


def _account(pk_or_obj):
    pk = getattr(pk_or_obj, "pk", pk_or_obj)
    return BankAccount.objects.select_related("entity", "bank", "currency", "gl_account", "csv_layout").get(pk=pk)


def _require_active(account, on=None):
    """R8: statements, documents and matches only for an active account, inside its active dates."""
    if account.status != BankAccount.Status.ACTIVE:
        raise error(f"Bank account {account} is {account.status}, not active (R8).",
                    f"الحساب البنكي {account} ليس نشطًا (R8).", code="account_not_active")
    if on is not None and (on < account.active_from or (account.active_to and on > account.active_to)):
        raise error(f"{on} is outside the active dates of bank account {account} (R8).",
                    f"التاريخ {on} خارج فترة نشاط الحساب البنكي {account} (R8).", code="outside_active_dates")


def _settings(entity):
    found = BankingSettings.objects.filter(entity=entity).first()
    if found is None:
        found = _save(BankingSettings(entity=entity))
    return found


def _labelled_interest(*texts):
    return any(INTEREST_WORDS.search(t or "") for t in texts)


# --- settings and CSV layouts (3.8, R10) ----------------------------------------------------------------------

SETTING_FIELDS = ("m1_date_window_days", "m2_date_window_days", "m3_max_items", "auto_confirm_m1")


def banking_settings(entity, *, user):
    """The entity's matching parameters (OQ-8 defaults). ``user`` needs ``banking.view``."""
    access.require(user, entity, "banking.view")
    return _settings(entity)


def update_settings(entity, *, user, **changes):
    """Change matching parameters (approver, ``banking.manage_settings``); logged with before / after."""
    access.require(user, entity, "banking.manage_settings")
    with transaction.atomic():
        unknown = set(changes) - set(SETTING_FIELDS)
        if unknown:
            raise error(f"Unknown settings {sorted(unknown)}.", f"إعدادات غير معروفة {sorted(unknown)}.", code="settings")
        obj = _settings(entity)
        before = {f: getattr(obj, f) for f in SETTING_FIELDS}
        for name, value in changes.items():
            if name == "auto_confirm_m1":
                if not isinstance(value, bool):
                    raise error("auto_confirm_m1 is true or false.", "قيمة التأكيد التلقائي صح أو خطأ.", code="settings")
            elif isinstance(value, bool) or not isinstance(value, int) or value < (2 if name == "m3_max_items" else 0) \
                    or value > 31:
                raise error(f"{name} must be a whole number of days or items in range.",
                            f"يجب أن تكون قيمة {name} عددًا صحيحًا ضمن النطاق.", code="settings")
            setattr(obj, name, value)
        obj.modified_by, obj.modified_at = user, timezone.now()
        _save(obj)
        _event(entity, user, Event.SETTINGS_CHANGED, obj, {"before": before, "after": {f: getattr(obj, f) for f in SETTING_FIELDS}})
        return obj


CSV_OPTIONS = ("delimiter", "encoding", "date_format", "decimal_separator", "thousands_separator", "skip_rows",
               "credit_values", "debit_values", "sample_verified")


def create_csv_layout(entity, *, user, code, name_en, name_ar, bank, columns, **options):
    """A bank CSV layout (R10, OQ-1), data per entity; ``banking.manage_settings``."""
    access.require(user, entity, "banking.manage_settings")
    with transaction.atomic():
        unknown = set(options) - set(CSV_OPTIONS)
        if unknown:
            raise error(f"Unknown layout options {sorted(unknown)}.", f"خيارات تنسيق غير معروفة {sorted(unknown)}.",
                        code="layout")
        if not isinstance(columns, dict) or "booking_date" not in columns or not (
                "amount" in columns or {"debit", "credit"} <= set(columns)):
            raise error("A layout maps booking_date and amount (or debit and credit) to columns.",
                        "يربط التنسيق تاريخ القيد والمبلغ (أو المدين والدائن) بالأعمدة.", code="layout")
        if not (name_en and name_ar):
            raise error("The layout needs an English and an Arabic name.", "يحتاج التنسيق إلى اسم بالإنجليزية والعربية.",
                        code="layout")
        layout = _save(CsvLayout(entity=entity, code=code, name_en=name_en, name_ar=name_ar, bank=_bank(bank),
                                 columns=columns, created_by=user, **options))
        _event(entity, user, Event.SETTINGS_CHANGED, layout, {"csv_layout": code, "columns": columns})
        return layout


# --- bank accounts (3.2, R3 to R8) ----------------------------------------------------------------------------

def _bank(value):
    if isinstance(value, Bank):
        return value
    found = Bank.objects.filter(code=value).first()
    if found is None or not found.is_active:
        raise error(f"Bank {value} is unknown or inactive.", f"البنك {value} غير معروف أو غير نشط.", code="bank")
    return found


def _gl(entity, value):
    if isinstance(value, Account):
        return value
    found = Account.objects.filter(entity=entity, code=value).first()
    if found is None:
        raise error(f"Account {value} does not exist in {entity.code}.", f"الحساب {value} غير موجود في {entity.code}.",
                    code="gl_account")
    return found


def _gl_has_lines(gl):
    return JournalLine.objects.filter(account=gl).exclude(entry__status=JournalEntry.Status.DRAFT).exists()


def _gl_problems(entity, gl, currency, exclude=None):
    """R5, R6: the GL account is the entity's own asset, postable, active, allowed, 1100 or a child of 1100."""
    if gl.entity_id != entity.pk:
        return [f"GL account {gl.code} belongs to another entity (R2, R5). / حساب الأستاذ {gl.code} يخص كيانًا آخر."]
    out = []
    if gl.type != Account.Type.ASSET or not gl.is_postable or not gl.is_active:
        out.append(f"GL account {gl.code} is not an open, postable asset account (R5). / "
                   f"حساب الأستاذ {gl.code} ليس حساب أصول مفتوحًا للترحيل.")
    if gl.sharia_prohibited:
        out.append(f"GL account {gl.code} is Sharia-prohibited (R5, R29). / حساب الأستاذ {gl.code} محظور شرعًا.")
    root = Account.objects.filter(entity=entity, code=BANK_GL_ROOT).first()
    if gl.code != BANK_GL_ROOT and (root is None or gl.parent_id != root.pk):
        out.append(f"GL account {gl.code} is not 1100 or a sub-account of 1100 (R5). / "
                   f"حساب الأستاذ {gl.code} ليس 1100 ولا حسابًا فرعيًا منه.")
    if gl.currency_id and gl.currency_id != currency.code:
        out.append(f"GL account {gl.code} is in {gl.currency_id}; the bank account is in {currency.code} (R6). / "
                   f"حساب الأستاذ {gl.code} بعملة {gl.currency_id} والحساب البنكي بعملة {currency.code}.")
    elif gl.currency_id is None and _gl_has_lines(gl):
        out.append(f"GL account {gl.code} has no currency and already has postings; use a sub-account set to "
                   f"{currency.code} (R6). / حساب الأستاذ {gl.code} بلا عملة وعليه قيود؛ استخدم حسابًا فرعيًا بالعملة {currency.code}.")
    used = BankAccount.objects.filter(gl_account=gl, status__in=[BankAccount.Status.DRAFT, BankAccount.Status.ACTIVE])
    if exclude is not None:
        used = used.exclude(pk=exclude.pk)
    other = used.first()
    if other is not None:
        out.append(f"GL account {gl.code} is already used by bank account {other.code}; one bank account per GL "
                   f"account (R5). / حساب الأستاذ {gl.code} مستخدم للحساب البنكي {other.code}.")
    return out


def _identifier_problems(bank, iban, account_number, currency, exclude=None):
    """R3: (normalised iban, account number, problems, warnings)."""
    norm = iban_rules.normalise(iban)
    number = "".join(str(account_number or "").split())
    problems, warnings = [], []
    if iban_rules.uses_iban(bank.country):
        problems += iban_rules.problems(norm, country=bank.country)
        if not problems and bank.country == "SA":
            if not bank.sa_bank_code:
                warnings.append(f"Bank {bank.code} has no IBAN bank code yet; the bank code is not checked (OQ-4). / "
                                f"لا يوجد رمز آيبان للبنك {bank.code} بعد؛ لم يُتحقق من رمز البنك.")
            elif norm[4:6] != bank.sa_bank_code:
                text = (f"Positions 5-6 of this IBAN ({norm[4:6]}) are not {bank.code}'s code {bank.sa_bank_code} (R3). / "
                        f"الخانتان 5-6 في رقم الآيبان ({norm[4:6]}) لا تطابقان رمز البنك {bank.sa_bank_code}.")
                (problems if bank.verified else warnings).append(text)
            elif not bank.verified:
                warnings.append(f"Bank code {bank.sa_bank_code} of {bank.code} is not yet verified (OQ-4). / "
                                f"رمز البنك {bank.sa_bank_code} لـ {bank.code} لم يُتحقق منه بعد.")
        if number and not (number.isascii() and number.isalnum() and len(number) <= 40):
            problems.append("The account number has letters and digits only, up to 40. / رقم الحساب أحرف وأرقام فقط.")
    else:
        if norm:
            problems.append(f"{bank.country} has no IBANs here; enter the account number instead (OQ-4). / "
                            f"لا يوجد آيبان لـ {bank.country} هنا؛ أدخل رقم الحساب.")
        if not number:
            problems.append("An account number is required where no IBAN is used (OQ-4). / رقم الحساب مطلوب.")
        elif not (number.isascii() and number.isalnum() and len(number) <= 40):
            problems.append("The account number has letters and digits only, up to 40. / رقم الحساب أحرف وأرقام فقط.")
    if norm and not problems:
        clash = BankAccount.objects.filter(iban_hash=keyed_hash(norm), currency=currency)
        clash = (clash.exclude(pk=exclude.pk) if exclude is not None else clash).first()
        if clash is not None:
            problems.append(f"This IBAN is already registered for {currency.code} (one IBAN belongs to one entity). / "
                            f"رقم الآيبان هذا مسجل مسبقًا بعملة {currency.code}.")
    return norm, number, problems, warnings


def _set_identifiers(account, norm, number):
    account.iban = norm
    account.iban_hash = keyed_hash(norm) if norm else ""
    account.iban_last4 = iban_rules.last4(norm) if norm else ""
    account.account_number = number
    account.account_number_last4 = number[-4:] if number else ""


ACCOUNT_FIELDS = ("code", "name_en", "name_ar", "bank", "currency", "gl_account", "sharia_basis", "statement_format",
                  "csv_layout", "opening_statement_date", "opening_statement_balance", "active_from", "active_to",
                  "iban", "account_number")


def _account_problems(entity, account):
    out = []
    if not re.fullmatch(r"[-a-zA-Z0-9_]{1,30}", account.code or ""):
        out.append("The code has letters, digits, - and _ only (up to 30). / الرمز أحرف وأرقام و- و_ فقط.")
    if not (account.name_en and account.name_ar):
        out.append("The bank account needs an English and an Arabic name. / يحتاج الحساب البنكي إلى اسم بالإنجليزية والعربية.")
    if account.sharia_basis not in BankAccount.ShariaBasis.values:
        out.append("Choose the Sharia basis: islamic or conventional. / اختر الأساس الشرعي: إسلامي أو تقليدي.")
    if account.statement_format not in StatementFormat.values:
        out.append("Choose the statement format: mt940, camt053 or csv. / اختر صيغة الكشف.")
    if account.statement_format == StatementFormat.CSV and account.csv_layout_id is None:
        out.append("A CSV statement format needs a CSV layout. / صيغة CSV تحتاج إلى تنسيق CSV.")
    if account.csv_layout_id and account.csv_layout.entity_id != entity.pk:
        out.append("The CSV layout belongs to another entity (R2). / تنسيق CSV يخص كيانًا آخر.")
    if account.active_to and account.active_to < account.active_from:
        out.append("The account's active dates end before they start. / تنتهي فترة نشاط الحساب قبل بدايتها.")
    if account.code and BankAccount.objects.filter(entity=entity, code=account.code).exclude(pk=account.pk).exists():
        out.append(f"Bank account code {account.code} is already used. / رمز الحساب البنكي {account.code} مستخدم.")
    return out


def create_bank_account(entity, *, user, code, name_en, name_ar, bank, currency, gl_account, sharia_basis,
                        statement_format, opening_statement_date, opening_statement_balance, active_from,
                        active_to=None, iban="", account_number="", csv_layout=None):
    """Draft a bank account (R3 to R7); it becomes active only when another person approves it."""
    access.require(user, entity, "banking.manage_bank_account")
    bank, cur, gl = _bank(bank), _currency(currency), _gl(entity, gl_account)
    _same_entity(entity, csv_layout, "CSV layout", "تنسيق CSV")
    balance = _money(opening_statement_balance, cur, "opening statement balance", positive=False)
    norm, number, problems, warnings = _identifier_problems(bank, iban, account_number, cur)
    account = BankAccount(
        entity=entity, code=code, name_en=name_en, name_ar=name_ar, bank=bank, currency=cur, gl_account=gl,
        sharia_basis=sharia_basis, statement_format=statement_format, csv_layout=csv_layout,
        opening_statement_date=opening_statement_date, opening_statement_balance=balance, active_from=active_from,
        active_to=active_to, created_by=user, warnings=warnings,
    )
    problems += _gl_problems(entity, gl, cur) + _account_problems(entity, account)
    if problems:
        raise _problems(problems, "bank_account_invalid")
    _set_identifiers(account, norm, number)
    with transaction.atomic():
        try:
            with transaction.atomic():
                _save(account)
        except IntegrityError:
            raise error("This IBAN or code is already registered.", "رقم الآيبان أو الرمز مسجل مسبقًا.",
                        code="bank_account_invalid") from None
        _event(entity, user, Event.BANK_ACCOUNT_CREATED, account, {
            "code": code, "number": account.masked_number, "currency": cur.code, "gl_account": gl.code,
            "bank": bank.code, "warnings": warnings,
        })
    return account


DRAFT_ONLY_FIELDS = ("code", "bank", "currency", "gl_account", "sharia_basis", "opening_statement_date",
                     "opening_statement_balance", "active_from", "iban", "account_number")
ACTIVE_EDITABLE = ("name_en", "name_ar", "active_to", "statement_format", "csv_layout")


def update_bank_account(account, *, user, **changes):
    """Edit a draft account; on an active one only names, end date and statement format (R7).

    A new IBAN, account number, currency or GL account of an active account is a change request approved by
    another person (:func:`propose_bank_account_change`).
    """
    entity = BankAccount.objects.select_related("entity").get(pk=account.pk).entity
    access.require(user, entity, "banking.manage_bank_account")
    with transaction.atomic():
        account = BankAccount.objects.select_for_update(of=("self",)).select_related("entity", "bank", "currency").get(
            pk=account.pk)
        unknown = set(changes) - set(ACCOUNT_FIELDS)
        if unknown:
            raise error(f"Unknown fields {sorted(unknown)}.", f"حقول غير معروفة {sorted(unknown)}.", code="fields")
        if account.status == BankAccount.Status.CLOSED:
            raise error("A closed bank account does not change.", "لا يتغير الحساب البنكي المغلق.", code="account_closed")
        if account.status == BankAccount.Status.ACTIVE and set(changes) - set(ACTIVE_EDITABLE):
            raise error(
                "An active account changes its IBAN, account number, currency or GL account only through an approved "
                "change request; its other master data only while it is a draft (R7).",
                "يتغير الآيبان أو رقم الحساب أو العملة أو حساب الأستاذ لحساب نشط بطلب تعديل معتمد فقط (R7).",
                code="needs_change_request",
            )
        before = {k: str(getattr(account, k + ("_id" if k in ("bank", "currency", "gl_account", "csv_layout") else ""), ""))
                  for k in changes if k not in ("iban", "account_number")}
        if "bank" in changes:
            account.bank = _bank(changes["bank"])
        if "currency" in changes:
            account.currency = _currency(changes["currency"])
        if "gl_account" in changes:
            account.gl_account = _gl(entity, changes["gl_account"])
        if "csv_layout" in changes:
            _same_entity(entity, changes["csv_layout"], "CSV layout", "تنسيق CSV")
            account.csv_layout = changes["csv_layout"]
        for name in ("code", "name_en", "name_ar", "sharia_basis", "statement_format", "opening_statement_date",
                     "active_from", "active_to"):
            if name in changes:
                setattr(account, name, changes[name])
        if "opening_statement_balance" in changes:
            account.opening_statement_balance = _money(changes["opening_statement_balance"], account.currency,
                                                       "opening statement balance", positive=False)
        problems = _account_problems(entity, account)
        if {"gl_account", "currency"} & set(changes):
            problems += _gl_problems(entity, account.gl_account, account.currency, exclude=account)
        if {"iban", "account_number", "bank", "currency"} & set(changes):
            norm, number, more, warnings = _identifier_problems(
                account.bank, changes.get("iban", account.iban), changes.get("account_number", account.account_number),
                account.currency, exclude=account)
            problems += more
            if not more:
                _set_identifiers(account, norm, number)
                account.warnings = warnings
        if problems:
            raise _problems(problems, "bank_account_invalid")
        account.modified_by, account.modified_at = user, timezone.now()
        _save(account)
        _event(entity, user, Event.BANK_ACCOUNT_CHANGED, account, {
            "before": before, "after": {k: str(getattr(account, k + ("_id" if k in ("bank", "currency", "gl_account",
                                                                                    "csv_layout") else ""), ""))
                                        for k in before},
            "number": account.masked_number,
        })
        return account


def approve_bank_account(account, *, user):
    """R7: an approver who did not create or last change the draft activates it (``bank_account_activated``)."""
    account = _account(account)
    entity = account.entity
    access.require(user, entity, "banking.approve_bank_account", document=account)
    with transaction.atomic():
        account = BankAccount.objects.select_for_update(of=("self",)).select_related("entity", "bank", "currency",
                                                                                      "gl_account").get(pk=account.pk)
        if account.status != BankAccount.Status.DRAFT:
            raise error(f"Only a draft account is approved; this one is {account.status}.",
                        f"يُعتمد الحساب المسودة فقط؛ حالة هذا الحساب {account.status}.", code="not_draft")
        problems = _gl_problems(entity, account.gl_account, account.currency, exclude=account)
        if problems:
            raise _problems(problems, "bank_account_invalid")
        gl = Account.objects.select_for_update().get(pk=account.gl_account_id)
        if gl.currency_id is None:  # R6: set at setup, before any posting
            gl.currency_id = account.currency_id
            gl.save(update_fields=["currency"])
        account.status = BankAccount.Status.ACTIVE
        account.approved_by, account.approved_at = user, timezone.now()
        try:
            with transaction.atomic():
                _save(account)
        except IntegrityError:
            raise error("Another active bank account uses this GL account or IBAN (R5).",
                        "يوجد حساب بنكي نشط آخر يستخدم حساب الأستاذ أو الآيبان نفسه.", code="bank_account_invalid") from None
        _event(entity, user, Event.BANK_ACCOUNT_ACTIVATED, account, {"code": account.code, "number": account.masked_number})
        return account


def _has_history(account):
    return StatementImport.objects.filter(bank_account=account).exists() or _gl_has_lines(account.gl_account)


def propose_bank_account_change(account, *, user, reason, iban=None, account_number=None, currency=None,
                                gl_account=None):
    """R7: a new IBAN, account number, currency or GL account for an active account, to be approved."""
    account = _account(account)
    entity = account.entity
    access.require(user, entity, "banking.manage_bank_account")
    with transaction.atomic():
        if account.status != BankAccount.Status.ACTIVE:
            raise error("Change requests are for active accounts; edit a draft directly.",
                        "طلبات التعديل للحسابات النشطة؛ عدّل المسودة مباشرة.", code="not_active")
        if not (reason or "").strip():
            raise error("A change request needs a reason.", "يحتاج طلب التعديل إلى سبب.", code="reason")
        if (currency is not None or gl_account is not None) and _has_history(account):
            raise error("Currency and GL account cannot change once a statement is imported or a line posted (R7).",
                        "لا تتغير العملة أو حساب الأستاذ بعد استيراد كشف أو ترحيل قيد (R7).", code="has_history")
        cur = _currency(currency) if currency is not None else account.currency
        problems = []
        change = BankAccountChange(entity=entity, bank_account=account, reason=reason, created_by=user)
        if currency is not None:
            change.currency = cur
        if gl_account is not None:
            change.gl_account = _gl(entity, gl_account)
            problems += _gl_problems(entity, change.gl_account, cur, exclude=account)
        if iban is not None or account_number is not None:
            norm, number, more, _ = _identifier_problems(account.bank, iban if iban is not None else account.iban,
                                                         account_number if account_number is not None
                                                         else account.account_number, cur, exclude=account)
            problems += more
            change.iban, change.account_number = norm, number
            change.iban_hash, change.iban_last4 = (keyed_hash(norm), iban_rules.last4(norm)) if norm else ("", "")
        if problems:
            raise _problems(problems, "bank_account_invalid")
        _save(change)
        _event(entity, user, Event.BANK_ACCOUNT_CHANGED, change, {"requested": True, "reason": reason,
                                                                  "new_number_last4": change.iban_last4})
        return change


def approve_bank_account_change(change, *, user):
    change = BankAccountChange.objects.select_related("entity", "bank_account").get(pk=change.pk)
    entity = change.entity
    access.require(user, entity, "banking.approve_bank_account", document=change)
    with transaction.atomic():
        change = BankAccountChange.objects.select_for_update(of=("self",)).get(pk=change.pk)
        if change.status != BankAccountChange.Status.DRAFT:
            raise error("This change request is already decided.", "تم البت في طلب التعديل هذا.", code="decided")
        account = BankAccount.objects.select_for_update(of=("self",)).get(pk=change.bank_account_id)
        if (change.currency_id or change.gl_account_id) and _has_history(account):
            raise error("Currency and GL account cannot change once a statement is imported or a line posted (R7).",
                        "لا تتغير العملة أو حساب الأستاذ بعد استيراد كشف أو ترحيل قيد (R7).", code="has_history")
        before = account.masked_number
        if change.currency_id:
            account.currency_id = change.currency_id
        if change.gl_account_id:
            account.gl_account_id = change.gl_account_id
        if change.iban or change.account_number:
            _set_identifiers(account, change.iban, change.account_number)
        account.modified_by, account.modified_at = change.created_by, timezone.now()
        try:
            with transaction.atomic():
                _save(account)
        except IntegrityError:
            raise error("Another bank account uses this GL account or IBAN.", "حساب بنكي آخر يستخدم حساب الأستاذ أو الآيبان.",
                        code="bank_account_invalid") from None
        change.status, change.decided_by, change.decided_at = BankAccountChange.Status.APPROVED, user, timezone.now()
        _save(change)
        _event(entity, user, Event.BANK_ACCOUNT_CHANGED, account, {"change": change.pk, "before": before,
                                                                   "after": account.masked_number, "approved": True})
        return account


def close_bank_account(account, *, user, active_to, reason):
    """Close an active account from ``active_to`` (approver, not its creator or last modifier)."""
    account = _account(account)
    entity = account.entity
    access.require(user, entity, "banking.approve_bank_account", document=account)
    with transaction.atomic():
        if account.status != BankAccount.Status.ACTIVE:
            raise error("Only an active account is closed.", "يُغلق الحساب النشط فقط.", code="not_active")
        if BankTransaction.objects.filter(bank_account=account, status__in=[TStatus.DRAFT, TStatus.SUBMITTED]).exists():
            raise error("Post or discard the account's open bank transactions first.",
                        "رحّل العمليات البنكية المفتوحة للحساب أو ألغها أولًا.", code="pending_documents")
        account = BankAccount.objects.select_for_update(of=("self",)).get(pk=account.pk)
        account.status, account.active_to = BankAccount.Status.CLOSED, active_to
        _save(account)
        _event(entity, user, Event.BANK_ACCOUNT_CLOSED, account, {"active_to": active_to, "reason": reason})
        return account


def bank_accounts(entity, *, user):
    """The entity's bank accounts for ``user`` (``banking.view``); show them with :func:`account_row` (masked)."""
    access.require(user, entity, "banking.view")
    _agent_read(user, entity, "bank_accounts")
    return BankAccount.objects.filter(entity=entity).select_related("bank", "currency", "gl_account")


def visible_bank_accounts(*, user):
    """Bank accounts in every entity where ``user`` holds ``banking.view`` (R1, access R6)."""
    entities = access.entities_for(user, "banking.view")
    for entity in entities:
        access.require(user, entity, "banking.view")
    return BankAccount.objects.filter(entity__in=entities).select_related("entity", "bank", "currency")


def get_bank_account(pk, *, user):
    """One bank account; another entity's account answers 404 (R1)."""
    account = access.get_scoped_or_404(BankAccount.objects.select_related("entity", "bank", "currency"), user,
                                       "banking.view", pk=pk)
    access.require(user, account.entity, "banking.view")
    _agent_read(user, account.entity, f"bank_account:{account.pk}")
    return account


def account_row(account, *, user):
    """A list row (R4): the IBAN or account number is always masked in lists."""
    access.require(user, account.entity, "banking.view")
    return {"id": account.pk, "code": account.code, "name_en": account.name_en, "name_ar": account.name_ar,
            "bank_en": account.bank.name_en, "bank_ar": account.bank.name_ar, "number": account.masked_number,
            "currency": account.currency_id, "status": account.status}


def bank_account_detail(account, *, user):
    """Detail view: the full IBAN only for ``banking.view_iban`` holders, logged (R4); masked for everyone else."""
    access.require(user, account.entity, "banking.view")
    row = account_row(account, user=user)
    if access.has(user, account.entity, "banking.view_iban"):
        row.update(reveal_iban(account, user=user))
    return row


def reveal_iban(account, *, user):
    """The unmasked IBAN and account number (``banking.view_iban``); writes ``iban_viewed`` and an access-log row."""
    account = _account(account)
    access.require(user, account.entity, "banking.view_iban")
    with transaction.atomic():
        _event(account.entity, user, Event.IBAN_VIEWED, account, {"number": account.masked_number})
        access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=account.entity,
                            permission="banking.view_iban",
                            details={"object": ["banking.bankaccount", account.pk], "last4": account.iban_last4
                                     or account.account_number_last4})
    return {"iban": account.iban, "account_number": account.account_number}


# --- statement import (3.3, 3.4, R9 to R15) -------------------------------------------------------------------

class _Refused(Exception):
    def __init__(self, problems, code):
        super().__init__(problems)
        self.problems, self.code = list(problems), code


def _line_hash(account, line):
    narrative = " ".join((line.narrative or "").lower().split())
    parts = [str(account.pk), line.booking_date.isoformat(), line.direction, f"{line.amount.normalize():f}",
             (line.bank_reference or "").strip().upper(), narrative]
    return keyed_hash("|".join(parts))


def _identifier_matches(account, identifier):
    ident = iban_rules.normalise(identifier)
    if not ident:
        return True
    candidates = {ident, iban_rules.normalise(ident.split("/")[-1])}
    own = {v for v in (account.iban, iban_rules.normalise(account.account_number)) if v}
    return any(c == o or (len(c) >= 8 and o.endswith(c)) for c in candidates for o in own)


def _check_statement(account, parsed, sha, accept_duplicates, duplicate_reason):
    """R8, R11 to R14 on a parsed statement; returns (line hashes, duplicate map) or raises _Refused."""
    cur = account.currency
    problems = []
    if account.status != BankAccount.Status.ACTIVE:
        raise _Refused([f"Bank account {account} is not active; statements are imported only after approval (R8). / "
                        f"الحساب البنكي {account} ليس نشطًا؛ تُستورد الكشوف بعد الاعتماد فقط."], "account_not_active")
    if not _identifier_matches(account, parsed.account_identifier):
        tail = iban_rules.normalise(parsed.account_identifier)[-4:]
        raise _Refused([f"The statement is for another account (…{tail}), not {account} (R2). / "
                        f"الكشف لحساب آخر (…{tail}) وليس للحساب {account}."], "other_account")
    if not parsed.statement_id:
        problems.append("The statement has no statement id. / لا يوجد رقم للكشف.")
    if parsed.period_start is None or parsed.period_end is None or parsed.period_end < parsed.period_start:
        raise _Refused(problems + ["The statement period is missing or ends before it starts. / فترة الكشف مفقودة أو غير صحيحة."],
                       "period")
    if parsed.opening_balance is None or parsed.closing_balance is None:
        raise _Refused(problems + ["The statement has no opening and closing balance; enter both (OQ-24). / "
                                   "لا يحتوي الكشف على رصيد افتتاحي وختامي؛ أدخلهما."], "balances_missing")
    if parsed.currency and parsed.currency != cur.code:
        raise _Refused([f"The statement is in {parsed.currency}; the account is in {cur.code} (R11). / "
                        f"الكشف بعملة {parsed.currency} والحساب بعملة {cur.code}."], "currency")
    for value, what in ((parsed.opening_balance, "opening balance"), (parsed.closing_balance, "closing balance")):
        if not fx.has_places(value, cur.minor_units):
            problems.append(f"The {what} {value} has more than {cur.minor_units} decimals for {cur.code} (R11, R42). / "
                            f"الرصيد {value} يتجاوز {cur.minor_units} خانات عشرية للعملة {cur.code}.")
    credits = debits = ZERO
    for line in parsed.lines:
        where = f"Line {line.line_no}"
        if line.currency and line.currency != cur.code:
            problems.append(f"{where} is in {line.currency}, not {cur.code} (R11). / السطر {line.line_no} بعملة {line.currency}.")
        if line.amount <= ZERO:
            problems.append(f"{where}: the amount must be above zero. / السطر {line.line_no}: يجب أن يكون المبلغ أكبر من صفر.")
        if not fx.has_places(line.amount, cur.minor_units):
            problems.append(f"{where}: {line.amount} has more than {cur.minor_units} decimals for {cur.code} (R11, R42). / "
                            f"السطر {line.line_no}: المبلغ {line.amount} يتجاوز {cur.minor_units} خانات عشرية.")
        if line.original_amount is not None and line.original_currency:
            orig = Currency.objects.filter(code=line.original_currency).first()
            if orig is None or not fx.has_places(line.original_amount, min(orig.minor_units, 3)):
                problems.append(f"{where}: the original amount does not fit {line.original_currency}. / "
                                f"السطر {line.line_no}: المبلغ الأصلي لا يناسب العملة {line.original_currency}.")
        if not (parsed.period_start <= line.booking_date <= parsed.period_end):
            problems.append(f"{where}: booking date {line.booking_date} is outside the statement period "
                            f"{parsed.period_start}..{parsed.period_end} (R11). / السطر {line.line_no}: تاريخ القيد خارج فترة الكشف.")
        if line.booking_date < account.active_from or (account.active_to and line.booking_date > account.active_to):
            problems.append(f"{where}: {line.booking_date} is outside the account's active dates (R8). / "
                            f"السطر {line.line_no}: التاريخ خارج فترة نشاط الحساب.")
        if line.direction == "credit":
            credits += line.amount
        else:
            debits += line.amount
    if parsed.opening_balance + credits - debits != parsed.closing_balance:
        problems.append(
            f"Opening {parsed.opening_balance} + credits {credits} - debits {debits} = "
            f"{parsed.opening_balance + credits - debits}, but the closing balance is {parsed.closing_balance} (R11). / "
            f"الرصيد الافتتاحي {parsed.opening_balance} + الإيداعات {credits} - السحوبات {debits} = "
            f"{parsed.opening_balance + credits - debits}، والرصيد الختامي {parsed.closing_balance}."
        )
    if problems:
        raise _Refused(problems, "statement_inconsistent")
    imports = StatementImport.objects.filter(bank_account=account)
    if imports.filter(statement_id=parsed.statement_id, sequence_no=parsed.sequence_no).exists():
        raise _Refused([f"Statement {parsed.statement_id} (sequence {parsed.sequence_no}) is already imported from "
                        "another file; a statement is never replaced (R12, R15). / "
                        f"الكشف {parsed.statement_id} مستورد مسبقًا من ملف آخر؛ لا يُستبدل الكشف."], "statement_replaced")
    approved = Reconciliation.objects.filter(bank_account=account, status=Reconciliation.Status.APPROVED,
                                             as_of__gte=parsed.period_start).order_by("-as_of").first()
    if approved is not None:
        raise _Refused([f"The period up to {approved.as_of} is reconciled and approved; no statement for those dates "
                        "is imported (R14). / الفترة حتى {approved.as_of} مسوّاة ومعتمدة؛ لا يُستورد كشف لتلك التواريخ."
                        .replace("{approved.as_of}", str(approved.as_of))], "period_reconciled")
    latest = imports.order_by("-period_end", "-sequence_no", "-pk").first()
    expected = latest.closing_balance if latest else account.opening_statement_balance
    if latest is None:
        if parsed.period_start < account.opening_statement_date:
            raise _Refused([f"The first statement starts {parsed.period_start}, before the opening statement date "
                            f"{account.opening_statement_date} (R12). / يبدأ الكشف الأول قبل تاريخ الرصيد الافتتاحي."], "order")
    elif parsed.period_end < latest.period_start:
        raise _Refused([f"This statement ({parsed.period_start}..{parsed.period_end}) is older than the latest one "
                        f"({latest.period_start}..{latest.period_end}); import in order (R12). / "
                        "هذا الكشف أقدم من آخر كشف مستورد؛ استورد الكشوف بالترتيب."], "order")
    elif parsed.period_start < latest.period_end or (
            parsed.period_start == latest.period_end and not (
                parsed.sequence_no is not None and latest.sequence_no is not None
                and parsed.sequence_no > latest.sequence_no)):
        raise _Refused([f"This statement ({parsed.period_start}..{parsed.period_end}) overlaps the latest one "
                        f"({latest.period_start}..{latest.period_end}) (R12). / "
                        "هذا الكشف يتداخل مع آخر كشف مستورد."], "overlap")
    if parsed.opening_balance != expected:
        raise _Refused([f"The statement opens with {parsed.opening_balance}; the expected opening balance is {expected} "
                        f"(closing balance of the previous statement) (R12). / يبدأ الكشف برصيد {parsed.opening_balance} "
                        f"والرصيد الافتتاحي المتوقع {expected}."], "discontinuity")
    hashes = [_line_hash(account, line) for line in parsed.lines]
    existing = StatementLine.objects.filter(bank_account=account)
    by_hash = {l.line_hash: l for l in existing.filter(line_hash__in=hashes)}
    duplicates = {}
    for line, h in zip(parsed.lines, hashes):
        found = by_hash.get(h)
        if found is None and line.bank_reference:
            found = existing.filter(bank_reference=line.bank_reference, amount=line.amount,
                                    direction=line.direction).first()
        if found is not None:
            duplicates[line.line_no] = found
    accept = set(accept_duplicates or ())
    unaccepted = sorted(set(duplicates) - accept)
    if unaccepted:
        raise _Refused([f"Lines {', '.join(map(str, unaccepted))} repeat lines already imported; re-submit with "
                        "accept_duplicates and a reason if they are real (R13). / "
                        f"الأسطر {', '.join(map(str, unaccepted))} تكرر أسطرًا مستوردة مسبقًا؛ أعد الإرسال مع قبول التكرار وسببه."],
                       "duplicate_lines")
    if accept - set(duplicates):
        raise _Refused([f"Lines {sorted(accept - set(duplicates))} are not duplicates. / الأسطر {sorted(accept - set(duplicates))} ليست مكررة."],
                       "duplicate_lines")
    if accept and not (duplicate_reason or "").strip():
        raise _Refused(["Accepted duplicate lines need a reason (R13). / الأسطر المكررة المقبولة تحتاج إلى سبب."],
                       "duplicate_lines")
    return hashes, duplicates


def import_statement(bank_account, data, *, user, file_name="statement", format=None, accept_duplicates=(),
                     duplicate_reason="", opening_balance=None, closing_balance=None, statement_id=None,
                     sequence_no=None, period_start=None, period_end=None):
    """Import one statement file (R9 to R14) and run matching (R18). Idempotent by file hash.

    ``format`` defaults to the account's statement format. For CSV exports without balances the importer
    enters ``opening_balance`` and ``closing_balance`` (OQ-24); a statement id, sequence and period may be
    entered too (otherwise derived from the file). A refusal stores nothing and is logged (``import_refused``).
    """
    account = _account(bank_account)
    entity = account.entity
    access.require(user, entity, "banking.import_statement")
    if not isinstance(data, (bytes, bytearray)):
        raise error("Pass the statement file's bytes.", "مرّر محتوى ملف الكشف.", code="file")
    data = bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    existing = StatementImport.objects.filter(bank_account=account, file_sha256=sha).first()
    if existing is not None:  # R9: same file again: nothing new
        _event(entity, user, Event.IMPORT_DUPLICATE_FILE, existing, {"file_sha256": sha, "file_name": file_name[:200]})
        return existing
    fmt = format or account.statement_format
    try:
        if account.status != BankAccount.Status.ACTIVE:  # R8, before the file is even read
            raise _Refused([f"Bank account {account} is not active; statements are imported only after approval "
                            f"(R8). / الحساب البنكي {account} ليس نشطًا؛ تُستورد الكشوف بعد الاعتماد فقط."],
                           "account_not_active")
        if len(data) > getattr(settings, "BANKING_MAX_STATEMENT_BYTES", 10 * 1024 * 1024):
            raise _Refused(["The file is too large for a statement. / الملف أكبر من الحد المسموح للكشف."], "file_too_large")
        if fmt not in StatementFormat.values:
            raise _Refused([f"Unsupported format {fmt!r} (R10). / الصيغة {fmt!r} غير مدعومة."], "unsupported_format")
        try:
            parsed = parse_statement_file(fmt, data, layout=account.csv_layout)
        except ParseError as exc:
            raise _Refused([str(exc)], "unsupported_format" if "Unsupported" in str(exc) else "parse_failed") from None
        _apply_entered(parsed, account, sha, opening_balance, closing_balance, statement_id, sequence_no,
                       period_start, period_end)
        with transaction.atomic():
            account = BankAccount.objects.select_for_update(of=("self",)).select_related(
                "entity", "bank", "currency", "gl_account", "csv_layout").get(pk=account.pk)
            hashes, duplicates = _check_statement(account, parsed, sha, accept_duplicates, duplicate_reason)
            stored = _store_statement(account, parsed, data, sha, fmt, file_name, user, hashes, duplicates,
                                      duplicate_reason)
    except _Refused as refused:
        _event(entity, user, Event.IMPORT_REFUSED, account, {
            "file_sha256": sha, "file_name": file_name[:200], "reason": refused.code, "problems": refused.problems,
        }, survive=True)
        raise _problems(refused.problems, refused.code) from None
    except ValueError as exc:  # a value a user entered (balances, ids) that is not usable
        _event(entity, user, Event.IMPORT_REFUSED, account, {"file_sha256": sha, "reason": "invalid_input"},
               survive=True)
        raise error(f"The statement cannot be imported: {exc}", f"تعذر استيراد الكشف: {exc}", code="invalid_input") from None
    _match_after(account, user)
    return stored


def _apply_entered(parsed, account, sha, opening_balance, closing_balance, statement_id, sequence_no, period_start,
                   period_end):
    """OQ-24 / OQ-1: values the importer enters for files without them (CSV); never overrides file values."""
    cur = account.currency
    for name, value in (("opening_balance", opening_balance), ("closing_balance", closing_balance)):
        if value is None:
            continue
        amount = _money(value, cur, name.replace("_", " "), positive=False)
        if getattr(parsed, name) is not None and getattr(parsed, name) != amount:
            raise _Refused([f"The file states its {name.replace('_', ' ')}; the entered value differs. / "
                            "يحدد الملف الرصيد؛ والقيمة المدخلة مختلفة."], "entered_differs")
        setattr(parsed, name, amount)
    if statement_id:
        parsed.statement_id = str(statement_id)[:35]
    if not parsed.statement_id:
        parsed.statement_id = f"CSV-{sha[:12]}"  # OQ-1: derived from the file hash
    if sequence_no is not None:
        parsed.sequence_no = int(sequence_no)
    if period_start is not None:
        parsed.period_start = period_start
    if period_end is not None:
        parsed.period_end = period_end


def _store_statement(account, parsed, data, sha, fmt, file_name, user, hashes, duplicates, duplicate_reason):
    entity = account.entity
    credits = sum((l.amount for l in parsed.lines if l.direction == "credit"), ZERO)
    debits = sum((l.amount for l in parsed.lines if l.direction == "debit"), ZERO)
    accepted = [{"line_no": no, "duplicate_of": dup.pk, "reason": duplicate_reason} for no, dup in sorted(duplicates.items())]
    stored = StatementImport.objects.create(
        entity=entity, bank_account=account, file_sha256=sha, file_content=base64.b64encode(data).decode("ascii"),
        file_name=file_name[:200], format=fmt, statement_id=parsed.statement_id, sequence_no=parsed.sequence_no,
        period_start=parsed.period_start, period_end=parsed.period_end, currency=account.currency,
        opening_balance=parsed.opening_balance, closing_balance=parsed.closing_balance, line_count=len(parsed.lines),
        total_credits=credits, total_debits=debits, accepted_duplicates=accepted, warnings=parsed.warnings,
        imported_by=user, source=_source(user),
    )
    StatementLine.objects.bulk_create([
        StatementLine(
            entity=entity, bank_account=account, statement=stored, line_no=line.line_no,
            booking_date=line.booking_date, value_date=line.value_date, direction=line.direction, amount=line.amount,
            currency=account.currency, original_amount=line.original_amount,
            original_currency_id=line.original_currency or None, bank_reference=line.bank_reference,
            end_to_end_id=line.end_to_end_id, customer_reference=line.customer_reference,
            transaction_code=line.transaction_code, counterparty_name=line.counterparty_name,
            counterparty_iban=iban_rules.normalise(line.counterparty_iban), narrative=line.narrative, line_hash=h,
            duplicate_of=duplicates.get(line.line_no),
        )
        for line, h in zip(parsed.lines, hashes)
    ])
    _event(entity, user, Event.IMPORT_ACCEPTED, stored, {
        "file_sha256": sha, "file_name": file_name[:200], "statement_id": parsed.statement_id,
        "period": [parsed.period_start, parsed.period_end], "lines": len(parsed.lines), "credits": credits,
        "debits": debits, "warnings": parsed.warnings,
    })
    if accepted:
        _event(entity, user, Event.DUPLICATES_ACCEPTED, stored, {"lines": accepted})
    return stored


# --- matching (3.5, R16 to R24) -------------------------------------------------------------------------------
# The matching engine only reads ledger lines and writes Match / MatchPart rows. It never creates, changes or posts
# a journal entry or a bank transaction (R16, R24; tested by banking/test_matching.py scanning this section).

def _book_items(account):
    """R16: posted (or reversed) lines on the account's GL, in the active range, with an account-currency amount."""
    qs = JournalLine.objects.filter(
        account_id=account.gl_account_id, entry__entity_id=account.entity_id, entry__status__in=BOOKED,
        entry__date__gte=account.active_from,
    ).exclude(line_kind=JournalLine.LineKind.REVALUATION).exclude(txn_amount=ZERO)
    if account.active_to:
        qs = qs.filter(entry__date__lte=account.active_to)
    return qs.select_related("entry")


def _consumed(field, ids, exclude_match=None):
    """{id: amount used by confirmed matches} for statement lines or journal lines (R17)."""
    qs = MatchPart.objects.filter(**{f"{field}__in": ids}, match__status=Match.Status.CONFIRMED)
    if exclude_match is not None:
        qs = qs.exclude(match_id=exclude_match)
    return {r[field]: r["total"] for r in qs.values(field).annotate(total=Sum("amount"))}


def _open_lines(account):
    lines = list(StatementLine.objects.filter(bank_account=account).order_by("booking_date", "pk"))
    used = _consumed("statement_line", [l.pk for l in lines])
    for line in lines:
        line.open_amount = line.amount - used.get(line.pk, ZERO)
    return [l for l in lines if l.open_amount > ZERO]


def _open_books(account):
    items = list(_book_items(account).order_by("entry__date", "pk"))
    used = _consumed("journal_line", [i.pk for i in items])
    for item in items:
        item.open_amount = item.txn_amount - used.get(item.pk, ZERO)
        item.money_in = item.debit > ZERO
    return [i for i in items if i.open_amount > ZERO]


def _book_references(item, cache):
    """M1 references of a book item (R20): entry number and reference, the source document's number, bank
    reference and allocated invoice numbers."""
    if item.entry_id in cache:
        return cache[item.entry_id]
    entry = item.entry
    refs = {entry.number, entry.reference}
    doc = None
    if entry.document_type:
        from ledger import documents

        doc_type = documents.get(entry.document_type)
        if doc_type is not None:
            doc = doc_type.model._default_manager.filter(pk=entry.document_id).first()
    if doc is not None:
        refs |= {getattr(doc, "number", ""), getattr(doc, "bank_reference", "")}
        applications = getattr(doc, "applications", None)
        if applications is not None and hasattr(applications, "select_related"):
            refs |= {a.target.number for a in applications.select_related("target") if getattr(a.target, "number", "")}
    refs = {r.strip() for r in refs if r and len(r.strip()) >= 4}
    cache[item.entry_id] = (refs, doc)
    return cache[item.entry_id]


def _token_found(ref, texts):
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(ref) + r"(?![A-Za-z0-9])", re.IGNORECASE)
    return any(pattern.search(t or "") for t in texts)


def _line_texts(line):
    return (line.bank_reference, line.end_to_end_id, line.customer_reference, line.narrative)


def _references_match(line, item, cache):
    refs, doc = _book_references(item, cache)
    if isinstance(doc, BankTransaction) and doc.statement_line_id == line.pk:
        return True
    texts = _line_texts(line)
    return any(_token_found(ref, texts) for ref in refs)


def _pair_key(parts):
    return frozenset((("s" if isinstance(o, StatementLine) else "b"), o.pk) for o, _ in parts)


def _known_keys(account):
    """Groups already proposed or rejected for this account: not proposed again."""
    keys, busy = set(), set()
    for match in Match.objects.filter(bank_account=account, status__in=[Match.Status.PROPOSED, Match.Status.REJECTED]):
        key = frozenset((("s", p.statement_line_id) if p.statement_line_id else ("b", p.journal_line_id))
                        for p in match.parts.all())
        keys.add(key)
        if match.status == Match.Status.PROPOSED:
            busy |= key
    return keys, busy


def _side_of(obj):
    if isinstance(obj, StatementLine):
        return MatchPart.Side.BANK_IN if obj.direction == StatementLine.Direction.CREDIT else MatchPart.Side.BANK_OUT
    return MatchPart.Side.BOOK_DEBIT if obj.debit > ZERO else MatchPart.Side.BOOK_CREDIT


def _new_match(account, rule, confidence, parts, actor, *, confirm_by=None, auto=False):
    """Write one match group with its parts (proposed, or confirmed by ``confirm_by``)."""
    match = _save(Match(entity=account.entity, bank_account=account, rule=rule, confidence=confidence,
                        proposed_by=actor))
    for obj, amount in parts:
        MatchPart.objects.create(
            entity=account.entity, match=match, amount=amount, side=_side_of(obj),
            statement_line=obj if isinstance(obj, StatementLine) else None,
            journal_line=obj if isinstance(obj, JournalLine) else None,
        )
    _event(account.entity, actor, Event.MATCH_PROPOSED, match, {"rule": rule, "confidence": confidence,
                                                                "parts": _parts_detail(parts)})
    if confirm_by is not None:
        match = _confirm(match, confirm_by, auto=auto)
    return match


def _parts_detail(parts):
    return [{"type": "statement_line" if isinstance(o, StatementLine) else "journal_line", "id": o.pk, "amount": a}
            for o, a in parts]


def _check_parts(account, parts):
    """R2, R16, R17: parts of one account, open enough, balancing in the account currency."""
    problems = []
    stmt = book = ZERO
    for obj, amount in parts:
        if isinstance(obj, StatementLine):
            if obj.bank_account_id != account.pk:
                problems.append(f"Statement line {obj.pk} belongs to another bank account (R2). / سطر الكشف {obj.pk} يخص حسابًا آخر.")
            stmt += amount if obj.direction == StatementLine.Direction.CREDIT else -amount
        elif isinstance(obj, JournalLine):
            entry = obj.entry
            if (obj.account_id != account.gl_account_id or entry.entity_id != account.entity_id
                    or entry.status not in BOOKED or obj.line_kind == JournalLine.LineKind.REVALUATION):
                problems.append(f"Journal line {obj.pk} is not a posted line on {account}'s GL account (R2, R16). / "
                                f"بند القيد {obj.pk} ليس بندًا مرحّلًا على حساب الأستاذ لهذا الحساب البنكي.")
            book += amount if obj.debit > ZERO else -amount
        else:
            problems.append("A match part is a statement line or a journal line. / جزء المطابقة سطر كشف أو بند قيد.")
        if amount <= ZERO:
            problems.append("Match amounts are above zero. / مبالغ المطابقة أكبر من صفر.")
    if stmt != book:
        problems.append(f"The group does not balance: bank {stmt} vs books {book} (R17). / "
                        f"المطابقة غير متوازنة: البنك {stmt} والدفاتر {book}.")
    return problems


def _confirm(match, user, *, auto=False):
    """Confirm a group under row locks on its lines (R17); proposals it makes impossible are rejected."""
    match = Match.objects.select_for_update(of=("self",)).select_related("bank_account", "entity").get(pk=match.pk)
    if match.status != Match.Status.PROPOSED:
        raise error(f"Only a proposed match is confirmed; this one is {match.status}.",
                    f"تُؤكد المطابقة المقترحة فقط؛ حالة هذه {match.status}.", code="match_status")
    parts = list(match.parts.all())
    s_ids = sorted(p.statement_line_id for p in parts if p.statement_line_id)
    j_ids = sorted(p.journal_line_id for p in parts if p.journal_line_id)
    lines = {l.pk: l for l in StatementLine.objects.select_for_update().filter(pk__in=s_ids).order_by("pk")}
    items = {j.pk: j for j in JournalLine.objects.select_for_update(of=("self",)).select_related("entry")
             .filter(pk__in=j_ids).order_by("pk")}
    objs = [(lines[p.statement_line_id] if p.statement_line_id else items[p.journal_line_id], p.amount) for p in parts]
    problems = _check_parts(match.bank_account, objs)
    used_s, used_j = _consumed("statement_line", s_ids), _consumed("journal_line", j_ids)
    for obj, amount in objs:
        full = obj.amount if isinstance(obj, StatementLine) else obj.txn_amount
        used = (used_s if isinstance(obj, StatementLine) else used_j).get(obj.pk, ZERO)
        if amount > full - used:
            problems.append(f"{'Statement line' if isinstance(obj, StatementLine) else 'Journal line'} {obj.pk}: "
                            f"{amount} is more than its open amount {full - used} (R17). / "
                            f"المبلغ {amount} أكبر من المبلغ المفتوح {full - used}.")
    if problems:
        raise _problems(problems, "match_invalid")
    match.status, match.auto = Match.Status.CONFIRMED, auto
    match.confirmed_by, match.confirmed_at = user, timezone.now()
    _save(match)
    _event(match.entity, user, Event.MATCH_CONFIRMED, match, {"rule": match.rule, "auto": auto})
    _reject_impossible(match, user)
    return match


def _reject_impossible(confirmed, user):
    """AT-12: proposals whose amounts are no longer open after ``confirmed`` are rejected automatically."""
    s_ids = list(confirmed.parts.exclude(statement_line=None).values_list("statement_line_id", flat=True))
    j_ids = list(confirmed.parts.exclude(journal_line=None).values_list("journal_line_id", flat=True))
    others = Match.objects.filter(bank_account=confirmed.bank_account, status=Match.Status.PROPOSED).filter(
        _q_parts(s_ids, j_ids)).distinct()
    for other in others:
        parts = list(other.parts.select_related("statement_line", "journal_line"))
        used_s = _consumed("statement_line", [p.statement_line_id for p in parts if p.statement_line_id])
        used_j = _consumed("journal_line", [p.journal_line_id for p in parts if p.journal_line_id])
        if any(p.amount > ((p.statement_line.amount - used_s.get(p.statement_line_id, ZERO)) if p.statement_line_id
                           else (p.journal_line.txn_amount - used_j.get(p.journal_line_id, ZERO))) for p in parts):
            other.status, other.rejected_by, other.rejected_at = Match.Status.REJECTED, user, timezone.now()
            _save(other)
            _event(other.entity, user, Event.MATCH_REJECTED, other, {"reason": "amount_consumed",
                                                                     "by_match": confirmed.pk, "auto": True})


def _q_parts(s_ids, j_ids):
    from django.db.models import Q

    return Q(parts__statement_line_id__in=s_ids) | Q(parts__journal_line_id__in=j_ids)


def _confirmer(user, entity):
    """Who confirms automatic matches (M0, M1): the acting person if they hold ``banking.match`` (never an agent)."""
    if user is not None and access_services.kind_of(user) == Kind.HUMAN and access.has(user, entity, "banking.match"):
        return user
    return None


def _run_matching(account, actor, confirmer):
    """R18: M0, M1, M2, M3 in order. Writes only Match / MatchPart rows and events."""
    cfg = _settings(account.entity)
    created = []
    keys, busy = _known_keys(account)
    created += _m0(account, actor, confirmer, keys)
    lines, books = _open_lines(account), _open_books(account)
    lines = [l for l in lines if ("s", l.pk) not in busy]
    books = [b for b in books if ("b", b.pk) not in busy]
    cache = {}
    m1, used = _m1(account, cfg, lines, books, actor, confirmer, keys, cache)
    created += m1
    lines = [l for l in lines if ("s", l.pk) not in used]
    books = [b for b in books if ("b", b.pk) not in used]
    m2, used = _m2(account, cfg, lines, books, actor, keys)
    created += m2
    lines = [l for l in lines if ("s", l.pk) not in used]
    books = [b for b in books if ("b", b.pk) not in used]
    created += _m3(account, cfg, lines, books, actor, keys, cache)
    return created


def _m0(account, actor, confirmer, keys):
    """R19: a posted line and its reversal's mirror line, both unmatched: one group, no statement line."""
    created = []
    items = {i.pk: i for i in _open_books(account)}
    for item in [i for i in items.values() if i.entry.status == JournalEntry.Status.REVERSED]:
        reversal = JournalEntry.objects.filter(reversal_of_id=item.entry_id, status=JournalEntry.Status.POSTED).first()
        if reversal is None:
            continue
        originals = list(JournalLine.objects.filter(entry_id=item.entry_id).order_by("pk"))
        mirrors = list(JournalLine.objects.filter(entry_id=reversal.pk).order_by("pk"))
        if len(originals) != len(mirrors):
            continue
        mirror = mirrors[[o.pk for o in originals].index(item.pk)]
        partner = items.get(mirror.pk)
        if partner is None or partner.open_amount != item.txn_amount or item.open_amount != item.txn_amount:
            continue
        parts = [(item, item.txn_amount), (partner, partner.txn_amount)]
        if _pair_key(parts) in keys:
            continue
        keys.add(_pair_key(parts))
        created.append(_new_match(account, Match.Rule.M0, 100, parts, actor, confirm_by=confirmer, auto=True))
        items.pop(item.pk, None)
        items.pop(partner.pk, None)
    return created


def _within(a, b, days):
    return abs((a - b).days) <= days


def _same_direction(line, item):
    return (line.direction == StatementLine.Direction.CREDIT) == item.money_in


def _m1(account, cfg, lines, books, actor, confirmer, keys, cache):
    """R20: same amount and direction, within the M1 window, a reference found as a whole token."""
    by_line, by_book = defaultdict(list), defaultdict(list)
    for line in lines:
        for item in books:
            if (item.open_amount == line.open_amount and _same_direction(line, item)
                    and _within(line.booking_date, item.entry.date, cfg.m1_date_window_days)
                    and _references_match(line, item, cache)):
                by_line[line.pk].append(item)
                by_book[item.pk].append(line)
    created, used = [], set()
    line_map = {l.pk: l for l in lines}
    for line_pk, items in by_line.items():
        line = line_map[line_pk]
        unique = len(items) == 1 and len(by_book[items[0].pk]) == 1
        for item in items:
            parts = [(line, line.open_amount), (item, item.open_amount)]
            used |= {("s", line.pk), ("b", item.pk)}
            if _pair_key(parts) in keys:
                continue
            keys.add(_pair_key(parts))
            confirm = confirmer if (unique and cfg.auto_confirm_m1) else None
            created.append(_new_match(account, Match.Rule.M1, 100, parts, actor, confirm_by=confirm,
                                      auto=confirm is not None))
    return created, used


def _m2(account, cfg, lines, books, actor, keys):
    """R21: same amount and direction within the M2 window; 70 if one candidate each side, else 40; never confirmed."""
    by_line, by_book = defaultdict(list), defaultdict(list)
    for line in lines:
        for item in books:
            if (item.open_amount == line.open_amount and _same_direction(line, item)
                    and _within(line.booking_date, item.entry.date, cfg.m2_date_window_days)):
                by_line[line.pk].append(item)
                by_book[item.pk].append(line)
    created, used = [], set()
    line_map = {l.pk: l for l in lines}
    for line_pk, items in by_line.items():
        line = line_map[line_pk]
        for item in items:
            confidence = 70 if len(items) == 1 and len(by_book[item.pk]) == 1 else 40
            parts = [(line, line.open_amount), (item, item.open_amount)]
            used |= {("s", line.pk), ("b", item.pk)}
            if _pair_key(parts) in keys:
                continue
            keys.add(_pair_key(parts))
            created.append(_new_match(account, Match.Rule.M2, confidence, parts, actor))
    return created, used


M3_CANDIDATES = 12  # nearest open items considered per line (keeps the subset search small)


def _subset(target, candidates, max_items):
    for size in range(2, max_items + 1):
        for combo in itertools.combinations(candidates, size):
            if sum((c.open_amount for c in combo), ZERO) == target:
                return combo
    return None


def _m3(account, cfg, lines, books, actor, keys, cache):
    """R22: one statement line = 2..max open book items, or one book item = 2..max lines, within the M2 window."""
    created, taken = [], set()
    for line in lines:
        pool = sorted((b for b in books if ("b", b.pk) not in taken and _same_direction(line, b)
                       and b.open_amount < line.open_amount
                       and _within(line.booking_date, b.entry.date, cfg.m2_date_window_days)),
                      key=lambda b: abs((b.entry.date - line.booking_date).days))[:M3_CANDIDATES]
        combo = _subset(line.open_amount, pool, cfg.m3_max_items)
        if combo is None:
            continue
        parts = [(line, line.open_amount)] + [(b, b.open_amount) for b in combo]
        if _pair_key(parts) in keys:
            continue
        keys.add(_pair_key(parts))
        confidence = 90 if all(_references_match(line, b, cache) for b in combo) else 50
        created.append(_new_match(account, Match.Rule.M3, confidence, parts, actor))
        taken |= {("s", line.pk)} | {("b", b.pk) for b in combo}
    for item in books:
        if ("b", item.pk) in taken:
            continue
        pool = sorted((l for l in lines if ("s", l.pk) not in taken and _same_direction(l, item)
                       and l.open_amount < item.open_amount
                       and _within(l.booking_date, item.entry.date, cfg.m2_date_window_days)),
                      key=lambda l: abs((l.booking_date - item.entry.date).days))[:M3_CANDIDATES]
        combo = _subset(item.open_amount, pool, cfg.m3_max_items)
        if combo is None:
            continue
        parts = [(l, l.open_amount) for l in combo] + [(item, item.open_amount)]
        if _pair_key(parts) in keys:
            continue
        keys.add(_pair_key(parts))
        confidence = 90 if all(_references_match(l, item, cache) for l in combo) else 50
        created.append(_new_match(account, Match.Rule.M3, confidence, parts, actor))
        taken |= {("b", item.pk)} | {("s", l.pk) for l in combo}
    return created


def _match_after(account, user):
    """R18: matching after an import or a posting, as ``user`` (automatic confirmations only for a person with
    ``banking.match``)."""
    with transaction.atomic():
        BankAccount.objects.select_for_update(of=("self",)).filter(pk=account.pk).first()
        return _run_matching(account, user, _confirmer(user, account.entity))


def run_matching(bank_account, *, user):
    """Run M0 to M3 on demand (``banking.suggest_match``). An agent gets proposals only (R43)."""
    account = _account(bank_account)
    access.require(user, account.entity, "banking.suggest_match")
    _require_active(account)
    return _match_after(account, user)


def _parts_from(account, statement_lines, journal_lines):
    """[(obj, Decimal)] from (object, amount or None = its open amount) pairs, read again from the database."""
    parts = []
    s_used = _consumed("statement_line", [getattr(o, "pk", o) for o, _ in statement_lines])
    j_used = _consumed("journal_line", [getattr(o, "pk", o) for o, _ in journal_lines])
    for obj, amount in statement_lines:
        line = StatementLine.objects.filter(pk=getattr(obj, "pk", obj)).first()
        if line is None:
            raise error("Unknown statement line.", "سطر كشف غير معروف.", code="match_invalid")
        value = line.amount - s_used.get(line.pk, ZERO) if amount is None else _money(amount, account.currency, "amount")
        parts.append((line, value))
    for obj, amount in journal_lines:
        item = JournalLine.objects.select_related("entry").filter(pk=getattr(obj, "pk", obj)).first()
        if item is None:
            raise error("Unknown journal line.", "بند قيد غير معروف.", code="match_invalid")
        value = item.txn_amount - j_used.get(item.pk, ZERO) if amount is None else _money(amount, account.currency, "amount")
        parts.append((item, value))
    if not parts:
        raise error("A match needs at least one part.", "تحتاج المطابقة إلى جزء واحد على الأقل.", code="match_invalid")
    return parts


def manual_match(bank_account, *, user, statement_lines=(), journal_lines=()):
    """R23: build and confirm any balancing group (``banking.match``). Parts: (object, amount or None)."""
    account = _account(bank_account)
    access.require(user, account.entity, "banking.match")
    with transaction.atomic():
        _require_active(account)
        parts = _parts_from(account, statement_lines, journal_lines)
        problems = _check_parts(account, parts)
        if problems:
            raise _problems(problems, "match_invalid")
        return _new_match(account, Match.Rule.MANUAL, 0, parts, user, confirm_by=user)


def propose_match(bank_account, *, user, statement_lines=(), journal_lines=()):
    """A suggested group (``banking.suggest_match``; agents too). Stays ``proposed`` until a person confirms it."""
    account = _account(bank_account)
    access.require(user, account.entity, "banking.suggest_match")
    with transaction.atomic():
        _require_active(account)
        parts = _parts_from(account, statement_lines, journal_lines)
        problems = _check_parts(account, parts)
        if problems:
            raise _problems(problems, "match_invalid")
        return _new_match(account, Match.Rule.MANUAL, 0, parts, user)


def confirm_match(match, *, user):
    match = Match.objects.select_related("bank_account", "entity").get(pk=match.pk)
    access.require(user, match.entity, "banking.match")
    with transaction.atomic():
        _require_active(_account(match.bank_account_id))
        return _confirm(match, user)


def reject_match(match, *, user, reason=""):
    match = Match.objects.select_related("entity").get(pk=match.pk)
    access.require(user, match.entity, "banking.match")
    with transaction.atomic():
        match = Match.objects.select_for_update(of=("self",)).get(pk=match.pk)
        if match.status != Match.Status.PROPOSED:
            raise error("Only a proposed match is rejected.", "تُرفض المطابقة المقترحة فقط.", code="match_status")
        match.status, match.rejected_by, match.rejected_at = Match.Status.REJECTED, user, timezone.now()
        _save(match)
        _event(match.entity, user, Event.MATCH_REJECTED, match, {"reason": reason})
        return match


def undo_match(match, *, user, reason):
    """R23, R35, R40: undo a confirmed group with a reason; refused once a reconciliation locked it."""
    match = Match.objects.select_related("entity").get(pk=match.pk)
    access.require(user, match.entity, "banking.match")
    with transaction.atomic():
        match = Match.objects.select_for_update(of=("self",)).get(pk=match.pk)
        if not (reason or "").strip():
            raise error("Undoing a match needs a reason.", "إلغاء المطابقة يحتاج إلى سبب.", code="reason")
        if match.status != Match.Status.CONFIRMED:
            raise error("Only a confirmed match is undone.", "تُلغى المطابقة المؤكدة فقط.", code="match_status")
        if match.reconciliation_id is not None:
            raise error("This match is locked by an approved reconciliation and cannot be undone (R40).",
                        "هذه المطابقة مقفلة بتسوية معتمدة ولا يمكن إلغاؤها.", code="match_locked")
        match.status, match.undone_by, match.undone_at, match.undo_reason = (Match.Status.UNDONE, user, timezone.now(),
                                                                            reason[:300])
        _save(match)
        _event(match.entity, user, Event.MATCH_UNDONE, match, {"reason": reason})
        return match


def _line_state(line):
    parts = MatchPart.objects.filter(statement_line=line, match__status=Match.Status.CONFIRMED).select_related("match")
    used = sum((p.amount for p in parts), ZERO)
    if used == ZERO:
        return "unmatched"
    if used < line.amount:
        return "partly_matched"
    return "locked" if all(p.match.reconciliation_id for p in parts) else "matched"


def statement_line_state(line, *, user):
    """R17: ``unmatched``, ``partly_matched``, ``matched`` or ``locked``, computed from confirmed match parts."""
    access.require(user, line.entity, "banking.view")
    return _line_state(line)


# --- bank transactions (3.6, R25 to R35) ------------------------------------------------------------------------

_posting_document = ContextVar("banking_posting_document", default=None)


@contextmanager
def _posting(document):
    token = _posting_document.set(document.pk)
    try:
        yield
    finally:
        _posting_document.reset(token)


DEFAULT_DESCRIPTIONS = {
    Kind_.BANK_CHARGE: ("Bank charge", "رسوم بنكية"),
    Kind_.ISLAMIC_PROFIT: ("Islamic deposit profit", "أرباح وديعة إسلامية"),
    Kind_.CONVENTIONAL_INTEREST_CREDIT: ("Conventional interest credited, to purify", "فوائد تقليدية مقيدة للتطهير"),
    Kind_.OWN_TRANSFER: ("Transfer between own accounts", "تحويل بين حسابات الكيان"),
    Kind_.INTERCOMPANY_TRANSFER: ("Intercompany transfer", "تحويل بين شركات المجموعة"),
}


def available_kinds(statement_line, *, user):
    """The bank transaction kinds offered for an unmatched line (R26 to R32). A conventional interest or penalty
    debit has none (R29, OQ-7)."""
    line = StatementLine.objects.select_related("bank_account", "entity").get(pk=statement_line.pk)
    access.require(user, line.entity, "banking.view")
    labelled = _labelled_interest(line.narrative, line.transaction_code, line.bank_reference)
    if line.direction == StatementLine.Direction.DEBIT:
        return [] if labelled else [Kind_.BANK_CHARGE, Kind_.OWN_TRANSFER, Kind_.INTERCOMPANY_TRANSFER]
    if labelled or line.bank_account.sharia_basis == BankAccount.ShariaBasis.CONVENTIONAL:
        return [Kind_.CONVENTIONAL_INTEREST_CREDIT, Kind_.OWN_TRANSFER, Kind_.INTERCOMPANY_TRANSFER]
    return [Kind_.ISLAMIC_PROFIT, Kind_.CONVENTIONAL_INTEREST_CREDIT, Kind_.OWN_TRANSFER, Kind_.INTERCOMPANY_TRANSFER]


def _txn_problems(txn, user):
    """R2, R8, R26 to R33 for a draft (its stored or proposed values)."""
    account = txn.bank_account
    entity = txn.entity
    problems = []
    try:
        _require_active(account, txn.date)
    except BankingError as exc:
        problems += exc.messages
    kind = txn.kind
    if kind not in Kind_.values:
        return problems + ["Unknown bank transaction kind. / نوع عملية بنكية غير معروف."]
    if txn.direction not in BankTransaction.Direction.values:
        problems.append("The direction is in or out. / الاتجاه وارد أو صادر.")
    fixed = {Kind_.BANK_CHARGE: "out", Kind_.ISLAMIC_PROFIT: "in", Kind_.CONVENTIONAL_INTEREST_CREDIT: "in"}
    if kind in fixed and txn.direction != fixed[kind]:
        problems.append(f"A {kind} is money {fixed[kind]} (R26 to R28). / اتجاه هذا النوع {fixed[kind]}.")
    line = txn.statement_line
    if line is not None:
        if line.bank_account_id != account.pk:
            problems.append("The statement line belongs to another bank account (R2). / سطر الكشف يخص حسابًا آخر.")
        expected = "in" if line.direction == StatementLine.Direction.CREDIT else "out"
        if txn.direction != expected:
            problems.append(f"The statement line is money {expected}. / سطر الكشف اتجاهه {expected}.")
        labelled = _labelled_interest(line.narrative, line.transaction_code, line.bank_reference)
        if labelled and line.direction == StatementLine.Direction.DEBIT:
            problems.append(
                "The bank debited conventional interest or a penalty: no bank transaction kind books it; it stays an "
                "outstanding item noted as a Sharia exception until OQ-7 is decided (R29). / "
                "خصم البنك فائدة أو غرامة تقليدية: لا يوجد نوع عملية يقيدها؛ تبقى بندًا معلقًا مع ملاحظة استثناء شرعي.")
        if labelled and kind == Kind_.ISLAMIC_PROFIT:
            problems.append("The bank describes this credit as interest: use conventional_interest_credit, never income "
                            "(R28). / وصف البنك هذا الإيداع بأنه فائدة: استخدم نوع الفوائد التقليدية وليس الإيراد.")
    if kind == Kind_.ISLAMIC_PROFIT and account.sharia_basis != BankAccount.ShariaBasis.ISLAMIC:
        problems.append("Returns on a conventional account are conventional interest: use conventional_interest_credit "
                        "(R27, R28). / عوائد الحساب التقليدي فوائد تقليدية: استخدم نوع الفوائد التقليدية.")
    if txn.currency_id != account.currency_id:
        problems.append("A bank transaction is in its bank account's currency. / العملية البنكية بعملة حسابها.")
    if kind == Kind_.OWN_TRANSFER:
        other = txn.to_bank_account
        if other is None:
            problems.append("An own transfer names the other own bank account (R30). / التحويل بين الحسابات يحدد الحساب الآخر.")
        elif other.entity_id != entity.pk:
            problems.append("The other account belongs to another entity: use an intercompany transfer (R30). / "
                            "الحساب الآخر يخص كيانًا آخر: استخدم التحويل بين شركات المجموعة.")
        elif other.pk == account.pk:
            problems.append("An own transfer is between two different accounts. / التحويل بين حسابين مختلفين.")
        else:
            try:
                _require_active(other, txn.to_date or txn.date)
            except BankingError as exc:
                problems += exc.messages
            if txn.to_date is not None and txn.to_date != txn.date:
                problems.append("Both legs of a transfer between currencies are on the same date (spot only, R31). / "
                                "طرفا التحويل في التاريخ نفسه (صرف فوري فقط).")
            if other.currency_id != account.currency_id:
                if txn.to_amount is None:
                    problems.append(f"Give the amount received in {other.currency_id} (R31). / حدّد المبلغ بعملة {other.currency_id}.")
            elif txn.to_amount is not None and txn.to_amount != txn.amount:
                problems.append("Same-currency transfers move one amount. / التحويل بالعملة نفسها بمبلغ واحد.")
    elif txn.to_bank_account_id or txn.to_amount is not None:
        problems.append("Only own transfers name another bank account. / التحويل بين الحسابات وحده يحدد حسابًا آخر.")
    if kind == Kind_.INTERCOMPANY_TRANSFER:
        cp, ic = txn.counterparty_entity, txn.intercompany_account
        if cp is None:
            problems.append("An intercompany transfer names the counterparty entity (R32, rule 6). / "
                            "التحويل بين شركات المجموعة يحدد الكيان المقابل.")
        elif cp.pk == entity.pk or not cp.is_active or not ledger.counterparty_choices(user, entity).filter(pk=cp.pk).exists():
            problems.append(f"{cp.code} is not another active entity of {entity.code}'s group (R32). / "
                            f"{cp.code} ليس كيانًا نشطًا آخر في مجموعة {entity.code}.")
        if ic is None or ic.entity_id != entity.pk or not ic.is_intercompany or not ic.is_postable or not ic.is_active \
                or ic.sharia_prohibited:
            problems.append("Choose an intercompany account of this entity (1210, 1220 or 2110) (R32). / "
                            "اختر حساب معاملات بين الشركات لهذا الكيان.")
        item = txn.settles_line
        if item is not None:
            problems += _settles_problems(txn, item)
    elif txn.counterparty_entity_id or txn.intercompany_account_id or txn.settles_line_id:
        problems.append("Only intercompany transfers carry a counterparty. / التحويل بين الشركات وحده يحمل طرفًا مقابلًا.")
    return problems


def _settles_problems(txn, item):
    entry = item.entry
    out = []
    if entry.entity_id != txn.entity_id or entry.status != JournalEntry.Status.POSTED \
            or item.line_kind != JournalLine.LineKind.NORMAL or item.account_id != txn.intercompany_account_id \
            or item.counterparty_entity_id != txn.counterparty_entity_id or item.currency_id != txn.currency_id:
        out.append("The item to settle is a posted line on the intercompany account, with this counterparty and "
                   "currency (R33). / البند المراد تسويته بند مرحّل على حساب المعاملات بين الشركات بالطرف والعملة نفسيهما.")
        return out
    if (item.debit > ZERO) == (txn.direction == "out"):
        out.append("Money out settles an amount owed (a credit item); money in settles a receivable (a debit item). / "
                   "الصادر يسوي مبلغًا مستحقًا علينا والوارد يسوي مبلغًا مستحقًا لنا.")
    open_txn, _ = ledger._open_item(item)
    if txn.amount > open_txn:
        out.append(f"Only {open_txn} {item.currency_id} of the item is open. / المفتوح من البند {open_txn} فقط.")
    return out


TXN_FIELDS = ("kind", "statement_line", "date", "to_date", "direction", "amount", "to_bank_account", "to_amount",
              "counterparty_entity", "intercompany_account", "settles_line", "description_en", "description_ar")


def _apply_txn_fields(txn, fields):
    account = txn.bank_account
    entity = txn.entity
    for name, value in fields.items():
        if name == "amount":
            txn.amount = _money(value, account.currency, "amount")
        elif name == "to_amount":
            other = fields.get("to_bank_account", txn.to_bank_account)
            cur = other.currency if other is not None else account.currency
            txn.to_amount = None if value is None else _money(value, cur, "amount received")
        elif name == "statement_line":
            if value is not None:
                value = StatementLine.objects.select_related("bank_account").get(pk=value.pk)
                _same_entity(entity, value, "statement line", "سطر الكشف")
            txn.statement_line = value
        elif name == "intercompany_account" and value is not None and not isinstance(value, Account):
            txn.intercompany_account = _gl(entity, value)
        elif name == "settles_line" and value is not None:
            txn.settles_line = JournalLine.objects.select_related("entry").get(pk=getattr(value, "pk", value))
        elif name == "to_bank_account" and value is not None:
            txn.to_bank_account = _account(value)
        elif name == "counterparty_entity" and value is not None and not isinstance(value, Entity):
            txn.counterparty_entity = Entity.objects.get(code=value)
        else:
            setattr(txn, name, value)


def create_bank_transaction(bank_account, *, user, kind, statement_line=None, amount=None, date=None, direction=None,
                            to_bank_account=None, to_amount=None, to_date=None, counterparty_entity=None,
                            intercompany_account=None, settles_line=None, description_en="", description_ar=""):
    """Draft a bank transaction (R25), from an unmatched statement line or manually. Agents may draft (R43).

    From a line, amount, date and direction default to the line's open amount, booking date and direction.
    Transfers between currencies use the actual bank rate (a manual rate, R31): their drafter needs
    ``ledger.use_manual_rate`` (agents never hold it).
    """
    account = _account(bank_account)
    entity = account.entity
    access.require(user, entity, "banking.create_bank_transaction")
    if statement_line is not None:
        statement_line = StatementLine.objects.select_related("bank_account").get(pk=statement_line.pk)
        _same_entity(entity, statement_line, "statement line", "سطر الكشف")
        open_amount = statement_line.amount - _consumed("statement_line", [statement_line.pk]).get(statement_line.pk, ZERO)
        if open_amount <= ZERO:
            raise error("The statement line is already fully matched.", "سطر الكشف مطابق بالكامل.", code="line_matched")
        amount = open_amount if amount is None else amount
        date = date or statement_line.booking_date
        direction = direction or ("in" if statement_line.direction == StatementLine.Direction.CREDIT else "out")
    if amount is None or date is None:
        raise error("A bank transaction needs an amount and a date.", "تحتاج العملية البنكية إلى مبلغ وتاريخ.",
                    code="txn_invalid")
    direction = direction or {Kind_.BANK_CHARGE: "out", Kind_.ISLAMIC_PROFIT: "in",
                              Kind_.CONVENTIONAL_INTEREST_CREDIT: "in"}.get(kind, "")
    en, ar = DEFAULT_DESCRIPTIONS.get(kind, ("Bank transaction", "عملية بنكية"))
    txn = BankTransaction(entity=entity, bank_account=account, kind=kind, currency=account.currency,
                          created_by=user, source=_source(user))
    _apply_txn_fields(txn, dict(
        statement_line=statement_line, date=date, to_date=to_date, direction=direction, amount=amount,
        to_bank_account=to_bank_account, to_amount=to_amount, counterparty_entity=counterparty_entity,
        intercompany_account=intercompany_account, settles_line=settles_line,
        description_en=(description_en or en)[:300], description_ar=(description_ar or ar)[:300],
    ))
    txn.requires_purification = kind == Kind_.CONVENTIONAL_INTEREST_CREDIT
    problems = _txn_problems(txn, user)
    if problems:
        raise _problems(problems, "txn_invalid")
    if _cross_currency(txn):
        access.require(user, entity, "ledger.use_manual_rate")  # R31: the actual bank rate is a manual rate (R16)
    with transaction.atomic():
        _save(txn)
        _event(entity, user, Event.BANK_TRANSACTION_CREATED, txn, {
            "kind": kind, "amount": txn.amount, "currency": txn.currency_id, "statement_line": txn.statement_line_id,
            "source": txn.source,
        })
    return txn


def create_receipt_from_line(statement_line, *, user, customer, reference_en="", reference_ar=""):
    """OQ-22 default: a draft customer receipt prefilled from an unmatched statement credit, created through
    ``invoicing.services.create_receipt`` with the caller's own invoicing rights (B7 applies to its posting).
    The customer is chosen by the user, never guessed. Nothing is matched or posted here (R24)."""
    from invoicing import services as invoicing

    line = StatementLine.objects.select_related("bank_account", "bank_account__gl_account", "entity").get(
        pk=statement_line.pk)
    access.require(user, line.entity, "banking.view")
    if line.direction != StatementLine.Direction.CREDIT:
        raise error("A receipt is drafted from a bank credit only.", "يُعد سند القبض من إيداع بنكي فقط.",
                    code="not_a_credit")
    _require_active(line.bank_account, line.booking_date)
    open_amount = line.amount - _consumed("statement_line", [line.pk]).get(line.pk, ZERO)
    if open_amount <= ZERO:
        raise error("The statement line is already fully matched.", "سطر الكشف مطابق بالكامل.", code="line_matched")
    return invoicing.create_receipt(
        line.entity, user=user, customer=customer, date=line.booking_date, amount=open_amount,
        currency=line.currency_id, bank_account=line.bank_account.gl_account,
        bank_reference=(line.bank_reference or line.end_to_end_id or line.customer_reference)[:100],
        reference_en=reference_en, reference_ar=reference_ar,
    )


def _cross_currency(txn):
    return txn.kind == Kind_.OWN_TRANSFER and txn.to_bank_account is not None \
        and txn.to_bank_account.currency_id != txn.currency_id


def _locked_txn(txn):
    return BankTransaction.objects.select_for_update(of=("self",)).select_related(
        "entity", "bank_account", "bank_account__gl_account", "bank_account__currency", "statement_line",
        "to_bank_account", "counterparty_entity", "intercompany_account", "settles_line", "settles_line__entry",
    ).get(pk=txn.pk)


def update_bank_transaction(txn, *, user, **changes):
    """Edit a draft; the editor becomes its last modifier and cannot approve it (access R14)."""
    txn = BankTransaction.objects.select_related("entity").get(pk=txn.pk)
    access.require(user, txn.entity, "banking.create_bank_transaction")
    with transaction.atomic():
        txn = _locked_txn(txn)
        if txn.status != TStatus.DRAFT:
            raise error("Only a draft bank transaction is edited.", "تُعدّل مسودة العملية البنكية فقط.", code="not_draft")
        unknown = set(changes) - set(TXN_FIELDS) - {"kind"}
        if unknown:
            raise error(f"Unknown fields {sorted(unknown)}.", f"حقول غير معروفة {sorted(unknown)}.", code="fields")
        _apply_txn_fields(txn, changes)
        txn.requires_purification = txn.kind == Kind_.CONVENTIONAL_INTEREST_CREDIT
        problems = _txn_problems(txn, user)
        if problems:
            raise _problems(problems, "txn_invalid")
        if _cross_currency(txn):
            access.require(user, txn.entity, "ledger.use_manual_rate")
        txn.modified_by, txn.modified_at = user, timezone.now()
        _save(txn)
        _event(txn.entity, user, Event.BANK_TRANSACTION_CHANGED, txn, {"fields": sorted(changes)})
        return txn


def submit_bank_transaction(txn, *, user):
    txn = BankTransaction.objects.select_related("entity").get(pk=txn.pk)
    access.require(user, txn.entity, "banking.submit_bank_transaction")
    with transaction.atomic():
        txn = _locked_txn(txn)
        if txn.status != TStatus.DRAFT:
            raise error("Only a draft is submitted.", "تُقدّم المسودة فقط.", code="not_draft")
        problems = _txn_problems(txn, user)
        if problems:
            raise _problems(problems, "txn_invalid")
        txn.status, txn.submitted_by, txn.submitted_at = TStatus.SUBMITTED, user, timezone.now()
        _save(txn)
        _event(txn.entity, user, Event.BANK_TRANSACTION_SUBMITTED, txn)
        return txn


def _next_number(entity, year):
    seq, _ = BankingSequence.objects.select_for_update().get_or_create(entity=entity, year=year)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"{entity.code}-BTX-{year}-{seq.last_number:06d}"


def approve_bank_transaction(txn, *, user):
    """R25: a second person approves a submitted bank transaction, which posts at once through the registry.

    ``create_entry(post=True, grant, document)`` with ``lines=None``: the registered ``build_lines`` builds §5.
    A refused posting (no rate, closed period, a ledger rule) changes nothing: the document stays submitted, no
    number is used and the grant stays unused (R34).
    """
    txn = BankTransaction.objects.select_related("entity").get(pk=txn.pk)
    entity = txn.entity
    grant = access.require(user, entity, "banking.approve_bank_transaction", document=txn)
    with transaction.atomic():
        txn = _locked_txn(txn)
        if txn.status != TStatus.SUBMITTED:
            raise error(f"Only a submitted bank transaction is approved; this one is {txn.status}.",
                        f"تُعتمد العملية البنكية المقدّمة فقط؛ حالتها {txn.status}.", code="not_submitted")
        if txn.modified_at and txn.modified_at > grant.checked_at:
            access.deny(user, entity, grant.permission, "grant_document_changed")
        problems = _txn_problems(txn, txn.created_by)
        if problems:
            raise _problems(problems, "txn_invalid")
        functional = entity.base_currency_id
        if txn.currency_id != functional:  # pinned at approval; the entry books at this rate (R34)
            try:
                txn.fx_exchange_rate = ledger.get_rate(entity, txn.currency_id, functional, txn.date, user=user).row
            except LedgerError as exc:
                raise _from_ledger(exc, "rate_required") from None
        now = timezone.now()
        txn.number = _next_number(entity, txn.date.year)
        txn.status = TStatus.POSTED
        txn.approved_by = txn.posted_by = user
        txn.approved_at = txn.posted_at = now
        _save(txn)
        entry_user = txn.created_by if _cross_currency(txn) else user
        try:
            with _posting(txn):
                entry = ledger.create_entry(
                    entity, txn.date, f"{txn.description_en} {txn.number} / {txn.description_ar}"[:500], None,
                    user=entry_user, reference=txn.number, post=True, grant=grant, document=txn,
                )
        except LedgerError as exc:
            raise _from_ledger(exc) from None
        txn.journal_entry = entry
        _save(txn)
        _event(entity, user, Event.BANK_TRANSACTION_APPROVED, txn, {"number": txn.number, "entry": entry.number})
        confirmer = _confirmer(txn.created_by, entity)  # the person who drafted it from the line (R20)
        for account in filter(None, (txn.bank_account, txn.to_bank_account)):
            _run_matching(_account(account), user, confirmer)
    return txn


def discard_bank_transaction(txn, *, user, reason):
    """A draft or submitted document is discarded (kept for audit, R35)."""
    txn = BankTransaction.objects.select_related("entity").get(pk=txn.pk)
    access.require(user, txn.entity, "banking.create_bank_transaction")
    with transaction.atomic():
        txn = _locked_txn(txn)
        if txn.status not in (TStatus.DRAFT, TStatus.SUBMITTED):
            raise error("Only drafts and submitted documents are discarded; reverse a posted one.",
                        "تُلغى المسودات والمستندات المقدّمة فقط؛ اعكس المرحّل.", code="not_draft")
        if not (reason or "").strip():
            raise error("Discarding needs a reason.", "الإلغاء يحتاج إلى سبب.", code="reason")
        txn.status, txn.discarded_by, txn.discarded_at, txn.discard_reason = (TStatus.DISCARDED, user, timezone.now(),
                                                                              reason[:300])
        _save(txn)
        _event(txn.entity, user, Event.BANK_TRANSACTION_DISCARDED, txn, {"reason": reason})
        return txn


def reverse_bank_transaction(txn, *, user, reason, date=None):
    """R35: reverse a posted document with ``reverse_entry`` (``banking.reverse_bank_transaction``).

    Refused while its bank line is in a confirmed match (undo the match first) and always once the match is
    locked by a reconciliation. ``date`` defaults to the document date (its period must be open).
    """
    txn = BankTransaction.objects.select_related("entity").get(pk=txn.pk)
    entity = txn.entity
    grant = access.require(user, entity, "banking.reverse_bank_transaction", document=txn)
    with transaction.atomic():
        txn = _locked_txn(txn)
        if txn.status != TStatus.POSTED or txn.journal_entry_id is None:
            raise error("Only a posted bank transaction is reversed.", "تُعكس العملية البنكية المرحّلة فقط.",
                        code="not_posted")
        if not (reason or "").strip():
            raise error("A reversal needs a reason.", "العكس يحتاج إلى سبب.", code="reason")
        parts = MatchPart.objects.filter(journal_line__entry_id=txn.journal_entry_id,
                                         match__status=Match.Status.CONFIRMED).select_related("match")
        if any(p.match.reconciliation_id for p in parts):
            raise error("Its bank line is in a match locked by an approved reconciliation; correct it with a new "
                        "document in an open period (R35).",
                        "بند البنك في مطابقة مقفلة بتسوية معتمدة؛ صحّح بمستند جديد في فترة مفتوحة.", code="match_locked")
        if parts:
            raise error("Its bank line is in a confirmed match; undo the match first (R35).",
                        "بند البنك في مطابقة مؤكدة؛ ألغِ المطابقة أولًا.", code="matched")
        try:
            reversal = ledger.reverse_entry(txn.journal_entry, grant, date=date or txn.date, reason=reason[:200])
        except LedgerError as exc:
            raise _from_ledger(exc) from None
        txn.status, txn.reversal_entry = TStatus.REVERSED, reversal
        _save(txn)
        _event(entity, user, Event.BANK_TRANSACTION_REVERSED, txn, {"reversal": reversal.number, "reason": reason})
    return txn  # the original and its reversal are paired by the next matching run (M0, R19)


# --- registered ledger hooks (ledger.documents; access R34 to R36) --------------------------------------------

def _require_posting_context(document):
    if _posting_document.get() != document.pk:
        raise error("A bank transaction posts only through banking.services.approve_bank_transaction.",
                    "تُرحَّل العملية البنكية من خلال اعتمادها في خدمات البنوك فقط.", code="post_through_services")
    if document.status != TStatus.POSTED or document.journal_entry_id is not None:
        raise error("Only an approved bank transaction that is not yet booked builds its entry.",
                    "لا يبني قيده إلا عملية معتمدة لم تُرحَّل بعد.", code="post_wrong_status")


def _spec(account, side, amount, currency, functional, counterparty=None, description=""):
    spec = {"account": account, "description": description[:300]}
    if counterparty is not None:
        spec["counterparty"] = counterparty
    if currency == functional:
        spec[side] = amount
    else:
        spec["currency"] = currency
        spec[f"txn_{side}"] = amount
    return spec


def _pinned(document, specs):
    row = document.fx_exchange_rate
    if row is None:
        return specs
    return [{**s, "exchange_rate": row} if s.get("currency") and not s.get("settles_line") and "rate" not in s else s
            for s in specs]


def _bank_transaction_lines(document):
    """Registered ``build_lines`` (§5 B1 to B8): the document's lines from its stored data only."""
    _require_posting_context(document)
    entity = document.entity
    functional = entity.base_currency_id
    account = document.bank_account
    gl = account.gl_account
    cur, amount, label = document.currency_id, document.amount, document.number
    money_in = document.direction == "in"
    bank_side, other_side = ("debit", "credit") if money_in else ("credit", "debit")
    kind = document.kind
    if kind == Kind_.BANK_CHARGE:  # B1, B2: gross to 5700, no VAT split (B8, OQ-13)
        return _pinned(document, [_spec(BANK_CHARGES, "debit", amount, cur, functional, description=label),
                                  _spec(gl, "credit", amount, cur, functional, description=label)])
    if kind == Kind_.ISLAMIC_PROFIT:  # B3
        return _pinned(document, [_spec(gl, "debit", amount, cur, functional, description=label),
                                  _spec(ISLAMIC_PROFIT, "credit", amount, cur, functional, description=label)])
    if kind == Kind_.CONVENTIONAL_INTEREST_CREDIT:  # B4: never income; to charity payable (OQ-6)
        return _pinned(document, [_spec(gl, "debit", amount, cur, functional, description=label),
                                  _spec(CHARITY_PAYABLE, "credit", amount, cur, functional,
                                        description=f"{label} purification / تطهير")])
    if kind == Kind_.OWN_TRANSFER:
        other = document.to_bank_account
        if other.currency_id == cur:  # B5
            return _pinned(document, [_spec(gl, bank_side, amount, cur, functional, description=label),
                                      _spec(other.gl_account, other_side, amount, cur, functional, description=label)])
        # B6 (R31): one spot exchange priced as ledger-fx R20, from the paying account to the receiving one.
        if money_in:
            legs = ledger._exchange_legs(entity, document.date, other.gl_account, other.currency_id, document.to_amount,
                                         gl, cur, amount, label)
        else:
            legs = ledger._exchange_legs(entity, document.date, gl, cur, amount, other.gl_account, other.currency_id,
                                         document.to_amount, label)
        return legs
    # intercompany transfer: B7, B8
    cp, ic = document.counterparty_entity, document.intercompany_account
    bank = _spec(gl, bank_side, amount, cur, functional, description=label)
    item = document.settles_line
    if item is None or cur == functional:
        return _pinned(document, [bank, _spec(ic, other_side, amount, cur, functional, cp, label)])
    rate = ledger._pinned_rate(entity, document.fx_exchange_rate, cur, functional, document.date).rate
    bank_value = fx.convert(amount, rate)
    bank = {**_pinned(document, [bank])[0], bank_side: bank_value}
    settle, settled = ledger._settlement_for_document(item, amount, f"{label} → {item.entry.number}")
    out = [bank, settle]
    debit = bank_value if bank_side == "debit" else settled
    credit = settled if bank_side == "debit" else bank_value
    diff = debit - credit
    if diff > ZERO:
        out.append({"account": ledger.FX_GAIN_REALISED, "credit": diff, "line_kind": "realised_fx",
                    "description": "Realised FX gain / أرباح فروقات العملة المحققة"})
    elif diff < ZERO:
        out.append({"account": ledger.FX_LOSS_REALISED, "debit": -diff, "line_kind": "realised_fx",
                    "description": "Realised FX loss / خسائر فروقات العملة المحققة"})
    return out


def _check_bank_transaction_lines(document, lines):
    """Registered ``validate_lines``: what any entry for this document must look like (R26 to R33)."""
    problems = []
    gl_id = document.bank_account.gl_account_id
    expected_side = "debit" if document.direction == "in" else "credit"
    bank_total = ZERO
    for line in lines:
        acc = line.account
        if acc.sharia_prohibited:
            problems.append(f"Account {acc.code} is Sharia-prohibited (R29). / الحساب {acc.code} محظور شرعًا.")
        if acc.type == Account.Type.INCOME:
            allowed = (document.kind == Kind_.ISLAMIC_PROFIT and acc.code == ISLAMIC_PROFIT) or (
                line.line_kind == JournalLine.LineKind.REALISED_FX and acc.code == ledger.FX_GAIN_REALISED)
            if not allowed:
                problems.append(f"Income account {acc.code} is not a line of a {document.kind}; conventional interest is "
                                "never income (R28). / حساب الإيراد {acc.code} ليس من بنود هذا النوع؛ الفوائد التقليدية "
                                "ليست إيرادًا أبدًا.")
        if acc.is_intercompany and line.counterparty_entity_id != document.counterparty_entity_id:
            problems.append(f"Line {acc.code} carries the document's counterparty (rule 6). / البند {acc.code} يحمل الطرف المقابل للمستند.")
        if acc.pk == gl_id and line.line_kind == JournalLine.LineKind.NORMAL:
            side = "debit" if line.debit > ZERO else "credit"
            bank_total += line.txn_amount if side == expected_side else -line.txn_amount
        if (line.currency_id != document.entity.base_currency_id and line.line_kind == JournalLine.LineKind.NORMAL
                and line.rate_source != JournalLine.RateSource.MANUAL and document.fx_exchange_rate_id is not None
                and line.currency_id == document.currency_id and line.exchange_rate_id != document.fx_exchange_rate_id):
            problems.append(f"Line {acc.code} is not at the rate pinned at approval. / البند {acc.code} ليس بالسعر المثبت عند الاعتماد.")
    if bank_total != document.amount:
        problems.append(f"The bank GL lines move {bank_total}, the document {document.amount}. / "
                        f"بنود حساب البنك {bank_total} ومبلغ المستند {document.amount}.")
    if document.kind == Kind_.CONVENTIONAL_INTEREST_CREDIT and not any(
            l.account.code == CHARITY_PAYABLE and l.credit > ZERO for l in lines):
        problems.append("Conventional interest is credited to 2600 for purification (R28). / تُقيد الفوائد التقليدية في 2600 للتطهير.")
    return problems


def _submitted_bank_transactions(entity, start_date, end_date):
    """Registered ``pending_documents`` (R41): submitted documents dated in the range block close_period."""
    return [f"{entity.code} bank transaction #{t.pk}" for t in BankTransaction.objects.filter(
        entity=entity, status=TStatus.SUBMITTED, date__gte=start_date, date__lte=end_date)]


# --- reconciliation (3.7, R36 to R41) ---------------------------------------------------------------------------

def _first_period(account, period):
    previous = FiscalPeriod.objects.filter(entity=account.entity, end_date=period.start_date - timedelta(days=1)).first()
    return previous is None or previous.end_date < max(account.active_from, account.opening_statement_date)


def _bank_balance(account, as_of):
    """R36: opening of the statement covering ``as_of`` + its lines booked up to then; statements continuous."""
    statements = list(StatementImport.objects.filter(bank_account=account, period_start__lte=as_of)
                      .order_by("period_start", "sequence_no", "pk"))
    if not statements:
        raise error(f"No statement is imported up to {as_of}; import the statements first (R36).",
                    f"لا يوجد كشف مستورد حتى {as_of}؛ استورد الكشوف أولًا.", code="statements_missing")
    for prev, nxt in zip(statements, statements[1:]):
        if nxt.period_start > prev.period_end + timedelta(days=1):
            raise error(f"Statements are missing for {prev.period_end + timedelta(days=1)}..{nxt.period_start - timedelta(days=1)} (R36).",
                        f"توجد كشوف ناقصة للفترة {prev.period_end + timedelta(days=1)}..{nxt.period_start - timedelta(days=1)}.",
                        code="statements_missing")
    cover = statements[-1]
    if cover.period_end < as_of:
        raise error(f"Statements are missing for {cover.period_end + timedelta(days=1)}..{as_of} (R36).",
                    f"توجد كشوف ناقصة للفترة {cover.period_end + timedelta(days=1)}..{as_of}.", code="statements_missing")
    net = sum((l.signed_amount for l in cover.lines.filter(booking_date__lte=as_of)), ZERO)
    return cover.opening_balance + net


def _counted_parts(account, as_of):
    """{("s"|"b", id): amount} matched by confirmed groups whose parts are all dated up to ``as_of`` (R37)."""
    counted = defaultdict(lambda: ZERO)
    matches = Match.objects.filter(bank_account=account, status=Match.Status.CONFIRMED).prefetch_related(
        "parts__statement_line", "parts__journal_line__entry")
    for match in matches:
        parts = list(match.parts.all())
        dates = [p.statement_line.booking_date if p.statement_line_id else p.journal_line.entry.date for p in parts]
        if dates and max(dates) <= as_of:
            for p in parts:
                key = ("s", p.statement_line_id) if p.statement_line_id else ("b", p.journal_line_id)
                counted[key] += p.amount
    return counted


def _figures(account, period):
    """R36 to R38: (figures, items) for the account at the period end."""
    as_of = period.end_date
    entity = account.entity
    bank_balance = _bank_balance(account, as_of)
    booked = JournalLine.objects.filter(account_id=account.gl_account_id, entry__entity=entity,
                                        entry__status__in=BOOKED, entry__date__lte=as_of)
    book = func = ZERO
    for line in booked:
        func += line.debit - line.credit
        if line.line_kind != JournalLine.LineKind.REVALUATION:
            book += line.txn_amount if line.debit > ZERO else -line.txn_amount
    counted = _counted_parts(account, as_of)
    previous = Reconciliation.objects.filter(bank_account=account, status=Reconciliation.Status.APPROVED,
                                             as_of__lt=as_of).order_by("-as_of").first()
    items, obd, obc, ubc, ubd = [], ZERO, ZERO, ZERO, ZERO
    for item in _book_items(account).filter(entry__date__lte=as_of).order_by("entry__date", "pk"):
        open_amount = item.txn_amount - counted.get(("b", item.pk), ZERO)
        if open_amount <= ZERO:
            continue
        debit = item.debit > ZERO
        if debit:
            obd += open_amount
        else:
            obc += open_amount
        late = bool(previous and item.entry.date <= previous.as_of and item.entry.posted_at
                    and previous.approved_at and item.entry.posted_at > previous.approved_at)
        items.append({"type": "book", "id": item.pk, "date": item.entry.date.isoformat(), "amount": str(open_amount),
                      "side": "debit" if debit else "credit", "reference": item.entry.number,
                      "posted_after_reconciliation": late})
    for line in StatementLine.objects.filter(bank_account=account, booking_date__lte=as_of).order_by("booking_date", "pk"):
        open_amount = line.amount - counted.get(("s", line.pk), ZERO)
        if open_amount <= ZERO:
            continue
        if line.direction == StatementLine.Direction.CREDIT:
            ubc += open_amount
        else:
            ubd += open_amount
        exception = bool(entity.sharia_enforced and line.direction == StatementLine.Direction.DEBIT
                         and _labelled_interest(line.narrative, line.transaction_code, line.bank_reference))
        items.append({"type": "bank", "id": line.pk, "date": line.booking_date.isoformat(), "amount": str(open_amount),
                      "direction": line.direction, "reference": line.bank_reference, "sharia_exception": exception})
    difference = book - (bank_balance + obd - obc - ubc + ubd)
    figures = {
        "as_of": as_of.isoformat(), "bank_balance": str(bank_balance), "book_balance": str(book),
        "book_balance_functional": str(func), "outstanding_book_debits": str(obd),
        "outstanding_book_credits": str(obc), "unmatched_bank_credits": str(ubc), "unmatched_bank_debits": str(ubd),
        "difference": str(difference),
    }
    return figures, items


def _snapshot(figures, items):
    return hashlib.sha256(json.dumps({"figures": figures, "items": items}, sort_keys=True).encode("utf-8")).hexdigest()


def _write_figures(rec, figures, items):
    for name in ("bank_balance", "book_balance", "book_balance_functional", "outstanding_book_debits",
                 "outstanding_book_credits", "unmatched_bank_credits", "unmatched_bank_debits", "difference"):
        setattr(rec, name, Decimal(figures[name]))
    rec.items = items
    rec.snapshot_sha256 = _snapshot(figures, items)


def prepare_reconciliation(bank_account, period, *, user):
    """Prepare (or re-prepare) the reconciliation of one account for one period (R36 to R39). Posts nothing."""
    account = _account(bank_account)
    entity = account.entity
    access.require(user, entity, "banking.prepare_reconciliation")
    if period.entity_id != entity.pk:
        raise error("The period belongs to another entity (R2).", "الفترة تخص كيانًا آخر.", code="other_entity")
    if account.status == BankAccount.Status.DRAFT:
        raise error("A draft bank account is not reconciled.", "لا يُسوّى الحساب البنكي المسودة.", code="account_not_active")
    if not _first_period(account, period):
        prev = FiscalPeriod.objects.get(entity=entity, end_date=period.start_date - timedelta(days=1))
        if not Reconciliation.objects.filter(bank_account=account, period=prev,
                                             status=Reconciliation.Status.APPROVED).exists():
            raise error(f"Reconcile {prev.name} first: period by period (R39).",
                        f"سوِّ الفترة {prev.name} أولًا: فترة بعد فترة.", code="previous_not_approved")
    with transaction.atomic():
        BankAccount.objects.select_for_update(of=("self",)).filter(pk=account.pk).first()
        rec = Reconciliation.objects.select_for_update(of=("self",)).filter(bank_account=account, period=period).exclude(
            status=Reconciliation.Status.SUPERSEDED).first()
        if rec is not None and rec.status == Reconciliation.Status.APPROVED:
            raise error("This period is already reconciled and approved; reopen it first (OQ-11).",
                        "هذه الفترة مسوّاة ومعتمدة؛ أعد فتحها أولًا.", code="already_approved")
        figures, items = _figures(account, period)
        if rec is None:
            rec = Reconciliation(entity=entity, bank_account=account, period=period, as_of=period.end_date,
                                 prepared_by=user)
        else:
            rec.modified_by, rec.modified_at = user, timezone.now()
            rec.status, rec.submitted_by, rec.submitted_at = Reconciliation.Status.DRAFT, None, None
        _write_figures(rec, figures, items)
        _save(rec)
        _event(entity, user, Event.RECONCILIATION_PREPARED, rec, {"period": period.name, **figures})
    return rec


def add_reconciliation_note(reconciliation, *, user, statement_line=None, journal_line=None, note_en="", note_ar="",
                            sharia_exception=False):
    """R38: explain an outstanding item (one note per item needed for unmatched statement lines)."""
    rec = Reconciliation.objects.select_related("entity").get(pk=reconciliation.pk)
    access.require(user, rec.entity, "banking.prepare_reconciliation")
    with transaction.atomic():
        if rec.status not in (Reconciliation.Status.DRAFT, Reconciliation.Status.SUBMITTED):
            raise error("Notes are added before approval.", "تُضاف الملاحظات قبل الاعتماد.", code="rec_status")
        if not ((note_en or "").strip() or (note_ar or "").strip()):
            raise error("A note needs text in English or Arabic.", "تحتاج الملاحظة إلى نص بالإنجليزية أو العربية.",
                        code="note")
        if (statement_line is None) == (journal_line is None):
            raise error("A note is for one statement line or one journal line.", "الملاحظة لسطر كشف أو بند قيد واحد.",
                        code="note")
        if statement_line is not None and statement_line.bank_account_id != rec.bank_account_id:
            raise error("The line belongs to another bank account (R2).", "السطر يخص حسابًا بنكيًا آخر.", code="other_entity")
        if journal_line is not None and journal_line.account_id != rec.bank_account.gl_account_id:
            raise error("The journal line is not on this account's GL account (R2).", "بند القيد ليس على حساب الأستاذ لهذا الحساب.",
                        code="other_entity")
        note = ReconciliationNote.objects.create(
            entity=rec.entity, reconciliation=rec, statement_line=statement_line, journal_line=journal_line,
            note_en=note_en[:500], note_ar=note_ar[:500], sharia_exception=bool(sharia_exception), created_by=user,
        )
        _event(rec.entity, user, Event.RECONCILIATION_NOTED, rec, {"note": note.pk, "sharia_exception": note.sharia_exception})
        return note


def _notes(rec):
    latest = {}
    for note in rec.notes.order_by("created_at", "pk"):
        latest[("bank", note.statement_line_id) if note.statement_line_id else ("book", note.journal_line_id)] = note
    return latest


def _approval_problems(rec):
    """R38, R39: figures unchanged since preparation, difference 0, notes on unmatched lines, nothing pending."""
    problems = []
    figures, items = _figures(rec.bank_account, rec.period)
    if _snapshot(figures, items) != rec.snapshot_sha256:
        return ["Postings, matches or imports changed since this reconciliation was prepared; re-prepare it (R39). / "
                "تغيرت القيود أو المطابقات أو الكشوف منذ إعداد هذه التسوية؛ أعد إعدادها."], items
    if rec.difference != ZERO:
        problems.append(f"The difference is {rec.difference}; it must be 0 (R38). / الفرق {rec.difference}؛ يجب أن يكون صفرًا.")
    notes = _notes(rec)
    missing = [i["id"] for i in items if i["type"] == "bank" and ("bank", i["id"]) not in notes]
    if missing:
        problems.append(f"Unmatched statement lines {missing} need a note (R38, OQ-10). / "
                        f"أسطر الكشف غير المطابقة {missing} تحتاج إلى ملاحظة.")
    pending = BankTransaction.objects.filter(bank_account=rec.bank_account, status__in=[TStatus.DRAFT, TStatus.SUBMITTED],
                                             statement_line__booking_date__lte=rec.as_of)
    if pending.exists():
        problems.append("Post or discard the draft and submitted bank transactions of this period first (R38). / "
                        "رحّل مسودات العمليات البنكية والمقدّمة لهذه الفترة أو ألغها أولًا.")
    return problems, items


def submit_reconciliation(reconciliation, *, user):
    rec = Reconciliation.objects.select_related("entity", "bank_account", "period").get(pk=reconciliation.pk)
    access.require(user, rec.entity, "banking.prepare_reconciliation")
    with transaction.atomic():
        rec = Reconciliation.objects.select_for_update(of=("self",)).select_related("bank_account", "period", "entity").get(pk=rec.pk)
        if rec.status != Reconciliation.Status.DRAFT:
            raise error("Only a draft reconciliation is submitted.", "تُقدّم التسوية المسودة فقط.", code="rec_status")
        problems, _ = _approval_problems(rec)
        if problems:
            raise _problems(problems, "reconciliation_refused")
        rec.status, rec.submitted_by, rec.submitted_at = Reconciliation.Status.SUBMITTED, user, timezone.now()
        _save(rec)
        _event(rec.entity, user, Event.RECONCILIATION_SUBMITTED, rec)
        return rec


def approve_reconciliation(reconciliation, *, user):
    """R39, R40: an approver who did not prepare or submit it approves; matches up to the period end are locked."""
    rec = Reconciliation.objects.select_related("entity").get(pk=reconciliation.pk)
    access.require(user, rec.entity, "banking.approve_reconciliation", document=rec)
    with transaction.atomic():
        rec = Reconciliation.objects.select_for_update(of=("self",)).select_related(
            "bank_account", "period", "entity").get(pk=rec.pk)
        if rec.status != Reconciliation.Status.SUBMITTED:
            raise error("Only a submitted reconciliation is approved.", "تُعتمد التسوية المقدّمة فقط.", code="rec_status")
        BankAccount.objects.select_for_update(of=("self",)).filter(pk=rec.bank_account_id).first()
        problems, items = _approval_problems(rec)
        if problems:
            raise _problems(problems, "reconciliation_refused")
        notes = _notes(rec)
        for item in items:
            note = notes.get((item["type"], item["id"]))
            if note is not None:
                item.update(note_en=note.note_en, note_ar=note.note_ar)
                if note.sharia_exception:
                    item["sharia_exception"] = True
        rec.items = items
        rec.status, rec.approved_by, rec.approved_at = Reconciliation.Status.APPROVED, user, timezone.now()
        _save(rec)
        locked = 0
        for match in Match.objects.filter(bank_account=rec.bank_account, status=Match.Status.CONFIRMED,
                                          reconciliation__isnull=True).prefetch_related(
                "parts__statement_line", "parts__journal_line__entry"):
            dates = [p.statement_line.booking_date if p.statement_line_id else p.journal_line.entry.date
                     for p in match.parts.all()]
            if dates and max(dates) <= rec.as_of:
                match.reconciliation = rec
                _save(match)
                locked += 1
        _event(rec.entity, user, Event.RECONCILIATION_APPROVED, rec, {"locked_matches": locked})
    return rec


def reopen_reconciliation(reconciliation, *, user, reason):
    """R40, OQ-11: the latest approved reconciliation of an account, by someone who did not prepare it, with a
    reason; it becomes ``superseded`` and its locks are released."""
    rec = Reconciliation.objects.select_related("entity").get(pk=reconciliation.pk)
    access.require(user, rec.entity, "banking.reopen_reconciliation", document=rec)
    with transaction.atomic():
        rec = Reconciliation.objects.select_for_update(of=("self",)).get(pk=rec.pk)
        if not (reason or "").strip():
            raise error("Reopening needs a reason.", "إعادة الفتح تحتاج إلى سبب.", code="reason")
        if rec.status != Reconciliation.Status.APPROVED:
            raise error("Only an approved reconciliation is reopened.", "يُعاد فتح التسوية المعتمدة فقط.", code="rec_status")
        if Reconciliation.objects.filter(bank_account=rec.bank_account_id, status=Reconciliation.Status.APPROVED,
                                         as_of__gt=rec.as_of).exists():
            raise error("Only the latest approved reconciliation of the account is reopened (OQ-11).",
                        "يُعاد فتح آخر تسوية معتمدة للحساب فقط.", code="not_latest")
        rec.status = Reconciliation.Status.SUPERSEDED
        rec.reopened_by, rec.reopened_at, rec.reopen_reason = user, timezone.now(), reason[:300]
        _save(rec)
        released = 0
        for match in Match.objects.filter(reconciliation=rec):
            match.reconciliation = None
            _save(match)
            released += 1
        _event(rec.entity, user, Event.RECONCILIATION_REOPENED, rec, {"reason": reason, "released_matches": released})
        return rec


# --- reports and views (R4, R28, R46) ---------------------------------------------------------------------------

def _masked_text(text):
    return "*** / مخفي" if text else ""


def statement_lines(bank_account, *, user):
    """Statement lines of an account for a list: counterparty name, IBAN and narrative masked without
    ``banking.view_iban`` (R4, OQ-23)."""
    account = _account(bank_account)
    access.require(user, account.entity, "banking.view")
    _agent_read(user, account.entity, f"statement_lines:{account.pk}")
    unmasked = access.has(user, account.entity, "banking.view_iban")
    rows = []
    for line in StatementLine.objects.filter(bank_account=account).order_by("booking_date", "pk"):
        rows.append({
            "id": line.pk, "line_no": line.line_no, "booking_date": line.booking_date, "direction": line.direction,
            "amount": line.amount, "currency": line.currency_id, "bank_reference": line.bank_reference,
            "state": _line_state(line),
            "counterparty_name": line.counterparty_name if unmasked else _masked_text(line.counterparty_name),
            "counterparty_iban": line.counterparty_iban if unmasked else (
                iban_rules.masked(line.counterparty_iban, line.counterparty_iban[-4:]) if line.counterparty_iban else ""),
            "narrative": line.narrative if unmasked else _masked_text(line.narrative),
        })
    return rows


def purification_report(entity, *, user):
    """R28: posted bank credits of conventional interest booked to 2600 for purification (payout: invoicing E15)."""
    access.require(user, entity, "banking.view")
    rows = BankTransaction.objects.filter(entity=entity, requires_purification=True,
                                          status=TStatus.POSTED).order_by("date", "number")
    return {
        "lines": [{"number": t.number, "date": t.date, "amount": t.amount, "currency": t.currency_id,
                   "bank_account": t.bank_account.code} for t in rows],
        "total_by_currency": {cur: sum((t.amount for t in rows if t.currency_id == cur), ZERO)
                              for cur in {t.currency_id for t in rows}},
    }


def reconciliation_statement(reconciliation, *, user):
    """The reconciliation statement in English and Arabic (R46)."""
    rec = Reconciliation.objects.select_related("entity", "bank_account__bank", "period").get(pk=reconciliation.pk)
    access.require(user, rec.entity, "banking.view")
    account = rec.bank_account
    cur = account.currency_id
    rows = [
        f"Bank reconciliation / التسوية البنكية — {rec.entity.name_en} / {rec.entity.name_ar}",
        f"Bank / البنك: {account.bank.name_en} / {account.bank.name_ar} — {account.code} {account.masked_number}",
        f"Period / الفترة: {rec.period.name} (as of / حتى {rec.as_of}) — Status / الحالة: {rec.get_status_display()}",
        f"Balance per bank statement / الرصيد حسب كشف البنك: {rec.bank_balance} {cur}",
        f"Add: deposits in transit / يضاف: إيداعات بالطريق: {rec.outstanding_book_debits} {cur}",
        f"Less: unpresented payments / يطرح: مدفوعات لم تُقدّم: {rec.outstanding_book_credits} {cur}",
        f"Less: bank credits not in books / يطرح: إيداعات بنكية غير مقيدة: {rec.unmatched_bank_credits} {cur}",
        f"Add: bank debits not in books / يضاف: سحوبات بنكية غير مقيدة: {rec.unmatched_bank_debits} {cur}",
        f"Balance per books / الرصيد حسب الدفاتر: {rec.book_balance} {cur} "
        f"({rec.book_balance_functional} {rec.entity.base_currency_id})",
        f"Difference / الفرق: {rec.difference} {cur}",
        "Outstanding items / البنود المعلقة:",
    ]
    for item in rec.items:
        flags = []
        if item.get("sharia_exception"):
            flags.append("Sharia exception / استثناء شرعي")
        if item.get("posted_after_reconciliation"):
            flags.append("posted after reconciliation / رُحّل بعد التسوية")
        rows.append(f"  {item['date']} {item['type']} {item['amount']} {item.get('reference', '')} "
                    f"{' '.join(filter(None, (item.get('note_en'), item.get('note_ar'))))} {'; '.join(flags)}".rstrip())
    return "\n".join(rows)
