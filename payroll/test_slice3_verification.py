"""Payroll slice 3, verification review of the fix round (2026-10-21; payroll.md §11.12): the in-kind housing row
is never used unverified (R7), one wage for the EOSB, the leave report and the settlement's leave encashment, GOSI
on a back-dated raise of a remuneration month, whole-word health screening, the 0023 data steps."""
import importlib
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core.access.testing import make_user, member
from hr import services as hr
from hr.models import FinalSettlementInput, StatutoryParameter
from payroll import services
from payroll.conftest import D, amount, approved, rate, saudi, slip
from payroll.models import MudadJustification, PayrollError, PayrollInput
from payroll.test_slice3_off_cycle import SAUDI_ABROAD, off_cycle, remuneration

MIGRATION = importlib.import_module("payroll.migrations.0023_slice3_review_fixes")


def migration_apps():
    """The historical models as of payroll.0023 (no service guards), as the migration sees them."""
    return MigrationExecutor(connection).loader.project_state(("payroll", "0023_slice3_review_fixes")).apps


def verify_sa_except(w, *codes):
    mp = make_user("sa-verifier")
    member(mp, w.alm, "managing_partner")
    for row in StatutoryParameter.objects.filter(jurisdiction="SA", status="unverified").exclude(code__in=codes):
        hr.verify_statutory_parameter(row, user=mp)


def test_v1_unverified_in_kind_housing_never_enters_the_termination_award(w, settings):
    """With verified parameters required, an unverified eosb.in_kind_housing is not read into hr's award (which would
    refuse the approval, R7): in-kind housing counts 0 and the termination carries the bilingual warning."""
    from payroll.test_final_settlement import LWD
    s4 = saudi(w, id_number="1000000994", components={"BASIC": "8000.00", "TRANSPORT": "500.00"},
               hire_date=date(2023, 6, 1), contract_kwargs={"housing_in_kind": True})
    verify_sa_except(w, "eosb.in_kind_housing")
    settings.HR_REQUIRE_VERIFIED_PARAMETERS = True
    t = hr.record_termination(s4, user=w.h, kind="employer_termination", last_working_day=LWD, reason_ar="سبب",
                              notice_given_on=date(2027, 1, 14))
    t = hr.approve_termination(hr.submit_termination(t, user=w.h), user=w.c)
    housing = hr.param("SA", "eosb.in_kind_housing", LWD)
    snap = t.eosb_calculations.order_by("-pk").first() if hasattr(t, "eosb_calculations") else None
    from hr.models import EosbCalculation
    snap = snap or EosbCalculation.objects.filter(employee=s4).order_by("-pk").first()
    assert snap.wage_basis == D("8500.00") and housing.pk not in snap.parameter_ids
    assert any("eosb.in_kind_housing" in x for x in t.warnings)


def test_v2_one_wage_for_the_leave_report_and_the_settlement_encashment(w):
    """R34 and R76 use the same Art. 2 wage: the settlement's daily wage equals the leave report's."""
    from payroll.test_final_settlement import LWD, leaver, terminate
    n1 = leaver(w)
    w.pclock.set(datetime(2027, 2, 20, 12, 0))
    inp = services.create_input(n1, user=w.p, kind="bonus", period="2027-02", amount="2400.00",
                                reason_ar="مكافأة شهرية متفق عليها", eosb_regular=True)
    services.approve_input(inp, user=w.p2)
    approved(w, "2027-02")
    t = terminate(w, n1)
    si = FinalSettlementInput.objects.get(termination=t)
    w.pclock.set(datetime(2027, 3, 20, 12, 0))
    report = services.leave_liability_report(w.alm, LWD, user=w.a)
    line = report.lines.get(employee=n1)
    # 8,000.00 + 2,400.00 / 12 months employed in the window (March 2026 to February 2027) = 8,200.00 / 30.
    assert si.daily_wage == line.daily_wage == D("273.33")


def test_v3_regular_flag_defaults_to_not_regular(w):
    s1 = saudi(w)
    inp = services.create_input(s1, user=w.p, kind="bonus", period="2027-03", amount="100.00", reason_ar="مكافأة")
    assert PayrollInput.objects.get(pk=inp.pk).eosb_regular is False


def test_v4_backdated_raise_of_a_subordinate_saudi_charges_gosi_on_the_delta_at_the_month_rate(w):
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    r2 = remuneration(w, id_number="1888888811", gosi_employment_relationship="subordinate",
                      gosi_relationship_ref="AGREEMENT-R2", **SAUDI_ABROAD)
    reg = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-03",
                                                     payroll_group="out_of_country"), user=w.p)
    reg = services.approve_run(services.submit_run(reg, user=w.p), user=w.h)
    assert slip(reg, r2).gosi_wage == D("18750.00")
    from hr.conftest import contract
    contract(w, r2, {"REMUNERATION": "6000.00"}, effective_date=date(2027, 3, 1), start_date=r2.hire_date,
             currency="USD")
    off = services.calculate_run(off_cycle(w, "2027-03", [r2]), user=w.p)
    ps = slip(off, r2)
    assert amount(off, r2, "earning") == D("3750.00")  # USD 1,000.00 at 3.75
    assert amount(off, r2, "employer_gosi") == D("375.00") and ps.gosi_wage == D("3750.00")  # 10% of the delta
    assert set(ps.lines.exclude(gosi_branch="").values_list("gosi_branch", flat=True)) == {"annuities"}


@pytest.mark.parametrize("text, refused", [
    ("فرق ناتج عن تطبيق لائحة تنظيم العمل", False), ("دوام طبيعي بعد الإجازة", False),
    ("حسب التقرير الطبي", True), ("medical certificate", True), ("misdiagnosis", False), ("diagnosis", True),
])
def test_v6_health_words_match_whole_words_only(w, text, refused):
    s1 = saudi(w)
    ps = slip(approved(w), s1)
    if refused:
        with pytest.raises(PayrollError) as exc:
            services.record_mudad_justification(ps, user=w.p, kind="amount_difference", reason="other",
                                                description_ar=text)
        assert exc.value.code == "justification_invalid"
    else:
        assert services.record_mudad_justification(ps, user=w.p, kind="amount_difference", reason="other",
                                                   description_ar=text).status == "draft"


def test_v7_a_settlement_approved_after_the_date_does_not_close_it(w):
    from payroll.test_final_settlement import LWD, go_live, leaver, settle, terminate
    n1 = leaver(w)
    t = terminate(w, n1)
    go_live(w)
    w.pclock.set(datetime(2027, 4, 2, 12, 0))
    services.approve_final_settlement(settle(w, t), user=w.c)
    w.pclock.set(datetime(2027, 4, 5, 12, 0))
    on_lwd = services.eosb_provision_report(w.alm, LWD, user=w.a)  # last working day == as-at date
    l = on_lwd.lines.get(employee=n1)
    assert l.valuation == "valued" and "settlement pending" in l.reason_en
    assert l.amount == hr.calculate_eosb(n1, LWD, "employer_termination", user=w.h).award
    assert services.eosb_provision_report(w.alm, date(2027, 4, 2), user=w.a).lines.get(employee=n1).valuation == \
        "excluded"


def test_v5_migration_0023_fills_employees_on_final_rows_and_backfills_taken_inputs(w):
    s1 = saudi(w)
    n1 = saudi(w, id_number="1000000556", iban="")
    run = approved(w)
    services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    live = MudadJustification.objects.get(run=run, employee_code=n1.employee_code)
    old = migration_apps().get_model("payroll", "MudadJustification")
    # A final row from before 0023 (no employee), as an import would have left it.
    j = old.objects.create(entity_id=live.entity_id, payslip_id=live.payslip_id, run_id=run.pk,
                           employee_code=n1.employee_code, kind="non_payment", reason="other", expected_amount=D("1"),
                           paid_amount=D("0"), description_ar="مبرر مستورد: تقرير طبي", status="withdrawn",
                           source="import", created_by_id=w.p.pk)
    MIGRATION.fill_rows(migration_apps(), SimpleNamespace(connection=connection))
    filled = MudadJustification.objects.get(pk=j.pk)
    assert filled.employee_id == n1.pk and filled.health_flag is True and filled.status == "withdrawn"
    if connection.vendor == "postgresql":  # the exemption is the fill only: a final row stays final
        from django.db import DatabaseError, transaction
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("UPDATE payroll_mudadjustification SET description_ar = 'x' WHERE id = %s", [j.pk])
    # Backfill: a remuneration input paid before slice 3 recorded it.
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    r1 = remuneration(w)
    inp = services.approve_input(services.create_input(r1, user=w.p, kind="bonus", period="2027-03", amount="100.00",
                                                       reason_ar="مكافأة"), user=w.p2)
    out = approved(w, payroll_group="out_of_country")
    with connection.cursor() as cursor:
        cursor.execute("UPDATE payroll_payrollinput SET taken_run_id = NULL WHERE id = %s", [inp.pk])
    MIGRATION.backfill_taken_inputs(migration_apps(), None)
    assert PayrollInput.objects.get(pk=inp.pk).taken_run_id == out.pk
    assert s1
