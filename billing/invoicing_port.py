"""The hand-off from billing to invoicing (billing.md R13, R43, R46c, R52 to R56).

Billing never writes ``SalesDocument`` or journal rows itself. It calls three invoicing services, always with the
billing system user (``core.access.system_user("billing")``):

- ``create_from_billing(period, user)``: create (and, per ``InvoicingSettings.auto_issue_from_approved_contract``,
  issue) the invoice of one ``BillingPeriod`` from its frozen ``invoice_lines`` (R52, R53). All-or-nothing: on a
  refusal it raises an ``InvoicingError`` with a distinct ``code`` (R54) and creates nothing. Idempotent: a second
  call for the same period returns its existing non-rejected document.
- ``create_adjustment_from_billing(adjustment, user)``: create the **draft** credit or debit note of one
  ``BillingAdjustment`` (R43, invoicing R73). Never issues. Idempotent.
- ``discard_adjustment_draft(document, user)``: delete a superseded billing draft (a submitted one is first
  returned to draft), drafts only (R46c; name proposed by billing.md, not yet in invoicing).

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
