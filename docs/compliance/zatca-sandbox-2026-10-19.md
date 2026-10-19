# ZATCA developer-portal sandbox run — 2026-10-19

Follows `zatca-sdk-3-findings-2026-10-17.md` ("findings-3"). It checks the open sandbox items of `docs/specs/zatca.md`
against ZATCA's live developer-portal sandbox: OQ-30 (API paths and headers), OQ-29 (386 as a compliance sample),
OQ-22 (tag 5 currency), OQ-28 / OQ-18 (QR tag 3), OQ-26 (simulation), findings-3 §7.5 (SignedProperties whitespace,
which rule set ZATCA runs).

**Run conditions.**
- **Host:** `https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal` (ZATCA's public developer-portal
  sandbox). Nothing was sent to the simulation or production environments.
- **Code:** a clean `git archive HEAD` export at **`9a4d962`**. The zatca code is unchanged up to `bd62d33`.
  - Keys and CSRs: `zatca.crypto`.
  - Documents: the module's own services, in the fake test world (fixture entities, users and customers).
  - HTTP calls: the module's `zatca.client.HttpZatcaClient`, driven from scratch scripts outside pytest.
  - Generators: a scratch-only test file in the export, not committed.
- **Fake data only.**
  - Seller VATs: `312345678900003`, `320000000000003` and `399999999900003` (ZATCA's documented test VAT).
  - Seller name: "شركة TST1 للاختبار". CSR names: "Fake Sandbox Trading Co" / "Fake Test Branch".
  - Buyers: the fixture buyers (VAT `399999999900003` or `311111111100003`) and "وزارة وهمية للاختبار"
    (`310000000000003`).
  - OTP: ZATCA's public developer-portal test OTP `123345`.
- **No Almiras identifiers or credentials** were used.
- **Keys and secrets:** EGS keys, CSIDs and secrets stayed in the session scratch directory, and were deleted at
  the end. None is in the repo. The documented OTP is a public test value.
- **No zatca code was changed.**

## 1. Results at a glance

| Open item | Result |
| --- | --- |
| OQ-30 paths / headers | **Settled for the developer portal** (§2). Also found: the client's default `Python-urllib` User-Agent is blocked by Cloudflare (error 1010, HTTP 403). The client must send its own User-Agent (§7 item 1) |
| QR tag 3 (OQ-28, OQ-18) | **Module form accepted.** `IssueDate"T"IssueTime` (AST, no `Z`) passes. `…Z` on the same AST time, `+03:00` and date-only get the warning `invoiceTimeStamp_QRCODE_INVALID` / `timestamp_QRCODE_INVALID` (HTTP 202). The correct UTC instant with `Z` (`…T07:00:00Z` for 10:00 AST) also passes. ZATCA's own QR on cleared documents writes the no-`Z` form |
| Tag 5 currency (OQ-22, D-18(a)) | **Not validated by ZATCA**: SAR 56.25, USD 15.00 and 0.01 all pass. ZATCA's own clearance QR takes the **first** `TaxTotal`: 56.25 SAR with the module's SAR-first order, 15.00 USD when USD is put first. The module's G2 choice is what ZATCA itself writes. Accepted; no change |
| SignedProperties whitespace (findings-3 §7.5) | **Accepted.** The module's whitespace-free form passes reporting, which **does** check the SignedProperties digest (`signed-properties-hashing`). ZATCA digests the element as written, with the dom4j namespace declarations (the module's `signed_properties_digest_form`, AT-66). A pretty-printed SignedProperties also passes when its digest is computed the same way. The plain-bytes digest fails |
| OQ-29 (386 compliance sample) | **Not settleable in the developer portal.** It does not track compliance steps: `requestID` is always `1234567890123`, and a production CSID is issued with no compliance invoice sent at all. A plain 386 passes compliance, clearance and reporting. The final 388 with deduction lines cannot be built by the module (`NotImplementedError`, R-39). Needs the simulation portal (§6) |
| Rule set run by ZATCA (findings-3 §7.5) | **Not R4.0.0's B2G rules on the developer portal.** All of these clear with no warning or error: GOV without BT-7 (R4.0.0: BR-KSA-92 warning), KSA-40 `LLC` (BR-KSA-91 warning), a 9-position KSA-2 (an R4.0.0 error). Production's rule set stays open; keep the module's stricter local checks |
| OQ-26 (simulation) | Not tested (needs a real taxpayer OTP). The developer portal accepts **any** template name (`TSTZATCA-`, `PREZATCA-`, `ZATCA-Code-Signing`), so it does not settle §3.2 either |

## 2. API paths and headers that work (OQ-30)

Base URL: `https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal`.

| API (`zatca.client` key) | Method + path | Auth / extra headers | Body | Answer |
| --- | --- | --- | --- | --- |
| `compliance_csid` | `POST /compliance` | `OTP: <6 digits>` | `{"csr": base64(PEM)}` (`crypto.csr_base64`) | 200 `{requestID (number), dispositionMessage "ISSUED", binarySecurityToken, secret, errors null}` |
| `compliance_check` | `POST /compliance/invoices` | Basic `binarySecurityToken:secret` of the **compliance** CSID | `{"invoiceHash", "uuid", "invoice"}` | 200 PASS, 202 with warnings, 400 with errors; `clearanceStatus` / `reportingStatus`, `qrBuyertStatus`, `qrSellertStatus` (sic). With the production CSID: 401 |
| `production_csid` | `POST /production/csids` | Basic of the compliance CSID | `{"compliance_request_id": "<13 digits>"}` | 200 `{requestID, tokenType, dispositionMessage, binarySecurityToken, secret}`; another id: 400 `Incorrect-ComplianceRequestId` "must be 13 digits"; with production CSID: 401 |
| `renew_production_csid` | `PATCH /production/csids` | Basic of the **production** CSID + `OTP` | `{"csr": …}` | 200 as above; without OTP 400 `Missing-OTP` |
| `clearance` | `POST /invoices/clearance/single` | Basic of the production CSID; `Clearance-Status: 1` | as compliance | 200 `{validationResults, clearanceStatus "CLEARED", clearedInvoice}`; `Clearance-Status: 0` → **303** `{"message": "Clearance is deactiviated. Please use the /invoices/reporting/single endpoint instead."}` with **no** `Location` header; header missing → treated as 1; with compliance CSID: 401 |
| `reporting` | `POST /invoices/reporting/single` | Basic of the production CSID | as compliance | 200 `{validationResults, reportingStatus "REPORTED"}` (no `clearedInvoice`) |

Headers on every call:
- `Accept-Version: V2` is required. Without it the answer is 406, as plain text ("This Version is not supported
  or not provided in the header.").
- `Content-Type: application/json` and `Accept: application/json`.
- `Accept-Language: ar` is accepted, but the messages still came back in English (one test), so the module's
  `arabic_for` table stays needed.

The module's client already sends exactly this (paths aside). Two gaps:
- **User-Agent.** `urllib`'s default `Python-urllib/3.13` gets Cloudflare error 1010 (HTTP 403, "browser_signature_banned").
  curl's default and an explicit `AlmirasERP-sandbox-test/0.1` work. This run installed an opener with that header
  in the scratch script.
- **Error bodies.** A malformed CSR gives `400 Invalid Request` as plain text, and a missing OTP gives
  `400 {"errors":[{"code":"Missing-OTP",…}]}`. The client's `_json` keeps plain-text bodies under `raw`, which
  is fine.

Base URLs of simulation and production were not called (§6).

## 3. Onboarding

- **Compliance CSID.** The certificate is issued for **our CSR and our key**:
  - Subject = our CSR's C/OU/O/CN; SAN DirectoryName = our SN/UID/title/registeredAddress/businessCategory.
  - Issuer `CN=eInvoicing`, valid 5 years.
  - `crypto.cert_base64_from_token` decodes the token correctly.
  - **Any OTP is accepted** (`123345`, `000000`, even the 5-digit `12345`). So the developer portal does not
    exercise R-7's rejection path; the fake client's `rejected_otps` stays the test for it.
  - Every call returns a new secret, but `requestID` is always `1234567890123`.
- **Production CSID.** Always the **same static certificate**, which is the ZATCA SDK's bundled dummy certificate:
  - Subject `CN=TST-886431145-399999999900003, OU=Riyadh Branch, O=Maximum Speed Tech Supply LTD, C=SA`, issuer
    `CN=PRZEINVOICESCA4-CA, DC=extgazt, DC=gov, DC=local`, serial `3791127428…387`, valid 2024-01-11 to 2029-01-09.
  - **Its public key is not the EGS key.** It matches the SDK's public `ec-secp256k1-priv-key.pem`.
  - It is issued without any compliance invoice having been sent.
  - Renewal (PATCH) also returns a static certificate.
- **Consequences for the module in `sandbox`.** `request_production_csid` would store this certificate and set
  the unit `active`. Every later stamp then stops at `key_mismatch` (`_sign_xml` checks key against certificate),
  so nothing wrong is signed, but the unit looks active and can never sign. `renew_production_csid` already refuses
  ("renewed certificate is not for the new key pair"). See §7 item 4.
- **Clearance and reporting are bound to the production CSID's VAT.** Documents of seller VAT `312345678900003`
  were refused with `certificate-permissions` ("User only allowed to use the vat number that exists in the
  authentication certificate"). So clearance and reporting were run with module documents for seller VAT
  `399999999900003`, signed with the SDK's public dummy key and the static production certificate. The compliance
  API does **not** check seller VAT against the compliance CSID: documents of `312…` passed with a CSID for `399…`.

## 4. Documents sent

All documents were built by the module:
- `A*`: `services.run_compliance_checks` as built, with the real compliance CSID handed to the fake client, so
  that exactly the R-8 samples were signed.
- Others: `stamp_document` on invoicing documents in the fake world (issue date 2026-10-15 10:00 AST), or
  `services._sign_xml` on module `InvoiceData` where the services refuse the document (386 and the §5 variants).
- Seller VAT `312345678900003`; compliance CSID auth.

| Document | Compliance API |
| --- | --- |
| A1–A3: R-8 samples standard 388, 383, 381 | 200 CLEARED, PASS, no warnings |
| A4–A6: R-8 samples simplified 388, 383, 381 | 200 REPORTED, PASS |
| B1–B3: standard 388 → 381 → 383 chain (ICV 1–3, PIH chained) | 200 CLEARED, PASS |
| C1–C3: simplified 388 → 381 → 383 chain | 200 REPORTED, PASS |
| D1: simplified USD 388 (rate 3.75; SAR TaxTotal first; tag 5 `56.25`, tag 4 `115.00` = BT-115 USD) | 200 REPORTED, PASS |
| D2: standard USD 388 | 200 CLEARED, PASS |
| E1: B2G standard 388, buyer KSA-40 `GOV`, BT-7 TaxPointDate 2026-10-05 (payment order date), KSA-2 `0100000` | 200 CLEARED, PASS, no BR-KSA-91/-92 |
| F1 / F2: 386 standard / simplified (probe: a 388's data re-typed 386, no deduction lines; the services refuse 386, R-39) | 200 CLEARED / REPORTED, PASS |
| 386 + final 388 with deduction lines | **not built**: `ubl.build_unsigned` raises `NotImplementedError` for prepayment lines (R-39, waits for invoicing prepayments) |

The same documents for seller VAT `399999999900003` (production-CSID auth) gave these results. Clearance: B1–B3,
D2, E1 → 200 CLEARED, PASS, `clearedInvoice` returned. Reporting: C1–C3, D1 → 200 REPORTED, PASS. No warnings.

**First run, with seller VAT = `399999999900003`.**
- Every **standard** document (R-8 samples and fixture documents) failed `BR-CUSTOM-VALIDATION-01`: "The Seller
  VAT registration number or seller group VAT registration number (BT-31) of The invoice must not match the buyer
  VAT registration number (BT-48)".
- The cause: `services.SAMPLE_BUYER` (the R-8 sample buyer) and the zatca fixture buyer both carry
  `399999999900003`, which is also ZATCA's documented test VAT that the developer portal's production CSID is
  bound to.
- `BR-CUSTOM-VALIDATION-01` is not in either SDK's rules. Clearance also rejects it (I1 below).

**Cleared XML.**
- Same UUID and same invoice hash as sent (so `_cleared_result` accepts it), pretty-printed.
- ZATCA's QR has tags 1–8: tag 3 `2026-10-15T10:00:00`; tag 4 BT-115; tag 5 = first `TaxTotal`.
- In the sandbox the cleared XML is **re-stamped**:
  - new `SigningTime` in AST without `Z` (`2026-10-19T21:32:30`);
  - re-indented SignedProperties with a recomputed digest (module form);
  - a new `SignatureValue` that does **not** verify over the invoice hash with the embedded certificate.
- The module rightly checks only UUID and hash on `cleared_xml`. Nothing should verify the seller signature of a
  cleared XML.

## 5. What the sandbox checks (negative controls)

**QR and signature variants.** These keep the invoice hash. Made from module-signed documents (C1 simplified,
B1 standard, D1 USD).

| Variant | Compliance API (CCSID) | Reporting / clearance (PCSID) |
| --- | --- | --- |
| Control (QR re-encoded unchanged) | PASS | PASS |
| Tag 3 `…T10:00:00Z` (wrong instant) | 202 W `invoiceTimeStamp_QRCODE_INVALID` | 202 W, same |
| Tag 3 `…+03:00` / date only | 202 W `invoiceTimeStamp_QRCODE_INVALID` / `timestamp_QRCODE_INVALID` | — |
| Tag 3 `2026-10-15T07:00:00Z` (same instant in UTC) | PASS | PASS |
| Tag 5 = USD 15.00 / `0.01` on the USD invoice | PASS / PASS (tag 5 not checked) | PASS / PASS |
| Tag 4 wrong | E `invoiceTotal_QRCODE_INVALID` | — |
| Tag 1 wrong | E `sellerName_QRCODE_INVALID` | — |
| Tag 9 missing / tags 8+9 missing | E `QRCODE_INVALID` / E `QRCODE_INVALID` | E `QRCODE_INVALID` |
| Tag 7 ≠ SignatureValue | E `INVOICE_SIGNATURE_VALUE_QRCODE_INVALID` | E, same |
| SignatureValue replaced by a wrong (valid-DER) signature, tag 7 matching | — | **PASS** (signature value not verified) |
| SignatureValue flipped, standard | **PASS** (CLEARED) | **PASS** (CLEARED) |
| Body `invoiceHash` wrong | E `invalid-invoice-hash` (+ `invoiceHash_QRCODE_INVALID` simplified) | E, same |
| SignedProperties digest wrong, or whitespace added with stale digest | **PASS** (not checked) | simplified: E `signed-properties-hashing`; standard clearance: **PASS** |
| SignedProperties pretty-printed, digest recomputed with dom4j namespace declarations | — | PASS |
| SignedProperties digest over the plain bytes (no namespace declarations), with or without whitespace | — | E `signed-properties-hashing` |
| Wrong-VAT CSID: 312 documents with a 399 CSID | PASS (not checked) | E `certificate-permissions` (clearance/reporting) |
| Documents signed with a certificate that is not the auth CSID's | — | W `certificate-issuer-name`, `invalid-signing-certificate` |

**Re-signed variants** (production-CSID auth):

| Variant | Result |
| --- | --- |
| G1 standard USD with the USD `TaxTotal` first | CLEARED, PASS; ZATCA's QR tag 5 = `15.00` (USD) |
| G2 simplified USD with USD first, tag 5 USD | REPORTED, PASS |
| H1 B2G `GOV` without BT-7 | CLEARED, PASS, no warning |
| H2 B2G KSA-40 `LLC` | CLEARED, PASS, no warning |
| H3 B2G 9-position KSA-2 `010000010` | CLEARED, PASS |
| H4 9-position KSA-2, no `GOV`, no BT-7 | CLEARED, PASS |
| H5 BT-7 after the issue date | CLEARED, PASS |
| I1 standard with buyer VAT = seller VAT | 400 `BR-CUSTOM-VALIDATION-01` |

**What this means.**
- The sandbox checks the invoice hash (recomputed server-side: the module's R-13 hash is confirmed by ZATCA).
- On simplified documents it checks the QR tags against the XML, and the SignedProperties digest on reporting.
- It does **not** verify the ECDSA signature value, and it does not check the SignedProperties digest on
  compliance or clearance.
- **A sandbox PASS is therefore no evidence that signatures are valid.** The offline SDK check (AT-69,
  findings-3 §7.2) stays the only signature evidence.
- The module's whitespace-free SignedProperties and its digest form are confirmed where ZATCA checks them.
- The module's RFC 2253 `X509IssuerName` form (`CN=PRZEINVOICESCA4-CA, DC=extgazt, DC=gov, DC=local`) drew no
  `certificate-issuer-name` warning with the matching certificate.

## 6. Not verified

- **Simulation** (`PREZATCA-Code-Signing`, base URL, the real onboarding gate between compliance and production
  CSID). Untested because it needs a real taxpayer's Fatoora portal OTP.
  - OQ-29 (whether a 386 is a required compliance sample) can only be settled there, or by ZATCA.
  - The same goes for which rule set production runs (R3.4.8 vs R4.0.0: 9-position KSA-2, BR-KSA-91/-92).
  - This needs the owner. Per OQ-36 the owner would onboard a test EGS in simulation with a dedicated test unit
    and give agents only scratch, non-production results. Real credentials never go to agents or the repo.
- The final 388 deducting a 386 (not buildable yet).
- 3-decimal currencies (KWD) against the sandbox: not sent this run. The SDK result (R-51, 2 decimals in XML)
  stands.
- Whether `Accept-Language: ar` returns Arabic messages for other error kinds (one test, English).

## 7. Zatca code and spec changes still needed (for module-builder / spec-writer; not made here)

| # | File | Change | Reason |
| --- | --- | --- | --- |
| 1 | `zatca/client.py` (`HttpZatcaClient._call`) | **Send an explicit `User-Agent`** (e.g. `AlmirasERP/<version>`), with a test that the header is set. **Blocking**: without it every real call fails 403 | §2: Cloudflare error 1010 bans `Python-urllib` on `gw-fatoora.zatca.gov.sa` |
| 2 | `zatca/client.py` | Do not follow redirects (a `urllib` opener without `HTTPRedirectHandler`, or treat 3xx as final). A 303 must reach `_classify` as `CLEARANCE_DISABLED` | §2: 303 is the clearance-off signal; it has no `Location` today, but urllib would re-send as GET if one appeared |
| 3 | `config/settings.py` (`ZATCA_API_PATHS`, `ZATCA_API_BASE_URLS["sandbox"]` default or `.env` example) | Paths `compliance`, `compliance/invoices`, `production/csids` (POST), `production/csids` (PATCH, renewal), `invoices/clearance/single`, `invoices/reporting/single`; sandbox base `https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal`. Simulation / production base URLs stay configuration until verified | OQ-30 |
| 4 | `zatca/services.py` (`request_production_csid`) | Before storing the production CSID, check that its public key is the unit's key (as `renew_production_csid` already does). Refuse with an en/ar message and keep the unit `compliance_passed`. Spec: the developer-portal `sandbox` supports onboarding and compliance checks only; its static production certificate cannot sign | §3 |
| 5 | `zatca/services.py` (`SAMPLE_BUYER`) | Use a sample-buyer VAT that is not ZATCA's test VAT `399999999900003` (e.g. `310000000000003`), and pick another one if it equals the seller VAT. The zatca fixture buyer has the same value: change it too, or the fixtures can never mirror a sandbox seller | §4, BR-CUSTOM-VALIDATION-01 |
| 6 | `zatca/rules.py` (`document_problems`) + `messages.LOCAL` | Local refusal before any ICV when buyer VAT (BT-48) = seller VAT (BT-31), en/ar, code `BR-CUSTOM-VALIDATION-01` | §5 I1: ZATCA rejects at clearance; not in the SDKs |
| 7 | `zatca/messages.py` (`ZATCA_CODES_AR`) | Arabic for the codes seen: `BR-CUSTOM-VALIDATION-01`, `certificate-permissions`, `certificate-issuer-name`, `invalid-signing-certificate`, `signed-properties-hashing`, `invoiceHash_QRCODE_INVALID`, `invoiceTimeStamp_QRCODE_INVALID`, `timestamp_QRCODE_INVALID`, `invoiceTotal_QRCODE_INVALID`, `sellerName_QRCODE_INVALID`, `INVOICE_SIGNATURE_VALUE_QRCODE_INVALID`, `QRCODE_INVALID`, `XML-INVOICE-ERROR`, `Missing-OTP`, `Incorrect-ComplianceRequestId` | OQ-16 table; ZATCA answered in English with `Accept-Language: ar` |
| 8 | `zatca/testing.py` (`FakeZatcaClient`) | Mirror the sandbox: `compliance_check` 202 with warnings; `certificate-permissions` when the document VAT ≠ the auth certificate's VAT on clearance/reporting; 401 for the wrong CSID kind; `BR-CUSTOM-VALIDATION-01`; 303 body text | keeps tests close to ZATCA |
| 9 | `docs/specs/zatca.md` | OQ-30: resolved for the developer portal (§2; simulation/production URLs open). OQ-28 / OQ-18 note: tag 3 no-`Z` form confirmed by the sandbox; the `Z` form is warned. OQ-22: the sandbox confirmation of D-18(a) is done; tag 5 is not validated and ZATCA's own QR uses the first TaxTotal. R-14 / AT-66: SignedProperties digest form and whitespace confirmed. OQ-29: still open, simulation only. OQ-26: the developer portal ignores the template name. R-8: sample buyer ≠ seller. New local rule for BR-CUSTOM-VALIDATION-01. §6: a sandbox PASS does not verify signatures; AT-69 stays the evidence. OQ-36: input for the owner's how-to (developer portal = free, any OTP, static production CSID; simulation = real Fatoora login) | this file |

**No change needed for:**
- QR tag 3 (R-15), tag 4 = BT-115, tags 1–9 on simplified documents (tags 8/9 required: `QRCODE_INVALID`
  without them).
- TaxTotal order and tag 5 in SAR (R-40, D-18(a)), invoice hash (R-13), SignedProperties form (R-14), issuer-name
  style.
- B2G emission (R-54: E1 cleared with `GOV` and BT-7).
- `_classify` (200 / 202 / 303 / 400 / 401 behave as coded) and `_cleared_result` (UUID and hash match).
- `run_compliance_checks` (accepts 200 and 202 without errors).

## 8. Reproduce

1. **Export.** `git archive HEAD` into a new scratch directory.
2. **Phase 1, compliance CSID.** A script outside pytest:
   - calls `django.conf.settings.configure()` with the §2 base URL and paths;
   - installs a urllib opener with an explicit User-Agent;
   - generates the key and CSR with `zatca.crypto.generate_private_key` / `build_csr`, using the fake values above;
   - calls `HttpZatcaClient().compliance_csid("sandbox", csr_b64, "123345")`.
3. **Phase 2, documents.** A scratch test file in the export's `zatca/` (never in the repo), run with pytest in
   the fake world:
   - (a) `run_compliance_checks` with the real compliance-CSID response scripted into `FakeZatcaClient` and
     `crypto.generate_private_key` patched to return the phase 1 key;
   - (b) invoicing documents stamped with the compliance (or static production) certificate, swapping the signing
     credential as in `docs/compliance/zatca-sdk-kit/scripts/sdk3/sdk3_export_generator.py`;
   - (c) variants signed with `services._sign_xml`. Writes `{invoiceHash, uuid, invoice}` bodies.
4. **Phase 3, submission.** `HttpZatcaClient.compliance_check` / `production_csid` / `clearance` / `reporting`
   with `Auth(binarySecurityToken, secret)`.
5. **Clearance and reporting.** Sign with the SDK's public `Data/Certificates/ec-secp256k1-priv-key.pem` and the
   static production CSID, using seller VAT `399999999900003` and a different buyer VAT.
6. **Clean up.** Delete keys and secrets afterwards.
