"""Contracts and lines (billing R1 to R9, R24 to R31, R36, R37, R68; AT-1, AT-5, AT-11 to AT-16, AT-20, AT-21,
AT-37, AT-47, AT-50)."""
from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from billing import hooks, services
from billing.conftest import LEASE, approved, clear, contract, period, run
from billing.models import BillingError, BillingPeriod, Contract, ContractEvent, ContractLine, ContractVersion
from core import access
from core.access.testing import make_user, member
from core.models import Currency
from invoicing import services as inv
from invoicing.conftest import ADDRESS, make_entity
from invoicing.models import SalesDocument
from ledger.models import Account

D = Decimal


def _codes(exc):
    return exc.value.code


# --- AT-1, R6 to R8 -----------------------------------------------------------------------------------

def test_at1_cfo_approves_with_number_schedule_and_event(w):
    c = contract(w)
    with pytest.raises(access.AccessDenied):
        services.approve_contract(c, user=w.s)  # not CFO
    c = services.approve_contract(c, user=w.c)
    assert (c.status, c.number) == ("active", "ALM-CTR-2027-000001")
    assert c.approved_by == w.c and c.current_version.version_no == 1
    assert c.current_version.status == "approved" and c.current_version.approved_by == w.c
    assert c.periods.count() == 12 and set(c.periods.values_list("status", flat=True)) == {"planned"}
    assert list(c.events.values_list("action", flat=True)) == ["created", "approved", "activated"]
    with pytest.raises(BillingError) as exc:  # R7: edits after approval go through versions
        services.update_contract(c, user=w.s, title_ar="عنوان آخر")
    assert exc.value.code == "not_draft"
    line = c.lines.get()
    with pytest.raises(BillingError):
        services.update_line(line, user=w.s, amount="11000.00")
    with pytest.raises(BillingError):  # the approved version's line cannot be saved at all (R38)
        line.amount = D("1.00")
        line._service_write = True
        line.save()
    with pytest.raises(BillingError):  # R6: active -> draft only through services, which have no such step
        stored = Contract.objects.get(pk=c.pk)
        stored.status = "draft"
        stored.save()
    second = approved(w)
    assert second.number == "ALM-CTR-2027-000002"  # gap-free per entity and year (R8)


def test_r8_creator_and_last_modifier_never_approve(w):
    c = contract(w, user=w.c)  # the CFO drafts it
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_contract(c, user=w.c)
    assert exc.value.code == "self_approval"
    c2 = contract(w)
    services.add_line(c2, user=w.c_b, income_type="rent_commercial", amount="500.00", description_ar="خدمات")
    with pytest.raises(access.AccessDenied):  # c_b changed the draft
        services.approve_contract(c2, user=w.c_b)
    assert services.approve_contract(c2, user=w.c).status == "active"


def test_r9_future_start_is_approved_then_active_on_the_first_run(w):
    c = approved(w, start=date(2027, 3, 1))
    assert c.status == "approved"
    run(w, date(2027, 2, 15))
    c.refresh_from_db()
    assert c.status == "approved"
    run(w, date(2027, 3, 1))
    c.refresh_from_db()
    assert c.status == "active"
    assert period(c, date(2027, 3, 1)).status == "generated"


def test_submit_and_return_contract(w):
    c = contract(w)
    c = services.submit_contract(c, user=w.s)
    assert c.submitted_by == w.s
    with pytest.raises(access.AccessDenied):  # the submitter cannot approve (R14)
        services.approve_contract(c, user=w.s)
    with pytest.raises(BillingError):
        services.return_contract(c, user=w.c, reason_ar="")
    c = services.return_contract(c, user=w.c, reason_ar="أكمل البيانات")
    assert c.submitted_at is None
    assert list(c.events.values_list("action", flat=True))[-2:] == ["submitted_for_approval", "returned"]


# --- AT-5, AT-37, R16, R37: amounts ------------------------------------------------------------------------

def test_at5_float_rejected_and_decimals_limited(w):
    c = contract(w, lines=())
    with pytest.raises(BillingError) as exc:
        services.add_line(c, user=w.s, income_type="rent_commercial", amount=10000.0, description_ar="إيجار")
    assert exc.value.code == "float_rejected"
    with pytest.raises(BillingError) as exc:
        services.add_line(c, user=w.s, income_type="rent_commercial", amount="10.005", description_ar="إيجار")
    assert exc.value.code == "amount_decimals"  # SAR: 2 dp
    with pytest.raises(BillingError):
        services.add_line(c, user=w.s, income_type="rent_commercial", amount="0", description_ar="إيجار")


def test_at37_kwd_three_decimals_jpy_refused(w):
    kwd = contract(w, currency="KWD", lines=(("rent_commercial", "1000.000"),))
    assert kwd.lines.get().amount == D("1000.000")
    with pytest.raises(BillingError) as exc:
        services.add_line(kwd, user=w.s, income_type="rent_commercial", amount="1.2345", description_ar="إيجار")
    assert exc.value.code == "amount_decimals"
    with pytest.raises(BillingError) as exc:
        contract(w, currency="JPY", lines=())
    assert "0 decimals" in str(exc.value) and "بلا خانات" in str(exc.value)


# --- AT-47, AT-16, R30, R31, R68: build gates --------------------------------------------------------------------

def test_at47_build_gates_refuse_at_approval(w):
    quarterly = contract(w, billing_frequency="quarterly", billing_timing="advance")
    with pytest.raises(BillingError) as exc:
        services.approve_contract(quarterly, user=w.c)
    assert "OQ-2" in str(exc.value) and "المؤجلة" in str(exc.value)
    assert not quarterly.periods.exists()
    assert approved(w, billing_frequency="quarterly", billing_timing="arrears").status == "active"  # arrears allowed
    offset = contract(w)
    services.set_advance_terms(offset, user=w.s, amount="20000.00", due_date=date(2027, 1, 1),
                               recovery="offset_against_invoices", notes_ar="تخصم من الفواتير")
    with pytest.raises(BillingError) as exc:
        services.approve_contract(offset, user=w.c)
    assert "OQ-20" in str(exc.value) and "الدفعات المقدمة" in str(exc.value)
    # a 0-decimal currency that got past the save check (e.g. an import) is refused at approval too
    jpy = contract(w, lines=())
    Contract._base_manager.filter(pk=jpy.pk).update(currency=Currency.objects.get(code="JPY"))
    with pytest.raises(BillingError) as exc:
        services.approve_contract(jpy, user=w.c)
    assert "JPY" in str(exc.value)


def test_at16_deposit_is_memo_only(w, django_capture_on_commit_callbacks):
    c = contract(w)
    services.set_deposit_terms(c, user=w.s, amount="30000.00", due_date=date(2027, 1, 1), refundable=True,
                               refund_conditions_ar="يرد عند انتهاء العقد")
    services.set_advance_terms(c, user=w.s, amount="10000.00", due_date=date(2027, 1, 1), recovery="none")
    c = services.approve_contract(c, user=w.c)
    run(w, date(2027, 1, 1))
    assert SalesDocument.objects.filter(entity=w.alm).count() == 1  # the January rent only, no deposit invoice
    assert hooks.contract_allows(c, "deposit") and hooks.contract_allows(c, "advance")
    assert not hooks.contract_allows(approved(w), "deposit")


# --- R24, R25, R27, R29 (AT-12, AT-13, AT-50) ------------------------------------------------------------------------

def test_at12_residential_lease_is_exempt_commercial_is_standard(w):
    res = contract(w, lease_use="residential", lines=())
    with pytest.raises(BillingError):
        services.add_line(res, user=w.s, income_type="rent_residential", amount="8000.00", description_ar="إيجار",
                          vat_category="S")
    line = services.add_line(res, user=w.s, income_type="rent_residential", amount="8000.00", description_ar="إيجار")
    assert (line.vat_category, line.vat_exemption_reason.code, line.income_account.code) == ("E", "VATEX-SA-30", "4120")
    com = contract(w, lines=())
    with pytest.raises(BillingError):
        services.add_line(com, user=w.s, income_type="rent_commercial", amount="1000.00", description_ar="إيجار",
                          vat_category="E", vat_exemption_reason="VATEX-SA-30")
    res = services.approve_contract(res, user=w.c)
    run(w, date(2027, 1, 1))
    doc = clear(w, period(res, date(2027, 1, 1)).sales_document)
    assert doc.total_vat == D("0.00")
    assert sorted((ln.account.code, ln.debit, ln.credit) for ln in doc.journal_entry.lines.all()) == [
        ("1200", D("8000.00"), D("0.00")), ("4120", D("0.00"), D("8000.00"))]  # B2 (amended)


def test_at13_sharia_prohibited_refused_purification_flagged(w):
    Account.objects.create(entity=w.alm, code="4950", name_en="Interest income (test)", name_ar="إيراد فوائد (تجريبي)",
                           type="income", sharia_prohibited=True)
    c = contract(w, lines=())
    with pytest.raises(BillingError) as exc:
        services.add_line(c, user=w.s, income_type="rent_commercial", amount="100.00", description_ar="إيجار",
                          income_account="4950")
    assert "Sharia" in str(exc.value) and "محظور شرعًا" in str(exc.value)
    services.add_line(c, user=w.s, income_type="rent_commercial", amount="100.00", description_ar="إيجار",
                      income_account="4900")
    c = services.approve_contract(c, user=w.c)
    assert c.requires_purification
    run(w, date(2027, 1, 1))
    assert period(c, date(2027, 1, 1)).sales_document.requires_purification


def test_r27_default_accounts_by_income_type(w):
    third = contract(w, lines=(("rent_commercial", "100.00"),))
    assert third.lines.get().income_account.code == "4100"
    ic = contract(w, customer=w.ic_customer, lines=(("rent_commercial", "100.00"),))
    assert ic.lines.get().income_account.code == "4110"
    ip = contract(w, customer=w.ic_customer, contract_type="service", lease_use="", property_ref="", unit_ref="",
                  lines=(("ip_licence", "20000.00"),))
    assert ip.lines.get().income_account.code == "4310"  # AT-50: accepted on 4310
    other = contract(w, contract_type="other", lease_use="", lines=())
    with pytest.raises(BillingError) as exc:
        services.add_line(other, user=w.s, income_type="other", amount="10.00", description_ar="أخرى")
    assert exc.value.code == "account_required"
    line = services.add_line(other, user=w.s, income_type="other", amount="10.00", description_ar="أخرى",
                             income_account="4240")
    assert line.income_account.code == "4240"


def test_r25_vehicle_and_secondment_default_standard(w):
    car = contract(w, contract_type="vehicle_rental", lease_use="", vehicle_ref="TEST-PLATE-1",
                   lines=(("car_rental", "3000.00"),))
    assert car.lines.get().vat_category == "S"
    with pytest.raises(BillingError):  # Z without a reason (invoicing R10)
        services.add_line(car, user=w.s, income_type="car_rental", amount="1.00", description_ar="سيارة",
                          vat_category="Z")


def test_r3_counterparty_is_computed_and_never_own_entity(w):
    ic = contract(w, customer=w.ic_customer)
    assert ic.counterparty_entity == w.sub and ic.is_intercompany
    with pytest.raises(BillingError):
        services.create_contract(w.alm, user=w.s, customer=w.customer, start_date=date(2027, 1, 1),
                                 end_date=date(2027, 12, 31), counterparty_entity=w.sub, **LEASE)
    with pytest.raises(IntegrityError), transaction.atomic():  # never the contract's own entity (DB check)
        Contract._base_manager.filter(pk=ic.pk).update(counterparty_entity=w.alm)


def test_contract_required_fields_bilingual(w):
    with pytest.raises(BillingError) as exc:
        contract(w, title_ar="", lines=())
    assert "Arabic title" in str(exc.value) and "العنوان بالعربية" in str(exc.value)
    with pytest.raises(BillingError):
        contract(w, end=None, lines=())  # OQ-9: leases need an end date
    with pytest.raises(BillingError):
        contract(w, property_ref="", lines=())
    c = contract(w, lines=())
    with pytest.raises(BillingError) as exc:  # AT-21: missing Arabic description
        services.add_line(c, user=w.s, income_type="rent_commercial", amount="10.00", description_ar="")
    assert "الوصف بالعربية" in str(exc.value)


# --- AT-14, AT-15, R1, R2, R5: entity separation -------------------------------------------------------------------

def test_at14_entity_separation(w):
    alm_c = approved(w)
    sub_c = approved(w, entity=w.sub, customer=w.sub_customer)
    assert list(services.contracts(w.sub, user=w.sub_s)) == [sub_c]
    with pytest.raises(access.AccessDenied):
        services.contracts(w.alm, user=w.sub_s)
    c = contract(w, lines=())
    sub_4100 = Account.objects.get(entity=w.sub, code="4100")
    with pytest.raises(BillingError) as exc:
        services.add_line(c, user=w.s, income_type="rent_commercial", amount="10.00", description_ar="إيجار",
                          income_account=sub_4100)
    assert "another entity" in str(exc.value)
    with pytest.raises(BillingError) as exc:
        services.create_contract(w.alm, user=w.s, customer=w.sub_customer, start_date=date(2027, 1, 1),
                                 end_date=date(2027, 12, 31), contract_type="service", currency="SAR",
                                 billing_frequency="monthly", billing_timing="arrears", title_ar="خدمة",
                                 description_ar="خدمة")
    assert "customer belongs to another entity" in str(exc.value)
    run(w, date(2027, 1, 1), entity=w.alm)
    assert period(alm_c, date(2027, 1, 1)).status == "generated"
    assert period(sub_c, date(2027, 1, 1)).status == "planned"  # the ALM run never touches SUB
    assert not SalesDocument.objects.filter(entity=w.sub).exists()


def test_at15_new_entity_bills_without_code_change(w):
    new = make_entity("NEW", "full", w.alm, vat="312222222200003", cr="1010000003")
    cfo, staff = make_user("cfo_new"), make_user("staff_new")
    member(cfo, new, "approver")
    member(staff, new, "accountant")
    member(w.sys, new, "system_billing")
    member(w.queue, new, "system_zatca_queue")
    customer = inv.create_customer(new, user=staff, code="N1", name_ar="عميل جديد", is_vat_registered=True,
                                   vat_number="313333333300003", **ADDRESS)
    c = contract(w, entity=new, customer=customer, user=staff)
    c = services.approve_contract(c, user=cfo)
    assert c.number == "NEW-CTR-2027-000001"
    run(w, date(2027, 1, 1), entity=new)
    assert period(c, date(2027, 1, 1)).sales_document.number == "NEW-INV-2027-000001"


# --- AT-20, R36: agents draft only -------------------------------------------------------------------------------

def test_at20_agent_drafts_only(w, agent):
    c = contract(w, user=agent)
    assert c.source == "agent:contract_drafter" and c.lines.get().source == "agent:contract_drafter"
    with pytest.raises(access.AccessDenied):
        services.approve_contract(c, user=agent)
    with pytest.raises(access.AccessDenied):
        services.submit_contract(c, user=agent)
    real = approved(w)
    with pytest.raises(access.AccessDenied):
        services.create_version(real, user=agent, kind="amendment", effective_date=date(2027, 2, 1),
                                reason_ar="س")  # OQ-28 default
    with pytest.raises(access.AccessDenied):
        services.start_manual_run(w.alm, user=agent)
    with pytest.raises(access.AccessDenied):
        services.run_billing(date(2027, 1, 1), entity=w.alm, user=agent)
    p = period(real, date(2027, 1, 1))
    with pytest.raises(access.AccessDenied):
        services.release_period(p, user=agent)
    with pytest.raises(access.AccessDenied):
        services.recompute_corrections(real, user=agent)


def test_at21_texts_in_both_languages(w):
    c = approved(w)
    r = run(w, date(2027, 1, 1))
    item = r.items.get()
    assert item.message_en.startswith("Invoice") and item.message_ar.startswith("أُنشئت")
    assert " / " in Contract.Status("active").label and " / " in ContractVersion.Kind("amendment").label
    assert all(" / " in str(label) for _, label in BillingPeriod.Status.choices)
    note = services.notifications(w.alm, user=w.s).get()
    assert note.message("ar").startswith("دورة الفوترة") and note.message("en").startswith("Billing run")


def test_r1_secondment_line_needs_its_person(w):
    c = contract(w, contract_type="secondment", customer=w.ic_customer, lease_use="", end=None,
                 billing_timing="arrears", lines=())
    with pytest.raises(BillingError):
        services.add_line(c, user=w.s, income_type="secondment", amount="31000.00", description_ar="إعارة")
    a = services.add_secondment(c, user=w.s, employee_ref="EMP-T-0001", role_title_ar="محلل", role_title_en="Analyst",
                                start_date=date(2027, 1, 1))
    line = services.add_line(c, user=w.s, income_type="secondment", amount="31000.00", description_ar="إعارة",
                             secondment=a)
    assert (line.vat_category, line.income_account.code) == ("S", "4210")
    assert a.receiving_entity == w.sub


def test_contract_events_are_append_only(w):
    c = approved(w)
    event = c.events.first()
    with pytest.raises(BillingError):
        event._service_write = True
        event.save()
    with pytest.raises(BillingError):
        ContractEvent.objects.filter(pk=event.pk).delete()
    assert ContractLine.objects.filter(contract=c).count() == 1
