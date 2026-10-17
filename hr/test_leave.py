"""hr R29 to R34, R56 with the compliance corrections of 2026-10-17 (30-day rate from the day after the 5-year
anniversary; holidays extend and sick leave suspends annual leave; sick-leave year from the first sick day; unpaid
leave beyond 20 days suspends accrual; maternity, paternity, bereavement, Hajj and exam rules)
(AT-15, AT-16, AT-17, AT-18, AT-31)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from core import access
from hr import services
from hr.conftest import contract, employee, hired
from hr.models import HrError, LeaveLedger, LeaveType, SickLeaveDay

D = Decimal


def _approved_leave(w, emp, code, start, end, approver=None, **fields):
    req = services.request_leave(emp, code, user=w.h, start_date=start, end_date=end, **fields)
    return services.approve_leave(req, user=approver or w.h2)


def _with_opening(w, days, **fields):
    emp = employee(w, **fields)
    services.record_opening_leave_balance(emp, "ANNUAL", D(days), emp.hire_date, user=w.h)
    contract(w, emp)
    emp.refresh_from_db()
    return emp


def _accrual(emp, period):
    return LeaveLedger.objects.get(employee=emp, kind="accrual", period=period).days


# --- R29, R30 / AT-15 --------------------------------------------------------------------------------------

def test_at15_accrual_idempotent_and_five_year_rate_from_the_day_after(w):
    e = hired(w, hire_date=date(2025, 1, 1))
    services.accrue_leave(2027, 1)
    services.accrue_leave(2027, 1)
    assert LeaveLedger.objects.filter(employee=e, kind="accrual", period="2027-01").count() == 1
    assert _accrual(e, "2027-01") == D("1.75")  # 21 / 12
    five = hired(w, hire_date=date(2022, 1, 1))
    services.accrue_leave(2027, 1)
    services.accrue_leave(2027, 2)
    # Anniversary 2027-01-01; the 30-day rate applies from 2027-01-02 (Art. 109): (21×1 + 30×30) / (12×31).
    assert _accrual(five, "2027-01") == D("2.48")
    assert _accrual(five, "2027-02") == D("2.50")
    row = LeaveLedger.objects.get(employee=e, period="2027-01")
    row.days = D("9")
    with pytest.raises(HrError):
        row.save()
    with pytest.raises(HrError):
        row.delete()
    with pytest.raises(HrError):
        LeaveLedger.objects.filter(pk=row.pk).update(days=D("9"))


def test_at15_adjustment_needs_approval(w):
    e = hired(w, hire_date=date(2025, 1, 1))
    adj = services.request_leave_adjustment(e, "ANNUAL", "2", date(2027, 3, 1), user=w.h, reason_ar="تصحيح")
    assert services.leave_balance(e, "ANNUAL", user=w.h) == 0
    with pytest.raises(access.AccessDenied):
        services.approve_leave_adjustment(adj, user=w.h)
    services.approve_leave_adjustment(adj, user=w.c)
    assert services.leave_balance(e, "ANNUAL", user=w.h) == D("2")


def test_r30_partial_first_month_is_pro_rata(w):
    e = hired(w, hire_date=date(2027, 1, 17))  # 15 of 31 days
    services.accrue_leave(2027, 1)
    assert _accrual(e, "2027-01") == D("0.85")  # 21 × 15 / (12 × 31) = 0.8467


def test_r30_unpaid_leave_beyond_twenty_days_suspends_accrual(w):
    e = hired(w, hire_date=date(2025, 1, 1))
    _approved_leave(w, e, "UNPAID", date(2027, 2, 1), date(2027, 2, 28), reason_ar="ظرف خاص")
    services.accrue_leave(2027, 2)
    assert _accrual(e, "2027-02") == D("1.25")  # 20 of 28 days accrue: 21 × 20 / (12 × 28)
    assert services.unpaid_leave_days(e, date(2027, 2, 1), date(2027, 2, 28), user=w.p) == 28


def test_r30_remuneration_and_local_staff_get_no_accrual(w):
    r = hired(w, basis="remuneration")
    local = employee(w, w.egy, basis="local_entity_employee")
    report = services.accrue_leave(2027, 2)
    assert not LeaveLedger.objects.filter(employee__in=[r, local]).exists()
    assert report["accrued"] == 0


# --- R56 / AT-31 -------------------------------------------------------------------------------------------

def test_at31_ceiling_thirty(w):
    twenty_one = hired(w, hire_date=date(2022, 1, 1))
    thirty = hired(w, hire_date=date(2022, 1, 1), contract_kwargs={"annual_leave_days": 30})
    services.accrue_leave(2027, 2)
    assert _accrual(twenty_one, "2027-02") == D("2.50") and _accrual(thirty, "2027-02") == D("2.50")
    young_thirty = hired(w, hire_date=date(2025, 1, 1), contract_kwargs={"annual_leave_days": 30})
    services.accrue_leave(2027, 3)
    assert _accrual(young_thirty, "2027-03") == D("2.50")
    adj = services.request_leave_adjustment(thirty, "ANNUAL", "25.50", date(2027, 3, 1), user=w.h, reason_ar="تصحيح")
    with pytest.raises(HrError) as exc:
        services.approve_leave_adjustment(adj, user=w.c)
    assert exc.value.code == "annual_ceiling"
    ok = services.request_leave_adjustment(thirty, "ANNUAL", "25.00", date(2027, 3, 1), user=w.h, reason_ar="تصحيح")
    assert services.approve_leave_adjustment(ok, user=w.c).status == "approved"


# --- R31 / AT-16 --------------------------------------------------------------------------------------------

def test_at16_balance_overlap_and_segregation(w):
    e = _with_opening(w, "5")
    with pytest.raises(HrError) as exc:
        services.request_leave(e, "ANNUAL", user=w.h, start_date=date(2027, 6, 10), end_date=date(2027, 6, 15))
    assert "balance" in exc.value.messages[0]
    req = services.request_leave(e, "ANNUAL", user=w.h, start_date=date(2027, 6, 10), end_date=date(2027, 6, 14))
    assert req.days == 5
    with pytest.raises(HrError):
        services.request_leave(e, "UNPAID", user=w.h, start_date=date(2027, 6, 12), end_date=date(2027, 6, 13))
    with pytest.raises(access.AccessDenied) as denied:
        services.approve_leave(req, user=w.h)
    assert denied.value.code == "self_approval"
    services.approve_leave(req, user=w.h2)
    assert services.leave_balance(e, "ANNUAL", user=w.h) == 0
    assert LeaveLedger.objects.get(employee=e, kind="taken").days == D("-5")


def test_r31_approver_never_approves_own_leave(w):
    own = _with_opening(w, "5", linked_user=w.h2)
    req = services.request_leave(own, "ANNUAL", user=w.h, start_date=date(2027, 6, 10), end_date=date(2027, 6, 11))
    with pytest.raises(access.AccessDenied):
        services.approve_leave(req, user=w.h2)


def test_r31_cancellation_returns_unused_days(w):
    e = _with_opening(w, "5")
    req = _approved_leave(w, e, "ANNUAL", date(2027, 7, 1), date(2027, 7, 3))
    assert services.leave_balance(e, "ANNUAL", user=w.h) == 2
    services.cancel_leave(req, user=w.h)
    assert services.leave_balance(e, "ANNUAL", user=w.h) == 5


def test_regs_art24_official_holiday_inside_annual_leave_extends_it(w):
    e = _with_opening(w, "10")
    req = services.request_leave(e, "ANNUAL", user=w.h, start_date=date(2027, 9, 20), end_date=date(2027, 9, 26))
    assert req.days == 6  # 23 September (National Day) is not deducted


def test_regs_art26_sick_leave_suspends_annual_leave(w):
    e = _with_opening(w, "10")
    _approved_leave(w, e, "ANNUAL", date(2027, 7, 1), date(2027, 7, 10))
    assert services.leave_balance(e, "ANNUAL", user=w.h) == 0
    _approved_leave(w, e, "SICK", date(2027, 7, 5), date(2027, 7, 6), document_ref="MED-1")
    assert services.leave_balance(e, "ANNUAL", user=w.h) == 2


# --- R32 / AT-17 ---------------------------------------------------------------------------------------------

def test_at17_sick_tiers(w):
    e = hired(w)
    with pytest.raises(HrError):  # a document reference is required
        services.request_leave(e, "SICK", user=w.h, start_date=date(2027, 2, 1), end_date=date(2027, 2, 2))
    _approved_leave(w, e, "SICK", date(2027, 2, 1), date(2027, 2, 25), document_ref="MED-1")  # 25 days
    req = _approved_leave(w, e, "SICK", date(2027, 3, 1), date(2027, 4, 9), document_ref="MED-2")  # 40 days
    days = list(SickLeaveDay.objects.filter(leave_request=req).order_by("date"))
    assert len(days) == 40
    assert [d.pay_pct for d in days[:5]] == [D("100")] * 5 and {d.pay_pct for d in days[5:]} == {D("75")}
    assert {d.cycle_start for d in days} == {date(2027, 2, 1)}
    with pytest.raises(HrError) as exc:
        services.request_leave(e, "SICK", user=w.h, start_date=date(2027, 4, 20), end_date=date(2027, 7, 28),
                               document_ref="MED-3")
    assert "unpaid" in exc.value.messages[0]


def test_r32_sick_year_starts_at_the_first_sick_day(w):
    e = hired(w)
    _approved_leave(w, e, "SICK", date(2027, 1, 4), date(2027, 1, 8), document_ref="MED-1")
    late = _approved_leave(w, e, "SICK", date(2028, 1, 4), date(2028, 1, 5), document_ref="MED-2")
    days = list(SickLeaveDay.objects.filter(leave_request=late))
    assert {d.cycle_start for d in days} == {date(2028, 1, 4)} and {d.tier_no for d in days} == {1}


# --- R33 / AT-18 ---------------------------------------------------------------------------------------------

HAJJ = dict(declarations={"hajj_not_performed_before": True})


def test_at18_gender_service_and_once_per_service(w):
    male = hired(w)
    with pytest.raises(HrError):
        services.request_leave(male, "MATERNITY", user=w.h, start_date=date(2027, 6, 20), end_date=date(2027, 9, 11),
                               event_date=date(2027, 7, 15), document_ref="BIRTH-1")
    young = hired(w, gender="female", hire_date=date(2026, 6, 1))
    with pytest.raises(HrError) as exc:
        services.request_leave(young, "HAJJ", user=w.h, start_date=date(2027, 6, 10), end_date=date(2027, 6, 19), **HAJJ)
    assert "years of service" in exc.value.messages[0]
    senior = hired(w, hire_date=date(2024, 1, 1))
    _approved_leave(w, senior, "HAJJ", date(2027, 6, 10), date(2027, 6, 19), **HAJJ)
    with pytest.raises(HrError) as exc:
        services.request_leave(senior, "HAJJ", user=w.h, start_date=date(2027, 8, 10), end_date=date(2027, 8, 19), **HAJJ)
    assert "once" in exc.value.messages[0]
    with pytest.raises(HrError):  # no declaration that Hajj was not performed before
        services.request_leave(hired(w, hire_date=date(2024, 1, 1)), "HAJJ", user=w.h, start_date=date(2027, 6, 10),
                               end_date=date(2027, 6, 19))
    with pytest.raises(HrError):  # more than 15 days
        services.request_leave(hired(w, hire_date=date(2024, 1, 1)), "HAJJ", user=w.h, start_date=date(2027, 6, 10),
                               end_date=date(2027, 6, 30), **HAJJ)


def test_art151_maternity_twelve_weeks_with_mandatory_post_birth(w):
    f = hired(w, gender="female")
    base = dict(event_date=date(2027, 7, 15), document_ref="BIRTH-1")
    with pytest.raises(HrError):  # starts more than 4 weeks before the expected date
        services.request_leave(f, "MATERNITY", user=w.h, start_date=date(2027, 6, 10), end_date=date(2027, 9, 1), **base)
    with pytest.raises(HrError):  # ends inside the 6 mandatory weeks after birth
        services.request_leave(f, "MATERNITY", user=w.h, start_date=date(2027, 7, 1), end_date=date(2027, 8, 10), **base)
    req = _approved_leave(w, f, "MATERNITY", date(2027, 6, 20), date(2027, 9, 11), **base)
    assert req.days == 84 and not req.unpaid
    assert LeaveType.objects.get(entity=w.alm, code="MATERNITY").paid_rule == "full"
    extra = _approved_leave(w, f, "MATERNITY_UNPAID", date(2027, 9, 12), date(2027, 10, 11), **base)
    assert extra.unpaid and extra.days == 30


def test_art113_paternity_window_and_sibling_bereavement(w):
    m = hired(w)
    with pytest.raises(HrError):
        services.request_leave(m, "PATERNITY", user=w.h, start_date=date(2027, 6, 15), end_date=date(2027, 6, 17),
                               event_date=date(2027, 6, 10))
    assert _approved_leave(w, m, "PATERNITY", date(2027, 6, 11), date(2027, 6, 13), event_date=date(2027, 6, 10)).days == 3
    with pytest.raises(HrError):
        services.request_leave(m, "BEREAVEMENT_SIBLING", user=w.h, start_date=date(2027, 7, 1),
                               end_date=date(2027, 7, 4), event_date=date(2027, 7, 1))
    with pytest.raises(HrError):  # counted from the event, not before it
        services.request_leave(m, "MARRIAGE", user=w.h, start_date=date(2027, 7, 1), end_date=date(2027, 7, 3),
                               event_date=date(2027, 7, 2))
    assert _approved_leave(w, m, "BEREAVEMENT_SIBLING", date(2027, 7, 1), date(2027, 7, 3),
                           event_date=date(2027, 7, 1)).days == 3


def test_art115_exam_leave(w):
    e = hired(w)
    with pytest.raises(HrError):  # study not approved by the employer
        services.request_leave(e, "EXAM", user=w.h, start_date=date(2027, 7, 1), end_date=date(2027, 7, 2),
                               document_ref="EXAM-1")
    with pytest.raises(HrError):  # less than 15 days ahead
        services.request_leave(e, "EXAM", user=w.h, start_date=date(2027, 6, 10), end_date=date(2027, 6, 11),
                               document_ref="EXAM-1", declarations={"employer_approved_study": True})
    req = services.request_leave(e, "EXAM", user=w.h, start_date=date(2027, 7, 1), end_date=date(2027, 7, 2),
                                 document_ref="EXAM-1",
                                 declarations={"employer_approved_study": True, "repeated_year": True})
    assert req.unpaid


# --- R52, R51 ------------------------------------------------------------------------------------------------

def test_r52_remuneration_staff_have_no_leave(w):
    r = hired(w, basis="remuneration")
    for code in ("ANNUAL", "SICK", "UNPAID"):
        with pytest.raises(HrError) as exc:
            services.request_leave(r, code, user=w.h, start_date=date(2027, 6, 10), end_date=date(2027, 6, 11))
        assert exc.value.code == "remuneration_no_leave"
    with pytest.raises(HrError):
        services.request_leave_adjustment(r, "ANNUAL", "1", date(2027, 6, 1), user=w.h, reason_ar="س")


def test_r51_local_employee_leave_refused_until_parameters(w):
    local = employee(w, w.egy, basis="local_entity_employee")
    local.status = "active"
    local._service_write = True
    local.save()
    with pytest.raises(HrError) as exc:
        services.request_leave(local, "UNPAID", user=w.he, start_date=date(2027, 6, 10), end_date=date(2027, 6, 11))
    assert exc.value.code in ("jurisdiction_not_configured", "leave_invalid")


def test_r29_opening_only_once_before_activation(w):
    e = _with_opening(w, "3")
    with pytest.raises(HrError):
        services.record_opening_leave_balance(e, "ANNUAL", D("1"), e.hire_date + timedelta(days=1), user=w.h)
