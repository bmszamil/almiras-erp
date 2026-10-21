"""Invoicing services (docs/specs/invoicing.md), slice 1. The only supported way to change invoicing data.

Every public function checks rights with ``core.access.require`` for the
entity that owns the record (access R1). Everything that affects the books
goes through ``ledger.services`` with the document's registered line hooks
(``ledger.documents``): the document builds its own journal lines from its
stored data, never from lines a caller passes.

Workflow (D17): staff create and submit drafts; the approver (CFO) approves.
Bills and vendor payments post on approval; sales documents are issued on
approval and post when ZATCA clears or reports them (R40, R41); customer
receipts may be posted by the person who recorded them (D26, access R46);
refunds are posted by a second person (R33, OQ-26).
"""
import calendar
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import Case, Count, F, Q, Sum, Value, When
from django.utils import timezone

from core import access
from core.access import services as access_services
from core.access.models import AccessLog, Kind, Membership
from core.models import Currency, Entity, label_part, validate_country_code, validate_vat_number
from core.services import entity_ready_to_issue
from ledger import fx
from ledger import restricted as ledger_restricted
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine, LedgerError

from banking import iban as iban_rules
from banking import services as banking
from banking.crypto import keyed_hash
from banking.models import Bank

from . import zatca_port
from .models import (
    BUYER_ID_PRIORITY,
    CharityBoardApproval,
    CharityRecipient,
    CharityRecipientAlert,
    CustomerReceipt,
    Customer,
    CustomerIdentifier,
    DeferredProfitRelease,
    DeferredSale,
    DeferredSaleAdjustment,
    DocumentSequence,
    IncomeType,
    InvoicingError,
    InvoicingSettings,
    NoteReason,
    PayableAllocation,
    PaymentApplication,
    PaymentMeans,
    PurchaseDocument,
    PurchaseDocumentLine,
    PurificationInstruction,
    PurificationTransfer,
    ReceiptApplication,
    ReceivableAllocation,
    SalesDocument,
    SalesDocumentLine,
    VatCategory,
    VatExemptionReason,
    VatRate,
    TreatyCertificate,
    Vendor,
    validate_treaty_document,
    VendorPayment,
    WhtCategory,
    WhtDeduction,
    billing_line_writes,
    error,
)

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")
SAR = "SAR"
OUT_OF_SCOPE_CODE = "VATEX-SA-OOS"  # BT-120 is free text for this code (row 1c)
CITIZEN_CODES = frozenset({"VATEX-SA-EDU", "VATEX-SA-HEA"})  # buyer NAT ID required (BR-KSA-49, row 1d)
STANDARD_INVOICE_DAYS = 15  # TIR §4.5: within 15 days from the end of the month of supply (row 6f)
SIMPLIFIED_LIMIT_SAR = Decimal("1000.00")  # R48: "less than SAR 1,000"; gross in SAR by default (OQ-44)

# Accounts (ledger/coa_template.py; invoicing §5).
AR, AR_GROUP, AP, AP_GROUP = "1200", "1210", "2100", "2110"
VAT_OUTPUT, VAT_INPUT, BANK, BANK_CHARGES = "2200", "1310", "1100", "5700"
WHT_PAYABLE, WHT_BORNE = "2220", "5920"  # PROPOSED accounts (§5, E20 to E22; D29)
NON_COMPLIANT_INCOME, CHARITY_PAYABLE = "4900", "2600"  # E14, E15
PURIFICATION_BANK = "1140"  # approved (owner decision C7): the purification account's GL; only purification rows use it
# Owner decision C6 (2026-10-18): the IFRS presentation of the purification account (restricted cash, IAS 7).
PURIFICATION_CASH_NOTE_EN = (
    "Restricted cash - purification account (1140): money set aside to be paid to charity (non-compliant income and "
    "any return earned on the account). It is held for charity, cannot be returned to the company and is not "
    "available for the company's use; it is presented apart from cash and bank and excluded from cash and cash "
    "equivalents in the statement of cash flows (IAS 7 / IFRS for SMEs section 7), with this disclosure. The "
    "matching obligation is the charity payable (2600).")
PURIFICATION_CASH_NOTE_AR = (
    "نقد مقيد - حساب التطهير (1140): أموال مجنبة لتُصرف للجهات الخيرية (الإيرادات غير المتوافقة وأي عائد على الحساب). "
    "تُحفظ لصالح الجهات الخيرية ولا تعود إلى الشركة وليست متاحة لاستخدامها؛ تُعرض منفصلة عن النقد والبنوك وتُستبعد "
    "من النقد وما في حكمه في قائمة التدفقات النقدية (معيار المحاسبة الدولي 7 / القسم 7 من المعيار الدولي للمنشآت "
    "الصغيرة والمتوسطة) مع هذا الإفصاح. والالتزام المقابل هو المستحق للجهات الخيرية (2600).")
CONTROL_ACCOUNTS = frozenset({AR, AR_GROUP, AP, AP_GROUP, VAT_OUTPUT, VAT_INPUT, WHT_PAYABLE, PURIFICATION_BANK})
# R112 / E28 (payroll R47, owner decision E8): the bai' ajel sale to an employee. Its receivable is 1240, its lines
# credit 1250 (cost) and 1241 (deferred profit); profit moves from 1241 to 4610 as the instalments are collected.
EMPLOYEE_RECEIVABLE, DEFERRED_PROFIT, SALE_ASSET, SALE_PROFIT_INCOME = "1240", "1241", "1250", "4610"
DEFERRED_SALE_LINE_ACCOUNTS = frozenset({SALE_ASSET, DEFERRED_PROFIT})
# Never on a line a person enters (sales or purchase): only the bai' ajel sale and its profit recognition post them
# in invoicing (payroll credits 1240 when it collects). 1250 stays open to purchase bills (E9).
DEFERRED_SALE_ONLY_ACCOUNTS = frozenset({EMPLOYEE_RECEIVABLE, DEFERRED_PROFIT, SALE_PROFIT_INCOME})
DEFERRED_SALE_INCOME = "deferred_sale"  # income type (reference data V3)
DEFERRED_SALE_SOURCE = "system:deferred_sale"
DEFERRED_SALE_SYSTEM = "deferred_sale"  # system user code (access seed V15)
# True only inside create_deferred_sale: the one writer of bai' ajel lines.
_deferred_sale_writes = ContextVar("invoicing_deferred_sale_writes", default=False)

Status = SalesDocument.Status
PStatus = PurchaseDocument.Status
SEQUENCE_CODES = {
    "invoice": "INV", "credit_note": "CN", "debit_note": "DN", "bill": "BILL", "vendor_credit_note": "VCN",
}

RIYADH = ZoneInfo("Asia/Riyadh")


def _riyadh_now():
    """Current time in Asia/Riyadh: the issue date and time of a sales document (BR-KSA-04, TIR §4.2.3).

    A function so tests can move the approval day (invoicing.testing.approval_day).
    """
    return timezone.now().astimezone(RIYADH)


# The person whose ledger.view right is used while a document's lines are built (fx_settlement_lines, R18).
_posting_user = ContextVar("invoicing_posting_user", default=None)


def _problems_error(problems, code):
    exc = InvoicingError(list(problems), code=code)
    exc.code = code
    return exc


# --- small helpers --------------------------------------------------------------------------------

def _on_line(line_no, problem):
    """Prefix a bilingual problem with its line number in both languages (nit 12)."""
    en, sep, ar = problem.partition(" / ")
    return f"Line {line_no}: {en} / البند {line_no}: {ar}" if sep else f"Line {line_no}: {problem}"


def _reference(model, code, what_en, what_ar):
    """A reference row by code; a bilingual error instead of DoesNotExist (nit 12)."""
    obj = model.objects.filter(code=code).first()
    if obj is None:
        raise error(f"Unknown {what_en} {code!r}.", f"{what_ar} {code!r} غير معروف.", code="unknown_reference")
    return obj


def _quantum(places):
    return Decimal(1).scaleb(-places)


def _round(amount, places=2):
    """ROUND_HALF_UP to ``places`` decimals (R9; 2 for SAR, the currency's minor units in the document currency)."""
    with localcontext() as ctx:
        ctx.prec = 60
        return amount.quantize(_quantum(places), rounding=ROUND_HALF_UP)


def _places(currency):
    """Decimals of a document currency (D27, R93): the Currency's ``minor_units`` (2 or 3)."""
    if currency is None:
        return 2
    if not isinstance(currency, Currency):
        currency = Currency.objects.get(code=currency)
    return currency.minor_units


def _doc_places(doc):
    return _places(doc.currency)


def _fmt(amount, places):
    """An amount as text with exactly ``places`` decimals (VAT breakdown JSON), whatever the column stores."""
    return str(Decimal(amount).quantize(_quantum(places)))


def _decimal(value, what):
    """Decimal from Decimal, int or string; floats are rejected (R56). LedgerError is re-raised bilingual."""
    try:
        return fx.to_decimal(value, what)
    except LedgerError as exc:
        raise InvoicingError(exc.messages, code="float_rejected" if isinstance(value, float) else "not_a_number") from None


def _money(value, what, *, positive=False, currency=None):
    """A Decimal amount with at most the currency's decimals (D27, R93; ledger-fx R8): 2 for SAR, USD, ...,
    3 for KWD, BHD, OMR, JOD. Without ``currency``: 2 decimals."""
    amount = _decimal(value, what)
    places = _places(currency)
    code = currency.code if isinstance(currency, Currency) else (currency or "")
    if not fx.has_places(amount, places):
        raise error(f"{what}: {code + ' ' if code else ''}amounts have at most {places} decimals ({amount}).",
                    f"{what}: المبالغ {('بعملة ' + code + ' ') if code else ''}بحد أقصى {places} خانات عشرية "
                    f"({amount}).", code="money_decimals")
    if amount < ZERO or (positive and amount == ZERO):
        raise error(f"{what} must be {'above' if positive else 'at least'} zero.",
                    f"يجب أن يكون {what} {'أكبر من' if positive else 'لا يقل عن'} صفر.", code="money_sign")
    return amount.quantize(_quantum(places))


def _source(user):
    return access_services.source_of(user) or "human"


def _log_agent(user, entity, permission, document, action):
    """Gateway rule: every agent action is logged (D8, access R21)."""
    if access_services.kind_of(user) == Kind.AGENT:
        access_services.log(
            AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=entity, permission=permission,
            details={"document": [document._meta.label_lower, document.pk], "action": action},
        )


SUPPORTED_MINOR_UNITS = (2, 3)  # D27 / R93: 2-decimal currencies and KWD, BHD, OMR, JOD; 0 (JPY) refused (OQ-58)


def _currency(code, entity):
    """D13, D27 (R93): an active currency with 2 or 3 minor units. 0-decimal currencies stay refused (OQ-58)."""
    cur = Currency.objects.filter(code=str(code or entity.base_currency_id).upper()).first()
    if cur is None or not cur.is_active:
        raise error(f"Currency {code} is unknown or inactive.", f"العملة {code} غير معروفة أو غير نشطة.",
                    code="currency_inactive")
    if cur.minor_units not in SUPPORTED_MINOR_UNITS:
        raise error(
            f"Documents in {cur.code} ({cur.minor_units} decimals) are not supported; currencies with 2 or 3 decimals "
            "are (OQ-58).",
            f"المستندات بعملة {cur.code} ({cur.minor_units} خانات عشرية) غير مدعومة؛ المدعومة عملات بخانتين أو ثلاث "
            "خانات عشرية.",
            code="currency_decimals",
        )
    return cur


def _settings(entity):
    obj, _ = InvoicingSettings.objects.get_or_create(entity=entity)
    return obj


def _next_number(entity, doc_type, year):
    """Gap-free per (entity, type, year), locked until the caller's transaction ends (R14, OQ-4)."""
    code = SEQUENCE_CODES.get(doc_type, doc_type)
    seq, _ = DocumentSequence.objects.select_for_update(of=("self",)).get_or_create(entity=entity, doc_type=code, year=year)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"{entity.code}-{code}-{year}-{seq.last_number:06d}"


def _open_period(entity, on):
    """R28 / AT-17: an open fiscal period of ``entity`` covers ``on``; else a bilingual error (nothing is used)."""
    period = FiscalPeriod.objects.filter(entity=entity, start_date__lte=on, end_date__gte=on).first()
    if period is None:
        return f"No fiscal period covers {on} for {entity.code}. / لا توجد فترة مالية تغطي {on} للكيان {entity.code}."
    if period.status != FiscalPeriod.Status.OPEN:
        return f"Period {period.name} is closed for {entity.code}. / الفترة {period.name} مقفلة للكيان {entity.code}."
    return None


def _account(entity, value):
    if isinstance(value, Account):
        return value
    account = Account.objects.filter(entity=entity, code=value).first()
    if account is None:
        raise error(f"Account {value} does not exist in {entity.code}.", f"الحساب {value} غير موجود في {entity.code}.",
                    code="account_missing")
    return account


def _account_problems(entity, account, types, what):
    """R1, R2, R5: the account is the entity's own, postable, of the right type and allowed by Sharia."""
    problems = []
    if account.entity_id != entity.pk:
        problems.append(f"Account {account.code} belongs to another entity. / الحساب {account.code} يخص كيانًا آخر.")
        return problems
    if not account.is_postable or not account.is_active:
        problems.append(f"Account {account.code} is not open for posting. / الحساب {account.code} غير مفتوح للترحيل.")
    if account.type not in types:
        problems.append(f"Account {account.code} is not a valid {what} account. / الحساب {account.code} غير صالح لهذا البند.")
    if account.code in CONTROL_ACCOUNTS:
        problems.append(f"Account {account.code} is a control or VAT account; it is set by the document. / "
                        f"الحساب {account.code} حساب رقابة أو ضريبة يحدده المستند.")
    if account.code in DEFERRED_SALE_ONLY_ACCOUNTS:
        problems.append(f"Account {account.code} is posted only by a bai' ajel sale approved in payroll and its profit "
                        f"recognition (R112). / الحساب {account.code} لا يُرحَّل إلا من خلال بيع آجل معتمد في الرواتب "
                        "وتحقق أرباحه.")
    if account.sharia_prohibited and entity.sharia_enforced:
        problems.append(f"Account {account.code} ({account.name_en}) is Sharia-prohibited. / "
                        f"الحساب {account.code} ({account.name_ar}) محظور شرعًا.")
    return problems


def _rate_on(category, on):
    """R10: the VatRate of ``category`` effective on ``on``."""
    qs = VatRate.objects.filter(category=category, valid_from__lte=on).order_by("-valid_from")
    for rate in qs:
        if rate.valid_to is None or rate.valid_to >= on:
            return rate
    return None


def _fx_for(entity, currency_code, on, user):
    """(rate, rate_date, source, rate row) to the functional currency on ``on`` (ledger-fx R12); bilingual error
    if none. The row is pinned on the document: its postings use that rate (review item 2)."""
    if currency_code == entity.base_currency_id:
        return Decimal("1"), on, "functional", None
    try:
        found = ledger.get_rate(entity, currency_code, entity.base_currency_id, on, user=user)
    except LedgerError as exc:
        raise _problems_error(
            [f"An exchange rate is required for {currency_code} on {on}. / سعر الصرف مطلوب للعملة {currency_code} "
             f"بتاريخ {on}.", *exc.messages], "rate_required") from None
    return found.rate, found.row.date if found.row else on, found.row.source if found.row else "table", found.row


def _vat_sar_rate(entity, currency_code, tax_point, user):
    """R100 as amended (G7, I2; VAT IR Art. 61, TIR §4.2.2): (rate, SAMA row) from the document currency to SAR for
    ``total_vat_sar`` and ``vat_breakdown.vat_sar``: the SAMA rate on the tax point, else the latest SAMA rate
    published before it (``ledger.get_sama_rate``). No SAMA rate on or before it: the document is not issued."""
    if currency_code == SAR:
        return Decimal("1"), None
    try:
        found = ledger.get_sama_rate(currency_code, tax_point, user=user, entity=entity)
    except LedgerError as exc:
        raise _problems_error(
            [f"The SAMA {currency_code}->SAR rate on the tax point {tax_point} is required to issue this document "
             f"(VAT in SAR, R100). / سعر البنك المركزي السعودي {currency_code} إلى الريال في تاريخ الاستحقاق "
             f"الضريبي {tax_point} مطلوب لإصدار هذا المستند (الضريبة بالريال).", *exc.messages],
            "sama_rate_missing") from None
    return found.rate, found.row


SAMA_TAX_POINT = "sama_tp"  # SalesDocument.fx_rate_source: booked at the SAMA tax-point row beyond the age limit


def _booking_rate(entity, doc, sama_row, user):
    """(rate, rate date, source, row) the document books at (R77; R100(d) as amended by owner decision H2, OQ-65):
    in a SAR-functional entity a foreign-currency document books **all** its lines at its SAMA tax-point row, so 2200
    equals ``total_vat_sar``. A row the ledger accepts as a pinned rate on the issue date (``ledger._pinned_rate``,
    not older than ``FX_MAX_RATE_AGE_DAYS``) books as a table rate; an older one (tax point well before the issue
    date, or a long gap in SAMA rates, I2) books as ``sama_tp``, which the ledger checks against the document's tax
    point (ledger-fx R51, ``_sama_tax_point``). A tax point **after** the issue date (a B2G invoice whose payment
    order date is later, R117 / I3) books at the R12 rate on the issue date, like other functional currencies: an
    entry dated on the issue date cannot use a rate of a later day (R51); the VAT return tie-out explains the
    difference between 2200 and the VAT in SAR (tax-returns §10.7, ``rate_difference_output``)."""
    cur = doc.currency_id
    if (sama_row is not None and entity.base_currency_id == SAR and cur != SAR
            and doc.tax_point_date <= doc.issue_date):
        pinned = ledger._pinned_rate(entity, sama_row, cur, SAR, doc.issue_date)
        if pinned is not None and not pinned.inverted:
            return pinned.rate, sama_row.date, sama_row.source, sama_row
        return sama_row.rate, sama_row.date, SAMA_TAX_POINT, sama_row
    return _fx_for(entity, cur, doc.issue_date, user)


def _sama_tax_point(document):
    """Registered ``sama_tax_point`` hook (ledger-fx R51, owner decisions H2, I2): ``(tax point, issued at)`` of a
    stored sales document booked at its SAMA tax-point row beyond the ledger's rate age limit, else None. The issue
    time (Riyadh, BR-KSA-04) is when its SAMA row was chosen: a SAMA row recorded later does not undo it."""
    if document.fx_rate_source == SAMA_TAX_POINT and document.fx_exchange_rate_id == document.vat_sar_exchange_rate_id:
        if document.issue_time is None:
            return document.tax_point_date
        issued_at = datetime.combine(document.issue_date, document.issue_time, tzinfo=RIYADH)
        return document.tax_point_date, issued_at
    return None


# --- settings ----------------------------------------------------------------------------------------

def invoicing_settings(entity, *, user):
    """The entity's InvoicingSettings (created with defaults). ``user`` needs ``invoicing.view``."""
    access.require(user, entity, "invoicing.view")
    return _settings(entity)


def update_settings(entity, *, user, **changes):
    """Change InvoicingSettings (``invoicing.manage_settings``, approver). ``deposit_vat_treatment`` stays
    unset until the tax adviser signs off (R66, OQ-16)."""
    access.require(user, entity, "invoicing.manage_settings")
    allowed = {"segregation_required", "auto_issue_from_approved_contract"}
    unknown = set(changes) - allowed
    if unknown:
        raise error(f"Not a setting that can be changed here: {', '.join(sorted(unknown))}.",
                    "ليس من الإعدادات التي يمكن تغييرها هنا.", code="unknown_setting")
    with transaction.atomic():
        obj = _settings(entity)
        for name, value in changes.items():
            setattr(obj, name, bool(value))
        obj.updated_by = user
        obj.save()
    return obj


# --- parties (3.1) ----------------------------------------------------------------------------------

def party_problems(party):
    """Problems with a customer or vendor ("English / العربية"); empty means it can be saved (R4, R16, AT-36).

    Pure check on the record passed; called from model ``clean`` and the party services.
    """
    problems = []
    if not (party.name_ar or "").strip():
        problems.append("The Arabic name is required. / الاسم بالعربية مطلوب.")
    if party.is_vat_registered and not (party.vat_number or "").strip():
        problems.append("A VAT-registered party needs its VAT number. / الطرف المسجل في الضريبة يحتاج إلى رقمه الضريبي.")
    if party.vat_number:
        try:
            validate_vat_number(party.vat_number)
        except Exception:  # noqa: BLE001 - ValidationError carries the bilingual message
            problems.append("VAT number must be 15 digits and start and end with 3. / "
                            "يجب أن يتكون الرقم الضريبي من 15 رقمًا ويبدأ وينتهي بالرقم 3.")
        if not party.is_vat_registered:
            problems.append("A VAT number is given but the party is not marked VAT registered. / "
                            "أُدخل رقم ضريبي والطرف غير مسجل في الضريبة.")
    if party.group_entity_id is not None and party.group_entity_id == party.entity_id:
        problems.append("A party cannot be its own entity (group entity = entity). / لا يكون الطرف هو الكيان نفسه.")
    try:
        validate_country_code(party.country)
    except Exception:  # noqa: BLE001
        problems.append(f"{party.country!r} is not an ISO country code. / {party.country!r} ليس رمز دولة صالحًا.")
    if party.country == "SA" and party.postal_code and not (len(party.postal_code) == 5 and party.postal_code.isdigit()):
        problems.append("A Saudi postal code has 5 digits. / الرمز البريدي السعودي من 5 أرقام.")
    if isinstance(party, Vendor) and party.vendor_kind not in Vendor.Kind.values:  # D24 / R91: required
        problems.append("The vendor kind (business, individual or government) is required. / "
                        "نوع المورد (منشأة أو فرد أو جهة حكومية) مطلوب.")
    if isinstance(party, Vendor) and party.wht_category_id is not None:
        if party.is_resident:  # D14: withholding tax is for payments to non-residents only
            problems.append("A withholding tax category is set only on non-resident vendors. / "
                            "تُحدَّد فئة ضريبة الاستقطاع للموردين غير المقيمين فقط.")
        if party.wht_category.sharia_prohibited and party.entity.sharia_enforced:
            problems.append(f"Withholding tax category {party.wht_category.code} (loan charges) never arises where "
                            "Sharia is enforced (OQ-8, R5). / فئة ضريبة الاستقطاع (أعباء القروض) لا تنشأ في كيان يطبق "
                            "الضوابط الشرعية.")
    if isinstance(party, Vendor) and not party.is_resident:
        try:
            validate_country_code(party.residence_country)
            if party.residence_country == "SA":
                raise ValueError
        except Exception:  # noqa: BLE001
            problems.append("A non-resident vendor needs its country of residence (not SA). / "
                            "المورد غير المقيم يحتاج إلى دولة إقامته (غير المملكة).")
    return problems


PARTY_FIELDS = {
    "code", "name_en", "name_ar", "is_vat_registered", "vat_number", "group_entity", "payment_terms_days", "currency",
    "is_active", "street_en", "street_ar", "building_no", "additional_no", "district_en", "district_ar", "city_en",
    "city_ar", "postal_code", "country",
}
CUSTOMER_FIELDS = PARTY_FIELDS | {"customer_kind"}
WHT_VENDOR_FIELDS = {"wht_category", "wht_gross_up"}  # D14: entered by holders of invoicing.enter_wht_data
VENDOR_FIELDS = PARTY_FIELDS - {"payment_terms_days"} | {"vendor_kind", "is_resident", "residence_country",
                                                          "bank_iban"} | WHT_VENDOR_FIELDS


def _save_party(party, user, fields, allowed):
    unknown = set(fields) - allowed
    if unknown:
        raise error(f"Unknown fields: {', '.join(sorted(unknown))}.", "حقول غير معروفة.", code="unknown_field")
    for name, value in fields.items():
        if name == "currency":
            value = _currency(value, party.entity)
        if name == "wht_category" and value is not None and not isinstance(value, WhtCategory):
            value = _wht_category(value)
        if name == "wht_gross_up":
            value = bool(value)
        setattr(party, name, value)
    if party.currency_id is None:
        party.currency_id = party.entity.base_currency_id
    problems = party_problems(party)
    try:
        party.full_clean(exclude=["name_ar", "vendor_kind"])
    except Exception as exc:  # noqa: BLE001 - field validators carry bilingual messages
        problems += [m for m in getattr(exc, "messages", [str(exc)]) if m not in problems]
    if problems:
        raise _problems_error(problems, "party_invalid")
    party.save()
    return party


@transaction.atomic
def create_customer(entity, *, user, **fields):
    """Create a customer in ``entity`` (``invoicing.manage_customer``; OQ-14: parties are per entity)."""
    access.require(user, entity, "invoicing.manage_customer")
    return _save_party(Customer(entity=entity, created_by=user, source=_source(user)), user, fields, CUSTOMER_FIELDS)


@transaction.atomic
def update_customer(customer, *, user, **fields):
    """Change a customer. Issued documents keep their frozen buyer snapshot (R15)."""
    customer = Customer.objects.select_for_update(of=("self",)).get(pk=customer.pk)
    access.require(user, customer.entity, "invoicing.manage_customer")
    customer.modified_by, customer.modified_at = user, timezone.now()
    return _save_party(customer, user, fields, CUSTOMER_FIELDS)


def add_customer_identifier(customer, scheme, value, *, user):
    """Record a buyer ID; the first in the §3.1 priority order is used on documents (R16, AT-61)."""
    access.require(user, customer.entity, "invoicing.manage_customer")
    if scheme not in BUYER_ID_PRIORITY:
        raise error(f"Unknown buyer ID scheme {scheme!r}.", f"نوع الهوية {scheme!r} غير معروف.", code="id_scheme")
    ident = CustomerIdentifier(entity=customer.entity, customer=customer, scheme=scheme, value=str(value).strip())
    try:
        ident.full_clean(exclude=["entity", "customer"])
    except Exception as exc:  # noqa: BLE001
        raise _problems_error(getattr(exc, "messages", [str(exc)]), "id_invalid") from None
    ident.save()
    return ident


@transaction.atomic
def create_vendor(entity, *, user, **fields):
    """Create a vendor in ``entity`` (``invoicing.manage_vendor``). ``vendor_kind`` is required (D24, R91)."""
    access.require(user, entity, "invoicing.manage_vendor")
    if set(fields) & WHT_VENDOR_FIELDS:
        access.require(user, entity, "invoicing.enter_wht_data")
    return _save_party(Vendor(entity=entity, created_by=user, source=_source(user)), user, fields, VENDOR_FIELDS)


@transaction.atomic
def update_vendor(vendor, *, user, **fields):
    vendor = Vendor.objects.select_for_update(of=("self",)).get(pk=vendor.pk)
    access.require(user, vendor.entity, "invoicing.manage_vendor")
    if set(fields) & WHT_VENDOR_FIELDS:
        access.require(user, vendor.entity, "invoicing.enter_wht_data")
    vendor.modified_by, vendor.modified_at = user, timezone.now()
    return _save_party(vendor, user, fields, VENDOR_FIELDS)


def customers(user, entity=None):
    """Customers ``user`` may see (R3): scoped by ``invoicing.view``."""
    qs = access.scope(Customer.objects.all(), user, "invoicing.view")
    return qs.filter(entity=entity) if entity is not None else qs


def vendors(user, entity=None):
    qs = access.scope(Vendor.objects.all(), user, "invoicing.view")
    return qs.filter(entity=entity) if entity is not None else qs


def sales_documents(user, entity=None):
    """Sales documents ``user`` may see (R3, AT-4)."""
    qs = access.scope(SalesDocument.objects.all(), user, "invoicing.view")
    return qs.filter(entity=entity) if entity is not None else qs


def purchase_documents(user, entity=None):
    qs = access.scope(PurchaseDocument.objects.all(), user, "invoicing.view")
    return qs.filter(entity=entity) if entity is not None else qs


# --- read-only lists for the web UI (web.md R3, R24, R37, R38; slice 1) ------------------------------------------

def _not_personal():
    """Customers whose name is not personal data: neither an individual nor an employee's own record (R51)."""
    return ~Q(customer__customer_kind=Customer.Kind.INDIVIDUAL) & Q(customer__employee__isnull=True)


def _without_personal_data(qs):
    """Never load personal data a screen does not show (web.md R24 "never prefetched"): the buyer snapshot and the
    customer's personal fields are deferred; business and government names come as annotations."""
    return (
        qs.defer(*BUYER_FIELDS, *(f"customer__{name}" for name in PERSONAL_PARTY_FIELDS))
        .annotate(
            customer_public_name_en=Case(When(_not_personal(), then=F("customer__name_en")), default=Value("")),
            customer_public_name_ar=Case(When(_not_personal(), then=F("customer__name_ar")), default=Value("")),
        )
    )


def list_sales_documents(entity, *, user, doc_type=None, status=None, zatca_status=None, date_from=None, date_to=None,
                         number=None):
    """Sales documents of ``entity`` for ``user`` (``invoicing.view`` there), newest first.

    Refused (AccessDenied, logged) without ``invoicing.view`` in ``entity``; rows also pass through
    :func:`sales_documents` (access.scope). Filters are enumerated values, dates and document numbers (web.md R23).
    Customers are selected in the same query (R37), without their personal fields or the buyer snapshot (R24);
    show the customer with :func:`listed_customer_name`, which masks individuals (R24, R51, R86).
    """
    access.require(user, entity, "invoicing.view")
    qs = sales_documents(user, entity)
    if doc_type:
        qs = qs.filter(doc_type=doc_type)
    if status:
        qs = qs.filter(status=status)
    if zatca_status:
        qs = qs.filter(zatca_status=zatca_status)
    if date_from:
        qs = qs.filter(issue_date__gte=date_from)
    if date_to:
        qs = qs.filter(issue_date__lte=date_to)
    if number:
        qs = qs.filter(number__icontains=number)
    return _without_personal_data(qs.select_related("entity", "customer", "currency")).order_by("-issue_date", "-id")


def listed_customer_name(document, language):
    """The customer's name on a row of :func:`list_sales_documents`, in ``language`` (``en``/``ar``).

    An individual's name is personal data and is never loaded or shown here (web.md R24, invoicing R51, R86): the
    active half of the mask instead. Business and government names are shown, the other language filling an
    empty one, else the customer code.
    """
    en, ar = document.customer_public_name_en, document.customer_public_name_ar
    if not en and not ar:
        individual = (document.customer.customer_kind == Customer.Kind.INDIVIDUAL
                      or document.customer.employee_id is not None)
        return label_part(MASK, language) if individual else document.customer.code
    first, other = (ar, en) if language == "ar" else (en, ar)
    return first or other


# Header totals, SAR amounts and the buyer snapshot are set when a document is issued (approve_sales_document);
# before that only the lines are priced, so screens say "calculated at issue" instead of showing zero totals.
PRICED_STATUSES = frozenset({SalesDocument.Status.ISSUED, SalesDocument.Status.FINAL, SalesDocument.Status.REJECTED})


def sales_document_detail(entity, pk, *, user):
    """One sales document of ``entity`` for the read-only screen (``invoicing.view``; web.md R3, R4, R24, R40).

    Returns ``{"document", "lines", "buyer", "masked", "totals_ready"}``. A document of another entity, or one the
    user may not see, is "not found" (Http404), never 403. An individual buyer's snapshot is **masked** for
    everybody here and is not even read from the database, and no personal-data row is written: unmasking is an
    explicit, logged action (R24, R51), not part of a GET page. A business buyer's snapshot is read in one query.
    """
    access.require(user, entity, "invoicing.view")
    qs = _without_personal_data(
        sales_documents(user, entity).select_related("entity", "customer", "currency", "original_document",
                                                     "journal_entry")
    )
    doc = access.get_scoped_or_404(qs, user, "invoicing.view", pk=pk)
    lines = list(doc.lines.select_related("income_type", "vat_rate").order_by("line_no"))
    masked = doc.customer.customer_kind == Customer.Kind.INDIVIDUAL or doc.customer.employee_id is not None
    if masked:
        buyer = {name: MASK for name in BUYER_FIELDS}
    else:
        buyer = SalesDocument.objects.filter(pk=doc.pk).values(*BUYER_FIELDS).get()
    return {"document": doc, "lines": lines, "buyer": buyer, "masked": masked,
            "totals_ready": doc.status in PRICED_STATUSES}


def sales_document_counts(entity, *, user):
    """``{status: count}`` of ``entity``'s sales documents, for the dashboard card (``invoicing.view``; R38)."""
    access.require(user, entity, "invoicing.view")
    rows = sales_documents(user, entity).values("status").annotate(n=Count("id")).order_by()
    counts = {s: 0 for s in SalesDocument.Status.values}
    counts.update({r["status"]: r["n"] for r in rows})
    return counts


def _same_entity(entity, obj, what_en, what_ar):
    if obj is None or obj.entity_id != entity.pk:
        raise error(f"The {what_en} belongs to another entity.", f"{what_ar} يخص كيانًا آخر.", code="entity_mismatch")


# --- sales documents: drafts (3.3) ------------------------------------------------------------------

SALES_HEADER_FIELDS = {
    "zatca_profile", "flag_third_party", "flag_nominal", "flag_export", "flag_summary", "flag_self_billed",
    "issue_date", "supply_date", "supply_end_date", "due_date", "payment_means_code", "reason_code", "reason_ar",
    "reason_en", "adjustment_event_date", "notes_en", "notes_ar", "document_discount",
    "payment_received_date", "payment_order_date",  # R100 tax point (G6); R117 B2G payment order date (G1)
}


def _apply_sales_header(doc, fields):
    unknown = set(fields) - SALES_HEADER_FIELDS
    if unknown:
        raise error(f"Unknown fields: {', '.join(sorted(unknown))}.", "حقول غير معروفة.", code="unknown_field")
    for name, value in fields.items():
        if value is None and name in ("reason_ar", "reason_en", "notes_en", "notes_ar", "payment_means_code"):
            value = ""
        if name == "document_discount":
            value = _money(value, "document discount", currency=doc.currency)
        if name == "reason_code" and value is not None and not isinstance(value, NoteReason):
            value = _reference(NoteReason, value, "note reason", "سبب الإشعار")
        setattr(doc, name, value)
    if "reason_code" in fields and doc.reason_code is not None:  # the pick list prefills the editable reason
        if "reason_ar" not in fields:
            doc.reason_ar = doc.reason_code.name_ar
        if "reason_en" not in fields:
            doc.reason_en = doc.reason_code.name_en
    _refresh_tax_point(doc)


# --- tax point (R100 as amended by G6 / G7) and B2G payment order date (R117, G1) -------------------------

TaxPoint = SalesDocument.TaxPointBasis


def _computed_tax_point(doc):
    """G6 (PSG §11.3; TIR §4.2.3): (date, basis) of a sales document's tax point.

    An invoice: the earliest of its supply date (for a supply over a period, i.e. with ``supply_end_date``, the end
    of the supply period: continuous supplies), its issue date and the date a payment or advance for it was
    received. On equal dates the issue date is named. A credit or debit note: its own date (D30(a), X9).
    A B2G invoice (R117 as amended by owner decision I3, VAT IR Art. 20(5)) with its payment order date: that date,
    also when it is after the issue date (Art. 20(5) defers the tax point to the payment order).
    """
    if doc.doc_type != SalesDocument.DocType.INVOICE:
        return doc.issue_date, TaxPoint.ISSUE
    if _is_b2g_tax_point(doc):
        return doc.payment_order_date, TaxPoint.PAYMENT_ORDER
    candidates = [(doc.issue_date, TaxPoint.ISSUE)]
    if doc.supply_end_date:
        candidates.append((doc.supply_end_date, TaxPoint.SUPPLY_END))
    elif doc.supply_date:
        candidates.append((doc.supply_date, TaxPoint.SUPPLY))
    if doc.payment_received_date:
        candidates.append((doc.payment_received_date, TaxPoint.PAYMENT))
    return min(candidates, key=lambda c: c[0])  # min keeps the first of equal dates: the issue date


def _is_b2g_tax_point(doc):
    """R117 as amended (I3): a standard **invoice** to a government customer with its payment order date is taxed on
    that date. Credit and debit notes of B2G supplies keep their own date (D30(a), X9)."""
    return doc.doc_type == SalesDocument.DocType.INVOICE and bool(doc.payment_order_date) and _is_b2g(doc)


def _refresh_tax_point(doc):
    """Keep a draft's tax point computed (R100); a tax point entered by hand stays until it is cleared. A B2G
    invoice's payment order date (I3) replaces a tax point entered by hand."""
    if doc.tax_point_basis != TaxPoint.MANUAL or _is_b2g_tax_point(doc):
        doc.tax_point_date, doc.tax_point_basis = _computed_tax_point(doc)
        doc.tax_point_reason = ""


@transaction.atomic
def set_tax_point(document, *, user, tax_point_date, reason=""):
    """Enter a draft invoice's tax point by hand, with a reason (R100, G6), or clear it (``tax_point_date=None``)
    to go back to the computed one. Drafts only (R13); a credit or debit note always uses its own date (D30(a)).
    The tax point is never after the issue date (it is the earliest of supply, issue and payment)."""
    doc = _draft_sales(document, user)
    if tax_point_date is None:
        doc.tax_point_basis = ""
    else:
        if doc.doc_type != SalesDocument.DocType.INVOICE:
            raise error("A credit or debit note is taxed on its own date; its tax point is not entered by hand "
                        "(D30(a)).", "يُحتسب الإشعار الدائن أو المدين بتاريخه؛ ولا يُدخل تاريخ استحقاقه الضريبي يدويًا.",
                        code="tax_point_note")
        if _is_b2g(doc):
            raise error("A standard invoice to a government customer is taxed on its payment order date (VAT IR "
                        "Art. 20(5), R117); enter the payment order date instead of a tax point.",
                        "تُستحق ضريبة الفاتورة القياسية لعميل حكومي في تاريخ أمر الدفع (المادة 20(5) من اللائحة "
                        "التنفيذية)؛ أدخل تاريخ أمر الدفع بدلًا من تاريخ الاستحقاق الضريبي.",
                        code="tax_point_payment_order")
        reason = (reason or "").strip()
        if not reason:
            raise error("A tax point entered by hand needs a reason.",
                        "يحتاج تاريخ الاستحقاق الضريبي المُدخل يدويًا إلى سبب.", code="tax_point_reason")
        if not isinstance(tax_point_date, date_type):
            raise error("The tax point must be a date.", "يجب أن يكون تاريخ الاستحقاق الضريبي تاريخًا.",
                        code="tax_point_date")
        if tax_point_date > doc.issue_date:
            raise error(_TAX_POINT_AFTER_ISSUE[0].format(tp=tax_point_date, issue=doc.issue_date),
                        _TAX_POINT_AFTER_ISSUE[1].format(tp=tax_point_date, issue=doc.issue_date),
                        code="tax_point_after_issue")
        doc.tax_point_date, doc.tax_point_basis, doc.tax_point_reason = tax_point_date, TaxPoint.MANUAL, reason
    _refresh_tax_point(doc)
    _touch(doc, user)
    doc.save()
    _log_agent(user, doc.entity, "invoicing.create_sales_document", doc, "set_tax_point")
    return doc


_TAX_POINT_AFTER_ISSUE = (
    "The tax point {tp} is after the issue date {issue}; it is the earliest of supply, issue and payment (R100).",
    "تاريخ الاستحقاق الضريبي {tp} بعد تاريخ الإصدار {issue}؛ وهو الأسبق من التوريد والإصدار والدفع.",
)


def _tax_point_problems(doc):
    """R100: a tax point entered by hand is never after the issue date (checked again at issue, when the issue
    date becomes the approval day)."""
    if doc.tax_point_basis == TaxPoint.MANUAL and doc.tax_point_date and doc.tax_point_date > doc.issue_date:
        en, ar = _TAX_POINT_AFTER_ISSUE
        return [f"{en.format(tp=doc.tax_point_date, issue=doc.issue_date)} / "
                f"{ar.format(tp=doc.tax_point_date, issue=doc.issue_date)}"]
    return []


def _is_b2g(doc):
    """R117 (G1; zatca R-54): a standard invoice or note to a ``government`` customer (VAT IR Art. 20(5))."""
    return (doc.zatca_profile == SalesDocument.Profile.STANDARD
            and doc.customer.customer_kind == Customer.Kind.GOVERNMENT)


PAYMENT_ORDER_REQUIRED = (
    "A standard invoice or note to a government customer needs the payment order date before it is issued: it is "
    "ZATCA's TaxPointDate (BR-KSA-92; VAT IR Art. 20(5)). Enter it on the draft.",
    "تحتاج الفاتورة أو الإشعار القياسي لعميل حكومي إلى تاريخ أمر الدفع قبل الإصدار: وهو تاريخ الاستحقاق الضريبي "
    "لدى الهيئة (BR-KSA-92؛ المادة 20(5) من اللائحة التنفيذية). أدخله في المسودة.",
)


def _payment_order_problems(doc):
    """R117 (G1): no B2G standard document is issued without its payment order date; the ZATCA queue also holds
    one that has none (zatca R-54, BR-KSA-92)."""
    if _is_b2g(doc) and not doc.payment_order_date:
        return [" / ".join(PAYMENT_ORDER_REQUIRED)]
    return []


def create_sales_document(entity, *, user, customer, issue_date, doc_type=SalesDocument.DocType.INVOICE,
                          currency=None, original_document=None, **fields):
    """Create a draft invoice, credit note or debit note (``invoicing.create_sales_document``; agents may draft)."""
    access.require(user, entity, "invoicing.create_sales_document")
    _same_entity(entity, customer, "customer", "العميل")
    if doc_type not in SalesDocument.DocType.values:
        raise error(f"Unknown document type {doc_type!r}.", f"نوع المستند {doc_type!r} غير معروف.", code="doc_type")
    if not customer.is_active:
        raise error("The customer is inactive.", "العميل غير نشط.", code="customer_inactive")
    if doc_type == SalesDocument.DocType.INVOICE and original_document is not None:
        raise error("An invoice has no original document.", "الفاتورة ليس لها مستند أصلي.", code="original_not_allowed")
    if original_document is not None:
        if not isinstance(original_document, SalesDocument):  # OQ-43 default: one original invoice per note
            raise error("A credit or debit note references exactly one original invoice (OQ-43).",
                        "يشير الإشعار إلى فاتورة أصلية واحدة فقط.", code="one_original")
        _same_entity(entity, original_document, "original invoice", "الفاتورة الأصلية")
        if _is_deferred_sale(original_document):
            raise _deferred_sale_note_refused()
    elif doc_type != SalesDocument.DocType.INVOICE:
        raise error("A credit or debit note references its original invoice.", "الإشعار يشير إلى فاتورته الأصلية.",
                    code="original_required")
    with transaction.atomic():
        doc = _new_sales_document(entity, user=user, customer=customer, issue_date=issue_date, doc_type=doc_type,
                                  currency=currency, original_document=original_document, fields=fields)
        _log_agent(user, entity, "invoicing.create_sales_document", doc, "create_draft")
    return doc


def _new_sales_document(entity, *, user, customer, issue_date, doc_type, currency, original_document, fields):
    """A saved draft (rights checked by the caller). Billing documents are built by their own hooks."""
    cur = _currency(currency or (original_document.currency_id if original_document else customer.currency_id), entity)
    doc = SalesDocument(
        entity=entity, doc_type=doc_type, customer=customer, issue_date=issue_date, currency=cur,
        original_document=original_document, created_by=user, source=_source(user),
    )
    if _natural_person_not_subject_to_vat(customer):  # OQ-19: simplified for individuals by default (R48)
        doc.zatca_profile = SalesDocument.Profile.SIMPLIFIED
    if original_document is not None:  # zatca R-19: the category follows the original invoice
        doc.zatca_profile = original_document.zatca_profile
    _apply_sales_header(doc, fields)
    doc.save()
    return doc


def _draft_sales(document, user, permission="invoicing.create_sales_document"):
    doc = SalesDocument.objects.select_for_update(of=("self",)).select_related("entity").get(pk=document.pk)
    access.require(user, doc.entity, permission)
    if doc.status != Status.DRAFT:
        raise error("Only drafts can be edited (R13).", "يمكن تعديل المسودات فقط.", code="not_draft")
    return doc


def _bump(doc):
    """A new version of the document: approvals and postings checked against an earlier one are refused
    (:func:`_refuse_stale_grant`). Called by every change of a draft, by submission and by return to draft."""
    doc.version = (doc.version or 0) + 1


def _touch(doc, user):
    """The user changed the draft: they cannot approve it, and earlier approvals no longer count (access R14, R42)."""
    doc.modified_by, doc.modified_at = user, timezone.now()
    _bump(doc)


# R73 / billing R56 (OQ-26 default): on a document created from billing a reviewer completes these header fields
# only; lines, amounts, dates and the supply period come from billing.
BILLING_HEADER_FIELDS = {"payment_means_code", "reason_code", "reason_ar", "reason_en", "notes_en", "notes_ar",
                         "payment_order_date"}  # R117: a government customer's payment order date


def _refuse_billing_lines(doc):
    if doc.from_billing:
        raise error(
            "The lines of a document created from billing are read-only for every user; correct the contract in "
            "billing instead (R73, billing R56).",
            "بنود المستند المنشأ من الفوترة للقراءة فقط لكل المستخدمين؛ صحّح العقد في وحدة الفوترة بدلًا من ذلك.",
            code="billing_lines_read_only")


@transaction.atomic
def update_sales_document(document, *, user, **fields):
    doc = _draft_sales(document, user)
    if doc.from_billing and set(fields) - BILLING_HEADER_FIELDS:
        raise error(
            "On a document created from billing only the payment means, reason and notes are completed here "
            f"({', '.join(sorted(set(fields) - BILLING_HEADER_FIELDS))} not allowed; billing R56).",
            "في المستند المنشأ من الفوترة تُستكمل هنا وسيلة الدفع والسبب والملاحظات فقط.", code="billing_header_read_only")
    _apply_sales_header(doc, fields)
    _touch(doc, user)
    doc.save()
    return doc


def _sales_line_values(doc, line, *, income_type, quantity, unit_price, description_ar, description_en, vat_category,
                       vat_exemption_reason, income_account, line_discount, unit_code, exemption_text_ar="",
                       exemption_text_en=""):
    entity = doc.entity
    if not isinstance(income_type, IncomeType):
        income_type = _reference(IncomeType, income_type, "income type", "نوع الإيراد")
    deferred = income_type.code == DEFERRED_SALE_INCOME
    if deferred and not _deferred_sale_writes.get():
        raise _deferred_sale_by_hand()
    line.income_type = income_type
    line.quantity = _decimal(quantity, "quantity")
    if not fx.has_places(line.quantity, 3) or line.quantity <= ZERO:
        raise error("Quantity must be above zero with at most 3 decimals.", "يجب أن تكون الكمية أكبر من صفر بثلاث خانات كحد أقصى.",
                    code="quantity")
    line.unit_price = _money(unit_price, "unit price", currency=doc.currency)  # D19 / R93: the currency's decimals
    line.line_discount = _money(line_discount, "line discount", currency=doc.currency)
    line.unit_code = unit_code or "C62"
    line.description_ar = (description_ar or "").strip()
    line.description_en = (description_en or "").strip()
    if not line.description_ar:  # OQ-18 (D19, confirmed I15): Arabic is required on every sales line
        raise error("The Arabic line description is required.", "وصف البند بالعربية مطلوب.", code="description_ar")
    line.vat_category = vat_category or income_type.default_vat_category
    if line.vat_category not in VatCategory.values:
        raise error(f"Unknown VAT category {line.vat_category!r}.", "فئة ضريبة غير معروفة.", code="vat_category")
    if line.vat_category == VatCategory.STANDARD and not (entity.vat_number or "").strip():
        raise error(  # R11, R94: an entity without a VAT number charges no VAT
            f"{entity.code} has no VAT number, so it charges no VAT: a standard-rated (S) line is not allowed; choose "
            "category Z, E or O (R11, R94).",
            f"ليس للكيان {entity.code} رقم ضريبي، فلا يفرض ضريبة القيمة المضافة: لا يُسمح ببند خاضع للنسبة الأساسية؛ "
            "اختر الفئة Z أو E أو O.", code="no_vat_number")
    reason = vat_exemption_reason or income_type.default_exemption_reason
    if reason is not None and not isinstance(reason, VatExemptionReason):
        reason = _reference(VatExemptionReason, reason, "VAT exemption reason", "سبب الإعفاء")
    line.vat_exemption_reason = reason
    line.exemption_text_ar = (exemption_text_ar or "").strip()
    line.exemption_text_en = (exemption_text_en or "").strip()
    if income_account is None:
        intercompany = doc.customer.group_entity_id is not None
        code = (income_type.default_intercompany_account_code if intercompany else "") or income_type.default_account_code
        if not code:
            raise error(f"Income type {income_type.code} has no default account; choose the income account.",
                        f"نوع الإيراد {income_type.code} ليس له حساب افتراضي؛ اختر حساب الإيراد.", code="account_required")
        income_account = code
    line.income_account = income_account if isinstance(income_account, Account) else _account(entity, income_account)
    problems = _sales_line_account_problems(entity, line)
    problems += _line_vat_problems(line, doc.supply_date or doc.issue_date)
    if problems:
        raise _problems_error(problems, "line_invalid")
    _price_line(line, doc.supply_date or doc.issue_date, _doc_places(doc))


def _line_vat_problems(line, on):
    """R10: a rate effective on the supply date; Z/E/O lines need an exemption reason of their category."""
    problems = []
    if _rate_on(line.vat_category, on) is None:
        problems.append(f"No VAT rate for category {line.vat_category} on {on}. / لا توجد نسبة ضريبة للفئة "
                        f"{line.vat_category} بتاريخ {on}.")
    if line.vat_category == VatCategory.STANDARD:
        if line.vat_exemption_reason_id is not None:
            problems.append("A standard-rated line has no exemption reason. / البند الخاضع للنسبة الأساسية ليس له سبب إعفاء.")
    elif line.vat_exemption_reason is None:
        problems.append(f"A line in category {line.vat_category} needs an exemption reason (VATEX). / "
                        f"البند في الفئة {line.vat_category} يحتاج إلى سبب إعفاء.")
    elif not line.vat_exemption_reason.is_active:  # nit 13: withdrawn codes are never used again
        problems.append(f"Exemption reason {line.vat_exemption_reason.code} is no longer in use. / "
                        f"سبب الإعفاء {line.vat_exemption_reason.code} لم يعد مستخدمًا.")
    elif line.vat_exemption_reason.category != line.vat_category:
        problems.append(f"Exemption reason {line.vat_exemption_reason.code} is not for category {line.vat_category}. / "
                        f"سبب الإعفاء {line.vat_exemption_reason.code} لا يخص الفئة {line.vat_category}.")
    out_of_scope = line.vat_exemption_reason is not None and line.vat_exemption_reason.code == OUT_OF_SCOPE_CODE
    texts = (getattr(line, "exemption_text_ar", ""), getattr(line, "exemption_text_en", ""))
    if out_of_scope and not texts[0].strip():  # row 1c: the taxpayer states the reason, visible, so in Arabic
        problems.append(f"{OUT_OF_SCOPE_CODE} needs the reason the supply is out of scope, in Arabic. / "
                        f"يحتاج {OUT_OF_SCOPE_CODE} إلى سبب كون التوريد خارج النطاق بالعربية.")
    if not out_of_scope and any(t.strip() for t in texts):
        problems.append(f"An exemption reason text is given only with {OUT_OF_SCOPE_CODE}. / "
                        f"يُدخل نص سبب الإعفاء مع {OUT_OF_SCOPE_CODE} فقط.")
    return problems


def _price_line(line, on, places=2, rate=None):
    """R9 / R93: net = round(qty x price - discount); line VAT = round(net x rate) for display only; both half-up
    to the document currency's decimals (``places``). ``rate``: a fixed ``VatRate`` (a note at its original's rate,
    R102) instead of the one in force on ``on``."""
    with localcontext() as ctx:
        ctx.prec = 60
        net = _round(line.quantity * line.unit_price - line.line_discount, places)
    if net < ZERO:
        raise error("The line discount is larger than the line amount.", "خصم البند أكبر من مبلغه.", code="line_negative")
    line.net_amount = net
    line.vat_rate = rate if rate is not None else _rate_on(line.vat_category, on)
    line.vat_amount = _round(net * line.vat_rate.rate / HUNDRED, places) if line.vat_rate else ZERO


@transaction.atomic
def add_sales_line(document, *, user, income_type, quantity, unit_price, description_ar, description_en="",
                   vat_category=None, vat_exemption_reason=None, income_account=None, line_discount="0",
                   unit_code="C62", exemption_text_ar="", exemption_text_en=""):
    """Add a line to a draft (R5 Sharia, R9 VAT, R10 rates, OQ-18 Arabic). ``exemption_text_ar`` / ``_en``: the
    taxpayer's own reason, required (Arabic) with VATEX-SA-OOS and only with it (row 1c)."""
    doc = _draft_sales(document, user)
    _refuse_billing_lines(doc)
    last = doc.lines.order_by("-line_no").values_list("line_no", flat=True).first() or 0
    line = SalesDocumentLine(entity=doc.entity, document=doc, line_no=last + 1)
    _sales_line_values(doc, line, income_type=income_type, quantity=quantity, unit_price=unit_price,
                       description_ar=description_ar, description_en=description_en, vat_category=vat_category,
                       vat_exemption_reason=vat_exemption_reason, income_account=income_account,
                       line_discount=line_discount, unit_code=unit_code, exemption_text_ar=exemption_text_ar,
                       exemption_text_en=exemption_text_en)
    line.save()
    _touch(doc, user)
    doc.save(update_fields=["modified_by", "modified_at", "version", "updated_at"])
    return line


@transaction.atomic
def delete_sales_line(line, *, user):
    doc = _draft_sales(line.document, user)
    _refuse_billing_lines(doc)
    found = SalesDocumentLine.objects.filter(pk=line.pk, document=doc).first()
    if found is None:
        raise error("The line is not on this document.", "البند ليس في هذا المستند.", code="line_missing")
    found.delete()
    _touch(doc, user)
    doc.save(update_fields=["modified_by", "modified_at", "version", "updated_at"])


@transaction.atomic
def delete_sales_draft(document, *, user):
    """Drafts may be deleted (R13); a document with a number never is (R14, R17)."""
    doc = _draft_sales(document, user)
    if doc.from_billing:  # billing R46c, R48: billing discards its own drafts (discard_adjustment_draft)
        raise error("A draft created from billing is discarded in billing, not deleted here.",
                    "تُستبعد المسودة المنشأة من الفوترة في وحدة الفوترة، ولا تُحذف هنا.", code="billing_document")
    if doc.number:
        raise error("A numbered document is never deleted.", "لا يُحذف مستند له رقم.", code="numbered")
    doc.lines.all().delete()
    doc.delete()


@transaction.atomic
def submit_sales_document(document, *, user):
    """draft -> submitted (R57). An agent never submits (R22, access R12); the submitter cannot approve (R14)."""
    doc = SalesDocument.objects.select_for_update(of=("self",)).select_related("entity").get(pk=document.pk)
    access.require(user, doc.entity, "invoicing.submit_sales_document")
    if doc.status != Status.DRAFT:
        raise error("Only drafts can be submitted.", "يمكن تقديم المسودات فقط.", code="not_draft")
    problems = _issue_problems(doc, check_period=False) + _payment_order_problems(doc)
    if problems:
        raise _problems_error(problems, "document_invalid")
    doc.status = Status.SUBMITTED
    doc.submitted_by, doc.submitted_at = user, timezone.now()
    _bump(doc)
    doc._service_write = True
    doc.save()
    return doc


@transaction.atomic
def return_to_draft(document, *, user):
    """submitted -> draft for edits (R57): by staff who submit, or by the approver."""
    doc = SalesDocument.objects.select_for_update(of=("self",)).select_related("entity").get(pk=document.pk)
    if not access.has(user, doc.entity, "invoicing.submit_sales_document"):
        access.require(user, doc.entity, "invoicing.approve_sales_document", document=doc)
    if doc.status != Status.SUBMITTED:
        raise error("Only submitted documents return to draft.", "تعود المستندات المقدمة فقط إلى مسودة.",
                    code="not_submitted")
    doc.status = Status.DRAFT
    # Earlier approvals no longer count (access R42), but returning it does not make the user a modifier:
    # the approver who sends it back for edits may still approve it later.
    doc.modified_at = timezone.now()
    _bump(doc)
    doc._service_write = True
    doc.save()
    return doc


# --- sales documents: validation, totals and issue ---------------------------------------------------

def _sales_breakdown(doc, lines, sar_rate=None):
    """R9: VAT per (category, rate) at document level, half-up to the document currency's decimals (R93); line VAT
    is display only. R56: SAR per category, 2 dp."""
    places = _doc_places(doc)
    groups = {}
    for line in lines:
        key = (line.vat_category, line.vat_rate.rate if line.vat_rate else ZERO)
        g = groups.setdefault(key, {"taxable": ZERO, "reasons": set(), "texts": set()})
        g["taxable"] += line.net_amount
        if line.vat_exemption_reason_id:
            g["reasons"].add(line.vat_exemption_reason.code)
            if line.vat_exemption_reason.code == OUT_OF_SCOPE_CODE:  # row 1c: the taxpayer's own BT-120 text
                g["texts"].add((line.exemption_text_en, line.exemption_text_ar))
    breakdown, total_vat, total_vat_sar = [], ZERO, ZERO
    for (category, rate), g in sorted(groups.items()):
        vat = _round(g["taxable"] * rate / HUNDRED, places)
        vat_sar = _round(vat * (sar_rate if sar_rate is not None else Decimal("1")), 2)  # SAR: always 2 dp
        breakdown.append({"category": category, "rate": str(rate), "taxable": _fmt(g["taxable"], places),
                          "vat": _fmt(vat, places), "vat_sar": _fmt(vat_sar, 2), "exemption_reasons": sorted(g["reasons"]),
                          **({"exemption_texts": [{"en": en, "ar": ar} for en, ar in sorted(g["texts"])]}
                             if g["texts"] else {})})
        total_vat += vat
        total_vat_sar += vat_sar
    total_net = sum((line.net_amount for line in lines), ZERO)
    q = _quantum(places)
    return breakdown, total_net.quantize(q), total_vat.quantize(q), total_vat_sar


def _buyer_snapshot(doc):
    """Buyer data from the customer, or from the group entity's §3.0 fields when intercompany (R15, R16, R68)."""
    c = doc.customer
    snap = {}
    if c.group_entity_id:
        g = c.group_entity
        snap.update(
            buyer_name_en=g.legal_name_en or g.name_en, buyer_name_ar=g.legal_name_ar or g.name_ar,
            buyer_vat_number=g.vat_number, buyer_building_no=g.building_no, buyer_street_en=g.street_en,
            buyer_street_ar=g.street_ar, buyer_district_en=g.district_en, buyer_district_ar=g.district_ar,
            buyer_additional_no=g.additional_no, buyer_postal_code=g.postal_code, buyer_city_en=g.city_en,
            buyer_city_ar=g.city_ar, buyer_country=g.country,
        )
        ids = [("CRN", g.cr_number), ("700", g.unified_number)]
    else:
        snap.update(
            buyer_name_en=c.name_en, buyer_name_ar=c.name_ar, buyer_vat_number=c.vat_number if c.is_vat_registered else "",
            buyer_building_no=c.building_no, buyer_street_en=c.street_en, buyer_street_ar=c.street_ar,
            buyer_district_en=c.district_en, buyer_district_ar=c.district_ar, buyer_additional_no=c.additional_no,
            buyer_postal_code=c.postal_code, buyer_city_en=c.city_en, buyer_city_ar=c.city_ar, buyer_country=c.country,
        )
        ids = [(i.scheme, i.value) for i in c.identifiers.all()]
    ids = sorted((i for i in ids if i[1]), key=lambda i: BUYER_ID_PRIORITY.index(i[0]))
    if doc.flag_export:  # BR-KSA-46: no buyer VAT number on an export invoice; cleared first (row 4b)
        snap["buyer_vat_number"] = ""
    if _uses_citizen_codes(doc):  # BR-KSA-49 (row 1d): the buyer's national ID, on every profile
        ids = [i for i in ids if i[0] == "NAT"]
    elif doc.zatca_profile == SalesDocument.Profile.SIMPLIFIED or snap["buyer_vat_number"]:
        ids = []  # R50: simplified documents store no buyer ID; a VAT number replaces it (BR-KSA-14)
    scheme, value = ids[0] if ids else ("", "")
    snap.update(buyer_other_id_scheme=scheme, buyer_other_id=value)
    return snap


def _uses_citizen_codes(doc):
    return doc.lines.filter(vat_exemption_reason__code__in=CITIZEN_CODES).exists()


def _citizen_problems(doc, snap):
    """BR-KSA-49 / BR-KSA-25 (row 1d): VATEX-SA-EDU or -HEA lines need the buyer's national ID (NAT) and name."""
    if not _uses_citizen_codes(doc):
        return []
    problems = []
    if snap["buyer_other_id_scheme"] != "NAT" or not snap["buyer_other_id"]:
        problems.append("Private education or healthcare to a citizen (VATEX-SA-EDU/HEA) needs the buyer's national ID "
                        "(NAT). / التعليم أو الصحة الخاصة للمواطن تحتاج إلى رقم الهوية الوطنية للمشتري.")
    if not snap["buyer_name_ar"].strip():
        problems.append("Private education or healthcare to a citizen needs the buyer's name in Arabic. / "
                        "التعليم أو الصحة الخاصة للمواطن تحتاج إلى اسم المشتري بالعربية.")
    return problems


def _buyer_problems(doc, snap):
    """R16 (standard documents): Arabic name, address (BR-KSA-10/63/67, OQ-45) and VAT number or other ID."""
    c = doc.customer
    problems = []
    if not snap["buyer_name_ar"].strip():
        problems.append("Buyer name in Arabic is missing. / اسم المشتري بالعربية ناقص.")
    if not (snap["buyer_street_en"] or snap["buyer_street_ar"]).strip():
        problems.append("Buyer street is missing. / شارع المشتري ناقص.")
    if not (snap["buyer_city_en"] or snap["buyer_city_ar"]).strip():
        problems.append("Buyer city is missing. / مدينة المشتري ناقصة.")
    if not snap["buyer_country"]:
        problems.append("Buyer country is missing. / دولة المشتري ناقصة.")
    if snap["buyer_country"] == "SA":
        if not (len(snap["buyer_building_no"]) == 4 and snap["buyer_building_no"].isdigit()):
            problems.append("Buyer building number (4 digits) is missing. / رقم مبنى المشتري (4 أرقام) ناقص.")
        if not (len(snap["buyer_postal_code"]) == 5 and snap["buyer_postal_code"].isdigit()):
            problems.append("Buyer postal code (5 digits) is missing. / الرمز البريدي للمشتري (5 أرقام) ناقص.")
        if not (snap["buyer_district_en"] or snap["buyer_district_ar"]).strip():
            problems.append("Buyer district is missing. / حي المشتري ناقص.")
    registered = bool(c.group_entity.vat_number) if c.group_entity_id else c.is_vat_registered
    if registered and not doc.flag_export:
        number = c.group_entity.vat_number if c.group_entity_id else c.vat_number
        if not number:
            problems.append("Buyer VAT number is missing (the buyer is VAT registered). / الرقم الضريبي للمشتري ناقص.")
    if not snap["buyer_vat_number"] and not snap["buyer_other_id"]:  # BR-KSA-81 (row 4b), exports included
        problems.append("Buyer ID (TIN, CRN, ... ) is missing for a buyer without a VAT number on this document. / "
                        "هوية المشتري ناقصة لمشترٍ ليس له رقم ضريبي في هذا المستند.")
    return problems


def _flag_problems(doc):
    """BR-KSA-07 / BR-KSA-31: no self-billed export; simplified allows only third party, nominal and summary."""
    problems = []
    if doc.flag_self_billed and doc.flag_export:
        problems.append("A self-billed invoice cannot be an export invoice. / لا تكون فاتورة الفوترة الذاتية فاتورة تصدير.")
    if doc.zatca_profile == SalesDocument.Profile.SIMPLIFIED and (doc.flag_export or doc.flag_self_billed):
        problems.append("A simplified invoice cannot be export or self-billed. / لا تكون الفاتورة المبسطة تصديرًا أو فوترة ذاتية.")
    return problems


def _credited_so_far(original, exclude_pk=None):
    """Gross of credit notes issued against ``original`` that are not rejected (R18 cap)."""
    qs = SalesDocument.objects.filter(original_document=original, doc_type=SalesDocument.DocType.CREDIT_NOTE,
                                      status__in=[Status.ISSUED, Status.FINAL])
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.aggregate(t=Sum("total_gross"))["t"] or ZERO


def _note_problems(doc, gross):
    """R18 / R19 / AT-11 / AT-63: original final, same entity and customer, Arabic reason, payment means, cap."""
    problems = []
    original = doc.original_document
    if original is None:
        return ["A credit or debit note references its original invoice. / الإشعار يشير إلى فاتورته الأصلية."]
    if original.doc_type != SalesDocument.DocType.INVOICE:
        problems.append("The original document must be an invoice. / يجب أن يكون المستند الأصلي فاتورة.")
    if _is_deferred_sale(original) and not (doc.doc_type == SalesDocument.DocType.CREDIT_NOTE
                                            and doc.source == DEFERRED_SALE_SOURCE and _deferred_sale_writes.get()):
        problems.append(" / ".join(DEFERRED_SALE_NOTE))  # R128: only the payroll-approved adjustment credit note
    if original.status != Status.FINAL:
        problems.append("The original invoice is not final (not yet cleared or reported). / "
                        "الفاتورة الأصلية ليست نهائية (لم تُعتمد أو يُبلَّغ عنها بعد).")
    if original.entity_id != doc.entity_id or original.customer_id != doc.customer_id:
        problems.append("The original invoice belongs to another entity or customer. / الفاتورة الأصلية تخص كيانًا أو عميلًا آخر.")
    if original.currency_id != doc.currency_id:
        problems.append("A note is in the currency of its original invoice. / يكون الإشعار بعملة فاتورته الأصلية.")
    if original.zatca_profile != doc.zatca_profile:  # zatca R-19
        problems.append("A note has the profile (standard or simplified) of its original invoice. / "
                        "يتبع الإشعار نوع فاتورته الأصلية (قياسية أو مبسطة).")
    if not doc.reason_ar.strip():
        problems.append("The reason in Arabic is required on a credit or debit note. / سبب الإشعار بالعربية مطلوب.")
    if doc.payment_means_code not in PaymentMeans.values:
        problems.append("Payment means code required on a credit or debit note. / رمز وسيلة الدفع مطلوب في الإشعار.")
    if doc.doc_type == SalesDocument.DocType.CREDIT_NOTE and original.status == Status.FINAL:
        creditable = original.total_gross - _credited_so_far(original, exclude_pk=doc.pk)
        if gross > creditable:
            problems.append(f"The credit note ({gross}) exceeds the amount still creditable on {original.number} "
                            f"({creditable}). / الإشعار الدائن ({gross}) يتجاوز المبلغ القابل للإشعار ({creditable}).")
        accounts = set(original.lines.values_list("income_account_id", flat=True))
        if any(line.income_account_id not in accounts for line in doc.lines.all()):  # E4: the original's accounts
            problems.append("A credit note uses the income accounts of its original invoice. / "
                            "يستخدم الإشعار الدائن حسابات إيراد فاتورته الأصلية.")
    return problems


def _issue_problems(doc, *, check_period=True, user=None):
    """Everything that must hold before a sales document is issued (R5, R9 to R11, R16, R18, R19, R48, R56, R69)."""
    problems = []
    entity = doc.entity
    lines = list(doc.lines.select_related("vat_rate", "vat_exemption_reason", "income_account", "income_type"))
    on = doc.supply_date or doc.issue_date
    if not lines:
        problems.append("A document needs at least one line. / يحتاج المستند إلى بند واحد على الأقل.")
    if doc.document_discount:
        problems.append("Document-level discounts are not supported yet; discount the lines instead. / "
                        "خصم المستند غير مدعوم بعد؛ اخصم البنود بدلًا منه.")
    if doc.customer.entity_id != entity.pk:
        problems.append("The customer belongs to another entity. / العميل يخص كيانًا آخر.")
    if doc.customer.group_entity_id == entity.pk:
        problems.append("A customer cannot be the issuing entity itself. / لا يكون العميل هو الكيان المُصدِر.")
    if (doc.notes_en or "").strip() and not (doc.notes_ar or "").strip():
        problems.append("Notes need an Arabic text when an English text is given. / الملاحظات تحتاج إلى نص عربي.")
    try:
        _currency(doc.currency_id, entity)
    except InvoicingError as exc:
        problems += exc.messages
    for line in lines:
        problems += [_on_line(line.line_no, p) for p in _sales_line_account_problems(entity, line)]
        if line.income_type.code == DEFERRED_SALE_INCOME and not _deferred_sale_writes.get():
            problems.append(_on_line(line.line_no, " / ".join(DEFERRED_SALE_BY_HAND)))
        problems += [_on_line(line.line_no, p) for p in _line_vat_problems(line, on)]
        if not line.description_ar.strip():
            problems.append(_on_line(line.line_no, "the Arabic description is required. / وصف البند بالعربية مطلوب."))
    standard_rated = any(line.vat_category == VatCategory.STANDARD for line in lines)
    missing = entity_ready_to_issue(entity, standard_rated=standard_rated)
    if missing:  # R69, AT-48; R11 / AT-16 via standard_rated (vat_number required)
        problems.append(f"Seller fields missing or invalid: {', '.join(missing)}. / بيانات البائع ناقصة أو غير صالحة: "
                        f"{', '.join(missing)}.")
    if standard_rated and entity.vat_effective_date and on < entity.vat_effective_date:  # OQ-32 default
        problems.append(f"Standard-rated supplies before the VAT registration date {entity.vat_effective_date}. / "
                        f"توريدات خاضعة قبل تاريخ التسجيل الضريبي {entity.vat_effective_date}.")
    if doc.zatca_profile == SalesDocument.Profile.STANDARD and not doc.supply_date:
        problems.append("Supply date is required on a standard document. / تاريخ التوريد مطلوب في المستند القياسي.")
    problems += _flag_problems(doc)
    for line in lines:
        if line.vat_rate_id is None or not _rate_on(line.vat_category, on) or _rate_on(line.vat_category, on).pk != line.vat_rate_id:
            _price_line(line, on, _doc_places(doc))  # supply date changed after the line was added: re-priced
    breakdown, net, vat, _ = _sales_breakdown(doc, lines)
    gross = net + vat
    problems += _third_decimal_problems(doc, lines, breakdown, net, vat)
    snap = _buyer_snapshot(doc)
    if doc.zatca_profile == SalesDocument.Profile.STANDARD:
        problems += _buyer_problems(doc, snap)
    problems += _citizen_problems(doc, snap)
    if doc.doc_type in (SalesDocument.DocType.CREDIT_NOTE, SalesDocument.DocType.DEBIT_NOTE):
        problems += _note_problems(doc, gross)
    problems += _tax_point_problems(doc)
    if check_period:
        period = _open_period(entity, doc.issue_date)
        if period:
            problems.append(period)
    return problems


def _third_decimal_problems(doc, lines, breakdown, net, vat):
    """OQ-62 default (invoicing R93, zatca R-51): until ZATCA confirms that its XML accepts 3-decimal amounts, a
    sales document in a 3-decimal currency is handed to ZATCA only when every amount ends in 0 in the third decimal
    (then it is emitted with 2 decimals). Issuing hands it to ZATCA, so such a document is refused here, before a
    number is used; it can still be drafted and corrected. Unit prices are not amounts in this sense (R9 note).
    """
    if _doc_places(doc) <= 2:
        return []
    amounts = [net, vat, net + vat, doc.document_discount]
    for line in lines:
        amounts += [line.net_amount, line.vat_amount, line.line_discount]
    for group in breakdown:
        amounts += [Decimal(group["taxable"]), Decimal(group["vat"])]
    odd = sorted({a for a in amounts if a != a.quantize(CENT)})
    if not odd:
        return []
    shown = ", ".join(str(a) for a in odd[:5])
    return [f"{doc.currency_id} amounts with a third decimal other than 0 ({shown}) cannot be sent to ZATCA until "
            "ZATCA confirms that 3-decimal amounts are accepted (OQ-62); change the lines so that every amount ends "
            f"in 0. / لا يمكن إرسال مبالغ {doc.currency_id} التي خانتها العشرية الثالثة ليست صفرًا ({shown}) إلى الهيئة "
            "حتى تؤكد الهيئة قبول المبالغ بثلاث خانات عشرية؛ عدّل البنود بحيث ينتهي كل مبلغ بصفر."]


def _natural_person_not_subject_to_vat(customer):
    """R48 / row 8a: an ``individual`` registered for VAT (a sole trader) is a business for the SAR 1,000 rule."""
    return customer.customer_kind == Customer.Kind.INDIVIDUAL and not customer.is_vat_registered


def _simplified_problems(doc, gross_sar):
    """R48 (OQ-19, OQ-44 default): simplified only below SAR 1,000 gross, or for a natural person not subject to VAT.

    Notes are not tested (row 8b): a credit or debit note "is subject to the same issuing requirements as the type
    of invoice on which it is based" (XML v1.2 §5.2) and keeps its original's profile (zatca R-19), so a note of
    SAR 1,000 or more on a simplified invoice stays issuable.
    """
    if doc.doc_type != SalesDocument.DocType.INVOICE:
        return []
    if (doc.zatca_profile == SalesDocument.Profile.SIMPLIFIED and not _natural_person_not_subject_to_vat(doc.customer)
            and gross_sar >= SIMPLIFIED_LIMIT_SAR):
        return [f"A simplified invoice is allowed only below SAR 1,000 for a non-individual buyer (gross SAR {gross_sar}); "
                f"issue a standard invoice. / الفاتورة المبسطة مسموحة لمشترٍ غير فرد فقط إذا قلّ المبلغ عن 1,000 ريال "
                f"(الإجمالي {gross_sar} ريال)؛ أصدر فاتورة قياسية."]
    return []


def _retention_until(entity, doc):
    """zatca R-38: end of the tax period of the issue date + 6 years (standard) or + 15 years (real estate).

    Tax periods are taken as calendar months or calendar quarters (``Entity.vat_return_period``).
    """
    d = doc.issue_date
    if entity.vat_return_period == Entity.VatReturnPeriod.QUARTERLY:
        month = ((d.month - 1) // 3 + 1) * 3
    else:
        month = d.month
    end = date_type(d.year, month, calendar.monthrange(d.year, month)[1])
    years = 15 if doc.retention_class == SalesDocument.Retention.REAL_ESTATE else 6
    year = end.year + years
    return date_type(year, end.month, min(end.day, calendar.monthrange(year, end.month)[1]))


def _late_invoice_warnings(doc):
    """Row 6f (TIR §4.5): a standard tax invoice is issued within 15 days from the end of the month of supply.

    Warning only. The end of supply (``supply_end_date``, else ``supply_date``) decides the month.
    """
    supplied = doc.supply_end_date or doc.supply_date
    if (doc.doc_type != SalesDocument.DocType.INVOICE or doc.zatca_profile != SalesDocument.Profile.STANDARD
            or supplied is None):
        return []
    month_end = date_type(supplied.year, supplied.month, calendar.monthrange(supplied.year, supplied.month)[1])
    deadline = month_end + timedelta(days=STANDARD_INVOICE_DAYS)
    if doc.issue_date > deadline:
        return [f"This tax invoice is issued after {deadline}, 15 days after the end of the month of supply "
                f"(TIR §4.5). / تصدر هذه الفاتورة الضريبية بعد {deadline}، أي بعد 15 يومًا من نهاية شهر التوريد."]
    return []


def _refuse_stale_grant(grant, doc, seen_version):
    """Review item 3 (access R14, R42): an approval checked against another version of the document does not count.

    The grant is taken before the row lock, for the document as read then (``seen_version``); a return to draft,
    edit or resubmission in between bumps the locked row's version and makes the grant stale. A version counter,
    not timestamps: the editing and approving servers' clocks may differ (verification review item 2).
    """
    if doc.version != seen_version:
        access.deny(grant.user, doc.entity, grant.permission, "grant_document_changed")


def _issue_date_warnings(doc):
    """6b: the draft asked for another date; the document is issued on the approval day instead (warning only)."""
    if doc.requested_issue_date and doc.requested_issue_date != doc.issue_date:
        return [f"Issued on {doc.issue_date} (the day of approval); the draft asked for {doc.requested_issue_date}. / "
                f"صدر بتاريخ {doc.issue_date} (يوم الاعتماد)؛ وكانت المسودة تطلب {doc.requested_issue_date}."]
    return []


def approve_sales_document(document, *, user):
    """CFO approval issues a submitted document (R57): number, snapshots and totals in one transaction, then the
    ZATCA hand-off after commit (R40). The approver is never the creator, submitter or last modifier: approvals
    that post are always segregated (access R35, OQ-19), whatever ``segregation_required`` says.

    Returns the document; ``document.warnings`` lists the issue-date (R78) and late-invoice (R84) warnings
    (bilingual), which do not block.
    """
    doc = SalesDocument.objects.select_related("entity", "customer").get(pk=document.pk)
    grant = access.require(user, doc.entity, "invoicing.approve_sales_document", document=doc)
    seen_version = doc.version  # the version the grant was checked against (segregation, R14)
    with transaction.atomic():
        doc = SalesDocument.objects.select_for_update(of=("self",)).select_related("entity", "customer").get(pk=doc.pk)
        if doc.status != Status.SUBMITTED:
            raise error("Only submitted documents are approved.", "تُعتمد المستندات المقدمة فقط.", code="not_submitted")
        _refuse_stale_grant(grant, doc, seen_version)
        _issue_submitted(doc, rate_user=user, approved_by=user, approved_at=timezone.now())
        access.use_grant(grant, target=doc)  # a used, current approval: the clearance posting checks it (access R42)
        port = zatca_port.get_port()
        transaction.on_commit(lambda: port.submit_document(SalesDocument.objects.get(pk=doc.pk), user))
    # R95 (D30a): a credit or debit note is dated and taxed on its own issue date; R71's 15-day warning from
    # adjustment_event_date is withdrawn (that date is information only).
    doc.warnings = _issue_date_warnings(doc) + _late_invoice_warnings(doc)
    return doc


NOT_VAT_REGISTERED = (
    "{code} has no VAT number: its documents carry no VAT and are not tax invoices. Which document title and type it "
    "issues, and how it posts, await tax sign-off (OQ-59), so it cannot be issued yet; it stays submitted.",
    "ليس للكيان {code} رقم ضريبي: مستنداته بلا ضريبة وليست فواتير ضريبية. ولا يزال عنوان المستند ونوعه وطريقة ترحيله "
    "بانتظار اعتماد المستشار الضريبي، فلا يمكن إصداره بعد؛ ويبقى مقدَّمًا.",
)


def _not_vat_registered(entity):
    en, ar = NOT_VAT_REGISTERED
    return _problems_error([f"{en.format(code=entity.code)} / {ar.format(code=entity.code)}"],
                           "entity_not_vat_registered")


def _issue_submitted(doc, *, rate_user, approved_by, approved_at, line_rates=None):
    """Issue a locked, submitted document (R57, R14, R15, R78): number, snapshots, totals and the recorded
    approval, in the caller's transaction. ``approved_by`` is the person whose approval is recorded: the CFO who
    approves it, or for an invoice auto-issued from billing the approver of the billed contract version (R60,
    billing R53). ``rate_user`` reads the exchange rates. The caller records the grant use and the ZATCA hand-off.
    """
    if not (doc.entity.vat_number or "").strip():
        # R94 / OQ-59 default: the title, type and posting path of an unregistered entity's documents (they are
        # not tax invoices and never go to ZATCA) await tax sign-off; until then they are drafted only.
        raise _not_vat_registered(doc.entity)
    if doc.original_document_id:  # R18 cap: one note at a time per original (review item 4)
        doc.original_document = SalesDocument.objects.select_for_update(of=("self",)).get(
            pk=doc.original_document_id)
    # 6b / BR-KSA-04: the issue date is the day of issue (approval) in Riyadh, never the draft's date and
    # never a future one; the draft's date is kept as the date requested. Time and date come from one clock.
    now = _riyadh_now()
    doc.requested_issue_date = doc.issue_date
    doc.issue_date = now.date()
    doc.issue_time = now.time().replace(microsecond=0)
    _refresh_tax_point(doc)  # R100 (G6): computed from the issue date of approval, unless entered by hand
    problems = _issue_problems(doc, user=rate_user) + _payment_order_problems(doc)
    if problems:
        raise _problems_error(problems, "document_invalid")
    entity = doc.entity
    lines = list(doc.lines.select_related("vat_rate", "vat_exemption_reason", "income_account", "income_type"))
    on = doc.supply_date or doc.issue_date
    sar_rate, sama_row = _vat_sar_rate(entity, doc.currency_id, doc.tax_point_date, rate_user)
    fx_rate, rate_date, rate_source, rate_row = _booking_rate(entity, doc, sama_row, rate_user)
    with billing_line_writes() if doc.from_billing else nullcontext():  # the approval re-prices billing lines too
        for line in lines:
            _price_line(line, on, _doc_places(doc), rate=(line_rates or {}).get(line.pk))
            line._service_write = True
            line.save()
    breakdown, net, vat, vat_sar = _sales_breakdown(doc, lines, sar_rate)
    gross = net + vat
    gross_sar = _round(gross * sar_rate, 2)
    problems = _simplified_problems(doc, gross_sar)
    if problems:
        raise _problems_error(problems, "simplified_threshold")
    snap = _buyer_snapshot(doc)
    for name, value in snap.items():
        setattr(doc, name, value or "")
    for name in ("vat_number", "cr_number", "unified_number", "building_no", "street_en", "street_ar",
                 "district_en", "district_ar", "additional_no", "postal_code", "city_en", "city_ar", "country"):
        setattr(doc, f"seller_{name}", getattr(entity, name) or "")
    doc.seller_legal_name_en, doc.seller_legal_name_ar = entity.legal_name_en, entity.legal_name_ar
    doc.total_net, doc.total_vat, doc.total_gross = net, vat, gross
    doc.total_vat_sar, doc.total_gross_sar, doc.vat_breakdown = vat_sar, gross_sar, breakdown
    doc.fx_rate, doc.fx_rate_date, doc.fx_rate_source, doc.sar_rate = fx_rate, rate_date, rate_source, sar_rate
    doc.fx_exchange_rate, doc.vat_sar_exchange_rate = rate_row, sama_row
    doc.requires_purification = any(
        line.income_account.requires_purification
        or (line.income_account.sharia_prohibited and not entity.sharia_enforced) for line in lines
    )
    doc.is_intercompany = doc.customer.group_entity_id is not None
    if any(line.income_type.is_real_estate_rent for line in lines):
        doc.retention_class = SalesDocument.Retention.REAL_ESTATE
    doc.retention_until = _retention_until(entity, doc)
    doc.due_date = doc.due_date or doc.issue_date  # OQ-3 default: prefilled with the document date
    doc.number = _next_number(entity, doc.doc_type, doc.issue_date.year)
    doc.status = Status.ISSUED
    doc.approved_by, doc.approved_at = approved_by, approved_at
    doc.approved_version = doc.version  # R87: the clearance posting relies on this very version
    doc._service_write = True
    doc.save()
    return doc


# --- ZATCA result and posting on clearance (R41 to R44) ------------------------------------------

def resubmit_to_zatca(document, *, user):
    """Hand an issued document to ZATCA again when the after-commit hand-off failed (R40; review item 6).

    Only documents ``issued`` with ``zatca_status = not_submitted`` (the hand-off never recorded a submission).
    Interim default until the owner decides (spec OQ-57): the ZATCA queue system user (automatic retry, with
    ``zatca.process_queue``) or a person holding the ``zatca_admin`` role in the entity (``zatca.retry_failed``
    through that role); agents never. Every resubmission is logged in the AccessLog. The port is called after
    commit (``transaction.on_commit``): inside an outer transaction, only when that one commits.
    """
    from core.access.models import Membership

    doc = SalesDocument.objects.select_related("entity").get(pk=document.pk)
    entity = doc.entity
    if access_services.kind_of(user) == Kind.SYSTEM:
        permission = "zatca.process_queue"
        access.require(user, entity, permission)
    else:  # agents are refused here: zatca.retry_failed is not open to agents (catalogue)
        permission = "zatca.retry_failed"
        grant = access.require(user, entity, permission)
        if not Membership.objects.filter(pk__in=grant.membership_ids, role__code="zatca_admin").exists():
            access.deny(user, entity, permission, "no_membership")
    with transaction.atomic():
        doc = SalesDocument.objects.select_for_update(of=("self",)).get(pk=doc.pk)
        if doc.status != Status.ISSUED or doc.zatca_status != SalesDocument.ZatcaStatus.NOT_SUBMITTED:
            raise error("Only issued documents that were never handed to ZATCA are resubmitted.",
                        "يُعاد إرسال المستندات الصادرة التي لم تُسلَّم للهيئة فقط.", code="not_resubmittable")
        access_services.log(
            AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=entity, permission=permission,
            details={"document": [doc._meta.label_lower, doc.pk], "action": "zatca_resubmit"},
        )
        # R40: the port sees the document only once the logged resubmission is committed, also when the caller
        # runs this inside its own transaction (which could still roll the log back).
        port = zatca_port.get_port()
        transaction.on_commit(lambda: port.submit_document(SalesDocument.objects.get(pk=doc.pk), user))
    return SalesDocument.objects.get(pk=doc.pk)


def on_zatca_result(document, result, *, user):
    """Callback from zatca with the result for an issued document (R41, R42).

    ``user`` is the ZATCA queue system user (``zatca.process_queue`` to record the
    result, ``invoicing.post_on_clearance`` to post). Cleared / reported (with or
    without warnings): the document posts, dated its issue date, with ``posted_by``
    = the recorded approver, and becomes ``final``. Rejected: ``rejected``, the
    number stays used and nothing is booked (R20). A posting failure leaves the
    result recorded; :func:`post_cleared_document` retries it.
    """
    doc = SalesDocument.objects.select_related("entity").get(pk=document.pk)
    access.require(user, doc.entity, "zatca.process_queue")
    if result.status not in zatca_port.RESULT_STATUSES:
        raise error(f"Unknown ZATCA status {result.status!r}.", f"حالة الهيئة {result.status!r} غير معروفة.")
    with transaction.atomic():
        doc = SalesDocument.objects.select_for_update(of=("self",)).get(pk=doc.pk)
        if doc.status != Status.ISSUED:
            raise error("Only issued documents await a ZATCA result.", "تنتظر المستندات الصادرة فقط نتيجة الهيئة.",
                        code="not_issued")
        doc.zatca_status = result.status
        doc.zatca_icv = result.icv if result.icv is not None else doc.zatca_icv
        doc.zatca_hash = result.invoice_hash or doc.zatca_hash
        doc.zatca_previous_hash = result.previous_hash or doc.zatca_previous_hash
        doc.zatca_qr = result.qr or doc.zatca_qr
        doc.zatca_messages = list(result.messages)
        if result.status == "rejected":
            doc.status = Status.REJECTED  # R20 / R42: the number stays used; no entry was ever booked
        doc._service_write = True
        doc.save()
    if result.status in zatca_port.POSTING_STATUSES:
        return post_cleared_document(doc, user=user)
    return doc


def post_cleared_document(document, *, user):
    """Post an issued document that ZATCA cleared or reported (R41); ``user`` is the ZATCA queue system user."""
    doc = SalesDocument.objects.select_related("entity").get(pk=document.pk)
    grant = access.require(user, doc.entity, "invoicing.post_on_clearance", document=doc)
    with transaction.atomic():
        doc = SalesDocument.objects.select_for_update(of=("self",)).select_related("entity", "approved_by").get(pk=doc.pk)
        if doc.status != Status.ISSUED or doc.zatca_status not in zatca_port.POSTING_STATUSES:
            raise error("Only issued documents cleared or reported by ZATCA are posted.",
                        "تُرحَّل المستندات الصادرة التي اعتمدتها الهيئة أو أُبلغت بها فقط.", code="not_cleared")
        if doc.approved_version is None or doc.version != doc.approved_version:
            # R87: the approval this posting relies on was given for another version of the document. A version
            # counter, not modified_at against the grant time: those come from two servers' clocks.
            access.deny(user, doc.entity, grant.permission, "grant_document_changed")
        targets = _lock_note_target(doc)
        with _posting_as(doc.approved_by):
            entry = ledger.create_entry(
                doc.entity, doc.issue_date, f"{doc.title_en} {doc.number} / {doc.title_ar} {doc.number}", None,
                user=user, reference=doc.number, post=True, grant=grant, posted_by=doc.approved_by, document=doc,
            )
        doc.journal_entry = entry
        doc.status = Status.FINAL
        doc._service_write = True
        doc.save()
        if targets and not _is_deferred_sale(doc):  # R37: a credit note is allocated to its original up to the open
            # amount; a bai' ajel adjustment is not: 1240 is collected by payroll, never allocated here (R121, R128)
            amount = _note_settlement_amount(doc)
            if amount > ZERO:
                _allocate_receivable(doc.entity, None, doc, targets[0], amount, doc.issue_date, doc.approved_by,
                                     via_settlement=doc.currency_id != doc.entity.base_currency_id)
    return doc


def _lock_note_target(doc):
    if doc.doc_type == SalesDocument.DocType.CREDIT_NOTE and doc.original_document_id:
        return list(SalesDocument.objects.select_for_update(of=("self",)).filter(pk=doc.original_document_id))
    return []


def _note_settlement_amount(doc):
    return min(doc.total_gross, _open_receivable(doc.original_document))


# --- document lines for the ledger (registered hooks, ledger.documents) --------------------------

@contextmanager
def _posting_as(user):
    token = _posting_user.set(user)
    try:
        yield
    finally:
        _posting_user.reset(token)


def _require_posting_context(document, status):
    """A build_lines hook runs only inside an invoicing posting, for a document in the status that posting
    gives it and not yet booked (nit 14)."""
    if _posting_user.get() is None:
        raise error(f"{document._meta.label} posts only through invoicing.services.",
                    f"يُرحَّل المستند {document._meta.label} من خلال خدمات الفوترة فقط.", code="post_through_services")
    if document.status != status or document.journal_entry_id is not None:
        raise error(f"{document._meta.label} {document.pk} is {document.status}; only a {status} document that is not "
                    "yet booked builds its entry.",
                    f"المستند {document.pk} حالته {document.status}؛ لا يبني قيده إلا مستند بحالة {status} لم يُرحَّل بعد.",
                    code="post_wrong_status")


def _control(party, receivable):
    if party.group_entity_id:
        return (AR_GROUP if receivable else AP_GROUP), party.group_entity
    return (AR if receivable else AP), None


def _line(account, side, amount, currency, functional, counterparty=None, description=""):
    spec = {"account": account, "description": description[:300]}
    if counterparty is not None:
        spec["counterparty"] = counterparty
    if currency == functional:
        spec[side] = amount
    else:
        spec["currency"] = currency
        spec[f"txn_{side}"] = amount
    return spec


def _control_line(document):
    """The control-account line that booked ``document`` (the item later documents settle, ledger-fx R18)."""
    if document.journal_entry_id is None:
        return None
    if isinstance(document, SalesDocument):
        account, _ = _sales_control(document)
    else:
        account, _ = _control(document.vendor, False)
    debit = isinstance(document, SalesDocument) and document.doc_type != SalesDocument.DocType.CREDIT_NOTE
    lines = JournalLine.objects.filter(entry_id=document.journal_entry_id, account__code=account, line_kind="normal")
    lines = lines.filter(debit__gt=0) if debit else lines.filter(credit__gt=0)
    return lines.order_by("pk").first()


def _pinned(document, specs):
    """Foreign-currency lines of ``document`` use the rate row pinned at approval (review item 2, nit 15)."""
    row = document.fx_exchange_rate
    if row is None:
        return list(specs)
    pin = {"exchange_rate": row}
    if getattr(document, "fx_rate_source", "") == SAMA_TAX_POINT:  # H2: the ledger checks it on the tax point (R51)
        pin["rate_source"] = "sama_tax_point"
    return [{**spec, **pin} if spec.get("currency") and not spec.get("settles_line") else spec for spec in specs]


def _with_settlements(document, currency, plain, settlements):
    """Lines for an entry that settles earlier items (ledger-fx R18) in a foreign currency.

    ``plain``: line dicts in the document currency, pinned to the document's approved rate.
    ``settlements``: (item JournalLine, transaction amount, description). Each
    item is cleared at its carrying rate (``ledger.services._settlement_for_document``);
    plain lines get their functional amounts at the document's pinned rate; the
    difference goes to the realised FX gain (4700) or loss (5750) line. Nothing
    here depends on anyone's current rights or on the rate table today (nit 15).
    In the functional currency no settlement lines are needed: plain lines only.
    """
    entity = document.entity
    functional = entity.base_currency_id
    if not settlements:
        return plain
    if currency == functional:
        out = list(plain)
        for item, amount, description in settlements:
            side = "credit" if item.debit > 0 else "debit"
            out.append(_line(item.account, side, amount, functional, functional, item.counterparty_entity, description))
        return out
    rate = document.fx_rate
    out, debit, credit = [], ZERO, ZERO
    for spec in plain:
        side = "debit" if "txn_debit" in spec else "credit"
        amount = fx.convert(spec[f"txn_{side}"], rate)
        out.append({**spec, side: amount})
        if side == "debit":
            debit += amount
        else:
            credit += amount
    for item, amount, description in settlements:
        settle, settled = ledger._settlement_for_document(item, amount, description)
        out.append(settle)
        if item.debit > 0:
            credit += settled
        else:
            debit += settled
    diff = debit - credit
    if diff > ZERO:
        out.append({"account": ledger.FX_GAIN_REALISED, "credit": diff, "line_kind": "realised_fx",
                    "description": "Realised FX gain / أرباح فروقات العملة المحققة"})
    elif diff < ZERO:
        out.append({"account": ledger.FX_LOSS_REALISED, "debit": -diff, "line_kind": "realised_fx",
                    "description": "Realised FX loss / خسائر فروقات العملة المحققة"})
    return out


def _sales_document_lines(document):
    """Registered ``build_lines`` for SalesDocument: E1 to E4 from the stored document (R4, R9, R27, R37)."""
    _require_posting_context(document, Status.ISSUED)
    if document.zatca_status not in zatca_port.POSTING_STATUSES:  # R41: only once ZATCA cleared or reported it
        raise error("A sales document is booked only once ZATCA cleared or reported it.",
                    "لا يُرحَّل مستند المبيعات إلا بعد اعتماده من الهيئة أو الإبلاغ عنه.", code="not_cleared")
    cur, functional = document.currency_id, document.entity.base_currency_id
    control, counterparty = _sales_control(document)
    credit_note = document.doc_type == SalesDocument.DocType.CREDIT_NOTE
    income_side, control_side = ("debit", "credit") if credit_note else ("credit", "debit")
    label = f"{document.number}"
    plain, per_account = [], {}
    for line in document.lines.select_related("income_account").order_by("line_no"):  # one line per account (E1)
        per_account.setdefault(line.income_account.pk, [line.income_account, ZERO])[1] += line.net_amount
    for account, amount in per_account.values():
        if amount:
            plain.append(_line(account, income_side, amount, cur, functional, description=label))
    for group in document.vat_breakdown:
        vat = Decimal(group["vat"])
        if vat:  # E3: zero-rated, exempt and out-of-scope categories have no 2200 line
            plain.append(_line(VAT_OUTPUT, income_side, vat, cur, functional,
                               description=f"{label} VAT {group['category']} {group['rate']}%"))
    settlements = []
    gross = document.total_gross
    if credit_note and document.original_document_id and cur != functional:
        amount = _note_settlement_amount(document)
        item = _control_line(document.original_document)
        if amount > ZERO and item is not None:
            settlements.append((item, amount, f"{label} → {document.original_document.number}"))
            gross -= amount
    if gross:
        plain.append(_line(control, control_side, gross, cur, functional, counterparty, description=label))
    return _with_settlements(document, cur, _pinned(document, plain), settlements)


def _check_document_rate(document, lines):
    """Registered ``validate_lines``: every foreign-currency line books at the rate pinned on the document at
    approval (its rate row and rate), never at a rate looked up later (review item 2)."""
    functional = document.entity.base_currency_id
    problems = []
    for line in lines:
        if line.currency_id == functional or line.line_kind != "normal":
            continue
        sama_tp = getattr(document, "fx_rate_source", "") == SAMA_TAX_POINT
        if sama_tp != (line.rate_source == "sama_tax_point"):  # H2: the SAMA tax-point source only as approved
            problems.append(
                f"Line {line.account.code} has rate source {line.rate_source}; the document was approved at "
                f"{'its SAMA tax-point rate' if sama_tp else 'a table rate'}. / مصدر سعر البند {line.account.code} "
                f"هو {line.rate_source}؛ اعتُمد المستند {'بسعر البنك المركزي في تاريخ استحقاق الضريبة' if sama_tp else 'بسعر الجدول'}."
            )
        if line.fx_rate != document.fx_rate or line.exchange_rate_id != document.fx_exchange_rate_id:
            problems.append(
                f"Line {line.account.code} is rated {line.fx_rate} (rate row {line.exchange_rate_id}); the document "
                f"was approved at {document.fx_rate} (rate row {document.fx_exchange_rate_id}) and books only at that "
                f"rate. / سعر البند {line.account.code} هو {line.fx_rate} (سجل السعر {line.exchange_rate_id})؛ "
                f"اعتُمد المستند بسعر {document.fx_rate} (سجل السعر {document.fx_exchange_rate_id}) ولا يُرحَّل إلا به."
            )
    return problems


def _pending_sales_documents(entity, start_date, end_date):
    """Registered ``pending_documents``: issued documents awaiting ZATCA in the range (R43, AT-32)."""
    return [d.number for d in SalesDocument.objects.filter(entity=entity, status=Status.ISSUED,
                                                           issue_date__gte=start_date, issue_date__lte=end_date)]


# --- open amounts and allocation (R34 to R38) -------------------------------------------------------

def _sum(qs):
    return qs.aggregate(t=Sum("amount"))["t"] or ZERO


def _in_minor_units(amount, document):
    """``amount`` with exactly the document currency's decimals (D27, R93): the columns hold 3 decimals, so a SAR
    sum would otherwise come back as ``1150.000``."""
    return amount.quantize(_quantum(_doc_places(document)))


def _open_receivable(document):
    """Open amount of a final invoice or debit note: gross minus allocations to it."""
    return _in_minor_units(document.total_gross - _sum(ReceivableAllocation.objects.filter(target=document)), document)


def _open_payable(document):
    return _in_minor_units(document.total_gross - _sum(PayableAllocation.objects.filter(target=document)), document)


def _available(source):
    if isinstance(source, CustomerReceipt):
        amount = source.amount - _sum(ReceivableAllocation.objects.filter(receipt=source))
    elif isinstance(source, SalesDocument):
        amount = source.total_gross - _sum(ReceivableAllocation.objects.filter(credit_note=source))
    elif isinstance(source, VendorPayment):  # AP settled: cash plus tax withheld (R62)
        amount = source.settled_amount - _sum(PayableAllocation.objects.filter(payment=source))
    else:
        amount = source.total_gross - _sum(PayableAllocation.objects.filter(vendor_credit_note=source))
    return _in_minor_units(amount, source)


def open_amount(document, *, user):
    """Open amount of an invoice, debit note or bill; available credit of a receipt, credit note or payment."""
    access.require(user, document.entity, "invoicing.view")
    if isinstance(document, SalesDocument) and document.doc_type != SalesDocument.DocType.CREDIT_NOTE:
        return _open_receivable(document)
    if isinstance(document, PurchaseDocument) and document.doc_type == PurchaseDocument.DocType.BILL:
        return _open_payable(document)
    return _available(document)


def payment_state(document, *, user):
    """unpaid / partial / paid / credited (3.3), computed from allocations."""
    access.require(user, document.entity, "invoicing.view")
    is_sales = isinstance(document, SalesDocument)
    allocations = (ReceivableAllocation if is_sales else PayableAllocation).objects.filter(target=document)
    allocated = _sum(allocations)
    if allocated == ZERO:
        return "unpaid"
    if allocated < document.total_gross:
        return "partial"
    by_note = _sum(allocations.filter(credit_note__isnull=False) if is_sales else
                   allocations.filter(vendor_credit_note__isnull=False))
    return "credited" if by_note == allocated else "paid"


def _allocate_receivable(entity, receipt, credit_note, target, amount, on, user, via_settlement=False):
    row = ReceivableAllocation(entity=entity, receipt=receipt, credit_note=credit_note, target=target, amount=amount,
                               date=on, created_by=user, via_settlement=via_settlement)
    row._service_write = True
    row.save()
    return row


def _allocate_payable(entity, payment, note, target, amount, on, user, via_settlement=False):
    row = PayableAllocation(entity=entity, payment=payment, vendor_credit_note=note, target=target, amount=amount,
                            date=on, created_by=user, via_settlement=via_settlement)
    row._service_write = True
    row.save()
    return row


def _allocation_problems(source, target, amount):
    """R34 / R44: same entity, party and currency; both booked (final / posted); within both open amounts."""
    problems = []
    sales = isinstance(target, SalesDocument)
    party = (lambda d: d.customer_id) if sales else (lambda d: d.vendor_id)
    if source.entity_id != target.entity_id:
        problems.append("Source and target belong to different entities. / المصدر والهدف يخصان كيانين مختلفين.")
    if party(source) != party(target):
        problems.append("Source and target belong to different parties. / المصدر والهدف يخصان طرفين مختلفين.")
    if source.currency_id != target.currency_id:
        problems.append("Source and target are in different currencies. / المصدر والهدف بعملتين مختلفتين.")
    elif source.currency_id != source.entity.base_currency_id:
        problems.append("Foreign-currency items are settled when the receipt, payment or note posts (ledger-fx R18). / "
                        "تُسوّى البنود بالعملة الأجنبية عند ترحيل السند أو الإشعار.")
    booked = {"final", "posted"}
    if source.status not in booked or target.status not in booked:
        problems.append("Both sides must be final or posted (R44). / يجب أن يكون الطرفان نهائيين أو مرحّلين.")
    if sales:
        if target.doc_type == SalesDocument.DocType.CREDIT_NOTE:
            problems.append("A credit note is not an allocation target. / الإشعار الدائن ليس هدفًا للتخصيص.")
        if _is_deferred_sale(target):
            problems.append(" / ".join(DEFERRED_SALE_COLLECTED_BY_PAYROLL))
        if isinstance(source, SalesDocument) and source.doc_type != SalesDocument.DocType.CREDIT_NOTE:
            problems.append("Only receipts and credit notes are allocated. / تُخصَّص المقبوضات والإشعارات الدائنة فقط.")
        if isinstance(source, CustomerReceipt) and source.kind != CustomerReceipt.Kind.RECEIPT:
            problems.append("A refund is not allocated to invoices. / لا يُخصَّص رد المبلغ للفواتير.")
        open_target = _open_receivable(target)
    else:
        if target.doc_type != PurchaseDocument.DocType.BILL:
            problems.append("Only bills are allocation targets. / الفواتير فقط أهداف للتخصيص.")
        if isinstance(source, VendorPayment) and source.kind != VendorPayment.Kind.PAYMENT:
            problems.append("A refund received is not allocated to bills. / لا يُخصَّص الاسترداد للفواتير.")
        open_target = _open_payable(target)
    if amount > open_target or amount > _available(source):
        problems.append(f"The amount {amount} is more than is open (target {open_target}, source {_available(source)}). / "
                        f"المبلغ {amount} أكبر من المفتوح.")
    return problems


def allocate(source, target, amount, *, user):
    """Allocate a receipt or credit note to an invoice / debit note, or a payment or vendor credit note to a bill.

    No GL effect (R35): both sides sit on the same control account. ``invoicing.allocate``.
    """
    access.require(user, target.entity, "invoicing.allocate")
    sales = isinstance(target, SalesDocument)
    with transaction.atomic():
        model_t = SalesDocument if sales else PurchaseDocument
        target = model_t.objects.select_for_update(of=("self",)).select_related("entity").get(pk=target.pk)
        source = type(source).objects.select_for_update(of=("self",)).select_related("entity").get(pk=source.pk)
        # Checked against the locked target's currency, not the caller's copy (reviewer round 6, item 8).
        amount = _money(amount, "allocation amount", positive=True, currency=target.currency_id)
        problems = _allocation_problems(source, target, amount)
        if problems:
            raise _problems_error(problems, "allocation_invalid")
        on = timezone.localdate()
        if sales:
            receipt = source if isinstance(source, CustomerReceipt) else None
            note = source if isinstance(source, SalesDocument) else None
            return _allocate_receivable(target.entity, receipt, note, target, amount, on, user)
        payment = source if isinstance(source, VendorPayment) else None
        note = source if isinstance(source, PurchaseDocument) else None
        return _allocate_payable(target.entity, payment, note, target, amount, on, user)


def unallocate(allocation, *, user):
    """R36: an un-allocation is a new negative row with ``reverses`` set; the original is never changed."""
    access.require(user, allocation.entity, "invoicing.allocate")
    model = type(allocation)
    with transaction.atomic():
        original = model.objects.select_for_update(of=("self",)).get(pk=allocation.pk)
        if original.amount <= ZERO or original.reverses_id is not None:
            raise error("Only an allocation is un-allocated, not an un-allocation.", "يُلغى التخصيص فقط.",
                        code="unallocate_invalid")
        if model.objects.filter(reverses=original).exists():
            raise error("This allocation is already un-allocated.", "أُلغي هذا التخصيص مسبقًا.", code="unallocate_invalid")
        if original.via_settlement:
            raise error("This allocation was settled in the ledger; reverse the document that made it instead.",
                        "سُوّي هذا التخصيص في دفتر الأستاذ؛ اعكس المستند الذي أنشأه بدلًا من ذلك.", code="unallocate_settled")
        row = model(
            entity=original.entity, target=original.target, amount=-original.amount, date=timezone.localdate(),
            created_by=user, reverses=original,
            **({"receipt": original.receipt, "credit_note": original.credit_note} if model is ReceivableAllocation
               else {"payment": original.payment, "vendor_credit_note": original.vendor_credit_note}),
        )
        row._service_write = True
        row.save()
    return row


# --- purchase documents (3.4) ----------------------------------------------------------------------

def create_purchase_document(entity, *, user, vendor, vendor_document_number, vendor_document_date, accounting_date,
                             doc_type=PurchaseDocument.DocType.BILL, currency=None, original_document=None,
                             vendor_vat_number=None, due_date=None, vendor_total_vat="0", vendor_total_gross="0",
                             attachment=None):
    """Create a draft bill or vendor credit note (``invoicing.create_bill``; agents may draft, R22)."""
    access.require(user, entity, "invoicing.create_bill")
    _same_entity(entity, vendor, "vendor", "المورد")
    if doc_type not in PurchaseDocument.DocType.values:
        raise error(f"Unknown document type {doc_type!r}.", f"نوع المستند {doc_type!r} غير معروف.", code="doc_type")
    if doc_type == PurchaseDocument.DocType.BILL and original_document is not None:
        raise error("A bill has no original document.", "فاتورة المورد ليس لها مستند أصلي.", code="original_not_allowed")
    if original_document is not None:
        _same_entity(entity, original_document, "original bill", "الفاتورة الأصلية")
    number = (vendor_document_number or "").strip()
    if not number:
        raise error("The vendor document number is required.", "رقم مستند المورد مطلوب.", code="vendor_number")
    cur = _currency(currency or (original_document.currency_id if original_document else vendor.currency_id), entity)
    duplicate = PurchaseDocument.objects.filter(entity=entity, vendor=vendor, vendor_document_number=number).exclude(
        status=PStatus.REVERSED)
    if duplicate.exists():  # R23 / AT-23 (also a DB partial unique)
        raise error(f"Vendor document {number} is already entered for this vendor.",
                    f"مستند المورد {number} مُدخل مسبقًا لهذا المورد.", code="duplicate_vendor_document")
    with transaction.atomic():
        doc = PurchaseDocument(
            entity=entity, doc_type=doc_type, vendor=vendor, vendor_document_number=number,
            vendor_document_date=vendor_document_date, accounting_date=accounting_date, currency=cur,
            original_document=original_document, due_date=due_date,
            vendor_vat_number=(vendor.vat_number if vendor_vat_number is None else vendor_vat_number) or "",
            vendor_total_vat=_money(vendor_total_vat, "vendor VAT", currency=cur),
            vendor_total_gross=_money(vendor_total_gross, "vendor total", currency=cur),
            reverse_charge=not vendor.is_resident, created_by=user, source=_source(user),
        )
        if attachment is not None:
            doc.attachment = attachment
        if doc.vendor_vat_number:
            validate_vat_number(doc.vendor_vat_number)
        doc.save()
        _log_agent(user, entity, "invoicing.create_bill", doc, "create_draft")
    return doc


def _draft_purchase(document, user):
    doc = PurchaseDocument.objects.select_for_update(of=("self",)).select_related("entity", "vendor").get(pk=document.pk)
    access.require(user, doc.entity, "invoicing.create_bill")
    if doc.status != PStatus.DRAFT:
        raise error("Only drafts can be edited (R24).", "يمكن تعديل المسودات فقط.", code="not_draft")
    return doc


def _vendor_master_registered(vendor):
    """The vendor record says the vendor is registered for VAT (flag or VAT number on the master)."""
    return bool(vendor.is_vat_registered or (vendor.vat_number or "").strip())


def _registered_vendor_number_missing(doc):
    """A resident vendor registered for VAT on its master, but the document's VAT number is blank (R92, R29).

    Refused at submit and approval: blanking the number on the document must not turn a registered vendor's bill
    into an R92 VAT-free bill (reviewer round 6)."""
    return (doc.vendor.is_resident and _vendor_master_registered(doc.vendor)
            and not (doc.vendor_vat_number or "").strip())


def _unregistered_vendor(doc):
    """D24 / R92: a resident vendor that is not registered for VAT (no flag and no VAT number on the vendor
    record, none on the document) charges no VAT. A vendor registered on its master never gets this treatment
    (:func:`_registered_vendor_number_missing`). Non-resident vendors have no KSA VAT number by nature and keep
    reverse charge (R30)."""
    return (doc.vendor.is_resident and not _vendor_master_registered(doc.vendor)
            and not (doc.vendor_vat_number or "").strip())


def _vat_free_category(doc, line):
    """R92 / OQ-60 default: lines of a bill from an unregistered resident vendor record category O, VAT 0.00."""
    if _unregistered_vendor(doc):
        line.vat_category = VatCategory.OUT_OF_SCOPE


PURCHASE_HEADER_FIELDS = {"vendor_document_date", "accounting_date", "due_date", "vendor_total_vat",
                          "vendor_total_gross", "vendor_vat_number", "vendor_vat_sar"}


@transaction.atomic
def update_purchase_document(document, *, user, **fields):
    doc = _draft_purchase(document, user)
    unknown = set(fields) - PURCHASE_HEADER_FIELDS
    if unknown:
        raise error(f"Unknown fields: {', '.join(sorted(unknown))}.", "حقول غير معروفة.", code="unknown_field")
    for name, value in fields.items():
        if name in ("vendor_total_vat", "vendor_total_gross"):
            value = _money(value, name, currency=doc.currency)
        elif name == "vendor_vat_sar" and value is not None:  # SAR, 2 decimals (tax-returns C12)
            value = _money(value, name, currency="SAR")
        setattr(doc, name, value)
    _touch(doc, user)
    doc.save()
    return doc


@transaction.atomic
def add_purchase_line(document, *, user, account, quantity, unit_price, vat_category=VatCategory.STANDARD,
                      vat_attribution=PurchaseDocumentLine.Attribution.TAXABLE, supply_kind="", description_ar="",
                      description_en="", line_discount="0", unit_code="C62", wht_category=None):
    """Add a line to a draft bill (R5 Sharia, R29 attribution, R30 supply kind for non-resident vendors).

    ``wht_category`` (code or row; R61): non-resident vendors only, default the vendor's category. A line without
    one can be booked, but the bill cannot be paid until it has one (R61)."""
    doc = _draft_purchase(document, user)
    entity = doc.entity
    last = doc.lines.order_by("-line_no").values_list("line_no", flat=True).first() or 0
    line = PurchaseDocumentLine(entity=entity, document=doc, line_no=last + 1)
    line.account = _account(entity, account)
    line.quantity = _decimal(quantity, "quantity")
    if not fx.has_places(line.quantity, 3) or line.quantity <= ZERO:
        raise error("Quantity must be above zero with at most 3 decimals.", "يجب أن تكون الكمية أكبر من صفر.",
                    code="quantity")
    line.unit_price = _money(unit_price, "unit price", currency=doc.currency)
    line.line_discount = _money(line_discount, "line discount", currency=doc.currency)
    line.unit_code = unit_code or "C62"
    line.description_ar, line.description_en = (description_ar or "").strip(), (description_en or "").strip()
    line.vat_category, line.vat_attribution, line.supply_kind = vat_category, vat_attribution, supply_kind or ""
    _vat_free_category(doc, line)
    problems = _account_problems(entity, line.account, {Account.Type.EXPENSE, Account.Type.ASSET}, "expense or asset")
    if wht_category is not None and not isinstance(wht_category, WhtCategory):
        wht_category = _wht_category(wht_category)
    if wht_category is not None and doc.vendor.is_resident:
        problems.append("A withholding tax category is set only on lines of non-resident vendors (D14). / "
                        "تُحدَّد فئة ضريبة الاستقطاع لبنود الموردين غير المقيمين فقط.")
    line.wht_category = wht_category or (doc.vendor.wht_category if not doc.vendor.is_resident else None)
    if line.wht_category is not None and line.wht_category.sharia_prohibited and entity.sharia_enforced:
        problems.append(f"Withholding tax category {line.wht_category.code} (loan charges) never arises where Sharia "
                        "is enforced (OQ-8, R5). / فئة أعباء القروض لا تنشأ في كيان يطبق الضوابط الشرعية.")
    if not (line.description_ar or line.description_en):
        problems.append("A line needs a description. / يحتاج البند إلى وصف.")
    if line.vat_category not in VatCategory.values:
        problems.append("Unknown VAT category. / فئة ضريبة غير معروفة.")
    if line.vat_attribution not in PurchaseDocumentLine.Attribution.values:
        problems.append("Unknown VAT attribution. / إسناد ضريبة غير معروف.")
    if not doc.vendor.is_resident and line.supply_kind not in PurchaseDocumentLine.SupplyKind.values:
        problems.append("Goods or services must be chosen for a non-resident vendor (R30). / "
                        "اختر سلعًا أو خدمات للمورد غير المقيم.")
    if line.vat_category in VatCategory.values and _rate_on(line.vat_category, doc.accounting_date) is None:
        problems.append(f"No VAT rate for category {line.vat_category} on {doc.accounting_date}. / لا توجد نسبة ضريبة.")
    if problems:
        raise _problems_error(problems, "line_invalid")
    _price_line(line, doc.accounting_date, _doc_places(doc))
    line.save()
    _touch(doc, user)
    doc.save(update_fields=["modified_by", "modified_at", "version", "updated_at"])
    return line


def _purchase_amounts(doc, lines):
    """R29 / R30 and OQ-11: VAT per category at document level; each line's cost and recoverable VAT.

    Returns (breakdown, net, charged_vat, rc_vat, costs {line_no: cost}, recoverable_vat, recoverable line numbers).
    Resident vendor: the vendor charges VAT; it is recoverable (1310) only for ``taxable`` lines with a vendor VAT
    number, otherwise it is added to the line's cost (``exempt``; ``mixed`` fully to cost until OQ-9 is signed off).
    Non-resident vendor: no VAT charged; standard-rated services are reverse charged (Dr 1310 or cost / Cr 2200);
    goods are not (import VAT at customs, OQ-29).
    Resident vendor without a VAT number on the document (D24, R92, E8a): not registered for VAT, so no VAT at
    all: every line VAT 0.00, nothing to 1310 and nothing added to cost; lines record category O (OQ-60 default).
    """
    resident = doc.vendor.is_resident
    places = _doc_places(doc)
    if _unregistered_vendor(doc):
        for line in lines:
            line.vat_amount = ZERO
        net = sum((l.net_amount for l in lines), ZERO)
        breakdown = [{"category": VatCategory.OUT_OF_SCOPE.value, "rate": "0.00", "taxable": _fmt(net, places),
                      "vat": _fmt(ZERO, places),
                      "reverse_charge": False, "unregistered_vendor": True}] if lines else []
        return breakdown, net, ZERO, ZERO, {l.line_no: l.net_amount for l in lines}, ZERO, set()
    recoverable_ok = bool(doc.vendor_vat_number) or not resident  # R30 overrides the VAT-number condition
    groups = {}
    for line in lines:
        rc_line = not resident and line.supply_kind == "services" and line.vat_category == VatCategory.STANDARD
        if not resident and not rc_line:
            continue  # non-resident goods (or non-S services): no KSA VAT in this module
        key = (line.vat_category, line.vat_rate.rate)
        groups.setdefault(key, []).append(line)
    costs = {line.line_no: line.net_amount for line in lines}
    breakdown, charged, rc_total, recoverable, recoverable_lines = [], ZERO, ZERO, ZERO, set()
    for (category, rate), group in sorted(groups.items(), key=lambda kv: kv[0]):
        taxable = sum((l.net_amount for l in group), ZERO)
        vat = _round(taxable * rate / HUNDRED, places)
        breakdown.append({"category": category, "rate": str(rate), "taxable": _fmt(taxable, places),
                          "vat": _fmt(vat, places), "reverse_charge": not resident})
        if resident:
            charged += vat
        else:
            rc_total += vat
        rec_lines = [l for l in group if l.vat_attribution == "taxable" and recoverable_ok]
        recoverable_lines |= {l.line_no for l in rec_lines}
        non_rec = [l for l in group if l not in rec_lines]
        to_cost = ZERO
        for l in non_rec:
            costs[l.line_no] += l.vat_amount
            to_cost += l.vat_amount
        rest = vat - to_cost
        if rec_lines:
            recoverable += rest
        elif non_rec:  # the category-level rounding difference stays on the last cost line
            costs[non_rec[-1].line_no] += rest
    net = sum((l.net_amount for l in lines), ZERO)
    return breakdown, net, charged, rc_total, costs, recoverable, recoverable_lines


def _purchase_problems(doc, check_period=True):
    problems = []
    entity = doc.entity
    lines = list(doc.lines.select_related("account", "vat_rate"))
    if not lines:
        problems.append("A document needs at least one line. / يحتاج المستند إلى بند واحد على الأقل.")
    if doc.vendor.entity_id != entity.pk:
        problems.append("The vendor belongs to another entity. / المورد يخص كيانًا آخر.")
    if _registered_vendor_number_missing(doc):  # R92 applies only to vendors not registered for VAT
        problems.append(
            f"Vendor {doc.vendor.code} is registered for VAT, so the bill needs the VAT number shown on the vendor "
            f"document (R92, R29). / المورد {doc.vendor.code} مسجل في ضريبة القيمة المضافة، لذا يلزم إدخال الرقم "
            "الضريبي الظاهر في مستند المورد.")
    try:
        _currency(doc.currency_id, entity)
    except InvoicingError as exc:
        problems += exc.messages
    for line in lines:
        problems += [_on_line(line.line_no, p) for p in _account_problems(
            entity, line.account, {Account.Type.EXPENSE, Account.Type.ASSET}, "expense or asset")]
        _vat_free_category(doc, line)  # R92: the VAT number may have been removed after the line was added
        _price_line(line, doc.accounting_date, _doc_places(doc))  # in memory: the rate on the accounting date (R10)
        if line.vat_rate is None:
            problems.append(_on_line(line.line_no, f"no VAT rate for category {line.vat_category} on {doc.accounting_date}. / "
                                                   f"لا توجد نسبة ضريبة للفئة {line.vat_category} بتاريخ {doc.accounting_date}."))
    if problems:
        return problems
    _, net, charged, _, _, _, _ = _purchase_amounts(doc, lines) if lines else (None, ZERO, ZERO, None, None, None, None)
    if _unregistered_vendor(doc) and doc.vendor_total_vat != ZERO:  # R92, OQ-60 default: refused, never booked
        problems.append(f"The vendor has no VAT number, so it is not registered for VAT and cannot charge VAT; the "
                        f"vendor document shows VAT {doc.vendor_total_vat}. Ask the vendor for a corrected document, "
                        f"or enter its VAT number if it is registered (R92). / المورد ليس له رقم ضريبي، فهو غير مسجل "
                        f"في ضريبة القيمة المضافة ولا يجوز له تحصيلها؛ ومستند المورد يُظهر ضريبة {doc.vendor_total_vat}. "
                        "اطلب من المورد مستندًا مصححًا، أو أدخل رقمه الضريبي إن كان مسجلًا.")
    elif lines and (doc.vendor_total_vat != charged or doc.vendor_total_gross != net + charged):  # OQ-11: exact match
        problems.append(f"Totals differ from the vendor document: computed VAT {charged}, total {net + charged}; "
                        f"vendor document VAT {doc.vendor_total_vat}, total {doc.vendor_total_gross}. / "
                        "الإجماليات تختلف عن مستند المورد.")
    if doc.doc_type == PurchaseDocument.DocType.VENDOR_CREDIT_NOTE:
        original = doc.original_document
        if original is None:  # OQ-10: a vendor credit note references a bill
            problems.append("A vendor credit note references its bill. / إشعار المورد الدائن يشير إلى فاتورته.")
        else:
            if (original.doc_type != PurchaseDocument.DocType.BILL or original.status != PStatus.POSTED
                    or original.vendor_id != doc.vendor_id or original.entity_id != entity.pk):
                problems.append("The original must be a posted bill of the same vendor and entity. / "
                                "يجب أن يكون الأصل فاتورة مرحّلة للمورد والكيان نفسيهما.")
            if original.currency_id != doc.currency_id:
                problems.append("A vendor credit note is in the currency of its bill. / يكون الإشعار بعملة فاتورته.")
            if any(line.account.code == SALE_ASSET for line in lines) and _bai_ajel_bill_use(original):
                problems.append(_bai_ajel_bill_message(original, credit_note=True))
            prior = PurchaseDocument.objects.filter(original_document=original, status=PStatus.POSTED).exclude(
                pk=doc.pk).aggregate(t=Sum("total_gross"))["t"] or ZERO
            if lines and net + charged > original.total_gross - prior:
                problems.append("The credit note exceeds the bill still open to credit. / الإشعار يتجاوز المتبقي من الفاتورة.")
    if check_period:
        period = _open_period(entity, doc.accounting_date)
        if period:
            problems.append(period)
    return problems


@transaction.atomic
def submit_purchase_document(document, *, user):
    """draft -> submitted (R58). An agent-created bill stays a draft until a human submits it (R22, AT-22)."""
    doc = PurchaseDocument.objects.select_for_update(of=("self",)).select_related("entity", "vendor").get(pk=document.pk)
    access.require(user, doc.entity, "invoicing.submit_bill")
    if doc.status != PStatus.DRAFT:
        raise error("Only drafts can be submitted.", "يمكن تقديم المسودات فقط.", code="not_draft")
    problems = _purchase_problems(doc, check_period=False)
    if problems:
        raise _problems_error(problems, "document_invalid")
    if _unregistered_vendor(doc):  # R92 / OQ-60 default: recorded as category O while the lines are still a draft
        for line in doc.lines.exclude(vat_category=VatCategory.OUT_OF_SCOPE):
            line.vat_category = VatCategory.OUT_OF_SCOPE
            _price_line(line, doc.accounting_date, _doc_places(doc))
            line.save()
    doc.status = PStatus.SUBMITTED
    doc.submitted_by, doc.submitted_at = user, timezone.now()
    _bump(doc)
    doc._service_write = True
    doc.save()
    return doc


def approve_purchase_document(document, *, user):
    """CFO approval posts a submitted bill or vendor credit note at once (R58, D17).

    Number and posting happen in one transaction: a refused posting (closed period, Sharia, ...) rolls back and
    the document stays ``submitted`` with no number used.
    """
    doc = PurchaseDocument.objects.select_related("entity").get(pk=document.pk)
    grant = access.require(user, doc.entity, "invoicing.approve_bill", document=doc)
    seen_version = doc.version
    with transaction.atomic():
        doc = PurchaseDocument.objects.select_for_update(of=("self",)).select_related("entity", "vendor").get(pk=doc.pk)
        if doc.status != PStatus.SUBMITTED:
            raise error("Only submitted documents are approved.", "تُعتمد المستندات المقدمة فقط.", code="not_submitted")
        _refuse_stale_grant(grant, doc, seen_version)
        targets = []
        if doc.original_document_id:  # the credit cap counts other notes: one note at a time per bill (item 4)
            targets = list(PurchaseDocument.objects.select_for_update(of=("self",)).filter(pk=doc.original_document_id))
            doc.original_document = targets[0]
        problems = _purchase_problems(doc)
        if problems:
            raise _problems_error(problems, "document_invalid")
        entity = doc.entity
        lines = list(doc.lines.select_related("account", "vat_rate"))
        for line in lines:  # R92: lines of an unregistered vendor's bill were stored as category O at submission
            _price_line(line, doc.accounting_date, _doc_places(doc))
        breakdown, net, charged, rc_vat, costs, recoverable, recoverable_lines = _purchase_amounts(doc, lines)
        for line in lines:
            line.vat_recoverable = line.line_no in recoverable_lines  # computed from R29 / R30, never user-set
            line._service_write = True
            line.save()
        doc.vat_breakdown = breakdown
        doc.total_net, doc.total_vat, doc.total_gross, doc.reverse_charge_vat = net, charged, net + charged, rc_vat
        doc.fx_rate, doc.fx_rate_date, doc.fx_rate_source, doc.fx_exchange_rate = _fx_for(
            entity, doc.currency_id, doc.accounting_date, user)
        doc.is_intercompany = doc.vendor.group_entity_id is not None
        doc.number = _next_number(entity, doc.doc_type, doc.accounting_date.year)
        doc.status = PStatus.POSTED
        doc.approved_by, doc.approved_at = user, timezone.now()
        doc._service_write = True
        doc.save()
        try:
            with _posting_as(user):
                entry = ledger.create_entry(
                    entity, doc.accounting_date, f"{doc.get_doc_type_display()} {doc.number} ({doc.vendor_document_number})",
                    None, user=user, reference=doc.number, post=True, grant=grant, document=doc,
                )
        except LedgerError as exc:
            raise _problems_error(exc.messages, getattr(exc, "code", None) or "posting_refused") from None
        doc.journal_entry = entry
        doc.requires_purification = entry.lines.filter(requires_purification=True).exists()
        doc._service_write = True
        doc.save(update_fields=["journal_entry", "requires_purification", "updated_at"])
        if targets:
            amount = min(doc.total_gross, _open_payable(targets[0]))
            if amount > ZERO:
                _allocate_payable(entity, None, doc, targets[0], amount, doc.accounting_date, user,
                                  via_settlement=doc.currency_id != entity.base_currency_id)
    return doc


def _purchase_document_lines(document):
    """Registered ``build_lines`` for PurchaseDocument: E7 to E10 from the stored document (R4, R29, R30)."""
    _require_posting_context(document, PStatus.POSTED)
    cur, functional = document.currency_id, document.entity.base_currency_id
    control, counterparty = _control(document.vendor, receivable=False)
    note = document.doc_type == PurchaseDocument.DocType.VENDOR_CREDIT_NOTE
    cost_side, control_side = ("credit", "debit") if note else ("debit", "credit")
    lines = list(document.lines.select_related("account", "vat_rate").order_by("line_no"))
    _, _, charged, rc_vat, costs, recoverable, _ = _purchase_amounts(document, lines)
    label = document.number
    plain = []
    for line in lines:
        if costs[line.line_no]:
            plain.append(_line(line.account, cost_side, costs[line.line_no], cur, functional,
                               description=f"{label} {line.description_ar or line.description_en}"))
    if recoverable:
        plain.append(_line(VAT_INPUT, cost_side, recoverable, cur, functional, description=f"{label} VAT input"))
    if rc_vat:  # R30: self-assessed output VAT on imported services
        other = "debit" if cost_side == "credit" else "credit"
        plain.append(_line(VAT_OUTPUT, other, rc_vat, cur, functional, description=f"{label} reverse charge VAT"))
    gross = document.total_gross
    settlements = []
    if note and document.original_document_id and cur != functional:
        amount = min(gross, _open_payable(document.original_document))
        item = _control_line(document.original_document)
        if amount > ZERO and item is not None:
            settlements.append((item, amount, f"{label} → {document.original_document.number}"))
            gross -= amount
    if gross:
        plain.append(_line(control, control_side, gross, cur, functional, counterparty, description=label))
    return _with_settlements(document, cur, _pinned(document, plain), settlements)


# --- receipts and payments (3.5) ---------------------------------------------------------------------

def _bank_account(entity, value):
    account = _account(entity, value or BANK)
    problems = []
    if account.entity_id != entity.pk or account.type != Account.Type.ASSET or not account.is_postable \
            or not account.is_active or account.code in CONTROL_ACCOUNTS:
        problems.append(f"Account {account.code} is not a bank account of {entity.code}. / "
                        f"الحساب {account.code} ليس حسابًا بنكيًا للكيان {entity.code}.")
    if problems:
        raise _problems_error(problems, "bank_account")
    return account


def _require_receipt_staff(user, entity):
    access.require(user, entity, "invoicing.allocate")
    if not access.has(user, entity, "invoicing.post_receipt"):
        access.deny(user, entity, "invoicing.post_receipt", "no_membership")


def create_receipt(entity, *, user, customer, date, amount, currency=None, bank_account=None, bank_charge="0",
                   kind=CustomerReceipt.Kind.RECEIPT, purpose=CustomerReceipt.Purpose.SETTLEMENT, reference_en="",
                   reference_ar="", bank_reference=""):
    """Record a draft customer receipt or refund. The recorder may post a receipt; a refund needs a second person
    (R33, D26, access R46).

    Recorded by holders of ``invoicing.post_receipt`` (checked with ``invoicing.allocate``, as a P permission is
    only granted for one stored document).
    """
    _require_receipt_staff(user, entity)
    _same_entity(entity, customer, "customer", "العميل")
    if purpose != CustomerReceipt.Purpose.SETTLEMENT:  # R65 to R67 (advances, deposits) are not built in slice 1
        raise error("Advance and deposit receipts need a qualifying contract (billing, not built yet).",
                    "مقبوضات الدفعات المقدمة والتأمينات تحتاج إلى عقد مؤهل (الفوترة غير مبنية بعد).", code="purpose")
    if kind not in CustomerReceipt.Kind.values:
        raise error("Unknown receipt kind.", "نوع سند غير معروف.", code="kind")
    cur = _currency(currency or customer.currency_id, entity)
    amount = _money(amount, "amount", positive=True, currency=cur)  # R93: the currency's own decimals
    charge = _money(bank_charge, "bank charge", currency=cur)
    if charge and (kind != CustomerReceipt.Kind.RECEIPT or charge >= amount):
        raise error("A bank charge is below the received amount, on receipts only.",
                    "الرسوم البنكية أقل من المبلغ المقبوض، وفي سندات القبض فقط.", code="bank_charge")
    with transaction.atomic():
        receipt = CustomerReceipt(
            entity=entity, customer=customer, kind=kind, purpose=purpose, date=date, amount=amount, bank_charge=charge,
            currency=cur, bank_account=_bank_account(entity, bank_account),
            reference_en=reference_en, reference_ar=reference_ar, bank_reference=bank_reference, created_by=user,
            source=_source(user),
        )
        receipt.save()
    return receipt


def _application_problems(source, target, amount, receivable, planned=ZERO):
    """R34: ``amount`` plus what is already ``planned`` for ``target`` on the same receipt or payment fits its open amount."""
    problems = []
    party_ok = (target.customer_id == source.customer_id) if receivable else (target.vendor_id == source.vendor_id)
    if target.entity_id != source.entity_id or not party_ok:
        problems.append("The document belongs to another entity or party. / المستند يخص كيانًا أو طرفًا آخر.")
    if receivable:
        if target.doc_type == SalesDocument.DocType.CREDIT_NOTE or target.status != Status.FINAL:
            problems.append("Only final invoices and debit notes are settled (R44). / تُسوّى الفواتير والإشعارات المدينة النهائية فقط.")
        if _is_deferred_sale(target):
            problems.append(" / ".join(DEFERRED_SALE_COLLECTED_BY_PAYROLL))
        open_ = _open_receivable(target)
    else:
        if target.doc_type != PurchaseDocument.DocType.BILL or target.status != PStatus.POSTED:
            problems.append("Only posted bills are settled. / تُسوّى فواتير الموردين المرحّلة فقط.")
        open_ = _open_payable(target)
    if target.currency_id != source.currency_id:
        problems.append("A settlement is in the document's currency (cross-currency settlement is not built). / "
                        "تكون التسوية بعملة المستند.")
    if planned + amount > open_:
        problems.append(f"{planned + amount} planned for {target.number} is more than its open amount {open_}. / "
                        f"{planned + amount} المخطط للمستند {target.number} أكبر من المبلغ المفتوح {open_}.")
    return problems


def _planned_for(movement, target):
    """What a draft receipt or payment already plans to settle on ``target`` (R34: summed per target)."""
    return _sum(movement.applications.filter(target_id=target.pk))


@transaction.atomic
def add_receipt_application(receipt, target, amount, *, user):
    """Plan which invoices a draft receipt settles; allocations are made when it posts (R34, ledger-fx R18)."""
    receipt = CustomerReceipt.objects.select_for_update(of=("self",)).select_related("entity").get(pk=receipt.pk)
    _require_receipt_staff(user, receipt.entity)
    if receipt.status != "draft" or receipt.kind != CustomerReceipt.Kind.RECEIPT:
        raise error("Only draft receipts get applications.", "تُضاف التخصيصات إلى مسودات سندات القبض فقط.", code="not_draft")
    amount = _money(amount, "amount", positive=True, currency=receipt.currency_id)
    target = SalesDocument.objects.select_for_update(of=("self",)).get(pk=target.pk)
    problems = _application_problems(receipt, target, amount, receivable=True, planned=_planned_for(receipt, target))
    planned = _sum(receipt.applications.all())
    if planned + amount > receipt.amount:
        problems.append("Applications exceed the receipt amount. / التخصيصات تتجاوز مبلغ السند.")
    if problems:
        raise _problems_error(problems, "application_invalid")
    app = ReceiptApplication(entity=receipt.entity, receipt=receipt, target=target, amount=amount)
    app.save()
    _touch(receipt, user)
    receipt._service_write = True
    receipt.save(update_fields=["modified_by", "modified_at", "version", "updated_at"])
    return app


def _post_money(movement, *, user, grant, receivable, approved=False):
    """Shared posting of a receipt or payment: lock, check, number, book, allocate (R26, R28, R33, R59).

    ``movement`` is the row the grant was checked against; its version must still be the locked row's.
    """
    seen_version = movement.version
    model = CustomerReceipt if receivable else VendorPayment
    target_model = SalesDocument if receivable else PurchaseDocument
    movement = model.objects.select_for_update(of=("self",)).select_related("entity").get(pk=movement.pk)
    entity = movement.entity
    expected = "draft" if receivable else "submitted"
    if movement.status != expected:
        raise error(f"Only {expected} documents are posted here.", "لا يُرحَّل هذا المستند في حالته الحالية.",
                    code="wrong_status")
    _refuse_stale_grant(grant, movement, seen_version)
    apps = list(movement.applications.select_related("target").order_by("pk"))
    targets = {t.pk: t for t in target_model.objects.select_for_update(of=("self",)).filter(pk__in=[a.target_id for a in apps])}
    wht_rows = []
    if not receivable and _needs_wht(movement.vendor, movement.kind):  # R62, R63: computed on the payment date
        _require_sar_functional(movement.entity)
        wht_rows, wht_total, wht_problems = _wht_plan(movement, movement.date)
        if wht_problems:
            raise _problems_error(wht_problems, "wht_invalid")
        movement.wht_amount, movement.wht_gross_up = wht_total, bool(movement.vendor.wht_gross_up)
    settles = movement.amount if receivable else movement.settled_amount
    problems, per_target = [], {}
    for app in apps:
        per_target[app.target_id] = per_target.get(app.target_id, ZERO) + app.amount
    for target_id, amount in per_target.items():  # R34: every application of one target together
        problems += _application_problems(movement, targets[target_id], amount, receivable)
    if sum((a.amount for a in apps), ZERO) > settles:
        problems.append("Applications exceed the amount. / التخصيصات تتجاوز المبلغ.")
    period = _open_period(entity, movement.date)
    if period:
        problems.append(period)
    if problems:
        raise _problems_error(problems, "document_invalid")
    movement.fx_rate, movement.fx_rate_date, movement.fx_rate_source, movement.fx_exchange_rate = _fx_for(
        entity, movement.currency_id, movement.date, user)
    movement.number = _next_number(entity, "RCPT" if receivable else "PAY", movement.date.year)
    movement.status = "posted"
    movement.posted_by, movement.posted_at = user, timezone.now()
    if approved:  # vendor payments: CFO approval posts (R59); recorded in the same write as the posting
        movement.approved_by, movement.approved_at = user, movement.posted_at
    movement._service_write = True
    movement.save()
    for row in wht_rows:  # R62: one immutable row per bill and category, read by the payment's build_lines
        _record_wht(movement, row, user)
    try:
        with _posting_as(user):
            entry = ledger.create_entry(entity, movement.date, f"{movement._meta.verbose_name} {movement.number}", None,
                                        user=user, reference=movement.number, post=True, grant=grant, document=movement)
    except LedgerError as exc:
        raise _problems_error(exc.messages, getattr(exc, "code", None) or "posting_refused") from None
    movement.journal_entry = entry
    movement._service_write = True
    movement.save(update_fields=["journal_entry", "updated_at"])
    foreign = movement.currency_id != entity.base_currency_id
    for app in apps:
        if receivable:
            _allocate_receivable(entity, movement, None, targets[app.target_id], app.amount, movement.date, user,
                                 via_settlement=foreign)
        else:
            _allocate_payable(entity, movement, None, targets[app.target_id], app.amount, movement.date, user,
                              via_settlement=foreign)
    return movement


def post_receipt(receipt, *, user):
    """Post a draft receipt or refund (``invoicing.post_receipt`` grant for that document).

    D26 / access R46: the person who recorded a customer receipt (``kind = receipt``) may post it, whatever
    ``segregation_required`` says. A refund to a customer keeps creator != poster: the ledger refuses a posting
    by its creator, submitter or last modifier (access R35, OQ-26).
    """
    receipt = CustomerReceipt.objects.select_related("entity").get(pk=receipt.pk)
    grant = access.require(user, receipt.entity, "invoicing.post_receipt", document=receipt)
    with transaction.atomic():
        return _post_money(receipt, user=user, grant=grant, receivable=True)


def _receipt_creator_may_post(document):
    """Registered ``creator_may_post`` (access R46, D26): only customer receipts, never refunds (OQ-26).

    Only a plain receipt with no bank charge: the charge is an expense (5700) that reduces the customer's
    receivable, so a recorder who could also post it alone could write off any balance as a "bank charge"
    (reviewer round 6). A receipt with a bank charge keeps creator ≠ poster (ledger R35).
    """
    return document.kind == CustomerReceipt.Kind.RECEIPT and (document.bank_charge or ZERO) == ZERO


def _receipt_lines(document):
    """Registered ``build_lines`` for CustomerReceipt: E5 / E6, settling foreign items at the carrying rate."""
    _require_posting_context(document, CustomerReceipt.Status.POSTED)
    cur, functional = document.currency_id, document.entity.base_currency_id
    control, counterparty = _control(document.customer, receivable=True)
    label = document.number
    if document.kind == CustomerReceipt.Kind.REFUND:  # E6
        return _pinned(document, [_line(control, "debit", document.amount, cur, functional, counterparty, label),
                                  _line(document.bank_account, "credit", document.amount, cur, functional,
                                        description=label)])
    plain = [_line(document.bank_account, "debit", document.amount - document.bank_charge, cur, functional,
                   description=label)]
    if document.bank_charge:  # OQ-23: booked gross to 5700, no VAT split
        plain.append(_line(BANK_CHARGES, "debit", document.bank_charge, cur, functional, description=f"{label} bank charge"))
    settlements, applied = [], ZERO
    for app in document.applications.select_related("target").order_by("pk"):
        item = _control_line(app.target)
        settlements.append((item, app.amount, f"{label} → {app.target.number}"))
        applied += app.amount
    if cur == functional:  # no settlement mechanics in the functional currency: one control line
        plain.append(_line(control, "credit", document.amount, cur, functional, counterparty, label))
        return plain
    if document.amount - applied:
        plain.append(_line(control, "credit", document.amount - applied, cur, functional, counterparty, label))
    return _with_settlements(document, cur, _pinned(document, plain), settlements)


def _needs_wht(vendor, kind):
    return not vendor.is_resident and kind == VendorPayment.Kind.PAYMENT


def _require_sar_functional(entity):
    """R62, R64 (review finding 7): WHT is assessed and returned in SAR (``base_amount_sar`` / ``wht_amount_sar``
    and the 2220 tie-out are functional amounts). An entity with another functional currency has no SAR books to
    tie to, so its WHT is refused until the owner decides how such an entity files (not specified)."""
    if entity.base_currency_id != SAR:
        raise error(f"Withholding tax needs SAR as the functional currency; {entity.code} keeps its books in "
                    f"{entity.base_currency_id} (R62, R64).",
                    f"تتطلب ضريبة الاستقطاع أن تكون العملة الوظيفية الريال السعودي؛ دفاتر {entity.code} بعملة "
                    f"{entity.base_currency_id}.", code="wht_requires_sar_functional")


def _no_payment_without_wht(vendor, kind):
    """D14 / R61: a payment to a non-resident vendor carries withholding tax, so it needs the vendor's withholding
    tax category; without one it is refused (the slice-1 block stays for those vendors)."""
    if _needs_wht(vendor, kind):
        _require_sar_functional(vendor.entity)
    if _needs_wht(vendor, kind) and vendor.wht_category_id is None:
        raise error(f"Vendor {vendor.code} is not resident: set its withholding tax category before paying it "
                    "(R61, D14).",
                    f"المورد {vendor.code} غير مقيم: حدّد فئة ضريبة الاستقطاع له قبل الدفع له.",
                    code="wht_category_required")


def create_vendor_payment(entity, *, user, vendor, date, amount, currency=None, bank_account=None,
                          kind=VendorPayment.Kind.PAYMENT, reference_en="", reference_ar="", bank_reference=""):
    """Draft vendor payment or refund received (``invoicing.create_vendor_payment``; agents never, catalogue)."""
    access.require(user, entity, "invoicing.create_vendor_payment")
    _same_entity(entity, vendor, "vendor", "المورد")
    vendor = Vendor.objects.get(pk=vendor.pk)  # the stored WHT settings, not the caller's copy
    _no_payment_without_wht(vendor, kind)
    if kind not in VendorPayment.Kind.values:
        raise error("Unknown payment kind.", "نوع سند غير معروف.", code="kind")
    cur = _currency(currency or vendor.currency_id, entity)
    with transaction.atomic():
        payment = VendorPayment(
            entity=entity, vendor=vendor, kind=kind, date=date,
            amount=_money(amount, "amount", positive=True, currency=cur),
            currency=cur, bank_account=_bank_account(entity, bank_account),
            reference_en=reference_en, reference_ar=reference_ar, bank_reference=bank_reference, created_by=user,
            source=_source(user),
        )
        payment.save()
    return payment


@transaction.atomic
def add_payment_application(payment, target, amount, *, user):
    payment = VendorPayment.objects.select_for_update(of=("self",)).select_related("entity").get(pk=payment.pk)
    access.require(user, payment.entity, "invoicing.create_vendor_payment")
    if payment.status != "draft" or payment.kind != VendorPayment.Kind.PAYMENT:
        raise error("Only draft payments get applications.", "تُضاف التخصيصات إلى مسودات سندات الصرف فقط.", code="not_draft")
    amount = _money(amount, "amount", positive=True, currency=payment.currency_id)
    target = PurchaseDocument.objects.select_for_update(of=("self",)).get(pk=target.pk)
    problems = _application_problems(payment, target, amount, receivable=False, planned=_planned_for(payment, target))
    # A non-resident payment settles cash plus the tax withheld (R62): its applications are checked against the
    # cash once the tax is computed, at submission and approval.
    if not _needs_wht(payment.vendor, payment.kind) and _sum(payment.applications.all()) + amount > payment.amount:
        problems.append("Applications exceed the payment amount. / التخصيصات تتجاوز مبلغ السند.")
    if problems:
        raise _problems_error(problems, "application_invalid")
    app = PaymentApplication(entity=payment.entity, payment=payment, target=target, amount=amount)
    app.save()
    _touch(payment, user)
    payment._service_write = True
    payment.save(update_fields=["modified_by", "modified_at", "version", "updated_at"])
    return app


@transaction.atomic
def submit_vendor_payment(payment, *, user):
    payment = VendorPayment.objects.select_for_update(of=("self",)).select_related("entity").get(pk=payment.pk)
    access.require(user, payment.entity, "invoicing.submit_vendor_payment")
    if payment.status != "draft":
        raise error("Only drafts can be submitted.", "يمكن تقديم المسودات فقط.", code="not_draft")
    if _needs_wht(payment.vendor, payment.kind):
        _no_payment_without_wht(payment.vendor, payment.kind)
        _, _, problems = _wht_plan(payment, payment.date)
        if problems:
            raise _problems_error(problems, "wht_invalid")
    payment.status = "submitted"
    payment.submitted_by, payment.submitted_at = user, timezone.now()
    _bump(payment)
    payment._service_write = True
    payment.save()
    return payment


def approve_vendor_payment(payment, *, user):
    """CFO approval posts a submitted vendor payment at once (R59); refused postings roll back (no number used)."""
    payment = VendorPayment.objects.select_related("entity").get(pk=payment.pk)
    grant = access.require(user, payment.entity, "invoicing.approve_vendor_payment", document=payment)
    _no_payment_without_wht(payment.vendor, payment.kind)
    with transaction.atomic():
        return _post_money(payment, user=user, grant=grant, receivable=False, approved=True)


def _payment_lines(document):
    """Registered ``build_lines`` for VendorPayment: E11, settling foreign bills at the carrying rate."""
    _require_posting_context(document, VendorPayment.Status.POSTED)
    cur, functional = document.currency_id, document.entity.base_currency_id
    control, counterparty = _control(document.vendor, receivable=False)
    label = document.number
    if document.kind == VendorPayment.Kind.REFUND_RECEIVED:
        return _pinned(document, [_line(document.bank_account, "debit", document.amount, cur, functional,
                                        description=label),
                                  _line(control, "credit", document.amount, cur, functional, counterparty, label)])
    plain = [_line(document.bank_account, "credit", document.amount, cur, functional, description=label)]
    if document.wht_amount:  # E20 / E21: withheld tax payable; borne by the entity on gross-up
        plain.append(_line(WHT_PAYABLE, "credit", document.wht_amount, cur, functional,
                           description=f"{label} withholding tax / ضريبة الاستقطاع"))
        if document.wht_gross_up:
            plain.append(_line(WHT_BORNE, "debit", document.wht_amount, cur, functional,
                               description=f"{label} withholding tax borne / ضريبة استقطاع محملة"))
    settlements, applied = [], ZERO
    for app in document.applications.select_related("target").order_by("pk"):
        settlements.append((_control_line(app.target), app.amount, f"{label} → {app.target.number}"))
        applied += app.amount
    settled = document.settled_amount
    if cur == functional:
        plain.append(_line(control, "debit", settled, cur, functional, counterparty, label))
        return plain
    if settled - applied:
        plain.append(_line(control, "debit", settled - applied, cur, functional, counterparty, label))
    return _with_settlements(document, cur, _pinned(document, plain), settlements)


# --- withholding tax (D14, R61 to R64; OQ-8 reference rates, tax-adviser sign-off pending) ---------------------

def _wht_category(code):
    """The current row of a WHT category code (rates change by new rows with the same code)."""
    row = WhtCategory.objects.filter(code=code).order_by("-valid_from").first()
    if row is None:
        raise error(f"Unknown withholding tax category {code!r}.", f"فئة ضريبة الاستقطاع {code!r} غير معروفة.",
                    code="unknown_reference")
    return row


def _wht_rate_row(code, on):
    """R62: the WhtCategory row of ``code`` effective on ``on`` (the payment date)."""
    for row in WhtCategory.objects.filter(code=code, valid_from__lte=on).order_by("-valid_from"):
        if row.valid_to is None or row.valid_to >= on:
            return row
    return None


def _treaty_certificate(vendor, code, on):
    """R62: a verified certificate valid on ``on`` for the vendor and category (one naming the category first,
    then one for all categories)."""
    qs = TreatyCertificate.objects.filter(entity=vendor.entity, vendor=vendor, verified_at__isnull=False,
                                          valid_from__lte=on, valid_to__gte=on).select_related("wht_category")
    specific = [c for c in qs if c.wht_category_id and c.wht_category.code == code]
    general = [c for c in qs if c.wht_category_id is None]
    found = sorted(specific, key=lambda c: c.pk) or sorted(general, key=lambda c: c.pk)
    return found[-1] if found else None


def _wht_plan(payment, on):
    """R61 to R63: withholding tax on a draft or submitted payment to a non-resident vendor, on date ``on``.

    Returns (rows, total, problems). Each application's settled amount is split over the bill's lines by WHT
    category, pro rata to line net (the last category takes the rounding). Rate: ``WhtCategory.rate`` effective on
    ``on``, or the ``treaty_rate`` of a verified treaty certificate valid on ``on``. Without gross-up WHT =
    round(base x rate) and the cash paid must be the settled amount less WHT; with ``vendor.wht_gross_up`` the
    vendor receives the full amount, WHT = round(base x rate / (1 - rate)) and is borne by the entity (E21).
    """
    vendor, entity = payment.vendor, payment.entity
    _require_sar_functional(entity)
    places = _places(payment.currency_id)
    gross_up = bool(vendor.wht_gross_up)
    problems, rows = [], []
    per_bill = {}
    for app in payment.applications.select_related("target").order_by("pk"):
        per_bill.setdefault(app.target_id, [app.target, ZERO])[1] += app.amount.quantize(_quantum(places))
    if not per_bill:
        problems.append("A payment to a non-resident vendor is applied to its bills, so the withholding tax can be "
                        "computed (R62). / تُخصَّص الدفعة لمورد غير مقيم إلى فواتيره لاحتساب ضريبة الاستقطاع.")
    for bill, settled in per_bill.values():
        lines = list(bill.lines.select_related("wht_category").order_by("line_no"))
        missing = [line.line_no for line in lines if line.wht_category_id is None]
        if missing:  # R61 / AT-57
            problems.append(f"Bill {bill.number}: line(s) {', '.join(map(str, missing))} have no withholding tax "
                            f"category (R61). / الفاتورة {bill.number}: بنود بلا فئة ضريبة استقطاع.")
            continue
        by_code = {}
        for line in lines:
            by_code[line.wht_category.code] = by_code.get(line.wht_category.code, ZERO) + line.net_amount
        total_net = sum(by_code.values(), ZERO)
        left = settled
        for i, (code, net) in enumerate(sorted(by_code.items())):
            base = left if i == len(by_code) - 1 else _round(settled * net / total_net, places)
            left -= base
            row = _wht_rate_row(code, on)
            if row is None:
                problems.append(f"No withholding tax rate for {code} on {on}. / لا توجد نسبة استقطاع للفئة {code} "
                                f"بتاريخ {on}.")
                continue
            if row.sharia_prohibited and entity.sharia_enforced:
                problems.append(f"Withholding tax category {code} never arises where Sharia is enforced (OQ-8). / "
                                f"فئة الاستقطاع {code} لا تنشأ في كيان يطبق الضوابط الشرعية.")
                continue
            cert = _treaty_certificate(vendor, code, on)
            rate = min(cert.treaty_rate, row.rate) if cert else row.rate  # a treaty never raises the rate
            r = rate / HUNDRED
            with localcontext() as ctx:
                ctx.prec = 60
                wht = _round(base * r / (1 - r), places) if gross_up else _round(base * r, places)
            rows.append({"bill": bill, "category": row, "base": base, "rate": rate, "certificate": cert,
                         "wht": wht, "gross_up": gross_up})
    total = sum((r["wht"] for r in rows), ZERO)
    settled_total = sum((s for _, s in per_bill.values()), ZERO)
    expected = settled_total if gross_up else settled_total - total
    if not problems and payment.amount != expected:
        problems.append(
            f"The cash paid must be {expected} {payment.currency_id}: settled {settled_total} "
            f"{'(withholding tax ' + str(total) + ' borne by the entity)' if gross_up else 'less withholding tax ' + str(total)}"
            f" (R62, R63). / يجب أن يكون المبلغ المدفوع {expected} {payment.currency_id}: المسدد {settled_total} "
            f"{'وتتحمل المنشأة ضريبة الاستقطاع ' + str(total) if gross_up else 'ناقص ضريبة الاستقطاع ' + str(total)}.")
    return rows, total, problems


def _record_wht(payment, row, user):
    """One WhtDeduction (R62, R64); SAR amounts at the payment's pinned rate, as its entry books them."""
    entity = payment.entity
    _require_sar_functional(entity)  # the functional amounts are the SAR amounts
    to_sar = (lambda a: fx.convert(a, payment.fx_rate)) if payment.currency_id != SAR else (lambda a: _round(a, 2))
    deduction = WhtDeduction(
        entity=entity, payment=payment, bill=row["bill"], wht_category=row["category"], base_amount=row["base"],
        base_amount_sar=to_sar(row["base"]), rate_applied=row["rate"], treaty_certificate=row["certificate"],
        wht_amount=row["wht"], wht_amount_sar=to_sar(row["wht"]), gross_up=row["gross_up"],
        return_month=payment.date.replace(day=1),
    )
    deduction._service_write = True
    deduction.save()
    return deduction


def wht_preview(payment, *, user):
    """What R62 / R63 withhold on a draft or submitted payment if it were posted today on its date:
    {"rows": [...], "wht": total, "cash_expected": ..., "problems": [...]}. ``invoicing.view``."""
    payment = VendorPayment.objects.select_related("entity", "vendor").get(pk=payment.pk)
    access.require(user, payment.entity, "invoicing.view")
    if not _needs_wht(payment.vendor, payment.kind):
        return {"rows": [], "wht": ZERO, "cash_expected": payment.amount, "problems": []}
    rows, total, problems = _wht_plan(payment, payment.date)
    settled = sum((r["base"] for r in rows), ZERO)
    return {
        "rows": [{"bill": r["bill"].number, "category": r["category"].code, "base": r["base"], "rate": r["rate"],
                  "treaty_certificate": r["certificate"].pk if r["certificate"] else None, "wht": r["wht"],
                  "gross_up": r["gross_up"]} for r in rows],
        "wht": total, "cash_expected": settled if payment.vendor.wht_gross_up else settled - total,
        "problems": problems,
    }


def _treaty_rate_cap(cert):
    """The statutory rate a treaty certificate may not exceed (review finding 6): its category's
    ``WhtCategory.rate`` effective on ``valid_from``; for a certificate for all categories, the lowest of them
    (Sharia-prohibited categories left out where Sharia is enforced). None if no rate is effective then."""
    on = cert.valid_from
    if cert.wht_category_id:
        rows = [_wht_rate_row(cert.wht_category.code, on)]
    else:
        rows = [_wht_rate_row(code, on) for code in
                WhtCategory.objects.order_by().values_list("code", flat=True).distinct()]
        rows = [r for r in rows if r is not None and not (r.sharia_prohibited and cert.entity.sharia_enforced)]
    rows = [r for r in rows if r is not None]
    return min(r.rate for r in rows) if rows else None


def _check_treaty_certificate(cert, vendor):
    """R62 (review findings 4, 6): what a certificate must satisfy when entered, changed and verified."""
    if vendor.is_resident:
        raise error("Treaty certificates are for non-resident vendors.", "شهادات الاتفاقيات للموردين غير المقيمين.",
                    code="vendor_resident")
    cert.country = str(cert.country or "").upper()
    try:
        validate_country_code(cert.country)
    except Exception:  # noqa: BLE001
        raise error(f"{cert.country!r} is not an ISO country code.", f"{cert.country!r} ليس رمز دولة صالحًا.",
                    code="country") from None
    residence = (vendor.residence_country or "").upper()
    if cert.country != residence:
        raise error(f"The treaty country {cert.country} is not the vendor's country of residence "
                    f"({residence or 'not set'}); treaty relief follows residence (R62).",
                    f"دولة الاتفاقية {cert.country} ليست دولة إقامة المورد ({residence or 'غير محددة'})؛ "
                    f"الإعفاء بموجب الاتفاقية يتبع الإقامة.", code="treaty_country_mismatch")
    rate = cert.treaty_rate
    if not isinstance(rate, Decimal) or not fx.has_places(rate, 2) or not (ZERO <= rate < HUNDRED):
        raise error("The treaty rate is a percentage from 0 to below 100 with 2 decimals.",
                    "نسبة الاتفاقية نسبة مئوية من 0 إلى أقل من 100 بخانتين عشريتين.", code="treaty_rate")
    if cert.valid_to < cert.valid_from:
        raise error("The certificate ends before it starts.", "تنتهي الشهادة قبل أن تبدأ.", code="treaty_dates")
    cap = _treaty_rate_cap(cert)
    if cap is None:
        raise error(f"No statutory withholding tax rate is effective on {cert.valid_from} to compare the treaty "
                    "rate with.", f"لا توجد نسبة استقطاع نظامية سارية بتاريخ {cert.valid_from} لمقارنة نسبة الاتفاقية بها.",
                    code="treaty_rate_above_statutory")
    if rate > cap:
        what = cert.wht_category.code if cert.wht_category_id else "the lowest category rate"
        what_ar = cert.wht_category.code if cert.wht_category_id else "أدنى نسبة بين الفئات"
        raise error(f"The treaty rate {rate}% is above the statutory rate {cap}% ({what}); a treaty only reduces "
                    "the tax (R62).",
                    f"نسبة الاتفاقية {rate}% أعلى من النسبة النظامية {cap}% ({what_ar})؛ الاتفاقية تخفض الضريبة فقط.",
                    code="treaty_rate_above_statutory")
    if cert.document:
        validate_treaty_document(cert.document)


TREATY_FIELDS = {"country", "treaty_rate", "wht_category", "valid_from", "valid_to", "received_on", "document"}


def _treaty_values(cert, fields):
    for name, value in fields.items():
        if name == "treaty_rate":
            value = _decimal(value, "treaty rate")
        elif name == "wht_category" and value is not None and not isinstance(value, WhtCategory):
            value = _wht_category(value)
        setattr(cert, name, value)


def add_treaty_certificate(vendor, *, user, country, treaty_rate, valid_from, valid_to, received_on,
                           wht_category=None, document=None):
    """Enter a treaty certificate for a non-resident vendor (``invoicing.enter_wht_data``; R62). It counts only
    once a second person verifies it (:func:`verify_treaty_certificate`). The country is the vendor's country of
    residence; the rate is not above the statutory rate (review finding 6); the scan is a PDF, PNG or JPG of at
    most 10 MB (finding 4)."""
    vendor = Vendor.objects.select_related("entity").get(pk=vendor.pk)
    access.require(user, vendor.entity, "invoicing.enter_wht_data")
    cert = TreatyCertificate(entity=vendor.entity, vendor=vendor, created_by=user)
    _treaty_values(cert, {"country": country, "treaty_rate": treaty_rate, "valid_from": valid_from,
                          "valid_to": valid_to, "received_on": received_on, "wht_category": wht_category})
    if document is not None:
        cert.document = document
    _check_treaty_certificate(cert, vendor)
    cert._service_write = True
    cert.save()
    return cert


@transaction.atomic
def update_treaty_certificate(certificate, *, user, **fields):
    """Change an unverified treaty certificate (``invoicing.enter_wht_data``); records who changed it, and that
    person cannot verify it either (review finding 4). A verified certificate is never changed."""
    cert = TreatyCertificate.objects.select_for_update(of=("self",)).select_related(
        "entity", "vendor", "wht_category").get(pk=certificate.pk)
    access.require(user, cert.entity, "invoicing.enter_wht_data")
    if cert.verified_at is not None:
        raise error("A verified treaty certificate is never edited; enter a new one.",
                    "لا تُعدَّل شهادة الاتفاقية بعد التحقق منها؛ أدخل شهادة جديدة.", code="certificate_verified")
    unknown = set(fields) - TREATY_FIELDS
    if unknown:
        raise error(f"These fields are not changed here: {', '.join(sorted(unknown))}.",
                    f"لا تُعدَّل هذه الحقول هنا: {', '.join(sorted(unknown))}.", code="field_not_editable")
    _treaty_values(cert, fields)
    _check_treaty_certificate(cert, cert.vendor)
    cert.modified_by, cert.modified_at = user, timezone.now()
    cert._service_write = True
    cert.save()
    return cert


@transaction.atomic
def verify_treaty_certificate(certificate, *, user):
    """A second person verifies a treaty certificate (``invoicing.verify_wht_data``; never the one who entered
    or last changed it). The R62 checks run again (the vendor's residence may have changed). From then on it is
    never edited (3.2)."""
    cert = TreatyCertificate.objects.select_for_update(of=("self",)).select_related(
        "entity", "vendor", "wht_category").get(pk=certificate.pk)
    access.require(user, cert.entity, "invoicing.verify_wht_data")
    if user.pk in (cert.created_by_id, cert.modified_by_id):
        access.deny(user, cert.entity, "invoicing.verify_wht_data", "self_approval")
    if cert.verified_at is not None:
        raise error("The certificate is already verified.", "تم التحقق من الشهادة مسبقًا.", code="certificate_verified")
    _check_treaty_certificate(cert, Vendor.objects.get(pk=cert.vendor_id))
    cert.verified_by, cert.verified_at = user, timezone.now()
    cert._service_write = True
    cert.save()
    return cert


# OQ-30 (payroll R43): withholding tax that other modules book on 2220 belongs in the same monthly return. An app
# registers its source from its AppConfig.ready(): ``rows(entity, start, end)`` returns rows in the shape below (with
# ``"source"`` naming the app; payees by code only, R91) and ``document_type`` is the ledger document type of its
# 2220 movements, so the 2220 tie-out covers them too. Payroll registers its remuneration WHT (payroll R43).
_WHT_RETURN_SOURCES = {}


def register_wht_return_source(document_type, rows):
    """Register another module's WHT rows for :func:`wht_return_data` (idempotent per document type)."""
    _WHT_RETURN_SOURCES[document_type] = rows


def wht_return_data(entity, month, *, user):
    """R64: monthly WHT return data of ``entity`` (``month`` = any day of it), per category and vendor, from
    ``WhtDeduction``: base, rate, WHT (SAR), treaty reference and country. A payment reversed in the month shows
    as a negative row. Ties to the 2220 movements of the month's vendor-payment entries (``ledger_2220_sar``).
    Remittance (E22) is not part of it. Individual vendors are shown by code only (R91). Refused with
    ``wht_requires_sar_functional`` for an entity whose functional currency is not SAR (review finding 7)."""
    access.require(user, entity, "invoicing.view")
    _require_sar_functional(entity)
    start = month.replace(day=1)
    end = date_type(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    rows = {}

    def add(d, sign):
        reversal = sign < 0
        key = (d.wht_category.code, d.payment.vendor.code, d.rate_applied, d.treaty_certificate_id, reversal)
        r = rows.setdefault(key, {
            "category": d.wht_category.code, "return_line_code": d.wht_category.return_line_code,
            "vendor": d.payment.vendor.code, "country": d.payment.vendor.residence_country, "rate": d.rate_applied,
            "treaty_certificate": d.treaty_certificate_id,
            "treaty_country": d.treaty_certificate.country if d.treaty_certificate_id else "",
            "base_sar": ZERO, "wht_sar": ZERO, "gross_up": d.gross_up,
        })
        if reversal:  # compliance B5: the row stays; the original month's return is to be amended
            month = d.return_month.strftime("%Y-%m")
            r.update({"reversal": True, "amends_return_month": month,
                      "note_en": f"Reversal of a payment of {month}: amend the WHT return of {month}",
                      "note_ar": f"عكس دفعة من شهر {month}: عدّل إقرار ضريبة الاستقطاع لشهر {month}"})
        r["base_sar"] += sign * d.base_amount_sar
        r["wht_sar"] += sign * d.wht_amount_sar

    base_qs = WhtDeduction.objects.filter(entity=entity).select_related(
        "wht_category", "payment__vendor", "treaty_certificate")
    for d in base_qs.filter(return_month=start):
        add(d, 1)
    for d in base_qs.filter(payment__reversal_entry__date__gte=start, payment__reversal_entry__date__lte=end):
        add(d, -1)
    lines = JournalLine.objects.filter(
        entry__entity=entity, entry__status__in=ledger.BOOKED, entry__date__gte=start, entry__date__lte=end,
        entry__document_type__in=["invoicing.vendorpayment", *sorted(_WHT_RETURN_SOURCES)], account__code=WHT_PAYABLE,
    ).aggregate(c=Sum("credit"), d=Sum("debit"))
    ledger_total = (lines["c"] or ZERO) - (lines["d"] or ZERO)
    out = list(rows.values())
    for _doc_type, source in sorted(_WHT_RETURN_SOURCES.items()):
        out.extend(source(entity, start, end))  # OQ-30: e.g. payroll remuneration WHT (payroll R43)
    out = sorted(out, key=lambda r: (r["category"], r["vendor"], r.get("source", "")))
    total = sum((r["wht_sar"] for r in out), ZERO)
    return {"entity": entity.code, "month": start, "rows": out, "total_wht_sar": total,
            "ledger_2220_sar": ledger_total, "difference": total - ledger_total}


# --- purification (R54, E14, E15; D31b purification account and charity recipients; OQ-30 amount, timing and the
# recipient list pending Sharia board sign-off) ------------------------------------------------------------------

PURIFICATION_PERMISSION = {
    PurificationTransfer.Kind.TRANSFER: "invoicing.post_purification_transfer",
    # owner decision C2 (2026-10-18): the CFO approves the funding of the purification account, never its recorder
    PurificationTransfer.Kind.FUNDING: "invoicing.approve_purification_funding",
    PurificationTransfer.Kind.PAYOUT: "invoicing.approve_purification_payout",
}
PKind = PurificationTransfer.Kind
# Owner decision C3 (2026-10-18): a charity recipient's approval lasts 12 months from the last board approval;
# alerts go out 30 and 7 days before it ends.
CHARITY_APPROVAL_YEARS = 1
CHARITY_ALERT_DAYS = (30, 7)


def _today():
    """Today in Asia/Riyadh (TIME_ZONE). A function so tests can move the clock."""
    return timezone.localdate()


def _require_person(user, entity, permission):
    """R54, D31b: purification records are handled by people only; never an agent or a system job."""
    if access_services.kind_of(user) != Kind.HUMAN:
        access.deny(user, entity, permission, "agent_forbidden")


def _posted_purification(entity, kind, as_of=None, exclude_pk=None):
    """Posted (not reversed) transfers, fundings or payouts of ``kind`` up to ``as_of``; reversed rows never count."""
    qs = PurificationTransfer.objects.filter(entity=entity, kind=kind, status=PurificationTransfer.Status.POSTED)
    if as_of is not None:
        qs = qs.filter(date__lte=as_of)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return _round(qs.aggregate(t=Sum("amount"))["t"] or ZERO, 2)


def _purification_series(entity, exclude_pk=None):
    """R6, R54, D31b: per booking date, the change of (flagged net, transferred by E14, 2600 balance, 1140 balance).

    Flagged lines are the ledger's (R6 amendment); transfers are posted E14 rows (reversed rows never count); the
    2600 Charity payable balance (credits less debits) and the 1140 purification account balance (debits less
    credits) are **all booked lines** on those accounts, so conventional interest credited to 2600 by banking B4
    is payable, and bank charges booked on 1140 count (review findings 3 and 4). Any return on the purification
    account itself (Islamic profit or conventional interest) is booked Dr 1140 / Cr 2600 (owner decision C5): it is
    owed to charity, payable at once and does not change the funding room."""
    deltas = {}

    def add(day, index, value):
        deltas.setdefault(day, [ZERO, ZERO, ZERO, ZERO])[index] += value or ZERO

    flagged = (JournalLine.objects.filter(entry__entity=entity, entry__status__in=ledger.BOOKED,
                                          requires_purification=True)
               .values("entry__date").annotate(c=Sum("credit"), d=Sum("debit")))
    for row in flagged:
        add(row["entry__date"], 0, (row["c"] or ZERO) - (row["d"] or ZERO))
    rows = (PurificationTransfer.objects.filter(entity=entity, kind=PKind.TRANSFER,
                                                status=PurificationTransfer.Status.POSTED)
            .exclude(pk=exclude_pk).values("date").annotate(t=Sum("amount")))
    for row in rows:
        add(row["date"], 1, row["t"])
    lines = (JournalLine.objects.filter(entry__entity=entity, entry__status__in=ledger.BOOKED,
                                        account__code__in=(CHARITY_PAYABLE, PURIFICATION_BANK))
             .values("entry__date", "account__code").annotate(d=Sum("debit"), c=Sum("credit")))
    for row in lines:
        d, c = row["d"] or ZERO, row["c"] or ZERO
        if row["account__code"] == CHARITY_PAYABLE:
            add(row["entry__date"], 2, c - d)
        else:
            add(row["entry__date"], 3, d - c)
    return deltas


# What each limit measures from (flagged, transferred, 2600 balance, 1140 balance).
_MEASURES = {
    "to_transfer": lambda f, t, p, c: f - t,  # flagged income not yet transferred to 2600 (E14)
    "payable": lambda f, t, p, c: p,  # 2600: owed to charity (E14 transfers and B4 interest, less payouts)
    "to_fund": lambda f, t, p, c: p - c,  # owed to charity but not yet in the purification account
}


def _purification_floor(entity, on, measure, exclude_pk=None):
    """The smallest value of ``measure`` on ``on`` **and on every later booking date**, as (amount, date): a row
    dated ``on`` moves it by the same amount on every later date, so a backdated row can never push a later date
    over its limit (review finding 1); the last date is the all-time position."""
    deltas = _purification_series(entity, exclude_pk)
    deltas.setdefault(on, [ZERO, ZERO, ZERO, ZERO])
    sums = [ZERO, ZERO, ZERO, ZERO]
    room = at = None
    for day in sorted(deltas):
        sums = [a + b for a, b in zip(sums, deltas[day])]
        if day < on:
            continue
        value = _MEASURES[measure](*sums)
        if room is None or value < room:
            room, at = value, day
    return _round(room, 2), at  # SQLite sums lose the scale; amounts are SAR with 2 decimals


def _purification_room(entity, kind, on, exclude_pk=None):
    """The room left for a ``kind`` row dated ``on`` (amount, date): a transfer up to flagged less transferred; a
    funding up to the 2600 balance less the 1140 balance (review finding 3: bank charges are topped up and the
    full amount owed reaches charity); a payout up to the 2600 balance (the 1140 balance is checked apart)."""
    measure = {PKind.TRANSFER: "to_transfer", PKind.FUNDING: "to_fund", PKind.PAYOUT: "payable"}[kind]
    return _purification_floor(entity, on, measure, exclude_pk)


def _purification_cash_room(entity, gl, on):
    """D31b: the purification account's GL (1140) balance on ``on`` and on every later booking date, as the
    smallest (amount, date): a credit on it never takes more than the account holds then or later."""
    return ledger._booked_balance_floor(entity, getattr(gl, "code", None) or PURIFICATION_BANK, on)


def purification_position(entity, *, user, as_of=None):
    """R6, R54 / AT-45, AT-71: non-compliant income flagged in the ledger (``ledger.purification_amount``), what was
    transferred to 2600 (E14), funded into the purification account (D31b) and paid out (E15), the booked 2600 and
    1140 balances, and what is left to transfer, to fund and to pay. ``invoicing.view`` and ``ledger.view``.
    Reversed rows do not count. ``payable`` is the 2600 balance (B4 interest included); ``to_fund`` is ``payable``
    less what the purification account holds (bank charges included)."""
    access.require(user, entity, "invoicing.view")
    flagged = ledger.purification_amount(entity, user=user, as_of=as_of)
    transferred = _posted_purification(entity, PKind.TRANSFER, as_of)
    funded = _posted_purification(entity, PKind.FUNDING, as_of)
    paid = _posted_purification(entity, PKind.PAYOUT, as_of)
    sums = [ZERO, ZERO, ZERO, ZERO]
    for day, delta in _purification_series(entity).items():
        if as_of is None or day <= as_of:
            sums = [a + b for a, b in zip(sums, delta)]
    payable, held = _round(sums[2], 2), _round(sums[3], 2)
    return {"flagged": flagged, "transferred": transferred, "funded": funded, "paid_out": paid,
            "to_transfer": flagged - transferred, "to_fund": payable - held, "payable": payable,
            "purification_account_balance": held}


def _purification_account(entity, user, *, lock=False, recorded=None):
    """D31b: the entity's active purification account (banking), or ``purification_account_required``."""
    if entity.base_currency_id != SAR:
        raise error(f"{entity.code} keeps its books in {entity.base_currency_id}; purification money is kept and paid "
                    "in SAR from a Saudi purification account (D31b).",
                    f"يمسك الكيان {entity.code} دفاتره بعملة {entity.base_currency_id}؛ تُحفظ مبالغ التطهير وتُصرف "
                    "بالريال من حساب تطهير سعودي.", code="purification_account_required")
    account = banking.purification_account(entity, user=user, lock=lock)
    if account is None:
        raise error(f"{entity.code} has no active purification account; designate one in banking first (D31b).",
                    f"لا يوجد للكيان {entity.code} حساب تطهير نشط؛ حدده في وحدة البنوك أولًا.",
                    code="purification_account_required")
    if recorded is not None and recorded != account.pk:
        raise error("The purification account changed after this row was recorded; record it again (D31b).",
                    "تغيّر حساب التطهير بعد تسجيل هذا القيد؛ سجّله من جديد.", code="purification_account_required")
    if account.gl_account.code != PURIFICATION_BANK or account.gl_account.entity_id != entity.pk:
        raise error(f"The purification account is not booked on {PURIFICATION_BANK} of {entity.code} (D31b).",
                    f"حساب التطهير غير مقيد على الحساب {PURIFICATION_BANK} للكيان {entity.code}.",
                    code="purification_account_required")
    return account


def _purification_recipient(entity, value, *, lock=False, on=None):
    """D31b: one approved, active charity recipient of ``entity``, or ``purification_recipient_not_approved``; C3:
    its board approval has not expired today nor on the payout date ``on`` (``charity_recipient_expired``)."""
    pk = getattr(value, "pk", value)
    qs = CharityRecipient.objects.filter(pk=pk) if pk else CharityRecipient.objects.none()
    if lock:
        qs = qs.select_for_update(of=("self",), no_key=True)
    recipient = qs.first()
    if recipient is None or recipient.entity_id != entity.pk or not recipient.is_approved or not recipient.is_active:
        raise error("Payouts go only to an approved, active charity recipient of this entity (D31b).",
                    "يُصرف فقط لجهة خيرية مستفيدة معتمدة ونشطة لهذا الكيان.", code="purification_recipient_not_approved")
    latest = max(filter(None, (_today(), on)))
    if recipient.is_expired(latest):
        if recipient.approval_expires_on is None:
            raise error(f"{recipient.name_en} has no board approval recorded; the Managing Partner records the board's "
                        "approval (minute reference and date) before it is paid (C5).",
                        f"لم يُسجَّل اعتماد مجلس الإدارة لـ {recipient.name_ar}؛ يسجّل الشريك المدير اعتماد المجلس (مرجع "
                        "المحضر وتاريخه) قبل الصرف لها.", code="charity_recipient_expired")
        raise error(f"The approval of {recipient.name_en} expired on {recipient.approval_expires_on} (12 months after "
                    "its last board approval); no payout goes to it on or after that date until the Managing Partner "
                    "records a new board approval (C3).",
                    f"انتهى اعتماد {recipient.name_ar} في {recipient.approval_expires_on} (بعد 12 شهرًا من آخر اعتماد "
                    "لمجلس الإدارة)؛ لا يُصرف لها في ذلك التاريخ أو بعده حتى يسجّل الشريك المدير اعتمادًا جديدًا من المجلس.",
                    code="charity_recipient_expired")
    return recipient


def _funding_source(entity, value):
    """D31b: the operating bank GL a funding is paid from: the entity's own SAR bank GL (1100 or a sub-account of
    1100), never the purification account itself."""
    if value in (None, ""):
        raise error("A funding needs the operating bank account it is paid from.",
                    "يحتاج التمويل إلى الحساب البنكي التشغيلي الذي يُدفع منه.", code="bank_account")
    account = _account(entity, value)
    parent_code = account.parent.code if account.parent_id else None
    if account.entity_id != entity.pk or account.type != Account.Type.ASSET or not account.is_postable \
            or not account.is_active or account.code == PURIFICATION_BANK or account.sharia_prohibited \
            or not (account.code == BANK or parent_code == BANK) \
            or account.currency_id not in (None, entity.base_currency_id):
        raise error(f"Account {account.code} is not an operating SAR bank account of {entity.code} (1100 or a "
                    f"sub-account of 1100, not {PURIFICATION_BANK}).",
                    f"الحساب {account.code} ليس حسابًا بنكيًا تشغيليًا بالريال للكيان {entity.code}.", code="bank_account")
    return account


def _prepare_purification(doc, user, *, lock=False):
    """D31b: set the purification account, bank GL and recipient of a funding or payout, or refuse it."""
    entity = doc.entity
    if doc.kind == PKind.TRANSFER:
        return
    account = _purification_account(entity, user, lock=lock, recorded=doc.purification_account_id)
    doc.purification_account = account
    if doc.kind == PKind.FUNDING:
        doc.bank_account = _funding_source(entity, doc.bank_account)
        return
    if doc.bank_account_id is not None and doc.bank_account_id != account.gl_account_id:
        raise error(f"Payouts are paid only from the purification account ({PURIFICATION_BANK}), not from "
                    f"{doc.bank_account.code} (D31b).",
                    f"يُصرف فقط من حساب التطهير ({PURIFICATION_BANK}) وليس من {doc.bank_account.code}.",
                    code="purification_account_required")
    doc.bank_account = account.gl_account
    recipient = _purification_recipient(entity, doc.recipient_id, lock=lock, on=doc.date)
    doc.recipient, doc.recipient_en, doc.recipient_ar = recipient, recipient.name_en, recipient.name_ar


def _purification_problems(doc, user, exclude_self=True):
    problems = []
    entity = doc.entity
    pk = doc.pk if exclude_self else None
    access.require(user, entity, "ledger.view")  # the limits come from the ledger's flagged lines
    left, at = _purification_room(entity, doc.kind, doc.date, exclude_pk=pk)
    if doc.kind == PKind.TRANSFER:
        if doc.amount > left:
            problems.append(f"Only {left} of non-compliant income is left to purify on {at} (R6, R54; checked on "
                            f"{doc.date} and every later date). / المتبقي من الإيرادات غير المتوافقة للتطهير {left} "
                            f"فقط بتاريخ {at} (يُتحقق في {doc.date} وفي كل تاريخ لاحق).")
    elif doc.kind == PKind.FUNDING:
        if doc.amount > left:
            problems.append(f"Only {left} owed to charity (2600) is not yet in the purification account "
                            f"({PURIFICATION_BANK}) on {at}; transfer first (D31b; checked on {doc.date} and every "
                            f"later date). / المستحق للجهات الخيرية (2600) غير الموجود في حساب التطهير "
                            f"({PURIFICATION_BANK}) {left} فقط بتاريخ {at}؛ حوّل أولًا (يُتحقق في {doc.date} وفي كل "
                            "تاريخ لاحق).")
    else:
        if doc.amount > left:
            problems.append(f"Only {left} is on the charity payable (2600) on {at}; transfer first (E14; checked on "
                            f"{doc.date} and every later date). / رصيد مستحقات التطهير {left} فقط بتاريخ {at}؛ "
                            f"حوّل أولًا (يُتحقق في {doc.date} وفي كل تاريخ لاحق).")
        cash, cash_at = _purification_cash_room(entity, doc.bank_account, doc.date)
        if doc.amount > cash:
            problems.append(f"Only {cash} is in the purification account ({PURIFICATION_BANK}) on {cash_at}; fund it "
                            f"first (D31b; checked on {doc.date} and every later date). / رصيد حساب التطهير "
                            f"({PURIFICATION_BANK}) {cash} فقط بتاريخ {cash_at}؛ موّله أولًا (يُتحقق في {doc.date} "
                            "وفي كل تاريخ لاحق).")
    period = _open_period(entity, doc.date)
    if period:
        problems.append(period)
    return problems


def _lock_purification(entity):
    """R54: posting and reversing purification rows of one entity run one at a time, so two concurrent posts can
    never both pass the limits (``FOR NO KEY UPDATE`` on the entity row: inserts that reference the entity are
    not blocked; a no-op on SQLite). The ledger takes the same lock for **every** entry with a line on 1140
    (bank charges, profit and interest in banking, migration openings, reversals), before validating it (review
    finding 2; registered in ``apps.py`` through ``ledger.restricted``)."""
    Entity.objects.select_for_update(no_key=True).filter(pk=getattr(entity, "pk", entity)).values_list(
        "pk", flat=True).first()


def _purification_gl_allowed(entry):
    """``ledger.restricted`` allowance for 1140: the entry of a purification funding or payout (they build their own
    lines, ``_purification_lines``)."""
    return PurificationTransfer.objects.filter(pk=entry.document_id, entity_id=entry.entity_id,
                                               kind__in=(PKind.FUNDING, PKind.PAYOUT)).exists()


def _purification_gl_check(entry, lines):
    """``ledger.restricted`` check for 1140 (review finding 2), run under :func:`_lock_purification`: an entry
    that takes money off the purification account (bank charge, reversal of a funding, profit or interest) never
    takes it below zero on its date or any later date. Payouts are checked by their own rules (E15)."""
    net = sum(((l.debit - l.credit) for l in lines if l.account.code == PURIFICATION_BANK), ZERO)
    if net >= ZERO:
        return []
    root = ledger_restricted.root_of(entry)
    if entry.reversal_of_id is None and root.document_type == "invoicing.purificationtransfer" and \
            PurificationTransfer.objects.filter(pk=root.document_id, kind=PKind.PAYOUT).exists():
        return []
    room, at = ledger._booked_balance_floor(entry.entity, PURIFICATION_BANK, entry.date)
    if -net <= room:
        return []
    return [f"The purification account ({PURIFICATION_BANK}) holds only {room} on {at}; this entry takes {-net} "
            f"off it on {entry.date} and would leave it below zero. Fund the purification account first "
            "(invoicing: purification funding), then post it again (D31b). / "
            f"رصيد حساب التطهير ({PURIFICATION_BANK}) {room} فقط بتاريخ {at}؛ هذا القيد يخصم منه {-net} بتاريخ "
            f"{entry.date} فيجعله سالبًا. موّل حساب التطهير أولًا (الفوترة: تمويل حساب التطهير) ثم أعد الترحيل."]


def create_purification_transfer(entity, *, user, kind, date, amount, bank_account=None, recipient=None,
                                 recipient_ar="", recipient_en="", reference_ar="", reference_en=""):
    """Record a purification transfer (E14), funding (D31b) or payout (E15) as a draft (R54). A person holding
    ``invoicing.post_purification_transfer`` records it; agents and system users never (R54: a human user). A
    second person posts it (:func:`post_purification_transfer`).

    ``bank_account``: for a funding, the operating bank GL it is paid from (1100 or a sub-account); for a payout
    only the purification account's GL (1140) is accepted and it is set by default. ``recipient``: for a payout,
    an approved, active :class:`CharityRecipient` of the entity (its names are copied to the row; ``recipient_en``
    / ``recipient_ar`` are not used any more and must agree if given)."""
    _require_person(user, entity, "invoicing.post_purification_transfer")
    if not access.has(user, entity, "invoicing.post_purification_transfer"):
        access.deny(user, entity, "invoicing.post_purification_transfer", "no_membership")
    access.require(user, entity, "ledger.view")  # the amounts come from the ledger's flagged lines
    if kind not in PKind.values:
        raise error("Unknown purification kind.", "نوع تطهير غير معروف.", code="kind")
    if kind != PKind.PAYOUT and recipient is not None:
        raise error("Only a payout has a charity recipient.", "الجهة الخيرية المستفيدة للصرف فقط.", code="kind")
    if kind == PKind.TRANSFER and bank_account is not None:
        raise error("A transfer (E14) books 4900 to 2600 only; it has no bank account.",
                    "التحويل يقيد من 4900 إلى 2600 فقط؛ بلا حساب بنكي.", code="kind")
    doc = PurificationTransfer(
        entity=entity, kind=kind, date=date, amount=_money(amount, "amount", positive=True,
                                                           currency=entity.base_currency_id),
        reference_ar=reference_ar or "", reference_en=reference_en or "", created_by=user, source=_source(user),
    )
    if bank_account is not None:
        doc.bank_account = _account(entity, bank_account)
    if kind == PKind.PAYOUT:
        doc.recipient_id = getattr(recipient, "pk", recipient)
    _prepare_purification(doc, user)
    if kind == PKind.PAYOUT:
        given = {(recipient_en or "").strip(), (recipient_ar or "").strip()} - {""}
        if given - {doc.recipient_en, doc.recipient_ar}:
            raise error("The recipient's names come from the approved charity recipient (D31b).",
                        "أسماء الجهة المستفيدة تؤخذ من سجل الجهة الخيرية المعتمدة.", code="purification_recipient_not_approved")
    problems = _purification_problems(doc, user)
    if problems:
        raise _problems_error(problems, "purification_invalid")
    doc.save()
    return doc


def _instruction_reference(doc):
    text = f"{doc.number} {doc.reference_en or doc.reference_ar}".strip()
    return text[:140]


def post_purification_transfer(transfer, *, user):
    """Post a recorded transfer (``invoicing.post_purification_transfer``), funding
    (``invoicing.approve_purification_funding``, the CFO, owner decision C2) or payout
    (``invoicing.approve_purification_payout``, the CFO): E14 / funding / E15 in one step, numbered ``PUR``. The
    recorder cannot post it (ledger segregation, access R35; approval permissions refuse the recorder too); agents
    never (R54). A payout is checked again against the purification account and the recipient (both locked until
    commit; the recipient's board approval must not have expired, C3) and produces its bank instruction."""
    doc = PurificationTransfer.objects.select_related("entity").get(pk=transfer.pk)
    permission = PURIFICATION_PERMISSION[doc.kind]
    _require_person(user, doc.entity, permission)
    grant = access.require(user, doc.entity, permission, document=doc)
    seen_version = doc.version
    with transaction.atomic():
        _lock_purification(doc.entity)
        doc = PurificationTransfer.objects.select_for_update(of=("self",)).select_related("entity").get(pk=doc.pk)
        if doc.status != PurificationTransfer.Status.DRAFT:
            raise error("Only recorded drafts are posted.", "تُرحَّل المسودات المسجلة فقط.", code="not_draft")
        _refuse_stale_grant(grant, doc, seen_version)
        _prepare_purification(doc, user, lock=True)
        problems = _purification_problems(doc, user)
        if problems:
            raise _problems_error(problems, "purification_invalid")
        doc.number = _next_number(doc.entity, "PUR", doc.date.year)
        doc.status = PurificationTransfer.Status.POSTED
        doc.posted_by, doc.posted_at = user, timezone.now()
        doc._service_write = True
        doc.save()
        label = (f"Purification {doc.get_kind_display()} {doc.number} / {doc.number}")
        try:
            with _posting_as(user):
                entry = ledger.create_entry(doc.entity, doc.date, label[:300], None, user=user, reference=doc.number,
                                            post=True, grant=grant, document=doc)
        except LedgerError as exc:
            raise _problems_error(exc.messages, getattr(exc, "code", None) or "posting_refused") from None
        doc.journal_entry = entry
        doc._service_write = True
        doc.save(update_fields=["journal_entry", "updated_at"])
        if doc.kind == PKind.PAYOUT:
            recipient, source = doc.recipient, doc.purification_account
            if keyed_hash(recipient.iban) != recipient.iban_hash or _bank_from_iban(recipient.iban) != recipient.bank \
                    or not source.iban_hash or keyed_hash(source.iban) != source.iban_hash:
                raise error("The stored IBAN of the recipient or of the purification account does not match its "
                            "hash or bank; nothing is paid (D31b).",
                            "رقم الآيبان المحفوظ للمستفيد أو لحساب التطهير لا يطابق بصمته أو بنكه؛ لا يُصرف شيء.",
                            code="purification_recipient_not_approved")
            instruction = PurificationInstruction(
                entity=doc.entity, payout=doc, from_account=source, from_iban_last4=source.iban_last4,
                from_iban_hash=source.iban_hash, recipient=recipient,
                beneficiary_name_en=recipient.name_en, beneficiary_name_ar=recipient.name_ar,
                beneficiary_bank_id=recipient.bank_id, beneficiary_iban=recipient.iban,
                beneficiary_iban_hash=recipient.iban_hash, beneficiary_iban_last4=recipient.iban_last4,
                amount=doc.amount, currency_id=SAR, reference=_instruction_reference(doc), execution_date=doc.date,
                created_by=user,
            )
            instruction._service_write = True
            instruction.save()
    return doc


def reverse_purification_transfer(transfer, *, user, reason, date=None):
    """Reverse a posted purification transfer (E14), funding (D31b) or payout (E15) (R54, review finding 8): through
    ``ledger.services.reverse_entry`` with ``invoicing.reverse_document`` (registered for the type), by a person,
    with a reason. The row becomes ``reversed`` and stops counting towards the limits. A transfer is not reversed
    while the payouts or fundings after it would then exceed what was transferred; a funding is not reversed while
    the purification account would then hold less than nothing (reverse the payout first). A reversed payout's
    bank instruction is cancelled. The reversal is dated on the original date if its period is open, else at the
    start of the first open period after it (R32). Owner decision C6: money in the purification account (1140,
    restricted cash) never flows back to the company; reversing a funding only corrects an error (wrong amount,
    date or source account), always here, with a reason, through ``ledger.services.reverse_entry``.
    """
    doc = PurificationTransfer.objects.select_related("entity").get(pk=transfer.pk)
    _require_person(user, doc.entity, "invoicing.reverse_document")
    grant = access.require(user, doc.entity, "invoicing.reverse_document", document=doc)
    if not (reason or "").strip():
        raise error("The reason for the reversal is required.", "سبب العكس مطلوب.", code="reason_required")
    with transaction.atomic():
        _lock_purification(doc.entity)
        doc = PurificationTransfer.objects.select_for_update(of=("self",)).select_related(
            "entity", "journal_entry", "bank_account").get(pk=doc.pk)
        if doc.status != PurificationTransfer.Status.POSTED or doc.journal_entry_id is None:
            raise error("Only posted purification rows are reversed.", "تُعكس قيود التطهير المرحّلة فقط.",
                        code="not_posted")
        on = _reversal_date(doc.entity, doc.journal_entry.date, date)
        if doc.kind == PKind.TRANSFER:  # the reversal takes doc.amount off 2600 from ``on`` onwards
            for measure, en, ar in (("payable", "the payouts would exceed what is owed on 2600",
                                     "تتجاوز المبالغ المصروفة المستحق في 2600"),
                                    ("to_fund", "the purification account would hold more than is owed on 2600 (its "
                                     "fundings)", "يزيد رصيد حساب التطهير (تمويله) على المستحق في 2600")):
                left, at = _purification_floor(doc.entity, on, measure)
                if left < doc.amount:
                    raise error(f"Without this transfer {en} by {doc.amount - left} on {at}; reverse them first "
                                "(R54).",
                                f"بدون هذا التحويل {ar} بمقدار {doc.amount - left} بتاريخ {at}؛ اعكسها أولًا.",
                                code="purification_invalid")
        elif doc.kind == PKind.FUNDING:
            gl = doc.purification_account.gl_account
            cash, at = _purification_cash_room(doc.entity, gl, on)
            if cash < doc.amount:
                raise error(f"The purification account holds only {cash} on {at}; reversing this funding of "
                            f"{doc.amount} would leave it below zero. Reverse the payout first (D31b).",
                            f"رصيد حساب التطهير {cash} فقط بتاريخ {at}؛ عكس هذا التمويل البالغ {doc.amount} يجعله "
                            "سالبًا. اعكس الصرف أولًا.", code="purification_invalid")
        instruction = PurificationInstruction.objects.select_for_update().filter(payout=doc).first()
        if instruction is not None and instruction.status in PurificationInstruction.SENT_STATUSES:
            raise error("The bank instruction of this payout was already exported to the bank; the payout is not "
                        "reversed. How a returned or failed bank payment is booked is still open (D31b, OQ-64(d)).",
                        "صُدِّر الأمر البنكي لهذا الصرف إلى البنك؛ لا يُعكس الصرف. طريقة قيد الدفعة المرتجعة أو "
                        "الفاشلة لم تُحدد بعد.",
                        code="instruction_exported")
        try:
            reversal = ledger.reverse_entry(doc.journal_entry, grant, date=on, reason=reason.strip()[:200])
        except LedgerError as exc:
            raise _problems_error(exc.messages, getattr(exc, "code", None) or "reversal_refused") from None
        doc.status = PurificationTransfer.Status.REVERSED
        doc.reversal_entry, doc.reversed_by, doc.reversed_at = reversal, user, timezone.now()
        doc.reversal_reason = reason.strip()[:300]
        doc._service_write = True
        doc.save(update_fields=["status", "reversal_entry", "reversed_by", "reversed_at", "reversal_reason",
                                "updated_at"])
        if instruction is not None and instruction.status == PurificationInstruction.Status.PENDING:
            instruction.status = PurificationInstruction.Status.CANCELLED
            instruction.cancelled_by, instruction.cancelled_at = user, timezone.now()
            instruction.cancel_reason = doc.reversal_reason
            instruction._service_write = True
            instruction.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancel_reason"])
    return doc


def _purification_lines(document):
    """Registered ``build_lines`` for PurificationTransfer: E14 (Dr 4900 / Cr 2600), funding (Dr 1140 / Cr operating
    bank) or E15 (Dr 2600 / Cr 1140), D31b."""
    _require_posting_context(document, PurificationTransfer.Status.POSTED)
    label = f"{document.number} {document.reference_ar or document.reference_en}".strip()
    if document.kind == PKind.TRANSFER:
        return [{"account": NON_COMPLIANT_INCOME, "debit": document.amount, "description": label},
                {"account": CHARITY_PAYABLE, "credit": document.amount, "description": label}]
    purification_gl = document.purification_account.gl_account if document.purification_account_id else None
    if purification_gl is None or purification_gl.code != PURIFICATION_BANK:
        raise error("A funding or payout books against the purification account (1140) only (D31b).",
                    "يُقيد التمويل أو الصرف على حساب التطهير (1140) فقط.", code="purification_account_required")
    if document.kind == PKind.FUNDING:
        return [{"account": purification_gl, "debit": document.amount, "description": label},
                {"account": document.bank_account, "credit": document.amount, "description": label}]
    if document.bank_account_id != purification_gl.pk:
        raise error("A payout is paid from the purification account (1140) only (D31b).",
                    "يُصرف من حساب التطهير (1140) فقط.", code="purification_account_required")
    return [{"account": CHARITY_PAYABLE, "debit": document.amount, "description": label},
            {"account": purification_gl, "credit": document.amount,
             "description": f"{label} {document.recipient_ar}"[:300]}]


# --- charity recipients and bank instructions (R54 amendment D31b) ---------------------------------------------

def _bank_from_iban(iban):
    """review finding 8: the bank of a Saudi IBAN is read from its positions 5-6 (the SAMA bank code): the one
    active Saudi bank with that code, else None (unknown or ambiguous code)."""
    norm = iban_rules.normalise(iban or "")
    if len(norm) < 6 or not norm.startswith("SA"):
        return None
    banks = list(Bank.objects.filter(country="SA", sa_bank_code=norm[4:6], is_active=True)[:2])
    return banks[0] if len(banks) == 1 else None


def _charity_bank(value, norm):
    """The recipient's bank is always the one its IBAN names (positions 5-6); a bank given by the caller must be
    that bank (review finding 8)."""
    derived = _bank_from_iban(norm)
    if derived is None:
        raise error(f"No active Saudi bank carries the bank code {norm[4:6]!r} of this IBAN (positions 5-6); the "
                    "recipient's bank is taken from its IBAN (D31b).",
                    f"لا يوجد بنك سعودي نشط برمز البنك {norm[4:6]!r} في هذا الآيبان (الخانتان 5-6)؛ يؤخذ بنك "
                    "المستفيد من رقم الآيبان.", code="charity_recipient_invalid")
    if value not in (None, ""):
        given = value if isinstance(value, Bank) else Bank.objects.filter(code=value).first()
        if given is None or given.pk != derived.pk:
            raise error(f"This IBAN belongs to {derived.name_en} (bank code {derived.sa_bank_code}, positions 5-6), "
                        f"not to {getattr(given, 'name_en', value)}.",
                        f"هذا الآيبان يخص {derived.name_ar} (رمز البنك {derived.sa_bank_code}، الخانتان 5-6) وليس "
                        f"{getattr(given, 'name_ar', value)}.", code="charity_recipient_invalid")
    return derived


def create_charity_recipient(entity, *, user, name_en, name_ar, iban, bank=None, platform_reference=""):
    """Add a charity recipient for purification payouts (D31b; ``invoicing.manage_charity_recipient``, people
    only). Its bank is read from the IBAN's bank code (positions 5-6); ``bank``, if given, must be that bank. It
    can be paid only after a Managing Partner who did not add it approves it."""
    _require_person(user, entity, "invoicing.manage_charity_recipient")
    access.require(user, entity, "invoicing.manage_charity_recipient")
    norm = iban_rules.normalise(iban)
    name_en, name_ar = (name_en or "").strip(), (name_ar or "").strip()
    problems = []
    if not (name_en and name_ar):
        problems.append("A charity recipient needs an English and an Arabic name. / "
                        "تحتاج الجهة الخيرية المستفيدة إلى اسم بالإنجليزية والعربية.")
    iban_problems = iban_rules.saudi_problems(norm)
    problems += iban_problems
    if not iban_problems:
        try:
            bank = _charity_bank(bank, norm)
        except InvoicingError as exc:
            problems += exc.messages
            bank = None
    if not iban_problems and CharityRecipient.objects.filter(entity=entity, iban_hash=keyed_hash(norm),
                                                             is_active=True).exists():
        problems.append("This IBAN is already an active charity recipient of this entity. / "
                        "رقم الآيبان هذا مسجل لجهة خيرية نشطة لهذا الكيان.")
    if len((platform_reference or "").strip()) > 100:
        problems.append("The platform reference has at most 100 characters. / مرجع المنصة بحد أقصى 100 حرف.")
    if problems:
        raise _problems_error(problems, "charity_recipient_invalid")
    recipient = CharityRecipient(entity=entity, name_en=name_en, name_ar=name_ar, bank=bank, iban=norm,
                                 iban_hash=keyed_hash(norm), iban_last4=iban_rules.last4(norm),
                                 platform_reference=(platform_reference or "").strip(), created_by=user)
    recipient._service_write = True
    recipient.save()
    return recipient


def _board_approval_fields(minute_reference, board_approved_on, today):
    """C5: the board approval the Managing Partner records: a minute reference and the board's date (not in the
    future, and recent enough that its 12 months have not run out). Returns (reference, date, expires_on)."""
    reference = (minute_reference or "").strip()
    if not reference or board_approved_on in (None, ""):
        raise error("The board approves the charity list: record the board minute reference and the date of the "
                    "board's approval (C5).",
                    "يعتمد مجلس الإدارة قائمة الجهات الخيرية: سجّل مرجع محضر المجلس وتاريخ اعتماده.",
                    code="board_approval_required")
    if len(reference) > 100:
        raise error("The board minute reference has at most 100 characters.",
                    "مرجع محضر مجلس الإدارة بحد أقصى 100 حرف.", code="board_approval_invalid")
    if not isinstance(board_approved_on, date_type):
        raise error("The board approval date must be a date.", "تاريخ اعتماد مجلس الإدارة يجب أن يكون تاريخًا.",
                    code="board_approval_invalid")
    if board_approved_on > today:
        raise error(f"The board approval date {board_approved_on} is in the future.",
                    f"تاريخ اعتماد مجلس الإدارة {board_approved_on} في المستقبل.", code="board_approval_invalid")
    expires = _plus_years(board_approved_on, CHARITY_APPROVAL_YEARS)
    if expires <= today:
        raise error(f"A board approval of {board_approved_on} expired on {expires} (12 months, C3); record a newer "
                    "board approval.",
                    f"اعتماد مجلس الإدارة بتاريخ {board_approved_on} انتهى في {expires} (12 شهرًا)؛ سجّل اعتمادًا أحدث.",
                    code="board_approval_invalid")
    return reference, board_approved_on, expires


def approve_charity_recipient(recipient, *, user, board_minute_reference=None, board_approved_on=None):
    """D31b, C3, C5: a Managing Partner (``invoicing.approve_charity_recipient``, A + S!) who did not add the
    recipient records the **board's** approval of it (minute reference and date, :class:`CharityBoardApproval`);
    only then can payouts go to it, until 12 months after the board's date. People only.

    - an unapproved, active recipient: first approval;
    - an approved, active recipient (expired or not): re-approval with a later board approval; it resets the 12
      months (C3);
    - a deactivated recipient whose reactivation the accountant requested
      (:func:`request_charity_recipient_reactivation`): reactivation, by a person other than the requester (C3: no
      re-entry). The deactivation it ends and the request are kept on the board approval row.
    """
    recipient = CharityRecipient.objects.select_related("entity").get(pk=getattr(recipient, "pk", recipient))
    entity = recipient.entity
    permission = "invoicing.approve_charity_recipient"
    _require_person(user, entity, permission)
    access.require(user, entity, permission, document=recipient)
    today = _today()
    reference, board_on, expires = _board_approval_fields(board_minute_reference, board_approved_on, today)
    with transaction.atomic():
        recipient = CharityRecipient.objects.select_for_update(of=("self",)).select_related("board_approval").get(
            pk=recipient.pk)
        extra = {}
        if not recipient.is_active:
            if recipient.reactivation_requested_by_id is None:
                raise error("A deactivated charity recipient is reactivated only after the accountant asks for it "
                            "(C3).", "يُعاد تفعيل الجهة الخيرية الموقوفة فقط بعد أن يطلب المحاسب ذلك.",
                            code="recipient_inactive")
            if recipient.reactivation_requested_by_id == user.pk:  # C3: a second person approves the reactivation
                access.deny(user, entity, permission, "self_approval")
            if CharityRecipient.objects.filter(entity=entity, iban_hash=recipient.iban_hash, is_active=True).exists():
                raise error("This IBAN is already an active charity recipient of this entity.",
                            "رقم الآيبان هذا مسجل لجهة خيرية نشطة لهذا الكيان.", code="charity_recipient_invalid")
            kind = CharityBoardApproval.Kind.REACTIVATION
            extra = dict(requested_by_id=recipient.reactivation_requested_by_id,
                         request_reason=recipient.reactivation_reason,
                         ended_deactivation_by_id=recipient.deactivated_by_id,
                         ended_deactivation_at=recipient.deactivated_at,
                         ended_deactivation_reason=recipient.deactivation_reason)
        elif recipient.is_approved:
            kind = CharityBoardApproval.Kind.REAPPROVAL
        else:
            kind = CharityBoardApproval.Kind.APPROVAL
        last = recipient.board_approval
        if kind == CharityBoardApproval.Kind.REACTIVATION:
            deactivated_on = timezone.localdate(recipient.deactivated_at) if recipient.deactivated_at else None
            if deactivated_on is not None and board_on < deactivated_on:
                raise error(f"The recipient was deactivated on {deactivated_on}; its reactivation needs a board "
                            "approval given on or after that date (C3, C5).",
                            f"أوقفت الجهة في {deactivated_on}؛ تحتاج إعادة تفعيلها إلى اعتماد من مجلس الإدارة في ذلك "
                            "التاريخ أو بعده.", code="board_approval_invalid")
        elif last is not None and board_on <= last.board_approved_on:
            raise error(f"The last board approval is of {last.board_approved_on}; record a later one to approve the "
                        "recipient again (C3).",
                        f"آخر اعتماد لمجلس الإدارة بتاريخ {last.board_approved_on}؛ سجّل اعتمادًا لاحقًا لإعادة "
                        "الاعتماد.", code="board_approval_invalid")
        if _bank_from_iban(recipient.iban) != recipient.bank or keyed_hash(recipient.iban) != recipient.iban_hash:
            raise error("The recipient's bank is not the bank its IBAN names (positions 5-6); add it again.",
                        "بنك المستفيد ليس البنك الذي يدل عليه رقم الآيبان (الخانتان 5-6)؛ أضفه من جديد.",
                        code="charity_recipient_invalid")
        board = CharityBoardApproval(entity=entity, recipient=recipient, kind=kind, minute_reference=reference,
                                     board_approved_on=board_on, expires_on=expires,
                                     previous_expires_on=recipient.approval_expires_on, recorded_by=user, **extra)
        board._service_write = True
        board.save()
        fields = ["approved_by", "approved_at", "board_approval", "approval_expires_on"]
        recipient.approved_by, recipient.approved_at = user, timezone.now()
        recipient.board_approval, recipient.approval_expires_on = board, expires
        if kind == CharityBoardApproval.Kind.REACTIVATION:
            recipient.is_active = True
            recipient.deactivated_by = recipient.deactivated_at = None
            recipient.deactivation_reason = ""
            recipient.reactivation_requested_by = recipient.reactivation_requested_at = None
            recipient.reactivation_reason = ""
            fields += ["is_active", "deactivated_by", "deactivated_at", "deactivation_reason",
                       "reactivation_requested_by", "reactivation_requested_at", "reactivation_reason"]
        recipient._service_write = True
        recipient.save(update_fields=fields)  # never re-encrypts the IBAN
    return recipient


def deactivate_charity_recipient(recipient, *, user, reason):
    """D31b: stop paying a charity recipient (``invoicing.manage_charity_recipient``). Payouts already posted keep
    it. C3: it can be reactivated later without re-entry (:func:`request_charity_recipient_reactivation`, then a
    board approval recorded by a second person)."""
    recipient = CharityRecipient.objects.select_related("entity").get(pk=getattr(recipient, "pk", recipient))
    _require_person(user, recipient.entity, "invoicing.manage_charity_recipient")
    access.require(user, recipient.entity, "invoicing.manage_charity_recipient")
    if not (reason or "").strip():
        raise error("A reason is required.", "السبب مطلوب.", code="reason_required")
    with transaction.atomic():
        recipient = CharityRecipient.objects.select_for_update().get(pk=recipient.pk)
        if not recipient.is_active:
            raise error("This charity recipient is already inactive.", "هذه الجهة الخيرية موقوفة مسبقًا.",
                        code="recipient_inactive")
        recipient.is_active = False
        recipient.deactivated_by, recipient.deactivated_at = user, timezone.now()
        recipient.deactivation_reason = reason.strip()[:300]
        recipient._service_write = True
        recipient.save(update_fields=["is_active", "deactivated_by", "deactivated_at", "deactivation_reason"])
    return recipient


def request_charity_recipient_reactivation(recipient, *, user, reason):
    """C3: ask for a deactivated charity recipient to be paid again (``invoicing.manage_charity_recipient``, people
    only), with a reason. Nothing is re-entered: names, bank and IBAN stay as approved. It becomes active only when a
    second person records a board approval (:func:`approve_charity_recipient`)."""
    recipient = CharityRecipient.objects.select_related("entity").get(pk=getattr(recipient, "pk", recipient))
    _require_person(user, recipient.entity, "invoicing.manage_charity_recipient")
    access.require(user, recipient.entity, "invoicing.manage_charity_recipient")
    if not (reason or "").strip():
        raise error("A reason is required.", "السبب مطلوب.", code="reason_required")
    with transaction.atomic():
        recipient = CharityRecipient.objects.select_for_update().get(pk=recipient.pk)
        if recipient.is_active:
            raise error("This charity recipient is active; only a deactivated one is reactivated.",
                        "هذه الجهة الخيرية نشطة؛ يُعاد تفعيل الجهة الموقوفة فقط.", code="recipient_active")
        if recipient.reactivation_requested_by_id is not None:
            raise error("Its reactivation was already requested; it waits for the board approval.",
                        "طُلبت إعادة تفعيلها مسبقًا؛ وهي بانتظار اعتماد مجلس الإدارة.", code="already_requested")
        if CharityRecipient.objects.filter(entity=recipient.entity, iban_hash=recipient.iban_hash,
                                           is_active=True).exists():
            raise error("This IBAN is already an active charity recipient of this entity.",
                        "رقم الآيبان هذا مسجل لجهة خيرية نشطة لهذا الكيان.", code="charity_recipient_invalid")
        recipient.reactivation_requested_by, recipient.reactivation_requested_at = user, timezone.now()
        recipient.reactivation_reason = reason.strip()[:300]
        recipient._service_write = True
        recipient.save(update_fields=["reactivation_requested_by", "reactivation_requested_at",
                                      "reactivation_reason"])
    return recipient


def charity_board_approvals(recipient, *, user):
    """C5: the board approvals recorded for a charity recipient, newest first (``invoicing.view`` or the approver's
    right)."""
    recipient = CharityRecipient.objects.select_related("entity").get(pk=getattr(recipient, "pk", recipient))
    _require_recipient_view(user, recipient.entity)
    return CharityBoardApproval.objects.filter(recipient=recipient, entity=recipient.entity)


def _alert_addressees(entity):
    """C3: the people of ``entity`` who add or approve charity recipients (accountant, Managing Partner)."""
    at = timezone.now()
    perms = ("invoicing.manage_charity_recipient", "invoicing.approve_charity_recipient")
    users = {m.user for m in Membership.objects.active(at).filter(entity=entity, role__permission_rows__codename__in=perms)
             .select_related("user")}
    return sorted((u for u in users if access_services.kind_of(u) == Kind.HUMAN
                   and any(access.has(u, entity, perm) for perm in perms)), key=lambda u: u.pk)


def send_charity_recipient_alerts(*, today=None, entity=None):
    """``invoicing_charity_recipient_alerts`` (daily; owner decision C3): for each active, approved charity recipient
    whose approval expires within 30 days, the first run on or after (expiry − 30) and on or after (expiry − 7)
    alerts the entity's accountants and Managing Partners once each (only the most urgent threshold reached).
    Text en/ar with the charity's name and the date, never its IBAN. Returns the number of recipients alerted."""
    today = today or _today()
    horizon = today + timedelta(days=max(CHARITY_ALERT_DAYS))
    qs = CharityRecipient.objects.filter(is_active=True, approved_by__isnull=False, approval_expires_on__gt=today,
                                         approval_expires_on__lte=horizon).select_related("entity")
    if entity is not None:
        qs = qs.filter(entity=entity)
    alerted = 0
    for rec in qs.order_by("pk"):
        days_left = (rec.approval_expires_on - today).days
        threshold = min(t for t in CHARITY_ALERT_DAYS if days_left <= t)
        with transaction.atomic():
            if CharityRecipientAlert.objects.filter(charity_recipient=rec, expires_on=rec.approval_expires_on,
                                                    days_before__lte=threshold).exists():
                continue
            en = (f"{rec.entity.code}: the approval of charity recipient {rec.name_en} expires on "
                  f"{rec.approval_expires_on} (in {days_left} days). No payout goes to it from that date until the "
                  "Managing Partner records a new board approval.")
            ar = (f"{rec.entity.code}: ينتهي اعتماد الجهة الخيرية {rec.name_ar} في {rec.approval_expires_on} (بعد "
                  f"{days_left} يومًا). لا يُصرف لها من ذلك التاريخ حتى يسجّل الشريك المدير اعتمادًا جديدًا من مجلس "
                  "الإدارة.")
            for person in _alert_addressees(rec.entity):
                profile = access_services.profile_of(person)
                CharityRecipientAlert.objects.get_or_create(
                    charity_recipient=rec, addressee=person, expires_on=rec.approval_expires_on,
                    days_before=threshold,
                    defaults=dict(entity=rec.entity, text_en=en, text_ar=ar,
                                  language=(getattr(profile, "preferred_language", "") or "en")))
            alerted += 1
    return alerted


def charity_recipient_alerts(entity, *, user):
    """The caller's own charity recipient expiry alerts in ``entity`` (C3), newest first."""
    _require_recipient_view(user, entity)
    return CharityRecipientAlert.objects.filter(entity=entity, addressee=user)


def _require_recipient_view(user, entity):
    """Recipient lists: ``invoicing.view``, or the approver's own right (the Managing Partner reviews what they
    approve, review finding 8)."""
    if not access.has(user, entity, "invoicing.approve_charity_recipient"):
        access.require(user, entity, "invoicing.view")


def charity_recipients(entity, *, user, usable_only=False):
    """The entity's charity recipients (``invoicing.view`` or ``invoicing.approve_charity_recipient``); show them
    with :func:`charity_recipient_row` (masked). ``usable_only``: approved and active ones (the only ones a payout
    accepts: approved, active and not expired, C3)."""
    _require_recipient_view(user, entity)
    qs = CharityRecipient.objects.filter(entity=entity).select_related("bank")
    if usable_only:  # C3: the board approval has not expired
        return qs.filter(is_active=True, approved_by__isnull=False, approval_expires_on__gt=_today())
    return qs


def charity_recipient_row(recipient, *, user):
    """A list row: the IBAN is always masked (banking R4). ``iban_bank_*``: the bank the IBAN itself names
    (positions 5-6), for the approver to check against the recipient's bank (review finding 8)."""
    _require_recipient_view(user, recipient.entity)
    derived = _bank_from_iban(recipient.iban)
    return {"id": recipient.pk, "name_en": recipient.name_en, "name_ar": recipient.name_ar,
            "bank_en": recipient.bank.name_en, "bank_ar": recipient.bank.name_ar, "iban": recipient.masked_iban,
            # review finding 8: what the approver checks: the bank the IBAN itself names (positions 5-6)
            "iban_bank_code": recipient.iban[4:6], "iban_bank_en": getattr(derived, "name_en", ""),
            "iban_bank_ar": getattr(derived, "name_ar", ""), "iban_bank_matches": derived == recipient.bank,
            "iban_bank_verified": bool(getattr(derived, "verified", False)),
            "platform_reference": recipient.platform_reference, "active": recipient.is_active,
            "approved": recipient.is_approved,
            # C3, C5: the board approval and its 12 months; a pending reactivation request
            "board_minute_reference": getattr(recipient.board_approval, "minute_reference", ""),
            "board_approved_on": getattr(recipient.board_approval, "board_approved_on", None),
            "approval_expires_on": recipient.approval_expires_on,
            "expired": recipient.is_approved and recipient.is_expired(_today()),
            "reactivation_requested": recipient.reactivation_requested_by_id is not None}


def _log_iban_view(user, entity, obj, last4):
    access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=entity,
                        permission="banking.view_iban",
                        details={"object": [obj._meta.label_lower, obj.pk], "last4": last4})


def reveal_charity_recipient_iban(recipient, *, user):
    """The unmasked IBAN of a charity recipient (``banking.view_iban``); each view writes an access-log row (R51,
    banking R4)."""
    recipient = CharityRecipient.objects.select_related("entity").get(pk=getattr(recipient, "pk", recipient))
    access.require(user, recipient.entity, "banking.view_iban")
    _log_iban_view(user, recipient.entity, recipient, recipient.iban_last4)
    return {"iban": recipient.iban}


def purification_instructions(entity, *, user):
    """The entity's purification bank instructions (``invoicing.view``); rows via
    :func:`purification_instruction_row` (masked)."""
    access.require(user, entity, "invoicing.view")
    return PurificationInstruction.objects.filter(entity=entity).select_related("from_account", "beneficiary_bank")


def purification_instruction_row(instruction, *, user):
    """A list row of a bank instruction: amount, masked IBANs, reference, status (banking R4)."""
    access.require(user, instruction.entity, "invoicing.view")
    return {"id": instruction.pk, "payout": instruction.payout_id, "amount": instruction.amount,
            "currency": instruction.currency_id, "from_account": instruction.from_account.code,
            "from_iban": iban_rules.masked("SA", instruction.from_iban_last4),
            "beneficiary_en": instruction.beneficiary_name_en,
            "beneficiary_ar": instruction.beneficiary_name_ar, "iban": instruction.masked_iban,
            "reference": instruction.reference, "execution_date": instruction.execution_date,
            "status": instruction.status, "exported_at": instruction.exported_at, "sent_at": instruction.sent_at,
            "bank_reference": instruction.bank_reference}


def _mark_instruction(instruction, user, status, bank_reference=""):
    permission = "invoicing.mark_purification_instruction_sent"
    instruction = PurificationInstruction.objects.select_related("entity").get(
        pk=getattr(instruction, "pk", instruction))
    _require_person(user, instruction.entity, permission)
    access.require(user, instruction.entity, permission)
    bank_reference = (bank_reference or "").strip()
    if len(bank_reference) > 100:
        raise error("The bank reference has at most 100 characters.", "مرجع البنك بحد أقصى 100 حرف.",
                    code="instruction_invalid")
    S = PurificationInstruction.Status
    allowed_from = {S.EXPORTED: (S.PENDING,), S.SENT: (S.PENDING, S.EXPORTED)}[status]
    with transaction.atomic():
        _lock_purification(instruction.entity)  # against a reversal of the payout running at the same time
        instruction = PurificationInstruction.objects.select_for_update().get(pk=instruction.pk)
        payout = PurificationTransfer.objects.select_for_update(of=("self",)).get(pk=instruction.payout_id)
        if payout.status != PurificationTransfer.Status.POSTED or instruction.status not in allowed_from:
            raise error(f"This instruction is {instruction.status} (payout {payout.status}); only a pending "
                        "instruction is marked exported, and a pending or exported one sent, while its payout stands "
                        "(C4).",
                        f"حالة هذا الأمر {instruction.status} (الصرف {payout.status})؛ يُعلَّم الأمر المعلق فقط "
                        "كمُصدَّر، والمعلق أو المُصدَّر كمُرسل، ما دام الصرف قائمًا.", code="instruction_status")
        now = timezone.now()
        instruction.status = status
        if status == S.EXPORTED:
            instruction.exported_by, instruction.exported_at = user, now
            fields = ["status", "exported_by", "exported_at"]
        else:
            instruction.sent_by, instruction.sent_at = user, now
            instruction.bank_reference = bank_reference
            fields = ["status", "sent_by", "sent_at", "bank_reference"]
        instruction._service_write = True
        instruction.save(update_fields=fields)
    return instruction


def mark_purification_instruction_exported(instruction, *, user):
    """C4: an accounting user (``invoicing.mark_purification_instruction_sent``, people only) marks a pending bank
    instruction as exported to a bank file. From then on its payout is not reversed (``instruction_exported``).
    Nothing is sent to a bank from here (an open-banking connection may come later, banking OQ-2)."""
    return _mark_instruction(instruction, user, PurificationInstruction.Status.EXPORTED)


def mark_purification_instruction_sent(instruction, *, user, bank_reference=""):
    """C4: an accounting user (``invoicing.mark_purification_instruction_sent``, people only) marks a pending or
    exported bank instruction as sent to the bank (e.g. paid through the bank's portal), with the bank's reference
    if any. From then on its payout is not reversed."""
    return _mark_instruction(instruction, user, PurificationInstruction.Status.SENT, bank_reference)


def reveal_purification_instruction(instruction, *, user):
    """The unmasked beneficiary IBAN of a bank instruction (``banking.view_iban``), for the bank file; logged."""
    instruction = PurificationInstruction.objects.select_related("entity").get(
        pk=getattr(instruction, "pk", instruction))
    access.require(user, instruction.entity, "banking.view_iban")
    _log_iban_view(user, instruction.entity, instruction, instruction.beneficiary_iban_last4)
    return {"beneficiary_iban": instruction.beneficiary_iban}


# --- personal data (PDPL; R49 to R53, R86, R91) ---------------------------------------------------------------

MASK = "*** / مخفي"
PERSONAL_PARTY_FIELDS = (
    "name_en", "name_ar", "vat_number", "street_en", "street_ar", "building_no", "additional_no", "district_en",
    "district_ar", "city_en", "city_ar", "postal_code",
)
BUYER_FIELDS = (
    "buyer_name_en", "buyer_name_ar", "buyer_vat_number", "buyer_other_id_scheme", "buyer_other_id", "buyer_building_no",
    "buyer_street_en", "buyer_street_ar", "buyer_district_en", "buyer_district_ar", "buyer_additional_no",
    "buyer_postal_code", "buyer_city_en", "buyer_city_ar", "buyer_country",
)


def _is_individual(party):
    kind = party.customer_kind if isinstance(party, Customer) else party.vendor_kind
    return kind == Customer.Kind.INDIVIDUAL


def _may_see_personal(user, entity):
    """R51: holders of ``invoicing.view_personal_data`` in the entity, people only (agents always see masks)."""
    return access_services.kind_of(user) == Kind.HUMAN and access.has(user, entity, "invoicing.view_personal_data")


def _log_personal_data(user, entity, records, how, event=AccessLog.Event.PERSONAL_DATA_VIEWED):
    """R51 / AT-44: one AccessLog row per unmasked view or export of individuals' personal data (user, entity,
    time, the records and how they were shown)."""
    if not records:
        return None
    return access_services.log(
        event, actor=user, entity=entity, permission="invoicing.view_personal_data",
        details={"records": [list(r) for r in records], "how": how},
    )


def _employee_of(party):
    return getattr(party, "employee", None) if isinstance(party, Customer) and party.employee_id else None


def _party_record(party, unmasked):
    fields = PERSONAL_PARTY_FIELDS + (("bank_iban",) if isinstance(party, Vendor) else ())
    record = {"code": party.code, "kind": party.customer_kind if isinstance(party, Customer) else party.vendor_kind,
              "entity": party.entity.code, "is_vat_registered": party.is_vat_registered, "country": party.country,
              "masked": not unmasked}
    for name in fields:
        record[name] = getattr(party, name) if unmasked else MASK
    employee = _employee_of(party)
    if employee is not None:  # review fix 10: an employee's name is read from hr, never stored here
        record["name_en"] = (employee.full_name_en or "") if unmasked else MASK
        record["name_ar"] = (employee.full_name_ar or "") if unmasked else MASK
    if isinstance(party, Customer):
        record["identifiers"] = ([{"scheme": i.scheme, "value": i.value} for i in party.identifiers.all()]
                                 if unmasked else MASK)
    return record


def _view_party(party, user, how):
    payslip_viewer = (_employee_of(party) is not None and access_services.kind_of(user) == Kind.HUMAN
                      and access.has(user, party.entity, "payroll.view_payslip"))
    if not payslip_viewer:
        access.require(user, party.entity, "invoicing.view")
    individual = _is_individual(party)
    personal = _may_see_personal(user, party.entity)
    unmasked = not individual or personal or payslip_viewer
    if individual and unmasked:
        _log_personal_data(user, party.entity, [(party._meta.label_lower, party.pk)],
                           how if personal else f"{how} (payroll.view_payslip)")
    return _party_record(party, unmasked)


def view_customer(customer, *, user):
    """A customer's record (R51): an individual's personal data only for holders of
    ``invoicing.view_personal_data`` in its entity, each such view logged (AT-44); masked for everyone else. An
    employee's own customer record (bai' ajel, R112) shows the employee's name from hr, unmasked only for holders of
    ``invoicing.view_personal_data`` or ``payroll.view_payslip`` (people), each view logged (review fix 10)."""
    return _view_party(Customer.objects.select_related("entity", "employee").get(pk=customer.pk), user, "view")


def view_vendor(vendor, *, user):
    """As :func:`view_customer` for vendors (individual vendors, D24, R91; the IBAN is personal data too)."""
    return _view_party(Vendor.objects.select_related("entity").get(pk=vendor.pk), user, "view")


def _export_parties(model, entity, user):
    access.require(user, entity, "invoicing.view")
    unmasked = _may_see_personal(user, entity)
    rows, shown = [], []
    for party in model.objects.filter(entity=entity).select_related("entity").order_by("code"):
        individual = _is_individual(party)
        rows.append(_party_record(party, unmasked or not individual))
        if individual and unmasked:
            shown.append((party._meta.label_lower, party.pk))
    _log_personal_data(user, entity, shown, "export", AccessLog.Event.PERSONAL_DATA_EXPORTED)
    return rows


def export_customers(entity, *, user):
    """All customers of ``entity`` as rows (R51): individuals unmasked only for holders of
    ``invoicing.view_personal_data``; one ``personal_data_exported`` row lists the individuals exported."""
    return _export_parties(Customer, entity, user)


def export_vendors(entity, *, user):
    return _export_parties(Vendor, entity, user)


def view_sales_document(document, *, user):
    """A sales document's header with its buyer snapshot (R51): an individual buyer's data only for holders of
    ``invoicing.view_personal_data``, logged; masked otherwise. Amounts, VAT and numbers are not personal data."""
    doc = SalesDocument.objects.select_related("entity", "customer").get(pk=document.pk)
    access.require(user, doc.entity, "invoicing.view")
    individual = _is_individual(doc.customer)
    unmasked = not individual or _may_see_personal(user, doc.entity)
    if individual and unmasked:
        _log_personal_data(user, doc.entity, [(doc._meta.label_lower, doc.pk)], "view")
    record = {"number": doc.number, "doc_type": doc.doc_type, "status": doc.status, "customer": doc.customer.code,
              "issue_date": doc.issue_date, "currency": doc.currency_id, "total_net": doc.total_net,
              "total_vat": doc.total_vat, "total_gross": doc.total_gross, "masked": not unmasked}
    for name in BUYER_FIELDS:
        record[name] = getattr(doc, name) if unmasked else MASK
    return record


# --- printable document (R47, R72, R94) -------------------------------------------------------------------------

_SELLER_FIELDS = ("legal_name_en", "legal_name_ar", "vat_number", "cr_number", "unified_number", "building_no",
                  "street_en", "street_ar", "district_en", "district_ar", "additional_no", "postal_code", "city_en",
                  "city_ar", "country")
_BANNERS = {
    "draft": ("Draft: not an invoice; not to be given to the buyer.", "مسودة: ليست فاتورة ولا تُسلَّم للمشتري."),
    "not_cleared": ("Not yet cleared or reported by ZATCA: not to be given to the buyer (R44).",
                    "لم تعتمدها الهيئة أو يُبلَّغ عنها بعد: لا تُسلَّم للمشتري."),
    "rejected": ("Rejected by ZATCA: not valid; a new document replaces it (R20).",
                 "رفضتها الهيئة: غير صالحة؛ يحل محلها مستند جديد."),
    # Worded so that neither title of R47 appears on such a document (R94).
    "no_vat": ("The seller has no VAT number and charges no VAT; the title and type of this document await tax "
               "sign-off (R94, OQ-59).",
               "ليس للبائع رقم ضريبي ولا يفرض الضريبة؛ وعنوان هذا المستند ونوعه بانتظار اعتماد المستشار الضريبي."),
}


def render_sales_document(document, *, user):
    """The printable document as bilingual HTML (Arabic right-to-left and English; R47, R72).

    Title per R47 (``SalesDocument.title_en`` / ``title_ar``; never "Tax Invoice" for a seller without a VAT
    number, R94), the seller's CR number and unified national number with their bilingual labels (R72), the
    buyer, lines, VAT breakdown per category with the SAR VAT, SAR totals and the ZATCA QR from ``zatca_qr``
    (written by the ZATCA hand-off, R45). Issued documents print their frozen snapshot (R15); drafts print the
    current data under a "draft" banner. An individual buyer's data is masked without
    ``invoicing.view_personal_data``; an unmasked print is logged (R51). ``invoicing.view``. No PDF: no HTML-to-PDF
    library is installed (WeasyPrint is not).
    """
    from django.template.loader import render_to_string

    from .qr_svg import qr_svg

    doc = SalesDocument.objects.select_related("entity", "customer", "original_document").get(pk=document.pk)
    access.require(user, doc.entity, "invoicing.view")
    places = _doc_places(doc)
    issued = doc.status in (Status.ISSUED, Status.FINAL, Status.REJECTED)
    lines = list(doc.lines.select_related("vat_rate", "vat_exemption_reason").order_by("line_no"))
    if issued:
        seller = {name: getattr(doc, f"seller_{name}") for name in _SELLER_FIELDS}
        buyer = {name[len("buyer_"):]: getattr(doc, name) for name in BUYER_FIELDS}
        breakdown = doc.vat_breakdown
        totals = {"net": doc.total_net, "vat": doc.total_vat, "gross": doc.total_gross, "vat_sar": doc.total_vat_sar,
                  "gross_sar": doc.total_gross_sar}
    else:
        seller = {name: getattr(doc.entity, name) or "" for name in _SELLER_FIELDS}
        buyer = {name[len("buyer_"):]: value for name, value in _buyer_snapshot(doc).items()}
        breakdown, net, vat, _ = _sales_breakdown(doc, lines)
        totals = {"net": net, "vat": vat, "gross": net + vat, "vat_sar": "—", "gross_sar": "—"}
    totals = {k: (_fmt(v, 2 if k.endswith("_sar") else places) if isinstance(v, Decimal) else v)
              for k, v in totals.items()}
    individual = _is_individual(doc.customer)
    if individual and not _may_see_personal(user, doc.entity):
        buyer = {k: (MASK if v else v) for k, v in buyer.items() if k != "country"} | {"country": buyer.get("country")}
    elif individual:
        _log_personal_data(user, doc.entity, [(doc._meta.label_lower, doc.pk)], "print")
    banners = []
    if not issued:
        banners.append("draft")
    elif doc.status == Status.REJECTED:
        banners.append("rejected")
    elif not zatca_port.issued_by_status(doc):
        banners.append("not_cleared")
    if not (seller["vat_number"] or "").strip():
        banners.append("no_vat")
    context = {
        "doc": doc, "title_en": doc.title_en, "title_ar": doc.title_ar, "seller": seller, "buyer": buyer,
        "original": doc.original_document.number if doc.original_document_id else "",
        "lines": [{"line_no": l.line_no, "description_ar": l.description_ar, "description_en": l.description_en,
                   "quantity": l.quantity.normalize() if l.quantity == l.quantity.to_integral() else l.quantity,
                   "unit_code": l.unit_code, "unit_price": _fmt(l.unit_price, places),
                   "discount": _fmt(l.line_discount, places), "net": _fmt(l.net_amount, places),
                   "category": l.vat_category, "rate": l.vat_rate.rate if l.vat_rate else "",
                   "vat": _fmt(l.vat_amount, places),
                   "reason": l.vat_exemption_reason.code if l.vat_exemption_reason_id else ""} for l in lines],
        "breakdown": breakdown, "totals": totals,
        "banners": [{"en": _BANNERS[b][0], "ar": _BANNERS[b][1]} for b in banners],
        "qr_svg": qr_svg(doc.zatca_qr) if doc.zatca_qr else None,
    }
    return render_to_string("invoicing/sales_document.html", context)


ANONYMISED_EN, ANONYMISED_AR = "ANONYMISED", "مجهّل"
_ANON_DOC_TEXT = ("buyer_name_en", "buyer_street_en", "buyer_district_en", "buyer_city_en")
_ANON_DOC_TEXT_AR = ("buyer_name_ar", "buyer_street_ar", "buyer_district_ar", "buyer_city_ar")
_ANON_DOC_BLANK = ("buyer_vat_number", "buyer_other_id", "buyer_building_no", "buyer_additional_no",
                   "buyer_postal_code")
_ANON_PARTY_TEXT = ("name_en", "street_en", "district_en", "city_en")
_ANON_PARTY_TEXT_AR = ("name_ar", "street_ar", "district_ar", "city_ar")
_ANON_PARTY_BLANK = ("vat_number", "building_no", "additional_no", "postal_code")


def _tax_period_end(entity, d):
    """The last day of the VAT return period (calendar month or quarter) holding ``d`` (zatca R-38)."""
    month = ((d.month - 1) // 3 + 1) * 3 if entity.vat_return_period == Entity.VatReturnPeriod.QUARTERLY else d.month
    return date_type(d.year, month, calendar.monthrange(d.year, month)[1])


def _plus_years(d, years):
    year = d.year + years
    return date_type(year, d.month, min(d.day, calendar.monthrange(year, d.month)[1]))


WHT_RECORD_YEARS = 10  # R52 / OQ-8: WHT payment records kept at least 10 years after payment (WHT p.31)


def _vendor_retention_until(vendor):
    """R52 (review finding 2): the last day an individual vendor's personal data must be kept, or None while it
    cannot be anonymised (no bill or payment yet, or one still a draft or submitted).

    The latest of: each posted or reversed bill or vendor credit note, 6 years after the end of the tax period of
    its accounting date (15 years when a line books to an asset account: capital-asset records, conservative);
    each posted or reversed payment, 6 years after the end of the tax period of its date; and each withholding tax
    deduction, 10 years after its payment date.
    """
    entity = vendor.entity
    dates = []
    docs = list(PurchaseDocument.objects.filter(vendor=vendor).values_list("pk", "status", "accounting_date"))
    payments = list(VendorPayment.objects.filter(vendor=vendor).values_list("status", "date"))
    if not docs and not payments:
        return None
    if any(status in (PStatus.DRAFT, PStatus.SUBMITTED) for _, status, _ in docs) or any(
            status in (VendorPayment.Status.DRAFT, VendorPayment.Status.SUBMITTED) for status, _ in payments):
        return None
    assets = set(PurchaseDocumentLine.objects.filter(document__vendor=vendor, account__type=Account.Type.ASSET)
                 .values_list("document_id", flat=True))
    for pk, _, on in docs:
        dates.append(_plus_years(_tax_period_end(entity, on), 15 if pk in assets else 6))
    for _, on in payments:
        dates.append(_plus_years(_tax_period_end(entity, on), 6))
    for on in WhtDeduction.objects.filter(payment__vendor=vendor).values_list("payment__date", flat=True):
        dates.append(_plus_years(on, WHT_RECORD_YEARS))
    return max(dates)


def anonymisation_candidates(today):
    """R52: what the anonymisation job would change on ``today``.

    - ``documents``: final or rejected sales documents of individual customers whose ``retention_until`` passed;
    - ``customers``: individual customers whose every document is (or is about to be) anonymised;
    - ``vendors``: individual vendors whose :func:`_vendor_retention_until` passed (review finding 2);
    - ``purchase_documents``: the posted or reversed bills of those vendors (their ``vendor_vat_number``).

    Parties without documents have no retention date and are left alone.
    """
    docs = SalesDocument.objects.filter(
        customer__customer_kind=Customer.Kind.INDIVIDUAL, status__in=[Status.FINAL, Status.REJECTED],
        retention_until__lt=today, anonymised_at__isnull=True,
    ).order_by("pk")
    doc_ids = set(docs.values_list("pk", flat=True))
    customers = []
    for c in Customer.objects.filter(customer_kind=Customer.Kind.INDIVIDUAL, anonymised_at__isnull=True,
                                     documents__isnull=False).distinct().order_by("pk"):
        mine = list(c.documents.values_list("pk", "anonymised_at"))
        if all(pk in doc_ids or at is not None for pk, at in mine):
            customers.append(c.pk)
    vendors = []
    for v in Vendor.objects.filter(vendor_kind=Vendor.Kind.INDIVIDUAL, anonymised_at__isnull=True).select_related(
            "entity").order_by("pk"):
        until = _vendor_retention_until(v)
        if until is not None and until < today:
            vendors.append(v.pk)
    bills = list(PurchaseDocument.objects.filter(vendor_id__in=vendors, status__in=[PStatus.POSTED, PStatus.REVERSED],
                                                 anonymised_at__isnull=True).order_by("pk").values_list("pk", flat=True))
    return {"documents": sorted(doc_ids), "customers": customers, "vendors": vendors, "purchase_documents": bills}


def _anonymise_party(party, now):
    for name in _ANON_PARTY_TEXT:
        setattr(party, name, ANONYMISED_EN)
    for name in _ANON_PARTY_TEXT_AR:
        setattr(party, name, ANONYMISED_AR)
    for name in _ANON_PARTY_BLANK:
        setattr(party, name, "")
    party.is_vat_registered, party.is_active, party.anonymised_at = False, False, now


def anonymise_expired_personal_data(*, today=None, apply=False):
    """R52 / AT-43: the anonymisation job (operator command ``invoicing_anonymise``; never a user action).

    Dry run by default: it only reports what it would change. With ``apply=True`` it replaces individuals' names,
    IDs and addresses on those documents and customers, and individual vendors' names, addresses, VAT numbers and
    IBANs (and the VAT number on their bills), by a fixed token; amounts, VAT, numbers, dates and the ZATCA hash and
    QR are kept, and journal entries are never touched. The signed or cleared ZATCA XML keeps the buyer data under
    ZATCA retention (open question in the spec). Applying is refused unless the setting
    ``INVOICING_ANONYMISATION_ENABLED`` is True: the retention rules await confirmation (zatca OQ-8, OQ-31), so
    it is off by default. Returns {"documents", "customers", "vendors", "purchase_documents", "applied", "date"}.
    """
    from django.conf import settings

    today = today or _riyadh_now().date()
    result = {**anonymisation_candidates(today), "applied": False, "date": today}
    if not apply:
        return result
    if not getattr(settings, "INVOICING_ANONYMISATION_ENABLED", False):
        raise error("The anonymisation job is disabled until the retention rules are confirmed (R52); set "
                    "INVOICING_ANONYMISATION_ENABLED to run it.",
                    "مهمة إخفاء الهوية معطلة حتى تأكيد قواعد الحفظ؛ فعّل الإعداد لتشغيلها.", code="anonymisation_disabled")
    now = _riyadh_now()
    if today > now.date():  # never ahead of time: the database checks the moment against retention_until
        raise error("The anonymisation date cannot be in the future.", "لا يكون تاريخ إخفاء الهوية في المستقبل.",
                    code="anonymisation_future")
    with transaction.atomic():
        for doc in SalesDocument.objects.select_for_update(of=("self",)).filter(pk__in=result["documents"]):
            for name in _ANON_DOC_TEXT:
                setattr(doc, name, ANONYMISED_EN)
            for name in _ANON_DOC_TEXT_AR:
                setattr(doc, name, ANONYMISED_AR)
            for name in _ANON_DOC_BLANK:
                setattr(doc, name, "")
            doc.anonymised_at = now
            doc._service_write = True
            doc.save(update_fields=[*_ANON_DOC_TEXT, *_ANON_DOC_TEXT_AR, *_ANON_DOC_BLANK, "anonymised_at",
                                    "updated_at"])
        for c in Customer.objects.select_for_update(of=("self",)).filter(pk__in=result["customers"]):
            _anonymise_party(c, now)
            c.save()
            CustomerIdentifier.objects.filter(customer=c).delete()
        for bill in PurchaseDocument.objects.select_for_update(of=("self",)).filter(
                pk__in=result["purchase_documents"]):
            bill.vendor_vat_number, bill.anonymised_at = "", now
            bill._service_write = True
            bill.save(update_fields=["vendor_vat_number", "anonymised_at", "updated_at"])
        for v in Vendor.objects.select_for_update(of=("self",)).filter(pk__in=result["vendors"]):
            _anonymise_party(v, now)
            v.bank_iban = ""
            v.save()
    result["applied"] = True
    return result


# --- reversal (R24, R25, R32, R33; OQ-28) -------------------------------------------------------------

def _reversal_date(entity, original, requested):
    """R32: the original date if its period is open, else the start of the first open period after it."""
    if requested is not None:
        return requested
    if _open_period(entity, original) is None:
        return original
    period = FiscalPeriod.objects.filter(entity=entity, status=FiscalPeriod.Status.OPEN,
                                         start_date__gt=original).order_by("start_date").first()
    if period is None:
        raise error("No open period after the original date for the reversal; pass a date.",
                    "لا توجد فترة مفتوحة بعد التاريخ الأصلي للعكس؛ حدّد تاريخًا.", code="no_open_period")
    return period.start_date


def reverse_document(document, *, user, date=None, reason=""):
    """Reverse a posted bill, vendor credit note, receipt or vendor payment (``invoicing.reverse_document``).

    Sales documents are never reversed: an issued invoice is corrected by a credit or debit note (R17, OQ-28).
    A document with allocations is reversed only after they are un-allocated (R25, R33); allocations the
    document made itself in the ledger (foreign-currency settlements) are undone with it. One booking per
    document: the corrected document is a new one (R24, access R36).
    """
    if isinstance(document, SalesDocument):
        raise error("Issued sales documents are never reversed or cancelled; issue a credit or debit note.",
                    "لا تُعكس مستندات المبيعات الصادرة ولا تُلغى؛ أصدر إشعارًا دائنًا أو مدينًا.", code="sales_not_reversible")
    model = type(document)
    if model not in (PurchaseDocument, CustomerReceipt, VendorPayment):
        raise error("This record cannot be reversed here.", "لا يمكن عكس هذا السجل هنا.", code="not_reversible")
    document = model.objects.select_related("entity").get(pk=document.pk)
    grant = access.require(user, document.entity, "invoicing.reverse_document", document=document)
    with transaction.atomic():
        document = model.objects.select_for_update(of=("self",)).select_related("entity").get(pk=document.pk)
        if document.status != "posted" or document.journal_entry_id is None:
            raise error("Only posted documents are reversed.", "تُعكس المستندات المرحّلة فقط.", code="not_posted")
        if model is PurchaseDocument and document.doc_type == PurchaseDocument.DocType.BILL:
            _refuse_bai_ajel_bill(document)  # R124: the asset of a bai' ajel sale or contract stays bought
        if model is PurchaseDocument and PurchaseDocument.objects.filter(
                original_document=document, status=PStatus.POSTED).exists():  # nit 17
            raise error("A posted vendor credit note references this bill; reverse the credit note first.",
                        "يشير إشعار دائن مرحّل من المورد إلى هذه الفاتورة؛ اعكس الإشعار أولًا.", code="has_credit_notes")
        own, others = _allocations_of(document)
        if any(not a.via_settlement for a in own) or others:
            raise error("The document has allocations; un-allocate them first (R25, R33).",
                        "للمستند تخصيصات؛ ألغِها أولًا.", code="has_allocations")
        on = _reversal_date(document.entity, document.journal_entry.date, date)
        token = _reversing_document.set((model._meta.label_lower, document.pk))
        try:
            reversal = ledger.reverse_entry(document.journal_entry, grant, date=on, reason=reason)
        except LedgerError as exc:
            raise _problems_error(exc.messages, getattr(exc, "code", None) or "reversal_refused") from None
        finally:
            _reversing_document.reset(token)
        for row in own:  # foreign-currency settlements made by this document's own entry
            undo = type(row)(entity=row.entity, target=row.target, amount=-row.amount, date=on, created_by=user,
                             reverses=row, via_settlement=True,
                             **({"receipt": row.receipt, "credit_note": row.credit_note}
                                if isinstance(row, ReceivableAllocation)
                                else {"payment": row.payment, "vendor_credit_note": row.vendor_credit_note}))
            undo._service_write = True
            undo.save()
        document.status = "reversed"
        document.reversal_entry = reversal
        document._service_write = True
        document.save(update_fields=["status", "reversal_entry", "updated_at"])
    return document


# (label, pk) while reverse_document reverses that document (registered reverse_guard of PurchaseDocument): a bill
# is never reversed behind invoicing's back, so its checks (allocations, credit notes, bai' ajel assets) always run.
_reversing_document = ContextVar("invoicing_reversing_document", default=None)


def _purchase_reverse_guard(document):
    return _reversing_document.get() == (document._meta.label_lower, document.pk)


def _allocations_of(document):
    """(net allocations the document made as a source, net allocations made to it as a target)."""
    def live(qs):
        rows = list(qs.filter(amount__gt=0))
        undone = set(qs.filter(reverses__isnull=False).values_list("reverses_id", flat=True))
        return [r for r in rows if r.pk not in undone]

    if isinstance(document, CustomerReceipt):
        return live(ReceivableAllocation.objects.filter(receipt=document)), []
    if isinstance(document, VendorPayment):
        return live(PayableAllocation.objects.filter(payment=document)), []
    own = live(PayableAllocation.objects.filter(vendor_credit_note=document))
    return own, live(PayableAllocation.objects.filter(target=document))


# --- sub-ledger tie-out (R39) ---------------------------------------------------------------------

def reconcile_subledger(entity, as_of, *, user):
    """R39 / AT-31: open sub-ledger balances equal the ledger on 1200, 1210, 2100 and 2110, per counterparty and
    currency (transaction amounts, debit positive). Returns rows with ``difference``; all zero means tied out."""
    access.require(user, entity, "invoicing.view")
    access.require(user, entity, "ledger.view")
    sub = {}

    def add(account, counterparty, currency, amount):
        key = (account, counterparty, currency)
        sub[key] = sub.get(key, ZERO) + amount

    def ctrl(party, receivable):
        account, cp = _control(party, receivable)
        return account, cp.code if cp else None

    def booked(qs, reversible=True):
        """Booked as of ``as_of`` (review item 7): its entry is dated on or before it and it was not reversed by
        then (no reversal, or a reversal dated after ``as_of``)."""
        qs = qs.filter(entity=entity, journal_entry__isnull=False, journal_entry__date__lte=as_of)
        if reversible:
            qs = qs.filter(Q(reversal_entry__isnull=True) | Q(reversal_entry__date__gt=as_of))
        return qs

    # R112: bai' ajel invoices (and their R128 adjustment credit notes) are receivables on 1240, collected by
    # payroll, not on 1200 / 1210.
    for d in booked(SalesDocument.objects.filter(deferred_sale__isnull=True, deferred_sale_adjustment__isnull=True),
                    reversible=False).select_related(
            "customer__group_entity"):
        sign = -1 if d.doc_type == SalesDocument.DocType.CREDIT_NOTE else 1
        add(*ctrl(d.customer, True), d.currency_id, sign * d.total_gross)
    for r in booked(CustomerReceipt.objects.all()).select_related("customer__group_entity"):
        add(*ctrl(r.customer, True), r.currency_id, r.amount if r.kind == CustomerReceipt.Kind.REFUND else -r.amount)
    for d in booked(PurchaseDocument.objects.all()).select_related("vendor__group_entity"):
        sign = 1 if d.doc_type == PurchaseDocument.DocType.VENDOR_CREDIT_NOTE else -1
        add(*ctrl(d.vendor, False), d.currency_id, sign * d.total_gross)
    for p in booked(VendorPayment.objects.all()).select_related("vendor__group_entity"):
        add(*ctrl(p.vendor, False), p.currency_id,
            -p.amount if p.kind == VendorPayment.Kind.REFUND_RECEIVED else p.settled_amount)
    gl = {}
    lines = JournalLine.objects.filter(
        entry__entity=entity, entry__status__in=ledger.BOOKED, entry__date__lte=as_of,
        account__code__in=[AR, AR_GROUP, AP, AP_GROUP],
    ).values("account__code", "counterparty_entity__code", "currency_id", "debit", "txn_amount")
    for line in lines:
        key = (line["account__code"], line["counterparty_entity__code"], line["currency_id"])
        gl[key] = gl.get(key, ZERO) + (line["txn_amount"] if line["debit"] > 0 else -line["txn_amount"])
    rows = []
    for key in sorted(set(sub) | set(gl), key=lambda k: tuple(str(x) for x in k)):
        s, g = sub.get(key, ZERO), gl.get(key, ZERO)
        rows.append({"account": key[0], "counterparty": key[1], "currency": key[2], "subledger": s, "ledger": g,
                     "difference": s - g})
    return rows


# --- billing hooks (R60, R73; billing.md R13, R43, R46c, R52 to R56) -----------------------------------------

BILLING_SOURCE = "system:billing"


def _billing_refusal(en, ar, code):
    return InvoicingError(f"{en} / {ar}", code=code)


def _billing_line(doc, row, *, unit_price):
    """One line of a billing document (quantity 1, the price billing froze); refused like any other line."""
    last = doc.lines.order_by("-line_no").values_list("line_no", flat=True).first() or 0
    line = SalesDocumentLine(entity=doc.entity, document=doc, line_no=last + 1)
    _sales_line_values(
        doc, line, income_type=row["income_type"], quantity="1", unit_price=unit_price,
        description_ar=row.get("description_ar", ""), description_en=row.get("description_en", ""),
        vat_category=row.get("vat_category") or None, vat_exemption_reason=row.get("vat_exemption_reason") or None,
        income_account=row.get("income_account") or None, line_discount="0", unit_code=row.get("unit_code") or "C62",
    )
    with billing_line_writes():  # R73 / billing R56: the only writer of billing lines besides the re-pricing
        line.save()
    return line


def _locked_billing_document(field, obj):
    """The non-rejected document created for ``obj`` (a period or an adjustment), locked, or None."""
    return (SalesDocument.objects.select_for_update(of=("self",))
            .filter(**{field: obj}).exclude(status=Status.REJECTED).order_by("pk").first())


def _save_billing_document(doc, field, obj):
    """Insert the draft; if a concurrent call created this period's document first, use that one (R60: a DB
    partial unique constraint, so a race cannot create two)."""
    from django.db import IntegrityError

    _refresh_tax_point(doc)  # R100: billing drafts show their computed tax point too
    try:
        with transaction.atomic():
            doc.save()
    except IntegrityError:
        existing = _locked_billing_document(field, obj)
        if existing is None:
            raise
        return existing, False
    return doc, True


def create_from_billing(period, user):
    """R60 / billing R52, R53: the invoice of one ``BillingPeriod`` from its frozen ``invoice_lines``.

    Only the billing system user (``invoicing.create_from_billing``, Sys:billing) calls it; anyone else gets
    ``AccessDenied``. The document is created and submitted by that user with ``source="system:billing"`` and
    ``billing_item`` = the period. With ``InvoicingSettings.auto_issue_from_approved_contract`` and an approved
    ``billed_version`` it is issued in the same call without per-invoice approval (D20): ``approved_by`` records
    the human who approved the billed version (the contract approver for version 1). Nobody approves it in that
    person's name: no ``invoicing.approve_sales_document`` grant is issued or used; posting on clearance accepts
    the contract approval itself (ledger ``_clearance_approver`` through the registered ``contract_approval``
    hook). Otherwise it stays ``submitted`` for the CFO.

    Idempotent: a second call returns the period's non-rejected document. All-or-nothing: a refusal raises with a
    code billing maps (billing R54) and leaves nothing behind: ``entity_not_vat_registered``, ``period_closed``,
    ``third_decimal_pending``, ``rate_required``, ``contract_not_billable``, ``purification_flag_mismatch`` (the
    contract's ``requires_purification`` disagrees with the lines' income accounts) or ``document_invalid``.
    The returned document carries ``warnings`` (en/ar, e.g. the late-invoice warning R84).
    """
    entity = period.entity
    access.require(user, entity, "invoicing.create_from_billing")
    with transaction.atomic():
        existing = _locked_billing_document("billing_item", period)
        if existing is not None:
            existing.warnings = []
            return existing
        contract, version = period.contract, period.billed_version
        if contract.entity_id != entity.pk or not period.invoice_lines or version is None:
            raise _billing_refusal("The billing period has no frozen invoice lines or billed version (billing R52).",
                                   "ليس لفترة الفوترة بنود فاتورة مجمدة أو نسخة مفوترة.", "contract_not_billable")
        if not (entity.vat_number or "").strip():  # R94 / OQ-59: held by billing
            raise _not_vat_registered(entity)
        today = _riyadh_now().date()
        closed = _open_period(entity, today)
        if closed:  # R28: the invoice is dated today; billing retries next run
            raise InvoicingError([closed], code="period_closed")
        currency = _currency(contract.currency_id, entity)
        due = today + timedelta(days=version.due_days) if version.due_days is not None else None
        doc = SalesDocument(
            entity=entity, doc_type=SalesDocument.DocType.INVOICE, customer=contract.customer, issue_date=today,
            currency=currency, supply_date=period.period_start, supply_end_date=period.period_end, due_date=due,
            created_by=user, source=BILLING_SOURCE, billing_item=period,
        )
        if _natural_person_not_subject_to_vat(contract.customer):  # OQ-19 (R48)
            doc.zatca_profile = SalesDocument.Profile.SIMPLIFIED
        doc, created = _save_billing_document(doc, "billing_item", period)
        if not created:
            doc.warnings = []
            return doc
        for row in sorted(period.invoice_lines, key=lambda r: r.get("line_no", 0)):
            _billing_line(doc, row, unit_price=row["net"])
        doc = SalesDocument.objects.select_for_update(of=("self",)).select_related("entity", "customer").get(pk=doc.pk)
        lines = list(doc.lines.select_related("vat_rate", "vat_exemption_reason", "income_account"))
        flagged = any(line.income_account.requires_purification
                      or (line.income_account.sharia_prohibited and not entity.sharia_enforced) for line in lines)
        if flagged != bool(contract.requires_purification):  # rule 5: billing and the invoice agree on the flag
            raise _billing_refusal(
                f"The contract says purification is {'' if contract.requires_purification else 'not '}required, but "
                f"the invoice lines' income accounts say it is{'' if flagged else ' not'}; correct the contract or the "
                "accounts in billing before invoicing (rule 5, R6).",
                "لا يتطابق مؤشر التطهير في العقد مع حسابات الإيراد في بنود الفاتورة؛ صحّح العقد أو الحسابات في وحدة "
                "الفوترة قبل الفوترة.", "purification_flag_mismatch")
        breakdown, net, vat, _ = _sales_breakdown(doc, lines)
        odd = _third_decimal_problems(doc, lines, breakdown, net, vat)
        if odd:  # OQ-62: billing holds the period until a person releases it
            raise InvoicingError(odd, code="third_decimal_pending")
        problems = _issue_problems(doc, check_period=False)
        if problems:
            raise _problems_error(problems, "document_invalid")
        doc.status = Status.SUBMITTED
        doc.submitted_by, doc.submitted_at = user, timezone.now()
        _bump(doc)
        doc._service_write = True
        doc.save()
        approved = version.status == "approved" and version.approved_by_id is not None
        warnings = []
        held_b2g = _payment_order_problems(doc)  # R117 (G1): stays submitted until a person enters the date
        if held_b2g:
            warnings = held_b2g
        elif approved and _settings(entity).auto_issue_from_approved_contract:
            _issue_submitted(doc, rate_user=user, approved_by=version.approved_by, approved_at=version.approved_at)
            access_services.log(
                AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=entity, permission="invoicing.create_from_billing",
                details={"document": [doc._meta.label_lower, doc.pk], "action": "issue_from_contract",
                         "billed_version": ["billing.contractversion", version.pk],
                         "approved_by": version.approved_by_id},
            )
            # The ZATCA queue system user hands it over (zatca R-32): the billing user holds no ZATCA right. A
            # hand-off failure leaves it not_submitted for the automatic resubmission (R79), never breaks the run.
            port, queue = zatca_port.get_port(), access.system_user("zatca_queue")
            transaction.on_commit(lambda: port.submit_document(SalesDocument.objects.get(pk=doc.pk), queue),
                                  robust=True)
            warnings = _late_invoice_warnings(doc)
    doc.warnings = warnings
    return doc


def _contract_approval(document):
    """Registered ``contract_approval`` (billing R53): the recorded approval of an invoice auto-issued from billing.

    For an invoice with ``source="system:billing"`` and a billing period, the approval that counts is the CFO's
    approval of the billed contract version: ``billing.approve_contract`` for the contract (version 1) or
    ``billing.approve_version`` for a later version. The ledger checks that the person recorded as ``posted_by``
    is that approver and that the access log holds their granted approval of that very record.
    """
    from ledger.documents import RecordedApproval

    deferred = _deferred_sale_approval(document)
    if deferred is not None or _is_deferred_sale(document):
        return deferred
    if (document.source != BILLING_SOURCE or document.billing_item_id is None
            or document.doc_type != SalesDocument.DocType.INVOICE or document.approved_by_id is None):
        return None
    version = document.billing_item.billed_version
    if version is None or version.status != "approved" or version.approved_by_id != document.approved_by_id:
        return None
    if version.version_no == 1:
        return RecordedApproval("billing.approve_contract", version.contract, version.approved_by_id,
                                version.approved_at)
    return RecordedApproval("billing.approve_version", version, version.approved_by_id, version.approved_at)


def create_adjustment_from_billing(adjustment, user):
    """R73 / billing R43: the **draft** credit or debit note of one ``BillingAdjustment``.

    Only the billing system user (``invoicing.create_adjustment_from_billing``). Draft only, whatever
    ``auto_issue_from_approved_contract`` says (R44): a person completes the header (``payment_means_code``)
    and submits; the CFO approves (R57). ``source="system:billing"``, ``billing_adjustment`` set, the adjustment's
    invoice as original, its supply period, one line per changed line key (quantity 1, price = the delta) and the
    version's reason; ``adjustment_event_date`` stays empty (D30a, R95). The lines are read-only for every user
    (billing R56). Idempotent: a second call returns the adjustment's existing note.
    """
    entity = adjustment.entity
    access.require(user, entity, "invoicing.create_adjustment_from_billing")
    with transaction.atomic():
        existing = _locked_billing_document("billing_adjustment", adjustment)
        if existing is not None:
            return existing
        original = SalesDocument.objects.select_related("customer").get(pk=adjustment.original_document_id)
        _same_entity(entity, original, "original invoice", "الفاتورة الأصلية")
        if original.status != Status.FINAL or original.doc_type != SalesDocument.DocType.INVOICE:
            raise _billing_refusal("The original invoice is not final yet; the note waits for it (billing R47).",
                                   "الفاتورة الأصلية ليست نهائية بعد؛ ينتظر الإشعار اعتمادها.", "original_not_final")
        doc_type = (SalesDocument.DocType.CREDIT_NOTE if adjustment.kind == "credit"
                    else SalesDocument.DocType.DEBIT_NOTE)
        rows = [row for row in adjustment.lines if Decimal(str(row["delta"])) != ZERO]
        if not rows:
            raise _billing_refusal("The correction has no changed amount.", "لا يتضمن التصحيح مبلغًا متغيرًا.",
                                   "nothing_to_correct")
        version = adjustment.version
        doc = SalesDocument(
            entity=entity, doc_type=doc_type, customer=original.customer, issue_date=_riyadh_now().date(),
            currency=original.currency, original_document=original, zatca_profile=original.zatca_profile,
            supply_date=adjustment.supply_start, supply_end_date=adjustment.supply_end,
            reason_ar=(version.reason_ar or "")[:300], reason_en=(version.reason_en or "")[:300],
            created_by=user, source=BILLING_SOURCE, billing_adjustment=adjustment,
        )
        doc, created = _save_billing_document(doc, "billing_adjustment", adjustment)
        if not created:
            return doc
        for row in rows:
            _billing_line(doc, row, unit_price=str(abs(Decimal(str(row["delta"])))))
    return SalesDocument.objects.get(pk=doc.pk)


def discard_adjustment_draft(document, user):
    """Billing R46c, R48: delete a superseded or discarded billing correction draft.

    Only the billing system user (``invoicing.create_adjustment_from_billing``) and only a document created by
    ``create_adjustment_from_billing``. A ``submitted`` note is first returned to draft in the same transaction
    (R96: only submitted -> draft exists); ``issued``, ``final`` and ``rejected`` notes are refused and never
    touched.
    """
    doc = SalesDocument.objects.select_related("entity").get(pk=document.pk)
    access.require(user, doc.entity, "invoicing.create_adjustment_from_billing")
    with transaction.atomic():
        doc = SalesDocument.objects.select_for_update(of=("self",)).get(pk=doc.pk)
        if doc.billing_adjustment_id is None or doc.source != BILLING_SOURCE:
            raise error("Only correction drafts created from billing are discarded here.",
                        "تُستبعد هنا مسودات التصحيح المنشأة من الفوترة فقط.", code="not_billing_draft")
        if doc.status not in (Status.DRAFT, Status.SUBMITTED):
            raise error("An issued, final or rejected note is never discarded or changed (R13, R96).",
                        "لا يُستبعد الإشعار الصادر أو النهائي أو المرفوض ولا يُعدَّل.", code="not_draft")
        if doc.status == Status.SUBMITTED:
            doc.status = Status.DRAFT
            doc.modified_at = timezone.now()
            _bump(doc)
            doc._service_write = True
            doc.save()
        with billing_line_writes():  # billing R46c: the one delete of billing lines (the guards refuse all others)
            SalesDocumentLine.objects.filter(document=doc).delete()
        doc.delete()


# --- bai' ajel sale to an employee (R112, E28; payroll R47, R48; owner decisions B8, E8) -----------------------------

DEFERRED_SALE_BY_HAND = (
    "A bai' ajel sale line is created only by the sale approved in payroll, never by hand (R112).",
    "بند البيع الآجل لا يُنشأ إلا من خلال البيع المعتمد في الرواتب، ولا يُدخل يدويًا.",
)
DEFERRED_SALE_NOTE = (
    "A bai' ajel sale invoice takes no credit or debit note: its price is fixed at the sale and never increases "
    "(Sharia, payroll R48); an early-settlement rebate (ibra') awaits its Sharia decision (payroll OQ-33).",
    "لا يصدر إشعار دائن أو مدين على فاتورة البيع الآجل: ثمنها ثابت عند البيع ولا يزيد أبدًا (الضوابط الشرعية)؛ "
    "والإبراء عند السداد المبكر بانتظار القرار الشرعي.",
)
DEFERRED_SALE_COLLECTED_BY_PAYROLL = (
    "A bai' ajel sale invoice is collected by the payroll instalments on 1240, never by receipts or allocations here "
    "(R112).",
    "تُحصَّل فاتورة البيع الآجل من خلال أقساط الرواتب على الحساب 1240، لا بسندات القبض أو التخصيص هنا.",
)
# Review fix 4: the person whose payroll approval collected an instalment is named by payroll's collecting marker
# (payroll.invoicing_port.collecting): payroll.approve_run for a run, payroll.approve_final_settlement for a final
# settlement, and payroll.reverse_run for a run reversal giving it back (owner decision A6).
DEFERRED_PROFIT_REVERSAL = "payroll.reverse_run"
# (release pk) while reverse_deferred_profit reverses that release (registered reverse_guard).
_reversing_profit = ContextVar("invoicing_reversing_profit", default=None)


def _deferred_sale_by_hand():
    return error(*DEFERRED_SALE_BY_HAND, code="deferred_sale_by_hand")


# Compliance A3 (RETT): real estate is never sold by bai' ajel here. A sale of real estate is exempt from VAT and
# subject to the Real Estate Transaction Tax (5%, RETT Implementing Regulations), so it cannot be the simplified 15%
# VAT invoice of R118; it would need its own RETT path (open question OQ-77).
REAL_ESTATE = "real_estate"
REAL_ESTATE_REFUSED = (
    "Real estate is not sold by bai' ajel in the ERP: a real-estate sale is exempt from VAT and subject to the Real "
    "Estate Transaction Tax (RETT 5%), not the 15% VAT invoice of the bai' ajel sale (R125, OQ-77).",
    "لا تُباع العقارات بالبيع الآجل في النظام: بيع العقار معفى من ضريبة القيمة المضافة وتُفرض عليه ضريبة التصرفات "
    "العقارية (5%)، لا فاتورة ضريبة القيمة المضافة 15% الخاصة بالبيع الآجل.",
)


def _bai_ajel_bill_use(bill):
    """R124: what still relies on ``bill`` as the purchase of a bai' ajel asset: ``"sale"`` (an active
    ``DeferredSale``, whatever its invoice's state), ``"contract"`` (a payroll contract that recorded it as its
    acquisition and is not discarded), or None. Read only; invoicing never writes payroll data."""
    from django.apps import apps

    pk = getattr(bill, "pk", bill)
    if DeferredSale.objects.filter(purchase_bill_id=pk, active=True).exists():
        return "sale"
    try:
        model = apps.get_model("payroll", "DeferredSaleContract")
    except LookupError:
        return None
    if model._default_manager.filter(purchase_bill_id=pk).exclude(status="discarded").exists():
        return "contract"
    return None


def _bai_ajel_bill_message(bill, *, credit_note=False):
    what_en = "credited on 1250 by a vendor credit note" if credit_note else "reversed"
    what_ar = "تخفيضها على الحساب 1250 بإشعار دائن من المورد" if credit_note else "عكسها"
    return (f"Bill {bill.number} is the purchase of an asset sold (or being sold) to an employee by bai' ajel: it cannot "
            f"be {what_en} (R124; payroll R47). The asset stays bought; a return or rebate goes through payroll's "
            f"bai' ajel services. / الفاتورة {bill.number} هي شراء أصل مبيع (أو قيد البيع) لموظف بالبيع الآجل: لا يجوز "
            f"{what_ar}. يبقى الأصل مشترى؛ والإرجاع أو الإبراء من خلال خدمات البيع الآجل في الرواتب.")


def _refuse_bai_ajel_bill(bill):
    if _bai_ajel_bill_use(bill):
        en, ar = _bai_ajel_bill_message(bill).split(" / ", 1)
        raise error(en, ar, code="bai_ajel_asset_bill")


def _deferred_sale_note_refused():
    return error(*DEFERRED_SALE_NOTE, code="deferred_sale_note")


def _is_deferred_sale(document):
    """True for the sales document of a bai' ajel sale (R112) or of its payroll-approved adjustment credit note
    (R128), whatever the caller's copy says."""
    if not isinstance(document, SalesDocument) or document.pk is None:
        return False
    return (DeferredSale.objects.filter(sales_document_id=document.pk).exists()
            or DeferredSaleAdjustment.objects.filter(credit_note_id=document.pk).exists())


def _sales_control(document):
    """The receivable a sales document books (E1 to E4): 1200 or 1210, or 1240 for a bai' ajel sale (E28)."""
    if _is_deferred_sale(document):
        return EMPLOYEE_RECEIVABLE, None
    return _control(document.customer, receivable=True)


def _sales_line_account_problems(entity, line):
    """R1, R2, R5 for a sales line: an income account, or for a bai' ajel line 1250 (cost) or 1241 (deferred
    profit) of the entity (R112, E28)."""
    account = line.income_account
    if line.income_type.code != DEFERRED_SALE_INCOME:
        return _account_problems(entity, account, {Account.Type.INCOME}, "income")
    if account.entity_id != entity.pk:
        return [f"Account {account.code} belongs to another entity. / الحساب {account.code} يخص كيانًا آخر."]
    problems = []
    if account.code not in DEFERRED_SALE_LINE_ACCOUNTS or account.type != Account.Type.ASSET:
        problems.append(f"A bai' ajel sale line credits 1250 (cost) or 1241 (deferred profit), not {account.code} "
                        f"(R112, E28). / يُقيَّد بند البيع الآجل دائنًا على 1250 (التكلفة) أو 1241 (الربح المؤجل) لا "
                        f"على {account.code}.")
    if not account.is_postable or not account.is_active:
        problems.append(f"Account {account.code} is not open for posting. / الحساب {account.code} غير مفتوح للترحيل.")
    if account.sharia_prohibited and entity.sharia_enforced:
        problems.append(f"Account {account.code} is Sharia-prohibited. / الحساب {account.code} محظور شرعًا.")
    return problems


def _deferred_sale_approval(document):
    """Registered ``contract_approval`` for a bai' ajel sale invoice (R112): the matrix approver's approval of the
    sale in payroll (``payroll.approve_deferred_sale`` for the contract), recorded on the sale. None otherwise."""
    from django.apps import apps

    from ledger.documents import RecordedApproval

    adjustment = DeferredSaleAdjustment.objects.select_related("deferred_sale").filter(
        credit_note_id=document.pk).first()
    if adjustment is not None:  # R128: the credit note is issued and posted on the rebate's approval in payroll
        sale, approved_by_id, approved_at = adjustment.deferred_sale, adjustment.approved_by_id, adjustment.approved_at
        if (document.source != DEFERRED_SALE_SOURCE or document.approved_by_id != approved_by_id
                or document.doc_type != SalesDocument.DocType.CREDIT_NOTE
                or document.original_document_id != sale.sales_document_id):
            return None
    else:
        sale = DeferredSale.objects.filter(sales_document_id=document.pk).first()
        if (sale is None or document.source != DEFERRED_SALE_SOURCE or document.approved_by_id != sale.approved_by_id
                or document.doc_type != SalesDocument.DocType.INVOICE):
            return None
        approved_by_id, approved_at = sale.approved_by_id, sale.approved_at
    try:
        model = apps.get_model(sale.source_type)
    except (LookupError, ValueError):
        return None
    target = model._default_manager.filter(pk=sale.source_id).first()
    if target is None:
        return None
    return RecordedApproval("payroll.approve_deferred_sale", target, approved_by_id, approved_at)


def _employee_customer(entity, employee, user):
    """The employee's own ``individual`` customer record in ``entity`` (R112), created on the first sale. Not VAT
    registered: the invoice is simplified and reported to ZATCA (R48). The name is personal data, masked like every
    individual's (R51)."""
    customer = Customer.objects.select_for_update(of=("self",)).filter(entity=entity, employee=employee).first()
    if customer is not None:
        if not customer.is_active or customer.anonymised_at is not None:
            raise error(f"The employee's customer record {customer.code} is inactive or anonymised.",
                        f"سجل العميل للموظف {customer.code} غير نشط أو مخفي الهوية.", code="customer_inactive")
        return customer
    code = f"EMP-{employee.employee_code}"[:20]
    if Customer.objects.filter(entity=entity, code=code).exists():
        raise error(f"Customer code {code} is taken by another customer; the employee's record cannot be created.",
                    f"رمز العميل {code} مستخدم لعميل آخر؛ لا يمكن إنشاء سجل الموظف.", code="customer_code_taken")
    # Review fix 10 (PDPL; OQ-74 for the DPO): the employee's name is not copied from hr; the record and the invoice
    # carry the employee code only. The name is shown, unmasked and logged, only by view_customer to holders of
    # invoicing.view_personal_data or payroll.view_payslip.
    customer = Customer(
        entity=entity, code=code, name_ar=code, name_en=code, customer_kind=Customer.Kind.INDIVIDUAL,
        is_vat_registered=False, currency_id=SAR, country="SA", created_by=user, source=DEFERRED_SALE_SOURCE,
        employee=employee,
    )
    problems = party_problems(customer)
    if problems:
        raise _problems_error(problems, "customer_invalid")
    customer.save()
    return customer


def _bill_asset_cost(bill):
    """The asset cost the posted purchase bill put on 1250 (E9), in SAR."""
    total = JournalLine.objects.filter(entry_id=bill.journal_entry_id, account__code=SALE_ASSET).aggregate(
        t=Sum("debit"))["t"] or ZERO
    return Decimal(total).quantize(CENT)


def _check_deferred_sale(entity, *, employee, bill, cost, possession_date, possession_evidence_ref, today, sale_date,
                         source_type, source_id, asset_type):
    """R112 / payroll R47 (owner decision E8): the company bought the asset in its own name and took possession
    before selling it, and sells it once; never real estate (R125) and never dated after today (R126). Raises with a
    code per refusal."""
    if not (asset_type or "").strip():
        raise error("The asset type is recorded with the acquisition (payroll R47 step 3).",
                    "يُسجَّل نوع الأصل مع الشراء.", code="asset_type_required")
    if asset_type == REAL_ESTATE:
        raise error(*REAL_ESTATE_REFUSED, code="real_estate_not_allowed")
    if sale_date is not None and sale_date > today:
        raise error(f"The sale date {sale_date} is after today {today} (Riyadh): a sale is never dated in the future "
                    "(R126).", f"تاريخ البيع {sale_date} بعد اليوم {today} (توقيت الرياض): لا يؤرَّخ البيع بتاريخ "
                    "مستقبلي.", code="sale_date_future")
    if entity.base_currency_id != SAR:
        raise error(f"A bai' ajel sale is invoiced in SAR by a Saudi entity; {entity.code} keeps its books in "
                    f"{entity.base_currency_id}.", f"يُفوتر البيع الآجل بالريال من كيان سعودي؛ دفاتر {entity.code} "
                    f"بعملة {entity.base_currency_id}.", code="currency")
    if getattr(employee, "entity_id", None) != entity.pk:
        raise error("The employee belongs to another entity.", "الموظف يتبع كيانًا آخر.", code="entity_mismatch")
    if (not isinstance(bill, PurchaseDocument) or bill.entity_id != entity.pk
            or bill.doc_type != PurchaseDocument.DocType.BILL or bill.status != PStatus.POSTED
            or bill.journal_entry_id is None or bill.reversal_entry_id is not None):
        raise error("The asset must be bought first: its purchase bill is a posted bill of this entity (R112, E9).",
                    "يجب شراء الأصل أولًا: فاتورة شرائه فاتورة مرحّلة لهذا الكيان.", code="purchase_bill_not_posted")
    sold = (DeferredSale.objects.filter(purchase_bill=bill, active=True)
            .exclude(source_type=source_type, source_id=source_id).exclude(sales_document__status=Status.REJECTED))
    if sold.exists():
        raise error("This asset is already sold: its purchase bill backs another bai' ajel sale (R112).",
                    "هذا الأصل مبيع مسبقًا: فاتورة شرائه مستخدمة لبيع آجل آخر.", code="asset_already_sold")
    on_asset = _bill_asset_cost(bill)
    if on_asset != cost:
        raise error(f"The purchase bill put {on_asset} on 1250; the sale's cost is {cost}. The cost is the posted cost "
                    "of the asset (R112).", f"قيدت فاتورة الشراء {on_asset} على الحساب 1250 وتكلفة البيع {cost}؛ التكلفة "
                    "هي التكلفة المرحّلة للأصل.", code="cost_mismatch")
    if possession_date is None or not (possession_evidence_ref or "").strip():
        raise error("The company's possession of the asset (date and evidence) must be recorded before the sale "
                    "(R112, owner decision E8).", "يجب تسجيل قبض الشركة للأصل (التاريخ والمستند) قبل البيع.",
                    code="possession_not_recorded")
    if possession_date < bill.accounting_date:
        raise error("Possession cannot come before the purchase.", "لا يسبق القبض الشراء.",
                    code="possession_not_recorded")
    if today < possession_date or (sale_date is not None and sale_date < possession_date):
        raise error(f"The sale is on or after the possession date {possession_date} (R112).",
                    f"يكون البيع في تاريخ القبض {possession_date} أو بعده.", code="sale_before_possession")


def _deferred_line(doc, account, amount, description_ar, description_en, vat_rate=None):
    last = doc.lines.order_by("-line_no").values_list("line_no", flat=True).first() or 0
    line = SalesDocumentLine(entity=doc.entity, document=doc, line_no=last + 1)
    _sales_line_values(
        doc, line, income_type=DEFERRED_SALE_INCOME, quantity="1", unit_price=amount, description_ar=description_ar[:300],
        description_en=description_en[:300], vat_category=VatCategory.STANDARD, vat_exemption_reason=None,
        income_account=account, line_discount="0", unit_code="C62",
    )
    if vat_rate is not None:  # a note at its original invoice line's rate (R102)
        _price_line(line, doc.supply_date or doc.issue_date, _doc_places(doc), rate=vat_rate)
    line.save()
    return line


def _deactivate(sale):
    sale.active = False
    sale._service_write = True
    sale.save(update_fields=["active"])


def create_deferred_sale(source, *, approver, employee, purchase_bill, cost, profit_amount, possession_date,
                         possession_evidence_ref, asset_description_ar, asset_description_en="", sale_date=None,
                         term_months=None, asset_type=""):
    """R112 / E28 (payroll R47 step 4, P19(k), P20(h); owner decisions B8, E8): the bai' ajel sale invoice.

    Called by payroll's sale approval through ``invoicing.payroll_port`` (never by hand), inside its transaction.
    ``source`` is the approved contract (a stored record of the entity) and ``approver`` the person approving the
    sale there; their ``payroll.approve_deferred_sale`` right for that contract is checked again here and recorded
    as used for this invoice: it is the approval the invoice is issued and posted on (ledger ``contract_approval``).

    Refused (en/ar, by code) unless the company bought the asset in its own name (a posted purchase bill of the
    entity, ``purchase_bill_not_posted``) at exactly ``cost`` on 1250 (``cost_mismatch``), recorded its possession
    with evidence (``possession_not_recorded``) on or before the sale (``sale_before_possession``), and the asset was
    not sold already (``asset_already_sold``).

    The deferred-sale system user creates, submits and issues a **simplified** invoice to the employee's own
    individual customer record: one line at the cost (Cr 1250) and one at the fixed profit (Cr 1241), category S,
    VAT 15% on the sale price charged to the employee; the document is handed to ZATCA after commit and posts on
    reporting like any sales document (R41): Dr 1240 sale price + VAT / Cr 1250 / Cr 1241 / Cr 2200. The price is
    cost + the fixed profit: no rate, penalty or late-payment increase exists (payroll R48).

    Idempotent per contract: a second call returns the contract's live invoice; after a ZATCA rejection a new
    invoice (new number) replaces the rejected one.
    """
    entity = Entity.objects.get(pk=source.entity_id)
    system = access.system_user(DEFERRED_SALE_SYSTEM)
    access.require(system, entity, "invoicing.create_deferred_sale")
    if access_services.kind_of(approver) != Kind.HUMAN:  # payroll R38: people approve sales, never agents or systems
        access.deny(approver, entity, "payroll.approve_deferred_sale", "agent_forbidden")
    grant = access.require(approver, entity, "payroll.approve_deferred_sale", document=source)
    source_type, source_id = source._meta.label_lower, source.pk
    cost = _money(cost, "cost", positive=True, currency=SAR)
    profit = _money(profit_amount, "profit", currency=SAR)
    description_ar = (asset_description_ar or "").strip()
    if not description_ar:
        raise error("The asset description in Arabic is required.", "وصف الأصل بالعربية مطلوب.",
                    code="description_ar")
    with transaction.atomic():
        live = (DeferredSale.objects.select_for_update(of=("self",)).select_related("sales_document")
                .filter(entity=entity, source_type=source_type, source_id=source_id, active=True).first())
        if live is not None:
            if live.sales_document.status != Status.REJECTED:
                doc = live.sales_document
                doc.warnings = []
                return doc
            _deactivate(live)  # ZATCA rejected it: the number stays used, a new invoice replaces it (R20)
        bill = PurchaseDocument.objects.select_for_update(of=("self",)).filter(pk=getattr(purchase_bill, "pk", None)).first()
        today = _riyadh_now().date()
        _check_deferred_sale(entity, employee=employee, bill=bill, cost=cost, possession_date=possession_date,
                             possession_evidence_ref=possession_evidence_ref, today=today, sale_date=sale_date,
                             source_type=source_type, source_id=source_id, asset_type=asset_type)
        if not (entity.vat_number or "").strip():  # R94 / OQ-59: an entity without a VAT number cannot issue it yet
            raise _not_vat_registered(entity)
        customer = _employee_customer(entity, employee, system)
        terms_en = f" in {term_months} monthly instalments" if term_months else ""
        terms_ar = f" على {term_months} قسطًا شهريًا" if term_months else ""
        doc = SalesDocument(
            entity=entity, doc_type=SalesDocument.DocType.INVOICE, customer=customer, issue_date=today,
            currency=_currency(SAR, entity), zatca_profile=SalesDocument.Profile.SIMPLIFIED,
            supply_date=sale_date or today, created_by=system, source=DEFERRED_SALE_SOURCE,
            notes_en=(f"Bai' ajel (deferred-payment) sale at a fixed price, paid from salary{terms_en}. The price never "
                      "increases, also when an instalment is late."),
            notes_ar=(f"بيع آجل بثمن ثابت يُسدَّد من الراتب{terms_ar}. لا يزيد الثمن أبدًا ولو تأخر سداد قسط."),
        )
        _refresh_tax_point(doc)
        doc.save()
        token = _deferred_sale_writes.set(True)
        try:
            _deferred_line(doc, SALE_ASSET, cost, f"{description_ar} (فاتورة الشراء {bill.number})",
                           f"{(asset_description_en or '').strip() or description_ar} (purchase bill {bill.number})")
            if profit > ZERO:
                _deferred_line(doc, DEFERRED_PROFIT, profit, "ربح البيع الآجل (ثابت)", "Bai' ajel profit (fixed)")
            doc = SalesDocument.objects.select_for_update(of=("self",)).select_related("entity", "customer").get(pk=doc.pk)
            lines = list(doc.lines.select_related("vat_rate", "vat_exemption_reason", "income_account", "income_type"))
            _, _, vat, _ = _sales_breakdown(doc, lines)
            problems = _issue_problems(doc, check_period=False)
            if problems:
                raise _problems_error(problems, "document_invalid")
            approved_at = timezone.now()
            sale = DeferredSale(
                entity=entity, sales_document=doc, customer=customer, purchase_bill=bill, source_type=source_type,
                source_id=source_id, cost=cost, profit_amount=profit, vat_amount=vat, possession_date=possession_date,
                possession_evidence_ref=possession_evidence_ref.strip()[:200], approved_by=approver,
                approved_at=approved_at, created_by=system,
            )
            sale._service_write = True
            try:
                with transaction.atomic():
                    sale.save()
            except IntegrityError:  # the same asset sold at the same moment (one live sale per bill)
                raise error("This asset is already sold: its purchase bill backs another bai' ajel sale (R112).",
                            "هذا الأصل مبيع مسبقًا: فاتورة شرائه مستخدمة لبيع آجل آخر.",
                            code="asset_already_sold") from None
            doc.status = Status.SUBMITTED
            doc.submitted_by, doc.submitted_at = system, timezone.now()
            _bump(doc)
            doc._service_write = True
            doc.save()
            _issue_submitted(doc, rate_user=system, approved_by=approver, approved_at=approved_at)
        finally:
            _deferred_sale_writes.reset(token)
        if Decimal(doc.total_vat) != sale.vat_amount:  # the same lines, rate and rounding: never differs
            raise error("The VAT of the issued invoice differs from the sale's.", "تختلف ضريبة الفاتورة عن ضريبة البيع.",
                        code="vat_mismatch")
        access.use_grant(grant, target=doc)  # the approval the posting on reporting relies on (contract_approval)
        access_services.log(
            AccessLog.Event.PERMISSION_GRANTED, actor=system, entity=entity, permission="invoicing.create_deferred_sale",
            details={"document": [doc._meta.label_lower, doc.pk], "action": "issue_deferred_sale",
                     "approved_in": [source_type, source_id], "approved_by": approver.pk},
        )
        # The ZATCA queue system user hands it over (zatca R-32); a hand-off failure leaves it not_submitted for the
        # automatic resubmission (R79) and never undoes the payroll approval.
        port, queue = zatca_port.get_port(), access.system_user("zatca_queue")
        transaction.on_commit(lambda: port.submit_document(SalesDocument.objects.get(pk=doc.pk), queue), robust=True)
    doc.warnings = []
    return doc


def _live_deferred_sale(entity, source, *, lock=False):
    qs = DeferredSale.objects.select_related("sales_document").filter(
        entity=entity, source_type=source._meta.label_lower, source_id=source.pk, active=True)
    if lock:
        qs = qs.select_for_update(of=("self",))
    return qs.first()


def deferred_sale_of(source):
    """The live bai' ajel sale approved in ``source`` (a payroll contract), or None. No right needed: payroll reads
    its own contract's sale through its port (``invoicing.payroll_port``)."""
    return _live_deferred_sale(Entity.objects.get(pk=source.entity_id), source)


def _adjustment_final(adjustment):
    """Review 2026-10-20b, finding 2: an adjustment counts only once its credit note is final (reported or cleared
    by ZATCA and posted)."""
    note = adjustment.credit_note
    return note.status == Status.FINAL and note.journal_entry_id is not None


def _sale_figures(sale, *, prudent=False):
    """(fixed profit, receivable) of a sale less its payroll-approved adjustments (R128): an ibra' waives part of the
    profit not yet recognised and the VAT on it, so both shrink by the credit note.

    Review 2026-10-20b (finding 2): only adjustments whose credit note is **final** count; a pending one (issued,
    awaiting ZATCA) changes nothing and a rejected one never counts. ``prudent=True`` (profit recognition and the
    rebate ceiling only) also deducts pending ones, so no profit a pending rebate may waive is ever recognised; a
    rejected one never."""
    profit, receivable = Decimal(sale.profit_amount), Decimal(sale.receivable)
    for a in sale.adjustments.select_related("credit_note").filter(active=True):
        if _adjustment_final(a) or (prudent and a.credit_note.status == Status.ISSUED):
            profit -= a.net_amount
            receivable -= a.gross_amount
    return profit.quantize(CENT), receivable.quantize(CENT)


def create_deferred_sale_adjustment(source, *, approver, source_ref, amount, reason_ar, reason_en=""):
    """R128 (compliance A8): the credit note of a payroll-approved early-settlement rebate (ibra') on the bai' ajel
    sale approved in ``source``. Called only by payroll's rebate approval through ``invoicing.payroll_port``, inside
    its transaction; ``approver`` is the person approving the rebate there (their ``payroll.approve_deferred_sale``
    right for the contract is checked again and recorded as used for the credit note, the approval it is issued and
    posted on). ``source_ref`` names the payroll rebate and makes the call idempotent.

    ``amount`` (net, SAR) waives part of the fixed profit **not yet recognised**; never more (``rebate_too_large``).
    The deferred-sale system user issues a simplified credit note on the (posted) sale invoice: one line on 1241 at
    ``amount``, category S at the rate in force, VAT 15% credited to the employee; it posts on ZATCA reporting
    Dr 1241 / Dr 2200 / Cr 1240. Payroll reduces the planned instalments by the credit note's gross in the same
    transaction. A note by hand on a bai' ajel invoice stays refused (``deferred_sale_note``). After a ZATCA
    rejection a new call issues a new credit note for the same rebate."""
    entity = Entity.objects.get(pk=source.entity_id)
    system = access.system_user(DEFERRED_SALE_SYSTEM)
    access.require(system, entity, "invoicing.create_deferred_sale")
    if access_services.kind_of(approver) != Kind.HUMAN:
        access.deny(approver, entity, "payroll.approve_deferred_sale", "agent_forbidden")
    grant = access.require(approver, entity, "payroll.approve_deferred_sale", document=source)
    amount = _money(amount, "rebate", positive=True, currency=SAR)
    ref = str(source_ref or "").strip()[:100]
    if not ref or not (reason_ar or "").strip():
        raise error("The rebate needs its payroll reference and the reason in Arabic.",
                    "يحتاج الإبراء إلى مرجعه في الرواتب والسبب بالعربية.", code="adjustment_required")
    from payroll import invoicing_port as payroll_port  # review 2026-10-20b, finding 1: only payroll's approval

    problems = payroll_port.rebate_problems(source, source_ref=ref, amount=amount, approver=approver)
    if problems:
        access_services.log_denial(actor=approver, entity=entity, permission="payroll.approve_deferred_sale",
                                   details={"reason": "rebate_not_from_payroll", "source_ref": ref})
        raise _problems_error(problems, "rebate_not_from_payroll")
    with transaction.atomic():
        sale = _live_deferred_sale(entity, source, lock=True)
        if sale is None:
            raise error("No bai' ajel sale was invoiced for this contract (R112).",
                        "لم تصدر فاتورة بيع آجل لهذا العقد.", code="no_deferred_sale")
        invoice = sale.sales_document
        if invoice.status != Status.FINAL or invoice.journal_entry_id is None:
            raise error(f"The sale invoice {invoice.number} is not posted yet; a rebate waits for it (R128).",
                        f"فاتورة البيع {invoice.number} لم تُرحَّل بعد؛ ينتظر الإبراء ترحيلها.", code="sale_not_posted")
        live = (DeferredSaleAdjustment.objects.select_for_update(of=("self",)).select_related("credit_note")
                .filter(deferred_sale=sale, source_ref=ref, active=True).first())
        if live is not None:
            if live.credit_note.status != Status.REJECTED:
                if live.net_amount != amount:
                    raise error(f"Rebate {ref} was issued for {live.net_amount}, not {amount}.",
                                f"صدر الإبراء {ref} بمبلغ {live.net_amount} لا {amount}.", code="adjustment_changed")
                return live
            live.active = False  # ZATCA rejected its credit note: the number stays used, a new note replaces it
            live._service_write = True
            live.save(update_fields=["active"])
        recognised = Decimal(sale.releases.filter(status=DeferredProfitRelease.Status.POSTED)
                             .aggregate(t=Sum("profit_amount"))["t"] or ZERO).quantize(CENT)
        unearned = _sale_figures(sale, prudent=True)[0] - recognised  # finding 2: less rebates still pending
        if amount > unearned:
            raise error(f"The rebate {amount} is more than the profit not yet recognised ({unearned}); an ibra' waives "
                        "only unearned profit, never the cost (R128).", f"الإبراء {amount} أكبر من الربح غير المحقق "
                        f"({unearned})؛ لا يكون الإبراء إلا من الربح غير المحقق ولا يمس التكلفة.", code="rebate_too_large")
        today = _riyadh_now().date()
        note = SalesDocument(
            entity=entity, doc_type=SalesDocument.DocType.CREDIT_NOTE, customer=invoice.customer, issue_date=today,
            currency=invoice.currency, original_document=invoice, zatca_profile=invoice.zatca_profile,
            created_by=system, source=DEFERRED_SALE_SOURCE, payment_means_code=PaymentMeans.INSTRUMENT_NOT_DEFINED,
            reason_ar=reason_ar.strip()[:300], reason_en=(reason_en or "").strip()[:300],
            notes_en="Early-settlement rebate (ibra') of part of the fixed bai' ajel profit, approved in payroll; the "
                     "remaining instalments are reduced by this credit note.",
            notes_ar="إبراء من جزء من ربح البيع الآجل الثابت عند السداد المبكر، معتمد في الرواتب؛ وتُخفَّض الأقساط "
                     "المتبقية بمبلغ هذا الإشعار.",
        )
        _refresh_tax_point(note)
        note.save()
        # Finding 5 (R102): the note's VAT is at the rate of the original invoice's profit line, not today's.
        original = (invoice.lines.select_related("vat_rate").filter(income_account__code=DEFERRED_PROFIT)
                    .order_by("line_no").first())
        if original is None or original.vat_rate is None:
            raise error(f"The sale invoice {invoice.number} has no profit line with a VAT rate (R118).",
                        f"ليس لفاتورة البيع {invoice.number} بند ربح بنسبة ضريبة.", code="document_invalid")
        token = _deferred_sale_writes.set(True)
        try:
            line = _deferred_line(note, DEFERRED_PROFIT, amount, "إبراء من ربح البيع الآجل",
                                  "Bai' ajel profit rebate (ibra')", vat_rate=original.vat_rate)
            note = SalesDocument.objects.select_for_update(of=("self",)).select_related("entity", "customer").get(
                pk=note.pk)
            lines = list(note.lines.select_related("vat_rate", "vat_exemption_reason", "income_account", "income_type"))
            _, _, vat, _ = _sales_breakdown(note, lines)
            approved_at = timezone.now()
            adjustment = DeferredSaleAdjustment(
                entity=entity, deferred_sale=sale, credit_note=note, source_ref=ref, net_amount=amount, vat_amount=vat,
                approved_by=approver, approved_at=approved_at, created_by=system)
            adjustment._service_write = True
            adjustment.save()
            note.status = Status.SUBMITTED
            note.submitted_by, note.submitted_at = system, timezone.now()
            _bump(note)
            note._service_write = True
            note.save()
            _issue_submitted(note, rate_user=system, approved_by=approver, approved_at=approved_at,
                             line_rates={line.pk: original.vat_rate})
        finally:
            _deferred_sale_writes.reset(token)
        if Decimal(note.total_vat) != adjustment.vat_amount:
            raise error("The VAT of the credit note differs from the rebate's.", "تختلف ضريبة الإشعار عن ضريبة الإبراء.",
                        code="vat_mismatch")
        access.use_grant(grant, target=note)
        access_services.log(
            AccessLog.Event.PERMISSION_GRANTED, actor=system, entity=entity, permission="invoicing.create_deferred_sale",
            details={"document": [note._meta.label_lower, note.pk], "action": "issue_deferred_sale_adjustment",
                     "approved_in": [source._meta.label_lower, source.pk], "source_ref": ref,
                     "approved_by": approver.pk},
        )
        port, queue = zatca_port.get_port(), access.system_user("zatca_queue")
        transaction.on_commit(lambda: port.submit_document(SalesDocument.objects.get(pk=note.pk), queue), robust=True)
    return adjustment


def _deferred_sale_adjustment_of(source, source_ref):
    """The active adjustment ``source_ref`` of the live sale of ``source`` (payroll reads it through its port)."""
    sale = _live_deferred_sale(Entity.objects.get(pk=source.entity_id), source)
    if sale is None:
        return None
    return sale.adjustments.select_related("credit_note").filter(source_ref=str(source_ref)[:100], active=True).first()


def _profit_figures(sale, collected_after, profit_before):
    """Profit to recognise when ``collected_after`` of the receivable is collected: the fixed profit times the
    collected share, rounded half-up once; all of it once everything is collected. Never time-based (R48). With an
    adjustment (R128) the profit and receivable less the adjustments, a pending one included (prudence, finding 2:
    never recognise profit a pending rebate may waive; once its note is rejected the full figures catch up);
    never negative."""
    profit, receivable = _sale_figures(sale, prudent=True)
    if collected_after >= receivable:
        to_date = profit
    else:
        with localcontext() as ctx:
            ctx.prec = 60
            to_date = _round(profit * collected_after / receivable, 2)
    return max(ZERO, (Decimal(to_date) - Decimal(profit_before)).quantize(CENT))


def _require_collecting_approver(user, entity, source, *, source_ref, amount=None, mode="collect"):
    """Review fix 4 (R122 as amended): the call comes from the payroll approval that collected the instalment (or
    from the run reversal that gives it back), ``source_ref`` names a deducted (run) or settled (final settlement)
    installment of ``source`` at ``amount``, and ``user`` is that approval's person, a human holding its right in
    ``entity`` (owner decision A6). A CFO or anyone else calling directly is refused."""
    from payroll import invoicing_port as payroll_port

    fallback = "invoicing.post_deferred_profit" if mode == "collect" else DEFERRED_PROFIT_REVERSAL
    if access_services.kind_of(user) != Kind.HUMAN:
        access.deny(user, entity, fallback, "agent_forbidden")
    problems, permission = payroll_port.collection_problems(source, source_ref=source_ref, amount=amount, user=user,
                                                           mode=mode)
    if problems:
        access_services.log_denial(actor=user, entity=entity, permission=permission or fallback,
                                   details={"reason": "collection_not_from_payroll", "source_ref": str(source_ref)[:100]})
        raise _problems_error(problems, "collection_not_from_payroll")
    if not access.has(user, entity, permission):
        access.deny(user, entity, permission, "no_membership")


def recognise_deferred_profit(source, *, user, collected_amount, collected_on, source_ref):
    """R112 / payroll E12, E13: recognise the profit of the bai' ajel sale approved in ``source`` as payroll collects
    its instalments: Dr 1241 / Cr 4610, dated ``collected_on`` (the date of the payroll entry that collected it).

    Review fix 4: accepted only inside payroll's collecting marker (``payroll.invoicing_port.collecting``) of the run
    or final-settlement approval that collected the installment: ``user`` is that approval's person (holder of
    ``payroll.approve_run`` or ``payroll.approve_final_settlement``), ``source_ref`` is
    ``payroll.deferredsaleinstallment:<pk>`` of an installment of ``source`` deducted by that run (or settled by that
    settlement) and ``collected_amount`` is what it collected; otherwise ``collection_not_from_payroll``. The
    deferred-sale system user posts after that approval (owner decision A6). The same reference returns the
    recognition already posted (idempotent).

    The profit recognised to date is ``profit x collected to date / receivable`` (receivable = sale price + VAT),
    rounded half-up once; the collection that completes the receivable releases the rest, so the total is exactly
    the fixed profit. Nothing depends on dates or time elapsed (payroll R48). Refused before the sale invoice is
    posted (``sale_not_posted``) or beyond the receivable (``over_collected``).
    """
    entity = Entity.objects.get(pk=source.entity_id)
    system = access.system_user(DEFERRED_SALE_SYSTEM)
    amount = _money(collected_amount, "collected amount", positive=True, currency=SAR)
    ref = str(source_ref or "").strip()[:100]
    if not ref or not isinstance(collected_on, date_type):
        raise error("The collection needs its reference and date.", "يحتاج التحصيل إلى مرجعه وتاريخه.",
                    code="collection_required")
    _require_collecting_approver(user, entity, source, source_ref=ref, amount=amount)
    with transaction.atomic():
        sale = _live_deferred_sale(entity, source, lock=True)
        if sale is None:
            raise error("No bai' ajel sale was invoiced for this contract (R112).",
                        "لم تصدر فاتورة بيع آجل لهذا العقد.", code="no_deferred_sale")
        existing = DeferredProfitRelease.objects.filter(deferred_sale=sale, source_ref=ref,
                                                        status=DeferredProfitRelease.Status.POSTED).first()
        if existing is not None:
            if existing.collected_amount != amount:
                raise error(f"Collection {ref} was recognised for {existing.collected_amount}, not {amount}; reverse it "
                            "first.", f"سُجّل التحصيل {ref} بمبلغ {existing.collected_amount} لا {amount}؛ اعكسه أولًا.",
                            code="collection_changed")
            return existing
        doc = sale.sales_document
        if doc.status != Status.FINAL or doc.journal_entry_id is None:
            raise error(f"The sale invoice {doc.number} is not posted yet (it waits for ZATCA reporting); its profit is "
                        "recognised once it is posted (R41, R112).", f"فاتورة البيع {doc.number} لم تُرحَّل بعد (بانتظار "
                        "الإبلاغ للهيئة)؛ تتحقق أرباحها بعد ترحيلها.", code="sale_not_posted")
        posted = DeferredProfitRelease.objects.filter(deferred_sale=sale, status=DeferredProfitRelease.Status.POSTED)
        collected_before = Decimal(posted.aggregate(t=Sum("collected_amount"))["t"] or ZERO).quantize(CENT)
        profit_before = Decimal(posted.aggregate(t=Sum("profit_amount"))["t"] or ZERO).quantize(CENT)
        after = collected_before + amount
        receivable = _sale_figures(sale)[1]
        if after > receivable:
            raise error(f"{after} collected is more than the receivable {receivable} (sale price + VAT, less any "
                        "rebate).", f"المحصل {after} أكبر من المستحق {receivable} (ثمن البيع مع الضريبة بعد أي إبراء).",
                        code="over_collected")
        release = DeferredProfitRelease(
            entity=entity, deferred_sale=sale, source_ref=ref, date=collected_on, collected_amount=amount,
            collected_to_date=after, profit_amount=_profit_figures(sale, after, profit_before), created_by=user,
        )
        release._service_write = True
        release.save()
        if release.profit_amount > ZERO:
            grant = access.require(system, entity, "invoicing.post_deferred_profit", document=release)
            with _posting_as(system):
                entry = ledger.create_entry(
                    entity, collected_on, f"Bai' ajel profit {doc.number} / أرباح البيع الآجل {doc.number}", None,
                    user=system, reference=doc.number, post=True, grant=grant, document=release,
                )
            release.journal_entry = entry
            release._service_write = True
            release.save(update_fields=["journal_entry"])
    return release


def catch_up_deferred_profit(source, *, source_ref, on, approver):
    """Payroll review 5 (2026-10-21, item 3; payroll OQ-33): when ZATCA rejects the credit note of a bai' ajel
    rebate, nothing was waived, so the profit held back while it was pending (prudence, :func:`_profit_figures`) is
    recognised at once on what is already collected: Dr 1241 / Cr 4610 for ``profit x collected to date /
    receivable`` (full figures) less what was recognised, dated ``on``, by the deferred-sale system user through the
    ledger registry (a recognition with no new collection, ``collected_amount`` 0). Then the profit not yet recognised
    is exactly the profit on what is still outstanding, whatever happens to the balance next (collection, recovery or
    write-off after exit).

    Accepted only inside payroll's marker for that rejected rebate (``payroll.invoicing_port.catching_up``;
    ``source_ref`` = ``payroll.deferredsalerebate:<pk>:catch_up:<credit note>``); otherwise
    ``collection_not_from_payroll``. ``approver`` (the rebate's payroll approver, recorded as ``created_by``) is
    never the poster. Idempotent per reference; returns None when nothing was held back."""
    from payroll import invoicing_port as payroll_port

    entity = Entity.objects.get(pk=source.entity_id)
    system = access.system_user(DEFERRED_SALE_SYSTEM)
    ref = str(source_ref or "").strip()[:100]
    if not isinstance(on, date_type):
        raise error("The catch-up needs its date.", "يحتاج التحقق اللاحق إلى تاريخه.", code="collection_required")
    problems = payroll_port.catch_up_problems(source, source_ref=ref, approver=approver)
    if problems:
        access_services.log_denial(actor=system, entity=entity, permission="invoicing.post_deferred_profit",
                                   details={"reason": "collection_not_from_payroll", "source_ref": ref})
        raise _problems_error(problems, "collection_not_from_payroll")
    with transaction.atomic():
        sale = _live_deferred_sale(entity, source, lock=True)
        if sale is None:
            return None
        posted = DeferredProfitRelease.objects.filter(deferred_sale=sale, status=DeferredProfitRelease.Status.POSTED)
        existing = posted.filter(source_ref=ref).first()
        if existing is not None:
            return existing
        doc = sale.sales_document
        if doc.status != Status.FINAL or doc.journal_entry_id is None:
            return None
        collected = Decimal(posted.aggregate(t=Sum("collected_amount"))["t"] or ZERO).quantize(CENT)
        profit_before = Decimal(posted.aggregate(t=Sum("profit_amount"))["t"] or ZERO).quantize(CENT)
        if collected <= ZERO:
            return None
        amount = _profit_figures(sale, collected, profit_before)
        if amount <= ZERO:
            return None
        release = DeferredProfitRelease(
            entity=entity, deferred_sale=sale, source_ref=ref, date=on, collected_amount=ZERO,
            collected_to_date=collected, profit_amount=amount, created_by=approver,
        )
        release._service_write = True
        release.save()
        grant = access.require(system, entity, "invoicing.post_deferred_profit", document=release)
        with _posting_as(system):
            entry = ledger.create_entry(
                entity, on, f"Bai' ajel profit catch-up {doc.number} / تحقق لاحق لأرباح البيع الآجل {doc.number}", None,
                user=system, reference=doc.number, post=True, grant=grant, document=release,
            )
        release.journal_entry = entry
        release._service_write = True
        release.save(update_fields=["journal_entry"])
    return release


def reverse_deferred_profit(source, *, user, source_ref, date=None, reason=""):
    """Reverse the profit recognised for collection ``source_ref`` (a reversed payroll run gives its instalment
    back): the reversal of its own entry, through ``ledger.reverse_entry`` with the person's ``payroll.reverse_run``
    grant for the recognition. The next recognition is computed from what stays collected."""
    entity = Entity.objects.get(pk=source.entity_id)
    ref = str(source_ref or "").strip()[:100]
    _require_collecting_approver(user, entity, source, source_ref=ref, mode="reverse")
    with transaction.atomic():
        sale = _live_deferred_sale(entity, source, lock=True)
        release = None if sale is None else (
            DeferredProfitRelease.objects.select_for_update(of=("self",))
            .filter(deferred_sale=sale, source_ref=ref, status=DeferredProfitRelease.Status.POSTED).first())
        if release is None:
            raise error(f"No posted profit recognition for collection {ref}.", f"لا يوجد تحقق أرباح مرحّل للتحصيل {ref}.",
                        code="nothing_to_reverse")
        grant = access.require(user, entity, DEFERRED_PROFIT_REVERSAL, document=release)
        reversal = None
        if release.journal_entry_id is not None:
            token = _reversing_profit.set(release.pk)
            try:
                reversal = ledger.reverse_entry(release.journal_entry, grant, date=date, reason=reason)
            finally:
                _reversing_profit.reset(token)
        else:
            access.verify_grant(grant, entity, {DEFERRED_PROFIT_REVERSAL}, document=release)
        release.status = DeferredProfitRelease.Status.REVERSED
        release.reversal_entry = reversal
        release.reversed_by, release.reversed_at = user, timezone.now()
        release.reversal_reason = (reason or "")[:300]
        release._service_write = True
        release.save(update_fields=["status", "reversal_entry", "reversed_by", "reversed_at", "reversal_reason"])
    return release


def _bai_ajel_sale_entry_allowed(entry):
    """``ledger.restricted`` allowance for 1240 and 1241 (R127): the posting of a bai' ajel sale invoice (R118) or of
    a payroll-approved bai' ajel adjustment credit note (R128) of the same entity."""
    doc = SalesDocument.objects.filter(pk=entry.document_id, entity_id=entry.entity_id).first()
    if doc is None:
        return False
    if doc.doc_type == SalesDocument.DocType.INVOICE:
        return DeferredSale.objects.filter(sales_document_id=doc.pk, entity_id=entry.entity_id).exists()
    return DeferredSaleAdjustment.objects.filter(credit_note_id=doc.pk, entity_id=entry.entity_id).exists()


def _deferred_profit_entry_allowed(entry):
    """``ledger.restricted`` allowance for 1241 and 4610 (R127): a profit recognition's own entry."""
    return DeferredProfitRelease.objects.filter(pk=entry.document_id, entity_id=entry.entity_id).exists()


def _deferred_profit_lines(release):
    """Registered ``build_lines`` for DeferredProfitRelease (payroll E12): Dr 1241 / Cr 4610 for its profit."""
    _require_posting_context(release, DeferredProfitRelease.Status.POSTED)
    if release.profit_amount <= ZERO:
        raise error("No profit to recognise.", "لا يوجد ربح للتحقق.", code="nothing_to_post")
    number = release.deferred_sale.sales_document.number
    label = f"{number} {release.source_ref}"
    return [
        {"account": DEFERRED_PROFIT, "debit": release.profit_amount,
         "description": f"Bai' ajel profit earned {label} / ربح بيع آجل متحقق"[:300]},
        {"account": SALE_PROFIT_INCOME, "credit": release.profit_amount,
         "description": f"Bai' ajel profit income {label} / إيرادات أرباح البيع الآجل"[:300]},
    ]


def _deferred_profit_reverse_guard(release):
    return _reversing_profit.get() == release.pk
