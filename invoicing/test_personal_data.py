"""Personal data (PDPL; invoicing R49 to R53, R86, R91; AT-43, AT-44, AT-88, AT-94). Made-up individuals only."""
from datetime import date

import pytest
from django.contrib import admin
from django.core.management import call_command
from django.test import RequestFactory, override_settings

from core.access.models import AccessLog
from core.access.testing import make_agent
from invoicing import services
from invoicing.conftest import ADDRESS, D, final, invoice
from invoicing.testing import approval_day
from invoicing.models import Customer, CustomerIdentifier, InvoicingError, SalesDocument, Vendor

pytestmark = pytest.mark.django_db


def person(w, code="P1", **fields):
    c = services.create_customer(w.alm, user=w.clerk, code=code, name_ar="شخص تجريبي", name_en="Test Person",
                                 customer_kind="individual", **{**ADDRESS, **fields})
    services.add_customer_identifier(c, "NAT", f"10000000{code[-1]}", user=w.clerk)
    return c


def logs(event=AccessLog.Event.PERSONAL_DATA_VIEWED):
    return list(AccessLog.objects.filter(event=event).order_by("pk"))


def test_at44_viewer_sees_masks_accountant_sees_and_is_logged(w):
    p = person(w)
    masked = services.view_customer(p, user=w.viewer)
    assert masked["masked"] and masked["name_ar"] == services.MASK and masked["identifiers"] == services.MASK
    assert not logs()
    shown = services.view_customer(p, user=w.accountant)
    assert shown["name_en"] == "Test Person" and shown["identifiers"] == [{"scheme": "NAT", "value": "100000001"}]
    (row,) = logs()
    assert (row.actor, row.entity, row.permission) == (w.accountant, w.alm, "invoicing.view_personal_data")
    assert row.details == {"records": [["invoicing.customer", p.pk]], "how": "view"} and row.at is not None
    exported = services.export_customers(w.alm, user=w.accountant)
    assert {r["code"]: r["masked"] for r in exported} == {"C001": False, "P1": False}
    (export,) = logs(AccessLog.Event.PERSONAL_DATA_EXPORTED)
    assert export.details["records"] == [["invoicing.customer", p.pk]] and export.details["how"] == "export"
    viewer_export = services.export_customers(w.alm, user=w.viewer)
    assert {r["code"]: r["masked"] for r in viewer_export} == {"C001": False, "P1": True}
    assert len(logs(AccessLog.Event.PERSONAL_DATA_EXPORTED)) == 1  # nothing unmasked, nothing logged


def test_r51_business_parties_are_not_logged_and_agents_always_see_masks(w):
    services.view_customer(w.customer, user=w.accountant)
    assert not logs()
    p = person(w)
    assert services._may_see_personal(make_agent("reader"), w.alm) is False  # agents: never unmasked (R51)
    assert not logs()


def test_r91_individual_vendor_and_iban(w):
    v = services.create_vendor(w.alm, user=w.ap, vendor_kind="individual", code="VI1", name_ar="مورد فرد",
                               bank_iban="SA0000000000000000000001")
    assert services.view_vendor(v, user=w.viewer)["bank_iban"] == services.MASK
    assert services.view_vendor(v, user=w.ap)["bank_iban"] == "SA0000000000000000000001"
    (row,) = logs()
    assert row.details["records"] == [["invoicing.vendor", v.pk]]
    services.export_vendors(w.alm, user=w.ap)
    assert logs(AccessLog.Event.PERSONAL_DATA_EXPORTED)[0].details["records"] == [["invoicing.vendor", v.pk]]


def test_r51_buyer_snapshot_on_a_document(w):
    doc = final(w, invoice(w, customer=person(w)))
    assert services.view_sales_document(doc, user=w.viewer)["buyer_name_ar"] == services.MASK
    assert services.view_sales_document(doc, user=w.clerk)["buyer_name_ar"] == "شخص تجريبي"
    assert logs()[-1].details == {"records": [["invoicing.salesdocument", doc.pk]], "how": "view"}


def _request(user):
    request = RequestFactory().get("/admin/")
    request.user = user
    return request


def test_at88_admin_pages_log_unmasked_individuals_only(w):
    p = person(w)
    customer_admin = admin.site._registry[Customer]
    for user in (w.viewer, w.clerk):
        response = customer_admin.changelist_view(_request(user))
        response.render()
        html = response.content.decode()
        assert ("Test Person" in html) == (user == w.clerk)
        assert "شركة المشتري التجريبية" in html  # a business name is shown to both
    (row,) = logs()
    assert row.actor == w.clerk and row.details == {"records": [["invoicing.customer", p.pk]], "how": "admin"}
    response = customer_admin.change_view(_request(w.clerk), str(p.pk))
    response.render()
    assert len(logs()) == 2
    customer_admin.change_view(_request(w.viewer), str(p.pk)).render()
    assert len(logs()) == 2


def test_at94_admin_vendor_lists_mask_and_log(w):
    v = services.create_vendor(w.alm, user=w.ap, vendor_kind="individual", code="VI2", name_ar="مورد فرد",
                               name_en="Individual Seller")
    vendor_admin = admin.site._registry[Vendor]
    viewer_page = vendor_admin.changelist_view(_request(w.viewer))
    viewer_page.render()
    assert "Individual Seller" not in viewer_page.content.decode() and not logs()
    page = vendor_admin.changelist_view(_request(w.ap))
    page.render()
    assert "Individual Seller" in page.content.decode()
    assert logs()[0].details["records"] == [["invoicing.vendor", v.pk]]


# --- R52 / AT-43: anonymisation -----------------------------------------------------------------------------------

def _two_documents(w):
    a, b = person(w, "P1"), person(w, "P2")
    early = final(w, invoice(w, customer=a, issue_date=date(2027, 1, 10)))  # rent (real estate, 15 years): kept until 2042-03-31
    late = final(w, invoice(w, customer=a, issue_date=date(2027, 4, 10)))  # until 2042-06-30
    only = final(w, invoice(w, customer=b, issue_date=date(2027, 1, 12)))
    assert (early.retention_until, late.retention_until) == (date(2042, 3, 31), date(2042, 6, 30))
    return a, b, early, late, only


def test_at43_default_is_a_dry_run_and_applying_is_disabled(w):
    a, b, early, late, only = _two_documents(w)
    result = services.anonymise_expired_personal_data(today=date(2042, 4, 1))
    assert (result["documents"], result["customers"], result["applied"]) == ([early.pk, only.pk], [b.pk], False)
    with pytest.raises(InvoicingError) as exc:
        services.anonymise_expired_personal_data(today=date(2042, 4, 1), apply=True)
    assert exc.value.code == "anonymisation_disabled"
    assert SalesDocument.objects.get(pk=early.pk).buyer_name_ar == "شخص تجريبي"
    call_command("invoicing_anonymise", "--date", "2042-04-01")  # the command is a dry run too


@override_settings(INVOICING_ANONYMISATION_ENABLED=True)
def test_at43_expired_individuals_are_anonymised_amounts_and_zatca_data_kept(w):
    a, b, early, late, only = _two_documents(w)
    before = SalesDocument.objects.get(pk=early.pk)
    entry_lines = list(before.journal_entry.lines.values_list("account__code", "debit", "credit"))
    with pytest.raises(InvoicingError):  # never with a date ahead of the clock
        services.anonymise_expired_personal_data(today=date(2042, 4, 1), apply=True)
    with approval_day(date(2042, 4, 1)):  # the clock of the job (the database compares it with retention_until)
        services.anonymise_expired_personal_data(today=date(2042, 4, 1), apply=True)
    doc = SalesDocument.objects.get(pk=early.pk)
    assert (doc.buyer_name_ar, doc.buyer_name_en, doc.buyer_street_ar, doc.buyer_other_id) == (
        "مجهّل", "ANONYMISED", "مجهّل", "")
    assert doc.anonymised_at is not None
    for name in ("number", "total_net", "total_vat", "total_gross", "zatca_hash", "zatca_qr", "issue_date", "uuid"):
        assert getattr(doc, name) == getattr(before, name)
    assert list(doc.journal_entry.lines.values_list("account__code", "debit", "credit")) == entry_lines
    assert SalesDocument.objects.get(pk=late.pk).buyer_name_ar == "شخص تجريبي"  # retention not over yet
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.name_ar == "شخص تجريبي" and a.anonymised_at is None  # still has a document under retention
    assert (b.name_ar, b.name_en, b.is_active) == ("مجهّل", "ANONYMISED", False) and b.anonymised_at
    assert not CustomerIdentifier.objects.filter(customer=b).exists()
    assert services.anonymise_expired_personal_data(today=date(2042, 4, 1))["documents"] == []  # done once
    assert SalesDocument.objects.get(pk=only.pk).total_gross == D("1150.000")


def test_r52_anonymised_documents_still_never_change(w):
    _, _, early, _, _ = _two_documents(w)
    doc = SalesDocument.objects.get(pk=early.pk)
    doc.buyer_name_ar = "x"
    with pytest.raises(InvoicingError):
        doc.save()
