# Billing: contract-based recurring and automatic invoicing

Django app: `billing`. Status: **draft spec**. Written 2026-10-16 by the spec-writer agent. Revised 2026-10-16 (project-owner decisions D8 to D10: auto-issue, retroactive amendment/termination with correction notes, no mirror bill). Amended 2026-10-17 (project-owner decisions D11, D12: 3-decimal contract currencies, correction notes dated and taxed on the note date; §3 conventions, R37, R42, R43, R45, AT-37, AT-38, OQ-25, OQ-29). Amended 2026-10-17 (Track C build prep: reconciled with what is built in `invoicing` slice 1, `ledger` + ledger-fx, `core.access` and `hr`; interface rules R52 to R69, intercompany reconciliation report, scheduler, notes on D6, R13, R27, R32, R35, R37, R43, R44, R46, R47, §5, §6, §7, AT-11, AT-37, new AT-39 to AT-52, OQ status and OQ-30 to OQ-35). Amended 2026-10-17 (D13, project owner / CFO decision pack: advance billing spread through 2310, secondment recharge basis, reconciliation tolerance, HR-only end and manual reopen, 3-decimal prices ending in 0, approved accounts, entities without a VAT number, deposits, legacy system Falcon; notes on R27, R31, R32, R37, R54, R59, R61, R64, R65, R68; new R70 to R72; §5 B11, B12; §6; §7; AT-53 to AT-56; OQ-2, OQ-18, OQ-20, OQ-30, OQ-31, OQ-33 to OQ-35, new OQ-36 to OQ-38). Amended 2026-10-18 (D14, project-owner answers `docs/approvals/owner-answers-2026-10-18.md` A7 to A10, B1: one approval per contract confirmed, automatic intercompany mirror draft bill, cost-plus / timesheets / cost pools at go-live, invoice date = last day of the service month or the earlier end date with the ZATCA advice, approvals through the engine and matrix, VAT SAR rate on the invoice date; notes on D5, D8, D10, R4, R8, R14, R15, R32, R35, R50, R63, R65, R66; new 3.13 to 3.16, R73 to R81, §5 B13 to B15, §6, §7, AT-57 to AT-67, OQ-5, OQ-11, OQ-37, new OQ-39 to OQ-45; all **to build**).
Depends on: `core` (Entity), `core.access` (built; `docs/specs/access.md`), `docs/specs/invoicing.md` (sales documents, customers, VAT; slice 1 built, billing hooks are stubs that raise), `docs/specs/zatca.md` (clearance and reporting; reached only through `invoicing.zatca_port`), `docs/specs/ledger-fx.md` (built: currencies, rates, `JournalLine` currency), `docs/specs/hr.md` (built: `hr/billing_port.py`, `AssignmentEvent` outbox), `docs/specs/payroll.md` (secondment cost report, not built).
**[CC-verify]** = tax, ZATCA or regulatory statement not checked against official text. The compliance-checker must verify it before module-builder starts.

## 1. Purpose

The billing module holds customer contracts for **every entity** in the group (Almiras, subsidiaries and any entity added later as an `Entity` row). It covers leases (commercial and residential), vehicle rentals, staff secondments, services (consulting, IP licences) and other recurring charges, for third-party customers and for other group entities (intercompany). From each approved contract it builds a billing schedule. On each due date it creates sales invoices automatically, through `invoicing.services` only, with `source="system"`. It never bills the same period twice. It prorates partial periods. It stops billing when a contract ends, is terminated, or (for secondments) when the HR assignment ends. Contracts stay amendable, terminable and cancellable after approval, also with a past effective date, through CFO-approved versions; already-billed periods are then corrected by system-drafted credit or debit notes that a person reviews and the CFO approves, never by editing issued invoices. It creates and posts no journal entries itself: invoicing posts them, and sales documents go through ZATCA as described in invoicing.md. Each entity's Sharia setting applies to every contract line, every user-facing text is in English and Arabic, and no contract, schedule or run crosses entity boundaries.

## 2. Decisions

| # | Decision | Who | Date |
|---|---|---|---|
| D1 | Some invoices must be generated automatically: (a) cars rented on annual contracts; (b) rent on commercial and residential leases; (c) seconded staff billed to the receiving entity until HR ends the secondment. | Project owner | 2026-10-16 |
| D2 | Group entities invoice each other often (intercompany rentals, IP, secondments, consulting). No group entity is in a VAT group, so intercompany invoices are normal VAT invoices that carry the counterparty entity. (This settles invoicing.md OQ-13 and zatca.md OQ-5 / OQ-17 for billing.) | Project owner | 2026-10-16 |
| D3 | Multi-currency is needed (SAR, CNY, EGP, GBP, EUR, USD, AED, and others). Currencies and rates are covered by `docs/specs/ledger-fx.md`. | Project owner | 2026-10-16 |
| D4 | A contract may have an advance, a security deposit, both, or neither. | Project owner | 2026-10-16 |
| D5 | Roles: accounting staff create; the CFO approves; posting is automatic on approval. Sales documents post only after ZATCA clearance or reporting. | Project owner | 2026-10-16 |
| D6 | Until the HR module exists, accounting staff record the secondment end date. *(Note 2026-10-17: HR is built. D6 still applies to secondments **not** linked to an HR assignment; for linked ones HR is the only source of the end date (R59, from hr R26 / hr OQ-16 default; owner confirmation in OQ-33).)* | Project owner (task brief) | 2026-10-16 |
| D7 | Rules fixed by CLAUDE.md apply: `entity` FK on every record; queries filtered by entity and permission; new subsidiaries are data; en/ar text; `Decimal` money with explicit currency; posting only via `ledger.services`; open source only. *(Rule 7 amended 2026-10-16: SAR 2 dp, foreign transaction amounts up to 3 dp; effect here: OQ-29.)* | CLAUDE.md | in repo as of 2026-10-16 |
| D8 | Invoices auto-generated from a CFO-approved contract need **no per-invoice approval** (resolves OQ-1; invoicing OQ-36). | Project owner | 2026-10-16 |
| D9 | A contract must stay modifiable and cancellable after approval, **including retroactively**, because staff may forget to tell the ERP that a contract or service was cancelled, cut short or modified. So: (a) contract amendments are versioned, CFO-approved, and the effective date may be in the past; (b) early termination or cancellation may have a past effective date; (c) for periods already billed, issued invoices are **never edited or deleted**: the system generates **draft** credit notes for over-billed amounts (debit notes for increases), a person reviews them and the CFO approves them; (d) posted-period rules still apply: corrections are dated in an open period. | Project owner | 2026-10-16 |
| D10 | Intercompany: by default **no** automatic bill is created in the buyer entity; the buyer entity's staff enter it (resolves OQ-11; invoicing OQ-37). | Project owner | 2026-10-16 |
| D11 | 3-decimal currencies (KWD, BHD, OMR, JOD): "yes, Almiras invoices in them"; contracts must support 3-dp transaction amounts; SAR VAT totals stay 2 dp (resolves OQ-29; invoicing D27, R93). | Project owner | 2026-10-17 |
| D12 | Tax: the date that counts for a credit or debit note is the note's own date. Backdated contract corrections are dated on the note date; the 15-day note warning is not based on a backdated event date (resolves OQ-25; invoicing D30a, R95). | Project owner | 2026-10-17 |
| D13 | **Project owner / CFO decision pack** (`docs/approvals/owner-cfo-decision-pack-2026-10-17-answered.docx`; invoicing D32). (a) A3: contracts billed quarterly or yearly in advance are allowed: "Yes spread the income monthly. Follow the matching principle", through 2310 Deferred income (R70; resolves OQ-2). (b) A4: secondment recharge at contract price, actual cost or cost plus a markup: "All is acceptable depending on the case. When we talk actual cost: it is all actual cost fully loaded (not only salary but also all benefits and government fees such as medical insurance, Iqama renewal etc…)" (R71; resolves OQ-30(a)). (c) A5: intercompany reconciliation tolerance "zero" (R64; resolves OQ-30(b) tolerance). (d) A6: once a secondment is linked to HR, only HR can end it: "Yes" (R59; resolves OQ-33). (e) A7: if HR removes a secondment end date, "Billing reopens manually" (R61; resolves OQ-31). (f) A8: KWD/BHD/OMR/JOD amounts are priced "in amounts ending 0" (R72; answers OQ-35 for prices). (g) CFO, part C: accounts 2310, 2320, 4120, 4220, 4230, 4240 and 4310 approved; policy A (2310 and 2320 among the control accounts, posted only by invoicing, never by manual journal); policy B: car rental income defaults to 4100 (R27). (h) Tax 5: an entity without a VAT number issues "فاتورة / Invoice" with no VAT, no VAT number and no ZATCA QR (invoicing R101; answers OQ-34). (i) Tax 10: a refundable deposit carries no VAT on receipt; applied to rent, VAT follows the rent (invoicing R106). (j) Tax 1: VAT in SAR at the SAMA rate on the tax point (invoicing R100; R32 note). (k) A10: Almiras's legacy system is Falcon, not Daftra (OQ-18). | Project owner (Part A), CFO (Part C), CFO / tax adviser (Part D) | 2026-10-17 |
| D14 | **Project-owner answers** (`docs/approvals/owner-answers-2026-10-18.md`). (a) A7: "Auto-generated invoices: one approval per contract" (confirms D8; R35). (b) A8: "Intercompany: automatic mirror bill in the buying company" (supersedes D10; R4, R63, R73). (c) A8: "Cost-plus, timesheets and cost pools are needed at go-live" (R74 to R76; was main B-D3, B-D12). (d) A9: "Invoice date: last day of the service month (owner's preference), unless ZATCA issues require otherwise (team to advise); services ending earlier are invoiced on that earlier end date" (R77, R78; replaces R15's run-date rule; team's ZATCA advice recorded in R78). (e) A10: "All approvable documents go through the same approval engine" (contracts, versions, correction notes, timesheets, cost-pool periods: R79; access A25, R57, R61). (f) B1: "Exchange rate for VAT in SAR: the invoice date rate" (R32 note; invoicing D33(b)). (g) A1, A2: authority per company; approvers from each entity's authority matrix, not "the CFO" (D5 note; access A24, A25). | Project owner | 2026-10-18 |

*Notes 2026-10-18 (D14):* **D5** stands for "accounting staff create; posting automatic on approval; sales post after ZATCA", but "the CFO approves" now reads "the approver named by the entity's authority matrix" (access R57). **D8** confirmed by D14(a). **D10** superseded by D14(b).

## 3. Data model

Conventions (same as invoicing.md): every model has `entity` FK → `core.Entity` (`on_delete=PROTECT`, required); money is `DecimalField(18, 2)` (*amended 2026-10-17, D11:* contract-currency amounts are `DecimalField(21, 3)`, decimals limited to the currency's `minor_units`; SAR amounts 2 dp); every model has `created_by`, `created_at`, `updated_at`, `source` (`human` / `import` / `agent:<name>` / `system`). "Bi" = field exists as `_en` and `_ar`.
*Amended 2026-10-17 (as built):* `source` takes the value of `core.access` `UserProfile.source` for the acting user: `human`, `import`, `agent:<name>`, or `system:billing` for the billing system user (`system_user("billing")`). "`source="system"`" in this spec and in invoicing R60/R73 means `system:billing`.

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
| recharge_basis | Choice `contract_price`, `actual_cost`, `cost_plus` | Y if `secondment` | | *Added 2026-10-17 (D13(b), R71; to build).* Default `contract_price` |
| markup_pct | Decimal(5,2) ≥ 0 | Y if `cost_plus` | | *Added 2026-10-17 (R71).* Percentage on the fully loaded cost |
| income_type | FK invoicing.IncomeType | Y | | *Added 2026-10-17 (as built: every `SalesDocumentLine` needs one).* Prefills `vat_category`, exemption reason and `income_account` (R27 amended); drives Art. 51 class and the real-estate retention class (R28) inside invoicing |

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
| invoice_lines | JSON, null | Y when `generated` | *Added 2026-10-17 (R52).* Frozen input handed to `create_from_billing`: per `line_key`: `line_no`, `description_en/ar` (with period text), `net` (contract currency, string `Decimal`), `unit_code`, `vat_category`, `vat_exemption_reason` code, `income_type` code, `income_account` code, `employee_ref` (secondment lines only). Written in the generating transaction; read-only afterwards |

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
`billing.services.end_secondment(entity, assignment, end_date, user, source)`. It is idempotent: the same end date twice is a no-op. A different end date applies the earlier or later date per R49 (was R20, withdrawn by D9).
*Amended 2026-10-17 (HR built):* HR does not call billing directly; it calls the port in `hr/billing_port.py` (`linked_assignment(entity, hr_assignment_ref)`, `end_secondment(entity, billing_assignment, end_date, user, source)`, `assignment_id(billing_assignment)`), chosen by setting `HR_BILLING_PORT`. Billing ships `billing.hr_port.BillingHrPort` implementing it on top of `billing.services.end_secondment` (R56 to R61). `source` is `manual` or `hr_event`; `end_date` may be `start_date − 1` (HR cancellation before start, R60). Fields added: `hr_linked_at` / `hr_linked_by` (set once by R61); `last_hr_end_date` (Date, null: last end date received with `source="hr_event"`, for audit).

### 3.7 BillingRun and BillingRunItem

| Model | Fields | Notes |
|---|---|---|
| BillingRun | entity (null = all entities the runner may bill, one item set per entity), run_date (Asia/Riyadh), trigger (`scheduler` / `manual`), started_by (system or human user), started_at, finished_at, status (`running`, `completed`, `completed_with_errors`, `aborted`), counts (generated, skipped, held, failed) | Append-only after finish |
| BillingRunItem | entity, run, billing_period, outcome (`generated`, `skipped_already_billed`, `held`, `failed`), sales_document (null), message_en / message_ar | One row per period attempted. Append-only |

*Amended 2026-10-17 (access R22, as built):* `started_by` is always the billing system user; a manual run also stores `initiated_by` (FK User, the human). Added fields: `BillingRun.sync_counts` (documents whose status was read back, R55); `BillingRunItem.error_code` (invoicing error code, R54) and `warnings` (JSON, en/ar invoicing warnings such as the late-invoice warning, invoicing R84).

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

### 3.12 Intercompany reconciliation row (computed, not stored; added 2026-10-17, R64)

| Field | Notes |
|---|---|
| as_of | Date |
| seller / buyer | Entity codes of the pair (each unordered pair shown once, seller = the lower code) |
| currency | Transaction currency (ledger-fx R29) |
| seller_position | Seller's net intercompany position with buyer, from `ledger.services.intercompany_balances(as_of, user=, group=, by_currency=True)` (debit − credit on accounts flagged `intercompany`: 1210, 1220, 2110) |
| buyer_position | Buyer's net position with seller, same source; null + "not visible" (en/ar) if the user cannot see the buyer |
| difference | `seller_position + buyer_position` (0 when the pair mirrors) |
| billing_open | Gross of billing-originated documents (invoices and notes, `final`) from seller to buyer in that currency, open in the seller's sub-ledger at `as_of` (information, helps find unbooked bills) |
| billing_unposted | Gross of billing-originated documents still `issued` (awaiting ZATCA) — booked in neither entity yet |

*Added 2026-10-18 (D14(b), (c); all to build).* Every model below has `entity`; amounts `Decimal`, currency explicit; texts en/ar.

### 3.13 `ContractLine` additions
`billing_basis`: `fixed` (today's `amount`), `timesheet`, `cost_pool`, `cost_plus` (secondment R71 stays on `recharge_basis`). `hourly_rate` (Money, Y for `timesheet`); `cost_pool` (FK 3.15, Y for `cost_pool`); `allocation_key` (`fixed_pct`, `headcount`, `revenue`), `allocation_value` (Decimal(9,4), Y for `fixed_pct`); `markup_pct` (Decimal(5,2), default 0).

### 3.14 `TimesheetEntry` (seller entity)
`entity`, `contract_line` (FK, `billing_basis=timesheet`), `work_date`, `hours` (Decimal(6,2), > 0, ≤ 24), `worker_ref` (pseudonymous, as `employee_ref`), `description_ar` (Y) / `description_en`, `status` (`draft`, `submitted`, `approved`, `rejected`), `created_by`, `submitted_by`, `approved_by` (engine, activity `billing.timesheet`), `billing_period` (FK, set when billed; then locked).

### 3.15 `CostPool` and `CostPoolPeriod` (seller entity)
`CostPool`: `entity`, `code`, `name_en/ar`, `accounts` (M2M `ledger.Account` of the same entity, expense type), `is_active`. `CostPoolPeriod`: `cost_pool`, `period` (`YYYY-MM`), `pool_amount` (functional currency, computed from **posted** ledger balances of `accounts` for the month), `drivers` (JSON per recipient: headcount or revenue used), `status` (`draft`, `submitted`, `approved`), approval through the engine (activity `billing.cost_pool_period`). Approved periods are never edited; a correction is a new version, billed as R42 adjustments.

### 3.16 `MirrorBillLink` and `IntercompanyAccountMap` (buyer entity)
`MirrorBillLink`: `seller_document` (FK invoicing SalesDocument, seller entity), `buyer_entity`, `buyer_bill` (FK invoicing PurchaseDocument, null), `status` (`created`, `skipped`, `failed`), `reason_en/ar`. Unique on `seller_document`. `IntercompanyAccountMap`: `entity` (buyer), `seller_entity`, `income_type` (seller's), `expense_account` (FK, buyer's chart); missing row → bill line without account, flagged for the buyer's staff.

## 4. Rules

"svc" = `billing.services`; "DB" = database constraint; "inv" = enforced by invoicing services; "ledger" = existing ledger code; "Q" = queryset/permission filter.

### Entity separation
1. A contract, its lines, steps, terms, schedule, assignments and runs all have the same `entity`. Customer and income accounts belong to that entity. **[svc `clean` on save; ledger `validate_entry` at posting; AT-14]**
2. Views and APIs list billing records filtered by entity and by the user's entity permissions. A run for entity A never reads or writes B's records. **[Q; svc run loop scoped per entity; AT-14]**
3. `counterparty_entity` comes only from `customer.group_entity`. It cannot be set by hand and never equals `entity`. **[svc; model `clean`; AT-11]**
4. The system never creates records in the counterparty entity's books: no automatic mirror bill; the buyer entity's staff enter the bill (D10). This also holds for correction notes (R50). *(Amended 2026-10-16: was "until OQ-11 is decided".)* **[svc; AT-11, AT-32]**
    *Replaced 2026-10-18 (D14(b); to build):* the system creates a **draft** mirror bill (and mirror vendor credit / debit note) in the buyer entity, as R73. It never posts, approves or submits anything in the buyer entity and never posts to the buyer's accounts; billing itself still writes nothing there except through `invoicing.create_mirror_bill` run by `system_intercompany` with its buyer-entity membership.
5. A new entity bills with no code change once it has its chart, periods, customers and an approved contract. **[AT-15]**

### Contract lifecycle
6. Status runs `draft → approved → active → ended`, or `active → terminated` (also `approved → terminated` before start). No other transitions exist. **[svc state machine; AT-1]**
7. Only `draft` contracts can be edited or deleted. After approval, price, dates, lines and VAT are changed only through a CFO-approved `ContractVersion` (D9, R38 to R41); termination and cancellation are versions too (R40). Secondment end dates (R19, R49) are the only change without CFO approval (D6). *(Amended 2026-10-16 by D9.)* **[model `save` guard + svc; AT-1, AT-22]**
8. Approval needs the CFO role on the contract's entity. The approver is not the creator (default, OQ-8). Approval assigns the contract number and builds the schedule in one transaction. **[svc; AT-1]**
    *Amended 2026-10-18 (D14(e), (g); to build):* "the CFO role" reads "the approver(s) named by the entity's authority matrix for `billing.contract`" through the approval engine (R79; access R57, R61). Approver ≠ creator, except the entity's top authority (access R58), logged and reported.
9. A contract becomes `active` on approval if `start_date ≤ today`, otherwise on the first run on or after `start_date`. It becomes `ended` when `end_date` has passed and every non-cancelled period is `generated`. **[svc run; AT-6]**

### Schedule and idempotency
10. The schedule is materialised at approval: one `BillingPeriod` per period up to `end_date`, or a rolling 13 months for open-ended contracts (extended by each run). Status flow: `planned → generated`; `planned → held → planned` (human release); `planned → failed → planned` (automatic retry next run); `planned → cancelled`. A `generated` period never returns to `planned` (R12 is the only re-generation path). **[svc; DB unique `(contract, period_start)`; AT-2]**
11. A period is billed at most once. The generator locks the `BillingPeriod` row (`select_for_update`) and skips it unless the status is `planned`. Runs also take a PostgreSQL advisory lock per entity, so two runners never work on the same entity at once. A re-run, or a crash and re-run, creates no second invoice. **[svc + DB unique + lock; AT-2, AT-3]**
12. If ZATCA rejects a generated invoice (invoicing R42: document `rejected`), the period goes to `failed` and the document moves to `superseded_documents`. Re-generation needs a human release after the cause is fixed. It creates a new document with a new number. **[svc callback; AT-17]**
13. The generator creates and issues documents only through `invoicing.services` (the create and issue functions named in invoicing.md, OQ-2 there), with `source="system"` and the billing service user. It never writes `SalesDocument` or `JournalEntry` rows directly. **[svc; code-reviewer; AT-4]**
    *Amended 2026-10-17 (as built):* the functions are `invoicing.services.create_from_billing(billing_item, user)` (invoice; `billing_item` = the `BillingPeriod`) and `invoicing.services.create_adjustment_from_billing(adjustment, user)` (draft note; `adjustment` = the `BillingAdjustment`), both called with `user=core.access.services.system_user("billing")`. Both are stubs today that raise `NotImplementedError`; invoicing implements them per R52 to R55 and the invoicing R60/R73 notes. Billing is built and tested against a fake of these two functions until then. **[svc; code-reviewer; AT-39]**

### Dates and amounts
14. `bill_date`: `advance` → `period_start`; `arrears` → day after `period_end`. A period is due when `bill_date - bill_lead_days ≤ run_date`. **[svc; AT-4]**
    *Amended 2026-10-18 (D14(d); to build):* `arrears` → `period_end`, or the effective end date (termination, secondment end, contract end) when it falls earlier inside the period (R77). `advance` unchanged (OQ-41).
15. `issue_date` is the actual run date (Asia/Riyadh). It is never backdated **[CC-verify]**. `supply_date` = `period_start` and `supply_end_date` = `period_end` (OQ-5). **[svc; AT-4]**
    *Amended 2026-10-18 (D14(d); to build):* still never backdated (ZATCA advice, R78); the run that carries the owner's preferred date runs **on** that date (R77). `supply_date` / `supply_end_date` unchanged (KSA-5 / KSA-24).
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
    *Amended 2026-10-17 (as built):* the default comes from the line's `income_type` (invoicing `IncomeType.default_account_code`, or `default_intercompany_account_code` when the contract is intercompany): commercial rent and car rental 4100 / 4110; residential rent 4120 / 4110; secondment 4200 / 4210; management fee, service charge, service fee 4220 / 4230 / 4240, intercompany 4210; IP licence 4300 / 4310. 4310 now exists in `ledger/coa_template.py` (proposed, accountant to review, invoicing D29), so intercompany IP lines are **accepted** on 4310; the "rejected until it exists" sentence is withdrawn. Income type `other` has no default: staff choose. **[svc; AT-11, AT-50]**
    *Confirmed 2026-10-17 (D13(g)):* 4120, 4220, 4230, 4240 and 4310 are approved (no longer proposed); car rental defaults to 4100 (policy B). No code change beyond removing the PROPOSED markers (invoicing R97 build).
28. Rent invoices on investment property are handed to invoicing with `retention_class=capital_asset_real_estate` (zatca.md R-38). **[svc → inv]**

### Sharia
29. A contract line cannot use a `sharia_prohibited` account in a Sharia-enforcing entity. No escalation, late fee or charge may be linked to an interest rate or index, or to a late-payment penalty (invoicing R7). Lines on `requires_purification` accounts are allowed but flag the contract and every generated invoice. **[svc at save and approval; ledger at posting; AT-13]**

### Advances and deposits
30. Advance terms are recorded. A contract whose advance is to be offset against periodic invoices cannot be approved until the invoicing prepayment (386) flow exists (invoicing R55 / OQ-16; zatca.md R-39). **[svc; AT-16]**
31. Deposit terms are a memo only. Billing creates no invoice or entry for a deposit (no deposit liability account exists; VAT treatment needs a tax adviser). **[svc; AT-16]**
    *Amended 2026-10-17 (D13(g), (i)):* 2320 is approved and the deposit VAT treatment is decided for receipt and application, so invoicing can record deposit receipts and apply them to rent (invoicing R106), using `contract_allows(contract, "deposit")`. Billing still creates no deposit invoice or entry; forfeiture is not built (invoicing OQ-16).

### Currency
32. The invoice currency is the contract currency. When it differs from `entity.base_currency`, the rate and base-currency amounts come from ledger-fx services. The default rate date is the invoice issue date (OQ-16). *(Note 2026-10-18, D14(f): the VAT SAR amount also uses the SAMA rate on the invoice date, invoicing R100 amended; with R77 that is normally the last day of the service month.)* If no rate exists, or ledger-fx / invoicing multi-currency is not built yet (invoicing OQ-6), the item fails with a bilingual message and no document is created. **[svc + ledger-fx + inv; AT-19]**
    *Amended 2026-10-17 (ledger-fx and invoicing built):* billing does not look up rates. Invoicing fixes the rate to the functional currency and the SAR rate for ZATCA on the document's `issue_date` (the run date) at issue, from the ledger rate table (entity rate first, then group; up to `FX_MAX_RATE_AGE_DAYS`, default 5, before), pins the rate row on the document, and books at that rate when ZATCA clears it (invoicing R56, R77). Correction notes take the rate on their own issue date. A missing rate makes `create_from_billing` raise `rate_required`; the period is `failed` and retried next run (R54). A contract-fixed rate or a supply-date rate is **not** supported (OQ-16 status). **[inv; AT-19]**
    *Note 2026-10-17 (D13(j)):* for the VAT in SAR invoicing will use the SAMA rate on the tax point (= `supply_date` = `period_start`, or the issue date when that is earlier, invoicing R100); a missing SAMA rate raises `sama_rate_missing`, mapped like `rate_required` (R54: `failed`, automatic retry). Billing still looks up no rate.

### Runs, notifications, approval of generated invoices
33. Each period is generated in its own transaction. One failure never stops the others or rolls them back. Every attempt writes a `BillingRunItem` with an en/ar message. **[svc; AT-17]**
34. After each run, the entity's accounting staff get a summary (generated, held, failed) in the app, in their language. Failures still unresolved after 3 consecutive runs are also sent to the CFO (default, OQ-17). **[svc; AT-17]**
35. Generated invoices are issued without per-invoice approval: the CFO's contract approval covers them (D8; was default OQ-1). This applies to invoices only, never to correction notes (R44). *(Amended 2026-10-16.)* **[svc; AT-4, AT-35]**
    *Confirmed 2026-10-18 (D14(a); no code change):* one approval per contract (and per approved version); also for invoices whose amounts come from approved timesheets, cost-pool periods or cost reports (R74 to R76; resolves OQ-37(a)). The inputs themselves are approved (R74, R75).
    *Amended 2026-10-17 (as built):* "no per-invoice approval" still means the full issue path: invoicing validation, gap-free number, ZATCA hand-off after commit, and posting only on ZATCA clearance/reporting by the ZATCA queue system user (`post_cleared_document`). The approver recorded on the invoice is the CFO who approved `billed_version` (R53). With `InvoicingSettings.auto_issue_from_approved_contract = False` the invoice is created `submitted` and waits for the CFO (invoicing R60); the period is still `generated`. **[inv; AT-39]**
36. Agents (gateway service users) may draft contracts if granted. They cannot approve, release, terminate, end secondments or start runs. The billing service user is a system user, not an agent, and it acts only on `active` contracts. **[Q + svc; AT-20]**
37. All amounts are `Decimal` with 2 dp, and the currency is stored on every money field's parent record. Contracts in a currency whose `minor_units` ≠ 2 are rejected at save until OQ-29 is decided (default). *(Amended 2026-10-16 after CLAUDE.md rule 7 change.)* **[svc + DB; AT-5]**
    *Amended 2026-10-17 (D11; resolves OQ-29):* "with 2 dp" and the `minor_units` ≠ 2 refusal are withdrawn for 3-decimal currencies: contract amounts are stored with the contract currency's decimals (2 or 3, never more), and proration and corrections (R16, R42) round half-up to those decimals. 0-decimal currencies stay refused (invoicing OQ-58, default). Float input stays refused. **[svc + DB; AT-5, AT-37]**
    *Amended 2026-10-17 (invoicing D27 as built, OQ-62 default):* invoicing refuses to issue a sales document in a 3-decimal currency while any amount (line net, VAT, totals, breakdown) has a third decimal other than 0, until ZATCA confirms 3-dp XML (invoicing `_third_decimal_problems`, OQ-62). Billing does **not** change contract amounts to avoid this (rounding a price is a business decision, OQ-35). Instead: (a) at contract or version approval in a 3-decimal currency, the CFO sees a bilingual warning that invoices whose amounts have a non-zero third decimal will be held; (b) when `create_from_billing` refuses for this reason, nothing is created, no number is used, and the period goes to `held` (not `failed`) with reason "3-decimal amounts await ZATCA confirmation (invoicing OQ-62)" (en/ar); (c) a human releases it after OQ-62 is resolved or after a version changes the amount. Correction notes in such currencies stay `submitted` when the CFO's approval is refused (invoicing behaviour); their adjustment stays `submitted` and counts in `billed_net` (R46). **[svc + inv; AT-37, AT-45]**
    *Amended 2026-10-17 (D13(f), to build; replaces "Billing does **not** change contract amounts … OQ-35" for entered amounts):* prices are agreed in amounts ending in 0: R72. (b) and (c) above stay for **computed** amounts (prorations, VAT, corrections) that still end in a non-zero third decimal.

### Amendments, termination, cancellation and corrections (added 2026-10-16, D9)
38. Every approved contract has versions 1..n. The terms that apply to a day are those of the approved version with the latest `effective_date` ≤ that day (equal dates: higher `version_no`). Approved versions, their lines and price steps are never edited or deleted. **[model `save/delete` guard + svc `terms_on(contract, day)`; AT-22]**
39. An amendment is drafted and submitted by accounting staff and takes effect only when the entity's CFO approves it; the approver is neither its creator nor its submitter. `reason_ar` is required. The effective date may be in the past (no lower limit other than `start_date`) or in the future. Default limits until OQ-23 is decided: entity, customer, currency and contract type cannot be amended (a new contract is needed); `billing_frequency`, `billing_timing` and `period_anchor` may change only with an effective date after the end of the last `generated` period. Each version is checked like a new contract: R1, R24, R25, R27, R29, R30. **[svc state machine + `core.access.require` (A); AT-22, AT-34, AT-36]**
40. Early termination is a version of kind `termination`: `terminated_on` = `effective_date` − 1 day = last billable day. Cancellation is a version of kind `cancellation` with `effective_date` = `start_date`: nothing is billable. Both may have a past effective date, need CFO approval like R39, and on approval set `status=terminated` and `termination_kind`. **[svc; AT-9, AT-23, AT-24]**
41. Approval of a version runs in one transaction under the entity's billing advisory lock (the same lock as runs, R11), so no run generates a period while terms change. It (a) sets `current_version`; (b) rebuilds only `planned`, `held` and `failed` periods for the new terms (cancel those outside the new range, add missing ones); (c) never changes, regenerates or re-bills a `generated` period or its documents; (d) runs the correction pass (R42). **[svc + advisory lock; AT-26, AT-27]**
42. Correction pass: for every `generated` period that ends on or after the earliest effective date affected, and per `line_key`: `required_net` = Σ over the parts of the period governed by each version: `round(amount × part_days / period_days, 2)` (amount per R18 within that version; actual days, R16; ROUND_HALF_UP; a part after `terminated_on`, or under a cancellation, is 0). `delta` = `required_net` − `billed_net` (3.11). Per period and original invoice, negative deltas form one `credit` adjustment and positive deltas one `debit` adjustment. Delta 0 → nothing. Mid-period effect by actual days is the default for amendments (OQ-24); scheduled price steps keep the OQ-4 default. **[svc `compute_corrections(contract, user)`; AT-23 to AT-26]**
    *Amended 2026-10-17 (D11):* `round(…, 2)` means rounding to the contract currency's decimals (3 for KWD, BHD, OMR, JOD).
43. For each adjustment, billing calls `invoicing.services.create_adjustment_from_billing(adjustment, user=<billing system user>)` (invoicing R73), which creates a **draft** credit note (`credit`) or debit note (`debit`), `source="system"`, in the contract currency, referencing the period's invoice (`original_document`), supply period = `supply_start`..`supply_end`, lines from the adjustment, `reason_ar`/`reason_en` from the version, `adjustment_event_date` = the version's `effective_date` (OQ-25). Billing never writes `SalesDocument` or journal rows itself (R13). **[svc → inv; AT-23]**
    *Amended 2026-10-17 (D12; resolves OQ-25):* billing no longer sets `adjustment_event_date` to the version's effective date. The note is dated, and its VAT counted, on its own issue date (invoicing R95); the effective date stays on the version and the supply period on the note. No 15-day warning arises from a backdated effective date. **[svc → inv; AT-38]**
    *Amended 2026-10-17 (as built):* exact call `invoicing.services.create_adjustment_from_billing(adjustment, user=system_user("billing"))`; the draft has `source="system:billing"`, `billing_adjustment` set, `adjustment_event_date` empty, and its lines are read-only for every user (only header fields such as `payment_means_code`, `reason_code` may be completed, R56). It never issues. Idempotent: a second call for the same adjustment returns the existing non-terminal note. **[inv; AT-48]**
44. Correction notes are **never issued automatically**, whatever `InvoicingSettings.auto_issue_from_approved_contract` says. A human reviewer (accounting staff) checks the draft, completes the fields invoicing requires (e.g. `payment_means_code`, invoicing R18) and submits it; the CFO approves (= issues) it under invoicing R57, not being the reviewer. The computed amounts are read-only for the reviewer (OQ-26). Agents and the billing system user cannot submit or approve. **[inv R57 + `core.access` R14; AT-23, AT-35]**
    *Amended 2026-10-17 (as built):* submit is `invoicing.services.submit_sales_document(document, user=)`; issue is `approve_sales_document(document, user=)` (`invoicing.approve_sales_document`, flags A + P). As built the approver is never the creator (the billing system user), submitter or last modifier, so a reviewer who submitted or completed the note can never approve it. The note is dated on the approval day (Asia/Riyadh) and taxed on that date (D12, invoicing R78, R95), goes to ZATCA, and posts on clearance with `posted_by` = that CFO.
45. Issued invoices and notes are never edited, cancelled or deleted (invoicing R13, R17). Notes are dated on their actual issue date in an open fiscal period; the supply period they refer to may lie in a closed period. If no open period covers the issue date, CFO approval fails and the note stays `submitted` (invoicing R28). Billing never reopens or backdates a period (R15, R23). **[inv + ledger `_find_open_period`; AT-28]**
    *Confirmed 2026-10-17 (D12):* the note's own date is its tax date (invoicing R95).
46. No double billing: (a) a `generated` period is never billed again (R11, R41c); (b) `billed_net` includes pending adjustments, so re-running the pass with no new version creates nothing; (c) when a later version is approved, every adjustment of the contract still `waiting_original`, `drafted` or `submitted` becomes `superseded` and its draft note is deleted (drafts only) before the pass recomputes; `issued`/`final` notes are kept and counted; (d) after all notes are final, for every generated period and line, invoice + debit notes − credit notes = `required_net`. Credit-note caps of invoicing R18 still apply. **[svc + DB unique 3.10; AT-26]**
    *Amended 2026-10-17 (as built):* (c) deletes superseded notes only through an invoicing service for billing drafts (name proposed: `invoicing.services.discard_adjustment_draft(document, user)`, `invoicing.create_adjustment_from_billing`, system user only). A note in `submitted` is first returned to `draft` (`submitted → draft` is the only backward step, invoicing R96) and deleted in the same transaction; `issued`, `final` and `rejected` notes are never touched. **[inv; AT-48]**
47. If the period's invoice is not yet `final` (awaiting ZATCA), the adjustment is created as `waiting_original` and its draft note is created when the invoice becomes `final` (credit/debit notes need a final original, invoicing R18/R19). If ZATCA rejects a correction note, the adjustment becomes `failed` and drops out of `billed_net`; the next pass (on the next version or on a human "recompute") creates a new one. **[svc callback; AT-29]**
    *Amended 2026-10-17 (as built):* invoicing has no callback to billing; billing reads document status back (R55). "When the invoice becomes `final`" means at the first sync after it does.
48. The reviewer may discard a draft (for example when the contract's terms forbid a refund) with a reason in Arabic (English optional). A discarded adjustment stays on the bilingual "unresolved billing differences" report for the entity and is not re-created by later passes for the same version; a later version or a human "recompute" may create a new one. **[svc + report; AT-30]**
49. Secondment end (R19, `end_secondment`) needs no CFO approval (D6) and is a correction trigger like a version: an end date before already-billed days produces draft credit notes via R42 to R48; an end date moved later puts cancelled periods back to `planned` and produces draft debit notes for re-added days that had been credited. The notes themselves need review and CFO approval (R44). **[svc; AT-31]**
50. Corrections stay inside the contract's entity; intercompany notes post to 1210 with `counterparty = counterparty_entity` (invoicing R4); nothing is created in the counterparty (R4, D10). *(Amended 2026-10-18, D14(b): once a note is `final`, a draft mirror vendor credit / debit note is created in the buyer, R73.)* Sharia checks of R29 run on every version line at save and approval; notes carry `requires_purification` from their lines. All version, adjustment and report texts are en/ar. **[svc + inv + ledger; AT-32, AT-36]**
51. After a version is approved, the entity's accounting staff get an in-app summary (en/ar) of the adjustments created, waiting and superseded. **[svc; AT-23]**

### Interface with invoicing as built (added 2026-10-17)
52. **Invoice call.** For each due period the generator, inside the period's own transaction (R33) and after locking it (R11), writes `invoice_lines` and calls `create_from_billing(period, user=system_user("billing"))`. Invoicing reads only: `period.entity`, `period.contract.customer`, `contract.currency`, `period_start` / `period_end` (→ `supply_date` / `supply_end_date`), `contract.due_days`, `invoice_lines` (→ one line each: quantity 1, `unit_price` = `net`), `billed_version` (approval status and approver) and `contract.requires_purification`. The call is all-or-nothing: if invoicing refuses, it raises, nothing is created and no number is used; billing rolls back the period's transaction and records the outcome (R54). Idempotent: a second call for the same period returns the existing non-rejected document. **[svc + inv; AT-39, AT-40]**
53. **Approval chain of an auto-issued invoice.** The invoice is issued by invoicing in the same call (R35), with `approved_by` = `billed_version.approved_by` (for version 1, the contract approver). Posting on clearance as built (`ledger._clearance_approver`) accepts as `posted_by` only a human whose **`invoicing.approve_sales_document`** grant for that document was used; an auto-issued invoice has none. So invoicing and ledger must accept, for documents with `source="system:billing"` and `billing_item` set, the CFO's used **`billing.approve_contract` / `billing.approve_version`** grant for `billed_version` as the recorded approval (still a human, not the document's creator, logged as `post_on_clearance`). Until that change exists, auto-issued invoices cannot post and billing must not be switched on in production (OQ-22, BLOCKING). **[inv + ledger + access; code-reviewer; AT-39]**
    *Amended 2026-10-17 (invoicing slice-2 review finding 3, as built):* "used" is checked: a successful `approve_contract` / `approve_version` records the use of its grant (`GrantUse`, target the contract or version; for termination and cancellation also the `billing.approve_termination` grant), and ledger `_contract_approval` counts only used grants issued no later than the recorded approval time. A grant logged for `return_contract` or `reject_version` (same permission, same record, never used) is refused. **[billing svc + ledger; test `test_r53_a_return_or_rejection_grant_is_not_an_approval`]**
54. **Outcome mapping.** Billing maps invoicing's result by error code, never by message text:

    | Invoicing result | Period | Retry |
    |---|---|---|
    | document returned (`issued` or `submitted`) | `generated`, `sales_document` set | — |
    | `rate_required` (no rate, R32) | `failed` | automatic, next run |
    | open-period problem (invoicing R28; R23 here) | `failed` | automatic, next run |
    | third-decimal problem (OQ-62; code to be added by invoicing, e.g. `third_decimal_pending`) | `held` (R37) | human release |
    | `entity_not_vat_registered` (invoicing R94) | `held` | human release (OQ-34). *Amended 2026-10-17 (D13(h)):* once invoicing R101 is built this code is no longer raised: the "فاتورة / Invoice" is issued and posted at once and the period is `generated` |
    | `sama_rate_missing` (invoicing R100; added 2026-10-17) | `failed` | automatic, next run |
    | `third_decimal_price` (invoicing R109; added 2026-10-17) | `failed` | after a version corrects the price (R72 normally prevents it) |
    | `permission_denied` (e.g. no `system_billing` membership in the entity) | `failed` | automatic, next run |
    | any other `InvoicingError` | `failed` | automatic; CFO alerted after 3 runs (R34) |

    Invoicing warnings returned with the document (late-invoice warning R84, issue-date warning) are stored on the `BillingRunItem` (en/ar) and shown in the run summary. **[svc; AT-40, AT-45]**
55. **Status read-back.** Every run first reads the current status of documents linked to the entity's `generated` periods that are not `final` and of non-terminal adjustments' notes: `rejected` invoice → R12; `final` invoice with `waiting_original` adjustments → R47 drafts; note `issued`/`final`/`rejected` → adjustment status (R47). A human "recompute" does the same for one contract. No invoicing callback is needed. **[svc; AT-17, AT-29]**
56. **Correction drafts are read-only in their amounts.** Invoicing refuses any line change (add, delete, edit) on a draft whose `billing_adjustment` is set, for every user; the reviewer completes header fields only (OQ-26 default). **[inv; AT-48]**

### Interface with HR as built (added 2026-10-17)
57. **Port.** Billing provides `billing.hr_port.BillingHrPort` and the deployment sets `HR_BILLING_PORT = "billing.hr_port.BillingHrPort"`. `linked_assignment(entity, ref)` returns the `SecondmentAssignment` of that entity with `hr_assignment_ref = ref`, or None; `assignment_id(a)` returns `str(a.pk)`; `end_secondment(...)` calls `billing.services.end_secondment(..., source="hr_event")`. Any exception means "not delivered": HR keeps the event `pending` and retries (hr R25); billing therefore raises (never swallows) on lock timeout, validation failure or permission refusal, and has no side effect when it raises. **[svc; AT-42]**
58. **Who may send `hr_event`.** HR calls with `user` = the HR user who ended the assignment (`AssignmentEvent.actor`). For `source="hr_event"` billing requires `hr.end_assignment` in the assignment's entity (hr OQ-16 default) instead of `billing.record_contract_event`; for `source="manual"` it requires `billing.record_contract_event`. Agents and system users are refused for both. The call is access-logged with source and HR reference. **[svc + `core.access.require`; AT-42]**
59. **No manual end once linked.** Once `hr_assignment_ref` is set, `end_secondment(..., source="manual")` is refused (en/ar: "This secondment is ended in HR"), whatever the user's role (hr R26). Unlinked assignments keep D6 (manual end by accounting staff). If HR delivery has `failed` (5 attempts, hr R25) the cause is fixed and HR re-delivers; billing still refuses a manual end (hr alert text and the HR re-queue step: OQ-32). **[svc; AT-42]**
    *Confirmed 2026-10-17 (D13(d); resolves OQ-33; no code change):* once linked, only HR ends a secondment.
60. **Idempotency and order.** `end_secondment` with the end date already stored is a no-op (no event, no adjustment). HR delivers events per assignment in order (hr R25); billing applies each as R49 (earlier → credits; later → periods back to `planned` and debits). An `end_date` of `start_date − 1` (HR `cancelled` event) is accepted: nothing is billable, every planned period is cancelled and every billed day is credited through R42 to R48. Any other `end_date < start_date` is refused. On a `draft` contract only the assignment's `end_date` is stored. **[svc; AT-31, AT-43]**
61. **Withdrawn termination.** When HR withdraws a termination it restores the previous end date and emits `end_changed` with that later date: billing applies it as a later end date (R49). When HR removes an end date entirely or reverses a cancellation, HR emits nothing (hr R41, warning). Billing then offers `reopen_secondment(assignment, *, user, reason_ar)` (`billing.record_contract_event`): allowed only for a linked assignment whose HR assignment (looked up by `hr_assignment_ref`; status and end date only, no personal data) is `approved`/`active` with no end date; it clears `end_date`, puts `cancelled` periods from the old end date back to `planned`, extends the rolling schedule and creates debit adjustments for re-added days that were credited (R42 to R48). Default until OQ-31 is answered. **[svc; AT-43]**
    *Confirmed 2026-10-17 (D13(e); resolves OQ-31):* billing reopens manually; HR sends no signal. The default above is the decision.
62. **Linking.** `link_hr_assignment(assignment, hr_assignment_ref, *, user)` (`billing.record_contract_event`) is allowed on draft contracts or in a draft version and is set once (never changed after the contract or version is approved). It requires an HR assignment of the same entity, kind `secondment`, status `approved`/`active`, whose `receiving_entity` equals `contract.counterparty_entity` (or, for a third-party receiver, the contract is not intercompany). It copies `employee_ref` (= HR `employee_code`), `role_title_en/ar`, `start_date` and `end_date` from HR's billing view; never names, ID numbers or salary (hr R28, OQ-12). **[svc; AT-49]**

### Intercompany, Track C (added 2026-10-17)
63. **Separate taxpayers.** Every group entity is its own VAT taxpayer (invoicing D11, access A4; D2 here). An intercompany contract bills through a normal standard VAT invoice: VAT per the line category, ZATCA clearance, buyer snapshot from the buyer entity's own §3.0 data (VAT number, address), AR on 1210 with `counterparty = counterparty_entity`, income on the intercompany account of R27 (4110, 4210, 4310). No automatic bill, draft or record in the buyer entity (D10, R4). The buyer's staff enter the vendor bill in the buyer entity (AP on 2110 with the seller as counterparty, invoicing). **[svc + inv + ledger; AT-11, AT-50]**
    *Amended 2026-10-18 (D14(b); to build):* the buyer's vendor bill is now drafted automatically (R73); the buyer's staff review, complete and submit it, and the buyer's matrix approves it.
64. **Intercompany reconciliation report.** `billing.services.intercompany_reconciliation(as_of, *, user, group)` returns 3.12 rows for every pair of entities in `group` with a non-zero position or billing-originated documents, per transaction currency. It uses `ledger.services.intercompany_balances(as_of, user=user, group=group, by_currency=True)` (user needs `ledger.view` on the group parent; rows limited to entities where the user holds `ledger.view`) plus `billing.view` for the billing columns, in the seller entity only. A pair whose other side the user cannot see shows that side as "not visible" (en/ar), never its amount. Default tolerance 0 (OQ-30); positions cover all accounts flagged `intercompany` (1210, 1220, 2110), not only billing. Export CSV/PDF, all labels en/ar. **[svc + ledger; AT-44]**
    *Confirmed 2026-10-17 (D13(c)):* tolerance **zero** is the decision: any non-zero `difference` is shown as a break (en/ar). No code change.
65. **Secondment recharge.** A secondment line's `amount` is the agreed monthly recharge entered by accounting staff (payroll OQ-15 default: contract price). Billing reads no payroll data. When payroll is built, its monthly secondment cost report (payroll R35) is shown next to the contract for the accountant to check or amend the price through a version (R39); cost-plus or automatic cost pass-through is not built (OQ-30). Recharge lines are category `S` by default (R25) on 4210 (intercompany) or 4200. **[svc; AT-7, AT-51]**
    *Amended 2026-10-17 (D13(b); resolves OQ-30(a)):* the basis is chosen per contract line: contract price (this rule, unchanged), actual cost or cost plus a markup (R71).
    *Amended 2026-10-18 (D14(c)):* cost-plus (R71, R76), timesheets (R74) and cost pools (R75) are required **at go-live**; the R68(d) gate becomes a go-live blocker, not a deferral.

### Scheduler (added 2026-10-17; resolves OQ-19 for the build)
66. **Command.** `python manage.py billing_run [--date YYYY-MM-DD] [--entity CODE]` calls `billing.services.run_billing(run_date, entity=None, user=system_user("billing"))`. Default date: today in Asia/Riyadh. Exit code 0 on `completed` and `completed_with_errors`, non-zero on `aborted`. It is run daily at 00:30 Asia/Riyadh by a systemd timer (`Persistent=true`, so a missed run catches up after downtime) or cron; both open source. Re-running is always safe (R11): same day twice creates nothing new. **[svc + command; AT-46]**
    *Amended 2026-10-18 (D14(d); to build):* a second daily run at `BILLING_MONTH_END_RUN_TIME` (default 20:00 Asia/Riyadh, OQ-40) bills periods whose `bill_date` is that day (R77), so the invoice is generated, and therefore dated, on the last day of the service month or the earlier end date. The 00:30 run catches up anything missed (dated on its own run date, R78).
67. **Run user.** Scheduled runs act as the billing system user, which holds only `billing.start_run`, `billing.issue_from_contract`, `invoicing.create_from_billing`, `invoicing.create_adjustment_from_billing` and the read rights `create_from_billing` needs (rates: `ledger.view_rates`, to be added to `SYSTEM_EXTRA["billing"]` in `core/access/catalogue.py`). It runs only for entities where it has an active `system_billing` membership (access R20); other entities are skipped with a run-level message. A manual run started by a person (`billing.start_run`) executes as the system user and stores `initiated_by` (access R22). Agents cannot start runs (R36). **[svc + access; AT-46]**

### Build gates (added 2026-10-17)
68. Contracts that need something not built are refused at **approval** (and at version approval) with a bilingual reason, never accepted and left unbillable: (a) `billing_timing=advance` with a frequency other than `monthly` (OQ-2); (b) an advance with `recovery=offset_against_invoices` (R30, OQ-20; invoicing has no 386 document type); (c) a currency with `minor_units` 0 (R37). Deposits and advances with `recovery=none` are recorded as memo only (R31). **[svc; AT-16, AT-47]**
    *Amended 2026-10-17 (D13(a), (b)):* (a) is **withdrawn** once R70 and invoicing R99 are built (advance billing of any frequency allowed; until then the gate stays). Added (d): a secondment line with `recharge_basis` `actual_cost` or `cost_plus` is refused until payroll's fully loaded secondment cost report (payroll R35 amendment) exists.
69. All new reports, warnings, hold reasons, run messages and port errors in R52 to R68 are stored and shown in English and Arabic, and every query in them is filtered by entity and the user's permissions. **[svc + Q; AT-21, AT-44]**

### Project owner / CFO decision pack (added 2026-10-17, D13; all to build)
70. **Advance billing spread monthly (D13(a)).** A contract with `billing_timing=advance` may have any `billing_frequency`. Its period invoice is issued on `period_start` for the whole period (VAT due then, as today). When the period covers more than one calendar month, `invoice_lines` mark each line `deferred=true` and invoicing books the net to **2310 Deferred income** and releases it to the line's income account month by month by actual days (invoicing R99, E26, E27). Billing provides the release schedule per period (month, `line_key`, amount; the last month takes the remainder; amounts rounded to the contract currency's decimals) in the same frozen `invoice_lines`. Corrections (R42) on such a period produce credit or debit notes as today; invoicing splits them between 2310 and income (OQ-38). Monthly-billed contracts are unchanged. Applies in every entity; Sharia checks of R29 apply to the income accounts released to. **[svc + inv; AT-53]**
71. **Secondment recharge basis (D13(b)).** Each secondment line has `recharge_basis`:
    - `contract_price`: the line `amount` (R65, as today).
    - `actual_cost`: the month's **fully loaded** cost of the seconded employee for the days on secondment: gross pay and every benefit paid through payroll, employer GOSI, the EOSB accrual, and the employee's other costs recorded for that month (government fees such as iqama renewal, work permit and visa fees, medical insurance, other benefits), all from payroll's approved secondment cost report (payroll R35 amendment).
    - `cost_plus`: `actual_cost` × (1 + `markup_pct` / 100), rounded half-up to the contract currency's decimals.
    Cost-based lines need `billing_timing=arrears`; the period waits as `held` ("cost report not yet approved", en/ar) until payroll's report for that month is approved, then the next run bills it. The invoice line shows the period and the basis (en/ar), never names or individual pay items (PDPL, R62). The amount is read once into `invoice_lines` and never changes for that period; a later payroll correction is billed as a debit or credit adjustment through R42 to R48. D8 applies: no per-invoice approval (OQ-37). VAT category `S` by default (R25). **[svc + payroll report; AT-54]**
72. **3-decimal prices end in 0 (D13(f)).** For a contract in KWD, BHD, OMR or JOD, line `amount`, `ContractPriceStep.new_amount`, advance and deposit term amounts with a third decimal other than 0 are refused at save and at approval (`third_decimal_price`, en/ar). Computed amounts keep R37 (b) and (c). **[svc; AT-55]**

### Project-owner answers 2026-10-18 (D14; all to build)
73. **Intercompany mirror draft bill (D14(b)).** When an intercompany sales document of the seller (`counterparty_entity` set; billing-originated or entered in invoicing) becomes `final` (cleared), `invoicing.create_mirror_bill(seller_document, user=system_user("intercompany"))` creates in the buyer entity, in one transaction, a **draft** vendor bill (credit / debit note → draft vendor credit / debit note referencing the buyer's mirror of the original): vendor = the buyer's vendor whose `group_entity` = seller (missing → `MirrorBillLink` `failed`, en/ar "create the group vendor"); vendor document number = seller document number; dates, supply period, currency, lines, quantities, net, VAT category and VAT amounts copied exactly; expense account from `IntercompanyAccountMap` (missing → empty, flagged); `source="system:intercompany"`; attachment = the seller's PDF. Conditions: the buyer is an active entity with invoicing set up and `system_intercompany` holds a membership there; otherwise `skipped` with a reason. One mirror per seller document (`MirrorBillLink` unique). The buyer's staff review, complete and submit it; the buyer's matrix approves it (access R57); amounts copied from the seller cannot be changed, only header fields and the account (default, OQ-39); a buyer who disagrees rejects it with a reason and the pair shows a break in R64. Nothing posts in the buyer without that approval. All texts en/ar. **[svc inv + billing + access; AT-57, AT-58]**
74. **Timesheets (D14(c)).** A line with `billing_basis=timesheet` bills, for each period, Σ `hours` of its `approved` `TimesheetEntry` rows with `work_date` in the period × `hourly_rate`, rounded half-up to the contract currency's decimals. Entries are drafted by accounting staff or the person assisting them, submitted, and approved through the engine (activity `billing.timesheet`, not the creator); an approved entry is locked once billed. The period waits as `held` ("timesheets not yet approved", en/ar) until every submitted entry of the period is decided, or until the cut-off of OQ-42; then the next run bills it. Late approved entries for a billed period are billed as a debit adjustment (R42 to R48). The invoice shows hours, rate and period, never personal data (R62). **[svc + engine; AT-59]**
75. **Cost pools (D14(c)).** A `CostPoolPeriod` is computed by `billing.services.compute_cost_pool(pool, period, user)` from the **posted** balances of the pool's accounts in the seller entity for that month (ledger trial balance, never unposted drafts); drivers (headcount, revenue) are entered or computed per recipient and stored on the period; it is approved through the engine (activity `billing.cost_pool_period`). A line with `billing_basis=cost_pool` bills `pool_amount × share × (1 + markup_pct/100)`, share = `allocation_value` for `fixed_pct` or the recipient's driver ÷ Σ drivers; rounded half-up; the shares of one pool and period may not exceed 100% (refused at approval). Lines are arrears only; the period is `held` until the pool period is approved. A later corrected pool period (new version) is billed through R42 to R48. Pool accounts must be of the seller entity; never another entity's accounts. **[svc + ledger read + engine; AT-60]**
76. **Cost-plus (D14(c)).** `billing_basis=cost_plus` uses as its cost either a cost-pool share (R75) or the secondment cost report (R71), plus `markup_pct`. Markup income stays on the line's income account (4210 intercompany); no separate account. Sharia checks of R29 apply to the pool's accounts (a `sharia_prohibited` account in a pool of a Sharia-enforcing entity is refused). Transfer-pricing and Zakat review of the markup: OQ-37(d). **[svc; AT-60]**
77. **Invoice date = last day of the service month (D14(d)).** For a monthly period billed in arrears, `bill_date` = the last day of the service month; when the service ends earlier in the month (termination, secondment end, contract end), `bill_date` = that end date. The month-end run (R66 amended) generates such periods **on** `bill_date`, so `issue_date` = `bill_date`. Periods that are not ready on `bill_date` (`held` for timesheets, cost reports or cost pools; `failed`; system down) are generated later and dated on their actual run date (R78). Periods billed in advance keep R14 (OQ-41). Every entity uses the same rule; the run times are settings, not code. **[svc + scheduler; AT-61, AT-62]**
78. **ZATCA dating advice recorded (D14(d), team advice to the owner, 2026-10-18).** A ZATCA e-invoice's `IssueDate` (BT-2) is the date the document is generated and cannot be backdated (issue date ≤ current date, BR-KSA-04; generation time = stamping time, zatca R-23; changing the system date is prohibited, zatca OQ-18). So: (a) to carry the last day of the month (or the earlier end date), the billing run for that month must run **on that day** (R77); (b) if it runs later, `IssueDate` = the run date, and the supply period in KSA-5 (supply date) / KSA-24 (supply end date) shows the service month; the invoice is then posted in the fiscal period of the run date; (c) a tax invoice for a supply is due by the **15th of the following month** (VAT Implementing Regulations Art. 53 **[verify]**): any arrears period of month M not issued by the 13th of month M+1 raises an alert (en/ar) to the entity's accounting staff and approvers, and a period still not issued on the 15th is listed on the unresolved-differences report as "late tax invoice". The issue date is never set by hand and never earlier than the generation date. **[svc + zatca + alert job; AT-62, AT-63]**
79. **Approvals through the engine (D14(e)).** Contract approval, version approval (R39, R40), correction-note approval (R44), timesheet approval (R74) and cost-pool period approval (R75) are approval requests of the engine; eligibility comes from the entity's authority matrix (activities `billing.contract`, `billing.version`, `billing.correction_note`, `billing.timesheet`, `billing.cost_pool_period`; amount basis per access OQ-35). The module services keep their checks (R38 to R48) and are called with the approver's grant (access R61). **[svc + engine; AT-64]**
80. **Mirror bills and reconciliation.** R64 adds `mirror_status` per pair (created, skipped, failed, rejected by the buyer) and counts `final` seller documents whose mirror is not yet approved as "mirror pending". Tolerance stays zero (D13(c)). **[svc; AT-58]**
81. **Bilingual and per entity.** Every new message, hold reason, alert and report column of R73 to R80 is en/ar; every query is scoped by entity and permission; the seller's users get no right in the buyer through a mirror bill. **[svc + Q; AT-21, AT-65]**

## 5. Accounting

Billing posts nothing. Each generated invoice is posted by invoicing (`create_entry` at issue, `post_entry` after ZATCA clearance/reporting; invoicing R41) on accounts from `ledger/coa_template.py`. The tables show the entries that result. Amounts are in the entity's base currency (SAR).

**B1. Commercial lease, monthly in advance, third party** (rent 10,000.00, S 15%)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1200 Trade receivables | 11,500.00 | | |
| 4100 Rental income | | 10,000.00 | |
| 2200 VAT output | | 1,500.00 | |

**B2. Residential lease, exempt** (rent 8,000.00, E): Dr 1200 8,000.00 / Cr 4100 8,000.00. No 2200 line. *(Amended 2026-10-17: income type `rent_residential` defaults to 4120 Residential rental income (proposed, invoicing D29): Cr 4120 8,000.00.)* *(2026-10-17, D13(g): 4120 approved.)*

**B3. Intercompany secondment, ALM → SUB, March in arrears, ended 2027-03-10** (31,000.00 × 10/31 = 10,000.00, S 15%)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1210 Due from group entities | 11,500.00 | | SUB |
| 4210 Intercompany service income | | 10,000.00 | |
| 2200 VAT output | | 1,500.00 | |

**B4. Intercompany vehicle rental or lease**: as B3 with 4110 Intercompany rental income.

**B5. Foreign-currency contract** (USD 1,000.00 net, S 15%, fixture rate 3.75 SAR/USD from ledger-fx): Dr 1200 4,312.50 / Cr 4100 3,750.00 / Cr 2200 562.50. How the currency amount and rate are stored on the JE is defined by ledger-fx.md (`JournalLine` has no currency field today). *(Amended 2026-10-17, ledger-fx built: each line carries `currency` USD and `txn_amount` (1,150.00 / 1,000.00 / 150.00) with the rate pinned on the invoice at issue; functional amounts as shown. Exchange differences arise only at settlement (invoicing R76, 4700 / 5750, proposed).)*

**B6. Credit note after early end** (R40, R42 to R49; mirror of the invoice for the unused days): Dr 4100/4110/4200/4210, Dr 2200 (if S), Cr 1200 or 1210 (+ counterparty). Issued and posted by invoicing (invoicing E4) after CFO approval and ZATCA clearance.

**B7. Retroactive cancellation, credit note per billed month** (added 2026-10-16, D9; AT-23: commercial lease 10,000.00 S 15%, January invoice credited in full; note issued 2027-03-20, posted on that date in open 2027-03 even if 2027-01 is closed)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 4100 Rental income | 10,000.00 | | |
| 2200 VAT output | 1,500.00 | | |
| 1200 Trade receivables | | 11,500.00 | |
Same for February and March: three notes, one per original invoice.

**B8. Backdated price increase, debit note** (added 2026-10-16, D9; AT-25: March delta net 1,032.26, VAT 154.84 at document level, invoicing R9)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1200 Trade receivables | 1,187.10 | | |
| 4100 Rental income | | 1,032.26 | |
| 2200 VAT output | | 154.84 | |
A backdated decrease gives the B7 shape for the delta. Intercompany: 1210 + counterparty, 4110/4210 (R50).

**B9. Intercompany IP licence, ALM → SUB** (added 2026-10-17, R27 amended; 20,000.00, S 15%)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1210 Due from group entities | 23,000.00 | | SUB |
| 4310 Intercompany IP and licence income (proposed) | | 20,000.00 | |
| 2200 VAT output | | 3,000.00 | |

**B10. Buyer side, for the reconciliation (R64) only — not created by billing** (D10): SUB's staff enter ALM's B3 invoice as a vendor bill in SUB (invoicing E7): Dr expense (e.g. 5100 or 5300, SUB's choice) 10,000.00 / Dr 1310 VAT input 1,500.00 / Cr 2110 Due to group entities 11,500.00, counterparty ALM. The pair then mirrors: ALM +11,500.00 with SUB, SUB −11,500.00 with ALM, difference 0.

**B11. Commercial lease billed quarterly in advance** (added 2026-10-17, D13(a), R70; 30,000.00 net for 2027-01-01..03-31, S 15%): invoice Dr 1200 34,500.00 / Cr 2310 Deferred income 30,000.00 / Cr 2200 4,500.00; month-end releases by invoicing (invoicing E27) Dr 2310 / Cr 4100: 10,333.33 (January, 31/90), 9,333.33 (February, 28/90), 10,333.34 (March, remainder).

**B12. Intercompany secondment at cost plus 5%** (added 2026-10-17, D13(b), R71; ALM → SUB, March in arrears; fully loaded cost from payroll 20,000.00: gross pay 16,000.00, employer GOSI 1,300.00, EOSB accrual 700.00, medical insurance 1,200.00, iqama renewal and other government fees 800.00; markup 5% → 21,000.00, S 15%)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1210 Due from group entities | 24,150.00 | | SUB |
| 4210 Intercompany service income | | 21,000.00 | |
| 2200 VAT output | | 3,150.00 | |

**B13. Mirror draft bill in the buyer** (added 2026-10-18, D14(b), R73; for B3): created as a **draft** in SUB, no entry. On approval by SUB's matrix, invoicing posts in SUB: Dr SUB's mapped expense (e.g. 5100) 10,000.00 / Dr 1310 VAT input 1,500.00 / Cr 2110 Due to group entities 11,500.00, counterparty ALM (invoicing E7). Pair difference with B3: 0.

**B14. Timesheet line, ALM → SUB, March in arrears** (added 2026-10-18, R74; 40.00 approved hours × 500.00 = 20,000.00, S 15%; invoice dated 2027-03-31)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1210 Due from group entities | 23,000.00 | | SUB |
| 4210 Intercompany service income | | 20,000.00 | |
| 2200 VAT output | | 3,000.00 | |

**B15. Cost-pool recharge at cost plus 5%, ALM → SUB, March** (added 2026-10-18, R75, R76; pool "shared finance services" posted cost 100,000.00; SUB `fixed_pct` 30% → 30,000.00; × 1.05 = 31,500.00, S 15%)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1210 Due from group entities | 36,225.00 | | SUB |
| 4210 Intercompany service income | | 31,500.00 | |
| 2200 VAT output | | 4,725.00 | |
The pool's own costs stay on ALM's expense accounts (already posted); billing posts nothing.

**No entry** for: contract approval, schedule creation, runs, timesheets, cost-pool periods, mirror bill drafts (2026-10-18), held or failed periods, advance terms, deposit terms, contract versions, the correction pass, draft or discarded adjustments, HR end events, links to HR, the reconciliation report. Notes post only through invoicing (`post_entry` after ZATCA, invoicing R41). *(Amended 2026-10-17, as built: sales documents post in one step on clearance, `create_entry(post=True)` from `post_cleared_document`, invoicing R41.)*

**Accounts missing** (not invented here; see invoicing OQ-22): deferred income for multi-month in-advance billing (OQ-2), customer advances, security deposits held, intercompany IP income, FX gain/loss. *(Amended 2026-10-17: 4310 intercompany IP income and 4700 / 5750 FX gain/loss now exist in `coa_template.py` as proposed. 2310 Deferred income and 2320 Security deposits received are proposed in invoicing §5 but **not** in `coa_template.py`, so OQ-2 and OQ-20 stay blocking.)* *(Amended 2026-10-17, D13(g): 2310 and 2320 are approved; to build: add them to `coa_template.py` with a data migration (invoicing §5 note). OQ-2 is resolved by D13(a); OQ-20 stays blocking only for advance offsets (no 386).)*

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| `invoicing` (internal) | Out | Create and issue sales invoice (lines, supply period, customer, currency, VAT category, account, `source="system"`); draft credit and debit notes from `BillingAdjustment` via `create_adjustment_from_billing` (R43; invoicing R73), never issued by billing | invoicing must add `system` to its `source` choices (it lists human/import/agent) |
| `invoicing` (internal) | In | Note status (`issued`, `final`, `rejected`) and deletion of superseded drafts | Drives R46, R47 |
| `invoicing` (internal) | In | Document status (`final`, `rejected`) via callback or polling | Drives R12 |
| ZATCA (via invoicing → zatca only) | Out/In | Clearance (B2B, incl. intercompany) or reporting (B2C, e.g. residential tenants who are individuals) | No direct call from billing |
| `ledger-fx` (internal) | In | Currency list, rate for (currency, date) | Spec not in repo yet |
| HR module (future) | In | Assignment start/end events → `end_secondment` | Manual entry until HR exists (D6) |
| Ejar (Saudi rental platform) | none yet | Lease registration, rent schedules | OQ-10 **[CC-verify]** whether registration is required |
| Banks, GOSI, Mudad | none | Secondment payroll and GOSI stay with HR/payroll | n/a |
| Legacy system (Falcon) or another source system | In (one-off) | Existing contracts at cutover, already-billed periods marked `generated` with `source=import` | migration-agent; OQ-18. *Amended 2026-10-17 (D13(k)):* Almiras's source is Falcon |

*Amended 2026-10-17 (as built):*
| System | Direction | What flows | Status |
|---|---|---|---|
| `invoicing` | Out | `create_from_billing(period, user)` (R52, R53); `create_adjustment_from_billing(adjustment, user)` (R43); `discard_adjustment_draft(document, user)` (R46c, name proposed). `source="system:billing"` already exists (`UserProfile.source`); no new choice needed | Stubs raise `NotImplementedError`; invoicing to implement (R53 also needs a ledger change) |
| `invoicing` | In | Document status read back by billing each run (R55); no callback | No invoicing change |
| `invoicing` | In | `contract_allows(contract, purpose)` for advance/deposit receipts (invoicing R67): True only if the contract's approved version has that term | Provided by billing; invoicing refuses such receipts today |
| ZATCA | via `invoicing.zatca_port` only | Hand-off after issue commit; result → `on_zatca_result` → `post_cleared_document` (ZATCA queue system user) | Port default `PendingZatcaPort`: documents stay `issued`, nothing posts |
| `ledger` / ledger-fx | In | Rates (through invoicing only, R32); `intercompany_balances(..., by_currency=True)` for R64 | Built |
| `hr` | In | `AssignmentEvent` outbox → `HR_BILLING_PORT` → `BillingHrPort.end_secondment(..., source="hr_event")` (R57 to R61); `billing_view` data at link time (R62); every 5 min plus on commit | Built on HR side; billing port to build |
| `payroll` | In (report, later) | Secondment cost report (payroll R35) shown next to the contract (R65) | Payroll not built |
| OS scheduler | — | systemd timer or cron → `billing_run` (R66) | Open source; replaces the option table above |
| *Added 2026-10-18 (D14), to build:* `invoicing` (buyer entity) | Out | `create_mirror_bill(seller_document, user)` by `system_intercompany` (R73) | New invoicing service (invoicing R113) |
| `approvals` engine | Both | Contract, version, correction note, timesheet and cost-pool period requests; decisions call billing services with the approver's grant (R79) | Engine from `main` (access 3.10) |
| `ledger` | In | Posted balances of cost-pool accounts per month (R75) | Read only |
| ZATCA (via invoicing) | Out | `IssueDate` = generation date; KSA-5 / KSA-24 = service month (R78) | No change in zatca |

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
| Issue credit note drafts from R20/R21 | *Withdrawn — see D9 (rows below)* | | | | | |
| Draft / submit amendment, termination or cancellation version (R39, R40) | Y | Y | Y | | | No (default, OQ-28) |
| Approve version (R39, R40) | | Y (not creator/submitter) | | | | No |
| Create draft correction notes (R43) | | | | Y (only) | | No |
| Review, complete and submit, or discard, a correction draft (R44, R48) | Y | | Y | | | No |
| Approve (= issue) correction note (R44; invoicing R57) | | Y (not reviewer) | | | | No |
| Request "recompute corrections" (R47, R48) | Y | Y | Y | | | No |
| View versions, adjustments, unresolved-differences report | Y | Y | Y | | Y | Scoped, if granted |
| View contracts, schedules, runs | Y | Y | Y | | Y | Scoped, if granted |
| View `employee_ref` on secondments | Y | Y | Y | | Masked | No |

*Amended 2026-10-17 (role model built; codes from `core/access/catalogue.py` and access.md 3.6).* Roles: accounting staff and accountant = `accountant`; CFO = `approver`; billing service user = `system_billing` (Sys:billing); viewer; agents per gateway. All grants per entity; a role in entity A gives nothing in B.

| Action | Permission (flags) | Holders | Built in catalogue? |
|---|---|---|---|
| Create / edit draft contract | `billing.edit_contract_draft` (Ag) | accountant, approver, agent if granted | Yes |
| Approve contract | `billing.approve_contract` (A) | approver, not creator/submitter | Yes |
| Draft / submit version | `billing.edit_version_draft` | accountant, approver | **No — add** |
| Approve version | `billing.approve_version` (A); termination/cancellation also `billing.approve_termination` (A) | approver, not creator/submitter | approve_version **No — add** |
| Record secondment end (unlinked), link to HR, reopen (R59, R61, R62) | `billing.record_contract_event` | accountant, approver | Yes |
| End secondment from HR (`hr_event`, R58) | `hr.end_assignment` (held by the HR actor) | HR roles | Yes |
| Start run | `billing.start_run` | accountant, approver, system_billing | Yes |
| Release held / failed period | `billing.release_period` | accountant, approver | Yes |
| Generate and issue invoices | `billing.issue_from_contract` + `invoicing.create_from_billing` (Sys:billing) | system_billing only | Yes |
| Create / discard correction drafts in invoicing | `invoicing.create_adjustment_from_billing` (Sys:billing) | system_billing only | **No — add** |
| Review, complete, submit or discard a correction | `billing.review_correction` + `invoicing.submit_sales_document` | accountant, receivables_clerk | review_correction **No — add** |
| Approve (= issue) a correction note | `invoicing.approve_sales_document` (A, P) | approver, not reviewer | Yes |
| Recompute corrections | `billing.recompute_corrections` | accountant, approver | **No — add** |
| Intercompany reconciliation (R64) | `ledger.view` on group parent + per entity, and `billing.view` | accountant, approver, viewer | Yes |
| View billing / `employee_ref` | `billing.view` (Ag) / `billing.view_employee_ref` | as access.md 3.6 | Yes |
| Rates for `create_from_billing` | `ledger.view_rates` | add to `SYSTEM_EXTRA["billing"]` | **No — add** |

*Added 2026-10-17 (D13), to build:* drafting the monthly deferred-income release (R70, invoicing R99): billing system user (`invoicing.create_deferred_release`, Sys:billing, new); approving it: `approver` (`invoicing.approve_deferred_release`, A, P, new), not the creator. Choosing `recharge_basis` and `markup_pct`: part of the contract or version draft and approval (R39). Agents: neither.

*Added 2026-10-18 (D14), to build:* approvals in this table that name "approver" / "CFO" read "eligible under the entity's authority matrix" (R79; access R57); self-approval only by the entity's top authority (access R58). New permissions:

| Action | Permission (flags) | Holders |
|---|---|---|
| Draft / submit timesheet entries | `billing.edit_timesheet` (Ag: no) | accountant, personal_assistant (draft only), approver |
| Approve timesheet entries | `billing.approve_timesheet` (A) | per matrix `billing.timesheet` |
| Maintain cost pools; compute and submit a pool period | `billing.manage_cost_pool` | accountant |
| Approve a cost-pool period | `billing.approve_cost_pool_period` (A) | per matrix `billing.cost_pool_period` |
| Maintain `IntercompanyAccountMap` (buyer) | `billing.manage_ic_account_map` | accountant of the buyer entity |
| Create a mirror draft bill in the buyer | `invoicing.create_mirror_bill` (Sys:intercompany) | `system_intercompany` only, with a buyer-entity membership |
| Review, complete, submit a mirror bill | `invoicing.submit_bill` | payables_clerk, accountant of the buyer |
| Approve (= post) a mirror bill | `invoicing.approve_bill` (A, P) | per the buyer's matrix `invoicing.bill` |

`billing.create_credit_note_draft` (catalogue, pre-D9) is replaced by `billing.review_correction` + `invoicing.create_adjustment_from_billing` and is to be removed from the catalogue. Catalogue changes are made by the billing builder with the access spec owner's review (access R13).

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
| AT-8 | *Withdrawn — see D9 (now AT-31).* | | |
| AT-9 | Annual vehicle rental, monthly in arrears; termination version with `terminated_on` 2027-05-15 drafted by S | Before and after C approves | Before: billing unchanged. After: May prorated 15/31; June onward `cancelled`; status `terminated`, `termination_kind=early_termination` (R40; was R21). |
| AT-10 | Fiscal period 2027-03 closed; March period due | Run 2027-03-02. Separately: run on 2027-04-01 with a catch-up period whose supply period is wholly in closed 2027-02 | First: item `failed` "period closed" (en/ar), no document or number consumed, retried next run. Second: period `held`, no invoice until released (R23). |
| AT-11 | ALM customer with `group_entity=SUB`; IC rental contract 5,000.00 S | Approve and run | Contract `counterparty_entity=SUB` (not editable). Invoice is a standard VAT invoice; JE Dr 1210 5,750.00 (counterparty SUB) / Cr 4110 5,000.00 / Cr 2200 750.00. No record created in SUB. An IC IP line → rejected (R3, R4, R26, R27). *Amended 2026-10-17:* the IC IP line is accepted on 4310 (AT-50). |
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

Added 2026-10-16 (D8 to D10). Common fixture for AT-23 to AT-30: ALM commercial lease L1, 10,000.00/month net, S 15%, monthly in advance, `calendar` anchor, start 2027-01-01, end 2027-12-31, version 1 approved by C; invoices for the months named are `final` unless stated. Notes are submitted by S and approved by C unless stated.

| # | Given | When | Then |
|---|---|---|---|
| AT-22 | L1 approved (version 1) | S drafts version 2 (amount 11,000.00, effective 2027-02-01, `reason_ar` set) and submits; S approves; C approves; anyone edits version 1 or 2 afterwards | S approval rejected (not CFO / own). C: version 2 `approved`, `current_version` = 2, `version_approved` event. Edits of approved versions rejected. Version 2 without `reason_ar`: rejected at submit (R38, R39). |
| AT-23 | L1, January to March invoiced; on 2027-03-20 S records that the lease never started | S drafts a `cancellation` version; C approves it | Three `credit` adjustments, one per original invoice, each net 10,000.00, VAT 1,500.00; three **draft** credit notes with `source=system`, `original_document` = the month's invoice, supply period = the month; none issued. Status `terminated`, `termination_kind=cancellation`; April to December periods `cancelled`; the 2027-04-01 run generates nothing. Staff summary sent (en/ar). After S submits (with `payment_means_code`) and C approves each, then ZATCA stub `cleared`: JEs as B7 dated 2027-03-20. The three issued invoices are unchanged (R40 to R45, R51). |
| AT-24 | L1, January to March invoiced; on 2027-03-20 S records a termination with `terminated_on` 2027-01-15 | C approves | January: `credit` net 5,161.29 (= 10,000.00 − round(10,000.00 × 15/31, 2) = 10,000.00 − 4,838.71), VAT 774.19. February and March: `credit` net 10,000.00 each. April onward `cancelled` (R40, R42). |
| AT-25 | L1, March to May invoiced at 10,000.00; on 2027-05-10 S records a price change to 12,000.00 effective **2027-03-16** | C approves version 2 | March: `debit` net 1,032.26 (= round(10,000.00 × 15/31, 2) 4,838.71 + round(12,000.00 × 16/31, 2) 6,193.55 − 10,000.00), VAT 154.84, gross 1,187.10 (B8). April and May: `debit` net 2,000.00 each. Three draft debit notes. The June run invoices 12,000.00. A decrease to 9,000.00 instead: same shape with `credit` adjustments (R42, R43). |
| AT-26 | AT-25 done; drafts not yet issued | (a) "recompute" with no new version; (b) re-run billing for March to May; (c) S/C issue only the April debit note, then C approves version 3 (back to 10,000.00 effective 2027-04-01) | (a) nothing created. (b) every item `skipped_already_billed`, no document, no number consumed. (c) March and May drafts `superseded` and deleted (drafts only); April debit note kept. New pass: March `debit` 1,032.26 (version 2 still governs 16 to 31 March); April `credit` 2,000.00; May nothing. After all notes final, per period invoice + DN − CN = required: March 11,032.26, April 10,000.00, May 10,000.00. No period has two invoices (R41, R46). |
| AT-27 | A billing run for ALM holds the advisory lock | C approves a version for an ALM contract at the same moment | Approval waits for the lock (or fails with a bilingual "billing run in progress, retry" message); no period is both generated with old terms after approval and left uncorrected (R41). |
| AT-28 | AT-24 setup; fiscal 2027-01 and 2027-02 closed; 2027-03 open; then 2027-03 closed and 2027-04 open | C approves the January credit note on 2027-03-22; separately approves a note on a date whose period is closed | First: JE dated 2027-03-22 in 2027-03; note's supply period 2027-01-16 to 2027-01-31. Second: approval rejected "period closed" (en/ar), note stays `submitted`, no number consumed. No period reopened (R45). |
| AT-29 | March invoice issued, ZATCA stub `pending` | C approves a version effective 2027-03-01; then stub returns `cleared` for March; later ZATCA rejects the March correction note | March adjustment `waiting_original`, no draft note; after clearance, draft note created. After rejection: adjustment `failed`; the next "recompute" creates a new adjustment and draft; the rejected note keeps its number (R47). |
| AT-30 | AT-25 drafts exist | S discards the April draft without a reason; then with `discard_reason_ar` | First rejected. Second: adjustment `discarded`, shown on the unresolved-differences report (en/ar) for ALM only; a "recompute" for the same version does not re-create it; a new version does (R48). |
| AT-31 | ALM → SUB secondment, 30,000.00/month, advance, S; April invoice `final`; S records end date 2027-04-10 | `end_secondment`; then the same date again; then, after the April credit note is final, end date moved to 2027-04-20 | Draft credit note net 20,000.00 (= 30,000.00 − round(30,000.00 × 10/30, 2)), not issued until S submits and C approves; May onward `cancelled`. Same date again: no-op. Moved later: May stays cancelled, draft debit note net 10,000.00 for 11 to 20 April (R19, R49). |
| AT-32 | ALM IC rental contract with SUB, 5,000.00 S, January invoiced | On 2027-02-10 C approves a retroactive cancellation | Draft credit note on 1210 with counterparty SUB (Dr 4110 5,000.00, Dr 2200 750.00, Cr 1210 5,750.00 on final). No document or bill created in SUB (R4, R50, D10). |
| AT-33 | Agent service user with draft-contract rights | Drafts a version; submits or discards a correction; approves a version or a note | All rejected (R44, OQ-28 default). |
| AT-34 | L1 with March invoiced | Versions: effective 2026-12-01 (before start); currency USD; customer changed; frequency `quarterly` effective 2027-02-01 | All rejected at submit with bilingual messages. Frequency change effective 2027-04-01 (after the last generated period): accepted (R39). |
| AT-35 | ALM `auto_issue_from_approved_contract=True` | Run generates April invoice; a version creates a correction | Invoice issued with no per-invoice approval, `approved_by` = contract approver (D8). Correction note stays `draft`; the billing system user's submit or approve call is rejected (R35, R44). |
| AT-36 | ALM Sharia enforced | Version adds a line on a `sharia_prohibited` account; a version line on 4900; render version, adjustment and report in `ar` | First rejected at save and approval. Second accepted; resulting notes `requires_purification=True`. Arabic text shown everywhere (R39, R50). |
| AT-37 | *Added 2026-10-17 (D11, R37).* ALM (SAR) lease in KWD 1,000.000/month, S 15%, `calendar` anchor, start 2027-01-15; a JPY contract; a KWD line 1.2345 | Approve; run 2027-01-15; save the others | January net = round_half_up(1,000.000 × 17/31, 3) = 548.387; invoice in KWD with 3-dp amounts, SAR VAT 2 dp (invoicing R93). JPY contract and the 4-dp line refused (en/ar). *Amended 2026-10-17 (R37, OQ-62 default):* the January period is `held` "3-decimal amounts await ZATCA confirmation" (548.387 has a non-zero third decimal), no document, no number used; see AT-45 |
| AT-38 | *Added 2026-10-17 (D12, R43).* L1 (AT-23 fixture), January invoiced; on 2027-03-20 S records a termination effective 2027-01-15; C approves; the credit note is approved on 2027-04-20 | Read the draft and the issued note | Draft `adjustment_event_date` empty; issued note dated 2027-04-20, VAT in the period of 2027-04-20; supply period 2027-01-15 to 2027-01-31; no 15-day warning |

Added 2026-10-17 (build against what is built). Extra fixtures: system users `billing` and `zatca_queue` with memberships in ALM and SUB; `invoicing.zatca_port` fake returning `cleared` unless stated; `hr.testing.FakeBillingPort` replaced by `BillingHrPort` where stated; HR user H (`hr.end_assignment` in ALM); viewer V with `ledger.view` on ALM (group parent) and ALM only; SUB has its own VAT number and §3.0 data.

| # | Given | When | Then |
|---|---|---|---|
| AT-39 | L1 approved by C; ALM `auto_issue_from_approved_contract=True` | Run 2027-01-01; ZATCA fake `cleared`; run again; same with the switch False | Invoice via `create_from_billing(period, user=system_user("billing"))`: `source="system:billing"`, `billing_item`=period, `approved_by`=C, `issued`, number used, port called after commit. On `cleared`: `post_cleared_document` by the queue user posts B1 with `posted_by`=C, document `final`. Second run: no new document. Switch False: document `submitted`, period `generated`; C approves it; posts on clearance (R13, R35, R52, R53) |
| AT-40 | L1 due; (a) fiscal period closed; (b) invoicing fake raises `rate_required`; (c) `system_billing` has no SUB membership and a SUB contract is due | Run | (a), (b): period `failed`, `error_code` stored, no document, no number used, retried next run. (c): SUB skipped with a run message; ALM unaffected (R23, R54, R67) |
| AT-41 | AT-39 invoice; ZATCA fake `rejected` | Next run; human releases; next run | Run sync sets period `failed`, document in `superseded_documents`; after release a new document with a new number; both documents link to the same period; DB allows it (one non-rejected document per period) (R12, R55) |
| AT-42 | ALM → SUB secondment linked to `ALM-ASG-2027-000001`; `HR_BILLING_PORT=BillingHrPort` | H ends it in HR 2027-03-10; delivery job runs twice; S calls `end_secondment(..., source="manual")`; an unlinked assignment ended manually by S; an agent calls `end_secondment`; a user without `hr.end_assignment` sends `hr_event` | Billing end date 2027-03-10, `end_source=hr_event`, credit adjustments per R49; second delivery no-op; S's manual end on the linked one refused (en/ar); unlinked manual end accepted; agent refused; `hr_event` without the right refused and the HR event stays `pending` with the error (R57 to R60) |
| AT-43 | Linked secondments; (a) one starting 2027-04-01, nothing billed yet, HR `cancelled` event with end 2027-03-31; (b) another with April invoiced: termination withdrawn: HR `end_changed` back to 2027-06-30 after an end 2027-04-10; (c) HR end date removed | Deliver; S calls `reopen_secondment` for (c); S calls it while HR still shows an end date | (a) accepted, every period `cancelled`, no adjustment. (b) later end: cancelled periods back to `planned`, debit adjustment for 11 to 30 April. (c) reopen accepted, end cleared, schedule extended; reopen while HR has an end date refused (R60, R61) |
| AT-44 | ALM invoiced SUB 11,500.00 SAR (B3, final) and USD 1,150.00 (final); SUB booked a vendor bill for the SAR invoice only (B10) | C runs `intercompany_reconciliation(2027-03-31, group=ALM)`; V runs it | C: SAR row difference 0; USD row seller +1,150.00, buyer 0, difference 1,150.00, `billing_open` 1,150.00. V: ALM side only, SUB side "not visible" (en/ar), no SUB amount shown; Arabic and English labels (R64, R69) |
| AT-45 | ALM KWD lease 1,000.000/month S 15%, start 2027-01-15 | C approves; runs 2027-01-15 and 2027-02-01; S releases January without change | Approval shows the 3-decimal warning. January `held` (548.387); February issued (net 1,000.000, VAT 150.000, all third decimals 0). Release: January `planned`, next run `held` again, no number used (R37, R54) |
| AT-46 | ALM and SUB with due periods | `billing_run` twice on 2027-03-01; S starts a manual run; agent tries | One invoice per period; second run all `skipped_already_billed`; exit code 0. Manual run: `started_by` = system user, `initiated_by` = S. Agent refused (R11, R66, R67) |
| AT-47 | Contract quarterly in advance; contract with an advance offset; JPY contract | C approves each | All refused at approval with bilingual reasons naming OQ-2, OQ-20 and 0-decimal currencies; nothing scheduled (R68) |
| AT-48 | AT-25 drafts exist | S edits a line amount on a draft; S fills `payment_means_code` and submits; C approves a new version superseding it | Line edit refused; header edit accepted. On the version approval the submitted note is returned to draft and deleted through invoicing in one transaction; issued notes untouched (R43, R46, R56) |
| AT-49 | HR assignment to SUB; ALM contract whose customer has `group_entity=PART` | S links it; S links a correct one; S changes the link after approval | First refused (receiving entity ≠ counterparty). Second: `employee_ref`, role titles, dates copied; no name or ID stored. Change after approval refused (R62) |
| AT-50 | ALM → SUB IP licence 20,000.00 S, income type `ip_licence` | Approve and run; ZATCA `cleared` | Income account defaults to 4310; JE as B9 with counterparty SUB; nothing created in SUB (R27, R63) |
| AT-51 | ALM → SUB secondment 31,000.00/month entered by S | Run; payroll absent | Invoice uses the contract amount; no payroll read; line S on 4210 (R65) |
| AT-52 | ALM has no VAT number (fixture variant) | Run with a due period | Period `held` with the invoicing R94 reason (en/ar); no document (R54, OQ-34). *Amended 2026-10-17 (D13(h)):* once invoicing R101 is built: a "فاتورة / Invoice" without VAT is issued and posted at once, period `generated`, nothing handed to ZATCA |
| AT-53 | *Added 2026-10-17 (D13(a), R70; to build).* ALM commercial lease 30,000.00 per quarter, `advance`, `quarterly`, start 2027-01-01, approved by C; a yearly-in-advance contract | Approve both; run 2027-01-01; month-end releases | Both approved (R68(a) no longer refuses); January invoice as B11 with `invoice_lines` holding the release schedule 10,333.33 / 9,333.33 / 10,333.34; releases posted by invoicing (invoicing AT-116); income in each month equals that month's share; the yearly contract releases 1/365 per day by month |
| AT-54 | *Added 2026-10-17 (D13(b), R71; to build).* ALM → SUB secondment lines: L1 `contract_price` 31,000.00; L2 `cost_plus` 5%, `arrears`; L3 `actual_cost` with `advance`; payroll's approved March report for L2's employee: fully loaded 20,000.00 | Approve; run 2027-04-01 before the March report is approved; approve the report; run again; payroll corrects March cost to 20,200.00 | L3 refused at approval (cost basis needs arrears); first run: L1 billed, L2 period `held` ("cost report not yet approved", en/ar); second run: L2 invoiced 21,000.00 + VAT (B12); correction: a draft debit note for 210.00 net (R42 to R48); no names or pay items on the invoice |
| AT-55 | *Added 2026-10-17 (D13(f), R72; to build).* KWD contract | Line amount 1,000.125; 1,000.120; price step 1,100.005; a USD contract line 1,000.125 | 1,000.125 and 1,100.005 refused (`third_decimal_price`, en/ar); 1,000.120 accepted; USD 1,000.125 refused by the USD decimals (ledger-fx R8) |
| AT-56 | *Added 2026-10-17 (D13(c), R64).* AT-44 data with a SAR difference of 0.01 | Run the reconciliation | The pair shows a break of 0.01 (tolerance zero), en/ar |

*Added 2026-10-18 (D14; all to build).* Extra fixtures: system user `intercompany` with memberships in ALM and SUB; SUB vendor V-ALM with `group_entity=ALM`; SUB `IntercompanyAccountMap` (ALM, `secondment`) → 5100; matrices as access §8 (2026-10-18 fixtures); ZATCA fake `cleared`.

| # | Given | When | Then |
|---|---|---|---|
| AT-57 | AT-7 secondment invoice ALM → SUB (net 10,000.00, VAT 1,500.00) | ZATCA `cleared`; repeat the hand-off; SUB staff edit the net to 9,000.00; SUB staff set the account and submit; SUB approver approves | One draft bill in SUB (vendor V-ALM, vendor doc no. = ALM invoice no., same supply period, net, VAT), `source="system:intercompany"`, `MirrorBillLink` `created`; second hand-off creates nothing; amount edit refused; submit accepted; approval posts B13; no ALM user gained a SUB right (R73, R81) |
| AT-58 | (a) SUB has no vendor with `group_entity=ALM`; (b) `system_intercompany` has no SUB membership; (c) a credit note on the AT-57 invoice becomes `final`; (d) SUB rejects a mirror bill with a reason | Hand-off; reconciliation | (a) `failed`, en/ar "create the group vendor", nothing in SUB; (b) `skipped` with reason; (c) draft vendor credit note in SUB referencing the mirror bill; (d) bill `rejected`, pair shows a break and `mirror_status` = rejected (R73, R80) |
| AT-59 | ALM → SUB timesheet line 500.00/hour; March entries 30 h approved, 10 h submitted | Month-end run 2027-03-31; approve the 10 h on 04-02 (approver ≠ creator); run 04-03; a 2 h entry for March approved on 04-10 | 03-31: period `held` (en/ar); 04-03: invoice 20,000.00 + VAT (B14) dated 2027-04-03 with supply 2027-03-01..03-31 (R78); 04-10: draft debit adjustment for 1,000.00 net; entries locked after billing; creator's approval refused (R74) |
| AT-60 | ALM pool "shared finance services", accounts 5900, March posted cost 100,000.00 plus a 5,000.00 draft; SUB 30% `fixed_pct`, markup 5%; PART 80% on the same pool | Compute the pool period; approve; run; a pool account `sharia_prohibited` in ALM | Pool amount 100,000.00 (draft ignored); shares 110% refused at approval; with PART 70%: SUB invoice 31,500.00 + 4,725.00 VAT (B15); prohibited account refused (R75, R76) |
| AT-61 | L1-type ALM lease monthly in arrears, 10,000.00; SUB secondment ending 2027-03-10 | Month-end run 2027-03-31 20:00; run 2027-03-10 20:00 | Lease invoice `issue_date` 2027-03-31, supply 03-01..03-31; secondment invoice `issue_date` 2027-03-10, supply 03-01..03-10, prorated; both posted in 2027-03 on clearance (R77) |
| AT-62 | Month-end run missed (system down) on 2027-03-31 | 00:30 run on 2027-04-01 | Invoice `issue_date` 2027-04-01 (never 03-31), supply 03-01..03-31 (KSA-5 / KSA-24), posted in 2027-04; no manual issue date accepted (R78) |
| AT-63 | March period still `held` on 2027-04-13 and on 2027-04-16 | Alert job | 04-13: alert (en/ar) to ALM accounting staff and approvers; 04-16: "late tax invoice" row on the report (R78) |
| AT-64 | ALM matrix `billing.contract` 0–1,000,000.00 → `approver`; above → `approver` then `managing_partner`; contract value 1,200,000.00 | Approver approves; MP approves; an approver approves directly through the billing service without a request | Two steps, number and schedule after the MP step; direct call refused `approval_request_required` (R79) |
| AT-65 | ALM user U (accountant ALM only) | U opens the SUB mirror bill created from ALM's invoice | 404 (not found), `permission_denied` logged (R81) |
| AT-66 | Quarterly-in-advance lease (R70) | Run 2027-01-01 | Unchanged: issued on `period_start` (R14, OQ-41 default) |
| AT-67 | ALM USD contract invoice dated 2027-03-31; SAMA USD rows on 03-31 and 03-15 | Issue | `total_vat_sar` at the 03-31 SAMA rate (invoice date, invoicing R100 amended) (R32 note) |

## 9. Open questions

**BLOCKING** = the builder cannot build that part without an answer. **Default** = safe default used in the rules, easy to change.

| # | Question | Owner | Status |
|---|---|---|---|
| OQ-1 | Do auto-generated invoices from a CFO-approved contract need per-invoice approval, e.g. above an amount, or for intercompany? | Project owner / CFO | **Resolved by D8**: no per-invoice approval (R35). Correction notes are not covered and always need CFO approval (D9, R44). |
| OQ-2 | Revenue recognition for multi-month in-advance billing (quarterly/annual): invoicing credits income in full at issue, and there is no deferred income account (invoicing OQ-22). | Accountant | **BLOCKING** for `advance` timing with non-monthly frequency. Default: such contracts cannot be approved until the account and deferral method exist. *2026-10-17:* still blocking. 2310 Deferred income is proposed in invoicing §5 (for 386 advances) but is not in `coa_template.py`; whether 2310 is also used for billed-in-advance periods, and the release method (monthly straight-line JE by billing? by invoicing?), are undecided. Refused at approval (R68a). **Resolved by D13(a) (2026-10-17):** allowed; income spread monthly through 2310 (R70; invoicing R99, posted by invoicing under policy A). Release posting authority: OQ-36. |
| OQ-3 | Proration method (actual days vs 30/360 vs whole months) and period anchor (calendar vs contract start). | Accountant | Default: actual days; `calendar` anchor (R16, R17). |
| OQ-4 | Escalation types: fixed %, fixed amount, compounding, index-linked (index-linked needs a Sharia check), mid-period effect. | Accountant + Sharia board | Default: explicit stepped amounts only, from the next period. |
| OQ-5 | Billing date vs supply date for ZATCA/VAT: tax point for continuous supplies (rent, rentals, secondments), whether the issue date may precede the supply period (lead days), and the rule against backdating **[CC-verify]**. | compliance-checker / tax adviser | Default: issue on run date, `bill_lead_days=0`, supply = period. **Resolved by D14(d) (2026-10-18):** invoice dated on the last day of the service month (or the earlier end date) by running on that day; never backdated; later runs carry the run date with the service month as supply period (R77, R78). Still **[verify]**: the 15th-of-the-following-month deadline (VAT IR Art. 53) by the compliance-checker. |
| OQ-6 | Refunds or credit for already-billed days after an early end (lease and rental terms may forbid refunds). | Accountant / Managing Partner | **Resolved by D9**: draft credit notes, reviewed by a person, CFO approves (R42 to R44); where terms forbid a refund the reviewer discards the draft with a reason (R48). |
| OQ-7 | Amendments and termination: does every change need CFO approval, and is there a notice-period rule? | Project owner | **Resolved by D9** for approval: amendments, terminations and cancellations are versioned and CFO-approved, effective date may be past (R38 to R40); secondment end dates need none (D6). Still open: notice-period rule (default: none enforced). |
| OQ-8 | Is there one CFO per entity or a group CFO? May a CFO approve a contract they created? | Project owner | Default: CFO role per entity; approver ≠ creator. |
| OQ-9 | Open-ended contracts and auto-renewal (leases, vehicle rentals). | Accountant | Default: end date required except secondment/service; no auto-renewal. |
| OQ-10 | Ejar: is registration of residential (and commercial) leases mandatory for these entities, and should the ERP integrate (contract number, payment schedule) **[CC-verify]**? | compliance-checker / Managing Partner | Default: Ejar number recorded only; no integration. |
| OQ-11 | Should an intercompany invoice create a draft mirror bill in the receiving entity? | Accountant | **Resolved by D10**: no automatic bill; the buyer entity's staff enter it (R4). Mismatches show in `intercompany_balances`. **Re-decided by D14(b) (2026-10-18):** automatic mirror **draft** bill in the buyer (R73). |
| OQ-12 | PDPL: which secondment data may appear on invoices to the receiving entity (name, role, days)? | Legal / HR | Default: role title and pseudonymous `employee_ref` only. |
| OQ-13 | Billing for a supply period in a closed fiscal period: issue in the current period, or require a human decision? | Accountant | Default: `held` until released (R23). |
| OQ-14 | Contract number format. | Accountant | Default `{entity.code}-CTR-{year}-{000001}`. |
| OQ-15 | Mixed-use leases (residential + commercial in one contract) and VAT on lease service charges, utilities, parking **[CC-verify]**. | Tax adviser | Default: one `lease_use` per contract; separate lines with their own category for extras. |
| OQ-16 | Foreign-currency contracts: rate on invoice date, on supply date, or a contract-fixed rate; which rate source (ledger-fx). | Accountant | Default: ledger-fx rate on issue date (R32). **BLOCKING** for non-base currencies until ledger-fx.md and invoicing OQ-6 are done. *2026-10-17:* **no longer blocking.** ledger-fx and invoicing multi-currency are built: rate on the invoice issue date, pinned at issue (R32 amended). Default stays (accountant to confirm); a contract-fixed rate would need an invoicing change. |
| OQ-17 | Notification channel (in-app, e-mail) and escalation recipients. | Accountant | Default: in-app to entity accounting staff; CFO after 3 failed runs. |
| OQ-18 | Migration of existing contracts from the legacy system (Falcon for Almiras, D13(k); was "Daftra/SAP B1"), and how already-billed periods are marked. | Accountant + migration-agent | Default: `source=import`, past periods `generated` with no document link. |
| OQ-19 | Scheduled job runner (section 6 options). | Dev | Default for build: management command; runner chosen before deployment. *2026-10-17:* resolved for the build by R66 (management command `billing_run`, systemd timer or cron). |
| OQ-20 | Advance (386) and security deposit handling: accounts, VAT treatment of deposits, offset against invoices. **Needs tax adviser.** | Accountant / tax adviser | **BLOCKING** for advance offsets (R30). Deposits memo only (R31). *2026-10-17:* still blocking: invoicing has no 386 document type, refuses advance and deposit receipts, and 2310 / 2320 are not in `coa_template.py`; deposit VAT treatment awaits the tax adviser (invoicing D30c). Contracts with an advance offset are refused (R68b). *Partly resolved 2026-10-17 (D13(g), (i)):* 2310 and 2320 approved; deposit VAT decided for receipt and application (invoicing R106). Still **BLOCKING** for advance offsets (no 386 until a current ZATCA SDK confirms it, zatca OQ-33); deposit forfeiture open (invoicing OQ-16). |
| OQ-21 | Vehicle rental extras (excess mileage, damage, traffic fines recharge). | Accountant | Default: out of scope; billed manually through invoicing. |
| OQ-22 | Exact invoicing service names and the status callback (invoicing.md OQ-2); adding `source="system"` to invoicing. | invoicing spec author | **BLOCKING** for R13; build against a stub. Now also covers `create_adjustment_from_billing` (R43; invoicing R73). *2026-10-17:* names and signatures settled (R13 amended); `source` exists. Still **BLOCKING for go-live, not for building billing**: invoicing must implement R52 to R56 (both functions are stubs; FKs `billing_item` / `billing_adjustment`; one non-rejected document per period; distinct error codes; read-only correction lines; draft deletion service) and invoicing + ledger + access must accept the contract/version approval as the recorded approval for posting on clearance (R53). |
| OQ-23 | *Added 2026-10-16 (D9).* Which terms may an amendment change retroactively: customer, currency, contract type, billing frequency/timing/anchor? | Accountant / Managing Partner | Default (R39): customer, currency, type never (new contract); frequency/timing/anchor only after the last generated period. |
| OQ-24 | *Added 2026-10-16 (D9).* Mid-period effect of an amendment: actual days from the effective date (R42) or from the next period (as OQ-4 does for price steps)? | Accountant | Default: actual days by segment, rounded per segment. |
| OQ-25 | *Added 2026-10-16 (D9).* VAT timing of backdated corrections: which date is the note's `adjustment_event_date` (effective date vs date the change was recorded) for the 15-day note rule (invoicing R71, TIR §7.2), and how far back corrections may go (prior VAT periods, prior audited fiscal years). **[Needs expert sign-off: tax]** | Tax adviser / accountant | Default: effective date; warning only (invoicing R71); no limit other than `start_date`. **Resolved by D12 (2026-10-17)** for the date: the note's own date counts (R43, invoicing R95). Still open, for the CFO's Word document: how far back corrections may go (prior VAT periods, prior audited years) **[Needs expert sign-off: tax]**; default: no limit other than `start_date`. |
| OQ-26 | *Added 2026-10-16 (D9).* May the reviewer change computed correction amounts (e.g. a negotiated partial refund)? | Accountant / CFO | Default: no; discard (R48) and record a new version. |
| OQ-27 | *Added 2026-10-16 (D9).* Amending `ended` or `terminated` contracts: extension after end, undoing a termination, re-activation. | Accountant / Managing Partner | Default: versions with effective date ≤ the end/termination date allowed (corrections); extension, undo and re-activation need a new contract. |
| OQ-28 | *Added 2026-10-16 (D9).* May agents draft contract versions (R36 lets them draft contracts)? | Managing Partner | Default: no. |
| OQ-29 | *Added 2026-10-16 (CLAUDE.md rule 7 amended).* Contracts in 3-decimal currencies (KWD, BHD, OMR, JOD) or 0-decimal (JPY): billing money fields are 2 dp; invoicing documents too (invoicing OQ-48). | Accountant + Dev | Default: contracts in currencies with `minor_units` ≠ 2 rejected (R37). **Resolved by D11 (2026-10-17)** for 3-decimal currencies (R37 amended); 0-decimal currencies: invoicing OQ-58. |

*Added 2026-10-17 (Track C build prep).*

| # | Question | Owner | Status |
|---|---|---|---|
| OQ-30 | Intercompany pricing and reconciliation: (a) is a secondment recharged at the entered contract price, at actual payroll cost, or cost plus a markup (transfer pricing / Zakat view)? (b) what difference is tolerated in the reconciliation report (R64), and should it split per account (1210 vs 2110) rather than net positions? | Managing Partner + CFO | Default: contract price entered by staff (payroll OQ-15); tolerance 0; net positions from `intercompany_balances`. **Resolved by D13(b), (c) (2026-10-17):** (a) any of the three per contract, actual cost fully loaded (R71); (b) tolerance zero. Still open: whether the report should split per account (1210 vs 2110) rather than net positions (default: net) |
| OQ-31 | When HR removes a secondment's end date or reverses a cancellation (termination withdrawn), HR sends nothing to billing. Reopen in billing by accounting staff (R61 default), or should HR send a `reopened` event (hr change)? | Managing Partner + HR spec owner | Default: `reopen_secondment` in billing, checked against HR (R61). **Resolved by D13(e) (2026-10-17):** billing reopens manually |
| OQ-32 | HR delivery `failed` after 5 attempts: hr's alert says "record it in billing", but billing refuses a manual end on linked secondments (R59). HR needs a re-queue step for failed events (and the alert text changed). | HR spec owner / Dev | Default: R59 refusal stands; until HR adds re-queue, the event is fixed and re-queued by a developer through a reviewed data fix. Not blocking billing |
| OQ-33 | Confirm that for secondments linked to HR only HR may end them (hr R26, R59 here), superseding D6 for linked ones. | Project owner | Default: yes (hr OQ-16 default). **Resolved by D13(d) (2026-10-17):** yes |
| OQ-34 | Entities without a VAT number (invoicing R94, OQ-59): may they have billing contracts at all, and what document do they issue? | CFO (Word document) **[Needs expert sign-off: tax]** | Default: contracts allowed, periods `held` (R54). **Resolved by D13(h) (2026-10-17):** contracts allowed; they issue "فاتورة / Invoice" with no VAT (invoicing R101); periods are generated, not held, once R101 is built |
| OQ-35 | 3-decimal contracts while ZATCA 3-dp XML is unconfirmed (invoicing OQ-62): hold invoices whose amounts have a non-zero third decimal (default), or agree prices/terms so amounts end in 0 (e.g. price per period chosen so net and VAT end in 0)? Billing never rounds a price on its own. | Project owner / CFO | Default: hold (R37 amended). **Answered for prices by D13(f) (2026-10-17):** price in amounts ending in 0 (R72). Still open: computed amounts (prorations, VAT) that end in a non-zero third decimal stay `held` (R37 (b)); whether to round them instead |
| OQ-36 | *Added 2026-10-17 (D13(a)).* Who posts the monthly deferred-income release (R70, invoicing R99): drafted by the billing system user and approved by the CFO each month (default), or posted automatically by a system user under the contract approval (as auto-issued invoices, D8)? | CFO | Default: system user drafts, CFO approves |
| OQ-37 | *Added 2026-10-17 (D13(b)).* Cost-based recharges (R71): (a) do invoices with variable cost amounts still need no per-invoice approval (D8)? (b) are yearly government fees (iqama renewal, insurance premiums) recharged in the month paid or spread over the months they cover? (c) which costs count as "all benefits" beyond payroll (air tickets, housing in kind, training)? (d) transfer-pricing and Zakat review of the markup. | Managing Partner + CFO | Default: (a) no per-invoice approval; (b) in the month the cost is recorded; (c) only costs recorded against the employee in payroll's cost report; (d) none in the ERP. **(a) resolved by D14(a) (2026-10-18):** one approval per contract, no per-invoice approval |
| OQ-38 | *Added 2026-10-17 (D13(a)).* Corrections on an advance-billed period (R42 on R70 periods): how is a credit note split between 2310 (not yet released) and income (already released)? | CFO | Default: unreleased part first from 2310, the rest from income (invoicing R99) |

*Added 2026-10-18 (D14).*

| # | Question | Owner | Status |
|---|---|---|---|
| OQ-39 | Mirror bills (R73): (a) for every intercompany sales document, or only billing-originated ones; (b) may the buyer change copied amounts (e.g. a disputed line), or only reject; (c) for buyers that are partly-owned or minority entities, is a mirror wanted (their other shareholders); (d) is a mirror created when the seller document is `issued` (before clearance) or only when `final`? | Project owner + CFO | Default: (a) all; (b) reject only, header and account editable; (c) yes when the buyer is on the ERP with a `system_intercompany` membership; (d) only `final` |
| OQ-40 | Month-end run time on the last day (R66 amended, R77): a service not yet complete at generation time (e.g. a late evening run) still gets the last day's date; acceptable run time? | Accountant | Default 20:00 Asia/Riyadh |
| OQ-41 | Advance-billed periods: the owner's "last day of the service month" names arrears billing; do advance-billed periods (R14, R70) keep `period_start`? | Project owner | Default: yes, `period_start` |
| OQ-42 | Timesheets (R74): who enters them (staff, the worker, self-service later), cut-off for approval, rounding of hours, hourly rate per worker grade or per line? | Accountant + project owner | Default: accounting staff enter; held until all submitted entries are decided; hours 2 dp; one rate per line |
| OQ-43 | Cost pools (R75): pool list per entity, allocation keys (fixed %, headcount, revenue: which revenue, which date), whether pool costs include depreciation and payroll, and VAT/transfer-pricing review. | CFO + tax adviser | Default: keys as R75; drivers entered per period; all posted expense accounts listed on the pool |
| OQ-44 | ZATCA advice (R78) is the team's reading of BR-KSA-04 and the stamping rule; the 15th-of-the-following-month deadline is VAT IR Art. 53 **[verify]**. Compliance-checker to confirm both against the downloaded documents. | compliance-checker | Default: as R78 |
| OQ-45 | Approval amount basis for contracts and pool periods (access OQ-35). | Project owner | Default: contract total over the term (12 months if open-ended); pool period: the billed total |

**Status update 2026-10-18 (D14).** Resolved: OQ-5 (dating, Art. 53 to verify), OQ-11 (mirror bill), OQ-37(a). To build: R73 to R81 and the 3.13 to 3.16 models; go-live needs R73 to R78 (D14(c) "at go-live"). New defaults: OQ-39 to OQ-45.

**Status update 2026-10-17 (D13, decision pack).** Resolved: OQ-2 (advance billing via 2310, R70), OQ-30 (a) and tolerance, OQ-31, OQ-33, OQ-34 (document type), OQ-35 (prices). Still blocking: OQ-20 for advance offsets only (386; zatca OQ-33). To build: R70 to R72, R68(d), R54 new codes; 2310 / 2320 in the template. New defaults: OQ-36 to OQ-38.

**Status 2026-10-17 (replaces the line below for the build).**
- **Blocking parts of the build** (refused at approval, R68): OQ-2 (advance timing with non-monthly frequency; 2310 proposed only), OQ-20 (advance offsets; no 386, no 2310/2320). Everything else in billing can be built now.
- **Blocking go-live, not building:** OQ-22 (invoicing implements R52 to R56; ledger/access accept contract approval for clearance posting, R53). Build billing against a fake of the two invoicing functions.
- **No longer blocking:** OQ-16 (ledger-fx built), invoicing OQ-25 (role model built in `core.access`), OQ-19 (R66), OQ-29 (D11).
- **Defaults in use (owner may change):** OQ-3, OQ-4, OQ-5, OQ-7 notice period, OQ-8, OQ-9, OQ-10, OQ-12, OQ-13, OQ-14, OQ-15, OQ-16, OQ-17, OQ-18, OQ-21, OQ-23, OQ-24, OQ-26, OQ-27, OQ-28, OQ-30 to OQ-35.
- **Expert sign-off:** OQ-25 remainder (how far back corrections go, tax), OQ-34 (tax). **Before building:** the compliance-checker verifies every [CC-verify] item (R15, R24, OQ-5, OQ-10, OQ-15).

*Superseded 2026-10-17:* **Blocking:** OQ-2 (multi-month in-advance), OQ-16 (non-base currency), OQ-20 (advance offsets), OQ-22 (invoicing interface, incl. R43), and invoicing OQ-25 (role model) for views. **Expert sign-off:** OQ-25 (tax). **Before building:** the compliance-checker verifies every [CC-verify] item (R15, R24, OQ-5, OQ-10, OQ-15).
