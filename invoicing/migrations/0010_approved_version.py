"""The version a sales document was approved at (invoicing R87, AT-89).

``approved_version`` is written by ``approve_sales_document`` together with the issue; ``post_cleared_document``
refuses when the locked row's ``version`` differs, instead of comparing ``modified_at`` with the approval time,
which came from two app-server clocks. Documents approved before this migration get their current version (an
issued document's version no longer changes). On PostgreSQL a trigger lets only the approval write the field, once,
equal to the version; the backfill runs with the lock trigger of migration 0009 disabled for that one statement.
"""

from django.db import migrations, models

FORWARD = r"""
CREATE FUNCTION invoicing_salesdocument_approved_version() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.approved_version IS NOT NULL THEN
            RAISE EXCEPTION 'sales documents are created without an approved version (R87)';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.approved_version IS DISTINCT FROM OLD.approved_version
       AND NOT (OLD.status = 'submitted' AND NEW.status = 'issued' AND OLD.approved_version IS NULL
                AND NEW.approved_version = NEW.version) THEN
        RAISE EXCEPTION 'sales document %: the approved version is written once, by approval, equal to the version (R87)',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER invoicing_salesdocument_approved_version BEFORE INSERT OR UPDATE ON invoicing_salesdocument
    FOR EACH ROW EXECUTE FUNCTION invoicing_salesdocument_approved_version();
"""

BACKWARD = r"""
DROP TRIGGER IF EXISTS invoicing_salesdocument_approved_version ON invoicing_salesdocument;
DROP FUNCTION IF EXISTS invoicing_salesdocument_approved_version();
"""

APPROVED = ("issued", "final", "rejected")


def backfill_postgresql(cursor):
    """Approved documents get their current version; the 0009 lock trigger refuses that write, so it is disabled
    for this one statement. Deferred foreign-key checks run first: ALTER TABLE refuses pending trigger events."""
    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    cursor.execute("ALTER TABLE invoicing_salesdocument DISABLE TRIGGER invoicing_salesdocument_lock")
    cursor.execute("UPDATE invoicing_salesdocument SET approved_version = version "
                   "WHERE status = ANY(%s) AND approved_version IS NULL", [list(APPROVED)])
    cursor.execute("ALTER TABLE invoicing_salesdocument ENABLE TRIGGER invoicing_salesdocument_lock")
    cursor.execute("SET CONSTRAINTS ALL DEFERRED")


def forwards(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            backfill_postgresql(cursor)
            cursor.execute(FORWARD)  # no parameters: the "%" in RAISE messages is not a placeholder
    else:
        SalesDocument = apps.get_model("invoicing", "SalesDocument")
        SalesDocument.objects.filter(status__in=APPROVED).update(approved_version=models.F("version"))


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(BACKWARD)


class Migration(migrations.Migration):

    dependencies = [
        ("invoicing", "0009_document_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesdocument",
            name="approved_version",
            field=models.PositiveIntegerField(
                blank=True,
                editable=False,
                help_text="The version the approval was checked against; the posting on clearance needs the same (R87) / النسخة التي فُحص الاعتماد عليها؛ لا يُرحَّل المستند عند اعتماد الهيئة إلا بالنسخة نفسها",
                null=True,
                verbose_name="Version approved / النسخة المعتمدة",
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
