from django.contrib import admin

from .models import Entity


@admin.register(Entity)
class EntityAdmin(admin.ModelAdmin):
    list_display = ["code", "name_en", "name_ar", "parent", "ownership_pct", "consolidation", "reporting_standard"]
    search_fields = ["code", "name_en", "name_ar"]
