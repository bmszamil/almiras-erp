"""PostgreSQL guards of the register (fixed-assets R30, R42; like invoicing 0034):

- no row of any fixed-asset table is ever deleted (also not by bulk delete or SQL);
- ``entity_id`` never changes;
- cost components keep their asset, kind, bill line and amount (only the deferred-payment flag follows the settings,
  R4a); fair values and reminders are append-only; a posted run's lines never change.

Nothing runs on SQLite (the model guards apply there).
"""
from django.db import migrations

TABLES = ("assetcategory", "assetlocation", "fixedassetsettings", "depreciationpolicy", "fixedasset",
          "assetcostcomponent", "depreciationrun", "depreciationline", "assetevent", "ipfairvalue", "reviewreminder")

FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION fixed_assets_keep() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'fixed-asset records are never deleted (R42): % %', TG_TABLE_NAME, OLD.id;
    END IF;
    IF NEW.entity_id <> OLD.entity_id THEN
        RAISE EXCEPTION 'the entity of a fixed-asset record never changes (R30): % %', TG_TABLE_NAME, OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fixed_assets_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% rows are written once and never changed or deleted (R42)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fixed_assets_component_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.asset_id <> OLD.asset_id OR NEW.kind <> OLD.kind OR NEW.amount <> OLD.amount
       OR NEW.purchase_line_id IS DISTINCT FROM OLD.purchase_line_id OR NEW.entity_id <> OLD.entity_id THEN
        RAISE EXCEPTION 'asset cost component % never changes and is never deleted (R4, R42)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fixed_assets_line_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'depreciation line % is never deleted (R42)', OLD.id;
    END IF;
    IF NEW.entity_id <> OLD.entity_id OR NEW.run_id <> OLD.run_id OR NEW.asset_id <> OLD.asset_id THEN
        RAISE EXCEPTION 'depreciation line % keeps its entity, run and asset (R30, R42)', OLD.id;
    END IF;
    IF EXISTS (SELECT 1 FROM fixed_assets_depreciationrun r WHERE r.id = OLD.run_id
               AND r.status IN ('posted', 'reversed')) THEN
        RAISE EXCEPTION 'depreciation line % of a posted run never changes (R42)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _triggers():
    sql = [FUNCTIONS]
    special = {"assetcostcomponent": "fixed_assets_component_lock", "depreciationline": "fixed_assets_line_lock",
               "ipfairvalue": "fixed_assets_append_only", "reviewreminder": "fixed_assets_append_only"}
    for table in TABLES:
        name = f"fixed_assets_{table}"
        fn = special.get(table, "fixed_assets_keep")
        sql.append(f"DROP TRIGGER IF EXISTS {name}_keep ON {name};")
        sql.append(f"CREATE TRIGGER {name}_keep BEFORE UPDATE OR DELETE ON {name} "
                   f"FOR EACH ROW EXECUTE FUNCTION {fn}();")
    return "\n".join(sql)


def _drop():
    sql = [f"DROP TRIGGER IF EXISTS fixed_assets_{t}_keep ON fixed_assets_{t};" for t in TABLES]
    sql += ["DROP FUNCTION IF EXISTS fixed_assets_keep();", "DROP FUNCTION IF EXISTS fixed_assets_append_only();",
            "DROP FUNCTION IF EXISTS fixed_assets_component_lock();", "DROP FUNCTION IF EXISTS fixed_assets_line_lock();"]
    return "\n".join(sql)


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, _triggers())


def backwards(apps, schema_editor):
    _run(schema_editor, _drop())


class Migration(migrations.Migration):
    dependencies = [("fixed_assets", "0001_initial")]

    operations = [migrations.RunPython(forwards, backwards)]
