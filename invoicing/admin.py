"""Thin, entity-scoped, read-only admin for invoicing (access R2, R24).

Every change goes through ``invoicing.services``, which check rights and rules;
the admin only shows records, scoped to the entities where the user holds
``invoicing.view``. Personal data of individual customers and vendors (D24; names, address, IDs,
VAT number, also as the buyer snapshot on documents and in list columns) and
vendor IBANs are hidden or masked unless the user holds
``invoicing.view_personal_data`` in that entity (R51, PDPL; review item 11).
Names of individuals are searchable only by users with that right; others
find individuals by code or number. ``Customer.__str__`` never includes an
individual's name, so admin titles, breadcrumbs and history pages show only
the code; list columns use ``Customer.display_name`` after the check. Every page that shows an individual's
personal data unmasked (a change page, or a list whose columns show their names) writes one
``personal_data_viewed`` AccessLog row per entity, listing the records (R51, AT-44, AT-88).
"""
from django.contrib import admin
from django.db.models import Q

from core import access
from core.admin import EntityScopedAdmin

from . import services
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
    """``invoicing.view_personal_data`` in ``entity``, checked once per request and entity (a changelist asks
    for every row)."""
    cache = getattr(request, "_invoicing_personal_data", None)
    if cache is None:
        cache = request._invoicing_personal_data = {}
    key = getattr(entity, "pk", entity)
    if key not in cache:
        cache[key] = access.has(request.user, entity, "invoicing.view_personal_data")
    return cache[key]


def _is_individual(party):
    """A customer or vendor of kind ``individual`` (personal data, R49, R91)."""
    kind = party.customer_kind if isinstance(party, Customer) else party.vendor_kind
    return kind == Customer.Kind.INDIVIDUAL


def _masked_customer(request, customer):
    """True when the party (customer or vendor) is an individual whose personal data the user may not see (R51)."""
    return _is_individual(customer) and not _may_see_personal(request, customer.entity)


_masked_party = _masked_customer


def _shown(request, obj, party):
    """Remember an individual's record shown unmasked on this page (logged once the page is rendered, R51)."""
    if _is_individual(party):
        shown = getattr(request, "_invoicing_personal_shown", None)
        if shown is None:
            shown = request._invoicing_personal_shown = {}
        shown.setdefault(party.entity_id, set()).add((obj._meta.label_lower, obj.pk))


def _log_shown(request):
    shown = getattr(request, "_invoicing_personal_shown", None) or {}
    from core.models import Entity

    for entity_id, records in shown.items():
        services._log_personal_data(request.user, Entity.objects.get(pk=entity_id), sorted(records), "admin")
    request._invoicing_personal_shown = {}


class PersonalViewLogMixin:
    """R51 / AT-44: log every admin page that shows an individual's personal data unmasked."""

    party_path = ""  # "" for the party itself; "customer" / "vendor" for documents

    def _party(self, obj):
        return getattr(obj, self.party_path) if self.party_path else obj

    def change_view(self, request, object_id, form_url="", extra_context=None):
        response = super().change_view(request, object_id, form_url, extra_context)
        obj = self.get_object(request, object_id)
        if obj is not None:
            party = self._party(obj)
            if _is_individual(party) and _may_see_personal(request, party.entity):
                _shown(request, obj, party)
        return self._log_after_render(request, response)

    def changelist_view(self, request, extra_context=None):
        return self._log_after_render(request, super().changelist_view(request, extra_context))

    def _log_after_render(self, request, response):
        if hasattr(response, "add_post_render_callback") and not getattr(response, "is_rendered", True):
            response.add_post_render_callback(lambda r: _log_shown(request))
        else:
            _log_shown(request)
        return response


def _customer_column(request, path, field, label):
    """A list column showing ``field`` of the party at ``path`` (""=the object), masked for individuals."""
    def column(obj):
        party = getattr(obj, path) if path else obj
        if _masked_party(request, party):
            return MASK
        _shown(request, obj, party)
        if field:
            return getattr(party, field)
        return party.display_name(personal=not _is_individual(party) or _may_see_personal(request, party.entity))
    column.short_description = label
    return column


_party_column = _customer_column


class PersonalSearchMixin:
    """Search by name finds individual parties only in entities where the user may see personal data."""

    customer_path = ""  # "" for the party itself, "customer" / "vendor" for documents, receipts and payments
    kind_field = "customer_kind"  # "vendor_kind" for vendors and their documents
    code_lookups = ("code",)

    def get_search_results(self, request, queryset, search_term):
        qs, duplicates = super().get_search_results(request, queryset, search_term)
        if search_term:
            prefix = f"{self.customer_path}__" if self.customer_path else ""
            allowed = access.entities_for(request.user, "invoicing.view_personal_data")
            by_code = Q()
            for lookup in self.code_lookups:
                by_code |= Q(**{f"{lookup}__icontains": search_term})
            qs = qs.exclude(Q(**{f"{prefix}{self.kind_field}": Customer.Kind.INDIVIDUAL})
                            & ~Q(**{f"{prefix}entity__in": allowed}) & ~by_code)
        return qs, duplicates


class CustomerAdmin(PersonalViewLogMixin, PersonalSearchMixin, ReadOnlyScopedAdmin):
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


PERSONAL_VENDOR_FIELDS = PERSONAL_CUSTOMER_FIELDS + ("bank_iban",)


class VendorAdmin(PersonalViewLogMixin, PersonalSearchMixin, ReadOnlyScopedAdmin):
    """Individual vendors are personal data like individual customers (D24, R91, R86)."""

    list_filter = ["vendor_kind", "is_resident", "is_active"]
    search_fields = ["code", "name_en", "name_ar"]
    kind_field = "vendor_kind"

    def get_list_display(self, request):
        return ["code", _party_column(request, "", "name_ar", "Name (Arabic) / الاسم (بالعربية)"),
                _party_column(request, "", "name_en", "Name (English) / الاسم (بالإنجليزية)"), "entity",
                "vendor_kind", "is_resident", "is_vat_registered", "is_active"]

    def get_exclude(self, request, obj=None):
        if obj is not None and _masked_party(request, obj):
            return PERSONAL_VENDOR_FIELDS
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


class SalesDocumentAdmin(PersonalViewLogMixin, PersonalSearchMixin, ReadOnlyScopedAdmin):
    party_path = "customer"
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


class PurchaseDocumentAdmin(PersonalViewLogMixin, PersonalSearchMixin, ReadOnlyScopedAdmin):
    party_path = "vendor"
    list_filter = ["doc_type", "status"]
    search_fields = ["number", "vendor_document_number", "vendor__code", "vendor__name_ar", "vendor__name_en"]
    customer_path = "vendor"
    kind_field = "vendor_kind"
    code_lookups = ("number", "vendor_document_number", "vendor__code")
    inlines = [PurchaseLineInline]

    def get_list_display(self, request):
        return ["number", "doc_type", "entity", _party_column(request, "vendor", "", "Vendor / المورد"),
                "vendor_document_number", "accounting_date", "currency", "total_gross", "status"]

    def get_exclude(self, request, obj=None):  # R91: an individual vendor's bill snapshot is personal data
        if obj is not None and _masked_party(request, obj.vendor):
            return ("vendor", "vendor_vat_number")
        return ()


class ReceiptAdmin(ReadOnlyScopedAdmin):
    def get_list_display(self, request):
        return ["number", "kind", "entity", _customer_column(request, "customer", "", "Customer / العميل"), "date",
                "amount", "currency", "status"]

    def get_exclude(self, request, obj=None):
        return ("customer",) if obj is not None and _masked_customer(request, obj.customer) else ()


class VendorPaymentAdmin(ReadOnlyScopedAdmin):
    def get_list_display(self, request):
        return ["number", "kind", "entity", _party_column(request, "vendor", "", "Vendor / المورد"), "date",
                "amount", "currency", "status"]

    def get_exclude(self, request, obj=None):
        return ("vendor",) if obj is not None and _masked_party(request, obj.vendor) else ()


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
