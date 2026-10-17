"""hr R42 to R45 (AT-23): document expiry, alerts, renewal."""
from datetime import date, timedelta

import pytest

from hr import services
from hr.conftest import TODAY, hired
from hr.models import EmployeeDocument, HrAlert, HrError


def _doc(w, emp, doc_type, expiry, number="X0000001", **fields):
    return services.add_document(emp, user=w.h, doc_type=doc_type, number=number, expiry_date=expiry, **fields)


def test_at23_alerts_once_to_hr_officers_without_number(w):
    emp = hired(w, saudi=False)
    iqama = _doc(w, emp, "iqama", TODAY + timedelta(days=59), number="2000000555")
    _doc(w, emp, "passport", TODAY - timedelta(days=1), number="P9999001")
    assert services.send_document_alerts(today=TODAY) == 2
    assert services.send_document_alerts(today=TODAY) == 0  # idempotent
    alerts = list(HrAlert.objects.filter(kind="document_expiry"))
    assert {a.recipient.username for a in alerts} == {"h", "h2"}  # ALM hr officers only
    assert len(alerts) == 4
    iqama_alerts = [a for a in alerts if "Iqama" in a.text_en]
    assert len(iqama_alerts) == 2 and "59 days" in iqama_alerts[0].text_en and "الإقامة" in iqama_alerts[0].text_ar
    assert all(emp.employee_code in a.text_en and "2000000555" not in a.text_en + a.text_ar
               and "P9999001" not in a.text_en + a.text_ar for a in alerts)
    iqama.refresh_from_db()
    assert set(iqama.alerts_sent) == {"90", "60"}  # only the 60-day alert sent; 90 marked
    assert services.document_flag(emp, user=w.h)[0] == "expired"
    # Renewal: the old iqama is superseded and stops alerting.
    new = _doc(w, emp, "iqama", TODAY + timedelta(days=700), number="2000000556", supersedes=iqama)
    iqama.refresh_from_db()
    assert iqama.superseded_by == new
    assert services.send_document_alerts(today=TODAY + timedelta(days=40)) == 0
    with pytest.raises(HrError):
        iqama.delete()


def test_r43_next_threshold_alerts_later(w):
    emp = hired(w, saudi=False)
    _doc(w, emp, "work_permit", TODAY + timedelta(days=59))
    services.send_document_alerts(today=TODAY)
    assert services.send_document_alerts(today=TODAY + timedelta(days=29)) == 1  # 30-day threshold


def test_r42_expiry_required_and_iqama_warning(w):
    emp = hired(w, saudi=False)
    with pytest.raises(HrError) as exc:
        services.add_document(emp, user=w.h, doc_type="iqama", number="2000000557")
    assert exc.value.code == "expiry_required"
    flag, warnings = services.document_flag(emp, user=w.h)
    assert flag == "ok" and warnings and "iqama" in warnings[0]
    _doc(w, emp, "iqama", TODAY + timedelta(days=200))
    assert services.document_flag(emp, user=w.h) == ("ok", [])
    _doc(w, emp, "passport", TODAY + timedelta(days=80))
    assert services.document_flag(emp, user=w.h)[0] == "expiring"


def test_r52_remuneration_needs_no_iqama(w):
    r = hired(w, basis="remuneration")
    _doc(w, r, "passport", TODAY + timedelta(days=400))
    assert services.document_flag(r, user=w.h) == ("ok", [])


def test_r45_renewal_must_match_type_and_employee(w):
    emp, other = hired(w, saudi=False), hired(w, saudi=False)
    old = _doc(w, emp, "iqama", TODAY + timedelta(days=30))
    with pytest.raises(HrError):
        _doc(w, other, "iqama", TODAY + timedelta(days=400), supersedes=old)
    with pytest.raises(HrError):
        _doc(w, emp, "passport", TODAY + timedelta(days=400), supersedes=old)
    assert EmployeeDocument.objects.get(pk=old.pk).superseded_by is None


def test_r10_document_number_encrypted(w):
    from django.db import connection

    doc = _doc(w, hired(w, saudi=False), "passport", date(2030, 1, 1), number="P1234567")
    with connection.cursor() as cur:
        cur.execute("SELECT number FROM hr_employeedocument WHERE id = %s", [doc.pk])
        assert cur.fetchone()[0].startswith("enc1:")
    assert EmployeeDocument.objects.get(pk=doc.pk).number == "P1234567"
