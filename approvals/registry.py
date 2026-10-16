"""Plug-in contract (spec §3.3): how a module's document becomes approvable.

A module provides an adapter class (subclass of ApprovalAdapter) in its own code and lists it in
settings.APPROVAL_ADAPTERS = {"<type code>": "<dotted.path.Adapter>"}; it also registers an
ApprovableType row through a data migration. Adapters are loaded lazily, so approvals never
imports module code at start-up and modules never write ApprovalRequest rows themselves.
"""
import hashlib
import json
from functools import lru_cache

from django.conf import settings
from django.utils.module_loading import import_string

from access.errors import RuleError
from access.services import to_json


class ApprovalAdapter:
    model = None
    #: Extra permission (besides approvals.request.submit) needed to submit, e.g. ledger.entry.create
    submit_permission = None
    #: Fields left out of the document fingerprint (workflow state, not content)
    fingerprint_exclude = {"status", "updated_at"}
    draft_status = "draft"

    def get_for_update(self, object_id):
        return self.model.objects.select_for_update().get(pk=object_id)

    def entity(self, doc):
        return doc.entity

    def created_by(self, doc):
        return doc.created_by

    def source(self, doc):
        return getattr(doc, "source", "human") or "human"

    def is_submittable(self, doc):
        return doc.status == self.draft_status

    def approval_amount(self, doc):
        """Return (Decimal or None, currency)."""
        raise NotImplementedError

    def approval_summary(self, doc):
        """Return (summary_en, summary_ar)."""
        raise NotImplementedError

    def fingerprint_data(self, doc):
        data = {}
        for f in doc._meta.concrete_fields:
            if f.name in self.fingerprint_exclude:
                continue
            data[f.attname] = to_json(getattr(doc, f.attname))
        return data

    def document_version(self, doc):
        payload = json.dumps(self.fingerprint_data(doc), sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def snapshot(self, doc):
        return {"status": doc.status}

    # Lifecycle hooks; called inside the approval transaction.
    def on_submitted(self, doc, request):
        raise NotImplementedError

    def on_approved(self, doc, request, actor):
        raise NotImplementedError

    def on_rejected(self, doc, request):
        raise NotImplementedError

    def on_returned(self, doc, request):
        raise NotImplementedError

    def on_cancelled(self, doc, request):
        self.on_returned(doc, request)

    def on_expired(self, doc, request):
        self.on_returned(doc, request)


@lru_cache(maxsize=None)
def _adapter(code, path):
    return import_string(path)()


def adapter_for_code(code):
    path = getattr(settings, "APPROVAL_ADAPTERS", {}).get(code)
    if not path:
        raise RuleError(
            "type_not_approvable",
            f"Document type {code} is not connected to approvals.",
            f"نوع المستند {code} غير مرتبط بالموافقات.",
        )
    return _adapter(code, path)


def code_for_model(model):
    for code, path in getattr(settings, "APPROVAL_ADAPTERS", {}).items():
        if _adapter(code, path).model is model._meta.concrete_model:
            return code
    raise RuleError(
        "type_not_approvable",
        f"{model._meta.label} is not connected to approvals.",
        f"{model._meta.label} غير مرتبط بالموافقات.",
    )
