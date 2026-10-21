"""Payroll slice 3, item 3 (docs/specs/payroll.md §11): Mudad justifications as data on run lines, their approval,
deadlines and the worksheet for the Mudad portal (R79 to R81 as amended by the compliance check of 2026-10-21: the
portal's drop-down, never the salary file; AT-83 to AT-85). Every portal option stays [verify]: the test fills the
parameter itself."""
from datetime import date, datetime

import pytest
from django.core.exceptions import PermissionDenied

from core.access.models import AccessLog
from core.access.testing import member
from hr import services as hr
from payroll import services
from payroll.conftest import D, approved, expat, hired, rate, saudi, slip
from payroll.models import MudadJustification, MudadJustificationExport, PayrollAlert, PayrollError

UNPAID_OPTION = {"ar": "إجازة بدون راتب", "en": "Unpaid leave"}


def unpaid_days(w, emp, days, period="2027-03"):
    inp = services.create_input(emp, user=w.p, kind="unpaid_days", period=period, quantity=str(days),
                                reason_ar="غياب بدون أجر")
    return services.approve_input(inp, user=w.p2)


def march(w):
    """AT-83 given: S1 with 3 unpaid days, N1 without a valid IBAN; the March run approved."""
    s1 = saudi(w)
    n1 = expat(w)  # no IBAN: a WPS exception
    unpaid_days(w, s1, 3)
    return s1, n1, approved(w)


def rows(run):
    return {(j.employee_code, j.reason): j for j in MudadJustification.objects.filter(run=run)}


def options_parameter(w, options):
    return hr.add_statutory_parameter(user=w.h, jurisdiction="SA", code="wps.mudad.justification_options",
                                      value={"options": options}, valid_from=date(2027, 1, 1),
                                      source_url="https://mudad.com.sa/", source_reference="test: options filled")


def test_at83_suggest_confirm_approve_and_worksheet_with_and_without_portal_options(w):
    s1, n1, run = march(w)
    f1 = services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    found = rows(run)
    assert set(found) == {(s1.employee_code, "unpaid_leave"), (n1.employee_code, "iban_exception")}
    j1, j2 = found[(s1.employee_code, "unpaid_leave")], found[(n1.employee_code, "iban_exception")]
    ps1 = slip(run, s1)
    assert (j1.kind, j1.expected_amount, j1.paid_amount) == ("amount_difference", D("13500.00"), ps1.gross)
    assert ps1.gross < D("13500.00")
    assert (j2.kind, j2.expected_amount, j2.paid_amount) == ("non_payment", D("8000.00"), D("0"))
    assert {j1.status, j2.status} == {"suggested"} and {j1.source, j2.source} == {"system"}
    assert j1.description_ar == "إجازة بدون أجر" and j1.mudad_option_ar == "" and j1.employee_id == s1.pk
    services.generate_wps_file(run, user=w.p, format_code="generic_csv")  # idempotent: one row per payslip, reason
    assert MudadJustification.objects.filter(run=run).count() == 2
    with pytest.raises(PayrollError) as exc:  # a suggestion is confirmed before it is approved
        services.approve_mudad_justification(j1, user=w.h)
    assert exc.value.code == "justification_status"
    j1 = services.confirm_mudad_justification(j1, user=w.p, evidence_ref="LEAVE-REQ-1")
    j2 = services.confirm_mudad_justification(j2, user=w.p)
    assert j1.status == j2.status == "draft"
    with pytest.raises(PermissionDenied):  # the payroll officer never approves (creator, last modifier, no right)
        services.approve_mudad_justification(j1, user=w.p)
    with pytest.raises(PermissionDenied):  # the matrix row is the hr_officer's; the MP's fallback is refused
        services.approve_mudad_justification(j1, user=w.mp)
    j1 = services.approve_mudad_justification(j1, user=w.h)
    j2 = services.approve_mudad_justification(j2, user=w.h)
    assert j1.status == j2.status == "approved" and j1.approved_by_id == w.h.pk and j1.mudad_option_ar == ""
    export = services.export_mudad_justifications(run, user=w.p)
    content = export.content
    assert export.row_count == 2 and s1.employee_code in content and n1.employee_code in content
    assert "إجازة بدون أجر" in content and "حساب بنكي غير صالح" in content and s1.id_number in content
    assert len(export.warnings) == 2 and all("portal option not configured" in x and "خيار بوابة مدد غير مهيأ" in x
                                             for x in export.warnings)
    assert AccessLog.objects.filter(details__action="mudad_justifications_exported", actor=w.p).exists()
    options_parameter(w, {"unpaid_leave": UNPAID_OPTION, "iban_exception": {"ar": "حساب غير صالح", "en": "Bad IBAN"}})
    again = services.export_mudad_justifications(run, user=w.p)
    assert "إجازة بدون راتب" in again.content and "Bad IBAN" in again.content and again.warnings == []
    assert MudadJustification.objects.get(pk=j1.pk).mudad_option_ar == "إجازة بدون راتب"
    html = services.export_mudad_justifications(run, user=w.p, format_code="html").content
    assert html.index("النص العربي هو المعتمد") < html.index("The Arabic text prevails")
    f3 = services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    assert f3.content == f1.content  # the salary file never carries justifications (C3)
    with pytest.raises(PayrollError):
        MudadJustificationExport.objects.get(pk=export.pk).delete()  # kept like WPS files (R31)


def test_r80_creator_last_modifier_and_employee_never_approve_and_no_health_detail(w):
    s1, n1, run = march(w)
    sh = saudi(w, id_number="1000000881", linked_user=w.h)  # the hr_officer's own pay
    member(w.h2, w.alm, "payroll_officer")  # an hr_officer who also records justifications
    run2 = approved(w, "2027-04", approver=w.h2)  # w.h has retro pay in it (own pay, R39)
    ps = slip(run2, sh)
    with pytest.raises(PayrollError) as exc:
        services.record_mudad_justification(ps, user=w.p, kind="amount_difference", reason="sick_leave_reduced",
                                            description_ar="تشخيص: التهاب رئوي")
    assert exc.value.code == "justification_invalid" and "health" in exc.value.messages[0]
    with pytest.raises(PayrollError) as exc:  # review 8: a surgery, in Arabic
        services.record_mudad_justification(ps, user=w.p, kind="amount_difference", reason="other",
                                            description_ar="فرق بسبب عملية جراحية")
    assert exc.value.code == "justification_invalid"
    with pytest.raises(PayrollError) as exc:  # review 8: the evidence reference is screened too
        services.record_mudad_justification(ps, user=w.p, kind="amount_difference", reason="other",
                                            description_ar="فرق في الأجر", evidence_ref="medical report 12")
    assert exc.value.code == "justification_invalid"
    with pytest.raises(PayrollError) as exc:  # 11.6: a reason takes its own kinds
        services.record_mudad_justification(ps, user=w.p, kind="late_payment", reason="unpaid_leave",
                                            description_ar="إجازة بدون أجر")
    assert exc.value.code == "justification_invalid"
    with pytest.raises(PayrollError) as exc:
        services.record_mudad_justification(ps, user=w.p, kind="amount_difference", reason="other", description_ar="")
    assert exc.value.code == "justification_invalid"
    own = services.record_mudad_justification(ps, user=w.p, kind="amount_difference", reason="other",
                                              description_ar="عملية تصحيح فرق في الأجر")  # "عملية" alone is fine
    assert own.status == "draft" and own.source == "manual" and own.expected_amount == D("13500.00")
    with pytest.raises(PermissionDenied):  # the employee never approves their own
        services.approve_mudad_justification(own, user=w.h)
    by_h2 = services.record_mudad_justification(slip(run, s1), user=w.h2, kind="amount_difference",
                                                reason="payroll_correction", description_ar="تصحيح")
    with pytest.raises(PermissionDenied):  # its creator never approves it
        services.approve_mudad_justification(by_h2, user=w.h2)
    assert services.approve_mudad_justification(by_h2, user=w.h).status == "approved"
    # Review 9: the last modifier never approves either (a suggestion created by the payroll officer).
    services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    sug = rows(run)[(n1.employee_code, "iban_exception")]
    sug = services.confirm_mudad_justification(sug, user=w.h2)
    assert sug.created_by_id == w.p.pk and sug.modified_by_id == w.h2.pk
    with pytest.raises(PermissionDenied):
        services.approve_mudad_justification(sug, user=w.h2)
    with pytest.raises(PayrollError) as exc:  # one live row per payslip and reason
        services.record_mudad_justification(slip(run, s1), user=w.p, kind="amount_difference",
                                            reason="payroll_correction", description_ar="تصحيح")
    assert exc.value.code == "justification_exists"
    with pytest.raises(PermissionDenied):  # R38: agents never record or approve
        services.record_mudad_justification(slip(run, s1), user=w.agent, kind="amount_difference", reason="other",
                                            description_ar="سبب")


def test_r80_review4_content_frozen_from_approval_and_final_after_answer(w):
    s1, n1, run = march(w)
    services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    j = rows(run)[(s1.employee_code, "unpaid_leave")]
    j = services.approve_mudad_justification(services.confirm_mudad_justification(j, user=w.p), user=w.h)
    stored = MudadJustification.objects.get(pk=j.pk)
    stored.description_ar = "نص معدل بعد الاعتماد"
    stored._service_write = True
    with pytest.raises(PayrollError) as exc:
        stored.save()
    assert exc.value.code == "justification_frozen"
    j = services.record_mudad_notice(j, user=w.p, notice_date=date(2027, 4, 12))  # dates still tracked
    assert j.due_date == date(2027, 4, 22)
    j = services.record_justification_submission(j, user=w.p, submitted_on=date(2027, 4, 14), mudad_reference="M-J1")
    stored = MudadJustification.objects.get(pk=j.pk)
    stored.mudad_option_ar = "خيار آخر"
    stored._service_write = True
    with pytest.raises(PayrollError) as exc:  # nothing but Mudad's answer after submission
        stored.save()
    assert exc.value.code == "justification_frozen"
    options_parameter(w, {"unpaid_leave": UNPAID_OPTION})
    services.export_mudad_justifications(run, user=w.p)  # a submitted row keeps its option as submitted
    assert MudadJustification.objects.get(pk=j.pk).mudad_option_ar == ""


def test_r80_submission_outcome_withdrawal_and_never_deleted(w):
    s1, n1, run = march(w)
    services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    j = rows(run)[(s1.employee_code, "unpaid_leave")]
    with pytest.raises(PayrollError):  # only approved rows are submitted
        services.record_justification_submission(j, user=w.p, submitted_on=date(2027, 4, 14), mudad_reference="M-J1")
    j = services.approve_mudad_justification(services.confirm_mudad_justification(j, user=w.p), user=w.h)
    j = services.record_justification_submission(j, user=w.p, submitted_on=date(2027, 4, 14), mudad_reference="M-J1")
    assert (j.status, j.worker_response_due) == ("submitted", date(2027, 4, 17))  # submission + 3 days (R53)
    with pytest.raises(PayrollError):
        services.withdraw_mudad_justification(j, user=w.p, reason_ar="خطأ")  # submitted: Mudad decides now
    j = services.record_justification_outcome(j, user=w.p, status="accepted", worker_response="agreed")
    assert (j.status, j.worker_response) == ("accepted", "agreed")
    with pytest.raises(PayrollError):  # final
        services.record_justification_outcome(j, user=w.p, status="rejected")
    with pytest.raises(PayrollError):
        MudadJustification.objects.get(pk=j.pk).delete()
    other = rows(run)[(n1.employee_code, "iban_exception")]
    with pytest.raises(PayrollError):
        services.withdraw_mudad_justification(other, user=w.p, reason_ar="")
    other = services.withdraw_mudad_justification(other, user=w.p, reason_ar="صُحح الآيبان وصُرف الأجر")
    assert other.status == "withdrawn"
    with pytest.raises(PayrollError):  # a withdrawn row is final
        services.confirm_mudad_justification(other, user=w.p)
    # A withdrawn row frees its reason: the next suggestion run writes it again.
    services.suggest_mudad_justifications(run, user=w.p)
    assert MudadJustification.objects.filter(run=run, reason="iban_exception").exclude(status="withdrawn").count() == 1


def test_at84_bank_rejection_notice_deadline_alerts_and_not_applicable_payslips(w):
    s1, n1, run = march(w)
    f = services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    # Review 7: a row without an ID never matches an employee whose ID is empty.
    services.attach_bank_processed_file(f, user=w.p, content=f"MOL-ID,32B-AMT,RET-CDE\r\n{s1.id_number},100,R05\r\n"
                                                             ",50,R09\r\n")
    j = rows(run)[(s1.employee_code, "bank_rejected")]
    assert (j.kind, j.evidence_ref, j.status) == ("non_payment", "RET-CDE R05", "suggested")
    assert not MudadJustification.objects.filter(run=run, reason="bank_rejected").exclude(pk=j.pk).exists()
    j = services.record_mudad_notice(j, user=w.p, notice_date=date(2027, 4, 12))
    assert j.due_date == date(2027, 4, 22)  # notice + 10 days (R53)
    sent = {}
    for day in (date(2027, 4, 19), date(2027, 4, 20), date(2027, 4, 22), date(2027, 4, 23)):
        services.send_alerts(today=day, entity=w.alm)
        sent[day] = set(PayrollAlert.objects.filter(kind="mudad_justification", key__startswith=f"mudad_j:{j.pk}:")
                        .values_list("key", flat=True))
    assert sent[date(2027, 4, 19)] == {f"mudad_j:{j.pk}:before"} and sent[date(2027, 4, 20)] == sent[date(2027, 4, 19)]
    assert sent[date(2027, 4, 23)] == {f"mudad_j:{j.pk}:{k}" for k in ("before", "due", "late")}
    alert = PayrollAlert.objects.filter(kind="mudad_justification", recipient=w.h, key__startswith="mudad_j:").first()
    assert alert is not None and alert.text_ar and s1.employee_code in alert.text_en
    assert PayrollAlert.objects.filter(kind="mudad_justification", recipient=w.p).exists()
    # C2: open justifications while the run's upload to Mudad is not recorded: one alert for the run.
    assert PayrollAlert.objects.filter(kind="mudad_justification", key=f"mudad_j_run:{run.pk}").exists()
    # Remuneration payslips never get a justification (R79).
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    r1 = hired(w, basis="remuneration")
    out = approved(w, payroll_group="out_of_country")
    with pytest.raises(PayrollError) as exc:
        services.record_mudad_justification(slip(out, r1), user=w.p, kind="non_payment", reason="other",
                                            description_ar="سبب")
    assert exc.value.code == "mudad_not_applicable" and " / " in exc.value.messages[0]
    with pytest.raises(PayrollError) as exc:
        services.export_mudad_justifications(out, user=w.p)
    assert exc.value.code == "mudad_not_applicable"


def test_c2_due_date_defaults_to_ten_days_after_the_upload(w):
    s1, n1, run = march(w)
    f = services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    j = rows(run)[(s1.employee_code, "unpaid_leave")]
    assert j.due_date is None
    services.attach_bank_processed_file(f, user=w.p, content="bank processed content")
    services.record_mudad_submission(f, user=w.p, submitted_on=date(2027, 4, 5), reference="M-1", status="submitted")
    assert MudadJustification.objects.get(pk=j.pk).due_date == date(2027, 4, 15)  # upload + 10 days
    j = services.record_mudad_notice(j, user=w.p, notice_date=date(2027, 4, 9))  # Mudad's own notice overrides
    assert j.due_date == date(2027, 4, 19)
    later = services.record_mudad_justification(slip(run, n1), user=w.p, kind="non_payment", reason="other",
                                                description_ar="سبب آخر")
    assert later.due_date == date(2027, 4, 15)


def test_r79_refused_payslip_joiner_and_late_pay_date_suggestions(w):
    s1 = saudi(w)
    s7 = saudi(w, cohort="", id_number="1000000771")  # refused: no GOSI cohort (R56)
    run = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-03"), user=w.p)
    assert slip(run, s7).refused
    run = services.approve_run(services.submit_run(run, user=w.p, exclude_refused=True), user=w.h)
    joiner = saudi(w, id_number="1000000772", hire_date=date(2027, 4, 11), gosi_number="")
    services.suggest_mudad_justifications(run, user=w.p)
    j = rows(run)[(s7.employee_code, "payroll_correction")]
    assert (j.kind, j.paid_amount, j.expected_amount) == ("non_payment", D("0"), D("13500.00"))
    # C7: late is more than 30 days after the previous pay date (27 March + 30 = 26 April).
    april = services.create_run(w.alm, user=w.p, period="2027-04", pay_date=date(2027, 4, 27))
    april = services.approve_run(services.submit_run(services.calculate_run(april, user=w.p), user=w.p,
                                                     exclude_refused=True), user=w.h)
    services.suggest_mudad_justifications(april, user=w.p)
    found = rows(april)
    jj = found[(joiner.employee_code, "joiner_leaver_proration")]
    assert jj.kind == "partial_payment"
    late = found[(s1.employee_code, "other")]
    assert late.kind == "late_payment" and "Art. 90" in late.description_en and late.description_ar
    # C6: approving a joiner justification warns while the GOSI joining registration is not recorded.
    jj = services.approve_mudad_justification(services.confirm_mudad_justification(jj, user=w.p), user=w.h)
    assert any("GOSI joining registration" in x and "التأمينات" in x for x in jj.warnings)
    with pytest.raises(PermissionDenied):
        services.suggest_mudad_justifications(run, user=w.v)


def test_c7_a_pay_date_30_days_after_the_previous_is_not_late(w):
    s1 = saudi(w)
    approved(w)  # paid 27 March
    april = services.create_run(w.alm, user=w.p, period="2027-04", pay_date=date(2027, 4, 26))  # 27 March + 30
    april = services.approve_run(services.submit_run(services.calculate_run(april, user=w.p), user=w.p), user=w.h)
    services.suggest_mudad_justifications(april, user=w.p)
    assert not MudadJustification.objects.filter(run=april, kind="late_payment").exists()
    assert s1


def test_c11_proactive_justification_months_ahead_one_per_worker(w):
    s1 = saudi(w)
    w.pclock.set(datetime(2027, 4, 10, 12, 0))
    with pytest.raises(PayrollError) as exc:  # starts this month
        services.record_proactive_justification(s1, user=w.p, reason="unpaid_leave", from_period="2027-04",
                                                to_period="2027-06", description_ar="إجازة بدون أجر طويلة")
    assert exc.value.code == "proactive_period"
    for start, end in (("2027-05", "2027-05"), ("2027-05", "2028-05")):  # one month; ends 13 months ahead
        with pytest.raises(PayrollError) as exc:
            services.record_proactive_justification(s1, user=w.p, reason="unpaid_leave", from_period=start,
                                                    to_period=end, description_ar="إجازة بدون أجر طويلة")
        assert exc.value.code == "proactive_period"
    with pytest.raises(PayrollError) as exc:  # a reason that cannot run ahead
        services.record_proactive_justification(s1, user=w.p, reason="bank_rejected", from_period="2027-05",
                                                to_period="2027-07", description_ar="رفض البنك")
    assert exc.value.code == "justification_invalid"
    j = services.record_proactive_justification(s1, user=w.p, reason="unpaid_leave", from_period="2027-05",
                                                to_period="2027-07", description_ar="إجازة بدون أجر طويلة")
    assert (j.kind, j.payslip_id, j.employee_id, j.expected_amount) == ("proactive", None, s1.pk, D("13500.00"))
    with pytest.raises(PayrollError) as exc:  # one live proactive justification per worker
        services.record_proactive_justification(s1, user=w.p, reason="contract_suspended", from_period="2027-06",
                                                to_period="2027-08", description_ar="عقد موقوف")
    assert exc.value.code == "justification_exists"
    j = services.approve_mudad_justification(j, user=w.h)
    assert j.status == "approved"
    r1 = hired(w, basis="remuneration")
    with pytest.raises(PayrollError) as exc:
        services.record_proactive_justification(r1, user=w.p, reason="unpaid_leave", from_period="2027-05",
                                                to_period="2027-07", description_ar="سبب")
    assert exc.value.code == "mudad_not_applicable"
    # The worksheet of a run in the covered months lists it.
    w.pclock.set(datetime(2027, 6, 20, 12, 0))
    for period in ("2027-03", "2027-04", "2027-05"):
        run = approved(w, period)
    export = services.export_mudad_justifications(run, user=w.p)
    assert "2027-05..2027-07" in export.content and export.row_count >= 1


def test_c3_the_salary_file_never_carries_justifications(w):
    """Compliance C3 (HRSD WPS guide): justifications are chosen on the Mudad portal, not uploaded in the salary
    file: a bank format row declaring justification columns changes nothing."""
    saudi(w, id_number="1000000661")
    s1, n1, run = march(w)
    services.configure_settings(w.alm, user=w.c, salary_bank="alrajhi")
    before = services.generate_wps_file(run, user=w.p, format_code="mudad_placeholder")
    j = rows(run)[(s1.employee_code, "unpaid_leave")]
    services.approve_mudad_justification(services.confirm_mudad_justification(j, user=w.p), user=w.h)
    hr.add_statutory_parameter(user=w.h, jurisdiction="SA", code="wps.format.alrajhi.justification_fields",
                               value={"columns": [{"name": "MUDAD-JUST", "carries": "code_and_text"}]},
                               valid_from=date(2027, 1, 1), source_url="https://example.invalid/alrajhi-format",
                               source_reference="test format: one justification column")
    f = services.generate_wps_file(run, user=w.p, format_code="mudad_placeholder")
    assert f.content == before.content and "MUDAD-JUST" not in f.content
