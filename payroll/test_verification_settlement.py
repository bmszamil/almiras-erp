"""Payroll final settlement, verification review (2026-10-20), items 1 to 3; the reviewer's probes B1, B2 and B3
(test_zz_rr2probe.py) ported as regression tests.

Every date is fixed (the hr clock 2027-06-01, the payroll clock 2027-03-20, conftest); nothing reads the real date."""
import pytest

from payroll import services
from payroll.conftest import D, approved, saudi, slip
from payroll.models import FinalSettlement, PayrollError
from payroll.test_final_settlement import leaver, lines, settle, terminate

def _approve(w, fs):
    return services.approve_final_settlement(fs, user=w.c)


def _has_arabic(text):
    return any("؀" <= ch <= "ۿ" for ch in text)


def _again(w, fs):
    """Return, recalculate and resubmit a submitted settlement."""
    fs = services.return_final_settlement(fs, user=w.c, reason_ar="أعد الاحتساب")
    return services.submit_final_settlement(services.prepare_final_settlement(fs, user=w.p), user=w.p)


# --- 1. runs and the settlement (B1, B2, the skip) ---------------------------------------------------------------

def test_v1_b1_a_calculated_run_with_the_employee_refuses_the_settlement(w):
    """B1: February calculated with the leaver, then the settlement (paying February as retro), then February
    approved paid February twice. The settlement is now refused while the run is not approved."""
    n1 = leaver(w)
    t = terminate(w, n1)
    feb = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-02"), user=w.p)
    assert feb.payslips.filter(employee=n1).exists()
    fs = settle(w, t)
    with pytest.raises(PayrollError) as exc:
        _approve(w, fs)
    assert exc.value.code == "settlement_runs_open" and "2027-02" in str(exc.value) and _has_arabic(str(exc.value))
    assert FinalSettlement.objects.get(pk=fs.pk).status == "submitted"
    feb = services.approve_run(services.submit_run(feb, user=w.p), user=w.h)
    assert slip(feb, n1).gross == D("8000.00")
    with pytest.raises(PayrollError) as exc:  # February is now paid: the settlement no longer pays it
        _approve(w, fs)
    assert exc.value.code == "recalculate"
    fs = _approve(w, _again(w, fs))
    assert fs.status == "approved"
    assert lines(fs, "retro") == 0  # February paid once, by the run


def test_v1_a_calculated_leaving_month_run_with_the_employee_refuses_until_recalculated(w):
    """The leaving month calculated before the termination includes the leaver: refused until it is recalculated
    (the recalculation leaves the leaver out, R10) or approved."""
    saudi(w)
    n1 = leaver(w)
    approved(w, "2027-02")
    mar = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-03"), user=w.p)
    assert slip(mar, n1).gross == D("8000.00")
    fs = settle(w, terminate(w, n1))
    with pytest.raises(PayrollError) as exc:
        _approve(w, fs)
    assert exc.value.code == "settlement_runs_open" and "2027-03" in str(exc.value)
    mar = services.calculate_run(mar, user=w.p)
    assert not mar.payslips.filter(employee=n1).exists()
    fs = _approve(w, fs)
    assert fs.status == "approved" and fs.journal_entry is not None
    mar = services.approve_run(services.submit_run(mar, user=w.p), user=w.h)
    assert not mar.payslips.filter(employee=n1).exists()


def test_v1_a_returned_run_with_the_employee_refuses_the_settlement(w):
    n1 = leaver(w)
    approved(w, "2027-01")
    t = terminate(w, n1)
    feb = services.submit_run(services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-02"),
                                                     user=w.p), user=w.p)
    feb = services.return_run(feb, user=w.h, reason_ar="مراجعة")
    fs = settle(w, t)
    with pytest.raises(PayrollError) as exc:
        _approve(w, fs)
    assert exc.value.code == "settlement_runs_open" and "2027-02" in str(exc.value)


def test_v1_b2_no_approved_run_yet_refuses_the_settlement_until_the_first_run_is_approved(w):
    """B2: with no approved run the settlement was approved (paying earlier months as retro), and the entity's first
    run was then refused as a whole. Now the settlement waits for the first regular run (the go-live month)."""
    other = saudi(w)
    n1 = leaver(w)
    fs = settle(w, terminate(w, n1))
    with pytest.raises(PayrollError) as exc:
        _approve(w, fs)
    msg = str(exc.value)
    assert exc.value.code == "settlement_runs_open" and "no approved regular payroll run" in msg and _has_arabic(msg)
    assert FinalSettlement.objects.get(pk=fs.pk).status == "submitted" and fs.journal_entry_id is None
    feb = approved(w, "2027-02")  # go-live February: the first run pays both employees
    assert slip(feb, n1).gross == D("8000.00") and slip(feb, other).gross > 0
    fs = _approve(w, _again(w, fs))
    assert fs.status == "approved"


def test_v1_go_live_in_the_leaving_month_is_enough(w):
    saudi(w)
    n1 = leaver(w)
    t = terminate(w, n1)
    mar = approved(w, "2027-03")  # go-live March: the leaver is left out (R10)
    assert not mar.payslips.filter(employee=n1).exists()
    assert _approve(w, settle(w, t)).status == "approved"


def test_v1_a_run_skips_a_settled_employee_with_a_warning_instead_of_refusing_the_whole_run(w):
    other = saudi(w)
    n1 = leaver(w)
    t = terminate(w, n1)
    approved(w, "2027-02")
    fs = _approve(w, settle(w, t))
    code = n1.employee_code
    # A regular run of a month the settlement closed (January, before go-live) is calculated for the others.
    jan = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-01"), user=w.p)
    assert jan.status == "calculated"
    assert not jan.payslips.filter(employee=n1).exists() and jan.payslips.filter(employee=other).exists()
    assert any("approved final settlement" in x and _has_arabic(x) for x in jan.warnings)
    assert any(code in x and fs.number in x for x in jan.employee_notes)
    assert not any(code in x for x in jan.warnings)  # counts only on the run (review 13)
    jan = services.approve_run(services.submit_run(jan, user=w.p), user=w.h)
    assert not jan.payslips.filter(employee=n1).exists()
    # An off-cycle run naming the settled employee is calculated; the employee is left out (was: refused).
    off = services.create_run(w.alm, user=w.p, period="2027-02", kind="off_cycle", reason_ar="تصحيح", employees=[n1])
    off = services.calculate_run(off, user=w.p)
    assert off.status == "calculated" and not off.payslips.filter(employee=n1).exists()
    assert any("approved final settlement" in x for x in off.warnings)


def test_v1_a_calculated_run_before_go_live_with_the_employee_refuses_the_settlement(w):
    saudi(w)
    n1 = leaver(w)
    t = terminate(w, n1)
    approved(w, "2027-02")
    fs = settle(w, t)
    jan = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-01"), user=w.p)
    assert jan.payslips.filter(employee=n1).exists()
    with pytest.raises(PayrollError) as exc:
        _approve(w, fs)
    assert exc.value.code == "settlement_runs_open" and "2027-01" in str(exc.value)


def test_v1_a_run_calculated_with_the_employee_before_the_settlement_is_refused_at_its_approval(w, monkeypatch):
    """Defence in depth (a calculation racing the settlement approval): run approval recomputes the run, the settled
    employee is now left out, so the fingerprint differs and the run must be recalculated."""
    saudi(w)
    n1 = leaver(w)
    t = terminate(w, n1)
    approved(w, "2027-02")
    fs = settle(w, t)
    jan = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-01"), user=w.p)
    assert jan.payslips.filter(employee=n1).exists()
    jan = services.submit_run(jan, user=w.p)
    monkeypatch.setattr(services, "_settlement_months_check", lambda fs, emp: None)  # as if the run was not seen yet
    _approve(w, fs)
    with pytest.raises(PayrollError) as exc:
        services.approve_run(jan, user=w.h)
    assert exc.value.code == "recalculate"
    jan = services.calculate_run(services.return_run(jan, user=w.h, reason_ar="أعد الاحتساب"), user=w.p)
    assert not jan.payslips.filter(employee=n1).exists()
