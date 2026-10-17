"""hr R7: statutory parameter rows are immutable once verified, superseded or used.

- CHECK constraints: the verifier is never the person who entered the row; a verified row names its verifier and
  the time; an unverified row has neither. The (jurisdiction, code, valid_from) key ignores superseded rows so a
  replacement may start on the same day as the row it supersedes.
- PostgreSQL trigger ``hr_parameter_lock`` (replaces the 0003 function, also on INSERT): rows are inserted
  unverified and unused; ``used_at`` goes only from NULL to a value; status only moves unverified -> verified ->
  superseded (or unverified -> superseded); the verifier and the verification time are set once; the figure and
  its source (jurisdiction, code, value, dates, source, entered_by) never change on a verified, superseded or used
  row. On SQLite the model guard (``StatutoryParameter.save``) and the CHECK constraints apply.
"""
from django.conf import settings
from django.db import migrations, models

FUNCTION = r"""
CREATE OR REPLACE FUNCTION hr_parameter_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'statutory parameters are never deleted (hr R7)';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'unverified' OR NEW.verified_by_id IS NOT NULL OR NEW.verified_at IS NOT NULL
                OR NEW.used_at IS NOT NULL THEN
            RAISE EXCEPTION 'statutory parameters are entered unverified and unused (hr R7)';
        END IF;
        RETURN NEW;
    END IF;
    changed := hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'created_at'] THEN
        RAISE EXCEPTION 'statutory parameter %: id and creation time never change (hr R7)', OLD.id;
    END IF;
    IF OLD.used_at IS NOT NULL AND NEW.used_at IS DISTINCT FROM OLD.used_at THEN
        RAISE EXCEPTION 'statutory parameter %: the first use is recorded once (hr R7)', OLD.id;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
            (OLD.status = 'unverified' AND NEW.status IN ('verified', 'superseded'))
            OR (OLD.status = 'verified' AND NEW.status = 'superseded')) THEN
        RAISE EXCEPTION 'statutory parameter %: status % cannot become % (hr R7)', OLD.id, OLD.status, NEW.status;
    END IF;
    IF (OLD.verified_by_id IS NOT NULL AND NEW.verified_by_id IS DISTINCT FROM OLD.verified_by_id)
            OR (OLD.verified_at IS NOT NULL AND NEW.verified_at IS DISTINCT FROM OLD.verified_at) THEN
        RAISE EXCEPTION 'statutory parameter % is verified once (hr R7)', OLD.id;
    END IF;
    IF NEW.verified_by_id IS NOT NULL AND NEW.verified_by_id = NEW.entered_by_id THEN
        RAISE EXCEPTION 'statutory parameter %: the verifier is not the person who entered it (hr R7)', OLD.id;
    END IF;
    IF (OLD.status <> 'unverified' OR OLD.used_at IS NOT NULL OR OLD.verified_by_id IS NOT NULL)
            AND changed && ARRAY['jurisdiction', 'code', 'value', 'valid_from', 'valid_to', 'source_url',
                                 'source_reference', 'entered_by_id'] THEN
        RAISE EXCEPTION 'statutory parameter % is verified, superseded or used and is never edited (hr R7)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS hr_parameter_lock ON hr_statutoryparameter;
CREATE TRIGGER hr_parameter_lock BEFORE INSERT OR UPDATE OR DELETE ON hr_statutoryparameter
    FOR EACH ROW EXECUTE FUNCTION hr_parameter_lock();
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
    start = old.index("CREATE OR REPLACE FUNCTION hr_parameter_lock()")
    end = old.index("CREATE OR REPLACE FUNCTION hr_contract_lock()")
    _run(schema_editor, old[start:end] + """
DROP TRIGGER IF EXISTS hr_parameter_lock ON hr_statutoryparameter;
CREATE TRIGGER hr_parameter_lock BEFORE UPDATE OR DELETE ON hr_statutoryparameter
    FOR EACH ROW EXECUTE FUNCTION hr_parameter_lock();
""")


class Migration(migrations.Migration):

    dependencies = [
        ("hr", "0010_contract_renewals_and_extended_hours"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="statutoryparameter",
            name="hr_param_once",
        ),
        migrations.AddConstraint(
            model_name="statutoryparameter",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "superseded"), _negated=True),
                fields=("jurisdiction", "code", "valid_from"),
                name="hr_param_once",
            ),
        ),
        migrations.AddConstraint(
            model_name="statutoryparameter",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("verified_by__isnull", True),
                    ("entered_by__isnull", True),
                    models.Q(("verified_by", models.F("entered_by")), _negated=True),
                    _connector="OR",
                ),
                name="hr_param_verifier_not_enterer",
            ),
        ),
        migrations.AddConstraint(
            model_name="statutoryparameter",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "verified"), _negated=True),
                    models.Q(
                        ("verified_by__isnull", False), ("verified_at__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="hr_param_verified_has_verifier",
            ),
        ),
        migrations.AddConstraint(
            model_name="statutoryparameter",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "unverified"), _negated=True),
                    models.Q(
                        ("verified_by__isnull", True), ("verified_at__isnull", True)
                    ),
                    _connector="OR",
                ),
                name="hr_param_unverified_has_no_verifier",
            ),
        ),
        migrations.RunPython(forward, backward),
    ]
