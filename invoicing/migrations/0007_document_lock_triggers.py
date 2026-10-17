"""Invoicing rules R13, R24, R33, R36, R45 in the database (PostgreSQL only; review item 8).

Mirrors the ledger triggers (ledger 0009, 0012): a minimal check against writes that bypass the ORM,
allowing exactly the changes ``invoicing.services`` makes. ``updated_at`` may always change.

- invoicing_salesdocument
  - INSERT: a draft, no number, no entry, ZATCA ``not_submitted``. DELETE: drafts only.
  - ``uuid``, ``entity``, ``doc_type`` and ``created_by`` never change; a number never changes once set.
  - draft -> draft / submitted: no number, entry, approval or ZATCA field is written.
  - submitted -> draft: only status and modified_at / modified_by (return to draft).
  - submitted -> issued: number, approver, approval time and issue time are set (the approval writes the
    snapshot and totals); no entry, ZATCA fields untouched. A submitted row is not otherwise changed.
  - issued -> issued / final / rejected: only status, the ``zatca_*`` fields and (to final) the journal
    entry, which must be a posted entry of this document; rejected keeps no entry.
  - final and rejected rows never change.
- invoicing_purchasedocument: draft -> submitted -> posted (number, approver) -> posted (its entry, set
  once, and the purification flag) -> reversed (its reversal entry, set once). Reversed rows never change.
- invoicing_customerreceipt: draft -> posted (number, poster) -> entry set once -> reversed.
- invoicing_vendorpayment: draft -> submitted -> posted (number, poster, approver) -> entry -> reversed.
- document lines: added, changed or deleted only while the document is a draft; while it is submitted the
  approval may re-price them (VAT rate, amounts, recoverable flag) and nothing else. The document row is read
  FOR SHARE, so a line write waits for a status change in flight.
- receipt and payment applications: written only while their receipt or payment is a draft.
- allocations: append-only (R36), never updated or deleted.

Raw SQL that performs one of the allowed transitions with plausible values is accepted: the database
role's privileges (access.md OQ-10) are the guard against such writes. On SQLite this migration does
nothing; the model and queryset guards apply.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION invoicing_changed_keys(old_row jsonb, new_row jsonb) RETURNS text[] AS $$
    SELECT coalesce(array_agg(n.key ORDER BY n.key), ARRAY[]::text[])
      FROM jsonb_each(new_row) n
     WHERE n.key <> 'updated_at' AND n.value IS DISTINCT FROM (old_row -> n.key);
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION invoicing_salesdocument_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
    zatca text[] := ARRAY['zatca_status', 'zatca_icv', 'zatca_previous_hash', 'zatca_hash', 'zatca_qr',
                          'zatca_messages'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'sales document % is %; only drafts are deleted (R13)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.number <> '' OR NEW.journal_entry_id IS NOT NULL
           OR NEW.zatca_status <> 'not_submitted' THEN
            RAISE EXCEPTION 'sales documents are created as drafts through invoicing.services';
        END IF;
        RETURN NEW;
    END IF;
    changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'uuid', 'entity_id', 'doc_type', 'created_by_id', 'created_at']
       OR (OLD.number <> '' AND NEW.number <> OLD.number) THEN
        RAISE EXCEPTION 'sales document %: identity, type, creator and number never change', OLD.id;
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status NOT IN ('draft', 'submitted')
           OR changed && (zatca || ARRAY['number', 'journal_entry_id', 'approved_by_id', 'approved_at']) THEN
            RAISE EXCEPTION 'draft sales document %: only edits and submission (R57)', OLD.id;
        END IF;
    ELSIF OLD.status = 'submitted' THEN
        IF NEW.status = 'draft' THEN
            IF NOT changed <@ ARRAY['status', 'modified_at', 'modified_by_id'] THEN
                RAISE EXCEPTION 'sales document %: a return to draft changes nothing else', OLD.id;
            END IF;
        ELSIF NEW.status = 'issued' THEN
            IF NEW.number = '' OR NEW.approved_by_id IS NULL OR NEW.approved_at IS NULL OR NEW.issue_time IS NULL
               OR changed && (zatca || ARRAY['journal_entry_id']) THEN
                RAISE EXCEPTION 'sales document % is issued only by approval (number, approver, time)', OLD.id;
            END IF;
        ELSIF array_length(changed, 1) IS NOT NULL THEN
            RAISE EXCEPTION 'submitted sales document % changes only by approval or return to draft', OLD.id;
        END IF;
    ELSIF OLD.status = 'issued' THEN
        IF NEW.status NOT IN ('issued', 'final', 'rejected')
           OR NOT changed <@ (zatca || ARRAY['status', 'journal_entry_id']) THEN
            RAISE EXCEPTION 'issued sales document % changes only through the ZATCA result and its posting (R45)', OLD.id;
        END IF;
        IF 'journal_entry_id' = ANY(changed) AND (OLD.journal_entry_id IS NOT NULL OR NEW.status <> 'final') THEN
            RAISE EXCEPTION 'sales document %: its entry is set once, when it becomes final', OLD.id;
        END IF;
        IF NEW.status = 'final' AND NOT EXISTS (
            SELECT 1 FROM ledger_journalentry e
             WHERE e.id = NEW.journal_entry_id AND e.status = 'posted' AND e.entity_id = NEW.entity_id
               AND e.document_type = 'invoicing.salesdocument' AND e.document_id = NEW.id
        ) THEN
            RAISE EXCEPTION 'sales document % is final only with its own posted entry (R41)', OLD.id;
        END IF;
        IF NEW.status = 'final' AND NEW.zatca_status NOT IN ('cleared', 'reported', 'cleared_with_warnings',
                                                              'reported_with_warnings') THEN
            RAISE EXCEPTION 'sales document % is final only once ZATCA cleared or reported it (R44)', OLD.id;
        END IF;
        IF NEW.status = 'rejected' AND (NEW.zatca_status <> 'rejected' OR NEW.journal_entry_id IS NOT NULL) THEN
            RAISE EXCEPTION 'sales document % is rejected only by a ZATCA rejection, with no entry (R42)', OLD.id;
        END IF;
    ELSIF array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'sales document % is %; it never changes (R13)', OLD.id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION invoicing_booked_document_lock() RETURNS trigger AS $$
-- Purchase documents, receipts and vendor payments. TG_ARGV[0] = 'purchase' | 'receipt' | 'payment'.
DECLARE
    changed text[];
    kind text := TG_ARGV[0];
    booked text[] := ARRAY['number', 'journal_entry_id', 'reversal_entry_id'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '% % is %; only drafts are deleted (R24)', kind, OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.number <> '' OR NEW.journal_entry_id IS NOT NULL
           OR NEW.reversal_entry_id IS NOT NULL THEN
            RAISE EXCEPTION '% records are created as drafts through invoicing.services', kind;
        END IF;
        RETURN NEW;
    END IF;
    changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'created_by_id', 'created_at'] OR (OLD.number <> '' AND NEW.number <> OLD.number) THEN
        RAISE EXCEPTION '% %: identity, creator and number never change', kind, OLD.id;
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'draft' OR (NEW.status = 'submitted' AND kind IN ('purchase', 'payment')) THEN
            IF changed && booked THEN
                RAISE EXCEPTION 'draft % %: no number or entry before posting', kind, OLD.id;
            END IF;
        ELSIF NEW.status = 'posted' AND kind = 'receipt' THEN
            IF NEW.number = '' OR (to_jsonb(NEW) ->> 'posted_by_id') IS NULL
               OR changed && ARRAY['journal_entry_id', 'reversal_entry_id'] THEN
                RAISE EXCEPTION 'receipt % is posted only through invoicing.services (number, poster)', OLD.id;
            END IF;
        ELSE
            RAISE EXCEPTION '% %: draft -> % is not a transition (R58, R59)', kind, OLD.id, NEW.status;
        END IF;
    ELSIF OLD.status = 'submitted' THEN
        IF NEW.status = 'posted' THEN
            IF NEW.number = '' OR (to_jsonb(NEW) ->> 'approved_by_id') IS NULL
               OR changed && ARRAY['journal_entry_id', 'reversal_entry_id'] THEN
                RAISE EXCEPTION '% % is posted only by approval (number, approver)', kind, OLD.id;
            END IF;
        ELSIF array_length(changed, 1) IS NOT NULL THEN
            RAISE EXCEPTION 'submitted % % changes only by approval (R58, R59)', kind, OLD.id;
        END IF;
    ELSIF OLD.status = 'posted' THEN
        IF NEW.status = 'posted' THEN
            IF NOT changed <@ ARRAY['journal_entry_id', 'requires_purification']
               OR ('journal_entry_id' = ANY(changed) AND OLD.journal_entry_id IS NOT NULL) THEN
                RAISE EXCEPTION 'posted % % is never edited; its entry is recorded once', kind, OLD.id;
            END IF;
        ELSIF NEW.status = 'reversed' THEN
            IF NOT changed <@ ARRAY['status', 'reversal_entry_id'] OR OLD.reversal_entry_id IS NOT NULL
               OR NEW.reversal_entry_id IS NULL OR OLD.journal_entry_id IS NULL THEN
                RAISE EXCEPTION '% % is reversed only with its reversal entry (R24, R32)', kind, OLD.id;
            END IF;
        ELSE
            RAISE EXCEPTION 'posted % % is only reversed (R24)', kind, OLD.id;
        END IF;
    ELSIF array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION '% % is %; it never changes', kind, OLD.id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION invoicing_line_lock() RETURNS trigger AS $$
-- Document lines. TG_ARGV[0] = the document table.
DECLARE
    doc_status text;
    changed text[];
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        EXECUTE format('SELECT status FROM %I WHERE id = $1 FOR SHARE', TG_ARGV[0]) INTO doc_status USING OLD.document_id;
        IF TG_OP = 'UPDATE' AND doc_status = 'submitted' AND NEW.document_id = OLD.document_id THEN
            changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
            IF NOT changed <@ ARRAY['vat_rate_id', 'vat_amount', 'net_amount', 'vat_recoverable'] THEN
                RAISE EXCEPTION 'line % of a submitted document is only re-priced at approval', OLD.id;
            END IF;
            RETURN NEW;
        END IF;
        IF doc_status IS NOT NULL AND doc_status <> 'draft' THEN
            RAISE EXCEPTION 'line % belongs to a % document; it never changes (R13, R24)', OLD.id, doc_status;
        END IF;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        EXECUTE format('SELECT status FROM %I WHERE id = $1 FOR SHARE', TG_ARGV[0]) INTO doc_status USING NEW.document_id;
        IF doc_status IS NOT NULL AND doc_status <> 'draft' THEN
            RAISE EXCEPTION 'lines are not added to or moved onto a % document', doc_status;
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION invoicing_application_lock() RETURNS trigger AS $$
-- Receipt and payment applications. TG_ARGV[0] = the source table, TG_ARGV[1] = its foreign key column.
DECLARE
    source_status text;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        EXECUTE format('SELECT status FROM %I WHERE id = $1 FOR SHARE', TG_ARGV[0]) INTO source_status
            USING (to_jsonb(OLD) ->> TG_ARGV[1])::bigint;
        IF source_status IS NOT NULL AND source_status <> 'draft' THEN
            RAISE EXCEPTION 'application % belongs to a % document; it never changes', OLD.id, source_status;
        END IF;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        EXECUTE format('SELECT status FROM %I WHERE id = $1 FOR SHARE', TG_ARGV[0]) INTO source_status
            USING (to_jsonb(NEW) ->> TG_ARGV[1])::bigint;
        IF source_status IS NOT NULL AND source_status <> 'draft' THEN
            RAISE EXCEPTION 'applications are added only to drafts, not to a % document', source_status;
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION invoicing_allocation_lock() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'allocation % is never changed or deleted; un-allocate with a new row (R36)', OLD.id;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER invoicing_salesdocument_lock BEFORE INSERT OR UPDATE OR DELETE ON invoicing_salesdocument
    FOR EACH ROW EXECUTE FUNCTION invoicing_salesdocument_lock();
CREATE TRIGGER invoicing_purchasedocument_lock BEFORE INSERT OR UPDATE OR DELETE ON invoicing_purchasedocument
    FOR EACH ROW EXECUTE FUNCTION invoicing_booked_document_lock('purchase');
CREATE TRIGGER invoicing_customerreceipt_lock BEFORE INSERT OR UPDATE OR DELETE ON invoicing_customerreceipt
    FOR EACH ROW EXECUTE FUNCTION invoicing_booked_document_lock('receipt');
CREATE TRIGGER invoicing_vendorpayment_lock BEFORE INSERT OR UPDATE OR DELETE ON invoicing_vendorpayment
    FOR EACH ROW EXECUTE FUNCTION invoicing_booked_document_lock('payment');
CREATE TRIGGER invoicing_salesdocumentline_lock BEFORE INSERT OR UPDATE OR DELETE ON invoicing_salesdocumentline
    FOR EACH ROW EXECUTE FUNCTION invoicing_line_lock('invoicing_salesdocument');
CREATE TRIGGER invoicing_purchasedocumentline_lock BEFORE INSERT OR UPDATE OR DELETE ON invoicing_purchasedocumentline
    FOR EACH ROW EXECUTE FUNCTION invoicing_line_lock('invoicing_purchasedocument');
CREATE TRIGGER invoicing_receiptapplication_lock BEFORE INSERT OR UPDATE OR DELETE ON invoicing_receiptapplication
    FOR EACH ROW EXECUTE FUNCTION invoicing_application_lock('invoicing_customerreceipt', 'receipt_id');
CREATE TRIGGER invoicing_paymentapplication_lock BEFORE INSERT OR UPDATE OR DELETE ON invoicing_paymentapplication
    FOR EACH ROW EXECUTE FUNCTION invoicing_application_lock('invoicing_vendorpayment', 'payment_id');
CREATE TRIGGER invoicing_receivableallocation_lock BEFORE UPDATE OR DELETE ON invoicing_receivableallocation
    FOR EACH ROW EXECUTE FUNCTION invoicing_allocation_lock();
CREATE TRIGGER invoicing_payableallocation_lock BEFORE UPDATE OR DELETE ON invoicing_payableallocation
    FOR EACH ROW EXECUTE FUNCTION invoicing_allocation_lock();
"""

TABLES = (
    "salesdocument", "purchasedocument", "customerreceipt", "vendorpayment", "salesdocumentline",
    "purchasedocumentline", "receiptapplication", "paymentapplication", "receivableallocation", "payableallocation",
)
REVERSE = "\n".join(f"DROP TRIGGER IF EXISTS invoicing_{t}_lock ON invoicing_{t};" for t in TABLES) + r"""
DROP FUNCTION IF EXISTS invoicing_salesdocument_lock();
DROP FUNCTION IF EXISTS invoicing_booked_document_lock();
DROP FUNCTION IF EXISTS invoicing_line_lock();
DROP FUNCTION IF EXISTS invoicing_application_lock();
DROP FUNCTION IF EXISTS invoicing_allocation_lock();
DROP FUNCTION IF EXISTS invoicing_changed_keys(jsonb, jsonb);
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
    dependencies = [("invoicing", "0006_oos_exemption_text"), ("ledger", "0013_invoicing_income_accounts")]

    operations = [migrations.RunPython(forwards, backwards)]
