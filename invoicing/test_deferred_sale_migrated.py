"""Bai' ajel contracts migrated at the cut-over (migration W14, R22, R25; invoicing R112; payroll R47 to R49), end to
end (review of aa856fa, items 2 to 4): the real payroll runs, final settlements and closures against the real invoicing
port, after a real migration opening entry carrying the contract's 1240 / 1241 balances is posted. Anonymised
fixtures, fictional numbers, a fixed clock; nothing external.

The migrated contract: sale price 2,000.00 (profit 200.00), VAT 300.00, 4 instalments of 575.00, 1,150.00 paid before
the cut-over 2026-12-31. Open at the cut-over: 1,150.00 on 1240 and 100.00 still deferred on 1241."""
from datetime import date

import pytest

from core.access import AccessDenied
from core.access.models import AccessLog
from invoicing.conftest import zatca  # noqa: F401  (invoicing's fake ZATCA port, a fixture)
from invoicing.models import DeferredProfitRelease, DeferredSale
from invoicing.test_deferred_sale import balance, bw, invoicing  # noqa: F401  (the real port and its world)
from invoicing.test_deferred_sale_review3 import after_exit, run
from ledger.services_setup import create_monthly_periods
from migration import readers
from migration import services as migration
from migration import testing as t
from payroll import services as payroll
from payroll.conftest import D, amount, clock, pclock, saudi, w  # noqa: F401
from payroll.models import DeferredSaleContract, FinalSettlement
from payroll.test_final_settlement import go_live, leaver, settle, terminate

CUT = date(2026, 12, 31)
CONTRACT = dict(legacy_number="BAJ-M-1", asset_description_ar="سيارة مرحلة", sale_date=date(2026, 3, 1),
                sale_price=D("2000.00"), profit_amount=D("200.00"), vat_amount=D("300.00"), instalments=4,
                paid_to_date=D("1150.00"), next_period="2027-03", cutover=CUT)
# Falcon code -> ERP code; the opening balances of the contract (debit positive) and the equity making it balance
MAP = {"1251": ("ذمم البيع الآجل", "1240"), "1252": ("أرباح مؤجلة", "1241"), "2601": ("رأس المال", "3100")}
TB = {"1251": "1150.00", "1252": "-100.00", "2601": "-1050.00"}


def books(w):
    return {a: balance(w.alm, a) for a in ("1240", "1241", "4610")}


def migrate(w, emp, *, consent_ref="SIGNED-M-1", post=True, **changes):
    """Loads the contract, then the migration opening entry (A drafts the map and the TB, C approves and posts)."""
    create_monthly_periods(w.alm, 2026)
    c = payroll.load_migrated_deferred_sale(emp, user=w.p, consent_ref=consent_ref, **{**CONTRACT, **changes})
    amap = migration.create_account_map(w.alm, user=w.a, rows=[{"legacy_code": k, "legacy_name": n, "erp_code": e}
                                                                for k, (n, e) in MAP.items()])
    t.approve_map(amap, approver=w.c)
    rows = t.tb_rows({k: (MAP[k][0], v, "0", "0") for k, v in TB.items()})
    tb = readers.read_trial_balance(t.table_csv(t.TB_HEADER, rows), {"columns": t.TB_COLUMNS})
    assert migration.load_opening_balances(w.alm, tb, user=w.a, cutover=CUT, apply=True).applied
    if post:
        result = migration.post_imported_entries(w.alm, user=w.c, kind="opening", apply=True)
        assert result["posted"], result["details"]
    return DeferredSaleContract.objects.get(pk=c.pk)


def sale_of(c):
    return DeferredSale.objects.get(source_id=c.pk, active=True)


def test_item3_runs_collect_a_migrated_contract_and_release_its_deferred_profit(bw, django_capture_on_commit_callbacks):
    w = bw
    emp = saudi(w, hire_date=date(2025, 1, 1))
    c = migrate(w, emp, post=False)
    assert not sale_of(c).is_posted  # item 4: not before the entity's opening entry is posted
    assert migration.post_imported_entries(w.alm, user=w.c, kind="opening", apply=True)["posted"]
    assert sale_of(c).is_posted
    assert books(w) == {"1240": D("1150.00"), "1241": D("-100.00"), "4610": D("0")}
    march = run(w, "2027-03", django_capture_on_commit_callbacks)
    assert amount(march, emp, "deferred_sale_installment") == D("575.00")
    release = DeferredProfitRelease.objects.get()  # Dr 1241 / Cr 4610: 100.00 x 575.00 / 1,150.00
    assert release.profit_amount == D("50.00") and release.journal_entry.status == "posted"
    lines = {(l.account.code, l.debit, l.credit) for l in release.journal_entry.lines.select_related("account")}
    assert lines == {("1241", D("50.00"), D("0")), ("4610", D("0"), D("50.00"))}
    assert books(w) == {"1240": D("575.00"), "1241": D("-50.00"), "4610": D("-50.00")}
    w.pclock.set(date(2027, 4, 20))
    run(w, "2027-04", django_capture_on_commit_callbacks)
    # the last collection releases the rest: nothing is left deferred, the whole open profit is income
    assert books(w) == {"1240": D("0.00"), "1241": D("0.00"), "4610": D("-100.00")}
    assert DeferredProfitRelease.objects.count() == 2
    assert not c.installments.filter(status="planned").exists()


def test_item2_without_consent_the_deductions_are_held_until_a_person_records_it(bw, django_capture_on_commit_callbacks):
    w = bw
    emp = saudi(w, hire_date=date(2025, 1, 1))
    c = migrate(w, emp, consent_ref="")
    assert c.consent_ref == ""  # never invented (R49)
    march = run(w, "2027-03", django_capture_on_commit_callbacks)
    assert amount(march, emp, "deferred_sale_installment") == D("0")
    assert not DeferredProfitRelease.objects.exists() and books(w)["1240"] == D("1150.00")
    for user in (w.a, w.h):  # the payroll officer's right (payroll.create_deferred_sale)
        with pytest.raises(AccessDenied):
            payroll.record_deferred_sale_consent(c, user=user, consent_ref="SIGNED-M-1")
    with pytest.raises(Exception) as exc:
        payroll.record_deferred_sale_consent(c, user=w.p, consent_ref="  ")
    assert getattr(exc.value, "code", "") == "consent_required"
    c = payroll.record_deferred_sale_consent(c, user=w.p, consent_ref="SIGNED-M-1")
    assert c.consent_ref == "SIGNED-M-1"
    assert AccessLog.objects.filter(actor=w.p, event="deduction_consent_recorded",  # its own event (review item 10)
                                    details__action="record_bai_ajel_consent",
                                    details__consent_ref="SIGNED-M-1").exists()
    with pytest.raises(Exception) as exc:  # recorded once
        payroll.record_deferred_sale_consent(c, user=w.p, consent_ref="SIGNED-M-2")
    assert getattr(exc.value, "code", "") == "consent_recorded"
    w.pclock.set(date(2027, 4, 20))
    april = run(w, "2027-04", django_capture_on_commit_callbacks)
    # the held March instalment is collected with April's once the consent is recorded (the run's catch-up)
    assert amount(april, emp, "deferred_sale_installment") == D("1150.00")
    assert books(w) == {"1240": D("0.00"), "1241": D("0.00"), "4610": D("-100.00")}


def test_item2_an_erp_contract_records_its_consent_with_its_terms(bw):
    w = bw
    emp = saudi(w)
    c = payroll.create_deferred_sale(emp, user=w.p, asset_description_ar="سيارة", cost="1000.00", profit_amount="100.00",
                                     term_months=4, first_period="2027-04", consent_ref="SIGNED-1")
    with pytest.raises(Exception) as exc:
        payroll.record_deferred_sale_consent(c, user=w.p, consent_ref="SIGNED-2")
    assert getattr(exc.value, "code", "") == "not_migrated"


def test_item1_r25_payroll_refuses_contracts_and_eosb_openings_once_the_opening_is_posted(bw):
    w = bw
    emp = saudi(w, hire_date=date(2025, 1, 1))
    migrate(w, emp)
    other = saudi(w, hire_date=date(2025, 1, 1))
    with pytest.raises(Exception) as exc:
        payroll.load_migrated_deferred_sale(other, user=w.p, **{**CONTRACT, "legacy_number": "BAJ-M-2"})
    assert getattr(exc.value, "code", "") == "opening_posted" and "R25" in str(exc.value)
    with pytest.raises(Exception) as exc:
        payroll.record_opening_eosb_provision(other, user=w.p, amount=D("100.00"), on=CUT)
    assert getattr(exc.value, "code", "") == "opening_posted"
    assert DeferredSaleContract.objects.filter(pre_go_live=True).count() == 1
    # the same contract again is still the same contract (idempotent re-run, nothing added)
    assert payroll.load_migrated_deferred_sale(emp, user=w.p, consent_ref="SIGNED-M-1", **CONTRACT) is not None


def _exit_with_balance(w, django_capture_on_commit_callbacks):
    """A leaver with a migrated contract and no recorded consent: the final settlement deducts nothing of it, so the
    1,150.00 stays on 1240 as outstanding after exit."""
    n1 = leaver(w)
    c = migrate(w, n1, consent_ref="")
    tm = terminate(w, n1)
    go_live(w)
    with django_capture_on_commit_callbacks(execute=True):
        fs = payroll.approve_final_settlement(FinalSettlement.objects.get(pk=settle(w, tm).pk), user=w.c)
    assert fs.status == "approved" and not fs.lines.filter(line_type="deferred_sale_recovery").exists()
    c = DeferredSaleContract.objects.get(pk=c.pk)
    assert after_exit(c) == D("1150.00") and books(w) == {"1240": D("1150.00"), "1241": D("-100.00"), "4610": D("0")}
    return c


def test_item3_a_migrated_contract_left_after_exit_is_written_off(bw, django_capture_on_commit_callbacks):
    w = bw
    c = _exit_with_balance(w, django_capture_on_commit_callbacks)
    x = payroll.prepare_deferred_sale_closure(c, user=w.p, kind="write_off", reason_ar="تعذر التحصيل")
    assert x.profit_share == D("100.00")
    expense = balance(w.alm, "5100")
    x = payroll.approve_deferred_sale_closure(payroll.submit_deferred_sale_closure(x, user=w.p), user=w.mp)
    assert x.journal_entry.status == "posted"
    assert books(w) == {"1240": D("0.00"), "1241": D("0.00"), "4610": D("0")}  # unearned profit is never income
    assert balance(w.alm, "5100") - expense == D("1050.00")
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "settled"


def test_item3_a_migrated_contract_left_after_exit_is_recovered(bw, django_capture_on_commit_callbacks):
    w = bw
    c = _exit_with_balance(w, django_capture_on_commit_callbacks)
    bank = payroll._account(w.alm, "1100")
    first = payroll.prepare_deferred_sale_closure(c, user=w.p, kind="recovery", amount="575.00", reason_ar="سداد جزئي",
                                                  bank_account=bank, received_on=date(2027, 3, 20))
    payroll.approve_deferred_sale_closure(payroll.submit_deferred_sale_closure(first, user=w.p), user=w.mp)
    assert books(w) == {"1240": D("575.00"), "1241": D("-50.00"), "4610": D("-50.00")}
    second = payroll.prepare_deferred_sale_closure(c, user=w.p, kind="recovery", amount="575.00", reason_ar="سداد",
                                                   bank_account=bank, received_on=date(2027, 3, 20))
    payroll.approve_deferred_sale_closure(payroll.submit_deferred_sale_closure(second, user=w.p), user=w.mp)
    assert books(w) == {"1240": D("0.00"), "1241": D("0.00"), "4610": D("-100.00")}
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "settled"
