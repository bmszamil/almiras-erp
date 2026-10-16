"""Registry of sub-ledger document types that may book journal entries (access.md R23, R30).

``create_entry(post=True, grant=..., document=...)`` posts an entry for one
sub-ledger document (a bill, a receipt, a sales document) with a grant for a
sub-ledger **P** permission. The ledger accepts such a posting only for a
document that

- is a saved model instance (never a tuple or another stand-in),
- is of a type registered here by the module that owns it, with the
  permissions allowed to post it, and
- exists in the database and belongs to the entity being posted.

A sub-ledger app registers its document models in its ``AppConfig.ready()``::

    from ledger import documents
    documents.register(Bill, post_permissions={"invoicing.approve_bill"},
                       reverse_permissions={"invoicing.reverse_document"})

Until a module registers a type, ``create_entry(post=True)`` with a sub-ledger
grant is refused. Nothing is registered by the ledger itself.
"""
from dataclasses import dataclass

from django.db import models

from core.access import catalogue


class DocumentRegistryError(RuntimeError):
    """A programming error in a document registration."""


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

    def entity_id_of(self, document):
        return getattr(document, f"{self.entity_field}_id")

    def changed_at_of(self, document):
        return getattr(document, self.changed_at_field, None) if self.changed_at_field else None


_REGISTRY = {}

# Sub-ledger P permissions that post a document's entry; reversal grants only reverse (R30).
POSTING_PERMISSIONS = catalogue.DOCUMENT_POST_PERMISSIONS
REVERSING_PERMISSIONS = catalogue.REVERSE_PERMISSIONS - {"ledger.reverse_entry"}


def register(model, *, post_permissions, reverse_permissions=(), entity_field="entity", changed_at_field="modified_at"):
    """Register ``model`` as a document type that books journal entries. Idempotent for the same arguments."""
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
    try:
        field = model._meta.get_field(entity_field)
    except Exception as exc:
        raise DocumentRegistryError(f"{label} has no field {entity_field!r}.") from exc
    if not (field.many_to_one and field.related_model._meta.label_lower == "core.entity"):
        raise DocumentRegistryError(f"{label}.{entity_field} is not a foreign key to core.Entity.")
    if changed_at_field is not None:
        model._meta.get_field(changed_at_field)
    doc_type = DocumentType(label, model, post, reverse, entity_field, changed_at_field)
    existing = _REGISTRY.get(label)
    if existing is not None and existing != doc_type:
        raise DocumentRegistryError(f"{label} is already registered with other settings.")
    _REGISTRY[label] = doc_type
    return doc_type


def unregister(model):
    """Remove a registration (tests only)."""
    _REGISTRY.pop(model._meta.label_lower, None)


def get(label):
    """The DocumentType registered under ``label`` (``app_label.model``), or None."""
    return _REGISTRY.get(label)


def registered():
    """A copy of the registry: {label: DocumentType}."""
    return dict(_REGISTRY)
