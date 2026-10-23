"""Invoicing's read-only side for the fixed-asset register (fixed-assets.md R2, R4, R4a, R43, R46; §6).

The ``fixed_assets`` app pulls posted purchase-document lines from here; nothing here writes, posts or changes how
invoicing books a document (no hook into invoicing posting). Every function reads stored rows only; callers check
their own rights first (``fixed_assets.services`` requires ``fixed_assets.view`` or ``fixed_assets.create_asset``).

- :func:`posted_lines`: posted bill and vendor-credit-note lines of one entity on given accounts.
- :func:`booking_of`: what the posted document booked for one line, in SAR: the functional amount on the line's
  account (signed: a bill debit is positive, a vendor credit note's credit negative), the VAT the vendor charged on the
  line and the part recovered on 1310 (non-recoverable VAT is already in the line's cost, invoicing R29 / R104), the
  accounting and due dates.
- :func:`credit_notes_of`: posted vendor credit notes referencing bills (R4 pending adjustments).

Mirror bills (invoicing R113) are listed here once they exist: they are posted ``PurchaseDocument`` rows like any bill.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ledger import fx
from ledger.models import JournalEntry, JournalLine

from .models import PurchaseDocument, PurchaseDocumentLine

ZERO = Decimal("0")
CENT = Decimal("0.01")
POSTED = PurchaseDocument.Status.POSTED
REVERSED = PurchaseDocument.Status.REVERSED
BILL = PurchaseDocument.DocType.BILL
CREDIT_NOTE = PurchaseDocument.DocType.VENDOR_CREDIT_NOTE


@dataclass(frozen=True)
class Booking:
    """One posted purchase-document line as the ledger booked it (SAR, 2 dp)."""

    line_id: int
    document_id: int
    document_number: str
    doc_type: str
    status: str
    account_id: int
    amount: Decimal  # functional debit on the line's account; negative for a vendor credit note
    vat_charged: Decimal  # VAT the vendor charged on the line, SAR
    vat_recovered: Decimal  # the part booked on 1310, SAR
    accounting_date: date
    due_date: date | None
    original_document_id: int | None
    currency: str

    @property
    def payment_days(self):
        """Due date minus accounting date (R4a); None without a due date."""
        return None if self.due_date is None else (self.due_date - self.accounting_date).days


def posted_lines(entity, account_ids, *, include_reversed=False, date_from=None, date_to=None):
    """Lines of posted bills and vendor credit notes of ``entity`` whose account is in ``account_ids``."""
    statuses = (POSTED, REVERSED) if include_reversed else (POSTED,)
    qs = (PurchaseDocumentLine.objects.filter(entity=entity, account_id__in=list(account_ids),
                                              document__status__in=statuses, document__entity=entity)
          .select_related("document", "account", "vat_rate").order_by("document__accounting_date", "document_id",
                                                                       "line_no"))
    if date_from is not None:
        qs = qs.filter(document__accounting_date__gte=date_from)
    if date_to is not None:
        qs = qs.filter(document__accounting_date__lte=date_to)
    return qs


def booked_on(document):
    """The date ``document`` was booked in the GL (its accounting date), and the date of its reversal entry once it
    is reversed (None before): what a register cost adjustment is dated on (fixed-assets R4)."""
    reversed_on = None
    if document.status == REVERSED and document.reversal_entry_id is not None:
        reversed_on = JournalEntry.objects.filter(pk=document.reversal_entry_id).values_list("date", flat=True).first()
    return document.accounting_date, reversed_on


def line_of(entity, pk, *, lock=False):
    """One purchase-document line of ``entity`` (any status), or None. With ``lock`` the line row is locked FOR UPDATE
    (inside the caller's transaction) so two register links of one line run one after the other (fixed-assets R4); a
    row lock is a read here: nothing is written."""
    qs = PurchaseDocumentLine.objects.select_related("document", "account").filter(entity=entity, pk=pk)
    if lock:
        qs = qs.select_for_update(of=("self",))
    return qs.first()


def _sar(amount, document):
    if document.currency_id == document.entity.base_currency_id:
        return Decimal(amount).quantize(CENT)
    return fx.convert(Decimal(amount), document.fx_rate)


def booking_of(line):
    """The :class:`Booking` of a line of a posted (or since reversed) document; None for a draft or submitted one.

    The functional amount is read from the document's own journal entry: its cost lines are written first, one per
    document line with a cost, in line order (``invoicing.services._purchase_document_lines``), so the n-th of them
    is the n-th costed line. Its account is checked against the line's.
    """
    doc = line.document
    if doc.status not in (POSTED, REVERSED) or doc.journal_entry_id is None:
        return None
    lines = list(doc.lines.select_related("account", "vat_rate").order_by("line_no"))
    from . import services as invoicing

    _, _, _, _, costs, _, recoverable_lines = invoicing._purchase_amounts(doc, lines)
    costed = [l.pk for l in lines if costs[l.line_no]]
    entry_lines = list(JournalLine.objects.filter(entry_id=doc.journal_entry_id).order_by("pk")[:len(costed)])
    amount = ZERO
    if line.pk in costed:
        booked = entry_lines[costed.index(line.pk)]
        if booked.account_id != line.account_id:  # never guessed: the entry is not laid out as expected
            raise ValueError(f"Entry {doc.journal_entry_id} does not book line {line.line_no} of {doc.number} first. / "
                             f"القيد {doc.journal_entry_id} لا يبدأ ببند {line.line_no} من {doc.number}.")
        amount = booked.debit - booked.credit
    vat = _sar(line.vat_amount, doc)
    recovered = vat if line.line_no in recoverable_lines else ZERO
    if doc.doc_type == CREDIT_NOTE:
        vat, recovered = -vat, -recovered
    return Booking(line.pk, doc.pk, doc.number, doc.doc_type, doc.status, line.account_id, amount, vat, recovered,
                   doc.accounting_date, doc.due_date, doc.original_document_id, doc.currency_id)


def credit_notes_of(bill_ids):
    """Posted vendor credit notes referencing the bills ``bill_ids`` (R4)."""
    return PurchaseDocument.objects.filter(original_document_id__in=list(bill_ids), doc_type=CREDIT_NOTE,
                                           status=POSTED)


def document_label(line):
    """``number`` line ``n`` of a purchase-document line, for messages (no personal data)."""
    return f"{line.document.number or line.document.vendor_document_number} line {line.line_no}"
