# Spec: `zatca` — ZATCA Fatoora Phase 2 e-invoicing (Track A)

**Compliance status: Technical rules checked 2026-10-16 against official ZATCA documents (see docs/compliance/zatca-verification-2026-10-16.md); SDK-confirm and expert items remain.** (Earlier record: `docs/compliance/track-a-2026-10-16.md`.)

Status: draft for review · Written: 2026-10-16 · Revised: 2026-10-16 (compliance review applied; project-owner decisions D-1 to D-9 recorded) · Revised: 2026-10-16 (ZATCA verification report Z1–Z43 applied) · Revised: 2026-10-16 (project-owner decisions D-10, D-11) · Author: spec-writer agent
Depends on: `core` (Entity, incl. national-address fields in `invoicing.md` §3.0), `docs/specs/invoicing.md` (AR/AP documents), `docs/specs/ledger-fx.md` (currency, D-6)

> **Tags.** A citation in parentheses, e.g. "(XML v1.2 BR-KSA-40, p.56)", points to an official ZATCA document checked on 2026-10-16; abbreviations (XML, DD, SFIS, DTG, DG, IR, DPM, TIR, PSG, WHT, QRG) are listed in the Sources table of `docs/compliance/zatca-verification-2026-10-16.md`.
> **[SDK-confirm]** = technical point the official documents leave open or contradict; the builder settles it against the ZATCA SDK / sandbox before building that part, and this spec is amended.
> **[Needs expert sign-off: tax|legal|security|Sharia]** = adviser decision needed; the safe default stands until then.
> No [CC-verify] tags remain in this spec.

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
| D-10 | 2026-10-16 | Project owner | Master key (OQ-3 unseal model): the master key **stays with the Managing Partner**, who may delegate key access to named custodians (D-3, `KeyCustodianDelegation`). **Shamir key shares are not adopted.** The availability risk is **accepted by the owner**: after a restart the sealed store blocks signing and submission until unsealed, which can stop B2C invoicing and endanger the 24 h simplified-reporting deadline (R-23). Mitigation: custodians to whom the MP delegates it may perform the unseal (scope `unseal`, R-49). The security sign-off on "non-exportable" keys (R-6, OQ-3 b) stays open. |
| D-11 | 2026-10-16 | Project owner | Seller ID (OQ-24 / invoicing OQ-33): use the CR number (scheme `CRN`; Almiras: Jeddah branch CR, `invoicing.md` §3.0) **and** also show the unified national number (starts with 7). BR-KSA-08 allows exactly one seller identification in the XML, so the XML carries `CRN` per the priority order and the human-readable invoice shows both, labelled en/ar (R-48; invoicing R72). |

Everything else is listed in §9 Open questions. The 2026-10-16 compliance review and the 2026-10-16 verification against official ZATCA documents are technical inputs, not business decisions; no D-n above was changed by them. Where the verification affects a decision's scope (D-4 via OQ-6 re-issue date; D-5 via OQ-20 VAT residency), the affected rule/OQ says so.

## 3. Data model

All models: `entity = FK(core.Entity, on_delete=PROTECT)`, required. `created_at`, `created_by`, `source` (human / import / agent / system, per `docs/ai-agent-gateway.md`) on every model; omitted from tables below.

### 3.1 `ZatcaProfile` — one per entity (taxpayer settings)

| Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- |
| entity | OneToOne(Entity) | Y | — | One profile per entity |
| vat_number | read from `Entity.vat_number` | Y | — | Not duplicated; profile cannot be enabled if blank. Format: 15 digits, first and last digit `3` (XML v1.2 BR-KSA-40, p.56) |
| legal name en/ar | read from `Entity.legal_name_en/_ar` | Y | Y | Seller name on XML/QR; not duplicated (D-7) |
| seller_id_scheme / seller_id_value | Choice: `CRN`, `MOM`, `MLS`, `700`, `SAG`, `OTH` / Char (alphanumeric only) | Y | — | Exactly one seller ID is emitted; if the entity holds several, the first in that order is used (XML v1.2 BR-KSA-08, p.48–49; mandatory per DG p.63). Default `CRN` = `Entity.cr_number`; where an entity has several CRs, the branch CR of the issuing branch. `700` = `Entity.unified_number` only when no CRN exists (D-11, R-48). The unified number is otherwise **not** in the XML; it is shown on the human-readable invoice only (invoicing R72) |
| address | read from `Entity` national-address fields (`invoicing.md` §3.0) | Y | Y (names) | Not duplicated (OQ-12 hand-off). Required set: street, building number, postal code, city, district, country code (XML v1.2 BR-KSA-09, p.49). Building number 4 digits (BR-KSA-37, p.56); postal code 5 digits (BR-KSA-66, p.58). Additional number (KSA-23) is **optional** (DD KSA-23 "O"): emitted when present, never required |
| vat_group | *Withdrawn — see Decision D-2* (no VAT groups) | | | |
| environment | Choice: `sandbox`, `simulation`, `production` | Y | — | Default `sandbox`. `production` settable only by permission in §7 and only outside test settings. `simulation` is not named in DPM v3 (Z34) **[SDK-confirm]** (OQ-26) |
| go_live_date | Date, null | N | — | Wave date for this entity (OQ-1). Null = not live; documents are not submitted |
| is_enabled | Bool | Y | — | Default False |

### 3.2 `EgsUnit` — an onboarded generation unit

| Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- |
| entity | FK(Entity) | Y | — | |
| code | Char(20) | Y | — | Unique per entity |
| name_en / name_ar | Char(200) | Y | Y | |
| branch_name_en / branch_name_ar | Char(200) | N | Y | CSR OU = branch name (SFIS §2.2.2 Table 1, p.16) |
| serial_number | Char | Y | — | CSR serial, format `1-<solution>\|2-<model>\|3-<serial>` (SFIS p.16; DTG p.34, e.g. `1-hay\|2-23\|3-35435`), placed in the CSR alternative names. The docs give no attribute OID **[SDK-confirm]** against the SDK CSR template |
| location / industry | Char | Y | — | Location = `registeredAddress` (2.5.4.26), default `Entity.short_address` ("preferably the Short Address", SFIS p.17). Industry = `businessCategory` (2.5.4.15), free text (SFIS p.17). All CSR fields are mandatory (DTG p.29) |
| supports_standard / supports_simplified | Bool | Y | — | Drive the CSR invoice-type flag: 4 digits `f"{int(std)}{int(simp)}00"`, e.g. 1000, 0100, 1100 (SFIS p.17 "TSCZ"; DTG p.28, 45 "TSXY", last two digits 0). The flag's OID is **[SDK-confirm]**: SFIS lists 2.5.4.15 for both this flag and Industry |
| status | Choice: `draft`, `csr_ready`, `compliance_csid`, `compliance_passed`, `active`, `renewal_due`, `expired`, `revoked` | Y | — | See R-5 |
| last_icv | PositiveBigInteger | Y | — | Default 0. Updated only by `services.stamp_document` |
| last_invoice_hash | Char(88) minimum | Y | — | PIH for next document. Initial value is the exact literal constant `NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOQ==` (XML v1.2 BR-KSA-26, p.54; DD KSA-13): base64 of the lowercase **hex text** of SHA-256("0"), 88 chars. Stored as a constant, never computed (R-12) |
| private_key_ref | Char | Y once `csr_ready` | — | **Reference** to an OpenBao path, never the key (D-3), e.g. `zatca/<entity.code>/<egs.code>/private_key`. A renewal writes a new version (R-10) |

Constraint: unique (`entity`, `code`).

**CSR field mapping (SFIS §2.2.2 Table 1, p.16–17; DTG §3.3.3 p.27–29)**

| CSR field | Source | Note |
| --- | --- | --- |
| OU (organisation unit) | `branch_name_*` | If an entity were ever in a VAT group (not the case, D-2): the 10-digit TIN, signalled by the 11th digit of the Organization Identifier being `1` (SFIS Table 1 p.16; DTG §3.3.3 p.27) |
| Serial number | `serial_number` | Format above; OID **[SDK-confirm]** |
| Invoice type | `supports_*` | 4-digit flag above; OID **[SDK-confirm]** |
| Location (`registeredAddress` 2.5.4.26) | `location`, default `Entity.short_address` | |
| Industry (`businessCategory` 2.5.4.15) | `industry` | Free text |
| Other Table 1 fields (common name, organisation name, organisation identifier, country) | `EgsUnit` / `Entity` | Mapped by the builder per SFIS Table 1 and checked against the SDK CSR template **[SDK-confirm]** |

### 3.3 `EgsCredential` — compliance CSID or production CSID (PCSID)

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| entity | FK(Entity) | Y | Must equal `egs_unit.entity` |
| egs_unit | FK(EgsUnit) | Y | |
| kind | Choice: `compliance`, `production` | Y | |
| environment | Choice: `sandbox`, `simulation`, `production` | Y | Environment that issued it; usable only there (R-36). `simulation`: OQ-26 |
| certificate_pem | Text | Y | Public certificate; not secret |
| secret_ref | Char | Y | Reference (OpenBao path + version) to the CSID API secret (D-3) |
| zatca_request_id | Char | Y | Returned by the compliance CSID API and passed to the production CSID API (DPM Table 2, p.64–65) |
| valid_from / valid_to | DateTime | Y | Parsed from the certificate |
| status | Choice: `active`, `superseded`, `expired`, `revoked` | Y | `superseded` on renewal: ZATCA revokes the old CSID when it issues the new one (DTG p.32) |
| replaced_by | FK(self), null | N | Renewal chain |

At most one `active` credential per (`egs_unit`, `kind`) — DB partial unique constraint.

### 3.4 `EInvoice` — the e-invoicing record for one business document

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| entity | FK(Entity) | Y | Must equal source document entity and `egs_unit.entity` |
| source_document | FK to invoicing document (exact model per `invoicing.md`, OQ-12) | Y | One `EInvoice` per issued document |
| egs_unit | FK(EgsUnit) | Y | |
| uuid | UUID | Y | Generated once, never reused (DTG p.72) |
| zatca_category | Choice: `standard`, `simplified` | Y | |
| type_code | Choice: `388` invoice, `381` credit note, `383` debit note, `386` prepayment invoice (R-39) | Y | XML v1.2 §11.2.1 p.30–31. UBL root element `Invoice` for all four ("The UBL Message type is 'Invoice' for all document types"); the same table names 381 "Credit note", so the root for 381 is **[SDK-confirm]** |
| subtype_code | Char(7) | Y | `NNPNESB` (XML v1.2 BR-KSA-06, p.48; DD KSA-2): positions 1–2 = `01` standard / `02` simplified; positions **3–7** = flags P third-party, N nominal, E export, S summary, B self-billed. Checks: no self-billed on export (BR-KSA-07, p.48); simplified allows only positions 3, 4 and 6 (BR-KSA-31, p.55) (R-46). The XML prose "final four positions" (p.30) and the 6-char DD example contradict BR-KSA-06; the normative rule governs |
| billing_reference | FK(EInvoice), null | Y for 381/383 | Original invoice (one, default; multiple: OQ-27). Emitted as BT-25 `cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID` = the original's invoice number (BT-1, the IRN) as text, **not** its UUID (DD BT-25; BR-KSA-56, p.57). Reason: R-19 |
| prepayment_references | M2M(EInvoice, type 386), same entity | N | On a 388 that settles prepayments (R-39) |
| issue_datetime | DateTime (stored UTC) | Y | The document's generation (stamping) time (IR Second(3)(B) p.3). Emitted as KSA-25 `hh:mm:ssZ` in UTC, or `hh:mm:ss` in AST with no suffix (XML v1.2 p.47); issue date ≤ current date (BR-KSA-04, p.47) |
| icv | PositiveBigInteger | Y after stamping | Unique per `egs_unit` |
| pih | Char(88) minimum | Y after stamping | |
| invoice_hash | Char(44) | Y after stamping | base64 of the **raw 32-byte** SHA-256 digest (XML v1.2 BR-KSA-26, p.53–54; DTG §5.2 p.52) |
| signed_xml | Text / file | Y after stamping | Immutable once written |
| qr_tlv | Text (base64) | Y after stamping | Simplified: always. Standard: per R-15 |
| cleared_xml | Text / file, null | Y when cleared | XML returned by ZATCA, stamped and with the QR included/updated by FATOORA (DTG §4.3.2 p.50; DPM p.84) |
| status | Choice: `pending`, `stamped`, `queued`, `submitting`, `cleared`, `cleared_with_warnings`, `reported`, `reported_with_warnings`, `reported_clearance_disabled`, `rejected`, `failed_retryable`, `failed_final` | Y | See R-20; `reported_clearance_disabled`: R-44 |
| reporting_deadline | DateTime, null | Y for simplified | `issue_datetime + 24h` (IR Second(3)(B) p.3; DG §4.2.2(c) p.18) |
| retention_class | Choice: `standard`, `capital_asset_real_estate`, `movable_capital_asset` | Y | Supplied by `invoicing` (R-38) |
| retention_until | Date | Y | Computed per record by R-38; child records (attempts, messages) inherit it |
| acquisition_date | Date, null | Y for `movable_capital_asset` | Start of the R-38 movable-asset period, supplied by `invoicing` |

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
| outcome | Choice: `accepted`, `accepted_with_warnings`, `rejected`, `clearance_disabled`, `transient_error`, `permanent_error` | Y | `clearance_disabled` = HTTP 303 (R-44) |
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

`entity`, `egs_unit`, `action` (csr_generated, otp_entered, otp_rejected, compliance_csid_issued, compliance_checks_passed, compliance_checks_failed, pcsid_issued, pcsid_renewed, revoked, environment_changed, delegation_granted, delegation_revoked, vat_status_changed, secret_store_unavailable, secret_store_available — the last two added 2026-10-16, R-47), `actor`, `at`, `detail` JSON (never secrets, never OTP values). Append-only.

### 3.8 `ComplianceTask` (human follow-up)

| Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- |
| entity | FK(Entity) | Y | — | = `einvoice.entity` |
| einvoice | FK(EInvoice) | Y | — | |
| kind | Choice: `late_reporting_notice` | Y | — | R-35 |
| title_en / title_ar, instructions_en / instructions_ar | Char / Text | Y | Y | From a template; the form URL comes from configuration (R-35) |
| status | Choice: `open`, `notified`, `done` | Y | — | `open → notified → done` (R-35) |
| notified_at | DateTime, null | Y from `notified` | — | When the entity notified ZATCA of the failure |
| notice_reference | Char, null | Y from `notified` | — | Reference/acknowledgement from the ZATCA form |
| recovered_notified_at | DateTime, null | Y when done | — | When the entity notified ZATCA that the incident ended (IR Seventh(5) p.7; DG §10 p.48) |
| recovery_notice_reference | Char, null | Y when done | — | Reference of the recovery notice |
| evidence | File, null | N | — | Screenshot / PDF of the submitted form(s) |
| done_by | FK User, null | Y when done | — | |

Cannot be deleted; `done` is final.

### 3.9 `KeyCustodianDelegation` (D-3; register of who may access an entity's ZATCA secrets)

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| entity | FK(Entity) | Y | Delegation is per entity, never group-wide. Exception: `unseal` acts on the whole store, so it is recorded on the group parent entity (R-49, OQ-32) |
| custodian | FK User | Y | Named person |
| scope | Choice: `onboard_renew` (generate CSR, write key/secret), `revoke`, `restore`, `unseal` (added 2026-10-16, D-10) | Y | `unseal` is a register entry: the ERP never receives or stores the unseal key; the MP hands it over outside the ERP |
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
| R-4 | A profile cannot be enabled while `Entity.vat_number` or a required address field is blank or malformed: VAT number 15 digits, first and last `3` (XML v1.2 BR-KSA-40, p.56); street, building number, postal code, city, district, country code present (BR-KSA-09, p.49); building number exactly 4 digits (BR-KSA-37, p.56); postal code exactly 5 digits (BR-KSA-66, p.58). The additional number is optional. | S |
| R-5 | EGS status transitions only along: draft → csr_ready → compliance_csid → compliance_passed → active ⇄ renewal_due → expired; compliance_csid → draft when a compliance check fails (R-8); any → revoked. No skipping. | S |
| R-6 | A key pair is generated inside the ERP per EGS unit; the private key is written only to the secret store (OpenBao, D-3) and the DB holds only `private_key_ref`. Private keys, CSID secrets and OTPs never appear in the DB, logs, `OnboardingEvent.detail`, error messages or fixtures. Keys must be non-exportable and, when held in software, on encrypted disk (SFIS req 6, p.11; IR Annex 1 p.11); how the OpenBao design meets this is OQ-3 **[Needs expert sign-off: security]**. | S + test that scans logs/DB dumps |
| R-7 | The OTP is obtained by a human from the portal (there is no API for OTP, DTG p.75–77), entered by a user with the onboarding permission, used once and not stored; it is sent in the API request header. Locally it must be exactly 6 numeric digits, otherwise it is refused before any call (DTG p.30, 75–77). OTP validity is 1 hour (DTG p.24–25), shown as a hint on the form. If ZATCA rejects the OTP as expired or invalid, the request fails, EGS status is unchanged, an `otp_rejected` event is logged (no OTP value), a bilingual message asks for a new OTP, and the ERP never resubmits the same OTP. | S |
| R-8 | An EGS unit becomes `active` only after all required compliance checks pass and a PCSID is issued (DTG §3.3.4 p.31). Required samples: flag 1000 → standard invoice, standard debit note, standard credit note (3); flag 0100 → simplified invoice, simplified debit note, simplified credit note (3); flag 1100 → all 6. Whether a 386 sample is also required is **[SDK-confirm]** (OQ-29; the guideline predates 386). If any check fails, the unit goes back to `draft`: onboarding restarts with a new OTP and a new CSR (DTG §3.3.4 p.31); a `compliance_checks_failed` event is logged. | S |
| R-9 | A document can be stamped only by an `active` EGS unit holding a non-expired production credential (or the matching compliance credential in `compliance_check` mode). | S |
| R-10 | `renewal_due` is set N days before `valid_to` (N = OQ-9, safe default 30). Renewal repeats the OTP + CSR flow and **always generates a new key pair** (IR Annex 1 p.11), written as a new OpenBao version; the old credential becomes `superseded` (ZATCA revokes it on issuing the new one, DTG p.32). An expired credential blocks stamping; nothing is silently signed with an expired certificate. | S (scheduled job) |
| R-11 | ICV is assigned under a row lock on `EgsUnit` (`select_for_update`): strictly increasing by 1 per EGS unit, no gaps, no reuse. | S + DB unique (`egs_unit`,`icv`) |
| R-12 | PIH of each document equals `invoice_hash` of the immediately preceding document generated by the same EGS unit. One sequence per EGS unit covers both standard and simplified documents (DTG p.48). **Rejected documents stay in the chain**: the next document's PIH is the rejected document's hash (DTG p.48, 71, 72, 77). ICV and UUID are never reused (DTG p.72). The first document uses the literal initial PIH constant of §3.2 (XML v1.2 BR-KSA-26, p.54; DD KSA-13), never a computed value. ICV and PIH are assigned in the same transaction as the hash. The official docs do not say that the sandbox skips the PIH check (DPM p.21–22, 78 only say compliance checks are not required there); a sandbox pass is not evidence that the seed is right, so AT-10 asserts the literal. | S |
| R-13 | Invoice hash (XML v1.2 BR-KSA-26, p.53–54; DTG §5.2 Step 1, p.52): (1) remove the XML declaration; (2) remove, via the XPath transforms `not(//ancestor-or-self::ext:UBLExtensions)`, `not(//ancestor-or-self::cac:Signature)`, `not(//ancestor-or-self::cac:AdditionalDocumentReference[cbc:ID='QR'])`; (3) canonicalise with C14N 1.1 (`http://www.w3.org/2006/12/xml-c14n11`); (4) SHA-256 to a 32-byte binary digest; (5) base64 of the **raw** digest (44 chars; DTG example `oRtv5YelD32v/jp/tC3MzzK0PumzfZ8lLQQkPlTBGj8=`). The AT-13 fixture comes from ZATCA SDK samples (the DD Stamp-example sheet is empty; DD XAdES sample digests are illustrative only). | S |
| R-14 | Stamp: ECDSA on curve **secp256k1** with SHA-256 (DTG p.53, 57, 62; the DTG QR tag-8 sample key has OID 1.3.132.0.10). SFIS's illustrative profile says "P-256" (SFIS §2.2.1 req 16 p.13, p.14) — docs conflict **[SDK-confirm]** (OQ-28). `ds:SignatureValue` = base64 of the DER-encoded ECDSA signature (ASN.1 SEQUENCE r, s; DTG example p.53). Target structure (DD KSA-15; DTG §5.2 Steps 3–6, p.53–56): `ds:Signature Id="signature"` → `ds:SignedInfo` (C14N 1.1; `ecdsa-sha256`; `Reference Id="invoiceSignedData" URI=""` with the 3 R-13 XPath transforms + C14N 1.1 and SHA-256; second Reference to `#xadesSignedProperties`) → `ds:SignatureValue` → `ds:KeyInfo/ds:X509Data/ds:X509Certificate` → `ds:Object/xades:QualifyingProperties Target="signature"/xades:SignedProperties Id="xadesSignedProperties"/xades:SignedSignatureProperties` with `xades:SigningTime` and `xades:SigningCertificate/xades:Cert/{xades:CertDigest (SHA-256), xades:IssuerSerial (ds:X509IssuerName, ds:X509SerialNumber)}`. CertDigest = base64 of the **hex text** of the certificate's SHA-256; SignedProperties digest = base64 of the raw SHA-256 of the linearised SignedProperties (spaces removed). UBL wrapper: `cbc:ID` `urn:oasis:names:specification:ubl:signature:1`, `sbc:ReferencedSignatureID` `urn:oasis:names:specification:ubl:signature:Invoice` (BR-KSA-28/29), `cac:Signature` method `urn:oasis:names:specification:ubl:dsig:enveloped:xades` (BR-KSA-30; XML v1.2 p.55). Open **[SDK-confirm]** (OQ-28): (a) what is signed — DTG Step 2 says the raw invoice hash, standard XMLDSig signs canonical SignedInfo; (b) SFIS-only elements (SigningCertificateV2 with the whole chain, SignaturePolicyIdentifier, DataObjectFormat MimeType `text/xml`; SFIS §2.3.3 p.19–22); (c) Reference Type URI of the SignedProperties reference: SFIS `http://uri.etsi.org/01903#SignedProperties` vs DD `http://www.w3.org/2000/09/xmldsig#SignatureProperties`. | S |
| R-15 | QR = base64 of TLV, tag and length 1 byte each, so **each value ≤ 255 bytes**; a longer value (e.g. a long Arabic seller name) rolls back stamping with a bilingual error, no ICV consumed (SFIS p.25; DTG p.60–63). Tags (SFIS §4.1 p.25–26; DTG §6 p.58–62): 1 seller name (UTF-8); 2 VAT number (UTF-8); 3 timestamp ISO 8601, e.g. `2022-02-21T12:13:57Z` (UTF-8); 4 total incl. VAT (UTF-8); 5 VAT total = BT-110 (UTF-8); 6 invoice hash — the base64 hash as text per the DTG sample (L=44), while SFIS says "32 bytes" **[SDK-confirm]** with `fatoora validateqr` (OQ-28); 7 ECDSA signature as base64 text; 8 public key as raw DER SubjectPublicKeyInfo bytes; 9 raw DER bytes of the certificate's signature (from the PCSID "ecdsa-with-SHA256" value). Tag 9 only on simplified documents and their notes. Simplified documents carry all 9 tags (DG p.18, 60). For standard documents the QR is the one in ZATCA's `cleared_xml` (ZATCA generates it at clearance); a seller QR is optional (DTG p.50, 78). Length limit: R-37. | S |
| R-16 | Before submission, the XML is validated locally against the UBL 2.1 XSD, the EN 16931 rule subset and the KSA rules (the checks ZATCA's APIs run; DPM p.83–84; DG p.61), and its size is checked against ZATCA's ~10 MB limit (HTTP 413; DPM p.83–84). Any local error stops the document at `pending` with `ValidationMessage(origin=local_*)` and consumes **no** ICV. | S |
| R-17 | Once `signed_xml` is written it is never modified; once `cleared_xml` is written it is never modified. Corrections are new documents (credit/debit note) created in `invoicing`, never edits. | M |
| R-18 | `EInvoice` rows with status other than `pending` cannot be deleted. `SubmissionAttempt`, `ValidationMessage`, `OnboardingEvent` cannot be updated or deleted. | M (+ DB trigger later, as for ledger) |
| R-19 | Credit and debit notes must reference an existing `EInvoice` of the same entity (BT-25 mandatory for 381/383, BR-KSA-56, p.57) and carry a reason. BT-25 is the original's invoice number (BT-1) as text, not a UUID (DD BT-25). The reason is KSA-10 `cac:PaymentMeans/cbc:InstructionNote`, mandatory for 381/383 (BR-KSA-17, p.52), 1–1,000 chars, visible on output; the Arabic reason is required, English optional, since visible fields must be in Arabic (DTG p.79; DG p.59, 62), aligned with `invoicing.md` R18/R19. Because KSA-10 sits in PaymentMeans, PaymentMeansCode (BT-81) is required on every CN/DN (BR-49, p.38); missing → local rejection. The category (standard/simplified) follows the original invoice. One original per note (default, OQ-27). | S |
| R-20 | EInvoice status flow: pending → stamped → queued → submitting → {cleared, cleared_with_warnings, reported, reported_with_warnings, reported_clearance_disabled (standard only, R-44), rejected, failed_retryable → queued, failed_final}. No other transitions. | S |
| R-21 | Standard (B2B) documents use the clearance API; simplified (B2C) documents use the reporting API. A document never goes to the wrong API, with one exception: a standard document whose clearance call returned HTTP 303 is resubmitted to reporting under R-44. | S |
| R-22 | A standard document is **issued** only when status is `cleared` or `cleared_with_warnings`: it is cleared before it is provided to the buyer (DTG p.50, 78). Until then `zatca.services.is_issued(doc)` returns False and the buyer copy (PDF/XML) cannot be generated or sent. `reported_clearance_disabled` is also not issued until OQ-7 is signed off (R-44). | S (+ contract test with `invoicing`) |
| R-23 | A simplified document is issued at stamping (signed XML + QR) and must be reported within 24 h of its generation (IR Second(3)(B) p.3; DG §4.2.2(c) p.18, §7.2 p.37); `issue_datetime` is that generation time. A document still unreported 2 h (configurable) before `reporting_deadline` raises an alert to the entity's ZATCA admins. Missed deadline: R-35. | S (scheduled job) |
| R-24 | Transient failures (network error, timeout, HTTP 429/5xx) → `failed_retryable`, retried with exponential backoff (default: 1 min, doubling, cap 1 h, max 10 attempts for standard documents — safe default, OQ-10). Simplified documents are **never** moved to `failed_final` for transient errors: after the max attempts they keep retrying at the cap interval until reported, as ZATCA expects continued attempts at regular intervals with evidence kept (DG §10 p.46, 48). Every attempt is an append-only `SubmissionAttempt` row; these rows are the evidence of reporting attempts. Retries resend the **same** signed XML with the same ICV/PIH/UUID; never re-stamp. | S |
| R-25 | ZATCA rejection (validation errors, HTTP 400) → `rejected`; no automatic retry. Errors stored as `ValidationMessage` in en (as received) and ar (from table). | S |
| R-26 | Accepted-with-warnings (HTTP 202) stores warnings and counts as cleared/reported; warnings are shown to users in both languages. | S |
| R-27 | *Amended 2026-10-16 (verification Z22).* ZATCA does not require submission in ICV order: generation does not depend on clearance and documents "can be submitted as 3, 1 and 2" (DTG FAQ p.71, 78; DTG p.48). The queue worker therefore does not hold a document back because an earlier ICV of the same unit is still `queued`/`submitting`. Serialising per EGS unit is an optional internal setting (`ZATCA_SERIALISE_PER_EGS`, default off). | S (queue worker) |
| R-28 | Documents of an entity whose `go_live_date` is null or in the future are not submitted; `invoicing` must treat them per pre-Phase-2 rules (OQ-1). | S |
| R-29 | Only the integration layer's ZATCA client performs HTTP calls. In test settings the client is replaced by a mock; any attempt to reach a real ZATCA host from tests fails. `environment=production` cannot be set when `settings.TESTING` or an agent service user is the actor. | S + T |
| R-30 | Archive (signed XML, cleared XML, responses, attempts, messages) is kept until `retention_until` (R-38) and cannot be purged earlier. No purge job is enabled (OQ-8). Storage location: KSA (D-5, R-43). Archived XML file names are built from seller VAT number + issue date + issue time + invoice number (IRN) (DG p.26); separator and date format are a house convention (`<vat>_<yyyymmdd>_<hhmmss>_<IRN>.xml`), not specified by ZATCA. | S + M |
| R-31 | *Amended 2026-10-16 (verification Z35).* Amounts in XML are taken from the `invoicing` document as `Decimal`, never floats. Rounding is half-up; VAT per category is computed at **document level** by `invoicing`, not as a sum of rounded line VAT (XML v1.2 §10, p.29). Locally the module rejects a document whose header net ≠ sum of line net amounts; whose category VAT ≠ round(category taxable amount × rate / 100, 2) (BR-CO-17 / BR-S-09, p.38–40); whose BT-110 ≠ Σ category VAT BT-117 (BR-CO-14); or whose line amount incl. VAT KSA-12 ≠ line net BT-131 + line VAT KSA-11 (BR-KSA-51, p.57). Line VAT and line total are still emitted on standard documents (BR-KSA-52/53). Σ line VAT is **not** cross-checked against BT-110: no line-vs-header tolerance is published. The SDK schematron is run in CI to confirm **[SDK-confirm]**. | S |
| R-32 | Sharia: this module does not validate Sharia compliance; it accepts only documents that `invoicing` has already validated (including Sharia checks). It has no path to issue a document that bypasses `invoicing` services. | S (single entry point `submit_document`) |
| R-33 | Bilingual: seller name/address in Arabic are always present in XML. Every field visible on the human-readable invoice, notes included, must be in Arabic and may be bilingual; Latin script is allowed for proper names; XML numerals are 0–9 (DTG p.79; DG p.28, 59, 62; TIR §4.2.1 p.16–17; XML v1.2 p.12). Buyer-facing output (PDF in `invoicing`) is Arabic, optionally with English. All statuses, errors and UI labels have en/ar. | S + translations |
| R-34 | AI agents (gateway service users) may view statuses and draft nothing in this module; they cannot onboard, enter OTPs, stamp, submit, or retry. | Q (permissions) |
| R-35 | When `reporting_deadline` passes and a simplified document is not `reported*`, the system opens one `ComplianceTask(kind=late_reporting_notice)` for the entity's ZATCA admins (en/ar). Step 1: the entity notifies ZATCA through the failure-notification form on ZATCA's website (DG §10 p.46, 48; DTG FAQ p.77); the task moves to `notified` only with `notified_at` and `notice_reference`. Step 2: once the connection is restored and the document is `reported*`, the entity notifies ZATCA again that the incident ended (IR Seventh(5) p.7; DG §10 p.48); the task moves to `done` only with `recovered_notified_at`, `recovery_notice_reference` and `done_by`. The form URL is configuration (`ZATCA_FAILURE_NOTICE_URL`, currently `https://zatca.gov.sa/en/E-Invoicing/FailureNotifications/Pages/VerifyTaxpayer.aspx`). Retries (R-24) continue regardless. | S (scheduled job) + M |
| R-36 | A credential is usable only in the environment that issued it: sandbox test CSIDs cannot be used in the core solution (DPM p.21); stamping/submitting requires `credential.environment == profile.environment`. Changing environment requires new onboarding. | S |
| R-37 | The QR base64 string is at most 700 characters (SFIS v1.2 §4.1 p.25; DTG p.58; the 2021 QR guide's 500 is superseded). Longer → stamping transaction rolls back (no ICV consumed), bilingual error. | S |
| R-38 | `retention_until` is computed per record and never shortened (TIR §8.3.3 p.42; IR Annex 1 p.10): `standard` = end of the tax period containing `issue_datetime` + 6 years ("no less than six years from the end of the related tax period"); `capital_asset_real_estate` = end of that tax period + 15 years ("not less than 15 years in all cases"; the docs give no start point, so the tax-period end is an **assumption** **[Needs expert sign-off: tax]**); `movable_capital_asset` = `acquisition_date` + the Art. 52 adjustment period + 5 years, and never earlier than the `standard` date (the adjustment period length is configuration pending **[Needs expert sign-off: tax]**, OQ-31). `invoicing` sets the class to `capital_asset_real_estate` when any line uses account 1510 (investment property) or the document is rent on investment property (default per OQ-8); it sets `movable_capital_asset` only by explicit flag (OQ-31). | S + M |
| R-39 | Type 386 prepayment invoices are in scope, standard (subtype 01) and simplified (02), submitted like 388; the 386 carries a UUID (optional today, to be mandated, XML v1.2 §9.5). The final 388 that settles prepayments (same entity, same buyer) adds, per VAT category/rate of the prepayment, one line with zero line amounts (BR-KSA-82) and `cac:DocumentReference` carrying KSA-26 = the 386's invoice number (BT-1), KSA-28 its issue date, KSA-29 its issue time, KSA-30 = `386`; the line also carries KSA-31 prepaid taxable amount, KSA-32 prepaid VAT = round(KSA-31 × KSA-34 / 100, 2), KSA-33 VAT category, KSA-34 rate. BT-113 PrepaidAmount = Σ(KSA-31 + KSA-32) (BR-KSA-80); amount payable = BT-112 − BT-113 (XML v1.2 §9.5 p.20–25, p.59–61; BR-KSA-73 to BR-KSA-82; DG §8 p.40–43). Over-deduction is not cross-checked by ZATCA in real time; a refund needs a credit note (DG §8). Produced only by the `invoicing` advances flow (invoicing R65, D15). | S |
| R-40 | *Amended 2026-10-16 (verification Z36).* **Every** XML, including SAR documents, carries `cbc:TaxCurrencyCode` = `SAR` (BT-6, BR-KSA-68 "must exist"; BR-KSA-EN16931-02 "must be SAR") and a second `cac:TaxTotal` in SAR without subtotals holding BT-111 (BR-53; BR-KSA-EN16931-09); all other `currencyID` values equal the document currency BT-5 (BR-KSA-CL-02) (XML v1.2 §9.1 p.16, p.37–38, 58, 63–67). Documents may be in any currency (D-6); for a non-SAR document the SAR VAT is taken from `invoicing` (`total_vat_sar`, SAMA daily rate on the tax due date, TIR §4.2.2 p.17) and never recomputed here; a non-SAR document without it is rejected locally (no ICV consumed). Non-SAR issuance stays blocked until `ledger-fx.md` is built (invoicing OQ-6/R56). QR tag 5 is BT-110 in the document currency; QR behaviour for non-SAR documents is **[SDK-confirm]** (OQ-22). | S |
| R-41 | Onboarding, renewal, revocation and restore actions for an entity need, besides the §7 role, an active `KeyCustodianDelegation` for that entity and scope. The ERP's runtime signing identity in OpenBao reads only the paths of entities it signs for. Grants and revocations are logged; a revoked delegation blocks the next action at once. | S + OpenBao policy |
| R-42 | After `rejected`, the corrected document is a new `EInvoice` for a new `invoicing` document with a new number, UUID, ICV and hash; the rejected number and UUID are never reused (D-4). The rejected document's hash stays in the PIH chain: the correction's PIH is the hash of the immediately preceding generated document (R-12; DTG p.48, 71, 72, 77). A re-issued standard document gets a new issue date ("The date on invoice should be updated", DTG p.78; OQ-6). | S |
| R-43 | Database, archive, backups, logs and the secret store run in KSA (D-5). Configuration pointing any of them to a non-KSA host is a deployment error caught by the release checklist. For VAT records this is also a statutory requirement for KSA-resident taxpayers (TIR §8.3.1 p.41; OQ-20). | Process + code-reviewer |
| R-44 | *Added 2026-10-16 (verification Z21).* If the clearance API returns HTTP **303**, ZATCA has disabled clearance and standard documents and their notes must be submitted to the reporting API (DTG p.51, 70; DPM p.64). The module records the attempt as `clearance_disabled`, resubmits the **same** signed XML (same ICV, UUID, hash) to reporting, and on acceptance sets `reported_clearance_disabled`. ZATCA returns no stamp or QR in this case. The status does **not** count as issued (R-22) and `invoicing` does not post or share it until OQ-7 is signed off **[Needs expert sign-off: tax]**; the entity's ZATCA admins are alerted (en/ar). | S |
| R-45 | *Added 2026-10-16 (verification Z31).* ZATCA auto-revokes an entity's CSIDs on VAT deregistration, suspension or joining a VAT group (DTG p.70). When a ZATCA admin records such a change for an entity (`vat_status_changed` event), all its credentials become `revoked`, all its EGS units `revoked`, and stamping is blocked until re-onboarding. Other entities are unaffected. (No entity is in a VAT group, D-2; the rule is data-driven for any future entity.) | S |
| R-46 | *Added 2026-10-16 (verification Z14, Z1).* Locally rejected (no ICV consumed): a `subtype_code` not matching `NNPNESB` (BR-KSA-06); self-billed together with export (BR-KSA-07); a simplified document with the export or self-billed flag (only positions 3, 4, 6 allowed, BR-KSA-31); an export document carrying a buyer VAT number (BR-KSA-46, p.57); a non-export buyer VAT number not matching 15 digits with first and last `3` (BR-KSA-44, p.56). | S |
| R-47 | *Added 2026-10-16 (D-10).* If the secret store is sealed or unreachable: stamping fails **before** ICV assignment (document stays `pending`, `last_icv` unchanged, retried by the queue worker); clearance/reporting of already-stamped documents is a transient failure under R-24 (simplified never `failed_final`). At the first failure the system writes `secret_store_unavailable` and alerts, in en/ar, the affected entities' ZATCA admins, the Managing Partner and every custodian with an active `unseal` delegation; `secret_store_available` is written on recovery. R-23 alerts and R-35 late-reporting tasks apply unchanged. The resulting downtime risk is accepted by the owner (D-10). | S (stamp + queue worker) |
| R-48 | *Added 2026-10-16 (D-11).* The XML carries **exactly one** seller identification (`cac:AccountingSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID` with `@schemeID`), chosen in the order CRN, MOM, MLS, 700, SAG, OTH (XML v1.2 BR-KSA-08 "must exist only once", p.48–49): `CRN` = `Entity.cr_number` (branch CR) when present, else `700` = `Entity.unified_number`. **The XML cannot carry both the CR and the unified number**; the unified number appears only on the human-readable invoice, where `invoicing` shows both with bilingual labels (invoicing R72). A document whose entity has no seller ID in any of these schemes is rejected locally (no ICV consumed). | S |
| R-49 | *Added 2026-10-16 (D-10).* An `unseal` delegation is granted only by the Managing Partner, to a named user holding the ZATCA admin role on the group parent (access R17), recorded on the group parent entity (`consolidation=parent`) because unsealing opens the store for all entities. The ERP stores no unseal key or share; the grant, any revocation and `valid_until` are logged like other delegations (R-41). The store is initialised with a single unseal key held by the MP (no Shamir split, D-10); auto-unseal is not used, since it would move the master key away from the MP. | S + OpenBao configuration + process |

## 5. Accounting

**This module creates no journal entries.** It never calls `ledger.services.post_entry`, `create_entry` or `reverse_entry`. Posting is done by `invoicing` per `invoicing.md` (accounts there, e.g. 1200 Trade receivables, 2200 VAT output, from `ledger/coa_template.py`).

What `invoicing` must and must not do based on this module's status:

| Case | `invoicing` must not | `invoicing` may |
| --- | --- | --- |
| Standard (B2B) invoice/note, status before `cleared*` | Post the journal entry, mark as issued, send to buyer, count it in AR ageing or VAT returns | Keep it as "awaiting clearance" |
| Standard, `rejected` | Post or issue it; reuse its number/UUID/ICV | Create a new corrected document under a new number (new UUID, ICV, new issue date); the rejected number stays used (D-4, R-42) |
| Standard, `failed_final` (could not reach ZATCA) | Post or issue it | Await human decision (OQ-7) |
| Standard, `reported_clearance_disabled` (R-44) | Post, issue or share it until OQ-7 is signed off | Keep it as "reported, clearance disabled" |
| Standard, `cleared*` | Edit it | Post it (date = issue date), issue the **cleared** XML/QR to buyer |
| Simplified (B2C), `stamped` / `queued` / `submitting` | Edit it; post the journal entry (D-8) | Give the stamped document to the buyer (issued at stamping, R-23) |
| Simplified, `reported*` | Edit it | Post it (D-8) |
| Simplified, `rejected` after hand-over to the buyer | Edit it; post its draft entry | Re-issue a corrected document under a new number (D-4); no GL effect to reverse since nothing was posted. Whether the buyer copy also needs a credit note: DTG FAQs conflict (p.77 "cancelled via a credit note and a new invoice" vs p.71 "no need to issue another invoice") **[Needs expert sign-off: tax]** (OQ-6) |
| 386 prepayment | Treat differently from 388 for issue/post timing | Same rows as above by category; its accounts are set in `invoicing.md` (OQ-16, OQ-22 there) |

## 6. Integrations

All calls go through the integration layer (`integrations/zatca/client.py` — location per integration-layer convention). Tests use a mock client with recorded sandbox-shaped fixtures; no test or agent calls any real ZATCA endpoint or uses real credentials.

| System | ERP → system | System → ERP |
| --- | --- | --- |
| ZATCA Fatoora — compliance CSID API (DPM Table 2, p.64–65; path **[SDK-confirm]**, OQ-30) | CSR (base64), OTP in header | Compliance CSID (binary security token), secret, request ID |
| ZATCA — compliance invoice check API (DTG §3.3.4 p.31; path **[SDK-confirm]**, OQ-30) | Sample signed documents per R-8 | Validation results |
| ZATCA — production CSID API, onboarding + renewal (DPM p.64–65; path **[SDK-confirm]**, OQ-30) | Compliance request ID (onboard) / new CSR + OTP (renew) | PCSID, secret, request ID |
| ZATCA — clearance API `POST /invoices/clearance/single` (DPM p.62–68, 83–86) | JSON `{"invoiceHash", "invoice"}` (invoice = base64 signed XML); UUID in body **[SDK-confirm]** (DPM lists 2 fields only; OQ-30) | 200 cleared + cleared XML; 202 cleared with warnings; 303 clearance disabled (R-44); 400 rejected; 413 too large; 500 server error (R-24) |
| ZATCA — reporting API `POST /invoices/reporting/single` (DPM p.62–68, 83–86) | Same body shape | 200 reported; 202 with warnings; 400 rejected; 500 server error |
| `invoicing` app (internal) | `is_issued`, status, cleared XML, QR, messages | Validated document via `zatca.services.submit_document(doc, user)` |
| Secret store: OpenBao, hosted in KSA (D-3, D-5) | Private key, CSID secret (write once per version, under custodian delegation R-41) | Same (read at signing/submission only, by the ERP signing identity) |
| `ledger-fx` (via `invoicing`) | None | SAR VAT amounts on non-SAR documents (R-40) |
| ZATCA website — failure-notification form (URL in config, R-35) | None automated: failure notice and recovery notice filed manually by the entity's ZATCA admin | References/screenshots uploaded to `ComplianceTask` |
| Banks, GOSI, Mudad, SAP B1 | None | None |

Request headers on clearance/reporting: HTTP Basic auth `CSID:Secret`; `accept-version: V2` ("V2 is currently the only valid version"); `accept-language` `en` or `ar` (DPM p.24–27). Environments: sandbox portal `https://sandbox.zatca.gov.sa/` (DPM); production. Base URLs per environment are not in the docs and are configuration, never code **[SDK-confirm]** (OQ-30). A "simulation" environment is not mentioned in the downloaded docs **[SDK-confirm]** (OQ-26).

Open-source libraries (in-house build, D-1; default set approved by D-9 / OQ-11):

| Need | Options |
| --- | --- |
| Key pair, CSR, ECDSA | `cryptography` (has a SECP256K1 curve; curve per R-14), `pyOpenSSL` |
| XML build / C14N | `lxml`. C14N 1.1 is required (XML v1.2 p.54); lxml's built-in c14n is 1.0/2.0, so the builder must prove the chosen library emits C14N 1.1 byte-for-byte against SDK samples (OQ-11) |
| XML signature | `signxml`, `python-xmlsec` (xmlsec1), or hand-built signed properties on `lxml` + `cryptography` |
| XSD / schematron validation | `lxml`/`xmlschema` for XSD; `lxml.isoschematron` (XSLT 1 only) or Saxon-HE via `saxonche` (MPL; check licence and XSLT 2 need) |
| QR image | `segno`, `qrcode` |
| Reference validation | ZATCA SDK (Java >= 11 and < 15, DPM p.81; free, not open source; licence not stated **[Needs expert sign-off: legal]**) for offline comparison in CI only, never as a runtime dependency |

## 7. Permissions

Role names are provisional (no role model exists yet; OQ-14). All grants are **per entity**.

| Action | ZATCA admin (entity) | Accountant (entity) | Sales user (entity) | Viewer / auditor | Queue worker (system user) | AI agent (gateway) |
| --- | --- | --- | --- | --- | --- | --- |
| Edit profile, create EGS unit | Yes | No | No | No | No | No |
| Generate CSR, enter OTP, renew, revoke | Yes | No | No | No | No | No |
| Record VAT status change (R-45) | Yes | No | No | No | No | No |
| Set environment = production | Yes + second approver (OQ-14) | No | No | No | No | No |
| Submit (triggered by issuing in `invoicing`) | Yes | Yes | Yes (as `invoicing` allows) | No | — | No |
| Stamp / send queued items | — | — | — | — | Yes (only) | No |
| Manually retry `failed_*` | Yes | Yes | No | No | No | No |
| View e-invoices, XML, messages | Yes | Yes | Own documents | Yes (read-only) | — | Status only, if granted |
| Download archive / export | Yes | Yes | No | Yes | No | No |
| Move a `late_reporting_notice` task to `notified` / `done` | Yes | No | No | View only | Opens it only | No |
| Key actions (CSR/key write, renew, revoke, restore) | Only with an active `KeyCustodianDelegation` for the entity and scope (R-41) | No | No | No | No | No |
| Grant / revoke key custodian delegation (incl. `unseal` on the group parent, R-49) | Managing Partner only (holds the master key, D-3, D-10) | No | No | No | No | No |

## 8. Acceptance tests

All tests use fixtures, a fake entity pair (`TST1`, `TST2`, made-up VAT numbers), a throwaway in-memory key store, and a mocked ZATCA client. No network.

| # | Given | When | Then |
| --- | --- | --- | --- |
| AT-1 | Entity TST1 with blank `vat_number` | Admin enables its profile | Rejected; message en/ar (R-4) |
| AT-2 | Draft EGS unit for TST1 | Admin generates CSR | Status `csr_ready`; `private_key_ref` set; no key material in DB or logs (R-6) |
| AT-3 | `csr_ready` unit, mock returns compliance CSID | Admin enters OTP | Credential `compliance` active; OTP not stored anywhere; `OnboardingEvent` without OTP (R-7) |
| AT-4 | `compliance_csid` unit with flag 1100, mock passes all 6 sample checks (standard and simplified invoice, DN, CN) | Run compliance checks | Status `compliance_passed` (R-8) |
| AT-5 | `compliance_csid` unit, mock fails one sample type | Request PCSID | Rejected; unit back to `draft`; `compliance_checks_failed` event; the next step requires a new CSR and a new OTP (R-8, R-5) |
| AT-6 | `draft` unit | Request PCSID directly | Rejected, transition not allowed (R-5) |
| AT-7 | Active unit, credential `valid_to` in 20 days, N=30 | Daily job runs | Status `renewal_due`; admins alerted (R-10) |
| AT-8 | Active unit, credential expired | Stamp a document | Rejected; nothing signed (R-9, R-10) |
| AT-9 | Renewal: mock issues new PCSID | Admin renews (new OTP + CSR) | A new key pair is generated (new `private_key_ref` version, public key differs from the old certificate's); old credential `superseded`, `replaced_by` set; one active per kind (DB) (R-10) |
| AT-10 | Active unit, `last_icv`=0 | Stamp first document | ICV=1; PIH equals exactly the literal `NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOQ==` (88 chars) (R-12) |
| AT-11 | Active unit after AT-10 | Stamp second document | ICV=2; PIH = hash of doc 1 |
| AT-12 | Two concurrent stamp calls on same unit | Both commit | ICVs n and n+1, no duplicate, PIH chain intact (R-11) |
| AT-13 | Fixture XML with known expected hash taken from ZATCA SDK samples | Compute hash | Matches expected; 44-char base64 of the raw digest (R-13) |
| AT-14 | Stamped simplified doc; cleared standard doc | Decode QR | Simplified: tags 1–9 present; seller name in Arabic; tags 1–5 UTF-8 text, tag 6 per R-15, tag 7 base64 text, tags 8–9 raw DER; every value ≤ 255 bytes. Standard: QR taken from `cleared_xml`, no tag 9 (R-15, R-33) |
| AT-15 | Document failing XSD (missing buyer VAT on B2B) | Submit | Stays `pending`; local error message; `last_icv` unchanged (R-16) |
| AT-16 | TST1 document, EGS unit belongs to TST2 | Stamp | Rejected (R-2) |
| AT-17 | Standard doc, mock clearance returns cleared + cleared XML | Queue runs | Status `cleared`; `cleared_xml` stored; `is_issued` True (R-22) |
| AT-18 | Standard doc, mock returns rejection with 2 errors | Queue runs | Status `rejected`; 2 `ValidationMessage` with en text and ar text/fallback; `is_issued` False; no journal entry exists for the document (R-25, §5) |
| AT-19 | Standard doc, mock returns accepted with warnings | Queue runs | `cleared_with_warnings`; warnings stored; `is_issued` True (R-26) |
| AT-20 | Simplified doc | Stamped | `is_issued` True before reporting; `reporting_deadline` = issue + 24h (R-23) |
| AT-21 | Simplified doc unreported, deadline in 1 h | Alert job runs | Alert raised to TST1 admins only, not TST2 (R-23, R-3) |
| AT-22 | Mock times out 3 times then succeeds | Queue runs with backoff | 4 attempts recorded; same request hash each time; ICV/UUID unchanged (R-24) |
| AT-23 | Standard doc; mock returns 5xx beyond max attempts | Queue runs | `failed_final`; admins alerted; `is_issued` False |
| AT-24 | Standard doc sent to reporting API (forced, no 303 received) | Submit | Rejected by service (R-21) |
| AT-25 | Cleared `EInvoice` | Update `signed_xml` or `cleared_xml`, or delete | Raises error (R-17, R-18) |
| AT-26 | `SubmissionAttempt` row | Update or delete | Raises error (R-18) |
| AT-27 | Credit note referencing TST2 invoice from TST1 | Submit | Rejected (R-19, R-1) |
| AT-28 | Credit note without reason, or with English reason only | Submit | Rejected (R-19) |
| AT-29 | Test settings | Code attempts a real HTTP call to a ZATCA host | Test fails loudly (R-29) |
| AT-30 | Agent service user | Tries to onboard, stamp, submit, retry or set production | Each denied (R-34) |
| AT-31 | User with permission only on TST1 | Lists e-invoices | Sees TST1 only (R-3) |
| AT-32 | Entity with `go_live_date` in future | Document handed over | Not submitted; status explains why (R-28) |
| AT-33 | (a) 3 lines net 10.03 S 15%: line VAT 1.50 each (sum 4.50), category VAT 4.51; (b) same with category VAT 4.50; (c) header net 30.10 vs line sum 30.09 | Submit | (a) passes local checks (no line-vs-header tolerance check, R-31); (b) rejected: category VAT ≠ round(30.09 × 15 / 100, 2); (c) rejected; no ICV consumed for (b), (c) (R-31) |
| AT-34 | Archive row with `retention_until` in future | Purge job | Row kept (R-30) |
| AT-35 | *Reworded 2026-10-16 (R-27 amended).* Default settings; ICV 5 of a unit still `submitting` | Worker picks ICV 6 | ICV 6 is submitted without waiting; mock accepts both in either order. With `ZATCA_SERIALISE_PER_EGS` on: ICV 6 waits for 5 (R-27) |
| AT-36 | Simplified TST1 doc; mock times out on every call | Clock passes `reporting_deadline`; later the mock recovers | Status not `failed_final`; retries continue at cap; one `SubmissionAttempt` per try; one open `late_reporting_notice` task for TST1 admins (none for TST2). Moving to `notified` without `notified_at`/`notice_reference` rejected. After the doc is `reported`, moving to `done` without `recovered_notified_at`/`recovery_notice_reference` rejected; with them accepted (R-24, R-35) |
| AT-37 | `csr_ready` unit; mock returns "OTP expired/invalid" | Admin enters OTP | Status stays `csr_ready`; `otp_rejected` event without OTP value; en/ar message; no automatic resubmission (R-7) |
| AT-38 | Profile `production`; only a `sandbox` credential active | Stamp / submit | Rejected (R-36) |
| AT-39 | Seller name long enough that QR base64 = 701 chars (each TLV value ≤ 255 bytes) | Stamp | Rejected; transaction rolled back; `last_icv` unchanged. 700 chars passes (R-37) |
| AT-40 | Issue date 2027-03-10, monthly tax period; doc A lines on 4200; doc B line on 1510 | Stamp | A: `retention_until` = 2033-03-31; B: class `capital_asset_real_estate`, 2042-03-31 (R-38) |
| AT-41 | Cleared 386 for buyer X in TST1 | Final 388 references it; then a 388 in TST2 references it | First: XML carries prepayment reference and deduction per R-39; second rejected (R-39, R-1) |
| AT-42 | TST1 USD invoice with `total_vat_sar` 562.50; a second USD invoice without it | Stamp | First: `TaxCurrencyCode` SAR; TaxTotal in USD with subtotals and a second TaxTotal in SAR 562.50 without subtotals; second rejected locally, `last_icv` unchanged (R-40) |
| AT-43 | Admin A with delegation `onboard_renew` for TST1 only; then the delegation is revoked | A generates CSR for TST1, then for TST2; after revocation A renews TST1 | TST1 CSR allowed; TST2 rejected; after revocation rejected; `delegation_granted` / `delegation_revoked` events exist with no secret values (R-41, D-3) |
| AT-44 | Standard invoice INV-1 rejected by the mock | `invoicing` creates the correction | New document with new number, UUID, ICV and a new issue date; INV-1's number and UUID never reused (R-42) |
| AT-45 | TST1 with full national address on `Entity`, profile enabled | Stamp a document | Seller name/address in XML come from `Entity` §3.0 fields in ar (and en where allowed); no address fields exist on `ZatcaProfile` (3.1) |
| AT-46 | Intercompany standard invoice TST1 → TST2 (TST2 as buyer) | Submit | Sent for clearance as a normal B2B invoice with TST2's VAT number as buyer; signed with TST1's EGS only (D-2, R-2) |
| AT-47 | Standard TST1 doc; mock clearance returns HTTP 303, mock reporting accepts | Queue runs | Attempt 1 outcome `clearance_disabled`; attempt 2 on reporting with the same request hash, ICV and UUID; status `reported_clearance_disabled`; `is_issued` False; no JE posted; TST1 admins alerted en/ar (R-44, R-20, R-22) |
| AT-48 | TST1 `Entity` with building number `754`, or postal code `2343`, or blank district; then complete address with blank additional number | Enable profile | First three rejected listing the field (en/ar); the last accepted (R-4) |
| AT-49 | Drafts with subtype flags: simplified + export; standard export + self-billed; export with a buyer VAT number; non-export buyer VAT `300000000000001` (ends with 1) | Submit | Each rejected locally; `last_icv` unchanged (R-46) |
| AT-50 | `csr_ready` unit | Admin enters OTP `12345`, then `12a456` | Both refused before any API call; no `otp_entered` event; mock not called (R-7) |
| AT-51 | TST1 SAR invoice | Stamp | XML has `TaxCurrencyCode` SAR, a TaxTotal in SAR with subtotals and a second TaxTotal in SAR without subtotals (R-40) |
| AT-52 | Cleared 386 in TST1: net 1,000.00 S 15%; final 388 net 4,000.00 S 15% deducting it | Stamp the 388 | One extra line with line amounts 0.00 and `DocumentReference` KSA-26 = 386 number, KSA-28/29 its date/time, KSA-30 `386`, KSA-31 1,000.00, KSA-32 150.00, KSA-33 `S`, KSA-34 15.00; BT-113 = 1,150.00; payable = BT-112 4,600.00 − 1,150.00 = 3,450.00 (R-39) |
| AT-53 | Active unit: doc A stamped, rejected by the mock; correction B stamped next | Stamp B | B's ICV = A's ICV + 1; B's PIH = A's hash (R-12, R-42) |
| AT-54 | TST1 credit note on invoice `TST1-INV-2027-000001`; a second note without `payment_means_code` | Stamp | First: BT-25 = `TST1-INV-2027-000001` (not a UUID); KSA-10 in `cac:PaymentMeans/cbc:InstructionNote` with BT-81 present. Second rejected locally, no ICV consumed (R-19) |
| AT-55 | Signed XML over 10 MB (fixture with a large attachment) | Submit | Rejected locally, stays `pending`, no ICV consumed, no API call (R-16) |
| AT-56 | Arabic seller name whose UTF-8 value is 256 bytes | Stamp | Rejected; transaction rolled back; `last_icv` unchanged; 255 bytes passes the TLV check (R-15) |
| AT-57 | TST1 and TST2 active; admin records VAT deregistration for TST1 | Stamp a TST1 doc, then a TST2 doc | TST1 credentials and units `revoked`, `vat_status_changed` event logged, stamp rejected; TST2 stamps normally (R-45) |
| AT-58 | Movable-asset document in TST1: issue date 2027-03-10, `acquisition_date` 2027-03-10, adjustment period set to 5 years in test settings (fixture value, not a statutory claim), monthly period | Stamp | `retention_until` = 2037-03-10 (= max(2033-03-31, 2027-03-10 + 5 + 5 years)); a later change of the setting never shortens it (R-38) |
| AT-59 | Stamped TST1 doc | Archive | File name = `<vat>_<yyyymmdd>_<hhmmss>_<IRN>.xml` from the doc's fields (R-30) |
| AT-60 | Stamped doc | Recompute the hash after changing only the QR value, the signature block or the XML declaration | Hash unchanged; changing any amount changes it (R-13) |
| AT-61 | *Added 2026-10-16 (D-10).* Fake key store set to "sealed"; TST1 and TST2 active; a stamped, unreported simplified TST1 doc; custodian K with active `unseal` delegation; custodian K2 whose `unseal` delegation is revoked | Stamp a new TST1 doc; queue worker runs; clock passes the reporting deadline; store set to "unsealed" | New doc stays `pending`, `last_icv` unchanged. Stamped doc `failed_retryable`, never `failed_final`, one `SubmissionAttempt` per try. One `secret_store_unavailable` event; alert (en/ar) to TST1 and TST2 admins, the MP and K, not K2. `late_reporting_notice` task opened for TST1. After unseal: new doc stamped, old doc `reported`, `secret_store_available` logged (R-47, R-24, R-35) |
| AT-62 | *Added 2026-10-16 (D-10).* Group parent PAR, child TST1; K is ZATCA admin on PAR and TST1 | MP grants K `unseal` on PAR; MP grants `unseal` on TST1; an admin who is not the MP grants `unseal` on PAR; DB and log scan | First allowed and logged (`delegation_granted`); second and third rejected; no unseal key, share or token in DB, logs or events (R-49, R-6) |
| AT-63 | *Added 2026-10-16 (D-11).* TST1 with fake CR `1010000001` and fake unified number `7000000001`; TST2 with fake unified number `7000000002` and no CR; TST3 with neither | Stamp a standard invoice for each | TST1: exactly one `cac:PartyIdentification/cbc:ID` for the seller, `schemeID="CRN"`, value `1010000001`; `7000000001` appears nowhere in the XML. TST2: one ID, `schemeID="700"`, `7000000002`. TST3: rejected locally, `last_icv` unchanged (R-48) |

## 9. Open questions

**B** = blocks building (or blocks go-live where noted); **D** = safe default exists, build can proceed with it.

| # | Question | Blocks? | Safe default if D |
| --- | --- | --- | --- |
| OQ-1 | Which entities are in which ZATCA integration wave, and their go-live dates? What does `invoicing` do for an entity before its go-live (Phase 1 only)? Note: a newly VAT-registered entity waits 2 business days before onboarding (DTG p.70). | D for build, **B for go-live** | Default approved (D-9): `go_live_date` null → no submission; Phase 1 QR generated by `invoicing`. Almiras not yet live; must be ready (D-1); date open |
| OQ-2 | Build in-house (this spec) vs connect to an external ZATCA middleware/provider through the integration layer? | — | **Resolved by D-1**: in-house, open source |
| OQ-3 | Secret storage for private keys and CSID secrets; who holds the master key; backup/restore? | D for build, **B for go-live** (remainder) | **Resolved by D-3**: OpenBao in KSA, Managing Partner master key, per-entity delegation (3.9, R-41). (a) Unseal model **resolved by D-10**: MP keeps the master key, no Shamir shares, no auto-unseal; availability risk accepted by the owner; mitigation = delegated `unseal` custodians (R-47, R-49). Still open from (a): OpenBao backup/restore and DR test; (b) how the design meets "keys must be marked as non-exportable" and disk encryption for software-held keys (SFIS req 6 p.11; IR Annex 1 p.11) — e.g. HSM/KMS-backed transit signing vs keys read out of a KV store **[Needs expert sign-off: security]**. Dev/tests: in-memory fake store only |
| OQ-4 | How many EGS units per entity (one per entity, per branch, per POS/till, per server)? | D | Resolved by D-7 / D-9: Almiras 1 (one branch); other entities default one per branch (invoicing OQ-35) |
| OQ-5 | Are any group entities registered as a VAT group (shared VAT number)? | — | **Resolved by D-2**: no VAT group; each entity (incl. associates and minority holdings) has its own VAT number and EGS unit(s). For reference if this ever changes: CSR OU = 10-digit TIN (§3.2 mapping) and joining a VAT group auto-revokes CSIDs (R-45) |
| OQ-6 | Correction path after rejection; does a rejected document stay in the PIH chain? | D | **Resolved by D-4**: corrected and re-issued under a new number; rejected number stays used (R-42). Answered by verification (Z20, Z23): the rejected document stays in the PIH chain (DTG p.48, 71, 72, 77; R-12); a re-issued standard document gets a new issue date (DTG p.78; R-42) — this refines D-4's re-issue, it does not change it. Remainder **[Needs expert sign-off: tax]**: whether a simplified copy already handed to the buyer also needs a credit note (DTG FAQ p.77 vs p.71 conflict); default: re-issue only, no credit note (nothing was posted) |
| OQ-7 | Behaviour when B2B clearance is unavailable. Official guidance (verification Z21): (a) if ZATCA disables clearance, the API returns 303 and documents go to reporting with no stamp/QR returned (DTG p.51, 70; DPM p.64) — handled by R-44; (b) if ZATCA servers are down: retry ~5 min, then share the uncleared invoice and keep records, no notification to ZATCA needed, retry ~every 15 min; the invoice "will not be fully compliant but will be considered as VAT invoice" and is not eligible for buyer VAT deduction (DG §10 p.47, 49; DTG p.78). Decide whether Almiras entities ever share uncleared invoices (b), and whether a `reported_clearance_disabled` document counts as issued and is posted (a). **[Needs expert sign-off: tax]** | D | Default approved (D-9): wait; never issue uncleared; alert. R-44: `reported_clearance_disabled` not issued, not posted. Expert sign-off still open |
| OQ-8 | Retention: which records are `capital_asset_real_estate`; who may approve purging. 6 years from the end of the tax period and 15 years for real estate are cited (TIR §8.3.3 p.42; IR Annex 1 p.10); the real-estate start point is not stated **[Needs expert sign-off: tax]**. Movable capital assets: OQ-31. | D | Default approved (D-9): R-38; no purge job; real-estate start = end of tax period (assumption). Tax-period frequency now held on `Entity.vat_return_period` (Almiras quarterly, D-7) |
| OQ-9 | Renewal lead time N and PCSID validity length. Validity is "up to 60 months (5 years)" in the illustrative profile (SFIS p.7, 14); the actual `valid_to` is read from each certificate; the portal also sends a reminder before expiry (DTG p.74). | D | N = 30 days |
| OQ-10 | Retry policy limits and alert recipients. | D | R-24 defaults; alert entity ZATCA admins |
| OQ-11 | Library choice for signing, C14N, schematron and QR (§6 options). Is the ZATCA SDK acceptable in CI only? The builder must prove C14N 1.1 output byte-for-byte (lxml built-in is 1.0/2.0; Z43). | D | `cryptography` + `lxml` + `segno`; schematron choice deferred to builder with reviewer sign-off |
| OQ-12 | Exact handover contract with `invoicing.md`: model name(s), who assigns the human invoice number, when `submit_document` is called, buyer data fields; ownership of ICV/PIH/hash/QR fields (invoicing OQ-2). | **B** | — (seller data now settled: read from `Entity`, D-7) |
| OQ-13 | 386 prepayment is now in scope (R-39), linked to `invoicing.md` advances (OQ-16 there). Still open: export, third-party, nominal, summary, self-billed invoices. | D | 388/381/383/386 standard and simplified only |
| OQ-14 | Role model and approvers: who is ZATCA admin per entity; is a second approver needed to switch to production or revoke? | D | Superuser only for onboarding; second approver required for production |
| OQ-15 | Offline B2C behaviour: may a POS/branch issue simplified invoices while offline and report later within 24 h; local stamping on device or central server? | D | Central server stamping only; no offline POS |
| OQ-16 | Arabic translations of ZATCA error codes: who maintains the table? | D | Show English text with Arabic "untranslated" label |
| OQ-17 | Intercompany invoices between group entities: confirm no special treatment. | — | **Resolved by D-2**: normal B2B clearance, counterparty entity as buyer (AT-46) |
| OQ-18 | Time source and timezone for `issue_datetime`. KSA-25 accepts `hh:mm:ssZ` (UTC) or `hh:mm:ss` (AST); issue date ≤ current date (BR-KSA-04, XML v1.2 p.47, 58); changing the system date is prohibited (DG p.27). No clock tolerance is stated **[SDK-confirm]**. | D | Server UTC stored (NTP), emitted as UTC with `Z`, Asia/Riyadh rendered |
| OQ-19 | Should the module back-fill Daftra-era invoices? (Likely no; confirm with migration plan.) | D | No back-fill |
| OQ-20 | Data residency. | — | **Resolved by D-5**: DB, archive, backups, logs and secret store in KSA (R-43). Verification (Z39): KSA hosting of VAT records is a **VAT requirement** for KSA-resident taxpayers ("the computer or server must be located within the Kingdom", TIR §8.3.1 p.41; DG §5.4–5.5 p.26–27), not only a business choice; cloud copies must be reachable by the Authority through a direct link. D-5 already satisfies it. PDPL offshore processors: **[Needs expert sign-off: legal]** still open (invoicing OQ-31, go-live) |
| OQ-21 | Late-reporting notice (R-35): who files the ZATCA website form per entity (failure and recovery notices), and what evidence is kept. | D | Default approved (D-9); recovery step added by verification (Z19) |
| OQ-22 | VAT in SAR: answered for the XML shape by R-40 (BR-KSA-68, BR-KSA-EN16931-02/09, BR-53). Still open: QR tags 4/5 for non-SAR documents (tag 5 = BT-110 in document currency) **[SDK-confirm]**. | D | SAR VAT total from `invoicing`; local rejection if missing; non-SAR blocked until ledger-fx is built |
| OQ-23 | *Withdrawn — see Decision D-2* (subsidiaries' own VAT numbers and EGS units: confirmed). | — | — |
| OQ-24 | Seller ID scheme on XML: `CRN` (branch CR) or `700` (unified number) (invoicing OQ-33). Answered by verification (Z2): BR-KSA-08 requires exactly one ID chosen in the order CRN, MOM, MLS, 700, SAG, OTH (XML v1.2 p.48–49), so CRN is used whenever the entity has one. | — | **Resolved by D-11**: XML `CRN` from `Entity.cr_number` (branch CR) only; unified number on the human-readable invoice (R-48; invoicing R72) |
| OQ-25 | Who may grant delegations if the Managing Partner is unavailable; maximum delegation length. Now also bears on availability (D-10): if the MP is unavailable and no `unseal` custodian is named, nobody can unseal. | D | Managing Partner only; no expiry unless `valid_until` set |
| OQ-26 | *Added 2026-10-16 (Z34).* Does a "simulation" environment exist? The downloaded docs name only the sandbox and production. **[SDK-confirm]** | D | Keep the `simulation` choice, unused and unselectable in UI until confirmed |
| OQ-27 | *Added 2026-10-16 (Z15).* May one credit/debit note reference several original invoices (BT-25 is 1..n and may be a range, DD BT-25; DG p.19, 62)? Business decision, shared with invoicing OQ-43. | D | One original per note (single FK, R-19) |
| OQ-28 | *Added 2026-10-16 (Z7, Z10–Z12, Z26).* Signature and QR points to settle against SDK output before building R-14/R-15: curve (secp256k1 per DTG vs P-256 per SFIS); whether the raw invoice hash or canonical SignedInfo is signed; SFIS-only XAdES elements; SignedProperties Reference Type URI; QR tag 6 encoding (base64 text vs 32 raw bytes); CSR invoice-type and serial-number OIDs. **[SDK-confirm]** | **B** for R-14/R-15 build | Targets as written in R-14/R-15/§3.2; builder records the SDK result here |
| OQ-29 | *Added 2026-10-16 (Z30).* Is a 386 sample required in compliance checks? **[SDK-confirm]** | D | Run the 3/6 samples of R-8; add a 386 sample if the sandbox accepts it |
| OQ-30 | *Added 2026-10-16 (Z34).* Paths of the compliance CSID, compliance check and production CSID APIs; base URLs per environment; whether the clearance/reporting body also needs `uuid`. Only in ZATCA's Swagger, not in the downloaded docs. **[SDK-confirm]** | D | Values held in configuration, taken from the Swagger at build time; body = documented fields plus `uuid` only if the Swagger lists it |
| OQ-31 | *Added 2026-10-16 (Z38).* Movable capital assets: which documents get `movable_capital_asset`, the Art. 52 adjustment period, and the acquisition date source. **[Needs expert sign-off: tax]** | D | Class set only by explicit flag from `invoicing` (no automatic trigger); period is a setting; R-38 never earlier than the standard date; no purge job |
| OQ-32 | *Added 2026-10-16 (D-10).* Unsealing one shared OpenBao opens every entity's secrets, so an `unseal` delegation cannot be per entity (D-3). Accept a store-wide `unseal` delegation recorded on the group parent (R-49), or run one store per entity (or per partly-owned / minority entity, cf. access OQ-3)? | D | Store-wide, recorded on the group parent (R-49); Managing Partner to confirm |

**Before building:** OQ-12 (hand-off contract with `invoicing.md`) blocks building; OQ-28 blocks building R-14/R-15 until the SDK comparison is done. All other [SDK-confirm] items (OQ-18, OQ-22, OQ-26, OQ-29, OQ-30; §3.2 OIDs; §3.4 381 root; R-31 schematron) are settled by the builder against the SDK/sandbox and recorded here. **Before go-live:** OQ-1 (go-live date), OQ-3 remainder (OpenBao backup/restore and DR test; non-exportable keys — security sign-off; unseal model settled by D-10, availability risk accepted), tax sign-off on OQ-6 remainder, OQ-7, OQ-8 real-estate start point and OQ-31; legal sign-off on PDPL (OQ-20 / invoicing OQ-31) and the SDK licence (§6).
