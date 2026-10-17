"""Regression tests for the verification review (round 5): the reviewer's confirmed exploits stay closed."""
from datetime import date
from unittest import mock

import pytest

from core import access
from invoicing import admin as invoicing_admin
from invoicing import services
from invoicing.conftest import ADDRESS, approve, invoice
from invoicing.models import Customer

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
