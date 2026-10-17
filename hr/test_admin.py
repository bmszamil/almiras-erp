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
