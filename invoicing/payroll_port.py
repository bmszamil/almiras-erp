"""The real payroll invoicing port (``PAYROLL_INVOICING_PORT``; payroll R47 step 4, payroll ``invoicing_port``;
invoicing R112, E28). It replaces payroll's refusing default ``NotBuiltInvoicingPort``.

Interface (every method runs inside the caller's transaction; refusals raise ``InvoicingError``, a
``ValidationError`` with an English / Arabic message and a ``code``):

- ``create_bai_ajel_sale(contract, *, user) -> payroll.invoicing_port.SaleResult(document_ref, vat_amount)``:
  payroll's sale approval (``user`` = the approver of the sale). Creates and issues the simplified sale invoice to the
  employee (VAT 15% on the sale price, charged to the employee) and hands it to ZATCA after commit; it posts on
  reporting (Dr 1240 / Cr 1250 / Cr 1241 / Cr 2200). Refusal codes: ``purchase_bill_not_posted``, ``cost_mismatch``,
  ``possession_not_recorded``, ``sale_before_possession``, ``asset_already_sold``, ``entity_not_vat_registered``,
  ``currency``, ``document_invalid`` (e.g. closed period). Idempotent per contract.
- ``sale_state(contract) -> SaleState | None``: the live sale invoice of the contract (number, status, posted,
  VAT, profit recognised so far); payroll marks the contract sold once ``posted`` (payroll R47 as amended).
- ``recognise_bai_ajel_profit(contract, *, amount, on, source_ref, user) -> ProfitResult``: an instalment (or a
  final-settlement recovery) of ``amount`` was collected by the payroll approval of ``user`` on ``on``; posts the
  profit share Dr 1241 / Cr 4610. Idempotent per ``source_ref``. Codes: ``sale_not_posted``, ``over_collected``,
  ``collection_changed``, ``no_deferred_sale``.
- ``reverse_bai_ajel_profit(contract, *, source_ref, user, on=None, reason="") -> ProfitResult``: a reversed run gives
  the collection back; ``user`` holds ``payroll.reverse_run``. Code: ``nothing_to_reverse``.

The contract is read only through these attributes: ``pk``, ``entity_id``, ``employee``, ``purchase_bill``, ``cost``,
``profit_amount``, ``possession_date``, ``possession_evidence_ref``, ``asset_description_ar`` /
``asset_description_en``, ``sale_date``, ``term_months``. Invoicing never writes payroll data.
"""
from dataclasses import dataclass
from decimal import Decimal

from . import services


@dataclass(frozen=True)
class SaleState:
    document_ref: str
    status: str
    posted: bool
    vat_amount: Decimal
    receivable: Decimal
    profit_recognised: Decimal


@dataclass(frozen=True)
class ProfitResult:
    entry_ref: str  # the journal entry number, "" when no profit was due on that collection
    profit_amount: Decimal
    collected_to_date: Decimal
    reversed: bool = False


def _result(release):
    entry = release.reversal_entry if release.status == "reversed" else release.journal_entry
    return ProfitResult(entry_ref=entry.number if entry is not None else "", profit_amount=release.profit_amount,
                        collected_to_date=release.collected_to_date, reversed=release.status == "reversed")


class PayrollInvoicingPort:
    def create_bai_ajel_sale(self, contract, *, user):
        from payroll.invoicing_port import SaleResult

        doc = services.create_deferred_sale(
            contract, approver=user, employee=contract.employee, purchase_bill=contract.purchase_bill,
            cost=contract.cost, profit_amount=contract.profit_amount, possession_date=contract.possession_date,
            possession_evidence_ref=contract.possession_evidence_ref,
            asset_description_ar=contract.asset_description_ar,
            asset_description_en=getattr(contract, "asset_description_en", ""),
            sale_date=getattr(contract, "sale_date", None), term_months=getattr(contract, "term_months", None),
        )
        return SaleResult(document_ref=doc.number, vat_amount=doc.deferred_sale.vat_amount)

    def sale_state(self, contract):
        sale = services.deferred_sale_of(contract)
        if sale is None:
            return None
        doc = sale.sales_document
        recognised = sum((r.profit_amount for r in sale.releases.filter(status="posted")), Decimal("0.00"))
        return SaleState(document_ref=doc.number, status=doc.status,
                         posted=doc.status == "final" and doc.journal_entry_id is not None,
                         vat_amount=sale.vat_amount, receivable=sale.receivable, profit_recognised=recognised)

    def recognise_bai_ajel_profit(self, contract, *, amount, on, source_ref, user):
        return _result(services.recognise_deferred_profit(contract, user=user, collected_amount=amount,
                                                          collected_on=on, source_ref=source_ref))

    def reverse_bai_ajel_profit(self, contract, *, source_ref, user, on=None, reason=""):
        return _result(services.reverse_deferred_profit(contract, user=user, source_ref=source_ref, date=on,
                                                        reason=reason))
