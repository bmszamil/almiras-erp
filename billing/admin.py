"""Read-only admin for billing records (access R2, R24). Every change goes through billing.services."""
from django.contrib import admin

from core.admin import EntityScopedAdmin

from .models import (
    BillingAdjustment,
    BillingPeriod,
    BillingRun,
    BillingRunItem,
    Contract,
    ContractEvent,
    ContractVersion,
    SecondmentAssignment,
)


class BillingAdmin(EntityScopedAdmin):
    access_view_permission = "billing.view"
    access_change_permission = None  # read-only: services only

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]


@admin.register(Contract)
class ContractAdmin(BillingAdmin):
    list_display = ("number", "entity", "contract_type", "customer", "currency", "start_date", "end_date", "status")
    list_filter = ("status", "contract_type")


@admin.register(ContractVersion)
class ContractVersionAdmin(BillingAdmin):
    list_display = ("contract", "version_no", "kind", "effective_date", "status")


@admin.register(BillingPeriod)
class BillingPeriodAdmin(BillingAdmin):
    list_display = ("contract", "period_start", "period_end", "bill_date", "status", "sales_document")
    list_filter = ("status",)


@admin.register(BillingRun)
class BillingRunAdmin(BillingAdmin):
    list_display = ("entity", "run_date", "trigger", "status", "generated_count", "held_count", "failed_count")


@admin.register(BillingRunItem)
class BillingRunItemAdmin(BillingAdmin):
    list_display = ("run", "billing_period", "outcome", "error_code")


@admin.register(BillingAdjustment)
class BillingAdjustmentAdmin(BillingAdmin):
    list_display = ("contract", "billing_period", "kind", "status", "document")


@admin.register(ContractEvent)
class ContractEventAdmin(BillingAdmin):
    list_display = ("contract", "action", "actor", "at")


@admin.register(SecondmentAssignment)
class SecondmentAssignmentAdmin(BillingAdmin):
    """``employee_ref`` is not shown here (masked, access R29); ``billing.services.view_secondment`` shows it to
    holders of ``billing.view_employee_ref`` and logs the view."""

    list_display = ("contract", "role_title_ar", "start_date", "end_date", "hr_assignment_ref")
    exclude = ("employee_ref",)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields if f.name != "employee_ref"]
