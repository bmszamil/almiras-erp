"""Payroll review fixes of 2026-10-21, fifth review (verification of the bai' ajel closures and the payroll WHT cap;
payroll §10.9 items 30 to 38), with payroll's fake invoicing port. Ported from the reviewer's rv5 probes
(test_zz_rv5.py, test_zz_rv5_wht.py); the real-port ledger balances are in invoicing/test_deferred_sale_review5.py
and the PostgreSQL cases (guards, concurrent approvals) in payroll/test_pg_review_2026_10_21b.py. Anonymised
fixtures; the payroll clock is fixed (2027-03-20); nothing external is called.

Item 1: a recovery is received in a bank or cash account only (never 1240, 1241, 1250 or a restricted account).
Item 2: a payment whose capped WHT takes all of it posts Dr 2330 / Cr 2220 only (no zero bank line). Item 4: a
recovery is not dated before the sale or the final settlement's approval. Item 5: the VAT in a written-off balance is
stored and listed for the tax adviser. Item 6: a rebate note found rejected at approval is recorded and the approval
asks to retry. Item 7: the write-off right's labels. Item 9: the cap in a 3-decimal currency."""
from datetime import date
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied
from django.db import models

from core import access
from core.access import catalogue
from ledger.models import Account, JournalEntry, JournalLine
from payroll import approvals, invoicing_port, services
from payroll.conftest import D, rate, slip
from payroll.models import (DeferredSaleClosure, DeferredSaleContract, DeferredSaleInstallment, DeferredSaleRebate,
                            FinalSettlement, PayrollError)
from payroll.test_review_fixes_2026_10_20 import bilingual, category, remuneration, remuneration_settlement
from payroll.test_review_fixes_2026_10_20c import _category, _pay, _payroll_rows, _usd_run
from payroll.test_review_fixes_2026_10_21 import _after_exit_balance, _lines, _recovery, _write_off


def _prepare_recovery(w, c, account, amount="69.00", on=date(2027, 3, 20)):
    return services.prepare_deferred_sale_closure(c, user=w.p, kind="recovery", amount=amount, reason_ar="سداد",
                                                  bank_account=account, received_on=on)


def _sub_account(w, code, parent, **fields):
    return Account.objects.create(entity=w.alm, code=code, name_en=f"Test {code}", name_ar=f"اختبار {code}",
                                  type="asset", parent=Account.objects.get(entity=w.alm, code=parent), **fields)


# --- item 1: a recovery debits a bank or cash account only (reviewer's p1, p2) ------------------------------------

@pytest.mark.parametrize("code", ["1240", "1241", "1250", "1140", "1230", "1310"])
def test_item1_a_recovery_into_the_receivable_or_any_non_bank_account_is_refused(w, code):
    n1, c = _after_exit_balance(w)
    acc = Account.objects.filter(entity=w.alm, code=code).first() or _sub_account(w, code, "1000")
    with pytest.raises(PayrollError) as exc:
        _prepare_recovery(w, c, acc)
    assert exc.value.code == "bank_account_invalid" and bilingual(exc.value)
    assert not DeferredSaleClosure.objects.filter(contract=c).exists()


def test_item1_a_recovery_into_1100_a_bank_sub_account_or_petty_cash_is_accepted(w):
    n1, c = _after_exit_balance(w)
    bank = _sub_account(w, "1101", "1100", currency_id="SAR")
    cash = Account.objects.filter(entity=w.alm, code="1110").first() or _sub_account(w, "1110", "1000")
    for acc in (services._account(w.alm, "1100"), bank, cash):
        assert services._bank_account(w.alm, acc) == acc
    x = services.approve_deferred_sale_closure(
        services.submit_deferred_sale_closure(_prepare_recovery(w, c, bank, amount="30.00"), user=w.p), user=w.mp)
    assert _lines(x) == [("1101", D("30.00"), D("0")), ("1240", D("0"), D("30.00"))]


def test_item1_an_inactive_or_header_bank_account_is_refused(w):
    n1, c = _after_exit_balance(w)
    closed = _sub_account(w, "1102", "1100", is_active=False)
    header = _sub_account(w, "1103", "1100", is_postable=False)
    for acc in (closed, header):
        with pytest.raises(PayrollError) as exc:
            _prepare_recovery(w, c, acc)
        assert exc.value.code == "bank_account_invalid"


def test_item1_the_account_is_checked_again_at_approval_and_in_the_closure_lines(w):
    n1, c = _after_exit_balance(w)
    x = services.submit_deferred_sale_closure(_prepare_recovery(w, c, services._account(w.alm, "1100")), user=w.p)
    # test data: the stored account changed behind the service (PostgreSQL's guard allows a submitted row to change)
    models.QuerySet.update(DeferredSaleClosure.objects.filter(pk=x.pk), bank_account=services._account(w.alm, "1240"))
    with pytest.raises(PayrollError) as exc:
        services.approve_deferred_sale_closure(DeferredSaleClosure.objects.get(pk=x.pk), user=w.mp)
    assert exc.value.code == "bank_account_invalid"
    assert DeferredSaleClosure.objects.get(pk=x.pk).status == "submitted"
    # the registered build_lines never debits the receivable, whoever calls it
    row = DeferredSaleClosure.objects.select_related("contract", "bank_account").get(pk=x.pk)
    with services._marker(services._posting, row), pytest.raises(PayrollError) as exc:
        services._closure_lines(row)
    assert exc.value.code == "bank_account_invalid"


def test_item1_a_payroll_payment_never_leaves_from_1240_or_a_restricted_account(w):
    from payroll.conftest import approved, saudi

    s1 = saudi(w)
    run = approved(w)
    for code in ("1240", "1140"):
        with pytest.raises(PayrollError) as exc:
            services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 3, 31),
                                    amount=str(slip(run, s1).net), bank_account=code)
        assert exc.value.code == "bank_account_invalid"


# --- reviewer's p3, p5, p6: kept as tests --------------------------------------------------------------------------

def test_p3_a_closure_entry_is_never_reversed_through_the_ledger_directly(w):
    from ledger import services as ledger

    n1, c = _after_exit_balance(w)
    x = services.approve_deferred_sale_closure(_write_off(w, c), user=w.mp)
    for u in (w.mp, w.c, w.a):
        with pytest.raises(PermissionDenied):
            grant = access.require(u, w.alm, "ledger.reverse_entry", document=x.journal_entry)
            ledger.reverse_entry(x.journal_entry, grant, date=date(2027, 3, 25), reason="probe")
    assert JournalEntry.objects.get(pk=x.journal_entry_id).status == "posted"
    assert not JournalEntry.objects.filter(reversal_of_id=x.journal_entry_id).exists()


def test_p5_the_approver_cannot_submit_a_closure(w):
    n1, c = _after_exit_balance(w)
    x = services.prepare_deferred_sale_closure(c, user=w.p, kind="write_off", reason_ar="سبب")
    with pytest.raises(PermissionDenied):
        services.submit_deferred_sale_closure(x, user=w.mp)


def test_p6_a_rejected_closure_never_moves_back(w):
    n1, c = _after_exit_balance(w)
    x = _write_off(w, c)
    services.reject_deferred_sale_closure(x, user=w.mp, reason_ar="لا")
    x = DeferredSaleClosure.objects.get(pk=x.pk)
    for status in ("submitted", "approved", "draft"):
        x.status = status
        with pytest.raises(PayrollError) as exc:
            services._save(x)
        assert exc.value.code == "closure_status"


# --- item 2: the capped WHT takes the whole payment (no zero bank line) -------------------------------------------

def _entry_lines(p):
    return sorted((l.account.code, l.debit, l.credit) for l in JournalLine.objects.filter(entry=p.journal_entry)
                  .select_related("account"))


def test_item2_a_0_01_usd_payslip_paid_alone_is_fully_withheld_with_no_bank_line(w):
    """The reviewer's w3: r1 keeps 0.01 USD of a 5,000.00 gross; paying only them, the WHT (250.00 due) is capped at
    their 0.01, the whole payment: before, the 0.00 bank line made the ledger refuse the posting."""
    r1, r2 = remuneration(w), remuneration(w)
    inp = services.create_input(r1, user=w.p, kind="one_off_deduction", period="2027-03", amount="4999.99",
                                deduction_basis="written_consent", consent_ref="S", reason_ar="استقطاع")
    services.approve_input(inp, user=w.p2)
    run = _usd_run(w)
    s1 = slip(run, r1)
    p = _pay(w, run, "0.01", payslips=[s1])
    assert p.status == "approved" and p.wht_amount == p.amount == D("0.01")
    lines = _entry_lines(p)
    assert [code for code, _, _ in lines] == ["2220", "2330"]  # Dr 2330 / Cr 2220 only
    assert not any(code == p.bank_account.code for code, _, _ in lines)
    assert sum(d for _, d, _ in lines) == sum(cr for _, _, cr in lines) == D("0.04")
    assert any("fully withheld" in t and "مستقطعة بالكامل" in t for t in p.warnings)
    assert [x["employee_code"] for x in p.wht_uncovered] == [r1.employee_code]


def _big_gross_settlement(w):
    """Test data as ``remuneration_settlement`` with gross 100,000.00 and net 100.00, written once (PostgreSQL locks
    an approved settlement, so it is not changed afterwards as the reviewer's probe did)."""
    from hr import services as hr
    from hr.models import FinalSettlementInput

    r1 = remuneration(w)
    t = hr.record_termination(r1, user=w.h, kind="end_of_remuneration", last_working_day=date(2027, 3, 15),
                              reason_ar="انتهاء المهمة")
    t = hr.approve_termination(hr.submit_termination(t, user=w.h), user=w.c)
    si = FinalSettlementInput.objects.filter(termination=t).order_by("-pk").first()
    fs = FinalSettlement(entity=w.alm, employee=r1, termination=t, settlement_input=si, kind=t.kind,
                         last_working_day=t.last_working_day, period="2027-03", status="approved",
                         gross=D("100000.00"), total_deductions=D("99900.00"), net=D("100.00"),
                         number="ALM-FS-TEST-2", created_by=w.p, approved_by=w.c)
    fs._service_write = True
    fs.save()
    return r1, fs


def test_item2_a_settlement_paid_in_two_fully_withheld_parts_ties_out_and_is_marked_paid(w):
    """The reviewer's w2: gross 100,000.00, net 100.00; payments of 40.00 and 60.00, each fully withheld (WHT due
    2,000.00 and 3,000.00, capped at the payment); the settlement then becomes paid and the WHT return ties out."""
    from invoicing import services as inv

    r1, fs = _big_gross_settlement(w)
    cat = category()
    paid = []
    for on, amount in ((date(2027, 3, 20), "40.00"), (date(2027, 3, 21), "60.00")):
        p = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs, payment_date=on,
                                    amount=amount, wht_category=cat)
        p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
        assert p.wht_amount == p.amount == D(amount)
        assert _entry_lines(p) == [("2220", D("0"), D(amount)), ("2330", D(amount), D("0"))]
        paid.append(p)
        assert FinalSettlement.objects.get(pk=fs.pk).status == ("approved" if amount == "40.00" else "paid")
    data = inv.wht_return_data(w.alm, date(2027, 3, 1), user=w.a)
    assert data["difference"] == 0 and data["total_wht_sar"] == D("100.00")


def test_item2_a_partly_withheld_payment_still_posts_its_bank_line(w):
    r1, fs = remuneration_settlement(w)
    p = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=date(2027, 3, 20), amount="5000.00", wht_category=category())
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    assert p.wht_amount == D("300.00")  # 5% of the 6,000.00 gross
    assert _entry_lines(p) == [(p.bank_account.code, D("0"), D("4700.00")), ("2220", D("0"), D("300.00")),
                               ("2330", D("5000.00"), D("0"))]
    assert not any("fully withheld" in t for t in p.warnings)


def test_w1_three_payees_two_partial_payments_tie_out(w):
    """The reviewer's w1: three payees (nets 0.01, 0.00 and 5,000.00), two partial payments."""
    r1, r2, r3 = remuneration(w), remuneration(w), remuneration(w)
    for emp, amt in ((r1, "4999.99"), (r2, "5000.00")):
        inp = services.create_input(emp, user=w.p, kind="one_off_deduction", period="2027-03", amount=amt,
                                    deduction_basis="written_consent", consent_ref="S", reason_ar="استقطاع")
        services.approve_input(inp, user=w.p2)
    run = _usd_run(w)
    s1, s2, s3 = slip(run, r1), slip(run, r2), slip(run, r3)
    _pay(w, run, "2500.00", payslips=[s1, s2, s3])
    _pay(w, run, str(s1.net_txn + s3.net_txn - D("2500.00")), payslips=[s1, s3])
    data, rows = _payroll_rows(w)
    assert data["difference"] == 0


# --- item 9: the cap in a 3-decimal currency ----------------------------------------------------------------------

def test_item9_the_wht_cap_keeps_the_currency_s_three_decimals(w):
    r1 = remuneration(w, contract_kwargs={"currency": "KWD"})
    r2 = remuneration(w, contract_kwargs={"currency": "KWD"})
    inp = services.create_input(r1, user=w.p, kind="one_off_deduction", period="2027-03", amount="4999.995",
                                deduction_basis="written_consent", consent_ref="S", reason_ar="استقطاع")
    services.approve_input(inp, user=w.p2)
    for on in (date(2027, 3, 31), date(2027, 4, 8)):
        rate("KWD", "SAR", on, "12.2000")
    from payroll.conftest import approved
    run = approved(w, payroll_group="out_of_country")
    s1, s2 = slip(run, r1), slip(run, r2)
    assert (s1.currency_id, s1.net_txn, s2.net_txn) == ("KWD", D("0.005"), D("5000.000"))
    p = _pay(w, run, "0.005", payslips=[s1], currency="KWD")
    assert p.wht_amount == p.amount == D("0.005")  # capped at their 0.005, not rounded to 2 decimals
    assert [code for code, _, _ in _entry_lines(p)] == ["2220", "2330"]  # fully withheld
    assert p.wht_amount_sar == D("0.06")  # 0.005 x 12.20 = 0.061
    p2 = _pay(w, run, "5000.000", payslips=[s2], currency="KWD")
    assert p2.wht_amount == D("250.000") and p2.wht_amount_sar == D("3050.00")
    bank = [l for l in JournalLine.objects.filter(entry=p2.journal_entry) if l.account_id == p2.bank_account_id]
    assert [(l.currency_id, l.txn_amount) for l in bank] == [("KWD", D("4750.000"))]
    data, rows = _payroll_rows(w)
    assert data["difference"] == 0


# --- item 4: the recovery date (reviewer's p4) --------------------------------------------------------------------

def test_item4_a_recovery_is_not_dated_before_the_final_settlement_s_approval(w):
    n1, c = _after_exit_balance(w)
    fs = FinalSettlement.objects.filter(employee_id=c.employee_id).order_by("-pk").first()
    assert fs.status == "approved" and fs.last_working_day == date(2027, 3, 15)
    for on in (date(2027, 1, 2), date(2027, 3, 15), date(2027, 3, 19)):  # the reviewer's 2027-01-02 posted before
        with pytest.raises(PayrollError) as exc:
            _recovery(w, c, "10.00", on=on)
        assert exc.value.code == "recovery_date" and bilingual(exc.value)
    x = services.approve_deferred_sale_closure(_recovery(w, c, "10.00", on=date(2027, 3, 20)), user=w.mp)
    assert x.journal_entry.date == date(2027, 3, 20)


def test_item4_a_recovery_is_not_dated_before_the_sale_and_is_checked_again_at_approval(w):
    n1, c = _after_exit_balance(w)
    x = _recovery(w, c, "10.00")
    models.QuerySet.update(DeferredSaleContract.objects.filter(pk=c.pk), sale_date=date(2027, 3, 21))  # test data
    with pytest.raises(PayrollError) as exc:
        services.approve_deferred_sale_closure(x, user=w.mp)
    assert exc.value.code == "recovery_date"
    with pytest.raises(PayrollError) as exc:
        _recovery(w, DeferredSaleContract.objects.get(pk=c.pk), "10.00")
    assert exc.value.code in ("recovery_date", "closure_exists")


# --- item 5: the VAT in the balance written off --------------------------------------------------------------------

def test_item5_a_write_off_stores_its_vat_share_warns_the_approver_and_lists_it_for_the_tax_adviser(w):
    n1, c = _after_exit_balance(w)
    x = _write_off(w, c)
    # 69.00 of a receivable of 1,518.00 that carries 198.00 of VAT: 69.00 x 198 / 1,518 = 9.00
    assert (x.amount, x.vat_share) == (D("69.00"), D("9.00"))
    assert any("2200" in t and "OQ-78" in t and "الديون المعدومة" in t for t in x.warnings)
    assert services.bai_ajel_write_off_vat_list(w.alm, user=w.mp) == []  # not approved yet
    x = services.approve_deferred_sale_closure(x, user=w.mp)
    assert x.vat_share == D("9.00") and x.warnings
    assert ("2200", D("0"), D("0")) not in _lines(x)  # nothing posted to 2200: the VAT stays there
    assert {code for code, _, _ in _lines(x)} == {"1240", "1241", "5100"}
    rows = services.bai_ajel_write_off_vat_list(w.alm, user=w.mp)
    assert [(r["closure"], r["amount"], r["vat_share"]) for r in rows] == [(x.number, D("69.00"), D("9.00"))]
    assert rows[0]["note_en"] and rows[0]["note_ar"]
    assert services.bai_ajel_write_off_vat_list(w.alm, user=w.mp, end=date(2027, 3, 19)) == []
    log = __import__("core.access.models", fromlist=["AccessLog"]).AccessLog.objects.filter(
        details__action="bai_ajel_write_off").get()
    assert log.details["vat_share"] == "9.00"


def test_item5_a_recovery_has_no_vat_share_and_the_list_needs_payroll_view(w):
    from core.access.testing import make_user

    n1, c = _after_exit_balance(w)
    x = services.approve_deferred_sale_closure(_recovery(w, c, "30.00"), user=w.mp)
    assert x.vat_share == D("0") and x.warnings == []
    with pytest.raises(PermissionDenied):
        services.bai_ajel_write_off_vat_list(w.alm, user=make_user())


# --- item 6: a rebate note found rejected at approval -------------------------------------------------------------

def test_item6_a_note_found_rejected_at_approval_is_recorded_and_the_approval_asks_to_retry(w):
    n1, c = _after_exit_balance(w)
    x = _write_off(w, c)
    # test data: a second rebate whose note is still pending (no service path reaches this after exit: the closure
    # is refused while a note is pending; the approval reads the notes again all the same)
    r2 = DeferredSaleRebate(entity=w.alm, contract=c, amount=D("10.00"), vat_amount=D("1.50"), reason_ar="إبراء",
                            request_ref="REQ-T", credit_note_ref="TEST-CN-T", credit_note_status="issued",
                            status="approved", created_by=w.p, approved_by=w.mp,
                            installment_cuts=[{"period": "2028-02", "amount": "11.50"}])
    services._save(r2)
    w.invoicing.rebates.append((c.pk, services._rebate_ref(r2), D("10.00"), w.mp.pk))
    w.invoicing.rebate_status = "rejected"
    with pytest.raises(PayrollError) as exc:
        services.approve_deferred_sale_closure(x, user=w.mp)
    assert exc.value.code == "rebate_rejected_retry" and bilingual(exc.value)
    # the refresh was its own step: recorded although the approval was refused, so the user is not stuck
    r2 = DeferredSaleRebate.objects.get(pk=r2.pk)
    assert (r2.status, r2.credit_note_status) == ("rejected", "rejected")
    assert sorted(i.amount for i in c.installments.filter(status="outstanding_after_exit")) == [D("11.50"),
                                                                                                D("69.00")]
    assert w.invoicing.caught_up[-1][:2] == (c.pk, invoicing_port.catch_up_ref(r2))  # the first: the fixture's note
    assert DeferredSaleClosure.objects.get(pk=x.pk).status == "submitted"
    with pytest.raises(PayrollError) as exc:  # retrying without the steps the message names
        services.approve_deferred_sale_closure(x, user=w.mp)
    assert exc.value.code == "rebate_open"
    services.discard_deferred_sale_rebate(r2, user=w.p)
    with pytest.raises(PayrollError) as exc:
        services.approve_deferred_sale_closure(x, user=w.mp)
    assert exc.value.code == "closure_amount"
    services.reject_deferred_sale_closure(x, user=w.mp, reason_ar="تغير الرصيد")
    y = services.approve_deferred_sale_closure(_write_off(w, DeferredSaleContract.objects.get(pk=c.pk)), user=w.mp)
    assert y.amount == D("80.50") and DeferredSaleContract.objects.get(pk=c.pk).status == "settled"


def test_item3_the_catch_up_is_refused_outside_payroll_s_marker(w):
    n1, c, r = __import__("payroll.test_review_fixes_2026_10_21", fromlist=["x"])._rejected_after_exit(w)
    assert [ref for _, ref, _ in w.invoicing.caught_up] == [invoicing_port.catch_up_ref(r)]  # once, inside it
    ref = invoicing_port.catch_up_ref(r)
    assert invoicing_port.catch_up_problems(c, source_ref=ref, approver=w.mp)  # no marker
    with invoicing_port.catching_up(r):
        assert invoicing_port.catch_up_problems(c, source_ref=ref, approver=w.mp) == []
        assert invoicing_port.catch_up_problems(c, source_ref=ref, approver=w.p)  # not the rebate's approver
        assert invoicing_port.catch_up_problems(c, source_ref=ref + "x", approver=w.mp)
        other = SimpleNamespace(pk=c.pk + 999, entity_id=c.entity_id)
        assert invoicing_port.catch_up_problems(other, source_ref=ref, approver=w.mp)


# --- item 7: labels -----------------------------------------------------------------------------------------------

# Amended at the merge with the access §10 build (2026-10-21, access 10.5): bai' ajel closures have their own right
# and matrix activity, so item 7's labels sit on payroll.approve_deferred_sale_closure / payroll.deferred_sale_closure
# and the loan write-off names loans only.

def test_item7_the_write_off_right_and_matrix_row_name_bai_ajel_write_offs_and_recoveries():
    perm = catalogue.CATALOGUE["payroll.approve_deferred_sale_closure"]
    assert "bai' ajel" in perm.label_en and "recoveries" in perm.label_en
    assert "البيع الآجل" in perm.label_ar and "استرداد" in perm.label_ar
    en, ar = approvals.LABELS["payroll.deferred_sale_closure"]
    assert "bai' ajel write-offs and recoveries" in en and "البيع الآجل" in ar and "استرداد" in ar
    assert approvals.LABELS["payroll.loan_write_off"][0] == "loan write-offs"
    assert "bai' ajel" not in catalogue.CATALOGUE["payroll.approve_loan_write_off"].label_en


def test_item7_the_access_spec_row_names_the_closure_approval():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "docs" / "specs" / "access.md").read_text()
    row = next(line for line in text.splitlines() if line.startswith("| `payroll.deferred_sale_closure`"))
    assert "approve_deferred_sale_closure" in row and "write-off or recovery" in row
