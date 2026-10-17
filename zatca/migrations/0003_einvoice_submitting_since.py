"""zatca finding 2 (review of 838e89b): ``EInvoice.submitting_since``, when a row last entered ``submitting``, for
the stuck-submission sweep (``ZATCA_SUBMITTING_STALE_MINUTES``)."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("zatca", "0002_immutability_triggers")]

    operations = [
        migrations.AddField(
            model_name="einvoice",
            name="submitting_since",
            field=models.DateTimeField(
                blank=True, editable=False, null=True,
                help_text="when the row last entered submitting (the stuck-submission sweep, "
                          "ZATCA_SUBMITTING_STALE_MINUTES)"),
        ),
    ]
