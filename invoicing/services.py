"""Invoicing services (docs/specs/invoicing.md), slice 1. The only supported way to change invoicing data.

Every public function checks rights with ``core.access.require`` for the
entity that owns the record (access R1). Everything that affects the books
goes through ``ledger.services`` with the document's registered line hooks
(``ledger.documents``): the document builds its own journal lines from its
stored data, never from lines a caller passes.

Workflow (D17): staff create and submit drafts; the approver (CFO) approves.
Bills and vendor payments post on approval; sales documents are issued on
approval and post when ZATCA clears or reports them (R40, R41); receipts are
posted by a second person (R33, OQ-49).
"""
import calendar
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext

from django.db import transaction
from django.db.models import Sum
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
    ReceiptApplication,
    ReceivableAllocation,
    SalesDocument,
    SalesDocumentLine,
    VatCategory,
    VatExemptionReason,
    VatRate,
    Vendor,
    VendorPayment,
    error,
)

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")
SAR = "SAR"
SIMPLIFIED_LIMIT_SAR = Decimal("1000.00")  # R48: "less than SAR 1,000"; gross in SAR by default (OQ-44)

# Accounts (ledger/coa_template.py; invoicing §5).
AR, AR_GROUP, AP, AP_GROUP = "1200", "1210", "2100", "2110"
VAT_OUTPUT, VAT_INPUT, BANK, BANK_CHARGES = "2200", "1310", "1100", "5700"
CONTROL_ACCOUNTS = frozenset({AR, AR_GROUP, AP, AP_GROUP, VAT_OUTPUT, VAT_INPUT})

Status = SalesDocument.Status
PStatus = PurchaseDocument.Status
SEQUENCE_CODES = {
    "invoice": "INV", "credit_note": "CN", "debit_note": "DN", "bill": "BILL", "vendor_credit_note": "VCN",
}

# The person whose ledger.view right is used while a document's lines are built (fx_settlement_lines, R18).
_posting_user = ContextVar("invoicing_posting_user", default=None)


def _problems_error(problems, code):
    exc = InvoicingError(list(problems), code=code)
    exc.code = code
    return exc


# --- small helpers --------------------------------------------------------------------------------

def _round(amount):
    with localcontext() as ctx:
        ctx.prec = 60
        return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value, what):
    """Decimal from Decimal, int or string; floats are rejected (R56). LedgerError is re-raised bilingual."""
    try:
        return fx.to_decimal(value, what)
    except LedgerError as exc:
        raise InvoicingError(exc.messages, code="float_rejected" if isinstance(value, float) else "not_a_number") from None


def _money(value, what, *, positive=False):
    amount = _decimal(value, what)
    if not fx.has_places(amount, 2):
        raise error(f"{what}: amounts have at most 2 decimals ({amount}).",
                    f"{what}: المبالغ بخانتين عشريتين كحد أقصى ({amount}).", code="money_decimals")
    if amount < ZERO or (positive and amount == ZERO):
        raise error(f"{what} must be {'above' if positive else 'at least'} zero.",
                    f"يجب أن يكون {what} {'أكبر من' if positive else 'لا يقل عن'} صفر.", code="money_sign")
    return amount.quantize(CENT)


def _source(user):
    return access_services.source_of(user) or "human"


def _log_agent(user, entity, permission, document, action):
    """Gateway rule: every agent action is logged (D8, access R21)."""
    if access_services.kind_of(user) == Kind.AGENT:
        access_services.log(
            AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=entity, permission=permission,
            details={"document": [document._meta.label_lower, document.pk], "action": action},
        )


def _currency(code, entity):
    """R74 (OQ-48 default) and D13: an active currency with 2 minor units."""
    cur = Currency.objects.filter(code=str(code or entity.base_currency_id).upper()).first()
    if cur is None or not cur.is_active:
        raise error(f"Currency {code} is unknown or inactive.", f"العملة {code} غير معروفة أو غير نشطة.",
                    code="currency_inactive")
    if cur.minor_units != 2:
        raise error(
            f"Documents in {cur.code} ({cur.minor_units} decimals) are not supported yet (OQ-48).",
            f"المستندات بعملة {cur.code} ({cur.minor_units} خانات عشرية) غير مدعومة بعد (OQ-48).",
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
    """(rate, rate_date, source) to the functional currency on ``on`` (ledger-fx R12); bilingual error if none."""
    if currency_code == entity.base_currency_id:
        return Decimal("1"), on, "functional"
    try:
        found = ledger.get_rate(entity, currency_code, entity.base_currency_id, on, user=user)
    except LedgerError as exc:
        raise _problems_error(
            [f"An exchange rate is required for {currency_code} on {on}. / سعر الصرف مطلوب للعملة {currency_code} "
             f"بتاريخ {on}.", *exc.messages], "rate_required") from None
    return found.rate, found.row.date if found.row else on, found.row.source if found.row else "table"


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
VENDOR_FIELDS = PARTY_FIELDS - {"payment_terms_days"} | {"is_resident", "residence_country", "bank_iban"}


def _save_party(party, user, fields, allowed):
    unknown = set(fields) - allowed
    if unknown:
        raise error(f"Unknown fields: {', '.join(sorted(unknown))}.", "حقول غير معروفة.", code="unknown_field")
    for name, value in fields.items():
        if name == "currency":
            value = _currency(value, party.entity)
        setattr(party, name, value)
    if party.currency_id is None:
        party.currency_id = party.entity.base_currency_id
    problems = party_problems(party)
    try:
        party.full_clean(exclude=["name_ar"])
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
    """Create a vendor in ``entity`` (``invoicing.manage_vendor``)."""
    access.require(user, entity, "invoicing.manage_vendor")
    return _save_party(Vendor(entity=entity, created_by=user, source=_source(user)), user, fields, VENDOR_FIELDS)


@transaction.atomic
def update_vendor(vendor, *, user, **fields):
    vendor = Vendor.objects.select_for_update(of=("self",)).get(pk=vendor.pk)
    access.require(user, vendor.entity, "invoicing.manage_vendor")
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
            value = _money(value, "document discount")
        if name == "reason_code" and value is not None and not isinstance(value, NoteReason):
            value = NoteReason.objects.get(code=value)
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
    cur = _currency(currency or (original_document.currency_id if original_document else customer.currency_id), entity)
    with transaction.atomic():
        doc = SalesDocument(
            entity=entity, doc_type=doc_type, customer=customer, issue_date=issue_date, currency=cur,
            original_document=original_document, created_by=user, source=_source(user),
        )
        if customer.customer_kind == Customer.Kind.INDIVIDUAL:  # OQ-19: simplified only for individuals by default
            doc.zatca_profile = SalesDocument.Profile.SIMPLIFIED
        if original_document is not None:  # zatca R-19: the category follows the original invoice
            doc.zatca_profile = original_document.zatca_profile
        _apply_sales_header(doc, fields)
        doc.save()
        _log_agent(user, entity, "invoicing.create_sales_document", doc, "create_draft")
    return doc


def _draft_sales(document, user, permission="invoicing.create_sales_document"):
    doc = SalesDocument.objects.select_for_update(of=("self",)).select_related("entity").get(pk=document.pk)
    access.require(user, doc.entity, permission)
    if doc.status != Status.DRAFT:
        raise error("Only drafts can be edited (R13).", "يمكن تعديل المسودات فقط.", code="not_draft")
    return doc


def _touch(doc, user):
    """The user changed the draft: they cannot approve it, and earlier approvals no longer count (access R14, R42)."""
    doc.modified_by, doc.modified_at = user, timezone.now()


@transaction.atomic
def update_sales_document(document, *, user, **fields):
    doc = _draft_sales(document, user)
    _apply_sales_header(doc, fields)
    _touch(doc, user)
    doc.save()
    return doc


def _sales_line_values(doc, line, *, income_type, quantity, unit_price, description_ar, description_en, vat_category,
                       vat_exemption_reason, income_account, line_discount, unit_code):
    entity = doc.entity
    if not isinstance(income_type, IncomeType):
        income_type = IncomeType.objects.get(code=income_type)
    line.income_type = income_type
    line.quantity = _decimal(quantity, "quantity")
    if not fx.has_places(line.quantity, 3) or line.quantity <= ZERO:
        raise error("Quantity must be above zero with at most 3 decimals.", "يجب أن تكون الكمية أكبر من صفر بثلاث خانات كحد أقصى.",
                    code="quantity")
    line.unit_price = _money(unit_price, "unit price")  # OQ-12: 2 dp
    line.line_discount = _money(line_discount, "line discount")
    line.unit_code = unit_code or "C62"
    line.description_ar = (description_ar or "").strip()
    line.description_en = (description_en or "").strip()
    if not line.description_ar:  # OQ-18 (D19, confirmed I15): Arabic is required on every sales line
        raise error("The Arabic line description is required.", "وصف البند بالعربية مطلوب.", code="description_ar")
    line.vat_category = vat_category or income_type.default_vat_category
    if line.vat_category not in VatCategory.values:
        raise error(f"Unknown VAT category {line.vat_category!r}.", "فئة ضريبة غير معروفة.", code="vat_category")
    reason = vat_exemption_reason or income_type.default_exemption_reason
    if reason is not None and not isinstance(reason, VatExemptionReason):
        reason = VatExemptionReason.objects.get(code=reason)
    line.vat_exemption_reason = reason
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
    _price_line(line, doc.supply_date or doc.issue_date)


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
    elif line.vat_exemption_reason.category != line.vat_category:
        problems.append(f"Exemption reason {line.vat_exemption_reason.code} is not for category {line.vat_category}. / "
                        f"سبب الإعفاء {line.vat_exemption_reason.code} لا يخص الفئة {line.vat_category}.")
    return problems


def _price_line(line, on):
    """R9: net = round(qty x price - discount); line VAT = round(net x rate) for display only."""
    with localcontext() as ctx:
        ctx.prec = 60
        net = _round(line.quantity * line.unit_price - line.line_discount)
    if net < ZERO:
        raise error("The line discount is larger than the line amount.", "خصم البند أكبر من مبلغه.", code="line_negative")
    line.net_amount = net
    line.vat_rate = _rate_on(line.vat_category, on)
    line.vat_amount = _round(net * line.vat_rate.rate / HUNDRED) if line.vat_rate else ZERO


@transaction.atomic
def add_sales_line(document, *, user, income_type, quantity, unit_price, description_ar, description_en="",
                   vat_category=None, vat_exemption_reason=None, income_account=None, line_discount="0",
                   unit_code="C62"):
    """Add a line to a draft (R5 Sharia, R9 VAT, R10 rates, OQ-18 Arabic)."""
    doc = _draft_sales(document, user)
    last = doc.lines.order_by("-line_no").values_list("line_no", flat=True).first() or 0
    line = SalesDocumentLine(entity=doc.entity, document=doc, line_no=last + 1)
    _sales_line_values(doc, line, income_type=income_type, quantity=quantity, unit_price=unit_price,
                       description_ar=description_ar, description_en=description_en, vat_category=vat_category,
                       vat_exemption_reason=vat_exemption_reason, income_account=income_account,
                       line_discount=line_discount, unit_code=unit_code)
    line.save()
    _touch(doc, user)
    doc.save(update_fields=["modified_by", "modified_at", "updated_at"])
    return line


@transaction.atomic
def delete_sales_line(line, *, user):
    doc = _draft_sales(line.document, user)
    SalesDocumentLine.objects.get(pk=line.pk, document=doc).delete()
    _touch(doc, user)
    doc.save(update_fields=["modified_by", "modified_at", "updated_at"])


@transaction.atomic
def delete_sales_draft(document, *, user):
    """Drafts may be deleted (R13); a document with a number never is (R14, R17)."""
    doc = _draft_sales(document, user)
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
    doc._service_write = True
    doc.save()
    return doc


# --- sales documents: validation, totals and issue ---------------------------------------------------

def _sales_breakdown(doc, lines, sar_rate=None):
    """R9: VAT per (category, rate) at document level, half-up; line VAT is display only. R56: SAR per category."""
    groups = {}
    for line in lines:
        key = (line.vat_category, line.vat_rate.rate if line.vat_rate else ZERO)
        g = groups.setdefault(key, {"taxable": ZERO, "reasons": set()})
        g["taxable"] += line.net_amount
        if line.vat_exemption_reason_id:
            g["reasons"].add(line.vat_exemption_reason.code)
    breakdown, total_vat, total_vat_sar = [], ZERO, ZERO
    for (category, rate), g in sorted(groups.items()):
        vat = _round(g["taxable"] * rate / HUNDRED)
        vat_sar = _round(vat * sar_rate) if sar_rate is not None else vat
        breakdown.append({"category": category, "rate": str(rate), "taxable": str(g["taxable"]), "vat": str(vat),
                          "vat_sar": str(vat_sar), "exemption_reasons": sorted(g["reasons"])})
        total_vat += vat
        total_vat_sar += vat_sar
    total_net = sum((line.net_amount for line in lines), ZERO)
    return breakdown, total_net, total_vat, total_vat_sar


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
    scheme, value = ids[0] if ids else ("", "")
    if doc.zatca_profile == SalesDocument.Profile.SIMPLIFIED or snap["buyer_vat_number"]:
        scheme, value = "", ""  # R50: simplified documents store no buyer ID; a VAT number replaces it (BR-KSA-14)
    if doc.flag_export:
        snap["buyer_vat_number"] = ""  # BR-KSA-46: no buyer VAT number on an export invoice
    snap.update(buyer_other_id_scheme=scheme, buyer_other_id=value)
    return snap


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
    elif not registered and not snap["buyer_other_id"]:
        problems.append("Buyer ID (TIN, CRN, ... ) is missing for a buyer without a VAT number. / "
                        "هوية المشتري ناقصة لمشترٍ ليس له رقم ضريبي.")
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
        problems += [f"Line {line.line_no}: {p}" for p in _account_problems(
            entity, line.income_account, {Account.Type.INCOME}, "income")]
        problems += [f"Line {line.line_no}: {p}" for p in _line_vat_problems(line, on)]
        if not line.description_ar.strip():
            problems.append(f"Line {line.line_no}: the Arabic description is required. / وصف البند بالعربية مطلوب.")
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
            _price_line(line, on)  # supply date changed after the line was added: priced again at issue
    breakdown, net, vat, _ = _sales_breakdown(doc, lines)
    gross = net + vat
    if doc.zatca_profile == SalesDocument.Profile.STANDARD:
        problems += _buyer_problems(doc, _buyer_snapshot(doc))
    if doc.doc_type in (SalesDocument.DocType.CREDIT_NOTE, SalesDocument.DocType.DEBIT_NOTE):
        problems += _note_problems(doc, gross)
    if check_period:
        period = _open_period(entity, doc.issue_date)
        if period:
            problems.append(period)
    return problems


def _simplified_problems(doc, gross_sar):
    """R48 (OQ-19, OQ-44 default): simplified only below SAR 1,000 gross, or for individual customers."""
    if (doc.zatca_profile == SalesDocument.Profile.SIMPLIFIED and doc.customer.customer_kind != Customer.Kind.INDIVIDUAL
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


def _note_warnings(doc):
    """R71 (OQ-47): a note should be issued by the 15th of the month after the adjustment event; warning only."""
    if doc.doc_type == SalesDocument.DocType.INVOICE or not doc.adjustment_event_date:
        return []
    e = doc.adjustment_event_date
    year, month = (e.year + 1, 1) if e.month == 12 else (e.year, e.month + 1)
    deadline = date_type(year, month, 15)
    if doc.issue_date > deadline:
        return [f"This note is issued after {deadline}, the 15th of the month after the adjustment event (TIR §7.2). / "
                f"يصدر هذا الإشعار بعد {deadline}، أي بعد اليوم الخامس عشر من الشهر التالي لحدث التعديل."]
    return []


def approve_sales_document(document, *, user):
    """CFO approval issues a submitted document (R57): number, snapshots and totals in one transaction, then the
    ZATCA hand-off after commit (R40). The approver is never the creator, submitter or last modifier: approvals
    that post are always segregated (access R35, OQ-19), whatever ``segregation_required`` says.

    Returns the document; ``document.warnings`` lists R71 warnings (bilingual), which do not block.
    """
    doc = SalesDocument.objects.select_related("entity", "customer").get(pk=document.pk)
    grant = access.require(user, doc.entity, "invoicing.approve_sales_document", document=doc)
    with transaction.atomic():
        doc = SalesDocument.objects.select_for_update(of=("self",)).select_related("entity", "customer").get(pk=doc.pk)
        if doc.status != Status.SUBMITTED:
            raise error("Only submitted documents are approved.", "تُعتمد المستندات المقدمة فقط.", code="not_submitted")
        problems = _issue_problems(doc, user=user)
        if problems:
            raise _problems_error(problems, "document_invalid")
        entity = doc.entity
        lines = list(doc.lines.select_related("vat_rate", "vat_exemption_reason", "income_account", "income_type"))
        on = doc.supply_date or doc.issue_date
        fx_rate, rate_date, rate_source = _fx_for(entity, doc.currency_id, doc.issue_date, user)
        sar_rate = _sar_rate(entity, doc.currency_id, fx_rate, doc.issue_date, user)
        for line in lines:
            _price_line(line, on)
            line._service_write = True
            line.save()
        breakdown, net, vat, vat_sar = _sales_breakdown(doc, lines, sar_rate)
        gross = net + vat
        gross_sar = _round(gross * sar_rate)
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
        doc.issue_time = timezone.localtime().time().replace(microsecond=0)
        doc.status = Status.ISSUED
        doc.approved_by, doc.approved_at = user, timezone.now()
        doc._service_write = True
        doc.save()
        access.use_grant(grant, target=doc)  # a used, current approval: the clearance posting checks it (access R42)
        port = zatca_port.get_port()
        transaction.on_commit(lambda: port.submit_document(SalesDocument.objects.get(pk=doc.pk), user))
    doc.warnings = _note_warnings(doc)
    return doc


# --- ZATCA result and posting on clearance (R41 to R44) ------------------------------------------

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


def _require_posting_context(document):
    user = _posting_user.get()
    if user is None:
        raise error(f"{document._meta.label} posts only through invoicing.services.",
                    f"يُرحَّل المستند {document._meta.label} من خلال خدمات الفوترة فقط.", code="post_through_services")
    return user


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


def _with_settlements(entity, on, currency, plain, settlements, user):
    """Lines for an entry that settles earlier items (ledger-fx R18) in a foreign currency.

    ``plain``: line dicts in the document currency (no functional amounts).
    ``settlements``: (item JournalLine, transaction amount, description). Each
    item is cleared at its carrying rate through ``ledger.services.fx_settlement_lines``;
    plain lines get their functional amounts at the entry date's rate; the
    difference goes to the realised FX gain (4700) or loss (5750) line.
    In the functional currency no settlement lines are needed: plain lines only.
    """
    functional = entity.base_currency_id
    if not settlements:
        return plain
    if currency == functional:
        out = list(plain)
        for item, amount, description in settlements:
            side = "credit" if item.debit > 0 else "debit"
            out.append(_line(item.account, side, amount, functional, functional, item.counterparty_entity, description))
        return out
    rate = ledger.get_rate(entity, currency, functional, on, user=user).rate
    out, debit, credit = [], ZERO, ZERO
    for spec in plain:
        side = "debit" if "txn_debit" in spec else "credit"
        amount = fx.convert(spec[f"txn_{side}"], rate)
        spec = {**spec, side: amount}
        out.append(spec)
        if side == "debit":
            debit += amount
        else:
            credit += amount
    for item, amount, description in settlements:
        built = ledger.fx_settlement_lines(entity, on, item, amount, item.account, user=user, description=description)
        counter_func = fx.convert(amount, rate)
        fx_line = next((b for b in built if b.get("line_kind") == "realised_fx"), None)
        gain = (fx_line.get("credit") or ZERO) - (fx_line.get("debit") or ZERO) if fx_line else ZERO
        item_debit = item.debit > 0
        # fx_settlement_lines: realised gain = counter - settled for a debit item, settled - counter for a credit item.
        settled = counter_func - gain if item_debit else counter_func + gain
        settle = built[1]
        out.append(settle)
        if item_debit:
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
    user = _require_posting_context(document)
    entity, cur, functional = document.entity, document.currency_id, document.entity.base_currency_id
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
    return _with_settlements(entity, document.issue_date, cur, plain, settlements, user)


def _check_document_rate(document, lines):
    """Registered ``validate_lines``: the ledger rated the document's lines at the rate stored on the document."""
    functional = document.entity.base_currency_id
    problems = []
    for line in lines:
        if line.currency_id != functional and line.line_kind == "normal" and line.fx_rate != document.fx_rate:
            problems.append(
                f"Line {line.account.code} is rated {line.fx_rate}; the document was approved at {document.fx_rate}. "
                f"Return it to draft and approve it again. / سعر البند {line.fx_rate} يختلف عن سعر الاعتماد "
                f"{document.fx_rate}؛ أعده إلى مسودة واعتمده من جديد."
            )
    return problems


def _pending_sales_documents(entity, start_date, end_date):
    """Registered ``pending_documents``: issued documents awaiting ZATCA in the range (R43, AT-32)."""
    return [d.number for d in SalesDocument.objects.filter(entity=entity, status=Status.ISSUED,
                                                           issue_date__gte=start_date, issue_date__lte=end_date)]


# --- open amounts and allocation (R34 to R38) -------------------------------------------------------

def _sum(qs):
    return qs.aggregate(t=Sum("amount"))["t"] or ZERO


def _open_receivable(document):
    """Open amount of a final invoice or debit note: gross minus allocations to it."""
    return document.total_gross - _sum(ReceivableAllocation.objects.filter(target=document))


def _open_payable(document):
    return document.total_gross - _sum(PayableAllocation.objects.filter(target=document))


def _available(source):
    if isinstance(source, CustomerReceipt):
        return source.amount - _sum(ReceivableAllocation.objects.filter(receipt=source))
    if isinstance(source, SalesDocument):
        return source.total_gross - _sum(ReceivableAllocation.objects.filter(credit_note=source))
    if isinstance(source, VendorPayment):
        return source.amount - _sum(PayableAllocation.objects.filter(payment=source))
    return source.total_gross - _sum(PayableAllocation.objects.filter(vendor_credit_note=source))


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
    amount = _money(amount, "allocation amount", positive=True)
    sales = isinstance(target, SalesDocument)
    with transaction.atomic():
        model_t = SalesDocument if sales else PurchaseDocument
        target = model_t.objects.select_for_update(of=("self",)).select_related("entity").get(pk=target.pk)
        source = type(source).objects.select_for_update(of=("self",)).select_related("entity").get(pk=source.pk)
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
            vendor_total_vat=_money(vendor_total_vat, "vendor VAT"),
            vendor_total_gross=_money(vendor_total_gross, "vendor total"),
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
            value = _money(value, name)
        setattr(doc, name, value)
    _touch(doc, user)
    doc.save()
    return doc


@transaction.atomic
def add_purchase_line(document, *, user, account, quantity, unit_price, vat_category=VatCategory.STANDARD,
                      vat_attribution=PurchaseDocumentLine.Attribution.TAXABLE, supply_kind="", description_ar="",
                      description_en="", line_discount="0", unit_code="C62"):
    """Add a line to a draft bill (R5 Sharia, R29 attribution, R30 supply kind for non-resident vendors)."""
    doc = _draft_purchase(document, user)
    entity = doc.entity
    last = doc.lines.order_by("-line_no").values_list("line_no", flat=True).first() or 0
    line = PurchaseDocumentLine(entity=entity, document=doc, line_no=last + 1)
    line.account = _account(entity, account)
    line.quantity = _decimal(quantity, "quantity")
    if not fx.has_places(line.quantity, 3) or line.quantity <= ZERO:
        raise error("Quantity must be above zero with at most 3 decimals.", "يجب أن تكون الكمية أكبر من صفر.",
                    code="quantity")
    line.unit_price = _money(unit_price, "unit price")
    line.line_discount = _money(line_discount, "line discount")
    line.unit_code = unit_code or "C62"
    line.description_ar, line.description_en = (description_ar or "").strip(), (description_en or "").strip()
    line.vat_category, line.vat_attribution, line.supply_kind = vat_category, vat_attribution, supply_kind or ""
    problems = _account_problems(entity, line.account, {Account.Type.EXPENSE, Account.Type.ASSET}, "expense or asset")
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
    _price_line(line, doc.accounting_date)
    line.save()
    _touch(doc, user)
    doc.save(update_fields=["modified_by", "modified_at", "updated_at"])
    return line


def _purchase_amounts(doc, lines):
    """R29 / R30 and OQ-11: VAT per category at document level; each line's cost and recoverable VAT.

    Returns (breakdown, net, charged_vat, rc_vat, costs {line_no: cost}, recoverable_vat, recoverable line numbers).
    Resident vendor: the vendor charges VAT; it is recoverable (1310) only for ``taxable`` lines with a vendor VAT
    number, otherwise it is added to the line's cost (``exempt``; ``mixed`` fully to cost until OQ-9 is signed off).
    Non-resident vendor: no VAT charged; standard-rated services are reverse charged (Dr 1310 or cost / Cr 2200);
    goods are not (import VAT at customs, OQ-29).
    """
    resident = doc.vendor.is_resident
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
        vat = _round(taxable * rate / HUNDRED)
        breakdown.append({"category": category, "rate": str(rate), "taxable": str(taxable), "vat": str(vat),
                          "reverse_charge": not resident})
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
    try:
        _currency(doc.currency_id, entity)
    except InvoicingError as exc:
        problems += exc.messages
    for line in lines:
        problems += [f"Line {line.line_no}: {p}" for p in _account_problems(
            entity, line.account, {Account.Type.EXPENSE, Account.Type.ASSET}, "expense or asset")]
        _price_line(line, doc.accounting_date)  # in memory: the rate effective on the accounting date (R10)
        if line.vat_rate is None:
            problems.append(f"Line {line.line_no}: no VAT rate for category {line.vat_category} on {doc.accounting_date}. / "
                            "لا توجد نسبة ضريبة لهذا البند.")
    if problems:
        return problems
    _, net, charged, _, _, _, _ = _purchase_amounts(doc, lines) if lines else (None, ZERO, ZERO, None, None, None, None)
    if lines and (doc.vendor_total_vat != charged or doc.vendor_total_gross != net + charged):  # OQ-11: exact match
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
    doc.status = PStatus.SUBMITTED
    doc.submitted_by, doc.submitted_at = user, timezone.now()
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
    with transaction.atomic():
        doc = PurchaseDocument.objects.select_for_update(of=("self",)).select_related("entity", "vendor").get(pk=doc.pk)
        if doc.status != PStatus.SUBMITTED:
            raise error("Only submitted documents are approved.", "تُعتمد المستندات المقدمة فقط.", code="not_submitted")
        problems = _purchase_problems(doc)
        if problems:
            raise _problems_error(problems, "document_invalid")
        entity = doc.entity
        lines = list(doc.lines.select_related("account", "vat_rate"))
        for line in lines:
            _price_line(line, doc.accounting_date)
        breakdown, net, charged, rc_vat, costs, recoverable, recoverable_lines = _purchase_amounts(doc, lines)
        for line in lines:
            line.vat_recoverable = line.line_no in recoverable_lines  # computed from R29 / R30, never user-set
            line._service_write = True
            line.save()
        doc.vat_breakdown = breakdown
        doc.total_net, doc.total_vat, doc.total_gross, doc.reverse_charge_vat = net, charged, net + charged, rc_vat
        doc.fx_rate, doc.fx_rate_date, doc.fx_rate_source = _fx_for(entity, doc.currency_id, doc.accounting_date, user)
        doc.is_intercompany = doc.vendor.group_entity_id is not None
        doc.number = _next_number(entity, doc.doc_type, doc.accounting_date.year)
        doc.status = PStatus.POSTED
        doc.approved_by, doc.approved_at = user, timezone.now()
        doc._service_write = True
        doc.save()
        targets = []
        if doc.doc_type == PurchaseDocument.DocType.VENDOR_CREDIT_NOTE:
            targets = list(PurchaseDocument.objects.select_for_update(of=("self",)).filter(pk=doc.original_document_id))
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
    user = _require_posting_context(document)
    entity, cur, functional = document.entity, document.currency_id, document.entity.base_currency_id
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
    return _with_settlements(entity, document.accounting_date, cur, plain, settlements, user)


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
    """Record a draft customer receipt or refund. A second person posts it (R33).

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
    amount = _money(amount, "amount", positive=True)
    charge = _money(bank_charge, "bank charge")
    if charge and (kind != CustomerReceipt.Kind.RECEIPT or charge >= amount):
        raise error("A bank charge is below the received amount, on receipts only.",
                    "الرسوم البنكية أقل من المبلغ المقبوض، وفي سندات القبض فقط.", code="bank_charge")
    with transaction.atomic():
        receipt = CustomerReceipt(
            entity=entity, customer=customer, kind=kind, purpose=purpose, date=date, amount=amount, bank_charge=charge,
            currency=_currency(currency or customer.currency_id, entity), bank_account=_bank_account(entity, bank_account),
            reference_en=reference_en, reference_ar=reference_ar, bank_reference=bank_reference, created_by=user,
            source=_source(user),
        )
        receipt.save()
    return receipt


def _application_problems(source, target, amount, receivable):
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
    if amount > open_:
        problems.append(f"{amount} is more than the open amount {open_}. / {amount} أكبر من المبلغ المفتوح {open_}.")
    return problems


@transaction.atomic
def add_receipt_application(receipt, target, amount, *, user):
    """Plan which invoices a draft receipt settles; allocations are made when it posts (R34, ledger-fx R18)."""
    receipt = CustomerReceipt.objects.select_for_update(of=("self",)).select_related("entity").get(pk=receipt.pk)
    _require_receipt_staff(user, receipt.entity)
    if receipt.status != "draft" or receipt.kind != CustomerReceipt.Kind.RECEIPT:
        raise error("Only draft receipts get applications.", "تُضاف التخصيصات إلى مسودات سندات القبض فقط.", code="not_draft")
    amount = _money(amount, "amount", positive=True)
    problems = _application_problems(receipt, target, amount, receivable=True)
    planned = _sum(receipt.applications.all())
    if planned + amount > receipt.amount:
        problems.append("Applications exceed the receipt amount. / التخصيصات تتجاوز مبلغ السند.")
    if problems:
        raise _problems_error(problems, "application_invalid")
    app = ReceiptApplication(entity=receipt.entity, receipt=receipt, target=target, amount=amount)
    app.save()
    _touch(receipt, user)
    receipt._service_write = True
    receipt.save(update_fields=["modified_by", "modified_at", "updated_at"])
    return app


def _post_money(movement, *, user, grant, receivable):
    """Shared posting of a receipt or payment: lock, check, number, book, allocate (R26, R28, R33, R59)."""
    model = CustomerReceipt if receivable else VendorPayment
    target_model = SalesDocument if receivable else PurchaseDocument
    movement = model.objects.select_for_update(of=("self",)).select_related("entity").get(pk=movement.pk)
    entity = movement.entity
    expected = "draft" if receivable else "submitted"
    if movement.status != expected:
        raise error(f"Only {expected} documents are posted here.", "لا يُرحَّل هذا المستند في حالته الحالية.",
                    code="wrong_status")
    apps = list(movement.applications.select_related("target").order_by("pk"))
    targets = {t.pk: t for t in target_model.objects.select_for_update(of=("self",)).filter(pk__in=[a.target_id for a in apps])}
    problems = []
    for app in apps:
        problems += _application_problems(movement, targets[app.target_id], app.amount, receivable)
    if sum((a.amount for a in apps), ZERO) > movement.amount:
        problems.append("Applications exceed the amount. / التخصيصات تتجاوز المبلغ.")
    period = _open_period(entity, movement.date)
    if period:
        problems.append(period)
    if problems:
        raise _problems_error(problems, "document_invalid")
    movement.fx_rate, movement.fx_rate_date, movement.fx_rate_source = _fx_for(entity, movement.currency_id,
                                                                              movement.date, user)
    movement.number = _next_number(entity, "RCPT" if receivable else "PAY", movement.date.year)
    movement.status = "posted"
    movement.posted_by, movement.posted_at = user, timezone.now()
    movement._service_write = True
    movement.save()
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
    """Post a draft receipt (``invoicing.post_receipt``) by someone other than its creator or last modifier:
    the ledger refuses a document posting by them (access R35; OQ-49 default, also when segregation is off)."""
    receipt = CustomerReceipt.objects.select_related("entity").get(pk=receipt.pk)
    grant = access.require(user, receipt.entity, "invoicing.post_receipt", document=receipt)
    with transaction.atomic():
        return _post_money(receipt, user=user, grant=grant, receivable=True)


def _receipt_lines(document):
    """Registered ``build_lines`` for CustomerReceipt: E5 / E6, settling foreign items at the carrying rate."""
    user = _require_posting_context(document)
    entity, cur, functional = document.entity, document.currency_id, document.entity.base_currency_id
    control, counterparty = _control(document.customer, receivable=True)
    label = document.number
    if document.kind == CustomerReceipt.Kind.REFUND:  # E6
        return [_line(control, "debit", document.amount, cur, functional, counterparty, label),
                _line(document.bank_account, "credit", document.amount, cur, functional, description=label)]
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
    return _with_settlements(entity, document.date, cur, plain, settlements, user)


def _no_payment_without_wht(vendor, kind):
    """D14 / R61 to R64: payments to non-resident vendors carry withholding tax, which is not built in slice 1."""
    if not vendor.is_resident and kind == VendorPayment.Kind.PAYMENT:
        raise error("Payments to non-resident vendors wait for withholding tax (R61 to R64, not built yet).",
                    "المدفوعات للموردين غير المقيمين بانتظار ضريبة الاستقطاع (غير مبنية بعد).", code="wht_not_built")


def create_vendor_payment(entity, *, user, vendor, date, amount, currency=None, bank_account=None,
                          kind=VendorPayment.Kind.PAYMENT, reference_en="", reference_ar="", bank_reference=""):
    """Draft vendor payment or refund received (``invoicing.create_vendor_payment``; agents never, catalogue)."""
    access.require(user, entity, "invoicing.create_vendor_payment")
    _same_entity(entity, vendor, "vendor", "المورد")
    _no_payment_without_wht(vendor, kind)
    if kind not in VendorPayment.Kind.values:
        raise error("Unknown payment kind.", "نوع سند غير معروف.", code="kind")
    with transaction.atomic():
        payment = VendorPayment(
            entity=entity, vendor=vendor, kind=kind, date=date, amount=_money(amount, "amount", positive=True),
            currency=_currency(currency or vendor.currency_id, entity), bank_account=_bank_account(entity, bank_account),
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
    amount = _money(amount, "amount", positive=True)
    problems = _application_problems(payment, target, amount, receivable=False)
    if _sum(payment.applications.all()) + amount > payment.amount:
        problems.append("Applications exceed the payment amount. / التخصيصات تتجاوز مبلغ السند.")
    if problems:
        raise _problems_error(problems, "application_invalid")
    app = PaymentApplication(entity=payment.entity, payment=payment, target=target, amount=amount)
    app.save()
    _touch(payment, user)
    payment._service_write = True
    payment.save(update_fields=["modified_by", "modified_at", "updated_at"])
    return app


@transaction.atomic
def submit_vendor_payment(payment, *, user):
    payment = VendorPayment.objects.select_for_update(of=("self",)).select_related("entity").get(pk=payment.pk)
    access.require(user, payment.entity, "invoicing.submit_vendor_payment")
    if payment.status != "draft":
        raise error("Only drafts can be submitted.", "يمكن تقديم المسودات فقط.", code="not_draft")
    payment.status = "submitted"
    payment.submitted_by, payment.submitted_at = user, timezone.now()
    payment._service_write = True
    payment.save()
    return payment


def approve_vendor_payment(payment, *, user):
    """CFO approval posts a submitted vendor payment at once (R59); refused postings roll back (no number used)."""
    payment = VendorPayment.objects.select_related("entity").get(pk=payment.pk)
    grant = access.require(user, payment.entity, "invoicing.approve_vendor_payment", document=payment)
    _no_payment_without_wht(payment.vendor, payment.kind)
    with transaction.atomic():
        payment = _post_money(payment, user=user, grant=grant, receivable=False)
        payment.approved_by, payment.approved_at = user, payment.posted_at
        payment._service_write = True
        payment.save(update_fields=["approved_by", "approved_at", "updated_at"])
        return payment


def _payment_lines(document):
    """Registered ``build_lines`` for VendorPayment: E11, settling foreign bills at the carrying rate."""
    user = _require_posting_context(document)
    entity, cur, functional = document.entity, document.currency_id, document.entity.base_currency_id
    control, counterparty = _control(document.vendor, receivable=False)
    label = document.number
    if document.kind == VendorPayment.Kind.REFUND_RECEIVED:
        return [_line(document.bank_account, "debit", document.amount, cur, functional, description=label),
                _line(control, "credit", document.amount, cur, functional, counterparty, label)]
    plain = [_line(document.bank_account, "credit", document.amount, cur, functional, description=label)]
    settlements, applied = [], ZERO
    for app in document.applications.select_related("target").order_by("pk"):
        settlements.append((_control_line(app.target), app.amount, f"{label} → {app.target.number}"))
        applied += app.amount
    if cur == functional:
        plain.append(_line(control, "debit", document.amount, cur, functional, counterparty, label))
        return plain
    if document.amount - applied:
        plain.append(_line(control, "debit", document.amount - applied, cur, functional, counterparty, label))
    return _with_settlements(entity, document.date, cur, plain, settlements, user)


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

    for d in SalesDocument.objects.filter(entity=entity, status=Status.FINAL, issue_date__lte=as_of).select_related(
            "customer__group_entity"):
        sign = -1 if d.doc_type == SalesDocument.DocType.CREDIT_NOTE else 1
        add(*ctrl(d.customer, True), d.currency_id, sign * d.total_gross)
    for r in CustomerReceipt.objects.filter(entity=entity, status="posted", date__lte=as_of).select_related(
            "customer__group_entity"):
        add(*ctrl(r.customer, True), r.currency_id, r.amount if r.kind == CustomerReceipt.Kind.REFUND else -r.amount)
    for d in PurchaseDocument.objects.filter(entity=entity, status=PStatus.POSTED, accounting_date__lte=as_of
                                             ).select_related("vendor__group_entity"):
        sign = 1 if d.doc_type == PurchaseDocument.DocType.VENDOR_CREDIT_NOTE else -1
        add(*ctrl(d.vendor, False), d.currency_id, sign * d.total_gross)
    for p in VendorPayment.objects.filter(entity=entity, status="posted", date__lte=as_of).select_related(
            "vendor__group_entity"):
        add(*ctrl(p.vendor, False), p.currency_id, -p.amount if p.kind == VendorPayment.Kind.REFUND_RECEIVED else p.amount)
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


# --- billing hooks (R60, R73): not built in slice 1 ---------------------------------------------------

def create_from_billing(billing_item, user):
    """R60. Not built: the billing models (billing.md) do not exist yet. Always raises."""
    raise NotImplementedError(
        "create_from_billing waits for the billing module (billing.md); not built in invoicing slice 1. / "
        "إنشاء الفواتير من الفوترة بانتظار وحدة الفوترة؛ غير مبني في المرحلة الأولى."
    )


def create_adjustment_from_billing(adjustment, user):
    """R73. Not built: ``BillingAdjustment`` (billing.md 3.10) does not exist yet. Always raises."""
    raise NotImplementedError(
        "create_adjustment_from_billing waits for the billing module (billing.md); not built in invoicing slice 1. / "
        "إنشاء الإشعارات من تعديلات الفوترة بانتظار وحدة الفوترة؛ غير مبني في المرحلة الأولى."
    )
