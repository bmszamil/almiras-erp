"""Review of 838e89b, findings 2 and 5: crashes while submitting (probe p3), the stuck-submission sweep,
unclear resubmission answers, and HTTP 200 without a usable cleared XML (probe p2)."""
import base64
from datetime import timedelta

import pytest

from core import access
from zatca import crypto, services
from zatca.client import ApiResponse
from zatca.conftest import ADMINS1, NOW, alerts, einvoice_of, held_codes, issue, no_entry, onboard, run, xml_text
from zatca.testing import FakeZatcaClient

pytestmark = pytest.mark.django_db
T = FakeZatcaClient


@pytest.fixture
def live(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    return zw


# --- finding 2 (probe p3): a crash while submitting -------------------------------------------------------

def test_f2_client_crash_is_a_transient_attempt_and_other_entities_continue(zw):
    onboard(zw, zw.tst1, zw.admin1)
    onboard(zw, zw.tst2, zw.admin2)

    def boom(body):
        raise RuntimeError("ZATCA API 'clearance' for 'sandbox' is not configured (OQ-30).")

    zw.fakes.client.script["clearance"] = [boom]
    d1, d2 = issue(zw, zw.tst1), issue(zw, zw.tst2)
    counts = run(zw)  # no exception escapes the run
    e1, e2 = einvoice_of(d1), einvoice_of(d2)
    assert e1.status == "failed_retryable" and e2.status == "cleared"
    attempt = e1.attempts.get()
    assert attempt.outcome == "transient_error" and attempt.http_status is None
    assert attempt.response_json == {"error": "client_error: RuntimeError"}
    assert alerts("submission_error", e1) == ADMINS1
    assert counts["sent"] == 2
    run(zw, now=NOW + timedelta(hours=1))
    e1.refresh_from_db()
    assert e1.status == "cleared" and e1.attempts.count() == 2 and alerts("submission_error", e1) == ADMINS1


def test_f2_unreadable_response_is_transient(live):
    live.fakes.client.script["clearance"] = [ApiResponse("200", {"validationResults": "?"})]
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "failed_retryable"
    assert einv.attempts.get().response_json["error"] == "response_error: TypeError"


def test_f2_unexpected_error_in_one_document_does_not_stop_the_run(live, monkeypatch):
    first, second = issue(live, live.tst1), issue(live, live.tst1)
    original = services._invoice_data

    def broken(einv):
        if einv.source_document_id == first.pk:
            raise KeyError("unexpected")
        return original(einv)

    monkeypatch.setattr(services, "_invoice_data", broken)
    counts = run(live)
    assert counts["errors"] == 1
    assert einvoice_of(first).status == "pending" and einvoice_of(second).status == "cleared"


def test_f2_stuck_submitting_is_swept_to_failed_retryable(live, settings):
    settings.ZATCA_SUBMITTING_STALE_MINUTES = 15

    def killed(body):
        raise SystemExit("worker killed mid-call")

    live.fakes.client.script["clearance"] = [killed]
    doc = issue(live, live.tst1)
    with pytest.raises(SystemExit):
        run(live)
    einv = einvoice_of(doc)
    assert einv.status == "submitting" and einv.submitting_since == NOW and not einv.attempts.exists()
    run(live, now=NOW + timedelta(minutes=10))  # not stale yet
    einv.refresh_from_db()
    assert einv.status == "submitting"
    run(live, now=NOW + timedelta(minutes=16))
    einv.refresh_from_db()
    assert einv.status == "failed_retryable"
    attempt = einv.attempts.get()
    assert attempt.outcome == "transient_error" and attempt.response_json == {"error": "stuck_in_submitting"}
    assert attempt.started_at == NOW
    assert alerts("stuck_submitting", einv) == ADMINS1
    run(live, now=NOW + timedelta(minutes=30))
    einv.refresh_from_db()
    assert einv.status == "cleared" and einv.attempts.count() == 2


def test_f2_sweep_job_is_for_the_queue_user_only(live):
    assert services.sweep_stuck_submissions(user=live.queue, now=NOW) == []
    with pytest.raises(access.AccessDenied):
        services.sweep_stuck_submissions(user=live.admin1, now=NOW)


def test_f2_unclear_answer_to_a_resubmission_is_retried_and_alerted(live):
    live.fakes.client.script["clearance"] = [T.timeout(), ApiResponse(409, {"message": "Invoice was already sent"})]
    doc = issue(live, live.tst1)
    run(live)
    run(live, now=NOW + timedelta(hours=1))
    einv = einvoice_of(doc)
    assert einv.status == "failed_retryable"  # never failed_final without a clear duplicate code (TODO OQ-30)
    assert [a.outcome for a in einv.attempts.order_by("attempt_no")] == ["transient_error", "transient_error"]
    assert einv.attempts.order_by("attempt_no").last().http_status == 409
    assert alerts("resubmission_unclear", einv) == ADMINS1
    run(live, now=NOW + timedelta(hours=2))
    einv.refresh_from_db()
    assert einv.status == "cleared"


def test_f2_unclear_answer_to_a_first_submission_stays_failed_final(live):
    live.fakes.client.script["clearance"] = [ApiResponse(409, {"message": "?"})]
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "failed_final" and einv.attempts.get().outcome == "permanent_error"
    assert alerts("failed_final", einv) == ADMINS1


# --- finding 5 (probe p2): HTTP 200 without a usable cleared XML -------------------------------------------------

def _tampered(change):
    def respond(body):
        good = T.cleared(body)
        xml = base64.b64decode(good.body["clearedInvoice"]).decode()
        return ApiResponse(200, {**good.body, "clearedInvoice": base64.b64encode(change(xml).encode()).decode()})

    return respond


@pytest.mark.parametrize("response, code", [
    (ApiResponse(200, {"validationResults": {"status": "PASS"}, "clearanceStatus": "CLEARED"}), "cleared_xml_missing"),
    (ApiResponse(200, {"clearanceStatus": "CLEARED", "clearedInvoice": "%%% not base64"}), "cleared_xml_missing"),
    (ApiResponse(200, {"clearanceStatus": "CLEARED", "clearedInvoice": base64.b64encode(b"not xml").decode()}),
     "cleared_xml_missing"),
    (_tampered(lambda x: x.replace("<cbc:UUID>", "<cbc:UUID>0", 1)), "cleared_xml_uuid"),
    (_tampered(lambda x: x.replace("reporting:1.0", "reporting:1.1", 1)), "cleared_xml_hash"),
])
def test_f5_clearance_without_usable_cleared_xml_is_not_issued(live, response, code):
    live.fakes.client.script["clearance"] = [response]
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    doc.refresh_from_db()
    assert einv.status == "failed_final" and einv.cleared_xml == ""
    assert einv.attempts.get().outcome == "permanent_error"
    assert code in held_codes(einv) and einv.messages.get(code=code).message_ar
    assert not services.is_issued(doc) and no_entry(doc) and doc.status != "final"
    assert alerts("failed_final", einv) == ADMINS1


def test_f5_good_cleared_xml_still_clears(live):
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "cleared" and xml_text(einv.cleared_xml, "/*/cbc:UUID") == str(einv.uuid)
    assert crypto.invoice_hash(einv.cleared_xml.encode()) == einv.invoice_hash
