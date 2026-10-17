"""hr R1 to R5, R8 to R14, R46, R47, R49, R52, R54, R55 (AT-1, AT-2, AT-3, AT-5, AT-6, AT-24, AT-25, AT-26, AT-29,
AT-30)."""
import re
from datetime import date

import pytest
from django.db import connection
from django.http import Http404

from core import access
from core.access.models import AccessLog
from core.access.testing import make_user
from hr import models, services
from hr.conftest import contract, contract_fields, employee, hired, make_entity
from hr.models import ContractComponent, Employee, HrError, SalaryComponent
from ledger.models import Account

ARABIC = re.compile(r"[؀-ۿ]")


def _codes(exc):
    return " ".join(exc.value.messages)


# --- entity separation (R1 to R3) ----------------------------------------------------------------------

def test_at1_lists_and_details_are_entity_scoped(w):
    e1, e2 = hired(w), hired(w, w.sub)
    assert list(services.employees(w.h)) == [e1]
    with pytest.raises(Http404):
        services.get_employee(w.h, e2.pk)
    assert services.get_employee(w.h, e1.pk) == e1
    with pytest.raises(access.AccessDenied):
        services.employee_record(e2, user=w.h)


def test_at1_component_of_another_entity_refused(w):
    e1 = employee(w)
    c = services.create_contract(e1, user=w.h, components={"BASIC": "5000.00"}, **contract_fields(w, e1))
    sub_basic = SalaryComponent.objects.get(entity=w.sub, code="BASIC")
    with pytest.raises(HrError) as exc:
        ContractComponent.objects.create(entity=w.alm, contract=c, component=sub_basic, monthly_amount="1.00")
    assert exc.value.code == "entity_mismatch"
    sub_5100 = Account.objects.get(entity=w.sub, code="5100")
    with pytest.raises(HrError):
        services.set_component_account(SalaryComponent.objects.get(entity=w.alm, code="BASIC"), sub_5100, user=w.h)


def test_at2_same_id_in_two_entities_is_two_employees(w):
    a = employee(w, id_number="1000000001")
    b = employee(w, w.sub, id_number="1000000001")
    assert a.entity != b.entity and a.employee_code == "ALM-EMP-000001" and b.employee_code == "SUB-EMP-000001"
    assert list(services.employees(w.hs)) == [b]


def test_at3_new_entity_works_as_data_egy_master_data_kept(w):
    new = make_entity("NEW", "full", w.alm)
    hn = make_user("hnew")
    cn = make_user("cnew")
    from core.access.testing import member

    member(hn, new, "hr_officer")
    member(cn, new, "approver")
    assert SalaryComponent.objects.filter(entity=new).count() == 5
    assert models.LeaveType.objects.filter(entity=new, code="ANNUAL").exists()
    dept = services.create_department(new, user=hn, code="ADM", name_en="Admin", name_ar="الإدارة")
    grade = services.create_grade(new, user=hn, code="G1", name_en="G1", name_ar="د1", overtime_eligible=False)
    emp = employee(w, new, by=hn)
    c = services.create_contract(emp, user=hn, components={"BASIC": "4000.00"},
                                 **contract_fields(w, emp, department=dept, grade=grade))
    services.submit_contract(c, user=hn)
    assert services.approve_contract(c, user=cn).status == "approved"
    # EGY master data saved; its jurisdiction has no parameters, no Saudi components or leave types.
    local = employee(w, w.egy, basis="local_entity_employee")
    assert local.pk and not SalaryComponent.objects.filter(entity=w.egy, code="REMUNERATION").exists()
    assert list(models.LeaveType.objects.filter(entity=w.egy).values_list("code", flat=True)) == ["UNPAID"]
    assert services.setup_entity(new).pk == services.setup_entity(new).pk  # idempotent


def test_r5_has_employees_false_refuses_records(w):
    s = models.HrSettings.objects.get(entity=w.sub)
    s.has_employees = False
    s.save()
    with pytest.raises(HrError) as exc:
        employee(w, w.sub)
    assert exc.value.code == "no_employees"


# --- identity and names (R8, R9, R14; AT-5) ---------------------------------------------------------------

def test_at5_identity_rules(w):
    with pytest.raises(HrError) as exc:
        employee(w, id_type="iqama")  # Saudi with iqama
    assert "national ID" in _codes(exc)
    with pytest.raises(HrError):
        employee(w, saudi=False, id_type="national_id", id_number="1000000002")
    with pytest.raises(HrError):
        employee(w, full_name_ar="")
    with pytest.raises(HrError):
        employee(w, saudi=False, id_type="gcc_id", id_number="G1234567")  # an Indian national has no GCC ID
    # Compliance 2026-10-17: the digit format is an unverified parameter, a warning, not a refusal.
    short = employee(w, id_number="100000000")
    assert short.warnings and "unverified" in short.warnings[0]
    ok_saudi = employee(w, id_number="1000000003")
    ok_iqama = employee(w, saudi=False, id_number="2000000003")
    assert ok_saudi.warnings == [] and ok_iqama.warnings == []
    with pytest.raises(HrError) as exc:
        employee(w, id_number="1000000003")
    assert exc.value.code == "id_duplicate"
    assert employee(w, saudi=False, nationality="KW", id_type="gcc_id", id_number="G7654321").pk


def test_r9_codes_are_gap_free_even_after_a_refusal(w):
    first = employee(w)
    with pytest.raises(HrError):
        employee(w, full_name_ar="")
    assert employee(w).employee_code == "ALM-EMP-000002" and first.employee_code == "ALM-EMP-000001"


def test_r14_active_employee_never_deleted_draft_by_creator_only(w):
    active = hired(w)
    with pytest.raises(HrError):
        services.delete_draft_employee(active, user=w.h)
    with pytest.raises(HrError):
        active.delete()
    draft = employee(w)
    with pytest.raises(HrError):
        services.delete_draft_employee(draft, user=w.h2)
    services.delete_draft_employee(draft, user=w.h)
    assert not Employee.objects.filter(pk=draft.pk).exists()
    with pytest.raises(HrError):
        Employee.objects.filter(pk=active.pk).update(status="draft")


def test_r14_terminated_employee_read_only(w):
    emp = hired(w, hire_date=date(2025, 1, 1))
    t = services.record_termination(emp, user=w.h, kind="employer_termination", last_working_day=date(2027, 5, 1),
                                    notice_given_on=date(2027, 3, 1), reason_ar="سبب")
    services.approve_termination(services.submit_termination(t, user=w.h), user=w.c)
    emp.refresh_from_db()
    assert emp.status == "terminated"
    with pytest.raises(HrError) as exc:
        services.update_employee(emp, user=w.h, phone="0500000000")
    assert exc.value.code == "employee_terminated"


def test_r8_active_only_with_started_approved_contract(w):
    emp = employee(w, hire_date=date(2027, 7, 1))
    contract(w, emp)
    emp.refresh_from_db()
    assert emp.status == "draft"
    w.clock.set(date(2027, 7, 1))
    assert services.refresh_statuses(today=date(2027, 7, 1)) >= 1
    emp.refresh_from_db()
    assert emp.status == "active"


def test_r9_iban_checked(w):
    with pytest.raises(HrError):
        employee(w, iban="SA0000000000000000000000")
    assert employee(w, iban="GB82WEST12345698765432").pk  # the textbook example IBAN


# --- personal data (R10 to R12; AT-6) --------------------------------------------------------------------

def test_at6_masking_logging_export_and_ciphertext(w):
    e1 = hired(w, iban="GB82WEST12345698765432", bank_name="Test Bank")
    before = AccessLog.objects.filter(event="personal_data_viewed").count()
    rec = services.employee_record(e1, user=w.a)
    assert rec["full_name_en"] == services.MASK and rec["id_number"] == services.MASK
    assert rec["employee_code"] == e1.employee_code
    # §7: the viewer sees counts only (OQ-11 default), never a record, masked or not.
    with pytest.raises(access.AccessDenied):
        services.employee_record(e1, user=w.v)
    assert not services.employees(w.v).exists()
    assert services.employee_counts(w.v, w.alm) == {"entity": "ALM", "total": 1, "by_status": {"active": 1},
                                                    "by_basis": {"ksa_employee": 1}}
    assert AccessLog.objects.filter(event="personal_data_viewed").count() == before
    rec = services.employee_record(e1, user=w.h)
    assert rec["full_name_en"] == "Test Person" and rec["iban"] == "GB82WEST12345698765432"
    row = AccessLog.objects.filter(event="personal_data_viewed").latest("id")
    assert row.actor == w.h and row.details["employee_code"] == e1.employee_code
    # Payroll officer: bank, IBAN and ID only (§7), logged.
    rec = services.employee_record(e1, user=w.p)
    assert rec["iban"] == "GB82WEST12345698765432" and rec["full_name_en"] == services.MASK
    assert AccessLog.objects.filter(event="personal_data_viewed", actor=w.p).exists()
    # Export: own data, en/ar labels, logged; viewers cannot export.
    rows = services.export_personal_data(e1, user=w.h)
    assert {r["field"] for r in rows} >= {"full_name_ar", "id_number"} and all(r["label_ar"] for r in rows)
    assert AccessLog.objects.filter(event="personal_data_exported", actor=w.h).exists()
    with pytest.raises(access.AccessDenied):
        services.export_personal_data(e1, user=w.p)
    # Stored ciphertext.
    with connection.cursor() as cur:
        cur.execute("SELECT full_name_en, id_number, iban, date_of_birth FROM hr_employee WHERE id = %s", [e1.pk])
        values = cur.fetchone()
    assert all(v.startswith("enc1:") for v in values)
    assert "Test Person" not in "".join(values)


def test_r11_object_label_is_code_only(w):
    e1 = hired(w)
    assert str(e1) == e1.employee_code


def test_r12_no_religion_health_or_family_fields():
    names = {f.name for f in Employee._meta.get_fields()}
    assert not names & {"religion", "marital_status", "health", "children", "spouse"}


# --- employment basis (R49, R52; AT-26) ---------------------------------------------------------------

def test_at26_basis_per_jurisdiction_and_fixed_after_approval(w, monkeypatch):
    assert employee(w).employment_basis == "ksa_employee"
    assert employee(w, basis="remuneration").country_of_residence == "EG"
    with pytest.raises(HrError):
        employee(w, basis="local_entity_employee")
    with pytest.raises(HrError):
        employee(w, w.egy, basis="ksa_employee")
    local = employee(w, w.egy, basis="local_entity_employee")
    dept = services.create_department(w.egy, user=w.he, code="OPS", name_en="Ops", name_ar="العمليات")
    grade = services.create_grade(w.egy, user=w.he, code="G1", name_en="G1", name_ar="د1", overtime_eligible=False)
    c = services.create_contract(local, user=w.he, components={"BASIC": "9000.00"},
                                 **contract_fields(w, local, department=dept, grade=grade, currency="EGP"))
    services.submit_contract(c, user=w.he)  # master data kept
    seen = []
    original = services.param

    def spy(jurisdiction, code, on, **kw):
        seen.append(jurisdiction)
        return original(jurisdiction, code, on, **kw)

    monkeypatch.setattr(services, "param", spy)
    with pytest.raises(HrError) as exc:
        services.approve_contract(c, user=w.ce)
    assert exc.value.code == "jurisdiction_not_configured"
    assert "غير مهيأة" in exc.value.messages[0]
    assert seen and "SA" not in seen
    monkeypatch.undo()
    ksa = hired(w)
    with pytest.raises(HrError) as exc:
        services.update_employee(ksa, user=w.h, employment_basis="remuneration", country_of_residence="EG")
    assert exc.value.code == "basis_fixed"


def test_r52_remuneration_needs_residence_outside_ksa(w):
    with pytest.raises(HrError):
        employee(w, basis="remuneration", country_of_residence="SA")
    with pytest.raises(HrError):
        employee(w, basis="remuneration", country_of_residence="")


def test_r51_local_employee_never_reads_saudi_rows(w):
    local = employee(w, w.egy, basis="local_entity_employee")
    with pytest.raises(HrError) as exc:
        services._jurisdiction(local)  # EG: allowed, but no parameters exist
        services.ParamUse().get(services._jurisdiction(local), "leave.annual", date(2027, 1, 1))
    assert exc.value.code == "jurisdiction_not_configured"


# --- departments, grades, overtime route (R54, R55; AT-29, AT-30) ------------------------------------------

def test_at29_department_head_and_grade_rules(w):
    head = hired(w, linked_user=make_user("fin_head"))
    services.set_department_head(w.fin, head, user=w.h)
    sub_emp = hired(w, w.sub, linked_user=make_user("sub_person"))
    no_user = hired(w)
    for bad in (sub_emp, no_user):
        with pytest.raises(HrError):
            services.set_department_head(w.fin, bad, user=w.h)
    gone = hired(w, linked_user=make_user("gone"), hire_date=date(2025, 1, 1))
    t = services.record_termination(gone, user=w.h, kind="employer_termination", last_working_day=date(2027, 5, 1),
                                    notice_given_on=date(2027, 3, 1), reason_ar="سبب")
    services.approve_termination(services.submit_termination(t, user=w.h), user=w.c)
    gone.refresh_from_db()
    with pytest.raises(HrError):
        services.set_department_head(w.fin, gone, user=w.h)
    with pytest.raises(HrError) as exc:
        services.create_grade(w.alm, user=w.h, code="G-X", name_en="X", name_ar="س")
    assert exc.value.code == "overtime_flag_required"
    mgmt = services.create_grade(w.alm, user=w.h, code="G-MGMT", name_en="Middle management", name_ar="الإدارة الوسطى",
                                 overtime_eligible=False)
    emp = employee(w)
    for missing in ({"grade": None}, {"department": None}):
        with pytest.raises(HrError):
            contract(w, emp, approve=False, **missing)
    c = contract(w, emp, grade=mgmt)
    assert c.status == "approved" and c.grade == mgmt
    with pytest.raises(access.AccessDenied):
        services.create_department(w.alm, user=w.p, code="X", name_en="X", name_ar="س")


def test_at30_overtime_route_seed_change_and_approver(w):
    route = lambda e: models.HrSettings.objects.get(entity=e).overtime_approval_route  # noqa: E731
    new = make_entity("NEW", "full", w.alm)
    assert (route(w.alm), route(w.sub), route(new)) == ("managing_partner", "department_head", "department_head")
    with pytest.raises(access.AccessDenied):
        services.set_overtime_route(w.sub, "managing_partner", user=w.h, reason="test")
    services.set_overtime_route(w.sub, "managing_partner", user=w.mp, reason="test")
    assert route(w.sub) == "managing_partner"
    log = AccessLog.objects.filter(event="entity_settings_changed", permission="hr.set_overtime_route").latest("id")
    assert log.details["after"] == "managing_partner" and log.details["message_ar"]
    services.set_overtime_route(w.sub, "department_head", user=w.mp, reason="back")
    # Approver per route.
    alm_emp = hired(w)
    assert services.overtime_approver(alm_emp, date(2027, 2, 1), user=w.h).route == "managing_partner"
    head_user = make_user("ops_head")
    head = hired(w, w.sub, linked_user=head_user)
    staff = hired(w, w.sub)
    assert services.overtime_approver(staff, date(2027, 2, 1), user=w.hs).route == "managing_partner"  # no head
    services.set_department_head(w.sub_dept, head, user=w.hs)
    got = services.overtime_approver(staff, date(2027, 2, 1), user=w.hs)
    assert got.route == "department_head" and got.user == head_user
    assert services.overtime_approver(head, date(2027, 2, 1), user=w.hs).route == "managing_partner"  # own
    mgmt = services.create_grade(w.sub, user=w.hs, code="G-M", name_en="Mgmt", name_ar="إدارة", overtime_eligible=False)
    manager = hired(w, w.sub, contract_kwargs={"grade": mgmt})
    with pytest.raises(HrError) as exc:
        services.overtime_approver(manager, date(2027, 2, 1), user=w.hs)
    assert exc.value.code == "grade_no_overtime"


def test_at25_sharia_prohibited_component_account_refused(w):
    basic = SalaryComponent.objects.get(entity=w.alm, code="BASIC")
    with pytest.raises(HrError) as exc:
        services.set_component_account(basic, Account.objects.get(entity=w.alm, code="5610"), user=w.h)
    assert "Sharia" in exc.value.messages[0] and "شرع" in exc.value.messages[0]
    with pytest.raises(HrError):
        services.set_component_account(basic, Account.objects.get(entity=w.alm, code="1100"), user=w.h)
    assert services.set_component_account(basic, Account.objects.get(entity=w.alm, code="5900"), user=w.h).pk


# --- language (R46; AT-24) -----------------------------------------------------------------------------------

def test_at24_every_choice_and_label_is_bilingual():
    for model in [m for m in vars(models).values() if isinstance(m, type) and hasattr(m, "_meta")
                  and getattr(m._meta, "app_label", "") == "hr" and not m._meta.abstract]:
        for f in model._meta.get_fields():
            if not getattr(f, "concrete", False) or f.auto_created or f.name in ("id", "id_number_hash"):
                continue
            assert ARABIC.search(str(f.verbose_name)), f"{model.__name__}.{f.name}"
            for _, label in getattr(f, "choices", None) or []:
                assert ARABIC.search(str(label)), f"{model.__name__}.{f.name}: {label}"
        assert ARABIC.search(str(model._meta.verbose_name)), model.__name__


def test_at24_errors_are_bilingual_and_follow_the_language(w):
    with pytest.raises(HrError) as exc:
        employee(w, full_name_ar="")
    assert ARABIC.search(exc.value.text("ar")) and not ARABIC.search(exc.value.text("en"))
    ar_user = make_user("ar_user", language="ar")
    with pytest.raises(access.AccessDenied) as denied:
        services.employee_record(hired(w), user=ar_user)
    assert ARABIC.search(str(denied.value))


def test_s7_counts_are_per_entity_and_need_a_right(w):
    hired(w)
    hired(w, w.sub)
    assert services.employee_counts(w.h, w.alm)["total"] == 1  # hr.view holders get counts too
    assert services.employee_counts(w.a, w.alm)["total"] == 1
    with pytest.raises(access.AccessDenied):
        services.employee_counts(w.v, w.sub)  # the viewer's role is in ALM only
    with pytest.raises(access.AccessDenied):
        services.employee_counts(w.agent, w.alm)
    from core.access.seed import ROLE_SEED

    assert "hr.view" not in ROLE_SEED["viewer"][5] and "hr.view_counts" in ROLE_SEED["viewer"][5]
    assert "hr.view" in ROLE_SEED["accountant"][5]  # AT-6: the accountant opens employees, masked
