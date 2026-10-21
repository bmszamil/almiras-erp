"""Payroll review fixes of 2026-10-21, fourth review of the ibra' path and payroll WHT (payroll §10.9 items 23 to 29),
with payroll's fake invoicing port. Ported from the reviewer's probes (rv4: test_zz_rv4.py, test_zz_rv4_wht.py);
the PostgreSQL cases (lock order, triggers, migration reversal) are in payroll/test_pg_review_2026_10_21.py and the
real-port ledger balances in invoicing/test_deferred_sale_review4.py. Anonymised fixtures; the payroll clock is
fixed; nothing external is called.

Item 1 / 6: the WHT withheld for a payee never exceeds their own part of the payment (any net, 0 or 0.01); the rest
is a warning and a review row of the WHT return. Item 2: one entity failing in the alerts job never stops the
others. Item 4: only payroll's services and its invoicing port reach the rebate credit note. Item 5: a bai' ajel
balance left after exit is written off or recovered (four eyes, ledger registry only)."""
import ast
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied
from django.core.management import CommandError, call_command

from core import access
from core.access.models import AccessLog
from ledger.models import JournalLine
from payroll import invoicing_port, services
from payroll.conftest import D, slip
from payroll.models import (DeferredSaleClosure, DeferredSaleContract, DeferredSaleInstallment, DeferredSaleRebate,
                            FinalSettlement, PayrollError)
from payroll.test_final_settlement import go_live, leaver, settle, terminate
from payroll.test_review_fixes_2026_10_20 import bilingual, remuneration
from payroll.test_review_fixes_2026_10_20b import planned, rebate, sold
from payroll.test_review_fixes_2026_10_20c import _pay, _payroll_rows, _usd_run, settled_leaver_with_pending_rebate


# --- item 1 / 6: WHT capped at the payee's own part of the payment ---------------------------------------------------

def _near_zero_net(w, deduction="4999.99"):
    """The reviewer's w1: r1 (gross 5,000.00 USD) keeps a net of 0.01 after a consented deduction, r2 is paid in
    full; one payment pays both nets (5,000.01)."""
    r1, r2 = remuneration(w), remuneration(w)
    inp = services.create_input(r1, user=w.p, kind="one_off_deduction", period="2027-03", amount=deduction,
                                deduction_basis="written_consent", consent_ref="SIGNED-R1", reason_ar="استقطاع")
    services.approve_input(inp, user=w.p2)
    run = _usd_run(w)
    s1, s2 = slip(run, r1), slip(run, r2)
    return r1, r2, run, s1, s2


def test_item1_a_payee_with_net_0_01_has_wht_capped_at_their_own_part_never_from_the_others(w):
    r1, r2, run, s1, s2 = _near_zero_net(w)
    assert (s1.net_txn, s1.gross_txn, s2.net_txn) == (D("0.010"), D("5000.000"), D("5000.000"))
    p = _pay(w, run, str(s1.net_txn + s2.net_txn))
    parts = {x["payslip_id"]: D(x["amount"]) for x in p.payslip_parts}
    assert parts == {s1.pk: D("0.01"), s2.pk: D("5000.00")}
    # before: 250.00 + 250.00 withheld, 249.99 of r1's WHT out of r2's money; now r1's WHT is at most their 0.01
    assert p.wht_amount == D("250.01")
    live = {x["employee_code"]: x for x in p.wht_parts}
    assert set(live) == {r1.employee_code, r2.employee_code}
    assert D(live[r1.employee_code]["wht_sar"]) <= D("0.04")  # 0.01 USD at 3.75, split to add up exactly
    assert D(live[r1.employee_code]["base_sar"]) == D("0.75")  # the share of the base the 0.01 covers (0.20 USD)
    assert D(live[r2.employee_code]["base_sar"]) == D("18750.00")
    assert sum(D(x["wht_sar"]) for x in p.wht_parts) == p.wht_amount_sar == D("937.54")
    assert p.wht_uncovered == [{"employee_id": r1.pk, "employee_code": r1.employee_code,
                                "country": r1.country_of_residence or "", "currency": "USD", "base_txn": "4999.80",
                                "wht_txn": "249.99", "base_sar": "18749.25", "wht_not_withheld_sar": "937.46"}]
    text = " ".join(p.warnings)
    assert r1.employee_code in text and "249.99" in text and "OQ-29" in text and "لا يُستقطع" in text
    # the bank line pays r2 their 4,750.00 and r1 nothing: no payee's net goes to another payee's WHT
    bank = JournalLine.objects.get(entry=p.journal_entry, account=p.bank_account)
    assert bank.txn_amount == D("4750.00")


def test_item6_the_wht_not_withheld_is_a_review_row_of_the_return_and_the_return_still_ties(w):
    r1, r2, run, s1, s2 = _near_zero_net(w)
    p = _pay(w, run, str(s1.net_txn + s2.net_txn))
    data, rows = _payroll_rows(w)
    review = [r for r in rows if r.get("not_withheld")]
    assert len(review) == 1
    row = review[0]
    assert (row["vendor"], row["base_sar"], row["wht_sar"], row["wht_not_withheld_sar"]) == (
        r1.employee_code, D("18749.25"), D("0.00"), D("937.46"))
    assert row["review_required"] and row["review_reasons"] == ["wht_not_withheld"]
    assert "OQ-29" in row["review_en"] and "المستشار الضريبي" in row["review_ar"]
    assert row["not_withheld_payments"] == [p.pk]
    assert not any(r.get("review_required") for r in rows if not r.get("not_withheld"))  # live rows unflagged
    assert data["difference"] == D("0.00")  # nothing not withheld was booked on 2220


def test_item6_a_zero_net_payee_is_a_review_row_too(w):
    r1, r2, run, s1, s2 = _near_zero_net(w, deduction="5000.00")
    assert s1.net_txn == D("0.000")
    p = _pay(w, run, "5000.00")
    assert p.wht_amount == D("250.000") and [x["employee_code"] for x in p.wht_parts] == [r2.employee_code]
    assert [(x["employee_code"], x["base_sar"], x["wht_not_withheld_sar"]) for x in p.wht_uncovered] == [
        (r1.employee_code, "18750.00", "937.50")]
    assert "no net pay" in " ".join(p.warnings) and "لا صافي" in " ".join(p.warnings)
    data, rows = _payroll_rows(w)
    review = [r for r in rows if r.get("review_required")]
    assert [(r["vendor"], r["wht_sar"], r["wht_not_withheld_sar"]) for r in review] == [
        (r1.employee_code, D("0.00"), D("937.50"))]
    assert data["difference"] == D("0.00")


def test_item6_a_reversed_payment_reverses_its_review_row_in_the_reversal_month(w):
    r1, r2, run, s1, s2 = _near_zero_net(w)
    p = _pay(w, run, str(s1.net_txn + s2.net_txn))
    from core.access.testing import make_user, member

    c3 = make_user("c3")
    member(c3, w.alm, "approver")
    services.reverse_payment(p, user=c3, reason="The bank rejected the transfer", date=date(2027, 4, 20))
    _, rows = _payroll_rows(w)
    review = sorted((r["wht_not_withheld_sar"], r.get("reversal", False)) for r in rows if r.get("not_withheld"))
    assert review == [(D("-937.46"), True), (D("937.46"), False)]


def test_item1_a_payee_whose_part_covers_the_wht_has_no_cap_and_no_review_row(w):
    r1, r2, run, s1, s2 = _near_zero_net(w, deduction="4000.00")  # net 1,000.00: WHT 250.00 fits
    p = _pay(w, run, str(s1.net_txn + s2.net_txn))
    assert p.wht_uncovered == [] and p.wht_amount == D("500.00")
    assert not any("OQ-29" in x for x in p.warnings or [])
    _, rows = _payroll_rows(w)
    assert not any(r.get("review_required") for r in rows)


# --- item 2: the alerts job goes on past a failing entity ------------------------------------------------------------

def test_item2_one_failing_entity_never_stops_the_entities_after_it(w, monkeypatch):
    """The reviewer's p1: the first entity's bai' ajel refresh fails; before, the job stopped there."""
    seen, alerted = [], []
    real_refresh, real_alerts = services._refresh_deferred_sales, services._entity_alerts

    def flaky(e):
        seen.append(e.code)
        if e.code == "ALM":
            raise RuntimeError("port down for ALM (test)")
        return real_refresh(e)

    def spy(e, today):
        alerted.append(e.code)
        return real_alerts(e, today)

    monkeypatch.setattr(services, "_refresh_deferred_sales", flaky)
    monkeypatch.setattr(services, "_entity_alerts", spy)
    with pytest.raises(services.AlertsFailed) as exc:
        services.send_alerts(today=date(2027, 3, 20))
    assert seen == ["ALM", "SUB"]  # EGY is not Saudi: no Mudad / GOSI alerts
    assert alerted == ["ALM", "SUB"]  # ALM's own alerts still run: the refresh commits apart from them
    assert [(code, step) for code, step, _ in exc.value.failures] == [("ALM", "bai_ajel_refresh")]
    assert "RuntimeError" in exc.value.failures[0][2] and exc.value.code == "alerts_failed"
    bilingual(exc.value)


def test_item2_a_failing_refresh_rolls_back_only_its_own_step(w, monkeypatch):
    c = sold(w)
    r = services.approve_deferred_sale_rebate(rebate(w, c, amount="120.00"), user=w.mp)
    cut = planned(c)
    w.invoicing.rebate_status = "rejected"

    def broken(*args, **kwargs):
        raise RuntimeError("alert store down (test)")

    monkeypatch.setattr(services, "_send_alerts", broken)
    with pytest.raises(services.AlertsFailed) as exc:
        services.send_alerts(today=date(2027, 3, 20))
    assert ("ALM", "bai_ajel_refresh") in [(code, step) for code, step, _ in exc.value.failures]
    assert planned(c) == cut and DeferredSaleRebate.objects.get(pk=r.pk).status == "approved"  # rolled back
    monkeypatch.undo()
    services.send_alerts(today=date(2027, 3, 20))  # read again the next day
    assert DeferredSaleRebate.objects.get(pk=r.pk).status == "rejected"


def test_item2_the_command_exits_non_zero_naming_the_failed_entity(w, monkeypatch):
    real = services._entity_alerts

    def flaky(e, today):
        if e.code == "SUB":
            raise RuntimeError("SUB down (test)")
        return real(e, today)

    monkeypatch.setattr(services, "_entity_alerts", flaky)
    with pytest.raises(CommandError) as exc:
        call_command("payroll_alerts")
    assert exc.value.returncode == 1 and "SUB (alerts)" in str(exc.value)
    monkeypatch.undo()
    call_command("payroll_alerts")  # nothing failing: exit 0


# --- reviewer's p3 / p4: after exit -------------------------------------------------------------------------------

def _rejected_after_exit(w):
    n1, c, r = settled_leaver_with_pending_rebate(w)
    w.invoicing.rebate_status = "rejected"
    services._refresh_deferred_sales(w.alm)
    return n1, c, DeferredSaleRebate.objects.get(pk=r.pk)


def test_p3_a_new_rebate_prepared_after_exit_is_refused_at_approval(w):
    n1, c, r = _rejected_after_exit(w)
    services.discard_deferred_sale_rebate(r, user=w.p)
    r2 = services.prepare_deferred_sale_rebate(DeferredSaleContract.objects.get(pk=c.pk), user=w.p, amount="10.00",
                                               reason_ar="سداد", request_ref="REQ-2")
    with pytest.raises(PayrollError) as exc:
        services.approve_deferred_sale_rebate(r2, user=w.mp)
    assert exc.value.code == "rebate_after_exit" and len(w.invoicing.rebates) == 1


def test_p4_a_rejection_read_twice_after_exit_restores_the_amount_once(w):
    n1, c, r = _rejected_after_exit(w)
    services._refresh_deferred_sales(w.alm)
    services.send_alerts(today=date(2027, 3, 20))
    rows = list(DeferredSaleInstallment.objects.filter(contract=c, status="outstanding_after_exit")
                .values_list("amount", flat=True))
    assert rows == [D("69.00")]


# --- item 4: who reaches the rebate credit note ----------------------------------------------------------------------

REBATE_CALLS = {"rebating", "create_bai_ajel_rebate", "create_deferred_sale_adjustment"}
REBATE_CALLERS = {"payroll/services.py", "invoicing/payroll_port.py"}  # the approval, and invoicing's port


def test_item4_only_payroll_services_and_the_invoicing_port_call_the_rebate_credit_note():
    """The rebating token protects against mistakes, not against in-process code (payroll.md §10.9 item 26): the
    guard is that nothing else calls these. Every non-test module of the project is parsed; a call, an attribute or
    a ``getattr`` string naming one of them outside the two callers fails."""
    root = Path(__file__).resolve().parent.parent
    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        parts = path.relative_to(root).parts
        if any(p.startswith(".") or p == "__pycache__" for p in parts) or parts[-1].startswith("test_") \
                or parts[-1] in ("conftest.py", "testing.py") or "migrations" in parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            name = (node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute)
                    else node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None)
            if name in REBATE_CALLS:
                found.append((rel, getattr(node, "lineno", 0), name))
    callers = {rel for rel, _, _ in found}
    assert callers <= REBATE_CALLERS, [f for f in found if f[0] not in REBATE_CALLERS]
    assert callers == REBATE_CALLERS  # the scan sees the real callers


# --- item 5: a bai' ajel balance left after exit, written off or recovered ------------------------------------------

def _after_exit_balance(w):
    """N1's contract with 69.00 outstanding after exit (a rebate credit note rejected after the final settlement),
    the rebate discarded."""
    n1, c, r = _rejected_after_exit(w)
    services.discard_deferred_sale_rebate(r, user=w.p)
    return n1, DeferredSaleContract.objects.get(pk=c.pk)


def _write_off(w, c, **kw):
    x = services.prepare_deferred_sale_closure(c, user=w.p, kind="write_off", reason_ar="تعذر التحصيل", **kw)
    return services.submit_deferred_sale_closure(x, user=w.p)


def _recovery(w, c, amount, on=date(2027, 3, 18)):
    x = services.prepare_deferred_sale_closure(c, user=w.p, kind="recovery", amount=amount, reason_ar="سداد الموظف",
                                               bank_account=services._account(w.alm, "1100"), received_on=on,
                                               bank_reference="TRF-1")
    return services.submit_deferred_sale_closure(x, user=w.p)


def _lines(x):
    return sorted((l.account.code, l.debit, l.credit) for l in JournalLine.objects.filter(entry=x.journal_entry))


def test_item5_write_off_posts_unearned_profit_to_1241_the_rest_to_5100_and_credits_1240(w):
    n1, c = _after_exit_balance(w)
    x = _write_off(w, c)
    assert (x.amount, x.profit_share, x.expense_account.code, x.status) == (D("69.00"), D("5.45"), "5100", "submitted")
    recognised = len(w.invoicing.recognised)
    x = services.approve_deferred_sale_closure(x, user=w.mp)
    assert len(w.invoicing.recognised) == recognised  # a write-off recognises no profit
    assert x.status == "approved" and x.number.startswith("ALM-BWO-")
    assert x.journal_entry.status == "posted" and x.journal_entry.document_type == "payroll.deferredsaleclosure"
    assert _lines(x) == [("1240", D("0"), D("69.00")), ("1241", D("5.45"), D("0")), ("5100", D("63.55"), D("0"))]
    assert set(c.installments.values_list("status", flat=True)) - {"settled_in_final"} == {"written_off"}
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "settled"
    log = AccessLog.objects.filter(details__action="bai_ajel_write_off").get()
    assert log.details["message_ar"] and log.details["amount"] == "69.00"


def test_item5_write_off_unearned_profit_is_read_from_invoicing_again_at_approval(w):
    n1, c = _after_exit_balance(w)
    x = _write_off(w, c)
    w.invoicing.unearned = D("60.00")  # e.g. the real figure on 1241
    x = services.approve_deferred_sale_closure(x, user=w.mp)
    assert _lines(x) == [("1240", D("0"), D("69.00")), ("1241", D("60.00"), D("0")), ("5100", D("9.00"), D("0"))]


def test_item5_write_off_refusals(w):
    n1, c, r = _rejected_after_exit(w)
    with pytest.raises(PayrollError) as exc:  # the rejected rebate is open: discard it first
        _write_off(w, c)
    assert exc.value.code == "rebate_open" and bilingual(exc.value)
    services.discard_deferred_sale_rebate(r, user=w.p)
    with pytest.raises(PayrollError) as exc:
        services.prepare_deferred_sale_closure(c, user=w.p, kind="write_off", reason_ar="")
    assert exc.value.code == "reason_required"
    with pytest.raises(PayrollError) as exc:
        services.prepare_deferred_sale_closure(c, user=w.p, kind="discount", reason_ar="سبب")
    assert exc.value.code == "closure_kind"
    with pytest.raises(PayrollError) as exc:  # an income account is not a write-off account
        _write_off(w, c, expense_account="4610")
    assert exc.value.code == "write_off_account"
    with pytest.raises(PermissionDenied):  # the approver does not prepare
        services.prepare_deferred_sale_closure(c, user=w.mp, kind="write_off", reason_ar="سبب")
    x = _write_off(w, c)
    with pytest.raises(PayrollError) as exc:  # one open closure per contract
        _write_off(w, c)
    assert exc.value.code == "closure_exists"
    with pytest.raises(PermissionDenied):  # the CFO-level approver role without the write-off right
        services.approve_deferred_sale_closure(x, user=w.c)
    with pytest.raises(PermissionDenied):  # four eyes: never the preparer (no right either)
        services.approve_deferred_sale_closure(x, user=w.p)
    w.invoicing.unearned = D("70.00")  # more than the balance: refused, nothing posted
    with pytest.raises(PayrollError) as exc:
        services.approve_deferred_sale_closure(x, user=w.mp)
    assert exc.value.code == "closure_profit"
    assert DeferredSaleClosure.objects.get(pk=x.pk).status == "submitted"
    services.reject_deferred_sale_closure(x, user=w.mp, reason_ar="مراجعة")
    assert DeferredSaleClosure.objects.get(pk=x.pk).status == "rejected"


def test_item5_four_eyes_against_the_preparer_holding_the_approval_right(w):
    from core.access.testing import member

    n1, c = _after_exit_balance(w)
    member(w.mp, w.alm, "payroll_officer")  # the MP also prepares: never approves their own
    x = services.prepare_deferred_sale_closure(c, user=w.mp, kind="write_off", reason_ar="تعذر التحصيل")
    x = services.submit_deferred_sale_closure(x, user=w.p)
    with pytest.raises(PermissionDenied):
        services.approve_deferred_sale_closure(x, user=w.mp)
    assert not DeferredSaleClosure.objects.get(pk=x.pk).journal_entry_id


def test_item5_no_closure_before_the_final_settlement_or_with_planned_instalments(w):
    c = sold(w)
    with pytest.raises(PayrollError) as exc:
        _write_off(w, c)
    assert exc.value.code == "closure_before_settlement" and bilingual(exc.value)


def test_item5_no_closure_while_a_rebate_note_is_pending(w):
    n1, c, r = settled_leaver_with_pending_rebate(w)  # the note is issued, not final: 1240 may still change
    with pytest.raises(PayrollError) as exc:
        _write_off(w, c)
    assert exc.value.code in ("rebate_pending", "closure_amount")
    w.invoicing.rebate_status = "final"
    services._refresh_deferred_sales(w.alm)
    with pytest.raises(PayrollError) as exc:  # final: the note credited 1240, nothing is left after exit
        _write_off(w, c)
    assert exc.value.code == "closure_amount"


def test_item5_the_balance_changed_between_preparation_and_approval(w, monkeypatch):
    n1, c = _after_exit_balance(w)
    x = _write_off(w, c)
    inst = DeferredSaleInstallment.objects.get(contract=c, status="outstanding_after_exit")
    services._save(DeferredSaleInstallment(entity=c.entity, contract=c, period=inst.period, amount=D("1.00"),
                                           carried_from=inst, status="outstanding_after_exit"))  # test data
    with pytest.raises(PayrollError) as exc:
        services.approve_deferred_sale_closure(x, user=w.mp)
    assert exc.value.code == "closure_amount" and bilingual(exc.value)


def test_item5_recovery_in_part_then_in_full_recognises_profit_through_the_port(w):
    n1, c = _after_exit_balance(w)
    x = services.approve_deferred_sale_closure(_recovery(w, c, "30.00"), user=w.mp)
    assert x.number.startswith("ALM-BRC-") and x.journal_entry.date == date(2027, 3, 18)
    assert _lines(x) == [("1100", D("30.00"), D("0")), ("1240", D("0"), D("30.00"))]
    rec = DeferredSaleInstallment.objects.get(pk=x.installment_parts[0]["installment_id"])
    assert (rec.status, rec.amount, rec.deducted_amount) == ("recovered_after_exit", D("30.00"), D("30.00"))
    left = DeferredSaleInstallment.objects.get(contract=c, status="outstanding_after_exit")
    assert (left.amount, left.carried_from_id) == (D("39.00"), rec.pk)
    assert w.invoicing.recognised[-1][1:4] == (D("30.00"), date(2027, 3, 18),
                                                f"payroll.deferredsaleinstallment:{rec.pk}")
    assert w.invoicing.recognised[-1][4] == w.mp.pk
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "sold"
    with pytest.raises(PayrollError) as exc:  # never more than is left
        _recovery(w, c, "39.01")
    assert exc.value.code == "closure_amount"
    x2 = services.approve_deferred_sale_closure(_recovery(w, c, "39.00"), user=w.mp)
    assert w.invoicing.recognised[-1][1] == D("39.00") and x2.status == "approved"
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "settled"
    with pytest.raises(PayrollError):  # nothing left to close
        _write_off(w, c)


def test_item5_recovery_refusals(w):
    n1, c = _after_exit_balance(w)
    with pytest.raises(PayrollError) as exc:
        services.prepare_deferred_sale_closure(c, user=w.p, kind="recovery", amount="10.00", reason_ar="سداد")
    assert exc.value.code == "recovery_invalid"
    with pytest.raises(PayrollError) as exc:
        _recovery(w, c, "10.00", on=date(2027, 12, 31))  # in the future
    assert exc.value.code == "recovery_invalid"
    with pytest.raises(PayrollError) as exc:
        _recovery(w, c, "10.001")
    assert exc.value.code == "amount_invalid"
    with pytest.raises(PayrollError) as exc:
        _recovery(w, c, 10.0)
    assert exc.value.code == "float_rejected"
    with pytest.raises(PayrollError) as exc:
        services.prepare_deferred_sale_closure(c, user=w.p, kind="recovery", amount="10.00", reason_ar="سداد",
                                               bank_account=services._account(w.alm, "5100"),
                                               received_on=date(2027, 3, 18))
    assert exc.value.code == "bank_account_invalid"


def test_item5_profit_of_a_recovery_is_recognised_only_for_its_own_installments(w):
    n1, c = _after_exit_balance(w)
    x = services.approve_deferred_sale_closure(_recovery(w, c, "30.00"), user=w.mp)
    rec = x.installment_parts[0]["installment_id"]
    left = DeferredSaleInstallment.objects.get(contract=c, status="outstanding_after_exit")
    ref = f"payroll.deferredsaleinstallment:{rec}"
    assert invoicing_port.collection_problems(c, source_ref=ref, amount=D("30.00"), user=w.mp)[0]  # no marker
    with invoicing_port.collecting(x, w.mp):
        assert invoicing_port.collection_problems(c, source_ref=ref, amount=D("30.00"), user=w.mp) == (
            [], "payroll.approve_deferred_sale_closure")  # its own approval right (access 10.5, 2026-10-21)
        assert invoicing_port.collection_problems(c, source_ref=ref, amount=D("29.00"), user=w.mp)[0]
        assert invoicing_port.collection_problems(  # still outstanding: not collected by this recovery
            c, source_ref=f"payroll.deferredsaleinstallment:{left.pk}", amount=left.amount, user=w.mp)[0]
    with invoicing_port.collecting(SimpleNamespace(_meta=x._meta, pk=x.pk + 999), w.mp):  # another closure
        assert invoicing_port.collection_problems(c, source_ref=ref, amount=D("30.00"), user=w.mp)[0]


def test_item5_closures_are_entity_scoped(w):
    n1, c = _after_exit_balance(w)
    with pytest.raises(PermissionDenied):  # SUB's payroll officer has no right in ALM
        services.prepare_deferred_sale_closure(c, user=w.ps, kind="write_off", reason_ar="سبب")


def test_item5_no_fee_penalty_or_rate_field_on_the_closure():
    banned = ("rate", "interest", "penalty", "late", "fee", "per_annum", "charge")
    names = [f.name for f in DeferredSaleClosure._meta.get_fields()]
    assert not [n for n in names if any(b in n for b in banned)], names


def test_item5_closure_is_service_written_and_bilingual():
    x = DeferredSaleClosure(amount=Decimal("1.00"))
    with pytest.raises(PayrollError):
        x.save()
    assert " / " in str(DeferredSaleClosure._meta.verbose_name)
    for choice in (*DeferredSaleClosure.Kind.choices, *DeferredSaleClosure.Status.choices):
        assert " / " in str(choice[1])
