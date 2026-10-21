"""Migration's adapter to the one approval engine (access.md §10.5, R61). Registered in ``MigrationConfig.ready``.
The legacy account map is never self-approved (R80 h: the opening balances must tie out for the external auditor)."""


def register():
    from core.access import engine

    from . import services
    from .models import AccountMap

    engine.register(AccountMap, "migration.account_map", permission="migration.approve_account_map",
                    amount=lambda d: None, label=lambda d: (f"Legacy account map #{d.pk}", f"خريطة حسابات رقم {d.pk}"),
                    approve=lambda d, user: services.approve_account_map(d, user=user))
