"""Project-owner decisions of 2026-10-17 in invoicing (D24, D26, D27, D30; R91 to R95; access R46, R50).

Acceptance tests AT-49, AT-73 (amended), AT-93 to AT-101 and access AT-53. All identifiers are made up.
"""
from datetime import date

import pytest

from core import access
from core.access.models import AccessLog
from core.access.testing import make_user, member
from invoicing import services
from invoicing.conftest import ADDRESS, D, final, invoice
from invoicing.models import InvoicingError
from invoicing.test_purchases import bill

pytestmark = pytest.mark.django_db


def receipt(w, amount="1150.00", *, user=None, entity=None, customer=None, applications=(), **fields):
    user = user or w.clerk
    r = services.create_receipt(entity or w.alm, user=user, customer=customer or w.customer, date=date(2027, 1, 20),
                                amount=amount, **fields)
    for target, value in applications:
        services.add_receipt_application(r, target, value, user=user)
    return r


# --- D26 / access R46: receipts posted by their recorder (AT-49, AT-73) -----------------------------------

def test_access_at49_recorder_posts_receipt_but_not_refund_bill_or_other_entity(w):
    services.update_settings(w.alm, user=w.cfo, segregation_required=False)
    r = receipt(w, "500.00")
    posted = services.post_receipt(r, user=w.clerk)  # K posts R
    assert (posted.status, posted.posted_by) == ("posted", w.clerk)
    row = AccessLog.objects.filter(event="permission_granted", actor=w.clerk, permission="invoicing.post_receipt",
                                   details__document=["invoicing.customerreceipt", r.pk]).get()
    assert row.details["self_post"] is True and row.grant_use is not None
    refund = receipt(w, "50.00", kind="refund")
    with pytest.raises(access.AccessDenied) as exc:  # K posts F
        services.post_receipt(refund, user=w.clerk)
    assert exc.value.code == "self_approval"
    c = make_user("c_both")
    member(c, w.alm, "accountant")
    member(c, w.alm, "approver")
    b = bill(w, user=c)  # C creates and submits B
    with pytest.raises(access.AccessDenied) as exc:  # despite segregation_required = False (ledger R35)
        services.approve_purchase_document(b, user=c)
    assert exc.value.code == "self_approval"
    sub_customer = services.create_customer(w.sub, user=w.sub_clerk, code="S1", name_ar="عميل التابعة",
                                            is_vat_registered=True, vat_number="330000000000003", **ADDRESS)
    sub_receipt = receipt(w, "10.00", user=w.sub_clerk, entity=w.sub, customer=sub_customer)
    with pytest.raises(access.AccessDenied) as exc:  # K posts a receipt of SUB
        services.post_receipt(sub_receipt, user=w.clerk)
    assert exc.value.code == "no_membership"
    # A second person still may post a receipt (the recorder is allowed, not required).
    assert services.post_receipt(receipt(w, "20.00"), user=w.clerk2).posted_by == w.clerk2


def test_at73_recording_needs_allocate_and_post_receipt_then_the_recorder_posts(w):
    u = make_user("allocate_only")
    member(u, w.alm, "payables_clerk")  # invoicing.allocate, no invoicing.post_receipt
    with pytest.raises(access.AccessDenied):
        receipt(w, user=u)
    inv = final(w, invoice(w))
    r = receipt(w, applications=[(inv, "1150.00")])  # K records it
    posted = services.post_receipt(r, user=w.clerk)  # K posts E5
    lines = sorted((l.account.code, l.debit, l.credit) for l in posted.journal_entry.lines.select_related("account"))
    assert lines == [("1100", D("1150.00"), D("0")), ("1200", D("0"), D("1150.00"))]
    with pytest.raises(InvoicingError):  # L then finds it posted
        services.post_receipt(r, user=w.clerk2)
    with pytest.raises(InvoicingError) as exc:
        receipt(w, purpose="advance")
    assert exc.value.code == "purpose"


# --- D28 / access A19, R50: zatca_admin as an add-on membership (access AT-53) ------------------------------

def _failed_hand_offs(w, count):
    """``count`` issued documents whose after-commit hand-off raised (still ``not_submitted``)."""
    from unittest import mock

    from invoicing.conftest import approve
    from invoicing.models import SalesDocument

    docs = []
    for _ in range(count):
        with mock.patch.object(w.zatca, "submit_document", side_effect=RuntimeError("connection reset")):
            with pytest.raises(RuntimeError):
                approve(w, invoice(w))
        docs.append(SalesDocument.objects.filter(status="issued").order_by("-pk").first())
    assert all(d.zatca_status == "not_submitted" for d in docs)
    return docs


def test_access_at53_zatca_admin_is_an_add_on_membership_checked_on_its_own_role(w):
    from django.utils import timezone

    from core.access import services as access_services
    from core.access.models import Membership, Role
    from invoicing.test_review_fixes import resubmit

    mp = make_user("mp")
    member(mp, w.alm, "managing_partner")
    a, c = make_user("a_acc"), make_user("c_cfo")
    now = timezone.now()
    for user, roles in ((a, ("accountant", "zatca_admin")), (c, ("approver", "zatca_admin", "receivables_clerk"))):
        for code in roles:  # granted by the MP as separate memberships in the same entity (R9 allows it)
            access_services.grant_membership(mp, user, w.alm, Role.objects.get(code=code), now, reason=f"{code} role")
    assert set(Membership.objects.active().filter(user=a, entity=w.alm).values_list("role__code", flat=True)) == {
        "accountant", "zatca_admin"}
    first, second, third = _failed_hand_offs(w, 3)
    assert resubmit(first, a).zatca_status == "pending"
    assert resubmit(second, c).zatca_status == "pending"
    for user in (a, c):
        assert AccessLog.objects.filter(actor=user, details__action="zatca_resubmit").count() == 1
    access_services.revoke_membership(mp, Membership.objects.get(user=a, role__code="zatca_admin"), "Duty ended")
    with pytest.raises(access.AccessDenied) as exc:  # still an accountant (who holds zatca.retry_failed): refused
        services.resubmit_to_zatca(third, user=a)
    assert exc.value.code == "no_membership"
    assert access.has(a, w.alm, "zatca.retry_failed")  # OQ-27 default: accountants keep the catalogue right
    own = invoice(w, user=c)  # C created and submitted it
    with pytest.raises(access.AccessDenied) as exc:  # R14 applies to the person, whatever roles they hold
        services.approve_sales_document(own, user=c)
    assert exc.value.code == "self_approval"


# --- D24 / R91: vendor kind and masked individual vendors (AT-93, AT-94) ------------------------------------

VENDOR_NAMES = ("Test Supplier Person", "مورد فرد")


def _individual_vendor(w, entity=None, user=None):
    return services.create_vendor(entity or w.alm, user=user or w.ap, vendor_kind="individual", code="IV1",
                                  name_en="Test Supplier Person", name_ar="مورد فرد")


def test_at93_vendor_kind_is_required_in_every_entity(w):
    from core.access.testing import member as m

    for entity, user in ((w.alm, w.ap), (w.sub, make_user("sub_ap"))):
        if entity is w.sub:
            m(user, w.sub, "payables_clerk")
        with pytest.raises(InvoicingError) as exc:  # no vendor_kind
            services.create_vendor(entity, user=user, code="NK", name_ar="مورد بلا نوع")
        assert "vendor kind" in " ".join(exc.value.messages) and "نوع المورد" in " ".join(exc.value.messages)
        with pytest.raises(InvoicingError):
            services.create_vendor(entity, user=user, vendor_kind="company", code="NK", name_ar="مورد")
        saved = _individual_vendor(w, entity, user)
        assert (saved.vendor_kind, saved.entity) == ("individual", entity)


def test_at94_individual_vendor_names_are_masked_without_the_right(w, client):
    from invoicing.test_verification_fixes import _page

    person = _individual_vendor(w)
    assert str(person) == "IV1" and person.display_name(personal=True) == "IV1 — مورد فرد"
    assert str(w.vendor) == "V001 — المورد التجريبي"  # a business vendor is not personal data
    doc = services.approve_purchase_document(bill(w, vendor=person, vat="0", gross="1000.00"), user=w.cfo)
    urls = (f"/admin/invoicing/vendor/{person.pk}/change/", f"/admin/invoicing/vendor/{person.pk}/history/",
            "/admin/invoicing/vendor/", f"/admin/invoicing/purchasedocument/{doc.pk}/change/",
            f"/admin/invoicing/purchasedocument/{doc.pk}/history/", "/admin/invoicing/purchasedocument/")
    assert not access.has(w.viewer, w.alm, "invoicing.view_personal_data")
    for url in urls:
        body = _page(client, w.viewer, url)
        assert not any(name in body for name in VENDOR_NAMES), url
    assert "V001" in _page(client, w.viewer, "/admin/invoicing/vendor/")  # business names stay visible
    assert "المورد التجريبي" in _page(client, w.viewer, "/admin/invoicing/vendor/")
    assert access.has(w.ap, w.alm, "invoicing.view_personal_data")
    for url in (f"/admin/invoicing/vendor/{person.pk}/change/", "/admin/invoicing/vendor/",
                "/admin/invoicing/purchasedocument/"):
        assert "مورد فرد" in _page(client, w.ap, url), url
    client.force_login(w.viewer)  # search by an individual's name finds nothing without the right
    assert "IV1" not in client.get("/admin/invoicing/vendor/", {"q": "مورد فرد"}).content.decode()
    assert "IV1" in client.get("/admin/invoicing/vendor/", {"q": "IV1"}).content.decode()


@pytest.mark.django_db(transaction=True)
def test_at93_migration_makes_existing_vendors_business():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from core.currency_seed import apply_currency_seed

    def migrate(targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    leaf = MigrationExecutor(connection).loader.graph.leaf_nodes()
    old = migrate([("invoicing", "0010_approved_version")])
    try:
        apply_currency_seed(old.get_model("core", "Currency"))
        Entity = old.get_model("core", "Entity")
        User = old.get_model("auth", "User")
        alm = Entity.objects.create(code="ALM", name_en="ALM", name_ar="كيان", consolidation="parent",
                                    base_currency_id="SAR")
        user = User.objects.create(username="migration.user")
        old.get_model("invoicing", "Vendor").objects.create(entity=alm, code="OLD", name_ar="مورد قديم",
                                                            currency_id="SAR", created_by=user)
    finally:
        new = migrate(leaf)
    assert new.get_model("invoicing", "Vendor").objects.get(code="OLD").vendor_kind == "business"
