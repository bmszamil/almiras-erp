"""Review finding 9: PostgreSQL triggers for the HR records the ORM guards protect, so raw SQL cannot bypass them
(review probes PG2). PostgreSQL only; on SQLite the model and queryset guards apply.

- hr_finalsettlementinput: inserted as a draft; only its status moves, draft -> superseded or discarded; never
  deleted (R38, R40, R41).
- hr_leaverequest: created as a draft or submitted; approved only from submitted, by someone other than its
  creator and submitter; an approved or taken request changes only by cancellation, a shorter end, the late-birth
  extension (once, with the birth and the unpaid start) or the exam result (once; not taken makes it unpaid);
  rejected and cancelled requests never change; only drafts are deleted (R31 to R33).
- hr_employee: created as a draft; entity and code never change; status moves only draft -> active ->
  on_notice / terminated (and on_notice -> active on withdrawal); the employment basis is fixed once a contract
  version is approved; a terminated employee changes only by re-encryption (hr_rehash_personal_data); only drafts
  are deleted (R8, R14, R38, R41, R49).
- hr_assignment: created as a draft; approved only from submitted, with a reference, by someone other than its
  creator and submitter; once approved only its status and end change (R22 to R27, R41).
- hr_leaveadjustment: created as a draft; approved by someone other than its creator; approved and rejected
  adjustments never change; never deleted after draft (R29).
- hr_termination (replaces the 0003 function): inserted as a draft; approved only from submitted, by someone other
  than its creator and submitter, or with no approver only as an Art. 79 bis acceptance by law of a resignation;
  an approved termination changes only by withdrawal or by recording its GOSI exclusion once (R36, R41, R65).
- hr_employmentcontract (replaces the 0007 function): also refuses an approval by its creator or submitter.
- hr_assignmentevent (replaces the 0003 function): a delivered or superseded event never changes; only a failed
  event is superseded (R25, finding 10).
- hr_assignmenteventaction: append-only. hr_hrchecklistitem: a closed item never changes; never deleted.
  hr_pregnancynotice: only its end date is set, once; never deleted.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION hr_settlement_input_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'final settlement inputs are never deleted (hr R38)';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'final settlement inputs are written as drafts (hr R38)';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status <> 'draft' OR NEW.status NOT IN ('superseded', 'discarded')
            OR NOT hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW)) <@ ARRAY['status'] THEN
        RAISE EXCEPTION 'final settlement input % changes only from draft to superseded or discarded (hr R40, R41)',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_leaverequest_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'leave request % is %; only drafts are deleted (hr R31)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status NOT IN ('draft', 'submitted') OR NEW.approved_by_id IS NOT NULL THEN
            RAISE EXCEPTION 'leave requests are created as drafts or submitted (hr R31)';
        END IF;
        RETURN NEW;
    END IF;
    changed := hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'employee_id', 'leave_type_id', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'leave request %: entity, employee and type never change', OLD.id;
    END IF;
    IF OLD.status IN ('rejected', 'cancelled') AND array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'leave request % is %; it never changes', OLD.id, OLD.status;
    END IF;
    IF NEW.status = 'approved' AND OLD.status NOT IN ('approved', 'taken') THEN
        IF OLD.status <> 'submitted' OR NEW.approved_by_id IS NULL OR NEW.approved_by_id = NEW.created_by_id
                OR NEW.approved_by_id IS NOT DISTINCT FROM NEW.submitted_by_id THEN
            RAISE EXCEPTION 'leave request % is approved only from submitted, by someone other than its requester (hr R31)',
                OLD.id;
        END IF;
    END IF;
    IF OLD.status IN ('approved', 'taken') THEN
        IF NEW.status NOT IN ('approved', 'taken', 'cancelled')
                OR NOT changed <@ ARRAY['status', 'end_date', 'days', 'actual_event_date', 'unpaid_from', 'exam_taken',
                                        'unpaid', 'modified_by_id', 'modified_at'] THEN
            RAISE EXCEPTION 'approved leave request % changes only by cancellation, shortening, the birth or the exam result (hr R31, R33)',
                OLD.id;
        END IF;
        IF NEW.end_date > OLD.end_date AND NOT (OLD.unpaid_from IS NULL AND NEW.unpaid_from IS NOT NULL
                AND OLD.actual_event_date IS NULL AND NEW.actual_event_date IS NOT NULL) THEN
            RAISE EXCEPTION 'approved leave request % is extended only by a late birth (hr R33)', OLD.id;
        END IF;
        IF (OLD.actual_event_date IS NOT NULL AND NEW.actual_event_date IS DISTINCT FROM OLD.actual_event_date)
                OR (OLD.unpaid_from IS NOT NULL AND NEW.unpaid_from IS DISTINCT FROM OLD.unpaid_from)
                OR (OLD.exam_taken IS NOT NULL AND NEW.exam_taken IS DISTINCT FROM OLD.exam_taken) THEN
            RAISE EXCEPTION 'leave request %: the birth and the exam result are recorded once (hr R33)', OLD.id;
        END IF;
        IF NEW.unpaid IS DISTINCT FROM OLD.unpaid AND NOT (OLD.unpaid = false AND NEW.unpaid = true
                AND OLD.exam_taken IS NULL AND NEW.exam_taken = false) THEN
            RAISE EXCEPTION 'leave request %: pay changes only when the exam was not taken (hr R33)', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_employee_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'employee % is %; only drafts are deleted (hr R14)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'employees are created as drafts (hr R8)';
        END IF;
        RETURN NEW;
    END IF;
    changed := hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'employee_code', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'employee %: entity and code never change (hr R1, R3)', OLD.id;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
            (OLD.status = 'draft' AND NEW.status = 'active')
            OR (OLD.status = 'active' AND NEW.status IN ('on_notice', 'terminated'))
            OR (OLD.status = 'on_notice' AND NEW.status IN ('terminated', 'active'))) THEN
        RAISE EXCEPTION 'employee %: status does not move from % to % (hr R8, R38)', OLD.id, OLD.status, NEW.status;
    END IF;
    IF NEW.employment_basis IS DISTINCT FROM OLD.employment_basis AND EXISTS (
            SELECT 1 FROM hr_employmentcontract WHERE employee_id = OLD.id AND status = 'approved') THEN
        RAISE EXCEPTION 'employee %: the employment basis is fixed once a contract is approved (hr R49)', OLD.id;
    END IF;
    IF OLD.status = 'terminated' AND NOT changed <@ ARRAY[
            'full_name_en', 'full_name_ar', 'id_number', 'id_number_hash', 'date_of_birth', 'gender', 'phone', 'email',
            'address', 'bank_name', 'iban', 'country_of_residence', 'gosi_number', 'gosi_cohort',
            'gosi_first_coverage_date'] THEN
        RAISE EXCEPTION 'terminated employee % is read-only (hr R14)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_assignment_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'assignment % is %; only drafts are deleted (hr R23)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.approved_by_id IS NOT NULL THEN
            RAISE EXCEPTION 'assignments are created as drafts (hr R23)';
        END IF;
        RETURN NEW;
    END IF;
    changed := hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'employee_id', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'assignment %: entity and employee never change', OLD.id;
    END IF;
    IF OLD.status IN ('draft', 'submitted') AND NEW.status IN ('approved', 'active') THEN
        IF OLD.status <> 'submitted' OR NEW.approved_by_id IS NULL OR NEW.reference = ''
                OR NEW.approved_by_id = NEW.created_by_id OR NEW.approved_by_id IS NOT DISTINCT FROM NEW.submitted_by_id THEN
            RAISE EXCEPTION 'assignment % is approved only from submitted, with a reference, by someone other than its drafter (hr R23)',
                OLD.id;
        END IF;
    END IF;
    IF OLD.status IN ('approved', 'active', 'ended', 'cancelled') THEN
        IF NEW.status IN ('draft', 'submitted') OR NOT changed <@ ARRAY['status', 'end_date', 'end_reason_en',
                'end_reason_ar', 'ended_by_id', 'ended_at', 'modified_by_id', 'modified_at'] THEN
            RAISE EXCEPTION 'approved assignment % changes only its status and end (hr R24, R27)', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_leaveadjustment_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'leave adjustment % is %; it is never deleted (hr R29)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.approved_by_id IS NOT NULL THEN
            RAISE EXCEPTION 'leave adjustments are created as drafts (hr R29)';
        END IF;
        RETURN NEW;
    END IF;
    changed := hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'employee_id', 'leave_type_id', 'kind', 'source', 'created_by_id',
                        'created_at'] THEN
        RAISE EXCEPTION 'leave adjustment %: entity, employee, type and kind never change', OLD.id;
    END IF;
    IF OLD.status IN ('approved', 'rejected') AND array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'leave adjustment % is %; it never changes (hr R29)', OLD.id, OLD.status;
    END IF;
    IF NEW.status = 'approved' AND OLD.status = 'draft'
            AND (NEW.approved_by_id IS NULL OR NEW.approved_by_id = NEW.created_by_id) THEN
        RAISE EXCEPTION 'leave adjustment % is approved by someone other than its creator (hr R29)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_termination_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'termination % is %; only drafts are deleted (hr R41)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.approved_by_id IS NOT NULL THEN
            RAISE EXCEPTION 'terminations are created as drafts (hr R36)';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'withdrawn' THEN
        RAISE EXCEPTION 'withdrawn termination % never changes', OLD.id;
    END IF;
    changed := hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'employee_id', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'termination %: entity and employee never change', OLD.id;
    END IF;
    IF NEW.status = 'approved' AND OLD.status <> 'approved' THEN
        IF OLD.status <> 'submitted' THEN
            RAISE EXCEPTION 'termination % is approved only from submitted (hr R36)', OLD.id;
        END IF;
        IF NEW.approved_by_id IS NULL THEN
            IF NOT (NEW.kind = 'resignation' AND NEW.accepted_on IS NOT NULL) THEN
                RAISE EXCEPTION 'termination % is approved by an approver (only a resignation is accepted by law, Art. 79 bis)',
                    OLD.id;
            END IF;
        ELSIF NEW.approved_by_id = NEW.created_by_id OR NEW.approved_by_id IS NOT DISTINCT FROM NEW.submitted_by_id THEN
            RAISE EXCEPTION 'termination % is approved by someone other than its drafter and submitter (hr R36)', OLD.id;
        END IF;
    END IF;
    IF OLD.status = 'approved' THEN
        IF NEW.status = 'withdrawn' THEN
            IF NOT changed <@ ARRAY['status', 'withdrawn_by_id', 'withdrawn_at', 'modified_by_id', 'modified_at'] THEN
                RAISE EXCEPTION 'approved termination % changes only by withdrawal (hr R41)', OLD.id;
            END IF;
        ELSIF NEW.status = 'approved' THEN
            IF NOT changed <@ ARRAY['gosi_exclusion_reason', 'gosi_excluded_on', 'gosi_exclusion_ref']
                    OR OLD.gosi_excluded_on IS NOT NULL OR OLD.gosi_exclusion_ref <> ''
                    OR (OLD.gosi_exclusion_reason <> '' AND NEW.gosi_exclusion_reason <> OLD.gosi_exclusion_reason) THEN
                RAISE EXCEPTION 'approved termination % records its GOSI exclusion once and changes no other way (hr R65)',
                    OLD.id;
            END IF;
        ELSE
            RAISE EXCEPTION 'approved termination % changes only by withdrawal (hr R41)', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

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
    ELSIF NEW.status = 'approved' AND (NEW.approved_by_id IS NULL OR OLD.status <> 'submitted'
            OR NEW.approved_by_id = NEW.created_by_id OR NEW.approved_by_id IS NOT DISTINCT FROM NEW.submitted_by_id) THEN
        RAISE EXCEPTION 'contract version % is approved only from submitted, by someone other than its drafter and submitter (hr R15)',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_event_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'assignment events are never deleted (hr R24)';
    END IF;
    IF OLD.delivery_status IN ('delivered', 'superseded') OR NOT hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW))
            <@ ARRAY['delivery_status', 'attempts', 'last_error_en', 'last_error_ar', 'billing_assignment_id',
                     'delivered_at'] THEN
        RAISE EXCEPTION 'assignment event %: only its delivery changes, and never after delivery or supersession (hr R25)',
            OLD.id;
    END IF;
    IF NEW.delivery_status = 'superseded' AND OLD.delivery_status <> 'failed' THEN
        RAISE EXCEPTION 'assignment event %: only a failed event is superseded (hr R25)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_checklist_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'HR checklist items are never deleted (hr R65)';
    END IF;
    IF OLD.status = 'closed' OR NOT hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW))
            <@ ARRAY['status', 'reminders_sent', 'closed_by_id', 'closed_at']
            OR (NEW.status = 'closed' AND NEW.closed_by_id IS NULL) THEN
        RAISE EXCEPTION 'HR checklist item %: a closed item never changes; closing names who closed it (hr R65)',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_pregnancy_notice_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'pregnancy notices are never deleted (hr R60)';
    END IF;
    IF OLD.ended_on IS NOT NULL OR NOT hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW))
            <@ ARRAY['ended_on', 'modified_by_id', 'modified_at', 'document_ref'] THEN
        RAISE EXCEPTION 'pregnancy notice %: only its end date is set, once (hr R60)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS hr_employee_delete_lock ON hr_employee;
CREATE TRIGGER hr_employee_lock BEFORE INSERT OR UPDATE OR DELETE ON hr_employee
    FOR EACH ROW EXECUTE FUNCTION hr_employee_lock();
DROP TRIGGER IF EXISTS hr_termination_lock ON hr_termination;
CREATE TRIGGER hr_termination_lock BEFORE INSERT OR UPDATE OR DELETE ON hr_termination
    FOR EACH ROW EXECUTE FUNCTION hr_termination_lock();
CREATE TRIGGER hr_settlement_input_lock BEFORE INSERT OR UPDATE OR DELETE ON hr_finalsettlementinput
    FOR EACH ROW EXECUTE FUNCTION hr_settlement_input_lock();
CREATE TRIGGER hr_leaverequest_lock BEFORE INSERT OR UPDATE OR DELETE ON hr_leaverequest
    FOR EACH ROW EXECUTE FUNCTION hr_leaverequest_lock();
CREATE TRIGGER hr_assignment_lock BEFORE INSERT OR UPDATE OR DELETE ON hr_assignment
    FOR EACH ROW EXECUTE FUNCTION hr_assignment_lock();
CREATE TRIGGER hr_leaveadjustment_lock BEFORE INSERT OR UPDATE OR DELETE ON hr_leaveadjustment
    FOR EACH ROW EXECUTE FUNCTION hr_leaveadjustment_lock();
CREATE TRIGGER hr_assignmenteventaction_append_only BEFORE UPDATE OR DELETE ON hr_assignmenteventaction
    FOR EACH ROW EXECUTE FUNCTION hr_append_only();
CREATE TRIGGER hr_checklist_lock BEFORE UPDATE OR DELETE ON hr_hrchecklistitem
    FOR EACH ROW EXECUTE FUNCTION hr_checklist_lock();
CREATE TRIGGER hr_pregnancy_notice_lock BEFORE UPDATE OR DELETE ON hr_pregnancynotice
    FOR EACH ROW EXECUTE FUNCTION hr_pregnancy_notice_lock();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS hr_pregnancy_notice_lock ON hr_pregnancynotice;
DROP TRIGGER IF EXISTS hr_checklist_lock ON hr_hrchecklistitem;
DROP TRIGGER IF EXISTS hr_assignmenteventaction_append_only ON hr_assignmenteventaction;
DROP TRIGGER IF EXISTS hr_leaveadjustment_lock ON hr_leaveadjustment;
DROP TRIGGER IF EXISTS hr_assignment_lock ON hr_assignment;
DROP TRIGGER IF EXISTS hr_leaverequest_lock ON hr_leaverequest;
DROP TRIGGER IF EXISTS hr_settlement_input_lock ON hr_finalsettlementinput;
DROP TRIGGER IF EXISTS hr_termination_lock ON hr_termination;
CREATE TRIGGER hr_termination_lock BEFORE UPDATE OR DELETE ON hr_termination
    FOR EACH ROW EXECUTE FUNCTION hr_termination_lock();
DROP TRIGGER IF EXISTS hr_employee_lock ON hr_employee;
CREATE TRIGGER hr_employee_delete_lock BEFORE DELETE ON hr_employee
    FOR EACH ROW EXECUTE FUNCTION hr_employee_delete_lock();
DROP FUNCTION IF EXISTS hr_pregnancy_notice_lock();
DROP FUNCTION IF EXISTS hr_checklist_lock();
DROP FUNCTION IF EXISTS hr_leaveadjustment_lock();
DROP FUNCTION IF EXISTS hr_assignment_lock();
DROP FUNCTION IF EXISTS hr_leaverequest_lock();
DROP FUNCTION IF EXISTS hr_settlement_input_lock();
DROP FUNCTION IF EXISTS hr_employee_lock();
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)


def _previous_functions():
    """The termination, event and contract functions as 0003 / 0007 left them (for the reverse migration)."""
    from importlib import import_module

    old = import_module("hr.migrations.0003_immutability_triggers").FORWARD
    contract = import_module("hr.migrations.0007_contract_lock_qiwa_reference").FUNCTION
    event = old[old.index("CREATE OR REPLACE FUNCTION hr_event_lock()"):old.index("CREATE OR REPLACE FUNCTION hr_termination_lock()")]
    term = old[old.index("CREATE OR REPLACE FUNCTION hr_termination_lock()"):old.index("CREATE OR REPLACE FUNCTION hr_employee_delete_lock()")]
    return event + term + contract


def forward(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backward(apps, schema_editor):
    _run(schema_editor, REVERSE + _previous_functions())


class Migration(migrations.Migration):
    dependencies = [("hr", "0015_spec_rules_gosi_notice_exam")]

    operations = [migrations.RunPython(forward, backward)]
