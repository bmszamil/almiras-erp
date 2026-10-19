"""Payroll review items 7 and 8 on the HR side: IBAN changes of an active employee need a second person's approval
and are logged (last four characters only); names that a spreadsheet would read as a formula are refused."""
import pytest
from django.core.exceptions import PermissionDenied

from core.access.models import AccessLog
from hr import services
from hr.conftest import employee, hired
from hr.models import Employee, HrError, IbanChangeRequest

OLD = "SA0380000000608010167519"
NEW = "GB82WEST12345698765432"


def test_review8_active_employee_iban_changes_only_with_a_second_persons_approval(w):
    emp = hired(w, iban=OLD, bank_name="Al Rajhi")
    with pytest.raises(HrError) as exc:
        services.update_employee(emp, user=w.h, iban=NEW)
    assert exc.value.code == "iban_change_needs_approval" and " / " in exc.value.messages[0]
    with pytest.raises(HrError):
        services.request_iban_change(emp, user=w.h, iban="SA0000000000000000000000", reason_ar="تغيير البنك")
    req = services.request_iban_change(emp, user=w.h, iban=NEW, bank_name="West Bank", reason_ar="تغيير البنك")
    assert Employee.objects.get(pk=emp.pk).iban == OLD  # nothing changes before the approval
    with pytest.raises(HrError):
        services.request_iban_change(emp, user=w.h2, iban=NEW, reason_ar="مرة أخرى")  # one pending request
    with pytest.raises(PermissionDenied):
        services.approve_iban_change(req, user=w.h)  # the requester
    services.approve_iban_change(req, user=w.h2)
    emp = Employee.objects.get(pk=emp.pk)
    assert emp.iban == NEW and emp.bank_name == "West Bank"
    assert IbanChangeRequest.objects.get(pk=req.pk).status == "approved"
    logs = AccessLog.objects.filter(details__employee_code=emp.employee_code,
                                    details__action__in=("iban_change_requested", "iban_change_approved"))
    assert logs.count() == 2
    assert all(OLD not in str(l.details) and NEW not in str(l.details) for l in logs)
    assert logs.get(details__action="iban_change_approved").details["iban_after"] == "****5432"


def test_review8_rejected_iban_change_leaves_the_iban(w):
    emp = hired(w, iban=OLD)
    req = services.request_iban_change(emp, user=w.h, iban=NEW, reason_ar="تغيير")
    with pytest.raises(HrError):
        services.reject_iban_change(req, user=w.h2, reason_ar="")
    services.reject_iban_change(req, user=w.h2, reason_ar="غير موثق")
    assert Employee.objects.get(pk=emp.pk).iban == OLD
    with pytest.raises(HrError):
        services.approve_iban_change(req, user=w.h2)


def test_review8_draft_employee_iban_edit_is_logged(w):
    emp = employee(w, iban=OLD)
    services.update_employee(emp, user=w.h, iban=NEW)
    log = AccessLog.objects.get(details__action="iban_changed_draft")
    assert log.details["iban_before"] == "****7519" and log.details["iban_after"] == "****5432"


@pytest.mark.parametrize("value", ["=HYPERLINK(\"http://x.example\")", "+1", "-2", "@SUM(A1)", "\tX", " =X"])
def test_review7_formula_like_names_are_refused(w, value):
    with pytest.raises(HrError) as exc:
        employee(w, full_name_en=value)
    assert "= + - @" in exc.value.messages[0]
    with pytest.raises(HrError):
        employee(w, bank_name=value)
