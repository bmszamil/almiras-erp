"""AT-6 under real concurrency (PostgreSQL only): two approvals at the same moment get consecutive numbers."""
import threading
from datetime import date

import pytest
from django.db import connection, connections, transaction

pytestmark = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL-only locking test")


@pytest.mark.django_db(transaction=True)
def test_at6_concurrent_approvals_get_consecutive_numbers():
    from django.apps import apps
    from django.contrib.auth import get_user_model

    from core.access.models import Role, RolePermission, UserProfile
    from core.access.seed import apply_seed, apply_system_users
    from core.currency_seed import apply_currency_seed
    from core.models import Currency
    from invoicing import reference_seed, services
    from invoicing.conftest import ADDRESS, invoice
    from invoicing.models import SalesDocument
    from invoicing.testing import fake_zatca

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    apply_system_users(get_user_model(), UserProfile)
    reference_seed.apply(apps)
    from types import SimpleNamespace

    from core.access.testing import make_user, member
    from invoicing.conftest import make_entity

    alm = make_entity("ALM", "parent", vat="300000000000003", cr="1010000001")
    clerk, cfo, cfo2 = make_user("clerk"), make_user("cfo"), make_user("cfo2")
    member(clerk, alm, "receivables_clerk")
    member(cfo, alm, "approver")
    member(cfo2, alm, "approver")
    customer = services.create_customer(alm, user=clerk, code="C1", name_ar="عميل", is_vat_registered=True,
                                        vat_number="310000000000003", **ADDRESS)
    with fake_zatca():
        w = SimpleNamespace(alm=alm, clerk=clerk, customer=customer)
        first, second = invoice(w), invoice(w)
        result = {}

        def other():
            try:
                result["number"] = services.approve_sales_document(second, user=cfo2).number
            except Exception as exc:  # noqa: BLE001 - reported in the assertion
                result["error"] = repr(exc)
            finally:
                connections.close_all()

        with transaction.atomic():
            number = services.approve_sales_document(first, user=cfo).number  # holds the sequence row
            thread = threading.Thread(target=other)
            thread.start()
            thread.join(timeout=1.5)
            assert thread.is_alive(), "the second approval should wait for the sequence lock"
        thread.join(timeout=10)
    assert (number, result.get("number")) == ("ALM-INV-2027-000001", "ALM-INV-2027-000002"), result
    assert SalesDocument.objects.filter(status="issued").count() == 2
