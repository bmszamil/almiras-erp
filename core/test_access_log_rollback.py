"""R27 / AT-1: a ``permission_denied`` row survives the caller's rollback.

These tests commit for real (transaction=True) and use the separate
access-log connection (settings.ACCESS_LOG_DB), as a request with
ATOMIC_REQUESTS would.
"""
from datetime import date

import pytest
from django.conf import settings
from django.db import connections, transaction

from core import access
from core.access import services as access_services
from core.access.models import AccessLog, Role, RolePermission
from core.access.seed import apply_seed
from core.access.testing import make_user, member
from core.currency_seed import apply_currency_seed
from core.models import Currency, Entity
from ledger import services as ledger
from ledger.models import FiscalPeriod
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts

LOG_DB = settings.ACCESS_LOG_DB
pytestmark = pytest.mark.django_db(transaction=True, databases=["default", LOG_DB])


class Boom(Exception):
    pass


def denials(**filters):
    return AccessLog.objects.filter(event=AccessLog.Event.PERMISSION_DENIED, **filters)


@pytest.fixture
def world():
    apply_seed(Role, RolePermission)  # transactional tests start from a flushed database
    apply_currency_seed(Currency)  # Entity.base_currency is a FK to Currency (ledger-fx.md 3.2)
    alm = Entity.objects.create(code="ALM", name_en="ALM", name_ar="كيان", consolidation="parent")
    install_chart_of_accounts(alm)
    create_monthly_periods(alm, 2027)
    clerk = make_user("clerk")
    member(clerk, alm, "accountant")
    return alm, clerk


def test_log_connection_is_configured_separately():
    db = settings.DATABASES[LOG_DB]
    assert db["NAME"] == settings.DATABASES["default"]["NAME"]
    assert db["ATOMIC_REQUESTS"] is False and db["AUTOCOMMIT"] is True
    assert db["TEST"]["MIRROR"] == "default"
    assert connections[LOG_DB] is not connections["default"]


def test_denial_survives_rollback_of_callers_transaction(world):
    alm, clerk = world
    with pytest.raises(access.AccessDenied):
        with transaction.atomic():  # as ATOMIC_REQUESTS wraps a view
            access.require(clerk, alm, "ledger.close_period")
    assert denials(actor=clerk, entity=alm, permission="ledger.close_period", details__reason="no_membership").count() == 1


def test_denial_survives_when_caller_rolls_back_for_another_reason(world):
    alm, clerk = world
    with pytest.raises(Boom):
        with transaction.atomic():
            try:
                access.require(clerk, alm, "ledger.close_period")
            except access.AccessDenied:
                raise Boom from None
    assert denials(actor=clerk, permission="ledger.close_period").count() == 1


def test_denied_close_period_is_logged_and_changes_nothing(world):
    alm, clerk = world
    jan = alm.periods.get(name="2027-01")
    with pytest.raises(access.AccessDenied):
        with transaction.atomic():
            ledger.close_period(jan, user=clerk)  # close_period is atomic itself
    assert FiscalPeriod.objects.get(pk=jan.pk).status == "open"
    assert denials(actor=clerk, entity=alm, permission="ledger.close_period").count() == 1


def test_denial_outside_a_transaction_is_logged_once(world):
    alm, clerk = world
    with pytest.raises(access.AccessDenied):
        access.require(clerk, alm, "ledger.close_period")
    assert denials(actor=clerk).count() == 1


def test_falls_back_to_callers_transaction_when_log_connection_cannot_write(world, monkeypatch):
    # e.g. the actor was created in the still-open transaction and the log
    # connection cannot see it: the row is then written in the caller's
    # transaction and kept if that transaction commits.
    alm, _ = world
    with transaction.atomic():
        newcomer = make_user("newcomer")  # not committed yet
        with pytest.raises(access.AccessDenied):
            access.require(newcomer, alm, "ledger.view")
    assert denials(actor=newcomer, permission="ledger.view").count() == 1


def test_granted_rows_still_follow_the_callers_transaction(world):
    # Only denials are written outside the transaction; grants and membership
    # changes stay in the caller's transaction (R27).
    alm, clerk = world
    cfo = make_user("cfo")
    member(cfo, alm, "approver")
    draft = ledger.create_entry(alm, date(2027, 1, 5), "x", [
        {"account": "1100", "debit": 1}, {"account": "3100", "credit": 1},
    ], user=clerk)
    with pytest.raises(Boom):
        with transaction.atomic():
            access.require(cfo, alm, "ledger.post_entry", document=draft)
            raise Boom
    assert not AccessLog.objects.filter(event=AccessLog.Event.PERMISSION_GRANTED, actor=cfo).exists()


def test_without_log_connection_denial_is_written_in_callers_transaction(world, settings):
    settings.ACCESS_LOG_DB = None
    alm, clerk = world
    with pytest.raises(access.AccessDenied):
        with transaction.atomic():
            access_services.require(clerk, alm, "ledger.close_period")
    assert not denials(actor=clerk).exists()  # the old behaviour this connection fixes
