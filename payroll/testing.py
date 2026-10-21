"""Test helpers for payroll: a fake invoicing port for the bai' ajel sale. Never used outside tests."""
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

from . import invoicing_port


class FakeInvoicingPort:
    """Stands in for invoicing R112 (the real port is ``invoicing.payroll_port.PayrollInvoicingPort``): VAT 15% on the
    sale price, charged to the employee (P20(h)); ``posted`` says whether the sale invoice is posted (ZATCA reported);
    profit recognitions and reversals are recorded, nothing is booked."""

    def __init__(self):
        self.calls = []
        self.posted = True
        self.status = "final"
        self.recognised = []  # (contract pk, amount, on, source_ref, user pk)
        self.reversed = []  # (contract pk, source_ref, user pk)
        self.rebates = []  # (contract pk, source_ref, amount, user pk)
        self.rebate_status = "issued"
        self.unearned = None  # review 4: what unearned_bai_ajel_profit reports (None: pro rata, see there)

    def create_bai_ajel_sale(self, contract, *, user):
        vat = (contract.sale_price * Decimal("0.15")).quantize(Decimal("0.01"))
        self.calls.append((contract.pk, user.pk, vat))
        return invoicing_port.SaleResult(document_ref=f"TEST-SI-{contract.pk}-{len(self.calls)}", vat_amount=vat)

    def sale_state(self, contract):
        if not self.calls:
            return None
        return SimpleNamespace(document_ref=f"TEST-SI-{contract.pk}-{len(self.calls)}", status=self.status,
                               posted=self.posted)

    def recognise_bai_ajel_profit(self, contract, *, amount, on, source_ref, user):
        self.recognised.append((contract.pk, amount, on, source_ref, user.pk))
        return SimpleNamespace(entry_ref="", profit_amount=Decimal("0"), collected_to_date=amount, reversed=False)

    def create_bai_ajel_rebate(self, contract, *, user, source_ref, amount, reason_ar, reason_en=""):
        """R128: a credit note of the rebate at 15% VAT; ``rebate_status`` is what ``rebate_state`` reports. Refused,
        like the real port, outside payroll's ``rebating`` marker (review 2026-10-20b, finding 1)."""
        if invoicing_port.rebate_problems(contract, source_ref=source_ref, amount=Decimal(amount), approver=user):
            raise invoicing_port.error("Not from payroll's rebate approval (test port).", "ليس من اعتماد الإبراء.",
                                       code="rebate_not_from_payroll")
        vat = (Decimal(amount) * Decimal("0.15")).quantize(Decimal("0.01"))
        self.rebates.append((contract.pk, source_ref, Decimal(amount), user.pk))
        return SimpleNamespace(document_ref=f"TEST-CN-{contract.pk}-{len(self.rebates)}", net_amount=Decimal(amount),
                               vat_amount=vat, status="issued")

    def rebate_state(self, contract, *, source_ref):
        mine = [r for r in self.rebates if r[1] == source_ref]
        if not mine:
            return None
        return SimpleNamespace(document_ref=f"TEST-CN-{contract.pk}-{len(self.rebates)}", net_amount=mine[-1][2],
                               vat_amount=Decimal("0"), status=self.rebate_status)

    def unearned_bai_ajel_profit(self, contract):
        """Review 4 (item 5): the profit not yet recognised on the contract. The fake books nothing, so unless
        ``unearned`` is set it is the fixed profit's share of what is outstanding after exit (profit x balance /
        receivable, rounded)."""
        if self.unearned is not None:
            return Decimal(self.unearned)
        from .models import DeferredSaleInstallment

        rows = DeferredSaleInstallment.objects.filter(contract=contract, status="outstanding_after_exit")
        balance = sum((i.amount for i in rows), Decimal("0"))
        return (contract.profit_amount * balance / contract.receivable).quantize(Decimal("0.01"))

    def reverse_bai_ajel_profit(self, contract, *, source_ref, user, on=None, reason=""):
        self.reversed.append((contract.pk, source_ref, user.pk))
        return SimpleNamespace(entry_ref="", profit_amount=Decimal("0"), collected_to_date=Decimal("0"), reversed=True)


@contextmanager
def fake_invoicing():
    port = FakeInvoicingPort()
    prev, invoicing_port._override = invoicing_port._override, port
    try:
        yield port
    finally:
        invoicing_port._override = prev
