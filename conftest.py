"""Shared fixtures: anonymised sample entities and users only (no real data)."""
import datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.test import APIClient

from access import services as access
from access.models import User
from approvals import services as approvals
from core.models import Entity

D = Decimal


@pytest.fixture
def alm(db):
    from ledger.services_setup import create_monthly_periods, install_chart_of_accounts

    e = Entity.objects.create(code="ALM", name_en="Almiras", name_ar="المراس", consolidation="parent")
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2027)
    return e


@pytest.fixture
def sub(alm):
    from ledger.services_setup import create_monthly_periods, install_chart_of_accounts

    e = Entity.objects.create(code="LZM", name_en="Lizam", name_ar="لزام", parent=alm, ownership_pct=D("60"))
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2027)
    return e


@pytest.fixture
def make_user(db):
    def make(username, *roles, user_type="human", language="en", email=None, agent_name=""):
        """roles: (entity, role_code) pairs."""
        u = User.objects.create(
            username=username, full_name_en=username.title(), full_name_ar=f"م {username}",
            email=f"{username}@example.test" if email is None else email,
            language=language, user_type=user_type, agent_name=agent_name,
        )
        for entity, role in roles:
            access.grant_access(None, u, entity, role, valid_from=datetime.date(2026, 1, 1))
        return u

    return make


@pytest.fixture
def mp(make_user, alm):
    return make_user("mp", (alm, "managing_partner"))


@pytest.fixture
def chairman(make_user, alm):
    return make_user("chairman", (alm, "chairman"))


@pytest.fixture
def accountant(make_user, alm):
    return make_user("acc", (alm, "accountant"))


@pytest.fixture
def agent(make_user, alm):
    return make_user("invoice-reader-bot", (alm, "agent_drafter"), user_type="service_agent", agent_name="invoice-reader")


@pytest.fixture
def payment_type(db):
    from testdocs.models import Bill, Payment

    approvals.register_type("payments.payment", Payment, "Payment", "دفعة", is_spending=True)
    approvals.register_type("payables.bill", Bill, "Supplier bill", "فاتورة مورد", is_spending=True)
    approvals.ensure_spending_policies()  # R6, as the modules' own data migrations will do


@pytest.fixture
def make_payment(payment_type):
    from testdocs.models import Payment

    def make(user, entity, amount="1000", **kw):
        return Payment.objects.create(
            entity=entity, counterparty=kw.pop("counterparty", "Supplier A"), amount=D(amount),
            date=kw.pop("date", datetime.date(2027, 1, 10)), created_by=user, source=user.source, **kw
        )

    return make


@pytest.fixture
def make_bill(payment_type):
    from testdocs.models import Bill

    def make(user, entity, amount="500"):
        return Bill.objects.create(entity=entity, counterparty="Supplier B", amount=D(amount),
                                   date=datetime.date(2027, 1, 10), created_by=user, source=user.source)

    return make


@pytest.fixture
def api():
    """API client signed in as a human with a verified second factor (D14), or a service token."""

    def client_for(user):
        c = APIClient()
        if user.is_human:
            device = TOTPDevice.objects.create(user=user, name="test", confirmed=True)
            c.force_login(user)
            session = c.session
            from django_otp import DEVICE_ID_SESSION_KEY

            session[DEVICE_ID_SESSION_KEY] = device.persistent_id
            session.save()
        else:
            key = access.issue_service_token(None, user, expires_at=timezone.now() + datetime.timedelta(days=30))
            c.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
        return c

    return client_for


@pytest.fixture
def clock(monkeypatch):
    """Freeze the approvals engine clock."""

    class Clock:
        def __init__(self):
            self.now = timezone.now()

        def set(self, value):
            self.now = value

        def advance(self, **kw):
            self.now = self.now + datetime.timedelta(**kw)

    c = Clock()
    monkeypatch.setattr(approvals, "now", lambda: c.now)
    return c
