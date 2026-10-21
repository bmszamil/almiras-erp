"""Submitting a contract is its own right (verification review of 49e1abd, item 3, probe N9; access R60, R81).

A personal assistant drafts contracts (``billing.edit_contract_draft``) but never submits them; the engine also
refuses a submission made under any right other than the one the adapter declares. Made-up data."""
import pytest
from django.core.exceptions import PermissionDenied

from billing import services
from billing.conftest import contract
from core.access import engine
from core.access import services as access
from core.access.authority_models import ApprovalRequest
from core.access.seed import ROLE_SEED
from core.access.testing import make_user, member

pytestmark = pytest.mark.django_db


def test_rv93_n9_a_personal_assistant_drafts_but_never_submits_a_contract(w):
    assert "billing.submit_contract" not in ROLE_SEED["personal_assistant"][5]
    assert "billing.edit_contract_draft" in ROLE_SEED["personal_assistant"][5]
    pa = make_user("pa_b")
    member(pa, w.alm, "personal_assistant")
    c = contract(w, user=pa)
    with pytest.raises(PermissionDenied):
        services.submit_contract(c, user=pa)
    assert not ApprovalRequest.objects.filter(document_type="billing.contract", document_id=c.pk).exists()
    done = services.submit_contract(c, user=w.s)
    assert done.submitted_by == w.s
    assert ApprovalRequest.objects.get(document_type="billing.contract", document_id=c.pk).submitted_by == w.s


def test_rv93_engine_submit_refuses_a_right_the_adapter_does_not_declare(w):
    c = contract(w)
    with pytest.raises(access.AccessUsageError):
        engine.submit(c, w.s, submit_permission="billing.edit_contract_draft")
    assert not ApprovalRequest.objects.filter(document_type="billing.contract", document_id=c.pk).exists()
