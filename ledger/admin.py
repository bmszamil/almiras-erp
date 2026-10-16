from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseRedirect

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
    """Admin edits functional amounts only; the form is never saved directly.

    JournalEntryAdmin passes the lines to ledger.services (create_entry /
    update_draft), which builds and checks them. Foreign-currency lines are
    created through ledger.services.create_entry (rates, R14), so a draft with
    any such line is read-only here.
    """


def _admin_entity(request):
    return getattr(request, "_ledger_admin_entity", None)


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    form = JournalLineForm
    extra = 2
    fields = ["account", "description", "debit", "credit", "counterparty_entity", "currency", "txn_amount", "fx_rate",
              "line_kind", "requires_purification"]
    readonly_fields = ["currency", "txn_amount", "fx_rate", "line_kind", "requires_purification"]

    def get_formset(self, request, obj=None, **kwargs):
        request._ledger_admin_entity = obj.entity if obj is not None else None
        return super().get_formset(request, obj, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        entity = _admin_entity(request)
        if db_field.name == "account":
            # Only the entry's own accounts (rule 4); on a new entry, accounts of entities the user drafts in.
            entities = [entity] if entity is not None else access.entities_for(request.user, "ledger.create_entry")
            kwargs["queryset"] = Account.objects.filter(entity__in=entities, is_postable=True, is_active=True)
        elif db_field.name == "counterparty_entity":
            kwargs["queryset"] = services.counterparty_choices(request.user, entity)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _editable(self, request, obj):
        if obj is None:
            return access.entities_for(request.user, "ledger.create_entry").exists()
        return JournalEntryAdmin.editable_in_admin(obj) and access.has(request.user, obj.entity, "ledger.create_entry")

    def has_change_permission(self, request, obj=None):
        return self._editable(request, obj)

    has_add_permission = has_delete_permission = has_change_permission

    def has_view_permission(self, request, obj=None):
        if obj is None:
            return access.entities_for(request.user, "ledger.view").exists()
        return access.has(request.user, obj.entity, "ledger.view")


class _ServiceRefused(Exception):
    def __init__(self, exc):
        self.exc = exc


@admin.register(JournalEntry)
class JournalEntryAdmin(EntityScopedAdmin):
    """Drafts are created and changed through ledger.services (create_entry, update_draft): the admin
    collects the header and lines and calls the service, which checks rights and rules and records the
    creator / last modifier (R14). Posted entries are read-only."""

    list_display = ["number", "entity", "date", "description", "status", "source", "total_debit"]
    list_filter = ["entity", "status", "source"]
    search_fields = ["number", "description", "reference"]
    inlines = [JournalLineInline]
    actions = ["post_selected", "reverse_selected"]
    readonly_fields = ["number", "status", "period", "posted_by", "posted_at", "reversal_of", "source", "created_by",
                       "submitted_by", "submitted_at", "modified_by", "modified_at", "document_type", "document_id"]
    access_view_permission = "ledger.view"
    access_change_permission = "ledger.create_entry"

    @staticmethod
    def editable_in_admin(obj):
        """Drafts with functional-currency, normal lines only; everything else is changed through services."""
        if obj.status != JournalEntry.Status.DRAFT or obj.document_type:
            return False
        return not obj.lines.exclude(currency_id=obj.entity.base_currency_id).exists() and not obj.lines.exclude(
            line_kind=JournalLine.LineKind.NORMAL
        ).exists()

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return fields + ["entity"] if obj is not None and "entity" not in fields else fields

    def has_change_permission(self, request, obj=None):
        return (obj is None or self.editable_in_admin(obj)) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return (obj is None or obj.status == JournalEntry.Status.DRAFT) and self._can_change(request, obj)

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except _ServiceRefused as refused:  # the admin's transaction rolled back; nothing was written
            exc = refused.exc
            text = " ".join(getattr(exc, "messages", None) or [str(exc)])
            self.message_user(request, f"Not saved / لم يُحفظ: {text}", messages.ERROR)
            return HttpResponseRedirect(request.get_full_path())

    def save_model(self, request, obj, form, change):
        """Nothing is saved here: save_related passes header and lines to ledger.services."""

    def save_formset(self, request, form, formset, change):
        """Lines are saved by ledger.services in save_related."""
        formset.new_objects, formset.changed_objects, formset.deleted_objects = [], [], []

    def save_related(self, request, form, formsets, change):
        obj = form.instance
        lines = []
        for formset in formsets:
            for line_form in formset.forms:
                data = getattr(line_form, "cleaned_data", None) or {}
                if not data or data.get("DELETE") or not data.get("account"):
                    continue
                lines.append({
                    "account": data["account"], "debit": data.get("debit") or 0, "credit": data.get("credit") or 0,
                    "description": data.get("description") or "", "counterparty": data.get("counterparty_entity"),
                })
        try:
            if change:
                entry = services.update_draft(
                    obj, user=request.user, date=obj.date, description=obj.description, reference=obj.reference, lines=lines,
                )
            else:
                entry = services.create_entry(
                    obj.entity, obj.date, obj.description, lines, user=request.user, reference=obj.reference,
                )
        except (ValidationError, PermissionDenied) as exc:
            raise _ServiceRefused(exc) from exc
        obj.pk = obj.id = entry.pk
        for formset in formsets:
            self.save_formset(request, form, formset, change)

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
                grant = access.require(request.user, entry.entity, "ledger.reverse_entry", document=entry)
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
