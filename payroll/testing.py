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
