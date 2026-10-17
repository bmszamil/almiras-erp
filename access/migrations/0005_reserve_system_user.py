"""Create the ERP system user up front so the 'system' username can never be taken by another account
(the ledger treats only this user, or no user, as a module-service context — review C)."""
from django.contrib.auth.hashers import make_password
from django.db import migrations


def forwards(apps, schema_editor):
    User = apps.get_model("access", "User")
    User.objects.get_or_create(
        username="system",
        defaults={"full_name_en": "ERP system", "full_name_ar": "نظام تخطيط الموارد",
                  "user_type": "service_integration", "password": make_password(None), "is_active": True},
    )


class Migration(migrations.Migration):
    dependencies = [("access", "0004_servicetoken_expiry_required")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
