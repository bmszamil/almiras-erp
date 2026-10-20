"""Registry of sub-ledger document types that may book journal entries (access.md R23, R30, R34 to R36).

``create_entry(post=True, grant=..., document=...)`` posts an entry for one
sub-ledger document (a bill, a receipt, a sales document) with a grant for a
sub-ledger **P** permission. The ledger accepts such a posting only for a
document that

- is a saved model instance (never a tuple or another stand-in),
- is of a type registered here by the module that owns it, with the
  permissions allowed to post it,
- exists in the database, belongs to the entity being posted, and has a
  recorded creator (so segregation can be checked on it), and
- passes the type's own line hooks (every type registers at least one).

A sub-ledger app registers its document models in its own
``AppConfig.ready()``; only the app that defines a model may register it,
and the registry is frozen once all apps are ready::

    from ledger import documents
    documents.register(Bill, post_permissions={"invoicing.approve_bill"},
                       reverse_permissions={"invoicing.reverse_document"},
                       build_lines=bill_lines)

Line hooks (required: a type registers ``build_lines``, ``validate_lines`` or both,
so no document can book lines that nothing ties to it):

- ``build_lines(document)`` returns the line dicts (as for ``create_entry``)
  that book the document. The ledger then builds the entry from them and the
  caller passes ``lines=None``.
- ``validate_lines(document, lines)`` receives the stored document and the
  unsaved, fully priced ``JournalLine`` objects (rounding line included)
  before anything is written, and returns a list of problems ("English /
  العربية"); any problem refuses the posting.

Optional segregation exception (access R46, A16):

- ``creator_may_post(document)`` returns True when the stored document may be
  posted by its own creator, submitter or last modifier (customer receipts
  with ``kind = receipt`` only). Accepted only for the types listed in
  ``CREATOR_MAY_POST_TYPES``; every other type keeps creator != poster (R35).
  The poster still needs a posting grant issued for that document.

Optional contract-approval hook (billing.md R53; invoicing R60):

- ``contract_approval(document)`` returns a :class:`RecordedApproval` (or None) for a stored document that was
  issued without a per-document approval because a person approved the contract terms it was generated from
  (an invoice auto-issued from billing, or a bai' ajel sale invoice approved in payroll, invoicing R112). Posting on clearance then accepts that person as ``posted_by`` only
  if the access log holds their granted approval (``permission``) of that very record (``target``), given no
  later than the recorded approval time. Accepted only for the types in ``CONTRACT_APPROVAL_TYPES`` and the
  permissions in ``CONTRACT_APPROVAL_PERMISSIONS``. Nobody approves the document in that person's name: the
  hook only names the approval that already exists.

Optional reversal hook (tax-returns review item 5):

- ``reverse_guard(document)`` returns True only while the document's own module is reversing it (its service
  sets a marker around its ``reverse_entry`` call). ``reverse_entry`` refuses the reversal otherwise
  (``reverse_through_owner``), so a reversal grant cannot undo the entry behind the module's back and leave the
  document's own status (paid, settled) out of step with the ledger.

An entry of a **registered** document type is never reversed with a ``ledger.reverse_entry`` grant: only with a
reversal permission registered for that type (``document_reversal_only``). Types registered with no reversal
permission (sales documents, the VAT return offset T1) are therefore never reversed.

Optional SAMA tax-point hook (owner decision H2, ledger-fx R51):

- ``sama_tax_point(document)`` returns the stored document's tax point (a date), or ``(tax_point, as_of)`` with the
  aware datetime at which the document chose its SAMA row, when its foreign-currency lines book at the SAMA rate
  on that tax point (line spec ``rate_source="sama_tax_point"`` with the SAMA ``exchange_rate`` row, from its own
  ``build_lines``), else None. The ledger then accepts that row even when it is older than
  ``FX_MAX_RATE_AGE_DAYS`` before the entry date, but only if it is a global SAMA spot row from the line currency
  to SAR (the functional currency), dated on the tax point or before it and the latest such row (owner decision
  I2; with ``as_of``, among the rows recorded by then), and the tax point is not after the entry date. Accepted
  only for the types in ``SAMA_TAX_POINT_TYPES``.

Optional period hook:

- ``pending_documents(entity, start_date, end_date)`` returns descriptions of
  documents of that entity dated in the range that are approved but wait to
  be booked (e.g. sales documents awaiting ZATCA clearance). ``close_period``
  refuses to close a period while any exist (invoicing R43).

Until a module registers a type, ``create_entry(post=True)`` with a sub-ledger
grant is refused. Nothing is registered by the ledger itself.
"""
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

from django.apps import apps
from django.conf import settings
from django.db import models

from core.access import catalogue


class DocumentRegistryError(RuntimeError):
    """A programming error in a document registration."""


@dataclass(frozen=True)
class RecordedApproval:
    """The approval of the terms a document was generated from (billing R53): ``approver_id`` was granted
    ``permission`` for ``target`` (a stored record, e.g. the billed contract version) at or before ``approved_at``."""

    permission: str
    target: object
    approver_id: int
    approved_at: object


@dataclass(frozen=True)
class DocumentType:
    """One registered document model and what may post or reverse its entries."""

    label: str  # app_label.model, as stored in JournalEntry.document_type
    model: type
    post_permissions: frozenset
    reverse_permissions: frozenset
    entity_field: str = "entity"
    # The document's "last changed" timestamp; approvals issued before it are stale (R14).
    changed_at_field: str | None = "modified_at"
    # Non-null foreign key to the user who created the document (segregation, R35).
    creator_field: str = "created_by"
    build_lines: Callable | None = None
    validate_lines: Callable | None = None
    pending_documents: Callable | None = None
    # access R46: True for a stored document its creator / submitter / modifier may post (receipts only).
    creator_may_post: Callable | None = None
    # billing R53: the contract approval recorded for a document auto-issued from approved terms.
    contract_approval: Callable | None = None
    # tax-returns review item 5: True only while the owning module reverses the document itself.
    reverse_guard: Callable | None = None
    # H2 (ledger-fx R51): the tax point whose SAMA rate the document books at, or None.
    sama_tax_point: Callable | None = None

    def entity_id_of(self, document):
        return getattr(document, f"{self.entity_field}_id")

    def changed_at_of(self, document):
        return getattr(document, self.changed_at_field, None) if self.changed_at_field else None

    def creator_id_of(self, document):
        return getattr(document, f"{self.creator_field}_id", None)

    def self_post_allowed(self, document):
        """access R46: the people who made ``document`` may post it themselves (never for other types)."""
        return self.creator_may_post is not None and self.creator_may_post(document) is True


_REGISTRY = {}

# Sub-ledger P permissions that post a document's entry; reversal grants only reverse (R30).
POSTING_PERMISSIONS = catalogue.DOCUMENT_POST_PERMISSIONS
REVERSING_PERMISSIONS = catalogue.REVERSE_PERMISSIONS - {"ledger.reverse_entry"}
# access R46 / A16: the only document types whose creator may post them (customer receipts; the kind is
# decided by the type's own creator_may_post hook). Refunds, bills, payments and sales documents are not here.
CREATOR_MAY_POST_TYPES = frozenset({"invoicing.customerreceipt"})
# billing R53: the only type whose clearance posting may rely on a contract approval, and the approvals that count.
CONTRACT_APPROVAL_TYPES = frozenset({"invoicing.salesdocument"})
# invoicing R112 (payroll R47): a bai' ajel sale invoice is issued on the matrix approver's approval of the sale in
# payroll (payroll.approve_deferred_sale for the contract), not on a per-invoice approval.
CONTRACT_APPROVAL_PERMISSIONS = frozenset({"billing.approve_contract", "billing.approve_version",
                                           "payroll.approve_deferred_sale"})
# H2 (ledger-fx R51): the only type that may book at a SAMA tax-point rate older than the rate age limit.
SAMA_TAX_POINT_TYPES = frozenset({"invoicing.salesdocument"})


def _frozen():
    """True once every AppConfig.ready() has run: no registration after start-up."""
    return apps.ready


def _check_caller(model, module_name):
    """Only the app that defines ``model`` registers it (e.g. from its AppConfig.ready())."""
    try:
        caller = apps.get_containing_app_config(module_name or "")
    except Exception:  # noqa: BLE001 - registry not populated: treated as an unknown caller
        caller = None
    if caller is None or caller.label != model._meta.app_label:
        raise DocumentRegistryError(
            f"{model._meta.label_lower} is registered only by its own app ({model._meta.app_label}), "
            f"not from {module_name or 'unknown code'}."
        )


def register(model, *, post_permissions, reverse_permissions=(), entity_field="entity", changed_at_field="modified_at",
             creator_field="created_by", build_lines=None, validate_lines=None, pending_documents=None,
             creator_may_post=None, contract_approval=None, reverse_guard=None, sama_tax_point=None):
    """Register ``model`` as a document type that books journal entries. Idempotent for the same arguments.

    Call it from the ``AppConfig.ready()`` of the app that defines ``model``.
    """
    if _frozen():
        raise DocumentRegistryError(
            "The document registry is frozen once apps are ready; register in your AppConfig.ready()."
        )
    _check_caller(model, sys._getframe(1).f_globals.get("__name__", ""))
    return _register(
        model, post_permissions=post_permissions, reverse_permissions=reverse_permissions, entity_field=entity_field,
        changed_at_field=changed_at_field, creator_field=creator_field, build_lines=build_lines,
        validate_lines=validate_lines, pending_documents=pending_documents, creator_may_post=creator_may_post,
        contract_approval=contract_approval, reverse_guard=reverse_guard, sama_tax_point=sama_tax_point,
    )


def _field(model, name, label):
    try:
        return model._meta.get_field(name)
    except Exception as exc:
        raise DocumentRegistryError(f"{label} has no field {name!r}.") from exc


def _register(model, *, post_permissions, reverse_permissions=(), entity_field="entity", changed_at_field="modified_at",
              creator_field="created_by", build_lines=None, validate_lines=None, pending_documents=None,
              creator_may_post=None, contract_approval=None, reverse_guard=None, sama_tax_point=None):
    """Validate and store one registration (no caller or freeze check; tests use it through the helpers below)."""
    if not (isinstance(model, type) and issubclass(model, models.Model)) or model._meta.abstract:
        raise DocumentRegistryError(f"{model!r} is not a concrete Django model.")
    label = model._meta.label_lower
    if model._meta.app_label == "ledger":
        raise DocumentRegistryError("Ledger models are not sub-ledger documents.")
    post = frozenset(post_permissions)
    reverse = frozenset(reverse_permissions)
    if not post:
        raise DocumentRegistryError(f"{label}: name at least one permission that posts it.")
    if post - POSTING_PERMISSIONS:
        raise DocumentRegistryError(f"{label}: {sorted(post - POSTING_PERMISSIONS)} do not post sub-ledger documents.")
    if reverse - REVERSING_PERMISSIONS:
        raise DocumentRegistryError(f"{label}: {sorted(reverse - REVERSING_PERMISSIONS)} do not reverse documents.")
    field = _field(model, entity_field, label)
    if not (field.many_to_one and field.related_model._meta.label_lower == "core.entity"):
        raise DocumentRegistryError(f"{label}.{entity_field} is not a foreign key to core.Entity.")
    creator = _field(model, creator_field, label)
    if not (creator.many_to_one and creator.related_model._meta.label == settings.AUTH_USER_MODEL) or creator.null:
        # Without a stored creator the ledger cannot keep the creator from posting it (R35).
        raise DocumentRegistryError(f"{label}.{creator_field} must be a non-null foreign key to the user model.")
    if changed_at_field is not None:
        _field(model, changed_at_field, label)
    for name, hook in (("build_lines", build_lines), ("validate_lines", validate_lines),
                       ("pending_documents", pending_documents), ("creator_may_post", creator_may_post),
                       ("contract_approval", contract_approval), ("reverse_guard", reverse_guard),
                       ("sama_tax_point", sama_tax_point)):
        if hook is not None and not callable(hook):
            raise DocumentRegistryError(f"{label}: {name} must be callable.")
    if build_lines is None and validate_lines is None:
        # Without a hook any lines could be booked for the document (nothing ties them to it).
        raise DocumentRegistryError(f"{label}: register build_lines or validate_lines (or both).")
    if creator_may_post is not None and label not in CREATOR_MAY_POST_TYPES:
        # access R46: the exception is for customer receipts only; every other type keeps creator != poster (R35).
        raise DocumentRegistryError(f"{label}: creator_may_post is allowed only for {sorted(CREATOR_MAY_POST_TYPES)}.")
    if reverse_guard is not None and not reverse:
        raise DocumentRegistryError(f"{label}: reverse_guard needs a reversal permission.")
    if contract_approval is not None and label not in CONTRACT_APPROVAL_TYPES:
        # billing R53: only invoices auto-issued from an approved contract post on a contract approval.
        raise DocumentRegistryError(f"{label}: contract_approval is allowed only for {sorted(CONTRACT_APPROVAL_TYPES)}.")
    if sama_tax_point is not None and label not in SAMA_TAX_POINT_TYPES:
        # H2: only sales documents book at their SAMA tax-point rate beyond the rate age limit.
        raise DocumentRegistryError(f"{label}: sama_tax_point is allowed only for {sorted(SAMA_TAX_POINT_TYPES)}.")
    if sama_tax_point is not None and build_lines is None:
        # The rate row and source come from the type's own build_lines, never from lines a caller passes.
        raise DocumentRegistryError(f"{label}: sama_tax_point needs build_lines.")
    doc_type = DocumentType(label, model, post, reverse, entity_field, changed_at_field, creator_field, build_lines,
                            validate_lines, pending_documents, creator_may_post, contract_approval, reverse_guard,
                            sama_tax_point)
    existing = _REGISTRY.get(label)
    if existing is not None and existing != doc_type:
        raise DocumentRegistryError(f"{label} is already registered with other settings.")
    _REGISTRY[label] = doc_type
    return doc_type


def _unregister(model):
    """Remove a registration. Tests only (through :func:`_registered_for_tests` or directly)."""
    _REGISTRY.pop(model._meta.label_lower, None)


@contextmanager
def _registered_for_tests(model, **options):
    """Tests only: register ``model`` with ``options`` (replacing any registration) and restore the registry after.

    ``_registered_for_tests(model, unregistered=True)`` leaves the type unregistered instead.
    """
    snapshot = dict(_REGISTRY)
    try:
        _unregister(model)
        if not options.pop("unregistered", False):
            _register(model, **options)
        yield get(model._meta.label_lower)
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(snapshot)


def pending_in_period(entity, start_date, end_date):
    """Descriptions of approved documents of ``entity`` dated in the range that still wait to be booked."""
    found = []
    for doc_type in _REGISTRY.values():
        if doc_type.pending_documents is not None:
            found += [str(d) for d in doc_type.pending_documents(entity, start_date, end_date)]
    return found


def get(label):
    """The DocumentType registered under ``label`` (``app_label.model``), or None."""
    return _REGISTRY.get(label)


def registered():
    """A copy of the registry: {label: DocumentType}."""
    return dict(_REGISTRY)
