"""Thin, entity-scoped, read-only admin for zatca (R-3, access R2, R24).

Every change goes through ``zatca.services``. Lists are scoped to the entities where the user holds
``zatca.view``. Signed and cleared XML are not shown in lists (they can hold buyer data); exports go through
``zatca.services.export_archive``. No model here stores a secret (references only, R-6).
"""
from django.contrib import admin

from core.admin import EntityScopedAdmin

from .models import (
    ComplianceTask,
    EgsCredential,
    EgsUnit,
    EInvoice,
    KeyCustodianDelegation,
    OnboardingEvent,
    SubmissionAttempt,
    ValidationMessage,
    ZatcaAlert,
    ZatcaProfile,
)


class ReadOnly(EntityScopedAdmin):
    access_view_permission = "zatca.view"
    access_change_permission = None

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ZatcaProfile)
class ZatcaProfileAdmin(ReadOnly):
    list_display = ("entity", "environment", "is_enabled", "go_live_date")


@admin.register(EgsUnit)
class EgsUnitAdmin(ReadOnly):
    list_display = ("entity", "code", "name_en", "status", "last_icv")
    exclude = ("csr_pem",)


@admin.register(EgsCredential)
class EgsCredentialAdmin(ReadOnly):
    list_display = ("entity", "egs_unit", "kind", "environment", "status", "valid_to")
    exclude = ("certificate_pem",)


@admin.register(EInvoice)
class EInvoiceAdmin(ReadOnly):
    list_display = ("entity", "source_document", "type_code", "zatca_category", "icv", "status", "hold_reason")
    list_filter = ("status", "zatca_category")
    exclude = ("signed_xml", "cleared_xml", "qr_tlv")


@admin.register(SubmissionAttempt)
class SubmissionAttemptAdmin(ReadOnly):
    list_display = ("entity", "einvoice", "attempt_no", "api", "http_status", "outcome", "finished_at")


@admin.register(ValidationMessage)
class ValidationMessageAdmin(ReadOnly):
    list_display = ("entity", "einvoice", "level", "origin", "code", "message_en", "display_ar")


@admin.register(OnboardingEvent)
class OnboardingEventAdmin(ReadOnly):
    list_display = ("entity", "egs_unit", "action", "actor", "at")


@admin.register(ComplianceTask)
class ComplianceTaskAdmin(ReadOnly):
    list_display = ("entity", "einvoice", "kind", "status", "notified_at")


@admin.register(KeyCustodianDelegation)
class KeyCustodianDelegationAdmin(ReadOnly):
    list_display = ("entity", "custodian", "scope", "granted_by", "granted_at", "revoked_at")


@admin.register(ZatcaAlert)
class ZatcaAlertAdmin(ReadOnly):
    list_display = ("entity", "recipient", "kind", "created_at")
