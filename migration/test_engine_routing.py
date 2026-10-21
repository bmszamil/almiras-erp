"""migration.md R13, R16 as amended (M7) and AT-25: the account map's approval and the posting of the imported
drafts go through the one approval engine (access §10, R61): submitted by the preparer (the request opens then),
decided in the engine under the entity's matrix (or the code default); a direct approval is refused. Fictional data."""
import pytest
from django.utils import timezone

from core.access import AccessDenied, engine, matrix
from core.access import services as access
from core.access.authority_models import ApprovalDecision, ApprovalRequest
from core.access.models import AccessLog
from core.access.testing import make_user, member
from ledger.models import JournalEntry, LedgerError
from migration import services
from migration.conftest import CUTOVER, OPEN_ITEMS, map_rows, tb_file
from migration.models import AccountMap, ImportedEntry, MigrationError

pytestmark = pytest.mark.django_db


@pytest.fixture
def governed(w):
    """ALM's top authority (managing partner) and access admin, who propose and decide matrix versions (R76)."""
    w.mp, w.aa = make_user("mig.mp"), make_user("mig.aa")
    member(w.mp, w.alm, "managing_partner")
    member(w.aa, w.alm, "access_admin")
    return w


def alm_matrix(w, rows):
    v = matrix.draft_version(w.alm, user=w.aa, effective_from=timezone.localdate(access.now()),
                             summary_en="Migration approvals", summary_ar="اعتمادات الترحيل", copy_current=False)
    for row in rows:
        matrix.add_rule(v, user=w.aa, **row)
    matrix.submit_version(v, user=w.aa)
    return matrix.decide_version(v, user=w.mp, approve=True)


def test_at25_map_approval_and_opening_posting_through_the_engine(governed, loaded):
    w = governed
    alm_matrix(w, [{"activity": "migration.account_map", "approver_roles": ["approver"]},
                   {"activity": "ledger.manual_entry", "approver_roles": ["approver"]}])
    amap = services.create_account_map(w.alm, user=w.a, rows=map_rows())
    for user in (w.a, w.p):  # a direct call is refused, whoever makes it
        with pytest.raises(AccessDenied) as exc:
            services.approve_account_map(amap, user=user)
        assert exc.value.code == "approval_request_required"
    with pytest.raises(MigrationError) as exc:  # not submitted: nothing for the engine to decide
        services.decide_account_map(amap, user=w.p)
    assert exc.value.code == "map_not_submitted"
    services.submit_account_map(amap, user=w.a)
    req = ApprovalRequest.objects.get(document_type="migration.accountmap", document_id=amap.pk)
    assert (req.status, req.rule_source, req.submitted_by) == ("open", "matrix", w.a)  # opened at submission
    with pytest.raises(AccessDenied):
        services.decide_account_map(amap, user=w.a)  # the preparer never approves (and holds no approval right)
    approved = services.decide_account_map(amap, user=w.p)
    assert (approved.status, approved.approved_by) == ("approved", w.p)
    req.refresh_from_db()
    decision = ApprovalDecision.objects.get(request=req)
    assert (req.status, decision.user, decision.rule_source, decision.rule_id is not None) == ("approved", w.p,
                                                                                             "matrix", True)
    assert AccessLog.objects.filter(event="approval_decided", actor=w.p, details__request=req.pk,
                                    details__rule_source="matrix").exists()

    plan = services.load_opening_balances(w.alm, tb_file(), user=w.a, cutover=CUTOVER,
                                          apply=True)
    entry = plan.created[0].journal_entry
    entry.refresh_from_db()
    req = ApprovalRequest.objects.get(document_type="ledger.journalentry", document_id=entry.pk)
    assert (entry.submitted_by, req.status, req.rule_source) == (w.a, "open", "matrix")  # submitted when loaded
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    assert [e.pk for e in result["posted"]] == [entry.pk]
    req.refresh_from_db()
    decision = ApprovalDecision.objects.get(request=req)
    assert (req.status, decision.user, decision.rule_source) == ("approved", w.p, "matrix")
    assert JournalEntry.objects.get(pk=entry.pk).posted_by == w.p


def test_r13_a_change_after_submission_needs_a_new_submission(governed):
    w = governed
    amap = services.create_account_map(w.alm, user=w.a, rows=map_rows())
    services.submit_account_map(amap, user=w.a)
    services.set_map_row(amap, user=w.a2, legacy_code="1101", erp_code="1110", legacy_name="الصندوق")
    assert AccountMap.objects.get(pk=amap.pk).submitted_at is None
    with pytest.raises(MigrationError) as exc:
        services.decide_account_map(amap, user=w.p)
    assert exc.value.code == "map_not_submitted"
    services.submit_account_map(amap, user=w.a2)
    with pytest.raises(AccessDenied):  # the accountant holds no approval right
        services.decide_account_map(amap, user=w.a2)
    assert services.decide_account_map(amap, user=w.p).status == "approved"


def test_r13_the_approver_may_return_a_submitted_map(governed):
    w = governed
    amap = services.create_account_map(w.alm, user=w.a, rows=map_rows())
    services.submit_account_map(amap, user=w.a)
    returned = services.decide_account_map(amap, user=w.p, approve=False, note="check 1102")
    assert (returned.status, returned.submitted_at) == ("draft", None)
    assert ApprovalRequest.objects.get(document_type="migration.accountmap", document_id=amap.pk).status == "returned"


def test_r16_two_step_matrix_records_the_first_decision_and_the_second_posts(governed, mapped, loaded):
    w = governed
    alm_matrix(w, [{"activity": "ledger.manual_entry", "approver_roles": ["approver"], "step_no": 1},
                   {"activity": "ledger.manual_entry", "approver_roles": ["managing_partner"], "step_no": 2}])
    services.load_opening_balances(w.alm, tb_file(), user=w.a, cutover=CUTOVER, apply=True)
    first = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    assert first["posted"] == [] and first["pending"] == ["opening"]
    assert "next step" in first["details"]["opening"][0] and "المرحلة التالية" in first["details"]["opening"][0]
    entry = ImportedEntry.objects.get(kind="opening").journal_entry
    assert JournalEntry.objects.get(pk=entry.pk).status == "draft"
    second = services.post_imported_entries(w.alm, user=w.mp, kind="opening", apply=True)
    assert [e.pk for e in second["posted"]] == [entry.pk]


def test_r22_the_engine_inbox_cannot_post_an_opening_entry_that_does_not_tie_out(governed, mapped, open_items):
    w = governed
    services.load_opening_balances(w.alm, tb_file(), user=w.a, cutover=CUTOVER, apply=True)
    entry = ImportedEntry.objects.get(kind="opening").journal_entry
    open_items["ALM"] = {**OPEN_ITEMS, ("1200", ""): OPEN_ITEMS[("1200", "")] - 1}
    with pytest.raises(LedgerError) as exc:  # decided straight from the engine inbox, not through migration_post
        engine.decide(entry, w.p, permission="ledger.post_entry")
    assert exc.value.code == "import_not_ready" and "1200" in str(exc.value)
    assert JournalEntry.objects.get(pk=entry.pk).status == "draft"
    open_items["ALM"] = dict(OPEN_ITEMS)
    engine.decide(entry, w.p, permission="ledger.post_entry")
    assert JournalEntry.objects.get(pk=entry.pk).status == "posted"
