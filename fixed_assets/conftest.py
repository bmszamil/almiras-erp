"""Fixtures for the fixed-asset tests (fixed-assets.md §8): fictional entities ALM, SUB, NSH and FUL, users per role,
the proposed accounts as fixture data and test-only approved policies. Amounts are Decimal."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from django.utils import timezone

from core.access import services as access_services

from .testing import world


@pytest.fixture
def w(db):
    return world()


@pytest.fixture
def clock(monkeypatch):
    """Move the access layer's clock (grants, engine dates, approval dates of R16)."""
    state = SimpleNamespace(t=None)
    real = access_services.now
    monkeypatch.setattr(access_services, "now", lambda: state.t or real())

    def set_(*args):
        state.t = timezone.make_aware(datetime(*args), timezone.get_default_timezone())

    state.set = set_
    return state
