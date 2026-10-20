"""PostgreSQL only: concurrent approvals (review item 1, 17). Ported from the reviewer's test_zz_pg_probes.py (PG1).

Each case starts two approvals in two threads at the same moment, with the open-amount read slowed down so that,
without the payroll locks, both would read the same open amount and both would post."""
import threading
import time
from datetime import date

import pytest
from django.db import connection, connections

from payroll import services
from payroll.conftest import D, approved, saudi, slip
from payroll.models import EmployeeLoan, FinalSettlement, PayrollPayment, PayrollRun

pytestmark = [pytest.mark.skipif(connection.vendor != "postgresql", reason="row locks need PostgreSQL"),
              # Committed transactions: the threads see each other's rows.
              pytest.mark.django_db(transaction=True)]

SEED_MIGRATIONS = (("hr.migrations.0002_seed_statutory_parameters", "seed"),
                   ("hr.migrations.0005_unpaid_suspension_parameter", "forward"),
                   ("hr.migrations.0017_seed_refresh_eid_gosi", "forward"),
                   ("payroll.migrations.0002_seed_parameters_accounts", "forwards"))


@pytest.fixture(autouse=True)
def seeded(transactional_db):
    """Transactional tests flush the database: put back the migration-seeded rows the ``w`` fixture relies on
    (roles, currencies, system users, statutory parameters), as invoicing's concurrency tests do."""
    from importlib import import_module

    from django.apps import apps
    from django.contrib.auth import get_user_model

    from core.access.models import Role, RolePermission, UserProfile
    from core.access.seed import apply_seed, apply_system_users
    from core.currency_seed import apply_currency_seed
    from core.models import Currency
    from hr.models import StatutoryParameter

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    apply_system_users(get_user_model(), UserProfile)
    if not StatutoryParameter.objects.exists():
        for module, name in SEED_MIGRATIONS:
            getattr(import_module(module), name)(apps, None)


def _race(monkeypatch, name, calls):
    """Run ``calls`` [(fn, kwargs)] in threads that start together; ``services.<name>`` sleeps after reading."""
    original = getattr(services, name)

    def slow(*args, **kwargs):
        out = original(*args, **kwargs)
        time.sleep(0.5)
        return out

    monkeypatch.setattr(services, name, slow)
    barrier = threading.Barrier(len(calls), timeout=20)
    results = [None] * len(calls)

    def go(i, fn, kwargs):
        try:
            barrier.wait()
            fn(**kwargs)
            results[i] = "ok"
        except Exception as exc:  # noqa: BLE001 - the refusal is the result under test
            results[i] = getattr(exc, "code", None) or type(exc).__name__
        finally:
            connections.close_all()

    threads = [threading.Thread(target=go, args=(i, fn, kw)) for i, (fn, kw) in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


def test_review1_two_salary_payments_approved_at_once_never_exceed_the_net(w, monkeypatch):
    s1 = saudi(w)
    run = approved(w, "2027-03")
    net = slip(run, s1).net
    payments = []
    for user in (w.p, w.p2):
        p = services.record_payment(w.alm, user=user, kind="net_salaries", run=run, payment_date=date(2027, 3, 27),
                                    amount=str(net))
        payments.append(services.submit_payment(p, user=user))
    results = _race(monkeypatch, "_run_payable",
                    [(services.approve_payment, dict(payment=p, user=w.c)) for p in payments])
    assert sorted(results) == ["ok", "overpayment"]
    total = sum(p.amount for p in PayrollPayment.objects.filter(run=run, status="approved"))
    assert total == net and PayrollRun.objects.get(pk=run.pk).status == "paid"


def test_review1_two_gosi_payments_approved_at_once_never_exceed_the_period(w, monkeypatch):
    saudi(w)
    approved(w, "2027-03")
    open_ = services._gosi_open(w.alm, "2027-03")
    payments = []
    for user in (w.p, w.p2):
        p = services.record_payment(w.alm, user=user, kind="gosi", gosi_period="2027-03",
                                    payment_date=date(2027, 4, 10), amount=str(open_))
        payments.append(services.submit_payment(p, user=user))
    results = _race(monkeypatch, "_gosi_open", [(services.approve_payment, dict(payment=p, user=w.c)) for p in payments])
    assert sorted(results) == ["ok", "overpayment"]
    assert services._gosi_open(w.alm, "2027-03") == 0


def test_review1_payment_and_reversal_at_once_never_both_happen(w, monkeypatch):
    s1 = saudi(w)
    run = approved(w, "2027-03")
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 3, 27),
                                amount=str(slip(run, s1).net))
    p = services.submit_payment(p, user=w.p)
    from core.access.testing import make_user, member
    c3 = make_user("c3")
    member(c3, w.alm, "approver")
    results = _race(monkeypatch, "_reversal_date",
                    [(services.approve_payment, dict(payment=p, user=w.c)),
                     (services.reverse_run, dict(run=run, user=c3, reason="error"))])
    run = PayrollRun.objects.get(pk=run.pk)
    paid = PayrollPayment.objects.filter(run=run, status="approved").exists()
    assert results.count("ok") == 1
    assert (run.status == "reversed") != paid


def test_review17_two_loans_of_one_employee_approved_at_once_respect_the_cap(w, monkeypatch):
    s1 = saudi(w)  # BASIC 10,000.00 -> cap 30,000.00
    loans = []
    for _ in range(2):
        loan = services.create_loan(s1, user=w.p, kind="loan", principal="20000.00", disbursement_date=date(2027, 2, 1),
                                    purpose_ar="قرض حسن", installments=[("2027-06", "20000.00")])
        loans.append(services.submit_loan(loan, user=w.p))
    results = _race(monkeypatch, "_loan_outstanding",
                    [(services.approve_loan, dict(loan=loan, user=w.mp)) for loan in loans])
    assert sorted(results) == ["loan_cap", "ok"]
    assert EmployeeLoan.objects.filter(employee=s1, status="active").count() == 1
    assert D("20000.00") == sum(x.principal for x in EmployeeLoan.objects.filter(employee=s1, status="active"))


# Re-review 15 (reviewer's test_zz_pgprobe.py): final settlement payment and approval races.

def test_rr15_two_final_settlement_payments_approved_at_once_never_exceed_the_net(w, monkeypatch):
    from payroll.test_final_settlement import go_live, leaver, settle, terminate

    t = terminate(w, leaver(w))
    go_live(w)
    fs = services.approve_final_settlement(settle(w, t), user=w.c)
    payments = []
    for user in (w.p, w.p2):
        p = services.record_payment(w.alm, user=user, kind="final_settlement", final_settlement=fs,
                                    payment_date=date(2027, 3, 20), amount=str(fs.net))
        payments.append(services.submit_payment(p, user=user))
    results = _race(monkeypatch, "_payment_room",
                    [(services.approve_payment, dict(payment=p, user=w.c)) for p in payments])
    assert sorted(results) == ["ok", "overpayment"]
    assert sum(p.amount for p in PayrollPayment.objects.filter(final_settlement=fs, status="approved")) == fs.net
    assert FinalSettlement.objects.get(pk=fs.pk).status == "paid"


def test_rr15_two_approvals_of_one_final_settlement_at_once_post_once(w, monkeypatch):
    from payroll.test_final_settlement import go_live, leaver, settle, terminate

    t = terminate(w, leaver(w))
    go_live(w)
    fs = settle(w, t)
    results = _race(monkeypatch, "_compute_settlement",
                    [(services.approve_final_settlement, dict(settlement=fs, user=u)) for u in (w.c, w.mp)])
    assert sorted(results) == ["ok", "settlement_status"]
    fs = FinalSettlement.objects.get(pk=fs.pk)
    assert fs.status == "approved" and fs.number.endswith("000001") and fs.journal_entry_id
