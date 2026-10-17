"""HR services (docs/specs/hr.md, corrected by docs/compliance/hr-payroll-labour-law-2026-10-17.md). The only
supported way to change HR data.

Every public function checks rights with ``core.access.require`` for the entity that owns the record (access R1,
hr R48); lists go through ``access.scope`` (R2). Approvals pass the stored record as ``document=`` so the access
layer refuses its creator, submitter or last modifier (access R14, R35); HR also refuses an approval of the
approver's own record (OQ-25). HR posts nothing to the ledger (§5): payroll does.

Regulatory figures come only from ``StatutoryParameter`` rows valid on the relevant date (R6). Each approval
collects the rows it used; with ``HR_REQUIRE_VERIFIED_PARAMETERS`` (production default) an approval that used an
unverified row is refused, naming it (R7). Statutory rules apply per employment basis (R49 to R52): Saudi rules
to ``ksa_employee`` only; ``local_entity_employee`` reads its entity's own jurisdiction (none seeded, OQ-26);
``remuneration`` has no statutory calculation at all.

Operator jobs (``hr_setup_entity``, ``hr_accrue_leave``, ``hr_document_alerts``, ``hr_deliver_assignment_events``,
``hr_refresh_status``) call the functions in the "jobs" section; they are reviewed entries of the access-scan
allow-list (no user action).
"""
import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import F, Max, Q, Sum
from django.utils import timezone
from hijridate import Gregorian, Hijri

from core import access
from core.access import services as access_services
from core.access.models import AccessLog, Kind, Membership
from core.models import Currency, Entity
from ledger import fx
from ledger.models import Account, LedgerError

from . import billing_port, crypto, payroll_port
from .models import (
    ANNUAL_LEAVE_CEILING,
    ANNUAL_LEAVE_CONTRACT_DAYS,
    MASK,
    ZERO,
    Assignment,
    AssignmentEvent,
    AssignmentEventAction,
    ContractComponent,
    Department,
    Employee,
    EmployeeDocument,
    EmploymentBasis,
    EmploymentContract,
    EosbCalculation,
    FinalSettlementInput,
    Grade,
    HrAlert,
    HrChecklistItem,
    HrError,
    HrSequence,
    HrSettings,
    LeaveAdjustment,
    LeaveLedger,
    LeaveRequest,
    LeaveType,
    PregnancyNotice,
    SalaryComponent,
    SickLeaveDay,
    Source,
    StatutoryParameter,
    Termination,
    error,
)

ALMIRAS_CODE = "ALM"  # R55: the managing-partner overtime route is seeded for Almiras only
GCC = frozenset({"BH", "KW", "OM", "QA", "AE"})  # SA itself is "Saudi"
DEFAULT_ALERT_DAYS = [90, 60, 30, 7, 0]  # OQ-10 default
CENT = Decimal("0.01")
Basis = EmploymentBasis
CStatus = EmploymentContract.Status
TKind = Termination.Kind


def _today():
    """Today in Asia/Riyadh (TIME_ZONE). A function so tests can move the clock."""
    return timezone.localdate()


def _problems(problems, code):
    exc = HrError(list(problems), code=code)
    exc.code = code
    return exc


def _source(user):
    return Source.SYSTEM if access_services.kind_of(user) == Kind.SYSTEM else Source.HUMAN


def _round(amount):
    with localcontext() as ctx:
        ctx.prec = 60
        return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)


def _frac_round(value):
    """A Fraction rounded half-up to 2 dp, as Decimal (R39: rounding only at the award)."""
    with localcontext() as ctx:
        ctx.prec = 60
        return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(CENT, rounding=ROUND_HALF_UP)


def _frac(text):
    """Exact rational from a Decimal string or "a/b" (parameter values, R6)."""
    if text is None:
        return None
    return Fraction(str(text))


def _decimal(value, what):
    try:
        return fx.to_decimal(value, what)
    except LedgerError as exc:
        raise HrError(exc.messages, code="float_rejected" if isinstance(value, float) else "not_a_number") from None


def _add_months(d, months):
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _lock(model, pk):
    return model._base_manager.select_for_update(of=("self",)).get(pk=pk)


def _save(obj, **kwargs):
    obj._service_write = True
    obj.save(**kwargs)
    return obj


def _settings(entity):
    s = HrSettings.objects.filter(entity=entity).first()
    if s is None:
        raise error(f"HR is not set up for {entity.code}; run hr_setup_entity {entity.code}.",
                    f"لم تُعدّ الموارد البشرية للكيان {entity.code}؛ شغّل الأمر hr_setup_entity {entity.code}.",
                    code="hr_not_set_up")
    return s


def _require_employees(entity):
    s = _settings(entity)
    if not s.has_employees:
        raise error(f"{entity.code} is set to have no employees; HR records are refused (R5).",
                    f"الكيان {entity.code} مضبوط على عدم وجود موظفين؛ تُرفض سجلات الموارد البشرية.", code="no_employees")
    return s


def _same_entity(entity, obj, what_en, what_ar):
    if obj is not None and getattr(obj, "entity_id", None) != entity.pk:
        raise error(f"The {what_en} belongs to another entity (R1).", f"{what_ar} يخص كيانًا آخر.", code="entity_mismatch")


def _deny_own(user, entity, permission, employee):
    """OQ-25: nobody approves a record about themselves (own contract, leave, termination)."""
    if employee.user_id is not None and employee.user_id == user.pk:
        access.deny(user, entity, permission, "self_approval")


def _group_parent(user, permission):
    parents = list(Entity.objects.filter(consolidation=Entity.Consolidation.PARENT, parent__isnull=True).order_by("pk"))
    for p in parents:
        if access.has(user, p, permission):
            return p
    if not parents:
        raise error("No group parent entity exists.", "لا يوجد كيان أم للمجموعة.", code="no_group")
    return parents[0]


# =====================================================================================================
# Statutory parameters (3.9; R6, R7, R51)
# =====================================================================================================

def _not_configured(jurisdiction, code, on, what_en="", what_ar=""):
    return error(
        f"Jurisdiction {jurisdiction} is not configured for {what_en or 'this calculation'}: no statutory parameter "
        f"{code} valid on {on} (R5, OQ-26).",
        f"الولاية القانونية {jurisdiction} غير مهيأة لـ{what_ar or 'هذا الحساب'}: لا يوجد معيار نظامي {code} ساري "
        f"بتاريخ {on}.", code="jurisdiction_not_configured")


def param(jurisdiction, code, on, *, required=True):
    """The ``StatutoryParameter`` row for ``(jurisdiction, code)`` valid on ``on`` (latest ``valid_from``; superseded
    rows ignored). Global reference data (no entity, no personal data). Missing → bilingual "jurisdiction not
    configured" (R5), or None with ``required=False``."""
    rows = (StatutoryParameter.objects.filter(jurisdiction=jurisdiction, code=code, valid_from__lte=on)
            .exclude(status=StatutoryParameter.Status.SUPERSEDED).order_by("-valid_from"))
    for row in rows:
        if row.valid_to is None or row.valid_to >= on:
            return row
    if required:
        raise _not_configured(jurisdiction, code, on)
    return None


def _required_verified():
    return getattr(settings, "HR_REQUIRE_VERIFIED_PARAMETERS", True)


def _unverified_error(rows):
    names = ", ".join(f"{r.jurisdiction} {r.code} ({r.valid_from})" for r in rows)
    return error(f"This uses unverified statutory parameters: {names}. They must be verified first (R7).",
                 f"يستخدم هذا الإجراء معايير نظامية غير متحقق منها: {names}. يجب التحقق منها أولًا.",
                 code="parameter_unverified")


@dataclass
class ParamUse:
    """The parameter rows one calculation or approval read (R7).

    Every path that writes an approved record or a leave-ledger row calls :meth:`check_verified` before its first
    write and :meth:`mark_used` after its last one. After ``check_verified`` the use is *strict*: a row read later in
    the same transaction is checked as it is read, so a write that needed an unverified row is refused (and rolled
    back) even when the row was first read after the check."""

    rows: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    strict: bool = False

    def _take(self, row):
        if self.strict and _required_verified() and row.status != StatutoryParameter.Status.VERIFIED:
            raise _unverified_error([row])
        self.rows[row.pk] = row

    def get(self, jurisdiction, code, on, what_en="", what_ar=""):
        row = param(jurisdiction, code, on, required=False)
        if row is None:
            raise _not_configured(jurisdiction, code, on, what_en, what_ar)
        self._take(row)
        return row.value

    def get_span(self, jurisdiction, code, start, end, what_en="", what_ar=""):
        """Every current row of ``code`` valid on some day of ``start``..``end``, as ``[(first, last, value)]``
        day ranges in date order (nit 12: a span is read from all overlapping rows, not only the row valid on its
        first day). A day no row covers refuses the calculation ("jurisdiction not configured", R5)."""
        rows = list(StatutoryParameter.objects.filter(jurisdiction=jurisdiction, code=code, valid_from__lte=end)
                    .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=start))
                    .exclude(status=StatutoryParameter.Status.SUPERSEDED).order_by("valid_from"))
        result, day = [], start
        while day <= end:
            row = next((r for r in reversed(rows) if r.valid_from <= day and (r.valid_to is None or r.valid_to >= day)),
                       None)
            if row is None:
                raise _not_configured(jurisdiction, code, day, what_en, what_ar)
            later = [r.valid_from for r in rows if r.valid_from > day]
            last = min([end, row.valid_to or end] + [d - timedelta(days=1) for d in later])
            self._take(row)
            result.append((day, last, row.value))
            day = last + timedelta(days=1)
        return result

    @property
    def ids(self):
        return sorted(self.rows)

    def unverified(self):
        """The rows read that are not verified, when the setting requires verified rows (else none)."""
        if not _required_verified():
            return []
        return [r for r in self.rows.values() if r.status != StatutoryParameter.Status.VERIFIED]

    def check_verified(self):
        bad = self.unverified()
        if bad:
            raise _unverified_error(bad)
        self.strict = True

    def mark_used(self):
        now = timezone.now()
        for row in self.rows.values():
            stored = StatutoryParameter.objects.select_for_update().get(pk=row.pk)
            if stored.used_at is None:
                stored.used_at = now
                stored.save(update_fields=["used_at"])

    def warn(self, text):
        if text not in self.warnings:
            self.warnings.append(text)


def _jurisdiction(employee, what_en="statutory calculations", what_ar="الحسابات النظامية"):
    """Which jurisdiction's parameters apply to ``employee`` (R49 to R52)."""
    basis = employee.employment_basis
    if basis == Basis.REMUNERATION:
        raise error(f"Remuneration staff have no labour regulation: no {what_en} (R52).",
                    f"لا يخضع أصحاب المكافأة الشهرية لتنظيم العمل: لا {what_ar}.", code="remuneration_no_statutory")
    if basis == Basis.KSA_EMPLOYEE:
        return "SA"
    jurisdiction = _settings(employee.entity).jurisdiction
    if jurisdiction == "SA":  # R51: a Saudi rule never applies to a local entity employee
        raise error("A local entity employee is never computed with Saudi parameters (R51).",
                    "لا يُحسب موظف الشركة المحلية بالمعايير السعودية أبدًا.", code="saudi_rule_for_local")
    return jurisdiction


@transaction.atomic
def add_statutory_parameter(*, user, jurisdiction, code, value, valid_from, source_url, source_reference, valid_to=None):
    """Enter a parameter row (R7; OQ-17: the group parent's hr_officer). Entered rows start ``unverified``."""
    group = _group_parent(user, "hr.maintain_statutory_parameter")
    access.require(user, group, "hr.maintain_statutory_parameter")
    row = StatutoryParameter(jurisdiction=jurisdiction, code=code, value=value, valid_from=valid_from,
                             valid_to=valid_to, source_url=source_url, source_reference=source_reference,
                             entered_by=user)
    try:
        row.full_clean()
    except ValidationError as exc:
        raise HrError(exc.messages, code="parameter_invalid") from None
    row.save()
    return row


@transaction.atomic
def verify_statutory_parameter(row, *, user):
    """R7: ``hr.verify_statutory_parameter`` on the group parent, by someone other than ``entered_by``."""
    group = _group_parent(user, "hr.verify_statutory_parameter")
    row = StatutoryParameter.objects.select_for_update().get(pk=row.pk)
    access.require(user, group, "hr.verify_statutory_parameter", document=row)
    if row.entered_by_id is not None and row.entered_by_id == user.pk:
        access.deny(user, group, "hr.verify_statutory_parameter", "self_approval")
    if row.status != StatutoryParameter.Status.UNVERIFIED:
        raise error("Only unverified parameters are verified.", "يُتحقق من المعايير غير المتحقق منها فقط.",
                    code="parameter_status")
    row.status, row.verified_by, row.verified_at = StatutoryParameter.Status.VERIFIED, user, timezone.now()
    row.save()
    return row


@transaction.atomic
def supersede_statutory_parameter(row, *, user, value, source_url, source_reference, valid_from=None, valid_to=None):
    """R7: a verified or used row is corrected by superseding it **and** entering its replacement in the same
    transaction (a superseded row without a replacement would leave the dates without a figure). The replacement
    starts on ``valid_from`` (default: the superseded row's own ``valid_from``), is entered by ``user`` and starts
    ``unverified``. Returns ``(superseded, replacement)``."""
    group = _group_parent(user, "hr.maintain_statutory_parameter")
    access.require(user, group, "hr.maintain_statutory_parameter")
    old = StatutoryParameter.objects.select_for_update().get(pk=row.pk)
    if old.status == StatutoryParameter.Status.SUPERSEDED:
        raise error("This parameter row is already superseded.", "صف المعيار هذا مستبدل مسبقًا.", code="parameter_status")
    start = valid_from or old.valid_from
    if start < old.valid_from:
        raise error("The replacement starts on or after the superseded row's valid-from date.",
                    "يبدأ الصف البديل في تاريخ سريان الصف المستبدل أو بعده.", code="parameter_invalid")
    old.status = StatutoryParameter.Status.SUPERSEDED
    old.save(update_fields=["status"])
    new = StatutoryParameter(jurisdiction=old.jurisdiction, code=old.code, value=value, valid_from=start,
                             valid_to=valid_to, source_url=source_url, source_reference=source_reference,
                             entered_by=user)
    try:
        new.full_clean(validate_constraints=False)
    except ValidationError as exc:
        raise HrError(exc.messages, code="parameter_invalid") from None
    try:
        with transaction.atomic():
            new.save()
    except IntegrityError:
        raise error(f"Another current row of {old.code} already starts on {start}.",
                    f"يوجد صف ساري آخر للمعيار {old.code} يبدأ في {start}.", code="parameter_invalid") from None
    return old, new


# =====================================================================================================
# Calendar helpers (Art. 10; Regs Art. 19, 24)
# =====================================================================================================

def _year_days(use, jurisdiction, basis, on):
    value = use.get(jurisdiction, "calendar.year_days", on)
    return int(value[basis])


def _hijri_add_years(start, years):
    """The Gregorian date ``years`` Umm al-Qura years after ``start`` (Art. 10). The Hijri day is kept, or the last
    day of the Hijri month when that month is shorter. Conversion by the open-source ``hijridate`` library (MIT),
    whose Umm al-Qura month table covers 1343-1500 AH (1924-2077)."""
    try:
        h = Gregorian(start.year, start.month, start.day).to_hijri()
        year = h.year + years
        day = min(h.day, Hijri(year, h.month, 1).month_length())
        g = Hijri(year, h.month, day).to_gregorian()
    except (ValueError, OverflowError):
        raise error(f"{start} plus {years} Hijri years is outside the Umm al-Qura calendar table (1924 to 2077).",
                    f"التاريخ {start} مضافًا إليه {years} سنة هجرية خارج جدول تقويم أم القرى (1924 إلى 2077).",
                    code="hijri_out_of_range") from None
    return date(g.year, g.month, g.day)


def _anniversary(start, years, basis, year_days=None):
    """The date ``years`` service years after ``start`` in the contract's calendar (Art. 10): Gregorian = the same
    calendar day; Hijri = the same Umm al-Qura day (R58)."""
    if basis == EmploymentContract.CalendarBasis.GREGORIAN:
        try:
            return start.replace(year=start.year + years)
        except ValueError:  # 29 February in a common year: the last day of February
            return start.replace(year=start.year + years, day=start.day - 1)
    return _hijri_add_years(start, years)


def _years_between(start, end_exclusive, basis, year_days):
    """Service years from ``start`` up to (not including) ``end_exclusive``: the completed anniversaries in the
    contract's calendar (Art. 10, R58: the same Gregorian day for ``gregorian``, the same Umm al-Qura day for
    ``hijri``) plus the remaining days of the part year / the parameter's divisor for that calendar (OQ-36 default:
    365 Gregorian, 354 Hijri). The divisor applies to the part year only (finding 5), so the 2-, 5- and 10-year
    thresholds fall on the anniversaries whatever leap days lie between."""
    days = (end_exclusive - start).days
    if days <= 0:
        return Fraction(days, year_days)
    n = 0
    while _anniversary(start, n + 1, basis) <= end_exclusive:
        n += 1
    return Fraction(n) + Fraction((end_exclusive - _anniversary(start, n, basis)).days, year_days)


def _holidays(use, jurisdiction, start, end):
    """Official holidays between start and end (parameter ``calendar.official_holidays``)."""
    value = use.get(jurisdiction, "calendar.official_holidays", start)
    result = set()
    for year in range(start.year, end.year + 1):
        for md in value.get("fixed_month_days", []):
            m, d = (int(x) for x in md.split("-"))
            day = date(year, m, d)
            if start <= day <= end:
                result.add(day)
    for a, b in value.get("date_ranges", []):
        for day in _days(max(start, date.fromisoformat(a)), min(end, date.fromisoformat(b))):
            result.add(day)
    return result


def _weekend(entity):
    s = _settings(entity)
    return {4, 5} if s.work_week_days <= 5 else {4}  # Friday (and Saturday)


# =====================================================================================================
# Setup (R4, R55)
# =====================================================================================================

COMPONENT_SEED = (
    # code, en, ar, gosi, eosb, overtime base, unpaid-leave deduction (3.3 seed flags [CC-verify]; R52 REMUNERATION)
    ("BASIC", "Basic salary", "الراتب الأساسي", True, True, True, True),
    ("HOUSING", "Housing allowance", "بدل السكن", True, True, False, True),
    ("TRANSPORT", "Transport allowance", "بدل النقل", False, True, False, True),
    ("OTHER", "Other allowance", "بدل آخر", False, True, False, True),
)
REMUNERATION_COMPONENT = ("REMUNERATION", "Monthly remuneration", "المكافأة الشهرية", False, False, False, False)

# code, en, ar, statutory, parameter, pay, deducts balance, document, gender, once per service
LEAVE_TYPE_SEED_SA = (
    ("ANNUAL", "Annual leave", "الإجازة السنوية", True, "leave.annual", "full", True, False, "", False),
    ("SICK", "Sick leave", "الإجازة المرضية", True, "leave.sick", "tiered", False, True, "", False),
    ("MARRIAGE", "Marriage leave", "إجازة الزواج", True, "leave.marriage", "full", False, False, "", False),
    ("BEREAVEMENT", "Bereavement leave (spouse, ascendant, descendant)", "إجازة الوفاة (الزوج أو الأصول أو الفروع)",
     True, "leave.bereavement", "full", False, False, "", False),
    ("BEREAVEMENT_SIBLING", "Bereavement leave (brother or sister)", "إجازة وفاة الأخ أو الأخت", True,
     "leave.bereavement_sibling", "full", False, False, "", False),
    ("PATERNITY", "Leave on the birth of a child", "إجازة المولود", True, "leave.paternity", "full", False, False,
     "male", False),
    ("MATERNITY", "Maternity leave", "إجازة الوضع", True, "leave.maternity", "full", False, True, "female", False),
    ("MATERNITY_UNPAID", "Extra maternity month (unpaid)", "شهر إضافي لإجازة الوضع (بدون أجر)", True,
     "leave.maternity_unpaid", "unpaid", False, False, "female", False),
    ("MATERNITY_CHILD", "Sick or disabled child (paid month)", "مرض المولود أو إعاقته (شهر بأجر)", True,
     "leave.maternity_child", "full", False, True, "female", False),
    ("MATERNITY_CHILD_UNPAID", "Sick or disabled child (unpaid month)", "مرض المولود أو إعاقته (شهر بدون أجر)", True,
     "leave.maternity_child_unpaid", "unpaid", False, True, "female", False),
    ("IDDAH", "Iddah leave", "إجازة العدة", True, "leave.iddah", "full", False, True, "female", False),
    ("IDDAH_EXTENSION", "Iddah extension until delivery (unpaid)", "تمديد العدة حتى الوضع (بدون أجر)", True,
     "leave.iddah_extension", "unpaid", False, True, "female", False),
    ("HAJJ", "Hajj leave", "إجازة الحج", True, "leave.hajj", "full", False, False, "", True),
    ("EXAM", "Exam leave", "إجازة الامتحان", True, "leave.exam", "full", False, True, "", False),
    # R37(c) (Art. 78): one paid day (or 8 hours) a week during the employer's notice, to look for other work.
    ("NOTICE_ABSENCE", "Paid weekly absence during the employer's notice (Art. 78)",
     "غياب أسبوعي بأجر خلال مهلة إشعار صاحب العمل (المادة 78)", True, "leave.notice_absence", "full", False, False, "",
     False),
)
LEAVE_TYPE_UNPAID = ("UNPAID", "Unpaid leave", "إجازة بدون أجر", False, "", "unpaid", False, False, "", False)
EVENT_TYPES = frozenset({"MARRIAGE", "BEREAVEMENT", "BEREAVEMENT_SIBLING", "PATERNITY", "MATERNITY", "MATERNITY_UNPAID",
                         "MATERNITY_CHILD", "MATERNITY_CHILD_UNPAID", "IDDAH", "IDDAH_EXTENSION"})


@transaction.atomic
def setup_entity(entity):
    """``hr_setup_entity <code>`` (operator, R4): HrSettings, seeded components and leave types for the entity's
    jurisdiction. Idempotent; a new subsidiary is data, never code. R55: route ``managing_partner`` for ALM,
    ``department_head`` for every other entity."""
    s, created = HrSettings.objects.get_or_create(entity=entity, defaults=dict(
        jurisdiction=entity.country or "SA",
        overtime_approval_route=(HrSettings.OvertimeRoute.MANAGING_PARTNER if entity.code == ALMIRAS_CODE
                                 else HrSettings.OvertimeRoute.DEPARTMENT_HEAD),
        employee_code_prefix=entity.code, alert_days=list(DEFAULT_ALERT_DAYS), has_employees=True,
    ))
    account = Account.objects.filter(entity=entity, code="5100").first()
    if account is None:
        raise error(f"{entity.code} has no account 5100; install its chart of accounts first.",
                    f"لا يوجد الحساب 5100 لدى {entity.code}؛ ثبّت دليل الحسابات أولًا.", code="account_missing")
    components = list(COMPONENT_SEED) + ([REMUNERATION_COMPONENT] if s.jurisdiction == "SA" else [])
    for code, en, ar, gosi, eosb, ot, unpaid in components:
        SalaryComponent.objects.get_or_create(entity=entity, code=code, defaults=dict(
            name_en=en, name_ar=ar, counts_for_gosi=gosi, counts_for_eosb=eosb, counts_for_overtime_base=ot,
            deducted_for_unpaid_leave=unpaid, expense_account=account, source=Source.SYSTEM))
    types = (list(LEAVE_TYPE_SEED_SA) if s.jurisdiction == "SA" else []) + [LEAVE_TYPE_UNPAID]
    for code, en, ar, statutory, pcode, pay, deducts, doc, gender, once in types:
        LeaveType.objects.get_or_create(entity=entity, code=code, defaults=dict(
            name_en=en, name_ar=ar, statutory=statutory, parameter_code=pcode, paid_rule=pay, deducts_balance=deducts,
            requires_document=doc, gender=gender, once_per_service=once))
    return s


@transaction.atomic
def set_overtime_route(entity, route, *, user, reason):
    """R55: only ``managing_partner`` changes the overtime approval route; logged in AccessLog (en/ar)."""
    access.require(user, entity, "hr.set_overtime_route")
    if route not in HrSettings.OvertimeRoute.values:
        raise error(f"Unknown overtime route {route!r}.", f"مسار العمل الإضافي {route!r} غير معروف.", code="route")
    if not reason:
        raise error("A reason is required.", "السبب مطلوب.", code="reason_required")
    s = HrSettings.objects.select_for_update().get(pk=_settings(entity).pk)
    before = s.overtime_approval_route
    s.overtime_approval_route, s.updated_by = route, user
    s.save()
    access_services.log(AccessLog.Event.ENTITY_SETTINGS_CHANGED, actor=user, entity=entity,
                        permission="hr.set_overtime_route",
                        details={"field": "hr.overtime_approval_route", "before": before, "after": route,
                                 "reason": reason, "message_en": "Overtime approval route changed",
                                 "message_ar": "تغيير مسار اعتماد العمل الإضافي"})
    return s


# =====================================================================================================
# Employees and personal data (R8 to R14, R49, R52)
# =====================================================================================================

EMPLOYEE_FIELDS = frozenset({
    "full_name_en", "full_name_ar", "nationality", "id_type", "id_number", "date_of_birth", "gender", "phone", "email",
    "address", "bank_name", "iban", "gosi_number", "gosi_registered_on", "hire_date", "linked_user", "employment_basis",
    "country_of_residence", "gosi_cohort", "gosi_first_coverage_date",
})
# AT-41: changes of these GOSI record fields are logged (en/ar) with the employee code.
GOSI_LOGGED = ("gosi_cohort", "gosi_first_coverage_date")


def _employee_values(fields):
    """``linked_user`` is the employee's own account (``Employee.user``; ``user`` is the actor in every service)."""
    values = dict(fields)
    if "linked_user" in values:
        values["user"] = values.pop("linked_user")
    return values


def _iban_ok(iban):
    s = "".join(iban.split()).upper()
    if not (15 <= len(s) <= 34 and s.isascii() and s.isalnum() and s[:2].isalpha() and s[2:4].isdigit()):
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


def _employee_problems(emp, hr_settings):
    problems, warnings = [], []
    if not (emp.full_name_en or "").strip() or not (emp.full_name_ar or "").strip():
        problems.append("Both the English and the Arabic full name are required (R8, OQ-24). / "
                        "الاسم الكامل بالإنجليزية والعربية مطلوبان.")
    jurisdiction = hr_settings.jurisdiction
    basis = emp.employment_basis
    if basis not in Basis.values:
        problems.append(f"Unknown employment basis {basis!r}. / أساس التوظيف {basis!r} غير معروف.")
    elif jurisdiction == "SA" and basis == Basis.LOCAL_ENTITY_EMPLOYEE:
        problems.append("A Saudi entity holds KSA employees or remuneration staff, not local entity employees (R49). / "
                        "يضم الكيان السعودي موظفين خاضعين لنظام العمل السعودي أو أصحاب مكافأة شهرية، لا موظفي شركات محلية.")
    elif jurisdiction != "SA" and basis != Basis.LOCAL_ENTITY_EMPLOYEE:
        problems.append(f"An entity under jurisdiction {jurisdiction} holds local entity employees only (R49). / "
                        f"لا يضم كيان خاضع للولاية {jurisdiction} إلا موظفي الشركة المحلية.")
    if basis == Basis.REMUNERATION:
        cor = (emp.country_of_residence or "").strip().upper()
        if not cor or cor == "SA":
            problems.append("Remuneration staff need a country of residence other than Saudi Arabia (R52, OQ-28). / "
                            "يلزم لصاحب المكافأة الشهرية بلد إقامة غير المملكة العربية السعودية.")
    nationality = (emp.nationality or "").upper()
    id_type, number = emp.id_type, "".join((emp.id_number or "").split())
    if nationality == "SA" and id_type != Employee.IdType.NATIONAL_ID:
        problems.append("A Saudi employee is identified by the national ID (R9). / يُعرَّف الموظف السعودي بالهوية الوطنية.")
    if nationality != "SA" and id_type == Employee.IdType.NATIONAL_ID:
        problems.append("Only Saudi nationals hold a national ID; use the iqama (R9). / "
                        "الهوية الوطنية للمواطنين السعوديين فقط؛ استخدم الإقامة.")
    if id_type == Employee.IdType.GCC_ID and nationality not in GCC:
        problems.append("A GCC ID is for GCC nationals only (R9). / الهوية الخليجية لمواطني دول المجلس فقط.")
    if not number or not number.isascii() or not number.isalnum():
        problems.append("The ID number is required: letters and digits only (R9). / رقم الهوية مطلوب: أحرف وأرقام فقط.")
    elif id_type in (Employee.IdType.NATIONAL_ID, Employee.IdType.IQAMA):
        # Compliance 2026-10-17: no official digit format found; the format is an unverified parameter and a
        # warning only [CC-verify]. Nit 19: read in the entity's own jurisdiction (never SA for a local entity).
        row = param(jurisdiction, "id.format", emp.hire_date or _today(), required=False)
        fmt = (row.value if row else {}).get(id_type)
        if fmt and not (number.isdigit() and len(number) == int(fmt["length"]) and number.startswith(fmt["prefix"])):
            warnings.append(f"ID number does not match the expected {id_type} format ({fmt['length']} digits starting "
                            f"{fmt['prefix']}; unverified rule). / رقم الهوية لا يطابق الصيغة المتوقعة "
                            f"({fmt['length']} أرقام تبدأ بـ {fmt['prefix']}؛ قاعدة غير متحقق منها).")
    if emp.gender not in Employee.Gender.values:
        problems.append("Gender is required (male or female). / الجنس مطلوب (ذكر أو أنثى).")
    if (emp.gosi_cohort or "") not in ("", *Employee.GosiCohort.values):
        problems.append(f"Unknown GOSI cohort {emp.gosi_cohort!r} (legacy_m33 or new_m273). / فئة التأمينات "
                        f"{emp.gosi_cohort!r} غير معروفة.")
    if nationality == "SA" and basis != Basis.LOCAL_ENTITY_EMPLOYEE and not (emp.gosi_cohort and
                                                                            emp.gosi_first_coverage_date):
        # hr 3.2 / payroll R55, R56: entered from the GOSI record, never derived from the hire date.
        warnings.append("GOSI cohort and first coverage date are not recorded: payroll refuses this employee's "
                        "payslip until they are entered from the GOSI record (payroll R56). / لم تُسجَّل فئة التأمينات "
                        "وتاريخ أول خضوع: ترفض الرواتب مسير هذا الموظف حتى يُدخَلا من سجل التأمينات.")
    if emp.iban and not _iban_ok(emp.iban):
        problems.append("The IBAN checksum is wrong. / رقم الآيبان غير صحيح (خانة التحقق).")
    if emp.email:
        try:
            validate_email(emp.email)
        except ValidationError:
            problems.append("The email address is not valid. / البريد الإلكتروني غير صالح.")
    if emp.user_id is not None and access_services.kind_of(emp.user) != Kind.HUMAN:
        problems.append("Only a person's user account can be linked to an employee. / لا يُربط بالموظف إلا حساب شخص.")
    try:
        emp.full_clean(exclude=["entity", "created_by", "modified_by", "id_number_hash", "employee_code", "user",
                                "full_name_en", "full_name_ar", "id_number", "gender"])
    except ValidationError as exc:
        problems.extend(exc.messages)
    return problems, warnings


def _next_number(entity, kind, year=0):
    seq, _ = HrSequence.objects.select_for_update(of=("self",)).get_or_create(entity=entity, kind=kind, year=year)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return seq.last_number


def _save_employee(emp, hr_settings):
    problems, warnings = _employee_problems(emp, hr_settings)
    if problems:
        raise _problems(problems, "employee_invalid")
    emp.nationality = emp.nationality.upper()
    emp.country_of_residence = (emp.country_of_residence or "").upper()
    emp.id_number = "".join(emp.id_number.split()).upper()
    hashes = crypto.keyed_hashes(emp.id_number)
    emp.id_number_hash = hashes[0]
    emp.warnings = warnings
    # R9 across key rotations (finding 8): a current employee hashed under an older key is found too; the partial
    # unique constraint covers rows hashed under the current key.
    clash = Employee.objects.filter(entity=emp.entity, id_number_hash__in=hashes).exclude(
        status=Employee.Status.TERMINATED)
    if emp.pk:
        clash = clash.exclude(pk=emp.pk)
    if clash.exists():
        raise error("Another current employee of this entity has the same ID number (R9).",
                    "يوجد موظف حالي آخر في هذا الكيان بنفس رقم الهوية.", code="id_duplicate")
    try:
        with transaction.atomic():
            _save(emp)
    except IntegrityError:
        raise error("Another current employee of this entity has the same ID number (R9).",
                    "يوجد موظف حالي آخر في هذا الكيان بنفس رقم الهوية.", code="id_duplicate") from None
    return emp


@transaction.atomic
def create_employee(entity, *, user, **fields):
    """Create a draft employee (``hr.manage_employee``). The employee code is the next gap-free
    ``{prefix}-EMP-{000001}`` of the entity (OQ-13)."""
    access.require(user, entity, "hr.manage_employee")
    s = _require_employees(entity)
    unknown = set(fields) - EMPLOYEE_FIELDS
    if unknown:
        raise error(f"Unknown employee fields: {sorted(unknown)}.", f"حقول غير معروفة: {sorted(unknown)}.", code="fields")
    emp = Employee(entity=entity, created_by=user, source=_source(user), **_employee_values(fields))
    emp.employee_code = ""  # assigned after validation so a refused employee uses no number
    problems, _ = _employee_problems(emp, s)
    if problems:
        raise _problems(problems, "employee_invalid")
    emp.employee_code = f"{s.employee_code_prefix}-EMP-{_next_number(entity, 'EMP'):06d}"
    _save_employee(emp, s)
    _log_gosi_changes(emp, {}, user)
    return emp


@transaction.atomic
def update_employee(employee, *, user, **fields):
    """Edit an employee (R14: terminated employees are read-only; R49: the basis is fixed once a contract version
    is approved; the entity never changes)."""
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.manage_employee")
    unknown = set(fields) - EMPLOYEE_FIELDS
    if unknown:
        raise error(f"Unknown or fixed employee fields: {sorted(unknown)}.", f"حقول غير معروفة أو ثابتة: {sorted(unknown)}.",
                    code="fields")
    if emp.status == Employee.Status.TERMINATED:
        raise error("A terminated employee is read-only (R14).", "سجل الموظف المنتهية خدمته للقراءة فقط.",
                    code="employee_terminated")
    if ("employment_basis" in fields and fields["employment_basis"] != emp.employment_basis
            and emp.contracts.filter(status=CStatus.APPROVED).exists()):
        raise error("The employment basis cannot change once a contract is approved; terminate and create a new "
                    "employee (R49, OQ-27).",
                    "لا يتغير أساس التوظيف بعد اعتماد عقد؛ أنهِ الخدمة وأنشئ سجل موظف جديدًا.", code="basis_fixed")
    before = {name: getattr(emp, name) for name in GOSI_LOGGED}
    for name, value in _employee_values(fields).items():
        setattr(emp, name, value)
    emp.modified_by, emp.modified_at = user, timezone.now()
    _save_employee(emp, _settings(emp.entity))
    _log_gosi_changes(emp, before, user)
    return emp


def _log_gosi_changes(emp, before, user):
    """AT-41: a change of the GOSI cohort or first coverage date is logged in AccessLog (en/ar) with the employee
    code (never a name or ID number, R11)."""
    changed = [name for name in GOSI_LOGGED if (before.get(name) or None) != (getattr(emp, name) or None)]
    if changed:
        access_services.log(AccessLog.Event.PROFILE_CHANGED, actor=user, entity=emp.entity,
                            permission="hr.manage_employee",
                            details={"employee_code": emp.employee_code, "fields": changed,
                                     "before": {n: str(before.get(n) or "") for n in changed},
                                     "after": {n: str(getattr(emp, n) or "") for n in changed},
                                     "message_en": "GOSI record data of the employee changed",
                                     "message_ar": "تغيّرت بيانات سجل التأمينات للموظف"})


@transaction.atomic
def delete_draft_employee(employee, *, user):
    """R14: a draft employee with no approved record may be deleted by its creator."""
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.manage_employee")
    if emp.status != Employee.Status.DRAFT or emp.created_by_id != user.pk:
        raise error("Only a draft employee is deleted, and only by its creator (R14).",
                    "لا يُحذف إلا الموظف المسودة، ومن منشئه فقط.", code="employee_delete")
    if (emp.contracts.exclude(status=CStatus.DRAFT).exists() or emp.assignments.exists()
            or emp.leave_requests.exists() or emp.documents.exists() or emp.leave_ledger.exists()):
        raise error("This employee has records; it cannot be deleted (R14).", "لهذا الموظف سجلات؛ لا يمكن حذفه.",
                    code="employee_delete")
    for c in emp.contracts.all():
        ContractComponent.objects.filter(contract=c).delete()
        c._service_delete = True
        c.delete()
    emp._service_delete = True
    emp.delete()


def employees(user, entity=None):
    """Employees ``user`` may see (R2): scoped by ``hr.view``. Every personal-data field of the rows handed out is
    masked, whatever the user's rights (finding 7, probe P14); the unmasked, logged view is ``employee_record``."""
    qs = access.scope(Employee.objects.masked(), user, "hr.view")
    return qs.filter(entity=entity) if entity is not None else qs


def employee_counts(user, entity):
    """hr.md §7: head counts of ``entity`` by status and employment basis, no record and no personal data. For
    viewers (``hr.view_counts``) and every ``hr.view`` holder."""
    if not access.has(user, entity, "hr.view"):
        access.require(user, entity, "hr.view_counts")
    rows = Employee.objects.filter(entity=entity).values_list("status", "employment_basis")
    by_status, by_basis = {}, {}
    for status, basis in rows:
        by_status[status] = by_status.get(status, 0) + 1
        by_basis[basis] = by_basis.get(basis, 0) + 1
    return {"entity": entity.code, "total": len(rows), "by_status": by_status, "by_basis": by_basis}


def get_employee(user, pk):
    """One employee through scope: another entity's employee is 404 (R2, AT-1)."""
    return access.get_scoped_or_404(Employee.objects.masked(), user, "hr.view", pk=pk)


def _visible_fields(user, entity):
    """Personal-data fields ``user`` may see unmasked in ``entity`` (R10, §7)."""
    if not access.has(user, entity, "hr.view_personal_data"):
        return ()
    grant = access.require(user, entity, "hr.view_personal_data")
    roles = set(Membership.objects.filter(pk__in=grant.membership_ids).values_list("role__code", flat=True))
    if roles & {"hr_officer", "managing_partner"}:
        return Employee.PERSONAL_FIELDS
    return Employee.PAYMENT_FIELDS  # payroll officer: bank, IBAN and ID for WPS only


def employee_record(employee, *, user):
    """The employee as a dict, personal data masked unless the user holds ``hr.view_personal_data`` in that entity;
    every unmasked view writes a ``personal_data_viewed`` AccessLog row with the employee code and fields (R10)."""
    emp = Employee.objects.get(pk=employee.pk)
    access.require(user, emp.entity, "hr.view")
    shown = _visible_fields(user, emp.entity)
    record = {
        "employee_code": emp.employee_code, "entity": emp.entity.code, "status": emp.status,
        "employment_basis": emp.employment_basis, "nationality": emp.nationality, "id_type": emp.id_type,
        "hire_date": emp.hire_date, "gosi_registered_on": emp.gosi_registered_on,
    }
    for name in Employee.PERSONAL_FIELDS:
        record[name] = getattr(emp, name) if name in shown else MASK
    if shown:
        access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=emp.entity,
                            permission="hr.view_personal_data",
                            details={"employee_code": emp.employee_code, "fields": list(shown)})
    return record


EXPORT_LABELS = {
    "full_name_en": ("Full name (English)", "الاسم الكامل (بالإنجليزية)"),
    "full_name_ar": ("Full name (Arabic)", "الاسم الكامل (بالعربية)"),
    "id_number": ("ID number", "رقم الهوية"), "date_of_birth": ("Date of birth", "تاريخ الميلاد"),
    "gender": ("Gender", "الجنس"), "phone": ("Phone", "الهاتف"), "email": ("Email", "البريد الإلكتروني"),
    "address": ("Address", "العنوان"), "bank_name": ("Bank", "البنك"), "iban": ("IBAN", "رقم الآيبان"),
    "country_of_residence": ("Country of residence", "بلد الإقامة"),
    "gosi_number": ("GOSI number", "رقم التأمينات"), "gosi_cohort": ("GOSI cohort", "فئة التأمينات"),
    "gosi_first_coverage_date": ("First subject to GOSI on", "تاريخ أول خضوع للتأمينات"),
}


def export_personal_data(employee, *, user):
    """R12: the employee's own personal data with English and Arabic labels; logged."""
    emp = Employee.objects.get(pk=employee.pk)
    access.require(user, emp.entity, "hr.export_personal_data")
    rows = [{"field": name, "label_en": EXPORT_LABELS[name][0], "label_ar": EXPORT_LABELS[name][1],
             "value": str(getattr(emp, name) or "")} for name in Employee.PERSONAL_FIELDS]
    rows.insert(0, {"field": "employee_code", "label_en": "Employee code", "label_ar": "رمز الموظف",
                    "value": emp.employee_code})
    access_services.log(AccessLog.Event.PERSONAL_DATA_EXPORTED, actor=user, entity=emp.entity,
                        permission="hr.export_personal_data",
                        details={"employee_code": emp.employee_code, "fields": list(Employee.PERSONAL_FIELDS)})
    return rows


# =====================================================================================================
# Departments, grades, overtime routing (R54, R55)
# =====================================================================================================

def _head_problems(entity, head):
    if head is None:
        return []
    if head.entity_id != entity.pk:
        return ["A department head is an employee of the same entity (R54). / رئيس القسم موظف في الكيان نفسه."]
    if head.status != Employee.Status.ACTIVE:
        return ["A department head is an active employee (R54). / رئيس القسم موظف نشط."]
    if head.user_id is None:
        return ["A department head needs a linked user account to approve overtime (R54). / "
                "يلزم ربط رئيس القسم بحساب مستخدم لاعتماد العمل الإضافي."]
    return []


@transaction.atomic
def create_department(entity, *, user, code, name_en, name_ar, parent=None, head=None):
    access.require(user, entity, "hr.manage_org")
    _same_entity(entity, parent, "parent department", "القسم الأعلى")
    problems = _head_problems(entity, head)
    if not name_en or not name_ar:
        problems.append("Department names in English and Arabic are required. / اسم القسم بالإنجليزية والعربية مطلوب.")
    if problems:
        raise _problems(problems, "department_invalid")
    try:
        with transaction.atomic():
            return Department.objects.create(entity=entity, code=code, name_en=name_en, name_ar=name_ar, parent=parent,
                                             head=head, created_by=user, source=_source(user))
    except IntegrityError:
        raise error(f"Department {code} already exists.", f"القسم {code} موجود مسبقًا.", code="duplicate") from None


@transaction.atomic
def set_department_head(department, head, *, user):
    dept = Department.objects.select_for_update().get(pk=department.pk)
    access.require(user, dept.entity, "hr.manage_org")
    problems = _head_problems(dept.entity, head)
    if problems:
        raise _problems(problems, "department_head")
    dept.head, dept.modified_by, dept.modified_at = head, user, timezone.now()
    dept.save()
    return dept


@transaction.atomic
def create_grade(entity, *, user, code, name_en, name_ar, overtime_eligible=None, sort_order=0):
    """R54: ``overtime_eligible`` must be given explicitly (True or False)."""
    access.require(user, entity, "hr.manage_org")
    if not isinstance(overtime_eligible, bool):
        raise error("State whether the grade is eligible for overtime (True or False) (R54).",
                    "حدّد صراحةً ما إذا كانت الدرجة مؤهلة للعمل الإضافي.", code="overtime_flag_required")
    if not name_en or not name_ar:
        raise error("Grade names in English and Arabic are required.", "اسم الدرجة بالإنجليزية والعربية مطلوب.",
                    code="grade_invalid")
    try:
        with transaction.atomic():
            return Grade.objects.create(entity=entity, code=code, name_en=name_en, name_ar=name_ar,
                                        overtime_eligible=overtime_eligible, sort_order=sort_order, created_by=user,
                                        source=_source(user))
    except IntegrityError:
        raise error(f"Grade {code} already exists.", f"الدرجة {code} موجودة مسبقًا.", code="duplicate") from None


@transaction.atomic
def set_component_account(component, account, *, user):
    """R47: a component's expense account is the entity's own postable expense account, never Sharia-prohibited where
    the entity enforces Sharia (the ledger checks again when payroll posts)."""
    comp = SalaryComponent.objects.select_for_update().get(pk=component.pk)
    access.require(user, comp.entity, "hr.manage_org")
    entity = comp.entity
    problems = []
    if account.entity_id != entity.pk:
        problems.append(f"Account {account.code} belongs to another entity (R1). / الحساب {account.code} يخص كيانًا آخر.")
    else:
        if account.type != Account.Type.EXPENSE or not account.is_postable or not account.is_active:
            problems.append(f"Account {account.code} is not a postable expense account. / الحساب {account.code} ليس حساب "
                            "مصروفات قابلًا للترحيل.")
        if account.sharia_prohibited and entity.sharia_enforced:
            problems.append(f"Account {account.code} ({account.name_en}) is Sharia-prohibited (R47). / الحساب "
                            f"{account.code} ({account.name_ar}) محظور شرعًا.")
    if problems:
        raise _problems(problems, "component_account")
    comp.expense_account = account
    comp.save()
    return comp


@dataclass(frozen=True)
class OvertimeApprover:
    """Who approves an overtime input (R55, payroll R45): ``route`` and, for a department head, the user."""

    route: str
    user: object = None
    reason_en: str = ""
    reason_ar: str = ""


def overtime_approver(employee, on, *, user):
    """R55 / OQ-30: the managing partner where the entity's route says so; else the user linked to the head of the
    employee's department on ``on``. A department without head, a head who is the employee, or a grade not eligible
    is answered explicitly. No overtime is calculated here."""
    emp = Employee.objects.select_related("entity").get(pk=employee.pk)
    access.require(user, emp.entity, "hr.view")
    if emp.employment_basis == Basis.REMUNERATION:
        raise error("Remuneration staff have no overtime (R52).", "لا عمل إضافي لأصحاب المكافأة الشهرية.",
                    code="remuneration_no_overtime")
    s = _settings(emp.entity)
    terms = _terms_on(emp, on)
    if terms is None:
        raise error(f"No approved contract is in force on {on}.", f"لا يوجد عقد معتمد ساري بتاريخ {on}.", code="no_terms")
    if terms.grade is not None and not terms.grade.overtime_eligible:
        raise error(f"Grade {terms.grade.code} is not eligible for overtime (R54).",
                    f"الدرجة {terms.grade.code} غير مؤهلة للعمل الإضافي.", code="grade_no_overtime")
    mp = HrSettings.OvertimeRoute.MANAGING_PARTNER
    if s.overtime_approval_route == mp:
        return OvertimeApprover(route=mp)
    head = terms.department.head if terms.department else None
    if head is None or head.user_id is None or head.status != Employee.Status.ACTIVE:
        return OvertimeApprover(route=mp, reason_en="Department has no active head with a user (OQ-30)",
                                reason_ar="لا يوجد للقسم رئيس نشط مرتبط بحساب مستخدم")
    if head.pk == emp.pk:
        return OvertimeApprover(route=mp, reason_en="Own overtime of the department head (OQ-30)",
                                reason_ar="عمل إضافي لرئيس القسم نفسه")
    return OvertimeApprover(route=HrSettings.OvertimeRoute.DEPARTMENT_HEAD, user=head.user)


# =====================================================================================================
# Contracts (R15 to R21, R49 to R56)
# =====================================================================================================

CONTRACT_FIELDS = frozenset({
    "effective_date", "contract_type", "start_date", "end_date", "calendar_basis", "calendar_basis_ref", "probation_periods",
    "probation_agreement_ref", "renewal_count", "extended_hours_ref", "job_title_en",
    "job_title_ar", "occupation_code", "weekly_hours", "daily_hours", "annual_leave_days", "notice_days_employer",
    "notice_days_employee", "currency", "department", "grade", "qiwa_contract_ref", "suspension_agreement_ref", "paid_monthly",
})


def _currency(code):
    cur = Currency.objects.filter(code=str(getattr(code, "code", code) or "").upper()).first()
    if cur is None or not cur.is_active:
        raise error(f"Currency {code} is unknown or inactive.", f"العملة {code} غير معروفة أو غير نشطة.",
                    code="currency_inactive")
    return cur


def _set_components(contract, components):
    ContractComponent.objects.filter(contract=contract).delete()
    for code, amount in (components or {}).items():
        comp = SalaryComponent.objects.filter(entity=contract.entity, code=code).first()
        if comp is None or not comp.is_active:
            raise error(f"Component {code} is not in {contract.entity.code}'s active catalogue (R18).",
                        f"المكوّن {code} ليس ضمن مكوّنات الكيان {contract.entity.code} النشطة.", code="component_unknown")
        value = _decimal(amount, code)
        ContractComponent.objects.create(entity=contract.entity, contract=contract, component=comp, monthly_amount=value)


def _contract_values(contract, fields):
    if "employee" in fields or "entity" in fields:
        raise error("An amendment cannot change the employee or the entity; a transfer is a termination and a new hire "
                    "(R20, OQ-14).", "لا يغيّر التعديل الموظف أو الكيان؛ النقل إنهاء خدمة وتعيين جديد.",
                    code="contract_identity")
    unknown = set(fields) - CONTRACT_FIELDS
    if unknown:
        raise error(f"Unknown contract fields: {sorted(unknown)}.", f"حقول عقد غير معروفة: {sorted(unknown)}.",
                    code="fields")
    for name, value in fields.items():
        if name == "currency":
            value = _currency(value)
        if name in ("weekly_hours", "daily_hours"):
            value = _decimal(value, name)
        setattr(contract, name, value)


@transaction.atomic
def create_contract(employee, *, user, components=None, **fields):
    """A new draft contract version (``hr.edit_contract_draft``): version 1 is the original, later ones amendments
    (R20). ``components`` maps component codes to monthly amounts (Decimal or string; float refused, R18)."""
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.edit_contract_draft")
    _require_employees(emp.entity)
    if emp.status == Employee.Status.TERMINATED:
        raise error("A terminated employee gets no new contract (R14).", "لا يُنشأ عقد لموظف منتهية خدمته.",
                    code="employee_terminated")
    last = EmploymentContract.objects.filter(employee=emp).aggregate(m=Max("version_no"))["m"] or 0
    contract = EmploymentContract(entity=emp.entity, employee=emp, version_no=last + 1,
                                  kind="original" if last == 0 else "amendment", created_by=user, source=_source(user))
    base = (EmploymentContract.objects.filter(employee=emp, status=CStatus.APPROVED).order_by("-version_no").first()
            if last else None)
    if base is None:
        fields.setdefault("currency", emp.entity.base_currency_id)
    else:  # an amendment starts from the latest approved terms
        for name in CONTRACT_FIELDS - {"effective_date"}:
            if name not in fields:
                setattr(contract, name, getattr(base, name))
        if components is None:
            components = {c.component.code: c.monthly_amount for c in base.components.select_related("component")}
    _contract_values(contract, fields)
    contract.save()
    _set_components(contract, components)
    return contract


@transaction.atomic
def update_contract(contract, *, user, components=None, **fields):
    """Edit a draft version (R15: only drafts are edited)."""
    c = _lock(EmploymentContract, contract.pk)
    access.require(user, c.entity, "hr.edit_contract_draft")
    if c.status != CStatus.DRAFT:
        raise error("Only draft contract versions are edited (R15).", "لا تُعدَّل إلا مسودات العقود.", code="not_draft")
    _contract_values(c, fields)
    c.modified_by, c.modified_at = user, timezone.now()
    c.save()
    if components is not None:
        _set_components(c, components)
    return c


def _approved_versions(emp):
    return EmploymentContract.objects.filter(employee=emp, status=CStatus.APPROVED)


def _probation_end(use, contract, jurisdiction="SA"):
    """Last probation day: probation days counted from the start, skipping official holidays and sick days
    (Regs Art. 19)."""
    days = contract.probation_days
    if not days:
        return None
    far = contract.start_date + timedelta(days=days * 2 + 60)
    skip = _holidays(use, jurisdiction, contract.start_date, far) | set(
        _live_sick_days(SickLeaveDay.objects.filter(employee=contract.employee)).values_list("date", flat=True))
    counted, d = 0, contract.start_date
    while True:
        if d not in skip:
            counted += 1
            if counted == days:
                return d
        d += timedelta(days=1)


def _statutory_contract_problems(contract, emp, use, jurisdiction):
    """Saudi statutory checks (R16, R17, R19, R21, R37, R56) with parameters valid on the effective date."""
    problems, warnings = [], []
    on = contract.effective_date
    # R17 as corrected (Art. 53-54, Regs Art. 19): one maximum, consecutive periods, one probation per employer.
    periods = contract.probation_periods or []
    if not isinstance(periods, list) or any(not isinstance(p, int) or isinstance(p, bool) or p <= 0 for p in periods):
        problems.append("Probation periods are a list of whole days above zero. / فترات التجربة قائمة بأيام صحيحة أكبر من صفر.")
    else:
        maximum = int(use.get(jurisdiction, "probation.max_days", on)["days"])
        if sum(periods) > maximum:
            problems.append(f"Probation totals {sum(periods)} days; the maximum is {maximum} days in all cases (Art. 53). / "
                            f"مجموع فترة التجربة {sum(periods)} يومًا؛ والحد الأقصى {maximum} يومًا في جميع الأحوال.")
        original = _approved_versions(emp).order_by("version_no").first()
        if contract.version_no > 1 and original is not None and periods != (original.probation_periods or []):
            problems.append("Probation is fixed in the original contract; an amendment cannot add or change it "
                            "(Art. 54). / تُحدَّد فترة التجربة في العقد الأصلي؛ ولا يضيفها التعديل أو يغيّرها.")
        if contract.version_no == 1 and periods:
            months = int(use.get(jurisdiction, "probation.repeat", on)["months_since_last_employment"])
            earlier = Termination.objects.filter(
                entity=emp.entity, employee__id_number_hash=emp.id_number_hash, status=Termination.Status.APPROVED,
            ).exclude(employee=emp).order_by("-last_working_day").first()
            if earlier is not None:
                recent = _add_months(earlier.last_working_day, months) > contract.start_date
                same_job = (earlier.contract.job_title_ar.strip() == contract.job_title_ar.strip()
                            or earlier.contract.job_title_en.strip().lower() == contract.job_title_en.strip().lower())
                if recent and same_job:
                    problems.append(f"This person was employed here within {months} months in the same job: no new "
                                    "probation (Art. 54). / عمل هذا الشخص لدى المنشأة نفسها في الوظيفة ذاتها خلال "
                                    f"{months} أشهر: لا تجوز فترة تجربة جديدة.")
                elif not (contract.probation_agreement_ref or "").strip():
                    problems.append("This person was employed here before: a new probation (another job, or "
                                    f"{months}+ months since the last employment ended) needs the written agreement "
                                    "reference (Art. 54). / سبق أن عمل هذا الشخص لدى المنشأة: تتطلب فترة التجربة الجديدة "
                                    "(وظيفة أخرى أو مرور المدة منذ انتهاء العمل السابق) مرجع الاتفاق المكتوب.")
    # R16 as corrected (Art. 37, Regs Art. 12): a non-Saudi's contract is always fixed-term.
    if contract.contract_type == EmploymentContract.ContractType.UNLIMITED and not emp.is_saudi:
        problems.append("A non-Saudi's contract must be fixed-term; an open-ended contract is refused (Art. 37, Regs "
                        "Art. 12). / يجب أن يكون عقد غير السعودي محدد المدة؛ ويُرفض العقد غير محدد المدة.")
    if contract.contract_type == EmploymentContract.ContractType.FIXED and emp.is_saudi and contract.end_date:
        # R16(b), R63 (Art. 55): 3 consecutive renewals or 4 years in total make the contract open-ended; the version
        # that reaches the limit warns, a later fixed term is refused (the next version must be open-ended).
        rule = use.get(jurisdiction, "contract.saudi_open_ended", on)
        max_renewals = int(rule["renewals"])
        fixed = list(_approved_versions(emp).filter(contract_type="fixed").exclude(pk=contract.pk).order_by("version_no"))
        ends = {c.end_date for c in fixed if c.end_date}
        renewals = max([contract.renewal_count, len(ends | {contract.end_date}) - 1] + [c.renewal_count for c in fixed])
        first_start = min([c.start_date for c in fixed] + [contract.start_date])
        limit = _anniversary(first_start, int(rule["years"]), contract.calendar_basis)
        prev = fixed[-1] if fixed else None
        renewed = prev is not None and prev.end_date != contract.end_date
        prev_reached = renewed and (max(prev.renewal_count, len(ends) - 1) >= max_renewals or limit <= prev.end_date)
        if renewals > max_renewals or prev_reached:
            problems.append(f"This Saudi employee's fixed-term contract has reached {max_renewals} consecutive renewals or "
                            f"{rule['years']} years: it is open-ended and the next version must be open-ended (Art. 55). "
                            f"/ بلغ عقد هذا الموظف السعودي محدد المدة {max_renewals} تجديدات متتالية أو {rule['years']} "
                            "سنوات: أصبح غير محدد المدة ويجب أن تكون النسخة التالية غير محددة المدة (المادة 55).")
        elif renewals == max_renewals or limit <= contract.end_date:
            warnings.append("This fixed-term contract reaches the renewal or four-year limit: it becomes open-ended for "
                            "a Saudi employee (Art. 55). / يبلغ هذا العقد حد التجديد أو السنوات الأربع: يصبح غير "
                            "محدد المدة للموظف السعودي.")
    # R19 (Art. 98, 99): 8 / 48 hours; 9 a day only with the ministerial decision for the job's category.
    max_daily = _decimal(use.get(jurisdiction, "hours.max_daily", on)["hours"], "hours")
    max_weekly = _decimal(use.get(jurisdiction, "hours.max_weekly", on)["hours"], "hours")
    if (contract.extended_hours_ref or "").strip():
        max_daily = _decimal(use.get(jurisdiction, "hours.max_daily_extended", on)["hours"], "hours")
    if contract.daily_hours > max_daily or contract.weekly_hours > max_weekly:
        problems.append(f"Working hours exceed {max_daily} a day or {max_weekly} a week (Art. 98, 99). / "
                        f"ساعات العمل تتجاوز {max_daily} يوميًا أو {max_weekly} أسبوعيًا.")
    # R21, R56.
    if contract.annual_leave_days not in ANNUAL_LEAVE_CONTRACT_DAYS:
        problems.append("Annual leave is 21 or 30 days, never more than 30 (H14, R56). / "
                        "الإجازة السنوية 21 أو 30 يومًا، ولا تزيد على 30 يومًا أبدًا.")
    else:
        statutory = _statutory_annual_days(use, jurisdiction, emp, contract, on)
        if contract.annual_leave_days < statutory:
            problems.append(f"Annual leave {contract.annual_leave_days} days is below the statutory {statutory} days for "
                            f"this service length (R21, Art. 109). / الإجازة السنوية {contract.annual_leave_days} يومًا "
                            f"أقل من الحد النظامي {statutory} يومًا لمدة الخدمة هذه.")
    # R37 notice minimums for open-ended contracts (Art. 75).
    if contract.contract_type == EmploymentContract.ContractType.UNLIMITED:
        notice = use.get(jurisdiction, "notice.min_days", on)
        if contract.notice_days_employer < int(notice["employer"]) or contract.notice_days_employee < int(notice["employee"]):
            problems.append(f"Notice is at least {notice['employer']} days for the employer and {notice['employee']} "
                            f"for the employee (Art. 75). / مدة الإشعار {notice['employer']} يومًا على الأقل لصاحب "
                            f"العمل و{notice['employee']} يومًا للموظف.")
    _year_days(use, jurisdiction, contract.calendar_basis, on)  # the calendar basis must be configured (Art. 10)
    return problems, warnings


def _statutory_annual_days(use, jurisdiction, emp, contract, on):
    value = use.get(jurisdiction, "leave.annual", on)
    _year_days(use, jurisdiction, contract.calendar_basis, on)  # the calendar basis is configured
    days = 0
    for tier in value["tiers"]:
        years = int(Decimal(tier["from_years"]))
        if years == 0 or on > _service_anniversary(use, jurisdiction, emp, years, contract.calendar_basis):
            days = max(days, int(Decimal(tier["days"])))
    return days


def _contract_problems(contract, *, statutory, use):
    """All checks of a contract version. ``statutory=False`` skips the jurisdiction's statutory checks (submission
    of a local entity employee's contract: master data is kept, approval refuses, R5/R51)."""
    emp, entity = contract.employee, contract.entity
    problems, warnings = [], []
    basis = emp.employment_basis
    if not contract.job_title_ar or not contract.job_title_en:
        problems.append("The job title in English and Arabic is required. / المسمى الوظيفي بالإنجليزية والعربية مطلوب.")
    if contract.effective_date < contract.start_date:
        problems.append("The effective date is on or after the start date (R20). / تاريخ السريان في تاريخ البداية أو بعده.")
    if contract.contract_type == EmploymentContract.ContractType.FIXED:
        if contract.end_date is None or contract.end_date <= contract.start_date:
            problems.append("A fixed-term contract needs an end date after its start date (R16). / "
                            "يلزم للعقد محدد المدة تاريخ نهاية بعد تاريخ بدايته.")
    elif contract.end_date is not None:
        problems.append("An open-ended contract has no end date (R16). / لا تاريخ نهاية للعقد غير محدد المدة.")
    if contract.calendar_basis not in EmploymentContract.CalendarBasis.values:
        problems.append(f"Unknown calendar basis {contract.calendar_basis!r}. / التقويم {contract.calendar_basis!r} غير معروف.")
    elif (basis == Basis.KSA_EMPLOYEE and contract.calendar_basis == EmploymentContract.CalendarBasis.GREGORIAN
            and not (contract.calendar_basis_ref or "").strip()):
        # R58 (Art. 10): Hijri unless the contract or work regulations state Gregorian.
        problems.append("A Gregorian calendar basis needs the contract clause or work-regulations article that states "
                        "it, e.g. unified contract cl. 14.6 (Art. 10, R58). / يلزم للتقويم الميلادي ذكر بند العقد أو مادة "
                        "لائحة تنظيم العمل التي تنص عليه، مثل البند 14.6 من العقد الموحد (المادة 10).")
    comps = list(contract.components.select_related("component"))
    codes = [c.component.code for c in comps]
    currency = contract.currency
    for c in comps:
        if c.monthly_amount <= 0:
            problems.append(f"{c.component.code}: the monthly amount must be above zero. / "
                            f"{c.component.code}: يجب أن يكون المبلغ الشهري أكبر من صفر.")
        if not fx.has_places(c.monthly_amount, currency.minor_units):
            problems.append(f"{c.component.code}: {currency.code} amounts have at most {currency.minor_units} decimals "
                            f"({c.monthly_amount.normalize()}) (R18, R53). / {c.component.code}: المبالغ بعملة "
                            f"{currency.code} بحد أقصى {currency.minor_units} خانات عشرية.")
    if basis == Basis.REMUNERATION:
        # R52, R53.
        if codes != ["REMUNERATION"]:
            problems.append("A remuneration contract has exactly one component, REMUNERATION (R52). / "
                            "لعقد المكافأة الشهرية مكوّن واحد فقط هو المكافأة الشهرية.")
        if contract.probation_days or contract.annual_leave_days:
            problems.append("Remuneration staff have no probation and no annual leave (R52). / "
                            "لا فترة تجربة ولا إجازة سنوية لأصحاب المكافأة الشهرية.")
        if currency.minor_units not in (2, 3) or not currency.is_active:
            problems.append(f"Currency {currency.code} is not supported (2 or 3 decimals, active). / "
                            f"العملة {currency.code} غير مدعومة.")
    else:
        # R18, R53: one BASIC, the entity's functional currency.
        if codes.count("BASIC") != 1:
            problems.append("The contract needs exactly one BASIC component above zero (R18). / "
                            "يلزم العقد مكوّن راتب أساسي واحد أكبر من صفر.")
        if "REMUNERATION" in codes:
            problems.append("REMUNERATION is only for remuneration staff (R52). / المكافأة الشهرية لأصحابها فقط.")
        if currency.code != entity.base_currency_id:
            problems.append(f"The contract is in {entity.base_currency_id}, the entity's functional currency, not "
                            f"{currency.code} (R18, R53, H10). / يكون العقد بعملة الكيان الوظيفية "
                            f"{entity.base_currency_id} لا بعملة {currency.code}.")
        # R54.
        for obj, en, ar in ((contract.department, "department", "القسم"), (contract.grade, "grade", "الدرجة")):
            if obj is None:
                problems.append(f"An active {en} of the entity is required (R54). / يلزم تحديد {ar} نشط من الكيان.")
            elif obj.entity_id != entity.pk or not obj.is_active:
                problems.append(f"The {en} must be active and of the same entity (R54). / يجب أن يكون {ar} نشطًا ومن "
                                "الكيان نفسه.")
        if statutory:
            jurisdiction = _jurisdiction(emp)
            p, w = _statutory_contract_problems(contract, emp, use, jurisdiction)
            problems += p
            warnings += w
    return problems, warnings


@transaction.atomic
def submit_contract(contract, *, user):
    c = _lock(EmploymentContract, contract.pk)
    access.require(user, c.entity, "hr.submit_contract")
    if c.status != CStatus.DRAFT:
        raise error("Only drafts are submitted.", "تُقدَّم المسودات فقط.", code="not_draft")
    use = ParamUse()
    statutory = c.employee.employment_basis == Basis.KSA_EMPLOYEE
    problems, warnings = _contract_problems(c, statutory=statutory, use=use)
    if problems:
        raise _problems(problems, "contract_invalid")
    c.status, c.submitted_by, c.warnings = CStatus.SUBMITTED, user, warnings
    c.modified_by, c.modified_at = user, timezone.now()
    return _save(c)


@transaction.atomic
def approve_contract(contract, *, user):
    """R15: ``hr.approve_contract`` (A) by someone other than the creator, submitter or last modifier, never for the
    approver's own contract. Statutory checks run again with the parameters valid on the effective date (R7).
    The employee becomes active once an approved version has started (R8)."""
    c = _lock(EmploymentContract, contract.pk)
    access.require(user, c.entity, "hr.approve_contract", document=c)
    emp = c.employee
    _deny_own(user, c.entity, "hr.approve_contract", emp)
    if c.status != CStatus.SUBMITTED:
        raise error("Only submitted contract versions are approved.", "تُعتمد نسخ العقود المقدمة فقط.", code="not_submitted")
    use = ParamUse()
    problems, warnings = _contract_problems(c, statutory=emp.employment_basis != Basis.REMUNERATION, use=use)
    if emp.employment_basis == Basis.KSA_EMPLOYEE and not c.qiwa_contract_ref.strip():
        warnings.append(QIWA_MISSING[0] + " The employee becomes active only once it is recorded (R61). / "
                        + QIWA_MISSING[1] + " ولا يصبح الموظف نشطًا إلا بعد تسجيله.")
    if problems:
        raise _problems(problems, "contract_invalid")
    use.check_verified()
    use.mark_used()
    c.status, c.approved_by, c.approved_at = CStatus.APPROVED, user, timezone.now()
    c.warnings, c.parameter_ids = warnings, use.ids
    _save(c)
    _activate_if_started(emp)
    return c


@transaction.atomic
def reject_contract(contract, *, user, reason_ar, reason_en=""):
    c = _lock(EmploymentContract, contract.pk)
    access.require(user, c.entity, "hr.approve_contract", document=c)
    if c.status != CStatus.SUBMITTED or not reason_ar:
        raise error("A submitted version is rejected with an Arabic reason.", "تُرفض النسخة المقدمة مع ذكر السبب بالعربية.",
                    code="reject")
    c.status, c.rejected_reason_ar, c.rejected_reason_en = CStatus.REJECTED, reason_ar, reason_en
    return _save(c)


@transaction.atomic
def withdraw_contract(contract, *, user):
    c = _lock(EmploymentContract, contract.pk)
    access.require(user, c.entity, "hr.edit_contract_draft")
    if c.status not in (CStatus.DRAFT, CStatus.SUBMITTED):
        raise error("Only draft or submitted versions are withdrawn.", "تُسحب المسودات والنسخ المقدمة فقط.", code="withdraw")
    c.status = CStatus.WITHDRAWN
    return _save(c)


def _terms_on(employee, day):
    """R20: the approved version with the latest effective date on or before ``day`` (ties: higher version)."""
    return (EmploymentContract.objects.filter(employee=employee, status=CStatus.APPROVED, effective_date__lte=day)
            .select_related("department", "grade", "department__head", "currency")
            .order_by("-effective_date", "-version_no").first())


def terms_on(employee, day, *, user):
    """The contract terms in force on ``day`` (R20); payroll computes retro differences from them."""
    access.require(user, employee.entity, "hr.view")
    return _terms_on(employee, day)


def _contract_end(contract):
    """Last day of a version: its termination end, else its end date extended by the suspended days (R62: unpaid
    leave beyond ``leave.unpaid.suspension_after_days`` suspends the contract and extends a fixed term, Art. 116,
    Regs Art. 25), unless ``suspension_agreement_ref`` records a written agreement that the term is not extended."""
    if contract.ended_on or contract.end_date is None:
        return contract.ended_on
    emp = contract.employee
    if emp.employment_basis != Basis.KSA_EMPLOYEE or (contract.suspension_agreement_ref or "").strip():
        return contract.end_date
    suspended = _suspended_days(ParamUse(), "SA", emp, contract.calendar_basis, contract.start_date,
                                contract.end_date)
    return contract.end_date + timedelta(days=len(suspended))


def service_anniversary(employee, years, *, user, on=None):
    """R58 (Art. 10): the date ``employee`` completes ``years`` service years, in the calendar of the contract in
    force on ``on`` (default today): the same Umm al-Qura day for ``hijri``, the same Gregorian day for
    ``gregorian``. Unpaid suspended days are not added here (R62 moves the anniversary of accrual tiers)."""
    access.require(user, employee.entity, "hr.view")
    terms = _terms_on(employee, on or _today()) or _approved_versions(employee).order_by("version_no").first()
    if terms is None:
        raise error("The employee has no approved contract.", "لا يوجد للموظف عقد معتمد.", code="no_terms")
    return _anniversary(employee.hire_date, years, terms.calendar_basis)


def contract_end_date(contract, *, user):
    """The effective last day of a contract version (termination, or fixed term extended by suspension)."""
    access.require(user, contract.entity, "hr.view")
    return _contract_end(contract)


QIWA_MISSING = ("The Qiwa contract reference is missing: contracts are documented on the Ministry's platform (Regs "
                "Art. 18).", "مرجع العقد في منصة قوى غير مسجل: توثَّق العقود إلكترونيًا عبر منصة الوزارة (المادة 18 من "
                "اللائحة التنفيذية).")


def _activation_problem(emp, today):
    """R8, R61: why ``emp`` cannot become active on ``today`` (None when it can). A ``ksa_employee`` needs the
    Qiwa reference on the approved version in force (Regs Art. 18; default refusal, OQ-21)."""
    first = _approved_versions(emp).order_by("version_no").first()
    terms = _terms_on(emp, today)
    if first is None or terms is None or first.start_date > today:
        return error("An employee becomes active with an approved contract that has started (R8).",
                     "يصبح الموظف نشطًا بعقد معتمد بدأ سريانه.", code="not_started")
    if emp.employment_basis == Basis.KSA_EMPLOYEE and not (terms.qiwa_contract_ref or "").strip():
        return error(*QIWA_MISSING, code="qiwa_required")
    return None


def _activate_if_started(emp):
    emp = _lock(Employee, emp.pk)
    if emp.status == Employee.Status.DRAFT and _activation_problem(emp, _today()) is None:
        emp.status = Employee.Status.ACTIVE
        _save(emp, update_fields=["status"])
    return emp


@transaction.atomic
def activate_employee(employee, *, user):
    """R8, R61 / AT-39: make a draft employee active; refused (en/ar, naming Regs Art. 18) while the approved
    version of a ``ksa_employee`` has no Qiwa reference."""
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.manage_employee")
    if emp.status != Employee.Status.DRAFT:
        raise error("Only a draft employee is activated.", "لا يُفعَّل إلا الموظف المسودة.", code="employee_status")
    problem = _activation_problem(emp, _today())
    if problem is not None:
        raise problem
    emp.status = Employee.Status.ACTIVE
    return _save(emp, update_fields=["status"])


@transaction.atomic
def record_qiwa_reference(contract, reference, *, user):
    """R61: record the Qiwa documentation reference of a contract version. On an approved version it may only be
    filled in once (the only change to an approved version besides ``ended_on``; the PostgreSQL trigger agrees);
    a draft employee whose version has started then becomes active."""
    c = _lock(EmploymentContract, contract.pk)
    access.require(user, c.entity, "hr.edit_contract_draft")
    reference = (reference or "").strip()
    if not reference or len(reference) > EmploymentContract._meta.get_field("qiwa_contract_ref").max_length:
        raise error("Enter the Qiwa contract reference (at most 40 characters).",
                    "أدخل مرجع العقد في منصة قوى (40 حرفًا بحد أقصى).", code="qiwa_reference")
    if c.status in (CStatus.REJECTED, CStatus.WITHDRAWN) or (c.status == CStatus.APPROVED and c.qiwa_contract_ref):
        raise error("The Qiwa reference of this version is already recorded or the version is closed.",
                    "مرجع قوى لهذه النسخة مسجل مسبقًا أو النسخة مغلقة.", code="qiwa_reference")
    c.qiwa_contract_ref, c.modified_by, c.modified_at = reference, user, timezone.now()
    if c.status == CStatus.APPROVED:
        _save(c, update_fields=["qiwa_contract_ref"])  # the approval's own warnings stay as they were
        _activate_if_started(c.employee)
    else:
        _save(c, update_fields=["qiwa_contract_ref", "modified_by", "modified_at"])
    return c


# =====================================================================================================
# Assignments and secondments (R22 to R28)
# =====================================================================================================

ASSIGNMENT_FIELDS = frozenset({"kind", "receiving_entity", "receiving_party_en", "receiving_party_ar", "role_title_en",
                               "role_title_ar", "start_date", "end_date"})
ASt = Assignment.Status


def _assignment_problems(a):
    problems = []
    emp = a.employee
    if a.receiving_entity is not None:
        if a.receiving_entity_id == a.entity_id:
            problems.append("The receiving entity is another entity (R22). / الكيان المستقبِل كيان آخر.")
        elif access_services.group_parent_of(a.receiving_entity).pk != access_services.group_parent_of(a.entity).pk:
            problems.append("The receiving entity belongs to the same group (R22). / الكيان المستقبِل من المجموعة نفسها.")
    elif not (a.receiving_party_en and a.receiving_party_ar):
        problems.append("A third-party receiver needs a name in English and Arabic (R22). / "
                        "يلزم اسم الجهة المستقبِلة بالإنجليزية والعربية.")
    if not (a.role_title_en and a.role_title_ar):
        problems.append("The role title in English and Arabic is required. / المسمى بالإنجليزية والعربية مطلوب.")
    if a.end_date is not None and a.end_date < a.start_date:
        problems.append("The end date is on or after the start date (R24). / تاريخ النهاية في تاريخ البداية أو بعده.")
    terms = _terms_on(emp, a.start_date)
    first = _approved_versions(emp).order_by("version_no").first()
    end = _contract_end(terms) if terms else None
    if terms is None or first is None or a.start_date < first.start_date or (
            end is not None and (a.end_date is None or a.end_date > end)):
        problems.append("The assignment lies within an approved contract's dates (R22). / "
                        "يقع التكليف ضمن مدة عقد معتمد.")
    if a.kind == Assignment.Kind.SECONDMENT:
        others = Assignment.objects.filter(employee=emp, kind=Assignment.Kind.SECONDMENT).exclude(pk=a.pk).exclude(
            status__in=(ASt.DRAFT, ASt.CANCELLED))
        for o in others:
            if o.start_date <= (a.end_date or date.max) and a.start_date <= (o.end_date or date.max):
                problems.append(f"The employee already has a secondment overlapping these dates ({o.reference}) (R22, "
                                f"OQ-15). / للموظف إعارة أخرى متداخلة مع هذه التواريخ ({o.reference}).")
                break
    return problems


@transaction.atomic
def create_assignment(employee, *, user, **fields):
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.manage_assignment")
    unknown = set(fields) - ASSIGNMENT_FIELDS
    if unknown:
        raise error(f"Unknown assignment fields: {sorted(unknown)}.", f"حقول غير معروفة: {sorted(unknown)}.", code="fields")
    a = Assignment(entity=emp.entity, employee=emp, created_by=user, source=_source(user), **fields)
    problems = _assignment_problems(a)
    if problems:
        raise _problems(problems, "assignment_invalid")
    a.save()
    return a


@transaction.atomic
def submit_assignment(assignment, *, user):
    a = _lock(Assignment, assignment.pk)
    access.require(user, a.entity, "hr.manage_assignment")
    if a.status != ASt.DRAFT:
        raise error("Only drafts are submitted.", "تُقدَّم المسودات فقط.", code="not_draft")
    problems = _assignment_problems(a)
    if problems:
        raise _problems(problems, "assignment_invalid")
    a.status, a.submitted_by = ASt.SUBMITTED, user
    return _save(a)


@transaction.atomic
def approve_assignment(assignment, *, user):
    """R23: ``hr.approve_assignment`` (A); approval assigns ``{entity}-ASG-{year}-{000001}`` (the billing
    ``hr_assignment_ref``). Nothing is created in billing."""
    a = _lock(Assignment, assignment.pk)
    access.require(user, a.entity, "hr.approve_assignment", document=a)
    _deny_own(user, a.entity, "hr.approve_assignment", a.employee)
    if a.status != ASt.SUBMITTED:
        raise error("Only submitted assignments are approved.", "تُعتمد التكليفات المقدمة فقط.", code="not_submitted")
    problems = _assignment_problems(a)
    if problems:
        raise _problems(problems, "assignment_invalid")
    year = a.start_date.year
    a.reference = f"{a.entity.code}-ASG-{year}-{_next_number(a.entity, 'ASG', year):06d}"
    a.status = ASt.ACTIVE if a.start_date <= _today() else ASt.APPROVED
    a.approved_by, a.approved_at = user, timezone.now()
    return _save(a)


def _emit(a, kind, end_date, previous, user, termination=None):
    event = AssignmentEvent.objects.create(entity=a.entity, assignment=a, kind=kind, end_date=end_date,
                                           previous_end_date=previous, actor=user, termination=termination)
    failed = AssignmentEvent.objects.filter(assignment=a, pk__lt=event.pk,
                                            delivery_status=AssignmentEvent.Delivery.FAILED).order_by("pk").first()
    if failed is not None:  # finding 10: say at once that the new event waits behind a failed one
        _alert(a.entity, _recipients(a.entity, "hr.receive_alerts", roles=("accountant",)),
               HrAlert.Kind.BILLING_DELIVERY_BLOCKED,
               f"{a.reference}: the new {kind} event #{event.pk} (end date {end_date}) waits behind failed event "
               f"#{failed.pk}; re-queue or supersede #{failed.pk} in HR.",
               f"{a.reference}: الحدث الجديد #{event.pk} (تاريخ النهاية {end_date}) ينتظر خلف الحدث المتعثر #{failed.pk}؛ "
               f"أعد الحدث #{failed.pk} إلى الانتظار أو استبدله في الموارد البشرية.")
    transaction.on_commit(lambda: _deliver_after_commit(event.pk))
    return event


def _blocked_text(event):
    later = list(AssignmentEvent.objects.filter(assignment_id=event.assignment_id, pk__gt=event.pk).exclude(
        delivery_status__in=AssignmentEvent.DONE).order_by("pk"))
    if not later:
        return "", ""
    items = ", ".join(f"#{e.pk} {e.kind} {e.end_date}" for e in later)
    return (f" Blocked behind it: {items}.", f" الأحداث المتوقفة خلفه: {items}.")


def _event_action(user, event, action, reason_ar, reason_en):
    if not reason_ar:
        raise error("The reason in Arabic is required.", "السبب بالعربية مطلوب.", code="reason_required")
    ev = AssignmentEvent.objects.select_for_update(of=("self",)).select_related("assignment").get(pk=event.pk)
    access.require(user, ev.entity, "hr.end_assignment")
    if ev.delivery_status != AssignmentEvent.Delivery.FAILED:
        raise error("Only a failed event is re-queued or superseded (R25).",
                    "لا يُعاد إلى الانتظار أو يُستبدل إلا الحدث المتعثر.", code="event_status")
    AssignmentEventAction.objects.create(entity=ev.entity, event=ev, action=action, attempts_before=ev.attempts,
                                         last_error_en=ev.last_error_en, last_error_ar=ev.last_error_ar,
                                         reason_ar=reason_ar, reason_en=reason_en, actor=user)
    return ev


@transaction.atomic
def requeue_assignment_event(event, *, user, reason_ar, reason_en=""):
    """Finding 10: put a ``failed`` outbox event back to ``pending`` with its attempts reset, e.g. once billing is
    fixed (``hr.end_assignment``). Logged (``AssignmentEventAction``: who, why, the attempts and last error)."""
    ev = _event_action(user, event, AssignmentEventAction.Action.REQUEUE, reason_ar, reason_en)
    ev.delivery_status, ev.attempts = AssignmentEvent.Delivery.PENDING, 0
    ev.save(update_fields=["delivery_status", "attempts"])
    transaction.on_commit(lambda: _deliver_after_commit(ev.pk))
    return ev


@transaction.atomic
def supersede_assignment_event(event, *, user, reason_ar, reason_en=""):
    """Finding 10: give up a ``failed`` event (its end date was recorded in billing by hand, or a later event
    replaces it); it stops blocking the assignment's later events. Logged like a re-queue."""
    ev = _event_action(user, event, AssignmentEventAction.Action.SUPERSEDE, reason_ar, reason_en)
    ev.delivery_status = AssignmentEvent.Delivery.SUPERSEDED
    ev.save(update_fields=["delivery_status"])
    later = AssignmentEvent.objects.filter(assignment_id=ev.assignment_id, pk__gt=ev.pk,
                                           delivery_status=AssignmentEvent.Delivery.PENDING).values_list("pk", flat=True)
    ids = list(later)
    if ids:
        transaction.on_commit(lambda: deliver_assignment_events(event_ids=ids))
    return ev


def _end_assignment(a, end_date, reason_ar, reason_en, user, termination=None):
    if a.status not in (ASt.APPROVED, ASt.ACTIVE, ASt.ENDED):
        raise error("Only approved, active or ended assignments are ended.", "تُنهى التكليفات المعتمدة أو السارية أو المنتهية فقط.",
                    code="assignment_status")
    if end_date < a.start_date:
        raise error("The end date is on or after the start date (R24).", "تاريخ النهاية في تاريخ البداية أو بعده.",
                    code="assignment_dates")
    if not reason_ar:
        raise error("The end reason in Arabic is required.", "سبب الإنهاء بالعربية مطلوب.", code="reason_required")
    if a.status == ASt.ENDED and a.end_date == end_date:
        return a, None  # R24: the same end date again is a no-op
    kind = AssignmentEvent.Kind.END_CHANGED if a.status == ASt.ENDED else AssignmentEvent.Kind.ENDED
    previous = a.end_date
    a.end_date, a.status, a.end_reason_ar, a.end_reason_en = end_date, ASt.ENDED, reason_ar, reason_en
    a.ended_by, a.ended_at = user, timezone.now()
    _save(a)
    return a, _emit(a, kind, end_date, previous, user, termination)


@transaction.atomic
def end_assignment(assignment, end_date, *, user, reason_ar, reason_en=""):
    """R24: ``hr.end_assignment``, no approval (H4, H5). One ``AssignmentEvent`` in the same transaction; the same
    end date again writes nothing. Returns ``(assignment, event or None)``."""
    a = _lock(Assignment, assignment.pk)
    access.require(user, a.entity, "hr.end_assignment")
    return _end_assignment(a, end_date, reason_ar, reason_en, user)


def _cancel_assignment(a, reason_ar, user):
    if a.status in (ASt.DRAFT, ASt.SUBMITTED):
        a.status, a.end_reason_ar = ASt.CANCELLED, reason_ar
        return _save(a), None
    if a.status != ASt.APPROVED or a.start_date <= _today():
        raise error("Only an assignment that has not started is cancelled; end a started one (R27).",
                    "لا يُلغى إلا تكليف لم يبدأ؛ أما الساري فيُنهى.", code="assignment_status")
    previous = a.end_date
    a.status, a.end_reason_ar, a.ended_by, a.ended_at = ASt.CANCELLED, reason_ar, user, timezone.now()
    a.end_date = a.start_date - timedelta(days=1)
    _save(a)
    return a, _emit(a, AssignmentEvent.Kind.CANCELLED, a.end_date, previous, user)


@transaction.atomic
def cancel_assignment(assignment, *, user, reason_ar):
    """R27 / OQ-16: a not-yet-started approved assignment is cancelled with a ``cancelled`` event whose billing end
    date is the day before its start (nothing billable)."""
    a = _lock(Assignment, assignment.pk)
    access.require(user, a.entity, "hr.end_assignment")
    if not reason_ar:
        raise error("The reason in Arabic is required.", "السبب بالعربية مطلوب.", code="reason_required")
    return _cancel_assignment(a, reason_ar, user)


def billing_view(assignment, *, user):
    """R28: everything billing receives: reference, employee code, role title, dates, receiving entity. Never names,
    ID numbers or salary."""
    access.require(user, assignment.entity, "hr.view")
    return {"hr_assignment_ref": assignment.reference, "employee_ref": assignment.employee.employee_code,
            "role_title_en": assignment.role_title_en, "role_title_ar": assignment.role_title_ar,
            "start_date": assignment.start_date, "end_date": assignment.end_date,
            "receiving_entity": assignment.receiving_entity.code if assignment.receiving_entity else None}


# =====================================================================================================
# Leave (R29 to R35, R56)
# =====================================================================================================

LR = LeaveRequest.Status
LIVE = (LR.SUBMITTED, LR.APPROVED, LR.TAKEN)


def _leave_type(entity, code):
    lt = LeaveType.objects.filter(entity=entity, code=getattr(code, "code", code)).first()
    if lt is None:
        raise error(f"Leave type {code} does not exist in {entity.code}.", f"نوع الإجازة {code} غير موجود في {entity.code}.",
                    code="leave_type")
    return lt


def _balance(employee, leave_type, on=None):
    qs = LeaveLedger.objects.filter(employee=employee, leave_type=leave_type)
    if on is not None:
        # Leave already taken or returned counts whatever its date; accruals (and their reversals) only up to ``on``.
        qs = qs.filter(Q(date__lte=on) | Q(kind__in=(LeaveLedger.Kind.TAKEN, LeaveLedger.Kind.REVERSAL),
                                           source_type="hr.leaverequest"))
    return qs.aggregate(s=Sum("days"))["s"] or ZERO


def leave_balance(employee, leave_type_code, *, user, on=None):
    """R29: Σ ledger days per (employee, leave type)."""
    access.require(user, employee.entity, "hr.view")
    return _balance(employee, _leave_type(employee.entity, leave_type_code), on)


def _counted_dates(use, jurisdiction, req, rule, start, end):
    """The days of a request that count: calendar or working days per the type's parameter; official holidays
    inside annual leave do not count and extend it (Regs Art. 24)."""
    count = rule.get("count", "calendar")
    skip = set()
    if count == "working":
        weekend = _weekend(req.entity)
        skip |= {d for d in _days(start, end) if d.weekday() in weekend}
    if req.leave_type.code == "ANNUAL":
        skip |= _holidays(use, jurisdiction, start, end)
    return [d for d in _days(start, end) if d not in skip]


def _count_days(use, jurisdiction, req, rule, start, end):
    return len(_counted_dates(use, jurisdiction, req, rule, start, end))


def _type_rule(use, jurisdiction, req):
    """The counting parameter of the request's type (``leave.annual`` for ANNUAL, R31)."""
    lt = req.leave_type
    return use.get(jurisdiction, lt.parameter_code, req.start_date) if lt.parameter_code else {}


def _live_sick_days(qs=None):
    """Sick days that still count: their request is approved or taken and the day lies inside the request's
    current dates (nit 18: a shortened sick leave keeps its append-only rows; the days cut off are trimmed here,
    for payroll, probation, the sick-leave year and the Art. 82 warning)."""
    qs = SickLeaveDay.objects.all() if qs is None else qs
    return qs.filter(leave_request__status__in=(LR.APPROVED, LR.TAKEN), date__gte=F("leave_request__start_date"),
                     date__lte=F("leave_request__end_date"))


def _sick_dates(emp, start, end, exclude=None):
    """Days of ``emp``'s approved sick leave between start and end (Regs Art. 26)."""
    qs = LeaveRequest.objects.filter(employee=emp, leave_type__code="SICK", status__in=(LR.APPROVED, LR.TAKEN),
                                     start_date__lte=end, end_date__gte=start)
    if exclude is not None:
        qs = qs.exclude(pk=exclude)
    result = set()
    for s in qs:
        result |= set(_days(max(s.start_date, start), min(s.end_date, end)))
    return result


def _resync_annual(a, use, jurisdiction, user, on):
    """R31, Regs Art. 24 and 26: bring the ledger rows of annual request ``a`` to what it deducts now: its counted
    days (ANNUAL counting rule, holidays excluded) between its current dates, less the days approved sick leave
    suspends; nothing once cancelled. One correcting row (``taken`` or ``reversal``) for the difference, so days
    already returned are never returned twice and a cancelled sick leave deducts its days again."""
    expected = ZERO
    if a.status in (LR.APPROVED, LR.TAKEN):
        rule = _type_rule(use, jurisdiction, a)
        dates = set(_counted_dates(use, jurisdiction, a, rule, a.start_date, a.end_date))
        expected = -Decimal(len(dates - _sick_dates(a.employee, a.start_date, a.end_date)))
    actual = (LeaveLedger.objects.filter(employee=a.employee, leave_type=a.leave_type, source_type="hr.leaverequest",
                                         source_id=a.pk).aggregate(s=Sum("days"))["s"] or ZERO)
    diff = expected - actual
    if diff:
        LeaveLedger.objects.create(entity=a.entity, employee=a.employee, leave_type=a.leave_type, date=on,
                                   kind=LeaveLedger.Kind.REVERSAL if diff > 0 else LeaveLedger.Kind.TAKEN, days=diff,
                                   source_type="hr.leaverequest", source_id=a.pk, created_by=user)


def _resync_annual_around(emp, start, end, use, jurisdiction, user, on):
    for a in LeaveRequest.objects.filter(employee=emp, leave_type__code="ANNUAL", start_date__lte=end,
                                         end_date__gte=start).exclude(status__in=(LR.DRAFT, LR.SUBMITTED, LR.REJECTED)):
        _resync_annual(_lock(LeaveRequest, a.pk), use, jurisdiction, user, max(on, a.start_date))


def _service_years(use, jurisdiction, emp, terms, on):
    year_days = _year_days(use, jurisdiction, terms.calendar_basis, on)
    return _years_between(emp.hire_date, on + timedelta(days=1), terms.calendar_basis, year_days)


def _sick_window_end(cycle_start, basis):
    """Last day of the sick-leave year that starts on ``cycle_start`` (Art. 117; Gregorian or Umm al-Qura, R58)."""
    return _anniversary(cycle_start, 1, basis) - timedelta(days=1)


def _sick_cycles(dates, basis):
    """Assign each sick day (sorted) to its sick-leave year: one year (in the contract's calendar) from the first
    sick day of the cycle (Art. 117). Returns {date: (cycle_start, n_before_in_cycle)}."""
    result, start, n = {}, None, 0
    for d in sorted(dates):
        if start is None or d > _sick_window_end(start, basis):
            start, n = d, 0
        result[d] = (start, n)
        n += 1
    return result


def _sick_plan(use, jurisdiction, req, terms):
    rule = use.get(jurisdiction, "leave.sick", req.start_date)
    _year_days(use, jurisdiction, terms.calendar_basis, req.start_date)  # the calendar basis is configured
    prior = set(_live_sick_days(SickLeaveDay.objects.filter(employee=req.employee))
                .exclude(leave_request_id=req.pk).values_list("date", flat=True))
    new = list(_days(req.start_date, req.end_date))
    cycles = _sick_cycles(prior | set(new), terms.calendar_basis)
    bounds, acc = [], 0
    for i, tier in enumerate(rule["tiers"], start=1):
        acc += int(tier["days"])
        bounds.append((acc, i, _decimal(tier["pay_pct"], "pay_pct")))
    # R64 (Regs Art. 24): an official holiday inside sick leave is paid in full whatever the tier.
    holidays = _holidays(use, jurisdiction, req.start_date, req.end_date)
    holiday_pct = _decimal(rule["holiday_pay_pct"], "holiday_pay_pct") if "holiday_pay_pct" in rule else None
    plan, beyond = [], 0
    for d in new:
        cycle_start, n = cycles[d]
        tier = next((t for t in bounds if n < t[0]), None)
        if tier is None:
            beyond += 1
        else:
            holiday = d in holidays and holiday_pct is not None
            plan.append((d, cycle_start, tier[1], holiday_pct if holiday else tier[2], holiday))
    return plan, beyond, bounds[-1][0]


def _leave_problems(req, use):
    emp, lt = req.employee, req.leave_type
    problems = []
    if emp.employment_basis == Basis.REMUNERATION:
        raise error("Remuneration staff have no leave (R52, H9).", "لا إجازات لأصحاب المكافأة الشهرية.",
                    code="remuneration_no_leave")
    jurisdiction = _jurisdiction(emp, "leave", "الإجازات")
    if emp.status not in (Employee.Status.ACTIVE, Employee.Status.ON_NOTICE):
        problems.append("Leave is recorded for active employees only. / تُسجَّل الإجازات للموظفين النشطين فقط.")
    if req.end_date < req.start_date:
        return problems + ["The end date is on or after the start date. / تاريخ النهاية في تاريخ البداية أو بعده."]
    if req.start_date < emp.hire_date:
        problems.append("Leave starts on or after the hire date. / تبدأ الإجازة في تاريخ التعيين أو بعده.")
    terms = _terms_on(emp, req.start_date)
    if terms is None:
        return problems + [f"No approved contract is in force on {req.start_date}. / لا يوجد عقد معتمد ساري في {req.start_date}."]
    if lt.gender and emp.gender != lt.gender:
        problems.append(f"{lt.name_en} is for {lt.gender} employees only (R33). / {lt.name_ar} مخصصة لـ"
                        f"{'الذكور' if lt.gender == 'male' else 'الإناث'} فقط.")
    # Overlap (R31). Approved sick leave may fall inside annual leave and suspends it (Regs Art. 26).
    overlapping = LeaveRequest.objects.filter(employee=emp, status__in=LIVE, start_date__lte=req.end_date,
                                              end_date__gte=req.start_date).exclude(pk=req.pk).select_related("leave_type")
    for o in overlapping:
        if not (lt.code == "SICK" and o.leave_type.code == "ANNUAL"):
            problems.append(f"This request overlaps another leave request ({o.start_date} to {o.end_date}) (R31). / "
                            f"يتداخل هذا الطلب مع طلب إجازة آخر ({o.start_date} إلى {o.end_date}).")
            break
    if lt.requires_document and not req.document_ref:
        problems.append(f"{lt.name_en} needs a document reference. / تتطلب {lt.name_ar} مرجع مستند.")
    rule = use.get(jurisdiction, lt.parameter_code, req.start_date) if lt.parameter_code else {}
    req.days = Decimal(_count_days(use, jurisdiction, req, rule, req.start_date, req.end_date))
    req.unpaid = lt.paid_rule == LeaveType.PaidRule.UNPAID
    if lt.code == "ANNUAL":
        balance = _balance(emp, lt, req.start_date)
        if req.days > balance:
            problems.append(f"Annual leave of {req.days} days exceeds the balance of {balance} days on {req.start_date} "
                            f"(R31, OQ-5). / الإجازة السنوية {req.days} يومًا تتجاوز الرصيد {balance} يومًا في "
                            f"{req.start_date}.")
    elif lt.code == "SICK":
        _, beyond, total = _sick_plan(use, jurisdiction, req, terms)
        if beyond:
            problems.append(f"{beyond} day(s) go past the {total} sick-leave days of the sick-leave year; record them as "
                            f"unpaid leave (R32, Art. 117). / {beyond} يومًا تتجاوز أيام الإجازة المرضية البالغة {total} "
                            "يومًا في السنة؛ تُسجَّل إجازة بدون أجر.")
    elif lt.code in EVENT_TYPES or lt.code in ("HAJJ", "EXAM"):
        problems += _statutory_leave_problems(req, rule, use, jurisdiction, terms)
    elif lt.code == "NOTICE_ABSENCE":
        problems += _notice_absence_problems(req, rule)
    return problems


def _notice_absence_problems(req, rule):
    """R37(c) (Art. 78): only inside an employer's notice period (from ``notice_given_on`` to the last working day
    of the employee's open or approved ``employer_termination``), and at most ``days_per_week`` days in each 7-day
    block counted from the notice date. The 8-hour form is not built: whole days only."""
    term = (Termination.objects.filter(employee=req.employee, kind=TKind.EMPLOYER_TERMINATION,
                                       status__in=(TS.DRAFT, TS.SUBMITTED, TS.APPROVED), notice_given_on__isnull=False)
            .order_by("-id").first())
    if term is None or req.start_date < term.notice_given_on or req.end_date > term.last_working_day:
        return ["This paid weekly absence is only taken during the employer's notice period (Art. 78). / يؤخذ هذا "
                "الغياب الأسبوعي بأجر خلال مهلة إشعار صاحب العمل فقط (المادة 78)."]
    per_week = int(rule["days_per_week"])
    others = LeaveRequest.objects.filter(employee=req.employee, leave_type=req.leave_type, status__in=LIVE).exclude(
        pk=req.pk)
    taken = [d for o in others for d in _days(o.start_date, o.end_date)]
    blocks = {}
    for d in taken + list(_days(req.start_date, req.end_date)):
        k = (d - term.notice_given_on).days // 7
        blocks[k] = blocks.get(k, 0) + 1
    if any(n > per_week for n in blocks.values()):
        return [f"At most {per_week} paid day(s) of absence in each week of the notice (Art. 78). / بحد أقصى "
                f"{per_week} يوم غياب بأجر في كل أسبوع من مهلة الإشعار (المادة 78)."]
    return []


def notice_absence_days(employee, start, end, *, user):
    """R37(c) / payroll hand-off: the approved Art. 78 paid absence days of ``employee`` between start and end
    (paid in full; payroll pays them as working days and shows them on the payslip)."""
    access.require(user, employee.entity, "hr.view")
    result = set()
    for r in LeaveRequest.objects.filter(employee=employee, leave_type__code="NOTICE_ABSENCE",
                                         status__in=(LR.APPROVED, LR.TAKEN), start_date__lte=end, end_date__gte=start):
        result |= set(_days(max(r.start_date, start), min(r.end_date, end)))
    return sorted(result)


@transaction.atomic
def record_exam_result(leave_request, taken, *, user):
    """R33 (Art. 115): after the exam, record whether it was taken. ``False`` reverses the exam-leave pay: the
    request's days become unpaid, so payroll deducts them (as a retro correction for a month already paid; payroll
    R14, R16). Recorded once."""
    req = _lock(LeaveRequest, leave_request.pk)
    access.require(user, req.entity, "hr.request_leave")
    if not isinstance(taken, bool):
        raise error("State whether the exam was taken (True or False).", "حدّد ما إذا أُدّي الامتحان.", code="exam_result")
    if req.leave_type.code != "EXAM" or req.status not in (LR.APPROVED, LR.TAKEN) or req.exam_taken is not None:
        raise error("The exam result is recorded once, on approved exam leave.",
                    "تُسجَّل نتيجة أداء الامتحان مرة واحدة على إجازة امتحان معتمدة.", code="leave_status")
    req.exam_taken = taken
    if not taken:
        req.unpaid = True
    req.modified_by, req.modified_at = user, timezone.now()
    _save(req, update_fields=["exam_taken", "unpaid", "modified_by", "modified_at"])
    return req


@transaction.atomic
def record_pregnancy(employee, *, user, document_ref, notified_on):
    """R60 (Art. 155): record a pregnancy (or related illness) notified by the employee, as a document reference only
    (R12): from ``notified_on`` an employer-initiated termination or notice gets the Art. 155 warning, also without
    any leave. ``hr.manage_employee``."""
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.manage_employee")
    if emp.gender != Employee.Gender.FEMALE:
        raise error("A pregnancy is recorded for a female employee.", "يُسجَّل الحمل لموظفة.", code="gender")
    if not (document_ref or "").strip():
        raise error("The document reference is required.", "مرجع المستند مطلوب.", code="document_required")
    notice = PregnancyNotice(entity=emp.entity, employee=emp, document_ref=document_ref.strip(), notified_on=notified_on,
                             created_by=user, source=_source(user))
    return _save(notice)


@transaction.atomic
def end_pregnancy_notice(notice, *, user, ended_on):
    """R60: the recorded pregnancy ended (delivery, or otherwise); the Art. 155 warning stops after ``ended_on``
    (maternity leave keeps its own warning)."""
    n = PregnancyNotice.objects.select_for_update().get(pk=notice.pk)
    access.require(user, n.entity, "hr.manage_employee")
    if n.ended_on is not None or ended_on < n.notified_on:
        raise error("The notice ends once, on or after the day it was notified.",
                    "ينتهي الإشعار مرة واحدة في يوم الإبلاغ أو بعده.", code="pregnancy_notice")
    n.ended_on, n.modified_by, n.modified_at = ended_on, user, timezone.now()
    return _save(n)


def _statutory_leave_problems(req, rule, use, jurisdiction, terms):
    """R33 with the compliance corrections: per-event entitlements, gender, timing, once-per-service and
    service-length conditions."""
    lt, emp, problems = req.leave_type, req.employee, []
    code = lt.code
    if code in EVENT_TYPES:
        if req.event_date is None:
            return [f"{lt.name_en} needs the date of the event (birth, death, marriage). / تتطلب {lt.name_ar} تاريخ الواقعة."]
        if code in ("MARRIAGE", "BEREAVEMENT", "BEREAVEMENT_SIBLING", "PATERNITY", "IDDAH") and req.start_date < req.event_date:
            problems.append("This leave is counted from the date of the event (Art. 113, 160). / تُحسب هذه الإجازة من تاريخ الواقعة.")
    if "days" in rule and code not in ("MATERNITY",):
        maximum = int(rule["days"])
        if req.days > maximum:
            problems.append(f"{lt.name_en} is at most {maximum} days per event (R33). / {lt.name_ar} بحد أقصى {maximum} أيام لكل واقعة.")
    if code == "PATERNITY" and req.event_date:
        last = req.event_date + timedelta(days=int(rule["within_days_of_event"]) - 1)
        if req.end_date > last:
            problems.append(f"Leave on the birth of a child is taken within {rule['within_days_of_event']} days of the "
                            f"birth (Art. 113). / تؤخذ إجازة المولود خلال {rule['within_days_of_event']} أيام من الولادة.")
    if code == "MATERNITY" and req.event_date:
        if req.days > int(rule["days"]):
            problems.append(f"Maternity leave is {rule['days']} days (12 weeks) fully paid (Art. 151). / "
                            f"إجازة الوضع {rule['days']} يومًا (12 أسبوعًا) بأجر كامل.")
        if req.start_date < req.event_date - timedelta(days=int(rule["earliest_days_before_due"])):
            problems.append("Maternity leave starts no earlier than 4 weeks before the expected delivery (Art. 151). / "
                            "لا تبدأ إجازة الوضع قبل أربعة أسابيع من التاريخ المتوقع للولادة.")
        if req.end_date < req.event_date + timedelta(days=int(rule["post_birth_mandatory_days"]) - 1):
            problems.append("The six weeks after the delivery are mandatory leave (Art. 151). / "
                            "الأسابيع الستة التالية للوضع إجازة إلزامية.")
    if code == "IDDAH_EXTENSION" and req.event_date and req.end_date > req.event_date:
        problems.append("The unpaid iddah extension ends at the delivery (Art. 160). / ينتهي تمديد العدة بدون أجر بالوضع.")
    if code == "HAJJ":
        if req.days < int(rule["min_days"]) or req.days > int(rule["max_days"]):
            problems.append(f"Hajj leave is {rule['min_days']} to {rule['max_days']} days, including Eid al-Adha "
                            f"(Art. 114). / إجازة الحج من {rule['min_days']} إلى {rule['max_days']} يومًا شاملة عيد الأضحى.")
        if not (req.declarations or {}).get("hajj_not_performed_before"):
            problems.append("Hajj leave needs the employee's declaration that Hajj was not performed before (Art. 114). / "
                            "تتطلب إجازة الحج إقرار الموظف بعدم أداء الحج من قبل.")
        if _service_years(use, jurisdiction, emp, terms, req.start_date) < int(rule["min_service_years"]):
            problems.append(f"Hajj leave needs {rule['min_service_years']} consecutive years of service (R33, Art. 114). / "
                            f"تتطلب إجازة الحج خدمة متصلة لمدة {rule['min_service_years']} سنتين.")
    if lt.once_per_service and LeaveRequest.objects.filter(employee=emp, leave_type=lt, status__in=LIVE).exclude(
            pk=req.pk).exists():
        problems.append(f"{lt.name_en} is granted once during service (R33). / تُمنح {lt.name_ar} مرة واحدة خلال الخدمة.")
    if code == "EXAM":
        decl = req.declarations or {}
        if not decl.get("employer_approved_study"):
            problems.append("Exam leave is for study the employer approved; otherwise use annual leave (Art. 115). / "
                            "إجازة الامتحان لدراسة وافق عليها صاحب العمل؛ وإلا فمن الإجازة السنوية.")
        if decl.get("repeated_year"):
            req.unpaid = True  # Art. 115: unpaid for a repeated year
        if req.pk is None and (req.start_date - _today()).days < int(rule["request_lead_days"]):
            problems.append(f"Exam leave is requested at least {rule['request_lead_days']} days ahead (Art. 115). / "
                            f"تُطلب إجازة الامتحان قبل {rule['request_lead_days']} يومًا على الأقل.")
    return problems


# Finding 7: the only declarations a leave request stores (True/False). PUBLIC ones may be shown to any hr.view
# holder; the others are health data (R60 pregnancy-related illness) and need hr.view_personal_data.
PUBLIC_DECLARATIONS = frozenset({"hajj_not_performed_before", "employer_approved_study", "repeated_year"})
DECLARATIONS = PUBLIC_DECLARATIONS | {"pregnancy_related"}


def _declarations(declarations):
    declarations = dict(declarations or {})
    unknown = set(declarations) - DECLARATIONS
    if unknown or any(not isinstance(v, bool) for v in declarations.values()):
        raise error(f"Declarations are True/False answers to: {', '.join(sorted(DECLARATIONS))} "
                    f"(unknown: {', '.join(sorted(unknown)) or '-'}).",
                    f"الإقرارات إجابات بنعم أو لا على: {', '.join(sorted(DECLARATIONS))}.", code="declarations")
    return declarations


@transaction.atomic
def request_leave(employee, leave_type, *, user, start_date, end_date, event_date=None, document_ref="", reason_ar="",
                  reason_en="", declarations=None):
    """Record a leave request on the employee's behalf (``hr.request_leave``, OQ-4) and submit it."""
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.request_leave")
    declarations = _declarations(declarations)
    req = LeaveRequest(entity=emp.entity, employee=emp, leave_type=_leave_type(emp.entity, leave_type),
                       start_date=start_date, end_date=end_date, event_date=event_date, document_ref=document_ref,
                       reason_ar=reason_ar, reason_en=reason_en, declarations=declarations or {}, created_by=user,
                       source=_source(user))
    problems = _leave_problems(req, ParamUse())
    if problems:
        raise _problems(problems, "leave_invalid")
    req.status, req.submitted_by = LR.SUBMITTED, user
    return _save(req)


@transaction.atomic
def approve_leave(leave_request, *, user):
    """R31: ``hr.approve_leave`` (A): never the requester, never the approver's own leave. Annual leave writes its
    ``taken`` row; sick leave its ``SickLeaveDay`` tiers (R32) and suspends overlapping annual leave (Regs Art. 26).
    The employee row is locked first so two approvals cannot both spend one balance (nit 17). The parameter rows
    used are checked (R7) before anything is written."""
    req = _lock(LeaveRequest, leave_request.pk)
    access.require(user, req.entity, "hr.approve_leave", document=req)
    _lock(Employee, req.employee_id)
    _deny_own(user, req.entity, "hr.approve_leave", req.employee)
    if req.status != LR.SUBMITTED:
        raise error("Only submitted requests are approved.", "تُعتمد الطلبات المقدمة فقط.", code="not_submitted")
    use = ParamUse()
    problems = _leave_problems(req, use)
    if problems:
        raise _problems(problems, "leave_invalid")
    jurisdiction = _jurisdiction(req.employee)
    plan = None
    if req.leave_type.code == "SICK":
        plan, _, _ = _sick_plan(use, jurisdiction, req, _terms_on(req.employee, req.start_date))
    use.check_verified()
    req.status, req.approved_by, req.approved_at = LR.APPROVED, user, timezone.now()
    req.warnings = list(use.warnings)
    _save(req)
    if req.leave_type.code == "ANNUAL":
        _resync_annual(req, use, jurisdiction, user, req.start_date)
    elif plan is not None:
        for d, cycle_start, tier, pct, holiday in plan:
            SickLeaveDay.objects.create(entity=req.entity, employee=req.employee, date=d, leave_request=req,
                                        cycle_start=cycle_start, tier_no=tier, pay_pct=pct, official_holiday=holiday)
        _resync_annual_around(req.employee, req.start_date, req.end_date, use, jurisdiction, user, req.start_date)
    use.mark_used()
    return req


@transaction.atomic
def reject_leave(leave_request, *, user, reason_ar=""):
    req = _lock(LeaveRequest, leave_request.pk)
    access.require(user, req.entity, "hr.approve_leave", document=req)
    if req.status != LR.SUBMITTED:
        raise error("Only submitted requests are rejected.", "تُرفض الطلبات المقدمة فقط.", code="not_submitted")
    req.status = LR.REJECTED
    if reason_ar:
        req.reason_ar = reason_ar
    return _save(req)


def _cancel_leave(req, user, from_day, use):
    """Cancel an approved request from ``from_day`` on (R31, R35). Annual leave gets back exactly the days it still
    deducts after the cut, never days sick leave already returned; a shortened or cancelled sick leave deducts the
    annual days it had suspended again (Regs Art. 26); a shortened sick leave's days after the cut stop counting
    (nit 18)."""
    jurisdiction = _jurisdiction(req.employee)
    first, last = req.start_date, req.end_date
    if from_day <= req.start_date:
        req.status = LR.CANCELLED
    else:
        req.end_date = from_day - timedelta(days=1)
        req.days = Decimal(_count_days(use, jurisdiction, req, _type_rule(use, jurisdiction, req), req.start_date,
                                       req.end_date))
    req.modified_by, req.modified_at = user, timezone.now()
    _save(req)
    if req.leave_type.code == "ANNUAL":
        _resync_annual(req, use, jurisdiction, user, max(from_day, first))
    elif req.leave_type.code == "SICK":
        _resync_annual_around(req.employee, max(from_day, first), last, use, jurisdiction, user, max(from_day, first))
    return req


@transaction.atomic
def cancel_leave(leave_request, *, user):
    """Cancel an approved leave from today on (unused days return). Sick leave is cancelled only before it starts:
    its tier rows then stop counting (they are append-only) and suspended annual days are deducted again."""
    req = _lock(LeaveRequest, leave_request.pk)
    access.require(user, req.entity, "hr.request_leave")
    _lock(Employee, req.employee_id)
    if req.status != LR.APPROVED:
        raise error("Only approved leave is cancelled.", "تُلغى الإجازات المعتمدة فقط.", code="leave_status")
    if req.leave_type.code == "SICK" and req.start_date <= _today():
        raise error("Started sick leave is not cancelled; record an adjustment instead.",
                    "لا تُلغى الإجازة المرضية بعد بدئها؛ سجّل تسوية بدلًا من ذلك.", code="leave_status")
    use = ParamUse()
    _jurisdiction(req.employee)
    _type_rule(use, _jurisdiction(req.employee), req)
    use.check_verified()
    req = _cancel_leave(req, user, max(_today(), req.start_date), use)
    use.mark_used()
    return req


@transaction.atomic
def record_opening_leave_balance(employee, leave_type, days, on, *, user, reason_ar="رصيد افتتاحي من ترحيل البيانات",
                                 reason_en="Opening balance from the data migration"):
    """An opening balance from a migration (Daftra or another system, hr §6) as of ``on``. It is an **approved
    adjustment**, not a direct ledger row (R29, review finding 1): this records a draft ``LeaveAdjustment`` of kind
    ``opening`` with ``source=import``; ``approve_leave_adjustment`` (``hr.adjust_leave_balance``, A, someone other
    than its creator) writes the ``opening`` ledger row, capped at what the service years up to ``on`` could have
    earned (R30, R56). Only one opening per employee and leave type, before any other row of that type."""
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.manage_employee")
    lt = _leave_type(emp.entity, leave_type)
    if emp.employment_basis == Basis.REMUNERATION:
        raise error("Remuneration staff have no leave (R52).", "لا إجازات لأصحاب المكافأة الشهرية.", code="remuneration_no_leave")
    if not lt.deducts_balance:
        raise error(f"{lt.code} has no balance: opening balances are for annual leave.",
                    f"لا رصيد لنوع الإجازة {lt.code}: الرصيد الافتتاحي للإجازة السنوية.", code="opening_type")
    if on < emp.hire_date:
        raise error("The opening balance date is on or after the hire date.", "تاريخ الرصيد الافتتاحي في تاريخ التعيين أو بعده.",
                    code="opening_date")
    if not reason_ar:
        raise error("The reason in Arabic is required.", "السبب بالعربية مطلوب.", code="reason_required")
    pending = LeaveAdjustment.objects.filter(employee=emp, leave_type=lt, kind=LeaveAdjustment.Kind.OPENING).exclude(
        status=LeaveAdjustment.Status.REJECTED)
    if pending.exists() or LeaveLedger.objects.filter(employee=emp, leave_type=lt).exists():
        raise error("Opening balances are loaded once, before any other leave row of that type (R29).",
                    "يُحمَّل الرصيد الافتتاحي مرة واحدة قبل أي قيد إجازة آخر من النوع نفسه.", code="opening_once")
    return LeaveAdjustment.objects.create(entity=emp.entity, employee=emp, leave_type=lt, date=on,
                                          days=_decimal(days, "days"), kind=LeaveAdjustment.Kind.OPENING,
                                          reason_ar=reason_ar, reason_en=reason_en, created_by=user, source=Source.IMPORT)


def _service_year_bounds(emp, terms, use, jurisdiction, on):
    year_days = _year_days(use, jurisdiction, terms.calendar_basis, on)
    n = 0
    while _anniversary(emp.hire_date, n + 1, terms.calendar_basis, year_days) <= on:
        n += 1
    start = _anniversary(emp.hire_date, n, terms.calendar_basis, year_days)
    return start, _anniversary(emp.hire_date, n + 1, terms.calendar_basis, year_days) - timedelta(days=1)


@transaction.atomic
def request_leave_adjustment(employee, leave_type, days, on, *, user, reason_ar, reason_en=""):
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.request_leave")
    if emp.employment_basis == Basis.REMUNERATION:
        raise error("Remuneration staff have no leave (R52).", "لا إجازات لأصحاب المكافأة الشهرية.", code="remuneration_no_leave")
    if not reason_ar:
        raise error("The reason in Arabic is required.", "السبب بالعربية مطلوب.", code="reason_required")
    return LeaveAdjustment.objects.create(entity=emp.entity, employee=emp, leave_type=_leave_type(emp.entity, leave_type),
                                          date=on, days=_decimal(days, "days"), reason_ar=reason_ar,
                                          reason_en=reason_en, created_by=user, source=_source(user))


@transaction.atomic
def reject_leave_adjustment(adjustment, *, user, reason_ar):
    """R29: the approver refuses a draft adjustment or opening balance (Arabic reason required); nothing is written
    to the ledger."""
    adj = _lock(LeaveAdjustment, adjustment.pk)
    access.require(user, adj.entity, "hr.adjust_leave_balance", document=adj)
    if adj.status != LeaveAdjustment.Status.DRAFT or not reason_ar:
        raise error("A draft adjustment is rejected with an Arabic reason.", "تُرفض التسوية المسودة مع ذكر السبب بالعربية.",
                    code="reject")
    adj.status = LeaveAdjustment.Status.REJECTED
    adj.reason_ar = f"{adj.reason_ar} | {reason_ar}"
    adj.modified_by, adj.modified_at = user, timezone.now()
    return _save(adj)


def _opening_cap(emp, on, use, jurisdiction):
    """R30, R56: the most annual leave the service from the hire date to ``on`` could have earned (what the monthly
    accrual would have written, suspended unpaid days excluded): the cap of an opening balance (finding 1)."""
    if not _approved_versions(emp).exists():
        raise error("Approve the employee's contract first: the opening balance is capped at the entitlement of the "
                    "service years under it (R30, R56).",
                    "اعتمد عقد الموظف أولًا: يُحدّ الرصيد الافتتاحي باستحقاق سنوات الخدمة وفق العقد.", code="no_terms")
    total = Fraction(0)
    for month_start, month_end in _months(emp.hire_date, on):
        total += _month_accrual_fraction(emp, month_start, month_end, use, jurisdiction, until=on, opening=False)
    return _frac_round(total)


@transaction.atomic
def approve_leave_adjustment(adjustment, *, user):
    """R29: the approver (``hr.adjust_leave_balance``, A) approves before the ledger row exists. R56: no adjustment
    gives more than 30 days of annual leave for one service year. An opening balance (kind ``opening``, from an
    import) is capped at the entitlement of the service years up to its date and written as the ``opening`` row."""
    adj = _lock(LeaveAdjustment, adjustment.pk)
    access.require(user, adj.entity, "hr.adjust_leave_balance", document=adj)
    emp = _lock(Employee, adj.employee_id)
    _deny_own(user, adj.entity, "hr.adjust_leave_balance", emp)
    if adj.status != LeaveAdjustment.Status.DRAFT:
        raise error("Only draft adjustments are approved.", "تُعتمد التسويات المسودة فقط.", code="not_draft")
    use = ParamUse()
    opening = adj.kind == LeaveAdjustment.Kind.OPENING
    if opening:
        jurisdiction = _jurisdiction(emp, "leave", "الإجازات")
        if LeaveLedger.objects.filter(employee=emp, leave_type=adj.leave_type).filter(
                Q(kind=LeaveLedger.Kind.OPENING) | Q(date__lte=adj.date)).exists():
            raise error("Opening balances are loaded once, before any other leave row of that type (R29).",
                        "يُحمَّل الرصيد الافتتاحي مرة واحدة قبل أي قيد إجازة آخر من النوع نفسه.", code="opening_once")
        if adj.days < 0:
            raise error("An opening balance is not negative.", "لا يكون الرصيد الافتتاحي سالبًا.", code="opening_cap")
        cap = _opening_cap(emp, adj.date, use, jurisdiction)
        if adj.days > cap:
            raise error(f"The opening balance of {adj.days} days is more than the {cap} days the service up to "
                        f"{adj.date} could have earned (R30, R56).",
                        f"الرصيد الافتتاحي {adj.days} يومًا يزيد على {cap} يومًا يمكن أن تكسبها الخدمة حتى {adj.date}.",
                        code="opening_cap")
    elif adj.leave_type.code == "ANNUAL" and adj.days > 0 and emp.employment_basis == Basis.KSA_EMPLOYEE:
        terms = _terms_on(emp, adj.date)
        if terms is None:
            raise error("No approved contract on that date.", "لا يوجد عقد معتمد في ذلك التاريخ.", code="no_terms")
        start, end = _service_year_bounds(emp, terms, use, "SA", adj.date)
        granted = LeaveLedger.objects.filter(
            employee=emp, leave_type=adj.leave_type, date__range=(start, end),
            kind__in=(LeaveLedger.Kind.ACCRUAL, LeaveLedger.Kind.ADJUSTMENT), days__gt=0,
        ).aggregate(s=Sum("days"))["s"] or ZERO
        if granted + adj.days > ANNUAL_LEAVE_CEILING:
            raise error(f"This adjustment would give {granted + adj.days} days of annual leave for the service year "
                        f"{start} to {end}; the ceiling is {ANNUAL_LEAVE_CEILING} days (R56, H14).",
                        f"تمنح هذه التسوية {granted + adj.days} يومًا من الإجازة السنوية لسنة الخدمة من {start} إلى "
                        f"{end}؛ والحد الأقصى {ANNUAL_LEAVE_CEILING} يومًا.", code="annual_ceiling")
    use.check_verified()
    adj.status, adj.approved_by, adj.approved_at = LeaveAdjustment.Status.APPROVED, user, timezone.now()
    _save(adj)
    LeaveLedger.objects.create(entity=adj.entity, employee=emp, leave_type=adj.leave_type, date=adj.date,
                               kind=LeaveLedger.Kind.OPENING if opening else LeaveLedger.Kind.ADJUSTMENT,
                               days=adj.days, source_type="hr.leaveadjustment",
                               source_id=adj.pk, created_by=user)
    use.mark_used()
    return adj


def _unpaid_days_of(r, start, end):
    """The unpaid days of one approved request between start and end (from ``unpaid_from`` when only its tail is
    unpaid, e.g. the maternity shortfall after a late birth)."""
    if r.unpaid or r.leave_type.paid_rule == LeaveType.PaidRule.UNPAID:
        return set(_days(max(r.start_date, start), min(r.end_date, end)))
    if r.unpaid_from is not None:
        return set(_days(max(r.unpaid_from, start), min(r.end_date, end)))
    return set()


def _unpaid_days(emp, start, end):
    """Approved unpaid leave days of ``emp`` between start and end (R34)."""
    result = set()
    for r in LeaveRequest.objects.filter(employee=emp, status__in=(LR.APPROVED, LR.TAKEN), start_date__lte=end,
                                         end_date__gte=start).select_related("leave_type"):
        result |= _unpaid_days_of(r, start, end)
    return result


def sick_pay_days(employee, start, end, *, user):
    """R32, R64 (payroll R17): each approved sick day between start and end with its pay %, tier and whether it is
    an official holiday (paid in full whatever the tier)."""
    access.require(user, employee.entity, "hr.view")
    return [(d.date, d.pay_pct, d.tier_no, d.official_holiday) for d in _live_sick_days(SickLeaveDay.objects.filter(
        employee=employee, date__range=(start, end))).order_by("date")]


@transaction.atomic
def record_birth(leave_request, birth_date, *, user):
    """R33 / AT-35 (Art. 151): record the actual birth on approved maternity leave. When fewer than the mandatory
    days after birth remain (a late birth), the leave is extended to complete them and the extension is unpaid."""
    req = _lock(LeaveRequest, leave_request.pk)
    access.require(user, req.entity, "hr.request_leave")
    if req.leave_type.code != "MATERNITY" or req.status != LR.APPROVED or req.actual_event_date is not None:
        raise error("The birth is recorded once, on approved maternity leave.",
                    "تُسجَّل الولادة مرة واحدة على إجازة وضع معتمدة.", code="leave_status")
    if birth_date < req.start_date or birth_date > req.end_date:
        raise error("The birth falls outside this maternity leave; record the leave from the birth instead.",
                    "الولادة خارج إجازة الوضع هذه؛ سجّل الإجازة اعتبارًا من الولادة.", code="birth_outside_leave")
    use = ParamUse()
    jurisdiction = _jurisdiction(req.employee, "leave", "الإجازات")
    rule = use.get(jurisdiction, "leave.maternity", req.start_date)
    shortfall = int(rule["post_birth_mandatory_days"]) - ((req.end_date - birth_date).days + 1)
    req.actual_event_date = birth_date
    if shortfall > 0:
        first, last = req.end_date + timedelta(days=1), req.end_date + timedelta(days=shortfall)
        clash = LeaveRequest.objects.filter(employee=req.employee, status__in=LIVE, start_date__lte=last,
                                            end_date__gte=first).exclude(pk=req.pk).first()
        if clash is not None:
            raise error(f"The mandatory weeks after birth run to {last}; cancel the leave from {clash.start_date} first "
                        "(Art. 151).", f"تمتد الأسابيع الإلزامية بعد الولادة حتى {last}؛ ألغِ الإجازة التي تبدأ في "
                        f"{clash.start_date} أولًا.", code="leave_overlap")
        req.unpaid_from, req.end_date = first, last
        req.days += shortfall
    use.check_verified()
    req.modified_by, req.modified_at = user, timezone.now()
    _save(req)
    use.mark_used()
    return req


def probation_end_date(contract, *, user):
    """R17 / AT-33 (Regs Art. 19): the last probation day, skipping Eid al-Fitr, Eid al-Adha, National Day, Founding
    Day and approved sick days; None without probation."""
    access.require(user, contract.entity, "hr.view")
    return _probation_end(ParamUse(), contract, _jurisdiction(contract.employee))


def unpaid_leave_days(employee, start, end, *, user):
    """Unpaid days for payroll (R34, payroll R16)."""
    access.require(user, employee.entity, "hr.view")
    return len(_unpaid_days(employee, start, end))


def _suspension_rule(use, jurisdiction, on):
    """R62 parameter ``leave.unpaid.suspension_after_days``: (threshold days, aggregation ``work_year`` (Art. 2,
    default) or ``per_leave`` (Art. 116); OQ-37)."""
    value = use.get(jurisdiction, "leave.unpaid.suspension_after_days", on)
    return int(value["days"]), value.get("aggregation", "work_year")


def _suspended_days(use, jurisdiction, emp, terms_basis, start, end):
    """R62: unpaid days within start..end beyond the threshold, counted per work year from the hire date (Art. 2)
    or per unpaid leave (Art. 116), as the parameter says (OQ-37). These days give no accrual and no EOSB service."""
    threshold, aggregation = _suspension_rule(use, jurisdiction, start)
    result = set()
    if aggregation == "per_leave":
        for r in LeaveRequest.objects.filter(employee=emp, status__in=(LR.APPROVED, LR.TAKEN), start_date__lte=end,
                                             end_date__gte=start).select_related("leave_type"):
            days = sorted(_unpaid_days_of(r, r.start_date, r.end_date))
            result |= {d for d in days[threshold:] if start <= d <= end}
        return result
    n = 0
    while True:
        ys = _anniversary(emp.hire_date, n, terms_basis)
        ye = _anniversary(emp.hire_date, n + 1, terms_basis) - timedelta(days=1)
        if ys > end:
            break
        if ye >= start:
            unpaid = sorted(_unpaid_days(emp, ys, ye))
            result |= {d for d in unpaid[threshold:] if start <= d <= end}
        n += 1
    return result


def _service_anniversary(use, jurisdiction, emp, years, basis):
    """The ``years`` anniversary of the hire date moved later by the suspended days before it (R30, R62:
    "completed five consecutive years" of service)."""
    plain = _anniversary(emp.hire_date, years, basis)
    shifted = plain
    for _ in range(10):
        moved = plain + timedelta(days=len(_suspended_days(use, jurisdiction, emp, basis, emp.hire_date,
                                                           shifted - timedelta(days=1))))
        if moved == shifted:
            break
        shifted = moved
    return shifted


def _months(start, end):
    """(first day, last day) of every calendar month from ``start``'s month to ``end``'s month."""
    year, month = start.year, start.month
    while date(year, month, 1) <= end:
        yield date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _period(day):
    return f"{day.year:04d}-{day.month:02d}"


def _end_of_service(emp):
    term = Termination.objects.filter(employee=emp, status=Termination.Status.APPROVED).order_by("-id").first()
    return term.last_working_day if term is not None else None


def _opening_row(emp, lt):
    return LeaveLedger.objects.filter(employee=emp, leave_type=lt, kind=LeaveLedger.Kind.OPENING).order_by("-date").first()


def _month_accrual_fraction(emp, month_start, month_end, use, jurisdiction, *, until=None, opening=True):
    """R30, R56 with the compliance correction, as an exact fraction: per day, entitlement = min(30, max(contract
    days, statutory days)), the 30-day statutory rate from the day after the 5-year anniversary; accrual = Σ
    entitlement / 12 / days in the month; unpaid days beyond 20 in a work year accrue nothing (R62). Days after the
    last working day of an approved termination, after ``until``, or on or before an opening balance's date (the
    opening covers them) do not accrue. None when no day of the month is in service."""
    versions = list(_approved_versions(emp).order_by("effective_date", "version_no"))
    if not versions:
        return None
    first = min(versions, key=lambda v: v.version_no)
    start = max(month_start, emp.hire_date, first.start_date)
    if opening:
        lt = LeaveType.objects.filter(entity=emp.entity, code="ANNUAL").first()
        row = _opening_row(emp, lt) if lt else None
        if row is not None:
            start = max(start, row.date + timedelta(days=1))
    end = min(month_end, _end_of_service(emp) or month_end, until or month_end)
    if start > end:
        return None
    terms_end = [v for v in versions if v.effective_date <= end]
    basis = (terms_end[-1] if terms_end else first).calendar_basis
    spans = use.get_span(jurisdiction, "leave.annual", start, end)
    suspended = _suspended_days(use, jurisdiction, emp, basis, start, end)
    _year_days(use, jurisdiction, basis, month_start)  # the calendar basis is configured
    anniversaries = {}
    total = 0
    for span_start, span_end, value in spans:
        tiers = [(int(Decimal(t["from_years"])), int(Decimal(t["days"]))) for t in value["tiers"]]
        for years, _ in tiers:
            if years not in anniversaries:
                anniversaries[years] = _service_anniversary(use, jurisdiction, emp, years, basis)
        for d in _days(span_start, span_end):
            if d in suspended:
                continue
            terms = None
            for v in versions:  # R20: the latest effective version on the day (ties: higher version)
                if v.effective_date <= d:
                    terms = v
            if terms is None:
                continue
            statutory = max(days for years, days in tiers if years == 0 or d > anniversaries[years])
            total += min(int(ANNUAL_LEAVE_CEILING), max(terms.annual_leave_days, statutory))
    days_in_month = (month_end - month_start).days + 1
    return Fraction(total, 12 * days_in_month)


def _month_accrual(emp, month_start, month_end, use, jurisdiction):
    value = _month_accrual_fraction(emp, month_start, month_end, use, jurisdiction)
    return None if value is None else _frac_round(value)


def _live_accruals(emp, lt, period=None):
    """Accrual rows that still count: not reversed by a termination withdrawal (R41)."""
    rows = LeaveLedger.objects.filter(employee=emp, leave_type=lt, kind=LeaveLedger.Kind.ACCRUAL)
    if period is not None:
        rows = rows.filter(period=period)
    reversed_ids = LeaveLedger.objects.filter(employee=emp, leave_type=lt, kind=LeaveLedger.Kind.REVERSAL,
                                              source_type="hr.leaveledger").values("source_id")
    return rows.exclude(pk__in=reversed_ids)


def _accrue_to_last_day(term, use, jurisdiction, user):
    """Finding 2 (R35, Art. 111; payroll R34): before the final-settlement balance is taken, the annual leave of
    every month up to the last working day is accrued: the months the monthly job has not written yet and the
    partial last month up to ``last_working_day`` (a row dated that day). A month the job already accrued in full
    past the last working day is corrected down to what was due. The rows carry the termination as source, so a
    withdrawal can reverse them."""
    emp, lwd = term.employee, term.last_working_day
    lt = LeaveType.objects.filter(entity=emp.entity, code="ANNUAL").first()
    if lt is None:
        return
    live = _live_accruals(emp, lt)
    periods = set(live.values_list("period", flat=True))
    start_day = emp.hire_date
    if periods:
        last = max(periods)
        year, month = (int(x) for x in last.split("-"))
        start_day = max(start_day, date(year, month, calendar.monthrange(year, month)[1]) + timedelta(days=1))
    opening = _opening_row(emp, lt)
    if opening is not None:
        start_day = max(start_day, opening.date + timedelta(days=1))

    def write(period, month_end, days):
        LeaveLedger.objects.create(entity=emp.entity, employee=emp, leave_type=lt, date=min(month_end, lwd),
                                   kind=LeaveLedger.Kind.ACCRUAL, days=days, period=period,
                                   source_type="hr.termination", source_id=term.pk, created_by=user)

    for month_start, month_end in _months(start_day, lwd):
        period = _period(month_start)
        if period in periods:
            continue
        days = _month_accrual(emp, month_start, month_end, use, jurisdiction)
        if days:
            write(period, month_end, days)
            periods.add(period)
    for period in sorted(p for p in periods if p >= _period(lwd)):
        year, month = (int(x) for x in period.split("-"))
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        due = _month_accrual(emp, month_start, month_end, use, jurisdiction) or ZERO
        actual = _live_accruals(emp, lt, period).aggregate(s=Sum("days"))["s"] or ZERO
        if actual != due:
            write(period, month_end, due - actual)


def _reverse_termination_accruals(term, user):
    """R41: a withdrawn termination's accrual rows for its last month and later go back (the job accrues those
    months again at month end); the full months before it stay, as the job would have written them."""
    rows = LeaveLedger.objects.filter(employee=term.employee, kind=LeaveLedger.Kind.ACCRUAL, source_type="hr.termination",
                                      source_id=term.pk, period__gte=_period(term.last_working_day))
    for row in rows:
        LeaveLedger.objects.create(entity=row.entity, employee=row.employee, leave_type=row.leave_type, date=row.date,
                                   kind=LeaveLedger.Kind.REVERSAL, days=-row.days, period=row.period,
                                   source_type="hr.leaveledger", source_id=row.pk, created_by=user)


# =====================================================================================================
# Termination and end-of-service (R27, R35 to R41, R52)
# =====================================================================================================

TS = Termination.Status
# Art. 79 bis: a postponement is the approver's decision (postpone_resignation), never part of the draft.
TERMINATION_FIELDS = frozenset({
    "kind", "last_working_day", "reason_ar", "reason_en", "notice_given_on", "submitted_on",
    "written_consent_ref", "article_ref", "article_paragraph", "hearing_ref",
    "absence_warning_ref", "marriage_contract_date", "delivery_date", "art77_compensation", "compensation_ref",
    "agreed_fraction",
})
# R64 (Art. 88): kinds where the worker ends the contract (14-day settlement deadline, payroll R34).
WORKER_INITIATED = frozenset({TKind.RESIGNATION, TKind.EMPLOYEE_NOTICE_ART75, TKind.ART81_EMPLOYEE_LEAVES,
                              TKind.FEMALE_MARRIAGE_CHILDBIRTH})
# R59, R60: employer-initiated terminations that get the Art. 82 / Art. 155 protection warnings.
EMPLOYER_INITIATED = frozenset({TKind.EMPLOYER_TERMINATION, TKind.ART80_DISMISSAL})
# R60: leave that records a pregnancy or maternity (iddah extension = declared pregnancy, Art. 160).
MATERNITY_FAMILY = ("MATERNITY", "MATERNITY_UNPAID", "MATERNITY_CHILD", "MATERNITY_CHILD_UNPAID", "IDDAH_EXTENSION")
ART82 = ("Art. 82", "The employee has sick leave in the current sick-leave year that is not used up: no dismissal for "
         "illness before the sick-leave periods are used up (Art. 82). The approver must acknowledge this warning.",
         "لدى الموظف إجازة مرضية في سنة الإجازة المرضية الحالية لم تُستنفد: لا يجوز الفصل بسبب المرض قبل استنفاد مدد "
         "الإجازة المرضية (المادة 82). يجب أن يقر المعتمد بهذا التنبيه.")
ART155 = ("Art. 155", "The employee is pregnant, on maternity leave or ill from pregnancy or delivery: no dismissal or "
          "notice during that time (Art. 155). The approver must acknowledge this warning.",
          "الموظفة حامل أو في إجازة وضع أو مريضة بسبب الحمل أو الوضع: لا يجوز فصلها أو إشعارها بالفصل خلال ذلك "
          "(المادة 155). يجب أن يقر المعتمد بهذا التنبيه.")
OQ33 = ("OQ-33: the Art. 85 resignation bands are applied to a worker's Art. 75 notice pending legal sign-off.",
        "المسألة 33: طُبقت شرائح الاستقالة في المادة 85 على إشعار العامل وفق المادة 75 إلى حين الرأي القانوني.")


def _actual_wage(contract):
    """Monthly actual wage of a version for HR checks: the components counting for EOSB (Art. 2, 84; 3.3)."""
    return sum((c.monthly_amount for c in contract.components.select_related("component") if c.component.counts_for_eosb),
               ZERO)


def _protection_warnings(term, use, jurisdiction):
    """R59 (Art. 82) and R60 (Art. 155) for an employer-initiated termination: [(article, en, ar)]."""
    if term.kind not in EMPLOYER_INITIATED:
        return []
    emp, lwd = term.employee, term.last_working_day
    start = min(_today(), term.notice_given_on or _today(), lwd)
    found = []
    sick = use.get(jurisdiction, "leave.sick", start)
    total = sum(int(t["days"]) for t in sick["tiers"])
    days = list(_live_sick_days(SickLeaveDay.objects.filter(employee=emp, date__lte=lwd)).order_by("date")
                .values_list("cycle_start", flat=True))
    if days:
        cycle = days[-1]
        if _sick_window_end(cycle, term.contract.calendar_basis) >= start and days.count(cycle) < total:
            found.append(ART82)
    limit = int(use.get(jurisdiction, "termination.protection", start)["pregnancy_illness_days"])
    maternity = LeaveRequest.objects.filter(employee=emp, status__in=LIVE, leave_type__code__in=MATERNITY_FAMILY,
                                            end_date__gte=start).exists()
    illness = [r for r in LeaveRequest.objects.filter(employee=emp, status__in=(LR.APPROVED, LR.TAKEN),
                                                      leave_type__code="SICK").order_by("start_date")
               if (r.declarations or {}).get("pregnancy_related")]
    ill_days = sorted({d for r in illness for d in _days(r.start_date, r.end_date)})
    protected = set(ill_days[:limit])  # the related illness is protected up to the parameter's days
    # R60: a pregnancy recorded by document reference without any leave (PregnancyNotice) protects too.
    noticed = PregnancyNotice.objects.filter(employee=emp, notified_on__lte=lwd).filter(
        Q(ended_on__isnull=True) | Q(ended_on__gte=start)).exists()
    if maternity or noticed or any(start <= d <= lwd for d in protected):
        found.append(ART155)
    return found


def _age_on(birth, day):
    """Completed Gregorian years of age on ``day``."""
    return day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))


def _retirement_warnings(term, use):
    """R37 (GOSI check): a ``retirement`` before the GOSI legal age of the employee's cohort is a warning (en/ar)
    the approver acknowledges, never a refusal (OQ-40). Non-Saudis and an empty cohort: a warning that no GOSI age
    check was done. Returns ``(warnings, articles needing acknowledgement)``."""
    emp, lwd = term.employee, term.last_working_day
    if not emp.is_saudi or not emp.gosi_cohort:
        why_en = "the employee is not Saudi" if not emp.is_saudi else "the GOSI cohort is not recorded"
        why_ar = "الموظف غير سعودي" if not emp.is_saudi else "فئة التأمينات غير مسجلة"
        return [f"No GOSI legal-age check was done for this retirement: {why_en} (R37, OQ-40). / لم يُجرَ التحقق من "
                f"السن النظامية للتأمينات لهذا التقاعد: {why_ar}."], []
    row = param("SA", "gosi.legal_age", lwd, required=False)
    entry = (row.value if row else {}).get(emp.gosi_cohort) if row else None
    if not entry or not entry.get("years"):
        return [f"No GOSI legal age is configured for cohort {emp.gosi_cohort} on {lwd}: no age check was done (R37). / "
                f"لا توجد سن نظامية مهيأة للفئة {emp.gosi_cohort} في {lwd}: لم يُجرَ التحقق من السن."], []
    value = use.get("SA", "gosi.legal_age", lwd)[emp.gosi_cohort]
    legal, article = int(Decimal(value["years"])), value["article"]
    age = _age_on(emp.date_of_birth, lwd)
    warnings = []
    if emp.gosi_cohort == Employee.GosiCohort.LEGACY_M33:
        warnings.append("Legacy contributor: checked against the age for contributors outside the Clause Fifth group; "
                        "the Clause Fifth schedule is not loaded, check it on the GOSI record if it applies (OQ-40). / "
                        "مشترك وفق النظام السابق: قورن بالسن لغير فئة البند خامسًا؛ جدول البند خامسًا غير محمّل، فتحقق "
                        "منه في سجل التأمينات إن انطبق.")
    if row.status != StatutoryParameter.Status.VERIFIED:
        warnings.append(f"The GOSI legal-age row ({row.source_reference[:60]}) is not verified yet (R7). / صف السن "
                        "النظامية للتأمينات غير متحقق منه بعد.")
    if age >= legal:
        return warnings, []
    warnings.append(f"Retirement at age {age}, before the GOSI legal age of {legal} for cohort {emp.gosi_cohort} "
                    f"({article}); whether Labour Law Art. 74(4) uses that age is open (OQ-40). The approver must "
                    f"acknowledge this warning. / تقاعد في سن {age} قبل السن النظامية للتأمينات {legal} للفئة "
                    f"{emp.gosi_cohort} ({article})؛ ومسألة اعتماد المادة 74(4) من نظام العمل لهذه السن مفتوحة. يجب أن "
                    "يقر المعتمد بهذا التنبيه.")
    return warnings, [article]


def _resignation_dates(term, use, jurisdiction, accepted_on=None):
    """Art. 79 bis: deemed accepted on day 30; postponement ≤ 60 days with a written reason; the last working day
    is derived from the acceptance, day 30 or the end of the postponement, never typed."""
    rule = use.get(jurisdiction, "termination.resignation", term.submitted_on)
    deemed = term.submitted_on + timedelta(days=int(rule["deemed_acceptance_days"]))
    problems = []
    if term.postponed_until:
        if not term.postponement_reason_ar:
            problems.append("A postponement needs a written reason in Arabic (Art. 79 bis). / يلزم سبب مكتوب بالعربية للتأجيل.")
        if term.postponed_until > deemed + timedelta(days=int(rule["max_postponement_days"])):
            problems.append(f"Acceptance is postponed by at most {rule['max_postponement_days']} days (Art. 79 bis). / "
                            f"لا يتجاوز تأجيل القبول {rule['max_postponement_days']} يومًا.")
        return term.postponed_until, problems
    if accepted_on is not None and accepted_on < deemed:
        return accepted_on, problems
    return deemed, problems


def _termination_problems(term, use):
    emp, problems, warnings = term.employee, [], []
    basis = emp.employment_basis
    term.worker_initiated = term.kind in WORKER_INITIATED
    term.protection_articles = []
    if not term.reason_ar:
        problems.append("The reason in Arabic is required (R36). / السبب بالعربية مطلوب.")
    if basis == Basis.REMUNERATION:
        if term.kind != TKind.END_OF_REMUNERATION:
            problems.append("Remuneration staff end only by end of remuneration (R52). / تنتهي المكافأة الشهرية بانتهائها فقط.")
        return problems, warnings
    if term.kind == TKind.END_OF_REMUNERATION:
        problems.append("End of remuneration is only for remuneration staff (R52). / انتهاء المكافأة الشهرية لأصحابها فقط.")
    if term.kind not in TKind.values:
        return problems + [f"Unknown termination kind {term.kind!r}. / نوع إنهاء غير معروف."], warnings
    jurisdiction = _jurisdiction(emp, "termination", "إنهاء الخدمة")
    contract = term.contract
    lwd = term.last_working_day
    fixed = contract.contract_type == EmploymentContract.ContractType.FIXED
    if term.kind == TKind.RESIGNATION and not fixed:
        problems.append("A resignation ends a fixed-term contract; an open-ended contract is ended by the employee "
                        "with notice (Art. 75). / الاستقالة لإنهاء العقد محدد المدة؛ أما غير محدد المدة فيُنهيه الموظف "
                        "بإشعار وفق المادة 75.")
    if term.kind == TKind.EMPLOYEE_NOTICE_ART75 and fixed:
        problems.append("Art. 75 notice ends an open-ended contract; use a resignation (Art. 79 bis). / "
                        "الإشعار وفق المادة 75 لإنهاء العقد غير محدد المدة؛ استخدم الاستقالة.")
    if term.kind == TKind.EMPLOYEE_NOTICE_ART75:
        warnings.append(f"{OQ33[0]} / {OQ33[1]}")
    if term.kind == TKind.MUTUAL_AGREEMENT and not term.written_consent_ref:
        problems.append("Mutual agreement needs a reference to the employee's written consent (Art. 74(1)). / "
                        "يلزم لاتفاق الطرفين مرجع الموافقة الكتابية للموظف.")
    if term.agreed_fraction is not None and (term.kind != TKind.MUTUAL_AGREEMENT or term.agreed_fraction != 1):
        problems.append("The end-of-service fraction is the full award: a lower agreed fraction waits for legal sign-off "
                        "(Art. 8, OQ-34); a higher amount is entered as a final-settlement earning. / نسبة المكافأة هي "
                        "المكافأة الكاملة: النسبة الأقل تنتظر الرأي القانوني (المادة 8)، والمبلغ الأعلى يُدخل استحقاقًا في "
                        "المخالصة النهائية.")
    if term.kind == TKind.RESIGNATION:
        if term.submitted_on is None:
            problems.append("A resignation needs the date it was submitted (Art. 79 bis). / يلزم تاريخ تقديم الاستقالة.")
    if lwd and lwd < contract.start_date:
        problems.append("The last working day is on or after the contract start (R36). / آخر يوم عمل في تاريخ بداية العقد أو بعده.")
    if term.kind == TKind.PROBATION_TERMINATION:
        end = _probation_end(use, contract, jurisdiction)
        if end is None or lwd > end:
            problems.append("The probation period has ended or there is none (Art. 54). / انتهت فترة التجربة أو لا توجد.")
    # R37: Art. 80 cases and the worker's hearing; Art. 81 cases.
    if term.kind in (TKind.ART80_DISMISSAL, TKind.ART81_EMPLOYEE_LEAVES):
        paragraphs = use.get(jurisdiction, "termination.article_paragraphs", lwd)
        article = "80" if term.kind == TKind.ART80_DISMISSAL else "81"
        allowed = paragraphs[f"art{article}"]
        if (term.article_paragraph or "").strip() not in allowed:
            problems.append(f"Name the paragraph of Art. {article} that applies ({', '.join(allowed)}) (R37). / حدد فقرة "
                            f"المادة {article} المنطبقة ({'، '.join(allowed)}).")
        if term.kind == TKind.ART80_DISMISSAL:
            if not (term.hearing_ref or "").strip():
                problems.append("Art. 80 needs the record that the worker was given the chance to object (hearing "
                                "reference) (R37). / تتطلب المادة 80 محضرًا يثبت إتاحة الفرصة للعامل لإبداء أسباب "
                                "معارضته.")
            if term.article_paragraph == paragraphs["art80_absence_paragraph"] and not (term.absence_warning_ref or "").strip():
                problems.append(f"Art. 80({term.article_paragraph}) absence needs the written warning reference (R37). / "
                                f"يتطلب الغياب وفق الفقرة ({term.article_paragraph}) من المادة 80 مرجع الإنذار الكتابي.")
    # R37: Art. 87 windows from the marriage contract or the delivery.
    if term.kind == TKind.FEMALE_MARRIAGE_CHILDBIRTH:
        windows = use.get(jurisdiction, "termination.art87_windows", lwd)
        ended = term.notice_given_on or term.submitted_on or lwd
        inside = []
        for day, months in ((term.marriage_contract_date, windows["marriage_months"]),
                            (term.delivery_date, windows["delivery_months"])):
            if day is not None:
                inside.append(day <= ended <= _add_months(day, int(months)))
        if emp.gender != Employee.Gender.FEMALE:
            problems.append("This kind is for a female worker (Art. 87). / هذا النوع للعاملة (المادة 87).")
        if not inside:
            problems.append("Enter the marriage contract date or the delivery date (Art. 87). / أدخل تاريخ عقد الزواج "
                            "أو تاريخ الوضع.")
        elif not any(inside):
            problems.append(f"The contract is ended outside {windows['marriage_months']} months of the marriage contract "
                            f"or {windows['delivery_months']} months of the delivery (Art. 87). / أُنهي العقد خارج "
                            f"{windows['marriage_months']} أشهر من عقد الزواج أو {windows['delivery_months']} أشهر من الوضع.")
    # R37 (d): the manual Art. 77 amount.
    if term.art77_compensation is not None:
        if term.kind != TKind.EMPLOYER_TERMINATION:
            problems.append("Art. 77 compensation is entered for an employer termination. / يُدخل التعويض وفق المادة 77 "
                            "لإنهاء صاحب العمل.")
        elif term.art77_compensation < 0:
            problems.append("The Art. 77 compensation is not negative. / لا يكون التعويض سالبًا.")
        else:
            months = _frac(use.get(jurisdiction, "compensation.art77_min_months", lwd)["months"])
            minimum = _frac_round(months * Fraction(_actual_wage(contract)))
            if term.art77_compensation < minimum and not (term.compensation_ref or "").strip():
                problems.append(f"Art. 77 compensation is never less than {months} months' wage ({minimum}) unless a "
                                "court judgment or contract clause is referenced (R37). / لا يقل التعويض وفق المادة 77 عن "
                                f"أجر {months} أشهر ({minimum}) ما لم يُذكر حكم محكمة أو بند في العقد.")
    # Notice (R37): open-ended contracts only; 30 days either side when not paid monthly; a shortfall is a warning
    # and a payroll input.
    if not fixed and term.kind in (TKind.EMPLOYER_TERMINATION, TKind.EMPLOYEE_NOTICE_ART75):
        notice = use.get(jurisdiction, "notice.min_days", lwd)
        side = "employer" if term.kind == TKind.EMPLOYER_TERMINATION else "employee"
        required = int(notice[side] if contract.paid_monthly else notice["not_monthly"])
        given = (lwd - term.notice_given_on).days if term.notice_given_on else 0
        term.notice_shortfall_days = max(0, required - given)
        if term.notice_shortfall_days:
            warnings.append(f"Notice is {term.notice_shortfall_days} day(s) short of {required} days: notice pay is a "
                            f"payroll input (Art. 75, 76). / مدة الإشعار تنقص {term.notice_shortfall_days} يومًا عن "
                            f"{required} يومًا: بدل الإشعار من مدخلات الرواتب.")
    # R59, R60.
    for article, en, ar in _protection_warnings(term, use, jurisdiction):
        term.protection_articles.append(article)
        warnings.append(f"{en} / {ar}")
    # R37 (GOSI check): retirement before the cohort's legal age.
    if term.kind == TKind.RETIREMENT:
        retirement_warnings, articles = _retirement_warnings(term, use)
        warnings += retirement_warnings
        term.protection_articles += articles
    return problems, warnings


@transaction.atomic
def record_termination(employee, *, user, **fields):
    """Draft a termination (``hr.record_termination``, R36). For a resignation the last working day is derived
    (Art. 79 bis) and must not be given."""
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.record_termination")
    unknown = set(fields) - TERMINATION_FIELDS
    if unknown:
        raise error(f"Unknown termination fields: {sorted(unknown)}.", f"حقول غير معروفة: {sorted(unknown)}.", code="fields")
    if emp.status not in (Employee.Status.ACTIVE, Employee.Status.ON_NOTICE):
        raise error("Only active employees are terminated.", "تُنهى خدمة الموظفين النشطين فقط.", code="employee_status")
    if Termination.objects.filter(employee=emp, status__in=(TS.DRAFT, TS.SUBMITTED, TS.APPROVED)).exists():
        raise error("This employee already has an open or approved termination.", "لهذا الموظف إنهاء خدمة قائم أو معتمد.",
                    code="termination_exists")
    for name in ("art77_compensation", "agreed_fraction"):
        if fields.get(name) is not None:
            fields[name] = _decimal(fields[name], name)
    use = ParamUse()
    term = Termination(entity=emp.entity, employee=emp, created_by=user, source=_source(user), **fields)
    if term.kind == TKind.RESIGNATION:
        if fields.get("last_working_day"):
            raise error("A resignation's last working day is derived from its acceptance (Art. 79 bis); do not enter it.",
                        "يُستنتج آخر يوم عمل في الاستقالة من قبولها؛ لا يُدخَل يدويًا.", code="lwd_derived")
        if term.submitted_on and emp.employment_basis == Basis.KSA_EMPLOYEE:
            term.last_working_day, problems = _resignation_dates(term, use, "SA")
            if problems:
                raise _problems(problems, "termination_invalid")
    if term.last_working_day is None:
        raise error("The last working day is required.", "آخر يوم عمل مطلوب.", code="lwd_required")
    term.contract = _terms_on(emp, term.last_working_day) or _approved_versions(emp).order_by("-version_no").first()
    if term.contract is None:
        raise error("The employee has no approved contract.", "لا يوجد للموظف عقد معتمد.", code="no_terms")
    problems, warnings = _termination_problems(term, use)
    if problems:
        raise _problems(problems, "termination_invalid")
    term.warnings = warnings
    term.save()
    return term


@transaction.atomic
def submit_termination(termination, *, user):
    t = _lock(Termination, termination.pk)
    access.require(user, t.entity, "hr.record_termination")
    if t.status != TS.DRAFT:
        raise error("Only drafts are submitted.", "تُقدَّم المسودات فقط.", code="not_draft")
    t.status, t.submitted_by = TS.SUBMITTED, user
    return _save(t)


@dataclass(frozen=True)
class EosbResult:
    """A pure EOSB calculation (R39, R40); ``parameter_ids`` lists the rows used."""

    employee_id: int
    kind: str
    as_of: date
    service_start: date
    service_end: date
    calendar_basis: str
    service_days: int
    excluded_days: int
    service_years: Fraction
    wage_basis: Decimal
    wage_components: dict
    full_award: Decimal
    fraction: Fraction
    award: Decimal
    currency: str
    contract_version: int
    parameter_ids: tuple
    legal_flags: tuple = ()


def _band_fraction(bands, years):
    result = None
    for band in bands:
        if "from" in band and years >= _frac(band["from"]):
            result = _frac(band["fraction"])
        if "above" in band and years > _frac(band["above"]):
            result = _frac(band["fraction"])
    return result if result is not None else Fraction(0)


def _compute_eosb(employee, as_of, kind, use, additional_wage=ZERO):
    """R39 (Art. 84, 85): service in years of the contract's calendar basis (Art. 10) minus suspended unpaid days;
    wage = the last monthly amounts of components counting for EOSB in the version in force on ``as_of`` plus
    ``additional_wage`` (regular bonuses, commissions, in-kind benefits from payroll; Art. 2, 84, 86); full award =
    ½ month per year for the first 5 years + 1 month per later year, part years pro rata; award = full × fraction
    of the kind. Exact rationals; rounded half-up to 2 dp only at the end."""
    emp = employee
    if emp.employment_basis == Basis.REMUNERATION:
        raise error("Remuneration staff have no end-of-service benefit (R52).", "لا مكافأة نهاية خدمة لأصحاب المكافأة الشهرية.",
                    code="remuneration_no_eosb")
    jurisdiction = _jurisdiction(emp, "end-of-service benefit", "مكافأة نهاية الخدمة")
    table = use.get(jurisdiction, "eosb.award_table", as_of, "end-of-service benefit", "مكافأة نهاية الخدمة")
    fractions = use.get(jurisdiction, "eosb.fraction_table", as_of, "end-of-service benefit", "مكافأة نهاية الخدمة")
    terms = _terms_on(emp, as_of)
    if terms is None:
        raise error(f"No approved contract is in force on {as_of}.", f"لا يوجد عقد معتمد ساري في {as_of}.", code="no_terms")
    if kind not in fractions:
        raise error(f"No end-of-service fraction for {kind}.", f"لا توجد نسبة مكافأة لنوع الإنهاء {kind}.", code="eosb_kind")
    year_days = _year_days(use, jurisdiction, terms.calendar_basis, as_of)
    start = emp.hire_date
    service_days = (as_of - start).days + 1
    excluded = len(_suspended_days(use, jurisdiction, emp, terms.calendar_basis, start, as_of))
    years = _years_between(start, as_of + timedelta(days=1 - excluded), terms.calendar_basis, year_days)
    components = {c.component.code: c.monthly_amount for c in terms.components.select_related("component")
                  if c.component.counts_for_eosb}
    # R39: payroll's regular additions (payroll_port.AdditionalWage), or a plain amount from a preview caller.
    extra_parts = dict(getattr(additional_wage, "parts", None) or {})
    extra = _decimal(getattr(additional_wage, "amount", additional_wage), "additional_wage")
    wage = sum(components.values(), ZERO) + extra
    if extra:
        components.update({f"additional:{code}": _decimal(v, code) for code, v in extra_parts.items()}
                          or {"additional": extra})
    wage_f = Fraction(wage)
    full, remaining, done = Fraction(0), years, Fraction(0)
    for tier in table["tiers"]:
        cap = _frac(tier["up_to_years"])
        span = remaining if cap is None else max(Fraction(0), min(remaining, cap - done))
        full += span * _frac(tier["months_per_year"]) * wage_f
        remaining -= span
        done += span
    fraction = _band_fraction(fractions[kind], years)
    return EosbResult(
        employee_id=emp.pk, kind=kind, as_of=as_of, service_start=start, service_end=as_of,
        calendar_basis=terms.calendar_basis, service_days=service_days, excluded_days=excluded, service_years=years,
        wage_basis=_round(wage), wage_components={k: str(v) for k, v in components.items()},
        full_award=_frac_round(full), fraction=fraction, award=_frac_round(full * fraction),
        currency=terms.currency_id, contract_version=terms.version_no, parameter_ids=tuple(use.ids),
        legal_flags=(f"{OQ33[0]} / {OQ33[1]}",) if kind == TKind.EMPLOYEE_NOTICE_ART75 else (),
    )


def calculate_eosb(employee, as_of, kind, *, user, additional_wage=ZERO):
    """R40: pure EOSB preview (no write); payroll uses it for the monthly provision (payroll R24)."""
    access.require(user, employee.entity, "hr.calculate_eosb")
    return _compute_eosb(employee, as_of, kind, ParamUse(), additional_wage)


def _snapshot(result, termination, user, supersedes=None):
    with localcontext() as ctx:
        ctx.prec = 60
        approx = (Decimal(result.fraction.numerator) / Decimal(result.fraction.denominator)).quantize(Decimal("0.0001"))
        years = (Decimal(result.service_years.numerator) / Decimal(result.service_years.denominator)).quantize(
            Decimal("0.0001"))
    return EosbCalculation.objects.create(
        entity=termination.entity, employee=termination.employee, termination=termination, kind=result.kind,
        as_of=result.as_of, service_start=result.service_start, service_end=result.service_end,
        calendar_basis=result.calendar_basis, service_days=result.service_days, excluded_days=result.excluded_days,
        service_years=years, wage_basis=result.wage_basis, wage_components=result.wage_components,
        full_award=result.full_award, fraction=approx, fraction_text=str(result.fraction), award=result.award,
        currency_id=result.currency, parameter_ids=list(result.parameter_ids), contract_version=result.contract_version,
        legal_flags=list(result.legal_flags), computed_by=user, supersedes=supersedes)


def _settlement_input(term, eosb, user, use):
    """R35, R64, payroll R34: annual balance at the last working day, daily wage of EOSB components / 30, notice
    shortfall, the Art. 77 amount, who ended the contract and the Art. 88 deadline (7 days, 14 when the worker ended
    it), and the loan balances payroll recovers from the final dues without the monthly caps (H18). Remuneration:
    no EOSB, leave or GOSI lines, no Labour Law deadline and outside WPS (R52, H17)."""
    emp = term.employee
    terms = term.contract
    statutory = emp.employment_basis != Basis.REMUNERATION
    balance, daily, encash, due_by = ZERO, ZERO, ZERO, None
    if statutory:
        jurisdiction = _jurisdiction(emp)
        lt = LeaveType.objects.filter(entity=emp.entity, code="ANNUAL").first()
        # Every accrual row is now what was due up to the last working day (finding 2), so the whole ledger is the
        # balance at the last working day.
        balance = _balance(emp, lt) if lt else ZERO
        divisor = int(use.get(jurisdiction, "leave.encashment", term.last_working_day)["day_divisor"])
        wage = _actual_wage(terms)
        daily = _round(Decimal(wage) / divisor)
        encash = _frac_round(Fraction(max(balance, ZERO)) * Fraction(wage) / divisor)
        due = use.get(jurisdiction, "final_settlement.due_days", term.last_working_day)
        due_by = term.last_working_day + timedelta(days=int(due["worker" if term.worker_initiated else "employer"]))
    loans = payroll_port.get_port().outstanding_balances(emp, term.last_working_day)
    si = FinalSettlementInput.objects.create(
        entity=term.entity, employee=emp, termination=term, eosb_calculation=eosb, last_working_day=term.last_working_day,
        currency_id=terms.currency_id, eosb_award=eosb.award if eosb else ZERO, leave_balance_days=max(balance, ZERO),
        daily_wage=daily, leave_encashment=encash, notice_shortfall_days=term.notice_shortfall_days,
        includes_statutory_lines=statutory, worker_initiated=term.worker_initiated, due_by=due_by,
        art77_compensation=term.art77_compensation or ZERO, outstanding_loans=_decimal(loans.loans, "loans"),
        outstanding_bai_ajel=_decimal(loans.bai_ajel, "bai_ajel"), bai_ajel_undertaking=bool(loans.bai_ajel_undertaking),
        wps_included=emp.wps_included, created_by=user)
    payroll_port.get_port().create_draft(si, user)
    return si


@transaction.atomic
def approve_termination(termination, *, user, acknowledge_protection=False):
    """R38: approval in one transaction: termination approved; employee terminated (or on notice until the last
    working day); contract ended; active assignments ended at the last working day (events, R27); leave after it
    cancelled and returned (R35); the annual leave accrued up to the last working day; an EOSB snapshot (not for
    remuneration staff, R52) with payroll's regular additions to the wage (R39); settlement inputs handed to payroll;
    the GOSI exclusion checklist item (R65). Any failure rolls everything back.

    R59, R60 and the R37 retirement age check: when the Art. 82, Art. 155 or GOSI legal-age warning applies, the
    approver must pass ``acknowledge_protection=True``; the acknowledgement (who, when, which articles) is stored on
    the termination. Without it the approval is refused (OQ-23 default: warn and acknowledge, not refuse
    outright)."""
    t = _lock(Termination, termination.pk)
    access.require(user, t.entity, "hr.approve_termination", document=t)
    emp = _lock(Employee, t.employee_id)
    _deny_own(user, t.entity, "hr.approve_termination", emp)
    if t.status != TS.SUBMITTED:
        raise error("Only submitted terminations are approved.", "يُعتمد إنهاء الخدمة المقدم فقط.", code="not_submitted")
    use = ParamUse()
    if t.kind == TKind.RESIGNATION and emp.employment_basis == Basis.KSA_EMPLOYEE:
        deemed = t.submitted_on + timedelta(
            days=int(use.get("SA", "termination.resignation", t.submitted_on)["deemed_acceptance_days"]))
        t.accepted_on = min(_today(), deemed)
        t.deemed_accepted = t.postponed_until is None and t.accepted_on == deemed
        t.last_working_day, problems = _resignation_dates(t, use, "SA", accepted_on=t.accepted_on)
        if problems:
            raise _problems(problems, "termination_invalid")
        t.contract = _terms_on(emp, t.last_working_day) or t.contract
    return _complete_termination(t, emp, use, actor=user, approver=user, acknowledge_protection=acknowledge_protection)


def _complete_termination(t, emp, use, *, actor, approver, acknowledge_protection):
    """The R38 effects of an approval by ``approver``, or of a deemed acceptance (Art. 79 bis, ``approver`` None,
    run by the job); ``actor`` is the user recorded on the rows written."""
    problems, warnings = _termination_problems(t, use)
    if problems:
        raise _problems(problems, "termination_invalid")
    if t.protection_articles:
        if not acknowledge_protection:
            names = ", ".join(t.protection_articles)
            raise error(f"Approval needs your acknowledgement of the warnings ({names}): no dismissal for illness before "
                        "sick leave is used up (Art. 82); none during pregnancy or maternity (Art. 155); retirement "
                        "before the GOSI legal age (R37).",
                        f"يتطلب الاعتماد إقرارك بالتنبيهات ({names}): لا فصل بسبب المرض قبل استنفاد الإجازة المرضية "
                        "(المادة 82)، ولا فصل أثناء الحمل أو الوضع (المادة 155)، والتقاعد قبل السن النظامية للتأمينات.",
                        code="protection_ack_required")
        t.protection_acknowledged_by, t.protection_acknowledged_at = approver, timezone.now()
    eosb_result = None
    if emp.employment_basis != Basis.REMUNERATION:
        extra = payroll_port.get_port().eosb_additional_wage(emp, t.last_working_day)
        eosb_result = _compute_eosb(emp, t.last_working_day, t.kind, use, extra)
    use.check_verified()
    lwd = t.last_working_day
    t.gosi_exclusion_reason = _mapped_exclusion_reason(t) if _gosi_excludable(emp) else ""
    t.status, t.approved_by, t.approved_at, t.warnings = TS.APPROVED, approver, timezone.now(), warnings + use.warnings
    _save(t)
    emp.status = Employee.Status.TERMINATED if lwd < _today() else Employee.Status.ON_NOTICE
    _save(emp, update_fields=["status"])
    for c in _approved_versions(emp).filter(ended_on__isnull=True):
        c.ended_on = lwd
        _save(c, update_fields=["ended_on"])
    for a in Assignment.objects.filter(employee=emp, status__in=(ASt.APPROVED, ASt.ACTIVE, ASt.ENDED)):
        a = _lock(Assignment, a.pk)
        if a.start_date > lwd:
            _cancel_assignment_for_termination(a, t, actor)
        elif a.end_date is None or a.end_date > lwd:
            _end_assignment(a, lwd, t.reason_ar, t.reason_en, actor, termination=t)
    for r in LeaveRequest.objects.filter(employee=emp, status=LR.APPROVED, end_date__gt=lwd).order_by("start_date"):
        _cancel_leave(_lock(LeaveRequest, r.pk), actor, lwd + timedelta(days=1), use)
    for r in LeaveRequest.objects.filter(employee=emp, status__in=(LR.DRAFT, LR.SUBMITTED), end_date__gt=lwd):
        r.status = LR.CANCELLED
        _save(r)
    if emp.employment_basis != Basis.REMUNERATION:
        _accrue_to_last_day(t, use, _jurisdiction(emp), actor)
    eosb = _snapshot(eosb_result, t, actor) if eosb_result else None
    _settlement_input(t, eosb, actor, use)
    _gosi_exclusion_item(t, emp)
    use.mark_used()
    return t


def _gosi_excludable(emp):
    """R65: who gets the GOSI exclusion item: a ``ksa_employee`` (any nationality) with a GOSI number; a remuneration
    person only when Saudi with a GOSI number (payroll OQ-39); never a local entity employee."""
    if not emp.gosi_number or emp.employment_basis == Basis.LOCAL_ENTITY_EMPLOYEE:
        return False
    return emp.employment_basis == Basis.KSA_EMPLOYEE or emp.is_saudi


def _mapped_exclusion_reason(t):
    """R65: the reason ``gosi.exclusion_reason_map`` gives the termination kind, or "" (empty map, OQ-42). A map row
    that is not verified while verified rows are required is not used (the item then asks for the reason), so an
    unverified mapping never reaches GOSI and never blocks the approval."""
    row = param("SA", "gosi.exclusion_reason_map", t.last_working_day, required=False)
    if row is None or (_required_verified() and row.status != StatutoryParameter.Status.VERIFIED):
        return ""
    return str((row.value or {}).get(t.kind, ""))[:40]


def _gosi_exclusion_item(t, emp):
    """R65: the open checklist item for the entity's hr_officers: exclude the contributor on the GOSI portal by the
    last day of the leaving month with the mapped reason (or choose it there when unmapped). Nothing is sent to
    GOSI."""
    if not _gosi_excludable(emp):
        return None
    lwd = t.last_working_day
    due = date(lwd.year, lwd.month, calendar.monthrange(lwd.year, lwd.month)[1])
    code, reason = emp.employee_code, t.gosi_exclusion_reason
    if reason:
        en = f"Exclude {code} on GOSI by {due} with reason {reason}, then record the date and the GOSI reference here."
        ar = f"استبعد {code} من التأمينات قبل {due} بالسبب {reason}، ثم سجّل التاريخ ومرجع التأمينات هنا."
    else:
        en = (f"Exclude {code} on GOSI by {due}: the reason is not mapped, choose it on the GOSI portal and record it "
              "here with the date and the GOSI reference.")
        ar = (f"استبعد {code} من التأمينات قبل {due}: السبب غير محدد، اختره في بوابة التأمينات وسجّله هنا مع التاريخ "
              "ومرجع التأمينات.")
    item = HrChecklistItem(entity=t.entity, termination=t, kind=HrChecklistItem.Kind.GOSI_EXCLUSION, due_date=due,
                           text_en=en, text_ar=ar)
    return _save(item)


@transaction.atomic
def record_gosi_exclusion(termination, *, user, excluded_on, reference, reason=""):
    """R65: the hr_officer records that the contributor was excluded on the GOSI portal (date and portal reference,
    and the reason used when none was mapped); the checklist item closes and its reminders stop."""
    t = _lock(Termination, termination.pk)
    access.require(user, t.entity, "hr.record_termination")
    item = HrChecklistItem.objects.select_for_update().filter(termination=t, kind=HrChecklistItem.Kind.GOSI_EXCLUSION,
                                                              status=HrChecklistItem.Status.OPEN).first()
    if t.status != TS.APPROVED or item is None:
        raise error("There is no open GOSI exclusion item for this termination.",
                    "لا يوجد بند مفتوح للاستبعاد من التأمينات لإنهاء الخدمة هذا.", code="checklist")
    reference, reason = (reference or "").strip(), (reason or "").strip()
    if not reference or len(reference) > 60 or excluded_on is None:
        raise error("Enter the exclusion date and the GOSI portal reference (at most 60 characters).",
                    "أدخل تاريخ الاستبعاد ومرجع بوابة التأمينات (60 حرفًا بحد أقصى).", code="checklist")
    if t.gosi_exclusion_reason and reason and reason != t.gosi_exclusion_reason:
        raise error(f"The mapped reason is {t.gosi_exclusion_reason}; a different reason is fined (R65).",
                    f"السبب المحدد هو {t.gosi_exclusion_reason}؛ واستخدام سبب مختلف تفرض عليه غرامة.", code="checklist")
    if not t.gosi_exclusion_reason and not reason:
        raise error("The reason was not mapped: enter the reason used on the GOSI portal (R65).",
                    "السبب غير محدد: أدخل السبب المستخدم في بوابة التأمينات.", code="checklist")
    t.gosi_exclusion_reason = t.gosi_exclusion_reason or reason[:40]
    t.gosi_excluded_on, t.gosi_exclusion_ref = excluded_on, reference
    _save(t, update_fields=["gosi_exclusion_reason", "gosi_excluded_on", "gosi_exclusion_ref"])
    item.status, item.closed_by, item.closed_at = HrChecklistItem.Status.CLOSED, user, timezone.now()
    _save(item)
    return t


def _gosi_exclusion_reminders(today):
    """R65 reminders (``hr_gosi_exclusion_reminders``, daily): on the 25th and on the last day of the leaving month,
    once each, the entity's HR officers are reminded of every open GOSI exclusion item (en/ar, employee code only)."""
    sent = 0
    for item_id in list(HrChecklistItem.objects.filter(status=HrChecklistItem.Status.OPEN,
                                                       kind=HrChecklistItem.Kind.GOSI_EXCLUSION,
                                                       due_date__year=today.year, due_date__month=today.month)
                        .values_list("pk", flat=True)):
        with transaction.atomic():
            item = HrChecklistItem.objects.select_for_update().get(pk=item_id)
            if item.status != HrChecklistItem.Status.OPEN or not (today.day == 25 or today == item.due_date):
                continue
            if str(today) in (item.reminders_sent or {}):
                continue
            _alert(item.entity, _recipients(item.entity, "hr.receive_alerts"), HrAlert.Kind.GOSI_EXCLUSION,
                   f"Reminder: {item.text_en}", f"تذكير: {item.text_ar}")
            item.reminders_sent = {**(item.reminders_sent or {}), str(today): timezone.now().isoformat()}
            _save(item)
            sent += 1
    return sent


@transaction.atomic
def postpone_resignation(termination, *, user, postponed_until, reason_ar, reason_en=""):
    """Art. 79 bis (R41): before day 30 the approver (``hr.approve_termination``, A, not the drafter or submitter)
    may postpone accepting a resignation by up to the parameter's days after day 30, with a written reason; the last
    working day becomes the end of the postponement. Postponed again only within the same limit."""
    t = _lock(Termination, termination.pk)
    access.require(user, t.entity, "hr.approve_termination", document=t)
    _deny_own(user, t.entity, "hr.approve_termination", t.employee)
    if t.kind != TKind.RESIGNATION or t.status not in (TS.DRAFT, TS.SUBMITTED) or t.submitted_on is None:
        raise error("Only an open resignation is postponed.", "لا يُؤجَّل إلا قبول استقالة قائمة.", code="postpone")
    use = ParamUse()
    deemed = t.submitted_on + timedelta(
        days=int(use.get("SA", "termination.resignation", t.submitted_on)["deemed_acceptance_days"]))
    if _today() >= deemed:
        raise error(f"A postponement is decided before {deemed}, the day the resignation is deemed accepted "
                    "(Art. 79 bis).", f"يُقرَّر التأجيل قبل {deemed}، يوم اعتبار الاستقالة مقبولة.", code="postpone_late")
    if postponed_until is None or postponed_until <= deemed:
        raise error(f"The postponement ends after {deemed} (Art. 79 bis).", f"ينتهي التأجيل بعد {deemed}.",
                    code="postpone")
    t.postponed_until, t.postponement_reason_ar, t.postponement_reason_en = postponed_until, reason_ar, reason_en
    t.postponed_by = user
    t.last_working_day, problems = _resignation_dates(t, use, "SA")
    if problems:
        raise _problems(problems, "termination_invalid")
    use.check_verified()
    _save(t)
    use.mark_used()
    return t


def _approvers(entity):
    return _recipients(entity, "hr.approve_termination")


def _resignation_deadlines(today):
    """Art. 79 bis job (``hr_resignation_deadlines``, daily): for each open resignation of a ``ksa_employee`` the
    approvers are reminded once on day 20 and once on day 27 after its submission (``HR_RESIGNATION_REMINDER_DAYS``,
    default); on day 30 with no reply (or at the end of a postponement) a submitted resignation is deemed accepted
    and the R38 effects run, recorded by the HR user who submitted it, with no approver. Unverified parameters
    (R7) skip the resignation and are reported. Returns ``{"reminders", "deemed", "skipped"}``."""
    reminder_days = tuple(getattr(settings, "HR_RESIGNATION_REMINDER_DAYS", (20, 27)))
    report = {"reminders": 0, "deemed": [], "skipped": []}
    for t_id in list(Termination.objects.filter(kind=TKind.RESIGNATION, status__in=(TS.DRAFT, TS.SUBMITTED),
                                                submitted_on__isnull=False,
                                                employee__employment_basis=Basis.KSA_EMPLOYEE)
                     .order_by("pk").values_list("pk", flat=True)):
        try:
            with transaction.atomic():
                t = _lock(Termination, t_id)
                row = param("SA", "termination.resignation", t.submitted_on)
                deemed = t.submitted_on + timedelta(days=int(row.value["deemed_acceptance_days"]))
                code = t.employee.employee_code
                for n in reminder_days:
                    if t.postponed_until is None and t.submitted_on + timedelta(days=n) <= today < deemed \
                            and str(n) not in (t.deadline_alerts or {}):
                        _alert(t.entity, _approvers(t.entity), HrAlert.Kind.RESIGNATION_DEADLINE,
                               f"{code}: the resignation submitted on {t.submitted_on} is deemed accepted on {deemed} "
                               "unless it is accepted or postponed (with a written reason) before then (Art. 79 bis).",
                               f"{code}: تُعد الاستقالة المقدمة في {t.submitted_on} مقبولة في {deemed} ما لم تُقبل أو "
                               "يُؤجَّل قبولها (بسبب مكتوب) قبل ذلك (المادة 79 مكرر).")
                        t.deadline_alerts = {**(t.deadline_alerts or {}), str(n): timezone.now().isoformat()}
                        _save(t, update_fields=["deadline_alerts"])
                        report["reminders"] += 1
                if t.status == TS.SUBMITTED and today >= (t.postponed_until or deemed):
                    _deem_resignation(t, deemed)
                    report["deemed"].append(code)
        except HrError as exc:
            report["skipped"].append((Termination.objects.get(pk=t_id).employee.employee_code, exc.messages[0]))
    return report


def _deem_resignation(t, deemed):
    """Art. 79 bis: no reply by day 30 (or the postponement ended): accepted by law on that day."""
    emp = _lock(Employee, t.employee_id)
    use = ParamUse()
    due = t.postponed_until or deemed
    t.accepted_on, t.deemed_accepted = due, t.postponed_until is None
    t.last_working_day, problems = _resignation_dates(t, use, "SA", accepted_on=due)
    if problems:
        raise _problems(problems, "termination_invalid")
    t.contract = _terms_on(emp, t.last_working_day) or t.contract
    actor = t.submitted_by or t.created_by
    _complete_termination(t, emp, use, actor=actor, approver=None, acknowledge_protection=False)
    _alert(t.entity, _approvers(t.entity), HrAlert.Kind.RESIGNATION_DEADLINE,
           f"{emp.employee_code}: the resignation is accepted by law on {due} (Art. 79 bis); last working day "
           f"{t.last_working_day}.",
           f"{emp.employee_code}: قُبلت الاستقالة بحكم النظام في {due} (المادة 79 مكرر)؛ آخر يوم عمل {t.last_working_day}.")
    return t


def _cancel_assignment_for_termination(a, t, user):
    previous = a.end_date
    a.status, a.end_reason_ar, a.ended_by, a.ended_at = ASt.CANCELLED, t.reason_ar, user, timezone.now()
    a.end_date = a.start_date - timedelta(days=1)
    _save(a)
    _emit(a, AssignmentEvent.Kind.CANCELLED, a.end_date, previous, user, termination=t)


@transaction.atomic
def withdraw_termination(termination, *, user, reason_ar):
    """R41 as corrected by Art. 79 bis.

    - A resignation not yet accepted is withdrawn within 7 days of its submission; later, or once accepted, it needs a
      new mutual agreement (refused here, legal sign-off).
    - Another approved termination is withdrawn before the last working day and before payroll approved the final
      settlement: the contract and employee status are restored, the settlement inputs discarded, assignments get
      their previous end date back (an open end cannot be sent to billing: OQ-16, warning).
    """
    t = _lock(Termination, termination.pk)
    if not reason_ar:
        raise error("The reason in Arabic is required.", "السبب بالعربية مطلوب.", code="reason_required")
    if t.status in (TS.DRAFT, TS.SUBMITTED):
        access.require(user, t.entity, "hr.record_termination")
        if t.kind == TKind.RESIGNATION and t.submitted_on:
            days = int(param("SA", "termination.resignation", t.submitted_on).value["withdrawal_days"])
            if (_today() - t.submitted_on).days > days:
                raise error(f"A resignation is withdrawn within {days} days of its submission; later it needs a new "
                            "agreement (Art. 79 bis).", f"تُسحب الاستقالة خلال {days} أيام من تقديمها؛ وبعدها تلزم "
                            "موافقة جديدة من الطرفين.", code="withdrawal_late")
        t.status, t.withdrawn_by, t.withdrawn_at = TS.WITHDRAWN, user, timezone.now()
        if t.kind == TKind.RESIGNATION:
            t.withdrawn_on = _today()  # Art. 79 bis statutory withdrawal
        return _save(t), []
    access.require(user, t.entity, "hr.approve_termination", document=t)
    if t.status != TS.APPROVED:
        raise error("This termination cannot be withdrawn.", "لا يمكن سحب إنهاء الخدمة هذا.", code="withdraw")
    if t.kind == TKind.RESIGNATION:
        raise error("An accepted resignation is not withdrawn by the employee; it needs a new mutual agreement "
                    "(Art. 79 bis, legal sign-off).", "لا تُسحب الاستقالة بعد قبولها؛ يلزم اتفاق جديد بين الطرفين.",
                    code="withdrawal_after_acceptance")
    if t.last_working_day <= _today() or payroll_port.get_port().is_approved(t):
        raise error("A termination is withdrawn only before the last working day and before the final settlement is "
                    "approved (R41).", "يُسحب إنهاء الخدمة قبل آخر يوم عمل وقبل اعتماد المخالصة النهائية فقط.",
                    code="withdrawal_late")
    emp = _lock(Employee, t.employee_id)
    t.status, t.withdrawn_by, t.withdrawn_at = TS.WITHDRAWN, user, timezone.now()
    _save(t)
    emp.status = Employee.Status.ACTIVE
    _save(emp, update_fields=["status"])
    for c in _approved_versions(emp).filter(ended_on=t.last_working_day):
        c.ended_on = None
        _save(c, update_fields=["ended_on"])
    warnings = []
    # Nit 16: the events this termination's approval emitted, by their link (not by actor and time).
    for ev in list(AssignmentEvent.objects.filter(termination=t).select_related("assignment").order_by("-id")):
        a = _lock(Assignment, ev.assignment_id)
        if ev.kind == AssignmentEvent.Kind.CANCELLED:
            a.status, a.end_date = ASt.APPROVED, ev.previous_end_date
            _save(a)
            warnings.append(f"{a.reference}: cancellation reversed in HR; billing must reopen it (OQ-16). / "
                            f"{a.reference}: أُلغي الإلغاء في الموارد البشرية؛ ويجب إعادة فتحه في الفوترة.")
        elif ev.previous_end_date is not None:
            a.end_date = ev.previous_end_date
            _save(a)
            _emit(a, AssignmentEvent.Kind.END_CHANGED, ev.previous_end_date, ev.end_date, user, termination=t)
        else:
            a.status, a.end_date = (ASt.ACTIVE if a.start_date <= _today() else ASt.APPROVED), None
            _save(a)
            warnings.append(f"{a.reference}: the end date was removed in HR; billing cannot remove an end date, it must "
                            "be reopened there (OQ-16). / " f"{a.reference}: أُزيل تاريخ النهاية في الموارد البشرية؛ "
                            "ولا تزيل الفوترة تاريخ النهاية فيجب إعادة فتحه هناك.")
    for si in FinalSettlementInput.objects.filter(termination=t, status=FinalSettlementInput.Status.DRAFT):
        si.status = FinalSettlementInput.Status.DISCARDED
        si._service_write = True
        si.save(update_fields=["status"])
    _reverse_termination_accruals(t, user)
    _void_snapshot(t, user)
    payroll_port.get_port().discard_draft(t, user)
    return t, warnings


def _void_snapshot(t, user):
    """R41 / nit 16: a withdrawal supersedes the termination's latest EOSB snapshot by a voided copy, so no current
    snapshot of a withdrawn termination is left for payroll."""
    latest = EosbCalculation.objects.filter(termination=t, superseded_by__isnull=True).order_by("-id").first()
    if latest is None:
        return None
    fields = {f.name: getattr(latest, f.name) for f in EosbCalculation._meta.concrete_fields
              if f.name not in ("id", "computed_by", "computed_at", "supersedes", "voided")}
    return EosbCalculation.objects.create(**fields, computed_by=user, supersedes=latest, voided=True)


@transaction.atomic
def recalculate_eosb(termination, *, user):
    """R40: before payroll approves the final settlement a recalculation writes a new snapshot that supersedes the
    latest one; afterwards it is refused."""
    t = _lock(Termination, termination.pk)
    access.require(user, t.entity, "hr.calculate_eosb")
    if t.status != TS.APPROVED:
        raise error("Only approved terminations are recalculated.", "يُعاد الحساب لإنهاء الخدمة المعتمد فقط.",
                    code="termination_status")
    if payroll_port.get_port().is_approved(t):
        raise error("The final settlement is approved; the end-of-service snapshot is final (R40).",
                    "اعتُمدت المخالصة النهائية؛ لقطة مكافأة نهاية الخدمة نهائية.", code="settlement_approved")
    latest = EosbCalculation.objects.filter(termination=t, superseded_by__isnull=True).order_by("-id").first()
    if latest is None:
        raise error("This termination has no end-of-service calculation.", "لا يوجد حساب مكافأة لهذا الإنهاء.",
                    code="no_eosb")
    use = ParamUse()
    _lock(Employee, t.employee_id)
    result = _compute_eosb(t.employee, t.last_working_day, t.kind, use)
    use.check_verified()
    _accrue_to_last_day(t, use, _jurisdiction(t.employee), user)
    snap = _snapshot(result, t, user, supersedes=latest)
    for si in FinalSettlementInput.objects.filter(termination=t, status=FinalSettlementInput.Status.DRAFT):
        si.status = FinalSettlementInput.Status.SUPERSEDED
        si._service_write = True
        si.save(update_fields=["status"])
    _settlement_input(t, snap, user, use)
    use.mark_used()
    return snap


# =====================================================================================================
# Documents and alerts (R42 to R45)
# =====================================================================================================

DOCUMENT_FIELDS = frozenset({"doc_type", "number", "issuing_country", "issue_date", "expiry_date", "expiry_date_hijri"})


def _required_documents(emp):
    if emp.employment_basis == Basis.REMUNERATION:  # R52: no iqama or work permit required
        return ("passport",)
    return EmployeeDocument.EXPIRY_REQUIRED


@transaction.atomic
def add_document(employee, *, user, supersedes=None, **fields):
    """Record a document (``hr.manage_employee``). A renewal passes ``supersedes``: the old row stops alerting (R45)."""
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.manage_employee")
    unknown = set(fields) - DOCUMENT_FIELDS
    if unknown:
        raise error(f"Unknown document fields: {sorted(unknown)}.", f"حقول غير معروفة: {sorted(unknown)}.", code="fields")
    doc = EmployeeDocument(entity=emp.entity, employee=emp, created_by=user, source=_source(user), **fields)
    if doc.doc_type in EmployeeDocument.EXPIRY_REQUIRED and doc.expiry_date is None:
        raise error("Iqama, passport and work permit need an expiry date (R42).",
                    "يلزم تاريخ انتهاء للإقامة وجواز السفر ورخصة العمل.", code="expiry_required")
    if not doc.number:
        raise error("The document number is required.", "رقم المستند مطلوب.", code="number_required")
    _save(doc)
    if supersedes is not None:
        old = EmployeeDocument.objects.select_for_update().get(pk=supersedes.pk)
        if old.employee_id != emp.pk or old.doc_type != doc.doc_type or old.superseded_by_id:
            raise error("A renewal replaces a current document of the same type and employee (R45).",
                        "يحل التجديد محل مستند ساري من النوع نفسه وللموظف نفسه.", code="renewal")
        old.superseded_by = doc
        _save(old, update_fields=["superseded_by"])
    return doc


def _document_flag(emp, on):
    s = _settings(emp.entity)
    horizon = max(s.alert_days or [0])
    docs = EmployeeDocument.objects.filter(employee=emp, superseded_by__isnull=True,
                                           doc_type__in=_required_documents(emp), expiry_date__isnull=False)
    flag, warnings = "ok", []
    for d in docs:
        if d.expiry_date < on:
            flag = "expired"
        elif flag == "ok" and d.expiry_date <= on + timedelta(days=horizon):
            flag = "expiring"
    if (emp.employment_basis == Basis.KSA_EMPLOYEE and not emp.is_saudi and emp.status == Employee.Status.ACTIVE
            and not docs.filter(doc_type="iqama", expiry_date__gte=on).exists()):
        warnings.append("A non-Saudi active employee has no current iqama (R42). / لا توجد إقامة سارية لموظف غير سعودي نشط.")
    return flag, warnings


def document_flag(employee, *, user, on=None):
    """R44: ``expired`` / ``expiring`` / ``ok`` and the R42 warning; payroll shows it as a warning only."""
    access.require(user, employee.entity, "hr.view")
    return _document_flag(employee, on or _today())


def _recipients(entity, permission=None, roles=()):
    at = timezone.now()
    q = Q()
    if permission:
        q |= Q(role__permission_rows__codename=permission)
    if roles:
        q |= Q(role__code__in=roles)
    users = {m.user for m in Membership.objects.active(at).filter(entity=entity).filter(q).select_related("user")}
    return sorted(users, key=lambda u: u.pk)


def _alert(entity, users, kind, text_en, text_ar):
    for u in users:
        profile = access_services.profile_of(u)
        HrAlert.objects.create(entity=entity, recipient=u, kind=kind, text_en=text_en, text_ar=text_ar,
                               language=(profile.preferred_language if profile else "en") or "en")


DOC_LABELS = {c.value: c.label for c in EmployeeDocument.DocType}


# =====================================================================================================
# Jobs (operator commands; reviewed allow-list entries)
# =====================================================================================================

def send_document_alerts(*, today=None, entity=None):
    """``hr_document_alerts`` (daily, R43): per current document and threshold, the first run on or after expiry −
    threshold alerts the entity's HR officers once (only the most urgent threshold reached; larger ones are marked
    sent too). Text: employee code, document type, date; never the number (R11)."""
    today = today or _today()
    sent = 0
    qs = EmployeeDocument.objects.filter(superseded_by__isnull=True, expiry_date__isnull=False,
                                         employee__status__in=(Employee.Status.ACTIVE, Employee.Status.ON_NOTICE))
    if entity is not None:
        qs = qs.filter(entity=entity)
    for doc in qs.select_related("employee", "entity"):
        with transaction.atomic():
            doc = EmployeeDocument.objects.select_for_update().select_related("employee", "entity").get(pk=doc.pk)
            thresholds = sorted(_settings(doc.entity).alert_days or [], reverse=True)
            reached = [t for t in thresholds if today >= doc.expiry_date - timedelta(days=t)
                       and str(t) not in (doc.alerts_sent or {})]
            if not reached:
                continue
            t = min(reached)
            label = DOC_LABELS[doc.doc_type]
            en_label, _, ar_label = str(label).rpartition(" / ")
            code = doc.employee.employee_code
            if t == 0 or doc.expiry_date < today:
                en = f"{code}: {en_label} expired on {doc.expiry_date}."
                ar = f"{code}: انتهت صلاحية {ar_label} بتاريخ {doc.expiry_date}."
            else:
                en = f"{code}: {en_label} expires on {doc.expiry_date} (in {(doc.expiry_date - today).days} days)."
                ar = f"{code}: تنتهي صلاحية {ar_label} بتاريخ {doc.expiry_date} (بعد {(doc.expiry_date - today).days} يومًا)."
            _alert(doc.entity, _recipients(doc.entity, "hr.receive_alerts"), HrAlert.Kind.DOCUMENT_EXPIRY, en, ar)
            stamp = timezone.now().isoformat()
            doc.alerts_sent = {**(doc.alerts_sent or {}), **{str(x): stamp for x in thresholds if x >= t}}
            _save(doc, update_fields=["alerts_sent"])
            sent += 1
    return sent


def _deliver_after_commit(event_id):
    """R25: deliver right after the HR end commits; a billing error never reaches the HR caller."""
    try:
        deliver_assignment_events(event_ids=[event_id])
    except Exception:  # noqa: BLE001 - the job retries
        pass


def deliver_assignment_events(*, entity=None, event_ids=None):
    """``hr_deliver_assignment_events`` (every 5 minutes, R25). Each pending event, in order per assignment, calls
    the billing port's ``end_secondment(..., source="hr_event")`` with the HR user who ended it. A delivered event is
    never sent again; a billing error leaves the event pending with the error (en/ar) and is retried; after
    ``HR_ASSIGNMENT_EVENT_MAX_ATTEMPTS`` failures it is ``failed`` and the entity's accountants and HR officers are
    alerted. No billing link → delivered with nothing to do. Returns the number of events delivered."""
    max_attempts = int(getattr(settings, "HR_ASSIGNMENT_EVENT_MAX_ATTEMPTS", 5))
    qs = AssignmentEvent.objects.filter(delivery_status=AssignmentEvent.Delivery.PENDING)
    if entity is not None:
        qs = qs.filter(entity=entity)
    if event_ids is not None:
        qs = qs.filter(pk__in=event_ids)
    delivered = 0
    for event_id in list(qs.order_by("id").values_list("id", flat=True)):
        with transaction.atomic():
            ev = (AssignmentEvent.objects.select_for_update(skip_locked=True, of=("self",))
                  .select_related("assignment", "entity", "actor").filter(pk=event_id,
                  delivery_status=AssignmentEvent.Delivery.PENDING).first())
            if ev is None:
                continue
            earlier = AssignmentEvent.objects.filter(assignment=ev.assignment, pk__lt=ev.pk).exclude(
                delivery_status__in=AssignmentEvent.DONE)
            if earlier.exists():
                continue  # keep the order per assignment
            port = billing_port.get_port()
            fields = ["delivery_status", "attempts", "last_error_en", "last_error_ar", "billing_assignment_id",
                      "delivered_at"]
            try:
                with transaction.atomic():
                    link = port.linked_assignment(ev.entity, ev.assignment.reference)
                    if link is not None:
                        port.end_secondment(ev.entity, link, ev.end_date, ev.actor, "hr_event")
                        ev.billing_assignment_id = str(port.assignment_id(link))
            except Exception as exc:  # noqa: BLE001 - any billing failure is retried (R25)
                ev.attempts += 1
                ev.last_error_en = f"Billing did not accept the end date: {exc}"[:2000]
                ev.last_error_ar = f"لم تقبل الفوترة تاريخ النهاية: {exc}"[:2000]
                if ev.attempts >= max_attempts:
                    ev.delivery_status = AssignmentEvent.Delivery.FAILED
                    ref = ev.assignment.reference
                    blocked_en, blocked_ar = _blocked_text(ev)
                    _alert(ev.entity, _recipients(ev.entity, "hr.receive_alerts", roles=("accountant",)),
                           HrAlert.Kind.BILLING_DELIVERY_FAILED,
                           f"{ref}: event #{ev.pk}, the secondment end date {ev.end_date}, could not be delivered to "
                           f"billing after {ev.attempts} attempts; fix billing and re-queue it, or record it in billing "
                           f"and supersede it.{blocked_en}",
                           f"{ref}: تعذر تسليم الحدث #{ev.pk} (تاريخ نهاية الإعارة {ev.end_date}) إلى الفوترة بعد "
                           f"{ev.attempts} محاولات؛ أصلح الفوترة وأعده إلى الانتظار، أو سجّله في الفوترة واستبدله."
                           f"{blocked_ar}")
                ev.save(update_fields=fields)
                continue
            ev.attempts += 1
            ev.delivery_status, ev.delivered_at = AssignmentEvent.Delivery.DELIVERED, timezone.now()
            ev.last_error_en = ev.last_error_ar = ""
            ev.save(update_fields=fields)
            delivered += 1
    return delivered


def accrue_leave(year, month, *, entity=None):
    """``hr_accrue_leave`` (month end, R30): one accrual row per ksa employee and month, idempotent (a month already
    accrued, also by a termination approval, is skipped). Local entity employees are skipped while their
    jurisdiction has no parameters (OQ-26); remuneration staff have no leave. R7 (finding 3): with
    ``HR_REQUIRE_VERIFIED_PARAMETERS`` an employee whose accrual reads an unverified parameter is skipped and reported
    (``skipped``: employee code and the bilingual reason naming the rows); the rows used are marked used."""
    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    period = _period(month_start)
    report = {"accrued": 0, "skipped": []}
    qs = Employee.objects.exclude(status=Employee.Status.DRAFT).exclude(employment_basis=Basis.REMUNERATION)
    if entity is not None:
        qs = qs.filter(entity=entity)
    for emp_id in list(qs.order_by("pk").values_list("pk", flat=True)):
        with transaction.atomic():
            emp = _lock(Employee, emp_id)
            lt = LeaveType.objects.filter(entity=emp.entity, code="ANNUAL").first()
            if lt is None or _live_accruals(emp, lt, period).exists():
                continue
            use = ParamUse()
            try:
                jurisdiction = _jurisdiction(emp)
                days = _month_accrual(emp, month_start, month_end, use, jurisdiction)
                if days is None:
                    continue
                use.check_verified()
            except HrError as exc:
                report["skipped"].append((emp.employee_code, exc.messages[0]))
                continue
            try:
                with transaction.atomic():
                    LeaveLedger.objects.create(entity=emp.entity, employee=emp, leave_type=lt, date=month_end,
                                               kind=LeaveLedger.Kind.ACCRUAL, days=days, period=period,
                                               source_type="hr_accrue_leave")
            except IntegrityError:
                continue  # another run wrote it
            use.mark_used()
            report["accrued"] += 1
    return report


def refresh_statuses(*, today=None):
    """``hr_refresh_status`` (daily): employees whose approved contract started become active (R8); employees on
    notice past their last working day become terminated (R38); approved assignments that started become active
    (R23)."""
    today = today or _today()
    changed = 0
    for emp in Employee.objects.filter(status=Employee.Status.DRAFT):
        with transaction.atomic():
            if _activation_problem(emp, today) is None:  # R8, R61 (Qiwa reference)
                e = _lock(Employee, emp.pk)
                e.status = Employee.Status.ACTIVE
                _save(e, update_fields=["status"])
                changed += 1
    for t in Termination.objects.filter(status=TS.APPROVED, employee__status=Employee.Status.ON_NOTICE,
                                        last_working_day__lt=today):
        with transaction.atomic():
            e = _lock(Employee, t.employee_id)
            e.status = Employee.Status.TERMINATED
            _save(e, update_fields=["status"])
            changed += 1
    for a in Assignment.objects.filter(status=ASt.APPROVED, start_date__lte=today):
        with transaction.atomic():
            a = _lock(Assignment, a.pk)
            a.status = ASt.ACTIVE
            _save(a, update_fields=["status"])
            changed += 1
    return changed
