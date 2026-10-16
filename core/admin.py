from django.contrib import admin

from .models import Entity


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
