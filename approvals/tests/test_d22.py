"""D22 (MP decision, 17 Oct 2026): the MP who submits an AI agent's or integration's draft and is the
only eligible approver may approve it as a logged self-approval override (like D10)."""
import datetime

import pytest
from django.test import TestCase
from django.utils import timezone

from access.errors import AccessDenied
from access.models import AuditLog
from approvals import services

pytestmark = pytest.mark.django_db


def approve(request, user):
    request.refresh_from_db()
    with TestCase.captureOnCommitCallbacks(execute=True):
        return services.act(request, user, "approve", document_version=request.document_version)


@pytest.fixture
def integration(make_user, alm):
    return make_user("bank-feed", (alm, "agent_drafter"), user_type="service_integration")


@pytest.mark.parametrize("creator", ["agent", "integration"])
def test_d22_mp_submits_service_draft_and_self_approves_with_override(request, alm, mp, make_bill, creator):
    bot = request.getfixturevalue(creator)
    r = services.submit(make_bill(bot, alm), mp)
    assert r.document_created_by == bot and r.submitted_by == mp
    assert "approve" in services.allowed_actions(r, mp)
    r = approve(r, mp)
    assert r.status == "approved"
    action = r.actions.get(action="approve")
    assert action.self_approval_override is True and action.actor == mp
    assert AuditLog.objects.filter(action="approval.self_approval_override", object_id=str(r.pk)).exists()
    now = timezone.localtime()
    assert [x["request_id"] for x in services.self_approval_report(mp, now.year, now.month)] == [r.pk]


def test_d22_no_override_when_another_approver_is_eligible(alm, mp, agent, make_user, make_bill):
    make_user("mp2", (alm, "managing_partner"))
    r = services.submit(make_bill(agent, alm), mp)
    with pytest.raises(AccessDenied):
        approve(r, mp)


def test_d22_accountant_submitter_of_agent_draft_cannot_self_approve(alm, mp, agent, accountant, make_bill):
    """Only the MP gets the override; the accountant who submitted is blocked by R8, the MP approves normally."""
    r = services.submit(make_bill(agent, alm), accountant)
    with pytest.raises(AccessDenied):
        approve(r, accountant)
    r = approve(r, mp)
    assert r.status == "approved" and not r.actions.get(action="approve").self_approval_override


def test_d22_d16_delegation_still_beats_override(alm, mp, agent, make_user, make_bill):
    deputy = make_user("deputy", (alm, "viewer"))
    now = timezone.now()
    services.create_delegation(mp, delegator=mp, delegate=deputy, starts_at=now - datetime.timedelta(hours=1),
                               ends_at=now + datetime.timedelta(days=1))
    r = services.submit(make_bill(agent, alm), mp)
    with pytest.raises(AccessDenied):
        approve(r, mp)
    r = approve(r, deputy)
    a = r.actions.get(action="approve")
    assert r.status == "approved" and a.on_behalf_of == mp and not a.self_approval_override
