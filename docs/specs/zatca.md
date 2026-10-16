# Spec: `zatca` — ZATCA Fatoora Phase 2 e-invoicing (Track A)

**Compliance status: reviewed 2026-10-16 from search excerpts only; official documents not yet read; re-check required before go-live.** (Record: `docs/compliance/track-a-2026-10-16.md`.)

Status: draft for review · Written: 2026-10-16 · Revised: 2026-10-16 (compliance review applied; project-owner decisions D-1 to D-9 recorded) · Author: spec-writer agent
Depends on: `core` (Entity, incl. national-address fields in `invoicing.md` §3.0), `docs/specs/invoicing.md` (AR/AP documents), `docs/specs/ledger-fx.md` (currency, D-6)

> **Verification tag.** Statements marked **[CC-verify]** are ZATCA technical requirements written from memory without access to the official documents. The compliance-checker agent must verify each one against the current official ZATCA publications (E-Invoicing Regulation, Implementing Resolution, XML Implementation Standard, Security Features Implementation Standard, Data Dictionary, Developer Portal / Fatoora API docs) before module-builder starts. Where the official text differs, the official text wins and this spec is amended.
> **[CC-verify: search excerpt 2026-10-16]** marks a finding adopted from the 2026-10-16 compliance review. The reviewer could not open official ZATCA/SDAIA pages (DNS blocked) and worked from search-result excerpts. These items are **not verified** and must be re-checked against the official text like any other [CC-verify] item.

---

## 1. Purpose

The `zatca` app is the e-invoicing layer between the `invoicing` app and ZATCA's Fatoora platform, for **every** VAT-registered entity in the group (Almiras and any subsidiary added as an `Entity` row). For each entity it onboards one or more EGS (e-invoice generation solution) units, holds their certificate metadata, and for each tax document handed over by `invoicing` it generates UBL 2.1 XML, assigns the invoice counter (ICV) and previous-invoice hash (PIH), computes the invoice hash, applies the cryptographic stamp, builds the QR code, and submits the document — **clearance** for standard (B2B) tax invoices and their notes, **reporting** for simplified (B2C) invoices and their notes. It stores what was sent and what ZATCA returned, surfaces errors and warnings in English and Arabic, and keeps the archive for the retention period. It does not create business documents and does not post journal entries.

## 2. Decisions

Binding from `CLAUDE.md`: open source only; outside systems only through the integration layer; no real credentials or production data for agents or tests; every record has an `entity` FK; bilingual text; logic in `services.py`; spec before code.

| # | Date | Who | Decision |
| --- | --- | --- | --- |
| — | 2026-10-16 | Caller (task brief) | Scope: e-invoicing layer only. Business documents are in `invoicing.md`. Tests use mocks/fixtures of the ZATCA sandbox/simulation only. This module creates no journal entries. (Secret storage: now D-3.) |
| D-1 | 2026-10-16 | Project owner | Build in-house with open-source libraries. Not yet live on Phase 2; must be ready. Go-live date still open (OQ-1), non-blocking for build. |
| D-2 | 2026-10-16 | Project owner | No entity is in a VAT group: each entity is a separate VAT taxpayer. **Confirmed:** each subsidiary is its own company with its own VAT number and its own EGS unit(s); partly-owned and minority-stake entities are also separate taxpayers issuing their own invoices. Intercompany invoices are normal VAT invoices (standard, clearance) with the counterparty entity carried. |
| D-3 | 2026-10-16 | Project owner | Secret store: **OpenBao** (open source), hosted in KSA. The Managing Partner holds the master key and can delegate access to named key custodians. Delegation is **per entity, logged and revocable** (R-41). The ERP stores references only. |
| D-4 | 2026-10-16 | Project owner | A rejected invoice is corrected and re-issued under a new number; the rejected number stays used (R-42). |
| D-5 | 2026-10-16 | Project owner | All hosting in KSA: DB, archive, backups, logs, secret store (R-43). PDPL offshore processors still need legal sign-off (invoicing OQ-31). |
| D-6 | 2026-10-16 | Project owner | Multi-currency invoices are in scope (invoicing D13, `ledger-fx.md`); VAT amounts for ZATCA are also expressed in SAR (R-40). |
| D-7 | 2026-10-16 | Project owner | Almiras: one branch (Jeddah), one EGS unit; identity and national address as in `invoicing.md` §3.0 (setup data only, never fixtures); VAT returns quarterly. |
| D-8 | 2026-10-16 | Project owner | Sales posting is automatic after CFO approval and ZATCA clearance/reporting (invoicing D17): `invoicing` posts only on `cleared*` / `reported*`, for both categories (§5). |
| D-9 | 2026-10-16 | Project owner | Safe defaults in §9 not contradicted by D-1 to D-8 are approved: OQ-4 (refined by D-7), OQ-7 (expert sign-off still open), OQ-8, OQ-9, OQ-10, OQ-11, OQ-13, OQ-14, OQ-15, OQ-16, OQ-18, OQ-19, OQ-21. |

Everything else is listed in §9 Open questions. The 2026-10-16 compliance review is a technical input, not a business decision; its proposals that need approval (e.g. data residency) are in §9.

## 3. Data model

All models: `entity = FK(core.Entity, on_delete=PROTECT)`, required. `created_at`, `created_by`, `source` (human / import / agent / system, per `docs/ai-agent-gateway.md`) on every model; omitted from tables below.

### 3.1 `ZatcaProfile` — one per entity (taxpayer settings)

| Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- |
| entity | OneToOne(Entity) | Y | — | One profile per entity |
| vat_number | read from `Entity.vat_number` | Y | — | Not duplicated; profile cannot be enabled if blank. Format **[CC-verify]**: 15 digits, starts and ends with 3 |
| legal name en/ar | read from `Entity.legal_name_en/_ar` | Y | Y | Seller name on XML/QR; not duplicated (D-7) |
| seller_id_scheme / seller_id_value | Char | Y | — | Default CRN = `Entity.cr_number`; `700` = `Entity.unified_number` is the alternative (OQ-24) **[CC-verify]** allowed schemes |
| address | read from `Entity` national-address fields (`invoicing.md` §3.0) | Y | Y (names) | Not duplicated (OQ-12 hand-off). Required set **[CC-verify]** |
| vat_group | *Withdrawn — see Decision D-2* (no VAT groups) | | | |
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
| last_invoice_hash | Char(64+), | Y | — | PIH for next document. Initial value **[CC-verify]**: base64 of SHA-256 of the string "0". The sandbox skips the PIH check on single-XML validation, so a sandbox pass does not prove this seed |
| private_key_ref | Char | Y once `csr_ready` | — | **Reference** to an OpenBao path, never the key (D-3), e.g. `zatca/<entity.code>/<egs.code>/private_key` |

Constraint: unique (`entity`, `code`).

### 3.3 `EgsCredential` — compliance CSID or production CSID (PCSID)

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| entity | FK(Entity) | Y | Must equal `egs_unit.entity` |
| egs_unit | FK(EgsUnit) | Y | |
| kind | Choice: `compliance`, `production` | Y | |
| environment | Choice: `sandbox`, `simulation`, `production` | Y | Environment that issued it; usable only there (R-36) |
| certificate_pem | Text | Y | Public certificate; not secret |
| secret_ref | Char | Y | Reference (OpenBao path + version) to the CSID API secret (D-3) |
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
| type_code | Choice: `388` invoice, `381` credit note, `383` debit note, `386` prepayment invoice (R-39) **[CC-verify]** | Y | |
| subtype_code | Char(7) | Y | e.g. `0100000` standard / `0200000` simplified, plus flags (third-party, nominal, export, summary, self-billed) — meaning of positions 4–7 **[CC-verify]** |
| billing_reference | FK(EInvoice), null | Y for 381/383 | Original invoice; reason `reason_ar` required, `reason_en` optional (R-19) **[CC-verify]** |
| prepayment_references | M2M(EInvoice, type 386), same entity | N | On a 388 that settles prepayments (R-39) |
| issue_datetime | DateTime (Asia/Riyadh) | Y | |
| icv | PositiveBigInteger | Y after stamping | Unique per `egs_unit` |
| pih | Char | Y after stamping | |
| invoice_hash | Char | Y after stamping | base64 SHA-256 **[CC-verify]** |
| signed_xml | Text / file | Y after stamping | Immutable once written |
| qr_tlv | Text (base64) | Y after stamping | |
| cleared_xml | Text / file, null | Y when cleared | XML returned by ZATCA (carries ZATCA stamp/QR) **[CC-verify]** |
| status | Choice: `pending`, `stamped`, `queued`, `submitting`, `cleared`, `cleared_with_warnings`, `reported`, `reported_with_warnings`, `rejected`, `failed_retryable`, `failed_final` | Y | See R-20 |
| reporting_deadline | DateTime, null | Y for simplified | `issue_datetime + 24h` **[CC-verify: search excerpt 2026-10-16]** |
| retention_class | Choice: `standard`, `capital_asset_real_estate` | Y | Supplied by `invoicing` (R-38) |
| retention_until | Date | Y | Computed per record by R-38; child records (attempts, messages) inherit it |

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

`entity`, `egs_unit`, `action` (csr_generated, otp_entered, otp_rejected, compliance_csid_issued, compliance_checks_passed, pcsid_issued, pcsid_renewed, revoked, environment_changed, delegation_granted, delegation_revoked), `actor`, `at`, `detail` JSON (never secrets, never OTP values). Append-only.

### 3.8 `ComplianceTask` (human follow-up)

| Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- |
| entity | FK(Entity) | Y | — | = `einvoice.entity` |
| einvoice | FK(EInvoice) | Y | — | |
| kind | Choice: `late_reporting_notice` | Y | — | R-35 |
| title_en / title_ar, instructions_en / instructions_ar | Char / Text | Y | Y | From a template |
| status | Choice: `open`, `done` | Y | — | |
| notified_at | DateTime, null | Y when done | — | When the entity notified ZATCA |
| notice_reference | Char, null | Y when done | — | Reference/acknowledgement from the ZATCA form |
| evidence | File, null | N | — | Screenshot / PDF of the submitted form |
| done_by | FK User, null | Y when done | — | |

Cannot be deleted; `done` is final.

### 3.9 `KeyCustodianDelegation` (D-3; register of who may access an entity's ZATCA secrets)

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| entity | FK(Entity) | Y | Delegation is per entity, never group-wide |
| custodian | FK User | Y | Named person |
| scope | Choice: `onboard_renew` (generate CSR, write key/secret), `revoke`, `restore` | Y | |
| granted_by | FK User | Y | The Managing Partner (or as OQ-25 decides) |
| granted_at / valid_until | DateTime / DateTime, null | Y / N | |
| openbao_policy_ref | Char | Y | Name of the OpenBao policy/entity alias granted; no token or secret |
| revoked_at / revoked_by / revoke_reason | DateTime / FK User / Char | N | Set once; revocation is final |

Append-only except the single revocation write. Each grant and revocation also writes an `OnboardingEvent`-style audit row (`delegation_granted`, `delegation_revoked`).

**What the ERP stores about secrets (D-3):** only OpenBao paths and versions (`private_key_ref`, `secret_ref`), policy names (`openbao_policy_ref`), the OpenBao address (configuration) and the ERP's own OpenBao auth role name (configuration). Never keys, CSID secrets, OTPs, tokens, unseal keys or the master key.

## 4. Rules

Enforcement column: **S** = `zatca/services.py`; **M** = model `save`/`delete` guard; **DB** = database constraint; **Q** = queryset/permission filter; **T** = test settings guard.

| # | Rule | Enforced by |
| --- | --- | --- |
| R-1 | Every `zatca` record has an `entity`; all child records (credential, e-invoice, attempt, message) have the same entity as their parent. | M (`clean`) + S |
| R-2 | An EGS unit signs only documents of its own entity. An entity's documents are never signed with another entity's EGS key or credential, even inside a VAT group. | S (`stamp_document`) |
| R-3 | All views/APIs list `zatca` records filtered by entity and user permission. | Q |
| R-4 | A profile cannot be enabled while `Entity.vat_number` or a required address field is blank. | S |
| R-5 | EGS status transitions only along: draft → csr_ready → compliance_csid → compliance_passed → active ⇄ renewal_due → expired; any → revoked. No skipping. | S |
| R-6 | A key pair is generated inside the ERP per EGS unit; the private key is written only to the secret store (OpenBao, D-3) and the DB holds only `private_key_ref`. Private keys, CSID secrets and OTPs never appear in the DB, logs, `OnboardingEvent.detail`, error messages or fixtures. | S + test that scans logs/DB dumps |
| R-7 | The OTP is entered by a human with the onboarding permission; it is used once and not stored. OTP validity is about 1 hour **[CC-verify: search excerpt 2026-10-16]** (shown as a hint on the form). If ZATCA rejects the OTP as expired or invalid, the request fails, EGS status is unchanged, an `otp_rejected` event is logged (no OTP value), a bilingual message asks for a new OTP, and the ERP never resubmits the same OTP. | S |
| R-8 | An EGS unit becomes `active` only after all required compliance checks for its supported document types pass **[CC-verify]** (set of sample documents per type) and a PCSID is issued. | S |
| R-9 | A document can be stamped only by an `active` EGS unit holding a non-expired production credential (or the matching compliance credential in `compliance_check` mode). | S |
| R-10 | `renewal_due` is set N days before `valid_to` (N = OQ-9, safe default 30). An expired credential blocks stamping; nothing is silently signed with an expired certificate. | S (scheduled job) |
| R-11 | ICV is assigned under a row lock on `EgsUnit` (`select_for_update`): strictly increasing by 1 per EGS unit, no gaps, no reuse. | S + DB unique (`egs_unit`,`icv`) |
| R-12 | PIH of each document equals `invoice_hash` of the previous document stamped by the same EGS unit; the first document uses the initial PIH value **[CC-verify]**. ICV and PIH are assigned in the same transaction as the hash. The sandbox skips the PIH check on single-XML validation **[CC-verify: search excerpt 2026-10-16]**, so a sandbox pass is not evidence that the seed is right; AT-10 uses the value confirmed from the official standard. | S |
| R-13 | Invoice hash = SHA-256 over the canonicalised XML excluding the UBLExtensions, QR and Signature elements, base64-encoded **[CC-verify]** (exact exclusions and canonicalisation method). | S |
| R-14 | Stamp = ECDSA signature over the invoice hash with the EGS private key (curve secp256k1 **[CC-verify]**), embedded per the ZATCA signature profile (XAdES-style signed properties **[CC-verify]**). | S |
| R-15 | QR = base64 TLV with tags 1 seller name, 2 VAT number, 3 timestamp, 4 total incl. VAT, 5 VAT total, 6 invoice hash, 7 ECDSA signature, 8 public key, 9 (simplified only) certificate signature **[CC-verify]**. Mandatory on simplified documents; for standard documents the QR on the cleared XML returned by ZATCA is the one used **[CC-verify]**. Length limit: R-37. | S |
| R-16 | Before submission, the XML is validated locally against the UBL 2.1 XSD and ZATCA business rules (schematron) **[CC-verify]**; any local error stops the document at `pending` with `ValidationMessage(origin=local_*)` and consumes **no** ICV. | S |
| R-17 | Once `signed_xml` is written it is never modified; once `cleared_xml` is written it is never modified. Corrections are new documents (credit/debit note) created in `invoicing`, never edits. | M |
| R-18 | `EInvoice` rows with status other than `pending` cannot be deleted. `SubmissionAttempt`, `ValidationMessage`, `OnboardingEvent` cannot be updated or deleted. | M (+ DB trigger later, as for ledger) |
| R-19 | Credit and debit notes must reference an existing `EInvoice` of the same entity and carry a reason; the Arabic reason is required, English optional (aligned with `invoicing.md` R18/R19) **[CC-verify]**. The category (standard/simplified) follows the original invoice. | S |
| R-20 | EInvoice status flow: pending → stamped → queued → submitting → {cleared, cleared_with_warnings, reported, reported_with_warnings, rejected, failed_retryable → queued, failed_final}. No other transitions. | S |
| R-21 | Standard (B2B) documents use the clearance API; simplified (B2C) documents use the reporting API. A document never goes to the wrong API. | S |
| R-22 | A standard document is **issued** only when status is `cleared` or `cleared_with_warnings`. Until then `zatca.services.is_issued(doc)` returns False and the buyer copy (PDF/XML) cannot be generated or sent. | S (+ contract test with `invoicing`) |
| R-23 | A simplified document is issued at stamping (signed XML + QR) and must be reported within 24 h of `issue_datetime` **[CC-verify: search excerpt 2026-10-16]**. A document still unreported 2 h (configurable) before `reporting_deadline` raises an alert to the entity's ZATCA admins. Missed deadline: R-35. | S (scheduled job) |
| R-24 | Transient failures (network error, timeout, HTTP 429/5xx) → `failed_retryable`, retried with exponential backoff (default: 1 min, doubling, cap 1 h, max 10 attempts for standard documents — safe default, OQ-10). Simplified documents are **never** moved to `failed_final` for transient errors: after the max attempts they keep retrying at the cap interval until reported **[CC-verify: search excerpt 2026-10-16]**. Every attempt is an append-only `SubmissionAttempt` row; these rows are the evidence of reporting attempts. Retries resend the **same** signed XML with the same ICV/PIH/UUID; never re-stamp. | S |
| R-25 | ZATCA rejection (validation errors) → `rejected`; no automatic retry. Errors stored as `ValidationMessage` in en (as received) and ar (from table). | S |
| R-26 | Accepted-with-warnings stores warnings and counts as cleared/reported; warnings are shown to users in both languages. | S |
| R-27 | Submission is serialised per EGS unit in ICV order for clearance; a document is not submitted while an earlier ICV of the same unit is still `queued`/`submitting` **[CC-verify]** whether ZATCA requires ordering. | S (queue worker) |
| R-28 | Documents of an entity whose `go_live_date` is null or in the future are not submitted; `invoicing` must treat them per pre-Phase-2 rules (OQ-1). | S |
| R-29 | Only the integration layer's ZATCA client performs HTTP calls. In test settings the client is replaced by a mock; any attempt to reach a real ZATCA host from tests fails. `environment=production` cannot be set when `settings.TESTING` or an agent service user is the actor. | S + T |
| R-30 | Archive (signed XML, cleared XML, responses, attempts, messages) is kept until `retention_until` (R-38) and cannot be purged earlier. No purge job is enabled (OQ-8). Storage location: KSA (D-5, R-43). | S + M |
| R-31 | Amounts in XML are taken from the `invoicing` document as `Decimal`. VAT per category is computed at **document level** (round(taxable amount per category × rate, 2)) by `invoicing`; line VAT is for display only **[CC-verify: search excerpt 2026-10-16]**. Locally the module rejects a document whose header net ≠ sum of line net amounts, whose category VAT ≠ round(category taxable amount × rate), or whose line VAT deviates from the document VAT beyond the tolerance in ZATCA's BR-KSA rules **[CC-verify]**. Never floats. | S |
| R-32 | Sharia: this module does not validate Sharia compliance; it accepts only documents that `invoicing` has already validated (including Sharia checks). It has no path to issue a document that bypasses `invoicing` services. | S (single entry point `submit_document`) |
| R-33 | Bilingual: seller name/address in Arabic are always present in XML; buyer-facing invoice output (PDF in `invoicing`) is Arabic, optionally with English **[CC-verify]** Arabic requirement. All statuses, errors and UI labels have en/ar. | S + translations |
| R-34 | AI agents (gateway service users) may view statuses and draft nothing in this module; they cannot onboard, enter OTPs, stamp, submit, or retry. | Q (permissions) |
| R-35 | When `reporting_deadline` passes and a simplified document is not `reported*`, the system opens one `ComplianceTask(kind=late_reporting_notice)` for the entity's ZATCA admins (en/ar): the entity must notify ZATCA through the form on ZATCA's website **[CC-verify: search excerpt 2026-10-16]**. The task closes only with `notified_at`, `notice_reference` and `done_by`. Retries (R-24) continue regardless. | S (scheduled job) + M |
| R-36 | A credential is usable only in the environment that issued it: sandbox/simulation CSIDs work only in their environment **[CC-verify: search excerpt 2026-10-16]**; stamping/submitting requires `credential.environment == profile.environment`. Changing environment requires new onboarding. | S |
| R-37 | The QR base64 string is at most 700 characters (Security Features Std v1.2; an older guide said 500) **[CC-verify: search excerpt 2026-10-16]**. Longer → stamping transaction rolls back (no ICV consumed), bilingual error. | S |
| R-38 | `retention_until` is computed per record: end of the tax period containing `issue_datetime` + at least 6 years for `standard`; + 15 years (cited) for `capital_asset_real_estate` **[CC-verify: search excerpt 2026-10-16]**. `invoicing` sets the class to `capital_asset_real_estate` when any line uses account 1510 (investment property) or the document is rent on investment property (default per OQ-8). A record's `retention_until` is never shortened. | S + M |
| R-39 | Type 386 prepayment invoices are in scope, standard and simplified, submitted like 388. A 388 that settles prepayments references its 386 documents (same entity, same buyer) and shows the prepaid amounts and VAT as deducted **[CC-verify]** (fields and BR-KSA rules). Produced only by the `invoicing` advances flow (invoicing R65, D15). | S |
| R-40 | Documents may be in any currency (D-6). The XML carries amounts in the document currency and, when it is not SAR, the VAT total also in SAR, taken from `invoicing` (`total_vat_sar`); the SAR amount is never recomputed here. The exact ZATCA rule (second TaxTotal in SAR, rate representation, QR tags 4/5 currency) is **[CC-verify]**. A non-SAR document without a SAR VAT amount is rejected locally (no ICV consumed). | S |
| R-41 | Onboarding, renewal, revocation and restore actions for an entity need, besides the §7 role, an active `KeyCustodianDelegation` for that entity and scope. The ERP's runtime signing identity in OpenBao reads only the paths of entities it signs for. Grants and revocations are logged; a revoked delegation blocks the next action at once. | S + OpenBao policy |
| R-42 | After `rejected`, the corrected document is a new `EInvoice` for a new `invoicing` document with a new number, UUID and ICV; the rejected number and UUID are never reused (D-4). Whether the rejected document's hash stays in the PIH chain follows the official standard **[CC-verify]**; until verified the chain continues from the last stamped document, including rejected ones. | S |
| R-43 | Database, archive, backups, logs and the secret store run in KSA (D-5). Configuration pointing any of them to a non-KSA host is a deployment error caught by the release checklist. | Process + code-reviewer |

## 5. Accounting

**This module creates no journal entries.** It never calls `ledger.services.post_entry`, `create_entry` or `reverse_entry`. Posting is done by `invoicing` per `invoicing.md` (accounts there, e.g. 1200 Trade receivables, 2200 VAT output, from `ledger/coa_template.py`).

What `invoicing` must and must not do based on this module's status:

| Case | `invoicing` must not | `invoicing` may |
| --- | --- | --- |
| Standard (B2B) invoice/note, status before `cleared*` | Post the journal entry, mark as issued, send to buyer, count it in AR ageing or VAT returns | Keep it as "awaiting clearance" |
| Standard, `rejected` | Post or issue it; reuse its number/UUID/ICV | Create a new corrected document under a new number (new UUID, ICV); the rejected number stays used (D-4, R-42) |
| Standard, `failed_final` (could not reach ZATCA) | Post or issue it | Await human decision (OQ-7) |
| Standard, `cleared*` | Edit it | Post it (date = issue date), issue the **cleared** XML/QR to buyer |
| Simplified (B2C), `stamped` / `queued` / `submitting` | Edit it; post the journal entry (D-8) | Give the stamped document to the buyer (issued at stamping, R-23) |
| Simplified, `reported*` | Edit it | Post it (D-8) |
| Simplified, `rejected` after hand-over to the buyer | Edit it; post its draft entry | Re-issue a corrected document under a new number (D-4); no GL effect to reverse since nothing was posted. Whether the buyer copy also needs a credit note **[CC-verify]** (OQ-6 remainder) |
| 386 prepayment | Treat differently from 388 for issue/post timing | Same rows as above by category; its accounts are set in `invoicing.md` (OQ-16, OQ-22 there) |

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
| Secret store: OpenBao, hosted in KSA (D-3, D-5) | Private key, CSID secret (write once, under custodian delegation R-41) | Same (read at signing/submission only, by the ERP signing identity) |
| `ledger-fx` (via `invoicing`) | None | SAR VAT amounts on non-SAR documents (R-40) |
| ZATCA website — late-reporting notification form **[CC-verify: search excerpt 2026-10-16]** | None automated: filed manually by the entity's ZATCA admin (R-35) | Reference/screenshot uploaded to `ComplianceTask` |
| Banks, GOSI, Mudad, SAP B1 | None | None |

Environments: sandbox (developer portal), simulation, production **[CC-verify]** names and base URLs; URLs are configuration, not code.

Open-source libraries (in-house build, D-1; default set approved by D-9 / OQ-11):

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
| Close a `late_reporting_notice` task | Yes | No | No | View only | Opens it only | No |
| Key actions (CSR/key write, renew, revoke, restore) | Only with an active `KeyCustodianDelegation` for the entity and scope (R-41) | No | No | No | No | No |
| Grant / revoke key custodian delegation | Managing Partner only (holds the master key, D-3) | No | No | No | No | No |

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
| AT-10 | Active unit, `last_icv`=0 | Stamp first document | ICV=1; PIH = initial value confirmed from the official standard, not from a sandbox pass (R-12) |
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
| AT-23 | Standard doc; mock returns 5xx beyond max attempts | Queue runs | `failed_final`; admins alerted; `is_issued` False |
| AT-24 | Standard doc sent to reporting API (forced) | Submit | Rejected by service (R-21) |
| AT-25 | Cleared `EInvoice` | Update `signed_xml` or `cleared_xml`, or delete | Raises error (R-17, R-18) |
| AT-26 | `SubmissionAttempt` row | Update or delete | Raises error (R-18) |
| AT-27 | Credit note referencing TST2 invoice from TST1 | Submit | Rejected (R-19, R-1) |
| AT-28 | Credit note without reason, or with English reason only | Submit | Rejected (R-19) |
| AT-29 | Test settings | Code attempts a real HTTP call to a ZATCA host | Test fails loudly (R-29) |
| AT-30 | Agent service user | Tries to onboard, stamp, submit, retry or set production | Each denied (R-34) |
| AT-31 | User with permission only on TST1 | Lists e-invoices | Sees TST1 only (R-3) |
| AT-32 | Entity with `go_live_date` in future | Document handed over | Not submitted; status explains why (R-28) |
| AT-33 | (a) 3 lines net 10.03 S 15%: line VAT 1.50 each (sum 4.50), category VAT 4.51; (b) same with category VAT 4.50; (c) header net 30.10 vs line sum 30.09 | Submit | (a) passes local checks if within the BR-KSA tolerance **[CC-verify]** (fixture value set once verified); (b) rejected: category VAT ≠ round(30.09 × 15%); (c) rejected; no ICV consumed for (b), (c) (R-31) |
| AT-34 | Archive row with `retention_until` in future | Purge job | Row kept (R-30) |
| AT-35 | Clearance queue: ICV 5 still `submitting` | Worker picks ICV 6 | Waits until 5 finishes (R-27) |
| AT-36 | Simplified TST1 doc; mock times out on every call | Clock passes `reporting_deadline` | Status not `failed_final`; retries continue at cap; one `SubmissionAttempt` per try; one open `late_reporting_notice` task for TST1 admins (none for TST2); closing it without `notified_at`/`notice_reference` rejected (R-24, R-35) |
| AT-37 | `csr_ready` unit; mock returns "OTP expired/invalid" | Admin enters OTP | Status stays `csr_ready`; `otp_rejected` event without OTP value; en/ar message; no automatic resubmission (R-7) |
| AT-38 | Profile `production`; only a `sandbox` credential active | Stamp / submit | Rejected (R-36) |
| AT-39 | Seller name long enough that QR base64 = 701 chars | Stamp | Rejected; transaction rolled back; `last_icv` unchanged. 700 chars passes (R-37) |
| AT-40 | Issue date 2027-03-10, monthly tax period; doc A lines on 4200; doc B line on 1510 | Stamp | A: `retention_until` = 2033-03-31; B: class `capital_asset_real_estate`, 2042-03-31 (R-38) |
| AT-41 | Cleared 386 for buyer X in TST1 | Final 388 references it; then a 388 in TST2 references it | First: XML carries prepayment reference and deduction; second rejected (R-39, R-1) |
| AT-42 | TST1 USD invoice with `total_vat_sar` 562.50; a second USD invoice without it | Stamp | First: XML has VAT total in USD and in SAR 562.50 (shape per verified rule); second rejected locally, `last_icv` unchanged (R-40) |
| AT-43 | Admin A with delegation `onboard_renew` for TST1 only; then the delegation is revoked | A generates CSR for TST1, then for TST2; after revocation A renews TST1 | TST1 CSR allowed; TST2 rejected; after revocation rejected; `delegation_granted` / `delegation_revoked` events exist with no secret values (R-41, D-3) |
| AT-44 | Standard invoice INV-1 rejected by the mock | `invoicing` creates the correction | New document with new number, UUID and ICV; INV-1's number and UUID never reused (R-42) |
| AT-45 | TST1 with full national address on `Entity`, profile enabled | Stamp a document | Seller name/address in XML come from `Entity` §3.0 fields in ar (and en where allowed); no address fields exist on `ZatcaProfile` (3.1) |
| AT-46 | Intercompany standard invoice TST1 → TST2 (TST2 as buyer) | Submit | Sent for clearance as a normal B2B invoice with TST2's VAT number as buyer; signed with TST1's EGS only (D-2, R-2) |

## 9. Open questions

**B** = blocks building (or blocks go-live where noted); **D** = safe default exists, build can proceed with it.

| # | Question | Blocks? | Safe default if D |
| --- | --- | --- | --- |
| OQ-1 | Which entities are in which ZATCA integration wave, and their go-live dates? What does `invoicing` do for an entity before its go-live (Phase 1 only)? | D for build, **B for go-live** | Default approved (D-9): `go_live_date` null → no submission; Phase 1 QR generated by `invoicing`. Almiras not yet live; must be ready (D-1); date open |
| OQ-2 | Build in-house (this spec) vs connect to an external ZATCA middleware/provider through the integration layer? | — | **Resolved by D-1**: in-house, open source |
| OQ-3 | Secret storage for private keys and CSID secrets; who holds the master key; backup/restore? | D for build, **B for go-live** (remainder) | **Resolved by D-3**: OpenBao in KSA, Managing Partner master key, per-entity delegation (3.9, R-41). Still open: unseal model (sole holder vs Shamir shares among custodians vs KSA-hosted auto-unseal) so a restart does not stop signing and B2C 24 h reporting while the Managing Partner is unavailable; OpenBao backup/restore and DR test. Dev/tests: in-memory fake store only |
| OQ-4 | How many EGS units per entity (one per entity, per branch, per POS/till, per server)? | D | Resolved by D-7 / D-9: Almiras 1 (one branch); other entities default one per branch (invoicing OQ-35) |
| OQ-5 | Are any group entities registered as a VAT group (shared VAT number)? | — | **Resolved by D-2**: no VAT group; each entity (incl. associates and minority holdings) has its own VAT number and EGS unit(s) |
| OQ-6 | Correction path after rejection; does a rejected document stay in the PIH chain? **[CC-verify]** | D | **Resolved by D-4**: corrected and re-issued under a new number; rejected number stays used (R-42). Remainder **[CC-verify]**: PIH chain treatment (default R-42) and whether a delivered simplified copy also needs a credit note |
| OQ-7 | Behaviour when ZATCA is unreachable for B2B clearance for an extended time. A guideline excerpt suggests a fallback may exist **[CC-verify: search excerpt 2026-10-16]**. **Needs expert sign-off** (tax adviser). | D | Default approved (D-9): wait; never issue uncleared; alert. Expert sign-off still open |
| OQ-8 | Retention: confirm 6 years / 15 years and the start point **[CC-verify: search excerpt 2026-10-16]**; which records are `capital_asset_real_estate`; who may approve purging. | D | Default approved (D-9): R-38; no purge job. Tax-period frequency now held on `Entity.vat_return_period` (Almiras quarterly, D-7) |
| OQ-9 | Renewal lead time N and PCSID validity length **[CC-verify]**. | D | N = 30 days |
| OQ-10 | Retry policy limits and alert recipients. | D | R-24 defaults; alert entity ZATCA admins |
| OQ-11 | Library choice for signing, C14N, schematron and QR (§6 options). Is the ZATCA SDK acceptable in CI only? | D | `cryptography` + `lxml` + `segno`; schematron choice deferred to builder with reviewer sign-off |
| OQ-12 | Exact handover contract with `invoicing.md`: model name(s), who assigns the human invoice number, when `submit_document` is called, buyer data fields; ownership of ICV/PIH/hash/QR fields (invoicing OQ-2). | **B** | — (seller data now settled: read from `Entity`, D-7) |
| OQ-13 | 386 prepayment is now in scope (R-39), linked to `invoicing.md` advances (OQ-16 there). Still open: export, third-party, nominal, summary, self-billed invoices. | D | 388/381/383/386 standard and simplified only |
| OQ-14 | Role model and approvers: who is ZATCA admin per entity; is a second approver needed to switch to production or revoke? | D | Superuser only for onboarding; second approver required for production |
| OQ-15 | Offline B2C behaviour: may a POS/branch issue simplified invoices while offline and report later within 24 h; local stamping on device or central server? | D | Central server stamping only; no offline POS |
| OQ-16 | Arabic translations of ZATCA error codes: who maintains the table? | D | Show English text with Arabic "untranslated" label |
| OQ-17 | Intercompany invoices between group entities: confirm no special treatment. | — | **Resolved by D-2**: normal B2B clearance, counterparty entity as buyer (AT-46) |
| OQ-18 | Time source and timezone for `issue_datetime` (server NTP, Asia/Riyadh) and tolerance ZATCA accepts **[CC-verify]**. | D | Server UTC stored, Asia/Riyadh rendered |
| OQ-19 | Should the module back-fill Daftra-era invoices? (Likely no; confirm with migration plan.) | D | No back-fill |
| OQ-20 | Data residency. | — | **Resolved by D-5**: DB, archive, backups, logs and secret store in KSA (R-43). PDPL offshore processors: legal sign-off still open (invoicing OQ-31, go-live) |
| OQ-21 | Late-reporting notice (R-35): who files the ZATCA website form per entity, and what evidence is kept. | D | Default approved (D-9) |
| OQ-22 | ZATCA rule for VAT in SAR on non-SAR invoices (R-40) **[CC-verify]**. | D | SAR VAT total from `invoicing`; local rejection if missing |
| OQ-23 | *Withdrawn — see Decision D-2* (subsidiaries' own VAT numbers and EGS units: confirmed). | — | — |
| OQ-24 | Seller ID scheme on XML: `CRN` (branch CR) or `700` (unified number) **[CC-verify]** (invoicing OQ-33). | D | `CRN` from `Entity.cr_number` |
| OQ-25 | Who may grant delegations if the Managing Partner is unavailable; maximum delegation length. | D | Managing Partner only; no expiry unless `valid_until` set |

**Before building:** compliance-checker verifies every **[CC-verify]** item against the official documents (the 2026-10-16 review used search excerpts only); OQ-12 (hand-off contract with `invoicing.md`) is the only remaining build blocker. **Before go-live:** OQ-1 (go-live date), OQ-3 remainder (OpenBao unseal/backup), expert sign-off on OQ-7, PDPL legal sign-off (invoicing OQ-31).
