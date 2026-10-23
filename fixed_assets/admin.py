"""Read-only admin for the fixed-asset register (access R2, R24; fixed-assets R29, R42). Every change goes through
fixed_assets.services; nothing is deleted."""
from django.contrib import admin

from core.admin import EntityScopedAdmin

from .models import (
    AssetCategory, AssetCostComponent, AssetEvent, AssetLocation, DepreciationLine, DepreciationPolicy, DepreciationRun,
    FixedAsset, FixedAssetSettings, IpFairValue, ReviewReminder,
)


class FixedAssetsAdmin(EntityScopedAdmin):
    access_view_permission = "fixed_assets.view"
    access_change_permission = None  # read-only: services only

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AssetCategory)
class AssetCategoryAdmin(FixedAssetsAdmin):
    list_display = ("code", "entity", "name_en", "name_ar", "asset_class", "cost_account", "depreciable",
                    "retention_class", "rental_fleet", "is_active")
    list_filter = ("asset_class", "retention_class")


@admin.register(AssetLocation)
class AssetLocationAdmin(FixedAssetsAdmin):
    list_display = ("code", "entity", "name_ar", "name_en", "city_ar")


@admin.register(FixedAssetSettings)
class FixedAssetSettingsAdmin(FixedAssetsAdmin):
    list_display = ("entity", "art52_adjustment_months_movable", "art52_adjustment_months_real_estate",
                    "normal_credit_days")


@admin.register(DepreciationPolicy)
class DepreciationPolicyAdmin(FixedAssetsAdmin):
    list_display = ("category", "entity", "effective_from", "method", "useful_life_months", "annual_rate",
                    "residual_pct", "status")
    list_filter = ("status", "method")


@admin.register(FixedAsset)
class FixedAssetAdmin(FixedAssetsAdmin):
    """``custodian_ref`` is an hr employee code, the register's only personal reference (R42)."""

    list_display = ("code", "entity", "description_ar", "category", "status", "cost", "acquisition_date",
                    "available_for_use_date", "retention_until")
    exclude = ("transferred_from",)  # R29: the origin is shown by services.asset_detail to users with both rights

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields if f.name != "transferred_from"]
    list_filter = ("status", "category__asset_class")
    search_fields = ("code", "description_ar", "description_en", "serial_or_plate", "legacy_code")


@admin.register(AssetCostComponent)
class AssetCostComponentAdmin(FixedAssetsAdmin):
    list_display = ("asset", "entity", "kind", "amount", "accounting_date", "purchase_line", "deferred_payment")


@admin.register(DepreciationRun)
class DepreciationRunAdmin(FixedAssetsAdmin):
    list_display = ("number", "entity", "period", "status", "total", "journal_entry")
    list_filter = ("status",)


@admin.register(DepreciationLine)
class DepreciationLineAdmin(FixedAssetsAdmin):
    list_display = ("run", "asset", "days", "amount", "catch_up", "opening_nbv")


@admin.register(AssetEvent)
class AssetEventAdmin(FixedAssetsAdmin):
    list_display = ("pk", "asset", "entity", "kind", "event_date", "amount", "status", "journal_entry")
    list_filter = ("kind", "status")


@admin.register(IpFairValue)
class IpFairValueAdmin(FixedAssetsAdmin):
    list_display = ("asset", "entity", "valuation_date", "amount", "valuer_name_ar")


@admin.register(ReviewReminder)
class ReviewReminderAdmin(FixedAssetsAdmin):
    list_display = ("entity", "kind", "due_date", "message_en")
