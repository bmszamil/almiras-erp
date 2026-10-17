"""Thin, entity-scoped, read-only admin for invoicing (access R2, R24).

Every change goes through ``invoicing.services``, which check rights and rules;
the admin only shows records, scoped to the entities where the user holds
``invoicing.view``. Personal data of individual customers (names, address, IDs,
VAT number, also as the buyer snapshot on documents and in list columns) and
vendor IBANs are hidden or masked unless the user holds
``invoicing.view_personal_data`` in that entity (R51, PDPL; review item 11).
Names of individuals are searchable only by users with that right; others
find individuals by code or number. Access logging per view is not built in
slice 1.
"""
from django.contrib import admin
from django.db.models import Q

from core import access
from core.admin import EntityScopedAdmin

from .models import (
    CustomerReceipt,
    Customer,
    IncomeType,
    InvoicingSettings,
    NoteReason,
    PayableAllocation,
    PurchaseDocument,
    PurchaseDocumentLine,
    ReceivableAllocation,
    SalesDocument,
    SalesDocumentLine,
    VatExemptionReason,
    VatRate,
    Vendor,
    VendorPayment,
)

MASK = "*** / مخفي"
PERSONAL_CUSTOMER_FIELDS = (
    "name_en", "name_ar", "vat_number", "street_en", "street_ar", "building_no", "additional_no", "district_en", "district_ar",
    "city_en", "city_ar", "postal_code",
)
PERSONAL_BUYER_FIELDS = (
    "customer", "buyer_name_en", "buyer_name_ar", "buyer_vat_number", "buyer_other_id_scheme", "buyer_other_id", "buyer_building_no", "buyer_street_en",
    "buyer_street_ar", "buyer_district_en", "buyer_district_ar", "buyer_additional_no", "buyer_postal_code",
    "buyer_city_en", "buyer_city_ar",
)


class ReadOnlyScopedAdmin(EntityScopedAdmin):
    access_view_permission = "invoicing.view"
    access_change_permission = None  # read-only: services make every change

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


def _may_see_personal(request, entity):
    return access.has(request.user, entity, "invoicing.view_personal_data")


def _masked_customer(request, customer):
    """True when ``customer`` is an individual whose personal data the user may not see (R51)."""
    return customer.customer_kind == Customer.Kind.INDIVIDUAL and not _may_see_personal(request, customer.entity)


def _customer_column(request, path, field, label):
    """A list column showing ``field`` of the customer at ``path`` (""=the object), masked for individuals."""
    def column(obj):
        customer = getattr(obj, path) if path else obj
        if _masked_customer(request, customer):
            return MASK
        return getattr(customer, field) if field else str(customer)
    column.short_description = label
    return column


class PersonalSearchMixin:
    """Search by name finds individual customers only in entities where the user may see personal data."""

    customer_path = ""  # "" for Customer itself, "customer" for documents and receipts
    code_lookups = ("code",)

    def get_search_results(self, request, queryset, search_term):
        qs, duplicates = super().get_search_results(request, queryset, search_term)
        if search_term:
            prefix = f"{self.customer_path}__" if self.customer_path else ""
            allowed = access.entities_for(request.user, "invoicing.view_personal_data")
            by_code = Q()
            for lookup in self.code_lookups:
                by_code |= Q(**{f"{lookup}__icontains": search_term})
            qs = qs.exclude(Q(**{f"{prefix}customer_kind": Customer.Kind.INDIVIDUAL})
                            & ~Q(**{f"{prefix}entity__in": allowed}) & ~by_code)
        return qs, duplicates


class CustomerAdmin(PersonalSearchMixin, ReadOnlyScopedAdmin):
    list_filter = ["customer_kind", "is_active"]
    search_fields = ["code", "name_en", "name_ar"]

    def get_list_display(self, request):
        return ["code", _customer_column(request, "", "name_ar", "Name (Arabic) / الاسم (بالعربية)"),
                _customer_column(request, "", "name_en", "Name (English) / الاسم (بالإنجليزية)"), "entity",
                "customer_kind", "is_vat_registered", "is_active"]

    def get_exclude(self, request, obj=None):
        if obj is not None and _masked_customer(request, obj):
            return PERSONAL_CUSTOMER_FIELDS
        return ()


class VendorAdmin(ReadOnlyScopedAdmin):
    list_display = ["code", "name_ar", "name_en", "entity", "is_resident", "is_vat_registered", "is_active"]
    search_fields = ["code", "name_en", "name_ar"]

    def get_exclude(self, request, obj=None):
        if obj is None or not _may_see_personal(request, obj.entity):
            return ("bank_iban",)
        return ()


class SalesLineInline(admin.TabularInline):
    model = SalesDocumentLine
    extra = 0
    can_delete = False
    exclude = ["entity"]

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class SalesDocumentAdmin(PersonalSearchMixin, ReadOnlyScopedAdmin):
    list_filter = ["doc_type", "status", "zatca_status", "zatca_profile"]
    search_fields = ["number", "customer__code", "customer__name_ar", "customer__name_en"]
    customer_path = "customer"
    code_lookups = ("number", "customer__code")
    inlines = [SalesLineInline]

    def get_list_display(self, request):
        return ["number", "doc_type", "entity", _customer_column(request, "customer", "", "Customer / العميل"),
                "issue_date", "currency", "total_gross", "status", "zatca_status"]

    def get_exclude(self, request, obj=None):
        if obj is not None and _masked_customer(request, obj.customer):
            return PERSONAL_BUYER_FIELDS
        return ()


class PurchaseLineInline(SalesLineInline):
    model = PurchaseDocumentLine


class PurchaseDocumentAdmin(ReadOnlyScopedAdmin):
    list_display = ["number", "doc_type", "entity", "vendor", "vendor_document_number", "accounting_date", "currency",
                    "total_gross", "status"]
    list_filter = ["doc_type", "status"]
    search_fields = ["number", "vendor_document_number", "vendor__name_ar", "vendor__name_en"]
    inlines = [PurchaseLineInline]


class ReceiptAdmin(ReadOnlyScopedAdmin):
    def get_list_display(self, request):
        return ["number", "kind", "entity", _customer_column(request, "customer", "", "Customer / العميل"), "date",
                "amount", "currency", "status"]

    def get_exclude(self, request, obj=None):
        return ("customer",) if obj is not None and _masked_customer(request, obj.customer) else ()


class VendorPaymentAdmin(ReadOnlyScopedAdmin):
    list_display = ["number", "kind", "entity", "vendor", "date", "amount", "currency", "status"]


class AllocationAdmin(ReadOnlyScopedAdmin):
    list_display = ["entity", "target", "amount", "date", "via_settlement", "created_by"]
    status_field = None


class SettingsAdmin(ReadOnlyScopedAdmin):
    list_display = ["entity", "segregation_required", "auto_issue_from_approved_contract", "deposit_vat_treatment"]
    status_field = None


class ReferenceAdmin(admin.ModelAdmin):
    """Statutory reference data changes by migration only (rates are data with effective dates)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(Customer, CustomerAdmin)
admin.site.register(Vendor, VendorAdmin)
admin.site.register(SalesDocument, SalesDocumentAdmin)
admin.site.register(PurchaseDocument, PurchaseDocumentAdmin)
admin.site.register(CustomerReceipt, ReceiptAdmin)
admin.site.register(VendorPayment, VendorPaymentAdmin)
admin.site.register(ReceivableAllocation, AllocationAdmin)
admin.site.register(PayableAllocation, AllocationAdmin)
admin.site.register(InvoicingSettings, SettingsAdmin)
for model in (VatRate, VatExemptionReason, NoteReason, IncomeType):
    admin.site.register(model, ReferenceAdmin)
