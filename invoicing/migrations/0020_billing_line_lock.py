"""Billing lines read-only in the database (invoicing R73, billing R56; review finding 5).

Replaces ``invoicing_line_lock`` (migration 0007) on PostgreSQL. For sales-document lines it now also refuses
every insert, update and delete of a line whose document (old or new) has a ``billing_item`` or a
``billing_adjustment``, unless the transaction-local setting ``invoicing.billing_lines`` is ``on``: only
``invoicing.models.billing_line_writes()`` sets it (``set_config(..., true)``, i.e. SET LOCAL), around the billing
hooks, the approval's re-pricing and the discard of a correction draft. Everything else is as in 0007. On SQLite the
model and queryset guards apply (including ``bulk_create``).
"""
from django.db import migrations

BODY = r"""
CREATE OR REPLACE FUNCTION invoicing_line_lock() RETURNS trigger AS $$
-- Document lines. TG_ARGV[0] = the document table.
DECLARE
    doc_status text;
    changed text[];
BEGIN
    IF TG_ARGV[0] = 'invoicing_salesdocument'
       AND COALESCE(current_setting('invoicing.billing_lines', true), '') <> 'on' THEN
        IF (TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
                SELECT 1 FROM invoicing_salesdocument d WHERE d.id = OLD.document_id
                   AND (d.billing_item_id IS NOT NULL OR d.billing_adjustment_id IS NOT NULL)))
           OR (TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
                SELECT 1 FROM invoicing_salesdocument d WHERE d.id = NEW.document_id
                   AND (d.billing_item_id IS NOT NULL OR d.billing_adjustment_id IS NOT NULL))) THEN
            RAISE EXCEPTION 'lines of a document created from billing are written only by the billing hooks (R73, billing R56)';
        END IF;
    END IF;
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        EXECUTE format('SELECT status FROM %I WHERE id = $1 FOR SHARE', TG_ARGV[0]) INTO doc_status USING OLD.document_id;
        IF TG_OP = 'UPDATE' AND doc_status = 'submitted' AND NEW.document_id = OLD.document_id THEN
            changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
            IF NOT changed <@ ARRAY['vat_rate_id', 'vat_amount', 'net_amount', 'vat_recoverable'] THEN
                RAISE EXCEPTION 'line % of a submitted document is only re-priced at approval', OLD.id;
            END IF;
            RETURN NEW;
        END IF;
        IF doc_status IS NOT NULL AND doc_status <> 'draft' THEN
            RAISE EXCEPTION 'line % belongs to a % document; it never changes (R13, R24)', OLD.id, doc_status;
        END IF;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        EXECUTE format('SELECT status FROM %I WHERE id = $1 FOR SHARE', TG_ARGV[0]) INTO doc_status USING NEW.document_id;
        IF doc_status IS NOT NULL AND doc_status <> 'draft' THEN
            RAISE EXCEPTION 'lines are not added to or moved onto a % document', doc_status;
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, BODY)


def backwards(apps, schema_editor):
    from importlib import import_module

    forward = import_module("invoicing.migrations.0007_document_lock_triggers").FORWARD
    start = forward.index("CREATE OR REPLACE FUNCTION invoicing_line_lock()")
    end = forward.index("CREATE OR REPLACE FUNCTION invoicing_application_lock()")
    _run(schema_editor, forward[start:end])


class Migration(migrations.Migration):
    dependencies = [("invoicing", "0019_treaty_certificate_guards")]

    operations = [migrations.RunPython(forwards, backwards)]
