"""Seed the role catalogue (access.md 3.1) and one system user per system code (R20)."""
from django.conf import settings
from django.db import migrations

from core.access.seed import ROLE_SEED_V1, SYSTEM_USERS_V1, apply_seed, apply_system_users


def forwards(apps, schema_editor):
    Role = apps.get_model("core", "Role")
    RolePermission = apps.get_model("core", "RolePermission")
    UserProfile = apps.get_model("core", "UserProfile")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    apply_seed(Role, RolePermission, ROLE_SEED_V1)
    apply_system_users(User, UserProfile, SYSTEM_USERS_V1)
    # Users that existed before this migration are humans (3.2: a profile for every user).
    for user in User.objects.filter(access_profile__isnull=True):
        UserProfile.objects.create(user=user, kind="human")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_access"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
