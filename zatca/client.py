"""ZATCA Fatoora API client (zatca §6, R-29). The only code that would make HTTP calls to ZATCA.

``ZATCA_CLIENT`` (dotted path) selects the implementation; tests install :class:`zatca.testing.FakeZatcaClient`
with :func:`override`. :class:`HttpZatcaClient` takes its base URLs and paths from configuration only
(``ZATCA_API_BASE_URLS``, ``ZATCA_API_PATHS``; placeholders, OQ-30) and refuses to run under pytest, so no test
can reach a real ZATCA host (R-29, AT-29). Secrets are passed in per call and never logged.
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


class HttpZatcaClient:
    timeout = 30

    def _url(self, environment, api):
        base = (getattr(settings, "ZATCA_API_BASE_URLS", {}) or {}).get(environment, "")
        path = (getattr(settings, "ZATCA_API_PATHS", {}) or {}).get(api, "")
        if not base or not path:
            raise RuntimeError(f"ZATCA API {api!r} for {environment!r} is not configured (OQ-30).")
        return base.rstrip("/") + "/" + path.lstrip("/")

    def _call(self, environment, api, body, *, auth=None, otp=None, language="en"):
        if os.environ.get("PYTEST_CURRENT_TEST") or getattr(settings, "ZATCA_BLOCK_REAL_CALLS", False):
            raise RealZatcaCallInTests(f"R-29: test code tried to call the real ZATCA {api} API.")
        url = self._url(environment, api)
        headers = {"Accept": "application/json", "Content-Type": "application/json", "Accept-Version": "V2",
                   "Accept-Language": language}
        if otp is not None:
            headers["OTP"] = otp
        if auth is not None:
            token = base64.b64encode(f"{auth.username}:{auth.secret}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        if api == API_CLEARANCE:
            headers["Clearance-Status"] = "1"
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        if api == API_RENEW_PRODUCTION_CSID:
            request.method = "PATCH"
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 (configured URL)
                return ApiResponse(response.status, _json(response.read()))
        except urllib.error.HTTPError as exc:
            return ApiResponse(exc.code, _json(exc.read()))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return ApiResponse(None, {}, type(exc).__name__)

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
