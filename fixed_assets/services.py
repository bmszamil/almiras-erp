"""Fixed-asset services (docs/specs/fixed-assets.md, slice 1). The only way to change the register.

Every public function checks the caller's right in the record's entity (``core.access.require``, R29); approvals are
decided through the one approval engine only (R36, access R61): the approve services refuse a direct call
(``approval_request_required``). Postings go through ``ledger.services.create_entry(post=True, grant=, document=)``
with the document's registered ``build_lines`` (R38); reversals through ``ledger.services.reverse_entry`` (R18).
"""
import hashlib
import json
from contextvars import ContextVar
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from core import access
from core.access import engine
from core.access import services as access_services
from core.access.models import AccessLog, Kind
from core.models import Entity
from invoicing import fixed_assets_port as port
from ledger import control_accounts
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine, LedgerError

from . import calc, template
from .messages import FixedAssetError, ROLE_NAMES, bi, error, problems_error, text
from .models import (
    IP_CLASSES, NON_DEPRECIABLE_CLASSES, REAL_ESTATE_CLASSES, AssetCategory, AssetClass, AssetCostComponent,
    AssetEvent, AssetLocation, AssetSequence, DepreciationLine, DepreciationPolicy, DepreciationRun, FixedAsset,
    FixedAssetSettings, HeldFor, IpFairValue, Method, RetentionClass, Usage, VatUse,
)

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")
SAR = "SAR"
AS = FixedAsset.Status
ES = AssetEvent.Status
EK = AssetEvent.Kind
RS = DepreciationRun.Status
BOOKED = (JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED)

# R38: the permission that approves (and, for posting kinds, posts) each event kind.
KIND_PERMISSION = {
    EK.FINANCED_ACQUISITION: "fixed_assets.capitalise",
    EK.CAPITALISE_CWIP: "fixed_assets.capitalise",
    EK.SUBSEQUENT_COST: "fixed_assets.capitalise",
    EK.IMPAIRMENT: "fixed_assets.impair",
    EK.IMPAIRMENT_REVERSAL: "fixed_assets.impair",
    EK.WRITE_OFF: "fixed_assets.dispose",
    EK.CHANGE_OF_ESTIMATE: "fixed_assets.change_estimate",
    EK.VAT_USE_CHANGE: "fixed_assets.change_estimate",
}
POSTING_KINDS = frozenset({EK.FINANCED_ACQUISITION, EK.CAPITALISE_CWIP, EK.IMPAIRMENT, EK.IMPAIRMENT_REVERSAL,
                           EK.WRITE_OFF})
NOT_BUILT = {EK.REVALUATION: ("not built until OQ-8 (revaluation model; Needs expert sign-off: auditor)",
                              "غير متاحة حتى يُحسم السؤال 8 (نموذج إعادة التقييم)"),
             EK.FAIR_VALUE_CHANGE: ("every entity uses the cost model until OQ-6; record fair values instead (R48)",
                                    "تطبق كل الكيانات نموذج التكلفة حتى يُحسم السؤال 6؛ سجّل القيم العادلة بدلًا من ذلك")}
SLICE_2 = frozenset({EK.DISPOSAL_SALE, EK.INTERCOMPANY_SALE, EK.RECLASSIFICATION})
FINANCING_COST_CODES = frozenset({"5600", "5610"})  # R7: financing cost (Islamic and conventional)
VIEW = "fixed_assets.view"

ACCOUNT_NAMES = {code: (en, ar) for code, en, ar, _type in template.PROPOSED_ACCOUNTS}
ACCOUNT_NAMES.update({"1500": ("Property, plant and equipment", "الممتلكات والآلات والمعدات"),
                      "1510": ("Investment property (rental assets)", "عقارات استثمارية"),
                      "1520": ("Vehicles", "السيارات"), "1590": ("Accumulated depreciation", "مجمع الاستهلاك")})


# --- helpers -------------------------------------------------------------------------------------------------------

def _now():
    return access_services.now()


def _today():
    return timezone.localdate(_now())


def _source(user):
    return access_services.source_of(user) or "human"


def _human(user, entity, permission):
    """R35: agents (and system users) draft only; a person submits, decides, posts and reverses."""
    if access_services.kind_of(user) != Kind.HUMAN:
        access.deny(user, entity, permission, "agent_forbidden")


def _log_agent(user, entity, permission, document, action):
    """Gateway rule: every agent action is logged (access R21)."""
    if access_services.kind_of(user) == Kind.AGENT:
        access_services.log(AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=entity, permission=permission,
                            details={"document": [document._meta.label_lower, document.pk], "action": action})


def _save(obj, **kwargs):
    obj._service_write = True
    obj.save(**kwargs)
    return obj


def _touch(obj, user):
    obj.modified_by, obj.modified_at = user, _now()
    obj.version = (obj.version or 0) + 1


def _money(value, what_en, what_ar, *, positive=False, allow_none=False):
    """A SAR amount typed into the module: Decimal (never float), at most 2 dp (R39, CLAUDE.md rule 7)."""
    if value in (None, ""):
        if allow_none:
            return None
        raise error("amount_positive", what=what_en, what_ar=what_ar)
    if isinstance(value, float):
        raise error("amount_decimals", what=what_en, what_ar=what_ar, amount=repr(value))
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise error("amount_decimals", what=what_en, what_ar=what_ar, amount=str(value)) from None
    if not amount.is_finite() or amount != amount.quantize(CENT):
        raise error("amount_decimals", what=what_en, what_ar=what_ar, amount=amount)
    if positive and amount <= ZERO:
        raise error("amount_positive", what=what_en, what_ar=what_ar)
    if amount < ZERO:
        raise error("amount_positive", what=what_en, what_ar=what_ar)
    return amount.quantize(CENT)


def _same_entity(entity, obj, what_en, what_ar):
    if obj is not None and getattr(obj, "entity_id", None) != entity.pk:
        raise error("other_entity", what=what_en, what_ar=what_ar)


def _through_engine(user, entity, permission):
    """access R61: fixed-asset approvals are decided in the engine inbox (``engine.decide``), never by a direct call."""
    if not engine.deciding():
        engine.refuse("approval_request_required", user, entity, permission, {"why": "direct_call"}, remaining=1)


def _lock(model, pk):
    return model._base_manager.select_for_update(of=("self",)).select_related("entity").get(pk=pk)


def _next_number(entity, kind, year=0):
    seq, _ = AssetSequence.objects.select_for_update().get_or_create(entity=entity, kind=kind, year=year)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    if kind == "FA":
        return f"{entity.code}-FA-{seq.last_number:06d}"
    return f"{entity.code}-{kind}-{year}-{seq.last_number:06d}"


def _settings(entity):
    found = FixedAssetSettings.objects.filter(entity=entity).first()
    if found is None:
        raise error("setup_missing", entity=entity.code)
    return found


def _period_of(entity, on):
    period = FiscalPeriod.objects.filter(entity=entity, start_date__lte=on, end_date__gte=on).first()
    if period is None:
        raise error("no_period", entity=entity.code, on=on)
    return period


def _r7_code(entity):
    """R7 scope: Sharia-enforcing entities and every IFRS for SMEs entity refuse (Sharia message); a full-IFRS entity
    without Sharia enforcement: capitalising borrowing costs is not built (OQ-9, OQ-24)."""
    if entity.sharia_enforced or entity.reporting_standard == Entity.Standard.IFRS_SME:
        return "financing_cost_capitalised"
    return "not_built_oq9"


def _financing_account(account):
    return account.code in FINANCING_COST_CODES or bool(account.sharia_prohibited)


def _role(role):
    en, ar = ROLE_NAMES[role]
    return {"role": en, "role_ar": ar}


def _need_account(category, account, role):
    """R45: a posting that needs an account the category does not name yet is refused; nothing falls back."""
    if account is None:
        raise error("account_not_set", category=category.label, **_role(role))
    return account


# --- setup: categories, locations, settings (3.1, 3.7, 3.9, R31) ----------------------------------------------------

def _account(entity, value):
    if value is None or value == "":
        return None
    if isinstance(value, Account):
        account = value
    else:
        account = Account.objects.filter(entity=entity, code=str(value)).first()
        if account is None:
            raise error("category_account", code=value, role="", role_ar="",
                        why=f"it is not in {entity.code}'s chart", why_ar=f"ليس في دليل حسابات {entity.code}")
    _same_entity(entity, account, f"Account {account.code}", f"الحساب {account.code}")
    return account


def _category_problems(entity, category):
    """R1, R6, R9, R45, R49 for a category's accounts and class."""
    out = []

    def bad(account, role, why_en, why_ar):
        out.append(bi("category_account", code=account.code, why=why_en, why_ar=why_ar, **_role(role)))

    roles = (("cost", category.cost_account), ("accumulated depreciation", category.accum_depr_account),
             ("depreciation expense", category.depr_expense_account),
             ("accumulated impairment", category.accum_impairment_account),
             ("impairment expense", category.impairment_expense_account), ("disposal", category.disposal_account))
    for role, account in roles:
        if account is None:
            continue
        if account.entity_id != entity.pk:
            out.append(bi("other_entity", what=f"Account {account.code}", what_ar=f"الحساب {account.code}"))
            continue
        if account.code == template.BAI_AJEL_ASSET:
            out.append(bi("bai_ajel_asset"))
            continue
        if not account.is_postable or not account.is_active:
            bad(account, role, "it is not open for posting", "غير مفتوح للترحيل")
        if control_accounts.is_subledger_control(account) or control_accounts.is_tax_control(account):
            bad(account, role, "it is a control or tax-control account", "حساب مراقبة أو حساب ضريبي")
        if account.sharia_prohibited:
            bad(account, role, "it is Sharia-prohibited", "محظور شرعًا")
        if role == "cost" and account.code not in template.COST_CODES:
            bad(account, role, "asset cost accounts are the restricted asset accounts (R9)",
                "حسابات تكلفة الأصول هي حسابات الأصول المقيدة")
        if role in ("accumulated depreciation", "accumulated impairment") and account.code not in \
                template.ACCUMULATED_CODES:
            bad(account, role, "accumulated accounts are the restricted contra accounts (R9)",
                "حسابات المجمع هي حسابات الأصول المقيدة المقابلة")
    if category.cost_account is None:
        out.append(bi("account_not_set", category=category.code, **_role("cost")))
    elif (category.cost_account.code in template.IP_COST_CODES) != (category.asset_class in IP_CLASSES):
        out.append(bi("ip_account_class"))  # R49
    if category.asset_class not in AssetClass.values:
        out.append(bi("event_values", why=f"Unknown asset class {category.asset_class}.",
                      why_ar=f"فئة أصل غير معروفة {category.asset_class}."))
    if not (category.name_ar or "").strip():
        out.append(bi("event_values", why="The Arabic name is required.", why_ar="الاسم بالعربية مطلوب."))
    return out


def create_category(entity, *, user, code, name_ar, name_en="", asset_class, cost_account, accum_depr_account=None,
                    depr_expense_account=None, accum_impairment_account=None, impairment_expense_account=None,
                    disposal_account=None, rental_fleet=False):
    """A category of ``entity`` (``fixed_assets.maintain_setup``). Depreciable and retention class follow the class
    (R12, §3.1); 1510 for investment property only (R49); 1250 never (R6)."""
    access.require(user, entity, "fixed_assets.maintain_setup")
    with transaction.atomic():
        category = AssetCategory(
            entity=entity, code=(code or "").strip()[:20], name_en=name_en or "", name_ar=name_ar or "",
            asset_class=asset_class, cost_account=_account(entity, cost_account),
            accum_depr_account=_account(entity, accum_depr_account),
            depr_expense_account=_account(entity, depr_expense_account),
            accum_impairment_account=_account(entity, accum_impairment_account),
            impairment_expense_account=_account(entity, impairment_expense_account),
            disposal_account=_account(entity, disposal_account),
            depreciable=asset_class not in NON_DEPRECIABLE_CLASSES,
            retention_class=(RetentionClass.REAL_ESTATE if asset_class in REAL_ESTATE_CLASSES
                             else RetentionClass.MOVABLE),
            rental_fleet=bool(rental_fleet), created_by=user, source=_source(user),
        )
        problems = _category_problems(entity, category)
        if problems:
            codes = [c for c in ("bai_ajel_asset", "ip_account_class") if any(bi(c) == p for p in problems)]
            raise problems_error(problems, codes[0] if codes else "category_invalid")
        if AssetCategory.objects.filter(entity=entity, code=category.code).exists():
            raise error("event_values", why=f"Category {category.code} exists already.",
                        why_ar=f"الفئة {category.code} موجودة مسبقًا.")
        return _save(category)


def create_location(entity, *, user, code, name_ar, name_en="", property_ref="", city_en="", city_ar=""):
    """A location of ``entity`` (``fixed_assets.maintain_setup``; 3.7)."""
    access.require(user, entity, "fixed_assets.maintain_setup")
    if not (name_ar or "").strip():
        raise error("event_values", why="The Arabic name is required.", why_ar="الاسم بالعربية مطلوب.")
    return _create_location(entity, user, code, name_ar, name_en, property_ref, city_en, city_ar)


def _create_location(entity, user, code, name_ar, name_en="", property_ref="", city_en="", city_ar=""):
    location = AssetLocation(entity=entity, code=(code or "").strip()[:20], name_ar=name_ar.strip()[:120],
                             name_en=(name_en or "")[:120], property_ref=(property_ref or "")[:60],
                             city_en=city_en or "", city_ar=city_ar or "", created_by=user, source=_source(user))
    return _save(location)


SETTINGS_FIELDS = ("art52_adjustment_months_movable", "art52_adjustment_months_real_estate", "normal_credit_days",
                   "reporting_dates")


def setup_entity(entity, *, user):
    """R31: a new entity gets its settings and the group template's categories as data (setup command). Returns
    (created category codes, skipped (code, missing account) pairs). Idempotent."""
    access.require(user, entity, "fixed_assets.maintain_setup")
    created, skipped = [], []
    with transaction.atomic():
        if not FixedAssetSettings.objects.filter(entity=entity).exists():
            _save(FixedAssetSettings(entity=entity, created_by=user, source=_source(user)))
        chart = {a.code: a for a in Account.objects.filter(entity=entity)}
        for code, en, ar, klass, cost, accum, accum_imp, fleet in template.CATEGORY_TEMPLATE:
            if AssetCategory.objects.filter(entity=entity, code=code).exists():
                continue
            if cost not in chart:
                skipped.append((code, cost))
                continue
            category = AssetCategory(
                entity=entity, code=code, name_en=en, name_ar=ar, asset_class=klass, cost_account=chart[cost],
                accum_depr_account=chart.get(accum) if accum else None,
                depr_expense_account=chart.get(template.DEPRECIATION) if accum else None,
                accum_impairment_account=chart.get(accum_imp) if accum_imp else None,
                impairment_expense_account=chart.get(template.IMPAIRMENT_EXPENSE) if accum_imp else None,
                disposal_account=chart.get(template.DISPOSAL),
                depreciable=klass not in NON_DEPRECIABLE_CLASSES,
                retention_class=RetentionClass.REAL_ESTATE if klass in REAL_ESTATE_CLASSES else RetentionClass.MOVABLE,
                rental_fleet=fleet, created_by=user, source=_source(user),
            )
            problems = _category_problems(entity, category)
            if problems:
                raise problems_error(problems, "category_invalid")
            _save(category)
            created.append(code)
    return created, skipped


def update_settings(entity, *, user, **fields):
    """Change the entity's settings (``fixed_assets.maintain_setup``); logged (3.9). Retention dates are re-derived and
    never shortened (R33); deferred-payment flags follow ``normal_credit_days`` (R4a)."""
    access.require(user, entity, "fixed_assets.maintain_setup")
    unknown = set(fields) - set(SETTINGS_FIELDS)
    if unknown:
        raise error("event_values", why=f"Unknown settings: {', '.join(sorted(unknown))}.",
                    why_ar="إعدادات غير معروفة.")
    with transaction.atomic():
        row = FixedAssetSettings.objects.select_for_update().get(entity=entity)
        before = {f: getattr(row, f) for f in SETTINGS_FIELDS}
        if "reporting_dates" in fields:
            fields["reporting_dates"] = sorted(str(date.fromisoformat(str(d))) for d in fields["reporting_dates"] or [])
        for name, value in fields.items():
            setattr(row, name, value)
        row.modified_by, row.modified_at = user, _now()
        _save(row)
        access_services.log(AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=entity,
                            permission="fixed_assets.maintain_setup",
                            details={"action": "fixed_asset_settings_changed",
                                     "before": {k: str(v) for k, v in before.items()},
                                     "after": {k: str(getattr(row, k)) for k in SETTINGS_FIELDS}})
        for asset in FixedAsset.objects.filter(entity=entity).exclude(status=AS.DISPOSED):
            _refresh_flags(asset, row)
            _refresh_dates(asset, row)
            _save(asset)
    return row


# --- policies (3.2, R14) ------------------------------------------------------------------------------------------

def _policy_problems(method, life, rate, residual_pct):
    out = []
    if method not in Method.values:
        out.append(("Unknown method.", "طريقة غير معروفة."))
    if method == Method.STRAIGHT_LINE and not life:
        out.append(("Straight line needs the useful life in months.", "القسط الثابت يحتاج إلى العمر الإنتاجي بالأشهر."))
    if method == Method.REDUCING_BALANCE and not (rate and ZERO < rate < Decimal("1")):
        out.append(("Reducing balance needs an annual rate between 0 and 1.",
                    "القسط المتناقص يحتاج إلى نسبة سنوية بين 0 و1."))
    if not (ZERO <= residual_pct < HUNDRED):
        out.append(("The residual value % is from 0 to below 100.", "نسبة القيمة المتبقية من 0 إلى أقل من 100."))
    return [f"{en} / {ar}" for en, ar in out]


def draft_policy(category, *, user, effective_from, method=Method.STRAIGHT_LINE, useful_life_months=None,
                 annual_rate=None, residual_pct="0", basis_note_ar, basis_note_en=""):
    """Draft a policy version (``fixed_assets.maintain_policy``). Values are data entered by the CFO's staff (OQ-2)."""
    entity = category.entity
    access.require(user, entity, "fixed_assets.maintain_policy")
    rate = Decimal(str(annual_rate)) if annual_rate not in (None, "") else None
    pct = Decimal(str(residual_pct or "0"))
    problems = _policy_problems(method, useful_life_months, rate, pct)
    if not category.depreciable:
        problems.append("A non-depreciable category has no depreciation policy (R12). / لا سياسة استهلاك لفئة لا تُستهلك.")
    if not (basis_note_ar or "").strip():
        problems.append(bi("reason_required"))
    if problems:
        raise problems_error(problems, "policy_invalid")
    policy = DepreciationPolicy(entity=entity, category=category, effective_from=effective_from, method=method,
                                useful_life_months=useful_life_months if method == Method.STRAIGHT_LINE else None,
                                annual_rate=rate if method == Method.REDUCING_BALANCE else None, residual_pct=pct,
                                basis_note_ar=basis_note_ar.strip(), basis_note_en=(basis_note_en or "").strip(),
                                created_by=user, source=_source(user))
    return _save(policy)


def submit_policy(policy, *, user):
    """draft -> submitted; opens the engine request (activity ``fixed_assets.policy``)."""
    with transaction.atomic():
        policy = _lock(DepreciationPolicy, policy.pk)
        access.require(user, policy.entity, "fixed_assets.maintain_policy")
        _human(user, policy.entity, "fixed_assets.maintain_policy")
        if policy.status != DepreciationPolicy.Status.DRAFT:
            raise error("not_draft")
        policy.status, policy.submitted_by, policy.submitted_at = DepreciationPolicy.Status.SUBMITTED, user, _now()
        _touch(policy, user)
        _save(policy)
        engine.submit(policy, user, submit_permission="fixed_assets.maintain_policy",
                      permission="fixed_assets.approve_policy")
    return policy


def approve_policy(policy, *, user):
    """The final engine decision of ``fixed_assets.policy`` approves the version (R14). No overlapping approved
    versions per category (3.2)."""
    entity = policy.entity
    _through_engine(user, entity, "fixed_assets.approve_policy")
    with transaction.atomic():
        policy = _lock(DepreciationPolicy, policy.pk)
        grant = access.require(user, entity, "fixed_assets.approve_policy", document=policy)
        if policy.status != DepreciationPolicy.Status.SUBMITTED:
            raise error("status", what="policy", what_ar="السياسة", status=policy.status)
        if DepreciationPolicy.objects.filter(category=policy.category, effective_from=policy.effective_from,
                                             status=DepreciationPolicy.Status.APPROVED).exists():
            raise error("policy_overlap", category=policy.category.label, effective=policy.effective_from)
        policy.status, policy.approved_by, policy.approved_at = DepreciationPolicy.Status.APPROVED, user, _now()
        _save(policy)
        access.use_grant(grant, target=policy)
    return policy


def return_policy(policy, *, user, note=""):
    """submitted -> draft (engine return)."""
    policy = _lock(DepreciationPolicy, policy.pk)
    if not access.has(user, policy.entity, "fixed_assets.maintain_policy"):
        access.require(user, policy.entity, "fixed_assets.approve_policy", document=policy, purpose="return")
    if policy.status != DepreciationPolicy.Status.SUBMITTED:
        raise error("status", what="policy", what_ar="السياسة", status=policy.status)
    policy.status = DepreciationPolicy.Status.DRAFT
    policy.modified_at = _now()
    policy.version += 1
    return _save(policy)


def _policy_in_force(category, on):
    """The approved version with the latest ``effective_from`` on or before ``on`` (R14)."""
    return (DepreciationPolicy.objects.filter(category=category, status=DepreciationPolicy.Status.APPROVED,
                                              effective_from__lte=on).order_by("-effective_from", "-pk").first())


def policy_in_force(category, on, *, user):
    """The approved policy of ``category`` in force on ``on``, or None (``fixed_assets.view``)."""
    access.require(user, category.entity, VIEW)
    return _policy_in_force(category, on)


# --- the register: capitalisable lines and drafts (R1 to R6, R44) -------------------------------------------------

def _categories_by_account(entity):
    out = {}
    for category in AssetCategory.objects.filter(entity=entity, is_active=True).select_related("cost_account"):
        out.setdefault(category.cost_account_id, []).append(category)
    return out


def _linked(line_id):
    return AssetCostComponent.objects.filter(purchase_line_id=line_id).aggregate(t=Sum("amount"))["t"] or ZERO


def _remaining(booking):
    return booking.amount - _linked(booking.line_id)


def capitalisable_lines(entity, *, user):
    """R2: posted, unreversed bill and vendor-credit-note lines of ``entity`` on a category cost account that are not
    fully linked: [(line, booking, remaining SAR)]. 1250 lines are never listed (R6). Read through the invoicing port."""
    access.require(user, entity, VIEW)
    by_account = _categories_by_account(entity)
    out = []
    for line in port.posted_lines(entity, by_account):
        booking = port.booking_of(line)
        if booking is None or not booking.amount:
            continue
        remaining = _remaining(booking)
        if remaining:
            out.append((line, booking, remaining))
    return out


def _new_asset(entity, user, category, location, description_ar, description_en, **fields):
    _same_entity(entity, category, "The category", "الفئة")
    _same_entity(entity, location, "The location", "الموقع")
    if location is None:
        raise error("missing_submission_data", fields="location", fields_ar="الموقع")
    usage = fields.get("usage") or Usage.OWN_USE
    if (usage == Usage.INVESTMENT_PROPERTY) != (category.asset_class in IP_CLASSES):
        raise error("ip_usage")
    asset = FixedAsset(entity=entity, category=category, location=location, code=_next_number(entity, "FA"),
                       description_ar=(description_ar or "")[:300], description_en=(description_en or "")[:300],
                       retention_class=category.retention_class, created_by=user, source=_source(user), **fields)
    if not asset.description_ar.strip():
        raise error("missing_submission_data", fields="Arabic description", fields_ar="الوصف بالعربية")
    return _save(asset)


ASSET_FIELDS = ("description_ar", "description_en", "location", "serial_or_plate", "custodian_ref", "usage",
                "held_for", "use_evidence_ref", "vat_use", "available_for_use_date", "residual_value")


def _check_choice(name, value):
    choices = {"usage": Usage.values, "held_for": HeldFor.values, "vat_use": VatUse.values}.get(name)
    if choices is not None and value not in ("", None) and value not in choices:
        raise error("event_values", why=f"Unknown {name} {value!r}.", why_ar=f"قيمة غير معروفة {value!r}.")


def create_asset(entity, *, user, category, location, description_ar, description_en="", usage=Usage.OWN_USE,
                 held_for="", vat_use="", serial_or_plate="", custodian_ref="", use_evidence_ref=""):
    """A draft asset with no cost yet (``fixed_assets.create_asset``): the asset of a financed acquisition (R7) or
    the target of a CWIP capitalisation (R44). Agents may draft (logged, R35)."""
    access.require(user, entity, "fixed_assets.create_asset")
    for name, value in (("usage", usage), ("held_for", held_for), ("vat_use", vat_use)):
        _check_choice(name, value)
    with transaction.atomic():
        asset = _new_asset(entity, user, category, location, description_ar, description_en, usage=usage,
                           held_for=held_for or "", vat_use=vat_use or "", serial_or_plate=serial_or_plate or "",
                           custodian_ref=custodian_ref or "", use_evidence_ref=use_evidence_ref or "")
        _log_agent(user, entity, "fixed_assets.create_asset", asset, "create_draft")
    return asset


def create_asset_from_line(line, *, user, category=None, asset=None, amount=None, location=None, description_ar=None,
                           description_en=None, usage=Usage.OWN_USE, held_for="", vat_use="", serial_or_plate="",
                           custodian_ref="", use_evidence_ref="", residual_value=None):
    """R2: draft one asset from a posted bill line, or link the line to an existing draft asset of the same entity
    (a ``cwip`` asset may be extended while not disposed, R44). ``amount`` splits the line (default: all that is not
    linked yet); a line never feeds more than its posted functional amount (R4). Posts nothing (FA1)."""
    entity = line.entity
    access.require(user, entity, "fixed_assets.create_asset")
    for name, value in (("usage", usage), ("held_for", held_for), ("vat_use", vat_use)):
        _check_choice(name, value)
    with transaction.atomic():
        line = port.line_of(entity, line.pk)
        if asset is not None:
            _same_entity(entity, asset, "The asset", "الأصل")
            asset = _lock(FixedAsset, asset.pk)
            _same_entity(entity, asset, "The asset", "الأصل")
            category = category or asset.category
        _same_entity(entity, category, "The category", "الفئة")
        _same_entity(entity, location, "The location", "الموقع")
        label = port.document_label(line)
        if line.account.code == template.BAI_AJEL_ASSET:
            raise error("bai_ajel_asset")  # R6
        if _financing_account(line.account):
            raise error(_r7_code(entity))  # R7
        booking = port.booking_of(line)
        candidates = _categories_by_account(entity).get(line.account_id, [])
        if booking is None or booking.status != port.POSTED or not candidates:
            raise error("line_not_capitalisable", line=label, entity=entity.code)
        if category is None:  # the one category on the account; a rental fleet is always chosen explicitly
            plain = [c for c in candidates if not c.rental_fleet] if len(candidates) > 1 else candidates
            if len(plain) != 1:
                raise error("line_category", line=label, code=line.account.code, category="?")
            category = plain[0]
        if category.cost_account_id != line.account_id:
            raise error("line_category", line=label, code=line.account.code, category=category.label)
        remaining = _remaining(booking)
        value = remaining
        if amount is not None:
            value = _money(amount, "Amount", "المبلغ", positive=True)
            value = -value if booking.amount < ZERO else value
        if not remaining or abs(value) > abs(remaining) or (value > ZERO) != (remaining > ZERO):
            raise error("line_fully_linked", line=label, amount=booking.amount)
        if asset is None:
            asset = _new_asset(entity, user, category, location, description_ar or line.description_ar,
                               description_en if description_en is not None else line.description_en, usage=usage,
                               held_for=held_for or "", vat_use=vat_use or "", serial_or_plate=serial_or_plate or "",
                               custodian_ref=custodian_ref or "", use_evidence_ref=use_evidence_ref or "",
                               residual_value=_money(residual_value, "Residual value", "القيمة المتبقية",
                                                     allow_none=True))
        elif asset.category.asset_class == AssetClass.CWIP and asset.status != AS.DISPOSED:
            pass  # R44: CWIP accumulates bill lines, never depreciated
        elif asset.status in (AS.ACTIVE, AS.FULLY_DEPRECIATED):
            raise error("active_through_event")
        elif asset.status != AS.DRAFT:
            raise error("not_draft")
        settings = _settings(entity)
        share = value / booking.amount if booking.amount else ZERO
        component = AssetCostComponent(
            entity=entity, asset=asset, kind=AssetCostComponent.Kind.BILL_LINE, purchase_line=line, amount=value,
            accounting_date=booking.accounting_date, vat_charged=calc.q(booking.vat_charged * share),
            vat_recovered=calc.q(booking.vat_recovered * share),
            effective_from=calc.next_month(_today()) if asset.status == AS.ACTIVE else None,
            deferred_payment=_deferred(booking, settings), created_by=user,
        )
        _save(component)
        _refresh_cost(asset)
        _refresh_flags(asset, settings)
        _refresh_dates(asset, settings)
        _touch(asset, user)
        _save(asset)
        _log_agent(user, entity, "fixed_assets.create_asset", asset, "link_bill_line")
    return asset


def update_asset(asset, *, user, **fields):
    """Change a draft asset (``fixed_assets.create_asset``). Active assets change only through events (R8, R16, R47)."""
    entity = asset.entity
    access.require(user, entity, "fixed_assets.create_asset")
    unknown = set(fields) - set(ASSET_FIELDS)
    if unknown:
        raise error("event_values", why=f"Unknown fields: {', '.join(sorted(unknown))}.", why_ar="حقول غير معروفة.")
    with transaction.atomic():
        asset = _lock(FixedAsset, asset.pk)
        if asset.status != AS.DRAFT:
            if "vat_use" in fields and fields["vat_use"] != asset.vat_use:
                raise error("vat_use_by_event")
            raise error("not_draft")
        for name, value in fields.items():
            _check_choice(name, value)
            if name == "location":
                _same_entity(entity, value, "The location", "الموقع")
            if name == "residual_value":
                value = _money(value, "Residual value", "القيمة المتبقية", allow_none=True)
            setattr(asset, name, value if value is not None or name in ("available_for_use_date", "residual_value")
                    else "")
        if (asset.usage == Usage.INVESTMENT_PROPERTY) != (asset.category.asset_class in IP_CLASSES):
            raise error("ip_usage")
        _refresh_dates(asset, _settings(entity))
        _touch(asset, user)
        _save(asset)
        _log_agent(user, entity, "fixed_assets.create_asset", asset, "update_draft")
    return asset


def _deferred(booking, settings):
    """R4a: payment terms longer than the entity's normal credit days; nothing is flagged while they are not set."""
    if settings.normal_credit_days is None or booking.payment_days is None:
        return False
    return booking.payment_days > settings.normal_credit_days


def _refresh_cost(asset):
    asset.cost = calc.q(asset.components.aggregate(t=Sum("amount"))["t"] or ZERO)
    dates = [c.accounting_date for c in asset.components.all() if c.amount > ZERO]
    if asset.status in (AS.DRAFT, AS.SUBMITTED) and not asset.is_migrated:
        asset.acquisition_date = min(dates) if dates else None  # R5: fixed at activation


def _refresh_flags(asset, settings):
    for component in asset.components.filter(purchase_line__isnull=False):
        booking = port.booking_of(component.purchase_line)
        flag = booking is not None and _deferred(booking, settings)
        if flag != component.deferred_payment:
            component.deferred_payment = flag
            _save(component)
    asset.deferred_payment_flag = asset.components.filter(deferred_payment=True).exists()


def _life_for_retention(asset):
    return asset.useful_life_months if asset.method == Method.STRAIGHT_LINE else None


def _refresh_dates(asset, settings):
    """R33, R46 (a): retention and Art. 52 dates from the acquisition date; retention is never shortened."""
    if asset.acquisition_date is None:
        return
    period = asset.entity.vat_return_period or "quarterly"
    computed = calc.retention_until(
        retention_class=asset.retention_class or asset.category.retention_class,
        acquisition_date=asset.acquisition_date, life_months=_life_for_retention(asset),
        movable_months=settings.art52_adjustment_months_movable, vat_return_period=period)
    # A draft's date is provisional (its life is copied at activation); from activation on it is never shortened.
    kept = asset.retention_until if asset.status not in (AS.DRAFT, AS.SUBMITTED) else None
    candidates = [d for d in (computed, kept, asset.retention_floor) if d is not None]
    asset.retention_until = max(candidates) if candidates else None
    asset.adjustment_period_end = calc.adjustment_period_end(
        retention_class=asset.retention_class or asset.category.retention_class,
        acquisition_date=asset.acquisition_date, life_months=_life_for_retention(asset),
        movable_months=settings.art52_adjustment_months_movable,
        real_estate_months=settings.art52_adjustment_months_real_estate)


def _vat_snapshot(asset):
    """R46 (a): VAT charged on the linked bill lines and the share recovered on 1310."""
    rows = asset.components.filter(purchase_line__isnull=False).aggregate(c=Sum("vat_charged"), r=Sum("vat_recovered"))
    charged, recovered = rows["c"] or ZERO, rows["r"] or ZERO
    if not asset.components.filter(purchase_line__isnull=False).exists():
        return None, None
    pct = (recovered / charged * HUNDRED).quantize(Decimal("0.0001")) if charged else None
    return calc.q(charged), pct


def _submission_problems(asset):
    """[(code, text)] that stop a submission or an activation (R5, R14, R46 (b))."""
    problems, missing_en, missing_ar = [], [], []
    category = asset.category
    if asset.cost <= ZERO:
        problems.append(("no_cost", bi("no_cost")))
    for field_name, en, ar in (("held_for", "held for", "الغرض من الاحتفاظ"), ("vat_use", "VAT use", "استخدام الضريبة")):
        if not getattr(asset, field_name):
            missing_en.append(en)
            missing_ar.append(ar)
    if category.depreciable and asset.available_for_use_date is None:
        missing_en.append("available-for-use date")
        missing_ar.append("تاريخ الجاهزية للاستخدام")
    if missing_en:
        problems.append(("missing_submission_data", bi("missing_submission_data", fields=", ".join(missing_en),
                                                       fields_ar="، ".join(missing_ar))))
    if asset.acquisition_date and asset.available_for_use_date and asset.acquisition_date > asset.available_for_use_date:
        problems.append(("acquisition_after_available", bi("acquisition_after_available",
                                                           acquired=asset.acquisition_date,
                                                           available=asset.available_for_use_date)))
    if category.depreciable and asset.available_for_use_date and \
            _policy_in_force(category, asset.available_for_use_date) is None:
        problems.append(("no_policy", bi("no_policy", category=category.label)))
    if (asset.usage == Usage.INVESTMENT_PROPERTY) != (category.asset_class in IP_CLASSES):
        problems.append(("ip_usage", bi("ip_usage")))
    return problems


def _refuse_problems(problems):
    if problems:
        raise problems_error([t for _c, t in problems], problems[0][0])


def submit_asset(asset, *, user, available_for_use_date=None):
    """draft -> submitted for capitalisation (``fixed_assets.submit_asset``; R2, R5, R14, R46 (b)); opens the engine
    request of ``fixed_assets.capitalisation`` (amount: cost, SAR)."""
    entity = asset.entity
    access.require(user, entity, "fixed_assets.submit_asset")
    _human(user, entity, "fixed_assets.submit_asset")
    with transaction.atomic():
        asset = _lock(FixedAsset, asset.pk)
        if asset.status != AS.DRAFT:
            raise error("not_draft")
        if available_for_use_date is not None:
            asset.available_for_use_date = available_for_use_date
        _refuse_problems(_submission_problems(asset))
        asset.status, asset.submitted_by, asset.submitted_at = AS.SUBMITTED, user, _now()
        _refresh_dates(asset, _settings(entity))
        _touch(asset, user)
        _save(asset)
        engine.submit(asset, user, submit_permission="fixed_assets.submit_asset", permission="fixed_assets.capitalise")
    return asset


def activate_asset(asset, *, user):
    """The final engine decision of ``fixed_assets.capitalisation`` activates the asset (R2, R36): method and life
    copied from the approved policy, residual from its %, acquisition date fixed, VAT and retention data captured
    (R33, R46). Posts nothing: the cost is in the GL from the bill (FA1)."""
    entity = asset.entity
    _through_engine(user, entity, "fixed_assets.capitalise")
    with transaction.atomic():
        asset = _lock(FixedAsset, asset.pk)
        grant = access.require(user, entity, "fixed_assets.capitalise", document=asset)
        if asset.status != AS.SUBMITTED:
            raise error("status", what="asset", what_ar="الأصل", status=asset.status)
        _refuse_problems(_submission_problems(asset))
        engine.check_issued_band(grant, asset, amount=asset.cost, currency=entity.base_currency_id)
        settings = _settings(entity)
        if asset.category.depreciable:
            policy = _policy_in_force(asset.category, asset.available_for_use_date)
            asset.policy, asset.method = policy, policy.method
            asset.useful_life_months, asset.annual_rate = policy.useful_life_months, policy.annual_rate
            if asset.residual_value is None:
                asset.residual_value = calc.q(asset.cost * policy.residual_pct / HUNDRED)
        elif asset.residual_value is None:
            asset.residual_value = ZERO
        asset.input_vat_amount, asset.initial_recovery_pct = _vat_snapshot(asset)
        asset.status, asset.approved_by, asset.approved_at = AS.ACTIVE, user, _now()
        asset.retention_until = None  # the draft's date was provisional; from here on it is never shortened (R33)
        _refresh_dates(asset, settings)
        _save(asset)
        access.use_grant(grant, target=asset)
    return asset


def return_asset(asset, *, user, note=""):
    """submitted -> draft (engine return), by the preparer's staff or the approver."""
    asset = _lock(FixedAsset, asset.pk)
    if not access.has(user, asset.entity, "fixed_assets.submit_asset"):
        access.require(user, asset.entity, "fixed_assets.capitalise", document=asset, purpose="return")
    if asset.status != AS.SUBMITTED:
        raise error("status", what="asset", what_ar="الأصل", status=asset.status)
    asset.status = AS.DRAFT
    asset.modified_at = _now()
    asset.version += 1
    return _save(asset)


def register(entity, *, user, status=None):
    """The register of ``entity`` the user may see (``fixed_assets.view``, R29)."""
    access.require(user, entity, VIEW)
    qs = FixedAsset.objects.filter(entity=entity).select_related("category", "location")
    return qs.filter(status=status) if status else qs


def asset_detail(asset, *, user):
    """One asset with its computed values; ``transferred_from`` only for users with the view right in both entities
    (R29), else "not visible" (en/ar)."""
    access.require(user, asset.entity, VIEW)
    origin = None
    if asset.transferred_from_id:
        source = FixedAsset.objects.select_related("entity").get(pk=asset.transferred_from_id)
        origin = source.code if access.has(user, source.entity, VIEW) else bi("not_visible")
    values = _values(asset, _today())
    return {"asset": asset, "transferred_from": origin, **values,
            "vat_use_history": list(_vat_history(asset))}


# --- values and schedules -----------------------------------------------------------------------------------------

def _posted_lines(asset):
    return DepreciationLine.objects.filter(asset=asset, run__status=RS.POSTED)


def _posted_depreciation(asset, through=None):
    qs = _posted_lines(asset)
    if through is not None:
        qs = qs.filter(run__period__end_date__lte=through)
    return qs.aggregate(t=Sum("amount"))["t"] or ZERO


def _posted_events(asset, *kinds, through=None):
    qs = asset.events.filter(status=ES.POSTED, kind__in=kinds)
    return qs.filter(event_date__lte=through) if through is not None else qs


def _net_impairment(asset, through=None):
    imp = _posted_events(asset, EK.IMPAIRMENT, through=through).aggregate(t=Sum("amount"))["t"] or ZERO
    rev = _posted_events(asset, EK.IMPAIRMENT_REVERSAL, through=through).aggregate(t=Sum("amount"))["t"] or ZERO
    return imp - rev


def _values(asset, as_of):
    """Cost, accumulated depreciation and impairment and NBV of ``asset`` on ``as_of`` (SAR)."""
    cost = asset.components.filter(accounting_date__lte=as_of).aggregate(t=Sum("amount"))["t"] or ZERO
    opening = asset.opening_accumulated_depreciation if (asset.opening_date and asset.opening_date <= as_of) else ZERO
    depreciation = opening + _posted_depreciation(asset, as_of)
    impairment = _net_impairment(asset, as_of)
    disposed = asset.disposal_date is not None and asset.disposal_date <= as_of and asset.status == AS.DISPOSED
    nbv = cost - depreciation - impairment
    return {"cost": calc.q(cost), "accumulated_depreciation": calc.q(depreciation),
            "accumulated_impairment": calc.q(impairment), "nbv": calc.q(nbv), "disposed": disposed}


def _carrying_now(asset):
    v = _values(asset, date.max)
    return v["nbv"]


def _start_of(asset):
    if asset.is_migrated and asset.opening_date:
        return asset.opening_date + timedelta(days=1)
    return asset.available_for_use_date


def _activation_values(asset):
    """Method, life, rate and residual at activation (the first approved change of estimate keeps them)."""
    first = (asset.events.filter(kind=EK.CHANGE_OF_ESTIMATE, status=ES.APPROVED).order_by("effective_from", "pk")
             .first())
    if first is not None and first.previous_values:
        p = first.previous_values
        return (p.get("method") or asset.method, p.get("useful_life_months"),
                Decimal(p["annual_rate"]) if p.get("annual_rate") else None,
                Decimal(p.get("residual_value") or "0"))
    return asset.method, asset.useful_life_months, asset.annual_rate, asset.residual_value or ZERO


def _basis(asset, *, with_impairment=True):
    """The :class:`calc.Basis` of an active (or since disposed) depreciable asset."""
    start = _start_of(asset)
    method, life, rate, residual = _activation_values(asset)
    if asset.is_migrated:
        life = asset.opening_remaining_life_months if method == Method.STRAIGHT_LINE else None
    base_cost = asset.components.filter(effective_from__isnull=True).aggregate(t=Sum("amount"))["t"] or ZERO
    carrying = base_cost - asset.opening_accumulated_depreciation
    life_end = calc.add_months(start, life) if (method == Method.STRAIGHT_LINE and life) else None
    changes = [calc.Change(on=c.effective_from, carrying_delta=c.amount)
               for c in asset.components.filter(effective_from__isnull=False)]
    if with_impairment:
        for e in _posted_events(asset, EK.IMPAIRMENT, EK.IMPAIRMENT_REVERSAL):
            delta = -e.amount if e.kind == EK.IMPAIRMENT else e.amount
            changes.append(calc.Change(on=calc.next_month(e.event_date), carrying_delta=delta))
    origin = asset.available_for_use_date or start
    for e in asset.events.filter(kind=EK.CHANGE_OF_ESTIMATE, status=ES.APPROVED).order_by("effective_from", "pk"):
        changes.append(calc.Change(
            on=e.effective_from, residual=e.new_residual_value, method=e.new_method or None,
            life_end=calc.add_months(origin, e.new_useful_life_months) if e.new_useful_life_months else None,
            annual_rate=e.new_annual_rate))
    stop = None
    write_off = _posted_events(asset, EK.WRITE_OFF).first()
    if write_off is not None:
        stop = write_off.depreciation_until
    return calc.Basis(start=start, carrying=carrying, residual=residual, method=method, life_end=life_end,
                      annual_rate=rate, changes=changes, stop=stop)


def _schedule(asset, until, *, with_impairment=True):
    if not asset.category.depreciable or _start_of(asset) is None or not asset.method:
        return []
    return calc.schedule(_basis(asset, with_impairment=with_impairment), until=until)


def depreciation_schedule(asset, *, user, until=None):
    """The planned depreciation per month of ``asset`` (``fixed_assets.view``)."""
    access.require(user, asset.entity, VIEW)
    return _schedule(asset, until)


# --- R4 pending adjustments and R43 unregistered lines -----------------------------------------------------------

def _pending_adjustments(asset):
    """R4: a vendor credit note on, or the reversal of, a bill that feeds the asset leaves the register's cost out of
    step until a cost adjustment event is approved. Returns the bill / credit-note labels."""
    out = []
    components = list(asset.components.filter(purchase_line__isnull=False).select_related("purchase_line__document"))
    bills = {c.purchase_line.document_id for c in components if c.amount > ZERO}
    for note in port.credit_notes_of(bills):
        for line in note.lines.select_related("document", "account"):
            if line.account.code in template.COST_CODES and not _linked(line.pk):
                booking = port.booking_of(line)
                if booking is not None and booking.amount:
                    out.append(port.document_label(line))
    for c in components:
        if c.purchase_line.document.status == port.REVERSED and c.amount > ZERO and not \
                AssetCostComponent.objects.filter(reverses=c).exists():
            out.append(f"{port.document_label(c.purchase_line)} (reversed / معكوسة)")
    return out


def _unregistered_lines(entity, start, end):
    """R43: posted bill lines dated in the range on a category cost account that are linked to no asset."""
    accounts = list(AssetCategory.objects.filter(entity=entity).values_list("cost_account_id", flat=True))
    if not accounts:
        return []
    out = []
    for line in port.posted_lines(entity, accounts, date_from=start, date_to=end):
        if not AssetCostComponent.objects.filter(purchase_line=line).exists():
            booking = port.booking_of(line)
            if booking is not None and booking.amount:
                out.append(port.document_label(line))
    return out


# --- depreciation runs (R10 to R18, R40) ---------------------------------------------------------------------------

def _run_assets(entity):
    return (FixedAsset.objects.filter(entity=entity, category__depreciable=True,
                                      status__in=(AS.ACTIVE, AS.FULLY_DEPRECIATED, AS.DISPOSED))
            .select_related("category", "category__depr_expense_account", "category__accum_depr_account")
            .order_by("pk"))


def _posted_periods(entity):
    return set(DepreciationRun.objects.filter(entity=entity, status=RS.POSTED).values_list("period_id", flat=True))


def _fingerprint(entity, period, assets):
    """R15: what the run depends on (assets, their events, earlier posted runs); amounts are recomputed from it."""
    rows = []
    for a in assets:
        rows.append([a.pk, a.status, str(a.cost), str(a.residual_value), a.method, a.useful_life_months,
                     str(a.annual_rate), str(a.available_for_use_date), str(a.opening_accumulated_depreciation),
                     a.opening_remaining_life_months, a.category_id, str(a.disposal_date),
                     sorted([e.pk, e.status] for e in a.events.exclude(status__in=(ES.DRAFT, ES.SUBMITTED,
                                                                                  ES.DISCARDED))),
                     sorted(a.components.values_list("pk", flat=True))])
    earlier = sorted(DepreciationRun.objects.filter(entity=entity, status=RS.POSTED,
                                                    period__start_date__lt=period.start_date).values_list("pk",
                                                                                                          flat=True))
    payload = json.dumps({"period": period.pk, "assets": rows, "runs": earlier}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _compute_run(entity, period):
    """(line dicts, fingerprint) of the run of ``period``: per asset its month's depreciation and a catch-up line for
    earlier months not yet booked (R17); refusals per R14, R15, R40, R45 and R4."""
    assets = list(_run_assets(entity))
    posted_periods = _posted_periods(entity)
    periods = {p.start_date: p for p in FiscalPeriod.objects.filter(entity=entity)}
    opening_posted = None
    lines = []
    for asset in assets:
        if asset.status == AS.DISPOSED and asset.disposal_date and calc.month_start(asset.disposal_date) < \
                period.start_date and not _posted_events(asset, EK.WRITE_OFF).filter(
                    depreciation_until__gte=period.start_date).exists():
            months = _schedule(asset, period.end_date)
            if calc.total_through(months, period.end_date) == _posted_depreciation(asset):
                continue
        months = _schedule(asset, period.end_date)
        scheduled = sum((m.amount for m in months), ZERO)
        posted = _posted_depreciation(asset)
        if scheduled == posted and not any(m.start == period.start_date for m in months):
            continue
        current = [m for m in months if m.start == period.start_date]
        earlier = [m for m in months if m.start < period.start_date]
        current_amount = sum((m.amount for m in current), ZERO)
        catch_up = scheduled - current_amount - posted
        if not current_amount and not catch_up:
            continue
        if _policy_in_force(asset.category, period.end_date) is None:
            raise error("no_policy", category=asset.category.label)  # R14
        if asset.is_migrated:
            if opening_posted is None:
                opening_posted = ledger.migration_opening_posted(entity, booked=False)
            if not opening_posted:
                raise error("opening_not_posted", entity=entity.code)  # R40
        pending = _pending_adjustments(asset)
        if pending:
            raise error("pending_adjustment", lines=", ".join(pending), asset=asset.code)  # R4
        _need_account(asset.category, asset.category.depr_expense_account, "depreciation expense")  # R45
        _need_account(asset.category, asset.category.accum_depr_account, "accumulated depreciation")
        for m in earlier:  # an earlier open month with depreciation and no posted run: post that run first
            p = periods.get(m.start)
            if p is not None and p.status == FiscalPeriod.Status.OPEN and p.pk not in posted_periods and m.amount:
                raise error("earlier_run_missing", period=p.name)
        opening = _carrying_now(asset)
        if catch_up:
            unbooked = [m for m in earlier if periods.get(m.start) is None or periods[m.start].pk not in posted_periods]
            lines.append({"asset": asset, "days": sum(m.days for m in unbooked), "amount": catch_up, "catch_up": True,
                          "opening_nbv": opening})
        if current_amount:
            lines.append({"asset": asset, "days": sum(m.days for m in current), "amount": current_amount,
                          "catch_up": False, "opening_nbv": opening - catch_up})
    return lines, _fingerprint(entity, period, assets)


def prepare_depreciation(entity, period, *, user):
    """R15: a draft run of ``period`` with one line per active depreciable asset (plus catch-up lines, R17)
    (``fixed_assets.prepare_depreciation``; agents may prepare, logged, R35). Refused while another live run exists
    for the period (``run_exists``) or in a closed period."""
    access.require(user, entity, "fixed_assets.prepare_depreciation")
    _same_entity(entity, period, "The period", "الفترة")
    with transaction.atomic():
        period = FiscalPeriod.objects.select_for_update().get(pk=period.pk)
        if period.status != FiscalPeriod.Status.OPEN:
            raise error("period_closed", period=period.name, entity=entity.code)
        live = DepreciationRun.objects.filter(entity=entity, period=period, status__in=(RS.DRAFT, RS.SUBMITTED,
                                                                                         RS.POSTED)).first()
        if live is not None:
            raise error("run_exists", entity=entity.code, status=live.status, period=period.name)
        lines, fingerprint = _compute_run(entity, period)
        if not lines:
            raise error("nothing_to_depreciate", entity=entity.code, period=period.name)
        run = DepreciationRun(entity=entity, period=period, number=_next_number(entity, "DEP", period.start_date.year),
                              fingerprint=fingerprint, total=calc.q(sum(l["amount"] for l in lines)),
                              created_by=user, source=_source(user))
        _save(run)
        for spec in lines:
            _save(DepreciationLine(entity=entity, run=run, **spec))
        _log_agent(user, entity, "fixed_assets.prepare_depreciation", run, "prepare_run")
    return run


def submit_depreciation(run, *, user):
    """draft -> submitted (a person, ``fixed_assets.prepare_depreciation``); the engine request of
    ``fixed_assets.depreciation_run`` is opened (version: the run's, amount: its total in SAR)."""
    entity = run.entity
    access.require(user, entity, "fixed_assets.prepare_depreciation")
    _human(user, entity, "fixed_assets.prepare_depreciation")
    with transaction.atomic():
        run = _lock(DepreciationRun, run.pk)
        if run.status != RS.DRAFT:
            raise error("not_draft")
        run.status, run.submitted_by, run.submitted_at = RS.SUBMITTED, user, _now()
        _touch(run, user)
        _save(run)
        engine.submit(run, user, submit_permission="fixed_assets.prepare_depreciation",
                      permission="fixed_assets.run_depreciation")
    return run


def post_depreciation(run, *, user):
    """The final engine decision of ``fixed_assets.depreciation_run`` (R15): recompute and refuse if the fingerprint
    changed (``run_changed``), check the issued band (``check_issued_band``), then post FA2 through
    ``ledger.services.create_entry(post=True, grant=, document=run)``. Refused outside the engine (access R61)."""
    entity = run.entity
    _through_engine(user, entity, "fixed_assets.run_depreciation")
    with transaction.atomic():
        run = _lock(DepreciationRun, run.pk)
        grant = access.require(user, entity, "fixed_assets.run_depreciation", document=run)
        if run.status != RS.SUBMITTED:
            raise error("status", what="run", what_ar="المسيّر", status=run.status)
        period = FiscalPeriod.objects.get(pk=run.period_id)
        try:
            lines, fingerprint = _compute_run(entity, period)
        except FixedAssetError as exc:
            if exc.code in ("pending_adjustment", "no_policy", "opening_not_posted", "account_not_set"):
                raise
            raise error("run_changed") from None
        if fingerprint != run.fingerprint:
            raise error("run_changed")
        total = calc.q(sum((l["amount"] for l in lines), ZERO))
        engine.check_issued_band(grant, run, amount=total, currency=entity.base_currency_id)
        stored = {(l.asset_id, l.catch_up): l for l in run.lines.all()}
        if set(stored) != {(l["asset"].pk, l["catch_up"]) for l in lines}:
            raise error("run_changed")
        for spec in lines:
            line = stored[(spec["asset"].pk, spec["catch_up"])]
            if (line.amount, line.days, line.opening_nbv) != (spec["amount"], spec["days"], spec["opening_nbv"]):
                line.amount, line.days, line.opening_nbv = spec["amount"], spec["days"], spec["opening_nbv"]
                _save(line)
        run.total, run.status, run.posted_by, run.posted_at = total, RS.POSTED, user, _now()
        _save(run)
        try:
            entry = ledger.create_entry(
                entity, period.end_date, f"Depreciation {period.name} {run.number} / استهلاك {period.name}", None,
                user=user, reference=run.number, post=True, grant=grant, document=run)
        except LedgerError as exc:
            raise problems_error(exc.messages, getattr(exc, "code", None) or "posting_refused") from None
        run.journal_entry = entry
        _save(run)
        for asset_id in {l["asset"].pk for l in lines}:
            _refresh_status(FixedAsset.objects.get(pk=asset_id))
    return run


def return_depreciation(run, *, user, note=""):
    """submitted -> draft (engine return; also the inbox's re-routing after ``amount_outside_band``)."""
    run = _lock(DepreciationRun, run.pk)
    if not access.has(user, run.entity, "fixed_assets.prepare_depreciation"):
        access.require(user, run.entity, "fixed_assets.run_depreciation", document=run, purpose="return")
    if run.status != RS.SUBMITTED:
        raise error("status", what="run", what_ar="المسيّر", status=run.status)
    run.status = RS.DRAFT
    run.modified_at = _now()
    run.version += 1
    return _save(run)


def discard_depreciation(run, *, user):
    """R15: drop a draft or submitted run (a person, ``fixed_assets.prepare_depreciation``) and withdraw its request."""
    entity = run.entity
    access.require(user, entity, "fixed_assets.prepare_depreciation")
    _human(user, entity, "fixed_assets.prepare_depreciation")
    with transaction.atomic():
        run = _lock(DepreciationRun, run.pk)
        if run.status not in (RS.DRAFT, RS.SUBMITTED):
            raise error("status", what="run", what_ar="المسيّر", status=run.status)
        engine.withdraw(run, user, permission="fixed_assets.run_depreciation")
        run.status = RS.DISCARDED
        _touch(run, user)
        _save(run)
    return run


def _refresh_status(asset):
    """active <-> fully_depreciated after a run or its reversal (R8)."""
    if asset.status not in (AS.ACTIVE, AS.FULLY_DEPRECIATED):
        return
    residual = asset.residual_value or ZERO
    done = asset.category.depreciable and _carrying_now(asset) <= residual and _posted_lines(asset).exists()
    status = AS.FULLY_DEPRECIATED if done else AS.ACTIVE
    if status != asset.status:
        asset.status = status
        _save(asset)


def _run_groups(run):
    groups = {}
    for line in run.lines.select_related("asset__category"):
        category = line.asset.category
        key = (category.depr_expense_account_id, category.accum_depr_account_id)
        groups[key] = groups.get(key, ZERO) + line.amount
    return groups


def _run_lines(run):
    """Registered ``build_lines`` of DepreciationRun (FA2): one pair per (expense, accumulated) account."""
    out = []
    label = f"{run.number} {run.period.name}"
    for (expense, accumulated), amount in sorted(_run_groups(run).items(), key=lambda kv: kv[0]):
        if not amount:
            continue
        exp, acc = Account.objects.get(pk=expense), Account.objects.get(pk=accumulated)
        debit, credit = (exp, acc) if amount > ZERO else (acc, exp)
        out.append({"account": debit, "debit": abs(amount), "description": f"{label} depreciation / استهلاك"})
        out.append({"account": credit, "credit": abs(amount), "description": f"{label} depreciation / استهلاك"})
    return out


def _line_problems(entity, lines, allowed, expected_total, label):
    """R38 ``validate_lines``: other entity, control or tax-control, Sharia-prohibited, not named by the document's
    categories, total different from the document's own."""
    out = []
    for line in lines:
        account = line.account
        if account.entity_id != entity.pk:
            out.append(f"{label}: account {account.code} belongs to another entity. / الحساب {account.code} يخص كيانًا آخر.")
        if control_accounts.is_subledger_control(account) or control_accounts.is_tax_control(account):
            out.append(f"{label}: account {account.code} is a control or tax-control account. / الحساب {account.code} "
                       "حساب مراقبة أو حساب ضريبي.")
        if account.sharia_prohibited:
            out.append(f"{label}: account {account.code} is Sharia-prohibited. / الحساب {account.code} محظور شرعًا.")
        if account.pk not in allowed:
            out.append(f"{label}: account {account.code} is not an account of its categories. / الحساب {account.code} "
                       "ليس من حسابات فئاته.")
    total = sum((l.debit for l in lines), ZERO)
    if total != expected_total:
        out.append(f"{label}: entry total {total} differs from the document's {expected_total}. / مجموع القيد {total} "
                   f"يختلف عن مجموع المستند {expected_total}.")
    return out


def _validate_run_lines(run, lines):
    allowed = set()
    for line in run.lines.select_related("asset__category"):
        allowed |= {line.asset.category.depr_expense_account_id, line.asset.category.accum_depr_account_id}
    expected = sum((abs(v) for v in _run_groups(run).values()), ZERO)
    problems = _line_problems(run.entity, lines, allowed - {None}, expected, run.number)
    net = sum((l.amount for l in run.lines.all()), ZERO)
    if net != run.total:
        problems.append(f"{run.number}: run total {run.total} differs from its lines {net}. / إجمالي المسيّر {run.total} "
                        f"يختلف عن بنوده {net}.")
    return problems


def _pending_runs(entity, start_date, end_date):
    """Registered ``pending_documents`` (R15, OQ-4): draft or submitted runs of the period, and a period with
    depreciation to post but no posted run."""
    out = []
    for run in DepreciationRun.objects.filter(entity=entity, status__in=(RS.DRAFT, RS.SUBMITTED),
                                              period__start_date__gte=start_date, period__end_date__lte=end_date):
        out.append(f"Depreciation run {run.number} ({run.status}) / مسيّر الاستهلاك {run.number}")
    period = FiscalPeriod.objects.filter(entity=entity, start_date=start_date, end_date=end_date).first()
    if period is None or not _run_assets(entity).exists():
        return out
    if DepreciationRun.objects.filter(entity=entity, period=period, status__in=(RS.POSTED, RS.DRAFT, RS.SUBMITTED)
                                      ).exists():
        return out
    for asset in _run_assets(entity):
        months = _schedule(asset, end_date)
        if any(m.start == period.start_date and m.amount for m in months):
            out.append(f"No posted depreciation run for {period.name} / لا يوجد مسيّر استهلاك مرحّل للفترة {period.name}")
            break
    return out


# --- asset events (R7, R16, R19 to R23, R25 to R27, R44, R47) ------------------------------------------------------

def _vehicle_contracts(asset):
    """R27: active billing ``vehicle_rental`` contracts of the asset's entity whose ``vehicle_ref`` is the asset's code
    or plate (read-only; OQ-13 until billing links the asset)."""
    from billing.models import Contract, ContractType

    refs = [r for r in (asset.code, asset.serial_or_plate) if r]
    return list(Contract.objects.filter(entity_id=asset.entity_id, contract_type=ContractType.VEHICLE_RENTAL,
                                        status=Contract.Status.ACTIVE, vehicle_ref__in=refs))


def _check_vehicle(asset):
    contracts = _vehicle_contracts(asset)
    if contracts:
        raise error("vehicle_on_contract", code=asset.code, contract=contracts[0].number or contracts[0].pk)


def _write_off_values(asset, on):
    """R23, R25: what a write-off dated ``on`` derecognises. A month already depreciated by a posted run gets no
    extra depreciation; otherwise the depreciation up to ``on`` is included (that month's run books it)."""
    period = _period_of(asset.entity, on)
    if _posted_lines(asset).filter(run__period=period).exists():
        until = period.end_date
    else:
        until = on
    depreciation = asset.opening_accumulated_depreciation + _posted_depreciation(asset)
    if asset.category.depreciable and _start_of(asset) and asset.method:
        basis = _basis(asset)
        basis.stop = until
        scheduled = sum((m.amount for m in calc.schedule(basis, until=until)), ZERO)
        depreciation = asset.opening_accumulated_depreciation + max(scheduled, _posted_depreciation(asset))
    impairment = _net_impairment(asset)
    cost = asset.cost
    return {"cost_derecognised": cost, "depreciation_derecognised": calc.q(depreciation),
            "impairment_derecognised": calc.q(impairment), "amount": calc.q(cost - depreciation - impairment),
            "depreciation_until": until}


def _impairment_cap(asset):
    """IAS 36.117 (R19): the carrying amount there would have been without the impairment, less the carrying amount
    now (both after the months already booked)."""
    posted = _posted_lines(asset)
    last = posted.order_by("-run__period__end_date").values_list("run__period__end_date", flat=True).first()
    if last is None:
        no_imp = sched_imp = ZERO
    else:
        no_imp = calc.total_through(_schedule(asset, last, with_impairment=False), last)
        sched_imp = calc.total_through(_schedule(asset, last), last)
    return calc.q((sched_imp - no_imp) + _net_impairment(asset))


def _event_values(event):
    """Computed amounts of an event (R7, R19, R23, R44); raises on invalid values."""
    asset, kind, entity = event.asset, event.kind, event.entity
    if kind == EK.FINANCED_ACQUISITION:
        if not event.financing_ref or event.cash_cost_price is None or event.financing_total is None:
            raise error("event_values", why="A financed acquisition needs the financing contract, the cash cost price "
                        "and the financing total (R7).", why_ar="يحتاج الاقتناء الممول إلى عقد التمويل وسعر التكلفة "
                        "النقدي وإجمالي التمويل.")
        if event.amount is None:
            event.amount = event.cash_cost_price
        if event.amount > event.cash_cost_price or event.financing_total < event.cash_cost_price:
            raise error(_r7_code(entity))  # R7: only the cash cost price is capitalised
    elif kind == EK.CAPITALISE_CWIP:
        target = event.target_asset
        if asset.category.asset_class != AssetClass.CWIP or target is None or target.entity_id != entity.pk or \
                not target.category.depreciable or target.status not in (AS.DRAFT, AS.ACTIVE) or target.pk == asset.pk:
            raise error("cwip_target")
        if event.amount is None or event.amount > asset.cost:
            raise error("cwip_amount", amount=event.amount, cost=asset.cost)
    elif kind == EK.SUBSEQUENT_COST:
        line, component, journal_line = event.purchase_line, event.reverses_component, event.source_journal_line
        if journal_line is not None:
            if _financing_account(journal_line.account):
                raise error(_r7_code(entity))  # R7: financing cost never capitalised
            raise error("event_values", why="Asset cost comes from bill lines, financed acquisitions and CWIP only "
                        "(OQ-22).", why_ar="تأتي تكلفة الأصل من بنود الفواتير والاقتناء الممول والمشروعات تحت التنفيذ فقط.")
        if component is not None:
            if component.asset_id != asset.pk or component.purchase_line is None or \
                    component.purchase_line.document.status != port.REVERSED:
                raise error("event_values", why="Only a component of a reversed bill of this asset is reversed.",
                            why_ar="يُعكس مكوّن فاتورة معكوسة لهذا الأصل فقط.")
            event.amount = -component.amount
        elif line is not None:
            if line.entity_id != entity.pk:
                raise error("other_entity", what="The bill line", what_ar="بند الفاتورة")
            if _financing_account(line.account):
                raise error(_r7_code(entity))
            booking = port.booking_of(line)
            if booking is None or booking.status != port.POSTED or line.account_id != asset.category.cost_account_id:
                raise error("line_not_capitalisable", line=port.document_label(line), entity=entity.code)
            remaining = _remaining(booking)
            if not remaining:
                raise error("line_fully_linked", line=port.document_label(line), amount=booking.amount)
            event.amount = remaining
        else:
            raise error("event_values", why="A subsequent cost names its bill line (R2).",
                        why_ar="تحدد التكلفة اللاحقة بند فاتورتها.")
    elif kind == EK.IMPAIRMENT:
        carrying = _carrying_now(asset)
        if event.recoverable_amount is None or event.recoverable_amount >= carrying:
            raise error("not_impaired", recoverable=event.recoverable_amount, carrying=carrying)
        event.amount = calc.q(carrying - event.recoverable_amount)
    elif kind == EK.IMPAIRMENT_REVERSAL:
        carrying = _carrying_now(asset)
        cap = _impairment_cap(asset)
        if event.recoverable_amount is None or event.recoverable_amount <= carrying or cap <= ZERO:
            raise error("nothing_to_reverse")
        event.amount = calc.q(min(event.recoverable_amount - carrying, cap))
    elif kind == EK.WRITE_OFF:
        for name, value in _write_off_values(asset, event.event_date).items():
            setattr(event, name, value)
    elif kind == EK.CHANGE_OF_ESTIMATE:
        if not (event.new_method or event.new_useful_life_months or event.new_annual_rate is not None
                or event.new_residual_value is not None):
            raise error("event_values", why="A change of estimate names the new life, residual, method or rate.",
                        why_ar="يحدد تغيير التقدير العمر أو القيمة المتبقية أو الطريقة أو النسبة الجديدة.")
        method = event.new_method or asset.method
        problems = _policy_problems(method, event.new_useful_life_months or asset.useful_life_months,
                                    event.new_annual_rate if event.new_annual_rate is not None else asset.annual_rate,
                                    ZERO)
        if problems:
            raise problems_error(problems, "event_values")
    elif kind == EK.VAT_USE_CHANGE:
        if event.new_vat_use not in VatUse.values or event.new_vat_use == asset.vat_use:
            raise error("event_values", why="Name a new VAT use different from the current one.",
                        why_ar="حدد استخدامًا ضريبيًا جديدًا مختلفًا عن الحالي.")
        event.old_vat_use = asset.vat_use
    return event


EVENT_STATUSES = {
    EK.FINANCED_ACQUISITION: (AS.DRAFT,),
    EK.CAPITALISE_CWIP: (AS.DRAFT, AS.SUBMITTED, AS.ACTIVE),
    EK.SUBSEQUENT_COST: (AS.ACTIVE, AS.FULLY_DEPRECIATED),
    EK.CHANGE_OF_ESTIMATE: (AS.ACTIVE,),
    EK.IMPAIRMENT: (AS.ACTIVE, AS.FULLY_DEPRECIATED),
    EK.IMPAIRMENT_REVERSAL: (AS.ACTIVE, AS.FULLY_DEPRECIATED),
    EK.WRITE_OFF: (AS.ACTIVE, AS.FULLY_DEPRECIATED),
    EK.VAT_USE_CHANGE: (AS.ACTIVE, AS.FULLY_DEPRECIATED),
}


def _refuse_kind(asset, kind):
    if kind in NOT_BUILT:
        why_en, why_ar = NOT_BUILT[kind]
        label = AssetEvent.Kind(kind).label.split(" / ")
        raise error("kind_refused", kind=label[0], kind_ar=label[-1], why=why_en, why_ar=why_ar)  # R20, R21
    if kind in SLICE_2:
        if kind != EK.RECLASSIFICATION and asset.retention_class == RetentionClass.REAL_ESTATE:
            raise error("real_estate_disposal_blocked")  # R22a
        label = AssetEvent.Kind(kind).label.split(" / ")
        raise error("kind_slice_2", kind=label[0], kind_ar=label[-1])
    if kind not in KIND_PERMISSION:
        raise error("event_values", why=f"Unknown event kind {kind!r}.", why_ar=f"نوع حدث غير معروف {kind!r}.")


def _check_event_accounts(event):
    """R45 for the accounts the event posts to."""
    category = event.asset.category
    if event.kind in (EK.IMPAIRMENT, EK.IMPAIRMENT_REVERSAL):
        _need_account(category, category.impairment_expense_account, "impairment expense")
        _need_account(category, category.accum_impairment_account, "accumulated impairment")
    elif event.kind == EK.WRITE_OFF:
        _need_account(category, category.disposal_account, "disposal")
        if event.depreciation_derecognised:
            _need_account(category, category.accum_depr_account, "accumulated depreciation")
        if event.impairment_derecognised:
            _need_account(category, category.accum_impairment_account, "accumulated impairment")


EVENT_FIELDS = ("amount", "cash_cost_price", "financing_total", "financing_ref", "recoverable_amount", "target_asset",
                "new_method", "new_useful_life_months", "new_annual_rate", "new_residual_value", "new_vat_use",
                "purchase_line", "reverses_component", "source_journal_line")
MONEY_FIELDS = {"amount": ("Amount", "المبلغ"), "cash_cost_price": ("Cash cost price", "سعر التكلفة النقدي"),
                "financing_total": ("Financing total", "إجمالي التمويل"),
                "recoverable_amount": ("Recoverable amount", "المبلغ القابل للاسترداد"),
                "new_residual_value": ("Residual value", "القيمة المتبقية")}


def draft_event(asset, *, user, kind, event_date, reason_ar, reason_en="", evidence_ref="", **values):
    """Draft an asset event (``fixed_assets.record_event``). Revaluation and fair-value changes are refused (R20, R21);
    sales and reclassification are slice 2 (real estate: R22a); amounts typed in are SAR, 2 dp (R39)."""
    entity = asset.entity
    access.require(user, entity, "fixed_assets.record_event")
    _refuse_kind(asset, kind)
    unknown = set(values) - set(EVENT_FIELDS)
    if unknown:
        raise error("event_values", why=f"Unknown fields: {', '.join(sorted(unknown))}.", why_ar="حقول غير معروفة.")
    if not (reason_ar or "").strip():
        raise error("reason_required")
    for name, (en, ar) in MONEY_FIELDS.items():
        if name in values:
            values[name] = _money(values[name], en, ar, allow_none=True)
    for name in ("target_asset", "purchase_line", "reverses_component"):
        if values.get(name) is not None:
            _same_entity(entity, values[name], "The linked record", "السجل المرتبط")
    if values.get("source_journal_line") is not None and values["source_journal_line"].entry.entity_id != entity.pk:
        raise error("other_entity", what="The journal line", what_ar="بند القيد")
    with transaction.atomic():
        asset = _lock(FixedAsset, asset.pk)
        if asset.status not in EVENT_STATUSES[kind]:
            raise error("status", what="asset", what_ar="الأصل", status=asset.status)
        event = AssetEvent(entity=entity, asset=asset, kind=kind, event_date=event_date, reason_ar=reason_ar.strip(),
                           reason_en=(reason_en or "").strip(), evidence_ref=evidence_ref or "", created_by=user,
                           source=_source(user), **{k: v for k, v in values.items()
                                                     if v is not None or k not in MONEY_FIELDS})
        _event_values(event)
        if kind == EK.WRITE_OFF:
            _check_vehicle(asset)  # R27
        return _save(event)


def record_cost_adjustment(asset, *, user, purchase_line=None, reverses_component=None, event_date=None, reason_ar,
                           reason_en=""):
    """R4: the event that brings a vendor credit note line, a further bill line or a reversed bill into an active
    asset's cost (kind ``subsequent_cost``; approved under ``fixed_assets.capitalisation``; no entry: invoicing booked
    the GL side)."""
    access.require(user, asset.entity, "fixed_assets.record_event")
    when = event_date or (purchase_line.document.accounting_date if purchase_line is not None else _today())
    return draft_event(asset, user=user, kind=EK.SUBSEQUENT_COST, event_date=when, reason_ar=reason_ar,
                       reason_en=reason_en, purchase_line=purchase_line, reverses_component=reverses_component)


def submit_event(event, *, user):
    """draft -> submitted (a person, ``fixed_assets.record_event``); opens the engine request of the kind's activity
    (R36: capitalisation, impairment, estimate_change or disposal)."""
    entity = event.entity
    access.require(user, entity, "fixed_assets.record_event")
    _human(user, entity, "fixed_assets.record_event")
    with transaction.atomic():
        event = _lock(AssetEvent, event.pk)
        if event.status != ES.DRAFT:
            raise error("not_draft")
        asset = _lock(FixedAsset, event.asset_id)
        event.asset = asset
        _refuse_kind(asset, event.kind)
        if asset.status not in EVENT_STATUSES[event.kind]:
            raise error("status", what="asset", what_ar="الأصل", status=asset.status)
        _event_values(event)
        if event.kind == EK.WRITE_OFF:
            _check_vehicle(asset)  # R27
        _check_event_accounts(event)  # R45
        event.status, event.submitted_by, event.submitted_at = ES.SUBMITTED, user, _now()
        _touch(event, user)
        _save(event)
        engine.submit(event, user, submit_permission="fixed_assets.record_event",
                      permission=KIND_PERMISSION[event.kind])
    return event


def _effective_month(entity, on):
    """R16 house convention: the first day of the earliest open month on or after ``on``."""
    period = (FiscalPeriod.objects.filter(entity=entity, status=FiscalPeriod.Status.OPEN, end_date__gte=on)
              .order_by("start_date").first())
    if period is None:
        raise error("no_period", entity=entity.code, on=on)
    return period.start_date


def approve_event(event, *, user, permission=None):
    """The final engine decision of the event's activity (R36). Posting kinds post through ``create_entry(post=True,
    grant=, document=event)`` with the grant whose permission matches the kind (R38); others are approved with no
    entry (change of estimate, VAT use, cost adjustments). ``permission``: the one the engine decided with (its
    adapter passes it); another kind's activity is refused. Refused outside the engine (access R61)."""
    entity = event.entity
    own = KIND_PERMISSION.get(event.kind)
    _through_engine(user, entity, permission or own or "fixed_assets.capitalise")
    if permission is not None and permission != own:  # R38: never decided under another kind's activity
        access.deny(user, entity, permission, "grant_wrong_permission")
    with transaction.atomic():
        event = _lock(AssetEvent, event.pk)
        grant = access.require(user, entity, own, document=event)
        if grant.permission != own:  # R38: the grant's permission matches the kind
            access.deny(user, entity, grant.permission, "grant_wrong_permission")
        if event.status != ES.SUBMITTED:
            raise error("status", what="event", what_ar="الحدث", status=event.status)
        asset = _lock(FixedAsset, event.asset_id)
        event.asset = asset
        _refuse_kind(asset, event.kind)
        if asset.status not in EVENT_STATUSES[event.kind]:
            raise error("status", what="asset", what_ar="الأصل", status=asset.status)
        _event_values(event)
        if event.kind == EK.WRITE_OFF:
            _check_vehicle(asset)
        _check_event_accounts(event)
        if event.amount is not None:
            engine.check_issued_band(grant, event, amount=abs(event.amount), currency=entity.base_currency_id)
        event.posted_by, event.posted_at = user, _now()
        if event.kind in POSTING_KINDS:
            event.status = ES.POSTED
            _save(event)
            try:
                entry = ledger.create_entry(
                    entity, event.event_date, f"{_event_label(event)} {asset.code}", None, user=user,
                    reference=asset.code, post=True, grant=grant, document=event)
            except LedgerError as exc:
                raise problems_error(exc.messages, getattr(exc, "code", None) or "posting_refused") from None
            event.journal_entry = entry
            _save(event)
        else:
            event.status = ES.APPROVED
            if event.kind == EK.CHANGE_OF_ESTIMATE:
                event.effective_from = _effective_month(entity, _today())
            _save(event)
            access.use_grant(grant, target=event)
        _apply_event(event, asset, user)
    return event


def _event_label(event):
    en, ar = AssetEvent.Kind(event.kind).label.split(" / ")
    return f"{en} / {ar}"


def _apply_event(event, asset, user):
    """The register side of an approved or posted event."""
    kind = event.kind
    settings = _settings(asset.entity)
    if kind == EK.FINANCED_ACQUISITION:
        _save(AssetCostComponent(entity=asset.entity, asset=asset, kind=AssetCostComponent.Kind.FINANCED_ACQUISITION,
                                 amount=event.amount, accounting_date=event.event_date, source_event=event,
                                 created_by=user))
        _refresh_cost(asset)
        if asset.acquisition_date is None or event.event_date < asset.acquisition_date:
            asset.acquisition_date = event.event_date
    elif kind == EK.CAPITALISE_CWIP:
        target = _lock(FixedAsset, event.target_asset_id)
        _save(AssetCostComponent(entity=asset.entity, asset=asset, kind=AssetCostComponent.Kind.CWIP_TRANSFER,
                                 amount=-event.amount, accounting_date=event.event_date, source_event=event,
                                 created_by=user))
        _save(AssetCostComponent(
            entity=asset.entity, asset=target, kind=AssetCostComponent.Kind.CWIP_TRANSFER, amount=event.amount,
            accounting_date=asset.acquisition_date or event.event_date, source_event=event, created_by=user,
            effective_from=calc.next_month(event.event_date) if target.status == AS.ACTIVE else None))
        _refresh_cost(asset)
        _refresh_cost(target)
        floor = asset.retention_until  # R33: the date is never shortened (CWIP: real estate by default)
        if floor and (target.retention_floor is None or floor > target.retention_floor):
            target.retention_floor = floor
        _refresh_dates(target, settings)
        target.version += 1
        _save(target)
    elif kind == EK.SUBSEQUENT_COST:
        _save(AssetCostComponent(
            entity=asset.entity, asset=asset, kind=AssetCostComponent.Kind.SUBSEQUENT_COST,
            purchase_line=event.purchase_line, reverses=event.reverses_component, amount=event.amount,
            accounting_date=event.event_date, source_event=event, created_by=user,
            effective_from=calc.next_month(event.event_date)))
        _refresh_cost(asset)
    elif kind == EK.WRITE_OFF:
        asset.status, asset.disposal_date = AS.DISPOSED, event.event_date
    elif kind == EK.CHANGE_OF_ESTIMATE:
        event.previous_values = {"method": asset.method, "useful_life_months": asset.useful_life_months,
                                 "annual_rate": str(asset.annual_rate) if asset.annual_rate is not None else None,
                                 "residual_value": str(asset.residual_value or ZERO)}
        _save(event)
        asset.method = event.new_method or asset.method
        asset.useful_life_months = event.new_useful_life_months or asset.useful_life_months
        if event.new_annual_rate is not None:
            asset.annual_rate = event.new_annual_rate
        if event.new_residual_value is not None:
            asset.residual_value = event.new_residual_value
    elif kind == EK.VAT_USE_CHANGE:
        asset.vat_use = event.new_vat_use  # R47: the only path that changes it
    _refresh_dates(asset, settings)
    asset.version += 1
    _save(asset)
    _refresh_status(asset)


def return_event(event, *, user, note=""):
    """submitted -> draft (engine return)."""
    event = _lock(AssetEvent, event.pk)
    if not access.has(user, event.entity, "fixed_assets.record_event"):
        access.require(user, event.entity, KIND_PERMISSION[event.kind], document=event, purpose="return")
    if event.status != ES.SUBMITTED:
        raise error("status", what="event", what_ar="الحدث", status=event.status)
    event.status = ES.DRAFT
    event.modified_at = _now()
    event.version += 1
    return _save(event)


def discard_event(event, *, user):
    """Drop a draft or submitted event (a person, ``fixed_assets.record_event``) and withdraw its request."""
    entity = event.entity
    access.require(user, entity, "fixed_assets.record_event")
    _human(user, entity, "fixed_assets.record_event")
    with transaction.atomic():
        event = _lock(AssetEvent, event.pk)
        if event.status not in (ES.DRAFT, ES.SUBMITTED):
            raise error("status", what="event", what_ar="الحدث", status=event.status)
        if event.kind in KIND_PERMISSION:
            engine.withdraw(event, user, permission=KIND_PERMISSION[event.kind])
        event.status = ES.DISCARDED
        _touch(event, user)
        _save(event)
    return event


def _event_lines(event):
    """Registered ``build_lines`` of AssetEvent (FA4 to FA6, FA8)."""
    asset, category = event.asset, event.asset.category
    label = f"{_event_label(event)} {asset.code}"
    out = []

    def pair(debit, credit, amount):
        if amount:
            out.append({"account": debit, "debit": amount, "description": label})
            out.append({"account": credit, "credit": amount, "description": label})

    if event.kind == EK.FINANCED_ACQUISITION:
        pair(category.cost_account, Account.objects.get(entity=event.entity, code=template.FINANCING), event.amount)
    elif event.kind == EK.CAPITALISE_CWIP:
        pair(event.target_asset.category.cost_account, category.cost_account, event.amount)
    elif event.kind == EK.IMPAIRMENT:
        pair(category.impairment_expense_account, category.accum_impairment_account, event.amount)
    elif event.kind == EK.IMPAIRMENT_REVERSAL:
        pair(category.accum_impairment_account, category.impairment_expense_account, event.amount)
    elif event.kind == EK.WRITE_OFF:
        for account, amount in ((category.accum_depr_account, event.depreciation_derecognised),
                                (category.accum_impairment_account, event.impairment_derecognised),
                                (category.disposal_account, event.amount)):
            if amount:
                out.append({"account": account, "debit": amount, "description": label})
        out.append({"account": category.cost_account, "credit": event.cost_derecognised, "description": label})
    return out


def _event_accounts(event):
    category = event.asset.category
    ids = {category.cost_account_id, category.accum_depr_account_id, category.accum_impairment_account_id,
           category.impairment_expense_account_id, category.disposal_account_id, category.depr_expense_account_id}
    if event.kind == EK.FINANCED_ACQUISITION:
        ids.add(Account.objects.get(entity=event.entity, code=template.FINANCING).pk)  # R38: 2400 for FA8 only
    if event.kind == EK.CAPITALISE_CWIP and event.target_asset_id:
        ids.add(event.target_asset.category.cost_account_id)
    return ids - {None}


def _validate_event_lines(event, lines):
    expected = {EK.WRITE_OFF: event.cost_derecognised}.get(event.kind, event.amount) or ZERO
    return _line_problems(event.entity, lines, _event_accounts(event), expected, f"event {event.pk}")


def _pending_events(entity, start_date, end_date):
    """Registered ``pending_documents``: submitted events dated in the period, and posted bill lines on asset cost
    accounts not in the register (R43, ``unregistered_capital_lines``)."""
    out = [f"Asset event #{e.pk} ({e.kind}, submitted) / حدث أصل رقم {e.pk}"
           for e in AssetEvent.objects.filter(entity=entity, status=ES.SUBMITTED, event_date__gte=start_date,
                                              event_date__lte=end_date)]
    lines = _unregistered_lines(entity, start_date, end_date)
    if lines:
        period = FiscalPeriod.objects.filter(entity=entity, start_date=start_date).values_list("name", flat=True).first()
        out.append(bi("unregistered_capital_lines", period=period or f"{start_date}..{end_date}", lines=", ".join(lines)))
    return out


# --- reversals (R18) ----------------------------------------------------------------------------------------------

_reversing = ContextVar("fixed_assets_reversing", default=None)


def _reverse_guard(document):
    """Registered ``reverse_guard``: only ``fixed_assets.services.reverse_document`` reverses runs and events."""
    return _reversing.get() == (document._meta.label_lower, document.pk)


def _submit_right(document):
    return "fixed_assets.prepare_depreciation" if isinstance(document, DepreciationRun) else "fixed_assets.record_event"


def request_reversal(document, *, user, reason_ar, reason_en=""):
    """Ask for the reversal of a posted run or event (a person with the document's own submit right); opens the engine
    request of ``fixed_assets.reversal`` (R18)."""
    entity = document.entity
    right = _submit_right(document)
    access.require(user, entity, right)
    _human(user, entity, right)
    if not (reason_ar or "").strip():
        raise error("reason_required")
    with transaction.atomic():
        document = _lock(type(document), document.pk)
        if document.status != "posted" or document.journal_entry_id is None:
            raise error("not_posted")
        document.reversal_requested_by, document.reversal_requested_at = user, _now()
        document.reversal_reason_ar, document.reversal_reason_en = reason_ar.strip(), (reason_en or "").strip()
        _save(document)
        engine.submit(document, user, submit_permission=right, permission="fixed_assets.reverse_document")
    return document


def _later_dependants(document):
    if isinstance(document, DepreciationRun):
        return DepreciationRun.objects.filter(entity=document.entity, status=RS.POSTED,
                                              period__start_date__gt=document.period.start_date).exists()
    asset = document.asset
    if document.kind in (EK.FINANCED_ACQUISITION, EK.CAPITALISE_CWIP):
        target = document.target_asset if document.kind == EK.CAPITALISE_CWIP else asset
        return target.status != AS.DRAFT
    if document.kind in (EK.IMPAIRMENT, EK.IMPAIRMENT_REVERSAL):
        return _posted_lines(asset).filter(run__period__start_date__gt=document.event_date).exists() or \
            asset.events.filter(status=ES.POSTED, event_date__gte=document.event_date).exclude(pk=document.pk).exists()
    return False


def reverse_document(document, *, user, date=None):
    """The final engine decision of ``fixed_assets.reversal``: reverse a posted run or event through
    ``ledger.services.reverse_entry`` with the ``fixed_assets.reverse_document`` grant (R18). The run is marked
    ``reversed`` and its period may be run again while open. Refused outside the engine (access R61)."""
    entity = document.entity
    _through_engine(user, entity, "fixed_assets.reverse_document")
    model = type(document)
    with transaction.atomic():
        document = _lock(model, document.pk)
        grant = access.require(user, entity, "fixed_assets.reverse_document", document=document, through_engine=True)
        if document.status != "posted" or document.journal_entry_id is None:
            raise error("not_posted")
        if not document.reversal_reason_ar:
            raise error("reason_required")
        if _later_dependants(document):
            raise error("reversal_order")
        token = _reversing.set((model._meta.label_lower, document.pk))
        try:
            reversal = ledger.reverse_entry(document.journal_entry, grant, date=date,
                                            reason=document.reversal_reason_en or document.reversal_reason_ar)
        except LedgerError as exc:
            raise problems_error(exc.messages, getattr(exc, "code", None) or "reversal_refused") from None
        finally:
            _reversing.reset(token)
        document.status, document.reversal_entry = "reversed", reversal
        _save(document)
        if isinstance(document, DepreciationRun):
            for asset_id in set(document.lines.values_list("asset_id", flat=True)):
                _refresh_status(FixedAsset.objects.get(pk=asset_id))
        else:
            _undo_event(document, user)
    return document


def cancel_reversal(document, *, user, note=""):
    """Engine return of a reversal request: the request closes and the document stays posted."""
    document = _lock(type(document), document.pk)
    if not access.has(user, document.entity, _submit_right(document)):
        access.require(user, document.entity, "fixed_assets.reverse_document", document=document, purpose="return",
                       through_engine=True)
    document.reversal_requested_by = None
    document.reversal_requested_at = None
    return _save(document)


def _undo_event(event, user):
    asset = _lock(FixedAsset, event.asset_id)
    for component in AssetCostComponent.objects.filter(source_event=event, reverses__isnull=True):
        _save(AssetCostComponent(entity=component.entity, asset=component.asset, kind=component.kind,
                                 amount=-component.amount, accounting_date=component.accounting_date,
                                 reverses=component, source_event=event, created_by=user,
                                 effective_from=component.effective_from))
        if component.asset_id != asset.pk:
            other = _lock(FixedAsset, component.asset_id)
            _refresh_cost(other)
            other.version += 1
            _save(other)
    if event.kind == EK.WRITE_OFF:
        asset.status, asset.disposal_date = AS.ACTIVE, None
    _refresh_cost(asset)
    asset.version += 1
    _save(asset)
    _refresh_status(asset)


# --- reports (R9, R34, R46, R48) ----------------------------------------------------------------------------------

def _gl_balance(entity, code, as_of):
    rows = JournalLine.objects.filter(entry__entity=entity, entry__status__in=BOOKED, entry__date__lte=as_of,
                                      account__code=code).aggregate(d=Sum("debit"), c=Sum("credit"))
    return calc.q((rows["d"] or ZERO) - (rows["c"] or ZERO))


def register_to_gl(entity, as_of, *, user):
    """R9: per asset account, the register's balance against the ledger's on ``as_of`` (zero difference expected).
    Migrated assets count once the entity's opening entry is posted (R40)."""
    access.require(user, entity, VIEW)
    opening = ledger.migration_opening_posted(entity)
    register_side = {}

    def add(account, amount):
        if account is not None:
            register_side[account.code] = register_side.get(account.code, ZERO) + amount

    for asset in FixedAsset.objects.filter(entity=entity).select_related(
            "category__cost_account", "category__accum_depr_account", "category__accum_impairment_account"):
        if asset.is_migrated and not opening:
            continue
        c = asset.category
        add(c.cost_account, asset.components.filter(accounting_date__lte=as_of).aggregate(t=Sum("amount"))["t"] or ZERO)
        depreciation = (asset.opening_accumulated_depreciation if asset.opening_date and asset.opening_date <= as_of
                        else ZERO) + _posted_depreciation(asset, as_of)
        written = _posted_events(asset, EK.WRITE_OFF, through=as_of).first()
        if written is not None:
            add(c.cost_account, -written.cost_derecognised)
            depreciation -= written.depreciation_derecognised
        add(c.accum_depr_account, -depreciation)
        impairment = _net_impairment(asset, as_of) - (written.impairment_derecognised if written else ZERO)
        add(c.accum_impairment_account, -impairment)
    codes = set(register_side) | set(AssetCategory.objects.filter(entity=entity).values_list("cost_account__code",
                                                                                              flat=True))
    rows = []
    for code in sorted(codes):
        reg = calc.q(register_side.get(code, ZERO))
        gl = _gl_balance(entity, code, as_of)
        rows.append({"account": code, "register": reg, "ledger": gl, "difference": gl - reg})
    return rows


def register_report(entity, *, user):
    """Register report (R4a, R40, R46): assets with missing VAT / Zakat data, deferred-payment assets, and the
    "normal credit terms not set" note while the CFO has not set them."""
    access.require(user, entity, VIEW)
    settings = _settings(entity)
    assets = list(FixedAsset.objects.filter(entity=entity).exclude(status=AS.DISPOSED).order_by("code"))
    notes = []
    if settings.normal_credit_days is None:
        notes.append("Normal credit terms not set / لم تُحدَّد مدة الائتمان المعتادة")
    return {
        "notes": notes,
        "deferred_payment": [a.code for a in assets if a.deferred_payment_flag],
        "missing": [{"asset": a.code, "fields": [f for f in ("vat_use", "held_for") if not getattr(a, f)]
                                                + (["initial_recovery_pct"] if a.initial_recovery_pct is None else [])}
                    for a in assets if not (a.vat_use and a.held_for and a.initial_recovery_pct is not None)],
    }


def zakat_inputs(entity, as_of, *, user):
    """R34: book values per category and class on ``as_of`` (CWIP and investment property apart), assets disposed in
    the year with proceeds, and per asset ``held_for`` and its evidence. The module never decides the Zakat
    treatment (tax-returns.md; Needs expert sign-off: tax)."""
    access.require(user, entity, VIEW)
    sections = {"ppe": {}, "cwip": {}, "investment_property": {}}
    assets, disposed = [], []
    opening = ledger.migration_opening_posted(entity)
    for asset in FixedAsset.objects.filter(entity=entity).select_related("category").order_by("code"):
        if (asset.acquisition_date and asset.acquisition_date > as_of) or (asset.is_migrated and not opening) or \
                not asset.cost and asset.status != AS.DISPOSED:
            continue  # drafts count too: their bills are in the GL (the CWIP accumulates as a draft, R44)
        v = _values(asset, as_of)
        if v["disposed"]:
            if asset.disposal_date.year == as_of.year:
                disposed.append({"asset": asset.code, "date": asset.disposal_date, "proceeds": ZERO,
                                 "nbv": calc.q(_write_off_nbv(asset))})
            continue
        klass = asset.category.asset_class
        key = "cwip" if klass == AssetClass.CWIP else "investment_property" if klass in IP_CLASSES else "ppe"
        row = sections[key].setdefault((asset.category.code, klass), {
            "category": asset.category.code, "asset_class": klass, "cost": ZERO, "accumulated_depreciation": ZERO,
            "accumulated_impairment": ZERO, "nbv": ZERO})
        for name in ("cost", "accumulated_depreciation", "accumulated_impairment", "nbv"):
            row[name] += v[name]
        assets.append({"asset": asset.code, "category": asset.category.code, "asset_class": klass, "nbv": v["nbv"],
                       "held_for": asset.held_for, "use_evidence_ref": asset.use_evidence_ref})
    return {"as_of": as_of, **{k: list(v.values()) for k, v in sections.items()}, "disposed_in_year": disposed,
            "assets": assets, "gl": register_to_gl(entity, as_of, user=user)}


def _write_off_nbv(asset):
    event = _posted_events(asset, EK.WRITE_OFF).first()
    return event.amount if event is not None else ZERO


def record_ip_fair_value(asset, *, user, valuation_date, amount=None, valuer_name_ar="", valuer_name_en="",
                         valuer_licence_ref="", evidence_ref="", undue_cost_justification_ar="",
                         undue_cost_justification_en=""):
    """R48: append a fair value (or the undue-cost-or-effort justification) for an investment property of the same
    entity (``fixed_assets.record_event``). Posts nothing."""
    entity = asset.entity
    access.require(user, entity, "fixed_assets.record_event")
    if asset.category.asset_class not in IP_CLASSES:
        raise error("not_ip")
    value = _money(amount, "Fair value", "القيمة العادلة", allow_none=True)
    if value is None and not (undue_cost_justification_ar or "").strip():
        raise error("event_values", why="Record a fair value or the justification for not measuring it.",
                    why_ar="سجّل القيمة العادلة أو مبرر عدم قياسها.")
    if value is not None and not ((valuer_name_ar or valuer_name_en) and evidence_ref):
        raise error("event_values", why="A fair value needs its valuer and evidence (R48).",
                    why_ar="تحتاج القيمة العادلة إلى المقيّم والمستند المؤيد.")
    row = IpFairValue(entity=entity, asset=asset, valuation_date=valuation_date, amount=value,
                      valuer_name_ar=valuer_name_ar or "", valuer_name_en=valuer_name_en or "",
                      valuer_licence_ref=valuer_licence_ref or "", evidence_ref=evidence_ref or "",
                      undue_cost_justification_ar=undue_cost_justification_ar or "",
                      undue_cost_justification_en=undue_cost_justification_en or "", created_by=user)
    return _save(row)


def _fiscal_year_end(entity, on):
    last = FiscalPeriod.objects.filter(entity=entity, start_date__year=on.year).order_by("-end_date").first()
    return last is not None and last.end_date == on


def ip_disclosure(entity, as_of, *, user):
    """R48: per investment property, carrying amount, latest fair value (or none), valuer and justification; for an
    IFRS for SMEs entity at a fiscal year end, a property with neither is listed as a gap (en/ar)."""
    access.require(user, entity, VIEW)
    rows, gaps = [], []
    year_end = _fiscal_year_end(entity, as_of)
    for asset in FixedAsset.objects.filter(entity=entity, category__asset_class__in=list(IP_CLASSES)).exclude(
            status__in=(AS.DRAFT, AS.SUBMITTED, AS.DISPOSED)).order_by("code"):
        fv = asset.fair_values.filter(valuation_date__lte=as_of, amount__isnull=False).first()
        justification = asset.fair_values.filter(valuation_date__lte=as_of).exclude(
            undue_cost_justification_ar="").first()
        rows.append({"asset": asset.code, "carrying_amount": _values(asset, as_of)["nbv"],
                     "fair_value": fv.amount if fv else None, "fair_value_text": None if fv else "none / لا يوجد",
                     "valuer": (fv.valuer_name_en or fv.valuer_name_ar) if fv else "",
                     "justification": justification.undue_cost_justification_ar if justification else ""})
        if entity.reporting_standard == Entity.Standard.IFRS_SME and year_end and fv is None and justification is None:
            gaps.append(f"{asset.code}: no fair value and no undue-cost justification at {as_of} (IFRS for SMEs s16) / "
                        f"{asset.code}: لا قيمة عادلة ولا مبرر لعدم قياسها في {as_of}")
    return {"as_of": as_of, "rows": rows, "gaps": gaps}


def _vat_history(asset):
    for e in asset.events.filter(kind=EK.VAT_USE_CHANGE, status=ES.APPROVED).order_by("event_date", "pk"):
        yield {"from": e.event_date, "old": e.old_vat_use, "new": e.new_vat_use, "event": e.pk}


def vat_use_on(asset, on, *, user):
    """R47: the asset's VAT use on ``on`` from its history of approved ``vat_use_change`` events."""
    access.require(user, asset.entity, VIEW)
    history = list(_vat_history(asset))
    current = history[0]["old"] if history else asset.vat_use
    for row in history:
        if row["from"] <= on:
            current = row["new"]
    return current
