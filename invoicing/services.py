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
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q, QuerySet, Sum
from django.utils import timezone

from core import access
from core.access import services as access_services
from core.access.models import AccessLog, Kind
from core.models import Currency, Entity, validate_country_code, validate_vat_number
from core.services import entity_ready_to_issue
from ledger import fx
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine, LedgerError

from . import zatca_port
from .models import (
    BUYER_ID_PRIORITY,
    CustomerReceipt,
    Customer,
    CustomerIdentifier,
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
    VendorPayment,
    WhtCategory,
    WhtDeduction,
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
CONTROL_ACCOUNTS = frozenset({AR, AR_GROUP, AP, AP_GROUP, VAT_OUTPUT, VAT_INPUT, WHT_PAYABLE})

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


def _sar_rate(entity, currency_code, fx_rate, on, user):
    """Rate from the document currency to SAR for the ZATCA VAT totals (R56)."""
    if currency_code == SAR:
        return Decimal("1")
    if entity.base_currency_id == SAR:
        return fx_rate
    try:
        return ledger.get_rate(entity, currency_code, SAR, on, user=user).rate
    except LedgerError as exc:
        raise _problems_error([f"A {currency_code}->SAR rate is required for ZATCA on {on}. / سعر {currency_code} "
                               f"إلى الريال مطلوب للهيئة بتاريخ {on}.", *exc.messages], "rate_required") from None


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


def _same_entity(entity, obj, what_en, what_ar):
    if obj is None or obj.entity_id != entity.pk:
        raise error(f"The {what_en} belongs to another entity.", f"{what_ar} يخص كيانًا آخر.", code="entity_mismatch")


# --- sales documents: drafts (3.3) ------------------------------------------------------------------

SALES_HEADER_FIELDS = {
    "zatca_profile", "flag_third_party", "flag_nominal", "flag_export", "flag_summary", "flag_self_billed",
    "issue_date", "supply_date", "supply_end_date", "due_date", "payment_means_code", "reason_code", "reason_ar",
    "reason_en", "adjustment_event_date", "notes_en", "notes_ar", "document_discount",
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
    elif doc_type != SalesDocument.DocType.INVOICE:
        raise error("A credit or debit note references its original invoice.", "الإشعار يشير إلى فاتورته الأصلية.",
                    code="original_required")
    with transaction.atomic():
        doc = _new_sales_document(entity, user=user, customer=customer, issue_date=issue_date, doc_type=doc_type,
                                  currency=currency, original_document=original_document, fields=fields)
        _log_agent(user, entity, "invoicing.create_sales_document", doc, "create_draft")
    return doc


def _new_sales_document(entity, *, user, customer, issue_date, doc_type, currency, original_document, fields,
                        source=None, **links):
    """A saved draft (rights checked by the caller)."""
    cur = _currency(currency or (original_document.currency_id if original_document else customer.currency_id), entity)
    doc = SalesDocument(
        entity=entity, doc_type=doc_type, customer=customer, issue_date=issue_date, currency=cur,
        original_document=original_document, created_by=user, source=source or _source(user), **links,
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
BILLING_HEADER_FIELDS = {"payment_means_code", "reason_code", "reason_ar", "reason_en", "notes_en", "notes_ar"}


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
    problems = _account_problems(entity, line.income_account, {Account.Type.INCOME}, "income")
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


def _price_line(line, on, places=2):
    """R9 / R93: net = round(qty x price - discount); line VAT = round(net x rate) for display only; both half-up
    to the document currency's decimals (``places``)."""
    with localcontext() as ctx:
        ctx.prec = 60
        net = _round(line.quantity * line.unit_price - line.line_discount, places)
    if net < ZERO:
        raise error("The line discount is larger than the line amount.", "خصم البند أكبر من مبلغه.", code="line_negative")
    line.net_amount = net
    line.vat_rate = _rate_on(line.vat_category, on)
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
    problems = _issue_problems(doc, check_period=False)
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
        problems += [_on_line(line.line_no, p) for p in _account_problems(
            entity, line.income_account, {Account.Type.INCOME}, "income")]
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


def _issue_submitted(doc, *, rate_user, approved_by, approved_at):
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
    problems = _issue_problems(doc, user=rate_user)
    if problems:
        raise _problems_error(problems, "document_invalid")
    entity = doc.entity
    lines = list(doc.lines.select_related("vat_rate", "vat_exemption_reason", "income_account", "income_type"))
    on = doc.supply_date or doc.issue_date
    fx_rate, rate_date, rate_source, rate_row = _fx_for(entity, doc.currency_id, doc.issue_date, rate_user)
    sar_rate = _sar_rate(entity, doc.currency_id, fx_rate, doc.issue_date, rate_user)
    for line in lines:
        _price_line(line, on, _doc_places(doc))
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
    doc.fx_exchange_rate = rate_row
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
        if targets:  # R37: a credit note is allocated to its original up to the open amount
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
    party = getattr(document, "customer", None) or document.vendor
    account, _ = _control(party, isinstance(document, SalesDocument))
    debit = isinstance(document, SalesDocument) and document.doc_type != SalesDocument.DocType.CREDIT_NOTE
    lines = JournalLine.objects.filter(entry_id=document.journal_entry_id, account__code=account, line_kind="normal")
    lines = lines.filter(debit__gt=0) if debit else lines.filter(credit__gt=0)
    return lines.order_by("pk").first()


def _pinned(document, specs):
    """Foreign-currency lines of ``document`` use the rate row pinned at approval (review item 2, nit 15)."""
    row = document.fx_exchange_rate
    if row is None:
        return list(specs)
    return [{**spec, "exchange_rate": row} if spec.get("currency") and not spec.get("settles_line") else spec
            for spec in specs]


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
    control, counterparty = _control(document.customer, receivable=True)
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
                          "vendor_total_gross", "vendor_vat_number"}


@transaction.atomic
def update_purchase_document(document, *, user, **fields):
    doc = _draft_purchase(document, user)
    unknown = set(fields) - PURCHASE_HEADER_FIELDS
    if unknown:
        raise error(f"Unknown fields: {', '.join(sorted(unknown))}.", "حقول غير معروفة.", code="unknown_field")
    for name, value in fields.items():
        if name in ("vendor_total_vat", "vendor_total_gross"):
            value = _money(value, name, currency=doc.currency)
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
            rate = cert.treaty_rate if cert else row.rate
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


def add_treaty_certificate(vendor, *, user, country, treaty_rate, valid_from, valid_to, received_on,
                           wht_category=None, document=None):
    """Enter a treaty certificate for a non-resident vendor (``invoicing.enter_wht_data``; R62). It counts only
    once a second person verifies it (:func:`verify_treaty_certificate`)."""
    vendor = Vendor.objects.select_related("entity").get(pk=vendor.pk)
    access.require(user, vendor.entity, "invoicing.enter_wht_data")
    if vendor.is_resident:
        raise error("Treaty certificates are for non-resident vendors.", "شهادات الاتفاقيات للموردين غير المقيمين.",
                    code="vendor_resident")
    rate = _decimal(treaty_rate, "treaty rate")
    if not fx.has_places(rate, 2) or not (ZERO <= rate < HUNDRED):
        raise error("The treaty rate is a percentage from 0 to below 100 with 2 decimals.",
                    "نسبة الاتفاقية نسبة مئوية من 0 إلى أقل من 100 بخانتين عشريتين.", code="treaty_rate")
    if valid_to < valid_from:
        raise error("The certificate ends before it starts.", "تنتهي الشهادة قبل أن تبدأ.", code="treaty_dates")
    if wht_category is not None and not isinstance(wht_category, WhtCategory):
        wht_category = _wht_category(wht_category)
    cert = TreatyCertificate(entity=vendor.entity, vendor=vendor, country=str(country).upper(), treaty_rate=rate,
                             wht_category=wht_category, valid_from=valid_from, valid_to=valid_to,
                             received_on=received_on, created_by=user)
    try:
        validate_country_code(cert.country)
    except Exception:  # noqa: BLE001
        raise error(f"{country!r} is not an ISO country code.", f"{country!r} ليس رمز دولة صالحًا.",
                    code="country") from None
    if document is not None:
        cert.document = document
    cert.save()
    return cert


@transaction.atomic
def verify_treaty_certificate(certificate, *, user):
    """A second person verifies a treaty certificate (``invoicing.verify_wht_data``; never the one who entered
    it). From then on it is never edited (3.2)."""
    cert = TreatyCertificate.objects.select_for_update(of=("self",)).select_related("entity").get(pk=certificate.pk)
    access.require(user, cert.entity, "invoicing.verify_wht_data")
    if cert.created_by_id == user.pk:
        access.deny(user, cert.entity, "invoicing.verify_wht_data", "self_approval")
    if cert.verified_at is not None:
        raise error("The certificate is already verified.", "تم التحقق من الشهادة مسبقًا.", code="certificate_verified")
    cert.verified_by, cert.verified_at = user, timezone.now()
    cert.save()
    return cert


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
        key = (d.wht_category.code, d.payment.vendor.code, d.rate_applied, d.treaty_certificate_id)
        r = rows.setdefault(key, {
            "category": d.wht_category.code, "return_line_code": d.wht_category.return_line_code,
            "vendor": d.payment.vendor.code, "country": d.payment.vendor.residence_country, "rate": d.rate_applied,
            "treaty_certificate": d.treaty_certificate_id,
            "treaty_country": d.treaty_certificate.country if d.treaty_certificate_id else "",
            "base_sar": ZERO, "wht_sar": ZERO, "gross_up": d.gross_up,
        })
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
        entry__document_type="invoicing.vendorpayment", account__code=WHT_PAYABLE,
    ).aggregate(c=Sum("credit"), d=Sum("debit"))
    ledger_total = (lines["c"] or ZERO) - (lines["d"] or ZERO)
    out = sorted(rows.values(), key=lambda r: (r["category"], r["vendor"]))
    total = sum((r["wht_sar"] for r in out), ZERO)
    return {"entity": entity.code, "month": start, "rows": out, "total_wht_sar": total,
            "ledger_2220_sar": ledger_total, "difference": total - ledger_total}


# --- purification (R54, E14, E15; OQ-30 amount, timing and recipient pending Sharia board sign-off) ----------

PURIFICATION_PERMISSION = {
    PurificationTransfer.Kind.TRANSFER: "invoicing.post_purification_transfer",
    PurificationTransfer.Kind.PAYOUT: "invoicing.approve_purification_payout",
}


def _posted_purification(entity, kind, as_of=None, exclude_pk=None):
    """Posted (not reversed) transfers or payouts of ``kind`` up to ``as_of``; reversed rows never count (R54)."""
    qs = PurificationTransfer.objects.filter(entity=entity, kind=kind, status=PurificationTransfer.Status.POSTED)
    if as_of is not None:
        qs = qs.filter(date__lte=as_of)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.aggregate(t=Sum("amount"))["t"] or ZERO


def _purification_room(entity, kind, on, exclude_pk=None):
    """R6, R54: the room left for a ``kind`` row dated ``on``, as (amount, date): the smallest of flagged less
    transferred (a transfer) or transferred less paid out (a payout) on ``on`` **and on every later date** on
    which a flagged line or a posted row is booked, so the last one is the all-time position. A backdated row can
    therefore never push a later date over its limit (review finding 1). Reversed rows do not count.
    """
    deltas = {}
    flagged = (JournalLine.objects.filter(entry__entity=entity, entry__status__in=ledger.BOOKED,
                                          requires_purification=True)
               .values("entry__date").annotate(c=Sum("credit"), d=Sum("debit")))
    for row in flagged:
        deltas.setdefault(row["entry__date"], [ZERO, ZERO, ZERO])[0] += (row["c"] or ZERO) - (row["d"] or ZERO)
    rows = (PurificationTransfer.objects.filter(entity=entity, status=PurificationTransfer.Status.POSTED)
            .exclude(pk=exclude_pk).values("date", "kind").annotate(t=Sum("amount")))
    for row in rows:
        i = 1 if row["kind"] == PurificationTransfer.Kind.TRANSFER else 2
        deltas.setdefault(row["date"], [ZERO, ZERO, ZERO])[i] += row["t"] or ZERO
    deltas.setdefault(on, [ZERO, ZERO, ZERO])
    flagged_sum = transferred = paid = ZERO
    room = at = None
    for day in sorted(deltas):
        f, t, p = deltas[day]
        flagged_sum, transferred, paid = flagged_sum + f, transferred + t, paid + p
        if day < on:
            continue
        left = flagged_sum - transferred if kind == PurificationTransfer.Kind.TRANSFER else transferred - paid
        if room is None or left < room:
            room, at = left, day
    return _round(room, 2), at  # SQLite sums lose the scale; amounts are SAR with 2 decimals


def purification_position(entity, *, user, as_of=None):
    """R6, R54 / AT-45, AT-71: non-compliant income flagged in the ledger (``ledger.purification_amount``), what was
    transferred to 2600 (E14) and paid out (E15), and what is left to transfer and to pay. ``invoicing.view`` and
    ``ledger.view``. Reversed transfers and payouts do not count."""
    access.require(user, entity, "invoicing.view")
    flagged = ledger.purification_amount(entity, user=user, as_of=as_of)
    transferred = _posted_purification(entity, PurificationTransfer.Kind.TRANSFER, as_of)
    paid = _posted_purification(entity, PurificationTransfer.Kind.PAYOUT, as_of)
    return {"flagged": flagged, "transferred": transferred, "paid_out": paid, "to_transfer": flagged - transferred,
            "payable": transferred - paid}


def _purification_problems(doc, user, exclude_self=True):
    problems = []
    entity = doc.entity
    pk = doc.pk if exclude_self else None
    access.require(user, entity, "ledger.view")  # the limits come from the ledger's flagged lines
    left, at = _purification_room(entity, doc.kind, doc.date, exclude_pk=pk)
    if doc.kind == PurificationTransfer.Kind.TRANSFER:
        if doc.amount > left:
            problems.append(f"Only {left} of non-compliant income is left to purify on {at} (R6, R54; checked on "
                            f"{doc.date} and every later date). / المتبقي من الإيرادات غير المتوافقة للتطهير {left} "
                            f"فقط بتاريخ {at} (يُتحقق في {doc.date} وفي كل تاريخ لاحق).")
    else:
        if doc.amount > left:
            problems.append(f"Only {left} is on the charity payable (2600) on {at}; transfer first (E14; checked on "
                            f"{doc.date} and every later date). / رصيد مستحقات التطهير {left} فقط بتاريخ {at}؛ "
                            f"حوّل أولًا (يُتحقق في {doc.date} وفي كل تاريخ لاحق).")
        if not doc.recipient_ar.strip():
            problems.append("The recipient charity is named in Arabic (OQ-30). / اسم الجهة الخيرية المستفيدة بالعربية مطلوب.")
        if doc.bank_account_id is None:
            problems.append("A payout needs the bank account it is paid from. / يحتاج الصرف إلى الحساب البنكي.")
    period = _open_period(entity, doc.date)
    if period:
        problems.append(period)
    return problems


def _lock_purification(entity):
    """R54: posting and reversing purification rows of one entity run one at a time, so two concurrent posts can
    never both pass the limits (``FOR NO KEY UPDATE`` on the entity row: inserts that reference the entity are
    not blocked; a no-op on SQLite)."""
    Entity.objects.select_for_update(no_key=True).filter(pk=entity.pk).values_list("pk", flat=True).first()


def create_purification_transfer(entity, *, user, kind, date, amount, bank_account=None, recipient_ar="",
                                 recipient_en="", reference_ar="", reference_en=""):
    """Record a purification transfer (E14) or payout (E15) as a draft (R54). A person holding
    ``invoicing.post_purification_transfer`` records it; agents and system users never (R54: a human user). A
    second person posts it (:func:`post_purification_transfer`)."""
    if access_services.kind_of(user) != Kind.HUMAN:  # R54: never an agent or a system job
        access.deny(user, entity, "invoicing.post_purification_transfer", "agent_forbidden")
    if not access.has(user, entity, "invoicing.post_purification_transfer"):
        access.deny(user, entity, "invoicing.post_purification_transfer", "no_membership")
    access.require(user, entity, "ledger.view")  # the amounts come from the ledger's flagged lines
    if kind not in PurificationTransfer.Kind.values:
        raise error("Unknown purification kind.", "نوع تطهير غير معروف.", code="kind")
    doc = PurificationTransfer(
        entity=entity, kind=kind, date=date, amount=_money(amount, "amount", positive=True,
                                                           currency=entity.base_currency_id),
        recipient_ar=(recipient_ar or "").strip(), recipient_en=(recipient_en or "").strip(),
        reference_ar=reference_ar or "", reference_en=reference_en or "", created_by=user, source=_source(user),
    )
    if kind == PurificationTransfer.Kind.PAYOUT:
        doc.bank_account = _bank_account(entity, bank_account)
    problems = _purification_problems(doc, user)
    if problems:
        raise _problems_error(problems, "purification_invalid")
    doc.save()
    return doc


def post_purification_transfer(transfer, *, user):
    """Post a recorded transfer (``invoicing.post_purification_transfer``) or payout
    (``invoicing.approve_purification_payout``, the CFO): E14 / E15 in one step, numbered ``PUR``. The recorder
    cannot post it (ledger segregation, access R35); agents never (R54)."""
    doc = PurificationTransfer.objects.select_related("entity").get(pk=transfer.pk)
    permission = PURIFICATION_PERMISSION[doc.kind]
    grant = access.require(user, doc.entity, permission, document=doc)
    seen_version = doc.version
    with transaction.atomic():
        _lock_purification(doc.entity)
        doc = PurificationTransfer.objects.select_for_update(of=("self",)).select_related("entity").get(pk=doc.pk)
        if doc.status != PurificationTransfer.Status.DRAFT:
            raise error("Only recorded drafts are posted.", "تُرحَّل المسودات المسجلة فقط.", code="not_draft")
        _refuse_stale_grant(grant, doc, seen_version)
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
    return doc


def reverse_purification_transfer(transfer, *, user, reason, date=None):
    """Reverse a posted purification transfer (E14) or payout (E15) (R54, review finding 8): through
    ``ledger.services.reverse_entry`` with ``invoicing.reverse_document`` (registered for the type), by a person,
    with a reason. The row becomes ``reversed`` and stops counting towards the limits. A transfer is not reversed
    while the payouts after it would then exceed what is left on 2600 (reverse the payout first). The reversal is
    dated on the original date if its period is open, else at the start of the first open period after it (R32).
    """
    doc = PurificationTransfer.objects.select_related("entity").get(pk=transfer.pk)
    if access_services.kind_of(user) != Kind.HUMAN:  # R54: never an agent or a system job
        access.deny(user, doc.entity, "invoicing.reverse_document", "agent_forbidden")
    grant = access.require(user, doc.entity, "invoicing.reverse_document", document=doc)
    if not (reason or "").strip():
        raise error("The reason for the reversal is required.", "سبب العكس مطلوب.", code="reason_required")
    with transaction.atomic():
        _lock_purification(doc.entity)
        doc = PurificationTransfer.objects.select_for_update(of=("self",)).select_related(
            "entity", "journal_entry").get(pk=doc.pk)
        if doc.status != PurificationTransfer.Status.POSTED or doc.journal_entry_id is None:
            raise error("Only posted purification rows are reversed.", "تُعكس قيود التطهير المرحّلة فقط.",
                        code="not_posted")
        if doc.kind == PurificationTransfer.Kind.TRANSFER:
            left, at = _purification_room(doc.entity, PurificationTransfer.Kind.PAYOUT, doc.date, exclude_pk=doc.pk)
            if left < ZERO:
                raise error(f"Without this transfer the payouts would exceed the charity payable (2600) by {-left} "
                            f"on {at}; reverse the payout first (R54).",
                            f"بدون هذا التحويل تتجاوز المبالغ المصروفة رصيد مستحقات التطهير بمقدار {-left} بتاريخ "
                            f"{at}؛ اعكس الصرف أولًا.", code="purification_invalid")
        on = _reversal_date(doc.entity, doc.journal_entry.date, date)
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
    return doc


def _purification_lines(document):
    """Registered ``build_lines`` for PurificationTransfer: E14 (Dr 4900 / Cr 2600) or E15 (Dr 2600 / Cr bank)."""
    _require_posting_context(document, PurificationTransfer.Status.POSTED)
    label = f"{document.number} {document.reference_ar or document.reference_en}".strip()
    if document.kind == PurificationTransfer.Kind.TRANSFER:
        return [{"account": NON_COMPLIANT_INCOME, "debit": document.amount, "description": label},
                {"account": CHARITY_PAYABLE, "credit": document.amount, "description": label}]
    return [{"account": CHARITY_PAYABLE, "debit": document.amount, "description": label},
            {"account": document.bank_account, "credit": document.amount,
             "description": f"{label} {document.recipient_ar}"[:300]}]


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


def _party_record(party, unmasked):
    fields = PERSONAL_PARTY_FIELDS + (("bank_iban",) if isinstance(party, Vendor) else ())
    record = {"code": party.code, "kind": party.customer_kind if isinstance(party, Customer) else party.vendor_kind,
              "entity": party.entity.code, "is_vat_registered": party.is_vat_registered, "country": party.country,
              "masked": not unmasked}
    for name in fields:
        record[name] = getattr(party, name) if unmasked else MASK
    if isinstance(party, Customer):
        record["identifiers"] = ([{"scheme": i.scheme, "value": i.value} for i in party.identifiers.all()]
                                 if unmasked else MASK)
    return record


def _view_party(party, user, how):
    access.require(user, party.entity, "invoicing.view")
    individual = _is_individual(party)
    unmasked = not individual or _may_see_personal(user, party.entity)
    if individual and unmasked:
        _log_personal_data(user, party.entity, [(party._meta.label_lower, party.pk)], how)
    return _party_record(party, unmasked)


def view_customer(customer, *, user):
    """A customer's record (R51): an individual's personal data only for holders of
    ``invoicing.view_personal_data`` in its entity, each such view logged (AT-44); masked for everyone else."""
    return _view_party(Customer.objects.select_related("entity").get(pk=customer.pk), user, "view")


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


def anonymisation_candidates(today):
    """R52: what the anonymisation job would change on ``today``: final or rejected sales documents of individual
    customers whose ``retention_until`` has passed, and individual customers whose every document is (or is about
    to be) anonymised. Customers without documents have no retention date and are left alone."""
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
    return sorted(doc_ids), customers


def anonymise_expired_personal_data(*, today=None, apply=False):
    """R52 / AT-43: the anonymisation job (operator command ``invoicing_anonymise``; never a user action).

    Dry run by default: it only reports what it would change. With ``apply=True`` it replaces individuals' names,
    IDs and addresses on those documents and customers by a fixed token; amounts, VAT, numbers, dates and the ZATCA
    hash and QR are kept, and journal entries are never touched. Applying is refused unless the setting
    ``INVOICING_ANONYMISATION_ENABLED`` is True: the retention rules await confirmation (zatca OQ-8, OQ-31), so
    it is off by default. Returns {"documents": [...], "customers": [...], "applied": bool}.
    """
    from django.conf import settings

    today = today or _riyadh_now().date()
    doc_ids, customer_ids = anonymisation_candidates(today)
    result = {"documents": doc_ids, "customers": customer_ids, "applied": False, "date": today}
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
        for doc in SalesDocument.objects.select_for_update(of=("self",)).filter(pk__in=doc_ids):
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
        for c in Customer.objects.select_for_update(of=("self",)).filter(pk__in=customer_ids):
            for name in _ANON_PARTY_TEXT:
                setattr(c, name, ANONYMISED_EN)
            for name in _ANON_PARTY_TEXT_AR:
                setattr(c, name, ANONYMISED_AR)
            for name in _ANON_PARTY_BLANK:
                setattr(c, name, "")
            c.is_vat_registered, c.is_active, c.anonymised_at = False, False, now
            c.save()
            CustomerIdentifier.objects.filter(customer=c).delete()
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
        if model is PurchaseDocument and PurchaseDocument.objects.filter(
                original_document=document, status=PStatus.POSTED).exists():  # nit 17
            raise error("A posted vendor credit note references this bill; reverse the credit note first.",
                        "يشير إشعار دائن مرحّل من المورد إلى هذه الفاتورة؛ اعكس الإشعار أولًا.", code="has_credit_notes")
        own, others = _allocations_of(document)
        if any(not a.via_settlement for a in own) or others:
            raise error("The document has allocations; un-allocate them first (R25, R33).",
                        "للمستند تخصيصات؛ ألغِها أولًا.", code="has_allocations")
        on = _reversal_date(document.entity, document.journal_entry.date, date)
        try:
            reversal = ledger.reverse_entry(document.journal_entry, grant, date=on, reason=reason)
        except LedgerError as exc:
            raise _problems_error(exc.messages, getattr(exc, "code", None) or "reversal_refused") from None
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

    for d in booked(SalesDocument.objects.all(), reversible=False).select_related("customer__group_entity"):
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
    line._billing_write = True
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
    ``third_decimal_pending``, ``rate_required``, ``contract_not_billable`` or ``document_invalid``.
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
        lines = list(doc.lines.select_related("vat_rate", "vat_exemption_reason"))
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
        if approved and _settings(entity).auto_issue_from_approved_contract:
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
        QuerySet.delete(SalesDocumentLine.objects.filter(document=doc))  # the billing-line guard refuses all others
        doc.delete()
