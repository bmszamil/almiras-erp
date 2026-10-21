"""ROLE_SEED_V17, SYSTEM_USERS_V3, the approval activity catalogue and one EntityGovernance row per entity
(access.md §10; R60, R67, R70, R76, R84, R89; OQ-42 to OQ-53 defaults).

- New roles: ceo, internal_auditor, external_auditor, business_analyst, personal_assistant, board_representative,
  system_governance (see core/access/seed.py V17 for each permission set).
- managing_partner, chairman, access_admin, approver: the §10 rights.
- System user ``system.governance`` (Sys:governance); it acts only in entities where it gets a membership (setup).
- ``ApprovalActivity`` rows mirror core/access/authority_defaults.py.
- ``EntityGovernance`` for each existing entity: top authority ``managing_partner`` for a top-level group parent,
  ``ceo`` otherwise (R67); report recipients the internal auditor and the top-authority role (OQ-33);
  self-approval on (10.3 default).
"""
from django.conf import settings
from django.db import migrations

from core.access import authority_defaults
from core.access.seed import ROLE_SEED_V16, ROLE_SEED_V17, SYSTEM_USERS_V3, apply_seed, apply_system_users


def seed(apps, schema_editor):
    Role = apps.get_model("core", "Role")
    apply_seed(Role, apps.get_model("core", "RolePermission"), ROLE_SEED_V17)
    apply_system_users(apps.get_model(*settings.AUTH_USER_MODEL.split(".")), apps.get_model("core", "UserProfile"),
                       {"governance": SYSTEM_USERS_V3["governance"]})
    authority_defaults.sync(apps.get_model("core", "ApprovalActivity"))
    Entity = apps.get_model("core", "Entity")
    EntityGovernance = apps.get_model("core", "EntityGovernance")
    auditor = Role.objects.get(code="internal_auditor")
    for entity in Entity.objects.all():
        if EntityGovernance.objects.filter(entity=entity).exists():
            continue
        top = "managing_partner" if entity.parent_id is None and entity.consolidation == "parent" else "ceo"
        role = Role.objects.get(code=top)
        gov = EntityGovernance.objects.create(entity=entity, top_authority_role=role, self_approval_enabled=True)
        gov.governance_report_recipients.set([auditor, role])


def unseed(apps, schema_editor):
    apps.get_model("core", "EntityGovernance").objects.all().delete()
    apps.get_model("core", "ApprovalActivity").objects.all().delete()
    Role = apps.get_model("core", "Role")
    apply_seed(Role, apps.get_model("core", "RolePermission"), ROLE_SEED_V16)
    Role.objects.filter(code__in=set(ROLE_SEED_V17) - set(ROLE_SEED_V16)).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("core", "0027_authority_matrix")]

    operations = [migrations.RunPython(seed, unseed)]
