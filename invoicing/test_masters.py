"""Parties, reference data, labels and admin (invoicing.md 3.1, 3.2, R1 to R4, R51 admin part, AT-33, AT-36)."""
from datetime import date

import pytest
from django.apps import apps
from django.test import RequestFactory

from core import access
from core.access.testing import make_user, member
from invoicing import services
from invoicing.conftest import ADDRESS, D
from invoicing.models import (
    BUYER_ID_PRIORITY,
    Customer,
    IncomeType,
    InvoicingError,
    NoteReason,
    PaymentMeans,
    VatExemptionReason,
    VatRate,
)

pytestmark = pytest.mark.django_db


def test_vat_rates_are_dated_data():
    s = VatRate.objects.filter(category="S").order_by("valid_from")
    assert [(r.rate, r.valid_from, r.valid_to) for r in s] == [
        (D("5.00"), date(2018, 1, 1), date(2020, 6, 30)), (D("15.00"), date(2020, 7, 1), None)]
    assert set(VatRate.objects.values_list("category", flat=True)) == {"S", "Z", "E", "O"}
    assert services._rate_on("S", date(2020, 7, 1)).rate == D("15.00")
    assert services._rate_on("S", date(2017, 12, 31)) is None


def test_exemption_reasons_cover_z_e_o_with_vatex_codes():
    reasons = VatExemptionReason.objects.all()
    assert all(r.code.startswith("VATEX-SA-") and r.name_en and r.name_ar for r in reasons)
    assert set(reasons.values_list("category", flat=True)) == {"Z", "E", "O"}
    assert VatExemptionReason.objects.get(code="VATEX-SA-30").category == "E"


def test_five_statutory_note_reasons_bilingual():
    assert NoteReason.objects.count() == 5
    assert all(r.name_en and r.name_ar for r in NoteReason.objects.all())


def test_income_types_from_the_spec():
    codes = set(IncomeType.objects.values_list("code", flat=True))
    assert codes == {"rent_commercial", "rent_residential", "management_fee", "service_charge", "service_fee",
                     "secondment", "car_rental", "ip_licence", "other"}  # no dividends (D16)
    residential = IncomeType.objects.get(code="rent_residential")
    assert (residential.default_vat_category, residential.art51_class) == ("E", "exempt")  # OQ-39 default


def test_payment_means_and_buyer_id_priority():
    assert set(PaymentMeans.values) == {"1", "10", "30", "42", "48"}
    assert BUYER_ID_PRIORITY[:3] == ["TIN", "CRN", "MOM"] and BUYER_ID_PRIORITY[-1] == "OTH"


def test_at33_every_field_and_model_has_english_and_arabic_labels():
    for model in apps.get_app_config("invoicing").get_models():
        assert " / " in str(model._meta.verbose_name), model
        for f in model._meta.get_fields():
            if getattr(f, "concrete", False) and not f.auto_created:
                assert " / " in str(f.verbose_name), f"{model.__name__}.{f.name}"


def test_party_rules(w):
    with pytest.raises(InvoicingError) as exc:
        services.create_customer(w.alm, user=w.clerk, code="X1", name_ar="")
    assert "Arabic name" in " ".join(exc.value.messages)
    with pytest.raises(InvoicingError):  # VAT number without registration
        services.create_customer(w.alm, user=w.clerk, code="X2", name_ar="عميل", vat_number="310000000000003")
    with pytest.raises(InvoicingError):  # bad format: must start and end with 3
        services.create_customer(w.alm, user=w.clerk, code="X3", name_ar="عميل", is_vat_registered=True,
                                 vat_number="410000000000004")
    with pytest.raises(InvoicingError):  # bad country
        services.create_customer(w.alm, user=w.clerk, code="X4", name_ar="عميل", country="XX")
    with pytest.raises(InvoicingError):  # Saudi postal code has 5 digits
        services.create_customer(w.alm, user=w.clerk, code="X5", name_ar="عميل", postal_code="123")
    with pytest.raises(InvoicingError):
        services.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="NR", name_ar="مورد", is_resident=False)
    with pytest.raises(InvoicingError):  # entity's own code once per entity
        services.create_customer(w.alm, user=w.clerk, code="C001", name_ar="مكرر")
    customer = Customer(entity=w.alm, code="X6", name_ar="", currency_id="SAR", created_by=w.clerk)
    with pytest.raises(Exception):
        customer.full_clean()


def test_party_rights_follow_roles(w):
    with pytest.raises(access.AccessDenied):  # payables clerk does not manage customers
        services.create_customer(w.alm, user=w.ap, code="X7", name_ar="عميل")
    with pytest.raises(access.AccessDenied):
        services.create_vendor(w.alm, user=w.clerk, vendor_kind="business", code="X8", name_ar="مورد")
    with pytest.raises(access.AccessDenied):  # no right in SUB
        services.create_customer(w.sub, user=w.clerk, code="X9", name_ar="عميل")
    with pytest.raises(InvoicingError):
        services.add_customer_identifier(w.customer, "XYZ", "1", user=w.clerk)


def test_update_customer_records_the_modifier(w):
    c = services.update_customer(w.customer, user=w.accountant, city_ar="مكة")
    assert (c.city_ar, c.modified_by) == ("مكة", w.accountant)


def test_settings_change_needs_the_approver(w):
    with pytest.raises(access.AccessDenied):
        services.update_settings(w.alm, user=w.clerk, segregation_required=False)
    with pytest.raises(InvoicingError):  # the deposit VAT treatment waits for the tax adviser (R66)
        services.update_settings(w.alm, user=w.cfo, deposit_vat_treatment="taxable")
    assert services.update_settings(w.alm, user=w.cfo, auto_issue_from_approved_contract=False).updated_by == w.cfo
    assert services.invoicing_settings(w.alm, user=w.viewer).deposit_vat_treatment is None


def test_billing_hooks_are_not_built(w):
    with pytest.raises(NotImplementedError):
        services.create_from_billing(None, w.clerk)
    with pytest.raises(NotImplementedError):
        services.create_adjustment_from_billing(None, w.clerk)


# --- admin: scoped, read-only, personal data hidden (access R2, R24; invoicing R51 in part) -----------

def _request(user):
    request = RequestFactory().get("/admin/")
    request.user = user
    return request


def test_admin_is_scoped_and_read_only(w):
    from django.contrib import admin

    sub_customer = services.create_customer(w.sub, user=w.sub_clerk, code="S1", name_ar="عميل التابعة", **ADDRESS)
    model_admin = admin.site._registry[Customer]
    qs = model_admin.get_queryset(_request(w.clerk))
    assert w.customer in qs and sub_customer not in qs
    superuser = make_user("root")
    superuser.is_superuser = True
    superuser.save()
    assert not model_admin.get_queryset(_request(superuser)).exists()  # R24: superuser gives nothing
    assert not model_admin.has_change_permission(_request(w.clerk), w.customer)
    assert not model_admin.has_add_permission(_request(w.clerk))
    assert not model_admin.has_delete_permission(_request(w.clerk), w.customer)


def test_admin_hides_personal_data_without_the_right(w):
    from django.contrib import admin

    person = services.create_customer(w.alm, user=w.clerk, code="P1", name_ar="فرد", customer_kind="individual",
                                      **ADDRESS)
    model_admin = admin.site._registry[Customer]
    assert "street_ar" in model_admin.get_exclude(_request(w.viewer), person)  # viewer: no view_personal_data
    assert model_admin.get_exclude(_request(w.clerk), person) == ()
    assert model_admin.get_exclude(_request(w.viewer), w.customer) == ()  # a business is not personal data
    vendor_admin = admin.site._registry[w.vendor.__class__]
    assert "bank_iban" in vendor_admin.get_exclude(_request(w.viewer), w.vendor)


def test_item11_admin_masks_names_of_individuals_without_the_right(w):
    from django.contrib import admin

    from invoicing.conftest import approve, invoice
    from invoicing.models import SalesDocument

    person = services.create_customer(w.alm, user=w.clerk, code="P1", name_ar="فرد معين", name_en="Some Person",
                                      customer_kind="individual", **ADDRESS)
    doc = approve(w, invoice(w, customer=person))
    viewer, clerk = _request(w.viewer), _request(w.clerk)
    customer_admin, sales_admin = admin.site._registry[Customer], admin.site._registry[SalesDocument]
    assert {"name_en", "name_ar"} <= set(customer_admin.get_exclude(viewer, person))
    assert {"buyer_name_en", "buyer_name_ar", "customer"} <= set(sales_admin.get_exclude(viewer, doc))
    assert sales_admin.get_exclude(clerk, doc) == ()
    columns = [c for c in customer_admin.get_list_display(viewer) if callable(c)]
    assert [c(person) for c in columns] == ["*** / مخفي"] * 2
    assert [c(w.customer) for c in columns] == [w.customer.name_ar, w.customer.name_en]  # a business: shown
    assert [c(person) for c in customer_admin.get_list_display(clerk) if callable(c)] == ["فرد معين", "Some Person"]
    doc_column = next(c for c in sales_admin.get_list_display(viewer) if callable(c))
    assert doc_column(doc) == "*** / مخفي"
    for model_admin, model, term in ((customer_admin, Customer, "فرد"), (sales_admin, SalesDocument, "Person")):
        qs, _ = model_admin.get_search_results(viewer, model_admin.get_queryset(viewer), term)
        assert not qs.exists()  # the viewer cannot find the individual by name
        qs, _ = model_admin.get_search_results(clerk, model_admin.get_queryset(clerk), term)
        assert qs.exists()
    qs, _ = customer_admin.get_search_results(viewer, customer_admin.get_queryset(viewer), "P1")
    assert list(qs) == [person]  # found by code, shown masked
