from django.apps import AppConfig


class PayrollConfig(AppConfig):
    """Payroll, slice 1 (docs/specs/payroll.md). Registers the module's ledger documents (§5, R11):

    - ``PayrollRun``: posted at approval with ``payroll.approve_run`` (hr_officer, P14); reversed with
      ``payroll.reverse_run`` only inside ``payroll.services.reverse_run`` (``reverse_guard``); ``pending_documents``
      keeps ``close_period`` from closing a period with a submitted run (R12).
    - ``PayrollPayment``: ``payroll.approve_payment`` (E2, E3, E8b).
    - ``EmployeeLoan``: ``payroll.approve_loan`` (E4, interest-free).

    Every type builds its own lines from stored data, only inside a ``payroll.services`` posting.
    """

    name = "payroll"
    verbose_name = "Payroll / الرواتب"

    def ready(self):
        from ledger import documents

        from . import services
        from .models import EmployeeLoan, PayrollPayment, PayrollRun

        documents.register(PayrollRun, post_permissions={"payroll.approve_run"},
                           reverse_permissions={"payroll.reverse_run"}, build_lines=services._run_lines,
                           pending_documents=services._pending_runs, reverse_guard=services._reverse_guard)
        documents.register(PayrollPayment, post_permissions={"payroll.approve_payment"},
                           build_lines=services._payment_lines)
        documents.register(EmployeeLoan, post_permissions={"payroll.approve_loan"},
                           build_lines=services._loan_disbursement_lines)
