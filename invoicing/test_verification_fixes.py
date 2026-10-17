"""Regression tests for the verification review (round 5): the reviewer's confirmed exploits stay closed."""
from datetime import date
from unittest import mock

import pytest
from django.db import DatabaseError, connection, transaction

from core import access
from invoicing import admin as invoicing_admin
from invoicing import services
from invoicing.conftest import ADDRESS, approve, invoice
from invoicing.models import Customer, PurchaseDocument
from invoicing.test_purchases import bill

pytestmark = pytest.mark.django_db

PERSON_NAMES = ("Some Person", "فرد معين")


@pytest.fixture(autouse=True)
def _no_access_log_connection(settings):
    settings.ACCESS_LOG_DB = None  # admin requests run in the test transaction


def _person(w):
    return services.create_customer(w.alm, user=w.clerk, code="P1", name_ar="فرد معين", name_en="Some Person",
                                    customer_kind="individual", **ADDRESS)


def _staff(user):
    user.is_staff = True
    user.save()
    return user


def _page(client, user, url):
    client.force_login(_staff(user))
    response = client.get(url)
    assert response.status_code == 200
    return response.content.decode()


# --- item 1: individual customers' names in admin object labels -------------------------------------

def test_item1_customer_label_never_names_an_individual(w):
    person = _person(w)
    assert str(person) == "P1"
    assert person.display_name() == "P1"
    assert person.display_name(personal=True) == "P1 — فرد معين"
    assert str(w.customer) == "C001 — شركة المشتري التجريبية"  # a business is not personal data


@pytest.mark.parametrize("page", ["change", "history"])
def test_item1_admin_customer_pages_hide_an_individuals_name_from_a_viewer(w, client, page):
    person = _person(w)
    body = _page(client, w.viewer, f"/admin/invoicing/customer/{person.pk}/{page}/")
    assert "P1" in body
    assert not any(name in body for name in PERSON_NAMES)


@pytest.mark.parametrize("page", ["change", "history"])
def test_item1_admin_customer_pages_for_an_authorised_user(w, client, page):
    person = _person(w)
    assert access.has(w.clerk, w.alm, "invoicing.view_personal_data")
    body = _page(client, w.clerk, f"/admin/invoicing/customer/{person.pk}/{page}/")
    assert "P1" in body
    if page == "change":  # the fields themselves are shown to a user with the right
        assert all(name in body for name in PERSON_NAMES)


def test_item1_admin_document_and_receipt_pages_hide_the_name_from_a_viewer(w, client):
    person = _person(w)
    doc = approve(w, invoice(w, customer=person))
    receipt = services.create_receipt(w.alm, user=w.clerk, customer=person, date=date(2027, 1, 20), amount="10.00")
    for url in (f"/admin/invoicing/salesdocument/{doc.pk}/change/", f"/admin/invoicing/salesdocument/{doc.pk}/history/",
                f"/admin/invoicing/customerreceipt/{receipt.pk}/change/", "/admin/invoicing/customer/",
                "/admin/invoicing/salesdocument/", "/admin/invoicing/customerreceipt/"):
        body = _page(client, w.viewer, url)
        assert not any(name in body for name in PERSON_NAMES), url


def test_item1_admin_lists_show_the_name_to_an_authorised_user(w, client):
    person = _person(w)
    approve(w, invoice(w, customer=person))
    for url in ("/admin/invoicing/customer/", "/admin/invoicing/salesdocument/"):
        assert "فرد معين" in _page(client, w.clerk, url), url


# --- item 9: the personal-data right is checked once per request and entity -------------------------

def test_item9_personal_data_right_cached_per_request_and_entity(w, client):
    for n in range(4):
        services.create_customer(w.alm, user=w.clerk, code=f"P{n}", name_ar=f"فرد {n}", customer_kind="individual",
                                 **ADDRESS)
    real_has = access.has
    calls = []

    def counting_has(user, entity, permission):
        if permission == "invoicing.view_personal_data":
            calls.append(entity.pk)
        return real_has(user, entity, permission)

    client.force_login(_staff(w.viewer))
    with mock.patch.object(invoicing_admin.access, "has", side_effect=counting_has):
        assert client.get("/admin/invoicing/customer/").status_code == 200
    assert calls == [w.alm.pk]


# --- item 3: the purification flag of a posted bill (PostgreSQL trigger, migration 0008) -------------

postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")


def _raw(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@postgres_only
def test_item3_purification_flag_of_a_posted_bill_never_flips_in_the_database(w):
    """Reviewer probe p11: raw SQL flipped requires_purification on a posted bill."""
    b = services.approve_purchase_document(bill(w), user=w.cfo)
    assert b.journal_entry_id is not None
    for sql in ("UPDATE invoicing_purchasedocument SET requires_purification = NOT requires_purification WHERE id = %s",
                "UPDATE invoicing_purchasedocument SET requires_purification = NOT requires_purification, "
                "journal_entry_id = journal_entry_id WHERE id = %s"):
        with pytest.raises(DatabaseError, match="purification flag"):
            _raw(sql, [b.pk])
    assert PurchaseDocument.objects.get(pk=b.pk).requires_purification == b.requires_purification


@postgres_only
def test_item3_purification_flag_is_written_with_the_entry_and_equals_its_lines(w):
    """A write that records the entry with a flag its lines do not carry is refused; the posting rolls back."""
    real_save = PurchaseDocument.save

    def wrong_flag(self, *args, **kwargs):
        if "journal_entry" in (kwargs.get("update_fields") or ()):
            self.requires_purification = not self.requires_purification
        return real_save(self, *args, **kwargs)

    doc = bill(w)
    with mock.patch.object(PurchaseDocument, "save", wrong_flag):
        with pytest.raises(DatabaseError, match="entry's lines"):
            services.approve_purchase_document(doc, user=w.cfo)
    assert PurchaseDocument.objects.get(pk=doc.pk).status == "submitted"
