"""WPS files, Mudad, payslips, reports and alerts (R2, R16, R24, R25, R31 to R33, R35, R40, R51, R53; AT-16,
AT-19, AT-21, AT-22, AT-34, AT-39, AT-60)."""
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied

from core.access.models import AccessLog
from hr import services as hr
from hr.models import StatutoryParameter
from ledger import services as ledger
from payroll import services
from payroll.conftest import D, approved, calculated, expat, hired, saudi, slip
from payroll.models import EosbProvisionEntry, PayrollAlert, PayrollError, WpsFile


def test_at19_at39_wps_file_excludes_invalid_iban_and_logs(w):
    s1 = saudi(w)
    n1 = expat(w)  # no IBAN
    s4 = saudi(w, components={"BASIC": "8000.00", "TRANSPORT": "500.00"}, contract_kwargs={"housing_in_kind": True})
    run = approved(w)
    with pytest.raises(PayrollError) as exc:
        services.generate_wps_file(run, user=w.p)
    assert exc.value.code == "wps_format_missing"
    with pytest.raises(PayrollError) as exc:  # C-C3: DEST-ID names the salary bank
        services.generate_wps_file(run, user=w.p, format_code="mudad_placeholder")
    assert exc.value.code == "salary_bank_missing"
    services.configure_settings(w.alm, user=w.c, salary_bank="alrajhi")
    f = services.generate_wps_file(run, user=w.p, format_code="mudad_placeholder")
    assert f.employee_count == 2 and [e["employee_code"] for e in f.exceptions] == [n1.employee_code]
    rows = f.content.splitlines()
    assert rows[0].startswith("DEST-ID,ESTB-ID") and rows[2].startswith("32B-AMT")
    by_id = {r.split(",")[10]: r for r in rows[3:]}  # MOL-ID
    assert ",10000.00,2500.00,1000.00,13500.00," in by_id[s1.id_number]  # MOL-BAS BASIC only, MOL-HALI cash HOUSING
    assert len(rows) == 5 and ",8000.00,0,500.00," in by_id[s4.id_number]  # S4: housing in kind -> MOL-HALI 0
    assert len(f.sha256) == 64 and run.journal_entry.lines.count() and WpsFile.objects.get(pk=f.pk).content == f.content
    assert AccessLog.objects.filter(details__action="wps_generated", actor=w.p).exists()
    services.download_wps_file(f, user=w.p)
    assert AccessLog.objects.filter(details__action="wps_downloaded").exists()
    with pytest.raises(PayrollError):
        services.record_mudad_submission(f, user=w.p, submitted_on=date(2027, 4, 5), reference="M-1",
                                         status="submitted")
    services.attach_bank_processed_file(f, user=w.p, content="bank processed content")
    m = services.record_mudad_submission(f, user=w.p, submitted_on=date(2027, 4, 5), reference="M-1",
                                         status="submitted")
    assert m.wps_file.bank_processed_sha256 and m.wps_file.sha256
    with pytest.raises(PayrollError):
        WpsFile.objects.get(pk=f.pk).delete()  # kept indefinitely (P19(m))


def test_at34_salary_bank_must_be_listed_and_bank_format_waits_for_the_bank(w):
    with pytest.raises(PayrollError):
        services.configure_settings(w.alm, user=w.c, salary_bank="hsbc")
    services.configure_settings(w.alm, user=w.c, salary_bank="alrajhi", wps_format="alrajhi")
    saudi(w)
    hired(w, basis="remuneration")
    from payroll.conftest import rate
    rate("USD", "SAR", date(2027, 3, 31), "3.75")
    run = approved(w)
    with pytest.raises(PayrollError) as exc:
        services.generate_wps_file(run, user=w.p)
    assert "wps.format.alrajhi" in exc.value.messages[0]
    out = approved(w, payroll_group="out_of_country")
    f = services.generate_wps_file(out, user=w.p, format_code="generic_csv")
    assert f.employee_count == 0 and f.exceptions == []  # remuneration staff are outside WPS (R51)


def test_at21_payslip_bilingual_masked_and_logged(w):
    s1 = saudi(w, full_name_en="Sample Person", full_name_ar="شخص نموذجي")
    run = approved(w)
    ps = slip(run, s1)
    with pytest.raises(PermissionDenied):
        services.render_payslip(ps, user=w.v)
    html = services.render_payslip(ps, user=w.p)
    assert "قسيمة راتب" in html and "Payslip" in html and "شخص نموذجي" in html and "الصافي" in html and "Net" in html
    assert s1.iban not in html and "****7519" in html and s1.id_number not in html
    assert AccessLog.objects.filter(details__action="payslip_viewed", actor=w.p).exists()
    StatutoryParameter.objects.filter(code="gosi.min_wage")  # stored values: nothing is recalculated on view
    assert services.render_payslip(ps, user=w.p) == html


def test_reports_register_gosi_and_bank_list(w):
    s1 = saudi(w)
    expat(w)
    run = approved(w)
    reg = services.payroll_register(run, user=w.h)
    assert reg["totals"]["net"] == sum(p.net for p in run.payslips.all()) and len(reg["rows"]) == 2
    g = services.gosi_report(w.alm, "2027-03", user=w.a)
    assert g["total"] == D("2837.50") and g["due_date"] == date(2027, 4, 15) and "Regs Art. 25" in g["late_fine_en"]
    banks = services.bank_transfer_list(run, user=w.p)
    assert {b["employee_code"] for b in banks} == {s1.employee_code, run.payslips.exclude(employee=s1).get().employee.employee_code}
    assert all("****" in b["iban"] or b["iban"] == "" for b in banks)
    with pytest.raises(PermissionDenied):
        services.payroll_register(run, user=w.a)  # individual pay


def test_at16_eosb_accrual_sub_ledger_and_tie_out_warning(w):
    s1 = saudi(w)
    run = approved(w)
    row = EosbProvisionEntry.objects.get(employee=s1)
    assert row.kind == "accrual" and row.amount == slip(run, s1).eosb_accrual > 0
    entry = ledger.create_entry(w.alm, date(2027, 3, 31), "Manual EOSB", [
        {"account": "5120", "debit": "100.00"}, {"account": "2500", "credit": "100.00"}], user=w.a)
    from core import access
    ledger.post_entry(entry, access.require(w.c, w.alm, "ledger.post_entry", document=entry))
    apr = calculated(w, "2027-04")
    assert any("differs from the EOSB sub-ledger by 100.00" in x for x in apr.warnings)
    with pytest.raises(PayrollError):
        row.delete()


def test_at22_secondment_cost_report_without_names(w):
    s1 = saudi(w, full_name_en="Hidden Name")
    a = hr.create_assignment(s1, user=w.h, kind="secondment", receiving_entity=w.sub, role_title_en="Analyst",
                             role_title_ar="محلل", start_date=date(2027, 3, 1), end_date=date(2027, 3, 10))
    hr.approve_assignment(hr.submit_assignment(a, user=w.h), user=w.c)
    run = approved(w)
    assert not run.journal_entry.lines.filter(account__code__in=("1210", "2110")).exists()
    rep = services.secondment_cost_report(w.alm, "2027-03", user=w.a)
    assert rep[0]["days"] == 10 and rep[0]["employee_code"] == s1.employee_code and "Hidden" not in str(rep)
    assert rep[0]["warnings"]


def test_at39_r53_mudad_alerts_and_at60_retention_nomination(w, pclock):
    saudi(w)
    run = approved(w)
    for day in (3, 6, 11):  # C-C5: pay date 2027-03-27 + 7, 10 and 15 days
        services.send_alerts(today=date(2027, 4, day), entity=w.alm)
        services.send_alerts(today=date(2027, 4, day), entity=w.alm)  # once each
    services.send_alerts(today=date(2027, 4, 7), entity=w.alm)  # day 7 of the month is not a Mudad day by itself
    assert PayrollAlert.objects.filter(kind="mudad_deadline", recipient=w.p).count() == 3
    f = services.generate_wps_file(run, user=w.p, format_code="generic_csv")  # generated 2027-03-20 (payroll clock)
    WpsFile.objects.filter(pk=f.pk)  # stored
    services.send_alerts(today=date(2027, 5, 1), entity=w.alm)
    assert WpsFile.objects.get(pk=f.pk).nominated_for_burn_at is None
    services.send_alerts(today=date(2037, 3, 19), entity=w.alm)
    assert WpsFile.objects.get(pk=f.pk).nominated_for_burn_at is None
    services.send_alerts(today=date(2037, 3, 22), entity=w.alm)
    assert WpsFile.objects.get(pk=f.pk).nominated_for_burn_at is not None  # nominated, never deleted


def test_r10_gosi_registration_alert_on_the_25th(w):
    saudi(w, gosi_number="")
    services.send_alerts(today=date(2027, 3, 25), entity=w.alm)
    assert PayrollAlert.objects.filter(kind="gosi_registration", recipient=w.h).exists()


def test_r22_gosi_payment_alert_on_the_10th(w):
    saudi(w)
    approved(w)
    services.send_alerts(today=date(2027, 4, 10), entity=w.alm)
    assert PayrollAlert.objects.filter(kind="gosi_payment", recipient=w.c).exists()
