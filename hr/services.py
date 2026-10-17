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
from django.db.models import Max, Q, Sum
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
    ZERO,
    Assignment,
    AssignmentEvent,
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
    HrError,
    HrSequence,
    HrSettings,
    LeaveAdjustment,
    LeaveLedger,
    LeaveRequest,
    LeaveType,
    SalaryComponent,
    SickLeaveDay,
    Source,
    StatutoryParameter,
    Termination,
    error,
)

MASK = "*** / مخفي"
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


@dataclass
class ParamUse:
    """The parameter rows one calculation or approval read (R7)."""

    rows: dict = field(default_factory=dict)

    def get(self, jurisdiction, code, on, what_en="", what_ar=""):
        row = param(jurisdiction, code, on, required=False)
        if row is None:
            raise _not_configured(jurisdiction, code, on, what_en, what_ar)
        self.rows[row.pk] = row
        return row.value

    @property
    def ids(self):
        return sorted(self.rows)

    def check_verified(self):
        if not getattr(settings, "HR_REQUIRE_VERIFIED_PARAMETERS", True):
            return
        bad = [r for r in self.rows.values() if r.status != StatutoryParameter.Status.VERIFIED]
        if bad:
            names = ", ".join(f"{r.jurisdiction} {r.code} ({r.valid_from})" for r in bad)
            raise error(f"This approval uses unverified statutory parameters: {names}. They must be verified first (R7).",
                        f"يستخدم هذا الاعتماد معايير نظامية غير متحقق منها: {names}. يجب التحقق منها أولًا.",
                        code="parameter_unverified")

    def mark_used(self):
        now = timezone.now()
        for row in self.rows.values():
            stored = StatutoryParameter.objects.select_for_update().get(pk=row.pk)
            if stored.used_at is None:
                stored.used_at = now
                stored.save(update_fields=["used_at"])


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
def supersede_statutory_parameter(row, *, user):
    """R7: a used row is corrected by superseding it and entering a replacement."""
    group = _group_parent(user, "hr.maintain_statutory_parameter")
    access.require(user, group, "hr.maintain_statutory_parameter")
    row = StatutoryParameter.objects.select_for_update().get(pk=row.pk)
    row.status = StatutoryParameter.Status.SUPERSEDED
    row.save()
    return row


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
    """Service years from ``start`` up to (not including) ``end_exclusive``. Gregorian: days / the parameter's
    Gregorian divisor (AT-21). Hijri: completed Umm al-Qura years plus the remaining days / the parameter's Hijri
    divisor (OQ-36 default: whole Hijri years + days/354)."""
    days = (end_exclusive - start).days
    if basis == EmploymentContract.CalendarBasis.GREGORIAN or days <= 0:
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
    "country_of_residence",
})


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
        # warning only [CC-verify].
        row = param("SA", "id.format", emp.hire_date or _today(), required=False)
        fmt = (row.value if row else {}).get(id_type)
        if fmt and not (number.isdigit() and len(number) == int(fmt["length"]) and number.startswith(fmt["prefix"])):
            warnings.append(f"ID number does not match the expected {id_type} format ({fmt['length']} digits starting "
                            f"{fmt['prefix']}; unverified rule). / رقم الهوية لا يطابق الصيغة المتوقعة "
                            f"({fmt['length']} أرقام تبدأ بـ {fmt['prefix']}؛ قاعدة غير متحقق منها).")
    if emp.gender not in Employee.Gender.values:
        problems.append("Gender is required (male or female). / الجنس مطلوب (ذكر أو أنثى).")
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
    emp.id_number_hash = crypto.keyed_hash(emp.id_number)
    emp.warnings = warnings
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
    return _save_employee(emp, s)


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
    for name, value in _employee_values(fields).items():
        setattr(emp, name, value)
    emp.modified_by, emp.modified_at = user, timezone.now()
    return _save_employee(emp, _settings(emp.entity))


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
    """Employees ``user`` may see (R2): scoped by ``hr.view``. Personal data is still masked (use employee_record)."""
    qs = access.scope(Employee.objects.all(), user, "hr.view")
    return qs.filter(entity=entity) if entity is not None else qs


def get_employee(user, pk):
    """One employee through scope: another entity's employee is 404 (R2, AT-1)."""
    return access.get_scoped_or_404(Employee.objects.all(), user, "hr.view", pk=pk)


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
        "hire_date": emp.hire_date, "gosi_number": emp.gosi_number,
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
    "effective_date", "contract_type", "start_date", "end_date", "calendar_basis", "calendar_basis_ref", "probation_periods", "job_title_en",
    "job_title_ar", "occupation_code", "weekly_hours", "daily_hours", "annual_leave_days", "notice_days_employer",
    "notice_days_employee", "currency", "department", "grade", "qiwa_contract_ref", "suspension_agreement_ref",
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
        SickLeaveDay.objects.filter(employee=contract.employee, leave_request__status__in=("approved", "taken"))
        .values_list("date", flat=True))
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
            if earlier is not None and _add_months(earlier.last_working_day, months) > contract.start_date:
                same_job = (earlier.contract.job_title_ar.strip() == contract.job_title_ar.strip()
                            or earlier.contract.job_title_en.strip().lower() == contract.job_title_en.strip().lower())
                if same_job:
                    problems.append(f"This person was employed here within {months} months in the same job: no new "
                                    "probation (Art. 54). / عمل هذا الشخص لدى المنشأة نفسها في الوظيفة ذاتها خلال "
                                    f"{months} أشهر: لا تجوز فترة تجربة جديدة.")
    # R16 as corrected (Art. 37, Regs Art. 12): a non-Saudi's contract is always fixed-term.
    if contract.contract_type == EmploymentContract.ContractType.UNLIMITED and not emp.is_saudi:
        problems.append("A non-Saudi's contract must be fixed-term; an open-ended contract is refused (Art. 37, Regs "
                        "Art. 12). / يجب أن يكون عقد غير السعودي محدد المدة؛ ويُرفض العقد غير محدد المدة.")
    if contract.contract_type == EmploymentContract.ContractType.FIXED and emp.is_saudi and contract.end_date:
        rule = use.get(jurisdiction, "contract.saudi_open_ended", on)
        fixed = list(_approved_versions(emp).filter(contract_type="fixed").order_by("version_no"))
        ends = {c.end_date for c in fixed if c.end_date} | {contract.end_date}
        first_start = min([c.start_date for c in fixed] + [contract.start_date])
        year_days = _year_days(use, jurisdiction, contract.calendar_basis, on)
        limit = _anniversary(first_start, int(rule["years"]), contract.calendar_basis, year_days)
        if len(ends) - 1 >= int(rule["renewals"]) or limit <= contract.end_date:
            warnings.append("This fixed-term contract reaches the renewal or four-year limit: it becomes open-ended for "
                            "a Saudi employee (Art. 55). / يبلغ هذا العقد حد التجديد أو السنوات الأربع: يصبح غير "
                            "محدد المدة للموظف السعودي.")
    # R19.
    max_daily = _decimal(use.get(jurisdiction, "hours.max_daily", on)["hours"], "hours")
    max_weekly = _decimal(use.get(jurisdiction, "hours.max_weekly", on)["hours"], "hours")
    if contract.daily_hours > max_daily or contract.weekly_hours > max_weekly:
        problems.append(f"Working hours exceed {max_daily} a day or {max_weekly} a week (Art. 98). / "
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
        problems.append("The Qiwa contract reference is required: contracts are documented on the Ministry's platform "
                        "(Regs Art. 18). / مرجع العقد في منصة قوى مطلوب: توثَّق العقود إلكترونيًا عبر منصة الوزارة.")
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


def _activate_if_started(emp):
    emp = _lock(Employee, emp.pk)
    if emp.status == Employee.Status.DRAFT and _terms_on(emp, _today()) is not None:
        first = _approved_versions(emp).order_by("version_no").first()
        if first.start_date <= _today():
            emp.status = Employee.Status.ACTIVE
            _save(emp, update_fields=["status"])
    return emp


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


def _emit(a, kind, end_date, previous, user):
    event = AssignmentEvent.objects.create(entity=a.entity, assignment=a, kind=kind, end_date=end_date,
                                           previous_end_date=previous, actor=user)
    transaction.on_commit(lambda: _deliver_after_commit(event.pk))
    return event


def _end_assignment(a, end_date, reason_ar, reason_en, user):
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
    return a, _emit(a, kind, end_date, previous, user)


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
        # Leave already taken or returned counts whatever its date; accruals only up to ``on``.
        qs = qs.filter(Q(date__lte=on) | Q(kind__in=(LeaveLedger.Kind.TAKEN, LeaveLedger.Kind.REVERSAL)))
    return qs.aggregate(s=Sum("days"))["s"] or ZERO


def leave_balance(employee, leave_type_code, *, user, on=None):
    """R29: Σ ledger days per (employee, leave type)."""
    access.require(user, employee.entity, "hr.view")
    return _balance(employee, _leave_type(employee.entity, leave_type_code), on)


def _count_days(use, jurisdiction, req, rule, start, end):
    """Days of a request: calendar or working days per the type's parameter; official holidays inside annual
    leave extend it (Regs Art. 24)."""
    count = rule.get("count", "calendar")
    skip = set()
    if count == "working":
        weekend = _weekend(req.entity)
        skip |= {d for d in _days(start, end) if d.weekday() in weekend}
    if req.leave_type.code == "ANNUAL":
        skip |= _holidays(use, jurisdiction, start, end)
    return sum(1 for d in _days(start, end) if d not in skip)


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
    prior = set(SickLeaveDay.objects.filter(employee=req.employee, leave_request__status__in=(LR.APPROVED, LR.TAKEN))
                .exclude(leave_request_id=req.pk).values_list("date", flat=True))
    new = list(_days(req.start_date, req.end_date))
    cycles = _sick_cycles(prior | set(new), terms.calendar_basis)
    bounds, acc = [], 0
    for i, tier in enumerate(rule["tiers"], start=1):
        acc += int(tier["days"])
        bounds.append((acc, i, _decimal(tier["pay_pct"], "pay_pct")))
    plan, beyond = [], 0
    for d in new:
        cycle_start, n = cycles[d]
        tier = next((t for t in bounds if n < t[0]), None)
        if tier is None:
            beyond += 1
        else:
            plan.append((d, cycle_start, tier[1], tier[2]))
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
    return problems


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


@transaction.atomic
def request_leave(employee, leave_type, *, user, start_date, end_date, event_date=None, document_ref="", reason_ar="",
                  reason_en="", declarations=None):
    """Record a leave request on the employee's behalf (``hr.request_leave``, OQ-4) and submit it."""
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.request_leave")
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
    ``taken`` row; sick leave its ``SickLeaveDay`` tiers (R32) and suspends overlapping annual leave."""
    req = _lock(LeaveRequest, leave_request.pk)
    access.require(user, req.entity, "hr.approve_leave", document=req)
    _deny_own(user, req.entity, "hr.approve_leave", req.employee)
    if req.status != LR.SUBMITTED:
        raise error("Only submitted requests are approved.", "تُعتمد الطلبات المقدمة فقط.", code="not_submitted")
    use = ParamUse()
    problems = _leave_problems(req, use)
    if problems:
        raise _problems(problems, "leave_invalid")
    use.check_verified()
    use.mark_used()
    req.status, req.approved_by, req.approved_at = LR.APPROVED, user, timezone.now()
    _save(req)
    jurisdiction = _jurisdiction(req.employee)
    if req.leave_type.code == "ANNUAL":
        LeaveLedger.objects.create(entity=req.entity, employee=req.employee, leave_type=req.leave_type,
                                   date=req.start_date, kind=LeaveLedger.Kind.TAKEN, days=-req.days,
                                   source_type="hr.leaverequest", source_id=req.pk, created_by=user)
    elif req.leave_type.code == "SICK":
        terms = _terms_on(req.employee, req.start_date)
        plan, _, _ = _sick_plan(use, jurisdiction, req, terms)
        for d, cycle_start, tier, pct in plan:
            SickLeaveDay.objects.create(entity=req.entity, employee=req.employee, date=d, leave_request=req,
                                        cycle_start=cycle_start, tier_no=tier, pay_pct=pct)
        _suspend_annual_for_sick(req, use, jurisdiction, user)
    return req


def _suspend_annual_for_sick(sick, use, jurisdiction, user):
    """Regs Art. 26: approved sick leave inside annual leave suspends it; the overlapping annual days go back."""
    annual = LeaveRequest.objects.filter(employee=sick.employee, leave_type__code="ANNUAL", status=LR.APPROVED,
                                         start_date__lte=sick.end_date, end_date__gte=sick.start_date)
    for a in annual:
        start, end = max(a.start_date, sick.start_date), min(a.end_date, sick.end_date)
        days = Decimal(_count_days(use, jurisdiction, a, {}, start, end))
        if days:
            LeaveLedger.objects.create(entity=a.entity, employee=a.employee, leave_type=a.leave_type, date=start,
                                       kind=LeaveLedger.Kind.REVERSAL, days=days, source_type="hr.leaverequest",
                                       source_id=sick.pk, created_by=user)


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


def _cancel_leave(req, user, from_day):
    """Cancel an approved request from ``from_day`` on: the unused annual days go back (R31, R35)."""
    use = ParamUse()
    jurisdiction = _jurisdiction(req.employee)
    if req.leave_type.code == "ANNUAL":
        start = max(req.start_date, from_day)
        if start <= req.end_date:
            days = Decimal(_count_days(use, jurisdiction, req, {}, start, req.end_date))
            if days:
                LeaveLedger.objects.create(entity=req.entity, employee=req.employee, leave_type=req.leave_type,
                                           date=start, kind=LeaveLedger.Kind.REVERSAL, days=days,
                                           source_type="hr.leaverequest", source_id=req.pk, created_by=user)
    if from_day <= req.start_date:
        req.status = LR.CANCELLED
    else:
        req.end_date = from_day - timedelta(days=1)
        req.days = Decimal(_count_days(use, jurisdiction, req, {}, req.start_date, req.end_date))
    return _save(req)


@transaction.atomic
def cancel_leave(leave_request, *, user):
    """Cancel an approved leave from today on (unused days return). Sick leave is cancelled only before it starts:
    its tier rows are then ignored (they are append-only)."""
    req = _lock(LeaveRequest, leave_request.pk)
    access.require(user, req.entity, "hr.request_leave")
    if req.status != LR.APPROVED:
        raise error("Only approved leave is cancelled.", "تُلغى الإجازات المعتمدة فقط.", code="leave_status")
    if req.leave_type.code == "SICK" and req.start_date <= _today():
        raise error("Started sick leave is not cancelled; record an adjustment instead.",
                    "لا تُلغى الإجازة المرضية بعد بدئها؛ سجّل تسوية بدلًا من ذلك.", code="leave_status")
    return _cancel_leave(req, user, max(_today(), req.start_date))


@transaction.atomic
def record_opening_leave_balance(employee, leave_type, days, on, *, user):
    """Opening balance from a migration (``source=import``): only before any other row of that employee and type,
    and while the employee is still a draft (afterwards corrections are approved adjustments, R29)."""
    emp = _lock(Employee, employee.pk)
    access.require(user, emp.entity, "hr.manage_employee")
    lt = _leave_type(emp.entity, leave_type)
    if emp.status != Employee.Status.DRAFT or LeaveLedger.objects.filter(employee=emp, leave_type=lt).exists():
        raise error("Opening balances are loaded once, before the employee is active (R29).",
                    "يُحمَّل الرصيد الافتتاحي مرة واحدة قبل تفعيل الموظف.", code="opening_once")
    return LeaveLedger.objects.create(entity=emp.entity, employee=emp, leave_type=lt, date=on,
                                      kind=LeaveLedger.Kind.OPENING, days=_decimal(days, "days"), created_by=user,
                                      source_type="import")


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
def approve_leave_adjustment(adjustment, *, user):
    """R29: the approver (``hr.adjust_leave_balance``, A) approves before the ledger row exists. R56: no adjustment
    gives more than 30 days of annual leave for one service year."""
    adj = _lock(LeaveAdjustment, adjustment.pk)
    access.require(user, adj.entity, "hr.adjust_leave_balance", document=adj)
    _deny_own(user, adj.entity, "hr.adjust_leave_balance", adj.employee)
    if adj.status != LeaveAdjustment.Status.DRAFT:
        raise error("Only draft adjustments are approved.", "تُعتمد التسويات المسودة فقط.", code="not_draft")
    emp = adj.employee
    if adj.leave_type.code == "ANNUAL" and adj.days > 0 and emp.employment_basis == Basis.KSA_EMPLOYEE:
        use = ParamUse()
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
    adj.status, adj.approved_by, adj.approved_at = LeaveAdjustment.Status.APPROVED, user, timezone.now()
    _save(adj)
    LeaveLedger.objects.create(entity=adj.entity, employee=emp, leave_type=adj.leave_type, date=adj.date,
                               kind=LeaveLedger.Kind.ADJUSTMENT, days=adj.days, source_type="hr.leaveadjustment",
                               source_id=adj.pk, created_by=user)
    return adj


def _unpaid_days_of(r, start, end):
    """The unpaid days of one approved request between start and end."""
    if r.unpaid or r.leave_type.paid_rule == LeaveType.PaidRule.UNPAID:
        return set(_days(max(r.start_date, start), min(r.end_date, end)))
    return set()


def _unpaid_days(emp, start, end):
    """Approved unpaid leave days of ``emp`` between start and end (R34)."""
    result = set()
    for r in LeaveRequest.objects.filter(employee=emp, status__in=(LR.APPROVED, LR.TAKEN), start_date__lte=end,
                                         end_date__gte=start).select_related("leave_type"):
        result |= _unpaid_days_of(r, start, end)
    return result


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


def _month_accrual(emp, month_start, month_end, use, jurisdiction):
    """R30, R56 with the compliance correction: per day, entitlement = min(30, max(contract days, statutory days)),
    the 30-day statutory rate from the day after the 5-year anniversary; accrual = Σ entitlement / 12 / days in the
    month; unpaid days beyond 20 in a work year accrue nothing."""
    end_of_service = None
    term = Termination.objects.filter(employee=emp, status=Termination.Status.APPROVED).order_by("-id").first()
    if term is not None:
        end_of_service = term.last_working_day
    first = _approved_versions(emp).order_by("version_no").first()
    if first is None:
        return None
    start = max(month_start, emp.hire_date, first.start_date)
    end = min(month_end, end_of_service or month_end)
    if start > end:
        return None
    value = use.get(jurisdiction, "leave.annual", month_start)
    basis = (_terms_on(emp, end) or first).calendar_basis
    suspended = _suspended_days(use, jurisdiction, emp, basis, start, end)
    _year_days(use, jurisdiction, basis, month_start)  # the calendar basis is configured
    tiers = [(int(Decimal(t["from_years"])), int(Decimal(t["days"]))) for t in value["tiers"]]
    anniversaries = {years: _service_anniversary(use, jurisdiction, emp, years, basis) for years, _ in tiers}
    total = 0
    for d in _days(start, end):
        if d in suspended:
            continue
        terms = _terms_on(emp, d)
        if terms is None:
            continue
        statutory = max(days for years, days in tiers if years == 0 or d > anniversaries[years])
        total += min(int(ANNUAL_LEAVE_CEILING), max(terms.annual_leave_days, statutory))
    days_in_month = (month_end - month_start).days + 1
    return _frac_round(Fraction(total, 12 * days_in_month))


# =====================================================================================================
# Termination and end-of-service (R27, R35 to R41, R52)
# =====================================================================================================

TS = Termination.Status
TERMINATION_FIELDS = frozenset({"kind", "last_working_day", "reason_ar", "reason_en", "notice_given_on", "submitted_on",
                                "postponed_until", "postponement_reason_ar", "written_consent_ref", "article_ref"})


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
    fixed = contract.contract_type == EmploymentContract.ContractType.FIXED
    if term.kind == TKind.RESIGNATION and not fixed:
        problems.append("A resignation ends a fixed-term contract; an open-ended contract is ended by the employee "
                        "with notice (Art. 75). / الاستقالة لإنهاء العقد محدد المدة؛ أما غير محدد المدة فيُنهيه الموظف "
                        "بإشعار وفق المادة 75.")
    if term.kind == TKind.EMPLOYEE_NOTICE_ART75 and fixed:
        problems.append("Art. 75 notice ends an open-ended contract; use a resignation (Art. 79 bis). / "
                        "الإشعار وفق المادة 75 لإنهاء العقد غير محدد المدة؛ استخدم الاستقالة.")
    if term.kind == TKind.MUTUAL_AGREEMENT and not term.written_consent_ref:
        problems.append("Mutual agreement needs a reference to the employee's written consent (Art. 74(1)). / "
                        "يلزم لاتفاق الطرفين مرجع الموافقة الكتابية للموظف.")
    if term.kind == TKind.RESIGNATION:
        if term.submitted_on is None:
            problems.append("A resignation needs the date it was submitted (Art. 79 bis). / يلزم تاريخ تقديم الاستقالة.")
    if term.last_working_day and term.last_working_day < contract.start_date:
        problems.append("The last working day is on or after the contract start (R36). / آخر يوم عمل في تاريخ بداية العقد أو بعده.")
    if term.kind == TKind.PROBATION_TERMINATION:
        end = _probation_end(use, contract, jurisdiction)
        if end is None or term.last_working_day > end:
            problems.append("The probation period has ended or there is none (Art. 54). / انتهت فترة التجربة أو لا توجد.")
    # Notice (R37): open-ended contracts only; a shortfall is a warning and a payroll input.
    if not fixed and term.kind in (TKind.EMPLOYER_TERMINATION, TKind.EMPLOYEE_NOTICE_ART75):
        notice = use.get(jurisdiction, "notice.min_days", term.last_working_day)
        required = int(notice["employer" if term.kind == TKind.EMPLOYER_TERMINATION else "employee"])
        given = (term.last_working_day - term.notice_given_on).days if term.notice_given_on else 0
        term.notice_shortfall_days = max(0, required - given)
        if term.notice_shortfall_days:
            warnings.append(f"Notice is {term.notice_shortfall_days} day(s) short of {required} days: notice pay is a "
                            f"payroll input (Art. 75, 76). / مدة الإشعار تنقص {term.notice_shortfall_days} يومًا عن "
                            f"{required} يومًا: بدل الإشعار من مدخلات الرواتب.")
    if term.kind in (TKind.EMPLOYER_TERMINATION, TKind.ART80_DISMISSAL):
        lwd = term.last_working_day
        if LeaveRequest.objects.filter(employee=emp, leave_type__code="SICK", status__in=(LR.APPROVED, LR.TAKEN),
                                       start_date__lte=lwd, end_date__gte=lwd - timedelta(days=1)).exists():
            warnings.append("The employee is on sick leave: no dismissal for illness before the sick-leave periods are "
                            "used up (Art. 82). / الموظف في إجازة مرضية: لا يجوز الفصل بسبب المرض قبل استنفاد مدد الإجازة "
                            "المرضية.")
        if LeaveRequest.objects.filter(employee=emp, leave_type__code__startswith="MATERNITY",
                                       status__in=(LR.APPROVED, LR.TAKEN), start_date__lte=lwd, end_date__gte=lwd).exists():
            warnings.append("The employee is on maternity leave: no dismissal or notice during it (Art. 155). / "
                            "الموظفة في إجازة وضع: لا يجوز فصلها أو إشعارها خلالها.")
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
    wage = sum(components.values(), ZERO) + _decimal(additional_wage, "additional_wage")
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
        computed_by=user, supersedes=supersedes)


def _settlement_input(term, eosb, user, use):
    """R35, payroll R34: annual balance at the last working day, daily wage of EOSB components / 30, notice
    shortfall. Remuneration: no EOSB, leave or GOSI lines (R52)."""
    emp = term.employee
    terms = term.contract
    statutory = emp.employment_basis != Basis.REMUNERATION
    balance, daily, encash = ZERO, ZERO, ZERO
    if statutory:
        jurisdiction = _jurisdiction(emp)
        lt = LeaveType.objects.filter(entity=emp.entity, code="ANNUAL").first()
        balance = _balance(emp, lt, term.last_working_day) if lt else ZERO
        divisor = int(use.get(jurisdiction, "leave.encashment", term.last_working_day)["day_divisor"])
        wage = sum((c.monthly_amount for c in terms.components.select_related("component") if c.component.counts_for_eosb), ZERO)
        daily = _round(Decimal(wage) / divisor)
        encash = _frac_round(Fraction(max(balance, ZERO)) * Fraction(wage) / divisor)
    si = FinalSettlementInput.objects.create(
        entity=term.entity, employee=emp, termination=term, eosb_calculation=eosb, last_working_day=term.last_working_day,
        currency_id=terms.currency_id, eosb_award=eosb.award if eosb else ZERO, leave_balance_days=max(balance, ZERO),
        daily_wage=daily, leave_encashment=encash, notice_shortfall_days=term.notice_shortfall_days,
        includes_statutory_lines=statutory, created_by=user)
    payroll_port.get_port().create_draft(si, user)
    return si


@transaction.atomic
def approve_termination(termination, *, user):
    """R38: approval in one transaction: termination approved; employee terminated (or on notice until the last
    working day); contract ended; active assignments ended at the last working day (events, R27); leave after it
    cancelled and returned (R35); an EOSB snapshot (not for remuneration staff, R52); settlement inputs handed to
    payroll. Any failure rolls everything back."""
    t = _lock(Termination, termination.pk)
    access.require(user, t.entity, "hr.approve_termination", document=t)
    emp = _lock(Employee, t.employee_id)
    _deny_own(user, t.entity, "hr.approve_termination", emp)
    if t.status != TS.SUBMITTED:
        raise error("Only submitted terminations are approved.", "يُعتمد إنهاء الخدمة المقدم فقط.", code="not_submitted")
    use = ParamUse()
    if t.kind == TKind.RESIGNATION and emp.employment_basis == Basis.KSA_EMPLOYEE:
        t.accepted_on = min(_today(), t.submitted_on + timedelta(
            days=int(use.get("SA", "termination.resignation", t.submitted_on)["deemed_acceptance_days"])))
        t.last_working_day, problems = _resignation_dates(t, use, "SA", accepted_on=t.accepted_on)
        if problems:
            raise _problems(problems, "termination_invalid")
        t.contract = _terms_on(emp, t.last_working_day) or t.contract
    problems, warnings = _termination_problems(t, use)
    if problems:
        raise _problems(problems, "termination_invalid")
    eosb_result = None
    if emp.employment_basis != Basis.REMUNERATION:
        eosb_result = _compute_eosb(emp, t.last_working_day, t.kind, use)
    use.check_verified()
    use.mark_used()
    lwd = t.last_working_day
    t.status, t.approved_by, t.approved_at, t.warnings = TS.APPROVED, user, timezone.now(), warnings
    _save(t)
    emp.status = Employee.Status.TERMINATED if lwd < _today() else Employee.Status.ON_NOTICE
    _save(emp, update_fields=["status"])
    for c in _approved_versions(emp).filter(ended_on__isnull=True):
        c.ended_on = lwd
        _save(c, update_fields=["ended_on"])
    for a in Assignment.objects.filter(employee=emp, status__in=(ASt.APPROVED, ASt.ACTIVE, ASt.ENDED)):
        a = _lock(Assignment, a.pk)
        if a.start_date > lwd:
            _cancel_assignment_for_termination(a, t, user)
        elif a.end_date is None or a.end_date > lwd:
            _end_assignment(a, lwd, t.reason_ar, t.reason_en, user)
    for r in LeaveRequest.objects.filter(employee=emp, status=LR.APPROVED, end_date__gt=lwd):
        _cancel_leave(_lock(LeaveRequest, r.pk), user, lwd + timedelta(days=1))
    for r in LeaveRequest.objects.filter(employee=emp, status__in=(LR.DRAFT, LR.SUBMITTED), end_date__gt=lwd):
        r.status = LR.CANCELLED
        _save(r)
    eosb = _snapshot(eosb_result, t, user) if eosb_result else None
    _settlement_input(t, eosb, user, use)
    return t


def _cancel_assignment_for_termination(a, t, user):
    previous = a.end_date
    a.status, a.end_reason_ar, a.ended_by, a.ended_at = ASt.CANCELLED, t.reason_ar, user, timezone.now()
    a.end_date = a.start_date - timedelta(days=1)
    _save(a)
    _emit(a, AssignmentEvent.Kind.CANCELLED, a.end_date, previous, user)


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
    for ev in AssignmentEvent.objects.filter(assignment__employee=emp, actor=t.approved_by,
                                             created_at__gte=t.approved_at).select_related("assignment").order_by("-id"):
        a = _lock(Assignment, ev.assignment_id)
        if ev.kind == AssignmentEvent.Kind.CANCELLED:
            a.status, a.end_date = ASt.APPROVED, ev.previous_end_date
            _save(a)
            warnings.append(f"{a.reference}: cancellation reversed in HR; billing must reopen it (OQ-16). / "
                            f"{a.reference}: أُلغي الإلغاء في الموارد البشرية؛ ويجب إعادة فتحه في الفوترة.")
        elif ev.previous_end_date is not None:
            a.end_date = ev.previous_end_date
            _save(a)
            _emit(a, AssignmentEvent.Kind.END_CHANGED, ev.previous_end_date, ev.end_date, user)
        else:
            a.status, a.end_date = (ASt.ACTIVE if a.start_date <= _today() else ASt.APPROVED), None
            _save(a)
            warnings.append(f"{a.reference}: the end date was removed in HR; billing cannot remove an end date, it must "
                            "be reopened there (OQ-16). / " f"{a.reference}: أُزيل تاريخ النهاية في الموارد البشرية؛ "
                            "ولا تزيل الفوترة تاريخ النهاية فيجب إعادة فتحه هناك.")
    for si in FinalSettlementInput.objects.filter(termination=t, status=FinalSettlementInput.Status.DRAFT):
        si.status = FinalSettlementInput.Status.DISCARDED
        si.save(update_fields=["status"])
    payroll_port.get_port().discard_draft(t, user)
    return t, warnings


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
    result = _compute_eosb(t.employee, t.last_working_day, t.kind, use)
    snap = _snapshot(result, t, user, supersedes=latest)
    for si in FinalSettlementInput.objects.filter(termination=t, status=FinalSettlementInput.Status.DRAFT):
        si.status = FinalSettlementInput.Status.SUPERSEDED
        si.save(update_fields=["status"])
    _settlement_input(t, snap, user, use)
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
                delivery_status=AssignmentEvent.Delivery.DELIVERED)
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
                    _alert(ev.entity, _recipients(ev.entity, "hr.receive_alerts", roles=("accountant",)),
                           HrAlert.Kind.BILLING_DELIVERY_FAILED,
                           f"{ref}: the secondment end date {ev.end_date} could not be delivered to billing after "
                           f"{ev.attempts} attempts; record it in billing.",
                           f"{ref}: تعذر تسليم تاريخ نهاية الإعارة {ev.end_date} إلى الفوترة بعد {ev.attempts} "
                           "محاولات؛ سجّله في الفوترة.")
                ev.save(update_fields=fields)
                continue
            ev.attempts += 1
            ev.delivery_status, ev.delivered_at = AssignmentEvent.Delivery.DELIVERED, timezone.now()
            ev.last_error_en = ev.last_error_ar = ""
            ev.save(update_fields=fields)
            delivered += 1
    return delivered


def accrue_leave(year, month, *, entity=None):
    """``hr_accrue_leave`` (month end, R30): one accrual row per ksa employee and month, idempotent. Local entity
    employees are skipped while their jurisdiction has no parameters (OQ-26); remuneration staff have no leave."""
    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    period = f"{year:04d}-{month:02d}"
    report = {"accrued": 0, "skipped": []}
    qs = Employee.objects.exclude(status=Employee.Status.DRAFT).exclude(employment_basis=Basis.REMUNERATION)
    if entity is not None:
        qs = qs.filter(entity=entity)
    for emp in qs.select_related("entity"):
        lt = LeaveType.objects.filter(entity=emp.entity, code="ANNUAL").first()
        if lt is None or LeaveLedger.objects.filter(employee=emp, leave_type=lt, kind=LeaveLedger.Kind.ACCRUAL,
                                                    period=period).exists():
            continue
        use = ParamUse()
        try:
            jurisdiction = _jurisdiction(emp)
            days = _month_accrual(emp, month_start, month_end, use, jurisdiction)
        except HrError as exc:
            report["skipped"].append((emp.employee_code, exc.messages[0]))
            continue
        if days is None:
            continue
        try:
            with transaction.atomic():
                LeaveLedger.objects.create(entity=emp.entity, employee=emp, leave_type=lt, date=month_end,
                                           kind=LeaveLedger.Kind.ACCRUAL, days=days, period=period,
                                           source_type="hr_accrue_leave")
        except IntegrityError:
            continue  # another run wrote it
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
            first = _approved_versions(emp).order_by("version_no").first()
            if first is not None and first.start_date <= today:
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
