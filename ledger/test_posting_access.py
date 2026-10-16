"""R23 / AT-21 / AT-24: ledger posting needs a Grant for the entry's own entity (access.md)."""
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from core import access
from core.access import services as access_services
from core.access.models import AccessLog
from core.access.testing import make_agent, make_user, member
from core.models import Entity
from ledger import services
from ledger.models import JournalEntry, LedgerError
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts

pytestmark = pytest.mark.django_db
CASH = [{"account": "1100", "debit": 10}, {"account": "3100", "credit": 10}]


def setup_entity(code, consolidation="full", parent=None):
    e = Entity.objects.create(code=code, name_en=code, name_ar=f"كيان {code}", consolidation=consolidation, parent=parent)
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2027)
    return e


@pytest.fixture
def w():
    alm = setup_entity("ALM", "parent")
    sub = setup_entity("SUB", "full", alm)
    clerk, cfo, mp = make_user("clerk"), make_user("cfo"), make_user("mp")
    for e in (alm, sub):
        member(clerk, e, "accountant")
    member(cfo, alm, "approver")
    member(mp, alm, "managing_partner")
    return SimpleNamespace(alm=alm, sub=sub, clerk=clerk, cfo=cfo, mp=mp)


def draft(w, entity, lines=CASH, d=date(2027, 1, 10)):
    return services.create_entry(entity, d, "Manual journal", lines, user=w.clerk)


def bill(entity, created_by):
    return SimpleNamespace(pk=7, _meta=SimpleNamespace(label_lower="invoicing.bill"), created_by_id=created_by.pk, submitted_by_id=None)


def status(entry):
    return JournalEntry.objects.get(pk=entry.pk).status


def test_at21_grant_for_alm_refused_for_sub_entry_and_accepted_for_alm(w):
    grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill(w.alm, w.clerk))
    sub_entry, alm_entry = draft(w, w.sub), draft(w, w.alm)
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(sub_entry, grant)
    assert exc.value.code == "grant_other_entity"
    assert status(sub_entry) == "draft"
    assert AccessLog.objects.filter(event="permission_denied", entity=w.sub, details__reason="grant_other_entity").exists()
    posted = services.post_entry(alm_entry, grant)
    assert posted.status == "posted" and posted.posted_by == w.cfo


def test_post_without_grant_refused(w):
    entry = draft(w, w.alm)
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(entry)
    assert exc.value.code == "grant_missing"
    with pytest.raises(access.AccessDenied):
        services.post_entry(entry, "not a grant")
    assert status(entry) == "draft"


def test_manual_journal_posted_by_approver_with_grant_for_that_draft(w):
    entry = draft(w, w.alm)
    grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=entry)
    posted = services.post_entry(entry, grant)
    assert (posted.status, posted.created_by, posted.posted_by) == ("posted", w.clerk, w.cfo)


def test_ledger_post_entry_grant_for_another_draft_refused(w):
    first, second = draft(w, w.alm), draft(w, w.alm)
    grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=first)
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(second, grant)
    assert exc.value.code == "grant_wrong_document" and status(second) == "draft"


def test_creator_cannot_post_own_manual_journal_even_with_self_approval_grant(w):
    member(w.clerk, w.alm, "approver")
    entry = draft(w, w.alm)
    with pytest.raises(access.AccessDenied):
        access.require(w.clerk, w.alm, "ledger.post_entry", document=entry)
    grant = access.require(w.clerk, w.alm, "ledger.post_entry", document=entry, allow_self_approval=True)
    with pytest.raises(access.AccessDenied) as exc:  # the ledger has no self-approval switch
        services.post_entry(entry, grant)
    assert exc.value.code == "self_approval" and status(entry) == "draft"


def test_accountant_cannot_post_manual_journals(w):  # OQ-5 default
    other = make_user()
    member(other, w.alm, "accountant")
    entry = draft(w, w.alm)
    with pytest.raises(access.AccessDenied):
        access.require(other, w.alm, "ledger.post_entry", document=entry)


def test_forged_grant_refused(w):
    nobody = make_user("nobody")
    entry = draft(w, w.alm)
    forged = access_services.Grant(user=nobody, entity=w.alm, permission="invoicing.approve_bill", membership_ids=(1,),
                                   checked_at=access_services.now())
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(entry, forged)
    assert exc.value.code == "no_membership" and status(entry) == "draft"


def test_grant_rechecked_after_revocation(w):  # R4
    entry = draft(w, w.alm)
    grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=entry)
    from core.access.models import Membership

    access.revoke_membership(w.mp, Membership.objects.get(user=w.cfo, role__code="approver"), "Left")
    with pytest.raises(access.AccessDenied):
        services.post_entry(entry, grant)
    assert status(entry) == "draft"


def test_non_posting_grant_refused(w):
    entry = draft(w, w.alm)
    grant = access.require(w.clerk, w.alm, "ledger.create_entry")
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(entry, grant)
    assert exc.value.code == "grant_wrong_permission"
    reverse_grant = access.require(w.clerk, w.alm, "ledger.reverse_entry")
    with pytest.raises(access.AccessDenied):  # a reversal right does not post new entries
        services.post_entry(entry, reverse_grant)


def test_agent_never_obtains_a_posting_grant(w):
    agent = make_agent()
    entry = draft(w, w.alm)
    with pytest.raises(access.AccessDenied):
        access.require(agent, w.alm, "ledger.post_entry", document=entry)
    forged = replace(access.require(w.cfo, w.alm, "ledger.post_entry", document=entry), user=agent)
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(entry, forged)
    assert exc.value.code == "agent_forbidden"


def test_reverse_needs_reverse_grant_for_same_entity(w):
    entry = draft(w, w.alm)
    posted = services.post_entry(entry, access.require(w.cfo, w.alm, "ledger.post_entry", document=entry))
    with pytest.raises(access.AccessDenied):
        services.reverse_entry(posted)
    with pytest.raises(access.AccessDenied) as exc:
        services.reverse_entry(posted, access.require(w.clerk, w.sub, "ledger.reverse_entry"))
    assert exc.value.code == "grant_other_entity"
    with pytest.raises(access.AccessDenied) as exc:
        services.reverse_entry(posted, access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill(w.alm, w.clerk)))
    assert exc.value.code == "grant_wrong_permission"
    assert status(posted) == "posted"
    rev = services.reverse_entry(posted, access.require(w.clerk, w.alm, "ledger.reverse_entry"), reason="typo")
    assert rev.status == "posted" and rev.posted_by == w.clerk and status(posted) == "reversed"


def test_create_entry_post_true_requires_grant_and_writes_nothing_when_refused(w):
    before = JournalEntry.objects.count()
    with pytest.raises(access.AccessDenied):
        services.create_entry(w.alm, date(2027, 1, 10), "x", CASH, user=w.clerk, post=True)
    other_grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill(w.alm, w.clerk))
    with pytest.raises(access.AccessDenied):
        services.create_entry(w.sub, date(2027, 1, 10), "x", CASH, user=w.clerk, post=True, grant=other_grant)
    assert JournalEntry.objects.count() == before


def test_create_entry_post_true_refuses_manual_journal_grant(w):
    some_draft = draft(w, w.alm)
    grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=some_draft)
    before = JournalEntry.objects.count()
    with pytest.raises(access.AccessDenied) as exc:
        services.create_entry(w.alm, date(2027, 1, 10), "x", CASH, user=w.cfo, post=True, grant=grant)
    assert exc.value.code == "grant_needs_draft" and JournalEntry.objects.count() == before


def test_create_entry_post_true_with_document_approval_grant(w):  # A1: approval posts automatically
    grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill(w.alm, w.clerk))
    e = services.create_entry(w.alm, date(2027, 1, 10), "Bill 7", CASH, user=w.clerk, post=True, grant=grant)
    assert e.status == "posted" and e.posted_by == w.cfo and e.created_by == w.clerk


def test_post_on_clearance_records_document_approver(w):  # R23, zatca D-8
    queue = access.system_user("zatca_queue")
    member(queue, w.alm, "system_zatca_queue")
    grant = access.require(queue, w.alm, "invoicing.post_on_clearance")
    entry = draft(w, w.alm)
    with pytest.raises(LedgerError, match="posted_by"):
        services.post_entry(entry, grant)
    posted = services.post_entry(entry, grant, posted_by=w.cfo)
    assert posted.posted_by == w.cfo


def test_only_clearance_may_set_another_posted_by(w):
    entry = draft(w, w.alm)
    grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=entry)
    with pytest.raises(LedgerError):
        services.post_entry(entry, grant, posted_by=w.mp)
    assert status(entry) == "draft"


def test_at24_full_rights_do_not_override_ledger_rules(w):  # R7
    sharia = draft(w, w.alm, [{"account": "5610", "debit": 50}, {"account": "1100", "credit": 50}], d=date(2027, 2, 10))
    with pytest.raises(LedgerError, match="Sharia-prohibited"):
        services.post_entry(sharia, access.require(w.mp, w.alm, "ledger.post_entry", document=sharia))
    services.close_period(w.alm.periods.get(name="2027-01"), user=w.cfo)
    late = draft(w, w.alm, d=date(2027, 1, 20))
    with pytest.raises(LedgerError, match="closed"):
        services.post_entry(late, access.require(w.mp, w.alm, "ledger.post_entry", document=late))
    assert status(sharia) == status(late) == "draft"


# --- access.md 3.6: close_period, draft creation and ledger views -------------

def test_close_period_needs_close_permission_in_that_entity(w):
    alm_jan, sub_jan = w.alm.periods.get(name="2027-01"), w.sub.periods.get(name="2027-01")
    for user, period in ((w.clerk, alm_jan), (w.cfo, sub_jan), (None, alm_jan), (make_agent(), alm_jan)):
        with pytest.raises(access.AccessDenied):
            services.close_period(period, user=user)
    assert alm_jan.__class__.objects.get(pk=alm_jan.pk).status == "open"
    assert sub_jan.__class__.objects.get(pk=sub_jan.pk).status == "open"
    assert services.close_period(alm_jan, user=w.cfo).status == "closed"


def test_close_period_permission_does_not_skip_draft_check(w):  # R7
    draft(w, w.alm, d=date(2027, 1, 31))
    draft(w, w.sub, d=date(2027, 2, 1))  # another entity / next month: does not block
    jan = w.alm.periods.get(name="2027-01")
    with pytest.raises(LedgerError, match="Draft entries"):
        services.close_period(jan, user=w.cfo)
    assert jan.__class__.objects.get(pk=jan.pk).status == "open"
    JournalEntry.objects.filter(entity=w.alm).delete()
    assert services.close_period(jan, user=w.cfo).status == "closed"


def test_draft_entry_needs_create_permission_in_that_entity(w):
    outsider, viewer = make_user("outsider"), make_user("viewer")
    member(viewer, w.alm, "viewer")
    member(outsider, w.sub, "accountant")
    for user in (None, viewer, outsider, w.cfo):
        with pytest.raises(access.AccessDenied) as exc:
            services.create_entry(w.alm, date(2027, 1, 10), "x", CASH, user=user)
        assert AccessLog.objects.filter(
            event="permission_denied", entity=w.alm, permission="ledger.create_entry", details__reason=exc.value.code
        ).exists()
    assert not JournalEntry.objects.exists()
    assert draft(w, w.alm).status == "draft"


def test_trial_balance_needs_ledger_view_in_that_entity(w):
    viewer, outsider = make_user("viewer"), make_user("outsider")
    member(viewer, w.alm, "viewer")
    member(outsider, w.sub, "viewer")
    assert services.trial_balance(w.alm, date(2027, 12, 31), user=viewer) == []
    for user in (outsider, None):
        with pytest.raises(access.AccessDenied):
            services.trial_balance(w.alm, date(2027, 12, 31), user=user)
    assert AccessLog.objects.filter(event="permission_denied", actor=outsider, entity=w.alm, permission="ledger.view").exists()


def _intercompany_world(w):
    """ALM <-> SUB and ALM <-> MIN (equity-method minority holding) balances, both sides booked."""
    mn = setup_entity("MIN", "equity", w.alm)
    member(w.clerk, mn, "accountant")
    member(w.cfo, w.sub, "approver")
    member(w.cfo, mn, "approver")
    for entity, other in ((w.alm, w.sub), (w.sub, w.alm), (w.alm, mn), (mn, w.alm)):
        lines = [
            {"account": "1210", "debit": 10, "counterparty": other} if entity == w.alm else {"account": "5200", "debit": 10},
            {"account": "4110", "credit": 10} if entity == w.alm else {"account": "2110", "credit": 10, "counterparty": other},
        ]
        entry = draft(w, entity, lines)
        services.post_entry(entry, access.require(w.cfo, entity, "ledger.post_entry", document=entry))
    return mn


def test_intercompany_balances_are_limited_to_what_the_user_may_see(w):
    mn = _intercompany_world(w)
    gv, alm_only, sub_only = make_user("gv"), make_user("alm.viewer"), make_user("sub.viewer")
    member(gv, w.alm, "group_viewer")  # covers ALM and full subsidiary SUB, not the equity holding (R6, OQ-3)
    member(alm_only, w.alm, "viewer")
    member(sub_only, w.sub, "viewer")
    as_of = date(2027, 1, 31)
    assert set(services.intercompany_balances(as_of, user=gv, group=w.alm)) == {("ALM", "SUB"), ("ALM", "MIN"), ("SUB", "ALM")}
    assert set(services.intercompany_balances(as_of, user=alm_only, group=w.alm)) == {("ALM", "SUB"), ("ALM", "MIN")}
    member(gv, mn, "viewer")
    assert ("MIN", "ALM") in services.intercompany_balances(as_of, user=gv, group=w.alm)
    with pytest.raises(access.AccessDenied):  # no ledger.view on the group parent
        services.intercompany_balances(as_of, user=sub_only, group=w.alm)
    with pytest.raises(LedgerError, match="not a group parent"):
        services.intercompany_balances(as_of, user=gv, group=w.sub)
