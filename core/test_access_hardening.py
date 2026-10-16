"""Access hardening (access.md A13, R9, R16, R19, R20, R26, R27, R32, R33; AT-22, AT-31, AT-32).

Sensitive entity and user settings, append-only records, dual-control
targets, delegation dates, seeded system users, log visibility and admin
choice lists. PostgreSQL-only tests are skipped on SQLite.
"""
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction

from core import services as core_services
from core.access import services
from core.access.models import (
    AccessLog,
    AppendOnlyError,
    DualControlRequest,
    GrantUse,
    Membership,
    Role,
    RolePermission,
    UserProfile,
)
from core.access.seed import SYSTEM_USERS_V1, SeedConflict, apply_seed, apply_system_users
from core.access.testing import make_agent, make_user, member
from core.models import Currency, Entity, bi, label_part
from ledger.models import Account

pytestmark = pytest.mark.django_db
postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL-only database guard")
User = get_user_model()


def entity(code, consolidation="full", parent=None):
    return Entity.objects.create(code=code, name_en=code, name_ar=f"كيان {code}", consolidation=consolidation, parent=parent)


@pytest.fixture
def w():
    alm = entity("ALM", "parent")
    sub = entity("SUB", "full", alm)
    mn = entity("MIN", "equity", alm)
    mp, acc, root = make_user("mp"), make_user("acc"), make_user("root")
    root.is_superuser = root.is_staff = True
    root.save()
    member(mp, alm, "managing_partner")
    member(acc, alm, "accountant")
    return SimpleNamespace(alm=alm, sub=sub, min=mn, mp=mp, acc=acc, root=root)


def denied(user, permission, entity=None):
    qs = AccessLog.objects.filter(event="permission_denied", actor=user, permission=permission)
    return qs.filter(entity=entity).exists() if entity is not None else qs.exists()


# --- AT-31, R32: sensitive entity settings ----------------------------------------------------

def _entity_form(e, **override):
    data = {
        "code": e.code, "name_en": e.name_en, "name_ar": e.name_ar, "legal_name_en": "", "legal_name_ar": "",
        "legal_form": "", "unified_number": "", "cr_number": "", "vat_number": "", "vat_effective_date": "",
        "vat_return_period": "", "building_no": "", "street_en": "", "street_ar": "", "district_en": "", "district_ar": "",
        "additional_no": "", "postal_code": "", "city_en": "", "city_ar": "", "country": "SA", "short_address": "",
        "ownership_pct": str(e.ownership_pct), "reporting_standard": e.reporting_standard, "presentation_currency": "",
    }
    data.update(override)
    return data


def test_at31_admin_cannot_change_sensitive_entity_fields(client, w):
    client.force_login(w.root)
    page = client.get(f"/admin/core/entity/{w.sub.pk}/change/").content.decode()
    for field in ("sharia_enforced", "consolidation", "parent", "base_currency", "is_active"):
        assert f'name="{field}"' not in page, field
    crafted = _entity_form(w.sub, name_en="SUB renamed", consolidation="none", parent="", base_currency="USD")  # sharia_enforced
    resp = client.post(f"/admin/core/entity/{w.sub.pk}/change/", crafted)  # omitted checkbox = False if it were editable
    assert resp.status_code == 302, resp.content.decode()[:2000]
    sub = Entity.objects.get(pk=w.sub.pk)
    assert sub.name_en == "SUB renamed"  # ordinary fields still edit
    assert (sub.sharia_enforced, sub.consolidation, sub.parent_id, sub.base_currency_id, sub.is_active) == (
        True, "full", w.alm.pk, "SAR", True)


def test_admin_creates_entities_through_the_service_and_logs_them(client, w):
    data = _entity_form(SimpleNamespace(code="NEW", name_en="New", name_ar="جديد", ownership_pct=100, reporting_standard="ifrs_sme"),
                        parent=w.alm.pk, consolidation="full", base_currency="SAR", is_active="on")
    client.force_login(w.root)  # a superuser without a membership cannot add entities (R24)
    assert client.get("/admin/core/entity/add/").status_code == 403
    assert client.post("/admin/core/entity/add/", data).status_code == 403
    assert not Entity.objects.filter(code="NEW").exists()
    w.mp.is_staff = True
    w.mp.save()
    client.force_login(w.mp)
    page = client.get("/admin/core/entity/add/")
    assert set(page.context["adminform"].form.fields["parent"].queryset) == {w.alm, w.sub, w.min}
    resp = client.post("/admin/core/entity/add/", data)
    assert resp.status_code == 302, resp.content.decode()[:3000]
    new = Entity.objects.get(code="NEW")
    row = AccessLog.objects.get(event="entity_settings_changed", entity=new)
    assert row.actor == w.mp and row.permission == "core.change_entity_settings" and row.details["created"] is True
    assert row.details["after"]["sharia_enforced"] is False and row.details["after"]["parent"] == "ALM"


def test_create_entity_needs_the_right_on_the_chosen_group(w):
    other = entity("OTH", "parent")
    for user, parent in ((w.acc, w.alm), (w.mp, other), (w.root, w.alm)):
        with pytest.raises(services.AccessDenied):
            core_services.create_entity(Entity(code="X1", name_en="X", name_ar="س", parent=parent), user=user)
    assert denied(w.mp, "core.change_entity_settings", other)
    with pytest.raises(ValidationError, match="parent entity"):  # new groups are set up by operators
        core_services.create_entity(Entity(code="X2", name_en="X", name_ar="س", consolidation="parent"), user=w.mp)
    with pytest.raises(ValidationError):  # the entity's own rules still apply
        core_services.create_entity(Entity(code="X3", name_en="X", name_ar="س", parent=w.sub, ownership_pct=0), user=w.mp)
    made = core_services.create_entity(Entity(code="X4", name_en="X", name_ar="س", parent=w.min), user=w.mp)
    assert made.pk and made.parent == w.min
    assert not Entity.objects.filter(code__in=["X1", "X2", "X3"]).exists()


def test_at31_entity_settings_service_needs_managing_partner_and_logs(w):
    with pytest.raises(services.AccessDenied):
        core_services.change_entity_settings(w.sub, user=w.acc, reason="x", sharia_enforced=False)
    assert denied(w.acc, "core.change_entity_settings", w.alm)
    assert Entity.objects.get(pk=w.sub.pk).sharia_enforced is True
    sub = core_services.change_entity_settings(w.sub, user=w.mp, reason="Board decision 12", sharia_enforced=False)
    assert sub.sharia_enforced is False
    row = AccessLog.objects.get(event="entity_settings_changed", entity=w.sub)
    assert row.actor == w.mp and row.permission == "core.change_entity_settings"
    assert row.details == {"before": {"sharia_enforced": True}, "after": {"sharia_enforced": False}, "reason": "Board decision 12"}


def test_entity_settings_service_covers_equity_entities_and_checks_structure(w):
    # MIN is an equity holding: the MP's group role does not cover it, but its settings are group data (R6).
    core_services.change_entity_settings(w.min, user=w.mp, reason="Sold", is_active=False)
    assert Entity.objects.get(pk=w.min.pk).is_active is False
    core_services.change_entity_settings(w.min, user=w.mp, reason="Bought back", is_active=True)
    with pytest.raises(ValidationError):  # without a parent an entity must be the group parent
        core_services.change_entity_settings(w.sub, user=w.mp, reason="x", parent=None)
    with pytest.raises(ValidationError):  # no cycles
        core_services.change_entity_settings(w.alm, user=w.mp, reason="x", parent=w.sub)
    with pytest.raises(ValidationError, match="set_functional_currency"):
        core_services.change_entity_settings(w.sub, user=w.mp, reason="x", base_currency="USD")
    with pytest.raises(ValidationError, match="reason"):
        core_services.change_entity_settings(w.sub, user=w.mp, reason="", consolidation="none")
    sub = core_services.change_entity_settings(w.sub, user=w.mp, reason="Deconsolidated", consolidation="none")
    assert sub.consolidation == "none"
    other_group = entity("OTH", "parent")
    with pytest.raises(services.AccessDenied):  # no right in the other group
        core_services.change_entity_settings(w.sub, user=w.mp, reason="x", parent=other_group)
    assert Entity.objects.get(pk=w.sub.pk).parent_id == w.alm.pk


def test_p5_top_level_group_parent_cannot_be_deactivated(w):
    """P5 (reviewer probe): deactivating ALM locked everyone out of reactivating it (inactive_entity)."""
    with pytest.raises(ValidationError, match="cannot be deactivated"):
        core_services.change_entity_settings(w.alm, user=w.mp, reason="test", is_active=False)
    assert Entity.objects.get(pk=w.alm.pk).is_active is True
    other = entity("OTH", "parent")
    with pytest.raises(ValidationError):  # nor by making a subsidiary top-level and inactive at once
        core_services.change_entity_settings(w.sub, user=w.mp, reason="x", parent=None, consolidation="parent",
                                             is_active=False)
    assert other.is_active and Entity.objects.get(pk=w.sub.pk).is_active


def test_inactive_group_parent_can_only_be_reactivated(w):
    Entity.objects.filter(pk=w.alm.pk).update(is_active=False)  # e.g. data from before this rule
    for changes in ({"sharia_enforced": False}, {"is_active": True, "sharia_enforced": False}):
        with pytest.raises(services.AccessDenied) as exc:
            core_services.change_entity_settings(w.alm, user=w.mp, reason="x", **changes)
        assert exc.value.code == "inactive_entity_reactivate_only"
    with pytest.raises(services.AccessDenied):  # nor other entities of the inactive group
        core_services.change_entity_settings(w.sub, user=w.mp, reason="x", is_active=False)
    with pytest.raises(services.AccessDenied) as exc:  # the I flag is only on core.change_entity_settings
        services.require(w.mp, Entity.objects.get(pk=w.alm.pk), "core.grant_membership")
    assert exc.value.code == "inactive_entity"
    with pytest.raises(services.AccessDenied):
        core_services.change_entity_settings(w.alm, user=w.acc, reason="x", is_active=True)
    alm = core_services.change_entity_settings(w.alm, user=w.mp, reason="Reopened", is_active=True)
    assert alm.is_active is True
    row = AccessLog.objects.get(event="entity_settings_changed", entity=w.alm)
    assert row.details["before"] == {"is_active": False} and row.details["after"] == {"is_active": True}


def test_reactivate_entity_command_is_logged_and_only_for_inactive_entities(w):
    from django.core.management import CommandError, call_command

    Entity.objects.filter(pk=w.alm.pk).update(is_active=False)
    with pytest.raises(CommandError):
        call_command("reactivate_entity", entity="ALM", reason="")
    call_command("reactivate_entity", entity="ALM", reason="Recovery after lock-out")
    assert Entity.objects.get(pk=w.alm.pk).is_active is True
    row = AccessLog.objects.get(event="entity_settings_changed", entity=w.alm)
    assert row.actor is None and row.source == "command" and row.details["os_user"]
    assert row.details["reason"] == "Recovery after lock-out"
    with pytest.raises(CommandError, match="already active"):
        call_command("reactivate_entity", entity="ALM", reason="again")
    with pytest.raises(CommandError):
        call_command("reactivate_entity", entity="NOPE", reason="x")


def test_functional_currency_change_is_logged(w):
    from ledger import services as ledger

    ledger.set_functional_currency(w.sub, "USD", user=w.mp)
    row = AccessLog.objects.get(event="entity_settings_changed", entity=w.sub)
    assert row.details == {"before": {"base_currency": "SAR"}, "after": {"base_currency": "USD"}}
    with pytest.raises(services.AccessDenied):
        ledger.set_functional_currency(w.sub, "EUR", user=w.acc)


# --- R32 for users: is_active ------------------------------------------------------------------

def test_user_admin_cannot_change_is_active(client, w):
    victim = make_user("victim")
    client.force_login(w.root)
    page = client.get(f"/admin/auth/user/{victim.pk}/change/").content.decode()
    assert 'name="is_active"' not in page
    data = {
        "username": "victim", "first_name": "", "last_name": "", "email": "", "last_login_0": "", "last_login_1": "",
        "date_joined_0": victim.date_joined.strftime("%Y-%m-%d"), "date_joined_1": victim.date_joined.strftime("%H:%M:%S"),
        "access_profile-TOTAL_FORMS": "1", "access_profile-INITIAL_FORMS": "1", "access_profile-MIN_NUM_FORMS": "0",
        "access_profile-MAX_NUM_FORMS": "1", "access_profile-0-id": victim.access_profile.pk, "access_profile-0-user": victim.pk,
    }  # is_active omitted: an unchecked box, if the field were editable
    resp = client.post(f"/admin/auth/user/{victim.pk}/change/", data)
    assert resp.status_code == 302, resp.content.decode()[:3000]
    assert User.objects.get(pk=victim.pk).is_active is True


def test_set_user_active_needs_managing_partner_and_logs(w):
    victim = make_user("victim")
    with pytest.raises(services.AccessDenied):
        services.set_user_active(w.acc, victim, False, "Left")
    assert denied(w.acc, "core.manage_users")
    with pytest.raises(services.AccessDenied):
        services.set_user_active(w.mp, w.mp, False, "Me")
    services.set_user_active(w.mp, victim, False, "Left the company")
    assert User.objects.get(pk=victim.pk).is_active is False
    row = AccessLog.objects.get(event="user_active_changed", subject_user=victim)
    assert row.actor == w.mp and row.details["before"] is True and row.details["after"] is False


# --- R33: append-only records ------------------------------------------------------------------

def test_bulk_update_and_delete_refused_on_access_records(w):
    m = Membership.objects.get(user=w.acc)
    row = AccessLog.objects.create(event="bootstrap", actor=w.mp, details={})
    for qs in (Membership.objects.filter(pk=m.pk), AccessLog.objects.filter(pk=row.pk), Membership.objects.active()):
        with pytest.raises(AppendOnlyError):
            qs.update(reason="edited")
        with pytest.raises(AppendOnlyError):
            qs.delete()
    with pytest.raises(AppendOnlyError):
        Membership.objects.update(revoked_at=services.now())
    # The controlled path (save) still revokes once.
    services.revoke_membership(w.mp, m, "Contract ended")
    assert Membership.objects.get(pk=m.pk).revoked_by == w.mp


def test_grant_use_is_append_only(w):
    bill = SimpleNamespace(pk=7, _meta=SimpleNamespace(label_lower="invoicing.bill"), created_by_id=None, submitted_by_id=None)
    grant = services.require(w.mp, w.alm, "invoicing.approve_bill", document=bill)
    use = services.use_grant(grant, target=bill)
    with pytest.raises(services.AccessDenied) as exc:
        services.use_grant(grant, target=bill)
    assert exc.value.code == "grant_used"
    with pytest.raises(AppendOnlyError):
        use.save()
    with pytest.raises(AppendOnlyError):
        use.delete()
    with pytest.raises(AppendOnlyError):
        GrantUse.objects.all().delete()
    with pytest.raises(services.AccessDenied) as exc:
        services.verify_grant(grant, w.alm, {"invoicing.approve_bill"}, document=bill)
    assert exc.value.code == "grant_used"


def test_dual_control_status_changes_only_through_services(w):
    z1 = make_user("z1")
    member(z1, w.alm, "zatca_admin")
    req = services.request_dual_control(z1, w.alm, "zatca.switch_to_production", w.alm, {"egs": "EGS-1"})
    req.status = DualControlRequest.Status.APPROVED
    with pytest.raises(AppendOnlyError):
        req.save()
    with pytest.raises(AppendOnlyError):
        DualControlRequest.objects.filter(pk=req.pk).update(status="approved", decided_by=w.mp)
    with pytest.raises(AppendOnlyError):
        DualControlRequest.objects.filter(pk=req.pk).delete()
    assert DualControlRequest.objects.get(pk=req.pk).status == "pending"


def _raw(sql, params):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@postgres_only
def test_at32_database_rejects_raw_changes_to_access_log_and_memberships(w):
    m = Membership.objects.get(user=w.acc)
    log_id = AccessLog.objects.filter(membership__isnull=True).values_list("pk", flat=True).first() or AccessLog.objects.create(
        event="bootstrap", actor=w.mp, details={}).pk
    for sql, params in (
        ("UPDATE core_accesslog SET permission = 'x' WHERE id = %s", [log_id]),
        ("DELETE FROM core_accesslog WHERE id = %s", [log_id]),
        ("UPDATE core_membership SET valid_to = now() + interval '1 day' WHERE id = %s", [m.pk]),
        ("UPDATE core_membership SET reason = 'edited' WHERE id = %s", [m.pk]),
        ("DELETE FROM core_membership WHERE id = %s", [m.pk]),
        ("UPDATE core_membership SET revoked_at = now(), role_id = role_id + 1 WHERE id = %s", [m.pk]),
    ):
        with pytest.raises(DatabaseError):
            _raw(sql, params)
    _raw("UPDATE core_membership SET revoked_at = now(), revoked_by_id = %s, revoke_reason = 'raw' WHERE id = %s", [w.mp.pk, m.pk])
    with pytest.raises(DatabaseError):  # the revocation write happens once
        _raw("UPDATE core_membership SET revoked_at = now(), revoke_reason = 'again' WHERE id = %s", [m.pk])
    assert Membership.objects.get(pk=m.pk).revoke_reason == "raw"


@postgres_only
def test_database_rejects_raw_changes_to_grant_uses(w):
    bill = SimpleNamespace(pk=7, _meta=SimpleNamespace(label_lower="invoicing.bill"), created_by_id=None, submitted_by_id=None)
    use = services.use_grant(services.require(w.mp, w.alm, "invoicing.approve_bill", document=bill), target=bill)
    for sql in ("UPDATE core_grantuse SET target_id = 8 WHERE id = %s", "DELETE FROM core_grantuse WHERE id = %s"):
        with pytest.raises(DatabaseError):
            _raw(sql, [use.pk])


# --- R19: dual control target -------------------------------------------------------------------

def test_dual_control_request_for_egs1_cannot_be_used_on_egs2(w):
    from ledger.services_setup import install_chart_of_accounts

    install_chart_of_accounts(w.alm)  # two rows to stand in for EGS units (zatca not built)
    egs1, egs2 = Account.objects.filter(entity=w.alm).order_by("pk")[:2]
    z1, z2 = make_user("z1"), make_user("z2")
    member(z1, w.alm, "zatca_admin")
    member(z2, w.alm, "zatca_admin")
    payload = {"to": "production"}  # same payload for both units: only the target differs
    req = services.request_dual_control(z1, w.alm, "zatca.switch_to_production", egs1, payload)
    services.decide_dual_control(z2, req, approve=True)
    with pytest.raises(services.AccessDenied) as exc:
        services.require_dual_control(z1, w.alm, "zatca.switch_to_production", req, payload, egs2)
    assert exc.value.code == "dual_control"
    with pytest.raises(services.AccessUsageError):
        services.require_dual_control(z1, w.alm, "zatca.switch_to_production", req, payload, None)
    with pytest.raises(TypeError):  # target is a required argument
        services.require_dual_control(z1, w.alm, "zatca.switch_to_production", req, payload)
    with pytest.raises(services.AccessUsageError):
        services.request_dual_control(z1, w.alm, "zatca.switch_to_production", None, payload)
    assert DualControlRequest.objects.get(pk=req.pk).status == "approved"
    grant = services.require_dual_control(z1, w.alm, "zatca.switch_to_production", req, payload, egs1)
    assert grant.log_id and DualControlRequest.objects.get(pk=req.pk).status == "used"


# --- R16: delegation dates -------------------------------------------------------------------------

def test_delegation_cannot_start_in_the_past(w):
    cfo, deputy = make_user("cfo"), make_user("deputy")
    member(cfo, w.alm, "approver", valid_from=services.now() - timedelta(days=30))
    now = services.now()
    with pytest.raises(services.AccessDenied) as exc:
        services.delegate_approval(cfo, deputy, w.alm, now - timedelta(days=2), now + timedelta(days=5), "Backdated")
    assert exc.value.code == "delegation_dates"
    assert not Membership.objects.filter(user=deputy).exists()
    dm = services.delegate_approval(cfo, deputy, w.alm, None, now + timedelta(days=5), "Leave")  # None = now
    assert dm.valid_from >= now
    later = services.delegate_approval(cfo, make_user("deputy2"), w.alm, services.now(), now + timedelta(days=5), "Leave")
    assert later.valid_from >= now  # "now" computed a moment earlier is accepted


# --- R20: seeded system users ----------------------------------------------------------------------

def test_seed_refuses_a_clashing_username():
    make_user("imposter")
    with pytest.raises(SeedConflict, match="imposter"):
        apply_system_users(User, UserProfile, {"billing": ("imposter", "Billing", "الفوترة")})
    assert UserProfile.objects.get(user__username="imposter").kind == "human"
    with pytest.raises(SeedConflict):  # an existing system user of another code
        apply_system_users(User, UserProfile, {"billing": (SYSTEM_USERS_V1["fx_import"][0], "x", "y")})


def test_seed_forces_unusable_password_and_no_flags():
    username = SYSTEM_USERS_V1["billing"][0]
    User.objects.filter(username=username).update(password=make_password("guessable"), is_superuser=True, is_staff=True)
    apply_system_users(User, UserProfile)
    user = User.objects.get(username=username)
    assert not user.has_usable_password() and not user.is_superuser and not user.is_staff
    assert user.access_profile.system_code == "billing"


def test_seed_is_idempotent_and_creates_missing_system_users():
    apply_system_users(User, UserProfile)  # all exist and match: nothing to refuse
    apply_system_users(User, UserProfile, {"payroll": ("system.payroll", "Payroll system user", "مستخدم نظام الرواتب")})
    user = services.system_user("payroll")
    assert user.username == "system.payroll" and user.access_profile.kind == "system"
    assert not user.has_usable_password() and not (user.is_staff or user.is_superuser)


# --- OQ-10, item 15: who sees entity-less log rows ----------------------------------------------------

def test_entity_less_log_rows_visible_on_group_parent_only(client, w):
    group_viewer, sub_viewer = make_user("gv"), make_user("sv")
    member(group_viewer, w.alm, "viewer")
    member(sub_viewer, w.sub, "viewer")
    flag_row = AccessLog.objects.filter(entity__isnull=True, event="superuser_flag_changed").latest("pk")
    assert services.visible_access_log(group_viewer).filter(pk=flag_row.pk).exists()
    assert not services.visible_access_log(sub_viewer).filter(pk=flag_row.pk).exists()
    for user, codes in ((group_viewer, (200,)), (sub_viewer, (302, 403))):
        user.is_staff = True
        user.save()
        client.force_login(user)
        assert client.get(f"/admin/core/accesslog/{flag_row.pk}/change/").status_code in codes


# --- item 14: admin choice lists ------------------------------------------------------------------------

def test_membership_admin_lists_only_own_entities_users_and_filters(client, w):
    aa = make_user("aa")
    aa.is_staff = True
    aa.save()
    member(aa, w.alm, "access_admin")
    sub_only, newcomer = make_user("sub.only"), make_user("newcomer")
    member(sub_only, w.sub, "viewer")
    client.force_login(aa)
    resp = client.get("/admin/core/membership/add/")
    choices = set(resp.context["adminform"].form.fields["user"].queryset.values_list("pk", flat=True))
    assert {newcomer.pk, w.acc.pk} <= choices
    assert sub_only.pk not in choices and aa.pk not in choices
    member(aa, w.min, "viewer")  # a second visible entity, so the filter is shown
    listing = client.get("/admin/core/membership/").content.decode()
    assert f"entity__id__exact={w.alm.pk}" in listing and f"entity__id__exact={w.min.pk}" in listing
    assert f"entity__id__exact={w.sub.pk}" not in listing  # SUB has memberships, but not visible to aa


# --- item 17 ---------------------------------------------------------------------------------------------

def test_label_part_splits_at_the_last_separator():
    assert label_part(bi("Debit / credit", "مدين"), "ar") == "مدين"
    assert label_part("Debit / credit / دائن", "ar") == "دائن"
    assert label_part("Debit / credit / دائن", "en") == "Debit / credit"
    assert label_part("English only", "ar") == "English only"


# --- item 10: access-log connection options and lock timeout ------------------------------------------

def test_access_log_options_keep_existing_libpq_options():
    from config.settings import _access_log_options

    opts = _access_log_options({"ENGINE": "django.db.backends.postgresql", "OPTIONS": {"options": "-c search_path=erp", "sslmode": "require"}})
    assert opts["options"] == "-c search_path=erp -c lock_timeout=2000" and opts["sslmode"] == "require"
    assert _access_log_options({"ENGINE": "django.db.backends.postgresql"})["options"] == "-c lock_timeout=2000"
    assert _access_log_options({"ENGINE": "django.db.backends.sqlite3"})["timeout"] == 2


def _transactional_world():
    from core.currency_seed import apply_currency_seed

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    alm = entity("ALM", "parent")
    clerk = make_user("clerk")
    member(clerk, alm, "accountant")
    return alm, clerk


@postgres_only
@pytest.mark.django_db(transaction=True, databases=["default", settings.ACCESS_LOG_DB])
def test_denial_with_entity_row_locked_falls_back_within_the_timeout():
    """R27 fallback: the log connection's FK check waits on a locked Entity row; after the 2 s lock
    timeout the row is written in the caller's transaction (and is lost if that rolls back)."""
    alm, clerk = _transactional_world()
    with transaction.atomic():
        Entity.objects.select_for_update().get(pk=alm.pk)
        started = time.monotonic()
        with pytest.raises(services.AccessDenied):
            services.require(clerk, alm, "ledger.close_period")
        elapsed = time.monotonic() - started
        assert 1.5 < elapsed < 10, elapsed
        assert AccessLog.objects.filter(event="permission_denied", actor=clerk, entity=alm).count() == 1
    assert AccessLog.objects.filter(event="permission_denied", actor=clerk, entity=alm).count() == 1  # committed with it


# --- R26: bootstrap under concurrency ---------------------------------------------------------------------

@postgres_only
@pytest.mark.django_db(transaction=True)
def test_concurrent_bootstraps_grant_one_managing_partner():
    from core.currency_seed import apply_currency_seed

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    alm = entity("ALM", "parent")
    first, second = make_user("first.mp"), make_user("second.mp")
    barrier = threading.Barrier(2)
    outcomes = []

    def run(user):
        try:
            barrier.wait(timeout=5)
            services.bootstrap_managing_partner(user, alm, "Initial setup", os_user="ops")
            outcomes.append("granted")
        except ValidationError:
            outcomes.append("refused")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(u,)) for u in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(outcomes) == ["granted", "refused"]
    assert Membership.objects.filter(role__code="managing_partner").count() == 1
