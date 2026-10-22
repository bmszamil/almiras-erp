"""One approval engine, catalogue and registry (fixed-assets.md R35 to R38; AT-24, AT-27 to AT-30, AT-38; access R61).
Fictional data; policies are fixture values (OQ-2)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from core import services as core_services
from core.access import authority_defaults, catalogue, delegations, engine, inbox, matrix, reports
from core.access import services as access
from core.access.authority_models import ApprovalActivity, ApprovalDecision, ApprovalRequest
from core.access.models import AccessLog, Membership
from core.access.testing import make_user, member
from fixed_assets import services, testing as t
from fixed_assets.models import AssetEvent, DepreciationRun, FixedAsset
from ledger import documents
from ledger.models import Account, JournalEntry, JournalLine

pytestmark = pytest.mark.django_db
D = Decimal


def vehicle(w, cost, entity=None, available=date(2027, 1, 1)):
    entity = entity or w.alm
    bill = t.bill(w, entity, [("1520", cost)], on=available)
    return t.capitalise(w, t.line_of(bill), available=available, residual_value=D("0.00"))


def submitted_run(w, entity, name, user):
    run = services.prepare_depreciation(entity, t.period(entity, name), user=user)
    return services.submit_depreciation(run, user=user)


# --- AT-24, R35 ---------------------------------------------------------------------------------------------------

def test_at24_agents_draft_and_prepare_only(w):
    bill = t.bill(w, w.alm, [("1520", "60000.00")])
    asset = services.create_asset_from_line(t.line_of(bill), user=w.g, location=w.loc, vat_use="taxable",
                                            held_for="own_use")
    assert asset.source == "agent:fa_drafter" and asset.status == "draft"
    logged = AccessLog.objects.filter(actor=w.g, event="permission_granted", permission="fixed_assets.create_asset")
    assert [r.details["action"] for r in logged] == ["link_bill_line"]
    with pytest.raises(access.AccessDenied) as exc:
        services.submit_asset(asset, user=w.g, available_for_use_date=date(2027, 1, 15))
    assert exc.value.code == "agent_forbidden"
    services.submit_asset(asset, user=w.a, available_for_use_date=date(2027, 1, 15))
    with pytest.raises(access.AccessDenied):
        t.decide(asset, w.g, permission="fixed_assets.capitalise")
    t.decide(asset, w.c, permission="fixed_assets.capitalise")
    run = services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.g)
    assert run.source == "agent:fa_drafter"
    assert AccessLog.objects.filter(actor=w.g, permission="fixed_assets.prepare_depreciation",
                                    details__action="prepare_run").exists()
    with pytest.raises(access.AccessDenied) as exc:
        services.submit_depreciation(run, user=w.g)
    assert exc.value.code == "agent_forbidden"
    with pytest.raises(access.AccessDenied):
        services.discard_depreciation(run, user=w.g)
    services.submit_depreciation(run, user=w.a)
    with pytest.raises(access.AccessDenied):
        t.decide(run, w.g, permission="fixed_assets.run_depreciation")
    with pytest.raises(access.AccessDenied):  # never posts directly either
        services.post_depreciation(run, user=w.g)
    assert not JournalEntry.objects.filter(document_type="fixed_assets.depreciationrun").exists()


# --- R61: no approval outside the engine --------------------------------------------------------------------------

def test_r61_approve_services_refuse_a_direct_call(w):
    bill = t.bill(w, w.alm, [("1520", "60000.00")])
    asset = services.create_asset_from_line(t.line_of(bill), user=w.a, location=w.loc, vat_use="taxable",
                                            held_for="own_use")
    services.submit_asset(asset, user=w.a, available_for_use_date=date(2027, 1, 15))
    with pytest.raises(access.AccessDenied) as exc:
        services.activate_asset(asset, user=w.c)
    assert exc.value.code == "approval_request_required" and exc.value.message_ar
    t.decide(asset, w.c, permission="fixed_assets.capitalise")
    run = submitted_run(w, w.alm, "2027-01", w.a)
    with pytest.raises(access.AccessDenied) as exc:
        services.post_depreciation(run, user=w.c)
    assert exc.value.code == "approval_request_required"
    asset.refresh_from_db()
    e = t.event(w, asset, "impairment", date(2027, 1, 20), approve=False, recoverable_amount=D("50000.00"))
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_event(e, user=w.c)
    assert exc.value.code == "approval_request_required"
    assert DepreciationRun.objects.get(pk=run.pk).status == "submitted"


# --- AT-27, R37 ---------------------------------------------------------------------------------------------------

def test_at27_self_approval_only_by_the_top_authority_and_logged(w):
    vehicle(w, "60000.00", entity=w.sub)
    run = submitted_run(w, w.sub, "2027-01", w.ce)  # SUB's ceo prepares, submits and decides
    t.decide(run, w.ce, permission="fixed_assets.run_depreciation")
    run.refresh_from_db()
    assert run.status == "posted" and run.posted_by == w.ce
    log = AccessLog.objects.get(event="self_approval", actor=w.ce, entity=w.sub)
    assert log.details["activity"] == "fixed_assets.depreciation_run"
    month = reports._build_content(w.sub, date(timezone.localdate().year, timezone.localdate().month, 1))
    assert [x["details"]["document"] for x in month["sections"]["self_approvals"]] == [
        ["fixed_assets.depreciationrun", run.pk]]

    vehicle(w, "60000.00")
    alm_run = submitted_run(w, w.alm, "2027-01", w.ca)  # ALM's ceo is not ALM's top authority
    with pytest.raises(access.AccessDenied) as exc:
        t.decide(alm_run, w.ca, permission="fixed_assets.run_depreciation")
    assert exc.value.code == "self_approval"
    services.discard_depreciation(alm_run, user=w.ca)
    member(w.m, w.alm, "accountant")  # the managing partner also acts as preparer in ALM
    mp_run = submitted_run(w, w.alm, "2027-01", w.m)
    t.decide(mp_run, w.m, permission="fixed_assets.run_depreciation")
    mp_run.refresh_from_db()
    assert mp_run.status == "posted"
    assert AccessLog.objects.filter(event="self_approval", actor=w.m, entity=w.alm).exists()

    ch = make_user("ch")
    member(ch, w.alm, "chairman")
    req = matrix.request_governance_change(w.sub, user=w.m, reason="board decision", self_approval_enabled=False)
    matrix.decide_governance_change(req, user=ch, approve=True)
    core_services.change_entity_settings(w.sub, user=w.m, reason="board decision", dual_control_request=req,
                                         self_approval_enabled=False)
    off = submitted_run(w, w.sub, "2027-02", w.ce)
    with pytest.raises(access.AccessDenied) as exc:
        t.decide(off, w.ce, permission="fixed_assets.run_depreciation")
    assert exc.value.code == "self_approval"


# --- AT-28, AT-29, AT-30, R36 -------------------------------------------------------------------------------------

@pytest.fixture
def ch(w):
    user = make_user("chair")
    member(user, w.alm, "chairman")
    return user


def approved_matrix(w, ch, rows):
    v = matrix.draft_version(w.alm, user=w.m, effective_from=timezone.localdate(access.now()),
                             summary_en="Test matrix", summary_ar="مصفوفة اختبار", copy_current=False)
    for row in rows:
        matrix.add_rule(v, user=w.m, **row)
    matrix.submit_version(v, user=w.m)
    return matrix.decide_version(v, user=ch, approve=True)


RUN = "fixed_assets.depreciation_run"


def test_at28_matrix_bands_route_runs_and_a_gap_refuses_submission(w, ch):
    approved_matrix(w, ch, [dict(activity=RUN, approver_roles=["approver"], amount_to="50000.00"),
                            dict(activity=RUN, approver_roles=["managing_partner"], amount_from="50000.01")])
    asset = vehicle(w, "3600000.00")  # 60,000.00 a month
    run = submitted_run(w, w.alm, "2027-01", w.a)
    assert run.total == D("60000.00")
    with pytest.raises(access.AccessDenied) as exc:
        t.decide(run, w.c, permission="fixed_assets.run_depreciation")
    assert exc.value.code == "not_in_matrix"
    t.decide(run, w.m, permission="fixed_assets.run_depreciation")
    run.refresh_from_db()
    assert run.status == "posted"
    req = ApprovalRequest.objects.get(document_type="fixed_assets.depreciationrun", document_id=run.pk)
    assert req.rule_source == "matrix"
    approved_matrix(w, ch, [dict(activity=RUN, approver_roles=["approver"], amount_to="50000.00"),
                            dict(activity=RUN, approver_roles=["managing_partner"], amount_from="60000.01")])
    feb = services.prepare_depreciation(w.alm, t.period(w.alm, "2027-02"), user=w.a)
    with pytest.raises(access.AccessDenied) as exc:
        services.submit_depreciation(feb, user=w.a)
    assert exc.value.code == "no_authority_rule"
    assert DepreciationRun.objects.get(pk=feb.pk).status == "draft"
    e = t.event(w, asset, "impairment", date(2027, 2, 10), approve=False, recoverable_amount=D("3000000.00"))
    imp = ApprovalRequest.objects.get(document_type="fixed_assets.assetevent", document_id=e.pk)
    assert (imp.rule_source, imp.activity.code) == ("default", "fixed_assets.impairment")


def test_at29_delegation_only_where_the_row_allows_it(w, ch):
    approved_matrix(w, ch, [dict(activity=RUN, approver_user=w.c)])
    d = delegations.delegate_authority(w.c, entity=w.alm, delegate=w.c2, valid_to=access.now() + timedelta(days=30),
                                       reason_en="Leave", reason_ar="إجازة", activities=[RUN])
    vehicle(w, "60000.00")
    run = submitted_run(w, w.alm, "2027-01", w.a)
    t.decide(run, w.c2, permission="fixed_assets.run_depreciation")
    run.refresh_from_db()
    assert run.status == "posted"
    dec = ApprovalDecision.objects.get(request__document_type="fixed_assets.depreciationrun", request__document_id=run.pk)
    assert (dec.user, dec.on_behalf_of, dec.delegation) == (w.c2, w.c, d)
    month = reports._build_content(w.alm, date(timezone.localdate().year, timezone.localdate().month, 1))
    assert [(x["document"], x["on_behalf_of"]) for x in month["sections"]["delegations"]["decisions"]] == [
        (["fixed_assets.depreciationrun", run.pk], month["sections"]["delegations"]["decisions"][0]["on_behalf_of"])]
    assert month["sections"]["delegations"]["decisions"][0]["on_behalf_of"]
    approved_matrix(w, ch, [dict(activity=RUN, approver_user=w.c, delegation_allowed=False)])
    feb = submitted_run(w, w.alm, "2027-02", w.a)
    with pytest.raises(access.AccessDenied) as exc:
        t.decide(feb, w.c2, permission="fixed_assets.run_depreciation")
    assert exc.value.code == "delegation_not_allowed"


def test_at30_an_issued_amount_outside_the_band_is_refused_and_returned(w, ch, monkeypatch):
    approved_matrix(w, ch, [dict(activity=RUN, approver_roles=["approver"], amount_to="50000.00"),
                            dict(activity=RUN, approver_roles=["managing_partner"], amount_from="50000.01")])
    vehicle(w, "3000000.00")  # 50,000.00 a month
    run = submitted_run(w, w.alm, "2027-01", w.a)
    req = ApprovalRequest.objects.get(document_type="fixed_assets.depreciationrun", document_id=run.pk)
    real = services._compute_run

    def plus_a_halala(entity, period):  # the test double: the fingerprint is unchanged, the total is not
        lines, fingerprint = real(entity, period)
        lines[0]["amount"] += D("0.01")
        return lines, fingerprint

    monkeypatch.setattr(services, "_compute_run", plus_a_halala)
    with pytest.raises(inbox.InboxRefused) as exc:
        inbox.decide(w.c, w.alm, req.pk, version=req.document_version, approve=True)
    assert exc.value.code == "amount_outside_band"
    run.refresh_from_db()
    assert (run.status, run.journal_entry) == ("draft", None)  # back to its preparer
    assert ApprovalRequest.objects.get(pk=req.pk).status == "returned"
    assert not JournalEntry.objects.filter(document_type="fixed_assets.depreciationrun").exists()


# --- AT-38, R38 ---------------------------------------------------------------------------------------------------

FLAGS = {
    "fixed_assets.view": {"Ag"}, "fixed_assets.maintain_setup": set(), "fixed_assets.maintain_policy": set(),
    "fixed_assets.approve_policy": {"A"}, "fixed_assets.create_asset": {"Ag"}, "fixed_assets.submit_asset": set(),
    "fixed_assets.capitalise": {"A", "P"}, "fixed_assets.prepare_depreciation": {"Ag"},
    "fixed_assets.run_depreciation": {"A", "P"}, "fixed_assets.record_event": set(), "fixed_assets.impair": {"A", "P"},
    "fixed_assets.change_estimate": {"A"}, "fixed_assets.dispose": {"A", "P"}, "fixed_assets.reverse_document": {"P"},
}
ACTIVITIES = {"fixed_assets.policy": "fixed_assets.approve_policy", "fixed_assets.capitalisation": "fixed_assets.capitalise",
              "fixed_assets.depreciation_run": "fixed_assets.run_depreciation", "fixed_assets.impairment": "fixed_assets.impair",
              "fixed_assets.estimate_change": "fixed_assets.change_estimate", "fixed_assets.disposal": "fixed_assets.dispose",
              "fixed_assets.reversal": "fixed_assets.reverse_document"}


def test_at38_catalogue_registry_and_seed(w):
    found = {c: set(p.flags) for c, p in catalogue.CATALOGUE.items() if c.startswith("fixed_assets.")}
    assert found == FLAGS
    assert not any(catalogue.CATALOGUE[c].never_self_approved for c in FLAGS)  # OQ-21: no NSA
    assert {"fixed_assets.capitalise", "fixed_assets.run_depreciation", "fixed_assets.impair",
            "fixed_assets.dispose"} <= catalogue.DOCUMENT_POST_PERMISSIONS
    assert "fixed_assets.reverse_document" in catalogue.REVERSE_PERMISSIONS
    for label in ("fixed_assets.depreciationrun", "fixed_assets.assetevent"):
        doc_type = documents.get(label)
        assert doc_type.build_lines and doc_type.validate_lines and doc_type.pending_documents and \
            doc_type.reverse_guard and doc_type.reverse_permissions == {"fixed_assets.reverse_document"}
        assert doc_type.creator_may_post is None and doc_type.contract_approval is None and \
            doc_type.sama_tax_point is None
    assert documents.get("fixed_assets.depreciationrun").post_permissions == {"fixed_assets.run_depreciation"}
    assert documents.get("fixed_assets.assetevent").post_permissions == {
        "fixed_assets.capitalise", "fixed_assets.impair", "fixed_assets.dispose"}
    for code, permission in ACTIVITIES.items():
        assert authority_defaults.get(code).permission == permission
        assert ApprovalActivity.objects.get(code=code).permission == permission
    for codename in ("fixed_assets.view", "fixed_assets.create_asset", "fixed_assets.prepare_depreciation"):
        assert catalogue.CATALOGUE[codename].agent_ok
    # validate_lines refuses a run whose lines touch a control (1200) or tax-control (2200) account
    vehicle(w, "60000.00")
    run = services.prepare_depreciation(w.alm, t.period(w.alm, "2027-01"), user=w.a)
    bad = [JournalLine(account=Account.objects.get(entity=w.alm, code="1200"), debit=run.total, credit=D("0")),
           JournalLine(account=Account.objects.get(entity=w.alm, code="2200"), debit=D("0"), credit=run.total)]
    problems = documents.get("fixed_assets.depreciationrun").validate_lines(run, bad)
    assert any("1200" in p and "control" in p for p in problems) and any("2200" in p for p in problems)
    good = [JournalLine(account=Account.objects.get(entity=w.alm, code="5400"), debit=run.total, credit=D("0")),
            JournalLine(account=Account.objects.get(entity=w.alm, code="1590"), debit=D("0"), credit=run.total)]
    assert documents.get("fixed_assets.depreciationrun").validate_lines(run, good) == []
    other = [JournalLine(account=Account.objects.get(entity=w.sub, code="5400"), debit=run.total, credit=D("0")),
             JournalLine(account=Account.objects.get(entity=w.alm, code="1590"), debit=D("0"), credit=run.total)]
    assert any("another entity" in p for p in documents.get("fixed_assets.depreciationrun").validate_lines(run, other))


def test_r36_seed_v21_holders(w):
    from core.access.seed import ROLE_SEED, ROLE_SEED_V21

    assert ROLE_SEED is ROLE_SEED_V21
    holders = {perm: {c for c, row in ROLE_SEED.items() if perm in row[5]} for perm in FLAGS}
    assert holders["fixed_assets.view"] == {"accountant", "approver", "ceo", "managing_partner", "internal_auditor",
                                            "external_auditor", "business_analyst"}
    assert holders["fixed_assets.prepare_depreciation"] == {"accountant", "ceo"}
    assert holders["fixed_assets.run_depreciation"] == {"approver", "ceo", "managing_partner"}
    assert holders["fixed_assets.reverse_document"] == {"approver", "ceo"}
    import importlib
    import inspect

    mod = importlib.import_module("core.migrations.0038_seed_access_v21_fixed_assets")
    assert "import ROLE_SEED" not in inspect.getsource(mod)
    for code, row in mod.ROLES_CHANGED.items():
        assert row == (*ROLE_SEED_V21[code][:5], tuple(sorted(set(ROLE_SEED_V21[code][5])))), code
    assert {a[0] for a in mod.ACTIVITIES} == set(ACTIVITIES)
