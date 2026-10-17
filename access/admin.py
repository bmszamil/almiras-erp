from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.db.models import Q

from . import services as access
from .admin_scoping import ReadOnlyScopedAdmin, ScopedAdmin, admin_ctx, setup_mode
from .models import AuditLog, EntityAccess, Permission, Role, ServiceToken, User

# Django groups are not used (roles are per entity); TOTP devices are enrolled with the audited
# `setup_totp` command / access.services.enroll_totp, not through a generic admin form.
for model in (Group,):
    if admin.site.is_registered(model):
        admin.site.unregister(model)
try:
    from django_otp.plugins.otp_totp.models import TOTPDevice

    if admin.site.is_registered(TOTPDevice):
        admin.site.unregister(TOTPDevice)
except ImportError:  # pragma: no cover
    pass

PRIVILEGE_FIELDS = {"user_type", "is_superuser", "is_staff", "is_active", "agent_name"}


def _manages_users(request):
    return access.entities_with_perm(request.user, "access.user.manage").exists()


def _manages_globally(request):
    """Global objects (roles) need the permission in a root (parent) entity."""
    return access.entities_with_perm(request.user, "access.user.manage").filter(parent__isnull=True).exists()


@admin.register(User)
class UserAdmin(ScopedAdmin, BaseUserAdmin):
    view_perm = "access.user.manage"
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
    filter_horizontal = ()

    def scope_queryset(self, request, qs):
        """Users holding a grant in an entity the viewer manages, plus users with no grant yet."""
        managed = self.erp_entities(request)
        if not managed.exists():
            return qs.none()
        return qs.filter(Q(entity_access__entity__in=managed) | Q(entity_access__isnull=True)).distinct()

    def get_readonly_fields(self, request, obj=None):
        ro = ["last_login", "date_joined"]
        if not setup_mode(request):
            ro.append("is_superuser")  # superuser is a setup-only flag
        return ro

    def has_add_permission(self, request, *args):
        return setup_mode(request) or _manages_users(request)

    def has_change_permission(self, request, obj=None):
        if setup_mode(request):
            return True
        if not _manages_users(request):
            return False
        return obj is None or self.visible(request, obj)

    def save_model(self, request, obj, form, change):
        if not obj.is_human:
            obj.is_staff = obj.is_superuser = False  # service users never use the admin
            obj.set_unusable_password()
        super().save_model(request, obj, form, change)

    def audit_action(self, obj, before, after, change):
        if not change:
            return "access.user.create"
        changed = {k for k in PRIVILEGE_FIELDS if (before or {}).get(k) != after.get(k)}
        return "access.user.privilege_change" if changed else "access.user.update"

    def user_change_password(self, request, id, form_url=""):
        response = super().user_change_password(request, id, form_url)
        if request.method == "POST" and getattr(response, "status_code", 200) == 302:
            target = User.objects.get(pk=id)
            access.audit(request.user, "access.user.password_change", target, ctx=admin_ctx(request))
        return response


@admin.register(Role)
class RoleAdmin(ScopedAdmin):
    view_perm = "access.entity_access.manage"
    entity_field = None
    list_display = ["code", "name_en", "name_ar", "is_system"]
    filter_horizontal = ["permissions"]

    def get_readonly_fields(self, request, obj=None):
        return ["is_system", "code"] if obj is not None and obj.is_system else ["is_system"]

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj) or _manages_users(request)

    def has_add_permission(self, request, *args):
        return setup_mode(request) or _manages_globally(request)

    def has_change_permission(self, request, obj=None):
        return setup_mode(request) or _manages_globally(request)

    def has_delete_permission(self, request, obj=None):
        return (obj is None or not obj.is_system) and (setup_mode(request) or _manages_globally(request))

    def audit_extra(self, obj):
        return {"permissions": sorted(obj.permissions.values_list("code", flat=True))} if obj and obj.pk else {}

    def audit_action(self, obj, before, after, change):
        return "access.role.change" if change else "access.role.create"


@admin.register(Permission)
class PermissionAdmin(ReadOnlyScopedAdmin):
    """The catalogue comes from access.catalogue (data migration); read-only here."""

    view_perm = "access.entity_access.manage"
    entity_field = None
    list_display = ["code", "name_en", "name_ar"]
    search_fields = ["code"]


@admin.register(EntityAccess)
class EntityAccessAdmin(ReadOnlyScopedAdmin):
    """Read-only list; grants and revokes go through access.services so they are audited."""

    view_perm = "access.entity_access.manage"
    list_display = ["user", "entity", "role", "valid_from", "valid_to", "revoked_at", "granted_by"]
    list_filter = ["entity", "role"]

    def erp_entities(self, request, perm=None):
        if perm:
            return super().erp_entities(request, perm)
        return access.entities_with_perm(request.user, "access.entity_access.manage") | \
            access.entities_with_perm(request.user, "access.audit.view")


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyScopedAdmin):
    view_perm = "access.audit.view"
    list_display = ["at", "actor", "on_behalf_of", "entity", "action", "object_type", "object_id", "source", "channel"]
    list_filter = ["action", "channel"]
    search_fields = ["object_type", "object_id", "action"]

    def scope_queryset(self, request, qs):
        return qs.filter(pk__in=access.visible_audit(request.user).values("pk"))

    def get_fields(self, request, obj=None):
        fields = [f.name for f in AuditLog._meta.fields]
        if obj is not None and not (setup_mode(request) or access.can_view_security(request.user, obj)):
            fields = [f for f in fields if f not in ("ip", "user_agent")]
        return fields

    def get_readonly_fields(self, request, obj=None):
        return self.get_fields(request, obj)


@admin.register(ServiceToken)
class ServiceTokenAdmin(ReadOnlyScopedAdmin):
    """Tokens are issued with `manage.py issue_service_token` (shown once). Revoke here; audited."""

    view_perm = "access.user.manage"
    entity_field = None
    list_display = ["user", "label", "created_at", "expires_at", "revoked_at"]
    exclude = ["key_hash"]
    actions = ["revoke_selected"]

    @admin.action(description="Revoke selected tokens / إلغاء الرموز المحددة")
    def revoke_selected(self, request, queryset):
        for token in queryset:
            try:
                access.revoke_service_token(request.user, token, ctx=admin_ctx(request))
            except Exception as exc:
                self.message_user(request, f"{token}: {exc}", messages.ERROR)
