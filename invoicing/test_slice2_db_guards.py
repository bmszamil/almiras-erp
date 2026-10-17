"""Slice 2 rows in the database (PostgreSQL triggers of migrations 0015 to 0017): withholding tax deductions and
verified treaty certificates never change, posted purification rows never change, billing links never change and
a final document changes only for the anonymisation job after its retention date (R52, R54, R60, R62, R73)."""
from datetime import date

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from invoicing import services
from invoicing.conftest import final, invoice
from invoicing.test_purification import flagged_invoice
from invoicing.test_withholding_tax import nr_bill, nr_vendor, pay

pytestmark = [pytest.mark.django_db,
              pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")]


def _refused(sql, params=()):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def test_wht_rows_and_verified_certificates_are_locked(w):
    nr = nr_vendor(w)
    p = pay(w, nr, "9500.00", [(nr_bill(w, nr), "10000.00")])
    _refused("UPDATE invoicing_whtdeduction SET wht_amount = 0 WHERE payment_id = %s", [p.pk])
    _refused("DELETE FROM invoicing_whtdeduction WHERE payment_id = %s", [p.pk])
    cert = services.add_treaty_certificate(nr, user=w.ap, country="GB", treaty_rate="0.00", valid_from=date(2027, 1, 1),
                                           valid_to=date(2027, 12, 31), received_on=date(2027, 1, 1))
    services.verify_treaty_certificate(cert, user=w.accountant)
    _refused("UPDATE invoicing_treatycertificate SET treaty_rate = 1 WHERE id = %s", [cert.pk])


def test_posted_purification_never_changes(w):
    flagged_invoice(w)
    t = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31),
                                              amount="100.00")
    t = services.post_purification_transfer(t, user=w.cfo)
    _refused("UPDATE invoicing_purificationtransfer SET amount = 1 WHERE id = %s", [t.pk])
    _refused("DELETE FROM invoicing_purificationtransfer WHERE id = %s", [t.pk])
    _refused("INSERT INTO invoicing_purificationtransfer (entity_id, kind, status, number, date, amount, recipient_en, "
             "recipient_ar, reference_en, reference_ar, created_by_id, created_at, updated_at, source, version) "
             "VALUES (%s, 'transfer', 'posted', 'X', '2027-01-31', 1, '', '', '', '', %s, now(), now(), 'human', 0)",
             [w.alm.pk, w.accountant.pk])


def test_final_documents_change_only_for_anonymisation_after_retention(w):
    doc = final(w, invoice(w))
    later = timezone.now()
    _refused("UPDATE invoicing_salesdocument SET buyer_name_ar = 'x', anonymised_at = %s WHERE id = %s",
             [later, doc.pk])  # retention_until (2042) not over
    _refused("UPDATE invoicing_salesdocument SET total_net = 0 WHERE id = %s", [doc.pk])
    with transaction.atomic(), connection.cursor() as cursor:  # the job after retention: allowed
        cursor.execute("UPDATE invoicing_salesdocument SET buyer_name_ar = 'مجهّل', anonymised_at = %s "
                       "WHERE id = %s", [doc.retention_until.replace(year=doc.retention_until.year + 1).isoformat()
                                         + "T12:00:00+03:00", doc.pk])
    _refused("UPDATE invoicing_salesdocument SET buyer_name_en = 'again' WHERE id = %s", [doc.pk])  # once only


def test_purification_reversal_only_with_its_own_reversal_entry(w):
    flagged_invoice(w)
    t = services.post_purification_transfer(services.create_purification_transfer(
        w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31), amount="100.00"), user=w.cfo)
    other = services.post_purification_transfer(services.create_purification_transfer(
        w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31), amount="50.00"), user=w.cfo)
    _refused("UPDATE invoicing_purificationtransfer SET status = 'reversed', reversal_entry_id = %s, "
             "reversed_by_id = %s, reversed_at = now() WHERE id = %s",
             [other.journal_entry_id, w.accountant.pk, t.pk])  # not a reversal of its own entry
    r = services.reverse_purification_transfer(t, user=w.accountant, reason="خطأ")
    _refused("UPDATE invoicing_purificationtransfer SET reversal_reason = 'x' WHERE id = %s", [r.pk])
    _refused("UPDATE invoicing_purificationtransfer SET status = 'posted' WHERE id = %s", [r.pk])
    _refused("DELETE FROM invoicing_purificationtransfer WHERE id = %s", [r.pk])


def test_unverified_certificate_edits_record_the_modifier_and_a_third_person_verifies(w):
    nr = nr_vendor(w)
    cert = services.add_treaty_certificate(nr, user=w.ap, country="GB", treaty_rate="5.00", valid_from=date(2027, 1, 1),
                                           valid_to=date(2027, 12, 31), received_on=date(2027, 1, 1))
    _refused("UPDATE invoicing_treatycertificate SET treaty_rate = 0 WHERE id = %s", [cert.pk])  # no modifier
    _refused("UPDATE invoicing_treatycertificate SET verified_by_id = created_by_id, verified_at = now() "
             "WHERE id = %s", [cert.pk])  # the creator never verifies
    _refused("UPDATE invoicing_treatycertificate SET vendor_id = %s, modified_by_id = %s, modified_at = now() "
             "WHERE id = %s", [w.vendor.pk, w.accountant.pk, cert.pk])
    services.update_treaty_certificate(cert, user=w.accountant, treaty_rate="1.00")
    _refused("UPDATE invoicing_treatycertificate SET verified_by_id = %s, verified_at = now() WHERE id = %s",
             [w.accountant.pk, cert.pk])  # nor the last modifier
    _refused("DELETE FROM invoicing_treatycertificate WHERE id = %s", [cert.pk])

