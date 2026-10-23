"""Asset events (fixed-assets.md R7, R19 to R21, R23, R25 to R27, R44, R45, R47, R48; AT-9, AT-12, AT-13, AT-15, AT-17,
AT-36, AT-37, AT-45, AT-46). Fictional data; policies are fixture values (OQ-2)."""
from datetime import date
from decimal import Decimal

import pytest

from core import access
from fixed_assets import services, testing as t
from fixed_assets.messages import FixedAssetError
from fixed_assets.models import AssetCostComponent, DepreciationRun
from invoicing import services as inv
from ledger import services as ledger
from ledger.models import JournalEntry

pytestmark = pytest.mark.django_db
D = Decimal


def code_of(exc):
    return getattr(exc.value, "code", None)


def policy(w, entity, category, life, method="straight_line", rate=None):
    preparer, approver = t.staff(w, entity)
    p = services.draft_policy(category, user=preparer, effective_from=date(2020, 1, 1), method=method,
                              useful_life_months=life, annual_rate=rate, basis_note_ar="سياسة اختبار")
    services.submit_policy(p, user=preparer)
    t.decide(p, approver, permission="fixed_assets.approve_policy")
    return p


# --- AT-9, R23, R25, R26 ------------------------------------------------------------------------------------------

@pytest.fixture
def w2(db):
    return t.world(entities=("ALM",), years=(2026, 2027))


def test_at9_write_offs_in_a_depreciated_month_and_in_an_open_month(w2):
    w = w2
    itx = services.create_category(w.alm, user=w.a, code="ITX", name_ar="أجهزة قصيرة العمر", asset_class="it_equipment",
                                   cost_account="1540", accum_depr_account="1590", depr_expense_account="5400",
                                   disposal_account="4800")
    policy(w, w.alm, itx, 7)
    first_bill = t.bill(w, w.alm, [("1540", "8000.00")], on=date(2026, 9, 1))
    first = t.capitalise(w, t.line_of(first_bill), available=date(2026, 9, 1), category_code="ITX",
                         residual_value=D("1000.00"))  # 1,000.00 a month to the residual, fully depreciated by March
    second_bill = t.bill(w, w.alm, [("1540", "3600.00")], on=date(2027, 1, 1))
    second = t.capitalise(w, t.line_of(second_bill), available=date(2027, 1, 1), category_code="IT",
                          residual_value=D("0.00"))
    for name in ("2026-09", "2026-10", "2026-11", "2026-12", "2027-01", "2027-02", "2027-03"):
        t.run(w, w.alm, name)
    first.refresh_from_db()
    assert services._values(first, date(2027, 3, 31))["accumulated_depreciation"] == D("7000.00")
    with pytest.raises(FixedAssetError) as exc:  # review item 1: never into a month a posted run depreciated
        t.event(w, first, "write_off", date(2027, 3, 20))
    assert code_of(exc) == "event_before_posted_run" and "2027-04-01" in str(exc.value)
    off = t.event(w, first, "write_off", date(2027, 4, 10))
    assert off.status == "posted" and off.journal_entry.document_type == "fixed_assets.assetevent"
    assert t.lines_of(off) == [("1540", D("0"), D("8000.00")), ("1590", D("7000.00"), D("0")),
                               ("4800", D("1000.00"), D("0"))]  # FA4: nothing left to depreciate, loss on 4800 (R26)
    first.refresh_from_db()
    assert (first.status, first.disposal_date, off.depreciation_until) == ("disposed", date(2027, 4, 10),
                                                                           date(2027, 4, 10))
    with pytest.raises(FixedAssetError):
        services.update_asset(first, user=w.a, serial_or_plate="X")  # disposed: read-only
    off2 = t.event(w, second, "write_off", date(2027, 4, 15))
    assert (off2.depreciation_derecognised, off2.amount) == (D("350.00"), D("3250.00"))  # 300 posted + 15 days
    april = t.run(w, w.alm, "2027-04")
    assert [(l.asset_id, l.days, l.amount) for l in april.lines.all()] == [(second.pk, 15, D("50.00"))]
    rows = {r["account"]: r for r in services.register_to_gl(w.alm, date(2027, 4, 30), user=w.a)}
    assert rows["1590"]["difference"] == 0 and rows["1540"]["difference"] == 0


def test_r23_the_disposal_grant_is_the_kinds_own_permission(w):
    bill = t.bill(w, w.alm, [("1520", "1000.00")])
    asset = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 15))
    e = t.event(w, asset, "write_off", date(2027, 1, 20), approve=False)
    with pytest.raises(access.AccessDenied):  # another kind's permission is not this event's approval
        t.decide(e, w.c, permission="fixed_assets.impair")
    t.decide(e, w.c, permission="fixed_assets.dispose")
    e.refresh_from_db()
    assert e.status == "posted"


# --- AT-12, R19 ---------------------------------------------------------------------------------------------------

def test_at12_impairment_recomputes_depreciation_and_its_reversal_is_capped(w):
    bill = t.bill(w, w.alm, [("1510", "900000.00")], on=date(2027, 1, 1))
    ip = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 1), category_code="IPB",
                      usage="investment_property", held_for="rental", residual_value=D("0.00"))
    imp = t.event(w, ip, "impairment", date(2027, 1, 15), recoverable_amount=D("850000.00"))
    assert (imp.status, imp.amount) == ("posted", D("50000.00"))
    assert t.lines_of(imp) == [("1596", D("0"), D("50000.00")), ("5420", D("50000.00"), D("0"))]  # FA6
    jan = t.run(w, w.alm, "2027-01")
    feb = t.run(w, w.alm, "2027-02")
    assert jan.total == D("1875.00")  # 900,000 / 480 (old basis in the impairment month)
    assert feb.total == ((D("900000.00") - D("1875.00") - D("50000.00")) / 479).quantize(D("0.01"))  # new basis
    carrying = services._values(ip, date(2027, 2, 28))["nbv"]
    rev = t.event(w, ip, "impairment_reversal", date(2027, 3, 1), recoverable_amount=carrying + D("80000.00"))
    assert rev.amount == D("896250.00") - carrying  # capped at 900,000 - 2 x 1,875 (IAS 36.117)
    assert rev.amount < D("50000.00")
    assert t.lines_of(rev) == [("1596", rev.amount, D("0")), ("5420", D("0"), rev.amount)]
    assert services._values(ip, date(2027, 3, 1))["nbv"] == D("896250.00")


# --- AT-13, R20, R21 ----------------------------------------------------------------------------------------------

def test_at13_fair_value_changes_and_revaluations_are_refused(w):
    for entity in (w.alm, w.sub):
        bill = t.bill(w, entity, [("1510", "500000.00"), ("1520", "50000.00")])
        ip = t.capitalise(w, t.line_of(bill, 1), available=date(2027, 1, 15), category_code="IPB",
                          usage="investment_property", held_for="rental")
        ppe = t.capitalise(w, t.line_of(bill, 2), available=date(2027, 1, 15))
        preparer, _ = t.staff(w, entity)
        with pytest.raises(FixedAssetError) as exc:
            services.draft_event(ip, user=preparer, kind="fair_value_change", event_date=date(2027, 6, 30),
                                 reason_ar="تقييم")
        assert code_of(exc) == "kind_refused" and "OQ-6" in str(exc.value) and "6" in str(exc.value).split(" / ")[1]
        with pytest.raises(FixedAssetError) as exc:
            services.draft_event(ppe, user=preparer, kind="revaluation", event_date=date(2027, 6, 30),
                                 reason_ar="إعادة تقييم")
        assert code_of(exc) == "kind_refused" and "OQ-8" in str(exc.value)


def test_r22a_slice_2_sales_are_refused_and_real_estate_cites_oq10(w):
    bill = t.bill(w, w.alm, [("1510", "500000.00"), ("1520", "50000.00")])
    ip = t.capitalise(w, t.line_of(bill, 1), available=date(2027, 1, 15), category_code="IPB",
                      usage="investment_property", held_for="rental")
    car = t.capitalise(w, t.line_of(bill, 2), available=date(2027, 1, 15))
    for kind in ("disposal_sale", "intercompany_sale"):
        with pytest.raises(FixedAssetError) as exc:
            services.draft_event(ip, user=w.a, kind=kind, event_date=date(2027, 6, 30), reason_ar="بيع")
        assert code_of(exc) == "real_estate_disposal_blocked" and "OQ-10" in str(exc.value)
        with pytest.raises(FixedAssetError) as exc:
            services.draft_event(car, user=w.a, kind=kind, event_date=date(2027, 6, 30), reason_ar="بيع")
        assert code_of(exc) == "kind_slice_2"


# --- AT-15, R27 ---------------------------------------------------------------------------------------------------

def _vehicle_contract(w, ref, status="active"):
    from billing.models import Contract

    customer = inv.create_customer(w.alm, user=w.a, code=f"C-{ref}", name_en="Test Renter", name_ar="مستأجر تجريبي",
                                   is_vat_registered=True, vat_number="310000000000003", street_en="Street",
                                   street_ar="شارع", building_no="2345", district_en="District", district_ar="حي",
                                   city_en="Jeddah", city_ar="جدة", postal_code="23456", country="SA")
    c = Contract(entity=w.alm, number=f"CT-{ref}", contract_type="vehicle_rental", customer=customer, currency_id="SAR",
                 title_ar="تأجير سيارة", description_ar="عقد تأجير سنوي", start_date=date(2027, 1, 1),
                 end_date=date(2027, 12, 31), billing_frequency="monthly", billing_timing="advance",
                 vehicle_ref=ref, status=status, created_by=w.a)
    c._service_write = True  # fixture shortcut: billing's own approval flow is tested in billing
    c.save()
    return c


def test_at15_a_vehicle_on_an_active_rental_contract_is_not_written_off(w):
    bill = t.bill(w, w.alm, [("1520", "90000.00")])
    car = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 15), category_code="VEHR", usage="rental_operating_lease",
                       held_for="rental")
    contract = _vehicle_contract(w, car.code)
    with pytest.raises(FixedAssetError) as exc:
        t.event(w, car, "write_off", date(2027, 3, 1))
    assert code_of(exc) == "vehicle_on_contract" and contract.number in str(exc.value)
    assert "العقد الساري" in str(exc.value)
    contract.status = "ended"
    contract._service_write = True
    contract.save()
    assert t.event(w, car, "write_off", date(2027, 3, 1)).status == "posted"


def test_r27_the_plate_also_matches(w):
    bill = t.bill(w, w.alm, [("1520", "90000.00")])
    car = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 15), serial_or_plate="ABC 1234")
    _vehicle_contract(w, "ABC 1234")
    e = services.draft_event(car, user=w.a, kind="impairment", event_date=date(2027, 2, 1), reason_ar="ضرر",
                             recoverable_amount=D("80000.00"))  # other events are not blocked
    with pytest.raises(FixedAssetError) as exc:
        services.draft_event(car, user=w.a, kind="write_off", event_date=date(2027, 2, 1), reason_ar="شطب")
    assert code_of(exc) == "vehicle_on_contract"
    assert e.status == "draft"


# --- AT-17, R7 ----------------------------------------------------------------------------------------------------

def _journal_line(w, entity, account, posted=True):
    preparer, approver = t.staff(w, entity)
    entry = ledger.create_entry(entity, date(2027, 2, 1), "financing cost", [
        {"account": account, "debit": D("5000.00")}, {"account": "1100", "credit": D("5000.00")}], user=preparer)
    if posted:
        grant = access.require(approver, entity, "ledger.post_entry", document=entry)
        entry = ledger.post_entry(entry, grant)
    return entry.lines.get(account__code=account)


def test_at17_murabaha_principal_at_the_cash_cost_price_never_the_financing_cost(w):
    for entity, code in ((w.alm, "financing_cost_capitalised"), (w.nsh, "financing_cost_capitalised"),
                         (w.ful, "not_built_oq9")):
        preparer, approver = t.staff(w, entity)
        loc = getattr(w, f"loc_{entity.code.lower()}")
        machine = services.create_asset(entity, user=preparer, category=t.category(entity, "MACH"), location=loc,
                                        description_ar="آلة ممولة بالمرابحة", vat_use="taxable", held_for="own_use")
        with pytest.raises(FixedAssetError) as exc:  # the murabaha sale price includes the deferred profit
            services.draft_event(machine, user=preparer, kind="financed_acquisition", event_date=date(2027, 2, 1),
                                 reason_ar="مرابحة", amount=D("330000.00"), cash_cost_price=D("300000.00"),
                                 financing_total=D("330000.00"), financing_ref="MUR-001")
        assert code_of(exc) == code, entity.code
        e = t.event(w, machine, "financed_acquisition", date(2027, 2, 1), cash_cost_price=D("300000.00"),
                    financing_total=D("330000.00"), financing_ref="MUR-001")
        assert e.status == "posted" and e.amount == D("300000.00")
        assert t.lines_of(e) == [("1500", D("300000.00"), D("0")), ("2400", D("0"), D("300000.00"))]  # FA8
        machine.refresh_from_db()
        assert machine.cost == D("300000.00")
        services.submit_asset(machine, user=preparer, available_for_use_date=date(2027, 2, 1))
        t.decide(machine, approver, permission="fixed_assets.capitalise")
        machine.refresh_from_db()
        lines = [_journal_line(w, entity, "5600")]
        lines.append(_journal_line(w, entity, "5610", posted=entity.code != "ALM"))  # 5610 never posts in ALM
        for line in lines:
            with pytest.raises(FixedAssetError) as exc:
                services.draft_event(machine, user=preparer, kind="subsequent_cost", event_date=date(2027, 2, 2),
                                     reason_ar="تكلفة تمويل", source_journal_line=line)
            assert code_of(exc) == code, (entity.code, line.account.code)
            assert "Sharia" in str(exc.value) if code == "financing_cost_capitalised" else "OQ-9" in str(exc.value)


# --- AT-36, R44 ---------------------------------------------------------------------------------------------------

def test_at36_cwip_accumulates_is_never_depreciated_and_capitalises_to_a_building(w):
    bill = t.bill(w, w.alm, [("1550", "1200000.00"), ("1550", "800000.00")], on=date(2027, 1, 10))
    cwip = services.create_asset_from_line(t.line_of(bill, 1), user=w.a, location=w.loc)
    cwip = services.create_asset_from_line(t.line_of(bill, 2), user=w.a, asset=cwip)
    assert (cwip.category.code, cwip.cost) == ("CWIP", D("2000000.00"))
    with pytest.raises(FixedAssetError) as exc:
        services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    assert code_of(exc) == "nothing_to_depreciate"
    building = services.create_asset(w.alm, user=w.a, category=t.category(w.alm, "BLDG"), location=w.loc,
                                     description_ar="مبنى المقر", vat_use="taxable", held_for="own_use")
    with pytest.raises(FixedAssetError) as exc:
        services.draft_event(cwip, user=w.a, kind="capitalise_cwip", event_date=date(2027, 2, 1), reason_ar="اكتمال",
                             amount=D("2000000.01"), target_asset=building)
    assert code_of(exc) == "cwip_amount"
    e = t.event(w, cwip, "capitalise_cwip", date(2027, 2, 1), amount=D("2000000.00"), target_asset=building)
    assert t.lines_of(e) == [("1550", D("0"), D("2000000.00")), ("1570", D("2000000.00"), D("0"))]  # FA5
    from core.access.models import AccessLog

    granted = AccessLog.objects.filter(event="permission_granted", permission="fixed_assets.capitalise",
                                       actor=w.c).order_by("-pk").first()
    assert granted.details["document"] == ["fixed_assets.assetevent", e.pk]
    cwip.refresh_from_db()
    building.refresh_from_db()
    assert (cwip.cost, building.cost, building.status) == (D("0.00"), D("2000000.00"), "draft")
    assert building.acquisition_date == date(2027, 1, 10)
    with pytest.raises(FixedAssetError) as exc:  # the building waits for its own capitalisation decision
        services.prepare_depreciation(w.alm, t.period(w.alm, "2027-02"), user=w.a)
    assert code_of(exc) == "nothing_to_depreciate"
    services.submit_asset(building, user=w.a, available_for_use_date=date(2027, 2, 1))
    t.decide(building, w.c, permission="fixed_assets.capitalise")
    run = t.run(w, w.alm, "2027-02")
    assert run.lines.get().asset_id == building.pk


# --- AT-37, R45 ---------------------------------------------------------------------------------------------------

def test_at37_an_event_that_needs_an_empty_account_is_refused(w):
    plain = services.create_category(w.alm, user=w.a, code="VNI", name_ar="سيارات بلا حساب هبوط", asset_class="vehicle",
                                     cost_account="1520", accum_depr_account="1590", depr_expense_account="5400")
    policy(w, w.alm, plain, 60)
    bill = t.bill(w, w.alm, [("1520", "70000.00")])
    car = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 15), category_code="VNI")
    e = services.draft_event(car, user=w.a, kind="impairment", event_date=date(2027, 2, 1), reason_ar="ضرر",
                             recoverable_amount=D("50000.00"))
    with pytest.raises(FixedAssetError) as exc:
        services.submit_event(e, user=w.a)
    assert code_of(exc) == "account_not_set"
    assert "VNI" in str(exc.value) and "impairment expense" in str(exc.value) and "خسائر هبوط القيمة" in str(exc.value)


# --- AT-45, R47 ---------------------------------------------------------------------------------------------------

def test_at45_vat_use_changes_only_through_an_event_with_history(w):
    bill = t.bill(w, w.alm, [("1510", "700000.00")])
    ip = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 15), category_code="IPB",
                      usage="investment_property", held_for="rental", vat_use="taxable")
    with pytest.raises(FixedAssetError) as exc:
        services.update_asset(ip, user=w.a, vat_use="exempt")
    assert code_of(exc) == "vat_use_by_event"
    entries = JournalEntry.objects.count()
    e = t.event(w, ip, "vat_use_change", date(2027, 6, 1), new_vat_use="exempt")
    assert (e.status, e.old_vat_use, e.journal_entry) == ("approved", "taxable", None)
    assert JournalEntry.objects.count() == entries
    ip.refresh_from_db()
    assert ip.vat_use == "exempt"
    assert services.vat_use_on(ip, date(2027, 5, 31), user=w.a) == "taxable"
    assert services.vat_use_on(ip, date(2027, 6, 1), user=w.a) == "exempt"
    history = services.asset_detail(ip, user=w.a)["vat_use_history"]
    assert history == [{"from": date(2027, 6, 1), "old": "taxable", "new": "exempt", "event": e.pk}]


# --- AT-46, R48 ---------------------------------------------------------------------------------------------------

def test_at46_investment_property_fair_values_and_the_disclosure_gaps(w):
    bill = t.bill(w, w.alm, [("1510", "700000.00"), ("1510", "400000.00"), ("1520", "50000.00")])
    x = t.capitalise(w, t.line_of(bill, 1), available=date(2027, 1, 15), category_code="IPB",
                     usage="investment_property", held_for="rental")
    y = t.capitalise(w, t.line_of(bill, 2), available=date(2027, 1, 15), category_code="IPB",
                     usage="investment_property", held_for="rental")
    car = t.capitalise(w, t.line_of(bill, 3), available=date(2027, 1, 15))
    entries = JournalEntry.objects.count()
    fv = services.record_ip_fair_value(x, user=w.a, valuation_date=date(2027, 12, 31), amount=D("820000.00"),
                                       valuer_name_ar="مقيم معتمد تجريبي", valuer_name_en="Test valuer",
                                       valuer_licence_ref="TQ-0000", evidence_ref="VAL-2027-01")
    assert fv.amount == D("820000.00") and JournalEntry.objects.count() == entries
    with pytest.raises(FixedAssetError) as exc:
        services.record_ip_fair_value(car, user=w.a, valuation_date=date(2027, 12, 31), amount=D("40000.00"),
                                      valuer_name_ar="مقيم", evidence_ref="EV")
    assert code_of(exc) == "not_ip"
    report = services.ip_disclosure(w.alm, date(2027, 12, 31), user=w.a)
    rows = {r["asset"]: r for r in report["rows"]}
    assert rows[x.code]["fair_value"] == D("820000.00") and rows[x.code]["valuer"] == "Test valuer"
    assert rows[y.code]["fair_value"] is None and rows[y.code]["fair_value_text"] == "none / لا يوجد"
    assert len(report["gaps"]) == 1 and y.code in report["gaps"][0] and "لا قيمة عادلة" in report["gaps"][0]
    assert services.ip_disclosure(w.alm, date(2027, 6, 30), user=w.a)["gaps"] == []  # not a year end


def test_r42_components_are_never_deleted(w):
    bill = t.bill(w, w.alm, [("1520", "1000.00")])
    asset = services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc)
    with pytest.raises(FixedAssetError):
        asset.components.get().delete()
    with pytest.raises(FixedAssetError):
        AssetCostComponent.objects.all().delete()
    with pytest.raises(FixedAssetError):
        DepreciationRun.objects.all().delete()
