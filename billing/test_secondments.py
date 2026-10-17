"""Secondments and the HR interface (billing R19, R49, R57 to R62, R65; AT-7, AT-31, AT-42, AT-43, AT-49, AT-51).

HR is the real ``hr`` app: employees, contracts and assignments are made through ``hr.services`` and its events
reach billing through ``HR_BILLING_PORT = billing.hr_port.BillingHrPort`` (no fake on the HR side here).
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from billing import services
from billing.conftest import approve_note, clear, period, run
from billing.hr_port import BillingHrPort
from billing.models import BillingAdjustment, BillingError
from core import access
from core.access.models import AccessLog
from core.access.testing import make_user, member
from hr import services as hr
from hr.conftest import hired
from hr.models import Assignment, AssignmentEvent
from invoicing import services as inv
from invoicing.conftest import ADDRESS, make_entity
from ledger.models import JournalLine

D = Decimal


@pytest.fixture
def hw(w, monkeypatch):
    """HR on top of the billing world: ALM and SUB set up for HR, HR officers, a department and a grade."""
    monkeypatch.setattr(hr, "_today", lambda: w.clock.day)
    for e in (w.alm, w.sub):
        hr.setup_entity(e)
    hs = make_user("hs")
    member(hs, w.sub, "hr_officer")
    dummy = SimpleNamespace(pk=-1)
    ns = SimpleNamespace(alm=w.alm, sub=w.sub, egy=dummy, h=w.h, hs=hs, he=None, c=w.c, c2=w.c2, ce=None)
    ns.fin = hr.create_department(w.alm, user=w.h, code="FIN", name_en="Finance", name_ar="المالية")
    ns.staff = hr.create_grade(w.alm, user=w.h, code="G-STAFF", name_en="Staff", name_ar="موظف",
                               overtime_eligible=True)
    ns.sub_dept = hr.create_department(w.sub, user=hs, code="OPS", name_en="Operations", name_ar="العمليات")
    ns.sub_grade = hr.create_grade(w.sub, user=hs, code="G-STAFF", name_en="Staff", name_ar="موظف",
                                   overtime_eligible=True)
    return ns


def hr_secondment(w, hw, *, start=date(2027, 1, 1), receiving=None):
    w.clock.set(min(w.clock.day, start))
    emp = hired(hw, w.alm, hire_date=date(2026, 12, 1))
    a = hr.create_assignment(emp, user=w.h, kind="secondment", receiving_entity=receiving or w.sub,
                             role_title_en="Analyst", role_title_ar="محلل", start_date=start)
    a = hr.submit_assignment(a, user=w.h)
    return hr.approve_assignment(a, user=w.c), emp


def secondment_contract(w, *, amount="31000.00", timing="arrears", start=date(2027, 1, 1), customer=None,
                        hr_ref=None, end=None, approve=True):
    c = services.create_contract(
        w.alm, user=w.s, customer=customer or w.ic_customer, contract_type="secondment", currency="SAR",
        start_date=start, end_date=None, billing_frequency="monthly", billing_timing=timing,
        title_ar="إعارة موظف", title_en="Staff secondment", description_ar="إعارة محلل إلى الشركة التابعة")
    a = services.add_secondment(c, user=w.s, employee_ref="EMP-T-0001", role_title_ar="محلل", role_title_en="Analyst",
                                start_date=start, end_date=end)
    if hr_ref:
        a = services.link_hr_assignment(a, hr_ref, user=w.s)
    services.add_line(c, user=w.s, income_type="secondment", amount=amount, description_ar="إعارة محلل",
                      description_en="Seconded analyst", secondment=a)
    if approve:
        c = services.approve_contract(c, user=w.c)
    return c, a


def _adjs(c):
    return list(BillingAdjustment.objects.filter(contract=c).order_by("billing_period__period_start", "pk"))


# --- AT-7, AT-51, R19, R65 --------------------------------------------------------------------------------

def test_at7_at51_unlinked_end_prorates_march_cancels_after(w):
    c, a = secondment_contract(w)
    for m in (2, 3):
        run(w, date(2027, m, 1))
    w.clock.set(date(2027, 3, 12))
    services.end_secondment(w.alm, a, date(2027, 3, 10), w.s, source="manual")
    a.refresh_from_db()
    assert (a.end_date, a.end_source, a.end_recorded_by) == (date(2027, 3, 10), "manual", w.s)
    assert set(c.periods.filter(period_start__gte=date(2027, 4, 1)).values_list("status", flat=True)) == {"cancelled"}
    run(w, date(2027, 4, 1))
    march = period(c, date(2027, 3, 1))
    doc = clear(w, march.sales_document)
    assert (doc.total_net, doc.total_vat, doc.is_intercompany) == (D("10000.00"), D("1500.00"), True)
    line = doc.lines.get()
    assert (line.vat_category, line.income_account.code) == ("S", "4210")  # AT-51: the contract price, no payroll
    lines = sorted((ln.account.code, ln.debit, ln.credit, getattr(ln.counterparty_entity, "code", None))
                   for ln in doc.journal_entry.lines.all())
    assert lines == [("1210", D("11500.00"), D("0.00"), "SUB"), ("2200", D("0.00"), D("1500.00"), None),
                     ("4210", D("0.00"), D("10000.00"), None)]  # B3
    assert run(w, date(2027, 5, 1)).generated_count == 0
    assert not JournalLine.objects.filter(entry__entity=w.sub).exists()  # nothing in SUB (R4)


def test_at31_end_before_billed_days_credit_then_later_end_debit(w):
    c, a = secondment_contract(w, amount="30000.00", timing="advance", start=date(2027, 4, 1))
    run(w, date(2027, 4, 1))
    clear(w, period(c, date(2027, 4, 1)).sales_document)
    w.clock.set(date(2027, 4, 12))
    services.end_secondment(w.alm, a, date(2027, 4, 10), w.s)
    (credit,) = _adjs(c)
    assert (credit.kind, credit.status, credit.lines[0]["delta"]) == ("credit", "drafted", "-20000.00")
    assert credit.document.status == "draft"  # not issued until reviewed and approved
    assert (credit.supply_start, credit.supply_end) == (date(2027, 4, 11), date(2027, 4, 30))
    assert set(c.periods.filter(period_start__gte=date(2027, 5, 1)).values_list("status", flat=True)) == {"cancelled"}
    events = c.events.count()
    services.end_secondment(w.alm, a, date(2027, 4, 10), w.s)  # same date: no-op
    assert c.events.count() == events and len(_adjs(c)) == 1
    approve_note(w, credit.document, day=date(2027, 4, 13))
    services.end_secondment(w.alm, a, date(2027, 4, 20), w.s)
    debit = _adjs(c)[-1]
    assert (debit.kind, debit.lines[0]["delta"]) == ("debit", "10000.00")
    assert (debit.supply_start, debit.supply_end) == (date(2027, 4, 11), date(2027, 4, 20))
    assert period(c, date(2027, 5, 1)).status == "cancelled"


def test_r60_end_date_limits_and_draft_contract(w):
    c, a = secondment_contract(w, approve=False)
    with pytest.raises(BillingError):
        services.end_secondment(w.alm, a, date(2026, 12, 30), w.s)  # before start - 1
    services.end_secondment(w.alm, a, date(2026, 12, 31), w.s)  # start - 1: nothing billable (HR cancellation)
    a.refresh_from_db()
    assert a.end_date == date(2026, 12, 31) and not c.periods.exists()  # draft: only the date is stored
    c = services.approve_contract(c, user=w.c)
    assert not c.periods.exclude(status="cancelled").exists()
    with pytest.raises(BillingError):
        services.end_secondment(w.sub, a, date(2027, 1, 5), w.s)  # another entity


def test_view_secondment_masks_employee_ref(w):
    c, a = secondment_contract(w)
    assert services.view_secondment(a, user=w.s)["employee_ref"] == "EMP-T-0001"
    assert AccessLog.objects.filter(event="personal_data_viewed", actor=w.s).exists()
    assert services.view_secondment(a, user=w.v)["employee_ref"] == "••••"
    with pytest.raises(access.AccessDenied):
        services.view_secondment(a, user=w.sub_s)


# --- AT-42, R57 to R60: HR is the source for linked secondments -----------------------------------------------

def test_at42_hr_end_delivered_once_manual_end_refused(w, hw, agent, django_capture_on_commit_callbacks):
    hr_a, emp = hr_secondment(w, hw)
    assert hr_a.reference == "ALM-ASG-2027-000001"
    c, a = secondment_contract(w, timing="advance", hr_ref=hr_a.reference)
    for m in (1, 2, 3):
        run(w, date(2027, m, 1))
        clear(w, period(c, date(2027, m, 1)).sales_document)
    w.clock.set(date(2027, 3, 12))
    with django_capture_on_commit_callbacks(execute=True):
        hr.end_assignment(hr_a, date(2027, 3, 10), user=w.h, reason_ar="انتهاء المهمة")
    event = AssignmentEvent.objects.get(assignment=hr_a)
    assert (event.delivery_status, event.billing_assignment_id) == ("delivered", str(a.pk))
    a.refresh_from_db()
    assert (a.end_date, a.end_source, a.last_hr_end_date, a.end_recorded_by) == (
        date(2027, 3, 10), "hr_event", date(2027, 3, 10), w.h)
    (credit,) = _adjs(c)
    assert credit.billing_period.period_start == date(2027, 3, 1) and credit.lines[0]["delta"] == "-21000.00"
    assert AccessLog.objects.filter(actor=w.h, details__action="end_secondment",
                                    details__hr_assignment_ref=hr_a.reference).exists()
    assert hr.deliver_assignment_events() == 0  # the job again: nothing pending
    BillingHrPort().end_secondment(w.alm, a, date(2027, 3, 10), w.h, "hr_event")  # re-delivery: no-op
    assert len(_adjs(c)) == 1
    with pytest.raises(BillingError) as exc:
        services.end_secondment(w.alm, a, date(2027, 3, 5), w.s, source="manual")
    assert exc.value.code == "linked_to_hr" and "ended in HR" in str(exc.value) and "الموارد البشرية" in str(exc.value)
    with pytest.raises(BillingError):  # whatever the role: the CFO cannot either
        services.end_secondment(w.alm, a, date(2027, 3, 5), w.c, source="manual")
    with pytest.raises(access.AccessDenied):
        services.end_secondment(w.alm, a, date(2027, 3, 5), agent, source="manual")
    with pytest.raises(access.AccessDenied):
        services.end_secondment(w.alm, a, date(2027, 3, 5), w.sys, source="hr_event")
    with pytest.raises(access.AccessDenied):  # an hr_event from someone without hr.end_assignment
        BillingHrPort().end_secondment(w.alm, a, date(2027, 3, 5), w.s, "hr_event")
    a.refresh_from_db()
    assert a.end_date == date(2027, 3, 10)  # every refusal left it unchanged
    # the HR event of an actor without the right stays pending with the error (hr R25)
    bad = AssignmentEvent.objects.create(entity=w.alm, assignment=hr_a, kind="end_changed",
                                         end_date=date(2027, 3, 20), previous_end_date=date(2027, 3, 10), actor=w.s)
    hr.deliver_assignment_events(event_ids=[bad.pk])
    bad.refresh_from_db()
    assert bad.delivery_status == "pending" and bad.attempts == 1 and bad.last_error_en
    # an unlinked secondment is still ended manually by accounting staff (D6)
    other, b = secondment_contract(w)
    services.end_secondment(w.alm, b, date(2027, 3, 31), w.s, source="manual")
    b.refresh_from_db()
    assert b.end_source == "manual"


def test_at43_hr_cancellation_withdrawn_termination_and_reopen(w, hw, django_capture_on_commit_callbacks):
    w.clock.set(date(2027, 3, 1))
    hr_a, _ = hr_secondment(w, hw, start=date(2027, 4, 1))
    assert hr_a.status == "approved"
    c, a = secondment_contract(w, timing="advance", start=date(2027, 4, 1), hr_ref=hr_a.reference)
    with django_capture_on_commit_callbacks(execute=True):
        hr.cancel_assignment(hr_a, user=w.h, reason_ar="ألغيت قبل البدء")
    a.refresh_from_db()
    assert a.end_date == date(2027, 3, 31)
    assert not c.periods.exclude(status="cancelled").exists() and not _adjs(c)  # (a)

    hr_b, _ = hr_secondment(w, hw, start=date(2027, 4, 1))
    c2, b = secondment_contract(w, amount="30000.00", timing="advance", start=date(2027, 4, 1), hr_ref=hr_b.reference)
    run(w, date(2027, 4, 1))
    clear(w, period(c2, date(2027, 4, 1)).sales_document)
    w.clock.set(date(2027, 4, 12))
    with django_capture_on_commit_callbacks(execute=True):
        hr.end_assignment(hr_b, date(2027, 4, 10), user=w.h, reason_ar="انتهاء")
    (credit,) = _adjs(c2)
    approve_note(w, credit.document, day=date(2027, 4, 13))
    with django_capture_on_commit_callbacks(execute=True):  # (b) termination withdrawn: back to 2027-06-30
        hr.end_assignment(hr_b, date(2027, 6, 30), user=w.h, reason_ar="سحب الإنهاء")
    b.refresh_from_db()
    assert b.end_date == date(2027, 6, 30)
    assert [period(c2, date(2027, m, 1)).status for m in (5, 6)] == ["planned", "planned"]
    debit = _adjs(c2)[-1]
    assert (debit.kind, debit.lines[0]["delta"], debit.supply_start, debit.supply_end) == (
        "debit", "20000.00", date(2027, 4, 11), date(2027, 4, 30))

    # (c) HR removes the end date and sends nothing; while HR still shows an end, reopening is refused
    with pytest.raises(BillingError) as exc:
        services.reopen_secondment(b, user=w.s, reason_ar="أعيد فتحها")
    assert exc.value.code == "hr_still_ended"
    Assignment._base_manager.filter(pk=hr_b.pk).update(status="active", end_date=None)
    with pytest.raises(BillingError):
        services.reopen_secondment(b, user=w.s, reason_ar="")
    services.reopen_secondment(b, user=w.s, reason_ar="أعيدت الإعارة في الموارد البشرية")
    b.refresh_from_db()
    assert b.end_date is None
    last = c2.periods.filter(status="planned").order_by("-period_start").first()
    assert (last.period_start, last.period_end) == (date(2028, 5, 1), date(2028, 5, 31))  # 13 months rolling again
    unlinked, u = secondment_contract(w)
    with pytest.raises(BillingError):
        services.reopen_secondment(u, user=w.s, reason_ar="س")


# --- AT-49, R62: linking -------------------------------------------------------------------------------------

def test_at49_link_checks_receiver_copies_billing_view_set_once(w, hw):
    part = make_entity("PART", "full", w.alm, vat="317777777700003", cr="1010000006")
    part_customer = inv.create_customer(w.alm, user=w.s, code="IC-PART", name_ar="شركة PART", is_vat_registered=True,
                                        vat_number="317777777700003", group_entity=part, **ADDRESS)
    hr_a, emp = hr_secondment(w, hw)
    wrong, wa = secondment_contract(w, customer=part_customer, approve=False)
    with pytest.raises(BillingError) as exc:
        services.link_hr_assignment(wa, hr_a.reference, user=w.s)
    assert exc.value.code == "receiving_entity"
    with pytest.raises(BillingError):
        services.link_hr_assignment(wa, "ALM-ASG-2027-999999", user=w.s)
    c, a = secondment_contract(w, approve=False)
    a = services.link_hr_assignment(a, hr_a.reference, user=w.s)
    assert (a.hr_assignment_ref, a.employee_ref, a.role_title_ar, a.start_date) == (
        hr_a.reference, emp.employee_code, "محلل", date(2027, 1, 1))
    stored = {f.name: getattr(a, f.name) for f in a._meta.fields}
    assert emp.full_name_ar not in [str(v) for v in stored.values()]  # no name or ID number stored
    with pytest.raises(BillingError) as exc:
        services.link_hr_assignment(a, hr_a.reference, user=w.s)
    assert exc.value.code == "already_linked"
    services.add_line(c, user=w.s, income_type="secondment", amount="31000.00", description_ar="إعارة", secondment=a)
    services.approve_contract(c, user=w.c)
    with pytest.raises(BillingError):
        services.link_hr_assignment(a, "ALM-ASG-2027-000002", user=w.s)
