"""Database guards for access §10 (PostgreSQL only; on SQLite the model and queryset guards apply, R44).

- R77: rows of a non-draft ``AuthorityMatrixVersion`` never change: INSERT, UPDATE and DELETE on ``core_authorityrule``
  and its ``approver_roles`` table are refused unless the row's version is ``draft``. A non-draft version keeps its
  entity, number, effective date, summaries, rows hash and proposer; only its status moves forward (submitted ->
  approved / rejected / withdrawn; approved -> superseded) with the decision columns. Versions are never deleted.
- R71: bands of one activity, currency and version are either the same band (several steps or parallel rows) or
  do not overlap.
- R85: a generated ``GovernanceReport`` never changes; the only update is the single review write (reviewer, time,
  notes). Reports are never deleted.
- ``ApprovalDecision`` and ``AccessAlert`` rows are append-only.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION core_matrix_version_status(version_id bigint) RETURNS text AS $$
    SELECT status FROM core_authoritymatrixversion WHERE id = version_id;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION core_authorityrule_guard() RETURNS trigger AS $$
DECLARE
    v_id bigint;
    clash integer;
BEGIN
    IF TG_OP = 'DELETE' THEN v_id := OLD.version_id; ELSE v_id := NEW.version_id; END IF;
    IF core_matrix_version_status(v_id) IS DISTINCT FROM 'draft'
       OR (TG_OP = 'UPDATE' AND core_matrix_version_status(OLD.version_id) IS DISTINCT FROM 'draft') THEN
        RAISE EXCEPTION 'rows of a non-draft authority matrix version never change (access.md R77)';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    SELECT count(*) INTO clash FROM core_authorityrule r
     WHERE r.version_id = NEW.version_id AND r.activity_id = NEW.activity_id AND r.id <> NEW.id
       AND r.band_currency_id IS NOT DISTINCT FROM NEW.band_currency_id
       AND NOT (r.amount_from = NEW.amount_from AND r.amount_to IS NOT DISTINCT FROM NEW.amount_to)
       AND r.amount_from <= COALESCE(NEW.amount_to, 1e30)
       AND NEW.amount_from <= COALESCE(r.amount_to, 1e30);
    IF clash > 0 THEN
        RAISE EXCEPTION 'authority matrix bands of one activity and currency may not overlap (access.md R71)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_authorityrule_frozen BEFORE INSERT OR UPDATE OR DELETE ON core_authorityrule
    FOR EACH ROW EXECUTE FUNCTION core_authorityrule_guard();

CREATE OR REPLACE FUNCTION core_authorityrule_roles_guard() RETURNS trigger AS $$
DECLARE
    r_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN r_id := OLD.authorityrule_id; ELSE r_id := NEW.authorityrule_id; END IF;
    IF core_matrix_version_status((SELECT version_id FROM core_authorityrule WHERE id = r_id))
       IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'rows of a non-draft authority matrix version never change (access.md R77)';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_authorityrule_roles_frozen BEFORE INSERT OR UPDATE OR DELETE ON core_authorityrule_approver_roles
    FOR EACH ROW EXECUTE FUNCTION core_authorityrule_roles_guard();

CREATE OR REPLACE FUNCTION core_matrix_version_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'authority matrix versions are never deleted (access.md R77)';
    END IF;
    IF OLD.status = 'draft' THEN
        RETURN NEW;
    END IF;
    IF (to_jsonb(NEW) - 'status' - 'decided_by_id' - 'decided_at' - 'decision_note' - 'superseded_by_id')
       IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'decided_by_id' - 'decided_at' - 'decision_note'
                         - 'superseded_by_id') THEN
        RAISE EXCEPTION 'a submitted authority matrix version never changes (access.md R77)';
    END IF;
    IF NOT ((OLD.status = 'submitted' AND NEW.status IN ('submitted', 'approved', 'rejected', 'withdrawn'))
            OR (OLD.status = 'approved' AND NEW.status IN ('approved', 'superseded'))
            OR (OLD.status = NEW.status AND NEW.decided_by_id IS NOT DISTINCT FROM OLD.decided_by_id
                AND NEW.superseded_by_id IS NOT DISTINCT FROM OLD.superseded_by_id)) THEN
        RAISE EXCEPTION 'authority matrix version status % cannot become % (access.md R77)', OLD.status, NEW.status;
    END IF;
    IF OLD.decided_by_id IS NOT NULL AND NEW.decided_by_id IS DISTINCT FROM OLD.decided_by_id THEN
        RAISE EXCEPTION 'an authority matrix decision is final (access.md R76)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_matrix_version_frozen BEFORE UPDATE OR DELETE ON core_authoritymatrixversion
    FOR EACH ROW EXECUTE FUNCTION core_matrix_version_guard();

CREATE OR REPLACE FUNCTION core_governance_report_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'governance reports are never deleted (access.md R85)';
    END IF;
    IF OLD.reviewed_at IS NOT NULL OR NEW.reviewed_at IS NULL
       OR (to_jsonb(NEW) - 'reviewed_by_id' - 'reviewed_at' - 'review_note_en' - 'review_note_ar')
          IS DISTINCT FROM (to_jsonb(OLD) - 'reviewed_by_id' - 'reviewed_at' - 'review_note_en' - 'review_note_ar') THEN
        RAISE EXCEPTION 'a generated governance report never changes; issue a new revision (access.md R85)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_governance_report_frozen BEFORE UPDATE OR DELETE ON core_governancereport
    FOR EACH ROW EXECUTE FUNCTION core_governance_report_guard();

CREATE TRIGGER core_approvaldecision_append_only BEFORE UPDATE OR DELETE ON core_approvaldecision
    FOR EACH ROW EXECUTE FUNCTION core_reject_change();
CREATE TRIGGER core_accessalert_append_only BEFORE UPDATE OR DELETE ON core_accessalert
    FOR EACH ROW EXECUTE FUNCTION core_reject_change();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS core_accessalert_append_only ON core_accessalert;
DROP TRIGGER IF EXISTS core_approvaldecision_append_only ON core_approvaldecision;
DROP TRIGGER IF EXISTS core_governance_report_frozen ON core_governancereport;
DROP TRIGGER IF EXISTS core_matrix_version_frozen ON core_authoritymatrixversion;
DROP TRIGGER IF EXISTS core_authorityrule_roles_frozen ON core_authorityrule_approver_roles;
DROP TRIGGER IF EXISTS core_authorityrule_frozen ON core_authorityrule;
DROP FUNCTION IF EXISTS core_governance_report_guard();
DROP FUNCTION IF EXISTS core_matrix_version_guard();
DROP FUNCTION IF EXISTS core_authorityrule_roles_guard();
DROP FUNCTION IF EXISTS core_authorityrule_guard();
DROP FUNCTION IF EXISTS core_matrix_version_status(bigint);
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    _run(schema_editor, REVERSE)


class Migration(migrations.Migration):
    dependencies = [("core", "0028_seed_access_v17_authority_matrix")]

    operations = [migrations.RunPython(forwards, backwards)]
