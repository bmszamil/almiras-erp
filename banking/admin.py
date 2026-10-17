"""Thin, entity-scoped, read-only admin for banking (access R2, R24; banking R1, R4).

Every change goes through ``banking.services``; the admin shows records scoped to the entities where the user
holds ``banking.view``. IBANs, account numbers, counterparty names and IBANs and narratives are never shown in
full here: labels and columns use the masked form (R4). Unmasked values are only returned by
``banking.services.reveal_iban``, which checks ``banking.view_iban`` and logs the view.
"""
from django.contrib import admin

from core.admin import EntityScopedAdmin

from .models import (
    Bank,
    BankAccount,
    BankingEvent,
    BankTransaction,
    Match,
    Reconciliation,
    StatementImport,
    StatementLine,
)


class ReadOnlyBankingAdmin(EntityScopedAdmin):
    access_view_permission = "banking.view"
    access_change_permission = None

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Bank)
class BankAdmin(admin.ModelAdmin):
    list_display = ["code", "name_en", "name_ar", "country", "sa_bank_code", "swift_bic", "verified", "is_active"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BankAccount)
class BankAccountAdmin(ReadOnlyBankingAdmin):
    list_display = ["code", "entity", "name_en", "name_ar", "bank", "masked_number", "currency", "gl_account", "status"]
    list_filter = [("entity", admin.RelatedOnlyFieldListFilter), "status"]
    fields = ["entity", "code", "name_en", "name_ar", "bank", "masked_number", "currency", "gl_account",
              "sharia_basis", "statement_format", "opening_statement_date", "opening_statement_balance",
              "active_from", "active_to", "status", "warnings", "created_by", "approved_by", "approved_at"]
    readonly_fields = fields

    @admin.display(description="IBAN / account (masked) / الآيبان أو الحساب (مخفي)")
    def masked_number(self, obj):
        return obj.masked_number


@admin.register(StatementImport)
class StatementImportAdmin(ReadOnlyBankingAdmin):
    list_display = ["bank_account", "statement_id", "sequence_no", "period_start", "period_end", "opening_balance",
                    "closing_balance", "line_count", "imported_by", "imported_at"]
    exclude = ["file_content"]


@admin.register(StatementLine)
class StatementLineAdmin(ReadOnlyBankingAdmin):
    list_display = ["bank_account", "statement", "line_no", "booking_date", "direction", "amount", "currency",
                    "bank_reference"]
    exclude = ["counterparty_name", "counterparty_iban", "narrative", "line_hash"]


@admin.register(Match)
class MatchAdmin(ReadOnlyBankingAdmin):
    list_display = ["bank_account", "rule", "confidence", "status", "auto", "proposed_by", "confirmed_by",
                    "reconciliation"]


@admin.register(BankTransaction)
class BankTransactionAdmin(ReadOnlyBankingAdmin):
    list_display = ["number", "entity", "bank_account", "kind", "date", "direction", "amount", "currency", "status",
                    "requires_purification", "source"]
    list_filter = [("entity", admin.RelatedOnlyFieldListFilter), "kind", "status"]


@admin.register(Reconciliation)
class ReconciliationAdmin(ReadOnlyBankingAdmin):
    list_display = ["bank_account", "period", "as_of", "bank_balance", "book_balance", "difference", "status",
                    "prepared_by", "approved_by"]


@admin.register(BankingEvent)
class BankingEventAdmin(ReadOnlyBankingAdmin):
    list_display = ["at", "entity", "event", "actor", "actor_kind", "object_type", "object_id"]
    list_filter = [("entity", admin.RelatedOnlyFieldListFilter), "event"]
