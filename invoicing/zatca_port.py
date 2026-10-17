"""The hand-off between invoicing and the (not yet built) ``zatca`` module (invoicing R40 to R46; zatca.md §5, §6).

The zatca spec names ``zatca.services.submit_document(document, user)``,
``zatca.services.is_issued(document)`` and a status callback into invoicing
(OQ-2 / zatca OQ-12: the exact contract is still open). Until zatca exists,
invoicing talks to a port with that shape:

- ``submit_document(document, user)``: called by invoicing after the issue
  transaction commits (R40). Standard documents go to clearance, simplified
  ones to reporting. The port records the submission (``zatca_status``).
- ``is_issued(document)``: True once the document may be given to the buyer
  (standard: cleared; simplified: reported, as the default port does not
  stamp; zatca R-22, R-23).
- The result comes back through ``invoicing.services.on_zatca_result(document,
  result, user=<zatca queue system user>)``; on ``cleared`` / ``reported`` /
  ``*_with_warnings`` the approved document posts (R41), on ``rejected`` it
  becomes ``rejected`` and keeps its number (R20, R42).

``zatca_*`` fields are written only on this side of the hand-off: by the
port (submission) and by the result callback (R45). Invoicing never
computes a hash, ICV, PIH or QR.

The port is chosen by the setting ``INVOICING_ZATCA_PORT`` (dotted path);
the default, :class:`PendingZatcaPort`, sends nothing anywhere and leaves
documents ``pending``, so no sales document is ever posted without a ZATCA
result. Tests use ``invoicing.testing.FakeZatcaPort``.
"""
from dataclasses import dataclass, field
from typing import Protocol

from django.conf import settings
from django.utils.module_loading import import_string

CLEARED_STATUSES = frozenset({"cleared", "cleared_with_warnings"})
REPORTED_STATUSES = frozenset({"reported", "reported_with_warnings"})
POSTING_STATUSES = CLEARED_STATUSES | REPORTED_STATUSES
RESULT_STATUSES = POSTING_STATUSES | {"pending", "rejected"}
DEFAULT_PORT = "invoicing.zatca_port.PendingZatcaPort"


@dataclass(frozen=True)
class ZatcaResult:
    """What zatca reports back for one document (written to the ``zatca_*`` fields)."""

    status: str
    icv: int | None = None
    invoice_hash: str = ""
    previous_hash: str = ""
    qr: str = ""
    messages: tuple = field(default_factory=tuple)  # ({"code", "text_en", "text_ar"}, ...)


class ZatcaPort(Protocol):
    def submit_document(self, document, user) -> None: ...

    def is_issued(self, document) -> bool: ...


def record_submission(document):
    """Mark ``document`` as handed to ZATCA (``zatca_status = pending``). Port side of R45."""
    from django.db import transaction

    from .models import SalesDocument

    with transaction.atomic():
        doc = SalesDocument.objects.select_for_update().get(pk=document.pk)
        if doc.zatca_status == SalesDocument.ZatcaStatus.NOT_SUBMITTED:
            doc.zatca_status = SalesDocument.ZatcaStatus.PENDING
            doc._service_write = True
            doc.save(update_fields=["zatca_status", "updated_at"])
    return doc


def issued_by_status(document):
    if document.zatca_profile == "standard":
        return document.zatca_status in CLEARED_STATUSES
    return document.zatca_status in REPORTED_STATUSES


class PendingZatcaPort:
    """Default port while zatca is not built: records the submission and waits; never clears anything."""

    def submit_document(self, document, user):
        record_submission(document)

    def is_issued(self, document):
        return issued_by_status(document)


_override = None  # tests only: invoicing.testing.FakeZatcaPort installs itself here


def get_port():
    if _override is not None:
        return _override
    return import_string(getattr(settings, "INVOICING_ZATCA_PORT", DEFAULT_PORT))()
