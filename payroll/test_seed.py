"""Seed data and permissions (hr R6, R7, R68; payroll R4, R38, §5, §7)."""
from datetime import date

from core.access import catalogue
from core.access.models import Role
from core.access.seed import ROLE_SEED, ROLE_SEED_V12, ROLE_SEED_V13, ROLE_SEED_V14, ROLE_SEED_V16, ROLE_SEED_V17
from hr.models import StatutoryParameter
from ledger.models import Account
from payroll import statutory_seed


def test_payroll_parameters_are_seeded_unverified_with_sources(db):
    rows = StatutoryParameter.objects.filter(code__in=set(statutory_seed.SA_PAYROLL) | set(statutory_seed.SA_GOSI))
    assert rows.count() == len(statutory_seed.SA_PAYROLL) + len(statutory_seed.SA_GOSI)
    assert set(rows.values_list("status", flat=True)) == {"unverified"} and not rows.filter(verified_by__isnull=False)
    steps = StatutoryParameter.objects.filter(code="gosi.rates.annuities").order_by("valid_from")
    assert [r.value["new_m273"]["employee"] for r in steps] == ["9", "9.5", "10", "10.5", "11"]
    assert all(r.value["legacy_m33"] == {"employer": "9", "employee": "9"} for r in steps)
    assert StatutoryParameter.objects.get(code="gosi.partial_month_divisor").value == {"divisor": "calendar"}
    fifth = StatutoryParameter.objects.get(code="gosi.legal_age").value["legacy_clause_fifth"]
    assert len(fifth["schedule"]) == 21 and fifth["deemed_50_hijri_at"] == "48y6m"
    assert "240 months" in fifth["track_rule"]


def test_r68_only_sa_and_eg_are_active_and_egypt_rows_are_research(db):
    active = StatutoryParameter.objects.filter(code="jurisdiction.active")
    assert set(active.values_list("jurisdiction", flat=True)) == {"SA", "EG"}
    eg = StatutoryParameter.objects.filter(jurisdiction="EG")
    assert eg.filter(code="si.rates").get().value == {"employee": "0.11", "employer": "0.1875"}
    assert all("not the Gazette" in r.source_reference or r.code == "jurisdiction.active" for r in eg)


def test_v13_seed_payroll_rights(db):
    assert ROLE_SEED is ROLE_SEED_V17
    perms = {code: set(row[5]) for code, row in ROLE_SEED.items()}
    assert {c for c, p in perms.items() if "payroll.approve_run" in p} == {"hr_officer", "managing_partner"}
    assert {c for c, p in perms.items() if "payroll.approve_loan" in p} == {"managing_partner"}
    assert perms["department_head"] == {"payroll.approve_overtime"}
    assert not any(c.startswith("payroll.") for c in perms["system_billing"])
    assert set(ROLE_SEED_V12) < set(ROLE_SEED_V13)
    assert not [c for c, p in catalogue.CATALOGUE.items() if c.startswith("payroll.") and p.agent_ok]  # R38
    assert "payroll.reverse_run" in catalogue.REVERSE_PERMISSIONS
    assert Role.objects.get(code="department_head").scope == "entity"


def test_v14_seed_final_settlement_rights(db):
    """C-A11: the payroll officer prepares final settlements; the approver approves (= posts) them, the MP as
    fallback; the preparer never holds the approval."""
    perms = {code: set(row[5]) for code, row in ROLE_SEED_V14.items()}
    assert {c for c, p in perms.items() if "payroll.prepare_final_settlement" in p} == {"payroll_officer"}
    assert {c for c, p in perms.items() if "payroll.approve_final_settlement" in p} == {"approver", "managing_partner"}
    flags = catalogue.get("payroll.approve_final_settlement").flags
    assert catalogue.APPROVAL in flags and catalogue.POSTS in flags
    assert set(Role.objects.get(code="approver").permission_rows.values_list("codename", flat=True)) >= {
        "payroll.approve_final_settlement"}


def test_approved_payroll_accounts_in_new_charts(db):
    from core.models import Entity
    from ledger.services_setup import install_chart_of_accounts
    e = Entity.objects.create(code="NEWP", name_en="New", name_ar="جديد")
    install_chart_of_accounts(e)
    codes = set(Account.objects.filter(entity=e).values_list("code", flat=True))
    assert {"1230", "1240", "2330", "2340", "5120", "5930"} <= codes
    fines = Account.objects.get(entity=e, code="5930")  # E14: an ordinary expense, never interest or purified (R37)
    assert (fines.type, fines.sharia_prohibited, fines.requires_purification) == ("expense", False, False)
    assert fines.name_ar == "غرامات تأخير التأمينات"


def test_e14_migration_adds_5930_to_existing_charts(db):
    import importlib

    from django.apps import apps
    from core.models import Entity
    from ledger.services_setup import install_chart_of_accounts
    e = Entity.objects.create(code="OLDP", name_en="Old", name_ar="قديم")
    install_chart_of_accounts(e)
    Account.objects.filter(entity=e, code="5930").delete()  # a chart from before the template had it
    importlib.import_module("payroll.migrations.0012_gosi_fines_account").forwards(apps, None)
    row = Account.objects.get(entity=e, code="5930")
    assert row.parent.code == "5000" and row.is_postable and row.type == "expense"
    assert date  # noqa


def test_v16_seed_payroll_slice2_rights(db):
    """Slice 2 (§10.8): preparers prepare, approvers approve; the preparer never holds the approval; loan write-offs
    by the group parent's top authority (MP); no agent or system right (R38)."""
    perms = {code: set(row[5]) for code, row in ROLE_SEED_V16.items()}
    for prepare, approve, approvers in (
            ("payroll.prepare_settlement_correction", "payroll.approve_settlement_correction", {"approver", "managing_partner"}),
            ("payroll.prepare_loan_write_off", "payroll.approve_loan_write_off", {"managing_partner"}),
            ("payroll.manage_cost_items", "payroll.approve_cost_item", {"approver", "managing_partner"}),
            ("payroll.prepare_cost_report", "payroll.approve_cost_report", {"approver", "managing_partner"}),
            ("payroll.record_gosi_fine", "payroll.approve_gosi_fine", {"approver", "managing_partner"})):
        assert {c for c, p in perms.items() if prepare in p} == {"payroll_officer"}, prepare
        assert {c for c, p in perms.items() if approve in p} == approvers, approve
        assert catalogue.APPROVAL in catalogue.get(approve).flags
    assert {c for c, p in perms.items() if "payroll.reverse_payment" in p} == {"approver"}
    assert "payroll.reverse_payment" in catalogue.REVERSE_PERMISSIONS
    assert {c for c, p in perms.items() if "payroll.prepare_profit_recognition" in p} == {"payroll_officer", "accountant"}
    assert not any(c.startswith("payroll.") for r, p in perms.items() if r.startswith("system_") for c in p)
    assert not [c for c, p in catalogue.CATALOGUE.items() if c.startswith("payroll.") and p.agent_ok]
