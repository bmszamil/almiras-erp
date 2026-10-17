"""Review of 838e89b, findings 3 and 7: the EGS unit is chosen at stamping time (probe p9), holds alert,
the production switch retires old units, renewal waits for documents in flight (probe p8)."""
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone

import pytest

from core import access
from zatca import services
from zatca.conftest import ADMINS1, NOW, alerts, einvoice_of, issue, onboard, run, second_unit
from zatca.messages import ZatcaError
from zatca.models import EgsCredential, EInvoice, OnboardingEvent, ZatcaProfile
from zatca.testing import FakeZatcaClient

pytestmark = pytest.mark.django_db
T = FakeZatcaClient


@pytest.fixture
def live(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    return zw


# --- finding 3 (probe p9): the EGS unit is chosen at stamping time ---------------------------------------------

def test_f3_pending_document_is_signed_by_the_replacement_unit(live):
    live.fakes.store.sealed = True
    doc = issue(live, live.tst1)
    run(live)
    live.fakes.store.sealed = False
    einv = einvoice_of(doc)
    assert einv.status == "pending" and einv.egs_unit_id is None  # no unit fixed before the ICV
    services.revoke_egs_unit(live.egs1, user=live.admin1, reason="replaced")
    egs2 = second_unit(live, live.tst1, live.admin1, "EGS2")
    run(live, now=NOW + timedelta(hours=1))
    einv.refresh_from_db()
    assert einv.status == "cleared" and einv.egs_unit == egs2 and einv.icv == 1


def test_f3_unit_without_a_valid_credential_is_skipped(zw):
    zw.fakes.client.cert_valid_days = 300  # EGS1's certificates expire on 2026-10-28
    egs1 = onboard(zw, zw.tst1, zw.admin1)
    zw.fakes.client.cert_valid_days = 3 * 365
    egs2 = second_unit(zw, zw.tst1, zw.admin1, "EGS2")
    later = datetime(2026, 11, 1, 9, 0, tzinfo=dt_timezone.utc)
    doc = issue(zw, zw.tst1)
    run(zw, now=later)
    einv = einvoice_of(doc)
    assert einv.status == "cleared" and einv.egs_unit == egs2
    egs1.refresh_from_db()
    assert egs1.last_icv == 0


def test_f3_egs_not_ready_hold_alerts_the_zatca_admins_once(live):
    credential = EgsCredential.objects.get(egs_unit=live.egs1, kind="production", status="active")
    doc = issue(live, live.tst1)
    after = credential.valid_to + timedelta(minutes=1)
    run(live, now=after)
    run(live, now=after + timedelta(minutes=5))
    einv = einvoice_of(doc)
    assert einv.hold_reason == "egs_not_ready" and einv.icv is None
    assert (alerts("egs_not_ready", einv)) == ADMINS1


def test_f3_no_egs_hold_alerts_the_zatca_admins(zw):
    from zatca.conftest import keys

    keys(zw, zw.tst1, zw.admin1)
    profile = services.create_profile(zw.tst1, user=zw.admin1, go_live_date=date(2026, 1, 1))
    services.enable_profile(profile, user=zw.admin1)
    doc = issue(zw, zw.tst1)
    run(zw)
    einv = einvoice_of(doc)
    assert einv.hold_reason == "no_egs" and (alerts("no_egs", einv)) == ADMINS1


def test_f3_production_switch_drains_first_and_retires_sandbox_units(live, settings):
    settings.ZATCA_PRODUCTION_ALLOWED = True
    profile = ZatcaProfile.objects.get(entity=live.tst1)
    live.fakes.client.script["clearance"] = [T.timeout()]
    doc = issue(live, live.tst1)
    run(live)
    assert einvoice_of(doc).status == "failed_retryable"
    req = services.request_production_switch(profile, user=live.admin1)
    access.decide_dual_control(live.admin1b, req, True)
    with pytest.raises(ZatcaError) as exc:
        services.switch_to_production(profile, user=live.admin1, request=req)
    assert exc.value.code == "documents_in_flight" and exc.value.ar
    run(live, now=NOW + timedelta(hours=1))
    assert einvoice_of(doc).status == "cleared"
    profile = services.switch_to_production(profile, user=live.admin1, request=req)
    assert profile.environment == "production"
    live.egs1.refresh_from_db()
    assert live.egs1.status == "revoked"
    assert not EgsCredential.objects.filter(egs_unit=live.egs1, status="active").exists()
    assert (alerts("environment_changed", entity=live.tst1)) == ADMINS1
    later = issue(live, live.tst1)
    run(live, now=NOW + timedelta(hours=2))
    assert einvoice_of(later).hold_reason == "no_egs"  # onboard production units first (R-36)


# --- finding 7 (probe p8): renewal waits for documents in flight ---------------------------------------------

def test_f7_renewal_refused_while_documents_are_with_zatca(live):
    live.fakes.client.script["clearance"] = [T.timeout()]
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "failed_retryable"
    writes = live.fakes.store.calls.count("write_private_key")
    with pytest.raises(ZatcaError) as exc:
        services.renew_production_csid(live.egs1, "654321", user=live.admin1)
    assert exc.value.code == "documents_in_flight" and doc.number in exc.value.en and doc.number in exc.value.ar
    assert live.fakes.store.calls.count("write_private_key") == writes  # no key generated
    assert not [c for c in live.fakes.client.calls if c[0] == "renew_production_csid"]  # OTP not used
    run(live, now=NOW + timedelta(hours=1))
    assert einvoice_of(doc).status == "cleared"
    egs = services.renew_production_csid(live.egs1, "654321", user=live.admin1)  # drained: renewal goes ahead
    assert OnboardingEvent.objects.filter(egs_unit=egs, action="pcsid_renewed").exists()


@pytest.mark.parametrize("status", ["queued", "submitting"])
def test_f7_each_in_flight_status_blocks_renewal(live, status):
    from django.db import connection

    einv = einvoice_of(issue(live, live.tst1))
    services.stamp_document(einv, user=live.queue, now=NOW)
    assert EInvoice.objects.get(pk=einv.pk).status == "queued"
    if status == "submitting":  # a worker mid-call (queued -> submitting is allowed by R-20 and the trigger)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE zatca_einvoice SET status = 'submitting' WHERE id = %s", [einv.pk])
    with pytest.raises(ZatcaError) as exc:
        services.renew_production_csid(live.egs1, "654321", user=live.admin1)
    assert exc.value.code == "documents_in_flight"
