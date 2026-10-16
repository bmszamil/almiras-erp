from django.contrib import admin

from core import access

from .models import Entity


class EntityScopedAdmin(admin.ModelAdmin):
    """Base admin for business models with an ``entity`` FK (R2, R24).

    Rights come from memberships through ``core.access``, never from Django
    permissions or ``is_superuser``: the changelist is scoped, objects outside
    the user's entities are not found, entity choices are limited, and
    ``status`` is read-only (status changes go through services). Without an
    ``access_change_permission`` the admin is read-only. Nothing is deleted
    unless a subclass allows it.
    """

    access_view_permission = None  # e.g. "ledger.view"
    access_change_permission = None  # e.g. "ledger.create_entry"
    entity_field = "entity"
    status_field = "status"

    def _entity_of(self, obj):
        value = obj
        for part in self.entity_field.split("__"):
            value = getattr(value, part)
        return value

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return access.scope(qs, request.user, self.access_view_permission, self.entity_field)

    def has_module_permission(self, request):
        return access.entities_for(request.user, self.access_view_permission).exists()

    def has_view_permission(self, request, obj=None):
        if obj is None:
            return self.has_module_permission(request)
        return access.has(request.user, self._entity_of(obj), self.access_view_permission)

    def _can_change(self, request, obj=None):
        if not self.access_change_permission:
            return False
        if obj is None:
            return access.entities_for(request.user, self.access_change_permission).exists()
        return access.has(request.user, self._entity_of(obj), self.access_change_permission)

    def has_add_permission(self, request):
        return self._can_change(request)

    def has_change_permission(self, request, obj=None):
        return self._can_change(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        names = {f.name for f in self.model._meta.get_fields()}
        if self.status_field in names and self.status_field not in fields:
            fields.append(self.status_field)
        return fields

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == self.entity_field and self.access_change_permission:
            kwargs["queryset"] = access.entities_for(request.user, self.access_change_permission)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Entity)
class EntityAdmin(admin.ModelAdmin):
    list_display = ["code", "name_en", "name_ar", "parent", "ownership_pct", "consolidation", "reporting_standard"]
    search_fields = ["code", "name_en", "name_ar", "legal_name_en", "legal_name_ar", "cr_number", "vat_number"]
    fieldsets = [
        ("Entity / الكيان", {"fields": ["code", "name_en", "name_ar", "is_active"]}),
        (
            "Registration / التسجيل",
            {"fields": ["legal_name_en", "legal_name_ar", "legal_form", "unified_number", "cr_number"]},
        ),
        ("VAT / ضريبة القيمة المضافة", {"fields": ["vat_number", "vat_effective_date", "vat_return_period"]}),
        (
            "National address / العنوان الوطني",
            {
                "fields": [
                    "building_no",
                    ("street_en", "street_ar"),
                    ("district_en", "district_ar"),
                    "additional_no",
                    "postal_code",
                    ("city_en", "city_ar"),
                    "country",
                    "short_address",
                ]
            },
        ),
        (
            "Group and accounting / المجموعة والمحاسبة",
            {
                "fields": [
                    "parent",
                    "ownership_pct",
                    "consolidation",
                    "reporting_standard",
                    "base_currency",
                    "sharia_enforced",
                ]
            },
        ),
    ]


from core.access import admin as _access_admin  # noqa: E402,F401  (registers access admins)
