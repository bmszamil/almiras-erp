"""Owner decision H1 (ledger-fx R12 amendment, AT-44): proposed non-publication days are loaded unverified and
verified by a second person; ``get_sama_rate`` bridges only across verified days. Days recorded before this
migration were entered by hand from the announcement (``create_non_publication_day``), so they count as verified by
the person who entered them, at the time they were entered (the rule for hand-entered days from now on too)."""
from django.db.models import F

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def verify_existing(apps, schema_editor):
    Day = apps.get_model("ledger", "NonPublicationDay")
    Day.objects.filter(verified_at__isnull=True).update(verified_by=F("created_by"), verified_at=F("created_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0018_sama_non_publication_days"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="nonpublicationday",
            name="source",
            field=models.CharField(
                blank=True, max_length=300, verbose_name="Source / المصدر"
            ),
        ),
        migrations.AddField(
            model_name="nonpublicationday",
            name="verified_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Verified at / تاريخ التحقق"
            ),
        ),
        migrations.AddField(
            model_name="nonpublicationday",
            name="verified_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Verified by / تحقق منه",
            ),
        ),
        migrations.AddConstraint(
            model_name="nonpublicationday",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("verified_by__isnull", True), ("verified_at__isnull", True)
                    ),
                    models.Q(
                        ("verified_by__isnull", False), ("verified_at__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="non_publication_day_verified_complete",
            ),
        ),
        # After the constraint: no ALTER TABLE follows the update (PostgreSQL pending deferred FK trigger events).
        migrations.RunPython(verify_existing, migrations.RunPython.noop),
    ]
