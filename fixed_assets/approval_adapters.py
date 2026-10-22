"""Fixed assets' adapters to the one approval engine (fixed-assets.md R36, R37; access §10.5, R61). Registered in
``FixedAssetsConfig.ready``.

Each adapter tells the engine a document's activity, approval and submit permissions, amount (SAR, the band measure,
access R73), currency (the entity's functional currency), version (a changed document is approved again), label
(number only, no personal data) and the module services the engine inbox calls. A model approved under several
activities registers one adapter per activity, picked by ``permission`` (as invoicing does for purification
transfers). The main adapter of each model is registered first (``engine._main_permission``).
"""


def _functional(document):
    return document.entity.base_currency_id


def _reversal_version(document):
    from core.access.engine import version_text

    return f"rev:{version_text(document.reversal_requested_at or '')}"


def _reversal_people(document):
    return [document.reversal_requested_by_id]


def register():
    from core.access import engine

    from . import services
    from .models import AssetEvent, DepreciationPolicy, DepreciationRun, FixedAsset

    engine.register(
        DepreciationPolicy, "fixed_assets.policy", permission="fixed_assets.approve_policy",
        submit_permission="fixed_assets.maintain_policy", amount=lambda d: None, currency=_functional,
        label=lambda d: (f"Depreciation policy {d.category.code} from {d.effective_from}",
                         f"سياسة استهلاك {d.category.code} من {d.effective_from}"),
        approve=lambda d, user: services.approve_policy(d, user=user),
        return_=lambda d, user, note: services.return_policy(d, user=user, note=note),
    )
    engine.register(
        FixedAsset, "fixed_assets.capitalisation", permission="fixed_assets.capitalise",
        submit_permission="fixed_assets.submit_asset", amount=lambda d: d.cost, currency=_functional,
        functional_amount=lambda d: d.cost,
        label=lambda d: (f"Asset capitalisation {d.code}", f"رسملة الأصل {d.code}"),
        approve=lambda d, user: services.activate_asset(d, user=user),
        return_=lambda d, user, note: services.return_asset(d, user=user, note=note),
    )
    engine.register(
        DepreciationRun, "fixed_assets.depreciation_run", permission="fixed_assets.run_depreciation",
        submit_permission="fixed_assets.prepare_depreciation", amount=lambda d: d.total, currency=_functional,
        functional_amount=lambda d: d.total,
        label=lambda d: (f"Depreciation run {d.number}", f"مسيّر استهلاك {d.number}"),
        approve=lambda d, user: services.post_depreciation(d, user=user),
        return_=lambda d, user, note: services.return_depreciation(d, user=user, note=note),
    )
    engine.register(
        DepreciationRun, "fixed_assets.reversal", permission="fixed_assets.reverse_document",
        submit_permission="fixed_assets.prepare_depreciation", amount=lambda d: d.total, currency=_functional,
        functional_amount=lambda d: d.total, version=_reversal_version, people=_reversal_people,
        label=lambda d: (f"Reversal of depreciation run {d.number}", f"عكس مسيّر الاستهلاك {d.number}"),
        approve=lambda d, user: services.reverse_document(d, user=user),
        return_=lambda d, user, note: services.cancel_reversal(d, user=user, note=note),
    )
    for activity, permission in (("fixed_assets.capitalisation", "fixed_assets.capitalise"),
                                 ("fixed_assets.impairment", "fixed_assets.impair"),
                                 ("fixed_assets.estimate_change", "fixed_assets.change_estimate"),
                                 ("fixed_assets.disposal", "fixed_assets.dispose")):
        engine.register(
            AssetEvent, activity, permission=permission, submit_permission="fixed_assets.record_event",
            amount=lambda d: abs(d.amount) if d.amount is not None else None, currency=_functional,
            functional_amount=lambda d: abs(d.amount) if d.amount is not None else None,
            label=lambda d: (f"Asset event #{d.pk} ({d.kind}) {d.asset.code}",
                             f"حدث أصل رقم {d.pk} ({d.kind}) {d.asset.code}"),
            approve=lambda d, user, p=permission: services.approve_event(d, user=user, permission=p),
            return_=lambda d, user, note: services.return_event(d, user=user, note=note),
        )
    engine.register(
        AssetEvent, "fixed_assets.reversal", permission="fixed_assets.reverse_document",
        submit_permission="fixed_assets.record_event",
        amount=lambda d: abs(d.amount) if d.amount is not None else None, currency=_functional,
        functional_amount=lambda d: abs(d.amount) if d.amount is not None else None,
        version=_reversal_version, people=_reversal_people,
        label=lambda d: (f"Reversal of asset event #{d.pk}", f"عكس حدث الأصل رقم {d.pk}"),
        approve=lambda d, user: services.reverse_document(d, user=user),
        return_=lambda d, user, note: services.cancel_reversal(d, user=user, note=note),
    )
