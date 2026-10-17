"""Intercompany fixtures: anonymised sample entities ALM (issuer) and LFN (receiver), fixed clock."""
import datetime
from decimal import Decimal

import pytest

from approvals import services as approvals
from approvals.models import ApprovalRequest
from core.models import Entity
from intercompany import services
from intercompany.models import ServiceType
from ledger.models import FiscalPeriod
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts

D = Decimal
TODAY = datetime.date(2026, 12, 5)
#: Anonymised national address (ZATCA structured address).
ADDRESS = dict(building_number="1234", street="Sample Street", district="Sample District", city="Jeddah",
               postal_code="23456", country_code="SA", additional_number="5678")


@pytest.fixture(autouse=True)
def ic_clock(monkeypatch):
    """Billing for November 2026 runs in early December 2026."""
    monkeypatch.setattr(services, "today", lambda: TODAY)
    return TODAY


@pytest.fixture
def alm(alm):
    create_monthly_periods(alm, 2026)
    alm.vat_number = "300000000000003"
    alm.books_in_erp, alm.books_in_erp_from = True, datetime.date(2026, 1, 1)
    for k, v in ADDRESS.items():
        setattr(alm, k, v)
    alm.save()
    return alm


def _entity(code, name_en, name_ar, parent, **kw):
    e = Entity.objects.create(code=code, name_en=name_en, name_ar=name_ar, parent=parent, **{**ADDRESS, **kw})
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2026)
    create_monthly_periods(e, 2027)
    return e


@pytest.fixture
def lfn(alm):
    return _entity("LFN", "Leafan", "ليفان", alm, vat_number="300000000000013", books_in_erp=True,
                   books_in_erp_from=datetime.date(2026, 1, 1))


@pytest.fixture
def lfn_off(alm):
    """A counterparty that does not keep its books in the ERP (D8)."""
    return _entity("LFX", "Leafan (off ERP)", "ليفان خارج النظام", alm, vat_number="300000000000023")


@pytest.fixture
def mp(make_user, alm, lfn):
    return make_user("mp", (alm, "managing_partner"), (lfn, "managing_partner"))


@pytest.fixture
def accountant(make_user, alm):
    return make_user("acc", (alm, "accountant"))


@pytest.fixture
def lfn_acc(make_user, lfn):
    return make_user("lfnacc", (lfn, "accountant"))


@pytest.fixture
def nov(alm):
    return FiscalPeriod.objects.get(entity=alm, name="2026-11")


@pytest.fixture
def fin():
    return ServiceType.objects.get(code="FIN")


def approve(doc, user, **kw):
    """Approve the pending request of a document (the MP's final approval issues/posts, D11)."""
    r = ApprovalRequest.objects.filter(object_id=str(doc.pk), status="pending",
                                       document_type__content_type__model=doc._meta.model_name).get()
    return approvals.act(r, user, "approve", document_version=r.document_version, **kw)


@pytest.fixture
def make_agreement(alm, lfn, accountant, mp, fin):
    """Create, submit and (by default) activate an agreement ALM -> counterparty."""

    def make(pricing="fixed_fee", counterparty=None, activate=True, user=None, **kw):
        user = user or accountant
        service_type = kw.pop("service_type", fin)
        fields = dict(
            description_en="Finance and accounting services", description_ar="خدمات المالية والمحاسبة",
            pricing_method=pricing, vat_treatment="standard", billing_frequency="monthly", payment_terms_days=30,
            start_date=datetime.date(2026, 1, 1),
        )
        if pricing == "fixed_fee":
            fields["fixed_fee"] = D("10000")
        elif pricing == "timesheet":
            fields["hourly_rate"] = D("450")
        fields.update(kw)
        a = services.create_agreement(user, entity=alm, counterparty=counterparty or lfn,
                                      service_type=service_type, **fields)
        if activate:
            approvals.submit(a, user)
            approve(a, mp)
            a.refresh_from_db()
        return a

    return make


@pytest.fixture
def issued_invoice(make_agreement, accountant, mp, alm, nov):
    """T8: the T1 invoice approved by the MP, hence issued."""

    def make(**kw):
        a = make_agreement(**kw)
        services.run_billing(accountant, alm, nov)
        inv = a.invoices.get()
        approvals.submit(inv, accountant)
        approve(inv, mp)
        inv.refresh_from_db()
        return inv

    return make
