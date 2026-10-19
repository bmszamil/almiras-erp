"""Review fixes (2026-10-19): payments, reversals and settings (review items 1, 5, 12, 16, 17).

Ported from the reviewer's probes (test_zz_probes.py P5 and P8 neighbours) as regression tests; the PostgreSQL
concurrency cases are in test_pg_concurrency.py."""
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied

from core.access.models import AccessLog
from hr import services as hr
from hr.models import HrError
from ledger.models import Account
from payroll import services
from payroll.conftest import D, amount, approved, calculated, expat, saudi, slip
from payroll.models import EmployeeLoan, LoanInstallment, PayrollError, PayrollRun


def _loan(w, emp, principal, installments):
    loan = services.create_loan(emp, user=w.p, kind="loan", principal=principal, disbursement_date=date(2027, 2, 1),
                                purpose_ar="قرض حسن", installments=installments)
    return services.approve_loan(services.submit_loan(loan, user=w.p), user=w.mp)


def test_review5_reversal_refused_while_a_later_run_deducted_the_carried_remainder(w):
    """Probe P5: March deducts 800.00 of a 1,000.00 installment (10% cap), the 200.00 remainder is carried and a
    later run deducts it; reversing March would re-plan 1,000.00 and over-collect."""
    n1 = expat(w)
    loan = _loan(w, n1, "2000.00", [("2027-03", "1000.00"), ("2027-04", "1000.00")])
    mar = approved(w, "2027-03")
    assert amount(mar, n1, "loan_installment") == D("800.00")
    for period in ("2027-04", "2027-05", "2027-06"):
        approved(w, period)
    assert services._loan_outstanding_of(EmployeeLoan.objects.get(pk=loan.pk)) == 0
    with pytest.raises(PayrollError) as exc:
        services.reverse_run(mar, user=w.c, reason="error")
    assert exc.value.code == "carry_deducted" and " / " in exc.value.messages[0]
    assert PayrollRun.objects.get(pk=mar.pk).status == "approved"
    # Nothing was changed: the loan is still fully recovered, once.
    assert services._loan_outstanding_of(EmployeeLoan.objects.get(pk=loan.pk)) == 0


def test_review5_reversal_with_a_planned_carry_supersedes_it(w):
    n1 = expat(w)
    loan = _loan(w, n1, "1000.00", [("2027-03", "1000.00")])
    mar = approved(w, "2027-03")
    carry = LoanInstallment.objects.get(loan=loan, carried_from__isnull=False)
    assert carry.status == "planned" and carry.amount == D("200.00")
    services.reverse_run(mar, user=w.c, reason="error")
    assert LoanInstallment.objects.get(pk=carry.pk).status == "superseded"
    first = LoanInstallment.objects.get(loan=loan, carried_from__isnull=True)
    assert first.status == "planned" and first.deducted_amount == 0
    assert services._loan_outstanding_of(EmployeeLoan.objects.get(pk=loan.pk)) == D("1000.00")


def test_review12_reversal_after_a_wps_file_needs_a_logged_acknowledgement(w):
    saudi(w)
    run = approved(w)
    services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    with pytest.raises(PayrollError) as exc:
        services.reverse_run(run, user=w.c, reason="error")
    assert exc.value.code == "wps_file_exists"
    with pytest.raises(PayrollError):
        services.reverse_run(run, user=w.c, reason="error", acknowledge_wps=True, wps_reason="  ")
    run = services.reverse_run(run, user=w.c, reason="error", acknowledge_wps=True,
                               wps_reason="Bank confirmed the file was not processed")
    assert run.status == "reversed"
    log = AccessLog.objects.get(details__action="reverse_run_after_wps_file")
    assert log.actor == w.c and "not processed" in log.details["reason"]


def test_review1_reversal_refused_once_a_gosi_payment_covers_the_run(w):
    saudi(w)
    run = approved(w)
    p = services.record_payment(w.alm, user=w.p, kind="gosi", gosi_period="2027-03", payment_date=date(2027, 4, 10),
                                amount="100.00")
    services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    with pytest.raises(PayrollError) as exc:
        services.reverse_run(run, user=w.c, reason="error")
    assert exc.value.code == "run_gosi_paid"


def test_review1_payment_refused_for_a_reversed_run_even_if_recorded_before(w):
    s1 = saudi(w)
    run = approved(w)
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 3, 31),
                                amount=str(slip(run, s1).net))
    p = services.submit_payment(p, user=w.p)
    services.reverse_run(run, user=w.c, reason="error")
    with pytest.raises(PayrollError) as exc:
        services.approve_payment(p, user=w.c)
    assert exc.value.code == "run_status"


def test_review17_payment_bank_account_is_a_postable_asset_of_the_entity(w):
    s1 = saudi(w)
    run = approved(w)
    net = str(slip(run, s1).net)
    for bad in ("5100", Account.objects.get(entity=w.sub, code="1100")):
        with pytest.raises(PayrollError) as exc:
            services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 3, 31),
                                    amount=net, bank_account=bad)
        assert exc.value.code == "bank_account_invalid"


def test_review17_return_run_follows_the_run_matrix_row(w):
    saudi(w)
    run = services.submit_run(calculated(w), user=w.p)
    with pytest.raises(PermissionDenied):
        services.return_run(run, user=w.mp, reason_ar="تصحيح")  # the MP holds approve_run but not the matrix row
    assert services.return_run(run, user=w.h, reason_ar="تصحيح").status == "returned"


def test_review17_employer_bank_settings_changes_are_logged_masked(w):
    services.configure_settings(w.alm, user=w.c, employer_bank_iban="SA0380000000608010167519", salary_bank="alrajhi")
    log = AccessLog.objects.filter(details__action="payroll_bank_settings").latest("pk")
    assert set(log.details["fields"]) == {"employer_bank_iban", "salary_bank"}
    assert log.details["after"]["employer_bank_iban"] == "****7519"
    assert "SA0380000000608010167519" not in str(log.details)


def test_review17_a_local_entity_employee_left_out_is_a_run_warning(w, monkeypatch):
    n1 = expat(w)
    expat(w)
    members = services._members

    def with_local(ctx):  # inconsistent data that hr itself refuses: a local entity employee in a Saudi entity
        out = members(ctx)
        for emp in out:
            if emp.pk == n1.pk:
                emp.employment_basis = "local_entity_employee"
        return out

    monkeypatch.setattr(services, "_members", with_local)
    run = calculated(w)
    assert not run.payslips.filter(employee=n1).exists()
    assert any("1 local entity employee" in x for x in run.warnings)


def test_review16_only_expected_hr_refusals_become_eosb_warnings(w, monkeypatch):
    s1 = saudi(w)

    def refuse(code):
        def fn(*args, **kwargs):
            raise HrError("Refused (test). / مرفوض (اختبار).", code=code)
        return fn

    monkeypatch.setattr(hr, "calculate_eosb", refuse("no_terms"))
    run = calculated(w)
    assert any("EOSB provision not computed" in x for x in slip(run, s1).warnings)
    monkeypatch.setattr(hr, "calculate_eosb", refuse("jurisdiction_not_configured"))
    with pytest.raises(HrError):
        services.calculate_run(run, user=w.p)
