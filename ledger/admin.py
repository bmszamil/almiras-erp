from django.contrib import admin, messages

from . import services
from .models import Account, FiscalPeriod, JournalEntry, JournalLine


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ["code", "name_en", "name_ar", "type", "entity", "is_postable", "is_intercompany", "sharia_prohibited"]
    list_filter = ["entity", "type", "sharia_prohibited", "is_intercompany"]
    search_fields = ["code", "name_en", "name_ar"]


@admin.register(FiscalPeriod)
class FiscalPeriodAdmin(admin.ModelAdmin):
    list_display = ["entity", "name", "start_date", "end_date", "status"]
    list_filter = ["entity", "status"]


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 2
    fields = ["account", "description", "debit", "credit", "counterparty_entity"]

    def has_change_permission(self, request, obj=None):
        return obj is None or obj.status == JournalEntry.Status.DRAFT

    has_add_permission = has_delete_permission = has_change_permission


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ["number", "entity", "date", "description", "status", "source", "total_debit"]
    list_filter = ["entity", "status", "source"]
    search_fields = ["number", "description", "reference"]
    inlines = [JournalLineInline]
    actions = ["submit_selected", "post_selected", "reverse_selected"]
    readonly_fields = ["number", "status", "source", "requires_approval", "period", "posted_by", "posted_at", "reversal_of"]

    def has_change_permission(self, request, obj=None):
        return obj is None or obj.status == JournalEntry.Status.DRAFT

    def has_delete_permission(self, request, obj=None):
        return obj is None or obj.status == JournalEntry.Status.DRAFT

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
            obj.source = request.user.source
            obj.requires_approval = True  # manual entries are posted by approval (D11)
        super().save_model(request, obj, form, change)

    @admin.action(description="Submit selected drafts for approval")
    def submit_selected(self, request, queryset):
        from access.services import Context
        from approvals.services import submit

        for entry in queryset:
            try:
                r = submit(entry, request.user, ctx=Context.from_request(request, channel="web"))
                self.message_user(request, f"Entry {entry.pk} submitted (request {r.pk}).")
            except Exception as exc:
                self.message_user(request, f"Entry {entry.pk}: {exc}", messages.ERROR)

    @admin.action(description="Post selected draft entries")
    def post_selected(self, request, queryset):
        for entry in queryset:
            try:
                services.post_entry(entry, user=request.user)
                self.message_user(request, f"Posted {entry.pk}.")
            except Exception as exc:  # show the rule that failed
                self.message_user(request, f"Entry {entry.pk}: {exc}", messages.ERROR)

    @admin.action(description="Reverse selected posted entries")
    def reverse_selected(self, request, queryset):
        for entry in queryset:
            try:
                rev = services.reverse_entry(entry, user=request.user)
                self.message_user(request, f"Reversed {entry.number} with {rev.number}.")
            except Exception as exc:
                self.message_user(request, f"{entry.number or entry.pk}: {exc}", messages.ERROR)
