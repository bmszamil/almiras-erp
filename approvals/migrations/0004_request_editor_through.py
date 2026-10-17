"""Make ApprovalRequest.document_editors use an explicit append-only through model (follow-up 2b).

The through model maps onto the table Django already created for the automatic many-to-many
(same columns, same unique pair), so this changes Django's state only; the database is untouched.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0003_review_a_editors"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.CreateModel(
                    name="RequestEditor",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("approvalrequest", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="approvals.approvalrequest")),
                        ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL)),
                    ],
                    options={"db_table": "approvals_approvalrequest_document_editors"},
                ),
                migrations.AlterField(
                    model_name="approvalrequest",
                    name="document_editors",
                    field=models.ManyToManyField(blank=True, help_text="Everyone who edited the draft before submission; treated like the creator under R8/D10", related_name="+", through="approvals.RequestEditor", to=settings.AUTH_USER_MODEL),
                ),
                migrations.AddConstraint(
                    model_name="requesteditor",
                    constraint=models.UniqueConstraint(fields=("approvalrequest", "user"), name="unique_request_editor"),
                ),
            ],
        ),
    ]
