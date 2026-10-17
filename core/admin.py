from django.contrib import admin

from access.admin_scoping import ScopedAdmin

from .models import Entity


@admin.register(Entity)
class EntityAdmin(ScopedAdmin):
    """Entities are visible where the user has access; adding or editing them is a setup task
    (ERP_ADMIN_SETUP_MODE superuser) and is audited."""

    view_perm = "core.entity.view"
    list_display = ["code", "name_en", "name_ar", "parent", "ownership_pct", "consolidation", "reporting_standard"]
    search_fields = ["code", "name_en", "name_ar"]

    def scope_queryset(self, request, qs):
        return qs.filter(pk__in=self.erp_entities(request))
