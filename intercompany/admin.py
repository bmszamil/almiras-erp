"""Intercompany admin: entity-scoped (access.admin_scoping), audited, and read-only for documents.

Documents change only through intercompany.services (API or the actions below), so the admin never
bypasses locks, numbering or approvals. Service types are group reference data: editable by holders of
intercompany.servicetype.manage in a root (parent) entity.
"""
from django.contrib import admin, messages

from access import services as access
from access.admin_scoping import ReadOnlyScopedAdmin, ScopedAdmin, admin_ctx, setup_mode

from . import services
from .models import (
    AgreementRate, AllocationDriver, BillableItem, BillingRun, CostPool, CostPoolPeriod, IntercompanyBill, IntercompanyBillLine,
    IntercompanyInvoice, InvoiceLine, ServiceAgreement, ServiceType, TimesheetEntry, VatRate,
)


def _run_action(modeladmin, request, queryset, fn, label):
    for obj in queryset:
        try:
            fn(obj)
            modeladmin.message_user(request, f"{label}: {obj}")
        except Exception as exc:  # show the rule that failed
            modeladmin.message_user(request, f"{obj}: {exc}", messages.ERROR)


class _ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_view_permission(self, request, obj=None):
        return True  # the parent admin already scoped the document

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ServiceType)
class ServiceTypeAdmin(ScopedAdmin):
    view_perm = "intercompany.agreement.view"
    entity_field = None
    list_display = ["code", "name_en", "name_ar", "category", "income_account_code", "expense_account_code", "is_active"]

    def _manager(self, request):
        return setup_mode(request) or access.entities_with_perm(
            request.user, "intercompany.servicetype.manage").filter(parent__isnull=True).exists()

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj) or self._manager(request)

    def scope_queryset(self, request, qs):
        return qs if (self.erp_entities(request).exists() or self._manager(request)) else qs.none()

    def has_add_permission(self, request, *args):
        return self._manager(request)

    def has_change_permission(self, request, obj=None):
        return self._manager(request)

    def get_readonly_fields(self, request, obj=None):
        return ["code"] if obj is not None else []


@admin.register(VatRate)
class VatRateAdmin(ReadOnlyScopedAdmin):
    view_perm = "intercompany.invoice.view"
    entity_field = None
    list_display = ["country", "rate", "valid_from", "valid_to"]


class RateInline(_ReadOnlyInline):
    model = AgreementRate
    fields = ["role_en", "role_ar", "hourly_rate", "valid_from", "valid_to"]


@admin.register(ServiceAgreement)
class ServiceAgreementAdmin(ReadOnlyScopedAdmin):
    view_perm = "intercompany.agreement.view"
    list_display = ["number", "entity", "counterparty", "service_type", "pricing_method", "billing_frequency",
                    "status", "source"]
    list_filter = ["status", "pricing_method", "billing_frequency"]
    search_fields = ["number", "description_en", "description_ar"]
    inlines = [RateInline]
    actions = ["submit_selected"]

    def scope_queryset(self, request, qs):
        return qs.filter(pk__in=services.visible_agreements(request.user).values("pk"))

    def has_view_permission(self, request, obj=None):
        if obj is None and not setup_mode(request):
            return request.user.is_authenticated and (
                self.erp_entities(request).exists()
                or access.entities_with_perm(request.user, "intercompany.agreement.create").exists())
        return super().has_view_permission(request, obj)

    @admin.action(description="Submit selected for approval / تقديم للاعتماد")
    def submit_selected(self, request, queryset):
        from approvals.services import submit

        _run_action(self, request, queryset, lambda a: submit(a, request.user, ctx=admin_ctx(request)), "Submitted")


@admin.register(TimesheetEntry)
class TimesheetEntryAdmin(ReadOnlyScopedAdmin):
    view_perm = "intercompany.agreement.view"
    list_display = ["entity", "agreement", "user", "date", "hours", "status", "source"]
    list_filter = ["status"]
    actions = ["submit_selected", "approve_selected"]

    def scope_queryset(self, request, qs):
        return qs.filter(pk__in=services.visible_timesheets(request.user).values("pk"))

    def has_view_permission(self, request, obj=None):
        if obj is None and not setup_mode(request):
            return request.user.is_authenticated and (
                self.erp_entities(request).exists()
                or access.entities_with_perm(request.user, "intercompany.timesheet.create").exists())
        return super().has_view_permission(request, obj)

    @admin.action(description="Submit selected entries / تقديم السجلات")
    def submit_selected(self, request, queryset):
        _run_action(self, request, queryset,
                    lambda e: services.submit_timesheet(request.user, e, ctx=admin_ctx(request)), "Submitted")

    @admin.action(description="Approve selected entries / اعتماد السجلات")
    def approve_selected(self, request, queryset):
        _run_action(self, request, queryset,
                    lambda e: services.approve_timesheet(request.user, e, ctx=admin_ctx(request)), "Approved")


@admin.register(CostPool)
class CostPoolAdmin(ReadOnlyScopedAdmin):
    view_perm = "intercompany.agreement.view"
    list_display = ["entity", "code", "name_en", "name_ar", "is_active"]


@admin.register(CostPoolPeriod)
class CostPoolPeriodAdmin(ReadOnlyScopedAdmin):
    view_perm = "intercompany.agreement.view"
    list_display = ["entity", "cost_pool", "period", "amount", "locked", "computed_at"]


@admin.register(AllocationDriver)
class AllocationDriverAdmin(ReadOnlyScopedAdmin):
    view_perm = "intercompany.agreement.view"
    list_display = ["entity", "cost_pool", "period", "basis", "target_entity", "value"]


@admin.register(BillableItem)
class BillableItemAdmin(ReadOnlyScopedAdmin):
    view_perm = "intercompany.agreement.view"
    list_display = ["entity", "agreement", "period_start", "period_end", "quantity", "unit_price", "status",
                    "source_type", "source_ref"]


@admin.register(BillingRun)
class BillingRunAdmin(ReadOnlyScopedAdmin):
    view_perm = "intercompany.invoice.view"
    list_display = ["entity", "period", "run_date", "status", "invoices_created", "created_by"]


class InvoiceLineInline(_ReadOnlyInline):
    model = InvoiceLine
    fields = ["description_en", "description_ar", "quantity", "unit_price", "net_amount", "vat_category", "vat_rate",
              "vat_amount", "income_account", "pricing_basis"]


@admin.register(IntercompanyInvoice)
class IntercompanyInvoiceAdmin(ReadOnlyScopedAdmin):
    view_perm = "intercompany.invoice.view"
    list_display = ["number", "kind", "entity", "counterparty", "billing_period", "issue_date", "gross_total",
                    "currency", "status", "zatca_status", "source"]
    list_filter = ["status", "kind", "zatca_status"]
    search_fields = ["number", "agreement__number"]
    inlines = [InvoiceLineInline]
    actions = ["submit_selected", "cancel_selected"]

    def scope_queryset(self, request, qs):
        return qs.filter(pk__in=services.visible_invoices(request.user).values("pk"))

    def has_view_permission(self, request, obj=None):
        if obj is None and not setup_mode(request):
            return request.user.is_authenticated and (
                self.erp_entities(request).exists()
                or access.entities_with_perm(request.user, "intercompany.invoice.create").exists())
        return super().has_view_permission(request, obj)

    @admin.action(description="Submit selected for approval / تقديم للاعتماد")
    def submit_selected(self, request, queryset):
        from approvals.services import submit

        _run_action(self, request, queryset, lambda i: submit(i, request.user, ctx=admin_ctx(request)), "Submitted")

    @admin.action(description="Cancel selected drafts / إلغاء المسودات")
    def cancel_selected(self, request, queryset):
        _run_action(self, request, queryset,
                    lambda i: services.cancel_draft(request.user, i, "admin", ctx=admin_ctx(request)), "Cancelled")


class BillLineInline(_ReadOnlyInline):
    model = IntercompanyBillLine
    fields = ["description_en", "description_ar", "net_amount", "vat_rate", "vat_amount", "expense_account",
              "cost_centre"]


@admin.register(IntercompanyBill)
class IntercompanyBillAdmin(ReadOnlyScopedAdmin):
    """R19: scoped to the receiving entity; the issuer sees bills only with rights on the receiver."""

    view_perm = "intercompany.bill.view"
    list_display = ["number", "kind", "entity", "counterparty", "bill_date", "gross_total", "status"]
    list_filter = ["status", "kind"]
    inlines = [BillLineInline]
    actions = ["submit_selected"]

    @admin.action(description="Submit selected for approval / تقديم للاعتماد")
    def submit_selected(self, request, queryset):
        _run_action(self, request, queryset,
                    lambda b: services.submit_bill(request.user, b, ctx=admin_ctx(request)), "Submitted")
