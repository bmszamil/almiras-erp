"""Test helpers for access fixtures. Anonymised users only; never used outside tests."""
from datetime import timedelta
from itertools import count

from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import Kind, Membership, Role

_seq = count(1)


def make_user(username=None, kind=Kind.HUMAN, language="en", **profile):
    """Create a user with the given profile kind (set before the first save, R10)."""
    User = get_user_model()
    user = User(username=username or f"user{next(_seq)}")
    user.set_unusable_password()
    user._access_profile = dict(kind=kind, preferred_language=language, **profile)
    user.save()
    return user


def make_agent(name="invoice_reader"):
    return make_user(f"agent.{name}", kind=Kind.AGENT, agent_name=name, agent_model="test-model", agent_version="0")


def grantor():
    """A named person used as ``granted_by`` on fixture memberships."""
    User = get_user_model()
    user = User.objects.filter(username="fixture.grantor").first()
    return user or make_user("fixture.grantor")


def member(user, entity, role_code, *, valid_from=None, valid_to=None, delegated_from=None, granted_by=None):
    """Create a membership directly (fixture setup; the grant service is tested separately)."""
    return Membership.objects.create(
        user=user,
        entity=entity,
        role=Role.objects.get(code=role_code),
        valid_from=valid_from or timezone.now() - timedelta(days=1),
        valid_to=valid_to,
        granted_by=granted_by or grantor(),
        reason="test fixture",
        delegated_from=delegated_from,
    )
