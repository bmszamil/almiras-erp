"""Banking's adapters to the one approval engine (access.md §10.5, R61). Registered in ``BankingConfig.ready``.
Bank accounts and their changes are S! (never self-approved, R80 d); reconciliations are NSA (R80 i)."""


def register():
    from core.access import engine

    from . import services
    from .models import BankAccount, BankAccountChange, BankTransaction, Reconciliation

    engine.register(BankAccount, "banking.bank_account", permission="banking.approve_bank_account",
                    amount=lambda d: None, label=lambda d: (f"Bank account #{d.pk}", f"حساب بنكي رقم {d.pk}"),
                    approve=lambda d, user: services.approve_bank_account(d, user=user))
    engine.register(BankAccountChange, "banking.bank_account_change", permission="banking.approve_bank_account",
                    version=lambda d: engine.version_text(getattr(d, "created_at", "")),  # a change request is never edited
                    amount=lambda d: None,
                    label=lambda d: (f"Bank account change #{d.pk}", f"تعديل حساب بنكي رقم {d.pk}"),
                    approve=lambda d, user: services.approve_bank_account_change(d, user=user))
    engine.register(BankTransaction, "banking.bank_transaction", permission="banking.approve_bank_transaction",
                    submit_permission="banking.submit_bank_transaction",
                    amount=lambda d: d.amount, currency=lambda d: d.currency_id,
                    label=lambda d: (f"Bank transaction {d.number or '#' + str(d.pk)}",
                                     f"عملية بنكية {d.number or '#' + str(d.pk)}"),
                    approve=lambda d, user: services.approve_bank_transaction(d, user=user))
    engine.register(Reconciliation, "banking.reconciliation", permission="banking.approve_reconciliation",
                    created_at=lambda r: r.prepared_at,  # R81: prepared, not created (review of 49e1abd, item 6)
                    submit_permission="banking.prepare_reconciliation",
                    amount=lambda d: None, label=lambda d: (f"Bank reconciliation #{d.pk}", f"تسوية بنكية رقم {d.pk}"),
                    approve=lambda d, user: services.approve_reconciliation(d, user=user))
