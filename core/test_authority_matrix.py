"""access §10: authority matrix, one approval engine, top-authority self-approval, delegation, personal assistants and
the monthly governance report (R69 to R90; AT-73 to AT-96; OQ-42 to OQ-55 defaults).

Fixtures as §10.8: ALM (group parent, top authority ``managing_partner``), SUB (``ceo``). Users: MP (MP on ALM), CH
(``chairman`` on ALM), CA (``ceo`` on ALM, not its top authority), CE (``ceo`` SUB), CF (``approver`` SUB), AA
(``access_admin`` SUB), AC (``accountant`` SUB), PA (``personal_assistant`` SUB), IA (``internal_auditor`` SUB), EA
(``external_auditor`` SUB, time-limited), BA (``business_analyst`` SUB), BR (``board_representative`` SUB), plus an
ALM CFO (AF) and ALM accountant (AL). All names, numbers and amounts are made up.
"""
import dataclasses
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.http import Http404
from django.utils import timezone

from core import services as core_services
from core.access import authority_defaults, catalogue, delegations, engine, matrix, reports
from core.access import services as access
from core.access.authority_models import (
    AccessAlert, ApprovalActivity, ApprovalDecision, ApprovalDelegation, ApprovalRequest, AuthorityMatrixVersion,
    AuthorityRule, GovernanceReport, canonical_hash,
)
from core.access.models import AccessLog, AppendOnlyError, Membership, Role
from core.access.seed import ROLE_SEED
from core.access.testing import make_agent, make_user, member
from core.models import Entity
from invoicing import services as inv
from invoicing.conftest import make_entity
from invoicing.models import PurchaseDocument
from ledger import services as ledger
from ledger.models import ExchangeRate

pytestmark = pytest.mark.django_db
D = Decimal
RIYADH = timezone.get_default_timezone()


def at(*args):
    return timezone.make_aware(datetime(*args), RIYADH)


@pytest.fixture
def clock(monkeypatch):
    """Move the access layer's clock (memberships, grants, matrix dates, delegations, reports)."""
    state = SimpleNamespace(t=None)
    real = access.now
    monkeypatch.setattr(access, "now", lambda: state.t or real())
    state.set = lambda value: setattr(state, "t", value)
    return state


@pytest.fixture
def g(db):
    alm = make_entity("ALM", "parent", vat="300000000000003", cr="1010000001", unified="7000000001")
    sub = make_entity("SUB", "full", alm, vat="311111111100003", cr="1010000002")
    ns = SimpleNamespace(alm=alm, sub=sub)
    for name, role, entity in (
        ("mp", "managing_partner", alm), ("ch", "chairman", alm), ("ca", "ceo", alm), ("af", "approver", alm),
        ("al", "accountant", alm), ("ce", "ceo", sub), ("cf", "approver", sub), ("aa", "access_admin", sub),
        ("ac", "accountant", sub), ("pa", "personal_assistant", sub), ("ia", "internal_auditor", sub),
        ("ba", "business_analyst", sub), ("br", "board_representative", sub),
    ):
        user = make_user(name)
        member(user, entity, role)
        setattr(ns, name, user)
    ns.ea = make_user("ea")
    member(ns.ea, sub, "external_auditor", valid_to=timezone.now() + timedelta(days=330))
    ns.gov = access.system_user("governance")
    for e in (alm, sub):
        member(ns.gov, e, "system_governance")
    ns.vendor = inv.create_vendor(sub, user=ns.ac, vendor_kind="business", code="V001", name_en="Test Supplier",
                                  name_ar="المورد التجريبي", is_vat_registered=True, vat_number="320000000000003")
    ns.alm_vendor = inv.create_vendor(alm, user=ns.al, vendor_kind="business", code="V002", name_en="Group Supplier",
                                      name_ar="مورد المجموعة", is_vat_registered=True, vat_number="321000000000003")
    return ns


_seq = iter(range(1, 10_000))


def bill(g, price, *, entity=None, user=None, submitter=None, vendor=None, currency=None, on=date(2027, 3, 15),
         submit=True):
    """A bill of one 5300 line at ``price`` (15% VAT); vendor totals = the computed ones."""
    entity = entity or g.sub
    user = user or g.ac
    vendor = vendor or (g.vendor if entity == g.sub else g.alm_vendor)
    doc = inv.create_purchase_document(entity, user=user, vendor=vendor, vendor_document_number=f"B-{next(_seq)}",
                                       vendor_document_date=on, accounting_date=on, currency=currency)
    inv.add_purchase_line(doc, user=user, account="5300", quantity="1", unit_price=str(price), description_ar="بند")
    doc.refresh_from_db()
    _, net, charged, _, _, _, _ = inv._purchase_amounts(doc, list(doc.lines.select_related("vat_rate")))
    inv.update_purchase_document(doc, user=user, vendor_total_vat=charged, vendor_total_gross=net + charged)
    if not submit:
        return PurchaseDocument.objects.get(pk=doc.pk)
    return inv.submit_purchase_document(doc, user=submitter or user)


def payment(g, amount="1000.00", user=None):
    p = inv.create_vendor_payment(g.sub, user=user or g.ac, vendor=g.vendor, date=date(2027, 3, 20), amount=amount)
    return inv.submit_vendor_payment(p, user=user or g.ac)


def approved_matrix(g, entity, rows, *, proposer=None, decider=None, effective=None):
    """Draft, submit and approve a matrix version with ``rows`` (dicts for matrix.add_rule)."""
    proposer = proposer or (g.aa if entity == g.sub else g.mp)
    decider = decider or (g.ce if entity == g.sub else g.ch)
    today = timezone.localdate(access.now())
    v = matrix.draft_version(entity, user=proposer, effective_from=effective or today, summary_en="Test matrix",
                             summary_ar="مصفوفة اختبار", copy_current=False)
    for row in rows:
        matrix.add_rule(v, user=proposer, **row)
    matrix.submit_version(v, user=proposer)
    return matrix.decide_version(v, user=decider, approve=True)


def codes(error):
    """Error codes of a model ValidationError (``full_clean`` collects ``clean`` errors under ``__all__``)."""
    items = [e for errs in error.error_dict.values() for e in errs] if hasattr(error, "error_dict") else error.error_list
    return [e.code for e in items]


def denied(user, code=None, **filters):
    qs = AccessLog.objects.filter(event="permission_denied", actor=user, **filters)
    return [r for r in qs if code is None or (r.details or {}).get("reason") == code]


def rate(g, currency, value, on=date(2027, 3, 15)):
    return ExchangeRate.objects.create(entity=None, from_currency_id=currency, to_currency_id="SAR", date=on,
                                       rate=D(value), source="sama", source_reference="test bulletin",
                                       rate_type="spot", created_by=g.mp)


# --- matrix rows (R69, R71, R72, R73) ----------------------------------------------------------------------------

def test_at73_rows_name_only_people_and_roles_of_the_entity(g):
    v = matrix.draft_version(g.sub, user=g.aa, effective_from=timezone.localdate(), summary_en="v1", summary_ar="١")
    with pytest.raises(access.AccessDenied) as exc:  # ALM's CFO: no SUB membership (R69)
        matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_user=g.af)
    assert exc.value.code == "not_member_of_entity" and exc.value.message_ar
    row = matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_roles=["approver"])
    assert row.entity == g.sub and set(row.approver_roles.values_list("code", flat=True)) == {"approver"}
    with pytest.raises(access.AccessDenied) as exc:  # a group role cannot be held in SUB
        matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_roles=["managing_partner"], step_no=2)
    assert exc.value.code == "role_not_for_entity"
    with pytest.raises(access.AccessDenied) as exc:  # R72: roles or one named user, never both
        matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_roles=["ceo"], approver_user=g.cf, step_no=2)
    assert exc.value.code == "one_approver_kind"
    assert AccessLog.objects.filter(event="authority_matrix_row_added", entity=g.sub).count() == 1


def test_r71_bands_of_one_activity_never_overlap_but_steps_share_a_band(g):
    v = matrix.draft_version(g.sub, user=g.aa, effective_from=timezone.localdate(), summary_en="v1", summary_ar="١")
    matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_roles=["approver"], amount_to="50000.00")
    matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_roles=["ceo"], amount_to="50000.00", step_no=2)
    with pytest.raises(access.AccessDenied) as exc:
        matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_roles=["ceo"], amount_from="40000.00")
    assert exc.value.code == "band_overlap"
    matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_roles=["ceo"], amount_from="50000.01")


def test_r69_only_the_top_authority_or_access_admin_drafts(g):  # OQ-43: not the CFO
    for user in (g.cf, g.ac):
        with pytest.raises(access.AccessDenied):
            matrix.draft_version(g.sub, user=user, effective_from=timezone.localdate(), summary_en="x", summary_ar="س")
    with pytest.raises(access.AccessDenied):  # a ceo of ALM is not its top authority (10.7)
        matrix.draft_version(g.alm, user=g.ca, effective_from=timezone.localdate(), summary_en="x", summary_ar="س")
    assert matrix.draft_version(g.sub, user=g.ce, effective_from=timezone.localdate(), summary_en="x", summary_ar="س")


# --- code defaults (R70) ---------------------------------------------------------------------------------------

def test_at74_no_row_means_the_code_default_and_is_recorded(g):
    b = bill(g, "1000.00")
    req = ApprovalRequest.objects.get(document_type="invoicing.purchasedocument", document_id=b.pk)
    assert (req.rule_source, req.status, req.matrix_version) == ("default", "open", None)
    step = req.steps[0]
    assert engine.eligible_users(g.sub, "invoicing.approve_bill", step, "default", "invoicing.bill",
                                 access.now()) == {g.cf.pk, g.ce.pk}  # SUB's approver holders and its ceo (OQ-41)
    with pytest.raises(access.AccessDenied) as exc:  # no cascade: the MP has no SUB membership (R64, R70)
        inv.approve_purchase_document(b, user=g.mp)
    assert exc.value.code == "no_membership"
    posted = inv.approve_purchase_document(b, user=g.cf)
    assert posted.status == "posted"
    d = ApprovalDecision.objects.get(request=req)
    assert (d.rule_source, d.user, d.decision) == ("default", g.cf, "approved")
    month = reports._build_content(g.sub,  # private since the review of 92c35c2 (item 11)
                                    date(timezone.localdate().year, timezone.localdate().month, 1))
    listed = month["sections"]["default_rule_approvals"]
    assert [x["document"] for x in listed] == [["invoicing.purchasedocument", b.pk]]


def test_r70_every_approval_permission_has_an_activity_and_a_code_default():
    for codename, perm in catalogue.CATALOGUE.items():
        if perm.is_approval:
            acts = authority_defaults.for_permission(codename)
            assert acts, codename
            assert all(isinstance(a.default, authority_defaults.Default) for a in acts)
    assert authority_defaults.get("payroll.run").default.roles == ("hr_officer",)  # never the MP (A2)
    assert authority_defaults.get("payroll.loan").default.top_authority  # OQ-39
    assert set(ApprovalActivity.objects.values_list("code", flat=True)) == set(authority_defaults.ACTIVITIES)


def test_at75_a_gap_inside_the_bands_is_refused_and_the_bill_stays_draft(g):
    approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["approver"], amount_to="50000.00")])
    small = bill(g, "1000.00")
    assert ApprovalRequest.objects.get(document_id=small.pk, document_type="invoicing.purchasedocument").rule_source \
        == "matrix"
    draft = bill(g, "60000.00", submit=False)
    with pytest.raises(access.AccessDenied) as exc:
        inv.submit_purchase_document(draft, user=g.ac)
    assert exc.value.code == "no_authority_rule"
    assert "SUB" in exc.value.message_en and "69000.00" in exc.value.message_en and exc.value.message_ar
    assert PurchaseDocument.objects.get(pk=draft.pk).status == "draft"  # the code default does not fill a gap


# --- named user, delegation flag, one person per request (R72, R74, R75) ---------------------------------------

def test_at76_a_named_user_who_lost_the_membership_leaves_the_step_waiting(g):
    approved_matrix(g, g.sub, [dict(activity="invoicing.vendor_payment", approver_user=g.cf)])
    m = Membership.objects.get(user=g.cf, entity=g.sub, role__code="approver")
    access.revoke_membership(g.ce, m, "left the company")
    p = payment(g)
    req = ApprovalRequest.objects.get(document_type="invoicing.vendorpayment", document_id=p.pk)
    assert req.status == "open"
    with pytest.raises(access.AccessDenied) as exc:  # nobody is substituted (R18)
        inv.approve_vendor_payment(p, user=g.ce)
    assert exc.value.code == "not_in_matrix"
    alerted = set(AccessAlert.objects.filter(kind="no_eligible_approver", entity=g.sub).values_list("recipient", flat=True))
    assert alerted == {g.ce.pk, g.aa.pk}
    assert all(a.message_ar for a in AccessAlert.objects.all())


def test_at78_a_step_without_delegation_is_decided_in_person(g):
    approved_matrix(g, g.sub, [dict(activity="invoicing.vendor_payment", approver_roles=["approver"],
                                    delegation_allowed=False)])
    delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.ac, valid_to=access.now() + timedelta(days=30),
                                   reason_en="Leave", reason_ar="إجازة", activities=["invoicing.vendor_payment"])
    p = payment(g, user=make_member(g, "ap2", "payables_clerk"))
    with pytest.raises(access.AccessDenied) as exc:
        inv.approve_vendor_payment(p, user=g.ac)
    assert exc.value.code == "delegation_not_allowed"
    assert inv.approve_vendor_payment(p, user=g.cf).status == "posted"


def make_member(g, name, role, entity=None):
    user = make_user(name)
    member(user, entity or g.sub, role)
    return user


def test_at79_one_person_decides_one_step_only(g):
    member(g.cf, g.sub, "ceo")
    approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["approver"]),
                               dict(activity="invoicing.bill", approver_roles=["ceo"], step_no=2)])
    b = bill(g, "1000.00")
    first = engine.decide(b, g.cf)
    assert first.final is False and PurchaseDocument.objects.get(pk=b.pk).status == "submitted"
    with pytest.raises(access.AccessDenied) as exc:
        engine.decide(b, g.cf)
    assert exc.value.code == "same_person_two_steps"
    with pytest.raises(access.AccessDenied) as exc:  # R61: a module call that would skip a step is refused
        inv.approve_purchase_document(b, user=g.cf)
    assert exc.value.code in ("same_person_two_steps", "approval_request_required")
    done = engine.decide(b, g.ce)
    assert done.final and PurchaseDocument.objects.get(pk=b.pk).status == "posted"
    assert list(ApprovalDecision.objects.filter(request=done.request).values_list("step_no", "user")) == [
        (1, g.cf.pk), (2, g.ce.pk)]


def test_r61_a_direct_module_call_on_a_two_step_document_is_refused(g):
    approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["approver"]),
                               dict(activity="invoicing.bill", approver_roles=["ceo"], step_no=2)])
    b = bill(g, "1000.00")
    with pytest.raises(access.AccessDenied) as exc:
        inv.approve_purchase_document(b, user=g.cf)
    assert exc.value.code == "approval_request_required" and exc.value.message_ar
    assert PurchaseDocument.objects.get(pk=b.pk).status == "submitted"


# --- band currency (R73) -------------------------------------------------------------------------------------------

def test_at77_band_currency_rows_apply_to_that_currency_only(g):
    rate(g, "USD", "3.7500")
    rate(g, "EUR", "4.0000")
    approved_matrix(g, g.sub, [
        dict(activity="invoicing.bill", approver_roles=["approver"], amount_to="50000.00"),
        dict(activity="invoicing.bill", approver_roles=["ceo"], band_currency="USD", amount_to="10000.000"),
    ])
    usd = bill(g, "7826.09", currency="USD")  # USD 9,000.00 gross
    req = ApprovalRequest.objects.get(document_type="invoicing.purchasedocument", document_id=usd.pk)
    rule = AuthorityRule.objects.get(pk=req.rule_ids[0])
    assert rule.band_currency_id == "USD"
    with pytest.raises(access.AccessDenied):
        access.require(g.cf, g.sub, "invoicing.approve_bill", document=usd)
    assert access.require(g.ce, g.sub, "invoicing.approve_bill", document=usd)
    with pytest.raises(access.AccessDenied) as exc:  # a gap inside the USD bands; SAR rows are not used
        bill(g, "10434.79", currency="USD")  # USD 12,000.01
    assert exc.value.code == "no_authority_rule"
    eur = bill(g, "8695.65", currency="EUR")  # EUR 10,000.00 = SAR 40,000.00: the functional-currency rows
    req = ApprovalRequest.objects.get(document_type="invoicing.purchasedocument", document_id=eur.pk)
    assert req.functional_amount == D("40000.00") and AuthorityRule.objects.get(pk=req.rule_ids[0]).band_currency is None
    assert access.require(g.cf, g.sub, "invoicing.approve_bill", document=eur)
    v = matrix.draft_version(g.sub, user=g.aa, effective_from=timezone.localdate(), summary_en="v", summary_ar="ن")
    with pytest.raises(access.AccessDenied) as exc:  # USD has 2 decimals; storage 3 (CLAUDE.md rule 7)
        matrix.add_rule(v, user=g.aa, activity="invoicing.vendor_payment", approver_roles=["ceo"], band_currency="USD",
                        amount_to="1.0005")
    assert exc.value.code == "amount_decimals"


# --- matrix versions under dual control (R76, R77, R78) ----------------------------------------------------------

def test_at80_dual_control_on_every_matrix_change_is_logged(g):
    v = matrix.draft_version(g.sub, user=g.aa, effective_from=timezone.localdate(), summary_en="v1", summary_ar="١")
    matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_roles=["approver"])
    v = matrix.submit_version(v, user=g.aa)
    req = v.dual_control_request
    assert req.permission == "core.approve_authority_matrix" and req.payload_hash == v.rows_hash == matrix.rows_hash(v)
    with pytest.raises(access.AccessDenied) as exc:
        matrix.decide_version(v, user=g.aa, approve=True)
    assert exc.value.code == "self_approval"
    with pytest.raises(access.AccessDenied) as exc:
        matrix.decide_version(v, user=g.cf, approve=True)
    assert exc.value.code == "no_membership"
    v = matrix.decide_version(v, user=g.ce, approve=True)
    assert (v.status, v.decided_by) == ("approved", g.ce)
    events = list(AccessLog.objects.filter(entity=g.sub, event__startswith="authority_matrix").values_list("event",
                                                                                                         flat=True))
    assert events == ["authority_matrix_drafted", "authority_matrix_row_added", "authority_matrix_submitted",
                      "authority_matrix_approved", "authority_matrix_effective"]
    sub_row = AccessLog.objects.get(event="authority_matrix_submitted")
    assert sub_row.details["dual_control_request"] == req.pk and sub_row.details["diff"]["added"][0]["activity"] == \
        "invoicing.bill"
    with pytest.raises(AppendOnlyError):  # rows of an approved version never change (R77)
        r = AuthorityRule.objects.get(version=v)
        r.min_approvals = 2
        r.save()


def test_at81_the_top_authority_s_own_change_goes_to_the_board_or_the_chairman(g):
    v3 = matrix.draft_version(g.sub, user=g.ce, effective_from=timezone.localdate(), summary_en="v", summary_ar="ن")
    matrix.submit_version(v3, user=g.ce)
    with pytest.raises(access.AccessDenied) as exc:
        matrix.decide_version(v3, user=g.ce, approve=True)
    assert exc.value.code == "self_approval"
    assert matrix.decider_roles(g.sub, g.ce) == {"board_representative"}
    assert matrix.decide_version(v3, user=g.br, approve=True).status == "approved"
    alm = matrix.draft_version(g.alm, user=g.mp, effective_from=timezone.localdate(), summary_en="v", summary_ar="ن")
    matrix.submit_version(alm, user=g.mp)
    with pytest.raises(access.AccessDenied):
        matrix.decide_version(alm, user=g.mp, approve=True)
    assert matrix.decide_version(alm, user=g.ch, approve=True).status == "approved"


def test_r76_someone_else_s_proposal_is_decided_by_the_top_authority_only(g):
    v = matrix.draft_version(g.sub, user=g.aa, effective_from=timezone.localdate(), summary_en="v", summary_ar="ن")
    matrix.submit_version(v, user=g.aa)
    with pytest.raises(access.AccessDenied) as exc:  # BR decides only what the top authority proposed (OQ-42)
        matrix.decide_version(v, user=g.br, approve=True)
    assert exc.value.code == "decider_role" and exc.value.message_ar
    rejected = matrix.decide_version(v, user=g.ce, approve=False, note="not now")
    assert rejected.status == "rejected" and engine.effective_version(g.sub, timezone.localdate()) is None
    assert AccessLog.objects.filter(event="authority_matrix_rejected", entity=g.sub).exists()


def test_at82_versions_are_never_retroactive_and_open_requests_keep_their_rules(g, clock):
    clock.set(at(2027, 2, 20, 10))
    v2 = matrix.draft_version(g.sub, user=g.aa, effective_from=date(2027, 3, 1), summary_en="v2", summary_ar="٢",
                              copy_current=False)
    matrix.add_rule(v2, user=g.aa, activity="invoicing.bill", approver_roles=["approver"])
    matrix.submit_version(v2, user=g.aa)
    clock.set(at(2027, 3, 10, 10))
    with pytest.raises(access.AccessDenied) as exc:  # approved 03-10, effective 03-01: backdated
        matrix.decide_version(v2, user=g.ce, approve=True)
    assert exc.value.code == "effective_from_backdated"
    matrix.withdraw_version(v2, user=g.aa, reason="backdated")
    v2b = approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["approver"])],
                          effective=date(2027, 3, 12))
    clock.set(at(2027, 3, 20, 10))
    v3 = approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["ceo"])],
                         effective=date(2027, 4, 1))
    rule = AuthorityRule.objects.get(version=v3)
    with pytest.raises(AppendOnlyError):
        rule.amount_to = D("10.00")
        rule._service_write = True
        rule.save()
    if connection.vendor == "postgresql":  # R77: also raw SQL
        with pytest.raises(Exception), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("UPDATE core_authorityrule SET min_approvals = 2 WHERE id = %s", [rule.pk])
    clock.set(at(2027, 3, 30, 10))
    b = bill(g, "1000.00")
    req = ApprovalRequest.objects.get(document_type="invoicing.purchasedocument", document_id=b.pk)
    assert req.matrix_version == v2b
    clock.set(at(2027, 4, 2, 10))
    assert engine.effective_version(g.sub, date(2027, 4, 2)) == v3
    with pytest.raises(access.AccessDenied):  # v3 names the ceo, but the bill keeps v2's rules (OQ-46)
        inv.approve_purchase_document(b, user=g.ce)
    assert inv.approve_purchase_document(b, user=g.cf).status == "posted"
    with pytest.raises(access.AccessUsageError):  # review of 92c35c2, item 12: the job is the system user's only
        matrix.activate_due_versions(g.mp, today=date(2027, 4, 2))
    matrix.activate_due_versions(access.system_user("governance"), today=date(2027, 4, 2))
    assert AuthorityMatrixVersion.objects.get(pk=v2b.pk).status == "superseded"
    assert AuthorityMatrixVersion.objects.get(pk=v2b.pk).superseded_by == v3


# --- self-approval (R79 to R83) --------------------------------------------------------------------------------------

def ceo_bills_matrix(g):
    approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["ceo", "approver"],
                                    amount_to="50000.00")])


def test_at83_the_top_authority_self_approves_and_it_is_logged(g):
    ceo_bills_matrix(g)
    b = bill(g, "26086.96", user=g.ce)  # SAR 30,000.00
    posted = inv.approve_purchase_document(b, user=g.ce)
    assert posted.status == "posted" and posted.journal_entry.posted_by == g.ce
    grant_row = AccessLog.objects.filter(event="permission_granted", actor=g.ce, permission="invoicing.approve_bill").get()
    assert grant_row.details["self_approval"] is True and grant_row.details["top_authority"] is True
    row = AccessLog.objects.get(event="self_approval", actor=g.ce)
    d = row.details
    assert (row.entity, row.role.code) == (g.sub, "ceo")
    assert d["activity"] == "invoicing.bill" and d["document"] == ["invoicing.purchasedocument", b.pk]
    assert d["amount"] == "30000.00" and d["currency"] == "SAR" and d["matrix_version"] and d["rule_id"]
    assert d["document_version"] and d["grant_log"] == grant_row.pk and row.at


def test_at84_nobody_else_self_approves(g):
    ceo_bills_matrix(g)
    member(g.cf, g.sub, "accountant")
    own_cf = bill(g, "1000.00", user=g.cf)
    with pytest.raises(access.AccessDenied) as exc:  # not the top authority
        inv.approve_purchase_document(own_cf, user=g.cf)
    assert exc.value.code == "self_approval"
    delegations.delegate_authority(g.ce, entity=g.sub, delegate=g.ac, valid_to=access.now() + timedelta(days=10),
                                   reason_en="Travel", reason_ar="سفر")
    own_ac = bill(g, "1000.00", user=g.ac)
    with pytest.raises(access.AccessDenied) as exc:  # a delegate never self-approves (R62, R79)
        inv.approve_purchase_document(own_ac, user=g.ac)
    assert exc.value.code == "self_approval"
    ca_bill = bill(g, "1000.00", entity=g.alm, user=g.ca)
    with pytest.raises(access.AccessDenied) as exc:  # ALM's top authority is the MP, not its ceo
        inv.approve_purchase_document(ca_bill, user=g.ca)
    assert exc.value.code == "self_approval"
    ce2 = make_member(g, "ce2", "ceo")
    own_ce = bill(g, "1000.00", user=g.ce)
    with pytest.raises(access.AccessDenied) as exc:  # two holders of the top-authority role (OQ-47)
        inv.approve_purchase_document(own_ce, user=g.ce)
    assert exc.value.code == "self_approval"
    access.revoke_membership(g.ce, Membership.objects.get(user=ce2, role__code="ceo"), "hand-over done")
    req = matrix.request_governance_change(g.sub, user=g.mp, reason="board decision", self_approval_enabled=False)
    with pytest.raises(access.AccessDenied):  # R76: never its requester
        matrix.decide_governance_change(req, user=g.mp, approve=True)
    with pytest.raises(access.AccessDenied):  # OQ-42 default (review of 49e1abd, item 10): never SUB's own ceo
        matrix.decide_governance_change(req, user=g.ce, approve=True)
    matrix.decide_governance_change(req, user=g.ch, approve=True)  # the group's chairman
    core_services.change_entity_settings(g.sub, user=g.mp, reason="board decision", dual_control_request=req,
                                         self_approval_enabled=False)
    assert engine.governance_of(g.sub).self_approval_enabled is False
    with pytest.raises(access.AccessDenied) as exc:  # switched off for the entity (R79 b)
        inv.approve_purchase_document(own_ce, user=g.ce)
    assert exc.value.code == "self_approval"
    reasons = [r.details.get("why") for r in AccessLog.objects.filter(event="permission_denied",
                                                                      details__reason="self_approval")]
    assert {"not_top_authority", "delegate", "top_authority_not_single", "self_approval_disabled"} <= set(reasons)


def test_at85_nsa_activities_are_never_self_approved(g):
    def doc(label, creator, pk=1):
        return SimpleNamespace(pk=pk, entity=g.sub, _meta=SimpleNamespace(label_lower=label), created_by_id=creator.pk)

    for perm, label in (("tax.approve_return", "tax.vatreturn"), ("tax.approve_payment", "tax.taxpayment"),
                        ("banking.approve_bank_account", "banking.bankaccountchange")):
        with pytest.raises(access.AccessDenied) as exc:
            access.require(g.ce, g.sub, perm, document=doc(label, g.ce))
        assert exc.value.code == "self_approval", perm
    v = matrix.draft_version(g.sub, user=g.ce, effective_from=timezone.localdate(), summary_en="v", summary_ar="ن")
    matrix.submit_version(v, user=g.ce)
    with pytest.raises(access.AccessDenied):
        matrix.decide_version(v, user=g.ce, approve=True)
    amap = SimpleNamespace(pk=7, entity=g.alm, _meta=SimpleNamespace(label_lower="migration.accountmap"),
                           created_by_id=g.mp.pk)
    with pytest.raises(access.AccessDenied) as exc:
        access.require(g.mp, g.alm, "migration.approve_account_map", document=amap)
    assert exc.value.code == "self_approval"
    for row, perms in catalogue.NSA_ROWS.items():  # the code-held exclusion list, rows a to i
        for perm in perms:
            assert catalogue.get(perm).never_self_approved, (row, perm)
    assert not catalogue.get("invoicing.approve_bill").never_self_approved


def test_at86_an_assistant_s_draft_counts_as_the_principal_s(g):
    delegations.assign_assistant(g.ce, entity=g.sub, assistant=g.pa, principal=g.ce,
                                 valid_to=access.now() + timedelta(days=60), reason="office of the CEO")
    ceo_bills_matrix(g)
    draft = bill(g, "1000.00", user=g.pa, submit=False)
    with pytest.raises(access.AccessDenied):  # the assistant drafts only (R60)
        inv.submit_purchase_document(draft, user=g.pa)
    b = inv.submit_purchase_document(draft, user=g.ac)
    assert ApprovalRequest.objects.get(document_type="invoicing.purchasedocument", document_id=b.pk).drafted_for == g.ce
    posted = inv.approve_purchase_document(b, user=g.ce)  # top authority: a logged self-approval
    assert posted.status == "posted"
    assert AccessLog.objects.get(event="self_approval", actor=g.ce).details["drafted_for"] == g.ce.pk
    other = inv.submit_purchase_document(bill(g, "500.00", user=g.pa, submit=False), user=g.ac)
    assert inv.approve_purchase_document(other, user=g.cf).status == "posted"  # not a self-approval for the CFO


def test_r81_for_any_other_principal_an_assistant_draft_is_refused(g):
    delegations.assign_assistant(g.cf, entity=g.sub, assistant=g.pa, principal=g.cf,
                                 valid_to=access.now() + timedelta(days=60), reason="CFO office")
    b = inv.submit_purchase_document(bill(g, "1000.00", user=g.pa, submit=False), user=g.ac)
    with pytest.raises(access.AccessDenied) as exc:
        inv.approve_purchase_document(b, user=g.cf)
    assert exc.value.code == "self_approval"


def test_r82_nobody_approves_their_own_benefit(g):
    beneficiary = SimpleNamespace(pk=5, entity=g.sub, _meta=SimpleNamespace(label_lower="hr.leaverequest"),
                                  created_by_id=g.ac.pk, employee=SimpleNamespace(user_id=g.ce.pk))
    with pytest.raises(access.AccessDenied) as exc:
        access.require(g.ce, g.sub, "hr.approve_leave", document=beneficiary)
    assert exc.value.code == "beneficiary" and exc.value.message_ar


# --- delegation (R62, R87, R88) -----------------------------------------------------------------------------------

def test_at91_delegations_always_expire_and_are_reminded(g, clock):
    clock.set(at(2027, 3, 1, 8))
    with pytest.raises(access.AccessDenied) as exc:  # over 90 days (OQ-45)
        delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.ac, valid_from=at(2027, 3, 1, 8),
                                       valid_to=at(2027, 6, 15, 8), reason_en="Leave", reason_ar="إجازة",
                                       activities=["invoicing.bill"])
    assert exc.value.code == "delegation_too_long"
    with pytest.raises(ValidationError):
        delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.ac, valid_to=None, reason_en="x", reason_ar="س")
    d = delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.ac, valid_from=at(2027, 3, 1, 8),
                                       valid_to=at(2027, 5, 29, 8), reason_en="Leave", reason_ar="إجازة",
                                       activities=["invoicing.bill"])
    clock.set(at(2027, 5, 26, 9))
    sent = delegations.send_delegation_reminders()
    assert sent == [d] and set(AccessAlert.objects.filter(kind="delegation_expiring").values_list(
        "recipient", flat=True)) == {g.cf.pk, g.ac.pk}
    assert all("2027-05-29" in a.message_en and a.message_ar for a in AccessAlert.objects.all())
    assert delegations.send_delegation_reminders() == []  # once
    clock.set(at(2027, 5, 30, 9))
    b = bill(g, "1000.00", user=make_member(g, "ap3", "payables_clerk"))
    with pytest.raises(access.AccessDenied):  # expired: no job needed
        inv.approve_purchase_document(b, user=g.ac)


def test_at66_r62_a_delegate_acts_within_the_delegator_s_authority(g):
    clerk = make_member(g, "ap4", "payables_clerk")
    d = delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.ac, valid_to=access.now() + timedelta(days=14),
                                       reason_en="Leave", reason_ar="إجازة", activities=["invoicing.bill"],
                                       amount_max=D("10000.00"))
    assert AccessLog.objects.filter(event="delegation_granted", details__approval_delegation=d.pk).exists()
    small = bill(g, "4347.83", user=clerk)  # 5,000.00
    posted = inv.approve_purchase_document(small, user=g.ac)
    assert posted.status == "posted"
    dec = ApprovalDecision.objects.get(request__document_id=small.pk)
    assert (dec.user, dec.on_behalf_of, dec.delegation) == (g.ac, g.cf, d)
    big = bill(g, "17391.31", user=clerk)  # 20,000.00: over amount_max
    with pytest.raises(access.AccessDenied):
        inv.approve_purchase_document(big, user=g.ac)
    with pytest.raises(access.AccessDenied):  # the delegate cannot delegate further
        delegations.delegate_authority(g.ac, entity=g.sub, delegate=clerk, valid_to=access.now() + timedelta(days=5),
                                       reason_en="x", reason_ar="س")
    with pytest.raises(access.AccessDenied):  # never to oneself
        delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.cf, valid_to=access.now() + timedelta(days=5),
                                       reason_en="x", reason_ar="س")
    access.revoke_membership(g.ce, Membership.objects.get(user=g.cf, role__code="approver"), "left")
    again = bill(g, "1000.00", user=clerk)
    with pytest.raises(access.AccessDenied):  # R87: the delegator's revocation ends the delegation at once
        inv.approve_purchase_document(again, user=g.ac)


def test_at92_delegation_limits(g):
    soon = access.now() + timedelta(days=10)
    with pytest.raises(access.AccessDenied) as exc:  # an internal auditor only
        delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.ia, valid_to=soon, reason_en="x", reason_ar="س")
    assert exc.value.code == "delegate_not_eligible"
    with pytest.raises(access.AccessDenied) as exc:  # matrix approval is DC / S!: never delegated
        delegations.delegate_authority(g.ce, entity=g.sub, delegate=g.cf, valid_to=soon, reason_en="x", reason_ar="س",
                                       activities=["core.authority_matrix"])
    assert exc.value.code == "activity_not_delegable"
    with pytest.raises(access.AccessDenied) as exc:  # a system user
        delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.gov, valid_to=soon, reason_en="x", reason_ar="س")
    assert exc.value.code == "delegate_not_eligible"
    with pytest.raises(AppendOnlyError):  # delegations change only through the services
        ApprovalDelegation.objects.all().update(amount_max=1)


def test_r62_revocation_by_the_delegator_is_logged(g):
    d = delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.ac, valid_to=access.now() + timedelta(days=10),
                                       reason_en="Leave", reason_ar="إجازة")
    delegations.revoke_delegation(g.cf, d, "back early")
    assert AccessLog.objects.filter(event="delegation_revoked", details__approval_delegation=d.pk).exists()
    with pytest.raises(access.AccessDenied):
        delegations.revoke_delegation(g.ac, d, "x")


# --- governance report (R84 to R86) -----------------------------------------------------------------------------------

def test_at88_the_monthly_report_lists_the_month_s_events(g, clock):
    clock.set(at(2027, 3, 5, 9))
    ceo_bills_matrix(g)
    b = bill(g, "1000.00", user=g.ce)
    inv.approve_purchase_document(b, user=g.ce)  # self-approval
    d = delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.ac, valid_to=at(2027, 4, 30, 9),
                                       reason_en="Leave", reason_ar="إجازة", activities=["invoicing.bill"])
    inv.approve_purchase_document(bill(g, "1000.00", user=make_member(g, "ap5", "payables_clerk")), user=g.ac)
    member(g.cf, g.sub, "accountant")
    with pytest.raises(access.AccessDenied):  # a refused self-approval
        inv.approve_purchase_document(bill(g, "1000.00", user=g.cf), user=g.cf)
    alm_bill = bill(g, "1000.00", entity=g.alm, user=g.al)
    inv.approve_purchase_document(alm_bill, user=g.af)  # ALM has no matrix: a code-default approval
    core_services.change_entity_settings(g.alm, user=g.mp, reason="quarterly", vat_return_period="monthly")
    clock.set(at(2027, 4, 1, 2, 0))
    generated, skipped = reports.run_monthly_reports()
    assert {r.entity for r in generated} == {g.alm, g.sub} and skipped == []
    sub_report = GovernanceReport.objects.get(entity=g.sub, month=date(2027, 3, 1))
    c = sub_report.content
    assert sub_report.revision == 1 and sub_report.content_hash == canonical_hash(c)
    s = c["sections"]
    assert [x["details"]["document"] for x in s["self_approvals"]] == [["invoicing.purchasedocument", b.pk]]
    assert [x["delegation"] for x in s["delegations"]["granted"]] == [d.pk]
    assert s["delegations"]["decisions"][0]["on_behalf_of"]["id"] == g.cf.pk
    assert {x["event"] for x in s["authority_matrix"]} >= {"authority_matrix_approved", "authority_matrix_submitted"}
    assert len(s["refused_self_approvals"]) == 1
    assert s["group_actions"] is None and not c["nothing_to_report"]
    assert c["labels"]["self_approvals"]["ar"] and c["title"]["ar"] == "تقرير الحوكمة الشهري"
    alm_c = GovernanceReport.objects.get(entity=g.alm, month=date(2027, 3, 1)).content
    assert alm_c["sections"]["group_actions"] and alm_c["sections"]["default_rule_approvals"]
    csv_bytes = reports.export_report(sub_report, user=g.ia, fmt="csv")
    assert "تقرير الحوكمة الشهري".encode() in csv_bytes and b"self_approvals" in csv_bytes
    pdf = reports.export_report(sub_report, user=g.ia, fmt="pdf")
    assert pdf.startswith(b"%PDF")
    assert reports.run_monthly_reports()[0] == generated  # idempotent: the same reports
    quiet = make_entity("QUI", "full", g.alm)
    member(g.gov, quiet, "system_governance")
    report = reports.generate_report(quiet, date(2027, 3, 1), user=g.gov)
    assert report.content["nothing_to_report"] and report.content["nothing_to_report_text"] == {
        "en": "Nothing to report", "ar": "لا يوجد ما يُبلَّغ عنه"}


def test_r84_the_job_runs_from_02_00_on_the_first_for_the_previous_month(g):
    early = reports.run_monthly_reports(at(2027, 4, 1, 1, 30))[0]
    assert {r.month for r in early} == {date(2027, 2, 1)}
    on_time = reports.run_monthly_reports(at(2027, 4, 1, 2, 0))[0]
    assert {r.month for r in on_time} == {date(2027, 3, 1)}
    with pytest.raises(access.AccessDenied):  # only the governance system user generates
        reports.generate_report(g.sub, date(2027, 3, 1), user=g.ia)


def test_r84_an_entity_without_the_system_user_is_skipped_and_its_top_authority_told(g):
    other = make_entity("OTH", "full", g.alm)
    boss = make_user("boss")
    member(boss, other, "ceo")
    generated, skipped = reports.run_monthly_reports(at(2027, 4, 1, 3))
    assert other in skipped
    assert AccessAlert.objects.filter(kind="report_missing_user", recipient=boss).exists()


def test_at89_readers_and_the_single_review(g, clock):
    report = reports.generate_report(g.sub, date(2027, 3, 1), user=g.gov)
    for reader in (g.ia, g.ea, g.ce):
        assert reports.view_report(report, user=reader) == report.content
        assert AccessLog.objects.filter(event="governance_report_viewed", actor=reader).exists()
    for outsider in (g.ac, g.ba, g.mp):
        with pytest.raises(access.AccessDenied):
            reports.view_report(report, user=outsider)
    with pytest.raises(access.AccessDenied):  # ALM's ceo is not ALM's top authority: not a recipient (OQ-33)
        reports.view_report(reports.generate_report(g.alm, date(2027, 3, 1), user=g.gov), user=g.ca)
    reviewed = reports.review_report(report, user=g.ia, note_en="Reviewed, no findings", note_ar="روجع، لا ملاحظات")
    assert reviewed.reviewed_by == g.ia
    with pytest.raises(ValidationError) as exc:
        reports.review_report(report, user=g.ia, note_en="again", note_ar="مرة أخرى")
    assert exc.value.code == "already_reviewed"
    with pytest.raises(access.AccessDenied):
        reports.review_report(report, user=g.ce, note_en="x", note_ar="س")
    unreviewed = reports.generate_report(g.sub, date(2027, 4, 1), user=g.gov)
    assert reports.send_unreviewed_alerts(unreviewed.generated_at + timedelta(days=29)) == []
    sent = [a for a in reports.send_unreviewed_alerts(unreviewed.generated_at + timedelta(days=30)) if a.entity == g.sub]
    assert [a.recipient for a in sent] == [g.ce] and sent[0].message_ar
    clock.set(timezone.now() + timedelta(days=340))  # the external auditor's membership has ended
    with pytest.raises(access.AccessDenied):
        reports.view_report(report, user=g.ea)


def test_at90_a_report_never_changes_and_a_correction_is_a_new_revision(g):
    report = reports.generate_report(g.sub, date(2027, 3, 1), user=g.gov)
    with pytest.raises(AppendOnlyError):
        report.content = {"forged": True}
        report._service_write = True
        report.save()
    with pytest.raises(AppendOnlyError):
        GovernanceReport.objects.filter(pk=report.pk).update(content={})
    if connection.vendor == "postgresql":
        with pytest.raises(Exception), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("UPDATE core_governancereport SET content = '{}'::jsonb WHERE id = %s", [report.pk])
    again = reports.generate_report(g.sub, date(2027, 3, 1), user=g.gov)
    assert again == report  # no reason: the existing report
    rev2 = reports.generate_report(g.sub, date(2027, 3, 1), user=g.gov, reissue_reason="late event")
    assert (rev2.revision, rev2.replaces, rev2.content["replaces"]) == (2, report, 1)
    assert GovernanceReport.objects.get(pk=report.pk).content_hash == report.content_hash


# --- roles (R89), per entity (R90) and scans (AT-96) -----------------------------------------------------------------

def test_at93_auditors_analysts_and_assistants_never_act(g):
    with pytest.raises(access.AccessDenied):
        inv.create_purchase_document(g.sub, user=g.ia, vendor=g.vendor, vendor_document_number="X",
                                     vendor_document_date=date(2027, 3, 1), accounting_date=date(2027, 3, 1))
    with pytest.raises(access.AccessDenied):
        access.require(g.ea, g.sub, "zatca.export_archive")
    with pytest.raises(ValidationError) as exc:
        access.grant_membership(g.ce, make_user("ea2"), g.sub, Role.objects.get(code="external_auditor"),
                                valid_from=timezone.now(), valid_to=timezone.now() + timedelta(days=400), reason="audit")
    assert "valid_to_too_long" in codes(exc.value)
    with pytest.raises(ValidationError) as exc:
        access.grant_membership(g.ce, make_user("ea3"), g.sub, Role.objects.get(code="external_auditor"),
                                valid_from=timezone.now(), reason="audit")
    assert "valid_to_required" in codes(exc.value)
    with pytest.raises(access.AccessDenied):
        access.require(g.ba, g.sub, "hr.view_personal_data")
    with pytest.raises(ValidationError):  # R11: agents never hold a human role
        member(make_agent("audit_bot"), g.sub, "internal_auditor")
    for code in ("internal_auditor", "external_auditor", "business_analyst", "personal_assistant"):
        perms = [catalogue.get(p) for p in ROLE_SEED[code][5]]
        assert not [p.codename for p in perms if p.is_approval or p.posts or p.is_dual_control or p.is_segregated], code
    for code in ("internal_auditor", "external_auditor", "business_analyst"):
        assert all(p.is_view or p.codename == "core.review_governance_report" for p in
                   (catalogue.get(c) for c in ROLE_SEED[code][5])), code
    assert not set(ROLE_SEED["external_auditor"][5]) & {"zatca.export_archive", "hr.view_personal_data",
                                                        "invoicing.view_personal_data", "banking.view_iban"}
    assert set(ROLE_SEED["personal_assistant"][5]) == {"invoicing.create_bill", "billing.edit_contract_draft"}
    assert {"invoicing.create_bill", "invoicing.submit_bill", "invoicing.approve_bill", "ledger.post_entry"} <= set(
        ROLE_SEED["ceo"][5])
    assert set(ROLE_SEED["board_representative"][5]) == {"core.approve_authority_matrix", "core.view_governance_report"}


def test_at94_a_group_parent_membership_sees_nothing_of_another_entity(g):
    approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["approver"])])
    delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.ac, valid_to=access.now() + timedelta(days=5),
                                   reason_en="x", reason_ar="س")
    report = reports.generate_report(g.sub, date(2027, 3, 1), user=g.gov)
    v = AuthorityMatrixVersion.objects.get(entity=g.sub)
    assert not matrix.versions(g.mp).filter(entity=g.sub).exists()
    assert not delegations.delegations(g.mp).filter(entity=g.sub).exists()
    assert not reports.reports(g.mp).filter(entity=g.sub).exists()
    for fn, pk in ((matrix.get_version, v.pk), (reports.get_report, report.pk),
                   (delegations.get_delegation, ApprovalDelegation.objects.get().pk)):
        with pytest.raises(Http404):
            fn(g.mp, pk)
    assert matrix.versions(g.ce).filter(entity=g.sub).exists() and reports.reports(g.ia).filter(pk=report.pk).exists()
    with pytest.raises(access.AccessDenied):  # no cascade of §10 rights (R90)
        access.require(g.mp, g.sub, "core.propose_authority_matrix")


def test_at95_an_intercompany_bill_is_approved_under_its_own_entity_s_rules(g):
    approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["ceo"])])
    approved_matrix(g, g.alm, [dict(activity="invoicing.bill", approver_roles=["approver"])])
    ic_vendor = inv.create_vendor(g.alm, user=g.al, vendor_kind="business", code="V-SUB", name_en="SUB as vendor",
                                  name_ar="الشركة التابعة كمورد", is_vat_registered=True, vat_number="311111111100003",
                                  group_entity=g.sub)
    b = bill(g, "1000.00", entity=g.alm, user=g.al, vendor=ic_vendor)
    req = ApprovalRequest.objects.get(document_type="invoicing.purchasedocument", document_id=b.pk)
    assert req.entity == g.alm and req.matrix_version.entity == g.alm
    with pytest.raises(access.AccessDenied):  # SUB's ceo has nothing in ALM
        inv.approve_purchase_document(b, user=g.ce)
    mp_bill = bill(g, "1000.00", entity=g.alm, user=make_member(g, "alm_ap", "payables_clerk", g.alm),
                   vendor=ic_vendor, submit=False)
    mine = inv.submit_purchase_document(mp_bill, user=Membership.objects.get(user__username="alm_ap").user)
    with pytest.raises(access.AccessDenied) as exc:  # ALM's rows name the approver only: the MP is not eligible
        inv.approve_purchase_document(mine, user=g.mp)
    assert exc.value.code == "not_in_matrix"
    assert inv.approve_purchase_document(b, user=g.af).status == "posted"


def test_at96_every_approval_permission_has_an_activity_an_adapter_and_a_default():
    adapters = engine.adapters()
    covered = {a.permission or authority_defaults.get(a.activity).permission for a in adapters.values()}
    covered |= {p for a in adapters.values() for p in authority_defaults.get(a.activity).permissions}
    # Reviewed: approval permissions with no document of their own yet (no module service calls them).
    no_document_yet = {"payroll.approve_profit_recognition": "bai' ajel profit is posted by the deferred-sale system "
                                                              "user after the payroll approval that collected it (E12)"}
    for codename, perm in catalogue.CATALOGUE.items():
        if not perm.is_approval:
            continue
        assert authority_defaults.for_permission(codename), codename
        assert codename in covered or codename in no_document_yet, f"{codename}: no engine adapter"
    for activity in authority_defaults.ACTIVITIES.values():
        assert activity.default is not None and activity.name_ar, activity.code


def test_r61_require_routes_every_approval_through_the_engine(g):
    stand_in = SimpleNamespace(pk=99, entity=g.sub, _meta=SimpleNamespace(label_lower="invoicing.bill"),
                               created_by_id=g.ac.pk)
    grant = access.require(g.cf, g.sub, "invoicing.approve_bill", document=stand_in)
    req = ApprovalRequest.objects.get(pk=grant.approval_request_id)
    assert (req.activity.code, req.rule_source, grant.rule_source) == ("invoicing.bill", "default", "default")
    assert AccessLog.objects.get(pk=grant.log_id).details["approval_request"] == req.pk


def test_r90_entity_settings_and_the_top_authority_stay_group_level_under_dual_control(g):
    with pytest.raises(access.AccessDenied):  # without an approved four-eyes request
        core_services.change_entity_settings(g.sub, user=g.mp, reason="x", top_authority_role="approver")
    with pytest.raises(access.AccessDenied):  # only the group parent's settings right asks for it (G)
        matrix.request_governance_change(g.sub, user=g.ce, reason="x", top_authority_role="approver")
    req = matrix.request_governance_change(g.sub, user=g.mp, reason="new GM", top_authority_role="approver")
    for wrong in (g.ce, g.br):  # OQ-42 default (review of 49e1abd, item 10): not SUB's ceo nor SUB's own board rep
        with pytest.raises(access.AccessDenied):
            matrix.decide_governance_change(req, user=wrong, approve=True)
    matrix.decide_governance_change(req, user=g.ch, approve=True)
    with pytest.raises(access.AccessDenied):  # exactly the approved change
        core_services.change_entity_settings(g.sub, user=g.mp, reason="other", dual_control_request=req,
                                             top_authority_role="approver")
    core_services.change_entity_settings(g.sub, user=g.mp, reason="new GM", dual_control_request=req,
                                         top_authority_role="approver")
    assert engine.governance_of(g.sub).top_authority_role == "approver"
    assert "approver" in engine.governance_of(g.sub).recipients
    row = AccessLog.objects.filter(event="entity_settings_changed", entity=g.sub).last()
    assert row.details["before"] == {"top_authority_role": "ceo"} and row.details["after"] == {
        "top_authority_role": "approver"}
    with pytest.raises(access.AccessDenied):  # single use
        core_services.change_entity_settings(g.sub, user=g.mp, reason="new GM", dual_control_request=req,
                                             top_authority_role="approver")


def test_r67_new_entities_get_their_top_authority(g):
    new = core_services.create_entity(Entity(code="NEW", name_en="New", name_ar="جديد", parent=g.alm), user=g.mp,
                                      reason="new subsidiary")
    gov = engine.governance_of(new)
    assert (gov.top_authority_role, gov.stored, gov.self_approval_enabled) == ("ceo", True, True)
    assert gov.recipients == {"internal_auditor", "ceo"}
    assert engine.governance_of(g.alm).top_authority_role == "managing_partner"


def test_r68_new_messages_and_labels_are_bilingual():
    for table in (engine.MESSAGES, matrix.MESSAGES, delegations.MESSAGES):
        for code, (en, ar) in table.items():
            assert en and ar and en != ar, code
    for a in authority_defaults.ACTIVITIES.values():
        assert a.name_en and a.name_ar
    for key, en, ar in reports.SECTIONS:
        assert en and ar


def test_r83_r85_append_only_engine_records(g):
    b = bill(g, "1000.00")
    req = ApprovalRequest.objects.get(document_type="invoicing.purchasedocument", document_id=b.pk)
    with pytest.raises(AppendOnlyError):
        ApprovalRequest.objects.filter(pk=req.pk).update(status="approved")
    inv.approve_purchase_document(b, user=g.cf)
    d = ApprovalDecision.objects.get(request=req)
    with pytest.raises(AppendOnlyError):
        d._service_write = True
        d.save()
    with pytest.raises(AppendOnlyError):
        d.delete()
    if connection.vendor == "postgresql":
        with pytest.raises(Exception), transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("DELETE FROM core_approvaldecision WHERE id = %s", [d.pk])
    with pytest.raises(IntegrityError), transaction.atomic():  # a version number is used once per entity
        v = AuthorityMatrixVersion(entity=g.sub, version_no=1, effective_from=date(2027, 1, 1), summary_en="x",
                                   summary_ar="س", proposed_by=g.aa)
        v._service_write = True
        v.save()
        v2 = AuthorityMatrixVersion(entity=g.sub, version_no=1, effective_from=date(2027, 1, 1), summary_en="x",
                                    summary_ar="س", proposed_by=g.aa)
        v2._service_write = True
        v2.save()


# --- review of 92c35c2: the reviewer's probes, as refusal tests ---------------------------------------------------

def test_rv_p1_only_the_proposer_edits_and_submits_a_draft(g):
    """R76 (probe P1): the second person never wrote any of the version."""
    v = matrix.draft_version(g.sub, user=g.aa, effective_from=timezone.localdate(), summary_en="v", summary_ar="ن",
                             copy_current=False)
    with pytest.raises(access.AccessDenied) as exc:
        matrix.add_rule(v, user=g.ce, activity="invoicing.bill", approver_roles=["ceo"])
    assert exc.value.code == "not_proposer"
    rule = matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_roles=["ceo"])
    with pytest.raises(access.AccessDenied) as exc:
        matrix.remove_rule(rule, user=g.ce)
    assert exc.value.code == "not_proposer"
    with pytest.raises(access.AccessDenied) as exc:
        matrix.submit_version(v, user=g.ce)
    assert exc.value.code == "not_proposer"
    matrix.submit_version(v, user=g.aa)
    assert matrix.decide_version(v, user=g.ce, approve=True).status == "approved"


def test_rv_p1b_a_row_author_never_decides_the_version(g):
    """R76 (probe P1b): the author of any row is barred from deciding, whoever submitted."""
    v = matrix.draft_version(g.sub, user=g.aa, effective_from=timezone.localdate(), summary_en="v", summary_ar="ن",
                             copy_current=False)
    rule = matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_roles=["ceo"])
    rule.created_by = g.ce  # as a row written before the proposer-only rule
    rule._service_write = True
    rule.save()
    matrix.submit_version(v, user=g.aa)
    with pytest.raises(access.AccessDenied) as exc:
        matrix.decide_version(v, user=g.ce, approve=True)
    assert exc.value.code == "self_approval"
    assert AuthorityMatrixVersion.objects.get(pk=v.pk).status == "submitted"


def test_rv_p2_a_delegate_never_approves_the_delegator_s_own_document(g):
    """R62, R14 (probe P2): the delegator made the bill, so their delegate cannot approve it either."""
    member(g.cf, g.sub, "accountant")
    own = bill(g, "1000.00", user=g.cf)
    delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.ac, valid_to=access.now() + timedelta(days=5),
                                   reason_en="x", reason_ar="س", activities=["invoicing.bill"])
    with pytest.raises(access.AccessDenied) as exc:
        inv.approve_purchase_document(own, user=g.ac)
    assert exc.value.code == "self_approval"
    assert PurchaseDocument.objects.get(pk=own.pk).status == "submitted"


def _leave_for(g, beneficiary):
    return SimpleNamespace(pk=99, entity=g.sub, _meta=SimpleNamespace(label_lower="hr.leaverequest"),
                           created_by_id=g.ac.pk, employee=SimpleNamespace(user_id=beneficiary.pk))


def test_rv_p3_a_delegate_never_approves_the_delegator_s_own_benefit(g):
    """R62, R82 (probe P3)."""
    delegations.delegate_authority(g.ce, entity=g.sub, delegate=g.cf, valid_to=access.now() + timedelta(days=5),
                                   reason_en="x", reason_ar="س")
    approved_matrix(g, g.sub, [dict(activity="hr.leave", approver_roles=["ceo"])])
    with pytest.raises(access.AccessDenied) as exc:
        access.require(g.cf, g.sub, "hr.approve_leave", document=_leave_for(g, g.ce))
    assert exc.value.code == "beneficiary"
    assert denied(g.cf, "beneficiary")


def test_rv_p4_a_check_grant_is_recorded_as_a_check_and_never_posts(g):
    """Probes P4 and P4b: ``purpose="check"`` checks eligibility only; its grant is not an approval (R31)."""
    approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["approver"], step_no=1),
                               dict(activity="invoicing.bill", approver_roles=["ceo"], step_no=2)])
    b = bill(g, "1000.00", user=make_member(g, "clk", "payables_clerk"))
    with pytest.raises(access.AccessDenied):
        access.require(g.cf, g.sub, "invoicing.approve_bill", document=b)
    grant = access.require(g.cf, g.sub, "invoicing.approve_bill", document=b, purpose="check")
    assert grant.purpose == "check"
    assert AccessLog.objects.get(pk=grant.log_id).details["purpose"] == "check"
    lines = [{"account": "5300", "debit": "10.00"}, {"account": "2100", "credit": "10.00"}]
    with pytest.raises(access.AccessDenied) as exc:
        ledger.create_entry(g.sub, date(2027, 3, 15), "Bill", lines, user=g.ac, post=True, document=b, grant=grant)
    assert exc.value.code == "grant_not_an_approval"
    forged = dataclasses.replace(grant, purpose="approve")  # the log row still says check (R31)
    with pytest.raises(access.AccessDenied):
        ledger.create_entry(g.sub, date(2027, 3, 15), "Bill", lines, user=g.ac, post=True, document=b, grant=forged)
    assert PurchaseDocument.objects.get(pk=b.pk).status == "submitted"


def test_rv_p4_the_beneficiary_rule_applies_to_checks_too(g):
    """R82 (review item 6): a beneficiary cannot even check their own benefit."""
    approved_matrix(g, g.sub, [dict(activity="hr.leave", approver_roles=["ceo"])])
    with pytest.raises(access.AccessDenied) as exc:
        access.require(g.ce, g.sub, "hr.approve_leave", document=_leave_for(g, g.ce), purpose="check")
    assert exc.value.code == "beneficiary"


def test_rv_p6_p7_the_legacy_entry_point_creates_a_limited_delegation(g):
    """R62, R87, R88 (probes P6, P7): ``delegate_approval`` makes an ApprovalDelegation with every limit."""
    clerk = make_member(g, "clk6", "payables_clerk")
    with pytest.raises(access.AccessDenied) as exc:  # at most 90 days
        access.delegate_approval(g.cf, g.ac, g.sub, None, access.now() + timedelta(days=200), "leave", "سبب")
    assert exc.value.code == "delegation_too_long"
    with pytest.raises(access.AccessDenied) as exc:  # never to a personal assistant only
        access.delegate_approval(g.cf, g.pa, g.sub, None, access.now() + timedelta(days=5), "office", "سبب")
    assert exc.value.code == "delegate_not_eligible"
    d = access.delegate_approval(g.cf, g.ac, g.sub, None, access.now() + timedelta(days=5), "leave", "سبب")
    assert isinstance(d, ApprovalDelegation) and d.delegator == g.cf
    assert not Membership.objects.filter(user=g.ac, delegated_from__isnull=False).exists()
    with pytest.raises(access.AccessDenied):  # the delegate cannot pass the authority on: no chain (R88)
        delegations.delegate_authority(g.ac, entity=g.sub, delegate=clerk, valid_to=access.now() + timedelta(days=5),
                                       reason_en="x", reason_ar="س", activities=["invoicing.bill"])
    assert not ApprovalDelegation.objects.filter(delegate=clerk).exists()


def test_rv_p6_a_legacy_delegate_membership_gives_no_approval(g):
    """R62 (probe P6): a ``delegated_from`` membership left from R16 never counts in the engine or delegations."""
    maker = make_member(g, "mk6", "payables_clerk")
    src = Membership.objects.get(user=g.cf, entity=g.sub, role__code="approver")
    legacy = Membership(user=g.ac, entity=g.sub, role=src.role, valid_from=access.now(), delegated_from=src,
                        valid_to=access.now() + timedelta(days=10), granted_by=g.cf, reason="old R16")
    Membership.objects.bulk_create([legacy])  # as left in the database before core.0031 (no clean)
    b = bill(g, "1000.00", user=maker)
    with pytest.raises(access.AccessDenied):
        inv.approve_purchase_document(b, user=g.ac)
    with pytest.raises(access.AccessDenied):
        delegations.delegate_authority(g.ac, entity=g.sub, delegate=maker, valid_to=access.now() + timedelta(days=5),
                                       reason_en="x", reason_ar="س")
    assert PurchaseDocument.objects.get(pk=b.pk).status == "submitted"


def test_rv_p8_ending_the_assignment_does_not_clear_the_principal(g):
    """R81 (probe P8): the principal recorded when the request opened stays a maker."""
    a = delegations.assign_assistant(g.cf, entity=g.sub, assistant=g.pa, principal=g.cf,
                                     valid_to=access.now() + timedelta(days=60), reason="CFO office")
    b = inv.submit_purchase_document(bill(g, "1000.00", user=g.pa, submit=False), user=g.ac)
    req = ApprovalRequest.objects.get(document_id=b.pk, document_type="invoicing.purchasedocument")
    assert req.drafted_for == g.cf
    delegations.end_assistant_assignment(g.cf, a, "done")
    with pytest.raises(access.AccessDenied) as exc:
        inv.approve_purchase_document(b, user=g.cf)
    assert exc.value.code == "self_approval"


def test_rv_r75_a_delegate_and_the_delegator_are_one_person_across_steps(g):
    """R75 (review item 8): a delegate's decision counts for the delegator too."""
    approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["approver"], step_no=1),
                               dict(activity="invoicing.bill", approver_roles=["approver", "ceo"], step_no=2)])
    delegations.delegate_authority(g.cf, entity=g.sub, delegate=g.ac, valid_to=access.now() + timedelta(days=5),
                                   reason_en="x", reason_ar="س")
    b = bill(g, "1000.00", user=make_member(g, "clk8", "payables_clerk"))
    out = engine.decide(b, g.ac)  # step 1 for the CFO, by the delegate
    assert out.final is False
    assert ApprovalDecision.objects.get(request=out.request, step_no=1).on_behalf_of == g.cf
    with pytest.raises(access.AccessDenied) as exc:
        engine.decide(b, g.cf)  # the CFO in person on step 2: the same person
    assert exc.value.code == "same_person_two_steps"
    assert engine.decide(b, g.ce).final


def test_rv_r12_engine_submit_checks_the_submit_right_itself(g):
    """Review item 12: a caller that skipped its own require cannot open a request."""
    draft = bill(g, "1000.00", submit=False)
    with pytest.raises(access.AccessDenied):
        engine.submit(draft, g.ba, submit_permission="invoicing.submit_bill")
    with pytest.raises(access.AccessUsageError):
        engine.submit(draft, g.ac, submit_permission="invoicing.approve_bill")
    assert not ApprovalRequest.objects.filter(document_id=draft.pk, document_type="invoicing.purchasedocument").exists()


def test_rv_r84_group_actions_cover_every_descendant_of_the_group_parent(g):
    """Review item 11: a grandchild's entity settings change is a group-level action in the parent's report."""
    grand = make_entity("GRD", "full", g.sub, vat="312222222200003", cr="1010000009")
    access.log(AccessLog.Event.ENTITY_SETTINGS_CHANGED, actor=g.mp, entity=grand,
               permission="core.change_entity_settings", details={"field": "test"})
    first = timezone.localdate(access.now()).replace(day=1)
    rows = reports._build_content(g.alm, first)["sections"]["group_actions"]
    assert any(r["event"] == "entity_settings_changed" and r["details"] == {"field": "test"} for r in rows)
    assert reports._build_content(g.sub, first)["sections"]["group_actions"] is None


def test_rv_r77_approver_roles_of_a_frozen_version_never_change(g):
    """Review item 13: also on SQLite (m2m_changed); PostgreSQL adds the core.0030 trigger."""
    v = approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["ceo"])])
    rule = v.rules.get()
    approver = Role.objects.get(code="approver")
    for change in (lambda: rule.approver_roles.add(approver), lambda: rule.approver_roles.clear(),
                   lambda: rule.approver_roles.set([approver])):
        with pytest.raises(AppendOnlyError), transaction.atomic():
            change()
    assert list(rule.approver_roles.values_list("code", flat=True)) == ["ceo"]
    draft = matrix.draft_version(g.sub, user=g.aa, effective_from=timezone.localdate(access.now()),
                                 summary_en="v", summary_ar="ن")
    copy = draft.rules.get()
    copy.approver_roles.add(approver)  # a draft's rows still change
    assert set(copy.approver_roles.values_list("code", flat=True)) == {"ceo", "approver"}


def test_rv_oq42_oversight_roles_in_a_subsidiary_come_from_the_group_top_authority(g):
    """Review item 14 (OQ-42 team default): SUB's ceo never grants or revokes the board representative or the
    internal auditor who oversee them; the managing partner of the group parent does."""
    for code in ("board_representative", "internal_auditor"):
        role = Role.objects.get(code=code)
        with pytest.raises(access.AccessDenied) as exc:
            access.grant_membership(g.ce, make_user(f"x_{code}"), g.sub, role, access.now(), reason="oversight")
        assert exc.value.code == "role_not_grantable"
        m = access.grant_membership(g.mp, make_user(f"y_{code}"), g.sub, role, access.now(), reason="oversight")
        assert m.granted_by == g.mp
    with pytest.raises(access.AccessDenied):
        access.revoke_membership(g.ce, Membership.objects.get(user=g.br, entity=g.sub), "removed")
    assert Membership.objects.get(user=g.br, entity=g.sub).revoked_at is None


def test_rv93_n3_the_group_top_authority_grants_oversight_roles_in_an_equity_entity(g):
    """OQ-42 team default (verification review of 49e1abd, item 2, probe N3): in an equity entity the managing
    partner's group membership does not reach, the group parent's top authority still grants and revokes the
    board representative; the entity's own ceo never does."""
    assoc = make_entity("ASC", "equity", g.alm)
    target = make_user("brx")
    member(target, assoc, "viewer")
    role = Role.objects.get(code="board_representative")
    ceo = make_user("ceoasc")
    member(ceo, assoc, "ceo")
    with pytest.raises(access.AccessDenied) as exc:
        access.grant_membership(ceo, target, assoc, role, access.now(), access.now() + timedelta(days=300), "board")
    assert exc.value.code == "role_not_grantable"
    m = access.grant_membership(g.mp, target, assoc, role, access.now(), access.now() + timedelta(days=300), "board")
    assert (m.entity, m.granted_by) == (assoc, g.mp)
    with pytest.raises(access.AccessDenied):
        access.revoke_membership(ceo, m, "removed")
    assert access.revoke_membership(g.mp, m, "term ended").revoked_at is not None
    with pytest.raises(access.AccessDenied):  # an access admin of the group parent is not its top authority
        access.grant_membership(make_member(g, "aa_alm", "access_admin", entity=g.alm), make_user("bry"), assoc, role,
                                access.now(), None, "board")


def _apps_at_0030():
    from django.db.migrations.executor import MigrationExecutor

    return MigrationExecutor(connection).loader.project_state(("core", "0030_authority_matrix_triggers")).apps


def test_rv93_n5_migration_0031_converts_only_live_delegations_within_the_limits(g):
    """Verification review of 49e1abd, item 4 (probe N5): the conversion starts no earlier than now, lasts at most
    90 days, covers only the source role's approval activities, skips a delegate whose source had ended, and logs
    with the system actor kind and the migration source."""
    import importlib

    apps = _apps_at_0030()
    HM = apps.get_model("core", "Membership")
    now = timezone.now()
    approver = apps.get_model("core", "Role").objects.get(code="approver")
    src = HM.objects.get(user_id=g.cf.pk, entity_id=g.sub.pk, role_id=approver.pk)
    long_ago = HM.objects.create(user_id=g.ac.pk, entity_id=g.sub.pk, role_id=approver.pk,
                                 valid_from=now - timedelta(days=200), valid_to=now + timedelta(days=300),
                                 granted_by_id=g.cf.pk, granted_at=now - timedelta(days=200), reason="old leave",
                                 delegated_from_id=src.pk)
    cf2 = make_member(g, "cf2", "approver")
    src2 = HM.objects.get(user_id=cf2.pk, entity_id=g.sub.pk)
    dead = HM.objects.create(user_id=g.ba.pk, entity_id=g.sub.pk, role_id=approver.pk,
                             valid_from=now - timedelta(days=5), valid_to=now + timedelta(days=20), granted_by_id=cf2.pk,
                             granted_at=now - timedelta(days=5), reason="cover", delegated_from_id=src2.pk)
    HM.objects.filter(pk=src2.pk).update(revoked_at=now - timedelta(days=1), revoked_by_id=g.ce.pk, revoke_reason="left")
    importlib.import_module("core.migrations.0031_convert_legacy_delegations").convert(apps, None)
    d = ApprovalDelegation.objects.get(delegate=g.ac)
    assert d.delegator == g.cf and d.valid_from >= now and (d.valid_to - d.valid_from) <= timedelta(days=90)
    codes = set(d.activities.values_list("code", flat=True))
    assert "invoicing.bill" in codes and "payroll.run" not in codes  # the approver role's approvals only
    assert Membership.objects.get(pk=long_ago.pk).revoked_at is not None
    assert not ApprovalDelegation.objects.filter(delegate=g.ba).exists()  # its source had ended: not revived
    assert Membership.objects.get(pk=dead.pk).revoked_at is None
    logs = AccessLog.objects.filter(source="migration:core.0031")
    assert {r.event for r in logs} == {"delegation_granted", "delegation_revoked"}
    assert {r.actor_kind for r in logs} == {"system"} and all(r.actor_id is None for r in logs)


def test_rv93_0029_carries_its_own_frozen_seed():
    """Verification review of 49e1abd, item 7: the migration does not import the live seed; its frozen copy is V18."""
    import importlib
    import inspect

    from core.access.seed import ROLE_SEED_V17, ROLE_SEED_V18

    mod = importlib.import_module("core.migrations.0029_seed_access_v18_authority_matrix")
    source = inspect.getsource(mod)
    assert "import ROLE_SEED" not in source and "import authority_defaults" not in source
    for code, row in ROLE_SEED_V18.items():
        frozen = mod.ROLES_CHANGED.get(code)
        expected = (*row[:5], tuple(sorted(set(row[5]))))
        if frozen is None:
            assert (*ROLE_SEED_V17[code][:5], tuple(sorted(set(ROLE_SEED_V17[code][5])))) == expected, code
        else:
            assert frozen == expected, code
    assert {a[0] for a in mod.ACTIVITIES} == set(authority_defaults.ACTIVITIES)


def test_rv93_n8_only_the_proposer_or_the_top_authority_withdraws_a_version(g):
    """Verification review of 49e1abd, item 5 (probe N8)."""
    aa2 = make_member(g, "aa2", "access_admin")
    v = matrix.draft_version(g.sub, user=g.aa, effective_from=timezone.localdate(), summary_en="v", summary_ar="ن",
                             copy_current=False)
    matrix.add_rule(v, user=g.aa, activity="invoicing.bill", approver_roles=["ceo"])
    matrix.submit_version(v, user=g.aa)
    with pytest.raises(access.AccessDenied) as exc:
        matrix.withdraw_version(v, user=aa2, reason="mine now")
    assert exc.value.code == "not_proposer"
    assert matrix.withdraw_version(v, user=g.ce, reason="board asked").status == "withdrawn"  # SUB's top authority
    v2 = matrix.draft_version(g.sub, user=g.aa, effective_from=timezone.localdate(), summary_en="w", summary_ar="ن",
                              copy_current=False)
    assert matrix.withdraw_version(v2, user=g.aa, reason="not needed").status == "withdrawn"


def test_rv93_r81_an_assistant_s_document_needs_its_creation_time(g):
    """Verification review of 49e1abd, item 6: no silent fall-back to now; adapters supply the time when the
    document has no created_at (a bank reconciliation is dated by prepared_at)."""
    delegations.assign_assistant(g.cf, entity=g.sub, assistant=g.pa, principal=g.cf,
                                 valid_to=access.now() + timedelta(days=60), reason="CFO office")
    undated = SimpleNamespace(pk=5, entity=g.sub, _meta=SimpleNamespace(label_lower="invoicing.purchasedocument"),
                              created_by_id=g.pa.pk)
    with pytest.raises(access.AccessUsageError):
        engine.document_people(g.sub, undated, access.now())
    dated = SimpleNamespace(**vars(undated), created_at=access.now())
    assert g.cf.pk in engine.document_people(g.sub, dated, access.now())
    adapter = engine.adapters()[("banking.reconciliation", "banking.reconciliation")]
    rec = SimpleNamespace(pk=6, entity=g.sub, _meta=SimpleNamespace(label_lower="banking.reconciliation"),
                          prepared_by_id=g.pa.pk, prepared_at=access.now())
    assert adapter.created_at(rec) == rec.prepared_at and g.cf.pk in engine.document_people(g.sub, rec, access.now())
    other = SimpleNamespace(pk=7, entity=g.sub, _meta=SimpleNamespace(label_lower="invoicing.purchasedocument"),
                            created_by_id=g.ac.pk)  # no assistant: no creation time needed
    assert engine.document_people(g.sub, other, access.now()) == {g.ac.pk}


def test_rv93_delegate_approval_takes_both_reasons(g):
    """Verification review of 49e1abd, item 8: the bilingual rule (CLAUDE.md) for the legacy entry point."""
    with pytest.raises(ValidationError):
        access.delegate_approval(g.cf, g.ac, g.sub, None, access.now() + timedelta(days=5), "Leave", "")
    d = access.delegate_approval(g.cf, g.ac, g.sub, None, access.now() + timedelta(days=5), "Leave", "إجازة")
    assert (d.reason_en, d.reason_ar) == ("Leave", "إجازة")


def test_rv93_the_through_table_of_a_frozen_row_refuses_direct_writes(g):
    """Verification review of 49e1abd, item 9."""
    v = approved_matrix(g, g.sub, [dict(activity="invoicing.bill", approver_roles=["ceo"])])
    rule = v.rules.get()
    through = AuthorityRule.approver_roles.through
    if connection.vendor == "postgresql":  # the core.0030 trigger; SQLite has only the m2m guard (spec 10.10)
        with pytest.raises(DatabaseError), transaction.atomic():
            through.objects.create(authorityrule=rule, role=Role.objects.get(code="approver"))
        with pytest.raises(DatabaseError), transaction.atomic():
            through.objects.filter(authorityrule=rule).delete()
    with pytest.raises(AppendOnlyError), transaction.atomic():
        rule.approver_roles.remove(Role.objects.get(code="ceo"))
    assert list(rule.approver_roles.values_list("code", flat=True)) == ["ceo"]


def test_rv93_a_subsidiary_s_governance_change_is_decided_in_the_group_parent(g):
    """OQ-42 team default (verification review of 49e1abd, item 10)."""
    proposer = make_member(g, "settings_admin", "managing_partner", entity=g.alm)
    req = matrix.request_governance_change(g.sub, user=proposer, reason="x", self_approval_enabled=False)
    for wrong in (g.ce, g.br, proposer):
        with pytest.raises(access.AccessDenied):
            matrix.decide_governance_change(req, user=wrong, approve=True)
    assert matrix.decide_governance_change(req, user=g.mp, approve=True).status == "approved"  # the group's MP


def test_rv93_n1_n2_no_chain_and_no_assistant_delegate_within_90_days(g):
    """Probes N1 and N2 of the verification review of 49e1abd, inside the 90-day limit."""
    clerk = make_member(g, "clk_n1", "payables_clerk")
    access.delegate_approval(g.cf, g.ac, g.sub, None, access.now() + timedelta(days=50), "Leave", "إجازة")
    with pytest.raises(access.AccessDenied):  # the delegate holds no delegation right: no chain (R88)
        delegations.delegate_authority(g.ac, entity=g.sub, delegate=clerk, valid_to=access.now() + timedelta(days=5),
                                       reason_en="x", reason_ar="س", activities=["invoicing.bill"])
    with pytest.raises(access.AccessDenied) as exc:  # never to a personal assistant only
        access.delegate_approval(g.cf, g.pa, g.sub, None, access.now() + timedelta(days=50), "Office", "مكتب")
    assert exc.value.code == "delegate_not_eligible"
