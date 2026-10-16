from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import AuditLog, EntityAccess, Permission, Role, ServiceToken, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ["username", "full_name_en", "full_name_ar", "email", "user_type", "language", "is_active"]
    list_filter = ["user_type", "is_active", "language"]
    search_fields = ["username", "full_name_en", "full_name_ar", "email"]
    ordering = ["username"]
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Profile", {"fields": ("full_name_en", "full_name_ar", "email", "language", "default_entity")}),
        ("Type", {"fields": ("user_type", "agent_name", "keycloak_id")}),
        ("Status", {"fields": ("is_active", "is_staff", "is_superuser", "last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": (
            "username", "full_name_en", "full_name_ar", "email", "user_type", "agent_name", "password1", "password2",
        )}),
    )
    readonly_fields = ["last_login", "date_joined"]

    def has_delete_permission(self, request, obj=None):
        return False  # R3: deactivate, never delete


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ["code", "name_en", "name_ar", "is_system"]
    filter_horizontal = ["permissions"]

    def has_delete_permission(self, request, obj=None):
        return obj is None or not obj.is_system


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ["code", "name_en", "name_ar"]
    search_fields = ["code"]


@admin.register(EntityAccess)
class EntityAccessAdmin(admin.ModelAdmin):
    """Read-only list; grants and revokes go through access.services so they are audited."""

    list_display = ["user", "entity", "role", "valid_from", "valid_to", "revoked_at", "granted_by"]
    list_filter = ["entity", "role"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["at", "actor", "on_behalf_of", "entity", "action", "object_type", "object_id", "source", "channel"]
    list_filter = ["action", "entity", "channel"]
    search_fields = ["object_type", "object_id", "action"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ServiceToken)
class ServiceTokenAdmin(admin.ModelAdmin):
    list_display = ["user", "label", "created_at", "expires_at", "revoked_at"]
    readonly_fields = ["key_hash"]

    def has_add_permission(self, request):
        return False  # issued with `manage.py issue_service_token`, shown once
