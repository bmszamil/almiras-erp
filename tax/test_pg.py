"""PostgreSQL only: the tax immutability triggers (migration 0003) refuse raw SQL that bypasses the ORM (AT-7, R3,
R26, R36)."""
from datetime import date

import pytest
from django.db import DatabaseError, connection, transaction

from tax import jobs, services
from tax.conftest import approved, bill, close_q1, filed, sale
from tax.models import TaxAlert, VatCorrection

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
