"""Test helpers for zatca: a fake ZATCA API with a fake CA, and a local secret store. Never used outside tests.

Nothing here calls a network service; every identifier, key and certificate is made up at run time.
"""
import base64
import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID

from . import client as zatca_client
from . import crypto, keystore, qr, ubl
from .client import ApiResponse


class FakeCA:
    """Issues certificates for CSRs, signed by a throwaway secp256k1 key (like ZATCA's test sub-CA)."""

    def __init__(self, valid_from=None, valid_days=365 * 3):
        self.key = crypto.generate_private_key()
        self.name = x509.Name([x509.NameAttribute(NameOID.DOMAIN_COMPONENT, "local"),
                               x509.NameAttribute(NameOID.DOMAIN_COMPONENT, "test"),
                               x509.NameAttribute(NameOID.COMMON_NAME, "FAKE-TEST-ZATCA-SubCA")])
        self.valid_from = valid_from
        self.valid_days = valid_days

    def issue(self, csr, valid_from=None, valid_days=None):
        start = valid_from or self.valid_from or datetime.now(timezone.utc) - timedelta(days=1)
        builder = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self.name)
                   .public_key(csr.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(start).not_valid_after(start + timedelta(days=valid_days or self.valid_days)))
        for ext in csr.extensions:  # ZATCA's certificates carry the CSR's SAN (SN, UID = VAT, title, ...)
            if isinstance(ext.value, x509.SubjectAlternativeName):
                builder = builder.add_extension(ext.value, ext.critical)
        return builder.sign(self.key, hashes.SHA256())


def _validation(errors=(), warnings=()):
    status = "ERROR" if errors else ("WARNING" if warnings else "PASS")
    return {"status": status, "infoMessages": [], "errorMessages": list(errors), "warningMessages": list(warnings)}


def zatca_error(code, message, category="KSA"):
    return {"type": "ERROR", "code": code, "category": category, "message": message, "status": "ERROR"}


def zatca_warning(code, message, category="KSA"):
    return {"type": "WARNING", "code": code, "category": category, "message": message, "status": "WARNING"}


class FakeZatcaClient(zatca_client.HttpZatcaClient):
    """Sandbox-shaped fake of the ZATCA APIs (developer-portal run 2026-10-19, §2 to §5).

    It subclasses :class:`zatca.client.HttpZatcaClient` and replaces only the transport: every call is built by the
    real client (URL, method, headers, JSON body) and answered here as the developer portal answers it:

    - no or the urllib default ``User-Agent``: 403 (Cloudflare error 1010); no ``Accept-Version: V2``: 406, plain text;
    - Basic auth with the wrong CSID kind or an unknown token: 401 (compliance CSID for ``compliance_check`` and
      ``production_csid``; production CSID for renewal, clearance and reporting);
    - no ``OTP`` header on ``compliance_csid`` / renewal: 400 ``Missing-OTP``; a request id that is not 13 digits:
      400 ``Incorrect-ComplianceRequestId``;
    - ``Clearance-Status: 0`` or ``clearance_disabled``: 303 with ZATCA's message and no ``Location``;
    - a body ``invoiceHash`` that is not the XML's hash: 400 ``invalid-invoice-hash``; buyer VAT = seller VAT: 400
      ``BR-CUSTOM-VALIDATION-01``; on clearance / reporting a seller VAT other than the auth certificate's: 400
      ``certificate-permissions``; on simplified documents QR tags checked against the XML (tag 3 only warned,
      202 ``invoiceTimeStamp_QRCODE_INVALID``);
    - ``developer_portal_production_certificate``: the production CSID is one fixed certificate for another key,
      whatever the CSR (the developer portal's static dummy certificate).

    ``script[api]`` holds responses to return after the header and auth checks (FIFO, each an ``ApiResponse`` or a
    callable(body) -> ApiResponse); afterwards the default behaviour applies."""

    def __init__(self, ca=None):
        self.ca = ca or FakeCA()
        self.calls = []  # (api, environment, body)
        self.requests = []  # (api, environment, method, url, headers) as built by the real client
        self.script = {}
        self.rejected_otps = set()
        self.fail_compliance_types = set()  # e.g. {("standard", "383")}
        self.compliance_warnings = {}  # (category, type_code) -> [zatca_warning(...)], answered 202
        self.issued_secrets = []
        self.issued_tokens = []
        self._tokens = {}  # token -> (kind, secret)
        self._csr_by_request = {}
        self.cert_valid_days = None
        self.cert_valid_from = None
        self.clearance_disabled = False
        self.developer_portal_production_certificate = False
        self._static_cert = None

    # -- transport: route the real client's request --
    def _route(self, request):
        for environment in ("sandbox", "simulation", "production"):
            for api in zatca_client.DEFAULT_API_PATHS:
                if (self._url(environment, api) == request.full_url
                        and zatca_client.API_METHODS.get(api, "POST") == request.get_method()):
                    return environment, api
        raise AssertionError(f"FakeZatcaClient: no ZATCA API at {request.get_method()} {request.full_url}")

    def _transport(self, request):
        environment, api = self._route(request)
        headers = {k.lower(): v for k, v in request.header_items()}
        body = json.loads(request.data or b"{}")
        self.requests.append((api, environment, request.get_method(), request.full_url,
                              {k: ("<redacted>" if k in ("authorization", "otp") else v) for k, v in headers.items()}))
        self.calls.append((api, environment, body))
        agent = headers.get("user-agent", "")
        if not agent or agent.lower().startswith("python-urllib"):
            return ApiResponse(403, {"raw": "error code: 1010"})
        if headers.get("accept-version") != "V2":
            return ApiResponse(406, {"raw": "This Version is not supported or not provided in the header."})
        return getattr(self, "_" + api)(environment, headers, body)

    def _auth_kind(self, headers):
        value = headers.get("authorization", "")
        if not value.startswith("Basic "):
            return None
        try:
            username, _, secret = base64.b64decode(value[6:]).decode().partition(":")
        except ValueError:
            return None
        kind, issued_secret = self._tokens.get(username, (None, None))
        return kind if secret == issued_secret else None

    @staticmethod
    def _unauthorised():
        return ApiResponse(401, {})

    @staticmethod
    def _missing_otp():
        return ApiResponse(400, {"errors": [{"code": "Missing-OTP", "message": "OTP is required"}]})

    # -- helpers --
    def _scripted(self, api, body):
        queue = self.script.get(api) or []
        if queue:
            item = queue.pop(0)
            return item(body) if callable(item) else item
        return None

    def _issue(self, csr, kind):
        if kind == "production" and self.developer_portal_production_certificate:
            if self._static_cert is None:  # one certificate for another key, whatever the CSR (sandbox run §3)
                other = crypto.generate_private_key()
                subject = x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "SA"),
                                     x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Fake Static Dummy Co"),
                                     x509.NameAttribute(NameOID.COMMON_NAME, "TST-000000000-300000000000003")])
                builder = x509.CertificateSigningRequestBuilder().subject_name(subject)
                self._static_cert = self.ca.issue(builder.sign(other, hashes.SHA256()), self.cert_valid_from,
                                                  self.cert_valid_days)
            cert = self._static_cert
        else:
            cert = self.ca.issue(csr, self.cert_valid_from, self.cert_valid_days)
        token = base64.b64encode(crypto.cert_base64(cert).encode()).decode()
        secret = "fake-secret-" + secrets.token_hex(8)
        request_id = str(7_100_000_000_000 + len(self.issued_tokens))  # 13 digits, as ZATCA's
        self.issued_secrets.append(secret)
        self.issued_tokens.append(token)
        self._tokens[token] = (kind, secret)
        self._csr_by_request[request_id] = csr
        return ApiResponse(200, {"requestID": request_id, "dispositionMessage": "ISSUED",
                                 "binarySecurityToken": token, "secret": secret, "errors": None})

    @staticmethod
    def _csr(csr_b64):
        return x509.load_pem_x509_csr(base64.b64decode(csr_b64))

    @staticmethod
    def _token_vat(headers):
        """The VAT number of the auth certificate (SAN UID, else the CN's last part)."""
        username = base64.b64decode(headers["authorization"][6:]).decode().partition(":")[0]
        cert = crypto.load_cert_base64(crypto.cert_base64_from_token(username))
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            for name in san.get_values_for_type(x509.DirectoryName):
                for attr in name:
                    if attr.oid == NameOID.USER_ID:
                        return attr.value
        except x509.ExtensionNotFound:
            pass
        common_name = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return common_name[0].value.rsplit("-", 1)[-1] if common_name else ""

    @staticmethod
    def document_findings(body, *, auth_vat=None):
        """(errors, warnings) the developer portal gives a document body (sandbox run §4, §5)."""
        xml = base64.b64decode(body["invoice"])
        g = lambda xp: ubl.find_text(xml, xp)  # noqa: E731
        errors, warnings = [], []
        if crypto.invoice_hash(xml) != body.get("invoiceHash"):
            errors.append(zatca_error("invalid-invoice-hash", "The invoice hash API body does not match the "
                                      "(calculated) Hash of the XML", "INVOICE_HASHING_ERRORS"))
        seller_vat = g("/*/cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID")
        buyer_vat = g("/*/cac:AccountingCustomerParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID")
        if buyer_vat and buyer_vat == seller_vat:
            errors.append(zatca_error("BR-CUSTOM-VALIDATION-01", "The Seller VAT registration number or seller group "
                                      "VAT registration number (BT-31) of The invoice must not match the buyer VAT "
                                      "registration number (BT-48)"))
        if auth_vat is not None and seller_vat != auth_vat:
            errors.append(zatca_error("certificate-permissions", "User only allowed to use the vat number that "
                                      "exists in the authentication certificate", "CERTIFICATE_ERRORS"))
        if g("/*/cbc:InvoiceTypeCode/@name").startswith("02"):
            try:
                tags = qr.decode(ubl.qr_from_xml(xml))
            except qr.QrInvalid:
                tags = {}
            if any(not tags.get(n) for n in range(1, 10)):
                errors.append(zatca_error("QRCODE_INVALID", "QR code is invalid", "QRCODE_ERRORS"))
            else:
                checks = (
                    (1, g("/*/cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName"),
                     "sellerName_QRCODE_INVALID"),
                    (4, g("/*/cac:LegalMonetaryTotal/cbc:PayableAmount"), "invoiceTotal_QRCODE_INVALID"),
                    (6, body.get("invoiceHash", ""), "invoiceHash_QRCODE_INVALID"),
                    (7, g("//ds:SignatureValue"), "INVOICE_SIGNATURE_VALUE_QRCODE_INVALID"))
                for tag, expected, code in checks:
                    if tags[tag].decode("utf-8", errors="replace") != expected:
                        errors.append(zatca_error(code, f"QR tag {tag} does not match the invoice", "QRCODE_ERRORS"))
                if tags[3].decode("utf-8", errors="replace") != f"{g('/*/cbc:IssueDate')}T{g('/*/cbc:IssueTime')}":
                    warnings.append(zatca_warning("invoiceTimeStamp_QRCODE_INVALID", "QR timestamp does not match "
                                                  "the invoice issue date and time", "QRCODE_ERRORS"))
        return errors, warnings

    # -- APIs (called by _transport with the request's headers and JSON body) --
    def _compliance_csid(self, environment, headers, body):
        if "otp" not in headers:
            return self._missing_otp()
        scripted = self._scripted("compliance_csid", None)
        if scripted:
            return scripted
        if headers["otp"] in self.rejected_otps:
            return ApiResponse(400, {"errors": [{"code": "Invalid-OTP", "message": "The provided OTP is invalid"}]})
        return self._issue(self._csr(body["csr"]), "compliance")

    def _compliance_check(self, environment, headers, body):
        if self._auth_kind(headers) != "compliance":
            return self._unauthorised()
        scripted = self._scripted("compliance_check", body)
        if scripted:
            return scripted
        xml = base64.b64decode(body["invoice"])
        type_code = ubl.find_text(xml, "/*/cbc:InvoiceTypeCode")
        subtype = ubl.find_text(xml, "/*/cbc:InvoiceTypeCode/@name")
        category = "standard" if subtype.startswith("01") else "simplified"
        key = "clearanceStatus" if category == "standard" else "reportingStatus"
        if (category, type_code) in self.fail_compliance_types:
            return ApiResponse(400, {"validationResults": _validation([zatca_error("BR-KSA-TEST", "Fake failure")])})
        errors, warnings = self.document_findings(body)  # the compliance API does not check the CSID's VAT
        warnings += list(self.compliance_warnings.get((category, type_code), ()))
        if errors:
            return ApiResponse(400, {"validationResults": _validation(errors, warnings),
                                     key: "NOT_CLEARED" if key[0] == "c" else "NOT_REPORTED"})
        return ApiResponse(202 if warnings else 200, {"validationResults": _validation(warnings=warnings),
                                                      key: "CLEARED" if key[0] == "c" else "REPORTED"})

    def _production_csid(self, environment, headers, body):
        if self._auth_kind(headers) != "compliance":
            return self._unauthorised()
        scripted = self._scripted("production_csid", None)
        if scripted:
            return scripted
        request_id = str(body.get("compliance_request_id", ""))
        if not (request_id.isdigit() and len(request_id) == 13) or request_id not in self._csr_by_request:
            return ApiResponse(400, {"code": "Incorrect-ComplianceRequestId",
                                     "message": "The compliance request id must be 13 digits"})
        return self._issue(self._csr_by_request[request_id], "production")

    def _renew_production_csid(self, environment, headers, body):
        if self._auth_kind(headers) != "production":
            return self._unauthorised()
        if "otp" not in headers:
            return self._missing_otp()
        scripted = self._scripted("renew_production_csid", None)
        if scripted:
            return scripted
        if headers["otp"] in self.rejected_otps:
            return ApiResponse(400, {"errors": [{"code": "Invalid-OTP", "message": "The provided OTP is invalid"}]})
        return self._issue(self._csr(body["csr"]), "production")

    def _clearance(self, environment, headers, body):
        if self._auth_kind(headers) != "production":
            return self._unauthorised()
        if self.clearance_disabled or headers.get("clearance-status") == "0":
            return self.clearance_deactivated()
        scripted = self._scripted("clearance", body)
        if scripted:
            return scripted
        errors, warnings = self.document_findings(body, auth_vat=self._token_vat(headers))
        if errors:
            return self.rejected(*errors)
        return self.cleared(body, warnings=warnings)

    def _reporting(self, environment, headers, body):
        if self._auth_kind(headers) != "production":
            return self._unauthorised()
        scripted = self._scripted("reporting", body)
        if scripted:
            return scripted
        errors, warnings = self.document_findings(body, auth_vat=self._token_vat(headers))
        if errors:
            return ApiResponse(400, {"validationResults": _validation(errors, warnings),
                                     "reportingStatus": "NOT_REPORTED"})
        if warnings:
            return self.accepted_with_warnings("reporting", *warnings)
        return ApiResponse(200, {"validationResults": _validation(), "reportingStatus": "REPORTED"})

    # -- response builders --
    @staticmethod
    def cleared(body, warnings=()):
        """ZATCA's cleared XML: the signed XML with a QR added (tags 1 to 8, no tag 9; R-15)."""
        xml = base64.b64decode(body["invoice"])
        g = lambda xp: ubl.find_text(xml, xp)  # noqa: E731
        cert = crypto.load_cert_base64(g("//ds:X509Certificate"))
        values = [(1, g("/*/cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName").encode()),
                  (2, g("/*/cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID").encode()),
                  (3, f"{g('/*/cbc:IssueDate')}T{g('/*/cbc:IssueTime')}".encode()),  # no "Z" (R-15)
                  (4, g("/*/cac:LegalMonetaryTotal/cbc:PayableAmount").encode()),  # BT-115 (R3.4.8 §3.1)
                  (5, g("/*/cac:TaxTotal/cbc:TaxAmount").encode()),  # the first TaxTotal: BT-111 in SAR (G2)
                  (6, body["invoiceHash"].encode()),
                  (7, g("//ds:SignatureValue").encode()), (8, crypto.spki_der(cert.public_key()))]
        qr_b64 = qr.encode(values)
        text = xml.decode("utf-8")
        marker = "<cac:Signature>"
        text = text.replace(marker, ubl.QR_ADR.format(qr=qr_b64) + marker, 1)
        status = 202 if warnings else 200
        return ApiResponse(status, {"validationResults": _validation(warnings=warnings),
                                    "clearanceStatus": "CLEARED",
                                    "clearedInvoice": base64.b64encode(text.encode()).decode()})

    @staticmethod
    def clearance_deactivated():
        """HTTP 303 as the developer portal sends it: a JSON message and no ``Location`` header (R-44)."""
        return ApiResponse(303, {"message": "Clearance is deactiviated. Please use the /invoices/reporting/single "
                                            "endpoint instead."})

    @staticmethod
    def rejected(*errors):
        return ApiResponse(400, {"validationResults": _validation(errors), "clearanceStatus": "NOT_CLEARED"})

    @staticmethod
    def accepted_with_warnings(api, *warnings):
        key = "reportingStatus" if api == "reporting" else "clearanceStatus"
        return ApiResponse(202, {"validationResults": _validation(warnings=warnings), key: "REPORTED"})

    @staticmethod
    def timeout():
        return ApiResponse(None, {}, "TimeoutError")


@contextmanager
def fake_zatca_services():
    """Install a fresh local secret store and fake ZATCA client for the duration of a test."""
    store, fake = keystore.LocalSecretStore(), FakeZatcaClient()
    previous_store, previous_client = keystore.override(store), zatca_client.override(fake)
    try:
        yield store, fake
    finally:
        keystore.override(previous_store)
        zatca_client.override(previous_client)
