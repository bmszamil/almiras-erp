"""The hand-off from billing to invoicing (billing.md R13, R43, R46c, R52 to R56).

Billing never writes ``SalesDocument`` or journal rows itself. It calls three invoicing services, always with the
billing system user (``core.access.system_user("billing")``):

- ``create_from_billing(period, user)``: create (and, per ``InvoicingSettings.auto_issue_from_approved_contract``,
  issue) the invoice of one ``BillingPeriod`` from its frozen ``invoice_lines`` (R52, R53).
- ``create_adjustment_from_billing(adjustment, user)``: create the **draft** credit or debit note of one
  ``BillingAdjustment`` (R43, invoicing R73).
- ``discard_adjustment_draft(document, user)``: delete a superseded or discarded billing draft (R46c, R48).

The contract the real hooks must satisfy (review finding 5; checked by ``billing/test_invoicing_contract.py``,
skipped until the hooks exist; the fake in ``billing.testing`` follows it where it can):

1. **Who.** Called only by the billing system user (``invoicing.create_from_billing`` /
   ``invoicing.create_adjustment_from_billing``, Sys:billing); anyone else gets ``AccessDenied``. Documents are
   created by that user with ``source="system:billing"``.
2. **Never approve as the CFO.** An auto-issued invoice is issued by the system user under the contract approval
   (D8, R35): ``approved_by`` records ``period.billed_version.approved_by`` (the human CFO who approved the terms,
   R53), but the hook never acts as, logs in as or uses the approval grant of that CFO (no
   ``approve_sales_document(user=<CFO>)``). Posting on clearance must then accept the CFO's used
   ``billing.approve_contract`` / ``billing.approve_version`` grant for ``billed_version`` (R53; invoicing, ledger
   and access change, OQ-22 BLOCKING for go-live). *The fake does approve as the CFO, so tests marked
   ``fake_cfo_approval`` hold only with the fake until R53 is built.*
3. **Links.** ``SalesDocument.billing_item`` (FK to the ``BillingPeriod``) and ``SalesDocument.billing_adjustment``
   (FK to the ``BillingAdjustment``) are set on every document a hook creates.
4. **One document per period, in the database.** At most one non-``rejected`` document per ``billing_item``
   (a partial unique constraint), so a second call returns the existing document (idempotent) and a race cannot
   create two. A ZATCA-rejected document frees the period for a new one (R12, AT-41). Same for adjustments: a
   second ``create_adjustment_from_billing`` returns the existing non-terminal note.
5. **All-or-nothing, refusals by code.** On a refusal the hook raises (``InvoicingError`` or ``AccessDenied``),
   creates nothing and uses no number. Billing maps the outcome by ``exc.code`` only, never by message text
   (R54); invoicing must therefore give each refusal billing distinguishes its own code:
   ``rate_required`` (no exchange rate, R32), ``third_decimal_pending`` (OQ-62: an amount with a third decimal
   other than 0; billing holds the period), ``entity_not_vat_registered`` (invoicing R94; held), an open-period
   code such as ``period_closed`` (invoicing R28; failed, retried), ``permission_denied`` / ``AccessDenied``
   (failed, retried). Any other code is a failure retried next run. Messages stay "English / العربية".
6. **Inputs.** Invoicing reads only: ``period.entity``, ``contract.customer``, ``contract.currency``,
   ``period_start`` / ``period_end`` (supply period), ``period.billed_version`` (its approver and its
   ``due_days``: the due days of an invoice are ``billed_version.due_days``, not ``contract.due_days``, which keeps
   the version-1 value; deviation from R52 reported to the spec-writer), ``invoice_lines`` (one line each:
   quantity 1, unit price = ``net``; secondment lines carry ``secondment`` = the ``SecondmentAssignment`` id, not
   ``employee_ref``) and ``contract.requires_purification``. Notes read the adjustment's ``lines`` (``delta``),
   ``supply_start`` / ``supply_end``, ``original_document`` and the version's ``reason_ar`` / ``reason_en``;
   ``adjustment_event_date`` stays empty (D12).
7. **Read-only lines.** On a document with ``billing_adjustment`` (and on billing invoices) invoicing refuses any
   line change (add, edit, delete) for every user; reviewers complete header fields only (R56, OQ-26).
8. **Auto-issue switch.** ``InvoicingSettings.auto_issue_from_approved_contract`` True: the invoice is ``issued``
   in the same call (number used, ZATCA hand-off after commit). False: it is ``submitted`` and waits for the CFO
   (invoicing R60); billing marks the period ``generated`` either way. Correction notes are never issued by a hook,
   whatever the switch (R44).
9. **Discard.** ``discard_adjustment_draft`` deletes only billing drafts (a ``submitted`` one is first returned to
   draft in the same transaction, invoicing R96); ``issued``, ``final`` and ``rejected`` notes are refused and never
   touched.

The two ``create_*`` functions exist in ``invoicing.services`` as stubs that raise ``NotImplementedError``;
``discard_adjustment_draft`` does not exist yet. Implementing them is the invoicing-hooks step. Until then
billing is built and tested against ``billing.testing.FakeInvoicing``, installed through ``_override``.
The port is chosen by the setting ``BILLING_INVOICING_PORT`` (dotted path); the default is
:class:`InvoicingServicesPort`.
"""
from django.conf import settings
from django.utils.module_loading import import_string

DEFAULT_PORT = "billing.invoicing_port.InvoicingServicesPort"


class InvoicingServicesPort:
    """Calls the invoicing services named in billing.md (stubs today; see the module docstring)."""

    def create_from_billing(self, period, user):
        from invoicing import services

        return services.create_from_billing(period, user=user)

    def create_adjustment_from_billing(self, adjustment, user):
        from invoicing import services

        return services.create_adjustment_from_billing(adjustment, user=user)

    def discard_adjustment_draft(self, document, user):
        from invoicing import services

        discard = getattr(services, "discard_adjustment_draft", None)
        if discard is None:
            raise NotImplementedError(
                "invoicing.services.discard_adjustment_draft is not built yet (billing R46c). / "
                "خدمة حذف مسودات تصحيح الفوترة غير مبنية بعد في وحدة الفوترة."
            )
        return discard(document, user=user)


_override = None  # tests only: billing.testing.fake_invoicing installs itself here


def get_port():
    if _override is not None:
        return _override
    return import_string(getattr(settings, "BILLING_INVOICING_PORT", DEFAULT_PORT))()
