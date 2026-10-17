"""Fixtures for banking tests (banking.md §8). Anonymised entities, synthetic IBANs, fake amounts only."""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.db import connection

from banking import services
from banking.models import Bank
from banking.testing import make_iban
from core import access
from core.access.models import Membership, Role, RolePermission
from core.access.testing import make_agent, make_user, member
from core.models import Entity
from ledger import services as ledger
from ledger.models import Account
from ledger.services_setup import create_monthly_periods, install_bank_sub_account, install_chart_of_accounts

D = Decimal
CUTOVER = date(2026, 12, 31)
postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")


def make_entity(code, consolidation="full", parent=None, **extra):
    e = Entity.objects.create(code=code, name_en=f"{code} Co", name_ar=f"شركة {code}", consolidation=consolidation,
                              parent=parent, **extra)
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2026)  # cutover balances are dated 2026-12-31 (OQ-17)
    create_monthly_periods(e, 2027)
    return e


IBANS = {
    "RIYAD-SAR": make_iban("20", "000000000000101234"),
    "RAJHI-SAR": make_iban("80", "000000000000105678"),
    "SNB-USD": make_iban("10", "000000000000109012"),
    "ALINMA-KWD": make_iban("05", "000000000000103456"),
    "SUB-ALINMA-SAR": make_iban("05", "000000000000207890"),
    "NSH-RIYAD-SAR": make_iban("20", "000000000000304321"),
}


def open_account(w, entity, code, bank, gl, currency, basis, *, preparer=None, approver=None, iban=None,
                 fmt="mt940", opening="100000.00", opening_date=date(2027, 1, 1), csv_layout=None):
    preparer = preparer or (w.a if entity == w.alm else w.users[entity.code]["acc"])
    approver = approver or (w.p if entity == w.alm else w.users[entity.code]["app"])
    account = services.create_bank_account(
        entity, user=preparer, code=code, name_en=f"{code} account", name_ar=f"حساب {code}", bank=bank,
        currency=currency, gl_account=gl, sharia_basis=basis, statement_format=fmt, csv_layout=csv_layout,
        opening_statement_date=opening_date, opening_statement_balance=D(opening), active_from=opening_date,
        iban=iban if iban is not None else IBANS[code if entity == w.alm else f"{entity.code}-{code}"],
    )
    return services.approve_bank_account(account, user=approver)


def journal(w, entity, on, lines, *, creator=None, poster=None, reference="", description="Test journal / قيد اختبار"):
    """A posted manual journal (drafted by an accountant, posted by an approver)."""
    creator = creator or (w.a if entity == w.alm else w.users[entity.code]["acc"])
    poster = poster or (w.p if entity == w.alm else w.users[entity.code]["app"])
    entry = ledger.create_entry(entity, on, description, lines, user=creator, reference=reference)
    grant = access.require(poster, entity, "ledger.post_entry", document=entry)
    return ledger.post_entry(entry, grant)


def bank_line(entry, gl_code):
    return entry.lines.get(account__code=gl_code)


def rate(w, currency, on, value, entity=None):
    return ledger.create_rate(user=w.t, from_currency=currency, to_currency="SAR", on=on, rate=D(value), group=w.alm)


@pytest.fixture
def w(db):
    alm = make_entity("ALM", "parent")
    sub = make_entity("SUB", "full", alm)
    nsh = make_entity("NSH", "full", alm, sharia_enforced=False)
    mini = make_entity("MIN", "equity", alm, ownership_pct=D("25"))
    other = make_entity("OTHER", "parent")
    Bank.objects.update(verified=True)  # the accountant verified the test bank codes (OQ-4)
    gl = SimpleNamespace(
        riyad=install_bank_sub_account(alm, "1101", "Riyad SAR", "الرياض ريال", "SAR"),
        snb=install_bank_sub_account(alm, "1102", "SNB USD", "الأهلي دولار", "USD"),
        rajhi=install_bank_sub_account(alm, "1103", "Al Rajhi SAR", "الراجحي ريال", "SAR"),
        alinma_kwd=install_bank_sub_account(alm, "1104", "Alinma KWD", "الإنماء دينار", "KWD"),
        sub_alinma=install_bank_sub_account(sub, "1101", "Alinma SAR", "الإنماء ريال", "SAR"),
        nsh_riyad=install_bank_sub_account(nsh, "1101", "Riyad SAR", "الرياض ريال", "SAR"),
        min_bank=install_bank_sub_account(mini, "1101", "Riyad SAR", "الرياض ريال", "SAR"),
    )
    users = {}
    ns = SimpleNamespace(alm=alm, sub=sub, nsh=nsh, min=mini, other=other, gl=gl, users=users)
    for name, entity, role in (
        ("a", alm, "accountant"), ("a2", alm, "accountant"), ("p", alm, "approver"), ("v", alm, "viewer"),
        ("s", sub, "accountant"), ("ps", sub, "approver"), ("n", nsh, "accountant"), ("pn", nsh, "approver"),
        ("m", mini, "accountant"), ("pm", mini, "approver"), ("mp", alm, "managing_partner"),
        ("t", alm, "group_treasury"),
    ):
        user = make_user(name)
        member(user, entity, role)
        setattr(ns, name, user)
    users.update({"SUB": {"acc": ns.s, "app": ns.ps}, "NSH": {"acc": ns.n, "app": ns.pn},
                  "MIN": {"acc": ns.m, "app": ns.pm}})
    role = Role.objects.create(code="agent_bank_assistant", name_en="Agent: bank assistant",
                               name_ar="وكيل مساعد البنوك", scope="entity", kind="agent")
    for codename in ("banking.view", "banking.suggest_match", "banking.create_bank_transaction"):
        RolePermission.objects.create(role=role, codename=codename)
    ns.g = make_agent("bank_assistant")
    Membership.objects.create(user=ns.g, entity=alm, role=role, valid_from=access.services.now() - timedelta(hours=1),
                              granted_by=ns.p, reason="pilot")
    return ns


@pytest.fixture
def accounts(w):
    """ALM: RIYAD-SAR (1101, conventional), RAJHI-SAR (1103, islamic), SNB-USD (1102), ALINMA-KWD (1104);
    SUB: ALINMA-SAR (1101). The opening statement balances are posted as cutover journals dated before the
    accounts' active range (OQ-17): they count in the book balance but are not book items to match (R16)."""
    ns = SimpleNamespace(
        riyad=open_account(w, w.alm, "RIYAD-SAR", "riyad", w.gl.riyad, "SAR", "conventional"),
        rajhi=open_account(w, w.alm, "RAJHI-SAR", "alrajhi", w.gl.rajhi, "SAR", "islamic"),
        snb=open_account(w, w.alm, "SNB-USD", "snb", w.gl.snb, "USD", "conventional", opening="0.00"),
        kwd=open_account(w, w.alm, "ALINMA-KWD", "alinma", w.gl.alinma_kwd, "KWD", "islamic", opening="0.000"),
        sub=open_account(w, w.sub, "ALINMA-SAR", "alinma", w.gl.sub_alinma, "SAR", "islamic", opening="0.00"),
    )
    journal(w, w.alm, CUTOVER, [{"account": "1101", "debit": D("100000.00")},
                                {"account": "3900", "credit": D("100000.00")}], reference="OPENING")
    journal(w, w.alm, CUTOVER, [{"account": "1103", "debit": D("100000.00")},
                                {"account": "3900", "credit": D("100000.00")}], reference="OPENING")
    return ns


# --- invoicing documents and statements ------------------------------------------------------------------------

def customer(w, entity=None, code="C001"):
    from invoicing import services as inv

    entity = entity or w.alm
    return inv.create_customer(
        entity, user=w.a if entity == w.alm else w.users[entity.code]["acc"], code=code, name_en="Test Buyer LLC",
        name_ar="شركة المشتري التجريبية", is_vat_registered=True, vat_number="310000000000003",
        street_en="Buyer Street", street_ar="شارع المشتري", building_no="2345", district_en="Buyer District",
        district_ar="حي المشتري", city_en="Jeddah", city_ar="جدة", postal_code="23456", country="SA",
    )


def receipt(w, amount, on, *, bank_reference="", gl="1101", buyer=None):
    """A posted customer receipt (recorded and posted by the accountant, invoicing D26)."""
    from invoicing import services as inv

    buyer = buyer or getattr(w, "_customer", None) or customer(w)
    w._customer = buyer
    doc = inv.create_receipt(w.alm, user=w.a, customer=buyer, date=on, amount=D(amount), bank_account=gl,
                             bank_reference=bank_reference)
    return inv.post_receipt(doc, user=w.a)


def vendor_payment(w, amount, on, *, bank_reference="", gl="1101"):
    from invoicing import services as inv

    supplier = getattr(w, "_vendor", None) or inv.create_vendor(
        w.alm, user=w.a, vendor_kind="business", code="V001", name_en="Test Supplier", name_ar="المورد التجريبي",
        is_vat_registered=True, vat_number="320000000000003")
    w._vendor = supplier
    doc = inv.create_vendor_payment(w.alm, user=w.a, vendor=supplier, date=on, amount=D(amount), bank_account=gl,
                                    bank_reference=bank_reference)
    inv.submit_vendor_payment(doc, user=w.a)
    return inv.approve_vendor_payment(doc, user=w.p)


def doc_bank_line(doc, gl="1101"):
    return doc.journal_entry.lines.get(account__code=gl, line_kind="normal")


def statement(w, account, lines, *, start, end, user=None, fmt="mt940", **kwargs):
    """Import the next statement of ``account`` (opening = the previous closing). ``lines`` as testing.mt940."""
    from banking.models import StatementImport
    from banking.testing import camt053, mt940

    latest = StatementImport.objects.filter(bank_account=account).order_by("-period_end", "-sequence_no").first()
    opening = latest.closing_balance if latest else account.opening_statement_balance
    sequence = (latest.sequence_no + 1) if latest else 1
    builder = camt053 if fmt == "camt053" else mt940
    data = builder(statement_id=f"{account.code}-{sequence}", sequence=sequence, currency=account.currency_id,
                   opening=str(opening), opening_date=start, closing_date=end, lines=lines, account=account.iban)
    return services.import_statement(account, data, user=user or (w.a if account.entity == w.alm else
                                                                  w.users[account.entity.code]["acc"]),
                                     format=fmt, **kwargs)
