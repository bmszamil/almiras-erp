"""Document registry: creators, line hooks, who may register (access.md R34 to R36; verification review).

Regression tests for the reviewer's probes (probe3):

- A: a document with no recorded creator was booked by the clerk who made it
  (segregation had nobody to compare with).
- B: any lines could be booked for a document; nothing tied them to it.
- F / F2: any code could register any model (e.g. ``core.Membership``), or
  unregister and re-register a type with other posting permissions.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.conf import settings
from django.db import IntegrityError, models, transaction

from core import access
from core.access.models import GrantUse, Membership
from core.models import Entity
from ledger import documents, services
from ledger.models import JournalEntry, LedgerError
from ledger.test_posting_hardening import CASH, doc, w  # noqa: F401
from testdocs.models import Bill, Receipt, Unregistered

pytestmark = pytest.mark.django_db


class NullableCreatorDocument(models.Model):
    """Never migrated: only its field definitions are read by the registry."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    modified_at = models.DateTimeField(null=True)

    class Meta:
        app_label = "testdocs"
        managed = False


class CreatorNotAUserDocument(models.Model):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    modified_at = models.DateTimeField(null=True)

    class Meta:
        app_label = "testdocs"
        managed = False


class NoCreatorDocument(models.Model):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    modified_at = models.DateTimeField(null=True)

    class Meta:
        app_label = "testdocs"
        managed = False


# --- item 2: a document records its creator -------------------------------------------------------

@pytest.mark.parametrize("model", [NullableCreatorDocument, CreatorNotAUserDocument, NoCreatorDocument])
def test_registration_needs_a_non_null_creator(model):
    with pytest.raises(documents.DocumentRegistryError, match="created_by"):
        documents._register(model, post_permissions={"invoicing.post_receipt"})
    assert documents.get(model._meta.label_lower) is None


def test_probe_a_documents_cannot_be_stored_without_a_creator(w):
    with pytest.raises(IntegrityError), transaction.atomic():
        Receipt.objects.create(entity=w.alm, created_by=None)


def test_posting_refuses_a_document_with_an_empty_creator(w, monkeypatch):
    """Defence in depth: rows that predate the NOT NULL rule, or a type whose creator reads empty."""
    receipt = doc(Receipt, w.alm, created_by=w.cfo)
    monkeypatch.setattr(documents.DocumentType, "creator_id_of", lambda self, document: None)
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt)
    with pytest.raises(access.AccessDenied) as exc:
        services.create_entry(w.alm, date(2027, 1, 5), "Receipt", CASH, user=w.clerk, post=True, grant=grant,
                              document=receipt)
    assert exc.value.code == "document_no_creator" and exc.value.message_ar
    assert not JournalEntry.objects.exists() and not GrantUse.objects.filter(log_id=grant.log_id).exists()


# --- item 2: the document type's line hooks -------------------------------------------------------------

def test_probe_b_lines_must_match_the_document(w):
    receipt = doc(Receipt, w.alm, created_by=w.cfo)  # amount 10.00
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt)
    unrelated = [{"account": "1100", "debit": 50}, {"account": "3100", "credit": 50}]
    with pytest.raises(LedgerError) as exc:
        services.create_entry(w.alm, date(2027, 1, 5), "unrelated", unrelated, user=w.clerk, post=True, grant=grant,
                              document=receipt)
    assert exc.value.code == "document_lines_rejected"
    text = " ".join(exc.value.messages)
    assert "do not match" in text and "لا تطابق" in text and "differ from the document amount" in text
    assert not JournalEntry.objects.exists() and not GrantUse.objects.filter(log_id=grant.log_id).exists()
    booked = services.create_entry(w.alm, date(2027, 1, 5), "Receipt", CASH, user=w.clerk, post=True, grant=grant,
                                   document=receipt)
    assert booked.status == "posted"


def test_validate_lines_sees_the_stored_document(w):
    receipt = doc(Receipt, w.alm, created_by=w.cfo)
    receipt.amount = Decimal("50.00")  # in memory only
    lines = [{"account": "1100", "debit": 50}, {"account": "3100", "credit": 50}]
    with pytest.raises(LedgerError) as exc:
        services.create_entry(w.alm, date(2027, 1, 5), "x", lines, user=w.clerk, post=True, document=receipt,
                              grant=access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt))
    assert exc.value.code == "document_lines_rejected"


def test_build_lines_hook_supplies_the_lines(w):
    def build(document):
        return [{"account": "1100", "debit": document.amount}, {"account": "3100", "credit": document.amount}]

    seen = []

    def validate(document, lines):
        seen.append((document.pk, [(l.account.code, l.debit, l.credit) for l in lines]))
        return []

    with documents._registered_for_tests(Unregistered, post_permissions={"invoicing.post_receipt"},
                                         build_lines=build, validate_lines=validate):
        item = Unregistered.objects.create(entity=w.alm, created_by=w.cfo, amount=Decimal("25.00"))
        grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=item)
        with pytest.raises(LedgerError) as exc:  # the type builds its lines: the caller passes none
            services.create_entry(w.alm, date(2027, 1, 5), "x", CASH, user=w.clerk, post=True, grant=grant, document=item)
        assert exc.value.code == "document_builds_lines"
        entry = services.create_entry(w.alm, date(2027, 1, 5), "Built", None, user=w.clerk, post=True, grant=grant,
                                      document=item)
    assert entry.status == "posted"
    assert sorted((l.account.code, l.debit, l.credit) for l in entry.lines.all()) == [
        ("1100", Decimal("25.00"), Decimal("0.00")), ("3100", Decimal("0.00"), Decimal("25.00")),
    ]
    assert seen == [(item.pk, [("1100", Decimal("25.00"), Decimal("0")), ("3100", Decimal("0"), Decimal("25.00"))])]
    assert documents.get("testdocs.unregistered") is None


def test_hooks_must_be_callable():
    with pytest.raises(documents.DocumentRegistryError, match="callable"):
        documents._register(Unregistered, post_permissions={"invoicing.post_receipt"}, validate_lines="not a function")
    assert documents.get("testdocs.unregistered") is None


# --- item 3: who may register, and when ----------------------------------------------------------------

def test_registry_is_frozen_after_start_up():
    with pytest.raises(documents.DocumentRegistryError, match="frozen"):
        documents.register(Unregistered, post_permissions={"invoicing.post_receipt"})
    assert documents.get("testdocs.unregistered") is None


def test_probe_f_only_the_models_own_app_registers_it(monkeypatch):
    monkeypatch.setattr(documents, "_frozen", lambda: False)  # as during start-up
    for model in (Membership, Bill, Unregistered):  # from a ledger test module: not their app
        with pytest.raises(documents.DocumentRegistryError, match="only by its own app"):
            documents.register(model, post_permissions={"invoicing.post_receipt"}, changed_at_field=None)
    assert documents.get("core.membership") is None and documents.get("testdocs.unregistered") is None
    assert documents.get("testdocs.bill").post_permissions == {"invoicing.approve_bill"}
    documents._check_caller(Bill, "testdocs.apps")  # the model's own app may
    for module in ("ledger.services", "core.access.services", "", "not_an_app"):
        with pytest.raises(documents.DocumentRegistryError):
            documents._check_caller(Bill, module)


def test_probe_f2_unregister_is_not_public():
    assert not hasattr(documents, "unregister")
    assert documents.get("testdocs.bill").post_permissions == {"invoicing.approve_bill"}
