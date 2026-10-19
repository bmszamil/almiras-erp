"""Fixtures for zatca tests (zatca.md §8): group parent PAR with children TST1 and TST2, made-up identifiers,
users per role, an in-memory secret store and a fake ZATCA API. No network: real ZATCA hosts are unreachable
(R-29) and any non-loopback connection fails the test.
"""
import os
import socket
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core import access
from core.access.models import Role, RolePermission
from core.access.testing import make_agent, make_user, member
from invoicing import services as invoicing_services
from invoicing.conftest import ADDRESS, approve, invoice, make_entity
from ledger.services_setup import create_monthly_periods
from zatca import services
from zatca.models import EInvoice
from zatca.testing import fake_zatca_services

D = Decimal
# The SDK offline check (AT-69) runs only where the ZATCA SDK is unpacked locally (never in the repo).
collect_ignore = [] if os.environ.get("ZATCA_SDK_DIR") else ["test_sdk_offline.py"]

TODAY = date(2026, 10, 15)  # a fixed, past issue day (BR-KSA-04: issue date <= current date)
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=dt_timezone.utc)  # 12:00 in Riyadh, after the approvals at 10:00


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """R-29 / AT-29: zatca tests cannot reach any non-loopback host."""
    original = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
            raise AssertionError(f"R-29: network connection to {host!r} attempted in a zatca test")
        return original(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def fakes():
    with fake_zatca_services() as (store, client):
        client.cert_valid_from = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)  # valid at NOW (fixed test clock)
        client.cert_valid_days = 3 * 365
        yield SimpleNamespace(store=store, client=client)


def _entity(code, parent, vat, cr, unified=""):
    e = make_entity(code, "full" if parent else "parent", parent, vat=vat, cr=cr, unified=unified,
                    short_address="ABCD1234")
    create_monthly_periods(e, 2026)
    return e


@pytest.fixture
def zw(db, fakes):
    return build_world(fakes)


def build_world(fakes):
    par = _entity("PAR", None, vat="300000000000003", cr="1010000009")
    tst1 = _entity("TST1", par, vat="311111111100003", cr="1010000001", unified="7000000001")
    tst2 = _entity("TST2", par, vat="322222222200003", cr="1010000002")
    users = {}
    for name, role, entities in (
        ("clerk", "receivables_clerk", (tst1, tst2)), ("cfo", "approver", (tst1, tst2)),
        ("accountant", "accountant", (tst1,)), ("viewer", "viewer", (tst1,)),
        ("admin1", "zatca_admin", (tst1,)), ("admin2", "zatca_admin", (tst2,)), ("admin1b", "zatca_admin", (tst1,)),
        ("mp", "managing_partner", (par,)),
    ):
        user = make_user(name)
        for e in entities:
            member(user, e, role)
        users[name] = user
    queue = access.system_user("zatca_queue")
    for e in (par, tst1, tst2):
        member(queue, e, "system_zatca_queue")
    agent = make_agent("zatca_reader")
    role = Role.objects.create(code="agent_zatca_reader", name_en="Agent: ZATCA status reader",
                               name_ar="وكيل قراءة حالة الفواتير", scope="entity", kind="agent")
    RolePermission.objects.create(role=role, codename="zatca.view_status")
    member(agent, tst1, "agent_zatca_reader")
    ns = SimpleNamespace(par=par, tst1=tst1, tst2=tst2, queue=queue, agent=agent, fakes=fakes, **users)
    ns.customers = {}
    for e in (tst1, tst2):
        ns.customers[e.code] = invoicing_services.create_customer(
            e, user=ns.clerk, code="C001", name_en="Test Buyer LLC", name_ar="شركة المشتري التجريبية",
            is_vat_registered=True, vat_number="344444444400003", **ADDRESS)  # not ZATCA's test VAT
        ns.customers[e.code + "-B2C"] = invoicing_services.create_customer(
            e, user=ns.clerk, code="C002", customer_kind="individual", name_en="Walk-in", name_ar="عميل نقدي")
    return ns


def keys(zw, entity, custodian):
    """Super key (once per group), the entity master key and an onboard_renew + revoke delegation."""
    from zatca.models import EntityMasterKey, GroupSuperKey

    if not GroupSuperKey.objects.filter(entity=zw.par).exists():
        services.register_group_super_key(zw.par, user=zw.mp)
    if not EntityMasterKey.objects.filter(entity=entity).exists():
        services.create_entity_master_key(entity, user=zw.mp)
    for scope in ("onboard_renew", "revoke"):
        services.grant_key_delegation(entity, user=zw.mp, custodian=custodian, scope=scope,
                                      openbao_policy_ref=f"zatca-{entity.code.lower()}-{scope}")


def onboard(zw, entity, admin, *, standard=True, simplified=True, code="EGS1", go_live=date(2026, 1, 1)):
    """Full onboarding through the services: profile, keys, EGS unit, CSR, OTP, compliance checks, PCSID."""
    from zatca.models import ZatcaProfile

    keys(zw, entity, admin)
    profile = ZatcaProfile.objects.filter(entity=entity).first() or services.create_profile(
        entity, user=admin, go_live_date=go_live)
    if not profile.is_enabled:
        profile = services.enable_profile(profile, user=admin)
    egs = services.create_egs_unit(entity, user=admin, code=code, name_en=f"{entity.code} EGS", name_ar="وحدة فوترة",
                                   serial_number=f"1-TestERP|2-1.0|3-{entity.code}-{code}", industry="Real estate",
                                   supports_standard=standard, supports_simplified=simplified,
                                   branch_name_en="Test Branch")
    services.generate_csr(egs, user=admin)
    services.enter_otp(egs, "123456", user=admin)
    services.run_compliance_checks(egs, user=admin)
    return services.request_production_csid(egs, user=admin)


def issue(zw, entity, *, simplified=False, lines=(("management_fee", "1", "100.00"),), day=TODAY, **fields):
    """An issued (approved) sales document of ``entity``; the port hands it to zatca after commit."""
    customer = zw.customers[entity.code + ("-B2C" if simplified else "")]
    w = SimpleNamespace(alm=entity, clerk=zw.clerk, customer=customer, cfo=zw.cfo)
    if simplified:
        fields.setdefault("zatca_profile", "simplified")
    doc = invoice(w, lines, customer=customer, user=zw.clerk, issue_date=day, entity=entity, **fields)
    return approve(w, doc, zw.cfo)


def einvoice_of(doc):
    return EInvoice.objects.get(source_document=doc)


def run(zw, now=NOW):
    return services.process_queue(user=zw.queue, now=now)


def at(day, hour=12):
    return datetime.combine(day, time(hour, 0), tzinfo=dt_timezone.utc)


def mutate(monkeypatch, **changes):
    """Change the XML data of the next stamping (fixtures invoicing would refuse to issue). ``changes`` maps
    attribute paths ("buyer.vat_number") to values or callables(data)."""
    original = services._invoice_data

    def patched(einv):
        data = original(einv)
        for path, value in changes.items():
            target, name = data, path
            if "." in path:
                head, name = path.split(".", 1)
                target = getattr(data, head)
            setattr(target, name, value(data) if callable(value) else value)
        return data

    monkeypatch.setattr(services, "_invoice_data", patched)


def plus(td_hours):
    return NOW + timedelta(hours=td_hours)


# --- shared by the review regression tests (zatca/test_review_*.py) -------------------------------------------------
ADMINS1 = ["admin1", "admin1b"]  # the ZATCA admins of TST1 in the fixture world


def no_entry(doc):
    return not _journal_entries().filter(document_type="invoicing.salesdocument", document_id=doc.pk).exists()


def alerts(kind, einv=None, entity=None):
    qs = _alerts().filter(kind=kind)
    if einv is not None:
        qs = qs.filter(einvoice=einv)
    if entity is not None:
        qs = qs.filter(entity=entity)
    return sorted(qs.values_list("recipient__username", flat=True))


def held_codes(einv):
    return set(einv.messages.values_list("code", flat=True))


def xml_text(xml, xpath):
    return _ubl().find_text(xml if isinstance(xml, bytes) else xml.encode(), xpath)


def second_unit(zw, entity, admin, code, **kw):
    egs = services.create_egs_unit(entity, user=admin, code=code, name_en=f"{entity.code} {code}", name_ar="وحدة",
                                   serial_number=f"1-TestERP|2-1.0|3-{entity.code}-{code}", industry="Real estate",
                                   supports_standard=kw.get("standard", True),
                                   supports_simplified=kw.get("simplified", True))
    services.generate_csr(egs, user=admin)
    services.enter_otp(egs, "123457", user=admin)
    services.run_compliance_checks(egs, user=admin)
    return services.request_production_csid(egs, user=admin)


def _journal_entries():
    from ledger.models import JournalEntry

    return JournalEntry.objects


def _alerts():
    from zatca.models import ZatcaAlert

    return ZatcaAlert.objects


def _ubl():
    from zatca import ubl

    return ubl
