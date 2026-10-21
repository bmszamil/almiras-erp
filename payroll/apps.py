from django.apps import AppConfig


class PayrollConfig(AppConfig):
    """Payroll, slice 1 (docs/specs/payroll.md). Registers the module's ledger documents (§5, R11):

    - ``PayrollRun``: posted at approval with ``payroll.approve_run`` (hr_officer, P14); reversed with
      ``payroll.reverse_run`` only inside ``payroll.services.reverse_run`` (``reverse_guard``); ``pending_documents``
      keeps ``close_period`` from closing a period with a submitted run (R12).
    - ``PayrollPayment``: ``payroll.approve_payment`` (E2, E3, E8b).
    - ``EmployeeLoan``: ``payroll.approve_loan`` (E4, interest-free).
    - ``FinalSettlement``: ``payroll.approve_final_settlement`` (E5); ``pending_documents`` for submitted ones.

    Slice 2 (§10.8): ``SettlementCorrection`` (``payroll.approve_settlement_correction``, difference lines),
    ``LoanWriteOff`` (``payroll.approve_loan_write_off``, Dr expense / Cr 1230), ``GosiFine``
    (``payroll.approve_gosi_fine``, E14); ``PayrollPayment`` is reversed with ``payroll.reverse_payment`` only inside
    ``payroll.services.reverse_payment``.

    Review 4 (2026-10-21): ``DeferredSaleClosure`` (``payroll.approve_deferred_sale_closure``): a bai' ajel balance left after
    exit written off (posts 1240 and 1241) or recovered (posts 1240).

    Every type builds its own lines from stored data, only inside a ``payroll.services`` posting.
    """

    name = "payroll"
    verbose_name = "Payroll / الرواتب"

    def ready(self):
        from ledger import documents, restricted

        from . import services
        from .models import (DeferredSaleClosure, EmployeeLoan, FinalSettlement, GosiFine, LoanWriteOff,
                             PayrollPayment, PayrollRun, SettlementCorrection)

        documents.register(PayrollRun, post_permissions={"payroll.approve_run"},
                           reverse_permissions={"payroll.reverse_run"}, build_lines=services._run_lines,
                           pending_documents=services._pending_runs, reverse_guard=services._reverse_guard)
        documents.register(PayrollPayment, post_permissions={"payroll.approve_payment"},
                           reverse_permissions={"payroll.reverse_payment"}, build_lines=services._payment_lines,
                           reverse_guard=services._reverse_guard)
        documents.register(EmployeeLoan, post_permissions={"payroll.approve_loan"},
                           build_lines=services._loan_disbursement_lines)
        documents.register(FinalSettlement, post_permissions={"payroll.approve_final_settlement"},
                           build_lines=services._settlement_lines, pending_documents=services._pending_settlements)
        documents.register(SettlementCorrection, post_permissions={"payroll.approve_settlement_correction"},
                           build_lines=services._correction_lines)
        documents.register(LoanWriteOff, post_permissions={"payroll.approve_loan_write_off"},
                           build_lines=services._write_off_lines)
        documents.register(GosiFine, post_permissions={"payroll.approve_gosi_fine"}, build_lines=services._fine_lines)
        # Review fix 3 (invoicing R127): 1240 is restricted by invoicing; payroll's runs and final settlements post it
        # (the bai' ajel instalments they collect, E11, E13), and so do their reversals.
        for doc_type in ("payroll.payrollrun", "payroll.finalsettlement"):
            restricted.allow("1240", document_type=doc_type)
        # Review 4 (2026-10-21, item 5): a bai' ajel balance left after exit, written off (Dr 1241 unearned profit /
        # Dr write-off account / Cr 1240) or recovered (Dr bank / Cr 1240); four eyes under the write-off right.
        documents.register(DeferredSaleClosure, post_permissions={"payroll.approve_deferred_sale_closure"},
                           build_lines=services._closure_lines)
        restricted.allow("1240", document_type="payroll.deferredsaleclosure")
        restricted.allow("1241", document_type="payroll.deferredsaleclosure", predicate=services._closure_entry_allowed)
        # OQ-30 (R43): payroll's remuneration WHT goes into invoicing's monthly WHT return and its 2220 tie-out.
        from invoicing import services as invoicing_services
        invoicing_services.register_wht_return_source("payroll.payrollpayment", services._wht_return_source)

        from . import approval_adapters

        approval_adapters.register()  # access R61: the module's documents go through the one approval engine
