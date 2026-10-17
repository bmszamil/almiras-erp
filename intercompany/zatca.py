"""Hand-off to the ZATCA module (separate spec; not built yet). R22, D2.

This app stops at a clean hand-off:
- On issue, an invoice or credit note gets `zatca_ready=True`, a `zatca_uuid`, its gap-free number, issue
  time, seller and buyer VAT numbers, invoice type code (388 / 381), and per line the VAT category
  (S / Z / E / O), rate, amount and exemption reason. Credit notes carry the original and a bilingual reason.
- Seller and buyer carry the structured national address; lines carry the VATEX exemption code; VAT
  totals are per category (`vat_breakdown`). PIH, ICV, QR, cryptographic stamp and debit notes (383)
  are the ZATCA module's job.
- `zatca_submit(invoice)` is the interface the ZATCA module will implement. It is NOT called from the
  issue path yet.
- `zatca_status` / `zatca_hash` are written only through `set_zatca_status`, for the ZATCA module.
"""
from django.db import transaction
from django.utils import timezone

from access.errors import RuleError

from .models import IntercompanyInvoice, workflow


def zatca_submit(invoice):
    """Generate, sign, QR and clear/report the invoice with ZATCA. Implemented by the ZATCA module."""
    raise NotImplementedError("ZATCA submission is part of the separate ZATCA module and is not built yet.")


def handoff_payload(invoice):
    """The data the ZATCA module needs, as plain values (Decimal amounts kept as strings)."""
    if not invoice.zatca_ready:
        raise RuleError("not_ready", "Only issued invoices are handed to ZATCA.", "تُسلَّم الفواتير الصادرة فقط لزاتكا.")
    return {
        "uuid": str(invoice.zatca_uuid),
        "number": invoice.number,
        "invoice_type_code": invoice.invoice_type_code,
        "issue_date": invoice.issue_date.isoformat(),
        "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        "supply_date_from": invoice.supply_date_from.isoformat(),
        "supply_date_to": invoice.supply_date_to.isoformat(),
        "due_date": invoice.due_date.isoformat(),
        "currency": invoice.currency,
        "seller": {"code": invoice.entity.code, "name_en": invoice.entity.name_en, "name_ar": invoice.entity.name_ar,
                   "vat_number": invoice.seller_vat_number, "cr_number": invoice.entity.cr_number,
                   "address": invoice.entity.address()},
        "buyer": {"code": invoice.counterparty.code, "name_en": invoice.counterparty.name_en,
                  "name_ar": invoice.counterparty.name_ar, "vat_number": invoice.buyer_vat_number,
                  "cr_number": invoice.counterparty.cr_number, "address": invoice.counterparty.address()},
        "trigger_date": invoice.trigger_date.isoformat() if invoice.trigger_date else None,
        "vat_breakdown": _breakdown(invoice),
        "billing_reference": invoice.credit_of.number if invoice.credit_of_id else None,
        "credit_reason": {"en": invoice.credit_reason_en, "ar": invoice.credit_reason_ar} if invoice.credit_of_id else None,
        "net_total": str(invoice.net_total),
        "vat_total": str(invoice.vat_total),
        "gross_total": str(invoice.gross_total),
        "lines": [
            {"description": {"en": l.description_en, "ar": l.description_ar}, "quantity": str(l.quantity),
             "unit_price": str(l.unit_price), "net_amount": str(l.net_amount), "vat_category": l.vat_category,
             "vat_rate": str(l.vat_rate), "vat_amount": str(l.vat_amount),
             "exemption_code": l.vat_exemption_code,
             "exemption_reason": {"en": l.vat_exemption_reason_en, "ar": l.vat_exemption_reason_ar}}
            for l in invoice.lines.order_by("id")
        ],
    }


def _breakdown(invoice):
    """VAT per category and rate: taxable amount and VAT rounded half-up on the category total."""
    from .services import HUNDRED, money

    groups = {}
    for l in invoice.lines.order_by("id"):
        g = groups.setdefault((l.vat_category, l.vat_rate), {"net": 0, "code": l.vat_exemption_code})
        g["net"] += l.net_amount
    return [{"category": cat, "rate": str(rate), "taxable_amount": str(money(g["net"])),
             "vat_amount": str(money(g["net"] * rate / HUNDRED)), "exemption_code": g["code"]}
            for (cat, rate), g in groups.items()]


@transaction.atomic
def set_zatca_status(invoice, status, zatca_hash=""):
    """For the ZATCA module only (R22)."""
    inv = IntercompanyInvoice.objects.select_for_update(of=("self",)).get(pk=invoice.pk)
    if status not in IntercompanyInvoice.ZatcaStatus.values:
        raise RuleError("invalid_status", "Unknown ZATCA status.", "حالة زاتكا غير معروفة.")
    inv.zatca_status, inv.zatca_hash, inv.zatca_updated_at = status, zatca_hash or inv.zatca_hash, timezone.now()
    with workflow(inv, flag="_zatca"):
        inv.save()
    return inv
