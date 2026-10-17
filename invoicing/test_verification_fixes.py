"""Regression tests for the verification review (round 5): the reviewer's confirmed exploits stay closed."""
from datetime import date, timedelta
from unittest import mock

import pytest
from django.db import DatabaseError, connection, transaction

from core import access
from core.access.models import AccessLog
from invoicing import admin as invoicing_admin
from invoicing import services
from invoicing.conftest import ADDRESS, approve, final, invoice
from invoicing.models import Customer, PurchaseDocument, SalesDocument
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


# --- item 2: stale approvals are found by the document version, not by two servers' clocks -----------

def _race_on_slow_clock(permission, action, behind=timedelta(seconds=2)):
    """Run ``action`` right after the ``permission`` grant is taken, on a server whose clock is ``behind``
    the approving one (reviewer probe p2b): its modified_at / submitted_at are older than the grant."""
    real_require, real_now = access.require, services.timezone.now

    def require_then_race(user, entity, perm, **kw):
        grant = real_require(user, entity, perm, **kw)
        if perm == permission:
            with mock.patch.object(services.timezone, "now", lambda: real_now() - behind):
                action()
        return grant

    return mock.patch.object(services.access, "require", side_effect=require_then_race)


def test_item2_sales_approval_refused_after_an_edit_on_a_server_with_a_slow_clock(w):
    doc = invoice(w)

    def edit():
        services.return_to_draft(doc, user=w.clerk2)
        services.update_sales_document(doc, user=w.clerk2, notes_en="changed", notes_ar="تغير")
        services.submit_sales_document(doc, user=w.clerk2)

    with _race_on_slow_clock("invoicing.approve_sales_document", edit), pytest.raises(access.AccessDenied) as exc:
        approve(w, doc)
    assert exc.value.code == "grant_document_changed"
    doc.refresh_from_db()
    assert (doc.status, doc.number) == ("submitted", "")
    assert final(w, doc).status == "final"  # a fresh approval of the new version issues and posts it


def test_item2_return_to_draft_alone_makes_the_approval_stale(w):
    """The approver's own return to draft and resubmission by staff: no modifier changes, the version does."""
    doc = invoice(w)

    def back_and_forth():
        services.return_to_draft(doc, user=w.clerk)
        services.submit_sales_document(doc, user=w.clerk)

    with _race_on_slow_clock("invoicing.approve_sales_document", back_and_forth), \
            pytest.raises(access.AccessDenied) as exc:
        approve(w, doc)
    assert exc.value.code == "grant_document_changed"


def test_item2_receipt_posting_refused_after_an_application_on_a_slow_clock(w):
    inv = final(w, invoice(w))
    r = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="1150.00")
    with _race_on_slow_clock("invoicing.post_receipt",
                             lambda: services.add_receipt_application(r, inv, "1150.00", user=w.clerk)), \
            pytest.raises(access.AccessDenied) as exc:
        services.post_receipt(r, user=w.clerk2)
    assert exc.value.code == "grant_document_changed"
    r.refresh_from_db()
    assert (r.status, r.number) == ("draft", "")
    assert services.post_receipt(r, user=w.clerk2).status == "posted"


@pytest.mark.parametrize("kind", ["bill", "payment"])
def test_item2_bill_and_payment_approval_refused_after_submission_on_a_slow_clock(w, kind):
    if kind == "bill":
        doc, permission, approve_ = bill(w, submit=False), "invoicing.approve_bill", services.approve_purchase_document
        submit = services.submit_purchase_document
    else:
        doc = services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 25), amount="100")
        permission, approve_, submit = ("invoicing.approve_vendor_payment", services.approve_vendor_payment,
                                        services.submit_vendor_payment)
    with _race_on_slow_clock(permission, lambda: submit(doc, user=w.ap)), pytest.raises(access.AccessDenied) as exc:
        approve_(doc, user=w.cfo)
    assert exc.value.code == "grant_document_changed"
    doc.refresh_from_db()
    assert (doc.status, doc.number, doc.journal_entry_id) == ("submitted", "", None)


def test_item2_every_change_of_a_draft_bumps_the_version(w):
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10),
                                         supply_date=date(2027, 1, 10))
    versions = [doc.version]

    def seen():
        versions.append(type(doc).objects.get(pk=doc.pk).version)

    line = services.add_sales_line(doc, user=w.clerk, income_type="rent_commercial", quantity="1", unit_price="10",
                                   description_ar="بند")
    seen()
    services.add_sales_line(doc, user=w.clerk, income_type="rent_commercial", quantity="1", unit_price="20",
                            description_ar="بند")
    seen()
    services.delete_sales_line(line, user=w.clerk)
    seen()
    services.update_sales_document(doc, user=w.clerk, notes_en="x", notes_ar="س")
    seen()
    services.submit_sales_document(doc, user=w.clerk)
    seen()
    services.return_to_draft(doc, user=w.clerk)
    seen()
    assert versions == sorted(set(versions)) and len(versions) == 7  # strictly increasing


@postgres_only
def test_item2_the_database_refuses_a_version_that_goes_down(w):
    doc = invoice(w)
    assert doc.version > 0
    with pytest.raises(DatabaseError, match="version only goes up"):
        _raw("UPDATE invoicing_salesdocument SET status = 'draft', version = 0 WHERE id = %s", [doc.pk])
    b = bill(w, submit=False)
    with pytest.raises(DatabaseError, match="version only goes up"):
        _raw("UPDATE invoicing_purchasedocument SET version = 0 WHERE id = %s", [b.pk])


# --- item 7: the resubmission's ZATCA hand-off runs only after commit (R40, R79) ----------------------

def _never_handed_off(w):
    with mock.patch.object(w.zatca, "submit_document", side_effect=RuntimeError("connection reset")):
        with pytest.raises(RuntimeError):
            approve(w, invoice(w))
    doc = SalesDocument.objects.get(status="issued")
    assert doc.zatca_status == "not_submitted"
    return doc


def _resubmit_logs(user):
    return AccessLog.objects.filter(actor=user, details__action="zatca_resubmit").count()


def test_item7_resubmit_inside_an_outer_transaction_calls_the_port_only_at_commit(w, django_capture_on_commit_callbacks):
    doc = _never_handed_off(w)
    sent = len(w.zatca.submitted)
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with transaction.atomic():  # the caller's own transaction
            returned = services.resubmit_to_zatca(doc, user=w.queue)
            assert returned.zatca_status == "not_submitted"
            assert len(w.zatca.submitted) == sent  # not before the outer transaction commits
        assert len(w.zatca.submitted) == sent
    assert len(callbacks) == 1
    assert w.zatca.submitted[-1][0] == doc.pk and len(w.zatca.submitted) == sent + 1
    assert SalesDocument.objects.get(pk=doc.pk).zatca_status == "pending"
    assert _resubmit_logs(w.queue) == 1


def test_item7_resubmit_rolled_back_by_the_caller_never_reaches_the_port(w, django_capture_on_commit_callbacks):
    doc = _never_handed_off(w)
    sent = len(w.zatca.submitted)
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(RuntimeError, match="caller fails"), transaction.atomic():
            services.resubmit_to_zatca(doc, user=w.queue)
            raise RuntimeError("caller fails")
    assert callbacks == [] and len(w.zatca.submitted) == sent
    assert _resubmit_logs(w.queue) == 0  # rolled back with the caller: nothing was handed off either
    assert SalesDocument.objects.get(pk=doc.pk).zatca_status == "not_submitted"


def test_item7_a_failing_port_after_commit_keeps_the_logged_resubmission(w, django_capture_on_commit_callbacks):
    doc = _never_handed_off(w)
    with mock.patch.object(w.zatca, "submit_document", side_effect=RuntimeError("still down")):
        with pytest.raises(RuntimeError, match="still down"):
            with django_capture_on_commit_callbacks(execute=True):
                services.resubmit_to_zatca(doc, user=w.queue)
    assert _resubmit_logs(w.queue) == 1
    assert SalesDocument.objects.get(pk=doc.pk).zatca_status == "not_submitted"  # can be resubmitted again
