"""Billing R7, R39 (OQ-23 default) in the database (PostgreSQL only): once a contract leaves ``draft`` its entity,
customer, currency, number, start date, due days (version-1 value), lead days and counterparty entity never change,
and it is never deleted (review finding 7). Mirrors ``Contract.save`` / ``Contract.delete``; versions carry every
later change of terms. On SQLite this migration does nothing; the model guard applies.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION billing_contract_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'billing contract % is %; approved contracts are never deleted (billing R7)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    changed := billing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'billing contract %: entity and creation never change', OLD.id;
    END IF;
    IF OLD.status <> 'draft' AND changed && ARRAY['customer_id', 'currency_id', 'due_days', 'bill_lead_days',
                                                  'start_date', 'counterparty_entity_id', 'number'] THEN
        RAISE EXCEPTION 'billing contract % is %; customer, currency, number, start, due days, lead days and counterparty never change (billing R7, R39)', OLD.id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER billing_contract_lock BEFORE UPDATE OR DELETE ON billing_contract
    FOR EACH ROW EXECUTE FUNCTION billing_contract_lock();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS billing_contract_lock ON billing_contract;
DROP FUNCTION IF EXISTS billing_contract_lock();
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
    dependencies = [("billing", "0002_immutability_triggers")]

    operations = [migrations.RunPython(forwards, backwards)]
