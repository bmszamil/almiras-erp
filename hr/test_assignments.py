"""hr R22 to R28 (AT-7, AT-12, AT-13, AT-14): secondments and the billing port."""
from datetime import date

import pytest

from core import access
from hr import services
from hr.conftest import hired, make_entity
from hr.models import Assignment, AssignmentEvent, AssignmentEventAction, HrAlert, HrError

SECONDMENT = dict(kind="secondment", role_title_en="Analyst", role_title_ar="محلل", start_date=date(2027, 2, 1))


def _approved(w, emp, **fields):
    a = services.create_assignment(emp, user=w.h, **{**SECONDMENT, "receiving_entity": w.sub, **fields})
    a = services.submit_assignment(a, user=w.h)
    return services.approve_assignment(a, user=w.c)


def test_at12_assignment_rules_and_reference(w):
    e1 = hired(w)
    other_group = make_entity("OTH", "parent")
    with pytest.raises(HrError):
        services.create_assignment(e1, user=w.h, **{**SECONDMENT, "receiving_entity": w.alm})
    with pytest.raises(HrError):
        services.create_assignment(e1, user=w.h, **{**SECONDMENT, "receiving_entity": other_group})
    with pytest.raises(HrError):
        services.create_assignment(e1, user=w.h, **{**SECONDMENT, "receiving_entity": w.sub,
                                                     "start_date": date(2026, 12, 1)})  # before the contract
    with pytest.raises(HrError):  # no receiver at all
        services.create_assignment(e1, user=w.h, **SECONDMENT)
    a = services.create_assignment(e1, user=w.h, **{**SECONDMENT, "receiving_entity": w.sub})
    a = services.submit_assignment(a, user=w.h)
    with pytest.raises(access.AccessDenied):
        services.approve_assignment(a, user=w.h)
    a = services.approve_assignment(a, user=w.c)
    assert a.reference == "ALM-ASG-2027-000001" and a.status == "active"
    with pytest.raises(HrError) as exc:  # overlapping second secondment
        services.create_assignment(e1, user=w.h, **{**SECONDMENT, "receiving_entity": w.sub,
                                                     "start_date": date(2027, 3, 1)})
    assert "overlapping" in exc.value.messages[0]
    assert w.billing.calls == []  # approval creates nothing in billing
    third = services.create_assignment(e1, user=w.h, kind="assignment", receiving_party_en="Client Co",
                                       receiving_party_ar="شركة العميل", role_title_en="Advisor",
                                       role_title_ar="مستشار", start_date=date(2027, 3, 1))
    assert third.pk  # a non-billable assignment may run alongside


def test_at12_future_start_is_approved_then_active(w):
    a = _approved(w, hired(w), start_date=date(2027, 7, 1))
    assert a.status == "approved"
    services.refresh_statuses(today=date(2027, 7, 1))
    a.refresh_from_db()
    assert a.status == "active"


def test_at13_end_once_repeat_no_op_change(w, django_capture_on_commit_callbacks):
    a = _approved(w, hired(w))
    w.billing.link(a.reference)
    with django_capture_on_commit_callbacks(execute=True):
        a, ev = services.end_assignment(a, date(2027, 3, 10), user=w.h, reason_ar="انتهاء المهمة")
    assert ev.kind == "ended" and a.status == "ended"
    with django_capture_on_commit_callbacks(execute=True):
        _, again = services.end_assignment(a, date(2027, 3, 10), user=w.h, reason_ar="انتهاء المهمة")
    assert again is None
    with django_capture_on_commit_callbacks(execute=True):
        _, changed = services.end_assignment(a, date(2027, 3, 20), user=w.h, reason_ar="تمديد")
    assert changed.kind == "end_changed" and changed.previous_end_date == date(2027, 3, 10)
    assert AssignmentEvent.objects.filter(assignment=a).count() == 2
    assert [c[2] for c in w.billing.calls] == [date(2027, 3, 10), date(2027, 3, 20)]
    assert all(c[4] == "hr_event" and c[3] == w.h.pk for c in w.billing.calls)
    with pytest.raises(HrError):
        services.end_assignment(a, date(2027, 1, 1), user=w.h, reason_ar="خطأ")  # before the start
    with pytest.raises(access.AccessDenied):
        services.end_assignment(a, date(2027, 3, 25), user=w.p, reason_ar="س")


def test_at14_billing_failure_never_rolls_back_hr_and_retries_once_delivered(w, django_capture_on_commit_callbacks):
    linked = _approved(w, hired(w))
    unlinked = _approved(w, hired(w))
    w.billing.link(linked.reference, "BILL-7")
    w.billing.fail_times = 1
    with django_capture_on_commit_callbacks(execute=True):
        _, ev = services.end_assignment(linked, date(2027, 4, 30), user=w.h, reason_ar="انتهاء")
        _, ev2 = services.end_assignment(unlinked, date(2027, 4, 30), user=w.h, reason_ar="انتهاء")
    ev.refresh_from_db()
    assert Assignment.objects.get(pk=linked.pk).status == "ended"  # HR end committed
    assert ev.delivery_status == "pending" and ev.attempts == 1 and ev.last_error_en and ev.last_error_ar
    ev2.refresh_from_db()
    assert ev2.delivery_status == "delivered" and ev2.billing_assignment_id == ""
    assert services.deliver_assignment_events() == 1
    assert services.deliver_assignment_events() == 0  # nothing left: never called twice
    ev.refresh_from_db()
    assert ev.delivery_status == "delivered" and ev.billing_assignment_id == "BILL-7"
    assert w.billing.calls == [("ALM", "BILL-7", date(2027, 4, 30), w.h.pk, "hr_event")]


def test_at14_five_failures_mark_failed_and_alert(w, settings):
    a = _approved(w, hired(w))
    w.billing.link(a.reference)
    w.billing.fail_times = 99
    _, ev = services.end_assignment(a, date(2027, 4, 30), user=w.h, reason_ar="انتهاء")
    for _ in range(6):
        services.deliver_assignment_events()
    ev.refresh_from_db()
    assert ev.delivery_status == "failed" and ev.attempts == settings.HR_ASSIGNMENT_EVENT_MAX_ATTEMPTS
    recipients = set(HrAlert.objects.filter(kind="billing_delivery_failed").values_list("recipient__username", flat=True))
    assert recipients == {"h", "h2", "a"}
    alert = HrAlert.objects.filter(kind="billing_delivery_failed").first()
    assert a.reference in alert.text_en and a.reference in alert.text_ar


def test_r25_events_of_one_assignment_are_delivered_in_order(w):
    a = _approved(w, hired(w))
    w.billing.link(a.reference)
    w.billing.fail_times = 1
    services.end_assignment(a, date(2027, 4, 30), user=w.h, reason_ar="انتهاء")
    services.deliver_assignment_events()  # first fails
    services.end_assignment(a, date(2027, 5, 15), user=w.h, reason_ar="تغيير")
    services.deliver_assignment_events()
    assert [c[2] for c in w.billing.calls] == [date(2027, 4, 30), date(2027, 5, 15)]


def test_r24_events_are_append_only(w):
    a = _approved(w, hired(w))
    _, ev = services.end_assignment(a, date(2027, 4, 30), user=w.h, reason_ar="انتهاء")
    ev.end_date = date(2027, 5, 1)
    with pytest.raises(HrError):
        ev.save()
    with pytest.raises(HrError):
        ev.delete()
    with pytest.raises(HrError):
        AssignmentEvent.objects.filter(pk=ev.pk).update(end_date=date(2027, 5, 1))


def test_r27_cancel_before_start_emits_cancelled(w):
    a = _approved(w, hired(w), start_date=date(2027, 8, 1))
    a, ev = services.cancel_assignment(a, user=w.h, reason_ar="إلغاء")
    assert a.status == "cancelled" and ev.kind == "cancelled" and ev.end_date == date(2027, 7, 31)
    started = _approved(w, hired(w))
    with pytest.raises(HrError):
        services.cancel_assignment(started, user=w.h, reason_ar="إلغاء")


def test_at7_billing_receives_no_personal_data(w):
    e1 = hired(w, full_name_en="Unique Fake Name", id_number="1000000999")
    a = _approved(w, e1)
    view = services.billing_view(a, user=w.a)
    assert view["employee_ref"] == e1.employee_code and view["hr_assignment_ref"] == a.reference
    text = repr(view)
    assert "Unique Fake Name" not in text and "1000000999" not in text and "6000" not in text
    _, ev = services.end_assignment(a, date(2027, 4, 30), user=w.h, reason_ar="انتهاء")
    fields = {f.name for f in AssignmentEvent._meta.get_fields()}
    assert not fields & {"full_name_en", "id_number", "salary"}


# --- Finding 10 (review probe P7): a failed event is re-queued or superseded, and blocked events are named ---------

def _failed_with_follow_up(w, settings):
    settings.HR_ASSIGNMENT_EVENT_MAX_ATTEMPTS = 1
    a = _approved(w, hired(w))
    w.billing.link(a.reference)
    w.billing.fail_times = 1
    _, first = services.end_assignment(a, date(2027, 7, 10), user=w.h, reason_ar="انتهاء")
    services.deliver_assignment_events()
    _, second = services.end_assignment(a, date(2027, 7, 20), user=w.h, reason_ar="تغيير")
    for _ in range(3):
        services.deliver_assignment_events()
    first.refresh_from_db()
    second.refresh_from_db()
    return a, first, second


def test_p7_a_failed_event_blocks_and_the_alerts_name_the_blocked_events(w, settings):
    a, first, second = _failed_with_follow_up(w, settings)
    assert (first.delivery_status, second.delivery_status) == ("failed", "pending") and w.billing.calls == []
    blocked = HrAlert.objects.filter(kind="billing_delivery_blocked").first()
    assert f"#{second.pk}" in blocked.text_en and f"#{first.pk}" in blocked.text_ar


def test_p7_requeue_delivers_the_failed_event_then_the_blocked_one_and_is_logged(w, settings):
    a, first, second = _failed_with_follow_up(w, settings)
    with pytest.raises(access.AccessDenied):
        services.requeue_assignment_event(first, user=w.p, reason_ar="أُصلحت الفوترة")
    with pytest.raises(HrError):
        services.requeue_assignment_event(first, user=w.h, reason_ar="")
    with pytest.raises(HrError) as exc:
        services.requeue_assignment_event(second, user=w.h, reason_ar="ليس متعثرًا")
    assert exc.value.code == "event_status"
    ev = services.requeue_assignment_event(first, user=w.h, reason_ar="أُصلحت الفوترة")
    assert ev.delivery_status == "pending" and ev.attempts == 0
    action = AssignmentEventAction.objects.get(event=first)
    assert action.action == "requeue" and action.actor == w.h and action.attempts_before == 1 and action.last_error_en
    services.deliver_assignment_events()
    assert [c[2] for c in w.billing.calls] == [date(2027, 7, 10), date(2027, 7, 20)]


def test_p7_superseding_a_failed_event_lets_the_later_one_through(w, settings):
    a, first, second = _failed_with_follow_up(w, settings)
    services.supersede_assignment_event(first, user=w.h, reason_ar="سُجل يدويًا في الفوترة")
    services.deliver_assignment_events()
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.delivery_status == "superseded" and second.delivery_status == "delivered"
    assert [c[2] for c in w.billing.calls] == [date(2027, 7, 20)]
    assert AssignmentEventAction.objects.get(event=first).action == "supersede"
    with pytest.raises(HrError):
        services.requeue_assignment_event(first, user=w.h, reason_ar="مرة أخرى")
