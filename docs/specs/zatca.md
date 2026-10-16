# Spec: `zatca` — ZATCA Fatoora Phase 2 e-invoicing (Track A)

Status: draft for review · Written: 2026-10-16 · Author: spec-writer agent
Depends on: `core` (Entity), `docs/specs/invoicing.md` (AR/AP documents, written in parallel)

> **Verification tag.** Statements marked **[CC-verify]** are ZATCA technical requirements written from memory without access to the official documents. The compliance-checker agent must verify each one against the current official ZATCA publications (E-Invoicing Regulation, Implementing Resolution, XML Implementation Standard, Security Features Implementation Standard, Data Dictionary, Developer Portal / Fatoora API docs) before module-builder starts. Where the official text differs, the official text wins and this spec is amended.

---

## 1. Purpose

The `zatca` app is the e-invoicing layer between the `invoicing` app and ZATCA's Fatoora platform, for **every** VAT-registered entity in the group (Almiras and any subsidiary added as an `Entity` row). For each entity it onboards one or more EGS (e-invoice generation solution) units, holds their certificate metadata, and for each tax document handed over by `invoicing` it generates UBL 2.1 XML, assigns the invoice counter (ICV) and previous-invoice hash (PIH), computes the invoice hash, applies the cryptographic stamp, builds the QR code, and submits the document — **clearance** for standard (B2B) tax invoices and their notes, **reporting** for simplified (B2C) invoices and their notes. It stores what was sent and what ZATCA returned, surfaces errors and warnings in English and Arabic, and keeps the archive for the retention period. It does not create business documents and does not post journal entries.

## 2. Decisions

| Date | Who | Decision |
| --- | --- | --- |
| — | — | **No business decisions have been recorded for this module yet.** The only binding rules are those in `CLAUDE.md` (open source only; connections to outside systems only through the integration layer; no real credentials or production data for agents or tests; every record has an `entity` FK; bilingual user-facing text; business logic in `services.py`; spec before code). |
| 2026-10-16 | Caller (task brief) | Scope: e-invoicing layer only. Business documents are in `invoicing.md`. Tests use mocks/fixtures of the ZATCA sandbox/simulation only. Secret storage left open. This module creates no journal entries. |

Everything else is listed in §9 Open questions.

## 3. Data model

All models: `entity = FK(core.Entity, on_delete=PROTECT)`, required. `created_at`, `created_by`, `source` (human / import / agent / system, per `docs/ai-agent-gateway.md`) on every model; omitted from tables below.

### 3.1 `ZatcaProfile` — one per entity (taxpayer settings)

| Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- |
| entity | OneToOne(Entity) | Y | — | One profile per entity |
| vat_number | read from `Entity.vat_number` | Y | — | Not duplicated; profile cannot be enabled if blank. Format **[CC-verify]**: 15 digits, starts and ends with 3 |
| legal_name_en / legal_name_ar | Char(200) | Y | Y | Seller name on XML/QR; default from `Entity.name_en/ar` |
| seller_id_scheme / seller_id_value | Char | Y | — | e.g. CRN (from `Entity.cr_number`) **[CC-verify]** allowed schemes |
| address: building_no, street_en/ar, district_en/ar, city_en/ar, postal_code, additional_no, country (default SA) | Char | Y | Y (names) | Saudi national address fields **[CC-verify]** required set |
| vat_group | FK(VatGroup), null | N | — | Only if VAT-group registration is confirmed (OQ-5) |
| environment | Choice: `sandbox`, `simulation`, `production` | Y | — | Default `sandbox`. `production` settable only by permission in §7 and only outside test settings |
| go_live_date | Date, null | N | — | Wave date for this entity (OQ-1). Null = not live; documents are not submitted |
| is_enabled | Bool | Y | — | Default False |

### 3.2 `EgsUnit` — an onboarded generation unit

| Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- |
| entity | FK(Entity) | Y | — | |
| code | Char(20) | Y | — | Unique per entity |
| name_en / name_ar | Char(200) | Y | Y | |
| branch_name_en / branch_name_ar | Char(200) | N | Y | CSR OU field **[CC-verify]** |
| serial_number | Char | Y | — | CSR serial, format `1-<solution>\|2-<model>\|3-<serial>` **[CC-verify]** |
| location / industry | Char | Y | — | CSR fields **[CC-verify]** |
| supports_standard / supports_simplified | Bool | Y | — | Drives CSR invoice-type flags **[CC-verify]** (4-digit "TSCZ" flag) |
| status | Choice: `draft`, `csr_ready`, `compliance_csid`, `compliance_passed`, `active`, `renewal_due`, `expired`, `revoked` | Y | — | See R-5 |
| last_icv | PositiveBigInteger | Y | — | Default 0. Updated only by `services.stamp_document` |
| last_invoice_hash | Char(64+), | Y | — | PIH for next document. Initial value **[CC-verify]**: base64 of SHA-256 of the string "0" |
| private_key_ref | Char | Y once `csr_ready` | — | **Reference** to a secret store entry, never the key (OQ-3) |

Constraint: unique (`entity`, `code`).

### 3.3 `EgsCredential` — compliance CSID or production CSID (PCSID)

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| entity | FK(Entity) | Y | Must equal `egs_unit.entity` |
| egs_unit | FK(EgsUnit) | Y | |
| kind | Choice: `compliance`, `production` | Y | |
| certificate_pem | Text | Y | Public certificate; not secret |
| secret_ref | Char | Y | Reference to the API secret in the secret store (OQ-3) |
| zatca_request_id | Char | Y | Returned by ZATCA on issue **[CC-verify]** |
| valid_from / valid_to | DateTime | Y | Parsed from the certificate |
| status | Choice: `active`, `superseded`, `expired`, `revoked` | Y | |
| replaced_by | FK(self), null | N | Renewal chain |

At most one `active` credential per (`egs_unit`, `kind`) — DB partial unique constraint.

### 3.4 `EInvoice` — the e-invoicing record for one business document

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| entity | FK(Entity) | Y | Must equal source document entity and `egs_unit.entity` |
| source_document | FK to invoicing document (exact model per `invoicing.md`, OQ-12) | Y | One `EInvoice` per issued document |
| egs_unit | FK(EgsUnit) | Y | |
| uuid | UUID | Y | Generated once, never reused |
| zatca_category | Choice: `standard`, `simplified` | Y | |
| type_code | Choice: `388` invoice, `381` credit note, `383` debit note (+ `386` prepayment if in scope, OQ-13) **[CC-verify]** | Y | |
| subtype_code | Char(7) | Y | e.g. `0100000` standard / `0200000` simplified, plus flags (third-party, nominal, export, summary, self-billed) **[CC-verify]** |
| billing_reference | FK(EInvoice), null | Y for 381/383 | Original invoice; plus reason text en/ar **[CC-verify]** reason required |
| issue_datetime | DateTime (Asia/Riyadh) | Y | |
| icv | PositiveBigInteger | Y after stamping | Unique per `egs_unit` |
| pih | Char | Y after stamping | |
| invoice_hash | Char | Y after stamping | base64 SHA-256 **[CC-verify]** |
| signed_xml | Text / file | Y after stamping | Immutable once written |
| qr_tlv | Text (base64) | Y after stamping | |
| cleared_xml | Text / file, null | Y when cleared | XML returned by ZATCA (carries ZATCA stamp/QR) **[CC-verify]** |
| status | Choice: `pending`, `stamped`, `queued`, `submitting`, `cleared`, `cleared_with_warnings`, `reported`, `reported_with_warnings`, `rejected`, `failed_retryable`, `failed_final` | Y | See R-20 |
| reporting_deadline | DateTime, null | Y for simplified | `issue_datetime + 24h` **[CC-verify]** |
| retention_until | Date | Y | From retention policy (OQ-8) |

Constraints: unique (`egs_unit`, `icv`); unique (`entity`, `uuid`); unique (`source_document`).

### 3.5 `SubmissionAttempt`

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| entity | FK(Entity) | Y | = `einvoice.entity` |
| einvoice | FK(EInvoice) | Y | |
| attempt_no | PositiveInteger | Y | 1..n |
| api | Choice: `compliance_check`, `clearance`, `reporting` | Y | |
| environment | Choice (as profile) | Y | |
| request_sha256 | Char | Y | Hash of the request body (body itself is the signed XML) |
| http_status | Integer, null | N | Null on network failure |
| response_json | JSON | N | Stored as received |
| outcome | Choice: `accepted`, `accepted_with_warnings`, `rejected`, `transient_error`, `permanent_error` | Y | |
| started_at / finished_at | DateTime | Y | |

Append-only: no update or delete after `finished_at` is set.

### 3.6 `ValidationMessage`

| Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- |
| entity | FK(Entity) | Y | — | |
| attempt | FK(SubmissionAttempt), null | N | — | Null for local (pre-submission) validation |
| einvoice | FK(EInvoice) | Y | — | |
| level | Choice: `error`, `warning`, `info` | Y | — | |
| origin | Choice: `local_xsd`, `local_rules`, `zatca` | Y | — | |
| code / category | Char | Y | — | As returned by ZATCA |
| message_en | Text | Y | Y | ZATCA text as received |
| message_ar | Text | N | Y | From a maintained code→Arabic table; falls back to "untranslated" label in Arabic UI |

### 3.7 `OnboardingEvent` (audit)

`entity`, `egs_unit`, `action` (csr_generated, otp_entered, compliance_csid_issued, compliance_checks_passed, pcsid_issued, pcsid_renewed, revoked, environment_changed), `actor`, `at`, `detail` JSON (never secrets, never OTP values). Append-only.

## 4. Rules

Enforcement column: **S** = `zatca/services.py`; **M** = model `save`/`delete` guard; **DB** = database constraint; **Q** = queryset/permission filter; **T** = test settings guard.

| # | Rule | Enforced by |
| --- | --- | --- |
| R-1 | Every `zatca` record has an `entity`; all child records (credential, e-invoice, attempt, message) have the same entity as their parent. | M (`clean`) + S |
| R-2 | An EGS unit signs only documents of its own entity. An entity's documents are never signed with another entity's EGS key or credential, even inside a VAT group. | S (`stamp_document`) |
| R-3 | All views/APIs list `zatca` records filtered by entity and user permission. | Q |
| R-4 | A profile cannot be enabled while `Entity.vat_number` or a required address field is blank. | S |
| R-5 | EGS status transitions only along: draft → csr_ready → compliance_csid → compliance_passed → active ⇄ renewal_due → expired; any → revoked. No skipping. | S |
| R-6 | A key pair is generated inside the ERP per EGS unit; the private key is written only to the secret store and the DB holds only `private_key_ref`. Private keys, CSID secrets and OTPs never appear in the DB, logs, `OnboardingEvent.detail`, error messages or fixtures. | S + test that scans logs/DB dumps |
| R-7 | The OTP is entered by a human with the onboarding permission; it is used once and not stored. | S |
| R-8 | An EGS unit becomes `active` only after all required compliance checks for its supported document types pass **[CC-verify]** (set of sample documents per type) and a PCSID is issued. | S |
| R-9 | A document can be stamped only by an `active` EGS unit holding a non-expired production credential (or the matching compliance credential in `compliance_check` mode). | S |
| R-10 | `renewal_due` is set N days before `valid_to` (N = OQ-9, safe default 30). An expired credential blocks stamping; nothing is silently signed with an expired certificate. | S (scheduled job) |
| R-11 | ICV is assigned under a row lock on `EgsUnit` (`select_for_update`): strictly increasing by 1 per EGS unit, no gaps, no reuse. | S + DB unique (`egs_unit`,`icv`) |
| R-12 | PIH of each document equals `invoice_hash` of the previous document stamped by the same EGS unit; the first document uses the initial PIH value **[CC-verify]**. ICV and PIH are assigned in the same transaction as the hash. | S |
| R-13 | Invoice hash = SHA-256 over the canonicalised XML excluding the UBLExtensions, QR and Signature elements, base64-encoded **[CC-verify]** (exact exclusions and canonicalisation method). | S |
| R-14 | Stamp = ECDSA signature over the invoice hash with the EGS private key (curve secp256k1 **[CC-verify]**), embedded per the ZATCA signature profile (XAdES-style signed properties **[CC-verify]**). | S |
| R-15 | QR = base64 TLV with tags 1 seller name, 2 VAT number, 3 timestamp, 4 total incl. VAT, 5 VAT total, 6 invoice hash, 7 ECDSA signature, 8 public key, 9 (simplified only) certificate signature **[CC-verify]**. Mandatory on simplified documents; for standard documents the QR on the cleared XML returned by ZATCA is the one used **[CC-verify]**. | S |
| R-16 | Before submission, the XML is validated locally against the UBL 2.1 XSD and ZATCA business rules (schematron) **[CC-verify]**; any local error stops the document at `pending` with `ValidationMessage(origin=local_*)` and consumes **no** ICV. | S |
| R-17 | Once `signed_xml` is written it is never modified; once `cleared_xml` is written it is never modified. Corrections are new documents (credit/debit note) created in `invoicing`, never edits. | M |
| R-18 | `EInvoice` rows with status other than `pending` cannot be deleted. `SubmissionAttempt`, `ValidationMessage`, `OnboardingEvent` cannot be updated or deleted. | M (+ DB trigger later, as for ledger) |
| R-19 | Credit and debit notes must reference an existing `EInvoice` of the same entity, and carry a reason in en/ar **[CC-verify]**. The category (standard/simplified) follows the original invoice. | S |
| R-20 | EInvoice status flow: pending → stamped → queued → submitting → {cleared, cleared_with_warnings, reported, reported_with_warnings, rejected, failed_retryable → queued, failed_final}. No other transitions. | S |
| R-21 | Standard (B2B) documents use the clearance API; simplified (B2C) documents use the reporting API. A document never goes to the wrong API. | S |
| R-22 | A standard document is **issued** only when status is `cleared` or `cleared_with_warnings`. Until then `zatca.services.is_issued(doc)` returns False and the buyer copy (PDF/XML) cannot be generated or sent. | S (+ contract test with `invoicing`) |
| R-23 | A simplified document is issued at stamping (signed XML + QR) and must be reported within 24 h of `issue_datetime` **[CC-verify]**. A document still unreported 2 h (configurable) before `reporting_deadline` raises an alert to the entity's ZATCA admins. | S (scheduled job) |
| R-24 | Transient failures (network error, timeout, HTTP 429/5xx) → `failed_retryable`, retried with exponential backoff (default: 1 min, doubling, cap 1 h, max 10 attempts — safe default, OQ-10). Retries resend the **same** signed XML with the same ICV/PIH/UUID; never re-stamp. | S |
| R-25 | ZATCA rejection (validation errors) → `rejected`; no automatic retry. Errors stored as `ValidationMessage` in en (as received) and ar (from table). | S |
| R-26 | Accepted-with-warnings stores warnings and counts as cleared/reported; warnings are shown to users in both languages. | S |
| R-27 | Submission is serialised per EGS unit in ICV order for clearance; a document is not submitted while an earlier ICV of the same unit is still `queued`/`submitting` **[CC-verify]** whether ZATCA requires ordering. | S (queue worker) |
| R-28 | Documents of an entity whose `go_live_date` is null or in the future are not submitted; `invoicing` must treat them per pre-Phase-2 rules (OQ-1). | S |
| R-29 | Only the integration layer's ZATCA client performs HTTP calls. In test settings the client is replaced by a mock; any attempt to reach a real ZATCA host from tests fails. `environment=production` cannot be set when `settings.TESTING` or an agent service user is the actor. | S + T |
| R-30 | Archive (signed XML, cleared XML, responses) is kept until `retention_until` and cannot be purged earlier; stored in KSA **[CC-verify]** data residency (also PDPL). | S + M |
| R-31 | Amounts in XML are taken from the `invoicing` document as `Decimal`, rounded to 2 dp per ZATCA rounding rules **[CC-verify]**; the module never recomputes VAT with floats and rejects a document whose line totals do not reconcile with its header. | S |
| R-32 | Sharia: this module does not validate Sharia compliance; it accepts only documents that `invoicing` has already validated (including Sharia checks). It has no path to issue a document that bypasses `invoicing` services. | S (single entry point `submit_document`) |
| R-33 | Bilingual: seller name/address in Arabic are always present in XML; buyer-facing invoice output (PDF in `invoicing`) is Arabic, optionally with English **[CC-verify]** Arabic requirement. All statuses, errors and UI labels have en/ar. | S + translations |
| R-34 | AI agents (gateway service users) may view statuses and draft nothing in this module; they cannot onboard, enter OTPs, stamp, submit, or retry. | Q (permissions) |

## 5. Accounting

**This module creates no journal entries.** It never calls `ledger.services.post_entry`, `create_entry` or `reverse_entry`. Posting is done by `invoicing` per `invoicing.md` (accounts there, e.g. 1200 Trade receivables, 2200 VAT output, from `ledger/coa_template.py`).

What `invoicing` must and must not do based on this module's status:

| Case | `invoicing` must not | `invoicing` may |
| --- | --- | --- |
| Standard (B2B) invoice/note, status before `cleared*` | Post the journal entry, mark as issued, send to buyer, count it in AR ageing or VAT returns | Keep it as "awaiting clearance" |
| Standard, `rejected` | Post or issue it; reuse its number/UUID/ICV | Cancel it and create a new corrected document (new ICV) — OQ-6 |
| Standard, `failed_final` (could not reach ZATCA) | Post or issue it | Await human decision (OQ-7) |
| Standard, `cleared*` | Edit it | Post it (date = issue date), issue the **cleared** XML/QR to buyer |
| Simplified (B2C), `stamped` onward | Edit it | Post and issue at stamping |
| Simplified, `rejected` after issue | Edit or delete the posted entry | Correction per OQ-6 (likely credit note + new invoice via `reverse_entry`) |

## 6. Integrations

All calls go through the integration layer (`integrations/zatca/client.py` — location per integration-layer convention). Tests use a mock client with recorded sandbox-shaped fixtures; no test or agent calls any real ZATCA endpoint or uses real credentials.

| System | ERP → system | System → ERP |
| --- | --- | --- |
| ZATCA Fatoora — compliance CSID API **[CC-verify]** | CSR (base64), OTP | Compliance CSID (binary security token), secret, request ID |
| ZATCA — compliance invoice check API **[CC-verify]** | Sample signed documents per type | Validation results |
| ZATCA — production CSID API (onboarding + renewal) **[CC-verify]** | Compliance request ID (onboard) / new CSR + OTP (renew) | PCSID, secret, request ID |
| ZATCA — clearance API **[CC-verify]** | Signed XML (base64), invoice hash, UUID | Status, cleared XML (ZATCA-stamped), warnings/errors |
| ZATCA — reporting API **[CC-verify]** | Signed XML, invoice hash, UUID | Status, warnings/errors |
| `invoicing` app (internal) | `is_issued`, status, cleared XML, QR, messages | Validated document via `zatca.services.submit_document(doc, user)` |
| Secret store (OQ-3) | Private key, CSID secret (write once) | Same (read at signing/submission only) |
| Banks, GOSI, Mudad, SAP B1 | None | None |

Environments: sandbox (developer portal), simulation, production **[CC-verify]** names and base URLs; URLs are configuration, not code.

Candidate open-source libraries (options, not decisions — OQ-11):

| Need | Options |
| --- | --- |
| Key pair, CSR, ECDSA | `cryptography` (supports secp256k1 **[CC-verify]**), `pyOpenSSL` |
| XML build / C14N | `lxml` (C14N 1.1 support **[CC-verify]** which C14N is required) |
| XML signature | `signxml`, `python-xmlsec` (xmlsec1), or hand-built signed properties on `lxml` + `cryptography` |
| XSD / schematron validation | `lxml`/`xmlschema` for XSD; `lxml.isoschematron` (XSLT 1 only) or Saxon-HE via `saxonche` (MPL; check licence and XSLT 2 need) |
| QR image | `segno`, `qrcode` |
| Reference validation | ZATCA SDK (Java, free, not open source — **[CC-verify]** licence) for offline comparison in CI only, never as a runtime dependency |

## 7. Permissions

Role names are provisional (no role model exists yet; OQ-14). All grants are **per entity**.

| Action | ZATCA admin (entity) | Accountant (entity) | Sales user (entity) | Viewer / auditor | Queue worker (system user) | AI agent (gateway) |
| --- | --- | --- | --- | --- | --- | --- |
| Edit profile, create EGS unit | Yes | No | No | No | No | No |
| Generate CSR, enter OTP, renew, revoke | Yes | No | No | No | No | No |
| Set environment = production | Yes + second approver (OQ-14) | No | No | No | No | No |
| Submit (triggered by issuing in `invoicing`) | Yes | Yes | Yes (as `invoicing` allows) | No | — | No |
| Stamp / send queued items | — | — | — | — | Yes (only) | No |
| Manually retry `failed_*` | Yes | Yes | No | No | No | No |
| View e-invoices, XML, messages | Yes | Yes | Own documents | Yes (read-only) | — | Status only, if granted |
| Download archive / export | Yes | Yes | No | Yes | No | No |

## 8. Acceptance tests

All tests use fixtures, a fake entity pair (`TST1`, `TST2`, made-up VAT numbers), a throwaway in-memory key store, and a mocked ZATCA client. No network.

| # | Given | When | Then |
| --- | --- | --- | --- |
| AT-1 | Entity TST1 with blank `vat_number` | Admin enables its profile | Rejected; message en/ar (R-4) |
| AT-2 | Draft EGS unit for TST1 | Admin generates CSR | Status `csr_ready`; `private_key_ref` set; no key material in DB or logs (R-6) |
| AT-3 | `csr_ready` unit, mock returns compliance CSID | Admin enters OTP | Credential `compliance` active; OTP not stored anywhere; `OnboardingEvent` without OTP (R-7) |
| AT-4 | `compliance_csid` unit, mock passes all sample checks | Run compliance checks | Status `compliance_passed` |
| AT-5 | `compliance_csid` unit, mock fails one sample type | Request PCSID | Rejected; status unchanged (R-8) |
| AT-6 | `draft` unit | Request PCSID directly | Rejected, transition not allowed (R-5) |
| AT-7 | Active unit, credential `valid_to` in 20 days, N=30 | Daily job runs | Status `renewal_due`; admins alerted (R-10) |
| AT-8 | Active unit, credential expired | Stamp a document | Rejected; nothing signed (R-9, R-10) |
| AT-9 | Renewal: mock issues new PCSID | Admin renews | Old credential `superseded`, `replaced_by` set; one active per kind (DB) |
| AT-10 | Active unit, `last_icv`=0 | Stamp first document | ICV=1; PIH = initial value (R-12) |
| AT-11 | Active unit after AT-10 | Stamp second document | ICV=2; PIH = hash of doc 1 |
| AT-12 | Two concurrent stamp calls on same unit | Both commit | ICVs n and n+1, no duplicate, PIH chain intact (R-11) |
| AT-13 | Fixture XML with known expected hash (from ZATCA sample, verified by compliance-checker) | Compute hash | Matches expected (R-13) |
| AT-14 | Stamped simplified doc | Decode QR | Tags 1–9 present with correct values; seller name in Arabic (R-15, R-33) |
| AT-15 | Document failing XSD (missing buyer VAT on B2B) | Submit | Stays `pending`; local error message; `last_icv` unchanged (R-16) |
| AT-16 | TST1 document, EGS unit belongs to TST2 | Stamp | Rejected (R-2) |
| AT-17 | Standard doc, mock clearance returns cleared + cleared XML | Queue runs | Status `cleared`; `cleared_xml` stored; `is_issued` True (R-22) |
| AT-18 | Standard doc, mock returns rejection with 2 errors | Queue runs | Status `rejected`; 2 `ValidationMessage` with en text and ar text/fallback; `is_issued` False; no journal entry exists for the document (R-25, §5) |
| AT-19 | Standard doc, mock returns accepted with warnings | Queue runs | `cleared_with_warnings`; warnings stored; `is_issued` True (R-26) |
| AT-20 | Simplified doc | Stamped | `is_issued` True before reporting; `reporting_deadline` = issue + 24h (R-23) |
| AT-21 | Simplified doc unreported, deadline in 1 h | Alert job runs | Alert raised to TST1 admins only, not TST2 (R-23, R-3) |
| AT-22 | Mock times out 3 times then succeeds | Queue runs with backoff | 4 attempts recorded; same request hash each time; ICV/UUID unchanged (R-24) |
| AT-23 | Mock returns 5xx beyond max attempts | Queue runs | `failed_final`; admins alerted; `is_issued` False for standard |
| AT-24 | Standard doc sent to reporting API (forced) | Submit | Rejected by service (R-21) |
| AT-25 | Cleared `EInvoice` | Update `signed_xml` or `cleared_xml`, or delete | Raises error (R-17, R-18) |
| AT-26 | `SubmissionAttempt` row | Update or delete | Raises error (R-18) |
| AT-27 | Credit note referencing TST2 invoice from TST1 | Submit | Rejected (R-19, R-1) |
| AT-28 | Credit note without reason | Submit | Rejected (R-19) |
| AT-29 | Test settings | Code attempts a real HTTP call to a ZATCA host | Test fails loudly (R-29) |
| AT-30 | Agent service user | Tries to onboard, stamp, submit, retry or set production | Each denied (R-34) |
| AT-31 | User with permission only on TST1 | Lists e-invoices | Sees TST1 only (R-3) |
| AT-32 | Entity with `go_live_date` in future | Document handed over | Not submitted; status explains why (R-28) |
| AT-33 | Line totals ≠ header total by 0.01 | Submit | Rejected locally (R-31) |
| AT-34 | Archive row with `retention_until` in future | Purge job | Row kept (R-30) |
| AT-35 | Clearance queue: ICV 5 still `submitting` | Worker picks ICV 6 | Waits until 5 finishes (R-27) |

## 9. Open questions

**B** = blocks building (or blocks go-live where noted); **D** = safe default exists, build can proceed with it.

| # | Question | Blocks? | Safe default if D |
| --- | --- | --- | --- |
| OQ-1 | Which entities are in which ZATCA integration wave, and their go-live dates? What does `invoicing` do for an entity before its go-live (Phase 1 only)? | D for build, B for go-live | `go_live_date` null → no submission; Phase 1 QR generated by `invoicing` |
| OQ-2 | Build in-house (this spec) vs connect to an external ZATCA middleware/provider through the integration layer? | **B** | — |
| OQ-3 | Secret storage for private keys and CSID secrets: HashiCorp Vault / OpenBao, PostgreSQL with envelope encryption (key from env/KMS), HSM, or OS keyring? Who holds the master key; backup/restore? | **B** | (dev/tests: in-memory fake store only) |
| OQ-4 | How many EGS units per entity (one per entity, per branch, per POS/till, per server)? | D | One EGS unit per entity |
| OQ-5 | Are any group entities registered as a VAT group (shared VAT number)? If so, whose name/VAT appears on invoices and how are EGS units assigned? | **B** if yes; D if all registered separately | Each entity has its own VAT number |
| OQ-6 | Correction path after rejection: for B2B, cancel and re-issue as a new document; for B2C already issued and posted, credit note + new invoice or resubmit corrected? Does a rejected document stay in the PIH chain? **[CC-verify]** | **B** | — |
| OQ-7 | Behaviour when ZATCA is unreachable for B2B clearance for an extended time (wait, or any permitted fallback **[CC-verify]** clearance-off mode)? | D | Wait; never issue uncleared; alert |
| OQ-8 | Retention period (VAT regulations, possibly longer for some asset types **[CC-verify]**), storage location in KSA, and who may approve purging. | D | Keep indefinitely; no purge job enabled |
| OQ-9 | Renewal lead time N and PCSID validity length **[CC-verify]**. | D | N = 30 days |
| OQ-10 | Retry policy limits and alert recipients. | D | R-24 defaults; alert entity ZATCA admins |
| OQ-11 | Library choice for signing, C14N, schematron and QR (§6 options). Is the ZATCA SDK acceptable in CI only? | D | `cryptography` + `lxml` + `segno`; schematron choice deferred to builder with reviewer sign-off |
| OQ-12 | Exact handover contract with `invoicing.md`: model name(s), who assigns the human invoice number, when `submit_document` is called, buyer data fields. | **B** (needs `invoicing.md` final) | — |
| OQ-13 | In-scope document types beyond 388/381/383: prepayment (386), export, third-party, nominal, summary, self-billed invoices? | D | 388/381/383 standard and simplified only |
| OQ-14 | Role model and approvers: who is ZATCA admin per entity; is a second approver needed to switch to production or revoke? | D | Superuser only for onboarding; second approver required for production |
| OQ-15 | Offline B2C behaviour: may a POS/branch issue simplified invoices while offline and report later within 24 h; local stamping on device or central server? | D | Central server stamping only; no offline POS |
| OQ-16 | Arabic translations of ZATCA error codes: who maintains the table? | D | Show English text with Arabic "untranslated" label |
| OQ-17 | Intercompany invoices between group entities (rentals, IP, secondments): standard B2B clearance with counterparty entity as buyer — confirm no special treatment, and VAT-group effect (OQ-5). | D | Treated as normal B2B |
| OQ-18 | Time source and timezone for `issue_datetime` (server NTP, Asia/Riyadh) and tolerance ZATCA accepts **[CC-verify]**. | D | Server UTC stored, Asia/Riyadh rendered |
| OQ-19 | Should the module back-fill Daftra-era invoices? (Likely no; confirm with migration plan.) | D | No back-fill |

**Before building:** compliance-checker verifies every **[CC-verify]** item; OQ-2, OQ-3, OQ-5, OQ-6 and OQ-12 are answered by the Managing Partner / accountant and recorded in §2 with dates.
