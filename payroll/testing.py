"""Test helpers for payroll: a fake invoicing port for the bai' ajel sale. Never used outside tests."""
from contextlib import contextmanager
from decimal import Decimal

from . import invoicing_port


class FakeInvoicingPort:
    """Stands in for invoicing R112: VAT 15% on the sale price, charged to the employee (P20(h))."""

    def __init__(self):
        self.calls = []

    def create_bai_ajel_sale(self, contract, *, user):
        vat = (contract.sale_price * Decimal("0.15")).quantize(Decimal("0.01"))
        self.calls.append((contract.pk, user.pk, vat))
        return invoicing_port.SaleResult(document_ref=f"TEST-SI-{contract.pk}", vat_amount=vat)


@contextmanager
def fake_invoicing():
    port = FakeInvoicingPort()
    prev, invoicing_port._override = invoicing_port._override, port
    try:
        yield port
    finally:
        invoicing_port._override = prev
