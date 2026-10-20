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
from types import SimpleNamespace
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
    SalaryComponent,
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
    EmployeeCostItem,
    EmployeeLoan,
    EosbProvisionEntry,
    FinalSettlement,
    FinalSettlementLine,
    GosiAbroadPeriod,
    GosiCatchUpNotice,
    GosiFine,
    GosiReconciliation,
    GosiVariableWageBase,
    LoanInstallment,
    LoanWriteOff,
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
    SecondmentCostLine,
    SecondmentCostReport,
    SettlementCorrection,
    SettlementCorrectionLine,
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

# Review 16: hr refusals of calculate_eosb that leave the run going with a warning (no contract on the day, no EOSB
# for the basis); any other refusal stops the calculation.
EOSB_SKIPPABLE = frozenset({"no_terms", "remuneration_no_eosb"})
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


def _entity_lock(entity):
    """Review 1: serialise the payroll postings of one entity (run approval and reversal, payments, final
    settlements). Taken first, before the document rows, in every such service, so two of them never decide on the
    same open amount at once. On SQLite the database serialises writers anyway."""
    return PayrollSettings._base_manager.select_for_update().filter(entity=entity).first()


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
    "gosi_housing_in_kind_exempt_ref", "wps_deductions_field", "loan_write_off_account", "work_governorate",
})


BANK_SETTINGS = ("salary_bank", "salary_bank_account", "wps_format", "wps_bank_code", "employer_bank_iban")


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
    bank_before = {k: getattr(s, k) for k in BANK_SETTINGS}
    for name, value in fields.items():
        if name in ("salary_bank_account", "loan_write_off_account") and value and not isinstance(value, Account):
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
    wo = s.loan_write_off_account
    if wo is not None and (wo.entity_id != entity.pk or wo.type != "expense" or not wo.is_postable
                           or (wo.sharia_prohibited and entity.sharia_enforced)):
        problems.append("The loan write-off account is a postable expense account of the entity, never Sharia-"
                        "prohibited (OQ-7). / حساب شطب القروض حساب مصروف قابل للترحيل من حسابات الكيان وغير محظور شرعًا.")
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
    bank_after = {k: getattr(s, k) for k in BANK_SETTINGS}
    changed = sorted(k for k in BANK_SETTINGS if bank_before[k] != bank_after[k])
    if changed:  # review 17: the employer's bank details are logged, masked (never the full IBAN)
        def shown(name, value):
            if name in ("employer_bank_iban", "wps_bank_code"):
                return _mask_iban(value or "")
            return str(getattr(value, "code", value) or "")
        access_services.log(AccessLog.Event.PROFILE_CHANGED, actor=user, entity=entity,
                            permission="payroll.manage_settings",
                            details={"action": "payroll_bank_settings", "fields": changed,
                                     "before": {k: shown(k, bank_before[k]) for k in changed},
                                     "after": {k: shown(k, bank_after[k]) for k in changed},
                                     "message_en": "Payroll bank settings changed",
                                     "message_ar": "تغيّرت إعدادات بنك الرواتب"})
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
    if jurisdiction not in CALCULATED_JURISDICTIONS:
        raise error(f"Jurisdiction {jurisdiction} is not configured for payroll: the local calculation is not built "
                    "(R4, OQ-28).", f"الولاية القانونية {jurisdiction} غير مهيأة للرواتب: الحساب المحلي غير مبني.",
                    code="jurisdiction_not_configured")
    return s


# R4 (P19(f)): the jurisdictions with a payroll calculation. Egypt (slice 2, §10.8 item 7) is calculated from its
# research rows; a run there is refused at submission and approval while any row it read is unverified.
CALCULATED_JURISDICTIONS = ("SA", "EG")


def _jurisdiction_of(entity):
    hs = HrSettings.objects.filter(entity=entity).first()
    return hs.jurisdiction if hs is not None else "SA"


def _local_unverified(run):
    """§10.8 item 7: an Egypt run reads research rows; it activates only once a person verifies every row it read,
    whatever ``HR_REQUIRE_VERIFIED_PARAMETERS`` says."""
    if _jurisdiction_of(run.entity) == "SA":
        return []
    return list(StatutoryParameter.objects.filter(pk__in=run.parameter_ids).exclude(
        status=StatutoryParameter.Status.VERIFIED).order_by("jurisdiction", "code", "valid_from"))


def _refuse_unverified_local(rows):
    names = ", ".join(f"{r.jurisdiction} {r.code} ({r.valid_from})" for r in rows)
    raise error(f"This Egypt run reads unverified research parameters: {names}. A person verifies each row before an "
                "Egypt run is submitted or approved (R4, OQ-28; hr R7).",
                f"يقرأ مسير مصر هذا معايير بحثية غير متحقق منها: {names}. يتحقق شخص من كل صف قبل تقديم مسير مصر أو "
                "اعتماده.", code="parameter_unverified")


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


def _overtime_year(emp, on):
    """The employee's year (contract calendar basis) holding ``on``."""
    terms = _terms(emp, on)
    if terms is not None and terms.calendar_basis == EmploymentContract.CalendarBasis.HIJRI:
        from hijridate import Gregorian
        hy = Gregorian(on.year, on.month, on.day).to_hijri().year
        from hijridate import Hijri
        g1, g2 = Hijri(hy, 1, 1).to_gregorian(), Hijri(hy + 1, 1, 1).to_gregorian()
        start, end = date(g1.year, g1.month, g1.day), date(g2.year, g2.month, g2.day) - timedelta(days=1)
    else:
        start, end = date(on.year, 1, 1), date(on.year, 12, 31)
    return start, end


def _year_overtime_hours(emp, on, exclude=None):
    start, end = _overtime_year(emp, on)
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
    if basis == Basis.LOCAL_ENTITY_EMPLOYEE:
        return _eg_input_problems(inp)
    if inp.kind == IK.ONE_OFF_DEDUCTION and inp.deduction_basis == PayrollInput.DeductionBasis.NAFAQA:
        problems.append("A nafaqa (maintenance) deduction basis is for Egypt payroll; in KSA a court-ordered debt is "
                        "'court_order' (Art. 92(6)). / أساس النفقة لرواتب مصر؛ وفي المملكة يكون الدين بحكم قضائي.")
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
                if inp.art106_case not in ("", *PayrollInput.Art106Case.values):
                    problems.append("Unknown Art. 106 case. / حالة غير معروفة من حالات المادة 106.")
                elif inp.art106_case == PayrollInput.Art106Case.INVENTORY_CLOSING:
                    # C-A7: inventory, balance sheets, closing of accounts and seasonal preparation, at most 30 days a
                    # year (Labour Law Art. 106(1)).
                    max_days = int(use.get("SA", "overtime.art106_inventory_max_days", inp.overtime_date)["days"])
                    ys, ye = _overtime_year(emp, inp.overtime_date)
                    days = set(PayrollInput.objects.filter(
                        employee=emp, kind=IK.OVERTIME_HOURS, art106_exceptional=True,
                        art106_case=PayrollInput.Art106Case.INVENTORY_CLOSING, overtime_date__range=(ys, ye))
                        .exclude(status=IS.REJECTED).exclude(pk=inp.pk).values_list("overtime_date", flat=True))
                    days.add(inp.overtime_date)
                    if len(days) > max_days:
                        problems.append(f"Inventory and closing work under Art. 106(1) is at most {max_days} days a year; "
                                        f"this would be day {len(days)} (C-A7). / العمل في الجرد وإقفال الحسابات وفق المادة "
                                        f"106(1) لا يتجاوز {max_days} يومًا في السنة.")
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
            # C-A4: the caps are per month for all deductions of that basis together.
            others = PayrollInput.objects.filter(employee=emp, kind=IK.ONE_OFF_DEDUCTION, deduction_basis=basis_,
                                                 period=inp.period).exclude(status=IS.REJECTED).exclude(pk=inp.pk) \
                .aggregate(t=Sum("amount"))["t"] or ZERO
            total = others + inp.amount
            if basis_ == PayrollInput.DeductionBasis.COURT_ORDER:
                limit = _round(Fraction(wage) * Fraction(caps["court_max_fraction"]))
                if total > limit:
                    problems.append(f"Court-ordered deductions are at most a quarter of the wage a month, {limit}; with the "
                                    f"others of {inp.period} they would be {total} (Art. 92(6), C-A4). / لا تتجاوز "
                                    f"الاستقطاعات بحكم قضائي ربع الأجر في الشهر {limit}؛ ومع غيرها تبلغ {total}.")
            else:
                limit = _round(Fraction(wage) * int(caps["damage_max_days_month"]) / 30)
                if total > limit:
                    problems.append(f"Damage recovery is at most 5 days' wage a month, {limit}; with the others of "
                                    f"{inp.period} it would be {total} (Art. 91, C-A4). / لا يتجاوز اقتطاع تعويض الإتلاف "
                                    f"أجر خمسة أيام في الشهر {limit}؛ ومع غيره يبلغ {total}.")
    if inp.kind == IK.MATERNITY_SUPPORT and not inp.legal_clearance_ref.strip():
        problems.append("Temporary maternity support needs the written legal clearance reference (R61(b)). / يحتاج "
                        "الدعم المؤقت أثناء الأمومة إلى مرجع الإذن القانوني المكتوب.")
    return problems


INPUT_FIELDS = frozenset({"kind", "period", "reference", "quantity", "amount", "component", "reason_en", "reason_ar",
                          "overtime_date", "art106_exceptional", "art106_case", "worker_consent_ref", "deduction_basis",
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
    if status in (MaternityGosiCase.Status.CONFIRMED, MaternityGosiCase.Status.REFUSED) and case.pk:
        open_ = _carried_maternity_open(case.employee, case)
        if open_:  # slice 2: the payroll officers settle the carried days (prepare_maternity_settlement)
            months = ", ".join(q for q, _ in open_)
            _send_alerts(case.entity, [(PayrollAlert.Kind.MATERNITY_DECIDED, f"maternity_decided:{case.pk}:{status}",
                                        f"GOSI {status.replace('gosi_', '')} the maternity case of "
                                        f"{case.employee.employee_code}: settle the carried days of {months} "
                                        "(prepare_maternity_settlement, R61).",
                                        f"قررت التأمينات حالة الأمومة لـ {case.employee.employee_code}: سوِّ الأيام المرحّلة "
                                        f"لفترة {months}.", ("payroll_officer",))])
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
    basis: str = ""  # deduction basis of a one-off deduction input (C-A4 caps)


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
    input_ids: list = field(default_factory=list)  # inputs the payslip takes (review 9)
    iban_hash: str = ""  # review 8: the IBAN the payslip was calculated with
    termination_ref: int = None  # review 8: the approved termination known at calculation
    taxable_income: Decimal = ZERO  # Egypt: the month's taxable salary (cumulative withholding, §10.8 item 7)

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
        self.jurisdiction = _jurisdiction_of(run.entity)
        self.period = run.period
        self.start, self.end = _period_bounds(run.period)
        self.use = hr.ParamUse()
        self._accounts = {}
        self.warnings = []
        self.skipped_local = 0
        self.leavers = []  # (employee_code, last working day): paid by the final settlement (R10)
        self.nothing_due = []  # off-cycle: employees with nothing left to pay
        self.settled = []  # (employee_code, settlement number): payroll closed by an approved final settlement

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
    _rate_basis_rule(ctx, slip, v, info)
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


def _rate_basis_rule(ctx, slip, v, info):
    """GOSI Regs Art. 18 (slice 2): a daily-rated contract's GOSI wage is the daily rate x 30 (Art. 18(1)), which is
    the monthly equivalent the contract states; an hourly-rated one follows the Governor's rules (Art. 18(3)), not
    published: ``gosi.variable_wage_basis`` "hourly" stays ``not_configured`` (unverified) and the GOSI calculation is
    refused until a person configures the rule (``{"method": "monthly_equivalent"}`` is the only one built)."""
    basis = getattr(v, "pay_rate_basis", "monthly")
    if basis == "daily":
        slip.notes.append("Daily-rated contract: GOSI wage = daily rate x 30, the monthly equivalent (GOSI Regs Art. "
                          "18(1)). / عقد بأجر يومي: أجر الاشتراك = الأجر اليومي × 30.")
    elif basis == "hourly":
        rule = ctx.p("gosi.variable_wage_basis", info.end).get("hourly", "not_configured")
        if rule == "not_configured" or not isinstance(rule, dict):
            slip.refuse("Hourly GOSI rules (GOSI Regs Art. 18(3)) not configured: the Governor's rules are not "
                        "published [CC-verify: Governor's hourly rules not supplied].",
                        "قواعد الاشتراك للأجر بالساعة (المادة 18(3) من اللائحة) غير مهيأة: لم تُنشر قواعد المحافظ.")
        if rule.get("method") != "monthly_equivalent":
            slip.refuse(f"Unknown hourly GOSI method {rule.get('method')!r}.", "طريقة اشتراك الأجر بالساعة غير معروفة.")
        slip.notes.append("Hourly-rated contract: GOSI wage on the monthly equivalent per the configured rule (Regs "
                          "Art. 18(3), unverified). / عقد بأجر بالساعة: الاشتراك على المعادل الشهري وفق القاعدة المهيأة.")


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
            # C-B4: the note never shows the other employers' amounts (their pay is not this employer's data).
            slip.notes.append("GOSI wage reduced pro rata: the SAR 45,000 cap applies across all employers (GOSI Regs Art. "
                              "19, R68). / خُفض أجر الاشتراك بالتناسب: يسري حد 45,000 ريال على جميع أصحاب العمل.")
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


def _allocate(parts):
    """Round a {key: Fraction} split so the rounded parts add up to the rounded total (largest remainder, by cent):
    a reduction booked per component then totals exactly what one rounding of the whole would give."""
    total = _round(sum(parts.values(), Fraction(0)))
    floors = {}
    for k, v in parts.items():
        with localcontext() as c:
            c.prec = 60
            floors[k] = (Decimal(v.numerator) / Decimal(v.denominator)).quantize(CENT, rounding="ROUND_FLOOR")
    rest = int((total - sum(floors.values(), ZERO)) / CENT)
    order = sorted(parts, key=lambda k: (Fraction(floors[k]) - parts[k], getattr(k, "pk", 0)))
    for k in order[:max(rest, 0)]:
        floors[k] += CENT
    return floors


def _by_component(info, days, flag, factor=Fraction(1), weighted=True):
    """{component: Fraction} of the earning components with ``flag`` for ``days`` (the version in force each day):
    monthly x day weight / divisor. Review 4: reductions use the same day weights as the earnings (R10), so day 31
    carries nothing on the 30-day basis and the last day of February carries the rest of the 30-day month."""
    out = {}
    for d in days:
        w = Fraction(info.weights.get(d, 0)) if weighted else Fraction(1)
        if not w:
            continue
        for c in _components(info.version_on(d)):
            if c.component.kind == "earning" and (flag is None or getattr(c.component, flag)):
                out[c.component] = out.get(c.component, Fraction(0)) + Fraction(c.monthly_amount) * factor * w / info.divisor
    return out


def _reduction_lines(ltype, parts, en, ar, quantity, source_ref=""):
    """One reduction line per component (WPS needs BASIC and HOUSING net of reductions, C-C2)."""
    out = []
    for comp, amount in _allocate(parts).items():
        if amount:
            out.append(Line(ltype, amount, comp.expense_account, f"{en} ({comp.code})", f"{ar} ({comp.name_ar})",
                            component=comp, quantity=quantity, source_ref=source_ref))
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
            suspended = _by_component(info, covered, "counts_for_eosb")
            full = _round(sum(suspended.values(), Fraction(0)))
            slip.lines.extend(_reduction_lines(LT.MATERNITY_SUSPENDED, suspended,
                                               "Salary suspended: GOSI maternity compensation",
                                               "أجر موقوف: تعويض الأمومة من التأمينات", Decimal(weight),
                                               f"maternity:{case.pk}"))
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
            carried = _by_component(info, wage_days, "counts_for_eosb")
            slip.lines.extend(_reduction_lines(LT.MATERNITY_SUSPENDED, carried,
                                               "Salary carried pending GOSI decision (not unpaid)",
                                               "أجر مُرحّل بانتظار قرار التأمينات (ليست إجازة بدون أجر)", Decimal(weight),
                                               f"maternity_pending:{req.pk}"))
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


def _month_pay_lines(ctx, slip, emp, info, inputs=()):
    """The contract pay of one month for ``info``: earnings per component (R10), maternity (R61), unpaid leave (R16)
    and sick-leave reductions (R17), all on the same day weights (review 4). Used for the run's own period, for
    earlier months re-examined (R14, review 10) and for the leaving month of a final settlement."""
    for pk, (comp, amount) in _component_earnings(info).items():
        if amount:
            slip.lines.append(Line(LT.EARNING, amount, comp.expense_account, comp.name_en, comp.name_ar,
                                   component=comp, quantity=Decimal(info.paid_days)))
    handled = _maternity(ctx, slip, emp, info, inputs)
    employed = {d for d, _ in info.days}
    unpaid = sorted((hr._unpaid_days(emp, info.start, info.end) & employed) - handled)
    if unpaid:
        weight = Decimal(_weight(info, unpaid))
        slip.lines.extend(_reduction_lines(LT.UNPAID_DEDUCTION, _by_component(info, unpaid, "deducted_for_unpaid_leave"),
                                           f"Unpaid leave {weight} days", f"إجازة بدون أجر {weight} يوم", weight))
    sick = {}
    sick_weight = 0
    for d in hr._live_sick_days(SickLeaveDay.objects.filter(employee=emp, date__range=(info.start, info.end))):
        if d.date not in employed or d.date in handled or d.official_holiday or d.pay_pct >= 100:
            continue
        cut = (100 - Fraction(d.pay_pct)) / 100
        for comp, amount in _by_component(info, [d.date], "counts_for_eosb", cut).items():
            sick[comp] = sick.get(comp, Fraction(0)) + amount
        sick_weight += info.weights.get(d.date, 0)
    if sick:
        slip.lines.extend(_reduction_lines(LT.SICK_DEDUCTION, sick, f"Sick-leave reduction {sick_weight} days (Art. 117)",
                                           f"تخفيض الإجازة المرضية {sick_weight} يوم (المادة 117)", Decimal(sick_weight)))
    return handled


def _employee_inputs(ctx, emp, *, every_period=False):
    """Review 9: approved inputs not yet taken by an approved run (or a final settlement) whose paid-in period is the
    run's period or earlier: an input approved after its period's run was approved goes to the next run. A final
    settlement takes every approved input not yet taken."""
    qs = PayrollInput.objects.filter(employee=emp, status=IS.APPROVED, taken_run__isnull=True,
                                     final_settlement__isnull=True)
    if not every_period:
        qs = qs.filter(payroll_period__lte=ctx.period)
    return list(qs.select_related("component", "component__expense_account").order_by("id"))


def _prior_payslips(ctx, emp):
    return list(Payslip.objects.filter(employee=emp, refused=False, run__entity=ctx.entity,
                                       run__status__in=(RS.APPROVED, RS.PAID), run__payroll_group=ctx.run.payroll_group,
                                       run__kind=PayrollRun.Kind.REGULAR, run__period__lt=ctx.period).select_related("run").order_by("run__period"))


SETTLED = (FinalSettlement.Status.APPROVED, FinalSettlement.Status.PAID)


def _booked_lines(emp, period, types):
    """Lines of approved (or paid) runs that belong to ``period``: the period's own runs and lines of later runs with
    that period of origin. Re-review 1: the lines of an approved (or paid) final settlement count too, by their period
    of origin or, without one, the leaving month, as the payslip line type they were computed as (a salary paid
    beyond the last working day and recovered from the final dues counts as less pay for the month)."""
    out = list(PayslipLine.objects.filter(payslip__employee=emp, payslip__run__status__in=(RS.APPROVED, RS.PAID),
                                          payslip__refused=False, line_type__in=types).filter(
        Q(origin_period=period) | (Q(origin_period="") & Q(payslip__run__period=period)))
        .select_related("component", "account").order_by("pk"))
    wanted = Q(slip_line_type__in=types)
    if LT.EARNING in types:
        wanted |= Q(slip_line_type=LT.DEDUCTION, source_ref__startswith="overpaid:")
    for l in (FinalSettlementLine.objects.filter(settlement__employee=emp, settlement__status__in=SETTLED)
              .filter(wanted).filter(Q(origin_period=period) | (Q(origin_period="") & Q(settlement__period=period)))
              .select_related("component", "account", "settlement").order_by("pk")):
        out.append(SimpleNamespace(line_type=l.slip_line_type, component_id=l.component_id, component=l.component,
                                   source_ref=l.source_ref, amount=l.amount, gosi_branch=l.gosi_branch,
                                   quantity=l.quantity, origin_period=l.origin_period or l.settlement.period,
                                   account=l.account, description_en=l.description_en,
                                   description_ar=l.description_ar, settlement_id=l.settlement_id))
    return out


PERIOD_TYPES = (LT.EARNING, LT.RETRO, LT.MATERNITY_TOP_UP, LT.UNPAID_DEDUCTION, LT.SICK_DEDUCTION,
                LT.MATERNITY_SUSPENDED)


def _net_key(line_type, component_id, source_ref):
    """(key, sign) of a contract-pay line when a period is netted against what earlier runs booked for it; None for
    lines that are never netted (inputs, loans, GOSI, EOSB)."""
    if source_ref.startswith("input:"):
        return None
    if source_ref.startswith("overpaid:"):  # salary beyond the last working day, recovered by a final settlement
        return ("pay", component_id), -1
    if line_type == LT.RETRO and source_ref.startswith("release:"):
        return ("red:" + source_ref.split(":")[1], component_id), -1
    if line_type in (LT.EARNING, LT.RETRO):
        return ("pay", component_id), 1
    if line_type == LT.MATERNITY_TOP_UP:
        return ("top_up", None), 1
    if line_type in PayslipLine.REDUCTIONS:
        return ("red:" + line_type, component_id), 1
    return None


def _net_against_booked(ctx, slip, emp, period, *, origin, mode, booked=None):
    """Replace the contract-pay lines of ``period`` in ``slip`` by what is still due after the approved runs that
    already booked that period (review 2: an off-cycle or later run never pays a month again; C-A10: a regular run
    pays the rest after a leave advance). ``mode``: ``current`` (the run's own period), ``prior`` (an earlier month:
    a pay decrease or a new reduction is a warning only, Art. 92) or ``final`` (a final settlement: an overpayment is a
    debt recovered from the final dues, Art. 88). Lines of other kinds are kept as they are."""
    booked = list(booked if booked is not None else _booked_lines(emp, period, PERIOD_TYPES))
    if not booked and mode == "current":
        return
    never_paid = not any(_net_key(l.line_type, l.component_id, l.source_ref) for l in booked)
    have, sample = {}, {}
    for l in booked:
        k = _net_key(l.line_type, l.component_id, l.source_ref)
        if k is None:
            continue
        have[k[0]] = have.get(k[0], ZERO) + k[1] * l.amount
        sample.setdefault(k[0], l)
    want, template, keep = {}, {}, []
    for l in slip.lines:
        k = _net_key(l.line_type, l.component.pk if l.component else None, l.source_ref)
        if k is None or (l.origin_period or "") != (origin if mode != "current" else ""):
            keep.append(l)
            continue
        want[k[0]] = want.get(k[0], ZERO) + k[1] * l.amount
        template.setdefault(k[0], l)
    out = []
    label = f" {period}" if mode != "current" else ""
    for key in sorted(set(want) | set(have), key=lambda x: (x[0], x[1] or 0)):
        delta = want.get(key, ZERO) - have.get(key, ZERO)
        if not delta:
            continue
        t = template.get(key)
        s = sample.get(key)
        comp = t.component if t is not None else (s.component if s is not None else None)
        account = t.account if t is not None else s.account
        en = t.desc_en if t is not None else s.description_en
        ar = t.desc_ar if t is not None else s.description_ar
        kind = key[0]
        if kind in ("pay", "top_up"):
            if delta > 0:
                ltype = (LT.RETRO if mode == "prior" else LT.EARNING) if kind == "pay" else LT.MATERNITY_TOP_UP
                den, dar = f"{en}{label}", f"{ar}{label}"
                if ltype == LT.RETRO and comp is not None:
                    den, dar = f"Retro {comp.code} for {period}", f"فرق بأثر رجعي {comp.name_ar} عن {period}"
                out.append(Line(ltype, delta, account, den, dar, component=comp,
                                quantity=t.quantity if t is not None else None, origin_period=origin))
            elif mode == "final":
                out.append(Line(LT.DEDUCTION, -delta, account, f"Salary paid for {period} beyond the last working day",
                                f"راتب مدفوع عن {period} بعد آخر يوم عمل", component=comp, origin_period=origin,
                                source_ref=f"overpaid:{period}"))
            else:
                slip.warn(f"{en} for {period}: {-delta} more was paid than is due now; recover only by an approved "
                          "deduction with written consent (Art. 92).",
                          f"{ar} عن {period}: دُفع {-delta} أكثر من المستحق الآن؛ لا يُسترد إلا باستقطاع معتمد بموافقة "
                          "كتابية (المادة 92).")
        else:
            ltype = kind.split(":", 1)[1]
            if delta < 0:  # less reduction than booked: release (e.g. carried maternity days now confirmed or refused)
                out.append(Line(LT.RETRO, -delta, account, f"Released: {en}{label}", f"صرف المحتجز: {ar}{label}",
                                component=comp, origin_period=origin, source_ref=f"release:{ltype}"))
            elif mode == "prior" and not never_paid:
                slip.warn(f"{en} for {period} found after the month was paid ({delta}); recover only by an approved "
                          "deduction with written consent (Art. 92).",
                          f"{ar} عن {period} ظهر بعد صرف الشهر ({delta})؛ لا يُسترد إلا باستقطاع معتمد بموافقة كتابية "
                          "(المادة 92).")
            else:
                out.append(Line(ltype, delta, account, f"{en}{label}", f"{ar}{label}", component=comp,
                                quantity=t.quantity if t is not None else None, origin_period=origin))
    slip.lines = keep + out


def _covered_gosi_wage(emp, period, booked, branch, employer_side):
    """The wage of ``period`` already subject to GOSI for a branch (R58): the GOSI wage of the period's approved
    payslips (any run kind) plus the retro wage deltas later runs booked for it. A share missing on a covered wage is a
    missed share (catch-up or borne); a wage not covered yet is paid now with its shares (retro)."""
    base = Payslip.objects.filter(employee=emp, run__period=period, refused=False,
                                  run__status__in=(RS.APPROVED, RS.PAID)).aggregate(t=Sum("gosi_wage"))["t"] or ZERO
    if employer_side:
        rows = [l for l in booked if l.gosi_branch == branch and l.line_type == LT.EMPLOYER_GOSI
                and not l.source_ref.startswith("borne:")]
    else:
        rows = [l for l in booked if l.gosi_branch == branch and l.line_type == LT.EMPLOYEE_GOSI]
    return base + sum((l.quantity for l in rows if l.quantity is not None and l.origin_period == period), ZERO)


GOSI_TYPES = (LT.EMPLOYEE_GOSI, LT.EMPLOYER_GOSI, LT.GOSI_CATCH_UP, LT.GOSI_UNCOLLECTED)
GOSI_BOOKED_TYPES = GOSI_TYPES


def _credits_2340(qs):
    """C-B7: the payslip lines credited to 2340 (GOSI payable): both shares, the uncollected employee share and a
    catch-up booked to 2340; a catch-up recovering an advanced share credits 1230 instead."""
    return qs.filter(Q(line_type__in=(LT.EMPLOYEE_GOSI, LT.EMPLOYER_GOSI, LT.GOSI_UNCOLLECTED))
                     | Q(line_type=LT.GOSI_CATCH_UP, account__code=GOSI_PAYABLE))


def _prior_corrections(ctx, slip, emp, versions):
    """R14 (retro pay) and R58 (GOSI shares of earlier months), review 10: every earlier month of the group with an
    approved regular run, from the hire month, in which the employee was employed is re-examined, whether or not the
    employee had a payslip in it (a contract approved late, a payslip refused and not yet paid off-cycle)."""
    hire_p = _period_of(emp.hire_date)
    periods = sorted(set(PayrollRun.objects.filter(
        entity=ctx.entity, payroll_group=ctx.run.payroll_group, kind=PayrollRun.Kind.REGULAR,
        status__in=(RS.APPROVED, RS.PAID), period__lt=ctx.period, period__gte=hire_p).values_list("period", flat=True)))
    for q in periods:
        info = _employment(emp, q, ctx.s, versions)
        if info is None:
            continue
        ps = (Payslip.objects.filter(employee=emp, run__period=q, refused=False, run__status__in=(RS.APPROVED, RS.PAID))
              .order_by("run__kind", "-pk").first())
        # Retro earnings (R14): the month's contract pay under the versions approved now, against what was booked.
        month = Slip(employee=emp, basis=slip.basis, nationality_class=slip.nationality_class, cohort=slip.cohort,
                     currency=slip.currency)
        try:
            _month_pay_lines(ctx, month, emp, info)
        except _Refused:
            slip.warn(f"Month {q} not re-examined: {month.refusal}", f"لم يُراجع شهر {q}: {month.refusal}")
            continue
        for l in month.lines:
            l.origin_period = q
        _net_against_booked(ctx, month, emp, q, origin=q, mode="prior")
        for w_ in month.warnings:
            if w_ not in slip.warnings:
                slip.warnings.append(w_)
        slip.lines.extend(month.lines)
        # GOSI of the earlier month on the wage now known (R58).
        cohort = ps.cohort if ps else (emp.gosi_cohort or "")
        nclass = ps.nationality_class if ps else _nationality_class(emp)
        basis = ps.employment_basis if ps else emp.employment_basis
        try:
            sub = Slip(employee=emp, basis=basis, nationality_class=nclass, cohort=cohort, currency=slip.currency)
            g = _compute_gosi(ctx, sub, emp, info, cohort=cohort, nclass=nclass, basis=basis,
                             remuneration_sar=(ps.gosi_wage_full if ps and basis == Basis.REMUNERATION else None))
        except _Refused:
            continue
        booked = _booked_lines(emp, q, GOSI_TYPES)
        for branch, wage, ee, er in g.branches:
            en, ar = BRANCH_TEXT[branch]
            rows = [l for l in booked if l.gosi_branch == branch]
            ee_rows = [l for l in rows if l.line_type in (LT.EMPLOYEE_GOSI, LT.GOSI_UNCOLLECTED)
                       or (l.line_type == LT.GOSI_CATCH_UP and l.account.code == GOSI_PAYABLE)
                       or l.source_ref.startswith("borne:")]
            er_rows = [l for l in rows if l.line_type == LT.EMPLOYER_GOSI and not l.source_ref.startswith("borne:")]
            covered = _covered_gosi_wage(emp, q, rows, branch, bool(er))
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
                if missed > 0:
                    _missed_share(ctx, slip, side, branch, pct, missed, q, cohort, nclass)
            # C-B7: an employee share advanced for month q (Dr 1230) is recovered with notice, within the caps.
            advanced = sum((l.amount for l in rows if l.line_type == LT.GOSI_UNCOLLECTED), ZERO)
            recovered = sum((l.amount for l in rows if l.line_type == LT.GOSI_CATCH_UP and l.account.code == LOANS),
                            ZERO)
            if advanced - recovered > 0:
                _recover_share(ctx, slip, branch, ee, advanced - recovered, q)


def _missed_share(ctx, slip, side, branch, pct, missed, q, cohort, nclass):
    """R58: a GOSI share of month ``q`` that was never booked. The employer share is booked now; a missed employee
    share is deducted later with a notice (``new_m273``, P19(j)) or borne by the employer (``legacy_m33``, SIL M/33
    Art. 19(4))."""
    en, ar = BRANCH_TEXT[branch]
    if side == "employer":
        slip.lines.append(Line(LT.EMPLOYER_GOSI, missed, ctx.account(GOSI_EXPENSE),
                               f"GOSI {en} employer share missed for {q}", f"حصة صاحب العمل الفائتة عن {q}",
                               branch=branch, rate=pct, origin_period=q))
    elif cohort == "legacy_m33" or nclass != Payslip.NationalityClass.SAUDI:
        slip.lines.append(Line(LT.EMPLOYER_GOSI, missed, ctx.account(GOSI_EXPENSE),
                               f"GOSI {en} employee share for {q} borne by the employer (SIL M/33 Art. 19(4))",
                               f"حصة الموظف عن {q} يتحملها صاحب العمل (المادة 19(4))", branch=branch,
                               rate=pct, origin_period=q, source_ref=f"borne:{branch}"))
        slip.warn(f"Missed employee GOSI share for {q} ({missed}) is not deducted later: borne by the employer "
                  "(SIL M/33 Art. 19(4), R58).", f"لا تُستقطع حصة الموظف الفائتة عن {q} لاحقًا ويتحملها "
                  "صاحب العمل (المادة 19(4)).")
    else:
        # C-B7: booked to 2340 in full now (Dr 1230, receivable from the employee) and recovered with notice.
        slip.lines.append(Line(LT.GOSI_UNCOLLECTED, missed, ctx.account(LOANS),
                               f"GOSI {en} employee share missed for {q}, paid by the employer and recovered later",
                               f"حصة الموظف في {ar} الفائتة عن {q} يدفعها صاحب العمل وتُسترد لاحقًا", branch=branch,
                               rate=pct, origin_period=q))
        _recover_share(ctx, slip, branch, pct, missed, q)


def _recover_share(ctx, slip, branch, pct, amount, q):
    """R58 (P19(j)), C-B7: deduct an employee GOSI share the employer advanced for month ``q`` (Cr 1230), within the
    caps (what the caps leave stays receivable for the next run), with the notice to the employee."""
    en, ar = BRANCH_TEXT[branch]
    slip.lines.append(Line(LT.GOSI_CATCH_UP, amount, ctx.account(LOANS),
                           f"GOSI {en} employee share for {q} recovered (advanced by the employer), with notice",
                           f"استرداد حصة الموظف في {ar} عن {q} (دفعها صاحب العمل) مع الإشعار", branch=branch, rate=pct,
                           origin_period=q))


def _catch_up_notices(slip):
    """R58 (P19(j)): one notice per month of origin for what this payslip actually recovers (after the caps)."""
    by_q = {}
    for l in slip.lines:
        if l.line_type == LT.GOSI_CATCH_UP and l.amount > 0:
            by_q[l.origin_period] = by_q.get(l.origin_period, ZERO) + l.amount
    for q, amount in sorted(by_q.items()):
        slip.notices.append((q, amount,
                             f"Your GOSI share for {q} ({amount} SAR) was not deducted then; the employer paid it to "
                             "GOSI and it is deducted from this salary (Law Art. 9(1); P19(j)).",
                             f"لم تُستقطع حصتك في التأمينات عن {q} ({amount} ريال) في حينها؛ ودفعها صاحب العمل للتأمينات "
                             "وتُستقطع من هذا الراتب."))


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
    _statutory_deduction_caps(ctx, slip, emp, info, due, actual, caps)
    gosi_ee = [l for l in slip.lines if l.line_type == LT.EMPLOYEE_GOSI]
    room = total_cap - fixed
    if room < 0:
        slip.refuse(f"Deductions {fixed} exceed half of the wage due ({total_cap}) (Labour Law Art. 93, R18).",
                    f"الاستقطاعات {fixed} تتجاوز نصف الأجر المستحق ({total_cap}) (المادة 93).")
    for l in gosi_ee:  # the statutory share first; what cannot be covered is caught up later (R61(f), R58)
        if l.amount > room:
            short = l.amount - max(room, ZERO)
            _uncollected_share(ctx, slip, l, short)
            l.amount = max(room, ZERO)
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


def _uncollected_share(ctx, slip, line, short):
    """C-B7: the full GOSI share is booked to 2340 every month. The part of the employee share the net pay cannot
    cover is advanced by the employer: a receivable from the employee (Dr 1230, recovered later with notice; new law,
    P19(j)) or, for ``legacy_m33``, employer cost (Dr 5110; SIL M/33 Art. 19(4): never deducted later)."""
    en, ar = BRANCH_TEXT[line.branch]
    if slip.cohort == "new_m273" and slip.nationality_class == Payslip.NationalityClass.SAUDI:
        slip.lines.append(Line(LT.GOSI_UNCOLLECTED, short, ctx.account(LOANS),
                               f"GOSI {en} employee share not covered by this pay: paid by the employer, recovered later",
                               f"حصة الموظف في {ar} لم يغطها هذا الأجر: يدفعها صاحب العمل وتُسترد لاحقًا",
                               branch=line.branch, rate=line.rate))
        slip.warn(f"Employee GOSI share {short} not covered by the net pay: booked to GOSI in full, recovered from a later "
                  "salary with notice (R58, C-B7).", f"حصة الموظف في التأمينات {short} لم يغطها صافي الأجر: قُيدت للتأمينات "
                  "كاملة وتُسترد من راتب لاحق مع الإشعار.")
    else:
        slip.lines.append(Line(LT.EMPLOYER_GOSI, short, ctx.account(GOSI_EXPENSE),
                               f"GOSI {en} employee share not covered by this pay, borne by the employer (SIL M/33 Art. "
                               "19(4))", f"حصة الموظف في {ar} لم يغطها هذا الأجر ويتحملها صاحب العمل (المادة 19(4))",
                               branch=line.branch, rate=line.rate, source_ref=f"borne:{line.branch}"))
        slip.warn(f"Employee GOSI share {short} not covered by the net pay: borne by the employer (SIL M/33 Art. 19(4), "
                  "C-B7).", f"حصة الموظف في التأمينات {short} لم يغطها صافي الأجر ويتحملها صاحب العمل (المادة 19(4)).")


def _statutory_deduction_caps(ctx, slip, emp, info, due, actual, caps):
    """C-A4: court-ordered deductions at most a quarter of the wage due for the month and damage recovery at most 5
    days' wage a month (Labour Law Art. 92(6), 91), counting every deduction of that basis the period's approved runs
    already took."""
    for basis, en, ar in ((PayrollInput.DeductionBasis.COURT_ORDER, "Court-ordered deductions", "الاستقطاعات بحكم قضائي"),
                          (PayrollInput.DeductionBasis.DAMAGE_ART91, "Damage recovery", "اقتطاع تعويض الإتلاف")):
        this = sum((l.amount for l in slip.lines if l.line_type == LT.DEDUCTION and l.basis == basis), ZERO)
        if not this:
            continue
        booked = PayrollInput.objects.filter(employee=emp, kind=IK.ONE_OFF_DEDUCTION, deduction_basis=basis,
                                             taken_run__period=ctx.period,
                                             taken_run__status__in=(RS.APPROVED, RS.PAID)).aggregate(t=Sum("amount"))["t"]
        booked = _round(booked or ZERO)
        if basis == PayrollInput.DeductionBasis.COURT_ORDER:
            month_due = due + (Payslip.objects.filter(employee=emp, run__period=ctx.period, refused=False,
                                                      run__status__in=(RS.APPROVED, RS.PAID))
                               .aggregate(t=Sum("gross"))["t"] or ZERO)
            limit = _round(Fraction(max(month_due, ZERO)) * Fraction(caps["court_max_fraction"]))
        else:
            limit = _round(Fraction(actual) * int(caps["damage_max_days_month"]) / 30)
        if this + booked > limit:
            slip.refuse(f"{en} of {ctx.period} ({this + booked}) exceed the monthly limit {limit} (Art. 92(6), 91; C-A4).",
                        f"{ar} لفترة {ctx.period} ({this + booked}) تتجاوز الحد الشهري {limit}.")


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
    if FinalSettlement.objects.filter(employee=emp, status__in=SETTLED).exists():
        # Re-review 6: the final settlement used and released the provision; nothing accrues after it.
        slip.warn("EOSB provision not accrued: the final settlement is approved.",
                  "لم يُحتسب مخصص نهاية الخدمة: المخالصة النهائية معتمدة.")
        return
    try:
        result = hr.calculate_eosb(emp, as_of, Termination.Kind.EMPLOYER_TERMINATION, user=ctx.user)
    except hr.HrError as exc:
        # Review 16: only the refusals that mean "no provision for this person" become a warning; a missing or
        # unverified parameter, a permission refusal or a bug stops the calculation.
        if getattr(exc, "code", None) not in EOSB_SKIPPABLE:
            raise
        slip.warn(f"EOSB provision not computed: {exc.text('en')}", f"لم يُحتسب مخصص نهاية الخدمة: {exc.text('ar')}")
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


def _input_lines(ctx, slip, info, inputs, v_last):
    """Lines of the approved inputs a run takes (R15, R16, R18; review 9). Explicit: never netted."""
    for inp in [i for i in inputs if i.kind == IK.UNPAID_DAYS]:
        parts = _by_component(info, [info.last], "deducted_for_unpaid_leave", Fraction(inp.quantity), weighted=False)
        slip.lines.extend(_reduction_lines(LT.UNPAID_DEDUCTION, parts, f"Unpaid absence {inp.quantity} days",
                                           f"غياب بدون أجر {inp.quantity} يوم", inp.quantity, f"input:{inp.pk}"))
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
                                   source_ref=f"input:{inp.pk}", basis=inp.deduction_basis))


def _gosi_for_period(ctx, slip, emp, info, nclass):
    """R20 to R22 for the run's own period, less the wage that approved runs of the period already covered (an
    off-cycle run never charges a month's GOSI twice, review 2)."""
    g = _compute_gosi(ctx, slip, emp, info, cohort=slip.cohort, nclass=nclass, basis=emp.employment_basis)
    slip.gosi_wage, slip.gosi_wage_full = g.wage, g.wage_full
    booked = _booked_lines(emp, ctx.period, GOSI_TYPES)
    if booked or Payslip.objects.filter(employee=emp, run__period=ctx.period, refused=False,
                                        run__status__in=(RS.APPROVED, RS.PAID)).exists():
        branches = []
        for branch, wage, ee, er in g.branches:
            delta = wage - _covered_gosi_wage(emp, ctx.period, booked, branch, bool(er))
            if delta > 0:
                branches.append((branch, delta, ee, er))
            elif delta < 0:  # e.g. a full month booked for a leaver: GOSI collects the leaving month on days (R66)
                slip.warn(f"GOSI {branch} was booked for {ctx.period} on {-delta} more wage than is due now: correct it "
                          "with GOSI and the GOSI reconciliation (R23).",
                          f"قُيدت اشتراكات {branch} عن {ctx.period} على أجر يزيد {-delta} على المستحق الآن: يُصحح مع "
                          "التأمينات وفي مطابقة التأمينات.")
        g.branches = branches
        slip.gosi_wage = max((w_ for _, w_, _, _ in branches), default=ZERO)
    slip.lines.extend(_gosi_lines(ctx, slip, g))


MODES = ("regular", "off_cycle", "final")


def _calc_leave_advance(ctx, emp, info):
    """C-A10 (Labour Law Art. 109: the leave wage is paid in advance): the contract pay of the annual leave days of
    the period, net of what the period's approved runs already paid. No inputs, GOSI, loans or EOSB here: the
    regular run of the period computes them and pays the rest of the month, net of this advance."""
    req = ctx.run.leave_request
    v_last = info.version_on(info.last)
    slip = Slip(employee=emp, basis=emp.employment_basis, nationality_class=_nationality_class(emp),
                cohort=emp.gosi_cohort or "", currency=ctx.entity.base_currency, contract=v_last,
                iban_hash=_iban_hash(emp.iban), termination_ref=getattr(_termination(emp), "pk", None))
    days = [d for d, _ in info.days if req.start_date <= d <= req.end_date]
    weight = Decimal(_weight(info, days))
    slip.paid_days = weight
    for comp, amount in _allocate(_by_component(info, days, None)).items():
        if amount:
            slip.lines.append(Line(LT.EARNING, amount, comp.expense_account,
                                   f"{comp.name_en}: annual leave pay in advance (Art. 109)",
                                   f"{comp.name_ar}: أجر الإجازة السنوية مقدمًا (المادة 109)", component=comp,
                                   quantity=weight))
    _net_against_booked(ctx, slip, emp, ctx.period, origin="", mode="current")
    return slip


def _calc_ksa(ctx, emp, versions, info, mode=None):
    """One KSA payslip. ``regular``: the month's pay, retro and GOSI corrections of earlier months, loans, caps, EOSB.
    ``off_cycle`` (review 2): only what the period's approved runs have not paid yet (a payslip refused in the regular
    run, carried maternity days now confirmed or refused) and the inputs not yet taken; no loans, no EOSB, no
    re-examination of earlier months. ``final`` (C-A11, R34): the leaving month to the last working day net of what
    was paid, earlier months re-examined, every input not yet taken; no caps, no loans and no EOSB here (the final
    settlement adds them)."""
    mode = mode or ("off_cycle" if ctx.run.kind == PayrollRun.Kind.OFF_CYCLE else "regular")
    if mode == "off_cycle" and getattr(ctx.run, "purpose", "") == "leave_advance":
        return _calc_leave_advance(ctx, emp, info)
    nclass = _nationality_class(emp)
    v_last = info.version_on(info.last)
    slip = Slip(employee=emp, basis=emp.employment_basis, nationality_class=nclass, cohort=emp.gosi_cohort or "",
                currency=ctx.entity.base_currency, contract=v_last, paid_days=Decimal(info.paid_days),
                iban_hash=_iban_hash(emp.iban), termination_ref=getattr(_termination(emp), "pk", None))
    try:
        inputs = _employee_inputs(ctx, emp, every_period=mode == "final")
        slip.input_ids = [i.pk for i in inputs]
        _month_pay_lines(ctx, slip, emp, info, inputs)
        _net_against_booked(ctx, slip, emp, ctx.period, origin="", mode="final" if mode == "final" else "current")
        if mode == "off_cycle":
            gross = slip.total(PayslipLine.EARNINGS) - slip.total(PayslipLine.REDUCTIONS)
            if gross < 0:
                slip.lines = [l for l in slip.lines if l.line_type not in PayslipLine.REDUCTIONS]
                slip.warn(f"Reductions found after the month was paid ({-gross}) are not recovered by this run; recover "
                          "only by an approved deduction with written consent (Art. 92).",
                          f"تخفيضات ظهرت بعد صرف الشهر ({-gross}) لا يستردها هذا المسير؛ لا تُسترد إلا باستقطاع معتمد "
                          "بموافقة كتابية (المادة 92).")
        _input_lines(ctx, slip, info, inputs, v_last)
        prior = _prior_payslips(ctx, emp)
        if mode in ("regular", "final"):
            _prior_corrections(ctx, slip, emp, versions)
        _gosi_for_period(ctx, slip, emp, info, nclass)
        if mode == "regular":
            _loan_lines(ctx, slip, emp)
        if mode != "final":
            _apply_caps(ctx, slip, emp, info)
        _catch_up_notices(slip)
        if mode == "regular":
            _eosb(ctx, slip, emp, info)
        _ksa_warnings(ctx, slip, emp, info, prior)
        lt = hr.LeaveType.objects.filter(entity=emp.entity, code="ANNUAL").first()
        if lt is not None:
            slip.leave_balance = hr._balance(emp, lt)
        net = slip.total(PayslipLine.EARNINGS) - slip.total(PayslipLine.REDUCTIONS) - slip.total(PayslipLine.DEDUCTIONS)
        if net < 0 and mode != "final":
            slip.refuse(f"Net pay would be negative ({net}) (R18).", f"سيكون صافي الراتب سالبًا ({net}).")
    except _Refused:
        slip.lines = []
    return slip


def _remuneration_prior(ctx, slip, emp, versions, cur, rate, add, sar):
    """Slice 2 (§10.8 item 4; R14, R58, R62 for remuneration staff): every earlier month of the out-of-country group
    with an approved regular run, from the hire month, is re-examined. Contract pay per component in the contract
    currency under the versions approved now, against what was booked for the month: a positive difference is a
    ``retro`` line paid now at this run's rate; a decrease is a warning (recovered only with written consent). For a
    subordinate Saudi (R41, R62) the month's GOSI annuities are recomputed on the remuneration now known at that
    month's rate: shares on a retro wage are booked now (the employee share deducted); a share missed on a covered
    wage is booked now, the employee share caught up with notice for ``new_m273`` (P19(j)) or borne by the employer
    for ``legacy_m33`` (SIL M/33 Art. 19(4)). No Saudi deduction caps apply (R41); net pay must stay >= 0."""
    hire_p = _period_of(emp.hire_date)
    periods = sorted(set(PayrollRun.objects.filter(
        entity=ctx.entity, payroll_group=PayrollGroup.OUT_OF_COUNTRY, kind=PayrollRun.Kind.REGULAR,
        status__in=(RS.APPROVED, RS.PAID), period__lt=ctx.period, period__gte=hire_p).values_list("period", flat=True)))
    saudi_sub = (slip.nationality_class == Payslip.NationalityClass.SAUDI
                 and emp.gosi_employment_relationship == Employee.GosiRelationship.SUBORDINATE)

    def employee_line(ltype, amount_sar, account, en, ar, **kw):
        txn = _round_to(Fraction(amount_sar) / Fraction(rate), cur.minor_units)
        slip.lines.append(Line(ltype, sar(txn), account, en, ar, amount_txn=txn, **kw))

    for q in periods:
        info = _employment(emp, q, ctx.s, versions)
        if info is None:
            continue
        booked = list(PayslipLine.objects.filter(payslip__employee=emp, payslip__refused=False,
                                                 payslip__run__status__in=(RS.APPROVED, RS.PAID))
                      .filter(Q(origin_period=q) | (Q(origin_period="") & Q(payslip__run__period=q)))
                      .select_related("account", "component"))
        have = {}
        for l in booked:
            if l.line_type in (LT.EARNING, LT.RETRO) and l.currency_id == cur.code and not l.source_ref.startswith("input:"):
                have[l.component_id] = have.get(l.component_id, ZERO) + l.amount_txn
        for comp, exact in _component_earnings(info, exact=True).values():
            delta = _round_to(exact, cur.minor_units) - have.get(comp.pk, ZERO)
            if delta > 0:
                add(LT.RETRO, delta, comp.expense_account, f"Retro {comp.code} for {q}", f"فرق بأثر رجعي {comp.name_ar} عن {q}",
                    component=comp, origin_period=q)
            elif delta < 0:
                slip.warn(f"{comp.code} for {q}: {-delta} {cur.code} more was paid than is due now; recover only with "
                          "the person's written consent.", f"{comp.name_ar} عن {q}: دُفع {-delta} {cur.code} أكثر من "
                          "المستحق الآن؛ لا يُسترد إلا بموافقة كتابية.")
        if not saudi_sub:
            continue
        ps = (Payslip.objects.filter(employee=emp, run__period=q, refused=False, run__status__in=(RS.APPROVED, RS.PAID))
              .order_by("pk").first())
        q_rate = ps.fx_rate if ps is not None else rate
        remuneration = sum((Fraction(c.monthly_amount) for c in _components(info.version_on(info.last))
                            if c.component.code == "REMUNERATION"), Fraction(0))
        sub = Slip(employee=emp, basis=emp.employment_basis, nationality_class=slip.nationality_class,
                   cohort=emp.gosi_cohort or "", currency=cur)
        try:
            g = _compute_gosi(ctx, sub, emp, info, cohort=sub.cohort, nclass=slip.nationality_class,
                              basis=emp.employment_basis, remuneration_sar=_round(remuneration * Fraction(q_rate)))
        except _Refused:
            slip.warn(f"GOSI of {q} not re-examined: {sub.refusal}", f"لم تُراجع اشتراكات {q}: {sub.refusal}")
            continue
        rows = [l for l in booked if l.line_type in GOSI_TYPES]
        for branch, wage, ee, er in g.branches:
            en, ar = BRANCH_TEXT[branch]
            brows = [l for l in rows if l.gosi_branch == branch]
            ee_rows = [l for l in brows if l.line_type == LT.EMPLOYEE_GOSI or l.source_ref.startswith("borne:")
                       or (l.line_type == LT.GOSI_CATCH_UP and l.account.code == GOSI_PAYABLE)]
            er_rows = [l for l in brows if l.line_type == LT.EMPLOYER_GOSI and not l.source_ref.startswith("borne:")]
            covered = _covered_gosi_wage(emp, q, brows, branch, bool(er))
            delta = wage - covered
            for side, pct, rows_ in (("employee", ee, ee_rows), ("employer", er, er_rows)):
                if not pct:
                    continue
                at_covered = _round(Fraction(covered) * Fraction(pct) / 100)
                retro = _round(Fraction(wage) * Fraction(pct) / 100) - at_covered if delta > 0 else ZERO
                missed = at_covered - sum((l.amount for l in rows_), ZERO)
                if retro > 0 and side == "employer":
                    slip.lines.append(Line(LT.EMPLOYER_GOSI, retro, ctx.account(GOSI_EXPENSE),
                                           f"GOSI {en} employer on retro wage for {q}", f"التأمينات {ar} على فرق الأجر عن {q}",
                                           branch=branch, quantity=delta, rate=pct, origin_period=q))
                elif retro > 0:
                    employee_line(LT.EMPLOYEE_GOSI, retro, ctx.account(GOSI_PAYABLE),
                                  f"GOSI {en} employee on retro wage for {q}", f"التأمينات {ar} على فرق الأجر عن {q}",
                                  branch=branch, quantity=delta, rate=pct, origin_period=q)
                if missed <= 0:
                    continue
                if side == "employer":
                    slip.lines.append(Line(LT.EMPLOYER_GOSI, missed, ctx.account(GOSI_EXPENSE),
                                           f"GOSI {en} employer share missed for {q}", f"حصة صاحب العمل الفائتة عن {q}",
                                           branch=branch, rate=pct, origin_period=q))
                elif sub.cohort == "legacy_m33":
                    slip.lines.append(Line(LT.EMPLOYER_GOSI, missed, ctx.account(GOSI_EXPENSE),
                                           f"GOSI {en} employee share for {q} borne by the employer (SIL M/33 Art. 19(4))",
                                           f"حصة الموظف عن {q} يتحملها صاحب العمل (المادة 19(4))", branch=branch,
                                           rate=pct, origin_period=q, source_ref=f"borne:{branch}"))
                    slip.warn(f"Missed employee GOSI share for {q} ({missed}) is not deducted later: borne by the "
                              "employer (SIL M/33 Art. 19(4), R58).", f"لا تُستقطع حصة الموظف الفائتة عن {q} لاحقًا "
                              "ويتحملها صاحب العمل (المادة 19(4)).")
                else:
                    employee_line(LT.GOSI_CATCH_UP, missed, ctx.account(GOSI_PAYABLE),
                                  f"GOSI {en} employee share for {q} deducted now, with notice (P19(j))",
                                  f"حصة الموظف في {ar} عن {q} تُستقطع الآن مع الإشعار", branch=branch, rate=pct,
                                  origin_period=q)


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
        if ctx.run.kind == PayrollRun.Kind.REGULAR:
            # Slice 2 (§10.8 item 4): earlier months re-examined (retro pay in the contract currency, GOSI shares of
            # a subordinate Saudi on the wage now known: retro, missed shares caught up with notice or borne).
            _remuneration_prior(ctx, slip, emp, versions, cur, rate, add, sar)
            _catch_up_notices(slip)
        if cur.code != ctx.entity.base_currency_id and len({l.account.pk for l in slip.lines
                                                            if l.line_type in PayslipLine.EARNINGS}) > 1:
            slip.refuse("Foreign-currency remuneration earnings use one expense account in slice 1 (R42).",
                        "تستخدم استحقاقات المكافأة بالعملة الأجنبية حساب مصروف واحدًا.")
        net = slip.total_txn(PayslipLine.EARNINGS) - slip.total_txn(PayslipLine.DEDUCTIONS)
        if net < 0:
            slip.refuse(f"Net pay would be negative ({net}) (R41).", f"سيكون صافي المكافأة سالبًا ({net}).")
    except _Refused:
        slip.lines = []
    return slip


# =====================================================================================================
# Runs (R6 to R13, R36, R39, R44, R50)
# =====================================================================================================

def _default_pay_date(settings, period, group=PayrollGroup.IN_COUNTRY):
    start = _period_bounds(period)[0]
    base = start if group == PayrollGroup.IN_COUNTRY else _period_bounds(_next_period(period))[0]
    return min(base.replace(day=min(settings.default_pay_day, 28)), _period_bounds(_period_of(base))[1])


def _leave_advance_periods(req, s):
    """C-A10: the periods of an approved annual leave whose pay date is after the leave starts (paid in advance)."""
    out, period = [], _period_of(req.start_date)
    while period <= _period_of(req.end_date):
        if req.start_date < _default_pay_date(s, period):
            out.append(period)
        period = _next_period(period)
    return out


@transaction.atomic
def prepare_leave_advance(leave_request, *, user):
    """C-A10 (Labour Law Art. 109): an approved annual leave of a KSA employee starting before the period's pay date
    is paid in advance: one off-cycle run per period of the leave whose pay date is after its start, paying the leave
    days' contract pay, with the pay date the day before the leave starts. The regular run of the period pays the
    rest of the month net of the advance. Returns the calculated runs (submitted and approved as usual)."""
    req = LeaveRequest.objects.select_related("entity", "employee", "leave_type").get(pk=leave_request.pk)
    _require(user, req.entity, "payroll.prepare_run")
    emp = req.employee
    if (req.leave_type.code != "ANNUAL" or req.status != LeaveRequest.Status.APPROVED
            or emp.employment_basis != Basis.KSA_EMPLOYEE):
        raise error("Leave pay in advance is for an approved annual leave of a KSA employee (C-A10).",
                    "أجر الإجازة مقدمًا لإجازة سنوية معتمدة لموظف خاضع لنظام العمل السعودي.", code="leave_advance")
    s = _settings_of(req.entity)
    runs = []
    for period in _leave_advance_periods(req, s):
        if PayrollRun.objects.filter(leave_request=req, period=period).exclude(status=RS.REVERSED).exists():
            continue
        run = create_run(req.entity, user=user, period=period, kind=PayrollRun.Kind.OFF_CYCLE,
                         pay_date=req.start_date - timedelta(days=1), employees=[emp], purpose="leave_advance",
                         leave_request=req, reason_ar="أجر الإجازة السنوية مقدمًا (المادة 109)",
                         reason_en="Annual leave pay in advance (Labour Law Art. 109)")
        runs.append(calculate_run(run, user=user))
    if not runs:
        raise error("Nothing to pay in advance for this leave (C-A10).", "لا يوجد ما يُدفع مقدمًا لهذه الإجازة.",
                    code="leave_advance_nothing")
    return runs


def _entry_date(period):
    return _period_bounds(period)[1]


@transaction.atomic
def create_run(entity, *, user, period, payroll_group=PayrollGroup.IN_COUNTRY, kind=PayrollRun.Kind.REGULAR,
               pay_date=None, reason_ar="", reason_en="", employees=(), purpose="", leave_request=None):
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
    if _jurisdiction_of(entity) != "SA" and (kind != PayrollRun.Kind.REGULAR
                                              or payroll_group != PayrollGroup.IN_COUNTRY):
        raise error("Egypt payroll runs are regular in-country runs (off-cycle runs there are not built; §10.8).",
                    "مسيرات مصر مسيرات عادية داخل البلد (لم تُبنَ المسيرات خارج الدورة هناك).", code="off_cycle_not_built")
    if kind == PayrollRun.Kind.OFF_CYCLE and payroll_group == PayrollGroup.OUT_OF_COUNTRY:
        raise error("Off-cycle remuneration runs are not built yet (review 2): correct in the next regular run.",
                    "لم تُبنَ المسيرات خارج الدورة للمكافآت بعد: صحّح في المسير العادي التالي.", code="off_cycle_not_built")
    s = _settings_of(entity)
    start, end = _period_bounds(period)
    if pay_date is None:
        pay_date = _default_pay_date(s, period, payroll_group)
    if purpose and (purpose != "leave_advance" or kind != PayrollRun.Kind.OFF_CYCLE or leave_request is None):
        raise error("A leave advance is an off-cycle run for one approved annual leave (C-A10).",
                    "دفعة الإجازة مقدمًا مسير خارج الدورة لإجازة سنوية معتمدة واحدة.", code="run_kind")
    run = PayrollRun(entity=entity, payroll_group=payroll_group, period=period, kind=kind, pay_date=pay_date,
                     entry_date=end, reason_ar=reason_ar, reason_en=reason_en, created_by=user, purpose=purpose,
                     leave_request=leave_request)
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
            ctx.leavers.append((emp.employee_code, term.last_working_day))
            continue
        settled = FinalSettlement.objects.filter(employee=emp, status__in=SETTLED, period__gte=ctx.period).first()
        if settled is not None:
            # Re-review 1, verification 1: the approved final settlement closed this employee's payroll up to the
            # leaving month; a run would pay or charge again what it settled. The employee is left out of the run (a
            # count warning and a note), never the whole run refused; corrections go through a later document.
            ctx.settled.append((emp.employee_code, settled.number))
            continue
        if emp.employment_basis == Basis.LOCAL_ENTITY_EMPLOYEE and ctx.jurisdiction != "EG":
            # Never computed with Saudi parameters (R41); review 17: the skip is visible on the run.
            ctx.skipped_local += 1
            continue
        calc = (_calc_eg if emp.employment_basis == Basis.LOCAL_ENTITY_EMPLOYEE
                else _calc_remuneration if emp.employment_basis == Basis.REMUNERATION else _calc_ksa)
        slip = calc(ctx, emp, versions, info)
        if ctx.run.kind == PayrollRun.Kind.OFF_CYCLE and not slip.refusal and not slip.lines:
            ctx.nothing_due.append(emp.employee_code)
            continue
        bad = sorted({l.account.code for l in slip.lines if l.account.sharia_prohibited})
        if bad and ctx.entity.sharia_enforced:  # R19, R37: checked here, and by the ledger again at posting
            slip.refusal = (f"{emp.employee_code}: account(s) {', '.join(bad)} are Sharia-prohibited in {ctx.entity.code} "
                            f"(R19, R37) / {emp.employee_code}: الحساب {', '.join(bad)} محظور شرعًا في {ctx.entity.code}")
            slip.lines = []
        slips.append(slip)
    if ctx.run.payroll_group == PayrollGroup.IN_COUNTRY and ctx.run.kind == PayrollRun.Kind.REGULAR:
        diff = _eosb_tie_out(ctx.entity, ctx.end)
        if diff:
            ctx.warnings.append(f"Account 2500 differs from the EOSB sub-ledger by {diff} (R25) / يختلف الحساب 2500 عن "
                                f"سجل مخصص نهاية الخدمة بمقدار {diff}")
    if ctx.leavers:
        n = len(ctx.leavers)
        ctx.warnings.append(f"{n} employee(s) with an approved termination in the period are paid through the final "
                            f"settlement, not the run (R10) / {n} من الموظفين بإنهاء خدمة معتمد في الفترة يُدفع لهم عبر "
                            "المخالصة النهائية لا المسير")
    if ctx.settled:
        n = len(ctx.settled)
        ctx.warnings.append(f"{n} employee(s) left out: an approved final settlement closed their payroll for "
                            f"{ctx.period} (re-review 1) / استُبعد {n} من الموظفين: أغلقت مخالصة نهائية معتمدة رواتبهم "
                            f"لفترة {ctx.period}")
    if ctx.nothing_due:
        n = len(ctx.nothing_due)
        ctx.warnings.append(f"{n} named employee(s) have nothing left to pay for the period (review 2) / لا يتبقى ما "
                            f"يُدفع للفترة لعدد {n} من الموظفين المحددين")
    if ctx.skipped_local:
        ctx.warnings.append(f"{ctx.skipped_local} local entity employee(s) left out: their local payroll is not built "
                            f"(R41, OQ-28) / استُبعد {ctx.skipped_local} من موظفي الشركات المحلية: رواتبهم المحلية غير "
                            "مبنية")
    return slips


def _eosb_tie_out(entity, as_of):
    """R25: account 2500 against the EOSB sub-ledger, both as of ``as_of`` (verification 3: sub-ledger rows by their
    date, which is the date of the entry that booked them)."""
    sub = EosbProvisionEntry.objects.filter(entity=entity, date__lte=as_of).aggregate(t=Sum("amount"))["t"] or ZERO
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
             sorted(s.loan_effects), sorted(s.sale_effects), [line(l) for l in s.lines], sorted(s.notices),
             sorted(s.input_ids), s.iban_hash, s.termination_ref]
            for s in slips]
    return hashlib.sha256(json.dumps([data, sorted(warnings), params], default=str).encode()).hexdigest()


def _stored_fingerprint(run):
    """Review 6: every stored payslip and line of the run, all columns, by primary key. The ledger lines are built
    from these rows, so approval compares this with the hash taken right after the calculation stored them."""
    def rows(model, **filters):
        names = [f.attname for f in model._meta.concrete_fields]
        return [[str(r[n]) for n in names] for r in model._base_manager.filter(**filters).order_by("pk").values(*names)]
    data = [rows(Payslip, run=run), rows(PayslipLine, payslip__run=run)]
    return hashlib.sha256(json.dumps(data, default=str).encode()).hexdigest()


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
                     refused=bool(s.refusal), refusal=s.refusal, warnings=s.warnings, notes=s.notes,
                     input_ids=sorted(s.input_ids), iban_hash=s.iban_hash, termination_ref=s.termination_ref,
                     taxable_income=s.taxable_income)
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


def _summary_warnings(slips):
    """Review 13: the run's own warnings are counts; each employee's warnings and refusal are on the payslip."""
    out = []
    warned = sum(1 for s in slips if s.warnings and not s.refusal)
    refused = sum(1 for s in slips if s.refusal)
    if warned:
        out.append(f"{warned} payslip(s) have warnings: see the payslips / {warned} من القسائم عليها تنبيهات: راجع القسائم")
    if refused:
        out.append(f"{refused} payslip(s) refused: fix them, or submit without them and pay them by an off-cycle run "
                   f"(R56, C-A2) / رُفضت {refused} من القسائم: صحّحها أو قدّم المسير دونها وادفعها بمسير خارج الدورة")
    return out


def _pay_date_warning(ctx):
    """C-A2: monthly wages are paid once a month (Labour Law Art. 90); a pay date more than a month after the previous
    run's pay date (30 days, or the same day of the next month when that is later) is a warning."""
    run = ctx.run
    prev = (PayrollRun.objects.filter(entity=run.entity, payroll_group=run.payroll_group, kind=PayrollRun.Kind.REGULAR,
                                      status__in=(RS.APPROVED, RS.PAID), period__lt=run.period)
            .order_by("-period").first())
    if prev is None or run.kind != PayrollRun.Kind.REGULAR:
        return
    p = prev.pay_date
    ny, nm = (p.year + 1, 1) if p.month == 12 else (p.year, p.month + 1)
    same_day = date(ny, nm, min(p.day, calendar.monthrange(ny, nm)[1]))
    limit = max(p + timedelta(days=30), same_day)
    if run.pay_date > limit:
        ctx.warnings.append(f"Pay date {run.pay_date} is more than a month after the previous pay date {p}: monthly wages "
                            f"are paid once a month (Labour Law Art. 90) / تاريخ الصرف {run.pay_date} بعد أكثر من شهر من "
                            f"تاريخ الصرف السابق {p}: يُصرف الأجر الشهري مرة كل شهر (المادة 90)")


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
    use = hr.ParamUse()
    ctx = Ctx(run, user, _jurisdiction_gate(run.entity, use))
    ctx.use = use
    _refresh_deferred_sales(run.entity)  # slice 2: sales posted on ZATCA reporting since enter the run (R47)
    slips = _calculate(ctx)
    _store(ctx, slips)
    run.status = RS.CALCULATED
    run.calc_version += 1
    run.parameter_ids = ctx.use.ids
    _pay_date_warning(ctx)
    run.warnings = ctx.warnings + _summary_warnings(slips)
    unverified = _local_unverified(run)
    if unverified:
        run.warnings.append(f"{len(unverified)} unverified research parameter(s) read: the run cannot be submitted or "
                            f"approved until they are verified (R4, OQ-28) / قُرئ {len(unverified)} من المعايير البحثية "
                            "غير المتحقق منها: لا يُقدم المسير ولا يُعتمد حتى يُتحقق منها")
    # Review 13: per-employee details stay on the payslips (payroll.view_payslip); the run shows counts only.
    run.employee_notes = ([f"{code}: last working day {lwd}: final settlement (R10) / {code}: آخر يوم عمل {lwd}: "
                           "المخالصة النهائية" for code, lwd in ctx.leavers]
                          + [f"{code}: final settlement {number} approved: left out / {code}: مخالصة نهائية معتمدة "
                             f"{number}: مستبعد" for code, number in ctx.settled]
                          + [f"{code}: nothing left to pay / {code}: لا يتبقى ما يُدفع" for code in ctx.nothing_due])
    run.inputs_hash = _fingerprint(slips, ctx.warnings, ctx.use.ids)
    run.stored_hash = _stored_fingerprint(run)
    run.calculated_by, run.calculated_at, run.modified_by, run.modified_at = user, _now(), user, _now()
    _save(run)
    _send_alerts(run.entity, [a for s in slips for a in s.alerts])
    return run


@transaction.atomic
def submit_run(run, *, user, exclude_refused=False):
    """R7, R56 as amended by C-A2: submit a calculated run (``payroll.submit_run``). While payslips are refused the
    run is submitted only with ``exclude_refused``: the refused payslips post nothing, take no input or installment,
    and their employees are paid by an off-cycle run once fixed (warning on the run)."""
    run = _lock(PayrollRun, run.pk)
    _require(user, run.entity, "payroll.submit_run")
    if run.status != RS.CALCULATED:
        raise error("Only a calculated run is submitted.", "يُقدم المسير المحتسب فقط.", code="run_status")
    refused = list(Payslip.objects.filter(run=run, refused=True).values_list("refusal", flat=True))
    if refused and not exclude_refused:
        raise _problems(["The run cannot be submitted while payslips are refused (R56): fix them, or submit without them "
                         "(exclude_refused) and pay them by an off-cycle run. / لا يُقدم المسير ما دامت قسائم مرفوضة: "
                         "صحّحها أو قدّمه دونها وادفعها بمسير خارج الدورة.", *refused], "payslips_refused")
    if not Payslip.objects.filter(run=run, refused=False).exists():
        raise error("The run has no payslips to pay.", "لا توجد قسائم للصرف في المسير.", code="run_empty")
    unverified = _local_unverified(run)
    if unverified:
        _refuse_unverified_local(unverified)
    if refused:
        n = len(refused)
        run.warnings = list(run.warnings) + [
            f"Submitted without {n} refused payslip(s): pay them by an off-cycle run (C-A2) / قُدّم دون {n} من القسائم "
            "المرفوضة: ادفعها بمسير خارج الدورة"]
    run.status, run.submitted_by, run.submitted_at = RS.SUBMITTED, user, _now()
    _save(run)
    return run


@transaction.atomic
def return_run(run, *, user, reason_ar):
    """R7: the approver returns a submitted run for recalculation (``payroll.approve_run``)."""
    run = _lock(PayrollRun, run.pk)
    grant = _require(user, run.entity, "payroll.approve_run", document=run)
    approvals.check(grant, run.entity, "payroll.run")  # review 17: the same matrix row as approval
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
    employee_ids = list(Payslip.objects.filter(run=run, refused=False).values_list("employee_id", flat=True))
    input_ids = [i for ids in Payslip.objects.filter(run=run, refused=False).values_list("input_ids", flat=True)
                 for i in (ids or [])]
    inputs = PayrollInput.objects.filter(pk__in=input_ids)
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
    _entity_lock(run.entity)  # review 1, 9: one approval at a time takes inputs and installments
    run = _lock(PayrollRun, run.pk)
    if run.status != RS.SUBMITTED:
        raise error("Only a submitted run is approved (R7).", "يُعتمد المسير المقدم فقط.", code="run_status")
    use = hr.ParamUse()
    ctx = Ctx(run, user, _jurisdiction_gate(run.entity, use))
    ctx.use = use  # jurisdiction.active is verified with the rest (hr R68, R7)
    slips = _calculate(ctx)
    if _fingerprint(slips, ctx.warnings, ctx.use.ids) != run.inputs_hash:
        raise error("A source changed after the calculation: recalculate the run and submit it again (R8).",
                    "تغيّر مصدر بعد الاحتساب: أعد احتساب المسير وقدّمه مجددًا.", code="recalculate")
    if _stored_fingerprint(run) != run.stored_hash:
        # Review 6: the entry is built from the stored rows; a change to them outside the calculation is refused.
        access_services.log_denial(actor=user, entity=run.entity, permission="payroll.approve_run",
                                   details={"reason": "stored_payslips_changed", "run": run.pk})
        raise error("The stored payslips of this run changed after the calculation: recalculate the run (R8, review 6).",
                    "تغيّرت قسائم هذا المسير المخزنة بعد الاحتساب: أعد احتساب المسير.", code="payslips_changed")
    ctx.use.check_verified()
    if ctx.jurisdiction != "SA":  # §10.8 item 7: Egypt activates only on verified rows
        bad = [r for r in ctx.use.rows.values() if r.status != StatutoryParameter.Status.VERIFIED]
        if bad:
            _refuse_unverified_local(bad)
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
    _apply_effects(run, slips, user)
    ctx.use.mark_used()
    return run


def _apply_effects(run, slips, user=None):
    """Approval effects: installments deducted (a reduced one carries its remainder to the next period, R18),
    EOSB sub-ledger rows (R24), catch-up notices (R58); slice 2: the bai' ajel profit of each collected installment
    recognised through invoicing (E12, Dr 1241 / Cr 4610 by collected share)."""
    for s in slips:
        if s.refusal:
            continue
        for inp in PayrollInput._base_manager.select_for_update().filter(pk__in=s.input_ids).order_by("pk"):
            if inp.taken_run_id is not None:
                raise error("An input of this run was taken by another run meanwhile: recalculate (R8, review 9).",
                            "أخذ مسير آخر مدخلًا من هذا المسير في الأثناء: أعد الاحتساب.", code="recalculate")
            inp.taken_run = run
            _save(inp)
        for ref, amount in s.loan_effects:
            inst = _lock(LoanInstallment, int(ref.split(":")[1]))
            _deduct_installment(inst, amount, run, LoanInstallment)
        for ref, amount in s.sale_effects:
            inst = _lock(DeferredSaleInstallment, int(ref.split(":")[1]))
            _deduct_installment(inst, amount, run, DeferredSaleInstallment)
            if amount > 0 and user is not None:
                _recognise_profit(inst.contract, amount, run.entry_date, _collection_ref(inst), user)
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
                if l.line_type == LT.EMPLOYEE_GOSI or (l.line_type == LT.GOSI_CATCH_UP and l.account.code == GOSI_PAYABLE):
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
            elif t in (LT.EMPLOYER_GOSI, LT.GOSI_UNCOLLECTED):
                add(l.account, l.amount)
                add(_account(run.entity, GOSI_PAYABLE), -l.amount)
            elif t in EG_EMPLOYER_PAYABLE:  # Egypt: employer social insurance and UHI, Dr expense / Cr payable
                add(l.account, l.amount)
                add(_account(run.entity, EG_ACCOUNTS[EG_EMPLOYER_PAYABLE[t]]), -l.amount)
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


def _installments_of(run):
    out = []
    for model in (LoanInstallment, DeferredSaleInstallment):
        out.extend(model._base_manager.select_for_update().filter(run=run).order_by("pk"))
    return out


def _carry_chain(inst):
    """Every remainder carried from ``inst`` (R18), followed down the chain."""
    model = type(inst)
    out, todo = [], [inst]
    while todo:
        cur = todo.pop()
        for carry in model._base_manager.select_for_update().filter(carried_from=cur).order_by("pk"):
            out.append(carry)
            todo.append(carry)
    return out


@transaction.atomic
def reverse_run(run, *, user, reason, date=None, acknowledge_wps=False, wps_reason=""):
    """R13 (E6): ``payroll.reverse_run`` by an approver who did not prepare the run, only while no payment of the run
    (and no GOSI payment covering it) is approved; the reversal is dated in an open period; installments go back to
    planned (planned remainders are superseded), EOSB sub-ledger rows are mirrored, the inputs it took are free for a
    later run. A reversed run is never re-approved.

    Refused (review 5) while a later run has deducted a remainder carried from one of its installments: reverse that
    run first. Refused (review 12) once a WPS file exists for the run, unless ``acknowledge_wps`` with a written
    ``wps_reason`` (the bank may already have paid it); the acknowledgement is logged."""
    run = PayrollRun.objects.select_related("entity", "journal_entry").get(pk=run.pk)
    grant = _require(user, run.entity, "payroll.reverse_run", document=run)
    approvals.check(grant, run.entity, "payroll.reversal")
    if user.pk in {run.created_by_id, run.calculated_by_id, run.submitted_by_id, run.modified_by_id}:
        access.deny(user, run.entity, "payroll.reverse_run", "self_approval")
    if not (reason or "").strip():
        raise error("A reason is required.", "السبب مطلوب.", code="reason_required")
    _entity_lock(run.entity)
    run = _lock(PayrollRun, run.pk)
    if run.status != RS.APPROVED or run.journal_entry_id is None:
        raise error("Only an approved, unpaid run is reversed (R13).", "يُعكس المسير المعتمد غير المدفوع فقط.",
                    code="run_status")
    if PayrollPayment.objects.filter(run=run, status=PayrollPayment.Status.APPROVED).exists():
        raise error("A payment of this run is approved: correct it in a later run instead (R13).",
                    "اعتُمدت دفعة لهذا المسير: صحّحه في مسير لاحق.", code="run_paid")
    gosi_paid = PayrollPayment.objects.filter(entity=run.entity, kind=PayrollPayment.Kind.GOSI, gosi_period=run.period,
                                              status=PayrollPayment.Status.APPROVED).aggregate(t=Sum("amount"))["t"] or ZERO
    if gosi_paid and gosi_paid > _gosi_booked(run.entity, run.period, exclude_run=run):
        raise error(f"GOSI payments of {run.period} ({gosi_paid}) cover this run's contributions: correct it in a later "
                    "run instead (R13).", f"مدفوعات التأمينات لفترة {run.period} ({gosi_paid}) تغطي اشتراكات هذا "
                    "المسير: صحّحه في مسير لاحق.", code="run_gosi_paid")
    if WpsFile.objects.filter(run=run).exists():
        if not (acknowledge_wps and (wps_reason or "").strip()):
            raise error("A salary (WPS) file was generated for this run and may have been paid by the bank: confirm "
                        "with acknowledge_wps and a written reason (review 12).",
                        "أُنشئ ملف رواتب (حماية الأجور) لهذا المسير وقد يكون البنك صرفه: أكّد ذلك مع ذكر السبب كتابةً.",
                        code="wps_file_exists")
        access_services.log(AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=run.entity,
                            permission="payroll.reverse_run",
                            details={"action": "reverse_run_after_wps_file", "run": run.pk,
                                     "wps_files": list(WpsFile.objects.filter(run=run).values_list("pk", flat=True)),
                                     "reason": wps_reason.strip()[:500],
                                     "message_en": "Run reversed after its salary file was generated",
                                     "message_ar": "عُكس المسير بعد إنشاء ملف الرواتب"})
    employees = Payslip.objects.filter(run=run, refused=False).values("employee_id")
    settlements = list(FS._base_manager.select_for_update().filter(employee_id__in=employees).exclude(
        status=FSS.DISCARDED).order_by("pk"))
    netted = [fs for fs in settlements if fs.status in (FSS.SUBMITTED, FSS.APPROVED, FSS.PAID)
              and (fs.period >= run.period or FinalSettlementLine.objects.filter(settlement=fs,
                                                                                   origin_period=run.period).exists())]
    if netted:
        # Re-review 2: the settlement netted its final dues against this run's lines (salary paid, GOSI booked,
        # shares advanced); reversing the run would leave the settlement wrong.
        raise error("A final settlement of an employee of this run is submitted or approved and was netted against it: "
                    "return the settlement first, or correct in a later document (R13, re-review 2).",
                    "مخالصة نهائية لموظف في هذا المسير مقدّمة أو معتمدة وقد احتُسبت مقابله: أعد المخالصة أولًا أو صحّح "
                    "بمستند لاحق.", code="settlement_netted")
    stale = [fs for fs in settlements if fs.status in (FSS.CALCULATED, FSS.RETURNED) and fs.period >= run.period]
    installments = _installments_of(run)
    for inst in installments:
        parent = inst.loan if isinstance(inst, LoanInstallment) else inst.contract
        rescheduled = isinstance(inst, LoanInstallment) and inst.schedule_version != parent.schedule_version
        for carry in [inst] + _carry_chain(inst):
            if (carry is not inst and carry.status != "planned") or rescheduled:
                raise error(f"A remainder of an installment of this run ({carry.period}) was deducted by a later run or "
                            "the loan was rescheduled since: reverse the later run first, or correct in a later run "
                            "(R13, R18; review 5).",
                            f"استُقطع باقي قسط من هذا المسير ({carry.period}) في مسير لاحق أو أُعيدت جدولة القرض بعده: "
                            "اعكس المسير اللاحق أولًا أو صحّح في مسير لاحق.", code="carry_deducted")
    on = _reversal_date(run.entity, run.entry_date, date)
    try:
        with _marker(_reversing, run):
            reversal = ledger.reverse_entry(run.journal_entry, grant, date=on, reason=reason.strip())
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "reversal_refused") from None
    run.status, run.reversal_entry, run.reversed_by, run.reversed_at = RS.REVERSED, reversal, user, _now()
    _save(run)
    for inst in installments:  # slice 2: the profit recognised on a collection the reversal gives back (E12)
        if isinstance(inst, DeferredSaleInstallment) and inst.deducted_amount > 0:
            _reverse_profit(inst.contract, _collection_ref(inst), user, on, reason.strip())
    # The run is reversed before its installments are freed: the PostgreSQL trigger lets a deducted installment go
    # back to planned only when its run is reversed (review 5).
    for inst in installments:
        model = type(inst)
        for carry in _carry_chain(inst):
            if model is LoanInstallment:
                carry.status = LoanInstallment.Status.SUPERSEDED
                _save(carry)
            else:
                carry._service_delete = True
                carry.delete()
        inst.status, inst.deducted_amount, inst.run = model.Status.PLANNED, ZERO, None
        _save(inst)
    for row in EosbProvisionEntry.objects.filter(run=run, kind=EosbProvisionEntry.Kind.ACCRUAL):
        _save(EosbProvisionEntry(entity=run.entity, employee=row.employee, date=on, kind=EosbProvisionEntry.Kind.REVERSAL,
                                 amount=-row.amount, run=run))
    _release_inputs(run)
    for loan in EmployeeLoan.objects.filter(entity=run.entity, status=EmployeeLoan.Status.SETTLED):
        if _loan_outstanding_of(loan) > 0:
            loan.status = EmployeeLoan.Status.ACTIVE
            _save(loan)
    for fs in stale:  # re-review 2: a calculated settlement netted against this run must be recalculated
        _clear_settlement_lines(fs)
        fs.status, fs.inputs_hash, fs.stored_hash = FSS.DRAFT, "", ""
        fs.warnings = list(fs.warnings) + [f"Run {run.number} was reversed: recalculate (re-review 2) / عُكس المسير "
                                           f"{run.number}: أعد الاحتساب"]
        fs.modified_by, fs.modified_at = user, _now()
        _save(fs)
    return run


def _release_inputs(run):
    """Inputs taken by a reversed run are free for a later run (review 9)."""
    for inp in PayrollInput._base_manager.select_for_update().filter(taken_run=run).order_by("pk"):
        inp.taken_run = None
        _save(inp)


# =====================================================================================================
# Loans (R26, R27, R46) and bai' ajel (R47 to R49)
# =====================================================================================================

def _loan_outstanding_of(loan):
    if loan.status not in (EmployeeLoan.Status.ACTIVE, EmployeeLoan.Status.SETTLED):
        return ZERO
    deducted = loan.installments.filter(status__in=(LoanInstallment.Status.DEDUCTED,
                                                    LoanInstallment.Status.SETTLED_IN_FINAL)) \
        .aggregate(t=Sum("deducted_amount"))["t"] or ZERO
    # Slice 2 (OQ-7 default): an approved write-off removes what it wrote off.
    written_off = loan.installments.filter(status=LoanInstallment.Status.WRITTEN_OFF).aggregate(t=Sum("amount"))["t"] \
        or ZERO
    return loan.principal - deducted - written_off


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
    # Review 17: the employee row is locked first, so two loans of the same person approved at once cannot both
    # pass the 3 x BASIC cap on the same outstanding balance.
    Employee._base_manager.select_for_update().filter(pk=loan.employee_id).first()
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


ASSET_ACCOUNT = "1250"


def _bill_cost(bill, entity):
    """The posted cost of the asset on a purchase bill: its debits on 1250 (E9), functional currency."""
    if bill.entity_id != entity.pk or bill.doc_type != "bill" or bill.status != "posted" or bill.journal_entry_id is None:
        return None
    return JournalLine.objects.filter(entry_id=bill.journal_entry_id, account__code=ASSET_ACCOUNT).aggregate(
        t=Sum("debit"))["t"] or ZERO


@transaction.atomic
def record_asset_acquisition(contract, *, user, purchase_bill, possession_date, possession_evidence_ref):
    """R47 step 3 (review 11): the company bought the asset in its own name (a posted bill of the same entity debiting
    1250 at exactly the contract cost, E9) and took possession (date and evidence). Only then may the sale be
    approved."""
    c = _lock(DeferredSaleContract, contract.pk)
    _require(user, c.entity, "payroll.create_deferred_sale")
    if c.status != DeferredSaleContract.Status.TERMS_APPROVED:
        raise error("The terms must be approved before the purchase is recorded (R47).",
                    "يجب اعتماد الشروط قبل تسجيل الشراء.", code="sale_status")
    problems = []
    cost = _bill_cost(purchase_bill, c.entity)
    if cost is None:
        problems.append("The purchase bill must be a posted bill of the same entity (R47, E9). / يجب أن تكون فاتورة "
                        "الشراء فاتورة مرحّلة للكيان نفسه.")
    elif cost != c.cost:
        problems.append(f"The bill debits {cost} to 1250; the contract cost is {c.cost} (R47). / تقيد الفاتورة {cost} على "
                        f"الحساب 1250 وتكلفة العقد {c.cost}.")
    if possession_date is None or not (possession_evidence_ref or "").strip():
        problems.append("Possession needs its date and the evidence reference (R47). / يحتاج القبض إلى تاريخه ومرجع "
                        "المستند.")
    elif purchase_bill is not None and possession_date < purchase_bill.accounting_date:
        problems.append("Possession cannot come before the purchase (R47). / لا يسبق القبض الشراء.")
    if purchase_bill is not None and DeferredSaleContract.objects.filter(purchase_bill=purchase_bill).exclude(
            pk=c.pk).exists():
        problems.append("This purchase bill already backs another bai' ajel contract: one asset is sold once (R47, "
                        "re-review 7). / فاتورة الشراء هذه مستخدمة لعقد بيع آجل آخر: الأصل يُباع مرة واحدة.")
    if problems:
        raise _problems(problems, "acquisition_invalid")
    c.purchase_bill, c.possession_date, c.possession_evidence_ref = purchase_bill, possession_date, possession_evidence_ref
    c.status, c.modified_by, c.modified_at = DeferredSaleContract.Status.ASSET_ACQUIRED, user, _now()
    try:
        with transaction.atomic():
            return _save(c)
    except IntegrityError:  # the same bill recorded for another contract at the same moment
        raise error("This purchase bill already backs another bai' ajel contract (R47, re-review 7).",
                    "فاتورة الشراء هذه مستخدمة لعقد بيع آجل آخر.", code="acquisition_invalid") from None


@transaction.atomic
def approve_deferred_sale(contract, *, user, sale_date=None):
    """R47 steps 4 and 5 (P19(k), P20(h)): the matrix approver approves the sale, only after the company bought the
    asset (posted bill, still posted) and took possession, on a sale date on or after possession (review 11); invoicing
    creates and posts the sale invoice at 15% VAT on the sale price, charged to the employee (invoicing R112, through
    ``invoicing_port``). The schedule then spreads sale price + VAT over the term exactly; installments are deducted
    with the signed consent within the 50% cap (no 10% loan cap, R49)."""
    c = DeferredSaleContract.objects.get(pk=contract.pk)
    grant = _require(user, c.entity, "payroll.approve_deferred_sale", document=c)
    approvals.check(grant, c.entity, "payroll.deferred_sale")
    c = _lock(DeferredSaleContract, c.pk)
    if c.status == DeferredSaleContract.Status.TERMS_APPROVED:
        raise error("The company must buy the asset and take possession before selling it (R47, review 11).",
                    "يجب أن تشتري الشركة الأصل وتقبضه قبل بيعه.", code="asset_not_acquired")
    if c.status == DeferredSaleContract.Status.SALE_INVOICED:
        return _reissue_rejected_sale(c, user)
    if c.status != DeferredSaleContract.Status.ASSET_ACQUIRED:
        raise error("The terms must be approved first (R47).", "يجب اعتماد الشروط أولًا.", code="sale_status")
    sale_date = sale_date or _today()
    if _bill_cost(c.purchase_bill, c.entity) != c.cost:
        raise error("The purchase bill is no longer posted at the contract cost (R47).",
                    "لم تعد فاتورة الشراء مرحّلة بتكلفة العقد.", code="asset_not_acquired")
    if c.possession_date is None or sale_date < c.possession_date:
        raise error("The sale date is on or after the possession date (R47, 3.11).",
                    "تاريخ البيع في تاريخ القبض أو بعده.", code="sale_before_possession")
    if not c.consent_ref.strip():
        raise error("The sale needs the employee's signed consent to salary deductions (R49).",
                    "يحتاج البيع إلى موافقة الموظف الموقعة على الاستقطاع من الراتب.", code="consent_required")
    c.sale_date = sale_date  # the port reads it (sale on or after possession, invoicing R112)
    port = invoicing_port.get_port()
    result = port.create_bai_ajel_sale(c, user=user)
    c.sale_document_ref, c.vat_amount = result.document_ref, _dec(result.vat_amount, "vat_amount")
    c.number = _next_number(c.entity, "BAJ", int(c.first_period[:4]))
    # Slice 2 (R47 as amended): sold once the invoice is posted on ZATCA reporting; until then installments wait.
    c.status = DeferredSaleContract.Status.SOLD if _sale_posted(port, c) else DeferredSaleContract.Status.SALE_INVOICED
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
# Final settlement (3.8; R28, R34, R49; E5; compliance C-A11, review 14)
# =====================================================================================================

FS = FinalSettlement
FSL = FinalSettlementLine.LineType
FSS = FinalSettlement.Status
SLIP_TO_FS = {
    LT.EARNING: FSL.SALARY_TO_LAST_DAY, LT.RETRO: FSL.RETRO, LT.MATERNITY_TOP_UP: FSL.OTHER_EARNING,
    LT.OVERTIME: FSL.OTHER_EARNING, LT.ONE_OFF_EARNING: FSL.OTHER_EARNING, LT.BONUS: FSL.OTHER_EARNING,
    LT.MATERNITY_SUPPORT: FSL.OTHER_EARNING, LT.UNPAID_DEDUCTION: FSL.SALARY_REDUCTION,
    LT.SICK_DEDUCTION: FSL.SALARY_REDUCTION, LT.MATERNITY_SUSPENDED: FSL.SALARY_REDUCTION,
    LT.DEDUCTION: FSL.OTHER_DEDUCTION, LT.EMPLOYEE_GOSI: FSL.EMPLOYEE_GOSI, LT.EMPLOYER_GOSI: FSL.EMPLOYER_GOSI,
    LT.GOSI_CATCH_UP: FSL.GOSI_RECOVERY, LT.GOSI_UNCOLLECTED: FSL.GOSI_UNCOLLECTED,
}


def _settlement_draft(settlement_input, user):
    """hr R38 hand-off (``payroll.hr_port``): the final settlement draft of a KSA employee's approved termination,
    inside hr's approval transaction (no right of its own: hr checked ``hr.approve_termination``). A recalculated
    EOSB (hr R40) points the open draft at the new input. Remuneration staff have no statutory settlement (H17): their
    last month is paid by the run."""
    si = settlement_input
    # Re-review 12: KSA employees only (remuneration staff are paid by the run, H17; local entity staff by their local
    # payroll, R41). Without payroll settings nobody could prepare it: the termination is refused until payroll is set.
    if not si.includes_statutory_lines or si.employee.employment_basis != Basis.KSA_EMPLOYEE:
        return None
    if PayrollSettings.objects.filter(entity=si.entity).first() is None:
        raise error("Payroll is not set up for this entity: set up payroll before approving the termination, so the "
                    "final settlement can be drafted (R34, re-review 12).",
                    "الرواتب غير مهيأة لهذا الكيان: هيّئ الرواتب قبل اعتماد إنهاء الخدمة لتُعد المخالصة النهائية.",
                    code="payroll_not_set_up")
    t = si.termination
    fs = FS._base_manager.select_for_update().filter(termination=t).first()
    if fs is not None and fs.status not in (FSS.DRAFT, FSS.CALCULATED, FSS.RETURNED):
        raise error("The final settlement is already submitted or approved (R40).",
                    "المخالصة النهائية مقدّمة أو معتمدة.", code="settlement_status")
    if fs is None:
        # The draft originates from the HR termination record: its drafter is the creator, so the termination's
        # approver (often the CFO, hr OQ-3) may still approve the settlement (access R35).
        fs = FS(entity=si.entity, employee=si.employee, termination=t, created_by=t.created_by)
    else:
        _clear_settlement_lines(fs)
        fs.modified_by, fs.modified_at = user, _now()
    fs.settlement_input, fs.eosb_calculation, fs.kind = si, si.eosb_calculation, t.kind
    fs.last_working_day, fs.period = si.last_working_day, _period_of(si.last_working_day)
    fs.worker_initiated, fs.due_date, fs.status = si.worker_initiated, si.due_by, FSS.DRAFT
    fs.inputs_hash = fs.stored_hash = ""
    return _save(fs)


def _discard_settlement(termination, user):
    """hr R41 hand-off: a withdrawn termination discards the open settlement."""
    fs = FS._base_manager.select_for_update().filter(termination=termination).first()
    if fs is None or fs.status == FSS.DISCARDED:
        return None
    if fs.status not in (FSS.DRAFT, FSS.CALCULATED, FSS.RETURNED, FSS.SUBMITTED):
        raise error("An approved final settlement is never discarded (R41).", "لا تُلغى المخالصة النهائية المعتمدة.",
                    code="settlement_status")
    fs.status, fs.modified_by, fs.modified_at = FSS.DISCARDED, user, _now()
    _save(fs)
    _clear_settlement_lines(fs)  # after the status: the PostgreSQL trigger keeps a submitted settlement's lines
    return fs


def _clear_settlement_lines(fs):
    for line in FinalSettlementLine.objects.filter(settlement=fs):
        line._service_delete = True
        line.delete()


def _settlement_approved(termination):
    """hr R40, R41: True once the final settlement of ``termination`` is approved (or paid)."""
    return FS.objects.filter(termination=termination, status__in=(FSS.APPROVED, FSS.PAID)).exists()


@dataclass
class SLine:
    line_type: str
    amount: Decimal
    account: object
    desc_en: str
    desc_ar: str
    component: object = None
    branch: str = ""
    quantity: Decimal = None
    rate: Decimal = None
    source_ref: str = ""
    origin_period: str = ""
    slip_type: str = ""  # re-review 1: the payslip line type of a line computed by _calc_ksa


@dataclass
class Settlement:
    lines: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    input_ids: list = field(default_factory=list)
    notices: list = field(default_factory=list)
    loan_effects: list = field(default_factory=list)  # [(installment pk, amount)]
    sale_effects: list = field(default_factory=list)
    sale_outstanding: list = field(default_factory=list)  # installment pks left after exit
    loan_outstanding: list = field(default_factory=list)  # re-review 8: loan installment pks left after exit
    eosb_award: Decimal = ZERO
    provision: Decimal = ZERO
    eosb_calc_id: int = None

    def total(self, kinds):
        return sum((l.amount for l in self.lines if l.line_type in kinds), ZERO)

    def warn(self, en, ar):
        text = f"{en} / {ar}"
        if text not in self.warnings:
            self.warnings.append(text)


def _settlement_ctx(fs, user, s):
    run = SimpleNamespace(entity=fs.entity, period=fs.period, kind="final_settlement", purpose="",
                          payroll_group=PayrollGroup.IN_COUNTRY, entry_date=fs.last_working_day)
    return Ctx(run, user, s)


def _latest_eosb(fs):
    from hr.models import EosbCalculation
    return (EosbCalculation.objects.filter(termination=fs.termination, superseded_by__isnull=True, voided=False)
            .order_by("-id").first())


def _settlement_input_now(fs):
    from hr.models import FinalSettlementInput
    return (FinalSettlementInput.objects.filter(termination=fs.termination, status=FinalSettlementInput.Status.DRAFT)
            .order_by("-id").first())


def _compute_settlement(fs, user, *, separate_amount, consent_ref):
    """C-A11 (R28, R34, R49, E5): every line of the final settlement, in memory (deterministic)."""
    s = _jurisdiction_gate(fs.entity, hr.ParamUse())
    ctx = _settlement_ctx(fs, user, s)
    emp = Employee.objects.get(pk=fs.employee_id)
    si = _settlement_input_now(fs)
    out = Settlement()
    if si is None:
        raise error("HR has no current settlement input for this termination.",
                    "لا توجد مدخلات مخالصة حالية من الموارد البشرية لهذا الإنهاء.", code="settlement_input")
    versions = _versions(emp)
    info = _employment(emp, fs.period, s, versions)
    # 1. Salary to the last working day, earlier months, GOSI of the leaving month on days of service (Regs Art.
    #    20(2)), every approved input not yet taken; net of what the runs paid (final mode of _calc_ksa).
    if info is not None:
        slip = _calc_ksa(ctx, emp, versions, info, mode="final")
        if slip.refusal:
            raise error(slip.refusal.split(" / ")[0], slip.refusal.split(" / ")[-1], code="settlement_refused")
        out.warnings.extend(slip.warnings)
        out.notes.extend(slip.notes)
        out.input_ids = list(slip.input_ids)
        out.notices = list(slip.notices)
        for l in slip.lines:
            if l.line_type not in SLIP_TO_FS:
                continue
            ltype = SLIP_TO_FS[l.line_type]
            if l.line_type == LT.RETRO and not l.origin_period:  # a release in the leaving month itself
                ltype = FSL.SALARY_TO_LAST_DAY
            out.lines.append(SLine(ltype, l.amount, l.account, l.desc_en, l.desc_ar, component=l.component,
                                   branch=l.branch, quantity=l.quantity, rate=l.rate, source_ref=l.source_ref,
                                   origin_period=l.origin_period, slip_type=l.line_type))
    salaries = ctx.account(SALARIES)
    # 2. Leave encashment (Art. 111: balance x actual daily wage / 30, from hr).
    if si.leave_encashment > 0:
        out.lines.append(SLine(FSL.LEAVE_ENCASHMENT, si.leave_encashment, salaries,
                               f"Leave encashment {si.leave_balance_days} days (Art. 111)",
                               f"بدل الإجازة {si.leave_balance_days} يوم (المادة 111)", quantity=si.leave_balance_days,
                               rate=si.daily_wage))
    # 3. Notice pay when the employer gave short notice (Art. 76); a worker's short notice is not deducted (Art. 92).
    if si.notice_shortfall_days:
        if not si.worker_initiated:
            # Re-review 9: wage x days / divisor, rounded once by hr (never the rounded daily wage x days).
            amount = si.notice_shortfall_wage
            out.lines.append(SLine(FSL.NOTICE_PAY, amount, salaries,
                                   f"Notice pay {si.notice_shortfall_days} days (Art. 76)",
                                   f"بدل الإشعار {si.notice_shortfall_days} يوم (المادة 76)",
                                   quantity=Decimal(si.notice_shortfall_days), rate=si.daily_wage))
        else:
            out.warn(f"The worker gave {si.notice_shortfall_days} days' short notice: not deducted without written "
                     "consent or a judgment (Art. 92).", f"قصّر العامل في الإشعار {si.notice_shortfall_days} يومًا: لا "
                     "يُستقطع دون موافقة كتابية أو حكم (المادة 92).")
    if si.art77_compensation > 0:
        out.lines.append(SLine(FSL.OTHER_EARNING, si.art77_compensation, salaries, "Compensation (Art. 77)",
                               "التعويض (المادة 77)"))
    # 4. EOSB: the hr snapshot award (R39, R40); charged to the employee's provision first (E5).
    eosb = _latest_eosb(fs)
    if eosb is not None and eosb.award > 0:
        out.eosb_award, out.eosb_calc_id = eosb.award, eosb.pk
        out.lines.append(SLine(FSL.EOSB, eosb.award, ctx.account(EOSB_PROVISION),
                               f"End-of-service award ({eosb.fraction_text} of {eosb.full_award})",
                               f"مكافأة نهاية الخدمة ({eosb.fraction_text} من {eosb.full_award})"))
    elif eosb is not None:
        out.eosb_calc_id = eosb.pk
    out.provision = EosbProvisionEntry.objects.filter(employee=emp).aggregate(t=Sum("amount"))["t"] or ZERO
    # 5. Separate settlement above the statutory award (mutual agreement, P19(i); Art. 8): never part of the EOSB.
    if separate_amount:
        if fs.kind != Termination.Kind.MUTUAL_AGREEMENT or not (consent_ref or "").strip():
            raise error("A separate settlement is for a mutual agreement, with the worker's written consent (R34, "
                        "P19(i)).", "التسوية المنفصلة لاتفاق الطرفين مع موافقة العامل الكتابية.", code="separate_settlement")
        out.lines.append(SLine(FSL.SEPARATE_SETTLEMENT, separate_amount, salaries,
                               f"Separate settlement (mutual agreement, consent {consent_ref})",
                               f"تسوية منفصلة (اتفاق الطرفين، الموافقة {consent_ref})", source_ref=consent_ref[:80]))
    # 6. Recoveries from the final dues, without the monthly 10% / 50% caps (P17, R28, R49).
    earnings = out.total(FinalSettlementLine.EARNINGS) - out.total(FinalSettlementLine.REDUCTIONS)
    fixed = out.total(FinalSettlementLine.DEDUCTIONS)
    overpaid = [l for l in out.lines if l.source_ref.startswith("overpaid:")]
    if earnings - fixed < 0 and overpaid:  # salary paid beyond the last day: recovered only up to the dues
        excess = fixed - earnings
        for l in overpaid:
            cut = min(l.amount, excess)
            l.amount -= cut
            excess -= cut
            out.warn(f"{cut} of the salary paid beyond the last working day is not covered by the final dues: recover "
                     "it separately.", f"{cut} من الأجر المدفوع بعد آخر يوم عمل لا تغطيه المستحقات النهائية: يُسترد "
                     "بشكل منفصل.")
        out.lines = [l for l in out.lines if l.amount > 0]
        fixed = out.total(FinalSettlementLine.DEDUCTIONS)
    room = earnings - fixed
    if room < 0:
        raise error(f"The deductions ({fixed}) exceed the final dues ({earnings}).",
                    f"الاستقطاعات ({fixed}) تتجاوز المستحقات النهائية ({earnings}).", code="settlement_negative")
    eosb_part = out.eosb_award
    allowed = bool(getattr(django_settings, "PAYROLL_EOSB_DEDUCTION_ALLOWED", True))
    room_non_eosb = max(ZERO, room - eosb_part)
    loan_room = room if allowed else room_non_eosb
    for loan in EmployeeLoan.objects.filter(employee=emp, status=EmployeeLoan.Status.ACTIVE).order_by("pk"):
        outstanding = _loan_outstanding_of(loan)
        take = min(outstanding, loan_room)
        if take > 0:
            out.lines.append(SLine(FSL.LOAN_RECOVERY, take, ctx.account(LOANS),
                                   f"{INTEREST_FREE[0]} {loan.number} recovered from the final dues",
                                   f"{INTEREST_FREE[1]} {loan.number} مسترد من المستحقات النهائية",
                                   source_ref=f"loan:{loan.pk}"))
            loan_room -= take
            room -= take
        # Re-review 8: only installments with a recovered part are settled (a partly recovered one is split in
        # _settlement_effects); the rest stays on 1230 as outstanding after exit (write-off: OQ-7).
        rest = take
        for inst in loan.installments.filter(status=LoanInstallment.Status.PLANNED).order_by("period", "pk"):
            part = min(inst.amount, rest)
            if part > 0:
                out.loan_effects.append((inst.pk, part))
            if part < inst.amount:
                out.loan_outstanding.append(inst.pk)
            rest -= part
        if outstanding > take:
            out.warn(f"Loan {loan.number}: {outstanding - take} not covered by the final dues"
                     f"{'' if allowed else ' other than the EOSB (PAYROLL_EOSB_DEDUCTION_ALLOWED off)'}; a write-off "
                     "needs a decision and account (OQ-7), refused until then.",
                     f"القرض {loan.number}: {outstanding - take} لا تغطيه المستحقات النهائية؛ يحتاج الشطب إلى قرار "
                     "وحساب ولا يُقبل حتى ذلك.")
    for c in DeferredSaleContract.objects.filter(employee=emp, status=DeferredSaleContract.Status.SOLD).order_by("pk"):
        planned = list(c.installments.filter(status=DeferredSaleInstallment.Status.PLANNED).order_by("period", "pk"))
        outstanding = sum((i.amount for i in planned), ZERO)
        sale_room = room if (allowed and c.final_dues_undertaking) else min(room, max(ZERO, room - eosb_part))
        take = min(outstanding, sale_room)
        rest = take
        for inst in planned:
            part = min(inst.amount, rest)
            if part > 0:
                out.sale_effects.append((inst.pk, part))
            if part < inst.amount:
                out.sale_outstanding.append(inst.pk)
            rest -= part
        if take > 0:
            out.lines.append(SLine(FSL.DEFERRED_SALE_RECOVERY, take, ctx.account(BAI_AJEL),
                                   f"Bai' ajel {c.number} balance recovered from the final dues (no increase)",
                                   f"رصيد البيع الآجل {c.number} مسترد من المستحقات النهائية (دون زيادة)",
                                   source_ref=f"sale:{c.pk}"))
            room -= take
        if outstanding > take:
            out.warn(f"Bai' ajel {c.number}: {outstanding - take} stays on 1240 as outstanding after exit"
                     f"{'' if c.final_dues_undertaking else ' (no final-dues undertaking: EOSB not used)'} (R49).",
                     f"البيع الآجل {c.number}: يبقى {outstanding - take} على الحساب 1240 قائمًا بعد انتهاء الخدمة.")
    return out


def _settlement_line_key(l):
    """One computed settlement line in the ``_settlement_fingerprint`` line format."""
    return [l.line_type, str(l.amount), l.account.code, l.component.pk if l.component else None, l.branch,
            str(l.quantity), str(l.rate), l.source_ref, l.origin_period, l.slip_type]


def _settlement_lines_hash(fs, out):
    """Verification 2: the stored lines (read from the database, in creation order) and the computed lines, each in
    the ``_settlement_fingerprint`` line format, hashed. Amounts are compared at their stored precision (2 decimals,
    quantity 2, rate 4), so the same lines give the same hash on SQLite and PostgreSQL. Returns (stored, computed)."""
    def norm(v, places):
        return None if v is None else str(Decimal(v).quantize(Decimal(1).scaleb(-places)))

    def key(t, amount, code, component, branch, quantity, rate, ref, origin, slip_type):
        return [t, norm(amount, 2), code, component, branch or "", norm(quantity, 2), norm(rate, 4), ref or "",
                origin or "", slip_type or ""]

    stored = [key(r.line_type, r.amount, r.account.code, r.component_id, r.gosi_branch, r.quantity, r.rate,
                  r.source_ref, r.origin_period, r.slip_line_type)
              for r in FinalSettlementLine._base_manager.filter(settlement=fs).select_related("account").order_by("pk")]
    computed = [key(l.line_type, l.amount, l.account.code, l.component.pk if l.component else None, l.branch,
                    l.quantity, l.rate, l.source_ref[:80], l.origin_period, l.slip_type) for l in out.lines]

    def h(rows):
        return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()
    return h(stored), h(computed)


def _settlement_fingerprint(fs, out):
    line = _settlement_line_key
    data = [fs.settlement_input_id, out.eosb_calc_id, str(out.eosb_award), str(out.provision), [line(l) for l in out.lines],
            sorted(out.loan_effects), sorted(out.sale_effects), sorted(out.sale_outstanding), sorted(out.input_ids),
            sorted(out.notices), sorted(out.loan_outstanding)]
    return hashlib.sha256(json.dumps(data, default=str).encode()).hexdigest()


# Re-review 3: the header fields the entry and the effects are built from are part of the stored hash.
SETTLEMENT_HEADER = ("entity_id", "employee_id", "termination_id", "settlement_input_id", "eosb_calculation_id", "kind",
                     "last_working_day", "period", "gross", "total_deductions", "net", "eosb_award",
                     "eosb_provision_balance", "separate_settlement_amount", "written_consent_ref", "input_ids",
                     "calc_version")


def _settlement_stored_hash(fs):
    """Review 6, re-review 3: every stored line (all columns) and the header fields, read from the database."""
    names = [f.attname for f in FinalSettlementLine._meta.concrete_fields]
    rows = [[str(r[n]) for n in names] for r in FinalSettlementLine._base_manager.filter(settlement=fs)
            .order_by("pk").values(*names)]
    header = FS._base_manager.filter(pk=fs.pk).values(*SETTLEMENT_HEADER).first() or {}
    data = [[k, str(header.get(k))] for k in SETTLEMENT_HEADER] + rows
    return hashlib.sha256(json.dumps(data, default=str).encode()).hexdigest()


def _settlement_header(fs, out):
    """Re-review 3: the header as the calculation gives it (compared at approval with the stored header)."""
    gross = out.total(FinalSettlementLine.EARNINGS) - out.total(FinalSettlementLine.REDUCTIONS)
    deductions = out.total(FinalSettlementLine.DEDUCTIONS)
    return {"gross": gross, "total_deductions": deductions, "net": gross - deductions, "eosb_award": out.eosb_award,
            "eosb_provision_balance": out.provision, "eosb_calculation_id": out.eosb_calc_id,
            "input_ids": sorted(out.input_ids)}


@transaction.atomic
def prepare_final_settlement(settlement, *, user, separate_settlement_amount=None, written_consent_ref=""):
    """C-A11 (R34, E5): calculate the final settlement (``payroll.prepare_final_settlement``): salary to the last
    working day net of what the runs paid, earlier months re-examined, leave encashment, EOSB award, notice pay, a
    separate settlement for a mutual agreement (with the written consent), GOSI of the leaving month on days of
    service, and loan and bai' ajel recovery from the final dues without the monthly caps (P17). Replaces the lines."""
    fs = _lock(FS, settlement.pk)
    _require(user, fs.entity, "payroll.prepare_final_settlement")
    if fs.status not in (FSS.DRAFT, FSS.CALCULATED, FSS.RETURNED):
        raise error(f"A {fs.status} final settlement is not recalculated.", f"لا يُعاد احتساب مخالصة بحالة {fs.status}.",
                    code="settlement_status")
    separate = _dec(separate_settlement_amount, "separate_settlement_amount") if separate_settlement_amount else ZERO
    if separate < 0 or not fx.has_places(separate, 2):  # re-review 10: SAR, at most 2 decimals (R5)
        raise error("The separate settlement is zero or more, in SAR with at most 2 decimals.",
                    "التسوية المنفصلة صفر أو أكثر بالريال وبخانتين عشريتين على الأكثر.", code="separate_settlement")
    out = _compute_settlement(fs, user, separate_amount=separate, consent_ref=written_consent_ref)
    _clear_settlement_lines(fs)
    for l in out.lines:
        _save(FinalSettlementLine(entity=fs.entity, settlement=fs, line_type=l.line_type, component=l.component,
                                  gosi_branch=l.branch, quantity=l.quantity, rate=l.rate, amount=l.amount,
                                  account=l.account, description_en=l.desc_en[:300], description_ar=l.desc_ar[:300],
                                  source_ref=l.source_ref[:80], origin_period=l.origin_period,
                                  slip_line_type=l.slip_type))
    for k, v in _settlement_header(fs, out).items():
        setattr(fs, k, v)
    fs.separate_settlement_amount, fs.written_consent_ref = separate, written_consent_ref or ""
    fs.settlement_input = _settlement_input_now(fs)
    fs.warnings, fs.notes = out.warnings, out.notes
    fs.status, fs.calc_version = FSS.CALCULATED, fs.calc_version + 1
    fs.inputs_hash = _settlement_fingerprint(fs, out)
    fs.calculated_by, fs.calculated_at, fs.modified_by, fs.modified_at = user, _now(), user, _now()
    _save(fs)
    fs.stored_hash = _settlement_stored_hash(fs)  # from the saved rows (re-review 3)
    return _save(fs)


@transaction.atomic
def submit_final_settlement(settlement, *, user):
    fs = _lock(FS, settlement.pk)
    _require(user, fs.entity, "payroll.prepare_final_settlement")
    if fs.status != FSS.CALCULATED:
        raise error("Only a calculated final settlement is submitted.", "تُقدّم المخالصة المحتسبة فقط.",
                    code="settlement_status")
    fs.status, fs.submitted_by, fs.submitted_at = FSS.SUBMITTED, user, _now()
    return _save(fs)


@transaction.atomic
def return_final_settlement(settlement, *, user, reason_ar):
    fs = _lock(FS, settlement.pk)
    grant = _require(user, fs.entity, "payroll.approve_final_settlement", document=fs)
    approvals.check(grant, fs.entity, "payroll.final_settlement")
    if fs.status != FSS.SUBMITTED or not (reason_ar or "").strip():
        raise error("Only a submitted final settlement is returned, with a reason in Arabic.",
                    "تُعاد المخالصة المقدّمة فقط مع سبب بالعربية.", code="settlement_status")
    fs.status, fs.returned_reason_ar = FSS.RETURNED, reason_ar
    return _save(fs)


@transaction.atomic
def approve_final_settlement(settlement, *, user):
    """C-A11 (R34, E5): the approver (``payroll.approve_final_settlement``, A: never its preparer or submitter)
    approves; the settlement is recomputed (refused "recalculate" if a source changed) and its stored lines checked;
    approval posts E5 through the registry: earnings Dr, EOSB Dr 2500 for the employee's provision balance with the
    shortfall Dr 5120 or the excess released Cr 5120, GOSI Cr 2340, recoveries Cr 1230 / 1240, net Cr 2330."""
    fs = FS.objects.select_related("entity").get(pk=settlement.pk)
    grant = _require(user, fs.entity, "payroll.approve_final_settlement", document=fs)
    approvals.check(grant, fs.entity, "payroll.final_settlement")
    if user.pk in {fs.calculated_by_id, fs.submitted_by_id}:
        access.deny(user, fs.entity, "payroll.approve_final_settlement", "self_approval")
    emp = Employee.objects.get(pk=fs.employee_id)
    if emp.user_id is not None and emp.user_id == user.pk:
        access.deny(user, fs.entity, "payroll.approve_final_settlement", "self_approval")
    _entity_lock(fs.entity)
    fs = _lock(FS, fs.pk)
    if fs.status != FSS.SUBMITTED:
        raise error("Only a submitted final settlement is approved.", "تُعتمد المخالصة المقدّمة فقط.",
                    code="settlement_status")
    out = _compute_settlement(fs, user, separate_amount=fs.separate_settlement_amount,
                              consent_ref=fs.written_consent_ref)
    if _settlement_fingerprint(fs, out) != fs.inputs_hash:
        raise error("A source changed after the calculation: recalculate the final settlement (R8).",
                    "تغيّر مصدر بعد الاحتساب: أعد احتساب المخالصة النهائية.", code="recalculate")
    stored_lines, computed_lines = _settlement_lines_hash(fs, out)
    if _settlement_stored_hash(fs) != fs.stored_hash or stored_lines != computed_lines or any(
            getattr(fs, k) != v for k, v in _settlement_header(fs, out).items()):
        # Review 6, re-review 3: the entry and the effects are built from the stored lines and header; verification
        # 2: the stored lines are also compared with the recomputed ones, so a raw edit with the stored hash recomputed
        # is refused too.
        access_services.log_denial(actor=user, entity=fs.entity, permission="payroll.approve_final_settlement",
                                   details={"reason": "stored_settlement_changed", "settlement": fs.pk})
        raise error("The stored lines or totals of this final settlement changed after the calculation: recalculate it.",
                    "تغيّرت بنود هذه المخالصة أو مجاميعها المخزنة بعد الاحتساب: أعد احتسابها.", code="payslips_changed")
    _settlement_months_check(fs, emp)
    on = _open_posting_date(fs.entity, fs.last_working_day)  # re-review 11: a closed month posts in the next open one
    fs.number = _next_number(fs.entity, "FS", fs.last_working_day.year)
    fs.status, fs.approved_by, fs.approved_at = FSS.APPROVED, user, _now()
    _save(fs)
    try:
        with _marker(_posting, fs):
            entry = ledger.create_entry(fs.entity, on,
                                        f"Final settlement {fs.number} / مخالصة نهائية {fs.number}", None, user=user,
                                        reference=fs.number, post=True, grant=grant, document=fs)
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "posting_refused") from None
    fs.journal_entry = entry
    _save(fs)
    _settlement_effects(fs, out, user)
    return fs


def _open_posting_date(entity, original):
    """Re-review 11: ``original`` when its fiscal period is open, else the first day of the next open period."""
    period = FiscalPeriod.objects.filter(entity=entity, start_date__lte=original, end_date__gte=original).first()
    if period is not None and period.status == FiscalPeriod.Status.OPEN:
        return original
    later = FiscalPeriod.objects.filter(entity=entity, status=FiscalPeriod.Status.OPEN, start_date__gt=original) \
        .order_by("start_date").first()
    if later is None:
        raise error("No open period to post the final settlement in.", "لا توجد فترة مفتوحة لترحيل المخالصة النهائية.",
                    code="no_open_period")
    return later.start_date


def _settlement_months_check(fs, emp):
    """Re-review 6, verification 1: the settlement closes the employee's payroll. The entity has an approved regular
    in-country run (its first one, for the months before leaving or the go-live month: until then nothing tells which
    months the runs will still pay); every month from the hire month (or the entity's first approved regular run, when
    payroll went live later: earlier months were paid outside the ERP) to the month before leaving has an approved
    regular run; no run up to the leaving month is submitted for an earlier month; and no run up to the leaving month
    that is not approved yet (draft, calculated, returned or submitted) includes the employee (a later approval would
    pay or accrue again what the settlement settles)."""
    regular = PayrollRun.objects.filter(entity=fs.entity, payroll_group=PayrollGroup.IN_COUNTRY,
                                        kind=PayrollRun.Kind.REGULAR)
    approved_periods = set(regular.filter(status__in=(RS.APPROVED, RS.PAID)).values_list("period", flat=True))
    hire_p = _period_of(emp.hire_date)
    problems = []
    if not approved_periods:
        problems.append("The entity has no approved regular payroll run yet: approve its first regular run (the months "
                        "before leaving, or the go-live month) before the final settlement (verification 1). / لا يوجد "
                        "للكيان مسير عادي معتمد بعد: اعتمد أول مسير عادي (الأشهر السابقة لترك العمل أو شهر بدء التشغيل) "
                        "قبل المخالصة النهائية.")
    else:
        q = max(hire_p, min(approved_periods))
        missing = []
        while q < fs.period:
            if q not in approved_periods:
                missing.append(q)
            q = _next_period(q)
        if missing:
            problems.append(f"No approved regular run for {', '.join(missing)}: approve it before the final settlement "
                            f"(re-review 6). / لا يوجد مسير عادي معتمد لفترة {', '.join(missing)}: اعتمده قبل المخالصة "
                            "النهائية.")
    up_to = PayrollRun.objects.filter(entity=fs.entity, period__lte=fs.period)
    waiting = up_to.filter(status=RS.SUBMITTED, period__lt=fs.period, period__gte=hire_p)
    with_emp = up_to.filter(status__in=(RS.DRAFT, RS.CALCULATED, RS.RETURNED, RS.SUBMITTED),
                            payslips__employee=emp, payslips__refused=False)
    periods = sorted(set(waiting.values_list("period", flat=True)) | set(with_emp.values_list("period", flat=True)))
    if periods:
        periods = ", ".join(periods)
        problems.append(f"A run for {periods} is not approved yet and includes the employee or an earlier month: approve it, "
                        f"or recalculate it (a recalculation leaves the leaver out of the leaving month), before the final "
                        f"settlement (re-review 6, verification 1). / مسير لفترة {periods} غير معتمد بعد ويشمل الموظف أو "
                        "شهرًا سابقًا: اعتمده أو أعد احتسابه قبل المخالصة النهائية.")
    if problems:
        raise _problems(problems, "settlement_runs_open")


def _settlement_effects(fs, out, user):
    """Installments settled in the final settlement (or outstanding after exit), loans settled, EOSB sub-ledger
    utilisation and release (E5), inputs taken, GOSI catch-up notices (R58)."""
    for pk, amount in out.loan_effects:
        inst = _lock(LoanInstallment, pk)
        if amount < inst.amount:  # re-review 8: the unrecovered part of a partly recovered installment stays after exit
            _save(LoanInstallment(entity=inst.entity, loan=inst.loan, schedule_version=inst.schedule_version,
                                  period=inst.period, amount=inst.amount - amount, carried_from=inst,
                                  status=LoanInstallment.Status.OUTSTANDING_AFTER_EXIT))
            inst.amount = amount
        inst.status, inst.deducted_amount = LoanInstallment.Status.SETTLED_IN_FINAL, amount
        _save(inst)
    settled_loans = {pk for pk, _ in out.loan_effects}
    for pk in out.loan_outstanding:
        if pk in settled_loans:
            continue
        inst = _lock(LoanInstallment, pk)
        inst.status = LoanInstallment.Status.OUTSTANDING_AFTER_EXIT
        _save(inst)
    for pk, amount in out.sale_effects:
        inst = _lock(DeferredSaleInstallment, pk)
        if amount < inst.amount:  # the unrecovered part stays as a planned remainder after exit
            _save(DeferredSaleInstallment(entity=inst.entity, contract=inst.contract, period=inst.period,
                                          amount=inst.amount - amount, carried_from=inst,
                                          status=DeferredSaleInstallment.Status.OUTSTANDING_AFTER_EXIT))
            inst.amount = amount
        inst.status, inst.deducted_amount = DeferredSaleInstallment.Status.SETTLED_IN_FINAL, amount
        _save(inst)
        if amount > 0:  # slice 2 (E13): recovered from the final dues; the last collection releases the rest
            _recognise_profit(inst.contract, amount, fs.journal_entry.date, _collection_ref(inst), user)
    taken = {pk for pk, _ in out.sale_effects}
    for pk in out.sale_outstanding:
        if pk in taken:
            continue
        inst = _lock(DeferredSaleInstallment, pk)
        inst.status = DeferredSaleInstallment.Status.OUTSTANDING_AFTER_EXIT
        _save(inst)
    for loan in EmployeeLoan.objects.filter(employee_id=fs.employee_id, status=EmployeeLoan.Status.ACTIVE):
        if _loan_outstanding_of(loan) == 0:
            loan.status = EmployeeLoan.Status.SETTLED
            _save(loan)
    award, balance = fs.eosb_award, max(fs.eosb_provision_balance, ZERO)
    on = fs.journal_entry.date  # verification 3: the entry date (the next open month when the leaving month closed)
    if award or balance:
        used = min(award, balance)
        if used:
            _save(EosbProvisionEntry(entity=fs.entity, employee_id=fs.employee_id, date=on,
                                     kind=EosbProvisionEntry.Kind.UTILISATION, amount=-used))
        if balance > award:
            _save(EosbProvisionEntry(entity=fs.entity, employee_id=fs.employee_id, date=on,
                                     kind=EosbProvisionEntry.Kind.RELEASE, amount=-(balance - award)))
    for inp in PayrollInput._base_manager.select_for_update().filter(pk__in=out.input_ids).order_by("pk"):
        if inp.taken_run_id is not None or inp.final_settlement_id is not None:
            raise error("An input of this settlement was taken meanwhile: recalculate.",
                        "أُخذ مدخل من هذه المخالصة في الأثناء: أعد الاحتساب.", code="recalculate")
        inp.final_settlement = fs
        _save(inp)
    for origin, amount, en, ar in out.notices:  # re-review 5: R58 notice for a share recovered from the final dues
        _save(GosiCatchUpNotice(entity=fs.entity, employee_id=fs.employee_id, final_settlement=fs,
                                period_of_origin=origin, amount=amount, text_en=en, text_ar=ar))


def _settlement_lines(fs):
    """Registered ``build_lines`` for FinalSettlement (§5 E5; R36: per account, no names or pay in descriptions)."""
    _require_posting(fs)
    label = f"{fs.number}"
    sums = {}

    def add(acc, amount):
        sums.setdefault(acc.pk, [acc, ZERO])[1] += amount

    for l in fs.lines.select_related("account"):
        t = l.line_type
        if t == FSL.EOSB:
            balance = max(fs.eosb_provision_balance, ZERO)
            add(_account(fs.entity, EOSB_PROVISION), balance)
            add(_account(fs.entity, EOSB_EXPENSE), l.amount - balance)  # shortfall Dr 5120, excess released Cr 5120
        elif t in FinalSettlementLine.EARNINGS:
            add(l.account, l.amount)
        elif t in FinalSettlementLine.REDUCTIONS or t in FinalSettlementLine.DEDUCTIONS:
            add(l.account, -l.amount)
        elif t in FinalSettlementLine.EMPLOYER:
            add(l.account, l.amount)
            add(_account(fs.entity, GOSI_PAYABLE), -l.amount)
    if fs.eosb_award == 0 and fs.eosb_provision_balance > 0:  # no award (e.g. Art. 80): the provision is released
        add(_account(fs.entity, EOSB_PROVISION), fs.eosb_provision_balance)
        add(_account(fs.entity, EOSB_EXPENSE), -fs.eosb_provision_balance)
    add(_account(fs.entity, SALARIES_PAYABLE), -fs.net)
    out = []
    for acc, amount in sums.values():
        if amount > 0:
            out.append({"account": acc, "debit": amount, "description": label})
        elif amount < 0:
            out.append({"account": acc, "credit": -amount, "description": label})
    return out


def _pending_settlements(entity, start_date, end_date):
    """Registered ``pending_documents``: submitted final settlements dated in the range block close_period."""
    return [f"final settlement {fs.employee.employee_code} (submitted)" for fs in
            FS.objects.filter(entity=entity, status=FSS.SUBMITTED, last_working_day__range=(start_date, end_date))
            .select_related("employee")]


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


def _settlement_credits_2340(entity, period):
    """Re-review 4: the lines of approved (or paid) final settlements credited to 2340: both shares, a share borne by
    the employer and the advanced (uncollected) employee share. Verification 3: they count under the month of the
    settlement's journal entry, i.e. the leaving month, or the next open month when the leaving month was closed, so
    the GOSI payable of a period ties out with what the ledger credited to 2340 in it."""
    start, end = _period_bounds(period)
    return FinalSettlementLine.objects.filter(entity=entity, settlement__status__in=SETTLED,
                                              settlement__journal_entry__date__gte=start,
                                              settlement__journal_entry__date__lte=end).filter(
        Q(line_type__in=(FSL.EMPLOYER_GOSI, FSL.GOSI_UNCOLLECTED))
        | Q(line_type__in=(FSL.EMPLOYEE_GOSI, FSL.GOSI_RECOVERY), account__code=GOSI_PAYABLE))


def _gosi_booked(entity, period, exclude_run=None):
    qs = _credits_2340(PayslipLine.objects.filter(entity=entity, payslip__run__period=period, payslip__refused=False,
                                                  payslip__run__status__in=(RS.APPROVED, RS.PAID)))
    if exclude_run is not None:
        qs = qs.exclude(payslip__run=exclude_run)
    runs = qs.aggregate(t=Sum("amount"))["t"] or ZERO
    return (runs + (_settlement_credits_2340(entity, period).aggregate(t=Sum("amount"))["t"] or ZERO)
            + _correction_credits_2340(entity, period) + _fines_2340(entity, period))


def _gosi_paid(entity, period):
    return PayrollPayment.objects.filter(entity=entity, kind=PayrollPayment.Kind.GOSI, gosi_period=period,
                                         status=PayrollPayment.Status.APPROVED).aggregate(t=Sum("amount"))["t"] or ZERO


def _gosi_open(entity, period):
    return _gosi_booked(entity, period) - _gosi_paid(entity, period)


def _bank_account(entity, account):
    """Review 17: a payment leaves from a postable asset account of the paying entity."""
    if account.entity_id != entity.pk or account.type != "asset" or not account.is_postable:
        raise error("The bank account is a postable asset account of the entity (R30).",
                    "الحساب البنكي حساب أصول قابل للترحيل من حسابات الكيان.", code="bank_account_invalid")
    return account


@transaction.atomic
def record_payment(entity, *, user, kind, payment_date, amount, run=None, gosi_period="", bank_account=None,
                   currency=None, wht_category=None, bank_reference="", final_settlement=None):
    """R29: the payroll officer records a payment after the bank release (``payroll.record_payment``). Net salaries
    of a run (per currency) or the GOSI contributions of a period. Remuneration staff in a foreign currency: WHT is
    considered on every payment (R43): a WHT category is required (rate from invoicing's global table). The entity's
    payroll lock and the run row are taken before the open amount is read (review 1)."""
    _require(user, entity, "payroll.record_payment")
    amount = _dec(amount, "amount")
    s = _settings_of(entity)
    _entity_lock(entity)
    if kind == PayrollPayment.Kind.NET_SALARIES:
        if run is None or run.entity_id != entity.pk:
            raise error("A salary payment names a run of the entity (R30).", "تحدد دفعة الرواتب مسيرًا للكيان.",
                        code="payment_run")
        run = _lock(PayrollRun, run.pk)
        currency = currency or entity.base_currency_id
    elif kind == PayrollPayment.Kind.GOSI:
        _check_period(gosi_period)
        currency = entity.base_currency_id
        run = None
    elif kind == PayrollPayment.Kind.FINAL_SETTLEMENT:
        if final_settlement is None or final_settlement.entity_id != entity.pk:
            raise error("A final settlement payment names a settlement of the entity (R30).",
                        "تحدد دفعة المخالصة مخالصة للكيان.", code="payment_run")
        final_settlement = _lock(FinalSettlement, final_settlement.pk)
        currency, run = entity.base_currency_id, None
    else:
        raise error("Unknown payment kind.", "نوع دفعة غير معروف.", code="payment_kind")
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
    _bank_account(entity, acc)
    payment = PayrollPayment(entity=entity, kind=kind, run=run, gosi_period=gosi_period, payment_date=payment_date,
                             bank_account=acc, currency=cur, amount=amount, wht_category=wht_category,
                             bank_reference=bank_reference, created_by=user,
                             final_settlement=final_settlement if kind == PayrollPayment.Kind.FINAL_SETTLEMENT else None)
    _payment_room(payment, run)
    return _save(payment)


def _payment_room(p, run):
    """R29: a payment never exceeds what is open. ``run`` is the locked run row (review 1)."""
    if p.kind == PayrollPayment.Kind.FINAL_SETTLEMENT:
        fs = p.final_settlement
        if fs.status not in (FSS.APPROVED, FSS.PAID):
            raise error("Only an approved final settlement is paid.", "تُدفع المخالصة المعتمدة فقط.",
                        code="settlement_status")
        paid = PayrollPayment.objects.filter(final_settlement=fs, status=PayrollPayment.Status.APPROVED) \
            .aggregate(t=Sum("amount"))["t"] or ZERO
        due = _settlement_due(fs)  # slice 2: with its approved corrections
        if p.amount > due - paid:
            raise error(f"The payment exceeds the open net {due - paid} of the final settlement (R29).",
                        f"الدفعة تتجاوز صافي المخالصة المتبقي {due - paid}.", code="overpayment")
    elif p.kind == PayrollPayment.Kind.NET_SALARIES:
        if run.status not in (RS.APPROVED, RS.PAID):
            raise error("Only an approved run is paid.", "يُدفع المسير المعتمد فقط.", code="run_status")
        total, paid = _run_payable(run, p.currency_id)
        if p.amount > total - paid:
            raise error(f"The payment exceeds the open net pay {total - paid} of the run (R29).",
                        f"الدفعة تتجاوز صافي الرواتب المتبقي {total - paid} للمسير.", code="overpayment")
    else:
        open_ = _gosi_open(p.entity, p.gosi_period)
        recon = (GosiReconciliation.objects.filter(entity=p.entity, period=p.gosi_period).order_by("-pk").first())
        # C-B7: up to the GOSI invoice recorded for the period (GOSI's figure governs what is paid); the part above
        # the ERP contributions is flagged on the payment for the reconciliation.
        allowed = max(open_, recon.gosi_invoice_amount - _gosi_paid(p.entity, p.gosi_period)) if recon else open_
        if p.amount > allowed:
            raise error(f"The payment exceeds the GOSI payable of {p.gosi_period} ({allowed}) (R29, C-B7).",
                        f"الدفعة تتجاوز اشتراكات التأمينات المستحقة لفترة {p.gosi_period} ({allowed}).",
                        code="overpayment")
        if p.amount > open_:
            p.warnings = [f"Pays {p.amount - max(open_, ZERO)} more than the ERP contributions of {p.gosi_period}, per the "
                          f"GOSI invoice {recon.gosi_invoice_amount}: difference to reconcile (C-B7) / تزيد الدفعة "
                          f"{p.amount - max(open_, ZERO)} على اشتراكات النظام وفق فاتورة التأمينات: فرق للمطابقة"]


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
    # Review 1: entity lock, then the run row, then the payment; the open amount is read under the locks, so two
    # approvals (or an approval and a run reversal) never both see the same open amount.
    _entity_lock(p.entity)
    run = _lock(PayrollRun, p.run_id) if p.run_id else None
    fs = _lock(FinalSettlement, p.final_settlement_id) if p.final_settlement_id else None
    p = _lock(PayrollPayment, p.pk)
    if fs is not None:
        p.final_settlement = fs
    if p.status != PayrollPayment.Status.SUBMITTED:
        raise error("Only a submitted payment is approved.", "تُعتمد الدفعة المقدمة فقط.", code="payment_status")
    if run is not None:
        p.run = run
    _payment_room(p, run)
    if p.wht_category_id:
        rate = p.wht_category.rate
        p.wht_amount = _round_to(Fraction(p.amount) * Fraction(rate) / 100, p.currency.minor_units)
        fx_rate = (Decimal("1") if p.currency_id == p.entity.base_currency_id else
                   ledger.get_rate(p.entity, p.currency_id, p.entity.base_currency_id, p.payment_date, user=user).rate)
        p.wht_amount_sar = fx.convert(p.wht_amount, fx_rate) if p.currency_id != p.entity.base_currency_id \
            else p.wht_amount
        # Slice 2 (R43, OQ-30): the base of the WHT return row, in SAR at the same rate.
        p.wht_base_sar = fx.convert(p.amount, fx_rate) if p.currency_id != p.entity.base_currency_id else p.amount
    p.status, p.approved_by, p.approved_at = PayrollPayment.Status.APPROVED, user, _now()
    _save(p)
    try:
        with _marker(_posting, p):
            entry = ledger.create_entry(p.entity, p.payment_date, f"Payroll payment {p.get_kind_display()} / دفعة رواتب",
                                        None, user=user, reference=(p.run.number if p.run else p.final_settlement.number
                                                                    if p.final_settlement_id else p.gosi_period),
                                        post=True, grant=grant, document=p)
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "posting_refused") from None
    p.journal_entry = entry
    _save(p)
    if fs is not None:
        paid = PayrollPayment.objects.filter(final_settlement=fs, status=PayrollPayment.Status.APPROVED) \
            .aggregate(t=Sum("amount"))["t"] or ZERO
        if paid >= _settlement_due(fs) and fs.status == FSS.APPROVED:
            fs.status = FSS.PAID
            _save(fs)
    if run is not None:
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
    label = (f"{p.run.number} payment" if p.run else f"{p.final_settlement.number} payment" if p.final_settlement_id
             else f"GOSI {p.gosi_period} payment")
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


def _split(total, weights):
    """Split ``total`` (2 dp) over ``weights`` {key: Decimal}, rounded so the parts add up exactly."""
    whole = sum(weights.values(), ZERO)
    if not whole:
        return {k: ZERO for k in weights}
    parts = {k: Fraction(total) * Fraction(v) / Fraction(whole) for k, v in weights.items()}
    return _allocate(parts)


def _wht_return_source(entity, start, end):
    """OQ-30 (R43; registered with ``invoicing.register_wht_return_source``): the remuneration WHT of payroll payments
    for invoicing's monthly WHT return, in its row shape: one row per WHT category, payee (employee code only, R36)
    and rate. A payment of a run is split over the run's remuneration payslips in its currency by their net pay (the
    parts add up to the payment's base and WHT exactly). A payment reversed in the month shows as a negative row."""
    rows = {}

    def add(p, sign):
        slips = {ps.employee: ps.net_txn for ps in Payslip.objects.filter(run=p.run, refused=False,
                                                                          currency_id=p.currency_id,
                                                                          employment_basis=Basis.REMUNERATION)
                 .select_related("employee")}
        base, wht = _split(p.wht_base_sar, slips), _split(p.wht_amount_sar, slips)
        for emp in slips:
            key = (p.wht_category.code, emp.employee_code, p.wht_category.rate)
            r = rows.setdefault(key, {
                "category": p.wht_category.code, "return_line_code": p.wht_category.return_line_code,
                "vendor": emp.employee_code, "country": emp.country_of_residence or "", "rate": p.wht_category.rate,
                "treaty_certificate": None, "treaty_country": "", "base_sar": ZERO, "wht_sar": ZERO, "gross_up": False,
                "source": "payroll", "payee_kind": "employee"})
            r["base_sar"] += sign * base[emp]
            r["wht_sar"] += sign * wht[emp]

    qs = PayrollPayment.objects.filter(entity=entity, wht_category__isnull=False, run__isnull=False) \
        .select_related("wht_category", "run")
    for p in qs.filter(status__in=(PayrollPayment.Status.APPROVED, PayrollPayment.Status.REVERSED),
                       payment_date__range=(start, end)):
        add(p, 1)
    for p in qs.filter(status=PayrollPayment.Status.REVERSED, reversal_entry__date__range=(start, end)):
        add(p, -1)
    return list(rows.values())


def wht_return_rows(entity, month, *, user):
    """R43: payroll's own list of the remuneration WHT of approved payments in ``month`` (one row per payment;
    ``payroll.view_cost_reports``). The rows that go into invoicing's monthly WHT return come from
    :func:`_wht_return_source` (slice 2, OQ-30: wired into ``invoicing.wht_return_data``)."""
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


def _iban_hash(iban):
    """Review 8: keyed hash of the normalised IBAN (never stored in clear on the payslip)."""
    text = "".join((iban or "").split()).upper()
    return hr.crypto.keyed_hash(text) if text else ""


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """Review 7: a CSV cell a spreadsheet would read as a formula is prefixed with an apostrophe; re-review 14: also
    when the formula character follows leading whitespace (spreadsheets ignore it)."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) or text.lstrip().startswith(FORMULA_START) else text


def _wps_amounts(ps):
    """C-C1, C-C2: MOL-BAS = this period's BASIC net of its reductions, MOL-HALI = cash HOUSING net of its reductions,
    MOL-OEA = every other earning net of reductions (retro, overtime, one-offs, released pay); none negative; their sum
    is the gross. Deductions = the payslip's total deductions."""
    basic = housing = ZERO
    for l in ps.lines.filter(line_type__in=(LT.EARNING, *PayslipLine.REDUCTIONS), origin_period="",
                             component__code__in=("BASIC", "HOUSING")).select_related("component"):
        sign = 1 if l.line_type == LT.EARNING else -1
        if l.component.code == "BASIC":
            basic += sign * l.amount
        else:
            housing += sign * l.amount
    basic, housing = max(basic, ZERO), max(housing, ZERO)
    other = ps.gross - basic - housing
    if other < 0:  # reductions of other components above their earnings: taken from housing, then basic
        cut = min(housing, -other)
        housing, other = housing - cut, other + cut
        cut = min(basic, -other)
        basic, other = basic - cut, other + cut
    return basic, housing, max(other, ZERO)


def _wps_rows(run, bank_codes=None):
    """Employees in the salary file: KSA employees only (R51; remuneration staff are outside WPS). Exceptions (listed,
    not in the file, R31): no valid IBAN; IBAN changed since the payslip was calculated, or a termination approved
    after the run was approved (review 8: check before paying); the bank of the IBAN unknown in ``wps.bank_codes``
    (C-C3)."""
    rows, exceptions = [], []
    for ps in Payslip.objects.filter(run=run, refused=False).select_related("employee").order_by("employee__employee_code"):
        emp = Employee.objects.get(pk=ps.employee_id)  # unmasked, inside the service only; logged below
        if not emp.wps_included:
            continue
        reason = None
        if not emp.iban or not hr._iban_ok(emp.iban):
            reason = "No valid IBAN / لا يوجد آيبان صالح"
        elif ps.iban_hash and _iban_hash(emp.iban) != ps.iban_hash:
            reason = ("IBAN changed since the payslip was calculated: confirm it before paying (review 8) / تغيّر "
                      "الآيبان بعد احتساب القسيمة: تحقق منه قبل الصرف")
        elif (getattr(_termination(emp), "pk", None) or 0) != (ps.termination_ref or 0):
            reason = ("Termination approved after the payslip was calculated: check before paying (review 8) / اعتُمد "
                      "إنهاء الخدمة بعد احتساب القسيمة: تحقق قبل الصرف")
        bic = ""
        if reason is None and bank_codes is not None:
            entry = bank_codes.get("".join(emp.iban.split()).upper()[4:6])
            if entry is None:
                reason = ("The bank of this IBAN has no code in wps.bank_codes (C-C3) / لا يوجد رمز لبنك هذا الآيبان في "
                          "wps.bank_codes")
            else:
                bic = entry["bic"]
        if reason is not None:
            exceptions.append({"employee_code": emp.employee_code, "reason": reason})
            continue
        basic, housing, other = _wps_amounts(ps)
        rows.append({"employee_code": emp.employee_code, "name": emp.full_name_en, "id": emp.id_number,
                     "iban": emp.iban, "bank": emp.bank_name, "bic": bic, "net": ps.net, "basic": basic,
                     "housing": housing, "other": other, "deductions": ps.total_deductions})
    return rows, exceptions


def _wps_csv(run, fmt, rows, s, use, bank_codes=None):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")

    def write(cells):
        w.writerow([_cell(c) for c in cells])

    total = sum((r["net"] for r in rows), ZERO)
    if fmt == "generic_csv":
        write(["employee_code", "name", "id_number", "iban", "bank", "basic", "housing", "other_earnings",
               "deductions", "net"])
        for r in rows:
            write([r["employee_code"], r["name"], r["id"], r["iban"], r["bank"], r["basic"], r["housing"], r["other"],
                   r["deductions"], r["net"]])
        return out.getvalue(), None
    fields = use.get("SA", "wps.fields.mol", run.entry_date)
    hs = HrSettings.objects.filter(entity=run.entity).first()
    dest = next((v["bic"] for v in (bank_codes or {}).values() if v.get("salary_bank") == s.salary_bank), "")
    header = {"DEST-ID": dest, "ESTB-ID": s.wps_bank_code, "BANK-ACC": s.employer_bank_iban,
              "32A-CCY": run.entity.base_currency_id, "32A-VAL": run.pay_date.isoformat(), "32A-AMT": str(total),
              "D-CATE": "", "FILE-REF": run.number, "FILE-REJCDE": "", "MOL-ESTBID": hs.mudad_establishment_no if hs else ""}
    # C-C1: the total deductions, under the field name the bank uses (settings; [CC-verify] per bank, OQ-10).
    content_fields = list(fields["content"]) + [s.wps_deductions_field or "DEDUCTIONS"]
    write(fields["header"])
    write([header.get(f, "") for f in fields["header"]])
    write(content_fields)
    for r in rows:
        content = {"32B-AMT": str(r["net"]), "ACC-59": r["iban"], "NAM-57": r["name"], "BANK-57": r["bic"],
                   "DET-70": "", "MOL-BAS": str(r["basic"]), "MOL-HALI": str(r["housing"]),
                   "MOL-OEA": str(r["other"]), "MOL-ALL": str(r["basic"] + r["housing"] + r["other"]), "MOL-ID": r["id"],
                   s.wps_deductions_field or "DEDUCTIONS": str(r["deductions"])}
        write([content.get(f, "") for f in content_fields])
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
    bank_codes = None
    if fmt == "mudad_placeholder":
        if not s.salary_bank:
            raise error("Set the salary bank first: the file names it in DEST-ID (R51, C-C3).",
                        "حدّد بنك الرواتب أولًا: يُذكر في الحقل DEST-ID.", code="salary_bank_missing")
        bank_codes = use.get("SA", "wps.bank_codes", run.entry_date)["by_iban_code"]
    rows, exceptions = _wps_rows(run, bank_codes)
    content, _ = _wps_csv(run, fmt, rows, s, use, bank_codes)
    f = _save(WpsFile(entity=run.entity, run=run, format_code=fmt, format_parameter=param_row, generated_by=user,
                      generated_at=_now(),
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
    if f.bank_processed_sha256:
        raise error("The bank-processed file of this WPS file is already attached; it is kept as received (review 15).",
                    "أُرفق ملف البنك المعالج لهذا الملف مسبقًا؛ ويُحفظ كما ورد.", code="bank_file_attached")
    limit = int(getattr(django_settings, "PAYROLL_BANK_FILE_MAX_BYTES", 5_000_000))
    if len(content.encode("utf-8")) > limit:
        raise error(f"The bank-processed file is larger than {limit} bytes (review 15).",
                    f"ملف البنك المعالج أكبر من {limit} بايت.", code="file_too_large")
    f.bank_processed_content = content
    f.bank_processed_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    f.bank_processed_received_at = _now()
    # Only these columns: the stored salary file is never rewritten (review 15; PostgreSQL trigger).
    return _save(f, update_fields=["bank_processed_content", "bank_processed_sha256", "bank_processed_received_at"])


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
                                 status=status, message_ar=message_ar, message_en=message_en, recorded_by=user,
                                 bank_processed_sha256=f.bank_processed_sha256))


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
        # C-A13: paid days, the pay date and the GOSI wage are on the payslip.
        "paid_days": ps.paid_days, "pay_date": ps.run.pay_date, "gosi_wage": ps.gosi_wage,
        "name_en": emp.full_name_en, "name_ar": emp.full_name_ar, "iban": _mask_iban(emp.iban),
        "currency": ps.currency_id, "earnings": [l for l in lines if l.line_type in PayslipLine.EARNINGS],
        "reductions": [l for l in lines if l.line_type in PayslipLine.REDUCTIONS],
        "deductions": [l for l in lines if l.line_type in PayslipLine.DEDUCTIONS],
        "employer": [l for l in lines if l.line_type in PayslipLine.EMPLOYER],
        "gross": ps.gross_txn, "deductions_total": ps.total_deductions, "net": ps.net_txn, "net_sar": ps.net,
        "loan_balance": ps.loan_balance, "leave_balance": ps.leave_balance_snapshot,
        "interest_free": any(l.line_type == LT.LOAN_INSTALLMENT for l in lines), "notes": ps.notes,
        "notices": list(GosiCatchUpNotice.objects.filter(run=ps.run, employee_id=ps.employee_id)),
        "wht": _payslip_wht(ps), "payslip": ps, "jurisdiction": _jurisdiction_of(ps.entity),
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
    """GOSI report of a period (approved runs): totals per branch and side, the due date (15th of the following
    month, Regs Art. 24(1)) and the late-fine rule (2% per month or part; waivers, Regs Art. 25). Review 13 (D1): the
    per-employee rows (employee code, GOSI wage and shares) are individual pay: they are included only for a holder of
    ``payroll.view_payslip`` in the entity, and that view is logged; ``payroll.view_cost_reports`` alone gets the
    aggregates."""
    _require(user, entity, "payroll.view_cost_reports")
    detail = access.has(user, entity, "payroll.view_payslip")
    rows, branches = {}, {}
    for l in _credits_2340(PayslipLine.objects.filter(entity=entity, payslip__run__period=period, payslip__refused=False,
                                                      payslip__run__status__in=(RS.APPROVED, RS.PAID))) \
            .select_related("payslip__employee"):
        side = "employer" if l.line_type == LT.EMPLOYER_GOSI and not l.source_ref.startswith("borne:") else "employee"
        b_ = branches.setdefault(l.gosi_branch or "", {"branch": l.gosi_branch or "", "employee": ZERO, "employer": ZERO})
        b_[side] += l.amount
        if detail:
            r = rows.setdefault(l.payslip.employee.employee_code, {"employee_code": l.payslip.employee.employee_code,
                                                                   "gosi_wage": l.payslip.gosi_wage, "employee": ZERO,
                                                                   "employer": ZERO})
            r[side] += l.amount
    for l in _settlement_credits_2340(entity, period).select_related("settlement__employee"):  # re-review 4
        side = "employer" if l.line_type == FSL.EMPLOYER_GOSI and not l.source_ref.startswith("borne:") else "employee"
        b_ = branches.setdefault(l.gosi_branch or "", {"branch": l.gosi_branch or "", "employee": ZERO, "employer": ZERO})
        b_[side] += l.amount
        if detail:
            code = l.settlement.employee.employee_code
            r = rows.setdefault(code, {"employee_code": code, "gosi_wage": ZERO, "employee": ZERO, "employer": ZERO})
            if not l.origin_period and l.quantity is not None and l.line_type != FSL.GOSI_UNCOLLECTED:
                r["gosi_wage"] = max(r["gosi_wage"], l.quantity)
            r[side] += l.amount
    start, end = _period_bounds(period)
    for l in SCL.objects.filter(entity=entity, correction__status=SCS.APPROVED,  # slice 2: settlement corrections
                                correction__journal_entry__date__range=(start, end),
                                line_type__in=("employee_gosi", "employer_gosi")) \
            .select_related("correction__employee", "account"):
        if l.line_type == "employee_gosi" and l.account.code != GOSI_PAYABLE:
            continue
        side = "employer" if l.line_type == "employer_gosi" else "employee"
        b_ = branches.setdefault(l.gosi_branch or "", {"branch": l.gosi_branch or "", "employee": ZERO, "employer": ZERO})
        b_[side] += l.sign * l.amount
        if detail:
            code = l.correction.employee.employee_code
            r = rows.setdefault(code, {"employee_code": code, "gosi_wage": ZERO, "employee": ZERO, "employer": ZERO})
            r[side] += l.sign * l.amount
    if detail:
        access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=entity,
                            permission="payroll.view_payslip",
                            details={"action": "gosi_report", "period": period, "employee_codes": sorted(rows)})
    due_day = int(hr.param("SA", "gosi.payment_due_day", _period_bounds(period)[1]).value["new_m273"])
    ns = _period_bounds(_next_period(period))[0]
    fine = hr.param("SA", "gosi.late_fine_pct", _period_bounds(period)[1]).value
    total = sum((b_["employee"] + b_["employer"] for b_ in branches.values()), ZERO)
    fines = _fines_2340(entity, period)
    return {"period": period, "rows": sorted(rows.values(), key=lambda r: r["employee_code"]),
            "branches": sorted(branches.values(), key=lambda b_: b_["branch"]), "total": total,
            "fines": fines, "total_due": total + fines,
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
    erp = _gosi_booked(entity, period)
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

MUDAD_ALERT_DAYS = (7, 10, 15)


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
        if _jurisdiction_of(e) != "SA":  # WPS, Mudad and GOSI are Saudi (Egypt research §2.4, §2.5)
            continue
        sent += len(_refresh_deferred_sales(e))  # slice 2: bai' ajel sales posted on ZATCA reporting (R47)
        prev = _period_of(today.replace(day=1) - timedelta(days=1))
        # C-C5: Mudad counts from the pay date: alerts 7, 10 and 15 days after it while no submission is recorded.
        for offset in MUDAD_ALERT_DAYS:
            for run in PayrollRun.objects.filter(entity=e, pay_date=today - timedelta(days=offset),
                                                 payroll_group=PayrollGroup.IN_COUNTRY,
                                                 status__in=(RS.APPROVED, RS.PAID)):
                if not MudadSubmission.objects.filter(run=run).exists():
                    _send_alerts(e, [(PayrollAlert.Kind.MUDAD_DEADLINE, f"mudad:{run.pk}:{offset}",
                                      f"Upload the bank-processed salary file of {run.period} to Mudad: {offset} days since "
                                      f"the pay date {run.pay_date} (R53, C-C5).",
                                      f"ارفع ملف الرواتب المعالج من البنك لفترة {run.period} على مدد: مضى {offset} أيام على "
                                      f"تاريخ الصرف {run.pay_date}.", roles)])
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
        # C-A10: an approved annual leave starting within 14 days before its period's pay date, not yet advanced.
        for req in LeaveRequest.objects.filter(entity=e, leave_type__code="ANNUAL", status=LeaveRequest.Status.APPROVED,
                                               employee__employment_basis=Basis.KSA_EMPLOYEE,
                                               start_date__gt=today, start_date__lte=today + timedelta(days=14)) \
                .select_related("employee"):
            s_ = PayrollSettings.objects.filter(entity=e).first()
            due = [p for p in _leave_advance_periods(req, s_)
                   if not PayrollRun.objects.filter(leave_request=req, period=p).exclude(status=RS.REVERSED).exists()]
            if due:
                _send_alerts(e, [(PayrollAlert.Kind.LEAVE_ADVANCE, f"leave_advance:{req.pk}",
                                  f"Annual leave of {req.employee.employee_code} starts {req.start_date}: pay the leave wage "
                                  "in advance (Labour Law Art. 109; prepare_leave_advance).",
                                  f"تبدأ الإجازة السنوية لـ {req.employee.employee_code} في {req.start_date}: اصرف أجر "
                                  "الإجازة مقدمًا (المادة 109).", ("payroll_officer",))])
                sent += 1
        # C-A11 (Art. 88): final settlements due within 7 days, or 14 when the worker ended the contract; alerts 3
        # days before, on the due day and the day after while unpaid.
        for fs in FS.objects.filter(entity=e, due_date__isnull=False).exclude(status__in=(FSS.PAID, FSS.DISCARDED)) \
                .select_related("employee"):
            when = {fs.due_date - timedelta(days=3): "before", fs.due_date: "due", fs.due_date + timedelta(days=1): "late"}
            if today in when:
                _send_alerts(e, [(PayrollAlert.Kind.FINAL_SETTLEMENT_DUE, f"fs:{fs.pk}:{when[today]}",
                                  f"Final settlement of {fs.employee.employee_code} is due by {fs.due_date} (Labour Law "
                                  f"Art. 88){' and is late' if when[today] == 'late' else ''}.",
                                  f"المخالصة النهائية لـ {fs.employee.employee_code} مستحقة قبل {fs.due_date} (المادة 88)"
                                  f"{' وقد تأخرت' if when[today] == 'late' else ''}.", ("payroll_officer", "approver"))])
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
            _save(f, update_fields=["nominated_for_burn_at"])
            sent += 1
    return sent


# =====================================================================================================
# Slice 2 (§10.8): final settlement corrections and the settlement statement (item 1)
# =====================================================================================================

SC = SettlementCorrection
SCS = SettlementCorrection.Status
SCL = SettlementCorrectionLine
CORRECTION_ACCOUNTS = {"eosb": EOSB_EXPENSE, "employee_gosi": GOSI_PAYABLE, "employer_gosi": GOSI_EXPENSE}
CORRECTION_TEXT = {
    "salary_to_last_day": ("Salary to the last working day", "الأجر حتى آخر يوم عمل"),
    "retro": ("Earlier months (retro)", "أشهر سابقة (بأثر رجعي)"),
    "leave_encashment": ("Leave encashment (Art. 111)", "بدل الإجازة (المادة 111)"),
    "eosb": ("End-of-service award", "مكافأة نهاية الخدمة"),
    "notice_pay": ("Notice pay (Art. 76)", "بدل الإشعار (المادة 76)"),
    "separate_settlement": ("Separate settlement (mutual agreement)", "تسوية منفصلة (اتفاق الطرفين)"),
    "other_earning": ("Other earning", "استحقاق آخر"),
    "other_deduction": ("Other deduction", "استقطاع آخر"),
    "employee_gosi": ("Employee GOSI", "حصة الموظف في التأمينات"),
    "employer_gosi": ("Employer GOSI", "حصة صاحب العمل في التأمينات"),
}


def _corrections_net(fs):
    """The approved corrections' change of the net due to the employee (positive: more due)."""
    return SC.objects.filter(settlement=fs, status=SCS.APPROVED).aggregate(t=Sum("net_change"))["t"] or ZERO


def _settlement_due(fs):
    """What the employee is owed by the settlement with its approved corrections."""
    return fs.net + _corrections_net(fs)


def _booked_by_type(fs, exclude=None):
    """Amount booked per (line type, GOSI branch) by the settlement and its approved corrections; a decrease may
    never take a line type below zero."""
    out = {}
    for l in FinalSettlementLine.objects.filter(settlement=fs):
        key = (l.line_type, l.gosi_branch if l.line_type in ("employee_gosi", "employer_gosi") else "")
        out[key] = out.get(key, ZERO) + l.amount
    qs = SCL.objects.filter(correction__settlement=fs, correction__status=SCS.APPROVED)
    if exclude is not None:
        qs = qs.exclude(correction=exclude)
    for l in qs:
        key = (l.line_type, l.gosi_branch)
        out[key] = out.get(key, ZERO) + l.sign * l.amount
    return out


def _correction_account(entity, line_type, component):
    if component is not None and line_type in SCL.EARNINGS and line_type != "eosb":
        return component.expense_account
    return _account(entity, CORRECTION_ACCOUNTS.get(line_type, SALARIES))


def _correction_problems(c, rows, fs):
    """Checks of a correction's difference lines (creation and again at submission and approval)."""
    problems = []
    if not (c.reason_ar or "").strip():
        problems.append("The reason in Arabic is required. / السبب بالعربية مطلوب.")
    if not rows:
        problems.append("A correction has at least one difference line. / يتضمن التصحيح بند فرق واحدًا على الأقل.")
    booked = _booked_by_type(fs, exclude=c if c.pk else None)
    delta = {}
    for r in rows:
        key = (r.line_type, r.gosi_branch)
        delta[key] = delta.get(key, ZERO) + r.sign * r.amount
        if r.account.entity_id != c.entity_id or not r.account.is_postable:
            problems.append(f"{r.line_type}: the account is a postable account of the entity. / الحساب حساب قابل "
                            "للترحيل من حسابات الكيان.")
        elif r.account.sharia_prohibited and c.entity.sharia_enforced:
            problems.append(f"{r.line_type}: account {r.account.code} is Sharia-prohibited in {c.entity.code} (R19). / "
                            f"الحساب {r.account.code} محظور شرعًا.")
        if r.line_type == "separate_settlement" and r.sign > 0 and (
                fs.kind != Termination.Kind.MUTUAL_AGREEMENT or not (c.written_consent_ref or "").strip()):
            problems.append("A separate settlement is for a mutual agreement, with the worker's written consent (R34, "
                            "P19(i)). / التسوية المنفصلة لاتفاق الطرفين مع موافقة العامل الكتابية.")
    for key, change in delta.items():
        if booked.get(key, ZERO) + change < 0:
            en, ar = CORRECTION_TEXT[key[0]]
            problems.append(f"{en}{' ' + key[1] if key[1] else ''}: the decrease {-change} is more than the settlement "
                            f"booked ({booked.get(key, ZERO)}). / {ar}: النقص {-change} يتجاوز ما قيدته المخالصة "
                            f"({booked.get(key, ZERO)}).")
    return problems


def _correction_net(rows):
    net = ZERO
    for r in rows:
        if r.line_type in SCL.EARNINGS:
            net += r.sign * r.amount
        elif r.line_type in SCL.DEDUCTIONS:
            net -= r.sign * r.amount
    return net


def _correction_warnings(c, rows, fs):
    out = []
    paid = PayrollPayment.objects.filter(final_settlement=fs, status=PayrollPayment.Status.APPROVED) \
        .aggregate(t=Sum("amount"))["t"] or ZERO
    due = _settlement_due(fs) + _correction_net(rows)
    if due < paid:
        out.append(f"After this correction the employee was paid {paid - due} more than is due: recover it only with "
                   f"the worker's written consent or a judgment (Labour Law Art. 92), outside payroll / بعد هذا التصحيح "
                   f"دُفع للموظف {paid - due} أكثر من المستحق: لا يُسترد إلا بموافقته الكتابية أو بحكم (المادة 92)")
    if any(r.line_type in ("employee_gosi", "employer_gosi") for r in rows):
        out.append("GOSI shares change: update the GOSI record and the GOSI reconciliation of the month (R23) / تتغير "
                   "حصص التأمينات: حدّث سجل التأمينات ومطابقة الشهر")
    if any(r.line_type == "eosb" for r in rows):
        out.append("The EOSB award changes without an hr recalculation (hr R40): keep the calculation note with the "
                   "correction / تتغير مكافأة نهاية الخدمة دون إعادة احتساب في الموارد البشرية: احتفظ بمذكرة الاحتساب")
    return out


def _correction_hash(c):
    names = [f.attname for f in SCL._meta.concrete_fields]
    rows = [[str(r[n]) for n in names] for r in SCL._base_manager.filter(correction=c).order_by("pk").values(*names)]
    header = SC._base_manager.filter(pk=c.pk).values("settlement_id", "employee_id", "net_change", "reason_ar",
                                                     "written_consent_ref").first()
    return hashlib.sha256(json.dumps([sorted((header or {}).items()), rows], default=str).encode()).hexdigest()


@transaction.atomic
def create_settlement_correction(settlement, *, user, reason_ar, lines, reason_en="", written_consent_ref=""):
    """§10.8 item 1: a correction of an approved (or paid) final settlement, drafted by the payroll officer
    (``payroll.prepare_settlement_correction``) with difference lines only: ``lines`` = [{line_type, direction
    (increase/decrease), amount, gosi_branch (GOSI lines), component (earnings, optional), description_ar,
    description_en}]. Nothing changes on the settlement or its entry; the correction posts its own entry at approval.
    A decrease never takes a line type below what the settlement booked; recoveries of loans, bai' ajel and GOSI
    shares are not corrected here."""
    fs = FS.objects.select_related("entity", "employee").get(pk=settlement.pk)
    _require(user, fs.entity, "payroll.prepare_settlement_correction")
    if fs.status not in (FSS.APPROVED, FSS.PAID):
        raise error("Only an approved final settlement is corrected; before approval recalculate it instead.",
                    "تُصحح المخالصة المعتمدة فقط؛ وقبل الاعتماد أعد احتسابها.", code="settlement_status")
    if SC.objects.filter(settlement=fs, status__in=(SCS.DRAFT, SCS.SUBMITTED, SCS.RETURNED)).exists():
        raise error("Another correction of this settlement is open: approve or discard it first.",
                    "يوجد تصحيح آخر مفتوح لهذه المخالصة: اعتمده أو ألغه أولًا.", code="correction_open")
    c = SC(entity=fs.entity, settlement=fs, employee=fs.employee, reason_ar=reason_ar or "", reason_en=reason_en,
           written_consent_ref=written_consent_ref or "", created_by=user)
    rows = []
    for spec in lines:
        spec = dict(spec)
        unknown = set(spec) - {"line_type", "direction", "amount", "gosi_branch", "component", "description_ar",
                               "description_en"}
        if unknown:
            raise error(f"Unknown correction line fields: {sorted(unknown)}.", f"حقول غير معروفة: {sorted(unknown)}.",
                        code="fields")
        ltype, direction = spec.get("line_type"), spec.get("direction")
        if ltype not in SCL.ALLOWED or direction not in SCL.Direction.values:
            raise error(f"A correction line is one of {sorted(SCL.ALLOWED)}, increase or decrease; loan, bai' ajel and "
                        "GOSI-share recoveries are not corrected here.",
                        "بند التصحيح من الأنواع المسموح بها، زيادة أو نقص؛ ولا تُصحح هنا استردادات القروض والبيع الآجل.",
                        code="correction_line_type")
        amount = _dec(spec.get("amount"), "amount")
        if amount <= 0 or not fx.has_places(amount, 2):
            raise error("A difference is above zero, in SAR with at most 2 decimals (R5).",
                        "الفرق أكبر من صفر بالريال وبخانتين عشريتين على الأكثر.", code="amount_invalid")
        branch = spec.get("gosi_branch", "") or ""
        if ltype in ("employee_gosi", "employer_gosi") and branch not in PayslipLine.Branch.values:
            raise error("A GOSI difference names its branch.", "يحدد فرق التأمينات فرعه.", code="correction_branch")
        comp = spec.get("component")
        if comp is not None and not hasattr(comp, "pk"):
            comp = SalaryComponent.objects.filter(entity=fs.entity, code=comp).first()
            if comp is None:
                raise error("Unknown component.", "مكوّن غير معروف.", code="component")
        if comp is not None and (comp.entity_id != fs.entity_id or ltype not in SCL.EARNINGS):
            raise error("A component of the entity is named on earnings lines only.",
                        "يُذكر مكوّن الكيان على بنود الاستحقاقات فقط.", code="component")
        en, ar = CORRECTION_TEXT[ltype]
        rows.append(SCL(entity=fs.entity, line_type=ltype, direction=direction, amount=amount, gosi_branch=branch,
                        component=comp, account=_correction_account(fs.entity, ltype, comp),
                        description_en=(spec.get("description_en") or f"Correction: {en} ({direction})")[:300],
                        description_ar=(spec.get("description_ar") or f"تصحيح: {ar}")[:300]))
    problems = _correction_problems(c, rows, fs)
    if problems:
        raise _problems(problems, "correction_invalid")
    if not _correction_entry_amounts(fs.entity, rows):
        raise error("The difference lines book nothing (they cancel out on the same accounts).",
                    "بنود الفرق لا تقيد شيئًا (تتقابل على الحسابات نفسها).", code="correction_empty")
    c.net_change = _correction_net(rows)
    c.warnings = _correction_warnings(c, rows, fs)
    _save(c)
    for r in rows:
        r.correction = c
        _save(r)
    c.stored_hash = _correction_hash(c)
    return _save(c)


def _correction_rows(c):
    return list(SCL._base_manager.filter(correction=c).select_related("account", "component").order_by("pk"))


@transaction.atomic
def submit_settlement_correction(correction, *, user):
    c = _lock(SC, correction.pk)
    _require(user, c.entity, "payroll.prepare_settlement_correction")
    if c.status not in (SCS.DRAFT, SCS.RETURNED):
        raise error("Only a draft or returned correction is submitted.", "يُقدَّم التصحيح المسودة أو المعاد فقط.",
                    code="correction_status")
    fs = FS.objects.get(pk=c.settlement_id)
    problems = _correction_problems(c, _correction_rows(c), fs)
    if problems:
        raise _problems(problems, "correction_invalid")
    c.status, c.submitted_by, c.submitted_at, c.modified_by, c.modified_at = SCS.SUBMITTED, user, _now(), user, _now()
    return _save(c)


@transaction.atomic
def return_settlement_correction(correction, *, user, reason_ar):
    c = _lock(SC, correction.pk)
    grant = _require(user, c.entity, "payroll.approve_settlement_correction", document=c)
    approvals.check(grant, c.entity, "payroll.settlement_correction")
    if c.status != SCS.SUBMITTED or not (reason_ar or "").strip():
        raise error("Only a submitted correction is returned, with a reason in Arabic.",
                    "يُعاد التصحيح المقدّم فقط مع سبب بالعربية.", code="correction_status")
    c.status, c.returned_reason_ar = SCS.RETURNED, reason_ar
    return _save(c)


@transaction.atomic
def discard_settlement_correction(correction, *, user):
    c = _lock(SC, correction.pk)
    _require(user, c.entity, "payroll.prepare_settlement_correction")
    if c.status not in (SCS.DRAFT, SCS.RETURNED):
        raise error("Only a draft or returned correction is discarded.", "يُلغى التصحيح المسودة أو المعاد فقط.",
                    code="correction_status")
    c.status, c.modified_by, c.modified_at = SCS.DISCARDED, user, _now()
    return _save(c)


@transaction.atomic
def approve_settlement_correction(correction, *, user):
    """§10.8 item 1: a second person approves (``payroll.approve_settlement_correction``, A, P; never the preparer,
    submitter or the employee; matrix row ``payroll.settlement_correction``). The stored lines are checked against the
    hash taken when they were written and the limits checked again; approval posts the correction's own entry, dated
    the approval day, through the registry: earnings Dr their accounts (an increase) or Cr (a decrease), EOSB on 5120
    (the provision was used up by the settlement), deductions the other way, employer GOSI Dr 5110 / Cr 2340, the
    change of the net on 2330. The final settlement and its entry are not touched (CLAUDE.md rule 1)."""
    c = SC.objects.select_related("entity").get(pk=correction.pk)
    grant = _require(user, c.entity, "payroll.approve_settlement_correction", document=c)
    approvals.check(grant, c.entity, "payroll.settlement_correction")
    emp = Employee.objects.get(pk=c.employee_id)
    if user.pk in {c.created_by_id, c.submitted_by_id, c.modified_by_id} or (emp.user_id and emp.user_id == user.pk):
        access.deny(user, c.entity, "payroll.approve_settlement_correction", "self_approval")
    _entity_lock(c.entity)
    fs = _lock(FS, c.settlement_id)
    c = _lock(SC, c.pk)
    if c.status != SCS.SUBMITTED:
        raise error("Only a submitted correction is approved.", "يُعتمد التصحيح المقدّم فقط.", code="correction_status")
    rows = _correction_rows(c)
    if _correction_hash(c) != c.stored_hash or _correction_net(rows) != c.net_change:
        access_services.log_denial(actor=user, entity=c.entity, permission="payroll.approve_settlement_correction",
                                   details={"reason": "stored_correction_changed", "correction": c.pk})
        raise error("The stored lines of this correction changed after they were entered: discard it and enter it "
                    "again.", "تغيّرت بنود هذا التصحيح المخزنة بعد إدخالها: ألغه وأدخله مجددًا.", code="payslips_changed")
    problems = _correction_problems(c, rows, fs)
    if problems:
        raise _problems(problems, "correction_invalid")
    c.warnings = _correction_warnings(c, rows, fs)
    c.number = _next_number(c.entity, "FSC", _today().year)
    c.status, c.approved_by, c.approved_at = SCS.APPROVED, user, _now()
    _save(c)
    try:
        with _marker(_posting, c):
            entry = ledger.create_entry(c.entity, _today(), f"Final settlement correction {c.number} / تصحيح مخالصة "
                                        f"نهائية {c.number}", None, user=user, reference=c.number, post=True,
                                        grant=grant, document=c)
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "posting_refused") from None
    c.journal_entry = entry
    _save(c)
    access_services.log(AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=c.entity,
                        permission="payroll.approve_settlement_correction",
                        details={"action": "settlement_correction_approved", "correction": c.pk, "settlement": fs.pk,
                                 "net_change": str(c.net_change), "message_en": "Final settlement correction approved",
                                 "message_ar": "اعتُمد تصحيح المخالصة النهائية"})
    return c


def _correction_entry_amounts(entity, rows):
    """{account pk: [account, signed amount]} of a correction (debit positive)."""
    sums = {}

    def add(acc, amount):
        sums.setdefault(acc.pk, [acc, ZERO])[1] += amount

    net = ZERO
    for r in rows:
        signed = r.sign * r.amount
        if r.line_type in SCL.EARNINGS:
            add(r.account, signed)
            net += signed
        elif r.line_type in SCL.DEDUCTIONS:
            add(r.account, -signed)
            net -= signed
        else:  # employer GOSI
            add(r.account, signed)
            add(_account(entity, GOSI_PAYABLE), -signed)
    add(_account(entity, SALARIES_PAYABLE), -net)
    return {k: v for k, v in sums.items() if v[1]}


def _correction_lines(c):
    """Registered ``build_lines`` for SettlementCorrection: the difference lines summarised per account (R36: the
    number only in descriptions)."""
    _require_posting(c)
    label = f"{c.number}"
    out = []
    for acc, amount in _correction_entry_amounts(c.entity, _correction_rows(c)).values():
        out.append({"account": acc, "debit" if amount > 0 else "credit": abs(amount), "description": label})
    return out


def _correction_credits_2340(entity, period):
    """Signed 2340 credits of approved corrections whose entry is dated in ``period`` (GOSI tie-outs, R23, R29)."""
    start, end = _period_bounds(period)
    total = ZERO
    for l in SCL.objects.filter(entity=entity, correction__status=SCS.APPROVED,
                                correction__journal_entry__date__range=(start, end),
                                line_type__in=("employee_gosi", "employer_gosi")):
        if l.line_type == "employer_gosi" or l.account.code == GOSI_PAYABLE:
            total += l.sign * l.amount
    return total


def settlement_statement_data(settlement, *, user):
    """§10.8 item 1: the printable bilingual statement of a final settlement (Arabic first, "the Arabic text
    prevails"): header, every line as stored, the approved corrections with their difference lines, payments and the
    balance still due. Individual pay: ``payroll.view_payslip``; each view is logged (PDPL)."""
    fs = FS.objects.select_related("entity", "employee", "termination").get(pk=settlement.pk)
    _require(user, fs.entity, "payroll.view_payslip")
    if fs.status in (FSS.DRAFT, FSS.DISCARDED):
        raise error("A statement is printed from a calculated or approved final settlement.",
                    "يُطبع البيان من مخالصة محتسبة أو معتمدة.", code="settlement_status")
    emp = Employee.objects.get(pk=fs.employee_id)
    access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=fs.entity,
                        permission="payroll.view_payslip",
                        details={"action": "settlement_statement_viewed", "settlement": fs.pk,
                                 "employee_code": emp.employee_code})
    lines = list(fs.lines.order_by("pk"))
    corrections = [{"correction": c, "lines": list(c.lines.order_by("pk"))}
                   for c in SC.objects.filter(settlement=fs, status=SCS.APPROVED).order_by("approved_at", "pk")]
    payments = list(PayrollPayment.objects.filter(final_settlement=fs, status=PayrollPayment.Status.APPROVED)
                    .order_by("payment_date", "pk"))
    paid = sum((p.amount for p in payments), ZERO)
    due = _settlement_due(fs)
    term = fs.termination
    return {
        "settlement": fs, "final": fs.status in (FSS.APPROVED, FSS.PAID),
        "entity_en": fs.entity.legal_name_en or fs.entity.name_en, "entity_ar": fs.entity.legal_name_ar or fs.entity.name_ar,
        "employee_code": emp.employee_code, "name_en": emp.full_name_en, "name_ar": emp.full_name_ar,
        "kind_en": term.get_kind_display(), "kind": fs.kind,
        "earnings": [l for l in lines if l.line_type in FinalSettlementLine.EARNINGS],
        "reductions": [l for l in lines if l.line_type in FinalSettlementLine.REDUCTIONS],
        "deductions": [l for l in lines if l.line_type in FinalSettlementLine.DEDUCTIONS],
        "employer": [l for l in lines if l.line_type in FinalSettlementLine.EMPLOYER],
        "corrections": corrections, "payments": payments, "paid": paid, "due": due, "balance": due - paid,
        "interest_free": any(l.line_type == FSL.LOAN_RECOVERY for l in lines),
    }


def render_settlement_statement(settlement, *, user):
    """Bilingual HTML statement (both languages on one page, print-ready)."""
    return render_to_string("payroll/settlement_statement.html", settlement_statement_data(settlement, user=user))


# =====================================================================================================
# Slice 2 (§10.8 item 2): loan write-off on exit (OQ-7 default)
# =====================================================================================================

WO = LoanWriteOff
WOS = LoanWriteOff.Status


def _write_off_account(entity, account):
    s = _settings_of(entity)
    acc = account if isinstance(account, Account) else (_account(entity, account) if account else
                                                        (s.loan_write_off_account or _account(entity, SALARIES)))
    if acc.entity_id != entity.pk or acc.type != "expense" or not acc.is_postable:
        raise error("The write-off is charged to a postable expense account of the entity (OQ-7).",
                    "يُحمّل الشطب على حساب مصروف قابل للترحيل من حسابات الكيان.", code="write_off_account")
    if acc.sharia_prohibited and entity.sharia_enforced:
        raise error(f"Account {acc.code} is Sharia-prohibited in {entity.code} (R37).",
                    f"الحساب {acc.code} محظور شرعًا في {entity.code}.", code="write_off_account")
    return acc


def _write_off_amount(loan):
    """What the final dues did not cover: the loan's installments outstanding after exit (re-review 8)."""
    rows = loan.installments.filter(status=LoanInstallment.Status.OUTSTANDING_AFTER_EXIT)
    planned = loan.installments.filter(status__in=(LoanInstallment.Status.PLANNED, LoanInstallment.Status.PROPOSED))
    return _round(rows.aggregate(t=Sum("amount"))["t"] or ZERO), planned.exists()


@transaction.atomic
def prepare_loan_write_off(loan, *, user, reason_ar, reason_en="", expense_account=None):
    """§10.8 item 2 (OQ-7 default): after the leaver's final settlement is approved, the part of an interest-free loan
    the final dues did not cover (installments ``outstanding_after_exit``) may be written off. The payroll officer
    drafts it (``payroll.prepare_loan_write_off``) with the reason; the expense account is the settings' write-off
    account (default 5100, CFO to confirm). Nothing posts until approval."""
    loan = EmployeeLoan.objects.select_related("entity", "employee").get(pk=loan.pk)
    _require(user, loan.entity, "payroll.prepare_loan_write_off")
    if not (reason_ar or "").strip():
        raise error("The reason in Arabic is required.", "السبب بالعربية مطلوب.", code="reason_required")
    if loan.status != EmployeeLoan.Status.ACTIVE:
        raise error("Only an active loan with a balance outstanding after exit is written off.",
                    "يُشطب القرض النشط ذو الرصيد القائم بعد انتهاء الخدمة فقط.", code="loan_status")
    if not FS.objects.filter(employee=loan.employee, status__in=SETTLED).exists():
        raise error("A loan is written off only after the employee's final settlement is approved: the final dues are "
                    "used first (R28, P17).", "لا يُشطب القرض إلا بعد اعتماد المخالصة النهائية للموظف: تُستخدم "
                    "المستحقات النهائية أولًا.", code="write_off_before_settlement")
    amount, planned = _write_off_amount(loan)
    if planned or amount <= 0 or amount != _round(_loan_outstanding_of(loan)):
        raise error("Only the balance left outstanding after exit is written off (nothing planned may remain).",
                    "يُشطب الرصيد القائم بعد انتهاء الخدمة فقط (دون أقساط مخططة متبقية).", code="write_off_amount")
    acc = _write_off_account(loan.entity, expense_account)
    try:
        with transaction.atomic():
            return _save(WO(entity=loan.entity, loan=loan, employee=loan.employee, amount=amount, expense_account=acc,
                            reason_ar=reason_ar, reason_en=reason_en, created_by=user))
    except IntegrityError:
        raise error("A write-off of this loan is already open or approved.", "يوجد شطب مفتوح أو معتمد لهذا القرض.",
                    code="write_off_exists") from None


@transaction.atomic
def submit_loan_write_off(write_off, *, user):
    w = _lock(WO, write_off.pk)
    _require(user, w.entity, "payroll.prepare_loan_write_off")
    if w.status != WOS.DRAFT:
        raise error("Only a draft write-off is submitted.", "يُقدّم الشطب المسودة فقط.", code="write_off_status")
    w.status, w.submitted_by, w.submitted_at, w.modified_by, w.modified_at = WOS.SUBMITTED, user, _now(), user, _now()
    return _save(w)


@transaction.atomic
def reject_loan_write_off(write_off, *, user, reason_ar):
    w = _lock(WO, write_off.pk)
    grant = _require(user, w.entity, "payroll.approve_loan_write_off", document=w)
    approvals.check(grant, w.entity, "payroll.loan_write_off")
    if w.status not in (WOS.DRAFT, WOS.SUBMITTED) or not (reason_ar or "").strip():
        raise error("An open write-off is rejected with a reason in Arabic.", "يُرفض الشطب المفتوح مع سبب بالعربية.",
                    code="write_off_status")
    w.status, w.rejected_reason_ar = WOS.REJECTED, reason_ar
    return _save(w)


@transaction.atomic
def approve_loan_write_off(write_off, *, user):
    """§10.8 item 2: the matrix approver (``payroll.approve_loan_write_off``, A, P: the Managing Partner in Almiras,
    the entity's CEO elsewhere, ``payroll.approvals``; never the preparer, submitter or the employee) approves; the
    amount is checked again against what is outstanding after exit; approval posts Dr expense / Cr 1230 through the
    registry, dated the approval day, marks the installments written off and the loan ``written_off``, and logs it."""
    w = WO.objects.select_related("entity", "loan").get(pk=write_off.pk)
    grant = _require(user, w.entity, "payroll.approve_loan_write_off", document=w)
    approvals.check(grant, w.entity, "payroll.loan_write_off")
    emp = Employee.objects.get(pk=w.employee_id)
    if user.pk in {w.created_by_id, w.submitted_by_id, w.modified_by_id} or (emp.user_id and emp.user_id == user.pk):
        access.deny(user, w.entity, "payroll.approve_loan_write_off", "self_approval")
    _entity_lock(w.entity)
    loan = _lock(EmployeeLoan, w.loan_id)
    w = _lock(WO, w.pk)
    if w.status != WOS.SUBMITTED:
        raise error("Only a submitted write-off is approved.", "يُعتمد الشطب المقدّم فقط.", code="write_off_status")
    amount, planned = _write_off_amount(loan)
    if planned or amount != w.amount or loan.status != EmployeeLoan.Status.ACTIVE:
        raise error("The loan's balance outstanding after exit changed: reject this write-off and prepare a new one.",
                    "تغيّر رصيد القرض القائم بعد انتهاء الخدمة: ارفض هذا الشطب وأعدّ شطبًا جديدًا.",
                    code="write_off_amount")
    _write_off_account(w.entity, w.expense_account)
    w.number = _next_number(w.entity, "LWO", _today().year)
    w.status, w.approved_by, w.approved_at = WOS.APPROVED, user, _now()
    _save(w)
    try:
        with _marker(_posting, w):
            entry = ledger.create_entry(w.entity, _today(), f"Loan write-off {w.number} / شطب قرض {w.number}", None,
                                        user=user, reference=w.number, post=True, grant=grant, document=w)
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "posting_refused") from None
    w.journal_entry = entry
    _save(w)
    for inst in LoanInstallment._base_manager.select_for_update().filter(
            loan=loan, status=LoanInstallment.Status.OUTSTANDING_AFTER_EXIT).order_by("pk"):
        inst.status = LoanInstallment.Status.WRITTEN_OFF
        _save(inst)
    loan.status = EmployeeLoan.Status.WRITTEN_OFF
    _save(loan)
    access_services.log(AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=w.entity,
                        permission="payroll.approve_loan_write_off",
                        details={"action": "loan_written_off", "write_off": w.pk, "loan": loan.pk,
                                 "employee_code": emp.employee_code, "amount": str(w.amount),
                                 "account": w.expense_account.code, "message_en": "Interest-free loan written off on exit",
                                 "message_ar": "شُطب قرض حسن عند انتهاء الخدمة"})
    return w


def _write_off_lines(w):
    """Registered ``build_lines`` for LoanWriteOff: Dr expense / Cr 1230 (no interest or fee, R26)."""
    _require_posting(w)
    label = f"{w.number} (interest-free / قرض حسن)"
    return [{"account": w.expense_account, "debit": w.amount, "description": label},
            {"account": _account(w.entity, LOANS), "credit": w.amount, "description": label}]


# =====================================================================================================
# Slice 2 (§10.8 item 4): GOSI late-payment fines (E14) and payment reversals
# =====================================================================================================

GOSI_FINES = "5930"
GF = GosiFine
GFS = GosiFine.Status


def _fines_2340(entity, period):
    """Approved GOSI fines of a contribution period (Cr 2340): paid to GOSI with that period's contributions."""
    return GF.objects.filter(entity=entity, period=period, status=GFS.APPROVED).aggregate(t=Sum("amount"))["t"] or ZERO


def _gosi_fine_estimate(entity, period, paid_on):
    """R37 (Law Art. 9(3); Regs Art. 24, 25): 2% of the contributions due for each month of delay or part of a month,
    counted from the day after the due day (the 15th of the following month). An estimate shown next to GOSI's
    figure; GOSI's invoice governs and the ERP never posts a fine by itself. Returns (estimate, months late)."""
    _check_period(period)
    end = _period_bounds(period)[1]
    due_day = int(hr.param("SA", "gosi.payment_due_day", end).value["new_m273"])
    due = _period_bounds(_next_period(period))[0].replace(day=due_day)
    if paid_on <= due:
        return ZERO, 0
    months, cursor = 0, due
    while cursor < paid_on:
        months += 1
        y, m = (cursor.year + (cursor.month // 12), cursor.month % 12 + 1)
        cursor = cursor.replace(year=y, month=m, day=min(due_day, calendar.monthrange(y, m)[1]))
    pct = Fraction(hr.param("SA", "gosi.late_fine_pct", end).value["pct"])
    base = _gosi_booked(entity, period) - _fines_2340(entity, period)
    return _round(Fraction(base) * pct / 100 * months), months


@transaction.atomic
def record_gosi_fine(entity, *, user, period, amount, fine_date, gosi_invoice_ref, paid_on=None, note_ar="",
                     note_en=""):
    """E14 (R37): the payroll officer records the late-payment fine from GOSI's invoice (``payroll.record_gosi_fine``)
    with GOSI's reference; the ERP estimate (2% a month or part, ``paid_on``: the day the contributions were paid,
    default the fine date) is stored next to it and a difference is a warning only. Posts nothing until approval."""
    _require(user, entity, "payroll.record_gosi_fine")
    _check_period(period)
    amount = _dec(amount, "amount")
    if amount <= 0 or not fx.has_places(amount, 2) or not (gosi_invoice_ref or "").strip() or fine_date is None:
        raise error("A GOSI fine needs the amount (SAR, 2 decimals), its date and GOSI's invoice reference (E14).",
                    "تحتاج غرامة التأمينات إلى المبلغ وتاريخها ومرجع فاتورة التأمينات.", code="fine_invalid")
    _account(entity, GOSI_FINES)
    estimate, months = _gosi_fine_estimate(entity, period, paid_on or fine_date)
    if estimate != amount:
        note_en = (note_en + " " if note_en else "") + (f"ERP estimate {estimate} ({months} month(s) at 2%); GOSI's "
                                                         "figure governs (Regs Art. 25 waivers).")
        note_ar = (note_ar + " " if note_ar else "") + f"تقدير النظام {estimate} ({months} شهر بنسبة 2%)؛ والمعتمد رقم التأمينات."
    return _save(GF(entity=entity, period=period, fine_date=fine_date, amount=amount, estimate=estimate,
                    gosi_invoice_ref=gosi_invoice_ref, note_ar=note_ar, note_en=note_en, created_by=user))


@transaction.atomic
def submit_gosi_fine(fine, *, user):
    f = _lock(GF, fine.pk)
    _require(user, f.entity, "payroll.record_gosi_fine")
    if f.status != GFS.DRAFT:
        raise error("Only a draft fine is submitted.", "تُقدّم الغرامة المسودة فقط.", code="fine_status")
    f.status, f.submitted_by, f.modified_by, f.modified_at = GFS.SUBMITTED, user, user, _now()
    return _save(f)


@transaction.atomic
def approve_gosi_fine(fine, *, user):
    """E14: the approver (``payroll.approve_gosi_fine``, A, P; never the recorder) posts Dr 5930 GOSI late-payment
    fines / Cr 2340 GOSI payable, dated the fine date (or the first day of the next open period). 5930 is never an
    interest account and carries no purification flag (R37); tax penalties never go there (P20(f))."""
    f = GF.objects.select_related("entity").get(pk=fine.pk)
    grant = _require(user, f.entity, "payroll.approve_gosi_fine", document=f)
    approvals.check(grant, f.entity, "payroll.gosi_fine")
    if user.pk in {f.created_by_id, f.submitted_by_id, f.modified_by_id}:
        access.deny(user, f.entity, "payroll.approve_gosi_fine", "self_approval")
    _entity_lock(f.entity)
    f = _lock(GF, f.pk)
    if f.status != GFS.SUBMITTED:
        raise error("Only a submitted fine is approved.", "تُعتمد الغرامة المقدّمة فقط.", code="fine_status")
    acc = _account(f.entity, GOSI_FINES)
    if acc.sharia_prohibited or acc.type != "expense" or getattr(acc, "requires_purification", False):
        raise error("Account 5930 must be a plain expense account (no interest, no purification; R37).",
                    "يجب أن يكون الحساب 5930 حساب مصروف عاديًا (لا فوائد ولا تطهير).", code="fine_account")
    on = _open_posting_date(f.entity, f.fine_date)
    f.number = _next_number(f.entity, "GFN", on.year)
    f.status, f.approved_by, f.approved_at = GFS.APPROVED, user, _now()
    _save(f)
    try:
        with _marker(_posting, f):
            entry = ledger.create_entry(f.entity, on, f"GOSI late-payment fine {f.number} {f.period} / غرامة تأخير "
                                        f"التأمينات {f.number}", None, user=user, reference=f.gosi_invoice_ref[:60],
                                        post=True, grant=grant, document=f)
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "posting_refused") from None
    f.journal_entry = entry
    return _save(f)


def _fine_lines(f):
    """Registered ``build_lines`` for GosiFine: E14, Dr 5930 / Cr 2340."""
    _require_posting(f)
    label = f"{f.number} {f.period}"
    return [{"account": _account(f.entity, GOSI_FINES), "debit": f.amount, "description": label},
            {"account": _account(f.entity, GOSI_PAYABLE), "credit": f.amount, "description": label}]


@transaction.atomic
def reverse_payment(payment, *, user, reason, date=None):
    """§10.8 item 4: an approved payroll payment (salaries, GOSI or final settlement) that the bank did not execute,
    or that went out wrong, is reversed with ``ledger.reverse_entry`` (``payroll.reverse_payment``, P; never its
    recorder or approver), dated in an open period; the payment becomes ``reversed`` and is never edited; a paid run
    or final settlement goes back to ``approved`` so the net is open again; the WHT of a remuneration payment shows
    as a negative row in the WHT return of the reversal month."""
    p = PayrollPayment.objects.select_related("entity", "journal_entry").get(pk=payment.pk)
    grant = _require(user, p.entity, "payroll.reverse_payment", document=p)
    approvals.check(grant, p.entity, "payroll.payment_reversal")
    if user.pk in {p.created_by_id, p.submitted_by_id, p.approved_by_id}:
        access.deny(user, p.entity, "payroll.reverse_payment", "self_approval")
    if not (reason or "").strip():
        raise error("A reason is required.", "السبب مطلوب.", code="reason_required")
    _entity_lock(p.entity)
    run = _lock(PayrollRun, p.run_id) if p.run_id else None
    fs = _lock(FinalSettlement, p.final_settlement_id) if p.final_settlement_id else None
    p = _lock(PayrollPayment, p.pk)
    if p.status != PayrollPayment.Status.APPROVED or p.journal_entry_id is None:
        raise error("Only an approved payment is reversed.", "تُعكس الدفعة المعتمدة فقط.", code="payment_status")
    on = _reversal_date(p.entity, p.payment_date, date)
    try:
        with _marker(_reversing, p):
            reversal = ledger.reverse_entry(p.journal_entry, grant, date=on, reason=reason.strip())
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "reversal_refused") from None
    p.status, p.reversal_entry, p.reversed_by, p.reversed_at = PayrollPayment.Status.REVERSED, reversal, user, _now()
    p.reversal_reason = reason.strip()
    _save(p)
    if run is not None and run.status == RS.PAID:
        run.status = RS.APPROVED
        _save(run)
    if fs is not None and fs.status == FSS.PAID:
        fs.status = FSS.APPROVED
        _save(fs)
    return p


# =====================================================================================================
# Slice 2 (§10.8 item 3): employee cost items (R63) and the fully loaded secondment cost report (R35; billing R71)
# =====================================================================================================

CI = EmployeeCostItem
CIS = EmployeeCostItem.Status
CR = SecondmentCostReport
CRS = SecondmentCostReport.Status
VAT_INPUT = "1310"


def _source_of(entity, source_bill, source_entry):
    """The posted source document of a cost item (R63) and the cost it booked: its journal entry's debits other
    than recoverable input VAT (1310). Returns (entry, cost, month)."""
    if (source_bill is None) == (source_entry is None):
        raise error("A cost item references exactly one source document: a posted vendor bill or a posted journal "
                    "entry of the same entity (R63).", "يشير بند التكلفة إلى مستند مصدر واحد: فاتورة مورد مرحّلة أو "
                    "قيد مرحّل للكيان نفسه.", code="cost_item_source")
    if source_bill is not None:
        if (source_bill.entity_id != entity.pk or source_bill.doc_type != "bill" or source_bill.status != "posted"
                or source_bill.journal_entry_id is None):
            raise error("The source bill must be a posted vendor bill of the same entity (R63).",
                        "يجب أن تكون فاتورة المصدر فاتورة مورد مرحّلة للكيان نفسه.", code="cost_item_source")
        entry = source_bill.journal_entry
    else:
        entry = source_entry
        if entry.entity_id != entity.pk or entry.status != "posted":
            raise error("The source journal entry must be a posted entry of the same entity (R63).",
                        "يجب أن يكون قيد المصدر قيدًا مرحّلًا للكيان نفسه.", code="cost_item_source")
    cost = JournalLine.objects.filter(entry=entry).exclude(account__code=VAT_INPUT).aggregate(t=Sum("debit"))["t"] \
        or ZERO
    return entry, _round(cost), _period_of(entry.date)


@transaction.atomic
def create_cost_item(employee, *, user, kind, amount, description_ar, source_bill=None, source_entry=None,
                     description_en="", month=None):
    """R63: a cost paid outside payroll that belongs to one employee (``payroll.manage_cost_items``). It references
    its posted source document (vendor bill or journal entry) of the same entity; its month is the source's
    accounting month (default, billing OQ-37); the items on one source never exceed the cost it booked. Posts
    nothing."""
    emp = Employee.objects.select_related("entity").get(pk=employee.pk)
    _require(user, emp.entity, "payroll.manage_cost_items")
    if kind not in CI.Kind.values or not (description_ar or "").strip():
        raise error("A cost item needs its kind and a description in Arabic (R63).",
                    "يحتاج بند التكلفة إلى نوعه ووصف بالعربية.", code="cost_item_invalid")
    amount = _dec(amount, "amount")
    if amount <= 0 or not fx.has_places(amount, 2):
        raise error("The amount is above zero, in the functional currency with 2 decimals (R5).",
                    "المبلغ أكبر من صفر بالعملة الوظيفية وبخانتين عشريتين.", code="amount_invalid")
    entry, cost, source_month = _source_of(emp.entity, source_bill, source_entry)
    if month is not None and month != source_month:
        raise error(f"A cost item belongs to its source's accounting month {source_month} (R63, billing OQ-37).",
                    f"ينتمي بند التكلفة إلى شهر قيد مصدره {source_month}.", code="cost_item_month")
    used = CI.objects.filter(entity=emp.entity, status__in=(CIS.DRAFT, CIS.APPROVED)).filter(
        Q(source_bill__journal_entry=entry) | Q(source_entry=entry)).aggregate(t=Sum("amount"))["t"] or ZERO
    if used + amount > cost:
        raise error(f"The cost items on this source ({used + amount}) would exceed the cost it booked ({cost}).",
                    f"بنود التكلفة على هذا المصدر ({used + amount}) تتجاوز التكلفة المقيدة فيه ({cost}).",
                    code="cost_item_exceeds_source")
    return _save(CI(entity=emp.entity, employee=emp, month=source_month, kind=kind, amount=amount,
                    description_ar=description_ar, description_en=description_en, source_bill=source_bill,
                    source_entry=source_entry if source_bill is None else None, created_by=user))


@transaction.atomic
def approve_cost_item(item, *, user):
    """R63: the approver (``payroll.approve_cost_item``, A: never its creator) approves; nothing is posted."""
    i = CI.objects.select_related("entity").get(pk=item.pk)
    grant = _require(user, i.entity, "payroll.approve_cost_item", document=i)
    approvals.check(grant, i.entity, "payroll.cost_item")
    i = _lock(CI, i.pk)
    if i.status != CIS.DRAFT:
        raise error("Only a draft cost item is approved.", "يُعتمد بند التكلفة المسودة فقط.", code="cost_item_status")
    i.status, i.approved_by, i.approved_at = CIS.APPROVED, user, _now()
    return _save(i)


@transaction.atomic
def reject_cost_item(item, *, user):
    i = CI.objects.select_related("entity").get(pk=item.pk)
    grant = _require(user, i.entity, "payroll.approve_cost_item", document=i)
    approvals.check(grant, i.entity, "payroll.cost_item")
    i = _lock(CI, i.pk)
    if i.status != CIS.DRAFT:
        raise error("Only a draft cost item is rejected.", "يُرفض بند التكلفة المسودة فقط.", code="cost_item_status")
    i.status, i.modified_by, i.modified_at = CIS.REJECTED, user, _now()
    return _save(i)


def _secondments(entity, period):
    start, end = _period_bounds(period)
    for a in Assignment.objects.filter(entity=entity, kind=Assignment.Kind.SECONDMENT, start_date__lte=end) \
            .exclude(status__in=(Assignment.Status.DRAFT, Assignment.Status.SUBMITTED, Assignment.Status.CANCELLED)) \
            .select_related("employee", "receiving_entity").order_by("employee__employee_code", "pk"):
        if a.end_date and a.end_date < start:
            continue
        yield a, (min(a.end_date or end, end) - max(a.start_date, start)).days + 1, (end - start).days + 1


def _cost_lines(entity, period):
    """R35 as amended (P19(c)): per secondment, the month's fully loaded cost for the days on secondment: gross pay
    and benefits through payroll, employer GOSI and the EOSB accrual of the month's approved payslips, and the
    employee's approved cost items of the month, prorated to the days on secondment (billing R71)."""
    out = []
    for a, days, dim in _secondments(entity, period):
        share = Fraction(days, dim)
        slips = Payslip.objects.filter(employee=a.employee, run__period=period, refused=False,
                                       run__status__in=(RS.APPROVED, RS.PAID))
        sums = slips.aggregate(g=Sum("gross"), e=Sum("employer_gosi"), b=Sum("eosb_accrual"))
        items = {}
        for i in CI.objects.filter(employee=a.employee, month=period, status=CIS.APPROVED).order_by("pk"):
            items[i.kind] = items.get(i.kind, ZERO) + i.amount
        items = {k: _round(Fraction(v) * share) for k, v in sorted(items.items())}
        row = SecondmentCostLine(entity=entity, assignment=a, employee_code=a.employee.employee_code,
                                 receiving_entity=a.receiving_entity, days=days, days_in_month=dim,
                                 gross=_round(Fraction(sums["g"] or ZERO) * share),
                                 employer_gosi=_round(Fraction(sums["e"] or ZERO) * share),
                                 eosb_accrual=_round(Fraction(sums["b"] or ZERO) * share),
                                 cost_items={k: str(v) for k, v in items.items()},
                                 cost_items_total=sum(items.values(), ZERO))
        row.total = row.gross + row.employer_gosi + row.eosb_accrual + row.cost_items_total
        warnings = []
        if not slips.exists():
            warnings.append("No approved payslip for the month: payroll costs are zero / لا توجد قسيمة معتمدة للشهر: "
                            "تكاليف الرواتب صفر")
        if a.receiving_entity is not None and a.receiving_entity.country == "SA":
            warnings.append("Receiving entity may have to register GOSI (Regs Art. 20(3)(a), OQ-48) / قد يلزم الكيان "
                            "المستقبل التسجيل في التأمينات")
        row.warnings = warnings
        out.append(row)
    return out


def _cost_report_hash(report):
    names = [f.attname for f in SecondmentCostLine._meta.concrete_fields]
    rows = [[str(r[n]) for n in names] for r in SecondmentCostLine._base_manager.filter(report=report).order_by("pk")
            .values(*names)]
    header = CR._base_manager.filter(pk=report.pk).values("entity_id", "period", "version", "total").first()
    return hashlib.sha256(json.dumps([sorted((header or {}).items()), rows], default=str).encode()).hexdigest()


def _cost_fingerprint(rows):
    return [[r.assignment_id, r.days, str(r.gross), str(r.employer_gosi), str(r.eosb_accrual),
             sorted(r.cost_items.items()), str(r.total)] for r in rows]


@transaction.atomic
def prepare_cost_report(entity, period, *, user):
    """R35, R63: the payroll officer prepares (or recalculates) the month's fully loaded secondment cost report
    (``payroll.prepare_cost_report``) once the month's regular run is approved. A report already approved for the
    month stays in force until a new version is approved (a later correction, billed as an adjustment by billing)."""
    _require(user, entity, "payroll.prepare_cost_report")
    _check_period(period)
    if not PayrollRun.objects.filter(entity=entity, period=period, kind=PayrollRun.Kind.REGULAR,
                                     status__in=(RS.APPROVED, RS.PAID)).exists():
        raise error(f"The secondment cost report of {period} is prepared after the month's regular run is approved "
                    "(R35).", f"يُعد تقرير تكلفة الإعارة لفترة {period} بعد اعتماد المسير العادي للشهر.",
                    code="cost_report_run_missing")
    open_ = CR._base_manager.select_for_update().filter(entity=entity, period=period,
                                                       status__in=(CRS.DRAFT, CRS.SUBMITTED)).first()
    if open_ is not None and open_.status == CRS.SUBMITTED:
        raise error("The report of this month is submitted: approve it or send it back first.",
                    "تقرير هذا الشهر مقدّم: اعتمده أو أعده أولًا.", code="cost_report_status")
    if open_ is None:
        last = CR.objects.filter(entity=entity, period=period).order_by("-version").first()
        open_ = _save(CR(entity=entity, period=period, version=(last.version + 1) if last else 1, created_by=user))
    else:
        for line in SecondmentCostLine.objects.filter(report=open_):
            line._service_delete = True
            line.delete()
        open_.modified_by, open_.modified_at = user, _now()
    rows = _cost_lines(entity, period)
    for r in rows:
        r.report = open_
        _save(r)
    open_.total = sum((r.total for r in rows), ZERO)
    open_.warnings = sorted({w_ for r in rows for w_ in r.warnings})
    _save(open_)
    open_.stored_hash = _cost_report_hash(open_)
    return _save(open_)


@transaction.atomic
def submit_cost_report(report, *, user):
    r = _lock(CR, report.pk)
    _require(user, r.entity, "payroll.prepare_cost_report")
    if r.status != CRS.DRAFT:
        raise error("Only a draft report is submitted.", "يُقدّم التقرير المسودة فقط.", code="cost_report_status")
    r.status, r.submitted_by, r.submitted_at = CRS.SUBMITTED, user, _now()
    return _save(r)


@transaction.atomic
def return_cost_report(report, *, user):
    r = CR.objects.select_related("entity").get(pk=report.pk)
    grant = _require(user, r.entity, "payroll.approve_cost_report", document=r)
    approvals.check(grant, r.entity, "payroll.cost_report")
    r = _lock(CR, r.pk)
    if r.status != CRS.SUBMITTED:
        raise error("Only a submitted report is returned.", "يُعاد التقرير المقدّم فقط.", code="cost_report_status")
    r.status = CRS.DRAFT
    return _save(r)


@transaction.atomic
def approve_cost_report(report, *, user):
    """R35 (P19(c)): the approver (``payroll.approve_cost_report``, A: never its preparer or submitter) approves the
    report after a recalculation check (refused "recalculate" if a payslip or cost item changed); the earlier
    approved version of the month is superseded. Nothing is posted; billing reads the approved totals only."""
    r = CR.objects.select_related("entity").get(pk=report.pk)
    grant = _require(user, r.entity, "payroll.approve_cost_report", document=r)
    approvals.check(grant, r.entity, "payroll.cost_report")
    if user.pk in {r.created_by_id, r.submitted_by_id, r.modified_by_id}:
        access.deny(user, r.entity, "payroll.approve_cost_report", "self_approval")
    r = _lock(CR, r.pk)
    if r.status != CRS.SUBMITTED:
        raise error("Only a submitted report is approved.", "يُعتمد التقرير المقدّم فقط.", code="cost_report_status")
    stored = list(SecondmentCostLine.objects.filter(report=r).order_by("pk"))
    if _cost_report_hash(r) != r.stored_hash or _cost_fingerprint(stored) != _cost_fingerprint(_cost_lines(r.entity,
                                                                                                           r.period)):
        raise error("A payslip, secondment or cost item changed after the report was prepared: prepare it again.",
                    "تغيّرت قسيمة أو إعارة أو بند تكلفة بعد إعداد التقرير: أعد إعداده.", code="recalculate")
    for old in CR._base_manager.select_for_update().filter(entity=r.entity, period=r.period, status=CRS.APPROVED):
        old.status = CRS.SUPERSEDED
        _save(old)
    r.status, r.approved_by, r.approved_at = CRS.APPROVED, user, _now()
    return _save(r)


def cost_report_data(report, *, user):
    """The report for accounting staff (``payroll.view_cost_reports``): codes and totals only, no names or pay
    items (R35, PDPL)."""
    r = CR.objects.select_related("entity").get(pk=report.pk)
    _require(user, r.entity, "payroll.view_cost_reports")
    return {"entity": r.entity.code, "period": r.period, "version": r.version, "status": r.status, "total": r.total,
            "warnings": r.warnings,
            "lines": [{"employee_code": l.employee_code, "assignment": l.assignment.reference or l.assignment_id,
                       "receiving_entity": l.receiving_entity.code if l.receiving_entity else "", "days": l.days,
                       "days_in_month": l.days_in_month, "gross": l.gross, "employer_gosi": l.employer_gosi,
                       "eosb_accrual": l.eosb_accrual, "cost_items": l.cost_items,
                       "cost_items_total": l.cost_items_total, "total": l.total, "warnings": l.warnings}
                      for l in r.lines.select_related("assignment", "receiving_entity").order_by("pk")]}


def _approved_secondment_cost(assignment, period):
    """Billing R71 seam (``payroll.billing_port``): the approved fully loaded cost of ``assignment`` for ``period``
    in the seconding entity's functional currency, or None while no report of that month is approved. Totals only,
    no personal data (R35); service to service, no user (billing's system user holds no payroll right, R38)."""
    line = (SecondmentCostLine.objects.filter(assignment_id=getattr(assignment, "pk", assignment),
                                              report__period=period, report__status=CRS.APPROVED)
            .select_related("report", "report__entity").first())
    if line is None:
        return None
    r = line.report
    return SimpleNamespace(total=line.total, currency=r.entity.base_currency_id, days=line.days,
                           days_in_month=line.days_in_month, report_id=r.pk, version=r.version,
                           approved_at=r.approved_at)


# =====================================================================================================
# Slice 2 (§10.8 item 7): Egypt payroll (jurisdiction EG; docs/compliance/egypt-payroll-2026-10-18.md)
# =====================================================================================================

# Accounts of the Egypt lines (defaults on the group chart, CFO to approve dedicated ones, research §2.5 item 3):
# social insurance (NOSI) on the social-insurance payable and expense accounts (2340 / 5110, "التأمينات الاجتماعية"),
# salary tax withheld on 2220 (withholding tax payable), Martyrs' Fund and Universal Health Insurance on 2300.
EG_ACCOUNTS = {"si_payable": GOSI_PAYABLE, "si_expense": GOSI_EXPENSE, "tax_payable": WHT_PAYABLE,
               "martyrs_payable": "2300", "uhi_payable": "2300", "uhi_expense": GOSI_EXPENSE}
EG_EMPLOYER_PAYABLE = {LT.SI_EMPLOYER: "si_payable", LT.UHI_EMPLOYER: "uhi_payable"}
EG_STATUTORY = (LT.SI_EMPLOYEE, LT.SALARY_TAX, LT.MARTYRS_FUND, LT.UHI_EMPLOYEE)
EG_INPUT_KINDS = (IK.ONE_OFF_EARNING, IK.BONUS, IK.ONE_OFF_DEDUCTION, IK.UNPAID_DAYS)
EG_DEDUCTION_BASES = (PayrollInput.DeductionBasis.WRITTEN_CONSENT, PayrollInput.DeductionBasis.COURT_ORDER,
                      PayrollInput.DeductionBasis.NAFAQA, PayrollInput.DeductionBasis.STATUTORY_OTHER)


def _eg_input_problems(inp):
    """Egypt inputs (Labour Law 14/2025, research rows): one-off earnings and bonuses, unpaid days and one-off
    deductions with their basis (written consent, court order, maintenance (nafaqa) order, other statutory). Overtime
    is refused until its base is confirmed (research §4 item 15); the Saudi Art. 91 damage basis does not apply."""
    problems = []
    if inp.kind not in EG_INPUT_KINDS:
        problems.append(f"{inp.kind} is not built for Egypt payroll (overtime base and premiums to confirm, research "
                        "§4 item 15). / هذا النوع غير مبني لرواتب مصر.")
        return problems
    if not (inp.reason_ar or "").strip():
        problems.append("The reason in Arabic is required. / السبب بالعربية مطلوب.")
    if inp.kind == IK.UNPAID_DAYS:
        if inp.quantity is None or inp.quantity <= 0 or inp.amount is not None:
            problems.append("Unpaid days are a quantity above zero. / الأيام بدون أجر كمية أكبر من صفر.")
        return problems
    cur = inp.employee.entity.base_currency
    if inp.amount is None or inp.amount <= 0 or inp.quantity is not None:
        problems.append("This input is an amount above zero. / هذا المدخل مبلغ أكبر من صفر.")
    elif not fx.has_places(inp.amount, cur.minor_units):
        problems.append(f"{cur.code} amounts have at most {cur.minor_units} decimals (R5). / مبالغ {cur.code} بحد أقصى "
                        f"{cur.minor_units} خانات عشرية.")
    if inp.component_id is not None and inp.component.entity_id != inp.entity_id:
        problems.append("The component belongs to another entity (R1). / المكوّن يخص كيانًا آخر.")
    if inp.kind == IK.ONE_OFF_DEDUCTION:
        if inp.deduction_basis not in EG_DEDUCTION_BASES:
            problems.append("An Egypt deduction states its basis: written consent, court order, maintenance (nafaqa) "
                            "order or another statutory deduction (Labour Law 14/2025 Art. 113). / يذكر الاستقطاع أساسه.")
        elif inp.deduction_basis == PayrollInput.DeductionBasis.WRITTEN_CONSENT and not inp.consent_ref.strip():
            problems.append("A deduction for a debt needs the worker's written consent (Art. 113). / يحتاج الاستقطاع "
                            "لدين إلى موافقة العامل الكتابية.")
    return problems


def _eg(ctx, code, on=None):
    return ctx.use.get("EG", code, on or ctx.end, "Egypt payroll", "رواتب مصر")


def _eg_row(ctx, code, on=None):
    row = hr.param("EG", code, on or ctx.end, required=False)
    if row is None:
        raise hr._not_configured("EG", code, on or ctx.end, "Egypt payroll", "رواتب مصر")
    ctx.use._take(row)
    return row


def _eg_annual_tax(ctx, income, resident):
    """Salary tax of a year's net taxable income (Law 91/2005 Art. 8 as amended by Law 7/2024; research §1.3): the
    brackets with the high-income exclusion, the column chosen by the whole income; the 0% band for residents only
    (research rows, unverified)."""
    income = Fraction(income)
    if income <= 0:
        return Fraction(0)
    bands = _eg(ctx, "tax.salary.brackets")["bands"]
    columns = _eg(ctx, "tax.salary.bracket_exclusion")["columns"]
    zero = _eg(ctx, "tax.salary.zero_band_residents_only")
    col = next(c for c in columns if c["income_upto"] is None or income <= Fraction(c["income_upto"]))
    rates = list(col["rates"])
    if not resident and zero.get("apply") and Fraction(rates[0]) == 0:
        rates[0] = zero["non_resident_first_band_rate"]
    tax, lower = Fraction(0), Fraction(0)
    for band, rate in zip(bands, rates):
        upper = Fraction(band["to"]) if band["to"] is not None else None
        if income <= lower:
            break
        top = income if upper is None else min(income, upper)
        tax += (top - lower) * Fraction(rate)
        if upper is None:
            break
        lower = upper
    return tax


def _eg_salary_tax(ctx, slip, emp, taxable):
    """Monthly withholding, cumulative over the calendar year (research §2.3; ``tax.salary.method``): annualise the
    year-to-date taxable pay, deduct the personal exemption, tax the year, withhold the year-to-date share less what
    earlier runs withheld; a negative result is not refunded by payroll (annual settlement, warning)."""
    method = _eg(ctx, "tax.salary.method")
    if method.get("method") != "cumulative_annualised":
        slip.refuse(f"Unknown salary tax method {method.get('method')!r}.", "طريقة ضريبة كسب العمل غير معروفة.")
    exemption = Fraction(_eg(ctx, "tax.salary.personal_exemption_annual")["amount"])
    year = ctx.period[:4]
    prev = Payslip.objects.filter(employee=emp, run__period__startswith=f"{year}-", run__period__lt=ctx.period,
                                  refused=False, run__status__in=(RS.APPROVED, RS.PAID))
    ytd = Fraction(sum((p.taxable_income for p in prev), ZERO) + taxable)
    months = len(set(prev.values_list("run__period", flat=True))) + 1
    withheld = PayslipLine.objects.filter(payslip__in=prev, line_type=LT.SALARY_TAX).aggregate(t=Sum("amount"))["t"] \
        or ZERO
    annual = ytd * 12 / months
    resident = (emp.country_of_residence or "EG") == "EG"
    annual_tax = _eg_annual_tax(ctx, max(annual - exemption, Fraction(0)), resident)
    due = _round(annual_tax * months / 12) - withheld
    if due < 0:
        slip.warn(f"Salary tax withheld so far exceeds the year-to-date tax by {-due}: settled in the annual employer "
                  "settlement, not refunded by payroll.", f"تجاوزت الضريبة المستقطعة حتى الآن الضريبة المستحقة بمبلغ "
                  f"{-due}: تُسوى في التسوية السنوية.")
        due = ZERO
    return due


def _eg_insurable_wage(ctx, slip, info):
    """Social insurance (Law 148/2019): the insurable wage of the version in force on the first employed day from
    the components listed in ``si.insurable_components``, clamped to the year's minimum and maximum
    (``si.wage_limits``, valid from 1 January of the run's year: NOSI announces each year's limits, so a year without
    its row is refused), prorated in a joining or leaving month per ``si.partial_month``."""
    row = _eg_row(ctx, "si.wage_limits")
    if row.valid_from.year != int(ctx.period[:4]):
        slip.refuse(f"Social insurance wage limits for {ctx.period[:4]} are not configured (si.wage_limits; NOSI "
                    "announces them each November).", f"حدود الأجر التأميني لسنة {ctx.period[:4]} غير مهيأة.")
    codes = set(_eg(ctx, "si.insurable_components")["codes"])
    v = info.version_on(info.first)
    full = sum((Fraction(c.monthly_amount) for c in _components(v) if c.component.code in codes), Fraction(0))
    full = min(max(full, Fraction(row.value["min_monthly"])), Fraction(row.value["max_monthly"]))
    wage = full
    if not info.full_month:
        rule = _eg(ctx, "si.partial_month")["rule"]
        if rule != "prorate_paid_days":
            slip.refuse(f"Unknown partial-month rule {rule!r}.", "قاعدة الشهر الجزئي غير معروفة.")
        wage = full * info.paid_days / info.divisor
        slip.notes.append("Joining or leaving month: insurable wage prorated by paid days (si.partial_month, "
                          "unverified). / شهر الالتحاق أو الترك: الأجر التأميني بنسبة الأيام المدفوعة.")
    return _round(full), _round(wage)


def _eg_caps(ctx, slip, emp):
    """Labour Law 14/2025 Art. 113/114 (research rows): on the wage after income tax and social insurance, a
    maintenance (nafaqa) order at most 50% and first; all other debts at most 25%, and with the nafaqa at most 50% in
    total; loans and advances at most 10% (payroll loans are KSA only today, OQ-32). A fixed deduction above its cap
    refuses the payslip (the Saudi caps are never used for Egypt)."""
    debt = _eg(ctx, "deduction.debt_max_pct")
    loan_pct = Fraction(_eg(ctx, "deduction.loan_max_pct")["pct"])
    gross = slip.total(PayslipLine.EARNINGS) - slip.total(PayslipLine.REDUCTIONS)
    after = gross - slip.total(set(EG_STATUTORY))
    if after <= 0:
        return
    nafaqa = sum((l.amount for l in slip.lines if l.line_type == LT.DEDUCTION and l.basis == "nafaqa"), ZERO)
    debts = sum((l.amount for l in slip.lines if l.line_type == LT.DEDUCTION and l.basis != "nafaqa"), ZERO)
    nafaqa_cap = _round(Fraction(after) * Fraction(debt["nafaqa"]))
    debt_cap = min(_round(Fraction(after) * Fraction(debt["debt"])), nafaqa_cap - nafaqa)
    if nafaqa > nafaqa_cap:
        slip.refuse(f"The maintenance (nafaqa) deduction {nafaqa} exceeds half of the wage after tax and insurance "
                    f"({nafaqa_cap}) (Art. 113).", f"استقطاع النفقة {nafaqa} يتجاوز نصف الأجر بعد الضريبة والتأمينات "
                    f"({nafaqa_cap}).")
    if debts > debt_cap:
        slip.refuse(f"Debt deductions {debts} exceed {debt_cap}: a quarter of the wage after tax and insurance, and "
                    "half together with a nafaqa (Art. 113).", f"استقطاعات الديون {debts} تتجاوز {debt_cap}: ربع الأجر "
                    "بعد الضريبة والتأمينات، والنصف مع النفقة.")
    if any(l.line_type == LT.LOAN_INSTALLMENT for l in slip.lines):
        loan_cap = _round(Fraction(after) * loan_pct)
        if slip.total({LT.LOAN_INSTALLMENT}) > loan_cap:
            slip.refuse(f"Loan installments exceed 10% of the wage after tax ({loan_cap}) (Art. 113).",
                        f"أقساط القروض تتجاوز 10% من الأجر بعد الضريبة ({loan_cap}).")


def _calc_eg(ctx, emp, versions, info):
    """§10.8 item 7: one Egypt payslip (local entity employee, EGP) from the EG parameters only (never a Saudi row,
    R51): prorated contract earnings, unpaid days, approved inputs; social insurance 11% employee / 18.75% employer
    on the insurable wage within the year's limits; Universal Health Insurance where the work governorate is live
    (employee 1%, employer 4% with the monthly minimum); Martyrs' Fund 0.05% of the gross; salary tax by cumulative
    annualised withholding with the band exclusion; the Egypt deduction caps; no EOSB accrual (no statutory gratuity,
    ``eosb.monthly_accrual``). All rows are unverified research until a person verifies them."""
    v_last = info.version_on(info.last)
    slip = Slip(employee=emp, basis=emp.employment_basis, nationality_class=_nationality_class(emp), cohort="",
                currency=ctx.entity.base_currency, contract=v_last, paid_days=Decimal(info.paid_days),
                iban_hash=_iban_hash(emp.iban), termination_ref=getattr(_termination(emp), "pk", None))
    try:
        inputs = _employee_inputs(ctx, emp)
        slip.input_ids = [i.pk for i in inputs]
        for pk, (comp, amount) in _component_earnings(info).items():
            if amount:
                slip.lines.append(Line(LT.EARNING, amount, comp.expense_account, comp.name_en, comp.name_ar,
                                       component=comp, quantity=Decimal(info.paid_days)))
        employed = {d for d, _ in info.days}
        unpaid = sorted(hr._unpaid_days(emp, info.start, info.end) & employed)
        if unpaid:
            weight = Decimal(_weight(info, unpaid))
            slip.lines.extend(_reduction_lines(LT.UNPAID_DEDUCTION,
                                               _by_component(info, unpaid, "deducted_for_unpaid_leave"),
                                               f"Unpaid leave {weight} days", f"إجازة بدون أجر {weight} يوم", weight))
        for inp in inputs:
            problems = _eg_input_problems(inp)
            if problems:
                slip.refuse(problems[0].split(" / ")[0], problems[0].split(" / ")[-1])
        for inp in inputs:  # never _input_lines: it reads Saudi overtime rows (R51)
            if inp.kind == IK.UNPAID_DAYS:
                parts = _by_component(info, [info.last], "deducted_for_unpaid_leave", Fraction(inp.quantity),
                                      weighted=False)
                slip.lines.extend(_reduction_lines(LT.UNPAID_DEDUCTION, parts, f"Unpaid absence {inp.quantity} days",
                                                   f"غياب بدون أجر {inp.quantity} يوم", inp.quantity, f"input:{inp.pk}"))
            elif inp.kind in (IK.ONE_OFF_EARNING, IK.BONUS):
                slip.lines.append(Line(LT.ONE_OFF_EARNING if inp.kind == IK.ONE_OFF_EARNING else LT.BONUS,
                                       _round(inp.amount), _expense(ctx, inp.component), inp.reason_en or inp.kind,
                                       inp.reason_ar, component=inp.component, source_ref=f"input:{inp.pk}"))
            elif inp.kind == IK.ONE_OFF_DEDUCTION:
                slip.lines.append(Line(LT.DEDUCTION, _round(inp.amount), _expense(ctx, inp.component),
                                       inp.reason_en or "Deduction", inp.reason_ar, component=inp.component,
                                       source_ref=f"input:{inp.pk}", basis=inp.deduction_basis))
        gross = slip.total(PayslipLine.EARNINGS) - slip.total(PayslipLine.REDUCTIONS)
        # Social insurance.
        full, wage = _eg_insurable_wage(ctx, slip, info)
        slip.gosi_wage, slip.gosi_wage_full = wage, full
        si = _eg(ctx, "si.rates")
        acc = {k: ctx.account(v) for k, v in EG_ACCOUNTS.items()}
        ee = _round(Fraction(wage) * Fraction(si["employee"]))
        er = _round(Fraction(wage) * Fraction(si["employer"]))
        slip.lines.append(Line(LT.SI_EMPLOYEE, ee, acc["si_payable"], f"Social insurance employee {si['employee']}",
                               f"التأمينات الاجتماعية حصة العامل {si['employee']}", quantity=wage,
                               rate=Decimal(si["employee"])))
        slip.lines.append(Line(LT.SI_EMPLOYER, er, acc["si_expense"], f"Social insurance employer {si['employer']}",
                               f"التأمينات الاجتماعية حصة صاحب العمل {si['employer']}", quantity=wage,
                               rate=Decimal(si["employer"])))
        # Universal Health Insurance (where the work governorate is live).
        live = _eg(ctx, "uhi.governorates_live")["governorates"]
        gov = (ctx.s.work_governorate or "").strip()
        if not gov:
            slip.warn("Work governorate not set in payroll settings: Universal Health Insurance not computed.",
                      "لم تُحدد محافظة مقر العمل: لم يُحتسب التأمين الصحي الشامل.")
        elif gov in live:
            uhi = _eg(ctx, "uhi.rates")
            uee = _round(Fraction(wage) * Fraction(uhi["employee"]))
            uer = max(_round(Fraction(wage) * Fraction(uhi["employer"])), Decimal(uhi["employer_min_monthly"]))
            slip.lines.append(Line(LT.UHI_EMPLOYEE, uee, acc["uhi_payable"], "Universal health insurance employee",
                                   "التأمين الصحي الشامل حصة العامل", quantity=wage, rate=Decimal(uhi["employee"])))
            slip.lines.append(Line(LT.UHI_EMPLOYER, uer, acc["uhi_expense"], "Universal health insurance employer",
                                   "التأمين الصحي الشامل حصة صاحب العمل", quantity=wage, rate=Decimal(uhi["employer"])))
            slip.warn("Family UHI contributions (non-working spouse, children) are not computed: dependants are not "
                      "recorded.", "لم تُحتسب اشتراكات التأمين الصحي للأسرة: لا توجد بيانات المعالين.")
        # Martyrs' Fund (employee-borne, withheld; who bears it is disputed, research C8).
        mf = _eg(ctx, "levy.martyrs_fund")
        martyrs = _round(Fraction(max(gross, ZERO)) * Fraction(mf["rate"]))
        if martyrs:
            slip.lines.append(Line(LT.MARTYRS_FUND, martyrs, acc["martyrs_payable"], "Martyrs' and Victims' Fund",
                                   "صندوق تكريم الشهداء والضحايا", quantity=max(gross, ZERO), rate=Decimal(mf["rate"])))
        # Salary tax.
        slip.taxable_income = max(gross - ee, ZERO)
        tax = _eg_salary_tax(ctx, slip, emp, slip.taxable_income)
        if tax:
            slip.lines.append(Line(LT.SALARY_TAX, tax, acc["tax_payable"], "Salary tax withheld",
                                   "ضريبة كسب العمل المستقطعة", quantity=slip.taxable_income))
        _eg_caps(ctx, slip, emp)
        if _eg(ctx, "eosb.monthly_accrual").get("accrue"):
            slip.refuse("An Egypt EOSB accrual is configured but not built.", "استحقاق نهاية الخدمة لمصر غير مبني.")
        net = slip.total(PayslipLine.EARNINGS) - slip.total(PayslipLine.REDUCTIONS) - slip.total(PayslipLine.DEDUCTIONS)
        if net < 0:
            slip.refuse(f"Net pay would be negative ({net}).", f"سيكون صافي الراتب سالبًا ({net}).")
    except _Refused:
        slip.lines = []
    return slip


# =====================================================================================================
# Slice 2 (§10.8 item 4): carried maternity days settled off-cycle; leave liability report (hr OQ-6 default)
# =====================================================================================================

def _carried_maternity_open(emp, case):
    """Periods whose pay was carried pending GOSI for ``case``'s leave (R61(b)) and not released yet: the carried
    reductions booked for the period less the releases booked for it since."""
    out = []
    req_ref = f"maternity_pending:{case.leave_request_id}"
    periods = sorted(set(PayslipLine.objects.filter(
        payslip__employee=emp, payslip__refused=False, payslip__run__status__in=(RS.APPROVED, RS.PAID),
        line_type=LT.MATERNITY_SUSPENDED, source_ref=req_ref).values_list("payslip__run__period", flat=True)))
    for q in periods:
        booked = _booked_lines(emp, q, PERIOD_TYPES)
        carried = sum((l.amount for l in booked if l.line_type == LT.MATERNITY_SUSPENDED and l.source_ref == req_ref),
                      ZERO)
        released = sum((l.amount for l in booked if l.line_type == LT.RETRO
                        and l.source_ref == f"release:{LT.MATERNITY_SUSPENDED}"), ZERO)
        top_up = sum((l.amount for l in booked if l.line_type == LT.MATERNITY_TOP_UP), ZERO)
        if case.status == MaternityGosiCase.Status.CONFIRMED:
            # Confirmed: the covered days stay suspended (GOSI pays them); what is due is the top-up (R61(a)).
            if not top_up and not released:
                out.append((q, carried))
        elif carried - released > 0:
            out.append((q, carried - released))
    return out


@transaction.atomic
def prepare_maternity_settlement(case, *, user):
    """R61(b) -> (a) / (c) (P20(d)): once the GOSI maternity case of an approved MATERNITY leave is confirmed or
    refused, the days of the months already paid that were carried pending GOSI are settled at once: one off-cycle run
    (``payroll.prepare_run``) per such month, for her only, calculated; it releases the carried pay (refused: full
    pay; confirmed: the top-up for the covered days) net of what the month booked. The regular run would otherwise
    release it in the next month's re-examination (R14). Returns the calculated runs."""
    case = MaternityGosiCase.objects.select_related("entity", "employee").get(pk=case.pk)
    _require(user, case.entity, "payroll.prepare_run")
    if case.status not in (MaternityGosiCase.Status.CONFIRMED, MaternityGosiCase.Status.REFUSED):
        raise error("The carried days are settled once GOSI has confirmed or refused the case (R61(b)).",
                    "تُسوى الأيام المرحّلة بعد أن تؤكد التأمينات الحالة أو ترفضها.", code="maternity_pending")
    runs = []
    for q, amount in _carried_maternity_open(case.employee, case):
        if PayrollRun.objects.filter(entity=case.entity, period=q, kind=PayrollRun.Kind.OFF_CYCLE,
                                     off_cycle_employees=case.employee,
                                     status__in=(RS.DRAFT, RS.CALCULATED, RS.SUBMITTED, RS.RETURNED)).exists():
            continue  # already prepared, waiting for its approval
        run = create_run(case.entity, user=user, period=q, kind=PayrollRun.Kind.OFF_CYCLE,
                         employees=[case.employee], reason_ar=f"تسوية أيام الأمومة المرحّلة ({amount})",
                         reason_en=f"Settlement of maternity days carried pending GOSI ({amount}) (R61)")
        runs.append(calculate_run(run, user=user))
    if not runs:
        raise error("Nothing carried pending GOSI is left to settle for this case.",
                    "لا يتبقى أجر مُرحّل بانتظار التأمينات لهذه الحالة.", code="maternity_nothing")
    return runs


def leave_liability_report(entity, as_of, *, user):
    """hr OQ-6 default (slice 2): unused annual leave is not accrued in the books (2350 / 5130 are approved only if
    leave is accrued monthly, a decision still open); this report gives the liability instead: per KSA employee, the
    annual leave balance x the actual daily wage (counts_for_eosb components / 30, Labour Law Art. 111 basis), as of
    ``as_of``. Totals for ``payroll.view_cost_reports``; per-employee rows (codes only) also need
    ``payroll.view_payslip`` (logged). Nothing is posted."""
    _require(user, entity, "payroll.view_cost_reports")
    detail = access.has(user, entity, "payroll.view_payslip")
    lt = hr.LeaveType.objects.filter(entity=entity, code="ANNUAL").first()
    rows, total = [], ZERO
    if lt is not None:
        for emp in Employee.objects.filter(entity=entity, employment_basis=Basis.KSA_EMPLOYEE,
                                           status=Employee.Status.ACTIVE).order_by("employee_code"):
            terms = _terms(emp, as_of)
            if terms is None:
                continue
            days = hr._balance(emp, lt)
            daily = Fraction(_sum(terms, "counts_for_eosb")) / 30
            value = _round(Fraction(days) * daily)
            total += value
            rows.append({"employee_code": emp.employee_code, "days": days, "value": value})
    if detail:
        access_services.log(AccessLog.Event.PERSONAL_DATA_VIEWED, actor=user, entity=entity,
                            permission="payroll.view_payslip",
                            details={"action": "leave_liability_report", "as_of": str(as_of)})
    return {"entity": entity.code, "as_of": as_of, "total": total, "posted": False,
            "rows": rows if detail else [], "employees": len(rows),
            "note_en": "Not booked: leave accrual (2350 / 5130) waits for hr OQ-6.",
            "note_ar": "غير مقيد: استحقاق الإجازات (2350 / 5130) بانتظار قرار OQ-6."}


# =====================================================================================================
# Slice 2 (§10.8 item 8): bai' ajel through the real invoicing port (invoicing R112, E28; E12, E13)
# =====================================================================================================

def _sale_posted(port, contract):
    """True once invoicing reports the sale invoice posted (``sale_state``); a port without it (slice 1 fakes) posts
    at once."""
    state_of = getattr(port, "sale_state", None)
    if state_of is None:
        return True
    state = state_of(contract)
    return bool(state is not None and state.posted)


def _collection_ref(inst):
    """The collection reference invoicing keys the profit recognition by (idempotent per installment row)."""
    return f"payroll.deferredsaleinstallment:{inst.pk}"


def _recognise_profit(contract, amount, on, ref, user):
    """E12 / E13 (invoicing R112, owner decision A6): the profit share of a collected installment moves from 1241 to
    4610 by collected share, never by time, posted by invoicing's bai' ajel system user after this approval."""
    port = invoicing_port.get_port()
    recognise = getattr(port, "recognise_bai_ajel_profit", None)
    if recognise is None:
        return None
    try:
        return recognise(contract, amount=amount, on=on, source_ref=ref, user=user)
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "posting_refused") from None


def _reverse_profit(contract, ref, user, on, reason):
    port = invoicing_port.get_port()
    reverse = getattr(port, "reverse_bai_ajel_profit", None)
    if reverse is None:
        return None
    try:
        return reverse(contract, source_ref=ref, user=user, on=on, reason=reason or "payroll run reversed")
    except LedgerError as exc:
        raise PayrollError(exc.messages, code=getattr(exc, "code", None) or "reversal_refused") from None


def _reissue_rejected_sale(c, user):
    """R47 / invoicing R112: an approved sale whose invoice ZATCA rejected is approved again: invoicing issues a new
    invoice (new number) for the same fixed price; the schedule is unchanged. Refused while the invoice is live."""
    port = invoicing_port.get_port()
    state = port.sale_state(c) if hasattr(port, "sale_state") else None
    if state is None or state.status != "rejected":
        raise error("The sale is approved and its invoice is waiting for ZATCA reporting; it is approved again only "
                    "after ZATCA rejects the invoice.", "البيع معتمد وفاتورته بانتظار الإبلاغ للهيئة؛ ولا يُعتمد ثانية "
                    "إلا بعد رفض الهيئة للفاتورة.", code="sale_status")
    result = port.create_bai_ajel_sale(c, user=user)
    if _dec(result.vat_amount, "vat_amount") != c.vat_amount:
        raise error("The new sale invoice's VAT differs from the approved sale.", "تختلف ضريبة الفاتورة الجديدة عن البيع "
                    "المعتمد.", code="vat_mismatch")
    c.sale_document_ref, c.modified_by, c.modified_at = result.document_ref, user, _now()
    if _sale_posted(port, c):
        c.status = DeferredSaleContract.Status.SOLD
    return _save(c)


def _refresh_deferred_sales(entity):
    """R47 as amended (slice 2): contracts whose sale invoice was posted on ZATCA reporting since become ``sold`` (their
    installments then enter the runs). Called before every run calculation and by the daily alerts job; reads
    invoicing through the port only. Returns the contracts marked sold."""
    port = invoicing_port.get_port()
    out = []
    for c in DeferredSaleContract._base_manager.select_for_update().filter(
            entity=entity, status=DeferredSaleContract.Status.SALE_INVOICED).order_by("pk"):
        state = port.sale_state(c) if hasattr(port, "sale_state") else None
        if state is not None and state.posted:
            c.status, c.sale_document_ref = DeferredSaleContract.Status.SOLD, state.document_ref
            out.append(_save(c))
    return out
