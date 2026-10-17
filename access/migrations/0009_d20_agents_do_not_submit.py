"""D20 (17 Oct 2026): AI agents and integrations create drafts only and never submit for approval.

Removes approvals.request.submit from the system role agent_drafter. has_perm strips it from every
service user anyway (SERVICE_FORBIDDEN); this keeps the role consistent with the decision.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Role = apps.get_model("access", "Role")
    Permission = apps.get_model("access", "Permission")
    role = Role.objects.filter(code="agent_drafter").first()
    perm = Permission.objects.filter(code="approvals.request.submit").first()
    if role is not None and perm is not None:
        role.permissions.remove(perm)


def backwards(apps, schema_editor):
    Role = apps.get_model("access", "Role")
    Permission = apps.get_model("access", "Permission")
    role = Role.objects.filter(code="agent_drafter").first()
    perm = Permission.objects.filter(code="approvals.request.submit").first()
    if role is not None and perm is not None:
        role.permissions.add(perm)


class Migration(migrations.Migration):
    dependencies = [("access", "0008_user_manager_guards_system_user")]
    operations = [migrations.RunPython(forwards, backwards)]
