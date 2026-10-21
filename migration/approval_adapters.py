"""Migration's adapter to the one approval engine (access.md §10.5, R61). Registered in ``MigrationConfig.ready``.
The legacy account map is never self-approved (R80 h: the opening balances must tie out for the external auditor).

R13 as amended (M7, AT-25): the preparer submits the draft map (``services.submit_account_map``, submit right
``migration.manage_account_map``), which opens its request; the approver decides it in the engine inbox
(``engine.decide`` / ``services.decide_account_map``). The approve service refuses any other caller. The posting of
the imported drafts goes through the ledger's own adapter (activity ``ledger.manual_entry``, migration R16)."""


def _version(amap):
    from core.access import engine

    return engine.version_text(getattr(amap, "modified_at", None) or getattr(amap, "created_at", None) or "")


def _label(amap):
    """Number and entity only (no personal data); version when known."""
    version = getattr(amap, "version", None)
    which = f"v{version}" if version else f"#{amap.pk}"
    return (f"Legacy account map {amap.entity.code} {which}", f"خريطة حسابات {amap.entity.code} {which}")


def register():
    from core.access import engine

    from . import services
    from .models import AccountMap

    engine.register(AccountMap, "migration.account_map", permission="migration.approve_account_map",
                    submit_permission="migration.manage_account_map", amount=lambda d: None, version=_version,
                    created_at=lambda d: getattr(d, "created_at", None),
                    label=_label,
                    approve=lambda d, user: services.approve_account_map(d, user=user),
                    return_=lambda d, user, note: services.return_account_map(d, user=user, note=note),
                    reject=lambda d, user, note: services.return_account_map(d, user=user, note=note))
