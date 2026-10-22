"""Test helpers for the fixed-asset module (fixed-assets.md §8). Fictional entities, users and amounts only; the
policy values are fixture values, not policy (OQ-2). Never used outside tests."""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from core.access import engine
from core.access.services import now as access_now
from core.access.models import Membership, Role, RolePermission
from core.access.testing import make_agent, make_user, member
from core.models import Entity
from invoicing import services as inv
from invoicing.conftest import SELLER
from ledger.models import Account, FiscalPeriod
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts

from . import services, template

D = Decimal
_seq = iter(range(1, 1_000_000))

# Fixture policies (category code -> (method, life months, annual rate)); test values only (OQ-2).
POLICIES = {"VEH": ("straight_line", 60, None), "VEHR": ("straight_line", 60, None), "IT": ("straight_line", 36, None),
            "MACH": ("straight_line", 120, None), "IPB": ("straight_line", 480, None),
            "BLDG": ("straight_line", 600, None), "LHI": ("straight_line", 120, None),
            "OTHER": ("straight_line", 60, None)}


def install_proposed_accounts(entity):
    """The proposed accounts of §5 as fixture data (OQ-5); the chart template is unchanged."""
    for code, en, ar, typ in template.PROPOSED_ACCOUNTS:
        parent = Account.objects.filter(entity=entity, code=code[0] + "000").first()
        Account.objects.get_or_create(entity=entity, code=code, defaults=dict(
            name_en=en, name_ar=ar, type=typ, parent=parent, is_postable=True))


def make_entity(code, consolidation="full", parent=None, *, proposed=True, years=(2027,), **extra):
    fields = {**SELLER, **extra}
    entity = Entity.objects.create(code=code, name_en=f"{code} Co", name_ar=f"شركة {code}",
                                   legal_name_en=f"{code} Test Company", legal_name_ar=f"شركة {code} للاختبار",
                                   consolidation=consolidation, parent=parent, **fields)
    install_chart_of_accounts(entity)
    if proposed:
        install_proposed_accounts(entity)
    for year in years:
        create_monthly_periods(entity, year)
    return entity


def agent_role():
    role, created = Role.objects.get_or_create(code="agent_fa_drafter", defaults=dict(
        name_en="Agent: fixed-asset drafter", name_ar="وكيل مسودات الأصول", scope="entity", kind="agent"))
    if created:
        for codename in ("fixed_assets.view", "fixed_assets.create_asset", "fixed_assets.prepare_depreciation"):
            RolePermission.objects.create(role=role, codename=codename)
    return role


def world(*, entities=("ALM", "SUB", "NSH", "FUL"), years=(2027,), policies=True):
    """Entities, users, vendors and approved fixture policies of §8."""
    ns = SimpleNamespace()
    ns.alm = make_entity("ALM", "parent", years=years, vat_number="300000000000003", cr_number="1010000001",
                         unified_number="7000000001")
    ns.entities = {"ALM": ns.alm}
    if "SUB" in entities:
        ns.sub = ns.entities["SUB"] = make_entity("SUB", "full", ns.alm, years=years, vat_number="311111111100003")
    if "NSH" in entities:
        ns.nsh = ns.entities["NSH"] = make_entity("NSH", "full", ns.alm, years=years, sharia_enforced=False)
    if "FUL" in entities:
        ns.ful = ns.entities["FUL"] = make_entity("FUL", "full", ns.alm, years=years, sharia_enforced=False,
                                                   reporting_standard="ifrs_full")
    others = [e for c, e in ns.entities.items() if c not in ("ALM", "SUB")]
    for name, role, where in (
        ("a", "accountant", [ns.alm, *others]), ("a2", "accountant", [ns.alm]), ("c", "approver", [ns.alm, *others]),
        ("c2", "approver", [ns.alm]), ("v", "internal_auditor", [ns.alm]), ("m", "managing_partner", [ns.alm]),
        ("ca", "ceo", [ns.alm]),
    ):
        user = make_user(name)
        for entity in where:
            member(user, entity, role)
        setattr(ns, name, user)
    if "SUB" in entities:
        ns.ce = make_user("ce")
        member(ns.ce, ns.sub, "ceo")
        ns.sa = make_user("sa")
        member(ns.sa, ns.sub, "accountant")
        ns.sc = make_user("sc")
        member(ns.sc, ns.sub, "approver")
    ns.g = make_agent("fa_drafter")
    Membership.objects.create(user=ns.g, entity=ns.alm, role=agent_role(), valid_from=access_now() - timedelta(days=1),
                              granted_by=ns.m, reason="pilot")
    ns.vendors = {}
    for code, entity in ns.entities.items():
        setup_user = ns.sa if code == "SUB" else ns.a
        services.setup_entity(entity, user=setup_user)
        ns.vendors[code] = inv.create_vendor(entity, user=setup_user, vendor_kind="business", code="V001",
                                             name_en="Test Supplier", name_ar="المورد التجريبي",
                                             is_vat_registered=True, vat_number="320000000000003")
        loc = services.create_location(entity, user=setup_user, code="HQ", name_ar="المقر الرئيسي", name_en="HQ",
                                       city_ar="الرياض", city_en="Riyadh")
        setattr(ns, f"loc_{code.lower()}", loc)
    ns.loc = ns.loc_alm
    if policies:
        for code, entity in ns.entities.items():
            approve_policies(ns, entity)
    return ns


def staff(w, entity):
    return (w.sa, w.sc) if getattr(w, "sub", None) is not None and entity == w.sub else (w.a, w.c)


def category(entity, code):
    from .models import AssetCategory

    return AssetCategory.objects.get(entity=entity, code=code)


def approve_policies(w, entity, codes=None, effective=date(2020, 1, 1)):
    preparer, approver = staff(w, entity)
    for code, (method, life, rate) in POLICIES.items():
        if codes is not None and code not in codes:
            continue
        cat = category(entity, code) if _has_category(entity, code) else None
        if cat is None:
            continue
        policy = services.draft_policy(cat, user=preparer, effective_from=effective, method=method,
                                       useful_life_months=life, annual_rate=rate, basis_note_ar="سياسة اختبار",
                                       basis_note_en="Test policy (fixture value)")
        services.submit_policy(policy, user=preparer)
        engine.decide(policy, approver, permission="fixed_assets.approve_policy")


def _has_category(entity, code):
    from .models import AssetCategory

    return AssetCategory.objects.filter(entity=entity, code=code).exists()


def bill(w, entity, lines, *, on=date(2027, 1, 15), due=None, currency=None, doc_type="bill", original=None,
         post=True, user=None, approver=None):
    """A posted bill (or vendor credit note) with ``lines`` of (account, price[, kwargs]); totals computed."""
    preparer, cfo = staff(w, entity)
    user, approver = user or preparer, approver or cfo
    doc = inv.create_purchase_document(entity, user=user, vendor=w.vendors[entity.code],
                                       vendor_document_number=f"B-{next(_seq)}", vendor_document_date=on,
                                       accounting_date=on, due_date=due, currency=currency, doc_type=doc_type,
                                       original_document=original)
    for account, price, *extra in lines:
        inv.add_purchase_line(doc, user=user, account=account, quantity="1", unit_price=str(price),
                              description_ar="أصل تجريبي", description_en="Test asset", **(extra[0] if extra else {}))
    doc.refresh_from_db()
    _, net, charged, _, _, _, _ = inv._purchase_amounts(doc, list(doc.lines.select_related("vat_rate")))
    inv.update_purchase_document(doc, user=user, vendor_total_vat=charged, vendor_total_gross=net + charged)
    doc = inv.submit_purchase_document(doc, user=user)
    if post:
        doc = inv.approve_purchase_document(doc, user=approver)
    return doc


def line_of(doc, n=1):
    return doc.lines.get(line_no=n)


def decide(document, user, permission=None, **kwargs):
    return engine.decide(document, user, permission=permission, **kwargs)


def capitalise(w, line, *, available, entity=None, category_code=None, vat_use="taxable", held_for="own_use",
               approver=None, **fields):
    """Draft from a bill line, submit and approve: an active asset."""
    entity = entity or line.entity
    preparer, cfo = staff(w, entity)
    cat = category(entity, category_code) if category_code else None
    loc = getattr(w, f"loc_{entity.code.lower()}")
    asset = services.create_asset_from_line(line, user=preparer, category=cat, location=loc, vat_use=vat_use,
                                            held_for=held_for, **fields)
    asset = services.submit_asset(asset, user=preparer, available_for_use_date=available)
    decide(asset, approver or cfo, permission="fixed_assets.capitalise")
    asset.refresh_from_db()
    return asset


def period(entity, name):
    return FiscalPeriod.objects.get(entity=entity, name=name)


def run(w, entity, name, *, preparer=None, approver=None):
    """Prepare, submit and post the run of period ``name``."""
    p, c = staff(w, entity)
    r = services.prepare_depreciation(entity, period(entity, name), user=preparer or p)
    r = services.submit_depreciation(r, user=preparer or p)
    decide(r, approver or c, permission="fixed_assets.run_depreciation")
    r.refresh_from_db()
    return r


def event(w, asset, kind, on, *, approve=True, user=None, approver=None, **values):
    p, c = staff(w, asset.entity)
    e = services.draft_event(asset, user=user or p, kind=kind, event_date=on, reason_ar="سبب تجريبي",
                             reason_en="Test reason", **values)
    e = services.submit_event(e, user=user or p)
    if approve:
        decide(e, approver or c, permission=services.KIND_PERMISSION[kind])
        e.refresh_from_db()
    return e


def close(w, entity, name, user=None):
    from ledger import services as ledger

    return ledger.close_period(period(entity, name), user=user or staff(w, entity)[1])


def lines_of(document):
    return sorted((l.account.code, l.debit, l.credit)
                  for l in document.journal_entry.lines.select_related("account"))
