"""Chairman role and the two-step Sharia switch-off (access.md A17, A19; R47 to R50; AT-50 to AT-52).

Turning Sharia enforcement off: a managing partner requests (first approval), the group's chairman approves
(second), a managing partner applies it once. Turning it on stays the managing partner alone (R49).
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from core import services as core_services
from core.access import catalogue, services
from core.access.models import AccessLog, DualControlRequest, Membership, Role
from core.access.seed import ROLE_SEED
from core.access.testing import make_user, member
from core.models import Entity

pytestmark = pytest.mark.django_db
postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL-only database guard")


def entity(code, consolidation="full", parent=None):
    return Entity.objects.create(code=code, name_en=code, name_ar=f"كيان {code}", consolidation=consolidation, parent=parent)


@pytest.fixture
def w():
    alm = entity("ALM", "parent")
    sub = entity("SUB", "full", alm)
    mn = entity("MIN", "equity", alm)
    mp, mp2, chair, cfo, admin = (make_user(n) for n in ("mp", "mp2", "chair", "cfo", "admin"))
    member(mp, alm, "managing_partner")
    member(mp2, alm, "managing_partner")
    member(chair, alm, "chairman")
    member(cfo, alm, "approver")
    member(admin, alm, "access_admin")
    return SimpleNamespace(alm=alm, sub=sub, min=mn, mp=mp, mp2=mp2, chair=chair, cfo=cfo, admin=admin)


def fresh(e):
    return Entity.objects.get(pk=e.pk)


def approved_request(w, target, reason="Board decision 7"):
    req = core_services.request_sharia_enforcement_off(target, user=w.mp, reason=reason)
    return core_services.decide_sharia_enforcement_off(req, user=w.chair, approve=True)


# --- catalogue and seed (3.1, 3.6) --------------------------------------------------------------------

def test_chairman_role_and_permission_are_seeded():
    role = Role.objects.get(code="chairman")
    assert (role.scope, role.kind, role.name_en, role.name_ar, role.is_seeded) == (
        "group", "human", "Chairman", "رئيس مجلس الإدارة", True)
    assert role.permission_codes == {"core.disable_sharia_enforcement"}  # nothing else (OQ-24 default)
    assert "core.disable_sharia_enforcement" in Role.objects.get(code="managing_partner").permission_codes
    perm = catalogue.get("core.disable_sharia_enforcement")
    assert perm.is_dual_control and perm.is_segregated and perm.is_group_only and perm.label_ar
    assert catalogue.dual_control_roles(perm.codename, "request") == {"managing_partner"}
    assert catalogue.dual_control_roles(perm.codename, "decide") == {"chairman"}
    assert catalogue.dual_control_roles("zatca.switch_to_production", "decide") is None  # unbound: any holder
    assert ROLE_SEED["chairman"][5] == ("core.disable_sharia_enforcement",)
    with pytest.raises(services.AccessUsageError):  # DC: never through require()
        services.require(make_user("x"), Entity.objects.create(code="P", name_en="P", name_ar="ب",
                                                                 consolidation="parent"),
                         "core.disable_sharia_enforcement")


# --- AT-50, R47: one chairman per group parent, granted by the MP only ----------------------------------

def test_at50_chairman_is_granted_by_the_mp_once_per_group_parent():
    alm = entity("ALM", "parent")
    sub = entity("SUB", "full", alm)
    m, h1, h2, admin = make_user("m"), make_user("h1"), make_user("h2"), make_user("admin")
    member(m, alm, "managing_partner")
    member(admin, alm, "access_admin")
    chairman = Role.objects.get(code="chairman")
    first = services.grant_membership(m, h1, alm, chairman, timezone.now(), reason="Appointed by the board")
    assert AccessLog.objects.filter(event="membership_granted", membership=first, actor=m).exists()
    with pytest.raises(ValidationError) as exc:  # a second holder while H1's membership is active
        services.grant_membership(m, h2, alm, chairman, timezone.now(), reason="Second chairman")
    assert exc.value.code == "single_holder" and "واحد" in str(exc.value)
    with pytest.raises(ValidationError, match="group parent"):  # R6: group roles only on a group parent
        services.grant_membership(m, h1, sub, chairman, timezone.now(), reason="On the subsidiary")
    with pytest.raises(services.AccessDenied) as exc:  # access_admin may not grant group roles (OQ-24 default)
        services.grant_membership(admin, h2, alm, chairman, timezone.now(), reason="By the access admin")
    assert exc.value.code == "role_not_grantable"
    with pytest.raises(services.AccessDenied) as exc:
        services.grant_membership(m, m, alm, chairman, timezone.now(), reason="Myself")
    assert exc.value.code == "self_grant"
    assert Membership.objects.filter(role=chairman).count() == 1
    services.revoke_membership(m, first, "Term ended")  # after revocation a new chairman can be appointed
    assert services.grant_membership(m, h2, alm, chairman, timezone.now() + timedelta(seconds=1),
                                     reason="New chairman").user == h2
    other_group = entity("OTH", "parent")  # one per group parent: another group has its own
    m2 = make_user("m2")
    member(m2, other_group, "managing_partner")
    assert services.grant_membership(m2, h1, other_group, chairman, timezone.now(), reason="Other group").pk


@postgres_only
def test_at50_database_refuses_a_second_active_chairman():
    alm = entity("ALM", "parent")
    h1, h2, grantor = make_user("h1"), make_user("h2"), make_user("g")
    member(h1, alm, "chairman")
    chairman = Role.objects.get(code="chairman")
    row = Membership(user=h2, entity=alm, role=chairman, valid_from=timezone.now(), granted_by=grantor,
                     granted_at=timezone.now(), reason="raw insert")
    with pytest.raises(DatabaseError, match="one active chairman"), transaction.atomic():
        Membership.objects.bulk_create([row])  # skips Membership.save: the trigger still refuses it


# --- AT-51, R48, R49: the two steps -------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["sub", "min", "alm"])
def test_at51_sharia_off_needs_mp_request_and_chairman_approval(w, target):
    e = getattr(w, target)
    with pytest.raises(services.AccessDenied) as exc:  # no request
        core_services.change_entity_settings(e, user=w.mp, reason="Board decision 7", sharia_enforced=False)
    assert exc.value.code == "dual_control" and exc.value.message_ar
    assert AccessLog.objects.filter(event="permission_denied", actor=w.mp,
                                    permission="core.disable_sharia_enforcement").exists()
    req = core_services.request_sharia_enforcement_off(e, user=w.mp, reason="Board decision 7")
    assert (req.entity, req.requested_by, req.status) == (w.alm, w.mp, "pending")  # on the group parent
    row = AccessLog.objects.get(event="dual_control_requested", details__request=req.pk)
    assert row.actor == w.mp and row.details["roles"] == ["managing_partner"]
    with pytest.raises(services.AccessDenied) as exc:  # M decides own request
        core_services.decide_sharia_enforcement_off(req, user=w.mp, approve=True)
    assert exc.value.code == "self_approval"
    with pytest.raises(services.AccessDenied) as exc:  # an approver (CFO) decides it
        core_services.decide_sharia_enforcement_off(req, user=w.cfo, approve=True)
    assert exc.value.code == "no_membership"
    with pytest.raises(services.AccessDenied) as exc:  # another MP holds the right, but not through chairman
        core_services.decide_sharia_enforcement_off(req, user=w.mp2, approve=True)
    assert exc.value.code == "dual_control_wrong_role" and "Chairman" in exc.value.message_en
    assert "رئيس مجلس الإدارة" in exc.value.message_ar
    with pytest.raises(services.AccessDenied):  # nobody applies a pending request
        core_services.change_entity_settings(e, user=w.mp, reason="Board decision 7", sharia_enforced=False,
                                             dual_control_request=req)
    req = core_services.decide_sharia_enforcement_off(req, user=w.chair, approve=True, note="Approved")
    assert (req.status, req.decided_by) == ("approved", w.chair)
    assert AccessLog.objects.get(event="dual_control_decided", details__request=req.pk).details["roles"] == ["chairman"]
    assert fresh(e).sharia_enforced is True  # the approval alone changes nothing
    changed = core_services.change_entity_settings(e, user=w.mp, reason="Board decision 7", sharia_enforced=False,
                                                   dual_control_request=req)
    assert changed.sharia_enforced is False and fresh(e).sharia_enforced is False
    assert DualControlRequest.objects.get(pk=req.pk).status == "used"
    assert AccessLog.objects.filter(event="dual_control_used", actor=w.mp, details__request=req.pk).exists()
    settings_row = AccessLog.objects.get(event="entity_settings_changed", entity=e)
    assert settings_row.details["after"] == {"sharia_enforced": False}
    assert settings_row.details["dual_control_request"] == req.pk
    with pytest.raises(ValidationError):  # applied again: nothing left to switch off, the request is used
        core_services.change_entity_settings(e, user=w.mp, reason="Board decision 7", sharia_enforced=False,
                                             dual_control_request=req)
    on = core_services.change_entity_settings(e, user=w.mp, reason="Compliant again", sharia_enforced=True)  # R49
    assert on.sharia_enforced is True
    assert AccessLog.objects.filter(event="entity_settings_changed", entity=e).count() == 2


def test_r48_steps_are_bound_to_their_roles(w):
    with pytest.raises(services.AccessDenied) as exc:  # the chairman cannot open the request
        core_services.request_sharia_enforcement_off(w.sub, user=w.chair, reason="x")
    assert exc.value.code == "dual_control_wrong_role"
    req = approved_request(w, w.sub)
    with pytest.raises(services.AccessDenied):  # nor apply it (no core.change_entity_settings either)
        core_services.change_entity_settings(w.sub, user=w.chair, reason="Board decision 7", sharia_enforced=False,
                                             dual_control_request=req)
    with pytest.raises(services.AccessDenied) as exc:  # the access layer itself refuses the chairman as user
        services.require_dual_control(w.chair, w.alm, "core.disable_sharia_enforcement", req,
                                      core_services.sharia_off_payload(w.sub, "Board decision 7"), target=w.sub)
    assert exc.value.code == "dual_control_wrong_role"
    # Another managing partner of the group may apply the approved request (A17: "then it applies").
    assert core_services.change_entity_settings(w.sub, user=w.mp2, reason="Board decision 7", sharia_enforced=False,
                                                dual_control_request=req).sharia_enforced is False
    with pytest.raises(services.AccessDenied):  # the requester's own approval does not count as the second
        services.decide_dual_control(w.mp, core_services.request_sharia_enforcement_off(w.min, user=w.mp, reason="y"),
                                     True)


# --- AT-52, R48: exactly the approved change, once ----------------------------------------------------------

def test_at52_payload_mixed_change_expiry_rejection_creation_and_inactive_subsidiary(w, monkeypatch):
    req = approved_request(w, w.sub, reason="X")
    with pytest.raises(services.AccessDenied) as exc:  # another reason: another payload
        core_services.change_entity_settings(w.sub, user=w.mp, reason="Y", sharia_enforced=False,
                                             dual_control_request=req)
    assert exc.value.code == "dual_control" and exc.value.message_ar
    with pytest.raises(ValidationError) as exc:  # not alone in its call
        core_services.change_entity_settings(w.sub, user=w.mp, reason="X", sharia_enforced=False, consolidation="none",
                                             dual_control_request=req)
    assert exc.value.code == "sharia_off_alone" and "الضوابط الشرعية" in str(exc.value)
    with pytest.raises(services.AccessDenied):  # the request names SUB, not MIN
        core_services.change_entity_settings(w.min, user=w.mp, reason="X", sharia_enforced=False,
                                             dual_control_request=req)
    later = timezone.now() + services.DUAL_CONTROL_TTL + timedelta(minutes=1)
    monkeypatch.setattr(services, "now", lambda: later)
    with pytest.raises(services.AccessDenied) as exc:  # after expires_at
        core_services.change_entity_settings(w.sub, user=w.mp, reason="X", sharia_enforced=False,
                                             dual_control_request=req)
    assert exc.value.code == "dual_control_expired"
    monkeypatch.undo()
    assert fresh(w.sub).sharia_enforced is True and fresh(w.sub).consolidation == "full"
    second = core_services.request_sharia_enforcement_off(w.sub, user=w.mp, reason="X")
    core_services.decide_sharia_enforcement_off(second, user=w.chair, approve=False, note="Not now")
    assert AccessLog.objects.get(event="dual_control_decided", details__request=second.pk).details["status"] == "rejected"
    with pytest.raises(services.AccessDenied) as exc:  # rejected
        core_services.change_entity_settings(w.sub, user=w.mp, reason="X", sharia_enforced=False,
                                             dual_control_request=second)
    assert exc.value.code == "dual_control"
    assert fresh(w.sub).sharia_enforced is True
    # create_entity with Sharia off: refused without a request, created with an approved one naming the code.
    with pytest.raises(services.AccessDenied) as exc:
        core_services.create_entity(Entity(code="NEW", name_en="New", name_ar="جديد", parent=w.alm,
                                           sharia_enforced=False), user=w.mp, reason="Conventional fund")
    assert exc.value.code == "dual_control" and not Entity.objects.filter(code="NEW").exists()
    creation = core_services.request_new_entity_sharia_off(w.alm, "NEW", user=w.mp, reason="Conventional fund")
    core_services.decide_sharia_enforcement_off(creation, user=w.chair, approve=True)
    with pytest.raises(services.AccessDenied):  # the request names NEW, not NEW2
        core_services.create_entity(Entity(code="NEW2", name_en="New", name_ar="جديد", parent=w.alm,
                                           sharia_enforced=False), user=w.mp, reason="Conventional fund",
                                    dual_control_request=creation)
    new = core_services.create_entity(Entity(code="NEW", name_en="New", name_ar="جديد", parent=w.alm,
                                             sharia_enforced=False), user=w.mp, reason="Conventional fund",
                                      dual_control_request=creation)
    assert new.sharia_enforced is False and DualControlRequest.objects.get(pk=creation.pk).status == "used"
    assert AccessLog.objects.get(event="entity_settings_changed", entity=new).details["dual_control_request"] == creation.pk
    # A18: the MP may change an inactive subsidiary's settings; Sharia off still needs the approved request.
    core_services.change_entity_settings(w.sub, user=w.mp, reason="Dormant", is_active=False)
    with pytest.raises(services.AccessDenied) as exc:
        core_services.change_entity_settings(w.sub, user=w.mp, reason="X", sharia_enforced=False)
    assert exc.value.code == "dual_control"
    applied = core_services.change_entity_settings(w.sub, user=w.mp, reason="X", sharia_enforced=False,
                                                   dual_control_request=req)
    assert applied.sharia_enforced is False and applied.is_active is False


def test_r48_a_request_is_only_for_switching_off(w):
    req = approved_request(w, w.sub)
    with pytest.raises(ValidationError) as exc:
        core_services.change_entity_settings(w.sub, user=w.mp, reason="x", consolidation="none",
                                             dual_control_request=req)
    assert exc.value.code == "dual_control_not_needed"
    with pytest.raises(ValidationError):
        core_services.create_entity(Entity(code="ON", name_en="On", name_ar="مفعل", parent=w.alm), user=w.mp,
                                    dual_control_request=req)
    with pytest.raises(ValidationError, match="reason"):
        core_services.request_sharia_enforcement_off(w.sub, user=w.mp, reason=" ")
    assert DualControlRequest.objects.get(pk=req.pk).status == "approved"  # nothing used it
