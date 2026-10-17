"""R2 / access R24: billing admin is scoped to the user's entities and read-only; employee_ref is not shown."""
from types import SimpleNamespace

from django.contrib import admin

from billing.conftest import approved
from billing.models import Contract, SecondmentAssignment


def _request(user):
    return SimpleNamespace(user=user)


def test_admin_scoped_and_read_only(w):
    alm = approved(w)
    sub = approved(w, entity=w.sub, customer=w.sub_customer)
    model_admin = admin.site._registry[Contract]
    assert list(model_admin.get_queryset(_request(w.s))) == [alm]
    assert list(model_admin.get_queryset(_request(w.sub_s))) == [sub]
    assert not model_admin.has_change_permission(_request(w.c), alm)
    assert not model_admin.has_add_permission(_request(w.c))
    assert not model_admin.has_delete_permission(_request(w.c), alm)
    assert "employee_ref" in admin.site._registry[SecondmentAssignment].exclude
