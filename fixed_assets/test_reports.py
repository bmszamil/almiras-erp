"""Retention, reports, restricted accounts, setup and reminders (fixed-assets.md R9, R16, R19, R31 to R34, R42;
AT-10, AT-16, AT-19, AT-23, AT-25, AT-34, AT-48). Fictional data; policies are fixture values (OQ-2)."""
import re
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from core.access import engine
from core.access import services as access
from core.access.authority_models import ApprovalRequest
from core.access.models import AccessLog
from core.access.testing import make_user, member
from fixed_assets import jobs, models as fa, services, testing as t
from fixed_assets.messages import MESSAGES, FixedAssetError
from ledger import restricted
from ledger import services as ledger
from ledger.models import JournalEntry, LedgerError

pytestmark = pytest.mark.django_db
D = Decimal
ARABIC = re.compile(r"[؀-ۿ]")


def code_of(exc):
    return getattr(exc.value, "code", None)


# --- AT-10, R33 ---------------------------------------------------------------------------------------------------

def test_at10_retention_dates_and_never_shortened(w):
    on = date(2027, 3, 10)
    bill = t.bill(w, w.alm, [("1510", "900000.00"), ("1520", "100000.00"), ("1540", "36000.00"),
                             ("1500", "120000.00"), ("1500", "50000.00"), ("1550", "80000.00")], on=on)
    ip = t.capitalise(w, t.line_of(bill, 1), available=on, category_code="IPB", usage="investment_property",
                      held_for="rental")
    car = t.capitalise(w, t.line_of(bill, 2), available=on)
    it = t.capitalise(w, t.line_of(bill, 3), available=on)
    machine = t.capitalise(w, t.line_of(bill, 4), available=on, category_code="MACH")
    leasehold = t.capitalise(w, t.line_of(bill, 5), available=on, category_code="LHI")
    cwip = services.create_asset_from_line(t.line_of(bill, 6), user=w.a, location=w.loc)
    assert ip.retention_until == date(2042, 3, 31)  # end of the tax period (Q1 2027) + 15 years
    assert car.retention_until == date(2037, 3, 10)  # 60 months + 5 years
    assert it.retention_until == date(2035, 3, 10)  # 36 months + 5 years, later than the 6-year date 2033-03-10
    assert machine.retention_until == date(2038, 3, 10)  # min(72, 120) months + 5 years
    assert (leasehold.retention_class, leasehold.retention_until) == ("capital_asset_real_estate", date(2042, 3, 31))
    assert (cwip.retention_class, cwip.retention_until) == ("capital_asset_real_estate", date(2042, 3, 31))
    target = services.create_asset(w.alm, user=w.a, category=t.category(w.alm, "VEH"), location=w.loc,
                                   description_ar="سيارة من مشروع", vat_use="taxable", held_for="own_use")
    t.event(w, cwip, "capitalise_cwip", date(2027, 4, 1), amount=D("80000.00"), target_asset=target)
    services.submit_asset(target, user=w.a, available_for_use_date=date(2027, 4, 1))
    t.decide(target, w.c, permission="fixed_assets.capitalise")
    target.refresh_from_db()
    assert (target.retention_class, target.acquisition_date) == ("movable_capital_asset", on)
    assert target.retention_until == date(2042, 3, 31)  # keeps the CWIP's real-estate date (never shortened)
    services.update_settings(w.alm, user=w.a, art52_adjustment_months_movable=60)
    for asset, expected in ((machine, date(2038, 3, 10)), (car, date(2037, 3, 10)), (target, date(2042, 3, 31))):
        asset.refresh_from_db()
        assert asset.retention_until == expected  # lowering the setting never shortens a date
    assert AccessLog.objects.filter(details__action="fixed_asset_settings_changed").exists()


# --- AT-16, R34 ---------------------------------------------------------------------------------------------------

def test_at16_zakat_inputs(w):
    on = date(2027, 12, 1)
    bill = t.bill(w, w.alm, [("1520", "60000.00"), ("1510", "480000.00"), ("1550", "100000.00"), ("1540", "3600.00")],
                  on=on)
    car = t.capitalise(w, t.line_of(bill, 1), available=on, residual_value=D("0.00"), use_evidence_ref="EV-CAR")
    ip = t.capitalise(w, t.line_of(bill, 2), available=on, category_code="IPB", usage="investment_property",
                      held_for="rental", residual_value=D("0.00"))
    services.create_asset_from_line(t.line_of(bill, 3), user=w.a, location=w.loc)
    it = t.capitalise(w, t.line_of(bill, 4), available=on, residual_value=D("0.00"))
    t.event(w, it, "write_off", date(2027, 12, 15))
    t.run(w, w.alm, "2027-12")
    report = services.zakat_inputs(w.alm, date(2027, 12, 31), user=w.v)
    ppe = {r["category"]: r for r in report["ppe"]}
    assert ppe["VEH"]["nbv"] == ppe["VEH"]["cost"] - ppe["VEH"]["accumulated_depreciation"] - \
        ppe["VEH"]["accumulated_impairment"] == D("59000.00")
    assert [(r["category"], r["nbv"]) for r in report["investment_property"]] == [("IPB", D("479000.00"))]
    assert [(r["category"], r["nbv"]) for r in report["cwip"]] == [("CWIP", D("100000.00"))]  # separately
    assert [(r["asset"], r["proceeds"]) for r in report["disposed_in_year"]] == [(it.code, D("0"))]
    held = {r["asset"]: (r["held_for"], r["use_evidence_ref"]) for r in report["assets"]}
    assert held[car.code] == ("own_use", "EV-CAR") and held[ip.code] == ("rental", "")
    assert all(r["difference"] == 0 for r in report["gl"]), report["gl"]
    outsider = make_user("outsider")
    member(outsider, w.alm, "payables_clerk")
    with pytest.raises(access.AccessDenied):
        services.zakat_inputs(w.alm, date(2027, 12, 31), user=outsider)


# --- AT-19, R32 ---------------------------------------------------------------------------------------------------

def test_at19_texts_are_english_and_arabic(w):
    for code, (en, ar) in MESSAGES.items():
        assert en and (ARABIC.search(ar) or re.fullmatch(r"\{\w+\}", ar)), code  # "{why_ar}": filled in Arabic
    for model in (fa.AssetCategory, fa.AssetLocation, fa.FixedAssetSettings, fa.DepreciationPolicy, fa.FixedAsset,
                  fa.AssetCostComponent, fa.DepreciationRun, fa.DepreciationLine, fa.AssetEvent, fa.IpFairValue,
                  fa.ReviewReminder):
        assert ARABIC.search(str(model._meta.verbose_name)), model
        for field in model._meta.fields:
            if field.name not in ("id", "created_by", "created_at") and not field.name.endswith("_ptr"):
                assert ARABIC.search(str(field.verbose_name)), (model, field.name)
    arabic = make_user("arabic.cfo", language="ar")
    member(arabic, w.alm, "ceo")
    bill = t.bill(w, w.alm, [("1520", "10000.00")])
    asset = services.create_asset_from_line(t.line_of(bill), user=arabic, location=w.loc, vat_use="taxable",
                                            held_for="own_use")
    services.submit_asset(asset, user=arabic, available_for_use_date=date(2027, 1, 20))
    with pytest.raises(access.AccessDenied) as exc:
        t.decide(asset, arabic, permission="fixed_assets.capitalise")
    assert ARABIC.search(str(exc.value))  # shown in the user's language
    req = ApprovalRequest.objects.get(document_type="fixed_assets.fixedasset", document_id=asset.pk)
    assert ARABIC.search(req.label_ar) and asset.code in req.label_ar and asset.code in req.label_en
    with pytest.raises(FixedAssetError) as exc:
        services.draft_event(asset, user=w.a, kind="impairment", event_date=date(2027, 2, 1), reason_ar="")
    assert code_of(exc) == "reason_required" and ARABIC.search(str(exc.value))
    t.decide(asset, w.c, permission="fixed_assets.capitalise")
    run = t.run(w, w.alm, "2027-01")
    assert ARABIC.search(run.journal_entry.description) and ARABIC.search(run.journal_entry.lines.first().description)
    for row in jobs.raise_reminders(date(2027, 12, 31)):
        assert ARABIC.search(row.message_ar) and row.message_en


# --- AT-23, R9 ----------------------------------------------------------------------------------------------------

def test_at23_asset_accounts_are_restricted(w):
    for code in ("1500", "1510", "1520", "1590", "1540", "1591", "1595"):
        assert code in restricted.codes()
    for lines in ([{"account": "5400", "debit": D("100.00")}, {"account": "1590", "credit": D("100.00")}],
                  [{"account": "1520", "debit": D("100.00")}, {"account": "1100", "credit": D("100.00")}]):
        with pytest.raises(LedgerError) as exc:  # drafts are refused already
            ledger.create_entry(w.alm, date(2027, 1, 20), "manual", lines, user=w.a)
        assert exc.value.code == "restricted_account" and "يُرحَّل إليه" in str(exc.value)
    for document_type, code, allowed in (("banking.banktransaction", "1520", False),
                                         ("payroll.payrollrun", "1590", False),
                                         ("invoicing.purchasedocument", "1520", True),
                                         ("invoicing.purchasedocument", "1590", False),
                                         ("fixed_assets.depreciationrun", "1590", True),
                                         ("fixed_assets.assetevent", "1520", True), ("", "1520", False)):
        entry = JournalEntry(entity=w.alm, document_type=document_type, source="human")
        assert restricted.allowed(code, entry) is allowed, (document_type, code)
    assert t.bill(w, w.alm, [("1520", "1000.00")]).status == "posted"
    from invoicing.models import InvoicingError

    with pytest.raises(InvoicingError) as exc:
        t.bill(w, w.alm, [("1590", "1000.00")])
    assert getattr(exc.value, "code", None) == "restricted_account"


def test_at23_register_to_gl_is_zero_after_every_posting_kind(w):
    on = date(2027, 1, 1)
    bill = t.bill(w, w.alm, [("1520", "120000.00"), ("1540", "8000.00"), ("1550", "50000.00"),
                             ("1510", "900000.00")], on=on)
    car = t.capitalise(w, t.line_of(bill, 1), available=on, residual_value=D("20000.00"))
    it = t.capitalise(w, t.line_of(bill, 2), available=on, residual_value=D("0.00"))
    cwip = services.create_asset_from_line(t.line_of(bill, 3), user=w.a, location=w.loc)
    ip = t.capitalise(w, t.line_of(bill, 4), available=on, category_code="IPB", usage="investment_property",
                      held_for="rental", residual_value=D("0.00"))
    t.run(w, w.alm, "2027-01")  # FA2
    jan = services.register_to_gl(w.alm, date(2027, 1, 31), user=w.v)
    assert all(r["difference"] == 0 and not r["reconciling"] for r in jan), jan
    off = t.event(w, it, "write_off", date(2027, 2, 1))  # FA4 (one day of February to its date)
    building = services.create_asset(w.alm, user=w.a, category=t.category(w.alm, "BLDG"), location=w.loc,
                                     description_ar="مبنى", vat_use="taxable", held_for="own_use")
    t.event(w, cwip, "capitalise_cwip", date(2027, 2, 1), amount=D("50000.00"), target_asset=building)  # FA5
    t.event(w, ip, "impairment", date(2027, 2, 1), recoverable_amount=D("850000.00"))  # FA6
    machine = services.create_asset(w.alm, user=w.a, category=t.category(w.alm, "MACH"), location=w.loc,
                                    description_ar="آلة", vat_use="taxable", held_for="own_use")
    t.event(w, machine, "financed_acquisition", date(2027, 2, 1), cash_cost_price=D("300000.00"),
            financing_total=D("330000.00"), financing_ref="MUR-9")  # FA8
    for on in (date(2027, 2, 1), date(2027, 2, 15), date(2027, 2, 28)):
        rows = services.register_to_gl(w.alm, on, user=w.v)
        assert rows and all(r["difference"] == 0 for r in rows), (on, rows)
    assert {r["account"] for r in rows} >= {"1500", "1510", "1520", "1540", "1550", "1570", "1590", "1591", "1596"}
    stub = {r["account"]: r["reconciling"] for r in rows}["1590"]  # the write-off's day of February, not run yet
    assert [(x["asset"], x["amount"]) for x in stub] == [(it.code, off.depreciation_derecognised - D("222.22"))]
    assert "لم يُرحَّل" in stub[0]["note"]
    t.run(w, w.alm, "2027-02")
    rows = services.register_to_gl(w.alm, date(2027, 2, 28), user=w.v)
    assert all(r["difference"] == 0 and not r["reconciling"] for r in rows), rows
    assert car.status == "active"


# --- AT-25, R31 ---------------------------------------------------------------------------------------------------

def test_at25_a_new_subsidiary_is_data_only(w):
    new = t.make_entity("NEW", "full", w.alm, proposed=False)
    acc, cfo = make_user("new.acc"), make_user("new.cfo")
    member(acc, new, "accountant")
    member(cfo, new, "approver")
    out = StringIO()
    call_command("fixed_assets_setup", "--entity", "NEW", "--user", "new.acc", stdout=out)
    assert "category VEH: created" in out.getvalue() and "category IT: skipped, account 1540" in out.getvalue()
    settings = fa.FixedAssetSettings.objects.get(entity=new)
    assert (settings.art52_adjustment_months_movable, settings.art52_adjustment_months_real_estate) == (72, 120)
    veh = t.category(new, "VEH")
    assert veh.accum_impairment_account is None and veh.disposal_account is None  # proposed accounts not there (R45)
    p = services.draft_policy(veh, user=acc, effective_from=date(2020, 1, 1), useful_life_months=60,
                              basis_note_ar="سياسة اختبار")
    services.submit_policy(p, user=acc)
    t.decide(p, cfo, permission="fixed_assets.approve_policy")
    vendor = t.inv.create_vendor(new, user=acc, vendor_kind="business", code="V001", name_en="Supplier",
                                 name_ar="مورد", is_vat_registered=True, vat_number="320000000000003")
    w.vendors["NEW"] = vendor
    loc = services.create_location(new, user=acc, code="HQ", name_ar="المقر")
    doc = t.inv.create_purchase_document(new, user=acc, vendor=vendor, vendor_document_number="N-1",
                                         vendor_document_date=date(2027, 1, 1), accounting_date=date(2027, 1, 1))
    t.inv.add_purchase_line(doc, user=acc, account="1520", quantity="1", unit_price="60000.00", description_ar="سيارة")
    t.inv.update_purchase_document(doc, user=acc, vendor_total_vat="9000.00", vendor_total_gross="69000.00")
    doc = t.inv.approve_purchase_document(t.inv.submit_purchase_document(doc, user=acc), user=cfo)
    asset = services.create_asset_from_line(doc.lines.get(), user=acc, location=loc, vat_use="taxable",
                                            held_for="own_use", residual_value=D("0.00"))
    services.submit_asset(asset, user=acc, available_for_use_date=date(2027, 1, 1))
    t.decide(asset, cfo, permission="fixed_assets.capitalise")
    run = services.submit_depreciation(services.prepare_depreciation(new, t.period(new, "2027-01"), user=acc),
                                       user=acc)
    t.decide(run, cfo, permission="fixed_assets.run_depreciation")
    run.refresh_from_db()
    assert asset.code == "NEW-FA-000001" and run.number == "NEW-DEP-2027-000001" and run.status == "posted"
    assert ApprovalRequest.objects.get(document_type="fixed_assets.depreciationrun", document_id=run.pk).rule_source \
        == "default"
    e = services.draft_event(fa.FixedAsset.objects.get(pk=asset.pk), user=acc, kind="impairment",
                             event_date=date(2027, 2, 1), reason_ar="ضرر", recoverable_amount=D("50000.00"))
    with pytest.raises(FixedAssetError) as exc:  # 5420 / 1595 not approved in NEW yet (R45)
        services.submit_event(e, user=acc)
    assert code_of(exc) == "account_not_set"


# --- AT-34, R42 ---------------------------------------------------------------------------------------------------

def test_at34_nothing_is_ever_deleted_and_no_job_deletes(w):
    bill = t.bill(w, w.alm, [("1510", "500000.00"), ("1520", "1000.00")], on=date(2027, 1, 1))
    ip = t.capitalise(w, t.line_of(bill, 1), available=date(2027, 1, 1), category_code="IPB",
                      usage="investment_property", held_for="rental", custodian_ref="EMP-0001")
    car = t.capitalise(w, t.line_of(bill, 2), available=date(2027, 1, 1))
    t.run(w, w.alm, "2027-01")
    t.event(w, car, "write_off", date(2027, 2, 1))
    services.record_ip_fair_value(ip, user=w.a, valuation_date=date(2027, 1, 31), amount=D("510000.00"),
                                  valuer_name_ar="مقيم", evidence_ref="EV-1")
    jobs.raise_reminders(date(2027, 12, 31))
    counts = {m: m.objects.count() for m in (fa.AssetCategory, fa.AssetLocation, fa.FixedAssetSettings,
                                             fa.DepreciationPolicy, fa.FixedAsset, fa.AssetCostComponent,
                                             fa.DepreciationRun, fa.DepreciationLine, fa.AssetEvent, fa.IpFairValue,
                                             fa.ReviewReminder)}
    for model in counts:
        row = model.objects.first()
        with pytest.raises(FixedAssetError):
            row.delete()
        with pytest.raises(FixedAssetError):
            model.objects.all().delete()
    call_command("fixed_assets_reminders", "--date", "2028-12-31", stdout=StringIO())
    jobs.raise_reminders(date(2027, 12, 31))  # every scheduled job of the module: none deletes
    assert {m: m.objects.count() for m in counts} == counts
    assert fa.FixedAsset.objects.get(pk=ip.pk).custodian_ref == "EMP-0001"


# --- AT-48, R16, R19 ----------------------------------------------------------------------------------------------

def test_at48_reminders_at_reporting_dates_and_year_ends(w):
    services.update_settings(w.alm, user=w.a, reporting_dates=["2027-06-30"])
    june = jobs.raise_reminders(date(2027, 6, 30))
    assert [(r.entity.code, r.kind) for r in june] == [("ALM", "impairment_indicators")]
    december = {(r.entity.code, r.kind): r for r in jobs.raise_reminders(date(2027, 12, 31))}
    assert ("ALM", "impairment_indicators") in december and ("FUL", "impairment_indicators") in december
    alm = december[("ALM", "estimate_review")]
    ful = december[("FUL", "estimate_review")]
    assert "17.19" in alm.message_en and "indications" in alm.message_en and "16.51" not in alm.message_en
    assert "IAS 16.51" in ful.message_en and "at least" not in alm.message_en
    assert "36.9" in december[("ALM", "impairment_indicators")].message_en
    for row in december.values():
        assert ARABIC.search(row.message_ar)
    assert jobs.raise_reminders(date(2027, 12, 31)) == []  # once per entity, kind and date
    assert fa.ReviewReminder.objects.filter(entity=w.ful, due_date=date(2027, 6, 30)).count() == 0
