"""Make ApprovalRequest.document_editors use an explicit append-only through model (follow-up 2b).

The through model maps onto the table Django already created for the automatic many-to-many
(same columns, same unique pair). The only database change is renaming the unique index/constraint
on (approvalrequest_id, user_id) from Django's generated name (which differs per backend) to
`unique_request_editor`, so the database matches the migration state.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

TABLE = "approvals_approvalrequest_document_editors"
COLUMNS = ["approvalrequest_id", "user_id"]
NAME = "unique_request_editor"


def _unique_name(schema_editor):
    """Name of the existing unique index/constraint on the editor pair (not the primary key)."""
    conn = schema_editor.connection
    with conn.cursor() as cursor:
        constraints = conn.introspection.get_constraints(cursor, TABLE)
    names = [n for n, c in constraints.items()
             if c["unique"] and not c["primary_key"] and c["columns"] == COLUMNS]
    if len(names) != 1:
        raise RuntimeError(f"Expected one unique constraint on {TABLE}{tuple(COLUMNS)}, found {names}.")
    return names[0]


def _rename(schema_editor, old, new):
    if old == new:
        return
    q = schema_editor.quote_name
    if schema_editor.connection.vendor == "sqlite":
        # SQLite keeps the pair as a unique index; indexes cannot be renamed, so drop and re-create.
        schema_editor.execute(f"DROP INDEX {q(old)}")
        schema_editor.execute(
            f"CREATE UNIQUE INDEX {q(new)} ON {q(TABLE)} ({', '.join(q(c) for c in COLUMNS)})")
    else:  # PostgreSQL: renaming the constraint renames its index too
        schema_editor.execute(f"ALTER TABLE {q(TABLE)} RENAME CONSTRAINT {q(old)} TO {q(new)}")


def forwards(apps, schema_editor):
    _rename(schema_editor, _unique_name(schema_editor), NAME)


def backwards(apps, schema_editor):
    original = str(schema_editor._create_index_name(TABLE, COLUMNS, suffix="_uniq"))
    _rename(schema_editor, _unique_name(schema_editor), original)


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0003_review_a_editors"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(forwards, backwards)],
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
