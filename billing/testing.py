"""Test helpers for billing: a fake of the two invoicing hooks (billing.md R13, R43, R46c). Never used outside tests.

``FakeInvoicing`` stands in for ``invoicing.services.create_from_billing``, ``create_adjustment_from_billing`` and the
proposed ``discard_adjustment_draft`` until invoicing implements them (the invoicing-hooks step). It checks the
caller's right the way invoicing will (``invoicing.create_from_billing`` / ``invoicing.create_adjustment_from_billing``,
Sys:billing) and then builds real sales documents with the **public** invoicing services, so numbering, VAT,
validation, ZATCA hand-off and posting on clearance are the real ones:

- the draft is created and submitted by a stand-in human clerk of the entity (as built, invoicing has no
  system-created draft yet; the real hook will record ``source="system:billing"`` and the billing system user);
- with ``auto_issue_from_approved_contract`` the invoice is approved by the CFO who approved ``billed_version``
  (R53), on the invoicing clock (the run date in tests), so its clearance posting is accepted by the ledger as
  built; otherwise it stays ``submitted`` for the CFO (R35, invoicing R60);
- correction notes are left as **drafts** (R44), with their lines from the adjustment.

All-or-nothing like the real hook (R52): a refusal raises an ``InvoicingError`` with a code (R54) and leaves
nothing behind. ``fail(code)`` makes the next calls raise that code; the third-decimal refusal is reported with
code ``third_decimal_pending`` (the code billing.md R54 asks invoicing to add).
"""
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from itertools import count

from django.db import transaction

from core import access
from core.access.testing import make_user, member
from invoicing import services as inv
from invoicing.models import InvoicingError, InvoicingSettings, SalesDocument

from . import invoicing_port

_seq = count(1)


class FakeInvoicing:
    def __init__(self):
        self.invoice_calls = []
        self.adjustment_calls = []
        self.discarded = []
        self.failures = []  # codes raised by the next create_from_billing calls, in order
        self.fail_periods = {}  # period pk -> code, raised every time for that period
        self.by_period = {}
        self.by_adjustment = {}
        self._clerks = {}

    # --- test controls ---------------------------------------------------------------------------
    def fail(self, code, times=1):
        self.failures += [code] * times

    def clerk(self, entity):
        if entity.pk not in self._clerks:
            user = make_user(f"fake.invoicing.{entity.code.lower()}.{next(_seq)}")
            member(user, entity, "receivables_clerk")
            self._clerks[entity.pk] = user
        return self._clerks[entity.pk]

    # --- the hooks -------------------------------------------------------------------------------
    def create_from_billing(self, period, user):
        access.require(user, period.entity, "invoicing.create_from_billing")
        self.invoice_calls.append(period.pk)
        existing = self.by_period.get(period.pk)
        if existing is not None:
            doc = SalesDocument.objects.get(pk=existing)
            if doc.status != SalesDocument.Status.REJECTED:
                return doc  # idempotent (R52)
        code = self.fail_periods.get(period.pk) or (self.failures.pop(0) if self.failures else None)
        if code:
            raise InvoicingError(f"Fake invoicing refused ({code}). / رفض تجريبي من الفوترة ({code}).", code=code)
        contract = period.contract
        clerk = self.clerk(period.entity)
        issue_date = inv._riyadh_now().date()
        due_days = period.billed_version.due_days  # the version's terms, not the (frozen) contract header
        due = {"due_date": issue_date + timedelta(days=due_days)} if due_days is not None else {}
        with transaction.atomic():
            doc = inv.create_sales_document(
                period.entity, user=clerk, customer=contract.customer, issue_date=issue_date,
                currency=contract.currency_id, supply_date=period.period_start, supply_end_date=period.period_end,
                **due,
            )
            for row in period.invoice_lines:
                inv.add_sales_line(
                    doc, user=clerk, income_type=row["income_type"], quantity="1", unit_price=row["net"],
                    description_ar=row["description_ar"], description_en=row["description_en"],
                    vat_category=row["vat_category"], vat_exemption_reason=row["vat_exemption_reason"] or None,
                    income_account=row["income_account"], unit_code=row["unit_code"],
                )
            doc = inv.submit_sales_document(doc, user=clerk)
            settings, _ = InvoicingSettings.objects.get_or_create(entity=period.entity)
            warnings = []
            if settings.auto_issue_from_approved_contract:
                try:
                    doc = inv.approve_sales_document(doc, user=period.billed_version.approved_by)
                except InvoicingError as exc:
                    if any("OQ-62" in m or "third decimal" in m for m in exc.messages):
                        raise InvoicingError(exc.messages, code="third_decimal_pending") from None
                    raise
                warnings = list(getattr(doc, "warnings", []))
        self.by_period[period.pk] = doc.pk
        doc.warnings = warnings
        return doc

    def create_adjustment_from_billing(self, adjustment, user):
        access.require(user, adjustment.entity, "invoicing.create_adjustment_from_billing")
        self.adjustment_calls.append(adjustment.pk)
        existing = self.by_adjustment.get(adjustment.pk)
        if existing is not None and SalesDocument.objects.filter(pk=existing).exists():
            return SalesDocument.objects.get(pk=existing)
        original = adjustment.original_document
        clerk = self.clerk(adjustment.entity)
        doc_type = (SalesDocument.DocType.CREDIT_NOTE if adjustment.kind == "credit"
                    else SalesDocument.DocType.DEBIT_NOTE)
        with transaction.atomic():
            doc = inv.create_sales_document(
                adjustment.entity, user=clerk, customer=original.customer, issue_date=inv._riyadh_now().date(),
                doc_type=doc_type, original_document=original, supply_date=adjustment.supply_start,
                supply_end_date=adjustment.supply_end, reason_ar=adjustment.version.reason_ar,
                reason_en=adjustment.version.reason_en,
            )
            for row in adjustment.lines:
                inv.add_sales_line(
                    doc, user=clerk, income_type=row["income_type"], quantity="1",
                    unit_price=str(abs(Decimal(row["delta"]))), description_ar=row["description_ar"],
                    description_en=row.get("description_en", ""), vat_category=row["vat_category"],
                    vat_exemption_reason=row.get("vat_exemption_reason") or None,
                    income_account=row["income_account"], unit_code=row.get("unit_code") or "C62",
                )
        self.by_adjustment[adjustment.pk] = doc.pk
        return doc

    def discard_adjustment_draft(self, document, user):
        access.require(user, document.entity, "invoicing.create_adjustment_from_billing")
        doc = SalesDocument.objects.get(pk=document.pk)
        clerk = self.clerk(doc.entity)
        if doc.status == SalesDocument.Status.SUBMITTED:
            doc = inv.return_to_draft(doc, user=clerk)
        inv.delete_sales_draft(doc, user=clerk)
        self.discarded.append(document.pk)


@contextmanager
def fake_invoicing():
    fake = FakeInvoicing()
    previous, invoicing_port._override = invoicing_port._override, fake
    try:
        yield fake
    finally:
        invoicing_port._override = previous
