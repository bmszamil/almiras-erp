"""Opening register from the migration workbook and its R22 tie-out (fixed-assets.md R40, R41; AT-32, AT-33).
Fictional DRYH data (migration/fixtures/dry_run.py); policies are fixture values (OQ-2)."""
from datetime import date
from decimal import Decimal

import pytest

from core import access
from core.access.authority_models import ApprovalRequest
from core.access.testing import make_agent, make_user, member
from fixed_assets import migration_port, services, testing as t
from fixed_assets.messages import FixedAssetError
from fixed_assets.models import FixedAsset
from ledger.models import JournalEntry
from migration import services as migration, subledgers, workbook
from migration import testing as mt
from migration.conftest import make_entity
from migration.fixtures import dry_run as f

pytestmark = pytest.mark.django_db
D = Decimal
H = f.HOLDING


def book_of(rows):
    return workbook.read_workbook(mt.filled_workbook(rows))


def opening_tb(vehicles="240000.00"):
    """DRYH's TB with only the fixed-asset accounts and the share capital that balances them."""
    rows = f.workbook_rows()
    equity = -(D(vehicles) + D("36000.00") - D("101500.00"))
    rows["trial_balance"] = [
        {"entity": H, "code": c, "name": f.CHART[H][c][0], "debit": D(v) if D(v) > 0 else None,
         "credit": -D(v) if D(v) < 0 else None}
        for c, v in (("1501", vehicles), ("1502", "36000.00"), ("1591", "-101500.00"), ("2601", str(equity)))]
    return workbook.trial_balance_for(book_of(rows), H)


@pytest.fixture
def dry(db):
    h = make_entity(H, "parent")
    make_entity(f.SUBSIDIARY, "full", h, sharia_enforced=False)  # the map names DRYS as a counterparty
    acc, apr = make_user("dry.acc"), make_user("dry.apr")
    member(acc, h, "accountant")
    member(apr, h, "approver")
    mt.approve_map(migration.create_account_map(h, user=acc, rows=f.map_rows(H)), approver=apr)
    f.setup_fixed_assets(h, acc)
    for code, life in (("VEH", 60), ("ITF", 36)):
        p = services.draft_policy(t.category(h, code), user=acc, effective_from=date(2020, 1, 1),
                                  useful_life_months=life, basis_note_ar="سياسة اختبار")
        services.submit_policy(p, user=acc)
        t.decide(p, apr, permission="fixed_assets.approve_policy")
    return type("Dry", (), {"h": h, "acc": acc, "apr": apr, "book": book_of(f.workbook_rows())})


def test_at32_the_opening_register_loads_once_and_counts_after_the_opening_entry(dry):
    plan = subledgers.load_subledgers(dry.h, dry.book, ["fixed_assets"], user=dry.acc)  # dry run
    assert plan.ok and not plan.applied and not FixedAsset.objects.exists()
    plan = subledgers.load_subledgers(dry.h, dry.book, ["fixed_assets"], user=dry.acc, apply=True)
    assert plan.applied and plan.sections["fixed_assets"].created == 3
    assets = {a.legacy_code: a for a in FixedAsset.objects.all()}
    vehicle = assets["FA-F-01"]
    assert {(a.status, a.source, a.held_for, a.legacy_source_system) for a in assets.values()} == {
        ("active", "import", "own_use", "falcon")}
    assert (vehicle.cost, vehicle.opening_accumulated_depreciation, vehicle.residual_value,
            vehicle.opening_remaining_life_months, vehicle.opening_date) == (
        D("100000.00"), D("40000.00"), D("0.00"), 36, f.CUTOVER)
    assert vehicle.components.get().kind == "opening_balance" and vehicle.category.code == "VEH"
    assert assets["FA-F-03"].category.code == "ITF"
    missing = {r["asset"]: r["fields"] for r in services.register_report(dry.h, user=dry.acc)["missing"]}
    assert "vat_use" in missing[vehicle.code]  # left for the accountant (OQ-27)
    assert not JournalEntry.objects.exists() and not ApprovalRequest.objects.filter(
        document_type="fixed_assets.fixedasset").exists()
    rerun = subledgers.load_subledgers(dry.h, dry.book, ["fixed_assets"], user=dry.acc, apply=True)
    assert (rerun.sections["fixed_assets"].created, rerun.sections["fixed_assets"].skipped) == (0, 3)
    rows = f.workbook_rows()
    first, second = rows["fixed_assets"][0], rows["fixed_assets"][1]
    first["cost"] -= D("1000.00")  # the sheet still ties to 1501: only the asset changed
    second["cost"] += D("1000.00")
    changed = subledgers.load_subledgers(dry.h, book_of(rows), ["fixed_assets"], user=dry.acc, apply=True)
    assert not changed.applied and any("FA-F-01" in p and "سبق تحميل" in p for p in changed.problems)
    with pytest.raises(access.AccessDenied) as exc:
        subledgers.load_subledgers(dry.h, dry.book, ["fixed_assets"], user=make_agent("migrator"), apply=True)
    assert exc.value.code == "agent_forbidden"
    with pytest.raises(FixedAssetError) as exc:
        services.prepare_depreciation(dry.h, t.period(dry.h, "2027-01"), user=dry.acc)
    assert exc.value.code == "opening_not_posted" and H in str(exc.value)
    migration.load_opening_balances(dry.h, opening_tb(), user=dry.acc, cutover=f.CUTOVER, apply=True)
    result = migration.post_imported_entries(dry.h, user=dry.apr, kind="opening", apply=True)
    assert result["posted"], result
    run = services.prepare_depreciation(dry.h, t.period(dry.h, "2027-01"), user=dry.acc)
    assert run.lines.get(asset=vehicle).amount == D("1666.67")  # (100,000 - 40,000) / 36
    late = subledgers.load_subledgers(dry.h, dry.book, ["fixed_assets"], user=dry.acc, apply=True)
    assert not late.applied and "is posted" in late.sections["fixed_assets"].problems[0]
    with pytest.raises(FixedAssetError) as exc:
        migration_port.load_opening_asset(
            dry.h, user=dry.acc, legacy_code="FA-F-09", description_ar="أصل متأخر", asset_class="vehicle",
            usage="own_use", location="المقر", cost_account=vehicle.category.cost_account,
            acquisition_date=date(2025, 1, 1), cost=D("1000.00"), cutover=f.CUTOVER)
    assert exc.value.code == "opening_posted"
    rows = services.register_to_gl(dry.h, f.CUTOVER, user=dry.acc)
    assert all(r["difference"] == 0 for r in rows), rows


def test_at33_the_opening_entry_ties_to_the_register_to_the_halala(dry):
    subledgers.load_subledgers(dry.h, dry.book, ["fixed_assets"], user=dry.acc, apply=True)
    migration.load_opening_balances(dry.h, opening_tb("240000.01"), user=dry.acc, cutover=f.CUTOVER, apply=True)
    result = migration.post_imported_entries(dry.h, user=dry.apr, kind="opening", apply=True)
    assert not result["posted"]
    detail = " ".join(result["details"]["opening"])
    assert "1520" in detail and "240000.01" in detail and "difference 0.01" in detail and "الفرق 0.01" in detail
    assert not JournalEntry.objects.filter(status="posted").exists()
    migration.discard_imported_drafts(dry.h, user=dry.acc, kind="opening", apply=True)
    migration.load_opening_balances(dry.h, opening_tb(), user=dry.acc, cutover=f.CUTOVER, apply=True)
    result = migration.post_imported_entries(dry.h, user=dry.apr, kind="opening", apply=True)
    assert result["posted"]
    assert migration_port.opening_register_balances(dry.h, "falcon") == {
        ("1520", ""): D("240000.00"), ("1500", ""): D("36000.00"), ("1590", ""): D("-101500.00")}
