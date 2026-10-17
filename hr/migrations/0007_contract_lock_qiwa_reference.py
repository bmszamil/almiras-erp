"""hr R61: an approved contract version may get its Qiwa reference once, after approval (activation needs it,
Regs Art. 18). The PostgreSQL contract trigger of 0003 allowed only ``ended_on`` to change on an approved version;
it now also allows ``qiwa_contract_ref`` to be filled in when it was empty. Nothing else changes."""
from django.db import migrations

FUNCTION = r"""
CREATE OR REPLACE FUNCTION hr_contract_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'contract version % is %; only drafts are deleted (hr R15)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.approved_by_id IS NOT NULL THEN
            RAISE EXCEPTION 'contract versions are created as drafts (hr R15)';
        END IF;
        RETURN NEW;
    END IF;
    changed := hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'employee_id', 'version_no', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'contract version %: entity, employee and version never change (hr R20)', OLD.id;
    END IF;
    IF OLD.status = 'approved' THEN
        IF NOT (changed <@ ARRAY['ended_on']
                OR (changed <@ ARRAY['ended_on', 'qiwa_contract_ref', 'modified_by_id', 'modified_at']
                    AND OLD.qiwa_contract_ref = '' AND NEW.qiwa_contract_ref <> '')) THEN
            RAISE EXCEPTION 'approved contract version % is immutable (hr R15, R61)', OLD.id;
        END IF;
    ELSIF OLD.status IN ('rejected', 'withdrawn') THEN
        IF array_length(changed, 1) IS NOT NULL THEN
            RAISE EXCEPTION 'contract version % is %; it never changes', OLD.id, OLD.status;
        END IF;
    ELSIF NEW.status = 'approved' AND (NEW.approved_by_id IS NULL OR OLD.status <> 'submitted') THEN
        RAISE EXCEPTION 'contract version % is approved only from submitted, with an approver (hr R15)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)


def forward(apps, schema_editor):
    _run(schema_editor, FUNCTION)


def backward(apps, schema_editor):
    from importlib import import_module

    old = import_module("hr.migrations.0003_immutability_triggers").FORWARD
    start = old.index("CREATE OR REPLACE FUNCTION hr_contract_lock()")
    end = old.index("CREATE OR REPLACE FUNCTION hr_component_lock()")
    _run(schema_editor, old[start:end])


class Migration(migrations.Migration):
    dependencies = [("hr", "0006_contract_suspension_agreement_ref")]

    operations = [migrations.RunPython(forward, backward)]
