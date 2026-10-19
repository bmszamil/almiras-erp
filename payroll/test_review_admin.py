"""Review item 3: the payroll admin is entity-scoped and read-only, never shows WPS file content, and has no GOSI
maternity cases. Ported from the reviewer's probe test_zz_admin_probe.py."""
from django.contrib import admin
from django.test import Client

from core.access.testing import make_user, member
from core.admin import EntityScopedAdmin
from payroll import models, services
from payroll.conftest import approved, saudi

IBAN = "SA0380000000608010167519"


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def test_review3_every_payroll_admin_is_entity_scoped_and_read_only():
    registered = [m for m in admin.site._registry if m._meta.app_label == "payroll"]
    assert registered
    for model in registered:
        ma = admin.site._registry[model]
        assert isinstance(ma, EntityScopedAdmin) and ma.access_view_permission and not ma.access_change_permission
    assert not admin.site.is_registered(models.MaternityGosiCase)
    assert not admin.site.is_registered(models.Payslip)


def test_review3_superuser_without_membership_sees_no_other_entitys_wps_file(w):
    saudi(w, w.sub)
    run = services.calculate_run(services.create_run(w.sub, user=w.ps, period="2027-03"), user=w.ps)
    run = services.approve_run(services.submit_run(run, user=w.ps), user=w.hs)
    f = services.generate_wps_file(run, user=w.ps, format_code="generic_csv")
    staff = make_user("staffy")
    member(staff, w.alm, "viewer")  # rights only in ALM
    staff.is_staff = staff.is_superuser = True
    staff.save()
    r = _client(staff).get(f"/admin/payroll/wpsfile/{f.pk}/change/")
    assert r.status_code != 200 and IBAN not in r.content.decode()
    assert _client(staff).get("/admin/payroll/payrollinput/").status_code == 403  # no payroll.view_payslip


def test_review3_wps_file_page_never_shows_the_file_content(w):
    saudi(w)
    run = approved(w)
    f = services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    services.attach_bank_processed_file(f, user=w.p, content=f"bank file {IBAN}")
    w.p.is_staff = True
    w.p.save()
    r = _client(w.p).get(f"/admin/payroll/wpsfile/{f.pk}/change/")
    assert r.status_code == 200 and IBAN not in r.content.decode()
    assert _client(w.p).get("/admin/payroll/payrollinput/").status_code == 200
