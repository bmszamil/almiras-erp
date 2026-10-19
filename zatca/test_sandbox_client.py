"""ZATCA developer-portal sandbox run of 2026-10-19 (docs/compliance/zatca-sandbox-2026-10-19.md §7 items 1 to 3
and 8), as built: explicit User-Agent, no redirects, API paths and base URLs (OQ-30) and the sandbox-shaped fake.

No test reaches a network: the real client's requests are answered by a local urllib handler or by the fake.
"""
import http.client
import io
import urllib.request
import urllib.response
from dataclasses import replace

import pytest

from zatca import client as zatca_client
from zatca import services
from zatca.conftest import einvoice_of, issue, mutate, onboard, run
from zatca.messages import ZATCA_CODES_AR
from zatca.models import EgsCredential, EInvoice, OnboardingEvent
from zatca.testing import zatca_warning

pytestmark = pytest.mark.django_db


# --- a local urllib handler: answers https requests without any socket ---------------------------------------------

class _Local(urllib.request.BaseHandler):
    handler_order = 100  # before the default HTTPSHandler

    def __init__(self, status, body=b"{}", location=None):
        self.status, self.body, self.location, self.seen = status, body, location, []

    def https_open(self, req):
        self.seen.append(req)
        headers = http.client.HTTPMessage()
        if self.location:
            headers["Location"] = self.location
        status = self.status if len(self.seen) == 1 else 200  # a followed redirect would get a 200
        response = urllib.response.addinfourl(io.BytesIO(self.body), headers, req.full_url, status)
        response.msg = "See Other" if status == 303 else "OK"
        return response


def _request(api=zatca_client.API_CLEARANCE, environment="sandbox", **kw):
    return zatca_client.HttpZatcaClient().build_request(environment, api, {"invoice": ""},
                                                        auth=zatca_client.Auth("token", "secret"), **kw)


# --- §7 item 1: explicit User-Agent ---------------------------------------------------------------------------------

def test_user_agent_is_set_on_every_api():
    for api in zatca_client.DEFAULT_API_PATHS:
        request = _request(api)
        assert request.get_header("User-agent") == "AlmirasERP/" + zatca_client.CLIENT_VERSION
        assert request.get_header("Accept-version") == "V2"
        assert request.get_header("Accept") == "application/json"
        assert request.get_header("Content-type") == "application/json"


def test_user_agent_is_what_goes_on_the_wire_not_python_urllib():
    local = _Local(200, b'{"reportingStatus": "REPORTED"}')
    response = zatca_client.send(_request(zatca_client.API_REPORTING), timeout=1, using=zatca_client.opener(local))
    assert response.status == 200 and response.body == {"reportingStatus": "REPORTED"}
    sent = local.seen[0]
    assert sent.get_header("User-agent") == "AlmirasERP/0.1"
    assert not any(v.startswith("Python-urllib") for _, v in sent.header_items())


def test_user_agent_setting_overrides_the_default(settings):
    settings.ZATCA_USER_AGENT = "AlmirasERP/9.9 (staging)"
    assert _request().get_header("User-agent") == "AlmirasERP/9.9 (staging)"


# --- §7 item 2: redirects are never followed; a 303 reaches _classify ----------------------------------------------

def test_303_is_returned_not_followed_even_with_a_location():
    local = _Local(303, b'{"message": "Clearance is deactiviated."}', location="https://elsewhere.invalid/x")
    response = zatca_client.send(_request(), timeout=1, using=zatca_client.opener(local))
    assert response.status == 303 and response.body == {"message": "Clearance is deactiviated."}
    assert len(local.seen) == 1 and local.seen[0].get_method() == "POST"
    assert services._classify(EInvoice.Api.CLEARANCE, response) == "clearance_disabled"


def test_control_urllibs_default_opener_would_follow_it_as_a_get():
    local = _Local(303, b"{}", location="https://elsewhere.invalid/x")
    with urllib.request.build_opener(local).open(_request(), timeout=1) as followed:
        assert followed.status == 200
    assert len(local.seen) == 2 and local.seen[1].get_method() == "GET"  # why the client must not follow


def test_303_without_location_and_plain_text_errors_are_kept():
    response = zatca_client.send(_request(), timeout=1, using=zatca_client.opener(_Local(303, b'{"message": "x"}')))
    assert response.status == 303
    response = zatca_client.send(_request(), timeout=1, using=zatca_client.opener(_Local(400, b"Invalid Request")))
    assert response.status == 400 and response.body == {"raw": "Invalid Request"}


# --- §7 item 3: paths, methods and base URLs (OQ-30) ----------------------------------------------------------------

def test_default_paths_and_methods_match_the_developer_portal():
    client = zatca_client.HttpZatcaClient()
    base = "https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal"
    expected = {"compliance_csid": ("POST", "/compliance"), "compliance_check": ("POST", "/compliance/invoices"),
                "production_csid": ("POST", "/production/csids"),
                "renew_production_csid": ("PATCH", "/production/csids"),
                "clearance": ("POST", "/invoices/clearance/single"),
                "reporting": ("POST", "/invoices/reporting/single")}
    for api, (method, path) in expected.items():
        request = client.build_request("sandbox", api, {})
        assert (request.get_method(), request.full_url) == (method, base + path)
    assert _request(zatca_client.API_CLEARANCE).get_header("Clearance-status") == "1"
    assert _request(zatca_client.API_REPORTING).get_header("Clearance-status") is None


def test_base_urls_and_which_are_verified():
    assert zatca_client.DEFAULT_BASE_URLS == {
        "sandbox": "https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal",
        "simulation": "https://gw-fatoora.zatca.gov.sa/e-invoicing/simulation",
        "production": "https://gw-fatoora.zatca.gov.sa/e-invoicing/core"}
    assert zatca_client.BASE_URL_VERIFIED["sandbox"] and not zatca_client.BASE_URL_VERIFIED["simulation"]
    assert not zatca_client.BASE_URL_VERIFIED["production"]


def test_configured_values_win_and_empty_ones_fall_back(settings):
    settings.ZATCA_API_BASE_URLS = {"sandbox": "", "simulation": "https://sim.example.invalid/x/"}
    settings.ZATCA_API_PATHS = {"clearance": "custom/clear", "reporting": ""}
    client = zatca_client.HttpZatcaClient()
    assert client._url("simulation", "clearance") == "https://sim.example.invalid/x/custom/clear"
    assert client._url("sandbox", "reporting") == \
        "https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal/invoices/reporting/single"
    with pytest.raises(RuntimeError, match="OQ-30"):
        client._url("elsewhere", "clearance")


def test_real_client_still_refuses_under_pytest(settings):
    with pytest.raises(zatca_client.RealZatcaCallInTests, match="R-29"):
        zatca_client.HttpZatcaClient().reporting("sandbox", zatca_client.Auth("u", "s"), {"invoice": ""})


# --- §7 item 8: the fake answers like the developer portal ----------------------------------------------------------

def _raw(fake, api, *, auth=None, otp=None, drop=(), body=None, environment="sandbox"):
    request = fake.build_request(environment, api, body if body is not None else {}, auth=auth, otp=otp)
    for header in drop:
        request.remove_header(header)
    return fake._transport(request)


def test_fake_refuses_python_urllib_and_a_missing_accept_version(fakes):
    fake = fakes.client
    request = fake.build_request("sandbox", "compliance_csid", {"csr": ""}, otp="123345")
    request.add_header("User-Agent", "Python-urllib/3.13")
    assert fake._transport(request).status == 403
    response = _raw(fake, "compliance_csid", otp="123345", drop=("Accept-version",))
    assert response.status == 406 and "Version" in response.body["raw"]


def test_fake_401_for_wrong_or_unknown_csid_and_missing_otp(live_ready):
    fake, compliance, production = live_ready
    body = {"invoiceHash": "x", "uuid": "u", "invoice": ""}
    assert _raw(fake, "clearance", auth=compliance, body=body).status == 401
    assert _raw(fake, "reporting", auth=compliance, body=body).status == 401
    assert _raw(fake, "compliance_check", auth=production, body=body).status == 401
    assert _raw(fake, "production_csid", auth=production, body={"compliance_request_id": "1"}).status == 401
    assert _raw(fake, "clearance", auth=zatca_client.Auth("unknown", "s"), body=body).status == 401
    assert _raw(fake, "clearance", auth=replace(production, secret="wrong"), body=body).status == 401
    response = _raw(fake, "renew_production_csid", auth=production, body={"csr": ""})
    assert response.status == 400 and response.body["errors"][0]["code"] == "Missing-OTP"
    response = _raw(fake, "production_csid", auth=compliance, body={"compliance_request_id": "123"})
    assert response.status == 400 and response.body["code"] == "Incorrect-ComplianceRequestId"


@pytest.fixture
def live_ready(zw):
    egs = onboard(zw, zw.tst1, zw.admin1)
    master = services._master_ref(zw.tst1)
    production = services._auth(services._active_credential(egs, "production"), master)
    old = EgsCredential.objects.get(egs_unit=egs, kind="compliance")
    compliance = services._auth(old, master)
    zw.egs1 = egs
    return zw.fakes.client, compliance, production


@pytest.fixture
def live(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    return zw


def test_fake_clearance_disabled_gives_303_and_the_document_goes_to_reporting(live):
    live.fakes.client.clearance_disabled = True
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    assert [(a.api, a.http_status, a.outcome) for a in einv.attempts.order_by("attempt_no")] == [
        ("clearance", 303, "clearance_disabled"), ("reporting", 200, "accepted")]
    assert einv.status == "reported_clearance_disabled" and not services.is_issued(doc)
    assert "deactiviated" in einv.attempts.get(attempt_no=1).response_json["message"]
    clearance = next(r for r in live.fakes.client.requests if r[0] == "clearance")
    assert clearance[4]["user-agent"] == "AlmirasERP/0.1" and clearance[4]["clearance-status"] == "1"
    assert clearance[4]["authorization"] == "<redacted>"  # the fake keeps no credentials


def test_fake_certificate_permissions_when_seller_vat_is_not_the_csids(live, monkeypatch):
    mutate(monkeypatch, **{"seller.vat_number": "312345678900003"})
    doc = issue(live, live.tst1)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "rejected" and "certificate-permissions" in set(einv.messages.values_list("code", flat=True))
    message = einv.messages.get(code="certificate-permissions")
    assert message.message_ar == ZATCA_CODES_AR["certificate-permissions"]


def test_fake_warns_202_on_a_qr_tag_3_with_z_and_reporting_accepts_it(live, monkeypatch):
    from zatca import qr

    original = qr.simplified_values
    monkeypatch.setattr(qr, "simplified_values", lambda **kw: original(**{**kw, "timestamp": kw["timestamp"] + "Z"}))
    doc = issue(live, live.tst1, simplified=True)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "reported_with_warnings"
    warning = einv.messages.get(code="invoiceTimeStamp_QRCODE_INVALID")
    assert warning.level == "warning" and warning.message_ar


def test_compliance_check_202_with_warnings_still_passes(zw):
    zw.fakes.client.compliance_warnings[("standard", "381")] = [zatca_warning("BR-KSA-W", "note")]
    egs = onboard(zw, zw.tst1, zw.admin1)
    assert egs.status == "active"
    event = OnboardingEvent.objects.get(entity=zw.tst1, action="compliance_checks_passed")
    assert event.detail["results"]["standard-381"] == {"http_status": 202, "passed": True, "errors": []}
