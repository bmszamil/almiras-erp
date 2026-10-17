"""Billing R12 in the database (PostgreSQL only): a ``generated`` period changes only to ``failed`` with no
document **and** ``needs_release`` set, which is what a ZATCA rejection does (``billing.services._sync_period``);
before, raw SQL could move it to an automatically retried ``failed`` and bill the period again without a person
(review finding 8). Replaces ``billing_period_lock()`` from migration 0002; the reverse restores that version.
Mirrors ``BillingPeriod.save``. On SQLite this migration does nothing.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION billing_period_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'billing periods are never deleted; they are cancelled (billing R10)';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'planned' OR NEW.sales_document_id IS NOT NULL OR NEW.invoice_lines IS NOT NULL THEN
            RAISE EXCEPTION 'billing periods are created planned (billing R10)';
        END IF;
        RETURN NEW;
    END IF;
    changed := billing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'contract_id'] THEN
        RAISE EXCEPTION 'billing period %: entity and contract never change', OLD.id;
    END IF;
    IF OLD.status = 'generated' THEN
        IF changed && ARRAY['period_start', 'period_end', 'full_period_days', 'bill_date', 'proration_factor',
                            'billed_version_id', 'invoice_lines', 'generated_at'] THEN
            RAISE EXCEPTION 'billing period % is generated; its dates, lines and version never change (billing R10, R41)', OLD.id;
        END IF;
        IF NEW.status = 'generated' THEN
            IF NEW.sales_document_id IS DISTINCT FROM OLD.sales_document_id THEN
                RAISE EXCEPTION 'billing period % is billed once (billing R11)', OLD.id;
            END IF;
        ELSIF NEW.status <> 'failed' OR NEW.sales_document_id IS NOT NULL OR NOT NEW.needs_release THEN
            RAISE EXCEPTION 'a generated billing period % changes only to failed, with no document and needing release, after a ZATCA rejection (billing R12)', OLD.id;
        END IF;
    ELSIF NEW.status = 'generated' AND (NEW.sales_document_id IS NULL OR NEW.invoice_lines IS NULL
                                        OR NEW.billed_version_id IS NULL) THEN
        RAISE EXCEPTION 'billing period % is generated only with its document, lines and version (billing R52)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

REVERSE = r"""
CREATE OR REPLACE FUNCTION billing_period_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'billing periods are never deleted; they are cancelled (billing R10)';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'planned' OR NEW.sales_document_id IS NOT NULL OR NEW.invoice_lines IS NOT NULL THEN
            RAISE EXCEPTION 'billing periods are created planned (billing R10)';
        END IF;
        RETURN NEW;
    END IF;
    changed := billing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'contract_id'] THEN
        RAISE EXCEPTION 'billing period %: entity and contract never change', OLD.id;
    END IF;
    IF OLD.status = 'generated' THEN
        IF changed && ARRAY['period_start', 'period_end', 'full_period_days', 'bill_date', 'proration_factor',
                            'billed_version_id', 'invoice_lines', 'generated_at'] THEN
            RAISE EXCEPTION 'billing period % is generated; its dates, lines and version never change (billing R10, R41)', OLD.id;
        END IF;
        IF NEW.status = 'generated' THEN
            IF NEW.sales_document_id IS DISTINCT FROM OLD.sales_document_id THEN
                RAISE EXCEPTION 'billing period % is billed once (billing R11)', OLD.id;
            END IF;
        ELSIF NEW.status <> 'failed' OR NEW.sales_document_id IS NOT NULL THEN
            RAISE EXCEPTION 'a generated billing period % changes only to failed after a ZATCA rejection (billing R12)', OLD.id;
        END IF;
    ELSIF NEW.status = 'generated' AND (NEW.sales_document_id IS NULL OR NEW.invoice_lines IS NULL
                                        OR NEW.billed_version_id IS NULL) THEN
        RAISE EXCEPTION 'billing period % is generated only with its document, lines and version (billing R52)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    _run(schema_editor, REVERSE)


class Migration(migrations.Migration):
    dependencies = [("billing", "0003_contract_header_lock")]

    operations = [migrations.RunPython(forwards, backwards)]
