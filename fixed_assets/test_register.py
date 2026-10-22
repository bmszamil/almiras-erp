"""The register and capitalisation (fixed-assets.md R1 to R8, R39, R46, R49; AT-1, AT-14, AT-18, AT-22, AT-26, AT-31,
AT-39, AT-44, AT-47). Fictional data; policies are fixture values (OQ-2)."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.db import IntegrityError, transaction

from core import access
from core.access.authority_models import ApprovalRequest
from core.access.testing import make_user, member
from fixed_assets import services, testing as t
from fixed_assets.messages import FixedAssetError
from fixed_assets.models import AssetCategory, AssetCostComponent, FixedAsset
from invoicing import services as inv
from ledger import services as ledger
from ledger.models import JournalEntry

pytestmark = pytest.mark.django_db
D = Decimal


def code_of(exc):
    return getattr(exc.value, "code", None)


# --- AT-1, R2, R3, R4 ---------------------------------------------------------------------------------------------

def test_at1_capitalise_from_a_posted_bill_line_and_the_credit_note_adjustment(w):
    bill = t.bill(w, w.alm, [("1520", "120000.00")], on=date(2027, 1, 5))
    listed = services.capitalisable_lines(w.alm, user=w.a)
    assert [(l.pk, r) for l, _b, r in listed] == [(t.line_of(bill).pk, D("120000.00"))]
    entries = JournalEntry.objects.count()
    asset = services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc, vat_use="taxable",
                                            held_for="own_use", residual_value=D("20000.00"))
    assert (asset.status, asset.cost, asset.category.code) == ("draft", D("120000.00"), "VEH")
    assert asset.components.get().amount == D("120000.00")
    assert services.capitalisable_lines(w.alm, user=w.a) == []  # fully linked now
    services.submit_asset(asset, user=w.a, available_for_use_date=date(2027, 1, 10))
    with pytest.raises(access.AccessDenied):  # the preparer does not decide (accountant: no capitalisation right)
        t.decide(asset, w.a, permission="fixed_assets.capitalise")
    t.decide(asset, w.c, permission="fixed_assets.capitalise")
    asset.refresh_from_db()
    assert (asset.status, asset.method, asset.useful_life_months, asset.approved_by) == ("active", "straight_line", 60,
                                                                                       w.c)
    assert asset.policy.category.code == "VEH" and asset.acquisition_date == date(2027, 1, 5)
    assert JournalEntry.objects.count() == entries  # activation posts nothing (FA1)
    # A vendor credit note on the bill reduces the line by 10,000.00: the next run waits for a cost adjustment (R4).
    t.bill(w, w.alm, [("1520", "10000.00")], on=date(2027, 1, 20), doc_type="vendor_credit_note", original=bill)
    with pytest.raises(FixedAssetError) as exc:
        services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    assert code_of(exc) == "pending_adjustment" and "معكوسة" not in str(exc.value)
    note_line = [l for l, _b, _r in services.capitalisable_lines(w.alm, user=w.a)][0]
    adjustment = services.record_cost_adjustment(asset, user=w.a, purchase_line=note_line, reason_ar="إشعار دائن")
    services.submit_event(adjustment, user=w.a)
    t.decide(adjustment, w.c, permission="fixed_assets.capitalise")
    adjustment.refresh_from_db()
    asset.refresh_from_db()
    assert (adjustment.status, adjustment.amount, adjustment.journal_entry) == ("approved", D("-10000.00"), None)
    assert asset.cost == D("110000.00")
    run = t.run(w, w.alm, "2027-01")
    assert run.status == "posted"


def test_at1_self_approval_is_refused_to_a_maker_who_holds_the_right(w):
    """R37: CA (ceo in ALM, not its top authority) drafts and submits, then cannot decide."""
    bill = t.bill(w, w.alm, [("1520", "50000.00")])
    asset = services.create_asset_from_line(t.line_of(bill), user=w.ca, location=w.loc, vat_use="taxable",
                                            held_for="own_use")
    services.submit_asset(asset, user=w.ca, available_for_use_date=date(2027, 2, 1))
    with pytest.raises(access.AccessDenied) as exc:
        t.decide(asset, w.ca, permission="fixed_assets.capitalise")
    assert exc.value.code == "self_approval" and exc.value.message_ar


def test_r3_no_capitalisation_threshold_every_line_is_listed(w):
    bill = t.bill(w, w.alm, [("1520", "1.00"), ("5300", "500.00")])
    listed = services.capitalisable_lines(w.alm, user=w.a)
    assert [(l.pk, r) for l, _b, r in listed] == [(t.line_of(bill, 1).pk, D("1.00"))]  # expense line not listed


def test_r4_a_line_feeds_at_most_its_posted_amount_across_split_components(w):
    bill = t.bill(w, w.alm, [("1520", "120000.00")])
    line = t.line_of(bill)
    first = services.create_asset_from_line(line, user=w.a, location=w.loc, amount=D("70000.00"))
    second = services.create_asset_from_line(line, user=w.a, location=w.loc, amount="50000.00")
    assert (first.cost, second.cost) == (D("70000.00"), D("50000.00"))
    with pytest.raises(FixedAssetError) as exc:
        services.create_asset_from_line(line, user=w.a, location=w.loc, amount="0.01")
    assert code_of(exc) == "line_fully_linked"
    with pytest.raises(IntegrityError), transaction.atomic():  # DB: one component per line and asset
        dup = AssetCostComponent(entity=w.alm, asset=first, kind="bill_line", purchase_line=line, amount=D("1.00"),
                                 accounting_date=date(2027, 1, 15), created_by=w.a)
        dup._service_write = True
        dup.save()


def test_r4_a_reversed_bill_needs_an_adjustment_before_the_next_run(w):
    bill = t.bill(w, w.alm, [("1520", "30000.00")], on=date(2027, 1, 5))
    asset = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 5))
    inv.reverse_document(bill, user=w.a, reason="wrong vendor")
    with pytest.raises(FixedAssetError) as exc:
        services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    assert code_of(exc) == "pending_adjustment" and "reversed" in str(exc.value)
    component = asset.components.get()
    e = services.record_cost_adjustment(asset, user=w.a, reverses_component=component, event_date=date(2027, 1, 20),
                                        reason_ar="فاتورة معكوسة")
    services.submit_event(e, user=w.a)
    t.decide(e, w.c, permission="fixed_assets.capitalise")
    asset.refresh_from_db()
    assert asset.cost == ZERO_COST


ZERO_COST = D("0.00")


def test_r5_acquisition_date_is_never_after_available_for_use(w):
    bill = t.bill(w, w.alm, [("1520", "1000.00")], on=date(2027, 3, 1))
    asset = services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc, vat_use="taxable",
                                            held_for="own_use")
    assert asset.acquisition_date == date(2027, 3, 1)
    with pytest.raises(FixedAssetError) as exc:
        services.submit_asset(asset, user=w.a, available_for_use_date=date(2027, 2, 1))
    assert code_of(exc) == "acquisition_after_available"


def test_r8_a_returned_submission_goes_back_to_draft(w):
    bill = t.bill(w, w.alm, [("1520", "1000.00")])
    asset = services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc, vat_use="taxable",
                                            held_for="own_use")
    services.submit_asset(asset, user=w.a, available_for_use_date=date(2027, 2, 1))
    t.decide(asset, w.c, permission="fixed_assets.capitalise", approve=False, note="أكمل البيانات")
    asset.refresh_from_db()
    assert asset.status == "draft"
    assert ApprovalRequest.objects.get(document_type="fixed_assets.fixedasset", document_id=asset.pk).status == \
        "returned"
    services.update_asset(asset, user=w.a, serial_or_plate="ABC 1234")
    services.submit_asset(asset, user=w.a)
    t.decide(asset, w.c, permission="fixed_assets.capitalise")
    asset.refresh_from_db()
    assert asset.status == "active"


# --- AT-14, R6, R28 -----------------------------------------------------------------------------------------------

def test_at14_bai_ajel_lines_are_never_registered(w):
    bill = t.bill(w, w.alm, [("1250", "36000.00")])
    assert services.capitalisable_lines(w.alm, user=w.a) == []
    with pytest.raises(FixedAssetError) as exc:
        services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc)
    assert code_of(exc) == "bai_ajel_asset" and "1250" in str(exc.value) and "للبيع الآجل" in str(exc.value)
    with pytest.raises(FixedAssetError) as exc:
        services.create_category(w.alm, user=w.a, code="BAJ", name_ar="بيع آجل", asset_class="vehicle",
                                 cost_account="1250")
    assert code_of(exc) == "bai_ajel_asset"
    # A register vehicle (its bill on 1520) can never be sold on bai' ajel: invoicing R120 sells only 1250 debits.
    vehicle_bill = t.bill(w, w.alm, [("1520", "120000.00")])
    t.capitalise(w, t.line_of(vehicle_bill), available=date(2027, 1, 15))
    from invoicing.services import _check_deferred_sale

    with pytest.raises(Exception) as exc:
        _check_deferred_sale(w.alm, employee=SimpleNamespace(entity_id=w.alm.pk), bill=vehicle_bill,
                             cost=D("120000.00"), possession_date=date(2027, 1, 16), possession_evidence_ref="EV-1",
                             today=date(2027, 2, 1), sale_date=date(2027, 2, 1), source_type="payroll",
                             source_id=1, asset_type="vehicle")
    assert getattr(exc.value, "code", None) == "cost_mismatch"


# --- AT-18, R1, R29 -----------------------------------------------------------------------------------------------

def test_at18_entity_separation(w):
    alm_bill = t.bill(w, w.alm, [("1520", "1000.00")])
    sub_bill = t.bill(w, w.sub, [("1520", "2000.00")])
    alm_asset = services.create_asset_from_line(t.line_of(alm_bill), user=w.a, location=w.loc)
    sub_asset = services.create_asset_from_line(t.line_of(sub_bill), user=w.sa, location=w.loc_sub)
    assert list(services.register(w.alm, user=w.a2)) == [alm_asset]
    with pytest.raises(access.AccessDenied):
        services.register(w.sub, user=w.a2)
    with pytest.raises(access.AccessDenied):  # no right in SUB at all
        services.create_asset_from_line(t.line_of(sub_bill), user=w.a2, asset=alm_asset)
    both = make_user("both")
    member(both, w.alm, "accountant")
    member(both, w.sub, "accountant")
    with pytest.raises(FixedAssetError) as exc:  # R1: a SUB line never feeds an ALM asset
        services.create_asset_from_line(t.line_of(sub_bill), user=both, asset=alm_asset)
    assert code_of(exc) == "other_entity"
    with pytest.raises(FixedAssetError) as exc:
        services.update_asset(alm_asset, user=both, location=w.loc_sub)
    assert code_of(exc) == "other_entity"
    with pytest.raises(FixedAssetError):
        services.create_asset_from_line(t.line_of(alm_bill), user=w.a, category=t.category(w.sub, "VEH"),
                                        location=w.loc)
    assert sub_asset.entity == w.sub


# --- AT-22, R8, R30, R42 ------------------------------------------------------------------------------------------

def test_at22_an_active_asset_changes_only_through_events(w):
    bill = t.bill(w, w.alm, [("1520", "1000.00")])
    asset = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 15))
    for name, value, code in (("entity", w.sub, "entity_immutable"), ("cost", D("1.00"), "service_only"),
                              ("category", t.category(w.alm, "VEHR"), "service_only"),
                              ("vat_use", "exempt", "vat_use_by_event")):
        fresh = FixedAsset.objects.get(pk=asset.pk)
        setattr(fresh, name, value)
        with pytest.raises(FixedAssetError) as exc:
            fresh.save()
        assert code_of(exc) == code, name
    with pytest.raises(FixedAssetError) as exc:  # the service refuses vat_use too (R47)
        services.update_asset(asset, user=w.a, vat_use="exempt")
    assert code_of(exc) == "vat_use_by_event"
    with pytest.raises(FixedAssetError) as exc:
        FixedAsset.objects.get(pk=asset.pk).delete()
    assert code_of(exc) == "never_deleted"
    with pytest.raises(FixedAssetError):
        FixedAsset.objects.filter(pk=asset.pk).delete()
    with pytest.raises(FixedAssetError):
        FixedAsset.objects.filter(pk=asset.pk).update(cost=D("1.00"))
    assert FixedAsset.objects.get(pk=asset.pk).cost == D("1000.00")


# --- AT-26, AT-31, R39 --------------------------------------------------------------------------------------------

def _rate(w, currency, rate, on):
    treasurer = make_user(f"treasury.{currency}.{on}")
    member(treasurer, w.alm, "group_treasury")
    return ledger.create_rate(user=treasurer, from_currency=currency, to_currency="SAR", on=on, rate=D(rate),
                              group=w.alm, source="sama", source_reference=f"SAMA test bulletin {on}")


def test_at26_foreign_currency_component_is_the_booked_sar_and_never_revalued(w):
    _rate(w, "USD", "3.7500", date(2027, 1, 15))
    _rate(w, "USD", "3.8000", date(2027, 1, 31))
    bill = t.bill(w, w.alm, [("1540", "10000.00")], currency="USD")
    asset = services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc)
    assert asset.cost == D("37500.00") and asset.category.code == "IT"
    run = ledger.revalue_period(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    if run.entry_id:
        assert not run.entry.lines.filter(account__code="1540").exists()
    asset.refresh_from_db()
    assert asset.cost == D("37500.00")


def test_at31_three_decimal_currency_and_two_decimal_register_amounts(w):
    _rate(w, "KWD", "12.2000", date(2027, 1, 15))
    bill = t.bill(w, w.alm, [("1520", "1000.125")], currency="KWD")
    asset = services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc)
    assert asset.cost == D("12201.53")  # 1,000.125 x 12.2000 = 12,201.525, half up
    with pytest.raises(FixedAssetError) as exc:
        services.update_asset(asset, user=w.a, residual_value=D("100.005"))
    assert code_of(exc) == "amount_decimals" and "100.005" in str(exc.value)
    with pytest.raises(FixedAssetError) as exc:
        services.update_asset(asset, user=w.a, residual_value=100.5)  # floats are refused, never converted
    assert code_of(exc) == "amount_decimals"


# --- AT-39, R4a ---------------------------------------------------------------------------------------------------

def test_at39_deferred_payment_flag_follows_normal_credit_terms(w):
    bill = t.bill(w, w.alm, [("1520", "80000.00")], on=date(2027, 2, 1), due=date(2027, 8, 1))
    asset = services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc)
    assert not asset.deferred_payment_flag
    report = services.register_report(w.alm, user=w.a)
    assert report["notes"] == ["Normal credit terms not set / لم تُحدَّد مدة الائتمان المعتادة"]
    services.update_settings(w.alm, user=w.a, normal_credit_days=60)
    asset.refresh_from_db()
    assert asset.deferred_payment_flag and asset.components.get().deferred_payment
    report = services.register_report(w.alm, user=w.a)
    assert report["notes"] == [] and report["deferred_payment"] == [asset.code]
    assert asset.cost == D("80000.00")  # the full bill amount stays the cost (no split, OQ-25)


# --- AT-44, R46 ---------------------------------------------------------------------------------------------------

def test_at44_vat_and_art52_data_at_capitalisation(w):
    """60% recovered: invoicing as built recovers a taxable line fully and puts an exempt line's VAT in its cost (R29;
    partial recovery per line, R104, is not built), so the 60% comes from two lines of one asset."""
    bill = t.bill(w, w.alm, [("1520", "60000.00"), ("1520", "40000.00", {"vat_attribution": "exempt"})])
    asset = services.create_asset_from_line(t.line_of(bill, 1), user=w.a, location=w.loc)
    asset = services.create_asset_from_line(t.line_of(bill, 2), user=w.a, asset=asset)
    assert asset.cost == D("106000.00")  # 60,000 + 40,000 + 6,000 non-recoverable VAT in cost
    with pytest.raises(FixedAssetError) as exc:
        services.submit_asset(asset, user=w.a, available_for_use_date=date(2027, 1, 15))
    assert code_of(exc) == "missing_submission_data" and "VAT use" in str(exc.value) and "held for" in str(exc.value)
    services.update_asset(asset, user=w.a, vat_use="mixed", held_for="own_use")
    services.submit_asset(asset, user=w.a, available_for_use_date=date(2027, 1, 15))
    t.decide(asset, w.c, permission="fixed_assets.capitalise")
    asset.refresh_from_db()
    assert (asset.input_vat_amount, asset.initial_recovery_pct) == (D("15000.00"), D("60.0000"))
    assert asset.adjustment_period_end == date(2032, 1, 15)  # acquisition + min(72, 60 months)


# --- AT-47, R9, R49 -----------------------------------------------------------------------------------------------

def test_at47_1510_is_for_investment_property_only(w):
    with pytest.raises(FixedAssetError) as exc:
        services.create_category(w.alm, user=w.a, code="VIP", name_ar="سيارات", asset_class="vehicle",
                                 cost_account="1510")
    assert code_of(exc) == "ip_account_class" and "العقارات الاستثمارية" in str(exc.value)
    with pytest.raises(FixedAssetError) as exc:
        services.create_category(w.alm, user=w.a, code="IPX", name_ar="مبنى", asset_class="ip_building",
                                 cost_account="1500")
    assert code_of(exc) == "ip_account_class"
    ok = services.create_category(w.alm, user=w.a, code="IP2", name_ar="مبنى استثماري", asset_class="ip_building",
                                  cost_account="1510", accum_depr_account="1591", depr_expense_account="5400")
    assert ok.retention_class == "capital_asset_real_estate"


def test_r9_category_accounts_are_restricted_asset_accounts(w):
    with pytest.raises(FixedAssetError):
        services.create_category(w.alm, user=w.a, code="BAD", name_ar="خطأ", asset_class="other", cost_account="5300")
    with pytest.raises(FixedAssetError):
        services.create_category(w.alm, user=w.a, code="BAD", name_ar="خطأ", asset_class="other",
                                 cost_account="1500", accum_depr_account="1100")
    assert not AssetCategory.objects.filter(code="BAD").exists()
