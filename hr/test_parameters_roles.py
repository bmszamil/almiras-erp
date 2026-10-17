"""hr R6, R7 (AT-4), R13 (AT-8), R57 (AT-32), R48: statutory parameters, roles, agents, rights."""
import ast
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from core import access
from core.access.models import AccessLog, Role, RolePermission
from core.access.seed import ROLE_SEED
from core.access.testing import make_user, member
from hr import services, statutory_seed
from hr.conftest import contract, employee, hired
from hr.models import HrError, StatutoryParameter

HR = Path(__file__).resolve().parent


def _submitted_termination(w, emp, kind="employer_termination", **fields):
    fields.setdefault("last_working_day", date(2027, 6, 30))
    fields.setdefault("notice_given_on", date(2027, 5, 1))
    t = services.record_termination(emp, user=w.h, kind=kind, reason_ar="سبب", **fields)
    return services.submit_termination(t, user=w.h)


# --- R6: parameters are data ------------------------------------------------------------------------

def test_r6_seeded_parameters_are_unverified_sa_rows(db):
    rows = StatutoryParameter.objects.filter(jurisdiction="SA")
    assert set(rows.values_list("code", flat=True)) == set(statutory_seed.ALL_CODES)
    assert set(rows.values_list("status", flat=True)) == {"unverified"}
    assert all(r.source_url and r.source_reference for r in rows)
    assert not StatutoryParameter.objects.exclude(jurisdiction="SA").exists()  # OQ-26: none for EG, CN, AE


def test_r6_param_reads_the_row_valid_on_the_day(db):
    old = services.param("SA", "probation.max_days", date(2027, 1, 1))
    StatutoryParameter.objects.create(jurisdiction="SA", code="probation.max_days", value={"days": "170"},
                                      valid_from=date(2028, 1, 1), source_url="https://example.test",
                                      source_reference="test row")
    assert services.param("SA", "probation.max_days", date(2027, 12, 31)).pk == old.pk
    assert services.param("SA", "probation.max_days", date(2028, 1, 1)).value == {"days": "170"}
    with pytest.raises(HrError) as exc:
        services.param("EG", "probation.max_days", date(2027, 1, 1))
    assert exc.value.code == "jurisdiction_not_configured" and "/" in exc.value.messages[0]


def test_r6_parameter_values_refuse_floats(db):
    with pytest.raises(HrError):
        StatutoryParameter.objects.create(jurisdiction="SA", code="x.y", value={"rate": 0.75},
                                          valid_from=date(2027, 1, 1), source_url="https://example.test",
                                          source_reference="t")


# Regulatory figures that must only be in hr/statutory_seed.py (R6 scan).
REGULATORY_NUMBERS = {180, 365, 354, 48, 84, 42, 28, 130, 117, 75, 45000, 1500}
REGULATORY_STRINGS = {"0.75", "1/3", "2/3", "1/2", "0.5", "180", "365", "354"}


def test_r6_scan_no_regulatory_literal_in_hr_code():
    found = []
    for path in HR.glob("*.py"):
        if path.name in ("statutory_seed.py", "conftest.py") or path.name.startswith("test_"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant):
                if (type(node.value) is int and node.value in REGULATORY_NUMBERS) or (
                        isinstance(node.value, str) and node.value in REGULATORY_STRINGS):
                    found.append(f"{path.name}:{node.lineno}: {node.value!r}")
    assert not found, found


# --- R7 / AT-4 ----------------------------------------------------------------------------------------

def test_at4_unverified_parameters_block_approval_until_verified(w, settings):
    emp = hired(w, hire_date=date(2023, 1, 1))
    t = _submitted_termination(w, emp)
    settings.HR_REQUIRE_VERIFIED_PARAMETERS = True
    with pytest.raises(HrError) as exc:
        services.approve_termination(t, user=w.c)
    assert exc.value.code == "parameter_unverified"
    assert "eosb.award_table" in exc.value.messages[0] and "غير متحقق" in exc.value.messages[0]

    # MP may not verify a row MP entered; MP verifies a row H entered.
    member(w.mp, w.alm, "hr_officer")
    own = services.add_statutory_parameter(user=w.mp, jurisdiction="SA", code="test.own", value={"x": "1"},
                                           valid_from=date(2027, 1, 1), source_url="https://example.test",
                                           source_reference="test")
    with pytest.raises(access.AccessDenied) as denied:
        services.verify_statutory_parameter(own, user=w.mp)
    assert denied.value.code == "self_approval"
    other = services.add_statutory_parameter(user=w.h, jurisdiction="SA", code="test.other", value={"x": "1"},
                                             valid_from=date(2027, 1, 1), source_url="https://example.test",
                                             source_reference="test")
    assert services.verify_statutory_parameter(other, user=w.mp).status == "verified"
    with pytest.raises(access.AccessDenied):  # H holds no verification right
        services.verify_statutory_parameter(own, user=w.h)

    for row in StatutoryParameter.objects.filter(jurisdiction="SA", status="unverified").exclude(code="test.own"):
        services.verify_statutory_parameter(row, user=w.mp)
    t = services.approve_termination(t, user=w.c)
    assert t.status == "approved"

    used = StatutoryParameter.objects.get(code="eosb.award_table", jurisdiction="SA")
    assert used.used_at is not None
    used.value = {"tiers": []}
    with pytest.raises(HrError) as exc:
        used.save()
    assert exc.value.code == "parameter_used"
    with pytest.raises(HrError):
        used.delete()
    # A correction is a superseding row entered with it; the used row keeps its value.
    old, new = services.supersede_statutory_parameter(used, user=w.h, value=used.value, source_url=used.source_url,
                                                      source_reference="corrected")
    assert StatutoryParameter.objects.get(pk=used.pk).status == "superseded"
    assert new.status == "unverified" and new.valid_from == used.valid_from and new.entered_by == w.h
    assert services.param("SA", "eosb.award_table", date(2027, 1, 1)).pk == new.pk


def test_r7_contract_approval_also_refused_on_unverified(w, settings):
    emp = employee(w)
    c = contract(w, emp, approve=False)
    settings.HR_REQUIRE_VERIFIED_PARAMETERS = True
    with pytest.raises(HrError) as exc:
        services.approve_contract(c, user=w.c)
    assert "probation.max_days" in exc.value.messages[0]


def test_r7_production_settings_require_verified_parameters():
    from config import settings as prod

    assert prod.HR_REQUIRE_VERIFIED_PARAMETERS is True


# --- R13 / AT-8: agents ----------------------------------------------------------------------------------

def test_at8_agents_cannot_read_or_draft_hr(w):
    member_role = Role.objects.create(code="agent_hr_reader", name_en="Agent", name_ar="وكيل", scope="entity",
                                      kind="agent")
    member(w.agent, w.alm, "agent_hr_reader")
    emp = hired(w)
    assert not services.employees(w.agent).exists()
    with pytest.raises(access.AccessDenied) as exc:
        services.request_leave(emp, "ANNUAL", user=w.agent, start_date=date(2027, 6, 10), end_date=date(2027, 6, 11))
    assert exc.value.code == "agent_forbidden"
    with pytest.raises(ValidationError):
        RolePermission.objects.create(role=member_role, codename="hr.view")
    from core.access import catalogue

    assert not [c for c in catalogue.CATALOGUE if c.startswith("hr.") and catalogue.CATALOGUE[c].agent_ok]
    from hr.models import Source

    assert "agent" not in Source.values


# --- R57 / AT-32: roles -----------------------------------------------------------------------------------

def test_at32_mp_grants_hr_roles_access_admin_cannot(w):
    user_h, user_p, other = make_user("newh"), make_user("newp"), make_user("other")
    hr_role, pay_role = Role.objects.get(code="hr_officer"), Role.objects.get(code="payroll_officer")
    m = access.grant_membership(w.mp, user_h, w.alm, hr_role, timezone.now(), reason="test grant")
    access.grant_membership(w.mp, user_p, w.alm, pay_role, timezone.now(), reason="test grant")
    assert AccessLog.objects.filter(event="membership_granted", membership=m).exists()
    with pytest.raises(access.AccessDenied) as exc:
        access.grant_membership(w.aa, other, w.alm, hr_role, timezone.now(), reason="test grant")
    assert exc.value.code == "role_not_grantable"
    with pytest.raises(access.AccessDenied):
        access.grant_membership(w.aa, other, w.alm, pay_role, timezone.now(), reason="test grant")
    assert hr_role.name_ar == "مسؤول الموارد البشرية" and pay_role.name_ar == "مسؤول الرواتب"
    assert ROLE_SEED["hr_officer"][2:4] == ("entity", "human")


def test_r48_every_hr_service_checks_rights(w):
    emp = hired(w)
    stranger = make_user("stranger")
    calls = [
        lambda: services.create_employee(w.alm, user=stranger, full_name_en="x"),
        lambda: services.employee_record(emp, user=stranger),
        lambda: services.create_contract(emp, user=stranger),
        lambda: services.calculate_eosb(emp, date(2027, 5, 31), "resignation", user=stranger),
        lambda: services.leave_balance(emp, "ANNUAL", user=stranger),
        lambda: services.document_flag(emp, user=stranger),
        lambda: services.terms_on(emp, date(2027, 2, 1), user=stranger),
    ]
    for call in calls:
        with pytest.raises(access.AccessDenied):
            call()


def test_hr_officer_of_sub_has_no_right_in_alm(w):
    emp = hired(w)
    with pytest.raises(access.AccessDenied):
        services.update_employee(emp, user=w.hs, phone="0500000000")
    assert services.employees(w.hs).filter(pk=emp.pk).count() == 0


def test_payroll_officer_cannot_approve_contracts_or_edit(w):
    emp = employee(w)
    c = contract(w, emp, approve=False)
    with pytest.raises(access.AccessDenied):
        services.approve_contract(c, user=w.p)
    with pytest.raises(access.AccessDenied):
        services.update_employee(emp, user=w.p, phone="0500000001")


# --- seed loader and the R62 data migration ----------------------------------------------------------------

def test_r7_seed_never_verifies_and_records_the_compliance_check(db):
    rows = {r.code: r for r in StatutoryParameter.objects.filter(jurisdiction="SA")}
    assert all(r.verified_by_id is None and r.verified_at is None for r in rows.values())
    assert "Checked 2026-10-17 against the HRSD text" in rows["probation.max_days"].source_reference
    assert "MP verification pending" in rows["probation.max_days"].source_reference
    assert "Checked 2026-10-17" not in rows["id.format"].source_reference  # [CC-verify]: no official format
    assert "Checked 2026-10-17" not in rows["calendar.year_days"].source_reference  # OQ-36 legal sign-off


def test_r7_seed_reload_leaves_verified_and_used_rows_alone(w):
    verified = StatutoryParameter.objects.get(jurisdiction="SA", code="leave.marriage")
    services.verify_statutory_parameter(verified, user=w.mp)
    unverified = StatutoryParameter.objects.get(jurisdiction="SA", code="leave.hajj")
    unverified.value = {"min_days": "1"}
    unverified.save()
    verified = StatutoryParameter.objects.get(pk=verified.pk)
    verified.source_reference = "edited by MP"
    with pytest.raises(HrError) as exc:  # R7: a verified row is never edited
        verified.save()
    assert exc.value.code == "parameter_used"
    seed_exam = StatutoryParameter.objects.get(jurisdiction="SA", code="leave.exam")
    _, entered = services.supersede_statutory_parameter(seed_exam, user=w.h, value={"request_lead_days": "16"},
                                                        source_url="https://example.test",
                                                        source_reference="entered by H")
    statutory_seed.apply(StatutoryParameter)
    verified.refresh_from_db()
    unverified.refresh_from_db()
    entered.refresh_from_db()
    assert verified.source_reference != "edited by MP" and verified.status == "verified"
    # Nit 21: a row a person entered (entered_by set) is never refreshed by the loader, even unverified and unused.
    assert entered.value == {"request_lead_days": "16"} and entered.status == "unverified"
    assert StatutoryParameter.objects.filter(jurisdiction="SA", code="leave.exam").count() == 2
    assert verified.verified_by == w.mp
    assert unverified.value == statutory_seed.SA_PARAMETERS["leave.hajj"][0] and unverified.verified_by_id is None


def _migration_0005():
    import importlib

    return importlib.import_module("hr.migrations.0005_unpaid_suspension_parameter")


def test_r62_data_migration_moves_the_threshold_and_never_copies_verification(w):
    """Nit 20: 0005 creates the new rows unverified (no verification state copied) and imports no live seed."""
    from django.apps import apps

    when = timezone.now()
    verified = services.add_statutory_parameter(
        user=w.h, jurisdiction="AE", code="leave.annual", value={"tiers": [], "unpaid_suspends_after_days": "20"},
        valid_from=date(2020, 1, 1), source_url="https://example.test", source_reference="test")
    services.verify_statutory_parameter(verified, user=w.mp)
    unverified = StatutoryParameter.objects.create(
        jurisdiction="QA", code="eosb.award_table", value={"tiers": [], "exclude_unpaid_days_beyond": "15"},
        valid_from=date(2020, 1, 1), source_url="https://example.test", source_reference="test")
    used = StatutoryParameter.objects.create(
        jurisdiction="BH", code="leave.annual", value={"tiers": [], "unpaid_suspends_after_days": "20"},
        valid_from=date(2020, 1, 1), source_url="https://example.test", source_reference="test")
    used.used_at = when
    used.save()
    _migration_0005().forward(apps, None)
    moved = StatutoryParameter.objects.get(jurisdiction="AE", code="leave.unpaid.suspension_after_days")
    assert moved.value == {"days": "20", "aggregation": "work_year"} and moved.valid_from == date(2020, 1, 1)
    assert (moved.status, moved.verified_by, moved.verified_at, moved.entered_by) == ("unverified", None, None, None)
    qa = StatutoryParameter.objects.get(jurisdiction="QA", code="leave.unpaid.suspension_after_days")
    assert qa.status == "unverified" and qa.verified_by_id is None and qa.value["days"] == "15"
    verified.refresh_from_db()
    unverified.refresh_from_db()
    used.refresh_from_db()
    assert verified.value["unpaid_suspends_after_days"] == "20"  # a verified row is never edited (R7)
    assert "exclude_unpaid_days_beyond" not in unverified.value
    assert used.value["unpaid_suspends_after_days"] == "20"  # a used row is never edited (R7)
    assert StatutoryParameter.objects.filter(jurisdiction="BH", code="leave.unpaid.suspension_after_days").exists()
    _migration_0005().forward(apps, None)  # idempotent
    assert StatutoryParameter.objects.filter(code="leave.unpaid.suspension_after_days", jurisdiction="AE").count() == 1
    source = Path(_migration_0005().__file__).read_text(encoding="utf-8")
    assert "statutory_seed" not in source.split('"""', 2)[2]  # no import of the live seed module


# --- R7: parameter immutability (review probes P4, P4b) ------------------------------------------------------

def _seed_row(code="eosb.award_table"):
    return services.param("SA", code, date(2027, 1, 1))


def test_r7_used_at_is_set_once_and_never_cleared(db):
    row = _seed_row()
    row.used_at = timezone.now()
    row.save()
    row = StatutoryParameter.objects.get(pk=row.pk)
    row.used_at = None
    with pytest.raises(HrError) as exc:
        row.save()
    assert exc.value.code == "parameter_used"
    row = StatutoryParameter.objects.get(pk=row.pk)
    row.used_at = timezone.now()
    with pytest.raises(HrError):
        row.save()
    row = StatutoryParameter.objects.get(pk=row.pk)
    row.value = {"tiers": [{"up_to_years": None, "months_per_year": "5"}]}
    with pytest.raises(HrError):
        row.save()


def test_r7_verified_row_value_and_source_never_change(w):
    row = services.add_statutory_parameter(user=w.h, jurisdiction="SA", code="leave.marriage", value={"days": "5"},
                                           valid_from=date(2027, 7, 1), source_url="https://example.test",
                                           source_reference="Art. 113")
    services.verify_statutory_parameter(row, user=w.mp)
    for field, value in (("value", {"days": "50"}), ("source_reference", "x"), ("source_url", "https://o.test"),
                         ("valid_from", date(2027, 8, 1)), ("valid_to", date(2028, 1, 1)), ("code", "leave.other")):
        r = StatutoryParameter.objects.get(pk=row.pk)
        setattr(r, field, value)
        with pytest.raises(HrError):
            r.save()
    r = StatutoryParameter.objects.get(pk=row.pk)
    assert r.value == {"days": "5"} and r.status == "verified" and r.verified_by == w.mp


def test_r7_status_moves_forward_only_and_verifier_is_set_once(w):
    row = services.add_statutory_parameter(user=w.h, jurisdiction="SA", code="test.status", value={"x": "1"},
                                           valid_from=date(2027, 1, 1), source_url="https://example.test",
                                           source_reference="t")
    r = StatutoryParameter.objects.get(pk=row.pk)
    r.status = "verified"  # no verifier
    with pytest.raises(HrError):
        r.save()
    r = StatutoryParameter.objects.get(pk=row.pk)
    r.status, r.verified_by, r.verified_at = "verified", w.h, timezone.now()  # the enterer verifies
    with pytest.raises(HrError):
        r.save()
    services.verify_statutory_parameter(row, user=w.mp)
    r = StatutoryParameter.objects.get(pk=row.pk)
    r.status, r.verified_by, r.verified_at = "unverified", None, None
    with pytest.raises(HrError):
        r.save()
    r = StatutoryParameter.objects.get(pk=row.pk)
    r.verified_by = w.c
    with pytest.raises(HrError):
        r.save()
    with pytest.raises(HrError):  # inserted already verified
        StatutoryParameter.objects.create(jurisdiction="SA", code="test.new", value={}, valid_from=date(2027, 1, 1),
                                          source_url="https://example.test", source_reference="t", status="verified",
                                          verified_by=w.mp, verified_at=timezone.now())
    old, new = services.supersede_statutory_parameter(row, user=w.h, value={"x": "2"},
                                                      source_url="https://example.test", source_reference="fix")
    r = StatutoryParameter.objects.get(pk=old.pk)
    r.status = "verified"
    with pytest.raises(HrError):
        r.save()


def test_r7_superseding_always_enters_a_replacement(w):
    row = _seed_row("leave.marriage")
    with pytest.raises(TypeError):
        services.supersede_statutory_parameter(row, user=w.h)  # no replacement given
    assert StatutoryParameter.objects.get(pk=row.pk).status == "unverified"
    with pytest.raises(HrError):  # a replacement never starts before the row it replaces
        services.supersede_statutory_parameter(row, user=w.h, value={"days": "5"}, source_url=row.source_url,
                                               source_reference="x", valid_from=row.valid_from - timedelta(days=1))
    assert StatutoryParameter.objects.get(pk=row.pk).status == "unverified"
    old, new = services.supersede_statutory_parameter(row, user=w.h, value={"days": "5"}, source_url=row.source_url,
                                                      source_reference="Art. 113 (re-entered)")
    assert old.status == "superseded" and services.param("SA", "leave.marriage", date(2027, 1, 1)).pk == new.pk
    with pytest.raises(HrError):
        services.supersede_statutory_parameter(old, user=w.h, value={"days": "5"}, source_url=row.source_url,
                                               source_reference="again")
    with pytest.raises(access.AccessDenied):
        services.supersede_statutory_parameter(new, user=w.p, value={"days": "5"}, source_url=row.source_url,
                                               source_reference="x")
