"""Read-mostly admin. Requests and actions change only through approvals.services."""
from django.contrib import admin

from .models import (
    ApprovableType, ApprovalAction, ApprovalPolicy, ApprovalRequest, ApprovalStep, Delegation, Notification,
)


class ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ApprovableType)
class ApprovableTypeAdmin(ReadOnly):
    list_display = ["code", "name_en", "name_ar", "is_spending"]


class StepInline(admin.TabularInline):
    model = ApprovalStep
    extra = 0

    def has_change_permission(self, request, obj=None):
        return obj is None or not obj.is_used

    has_add_permission = has_delete_permission = has_change_permission


@admin.register(ApprovalPolicy)
class ApprovalPolicyAdmin(admin.ModelAdmin):
    """Matrix changes are data (R7). Used policies cannot be edited, only retired (new version)."""

    list_display = ["document_type", "entity", "amount_min", "amount_max", "currency", "priority", "is_active", "valid_from", "valid_to"]
    list_filter = ["document_type", "entity", "is_active"]
    inlines = [StepInline]

    def has_delete_permission(self, request, obj=None):
        return obj is None or not obj.is_used


class ActionInline(admin.TabularInline):
    model = ApprovalAction
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in ApprovalAction._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(ReadOnly):
    list_display = ["id", "entity", "document_type", "object_id", "amount", "currency", "status", "current_sequence", "submitted_by", "submitted_at"]
    list_filter = ["status", "entity", "document_type"]
    inlines = [ActionInline]


@admin.register(Delegation)
class DelegationAdmin(ReadOnly):
    list_display = ["delegator", "delegate", "entity", "amount_max", "starts_at", "ends_at"]


@admin.register(Notification)
class NotificationAdmin(ReadOnly):
    list_display = ["user", "kind", "alert_code", "title_en", "created_at", "read_at"]
    list_filter = ["kind", "alert_code"]
