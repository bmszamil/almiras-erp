"""Admin for access records. Grants and revocations go through core.access.services and are logged.

Rights come from memberships, not Django permissions (R3, R24): a superuser
with no membership sees no memberships and can grant nothing.
"""
from django import forms
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q

from . import services
from .models import AccessLog, DualControlRequest, Membership, Role, RolePermission, UserProfile

VIEW = "core.view_access"
GRANT = "core.grant_membership"


def _any(request, permission):
    return services.entities_for(request.user, permission).exists()


class MembershipAddForm(forms.ModelForm):
    class Meta:
        model = Membership
        fields = ["user", "entity", "role", "valid_from", "valid_to", "reason"]

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        if actor is not None and "entity" in self.fields:
            entities = services.entities_for(actor, GRANT)
            self.fields["entity"].queryset = entities
        if actor is not None and "user" in self.fields:
            # People already in the admin's entities, plus people with no membership anywhere (new hires);
            # users who belong only to other entities are not listed.
            self.fields["user"].queryset = (
                get_user_model().objects.filter(
                    Q(memberships__entity__in=services.entities_for(actor, GRANT)) | Q(memberships__isnull=True)
                ).exclude(pk=actor.pk).distinct().order_by("username")
            )

    def clean(self):
        data = super().clean()
        entity, user = data.get("entity"), data.get("user")
        if entity is None or user is None or data.get("role") is None:
            return data
        if not services.has(self.actor, entity, GRANT):
            raise ValidationError("You cannot grant memberships in this entity. / لا يمكنك منح العضويات في هذا الكيان.")
        if user.pk == self.actor.pk:
            raise ValidationError("Nobody grants a membership to themselves. / لا يجوز منح العضوية للنفس.")
        return data

    def _post_clean(self):
        # Model validation (R6, R8, R11, dates) runs in grant_membership; preview it here so
        # the form shows errors instead of a failed save.
        super()._post_clean()
        if self.errors or self.actor is None:
            return
        self.instance.granted_by = self.actor
        try:
            self.instance.full_clean()
            if self.instance.overlapping().exists():
                raise ValidationError("An overlapping membership already exists. / توجد عضوية متداخلة.")
        except ValidationError as exc:
            self._update_errors(exc)


class MembershipRevokeForm(forms.ModelForm):
    class Meta:
        model = Membership
        fields = ["revoke_reason"]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "entity", "role", "valid_from", "valid_to", "delegated_from", "revoked_at", "granted_by"]
    list_filter = [("entity", admin.RelatedOnlyFieldListFilter), "role"]
    search_fields = ["user__username", "role__code", "reason"]

    def get_queryset(self, request):
        return services.scope(super().get_queryset(request), request.user, VIEW).select_related("user", "entity", "role")

    def has_module_permission(self, request):
        return _any(request, VIEW) or _any(request, GRANT)

    def has_view_permission(self, request, obj=None):
        if obj is None:
            return self.has_module_permission(request)
        return services.has(request.user, obj.entity, VIEW) or services.has(request.user, obj.entity, GRANT)

    def has_add_permission(self, request):
        return _any(request, GRANT)

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return _any(request, GRANT)
        return obj.revoked_at is None and services.has(request.user, obj.entity, GRANT)

    def has_delete_permission(self, request, obj=None):
        return False  # R9: never deleted

    def get_form(self, request, obj=None, change=False, **kwargs):
        kwargs["form"] = MembershipRevokeForm if obj is not None else MembershipAddForm
        form = super().get_form(request, obj, change=change, **kwargs)
        if obj is None:
            actor = request.user

            class Bound(form):
                def __init__(self, *a, **kw):
                    kw["actor"] = actor
                    super().__init__(*a, **kw)

            return Bound
        return form

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return []
        fields = ["user", "entity", "role", "valid_from", "valid_to", "granted_by", "granted_at", "reason",
                  "delegated_from", "revoked_at", "revoked_by"]
        if obj.revoked_at is not None:
            fields.append("revoke_reason")
        return fields

    def get_fields(self, request, obj=None):
        if obj is None:
            return ["user", "entity", "role", "valid_from", "valid_to", "reason"]
        return self.get_readonly_fields(request, obj)[:11] + ["revoke_reason"]

    def save_model(self, request, obj, form, change):
        if not change:
            m = services.grant_membership(
                request.user, obj.user, obj.entity, obj.role, obj.valid_from, obj.valid_to, obj.reason
            )
            obj.pk = obj.id = m.pk
            return
        stored = Membership.objects.get(pk=obj.pk)
        if obj.revoke_reason and stored.revoked_at is None:
            services.revoke_membership(request.user, stored, obj.revoke_reason)
            obj.refresh_from_db()
        elif not obj.revoke_reason:
            messages.info(request, "Nothing changed. Enter a revoke reason to revoke. / لم يتغير شيء.")


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AccessLog)
class AccessLogAdmin(ReadOnlyAdmin):
    list_display = ["at", "event", "actor", "actor_kind", "entity", "subject_user", "role", "permission"]
    list_filter = ["event", ("entity", admin.RelatedOnlyFieldListFilter), "actor_kind"]
    search_fields = ["permission", "actor__username", "subject_user__username"]

    def get_queryset(self, request):
        # Entity rows by core.view_access; rows without an entity for view_access on the group parent.
        return services.visible_access_log(request.user)

    def has_module_permission(self, request):
        return _any(request, VIEW)

    def has_view_permission(self, request, obj=None):
        if obj is None:
            return _any(request, VIEW)
        return services.visible_access_log(request.user).filter(pk=obj.pk).exists()


@admin.register(DualControlRequest)
class DualControlRequestAdmin(ReadOnlyAdmin):
    list_display = ["requested_at", "entity", "permission", "target_type", "target_id", "status", "requested_by", "decided_by"]
    list_filter = ["status", ("entity", admin.RelatedOnlyFieldListFilter)]

    def get_queryset(self, request):
        return services.scope(super().get_queryset(request), request.user, VIEW)

    def has_module_permission(self, request):
        return _any(request, VIEW)

    def has_view_permission(self, request, obj=None):
        return _any(request, VIEW) if obj is None else services.has(request.user, obj.entity, VIEW)


class RolePermissionInline(admin.TabularInline):
    model = RolePermission
    extra = 0
    fields = ["codename"]
    readonly_fields = ["codename"]
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Role)
class RoleAdmin(ReadOnlyAdmin):
    """The role catalogue is changed by migration (3.1); admin shows it read-only."""

    list_display = ["code", "name_en", "name_ar", "scope", "kind", "system_code", "is_seeded", "is_active"]
    list_filter = ["scope", "kind", "is_seeded"]
    inlines = [RolePermissionInline]


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    fk_name = "user"  # UserProfile also has created_by -> User
    can_delete = False
    fields = ["kind", "system_code", "agent_name", "display_name_en", "display_name_ar", "preferred_language",
              "allow_external_ai", "created_by"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


User = get_user_model()
if admin.site.is_registered(User):
    admin.site.unregister(User)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Django's user admin, recording who changed superuser / staff flags (R25).

    ``is_active`` is read-only on existing users: it changes only through
    core.access.services.set_user_active, which checks ``core.manage_users``
    and writes the access log (R32).
    """

    def get_inlines(self, request, obj=None):
        return [UserProfileInline] if obj is not None else []

    def user_change_password(self, request, id, form_url=""):
        """Setting the password of a person who must use MFA (web.md R13) is for superusers and the group managing
        partner of all their entities only (verification review of f95e1c8, item 2): otherwise an access admin could
        take over an approver's account by password while the MFA reset stays out of reach. Refused and logged."""
        target = self.get_object(request, unquote(id))
        if target is not None and not services.may_set_password(request.user, target):
            services.log(AccessLog.Event.PERMISSION_DENIED, actor=request.user, subject_user=target,
                         permission="admin.set_password", details={"reason": "mfa_user_password"})
            raise PermissionDenied(
                "Only a superuser or the group managing partner sets the password of a person who must use MFA. / "
                "لا يعيّن كلمة مرور من يلزمه التحقق بخطوتين إلا مستخدم أعلى أو الشريك المدير للمجموعة.")
        return super().user_change_password(request, id, form_url)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and "is_active" not in fields:
            fields.append("is_active")
        return fields

    def save_model(self, request, obj, form, change):
        if change:  # a crafted POST cannot change it either
            obj.is_active = User.objects.filter(pk=obj.pk).values_list("is_active", flat=True).get()
        obj._access_actor = request.user
        try:
            super().save_model(request, obj, form, change)
        except ValidationError as exc:
            raise PermissionDenied(" ".join(exc.messages)) from exc
