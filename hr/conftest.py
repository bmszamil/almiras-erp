"""Fixtures for HR tests (hr.md §8): anonymised entities ALM (parent, SA, SAR), SUB (child, SA), EGY (child, EG,
EGP) and NEW, users per role, fake billing and payroll ports, a fixed clock.

All names and identifiers are made up (R70); ID numbers are generated at run time; amounts are Decimal strings.
"""
from datetime import date
from decimal import Decimal
from itertools import count
from types import SimpleNamespace

import pytest

from core.access.testing import make_agent, make_user, member
from core.models import Entity
from hr import services
from hr.testing import fake_ports
from ledger.services_setup import install_chart_of_accounts

D = Decimal
TODAY = date(2027, 6, 1)
_national = count(1000000101)
_iqama = count(2000000101)


def make_entity(code, consolidation="full", parent=None, country="SA", currency="SAR"):
    e = Entity.objects.create(code=code, name_en=f"{code} Co", name_ar=f"شركة {code}", consolidation=consolidation,
                              parent=parent, country=country, base_currency_id=currency)
    install_chart_of_accounts(e)
    services.setup_entity(e)
    return e


@pytest.fixture
def clock(monkeypatch):
    """The HR clock (services._today); tests move it with clock.set(day)."""
    state = SimpleNamespace(day=TODAY)
    monkeypatch.setattr(services, "_today", lambda: state.day)

    def set_day(day):
        state.day = day

    state.set = set_day
    return state


@pytest.fixture
def ports():
    with fake_ports() as (billing, payroll):
        yield SimpleNamespace(billing=billing, payroll=payroll)


@pytest.fixture
def w(db, clock, ports):
    alm = make_entity("ALM", "parent")
    sub = make_entity("SUB", "full", alm)
    egy = make_entity("EGY", "full", alm, country="EG", currency="EGP")
    users = {}
    for name, role, entity in (
        ("h", "hr_officer", alm), ("h2", "hr_officer", alm), ("p", "payroll_officer", alm), ("c", "approver", alm),
        ("c2", "approver", sub), ("a", "accountant", alm), ("v", "viewer", alm), ("mp", "managing_partner", alm),
        ("hs", "hr_officer", sub), ("he", "hr_officer", egy), ("ce", "approver", egy), ("aa", "access_admin", alm),
    ):
        user = make_user(name)
        member(user, entity, role)
        users[name] = user
    ns = SimpleNamespace(alm=alm, sub=sub, egy=egy, clock=clock, billing=ports.billing, payroll=ports.payroll, **users)
    ns.agent = make_agent("hr_reader")
    ns.fin = services.create_department(alm, user=ns.h, code="FIN", name_en="Finance", name_ar="المالية")
    ns.staff = services.create_grade(alm, user=ns.h, code="G-STAFF", name_en="Staff", name_ar="موظف",
                                     overtime_eligible=True)
    ns.sub_dept = services.create_department(sub, user=ns.hs, code="OPS", name_en="Operations", name_ar="العمليات")
    ns.sub_grade = services.create_grade(sub, user=ns.hs, code="G-STAFF", name_en="Staff", name_ar="موظف",
                                         overtime_eligible=True)
    return ns


def employee(w, entity=None, *, by=None, saudi=True, basis="ksa_employee", hire_date=date(2027, 1, 1), **fields):
    entity = entity or w.alm
    if by is None:
        by = {w.alm.pk: w.h, w.sub.pk: w.hs, w.egy.pk: w.he}[entity.pk]
    values = dict(
        full_name_en="Test Person", full_name_ar="شخص تجريبي", nationality="SA" if saudi else "IN",
        id_type="national_id" if saudi else "iqama",
        id_number=str(next(_national) if saudi else next(_iqama)), date_of_birth=date(1990, 5, 5), gender="male",
        hire_date=hire_date, employment_basis=basis,
    )
    if basis == "remuneration":
        values.update(nationality="EG", id_type="passport", id_number=f"P{next(_iqama)}", country_of_residence="EG")
    if basis == "local_entity_employee":
        values.update(nationality="EG", id_type="passport", id_number=f"P{next(_iqama)}")
    values.update(fields)
    return services.create_employee(entity, user=by, **values)


def contract_fields(w, emp, **fields):
    entity = emp.entity
    dept = {w.alm.pk: w.fin, w.sub.pk: w.sub_dept}.get(entity.pk)
    grade = {w.alm.pk: w.staff, w.sub.pk: w.sub_grade}.get(entity.pk)
    values = dict(
        effective_date=emp.hire_date, start_date=emp.hire_date, contract_type="unlimited" if emp.is_saudi else "fixed",
        end_date=None if emp.is_saudi else date(emp.hire_date.year + 2, emp.hire_date.month, 1),
        calendar_basis="gregorian", calendar_basis_ref="unified contract cl. 14.6", probation_periods=[90], job_title_en="Analyst", job_title_ar="محلل",
        weekly_hours="40", daily_hours="8", annual_leave_days=21, notice_days_employer=60, notice_days_employee=30,
        department=dept, grade=grade, qiwa_contract_ref="QIWA-TEST-1",
    )
    if emp.employment_basis == "remuneration":
        values.update(contract_type="fixed", end_date=date(emp.hire_date.year + 1, 12, 31), probation_periods=[],
                      annual_leave_days=0, department=None, grade=None, currency="USD", qiwa_contract_ref="")
    values.update(fields)
    return values


def contract(w, emp, components=None, *, approve=True, approver=None, drafter=None, **fields):
    drafter = drafter or {w.alm.pk: w.h, w.sub.pk: w.hs, w.egy.pk: w.he}[emp.entity.pk]
    if components is None:
        components = ({"REMUNERATION": "5000.00"} if emp.employment_basis == "remuneration"
                      else {"BASIC": "6000.00", "HOUSING": "1500.00", "TRANSPORT": "500.00"})
    c = services.create_contract(emp, user=drafter, components=components, **contract_fields(w, emp, **fields))
    c = services.submit_contract(c, user=drafter)
    if approve:
        approver = approver or {w.alm.pk: w.c, w.sub.pk: w.c2, w.egy.pk: w.ce}[emp.entity.pk]
        c = services.approve_contract(c, user=approver)
    return c


def hired(w, entity=None, components=None, contract_kwargs=None, **fields):
    """An active employee with an approved contract."""
    emp = employee(w, entity, **fields)
    contract(w, emp, components, **(contract_kwargs or {}))
    emp.refresh_from_db()
    return emp
