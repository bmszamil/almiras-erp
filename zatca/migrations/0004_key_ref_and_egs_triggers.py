"""zatca finding 16 (review of 838e89b): tighter PostgreSQL triggers (as 0002; SQLite does nothing).

  * key references (``zatca_groupsuperkey``, ``zatca_entitymasterkey``): entity and ``protected_by`` fixed;
    ``key_ref`` changes only together with a higher ``key_version`` and a version changes only with a new
    reference;
  * EGS units: status changes only along R-5 (any -> revoked; revoked is final) and ``private_key_ref`` changes
    only when a CSR is generated (draft -> csr_ready) or on renewal (active/renewal_due -> active with a new CSR),
    never to empty.
"""
from django.db import migrations

KEY_REF_LOCK = r"""
CREATE OR REPLACE FUNCTION zatca_key_ref_lock() RETURNS trigger AS $$
DECLARE
    ref_changed boolean := NEW.key_ref IS DISTINCT FROM OLD.key_ref;
    version_changed boolean := NEW.key_version IS DISTINCT FROM OLD.key_version;
BEGIN
    IF NEW.entity_id <> OLD.entity_id OR NEW.key_version < OLD.key_version OR ref_changed <> version_changed
       OR (to_jsonb(NEW) ->> 'protected_by_id') IS DISTINCT FROM (to_jsonb(OLD) ->> 'protected_by_id') THEN
        RAISE EXCEPTION 'key reference %: entity fixed, a new reference only with a higher version (R-50)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

OLD_KEY_REF_LOCK = r"""
CREATE OR REPLACE FUNCTION zatca_key_ref_lock() RETURNS trigger AS $$
BEGIN
    IF NEW.entity_id <> OLD.entity_id OR NEW.key_version < OLD.key_version THEN
        RAISE EXCEPTION 'key reference %: entity fixed, versions only increase (R-50)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

EGS_STATUS_LOCK = r"""
CREATE OR REPLACE FUNCTION zatca_egsunit_status_lock() RETURNS trigger AS $$
DECLARE
    changed text[] := zatca_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    allowed boolean;
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        allowed := (NEW.status = 'revoked' AND OLD.status <> 'revoked') OR CASE OLD.status
            WHEN 'draft' THEN NEW.status = 'csr_ready'
            WHEN 'csr_ready' THEN NEW.status = 'compliance_csid'
            WHEN 'compliance_csid' THEN NEW.status IN ('compliance_passed', 'draft')
            WHEN 'compliance_passed' THEN NEW.status = 'active'
            WHEN 'active' THEN NEW.status IN ('renewal_due', 'expired')
            WHEN 'renewal_due' THEN NEW.status IN ('active', 'expired')
            ELSE false
        END;
        IF NOT allowed THEN
            RAISE EXCEPTION 'EGS unit %: % -> % is not allowed (R-5)', OLD.id, OLD.status, NEW.status;
        END IF;
    END IF;
    IF 'private_key_ref' = ANY(changed) AND (NEW.private_key_ref = '' OR NOT (
            (OLD.status = 'draft' AND NEW.status = 'csr_ready')
            OR (OLD.status IN ('active', 'renewal_due') AND NEW.status = 'active' AND 'csr_pem' = ANY(changed)))) THEN
        RAISE EXCEPTION 'EGS unit %: the private key reference changes only with a new CSR (R-6, R-10)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER zatca_egsunit_status_lock BEFORE UPDATE ON zatca_egsunit
    FOR EACH ROW EXECUTE FUNCTION zatca_egsunit_status_lock();
"""

DROP_EGS_STATUS_LOCK = r"""
DROP TRIGGER IF EXISTS zatca_egsunit_status_lock ON zatca_egsunit;
DROP FUNCTION IF EXISTS zatca_egsunit_status_lock();
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forward(apps, schema_editor):
    _run(schema_editor, KEY_REF_LOCK + EGS_STATUS_LOCK)


def reverse(apps, schema_editor):
    _run(schema_editor, DROP_EGS_STATUS_LOCK + OLD_KEY_REF_LOCK)


class Migration(migrations.Migration):
    dependencies = [("zatca", "0003_einvoice_submitting_since")]

    operations = [migrations.RunPython(forward, reverse)]
