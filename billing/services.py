"""Billing services (docs/specs/billing.md). The only supported way to change billing data.

Every public function checks rights with ``core.access.require`` for the entity that owns the record (access R1).
Billing books nothing: invoices and correction notes are created through ``billing.invoicing_port`` (invoicing
services, R13, R43) by the billing system user, and invoicing posts them after ZATCA (§5).

Workflow: accounting staff draft contracts and versions; the entity's CFO approves (never the creator, submitter
or last modifier, R8, R39); approval builds the schedule; the daily run (``billing_run``) issues due periods as
the billing system user (R11, R52, R66); approved versions and secondment ends recompute already-billed periods
into draft correction notes that a person reviews and the CFO approves in invoicing (R42 to R49).
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from core import access
from core.access import services as access_services
from core.access.models import AccessLog, Kind, Membership
from core.models import Currency, Entity
from ledger.models import Account, FiscalPeriod

from . import calc, invoicing_port
from .models import (
    AdvanceTerm,
    BillingAdjustment,
    BillingError,
    BillingNotification,
    BillingPeriod,
    BillingRun,
    BillingRunItem,
    Contract,
    ContractEvent,
    ContractLine,
    ContractPriceStep,
    ContractSequence,
    ContractType,
    ContractVersion,
    DepositTerm,
    Frequency,
    SecondmentAssignment,
    VatCategory,
    error,
)

ZERO = Decimal("0")
RIYADH = ZoneInfo("Asia/Riyadh")
LOCK_CLASS = 0x42494C4C  # "BILL": first key of the per-entity PostgreSQL advisory lock (R11, R41)
SKIP_LOOKBACK_DAYS = 31  # generated periods due this recently get a skipped_already_billed run item (AT-2)
ESCALATE_AFTER_RUNS = 3  # R34 / OQ-17 default
CONTROL_ACCOUNTS = frozenset({"1200", "1210", "2100", "2110", "2200", "1310"})
INCOME_TYPE_EXEMPT = "E"

CStatus = Contract.Status
VStatus = ContractVersion.Status
PStatus = BillingPeriod.Status
AStatus = BillingAdjustment.Status
Event = ContractEvent.Action

# Invoicing error codes that put a period on hold for a person (R54); every other refusal is retried next run.
HELD_CODES = frozenset({"third_decimal_pending", "entity_not_vat_registered"})

MESSAGES = {
    "generated": ("Invoice {number} created.", "أُنشئت الفاتورة {number}."),
    "already_billed": ("Already billed ({number}); nothing created.", "مفوترة مسبقًا ({number})؛ لم يُنشأ شيء."),
    "period_closed": ("No open fiscal period covers the run date {day} (period closed or missing); retried next run.",
                      "لا توجد فترة مالية مفتوحة تغطي تاريخ الدورة {day} (الفترة مقفلة أو غير موجودة)؛ يُعاد في الدورة التالية."),
    "supply_closed": ("The supply period {start} to {end} lies wholly in a closed fiscal period; a person must "
                      "release it (R23, OQ-13).",
                      "تقع فترة التوريد من {start} إلى {end} كلها في فترة مالية مقفلة؛ يجب أن يطلقها شخص."),
    "third_decimal": ("3-decimal amounts await ZATCA confirmation (invoicing OQ-62); released by a person.",
                      "المبالغ ذات الخانات العشرية الثلاث بانتظار تأكيد الهيئة؛ يطلقها شخص."),
    "not_vat_registered": ("The entity has no VAT number; its invoices wait for tax sign-off (invoicing R94, OQ-34).",
                           "ليس للكيان رقم ضريبي؛ تنتظر فواتيره اعتماد المستشار الضريبي."),
    "rate_required": ("No exchange rate for the invoice date; retried next run (R32).",
                      "لا يوجد سعر صرف لتاريخ الفاتورة؛ يُعاد في الدورة التالية."),
    "permission_denied": ("The billing system user may not issue in this entity; retried next run.",
                          "لا يجوز لمستخدم نظام الفوترة الإصدار في هذا الكيان؛ يُعاد في الدورة التالية."),
    "invoicing_not_built": ("Invoicing cannot create invoices from billing yet (create_from_billing is not built).",
                            "لا يمكن لوحدة الفوترة إنشاء الفواتير من العقود بعد."),
    "nothing_billable": ("Nothing is billable in this period.", "لا يوجد ما يُفوتر في هذه الفترة."),
    "no_membership": ("The billing system user has no membership in {entity}; the entity was skipped (access R20).",
                      "ليست لمستخدم نظام الفوترة عضوية في {entity}؛ تم تخطي الكيان."),
    "zatca_rejected": ("ZATCA rejected invoice {number}; fix the cause and release the period (R12).",
                       "رفضت الهيئة الفاتورة {number}؛ عالج السبب ثم أطلق الفترة."),
    "run_in_progress": ("A billing run for {entity} is in progress; retry in a moment (R41).",
                        "دورة فوترة للكيان {entity} قيد التنفيذ؛ أعد المحاولة بعد قليل."),
    "draft_failed": ("The draft note could not be created yet: {reason}", "تعذر إنشاء مسودة الإشعار بعد: {reason}"),
    "run_summary": ("Billing run {day} for {entity}: {generated} generated, {held} held, {failed} failed.",
                    "دورة الفوترة {day} للكيان {entity}: صدرت {generated}، علّقت {held}، تعثرت {failed}."),
    "escalation": ("{count} billing period(s) of {entity} failed in {runs} consecutive runs.",
                   "تعثرت {count} من فترات الفوترة للكيان {entity} في {runs} دورات متتالية."),
    "corrections": ("Contract {contract}: {created} correction(s) created ({waiting} waiting for the original invoice), "
                    "{superseded} superseded.",
                    "العقد {contract}: أُنشئ {created} من التصحيحات ({waiting} بانتظار الفاتورة الأصلية)، وحلّ محل {superseded}."),
    "three_dp_warning": ("Invoices in {currency} whose amounts have a third decimal other than 0 will be held until "
                         "ZATCA confirms 3-decimal amounts (R37, invoicing OQ-62).",
                         "ستُعلَّق فواتير {currency} التي خانتها العشرية الثالثة ليست صفرًا حتى تؤكد الهيئة قبول ثلاث خانات."),
    "linked_manual_end": ("This secondment is ended in HR.", "تُنهى هذه الإعارة في نظام الموارد البشرية."),
    "not_visible": ("not visible", "غير ظاهر"),
}


def _msg(code, **fmt):
    en, ar = MESSAGES[code]
    return en.format(**fmt), ar.format(**fmt)


def _fail(en, ar, code):
    raise error(en, ar, code=code)


def _problems(problems, code="invalid"):
    exc = BillingError(list(problems), code=code)
    exc.code = code
    return exc


def _split(text):
    """English and Arabic halves of an "English / العربية" message."""
    en, sep, ar = str(text).partition(" / ")
    return (en, ar) if sep else (str(text), str(text))


def _today():
    """Today in Asia/Riyadh (R15, R66); a function so tests can move the clock."""
    return timezone.now().astimezone(RIYADH).date()


def _source(user):
    return access_services.source_of(user) or "human"


def _save(obj, **kwargs):
    obj._service_write = True
    obj.save(**kwargs)
    return obj


def _event(contract, action, actor, **detail):
    return _save(ContractEvent(entity=contract.entity, contract=contract, action=action, actor=actor,
                               detail={k: (str(v) if isinstance(v, (date, Decimal)) else v) for k, v in detail.items()}))


def _system_user():
    return access.system_user("billing")


def _refuse_agent(user, entity, permission):
    """Agents create drafts only (R36, gateway rules); refused for anything else with a logged denial."""
    if access_services.kind_of(user) == Kind.AGENT:
        access.deny(user, entity, permission, "agent_forbidden")


def _refuse_non_human(user, entity, permission):
    if access_services.kind_of(user) == Kind.AGENT:
        access.deny(user, entity, permission, "agent_forbidden")
    if access_services.kind_of(user) == Kind.SYSTEM:
        access.deny(user, entity, permission, "system_forbidden")


# --- locks -------------------------------------------------------------------------------------------

def _entity_lock_now(entity):
    """R41: approvals and secondment ends take the entity's billing lock in their transaction; a run holding it
    makes them fail with a bilingual "retry" (AT-27), so no period is generated with old terms meanwhile."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", [LOCK_CLASS, entity.pk])
        if not cursor.fetchone()[0]:
            raise error(*_msg("run_in_progress", entity=entity.code), code="run_in_progress")


def _run_lock(entity):
    """R11: one runner per entity at a time; a second run waits for the first (session lock, AT-3)."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s, %s)", [LOCK_CLASS, entity.pk])


def _run_unlock(entity):
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_CLASS, entity.pk])


# --- values and validation -----------------------------------------------------------------------------

def _places(currency):
    return currency.minor_units


def _amount(value, what_en, what_ar, places):
    """A positive Decimal with at most ``places`` decimals; floats are refused, never converted (R16, R37)."""
    if isinstance(value, float):
        _fail(f"{what_en} is a float; give a Decimal or a string.", f"{what_ar} عدد عشري ثنائي؛ أدخله كنص أو Decimal.",
              "float_rejected")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        _fail(f"{what_en} is not a number.", f"{what_ar} ليس رقمًا.", "amount")
    if not amount.is_finite() or amount <= ZERO:
        _fail(f"{what_en} must be above zero.", f"يجب أن يكون {what_ar} أكبر من صفر.", "amount")
    if not calc.has_places(amount, places):
        _fail(f"{what_en} has more than {places} decimals for this currency.",
              f"{what_ar} يتجاوز {places} خانات عشرية لهذه العملة.", "amount_decimals")
    return amount


def _currency(value):
    code = getattr(value, "code", value)
    cur = Currency.objects.filter(code=code).first()
    if cur is None or not cur.is_active:
        _fail(f"Currency {code} is not an active currency.", f"العملة {code} ليست عملة نشطة.", "currency")
    return cur


def _currency_problems(cur):
    if cur.minor_units == 0:  # R37 / R68c: 0-decimal currencies are refused (invoicing OQ-58)
        return [f"Contracts in {cur.code} (0 decimals) are not supported until invoicing supports 0-decimal "
                f"currencies (invoicing OQ-58). / العقود بعملة {cur.code} (بلا خانات عشرية) غير مدعومة بعد."]
    if cur.minor_units > 3:
        return [f"Contracts in {cur.code} ({cur.minor_units} decimals) are not supported. / "
                f"العقود بعملة {cur.code} غير مدعومة."]
    return []


def _contract_problems(c):
    """R1, R3, R37 and the 3.1 required fields."""
    problems = []
    if c.customer.entity_id != c.entity_id:
        problems.append("The customer belongs to another entity. / العميل يخص كيانًا آخر.")
    if c.customer.group_entity_id == c.entity_id:
        problems.append("The customer is this entity itself. / العميل هو الكيان نفسه.")
    if not (c.title_ar or "").strip():
        problems.append("The Arabic title is required. / العنوان بالعربية مطلوب.")
    if not (c.description_ar or "").strip():
        problems.append("The Arabic description is required. / الوصف بالعربية مطلوب.")
    problems += _currency_problems(c.currency)
    if c.end_date is None and c.contract_type not in (ContractType.SECONDMENT, ContractType.SERVICE):
        problems.append("An end date is required except for secondments and services (OQ-9). / "
                        "تاريخ النهاية مطلوب إلا في الإعارات والخدمات.")
    if c.end_date is not None and c.end_date < c.start_date:
        problems.append("The end date is before the start date. / تاريخ النهاية قبل تاريخ البداية.")
    if c.contract_type == ContractType.LEASE:
        if c.lease_use not in ("residential", "commercial"):
            problems.append("A lease needs its use (residential or commercial). / يحتاج عقد الإيجار إلى نوع الاستخدام.")
        if not c.property_ref.strip() or not c.unit_ref.strip():
            problems.append("A lease needs the property and unit. / يحتاج عقد الإيجار إلى العقار والوحدة.")
    if c.contract_type == ContractType.VEHICLE_RENTAL and not c.vehicle_ref.strip():
        problems.append("A vehicle rental needs the vehicle (plate or asset code). / يحتاج تأجير السيارة إلى بياناتها.")
    return problems


def _line_problems(contract, line):
    """R1, R24, R25, R27, R29, R37 for one line (at save and again at approval)."""
    problems = []
    entity = contract.entity
    pre = f"Line {line.line_no}: "
    pre_ar = f"البند {line.line_no}: "
    account = line.income_account
    if line.entity_id != contract.entity_id:
        problems.append(f"{pre}belongs to another entity. / {pre_ar}يخص كيانًا آخر.")
    if account.entity_id != entity.pk:
        problems.append(f"{pre}account {account.code} belongs to another entity. / {pre_ar}الحساب {account.code} يخص كيانًا آخر.")
    else:
        if account.type != Account.Type.INCOME or not account.is_postable or not account.is_active:
            problems.append(f"{pre}account {account.code} is not a postable income account. / "
                            f"{pre_ar}الحساب {account.code} ليس حساب إيراد قابلًا للترحيل.")
        if account.code in CONTROL_ACCOUNTS:
            problems.append(f"{pre}account {account.code} is a control account. / {pre_ar}الحساب {account.code} حساب رقابة.")
        if account.sharia_prohibited and entity.sharia_enforced:
            problems.append(f"{pre}account {account.code} is Sharia-prohibited in {entity.code} (R29). / "
                            f"{pre_ar}الحساب {account.code} محظور شرعًا في {entity.code}.")
    if not (line.description_ar or "").strip():
        problems.append(f"{pre}the Arabic description is required. / {pre_ar}الوصف بالعربية مطلوب.")
    if isinstance(line.amount, float) or not isinstance(line.amount, Decimal) or line.amount <= ZERO \
            or not calc.has_places(line.amount, _places(contract.currency)):
        problems.append(f"{pre}the amount must be a positive Decimal with at most {_places(contract.currency)} decimals. / "
                        f"{pre_ar}يجب أن يكون المبلغ موجبًا وبحد أقصى {_places(contract.currency)} خانات عشرية.")
    reason = line.vat_exemption_reason
    if line.vat_category not in VatCategory.values:
        problems.append(f"{pre}unknown VAT category. / {pre_ar}فئة ضريبة غير معروفة.")
    elif line.vat_category == VatCategory.STANDARD:
        if reason is not None:
            problems.append(f"{pre}a standard-rated line has no exemption reason. / {pre_ar}البند الخاضع للنسبة الأساسية بلا سبب إعفاء.")
    elif reason is None or reason.category != line.vat_category:
        problems.append(f"{pre}category {line.vat_category} needs an exemption reason of that category. / "
                        f"{pre_ar}الفئة {line.vat_category} تحتاج إلى سبب إعفاء من الفئة نفسها.")
    if contract.contract_type == ContractType.LEASE:  # R24 [CC-verify]
        if contract.lease_use == "residential" and line.vat_category != VatCategory.EXEMPT:
            problems.append(f"{pre}a residential lease line is exempt (E) with an exemption reason (R24). / "
                            f"{pre_ar}بند الإيجار السكني معفى (E) مع سبب الإعفاء.")
        if contract.lease_use == "commercial" and line.vat_category != VatCategory.STANDARD:
            problems.append(f"{pre}a commercial lease line is standard rated (S) (R24). / "
                            f"{pre_ar}بند الإيجار التجاري خاضع للنسبة الأساسية (S).")
    if contract.contract_type == ContractType.SECONDMENT:
        if line.secondment_id is None:
            problems.append(f"{pre}a secondment line names its seconded person (3.2). / {pre_ar}بند الإعارة يحدد الموظف المعار.")
        elif line.secondment.contract_id != contract.pk:
            problems.append(f"{pre}the secondment belongs to another contract. / {pre_ar}الإعارة تخص عقدًا آخر.")
    elif line.secondment_id is not None:
        problems.append(f"{pre}only secondment contracts have secondment lines. / {pre_ar}بنود الإعارة لعقود الإعارة فقط.")
    return problems


def _gate_problems(contract, terms):
    """R68 build gates, checked at contract and version approval (AT-47)."""
    problems = []
    if terms.billing_timing == "advance" and terms.billing_frequency != Frequency.MONTHLY:
        problems.append("Billing in advance for more than one month needs deferred income, which is not built "
                        "(OQ-2). / الفوترة المقدمة لأكثر من شهر تحتاج إلى الإيرادات المؤجلة غير المبنية بعد.")
    if contract.advance_terms_id and contract.advance_terms.recovery == AdvanceTerm.Recovery.OFFSET:
        problems.append("An advance offset against invoices is blocked until the prepayment (386) flow exists "
                        "(R30, OQ-20). / خصم الدفعة المقدمة من الفواتير موقوف حتى يُبنى مسار الدفعات المقدمة.")
    problems += _currency_problems(contract.currency)
    return problems


def _resolve(model, value, what_en, what_ar, **filters):
    if value is None or isinstance(value, model):
        return value
    obj = model.objects.filter(code=value, **filters).first()
    if obj is None:
        _fail(f"Unknown {what_en} {value!r}.", f"{what_ar} {value!r} غير معروف.", "unknown_reference")
    return obj


# --- terms (R38) ------------------------------------------------------------------------------------

def _line_terms(line):
    attrs = {
        "line_no": line.line_no, "description_en": line.description_en, "description_ar": line.description_ar,
        "unit_code": line.unit_code, "vat_category": line.vat_category,
        "vat_exemption_reason": line.vat_exemption_reason.code if line.vat_exemption_reason_id else "",
        "income_type": line.income_type.code, "income_account": line.income_account.code,
    }
    window = None
    if line.secondment_id:
        # The secondment's id, never its employee_ref: invoice_lines is readable by every billing.view holder,
        # and employee_ref is shown only with billing.view_employee_ref (access R29, PDPL; review finding 2).
        attrs["secondment"] = line.secondment_id
        window = (line.secondment.start_date, line.secondment.end_date)
    steps = tuple(sorted((s.effective_from, s.new_amount) for s in line.price_steps.all()))
    return calc.LineTerms(str(line.line_key), line.line_no, line.amount, steps, window, attrs)


def _version_terms(contract, include=None):
    """Approved versions as plain terms (plus ``include``, a version being checked before its approval)."""
    qs = ContractVersion.objects.filter(contract=contract).filter(
        Q(status=VStatus.APPROVED) | Q(pk=getattr(include, "pk", None)))
    out = []
    for v in qs.prefetch_related("lines__price_steps", "lines__secondment", "lines__income_type",
                                 "lines__income_account", "lines__vat_exemption_reason"):
        out.append(calc.VersionTerms(
            v.pk, v.version_no, v.kind, v.effective_date, v.end_date, v.billing_frequency, v.billing_timing,
            v.period_anchor, tuple(_line_terms(line) for line in v.lines.all())))
    return out


def _billable_end(contract, versions):
    cancelled = contract.termination_kind == Contract.TerminationKind.CANCELLATION
    windows = None
    if contract.contract_type == ContractType.SECONDMENT:
        last = calc.latest([v for v in versions if not v.bills_nothing])
        windows = [line.window for line in (last.lines if last else ()) if line.window]
    return calc.billable_end(contract.start_date, versions, terminated_on=contract.terminated_on, cancelled=cancelled,
                             secondment_windows=windows)


def terms_on(contract, day, *, user):
    """R38: the approved terms in force on ``day`` (``billing.view``)."""
    access.require(user, contract.entity, "billing.view")
    return calc.governing(_version_terms(contract), day)


# --- schedule (R10, R17, R41b) ----------------------------------------------------------------------

def _rebuild_schedule(contract, today=None):
    """Re-plan every ``planned``, ``held``, ``failed`` and ``cancelled`` period for the current terms; never touch
    a ``generated`` one (R41b, c). Periods outside the new range are cancelled; cancelled periods back in range
    return to ``planned`` (R49); missing ones are added."""
    versions = _version_terms(contract)
    end = _billable_end(contract, versions)
    horizon = calc.horizon_for(today or _today(), contract.start_date) if end is None else None
    desired = calc.schedule(contract.start_date, versions, end, horizon)
    existing = {p.period_start: p for p in BillingPeriod.objects.filter(contract=contract)}
    generated = [p for p in existing.values() if p.status == PStatus.GENERATED]
    wanted = set()
    for d in desired:
        p = existing.get(d.period_start)
        if p is not None and p.status == PStatus.GENERATED:
            wanted.add(d.period_start)
            continue
        if any(g.period_start <= d.period_end and d.period_start <= g.period_end for g in generated):
            continue  # already billed under a generated period; corrections cover the difference
        wanted.add(d.period_start)
        if p is None:
            p = BillingPeriod(entity=contract.entity, contract=contract, period_start=d.period_start)
        p.period_end, p.full_period_days, p.bill_date, p.proration_factor = (
            d.period_end, d.full_days, d.bill_date, d.proration_factor)
        if p.status == PStatus.CANCELLED:
            p.status = PStatus.PLANNED
        _save(p)
    for start, p in existing.items():
        if start not in wanted and p.status in (PStatus.PLANNED, PStatus.HELD, PStatus.FAILED):
            p.status = PStatus.CANCELLED
            _save(p)


# --- contracts (3.1, R6 to R9) ------------------------------------------------------------------------

CONTRACT_FIELDS = frozenset({
    "title_en", "title_ar", "description_en", "description_ar", "start_date", "end_date", "billing_frequency",
    "billing_timing", "period_anchor", "bill_lead_days", "due_days", "lease_use", "property_ref", "unit_ref",
    "ejar_contract_no", "vehicle_ref",
})
TERM_FIELDS = ("end_date", "billing_frequency", "billing_timing", "period_anchor", "due_days")


def _contract_terms_to_version(c, v):
    v.effective_date = c.start_date
    for name in TERM_FIELDS:
        setattr(v, name, getattr(c, name))


def create_contract(entity, *, user, customer, contract_type, currency, start_date, billing_frequency, billing_timing,
                    title_ar, description_ar, **fields):
    """A draft contract with its draft version 1 (``billing.edit_contract_draft``; agents may draft, R36).

    ``counterparty_entity`` is copied from the customer, never given (R3)."""
    access.require(user, entity, "billing.edit_contract_draft")
    unknown = set(fields) - CONTRACT_FIELDS
    if "counterparty_entity" in fields or unknown:
        _fail(f"Unknown or computed fields: {sorted(unknown | ({'counterparty_entity'} & set(fields)))}.",
              "حقول غير معروفة أو محسوبة.", "fields")
    cur = _currency(currency)
    c = Contract(entity=entity, customer=customer, contract_type=contract_type, currency=cur, start_date=start_date,
                 billing_frequency=billing_frequency, billing_timing=billing_timing, title_ar=title_ar,
                 description_ar=description_ar, counterparty_entity=customer.group_entity, created_by=user,
                 source=_source(user), **fields)
    if contract_type not in ContractType.values:
        _fail(f"Unknown contract type {contract_type!r}.", "نوع عقد غير معروف.", "contract_type")
    problems = _contract_problems(c)
    if problems:
        raise _problems(problems, "contract_invalid")
    with transaction.atomic():
        _save(c)
        v = ContractVersion(entity=entity, contract=c, version_no=1, kind=ContractVersion.Kind.ORIGINAL,
                            reason_ar="الشروط الأصلية للعقد", reason_en="Original contract terms", created_by=user,
                            source=_source(user))
        _contract_terms_to_version(c, v)
        _save(v)
        _event(c, Event.CREATED, user, source=_source(user))
    return c


def _draft_contract(contract, user, permission="billing.edit_contract_draft"):
    c = Contract.objects.select_for_update(of=("self",)).select_related("entity", "customer", "currency").get(
        pk=contract.pk)
    access.require(user, c.entity, permission)
    if c.status != CStatus.DRAFT:
        _fail("Only draft contracts are edited; after approval change it through a version (R7).",
              "تُعدَّل مسودات العقود فقط؛ وبعد الاعتماد يكون التغيير بنسخة جديدة.", "not_draft")
    return c


def _touch(obj, user):
    obj.modified_by = user


@transaction.atomic
def update_contract(contract, *, user, **fields):
    c = _draft_contract(contract, user)
    unknown = set(fields) - CONTRACT_FIELDS
    if unknown:
        _fail(f"Unknown fields: {sorted(unknown)}.", "حقول غير معروفة.", "fields")
    for name, value in fields.items():
        setattr(c, name, value)
    problems = _contract_problems(c)
    if problems:
        raise _problems(problems, "contract_invalid")
    _touch(c, user)
    c.submitted_by, c.submitted_at = None, None  # a changed draft is submitted again
    _save(c)
    v = c.versions.get(version_no=1)
    _contract_terms_to_version(c, v)
    _touch(v, user)
    _save(v)
    return c


@transaction.atomic
def set_advance_terms(contract, *, user, amount, due_date, recovery, notes_ar="", notes_en=""):
    """3.4 / R30: recorded only; an offset against invoices blocks approval (R68b)."""
    c = _draft_contract(contract, user)
    if recovery not in AdvanceTerm.Recovery.values:
        _fail("Unknown recovery.", "طريقة استرداد غير معروفة.", "recovery")
    term = _save(AdvanceTerm(entity=c.entity, amount=_amount(amount, "The advance", "مبلغ الدفعة المقدمة",
                                                               _places(c.currency)),
                             currency=c.currency, due_date=due_date, recovery=recovery, notes_ar=notes_ar,
                             notes_en=notes_en, created_by=user, source=_source(user)))
    c.advance_terms = term
    _touch(c, user)
    _save(c)
    return term


@transaction.atomic
def set_deposit_terms(contract, *, user, amount, due_date, refundable=True, refund_conditions_ar="",
                      refund_conditions_en=""):
    """3.4 / R31: a memo; billing creates no invoice or entry for a deposit."""
    c = _draft_contract(contract, user)
    term = _save(DepositTerm(entity=c.entity, amount=_amount(amount, "The deposit", "مبلغ التأمين", _places(c.currency)),
                             currency=c.currency, due_date=due_date, refundable=refundable,
                             refund_conditions_ar=refund_conditions_ar, refund_conditions_en=refund_conditions_en,
                             created_by=user, source=_source(user)))
    c.deposit_terms = term
    _touch(c, user)
    _save(c)
    return term


def _editable_version(target, user):
    """The draft version a line edit applies to, after the rights check: version 1 of a draft contract
    (``billing.edit_contract_draft``) or a draft amendment of an approved one (``billing.edit_version_draft``)."""
    if isinstance(target, Contract):
        c = _draft_contract(target, user)
        return c, c.versions.select_for_update().get(version_no=1)
    v = ContractVersion.objects.select_for_update(of=("self",)).select_related("contract").get(pk=target.pk)
    c = Contract.objects.select_related("entity", "customer", "currency").get(pk=v.contract_id)
    permission = "billing.edit_contract_draft" if c.status == CStatus.DRAFT else "billing.edit_version_draft"
    access.require(user, c.entity, permission)
    if v.status != VStatus.DRAFT:
        _fail(*APPROVED_DRAFT_ONLY, "version_not_draft")
    if v.kind in (ContractVersion.Kind.TERMINATION, ContractVersion.Kind.CANCELLATION):
        _fail("Termination and cancellation versions have no lines.", "نسخ الإنهاء والإلغاء بلا بنود.", "no_lines")
    return c, v


APPROVED_DRAFT_ONLY = ("Only draft versions are edited; approved versions never change (R38).",
                       "تُعدَّل مسودات النسخ فقط؛ ولا تتغير النسخ المعتمدة.")


def _line_defaults(c, line, income_type, vat_category, vat_exemption_reason, income_account):
    from invoicing.models import IncomeType, VatExemptionReason

    line.income_type = _resolve(IncomeType, income_type, "income type", "نوع الإيراد")
    if line.income_type is None:
        _fail("The income type is required.", "نوع الإيراد مطلوب.", "income_type")
    default_category = line.income_type.default_vat_category
    if c.contract_type in (ContractType.VEHICLE_RENTAL, ContractType.SECONDMENT):
        default_category = VatCategory.STANDARD  # R25
    line.vat_category = vat_category or default_category
    reason = vat_exemption_reason
    if reason is None and line.vat_category != VatCategory.STANDARD and vat_category is None:
        reason = line.income_type.default_exemption_reason
    line.vat_exemption_reason = _resolve(VatExemptionReason, reason, "exemption reason", "سبب الإعفاء")
    if income_account is None:  # R27 as built: from the income type, intercompany account when intercompany
        code = (line.income_type.default_intercompany_account_code if c.is_intercompany else "") \
            or line.income_type.default_account_code
        if not code:
            _fail(f"Income type {line.income_type.code} has no default account; choose the income account.",
                  f"نوع الإيراد {line.income_type.code} ليس له حساب افتراضي؛ اختر حساب الإيراد.", "account_required")
        income_account = code
    if isinstance(income_account, Account):
        line.income_account = income_account
    else:
        line.income_account = Account.objects.filter(entity=c.entity, code=income_account).first()
        if line.income_account is None:
            _fail(f"Account {income_account} does not exist in {c.entity.code}.",
                  f"الحساب {income_account} غير موجود في {c.entity.code}.", "account_missing")


def _check_line(c, line):
    problems = _line_problems(c, line)
    if problems:
        raise _problems(problems, "line_invalid")


@transaction.atomic
def add_line(target, *, user, income_type, amount, description_ar, description_en="", unit_code="MON",
             vat_category=None, vat_exemption_reason=None, income_account=None, secondment=None, line_key=None):
    """Add a line to version 1 of a draft contract or to a draft amendment (R7, R39). ``line_key`` is new unless
    the line continues one of an earlier version (R42)."""
    c, v = _editable_version(target, user)
    last = v.lines.order_by("-line_no").values_list("line_no", flat=True).first() or 0
    line = ContractLine(entity=c.entity, contract=c, version=v, line_no=last + 1, description_ar=description_ar,
                        description_en=description_en, unit_code=unit_code, secondment=secondment,
                        created_by=user, source=_source(user))
    if line_key is not None:
        line.line_key = line_key
    line.amount = _amount(amount, "The amount", "المبلغ", _places(c.currency))
    _line_defaults(c, line, income_type, vat_category, vat_exemption_reason, income_account)
    _check_line(c, line)
    _save(line)
    _touch(v, user)
    _save(v)
    _touch(c, user)
    _save(c)
    return line


LINE_FIELDS = frozenset({"amount", "description_ar", "description_en", "unit_code", "vat_category",
                         "vat_exemption_reason", "income_account", "income_type"})


@transaction.atomic
def update_line(line, *, user, **fields):
    line = ContractLine.objects.select_related("version").get(pk=line.pk)
    c, v = _editable_version(line.version if line.contract.status != CStatus.DRAFT else line.contract, user)
    unknown = set(fields) - LINE_FIELDS
    if unknown:
        _fail(f"Unknown fields: {sorted(unknown)}.", "حقول غير معروفة.", "fields")
    if "amount" in fields:
        line.amount = _amount(fields.pop("amount"), "The amount", "المبلغ", _places(c.currency))
    for name in ("description_ar", "description_en", "unit_code"):
        if name in fields:
            setattr(line, name, fields.pop(name))
    if fields:
        _line_defaults(c, line, fields.get("income_type", line.income_type),
                       fields.get("vat_category", line.vat_category),
                       fields.get("vat_exemption_reason", line.vat_exemption_reason),
                       fields.get("income_account", line.income_account))
    _check_line(c, line)
    _save(line)
    _touch(v, user)
    _save(v)
    _touch(c, user)
    _save(c)
    return line


@transaction.atomic
def remove_line(line, *, user):
    line = ContractLine.objects.select_related("version", "contract").get(pk=line.pk)
    c, v = _editable_version(line.version if line.contract.status != CStatus.DRAFT else line.contract, user)
    for step in line.price_steps.all():
        _delete(step)
    _delete(line)
    _touch(v, user)
    _save(v)


def _delete(obj):
    obj._service_write = True
    obj.delete()


@transaction.atomic
def add_price_step(line, *, user, effective_from, new_amount):
    """3.3 / R18: an explicit stepped amount from ``effective_from`` (inside the contract dates; OQ-4)."""
    line = ContractLine.objects.select_related("version", "contract").get(pk=line.pk)
    c, v = _editable_version(line.version if line.contract.status != CStatus.DRAFT else line.contract, user)
    end = v.end_date
    if effective_from < c.start_date or (end is not None and effective_from > end):
        _fail("A price step falls inside the contract dates.", "تقع الخطوة السعرية ضمن تواريخ العقد.", "step_date")
    step = ContractPriceStep(entity=c.entity, line=line, effective_from=effective_from,
                             new_amount=_amount(new_amount, "The new amount", "المبلغ الجديد", _places(c.currency)))
    _save(step)
    _touch(v, user)
    _save(v)
    return step


@transaction.atomic
def add_secondment(contract, *, user, employee_ref, role_title_ar, start_date, role_title_en="", end_date=None):
    """3.6: one seconded person (pseudonymous ``employee_ref`` only, OQ-12). On a draft contract, or on an approved
    one with a draft amendment that will add the line (``billing.edit_version_draft``)."""
    c = Contract.objects.select_for_update(of=("self",)).select_related("entity").get(pk=contract.pk)
    if c.status == CStatus.DRAFT:
        access.require(user, c.entity, "billing.edit_contract_draft")
    else:
        access.require(user, c.entity, "billing.edit_version_draft")
        if not c.versions.filter(status=VStatus.DRAFT).exists():
            _fail("Add a seconded person through a draft amendment (R39).", "يضاف الموظف المعار بمسودة تعديل.",
                  "not_draft")
    if c.contract_type != ContractType.SECONDMENT:
        _fail("Only secondment contracts have seconded staff.", "الإعارات لعقود الإعارة فقط.", "contract_type")
    if not (role_title_ar or "").strip() or not (employee_ref or "").strip():
        _fail("The employee reference and the Arabic role title are required.", "مرجع الموظف والمسمى بالعربية مطلوبان.",
              "secondment_fields")
    if end_date is not None and end_date < start_date - timedelta(days=1):
        _fail("The end date is before the start date.", "تاريخ النهاية قبل تاريخ البداية.", "secondment_dates")
    return _save(SecondmentAssignment(
        entity=c.entity, contract=c, receiving_entity=c.counterparty_entity, employee_ref=employee_ref.strip(),
        role_title_ar=role_title_ar, role_title_en=role_title_en, start_date=start_date, end_date=end_date,
        created_by=user, source=_source(user)))


@transaction.atomic
def submit_contract(contract, *, user):
    """Draft → submitted for the CFO (status stays ``draft``; event ``submitted_for_approval``). Agents never
    submit (gateway: drafts only)."""
    c = _draft_contract(contract, user)
    _refuse_agent(user, c.entity, "billing.edit_contract_draft")
    problems = _approval_problems(c, c.versions.get(version_no=1))
    if problems:
        raise _problems(problems, "contract_invalid")
    c.submitted_by, c.submitted_at = user, timezone.now()
    _save(c)
    _event(c, Event.SUBMITTED, user)
    return c


@transaction.atomic
def return_contract(contract, *, user, reason_ar, reason_en=""):
    """The CFO sends a submitted draft back (event ``returned``)."""
    c = Contract.objects.select_for_update(of=("self",)).select_related("entity").get(pk=contract.pk)
    access.require(user, c.entity, "billing.approve_contract", document=c)
    if c.status != CStatus.DRAFT or c.submitted_at is None:
        _fail("Only submitted draft contracts are returned.", "تُعاد مسودات العقود المقدمة فقط.", "not_submitted")
    if not (reason_ar or "").strip():
        _fail("The reason in Arabic is required.", "السبب بالعربية مطلوب.", "reason_required")
    c.submitted_by, c.submitted_at = None, None
    _save(c)
    _event(c, Event.RETURNED, user, reason_ar=reason_ar, reason_en=reason_en)
    return c


def _approval_problems(c, version):
    problems = _contract_problems(c)
    lines = list(version.lines.select_related("income_account", "vat_exemption_reason", "secondment", "income_type"))
    if version.kind not in (ContractVersion.Kind.TERMINATION, ContractVersion.Kind.CANCELLATION) and not lines:
        problems.append("A contract needs at least one line. / يحتاج العقد إلى بند واحد على الأقل.")
    for line in lines:
        problems += _line_problems(c, line)
    return problems


def _three_dp_warnings(c):
    return [" / ".join(_msg("three_dp_warning", currency=c.currency_id))] if _places(c.currency) == 3 else []


def _purification(c, version):
    return version.lines.filter(income_account__requires_purification=True).exists()


def _next_number(entity, year):
    seq, _ = ContractSequence.objects.select_for_update().get_or_create(entity=entity, year=year)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"{entity.code}-CTR-{year}-{seq.last_number:06d}"


def approve_contract(contract, *, user):
    """R8: the entity's CFO (not the creator, submitter or last modifier) approves; the number and the schedule are
    made in the same transaction; R68 gates; R9 activation. Returns the contract with ``warnings`` (en/ar)."""
    c = Contract.objects.select_related("entity", "customer", "currency", "advance_terms").get(pk=contract.pk)
    v1 = c.versions.get(version_no=1)
    access.require(user, c.entity, "billing.approve_contract", document=c)
    if user.pk in (v1.created_by_id, v1.modified_by_id):
        access.deny(user, c.entity, "billing.approve_contract", "self_approval")
    with transaction.atomic():
        c = Contract.objects.select_for_update(of=("self",)).select_related(
            "entity", "customer", "currency", "advance_terms").get(pk=c.pk)
        if c.status != CStatus.DRAFT:
            _fail("Only draft contracts are approved.", "تُعتمد مسودات العقود فقط.", "not_draft")
        v1 = c.versions.select_for_update().get(version_no=1)
        problems = _approval_problems(c, v1) + _gate_problems(c, v1)
        if problems:
            raise _problems(problems, "contract_invalid")
        _entity_lock_now(c.entity)
        today = _today()
        now = timezone.now()
        v1.status, v1.approved_by, v1.approved_at = VStatus.APPROVED, user, now
        _save(v1)
        c.number = _next_number(c.entity, today.year)
        c.status, c.approved_by, c.approved_at, c.current_version = CStatus.APPROVED, user, now, v1
        c.requires_purification = _purification(c, v1)
        _save(c)
        _rebuild_schedule(c, today)
        _event(c, Event.APPROVED, user, number=c.number)
        if c.start_date <= today:
            c.status = CStatus.ACTIVE
            _save(c)
            _event(c, Event.ACTIVATED, user)
    c.warnings = _three_dp_warnings(c)
    return c


def contracts(entity, *, user):
    """The entity's contracts (``billing.view``, R2)."""
    access.require(user, entity, "billing.view")
    return Contract.objects.filter(entity=entity)


def view_secondment(assignment, *, user):
    """3.6 / access R29: the secondment with ``employee_ref`` masked unless the user holds
    ``billing.view_employee_ref``; an unmasked view is logged (PDPL)."""
    a = SecondmentAssignment.objects.select_related("entity").get(pk=assignment.pk)
    access.require(user, a.entity, "billing.view")
    shown = access.has(user, a.entity, "billing.view_employee_ref") and access_services.kind_of(user) == Kind.HUMAN
    if shown:
        access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=a.entity,
                            permission="billing.view_employee_ref",
                            details={"secondment": a.pk, "field": "employee_ref"})
    return {
        "employee_ref": a.employee_ref if shown else "••••", "role_title_en": a.role_title_en,
        "role_title_ar": a.role_title_ar, "start_date": a.start_date, "end_date": a.end_date,
        "hr_assignment_ref": a.hr_assignment_ref, "end_source": a.end_source,
    }


# --- versions (3.9, R38 to R41) ----------------------------------------------------------------------

VERSION_TERM_FIELDS = frozenset({"end_date", "billing_frequency", "billing_timing", "period_anchor", "due_days",
                                 "reason_ar", "reason_en", "effective_date"})
FORBIDDEN_AMENDMENTS = frozenset({"entity", "customer", "currency", "contract_type"})  # R39 / OQ-23 default


@transaction.atomic
def create_version(contract, *, user, kind, effective_date, reason_ar="", reason_en="", **terms):
    """A draft amendment, termination or cancellation (``billing.edit_version_draft``; not agents, OQ-28).

    Amendments start as a full copy of the current version's lines (same ``line_key``), to be edited. Customer,
    currency, contract type and entity are never amended (R39): those need a new contract."""
    c = Contract.objects.select_for_update(of=("self",)).select_related("entity", "currency").get(pk=contract.pk)
    access.require(user, c.entity, "billing.edit_version_draft")
    if set(terms) & FORBIDDEN_AMENDMENTS:
        _fail(f"{sorted(set(terms) & FORBIDDEN_AMENDMENTS)} cannot be amended; make a new contract (R39).",
              "لا يمكن تعديل العميل أو العملة أو نوع العقد أو الكيان؛ يلزم عقد جديد.", "not_amendable")
    unknown = set(terms) - VERSION_TERM_FIELDS
    if unknown:
        _fail(f"Unknown fields: {sorted(unknown)}.", "حقول غير معروفة.", "fields")
    if c.status in (CStatus.DRAFT,):
        _fail("A draft contract is edited directly; versions follow approval.", "تُعدَّل مسودة العقد مباشرة.",
              "not_approved")
    if kind not in (ContractVersion.Kind.AMENDMENT, ContractVersion.Kind.TERMINATION,
                    ContractVersion.Kind.CANCELLATION):
        _fail("Unknown version kind.", "نوع نسخة غير معروف.", "kind")
    if c.versions.filter(status__in=[VStatus.DRAFT, VStatus.SUBMITTED]).exists():
        _fail("This contract already has an open version; finish or withdraw it first.",
              "للعقد نسخة مفتوحة؛ أكملها أو اسحبها أولًا.", "open_version")
    current = c.current_version
    if kind == ContractVersion.Kind.CANCELLATION:
        effective_date = c.start_date  # R40: nothing was ever billable
    v = ContractVersion(entity=c.entity, contract=c, kind=kind, effective_date=effective_date, reason_ar=reason_ar,
                        reason_en=reason_en, created_by=user, source=_source(user),
                        version_no=(c.versions.order_by("-version_no").values_list("version_no", flat=True).first() or 0) + 1)
    for name in TERM_FIELDS:
        setattr(v, name, terms.get(name, getattr(current, name)))
    if kind == ContractVersion.Kind.TERMINATION:
        v.end_date = effective_date - timedelta(days=1)
    if kind == ContractVersion.Kind.CANCELLATION:
        v.end_date = c.start_date - timedelta(days=1)
    _save(v)
    if kind == ContractVersion.Kind.AMENDMENT:
        for old in current.lines.select_related("vat_exemption_reason").prefetch_related("price_steps"):
            new = ContractLine(entity=c.entity, contract=c, version=v, line_key=old.line_key, line_no=old.line_no,
                               description_en=old.description_en, description_ar=old.description_ar,
                               amount=old.amount, unit_code=old.unit_code, vat_category=old.vat_category,
                               vat_exemption_reason=old.vat_exemption_reason, income_type=old.income_type,
                               income_account=old.income_account, secondment=old.secondment, created_by=user,
                               source=_source(user))
            _save(new)
            for step in old.price_steps.all():
                _save(ContractPriceStep(entity=c.entity, line=new, effective_from=step.effective_from,
                                        new_amount=step.new_amount))
    return v


@transaction.atomic
def update_version(version, *, user, **fields):
    v = ContractVersion.objects.select_for_update(of=("self",)).select_related("contract__entity").get(pk=version.pk)
    access.require(user, v.entity, "billing.edit_version_draft")
    if v.status != VStatus.DRAFT:
        _fail(*APPROVED_DRAFT_ONLY, "version_not_draft")
    if set(fields) & FORBIDDEN_AMENDMENTS:
        _fail("Customer, currency, contract type and entity cannot be amended (R39).",
              "لا يمكن تعديل العميل أو العملة أو نوع العقد أو الكيان.", "not_amendable")
    unknown = set(fields) - VERSION_TERM_FIELDS
    if unknown:
        _fail(f"Unknown fields: {sorted(unknown)}.", "حقول غير معروفة.", "fields")
    for name, value in fields.items():
        setattr(v, name, value)
    if v.kind == ContractVersion.Kind.TERMINATION:
        v.end_date = v.effective_date - timedelta(days=1)
    _touch(v, user)
    _save(v)
    return v


def _last_generated_end(contract):
    return BillingPeriod.objects.filter(contract=contract, status=PStatus.GENERATED).order_by("-period_end").values_list(
        "period_end", flat=True).first()


def _version_problems(v):
    """R39, R40 and the per-line checks of a new contract (R1, R24, R25, R27, R29)."""
    c = v.contract
    problems = []
    current = c.current_version
    if not (v.reason_ar or "").strip():
        problems.append("The reason in Arabic is required; it becomes the reason on correction notes (R39). / "
                        "السبب بالعربية مطلوب؛ ويصبح سبب إشعارات التصحيح.")
    if v.effective_date < c.start_date:
        problems.append(f"The effective date is before the contract start {c.start_date} (R39). / "
                        f"تاريخ السريان قبل بداية العقد {c.start_date}.")
    if v.kind == ContractVersion.Kind.CANCELLATION and v.effective_date != c.start_date:
        problems.append("A cancellation is effective on the contract start (R40). / يسري الإلغاء من بداية العقد.")
    if c.status == CStatus.TERMINATED and c.terminated_on is not None and v.effective_date > c.terminated_on \
            and v.kind == ContractVersion.Kind.AMENDMENT:
        problems.append("A terminated contract is only corrected up to its termination; extensions need a new "
                        "contract (OQ-27). / يُصحَّح العقد المنهى حتى تاريخ إنهائه فقط؛ والتمديد بعقد جديد.")
    if c.termination_kind == Contract.TerminationKind.CANCELLATION:
        problems.append("A cancelled contract is not amended (OQ-27). / لا يُعدَّل العقد الملغى.")
    schedule_change = any(getattr(v, f) != getattr(current, f) for f in ("billing_frequency", "billing_timing",
                                                                         "period_anchor"))
    last_end = _last_generated_end(c)
    if schedule_change and last_end is not None and v.effective_date <= last_end:
        problems.append(f"Frequency, timing and anchor change only after the last billed period (ends {last_end}) "
                        f"(R39). / لا تتغير الدورية والتوقيت وبداية الفترات إلا بعد آخر فترة مفوترة ({last_end}).")
    if v.billing_frequency not in Frequency.values:
        problems.append("Unknown billing frequency. / دورية فوترة غير معروفة.")
    if v.end_date is not None and v.end_date < c.start_date - timedelta(days=1):
        problems.append("The end date is before the contract start. / تاريخ النهاية قبل بداية العقد.")
    if v.kind == ContractVersion.Kind.AMENDMENT:
        if v.end_date is None and c.contract_type not in (ContractType.SECONDMENT, ContractType.SERVICE):
            problems.append("An end date is required except for secondments and services. / تاريخ النهاية مطلوب.")
        lines = list(v.lines.select_related("income_account", "vat_exemption_reason", "secondment", "income_type"))
        if not lines:
            problems.append("An amendment keeps at least one line. / يبقى في التعديل بند واحد على الأقل.")
        for line in lines:
            problems += _line_problems(c, line)
    return problems


@transaction.atomic
def submit_version(version, *, user):
    v = ContractVersion.objects.select_for_update(of=("self",)).select_related(
        "contract__entity", "contract__currency", "contract__current_version").get(pk=version.pk)
    access.require(user, v.entity, "billing.edit_version_draft")
    if v.status != VStatus.DRAFT:
        _fail("Only draft versions are submitted.", "تُقدَّم مسودات النسخ فقط.", "version_not_draft")
    problems = _version_problems(v)
    if problems:
        raise _problems(problems, "version_invalid")
    v.status, v.submitted_by, v.submitted_at = VStatus.SUBMITTED, user, timezone.now()
    _save(v)
    _event(v.contract, Event.VERSION_SUBMITTED, user, version=v.version_no, kind=v.kind)
    return v


@transaction.atomic
def reject_version(version, *, user, reason_ar, reason_en=""):
    v = ContractVersion.objects.select_for_update(of=("self",)).select_related("contract__entity").get(pk=version.pk)
    access.require(user, v.entity, "billing.approve_version", document=v)
    if v.status != VStatus.SUBMITTED:
        _fail("Only submitted versions are rejected.", "تُرفض النسخ المقدمة فقط.", "version_not_submitted")
    if not (reason_ar or "").strip():
        _fail("The reason in Arabic is required.", "السبب بالعربية مطلوب.", "reason_required")
    v.status, v.rejected_reason_ar, v.rejected_reason_en = VStatus.REJECTED, reason_ar, reason_en
    _save(v)
    _event(v.contract, Event.VERSION_REJECTED, user, version=v.version_no)
    return v


@transaction.atomic
def withdraw_version(version, *, user):
    v = ContractVersion.objects.select_for_update(of=("self",)).select_related("contract__entity").get(pk=version.pk)
    access.require(user, v.entity, "billing.edit_version_draft")
    if v.status not in (VStatus.DRAFT, VStatus.SUBMITTED):
        _fail("Only open versions are withdrawn.", "تُسحب النسخ المفتوحة فقط.", "version_closed")
    v.status = VStatus.WITHDRAWN
    _save(v)
    _event(v.contract, Event.VERSION_WITHDRAWN, user, version=v.version_no)
    return v


def approve_version(version, *, user):
    """R39 to R41: the CFO (not creator, submitter or last modifier) approves; termination and cancellation also
    need ``billing.approve_termination``. Under the entity's billing lock: the version takes effect, the schedule is
    rebuilt (generated periods untouched) and the correction pass drafts credit / debit notes (R42 to R46, R51).
    Returns the version with ``adjustments`` (created) and ``warnings``."""
    v = ContractVersion.objects.select_related("contract__entity").get(pk=version.pk)
    access.require(user, v.entity, "billing.approve_version", document=v)
    if v.kind in (ContractVersion.Kind.TERMINATION, ContractVersion.Kind.CANCELLATION):
        access.require(user, v.entity, "billing.approve_termination", document=v)
    sys = _system_user()
    with transaction.atomic():
        v = ContractVersion.objects.select_for_update(of=("self",)).get(pk=v.pk)
        c = Contract.objects.select_for_update(of=("self",)).select_related(
            "entity", "currency", "customer", "advance_terms", "current_version").get(pk=v.contract_id)
        if v.status != VStatus.SUBMITTED:
            _fail("Only submitted versions are approved.", "تُعتمد النسخ المقدمة فقط.", "version_not_submitted")
        v.contract = c
        problems = _version_problems(v)
        if v.kind == ContractVersion.Kind.AMENDMENT:
            problems += _gate_problems(c, v)
        if problems:
            raise _problems(problems, "version_invalid")
        _entity_lock_now(c.entity)
        _sync_contract(c, sys)
        v.status, v.approved_by, v.approved_at = VStatus.APPROVED, user, timezone.now()
        _save(v)
        c.current_version = v
        if v.kind == ContractVersion.Kind.TERMINATION:
            c.status, c.termination_kind = CStatus.TERMINATED, Contract.TerminationKind.EARLY
            c.terminated_on = v.effective_date - timedelta(days=1)
            c.termination_reason_ar, c.termination_reason_en = v.reason_ar, v.reason_en
            action = Event.TERMINATED
        elif v.kind == ContractVersion.Kind.CANCELLATION:
            c.status, c.termination_kind, c.terminated_on = CStatus.TERMINATED, Contract.TerminationKind.CANCELLATION, None
            c.termination_reason_ar, c.termination_reason_en = v.reason_ar, v.reason_en
            action = Event.CANCELLED
        else:
            for name in TERM_FIELDS:
                setattr(c, name, getattr(v, name))
            c.requires_purification = _purification(c, v)
            action = Event.AMENDED
        _save(c)
        _event(c, Event.VERSION_APPROVED, user, version=v.version_no, kind=v.kind,
               effective_date=v.effective_date)
        _event(c, action, user, version=v.version_no)
        _rebuild_schedule(c)
        result = _correction_pass(c, sys, trigger="version", version=v, affected=(v.effective_date, None),
                                  supersede=True)
        _notify_corrections(c, result)
    v.adjustments = result["created"]
    v.warnings = _three_dp_warnings(c) if v.kind == ContractVersion.Kind.AMENDMENT else []
    return v


# --- corrections (R42 to R51) ------------------------------------------------------------------------

def _billed_by_key(period):
    """3.11: the invoice's net per line key plus counted corrections of that invoice."""
    billed = defaultdict(lambda: ZERO)
    for line in period.invoice_lines or ():
        billed[line["line_key"]] += Decimal(line["net"])
    for adj in BillingAdjustment.objects.filter(billing_period=period, original_document=period.sales_document_id,
                                                status__in=BillingAdjustment.COUNTED):
        for line in adj.lines:
            billed[line["line_key"]] += Decimal(line["delta"])
    return billed


def _invoice_line_attrs(period, key, line_terms):
    for line in period.invoice_lines or ():
        if line["line_key"] == key:
            return {k: line[k] for k in ("line_no", "description_en", "description_ar", "unit_code", "vat_category",
                                         "vat_exemption_reason", "income_type", "income_account")}
    attrs = dict(line_terms.attrs) if line_terms else {}
    attrs.pop("secondment", None)
    return attrs


def _supersede(adj, sys, actor):
    """R46c: a pending correction is superseded; its draft note is deleted through invoicing (drafts only)."""
    doc = adj.document
    adj.status, adj.document = AStatus.SUPERSEDED, None
    _save(adj)
    if doc is not None:
        from invoicing.models import SalesDocument

        stored = SalesDocument.objects.filter(pk=doc.pk).values_list("status", flat=True).first()
        if stored in ("draft", "submitted"):
            invoicing_port.get_port().discard_adjustment_draft(doc, sys)
    _event(adj.contract, Event.CORRECTION_SUPERSEDED, actor, adjustment=adj.pk, period=adj.billing_period_id,
           document=getattr(doc, "pk", None))


def _draft_note(adj, sys, actor):
    """R43, R47: ask invoicing for the draft note once the original invoice is final; on a refusal the correction
    stays ``waiting_original`` with the reason and is tried again at the next status read-back."""
    try:
        with transaction.atomic():
            doc = invoicing_port.get_port().create_adjustment_from_billing(adj, sys)
            adj.document, adj.status = doc, AStatus.DRAFTED
            adj.message_en = adj.message_ar = ""
            _save(adj)
            _event(adj.contract, Event.CORRECTION_DRAFTED, actor, adjustment=adj.pk, document=doc.pk)
    except Exception as exc:  # noqa: BLE001 - any refusal leaves the correction waiting, with the reason
        adj.refresh_from_db()
        en, ar = _split(_exc_text(exc))
        adj.message_en, adj.message_ar = _msg("draft_failed", reason=en)[0], _msg("draft_failed", reason=ar)[1]
        _save(adj)
        return False
    return True


def _exc_text(exc):
    messages = getattr(exc, "messages", None)
    if messages:
        return messages[0]
    if hasattr(exc, "message_en"):
        return f"{exc.message_en} / {exc.message_ar}"
    return str(exc)


def _correction_pass(contract, sys, *, trigger, version, affected, supersede, include_discarded=False):
    """R42: recompute every generated period against the current terms and create one credit and/or one debit
    correction per period and original invoice. ``affected`` = (first, last or None) days whose terms changed,
    used for the supply period of the notes."""
    c = contract
    superseded = 0
    if supersede:
        for adj in BillingAdjustment.objects.filter(contract=c, status__in=BillingAdjustment.PENDING).select_related(
                "contract", "document"):
            _supersede(adj, sys, sys)
            superseded += 1
    versions = _version_terms(c)
    end = _billable_end(c, versions)
    places = _places(c.currency)
    created = []
    from invoicing.models import SalesDocument

    for p in BillingPeriod.objects.filter(contract=c, status=PStatus.GENERATED, sales_document__isnull=False).order_by(
            "period_start"):
        required = calc.required_nets(p.period_start, p.period_end, p.full_period_days, versions, c.start_date, end,
                                      places)
        billed = _billed_by_key(p)
        sides = {"credit": [], "debit": []}
        for key in list(dict.fromkeys([*billed.keys(), *required.keys()])):
            req, line_terms = required.get(key, (ZERO, None))
            have = billed.get(key, ZERO)
            delta = req - have
            if delta == ZERO:
                continue
            row = {"line_key": key, "required": str(req), "billed": str(have), "delta": str(delta),
                   **_invoice_line_attrs(p, key, line_terms)}
            sides["credit" if delta < ZERO else "debit"].append(row)
        first, last = affected
        start = max(p.period_start, first) if first else p.period_start
        stop = min(p.period_end, last) if last else p.period_end
        if start > stop:
            start, stop = p.period_start, p.period_end
        for kind, rows in sides.items():
            if not rows:
                continue
            pending = BillingAdjustment.objects.filter(billing_period=p, original_document=p.sales_document_id,
                                                       kind=kind, status__in=BillingAdjustment.PENDING)
            if pending.exists():
                continue
            discarded = BillingAdjustment.objects.filter(billing_period=p, kind=kind, version=version,
                                                         status=AStatus.DISCARDED)
            if trigger == "recompute" and not include_discarded and discarded.exists():
                continue  # R48 / AT-30: not re-created for the same version
            adj = _save(BillingAdjustment(
                entity=c.entity, contract=c, version=version, trigger=trigger, billing_period=p,
                original_document_id=p.sales_document_id, kind=kind, lines=rows, supply_start=start,
                supply_end=stop, status=AStatus.WAITING_ORIGINAL))
            created.append(adj)
            original = SalesDocument.objects.filter(pk=p.sales_document_id).values_list("status", flat=True).first()
            if original == "final":
                _draft_note(adj, sys, sys)
    _event(c, Event.CORRECTIONS_COMPUTED, sys, trigger=trigger, version=version.version_no, created=len(created),
           superseded=superseded)
    return {"created": created, "superseded": superseded}


def _notify_corrections(c, result):
    """R51: the entity's accounting staff get a summary of the corrections (en/ar)."""
    created = result["created"]
    waiting = sum(1 for a in created if a.status == AStatus.WAITING_ORIGINAL)
    if not created and not result["superseded"]:
        return
    en, ar = _msg("corrections", contract=c.number, created=len(created), waiting=waiting,
                  superseded=result["superseded"])
    _notify(c.entity, "billing.release_period", BillingNotification.Kind.CORRECTIONS, en, ar, contract=c)


def recompute_corrections(contract, *, user, include_discarded=False):
    """R47, R48: a person asks for the correction pass again (no new version). Pending corrections count in the
    billed position, so nothing is created twice (R46b); discarded ones are re-created only when asked."""
    c = Contract.objects.select_related("entity", "currency", "current_version").get(pk=contract.pk)
    access.require(user, c.entity, "billing.recompute_corrections")
    _refuse_non_human(user, c.entity, "billing.recompute_corrections")
    sys = _system_user()
    with transaction.atomic():
        c = Contract.objects.select_for_update(of=("self",)).select_related("entity", "currency",
                                                                             "current_version").get(pk=c.pk)
        _entity_lock_now(c.entity)
        _sync_contract(c, sys)
        result = _correction_pass(c, sys, trigger="recompute", version=c.current_version, affected=(None, None),
                                  supersede=False, include_discarded=include_discarded)
        _notify_corrections(c, result)
    return result["created"]


@transaction.atomic
def discard_adjustment(adjustment, *, user, reason_ar, reason_en=""):
    """R48: the reviewer discards a pending correction with an Arabic reason; its draft note is deleted through
    invoicing. It stays on the unresolved-differences report."""
    adj = BillingAdjustment.objects.select_for_update(of=("self",)).select_related("contract__entity").get(
        pk=adjustment.pk)
    access.require(user, adj.entity, "billing.review_correction")
    _refuse_non_human(user, adj.entity, "billing.review_correction")
    if not (reason_ar or "").strip():
        _fail("The discard reason in Arabic is required (R48).", "سبب الاستبعاد بالعربية مطلوب.", "reason_required")
    sys = _system_user()
    _sync_adjustment(adj)
    if adj.status not in BillingAdjustment.PENDING:
        _fail("Only pending corrections are discarded; issued notes are never changed.",
              "تُستبعد التصحيحات المعلقة فقط؛ ولا تتغير الإشعارات الصادرة.", "not_pending")
    doc = adj.document
    adj.status, adj.document, adj.reviewed_by = AStatus.DISCARDED, None, user
    adj.discard_reason_ar, adj.discard_reason_en = reason_ar, reason_en
    _save(adj)
    if doc is not None:
        invoicing_port.get_port().discard_adjustment_draft(doc, sys)
    _event(adj.contract, Event.CORRECTION_DISCARDED, user, adjustment=adj.pk, reason_ar=reason_ar)
    return adj


def adjustments(entity, *, user):
    access.require(user, entity, "billing.view")
    return BillingAdjustment.objects.filter(entity=entity)


def unresolved_differences(entity, *, user):
    """R48: discarded and ZATCA-rejected corrections of the entity, with en/ar labels (``billing.view``)."""
    access.require(user, entity, "billing.view")
    rows = []
    for adj in BillingAdjustment.objects.filter(entity=entity, status__in=[AStatus.DISCARDED, AStatus.FAILED]) \
            .select_related("contract", "billing_period", "original_document"):
        delta = sum((Decimal(line["delta"]) for line in adj.lines), ZERO)
        status = BillingAdjustment.Status(adj.status)
        en, ar = _split(status.label)
        rows.append({
            "contract": adj.contract.number, "period_start": adj.billing_period.period_start,
            "period_end": adj.billing_period.period_end, "original_document": adj.original_document.number,
            "kind": adj.kind, "currency": adj.contract.currency_id, "delta": delta, "status": adj.status,
            "status_en": en, "status_ar": ar, "reason_ar": adj.discard_reason_ar, "reason_en": adj.discard_reason_en,
        })
    return rows


# --- status read-back (R12, R47, R55) ------------------------------------------------------------------

NOTE_STATUS = {"draft": AStatus.DRAFTED, "submitted": AStatus.SUBMITTED, "issued": AStatus.ISSUED,
               "final": AStatus.FINAL, "rejected": AStatus.FAILED}


def _sync_adjustment(adj):
    if adj.document_id is None or adj.status not in (AStatus.DRAFTED, AStatus.SUBMITTED, AStatus.ISSUED):
        return False
    from invoicing.models import SalesDocument

    doc = SalesDocument.objects.filter(pk=adj.document_id).values("status", "submitted_by_id").first()
    if doc is None:
        return False
    new = NOTE_STATUS.get(doc["status"], adj.status)
    if new == adj.status:
        return False
    adj.status = new
    if doc["submitted_by_id"] and adj.reviewed_by_id is None:
        adj.reviewed_by_id = doc["submitted_by_id"]
    _save(adj)
    if new == AStatus.SUBMITTED:
        _event(adj.contract, Event.CORRECTION_SUBMITTED, _system_user(), adjustment=adj.pk)
    return True


def _sync_period(p, sys):
    """R12: a ZATCA-rejected invoice sends its period to ``failed`` (human release) and joins
    ``superseded_documents``; its pending corrections are superseded."""
    from invoicing.models import SalesDocument

    doc = SalesDocument.objects.filter(pk=p.sales_document_id).first()
    if doc is None or doc.status != "rejected":
        return False
    for adj in BillingAdjustment.objects.filter(billing_period=p, original_document=doc,
                                                status__in=BillingAdjustment.PENDING).select_related("contract"):
        _supersede(adj, sys, sys)
    p.superseded_documents.add(doc)
    en, ar = _msg("zatca_rejected", number=doc.number)
    p.status, p.sales_document, p.needs_release = PStatus.FAILED, None, True
    p.failure_reason_en, p.failure_reason_ar, p.error_code = en[:300], ar[:300], "zatca_rejected"
    _save(p)
    return True


def _sync_contract(contract, sys):
    """Read back invoice and note statuses of one contract (R55). Returns the number of changes."""
    from invoicing.models import SalesDocument

    count = 0
    for p in BillingPeriod.objects.filter(contract=contract, status=PStatus.GENERATED,
                                          sales_document__isnull=False).exclude(sales_document__status="final"):
        count += _sync_period(p, sys)
    for adj in BillingAdjustment.objects.filter(contract=contract, status__in=[
            AStatus.DRAFTED, AStatus.SUBMITTED, AStatus.ISSUED]).select_related("contract"):
        count += _sync_adjustment(adj)
    finals = SalesDocument.objects.filter(status="final").values("pk")
    for adj in BillingAdjustment.objects.filter(contract=contract, status=AStatus.WAITING_ORIGINAL,
                                                original_document__in=finals).select_related("contract"):
        count += _draft_note(adj, sys, sys)
    return count


# --- runs (R11, R14, R23, R33, R34, R52 to R55, R66, R67) ----------------------------------------------------

def _is_billing_system_user(user):
    profile = access_services.profile_of(user)
    return profile is not None and profile.kind == Kind.SYSTEM and profile.system_code == "billing"


def run_billing(run_date=None, entity=None, user=None, *, trigger=BillingRun.Trigger.SCHEDULER, initiated_by=None):
    """R66: the daily run, as the billing system user. One ``BillingRun`` per entity (scoped, R2); entities where
    the system user has no ``system_billing`` membership are recorded as skipped (R67). Returns the runs."""
    user = user or _system_user()
    run_date = run_date or _today()
    if entity is not None:
        entities = [entity]
    else:
        ids = set(Contract.objects.filter(status__in=[CStatus.APPROVED, CStatus.ACTIVE, CStatus.TERMINATED])
                  .values_list("entity_id", flat=True))
        ids |= set(access.entities_for(user, "billing.issue_from_contract").values_list("pk", flat=True))
        entities = list(Entity.objects.filter(pk__in=ids).order_by("code"))
    runs = []
    for e in entities:
        if not _is_billing_system_user(user):
            access.deny(user, e, "billing.issue_from_contract", "system_only")
        if not access.has(user, e, "billing.issue_from_contract"):
            en, ar = _msg("no_membership", entity=e.code)
            runs.append(_save(BillingRun(entity=e, run_date=run_date, trigger=trigger, started_by=user,
                                         initiated_by=initiated_by, status=BillingRun.Status.SKIPPED,
                                         finished_at=timezone.now(), messages=[{"en": en, "ar": ar}])))
            continue
        access.require(user, e, "billing.start_run")
        runs.append(_run_entity(e, run_date, user, trigger, initiated_by))
    return runs


def start_manual_run(entity, *, user, run_date=None):
    """R67 / access R22: a person (``billing.start_run``; never an agent, R36) starts a run; it executes as the
    billing system user and records ``initiated_by``."""
    access.require(user, entity, "billing.start_run")
    _refuse_non_human(user, entity, "billing.start_run")
    return run_billing(run_date, entity=entity, user=_system_user(), trigger=BillingRun.Trigger.MANUAL,
                       initiated_by=user)[0]


def _run_entity(entity, run_date, sys, trigger, initiated_by):
    run = _save(BillingRun(entity=entity, run_date=run_date, trigger=trigger, started_by=sys,
                           initiated_by=initiated_by))
    counts = defaultdict(int)
    _run_lock(entity)
    try:
        with transaction.atomic():
            for c in Contract.objects.filter(entity=entity, status__in=[CStatus.APPROVED, CStatus.ACTIVE,
                                                                        CStatus.TERMINATED]):
                counts["sync"] += _sync_contract(c, sys)
        _prepare_contracts(entity, run_date, sys)
        due = (BillingPeriod.objects.filter(entity=entity, contract__status__in=[CStatus.ACTIVE, CStatus.TERMINATED],
                                            status__in=[PStatus.PLANNED, PStatus.FAILED], needs_release=False)
               .select_related("contract").order_by("bill_date", "pk"))
        for p in list(due):
            if p.bill_date - timedelta(days=p.contract.bill_lead_days) <= run_date:
                outcome = _bill_period(run, p.pk, sys, run_date)
                if outcome:
                    counts[outcome] += 1
        lookback = run_date - timedelta(days=SKIP_LOOKBACK_DAYS)
        recent = (BillingPeriod.objects.filter(entity=entity, status=PStatus.GENERATED, bill_date__gt=lookback,
                                               bill_date__lte=run_date + timedelta(days=366))
                  .select_related("contract", "sales_document")
                  .exclude(run_items__run=run))
        for p in recent:
            if p.bill_date - timedelta(days=p.contract.bill_lead_days) <= run_date:
                en, ar = _msg("already_billed", number=p.sales_document.number if p.sales_document else "")
                _save(BillingRunItem(entity=entity, run=run, billing_period=p, outcome=BillingRunItem.Outcome.SKIPPED,
                                     sales_document=p.sales_document, message_en=en, message_ar=ar))
                counts["skipped_already_billed"] += 1
        _close_contracts(entity, run_date, sys)
        run.status = (BillingRun.Status.COMPLETED_WITH_ERRORS if counts["failed"] else BillingRun.Status.COMPLETED)
    except Exception as exc:  # noqa: BLE001 - the run is recorded as aborted; the command exits non-zero
        en, ar = _split(_exc_text(exc))
        run.status = BillingRun.Status.ABORTED
        run.messages = [*run.messages, {"en": en, "ar": ar}]
    finally:
        _run_unlock(entity)
    run.generated_count, run.held_count, run.failed_count = counts["generated"], counts["held"], counts["failed"]
    run.skipped_count, run.sync_count = counts["skipped_already_billed"], counts["sync"]
    run.finished_at = timezone.now()
    _save(run)
    _notify_run(run)
    return run


def _prepare_contracts(entity, run_date, sys):
    """R9: approved contracts become active on their start; open-ended schedules roll forward (R10)."""
    for c in Contract.objects.filter(entity=entity, status__in=[CStatus.APPROVED, CStatus.ACTIVE]):
        with transaction.atomic():
            c = Contract.objects.select_for_update(of=("self",)).get(pk=c.pk)
            if c.status == CStatus.APPROVED and c.start_date <= run_date:
                c.status = CStatus.ACTIVE
                _save(c)
                _event(c, Event.ACTIVATED, sys)
            if _billable_end(c, _version_terms(c)) is None:
                _rebuild_schedule(c, run_date)


def _close_contracts(entity, run_date, sys):
    """R9: ``ended`` once the end has passed and every non-cancelled period is generated."""
    for c in Contract.objects.filter(entity=entity, status=CStatus.ACTIVE):
        end = _billable_end(c, _version_terms(c))
        if end is None or end >= run_date:
            continue
        if BillingPeriod.objects.filter(contract=c).exclude(status__in=[PStatus.GENERATED, PStatus.CANCELLED]).exists():
            continue
        with transaction.atomic():
            c = Contract.objects.select_for_update(of=("self",)).get(pk=c.pk)
            c.status = CStatus.ENDED
            _save(c)
            _event(c, Event.ENDED, sys)


def _open_period_problem(entity, day):
    period = FiscalPeriod.objects.filter(entity=entity, start_date__lte=day, end_date__gte=day).first()
    return period is None or period.status != FiscalPeriod.Status.OPEN


def _wholly_closed(entity, start, end):
    periods = list(FiscalPeriod.objects.filter(entity=entity, start_date__lte=end, end_date__gte=start))
    if not periods or any(p.status == FiscalPeriod.Status.OPEN for p in periods):
        return False
    covered = sum(calc.days(max(p.start_date, start), min(p.end_date, end)) for p in periods)
    return covered >= calc.days(start, end)


def _invoice_lines(p, contract):
    """R52: the frozen invoice input, one entry per line key with a positive net (actual days, R16).

    Secondment lines carry ``secondment`` (the ``SecondmentAssignment`` id), not ``employee_ref``: the frozen
    lines are visible to every ``billing.view`` holder (``periods``, admin), and ``employee_ref`` is personal data
    shown only with ``billing.view_employee_ref`` (3.6, access R29). Invoicing resolves the id itself if it needs
    the reference (deviation from 3.5, which lists ``employee_ref``; reported to the spec-writer)."""
    versions = _version_terms(contract)
    end = _billable_end(contract, versions)
    nets = calc.required_nets(p.period_start, p.period_end, p.full_period_days, versions, contract.start_date, end,
                              _places(contract.currency))
    en, ar = calc.period_text(p.period_start, p.period_end)
    lines = []
    for key, (net, line) in nets.items():
        if net <= ZERO or line is None:
            continue
        a = line.attrs
        row = {"line_key": key, "line_no": a["line_no"], "description_en": (a["description_en"] + en).strip()
               if a["description_en"] else "", "description_ar": a["description_ar"] + ar, "net": str(net),
               "unit_code": a["unit_code"], "vat_category": a["vat_category"],
               "vat_exemption_reason": a["vat_exemption_reason"], "income_type": a["income_type"],
               "income_account": a["income_account"]}
        if "secondment" in a:
            row["secondment"] = a["secondment"]
        lines.append(row)
    return sorted(lines, key=lambda r: r["line_no"])


def _error_code(exc):
    from core.access import AccessDenied

    if isinstance(exc, AccessDenied):
        return "permission_denied"
    if isinstance(exc, NotImplementedError):
        return "invoicing_not_built"
    return getattr(exc, "code", None) or "invoicing_error"


def _item(run, p, outcome, en, ar, *, code="", doc=None, warnings=()):
    _save(BillingRunItem(entity=p.entity, run=run, billing_period=p, outcome=outcome, sales_document=doc,
                         error_code=code, message_en=en, message_ar=ar, warnings=list(warnings)))


def _set_outcome(p, status, code, en, ar):
    p.status, p.error_code = status, code
    if status == PStatus.HELD:
        p.hold_reason_en, p.hold_reason_ar = en[:300], ar[:300]
    else:
        p.failure_reason_en, p.failure_reason_ar = en[:300], ar[:300]
        p.consecutive_failures += 1
    _save(p)


def _bill_period(run, period_id, sys, run_date):
    """One period in its own transaction (R33): lock, check, freeze the lines, call invoicing (R52), map the
    outcome by error code (R54). Returns the outcome or None (nothing to do)."""
    with transaction.atomic():
        p = BillingPeriod.objects.select_for_update(of=("self",)).select_related(
            "contract__currency", "contract__entity", "entity").get(pk=period_id)
        c = p.contract
        if p.status == PStatus.FAILED and not p.needs_release:
            p.status = PStatus.PLANNED  # R10: automatic retry
        if p.status != PStatus.PLANNED or c.status not in (CStatus.ACTIVE, CStatus.TERMINATED):
            return None
        if _open_period_problem(p.entity, run_date):  # R23: before anything is issued; no number used
            en, ar = _msg("period_closed", day=run_date)
            _set_outcome(p, PStatus.FAILED, "period_closed", en, ar)
            _item(run, p, BillingRunItem.Outcome.FAILED, en, ar, code="period_closed")
            return "failed"
        if _wholly_closed(p.entity, p.period_start, p.period_end):  # R23 catch-up / OQ-13
            en, ar = _msg("supply_closed", start=p.period_start, end=p.period_end)
            _set_outcome(p, PStatus.HELD, "supply_period_closed", en, ar)
            _item(run, p, BillingRunItem.Outcome.HELD, en, ar, code="supply_period_closed")
            return "held"
        if not (p.entity.vat_number or "").strip():  # R54 / OQ-34 default: held, nothing is created
            en, ar = _msg("not_vat_registered")
            _set_outcome(p, PStatus.HELD, "entity_not_vat_registered", en, ar)
            _item(run, p, BillingRunItem.Outcome.HELD, en, ar, code="entity_not_vat_registered")
            return "held"
        lines = _invoice_lines(p, c)
        if not lines:
            p.status = PStatus.CANCELLED
            _save(p)
            return None
        if _places(c.currency) == 3 and any(Decimal(r["net"]) != Decimal(r["net"]).quantize(Decimal("0.01"))
                                            for r in lines):  # R37 (b): held, nothing created
            en, ar = _msg("third_decimal")
            _set_outcome(p, PStatus.HELD, "third_decimal_pending", en, ar)
            _item(run, p, BillingRunItem.Outcome.HELD, en, ar, code="third_decimal_pending")
            return "held"
        try:
            with transaction.atomic():
                p.invoice_lines, p.billed_version_id = lines, c.current_version_id
                _save(p)
                doc = invoicing_port.get_port().create_from_billing(p, sys)
        except (ValidationError, PermissionDenied, NotImplementedError) as exc:
            # InvoicingError, LedgerError, AccessDenied, the stub: mapped by error code (R54); anything else aborts
            return _refused(run, p.pk, exc)
        p.refresh_from_db()
        p.status, p.sales_document, p.generated_at = PStatus.GENERATED, doc, timezone.now()
        p.error_code, p.consecutive_failures, p.needs_release = "", 0, False
        p.hold_reason_en = p.hold_reason_ar = p.failure_reason_en = p.failure_reason_ar = ""
        _save(p)
        en, ar = _msg("generated", number=doc.number or doc.pk)
        warnings = [{"en": _split(w)[0], "ar": _split(w)[1]} for w in getattr(doc, "warnings", []) or []]
        _item(run, p, BillingRunItem.Outcome.GENERATED, en, ar, doc=doc, warnings=warnings)
        return "generated"


def _refused(run, period_id, exc):
    """R54: nothing was created (the savepoint rolled back); the period is held or failed by error code."""
    p = BillingPeriod.objects.select_related("entity").get(pk=period_id)
    code = _error_code(exc)
    if code in MESSAGES and code not in ("generated",):
        en, ar = _msg(code)
    elif code == "third_decimal_pending":
        en, ar = _msg("third_decimal")
    elif code == "entity_not_vat_registered":
        en, ar = _msg("not_vat_registered")
    else:
        en, ar = _split(_exc_text(exc))
    if code in HELD_CODES:
        _set_outcome(p, PStatus.HELD, code, en, ar)
        _item(run, p, BillingRunItem.Outcome.HELD, en, ar, code=code)
        return "held"
    if p.status != PStatus.PLANNED:
        p.status = PStatus.PLANNED
    _set_outcome(p, PStatus.FAILED, code, en, ar)
    _item(run, p, BillingRunItem.Outcome.FAILED, en, ar, code=code)
    return "failed"


@transaction.atomic
def release_period(period, *, user, reason_ar=""):
    """R10, R12, R23, R37c: a person releases a ``held`` period, or a ``failed`` one that needs release, back to
    ``planned`` (``billing.release_period``; never an agent, R36)."""
    p = BillingPeriod.objects.select_for_update(of=("self",)).select_related("contract", "entity").get(pk=period.pk)
    access.require(user, p.entity, "billing.release_period")
    _refuse_non_human(user, p.entity, "billing.release_period")
    if not (p.status == PStatus.HELD or (p.status == PStatus.FAILED and p.needs_release)):
        _fail("Only held periods, or failed ones that need release, are released.",
              "تُطلق الفترات المعلقة، أو المتعثرة التي تحتاج إلى إطلاق، فقط.", "not_releasable")
    previous = p.status
    p.status, p.needs_release = PStatus.PLANNED, False
    p.hold_reason_en = p.hold_reason_ar = ""
    _save(p)
    _event(p.contract, Event.PERIOD_RELEASED, user, period=p.pk, previous=previous, reason_ar=reason_ar)
    return p


def periods(contract, *, user):
    access.require(user, contract.entity, "billing.view")
    return BillingPeriod.objects.filter(entity=contract.entity, contract=contract)


def runs(entity, *, user):
    access.require(user, entity, "billing.view")
    return BillingRun.objects.filter(entity=entity)


# --- notifications (R34, R51) -------------------------------------------------------------------------

def _holders(entity, permission):
    at = timezone.now()
    chain = access_services.covering_entities(entity)
    users = {m.user for m in Membership.objects.active(at).filter(
        entity__in=chain, role__permission_rows__codename=permission, role__kind=Kind.HUMAN).select_related("user")}
    return sorted((u for u in users if access.has(u, entity, permission)), key=lambda u: u.pk)


def _notify(entity, permission, kind, en, ar, *, run=None, contract=None):
    for u in _holders(entity, permission):
        BillingNotification.objects.create(entity=entity, recipient=u, kind=kind, message_en=en, message_ar=ar,
                                           run=run, contract=contract)


def _notify_run(run):
    if run.status == BillingRun.Status.SKIPPED:
        return
    en, ar = _msg("run_summary", day=run.run_date, entity=run.entity.code, generated=run.generated_count,
                  held=run.held_count, failed=run.failed_count)
    _notify(run.entity, "billing.release_period", BillingNotification.Kind.RUN_SUMMARY, en, ar, run=run)
    stuck = BillingPeriod.objects.filter(entity=run.entity, status=PStatus.FAILED,
                                         consecutive_failures__gte=ESCALATE_AFTER_RUNS).count()
    if stuck:
        en, ar = _msg("escalation", count=stuck, entity=run.entity.code, runs=ESCALATE_AFTER_RUNS)
        _notify(run.entity, "billing.approve_contract", BillingNotification.Kind.FAILURE_ESCALATION, en, ar, run=run)


def notifications(entity, *, user):
    """The user's own billing notifications in ``entity`` (``billing.view``)."""
    access.require(user, entity, "billing.view")
    return BillingNotification.objects.filter(entity=entity, recipient=user)


# --- secondments and HR (R19, R49, R57 to R62) -------------------------------------------------------------

def _hr_assignment(entity, reference):
    from hr.models import Assignment

    return Assignment.objects.filter(entity=entity, reference=reference).select_related("receiving_entity").first()


@transaction.atomic
def link_hr_assignment(assignment, hr_assignment_ref, *, user):
    """R62: link a secondment to its HR assignment once, on a draft contract or for a draft version; copies only
    the pseudonymous billing view (employee code, role titles, dates), never names, IDs or salary."""
    from hr import services as hr_services

    a = SecondmentAssignment.objects.select_for_update(of=("self",)).select_related(
        "contract__entity", "contract__counterparty_entity").get(pk=assignment.pk)
    access.require(user, a.entity, "billing.record_contract_event")
    _refuse_non_human(user, a.entity, "billing.record_contract_event")
    c = a.contract
    if a.hr_assignment_ref:
        _fail("The HR link is set once and never changed (R62).", "يُحدد الربط بالموارد البشرية مرة واحدة ولا يتغير.",
              "already_linked")
    open_version = c.versions.filter(status=VStatus.DRAFT).exists()
    in_approved = ContractLine.objects.filter(secondment=a, version__status=VStatus.APPROVED).exists()
    if c.status != CStatus.DRAFT and (not open_version or in_approved):
        _fail("A secondment is linked to HR on a draft contract or in a draft version only (R62).",
              "تُربط الإعارة بالموارد البشرية في مسودة العقد أو مسودة النسخة فقط.", "not_draft")
    hr_a = _hr_assignment(a.entity, hr_assignment_ref)
    if hr_a is None or hr_a.kind != "secondment" or hr_a.status not in ("approved", "active"):
        _fail("No approved or active HR secondment with this reference in this entity.",
              "لا توجد إعارة معتمدة أو سارية بهذا المرجع في هذا الكيان.", "hr_assignment")
    if (hr_a.receiving_entity_id or None) != (c.counterparty_entity_id or None):
        _fail("The HR receiving entity is not the contract's counterparty (R62).",
              "الكيان المستقبِل في الموارد البشرية ليس الطرف المقابل في العقد.", "receiving_entity")
    view = hr_services.billing_view(hr_a, user=user)
    a.hr_assignment_ref = view["hr_assignment_ref"]
    a.employee_ref = view["employee_ref"]
    a.role_title_en, a.role_title_ar = view["role_title_en"], view["role_title_ar"]
    a.start_date, a.end_date = view["start_date"], view["end_date"]
    a.hr_linked_at, a.hr_linked_by = timezone.now(), user
    _touch(a, user)
    _save(a)
    _event(c, Event.SECONDMENT_LINKED, user, secondment=a.pk, hr_assignment_ref=a.hr_assignment_ref)
    return a


def end_secondment(entity, assignment, end_date, user, source="manual"):
    """R19, R49, R57 to R60: record a secondment's end date (no CFO approval, D6).

    ``source="hr_event"``: HR's delivery (the HR user who ended it, ``hr.end_assignment``). ``source="manual"``:
    accounting staff (``billing.record_contract_event``), refused once the assignment is linked to HR (R59).
    Agents and system users are refused. The same end date again is a no-op; ``start_date − 1`` (HR cancellation)
    means nothing is billable. Planned periods after the end are cancelled, a later end puts them back, and
    already-billed days become draft credit (or debit) corrections. One transaction: a refusal changes nothing."""
    if source not in SecondmentAssignment.EndSource.values:
        _fail("Unknown source.", "مصدر غير معروف.", "source")
    a = SecondmentAssignment.objects.select_related("contract__entity").get(pk=assignment.pk)
    if a.entity_id != entity.pk:
        _fail("The secondment belongs to another entity.", "الإعارة تخص كيانًا آخر.", "entity_mismatch")
    permission = "hr.end_assignment" if source == "hr_event" else "billing.record_contract_event"
    access.require(user, a.entity, permission)
    _refuse_non_human(user, a.entity, permission)
    if source == "manual" and a.hr_assignment_ref:
        en, ar = _msg("linked_manual_end")
        access_services.log_denial(actor=user, entity=a.entity, permission=permission,
                                   details={"reason": "linked_to_hr", "hr_assignment_ref": a.hr_assignment_ref})
        _fail(en, ar, "linked_to_hr")
    if end_date is not None and end_date < a.start_date - timedelta(days=1):
        _fail("The end date is before the secondment start (R60).", "تاريخ النهاية قبل بداية الإعارة.",
              "secondment_dates")
    if a.end_date == end_date:
        return a  # R60: idempotent
    sys = _system_user()
    with transaction.atomic():
        a = SecondmentAssignment.objects.select_for_update(of=("self",)).get(pk=a.pk)
        c = Contract.objects.select_for_update(of=("self",)).select_related(
            "entity", "currency", "current_version").get(pk=a.contract_id)
        if a.end_date == end_date:
            return a
        previous = a.end_date
        a.end_date = end_date
        a.end_recorded_by, a.end_recorded_at, a.end_source = user, timezone.now(), source
        if source == "hr_event":
            a.last_hr_end_date = end_date
        _save(a)
        access_services.log(AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=a.entity, permission=permission,
                            details={"action": "end_secondment", "source": source, "secondment": a.pk,
                                     "hr_assignment_ref": a.hr_assignment_ref or "", "end_date": str(end_date)})
        if c.status == CStatus.DRAFT:
            return a  # R60: on a draft only the date is stored
        _entity_lock_now(c.entity)
        _sync_contract(c, sys)
        _event(c, Event.SECONDMENT_ENDED, user, secondment=a.pk, end_date=end_date, previous=previous, source=source)
        _rebuild_schedule(c)
        low = min(d for d in (previous, end_date) if d is not None)
        high = max(previous, end_date) if previous is not None else None
        result = _correction_pass(c, sys, trigger=f"secondment_end:{end_date}", version=c.current_version,
                                  affected=(low + timedelta(days=1), high), supersede=True)
        _notify_corrections(c, result)
    return a


def reopen_secondment(assignment, *, user, reason_ar, reason_en=""):
    """R61 (OQ-31 default): HR removed the end date and sent nothing; accounting staff reopen the linked
    secondment once HR shows it approved or active with no end date."""
    a = SecondmentAssignment.objects.select_related("contract__entity").get(pk=assignment.pk)
    access.require(user, a.entity, "billing.record_contract_event")
    _refuse_non_human(user, a.entity, "billing.record_contract_event")
    if not (reason_ar or "").strip():
        _fail("The reason in Arabic is required.", "السبب بالعربية مطلوب.", "reason_required")
    if not a.hr_assignment_ref:
        _fail("Only a secondment linked to HR is reopened; change an unlinked one with a new end date.",
              "يُعاد فتح الإعارة المرتبطة بالموارد البشرية فقط.", "not_linked")
    hr_a = _hr_assignment(a.entity, a.hr_assignment_ref)
    if hr_a is None or hr_a.status not in ("approved", "active") or hr_a.end_date is not None:
        _fail("HR still shows this secondment ended; reopen it in HR first (R61).",
              "لا تزال الإعارة منتهية في الموارد البشرية؛ أعد فتحها هناك أولًا.", "hr_still_ended")
    if a.end_date is None:
        return a
    sys = _system_user()
    with transaction.atomic():
        a = SecondmentAssignment.objects.select_for_update(of=("self",)).get(pk=a.pk)
        c = Contract.objects.select_for_update(of=("self",)).select_related(
            "entity", "currency", "current_version").get(pk=a.contract_id)
        previous = a.end_date
        a.end_date, a.end_recorded_by, a.end_recorded_at, a.end_source = None, user, timezone.now(), ""
        _save(a)
        if c.status == CStatus.DRAFT:
            return a
        _entity_lock_now(c.entity)
        _sync_contract(c, sys)
        _event(c, Event.SECONDMENT_REOPENED, user, secondment=a.pk, previous=previous, reason_ar=reason_ar)
        _rebuild_schedule(c)
        result = _correction_pass(c, sys, trigger="secondment_reopened", version=c.current_version,
                                  affected=(previous + timedelta(days=1), None), supersede=True)
        _notify_corrections(c, result)
    return a


# --- intercompany reconciliation (R64, 3.12) ---------------------------------------------------------------

def intercompany_reconciliation(as_of, *, user, group):
    """R64: one row per unordered pair of group entities and transaction currency (seller = the lower code).

    Positions come from ``ledger.services.intercompany_balances`` (``ledger.view`` on the group parent; rows only
    for entities where the user holds ``ledger.view``). A side the user cannot see is "not visible" (en/ar) and
    shows no amount. Billing columns cover documents billing created (invoices and notes), per issuing side, only
    where the user holds ``billing.view``. Tolerance 0 (OQ-30)."""
    from invoicing import services as invoicing_services
    from invoicing.models import SalesDocument
    from ledger import services as ledger_services

    access.require(user, group, "ledger.view")  # checked again by intercompany_balances (group report, access R6)
    balances = ledger_services.intercompany_balances(as_of, user=user, group=group, by_currency=True)
    tree = ledger_services._group_tree(group)
    visible = set(access.entities_for(user, "ledger.view").filter(pk__in=tree).values_list("code", flat=True))
    billing_visible = set(access.entities_for(user, "billing.view").filter(pk__in=tree).values_list("code", flat=True))
    positions = defaultdict(dict)
    for (ent, cp, cur), amount in balances.items():
        if cp is None:
            continue
        seller, buyer = sorted((ent, cp))
        positions[(seller, buyer, cur)][ent] = amount
    billing = defaultdict(lambda: {"open": ZERO, "unposted": ZERO})
    doc_ids = set(BillingPeriod.objects.filter(entity__code__in=billing_visible, sales_document__isnull=False)
                  .values_list("sales_document_id", flat=True))
    doc_ids |= set(BillingAdjustment.objects.filter(entity__code__in=billing_visible, document__isnull=False)
                   .values_list("document_id", flat=True))
    group_codes = set(Entity.objects.filter(pk__in=tree).values_list("code", flat=True))
    for doc in SalesDocument.objects.filter(pk__in=doc_ids, customer__group_entity__isnull=False,
                                            issue_date__lte=as_of, status__in=["issued", "final"]) \
            .select_related("entity", "customer__group_entity"):
        issuer, other = doc.entity.code, doc.customer.group_entity.code
        if other not in group_codes:
            continue
        sign = -1 if doc.doc_type == "credit_note" else 1
        if doc.status == "final":
            if access.has(user, doc.entity, "invoicing.view"):
                amount = invoicing_services.open_amount(doc, user=user)
            else:
                amount = doc.total_gross
            billing[(issuer, other, doc.currency_id)]["open"] += sign * amount
        else:
            billing[(issuer, other, doc.currency_id)]["unposted"] += sign * doc.total_gross
    keys = set(positions)
    for issuer, other, cur in billing:
        keys.add((*sorted((issuer, other)), cur))
    not_en, not_ar = _msg("not_visible")
    rows = []
    for seller, buyer, cur in sorted(keys):
        pos = positions.get((seller, buyer, cur), {})
        s_vis, b_vis = seller in visible, buyer in visible
        sp = pos.get(seller, ZERO) if s_vis else None
        bp = pos.get(buyer, ZERO) if b_vis else None
        out = billing.get((seller, buyer, cur), {"open": ZERO, "unposted": ZERO})
        back = billing.get((buyer, seller, cur), {"open": ZERO, "unposted": ZERO})
        if not any([sp, bp, out["open"], out["unposted"], back["open"], back["unposted"]]):
            continue
        rows.append({
            "as_of": as_of, "seller": seller, "buyer": buyer, "currency": cur,
            "seller_position": sp, "buyer_position": bp,
            "seller_visible": s_vis, "buyer_visible": b_vis,
            "seller_note_en": "" if s_vis else not_en, "seller_note_ar": "" if s_vis else not_ar,
            "buyer_note_en": "" if b_vis else not_en, "buyer_note_ar": "" if b_vis else not_ar,
            "difference": (sp + bp) if (s_vis and b_vis) else None,
            "billing_open": out["open"] if seller in billing_visible else None,
            "billing_unposted": out["unposted"] if seller in billing_visible else None,
            "buyer_billing_open": back["open"] if buyer in billing_visible else None,
            "buyer_billing_unposted": back["unposted"] if buyer in billing_visible else None,
        })
    return rows
