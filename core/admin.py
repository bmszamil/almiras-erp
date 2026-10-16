from django.contrib import admin

from core import access

from .models import Currency, Entity
from .services import SENSITIVE_ENTITY_FIELDS, create_entity


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
    """Registration and address fields are edited here. Sensitive settings are read-only once the entity
    exists: they change through core.services.change_entity_settings and ledger.services.set_functional_currency,
    which check rights and write the access log (access.md R32). A new entity's settings are logged."""

    LOCKED_FIELDS = (*SENSITIVE_ENTITY_FIELDS, "base_currency")

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
                    "presentation_currency",
                    "sharia_enforced",
                ]
            },
        ),
    ]

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields += [f for f in self.LOCKED_FIELDS if f not in fields]
        return fields

    def _settings_groups(self, request):
        """Group parents where the user holds core.change_entity_settings (entities are added under them)."""
        return access.entities_for(request.user, "core.change_entity_settings").filter(parent__isnull=True)

    def has_add_permission(self, request):
        # From memberships only, never Django permissions or is_superuser (access.md R3, R24).
        return self._settings_groups(request).exists()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "parent":  # entities of the groups the user may change (create_entity checks again)
            groups = set(self._settings_groups(request).values_list("pk", flat=True))
            ids = [e.pk for e in Entity.objects.all() if access.group_parent_of(e).pk in groups]
            kwargs["queryset"] = Entity.objects.filter(pk__in=ids)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if change:  # read-only in the form; a crafted POST cannot change them either
            stored = Entity.objects.get(pk=obj.pk)
            for attr in ("sharia_enforced", "consolidation", "parent_id", "is_active", "base_currency_id"):
                setattr(obj, attr, getattr(stored, attr))
            super().save_model(request, obj, form, change)
            return
        # A new entity: core.services.create_entity checks the right on the chosen group and logs it (R32).
        create_entity(obj, user=request.user)


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    """Read-only: ISO 4217 reference data is loaded and changed by migration (ledger-fx.md 3.1)."""

    list_display = ["code", "numeric_code", "name_en", "name_ar", "minor_units", "symbol", "is_active"]
    search_fields = ["code", "name_en", "name_ar"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


from core.access import admin as _access_admin  # noqa: E402,F401  (registers access admins)
