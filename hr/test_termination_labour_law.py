"""hr R37 (Art. 75, 77, 80, 81, 87; mutual agreement), R59 / R60 (Art. 82, 155 acknowledgement), R64 (worker
initiated, Art. 88 deadline), H17 / H18 (WPS scope, loans from final dues) (AT-36, AT-38)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from hr import services
from hr.conftest import employee, hired
from hr.models import EosbCalculation, FinalSettlementInput, HrError
from hr.payroll_port import LoanBalances

D = Decimal
LWD = date(2027, 6, 30)


def _record(w, emp, kind="employer_termination", **fields):
    fields.setdefault("reason_ar", "سبب الإنهاء")
    if kind != "resignation":
        fields.setdefault("last_working_day", LWD)
    if kind in ("employer_termination", "employee_notice_art75"):
        fields.setdefault("notice_given_on", fields["last_working_day"] - timedelta(days=60))
    return services.record_termination(emp, user=w.h, kind=kind, **fields)


def _approve(w, emp, kind="employer_termination", ack=False, **fields):
    t = services.submit_termination(_record(w, emp, kind, **fields), user=w.h)
    return services.approve_termination(t, user=w.c, acknowledge_protection=ack)


def _leave(w, emp, code, start, end, **fields):
    req = services.request_leave(emp, code, user=w.h, start_date=start, end_date=end, **fields)
    return services.approve_leave(req, user=w.h2)


# --- R59, R60 / AT-38 -----------------------------------------------------------------------------------------

def test_at38_art82_warning_and_stored_acknowledgement(w):
    e1 = hired(w, hire_date=date(2025, 1, 1))
    _leave(w, e1, "SICK", date(2027, 5, 1), date(2027, 5, 10), document_ref="MED-1")  # 10 sick days in the window
    t = _record(w, e1)
    assert t.protection_articles == ["Art. 82"]
    assert any("Art. 82" in x and "المادة 82" in x for x in t.warnings)
    t = services.submit_termination(t, user=w.h)
    with pytest.raises(HrError) as exc:
        services.approve_termination(t, user=w.c)
    assert exc.value.code == "protection_ack_required" and "Art. 82" in exc.value.messages[0]
    t.refresh_from_db()
    assert t.status == "submitted" and t.protection_acknowledged_by is None
    t = services.approve_termination(t, user=w.c, acknowledge_protection=True)
    t.refresh_from_db()
    assert t.status == "approved" and t.protection_acknowledged_by == w.c and t.protection_acknowledged_at
    assert t.protection_articles == ["Art. 82"]


def test_at38_art155_warning_for_maternity_leave(w):
    e2 = hired(w, gender="female")
    _leave(w, e2, "MATERNITY", date(2027, 5, 20), date(2027, 8, 11), event_date=date(2027, 6, 10),
           document_ref="MAT-1")
    t = _record(w, e2, last_working_day=date(2027, 8, 31))
    assert t.protection_articles == ["Art. 155"]
    assert any("Art. 155" in x and "المادة 155" in x for x in t.warnings)
    t = services.submit_termination(t, user=w.h)
    with pytest.raises(HrError) as exc:
        services.approve_termination(t, user=w.c)
    assert exc.value.code == "protection_ack_required" and "Art. 155" in exc.value.messages[0]
    t = services.approve_termination(t, user=w.c, acknowledge_protection=True)
    assert t.protection_acknowledged_by == w.c


def test_r60_pregnancy_related_illness_and_art80_also_warn(w):
    e = hired(w, gender="female")
    _leave(w, e, "SICK", date(2027, 6, 20), date(2027, 6, 25), document_ref="MED-P",
           declarations={"pregnancy_related": True})
    t = _record(w, e, "art80_dismissal", article_paragraph="1", hearing_ref="HEARING-1")
    assert set(t.protection_articles) == {"Art. 82", "Art. 155"}


def test_r59_no_warning_after_the_sick_year_or_for_worker_kinds(w):
    old = hired(w, hire_date=date(2025, 1, 1))
    _leave(w, old, "SICK", date(2026, 1, 5), date(2026, 1, 6), document_ref="MED-1")  # window ended 2027-01-04
    t = _approve(w, old)
    assert t.status == "approved" and t.protection_articles == [] and t.protection_acknowledged_by is None
    worker = hired(w, hire_date=date(2025, 1, 1))
    _leave(w, worker, "SICK", date(2027, 5, 1), date(2027, 5, 3), document_ref="MED-2")
    t = _record(w, worker, "employee_notice_art75", notice_given_on=LWD - timedelta(days=30))
    assert t.protection_articles == []


# --- R37 / AT-36 ------------------------------------------------------------------------------------------------

def test_at36_notice_monthly_and_not_monthly(w):
    monthly = hired(w, hire_date=date(2020, 1, 1))
    t = _record(w, monthly, notice_given_on=LWD - timedelta(days=45))
    assert t.notice_shortfall_days == 15 and any("15 day(s) short of 60" in x for x in t.warnings)
    weekly = hired(w, hire_date=date(2020, 1, 1), contract_kwargs={"paid_monthly": False})
    t = _record(w, weekly, "employee_notice_art75", notice_given_on=LWD - timedelta(days=30))
    assert t.notice_shortfall_days == 0
    weekly2 = hired(w, hire_date=date(2020, 1, 1), contract_kwargs={"paid_monthly": False})
    t = _record(w, weekly2, notice_given_on=LWD - timedelta(days=30))
    assert t.notice_shortfall_days == 0  # 30 days either side when not paid monthly (Art. 75)


def test_at36_art77_minimum_two_months_unless_referenced(w):
    emp = hired(w, hire_date=date(2020, 1, 1))  # actual wage 8,000.00
    with pytest.raises(HrError) as exc:
        _record(w, emp, art77_compensation="8000.00")
    assert "16000.00" in exc.value.messages[0] and "Art. 77" in exc.value.messages[0]
    with pytest.raises(HrError):
        _record(w, emp, art77_compensation=8000.0)  # float refused
    t = _approve(w, emp, art77_compensation="8000.00", compensation_ref="Labour court judgment 1448/77")
    assert t.art77_compensation == D("8000.00")
    assert FinalSettlementInput.objects.get(termination=t).art77_compensation == D("8000.00")
    other = hired(w, hire_date=date(2020, 1, 1))
    assert _record(w, other, art77_compensation="16000.00").pk
    third = hired(w, hire_date=date(2020, 1, 1))
    with pytest.raises(HrError):
        _record(w, third, "art80_dismissal", article_paragraph="1", hearing_ref="H-1", art77_compensation="20000.00")


def test_at36_mutual_agreement_consent_and_fraction(w):
    emp = hired(w, saudi=False)
    with pytest.raises(HrError):
        _record(w, emp, "mutual_agreement")
    with pytest.raises(HrError) as exc:
        _record(w, emp, "mutual_agreement", written_consent_ref="CONSENT-1", agreed_fraction="0.5")
    assert "OQ-34" in exc.value.messages[0]
    t = _record(w, emp, "mutual_agreement", written_consent_ref="CONSENT-1", agreed_fraction="1")
    assert t.agreed_fraction == 1


def test_at36_art80_paragraph_hearing_and_absence_warning(w):
    emp = hired(w, hire_date=date(2020, 1, 1))
    with pytest.raises(HrError):
        _record(w, emp, "art80_dismissal", article_paragraph="1")  # no hearing reference
    with pytest.raises(HrError):
        _record(w, emp, "art80_dismissal", hearing_ref="H-1")  # no paragraph
    with pytest.raises(HrError):
        _record(w, emp, "art80_dismissal", article_paragraph="10", hearing_ref="H-1")  # 9 cases only
    with pytest.raises(HrError) as exc:
        _record(w, emp, "art80_dismissal", article_paragraph="7", hearing_ref="H-1")
    assert "written warning" in exc.value.messages[0]
    t = _approve(w, emp, "art80_dismissal", article_paragraph="7", hearing_ref="H-1", absence_warning_ref="WARN-20")
    assert t.status == "approved" and EosbCalculation.objects.get(termination=t).award == 0


def test_at36_art81_paragraph(w):
    emp = hired(w, hire_date=date(2020, 1, 1))
    with pytest.raises(HrError):
        _record(w, emp, "art81_employee_leaves")
    with pytest.raises(HrError):
        _record(w, emp, "art81_employee_leaves", article_paragraph="8")
    assert _record(w, emp, "art81_employee_leaves", article_paragraph="2").article_paragraph == "2"


def test_at36_art87_windows(w):
    her = hired(w, gender="female")
    with pytest.raises(HrError):
        _record(w, her, "female_marriage_childbirth")  # no date
    with pytest.raises(HrError) as exc:
        _record(w, her, "female_marriage_childbirth", marriage_contract_date=date(2026, 11, 30))  # 7 months
    assert "6 months" in exc.value.messages[0]
    with pytest.raises(HrError):
        _record(w, her, "female_marriage_childbirth", delivery_date=date(2027, 3, 15))  # > 3 months
    assert _record(w, her, "female_marriage_childbirth", marriage_contract_date=date(2027, 1, 15)).pk
    him = hired(w)
    with pytest.raises(HrError):
        _record(w, him, "female_marriage_childbirth", marriage_contract_date=date(2027, 1, 15))
    other = hired(w, gender="female")
    assert _record(w, other, "female_marriage_childbirth", delivery_date=date(2027, 5, 1)).pk


def test_at36_art75_worker_notice_award_carries_the_legal_flag(w):
    emp = hired(w, hire_date=date(2023, 7, 1))  # 4 years: the fourth anniversary is the day after LWD
    t = _approve(w, emp, "employee_notice_art75", notice_given_on=LWD - timedelta(days=30))
    snap = EosbCalculation.objects.get(termination=t)
    assert snap.fraction_text == "1/3" and snap.award == D("5333.33")
    assert any("OQ-33" in x for x in snap.legal_flags) and any("OQ-33" in x for x in t.warnings)
    preview = services.calculate_eosb(emp, LWD, "employer_termination", user=w.h)
    assert preview.legal_flags == ()


# --- R64, H17, H18: payroll hand-offs -----------------------------------------------------------------------

@pytest.mark.parametrize("kind, fields, worker", [
    ("employer_termination", {}, False),
    ("employee_notice_art75", {"notice_given_on": LWD - timedelta(days=30)}, True),
    ("art81_employee_leaves", {"article_paragraph": "1"}, True),
    ("mutual_agreement", {"written_consent_ref": "CONSENT-1"}, False),
])
def test_r64_worker_initiated_flag_and_art88_deadline(w, kind, fields, worker):
    emp = hired(w, hire_date=date(2020, 1, 1))
    t = _approve(w, emp, kind, **fields)
    si = FinalSettlementInput.objects.get(termination=t)
    assert t.worker_initiated is worker and si.worker_initiated is worker
    assert si.due_by == LWD + timedelta(days=14 if worker else 7)  # Art. 88
    assert si.wps_included


def test_r64_resignation_and_art87_are_worker_initiated(w):
    emp = hired(w, saudi=False)
    t = _approve(w, emp, "resignation", submitted_on=date(2027, 4, 1))
    assert t.worker_initiated and t.deemed_accepted  # no reply within 30 days (Art. 79 bis)
    assert FinalSettlementInput.objects.get(termination=t).due_by == t.last_working_day + timedelta(days=14)
    her = hired(w, gender="female")
    assert _record(w, her, "female_marriage_childbirth", delivery_date=date(2027, 5, 1)).worker_initiated


def test_art79bis_acceptance_before_day_30_is_not_deemed_and_withdrawal_date_is_kept(w):
    emp = hired(w, saudi=False)
    t = _approve(w, emp, "resignation", submitted_on=date(2027, 5, 25))
    assert t.accepted_on == date(2027, 6, 1) and not t.deemed_accepted
    other = hired(w, saudi=False)
    t = _record(w, other, "resignation", submitted_on=date(2027, 5, 28))
    t, _ = services.withdraw_termination(t, user=w.h, reason_ar="تراجع")
    assert t.withdrawn_on == date(2027, 6, 1)


def test_h18_loan_balances_from_payroll_go_on_the_settlement_input(w):
    emp = hired(w, hire_date=date(2020, 1, 1))
    w.payroll.balances[emp.pk] = LoanBalances(loans=D("12000.00"), bai_ajel=D("3000.00"), bai_ajel_undertaking=True)
    t = _approve(w, emp)
    si = FinalSettlementInput.objects.get(termination=t)
    assert si.outstanding_loans == D("12000.00") and si.outstanding_bai_ajel == D("3000.00")
    assert si.bai_ajel_undertaking
    plain = hired(w, hire_date=date(2020, 1, 1))
    si = FinalSettlementInput.objects.get(termination=_approve(w, plain))
    assert si.outstanding_loans == 0 and si.outstanding_bai_ajel == 0 and not si.bai_ajel_undertaking


def test_h18_default_port_reports_no_balances(db):
    from hr.payroll_port import NoPayrollPort

    assert NoPayrollPort().outstanding_balances(None, LWD) == LoanBalances()


def test_h17_remuneration_staff_outside_wps_and_without_labour_law_deadline(w):
    r1 = hired(w, basis="remuneration")
    assert not r1.wps_included
    t = _approve(w, r1, "end_of_remuneration")
    si = FinalSettlementInput.objects.get(termination=t)
    assert not si.wps_included and si.due_by is None and not si.worker_initiated
    local = employee(w, w.egy, basis="local_entity_employee")
    assert not local.wps_included and hired(w).wps_included


# --- Finding 11: spec rules that were missing ----------------------------------------------------------------

def test_at42_retirement_before_the_gosi_legal_age_warns_and_needs_acknowledgement(w):
    e7 = hired(w, date_of_birth=date(1967, 1, 1), gosi_cohort="new_m273", gosi_first_coverage_date=date(1990, 1, 1))
    t = _record(w, e7, "retirement")
    assert t.protection_articles == ["SIL Art. 16(1)"]
    assert any("SIL Art. 16(1)" in x and "65" in x and "OQ-40" in x for x in t.warnings)
    t = services.submit_termination(t, user=w.h)
    with pytest.raises(HrError) as exc:
        services.approve_termination(t, user=w.c)
    assert exc.value.code == "protection_ack_required"
    assert services.approve_termination(t, user=w.c, acknowledge_protection=True).status == "approved"
    e8 = hired(w, date_of_birth=date(1972, 1, 1), gosi_cohort="legacy_m33", gosi_first_coverage_date=date(1995, 1, 1))
    t8 = _record(w, e8, "retirement")
    assert t8.protection_articles == ["SIL M/33 Art. 38(1)(a)"]
    assert any("Clause Fifth" in x for x in t8.warnings) and any("not verified" in x for x in t8.warnings)
    n2 = hired(w, saudi=False, date_of_birth=date(1967, 1, 1))
    tn = _record(w, n2, "retirement")
    assert tn.protection_articles == [] and any("No GOSI legal-age check" in x for x in tn.warnings)
    older = hired(w, date_of_birth=date(1960, 1, 1), gosi_cohort="new_m273", gosi_first_coverage_date=date(1985, 1, 1))
    assert _record(w, older, "retirement").protection_articles == []


def _map_resignation_only():
    row = services.param("SA", "gosi.exclusion_reason_map", LWD)
    row.value = {"resignation": "GOSI-RESIGN"}  # test fixture: the real list is not supplied (OQ-42)
    row.save()


def test_at45_gosi_exclusion_checklist_reminders_and_closing(w):
    from hr import jobs
    from hr.models import HrAlert, HrChecklistItem

    _map_resignation_only()
    e1 = hired(w, saudi=False, gosi_number="111222333")
    w.clock.set(date(2027, 5, 11))
    t = services.submit_termination(_record(w, e1, "resignation", submitted_on=date(2027, 5, 11)), user=w.h)
    t = services.approve_termination(t, user=w.c)
    assert t.last_working_day == date(2027, 5, 11) and t.gosi_exclusion_reason == "GOSI-RESIGN"
    item = HrChecklistItem.objects.get(termination=t)
    assert item.due_date == date(2027, 5, 31) and "GOSI-RESIGN" in item.text_en and "GOSI-RESIGN" in item.text_ar
    e1b = hired(w, gosi_number="111222334")
    tb = _approve(w, e1b, "employer_termination")
    itemb = HrChecklistItem.objects.get(termination=tb)
    assert tb.status == "approved" and tb.gosi_exclusion_reason == "" and "not mapped" in itemb.text_en
    e3 = hired(w)
    t3 = _approve(w, e3, "employer_termination")
    assert not HrChecklistItem.objects.filter(termination=t3).exists()
    assert jobs.gosi_exclusion_reminders(today=date(2027, 5, 24)) == 0
    assert jobs.gosi_exclusion_reminders(today=date(2027, 5, 25)) == 1
    assert jobs.gosi_exclusion_reminders(today=date(2027, 5, 25)) == 0  # once a day
    assert jobs.gosi_exclusion_reminders(today=date(2027, 5, 31)) == 1
    assert {a.recipient.username for a in HrAlert.objects.filter(kind="gosi_exclusion")} == {"h", "h2"}
    with pytest.raises(HrError):  # unmapped: the reason used is required
        services.record_gosi_exclusion(tb, user=w.h, excluded_on=date(2027, 6, 30), reference="GOSI-REF-2")
    services.record_gosi_exclusion(tb, user=w.h, excluded_on=date(2027, 6, 30), reference="GOSI-REF-2",
                                   reason="END-OF-SERVICE")
    with pytest.raises(HrError):  # a reason other than the mapped one
        services.record_gosi_exclusion(t, user=w.h, excluded_on=date(2027, 5, 20), reference="R", reason="OTHER")
    t = services.record_gosi_exclusion(t, user=w.h, excluded_on=date(2027, 5, 20), reference="GOSI-REF-1")
    assert (t.gosi_excluded_on, t.gosi_exclusion_ref) == (date(2027, 5, 20), "GOSI-REF-1")
    assert HrChecklistItem.objects.get(pk=item.pk).status == "closed"
    with pytest.raises(HrError):
        services.record_gosi_exclusion(t, user=w.h, excluded_on=date(2027, 5, 20), reference="AGAIN")
    assert jobs.gosi_exclusion_reminders(today=date(2027, 6, 30)) == 0  # closed: no more reminders


def test_r65_unverified_map_is_not_used_when_verified_rows_are_required(w, settings):
    _map_resignation_only()
    e1 = hired(w, saudi=False, gosi_number="111222335")
    t = services.submit_termination(_record(w, e1, "resignation", submitted_on=date(2027, 5, 25)), user=w.h)
    for row in services.StatutoryParameter.objects.filter(jurisdiction="SA", status="unverified").exclude(
            code="gosi.exclusion_reason_map"):
        services.verify_statutory_parameter(row, user=w.mp)
    settings.HR_REQUIRE_VERIFIED_PARAMETERS = True
    t = services.approve_termination(t, user=w.c)  # not refused because of the unverified map
    assert t.status == "approved" and t.gosi_exclusion_reason == ""


def test_r60_recorded_pregnancy_without_leave_warns(w):
    from hr.models import PregnancyNotice

    e = hired(w, gender="female")
    with pytest.raises(HrError):
        services.record_pregnancy(hired(w), user=w.h, document_ref="MED-P", notified_on=date(2027, 5, 1))
    notice = services.record_pregnancy(e, user=w.h, document_ref="MED-P1", notified_on=date(2027, 5, 1))
    t = _record(w, e)
    assert "Art. 155" in t.protection_articles
    with pytest.raises(HrError):
        PregnancyNotice.objects.filter(pk=notice.pk).update(ended_on=date(2027, 5, 2))
    services.end_pregnancy_notice(notice, user=w.h, ended_on=date(2027, 5, 15))
    services.withdraw_termination(t, user=w.h, reason_ar="سحب")
    # Notice given after the pregnancy ended: no Art. 155 warning.
    assert "Art. 155" not in _record(w, e, notice_given_on=date(2027, 5, 20)).protection_articles


def test_r37c_art78_weekly_paid_absence_during_employer_notice(w):
    e = hired(w, hire_date=date(2025, 1, 1))
    with pytest.raises(HrError):  # no employer notice yet
        _leave(w, e, "NOTICE_ABSENCE", date(2027, 5, 5), date(2027, 5, 5))
    _record(w, e, notice_given_on=date(2027, 5, 1))
    first = _leave(w, e, "NOTICE_ABSENCE", date(2027, 5, 3), date(2027, 5, 3))
    assert first.days == 1 and not first.unpaid
    with pytest.raises(HrError) as exc:  # a second day in the same week (May 1 to 7)
        _leave(w, e, "NOTICE_ABSENCE", date(2027, 5, 6), date(2027, 5, 6))
    assert "Art. 78" in exc.value.messages[0]
    _leave(w, e, "NOTICE_ABSENCE", date(2027, 5, 9), date(2027, 5, 9))  # next week
    with pytest.raises(HrError):  # after the last working day
        _leave(w, e, "NOTICE_ABSENCE", date(2027, 7, 1), date(2027, 7, 1))
    assert services.notice_absence_days(e, date(2027, 5, 1), date(2027, 5, 31), user=w.p) == [date(2027, 5, 3),
                                                                                              date(2027, 5, 9)]


def test_r39_snapshot_takes_payroll_regular_additions(w):
    from hr.payroll_port import AdditionalWage

    e = hired(w, hire_date=date(2023, 7, 1))  # 4 years on LWD
    w.payroll.additional_wage[e.pk] = AdditionalWage(amount=D("1000.00"), parts={"COMMISSION": "1000.00"})
    t = _approve(w, e)
    snap = EosbCalculation.objects.get(termination=t)
    assert snap.wage_basis == D("9000.00") and snap.wage_components["additional:COMMISSION"] == "1000.00"
    assert snap.award == D("18000.00")  # 4 x 1/2 x 9,000
