"""PostgreSQL guards of posted runs and events (review of ccb9cef, item 10; fixed-assets R18, R30, R42).

- A posted (or reversed) depreciation run keeps its period, number, fingerprint, total, poster and entry; its status
  only goes posted -> reversed, with the reversal entry and the reversal request fields. A reversed run never changes
  status again.
- A posted or approved event keeps its kind, asset, date, amounts, links and estimate values; its status only goes
  posted / approved -> reversed. Its journal entry is set once.
- No line is inserted into, changed in or deleted from a posted or reversed run.

Replaces the generic keep trigger of 0002 on these two tables (deletes and entity changes stay refused). Nothing runs
on SQLite (the services and model guards apply there).
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
           OR (OLD.journal_entry_id IS NOT NULL AND NEW.journal_entry_id IS DISTINCT FROM OLD.journal_entry_id)
           OR (OLD.reversal_entry_id IS NOT NULL AND NEW.reversal_entry_id IS DISTINCT FROM OLD.reversal_entry_id) THEN
            RAISE EXCEPTION 'posted depreciation run % keeps its amounts and entry (R18, R42)', OLD.id;
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
           OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
           OR NEW.new_method <> OLD.new_method OR NEW.new_vat_use <> OLD.new_vat_use
           OR NEW.new_useful_life_months IS DISTINCT FROM OLD.new_useful_life_months
           OR NEW.new_annual_rate IS DISTINCT FROM OLD.new_annual_rate
           OR NEW.new_residual_value IS DISTINCT FROM OLD.new_residual_value
           OR NEW.posted_by_id IS DISTINCT FROM OLD.posted_by_id OR NEW.posted_at IS DISTINCT FROM OLD.posted_at
           OR (OLD.journal_entry_id IS NOT NULL AND NEW.journal_entry_id IS DISTINCT FROM OLD.journal_entry_id)
           OR (OLD.reversal_entry_id IS NOT NULL AND NEW.reversal_entry_id IS DISTINCT FROM OLD.reversal_entry_id) THEN
            RAISE EXCEPTION 'posted or approved asset event % keeps its values and entry (R18, R42)', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fixed_assets_line_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'depreciation line % is never deleted (R42)', OLD.id;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.entity_id <> OLD.entity_id OR NEW.run_id <> OLD.run_id OR NEW.asset_id <> OLD.asset_id THEN
            RAISE EXCEPTION 'depreciation line % keeps its entity, run and asset (R30, R42)', OLD.id;
        END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM fixed_assets_depreciationrun r WHERE r.id = NEW.run_id
               AND r.status IN ('posted', 'reversed')) THEN
        RAISE EXCEPTION 'lines of a posted run never change and none are added (R42): run %', NEW.run_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS fixed_assets_depreciationrun_keep ON fixed_assets_depreciationrun;
CREATE TRIGGER fixed_assets_depreciationrun_keep BEFORE UPDATE OR DELETE ON fixed_assets_depreciationrun
    FOR EACH ROW EXECUTE FUNCTION fixed_assets_run_lock();
DROP TRIGGER IF EXISTS fixed_assets_assetevent_keep ON fixed_assets_assetevent;
CREATE TRIGGER fixed_assets_assetevent_keep BEFORE UPDATE OR DELETE ON fixed_assets_assetevent
    FOR EACH ROW EXECUTE FUNCTION fixed_assets_event_lock();
DROP TRIGGER IF EXISTS fixed_assets_depreciationline_keep ON fixed_assets_depreciationline;
CREATE TRIGGER fixed_assets_depreciationline_keep BEFORE INSERT OR UPDATE OR DELETE ON fixed_assets_depreciationline
    FOR EACH ROW EXECUTE FUNCTION fixed_assets_line_lock();
"""

BACK = r"""
DROP TRIGGER IF EXISTS fixed_assets_depreciationrun_keep ON fixed_assets_depreciationrun;
CREATE TRIGGER fixed_assets_depreciationrun_keep BEFORE UPDATE OR DELETE ON fixed_assets_depreciationrun
    FOR EACH ROW EXECUTE FUNCTION fixed_assets_keep();
DROP TRIGGER IF EXISTS fixed_assets_assetevent_keep ON fixed_assets_assetevent;
CREATE TRIGGER fixed_assets_assetevent_keep BEFORE UPDATE OR DELETE ON fixed_assets_assetevent
    FOR EACH ROW EXECUTE FUNCTION fixed_assets_keep();
DROP TRIGGER IF EXISTS fixed_assets_depreciationline_keep ON fixed_assets_depreciationline;
CREATE TRIGGER fixed_assets_depreciationline_keep BEFORE UPDATE OR DELETE ON fixed_assets_depreciationline
    FOR EACH ROW EXECUTE FUNCTION fixed_assets_line_lock();
DROP FUNCTION IF EXISTS fixed_assets_run_lock();
DROP FUNCTION IF EXISTS fixed_assets_event_lock();
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, SQL)


def backwards(apps, schema_editor):
    original = importlib.import_module("fixed_assets.migrations.0002_keep_everything_triggers")
    _run(schema_editor, BACK + original.FUNCTIONS)  # 0002's own fixed_assets_line_lock() body again


class Migration(migrations.Migration):
    dependencies = [("fixed_assets", "0003_post_review_fields")]

    operations = [migrations.RunPython(forwards, backwards)]
