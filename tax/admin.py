"""Thin, entity-scoped, read-only admin for the VAT slice (access R2, R24). Every change goes through tax.services."""
from django.contrib import admin

from core.admin import EntityScopedAdmin

from .models import ImportVatRecord, TaxAlert, TaxPayment, TaxRefund, VatCorrection, VatReturn, VatReturnSchedule


class ReadOnlyTaxAdmin(EntityScopedAdmin):
    access_view_permission = "tax.view"
    access_change_permission = None

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(VatReturn)
class VatReturnAdmin(ReadOnlyTaxAdmin):
    list_display = ["entity", "period_start", "period_end", "version", "status", "net_vat_sar", "due_date"]
    list_filter = ["status", "frequency"]


@admin.register(VatReturnSchedule)
class VatReturnScheduleAdmin(ReadOnlyTaxAdmin):
    list_display = ["entity", "period_start", "period_end", "due_date", "frequency"]


@admin.register(VatCorrection)
class VatCorrectionAdmin(ReadOnlyTaxAdmin):
    list_display = ["entity", "original_return", "tax_difference_sar", "treatment", "notify_by", "status"]


@admin.register(ImportVatRecord)
class ImportVatRecordAdmin(ReadOnlyTaxAdmin):
    list_display = ["entity", "customs_declaration_no", "declaration_date", "vat_paid_sar", "attribution", "status"]


@admin.register(TaxPayment)
class TaxPaymentAdmin(ReadOnlyTaxAdmin):
    list_display = ["entity", "vat_return", "amount_sar", "payment_date", "status"]


@admin.register(TaxRefund)
class TaxRefundAdmin(ReadOnlyTaxAdmin):
    list_display = ["entity", "vat_return", "amount_sar", "received_on", "status"]


@admin.register(TaxAlert)
class TaxAlertAdmin(ReadOnlyTaxAdmin):
    list_display = ["entity", "kind", "alert_date", "due_date", "message_en", "exposure_sar"]
