"""Ledger admin: entity-scoped, audited, and every state change goes through ledger.services.

- Manual entries are created here as drafts needing approval; "post" applies only to system entries
  and needs ledger.entry.post in the entity. Manual entries are posted by final approval (D11).
- Reversal needs ledger.entry.reverse or ledger.entry.post (D15) and a reason.
- System drafts (made by module services) cannot be edited here, header or lines.
- Periods: add with ledger.period.manage; close through the service; reopening is not offered (default deny).
"""
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from django.template.response import TemplateResponse

from access import services as access
from access.admin_scoping import ScopedAdmin, admin_ctx, setup_mode

from . import services
from .models import Account, FiscalPeriod, JournalEntry, JournalLine

S = JournalEntry.Status


@admin.register(Account)
class AccountAdmin(ScopedAdmin):
    view_perm = "ledger.entry.view"
    list_display = ["code", "name_en", "name_ar", "type", "entity", "is_postable", "is_intercompany", "sharia_prohibited"]
    list_filter = ["type", "sharia_prohibited", "is_intercompany"]
    search_fields = ["code", "name_en", "name_ar"]

    def has_add_permission(self, request, *args):
        return setup_mode(request) or self.erp_entities(request, "ledger.account.manage").exists()

    def has_change_permission(self, request, obj=None):
        if setup_mode(request):
            return True
        managed = self.erp_entities(request, "ledger.account.manage")
        return managed.filter(pk=obj.entity_id).exists() if obj is not None else managed.exists()

    def get_readonly_fields(self, request, obj=None):
        return ["entity"] if obj is not None else []

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        managed = self.erp_entities(request, "ledger.account.manage")
        if db_field.name == "entity" and not setup_mode(request):
            kwargs["queryset"] = managed
        if db_field.name == "parent" and not setup_mode(request):
            kwargs["queryset"] = Account.objects.filter(entity__in=managed)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(FiscalPeriod)
class FiscalPeriodAdmin(ScopedAdmin):
    view_perm = "ledger.entry.view"
    list_display = ["entity", "name", "start_date", "end_date", "status"]
    list_filter = ["status"]
    readonly_fields = ["status"]
    actions = ["close_selected"]

    def has_add_permission(self, request, *args):
        return self.erp_entities(request, "ledger.period.manage").exists()

    def has_change_permission(self, request, obj=None):
        return False  # status moves only through close_selected / ledger.services; no reopen here

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "entity":
            kwargs["queryset"] = self.erp_entities(request, "ledger.period.manage")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.action(description="Close selected periods / إقفال الفترات المحددة")
    def close_selected(self, request, queryset):
        for period in queryset:
            try:
                services.close_period(period, user=request.user)
                self.message_user(request, f"Closed {period}.")
            except Exception as exc:
                self.message_user(request, f"{period}: {exc}", messages.ERROR)


def _entry_editable(request, entry):
    """Manual drafts only, only by their creator (review A), who still needs ledger.entry.create there."""
    return (
        entry.status == S.DRAFT
        and entry.requires_approval
        and entry.created_by_id == request.user.pk
        and access.has_perm(request.user, entry.entity, "ledger.entry.create")
    )


class JournalLineFormSet(BaseInlineFormSet):
    """Lines must use accounts of the entry's own entity (review F)."""

    def clean(self):
        super().clean()
        entity_id = self.instance.entity_id
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or form.cleaned_data.get("DELETE"):
                continue
            account = form.cleaned_data.get("account")
            if account is not None and account.entity_id != entity_id:
                raise ValidationError("Every line must use an account of the entry's entity. / "
                                      "يجب أن تكون حسابات البنود من منشأة القيد.")


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    formset = JournalLineFormSet
    extra = 2
    fields = ["account", "description", "debit", "credit", "counterparty_entity"]

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        if obj is not None and "account" in formset.form.base_fields:
            field = formset.form.base_fields["account"]
            field.queryset = field.queryset.filter(entity_id=obj.entity_id)
        return formset

    def _can_edit(self, request, obj):
        if obj is None:
            return access.entities_with_perm(request.user, "ledger.entry.create").exists()
        return _entry_editable(request, obj)

    def has_view_permission(self, request, obj=None):
        return True  # the parent admin already scoped the entry

    def has_change_permission(self, request, obj=None):
        return self._can_edit(request, obj)

    def has_add_permission(self, request, obj=None):
        return self._can_edit(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._can_edit(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "account":
            kwargs["queryset"] = Account.objects.filter(
                entity__in=access.entities_with_perm(request.user, "ledger.entry.create"), is_postable=True, is_active=True
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(JournalEntry)
class JournalEntryAdmin(ScopedAdmin):
    view_perm = "ledger.entry.view"
    list_display = ["number", "entity", "date", "description", "status", "source", "requires_approval", "total_debit"]
    list_filter = ["status", "source", "requires_approval"]
    search_fields = ["number", "description", "reference"]
    inlines = [JournalLineInline]
    actions = ["submit_selected", "post_selected", "reverse_selected"]

    def get_readonly_fields(self, request, obj=None):
        ro = ["number", "status", "source", "requires_approval", "period", "posted_by", "posted_at",
              "reversal_of", "created_by"]
        return ro + ["entity"] if obj is not None else ro

    def has_add_permission(self, request, *args):
        return access.entities_with_perm(request.user, "ledger.entry.create").exists()

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return self.has_add_permission(request)
        return _entry_editable(request, obj)

    def has_delete_permission(self, request, obj=None):
        return obj is not None and _entry_editable(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "entity":
            kwargs["queryset"] = access.entities_with_perm(request.user, "ledger.entry.create")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
            obj.source = request.user.source
            obj.requires_approval = True  # manual entries are posted by approval (D11)
        elif not _entry_editable(request, obj):
            raise PermissionError("Entry is not editable.")
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        services.record_editor(form.instance, request.user, "edit" if change else "create")

    def audit_extra(self, obj):
        if obj is None or obj.pk is None:
            return {}
        return {"lines": [[l.account_id, str(l.debit), str(l.credit), l.counterparty_entity_id]
                          for l in obj.lines.order_by("id")]}

    def audit_action(self, obj, before, after, change):
        return "entry.update" if change else "entry.create"

    @admin.action(description="Submit selected drafts for approval / تقديم المسودات للاعتماد")
    def submit_selected(self, request, queryset):
        from approvals.services import submit

        for entry in queryset:
            try:
                r = submit(entry, request.user, ctx=admin_ctx(request))
                self.message_user(request, f"Entry {entry.pk} submitted (request {r.pk}).")
            except Exception as exc:
                self.message_user(request, f"Entry {entry.pk}: {exc}", messages.ERROR)

    @admin.action(description="Post selected system entries / ترحيل قيود النظام المحددة")
    def post_selected(self, request, queryset):
        for entry in queryset:
            if entry.requires_approval:
                self.message_user(request, f"Entry {entry.pk}: manual entries are posted by approval.", messages.ERROR)
                continue
            if not access.has_perm(request.user, entry.entity, "ledger.entry.post"):
                self.message_user(request, f"Entry {entry.pk}: you need ledger.entry.post in {entry.entity.code}.",
                                  messages.ERROR)
                continue
            try:
                services.post_entry(entry, user=request.user)
                self.message_user(request, f"Posted {entry.pk}.")
            except Exception as exc:  # show the rule that failed
                self.message_user(request, f"Entry {entry.pk}: {exc}", messages.ERROR)

    @admin.action(description="Reverse selected posted entries (reason required) / عكس القيود")
    def reverse_selected(self, request, queryset):
        reason = (request.POST.get("reason") or "").strip()
        if not request.POST.get("reason_confirmed") or not reason:
            if request.POST.get("reason_confirmed"):
                self.message_user(request, "A reason is required. / السبب مطلوب.", messages.ERROR)
            return TemplateResponse(request, "admin/ledger/reverse_reason.html",
                                    {**self.admin_site.each_context(request), "entries": list(queryset),
                                     "title": "Reverse entries / عكس القيود"})
        for entry in queryset:
            try:
                rev = services.reverse_entry(entry, user=request.user, reason=reason)
                self.message_user(request, f"Reversed {entry.number} with {rev.number}.")
            except Exception as exc:
                self.message_user(request, f"{entry.number or entry.pk}: {exc}", messages.ERROR)
        return None
