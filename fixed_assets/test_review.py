"""Review of ccb9cef (module/fixed-assets): the reviewer's probes (scratchpad fa-review test_probe_a/b/c) as tests, and
one test per finding (items 1 to 11 and the nits). Fictional data; policies are fixture values (OQ-2)."""
from datetime import date
from decimal import Decimal

import pytest
from django.db.models import Sum

from core import access
from core.access.authority_models import ApprovalRequest
from fixed_assets import migration_port, services, testing as t
from fixed_assets.messages import FixedAssetError
from fixed_assets.models import AssetCostComponent, FixedAsset
from invoicing import services as inv
from invoicing.models import PayableAllocation
from ledger.models import Account, JournalLine
from migration import workbook, workbook_checks
from migration import testing as mt

pytestmark = pytest.mark.django_db
D = Decimal


def code_of(exc):
    return getattr(exc.value, "code", None)


def bal(entity, code):
    r = JournalLine.objects.filter(entry__entity=entity, entry__status__in=("posted", "reversed"),
                                   account__code=code).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (r["d"] or D("0")) - (r["c"] or D("0"))


@pytest.fixture
def w1(db):
    return t.world(entities=("ALM",))


def it_asset(w, cost="3600.00", on=date(2027, 1, 1)):
    bill = t.bill(w, w.alm, [("1540", cost)], on=on)
    return bill, t.capitalise(w, t.line_of(bill), available=on, category_code="IT", residual_value=D("0.00"))


def short_category(w, months):
    itx = services.create_category(w.alm, user=w.a, code="ITX", name_ar="أجهزة قصيرة العمر", asset_class="it_equipment",
                                   cost_account="1540", accum_depr_account="1590", depr_expense_account="5400",
                                   disposal_account="4800")
    p = services.draft_policy(itx, user=w.a, effective_from=date(2020, 1, 1), useful_life_months=months,
                              basis_note_ar="سياسة اختبار")
    services.submit_policy(p, user=w.a)
    t.decide(p, w.c, permission="fixed_assets.approve_policy")
    return itx


# --- item 1 (probe a): no event back into a depreciated month; the write-off nets to zero ---------------------------

def test_item1_a_back_dated_write_off_is_refused_and_a_later_one_nets_to_zero(w1):
    w = w1
    _bill, asset = it_asset(w)  # 3,600.00 over 36 months: 100.00 a month
    for name in ("2027-01", "2027-02", "2027-03", "2027-04"):
        t.run(w, w.alm, name)
    with pytest.raises(FixedAssetError) as exc:
        t.event(w, asset, "write_off", date(2027, 2, 20))
    assert code_of(exc) == "event_before_posted_run" and "2027-04-30" in str(exc.value) and "مسيّر مرحّل" in str(exc.value)
    off = t.event(w, asset, "write_off", date(2027, 5, 15))
    assert (off.depreciation_derecognised, off.amount, off.depreciation_until) == (
        D("448.39"), D("3151.61"), date(2027, 5, 15))  # 400 posted + 15 / 31 days of May
    may = t.run(w, w.alm, "2027-05")
    assert [(l.days, l.amount, l.catch_up) for l in may.lines.all()] == [(15, D("48.39"), False)]
    assert (bal(w.alm, "1590"), bal(w.alm, "1540"), bal(w.alm, "4800")) == (D("0"), D("0"), D("3151.61"))
    assert all(r["difference"] == 0 and not r["reconciling"]
               for r in services.register_to_gl(w.alm, date(2027, 5, 31), user=w.a))


# --- item 3 (probe a): impairments and changes of estimate ----------------------------------------------------------

def test_item3_a_back_dated_impairment_is_refused(w1):
    w = w1
    _bill, asset = it_asset(w)
    for name in ("2027-01", "2027-02", "2027-03"):
        t.run(w, w.alm, name)
    with pytest.raises(FixedAssetError) as exc:
        t.event(w, asset, "impairment", date(2027, 1, 10), recoverable_amount=D("2000.00"))
    assert code_of(exc) == "event_before_posted_run"
    imp = t.event(w, asset, "impairment", date(2027, 4, 10), recoverable_amount=D("2000.00"))
    assert imp.amount == D("1300.00")  # 3,600 - 300 posted - 2,000
    with pytest.raises(FixedAssetError) as exc:  # (a VAT use change is exempt from the guard: follow-up P5)
        services.draft_event(asset, user=w.a, kind="impairment_reversal", event_date=date(2027, 3, 31),
                             reason_ar="س", recoverable_amount=D("3000.00"))
    assert code_of(exc) == "event_before_posted_run"


def test_item3_a_change_of_estimate_takes_the_first_open_month_without_a_posted_run(w1, clock):
    w = w1
    _bill, asset = it_asset(w)
    for m in range(1, 12):
        t.run(w, w.alm, f"2027-{m:02d}")
    clock.set(2027, 11, 20, 10, 0)  # November is run already: the change takes December
    e = t.event(w, asset, "change_of_estimate", date(2027, 11, 20), new_useful_life_months=24)
    assert e.effective_from == date(2027, 12, 1)
    t.run(w, w.alm, "2027-12")
    clock.set(2027, 12, 20, 10, 0)  # December is run too and 2028 has no periods: nothing to take effect in
    e2 = t.event(w, asset, "change_of_estimate", date(2027, 12, 20), approve=False, new_useful_life_months=30)
    with pytest.raises(FixedAssetError) as exc:
        t.decide(e2, w.c, permission="fixed_assets.change_estimate")
    assert code_of(exc) == "no_unrun_month"


def test_item6_a_new_life_must_exceed_the_months_elapsed(w1, clock):
    w = w1
    _bill, asset = it_asset(w)
    for m in range(1, 7):
        t.run(w, w.alm, f"2027-{m:02d}")
    clock.set(2027, 7, 10, 10, 0)
    e = t.event(w, asset, "change_of_estimate", date(2027, 7, 10), approve=False, new_useful_life_months=6)
    with pytest.raises(FixedAssetError) as exc:  # 6 months from 2027-01-01 end 2027-07-01, not after July
        t.decide(e, w.c, permission="fixed_assets.change_estimate")
    assert code_of(exc) == "life_too_short" and "الأشهر المنقضية" in str(exc.value)


# --- item 2 (probe c): a line links only to an asset whose own category takes it ------------------------------------

def test_item2_a_line_of_another_category_never_feeds_an_asset(w1):
    w = w1
    bill = t.bill(w, w.alm, [("1520", "50000.00"), ("1540", "3000.00")], on=date(2027, 1, 10))
    car = services.create_asset_from_line(t.line_of(bill, 1), user=w.a, location=w.loc)
    for category in (t.category(w.alm, "IT"), None):
        with pytest.raises(FixedAssetError) as exc:
            services.create_asset_from_line(t.line_of(bill, 2), user=w.a, asset=car, category=category)
        assert code_of(exc) == "line_category"
    car.refresh_from_db()
    assert car.cost == D("50000.00") and car.components.count() == 1


# --- item 4 (probe b): an incomplete legacy estimate is refused, never a TypeError ---------------------------------

def test_item4_opening_rows_need_a_complete_straight_line_estimate(w1):
    w = w1
    vehicles = Account.objects.get(entity=w.alm, code="1520")
    for i, (method, life, remaining) in enumerate((("", None, None), ("straight_line", 60, None),
                                                   ("reducing_balance", None, None))):
        with pytest.raises(FixedAssetError) as exc:
            migration_port.load_opening_asset(
                w.alm, user=w.a, legacy_code=f"L{i}", description_ar="سيارة", asset_class="vehicle", usage="own_use",
                location="HQ", cost_account=vehicles, acquisition_date=date(2024, 1, 1),
                available_for_use_date=date(2024, 1, 1), cost="100000.00", accumulated_depreciation="40000.00",
                residual_value="0", method=method, useful_life_months=life, remaining_life_months=remaining,
                cutover=date(2026, 12, 31))
        assert code_of(exc) == "opening_row" and f"L{i}" in str(exc.value) and "الأصل الافتتاحي" in str(exc.value)
    assert not FixedAsset.objects.exists()
    rows = mt.workbook_rows()
    rows["fixed_assets"].append({**rows["fixed_assets"][0], "asset_code": "FA-0002", "method": "reducing_balance",
                                 "cost": D("1.00"), "accumulated_depreciation": D("0.00")})
    rows["fixed_assets"].append({**rows["fixed_assets"][0], "asset_code": "FA-0003", "remaining_life_months": None,
                                 "cost": D("1.00"), "accumulated_depreciation": D("0.00")})
    report = workbook_checks.check_workbook(workbook.read_workbook(mt.filled_workbook(rows)))
    assert {p.code for p in report.errors} >= {"reducing_balance_rate", "depreciation_estimate"}


# --- item 5 (probe b): a CWIP transfer is booked on its date; the target keeps the CWIP's acquisition date ----------

def test_item5_cwip_register_ties_to_the_ledger_at_every_month_end(w1):
    w = w1
    bill = t.bill(w, w.alm, [("1550", "1200000.00")], on=date(2027, 1, 10))
    cwip = services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc)
    building = services.create_asset(w.alm, user=w.a, category=t.category(w.alm, "BLDG"), location=w.loc,
                                     description_ar="مبنى", vat_use="taxable", held_for="own_use")
    t.event(w, cwip, "capitalise_cwip", date(2027, 3, 15), target_asset=building, amount=D("1200000.00"))
    for on in (date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31)):
        rows = {r["account"]: r for r in services.register_to_gl(w.alm, on, user=w.a)}
        assert rows["1550"]["difference"] == 0 and rows["1570"]["difference"] == 0, on
    building.refresh_from_db()
    assert (building.acquisition_date, building.cost) == (date(2027, 1, 10), D("1200000.00"))
    assert building.components.get().accounting_date == date(2027, 3, 15)
    assert building.retention_until >= cwip.retention_until  # never shortened (R33)


# --- item 6 (probe b): a cost change after the life has ended --------------------------------------------------------

def test_item6_a_credit_note_after_full_depreciation_never_leaves_a_negative_nbv(w1):
    w = w1
    short_category(w, 2)
    bill = t.bill(w, w.alm, [("1540", "1000.00")], on=date(2027, 1, 1))
    asset = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 1), category_code="ITX", residual_value=D("0.00"))
    t.run(w, w.alm, "2027-01")
    t.run(w, w.alm, "2027-02")
    asset.refresh_from_db()
    assert asset.status == "fully_depreciated"
    note = t.bill(w, w.alm, [("1540", "300.00")], on=date(2027, 3, 5), doc_type="vendor_credit_note", original=bill)
    e = services.record_cost_adjustment(asset, user=w.a, purchase_line=t.line_of(note), reason_ar="إشعار")
    services.submit_event(e, user=w.a)
    t.decide(e, w.c, permission="fixed_assets.capitalise")
    april = t.run(w, w.alm, "2027-04")
    assert [(l.amount, l.days) for l in april.lines.all()] == [(D("-300.00"), 0)]  # the excess depreciation back
    values = services._values(asset, date(2027, 4, 30))
    assert (values["cost"], values["accumulated_depreciation"], values["nbv"]) == (D("700.00"), D("700.00"), D("0.00"))
    off = t.event(w, asset, "write_off", date(2027, 5, 10))
    assert (off.cost_derecognised, off.depreciation_derecognised, off.amount) == (D("700.00"), D("700.00"),
                                                                                  D("0.00"))
    assert (bal(w.alm, "1540"), bal(w.alm, "1590")) == (D("0"), D("0"))


def test_item6_a_further_cost_after_the_life_ended_is_depreciated_at_once(w1):
    w = w1
    short_category(w, 2)
    bill = t.bill(w, w.alm, [("1540", "1000.00")], on=date(2027, 1, 1))
    asset = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 1), category_code="ITX", residual_value=D("0.00"))
    t.run(w, w.alm, "2027-01")
    t.run(w, w.alm, "2027-02")
    extra = t.bill(w, w.alm, [("1540", "200.00")], on=date(2027, 3, 5))
    e = services.record_cost_adjustment(asset, user=w.a, purchase_line=t.line_of(extra), reason_ar="تكلفة لاحقة")
    services.submit_event(e, user=w.a)
    t.decide(e, w.c, permission="fixed_assets.capitalise")
    april = t.run(w, w.alm, "2027-04")
    assert april.lines.get().amount == D("200.00")
    assert services._values(asset, date(2027, 4, 30))["nbv"] == D("0.00")


# --- item 7 (probe b): a credit note on the bill of a disposed asset -------------------------------------------------

def test_item7_a_credit_note_after_a_write_off_goes_to_the_disposal_result(w1):
    w = w1
    bill = t.bill(w, w.alm, [("1520", "1000.00")], on=date(2027, 1, 5))
    asset = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 5))
    t.run(w, w.alm, "2027-01")
    t.event(w, asset, "write_off", date(2027, 2, 1))
    t.run(w, w.alm, "2027-02")
    note = t.bill(w, w.alm, [("1520", "100.00")], on=date(2027, 2, 5), doc_type="vendor_credit_note", original=bill)
    loss = bal(w.alm, "4800")
    with pytest.raises(FixedAssetError):  # a disposed asset is not linked directly
        services.create_asset_from_line(t.line_of(note), user=w.a, asset=asset)
    e = services.record_cost_adjustment(asset, user=w.a, purchase_line=t.line_of(note), reason_ar="إشعار بعد الشطب")
    assert (e.kind, e.amount) == ("disposal_adjustment", D("-100.00"))
    services.submit_event(e, user=w.a)
    req = ApprovalRequest.objects.get(document_type="fixed_assets.assetevent", document_id=e.pk)
    assert req.activity.code == "fixed_assets.disposal"
    t.decide(e, w.c, permission="fixed_assets.dispose")
    e.refresh_from_db()
    assert t.lines_of(e) == [("1520", D("100.00"), D("0")), ("4800", D("0"), D("100.00"))]
    assert (bal(w.alm, "1520"), bal(w.alm, "4800")) == (D("0"), loss - D("100.00"))
    assert all(r["difference"] == 0 for r in services.register_to_gl(w.alm, date(2027, 2, 28), user=w.a))
    t.close(w, w.alm, "2027-01")
    assert t.close(w, w.alm, "2027-02").status == "closed"


# --- item 8: reversed source documents of either sign, and voiding an approved adjustment --------------------------

def test_item8_a_reversed_credit_note_is_flagged_and_an_approved_adjustment_can_be_voided(w1):
    w = w1
    bill = t.bill(w, w.alm, [("1520", "60000.00")], on=date(2027, 1, 5))
    asset = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 5), residual_value=D("0.00"))
    note = t.bill(w, w.alm, [("1520", "6000.00")], on=date(2027, 1, 20), doc_type="vendor_credit_note", original=bill)
    e = services.record_cost_adjustment(asset, user=w.a, purchase_line=t.line_of(note), reason_ar="إشعار")
    services.submit_event(e, user=w.a)
    t.decide(e, w.c, permission="fixed_assets.capitalise")
    asset.refresh_from_db()
    assert asset.cost == D("54000.00")
    # The adjustment was wrong: it is voided through the reversal activity (no entry to reverse).
    services.request_reversal(e, user=w.a, reason_ar="تسوية خاطئة")
    t.decide(e, w.c, permission="fixed_assets.reverse_document")
    e.refresh_from_db()
    asset.refresh_from_db()
    assert (e.status, e.reversal_entry, asset.cost) == ("reversed", None, D("60000.00"))
    with pytest.raises(FixedAssetError) as exc:  # the credit note is no longer in the register: the run waits
        services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    assert code_of(exc) == "pending_adjustment"
    e2 = services.record_cost_adjustment(asset, user=w.a, purchase_line=t.line_of(note), reason_ar="إشعار")
    services.submit_event(e2, user=w.a)
    t.decide(e2, w.c, permission="fixed_assets.capitalise")
    # Then the credit note itself is reversed in invoicing: its (negative) component is flagged too.
    for allocation in PayableAllocation.objects.filter(vendor_credit_note=note, amount__gt=0):
        inv.unallocate(allocation, user=w.a)
    inv.reverse_document(note, user=w.a, reason="credit note withdrawn")
    with pytest.raises(FixedAssetError) as exc:
        services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    assert code_of(exc) == "pending_adjustment" and "معكوسة" in str(exc.value)
    component = asset.components.get(source_event=e2, reverses__isnull=True)
    fix = services.record_cost_adjustment(asset, user=w.a, reverses_component=component, event_date=date(2027, 1, 25),
                                          reason_ar="إشعار معكوس")
    assert fix.amount == D("6000.00")
    services.submit_event(fix, user=w.a)
    t.decide(fix, w.c, permission="fixed_assets.capitalise")
    asset.refresh_from_db()
    assert asset.cost == D("60000.00")
    assert t.run(w, w.alm, "2027-01").status == "posted"


# --- item 11: the reversal of a write-off is measured on its entry total ------------------------------------------

def test_item11_a_write_off_reversal_is_routed_on_the_cost_derecognised(w1):
    w = w1
    _bill, asset = it_asset(w)
    t.run(w, w.alm, "2027-01")
    off = t.event(w, asset, "write_off", date(2027, 2, 1))
    services.request_reversal(off, user=w.a, reason_ar="شطب خاطئ")
    req = ApprovalRequest.objects.get(document_type="fixed_assets.assetevent", document_id=off.pk,
                                      activity__code="fixed_assets.reversal")
    assert req.amount == off.cost_derecognised == D("3600.00") and off.amount != req.amount


# --- nits -----------------------------------------------------------------------------------------------------------

def test_nit_plates_match_without_spaces_hyphens_or_case(w1):
    from fixed_assets.test_events import _vehicle_contract

    w = w1
    bill = t.bill(w, w.alm, [("1520", "90000.00")])
    car = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 15), serial_or_plate="ABC 1234")
    _vehicle_contract(w, "abc-1234")
    with pytest.raises(FixedAssetError) as exc:
        services.draft_event(car, user=w.a, kind="write_off", event_date=date(2027, 2, 1), reason_ar="شطب")
    assert code_of(exc) == "vehicle_on_contract"


def test_nit_policy_and_settings_values_are_checked(w1):
    w = w1
    category = t.category(w.alm, "IT")
    for kwargs in ({"annual_rate": 0.4, "method": "reducing_balance"}, {"residual_pct": 2.5},
                   {"residual_pct": "2.5555"}, {"annual_rate": "0.40001", "method": "reducing_balance"}):
        with pytest.raises(FixedAssetError) as exc:
            services.draft_policy(category, user=w.a, effective_from=date(2027, 1, 1), useful_life_months=36,
                                  basis_note_ar="س", **kwargs)
        assert code_of(exc) == "amount_decimals", kwargs
    p = services.draft_policy(category, user=w.a, effective_from=date(2027, 1, 1), method="reducing_balance",
                              annual_rate="0.4", residual_pct="2.5", basis_note_ar="س")
    assert (p.annual_rate, p.residual_pct) == (D("0.4000"), D("2.500"))
    for kwargs in ({"art52_adjustment_months_movable": 0}, {"art52_adjustment_months_movable": "72"},
                   {"normal_credit_days": -1}, {"normal_credit_days": 1.5}, {"reporting_dates": ["2027-06-15"]},
                   {"reporting_dates": ["30/06/2027"]}):
        with pytest.raises(FixedAssetError) as exc:
            services.update_settings(w.alm, user=w.a, **kwargs)
        assert code_of(exc) == "settings_value", kwargs
    assert services.update_settings(w.alm, user=w.a, normal_credit_days=None).normal_credit_days is None


def test_nit_a_chart_without_2400_gets_a_bilingual_refusal(w1):
    w = w1
    Account.objects.filter(entity=w.alm, code="2400").delete()
    machine = services.create_asset(w.alm, user=w.a, category=t.category(w.alm, "MACH"), location=w.loc,
                                    description_ar="آلة", vat_use="taxable", held_for="own_use")
    e = services.draft_event(machine, user=w.a, kind="financed_acquisition", event_date=date(2027, 2, 1),
                             reason_ar="مرابحة", cash_cost_price=D("1000.00"), financing_total=D("1100.00"),
                             financing_ref="MUR-1")
    with pytest.raises(FixedAssetError) as exc:
        services.submit_event(e, user=w.a)
    assert code_of(exc) == "account_not_set" and "2400" in str(exc.value) and "التمويل الإسلامي" in str(exc.value)


def test_nit_admin_hides_the_origin_of_a_transferred_asset():
    from django.contrib import admin

    from fixed_assets.models import FixedAsset as Model

    model_admin = admin.site._registry[Model]
    assert "transferred_from" in model_admin.exclude
    assert "transferred_from" not in model_admin.get_readonly_fields(None)


def test_item9_the_line_is_locked_before_its_remaining_amount_is_read(w1, monkeypatch):
    """The R4 race itself is in test_pg.py; here: every linker reads the line through the locking port call."""
    w = w1
    from invoicing import fixed_assets_port as port

    calls = []
    real = port.line_of
    monkeypatch.setattr(port, "line_of", lambda *a, **k: calls.append(k.get("lock")) or real(*a, **k))
    bill = t.bill(w, w.alm, [("1520", "1000.00"), ("1520", "500.00")])
    asset = t.capitalise(w, t.line_of(bill, 1), available=date(2027, 1, 15))
    e = services.record_cost_adjustment(asset, user=w.a, purchase_line=t.line_of(bill, 2), reason_ar="تكلفة")
    assert calls and all(calls) and e.amount == D("500.00")
    assert AssetCostComponent.objects.filter(asset=asset).count() == 1


# --- follow-up review of acb5701 (probes_head test_probe_d, P1 to P5) ----------------------------------------------

def _gaps(w, on):
    return {r["account"]: r["difference"] for r in services.register_to_gl(w.alm, on, user=w.a) if r["difference"]}


def _adjust(w, asset, line, **kw):
    e = services.record_cost_adjustment(asset, user=w.a, purchase_line=line, reason_ar="تسوية", **kw)
    services.submit_event(e, user=w.a)
    t.decide(e, w.c, permission=services.KIND_PERMISSION[e.kind])
    e.refresh_from_db()
    return e


def _void(w, e):
    services.request_reversal(e, user=w.a, reason_ar="إلغاء")
    t.decide(e, w.c, permission="fixed_assets.reverse_document")
    e.refresh_from_db()
    return e


def _vehicle(w, cost="60000.00", on=date(2027, 1, 5)):
    bill = t.bill(w, w.alm, [("1520", cost)], on=on)
    return bill, t.capitalise(w, t.line_of(bill), available=on, residual_value=D("0.00"))


def test_p1_a_voided_bill_line_is_unregistered_again_and_blocks_the_close(w1):
    from ledger.services import LedgerError

    w = w1
    _bill, asset = _vehicle(w)
    extra = t.bill(w, w.alm, [("1520", "6000.00")], on=date(2027, 2, 10))
    _void(w, _adjust(w, asset, t.line_of(extra)))
    t.run(w, w.alm, "2027-01")
    t.run(w, w.alm, "2027-02")
    t.close(w, w.alm, "2027-01")
    assert _gaps(w, date(2027, 2, 28)) == {"1520": D("6000.00")}  # the line is out of the register again
    with pytest.raises(LedgerError) as exc:  # R43: net of the void it is linked to nothing
        t.close(w, w.alm, "2027-02")
    assert f"{extra.number} line 1" in str(exc.value) and "غير مسجلة في سجل الأصول" in str(exc.value)
    _adjust(w, asset, t.line_of(extra))  # linked again: booked on its date, depreciated from March
    assert _gaps(w, date(2027, 2, 28)) == {}
    assert t.close(w, w.alm, "2027-02").status == "closed"


def test_p2_a_cost_adjustment_is_not_voided_after_a_disposal_or_a_later_event(w1):
    w = w1
    bill, asset = _vehicle(w)
    note = t.bill(w, w.alm, [("1520", "6000.00")], on=date(2027, 1, 20), doc_type="vendor_credit_note", original=bill)
    e = _adjust(w, asset, t.line_of(note))
    bill2, other = _vehicle(w, "30000.00")
    note2 = t.bill(w, w.alm, [("1520", "3000.00")], on=date(2027, 1, 20), doc_type="vendor_credit_note",
                   original=bill2)
    e2 = _adjust(w, other, t.line_of(note2))
    t.run(w, w.alm, "2027-01")
    t.event(w, asset, "write_off", date(2027, 2, 10))
    t.event(w, other, "impairment", date(2027, 2, 10), recoverable_amount=D("20000.00"))
    t.run(w, w.alm, "2027-02")
    for event in (e, e2):  # a disposed asset; a later posted impairment
        services.request_reversal(event, user=w.a, reason_ar="إلغاء")
        with pytest.raises(FixedAssetError) as exc:
            t.decide(event, w.c, permission="fixed_assets.reverse_document")
        assert code_of(exc) == "reversal_order"
        event.refresh_from_db()
        assert event.status == "approved"
    assert _gaps(w, date(2027, 2, 28)) == {}


def test_p3_a_write_off_is_reversed_only_after_its_disposal_adjustments(w1):
    w = w1
    bill, asset = _vehicle(w, "1000.00")
    t.run(w, w.alm, "2027-01")
    off = t.event(w, asset, "write_off", date(2027, 2, 1))
    t.run(w, w.alm, "2027-02")
    credit = _adjust(w, asset, t.line_of(t.bill(w, w.alm, [("1520", "100.00")], on=date(2027, 2, 5),
                                                doc_type="vendor_credit_note", original=bill)))
    debit = _adjust(w, asset, t.line_of(t.bill(w, w.alm, [("1520", "50.00")], on=date(2027, 2, 6))))
    assert (credit.kind, debit.kind) == ("disposal_adjustment", "disposal_adjustment")
    services.request_reversal(off, user=w.a, reason_ar="شطب خاطئ")
    with pytest.raises(FixedAssetError) as exc:
        t.decide(off, w.c, permission="fixed_assets.reverse_document")
    assert code_of(exc) == "reversal_order"
    asset.refresh_from_db()
    assert (asset.status, asset.cost) == ("disposed", D("950.00"))  # 1,000.00 - 100.00 + 50.00
    _void(w, credit)
    _void(w, debit)
    assert (credit.status, debit.status) == ("reversed", "reversed")
    t.decide(off, w.c, permission="fixed_assets.reverse_document")  # the request stayed open
    off.refresh_from_db()
    asset.refresh_from_db()
    assert (off.status, asset.status, asset.cost) == ("reversed", "active", D("1000.00"))


def test_p4_a_credit_note_entered_after_its_month_was_run_keeps_its_document_date(w1):
    w = w1
    bill, asset = _vehicle(w)
    jan = t.run(w, w.alm, "2027-01")
    feb = t.run(w, w.alm, "2027-02")
    note = t.bill(w, w.alm, [("1520", "6000.00")], on=date(2027, 1, 20), doc_type="vendor_credit_note", original=bill)
    e = _adjust(w, asset, t.line_of(note))  # the default date (the document's) is accepted
    assert e.event_date == date(2027, 1, 20)
    component = asset.components.get(source_event=e)
    assert (component.accounting_date, component.effective_from) == (date(2027, 1, 20), date(2027, 3, 1))
    for on in (date(2027, 1, 31), date(2027, 2, 28)):
        assert _gaps(w, on) == {}, on
    assert t.close(w, w.alm, "2027-01").status == "closed"
    march = t.run(w, w.alm, "2027-03")
    assert march.total < feb.total  # depreciated on 54,000.00 from March, never in a month already run
    assert jan.total < feb.total  # (January from the 5th)


def test_p4_the_adjustment_for_a_reversed_bill_is_never_voided(w1):
    w = w1
    bill, asset = _vehicle(w)
    note = t.bill(w, w.alm, [("1520", "6000.00")], on=date(2027, 1, 20), doc_type="vendor_credit_note", original=bill)
    e = _adjust(w, asset, t.line_of(note))
    for allocation in PayableAllocation.objects.filter(vendor_credit_note=note, amount__gt=0):
        inv.unallocate(allocation, user=w.a)
    inv.reverse_document(note, user=w.a, reason="credit note withdrawn")
    fix = services.record_cost_adjustment(asset, user=w.a, reverses_component=asset.components.get(source_event=e),
                                          event_date=date(2027, 1, 25), reason_ar="إشعار معكوس")
    services.submit_event(fix, user=w.a)
    t.decide(fix, w.c, permission="fixed_assets.capitalise")
    with pytest.raises(FixedAssetError) as exc:
        services.request_reversal(fix, user=w.a, reason_ar="إلغاء")
    assert code_of(exc) == "void_reversed_bill" and note.number in str(exc.value)
    fix.refresh_from_db()
    asset.refresh_from_db()
    assert (fix.status, asset.cost) == ("approved", D("60000.00"))


def test_p5_a_vat_use_change_may_be_dated_in_a_month_already_run(w1):
    w = w1
    _bill, asset = _vehicle(w)
    t.run(w, w.alm, "2027-01")
    e = t.event(w, asset, "vat_use_change", date(2027, 1, 20), new_vat_use="exempt")
    assert e.status == "approved"
    assert services.vat_use_on(asset, date(2027, 1, 25), user=w.a) == "exempt"
