"""Payroll slice 3, item 2 (docs/specs/payroll.md §11): leave liability and EOSB provision reports, stored snapshots,
read-only, nothing posted (R75 to R78; AT-79 to AT-82); and the V17 permissions (11.7)."""
from datetime import date, datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404

from core.access import catalogue
from core.access.models import AccessLog
from core.access.seed import ROLE_SEED_V17
from core.access.testing import make_user, member
from hr import services as hr
from hr.models import LeaveLedger, LeaveType
from ledger.models import JournalEntry
from payroll import services
from payroll.conftest import D, approved, hired, saudi
from payroll.models import (EosbProvisionEntry, PayrollError, PayrollLiabilityReport, PayrollLiabilityReportLine)
from payroll.test_slice2_egypt import eg, egyptian  # noqa: F401  (the Egypt fixture)


def remuneration(w, **fields):
    return hired(w, basis="remuneration", **fields)


def ledger_row(emp, days, on, kind="accrual", **fields):
    """A leave ledger row as hr writes it (append-only; the monthly job's accrual by default)."""
    lt = LeaveType.objects.get(entity=emp.entity, code="ANNUAL")
    values = dict(period=f"{on:%Y-%m}", source_type="hr_accrue_leave") if kind == "accrual" else {}
    values.update(fields)
    return LeaveLedger.objects.create(entity=emp.entity, employee=emp, leave_type=lt, date=on, kind=kind,
                                      days=D(days), **values)


def line(report, emp):
    return report.lines.get(employee=emp)


# --- 11.7 permissions ------------------------------------------------------------------------------------------

def test_v17_seed_slice3_rights(db):
    perms = {code: set(row[5]) for code, row in ROLE_SEED_V17.items()}
    assert {c for c, p in perms.items() if "payroll.prepare_liability_report" in p} == {"payroll_officer", "accountant"}
    assert {c for c, p in perms.items() if "payroll.record_mudad_justification" in p} == {"payroll_officer"}
    assert {c for c, p in perms.items() if "payroll.approve_mudad_justification" in p} == {"hr_officer",
                                                                                          "managing_partner"}
    assert catalogue.get("payroll.approve_mudad_justification").is_approval
    assert not catalogue.get("payroll.prepare_liability_report").flags
    assert not any(c.startswith("payroll.") for r, p in perms.items() if r.startswith("system_") for c in p)
    assert not [c for c, p in catalogue.CATALOGUE.items() if c.startswith("payroll.") and p.agent_ok]  # R38


# --- R75, R76, R78: leave liability --------------------------------------------------------------------------------

def test_at79_leave_liability_values_ksa_excludes_remuneration_and_shows_egypt_days_unvalued(eg):
    w = eg
    s1 = saudi(w)
    r1 = remuneration(w)
    ledger_row(s1, "12.50", date(2027, 3, 31))
    w.pclock.set(datetime(2027, 4, 5, 12, 0))
    rows_before = LeaveLedger.objects.count()
    report = services.leave_liability_report(w.alm, date(2027, 3, 31), user=w.a)
    l = line(report, s1)
    assert (l.valuation, l.untaken_days, l.daily_wage, l.amount) == ("valued", D("12.50"), D("450.00"), D("5625.00"))
    assert l.pro_rata_days == 0 and l.future_leave_days == 0  # March was accrued on its last day
    assert line(report, r1).valuation == "excluded" and "no Saudi leave" in line(report, r1).reason_en
    assert line(report, r1).reason_ar
    assert report.total_amount == D("5625.00") and report.excluded == {"remuneration_leave": 1}
    # R78: the header states the date, the law basis, "nothing is posted", the exclusions and the parameters read.
    for text in ("2027-03-31", "Art. 2", "Art. 84", "Art. 111", "P19(h)", "nothing is posted", "hr OQ-6",
                 "Remuneration staff: no Saudi leave (P16): 1", "Parameters read"):
        assert text in report.basis_en
    assert "لا يُرحَّل شيء" in report.basis_ar and "النص العربي هو المعتمد" in report.basis_ar
    assert report.parameter_ids and report.sha256 and len(report.sha256) == 64
    assert not JournalEntry.objects.filter(entity=w.alm).exists() and LeaveLedger.objects.count() == rows_before
    assert not EosbProvisionEntry.objects.exists()
    # Egypt: days shown, not valued while leave.encashment_basis is empty (OQ-55).
    l1 = egyptian(w)
    # hr seeds no leave types for Egypt yet (OQ-26): the test entity gets its annual leave type as data.
    LeaveType.objects.create(entity=w.egy, code="ANNUAL", name_en="Annual leave", name_ar="الإجازة السنوية",
                             statutory=True, paid_rule="full", deducts_balance=True)
    ledger_row(l1, "15.00", date(2027, 3, 31), kind="opening")
    with pytest.raises(PermissionDenied):  # the hr_officer does not generate liability reports (11.7)
        services.leave_liability_report(w.egy, date(2027, 3, 31), user=w.he)
    eg = services.leave_liability_report(w.egy, date(2027, 3, 31), user=w.pe)
    e = line(eg, l1)
    assert (e.valuation, e.untaken_days, e.amount) == ("not_valued", D("15.00"), D("0"))
    assert "leave.encashment_basis" in e.reason_en and e.reason_ar


def test_at80_pro_rata_future_leave_dates_rights_and_scope(w, monkeypatch):
    s1 = saudi(w)
    ledger_row(s1, "15.00", date(2027, 4, 30))
    # The approved annual leave of 20 to 24 May has its taken row dated 2 May (a row dated before the leave, as a
    # migrated or advance booking would be): hr's own approval dates it on the first leave day.
    real = hr._resync_annual
    monkeypatch.setattr(hr, "_resync_annual", lambda a, use, j, user, on: real(a, use, j, user, date(2027, 5, 2)))
    req = hr.approve_leave(hr.request_leave(s1, "ANNUAL", user=w.h, start_date=date(2027, 5, 20),
                                            end_date=date(2027, 5, 24)), user=w.h2)
    monkeypatch.setattr(hr, "_resync_annual", real)
    assert req.days == D("5")
    w.pclock.set(datetime(2027, 5, 15, 12, 0))
    report = services.leave_liability_report(w.alm, date(2027, 5, 15), user=w.a)
    l = line(report, s1)
    assert (l.balance_days, l.pro_rata_days, l.future_leave_days) == (D("10.00"), D("0.85"), D("5.00"))
    assert l.untaken_days == D("15.85") and l.amount == D("7132.50")  # 15.85 x 450.00
    with pytest.raises(PayrollError) as exc:
        services.leave_liability_report(w.alm, date(2027, 12, 31), user=w.a)
    assert exc.value.code == "as_of_future" and " / " in exc.value.messages[0]
    viewer = services.liability_report_data(report, user=w.v)
    assert viewer["lines"] == [] and viewer["total_amount"] == D("7132.50") and not viewer["detail"]
    assert not AccessLog.objects.filter(details__action="liability_report_viewed").exists()
    detail = services.liability_report_data(report, user=w.c)
    assert [x.employee_code for x in detail["lines"]] == [s1.employee_code]
    assert AccessLog.objects.filter(details__action="liability_report_viewed", actor=w.c).exists()
    with pytest.raises(PermissionDenied):  # review 5 (R75): payslip rights alone never give the totals
        services.liability_report_data(report, user=w.p2)
    assert services.get_liability_report(w.v, report.pk) == report
    with pytest.raises(Http404):  # another entity's report
        services.get_liability_report(w.hs, report.pk)
    with pytest.raises(PermissionDenied):
        services.liability_report_data(report, user=w.hs)
    with pytest.raises(PermissionDenied):  # R38
        services.leave_liability_report(w.alm, date(2027, 5, 15), user=w.agent)
    html = services.render_liability_report(report, user=w.c)
    assert html.index("النص العربي هو المعتمد") < html.index("The Arabic text prevails")
    csv_ = services.render_liability_report(report, user=w.c, format_code="csv")
    assert s1.employee_code in csv_ and s1.full_name_en not in csv_ and s1.id_number not in csv_
    assert "7132.50" in services.render_liability_report(report, user=w.v, format_code="csv")
    assert s1.employee_code not in services.render_liability_report(report, user=w.v, format_code="csv")


def test_r76_hr_native_leave_dates_give_the_same_liability(w):
    """With hr's own dating (the taken row on the first leave day) the days stay in the balance instead: same
    total."""
    s1 = saudi(w)
    ledger_row(s1, "15.00", date(2027, 4, 30))
    hr.approve_leave(hr.request_leave(s1, "ANNUAL", user=w.h, start_date=date(2027, 5, 20),
                                      end_date=date(2027, 5, 24)), user=w.h2)
    w.pclock.set(datetime(2027, 5, 15, 12, 0))
    l = line(services.leave_liability_report(w.alm, date(2027, 5, 15), user=w.a), s1)
    assert (l.balance_days, l.pro_rata_days, l.future_leave_days, l.untaken_days) == (
        D("15.00"), D("0.85"), D("0"), D("15.85"))


def test_r75_reports_are_stored_never_edited_and_superseded(w):
    s1 = saudi(w)
    ledger_row(s1, "12.50", date(2027, 3, 31))
    w.pclock.set(datetime(2027, 4, 5, 12, 0))
    first = services.leave_liability_report(w.alm, date(2027, 3, 31), user=w.a)
    second = services.leave_liability_report(w.alm, date(2027, 3, 31), user=w.p)
    first = PayrollLiabilityReport.objects.get(pk=first.pk)
    assert (first.status, first.superseded_by_id, second.status) == ("superseded", second.pk, "generated")
    first.total_amount = D("1.00")
    first._service_write = True
    with pytest.raises(PayrollError) as exc:
        first.save()
    assert exc.value.code == "report_locked"
    with pytest.raises(PayrollError):
        second.delete()
    with pytest.raises(PayrollError):
        PayrollLiabilityReportLine.objects.filter(report=second).update(amount=D("0"))
    l = second.lines.get()
    with pytest.raises(PayrollError):
        l.delete()
    from django.db import connection, transaction
    sql = "UPDATE payroll_payrollliabilityreportline SET amount = 1 WHERE report_id = %s"
    if connection.vendor == "postgresql":  # the database refuses a change of a stored line (0021)
        with pytest.raises(Exception), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [second.pk])
    else:  # SQLite has no trigger: reading the tampered snapshot is refused by its hash
        with connection.cursor() as cursor:
            cursor.execute(sql, [second.pk])
        with pytest.raises(PayrollError) as exc:
            services.liability_report_data(second, user=w.c)
        assert exc.value.code == "report_changed"


# --- R77, R78: EOSB provision ------------------------------------------------------------------------------------

def test_at81_eosb_report_art84_award_art85_memo_subledger_and_nothing_posted(w):
    s1 = saudi(w, hire_date=date(2022, 1, 1))
    approved(w)  # March 2027: the EOSB accrual books 2500 and the sub-ledger
    entries = JournalEntry.objects.filter(entity=w.alm).count()
    w.pclock.set(datetime(2027, 4, 5, 12, 0))
    report = services.eosb_provision_report(w.alm, date(2027, 3, 31), user=w.a)
    l = line(report, s1)
    r = hr.calculate_eosb(s1, date(2027, 3, 31), "employer_termination", user=w.h)
    assert l.art84_full_award == r.full_award == l.amount
    # Review 9: exactly 5 years at half a month (33,750.00) + 90 days of the sixth year at a month
    # (13,500.00 x 90 / 365 = 3,328.77).
    assert l.art84_full_award == D("37078.77")
    assert l.art85_resignation_memo == D("24719.18")  # more than 5 years, under 10: 2/3
    memo = sum(e.amount for e in EosbProvisionEntry.objects.filter(employee=s1, date__lte=date(2027, 3, 31)))
    assert l.subledger_balance_memo == memo and memo > 0
    assert report.account_2500_memo == memo
    assert (l.service_start, l.excluded_days, l.wage_basis) == (date(2022, 1, 1), 0, D("13500.00"))
    assert JournalEntry.objects.filter(entity=w.alm).count() == entries  # nothing posted
    assert EosbProvisionEntry.objects.filter(employee=s1).count() == 1
    for text in ("Art. 84", "Art. 85", "memo", "nothing is posted", "Undiscounted"):
        assert text in report.basis_en


def test_at82_eosb_exclusions_not_valued_egypt_and_supersession(eg, monkeypatch):
    w = eg
    s1 = saudi(w)
    r1 = remuneration(w)
    s2 = saudi(w, id_number="1000000991")
    l1 = egyptian(w)
    from payroll.models import FinalSettlement
    real = services._settled
    monkeypatch.setattr(services, "_settled", lambda emp, as_of: emp.pk == s2.pk or real(emp, as_of))  # an approved settlement
    real_eosb = hr._compute_eosb

    def no_terms(emp, as_of, kind, use, additional_wage=D("0")):
        if emp.pk == s1.pk:
            raise hr.error("No approved contract is in force (test).", "لا يوجد عقد ساري (اختبار).", code="no_terms")
        return real_eosb(emp, as_of, kind, use, additional_wage)
    monkeypatch.setattr(hr, "_compute_eosb", no_terms)
    w.pclock.set(datetime(2027, 4, 5, 12, 0))
    first = services.eosb_provision_report(w.alm, date(2027, 3, 31), user=w.a)
    assert line(first, r1).valuation == "excluded" and "no end-of-service award" in line(first, r1).reason_en
    assert line(first, s2).valuation == "excluded" and "Final settlement approved" in line(first, s2).reason_en
    assert line(first, s1).valuation == "not_valued" and "No approved contract" in line(first, s1).reason_en
    assert first.excluded == {"remuneration_eosb": 1, "settled": 1}
    eg = services.eosb_provision_report(w.egy, date(2027, 3, 31), user=w.pe)
    assert line(eg, l1).valuation == "excluded" and eg.total_amount == 0
    assert "No statutory end-of-service award is configured" in eg.basis_en
    assert "لا توجد مكافأة نهاية خدمة نظامية مهيأة" in eg.basis_ar
    second = services.eosb_provision_report(w.alm, date(2027, 3, 31), user=w.a)
    first = PayrollLiabilityReport.objects.get(pk=first.pk)
    assert first.status == "superseded" and first.superseded_by_id == second.pk and second.status == "generated"
    assert FinalSettlement  # noqa


def test_r77_other_hr_refusals_stop_the_eosb_report(w, monkeypatch):
    saudi(w)

    def broken(emp, as_of, kind, use, additional_wage=D("0")):
        raise hr.error("Parameter missing (test).", "معيار مفقود (اختبار).", code="not_configured")
    monkeypatch.setattr(hr, "_compute_eosb", broken)
    w.pclock.set(datetime(2027, 4, 5, 12, 0))
    with pytest.raises(Exception) as exc:
        services.eosb_provision_report(w.alm, date(2027, 3, 31), user=w.a)
    assert getattr(exc.value, "code", None) == "not_configured"
    assert not PayrollLiabilityReport.objects.exists()


def test_r75_one_entity_per_report_and_no_rights_no_report(w):
    saudi(w)
    w.pclock.set(datetime(2027, 4, 5, 12, 0))
    with pytest.raises(PermissionDenied):  # a SUB payroll officer has no membership in ALM
        services.leave_liability_report(w.alm, date(2027, 3, 31), user=w.ps)
    with pytest.raises(PermissionDenied):  # the viewer reads totals but does not generate
        services.eosb_provision_report(w.alm, date(2027, 3, 31), user=w.v)
    outsider = make_user("outsider")
    member(outsider, w.sub, "accountant")
    report = services.leave_liability_report(w.sub, date(2027, 3, 31), user=outsider)
    assert report.entity_id == w.sub.pk and not report.lines.exists()


# --- review 6 and compliance C4 ----------------------------------------------------------------------------------

def test_review6_leaver_without_settlement_is_valued_due_and_settled_counts_from_its_last_day(w):
    from payroll.test_final_settlement import LWD, go_live, leaver, settle, terminate
    n1 = leaver(w)
    t = terminate(w, n1)  # employer termination, last working day 2027-03-15
    w.pclock.set(datetime(2027, 4, 5, 12, 0))
    report = services.eosb_provision_report(w.alm, date(2027, 3, 31), user=w.a)
    l = line(report, n1)
    award = hr.calculate_eosb(n1, LWD, "employer_termination", user=w.h).award
    assert (l.valuation, l.amount, l.art85_resignation_memo) == ("valued", award, None)
    assert "settlement pending" in l.reason_en and "معلقة" in l.reason_ar
    go_live(w)
    services.approve_final_settlement(settle(w, t), user=w.c)
    before = services.eosb_provision_report(w.alm, date(2027, 3, 10), user=w.a)  # still employed on that date
    assert line(before, n1).valuation == "valued" and line(before, n1).reason_en == ""
    after = services.eosb_provision_report(w.alm, date(2027, 3, 31), user=w.a)
    assert line(after, n1).valuation == "excluded" and "Final settlement approved" in line(after, n1).reason_en


def test_c4_regular_bonuses_and_in_kind_housing_join_the_eosb_and_leave_wage(w):
    s1 = saudi(w)
    s4 = saudi(w, id_number="1000000994", components={"BASIC": "8000.00", "TRANSPORT": "500.00"},
               contract_kwargs={"housing_in_kind": True})
    w.pclock.set(datetime(2027, 2, 20, 12, 0))
    inp = services.create_input(s1, user=w.p, kind="bonus", period="2027-02", amount="1200.00", reason_ar="مكافأة")
    services.approve_input(inp, user=w.p2)
    feb = approved(w, "2027-02")
    assert feb.payslips.get(employee=s1).lines.filter(line_type="bonus").exists()
    w.pclock.set(datetime(2027, 4, 5, 12, 0))
    report = services.eosb_provision_report(w.alm, date(2027, 3, 31), user=w.a)
    assert line(report, s1).wage_basis == D("13600.00")  # 13,500.00 + 1,200.00 / 12
    assert line(report, s4).wage_basis == D("10500.00")  # 8,500.00 + in-kind housing 8,000.00 x 3 / 12
    ledger_row(s1, "10.00", date(2027, 3, 31))
    leave = services.leave_liability_report(w.alm, date(2027, 3, 31), user=w.a)
    assert line(leave, s1).daily_wage == D("453.33")  # 13,600.00 / 30
