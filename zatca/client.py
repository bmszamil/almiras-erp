"""ZATCA Fatoora API client (zatca §6, R-29). The only code that would make HTTP calls to ZATCA.

``ZATCA_CLIENT`` (dotted path) selects the implementation; tests install :class:`zatca.testing.FakeZatcaClient`
with :func:`override`. :class:`HttpZatcaClient` builds every request itself (:meth:`HttpZatcaClient.build_request`)
and sends it in :meth:`HttpZatcaClient._transport`, which refuses to run under pytest, so no test can reach a real
ZATCA host (R-29, AT-29). The fake client subclasses it and replaces only the transport, so the headers and paths
the tests see are the ones a real call would send. Secrets are passed in per call and never logged.

Base URLs and paths (OQ-30): the settings ``ZATCA_API_BASE_URLS`` / ``ZATCA_API_PATHS`` win when they hold a
value; otherwise the defaults below apply. The developer-portal paths and headers were checked against ZATCA's
developer-portal sandbox on 2026-10-19 (``docs/compliance/zatca-sandbox-2026-10-19.md`` §2). The simulation and
production base URLs are ZATCA's published values and have **not** been called by this project
(``BASE_URL_VERIFIED``).
"""
import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from django.conf import settings
from django.utils.module_loading import import_string

API_COMPLIANCE_CSID = "compliance_csid"
API_COMPLIANCE_CHECK = "compliance_check"
API_PRODUCTION_CSID = "production_csid"
API_RENEW_PRODUCTION_CSID = "renew_production_csid"
API_CLEARANCE = "clearance"
API_REPORTING = "reporting"

# Sent on every call. Cloudflare in front of gw-fatoora.zatca.gov.sa bans urllib's default
# "Python-urllib/3.x" (error 1010, HTTP 403; sandbox run 2026-10-19 §2), so the client always names itself.
CLIENT_VERSION = "0.1"
USER_AGENT = f"AlmirasERP/{CLIENT_VERSION}"
ACCEPT_VERSION = "V2"  # required; without it ZATCA answers 406 (sandbox run §2)

DEFAULT_BASE_URLS = {
    "sandbox": "https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal",
    "simulation": "https://gw-fatoora.zatca.gov.sa/e-invoicing/simulation",
    "production": "https://gw-fatoora.zatca.gov.sa/e-invoicing/core",
}
# Which defaults were exercised against ZATCA. Simulation and production are unverified (OQ-26, OQ-30).
BASE_URL_VERIFIED = {"sandbox": "2026-10-19 developer-portal run", "simulation": "", "production": ""}

# api -> (HTTP method, path). Renewal is a PATCH on the production CSID path.
DEFAULT_API_PATHS = {
    API_COMPLIANCE_CSID: "/compliance",
    API_COMPLIANCE_CHECK: "/compliance/invoices",
    API_PRODUCTION_CSID: "/production/csids",
    API_RENEW_PRODUCTION_CSID: "/production/csids",
    API_CLEARANCE: "/invoices/clearance/single",
    API_REPORTING: "/invoices/reporting/single",
}
API_METHODS = {API_RENEW_PRODUCTION_CSID: "PATCH"}  # every other API is POST


@dataclass(frozen=True)
class ApiResponse:
    """``status`` None = network failure or timeout (transient, R-24)."""

    status: int | None
    body: dict = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class Auth:
    """HTTP Basic credentials ``binarySecurityToken:secret`` (DPM p.24-27). ``repr`` hides the secret."""

    username: str
    secret: str = field(repr=False)


class RealZatcaCallInTests(RuntimeError):
    """R-29: a test tried to reach a real ZATCA host."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect. ZATCA's 303 means "clearance disabled" (R-44) and carries no ``Location`` today;
    if one appeared, urllib would re-send the request as a GET elsewhere. Every 3xx is returned as the answer
    (an ``HTTPError`` with its status), so ``services._classify`` sees the 303 itself."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def opener(*handlers):
    """An opener that does not follow redirects; ``handlers`` are extra handlers (tests pass a local one)."""
    return urllib.request.build_opener(NoRedirects, *handlers)


def _configured(name, defaults, key):
    value = ((getattr(settings, name, {}) or {}).get(key) or "").strip()
    return value or defaults.get(key, "")


def send(request, *, timeout, using=None):
    """Send ``request`` with ``using`` (default: :func:`opener`) and map the answer to an :class:`ApiResponse`.
    HTTP errors and 3xx keep their status and body; network failures give ``status`` None."""
    try:
        with (using or opener()).open(request, timeout=timeout) as response:  # noqa: S310 (configured URL)
            return ApiResponse(response.status, _json(response.read()))
    except urllib.error.HTTPError as exc:
        return ApiResponse(exc.code, _json(exc.read()))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ApiResponse(None, {}, type(exc).__name__)


class HttpZatcaClient:
    timeout = 30

    def _url(self, environment, api):
        base = _configured("ZATCA_API_BASE_URLS", DEFAULT_BASE_URLS, environment)
        path = _configured("ZATCA_API_PATHS", DEFAULT_API_PATHS, api)
        if not base or not path:
            raise RuntimeError(f"ZATCA API {api!r} for {environment!r} is not configured (OQ-30).")
        return base.rstrip("/") + "/" + path.lstrip("/")

    def headers(self, api, *, auth=None, otp=None, language="en"):
        headers = {"Accept": "application/json", "Content-Type": "application/json", "Accept-Version": ACCEPT_VERSION,
                   "Accept-Language": language,
                   "User-Agent": getattr(settings, "ZATCA_USER_AGENT", "") or USER_AGENT}
        if otp is not None:
            headers["OTP"] = otp
        if auth is not None:
            token = base64.b64encode(f"{auth.username}:{auth.secret}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        if api == API_CLEARANCE:
            headers["Clearance-Status"] = "1"
        return headers

    def build_request(self, environment, api, body, *, auth=None, otp=None, language="en"):
        return urllib.request.Request(self._url(environment, api), data=json.dumps(body).encode(),
                                      headers=self.headers(api, auth=auth, otp=otp, language=language),
                                      method=API_METHODS.get(api, "POST"))

    def _transport(self, request):
        if os.environ.get("PYTEST_CURRENT_TEST") or getattr(settings, "ZATCA_BLOCK_REAL_CALLS", False):
            raise RealZatcaCallInTests(f"R-29: test code tried to call the real ZATCA API {request.full_url}.")
        return send(request, timeout=self.timeout)

    def _call(self, environment, api, body, *, auth=None, otp=None, language="en"):
        return self._transport(self.build_request(environment, api, body, auth=auth, otp=otp, language=language))

    def compliance_csid(self, environment, csr_b64, otp):
        return self._call(environment, API_COMPLIANCE_CSID, {"csr": csr_b64}, otp=otp)

    def compliance_check(self, environment, auth, body):
        return self._call(environment, API_COMPLIANCE_CHECK, body, auth=auth)

    def production_csid(self, environment, auth, compliance_request_id):
        return self._call(environment, API_PRODUCTION_CSID, {"compliance_request_id": compliance_request_id},
                          auth=auth)

    def renew_production_csid(self, environment, auth, csr_b64, otp):
        return self._call(environment, API_RENEW_PRODUCTION_CSID, {"csr": csr_b64}, auth=auth, otp=otp)

    def clearance(self, environment, auth, body, language="en"):
        return self._call(environment, API_CLEARANCE, body, auth=auth, language=language)

    def reporting(self, environment, auth, body, language="en"):
        return self._call(environment, API_REPORTING, body, auth=auth, language=language)


def _json(raw):
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {"raw": raw.decode("utf-8", errors="replace")[:2000]}
    return value if isinstance(value, dict) else {"value": value}


_override = None


def get_client():
    if _override is not None:
        return _override
    return import_string(getattr(settings, "ZATCA_CLIENT", "zatca.client.HttpZatcaClient"))()


def override(client):
    global _override
    previous, _override = _override, client
    return previous
