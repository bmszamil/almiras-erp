"""Fixed assets' side of the migration sub-ledger loader (fixed-assets R40, R41; migration R22 to R25, W11).

The migration app (``migration.subledgers``, section ``fixed_assets``) reads the workbook's Fixed assets sheet and calls
:func:`load_opening_asset` inside its own transaction; only this module writes the register:

- one **active** asset per row, ``source="import"``, with its legacy code, dates, one ``opening_balance`` component
  (the cost), the opening accumulated depreciation, residual value and the legacy method, life and remaining life
  (kept prospectively, OQ-20); ``opening_date`` is the cut-over date. No journal entry and no approval request: the
  balances are posted by the migration opening entry (approved through the engine, migration R16).
- a re-run with the same values is skipped; other values are refused (``opening_asset_changed``); once the entity's
  opening entry is posted nothing is loaded (``opening_posted``; the opening rows are locked first, R25).

:func:`opening_register_balances` is the R22 source registered in ``MigrationConfig.ready``: the opening entry posts
only when its lines on the asset accounts equal the loaded register to the halala (R41).
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from core import access
from core.access import services as access_services
from core.access.models import Kind
from ledger import services as ledger

from . import calc, services, template
from .messages import error
from .models import AssetCategory, AssetCostComponent, AssetLocation, FixedAsset, HeldFor, Method, Usage

ZERO = Decimal("0")
HELD_FOR = {Usage.OWN_USE: HeldFor.OWN_USE, Usage.RENTAL: HeldFor.RENTAL, Usage.INVESTMENT_PROPERTY: HeldFor.RENTAL}
GATED_CODES = frozenset(template.RESTRICTED_CODES)  # R41: 1500, 1510, 1520, 1590 and the proposed 15xx codes


def _human(user, entity):
    if access_services.kind_of(user) != Kind.HUMAN:  # R40: a person, never an agent or system user
        access.deny(user, entity, "migration.import", "agent_forbidden")


def refuse_after_opening(entity):
    """Migration R25: nothing is added to what was tied out once the opening entry is posted (rows locked first)."""
    if ledger.migration_opening_posted(entity, lock=True):
        raise error("opening_posted", entity=entity.code)


def _category(entity, asset_class, cost_account):
    found = AssetCategory.objects.filter(entity=entity, asset_class=asset_class, cost_account=cost_account,
                                         is_active=True).order_by("code").first()
    if found is None:
        raise error("opening_category", entity=entity.code, asset_class=asset_class,
                    code=getattr(cost_account, "code", cost_account))
    return found


def _location(entity, user, name):
    name = (name or "").strip()[:120]
    found = AssetLocation.objects.filter(entity=entity, name_ar=name).first()
    if found is not None:
        return found
    code = f"L{AssetLocation.objects.filter(entity=entity).count() + 1:04d}"
    return services._create_location(entity, user, code, name)


def _same(asset, values):
    return all(getattr(asset, k) == v for k, v in values.items())


def load_opening_asset(entity, *, user, legacy_code, description_ar, description_en="", asset_class, usage,
                       location, cost_account, acquisition_date, available_for_use_date=None, cost,
                       accumulated_depreciation=None, residual_value=None, method=None, useful_life_months=None,
                       remaining_life_months=None, serial_or_plate="", custodian_ref="", cutover,
                       source_system="falcon"):
    """Load one opening asset (R40). ``user`` holds ``migration.import`` in ``entity``. Returns (asset, created)."""
    access.require(user, entity, "migration.import")
    _human(user, entity)
    with transaction.atomic():  # R25: the opening lock is held until the asset is written
        refuse_after_opening(entity)
        category = _category(entity, asset_class, cost_account)
        cost = services._money(cost, "Cost", "التكلفة", positive=True)
        accumulated = services._money(accumulated_depreciation or ZERO, "Accumulated depreciation", "مجمع الاستهلاك")
        residual = services._money(residual_value or ZERO, "Residual value", "القيمة المتبقية")
        if not category.depreciable:
            accumulated, method, useful_life_months, remaining_life_months = ZERO, "", None, None
        elif method not in Method.values:
            method = Method.STRAIGHT_LINE
        values = {
            "category": category, "description_ar": (description_ar or "")[:300],
            "description_en": (description_en or "")[:300], "usage": usage, "acquisition_date": acquisition_date,
            "available_for_use_date": available_for_use_date, "cost": cost,
            "opening_accumulated_depreciation": accumulated, "residual_value": residual, "method": method or "",
            "useful_life_months": useful_life_months, "opening_remaining_life_months": remaining_life_months,
            "opening_date": cutover, "serial_or_plate": serial_or_plate or "", "custodian_ref": custodian_ref or "",
        }
        existing = FixedAsset.objects.filter(entity=entity, legacy_source_system=source_system,
                                             legacy_code=legacy_code).first()
        if existing is not None:
            if not _same(existing, values):
                raise error("opening_asset_changed", legacy_code=legacy_code)
            return existing, False
        settings = services._settings(entity)
        asset = FixedAsset(
            entity=entity, code=services._next_number(entity, "FA"), location=_location(entity, user, location),
            held_for=HELD_FOR.get(usage, HeldFor.OWN_USE), status=FixedAsset.Status.ACTIVE, source="import",
            legacy_source_system=source_system, legacy_code=legacy_code, retention_class=category.retention_class,
            created_by=user, approved_by=None, **values)
        services._save(asset)
        services._save(AssetCostComponent(entity=entity, asset=asset, kind=AssetCostComponent.Kind.OPENING_BALANCE,
                                          amount=cost, accounting_date=cutover, created_by=user))
        services._refresh_dates(asset, settings)
        services._save(asset)
        return asset, True


def opening_register_balances(entity, source_system=None):
    """The R22 source: {(account code, ""): signed SAR, debit positive}: Σ cost of the loaded assets per category
    cost account, −Σ opening accumulated depreciation per accumulated account. Internal (no access check): the
    migration posting gate calls it for an entry it is already authorised to post."""
    out = {}
    assets = FixedAsset.objects.filter(entity=entity).exclude(legacy_code="")
    if source_system:
        assets = assets.filter(legacy_source_system=source_system)
    for row in (AssetCostComponent.objects.filter(asset__in=assets, kind=AssetCostComponent.Kind.OPENING_BALANCE)
                .values("asset__category__cost_account__code").annotate(t=Sum("amount"))):
        key = (row["asset__category__cost_account__code"], "")
        out[key] = out.get(key, ZERO) + Decimal(row["t"] or ZERO)
    for row in (assets.exclude(category__accum_depr_account__isnull=True)
                .values("category__accum_depr_account__code").annotate(t=Sum("opening_accumulated_depreciation"))):
        key = (row["category__accum_depr_account__code"], "")
        out[key] = out.get(key, ZERO) - Decimal(row["t"] or ZERO)
    return {k: calc.q(v) for k, v in out.items()}
