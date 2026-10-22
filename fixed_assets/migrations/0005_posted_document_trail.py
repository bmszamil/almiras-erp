"""PostgreSQL guards of posted runs and events, second pass (follow-up review of acb5701; fixed-assets R18, R42).

On top of 0004, a posted or reversed run also keeps its maker trail (created by and at, submitted by and at), and a
posted, approved or reversed event also keeps its evidence (cash cost price, financing total and contract, recoverable
amount, source journal line, reason, evidence reference), its old VAT use and its maker trail. ``previous_values`` of
an event is written once: only while it is still empty (``{}``), which is when a change of estimate is applied.

Nothing runs on SQLite (the services and model guards apply there). Backwards restores 0004's functions.
"""
import importlib

from django.db import migrations

SQL = r"""
CREATE OR REPLACE FUNCTION fixed_assets_run_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'fixed-asset records are never deleted (R42): % %', TG_TABLE_NAME, OLD.id;
    END IF;
    IF NEW.entity_id <> OLD.entity_id THEN
        RAISE EXCEPTION 'the entity of a fixed-asset record never changes (R30): % %', TG_TABLE_NAME, OLD.id;
    END IF;
    IF OLD.status IN ('posted', 'reversed') THEN
        IF NOT (NEW.status = OLD.status OR (OLD.status = 'posted' AND NEW.status = 'reversed')) THEN
            RAISE EXCEPTION 'posted depreciation run % only becomes reversed (R18, R42)', OLD.id;
        END IF;
        IF NEW.period_id <> OLD.period_id OR NEW.number <> OLD.number OR NEW.fingerprint <> OLD.fingerprint
           OR NEW.total <> OLD.total OR NEW.posted_by_id IS DISTINCT FROM OLD.posted_by_id
           OR NEW.posted_at IS DISTINCT FROM OLD.posted_at
           OR NEW.created_by_id IS DISTINCT FROM OLD.created_by_id OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.submitted_by_id IS DISTINCT FROM OLD.submitted_by_id
           OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at
           OR (OLD.journal_entry_id IS NOT NULL AND NEW.journal_entry_id IS DISTINCT FROM OLD.journal_entry_id)
           OR (OLD.reversal_entry_id IS NOT NULL AND NEW.reversal_entry_id IS DISTINCT FROM OLD.reversal_entry_id) THEN
            RAISE EXCEPTION 'posted depreciation run % keeps its amounts, entry and maker trail (R18, R42)', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fixed_assets_event_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'fixed-asset records are never deleted (R42): % %', TG_TABLE_NAME, OLD.id;
    END IF;
    IF NEW.entity_id <> OLD.entity_id THEN
        RAISE EXCEPTION 'the entity of a fixed-asset record never changes (R30): % %', TG_TABLE_NAME, OLD.id;
    END IF;
    IF OLD.status IN ('posted', 'approved', 'reversed') THEN
        IF NOT (NEW.status = OLD.status OR (OLD.status IN ('posted', 'approved') AND NEW.status = 'reversed')) THEN
            RAISE EXCEPTION 'posted or approved asset event % only becomes reversed (R18, R42)', OLD.id;
        END IF;
        IF NEW.kind <> OLD.kind OR NEW.asset_id <> OLD.asset_id OR NEW.event_date <> OLD.event_date
           OR NEW.amount IS DISTINCT FROM OLD.amount
           OR NEW.cost_derecognised IS DISTINCT FROM OLD.cost_derecognised
           OR NEW.depreciation_derecognised IS DISTINCT FROM OLD.depreciation_derecognised
           OR NEW.impairment_derecognised IS DISTINCT FROM OLD.impairment_derecognised
           OR NEW.depreciation_until IS DISTINCT FROM OLD.depreciation_until
           OR NEW.target_asset_id IS DISTINCT FROM OLD.target_asset_id
           OR NEW.purchase_line_id IS DISTINCT FROM OLD.purchase_line_id
           OR NEW.reverses_component_id IS DISTINCT FROM OLD.reverses_component_id
           OR NEW.source_journal_line_id IS DISTINCT FROM OLD.source_journal_line_id
           OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
           OR NEW.new_method <> OLD.new_method OR NEW.new_vat_use <> OLD.new_vat_use
           OR NEW.old_vat_use <> OLD.old_vat_use
           OR NEW.new_useful_life_months IS DISTINCT FROM OLD.new_useful_life_months
           OR NEW.new_annual_rate IS DISTINCT FROM OLD.new_annual_rate
           OR NEW.new_residual_value IS DISTINCT FROM OLD.new_residual_value
           OR NEW.cash_cost_price IS DISTINCT FROM OLD.cash_cost_price
           OR NEW.financing_total IS DISTINCT FROM OLD.financing_total
           OR NEW.financing_ref <> OLD.financing_ref
           OR NEW.recoverable_amount IS DISTINCT FROM OLD.recoverable_amount
           OR NEW.reason_ar <> OLD.reason_ar OR NEW.reason_en <> OLD.reason_en
           OR NEW.evidence_ref <> OLD.evidence_ref
           OR (OLD.previous_values <> '{}'::jsonb AND NEW.previous_values IS DISTINCT FROM OLD.previous_values)
           OR NEW.created_by_id IS DISTINCT FROM OLD.created_by_id OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.submitted_by_id IS DISTINCT FROM OLD.submitted_by_id
           OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at
           OR NEW.posted_by_id IS DISTINCT FROM OLD.posted_by_id OR NEW.posted_at IS DISTINCT FROM OLD.posted_at
           OR (OLD.journal_entry_id IS NOT NULL AND NEW.journal_entry_id IS DISTINCT FROM OLD.journal_entry_id)
           OR (OLD.reversal_entry_id IS NOT NULL AND NEW.reversal_entry_id IS DISTINCT FROM OLD.reversal_entry_id) THEN
            RAISE EXCEPTION 'posted or approved asset event % keeps its values, evidence, entry and maker trail (R18, R42)',
                OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, SQL)


def backwards(apps, schema_editor):
    previous = importlib.import_module("fixed_assets.migrations.0004_posted_document_triggers")
    _run(schema_editor, previous.SQL)  # 0004's function bodies (its trigger statements are idempotent)


class Migration(migrations.Migration):
    dependencies = [("fixed_assets", "0004_posted_document_triggers")]

    operations = [migrations.RunPython(forwards, backwards)]
