"""Review fixes (2026-10-19): salary (WPS) files and IBAN control (review items 7, 8, 15; compliance C-C1, C-C2,
C-C3). Ported from the reviewer's probes test_zz_probes.py (P7, P8)."""
from datetime import date

import pytest

from hr import services as hr
from payroll import services
from payroll.conftest import D, approved, expat, saudi, slip, submitted
from payroll.models import MudadSubmission, PayrollError

NEW_IBAN = "GB82WEST12345698765432"


def iban(bank_code, account="0000608010167519"):
    """A checksum-valid Saudi IBAN for a bank identifier (test data)."""
    bban = f"{bank_code}{account}"
    digits = "".join(str(int(c, 36)) for c in bban + "SA00")
    return f"SA{98 - int(digits) % 97:02d}{bban}"


def test_review7_csv_cells_that_look_like_formulas_are_escaped():
    """Probe P7: a name starting with "=" reached the salary file as a live formula (hr now refuses such names at
    entry, hr/test_iban_change.py; the writer escapes whatever reaches it)."""
    rows = [{"employee_code": "E-1", "name": '=HYPERLINK("http://x.example")', "id": "+966", "iban": "SA00",
             "bank": "@bank", "basic": D("1.00"), "housing": D("0"), "other": D("0"), "deductions": D("-1.00"),
             "net": D("1.00")}]
    content, _ = services._wps_csv(None, "generic_csv", rows, None, None)
    line = content.splitlines()[1]
    assert line.startswith("E-1,\"'=HYPERLINK(") and ",'+966," in line and ",'@bank," in line and ",'-1.00," in line
    for value, safe in (("=1", "'=1"), ("\tx", "'\tx"), ("\rx", "'\rx"), ("ok", "ok"), (D("5.00"), "5.00")):
        assert services._cell(value) == safe


def test_review8_iban_change_after_calculation_forces_a_recalculation(w):
    s1 = saudi(w)
    run = submitted(w)
    req = hr.request_iban_change(s1, user=w.h, iban=NEW_IBAN, reason_ar="تغيير البنك")
    hr.approve_iban_change(req, user=w.h2)
    with pytest.raises(PayrollError) as exc:
        services.approve_run(run, user=w.h)
    assert exc.value.code == "recalculate"


def test_review8_wps_exceptions_for_an_iban_changed_or_a_termination_after_approval(w):
    s1 = saudi(w)
    n1 = expat(w, iban=iban("80", "0000608010167520"), bank_name="Al Rajhi")
    run = approved(w)
    req = hr.request_iban_change(s1, user=w.h, iban=NEW_IBAN, reason_ar="تغيير البنك")
    hr.approve_iban_change(req, user=w.h2)
    t = hr.record_termination(n1, user=w.h, kind="employer_termination", last_working_day=date(2027, 5, 31),
                              notice_given_on=date(2027, 3, 25), reason_ar="إنهاء")
    hr.approve_termination(hr.submit_termination(t, user=w.h), user=w.c)
    f = services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    reasons = {e["employee_code"]: e["reason"] for e in f.exceptions}
    assert f.employee_count == 0
    assert "IBAN changed" in reasons[s1.employee_code] and "Termination approved after" in reasons[n1.employee_code]
    assert NEW_IBAN not in f.content


def test_c_c1_c_c2_c_c3_mol_fields_net_of_reductions_deductions_and_bank_codes(w):
    n1 = expat(w, iban=iban("80"), bank_name="Al Rajhi")
    other_bank = expat(w, iban=iban("99", "0000608010167521"), bank_name="Unknown")
    inp = services.create_input(n1, user=w.p, kind="unpaid_days", period="2027-03", quantity="3", reason_ar="غياب")
    services.approve_input(inp, user=w.p2)
    run = approved(w)
    services.configure_settings(w.alm, user=w.c, salary_bank="alrajhi", wps_deductions_field="TOTAL-DED")
    f = services.generate_wps_file(run, user=w.p, format_code="mudad_placeholder")
    rows = f.content.splitlines()
    header = dict(zip(rows[0].split(","), rows[1].split(",")))
    assert header["DEST-ID"] == "RJHISARI"
    fields = rows[2].split(",")
    assert fields[-1] == "TOTAL-DED"
    row = dict(zip(fields, rows[3].split(",")))
    ps = slip(run, n1)
    # 3 unpaid days of 8,000.00: BASIC 6,000.00 - 600.00, HOUSING 1,500.00 - 150.00, TRANSPORT 500.00 - 50.00.
    assert (row["MOL-BAS"], row["MOL-HALI"], row["MOL-OEA"]) == ("5400.00", "1350.00", "450.00")
    assert D(row["MOL-ALL"]) == ps.gross and row["TOTAL-DED"] == str(ps.total_deductions)
    assert row["BANK-57"] == "RJHISARI" and row["32B-AMT"] == str(ps.net)
    assert [e["employee_code"] for e in f.exceptions] == [other_bank.employee_code]
    assert "wps.bank_codes" in f.exceptions[0]["reason"]


def test_review15_bank_processed_file_attached_once_within_the_size_limit_and_hashed_on_mudad(w, settings):
    """Probe P8: the bank-processed file was overwritten after the Mudad submission."""
    saudi(w)
    run = approved(w)
    f = services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    settings.PAYROLL_BANK_FILE_MAX_BYTES = 10
    with pytest.raises(PayrollError) as exc:
        services.attach_bank_processed_file(f, user=w.p, content="x" * 11)
    assert exc.value.code == "file_too_large"
    settings.PAYROLL_BANK_FILE_MAX_BYTES = 5_000_000
    f = services.attach_bank_processed_file(f, user=w.p, content="processed-1")
    m = services.record_mudad_submission(f, user=w.p, submitted_on=date(2027, 4, 5), reference="M-1",
                                         status="submitted")
    assert MudadSubmission.objects.get(pk=m.pk).bank_processed_sha256 == f.bank_processed_sha256
    with pytest.raises(PayrollError) as exc:
        services.attach_bank_processed_file(f, user=w.p, content="something else")
    assert exc.value.code == "bank_file_attached"
