"""Access control rules and acceptance tests (docs/specs/access.md §4, §8).

Fixtures follow §8: ALM (parent), SUB (full, 100%), PART (full, 60%),
MIN (equity, 25%); users per role; seeded system users; one agent user.
All anonymised. Documents of modules not built yet (invoicing bills) are
stand-in objects with the attributes ``require`` reads.
"""
import re
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone

from core.access import catalogue, services
from core.access.backends import EntityRoleBackend
from core.access.checks import no_django_groups_or_user_permissions
from core.access.models import AccessLog, AppendOnlyError, DualControlRequest, Kind, Membership, Role, RolePermission, UserProfile
from core.access.seed import ROLE_SEED, SYSTEM_USERS_V1
from core.access.testing import make_agent, make_user, member
from core.models import Entity
from ledger.models import JournalEntry

pytestmark = pytest.mark.django_db
User = get_user_model()
RIYADH = ZoneInfo("Asia/Riyadh")
ARABIC = re.compile(r"[؀-ۿ]")


def entity(code, consolidation="full", parent=None, pct="100"):
    return Entity.objects.create(
        code=code, name_en=code, name_ar=f"كيان {code}", consolidation=consolidation, parent=parent,
        ownership_pct=Decimal(pct),
    )


@pytest.fixture
def w():
    alm = entity("ALM", "parent")
    sub = entity("SUB", "full", alm)
    part = entity("PART", "full", alm, "60")
    mino = entity("MIN", "equity", alm, "25")
    mp = make_user("mp")
    member(mp, alm, "managing_partner")
    return SimpleNamespace(alm=alm, sub=sub, part=part, min=mino, mp=mp)


def bill(entity, created_by=None, submitted_by=None, pk=1):
    """Stand-in for an invoicing bill (module not built yet)."""
    return SimpleNamespace(
        pk=pk, entity=entity, status="submitted", _meta=SimpleNamespace(label_lower="invoicing.bill"),
        created_by_id=getattr(created_by, "pk", None), submitted_by_id=getattr(submitted_by, "pk", None),
    )


def denied_logged(user, permission, entity=None):
    qs = AccessLog.objects.filter(event="permission_denied", actor=user, permission=permission)
    if entity is not None:
        qs = qs.filter(entity=entity)
    return qs.exists()


@pytest.fixture
def clock(monkeypatch):
    """Move the access layer's clock: ``clock.set(datetime)``."""
    state = SimpleNamespace(t=None)

    def set_(t):
        state.t = t

    real_now = services.now
    monkeypatch.setattr(services, "now", lambda: state.t or real_now())
    state.set = set_
    return state


def at(*args):
    return datetime(*args, tzinfo=RIYADH)


# --- Seed and catalogue ------------------------------------------------------

def test_seeded_roles_match_seed_and_catalogue():
    for code, (en, ar, scope, kind, system_code, perms) in ROLE_SEED.items():
        role = Role.objects.get(code=code)
        assert (role.scope, role.kind, role.system_code, role.is_seeded) == (scope, kind, system_code, True)
        assert role.permission_codes == frozenset(perms)
        for codename in perms:
            # Every seeded permission exists and is allowed for the role's kind and scope (R12, R13, G)
            assert catalogue.role_may_hold(kind, scope, system_code, catalogue.get(codename)) is None, (code, codename)


def test_one_system_user_per_code_with_unusable_password():  # R20
    for code, (username, _, _) in SYSTEM_USERS_V1.items():
        user = services.system_user(code)
        assert user.username == username and not user.has_usable_password()
        assert user.access_profile.kind == Kind.SYSTEM and user.access_profile.source == f"system:{code}"
        assert not (user.is_staff or user.is_superuser)
    with pytest.raises(UserProfile.DoesNotExist):
        services.system_user("payroll")


def test_managing_partner_holds_every_approval_permission():  # OQ-1 default
    approvals = {c for c, p in catalogue.CATALOGUE.items() if p.is_approval}
    assert approvals <= Role.objects.get(code="managing_partner").permission_codes


# Named by the spec (access R48). It changes the entity's own setting through two approvals; the ledger's Sharia
# check (ledger.services) reads Entity.sharia_enforced only and consults no permission (R7).
SETTING_PERMISSIONS = frozenset({"core.disable_sharia_enforcement"})


def test_no_permission_bypasses_ledger_rules():  # R7
    for codename, perm in catalogue.CATALOGUE.items():
        if codename in SETTING_PERMISSIONS:
            assert perm.is_dual_control and perm.is_segregated and perm.is_group_only and not perm.posts
            continue
        assert not re.search(r"bypass|override|sharia|closed|unbalanced|any_entity|all_entities", codename), codename


# --- R3, R24: only memberships give rights ------------------------------------

def test_superuser_groups_and_user_permissions_give_nothing(w):
    s = make_user("root")
    s.is_superuser = s.is_staff = True
    s.save()
    g = Group.objects.create(name="everything")
    g.permissions.set(Permission.objects.all())
    s.groups.add(g)
    s.user_permissions.set(Permission.objects.all())
    for perm in ("ledger.view", "ledger.reverse_entry", "core.grant_membership"):
        assert not services.has(s, w.alm, perm)
        with pytest.raises(services.AccessDenied):
            services.require(s, w.alm, perm, document=bill(w.alm) if catalogue.get(perm).posts else None)
    assert not services.entities_for(s, "ledger.view").exists()
    assert services.scope(JournalEntry.objects.all(), s, "ledger.view").count() == 0
    assert denied_logged(s, "ledger.view", w.alm)


def test_system_check_flags_django_groups_and_user_permissions(w):  # R3 [C]
    u = make_user("staffer")
    assert no_django_groups_or_user_permissions(databases=["default"]) == []
    u.user_permissions.add(Permission.objects.first())
    errors = no_django_groups_or_user_permissions(databases=["default"])
    assert [e.id for e in errors] == ["access.E001"] and "staffer" in errors[0].msg
    assert no_django_groups_or_user_permissions(databases=None) == []  # never touches the DB by default


def test_unknown_permission_is_a_programming_error(w):
    with pytest.raises(KeyError):
        services.require(w.mp, w.alm, "ledger.do_anything")


def test_backend_has_perm_needs_an_entity(w):
    u = make_user()
    member(u, w.alm, "viewer")
    backend = EntityRoleBackend()
    assert backend.has_perm(u, "ledger.view", w.alm)
    assert not backend.has_perm(u, "ledger.view")
    assert not backend.has_perm(u, "ledger.view", w.sub)
    assert u.has_perm("ledger.view", w.alm) and not u.has_perm("ledger.view")


# --- AT-1, AT-2, R2, R5: entity separation ------------------------------------

def test_at1_right_in_alm_gives_nothing_in_sub(w):
    u = make_user()
    member(u, w.alm, "accountant")
    alm_entry = JournalEntry.objects.create(entity=w.alm, date=at(2027, 1, 1).date(), description="a")
    sub_entry = JournalEntry.objects.create(entity=w.sub, date=at(2027, 1, 1).date(), description="s")
    listed = services.scope(JournalEntry.objects.all(), u, "ledger.view")
    assert list(listed) == [alm_entry]
    assert services.get_scoped_or_404(JournalEntry.objects.all(), u, "ledger.view", pk=alm_entry.pk) == alm_entry
    with pytest.raises(Http404):  # not 403: existence is not revealed (R2)
        services.get_scoped_or_404(JournalEntry.objects.all(), u, "ledger.view", pk=sub_entry.pk)
    with pytest.raises(services.AccessDenied) as exc:
        services.require(u, w.sub, "invoicing.view")
    assert exc.value.code == "no_membership"
    assert denied_logged(u, "invoicing.view", w.sub)


def test_at2_approver_in_sub_cannot_approve_alm_bill(w):
    clerk, p = make_user(), make_user()
    member(p, w.sub, "approver")
    b = bill(w.alm, created_by=clerk, submitted_by=clerk)
    with pytest.raises(services.AccessDenied):
        services.require(p, w.alm, "invoicing.approve_bill", document=b)
    assert b.status == "submitted"
    assert services.require(p, w.sub, "invoicing.approve_bill", document=bill(w.sub, created_by=clerk))


def test_r5_child_right_gives_nothing_in_parent(w):
    u = make_user()
    member(u, w.sub, "viewer")
    assert services.has(u, w.sub, "ledger.view")
    assert not services.has(u, w.alm, "ledger.view")
    assert not services.has(u, w.part, "ledger.view")


# --- AT-3, R6: group roles --------------------------------------------------------

def test_at3_group_viewer_covers_full_subsidiaries_only(w):
    g = make_user()
    member(g, w.alm, "group_viewer")
    assert [services.has(g, e, "ledger.view") for e in (w.alm, w.sub, w.part, w.min)] == [True, True, True, False]
    assert set(services.entities_for(g, "ledger.view")) == {w.alm, w.sub, w.part}
    with pytest.raises(services.AccessDenied):
        services.require(g, w.min, "ledger.view")
    member(g, w.min, "viewer")
    assert services.require(g, w.min, "ledger.view")


def test_r6_group_coverage_stops_at_equity_link(w):
    below_min = entity("MINSUB", "full", w.min)  # full child of a minority holding
    deep = entity("DEEP", "full", w.sub)  # full grandchild through full links
    assert services.has(w.mp, deep, "ledger.view")
    assert not services.has(w.mp, below_min, "ledger.view")
    assert set(services.entities_for(w.mp, "ledger.view")) == {w.alm, w.sub, w.part, deep}


def test_r6_group_role_only_on_parent_entity(w):
    with pytest.raises(ValidationError, match="group parent"):
        member(make_user(), w.sub, "group_viewer")


# --- AT-4, AT-5, R3, R4: time-bounded memberships -------------------------------

def test_at4_expired_membership_refused(w):
    u = make_user()
    member(u, w.alm, "accountant", valid_from=services.now() - timedelta(days=30), valid_to=services.now() - timedelta(days=1))
    with pytest.raises(services.AccessDenied):
        services.require(u, w.alm, "invoicing.create_sales_document")


def test_at5_future_and_revoked_memberships_refused(w):
    u = make_user()
    member(u, w.alm, "accountant", valid_from=services.now() + timedelta(days=1))
    m = member(u, w.sub, "accountant")
    assert services.has(u, w.sub, "invoicing.create_sales_document")
    services.revoke_membership(w.mp, m, "Left the company")
    for e in (w.alm, w.sub):
        with pytest.raises(services.AccessDenied):
            services.require(u, e, "invoicing.create_sales_document")


def test_r4_revocation_takes_effect_at_next_check(w):
    u = make_user()
    m = member(u, w.alm, "viewer")
    grant = services.require(u, w.alm, "ledger.view")
    assert grant.entity == w.alm and grant.membership_ids == (m.pk,)
    services.revoke_membership(w.mp, m, "Audit finished")
    assert not services.has(u, w.alm, "ledger.view")


def test_inactive_user_and_role_grant_nothing(w):
    u = make_user()
    member(u, w.alm, "viewer")
    u.is_active = False
    u.save()
    assert not services.has(u, w.alm, "ledger.view")
    v = make_user()
    member(v, w.alm, "viewer")
    Role.objects.filter(code="viewer").update(is_active=False)
    assert not services.has(v, w.alm, "ledger.view")


def test_inactive_entity_allows_viewing_only(w):  # OQ-8 default
    u = make_user()
    member(u, w.sub, "accountant")
    w.sub.is_active = False
    w.sub.save()
    assert services.has(u, w.sub, "ledger.view")
    assert not services.has(u, w.sub, "ledger.create_entry")
    assert w.sub in services.entities_for(u, "ledger.view") and w.sub not in services.entities_for(u, "ledger.create_entry")


def test_anonymous_user_refused(w):
    from django.contrib.auth.models import AnonymousUser

    with pytest.raises(services.AccessDenied) as exc:
        services.require(AnonymousUser(), w.alm, "ledger.view")
    assert exc.value.code == "no_user"
    assert not services.entities_for(AnonymousUser(), "ledger.view").exists()


# --- AT-6, AT-7, AT-8, R14: creator cannot approve -----------------------------

def test_at6_creator_cannot_approve_own_document(w):
    c = make_user()
    member(c, w.alm, "accountant")
    member(c, w.alm, "approver")
    x = bill(w.alm, created_by=c, submitted_by=c)
    with pytest.raises(services.AccessDenied) as exc:
        services.require(c, w.alm, "invoicing.approve_bill", document=x)
    assert exc.value.code == "self_approval" and x.status == "submitted"
    assert denied_logged(c, "invoicing.approve_bill", w.alm)


def test_r14_submitter_cannot_approve_either(w):
    clerk, c = make_user(), make_user()
    member(c, w.alm, "approver")
    with pytest.raises(services.AccessDenied):
        services.require(c, w.alm, "invoicing.approve_sales_document", document=bill(w.alm, created_by=clerk, submitted_by=c))


def test_at7_self_approval_when_module_switch_allows_it_is_logged(w):
    # Amended by access R14 (2026-10-18, A26) and R79 (§10): the per-module switch is withdrawn; a caller passing
    # allow_self_approval=True is refused unless the user is the entity's top authority (engine path, AT-83).
    c = make_user()
    member(c, w.alm, "approver")
    with pytest.raises(services.AccessDenied) as exc:
        services.require(c, w.alm, "invoicing.approve_bill", document=bill(w.alm, created_by=c), allow_self_approval=True)
    assert exc.value.code == "self_approval" and denied_logged(c, "invoicing.approve_bill", w.alm)
    grant = services.require(w.mp, w.alm, "invoicing.approve_bill", document=bill(w.alm, created_by=w.mp))
    assert grant.self_approval and grant.top_authority  # ALM's top authority (managing partner, R79)
    row = AccessLog.objects.get(pk=grant.log_id)
    assert row.event == "permission_granted" and row.details["self_approval"] is True
    assert row.details["top_authority"] is True


def test_at8_other_approver_approves_and_grant_is_logged(w):
    clerk, p = make_user(), make_user()
    member(p, w.alm, "approver")
    grant = services.require(p, w.alm, "invoicing.approve_bill", document=bill(w.alm, created_by=clerk, submitted_by=clerk))
    assert not grant.self_approval and grant.document_ref == ("invoicing.bill", 1)
    row = AccessLog.objects.get(pk=grant.log_id)
    assert (row.event, row.actor, row.entity, row.permission) == ("permission_granted", p, w.alm, "invoicing.approve_bill")


def test_r14_approval_without_document_is_a_programming_error(w):
    p = make_user()
    member(p, w.alm, "approver")
    with pytest.raises(services.AccessUsageError):
        services.require(p, w.alm, "invoicing.approve_bill")


def test_mp_is_fallback_approver_but_not_for_own_document(w):  # OQ-1 default + R14
    # Amended by access R70 / R64 (§10): the managing partner's fallback approval applies only in the group parent;
    # a group membership gives no approval in a subsidiary (no cascade). In ALM, where the MP is the top authority,
    # the MP's own document is a logged top-authority self-approval (R79, AT-83); a clerk's is a plain approval.
    clerk = make_user()
    assert services.require(w.mp, w.alm, "invoicing.approve_bill", document=bill(w.alm, created_by=clerk))
    with pytest.raises(services.AccessDenied) as exc:
        services.require(w.mp, w.sub, "invoicing.approve_bill", document=bill(w.sub, created_by=clerk, pk=2))
    assert exc.value.code == "no_membership"
    with pytest.raises(services.AccessDenied):  # minority holding not covered (OQ-3)
        services.require(w.mp, w.min, "invoicing.approve_bill", document=bill(w.min, created_by=clerk, pk=3))


# --- AT-9, AT-10, R12: agents -----------------------------------------------------

@pytest.fixture
def agent_role():
    role = Role.objects.create(code="agent_invoice_reader", name_en="Agent: invoice reader", name_ar="وكيل قراءة الفواتير",
                               scope="entity", kind="agent")
    for codename in ("invoicing.create_bill", "invoicing.view"):
        RolePermission.objects.create(role=role, codename=codename)
    return role


def test_at9_agent_drafts_but_cannot_submit_approve_or_post(w, agent_role):
    agent = make_agent()
    Membership.objects.create(user=agent, entity=w.alm, role=agent_role, valid_from=services.now() - timedelta(hours=1),
                              granted_by=w.mp, reason="pilot")
    assert services.require(agent, w.alm, "invoicing.create_bill")
    assert services.source_of(agent) == "agent:invoice_reader"
    with pytest.raises(services.AccessDenied):
        services.require(agent, w.alm, "invoicing.submit_bill")
    for perm in ("invoicing.approve_bill", "ledger.post_entry"):
        with pytest.raises(services.AccessDenied) as exc:
            services.require(agent, w.alm, perm, document=bill(w.alm))
        assert exc.value.code == "agent_forbidden"
    assert not services.entities_for(agent, "ledger.post_entry").exists()


def test_at10_agent_role_cannot_hold_approval_even_if_forced(w, agent_role):
    with pytest.raises(ValidationError):
        RolePermission.objects.create(role=agent_role, codename="invoicing.approve_bill")
    for codename in ("ledger.post_entry", "core.grant_membership", "zatca.switch_to_production", "invoicing.create_from_billing"):
        with pytest.raises(ValidationError):
            RolePermission(role=agent_role, codename=codename).save()
    RolePermission.objects.bulk_create([RolePermission(role=agent_role, codename="invoicing.approve_bill")])  # forced
    agent = make_agent()
    Membership.objects.create(user=agent, entity=w.alm, role=agent_role, valid_from=services.now() - timedelta(hours=1),
                              granted_by=w.mp, reason="pilot")
    with pytest.raises(services.AccessDenied):
        services.require(agent, w.alm, "invoicing.approve_bill", document=bill(w.alm))
    assert not services.entities_for(agent, "invoicing.approve_bill").exists()


def test_forced_permission_on_wrong_human_role_grants_nothing(w):  # R13 re-checked in require
    u = make_user()
    member(u, w.alm, "accountant")
    RolePermission.objects.bulk_create([RolePermission(role=Role.objects.get(code="accountant"), codename="invoicing.create_from_billing")])
    with pytest.raises(services.AccessDenied):
        services.require(u, w.alm, "invoicing.create_from_billing")


def test_role_permission_must_be_in_catalogue():
    with pytest.raises(ValidationError):
        RolePermission.objects.create(role=Role.objects.get(code="viewer"), codename="ledger.nonexistent")


def test_group_only_permission_refused_on_entity_role():
    with pytest.raises(ValidationError):
        RolePermission.objects.create(role=Role.objects.get(code="approver"), codename="zatca.grant_key_delegation")


# --- AT-11, AT-12, R11, R13, R20: system users ------------------------------------

def test_at11_billing_system_user_only_its_own_rights(w):
    billing = services.system_user("billing")
    member(billing, w.alm, "system_billing")
    assert services.require(billing, w.alm, "invoicing.create_from_billing")
    assert services.require(billing, w.alm, "billing.start_run")
    with pytest.raises(services.AccessDenied):
        services.require(billing, w.sub, "invoicing.create_from_billing")
    for perm, doc in (("invoicing.approve_bill", bill(w.alm)), ("ledger.post_entry", bill(w.alm)), ("zatca.manage_profile", None),
                      ("invoicing.view", None), ("ledger.view", None)):
        with pytest.raises(services.AccessDenied) as exc:
            services.require(billing, w.alm, perm, document=doc)
        assert exc.value.code == "system_forbidden"


def test_r13_system_role_cannot_hold_other_permissions():
    role = Role.objects.get(code="system_billing")
    for codename in ("invoicing.approve_bill", "ledger.post_entry", "zatca.process_queue", "invoicing.view"):
        with pytest.raises(ValidationError):
            RolePermission.objects.create(role=role, codename=codename)


def test_r13_sys_permission_refused_for_humans_and_other_system_users(w):
    u = make_user()
    member(u, w.alm, "accountant")
    with pytest.raises(services.AccessDenied) as exc:
        services.require(u, w.alm, "invoicing.create_from_billing")
    assert exc.value.code == "system_only"
    queue = services.system_user("zatca_queue")
    member(queue, w.alm, "system_zatca_queue")
    with pytest.raises(services.AccessDenied):
        services.require(queue, w.alm, "invoicing.create_from_billing")
    assert services.require(queue, w.alm, "invoicing.post_on_clearance", document=bill(w.alm))


def test_at12_role_kind_must_match_user_kind(w):
    human = make_user()
    with pytest.raises(ValidationError, match="system role"):
        services.grant_membership(w.mp, human, w.alm, Role.objects.get(code="system_billing"), services.now(), reason="x")
    with pytest.raises(ValidationError, match="human role"):
        services.grant_membership(w.mp, make_agent(), w.alm, Role.objects.get(code="accountant"), services.now(), reason="x")
    with pytest.raises(ValidationError, match="another system user"):
        member(services.system_user("billing"), w.alm, "system_zatca_queue")
    assert not Membership.objects.filter(user=human).exists()


def test_at13_system_user_cannot_log_in(client):
    billing = services.system_user("billing")
    assert authenticate(username=billing.username, password="") is None
    billing.set_password("known-only-to-this-test")
    billing.save()
    assert authenticate(username=billing.username, password="known-only-to-this-test") is None
    assert not client.login(username=billing.username, password="known-only-to-this-test")
    resp = client.post("/admin/login/", {"username": billing.username, "password": "known-only-to-this-test"})
    assert resp.status_code == 200 and "_auth_user_id" not in client.session


def test_r21_agent_user_cannot_use_session_login(client):
    agent = make_agent()
    agent.set_password("known-only-to-this-test")
    agent.save()
    assert not client.login(username=agent.username, password="known-only-to-this-test")
    client.force_login(agent)  # even a forced session is not accepted
    assert client.get("/admin/").status_code == 302


def test_human_can_log_in(client):
    u = make_user("person")
    u.set_password("known-only-to-this-test")
    u.save()
    assert client.login(username="person", password="known-only-to-this-test")


# --- R10, R25: profiles and flags ------------------------------------------------------

def test_r10_user_kind_never_changes():
    u = make_user()
    profile = u.access_profile
    assert profile.kind == Kind.HUMAN and profile.source == "human"
    profile.kind = Kind.AGENT
    profile.agent_name = "sneaky"
    with pytest.raises(ValidationError):
        profile.save()
    with pytest.raises(AppendOnlyError):
        UserProfile.objects.get(user=u).delete()


def test_profile_created_for_every_user():
    u = User.objects.create_user("plain")
    assert u.access_profile.kind == Kind.HUMAN


def test_at22_r25_system_and_agent_users_never_superuser_or_staff():
    billing = services.system_user("billing")
    billing.is_superuser = True
    with pytest.raises(ValidationError):
        billing.save()
    assert not User.objects.get(pk=billing.pk).is_superuser
    agent = make_agent()
    agent.is_staff = True
    with pytest.raises(ValidationError):
        agent.save()
    u = User(username="agent.y", is_staff=True)  # also refused at creation
    u._access_profile = dict(kind=Kind.AGENT, agent_name="y")
    with pytest.raises(ValidationError):
        u.save()
    assert not User.objects.filter(username="agent.y").exists()


def test_at22_superuser_and_staff_flag_changes_logged():
    u = make_user("ops")
    u.is_staff = True
    u.save()
    u.is_superuser = True
    u._access_actor = make_user("changer")
    u.save()
    rows = AccessLog.objects.filter(subject_user=u).order_by("id")
    assert [(r.event, r.details["after"]) for r in rows] == [("staff_flag_changed", True), ("superuser_flag_changed", True)]
    assert rows.last().actor.username == "changer"
    User.objects.create_superuser("root2", password=None)
    assert AccessLog.objects.filter(subject_user__username="root2", event="superuser_flag_changed").exists()


# --- AT-15, AT-16, R8, R9, R27: granting and revoking --------------------------------------

@pytest.fixture
def admin_a(w):
    a = make_user("access.admin")
    member(a, w.alm, "access_admin")
    return a


def test_at15_access_admin_grants_and_is_logged(w, admin_a):
    b = make_user("b")
    vf = services.now()
    m = services.grant_membership(admin_a, b, w.alm, Role.objects.get(code="payables_clerk"), vf, reason="Hired as AP clerk")
    assert m.granted_by == admin_a and services.has(b, w.alm, "invoicing.create_bill")
    row = AccessLog.objects.get(event="membership_granted", membership=m)
    assert (row.actor, row.subject_user, row.entity, row.role.code) == (admin_a, b, w.alm, "payables_clerk")
    assert row.details["valid_from"] == vf.isoformat() and row.details["reason"] == "Hired as AP clerk"


def test_at15_no_self_grant_and_no_grant_in_other_entity(w, admin_a):
    with pytest.raises(services.AccessDenied) as exc:
        services.grant_membership(admin_a, admin_a, w.alm, Role.objects.get(code="viewer"), services.now(), reason="me")
    assert exc.value.code == "self_grant"
    with pytest.raises(services.AccessDenied):
        services.grant_membership(admin_a, make_user(), w.sub, Role.objects.get(code="viewer"), services.now(), reason="x")
    assert AccessLog.objects.filter(event="permission_denied", actor=admin_a, permission="core.grant_membership").count() == 2
    assert not Membership.objects.filter(granted_by=admin_a).exists()


def test_oq2_access_admin_cannot_grant_sensitive_roles(w, admin_a):
    for code in ("approver", "zatca_admin", "access_admin", "managing_partner", "group_viewer"):
        target_entity = w.alm
        with pytest.raises(services.AccessDenied) as exc:
            services.grant_membership(admin_a, make_user(), target_entity, Role.objects.get(code=code), services.now(), reason="x")
        assert exc.value.code == "role_not_grantable"
    m = services.grant_membership(w.mp, make_user(), w.alm, Role.objects.get(code="approver"), services.now(), reason="New CFO")
    assert m.granted_by == w.mp
    with pytest.raises(services.AccessDenied):
        services.revoke_membership(admin_a, m, "x")


def test_r8_user_without_grant_right_cannot_grant(w):
    clerk = make_user()
    member(clerk, w.alm, "accountant")
    with pytest.raises(services.AccessDenied):
        services.grant_membership(clerk, make_user(), w.alm, Role.objects.get(code="viewer"), services.now(), reason="x")


def test_r8_nobody_revokes_own_membership(w, admin_a):
    m = Membership.objects.get(user=admin_a)
    with pytest.raises(services.AccessDenied):
        services.revoke_membership(admin_a, m, "quitting")


def test_at16_revoke_logged_and_rows_never_edited_or_deleted(w, admin_a):
    b = make_user("b")
    role = Role.objects.get(code="payables_clerk")
    m = services.grant_membership(admin_a, b, w.alm, role, services.now() - timedelta(minutes=1), reason="Hired")
    with pytest.raises(ValidationError, match="overlapping"):
        services.grant_membership(admin_a, b, w.alm, role, services.now() + timedelta(days=5), reason="again")
    services.revoke_membership(admin_a, m, "Moved to another team")
    row = AccessLog.objects.get(event="membership_revoked", membership=m)
    assert row.actor == admin_a and row.details["reason"] == "Moved to another team"
    m.refresh_from_db()
    m.valid_to = services.now() + timedelta(days=10)
    with pytest.raises(AppendOnlyError):
        m.save()
    with pytest.raises(AppendOnlyError):
        Membership.objects.get(pk=m.pk).delete()
    with pytest.raises(AppendOnlyError):  # revocation is final
        services.revoke_membership(admin_a, Membership.objects.get(pk=m.pk), "again")
    # After revocation a new grant of the same role is allowed (change = revoke + new grant).
    assert services.grant_membership(admin_a, b, w.alm, role, services.now(), reason="Back")


def test_r9_revocation_needs_reason(w, admin_a):
    m = member(make_user(), w.alm, "viewer")
    with pytest.raises(ValidationError):
        services.revoke_membership(admin_a, m, "")


def test_r27_access_log_is_append_only(w, admin_a):
    row = AccessLog.objects.filter(event="membership_granted").first() or services.log("bootstrap", actor=admin_a)
    row.details = {"edited": True}
    with pytest.raises(AppendOnlyError):
        row.save()
    with pytest.raises(AppendOnlyError):
        row.delete()


def test_membership_needs_granted_by_and_reason(w):
    with pytest.raises(ValidationError):
        Membership.objects.create(user=make_user(), entity=w.alm, role=Role.objects.get(code="viewer"),
                                  valid_from=services.now(), reason="no grantor")
    with pytest.raises(ValidationError):
        Membership.objects.create(user=make_user(), entity=w.alm, role=Role.objects.get(code="viewer"),
                                  valid_from=services.now(), granted_by=w.mp, reason="")


def test_grant_membership_refuses_delegation_shortcut(w):
    with pytest.raises(services.AccessUsageError):
        services.grant_membership(w.mp, make_user(), w.alm, Role.objects.get(code="approver"), services.now(),
                                  reason="x", delegated_from=Membership.objects.first())


# --- AT-17, R16: CFO delegation -----------------------------------------------------

@pytest.fixture
def cfo_setup(w, clock):
    """A 2027 timeline anchored so that 12 March 2027 09:00 is the real present.

    Revocations are dated by the real clock (R9, R33), so the scenario's
    revocation moment is now; every other moment is ``c.at(...)`` on the
    same shifted timeline.
    """
    shift = timezone.now() - at(2027, 3, 12, 9)

    def t(*args):
        return at(*args) + shift

    clock.set(t(2027, 1, 1, 9))
    f, d, clerk = make_user("cfo"), make_user("deputy"), make_user("clerk")
    src = member(f, w.alm, "approver", valid_from=t(2027, 1, 1), valid_to=t(2027, 12, 31))
    clock.set(t(2027, 2, 20, 9))
    dm = services.delegate_approval(f, d, w.alm, t(2027, 3, 1), t(2027, 3, 15), "Annual leave")
    return SimpleNamespace(f=f, d=d, clerk=clerk, src=src, dm=dm, b=bill(w.alm, created_by=clerk, submitted_by=clerk), at=t)


def test_at17_delegate_approves_within_dates(w, clock, cfo_setup):
    c = cfo_setup
    assert c.dm.delegated_from == c.src and c.dm.granted_by == c.f
    assert AccessLog.objects.filter(event="delegation_granted", membership=c.dm, actor=c.f).exists()
    clock.set(c.at(2027, 2, 28, 9))
    assert not services.has(c.d, w.alm, "invoicing.approve_bill")
    clock.set(c.at(2027, 3, 10, 9))
    assert services.require(c.d, w.alm, "invoicing.approve_bill", document=c.b)
    clock.set(c.at(2027, 3, 16, 9))
    with pytest.raises(services.AccessDenied):
        services.require(c.d, w.alm, "invoicing.approve_bill", document=c.b)


def test_at17_revoking_delegator_ends_delegation(w, clock, cfo_setup):
    c = cfo_setup
    clock.set(c.at(2027, 3, 10, 9))
    assert services.require(c.d, w.alm, "invoicing.approve_bill", document=c.b)
    clock.set(c.at(2027, 3, 12, 9))  # the real present
    services.revoke_membership(w.mp, c.src, "Left the company")
    clock.set(c.at(2027, 3, 12, 10))
    with pytest.raises(services.AccessDenied):
        services.require(c.d, w.alm, "invoicing.approve_bill", document=c.b)


def test_at17_delegation_needs_end_and_cannot_chain_or_outlast(w, clock, cfo_setup):
    c = cfo_setup
    with pytest.raises(ValidationError):
        services.delegate_approval(c.f, make_user(), w.alm, c.at(2027, 4, 1), None, "no end")
    clock.set(c.at(2027, 3, 10, 9))
    with pytest.raises(services.AccessDenied) as exc:
        services.delegate_approval(c.d, make_user(), w.alm, c.at(2027, 3, 11), c.at(2027, 3, 12), "chain")
    assert exc.value.code == "delegate_cannot_delegate"
    with pytest.raises(ValidationError, match="outlast"):
        services.delegate_approval(c.f, make_user(), w.alm, c.at(2027, 12, 1), c.at(2028, 1, 31), "too long")
    with pytest.raises(services.AccessDenied):  # S!: no self-delegation
        services.delegate_approval(c.f, c.f, w.alm, c.at(2027, 4, 1), c.at(2027, 4, 2), "me")


def test_r16_managing_partner_can_create_delegation(w, clock, cfo_setup):
    c = cfo_setup
    clock.set(c.at(2027, 5, 1, 9))
    dm = services.delegate_approval(w.mp, make_user("deputy2"), w.alm, c.at(2027, 5, 2), c.at(2027, 5, 9), "Cover",
                                    source=c.src)
    assert dm.delegated_from == c.src and dm.granted_by == w.mp


def test_r16_delegator_can_revoke_own_delegation(w, clock, cfo_setup):
    c = cfo_setup
    clock.set(c.at(2027, 3, 12, 9))  # the real present, during the delegation
    assert services.has(c.d, w.alm, "invoicing.approve_bill")
    services.revoke_membership(c.f, c.dm, "Back early")
    assert AccessLog.objects.filter(event="delegation_revoked", membership=c.dm, actor=c.f).exists()
    assert not services.has(c.d, w.alm, "invoicing.approve_bill")


def test_revocation_is_dated_now(w):
    """R9 / item 13: revoked_at is the moment of the revocation write, never back- or forward-dated."""
    m = member(make_user(), w.alm, "viewer")
    for moment in (timezone.now() - timedelta(minutes=5), timezone.now() + timedelta(minutes=5)):
        m = Membership.objects.get(pk=m.pk)
        m.revoked_at, m.revoked_by, m.revoke_reason = moment, w.mp, "dated"
        with pytest.raises(AppendOnlyError, match="takes effect now"):
            m.save()
    assert Membership.objects.get(pk=m.pk).revoked_at is None
    assert abs(services.revoke_membership(w.mp, m, "Left").revoked_at - timezone.now()) < timedelta(seconds=10)


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL-only database guard")
def test_database_rejects_back_dated_revocation(w):
    from django.db import DatabaseError

    m = member(make_user(), w.alm, "viewer")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE core_membership SET revoked_at = clock_timestamp() - interval '1 hour', revoked_by_id = %s, "
                       "revoke_reason = 'back-dated' WHERE id = %s", [w.mp.pk, m.pk])
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE core_membership SET revoked_at = clock_timestamp(), revoked_by_id = %s, "
                       "revoke_reason = 'now' WHERE id = %s", [w.mp.pk, m.pk])
    assert Membership.objects.get(pk=m.pk).revoke_reason == "now"


# --- AT-18, R17 (partial: zatca module not built) ------------------------------------

def test_at18_only_managing_partner_may_grant_key_delegation(w):
    accountant, z = make_user(), make_user()
    member(accountant, w.alm, "accountant")
    member(z, w.alm, "zatca_admin")
    assert services.require(w.mp, w.alm, "zatca.grant_key_delegation")
    for u in (accountant, z):
        with pytest.raises(services.AccessDenied):
            services.require(u, w.alm, "zatca.grant_key_delegation")
    assert services.require(z, w.alm, "zatca.onboard")
    with pytest.raises(services.AccessDenied):
        services.require(accountant, w.alm, "zatca.onboard")


# --- AT-19, AT-20, R15, R19: dual control ------------------------------------------

@pytest.fixture
def dc(w):
    z1, z2 = make_user("z1"), make_user("z2")
    member(z1, w.alm, "zatca_admin")
    member(z2, w.alm, "zatca_admin")
    target = w.alm  # stand-in for the EGS unit (zatca not built)
    payload = {"egs": "EGS-1", "to": "production"}
    req = services.request_dual_control(z1, w.alm, "zatca.switch_to_production", target, payload)
    return SimpleNamespace(z1=z1, z2=z2, target=target, payload=payload, req=req)


def test_at19_dual_control_four_eyes_and_single_use(w, dc):
    assert dc.req.status == "pending" and dc.req.expires_at - dc.req.requested_at == timedelta(hours=72)
    with pytest.raises(services.AccessDenied) as exc:
        services.decide_dual_control(dc.z1, dc.req, approve=True)
    assert exc.value.code == "self_approval"
    with pytest.raises(services.AccessDenied):  # not approved yet
        services.require_dual_control(dc.z1, w.alm, "zatca.switch_to_production", dc.req, dc.payload, dc.target)
    services.decide_dual_control(dc.z2, dc.req, approve=True, note="Checked")
    grant = services.require_dual_control(dc.z1, w.alm, "zatca.switch_to_production", dc.req, dc.payload, target=dc.target)
    assert DualControlRequest.objects.get(pk=dc.req.pk).status == "used"
    assert AccessLog.objects.get(pk=grant.log_id).event == "dual_control_used"
    with pytest.raises(services.AccessDenied):
        services.require_dual_control(dc.z1, w.alm, "zatca.switch_to_production", dc.req, dc.payload, dc.target)
    events = set(AccessLog.objects.filter(entity=w.alm).values_list("event", flat=True))
    assert {"dual_control_requested", "dual_control_decided", "dual_control_used"} <= events


def test_at19_managing_partner_may_be_second_approver(w, dc):
    services.decide_dual_control(w.mp, dc.req, approve=True)
    assert DualControlRequest.objects.get(pk=dc.req.pk).decided_by == w.mp


def test_r19_decider_must_hold_permission(w, dc):
    acc = make_user()
    member(acc, w.alm, "accountant")
    with pytest.raises(services.AccessDenied):
        services.decide_dual_control(acc, dc.req, approve=True)


def test_r19_rejected_request_cannot_be_used(w, dc):
    services.decide_dual_control(dc.z2, dc.req, approve=False, note="Not ready")
    with pytest.raises(services.AccessDenied):
        services.require_dual_control(dc.z1, w.alm, "zatca.switch_to_production", dc.req, dc.payload, dc.target)


def test_at20_other_payload_target_or_entity_refused(w, dc):
    services.decide_dual_control(dc.z2, dc.req, approve=True)
    with pytest.raises(services.AccessDenied):
        services.require_dual_control(dc.z1, w.alm, "zatca.switch_to_production", dc.req, {**dc.payload, "egs": "EGS-2"}, dc.target)
    with pytest.raises(services.AccessDenied):
        services.require_dual_control(dc.z1, w.alm, "zatca.switch_to_production", dc.req, dc.payload, target=w.sub)
    member(dc.z1, w.sub, "zatca_admin")
    with pytest.raises(services.AccessDenied):
        services.require_dual_control(dc.z1, w.sub, "zatca.switch_to_production", dc.req, dc.payload, dc.target)
    assert DualControlRequest.objects.get(pk=dc.req.pk).status == "approved"


def test_at20_expired_request_refused(w, dc, clock):
    services.decide_dual_control(dc.z2, dc.req, approve=True)
    clock.set(dc.req.requested_at + timedelta(hours=72, seconds=1))
    with pytest.raises(services.AccessDenied) as exc:
        services.require_dual_control(dc.z1, w.alm, "zatca.switch_to_production", dc.req, dc.payload, dc.target)
    assert exc.value.code == "dual_control_expired"
    assert DualControlRequest.objects.get(pk=dc.req.pk).status == "expired"


def test_r19_plain_require_refuses_dual_control_permission(w, dc):
    with pytest.raises(services.AccessUsageError):
        services.require(dc.z1, w.alm, "zatca.switch_to_production")


def test_r19_decider_cannot_equal_requester_in_db(w, dc):
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        DualControlRequest.objects.filter(pk=dc.req.pk)._transition(decided_by=dc.z1)  # the service-only path


# --- AT-23, R26: bootstrap -----------------------------------------------------------

def test_at23_bootstrap_only_once():
    alm = entity("ALM", "parent")
    first = make_user("first.mp")
    call_command("access_bootstrap", username="first.mp", entity="ALM", reason="Initial setup")
    m = Membership.objects.get(user=first, role__code="managing_partner")
    assert m.granted_by is None and services.has(first, alm, "core.grant_membership")
    row = AccessLog.objects.get(event="bootstrap")
    assert row.membership == m and row.details["os_user"]
    make_user("second.mp")
    with pytest.raises(CommandError, match="already exists"):
        call_command("access_bootstrap", username="second.mp", entity="ALM", reason="Again")
    assert Membership.objects.filter(role__code="managing_partner").count() == 1


def test_bootstrap_refuses_non_parent_and_service_users():
    alm = entity("ALM", "parent")
    entity("SUB", "full", alm)
    make_user("p")
    with pytest.raises(CommandError, match="group parent"):
        call_command("access_bootstrap", username="p", entity="SUB", reason="x")
    with pytest.raises(CommandError):
        call_command("access_bootstrap", username="system.billing", entity="ALM", reason="x")


# --- AT-25, R28: language -----------------------------------------------------------

def test_at25_arabic_message_for_arabic_user(w):
    u = make_user(language="ar")
    with pytest.raises(services.AccessDenied) as exc:
        services.require(u, w.alm, "ledger.view")
    e = exc.value
    assert str(e) == e.message_ar and ARABIC.search(e.message_ar) and "عرض دفتر الأستاذ" in e.message_ar
    assert e.message_en and not ARABIC.search(e.message_en)
    assert isinstance(e, PermissionDenied)


def test_at25_every_role_permission_and_message_is_bilingual():
    for role in Role.objects.all():
        assert role.name_en and ARABIC.search(role.name_ar), role.code
    for perm in catalogue.CATALOGUE.values():
        assert perm.label_en and ARABIC.search(perm.label_ar), perm.codename
    for code, (en, ar) in services.MESSAGES.items():
        assert en and ARABIC.search(ar), code
