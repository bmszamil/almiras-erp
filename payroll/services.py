"""Payroll services, slice 1 (docs/specs/payroll.md): KSA monthly payroll end to end.

Everything that changes payroll data goes through this module. Rules are referenced by number (R1 to R68; R64 to
R68 from the GOSI Implementing Regulations check, docs/compliance/gosi-regulations-2026-10-19.md). Money is Decimal,
SAR 2 dp rounded half-up per line per employee (R5, OQ-25); foreign remuneration amounts carry their currency's own
decimals (R42). Statutory figures come only from ``hr.StatutoryParameter`` rows (hr R6, R7) read through
``hr.services.ParamUse``. Entries post only through ``ledger.services`` with the registered document hooks (R11).
"""
import calendar
import csv
import hashlib
import io
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction

from django.conf import settings as django_settings
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.template.loader import render_to_string
from django.utils import timezone

from core import access
from core.access import services as access_services
from core.access.models import AccessLog, Kind, Membership
from core.models import Currency, Entity
from hr import services as hr
from hr.models import (
    Assignment,
    Employee,
    EmploymentBasis,
    EmploymentContract,
    HrSettings,
    LeaveRequest,
    SickLeaveDay,
    StatutoryParameter,
    Termination,
)
from ledger import fx
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod, JournalLine, LedgerError

from . import approvals, invoicing_port
from .models import (
    DeferredSaleContract,
    DeferredSaleInstallment,
    EmployeeLoan,
    EosbProvisionEntry,
    GosiAbroadPeriod,
    GosiCatchUpNotice,
    GosiReconciliation,
    GosiVariableWageBase,
    LoanInstallment,
    MaternityGosiCase,
    MudadSubmission,
    PayrollAlert,
    PayrollError,
    PayrollGroup,
    PayrollInput,
    PayrollPayment,
    PayrollRun,
    PayrollSequence,
    PayrollSettings,
    Payslip,
    PayslipLine,
    WpsFile,
    error,
)

ZERO = Decimal("0")
CENT = Decimal("0.01")
Basis = EmploymentBasis
LT = PayslipLine.LineType
RS = PayrollRun.Status
IS = PayrollInput.Status
IK = PayrollInput.Kind

# Accounts (§5; approved by the CFO 2026-10-17, P19(g)).
SALARIES = "5100"
GOSI_EXPENSE = "5110"
EOSB_EXPENSE = "5120"
EOSB_PROVISION = "2500"
SALARIES_PAYABLE = "2330"
GOSI_PAYABLE = "2340"
LOANS = "1230"
BAI_AJEL = "1240"
WHT_PAYABLE = "2220"
FX_GAIN = "4700"
FX_LOSS = "5750"

MATERNITY_BIRTHS_FROM = date(2025, 7, 1)  # R61: GOSI maternity compensation for births from July 2025 (default gate)
GCC = hr.GCC
INTEREST_WORDS = ("interest", "fee", "profit", "penalty", "late_fee", "rate", "charge")
SHARIA_NO_CHARGE = ("Loans and bai' ajel are interest-free and never carry a fee, profit increase, penalty or late "
                    "charge, in every entity (R26, R48).",
                    "القروض والبيع الآجل بدون فوائد ولا تُضاف إليها رسوم أو زيادة ربح أو غرامة أو رسوم تأخير، في كل "
                    "الكيانات.")
INTEREST_FREE = ("Interest-free loan (qard hasan)", "قرض حسن بدون فوائد")


def _today():
    """Today in Asia/Riyadh; a function so tests can move the clock."""
    return timezone.localdate()


def _now():
    return timezone.now()


def _round(value):
    with localcontext() as ctx:
        ctx.prec = 60
        if isinstance(value, Fraction):
            value = Decimal(value.numerator) / Decimal(value.denominator)
        return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _round_to(value, places):
    with localcontext() as ctx:
        ctx.prec = 60
        if isinstance(value, Fraction):
            value = Decimal(value.numerator) / Decimal(value.denominator)
        return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _dec(value, what):
    try:
        return fx.to_decimal(value, what)
    except LedgerError as exc:
        raise PayrollError(exc.messages, code="float_rejected" if isinstance(value, float) else "not_a_number") from None


def _save(obj, **kwargs):
    obj._service_write = True
    obj.save(**kwargs)
    return obj


def _lock(model, pk):
    return model._base_manager.select_for_update(of=("self",)).get(pk=pk)


def _human(user, entity, permission):
    """R38 / P3: agents and system users hold no payroll right and never act in payroll."""
    if access_services.kind_of(user) in (Kind.AGENT, Kind.SYSTEM):
        access.deny(user, entity, permission, "agent_forbidden")


def _require(user, entity, permission, **kwargs):
    _human(user, entity, permission)
    return access.require(user, entity, permission, **kwargs)


def _problems(problems, code):
    exc = PayrollError(list(problems), code=code)
    exc.code = code
    return exc


# --- periods ------------------------------------------------------------------------------------

def _check_period(period):
    try:
        year, month = (int(x) for x in str(period).split("-"))
        if len(str(period)) != 7 or not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        raise error(f"A period is written YYYY-MM, not {period!r}.", f"تُكتب الفترة YYYY-MM لا {period!r}.",
                    code="period") from None
    return year, month


def _period_bounds(period):
    year, month = _check_period(period)
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _period_of(day):
    return f"{day.year:04d}-{day.month:02d}"


def _next_period(period, months=1):
    year, month = _check_period(period)
    month += months
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return f"{year:04d}-{month:02d}"


def _cutoff_date(period, group, settings):
    """R44 (P10): in-country inputs approved on or before the 27th of the period go into that period's run;
    out-of-country inputs until the 6th of the following month."""
    start, end = _period_bounds(period)
    if group == PayrollGroup.OUT_OF_COUNTRY:
        ns, ne = _period_bounds(_next_period(period))
        return min(ns.replace(day=1) + timedelta(days=settings.out_of_country_cutoff_day - 1), ne)
    return min(start + timedelta(days=settings.input_cutoff_day - 1), end)


def _payroll_period_for(work_period, approved_on, group, settings):
    """R9, R44: the first period from ``work_period`` whose cut-off is on or after the approval date."""
    period = work_period
    for _ in range(240):
        if approved_on <= _cutoff_date(period, group, settings):
            return period
        period = _next_period(period)
    raise error("Approval date too far after the work period.", "تاريخ الاعتماد بعيد جدًا عن فترة العمل.", code="period")


def _group_of(employee):
    return (PayrollGroup.OUT_OF_COUNTRY if employee.employment_basis == Basis.REMUNERATION
            else PayrollGroup.IN_COUNTRY)


# --- settings (3.1, R3, R4, R10, R21, R51) --------------------------------------------------------

SETTINGS_FIELDS = frozenset({
    "enabled", "default_pay_day", "input_cutoff_day", "out_of_country_cutoff_day", "salary_bank", "day_basis",
    "day_basis_ref", "salary_bank_account", "wps_format", "wps_bank_code", "employer_bank_iban",
    "gosi_oh_rate_override", "gosi_oh_override_ref", "gosi_oh_override_from", "gosi_oh_override_to",
    "gosi_housing_in_kind_exempt_ref",
})


def _account(entity, code):
    acc = Account.objects.filter(entity=entity, code=code).first()
    if acc is None:
        raise error(f"{entity.code} has no account {code}; install the chart of accounts (§5).",
                    f"لا يوجد الحساب {code} لدى {entity.code}؛ ثبّت دليل الحسابات.", code="account_missing")
    return acc


def _settings_of(entity):
    s = PayrollSettings.objects.filter(entity=entity).first()
    if s is None:
        raise error(f"Payroll is not set up for {entity.code} (payroll_setup_entity).",
                    f"لم تُعدّ الرواتب للكيان {entity.code}.", code="payroll_not_set_up")
    return s


def setup_entity(entity):
    """``payroll_setup_entity <code>`` (operator, R3): settings for a new entity, disabled (OQ-2), bank account 1100.
    A new subsidiary is data, never code. Idempotent."""
    s = PayrollSettings.objects.filter(entity=entity).first()
    if s is None:
        s = PayrollSettings(entity=entity, salary_bank_account=_account(entity, "1100"))
        _save(s)
    return s


@transaction.atomic
def configure_settings(entity, *, user, **fields):
    """Change payroll settings (``payroll.manage_settings``). Checks: calendar day basis needs its reference (R10,
    Art. 2); salary bank one of the five (R51); OH override above 2.00 and at most double (4.00), with the GOSI
    decision reference (R21(f); Regs Art. 22); the bank account is a postable asset of the entity. Changes of the OH
    override are logged (AccessLog)."""
    _require(user, entity, "payroll.manage_settings")
    unknown = set(fields) - SETTINGS_FIELDS
    if unknown:
        raise error(f"Unknown payroll settings: {sorted(unknown)}.", f"إعدادات رواتب غير معروفة: {sorted(unknown)}.",
                    code="fields")
    s = PayrollSettings._base_manager.select_for_update().filter(entity=entity).first() or setup_entity(entity)
    before = {k: getattr(s, k) for k in ("gosi_oh_rate_override", "gosi_oh_override_ref", "gosi_oh_override_from",
                                         "gosi_oh_override_to")}
    for name, value in fields.items():
        if name == "salary_bank_account" and not isinstance(value, Account):
            value = _account(entity, value)
        if name == "gosi_oh_rate_override" and value is not None:
            value = _dec(value, name)
        setattr(s, name, value)
    problems = []
    if s.day_basis not in PayrollSettings.DayBasis.values:
        problems.append(f"Unknown day basis {s.day_basis!r}. / أساس الأيام {s.day_basis!r} غير معروف.")
    elif s.day_basis == PayrollSettings.DayBasis.CALENDAR and not s.day_basis_ref.strip():
        problems.append("Calendar days need the contract clause or work-regulations article that stipulates them; "
                        "otherwise the month is 30 days (Labour Law Art. 2, R10). / يلزم لاعتماد أيام التقويم ذكر بند "
                        "العقد أو مادة لائحة تنظيم العمل؛ وإلا فالشهر ثلاثون يومًا (المادة 2).")
    if s.salary_bank and s.salary_bank not in PayrollSettings.SalaryBank.values:
        problems.append(f"{s.salary_bank!r} is not one of the five salary banks (P15, R51). / {s.salary_bank!r} ليس "
                        "من بنوك الرواتب الخمسة.")
    if s.wps_format and s.wps_format not in ("generic_csv", "mudad_placeholder") and s.wps_format != s.salary_bank:
        problems.append("The WPS format must be the salary bank's own format (R51). / يجب أن تكون صيغة ملف حماية "
                        "الأجور صيغة بنك الرواتب نفسه.")
    acc = s.salary_bank_account
    if acc.entity_id != entity.pk or acc.type != "asset" or not acc.is_postable:
        problems.append("The salary bank account is a postable asset account of the entity. / حساب بنك الرواتب "
                        "حساب أصول قابل للترحيل من حسابات الكيان.")
    for day_field in ("default_pay_day", "input_cutoff_day", "out_of_country_cutoff_day"):
        if not 1 <= int(getattr(s, day_field)) <= 31:
            problems.append(f"{day_field} is a day 1 to 31. / {day_field} يوم من 1 إلى 31.")
    if s.gosi_oh_rate_override is not None:
        if not Decimal("2.00") < s.gosi_oh_rate_override <= Decimal("4.00"):
            problems.append("The OH rate override is above 2.00% and at most double, 4.00% (SIL Art. 29(2); Regs Art. "
                            "22). / تعديل نسبة الأخطار المهنية أعلى من 2.00% ولا يتجاوز الضعف 4.00%.")
        if not s.gosi_oh_override_ref.strip():
            problems.append("The OH rate override needs the GOSI decision reference (R21(f)). / يحتاج تعديل نسبة "
                            "الأخطار المهنية إلى مرجع قرار التأمينات.")
    if problems:
        raise _problems(problems, "settings_invalid")
    s.updated_by = user
    _save(s)
    after = {k: getattr(s, k) for k in before}
    if after != before:
        access_services.log(AccessLog.Event.PROFILE_CHANGED, actor=user, entity=entity,
                            permission="payroll.manage_settings",
                            details={"action": "gosi_oh_override", "before": {k: str(v) for k, v in before.items()},
                                     "after": {k: str(v) for k, v in after.items()},
                                     "message_en": "GOSI occupational hazards rate override changed",
                                     "message_ar": "تغيّر تعديل نسبة الأخطار المهنية"})
    return s


def _jurisdiction_gate(entity, use):
    """R3, R4 (P19(f), hr R68): payroll needs enabled settings and an active jurisdiction; only SA is calculated in
    slice 1. Egypt is activated as data but its parameters are unverified research (OQ-28): refused, en/ar."""
    s = _settings_of(entity)
    if not s.enabled:
        raise error(f"Payroll is not enabled for {entity.code} (R3).", f"الرواتب غير مفعلة للكيان {entity.code}.",
                    code="payroll_disabled")
    hs = HrSettings.objects.filter(entity=entity).first()
    if hs is None:
        raise error(f"HR is not set up for {entity.code} (R3).", f"لم تُعدّ الموارد البشرية للكيان {entity.code}.",
                    code="hr_not_set_up")
    jurisdiction = hs.jurisdiction
    row = hr.param(jurisdiction, "jurisdiction.active", _today(), required=False)
    if row is None or not (row.value or {}).get("active"):
        raise error(f"Jurisdiction {jurisdiction} is not activated for payroll (hr R68, R4).",
                    f"الولاية القانونية {jurisdiction} غير مفعلة للرواتب.", code="jurisdiction_not_activated")
    use._take(row)
    if jurisdiction != "SA":
        raise error(f"Jurisdiction {jurisdiction} is not configured for payroll: its local parameters are unverified "
                    "research and the local calculation is not built (R4, OQ-28).",
                    f"الولاية القانونية {jurisdiction} غير مهيأة للرواتب: معاييرها المحلية بحث غير متحقق منه والحساب "
                    "المحلي غير مبني.", code="jurisdiction_not_configured")
    return s


# =====================================================================================================
# Inputs (3.2; R9, R18, R44, R45, R52, R54, R61)
# =====================================================================================================

def _terms(emp, day):
    return hr._terms_on(emp, day)


def _components(terms):
    return list(terms.components.select_related("component", "component__expense_account")) if terms else []


def _sum(terms, flag):
    return sum((c.monthly_amount for c in _components(terms) if getattr(c.component, flag)), ZERO)


def _basic(terms):
    return sum((c.monthly_amount for c in _components(terms) if c.component.code == "BASIC"), ZERO)


def _overtime_route(emp, on):
    """R45 (P11, P20(a)): the entity's hr route (data). ``managing_partner`` -> the MP; ``department_head`` -> the
    user linked to the head of the employee's department; a department without an active head, or the head's own
    overtime, goes to the next eligible approver, the MP (hr OQ-30 default). Returns (roles, head_user)."""
    s = hr._settings(emp.entity)
    if s.overtime_approval_route == HrSettings.OvertimeRoute.MANAGING_PARTNER:
        return ("managing_partner",), None
    terms = _terms(emp, on)
    head = terms.department.head if terms and terms.department else None
    if head is None or head.user_id is None or head.status != Employee.Status.ACTIVE or head.pk == emp.pk:
        return ("managing_partner",), None
    return ("department_head",), head.user


def _year_overtime_hours(emp, on, exclude=None):
    terms = _terms(emp, on)
    if terms is not None and terms.calendar_basis == EmploymentContract.CalendarBasis.HIJRI:
        from hijridate import Gregorian
        hy = Gregorian(on.year, on.month, on.day).to_hijri().year
        from hijridate import Hijri
        g1, g2 = Hijri(hy, 1, 1).to_gregorian(), Hijri(hy + 1, 1, 1).to_gregorian()
        start, end = date(g1.year, g1.month, g1.day), date(g2.year, g2.month, g2.day) - timedelta(days=1)
    else:
        start, end = date(on.year, 1, 1), date(on.year, 12, 31)
    qs = PayrollInput.objects.filter(employee=emp, kind=IK.OVERTIME_HOURS, overtime_date__range=(start, end)) \
        .exclude(status=IS.REJECTED)
    if exclude is not None:
        qs = qs.exclude(pk=exclude)
    return qs.aggregate(t=Sum("quantity"))["t"] or ZERO


def _input_problems(inp, use):
    """Checks shared by entry and approval (R18, R45, R52, R54, Art. 109(2))."""
    emp = inp.employee
    problems = []
    basis = emp.employment_basis
    if inp.kind == IK.LEAVE_ENCASHMENT:
        problems.append("Cash instead of leave during service is not allowed; leave is encashed only in the final "
                        "settlement (Labour Law Art. 109(2), R34). / لا يجوز صرف بدل نقدي عن الإجازة أثناء الخدمة؛ "
                        "يُصرف البدل في المخالصة النهائية فقط (المادة 109(2)).")
        return problems
    if inp.kind == IK.DISCIPLINARY_FINE:
        problems.append("Disciplinary fines are not supported; they would go to a workers' fines fund, never income "
                        "(Labour Law Art. 73, R54, OQ-8). / الجزاءات التأديبية غير مدعومة.")
        return problems
    if not (inp.reason_ar or "").strip():
        problems.append("The reason in Arabic is required. / السبب بالعربية مطلوب.")
    if inp.kind in (IK.OVERTIME_HOURS, IK.UNPAID_DAYS):
        if inp.quantity is None or inp.quantity <= 0 or inp.amount is not None:
            problems.append("Overtime and unpaid days are entered as a quantity above zero, not an amount. / يُدخل "
                            "العمل الإضافي والأيام بدون أجر ككمية أكبر من صفر لا كمبلغ.")
        if basis == Basis.REMUNERATION:
            problems.append("Remuneration staff have no overtime and no unpaid-leave deduction (R41, P16). / لا عمل "
                            "إضافي ولا خصم أيام بدون أجر لأصحاب المكافأة الشهرية.")
    else:
        if inp.amount is None or inp.amount <= 0 or inp.quantity is not None:
            problems.append("This input is an amount above zero. / هذا المدخل مبلغ أكبر من صفر.")
        else:
            cur = _payslip_currency(emp, inp.period)
            if cur is not None and not fx.has_places(inp.amount, cur.minor_units):
                problems.append(f"{cur.code} amounts have at most {cur.minor_units} decimals (R5, R42). / مبالغ "
                                f"{cur.code} بحد أقصى {cur.minor_units} خانات عشرية.")
    if inp.component_id is not None and inp.component.entity_id != inp.entity_id:
        problems.append("The component belongs to another entity (R1). / المكوّن يخص كيانًا آخر.")
    start, end = _period_bounds(inp.period)
    if inp.kind == IK.OVERTIME_HOURS and basis != Basis.REMUNERATION:
        if inp.overtime_date is None or not start <= inp.overtime_date <= end:
            problems.append("Overtime needs its date inside the work period (R52). / يحتاج العمل الإضافي إلى تاريخه "
                            "ضمن فترة العمل.")
        else:
            terms = _terms(emp, inp.overtime_date)
            if terms is None:
                problems.append("No approved contract is in force on the overtime date. / لا يوجد عقد معتمد ساري في "
                                "تاريخ العمل الإضافي.")
            elif terms.grade is not None and not terms.grade.overtime_eligible:
                problems.append(f"Grade {terms.grade.code} is not eligible for overtime (P11, R45). / الدرجة "
                                f"{terms.grade.code} غير مؤهلة للعمل الإضافي.")
            year_cap = Decimal(use.get("SA", "overtime.max_hours_year", inp.overtime_date)["hours"])
            used = _year_overtime_hours(emp, inp.overtime_date, exclude=inp.pk)
            if used + inp.quantity > year_cap and not inp.worker_consent_ref.strip():
                problems.append(f"Overtime above {year_cap} hours a year needs the worker's consent (Regs Art. 22, R52); "
                                f"{used} hours are already recorded. / يتجاوز العمل الإضافي {year_cap} ساعة في السنة "
                                "ويحتاج إلى موافقة العامل (المادة 22 من اللائحة).")
            if inp.art106_exceptional:
                caps = use.get("SA", "overtime.exceptional_max", inp.overtime_date)
                same_day = PayrollInput.objects.filter(employee=emp, kind=IK.OVERTIME_HOURS,
                                                       overtime_date=inp.overtime_date, art106_exceptional=True) \
                    .exclude(status=IS.REJECTED).exclude(pk=inp.pk).aggregate(t=Sum("quantity"))["t"] or ZERO
                if same_day + inp.quantity > Decimal(caps["daily"]):
                    problems.append(f"In the exceptional cases of Art. 106 at most {caps['daily']} hours a day (R52). / "
                                    f"في الحالات الاستثنائية للمادة 106 بحد أقصى {caps['daily']} ساعات في اليوم.")
                # Week = Sunday to Saturday (KSA weekend Friday and Saturday; default).
                wstart = inp.overtime_date - timedelta(days=(inp.overtime_date.weekday() + 1) % 7)
                week = PayrollInput.objects.filter(employee=emp, kind=IK.OVERTIME_HOURS, art106_exceptional=True,
                                                   overtime_date__range=(wstart, wstart + timedelta(days=6))) \
                    .exclude(status=IS.REJECTED).exclude(pk=inp.pk).aggregate(t=Sum("quantity"))["t"] or ZERO
                if week + inp.quantity > Decimal(caps["weekly"]):
                    problems.append(f"In the exceptional cases of Art. 106 at most {caps['weekly']} hours a week (R52). / "
                                    f"في الحالات الاستثنائية للمادة 106 بحد أقصى {caps['weekly']} ساعة في الأسبوع.")
    if inp.kind == IK.ONE_OFF_DEDUCTION:
        basis_ = inp.deduction_basis
        if basis_ not in PayrollInput.DeductionBasis.values:
            problems.append("A one-off deduction states its basis: written consent, court order, damage (Art. 91) or "
                            "another statutory deduction (Art. 92, R18). / يذكر الاستقطاع أساسه: موافقة كتابية أو حكم "
                            "قضائي أو تعويض إتلاف أو استقطاع نظامي آخر.")
        elif basis_ == PayrollInput.DeductionBasis.WRITTEN_CONSENT and not inp.consent_ref.strip():
            problems.append("A deduction for a private claim needs the worker's written consent (Labour Law Art. 92, "
                            "R18). / يحتاج الاستقطاع لدين خاص إلى موافقة العامل الكتابية (المادة 92).")
        elif basis_ in (PayrollInput.DeductionBasis.COURT_ORDER, PayrollInput.DeductionBasis.DAMAGE_ART91) \
                and inp.amount and basis != Basis.REMUNERATION:
            terms = _terms(emp, end)
            wage = _sum(terms, "counts_for_eosb")
            caps = use.get("SA", "deduction.caps", end)
            if basis_ == PayrollInput.DeductionBasis.COURT_ORDER:
                limit = _round(Fraction(wage) * Fraction(caps["court_max_fraction"]))
                if inp.amount > limit:
                    problems.append(f"A court-ordered deduction is at most a quarter of the wage, {limit} (Art. 92(6)). / "
                                    f"لا يتجاوز الاستقطاع بحكم قضائي ربع الأجر {limit}.")
            else:
                limit = _round(Fraction(wage) * int(caps["damage_max_days_month"]) / 30)
                if inp.amount > limit:
                    problems.append(f"Damage recovery is at most 5 days' wage a month, {limit} (Art. 91). / لا يتجاوز "
                                    f"اقتطاع تعويض الإتلاف أجر خمسة أيام في الشهر {limit}.")
    if inp.kind == IK.MATERNITY_SUPPORT and not inp.legal_clearance_ref.strip():
        problems.append("Temporary maternity support needs the written legal clearance reference (R61(b)). / يحتاج "
                        "الدعم المؤقت أثناء الأمومة إلى مرجع الإذن القانوني المكتوب.")
    return problems


INPUT_FIELDS = frozenset({"kind", "period", "reference", "quantity", "amount", "component", "reason_en", "reason_ar",
                          "overtime_date", "art106_exceptional", "worker_consent_ref", "deduction_basis",
                          "consent_ref", "legal_clearance_ref"})


@transaction.atomic
def create_input(employee, *, user, **fields):
    """Enter a payroll input (``payroll.manage_inputs``). Leave-driven unpaid and sick days are read from hr, not
    entered (R16, R17). A charge linked to a loan or bai' ajel contract is refused with a Sharia message (R26, R48)."""
    emp = Employee.objects.select_related("entity").get(pk=employee.pk)
    _require(user, emp.entity, "payroll.manage_inputs")
    if {"loan", "deferred_sale"} & set(fields) or any(w in str(fields.get("reference", "")).lower() for w in
                                                    ("fee", "interest", "late")):
        raise error(*SHARIA_NO_CHARGE, code="sharia_no_charge")
    unknown = set(fields) - INPUT_FIELDS
    if unknown:
        raise error(f"Unknown input fields: {sorted(unknown)}.", f"حقول مدخلات غير معروفة: {sorted(unknown)}.",
                    code="fields")
    values = dict(fields)
    for name in ("quantity", "amount"):
        if values.get(name) is not None:
            values[name] = _dec(values[name], name)
    _check_period(values.get("period", ""))
    inp = PayrollInput(entity=emp.entity, employee=emp, created_by=user, **values)
    problems = _input_problems(inp, hr.ParamUse())
    if problems:
        raise _problems(problems, "input_invalid")
    try:
        with transaction.atomic():
            _save(inp)
    except IntegrityError:
        raise error("This input is already entered (same employee, period, kind and reference).",
                    "هذا المدخل مُدخل مسبقًا.", code="input_duplicate") from None
    return inp


def _approve_input_common(inp, user, permission, activity, roles=None, extra_people=()):
    grant = _require(user, inp.entity, permission, document=inp)
    approvals.check(grant, inp.entity, activity, roles=roles)
    if inp.employee.user_id is not None and inp.employee.user_id == user.pk:
        access.deny(user, inp.entity, permission, "self_approval")  # never one's own pay (R45, §7)
    if user.pk in extra_people:
        access.deny(user, inp.entity, permission, "self_approval")
    return grant


@transaction.atomic
def approve_input(payroll_input, *, user):
    """Approve an input other than overtime and maternity support (``payroll.approve_input``, A: never its creator
    or the employee). The run that takes it follows the cut-off of its group (R9, R44)."""
    inp = _lock(PayrollInput, payroll_input.pk)
    if inp.kind in (IK.OVERTIME_HOURS, IK.MATERNITY_SUPPORT):
        raise error("Overtime is approved with approve_overtime and maternity support with approve_maternity_support.",
                    "يُعتمد العمل الإضافي ودعم الأمومة من خلال خدمتيهما.", code="wrong_approval")
    _approve_input_common(inp, user, "payroll.approve_input", "payroll.input")
    return _approve(inp, user)


@transaction.atomic
def approve_overtime(payroll_input, *, user):
    """R45 (P11, P20(a)): overtime enters a run only after ``payroll.approve_overtime`` by the route's approver:
    the MP (route ``managing_partner``), or the user linked to the head of the employee's department (role
    ``department_head``). Never the employee or the input's creator."""
    inp = _lock(PayrollInput, payroll_input.pk)
    if inp.kind != IK.OVERTIME_HOURS:
        raise error("Only overtime inputs.", "مدخلات العمل الإضافي فقط.", code="wrong_approval")
    roles, head = _overtime_route(inp.employee, inp.overtime_date)
    _approve_input_common(inp, user, "payroll.approve_overtime", "payroll.overtime", roles=roles)
    if head is not None and head.pk != user.pk:
        access_services.log_denial(actor=user, entity=inp.entity, permission="payroll.approve_overtime",
                                   details={"reason": "not_department_head"})
        raise error("Overtime of this department is approved by its head (R45).",
                    "يعتمد رئيس القسم العمل الإضافي لقسمه.", code="not_department_head")
    return _approve(inp, user)


@transaction.atomic
def approve_maternity_support(payroll_input, *, user):
    """R61(b): a temporary support payment with the written legal clearance, approved by the approver (A)."""
    inp = _lock(PayrollInput, payroll_input.pk)
    if inp.kind != IK.MATERNITY_SUPPORT:
        raise error("Only maternity support inputs.", "مدخلات دعم الأمومة فقط.", code="wrong_approval")
    _approve_input_common(inp, user, "payroll.approve_maternity_clearance", "payroll.maternity_clearance")
    return _approve(inp, user)


def _approve(inp, user):
    if inp.status != IS.DRAFT:
        raise error("Only draft inputs are approved.", "تُعتمد المدخلات المسودة فقط.", code="input_status")
    use = hr.ParamUse()
    problems = _input_problems(inp, use)
    if problems:
        raise _problems(problems, "input_invalid")
    s = _settings_of(inp.entity)
    inp.status, inp.approved_by, inp.approved_at = IS.APPROVED, user, _now()
    inp.payroll_period = _payroll_period_for(inp.period, timezone.localtime(inp.approved_at).date(),
                                            _group_of(inp.employee), s)
    _save(inp)
    return inp


@transaction.atomic
def reject_input(payroll_input, *, user, reason_ar):
    inp = _lock(PayrollInput, payroll_input.pk)
    _require(user, inp.entity, "payroll.manage_inputs")
    if inp.status != IS.DRAFT:
        raise error("Only draft inputs are rejected.", "تُرفض المدخلات المسودة فقط.", code="input_status")
    inp.status, inp.modified_by, inp.modified_at = IS.REJECTED, user, _now()
    inp.reason_ar = f"{inp.reason_ar} | {reason_ar}"
    _save(inp)
    return inp


# --- GOSI records entered by people ---------------------------------------------------------------

@transaction.atomic
def record_maternity_case(leave_request, *, user, status, gosi_reference="", confirmed_from=None, confirmed_to=None,
                          monthly_amount=None, refusal_reference="", medical_report_ref=""):
    """R61 (hr 3.7 note): the hr_officer records the GOSI case of an approved MATERNITY leave from GOSI.
    Confirmed needs the GOSI reference, period and monthly amount; refused needs the refusal reference (P20(d))."""
    req = LeaveRequest.objects.select_related("entity", "employee", "leave_type").get(pk=leave_request.pk)
    _require(user, req.entity, "payroll.record_maternity_case")
    if req.leave_type.code != "MATERNITY" or req.status not in (LeaveRequest.Status.APPROVED, LeaveRequest.Status.TAKEN):
        raise error("A GOSI maternity case belongs to an approved MATERNITY leave.",
                    "تتعلق حالة الأمومة لدى التأمينات بإجازة وضع معتمدة.", code="leave_status")
    case = MaternityGosiCase._base_manager.select_for_update().filter(leave_request=req).first()
    if case is None:
        case = MaternityGosiCase(entity=req.entity, employee=req.employee, leave_request=req, created_by=user)
    else:
        case.modified_by, case.modified_at = user, _now()
    if monthly_amount is not None:
        monthly_amount = _dec(monthly_amount, "monthly_amount")
    case.status, case.gosi_reference, case.refusal_reference = status, gosi_reference, refusal_reference
    case.confirmed_from, case.confirmed_to, case.monthly_amount = confirmed_from, confirmed_to, monthly_amount
    case.medical_report_ref = medical_report_ref or case.medical_report_ref
    problems = []
    if status not in MaternityGosiCase.Status.values:
        problems.append(f"Unknown status {status!r}. / الحالة {status!r} غير معروفة.")
    if status == MaternityGosiCase.Status.CONFIRMED and not (gosi_reference.strip() and confirmed_from and confirmed_to
                                                              and monthly_amount is not None and monthly_amount >= 0):
        problems.append("A confirmed case needs the GOSI reference, the compensation period and the monthly amount "
                        "(R61(a)). / تحتاج الحالة المؤكدة إلى مرجع التأمينات وفترة التعويض والمبلغ الشهري.")
    if status == MaternityGosiCase.Status.REFUSED and not refusal_reference.strip():
        problems.append("A refused case needs GOSI's refusal reference (R61(c), P20(d)). / تحتاج الحالة المرفوضة إلى "
                        "مرجع رفض التأمينات.")
    if problems:
        raise _problems(problems, "maternity_case_invalid")
    _save(case)
    return case


@transaction.atomic
def approve_maternity_clearance(case, *, user, legal_clearance_ref):
    """R61(d): written legal clearance allowing full salary next to GOSI compensation, approved by the approver
    (A, not the case's recorder)."""
    case = _lock(MaternityGosiCase, case.pk)
    grant = _require(user, case.entity, "payroll.approve_maternity_clearance", document=case)
    approvals.check(grant, case.entity, "payroll.maternity_clearance")
    if not (legal_clearance_ref or "").strip():
        raise error("The written legal clearance reference is required.", "مرجع الإذن القانوني المكتوب مطلوب.",
                    code="clearance_required")
    case.legal_clearance_ref, case.clearance_approved_by, case.clearance_approved_at = legal_clearance_ref, user, _now()
    _save(case)
    return case


@transaction.atomic
def record_abroad_period(employee, *, user, exit_date, purpose, reentry_date=None):
    """R64 (GOSI Regs Art. 23): a stay outside the Kingdom from the exit and re-entry records."""
    emp = Employee.objects.select_related("entity").get(pk=employee.pk)
    _require(user, emp.entity, "payroll.manage_inputs")
    if purpose not in GosiAbroadPeriod.Purpose.values or (reentry_date and reentry_date < exit_date):
        raise error("A stay abroad needs its purpose and a re-entry on or after the exit.",
                    "تحتاج الإقامة بالخارج إلى غرضها وتاريخ عودة لا يسبق المغادرة.", code="abroad_invalid")
    return _save(GosiAbroadPeriod(entity=emp.entity, employee=emp, exit_date=exit_date, reentry_date=reentry_date,
                                  purpose=purpose, created_by=user))


@transaction.atomic
def record_variable_wage_base(employee, *, user, year, amount, method, source_note):
    """R65 (GOSI Regs Art. 18(2)): the GOSI amount of commission / profit-share pay for ``year``."""
    emp = Employee.objects.select_related("entity").get(pk=employee.pk)
    _require(user, emp.entity, "payroll.manage_inputs")
    amount = _dec(amount, "amount")
    if method not in GosiVariableWageBase.Method.values or amount < 0 or not (source_note or "").strip():
        raise error("The variable wage base needs a method, an amount of zero or more and its source.",
                    "يحتاج أساس الأجر المتغير إلى الطريقة ومبلغ لا يقل عن صفر ومصدره.", code="variable_base_invalid")
    try:
        with transaction.atomic():
            return _save(GosiVariableWageBase(entity=emp.entity, employee=emp, year=year, amount=amount, method=method,
                                              source_note=source_note, created_by=user))
    except IntegrityError:
        raise error("A base for this year is already recorded.", "سُجل أساس لهذه السنة مسبقًا.",
                    code="variable_base_duplicate") from None


# =====================================================================================================
# Calculation (R8 to R25, R41, R42, R55 to R68)
# =====================================================================================================

def _payslip_currency(emp, period):
    if emp.employment_basis != Basis.REMUNERATION:
        return emp.entity.base_currency
    terms = _terms(emp, _period_bounds(period)[1]) or _terms(emp, _period_bounds(period)[0])
    return terms.currency if terms else None


@dataclass
class Line:
    line_type: str
    amount: Decimal
    account: object
    desc_en: str
    desc_ar: str
    component: object = None
    branch: str = ""
    quantity: Decimal = None
    rate: Decimal = None
    amount_txn: Decimal = None
    source_ref: str = ""
    origin_period: str = ""


@dataclass
class Employment:
    period: str
    start: date
    end: date
    days: list  # [(day, version)] employed days
    weights: dict  # day -> paid-day weight
    divisor: int
    dim: int

    @property
    def first(self):
        return self.days[0][0]

    @property
    def last(self):
        return self.days[-1][0]

    @property
    def full_month(self):
        return len(self.days) == self.dim

    def version_on(self, day):
        for d, v in reversed(self.days):
            if d <= day:
                return v
        return self.days[0][1]

    @property
    def paid_days(self):
        return sum(self.weights[d] for d, _ in self.days)


@dataclass
class Slip:
    employee: object
    basis: str
    nationality_class: str
    cohort: str
    currency: object
    fx_rate: Decimal = Decimal("1")
    contract: object = None
    paid_days: Decimal = ZERO
    lines: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    refusal: str = ""
    gosi_wage: Decimal = ZERO
    gosi_wage_full: Decimal = ZERO
    maternity_status: str = "none"
    maternity_days: Decimal = ZERO
    gosi_benefit_amount: Decimal = ZERO
    maternity_top_up: Decimal = ZERO
    pending_gosi_amount: Decimal = ZERO
    legal_clearance_ref: str = ""
    leave_balance: Decimal = ZERO
    loan_balance: Decimal = ZERO
    document_flag: str = ""
    loan_effects: list = field(default_factory=list)  # [(installment pk, deducted)]
    sale_effects: list = field(default_factory=list)
    eosb: tuple = None  # (amount, required)
    notices: list = field(default_factory=list)  # [(origin, amount, en, ar)]
    alerts: list = field(default_factory=list)  # [(kind, key, en, ar, roles)]

    def warn(self, en, ar):
        text = f"{self.employee.employee_code}: {en} / {self.employee.employee_code}: {ar}"
        if text not in self.warnings:
            self.warnings.append(text)

    def refuse(self, en, ar):
        self.refusal = f"{self.employee.employee_code}: {en} / {self.employee.employee_code}: {ar}"
        raise _Refused()

    def total(self, kinds):
        return sum((l.amount for l in self.lines if l.line_type in kinds), ZERO)

    def total_txn(self, kinds):
        return sum((l.amount_txn for l in self.lines if l.line_type in kinds), ZERO)


class _Refused(Exception):
    pass


class Ctx:
    """One calculation: the run's entity, settings, period, parameter use and account cache."""

    def __init__(self, run, user, s):
        self.run, self.user, self.s = run, user, s
        self.entity = run.entity
        self.period = run.period
        self.start, self.end = _period_bounds(run.period)
        self.use = hr.ParamUse()
        self._accounts = {}
        self.warnings = []

    def account(self, code):
        if code not in self._accounts:
            self._accounts[code] = _account(self.entity, code)
        return self._accounts[code]

    def p(self, code, on=None):
        return self.use.get("SA", code, on or self.end, "payroll", "الرواتب")


def _weights(start, end, basis):
    dim = (end - start).days + 1
    weights = {}
    for i in range(dim):
        d = start + timedelta(days=i)
        if basis == PayrollSettings.DayBasis.CALENDAR:
            weights[d] = 1
        elif d.day == 31:
            weights[d] = 0
        elif d == end and dim < 30:
            weights[d] = 31 - d.day  # Feb: the last day stands for the rest of a 30-day month
        else:
            weights[d] = 1
    return weights, (dim if basis == PayrollSettings.DayBasis.CALENDAR else 30), dim


def _versions(emp):
    return list(EmploymentContract.objects.filter(employee=emp, status=EmploymentContract.Status.APPROVED)
                .select_related("department", "department__head", "grade", "currency")
                .order_by("effective_date", "version_no"))


def _termination(emp):
    return Termination.objects.filter(employee=emp, status=Termination.Status.APPROVED).order_by("-approved_at").first()


def _employment(emp, period, s, versions=None):
    """R10: the employed days of ``period`` with the version in force each day (R20), or None. A termination's last
    working day and a fixed term's end (extended by suspended days, hr R62) end it."""
    start, end = _period_bounds(period)
    versions = versions if versions is not None else _versions(emp)
    if not versions:
        return None
    term = _termination(emp)
    ends = {}
    days = []
    d = start
    while d <= end:
        v = None
        for cand in versions:
            if cand.effective_date <= d:
                v = cand
        if v is not None and d >= emp.hire_date and d >= v.start_date and (term is None or d <= term.last_working_day):
            if v.pk not in ends:
                ends[v.pk] = hr._contract_end(v)
            if ends[v.pk] is None or d <= ends[v.pk]:
                days.append((d, v))
        d += timedelta(days=1)
    if not days:
        return None
    weights, divisor, dim = _weights(start, end, s.day_basis)
    return Employment(period, start, end, days, weights, divisor, dim)


def _component_earnings(emp_info, exact=False):
    """Prorated contract earnings per component (R10): monthly x weighted days of each version / divisor, exact,
    rounded once per component."""
    by_comp = {}
    per_version = {}
    for d, v in emp_info.days:
        per_version.setdefault(v.pk, [v, 0])[1] += emp_info.weights[d]
    for v, weight in per_version.values():
        for c in _components(v):
            if c.component.kind != "earning":
                continue
            entry = by_comp.setdefault(c.component.pk, [c.component, Fraction(0)])
            entry[1] += Fraction(c.monthly_amount) * weight / emp_info.divisor
    return {pk: (comp, amount if exact else _round(amount)) for pk, (comp, amount) in by_comp.items()}


def _nationality_class(emp):
    if emp.nationality == "SA":
        return Payslip.NationalityClass.SAUDI
    if emp.nationality in GCC:
        return Payslip.NationalityClass.GCC
    return Payslip.NationalityClass.NON_SAUDI


def _age_on(emp, day):
    dob = emp.gosi_registered_dob or emp.date_of_birth  # Regs Art. 9: the GOSI record governs
    if dob is None:
        return None
    return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


def _reaches(emp, years, period):
    """Period in which the employee reaches ``years`` (GOSI-registered date of birth)."""
    dob = emp.gosi_registered_dob or emp.date_of_birth
    if dob is None:
        return None
    return f"{dob.year + years:04d}-{dob.month:02d}"


@dataclass
class Gosi:
    branches: list = field(default_factory=list)  # [(branch, wage, employee_pct, employer_pct)]
    wage: Decimal = ZERO
    wage_full: Decimal = ZERO


BRANCH_TEXT = {
    "annuities": ("Annuities", "المعاشات"),
    "saned": ("SANED (unemployment insurance)", "ساند (التأمين ضد التعطل)"),
    "occupational_hazards": ("Occupational hazards", "الأخطار المهنية"),
}


def _gosi_wage_full(ctx, slip, emp, info, versions_cache=None):
    """R20 / R65 / GOSI Regs Art. 17, 18: the monthly contributory wage of the version in force on the first employed
    day: basic and cash housing per contract (counts_for_gosi), commission and profit shares at last year's monthly
    average (GosiVariableWageBase), in-kind housing at BASIC x months / 12. Never reduced by unpaid, sick or maternity
    days (R67, Regs Art. 20(1), (3))."""
    v = info.version_on(info.first)
    total = Fraction(0)
    year = int(info.period[:4])
    for c in _components(v):
        if not c.component.counts_for_gosi:
            continue
        if c.component.code in ("COMMISSION", "PROFIT_SHARE"):
            ctx.p("gosi.variable_wage_basis", info.end)
            base = GosiVariableWageBase.objects.filter(employee=emp, year=year).first()
            if base is None:
                slip.refuse(f"GOSI wage base of {c.component.code} for {year} missing: the previous year's monthly "
                            "average is entered first (GOSI Regs Art. 18(2), R65).",
                            f"أساس اشتراك {c.component.code} لسنة {year} غير مُدخل: يُدخل متوسط السنة السابقة الشهري "
                            "أولًا (المادة 18(2) من اللائحة).")
            total += Fraction(base.amount)
            continue
        total += Fraction(c.monthly_amount)
    if v.housing_in_kind and not ctx.s.gosi_housing_in_kind_exempt_ref.strip():
        row = ctx.p("gosi.housing_in_kind_basic_months", info.end)
        per = Fraction(12) if row.get("per") == "year" else Fraction(1)
        total += Fraction(_basic(v)) * Fraction(row["months"]) / per
        slip.notes.append("In-kind housing counted at BASIC x 2 / 12 (GOSI Regs Art. 17(c); period reading "
                          "unverified). / احتُسب السكن العيني بالراتب الأساسي × 2 / 12.")
    return total, v


def _other_employers_wage(emp, info):
    """R68 (GOSI Regs Art. 19): the same person's GOSI wage with other employers: the GOSI record figure and the
    contracts of the same person (same national ID) in other group entities this month."""
    total = Fraction(emp.gosi_other_employers_wage or 0)
    others = Employee.objects.filter(id_number_hash=emp.id_number_hash).exclude(entity=emp.entity) \
        .exclude(status__in=(Employee.Status.DRAFT,))
    for other in others:
        v = hr._terms_on(other, info.end)
        if v is None or other.hire_date > info.end:
            continue
        t = _termination(other)
        if t is not None and t.last_working_day < info.start:
            continue
        total += Fraction(_sum(v, "counts_for_gosi"))
    return total


def _compute_gosi(ctx, slip, emp, info, *, cohort, nclass, basis, remuneration_sar=None):
    """R20 to R22, R41, R55, R56, R62, R64, R66, R68: branches, contributory wage and rates for one period."""
    end = info.end
    g = Gosi()
    if nclass == Payslip.NationalityClass.GCC:
        ctx.p("gosi.gcc", end)
        slip.refuse("GCC GOSI treatment not configured (R21(h); no GCC provision in the Law or Regulations).",
                    "معالجة التأمينات لمواطني دول الخليج غير مهيأة.")
    saudi = nclass == Payslip.NationalityClass.SAUDI
    if basis == Basis.REMUNERATION:
        if not saudi:
            return g  # non-Saudis working abroad: no GOSI branch (SIL Art. 14, 28, 43)
        rel = emp.gosi_employment_relationship
        if not rel:
            slip.refuse("GOSI employment relationship of this Saudi remuneration person not recorded (hr R69, R56(c)).",
                        "لم تُسجل علاقة العمل لدى التأمينات لصاحب المكافأة السعودي.")
        if rel == Employee.GosiRelationship.NON_SUBORDINATE:
            slip.notes.append("Not a GOSI worker (no subordination; R41, P19(b)). / ليس عاملًا لدى التأمينات (دون تبعية).")
            return g
    if saudi and not (cohort and emp.gosi_first_coverage_date):
        slip.refuse("GOSI cohort or first coverage date missing (R56(a), R55).",
                    "فئة التأمينات أو تاريخ أول خضوع غير مسجل.")
    key = cohort or "new_m273"
    # Wage (R20, R62, R65).
    if remuneration_sar is not None:
        full = Fraction(remuneration_sar)
        slip.notes.append("GOSI wage = remuneration in SAR at the run's rate (R62, OQ-45). / أجر الاشتراك = المكافأة "
                          "بالريال بسعر المسير.")
    else:
        full, _v = _gosi_wage_full(ctx, slip, emp, info)
    floor = Fraction(ctx.p("gosi.min_wage", end)["saudi" if saudi else "non_saudi"])
    caps = ctx.p("gosi.max_contributory_wage", end)
    cap = Fraction(caps[key])
    full = max(full, floor)
    own = full
    if remuneration_sar is None:
        other = _other_employers_wage(emp, info)
        if other and own + other > cap:
            own = cap * own / (own + other)
            slip.notes.append(f"GOSI wage reduced pro rata: other employers {_round(other)} (GOSI Regs Art. 19, R68). / "
                              f"خُفض أجر الاشتراك بالتناسب لوجود أصحاب عمل آخرين.")
    own = min(own, cap)
    g.wage_full = _round(own)
    wage = own
    if remuneration_sar is None and not info.full_month:
        rule = ctx.p("gosi.partial_month_divisor", end)["divisor"]
        if rule == "thirty":
            wage = own * info.paid_days / 30
        else:
            wage = own * len(info.days) / info.dim
        slip.notes.append("Joining or leaving month: GOSI on days of service (GOSI Regs Art. 20(2), R66; divisor "
                          "unverified). / شهر الالتحاق أو الترك: الاشتراك عن أيام الخدمة.")
    g.wage = _round(wage)
    # Rates (R21).
    ann = ctx.p("gosi.rates.annuities", end)[key]
    if ctx.period[5:] == "07" and ctx.period in ctx.p("gosi.annuities_step_start", end).get("july_months", []):
        slip.warn("July rate step month read from the unverified parameter gosi.annuities_step_start (R21(b)).",
                  "شهر تغيير نسبة المعاشات في يوليو مأخوذ من معيار غير متحقق منه.")
    in_kingdom = basis == Basis.KSA_EMPLOYEE
    branches = []
    age_first = _age_on(emp, emp.gosi_first_coverage_date) if emp.gosi_first_coverage_date else None
    if saudi:
        legal = ctx.use.get("SA", "gosi.legal_age", end)
        legal_years = int(legal.get(key, legal["new_m273"])["years"])
        annuities = True
        if key == "new_m273" and age_first is not None and age_first >= legal_years:
            annuities = False  # Law Art. 14(2): below the legal age when first subject
            slip.notes.append("No annuities: first covered at or above the legal age (Law Art. 14(2), R55(a)). / لا "
                              "معاشات: أول خضوع في سن النظامية أو بعدها.")
        if annuities:
            branches.append(("annuities", Decimal(ann["employee"]), Decimal(ann["employer"])))
        if in_kingdom:
            saned_row = ctx.p("gosi.saned_max_entry_age", end)[key]
            entry_max = int(saned_row["max_entry_age"])
            stop_years = legal_years if saned_row.get("stop") == "legal_age" else int(saned_row["stop_age"])
            saned = age_first is None or age_first < entry_max
            reach = _reaches(emp, stop_years, ctx.period)
            if saned and reach is not None and info.period >= reach:
                rule = ctx.p("gosi.saned_stop_month_rule", end)["rule"]
                if info.period > reach or rule != "month_after_reaching_age":
                    saned = False
                else:
                    slip.warn(f"Reaches {stop_years} this month: SANED stops from next month (unverified stop-month "
                              "rule, R55(c)).", f"يبلغ {stop_years} هذا الشهر: يتوقف ساند من الشهر التالي (قاعدة غير "
                              "متحقق منها).")
            if saned:
                sr = ctx.p("gosi.rates.saned", end)[key]
                branches.append(("saned", Decimal(sr["employee"]), Decimal(sr["employer"])))
            elif age_first is not None and age_first >= entry_max:
                slip.notes.append(f"No SANED: first covered at {age_first} (entry age below {entry_max}; R55(b)). / لا "
                                  "ساند: سن أول خضوع تتجاوز الحد.")
        elif basis == Basis.REMUNERATION and key == "legacy_m33":
            slip.warn("OH and SANED abroad under SIL M/33 Art. 4(3) and UIL Art. 3(3) not confirmed (OQ-40(c)).",
                      "الأخطار المهنية وساند في الخارج وفق النظام السابق غير مؤكدة.")
    if in_kingdom:
        oh = ctx.p("gosi.rates.occupational_hazards", end)
        er = Decimal(oh[key]["employer"])
        s = ctx.s
        if s.gosi_oh_rate_override is not None and (s.gosi_oh_override_from is None or s.gosi_oh_override_from <= end) \
                and (s.gosi_oh_override_to is None or s.gosi_oh_override_to >= info.start):
            er = s.gosi_oh_rate_override
            slip.notes.append(f"OH rate {er}% per GOSI decision {s.gosi_oh_override_ref} (Regs Art. 22). / نسبة الأخطار "
                              f"المهنية {er}% بقرار التأمينات.")
        if _oh_suspended(ctx, emp, info):
            slip.notes.append("OH suspended while abroad for non-work reasons (GOSI Regs Art. 23, R64). / أوقفت الأخطار "
                              "المهنية أثناء الإقامة خارج المملكة لغير العمل.")
        else:
            branches.append(("occupational_hazards", ZERO, er))
        if not saudi:
            v = info.version_on(info.last)
            if v.end_date and (v.end_date - v.start_date).days <= 92:
                slip.warn("Non-Saudi on a contract of 3 months or less: may be outside GOSI (SIL M/33 Art. 5(1)(f); Law "
                          "Art. 6(5) for 60-day temporary visas); OH kept.",
                          "غير سعودي بعقد ثلاثة أشهر أو أقل: قد يكون خارج التأمينات؛ أُبقيت الأخطار المهنية.")
    g.branches = [(b, g.wage, ee, er) for b, ee, er in branches]
    return g


def _oh_suspended(ctx, emp, info):
    """R64 (GOSI Regs Art. 23): OH stops from the 1st of the month after a non-work exit to the month before the
    return."""
    for stay in GosiAbroadPeriod.objects.filter(employee=emp, purpose=GosiAbroadPeriod.Purpose.PERSONAL,
                                                exit_date__lt=info.start):
        exit_p = _period_of(stay.exit_date)
        back_p = _period_of(stay.reentry_date) if stay.reentry_date else None
        if exit_p < info.period and (back_p is None or info.period < back_p):
            ctx.p("gosi.oh_abroad_suspension", info.end)
            return True
    return False


def _gosi_lines(ctx, slip, g, origin="", employee_wage_delta=None):
    """Employee and employer lines per branch (R22): employee share a deduction, employer share Dr 5110; both Cr 2340."""
    out = []
    for branch, wage, ee, er in g.branches:
        en, ar = BRANCH_TEXT[branch]
        if ee:
            out.append(Line(LT.EMPLOYEE_GOSI, _round(Fraction(wage) * Fraction(ee) / 100), ctx.account(GOSI_PAYABLE),
                            f"GOSI {en} employee {ee}%", f"التأمينات {ar} حصة الموظف {ee}%", branch=branch,
                            quantity=wage, rate=ee, origin_period=origin))
        if er:
            out.append(Line(LT.EMPLOYER_GOSI, _round(Fraction(wage) * Fraction(er) / 100), ctx.account(GOSI_EXPENSE),
                            f"GOSI {en} employer {er}%", f"التأمينات {ar} حصة صاحب العمل {er}%", branch=branch,
                            quantity=wage, rate=er, origin_period=origin))
    return out


def _expense(ctx, component):
    return component.expense_account if component is not None else ctx.account(SALARIES)


def _by_account(ctx, info, days, flag, factor=Fraction(1)):
    """{account: Fraction} of components with ``flag`` for the given days (version of each day), / divisor."""
    out = {}
    for d in days:
        v = info.version_on(d)
        for c in _components(v):
            if c.component.kind == "earning" and getattr(c.component, flag):
                acc = c.component.expense_account
                out[acc] = out.get(acc, Fraction(0)) + Fraction(c.monthly_amount) * factor / info.divisor
    return out


def _weight(info, days):
    return sum(info.weights.get(d, 0) for d in days)


def _maternity(ctx, slip, emp, info, inputs):
    """R61 (P19(a), P20(d), (e)): the GOSI maternity case of an approved MATERNITY leave with a birth from July 2025.
    Returns the days whose contract pay is suspended or carried (excluded from unpaid and sick reductions)."""
    employed = {d for d, _ in info.days}
    reqs = LeaveRequest.objects.filter(employee=emp, leave_type__code="MATERNITY", start_date__lte=info.end,
                                       end_date__gte=info.start,
                                       status__in=(LeaveRequest.Status.APPROVED, LeaveRequest.Status.TAKEN))
    handled = set()
    for req in reqs:
        birth = req.actual_event_date or req.event_date
        if birth is None or birth < MATERNITY_BIRTHS_FROM:
            continue
        unpaid = hr._unpaid_days_of(req, info.start, info.end)
        days = {d for d in hr._days(max(req.start_date, info.start), min(req.end_date, info.end))} & employed - unpaid
        if not days:
            continue
        case = MaternityGosiCase.objects.filter(leave_request=req).first()
        status = case.status if case else MaternityGosiCase.Status.PENDING
        slip.maternity_status = status
        wage_days = sorted(days)
        supports = [i for i in inputs if i.kind == IK.MATERNITY_SUPPORT]
        if status == MaternityGosiCase.Status.REFUSED:
            slip.notes.append(f"GOSI refused the maternity claim ({case.refusal_reference}): full pay for the leave days "
                              "(R61(c), P20(d)). / رفضت التأمينات التعويض: أجر كامل عن أيام الإجازة.")
            continue
        if status == MaternityGosiCase.Status.CONFIRMED:
            covered = [d for d in wage_days if case.confirmed_from <= d <= case.confirmed_to]
            if not covered:
                continue
            if case.legal_clearance_ref and case.clearance_approved_at:
                slip.legal_clearance_ref = case.legal_clearance_ref
                slip.notes.append(f"Full salary next to GOSI compensation under written legal clearance "
                                  f"{case.legal_clearance_ref} (R61(d)). / أجر كامل مع تعويض التأمينات بإذن قانوني.")
                continue
            if supports:
                slip.refuse("Full salary or support next to confirmed GOSI compensation needs written legal clearance "
                            "(R61(d)).", "الأجر الكامل أو الدعم مع تعويض التأمينات المؤكد يحتاج إلى إذن قانوني مكتوب.")
            weight = _weight(info, covered)
            suspended = _by_account(ctx, info, covered, "counts_for_eosb")
            full = _round(sum(suspended.values(), Fraction(0)))
            # The days' share of contract pay is suspended per expense account.
            for acc, amount in suspended.items():
                slip.lines.append(Line(LT.MATERNITY_SUSPENDED, _round(amount), acc,
                                       "Salary suspended: GOSI maternity compensation", "أجر موقوف: تعويض الأمومة من التأمينات",
                                       quantity=Decimal(weight), source_ref=f"maternity:{case.pk}"))
            benefit = _round(Fraction(case.monthly_amount) * weight / info.divisor)
            top_up = max(ZERO, full - benefit)
            slip.maternity_days, slip.gosi_benefit_amount, slip.maternity_top_up = Decimal(weight), benefit, top_up
            if top_up:
                slip.lines.append(Line(LT.MATERNITY_TOP_UP, top_up, ctx.account(SALARIES),
                                       "Maternity top-up to full pay (Art. 151)", "فرق الأجر الكامل أثناء الأمومة (المادة 151)",
                                       quantity=Decimal(weight), source_ref=f"maternity:{case.pk}"))
            handled |= set(covered)
        else:  # pending: not unpaid, carried, escalated (R61(b))
            weight = _weight(info, wage_days)
            carried = _by_account(ctx, info, wage_days, "counts_for_eosb")
            for acc, amount in carried.items():
                slip.lines.append(Line(LT.MATERNITY_SUSPENDED, _round(amount), acc,
                                       "Salary carried pending GOSI decision (not unpaid)",
                                       "أجر مُرحّل بانتظار قرار التأمينات (ليست إجازة بدون أجر)", quantity=Decimal(weight),
                                       source_ref=f"maternity_pending:{req.pk}"))
            slip.pending_gosi_amount = _round(sum(carried.values(), Fraction(0)))
            slip.maternity_days = Decimal(weight)
            slip.warn("Maternity case pending at GOSI: salary for the leave days carried, not unpaid (R61(b)).",
                      "حالة الأمومة بانتظار التأمينات: أجر أيام الإجازة مُرحّل وليس بدون أجر.")
            slip.alerts.append((PayrollAlert.Kind.MATERNITY_PENDING, f"maternity:{req.pk}:{info.period}",
                                f"URGENT: maternity case of {emp.employee_code} pending at GOSI for {info.period}; escalate "
                                "to HR and legal (R61(b)).",
                                f"عاجل: حالة أمومة {emp.employee_code} بانتظار التأمينات لفترة {info.period}؛ صعّدها إلى "
                                "الموارد البشرية والقانونية.", ("hr_officer", "managing_partner")))
            handled |= set(wage_days)
    return handled


def _employee_inputs(ctx, emp):
    return list(PayrollInput.objects.filter(employee=emp, status=IS.APPROVED, payroll_period=ctx.period)
                .select_related("component", "component__expense_account").order_by("id"))


def _prior_payslips(ctx, emp):
    return list(Payslip.objects.filter(employee=emp, refused=False, run__entity=ctx.entity,
                                       run__status__in=(RS.APPROVED, RS.PAID), run__payroll_group=ctx.run.payroll_group,
                                       run__period__lt=ctx.period).select_related("run").order_by("run__period"))


def _booked_lines(emp, period, types):
    return PayslipLine.objects.filter(payslip__employee=emp, payslip__run__status__in=(RS.APPROVED, RS.PAID),
                                      line_type__in=types).filter(
        Q(origin_period=period) | (Q(origin_period="") & Q(payslip__run__period=period)))


def _prior_corrections(ctx, slip, emp, versions):
    """R14 (retro pay) and R58 (GOSI shares of earlier months), for every earlier approved payslip of the group."""
    for ps in _prior_payslips(ctx, emp):
        q = ps.run.period
        info = _employment(emp, q, ctx.s, versions)
        if info is None:
            continue
        # Retro earnings (R14): the month's contract pay under the versions approved now, against what was paid.
        if True:
            required = _component_earnings(info)
            for pk, (comp, amount) in required.items():
                paid = _booked_lines(emp, q, (LT.EARNING, LT.RETRO)).filter(component_id=pk) \
                    .aggregate(t=Sum("amount"))["t"] or ZERO
                diff = amount - paid
                if diff > 0:
                    slip.lines.append(Line(LT.RETRO, diff, comp.expense_account, f"Retro {comp.code} for {q}",
                                           f"فرق بأثر رجعي {comp.name_ar} عن {q}", component=comp, origin_period=q))
                elif diff < 0:
                    slip.warn(f"Contract decrease for {q}: {comp.code} overpaid by {-diff}; recover only by an approved "
                              "deduction with written consent (Art. 92).",
                              f"تخفيض بأثر رجعي عن {q}: زيادة مدفوعة {-diff}؛ لا تُسترد إلا باستقطاع معتمد بموافقة "
                              "كتابية (المادة 92).")
        # GOSI of the earlier month on the wage now known (R58).
        try:
            sub = Slip(employee=emp, basis=slip.basis, nationality_class=slip.nationality_class, cohort=slip.cohort,
                       currency=slip.currency)
            g = _compute_gosi(ctx, sub, emp, info, cohort=ps.cohort, nclass=ps.nationality_class,
                             basis=ps.employment_basis,
                             remuneration_sar=(ps.gosi_wage_full if ps.employment_basis == Basis.REMUNERATION else None))
        except _Refused:
            continue
        booked = list(_booked_lines(emp, q, (LT.EMPLOYEE_GOSI, LT.EMPLOYER_GOSI, LT.GOSI_CATCH_UP)))
        for branch, wage, ee, er in g.branches:
            en, ar = BRANCH_TEXT[branch]
            rows = [l for l in booked if l.gosi_branch == branch]
            ee_rows = [l for l in rows if l.line_type in (LT.EMPLOYEE_GOSI, LT.GOSI_CATCH_UP)
                       or l.source_ref.startswith("borne:")]
            er_rows = [l for l in rows if l.line_type == LT.EMPLOYER_GOSI and not l.source_ref.startswith("borne:")]
            covered = sum((l.quantity or ZERO for l in ee_rows if l.line_type == LT.EMPLOYEE_GOSI), ZERO) if ee \
                else sum((l.quantity or ZERO for l in er_rows), ZERO)
            delta = wage - covered
            for side, pct, rows_, account, ltype in (("employee", ee, ee_rows, GOSI_PAYABLE, LT.EMPLOYEE_GOSI),
                                                     ("employer", er, er_rows, GOSI_EXPENSE, LT.EMPLOYER_GOSI)):
                if not pct:
                    continue
                booked_amt = sum((l.amount for l in rows_), ZERO)
                at_covered = _round(Fraction(covered) * Fraction(pct) / 100)
                retro = _round(Fraction(wage) * Fraction(pct) / 100) - at_covered if delta > 0 else ZERO
                missed = at_covered - booked_amt
                if retro > 0:
                    slip.lines.append(Line(ltype, retro, ctx.account(account), f"GOSI {en} {side} on retro wage for {q}",
                                           f"التأمينات {ar} على فرق الأجر عن {q}", branch=branch, quantity=delta,
                                           rate=pct, origin_period=q))
                if missed <= 0:
                    continue
                if side == "employer":
                    slip.lines.append(Line(LT.EMPLOYER_GOSI, missed, ctx.account(GOSI_EXPENSE),
                                           f"GOSI {en} employer share missed for {q}", f"حصة صاحب العمل الفائتة عن {q}",
                                           branch=branch, rate=pct, origin_period=q))
                elif ps.cohort == "legacy_m33" or ps.nationality_class != Payslip.NationalityClass.SAUDI:
                    slip.lines.append(Line(LT.EMPLOYER_GOSI, missed, ctx.account(GOSI_EXPENSE),
                                           f"GOSI {en} employee share for {q} borne by the employer (SIL M/33 Art. 19(4))",
                                           f"حصة الموظف عن {q} يتحملها صاحب العمل (المادة 19(4))", branch=branch,
                                           rate=pct, origin_period=q, source_ref=f"borne:{branch}"))
                    slip.warn(f"Missed employee GOSI share for {q} ({missed}) is not deducted later: borne by the employer "
                              "(SIL M/33 Art. 19(4), R58).", f"لا تُستقطع حصة الموظف الفائتة عن {q} لاحقًا ويتحملها "
                              "صاحب العمل (المادة 19(4)).")
                else:
                    slip.lines.append(Line(LT.GOSI_CATCH_UP, missed, ctx.account(GOSI_PAYABLE),
                                           f"GOSI {en} employee share missed for {q}, deducted later with notice",
                                           f"استدراك حصة الموظف في {ar} عن {q} مع الإشعار", branch=branch, rate=pct,
                                           origin_period=q))
                    slip.notices.append((q, missed,
                                         f"Your GOSI {en} share for {q} ({missed} SAR) was not deducted then; it is "
                                         "deducted from this salary (Law Art. 9(1); P19(j)).",
                                         f"لم تُستقطع حصتك في {ar} عن {q} ({missed} ريال) في حينها؛ وتُستقطع من هذا الراتب."))


def _apply_caps(ctx, slip, emp, info):
    """R18 (Art. 91 to 93): the loan installment <= 10% of the actual wage; total deductions <= 50% of the wage due
    for the period. Installments and catch-up shares are reduced (rest re-planned / carried, warning); fixed one-off
    deductions above the cap refuse the payslip. Monthly runs only (P17)."""
    caps = ctx.p("deduction.caps", info.end)
    actual = _sum(info.version_on(info.last), "counts_for_eosb")
    due = slip.total(PayslipLine.EARNINGS) - slip.total(PayslipLine.REDUCTIONS)
    total_cap = _round(Fraction(due) * Fraction(caps["total_max_pct"]) / 100) if due > 0 else ZERO
    loan_cap = _round(Fraction(actual) * Fraction(caps["loan_max_pct"]) / 100)
    fixed = slip.total({LT.DEDUCTION})
    gosi_ee = [l for l in slip.lines if l.line_type == LT.EMPLOYEE_GOSI]
    room = total_cap - fixed
    if room < 0:
        slip.refuse(f"Deductions {fixed} exceed half of the wage due ({total_cap}) (Labour Law Art. 93, R18).",
                    f"الاستقطاعات {fixed} تتجاوز نصف الأجر المستحق ({total_cap}) (المادة 93).")
    for l in gosi_ee:  # the statutory share first; what cannot be covered is caught up later (R61(f), R58)
        if l.amount > room:
            short = l.amount - room
            slip.warn(f"Employee GOSI share reduced by {short} to stay within half of the wage due; deducted later (R58).",
                      f"خُفضت حصة الموظف في التأمينات بمقدار {short} للبقاء ضمن نصف الأجر؛ تُستقطع لاحقًا.")
            l.amount = room
            l.quantity = _round(Fraction(l.amount) * 100 / Fraction(l.rate)) if l.rate else l.quantity
        room -= l.amount
    slip.lines = [l for l in slip.lines if not (l.line_type == LT.EMPLOYEE_GOSI and l.amount <= 0)]
    loan_room = loan_cap
    for kind, effects in ((LT.LOAN_INSTALLMENT, slip.loan_effects), (LT.DEFERRED_SALE_INSTALLMENT, slip.sale_effects),
                          (LT.GOSI_CATCH_UP, None)):
        for l in [x for x in slip.lines if x.line_type == kind]:
            limit = room
            if kind == LT.LOAN_INSTALLMENT:
                limit = min(limit, loan_room)
            elif kind == LT.DEFERRED_SALE_INSTALLMENT and ctx.p("deferred_sale.apply_loan_cap", info.end).get("apply"):
                limit = min(limit, loan_room)
            take = max(ZERO, min(l.amount, limit))
            if take < l.amount:
                slip.warn(f"{l.desc_en}: {l.amount} reduced to {take} by the deduction limits (Art. 92(1), 93); the rest "
                          "is re-planned (R18).", f"{l.desc_ar}: خُفض {l.amount} إلى {take} بحدود الاستقطاع؛ يُعاد "
                          "جدولة الباقي.")
            if effects is not None:
                for i, (pk, amt) in enumerate(effects):
                    if pk == l.source_ref:
                        effects[i] = (pk, take)
            l.amount = take
            room -= take
            if kind in (LT.LOAN_INSTALLMENT,) or (kind == LT.DEFERRED_SALE_INSTALLMENT and
                                                  ctx.p("deferred_sale.apply_loan_cap", info.end).get("apply")):
                loan_room -= take
    slip.lines = [l for l in slip.lines if l.amount > 0 or l.line_type in PayslipLine.EARNINGS]


def _loan_lines(ctx, slip, emp):
    """R18, R27: planned installments of active loans up to this period, Cr 1230 (interest-free)."""
    for inst in LoanInstallment.objects.filter(loan__employee=emp, loan__status=EmployeeLoan.Status.ACTIVE,
                                               status=LoanInstallment.Status.PLANNED, period__lte=ctx.period) \
            .select_related("loan").order_by("period", "id"):
        ref = f"loan_installment:{inst.pk}"
        slip.lines.append(Line(LT.LOAN_INSTALLMENT, inst.amount, ctx.account(LOANS),
                               f"{INTEREST_FREE[0]} {inst.loan.number} installment {inst.period}",
                               f"{INTEREST_FREE[1]} {inst.loan.number} قسط {inst.period}", source_ref=ref))
        slip.loan_effects.append((ref, inst.amount))
    for inst in DeferredSaleInstallment.objects.filter(contract__employee=emp,
                                                       contract__status=DeferredSaleContract.Status.SOLD,
                                                       status=DeferredSaleInstallment.Status.PLANNED,
                                                       period__lte=ctx.period).select_related("contract"):
        if not inst.contract.consent_ref.strip():
            slip.warn("Bai' ajel installment not deducted: no signed consent (R49).", "لم يُستقطع قسط البيع الآجل: "
                      "لا توجد موافقة موقعة.")
            continue
        ref = f"sale_installment:{inst.pk}"
        slip.lines.append(Line(LT.DEFERRED_SALE_INSTALLMENT, inst.amount, ctx.account(BAI_AJEL),
                               f"Bai' ajel {inst.contract.number} installment {inst.period} (no increase)",
                               f"قسط البيع الآجل {inst.contract.number} لفترة {inst.period} (دون زيادة)", source_ref=ref))
        slip.sale_effects.append((ref, inst.amount))


def _eosb(ctx, slip, emp, info):
    """R24: required provision at period end (hr calculate_eosb, full award as on employer termination, OQ-11)
    minus the employee's sub-ledger balance; positive Dr 5120 / Cr 2500, negative the other way."""
    as_of = info.last
    try:
        result = hr.calculate_eosb(emp, as_of, Termination.Kind.EMPLOYER_TERMINATION, user=ctx.user)
    except PayrollError:
        raise
    except Exception as exc:  # noqa: BLE001 - hr refusals become warnings; the run is not blocked
        slip.warn(f"EOSB provision not computed: {exc}", f"لم يُحتسب مخصص نهاية الخدمة: {exc}")
        return
    balance = EosbProvisionEntry.objects.filter(employee=emp).aggregate(t=Sum("amount"))["t"] or ZERO
    amount = result.full_award - balance
    slip.eosb = (amount, result.full_award)
    if amount > 0:
        slip.lines.append(Line(LT.EOSB_ACCRUAL, amount, ctx.account(EOSB_EXPENSE), "End-of-service provision",
                               "مخصص مكافأة نهاية الخدمة"))
    elif amount < 0:
        slip.lines.append(Line(LT.EOSB_RELEASE, -amount, ctx.account(EOSB_EXPENSE), "End-of-service provision released",
                               "تحرير مخصص مكافأة نهاية الخدمة"))


def _ksa_warnings(ctx, slip, emp, info, prior):
    """R10, R57, R60, OQ-48 warnings (never block the run)."""
    if not (emp.gosi_number or "").strip():
        slip.warn("Unregistered at GOSI: work-injury cover may not apply (SIL Art. 31(2)); register by the last day of "
                  "the joining month (GOSI Regs Art. 6); late registration: annuities only, at most 2 years back, with "
                  "fines (Regs Art. 16).", "غير مسجل في التأمينات: قد لا تنطبق تغطية إصابات العمل؛ يُسجل قبل نهاية شهر "
                  "الالتحاق؛ التسجيل المتأخر للمعاشات فقط ولمدة لا تتجاوز سنتين مع الغرامات.")
    flag = hr._document_flag(emp, info.end)
    slip.document_flag = flag if isinstance(flag, str) else str(flag)
    if slip.document_flag == "expired":
        slip.warn("A required document is expired (hr R44).", "مستند مطلوب منتهي الصلاحية.")
    if prior and prior[-1].gosi_wage_full and slip.gosi_wage_full and prior[-1].gosi_wage_full != slip.gosi_wage_full:
        due = _period_bounds(info.period)[1]
        slip.warn(f"GOSI wage changed ({prior[-1].gosi_wage_full} -> {slip.gosi_wage_full}): update GOSI by {due} "
                  "(Regs Art. 6(2)(c)); after that only at GOSI's discretion (Regs Art. 21).",
                  f"تغير أجر الاشتراك: حدّث بيانات التأمينات قبل {due}؛ وبعده يكون التصحيح بتقدير التأمينات (المادة 21).")
    if slip.cohort == "legacy_m33" and prior and prior[-1].gosi_wage_full and slip.gosi_wage_full > prior[-1].gosi_wage_full:
        age = _age_on(emp, info.start)
        if age is not None and age >= 50:
            ctx.p("gosi.legacy_increase_after_50_ratio", info.end)
            slip.warn("Wage increase after age 50 (legacy cohort): contributions on the increase only within the ratio "
                      "set by the Regulations (SIL M/33 Art. 19(2)); ratio not set, full wage used (R60).",
                      "زيادة أجر بعد سن الخمسين: الاشتراك على الزيادة ضمن النسبة التي تحددها اللائحة؛ النسبة غير محددة.")
    for a in Assignment.objects.filter(employee=emp, kind=Assignment.Kind.SECONDMENT, start_date__lte=info.end,
                                       status__in=(Assignment.Status.APPROVED, Assignment.Status.ACTIVE,
                                                   Assignment.Status.ENDED)).select_related("receiving_entity"):
        if a.end_date and a.end_date < info.start:
            continue
        if a.receiving_entity is not None and a.receiving_entity.country == "SA":
            slip.warn(f"Seconded to {a.receiving_entity.code}: the receiving entity may have to register GOSI (Regs Art. "
                      "20(3)(a)); legal sign-off pending (OQ-48); home entity keeps paying.",
                      f"معار إلى {a.receiving_entity.code}: قد يلزم الكيان المستقبل التسجيل في التأمينات؛ بانتظار "
                      "الرأي القانوني.")


def _calc_ksa(ctx, emp, versions, info):
    nclass = _nationality_class(emp)
    v_last = info.version_on(info.last)
    slip = Slip(employee=emp, basis=emp.employment_basis, nationality_class=nclass, cohort=emp.gosi_cohort or "",
                currency=ctx.entity.base_currency, contract=v_last, paid_days=Decimal(info.paid_days))
    try:
        inputs = _employee_inputs(ctx, emp)
        for pk, (comp, amount) in _component_earnings(info).items():
            if amount:
                slip.lines.append(Line(LT.EARNING, amount, comp.expense_account, comp.name_en, comp.name_ar,
                                       component=comp, quantity=Decimal(info.paid_days)))
        handled = _maternity(ctx, slip, emp, info, inputs)
        employed = {d for d, _ in info.days}
        unpaid = sorted((hr._unpaid_days(emp, info.start, info.end) & employed) - handled)
        if unpaid:
            for acc, amount in _by_account(ctx, info, unpaid, "deducted_for_unpaid_leave").items():
                slip.lines.append(Line(LT.UNPAID_DEDUCTION, _round(amount), acc, f"Unpaid leave {len(unpaid)} days",
                                       f"إجازة بدون أجر {len(unpaid)} يوم", quantity=Decimal(len(unpaid))))
        for inp in [i for i in inputs if i.kind == IK.UNPAID_DAYS]:
            day = info.last
            for acc, amount in _by_account(ctx, info, [day], "deducted_for_unpaid_leave",
                                           Fraction(inp.quantity)).items():
                slip.lines.append(Line(LT.UNPAID_DEDUCTION, _round(amount), acc, f"Unpaid absence {inp.quantity} days",
                                       f"غياب بدون أجر {inp.quantity} يوم", quantity=inp.quantity,
                                       source_ref=f"input:{inp.pk}"))
        sick = Fraction(0)
        sick_days = 0
        for d in hr._live_sick_days(SickLeaveDay.objects.filter(employee=emp, date__range=(info.start, info.end))):
            if d.date not in employed or d.date in handled or d.official_holiday or d.pay_pct >= 100:
                continue
            wage = _sum(info.version_on(d.date), "counts_for_eosb")
            sick += Fraction(wage) / info.divisor * (100 - Fraction(d.pay_pct)) / 100
            sick_days += 1
        if sick:
            slip.lines.append(Line(LT.SICK_DEDUCTION, _round(sick), ctx.account(SALARIES),
                                   f"Sick-leave reduction {sick_days} days (Art. 117)",
                                   f"تخفيض الإجازة المرضية {sick_days} يوم (المادة 117)", quantity=Decimal(sick_days)))
        premium = Fraction(ctx.p("overtime.premium_pct_of_basic")["pct"]) / 100
        for inp in [i for i in inputs if i.kind == IK.OVERTIME_HOURS]:
            ctx.p("overtime.hour_divisor_rule")
            v = info.version_on(inp.overtime_date) if inp.overtime_date else v_last
            hours = Fraction(v.daily_hours)
            hourly = (Fraction(_sum(v, "counts_for_eosb")) / info.divisor / hours
                      + premium * Fraction(_sum(v, "counts_for_overtime_base")) / info.divisor / hours)
            amount = _round(hourly * Fraction(inp.quantity))
            slip.lines.append(Line(LT.OVERTIME, amount, ctx.account(SALARIES),
                                   f"Overtime {inp.quantity} h on {inp.overtime_date} (Art. 107)",
                                   f"عمل إضافي {inp.quantity} ساعة في {inp.overtime_date} (المادة 107)",
                                   quantity=inp.quantity, rate=_round_to(hourly, 4), source_ref=f"input:{inp.pk}"))
        for inp in inputs:
            if inp.kind in (IK.ONE_OFF_EARNING, IK.BONUS, IK.MATERNITY_SUPPORT):
                ltype = {IK.ONE_OFF_EARNING: LT.ONE_OFF_EARNING, IK.BONUS: LT.BONUS,
                         IK.MATERNITY_SUPPORT: LT.MATERNITY_SUPPORT}[inp.kind]
                slip.lines.append(Line(ltype, _round(inp.amount), _expense(ctx, inp.component), inp.reason_en or inp.kind,
                                       inp.reason_ar, component=inp.component, source_ref=f"input:{inp.pk}"))
            elif inp.kind == IK.ONE_OFF_DEDUCTION:
                slip.lines.append(Line(LT.DEDUCTION, _round(inp.amount), _expense(ctx, inp.component),
                                       inp.reason_en or "Deduction", inp.reason_ar, component=inp.component,
                                       source_ref=f"input:{inp.pk}"))
        prior = _prior_payslips(ctx, emp)
        if ctx.run.kind == PayrollRun.Kind.REGULAR:
            _prior_corrections(ctx, slip, emp, versions)
        g = _compute_gosi(ctx, slip, emp, info, cohort=slip.cohort, nclass=nclass, basis=emp.employment_basis)
        slip.gosi_wage, slip.gosi_wage_full = g.wage, g.wage_full
        slip.lines.extend(_gosi_lines(ctx, slip, g))
        _loan_lines(ctx, slip, emp)
        _apply_caps(ctx, slip, emp, info)
        if ctx.run.kind == PayrollRun.Kind.REGULAR:
            _eosb(ctx, slip, emp, info)
        _ksa_warnings(ctx, slip, emp, info, prior)
        lt = hr.LeaveType.objects.filter(entity=emp.entity, code="ANNUAL").first()
        if lt is not None:
            slip.leave_balance = hr._balance(emp, lt)
        net = slip.total(PayslipLine.EARNINGS) - slip.total(PayslipLine.REDUCTIONS) - slip.total(PayslipLine.DEDUCTIONS)
        if net < 0:
            slip.refuse(f"Net pay would be negative ({net}) (R18).", f"سيكون صافي الراتب سالبًا ({net}).")
    except _Refused:
        slip.lines = []
    return slip


def _calc_remuneration(ctx, emp, versions, info):
    """R41, R42, R62: monthly REMUNERATION in the contract currency (prorated for joining and leaving months), approved
    one-off items; GOSI annuities only for a subordinate Saudi; no EOSB, overtime, leave or Saudi caps."""
    v_last = info.version_on(info.last)
    cur = v_last.currency
    nclass = _nationality_class(emp)
    slip = Slip(employee=emp, basis=emp.employment_basis, nationality_class=nclass, cohort=emp.gosi_cohort or "",
                currency=cur, contract=v_last, paid_days=Decimal(info.paid_days))
    try:
        rate = Decimal("1")
        if cur.code != ctx.entity.base_currency_id:
            rate = ledger.get_rate(ctx.entity, cur.code, ctx.entity.base_currency_id, ctx.run.entry_date,
                                   user=ctx.user).rate
        slip.fx_rate = rate

        def sar(txn):
            return fx.convert(txn, rate) if cur.code != ctx.entity.base_currency_id else txn

        def add(ltype, txn, account, en, ar, **kw):
            txn = _round_to(txn, cur.minor_units)
            slip.lines.append(Line(ltype, sar(txn), account, en, ar, amount_txn=txn, **kw))

        for pk, (comp, exact) in _component_earnings(info, exact=True).items():
            add(LT.EARNING, exact, comp.expense_account, comp.name_en, comp.name_ar, component=comp,
                quantity=Decimal(info.paid_days))
        for inp in _employee_inputs(ctx, emp):
            if inp.kind in (IK.ONE_OFF_EARNING, IK.BONUS):
                add(LT.ONE_OFF_EARNING if inp.kind == IK.ONE_OFF_EARNING else LT.BONUS, Fraction(inp.amount),
                    _expense(ctx, inp.component), inp.reason_en or inp.kind, inp.reason_ar, component=inp.component,
                    source_ref=f"input:{inp.pk}")
            elif inp.kind == IK.ONE_OFF_DEDUCTION:
                add(LT.DEDUCTION, Fraction(inp.amount), _expense(ctx, inp.component), inp.reason_en or "Deduction",
                    inp.reason_ar, component=inp.component, source_ref=f"input:{inp.pk}")
        remuneration = sum((Fraction(c.monthly_amount) for c in _components(v_last)
                            if c.component.code == "REMUNERATION"), Fraction(0))
        g = _compute_gosi(ctx, slip, emp, info, cohort=slip.cohort, nclass=nclass, basis=emp.employment_basis,
                         remuneration_sar=_round(remuneration * Fraction(rate)) if nclass == "saudi" else None)
        slip.gosi_wage, slip.gosi_wage_full = g.wage, g.wage_full
        for line in _gosi_lines(ctx, slip, g):
            if line.line_type == LT.EMPLOYEE_GOSI:
                # R62: the SAR share converted into the payslip currency; the SAR amount is what the ledger books.
                txn = _round_to(Fraction(line.amount) / Fraction(rate), cur.minor_units)
                line.amount_txn, line.amount = txn, sar(txn)
            else:
                line.amount_txn = None
            slip.lines.append(line)
        if cur.code != ctx.entity.base_currency_id and len({l.account.pk for l in slip.lines
                                                            if l.line_type in PayslipLine.EARNINGS}) > 1:
            slip.refuse("Foreign-currency remuneration earnings use one expense account in slice 1 (R42).",
                        "تستخدم استحقاقات المكافأة بالعملة الأجنبية حساب مصروف واحدًا.")
        # Slice 1: earlier months of remuneration staff are not re-examined (retro and GOSI catch-up: slice 2).
        net = slip.total_txn(PayslipLine.EARNINGS) - slip.total_txn(PayslipLine.DEDUCTIONS)
        if net < 0:
            slip.refuse(f"Net pay would be negative ({net}) (R41).", f"سيكون صافي المكافأة سالبًا ({net}).")
    except _Refused:
        slip.lines = []
    return slip


# =====================================================================================================
# Runs (R6 to R13, R36, R39, R44, R50)
# =====================================================================================================

def _entry_date(period):
    return _period_bounds(period)[1]


@transaction.atomic
def create_run(entity, *, user, period, payroll_group=PayrollGroup.IN_COUNTRY, kind=PayrollRun.Kind.REGULAR,
               pay_date=None, reason_ar="", reason_en="", employees=()):
    """A draft run (``payroll.prepare_run``). One live regular run per (entity, period, group) (R6, R44); an
    off-cycle run needs an Arabic reason and named employees (R6). Both are dated the last day of the period (R12)."""
    _require(user, entity, "payroll.prepare_run")
    _jurisdiction_gate(entity, hr.ParamUse())
    _check_period(period)
    if payroll_group not in PayrollGroup.values or kind not in PayrollRun.Kind.values:
        raise error("Unknown payroll group or run kind.", "مجموعة رواتب أو نوع مسير غير معروف.", code="run_kind")
    if kind == PayrollRun.Kind.OFF_CYCLE and (not reason_ar.strip() or not employees):
        raise error("An off-cycle run needs a reason in Arabic and the named employees (R6).",
                    "يحتاج المسير خارج الدورة إلى سبب بالعربية وأسماء الموظفين.", code="off_cycle_reason")
    s = _settings_of(entity)
    start, end = _period_bounds(period)
    if pay_date is None:
        base = start if payroll_group == PayrollGroup.IN_COUNTRY else _period_bounds(_next_period(period))[0]
        pay_date = min(base.replace(day=min(s.default_pay_day, 28)), _period_bounds(_period_of(base))[1])
    run = PayrollRun(entity=entity, payroll_group=payroll_group, period=period, kind=kind, pay_date=pay_date,
                     entry_date=end, reason_ar=reason_ar, reason_en=reason_en, created_by=user)
    try:
        with transaction.atomic():
            _save(run)
    except IntegrityError:
        raise error(f"A regular {payroll_group} run for {period} already exists (R6, R44).",
                    f"يوجد مسير عادي {payroll_group} للفترة {period} مسبقًا.", code="run_exists") from None
    for emp in employees:
        if emp.entity_id != entity.pk:
            raise error("An employee of another entity cannot be in this run (R1).", "لا يدخل موظف كيان آخر في هذا المسير.",
                        code="entity_mismatch")
        run.off_cycle_employees.add(emp)
    return run


def _members(ctx):
    """R10, R41, R44: the group's employees with employed days in the period."""
    run = ctx.run
    bases = ((Basis.REMUNERATION,) if run.payroll_group == PayrollGroup.OUT_OF_COUNTRY
             else (Basis.KSA_EMPLOYEE, Basis.LOCAL_ENTITY_EMPLOYEE))
    qs = Employee.objects.filter(entity=ctx.entity, employment_basis__in=bases).exclude(status=Employee.Status.DRAFT)
    if run.kind == PayrollRun.Kind.OFF_CYCLE:
        qs = qs.filter(pk__in=run.off_cycle_employees.values("pk"))
    return list(qs.order_by("employee_code"))


def _calculate(ctx):
    """Every payslip of the run, in memory (deterministic, R8). Returns ([Slip], run warnings)."""
    slips = []
    for emp in _members(ctx):
        versions = _versions(emp)
        info = _employment(emp, ctx.period, ctx.s, versions)
        if info is None:
            continue
        term = _termination(emp)
        if term is not None and info.start <= term.last_working_day <= info.end and emp.employment_basis != Basis.REMUNERATION:
            ctx.warnings.append(f"{emp.employee_code}: termination approved, last working day {term.last_working_day}: "
                                f"paid through the final settlement, not the run (R10) / {emp.employee_code}: إنهاء خدمة "
                                f"معتمد بآخر يوم عمل {term.last_working_day}: يُدفع عبر المخالصة النهائية لا المسير")
            continue
        if emp.employment_basis == Basis.LOCAL_ENTITY_EMPLOYEE:
            continue  # refused at the jurisdiction gate; never computed with Saudi parameters (R41)
        calc = _calc_remuneration if emp.employment_basis == Basis.REMUNERATION else _calc_ksa
        slips.append(calc(ctx, emp, versions, info))
    if ctx.run.payroll_group == PayrollGroup.IN_COUNTRY and ctx.run.kind == PayrollRun.Kind.REGULAR:
        diff = _eosb_tie_out(ctx.entity, ctx.end)
        if diff:
            ctx.warnings.append(f"Account 2500 differs from the EOSB sub-ledger by {diff} (R25) / يختلف الحساب 2500 عن "
                                f"سجل مخصص نهاية الخدمة بمقدار {diff}")
    return slips


def _eosb_tie_out(entity, as_of):
    sub = EosbProvisionEntry.objects.filter(entity=entity).aggregate(t=Sum("amount"))["t"] or ZERO
    lines = JournalLine.objects.filter(entry__entity=entity, entry__status__in=("posted", "reversed"),
                                       account__code=EOSB_PROVISION, entry__date__lte=as_of) \
        .aggregate(d=Sum("debit"), c=Sum("credit"))
    book = (lines["c"] or ZERO) - (lines["d"] or ZERO)
    return book - sub


def _fingerprint(slips, warnings, params):
    def line(l):
        return [l.line_type, str(l.amount), str(l.amount_txn), l.account.code, l.component.pk if l.component else None,
                l.branch, str(l.quantity), str(l.rate), l.source_ref, l.origin_period]
    data = [[s.employee.pk, s.refusal, str(s.fx_rate), str(s.gosi_wage), str(s.eosb), s.maternity_status,
             sorted(s.loan_effects), sorted(s.sale_effects), [line(l) for l in s.lines], sorted(s.notices)]
            for s in slips]
    return hashlib.sha256(json.dumps([data, sorted(warnings), params], default=str).encode()).hexdigest()


def _store(ctx, slips):
    run = ctx.run
    for ps in Payslip.objects.filter(run=run):
        for line in PayslipLine.objects.filter(payslip=ps):
            line._service_delete = True
            line.delete()
        ps._service_delete = True
        ps.delete()
    for s in slips:
        ps = Payslip(entity=ctx.entity, run=run, employee=s.employee, contract=s.contract, employment_basis=s.basis,
                     nationality_class=s.nationality_class, cohort=s.cohort, currency=s.currency, fx_rate=s.fx_rate,
                     paid_days=s.paid_days, gosi_wage=s.gosi_wage, gosi_wage_full=s.gosi_wage_full,
                     maternity_status=s.maternity_status, maternity_days=s.maternity_days,
                     gosi_benefit_amount=s.gosi_benefit_amount, maternity_top_up=s.maternity_top_up,
                     pending_gosi_amount=s.pending_gosi_amount, legal_clearance_ref=s.legal_clearance_ref,
                     leave_balance_snapshot=s.leave_balance, document_flag=s.document_flag[:10],
                     refused=bool(s.refusal), refusal=s.refusal, warnings=s.warnings, notes=s.notes)
        gross = s.total(PayslipLine.EARNINGS) - s.total(PayslipLine.REDUCTIONS)
        ded = s.total(PayslipLine.DEDUCTIONS)
        ps.gross, ps.total_deductions, ps.net = gross, ded, gross - ded
        ps.employer_gosi = s.total({LT.EMPLOYER_GOSI})
        ps.employee_gosi = s.total({LT.EMPLOYEE_GOSI, LT.GOSI_CATCH_UP})
        ps.eosb_accrual = s.eosb[0] if s.eosb else ZERO
        if s.basis == Basis.REMUNERATION:
            ps.gross_txn = s.total_txn(PayslipLine.EARNINGS)
            ps.net_txn = ps.gross_txn - s.total_txn(PayslipLine.DEDUCTIONS)
        else:
            ps.gross_txn, ps.net_txn = ps.gross, ps.net
        outstanding = _loan_outstanding(s.employee)
        ps.loan_balance = outstanding - sum((amt for _, amt in s.loan_effects), ZERO)
        _save(ps)
        for l in s.lines:
            txn = l.amount_txn if l.amount_txn is not None else l.amount
            cur = s.currency if l.amount_txn is not None else ctx.entity.base_currency
            _save(PayslipLine(entity=ctx.entity, payslip=ps, line_type=l.line_type, component=l.component,
                              gosi_branch=l.branch, quantity=l.quantity, rate=l.rate, amount=l.amount, currency=cur,
                              amount_txn=txn, account=l.account, description_en=l.desc_en[:300],
                              description_ar=l.desc_ar[:300], source_ref=l.source_ref, origin_period=l.origin_period))


def _send_alerts(entity, alerts):
    for kind, key, en, ar, roles in alerts:
        for user in hr._recipients(entity, roles=roles):
            PayrollAlert.objects.get_or_create(entity=entity, recipient=user, kind=kind, key=key,
                                               defaults=dict(text_en=en, text_ar=ar))


@transaction.atomic
def calculate_run(run, *, user):
    """R8: (re)calculate a draft, calculated or returned run from the approved sources; replaces its payslips and
    lines, bumps ``calc_version`` and stores the result fingerprint (``inputs_hash``)."""
    run = _lock(PayrollRun, run.pk)
    _require(user, run.entity, "payroll.prepare_run")
    if run.status not in (RS.DRAFT, RS.CALCULATED, RS.RETURNED):
        raise error(f"A {run.status} run is not recalculated (R7, R13).", f"لا يُعاد احتساب مسير بحالة {run.status}.",
                    code="run_status")
    ctx = Ctx(run, user, _jurisdiction_gate(run.entity, hr.ParamUse()))
    slips = _calculate(ctx)
    _store(ctx, slips)
    run.status = RS.CALCULATED
    run.calc_version += 1
    run.parameter_ids = ctx.use.ids
    run.warnings = ctx.warnings + [w for s in slips for w in s.warnings] + [s.refusal for s in slips if s.refusal]
    run.inputs_hash = _fingerprint(slips, ctx.warnings, ctx.use.ids)
    run.calculated_by, run.calculated_at, run.modified_by, run.modified_at = user, _now(), user, _now()
    _save(run)
    _send_alerts(run.entity, [a for s in slips for a in s.alerts])
    return run


@transaction.atomic
def submit_run(run, *, user):
    """R7, R56: submit a calculated run (``payroll.submit_run``); refused while any payslip is refused."""
    run = _lock(PayrollRun, run.pk)
    _require(user, run.entity, "payroll.submit_run")
    if run.status != RS.CALCULATED:
        raise error("Only a calculated run is submitted.", "يُقدم المسير المحتسب فقط.", code="run_status")
    refused = list(Payslip.objects.filter(run=run, refused=True).values_list("refusal", flat=True))
    if refused:
        raise _problems(["The run cannot be submitted while payslips are refused (R56). / لا يُقدم المسير ما دامت "
                         "قسائم مرفوضة.", *refused], "payslips_refused")
    if not Payslip.objects.filter(run=run).exists():
        raise error("The run has no payslips.", "لا توجد قسائم في المسير.", code="run_empty")
    run.status, run.submitted_by, run.submitted_at = RS.SUBMITTED, user, _now()
    _save(run)
    return run


@transaction.atomic
def return_run(run, *, user, reason_ar):
    """R7: the approver returns a submitted run for recalculation (``payroll.approve_run``)."""
    run = _lock(PayrollRun, run.pk)
    _require(user, run.entity, "payroll.approve_run", document=run)
    if run.status != RS.SUBMITTED or not (reason_ar or "").strip():
        raise error("Only a submitted run is returned, with a reason in Arabic.",
                    "يُعاد المسير المقدم فقط مع سبب بالعربية.", code="run_status")
    run.status, run.returned_reason_ar = RS.RETURNED, reason_ar
    _save(run)
    return run


_posting = ContextVar("payroll_posting_document", default=None)
_reversing = ContextVar("payroll_reversing_document", default=None)


@contextmanager
def _marker(var, document):
    token = var.set((document._meta.label_lower, document.pk))
    try:
        yield
    finally:
        var.reset(token)


def _require_posting(document):
    if _posting.get() != (document._meta.label_lower, document.pk):
        raise error(f"{document._meta.label} posts only through payroll.services.",
                    f"يُرحَّل المستند {document._meta.label} من خلال خدمات الرواتب فقط.", code="post_through_services")


def _reverse_guard(document):
    return _reversing.get() == (document._meta.label_lower, document.pk)


def _next_number(entity, kind, year):
    seq, _ = PayrollSequence.objects.select_for_update(of=("self",)).get_or_create(entity=entity, kind=kind, year=year)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"{entity.code}-{kind}-{year}-{seq.last_number:06d}"


def _approver_checks(run, user):
    """R39, R50 (defaults OQ-14, OQ-36): an approver whose own payslip in the run has inputs, or who created or
    approved an input taken by the run, is refused for it."""
    employee_ids = list(Payslip.objects.filter(run=run).values_list("employee_id", flat=True))
    inputs = PayrollInput.objects.filter(employee_id__in=employee_ids, status=IS.APPROVED, payroll_period=run.period)
    own = Employee.objects.filter(pk__in=employee_ids, user=user).first()
    if own is not None and (inputs.filter(employee=own).exists()
                            or PayslipLine.objects.filter(payslip__run=run, payslip__employee=own,
                                                          line_type=LT.RETRO).exists()):
        access.deny(user, run.entity, "payroll.approve_run", "self_approval")
    if inputs.filter(Q(created_by=user) | Q(approved_by=user)).exists():
        access.deny(user, run.entity, "payroll.approve_run", "self_approval")


@transaction.atomic
def approve_run(run, *, user):
    """R8, R11, R12, R50: the hr_officer approves a submitted run (A: never its creator, submitter or last modifier);
    approval recomputes the run (refused "recalculate" if any source changed, the grant stays unused), assigns the
    number and posts the entry through the registry, dated the last day of the period. A ledger refusal (closed
    period, Sharia-blocked account) leaves the run submitted."""
    run = PayrollRun.objects.select_related("entity").get(pk=run.pk)
    grant = _require(user, run.entity, "payroll.approve_run", document=run)
    approvals.check(grant, run.entity, "payroll.run")
    _approver_checks(run, user)
    run = _lock(PayrollRun, run.pk)
    if run.status != RS.SUBMITTED:
        raise error("Only a submitted run is approved (R7).", "يُعتمد المسير المقدم فقط.", code="run_status")
    s = _jurisdiction_gate(run.entity, hr.ParamUse())
    ctx = Ctx(run, user, s)
    slips = _calculate(ctx)
    if _fingerprint(slips, ctx.warnings, ctx.use.ids) != run.inputs_hash:
        raise error("A source changed after the calculation: recalculate the run and submit it again (R8).",
                    "تغيّر مصدر بعد الاحتساب: أعد احتساب المسير وقدّمه مجددًا.", code="recalculate")
    ctx.use.check_verified()
    run.number = _next_number(run.entity, "PAY", int(run.period[:4]))
    run.status, run.approved_by, run.approved_at = RS.APPROVED, user, _now()
    _save(run)
    try:
        with _marker(_posting, run):
            entry = ledger.create_entry(run.entity, run.entry_date, f"Payroll {run.number} {run.period} / مسير رواتب "
                                        f"{run.number} {run.period}", None, user=user, reference=run.number, post=True,
                                        grant=grant, document=run)
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "posting_refused") from None
    run.journal_entry = entry
    _save(run)
    _apply_effects(run, slips)
    ctx.use.mark_used()
    return run


def _apply_effects(run, slips):
    """Approval effects: installments deducted (a reduced one carries its remainder to the next period, R18),
    EOSB sub-ledger rows (R24), catch-up notices (R58)."""
    for s in slips:
        if s.refusal:
            continue
        for ref, amount in s.loan_effects:
            inst = _lock(LoanInstallment, int(ref.split(":")[1]))
            _deduct_installment(inst, amount, run, LoanInstallment)
        for ref, amount in s.sale_effects:
            inst = _lock(DeferredSaleInstallment, int(ref.split(":")[1]))
            _deduct_installment(inst, amount, run, DeferredSaleInstallment)
        if s.eosb and s.eosb[0]:
            _save(EosbProvisionEntry(entity=run.entity, employee=s.employee, date=run.entry_date,
                                     kind=EosbProvisionEntry.Kind.ACCRUAL, amount=s.eosb[0], run=run,
                                     required_provision=s.eosb[1]))
        for origin, amount, en, ar in s.notices:
            _save(GosiCatchUpNotice(entity=run.entity, employee=s.employee, run=run, period_of_origin=origin,
                                    amount=amount, text_en=en, text_ar=ar))
    for loan in EmployeeLoan.objects.filter(entity=run.entity, status=EmployeeLoan.Status.ACTIVE):
        if _loan_outstanding_of(loan) == 0:
            loan.status = EmployeeLoan.Status.SETTLED
            _save(loan)


def _deduct_installment(inst, amount, run, model):
    inst.deducted_amount, inst.run = amount, run
    inst.status = model.Status.DEDUCTED
    _save(inst)
    rest = inst.amount - amount
    if rest > 0:
        parent = inst.loan if model is LoanInstallment else inst.contract
        last = parent.installments.exclude(status__in=("superseded",)).order_by("-period").first().period
        kwargs = dict(entity=inst.entity, period=_next_period(max(last, run.period)), amount=rest, carried_from=inst)
        if model is LoanInstallment:
            kwargs.update(loan=parent, schedule_version=inst.schedule_version)
        else:
            kwargs.update(contract=parent)
        _save(model(**kwargs))


def _pending_runs(entity, start_date, end_date):
    """Registered ``pending_documents``: submitted runs dated in the range block close_period (R12)."""
    return [f"payroll run {r.period} {r.payroll_group} (submitted)" for r in
            PayrollRun.objects.filter(entity=entity, status=RS.SUBMITTED, entry_date__range=(start_date, end_date))]


def _run_lines(run):
    """Registered ``build_lines`` for PayrollRun (§5 E1, E8a, E15; R36): summarised per account (and currency); no
    names, IDs or individual pay in descriptions. Foreign remuneration lines are booked as matching pairs so the SAR
    amounts balance exactly (R42, R62)."""
    _require_posting(run)
    label = f"{run.number} {run.period}"
    sar = {}
    foreign = {}
    pairs = []

    def add(acc, amount):
        sar[acc.pk] = sar.get(acc.pk, [acc, ZERO])
        sar[acc.pk][1] += amount

    payable = _account(run.entity, SALARIES_PAYABLE)
    for ps in Payslip.objects.filter(run=run, refused=False).select_related("currency"):
        lines = list(ps.lines.select_related("account"))
        if ps.employment_basis == Basis.REMUNERATION and ps.currency_id != run.entity.base_currency_id:
            # Every credit in the payslip currency is matched by a debit of the same amount on the earnings account,
            # so each pair converts to the same SAR amount (one earnings account per payslip, checked at calculation).
            cur = ps.currency_id
            debit_acc = next(l.account for l in lines if l.line_type in PayslipLine.EARNINGS)
            for l in lines:
                if l.line_type == LT.EMPLOYEE_GOSI:
                    pairs.append((cur, debit_acc, l.amount_txn, l.amount))
                elif l.line_type in PayslipLine.DEDUCTIONS:
                    key = (cur, debit_acc.pk, l.account.pk)
                    foreign.setdefault(key, [debit_acc, l.account, ZERO])[2] += l.amount_txn
                elif l.line_type == LT.EMPLOYER_GOSI:
                    add(l.account, l.amount)
                    add(_account(run.entity, GOSI_PAYABLE), -l.amount)
            if ps.net_txn:
                key = (cur, debit_acc.pk, payable.pk)
                foreign.setdefault(key, [debit_acc, payable, ZERO])[2] += ps.net_txn
            continue
        for l in lines:
            t = l.line_type
            if t in PayslipLine.EARNINGS:
                add(l.account, l.amount)
            elif t in PayslipLine.REDUCTIONS:
                add(l.account, -l.amount)
            elif t in PayslipLine.DEDUCTIONS:
                add(l.account, -l.amount)
            elif t == LT.EMPLOYER_GOSI:
                add(l.account, l.amount)
                add(_account(run.entity, GOSI_PAYABLE), -l.amount)
            elif t == LT.EOSB_ACCRUAL:
                add(l.account, l.amount)
                add(_account(run.entity, EOSB_PROVISION), -l.amount)
            elif t == LT.EOSB_RELEASE:
                add(l.account, -l.amount)
                add(_account(run.entity, EOSB_PROVISION), l.amount)
        add(payable, -ps.net)
    out = []
    for acc, amount in sar.values():
        if amount > 0:
            out.append({"account": acc, "debit": amount, "description": label})
        elif amount < 0:
            out.append({"account": acc, "credit": -amount, "description": label})
    gosi = _account(run.entity, GOSI_PAYABLE)
    for (cur, _d, _c), (debit_acc, credit_acc, amount) in foreign.items():
        out.append({"account": debit_acc, "currency": cur, "txn_debit": amount, "description": label})
        out.append({"account": credit_acc, "currency": cur, "txn_credit": amount, "description": label})
    for cur, debit_acc, txn, amount_sar in pairs:
        # R62: the employee GOSI share leaves the payslip currency at the run's rate and is owed to GOSI in SAR.
        out.append({"account": debit_acc, "currency": cur, "txn_debit": txn, "description": label})
        out.append({"account": gosi, "credit": amount_sar, "description": label})
    return out


def _reversal_date(entity, original, requested):
    if requested is not None:
        return requested
    period = FiscalPeriod.objects.filter(entity=entity, start_date__lte=original, end_date__gte=original).first()
    if period is not None and period.status == FiscalPeriod.Status.OPEN:
        return original
    later = FiscalPeriod.objects.filter(entity=entity, status=FiscalPeriod.Status.OPEN, start_date__gt=original) \
        .order_by("start_date").first()
    if later is None:
        raise error("No open period for the reversal; pass a date.", "لا توجد فترة مفتوحة للعكس؛ حدّد تاريخًا.",
                    code="no_open_period")
    return later.start_date


@transaction.atomic
def reverse_run(run, *, user, reason, date=None):
    """R13 (E6): ``payroll.reverse_run`` by an approver who did not prepare the run, only while no payment of the run
    is approved; the reversal is dated in an open period; installments go back to planned (carried remainders are
    superseded), EOSB sub-ledger rows are mirrored. A reversed run is never re-approved."""
    run = PayrollRun.objects.select_related("entity", "journal_entry").get(pk=run.pk)
    grant = _require(user, run.entity, "payroll.reverse_run", document=run)
    approvals.check(grant, run.entity, "payroll.reversal")
    if user.pk in {run.created_by_id, run.calculated_by_id, run.submitted_by_id, run.modified_by_id}:
        access.deny(user, run.entity, "payroll.reverse_run", "self_approval")
    if not (reason or "").strip():
        raise error("A reason is required.", "السبب مطلوب.", code="reason_required")
    run = _lock(PayrollRun, run.pk)
    if run.status != RS.APPROVED or run.journal_entry_id is None:
        raise error("Only an approved, unpaid run is reversed (R13).", "يُعكس المسير المعتمد غير المدفوع فقط.",
                    code="run_status")
    if PayrollPayment.objects.filter(run=run, status=PayrollPayment.Status.APPROVED).exists():
        raise error("A payment of this run is approved: correct it in a later run instead (R13).",
                    "اعتُمدت دفعة لهذا المسير: صحّحه في مسير لاحق.", code="run_paid")
    on = _reversal_date(run.entity, run.entry_date, date)
    try:
        with _marker(_reversing, run):
            reversal = ledger.reverse_entry(run.journal_entry, grant, date=on, reason=reason.strip())
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "reversal_refused") from None
    for model in (LoanInstallment, DeferredSaleInstallment):
        for inst in model._base_manager.select_for_update().filter(run=run):
            inst.status, inst.deducted_amount, inst.run = model.Status.PLANNED, ZERO, None
            _save(inst)
            for carry in model._base_manager.filter(carried_from=inst):
                if model is LoanInstallment:
                    carry.status = LoanInstallment.Status.SUPERSEDED
                    _save(carry)
                else:
                    carry._service_delete = True
                    carry.delete()
    for row in EosbProvisionEntry.objects.filter(run=run, kind=EosbProvisionEntry.Kind.ACCRUAL):
        _save(EosbProvisionEntry(entity=run.entity, employee=row.employee, date=on, kind=EosbProvisionEntry.Kind.REVERSAL,
                                 amount=-row.amount, run=run))
    for loan in EmployeeLoan.objects.filter(entity=run.entity, status=EmployeeLoan.Status.SETTLED):
        if _loan_outstanding_of(loan) > 0:
            loan.status = EmployeeLoan.Status.ACTIVE
            _save(loan)
    run.status, run.reversal_entry, run.reversed_by, run.reversed_at = RS.REVERSED, reversal, user, _now()
    _save(run)
    return run


# =====================================================================================================
# Loans (R26, R27, R46) and bai' ajel (R47 to R49)
# =====================================================================================================

def _loan_outstanding_of(loan):
    if loan.status not in (EmployeeLoan.Status.ACTIVE, EmployeeLoan.Status.SETTLED):
        return ZERO
    deducted = loan.installments.filter(status__in=(LoanInstallment.Status.DEDUCTED,
                                                    LoanInstallment.Status.SETTLED_IN_FINAL)) \
        .aggregate(t=Sum("deducted_amount"))["t"] or ZERO
    return loan.principal - deducted


def _loan_outstanding(emp):
    return sum((_loan_outstanding_of(l) for l in EmployeeLoan.objects.filter(employee=emp,
                                                                            status=EmployeeLoan.Status.ACTIVE)), ZERO)


def _refuse_charges(extra):
    if any(any(w in str(k).lower() for w in INTEREST_WORDS) for k in extra):
        raise error(*SHARIA_NO_CHARGE, code="sharia_no_charge")
    if extra:
        raise error(f"Unknown fields: {sorted(extra)}.", f"حقول غير معروفة: {sorted(extra)}.", code="fields")


def _schedule(installments, total, what_en, what_ar):
    rows = [(p, _dec(a, "installment")) for p, a in installments]
    for p, a in rows:
        _check_period(p)
        if a <= 0:
            raise error("Installments are above zero.", "الأقساط أكبر من صفر.", code="schedule_invalid")
    if sum((a for _, a in rows), ZERO) != total:
        raise error(f"The installments must add up to the {what_en} {total} exactly (R26).",
                    f"يجب أن تساوي الأقساط {what_ar} {total} تمامًا.", code="schedule_sum")
    return rows


@transaction.atomic
def create_loan(employee, *, user, kind, principal, disbursement_date, purpose_ar, installments, purpose_en="",
                disbursement_account="1100", **extra):
    """R26, R27: draft an interest-free loan or salary advance (``payroll.create_loan``) with its schedule. No interest,
    profit, fee or penalty in any entity; the installments add up to the principal exactly; an advance is one
    installment; ksa_employee only (OQ-32 default)."""
    emp = Employee.objects.select_related("entity").get(pk=employee.pk)
    _require(user, emp.entity, "payroll.create_loan")
    _refuse_charges(extra)
    if emp.employment_basis != Basis.KSA_EMPLOYEE:
        raise error("Loans are for KSA employees only (OQ-32 default).", "القروض لموظفي المملكة فقط.", code="loan_basis")
    principal = _dec(principal, "principal")
    if principal <= 0 or not fx.has_places(principal, 2):
        raise error("The principal is above zero with 2 decimals.", "أصل القرض أكبر من صفر بخانتين عشريتين.",
                    code="loan_invalid")
    if kind not in EmployeeLoan.Kind.values or not (purpose_ar or "").strip():
        raise error("A loan needs its kind and purpose in Arabic.", "يحتاج القرض إلى نوعه وغرضه بالعربية.",
                    code="loan_invalid")
    rows = _schedule(installments, principal, "principal", "أصل القرض")
    if kind == EmployeeLoan.Kind.SALARY_ADVANCE and len(rows) != 1:
        raise error("A salary advance is one installment in the next run (3.6).", "السلفة قسط واحد في المسير التالي.",
                    code="advance_one_installment")
    loan = _save(EmployeeLoan(entity=emp.entity, employee=emp, kind=kind, principal=principal,
                              disbursement_date=disbursement_date, purpose_ar=purpose_ar, purpose_en=purpose_en,
                              disbursement_account=_account(emp.entity, disbursement_account), created_by=user))
    for p, a in rows:
        _save(LoanInstallment(entity=emp.entity, loan=loan, schedule_version=1, period=p, amount=a))
    return loan


@transaction.atomic
def submit_loan(loan, *, user):
    loan = _lock(EmployeeLoan, loan.pk)
    _require(user, loan.entity, "payroll.create_loan")
    if loan.status != EmployeeLoan.Status.DRAFT:
        raise error("Only a draft loan is submitted.", "يُقدم القرض المسودة فقط.", code="loan_status")
    loan.status, loan.submitted_by, loan.submitted_at = EmployeeLoan.Status.SUBMITTED, user, _now()
    return _save(loan)


@transaction.atomic
def approve_loan(loan, *, user):
    """R46 (P12, P20): ``payroll.approve_loan`` by the matrix approver (MP); the principal plus the employee's
    outstanding loans and advances <= 3 x BASIC in force on the approval date (cap and BASIC stored); approval posts
    the disbursement E4 (Dr 1230 / Cr bank)."""
    loan = EmployeeLoan.objects.select_related("entity", "employee").get(pk=loan.pk)
    grant = _require(user, loan.entity, "payroll.approve_loan", document=loan)
    approvals.check(grant, loan.entity, "payroll.loan")
    loan = _lock(EmployeeLoan, loan.pk)
    if loan.status != EmployeeLoan.Status.SUBMITTED:
        raise error("Only a submitted loan is approved.", "يُعتمد القرض المقدم فقط.", code="loan_status")
    terms = _terms(loan.employee, _today())
    basic = _basic(terms)
    cap = basic * 3
    total = loan.principal + _loan_outstanding(loan.employee)
    if total > cap:
        raise error(f"Profit-free loans and advances are at most 3 basic salaries ({cap}); with this one they would be "
                    f"{total} (P12, R46). Larger financing: a Sharia-compliant bai' ajel (R47).",
                    f"لا تتجاوز القروض الحسنة والسلف ثلاثة رواتب أساسية ({cap})؛ ومع هذا القرض تبلغ {total}. للتمويل "
                    "الأكبر: البيع الآجل المتوافق مع الشريعة.", code="loan_cap")
    loan.cap_basic_at_approval, loan.cap_amount = basic, cap
    loan.number = _next_number(loan.entity, "LOAN", loan.disbursement_date.year)
    loan.status, loan.approved_by, loan.approved_at = EmployeeLoan.Status.ACTIVE, user, _now()
    _save(loan)
    try:
        with _marker(_posting, loan):
            entry = ledger.create_entry(loan.entity, loan.disbursement_date,
                                        f"Interest-free employee loan {loan.number} / قرض حسن للموظف {loan.number}",
                                        None, user=user, reference=loan.number, post=True, grant=grant, document=loan)
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "posting_refused") from None
    loan.journal_entry = entry
    return _save(loan)


def _loan_disbursement_lines(loan):
    """Registered ``build_lines`` for EmployeeLoan: E4, Dr 1230 / Cr bank. No interest or fee line ever exists."""
    _require_posting(loan)
    label = f"{loan.number} (interest-free / قرض حسن)"
    return [{"account": _account(loan.entity, LOANS), "debit": loan.principal, "description": label},
            {"account": loan.disbursement_account, "credit": loan.principal, "description": label}]


@transaction.atomic
def propose_schedule(loan, *, user, installments):
    """R27: rescheduling or skipping is a new schedule version (proposed, then approved by the loan approver); it
    adds nothing to the outstanding principal (R26)."""
    loan = _lock(EmployeeLoan, loan.pk)
    _require(user, loan.entity, "payroll.create_loan")
    if loan.status != EmployeeLoan.Status.ACTIVE:
        raise error("Only an active loan is rescheduled.", "يُعاد جدولة القرض النشط فقط.", code="loan_status")
    rows = _schedule(installments, _loan_outstanding_of(loan), "outstanding balance", "الرصيد القائم")
    for inst in loan.installments.filter(status=LoanInstallment.Status.PROPOSED):
        inst._service_delete = True
        inst.delete()
    for p, a in rows:
        _save(LoanInstallment(entity=loan.entity, loan=loan, schedule_version=loan.schedule_version + 1, period=p,
                              amount=a, status=LoanInstallment.Status.PROPOSED))
    loan.modified_by, loan.modified_at = user, _now()
    return _save(loan)


@transaction.atomic
def approve_schedule(loan, *, user):
    loan = EmployeeLoan.objects.get(pk=loan.pk)
    grant = _require(user, loan.entity, "payroll.approve_loan", document=loan)
    approvals.check(grant, loan.entity, "payroll.loan")
    loan = _lock(EmployeeLoan, loan.pk)
    proposed = list(loan.installments.filter(status=LoanInstallment.Status.PROPOSED))
    if not proposed:
        raise error("No proposed schedule.", "لا يوجد جدول مقترح.", code="no_schedule")
    for inst in loan.installments.filter(status=LoanInstallment.Status.PLANNED):
        inst.status = LoanInstallment.Status.SUPERSEDED
        _save(inst)
    for inst in proposed:
        inst.status = LoanInstallment.Status.PLANNED
        _save(inst)
    loan.schedule_version += 1
    return _save(loan)


def skip_installment(installment, *, user):
    """R27: an installment is never just skipped; a new approved schedule version is needed."""
    raise error("An installment is skipped only through a new approved schedule version (R27).",
                "لا يُتخطى القسط إلا بنسخة جدول جديدة معتمدة.", code="skip_needs_schedule")


@transaction.atomic
def create_deferred_sale(employee, *, user, asset_description_ar, cost, profit_amount, term_months, first_period,
                         consent_ref="", final_dues_undertaking=False, asset_description_en="", **extra):
    """R47 step 1: the payroll officer drafts a bai' ajel request. Fixed profit, never time-accruing; no rate, late fee
    or penalty field (R48); a final-dues undertaking only with the signed consent (3.11)."""
    emp = Employee.objects.select_related("entity").get(pk=employee.pk)
    _require(user, emp.entity, "payroll.create_deferred_sale")
    _refuse_charges(extra)
    if emp.employment_basis != Basis.KSA_EMPLOYEE:
        raise error("Bai' ajel is for KSA employees only (OQ-32 default).", "البيع الآجل لموظفي المملكة فقط.",
                    code="sale_basis")
    cost, profit = _dec(cost, "cost"), _dec(profit_amount, "profit_amount")
    if cost <= 0 or profit < 0 or int(term_months) < 1 or not (asset_description_ar or "").strip():
        raise error("Cost above zero, profit zero or more, a term and the asset in Arabic are required.",
                    "يلزم تكلفة أكبر من صفر وربح لا يقل عن صفر ومدة ووصف الأصل بالعربية.", code="sale_invalid")
    if final_dues_undertaking and not consent_ref.strip():
        raise error("The final-dues undertaking needs the signed consent reference (3.11, R49).",
                    "يحتاج التعهد بالاستقطاع من المستحقات النهائية إلى مرجع الموافقة الموقعة.", code="undertaking")
    _check_period(first_period)
    return _save(DeferredSaleContract(entity=emp.entity, employee=emp, asset_description_ar=asset_description_ar,
                                      asset_description_en=asset_description_en, cost=cost, profit_amount=profit,
                                      term_months=term_months, first_period=first_period, consent_ref=consent_ref,
                                      final_dues_undertaking=final_dues_undertaking, created_by=user))


@transaction.atomic
def submit_deferred_sale_terms(contract, *, user):
    c = _lock(DeferredSaleContract, contract.pk)
    _require(user, c.entity, "payroll.create_deferred_sale")
    if c.status != DeferredSaleContract.Status.DRAFT:
        raise error("Only draft terms are submitted.", "تُقدم الشروط المسودة فقط.", code="sale_status")
    c.status, c.submitted_by = DeferredSaleContract.Status.TERMS_SUBMITTED, user
    return _save(c)


@transaction.atomic
def approve_deferred_sale_terms(contract, *, user):
    """R47 step 2: the matrix approver (MP in Almiras) fixes term and profit."""
    c = DeferredSaleContract.objects.get(pk=contract.pk)
    grant = _require(user, c.entity, "payroll.approve_deferred_sale_terms", document=c)
    approvals.check(grant, c.entity, "payroll.deferred_sale_terms")
    c = _lock(DeferredSaleContract, c.pk)
    if c.status != DeferredSaleContract.Status.TERMS_SUBMITTED:
        raise error("Only submitted terms are approved.", "تُعتمد الشروط المقدمة فقط.", code="sale_status")
    c.status, c.terms_approved_by, c.terms_approved_at = DeferredSaleContract.Status.TERMS_APPROVED, user, _now()
    return _save(c)


@transaction.atomic
def approve_deferred_sale(contract, *, user):
    """R47 steps 3 to 5 (P19(k), P20(h)): the matrix approver approves the sale; invoicing creates and posts the sale
    invoice at 15% VAT on the sale price, charged to the employee (invoicing R112, through ``invoicing_port``). The
    schedule then spreads sale price + VAT over the term exactly; installments are deducted with the signed consent
    within the 50% cap (no 10% loan cap, R49)."""
    c = DeferredSaleContract.objects.get(pk=contract.pk)
    grant = _require(user, c.entity, "payroll.approve_deferred_sale", document=c)
    approvals.check(grant, c.entity, "payroll.deferred_sale")
    c = _lock(DeferredSaleContract, c.pk)
    if c.status != DeferredSaleContract.Status.TERMS_APPROVED:
        raise error("The terms must be approved first (R47).", "يجب اعتماد الشروط أولًا.", code="sale_status")
    if not c.consent_ref.strip():
        raise error("The sale needs the employee's signed consent to salary deductions (R49).",
                    "يحتاج البيع إلى موافقة الموظف الموقعة على الاستقطاع من الراتب.", code="consent_required")
    result = invoicing_port.get_port().create_bai_ajel_sale(c, user=user)
    c.sale_document_ref, c.vat_amount = result.document_ref, _dec(result.vat_amount, "vat_amount")
    c.number = _next_number(c.entity, "BAJ", int(c.first_period[:4]))
    c.status = DeferredSaleContract.Status.SOLD
    _save(c)
    total = c.receivable
    n = int(c.term_months)
    each = _round(Fraction(total) / n)
    period = c.first_period
    for i in range(n):
        amount = each if i < n - 1 else total - each * (n - 1)
        _save(DeferredSaleInstallment(entity=c.entity, contract=c, period=period, amount=amount))
        period = _next_period(period)
    return c


# =====================================================================================================
# Payments (R29, R43; §5 E2, E3, E8b)
# =====================================================================================================

def _run_payable(run, currency):
    """Net pay of the run in ``currency`` (txn amounts) and what approved payments already paid."""
    qs = Payslip.objects.filter(run=run, refused=False, currency_id=currency)
    total = qs.aggregate(t=Sum("net_txn"))["t"] or ZERO
    paid = PayrollPayment.objects.filter(run=run, kind=PayrollPayment.Kind.NET_SALARIES, currency_id=currency,
                                         status=PayrollPayment.Status.APPROVED).aggregate(t=Sum("amount"))["t"] or ZERO
    return total, paid


def _gosi_open(entity, period):
    total = PayslipLine.objects.filter(entity=entity, payslip__run__period=period, payslip__refused=False,
                                       payslip__run__status__in=(RS.APPROVED, RS.PAID),
                                       line_type__in=(LT.EMPLOYEE_GOSI, LT.EMPLOYER_GOSI, LT.GOSI_CATCH_UP)) \
        .aggregate(t=Sum("amount"))["t"] or ZERO
    paid = PayrollPayment.objects.filter(entity=entity, kind=PayrollPayment.Kind.GOSI, gosi_period=period,
                                         status=PayrollPayment.Status.APPROVED).aggregate(t=Sum("amount"))["t"] or ZERO
    return total - paid


@transaction.atomic
def record_payment(entity, *, user, kind, payment_date, amount, run=None, gosi_period="", bank_account=None,
                   currency=None, wht_category=None, bank_reference=""):
    """R29: the payroll officer records a payment after the bank release (``payroll.record_payment``). Net salaries
    of a run (per currency) or the GOSI contributions of a period. Remuneration staff in a foreign currency: WHT is
    considered on every payment (R43): a WHT category is required (rate from invoicing's global table)."""
    _require(user, entity, "payroll.record_payment")
    amount = _dec(amount, "amount")
    s = _settings_of(entity)
    if kind == PayrollPayment.Kind.NET_SALARIES:
        if run is None or run.entity_id != entity.pk:
            raise error("A salary payment names a run of the entity (R30).", "تحدد دفعة الرواتب مسيرًا للكيان.",
                        code="payment_run")
        currency = currency or entity.base_currency_id
    else:
        _check_period(gosi_period)
        currency = entity.base_currency_id
    cur = Currency.objects.get(code=getattr(currency, "code", currency))
    if amount <= 0 or not fx.has_places(amount, cur.minor_units):
        raise error(f"The amount is above zero with at most {cur.minor_units} decimals for {cur.code} (R5).",
                    f"المبلغ أكبر من صفر بحد أقصى {cur.minor_units} خانات عشرية للعملة {cur.code}.", code="amount_invalid")
    remuneration = kind == PayrollPayment.Kind.NET_SALARIES and Payslip.objects.filter(
        run=run, employment_basis=Basis.REMUNERATION).exists()
    if remuneration and wht_category is None:
        raise error("Remuneration payments need the WHT category (R43; category to confirm, OQ-29).",
                    "تحتاج دفعات المكافآت إلى فئة ضريبة الاستقطاع.", code="wht_category_required")
    acc = bank_account if isinstance(bank_account, Account) else _account(entity, bank_account) if bank_account \
        else s.salary_bank_account
    payment = PayrollPayment(entity=entity, kind=kind, run=run, gosi_period=gosi_period, payment_date=payment_date,
                             bank_account=acc, currency=cur, amount=amount, wht_category=wht_category,
                             bank_reference=bank_reference, created_by=user)
    _payment_room(payment)
    return _save(payment)


def _payment_room(p):
    if p.kind == PayrollPayment.Kind.NET_SALARIES:
        if p.run.status not in (RS.APPROVED, RS.PAID):
            raise error("Only an approved run is paid.", "يُدفع المسير المعتمد فقط.", code="run_status")
        total, paid = _run_payable(p.run, p.currency_id)
        if p.amount > total - paid:
            raise error(f"The payment exceeds the open net pay {total - paid} of the run (R29).",
                        f"الدفعة تتجاوز صافي الرواتب المتبقي {total - paid} للمسير.", code="overpayment")
    elif p.amount > _gosi_open(p.entity, p.gosi_period):
        raise error(f"The payment exceeds the GOSI payable of {p.gosi_period} (R29).",
                    f"الدفعة تتجاوز اشتراكات التأمينات المستحقة لفترة {p.gosi_period}.", code="overpayment")


@transaction.atomic
def submit_payment(payment, *, user):
    p = _lock(PayrollPayment, payment.pk)
    _require(user, p.entity, "payroll.record_payment")
    if p.status != PayrollPayment.Status.DRAFT:
        raise error("Only a draft payment is submitted.", "تُقدم الدفعة المسودة فقط.", code="payment_status")
    p.status, p.submitted_by, p.submitted_at = PayrollPayment.Status.SUBMITTED, user, _now()
    return _save(p)


@transaction.atomic
def approve_payment(payment, *, user):
    """R29: the approver approves (= posts) a submitted payment (A: never its recorder); the run is ``paid`` when its
    net pay is fully paid in every currency."""
    p = PayrollPayment.objects.select_related("entity", "run").get(pk=payment.pk)
    grant = _require(user, p.entity, "payroll.approve_payment", document=p)
    approvals.check(grant, p.entity, "payroll.payment")
    p = _lock(PayrollPayment, p.pk)
    if p.status != PayrollPayment.Status.SUBMITTED:
        raise error("Only a submitted payment is approved.", "تُعتمد الدفعة المقدمة فقط.", code="payment_status")
    _payment_room(p)
    if p.wht_category_id:
        rate = p.wht_category.rate
        p.wht_amount = _round_to(Fraction(p.amount) * Fraction(rate) / 100, p.currency.minor_units)
        fx_rate = (Decimal("1") if p.currency_id == p.entity.base_currency_id else
                   ledger.get_rate(p.entity, p.currency_id, p.entity.base_currency_id, p.payment_date, user=user).rate)
        p.wht_amount_sar = fx.convert(p.wht_amount, fx_rate) if p.currency_id != p.entity.base_currency_id \
            else p.wht_amount
    p.status, p.approved_by, p.approved_at = PayrollPayment.Status.APPROVED, user, _now()
    _save(p)
    try:
        with _marker(_posting, p):
            entry = ledger.create_entry(p.entity, p.payment_date, f"Payroll payment {p.get_kind_display()} / دفعة رواتب",
                                        None, user=user, reference=(p.run.number if p.run else p.gosi_period),
                                        post=True, grant=grant, document=p)
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "posting_refused") from None
    p.journal_entry = entry
    _save(p)
    if p.run is not None:
        run = _lock(PayrollRun, p.run_id)
        currencies = set(Payslip.objects.filter(run=run, refused=False).values_list("currency_id", flat=True))
        if all(_run_payable(run, c)[0] <= _run_payable(run, c)[1] for c in currencies) and run.status == RS.APPROVED:
            run.status = RS.PAID
            _save(run)
    return p


def _payment_lines(p):
    """Registered ``build_lines`` for PayrollPayment: E3 Dr 2330 / Cr bank; E2 Dr 2340 / Cr bank; E8b for a foreign
    remuneration payment: Dr 2330 settling the run's line at its carrying rate, Cr bank at the payment-date rate, Cr
    2220 WHT in SAR, the realised FX difference on 4700 / 5750."""
    _require_posting(p)
    label = f"{p.run.number if p.run else 'GOSI ' + p.gosi_period} payment"
    if p.kind == PayrollPayment.Kind.GOSI:
        return [{"account": _account(p.entity, GOSI_PAYABLE), "debit": p.amount, "description": label},
                {"account": p.bank_account, "credit": p.amount, "description": label}]
    payable = _account(p.entity, SALARIES_PAYABLE)
    if p.currency_id == p.entity.base_currency_id:
        lines = [{"account": payable, "debit": p.amount, "description": label},
                 {"account": p.bank_account, "credit": p.amount - p.wht_amount, "description": label}]
        if p.wht_amount:
            lines.append({"account": _account(p.entity, WHT_PAYABLE), "credit": p.wht_amount, "description": label})
        return lines
    item = JournalLine.objects.filter(entry=p.run.journal_entry, account=payable, currency_id=p.currency_id,
                                      credit__gt=0).first()
    settled = JournalLine.objects.filter(settles_line=item, entry__status="posted") \
        .aggregate(t=Sum("txn_amount"), f=Sum("debit"))
    open_txn = item.txn_amount - (settled["t"] or ZERO)
    open_func = item.credit - (settled["f"] or ZERO)
    carrying = open_func if p.amount == open_txn else fx.convert(p.amount, item.fx_rate)
    rate = ledger.get_rate(p.entity, p.currency_id, p.entity.base_currency_id, p.payment_date, user=p.approved_by).rate
    bank_txn = p.amount - p.wht_amount
    bank_sar = fx.convert(bank_txn, rate)
    lines = [{"account": payable, "currency": p.currency_id, "txn_debit": p.amount, "settles_line": item,
              "description": label},
             {"account": p.bank_account, "currency": p.currency_id, "txn_credit": bank_txn, "description": label}]
    if p.wht_amount_sar:
        lines.append({"account": _account(p.entity, WHT_PAYABLE), "credit": p.wht_amount_sar, "description": label})
    diff = carrying - bank_sar - p.wht_amount_sar
    if diff > 0:
        lines.append({"account": _account(p.entity, FX_GAIN), "credit": diff, "line_kind": "realised_fx",
                      "description": label})
    elif diff < 0:
        lines.append({"account": _account(p.entity, FX_LOSS), "debit": -diff, "line_kind": "realised_fx",
                      "description": label})
    return lines


def wht_return_rows(entity, month, *, user):
    """R43, OQ-30 seam: payroll's remuneration WHT for invoicing's monthly WHT return data (invoicing must include
    these rows; not wired into ``invoicing.wht_return_data`` in slice 1). Employees by code only."""
    _require(user, entity, "payroll.view_cost_reports")
    start = month.replace(day=1)
    end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    return [{"run": p.run.number, "category": p.wht_category.code, "rate": p.wht_category.rate,
             "base": p.amount, "currency": p.currency_id, "wht": p.wht_amount, "wht_sar": p.wht_amount_sar,
             "payment_date": p.payment_date}
            for p in PayrollPayment.objects.filter(entity=entity, status=PayrollPayment.Status.APPROVED,
                                                   wht_category__isnull=False, payment_date__range=(start, end))
            .select_related("run", "wht_category")]


# =====================================================================================================
# WPS files and Mudad (R31, R32, R51, R53)
# =====================================================================================================

def _mask_iban(iban):
    return f"****{iban[-4:]}" if iban else ""


def _wps_rows(run):
    """Employees in the salary file: KSA employees only (R51; remuneration staff are outside WPS); without a valid
    IBAN they are listed as exceptions (R31)."""
    rows, exceptions = [], []
    for ps in Payslip.objects.filter(run=run, refused=False).select_related("employee").order_by("employee__employee_code"):
        emp = Employee.objects.get(pk=ps.employee_id)  # unmasked, inside the service only; logged below
        if not emp.wps_included:
            continue
        if not emp.iban or not hr._iban_ok(emp.iban):
            exceptions.append({"employee_code": emp.employee_code,
                               "reason": "No valid IBAN / لا يوجد آيبان صالح"})
            continue
        comp = {l.component.code: l.amount for l in ps.lines.filter(line_type=LT.EARNING).select_related("component")}
        basic = comp.get("BASIC", ZERO)
        housing = comp.get("HOUSING", ZERO)
        rows.append({"employee_code": emp.employee_code, "name": emp.full_name_en, "id": emp.id_number,
                     "iban": emp.iban, "bank": emp.bank_name, "net": ps.net, "basic": basic, "housing": housing,
                     "other": ps.gross - basic - housing, "deductions": ps.total_deductions})
    return rows, exceptions


def _wps_csv(run, fmt, rows, s, use):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    total = sum((r["net"] for r in rows), ZERO)
    if fmt == "generic_csv":
        w.writerow(["employee_code", "name", "id_number", "iban", "bank", "basic", "housing", "other_earnings",
                    "deductions", "net"])
        for r in rows:
            w.writerow([r["employee_code"], r["name"], r["id"], r["iban"], r["bank"], r["basic"], r["housing"],
                        r["other"], r["deductions"], r["net"]])
        return out.getvalue(), None
    fields = use.get("SA", "wps.fields.mol", run.entry_date)
    hs = HrSettings.objects.filter(entity=run.entity).first()
    header = {"DEST-ID": s.salary_bank, "ESTB-ID": s.wps_bank_code, "BANK-ACC": s.employer_bank_iban,
              "32A-CCY": run.entity.base_currency_id, "32A-VAL": run.pay_date.isoformat(), "32A-AMT": str(total),
              "D-CATE": "", "FILE-REF": run.number, "FILE-REJCDE": "", "MOL-ESTBID": hs.mudad_establishment_no if hs else ""}
    w.writerow(fields["header"])
    w.writerow([header.get(f, "") for f in fields["header"]])
    w.writerow(fields["content"])
    for r in rows:
        content = {"32B-AMT": str(r["net"]), "ACC-59": r["iban"], "NAM-57": r["name"], "BANK-57": r["bank"],
                   "DET-70": "", "MOL-BAS": str(r["basic"]), "MOL-HALI": str(r["housing"]),
                   "MOL-OEA": str(r["other"]), "MOL-ALL": str(r["basic"] + r["housing"] + r["other"]), "MOL-ID": r["id"]}
        w.writerow([content.get(f, "") for f in fields["content"]])
    return out.getvalue(), None


@transaction.atomic
def generate_wps_file(run, *, user, format_code=None):
    """R31, R51: a salary file from an approved run. ``generic_csv`` (an internal review / transfer list, not a bank
    upload format) and ``mudad_placeholder`` (the Ministry header and content fields, wps.fields.mol) are always
    available; a bank format ``<bank>`` needs the settings' salary bank and its ``wps.format.<bank>`` parameter
    (verified when required, hr R7), which the banks have not supplied yet (P18, OQ-10). Generation writes nothing to
    the run or the ledger; the file is stored encrypted and kept indefinitely (P19(m)); personal data read is logged."""
    run = PayrollRun.objects.select_related("entity").get(pk=run.pk)
    _require(user, run.entity, "payroll.generate_wps_file")
    if run.status not in (RS.APPROVED, RS.PAID):
        raise error("A salary file is generated from an approved run (R31).", "يُنشأ ملف الرواتب من مسير معتمد.",
                    code="run_status")
    s = _settings_of(run.entity)
    fmt = format_code or s.wps_format
    if not fmt:
        raise error("No WPS format is set (R31, OQ-10).", "لم تُحدد صيغة ملف حماية الأجور.", code="wps_format_missing")
    use = hr.ParamUse()
    param_row = None
    if fmt not in ("generic_csv", "mudad_placeholder"):
        if fmt != s.salary_bank:
            raise error("The WPS format must be the salary bank's own (R51).", "يجب أن تكون الصيغة صيغة بنك الرواتب.",
                        code="wps_format_bank")
        param_row = hr.param("SA", f"wps.format.{fmt}", run.entry_date, required=False)
        if param_row is None:
            raise error(f"The {fmt} salary file format is not configured yet (wps.format.{fmt}; P18, OQ-10).",
                        f"صيغة ملف الرواتب لبنك {fmt} غير مهيأة بعد.", code="wps_format_missing")
        use._take(param_row)
        use.check_verified()
        raise error(f"The {fmt} byte layout is not built until the bank supplies it (P18).",
                    f"لم يُبنَ تنسيق بنك {fmt} حتى يزوده البنك.", code="wps_format_missing")
    rows, exceptions = _wps_rows(run)
    content, _ = _wps_csv(run, fmt, rows, s, use)
    f = _save(WpsFile(entity=run.entity, run=run, format_code=fmt, format_parameter=param_row, generated_by=user,
                      sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(), employee_count=len(rows),
                      total_amount=sum((r["net"] for r in rows), ZERO), exceptions=exceptions, content=content))
    access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=run.entity,
                        permission="payroll.generate_wps_file",
                        details={"action": "wps_generated", "wps_file": f.pk, "sha256": f.sha256,
                                 "employee_codes": [r["employee_code"] for r in rows],
                                 "fields": ["full_name_en", "id_number", "iban", "bank_name"]})
    return f


def download_wps_file(wps_file, *, user):
    """Each download is logged (R31)."""
    f = WpsFile.objects.select_related("entity").get(pk=wps_file.pk)
    _require(user, f.entity, "payroll.generate_wps_file")
    access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=f.entity,
                        permission="payroll.generate_wps_file",
                        details={"action": "wps_downloaded", "wps_file": f.pk, "sha256": f.sha256})
    return f.content


@transaction.atomic
def attach_bank_processed_file(wps_file, *, user, content):
    """R32: the bank-processed file (with TRN-* and RET-CDE) is stored on the WPS file, both hashes recorded."""
    f = _lock(WpsFile, wps_file.pk)
    _require(user, f.entity, "payroll.record_mudad_submission")
    if not content:
        raise error("The bank-processed file is empty.", "ملف البنك المعالج فارغ.", code="file_empty")
    f.bank_processed_content = content
    f.bank_processed_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    f.bank_processed_received_at = _now()
    return _save(f)


@transaction.atomic
def record_mudad_submission(wps_file, *, user, submitted_on, reference, status, message_ar="", message_en=""):
    """R32: the officer uploads the bank-processed file in Mudad (no API) and records reference and status."""
    f = WpsFile.objects.select_related("run").get(pk=wps_file.pk)
    _require(user, f.entity, "payroll.record_mudad_submission")
    if not f.bank_processed_sha256:
        raise error("Mudad takes the bank-processed file: attach it first (R32).",
                    "يُرفع على مدد ملف البنك المعالج: أرفقه أولًا.", code="bank_file_missing")
    if status not in MudadSubmission.Status.values or not (reference or "").strip():
        raise error("Mudad reference and status are required.", "مرجع مدد والحالة مطلوبان.", code="mudad_invalid")
    return _save(MudadSubmission(entity=f.entity, run=f.run, wps_file=f, submitted_on=submitted_on, reference=reference,
                                 status=status, message_ar=message_ar, message_en=message_en, recorded_by=user))


# =====================================================================================================
# Payslips and reports (R2, R33, R35, R40; PDPL)
# =====================================================================================================

def runs(user, entity=None):
    qs = access.scope(PayrollRun.objects.all(), user, "payroll.view")
    return qs.filter(entity=entity) if entity is not None else qs


def get_run(user, pk):
    """Another entity's run is 404 (R2)."""
    return access.get_scoped_or_404(PayrollRun.objects.all(), user, "payroll.view", pk=pk)


def get_payslip(user, pk):
    return access.get_scoped_or_404(Payslip.objects.select_related("run", "employee"), user, "payroll.view_payslip",
                                    pk=pk)


def payslip_data(payslip, *, user):
    """R33: one payslip from its stored lines (never recalculated), both languages; personal data masked (IBAN last 4
    digits, no ID number); each view logged (PDPL)."""
    ps = Payslip.objects.select_related("run", "entity", "currency").get(pk=payslip.pk)
    _require(user, ps.entity, "payroll.view_payslip")
    if ps.run.status not in (RS.APPROVED, RS.PAID, RS.REVERSED):
        raise error("Payslips are issued from approved runs (R33).", "تصدر القسائم من المسيرات المعتمدة.",
                    code="run_status")
    emp = Employee.objects.get(pk=ps.employee_id)
    access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=ps.entity,
                        permission="payroll.view_payslip",
                        details={"action": "payslip_viewed", "payslip": ps.pk, "employee_code": emp.employee_code})
    lines = list(ps.lines.order_by("id"))
    return {
        "entity_en": ps.entity.legal_name_en or ps.entity.name_en, "entity_ar": ps.entity.legal_name_ar or ps.entity.name_ar,
        "period": ps.run.period, "number": ps.run.number, "employee_code": emp.employee_code,
        "name_en": emp.full_name_en, "name_ar": emp.full_name_ar, "iban": _mask_iban(emp.iban),
        "currency": ps.currency_id, "earnings": [l for l in lines if l.line_type in PayslipLine.EARNINGS],
        "reductions": [l for l in lines if l.line_type in PayslipLine.REDUCTIONS],
        "deductions": [l for l in lines if l.line_type in PayslipLine.DEDUCTIONS],
        "employer": [l for l in lines if l.line_type in PayslipLine.EMPLOYER],
        "gross": ps.gross_txn, "deductions_total": ps.total_deductions, "net": ps.net_txn, "net_sar": ps.net,
        "loan_balance": ps.loan_balance, "leave_balance": ps.leave_balance_snapshot,
        "interest_free": any(l.line_type == LT.LOAN_INSTALLMENT for l in lines), "notes": ps.notes,
        "notices": list(GosiCatchUpNotice.objects.filter(run=ps.run, employee_id=ps.employee_id)),
        "wht": _payslip_wht(ps), "payslip": ps,
    }


def _payslip_wht(ps):
    """R43: shown as information (gross, WHT, net of WHT) once the payment is approved."""
    p = PayrollPayment.objects.filter(run=ps.run, status=PayrollPayment.Status.APPROVED, wht_category__isnull=False) \
        .select_related("wht_category").first()
    if p is None or ps.employment_basis != Basis.REMUNERATION:
        return None
    wht = _round_to(Fraction(ps.net_txn) * Fraction(p.wht_category.rate) / 100, ps.currency.minor_units)
    return {"rate": p.wht_category.rate, "wht": wht, "net_of_wht": ps.net_txn - wht}


def render_payslip(payslip, *, user):
    """Bilingual HTML payslip (English and Arabic on one slip, OQ-16)."""
    return render_to_string("payroll/payslip.html", payslip_data(payslip, user=user))


def payroll_register(run, *, user):
    """Payroll register: one row per payslip by employee code (individual pay: ``payroll.view_payslip``, logged)."""
    run = PayrollRun.objects.get(pk=run.pk)
    _require(user, run.entity, "payroll.view_payslip")
    rows = [{"employee_code": ps.employee.employee_code, "basis": ps.employment_basis, "currency": ps.currency_id,
             "paid_days": ps.paid_days, "gross": ps.gross, "deductions": ps.total_deductions, "net": ps.net,
             "employee_gosi": ps.employee_gosi, "employer_gosi": ps.employer_gosi, "eosb_accrual": ps.eosb_accrual,
             "refused": ps.refusal} for ps in run.payslips.select_related("employee")]
    access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=run.entity,
                        permission="payroll.view_payslip", details={"action": "payroll_register", "run": run.pk})
    return {"run": run.number or run.pk, "period": run.period, "rows": rows,
            "totals": {k: sum((r[k] for r in rows), ZERO) for k in ("gross", "deductions", "net", "employee_gosi",
                                                                   "employer_gosi", "eosb_accrual")}}


def gosi_report(entity, period, *, user):
    """GOSI report of a period (approved runs; codes only): wage and shares per branch, the due date (15th of the
    following month, Regs Art. 24(1)) and the late-fine rule (2% per month or part; waivers, Regs Art. 25)."""
    _require(user, entity, "payroll.view_cost_reports")
    rows = {}
    for l in PayslipLine.objects.filter(entity=entity, payslip__run__period=period, payslip__refused=False,
                                        payslip__run__status__in=(RS.APPROVED, RS.PAID),
                                        line_type__in=(LT.EMPLOYEE_GOSI, LT.EMPLOYER_GOSI, LT.GOSI_CATCH_UP)) \
            .select_related("payslip__employee"):
        r = rows.setdefault(l.payslip.employee.employee_code, {"employee_code": l.payslip.employee.employee_code,
                                                               "gosi_wage": l.payslip.gosi_wage, "employee": ZERO,
                                                               "employer": ZERO})
        r["employer" if l.line_type == LT.EMPLOYER_GOSI else "employee"] += l.amount
    due_day = int(hr.param("SA", "gosi.payment_due_day", _period_bounds(period)[1]).value["new_m273"])
    ns = _period_bounds(_next_period(period))[0]
    fine = hr.param("SA", "gosi.late_fine_pct", _period_bounds(period)[1]).value
    total = sum((r["employee"] + r["employer"] for r in rows.values()), ZERO)
    return {"period": period, "rows": sorted(rows.values(), key=lambda r: r["employee_code"]), "total": total,
            "due_date": ns.replace(day=due_day), "open": _gosi_open(entity, period),
            "late_fine_en": f"Late payment: {fine['pct']}% of the due contribution per month or part (Law Art. 9(3)); a "
                            f"delay of up to {fine['full_waiver_max_delay_days']} days may be fully waived (Regs Art. 25); "
                            "fines are entered manually on 5930 (E14).",
            "late_fine_ar": f"التأخير في السداد: {fine['pct']}% من الاشتراك المستحق عن كل شهر أو جزء منه؛ ويجوز الإعفاء "
                            f"الكامل لتأخير لا يتجاوز {fine['full_waiver_max_delay_days']} أيام."}


def bank_transfer_list(run, *, user):
    """Bank transfer list: employee code, masked IBAN, amount (individual pay: ``payroll.view_payslip``)."""
    run = PayrollRun.objects.get(pk=run.pk)
    _require(user, run.entity, "payroll.view_payslip")
    out = []
    for ps in run.payslips.filter(refused=False).select_related("employee"):
        emp = Employee.objects.get(pk=ps.employee_id)
        out.append({"employee_code": emp.employee_code, "iban": _mask_iban(emp.iban), "currency": ps.currency_id,
                    "amount": ps.net_txn, "wps": emp.wps_included})
    access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=run.entity,
                        permission="payroll.view_payslip", details={"action": "bank_transfer_list", "run": run.pk})
    return out


def secondment_cost_report(entity, period, *, user):
    """R35 (payroll-only part): per secondment, employee code, days on secondment and the prorated gross, employer
    GOSI and EOSB accrual of the period's approved run, with the GOSI Regs Art. 20(3)(a) warning for a KSA-to-KSA
    secondment (OQ-48). The fully loaded report with employee cost items (R63) and its approval are slice 2."""
    _require(user, entity, "payroll.view_cost_reports")
    start, end = _period_bounds(period)
    out = []
    for a in Assignment.objects.filter(entity=entity, kind=Assignment.Kind.SECONDMENT, start_date__lte=end) \
            .exclude(status__in=(Assignment.Status.DRAFT, Assignment.Status.SUBMITTED, Assignment.Status.CANCELLED)) \
            .select_related("employee", "receiving_entity"):
        if a.end_date and a.end_date < start:
            continue
        ps = Payslip.objects.filter(employee=a.employee, run__period=period, refused=False,
                                    run__status__in=(RS.APPROVED, RS.PAID)).first()
        days = (min(a.end_date or end, end) - max(a.start_date, start)).days + 1
        dim = (end - start).days + 1
        share = Fraction(days, dim)
        row = {"employee_code": a.employee.employee_code, "assignment": a.reference or a.pk, "days": days,
               "receiving_entity": a.receiving_entity.code if a.receiving_entity else "",
               "gross": _round(Fraction(ps.gross) * share) if ps else ZERO,
               "employer_gosi": _round(Fraction(ps.employer_gosi) * share) if ps else ZERO,
               "eosb_accrual": _round(Fraction(ps.eosb_accrual) * share) if ps else ZERO, "warnings": []}
        if a.receiving_entity is not None and a.receiving_entity.country == "SA":
            row["warnings"].append("Receiving entity may have to register GOSI (Regs Art. 20(3)(a), OQ-48) / قد يلزم "
                                   "الكيان المستقبل التسجيل في التأمينات")
        out.append(row)
    return out


@transaction.atomic
def record_gosi_reconciliation(entity, period, *, user, gosi_invoice_amount, note_ar="", note_en=""):
    """R23: the GOSI invoice against the ERP contributions of the period; the difference is reported, never posted."""
    _require(user, entity, "payroll.prepare_run")
    invoice = _dec(gosi_invoice_amount, "gosi_invoice_amount")
    erp = PayslipLine.objects.filter(entity=entity, payslip__run__period=period, payslip__refused=False,
                                     payslip__run__status__in=(RS.APPROVED, RS.PAID),
                                     line_type__in=(LT.EMPLOYEE_GOSI, LT.EMPLOYER_GOSI, LT.GOSI_CATCH_UP)) \
        .aggregate(t=Sum("amount"))["t"] or ZERO
    return _save(GosiReconciliation(entity=entity, period=period, gosi_invoice_amount=invoice, erp_amount=erp,
                                    difference=invoice - erp, note_ar=note_ar, note_en=note_en, recorded_by=user))


@transaction.atomic
def record_catch_up_notification(notice, *, user, notified_on, channel):
    """R58: how the employee was told of a later GOSI deduction (until self-service exists)."""
    n = _lock(GosiCatchUpNotice, notice.pk)
    _require(user, n.entity, "payroll.prepare_run")
    if not (channel or "").strip():
        raise error("The notification channel is required.", "وسيلة الإبلاغ مطلوبة.", code="channel_required")
    n.notified_on, n.channel, n.recorded_by = notified_on, channel, user
    return _save(n)


# =====================================================================================================
# Alerts job (R10, R22, R53; P19(m))
# =====================================================================================================

def send_alerts(*, today=None, entity=None):
    """``payroll_alerts`` (daily): Mudad upload due (days 7, 10, 15 of the following month while no submission is
    recorded, R53); GOSI registration (25th and last day of the joining month, Regs Art. 6); GOSI payment due (the
    10th, before the 15th; Regs Art. 24, 25); WPS files older than 10 years nominated for a human burn decision (never
    deleted, P19(m)). Idempotent: one alert per recipient and key."""
    today = today or _today()
    entities = Entity.objects.filter(pk=entity.pk) if entity is not None else Entity.objects.filter(
        payroll_settings__enabled=True)
    sent = 0
    for e in entities:
        roles = ("payroll_officer", "hr_officer")
        prev = _period_of(today.replace(day=1) - timedelta(days=1))
        if today.day in (7, 10, 15):
            for run in PayrollRun.objects.filter(entity=e, period=prev, payroll_group=PayrollGroup.IN_COUNTRY,
                                                 status__in=(RS.APPROVED, RS.PAID)):
                if not MudadSubmission.objects.filter(run=run).exists():
                    _send_alerts(e, [(PayrollAlert.Kind.MUDAD_DEADLINE, f"mudad:{run.pk}:{today.day}",
                                      f"Upload the bank-processed salary file of {run.period} to Mudad (day {today.day}; "
                                      "Mudad refers to inspection on day 20).",
                                      f"ارفع ملف الرواتب المعالج من البنك لفترة {run.period} على مدد (اليوم {today.day}؛ "
                                      "يُحال إلى التفتيش في اليوم 20).", roles)])
                    sent += 1
        last = calendar.monthrange(today.year, today.month)[1]
        if today.day in (25, last):
            for emp in Employee.objects.filter(entity=e, employment_basis=Basis.KSA_EMPLOYEE,
                                               status=Employee.Status.ACTIVE):
                if not (emp.gosi_number or "").strip():
                    _send_alerts(e, [(PayrollAlert.Kind.GOSI_REGISTRATION, f"gosi_reg:{emp.pk}:{_period_of(today)}:{today.day}",
                                      f"Register {emp.employee_code} at GOSI by {today.replace(day=last)} (Regs Art. 6).",
                                      f"سجّل {emp.employee_code} في التأمينات قبل {today.replace(day=last)}.", roles)])
                    sent += 1
        if today.day == 10:
            open_ = _gosi_open(e, prev)
            if open_ > 0:
                _send_alerts(e, [(PayrollAlert.Kind.GOSI_PAYMENT, f"gosi_pay:{prev}",
                                  f"GOSI contributions of {prev} ({open_}) are due by the 15th (Regs Art. 24(1)); late "
                                  "payment: 2% a month (Law Art. 9(3)).",
                                  f"اشتراكات التأمينات لفترة {prev} ({open_}) مستحقة قبل اليوم 15؛ غرامة التأخير 2% شهريًا.",
                                  ("payroll_officer", "approver"))])
                sent += 1
        from datetime import datetime
        cutoff = timezone.make_aware(datetime(today.year - 10, today.month, min(today.day, 28)))
        for f in WpsFile.objects.filter(entity=e, nominated_for_burn_at__isnull=True, generated_at__lt=cutoff):
            f.nominated_for_burn_at = _now()
            _save(f)
            sent += 1
    return sent
