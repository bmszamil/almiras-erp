"""Entity-scoped, audited Django admin (review item 5).

- Every admin queryset is filtered by the user's EntityAccess and ERP permissions, not Django model
  permissions. Superusers bypass scoping only while settings.ERP_ADMIN_SETUP_MODE is on (initial setup).
- Every admin add/change/delete writes an AuditLog row with before/after (R19).
- Bulk "delete selected" is removed everywhere; deletions go one object at a time through the guards.
"""
from django.conf import settings
from django.contrib import admin
from django.contrib.admin.options import ActionLocation

from . import services as access
from .models import AuditLog
from .services import Context, audit, snapshot


def setup_mode(request):
    user = request.user
    return bool(getattr(settings, "ERP_ADMIN_SETUP_MODE", False) and user.is_active and user.is_superuser)


def admin_ctx(request):
    return Context.from_request(request, channel=AuditLog.Channel.WEB)


class ScopedAdmin(admin.ModelAdmin):
    #: ERP permission needed to see objects; objects are limited to entities where the user holds it.
    view_perm = None
    #: Lookup from the model to its entity, or None for global models (then any entity with view_perm suffices).
    entity_field = "entity"

    # ---------------------------------------------------------------- scoping
    def erp_entities(self, request, perm=None):
        return access.entities_with_perm(request.user, perm or self.view_perm)

    def scope_queryset(self, request, qs):
        if self.entity_field is None:
            return qs if self.erp_entities(request).exists() else qs.none()
        return qs.filter(**{f"{self.entity_field}__in": self.erp_entities(request)})

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if setup_mode(request) else self.scope_queryset(request, qs)

    def visible(self, request, obj):
        return setup_mode(request) or self.get_queryset(request).filter(pk=obj.pk).exists()

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def has_view_permission(self, request, obj=None):
        if not request.user.is_authenticated or not request.user.is_active:
            return False
        if setup_mode(request):
            return True
        if obj is not None:
            return self.visible(request, obj)
        return self.view_perm is not None and self.erp_entities(request).exists()

    def has_add_permission(self, request, *args):
        return setup_mode(request)

    def has_change_permission(self, request, obj=None):
        return setup_mode(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request, action_location=ActionLocation.CHANGE_LIST):
        actions = super().get_actions(request, action_location)
        actions.pop("delete_selected", None)
        return actions

    # ---------------------------------------------------------------- audit
    def audit_extra(self, obj):
        """Related data to include in before/after (e.g. lines, permissions, steps)."""
        return {}

    def audit_action(self, obj, before, after, change):
        return f"admin.{obj._meta.label_lower}.{'change' if change else 'add'}"

    def _snapshot(self, obj):
        return (snapshot(obj) or {}) | self.audit_extra(obj)

    def save_model(self, request, obj, form, change):
        request._erp_audit_before = None
        if change:
            stored = type(obj)._default_manager.filter(pk=obj.pk).first()
            request._erp_audit_before = self._snapshot(stored) if stored else None
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        before = getattr(request, "_erp_audit_before", None)
        after = self._snapshot(type(obj)._default_manager.get(pk=obj.pk))
        audit(request.user, self.audit_action(obj, before, after, change), obj, before=before, after=after,
              ctx=admin_ctx(request))

    def delete_model(self, request, obj):
        before = self._snapshot(obj)
        pk = obj.pk
        entity = getattr(obj, "entity", None) if hasattr(obj, "entity_id") else None
        super().delete_model(request, obj)
        audit(request.user, f"admin.{obj._meta.label_lower}.delete", None, entity=entity,
              object_type=obj._meta.label_lower, object_id=str(pk), before=before, ctx=admin_ctx(request))


class ReadOnlyScopedAdmin(ScopedAdmin):
    def has_add_permission(self, request, *args):
        return False

    def has_change_permission(self, request, obj=None):
        return False
