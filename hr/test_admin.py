"""hr R2, R10 in the admin: scoped, read-only, personal data never a form field and masked or logged."""
from django.contrib import admin
from django.test import RequestFactory

from core.access.models import AccessLog
from hr.conftest import hired
from hr.models import Employee


def _request(user):
    request = RequestFactory().get("/admin/hr/employee/")
    request.user = user
    return request


def test_employee_admin_scoped_read_only_and_masked(w):
    e1, e2 = hired(w), hired(w, w.sub)
    model_admin = admin.site._registry[Employee]
    assert list(model_admin.get_queryset(_request(w.a))) == [e1]
    assert not model_admin.has_change_permission(_request(w.h), e1) and not model_admin.has_add_permission(_request(w.h))
    assert not model_admin.has_delete_permission(_request(w.h), e1)
    assert set(Employee.PERSONAL_FIELDS) <= set(model_admin.exclude)
    field = model_admin.get_readonly_fields(_request(w.a), e1)[-1]
    assert "Test Person" not in field(e1) and "***" in field(e1)
    before = AccessLog.objects.filter(event="personal_data_viewed").count()
    field = model_admin.get_readonly_fields(_request(w.h), e1)[-1]
    assert "Test Person" in field(e1)
    assert AccessLog.objects.filter(event="personal_data_viewed").count() == before + 1
    assert e2 not in model_admin.get_queryset(_request(w.h))


# --- Finding 7: sensitive details are left out unless the user may see personal data ------------------------------

def _fields(model, user, obj):
    model_admin = admin.site._registry[model]
    request = _request(user)
    names = [f for f in model_admin.get_fields(request, obj) if isinstance(f, str)]
    return model_admin, request, names


def test_admin_hides_health_family_disciplinary_and_pay_details_without_the_right(w):
    from datetime import date
    from decimal import Decimal

    from hr import services
    from hr.models import EosbCalculation, FinalSettlementInput, LeaveRequest, Termination

    emp = hired(w, gender="female", hire_date=date(2025, 1, 1))
    req = services.request_leave(emp, "SICK", user=w.h, start_date=date(2027, 6, 2), end_date=date(2027, 6, 3),
                                 document_ref="MED-77", declarations={"pregnancy_related": True})
    w.payroll.balances[emp.pk] = services.payroll_port.LoanBalances(loans=Decimal("1000.00"))
    t = services.record_termination(emp, user=w.h, kind="art80_dismissal", last_working_day=date(2027, 6, 30),
                                    article_paragraph="1", hearing_ref="HEARING-9", reason_ar="سبب")
    t = services.approve_termination(services.submit_termination(t, user=w.h), user=w.c, acknowledge_protection=True)
    cases = ((LeaveRequest, req, {"document_ref"}), (Termination, t, {"hearing_ref", "marriage_contract_date"}),
             (EosbCalculation, EosbCalculation.objects.get(termination=t), {"wage_components", "wage_basis"}),
             (FinalSettlementInput, FinalSettlementInput.objects.get(termination=t), {"outstanding_loans"}))
    for model, obj, hidden in cases:
        _, _, names = _fields(model, w.a, obj)
        assert not hidden & set(names), (model, names)
        _, _, names = _fields(model, w.h, obj)
        assert hidden <= set(names), (model, names)
    model_admin, request, _ = _fields(LeaveRequest, w.a, req)
    assert "declarations" not in model_admin.get_fields(request, req)
    shown = model_admin.get_readonly_fields(request, req)[-1]
    assert "pregnancy" not in shown(req)
    model_admin, request, _ = _fields(LeaveRequest, w.h, req)
    assert "pregnancy_related" in model_admin.get_readonly_fields(request, req)[-1](req)
    before = AccessLog.objects.filter(event="personal_data_viewed").count()
    model_admin.get_object(request, str(req.pk))
    assert AccessLog.objects.filter(event="personal_data_viewed").count() == before + 1
