"""Thin, entity-scoped, read-only admin for payroll (access R2, R24; payroll R2; review item 3).

Every change goes through ``payroll.services``. Lists and objects are scoped by ``core.admin.EntityScopedAdmin`` to
the entities where the user holds the model's view permission (memberships, never Django permissions or
``is_superuser``). Individual pay (inputs, loans) needs ``payroll.view_payslip``; WPS files need
``payroll.generate_wps_file`` and never show the file content or the bank-processed file (they hold names, IDs and
IBANs: downloads go through the logged ``payroll.services.download_wps_file``). Encrypted employer bank details are
not shown. GOSI maternity cases (health data) are not in the admin. Final settlement payments need
``payroll.view_payslip`` (re-review 13).
"""
from django.contrib import admin
from django.db.models import Q

from core import access
from core.admin import EntityScopedAdmin

from . import models


class PayrollAdmin(EntityScopedAdmin):
    access_view_permission = "payroll.view"
    access_change_permission = None

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(models.PayrollSettings)
class PayrollSettingsAdmin(PayrollAdmin):
    list_display = ["entity", "enabled", "salary_bank", "day_basis"]
    exclude = ["wps_bank_code", "employer_bank_iban"]


@admin.register(models.PayrollRun)
class PayrollRunAdmin(PayrollAdmin):
    list_display = ["number", "entity", "period", "payroll_group", "kind", "status", "pay_date"]
    list_filter = ["status", "kind", "payroll_group"]
    exclude = ["employee_notes", "off_cycle_employees"]


@admin.register(models.PayrollPayment)
class PayrollPaymentAdmin(PayrollAdmin):
    """Re-review 13: a final settlement payment is one person's pay: listed and shown only to a holder of
    ``payroll.view_payslip`` in its entity."""

    list_display = ["entity", "kind", "run", "gosi_period", "payment_date", "amount", "currency", "status"]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(~Q(kind=models.PayrollPayment.Kind.FINAL_SETTLEMENT)
                         | Q(entity__in=access.entities_for(request.user, "payroll.view_payslip")))

    def has_view_permission(self, request, obj=None):
        if obj is not None and obj.kind == models.PayrollPayment.Kind.FINAL_SETTLEMENT:
            return access.has(request.user, obj.entity, "payroll.view_payslip")
        return super().has_view_permission(request, obj)


@admin.register(models.MudadSubmission)
class MudadSubmissionAdmin(PayrollAdmin):
    list_display = ["entity", "run", "submitted_on", "reference", "status"]
    exclude = ["justifications"]


@admin.register(models.GosiReconciliation)
class GosiReconciliationAdmin(PayrollAdmin):
    list_display = ["entity", "period", "gosi_invoice_amount", "erp_amount", "difference"]


class IndividualPayAdmin(PayrollAdmin):
    """Records of one person's pay: ``payroll.view_payslip`` in the record's entity."""

    access_view_permission = "payroll.view_payslip"


@admin.register(models.PayrollInput)
class PayrollInputAdmin(IndividualPayAdmin):
    list_display = ["employee", "entity", "period", "kind", "status", "payroll_period"]
    list_filter = ["status", "kind"]


@admin.register(models.EmployeeLoan)
class EmployeeLoanAdmin(IndividualPayAdmin):
    list_display = ["number", "employee", "entity", "kind", "principal", "status"]


@admin.register(models.WpsFile)
class WpsFileAdmin(PayrollAdmin):
    access_view_permission = "payroll.generate_wps_file"
    list_display = ["entity", "run", "format_code", "generated_at", "employee_count", "total_amount", "sha256"]
    exclude = ["content", "bank_processed_content", "exceptions"]
