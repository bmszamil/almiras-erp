"""Thin, entity-scoped, read-only admin for zatca (R-3, access R2, R24).

Every change goes through ``zatca.services``. Lists are scoped to the entities where the user holds
``zatca.view``. Signed and cleared XML are not shown in lists (they can hold buyer data); exports go through
``zatca.services.export_archive``. No model here stores a secret (references only, R-6).
"""
from django.contrib import admin

from core import access
from core.admin import EntityScopedAdmin

from . import services

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
    list_display = ("entity", "code", "name_en", "status", "attention_reason", "last_icv")
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


class HidesKeyEvents(ReadOnly):
    """§7 (D-19): secret-version retirement events and their alerts are shown only where the user holds
    ``zatca.view_key_events`` for the row's entity (ZATCA admin, viewer); ``zatca.view`` alone does not show them."""

    key_event_field = None

    def get_queryset(self, request):
        return services._hide_key_events(super().get_queryset(request), request.user, field=self.key_event_field)

    def has_view_permission(self, request, obj=None):
        if obj is not None and getattr(obj, self.key_event_field) in self.key_event_kinds() and not access.has(
                request.user, obj.entity, services.VIEW_KEY_EVENTS):
            return False
        return super().has_view_permission(request, obj)

    def key_event_kinds(self):
        return services.RETIREMENT_ACTIONS if self.key_event_field == "action" else services.RETIREMENT_ALERT_KINDS


@admin.register(OnboardingEvent)
class OnboardingEventAdmin(HidesKeyEvents):
    list_display = ("entity", "egs_unit", "action", "actor", "at")
    key_event_field = "action"


@admin.register(ComplianceTask)
class ComplianceTaskAdmin(ReadOnly):
    list_display = ("entity", "einvoice", "kind", "status", "notified_at")


@admin.register(KeyCustodianDelegation)
class KeyCustodianDelegationAdmin(ReadOnly):
    list_display = ("entity", "custodian", "scope", "granted_by", "granted_at", "revoked_at")


@admin.register(ZatcaAlert)
class ZatcaAlertAdmin(HidesKeyEvents):
    list_display = ("entity", "recipient", "kind", "created_at")
    key_event_field = "kind"
