"""Intercompany billing services: the only supported way to create and change intercompany records.

Spec: docs/specs/intercompany-billing.md (rules R1-R22, D11, D12). Approvals plug in through
intercompany.approval (approvals spec §3.3); final approval issues and posts in the same transaction
(approvals D11). Journal entries are made only with ledger.services.create_entry(system=True, post=True)
from here, never written directly; the approval record holds the human approver (approvals review C).

Entry points
  agreements   create_agreement, update_agreement, add_rate, suspend_agreement, end_agreement
  timesheets   create_timesheet, update_timesheet, submit_timesheet, approve_timesheet, reject_timesheet
  cost pools   create_cost_pool, set_pool_accounts, compute_pool_period, set_driver
  feed (D9)    add_billable_item
  billing      run_billing, create_invoice_draft, update_invoice_draft, cancel_draft
  issue        issue_invoice (approved only; normally called by final approval)
  credit notes create_credit_note, uncredited_balance
  bills        update_bill_coding, dispute_bill, undispute_bill, submit_bill
  reporting    reconciliation_report, reconciliation_xlsx
  visibility   visible_agreements, visible_timesheets, visible_invoices, visible_bills, ...
"""
import calendar
import datetime
import uuid
from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from access import services as access
from access.errors import AccessDenied, BilingualError, Conflict, RuleError
from access.services import audit, snapshot
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine

from .models import (
    AgreementRate, AllocationDriver, BillableItem, BillingRun, CostPool, CostPoolPeriod, DraftEditor,
    IntercompanyBill, IntercompanyBillLine, IntercompanyInvoice, InvoiceLine, NumberSequence, ServiceAgreement,
    Source, TimesheetEntry, VatRate, workflow,
)

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

A = ServiceAgreement
INV = IntercompanyInvoice
BILL = IntercompanyBill
TS = TimesheetEntry

#: Accounts never allowed in a cost pool even if they are expense accounts (Q6 open; conservative).
POOL_EXCLUDED_ACCOUNT_CODES = {"5800"}  # Zakat

VAT_CATEGORY = {
    A.VatTreatment.STANDARD: InvoiceLine.VatCategory.STANDARD,
    A.VatTreatment.ZERO_RATED: InvoiceLine.VatCategory.ZERO,
    A.VatTreatment.EXEMPT: InvoiceLine.VatCategory.EXEMPT,
    A.VatTreatment.OUT_OF_SCOPE: InvoiceLine.VatCategory.OUT_OF_SCOPE,
}

# Ledger accounts used by this module (ledger/coa_template.py, spec §5).
DUE_FROM, DUE_TO, VAT_OUTPUT, VAT_INPUT = "1210", "2110", "2200", "1310"


def today():
    """Single clock for this module; tests replace it."""
    return timezone.localdate()


def money(value):
    """2 decimals, half-up (R6, R8)."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- helpers


def source_fields(user):
    """D10: source and source_agent for records created by `user` (None = the system)."""
    if user is None:
        return {"source": Source.IMPORT, "source_agent": ""}
    if user.user_type == "service_agent":
        return {"source": Source.AGENT, "source_agent": user.agent_name}
    if user.user_type == "service_integration":
        return {"source": Source.IMPORT, "source_agent": ""}
    return {"source": Source.HUMAN, "source_agent": ""}


def _invalid(exc):
    if hasattr(exc, "error_dict"):
        detail = {k: [str(m) for m in v] for k, v in exc.message_dict.items()}
    else:
        detail = {"__all__": [str(m) for m in exc.messages]}
    flat = "; ".join(f"{k}: {' '.join(v)}" for k, v in detail.items())
    return RuleError("invalid", f"Invalid data: {flat}", f"بيانات غير صالحة: {flat}", detail=detail)


def _clean(obj, exclude=None):
    try:
        obj.full_clean(exclude=exclude)
    except ValidationError as exc:
        raise _invalid(exc)


def _active_user(user):
    if user is None or not getattr(user, "is_active", False):
        raise AccessDenied("inactive", "You need an active user.", "تحتاج إلى مستخدم نشط.")


def _require(user, entity, perm):
    _active_user(user)
    access.require_perm(user, entity, perm)


def _require_human(user):
    if not user.is_human:
        raise AccessDenied("service_user_drafts_only", "Service users can only create drafts (R20).",
                           "يمكن لمستخدمي الخدمة إنشاء المسودات فقط.")


def _require_creator(user, doc):
    if doc.created_by_id != user.pk:
        raise AccessDenied("not_own_draft", "Only the creator can edit this draft.", "يمكن لمنشئ المسودة فقط تعديلها.")


def _audit(user, action, obj, *, before=None, after=None, entity=None, ctx=None):
    return audit(user or access.system_user(), action, obj, entity=entity, before=before, after=after, ctx=ctx)


def record_draft_editor(doc, user, action="edit"):
    """Remember who created/edited a draft: they cannot approve it (approvals R8, review A)."""
    if user is None or not getattr(user, "pk", None):
        return
    DraftEditor.objects.create(entity_id=doc.entity_id, object_type=doc._meta.label_lower, object_id=str(doc.pk),
                               user=user, action=action)


def draft_editor_ids(doc):
    return set(DraftEditor.objects.filter(object_type=doc._meta.label_lower, object_id=str(doc.pk))
               .values_list("user_id", flat=True))


def _move(doc, status, **fields):
    doc.status = status
    for k, v in fields.items():
        setattr(doc, k, v)
    with workflow(doc):
        doc.save()


def _next_number(entity, prefix, year, width):
    """Gap-free per entity, prefix and year; the row is locked until the transaction ends (R11)."""
    seq, _ = NumberSequence.objects.select_for_update().get_or_create(entity=entity, prefix=prefix, year=year)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"{entity.code}-{prefix}-{year}-{seq.last_number:0{width}d}"


def _posted_lines():
    return JournalLine.objects.filter(entry__status__in=[JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED])


# ---------------------------------------------------------------- visibility (R19)


def _visible(qs, user, view_perm, create_perm=None, own=None):
    """Records of entities where the user holds view_perm, plus own drafts where they hold create_perm."""
    if user is None or not user.is_authenticated or not user.is_active:
        return qs.none()
    q = Q(entity__in=access.entities_with_perm(user, view_perm))
    if create_perm:
        q |= Q(entity__in=access.entities_with_perm(user, create_perm)) & (own or Q(created_by=user))
    return qs.filter(q)


def visible_agreements(user):
    return _visible(A.objects.select_related("entity", "counterparty", "service_type"), user,
                    "intercompany.agreement.view", "intercompany.agreement.create")


def visible_timesheets(user):
    own = (Q(created_by=user) | Q(user=user)) if user is not None and user.is_authenticated else None
    return _visible(TS.objects.select_related("agreement"), user, "intercompany.agreement.view",
                    "intercompany.timesheet.create", own=own)


def visible_cost_pools(user):
    qs = CostPool.objects.all()
    return _visible(qs, user, "intercompany.agreement.view") | _visible(qs, user, "intercompany.costpool.manage")


def visible_billable_items(user):
    return _visible(BillableItem.objects.all(), user, "intercompany.agreement.view", "intercompany.invoice.create")


def visible_invoices(user):
    return _visible(INV.objects.select_related("entity", "counterparty", "agreement"), user,
                    "intercompany.invoice.view", "intercompany.invoice.create")


def visible_billing_runs(user):
    qs = BillingRun.objects.select_related("period")
    return _visible(qs, user, "intercompany.invoice.view") | _visible(qs, user, "intercompany.billing.run")


def visible_bills(user):
    """R19: bills live in the receiver; the issuer sees them only with rights on the receiver."""
    return _visible(BILL.objects.select_related("entity", "counterparty", "invoice"), user, "intercompany.bill.view")


# ---------------------------------------------------------------- accounts


def _account(entity, code, type_, what_en, what_ar):
    acc = Account.objects.filter(entity=entity, code=code).first()
    if acc is None or acc.type != type_ or not acc.is_postable or not acc.is_active:
        raise RuleError("account_invalid", f"{what_en} {code} is not an open {type_} account of {entity.code}.",
                        f"{what_ar} {code} ليس حساباً مفتوحاً من النوع المطلوب في {entity.code}.")
    if acc.sharia_prohibited:
        raise RuleError("sharia_prohibited", f"Account {code} is Sharia-prohibited (R18).", f"الحساب {code} محظور شرعاً.")
    if acc.requires_purification:
        raise RuleError("account_invalid", f"Account {code} is for non-compliant income only.",
                        f"الحساب {code} مخصص للإيرادات غير المتوافقة فقط.")
    return acc


def income_account_for(agreement):
    code = agreement.income_account_code or agreement.service_type.income_account_code
    return _account(agreement.entity, code, Account.Type.INCOME, "Income account", "حساب الإيراد")


def expense_account_for(agreement):
    """Receiver's default expense account (spec 5.6, overridable per agreement)."""
    code = agreement.expense_account_code or agreement.service_type.expense_account_code
    return _account(agreement.counterparty, code, Account.Type.EXPENSE, "Expense account", "حساب المصروف")


# ---------------------------------------------------------------- agreements (3.3)

AGREEMENT_EDITABLE = {
    "counterparty", "service_type", "description_en", "description_ar", "pricing_method", "hourly_rate", "fixed_fee",
    "markup_pct", "cost_pool", "allocation_basis", "allocation_pct", "vat_treatment", "vat_exemption_reason_en",
    "vat_exemption_reason_ar", "billing_frequency", "payment_terms_days", "start_date", "end_date",
    "income_account_code", "expense_account_code", "tp_method", "tp_documentation_ref", "source_type", "source_ref",
}


def _check_fields(fields, allowed):
    unknown = set(fields) - allowed
    if unknown:
        raise RuleError("field_not_editable", f"These fields cannot be set: {', '.join(sorted(unknown))}.",
                        f"لا يمكن تعيين هذه الحقول: {', '.join(sorted(unknown))}.", detail=sorted(unknown))


def validate_agreement(agreement):
    """Everything an agreement must satisfy before approval and billing (R1, R2, R18)."""
    _clean(agreement)
    if not agreement.service_type.is_active:
        raise RuleError("service_type_inactive", "The service type is inactive.", "نوع الخدمة غير نشط.")
    income_account_for(agreement)
    if agreement.counterparty.books_in_erp:
        expense_account_for(agreement)
    if agreement.pricing_method == A.Pricing.COST_PLUS:
        if not agreement.cost_pool.is_active:
            raise RuleError("pool_inactive", "The cost pool is inactive.", "مجمع التكاليف غير نشط.")
        if agreement.allocation_basis == A.AllocationBasis.FIXED_PCT:
            others = (A.objects.filter(cost_pool=agreement.cost_pool, allocation_basis=A.AllocationBasis.FIXED_PCT,
                                       status__in=[A.Status.PENDING_APPROVAL, A.Status.ACTIVE, A.Status.SUSPENDED])
                      .exclude(pk=agreement.pk).aggregate(t=Sum("allocation_pct"))["t"] or ZERO)
            if others + agreement.allocation_pct > HUNDRED:
                raise RuleError("allocation_over_100",
                                f"Fixed allocations of pool {agreement.cost_pool.code} would exceed 100% "
                                f"({others + agreement.allocation_pct}%).",
                                f"تتجاوز نسب توزيع المجمع {agreement.cost_pool.code} نسبة 100%.")


@transaction.atomic
def create_agreement(user, *, entity, counterparty, service_type, ctx=None, **fields):
    _require(user, entity, "intercompany.agreement.create")
    _check_fields(fields, AGREEMENT_EDITABLE)
    a = A(entity=entity, counterparty=counterparty, service_type=service_type, currency=entity.base_currency,
          created_by=user, **source_fields(user), **fields)
    a.number = _next_number(entity, "AGR", today().year, 4)
    _clean(a)
    income_account_for(a)
    a.save()
    record_draft_editor(a, user, "create")
    _audit(user, "intercompany.agreement.create", a, after=snapshot(a), ctx=ctx)
    return a


@transaction.atomic
def update_agreement(user, agreement, ctx=None, **fields):
    agreement = A.objects.select_for_update().get(pk=agreement.pk)
    _require(user, agreement.entity, "intercompany.agreement.create")
    _require_creator(user, agreement)
    if agreement.status != A.Status.DRAFT:
        raise Conflict("not_draft", "Only draft agreements can be edited.", "يمكن تعديل مسودات الاتفاقيات فقط.")
    _check_fields(fields, AGREEMENT_EDITABLE)
    before = snapshot(agreement)
    for k, v in fields.items():
        setattr(agreement, k, v)
    _clean(agreement)
    agreement.save()
    record_draft_editor(agreement, user, "edit")
    _audit(user, "intercompany.agreement.update", agreement, before=before, after=snapshot(agreement), ctx=ctx)
    return agreement


@transaction.atomic
def add_rate(user, agreement, *, role_en, role_ar, hourly_rate, valid_from, valid_to=None, ctx=None):
    agreement = A.objects.select_for_update().get(pk=agreement.pk)
    _require(user, agreement.entity, "intercompany.agreement.create")
    _require_creator(user, agreement)
    if agreement.pricing_method != A.Pricing.TIMESHEET:
        raise RuleError("not_timesheet", "Rates are for timesheet agreements.", "الأسعار لاتفاقيات الساعات فقط.")
    r = AgreementRate(entity=agreement.entity, agreement=agreement, role_en=role_en, role_ar=role_ar,
                      hourly_rate=hourly_rate, valid_from=valid_from, valid_to=valid_to)
    _clean(r)
    r.save()
    record_draft_editor(agreement, user, "edit")
    _audit(user, "intercompany.agreement.add_rate", agreement, after=snapshot(r), ctx=ctx)
    return r


@transaction.atomic
def suspend_agreement(user, agreement, reason, ctx=None):
    """Stops billing. Resuming needs a new approval (submit the suspended agreement)."""
    agreement = A.objects.select_for_update().get(pk=agreement.pk)
    _require(user, agreement.entity, "intercompany.agreement.manage")
    _require_human(user)
    if not (reason or "").strip():
        raise RuleError("reason_required", "A reason is required.", "السبب مطلوب.")
    if agreement.status != A.Status.ACTIVE:
        raise Conflict("not_active", "Only active agreements can be suspended.", "يمكن إيقاف الاتفاقيات النشطة فقط.")
    before = snapshot(agreement)
    _move(agreement, A.Status.SUSPENDED)
    _audit(user, "intercompany.agreement.suspend", agreement, before=before,
           after=snapshot(agreement) | {"reason": reason}, ctx=ctx)
    return agreement


@transaction.atomic
def end_agreement(user, agreement, end_date, reason, ctx=None):
    agreement = A.objects.select_for_update().get(pk=agreement.pk)
    _require(user, agreement.entity, "intercompany.agreement.manage")
    _require_human(user)
    if not (reason or "").strip():
        raise RuleError("reason_required", "A reason is required.", "السبب مطلوب.")
    if agreement.status not in (A.Status.ACTIVE, A.Status.SUSPENDED):
        raise Conflict("not_active", "Only active or suspended agreements can be ended.",
                       "يمكن إنهاء الاتفاقيات النشطة أو الموقوفة فقط.")
    if end_date < agreement.start_date or (agreement.end_date and end_date > agreement.end_date):
        raise RuleError("end_date_invalid", "The end date must be within the agreement dates.",
                        "يجب أن يكون تاريخ الانتهاء ضمن مدة الاتفاقية.")
    billed_after = agreement.invoices.filter(kind=INV.Kind.INVOICE, supply_date_from__gt=end_date) \
        .exclude(status__in=INV.DEAD).exists()
    if billed_after:
        raise RuleError("billed_after_end", "Invoices exist after this end date; credit them first.",
                        "توجد فواتير بعد تاريخ الانتهاء؛ أصدر إشعارات دائنة أولاً.")
    before = snapshot(agreement)
    _move(agreement, A.Status.ENDED, end_date=end_date)
    _audit(user, "intercompany.agreement.end", agreement, before=before,
           after=snapshot(agreement) | {"reason": reason}, ctx=ctx)
    return agreement


# ---------------------------------------------------------------- timesheets (3.4, R5)

TIMESHEET_EDITABLE = {"date", "hours", "description_en", "description_ar", "rate"}


@transaction.atomic
def create_timesheet(user, agreement, *, worker=None, ctx=None, **fields):
    """Billing clerks record their own entries; holders of timesheet.approve record for anyone; agents
    draft entries for a worker, who (or an approver) submits them (R20)."""
    agreement = A.objects.get(pk=agreement.pk)
    _require(user, agreement.entity, "intercompany.timesheet.create")
    _check_fields(fields, TIMESHEET_EDITABLE)
    worker = worker or user
    if worker.pk != user.pk and user.is_human and not access.has_perm(user, agreement.entity,
                                                                        "intercompany.timesheet.approve"):
        raise AccessDenied("own_entries_only", "You can record your own hours only.", "يمكنك تسجيل ساعاتك فقط.")
    if not worker.is_human:
        raise RuleError("worker_not_human", "Timesheets record hours of people.", "سجلات الوقت لساعات الأشخاص.")
    if agreement.status != A.Status.ACTIVE:
        raise Conflict("agreement_not_active", "The agreement is not active.", "الاتفاقية غير نشطة.")
    e = TS(entity=agreement.entity, agreement=agreement, user=worker, created_by=user, **source_fields(user), **fields)
    _clean(e, exclude=["invoice_line"])
    e.save()
    _audit(user, "intercompany.timesheet.create", e, after=snapshot(e), ctx=ctx)
    return e


@transaction.atomic
def update_timesheet(user, entry, ctx=None, **fields):
    entry = TS.objects.select_for_update().get(pk=entry.pk)
    _require(user, entry.entity, "intercompany.timesheet.create")
    if user.pk not in (entry.created_by_id, entry.user_id):
        raise AccessDenied("not_own_entry", "You can edit your own entries only.", "يمكنك تعديل سجلاتك فقط.")
    if entry.status != TS.Status.DRAFT:
        raise Conflict("not_draft", "Only draft entries can be edited.", "يمكن تعديل المسودات فقط.")
    _check_fields(fields, TIMESHEET_EDITABLE)
    before = snapshot(entry)
    for k, v in fields.items():
        setattr(entry, k, v)
    _clean(entry)
    entry.save()
    _audit(user, "intercompany.timesheet.update", entry, before=before, after=snapshot(entry), ctx=ctx)
    return entry


@transaction.atomic
def submit_timesheet(user, entry, ctx=None):
    entry = TS.objects.select_for_update().get(pk=entry.pk)
    _require(user, entry.entity, "intercompany.timesheet.create")
    _require_human(user)
    if user.pk not in (entry.user_id, entry.created_by_id) and not access.has_perm(
            user, entry.entity, "intercompany.timesheet.approve"):
        raise AccessDenied("not_own_entry", "You can submit your own entries only.", "يمكنك تقديم سجلاتك فقط.")
    if entry.status != TS.Status.DRAFT:
        raise Conflict("not_draft", "Only draft entries can be submitted.", "يمكن تقديم المسودات فقط.")
    _clean(entry)
    _move(entry, TS.Status.SUBMITTED)
    _audit(user, "intercompany.timesheet.submit", entry, after={"status": entry.status}, ctx=ctx)
    return entry


@transaction.atomic
def approve_timesheet(user, entry, ctx=None):
    entry = TS.objects.select_for_update().get(pk=entry.pk)
    _require(user, entry.entity, "intercompany.timesheet.approve")
    _require_human(user)
    if user.pk in (entry.user_id, entry.created_by_id):
        raise AccessDenied("own_entry", "You cannot approve your own hours.", "لا يمكنك اعتماد ساعاتك.")
    if entry.status != TS.Status.SUBMITTED:
        raise Conflict("not_submitted", "Only submitted entries can be approved.", "يمكن اعتماد السجلات المقدمة فقط.")
    _move(entry, TS.Status.APPROVED, approved_by=user, approved_at=timezone.now())
    _audit(user, "intercompany.timesheet.approve", entry, after={"status": entry.status}, ctx=ctx)
    return entry


@transaction.atomic
def reject_timesheet(user, entry, reason, ctx=None):
    entry = TS.objects.select_for_update().get(pk=entry.pk)
    _require(user, entry.entity, "intercompany.timesheet.approve")
    _require_human(user)
    if not (reason or "").strip():
        raise RuleError("reason_required", "A reason is required.", "السبب مطلوب.")
    if entry.status != TS.Status.SUBMITTED:
        raise Conflict("not_submitted", "Only submitted entries can be rejected.", "يمكن رفض السجلات المقدمة فقط.")
    _move(entry, TS.Status.REJECTED)
    _audit(user, "intercompany.timesheet.reject", entry, after={"status": entry.status, "reason": reason}, ctx=ctx)
    return entry


# ---------------------------------------------------------------- cost pools (3.5, R6, R7, D12)


def validate_pool_accounts(pool, accounts):
    """R7: same entity, expense type, postable, never Sharia-prohibited (and not Zakat, Q6)."""
    for acc in accounts:
        if acc.entity_id != pool.entity_id:
            raise RuleError("pool_account_entity", f"Account {acc.code} belongs to another entity.",
                            f"الحساب {acc.code} يتبع منشأة أخرى.")
        if acc.type != Account.Type.EXPENSE or not acc.is_postable:
            raise RuleError("pool_account_type", f"Account {acc.code} is not a postable expense account.",
                            f"الحساب {acc.code} ليس حساب مصروف قابلاً للترحيل.")
        if acc.sharia_prohibited:
            raise RuleError("sharia_prohibited", f"Account {acc.code} ({acc.name_en}) is Sharia-prohibited (R7).",
                            f"الحساب {acc.code} ({acc.name_ar}) محظور شرعاً.")
        if acc.code in POOL_EXCLUDED_ACCOUNT_CODES:
            raise RuleError("pool_account_excluded", f"Account {acc.code} cannot be recharged (Q6).",
                            f"لا يمكن إعادة تحميل الحساب {acc.code}.")


@transaction.atomic
def create_cost_pool(user, entity, *, code, name_en, name_ar, accounts=(), ctx=None):
    _require(user, entity, "intercompany.costpool.manage")
    pool = CostPool(entity=entity, code=code, name_en=name_en, name_ar=name_ar, created_by=user, **source_fields(user))
    _clean(pool)
    validate_pool_accounts(pool, accounts)
    pool.save()
    pool.accounts.set(accounts)
    _audit(user, "intercompany.costpool.create", pool, after=snapshot(pool) | {"accounts": [a.code for a in accounts]},
           ctx=ctx)
    return pool


@transaction.atomic
def set_pool_accounts(user, pool, accounts, ctx=None):
    _require(user, pool.entity, "intercompany.costpool.manage")
    validate_pool_accounts(pool, accounts)
    before = sorted(pool.accounts.values_list("code", flat=True))
    pool.accounts.set(accounts)
    _audit(user, "intercompany.costpool.set_accounts", pool, before={"accounts": before},
           after={"accounts": sorted(a.code for a in accounts)}, ctx=ctx)
    return pool


def pool_cost(pool, period):
    """R7: posted ledger lines of the issuer on the pool's accounts in the period (reversals net out)."""
    totals = _posted_lines().filter(
        entry__entity=pool.entity, account__in=pool.accounts.all(),
        entry__date__gte=period.start_date, entry__date__lte=period.end_date,
    ).aggregate(d=Sum("debit"), c=Sum("credit"))
    return money((totals["d"] or ZERO) - (totals["c"] or ZERO))


def compute_pool_period(pool, period, user=None, ctx=None):
    """Compute (or recompute while unlocked) the pool cost for a period. Locked periods are returned as is."""
    if period.entity_id != pool.entity_id:
        raise RuleError("period_entity", "The period belongs to another entity.", "الفترة تتبع منشأة أخرى.")
    with transaction.atomic():
        cpp = CostPoolPeriod.objects.select_for_update().filter(cost_pool=pool, period=period).first()
        if cpp is None:
            cpp = CostPoolPeriod(entity=pool.entity, cost_pool=pool, period=period, created_by=user,
                                 **source_fields(user))
        if cpp.locked:
            return cpp
        before = cpp.amount if cpp.pk else None
        cpp.amount = pool_cost(pool, period)
        cpp.computed_at = timezone.now()
        cpp.save()
        if before != cpp.amount:
            _audit(user, "intercompany.costpool.compute", cpp, before={"amount": before}, after={"amount": cpp.amount},
                   ctx=ctx)
        return cpp


@transaction.atomic
def set_driver(user, pool, period, basis, target_entity, value, ctx=None):
    """D12: record headcount or revenue of an entity sharing the pool in a period."""
    _require(user, pool.entity, "intercompany.costpool.manage")
    d = AllocationDriver.objects.filter(cost_pool=pool, period=period, basis=basis, target_entity=target_entity).first()
    before = snapshot(d) if d else None
    if d is None:
        d = AllocationDriver(entity=pool.entity, cost_pool=pool, period=period, basis=basis,
                             target_entity=target_entity, created_by=user, **source_fields(user))
    d.value = Decimal(value)
    _clean(d)
    d.save()
    _audit(user, "intercompany.costpool.set_driver", d, before=before, after=snapshot(d), ctx=ctx)
    return d


def allocation_share(agreement, period):
    """D12: (share as a fraction, text) of the pool billed to the agreement's counterparty."""
    if agreement.allocation_basis == A.AllocationBasis.FIXED_PCT:
        return agreement.allocation_pct / HUNDRED, f"{agreement.allocation_pct.normalize():f}%"
    rows = AllocationDriver.objects.filter(cost_pool=agreement.cost_pool, period=period,
                                           basis=agreement.allocation_basis)
    total = rows.aggregate(t=Sum("value"))["t"] or ZERO
    mine = rows.filter(target_entity=agreement.counterparty).first()
    if mine is None or total <= ZERO:
        raise RuleError("drivers_missing",
                        f"No {agreement.allocation_basis} drivers for {agreement.counterparty.code} in {period.name}.",
                        f"لا توجد أسس توزيع ({agreement.allocation_basis}) للمنشأة {agreement.counterparty.code}.")
    return mine.value / total, f"{agreement.allocation_basis} {mine.value.normalize():f}/{total.normalize():f}"


# ---------------------------------------------------------------- billable items (3.6, D9)


@transaction.atomic
def add_billable_item(user, agreement, *, period_start, period_end, description_en, description_ar, quantity,
                      unit_price, source_type="", source_ref="", ctx=None):
    """Feeding modules (rental leases later) add lines for an agreement; billed by the next run."""
    agreement = A.objects.get(pk=agreement.pk)
    if agreement.status not in (A.Status.ACTIVE, A.Status.SUSPENDED):
        raise Conflict("agreement_not_active", "The agreement is not active.", "الاتفاقية غير نشطة.")
    if user is not None:
        _require(user, agreement.entity, "intercompany.invoice.create")
    item = BillableItem(entity=agreement.entity, agreement=agreement, period_start=period_start,
                        period_end=period_end, description_en=description_en, description_ar=description_ar,
                        quantity=Decimal(quantity), unit_price=Decimal(unit_price), source_type=source_type,
                        source_ref=source_ref, created_by=user, **source_fields(user))
    _clean(item, exclude=["invoice_line"])
    item.save()
    _audit(user, "intercompany.billable_item.create", item, after=snapshot(item), ctx=ctx)
    return item


# ---------------------------------------------------------------- VAT (R8)


def vat_rate_on(day, country="SA"):
    rate = (VatRate.objects.filter(country=country, valid_from__lte=day)
            .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=day)).order_by("-valid_from").first())
    if rate is None:
        raise RuleError("no_vat_rate", f"No VAT rate in tax settings for {day}.", f"لا توجد نسبة ضريبة في {day}.")
    return rate.rate


def _vat(treatment, supply_date):
    rate = vat_rate_on(supply_date) if treatment == A.VatTreatment.STANDARD else ZERO
    return rate, VAT_CATEGORY[treatment]


# ---------------------------------------------------------------- billing (R3-R9, D12)


def _is_calendar_month(period):
    last = calendar.monthrange(period.start_date.year, period.start_date.month)[1]
    return period.start_date.day == 1 and period.end_date == period.start_date.replace(day=last)


def supply_window(agreement, period):
    """(billing_period key, supply start, supply end) when the agreement bills in this fiscal month, else None."""
    F = A.Frequency
    end = period.end_date
    if agreement.billing_frequency == F.MONTHLY:
        return f"{end:%Y-%m}", period.start_date, end
    if agreement.billing_frequency == F.QUARTERLY:
        if end.month % 3:
            return None
        return f"{end.year}-Q{end.month // 3}", datetime.date(end.year, end.month - 2, 1), end
    if agreement.billing_frequency == F.ANNUAL:
        if end.month != 12:
            return None
        return f"{end.year}", datetime.date(end.year, 1, 1), end
    if not (period.start_date <= agreement.start_date <= end):  # one-off: the month it starts
        return None
    return f"{end:%Y-%m}", agreement.start_date, agreement.end_date or agreement.start_date


def _coverage(agreement, start, end):
    full = agreement.start_date <= start and (agreement.end_date is None or agreement.end_date >= end)
    overlap = agreement.start_date <= end and (agreement.end_date is None or agreement.end_date >= start)
    return full, overlap


def _line(agreement, en, ar, quantity, unit_price, net, basis, supply_to):
    rate, category = _vat(agreement.vat_treatment, supply_to)
    return {
        "description_en": en[:300], "description_ar": ar[:300], "quantity": Decimal(quantity),
        "unit_price": Decimal(unit_price), "net_amount": money(net), "vat_treatment": agreement.vat_treatment,
        "vat_category": category, "vat_rate": rate, "vat_amount": money(money(net) * rate / HUNDRED),
        "vat_exemption_reason_en": agreement.vat_exemption_reason_en,
        "vat_exemption_reason_ar": agreement.vat_exemption_reason_ar, "pricing_basis": basis[:200],
    }


def _build_lines(agreement, period, key, start, end):
    """Return [(line dict, links)] for the agreement and supply window. links: what the line bills."""
    P = A.Pricing
    out = []
    full, overlap = _coverage(agreement, start, end)
    if agreement.pricing_method in (P.FIXED_FEE, P.COST_PLUS) and agreement.billing_frequency != A.Frequency.ONE_OFF \
            and not full:
        raise RuleError("partial_period",
                        "The agreement does not cover the whole supply period; pro-rating is not decided (R3).",
                        "لا تغطي الاتفاقية فترة التوريد كاملة؛ التناسب الزمني غير مقرر.")
    if not overlap:
        raise RuleError("outside_dates", "The agreement dates do not cover the supply period (R3).",
                        "تواريخ الاتفاقية لا تغطي فترة التوريد.")
    desc_en, desc_ar = agreement.description_en, agreement.description_ar
    if agreement.pricing_method == P.FIXED_FEE:
        fee = agreement.fixed_fee
        out.append((_line(agreement, f"{desc_en} ({key})", f"{desc_ar} ({key})", 1, fee, fee,
                          f"fixed fee {fee:,.2f} / {key}", end), {}))
    elif agreement.pricing_method == P.TIMESHEET:
        entries = list(TS.objects.select_for_update().filter(
            agreement=agreement, status=TS.Status.APPROVED, invoice_line__isnull=True, date__gte=start, date__lte=end,
        ).select_related("rate").order_by("date", "id"))
        groups = OrderedDict()
        for e in entries:
            groups.setdefault(e.rate_id, []).append(e)
        for rate_id, group in groups.items():
            rate = group[0].rate
            unit = rate.hourly_rate if rate else agreement.hourly_rate
            hours = sum((e.hours for e in group), ZERO)
            role_en = f" - {rate.role_en}" if rate else ""
            role_ar = f" - {rate.role_ar}" if rate else ""
            out.append((_line(agreement, f"{desc_en}{role_en} ({start} to {end})", f"{desc_ar}{role_ar} ({start} - {end})",
                              hours, unit, hours * unit, f"{hours.normalize():f} h x {unit:,.2f}", end),
                        {"timesheets": group}))
    elif agreement.pricing_method == P.COST_PLUS:
        cpp = compute_pool_period(agreement.cost_pool, period)
        cpp = CostPoolPeriod.objects.select_for_update().get(pk=cpp.pk)
        if cpp.amount <= ZERO:
            raise RuleError("pool_empty", f"Cost pool {agreement.cost_pool.code} has no cost in {period.name}.",
                            f"لا توجد تكاليف في المجمع {agreement.cost_pool.code} للفترة {period.name}.")
        share, share_text = allocation_share(agreement, period)
        markup = agreement.markup_pct
        net = money(cpp.amount * share * (1 + markup / HUNDRED))  # R6, one rounding at the end
        out.append((_line(agreement, f"{desc_en} ({key})", f"{desc_ar} ({key})", 1, net, net,
                          f"cost {cpp.amount:,.2f} x {share_text} + {markup.normalize():f}%", end), {"pool": cpp}))
    items = BillableItem.objects.select_for_update().filter(
        agreement=agreement, status=BillableItem.Status.OPEN, period_end__lte=end).order_by("period_start", "id")
    for item in items:
        out.append((_line(agreement, item.description_en, item.description_ar, item.quantity, item.unit_price,
                          item.quantity * item.unit_price,
                          f"{item.quantity.normalize():f} x {item.unit_price.normalize():f}", end), {"item": item}))
    return out


def _live_invoice(agreement, key):
    return INV.objects.filter(agreement=agreement, billing_period=key, kind=INV.Kind.INVOICE) \
        .exclude(status__in=INV.DEAD).first()


def _draft_invoice(agreement, period, user, run=None):
    """Build one draft invoice (R9: drafts only, nothing posted). Raises RuleError when nothing to bill."""
    if agreement.status != A.Status.ACTIVE:
        raise RuleError("agreement_not_active", f"Agreement {agreement.number} is {agreement.status}.",
                        f"الاتفاقية {agreement.number} غير نشطة.")
    window = supply_window(agreement, period)
    if window is None:
        raise RuleError("not_due", f"Not billed in {period.name} ({agreement.billing_frequency}).",
                        f"لا تُفوتر في {period.name}.")
    key, start, end = window
    if _live_invoice(agreement, key):
        raise Conflict("already_billed", f"{agreement.number} already billed for {key} (R4).",
                       f"تمت فوترة {agreement.number} للفترة {key}.")
    lines = _build_lines(agreement, period, key, start, end)
    if not lines:
        raise RuleError("nothing_to_bill", "Nothing to bill for the period.", "لا يوجد ما يُفوتر لهذه الفترة.")
    income = income_account_for(agreement)
    issue_date = period.end_date  # D12: dated the last day of the service month
    inv = INV(
        entity=agreement.entity, counterparty=agreement.counterparty, agreement=agreement, kind=INV.Kind.INVOICE,
        issue_date=issue_date, supply_date_from=start, supply_date_to=end,
        due_date=issue_date + datetime.timedelta(days=agreement.payment_terms_days), billing_period=key,
        currency=agreement.currency, billing_run=run, invoice_type_code="388",
        seller_vat_number=agreement.entity.vat_number, buyer_vat_number=agreement.counterparty.vat_number,
        created_by=user, **source_fields(user),
    )
    try:
        with transaction.atomic():
            inv.save()
    except IntegrityError:
        raise Conflict("already_billed", f"{agreement.number} already billed for {key} (R4).",
                       f"تمت فوترة {agreement.number} للفترة {key}.")
    for data, links in lines:
        line = InvoiceLine(entity=inv.entity, invoice=inv, agreement=agreement, income_account=income,
                           cost_pool_period=links.get("pool"), **data)
        line.save()
        for e in links.get("timesheets", []):
            _move(e, TS.Status.BILLED, invoice_line=line)  # R5
        if "item" in links:
            _move(links["item"], BillableItem.Status.BILLED, invoice_line=line)
        if "pool" in links and not links["pool"].locked:
            links["pool"].locked = True  # R6
            with workflow(links["pool"]):
                links["pool"].save()
    _set_totals(inv)
    if user is not None:
        record_draft_editor(inv, user, "create")
    return inv


def _set_totals(inv):
    agg = inv.lines.aggregate(n=Sum("net_amount"), v=Sum("vat_amount"))
    inv.net_total = money(agg["n"] or ZERO)
    inv.vat_total = money(agg["v"] or ZERO)
    inv.gross_total = money(inv.net_total + inv.vat_total)
    inv.save()


def _check_run_period(entity, period):
    period = FiscalPeriod.objects.get(pk=period.pk)  # never trust a caller's stale copy of the status
    if period.entity_id != entity.pk:
        raise RuleError("period_entity", "The period belongs to another entity.", "الفترة تتبع منشأة أخرى.")
    if not _is_calendar_month(period):
        raise RuleError("period_not_month", "Billing runs are monthly (calendar months).", "الفوترة شهرية.")
    if period.end_date >= today():
        raise RuleError("period_not_finished", f"{period.name} has not ended yet.", f"لم تنتهِ الفترة {period.name} بعد.")
    if period.status != FiscalPeriod.Status.OPEN:
        raise RuleError("period_closed", f"Period {period.name} is closed for {entity.code}.",
                        f"الفترة {period.name} مقفلة للمنشأة {entity.code}.")
    return period


def run_billing(user, entity, period, ctx=None):
    """Monthly billing run (R3, R4, R9, D12). Idempotent: already-billed agreements are logged and skipped.
    Creates drafts only; nothing is posted. An unexpected error rolls the run back and records it as failed."""
    _require(user, entity, "intercompany.billing.run")
    _require_human(user)
    period = _check_run_period(entity, period)
    try:
        with transaction.atomic():
            return _run(user, entity, period, ctx)
    except BilingualError:
        raise
    except Exception as exc:
        with transaction.atomic():
            run = BillingRun.objects.create(entity=entity, period=period, run_date=today(),
                                            status=BillingRun.Status.FAILED, created_by=user, **source_fields(user),
                                            log=[{"error": type(exc).__name__}])
            _audit(user, "intercompany.billing.failed", run, after={"error": type(exc).__name__}, ctx=ctx)
        raise


def _run(user, entity, period, ctx):
    run = BillingRun.objects.create(entity=entity, period=period, run_date=today(), created_by=user,
                                    **source_fields(user))
    log, created = [], 0
    agreements = (A.objects.select_for_update().filter(entity=entity, start_date__lte=period.end_date)
                  .filter(Q(end_date__isnull=True) | Q(end_date__gte=period.start_date))
                  .exclude(status=A.Status.DRAFT).order_by("number"))
    for a in agreements:
        row = {"agreement": a.number, "counterparty": a.counterparty.code}
        try:
            with transaction.atomic():
                inv = _draft_invoice(a, period, user, run)
            created += 1
            row.update(result="created", invoice_id=inv.pk, gross=str(inv.gross_total))
        except Conflict as exc:
            row.update(result="already billed" if exc.code == "already_billed" else "skipped", reason=exc.message_en)
        except RuleError as exc:
            row.update(result="skipped", code=exc.code, reason=exc.message_en)
        log.append(row)
    run.status = BillingRun.Status.COMPLETED
    run.invoices_created = created
    run.log = log
    run.save()
    _audit(user, "intercompany.billing.run", run, after={"period": period.name, "created": created, "log": log},
           ctx=ctx)
    return run


@transaction.atomic
def create_invoice_draft(user, agreement, period, ctx=None):
    """One agreement, one period, outside a run (agents may do this: drafts only, R20)."""
    _require(user, agreement.entity, "intercompany.invoice.create")
    agreement = A.objects.select_for_update().get(pk=agreement.pk)
    period = _check_run_period(agreement.entity, period)
    inv = _draft_invoice(agreement, period, user)
    _audit(user, "intercompany.invoice.create", inv, after=snapshot(inv), ctx=ctx)
    return inv


@transaction.atomic
def update_invoice_draft(user, invoice, *, lines=None, credit_reason_en=None, credit_reason_ar=None, ctx=None):
    """Draft edits are text only: line descriptions (both languages) and credit-note reasons. Amounts come
    from the agreement and billed records; to change them cancel the draft and bill again."""
    invoice = INV.objects.select_for_update().get(pk=invoice.pk)
    _require(user, invoice.entity, "intercompany.invoice.create")
    _require_creator(user, invoice)
    if invoice.status != INV.Status.DRAFT:
        raise Conflict("not_draft", "Only drafts can be edited.", "يمكن تعديل المسودات فقط.")
    before = invoice_snapshot(invoice)
    for line_id, text in (lines or {}).items():
        line = invoice.lines.get(pk=line_id)
        line.description_en = text.get("description_en", line.description_en)
        line.description_ar = text.get("description_ar", line.description_ar)
        if not line.description_en.strip() or not line.description_ar.strip():
            raise RuleError("bilingual_required", "Line descriptions need English and Arabic (R21).",
                            "وصف البند مطلوب بالعربية والإنجليزية.")
        line.save()
    if invoice.is_credit_note:
        if credit_reason_en is not None:
            invoice.credit_reason_en = credit_reason_en
        if credit_reason_ar is not None:
            invoice.credit_reason_ar = credit_reason_ar
        invoice.save()
    record_draft_editor(invoice, user, "edit")
    _audit(user, "intercompany.invoice.update", invoice, before=before, after=invoice_snapshot(invoice), ctx=ctx)
    return invoice


def invoice_snapshot(inv):
    return snapshot(inv) | {"lines": [
        [l.pk, l.description_en, l.description_ar, str(l.net_amount), str(l.vat_amount)] for l in inv.lines.all()]}


def _release(inv):
    """R14: give back timesheets, billable items and pool locks billed by a cancelled/rejected draft."""
    for line in inv.lines.all():
        for e in TS.objects.filter(invoice_line=line):
            _move(e, TS.Status.APPROVED, invoice_line=None)
        for item in BillableItem.objects.filter(invoice_line=line):
            _move(item, BillableItem.Status.OPEN, invoice_line=None)
        cpp = line.cost_pool_period
        if cpp is not None:
            still = InvoiceLine.objects.filter(cost_pool_period=cpp).exclude(invoice=inv) \
                .exclude(invoice__status__in=INV.DEAD).exists()
            if not still:
                cpp.locked = False
                with workflow(cpp):
                    cpp.save()


@transaction.atomic
def cancel_draft(user, invoice, reason="", ctx=None):
    invoice = INV.objects.select_for_update().get(pk=invoice.pk)
    _require(user, invoice.entity, "intercompany.invoice.create")
    if not user.is_human:
        _require_creator(user, invoice)
    if invoice.status != INV.Status.DRAFT:
        raise Conflict("not_draft", "Only drafts can be cancelled; pending requests must be cancelled first.",
                       "يمكن إلغاء المسودات فقط؛ ألغِ طلب الاعتماد أولاً.")
    _release(invoice)
    _move(invoice, INV.Status.CANCELLED_DRAFT)
    _audit(user, "intercompany.invoice.cancel_draft", invoice, after={"status": invoice.status, "reason": reason},
           ctx=ctx)
    return invoice


# ---------------------------------------------------------------- credit notes (R13, R17)


def _live_credits(invoice):
    return INV.objects.filter(credit_of=invoice).exclude(status__in=INV.DEAD)


def uncredited_balance(invoice):
    """Gross of the invoice less every live (draft, pending, approved or issued) credit note."""
    credited = _live_credits(invoice).aggregate(g=Sum("gross_total"))["g"] or ZERO
    return invoice.gross_total - credited


def _line_remaining(line, exclude_invoice=None):
    qs = InvoiceLine.objects.filter(credits_line=line).exclude(invoice__status__in=INV.DEAD)
    if exclude_invoice is not None:
        qs = qs.exclude(invoice=exclude_invoice)
    agg = qs.aggregate(n=Sum("net_amount"), v=Sum("vat_amount"))
    return line.net_amount - (agg["n"] or ZERO), line.vat_amount - (agg["v"] or ZERO)


@transaction.atomic
def create_credit_note(user, invoice, *, reason_en, reason_ar, lines=None, issue_date=None, ctx=None):
    """Full (lines=None) or partial credit note. lines: [{"line": InvoiceLine or id, "net_amount": Decimal}].
    A credit note can never exceed the original's uncredited balance (R13)."""
    invoice = INV.objects.select_for_update().get(pk=invoice.pk)
    _require(user, invoice.entity, "intercompany.invoice.create")
    if invoice.kind != INV.Kind.INVOICE or invoice.status != INV.Status.ISSUED:
        raise Conflict("not_issued", "Credit notes are raised on issued invoices only.",
                       "تصدر الإشعارات الدائنة للفواتير الصادرة فقط.")
    if not (reason_en or "").strip() or not (reason_ar or "").strip():
        raise RuleError("reason_required", "A credit reason in English and Arabic is required (ZATCA).",
                        "سبب الإشعار الدائن مطلوب بالعربية والإنجليزية.")
    originals = {l.pk: l for l in invoice.lines.all()}
    if lines is None:
        wanted = [(l, _line_remaining(l)[0]) for l in originals.values()]
        wanted = [(l, n) for l, n in wanted if n > ZERO]
    else:
        wanted = []
        for spec in lines:
            line_id = spec["line"].pk if isinstance(spec["line"], InvoiceLine) else int(spec["line"])
            if line_id not in originals:
                raise RuleError("line_not_on_invoice", "The line is not on this invoice.", "البند ليس في هذه الفاتورة.")
            wanted.append((originals[line_id], money(Decimal(spec["net_amount"]))))
    built, gross = [], ZERO
    for line, net in wanted:
        rem_net, rem_vat = _line_remaining(line)
        if net <= ZERO or net > rem_net:
            raise RuleError("credit_exceeds_line", f"Credit {net} exceeds the uncredited {rem_net} of the line (R13).",
                            f"المبلغ {net} يتجاوز الرصيد غير المُسترد للبند {rem_net}.")
        vat = rem_vat if net == rem_net else money(net * line.vat_rate / HUNDRED)
        built.append((line, net, vat))
        gross += net + vat
    if not built:
        raise RuleError("nothing_to_credit", "Nothing left to credit.", "لا يوجد رصيد للإسترداد.")
    if gross > uncredited_balance(invoice):
        raise RuleError("credit_exceeds_balance",
                        f"Credit {gross} exceeds the uncredited balance {uncredited_balance(invoice)} (R13).",
                        f"الإشعار {gross} يتجاوز الرصيد غير المُسترد {uncredited_balance(invoice)}.")
    day = issue_date or today()
    if day < invoice.issue_date:
        raise RuleError("credit_before_invoice", "A credit note cannot be dated before its invoice.",
                        "لا يمكن أن يسبق تاريخ الإشعار تاريخ الفاتورة.")
    cn = INV(
        entity=invoice.entity, counterparty=invoice.counterparty, agreement=invoice.agreement,
        kind=INV.Kind.CREDIT_NOTE, credit_of=invoice, credit_reason_en=reason_en, credit_reason_ar=reason_ar,
        issue_date=day, supply_date_from=invoice.supply_date_from, supply_date_to=invoice.supply_date_to,
        due_date=day, billing_period=invoice.billing_period, currency=invoice.currency, invoice_type_code="381",
        seller_vat_number=invoice.seller_vat_number, buyer_vat_number=invoice.buyer_vat_number,
        created_by=user, **source_fields(user),
    )
    cn.save()
    for line, net, vat in built:
        InvoiceLine(
            entity=cn.entity, invoice=cn, agreement=line.agreement, description_en=line.description_en,
            description_ar=line.description_ar, quantity=Decimal("1"), unit_price=net, net_amount=net,
            vat_treatment=line.vat_treatment, vat_category=line.vat_category, vat_rate=line.vat_rate, vat_amount=vat,
            vat_exemption_reason_en=line.vat_exemption_reason_en, vat_exemption_reason_ar=line.vat_exemption_reason_ar,
            income_account=line.income_account, pricing_basis=f"credit of {invoice.number}"[:200], credits_line=line,
        ).save()
    _set_totals(cn)
    record_draft_editor(cn, user, "create")
    _audit(user, "intercompany.credit_note.create", cn, after=invoice_snapshot(cn), ctx=ctx)
    return cn


# ---------------------------------------------------------------- submission checks and issue (R10, R11, R15, R17)


def validate_for_issue(inv):
    """Checks before submission and again at final approval (R13, R21, T22). Raises RuleError."""
    lines = list(inv.lines.all())
    problems = []
    if not lines:
        problems.append("The invoice has no lines.")
    for l in lines:
        if not (l.description_en or "").strip() or not (l.description_ar or "").strip():
            problems.append(f"Line {l.pk} needs an English and an Arabic description (R21).")
    if inv.is_credit_note and (not inv.credit_reason_en.strip() or not inv.credit_reason_ar.strip()):
        problems.append("The credit note needs a reason in English and Arabic.")
    net = sum((l.net_amount for l in lines), ZERO)
    vat = sum((l.vat_amount for l in lines), ZERO)
    if (net, vat, net + vat) != (inv.net_total, inv.vat_total, inv.gross_total) or inv.gross_total <= ZERO:
        problems.append("Invoice totals do not equal the sum of the lines (R8).")
    if inv.entity_id == inv.counterparty_id or not inv.entity.is_active or not inv.counterparty.is_active:
        problems.append("Issuer and counterparty must be different active entities (R1).")
    if inv.vat_total > ZERO and not inv.entity.vat_number:
        problems.append(f"{inv.entity.code} has no VAT number; a VAT invoice cannot be issued.")
    if inv.is_credit_note:
        original = INV.objects.select_for_update().get(pk=inv.credit_of_id)
        if original.status != INV.Status.ISSUED:
            problems.append("The original invoice is not issued.")
        for l in lines:
            rem_net, _ = _line_remaining(l.credits_line, exclude_invoice=inv)
            if l.net_amount > rem_net:
                problems.append(f"Line {l.pk} exceeds the uncredited balance of the original (R13).")
        others = _live_credits(original).exclude(pk=inv.pk).aggregate(g=Sum("gross_total"))["g"] or ZERO
        if inv.gross_total > original.gross_total - others:
            problems.append("The credit note exceeds the uncredited balance of the original (R13).")
    if problems:
        raise RuleError("invoice_invalid", "The invoice cannot be approved: " + " ".join(problems),
                        "لا يمكن اعتماد الفاتورة: " + " ".join(problems), detail=problems)


def _invoice_entry_lines(inv):
    """Spec 5.1 (invoice) and 5.3 (credit note). Every line carries the counterparty (R12)."""
    cp = inv.counterparty
    dr, cr = ("debit", "credit") if not inv.is_credit_note else ("credit", "debit")
    out = [{"account": DUE_FROM, dr: inv.gross_total, "counterparty": cp, "description": inv.number}]
    income = OrderedDict()
    for l in inv.lines.select_related("income_account").order_by("id"):
        income[l.income_account] = income.get(l.income_account, ZERO) + l.net_amount
    for account, amount in income.items():
        out.append({"account": account, cr: amount, "counterparty": cp, "description": inv.number})
    if inv.vat_total > ZERO:
        out.append({"account": VAT_OUTPUT, cr: inv.vat_total, "counterparty": cp, "description": inv.number})
    return out


def issue_invoice(invoice, user, ctx=None):
    """Issue an approved invoice or credit note. Needs intercompany.invoice.issue and MP approval first.
    Normally final approval issues automatically (approvals D11); refusals are audited (T17)."""
    reason = None
    if user is None or not user.is_active:
        reason = ("inactive", "You need an active user.", "تحتاج إلى مستخدم نشط.")
    elif not user.is_human:
        reason = ("service_user_cannot_issue", "Service users cannot issue invoices (R20).",
                  "لا يمكن لمستخدمي الخدمة إصدار الفواتير.")
    elif not access.has_perm(user, invoice.entity, "intercompany.invoice.issue"):
        reason = ("permission_denied", f"You cannot issue invoices in {invoice.entity.code}.",
                  f"لا يمكنك إصدار الفواتير في {invoice.entity.code}.")
    if reason is None:
        invoice.refresh_from_db()
        if invoice.status != INV.Status.APPROVED:
            reason = ("not_approved", "The invoice needs Managing Partner approval before it is issued (R10).",
                      "تحتاج الفاتورة إلى اعتماد الشريك المدير قبل إصدارها.")
    if reason is not None:
        if user is not None:
            _audit(user, "intercompany.invoice.issue_refused", invoice, after={"code": reason[0]}, ctx=ctx)
        cls = RuleError if reason[0] == "not_approved" else AccessDenied
        raise cls(*reason)
    return _issue(invoice, approver=invoice.approved_by, actor=user, ctx=ctx)


@transaction.atomic
def _issue(invoice, approver=None, actor=None, ctx=None):
    """R11: number, journal entry (create_entry + post_entry, system entry) and status in one transaction.
    A ledger refusal (e.g. closed period, T15) rolls everything back, including the number."""
    inv = INV.objects.select_for_update().select_related("entity", "counterparty", "agreement").get(pk=invoice.pk)
    if inv.status != INV.Status.APPROVED:
        raise Conflict("not_approved", "Only approved invoices can be issued.", "يمكن إصدار الفواتير المعتمدة فقط.")
    validate_for_issue(inv)
    prefix = "ICC" if inv.is_credit_note else "ICI"
    number = _next_number(inv.entity, prefix, inv.issue_date.year, 6)
    inv.number = number
    kind_en = "credit note" if inv.is_credit_note else "invoice"
    entry = ledger.create_entry(
        inv.entity, inv.issue_date, f"Intercompany {kind_en} {number} to {inv.counterparty.code}: "
        f"{inv.agreement.description_en}"[:500], _invoice_entry_lines(inv), user=None, reference=number,
        system=True, post=True, source=inv.source_label,
    )
    _move(inv, INV.Status.ISSUED, number=number, journal_entry=entry, issued_at=timezone.now(),
          approved_by=approver, zatca_uuid=uuid.uuid4(), zatca_ready=True,
          seller_vat_number=inv.entity.vat_number, buyer_vat_number=inv.counterparty.vat_number)
    bill = _create_mirror(inv)
    _audit(actor or approver, "intercompany.invoice.issue", inv,
           before={"status": INV.Status.APPROVED},
           after={"status": inv.status, "number": number, "journal_entry": entry.number,
                  "mirror_bill": bill.number if bill else None}, ctx=ctx)
    return inv


def _create_mirror(inv):
    """R15 / R17 (D8): a draft bill (or bill credit) in the counterparty's books when it keeps them here."""
    cp = inv.counterparty
    original_bill = None
    if inv.is_credit_note:
        original_bill = BILL.objects.filter(invoice_id=inv.credit_of_id).first()
        if original_bill is None:
            return None
    elif not cp.keeps_books_on(inv.issue_date):
        return None
    default_expense = expense_account_for(inv.agreement)
    bill = BILL(
        entity=cp, counterparty=inv.entity, invoice=inv,
        kind=BILL.Kind.CREDIT if inv.is_credit_note else BILL.Kind.BILL, credit_of=original_bill,
        number=_next_number(cp, "ICB", inv.issue_date.year, 6), bill_date=inv.issue_date, due_date=inv.due_date,
        currency=inv.currency, net_total=inv.net_total, vat_total=inv.vat_total, gross_total=inv.gross_total,
        source=Source.IMPORT, source_agent="", created_by=None,
    )
    bill._system = True
    bill.save()
    for l in inv.lines.order_by("id"):
        expense = default_expense
        if l.credits_line_id:
            mirror_line = IntercompanyBillLine.objects.filter(invoice_line_id=l.credits_line_id).first()
            expense = mirror_line.expense_account if mirror_line else default_expense
        bl = IntercompanyBillLine(
            entity=cp, bill=bill, invoice_line=l, description_en=l.description_en, description_ar=l.description_ar,
            quantity=l.quantity, unit_price=l.unit_price, net_amount=l.net_amount, vat_rate=l.vat_rate,
            vat_amount=l.vat_amount, expense_account=expense,
        )
        bl._system = True
        bl.save()
    _audit(None, "intercompany.bill.create", bill, entity=cp, after=bill_snapshot(bill))
    return bill


# ---------------------------------------------------------------- mirror bills (R16, 5.2)


def bill_snapshot(bill):
    return snapshot(bill) | {"lines": [[l.pk, l.expense_account.code, l.cost_centre, str(l.net_amount),
                                        str(l.vat_amount)] for l in bill.lines.select_related("expense_account")]}


@transaction.atomic
def update_bill_coding(user, bill, lines, ctx=None):
    """R16: only expense account and cost centre change. lines: {line_id: {"expense_account": Account|code,
    "cost_centre": str}}. Amounts, dates and VAT are refused by the model."""
    bill = BILL.objects.select_for_update().get(pk=bill.pk)
    _require(user, bill.entity, "intercompany.bill.edit")
    _require_human(user)
    if bill.status not in (BILL.Status.DRAFT, BILL.Status.DISPUTED):
        raise Conflict("bill_locked", "The bill is in approval or posted.", "الفاتورة قيد الاعتماد أو مرحّلة.")
    before = bill_snapshot(bill)
    for line_id, change in lines.items():
        unknown = set(change) - {"expense_account", "cost_centre"}
        if unknown:
            raise RuleError("field_not_editable", "Only the expense account and cost centre can change (R16).",
                            "يمكن تغيير حساب المصروف ومركز التكلفة فقط.", detail=sorted(unknown))
        line = bill.lines.get(pk=line_id)
        if "expense_account" in change:
            acc = change["expense_account"]
            if not isinstance(acc, Account):
                acc = Account.objects.filter(entity=bill.entity, code=str(acc)).first()
                if acc is None:
                    raise RuleError("account_invalid", "Unknown expense account.", "حساب مصروف غير معروف.")
            line.expense_account = acc
        if "cost_centre" in change:
            line.cost_centre = change["cost_centre"] or ""
        _clean(line)
        line.save()
    record_draft_editor(bill, user, "edit")
    _audit(user, "intercompany.bill.update_coding", bill, before=before, after=bill_snapshot(bill), ctx=ctx)
    return bill


@transaction.atomic
def dispute_bill(user, bill, reason, ctx=None):
    bill = BILL.objects.select_for_update().get(pk=bill.pk)
    _require(user, bill.entity, "intercompany.bill.edit")
    _require_human(user)
    if not (reason or "").strip():
        raise RuleError("reason_required", "A reason is required.", "السبب مطلوب.")
    if bill.status != BILL.Status.DRAFT:
        raise Conflict("not_draft", "Only draft bills can be disputed.", "يمكن الاعتراض على المسودات فقط.")
    _move(bill, BILL.Status.DISPUTED, dispute_reason=reason[:500])
    _audit(user, "intercompany.bill.dispute", bill, after={"status": bill.status, "reason": reason}, ctx=ctx)
    return bill


@transaction.atomic
def undispute_bill(user, bill, ctx=None):
    bill = BILL.objects.select_for_update().get(pk=bill.pk)
    _require(user, bill.entity, "intercompany.bill.edit")
    _require_human(user)
    if bill.status != BILL.Status.DISPUTED:
        raise Conflict("not_disputed", "The bill is not disputed.", "الفاتورة غير معترض عليها.")
    _move(bill, BILL.Status.DRAFT)
    _audit(user, "intercompany.bill.undispute", bill, after={"status": bill.status}, ctx=ctx)
    return bill


@transaction.atomic
def submit_bill(user, bill, ctx=None):
    """The receiver user who submits a system-made mirror bill becomes its preparer (the document's
    creator for segregation of duties); the MP of the receiver approves (D7)."""
    from approvals import services as approvals

    bill = BILL.objects.select_for_update().get(pk=bill.pk)
    _require(user, bill.entity, "intercompany.bill.edit")
    _require_human(user)
    if bill.status != BILL.Status.DRAFT:
        raise Conflict("not_draft", "Only draft bills can be submitted.", "يمكن تقديم المسودات فقط.")
    if bill.prepared_by_id is None:
        bill.prepared_by = user
        with workflow(bill):
            bill.save()
        record_draft_editor(bill, user, "edit")
    elif bill.prepared_by_id != user.pk:
        raise AccessDenied("not_preparer", "Another user prepared this bill; they submit it.",
                           "أعدّ هذه الفاتورة مستخدم آخر؛ هو من يقدّمها.")
    return approvals.submit(bill, user, ctx=ctx)


def _bill_entry_lines(bill):
    """Spec 5.2 (bill) and 5.3 receiver side (bill credit). Every line carries the issuer (R12)."""
    cp = bill.counterparty
    dr, cr = ("debit", "credit") if bill.kind == BILL.Kind.BILL else ("credit", "debit")
    out = []
    expense = OrderedDict()
    for l in bill.lines.select_related("expense_account").order_by("id"):
        expense[l.expense_account] = expense.get(l.expense_account, ZERO) + l.net_amount
    for account, amount in expense.items():
        out.append({"account": account, dr: amount, "counterparty": cp, "description": bill.number})
    if bill.vat_total > ZERO:
        out.append({"account": VAT_INPUT, dr: bill.vat_total, "counterparty": cp, "description": bill.number})
    out.append({"account": DUE_TO, cr: bill.gross_total, "counterparty": cp, "description": bill.number})
    return out


def check_bill_postable(bill):
    if bill.kind == BILL.Kind.CREDIT and (bill.credit_of is None or bill.credit_of.status != BILL.Status.POSTED):
        raise RuleError("original_bill_not_posted", "Post the original bill before its credit.",
                        "رحّل الفاتورة الأصلية قبل الإشعار الدائن.")
    for line in bill.lines.select_related("expense_account"):
        _clean(line)


def post_bill(bill):
    """Called by final approval of the bill (D11) inside the approval transaction."""
    bill = BILL.objects.select_for_update().get(pk=bill.pk)
    if bill.status != BILL.Status.APPROVED:
        raise Conflict("not_approved", "Only approved bills can be posted.", "يمكن ترحيل الفواتير المعتمدة فقط.")
    check_bill_postable(bill)
    kind_en = "bill credit" if bill.kind == BILL.Kind.CREDIT else "bill"
    entry = ledger.create_entry(
        bill.entity, bill.bill_date, f"Intercompany {kind_en} {bill.number} from {bill.counterparty.code} "
        f"({bill.invoice.number})"[:500], _bill_entry_lines(bill), user=None, reference=bill.invoice.number,
        system=True, post=True, source=bill.source_label,
    )
    _move(bill, BILL.Status.POSTED, journal_entry=entry)
    _audit(None, "intercompany.bill.post", bill, after={"status": bill.status, "journal_entry": entry.number})
    return bill


# ---------------------------------------------------------------- reconciliation report (§6, 5.8)

HEADINGS = OrderedDict([
    ("issuer", ("Issuer", "المُصدر")),
    ("counterparty", ("Counterparty", "الطرف المقابل")),
    ("consolidation", ("Consolidation", "التوحيد")),
    ("issuer_balance", ("Issuer 1210 balance", "رصيد 1210 لدى المُصدر")),
    ("receiver_balance", ("Receiver 2110 balance", "رصيد 2110 لدى المستلم")),
    ("difference", ("Difference", "الفرق")),
    ("unmatched", ("Unmatched items", "بنود غير مطابقة")),
])

CONSOLIDATION_LABELS = {
    "parent": ("Parent", "الشركة الأم"),
    "full": ("Full consolidation", "توحيد كامل"),
    "equity": ("Equity method", "طريقة حقوق الملكية"),
    "none": ("Not consolidated", "غير موحدة"),
}


def _balance(entity, account_code, counterparty, as_of, sign):
    t = _posted_lines().filter(entry__entity=entity, account__code=account_code, counterparty_entity=counterparty,
                               entry__date__lte=as_of).aggregate(d=Sum("debit"), c=Sum("credit"))
    return money(sign * ((t["d"] or ZERO) - (t["c"] or ZERO)))


def reconciliation_report(user, as_of, entity=None):
    """Per issuer/receiver pair as of a date (§6). Each side's figures need intercompany.report.view in
    that entity; otherwise they show as restricted (R19)."""
    _active_user(user)
    viewable = set(access.entities_with_perm(user, "intercompany.report.view").values_list("pk", flat=True))
    if not viewable:
        raise AccessDenied("permission_denied", "You cannot view the reconciliation report.",
                           "لا يمكنك عرض تقرير المطابقة.")
    invoices = INV.objects.filter(status=INV.Status.ISSUED, issue_date__lte=as_of) \
        .filter(Q(entity_id__in=viewable) | Q(counterparty_id__in=viewable))
    if entity is not None:
        invoices = invoices.filter(Q(entity=entity) | Q(counterparty=entity))
    pairs = OrderedDict()
    for inv in invoices.select_related("entity", "counterparty").order_by("entity__code", "counterparty__code", "id"):
        pairs.setdefault((inv.entity, inv.counterparty), []).append(inv)
    rows = []
    for (issuer, cp), invs in pairs.items():
        see_issuer, see_cp = issuer.pk in viewable, cp.pk in viewable
        issuer_balance = _balance(issuer, DUE_FROM, cp, as_of, 1) if see_issuer else None
        if not cp.books_in_erp:
            receiver = {"status": "not_on_erp", "label_en": "Counterparty not on ERP", "label_ar": "الطرف المقابل خارج النظام",
                        "amount": None}
        elif not see_cp:
            receiver = {"status": "restricted", "label_en": "No access", "label_ar": "لا توجد صلاحية", "amount": None}
        else:
            receiver = {"status": "on_erp", "label_en": "", "label_ar": "",
                        "amount": _balance(cp, DUE_TO, issuer, as_of, -1)}
        difference = None
        if issuer_balance is not None and receiver["amount"] is not None:
            difference = issuer_balance - receiver["amount"]
        unmatched = []
        if cp.books_in_erp:
            for inv in invs:
                bill = BILL.objects.filter(invoice=inv).select_related("journal_entry").first()
                if bill is None:
                    if cp.keeps_books_on(inv.issue_date):
                        unmatched.append({"type": "no_bill", "invoice": inv.number,
                                          "label_en": "Invoice without bill", "label_ar": "فاتورة بلا فاتورة مقابلة"})
                    continue
                if bill.status == BILL.Status.DISPUTED and see_cp:
                    unmatched.append({"type": "disputed", "invoice": inv.number, "bill": bill.number,
                                      "label_en": "Disputed bill", "label_ar": "فاتورة معترض عليها"})
                elif bill.status != BILL.Status.POSTED or bill.journal_entry.date > as_of:
                    unmatched.append({"type": "bill_not_posted", "invoice": inv.number,
                                      "bill": bill.number if see_cp else None,
                                      "label_en": "Invoice without posted bill", "label_ar": "فاتورة بلا فاتورة مقابلة مرحّلة"})
                elif (bill.journal_entry.date.year, bill.journal_entry.date.month) != (inv.issue_date.year,
                                                                                       inv.issue_date.month):
                    unmatched.append({"type": "other_period", "invoice": inv.number,
                                      "bill": bill.number if see_cp else None,
                                      "label_en": "Bill dated in another period", "label_ar": "فاتورة مقابلة بتاريخ فترة أخرى"})
        label_en, label_ar = CONSOLIDATION_LABELS.get(cp.consolidation, (cp.consolidation, cp.consolidation))
        pct = f"{cp.ownership_pct.normalize():f}"
        rows.append({
            "issuer": issuer.code, "counterparty": cp.code,
            "consolidation": {"type": cp.consolidation, "ownership_pct": str(cp.ownership_pct),
                              "label_en": f"{label_en} {pct}%", "label_ar": f"{label_ar} {pct}%"},
            "issuer_balance": issuer_balance, "receiver_balance": receiver["amount"], "receiver_status": receiver,
            "difference": difference, "unmatched": unmatched,
        })
    return {"as_of": as_of, "headings": {k: {"en": en, "ar": ar} for k, (en, ar) in HEADINGS.items()}, "rows": rows}


def reconciliation_xlsx(report, language="en"):
    """XLSX export with headings in the chosen language (openpyxl, open source)."""
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Reconciliation" if language == "en" else "المطابقة"
    if language == "ar":
        ws.sheet_view.rightToLeft = True
    ws.append([v[language] for v in report["headings"].values()])
    for r in report["rows"]:
        rs = r["receiver_status"]
        ws.append([
            r["issuer"], r["counterparty"], r["consolidation"][f"label_{language}"],
            r["issuer_balance"] if r["issuer_balance"] is not None else "-",
            r["receiver_balance"] if r["receiver_balance"] is not None else rs[f"label_{language}"],
            r["difference"] if r["difference"] is not None else "-",
            "; ".join(f"{u[f'label_{language}']} {u['invoice']}" for u in r["unmatched"]),
        ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
