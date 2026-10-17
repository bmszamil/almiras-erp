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
    from invoicing.testing import approval_day, fake_zatca

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
    with fake_zatca(), approval_day(date(2027, 1, 10)):
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


def _seeded_world():
    """Roles, currencies, reference data and an ALM entity with a clerk, two approvers and a customer."""
    from types import SimpleNamespace

    from django.apps import apps
    from django.contrib.auth import get_user_model

    from core import access
    from core.access.models import Role, RolePermission, UserProfile
    from core.access.seed import apply_seed, apply_system_users
    from core.access.testing import make_user, member
    from core.currency_seed import apply_currency_seed
    from core.models import Currency
    from invoicing import reference_seed, services
    from invoicing.conftest import ADDRESS, make_entity

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    apply_system_users(get_user_model(), UserProfile)
    reference_seed.apply(apps)
    alm = make_entity("ALM", "parent", vat="300000000000003", cr="1010000001")
    w = SimpleNamespace(alm=alm, clerk=make_user("clerk"), ap=make_user("ap"), cfo=make_user("cfo"),
                        cfo2=make_user("cfo2"), queue=access.system_user("zatca_queue"))
    member(w.clerk, alm, "receivables_clerk")
    member(w.ap, alm, "payables_clerk")
    member(w.cfo, alm, "approver")
    member(w.cfo2, alm, "approver")
    member(w.queue, alm, "system_zatca_queue")
    w.customer = services.create_customer(alm, user=w.clerk, code="C1", name_ar="عميل", is_vat_registered=True,
                                          vat_number="310000000000003", **ADDRESS)
    w.vendor = services.create_vendor(alm, user=w.ap, code="V1", name_ar="مورد", is_vat_registered=True,
                                      vat_number="320000000000003")
    return w


def _race(first_call, second_call):
    """Run ``first_call`` in a transaction that stays open while ``second_call`` starts in another thread."""
    result = {}

    def other():
        try:
            result["value"] = second_call()
        except Exception as exc:  # noqa: BLE001 - reported to the test
            result["error"] = exc
        finally:
            connections.close_all()

    with transaction.atomic():
        first = first_call()
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive(), "the second approval should wait for the lock on the original document"
    thread.join(timeout=10)
    return first, result


@pytest.mark.django_db(transaction=True)
def test_item4_two_credit_notes_approved_at_once_cannot_exceed_the_invoice():
    from invoicing import services
    from invoicing.conftest import invoice
    from invoicing.models import SalesDocument
    from invoicing.testing import approval_day, fake_zatca

    w = _seeded_world()
    with fake_zatca() as port, approval_day(date(2027, 1, 10)):
        inv = services.approve_sales_document(invoice(w), user=w.cfo)  # gross 1,150.00
        port.clear(inv)
        notes = [invoice(w, doc_type="credit_note", original_document=SalesDocument.objects.get(pk=inv.pk),
                         payment_means_code="30", reason_ar="إلغاء") for _ in range(2)]  # 1,150.00 each
        first, result = _race(lambda: services.approve_sales_document(notes[0], user=w.cfo),
                              lambda: services.approve_sales_document(notes[1], user=w.cfo2))
    assert first.status == "issued"
    assert "error" in result and getattr(result["error"], "code", None) == "document_invalid", result
    assert SalesDocument.objects.get(pk=notes[1].pk).status == "submitted"


@pytest.mark.django_db(transaction=True)
def test_item4_two_vendor_credit_notes_approved_at_once_cannot_exceed_the_bill():
    from invoicing import services
    from invoicing.models import PurchaseDocument
    from invoicing.test_purchases import bill

    w = _seeded_world()
    b = services.approve_purchase_document(bill(w), user=w.cfo)  # gross 1,150.00
    notes = [bill(w, number=f"CN-{i}", doc_type="vendor_credit_note", original_document=b) for i in range(2)]
    first, result = _race(lambda: services.approve_purchase_document(notes[0], user=w.cfo),
                          lambda: services.approve_purchase_document(notes[1], user=w.cfo2))
    assert first.status == "posted"
    assert "error" in result and getattr(result["error"], "code", None) == "document_invalid", result
    assert PurchaseDocument.objects.get(pk=notes[1].pk).status == "submitted"
