from django import forms
from django.contrib import admin, messages

from core import access
from core.admin import EntityScopedAdmin

from . import services
from .models import Account, ExchangeRate, FiscalPeriod, JournalEntry, JournalLine, RevaluationRun


@admin.register(Account)
class AccountAdmin(EntityScopedAdmin):
    list_display = [
        "code", "name_en", "name_ar", "type", "entity", "is_postable", "is_intercompany", "sharia_prohibited",
        "is_monetary", "currency",
    ]
    list_filter = ["entity", "type", "sharia_prohibited", "is_intercompany", "is_monetary"]
    search_fields = ["code", "name_en", "name_ar"]
    access_view_permission = "ledger.view"


@admin.register(FiscalPeriod)
class FiscalPeriodAdmin(EntityScopedAdmin):
    """Read-only: periods are created by setup and closed by ledger.services.close_period."""

    list_display = ["entity", "name", "start_date", "end_date", "status"]
    list_filter = ["entity", "status"]
    access_view_permission = "ledger.view"


class JournalLineForm(forms.ModelForm):
    """Admin edits functional amounts only. Foreign-currency lines are created through
    ledger.services.create_entry (rates, R14); here their amounts are checked at posting (R7)."""

    def save(self, commit=True):
        line = super().save(commit=False)
        if line.pk and line.rate_source == JournalLine.RateSource.FUNCTIONAL:
            line.txn_amount = line.debit + line.credit  # R6: keep a functional line's txn equal to its amount
        if commit:
            line.save()
        return line


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    form = JournalLineForm
    extra = 2
    fields = ["account", "description", "debit", "credit", "counterparty_entity", "currency", "txn_amount", "fx_rate", "line_kind"]
    readonly_fields = ["currency", "txn_amount", "fx_rate", "line_kind"]

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return access.entities_for(request.user, "ledger.create_entry").exists()
        return obj.status == JournalEntry.Status.DRAFT and access.has(request.user, obj.entity, "ledger.create_entry")

    has_add_permission = has_delete_permission = has_change_permission

    def has_view_permission(self, request, obj=None):
        if obj is None:
            return access.entities_for(request.user, "ledger.view").exists()
        return access.has(request.user, obj.entity, "ledger.view")


@admin.register(JournalEntry)
class JournalEntryAdmin(EntityScopedAdmin):
    list_display = ["number", "entity", "date", "description", "status", "total_debit"]
    list_filter = ["entity", "status"]
    search_fields = ["number", "description", "reference"]
    inlines = [JournalLineInline]
    actions = ["post_selected", "reverse_selected"]
    readonly_fields = ["number", "status", "period", "posted_by", "posted_at", "reversal_of"]
    exclude = ["created_by"]
    access_view_permission = "ledger.view"
    access_change_permission = "ledger.create_entry"

    def has_change_permission(self, request, obj=None):
        return (obj is None or obj.status == JournalEntry.Status.DRAFT) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return (obj is None or obj.status == JournalEntry.Status.DRAFT) and self._can_change(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description="Post selected draft entries")
    def post_selected(self, request, queryset):
        for entry in queryset:
            try:
                grant = access.require(request.user, entry.entity, "ledger.post_entry", document=entry)
                services.post_entry(entry, grant)
                self.message_user(request, f"Posted {entry.pk}.")
            except Exception as exc:  # show the rule that failed
                self.message_user(request, f"Entry {entry.pk}: {exc}", messages.ERROR)

    @admin.action(description="Reverse selected posted entries")
    def reverse_selected(self, request, queryset):
        for entry in queryset:
            try:
                grant = access.require(request.user, entry.entity, "ledger.reverse_entry")
                rev = services.reverse_entry(entry, grant)
                self.message_user(request, f"Reversed {entry.number} with {rev.number}.")
            except Exception as exc:
                self.message_user(request, f"{entry.number or entry.pk}: {exc}", messages.ERROR)


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    """Read-only list: rates are entered with ledger.services.create_rate / import_rates (R11, R28).

    Shows the user's entities' overrides plus group-wide rows (rates_visible_to).
    """

    list_display = ["date", "from_currency", "to_currency", "rate", "rate_type", "source", "entity", "import_batch"]
    list_filter = ["rate_type", "source", "from_currency", "to_currency"]
    date_hierarchy = "date"

    def get_queryset(self, request):
        return services.rates_visible_to(request.user).select_related("entity")

    def has_module_permission(self, request):
        return access.entities_for(request.user, "ledger.view_rates").exists()

    def has_view_permission(self, request, obj=None):
        if obj is None:
            return self.has_module_permission(request)
        return services.rates_visible_to(request.user).filter(pk=obj.pk).exists()

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RevaluationRun)
class RevaluationRunAdmin(EntityScopedAdmin):
    """Read-only: runs are made by ledger.services.revalue_period and posted by post_revaluation."""

    list_display = ["entity", "period", "rate_date", "entry", "reversal", "run_by", "run_at"]
    list_filter = ["entity"]
    access_view_permission = "ledger.view"
