"""PostgreSQL only: the tax immutability triggers (migration 0003) refuse raw SQL that bypasses the ORM (AT-7, R3,
R26, R36)."""
from datetime import date

import pytest
from django.db import DatabaseError, connection, transaction

from tax import jobs, services
from tax.conftest import approved, bill, close_q1, filed, sale
from tax.models import TaxAlert, VatCorrection, VatReturn

pytestmark = [pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers"),
              pytest.mark.django_db]


def _refused(sql, params=()):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _ok(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def test_at7_raw_sql_cannot_change_a_locked_return(t):
    sale(t)
    bill(t, [("5300", "1", "1000.00")])  # an offset, so the return has its T1 entry
    draft = services.compute_draft(t.alm, date(2027, 4, 1), user=t.a1)
    _ok("UPDATE tax_vatreturnbox SET vat_sar = vat_sar WHERE vat_return_id = %s", [draft.pk])  # a draft is not locked
    close_q1(t)
    ret = approved(t)
    _refused("UPDATE tax_vatreturn SET net_vat_sar = 0 WHERE id = %s", [ret.pk])
    _refused("UPDATE tax_vatreturn SET status = 'draft' WHERE id = %s", [ret.pk])
    _refused("DELETE FROM tax_vatreturn WHERE id = %s", [ret.pk])
    _refused("UPDATE tax_vatreturnbox SET vat_sar = 1 WHERE vat_return_id = %s", [ret.pk])
    _refused("DELETE FROM tax_vatreturnsourceline WHERE vat_return_id = %s", [ret.pk])
    _refused("INSERT INTO tax_vatreturnsourceline (entity_id, vat_return_id, box_no, \"column\", source_type, "
             "document_number, amount_sar, note) VALUES (%s, %s, 1, 'vat', 'manual_adjustment', '', 1, '')",
             [t.alm.pk, ret.pk])
    assert ret.settlement_entry_id is not None
    _refused("UPDATE tax_vatreturn SET settlement_entry_id = NULL WHERE id = %s", [ret.pk])
    filed(t, ret)
    _refused("UPDATE tax_vatreturn SET zatca_filing_reference = 'X' WHERE id = %s", [ret.pk])
    ret.refresh_from_db()
    assert ret.net_vat_sar == ret.boxes.get(box_no=16).vat_sar


def test_r36_raw_sql_cannot_change_a_posted_payment(t):
    sale(t)
    close_q1(t)
    ret = filed(t, approved(t))
    payment = services.create_vat_payment(ret, user=t.a1, amount_sar="1500.00", payment_date=date(2027, 4, 25),
                                          bank_account="1100")
    services.submit_tax_document(payment, user=t.a1)
    services.approve_tax_document(payment, user=t.c1)
    _refused("UPDATE tax_taxpayment SET amount_sar = 1 WHERE id = %s", [payment.pk])
    _refused("UPDATE tax_taxpayment SET status = 'draft' WHERE id = %s", [payment.pk])
    _refused("DELETE FROM tax_taxpayment WHERE id = %s", [payment.pk])


def test_r3_raw_overlapping_period_refused(t):
    services.compute_draft(t.alm, date(2027, 1, 1), user=t.a1)
    _refused("INSERT INTO tax_vatreturnschedule (entity_id, period_start, period_end, due_date, frequency) "
             "VALUES (%s, '2027-02-01', '2027-02-28', '2027-03-31', 'monthly')", [t.alm.pk])
    _refused("UPDATE tax_vatreturnschedule SET due_date = '2027-05-31' WHERE entity_id = %s AND "
             "period_start = '2027-01-01'", [t.alm.pk])


def test_r26_correcting_entries_and_alerts_are_fixed(t):
    sale(t)
    close_q1(t)
    ret = filed(t, approved(t))
    missed = sale(t, on=date(2027, 4, 15))
    corr = services.record_correction(ret, user=t.a1, discovered_on=date(2027, 5, 1), description_en="Missed",
                                      description_ar="فاتورة لم تُدرج", tax_difference_sar="1500.00",
                                      correcting_entries=[missed.journal_entry])
    _refused("DELETE FROM tax_vatcorrection_correcting_entries WHERE vatcorrection_id = %s", [corr.pk])
    _refused("DELETE FROM tax_vatcorrection WHERE id = %s", [corr.pk])
    services.cancel_correction(corr, user=t.a1, reason="test")
    _refused("UPDATE tax_vatcorrection SET status = 'open' WHERE id = %s", [corr.pk])
    assert VatCorrection.objects.get(pk=corr.pk).status == "cancelled"
    jobs.deadline_alerts(today=date(2027, 5, 1))
    alert = TaxAlert.objects.first()
    _refused("UPDATE tax_taxalert SET message_en = 'x' WHERE id = %s", [alert.pk])
    _refused("DELETE FROM tax_taxalert WHERE id = %s", [alert.pk])


# --- review fixes (tax-returns §10.4): the reviewer's PostgreSQL probes, now refused ------------------------------

def test_item9_raw_sql_cannot_file_an_approved_return_without_its_filing_fields(t):
    sale(t)
    close_q1(t)
    ret = approved(t)
    _refused("UPDATE tax_vatreturn SET status = 'filed' WHERE id = %s", [ret.pk])
    draft = services.compute_draft(t.alm, date(2027, 4, 1), user=t.a1)
    _refused("UPDATE tax_vatreturn SET status = 'approved', approved_by_id = created_by_id WHERE id = %s", [draft.pk])
    _refused("UPDATE tax_vatreturn SET source = 'import' WHERE id = %s", [draft.pk])


def test_item9_raw_sql_cannot_insert_a_filed_return(t):
    sale(t)
    ret = services.compute_draft(t.alm, date(2027, 1, 1), user=t.a1)
    _refused("""INSERT INTO tax_vatreturn (entity_id, schedule_id, period_start, period_end, frequency, version,
                amends_id, due_date, status, source, created_by_id, created_at, returned_reason_en,
                returned_reason_ar, zatca_filing_reference, acknowledgement, sadad_bill_number, output_vat_sar,
                input_vat_sar, rc_output_vat_sar, net_vat_sar, refund_requested, source_hash,
                settlement_offset_sar, notes_en, notes_ar)
                VALUES (%s, %s, '2027-01-01', '2027-03-31', 'quarterly', 1, %s, '2027-04-30', 'filed', 'human',
                %s, now(), '', '', 'X', '', '', 0, 0, 0, -50000, false, '', 0, '', '')""",
             [t.alm.pk, ret.schedule_id, ret.pk, t.a1.pk])
    _refused("""INSERT INTO tax_vatreturn (entity_id, schedule_id, period_start, period_end, frequency, version,
                amends_id, due_date, status, source, created_by_id, created_at, returned_reason_en,
                returned_reason_ar, zatca_filing_reference, acknowledgement, sadad_bill_number, output_vat_sar,
                input_vat_sar, rc_output_vat_sar, net_vat_sar, refund_requested, source_hash,
                settlement_offset_sar, notes_en, notes_ar)
                VALUES (%s, %s, '2027-01-01', '2027-03-31', 'quarterly', 1, %s, '2027-04-30', 'draft', 'human',
                %s, now(), '', '', '', '', '', 0, 0, 0, 0, false, '', 0, '', '')""",
             [t.alm.pk, ret.schedule_id, ret.pk, t.a1.pk])  # an amendment of a return that is not filed
    assert not VatReturn.objects.filter(entity=t.alm, version=1).exists()


def test_item9_raw_sql_cannot_change_a_correction_carried_by_an_approved_return(t):
    from tax.conftest import imported, journal, prepared, reviewed

    old = imported(t, date(2026, 10, 1), ref="OLD", filed_on=date(2027, 1, 20))
    entry = journal(t, date(2027, 2, 2), [{"account": "1100", "debit": "100.00"}, {"account": "2200", "credit": "100.00"}])
    corr = services.record_correction(old, user=t.a1, discovered_on=date(2027, 2, 1), description_en="x",
                                      description_ar="س", tax_difference_sar="100.00", correcting_entries=[entry])
    sale(t)
    ret = prepared(t)
    services.acknowledge_exception(ret, user=t.a2, kind="correcting_entry", reference=f"je:{entry.pk}",
                                   decision="confirmed_correction", reason_ar="قيد التصحيح")
    close_q1(t)
    ret = services.approve_return(reviewed(t, ret), user=t.c1)
    corr.refresh_from_db()
    assert corr.status == "assigned" and corr.target_return_id == ret.pk
    _refused("UPDATE tax_vatcorrection SET tax_difference_sar = 1, target_return_id = NULL, status = 'open' "
             "WHERE id = %s", [corr.pk])
    other = journal(t, date(2027, 4, 2), [{"account": "1100", "debit": "1.00"}, {"account": "2200", "credit": "1.00"}])
    _refused("INSERT INTO tax_vatcorrection_correcting_entries (vatcorrection_id, journalentry_id) VALUES (%s, %s)",
             [corr.pk, other.pk])
    corr.refresh_from_db()
    assert (corr.tax_difference_sar, corr.status) == (100, "assigned")
    filed(t, ret)  # filing still changes it
    assert VatCorrection.objects.get(pk=corr.pk).status == "filed"


def test_item3_raw_sql_import_reaches_filed_only_through_review_and_approval_before_golive(t):
    draft = services.import_filed_return(t.alm, user=t.a1, period_start=date(2026, 10, 1), zatca_filing_reference="F",
                                         filed_on=date(2027, 1, 20), boxes={})
    _refused("UPDATE tax_vatreturn SET status = 'filed' WHERE id = %s", [draft.pk])
    _ok("UPDATE tax_vatreturn SET status = 'reviewed', reviewed_by_id = created_by_id WHERE id = %s", [draft.pk])
    _refused("UPDATE tax_vatreturn SET status = 'filed', approved_by_id = created_by_id WHERE id = %s", [draft.pk])
    _ok("UPDATE tax_taxsettings SET vat_golive_date = '2026-10-01' WHERE entity_id = %s", [t.alm.pk])
    _refused("UPDATE tax_vatreturn SET status = 'filed', approved_by_id = %s, reviewed_by_id = %s WHERE id = %s",
             [t.c1.pk, t.a2.pk, draft.pk])  # the period is no longer before the go-live date
    _ok("UPDATE tax_taxsettings SET vat_golive_date = '2027-01-01' WHERE entity_id = %s", [t.alm.pk])
    _ok("UPDATE tax_vatreturn SET status = 'filed', approved_by_id = %s, reviewed_by_id = %s WHERE id = %s",
        [t.c1.pk, t.a2.pk, draft.pk])


def test_item6_frequency_history_is_append_only(t):
    from core import services as core_services
    from invoicing.conftest import make_entity

    new = make_entity("FRQ", "full", t.alm, vat="366666666600003", cr="1010000013", vat_effective_date=date(2027, 1, 1))
    core_services.change_entity_settings(new, user=t.mp, reason="Monthly", vat_return_period="monthly")
    change = new.vat_frequency_changes.get()
    _refused("UPDATE tax_vatfrequencychange SET frequency = 'quarterly' WHERE id = %s", [change.pk])
    _refused("DELETE FROM tax_vatfrequencychange WHERE id = %s", [change.pk])
