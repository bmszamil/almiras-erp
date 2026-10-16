# Billing: contract-based recurring and automatic invoicing

Django app: `billing`. Status: **draft spec**. Written 2026-10-16 by the spec-writer agent. Revised 2026-10-16 (project-owner decisions D8 to D10: auto-issue, retroactive amendment/termination with correction notes, no mirror bill).
Depends on: `core` (Entity), `docs/specs/invoicing.md` (sales documents, customers, VAT; being revised in parallel, so concepts are referenced, not copied), `docs/specs/zatca.md` (clearance and reporting), `docs/specs/ledger-fx.md` (currencies and rates; parallel spec, **not in the repo yet**), HR module (does not exist; interface defined in 3.6).
**[CC-verify]** = tax, ZATCA or regulatory statement not checked against official text. The compliance-checker must verify it before module-builder starts.

## 1. Purpose

The billing module holds customer contracts for **every entity** in the group (Almiras, subsidiaries and any entity added later as an `Entity` row). It covers leases (commercial and residential), vehicle rentals, staff secondments, services (consulting, IP licences) and other recurring charges, for third-party customers and for other group entities (intercompany). From each approved contract it builds a billing schedule. On each due date it creates sales invoices automatically, through `invoicing.services` only, with `source="system"`. It never bills the same period twice. It prorates partial periods. It stops billing when a contract ends, is terminated, or (for secondments) when the HR assignment ends. It creates and posts no journal entries itself: invoicing posts them, and sales documents go through ZATCA as described in invoicing.md. Each entity's Sharia setting applies to every contract line, every user-facing text is in English and Arabic, and no contract, schedule or run crosses entity boundaries.

## 2. Decisions

| # | Decision | Who | Date |
|---|---|---|---|
| D1 | Some invoices must be generated automatically: (a) cars rented on annual contracts; (b) rent on commercial and residential leases; (c) seconded staff billed to the receiving entity until HR ends the secondment. | Project owner | 2026-10-16 |
| D2 | Group entities invoice each other often (intercompany rentals, IP, secondments, consulting). No group entity is in a VAT group, so intercompany invoices are normal VAT invoices that carry the counterparty entity. (This settles invoicing.md OQ-13 and zatca.md OQ-5 / OQ-17 for billing.) | Project owner | 2026-10-16 |
| D3 | Multi-currency is needed (SAR, CNY, EGP, GBP, EUR, USD, AED, and others). Currencies and rates are covered by `docs/specs/ledger-fx.md`. | Project owner | 2026-10-16 |
| D4 | A contract may have an advance, a security deposit, both, or neither. | Project owner | 2026-10-16 |
| D5 | Roles: accounting staff create; the CFO approves; posting is automatic on approval. Sales documents post only after ZATCA clearance or reporting. | Project owner | 2026-10-16 |
| D6 | Until the HR module exists, accounting staff record the secondment end date. | Project owner (task brief) | 2026-10-16 |
| D7 | Rules fixed by CLAUDE.md apply: `entity` FK on every record; queries filtered by entity and permission; new subsidiaries are data; en/ar text; `Decimal` money with explicit currency; posting only via `ledger.services`; open source only. *(Rule 7 amended 2026-10-16: SAR 2 dp, foreign transaction amounts up to 3 dp; effect here: OQ-29.)* | CLAUDE.md | in repo as of 2026-10-16 |
| D8 | Invoices auto-generated from a CFO-approved contract need **no per-invoice approval** (resolves OQ-1; invoicing OQ-36). | Project owner | 2026-10-16 |
| D9 | A contract must stay modifiable and cancellable after approval, **including retroactively**, because staff may forget to tell the ERP that a contract or service was cancelled, cut short or modified. So: (a) contract amendments are versioned, CFO-approved, and the effective date may be in the past; (b) early termination or cancellation may have a past effective date; (c) for periods already billed, issued invoices are **never edited or deleted**: the system generates **draft** credit notes for over-billed amounts (debit notes for increases), a person reviews them and the CFO approves them; (d) posted-period rules still apply: corrections are dated in an open period. | Project owner | 2026-10-16 |
| D10 | Intercompany: by default **no** automatic bill is created in the buyer entity; the buyer entity's staff enter it (resolves OQ-11; invoicing OQ-37). | Project owner | 2026-10-16 |

## 3. Data model

Conventions (same as invoicing.md): every model has `entity` FK → `core.Entity` (`on_delete=PROTECT`, required); money is `DecimalField(18, 2)`; every model has `created_by`, `created_at`, `updated_at`, `source` (`human` / `import` / `agent:<name>` / `system`). "Bi" = field exists as `_en` and `_ar`.

### 3.1 Contract (unique `(entity, number)`)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Seller / lessor / seconding entity |
| number | Char(40) | Y at approval | | Gap-free. Default format `{entity.code}-CTR-{year}-{000001}` (OQ-14) |
| contract_type | Choice: `lease`, `vehicle_rental`, `secondment`, `service`, `other` | Y | | |
| customer | FK invoicing.Customer (same entity) | Y | | Invoices are issued to this customer |
| counterparty_entity | FK Entity, null | computed | | Copied from `customer.group_entity`. Set → intercompany. Never equals `entity` |
| title / description | Char(200) / Text | Y / Y | Bi | Arabic required |
| currency | Char(3) ISO 4217 | Y | | Contract and invoice currency. Validated against ledger-fx currency list |
| start_date / end_date | Date / Date null | Y / N | | `end_date` null allowed only for `secondment` and `service` (OQ-9) |
| billing_frequency | Choice: `monthly`, `quarterly`, `semiannual`, `annual` | Y | | |
| billing_timing | Choice: `advance`, `arrears` | Y | | See R14 |
| period_anchor | Choice: `calendar`, `contract_start` | Y | | Default `calendar` (OQ-3) |
| bill_lead_days | PositiveSmallInt | Y | | Default 0. Days before `bill_date` the run may issue (OQ-5) |
| due_days | PositiveSmallInt, null | N | | Invoice due date = issue date + `due_days`; null → invoicing default |
| lease_use | Choice: `residential`, `commercial`, null | Y if `lease` | | Drives VAT category (R24) |
| property_ref / unit_ref | Char(60) | Y if `lease` | | Free text until an asset register exists |
| ejar_contract_no | Char(40) | N | | Recorded only. Integration open (OQ-10) |
| vehicle_ref (plate / asset code) | Char(40) | Y if `vehicle_rental` | | |
| advance_terms | FK AdvanceTerm, null | N | | 3.4 |
| deposit_terms | FK DepositTerm, null | N | | 3.4 |
| status | Choice: `draft`, `approved`, `active`, `ended`, `terminated` | Y | | R6 |
| approved_by / approved_at | FK User / DateTime | at approval | | CFO of this entity |
| terminated_on / termination_reason | Date / Text | Y if `terminated` | Bi (reason) | Last billable day (inclusive). Null for `cancellation`. Set only by an approved termination/cancellation version (R40) |
| termination_kind | Choice: `early_termination`, `cancellation`, null | Y if `terminated` | | `cancellation` = nothing was ever billable (R40). Added 2026-10-16 (D9) |
| current_version | FK ContractVersion | Y after approval | | Latest approved version (3.9). Added 2026-10-16 (D9) |
| requires_purification | Bool | computed | | Any line on an account with `requires_purification` |

### 3.2 ContractLine

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Equals `contract.entity` |
| contract | FK Contract | Y | | |
| version | FK ContractVersion | Y | | Lines belong to one version; approved versions are immutable (R38). Added 2026-10-16 (D9) |
| line_key | UUID | Y | | Same value for "the same line" across versions, so corrections compare like with like (R42). A line absent from a later version is billable 0 from its effective date |
| line_no | PositiveInt | Y | | |
| description | Char(300) | Y | Bi | Arabic required (invoicing default OQ-18). Period text is appended at generation |
| amount | Money (contract currency) | Y | | Net amount per **full** billing period. > 0 |
| unit_code | Char(3) | Y | | UN/ECE rec 20, e.g. `MON`, `ANN`, `C62` |
| vat_category | Choice `S`/`Z`/`E`/`O` (invoicing `VatRate.category`) | Y | | Rate is resolved by invoicing at supply date (invoicing R10) |
| vat_exemption_reason | FK invoicing.VatExemptionReason | Y if Z/E/O | | |
| income_account | FK ledger.Account (same entity, income, postable, not Sharia-prohibited where enforced) | Y | | Defaults in R27 |
| secondment | FK SecondmentAssignment, null | Y if contract type `secondment` | | One line per seconded person |

### 3.3 ContractPriceStep (escalation)

| Field | Type | Req | Notes |
|---|---|---|---|
| entity, line | FK | Y | Same entity |
| effective_from | Date | Y | Must fall inside the contract dates |
| new_amount | Money | Y | Replaces `line.amount` for periods starting on or after `effective_from` |

Only explicit stepped amounts are supported until escalation types are decided (OQ-4). A step that starts inside a billing period applies from the next period (default, OQ-4).

### 3.4 AdvanceTerm and DepositTerm

| Model | Fields | Notes |
|---|---|---|
| AdvanceTerm | entity, amount (Money), currency, due_date, recovery: `offset_against_invoices` / `none`, notes (Bi) | Invoicing blocks advances (invoicing R55 / OQ-16). Recorded only (R30) |
| DepositTerm | entity, amount, currency, due_date, refundable (Bool), refund_conditions (Bi) | Memo only. No invoice, no posting from billing (R31) |

### 3.5 BillingPeriod (the schedule; unique `(contract, period_start)`)

| Field | Type | Req | Notes |
|---|---|---|---|
| entity | FK Entity | Y | Equals `contract.entity` |
| contract | FK Contract | Y | |
| period_start / period_end | Date | Y | Inclusive. Supply period on the invoice |
| bill_date | Date | Y | `advance`: `period_start`; `arrears`: `period_end` + 1 day |
| proration_factor | Decimal(9,6) | Y | 1 for full periods (R16) |
| status | Choice: `planned`, `generated`, `held`, `failed`, `cancelled` | Y | R10 |
| hold_reason / failure_reason | Char(300) | N | Bi |
| sales_document | FK invoicing.SalesDocument, null, PROTECT | N | Current document for this period |
| superseded_documents | M2M invoicing.SalesDocument | N | ZATCA-rejected earlier attempts (R12) |
| credit_document | *Withdrawn — see D9* (corrections are `BillingAdjustment` rows, 3.10; a period may have several) | | |
| billed_version | FK ContractVersion, null | Y when `generated` | Version in force when the invoice was generated. Never changed afterwards (R41). Added 2026-10-16 (D9) |

### 3.6 SecondmentAssignment (HR interface)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Seconding (billing) entity |
| receiving_entity | FK Entity, null | N | | Equals `contract.counterparty_entity` when intercompany |
| hr_assignment_ref | Char(60), null | N | | Future FK to the HR assignment. Unique per entity when set |
| employee_ref | Char(40) | Y | | Pseudonymous employee code, no name or ID number (PDPL; OQ-12) |
| role_title | Char(200) | Y | Bi | Shown on the invoice line |
| start_date / end_date | Date / Date null | Y / N | | `end_date` stops billing (R19) |
| end_recorded_by / end_recorded_at / end_source | FK User / DateTime / Choice `manual`, `hr_event` | at end | | |

Interface the HR module must call (until then, the accounting-staff UI calls it):
`billing.services.end_secondment(entity, assignment, end_date, user, source)`. It is idempotent: the same end date twice is a no-op. A different end date applies the earlier or later date per R20.

### 3.7 BillingRun and BillingRunItem

| Model | Fields | Notes |
|---|---|---|
| BillingRun | entity (null = all entities the runner may bill, one item set per entity), run_date (Asia/Riyadh), trigger (`scheduler` / `manual`), started_by (system or human user), started_at, finished_at, status (`running`, `completed`, `completed_with_errors`, `aborted`), counts (generated, skipped, held, failed) | Append-only after finish |
| BillingRunItem | entity, run, billing_period, outcome (`generated`, `skipped_already_billed`, `held`, `failed`), sales_document (null), message_en / message_ar | One row per period attempted. Append-only |

### 3.8 ContractEvent (audit, append-only)

entity, contract, action (`created`, `submitted_for_approval`, `approved`, `returned`, `activated`, `amended`, `secondment_ended`, `ended`, `terminated`, `period_released`; added 2026-10-16 (D9): `version_submitted`, `version_approved`, `version_rejected`, `cancelled`, `corrections_computed`, `correction_drafted`, `correction_submitted`, `correction_discarded`, `correction_superseded`), actor, at, detail JSON (never personal data beyond `employee_ref`).

### 3.9 ContractVersion (added 2026-10-16, D9; unique `(contract, version_no)`)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Equals `contract.entity` |
| contract | FK Contract | Y | | |
| version_no | PositiveInt | Y | | 1 = terms approved with the contract (R8). Gap-free per contract |
| kind | Choice: `original`, `amendment`, `termination`, `cancellation` | Y | | |
| effective_date | Date | Y | | First day the new terms apply. **May be in the past** (D9). ≥ `contract.start_date`. `original` and `cancellation`: = `start_date`. `termination`: = `terminated_on` + 1 day |
| end_date, billing_frequency, billing_timing, period_anchor, due_days | as Contract 3.1 | Y | | The version's terms. Limits on retroactive change: R39 |
| lines | ContractLine rows with `version` = this (with their ContractPriceStep rows) | Y unless termination/cancellation | | Full set, not a diff |
| reason_en / reason_ar | Text | Y (ar) / N (en) | Bi | Arabic required: it becomes the reason on correction notes (invoicing R18/R19) |
| status | Choice: `draft`, `submitted`, `approved`, `rejected`, `withdrawn` | Y | | Only `draft` is editable; `approved` is immutable (R38) |
| created_by, submitted_by, approved_by, approved_at, rejected_reason (Bi) | | | | Approver = CFO of the entity, not creator or submitter (R39) |

### 3.10 BillingAdjustment (added 2026-10-16, D9; one correction item)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Equals `contract.entity` |
| contract / version | FK Contract / FK ContractVersion | Y | | `version` = the approved version (or secondment end, R49) that caused it |
| billing_period | FK BillingPeriod (status `generated`) | Y | | |
| original_document | FK invoicing.SalesDocument (invoice of that period) | Y | | Becomes the note's `original_document` (BT-25) |
| kind | Choice: `credit`, `debit` | Y | | `credit` = over-billed (draft credit note); `debit` = under-billed (draft debit note) |
| lines | JSON: per `line_key`: required net, billed net, delta (contract currency, `Decimal`) | Y | | Computed by R42; read-only |
| supply_start / supply_end | Date | Y | | Part of the original period affected (≥ `effective_date`) |
| document | FK invoicing.SalesDocument (credit/debit note), null, PROTECT | N | | Draft created via invoicing (R43) |
| status | Choice: `waiting_original`, `drafted`, `submitted`, `issued`, `final`, `discarded`, `superseded`, `failed` | Y | | R44 to R48 |
| reviewed_by / discard_reason (Bi) | FK User / Text | N | Bi | Human reviewer; discard needs a bilingual reason (R48) |

Unique: one non-terminal (`waiting_original`, `drafted`, `submitted`) adjustment per (`billing_period`, `original_document`, `kind`).

### 3.11 Billed position (computed, not stored)

`billed_net(period, line_key)` = net of that line on the period's current invoice + Σ net on its debit notes − Σ net on its credit notes, counting notes with status `issued` or `final`, plus adjustments in `waiting_original`, `drafted` or `submitted` (so a pending correction is never computed twice, R46). Notes that ZATCA rejected do not count.

## 4. Rules

"svc" = `billing.services`; "DB" = database constraint; "inv" = enforced by invoicing services; "ledger" = existing ledger code; "Q" = queryset/permission filter.

### Entity separation
1. A contract, its lines, steps, terms, schedule, assignments and runs all have the same `entity`. Customer and income accounts belong to that entity. **[svc `clean` on save; ledger `validate_entry` at posting; AT-14]**
2. Views and APIs list billing records filtered by entity and by the user's entity permissions. A run for entity A never reads or writes B's records. **[Q; svc run loop scoped per entity; AT-14]**
3. `counterparty_entity` comes only from `customer.group_entity`. It cannot be set by hand and never equals `entity`. **[svc; model `clean`; AT-11]**
4. The system never creates records in the counterparty entity's books: no automatic mirror bill; the buyer entity's staff enter the bill (D10). This also holds for correction notes (R50). *(Amended 2026-10-16: was "until OQ-11 is decided".)* **[svc; AT-11, AT-32]**
5. A new entity bills with no code change once it has its chart, periods, customers and an approved contract. **[AT-15]**

### Contract lifecycle
6. Status runs `draft → approved → active → ended`, or `active → terminated` (also `approved → terminated` before start). No other transitions exist. **[svc state machine; AT-1]**
7. Only `draft` contracts can be edited or deleted. After approval, price, dates, lines and VAT are changed only through a CFO-approved `ContractVersion` (D9, R38 to R41); termination and cancellation are versions too (R40). Secondment end dates (R19, R49) are the only change without CFO approval (D6). *(Amended 2026-10-16 by D9.)* **[model `save` guard + svc; AT-1, AT-22]**
8. Approval needs the CFO role on the contract's entity. The approver is not the creator (default, OQ-8). Approval assigns the contract number and builds the schedule in one transaction. **[svc; AT-1]**
9. A contract becomes `active` on approval if `start_date ≤ today`, otherwise on the first run on or after `start_date`. It becomes `ended` when `end_date` has passed and every non-cancelled period is `generated`. **[svc run; AT-6]**

### Schedule and idempotency
10. The schedule is materialised at approval: one `BillingPeriod` per period up to `end_date`, or a rolling 13 months for open-ended contracts (extended by each run). Status flow: `planned → generated`; `planned → held → planned` (human release); `planned → failed → planned` (automatic retry next run); `planned → cancelled`. A `generated` period never returns to `planned` (R12 is the only re-generation path). **[svc; DB unique `(contract, period_start)`; AT-2]**
11. A period is billed at most once. The generator locks the `BillingPeriod` row (`select_for_update`) and skips it unless the status is `planned`. Runs also take a PostgreSQL advisory lock per entity, so two runners never work on the same entity at once. A re-run, or a crash and re-run, creates no second invoice. **[svc + DB unique + lock; AT-2, AT-3]**
12. If ZATCA rejects a generated invoice (invoicing R42: document `rejected`), the period goes to `failed` and the document moves to `superseded_documents`. Re-generation needs a human release after the cause is fixed. It creates a new document with a new number. **[svc callback; AT-17]**
13. The generator creates and issues documents only through `invoicing.services` (the create and issue functions named in invoicing.md, OQ-2 there), with `source="system"` and the billing service user. It never writes `SalesDocument` or `JournalEntry` rows directly. **[svc; code-reviewer; AT-4]**

### Dates and amounts
14. `bill_date`: `advance` → `period_start`; `arrears` → day after `period_end`. A period is due when `bill_date - bill_lead_days ≤ run_date`. **[svc; AT-4]**
15. `issue_date` is the actual run date (Asia/Riyadh). It is never backdated **[CC-verify]**. `supply_date` = `period_start` and `supply_end_date` = `period_end` (OQ-5). **[svc; AT-4]**
16. Proration (default, OQ-3): actual days. `factor = days in the partial period / days in the full period that contains it`, inclusive. Line net = `round(amount × factor, 2)`, ROUND_HALF_UP, computed in `Decimal`. A float input is rejected. VAT is then computed by invoicing at document level (invoicing R9). **[svc; AT-5]**
17. A first or last period is partial when `start_date` or the effective end date falls inside a period. With `period_anchor=contract_start`, only the last period can be partial. **[svc; AT-5, AT-6]**
18. The amount for a period is the line amount (or the latest `ContractPriceStep` with `effective_from ≤ period_start`) × factor. **[svc; AT-18]**

### Secondment, termination and end
19. A secondment line is billed only for days from `assignment.start_date` to `assignment.end_date` inclusive. Recording an end date cancels `planned` periods that start after it and prorates the period that contains it. **[svc `end_secondment`; AT-7]**
20. *Withdrawn — see D9 (replaced by R42 to R49; secondment case R49).*
21. *Withdrawn — see D9 (replaced by R40 to R48).*
22. A contract that has `ended` or been `terminated` produces no further invoices. **[svc; AT-6]**

### Closed periods and failures
23. If no open fiscal period covers the run date, the item fails before the invoice is issued. No document number is consumed (invoicing R28). The period stays `failed` with reason "period closed / missing" (en/ar) and is retried next run. Catch-up periods whose supply period lies wholly in a **closed** fiscal period are put on `held` for a human to release (default, OQ-13). Billing never reopens a period. **[svc + ledger `_find_open_period`; AT-10]**

### VAT, leases, vehicles, intercompany
24. A `lease` line with `lease_use=residential` must be category `E` with an exemption reason. With `commercial` it must be `S`. A mismatch is rejected at save and at approval **[CC-verify]**. Mixed-use leases: OQ-15. **[svc; AT-12]**
25. Vehicle rental and secondment lines default to `S`. Other categories need an exemption reason (invoicing R10). **[svc + inv]**
26. Intercompany contracts produce normal VAT invoices with the counterparty entity (D2). Invoicing posts AR to 1210 with `counterparty = counterparty_entity` (invoicing R4). **[inv + ledger; AT-11]**
27. Default income accounts by type: lease and vehicle → 4100 (third party) / 4110 (intercompany); secondment and consulting → 4200 / 4210; IP licence → 4300 (third party). Intercompany IP has no account yet (invoicing OQ-22), so an intercompany IP line is rejected until it exists. Staff may choose another postable income account of the same entity. **[svc; AT-11]**
28. Rent invoices on investment property are handed to invoicing with `retention_class=capital_asset_real_estate` (zatca.md R-38). **[svc → inv]**

### Sharia
29. A contract line cannot use a `sharia_prohibited` account in a Sharia-enforcing entity. No escalation, late fee or charge may be linked to an interest rate or index, or to a late-payment penalty (invoicing R7). Lines on `requires_purification` accounts are allowed but flag the contract and every generated invoice. **[svc at save and approval; ledger at posting; AT-13]**

### Advances and deposits
30. Advance terms are recorded. A contract whose advance is to be offset against periodic invoices cannot be approved until the invoicing prepayment (386) flow exists (invoicing R55 / OQ-16; zatca.md R-39). **[svc; AT-16]**
31. Deposit terms are a memo only. Billing creates no invoice or entry for a deposit (no deposit liability account exists; VAT treatment needs a tax adviser). **[svc; AT-16]**

### Currency
32. The invoice currency is the contract currency. When it differs from `entity.base_currency`, the rate and base-currency amounts come from ledger-fx services. The default rate date is the invoice issue date (OQ-16). If no rate exists, or ledger-fx / invoicing multi-currency is not built yet (invoicing OQ-6), the item fails with a bilingual message and no document is created. **[svc + ledger-fx + inv; AT-19]**

### Runs, notifications, approval of generated invoices
33. Each period is generated in its own transaction. One failure never stops the others or rolls them back. Every attempt writes a `BillingRunItem` with an en/ar message. **[svc; AT-17]**
34. After each run, the entity's accounting staff get a summary (generated, held, failed) in the app, in their language. Failures still unresolved after 3 consecutive runs are also sent to the CFO (default, OQ-17). **[svc; AT-17]**
35. Generated invoices are issued without per-invoice approval: the CFO's contract approval covers them (D8; was default OQ-1). This applies to invoices only, never to correction notes (R44). *(Amended 2026-10-16.)* **[svc; AT-4, AT-35]**
36. Agents (gateway service users) may draft contracts if granted. They cannot approve, release, terminate, end secondments or start runs. The billing service user is a system user, not an agent, and it acts only on `active` contracts. **[Q + svc; AT-20]**
37. All amounts are `Decimal` with 2 dp, and the currency is stored on every money field's parent record. **[svc + DB; AT-5]**

## 5. Accounting

Billing posts nothing. Each generated invoice is posted by invoicing (`create_entry` at issue, `post_entry` after ZATCA clearance/reporting; invoicing R41) on accounts from `ledger/coa_template.py`. The tables show the entries that result. Amounts are in the entity's base currency (SAR).

**B1. Commercial lease, monthly in advance, third party** (rent 10,000.00, S 15%)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1200 Trade receivables | 11,500.00 | | |
| 4100 Rental income | | 10,000.00 | |
| 2200 VAT output | | 1,500.00 | |

**B2. Residential lease, exempt** (rent 8,000.00, E): Dr 1200 8,000.00 / Cr 4100 8,000.00. No 2200 line.

**B3. Intercompany secondment, ALM → SUB, March in arrears, ended 2027-03-10** (31,000.00 × 10/31 = 10,000.00, S 15%)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1210 Due from group entities | 11,500.00 | | SUB |
| 4210 Intercompany service income | | 10,000.00 | |
| 2200 VAT output | | 1,500.00 | |

**B4. Intercompany vehicle rental or lease**: as B3 with 4110 Intercompany rental income.

**B5. Foreign-currency contract** (USD 1,000.00 net, S 15%, fixture rate 3.75 SAR/USD from ledger-fx): Dr 1200 4,312.50 / Cr 4100 3,750.00 / Cr 2200 562.50. How the currency amount and rate are stored on the JE is defined by ledger-fx.md (`JournalLine` has no currency field today).

**B6. Credit note after early end** (R20/R21; mirror of the invoice for the unused days): Dr 4100/4110/4200/4210, Dr 2200 (if S), Cr 1200 or 1210 (+ counterparty). Issued and posted by invoicing (invoicing E4).

**No entry** for: contract approval, schedule creation, runs, held or failed periods, advance terms, deposit terms.

**Accounts missing** (not invented here; see invoicing OQ-22): deferred income for multi-month in-advance billing (OQ-2), customer advances, security deposits held, intercompany IP income, FX gain/loss.

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| `invoicing` (internal) | Out | Create and issue sales invoice (lines, supply period, customer, currency, VAT category, account, `source="system"`); draft credit notes | invoicing must add `system` to its `source` choices (it lists human/import/agent) |
| `invoicing` (internal) | In | Document status (`final`, `rejected`) via callback or polling | Drives R12 |
| ZATCA (via invoicing → zatca only) | Out/In | Clearance (B2B, incl. intercompany) or reporting (B2C, e.g. residential tenants who are individuals) | No direct call from billing |
| `ledger-fx` (internal) | In | Currency list, rate for (currency, date) | Spec not in repo yet |
| HR module (future) | In | Assignment start/end events → `end_secondment` | Manual entry until HR exists (D6) |
| Ejar (Saudi rental platform) | none yet | Lease registration, rent schedules | OQ-10 **[CC-verify]** whether registration is required |
| Banks, GOSI, Mudad | none | Secondment payroll and GOSI stay with HR/payroll | n/a |
| SAP B1 / Daftra | In (one-off) | Existing contracts at cutover, already-billed periods marked `generated` with `source=import` | migration-agent; OQ-18 |

**Scheduled job runner (options, not decisions; OQ-19).** All open source.
| Option | Notes |
|---|---|
| Django management command `billing_run` + cron | Simplest. Idempotency (R11) makes re-runs safe |
| Same command + systemd timer | `Persistent=true` catches up missed runs after downtime; journald logs |
| Celery beat + worker (Redis/RabbitMQ broker) | Retries and monitoring; more infrastructure |
| django-q2, or Django's built-in tasks framework with a scheduler | Lighter; check maturity **[verify]** |

Whatever the runner, the job calls one service, `billing.services.run_billing(run_date, entity=None, user=<billing service user>)`, daily after 00:00 Asia/Riyadh.

## 7. Permissions

Granted **per entity**. The role model is missing from `core` (invoicing OQ-25).

| Action | Accounting staff | CFO | Accountant (invoicing) | Billing service user | Viewer / auditor | Agent |
|---|---|---|---|---|---|---|
| Create / edit draft contract | Y | Y | Y | | | Draft only, if granted |
| Approve contract / amendment | | Y (not own) | | | | No |
| Record termination | Y | Y | Y | | | No |
| Approve termination | | Y | | | | No |
| Record secondment end date | Y | Y | Y | (HR events later) | | No |
| Start a manual run | Y | Y | Y | Y (scheduled) | | No |
| Release `held` / `failed` period | Y | Y | Y | | | No |
| Generate and issue invoices from active contracts | | | | Y (only) | | No |
| Issue credit note drafts from R20/R21 | | Y | Y | | | No |
| View contracts, schedules, runs | Y | Y | Y | | Y | Scoped, if granted |
| View `employee_ref` on secondments | Y | Y | Y | | Masked | No |

## 8. Acceptance tests

Fixtures: entities ALM (parent, SAR, Sharia enforced, VAT number) and SUB (child, SAR, Sharia enforced), template chart, 2027 monthly periods open unless stated, invoicing and ZATCA stubs (stub returns `cleared`/`reported` unless stated), ledger-fx stub, anonymised customers. Users: S (accounting staff ALM), C (CFO ALM), C2 (CFO SUB). All amounts `Decimal`.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | Draft ALM lease created by S | S approves; C approves; S edits the amount afterwards | S rejected (not CFO). C: status `approved`, number `ALM-CTR-2027-000001`, schedule built, `approved` event. Edit rejected (R7). `active → draft` rejected (R6). |
| AT-2 | Active monthly contract, March period due | Run on 2027-03-01, then run again the same day, then again the next day | One invoice for March. Second and third runs: item `skipped_already_billed`, no new document, no number consumed (R11). |
| AT-3 | Same as AT-2 | Two runs started at the same time | One invoice. The other run waits on the advisory lock or skips (R11). |
| AT-4 | Commercial lease ALM, 10,000.00/month, advance, from 2027-03-01 | Run 2027-03-01 | Invoice `source="system"`, issue date 2027-03-01, supply 2027-03-01 to 2027-03-31, line S, created via invoicing services. No per-invoice approval. After ZATCA `cleared`: JE as B1 (R13 to R15, R35). |
| AT-5 | Lease 10,000.00/month, `calendar` anchor, start 2027-01-15; amount given as float | Approve; run 2027-01-15; save float | January factor 17/31; net 5,483.87; February 10,000.00. Float rejected (R16, R37). |
| AT-6 | Contract end 2027-06-20, monthly in arrears | Runs through 2027-07-01, then run 2027-08-01 | June factor 20/30, net = round(amount × 20/30, 2). Status `ended` after the June invoice. August run: nothing generated (R9, R22). |
| AT-7 | ALM → SUB secondment, 31,000.00/month, arrears, S; S records end date 2027-03-10 | Run 2027-04-01 | March invoice net 10,000.00, VAT 1,500.00, JE as B3 with counterparty SUB. April onward `cancelled`. No later invoices (R19). |
| AT-8 | Secondment billed in advance; April invoice `final`; end date recorded as 2027-04-10 | `end_secondment` | Draft credit note for 20/30 of April. Not issued until an accountant issues it. Recording the same date again: no-op (R20, 3.6). |
| AT-9 | Annual vehicle rental, monthly in arrears; termination on 2027-05-15 recorded by S | Before and after C approves | Before: billing unchanged. After: May prorated 15/31; June onward `cancelled`; status `terminated` (R21). |
| AT-10 | Fiscal period 2027-03 closed; March period due | Run 2027-03-02. Separately: run on 2027-04-01 with a catch-up period whose supply period is wholly in closed 2027-02 | First: item `failed` "period closed" (en/ar), no document or number consumed, retried next run. Second: period `held`, no invoice until released (R23). |
| AT-11 | ALM customer with `group_entity=SUB`; IC rental contract 5,000.00 S | Approve and run | Contract `counterparty_entity=SUB` (not editable). Invoice is a standard VAT invoice; JE Dr 1210 5,750.00 (counterparty SUB) / Cr 4110 5,000.00 / Cr 2200 750.00. No record created in SUB. An IC IP line → rejected (R3, R4, R26, R27). |
| AT-12 | Residential lease line with category S; then E with exemption reason; commercial lease line with E | Save / approve | First and third rejected. Second accepted; invoice has no VAT; JE as B2 (R24). |
| AT-13 | ALM line on an account flagged `sharia_prohibited`; line on 4900 | Save; approve and run | First rejected. Second accepted; contract and invoice `requires_purification=True` (R29). |
| AT-14 | User with ALM rights only; SUB contract; ALM contract line on SUB's 4100; ALM contract with a SUB customer | List; save; run for ALM | Sees ALM only. Line and customer rejected. The ALM run creates nothing in SUB (R1, R2). |
| AT-15 | New entity NEW added as data with chart, periods, customer, contract approved by its CFO | Run | Invoice generated with `NEW` numbering, no code change (R5). |
| AT-16 | Contract with an advance offset against invoices; another with only a refundable deposit | Approve each | First rejected "blocked until prepayment flow" (en/ar). Second approved; no deposit invoice or entry (R30, R31). |
| AT-17 | Run with three due periods; invoicing stub raises an error for the second; ZATCA stub later rejects the third | Run; ZATCA callback; next run | First and third generated, second `failed` with message; run `completed_with_errors`; summary sent to ALM staff only. Third becomes `failed` with its document in `superseded_documents`. Next run retries the second but not the third (needs release). After release: new document, new number (R12, R33, R34). |
| AT-18 | Step to 11,000.00 effective 2027-07-01 on a monthly lease; a step effective 2027-07-15 on another | Run July and August | First: July 11,000.00. Second: July old amount, August new amount (R18, OQ-4 default). |
| AT-19 | ALM (base SAR) contract in USD 1,000.00/month S; ledger-fx stub rate 3.75 on the issue date; another USD contract with no rate | Run | First: invoice in USD (net 1,000.00, VAT 150.00); JE in SAR as B5. Second: item `failed` "no exchange rate", no document (R32). |
| AT-20 | Agent service user | Creates a draft contract; tries to approve, release, end a secondment, start a run | Draft allowed (`source=agent:<name>`). The rest rejected (R36). |
| AT-21 | Any contract, line, status, run message | Render in `ar` and `en` | Arabic and English shown. Missing Arabic description rejected at save. |

## 9. Open questions

**BLOCKING** = the builder cannot build that part without an answer. **Default** = safe default used in the rules, easy to change.

| # | Question | Owner | Status |
|---|---|---|---|
| OQ-1 | Do auto-generated invoices from a CFO-approved contract need per-invoice approval, e.g. above an amount, or for intercompany? | Project owner / CFO | Default: no; contract approval suffices (R35). **Flagged for confirmation.** |
| OQ-2 | Revenue recognition for multi-month in-advance billing (quarterly/annual): invoicing credits income in full at issue, and there is no deferred income account (invoicing OQ-22). | Accountant | **BLOCKING** for `advance` timing with non-monthly frequency. Default: such contracts cannot be approved until the account and deferral method exist. |
| OQ-3 | Proration method (actual days vs 30/360 vs whole months) and period anchor (calendar vs contract start). | Accountant | Default: actual days; `calendar` anchor (R16, R17). |
| OQ-4 | Escalation types: fixed %, fixed amount, compounding, index-linked (index-linked needs a Sharia check), mid-period effect. | Accountant + Sharia board | Default: explicit stepped amounts only, from the next period. |
| OQ-5 | Billing date vs supply date for ZATCA/VAT: tax point for continuous supplies (rent, rentals, secondments), whether the issue date may precede the supply period (lead days), and the rule against backdating **[CC-verify]**. | compliance-checker / tax adviser | Default: issue on run date, `bill_lead_days=0`, supply = period. |
| OQ-6 | Refunds or credit for already-billed days after an early end (lease and rental terms may forbid refunds). | Accountant / Managing Partner | Default: draft credit note, human issues (R20). |
| OQ-7 | Amendments and termination: does every change need CFO approval, and is there a notice-period rule? | Project owner | Default: CFO approves amendments and terminations; secondment end dates need none (D6). |
| OQ-8 | Is there one CFO per entity or a group CFO? May a CFO approve a contract they created? | Project owner | Default: CFO role per entity; approver ≠ creator. |
| OQ-9 | Open-ended contracts and auto-renewal (leases, vehicle rentals). | Accountant | Default: end date required except secondment/service; no auto-renewal. |
| OQ-10 | Ejar: is registration of residential (and commercial) leases mandatory for these entities, and should the ERP integrate (contract number, payment schedule) **[CC-verify]**? | compliance-checker / Managing Partner | Default: Ejar number recorded only; no integration. |
| OQ-11 | Should an intercompany invoice create a draft mirror bill in the receiving entity? | Accountant | Default: no (R4). Mismatches show in `intercompany_balances`. |
| OQ-12 | PDPL: which secondment data may appear on invoices to the receiving entity (name, role, days)? | Legal / HR | Default: role title and pseudonymous `employee_ref` only. |
| OQ-13 | Billing for a supply period in a closed fiscal period: issue in the current period, or require a human decision? | Accountant | Default: `held` until released (R23). |
| OQ-14 | Contract number format. | Accountant | Default `{entity.code}-CTR-{year}-{000001}`. |
| OQ-15 | Mixed-use leases (residential + commercial in one contract) and VAT on lease service charges, utilities, parking **[CC-verify]**. | Tax adviser | Default: one `lease_use` per contract; separate lines with their own category for extras. |
| OQ-16 | Foreign-currency contracts: rate on invoice date, on supply date, or a contract-fixed rate; which rate source (ledger-fx). | Accountant | Default: ledger-fx rate on issue date (R32). **BLOCKING** for non-base currencies until ledger-fx.md and invoicing OQ-6 are done. |
| OQ-17 | Notification channel (in-app, e-mail) and escalation recipients. | Accountant | Default: in-app to entity accounting staff; CFO after 3 failed runs. |
| OQ-18 | Migration of existing contracts from Daftra/SAP B1, and how already-billed periods are marked. | Accountant + migration-agent | Default: `source=import`, past periods `generated` with no document link. |
| OQ-19 | Scheduled job runner (section 6 options). | Dev | Default for build: management command; runner chosen before deployment. |
| OQ-20 | Advance (386) and security deposit handling: accounts, VAT treatment of deposits, offset against invoices. **Needs tax adviser.** | Accountant / tax adviser | **BLOCKING** for advance offsets (R30). Deposits memo only (R31). |
| OQ-21 | Vehicle rental extras (excess mileage, damage, traffic fines recharge). | Accountant | Default: out of scope; billed manually through invoicing. |
| OQ-22 | Exact invoicing service names and the status callback (invoicing.md OQ-2); adding `source="system"` to invoicing. | invoicing spec author | **BLOCKING** for R13; build against a stub. |

**Blocking:** OQ-2 (multi-month in-advance), OQ-16 (non-base currency), OQ-20 (advance offsets), OQ-22 (invoicing interface), and invoicing OQ-25 (role model) for views. **Before building:** the compliance-checker verifies every [CC-verify] item (R15, R24, OQ-5, OQ-10, OQ-15).
