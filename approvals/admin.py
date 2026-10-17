"""Approvals admin. Requests, actions, delegations and notifications are read-only here (they change
only through approvals.services). Policies are managed with permission checks and audit through
approvals.services (R7)."""
from django.contrib import admin
from django.core.exceptions import PermissionDenied

from access import services as access
from access.admin_scoping import ReadOnlyScopedAdmin, ScopedAdmin, admin_ctx, setup_mode
from access.errors import BilingualError

from . import services
from .models import ApprovableType, ApprovalAction, ApprovalPolicy, ApprovalRequest, ApprovalStep, Delegation, Notification


@admin.register(ApprovableType)
class ApprovableTypeAdmin(ReadOnlyScopedAdmin):
    view_perm = "approvals.request.view"
    entity_field = None
    list_display = ["code", "name_en", "name_ar", "is_spending"]


def _can_manage(request, policy):
    try:
        services._require_policy_perm(request.user, policy.entity)
        return True
    except BilingualError:
        return False


class StepInline(admin.TabularInline):
    model = ApprovalStep
    extra = 0

    def _editable(self, request, obj):
        if obj is None:
            return access.entities_with_perm(request.user, "approvals.policy.manage").exists()
        return not obj.is_used and _can_manage(request, obj)

    def has_view_permission(self, request, obj=None):
        return True  # the parent admin already scoped the policy

    def has_change_permission(self, request, obj=None):
        return self._editable(request, obj)

    has_add_permission = has_delete_permission = has_change_permission


@admin.register(ApprovalPolicy)
class ApprovalPolicyAdmin(ScopedAdmin):
    """Matrix changes are data (R7). Scope fields are fixed after creation; used policies can only be
    retired (is_active / valid_to). Every save is permission-checked and audited with its steps."""

    view_perm = "approvals.policy.manage"
    list_display = ["document_type", "entity", "amount_min", "amount_max", "currency", "priority", "is_active", "valid_from", "valid_to"]
    list_filter = ["document_type", "is_active"]
    inlines = [StepInline]

    def scope_queryset(self, request, qs):
        return qs.filter(pk__in=services.manageable_policies(request.user).values("pk"))

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return []
        if obj.is_used:
            return [f.name for f in ApprovalPolicy._meta.fields if f.name not in ApprovalPolicy.RETIRE_FIELDS]
        return ["entity", "document_type", "currency"]

    def has_add_permission(self, request, *args):
        return access.entities_with_perm(request.user, "approvals.policy.manage").exists()

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return self.has_add_permission(request)
        return _can_manage(request, obj)

    def has_delete_permission(self, request, obj=None):
        return obj is not None and not obj.is_used and _can_manage(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "entity" and not setup_mode(request):
            kwargs["queryset"] = access.entities_with_perm(request.user, "approvals.policy.manage")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def audit_extra(self, obj):
        return services._policy_extra(obj) if obj is not None and obj.pk else {}

    def save_model(self, request, obj, form, change):
        original = ApprovalPolicy.objects.get(pk=obj.pk) if change else None
        try:
            services.check_policy_admin(request.user, obj, original)
        except BilingualError as exc:
            raise PermissionDenied(str(exc))
        request._erp_audit_before = (services.snapshot(original) | services._policy_extra(original)) if original else None
        obj.save()

    def save_related(self, request, form, formsets, change):
        admin.ModelAdmin.save_related(self, request, form, formsets, change)
        services.record_policy_change(request.user, form.instance, getattr(request, "_erp_audit_before", None),
                                      created=not change, ctx=admin_ctx(request))

    def delete_model(self, request, obj):
        services.delete_policy(request.user, obj, ctx=admin_ctx(request))


class ActionInline(admin.TabularInline):
    model = ApprovalAction
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in ApprovalAction._meta.fields]

    def has_view_permission(self, request, obj=None):
        return True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(ReadOnlyScopedAdmin):
    view_perm = "approvals.request.view"
    list_display = ["id", "entity", "document_type", "object_id", "amount", "currency", "status", "current_sequence", "submitted_by", "submitted_at"]
    list_filter = ["status", "document_type"]
    inlines = [ActionInline]

    def scope_queryset(self, request, qs):
        return qs.filter(pk__in=services.visible_requests(request.user).values("pk"))

    def has_view_permission(self, request, obj=None):
        if obj is None:
            return setup_mode(request) or services.visible_requests(request.user).exists() or super().has_view_permission(request)
        return super().has_view_permission(request, obj)


@admin.register(Delegation)
class DelegationAdmin(ReadOnlyScopedAdmin):
    view_perm = "core.entity.view"
    entity_field = None
    list_display = ["delegator", "delegate", "entity", "amount_max", "starts_at", "ends_at"]

    def scope_queryset(self, request, qs):
        return qs.filter(pk__in=services.visible_delegations(request.user).values("pk"))


@admin.register(Notification)
class NotificationAdmin(ReadOnlyScopedAdmin):
    view_perm = "core.entity.view"
    entity_field = None
    list_display = ["user", "kind", "alert_code", "title_en", "created_at", "read_at"]
    list_filter = ["kind", "alert_code"]

    def scope_queryset(self, request, qs):
        return qs.filter(user=request.user)
