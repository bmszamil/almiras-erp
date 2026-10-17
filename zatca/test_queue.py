"""Submission queue and results (R-20 to R-29, R-35, R-44, R-47; AT-17 to AT-24, AT-29, AT-31, AT-32, AT-35,
AT-36, AT-47, AT-61, AT-73)."""
from datetime import date, timedelta

import pytest
from django.db import connection

from core import access
from core.access.testing import make_user, member
from invoicing import services as invoicing_services
from invoicing.models import SalesDocument
from ledger.models import JournalEntry
from zatca import client as zatca_client
from zatca import services
from zatca.conftest import NOW, TODAY, einvoice_of, issue, onboard, run
from zatca.messages import ZatcaError
from zatca.models import ComplianceTask, EgsUnit, EInvoice, OnboardingEvent, SubmissionAttempt, ZatcaAlert, ZatcaProfile
from zatca.testing import FakeZatcaClient, zatca_error, zatca_warning

pytestmark = pytest.mark.django_db
T = FakeZatcaClient


@pytest.fixture
def live(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    return zw


def no_entry(doc):
    return not JournalEntry.objects.filter(document_type="invoicing.salesdocument", document_id=doc.pk).exists()


def alerted(kind, entity):
    return set(ZatcaAlert.objects.filter(kind=kind, entity=entity).values_list("recipient__username", flat=True))


def test_at18_rejection_stores_messages_and_books_nothing(live):
    live.fakes.client.script["clearance"] = [T.rejected(zatca_error("BR-KSA-44", "Buyer VAT number is invalid"),
                                                         zatca_error("BR-XX-99", "Some new rule"))]
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "rejected"
    messages = list(einv.messages.filter(origin="zatca").order_by("id"))
    assert [(m.code, m.level) for m in messages] == [("BR-KSA-44", "error"), ("BR-XX-99", "error")]
    assert messages[0].message_en == "Buyer VAT number is invalid" and messages[0].message_ar  # from the table
    assert messages[1].message_ar == "" and messages[1].display_ar.startswith("رسالة من الهيئة غير مترجمة")
    assert not services.is_issued(doc) and no_entry(doc)
    doc.refresh_from_db()
    assert doc.status == "rejected" and [m["code"] for m in doc.zatca_messages] == ["BR-KSA-44", "BR-XX-99"]
    run(live, NOW + timedelta(hours=2))  # R-25: no automatic retry
    assert SubmissionAttempt.objects.filter(einvoice=einv).count() == 1


def test_at19_warnings_count_as_cleared(live):
    live.fakes.client.script["clearance"] = [lambda body: T.cleared(body, warnings=[
        zatca_warning("BR-KSA-08", "Seller identification should be checked")])]
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "cleared_with_warnings" and einv.cleared_xml
    w = einv.messages.get(level="warning")
    assert w.code == "BR-KSA-08" and w.message_ar
    assert services.is_issued(doc)
    doc.refresh_from_db()
    assert doc.status == "final" and doc.zatca_status == "cleared_with_warnings"


def test_r26_simplified_warnings_count_as_reported(live):
    live.fakes.client.script["reporting"] = [T.accepted_with_warnings("reporting", zatca_warning("X-1", "note"))]
    doc = issue(live, live.tst1, simplified=True)
    run(live)
    assert einvoice_of(doc).status == "reported_with_warnings"


def test_at22_timeouts_then_success_same_request(live):
    live.fakes.client.script["clearance"] = [T.timeout(), T.timeout(), T.timeout()]
    doc = issue(live, live.tst1)
    now = NOW
    run(live, now)
    for minutes in (1, 2, 4):  # backoff 1, 2, 4 minutes (R-24)
        einv = einvoice_of(doc)
        assert einv.status == "failed_retryable"
        assert einv.next_attempt_at == now + timedelta(minutes=minutes)
        run(live, now + timedelta(minutes=minutes) - timedelta(seconds=1))  # not yet due
        assert SubmissionAttempt.objects.filter(einvoice=einv).count() == {1: 1, 2: 2, 4: 3}[minutes]
        now = now + timedelta(minutes=minutes)
        run(live, now)
    einv = einvoice_of(doc)
    attempts = list(SubmissionAttempt.objects.filter(einvoice=einv).order_by("attempt_no"))
    assert [a.outcome for a in attempts] == ["transient_error"] * 3 + ["accepted"]
    assert len({a.request_sha256 for a in attempts}) == 1  # the same signed XML each time
    assert einv.status == "cleared" and einv.icv == 1
    bodies = [c[2] for c in live.fakes.client.calls if c[0] == "clearance"]
    assert len({b["uuid"] for b in bodies}) == 1 and len({b["invoice"] for b in bodies}) == 1


def test_at23_standard_fails_final_after_max_attempts(live, settings):
    settings.ZATCA_MAX_ATTEMPTS_STANDARD = 3
    live.fakes.client.script["clearance"] = [zatca_client.ApiResponse(503, {})] * 5
    doc = issue(live, live.tst1)
    now = NOW
    for _ in range(4):
        run(live, now)
        now += timedelta(hours=2)
    einv = einvoice_of(doc)
    assert einv.status == "failed_final" and einv.attempts.count() == 3
    assert alerted("failed_final", live.tst1) == {"admin1", "admin1b"}
    assert not services.is_issued(doc) and no_entry(doc)
    with pytest.raises(ZatcaError):  # R-20: no way out of failed_final here (OQ-7)
        services.retry_now(einv, user=live.admin1)


def test_at24_standard_document_never_forced_to_reporting(live):
    doc = issue(live, live.tst1)
    einv = einvoice_of(doc)
    services.stamp_document(einv, user=live.queue, now=NOW)
    with pytest.raises(ZatcaError) as exc:
        services.send_einvoice(einv, user=live.queue, api="reporting")
    assert exc.value.code == "wrong_api"
    simp = einvoice_of(issue(live, live.tst1, simplified=True))
    services.stamp_document(simp, user=live.queue, now=NOW)
    with pytest.raises(ZatcaError):
        services.send_einvoice(simp, user=live.queue, api="clearance")
    assert not [c for c in live.fakes.client.calls if c[0] in ("clearance", "reporting")]


def test_at47_clearance_disabled_goes_to_reporting_and_is_not_issued(live):
    live.fakes.client.script["clearance"] = [zatca_client.ApiResponse(303, {})]
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    attempts = list(einv.attempts.order_by("attempt_no"))
    assert [(a.api, a.outcome) for a in attempts] == [("clearance", "clearance_disabled"), ("reporting", "accepted")]
    assert attempts[0].request_sha256 == attempts[1].request_sha256
    assert einv.status == "reported_clearance_disabled" and einv.icv == 1
    assert not services.is_issued(doc) and no_entry(doc)
    doc.refresh_from_db()
    assert doc.status == "issued" and doc.zatca_status == "pending"
    alerts = ZatcaAlert.objects.filter(kind="clearance_disabled", entity=live.tst1)
    assert {a.recipient.username for a in alerts} == {"admin1", "admin1b"} and all(a.message_ar for a in alerts)


def test_at35_no_waiting_for_earlier_icv_unless_serialised(live, settings):
    first, second = issue(live, live.tst1), issue(live, live.tst1)
    e1, e2 = einvoice_of(first), einvoice_of(second)
    services.stamp_document(e1, user=live.queue, now=NOW)
    services.stamp_document(e2, user=live.queue, now=NOW)
    e1.refresh_from_db()
    e1.status = "submitting"  # a call for ICV 1 still in flight
    e1._service_write = True
    e1.save()
    settings.ZATCA_SERIALISE_PER_EGS = True
    run(live)
    e2.refresh_from_db()
    assert e2.status == "queued"  # waits for ICV 1
    settings.ZATCA_SERIALISE_PER_EGS = False
    run(live)
    e2.refresh_from_db()
    assert e2.icv == 2 and e2.status == "cleared"


def test_at21_deadline_alert_only_to_the_entitys_admins(live):
    onboard(live, live.tst2, live.admin2)
    live.fakes.client.script["reporting"] = [T.timeout()] * 50
    doc = issue(live, live.tst1, simplified=True)
    run(live)
    einv = einvoice_of(doc)
    services.run_deadline_checks(user=live.queue, now=einv.reporting_deadline - timedelta(hours=3))
    assert not alerted("reporting_deadline", live.tst1)
    services.run_deadline_checks(user=live.queue, now=einv.reporting_deadline - timedelta(hours=1))
    assert alerted("reporting_deadline", live.tst1) == {"admin1", "admin1b"}
    assert not ZatcaAlert.objects.filter(kind="reporting_deadline", entity=live.tst2).exists()
    services.run_deadline_checks(user=live.queue, now=einv.reporting_deadline - timedelta(minutes=30))
    assert ZatcaAlert.objects.filter(kind="reporting_deadline").count() == 2  # once per admin


def test_at36_simplified_never_final_and_late_notice_flow(live, settings):
    settings.ZATCA_MAX_ATTEMPTS_STANDARD = 3
    onboard(live, live.tst2, live.admin2)
    live.fakes.client.script["reporting"] = [T.timeout()] * 12
    doc = issue(live, live.tst1, simplified=True)
    now = NOW
    for _ in range(12):
        run(live, now)
        now += timedelta(hours=1, minutes=1)  # beyond the 1 h cap each time
    einv = einvoice_of(doc)
    assert einv.status == "failed_retryable" and einv.attempts.count() == 12  # one attempt per try
    assert einv.next_attempt_at - now + timedelta(hours=1, minutes=1) == timedelta(hours=1)  # at the cap
    services.run_deadline_checks(user=live.queue, now=einv.reporting_deadline + timedelta(minutes=1))
    tasks = ComplianceTask.objects.filter(kind="late_reporting_notice")
    assert [t.entity for t in tasks] == [live.tst1]
    task = tasks.get()
    assert task.title_ar and "zatca.gov.sa" in task.instructions_en and task.instructions_ar
    services.run_deadline_checks(user=live.queue, now=einv.reporting_deadline + timedelta(hours=2))
    assert ComplianceTask.objects.count() == 1
    with pytest.raises(ZatcaError):
        services.mark_task_notified(task, user=live.admin1, notified_at=None, notice_reference="")
    with pytest.raises(access.AccessDenied):  # §7: the queue worker opens it; only a ZATCA admin moves it
        services.mark_task_notified(task, user=live.accountant, notified_at=NOW, notice_reference="REF-1")
    task = services.mark_task_notified(task, user=live.admin1, notified_at=NOW, notice_reference="FAKE-REF-1")
    with pytest.raises(ZatcaError):  # not reported yet
        services.mark_task_done(task, user=live.admin1, recovered_notified_at=NOW, recovery_notice_reference="R")
    run(live, now + timedelta(hours=2))  # the fake recovers
    assert einvoice_of(doc).status == "reported"
    with pytest.raises(ZatcaError):
        services.mark_task_done(task, user=live.admin1, recovered_notified_at=None, recovery_notice_reference="")
    task = services.mark_task_done(task, user=live.admin1, recovered_notified_at=NOW,
                                   recovery_notice_reference="FAKE-REF-2")
    assert task.status == "done" and task.done_by == live.admin1
    with pytest.raises(ZatcaError):
        services.mark_task_notified(task, user=live.admin1, notified_at=NOW, notice_reference="again")


def test_at61_sealed_store_blocks_signing_not_other_rules(live):
    onboard(live, live.tst2, live.admin2)
    k, k2 = live.admin1, live.admin1b
    services.grant_key_delegation(live.tst1, user=live.mp, custodian=k, scope="master_key", openbao_policy_ref="m1")
    d2 = services.grant_key_delegation(live.tst1, user=live.mp, custodian=k2, scope="master_key",
                                       openbao_policy_ref="m1b")
    services.revoke_key_delegation(d2, user=live.mp, reason="left")
    old = issue(live, live.tst1, simplified=True)
    services.stamp_document(einvoice_of(old), user=live.queue, now=NOW)
    live.fakes.store.sealed = True
    new = issue(live, live.tst1, simplified=True)
    now = NOW
    for _ in range(3):
        run(live, now)
        now += timedelta(hours=2)
    e_new, e_old = einvoice_of(new), einvoice_of(old)
    assert e_new.status == "pending" and e_new.icv is None
    assert EgsUnit.objects.get(pk=live.egs1.pk).last_icv == 1
    assert e_old.status == "failed_retryable" and e_old.attempts.count() == 3
    assert {a.response_json["error"] for a in e_old.attempts.all()} == {"secret_store_unavailable"}
    for entity in (live.tst1, live.tst2):
        assert OnboardingEvent.objects.filter(entity=entity, action="secret_store_unavailable").count() == 1
    assert alerted("secret_store_unavailable", live.tst1) == {"admin1", "admin1b", "mp"}  # admins, MP, custodian K
    assert alerted("secret_store_unavailable", live.tst2) == {"admin2", "mp"}
    k2_alerts = ZatcaAlert.objects.filter(kind="secret_store_unavailable", recipient=k2)
    assert k2_alerts.count() == 1  # as a ZATCA admin of TST1 only, not as a (revoked) custodian
    services.run_deadline_checks(user=live.queue, now=e_old.reporting_deadline + timedelta(minutes=1))
    assert ComplianceTask.objects.filter(entity=live.tst1, einvoice=e_old).exists()
    live.fakes.store.sealed = False
    run(live, now + timedelta(hours=2))
    e_new.refresh_from_db()
    e_old.refresh_from_db()
    assert e_new.icv == 2 and e_old.status == "reported"
    assert OnboardingEvent.objects.filter(entity=live.tst1, action="secret_store_available").exists()


def test_at32_not_live_documents_are_not_submitted(zw):
    onboard(zw, zw.tst1, zw.admin1, go_live=date(2030, 1, 1))
    doc = issue(zw, zw.tst1)
    einv = einvoice_of(doc)
    assert einv.hold_reason == "not_live"
    run(zw)
    einv.refresh_from_db()
    assert einv.status == "pending" and einv.icv is None
    assert not [c for c in zw.fakes.client.calls if c[0] in ("clearance", "reporting")]
    doc.refresh_from_db()
    assert doc.zatca_status == "pending" and doc.zatca_messages[0]["code"] == "not_live"


def test_r28_entity_without_profile_keeps_the_old_hand_off(zw):
    doc = issue(zw, zw.tst2)
    assert not EInvoice.objects.filter(source_document=doc).exists()
    doc.refresh_from_db()
    assert doc.zatca_status == "pending" and doc.status == "issued"


def test_at31_lists_are_entity_filtered(live):
    onboard(live, live.tst2, live.admin2)
    issue(live, live.tst1)
    issue(live, live.tst2)
    assert {e.entity for e in services.einvoices(live.accountant)} == {live.tst1}
    assert {e.entity for e in services.einvoices(live.admin2)} == {live.tst2}
    assert not services.einvoices(live.clerk).exists()


def test_at29_real_zatca_calls_fail_in_tests(settings):
    settings.ZATCA_API_BASE_URLS = {"sandbox": "https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal"}
    settings.ZATCA_API_PATHS = {"clearance": "/invoices/clearance/single"}
    real = zatca_client.HttpZatcaClient()
    with pytest.raises(zatca_client.RealZatcaCallInTests):
        real.clearance("sandbox", zatca_client.Auth("u", "s"), {"invoice": ""})
    import socket

    with pytest.raises(AssertionError, match="R-29"), socket.socket() as sock:  # any non-loopback address
        sock.connect(("203.0.113.10", 443))
    assert "top-secret-value" not in repr(zatca_client.Auth("user", "top-secret-value"))  # never in reprs or logs


def test_r29_default_client_is_not_the_fake_outside_tests():
    from django.conf import settings

    assert settings.ZATCA_CLIENT == "zatca.client.HttpZatcaClient"
    assert settings.ZATCA_SECRET_STORE == "zatca.keystore.OpenBaoSecretStore"
    from zatca import keystore

    with pytest.raises(keystore.SecretStoreUnavailable):
        keystore.OpenBaoSecretStore().sign("zatca/X/E/private_key#v1", "zatca/X/master_key#v1", b"\x00" * 32)


def test_at73_resubmit_after_a_failed_hand_off(live):
    from django.db import connection as conn

    from invoicing.conftest import invoice
    from invoicing.testing import approval_day
    from types import SimpleNamespace

    a, b = make_user("acc_only"), make_user("acc_admin")
    member(a, live.tst1, "accountant")
    member(b, live.tst1, "accountant")
    member(b, live.tst1, "zatca_admin")
    w = SimpleNamespace(alm=live.tst1, clerk=live.clerk, customer=live.customers["TST1"], cfo=live.cfo)
    docs = []
    for _ in range(2):
        draft = invoice(w, [("management_fee", "1", "100.00")], customer=w.customer, issue_date=TODAY, entity=live.tst1)
        before = len(conn.run_on_commit)
        with approval_day(TODAY):
            doc = invoicing_services.approve_sales_document(draft, user=live.cfo)
        del conn.run_on_commit[before:]  # the after-commit hand-off never ran
        docs.append(SalesDocument.objects.get(pk=doc.pk))
    assert docs[0].zatca_status == "not_submitted"
    with pytest.raises(access.AccessDenied):
        invoicing_services.resubmit_to_zatca(docs[0], user=a)
    with pytest.raises(access.AccessDenied):  # and directly at the port, too
        services.submit_document(docs[0], a)
    for doc, user in ((docs[0], b), (docs[1], live.queue)):
        before = len(conn.run_on_commit)
        invoicing_services.resubmit_to_zatca(doc, user=user)
        for _, callback, _ in conn.run_on_commit[before:]:
            callback()
        del conn.run_on_commit[before:]
        assert einvoice_of(doc).status == "pending"
    run(live)
    assert {einvoice_of(d).status for d in docs} == {"cleared"}


def test_retry_now_queues_a_failed_retryable_document(live):
    live.fakes.client.script["clearance"] = [T.timeout()]
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    with pytest.raises(access.AccessDenied):
        services.retry_now(einv, user=live.clerk)
    einv = services.retry_now(einv, user=live.accountant)
    assert einv.status == "queued" and einv.next_attempt_at is None
    run(live)
    assert einvoice_of(doc).status == "cleared"


def test_r20_no_skipped_transitions(live):
    doc = issue(live, live.tst1)
    einv = einvoice_of(doc)
    with pytest.raises(ZatcaError):
        services._transition(einv, "cleared")
    einv.status = "queued"
    with pytest.raises(ZatcaError):
        services._transition(einv, "cleared")


def test_r32_only_issued_documents_are_handed_over(live):
    from types import SimpleNamespace

    from invoicing.conftest import invoice

    w = SimpleNamespace(alm=live.tst1, clerk=live.clerk, customer=live.customers["TST1"], cfo=live.cfo)
    draft = invoice(w, [("management_fee", "1", "100.00")], customer=w.customer, issue_date=TODAY, entity=live.tst1)
    with pytest.raises(ZatcaError):
        services.submit_document(draft, live.queue)
