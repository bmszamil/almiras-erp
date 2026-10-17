"""A version counter on sales and purchase documents, receipts and vendor payments (verification review item 2).

Every edit, line change, submission and return to draft bumps ``version`` (invoicing.services._bump). An approval
or posting records the version its grant was checked against and is refused when the locked row has another
one, instead of comparing ``modified_at`` / ``submitted_at`` with the grant time, which came from two app-server
clocks. The triggers (PostgreSQL only) let a return to draft write the version and refuse a version that goes
down; otherwise they are those of migrations 0007 and 0008.
"""

from django.db import migrations, models

FORWARD = r"""
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
    IF NEW.version < OLD.version THEN
        RAISE EXCEPTION '% %: the version only goes up', 'sales document', OLD.id;
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status NOT IN ('draft', 'submitted')
           OR changed && (zatca || ARRAY['number', 'journal_entry_id', 'approved_by_id', 'approved_at']) THEN
            RAISE EXCEPTION 'draft sales document %: only edits and submission (R57)', OLD.id;
        END IF;
    ELSIF OLD.status = 'submitted' THEN
        IF NEW.status = 'draft' THEN
            IF NOT changed <@ ARRAY['status', 'modified_at', 'modified_by_id', 'version'] THEN
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
    IF NEW.version < OLD.version THEN
        RAISE EXCEPTION '% %: the version only goes up', kind, OLD.id;
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
            -- The purification flag is the entry's: written only with the entry, and equal to its lines.
            IF 'requires_purification' = ANY(changed) AND NOT 'journal_entry_id' = ANY(changed) THEN
                RAISE EXCEPTION '% %: the purification flag changes only when its entry is recorded (rule 5)',
                    kind, OLD.id;
            END IF;
            IF 'journal_entry_id' = ANY(changed) AND kind = 'purchase'
               AND (to_jsonb(NEW) ->> 'requires_purification')::boolean IS DISTINCT FROM EXISTS (
                   SELECT 1 FROM ledger_journalline l
                    WHERE l.entry_id = NEW.journal_entry_id AND l.requires_purification) THEN
                RAISE EXCEPTION '% %: the purification flag is the one its entry''s lines carry (rule 5)', kind, OLD.id;
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
"""

PREVIOUS = r"""
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
            -- The purification flag is the entry's: written only with the entry, and equal to its lines.
            IF 'requires_purification' = ANY(changed) AND NOT 'journal_entry_id' = ANY(changed) THEN
                RAISE EXCEPTION '% %: the purification flag changes only when its entry is recorded (rule 5)',
                    kind, OLD.id;
            END IF;
            IF 'journal_entry_id' = ANY(changed) AND kind = 'purchase'
               AND (to_jsonb(NEW) ->> 'requires_purification')::boolean IS DISTINCT FROM EXISTS (
                   SELECT 1 FROM ledger_journalline l
                    WHERE l.entry_id = NEW.journal_entry_id AND l.requires_purification) THEN
                RAISE EXCEPTION '% %: the purification flag is the one its entry''s lines carry (rule 5)', kind, OLD.id;
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
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    _run(schema_editor, PREVIOUS)


class Migration(migrations.Migration):

    dependencies = [
        ("invoicing", "0008_purification_flag_trigger"),
    ]

    operations = [
        migrations.AddField(
            model_name="customerreceipt",
            name="version",
            field=models.PositiveIntegerField(
                default=0,
                editable=False,
                help_text="Bumped by every edit, line change, submission and return to draft: an approval or posting checked against an older version is refused (access R14, R42) / يزداد مع كل تعديل أو تغيير بند أو تقديم أو إعادة إلى مسودة: يُرفض الاعتماد المبني على نسخة أقدم",
                verbose_name="Version / رقم النسخة",
            ),
        ),
        migrations.AddField(
            model_name="purchasedocument",
            name="version",
            field=models.PositiveIntegerField(
                default=0,
                editable=False,
                help_text="Bumped by every edit, line change, submission and return to draft: an approval or posting checked against an older version is refused (access R14, R42) / يزداد مع كل تعديل أو تغيير بند أو تقديم أو إعادة إلى مسودة: يُرفض الاعتماد المبني على نسخة أقدم",
                verbose_name="Version / رقم النسخة",
            ),
        ),
        migrations.AddField(
            model_name="salesdocument",
            name="version",
            field=models.PositiveIntegerField(
                default=0,
                editable=False,
                help_text="Bumped by every edit, line change, submission and return to draft: an approval or posting checked against an older version is refused (access R14, R42) / يزداد مع كل تعديل أو تغيير بند أو تقديم أو إعادة إلى مسودة: يُرفض الاعتماد المبني على نسخة أقدم",
                verbose_name="Version / رقم النسخة",
            ),
        ),
        migrations.AddField(
            model_name="vendorpayment",
            name="version",
            field=models.PositiveIntegerField(
                default=0,
                editable=False,
                help_text="Bumped by every edit, line change, submission and return to draft: an approval or posting checked against an older version is refused (access R14, R42) / يزداد مع كل تعديل أو تغيير بند أو تقديم أو إعادة إلى مسودة: يُرفض الاعتماد المبني على نسخة أقدم",
                verbose_name="Version / رقم النسخة",
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
