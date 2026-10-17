from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
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
IDENTITY_FIELDS = {"username", "keycloak_id"}


def _chaired_entities(request):
    """D19: entities where the admin user holds an active Chairman grant."""
    return set(access.active_grants(request.user).filter(role__code=access.CHAIRMAN_ROLE_CODE)
               .values_list("entity_id", flat=True))


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
        """Users holding a grant in an entity the viewer manages, plus users with no grant yet.
        D19: a Chairman also sees the Managing Partners of the entities where he is Chairman."""
        managed = self.erp_entities(request)
        chaired = _chaired_entities(request)
        q = Q(pk__in=[])
        if managed.exists():
            q |= Q(entity_access__entity__in=managed) | Q(entity_access__isnull=True)
        if chaired:
            q |= Q(entity_access__entity__in=chaired, entity_access__role__code=access.MP_ROLE_CODE,
                   entity_access__revoked_at__isnull=True)
        return qs.filter(q).distinct()

    def has_view_permission(self, request, obj=None):
        if obj is None and request.user.is_authenticated and request.user.is_active and _chaired_entities(request):
            return True
        return super().has_view_permission(request, obj)

    actions = ["unlock_selected"]

    def _may_manage(self, request, obj):
        # Review B + D19 (Managing Partners: Chairman only) + 2a (system user never); setup mode excepted.
        return access.can_manage_user(request.user, obj, setup=setup_mode(request))

    def get_readonly_fields(self, request, obj=None):
        ro = ["last_login", "date_joined"]
        if obj is None:
            return ro + ([] if setup_mode(request) else ["is_superuser"])
        if not setup_mode(request) or obj.pk == request.user.pk:
            ro += ["is_superuser", "user_type", "agent_name"]  # user_type changes are setup-only (review B)
        if obj.pk == request.user.pk and not setup_mode(request):
            ro += sorted(IDENTITY_FIELDS)  # nobody renames themselves or changes their own identity link
        if not self._may_manage(request, obj):
            ro += ["is_staff", "is_active"]  # own privileges, or a user outside the editor's entities
        return sorted(set(ro))

    def has_add_permission(self, request, *args):
        return setup_mode(request) or _manages_users(request)

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return setup_mode(request) or _manages_users(request) or bool(_chaired_entities(request))
        if obj.pk == request.user.pk:
            return True  # own profile (name, email, language); privileges stay read-only
        return self._may_manage(request, obj)

    def save_model(self, request, obj, form, change):
        if change:
            stored = User.objects.get(pk=obj.pk)
            if obj.pk == request.user.pk and not setup_mode(request) and any(
                    getattr(stored, f) != getattr(obj, f) for f in IDENTITY_FIELDS):
                raise PermissionDenied("You cannot change your own username or Keycloak ID.")
            changed = {f for f in PRIVILEGE_FIELDS if getattr(stored, f) != getattr(obj, f)}
            if changed:
                if obj.pk == request.user.pk:
                    raise PermissionDenied("You cannot change your own privileges.")
                if not setup_mode(request) and changed & {"user_type", "agent_name", "is_superuser"}:
                    raise PermissionDenied("User type and superuser status change only in setup mode.")
                if not self._may_manage(request, obj):
                    raise PermissionDenied("You need user-management rights in every entity of this user.")
            elif obj.pk != request.user.pk and not self._may_manage(request, obj):
                raise PermissionDenied("You cannot change this user.")
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
        target = User.objects.filter(pk=id).first()
        if target is not None and target.pk != request.user.pk and not self._may_manage(request, target):
            raise PermissionDenied("You cannot change this user's password.")
        response = super().user_change_password(request, id, form_url)
        if request.method == "POST" and getattr(response, "status_code", 200) == 302:
            access.audit(request.user, "access.user.password_change", target, ctx=admin_ctx(request))
        return response

    @admin.action(description="Clear sign-in lockout / إلغاء قفل تسجيل الدخول")
    def unlock_selected(self, request, queryset):
        for user in queryset:
            try:
                access.unlock_user(request.user, user, ctx=admin_ctx(request), setup=setup_mode(request))
                self.message_user(request, f"Unlocked {user.username}.")
            except Exception as exc:
                self.message_user(request, f"{user.username}: {exc}", messages.ERROR)


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
        if setup_mode(request):
            return True
        if not _manages_globally(request):
            return False
        return obj is None or access.can_edit_role(request.user, obj)

    def has_delete_permission(self, request, obj=None):
        return (obj is None or not obj.is_system) and self.has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if change and not access.can_edit_role(request.user, obj, setup=setup_mode(request)):
            raise PermissionDenied("Roles held by a Managing Partner change only through the Chairman (D19).")
        super().save_model(request, obj, form, change)

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
                access.revoke_service_token(request.user, token, ctx=admin_ctx(request), setup=setup_mode(request))
            except Exception as exc:
                self.message_user(request, f"{token}: {exc}", messages.ERROR)
