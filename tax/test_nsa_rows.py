"""NSA rows a and b on tax's non-A rights (access R80; review of 92c35c2, item 10): ``tax.review_return`` and
``tax.maintain_rates`` are not approval permissions, so the engine never sees them; tax's own checks refuse the
maker, and they refuse the entity's top authority too (the managing partner in ALM). Anonymised data."""
from datetime import date

import pytest

from core import access
from core.access import catalogue
from core.access.testing import member
from tax import services
from tax.models import VatBoxDefinition

pytestmark = pytest.mark.django_db
Q1 = date(2027, 1, 1)


def test_nsa_a_the_top_authority_never_reviews_a_return_they_prepared(t):
    assert "tax.review_return" in catalogue.NSA_ROWS["a"]
    member(t.mp, t.alm, "accountant")  # the MP prepares and could review
    ret = services.submit_for_review(services.compute_draft(t.alm, Q1, user=t.mp), user=t.mp)
    with pytest.raises(access.AccessDenied) as exc:
        services.complete_review(ret, user=t.mp)
    assert exc.value.code == "self_approval"
    assert services.complete_review(ret, user=t.a2).status == "reviewed"


def test_nsa_b_the_top_authority_never_verifies_their_own_box_proposal(t):
    assert "tax.maintain_rates" in catalogue.NSA_ROWS["b"]
    member(t.mp, t.alm, "accountant")
    member(t.mp, t.alm, "approver")
    definition = VatBoxDefinition.objects.get(box_no=5)
    services.propose_box_verification(definition, user=t.mp, entity=t.alm)
    with pytest.raises(access.AccessDenied) as exc:
        services.verify_box_definition(definition, user=t.mp, entity=t.alm)
    assert exc.value.code == "self_approval"
    assert VatBoxDefinition.objects.get(pk=definition.pk).verified_at is None
