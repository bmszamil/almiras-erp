"""Depreciation runs (fixed-assets.md R10 to R18, R43; AT-2 to AT-8, AT-20, AT-21, AT-35). Fictional data; policies
are fixture values (OQ-2)."""
from datetime import date
from decimal import Decimal

import pytest

from core import access
from core.access.authority_models import ApprovalRequest
from fixed_assets import migration_port, services, testing as t
from fixed_assets.messages import FixedAssetError
from fixed_assets.models import DepreciationLine, DepreciationRun
from ledger import services as ledger
from ledger.models import JournalEntry, LedgerError

pytestmark = pytest.mark.django_db
D = Decimal


def code_of(exc):
    return getattr(exc.value, "code", None)


def vehicle(w, *, on=date(2027, 1, 5), available=date(2027, 1, 10), cost="120000.00", residual="20000.00",
            entity=None, category_code=None):
    entity = entity or w.alm
    bill = t.bill(w, entity, [("1520", cost)], on=on)
    return t.capitalise(w, t.line_of(bill), available=available, residual_value=D(residual),
                        category_code=category_code)


# --- AT-2, R10, R11, R13 ------------------------------------------------------------------------------------------

@pytest.fixture
def w6(db):
    return t.world(entities=("ALM",), years=range(2027, 2033))


def test_at2_straight_line_prorated_by_days_and_the_last_month_absorbs_the_residue(w6):
    w = w6
    asset = vehicle(w)
    amounts = []
    for year in range(2027, 2033):
        for month in range(1, 13):
            name = f"{year}-{month:02d}"
            try:
                run = t.run(w, w.alm, name)
            except FixedAssetError as exc:
                assert exc.code == "nothing_to_depreciate" and name == "2032-02"
                break
            amounts.append(run.lines.get(asset=asset).amount)
        else:
            continue
        break
    assert len(amounts) == 61
    assert amounts[0] == D("1182.80")  # January 2027: 22 / 31 days of 1,666.67
    assert set(amounts[1:60]) == {D("1666.67")}
    assert amounts[60] == D("100000.00") - D("1182.80") - 59 * D("1666.67") == D("483.67")
    assert sum(amounts) == D("100000.00")  # cost - residual exactly (R13)
    asset.refresh_from_db()
    assert asset.status == "fully_depreciated"
    first = DepreciationRun.objects.get(entity=w.alm, period__name="2027-01", status="posted")
    assert t.lines_of(first) == [("1590", D("0"), D("1182.80")), ("5400", D("1182.80"), D("0"))]  # FA2


def test_at3_reducing_balance_never_below_residual(w):
    category = t.category(w.alm, "IT")
    policy = services.draft_policy(category, user=w.a, effective_from=date(2027, 1, 1), method="reducing_balance",
                                   annual_rate="0.4000", basis_note_ar="سياسة اختبار")
    services.submit_policy(policy, user=w.a)
    t.decide(policy, w.c, permission="fixed_assets.approve_policy")
    bill = t.bill(w, w.alm, [("1540", "10000.00"), ("1540", "10000.00")], on=date(2027, 1, 1))
    a = t.capitalise(w, t.line_of(bill, 1), available=date(2027, 1, 1), residual_value=D("0.00"))
    b = t.capitalise(w, t.line_of(bill, 2), available=date(2027, 1, 1), residual_value=D("9500.00"))
    assert (a.method, a.annual_rate) == ("reducing_balance", D("0.4000"))
    jan = t.run(w, w.alm, "2027-01")
    feb = t.run(w, w.alm, "2027-02")
    assert [jan.lines.get(asset=x).amount for x in (a, b)] == [D("333.33"), D("333.33")]
    assert [feb.lines.get(asset=x).amount for x in (a, b)] == [D("322.22"), D("166.67")]  # b stops at its residual
    mar = t.run(w, w.alm, "2027-03")
    assert not mar.lines.filter(asset=b).exists()


# --- AT-4, R15, R18 -----------------------------------------------------------------------------------------------

def test_at4_run_decision_one_run_per_period_and_reversal_through_the_engine(w):
    asset = vehicle(w)
    run = services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    services.submit_depreciation(run, user=w.a)
    with pytest.raises(access.AccessDenied):  # the preparer does not decide
        t.decide(run, w.a, permission="fixed_assets.run_depreciation")
    t.decide(run, w.c, permission="fixed_assets.run_depreciation")
    run.refresh_from_db()
    assert (run.status, run.posted_by, run.total) == ("posted", w.c, D("1182.80"))
    assert t.lines_of(run) == [("1590", D("0"), D("1182.80")), ("5400", D("1182.80"), D("0"))]
    assert run.journal_entry.document_type == "fixed_assets.depreciationrun"
    with pytest.raises(FixedAssetError) as exc:
        services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    assert code_of(exc) == "run_exists"
    grant = access.require(w.a, w.alm, "ledger.reverse_entry", document=run.journal_entry)
    with pytest.raises(access.AccessDenied) as exc:
        ledger.reverse_entry(run.journal_entry, grant)
    assert exc.value.code == "document_reversal_only"
    grant = access.require(w.c, w.alm, "fixed_assets.reverse_document", document=run)
    with pytest.raises(access.AccessDenied) as exc:  # reverse_guard: only fixed_assets.services reverses it
        ledger.reverse_entry(run.journal_entry, grant)
    assert exc.value.code == "reverse_through_owner"
    with pytest.raises(access.AccessDenied) as exc:  # access R61: not outside the engine
        services.reverse_document(run, user=w.c)
    assert exc.value.code == "approval_request_required"
    services.request_reversal(run, user=w.a, reason_ar="خطأ في المسيّر", reason_en="Wrong run")
    with pytest.raises(access.AccessDenied) as exc:
        t.decide(run, w.a, permission="fixed_assets.reverse_document")
    t.decide(run, w.c, permission="fixed_assets.reverse_document")
    run.refresh_from_db()
    assert run.status == "reversed" and run.reversal_entry.reversal_of_id == run.journal_entry_id
    assert run.journal_entry.status == "reversed"
    rerun = t.run(w, w.alm, "2027-01")  # the period is re-runnable while open
    assert rerun.total == D("1182.80") and rerun.number != run.number
    asset.refresh_from_db()
    assert asset.status == "active"


def test_r15_discard_withdraws_the_request_and_the_period_can_be_prepared_again(w):
    vehicle(w)
    run = services.submit_depreciation(services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a),
                                       user=w.a)
    services.discard_depreciation(run, user=w.a)
    run.refresh_from_db()
    assert run.status == "discarded"
    assert ApprovalRequest.objects.get(document_type="fixed_assets.depreciationrun", document_id=run.pk).status == \
        "superseded"
    assert services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a).status == "draft"


def test_r15_a_run_posts_only_in_an_open_period(w):
    t.close(w, w.alm, "2027-01")
    with pytest.raises(FixedAssetError) as exc:
        services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    assert code_of(exc) == "period_closed"


# --- AT-5, R17 ----------------------------------------------------------------------------------------------------

def test_at5_late_activation_gets_a_catch_up_line_in_the_next_open_run(w):
    bill = t.bill(w, w.alm, [("1520", "120000.00")], on=date(2027, 1, 5))
    asset = services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc, vat_use="taxable",
                                            held_for="own_use", residual_value=D("20000.00"))
    t.close(w, w.alm, "2027-01")  # the line is in the register (draft): January closes (R43)
    services.submit_asset(asset, user=w.a, available_for_use_date=date(2027, 1, 10))
    t.decide(asset, w.c, permission="fixed_assets.capitalise")
    feb = t.run(w, w.alm, "2027-02")
    lines = {l.catch_up: l for l in feb.lines.all()}
    assert (lines[True].amount, lines[True].days) == (D("1182.80"), 22)
    assert (lines[False].amount, lines[False].days) == (D("1666.67"), 28)
    assert feb.journal_entry.date == date(2027, 2, 28)
    assert not JournalEntry.objects.filter(entity=w.alm, date__lte=date(2027, 1, 31),
                                           document_type="fixed_assets.depreciationrun").exists()


# --- AT-6, R15 (OQ-4) ---------------------------------------------------------------------------------------------

def test_at6_close_period_waits_for_runs(w):
    vehicle(w, on=date(2027, 1, 1), available=date(2027, 1, 1))
    t.run(w, w.alm, "2027-01")
    t.close(w, w.alm, "2027-01")
    run = services.submit_depreciation(services.prepare_depreciation(w.alm, t.period(w.alm, "2027-02"), user=w.a),
                                       user=w.a)
    with pytest.raises(LedgerError) as exc:
        t.close(w, w.alm, "2027-02")
    assert run.number in str(exc.value) and "مسيّر الاستهلاك" in str(exc.value)
    with pytest.raises(LedgerError) as exc:
        t.close(w, w.alm, "2027-03")
    assert "No posted depreciation run for 2027-03" in str(exc.value) and "لا يوجد مسيّر استهلاك" in str(exc.value)


# --- AT-7, R15 ----------------------------------------------------------------------------------------------------

def test_at7_a_run_changed_after_preparation_is_refused(w):
    vehicle(w)
    run = services.submit_depreciation(services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a),
                                       user=w.a)
    vehicle(w, cost="60000.00", residual="0.00")  # another asset activated after preparation
    with pytest.raises(FixedAssetError) as exc:
        t.decide(run, w.c, permission="fixed_assets.run_depreciation")
    assert code_of(exc) == "run_changed" and "أعد إعداده" in str(exc.value)
    run.refresh_from_db()
    assert run.status == "submitted" and run.journal_entry is None
    assert not JournalEntry.objects.filter(document_type="fixed_assets.depreciationrun").exists()


def test_at7_an_event_posted_after_preparation_changes_the_run(w):
    asset = vehicle(w)
    t.run(w, w.alm, "2027-01")
    run = services.submit_depreciation(services.prepare_depreciation(w.alm, t.period(w.alm, "2027-02"), user=w.a),
                                       user=w.a)
    t.event(w, asset, "impairment", date(2027, 2, 10), recoverable_amount=D("100000.00"))
    with pytest.raises(FixedAssetError) as exc:
        t.decide(run, w.c, permission="fixed_assets.run_depreciation")
    assert code_of(exc) == "run_changed"


# --- AT-8, R14 ----------------------------------------------------------------------------------------------------

def test_at8_no_approved_policy_no_capitalisation_and_no_run(w):
    bill = t.bill(w, w.alm, [("1530", "5000.00")])
    asset = services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc, vat_use="taxable",
                                            held_for="own_use")
    with pytest.raises(FixedAssetError) as exc:
        services.submit_asset(asset, user=w.a, available_for_use_date=date(2027, 1, 15))
    assert code_of(exc) == "no_policy" and "FURN" in str(exc.value) and "سياسة استهلاك" in str(exc.value)
    migration_port.load_opening_asset(  # an asset in FURN that is active without a capitalisation (opening load)
        w.alm, user=w.a, legacy_code="F-1", description_ar="أثاث", asset_class="furniture_office", usage="own_use",
        location="المقر", cost_account=t.category(w.alm, "FURN").cost_account, acquisition_date=date(2025, 1, 1),
        available_for_use_date=date(2025, 1, 1), cost=D("1200.00"), accumulated_depreciation=D("200.00"),
        method="straight_line", useful_life_months=60, remaining_life_months=50, cutover=date(2026, 12, 31))
    with pytest.raises(FixedAssetError) as exc:
        services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    assert code_of(exc) == "no_policy" and "FURN" in str(exc.value)
    policy = services.draft_policy(t.category(w.alm, "FURN"), user=w.a, effective_from=date(2020, 1, 1),
                                   useful_life_months=60, basis_note_ar="سياسة اختبار")
    services.submit_policy(policy, user=w.a)
    with pytest.raises(access.AccessDenied):
        t.decide(policy, w.a, permission="fixed_assets.approve_policy")
    t.decide(policy, w.c, permission="fixed_assets.approve_policy")
    policy.refresh_from_db()
    assert (policy.status, policy.approved_by) == ("approved", w.c)
    services.submit_asset(asset, user=w.a, available_for_use_date=date(2027, 1, 15))  # allowed now
    with pytest.raises(FixedAssetError) as exc:  # the migrated asset counts only once the opening entry is posted
        services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    assert code_of(exc) == "opening_not_posted"


# --- AT-20, R12 ---------------------------------------------------------------------------------------------------

def test_at20_land_and_ip_land_are_never_depreciated(w):
    bill = t.bill(w, w.alm, [("1560", "500000.00"), ("1510", "300000.00"), ("1520", "60000.00")])
    land = t.capitalise(w, t.line_of(bill, 1), available=date(2027, 1, 15))
    ip_land = t.capitalise(w, t.line_of(bill, 2), available=date(2027, 1, 15), category_code="IPL",
                           usage="investment_property", held_for="rental")
    car = t.capitalise(w, t.line_of(bill, 3), available=date(2027, 1, 15))
    assert (land.status, ip_land.status, land.category.depreciable) == ("active", "active", False)
    run = t.run(w, w.alm, "2027-01")
    assert set(run.lines.values_list("asset_id", flat=True)) == {car.pk}


# --- AT-21, R16 ---------------------------------------------------------------------------------------------------

def test_at21_change_of_estimate_is_prospective_from_the_earliest_open_month(w, clock):
    asset = vehicle(w, on=date(2027, 1, 1), available=date(2027, 1, 1), residual="0.00")
    runs = [t.run(w, w.alm, f"2027-{m:02d}") for m in range(1, 12)]
    assert {r.total for r in runs} == {D("2000.00")}
    clock.set(2027, 12, 15, 10, 0)
    entries = JournalEntry.objects.count()
    e = t.event(w, asset, "change_of_estimate", date(2027, 12, 15), new_useful_life_months=48)
    assert (e.status, e.effective_from, e.journal_entry) == ("approved", date(2027, 12, 1), None)
    assert JournalEntry.objects.count() == entries  # no entry
    asset.refresh_from_db()
    assert asset.useful_life_months == 48 and e.previous_values["useful_life_months"] == 60
    dec = t.run(w, w.alm, "2027-12")
    # 120,000 - 11 x 2,000 = 98,000 spread over the 37 months left of 48 (Dec 2027 to Dec 2030)
    assert dec.lines.get(asset=asset).amount == D("2648.65")
    assert {r.total for r in DepreciationRun.objects.filter(pk__in=[r.pk for r in runs])} == {D("2000.00")}
    assert DepreciationLine.objects.filter(run__in=runs).count() == 11  # earlier runs untouched


# --- AT-35, R43 ---------------------------------------------------------------------------------------------------

def test_at35_unregistered_capital_lines_block_period_close(w):
    bill = t.bill(w, w.alm, [("1520", "45000.00")], on=date(2027, 2, 10))
    with pytest.raises(LedgerError) as exc:
        t.close(w, w.alm, "2027-02")
    text = str(exc.value)
    assert f"{bill.number} line 1" in text and "not in the register" in text and "غير مسجلة في سجل الأصول" in text
    services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc)
    assert t.close(w, w.alm, "2027-02").status == "closed"
