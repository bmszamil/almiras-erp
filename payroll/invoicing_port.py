"""The hand-off from payroll to invoicing for the bai' ajel sale invoice (payroll R47 step 4, P19(k); invoicing
R112, E28): invoicing creates a simplified sales invoice to the employee at 15% VAT on the sale price (cost + fixed
profit; the VAT is charged to and paid by the employee, P20(h)), reports it to ZATCA and posts Dr 1240 / Cr 1250 /
Cr 1241 / Cr 2200.

Invoicing R112 is not built yet, so the default :class:`NotBuiltInvoicingPort` refuses (en/ar) and no bai' ajel sale
happens. Setting ``PAYROLL_INVOICING_PORT`` picks another port; tests use ``payroll.testing.FakeInvoicingPort``.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.utils.module_loading import import_string

from .models import error

DEFAULT_PORT = "payroll.invoicing_port.NotBuiltInvoicingPort"


@dataclass(frozen=True)
class SaleResult:
    document_ref: str
    vat_amount: Decimal


class NotBuiltInvoicingPort:
    def create_bai_ajel_sale(self, contract, *, user):
        raise error("The bai' ajel sale invoice is created by invoicing (R112), which is not built yet.",
                    "تُنشأ فاتورة البيع الآجل من خلال الفوترة (R112) التي لم تُبنَ بعد.", code="invoicing_not_built")


_override = None


def get_port():
    if _override is not None:
        return _override
    return import_string(getattr(settings, "PAYROLL_INVOICING_PORT", DEFAULT_PORT))()
