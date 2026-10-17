"""zatca R-11, R-12, R-17, R-18, R-20, R-38 in the database (PostgreSQL only), mirroring the model guards.

- Append-only: submission attempts, validation messages, onboarding events, alerts (no UPDATE / DELETE).
- e-invoices: created ``pending`` with no ICV or XML; identity fields never change; ICV, PIH, hash, signed XML,
  signing time, QR and cleared XML are written once; status changes only along R-20; the ICV is set only while
  stamping and only as the next ICV of its EGS unit with the unit's current hash as PIH (R-11, R-12);
  retention never shortens; only ``pending`` rows without an ICV are deleted.
- EGS units: never deleted; created at ICV 0 with the literal initial PIH; ``last_icv`` moves only by +1 and
  only to an e-invoice of that unit with that ICV whose PIH was the previous hash (the PIH chain).
- credentials: identity and certificate never change; status leaves ``active`` once; ``replaced_by`` once.
- delegations: only the single revocation write; compliance tasks: open -> notified -> done, done final;
  key references: entity fixed, versions only increase; nothing of these is deleted.

On SQLite this migration does nothing; the model and queryset guards apply.
"""
from django.db import migrations

INITIAL_PIH = "NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOQ=="

FORWARD = r"""
CREATE OR REPLACE FUNCTION zatca_changed_keys(old_row jsonb, new_row jsonb) RETURNS text[] AS $$
    SELECT coalesce(array_agg(n.key ORDER BY n.key), ARRAY[]::text[])
      FROM jsonb_each(new_row) n
     WHERE n.value IS DISTINCT FROM (old_row -> n.key);
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION zatca_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'zatca %: rows are append-only evidence (R-18)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION zatca_no_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'zatca %: rows are never deleted', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION zatca_einvoice_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
    egs record;
    once text[] := ARRAY['icv', 'pih', 'invoice_hash', 'signed_xml', 'signing_time', 'qr_tlv', 'cleared_xml'];
    k text;
    allowed boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'pending' OR OLD.icv IS NOT NULL THEN
            RAISE EXCEPTION 'e-invoice % is %; only pending rows without an ICV are deleted (R-18)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.icv IS NOT NULL OR NEW.signed_xml <> '' OR NEW.cleared_xml <> '' THEN
            RAISE EXCEPTION 'e-invoices are created pending, unsigned, through zatca.services';
        END IF;
        RETURN NEW;
    END IF;
    changed := zatca_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'source_document_id', 'uuid', 'zatca_category', 'type_code',
                        'created_at', 'created_by_id'] THEN
        RAISE EXCEPTION 'e-invoice %: identity fields never change', OLD.id;
    END IF;
    FOREACH k IN ARRAY once LOOP
        IF k = ANY(changed) AND coalesce(to_jsonb(OLD) ->> k, '') <> '' THEN
            RAISE EXCEPTION 'e-invoice %: % is written once (R-17)', OLD.id, k;
        END IF;
    END LOOP;
    IF 'egs_unit_id' = ANY(changed) AND OLD.icv IS NOT NULL THEN
        RAISE EXCEPTION 'e-invoice %: the signing EGS unit never changes after stamping', OLD.id;
    END IF;
    IF OLD.retention_until IS NOT NULL AND (NEW.retention_until IS NULL OR NEW.retention_until < OLD.retention_until) THEN
        RAISE EXCEPTION 'e-invoice %: retention is never shortened (R-38)', OLD.id;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        allowed := CASE OLD.status
            WHEN 'pending' THEN NEW.status = 'stamped'
            WHEN 'stamped' THEN NEW.status = 'queued'
            WHEN 'queued' THEN NEW.status = 'submitting'
            WHEN 'submitting' THEN NEW.status IN ('cleared', 'cleared_with_warnings', 'reported',
                'reported_with_warnings', 'reported_clearance_disabled', 'rejected', 'failed_retryable', 'failed_final')
            WHEN 'failed_retryable' THEN NEW.status = 'queued'
            ELSE false
        END;
        IF NOT allowed THEN
            RAISE EXCEPTION 'e-invoice %: % -> % is not allowed (R-20)', OLD.id, OLD.status, NEW.status;
        END IF;
    END IF;
    IF OLD.status IN ('cleared', 'cleared_with_warnings', 'reported', 'reported_with_warnings',
                      'reported_clearance_disabled', 'rejected', 'failed_final')
       AND NOT changed <@ ARRAY['invoicing_synced', 'deadline_alerted_at'] THEN
        RAISE EXCEPTION 'e-invoice % is %; it never changes (R-17)', OLD.id, OLD.status;
    END IF;
    IF 'cleared_xml' = ANY(changed) AND NEW.status NOT IN ('cleared', 'cleared_with_warnings') THEN
        RAISE EXCEPTION 'e-invoice %: cleared XML comes only with clearance', OLD.id;
    END IF;
    IF OLD.icv IS NULL AND NEW.icv IS NOT NULL THEN
        SELECT last_icv, last_invoice_hash, entity_id INTO egs FROM zatca_egsunit WHERE id = NEW.egs_unit_id;
        IF NEW.status <> 'stamped' OR NEW.signed_xml = '' OR NEW.invoice_hash = '' OR egs.entity_id <> NEW.entity_id
           OR NEW.icv <> egs.last_icv + 1 OR NEW.pih <> egs.last_invoice_hash THEN
            RAISE EXCEPTION 'e-invoice %: the ICV is the next one of its own EGS unit and the PIH its last hash (R-11, R-12)',
                OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION zatca_egsunit_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'EGS unit %: units are never deleted', OLD.id;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.last_icv <> 0 OR NEW.last_invoice_hash <> '__INITIAL_PIH__' THEN
            RAISE EXCEPTION 'EGS units start at ICV 0 with the initial PIH (R-12)';
        END IF;
        RETURN NEW;
    END IF;
    changed := zatca_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'code', 'created_at', 'created_by_id'] THEN
        RAISE EXCEPTION 'EGS unit %: identity never changes', OLD.id;
    END IF;
    IF changed && ARRAY['last_icv', 'last_invoice_hash'] THEN
        IF NEW.last_icv <> OLD.last_icv + 1 OR NOT EXISTS (
            SELECT 1 FROM zatca_einvoice e
             WHERE e.egs_unit_id = NEW.id AND e.icv = NEW.last_icv AND e.invoice_hash = NEW.last_invoice_hash
               AND e.pih = OLD.last_invoice_hash
        ) THEN
            RAISE EXCEPTION 'EGS unit %: the chain moves by one stamped e-invoice at a time (R-11, R-12)', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION zatca_credential_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    changed := zatca_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'egs_unit_id', 'kind', 'environment', 'certificate_pem', 'secret_ref',
                        'zatca_request_id', 'valid_from', 'valid_to', 'created_at', 'created_by_id'] THEN
        RAISE EXCEPTION 'credential %: certificate and identity never change', OLD.id;
    END IF;
    IF 'status' = ANY(changed) AND OLD.status <> 'active' THEN
        RAISE EXCEPTION 'credential %: a % credential stays %', OLD.id, OLD.status, OLD.status;
    END IF;
    IF 'replaced_by_id' = ANY(changed) AND OLD.replaced_by_id IS NOT NULL THEN
        RAISE EXCEPTION 'credential %: replaced_by is set once', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION zatca_delegation_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    changed := zatca_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF OLD.revoked_at IS NOT NULL OR NOT changed <@ ARRAY['revoked_at', 'revoked_by_id', 'revoke_reason'] THEN
        RAISE EXCEPTION 'delegation %: only a single revocation write is allowed (R-41)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION zatca_task_lock() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'done' THEN
        RAISE EXCEPTION 'compliance task %: done is final (R-35)', OLD.id;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'open' AND NEW.status = 'notified' AND NEW.notified_at IS NOT NULL AND NEW.notice_reference <> '')
        OR (OLD.status = 'notified' AND NEW.status = 'done' AND NEW.recovered_notified_at IS NOT NULL
            AND NEW.recovery_notice_reference <> '' AND NEW.done_by_id IS NOT NULL)
    ) THEN
        RAISE EXCEPTION 'compliance task %: open -> notified -> done, each with its notice (R-35)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION zatca_key_ref_lock() RETURNS trigger AS $$
BEGIN
    IF NEW.entity_id <> OLD.entity_id OR NEW.key_version < OLD.key_version THEN
        RAISE EXCEPTION 'key reference %: entity fixed, versions only increase (R-50)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER zatca_submissionattempt_append_only BEFORE UPDATE OR DELETE ON zatca_submissionattempt
    FOR EACH ROW EXECUTE FUNCTION zatca_append_only();
CREATE TRIGGER zatca_validationmessage_append_only BEFORE UPDATE OR DELETE ON zatca_validationmessage
    FOR EACH ROW EXECUTE FUNCTION zatca_append_only();
CREATE TRIGGER zatca_onboardingevent_append_only BEFORE UPDATE OR DELETE ON zatca_onboardingevent
    FOR EACH ROW EXECUTE FUNCTION zatca_append_only();
CREATE TRIGGER zatca_zatcaalert_append_only BEFORE UPDATE OR DELETE ON zatca_zatcaalert
    FOR EACH ROW EXECUTE FUNCTION zatca_append_only();
CREATE TRIGGER zatca_einvoice_lock BEFORE INSERT OR UPDATE OR DELETE ON zatca_einvoice
    FOR EACH ROW EXECUTE FUNCTION zatca_einvoice_lock();
CREATE TRIGGER zatca_egsunit_lock BEFORE INSERT OR UPDATE OR DELETE ON zatca_egsunit
    FOR EACH ROW EXECUTE FUNCTION zatca_egsunit_lock();
CREATE TRIGGER zatca_credential_lock BEFORE UPDATE ON zatca_egscredential
    FOR EACH ROW EXECUTE FUNCTION zatca_credential_lock();
CREATE TRIGGER zatca_delegation_lock BEFORE UPDATE ON zatca_keycustodiandelegation
    FOR EACH ROW EXECUTE FUNCTION zatca_delegation_lock();
CREATE TRIGGER zatca_task_lock BEFORE UPDATE ON zatca_compliancetask
    FOR EACH ROW EXECUTE FUNCTION zatca_task_lock();
CREATE TRIGGER zatca_groupsuperkey_lock BEFORE UPDATE ON zatca_groupsuperkey
    FOR EACH ROW EXECUTE FUNCTION zatca_key_ref_lock();
CREATE TRIGGER zatca_entitymasterkey_lock BEFORE UPDATE ON zatca_entitymasterkey
    FOR EACH ROW EXECUTE FUNCTION zatca_key_ref_lock();
""".replace("__INITIAL_PIH__", INITIAL_PIH)

NO_DELETE_TABLES = ("zatca_egscredential", "zatca_keycustodiandelegation", "zatca_compliancetask",
                    "zatca_groupsuperkey", "zatca_entitymasterkey", "zatca_zatcaprofile")
FORWARD += "".join(
    f"CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION zatca_no_delete();\n"
    for t in NO_DELETE_TABLES)

TRIGGERS = (
    ("zatca_submissionattempt", "zatca_submissionattempt_append_only"),
    ("zatca_validationmessage", "zatca_validationmessage_append_only"),
    ("zatca_onboardingevent", "zatca_onboardingevent_append_only"),
    ("zatca_zatcaalert", "zatca_zatcaalert_append_only"),
    ("zatca_einvoice", "zatca_einvoice_lock"),
    ("zatca_egsunit", "zatca_egsunit_lock"),
    ("zatca_egscredential", "zatca_credential_lock"),
    ("zatca_keycustodiandelegation", "zatca_delegation_lock"),
    ("zatca_compliancetask", "zatca_task_lock"),
    ("zatca_groupsuperkey", "zatca_groupsuperkey_lock"),
    ("zatca_entitymasterkey", "zatca_entitymasterkey_lock"),
    *((t, f"{t}_no_delete") for t in NO_DELETE_TABLES),
)
REVERSE = "".join(f"DROP TRIGGER IF EXISTS {name} ON {table};\n" for table, name in TRIGGERS) + """
DROP FUNCTION IF EXISTS zatca_key_ref_lock(); DROP FUNCTION IF EXISTS zatca_task_lock();
DROP FUNCTION IF EXISTS zatca_delegation_lock(); DROP FUNCTION IF EXISTS zatca_credential_lock();
DROP FUNCTION IF EXISTS zatca_egsunit_lock(); DROP FUNCTION IF EXISTS zatca_einvoice_lock();
DROP FUNCTION IF EXISTS zatca_no_delete(); DROP FUNCTION IF EXISTS zatca_append_only();
DROP FUNCTION IF EXISTS zatca_changed_keys(jsonb, jsonb);
"""


def forward(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD)


def reverse(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE)


class Migration(migrations.Migration):
    dependencies = [("zatca", "0001_initial")]

    operations = [migrations.RunPython(forward, reverse)]
