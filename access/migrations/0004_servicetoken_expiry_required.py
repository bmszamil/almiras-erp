"""Service tokens must expire (compliance). Tokens without expiry are treated as expired at creation."""
from django.db import migrations, models
from django.db.models import F


def expire_open_tokens(apps, schema_editor):
    ServiceToken = apps.get_model("access", "ServiceToken")
    ServiceToken.objects.filter(expires_at__isnull=True).update(expires_at=F("created_at"))


class Migration(migrations.Migration):
    dependencies = [("access", "0003_seed_review_permissions")]
    operations = [
        migrations.RunPython(expire_open_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="servicetoken",
            name="expires_at",
            field=models.DateTimeField(help_text="Mandatory; tokens never live forever"),
        ),
    ]
