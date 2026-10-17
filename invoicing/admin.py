"""Thin, entity-scoped, read-only admin for invoicing (access R2, R24).

Every change goes through ``invoicing.services``, which check rights and rules;
the admin only shows records, scoped to the entities where the user holds
``invoicing.view``. Personal data of individual customers (address, IDs, VAT
number) and vendor IBANs are hidden unless the user holds
``invoicing.view_personal_data`` in that entity (R51, PDPL; masking and access
logging per view are not built in slice 1).
"""
from django.contrib import admin

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

PERSONAL_CUSTOMER_FIELDS = (
    "vat_number", "street_en", "street_ar", "building_no", "additional_no", "district_en", "district_ar",
    "city_en", "city_ar", "postal_code",
)
PERSONAL_BUYER_FIELDS = (
    "buyer_vat_number", "buyer_other_id_scheme", "buyer_other_id", "buyer_building_no", "buyer_street_en",
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


class CustomerAdmin(ReadOnlyScopedAdmin):
    list_display = ["code", "name_ar", "name_en", "entity", "customer_kind", "is_vat_registered", "is_active"]
    list_filter = ["customer_kind", "is_active"]
    search_fields = ["code", "name_en", "name_ar"]

    def get_exclude(self, request, obj=None):
        if obj is not None and obj.customer_kind == Customer.Kind.INDIVIDUAL and not _may_see_personal(request, obj.entity):
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


class SalesDocumentAdmin(ReadOnlyScopedAdmin):
    list_display = ["number", "doc_type", "entity", "customer", "issue_date", "currency", "total_gross", "status",
                    "zatca_status"]
    list_filter = ["doc_type", "status", "zatca_status", "zatca_profile"]
    search_fields = ["number", "customer__name_ar", "customer__name_en"]
    inlines = [SalesLineInline]

    def get_exclude(self, request, obj=None):
        if obj is not None and obj.customer.customer_kind == Customer.Kind.INDIVIDUAL and not _may_see_personal(
                request, obj.entity):
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
    list_display = ["number", "kind", "entity", "customer", "date", "amount", "currency", "status"]


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
