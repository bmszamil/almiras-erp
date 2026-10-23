# Spec: fixed assets (`fixed_assets`)

Status: draft for review · Written: 2026-10-17 · Author: spec-writer agent
*Amended 2026-10-22 (spec-writer): brought in line with the approval engine and per-entity authority matrix (access A25, A26, R57, R58, R69 to R83), the intercompany mirror bill (invoicing D33(c), R113; billing R73), control and restricted accounts (invoicing R97, `ledger/control_accounts.py`, `ledger/restricted.py`), the migration loaders (migration R22 to R25, W11), owner answers of 2026-10-18 to 2026-10-21 (A to J), bai' ajel as built (invoicing R112 to R128), retention (invoicing R108 as amended), CLAUDE.md rule 7. Build slices: 4.0.*
*Amended 2026-10-22 (spec-writer, second pass): compliance report `docs/compliance/fixed-assets-spec-compliance-2026-10-22.md` applied (A1 to A16, B1, B4, S1 to S13, N1 to N9) as team defaults (D19). Verified [CC-verify] tags are replaced by their references; tags remain only where the report could not verify.*
*As built 2026-10-22 (module-builder): slice 1; details settled while building are in §10.*
Django app: `fixed_assets` (new). Reads: `invoicing` (posted vendor bill lines, mirror bills, sales documents), `ledger` (posting, periods, accounts), `billing` (vehicle rental contracts), `payroll` (bai' ajel boundary), `migration` (opening register). Feeds: `ledger` (depreciation and event entries), `core.access` (engine adapters, activities), `migration` (R22 sub-ledger source), `zatca`/`invoicing` (retention class, acquisition date), `consolidation` (intercompany transfers), `docs/specs/tax-returns.md` (Zakat base inputs), `docs/specs/reporting.md` (PPE / investment property notes).
**[CC-verify]** = standards, tax or Sharia statement not yet verified against a primary source. **Needs expert sign-off: <who>** = verified as far as possible by the compliance-checker (search extracts only, 2026-10-22); a licensed tax adviser, the auditor or the Sharia board must confirm it.

## 1. Purpose

Keeps an **asset register per entity** for property, plant and equipment, vehicles (including vehicles rented out on annual contracts), investment property (1510) and capital work in progress. Every entity is covered: Almiras or any subsidiary added as data. Tangible assets only; intangibles on 1600 are out of scope, and so are decommissioning provisions (N5, §5).

Assets are created from posted vendor bill lines, from a murabaha-financed acquisition, or from the migration opening register. A monthly depreciation run posts through `ledger.services`, straight line by default (reducing balance optional), using useful lives held as approved policy data. Assets leave the register by write-off, disposal, or intercompany sale (a normal VAT invoice; the buyer gets an automatic mirror draft bill). Every approval goes through the one approval engine and the entity's authority matrix.

The module also records impairment and investment-property fair values, captures the VAT and Zakat data the tax adviser needs later, supplies retention classes and acquisition dates to the ZATCA archive, and supplies net book values for the Zakat base. Sharia: financing cost or deferred profit is never capitalised (R7). All texts are English and Arabic.

## 2. Decisions

| # | Date | Who | Decision |
|---|---|---|---|
| D1 | in repo (checked 2026-10-22) | `ledger/coa_template.py` (accountant draft) | Accounts 1500 PPE, 1510 Investment property (rental assets), 1520 Vehicles, 1590 Accumulated depreciation, 5400 Depreciation exist in the group template, none with a `control` or `tax_control` flag. Also present: 1250 (bai' ajel assets, D13), 1600 (intangibles, out of scope), 2400 (Islamic financing), 3900 (opening balance equity). None of the proposed codes of §5 is in the template. |
| D2 | in repo | CLAUDE.md | Posting only via `ledger.services.post_entry` / `create_entry`; posted entries never edited, corrections are reversals; closed periods reject postings; entity FK on every record; Sharia enforced per entity; `Decimal` money; en/ar; new entities are data. |
| D3 | 2026-10-16 | Project owner (billing D1) | Cars rented on annual contracts are invoiced automatically from billing contracts. |
| D4 | 2026-10-16 | Project owner (invoicing D11, billing D2, D10) | No VAT group: intercompany sales are normal VAT invoices with the counterparty. ~~No automatic bill in the buyer entity.~~ *Amended 2026-10-22:* the "no automatic bill" part is superseded by D11. |
| D5 | 2026-10-16 | Project owner (billing D5) | Accounting staff create; ~~the CFO approves~~. *Amended 2026-10-22:* "the CFO approves" reads "the approvers named by the entity's authority matrix, through the approval engine" (D10). |
| D6 | 2026-10-17 | Project owner (payroll P12) | Bai' ajel may finance an asset for an employee; margin decided by the Managing Partner per term and value; Sharia compliant; no interest or late fees. *As built (D13):* invoicing R112 to R128. |
| D7 | 2026-10-17 | Project owner (ledger-fx D10, invoicing D29, payroll P6) | Proposed account codes stay **proposed** until the accountant/CFO reviews them. |
| D8 | in repo (2026-10-16) | zatca.md R-38 (from ZATCA documents) | Retention: `capital_asset_real_estate` ≥ 15 years; `movable_capital_asset` = acquisition date + Art. 52 adjustment period + 5 years, never earlier than the standard 6 years. *Amended 2026-10-22 (compliance S1, TIR §8.3.3):* where the accounting useful life is shorter than the adjustment period, the useful life is used (R33). zatca R-38 needs the same correction; that is the zatca spec owner's task, not this one. |
| D9 | caller brief 2026-10-17, not a business decision | Spec scope | Useful lives are policy data approved by the CFO; straight line default, reducing balance optional. *Amended 2026-10-22:* "approved by the CFO" = approved through the engine under activity `fixed_assets.policy` (D10). |
| D10 | 2026-10-18 | Project owner A2, A3, A10, A13 (access A25, A26, R57, R58; R69 to R83 as built) | "All approvable documents go through the same approval engine." Each company has its own authority matrix per activity and amount band; where an entity has no row for an activity, the activity's code default applies (access R70); a gap inside an entity's bands is refused (R71). Self-approval only by the entity's top authority (Managing Partner in Almiras, CEO elsewhere), logged and listed in the monthly governance report (R79, R83, R84). Delegation per access R62, R74. |
| D11 | 2026-10-18 | Project owner A8 (invoicing D33(c), R113; billing R73) | "Intercompany: automatic mirror bill in the buying company." A final intercompany sales document creates a **draft** bill in the buyer, posted only by the buyer's own matrix approval. Not built yet (`create_mirror_bill`, system user `system_intercompany`). |
| D12 | 2026-10-17, 2026-10-21 | CFO, policy A (invoicing D32(a), R97); dry run gap 7 (`ledger/control_accounts.py`) | Sub-ledger control accounts: 1200, 1210, 2100, 2110 (flag `control`); tax control: 1310, 2200, 2220 (flag `tax_control`); one list read from the template. Only invoicing documents (and the migration opening entry) post to the sub-ledger control accounts. The ledger refusal of manual journals on them (R97) is being built. |
| D13 | 2026-10-17, 2026-10-18 | CFO P19(g); owner B8, E8 (payroll R47; invoicing R112 to R128 as built) | Accounts 1240, 1241, 1250, 4610 approved. 1250 holds the cost of an asset bought for sale to an employee until sold; the sale is a simplified invoice at 15% on cost + fixed profit, VAT paid by the employee; real estate refused (R125); the purchase bill is locked (R124); 1240, 1241, 4610 are restricted accounts (R127). |
| D14 | 2026-10-17, 2026-10-18 | CFO/legal (invoicing D32(m)); owner A11, B7 (invoicing R108 amended; access R55 amended) | "Keep everything"; nothing is deleted or anonymised automatically; personal data older than 10 years is nominated; destruction (`burn`) needs an **internal audit committee approval minute**. |
| D15 | 2026-10-16 | Project owner (CLAUDE.md rule 7) | SAR amounts 2 decimals; foreign transaction-currency amounts up to 3 decimals, checked against that currency's own decimals. |
| D16 | 2026-10-17 | CFO, policy B (invoicing D32(a), R98; billing R27) | Car rental income defaults to 4100 (third party) / 4110 (intercompany). |
| D17 | 2026-10-18 | Project owner B2, D (invoicing D33(e), (j); migration M6, R22 to R25 as built) | Opening balances are loaded into the ERP accounts by the migration opening entry, the one entry allowed on control and restricted accounts; sub-ledger detail is loaded from the migration workbook and must tie out before that entry posts. |
| D18 | 2026-10-18 | Project owner A4, A5, G8 | Each company is granted separately (no cascade). The CEO can approve and post and can also act as accountant. Group-level settings (global rates, the Sharia switch under dual control) stay group-level. |
| D19 | 2026-10-22 | Team default under the owner's instruction "go with your recommendations on all open decisions" (owner answers G, H, I, J); owner or the named expert may override | The compliance report of 2026-10-22 is adopted:<br>- R7 rewritten: principal on 2400 is allowed; financing cost and deferred profit are never capitalised. Scope: Sharia-enforcing entities and every IFRS for SMEs entity.<br>- New rules: R22a (real-estate disposals refused until OQ-10), R4a (deferred-payment flag), R46 to R49 (VAT, Zakat and investment-property data, `vat_use_change` events, 1510 for IP only).<br>- Retention formula and retention class corrected (R33, §3.1).<br>- Review reminders added (R16, R19).<br>Each changed rule carries a "team default 2026-10-22" note. |

Decision pack `docs/approvals/owner-cfo-decision-pack-2026-10-17-answered.docx`: checked 2026-10-22, no fixed-asset items (no capitalisation threshold, useful lives, residual values, investment property model, revaluation or transfer prices). Nothing else is decided (§9).

**Framework edition (compliance S12, team default 2026-10-22).** Paragraph references are to IFRS for SMEs (2025, 3rd edition, effective 1 January 2027) and to IFRS as endorsed by SOCPA. The auditor confirms which edition each entity applies in FY2027 and any SOCPA modifications (reporting.md `framework_edition`; OQ-28). The module reads each entity's framework from `Entity.reporting_standard` (`ifrs_sme` default, or full IFRS). Paragraph numbers 17.15B, 27.30 and the s20 lessor paragraph are to be re-checked against the 2025 text. **Needs expert sign-off: auditor.**

## 3. Data model

Conventions as invoicing.md: `entity` FK → `core.Entity` (PROTECT, required) on every model, immutable after creation; functional money `DecimalField(18, 2)`; `created_by` (non-null FK to the user model: ledger registry `creator_field`), `created_at`, `modified_at` (registry `changed_at_field`), `source` (`human` / `import` / `agent:<name>` / `system:<name>`). Documents that go through the engine also carry `submitted_by` / `submitted_at`. "Bi" = `_en` and `_ar`, Arabic required. No model is ever deleted (R42).

### 3.1 `AssetCategory` (per entity, seeded from a group template)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity, code | FK / Char(20) | Y | | Unique `(entity, code)` |
| name | Char(100) | Y | Bi | |
| asset_class | Choice `land`, `building`, `ip_land`, `ip_building`, `vehicle`, `furniture_office`, `it_equipment`, `machinery`, `leasehold_improvement`, `cwip`, `other` | Y | | `ip_*` = investment property. Same list as the migration workbook (`migration/workbook_spec.py` `asset_class`) |
| cost_account / accum_depr_account / depr_expense_account | FK ledger.Account (same entity, postable) | Y (depr. accounts N for non-depreciable) | | Defaults §5. Cost and accumulated accounts must be restricted codes (R9); 1510 only for `ip_*` (R49) |
| accum_impairment_account / impairment_expense_account / disposal_account | FK ledger.Account, null | N | | Empty until the proposed accounts are approved (OQ-5); an event that needs an empty one is refused (R45) |
| depreciable | Bool | Y | | False for `land`, `ip_land`, `cwip` (R12) |
| retention_class | Choice `capital_asset_real_estate`, `movable_capital_asset` | Y | | *Amended 2026-10-22 (compliance S2; team default; Needs expert sign-off: tax):* `land`, `building`, `ip_*`, `leasehold_improvement` → real estate; `cwip` → real estate by default (conservative), re-derived from the target category at `capitalise_cwip` but never shortened (R33); others movable |
| rental_fleet | Bool | Y | | True for vehicle categories let on rental contracts (Art. 50 exception (b), R46; invoicing change slice 2) |

### 3.2 `DepreciationPolicy` (versioned policy data; engine activity `fixed_assets.policy`)

`entity`, `category` (FK), `effective_from`, `method` (`straight_line`, `reducing_balance`), `useful_life_months` (Y for straight line), `annual_rate` (Decimal(7,4), Y for reducing balance), `residual_pct` (Decimal(6,3), default 0), `status` (`draft`, `submitted`, `approved`), `submitted_by`/`at`, `approved_by`/`approved_at` (set from the final engine decision), `basis_note` (Bi). No overlapping approved versions per category.

### 3.3 `FixedAsset`

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Never changes (R30) |
| code | Char(40) | Y | | `{entity.code}-FA-{000001}`, unique per entity |
| category | FK AssetCategory (same entity) | Y | | |
| description | Char(300) | Y | Bi | |
| location | FK `AssetLocation` (same entity) | Y | | 3.7 |
| serial_or_plate | Char(60) | N | | Plate for vehicles (billing `vehicle_ref`, R27) |
| custodian_ref | Char(20) | N | | hr `employee_code` only, no personal data |
| usage | Choice `own_use`, `rental_operating_lease`, `investment_property` | Y | | `investment_property` only with `ip_*` classes |
| held_for | Choice `own_use`, `rental`, `capital_appreciation`, `trade` | Y | | Zakat input (R46; compliance S10) |
| use_evidence_ref | Char(200) | N | | Evidence of use in the activity (Zakat, R46) |
| vat_use | Choice `taxable`, `exempt`, `mixed` | Y | | Art. 52 data (R46, R47) |
| initial_recovery_pct | Decimal(7,4) | Y for bill-sourced | | Snapshot from the bill: VAT recovered ÷ VAT charged on the linked lines (compliance S3 `vat_recovery_pct`, S4); 100 for migrated assets unless the workbook says otherwise (OQ-27) |
| input_vat_amount | Money | Y for bill-sourced | | VAT charged on the linked bill lines (SAR) |
| adjustment_period_end | Date | computed | | `acquisition_date` + Art. 52 adjustment period setting (R33, R46) |
| deferred_payment_flag | Bool | computed | | R4a |
| acquisition_date | Date | Y | | Accounting date of the first cost component (bill), transfer date (R24), financed-acquisition date (R7) or legacy acquisition date (R40) |
| available_for_use_date | Date, null | Y at submission for capitalisation | | Depreciation starts here (R11) |
| cost | Money (functional) | computed | | Σ `AssetCostComponent.amount` |
| residual_value | Money | Y | | Default from policy `residual_pct` × cost |
| method / useful_life_months / annual_rate | as 3.2 | Y | | Copied from policy at activation (legacy values for migrated assets, R40); changes only by R16 |
| opening_date / opening_accumulated_depreciation / opening_remaining_life_months | Date / Money / Int, null | Y if migrated | | Cut-over figures (R40) |
| legacy_source_system / legacy_code | Char(20) / Char(40), blank | Y if migrated | | Unique `(entity, legacy_source_system, legacy_code)` |
| accumulated_depreciation / accumulated_impairment / nbv | Money | computed | | Opening figure + posted runs and events |
| status | Choice `draft`, `submitted`, `active`, `fully_depreciated`, `disposed` | Y | | R8 |
| transferred_from | FK FixedAsset (other entity), null | N | | Intercompany origin (R24, slice 2); shown only to users with rights in both entities |
| retention_until | Date | computed | | R33 |
| submitted_by / submitted_at / approved_by / approved_at | FK User / DateTime | at submission / activation | | Approval from the final engine decision (R36, R37) |

### 3.4 `AssetCostComponent`

`entity`, `asset`, `kind` (`bill_line`, `financed_acquisition`, `cwip_transfer`, `opening_balance`, `subsequent_cost`), `purchase_line` (FK invoicing `PurchaseDocumentLine`, same entity, posted; null for other kinds; unique across components unless split, R4), `amount` (functional, 2 dp, = the functional debit the posted bill booked on the cost account for that line, including any non-recoverable VAT charged to it (invoicing R104), or a split of it; for `financed_acquisition` the cash cost price, R7), `source_event` (FK AssetEvent, null), `deferred_payment` (Bool, R4a). Foreign-currency bills: functional amount as booked at the bill's rate; never recomputed and never revalued (non-monetary, ledger-fx R21; R39).

### 3.5 `DepreciationRun` (document; registered in `ledger/documents.py`, R38)

`entity`, `period` (FK FiscalPeriod), `number` (`{code}-DEP-{yyyy}-{000001}`), `status` (`draft`, `submitted`, `posted`, `discarded`, `reversed`), `created_by`/`at` (preparer), `submitted_by`/`at`, `posted_by`/`at`, `journal_entry` (FK JournalEntry, PROTECT, null), `fingerprint` (hash of assets, policies, events in scope; the engine `version`), `total` (Money). **`DepreciationLine`**: `run`, `asset`, `days`, `amount`, `catch_up` (Bool), `opening_nbv`.

### 3.6 `AssetEvent` (document; registered in `ledger/documents.py`, R38)

`entity`, `asset`, `kind` (`financed_acquisition`, `capitalise_cwip`, `subsequent_cost`, `change_of_estimate`, `impairment`, `impairment_reversal`, `write_off`, `vat_use_change`, `disposal_sale`, `intercompany_sale`, `reclassification`, `revaluation`, `fair_value_change`), `event_date`, `amounts` (fields per kind: proceeds, recoverable_amount, new life, cash cost price and financing total for `financed_acquisition`, old and new `vat_use` for `vat_use_change`, etc.), `financing_ref` (murabaha / tawarruq contract reference, Y for `financed_acquisition`), `sales_document` (FK invoicing SalesDocument, null; Y for `disposal_sale`, `intercompany_sale`), `evidence_ref`, `reason` (Bi), `status` (`draft`, `submitted`, `posted`, `approved` (kinds that post nothing), `discarded`, `reversed`), `created_by`, `submitted_by`, `posted_by`, `journal_entry`. `revaluation` and `fair_value_change` exist as choices but are refused (R20, R21); `disposal_sale`, `intercompany_sale` and `reclassification` are slice 2.

### 3.7 `AssetLocation`

`entity`, `code`, `name` (Bi), `property_ref` (billing `property_ref` / `unit_ref` text), `city` (Bi).

### 3.8 `IpFairValue` (added 2026-10-22, compliance S8; R48)

`entity`, `asset` (FK FixedAsset with an `ip_*` class, same entity), `valuation_date`, `amount` (Money), `valuer_name` (Bi), `valuer_licence_ref` (Char(60), N; Taqeem accreditation, OQ-6), `evidence_ref`, `undue_cost_justification` (Bi text, N; s16 cost-model justification per property), `created_by`, `created_at`. Append-only; posts nothing.

### 3.9 `FixedAssetSettings` (per entity; added 2026-10-22)

`entity` (one row), `art52_adjustment_months_movable` (Int, default 72; Needs expert sign-off: tax, OQ-27), `art52_adjustment_months_real_estate` (Int, default 120; same), `normal_credit_days` (Int, null: set by the CFO; R4a), `reporting_dates` (list of period ends the entity designates as reporting dates, besides the fiscal year end; R16, R19). Changes are logged.

## 4. Rules

Tags: **[S1]** slice 1, **[S2]** slice 2. Enforcement in bold brackets.

### 4.0 Build slices

**Slice 1** (buildable now; no BLOCKING question):

- categories, locations, settings and policy data (R14, R31, 3.9);
- register and capitalisation from posted bill lines (R1 to R8, R44);
- murabaha-financed acquisition at the cash cost price (R7);
- deferred-payment flag (R4a);
- restricted asset accounts and the register-to-GL report (R9);
- monthly depreciation run with posting, catch-up, change of estimate, year-end review reminder and reversal (R10 to R18);
- impairment with the reporting-date indicator reminder (R19);
- refusal of fair value and revaluation (R20, R21);
- write-off (R23, R25, R26 for write-offs);
- rental-vehicle guard on write-off (R27);
- bai' ajel boundary (R6, R28);
- entity separation, setup and bilingual texts (R29 to R32);
- retention date on the register (R33 first part, R42);
- Zakat inputs report with `held_for` (R34, R46);
- agents (R35);
- engine activities, self-approval and delegation (R36, R37);
- ledger document registration (R38);
- currency decimals (R39);
- migration opening register and its R22 tie-out (R40, R41);
- period-close check (R15, R43);
- missing accounts (R45);
- VAT, Art. 52 and Zakat data capture (R46);
- `vat_use_change` events without a GL entry (R47);
- investment-property fair-value records (R48);
- 1510 used for IP only (R49).

Policy values (OQ-2) are data entered by the CFO and approved through the engine; tests use fixture values. Proposed accounts (OQ-5) are fixture data; in a real entity the impairment, write-off and CWIP postings wait for their approval (R45).

**Slice 2** (blocked by OQ-14, and by the build of invoicing R113 / billing R73 for intercompany):

- sale to a third party (R22) and the real-estate refusal (R22a);
- intercompany sale and the buyer's asset from the mirror bill (R24);
- disposal-month depreciation and gain or loss on sales (R25, R26 for sales);
- retention hand-off to sales documents (R33 second part);
- the year-end Art. 52 report (R47 second part);
- the invoicing `blocked` VAT attribution for vehicles (R46(c); invoicing change);
- reclassification between categories (OQ-16).

### Register and acquisition
1. **[S1]** Every asset belongs to one entity; its category, location, cost components and accounts belong to the same entity. **[M clean + svc; AT-18]**
2. **[S1]** *(Amended 2026-10-22: pull from invoicing, engine approval.)* `capitalisable_lines(entity, *, user)` lists the posted, unreversed `PurchaseDocumentLine`s of that entity (bills, vendor credit notes, and mirror bills once R113 exists) whose `account` is the `cost_account` of one of its categories and which are not yet fully linked. It reads through a read-only invoicing port (`invoicing.fixed_assets_port`; no change to invoicing behaviour). `create_asset_from_line(line, *, user, ...)` (`fixed_assets.create_asset`) drafts one asset per line, or links the line to an existing draft asset of the same entity (to an active asset only through a `subsequent_cost` event). Lines on CWIP (1550, proposed) create or extend a `cwip` asset. The preparer submits the draft with its `available_for_use_date` (`fixed_assets.submit_asset` → `engine.submit`). The asset becomes `active` only on the final engine decision of activity `fixed_assets.capitalisation` (R36, R37). Activation copies method and life from the approved policy and posts nothing: the cost is already in the GL from the bill (FA1). Vehicle input VAT: R46(c). **[svc + invoicing port + engine; AT-1]**
3. **[S1]** Capitalisation threshold: none (every line on a cost account is registered) until the CFO sets one (default, OQ-1). Below-threshold items are a staff choice of an expense account at bill entry, not a register rule. **[svc; AT-1]**
4. **[S1]** A bill line feeds at most its own posted functional amount across components; reversing or crediting the bill (vendor credit note) reduces the component and needs an `AssetEvent` before the next run. **[svc + DB unique; AT-1]**
4a. **[S1]** *(Added 2026-10-22, compliance S13; team default; Needs expert sign-off: auditor and Sharia board.)* **Deferred payment flag.** A bill line whose payment terms are longer than the entity's `normal_credit_days` (due date − bill accounting date) marks its component and asset `deferred_payment`. The builder does not split the amount: the full bill amount stays the cost. Background: s17.13 and IAS 16.23 measure cost at the cash price equivalent; in a Sharia entity an imputed amount must never post to an interest account. The classification of any imputed financing component (cost or 5600 deferred-sale profit) is OQ-25. The flagged assets are listed for the accountant. While `normal_credit_days` is empty, nothing is flagged and the register report shows "normal credit terms not set / لم تُحدَّد مدة الائتمان المعتادة". Deferred-payment disposals get the same flag on the sales document (slice 2, OQ-25). **[svc; AT-39]**
5. **[S1]** `acquisition_date` = accounting date of the earliest cost component (bill), the transfer date (R24), the financed-acquisition date (R7) or the legacy acquisition date (R40); never later than `available_for_use_date`. It is fixed at activation. **[M clean; AT-10]**
6. **[S1]** *(Amended 2026-10-22: 1250 is in the template, bai' ajel built.)* Bill lines on 1250 (assets acquired for an employee's bai' ajel, approved, D13) are never listed by R2, registered or depreciated: they are held for sale to the employee (payroll R47; invoicing R118 to R124). `create_asset_from_line` refuses a 1250 line (`bai_ajel_asset`, en/ar), and 1250 can never be a category account. A register asset cannot be sold on bai' ajel: invoicing R120 only sells against a posted bill's 1250 debits (`cost_mismatch`), so an asset on 1500 to 1570 never qualifies (OQ-12 unchanged). **[svc + invoicing R120; AT-14]**
7. **[S1]** *(Rewritten 2026-10-22, compliance B1, S11, A1, A2; team default D19. Needs expert sign-off: owner, auditor and Sharia board.)* **Sharia: no financing cost is capitalised.**
    - **Scope.** Every entity with `sharia_enforced=True`, and every entity whose `reporting_standard` is IFRS for SMEs (s25 expenses all borrowing costs anyway). A full-IFRS entity with `sharia_enforced=False`: OQ-9, OQ-24 (a group-wide rule needs the owner's decision). In such an entity the refusal below is not applied, but nothing capitalises borrowing costs either: that is not built (`not_built_oq9`, en/ar).
    - **What is refused.** A cost component, or an event debiting an asset cost account, whose source line is on a financing-cost account (5600, 5610, any `prohibited` account), or which carries the deferred-profit portion of an Islamic financing (murabaha / tawarruq / ijara deferred profit), is refused (Sharia message, en/ar).
    - **What is allowed.** An asset bought on murabaha is booked by a `financed_acquisition` event: Dr the category cost account / Cr 2400 for the **cash cost price** only, with `financing_ref` and the financing total recorded. An amount above the cash cost price is refused. The deferred profit is booked against 2400's contra and expensed to 5600 over the term by the financing records, outside this module. The event is approved under `fixed_assets.capitalisation` and posted with `fixed_assets.capitalise`.
    - **Other rules.** Conventional loans (2410) are already blocked where Sharia is enforced (ledger).
    - **Standards.** Under full IFRS, IAS 23 requires capitalising borrowing costs on qualifying assets (IAS 23.8). IFRS for SMEs expenses them (IFRS for SMEs 25.2, unchanged in the 3rd edition). The conflict for Islamic financing cost in full-IFRS entities, and how murabaha deferred profit is split between cost and expense, are OQ-9 (Sharia board + auditor).

    **[svc; AT-17]**
8. **[S1]** *(Amended 2026-10-22.)* Status: `draft → submitted → active → fully_depreciated → disposed` (or `active → disposed`); a returned or rejected submission goes back to `draft`. An `active` asset's category, cost, method and life change only through posted or approved events (R16). Disposed assets are kept, read-only. **[svc + M guard; AT-22]**
9. **[S1]** *(Amended 2026-10-22: answers the flag part of OQ-5.)* **Restricted asset accounts, no control flag.**
    - Asset accounts get **no** `control` or `tax_control` flag. Those flags mean an invoicing open-item sub-ledger or a tax account (`ledger/control_accounts.py`); with them, invoicing R39, migration W8 and R22, and the R97 refusal would treat 15xx as invoicing accounts.
    - Instead, in its `AppConfig.ready()`, `fixed_assets` registers the category cost and accumulated accounts as **restricted accounts** (`ledger.restricted.restrict`): 1500, 1510, 1520, 1590, and each proposed 15xx code once approved (§5).
    - Allowances (`restricted.allow`):
      - (a) `fixed_assets.depreciationrun` and `fixed_assets.assetevent` entries, on all of them;
      - (b) `invoicing.purchasedocument` entries (bills, vendor credit notes, mirror bills), on the cost accounts only, never on accumulated accounts;
      - (c) the migration opening entry (the existing allowance for every restricted account, migration R16 amendment).
    - Reversals follow their original.
    - Refused (`restricted_account`, en/ar): manual journals (drafts included), replayed legacy journals, bank transactions and payroll entries on these accounts.
    - The accountant is to confirm this default (OQ-5).
    - Expense and income accounts (5400, 5420, 4800) are not restricted.
    - A register-to-GL reconciliation report (per entity, account, date) must show zero difference.

    **[ledger.restricted + report; AT-23]**

### Depreciation
10. **[S1]** Methods: `straight_line` (default) = (cost − residual − accumulated impairment allocation) spread evenly over the remaining life in months; `reducing_balance` = opening NBV × `annual_rate` / 12, never below residual. **[svc; AT-2, AT-3]**
11. **[S1]** Depreciation starts on `available_for_use_date` and stops on disposal date or when NBV = residual. First and last months are prorated by days (actual days / days in month) (default, OQ-3). **[svc; AT-2]**
12. **[S1]** `land`, `ip_land` and `cwip` are never depreciated. **[svc; AT-20]**
13. **[S1]** Per asset per month the amount is rounded half up to 2 dp; the last month absorbs the residue so total depreciation = cost − residual exactly. **[svc; AT-2]**
14. **[S1]** *(Amended 2026-10-22.)* An asset whose category has no **approved** policy in force cannot be submitted for capitalisation; a run refuses to include it (`no_policy`, en/ar, naming the category). Useful lives and rates are not defaulted in code (OQ-2). Policy versions are drafted with `fixed_assets.maintain_policy`, submitted with `engine.submit`, and approved by the final engine decision of activity `fixed_assets.policy`. **[svc + engine; AT-8]**
15. **[S1]** *(Amended 2026-10-22: engine.)* **Monthly run.**
    - **Prepare.** `prepare_depreciation(entity, period, *, user)` (`fixed_assets.prepare_depreciation`) builds a draft run with one line per active depreciable asset.
    - **Submit.** The preparer submits it (`submit_depreciation` → `engine.submit`, submit permission `fixed_assets.prepare_depreciation`; version = fingerprint, amount = run total).
    - **Decide and post.** The final decision of activity `fixed_assets.depreciation_run` (`engine.decide`, permission `fixed_assets.run_depreciation`) calls the adapter's approve service `post_depreciation(run, user)`. That service:
      - recomputes the run and refuses if the fingerprint changed (`run_changed`, "discard and re-prepare", en/ar);
      - calls `engine.check_issued_band(grant, run, amount=total, currency=SAR)`;
      - posts through `ledger.services.create_entry(..., post=True, grant=..., document=run)` with the run's registered `build_lines` (§5 FA2).

      Called outside the engine it is refused (`approval_request_required`, access R61).
    - **Limits.** One posted run per entity and period; a second is refused. Runs post only in open periods.
    - **Discard.** `discard_depreciation` drops a draft or submitted run and withdraws its request (`engine.withdraw`).
    - **Period close.** The registered `pending_documents` makes `close_period` refuse a period with a draft or submitted run, or with active depreciable assets and no posted run (default, OQ-4).

    **[svc + engine + ledger registry; AT-4, AT-5, AT-6, AT-7]**
16. **[S1]** *(Reworded 2026-10-22, compliance A3, S6; team default.)* **Changes of estimate** (life, residual, method) are prospective (IAS 8.36; IFRS for SMEs 10.16, 17.23).
    - **House convention:** effective from the first day of the earliest open month on or after the approval date.
    - **How:** a `change_of_estimate` event, approved by the final engine decision of activity `fixed_assets.estimate_change`; it posts nothing.
    - **Policy versions:** a new approved policy version applies only to assets activated after its `effective_from`, unless an event re-applies it.
    - **Review reminder:**
      - full-IFRS entities: residual value, useful life and method are reviewed at least at each financial year end (IAS 16.51), so the system raises a bilingual reminder per entity at each fiscal year end;
      - IFRS for SMEs entities: reviewed when indicators exist (s17.19), so the same reminder is raised, worded as an indicator check.

      The paragraph numbers come from the compliance-checker's memory; they are to be re-checked against the primary text (S12).

    **[svc + engine + job; AT-21, AT-48]**
17. **[S1]** Assets activated late (available-for-use month already closed) get a `catch_up` line in the next open run; nothing posts to a closed period. **[svc; AT-5]**
18. **[S1]** *(Amended 2026-10-22.)* Reversing a posted run or event uses `ledger.services.reverse_entry` with a grant for `fixed_assets.reverse_document`, the only reversal permission registered for these types (a `ledger.reverse_entry` grant is refused, `document_reversal_only`). The decision is an engine request of activity `fixed_assets.reversal`. The registered `reverse_guard` refuses a reversal that does not come from `fixed_assets.services.reverse_document`. The run is marked `reversed`, and the period may be re-run while it is open. **[svc + engine + registry; AT-4]**

### Impairment, investment property, revaluation
19. **[S1]** **Impairment** (IAS 36; IFRS for SMEs s27): an `impairment` event records recoverable amount and evidence, and posts Dr impairment expense / Cr accumulated impairment for carrying amount − recoverable amount.
    - Later depreciation uses the new carrying amount over the remaining life.
    - A reversal is allowed when IAS 36.114 permits it, and is capped at the carrying amount there would have been without the impairment (IAS 36.117; s27.30, paragraph number to confirm against the 2025 edition).
    - *(Amended 2026-10-22: was "Approver posts, not the creator".)* It posts on the final engine decision of activity `fixed_assets.impairment` (R36, R37).
    - *(Amended 2026-10-22, compliance A4, S7; team default.)* Indicator assessment at each reporting date (IAS 36.9; s27.7). The system raises a reminder at each fiscal year end and at each period end in `FixedAssetSettings.reporting_dates` (OQ-7).

    **[svc + engine + job; AT-12, AT-48]**
20. **[S1]** **Investment property (1510).** *(Reworded 2026-10-22, compliance A5, A6, S8; team default.)*
    - **IFRS for SMEs s16** requires fair value through profit or loss when fair value can be measured reliably without undue cost or effort, otherwise the cost model (IFRS Foundation Module 16, 16.7–16.8). s16 is **not** a policy choice: an entity using the cost model must be able to show undue cost or effort, property by property, and disclose that fact and its reasons.
    - **Full IFRS:** IAS 40 is a policy choice (IAS 40.30). Under the cost model, IAS 40.79(e) still requires the fair value to be disclosed.
    - **Until OQ-6 is decided,** every entity uses the cost model: IP buildings are depreciated, IP land is not, and `fair_value_change` events are refused (en/ar). The fair value and the per-property justification are recorded as data (R48).
    - Land and building are separate assets.
    - The cost-model default may be a departure for IFRS for SMEs entities that hold IP with an available market value: **Needs expert sign-off: auditor** (OQ-6).

    **[svc; AT-13, AT-46]**
21. **[S1]** **Revaluation** of PPE *(reworded 2026-10-22, compliance A7)*: IFRS for SMEs s17.15–17.15B (since the 2015 amendments, effective 1 January 2017) and IAS 16.29 both allow the revaluation model per class of PPE; paragraph numbers to check against the 2025 edition. Not built: `revaluation` events are refused in every entity until OQ-8. **Needs expert sign-off: auditor.** **[svc; AT-13]**

### Disposal and transfers
22. **[S2]** **Sale to a third party.**
    - Staff draft an invoicing sales document (tax invoice, ZATCA clearance per invoicing/zatca) with a line of income type "fixed asset disposal" on 4800 (proposed) and the `fixed_asset` reference, plus a `disposal_sale` event linked to it.
    - The event posts when the sales document posts (after ZATCA clearance or reporting, invoicing R41), dated the sales document's posting date. It derecognises cost, accumulated depreciation and impairment against 4800.
    - VAT category per invoicing *(reworded 2026-10-22, compliance A8, A9, N2)*:
      - used movable asset: standard rate 15% on the full price. The profit-margin method is not available, since Almiras entities are not licensed car traders with ZATCA approval; the sales line offers no margin-scheme option.
      - real estate (land, buildings, IP): VAT exempt (Royal Order A/84). RETT at 5% of the value is due under the RETT Law (2024) and its Implementing Regulations (in force 10 April 2025). RETT is not handled by this module; see R22a. **Needs expert sign-off: tax** (OQ-10).

    **[svc + invoicing; AT-40]**

    *Amended 2026-10-22:* the sales document is approved under the seller's activity `invoicing.sales_document`, and the `disposal_sale` event under `fixed_assets.disposal` (amount = NBV derecognised). The event's engine decision gives the grant; the posting itself waits for the sales document's entry. A customer linked to an employee makes that employee a beneficiary (access R82; invoicing adapter `_employee_users`). Blocked by OQ-14.
22a. **[S2]** *(Added 2026-10-22, compliance B4, S9; team default; Needs expert sign-off: tax.)* **Real estate: no disposal until OQ-10.**
    - A `disposal_sale` or `intercompany_sale` event for an asset whose `retention_class` is `capital_asset_real_estate` is refused (`real_estate_disposal_blocked`, en/ar, citing OQ-10) until OQ-10 is signed off and the entity's setting enables it.
    - Once enabled:
      - the linked sales document must use VAT category E (real-estate exemption) with the matching exemption reason; the code is to be confirmed with zatca.md and the ZATCA XML rules first (N9);
      - the event cannot post until a RETT record is attached (amount = 5% of the value, or a documented exemption article such as the intra-group exemption of S9 / OQ-11(b)) together with its payment evidence;
      - RETT borne by the seller posts to a proposed RETT expense account within the disposal result (code to be proposed with OQ-5).

    **[svc; AT-42, AT-43]**
23. **[S1]** *(Amended 2026-10-22.)* **Write-off / scrap** without proceeds: a `write_off` event, Dr disposal account (4800) / Cr cost, Dr accumulated accounts. It posts on the final engine decision of activity `fixed_assets.disposal` (amount = NBV written off). **[svc + engine; AT-9]**
24. **[S2]** *(Amended 2026-10-22: D11 replaces "no automatic bill".)* **Intercompany sale.** Assets never move between entities.
    - **Seller.** It issues a normal VAT invoice to the buyer entity's customer record (counterparty set, AR on 1210, D4), with an `intercompany_sale` event (as R22). Real estate: R22a.
    - **Mirror bill.** When the invoice is `final`, invoicing R113 / billing R73 create a **draft mirror bill** in the buyer: created by the system user `system_intercompany`; vendor = the buyer's group vendor for the seller; amounts and VAT copied and read-only; linked by `MirrorBillLink`.
    - **Buyer.** The buyer's staff set the line's account to the buyer's category cost account (or `IntercompanyAccountMap` gives it), then submit. The buyer's matrix approves (`invoicing.bill`), and posting books Dr the buyer's cost account / Dr 1310 / Cr 2110 with the seller as counterparty.
    - **Buyer's asset.** R2 then lists the posted mirror line. The draft asset gets:
      - `transferred_from` = the seller's asset (found through `MirrorBillLink` → seller document → its `intercompany_sale` event);
      - `acquisition_date` = the buyer bill's accounting date;
      - cost = the copied line amount;
      - life from the buyer's policy (OQ-11).
    - **Price.** Approved on the seller's side only: its sales document (`invoicing.sales_document`) and the event (`fixed_assets.disposal`), each under the seller's matrix. The buyer cannot change it; it can only reject the mirror bill with a reason (billing R73).
    - Nothing posts in the buyer without the buyer's own approval (CLAUDE.md rule 4).
    - `consolidation` reads the pair for unrealised profit (consolidation R21).

    **[svc + invoicing R113; AT-11, AT-43]**
25. **[S1 write-off / S2 sale]** A disposal, write-off or intercompany sale event dated in a month already depreciated posts no extra depreciation; one dated in an open month includes depreciation up to the disposal date in that month's run (default, OQ-3). **[svc; AT-9]**
26. **[S1 write-off / S2 sale]** Gain/loss = proceeds (net of VAT) − NBV, shown net on 4800 (may carry a debit balance). Separate gain and loss accounts: OQ-5. **[svc; AT-9, AT-40]**

### Vehicles, bai' ajel, retention, Zakat
27. **[S1]** **Rental vehicles.** Vehicles let on annual contracts (billing `vehicle_rental`) stay in the lessor's register as `usage=rental_operating_lease` and are depreciated (lessor operating lease, s20 / IFRS 16 **[CC-verify]**: likely IFRS 16.88 and s20.29 (2015 numbering), not verified 2026-10-22; verify the s20 paragraph in the 2025 edition). Hand-off to billing: `vehicle_ref` should hold the asset `code` of an active vehicle of the same entity (billing amendment, OQ-13; free text until then). Once linked, a disposal or intercompany sale of a vehicle with an `active` billing contract on it is refused (en/ar).
    *Amended 2026-10-22:* the guard also covers `write_off` (slice 1). Until OQ-13, billing is queried read-only for active `vehicle_rental` contracts of the same entity whose `vehicle_ref` equals the asset's `code` or `serial_or_plate` (`vehicle_on_contract`, en/ar, naming the contract). Rental income stays billing's and invoicing's: 4100 for a third party, 4110 for a group entity (D16). An intercompany lease does not move the asset (it stays in the lessor's register). **[svc + billing query; AT-15]**
28. **[S1]** **Bai' ajel boundary** (payroll R47 to R49; invoicing R112 to R128 as built): purchase on 1250, sale on 1240/1241/4610 and profit recognition are payroll's and invoicing's; 1240, 1241 and 4610 are restricted accounts owned by invoicing (R127). This module only refuses registration (R6) and never uses those accounts. **[svc; AT-14]**
29. **[S1]** **Entity separation.** All views and APIs are filtered by entity and permission (`access.scope`); memberships are per company, with no cascade (D18; access R64). A user sees an asset's `transferred_from` only with `fixed_assets.view` in both entities; otherwise "not visible" (en/ar). **[svc + access; AT-18]**
30. **[S1]** `entity` on assets, components, runs and events never changes. **[M guard; AT-22]**
31. **[S1]** Each new subsidiary gets categories, settings and accounts from the group template as data (setup command); no code path names an entity. **[setup + code-reviewer; AT-25]**
32. **[S1]** Every label, message, event reason, reminder and journal description (en, Arabic in reason until ledger OQ-15) exists in English and Arabic (key messages: §7 table). **[svc; AT-19]**
33. **[S1 register / S2 hand-off]** **Retention** (zatca R-38, D8). Each asset has `retention_until` = the later of:
    - (a) R-38 computed with its category's `retention_class` and `acquisition_date`. *Amended 2026-10-22 (compliance S1; TIR §8.3.3; team default):*
      - real estate: end of the tax period of acquisition + 15 years ("not less than 15 years in all cases"; the start point is an assumption, N7);
      - movable: `acquisition_date` + min(`art52_adjustment_months_movable`, accounting useful life in months) + 5 years, never earlier than the standard date. The setting defaults to 6 years (secondary sources; **Needs expert sign-off: tax**, zatca OQ-31, OQ-27).
    - (b) the `retention_until` of its bills and disposal documents.

    A `cwip` asset is real estate until `capitalise_cwip` re-derives the class from the target category, and the date is never shortened (§3.1). `retention_until` is never shortened, and there is no purge job.

    **Hand-off (slice 2).** A disposal or intercompany sales document linked to an asset gets `retention_class` from the category (real estate even though the line is on 4800, not 1510), and, for movable assets, the explicit `movable_capital_asset` flag with `acquisition_date` (invoicing OQ-46, zatca OQ-31). This needs an invoicing change: the class is taken from the linked asset, not only from account 1510 (OQ-14). **[svc + invoicing; AT-10, AT-41]**

    *Amended 2026-10-22 (D14):* `retention_until` is information for the archive and for retention nominations; it never triggers deletion (R42). Records are kept indefinitely, so the formula only changes the nomination date.
34. **[S1]** **Zakat inputs.** `zakat_inputs(entity, as_of, *, user)` returns, per category and `asset_class`:
    - cost, accumulated depreciation, impairment and NBV;
    - CWIP separately;
    - investment property separately;
    - assets disposed in the year, with proceeds;
    - *(amended 2026-10-22, compliance A13, S10; team default)* per asset, `held_for` (own use, rental, capital appreciation, trade) and `use_evidence_ref`.

    Book NBV is the basis (Zakat Implementing Regulations 1445H / 2024, Ministerial Resolution 1007: non-current assets deducted only if acquired for use, not for resale, at net value in the financial statements; article numbers not read). The module never decides zakat treatment (deductibility of fixed assets and IP, land held for trade); `tax-returns.md` owns that. **Needs expert sign-off: tax.**

    Tax depreciation (Income Tax Law Art. 17: pooled declining balance by group; status of the 2023 draft law to be checked) is not built (OQ-15). If it is built, it works per **group pool**, not per asset. **Needs expert sign-off: tax.** **[svc; AT-16]**
35. **[S1]** Agents may draft assets from posted bills and draft runs (draft only, logged, gateway); they never submit for approval, decide, activate, post or reverse (catalogue: **Ag** only on `fixed_assets.view`, `create_asset`, `prepare_depreciation`; access R12). **[access; AT-24]**

### Added 2026-10-22
36. **[S1]** **One approval engine (D10).**
    - Every fixed-asset approval is an engine request (`core.access.engine`: `submit`, `decide`, `withdraw`).
    - Adapters are registered in `fixed_assets/approval_adapters.py` from `FixedAssetsConfig.ready()`, following `invoicing/approval_adapters.py`: activity, `permission`, `submit_permission`, `amount` (SAR), `currency` (the entity's functional currency), `version`, `label` (en, ar: number only, no personal data), `approve`, `return_`/`reject`.
    - One model approved under several activities (AssetEvent) registers one adapter per activity, selected by `permission`, as invoicing does for `PurificationTransfer`.
    - The activities and their code defaults go into `core/access/authority_defaults.py`, and the `ApprovalActivity` rows into a seed migration (§7).
    - An entity with no matrix row uses the code default (access R70). A gap inside its bands refuses submission (`no_authority_rule`, R71).
    - A document whose amount at posting leaves the routed band is refused by `check_issued_band` (`amount_outside_band`) and goes back to its preparer. A changed document (new `version`) is approved again.
    - Module services never approve outside the engine (access R61).

    **[engine + adapters + seed; AT-27, AT-28, AT-30, AT-38]**
37. **[S1]** **Self-approval and delegation (D10).**
    - The creator, submitter, last modifier, or the principal of a personal-assistant draft (access R81), may decide a fixed-asset request only when all of these hold: they are the entity's top authority (Managing Partner in Almiras, CEO elsewhere); `self_approval_enabled` is on; they are eligible under the matrix or the code default; and they are not a beneficiary.
    - Each such decision is logged (`self_approval`) and listed in the monthly governance report (access R79, R83, R84). Everyone else is refused `self_approval` (en/ar).
    - One person decides at most one step (R75).
    - Delegates are eligible only where the row allows delegation (R62, R74).
    - No fixed-asset permission is NSA (default, OQ-21).
    - This replaces every "approver, not the creator / preparer" clause of the 2026-10-17 draft.

    **[engine; AT-4, AT-27, AT-29]**
38. **[S1]** **Ledger document types.** In `FixedAssetsConfig.ready()`, `ledger.documents.register` is called for:
    - `DepreciationRun`: `post_permissions={"fixed_assets.run_depreciation"}`, `reverse_permissions={"fixed_assets.reverse_document"}`, `build_lines` (FA2), `validate_lines`, `pending_documents` (R15, R43), `reverse_guard` (R18).
    - `AssetEvent`: `post_permissions={"fixed_assets.capitalise", "fixed_assets.impair", "fixed_assets.dispose"}`, the same reversal permission, `build_lines` per kind (FA4 to FA6, FA8), `validate_lines`, `pending_documents` (submitted events dated in the period), `reverse_guard`.

    **Grant check.** The service checks that the grant's permission matches the event kind:
    - `financed_acquisition`, `capitalise_cwip` → `fixed_assets.capitalise`;
    - `impairment`, `impairment_reversal` → `fixed_assets.impair`;
    - `write_off`, `disposal_sale`, `intercompany_sale` → `fixed_assets.dispose`.

    **`validate_lines`** refuses (en/ar):
    - any line on an account of another entity;
    - any line on a sub-ledger control or tax-control account (`ledger.control_accounts`);
    - any line on a Sharia-prohibited account;
    - any line on an account the categories of the document's assets do not name, except 2400 for `financed_acquisition`;
    - any total that differs from the document's own lines.

    **Not used:** `creator_may_post`, `contract_approval`, `sama_tax_point` (the registry allows them for other types only).

    **Catalogue.** New permissions with **P** are added to `core/access/catalogue.py`, and `fixed_assets.reverse_document` to `REVERSE_PERMISSIONS`.

    **[registry + catalogue; AT-23, AT-38]**
39. **[S1]** **Currency and decimals (D15).** Every register amount is functional (SAR), 2 dp, half up. A component from a foreign-currency bill line takes the SAR debit the bill booked (the bill's 2-dp or 3-dp transaction amount is never re-converted here). An amount typed into the module (residual value, recoverable amount, migrated cost, cash cost price, fair value) with more than 2 dp is refused (`amount_decimals`, en/ar). Engine band measures are in SAR (access R73). **[svc + M; AT-26, AT-31]**
40. **[S1]** **Opening register from the migration workbook (D17; answers OQ-19).** The `fixed_assets` section of `migration.subledgers.load_subledgers` (command `migration_subledgers --section fixed_assets`) loads the workbook's Fixed assets sheet (W1, W11) through `fixed_assets.migration_port.load_opening_asset` (right `migration.import`, a person; agents and system users refused `agent_forbidden`). Rules:
    - The entity's workbook rows must check without an error first (W4, W11, W17), and the sheet must tie to the TB per Falcon code (W11).
    - The category is resolved by (entity, `asset_class`, the ERP cost account the approved map gives `cost_code`); a row without a matching category is refused (en/ar). Categories are set up before the load.
    - The location is found or created by name in the entity; `custodian_employee_no` → `custodian_ref`.
    - Per row: one active asset with `source="import"`, `legacy_source_system`, `legacy_code` = `asset_code`, `acquisition_date`, `available_for_use_date`, one `opening_balance` component = `cost`, `opening_accumulated_depreciation` = `accumulated_depreciation`, `residual_value`, and `method`, `useful_life_months` and `opening_remaining_life_months` from the row (legacy estimates kept, prospective; OQ-20). `held_for` defaults from `usage` (`own_use` → own use, `rental_operating_lease` → rental, `investment_property` → rental); `vat_use` and `initial_recovery_pct` are left for the accountant to complete (register report lists missing values; OQ-27). `opening_date` = the entity's cut-over date. No journal entry and no approval request: the balances are posted by the migration opening entry, approved through the engine (`ledger.manual_entry`, migration R16).
    - A category-level row (no per-asset detail) loads the same way as one asset (OQ-19 default).
    - Non-depreciable classes load with accumulated depreciation 0 (W11 warns otherwise).
    - Dry run unless `apply`; all or nothing. A re-run with the same values is skipped; other values are refused (`opening_asset_changed`, en/ar).
    - Refused once the entity's opening entry is posted (`opening_posted`, en/ar; parity with migration R25: the port first locks the opening entry rows, as `invoicing.migration_port.load_open_item` does).
    - A run that includes migrated assets is refused while the opening entry is not posted (`opening_not_posted`, en/ar).
    - First depreciation: the month after the cut-over, (cost − opening accumulated − residual) over `opening_remaining_life_months` (straight line) or the reducing-balance rate on the opening NBV.

    **[svc + migration; AT-32]**
41. **[S1]** **Opening tie-out gate (migration R22).**
    - **Source.** `fixed_assets.migration_port.opening_register_balances(entity, source_system)` is registered in `MigrationConfig.ready` with `register_open_items_source(fn, codes={"1500", "1510", "1520", "1590"} (+ approved proposed 15xx codes), per_source_system=True)`. It returns {(code, ""): signed SAR, debit positive}: Σ cost of the loaded assets per category cost account, and −Σ opening accumulated depreciation per accumulated account.
    - **Gate.** The opening entry posts only when its line on each of these accounts equals the register to the halala; otherwise the posting is refused with the differences listed (en/ar; migration R22). An entity whose opening entry carries 15xx balances therefore needs its register loaded first.
    - **Dry run.** This changes the migration dry-run fixture (`migration/fixtures/dry_run.py`: DRYH 1500 36,000.00, 1520 240,000.00, 1590 −101,500.00). The builder adds the fixed-asset load there.

    **[migration gate + port; AT-32, AT-33]**
42. **[S1]** **Keep everything (D14).** Assets, components, runs, lines, events, policies, fair values and locations are never deleted, by any user, job or agent (model guard and PostgreSQL trigger, like `invoicing/0034`). There is no purge job. The register holds no personal data except `custodian_ref` (an hr employee code) and the valuer's name. Nominations and any `burn` with an internal audit committee minute are hr's and core's (access R55), never this module's; a burn never touches asset records or amounts. **[M + PG; AT-34]**
43. **[S1]** **Unregistered capital lines block period close (default, OQ-4).** `pending_documents` also lists the posted bill lines dated in the period on a category cost account that are not linked to any asset (draft or active). `close_period` refuses while any exist (`unregistered_capital_lines`, en/ar, naming bill and line). *As built:* the refusal keeps the ledger's code `documents_pending`; the `unregistered_capital_lines` text (en/ar, naming bill and line) is in its message (§10). **[registry; AT-35]**
44. **[S1]** **CWIP.** Bill lines on the CWIP cost account accumulate on a `cwip` asset (never depreciated, R12). A `capitalise_cwip` event moves an amount ≤ the CWIP cost to a new or existing asset of a depreciable category of the same entity (FA5), on the final engine decision of activity `fixed_assets.capitalisation`. The new asset then needs its own `available_for_use_date` and approved policy (R14); its retention class follows §3.1. **[svc + engine; AT-36]**
45. **[S1]** **Accounts not yet approved.** A category account FK may stay empty while its proposed account is not in the entity's chart (OQ-5, D7). An event or run that needs an empty account is refused (`account_not_set`, en/ar, naming category and account role). No code path falls back to another account. **[svc; AT-37]**
46. **[S1 / S2 for (c)]** *(Added 2026-10-22, compliance S3, S4, S10, A11, A16; team default; Needs expert sign-off: tax.)* **VAT, Art. 52 and Zakat data.**
    - (a) At capitalisation, each bill-sourced asset snapshots `input_vat_amount` and `initial_recovery_pct` from its linked bill lines (VAT recovered on 1310 ÷ VAT charged; non-recoverable VAT is already in the cost, invoicing R104). `adjustment_period_end` is computed from `acquisition_date` and the 3.9 setting for its retention class (movable: the shorter of the setting and the accounting life).
    - (b) `vat_use` and `held_for` are required at submission (preparer's choice, en/ar dropdowns). `use_evidence_ref` is optional.
    - (c) **Slice 2, invoicing change:** a `blocked` value for `vat_attribution` on bill lines (Art. 50: input VAT is blocked on vehicles available for private use, with no apportionment; recoverable for vehicles let on rental contracts or used exclusively for business). Its VAT is added to the asset's cost component. Vehicle cost lines default to `blocked` unless the category is `rental_fleet` or the line is marked "exclusive business use, no private availability" with evidence. Whether a given rental fleet qualifies under exception (b) is OQ-26.

    **[svc; AT-44]**
47. **[S1 event / S2 report]** *(Added 2026-10-22, compliance S4; team default; Needs expert sign-off: tax.)* **Changes of VAT use.** A change of `vat_use` (or of `usage` that changes it, e.g. IP moving from commercial to residential lease, a rental vehicle moving to private use) is recorded only by an `AssetEvent(kind=vat_use_change)`: no GL entry, approved under `fixed_assets.estimate_change`, effective on its `event_date`. Editing `vat_use` directly is refused. Slice 2: a year-end Art. 52 report lists, per entity, the capital assets inside their adjustment period whose `vat_use` or `usage` changed during the year, for the tax adviser; no adjustment is computed or posted (OQ-10, OQ-27). **[svc + engine; AT-45]**
48. **[S1]** *(Added 2026-10-22, compliance S8, A5, A6, A15; team default; Needs expert sign-off: auditor.)* **Investment-property fair values.** `record_ip_fair_value(asset, *, user, valuation_date, amount, valuer_name_ar, valuer_name_en, valuer_licence_ref, evidence_ref, undue_cost_justification_ar/en)` (`fixed_assets.record_event`) appends an `IpFairValue` for an `ip_*` asset of the same entity; it posts nothing. It serves the IAS 40.79(e) disclosure under the cost model and the s16 undue-cost-or-effort justification per property. `ip_disclosure(entity, as_of, *, user)` returns, per IP asset, carrying amount, latest fair value (or "none"), valuer and justification. For an IFRS for SMEs entity, an IP asset with no fair value and no justification at a fiscal year end is listed as a gap (en/ar) in that report. **[svc; AT-46]**
49. **[S1]** *(Added 2026-10-22, compliance S5; team default.)* **1510 is for investment property only.** A category whose cost account is 1510 must have an `ip_land` or `ip_building` class, and an `ip_*` category must use 1510 (or an approved IP sub-account): otherwise saving it is refused (en/ar). This keeps rental vehicles out of IP and out of 15-year real-estate retention. Renaming 1510 to "Investment property (land and buildings) / العقارات الاستثمارية (أراضٍ ومبانٍ)" is a CFO proposal under D7 / OQ-5, not done here. **[M clean + svc; AT-47]**

## 5. Accounting

Template check 2026-10-22 (`ledger/coa_template.py`):
- **Exist:** 1500, 1510, 1520, 1590, 5400 (all used here); 1250 (bai' ajel, R6); 1600 (out of scope); 1200, 1210, 2100, 2110 (`control`); 1310, 2200, 2220 (`tax_control`); 2400 and 5600 (Islamic financing; 2400 used by FA8); 2410 and 5610 (`prohibited`); 3900 (opening equity).
- **Not in the template:** every code in the table below.

Taken codes checked (2026-10-17): `ledger/coa_template.py` (all codes incl. 1500–1600, 3400, 4500, 4700, 4710, 5750–5770, 5900); invoicing.md §5 (1330, 2220, 2310, 2320, 4120–4310, 5910, 5920); ledger-fx.md (3400, 4700, 4710, 5750, 5760, 5770, 1101); payroll.md (1230, 1240, 1241, 1250, 2330, 2340, 2350, 5120, 5130, 4610, 5930); banking.md (1101–1104 examples); consolidation.md (1610, 3600, 3700). Rechecked 2026-10-22: also 1140 (purification) and 5940 (tax penalties, proposed, invoicing §5). No clash with the codes below: 1591 appears only as a Falcon code in migration fixtures, not as an ERP code.

**Proposed, CFO to approve (OQ-5; D7):**

| Code | name_en | name_ar [AR-review] | Type | Flags (amended 2026-10-22) | Used for |
|---|---|---|---|---|---|
| 1530 | Furniture, fixtures and office equipment | الأثاث والتجهيزات والمعدات المكتبية | asset | restricted (R9) | Cost |
| 1540 | Computer and IT equipment | أجهزة الحاسب وتقنية المعلومات | asset | restricted | Cost |
| 1550 | Capital work in progress | مشروعات تحت التنفيذ | asset | restricted | Not depreciated |
| 1560 | Land (own use) | الأراضي (للاستخدام الخاص) | asset | restricted | Not depreciated |
| 1570 | Buildings (own use) | المباني (للاستخدام الخاص) | asset | restricted | Cost |
| 1591 | Accumulated depreciation – investment property | مجمع استهلاك العقارات الاستثمارية | asset (contra) | restricted | IP shown separately from PPE |
| 1595 | Accumulated impairment – property, plant and equipment | مجمع خسائر هبوط قيمة الممتلكات والآلات والمعدات | asset (contra) | restricted | FA6 |
| 1596 | Accumulated impairment – investment property | مجمع خسائر هبوط قيمة العقارات الاستثمارية | asset (contra) | restricted | FA6 |
| 4800 | Net gain (loss) on disposal of fixed assets | صافي أرباح (خسائر) استبعاد الأصول الثابتة | income | may carry debits | FA3–FA5, FA7 |
| 5420 | Impairment losses (reversals) on fixed assets | خسائر (عكس) هبوط قيمة الأصول الثابتة | expense | may carry credits | FA6 |
| 3500 | Revaluation surplus | فائض إعادة التقييم | equity | | Reserved; not used until OQ-8 |
| 4810 | Fair value gain (loss) on investment property | أرباح (خسائر) القيمة العادلة للعقارات الاستثمارية | income | may carry debits | Reserved; not used until OQ-6 |

Notes on these accounts:
- "Restricted" = a `ledger.restricted` registration by `fixed_assets` (R9), **not** the template flag `control`.
- Existing 1590 stays for accumulated depreciation of all PPE (1500, 1520, 1530–1570). Splitting 1590 by class: OQ-5.
- When proposed 15xx codes are approved, migration W11 (`ASSET_COST_CODES`) and the R41 source codes are extended with them.
- 1510 rename: R49.
- A RETT expense account for R22a is to be proposed with OQ-10.
- *Note 2026-10-22 (compliance N8):* 4800 gains and losses map outside revenue in reporting.md (IAS 16.68).
- *Note (compliance N5):* decommissioning provisions (IAS 16.16(c), s17.10(c)) are out of scope. If they are added later, the unwinding of the discount is interest-like and needs a Sharia classification.

**FA0. Opening balances** (posted by the migration opening entry, migration R14 to R16; no entry here). Example DRYH (dry run 2026-10-21): Dr 1500 36,000.00 / Dr 1520 240,000.00 / Cr 1590 101,500.00 (other lines of the opening entry not shown). Gate: R41.

**FA1. Acquisition** (posted by invoicing, E7; no entry here): Dr 1520 Vehicles 120,000.00 / Dr 1310 VAT input 18,000.00 (if recoverable, invoicing R29) / Cr 2100 138,000.00. The register links the line (R2). Input VAT on passenger cars (Art. 50): input VAT is blocked on vehicles available for private use, and recoverable for vehicles let on rental contracts or used exclusively for business. A blocked vehicle books the 18,000.00 to 1520 instead (R46(c), slice 2). **Needs expert sign-off: tax.**

**FA2. Monthly depreciation run** (vehicle cost 120,000.00, residual 20,000.00, 60 months, available 2027-01-10; January 22/31 days)
| Account | Dr | Cr |
|---|---|---|
| 5400 Depreciation | 1,182.80 | |
| 1590 Accumulated depreciation | | 1,182.80 |
Full months: 1,666.67; IP buildings credit 1591. One line pair per (expense account, accumulated account); per-asset detail in `DepreciationLine`. Registered type `fixed_assets.depreciationrun`; posting grant `fixed_assets.run_depreciation`.

**FA3. Sale to a third party** (slice 2; cost 120,000.00, accumulated 70,000.00, NBV 50,000.00; proceeds 55,000.00 + VAT 15%, standard rate since 1 July 2020)
Sales document (invoicing, after ZATCA): Dr 1200 63,250.00 / Cr 4800 55,000.00 / Cr 2200 8,250.00.
Disposal event:
| Account | Dr | Cr |
|---|---|---|
| 1590 Accumulated depreciation | 70,000.00 | |
| 4800 Net gain (loss) on disposal (proposed) | 50,000.00 | |
| 1520 Vehicles | | 120,000.00 |
Net 4800: credit 5,000.00 (gain).

**FA4. Write-off** (slice 1; cost 8,000.00, accumulated 7,000.00): Dr 1590 7,000.00 / Dr 4800 1,000.00 / Cr 1540 8,000.00. Grant `fixed_assets.dispose`.

**FA5. CWIP completion** (slice 1): Dr 1570 Buildings 2,000,000.00 / Cr 1550 CWIP 2,000,000.00 (`capitalise_cwip`; grant `fixed_assets.capitalise`).

**FA6. Impairment** (slice 1; IP building carrying 900,000.00, recoverable 850,000.00): Dr 5420 50,000.00 / Cr 1596 50,000.00. Reversal: reverse sides, capped (R19). Grant `fixed_assets.impair`.

**FA7. Intercompany sale** (slice 2; ALM vehicle cost 100,000.00, accumulated 40,000.00, sold to SUB at 80,000.00 + VAT 12,000.00 at 15%)
| Entity | Account | Dr | Cr | Counterparty |
|---|---|---|---|---|
| ALM | 1210 Due from group entities | 92,000.00 | | SUB |
| ALM | 4800 (proposed) | | 80,000.00 | |
| ALM | 2200 VAT output | | 12,000.00 | |
| ALM | 1590 / 4800 / 1520 (event) | 40,000.00 / 60,000.00 | 100,000.00 | |
| SUB (mirror bill drafted by `system_intercompany`, approved by SUB's matrix; amended 2026-10-22) | 1520 Vehicles | 80,000.00 | | |
| SUB | 1310 VAT input | 12,000.00 | | |
| SUB | 2110 Due to group entities | | 92,000.00 | ALM |
Consolidation eliminates the 20,000.00 gain and excess depreciation (consolidation CE4).

**FA8. Murabaha-financed acquisition** (added 2026-10-22, R7; slice 1; machine cash cost price 300,000.00, murabaha sale price 330,000.00 over 3 years)
| Account | Dr | Cr |
|---|---|---|
| 1500 PPE | 300,000.00 | |
| 2400 Islamic financing | | 300,000.00 |
The deferred profit of 30,000.00 is never capitalised. It is booked against 2400's contra and expensed to 5600 over the term by the financing records, outside this module (split: OQ-9). VAT on the underlying supply is booked by the documents that carry it, not by this event.

**No entry** for: categories, policies, settings, draft assets, activation (cost already posted by the bill), locations, change of estimate (prospective), `vat_use_change`, IP fair values, opening register load (FA0 is migration's), reconciliation and Zakat reports.

No fixed-asset document ever has a line on 1200, 1210, 2100, 2110, 1310, 2200, 2220, 1240, 1241, 1250 or 4610 (R38 `validate_lines`); receivables, payables and VAT of disposals are invoicing's.

## 6. Integrations

| System | Direction | What |
|---|---|---|
| `invoicing` (internal) | In / Out | In (S1): posted `PurchaseDocumentLine`s on category cost accounts, with their VAT, recovery and due dates, through the read-only `invoicing.fixed_assets_port` (R2, R4a, R46; pull, no post hook). Out (S2): income type "fixed asset disposal" (4800) and `fixed_asset` reference on sales lines; retention class and `acquisition_date` for disposal documents (R33; invoicing change, OQ-14); `blocked` VAT attribution (R46(c)); category E and RETT evidence for real estate (R22a). In (S2): mirror bills and `MirrorBillLink` (R24; invoicing R113, to build). |
| `core.access` (internal) | Out | Engine adapters (`fixed_assets/approval_adapters.py`), activities in `authority_defaults.py`, catalogue permissions, seed migration (§7). `submit`, `decide`, `withdraw`, `check_issued_band`. |
| `ledger` (internal) | Out | `create_entry(post=True, grant, document=run/event)`; types registered in `ledger/documents.py` (R38); restricted accounts in `ledger/restricted.py` (R9); `reverse_entry`; `close_period` via `pending_documents`. |
| `migration` (internal) | In / Out | In: workbook Fixed assets sheet (W1, W11) loaded by section `fixed_assets` of `load_subledgers` (R40). Out: `opening_register_balances` as an R22 source (R41). |
| `zatca` (internal) | Out (via invoicing) | `retention_class`, `acquisition_date` (zatca 3.x, R-38) (S2). zatca R-38 needs the useful-life correction of R33 (zatca spec owner). |
| `billing` (internal) | In | Active `vehicle_rental` contracts by `vehicle_ref` (R27); `property_ref` for locations. Cost pools (billing R75, OQ-43; owner A8 "cost pools at go-live") read depreciation from the ledger (5400), not from this module. *Note 2026-10-22 (compliance N4):* rent from IP let to tenants whose activity is not Sharia-compliant should be flagged for purification (4900 → 2600); that is billing's, not this module's. |
| `payroll` (internal) | none / boundary | Bai' ajel assets stay on 1250 (R6, R28). |
| `hr` (internal) | In | Employee code for `custodian_ref` (no personal data copied). |
| `consolidation` (internal) | Out | Intercompany sale pairs, buyer depreciation (consolidation R21) (S2). |
| `tax_returns` (internal) | Out | `zakat_inputs` (R34); Art. 52 report (R47, S2). |
| `reporting` (internal) | Out | Movement schedule per class (opening, additions, disposals, depreciation, impairment, closing) for PPE and IP notes; IP fair-value disclosure (R48); 4800 mapped outside revenue (N8). |
| ZATCA (external) | none direct | Through invoicing / zatca only. |
| Banks, GOSI, Mudad, SAP B1 | none | |
| Falcon (or other legacy system) | In (migration) | Through the migration workbook only (R40, R41); fictional data in tests and dry runs; real exports never committed or given to an agent (migration W2). |

*Data location (added 2026-10-22, compliance N1):* the register, its events and fair values are VAT records for capital assets (Art. 52, TIR §8.3.3). They are hosted in KSA with the rest of the ERP data (TIR §8.3.1; zatca R-43, Z39).

## 7. Permissions (per entity; amended 2026-10-22)

**New catalogue permissions** (`core/access/catalogue.py`; seed migration `core/00NN_seed_access_v21_fixed_assets.py`, roles frozen as in 0029 / 0035):

| Codename | Flags | Default roles (seed V21) | Agent |
|---|---|---|---|
| `fixed_assets.view` | Ag | accountant, approver, ceo, managing_partner, internal_auditor, external_auditor, business_analyst | Read if granted |
| `fixed_assets.maintain_setup` (categories, locations, settings, setup command) | — | accountant, ceo | No |
| `fixed_assets.maintain_policy` (draft and submit policy versions) | — | accountant, ceo | No |
| `fixed_assets.approve_policy` | A | approver, ceo, managing_partner | No |
| `fixed_assets.create_asset` (draft assets, link bill lines) | Ag | accountant, ceo | Draft only, logged |
| `fixed_assets.submit_asset` (submit for capitalisation) | — | accountant, ceo | No |
| `fixed_assets.capitalise` (activation, CWIP transfer, financed acquisition) | A, P | approver, ceo, managing_partner | No |
| `fixed_assets.prepare_depreciation` (prepare, submit, discard runs) | Ag (prepare only) | accountant, ceo | Draft only |
| `fixed_assets.run_depreciation` | A, P | approver, ceo, managing_partner | No |
| `fixed_assets.record_event` (draft, submit, discard events; record IP fair values) | — | accountant, ceo | No |
| `fixed_assets.impair` | A, P | approver, ceo, managing_partner | No |
| `fixed_assets.change_estimate` (estimates, `vat_use_change`) | A | approver, ceo, managing_partner | No |
| `fixed_assets.dispose` (write-off; S2 sales) | A, P | approver, ceo, managing_partner | No |
| `fixed_assets.reverse_document` (in `REVERSE_PERMISSIONS`) | P | approver, ceo | No |

The opening register load uses `migration.import` (accountant, ceo), as invoicing's open items do (R40). `managing_partner` is a group-scope role: with no cascade (access R64) it acts only where it holds a membership (the group parent by default). Agents never hold A or P permissions (access R12).

**Approval activities** (`core/access/authority_defaults.py`, `ApprovalActivity` rows in the same seed):

| Activity | name_en / name_ar [AR-review] | Permission | Amount basis | Code default (no matrix row) | Slice |
|---|---|---|---|---|---|
| `fixed_assets.policy` | Depreciation policies / سياسات الاستهلاك | `fixed_assets.approve_policy` | none | holders in the entity | S1 |
| `fixed_assets.capitalisation` | Asset capitalisation, CWIP transfers and financed acquisitions / رسملة الأصول وتحويل المشروعات تحت التنفيذ والاقتناء الممول | `fixed_assets.capitalise` | amount (cost capitalised, SAR) | holders | S1 |
| `fixed_assets.depreciation_run` | Depreciation runs / مسيرات الاستهلاك | `fixed_assets.run_depreciation` | amount (run total) | holders | S1 |
| `fixed_assets.impairment` | Impairments and reversals / هبوط القيمة وعكسه | `fixed_assets.impair` | amount | holders | S1 |
| `fixed_assets.estimate_change` | Changes of estimate and of VAT use / تغييرات التقديرات واستخدام الضريبة | `fixed_assets.change_estimate` | none | holders | S1 |
| `fixed_assets.disposal` | Write-offs and disposals / الشطب والاستبعاد | `fixed_assets.dispose` | amount (NBV derecognised) | holders | S1 (write-off), S2 (sales) |
| `fixed_assets.reversal` | Reversals of runs and events / عكس المسيرات والأحداث | `fixed_assets.reverse_document` | amount (entry total) | holders | S1 |

"Holders" = every holder of the permission through a membership in that entity: the CFO (approver) and the CEO (D18, A5), and the Managing Partner in the group parent. This reproduces D5 until each entity enters its own matrix rows (OQ-21). Self-approval: R37.

**Key messages** (en / ar [AR-review]):

| Code | English | العربية |
|---|---|---|
| `no_policy` | Category {category} has no approved depreciation policy in force. | لا توجد سياسة استهلاك معتمدة سارية للفئة {category}. |
| `run_changed` | The run's assets or events changed after it was prepared: discard and re-prepare. | تغيّرت أصول المسيّر أو أحداثه بعد إعداده: ألغِه وأعد إعداده. |
| `unregistered_capital_lines` | Period {period} has posted bill lines on asset accounts that are not in the register: {lines}. | في الفترة {period} بنود فواتير مرحّلة على حسابات الأصول غير مسجلة في سجل الأصول: {lines}. |
| `account_not_set` | Category {category} has no {role} account; it is set once the account is approved. | لم يُحدَّد حساب {role} للفئة {category}؛ يُحدَّد بعد اعتماد الحساب. |
| `vehicle_on_contract` | Vehicle {code} is on active contract {contract}; it cannot be disposed of. | السيارة {code} مرتبطة بالعقد الساري {contract}؛ لا يمكن استبعادها. |
| `bai_ajel_asset` | Lines on 1250 are held for an employee's deferred sale and are not registered. | البنود على الحساب 1250 محتفظ بها للبيع الآجل للموظف ولا تُسجَّل أصولًا. |
| `opening_asset_changed` | Asset {legacy_code} was loaded with other values; a re-run does not change it. | سبق تحميل الأصل {legacy_code} بقيم مختلفة؛ ولا تغيّره إعادة التشغيل. |
| `opening_not_posted` | The migrated register of {entity} counts only once its opening entry is posted. | لا يُعتدّ بسجل الأصول المرحّل للكيان {entity} إلا بعد ترحيل القيد الافتتاحي. |
| `financing_cost_capitalised` | Financing cost or deferred profit cannot be part of an asset's cost (Sharia). | لا يجوز أن تكون تكلفة التمويل أو الربح المؤجل جزءًا من تكلفة الأصل (الضوابط الشرعية). |
| `not_built_oq9` | Capitalising borrowing costs is not built (OQ-9). | رسملة تكاليف الاقتراض غير متاحة (السؤال المفتوح 9). |
| `real_estate_disposal_blocked` | Disposals of real estate wait for the tax adviser's sign-off on VAT and RETT (OQ-10). | استبعاد العقارات بانتظار اعتماد المستشار الضريبي لضريبة القيمة المضافة وضريبة التصرفات العقارية. |
| `vat_use_by_event` | VAT use changes only through a change-of-VAT-use event. | لا يتغير استخدام الضريبة إلا من خلال حدث تغيير استخدام الضريبة. |
| `ip_account_class` | Account 1510 is for investment property categories only. | الحساب 1510 مخصص لفئات العقارات الاستثمارية فقط. |

## 8. Acceptance tests

**Fixtures:**
- Entities:
  - ALM: group parent, SAR, Sharia enforced, IFRS for SMEs, top authority `managing_partner`;
  - SUB: 100%, SAR, top authority `ceo`;
  - NSH: SAR, `sharia_enforced=False`, IFRS for SMEs;
  - FUL: SAR, `sharia_enforced=False`, full IFRS.
- Categories and **test-only** approved policies (vehicle 60 months, IT 36 months, machinery 120 months, IP building 480 months; fixture values, not policy).
- Proposed accounts §5 as fixture data; `FixedAssetSettings` with defaults.
- 2027 periods.
- Users: A (accountant), A2 (accountant), C (approver), C2 (approver), V (viewer), M (managing partner, ALM), CA (ceo in ALM, not top authority), CE (ceo in SUB), agent service user G.
- Amounts `Decimal`. No matrix rows unless stated (code defaults apply).

| # | S | Given | When | Then |
|---|---|---|---|---|
| AT-1 | 1 | Bill line Dr 1520 120,000.00, posted | A drafts the asset from `capitalisable_lines`, submits; A decides; C decides; a vendor credit note reduces the line by 10,000.00 | Draft asset with component 120,000.00; A refused `self_approval`; C's decision activates it (policy copied, no entry); the credit note leaves a pending adjustment and the next run refuses until an `AssetEvent` is posted |
| AT-2 | 1 | FA2 vehicle | Runs Jan 2027 to the last month | Jan 1,182.80; full months 1,666.67; total = 100,000.00 exactly; last month absorbs residue |
| AT-3 | 1 | IT asset 10,000.00, reducing balance 40%/yr | Run Jan, Feb | 333.33, then 322.22; never below residual |
| AT-4 | 1 | Run for ALM 2027-01 prepared and submitted by A | A decides; C decides; A prepares a second run for 2027-01; C requests a reversal with a `ledger.reverse_entry` grant; then through the engine (`fixed_assets.reversal`) | A refused `self_approval`; C's decision posts FA2 lines; second run refused; `ledger.reverse_entry` grant refused (`document_reversal_only`); engine reversal posted, run `reversed`, period re-runnable |
| AT-5 | 1 | 2027-01 closed; asset available 2027-01-10 activated in February | Prepare Feb run | Catch-up line for January days in February run; nothing dated January |
| AT-6 | 1 | Submitted run for 2027-02; active assets and no run for 2027-03 | `close_period` 2027-02; 2027-03 | Both refused (en/ar) naming the run / missing run |
| AT-7 | 1 | Submitted run; then an asset is activated or an event posted | C decides the run | Refused `run_changed` (en/ar); nothing posted |
| AT-8 | 1 | Category "Furniture" without approved policy; then A drafts and submits a policy, C approves via engine | Submit an asset; prepare a run including it; repeat after approval | Before: both refused `no_policy` naming the category; after: allowed |
| AT-9 | 1 | FA4 IT asset (cost 8,000.00, accumulated 7,000.00 after the posted 2027-03 run); second IT asset in service, no April run yet | A submits `write_off` of the first dated 2027-03-20, C decides; A submits `write_off` of the second dated 2027-04-15, C decides; prepare the April run | First: no extra depreciation (R25), FA4 posts with grant `fixed_assets.dispose`, 4800 debit 1,000.00, asset `disposed`, read-only. Second: April run holds its line for 15 days, and the write-off derecognises the NBV after that line |
| AT-10 | 1 | Acquired 2027-03-10: IP building (1510); vehicle (life 60 months); IT asset (36 months); machinery (120 months); leasehold improvement; a `cwip` asset later capitalised into a vehicle category. Setting `art52_adjustment_months_movable` = 72 (default, pending tax sign-off) | Compute `retention_until`; then lower the setting to 60 | IP: 2042-03-31; vehicle: 2037-03-10 (60 months + 5 years); IT: 2035-03-10 (36 months + 5 years, later than the 6-year standard date 2033-03-10); machinery: 2038-03-10 (72 months + 5 years); leasehold improvement and CWIP: real-estate class; the CWIP-turned-vehicle keeps its real-estate date (never shortened); lowering the setting never shortens any date |
| AT-11 | 2 | FA7 data; R113 built; SUB has group vendor ALM | ALM's invoice with `intercompany_sale` event becomes final; SUB staff set the line to 1520 and submit; SUB's approver decides; SUB user lists capitalisable lines | ALM: 1210 with counterparty SUB, 4800 net credit 20,000.00; SUB: draft mirror bill created by `system_intercompany`, amounts read-only, nothing posted until SUB's decision; then posted Dr 1520 / Dr 1310 / Cr 2110 (ALM); draft asset with `transferred_from` = ALM asset, `acquisition_date` = bill accounting date; ALM-only user sees "not visible" for the origin in SUB |
| AT-12 | 1 | FA6 | A submits impairment; C decides; then a reversal of 80,000.00 | Impairment 50,000.00 posted; depreciation recomputed on 850,000.00; reversal capped at the no-impairment carrying amount |
| AT-13 | 1 | IP asset; PPE asset | Submit `fair_value_change`; submit `revaluation` | Both refused in ALM and SUB (en/ar, citing OQ-6 / OQ-8) |
| AT-14 | 1 | Bill line on 1250 (bai' ajel), posted | List capitalisable lines; create an asset from it; set 1250 as a category account; payroll sells a register vehicle (bill on 1520) on bai' ajel | Not listed; creation refused `bai_ajel_asset`; category refused; the sale refused by invoicing (`cost_mismatch`) |
| AT-15 | 1 | Vehicle whose `code` is a billing `vehicle_ref` on an active contract | Submit write-off; contract ends; submit again | First refused `vehicle_on_contract` (en/ar, naming the contract); second allowed |
| AT-16 | 1 | ALM register at 2027-12-31 with `held_for` set | `zakat_inputs(ALM, 2027-12-31)` | NBV per category = cost − accumulated depreciation − impairment; CWIP and IP separate; per-asset `held_for` and evidence reference; ties to GL accounts 15xx; V allowed, user without `fixed_assets.view` refused |
| AT-17 | 1 | Machine bought on murabaha: cash cost price 300,000.00, murabaha price 330,000.00; a posted 5600 journal line of 5,000.00 and a 5610 line | In ALM, NSH and FUL: submit `financed_acquisition` at 300,000.00 (C decides); at 330,000.00; a `subsequent_cost` event sourced from the 5600 line; from the 5610 line | ALM and NSH: 300,000.00 posts FA8 (Dr 1500 / Cr 2400); 330,000.00 refused `financing_cost_capitalised` (en/ar); 5600- and 5610-sourced components refused. FUL: 300,000.00 posts; 330,000.00 and the 5600-sourced component refused `not_built_oq9` (en/ar) |
| AT-18 | 1 | User of ALM only | List assets; link an SUB bill line to an ALM asset; set an SUB location on an ALM asset | Only ALM assets listed; cross-entity links refused |
| AT-19 | 1 | `ar` locale | Register screens, run errors, event reasons, reminders, §7 messages, engine inbox labels | Arabic shown |
| AT-20 | 1 | Land asset (1560) and IP land | Prepare run | No depreciation lines for them |
| AT-21 | 1 | Vehicle 60 months, after 12 months; change approved 2027-12-15 while December 2027 is open | A submits `change_of_estimate` to 48 months total; C decides | Approved, no entry; effective 2027-12-01 (earliest open month on or after approval); remaining NBV spread over the remaining months from December; earlier runs untouched |
| AT-22 | 1 | Active asset | Change `entity`, cost, category or `vat_use` directly; delete a disposed asset | Refused (`vat_use_by_event` for `vat_use`) |
| AT-23 | 1 | Restricted 1500, 1510, 1520, 1590 | Manual journal Dr 5400 / Cr 1590 (draft and post); manual journal Dr 1520 / Cr 1100; bank transaction on 1520; bill on 1520; bill on 1590; migration opening entry with 1520 | Manual journals and bank transaction refused `restricted_account` (en/ar); bill on 1520 posts; bill on 1590 refused; opening entry allowed (gate R41 permitting); register-to-GL report zero after FA2, FA4 to FA6, FA8 |
| AT-24 | 1 | Agent G | Draft asset from a posted bill; prepare a run; submit either; decide; post | Drafts allowed and logged; submit, decide and post refused |
| AT-25 | 1 | New subsidiary NEW added as data, setup run | Bill to 1520, capitalise, run | Works with no code change; numbers prefixed `NEW`; code defaults and settings apply in NEW |
| AT-26 | 1 | USD bill line USD 10,000.00 at 3.7500 on 1540 | Post bill; register; period-end revaluation | Component 37,500.00 SAR; revaluation does not touch 1540 or the asset |
| AT-27 | 1 | SUB: CE prepares and submits a run; ALM: CA prepares and submits a run; ALM: M prepares and submits a run; SUB with `self_approval_enabled=False` | Each decides their own run | CE allowed, `self_approval` log row, listed in SUB's governance report; CA refused `self_approval`; M allowed and logged; with the switch off CE refused |
| AT-28 | 1 | ALM matrix rows for `fixed_assets.depreciation_run`: 0–50,000.00 approver; 50,000.01–∞ managing partner; then a version with a gap 50,000.01–60,000.00 | Submit a run of 60,000.00 (first version); C decides; M decides; submit under the gap version; no rows at all for `fixed_assets.impairment` | C refused `not_in_matrix`; M posts; gap version: submission refused `no_authority_rule`; impairment routed by code default (`rule_source=default`) |
| AT-29 | 1 | C delegates `fixed_assets.run_depreciation` approvals to C2 for 30 days; one row with `delegation_allowed=False` | C2 decides a run under each row | Allowed (`on_behalf_of` C, in governance report) under the first; refused `delegation_not_allowed` under the second |
| AT-30 | 1 | ALM rows as AT-28; a run of 50,000.00 routed to C; a test double makes the recomputed total 50,000.01 with the fingerprint unchanged | C's final decision | `post_depreciation` calls `check_issued_band`: refused `amount_outside_band` (en/ar); nothing posted; the request is returned to the preparer |
| AT-31 | 1 | KWD bill line KWD 1,000.125 at rate 12.2000 on 1520 | Post bill; register; enter residual 100.005 SAR | Component 12,201.53 SAR (half up); residual refused `amount_decimals` (en/ar) |
| AT-32 | 1 | Fictional workbook for DRYH: 3 assets (1520: 240,000.00; 1500: 36,000.00; accumulated 101,500.00 on 1590), cut-over 2026-12-31, map approved; one vehicle cost 100,000.00, accumulated 40,000.00, residual 0, remaining 36 months | Dry run; apply; re-run; re-run with a changed cost; agent runs the section; prepare Jan 2027 run before and after the opening entry posts; load after posting | Dry run writes nothing; apply creates 3 active `import` assets (`held_for` from usage; missing `vat_use` listed), no entry, no request; re-run skipped; changed refused `opening_asset_changed`; agent refused; run refused `opening_not_posted` before, then the vehicle line 1,666.67; load after posting refused `opening_posted` |
| AT-33 | 1 | AT-32 register loaded; opening entry with 1520 240,000.01 | Post the opening entry; fix to 240,000.00; post | First refused, difference 1520 0.01 listed (en/ar); second posts |
| AT-34 | 1 | Assets, runs, events, fair values in every status | Delete any (ORM, bulk, SQL on PostgreSQL); run every scheduled job; a `burn` decision on the custodian employee | All deletes refused; no job deletes; asset rows and amounts unchanged |
| AT-35 | 1 | Posted bill line on 1520 dated 2027-02 not linked | `close_period` 2027-02; link to a draft asset; close again | Refused (code `documents_pending`, message with the `unregistered_capital_lines` text naming bill and line); then closes (no other pending item) |
| AT-36 | 1 | Bills on 1550 of 1,200,000.00 and 800,000.00 | Register; prepare run; submit `capitalise_cwip` 2,000,000.00 to a new building asset; C decides | One `cwip` asset 2,000,000.00, no depreciation; FA5 posts with grant `fixed_assets.capitalise`; the building needs its own capitalisation decision before depreciation |
| AT-37 | 1 | Category with empty impairment accounts | Submit an impairment | Refused `account_not_set` (en/ar) naming category and account role |
| AT-38 | 1 | Catalogue, registry, seed | Inspect | Permissions of §7 with their flags; P ones in `DOCUMENT_POST_PERMISSIONS`, `fixed_assets.reverse_document` in `REVERSE_PERMISSIONS`; both document types registered with `build_lines` and `validate_lines`; `ApprovalActivity` rows exist; a run whose lines touch 1200 or 2200 is refused by `validate_lines` |
| AT-39 | 1 | ALM `normal_credit_days` empty, then 60; bill on 1520 dated 2027-02-01, due 2027-08-01 | Register the line (before and after the setting) | Before: not flagged, register report says terms not set (en/ar); after: component and asset `deferred_payment`, listed; cost still the full bill amount |
| AT-40 | 2 | FA3 asset | Draft sales document + `disposal_sale` event; decide event before ZATCA clearance; after clearance | Event waits while the sales document is pending; after clearance FA3 posts, 4800 net credit 5,000.00, asset `disposed`; the sales line offers no profit-margin option |
| AT-41 | 2 | AT-10 assets | Draft disposal invoices for the IP building and the vehicle | IP invoice class `capital_asset_real_estate` though its line is on 4800; vehicle invoice flagged `movable_capital_asset` with `acquisition_date` 2027-03-10 |
| AT-42 | 2 | IP building in ALM; R22a not enabled | Submit `disposal_sale` with a sales document at 15% | Refused `real_estate_disposal_blocked` (en/ar, citing OQ-10); after enablement: refused with category S, refused without RETT record and payment evidence, allowed with category E and evidence |
| AT-43 | 2 | IP building in ALM; SUB buyer | Submit `intercompany_sale` | Refused `real_estate_disposal_blocked` until OQ-10 (and OQ-11(b)) are signed off; no mirror bill created |
| AT-44 | 1 | Bill line Dr 1520 100,000.00 with VAT 15,000.00, 60% recovered (9,000.00 on 1310, 6,000.00 in cost) | Capitalise; submit without `vat_use` or `held_for` | Submission refused until both set; `input_vat_amount` 15,000.00, `initial_recovery_pct` 60.0000, `adjustment_period_end` = acquisition + min(72, life) months |
| AT-45 | 1 | Active IP building, `vat_use=taxable` | Edit `vat_use` directly; A submits `vat_use_change` to `exempt` dated 2027-06-01, C decides | Direct edit refused `vat_use_by_event`; event approved, no journal entry, asset shows `exempt` from 2027-06-01 with history kept |
| AT-46 | 1 | ALM (IFRS for SMEs) IP buildings X and Y at 2027-12-31 | Record a fair value for X with valuer and evidence; record one for a vehicle; run `ip_disclosure(ALM, 2027-12-31)` | X recorded, no entry; vehicle refused; report shows X with fair value, Y listed as a gap (no fair value, no justification) en/ar |
| AT-47 | 1 | Categories | Save a vehicle category with cost account 1510; an `ip_building` category with 1500 | Both refused `ip_account_class` (en/ar) |
| AT-48 | 1 | ALM (IFRS for SMEs) and FUL (full IFRS); fiscal year end 2027-12-31; ALM `reporting_dates` includes 2027-06-30 | Run the reminder job on 2027-06-30 and 2027-12-31 | ALM: impairment-indicator reminders on both dates; estimate review reminder at year end worded as an indicator check; FUL: estimate review reminder at year end (IAS 16.51) and impairment-indicator reminder at year end; all en/ar |

## 9. Open questions

**BLOCKING** = builder cannot build that part. **Default** = safe default used in the rules. **Go-live** = buildable now, but real use waits for the answer.

| # | Question | Owner | Status |
|---|---|---|---|
| OQ-1 | Capitalisation threshold (amount, per item or per invoice; group-wide or per entity). | CFO | Default: none (R3) |
| OQ-2 | Useful lives, reducing-balance rates and residual values per category; group-wide or per entity. | CFO | *Amended 2026-10-22:* **not blocking the build** (policy data entered and approved through the engine, R14; tests use fixture values). **Go-live:** no real asset is capitalised until its category's policy is approved |
| OQ-3 | Depreciation convention: daily pro-rata vs full month (from in-service month or month after); disposal month. | CFO / accountant | Default: daily pro-rata both ends (R11, R25) |
| OQ-4 | Must `close_period` refuse a period with active assets and no posted run? *Added 2026-10-22:* and with posted bill lines on asset accounts not in the register (R43)? | Accountant | Default: yes to both (R15, R43) |
| OQ-5 | Approve proposed accounts 1530, 1540, 1550, 1560, 1570, 1591, 1595, 1596, 4800, 5420 (3500, 4810 reserved); split 1590 per class; separate gain (48xx) and loss (54xx) accounts; ~~`is_control` on asset accounts~~. *Amended 2026-10-22:* asset accounts get no `control` flag (R9); confirm instead that cost and accumulated accounts are **restricted** to bills, fixed-asset documents and the opening entry. Also: rename 1510 to "Investment property (land and buildings) / العقارات الاستثمارية (أراضٍ ومبانٍ)" (compliance S5, CFO proposal under D7, not done); a RETT expense account (R22a). | CFO / accountant | Default: proposed codes in fixtures; template unchanged until approved; restricted (R9). **Go-live:** impairment, write-off and CWIP postings in a real entity need 4800, 5420, 1550, 1595/1596 (R45) |
| OQ-6 | Investment property model per entity: s16 fair value through P&L where reliably measurable without undue cost or effort vs cost model; IAS 40 choice for full-IFRS entities; valuer and frequency **[CC-verify]**: not verified 2026-10-22 (under s16.7 fair value is measured at each reporting date; valuer accreditation in KSA (Taqeem) not searched). **Needs expert sign-off: auditor.** Group alignment (consolidation OQ-15). | Accountant / auditor | Default: cost model for all (R20), fair values and per-property justification recorded (R48). *Amended 2026-10-22 (compliance S8):* **BLOCKING for the first annual financial statements of any IFRS for SMEs entity holding IP** (not for the slice-1 build) |
| OQ-7 | Impairment: cash-generating units for rental property, who supplies recoverable amounts; which period ends are reporting dates. | Accountant | *Amended 2026-10-22 (compliance A4):* indicator assessment at each reporting date (R19); default: fiscal year end plus the entity's `reporting_dates`; manual recoverable amount with evidence |
| OQ-8 | Revaluation model for PPE: IFRS for SMEs s17.15–17.15B and IAS 16.29 both allow it per class (compliance A7); which classes, and any SOCPA or local restriction (the CMA cost-model requirement covered listed companies only, for years before 2022; SOCPA's SME endorsement text not found). **Needs expert sign-off: auditor.** | Accountant / auditor | Default: not built (R21) |
| OQ-9 | Full-IFRS entities with Islamic financing (murabaha/tawarruq on 2400/5600) for a qualifying asset: IAS 23 capitalisation (IAS 23.8) vs the Sharia rule "no interest capitalisation". *Added 2026-10-22 (compliance B1):* how murabaha deferred profit is split between cost and expense, and its booking against 2400's contra. **Needs Sharia board sign-off** and auditor view. | Managing Partner + Sharia board + auditor | Default: nothing capitalised; principal at cash cost price allowed (R7, FA8) |
| OQ-10 | VAT and RETT on disposals and intercompany transfers of real estate (VAT exempt under Royal Order A/84; RETT 5% under the RETT Law 2024 and its Implementing Regulations in force 10 April 2025; exemptions); VAT on used vehicles (15% on the full price; no margin scheme); input VAT on passenger cars (Art. 50); the Art. 52 capital-asset adjustment. Art. 52 applies to capital assets within the adjustment period (secondary sources: 6 years for movable assets or a shorter accounting life; 10 years for real estate) when the taxable-use percentage changes, e.g. IP moving from commercial to residential lease. *Added 2026-10-22 (compliance N3):* are occasional sales of capital assets left out of the Art. 51 recovery ratio (invoicing R104)? **Needs expert sign-off: tax.** | Tax adviser | Default: VAT category chosen by staff per invoicing; real-estate disposals refused (R22a); RETT not built; no Art. 52 adjustment computed (data captured, R46, R47) |
| OQ-11 | Intercompany transfer price (NBV, market value, cost-plus; transfer pricing / Zakat view) and the buyer's useful life (new life vs remaining life). *Added 2026-10-22 (compliance S9):* (a) VAT value between related persons: the VAT Law allows fair-market-value substitution, which matters where the buyer cannot fully recover VAT (e.g. residential IP); article not verified. (b) Real estate: a RETT exemption for 100% intra-group transfers (described as Art. 3(18) of the 2025 RETT Implementing Regulations) with a 5-year lock-up and retroactive clawback; not verified against the regulation text. (c) Transfer pricing and the Zakat view of the price. **Needs expert sign-off: tax.** | Managing Partner + CFO + tax adviser | Default: price entered by the seller's staff, approved under the seller's matrix (sales document + `fixed_assets.disposal`); the buyer's cost is the mirror bill's copied amount (D11); buyer's policy life; real estate refused (R22a). Owner A8 "cost-plus" was given for services; whether it applies to asset transfers is open |
| OQ-12 | May an existing register asset be sold to an employee on bai' ajel (payroll R47)? **Needs Sharia board sign-off.** | Managing Partner + Sharia board | Default: refused (R6); *note 2026-10-22:* enforced by built invoicing R120 (sale only against 1250 bill debits) |
| OQ-13 | Billing `vehicle_ref` to become a FK to `FixedAsset` (billing change). | Dev + accountant | Default: free text matched on asset code or plate (R27) |
| OQ-14 | Invoicing change: sales line `fixed_asset` reference, "fixed asset disposal" income type on 4800, retention class from the linked asset (R33). Retention of purchase bills for capital assets. | Dev + tax adviser | **BLOCKING** for slice 2 only (R22, R24, R33 hand-off); slice 1 does not need it |
| OQ-15 | Tax depreciation (Income Tax Law Art. 17: pooled declining balance by group: fixed buildings 5%; movable industrial and agricultural buildings 10%; factories, machinery, computers, software and vehicles 25%; geological and exploration costs 20%; other assets 10%; status of the 2023 draft law to be checked) and deferred tax for entities with non-Saudi shareholders; Zakat treatment of NBV, IP and land (owned by tax-returns.md; Zakat IR 2024: net book value of assets acquired for use). *Note 2026-10-22 (compliance N6):* if built, Art. 17 works on group pools; a cap on deductible repair costs is also believed to apply (not verified). **Needs expert sign-off: tax.** | Tax adviser | Default: book NBV only, with `held_for` and evidence (R34, R46) |
| OQ-16 | Componentisation (e.g. building structure vs lifts, vehicle vs fit-out) and major overhauls (subsequent cost); reclassification between categories. | Accountant | Default: one asset per bill line; land and building separate; subsequent cost via event; reclassification slice 2 |
| OQ-17 | Leases as lessee (right-of-use assets, IFRS 16 / s20) in scope? | Accountant | Default: out of scope |
| OQ-18 | Physical verification (asset tags, counts) and custodian sign-off. | CFO | Default: not built; `custodian_ref` and location only |
| OQ-19 | Opening register from Falcon (or other legacy system): per-asset detail available, or category totals only? | Accountant + migration-agent | *Resolved 2026-10-22 by the built workbook (W1, W11):* one row per asset, or per category where no detail exists; loaded by R40 and tied by R41. Remainder: OQ-20 |
| OQ-20 | *Added 2026-10-22.* Migrated assets: keep the legacy method, useful life and remaining life after the cut-over (prospective), or re-estimate under the approved policy at go-live? Legacy accumulated impairment: the workbook has no column, so is any impairment inside the legacy accumulated figure? | Accountant / CFO | Default: legacy estimates kept until a `change_of_estimate` (R40); opening impairment 0 (the whole legacy accumulated figure is depreciation) |
| OQ-21 | *Added 2026-10-22.* Authority matrix rows per entity for the seven fixed-asset activities (bands, e.g. write-offs above an amount to the top authority as for payroll loan write-offs); should write-offs or policy approvals be NSA (never self-approved)? | Each entity's top authority (access R76) | Default: code default "holders" (CFO, CEO; MP in the group parent); no NSA (R37) |
| OQ-22 | *Added 2026-10-22.* May an asset be acquired other than by a vendor bill or a murabaha acquisition event (paid straight from a bank transaction, built by own staff with payroll cost, received as a capital contribution)? | Accountant | Default: no. Restricted accounts accept bills, mirror bills, fixed-asset events (CWIP, financed acquisition) and the opening entry only (R9); other routes refused |
| OQ-23 | *Added 2026-10-22.* Beneficiary rule (access R82) for asset events, e.g. may the custodian of an asset approve its write-off? | CFO | Default: no beneficiary in slice 1; disposal sales use invoicing's customer-employee link (R22) |
| OQ-24 | *Added 2026-10-22 (compliance S11).* Group-wide rule "no financing cost is capitalised in any entity", including full-IFRS entities with `sharia_enforced=False` (a departure from IAS 23.8 there)? | Project owner + auditor + Sharia board | Default: R7 applies to Sharia-enforcing and IFRS for SMEs entities only; full-IFRS non-Sharia entities: capitalisation not built (`not_built_oq9`) |
| OQ-25 | *Added 2026-10-22 (compliance S13).* Deferred-payment purchases and disposals: cash-price-equivalent measurement (s17.13, IAS 16.23; significant financing component on sales under s23 / IFRS 15). How is any imputed component classified in a Sharia entity (cost or 5600 deferred-sale profit, never interest)? What are each entity's normal credit terms? | Auditor + Sharia board; CFO for the terms | Default: flag only, no split (R4a); terms empty until the CFO sets them |
| OQ-26 | *Added 2026-10-22 (compliance S3, A11).* Invoicing `blocked` VAT attribution for vehicles (Art. 50) and the evidence for "exclusive business use"; does each rental fleet qualify under exception (b)? | Tax adviser + dev (invoicing change) | Default: slice 2; until then staff choose the attribution per invoicing R29 / R104 |
| OQ-27 | *Added 2026-10-22 (compliance S1, S2, S4, A16).* Art. 52 adjustment periods (movable 6 years or shorter accounting life; real estate 10 years; secondary sources), whether leasehold improvements and CWIP for buildings are real estate, the year-end Art. 52 report format, and `vat_use` / recovery values for migrated assets. | Tax adviser | Defaults: 72 / 120 months (3.9); leasehold and CWIP real estate (§3.1); report slice 2; migrated assets 100% recovery unless the accountant enters otherwise |
| OQ-28 | *Added 2026-10-22 (compliance S12).* Which IFRS for SMEs edition (2015 or 2025, 3rd edition effective 1 January 2027) and which SOCPA modifications each entity applies in FY2027; re-check 17.15B, 27.30, s20 and the other paragraph numbers against the 2025 text. | Auditor | Default: 2025 edition references; `Entity.reporting_standard` governs (reporting.md) |
| OQ-29 | *Added 2026-10-22 (review of ccb9cef).* An abandoned CWIP or other draft asset whose bills are in the GL: how is it written off? A `write_off` event needs an active asset, and a draft has no depreciation. | Accountant | Default: not built; the draft stays in the register (and in `zakat_inputs`, R34) until decided |

**Blocking:** OQ-14 for slice 2 only (also needs invoicing R113 / billing R73 built for R24); R22a's enablement needs OQ-10. **Nothing blocks slice 1's build.**

**Go-live for slice 1:**
- OQ-2: policy data;
- OQ-5: accounts for impairment, write-off, CWIP and the proposed classes;
- OQ-6: blocks the first annual statements of IFRS for SMEs entities holding IP;
- OQ-27: Art. 52 settings, sign-off.

**Sign-offs:**
- Sharia board: OQ-9, OQ-12, OQ-24, OQ-25.
- Tax adviser: OQ-10, OQ-11, OQ-15, OQ-26, OQ-27; R22a, R33, R34, R46, R47.
- Auditor: OQ-6, OQ-8, OQ-24, OQ-25, OQ-28; R20, R21, R48.
- Re-verification: every compliance source was read through search extracts (compliance report, "How far this was verified"). Before approval, someone opens the VAT IR (Art. 50, 52, 66), the Zakat IR 2024 and the IFRS for SMEs 2025 text and confirms the article and paragraph numbers.

## 10. As built: slice 1 (module-builder, 2026-10-22)

Branch `module/fixed-assets`. App `fixed_assets` (models, `services.py`, pure arithmetic in `calc.py`, `approval_adapters.py`, `migration_port.py`, `jobs.py`, read-only admin, commands `fixed_assets_setup` and `fixed_assets_reminders`); read-only port `invoicing/fixed_assets_port.py`; access seed V21 in `core/0038`. Details settled or deviating from the text above:

- **R2, default category.** When several categories share a cost account (VEH and VEHR on 1520), the one that is not a rental fleet is the default; a rental fleet is always chosen explicitly. Otherwise the preparer names the category.
- **R4, cost adjustments.** A posted vendor credit note on a bill feeding an asset, or the reversal of the source document of any component (whatever its sign), makes the next run refuse (`pending_adjustment`, naming the document). It is resolved by `record_cost_adjustment` (a `subsequent_cost` event, approved under `fixed_assets.capitalisation`) which posts nothing (invoicing booked the GL side) and ends `approved`, not `posted`; the cost change is depreciated from the next month. A further bill line on an active asset uses the same event. A draft asset takes credit-note lines directly.
- **R7.** The 330,000.00 attempt (AT-17) is refused at drafting; a `subsequent_cost` event may name a source journal line only to be refused (financing account: R7 code; anything else: OQ-22).
- **R9.** The proposed 15xx codes (1530 to 1570, 1591, 1595, 1596) are restricted now as well: no real chart has them until the CFO approves them, so this changes nothing until then and needs no code change later. The R41 source answers for the same codes.
- **R14.** Only depreciable categories need a policy. Submission checks the policy in force on the available-for-use date; activation copies that policy (R16's "activated after its effective_from" read as available for use after it); a run checks the policy in force at the period end, for migrated assets too.
- **R15.** The fingerprint hashes the run's inputs (assets, their events and components, earlier posted runs of the entity), not its amounts, so `check_issued_band` sees a recomputed total (AT-30). Also refused: a run while an earlier open month with depreciation has no posted run (`earlier_run_missing`), and a run with nothing to post (`nothing_to_depreciate`). Every fixed-asset approve service refuses a direct call (`approval_request_required`), not only `post_depreciation`.
- **R15, R43 close.** `close_period` keeps the ledger's code `documents_pending`; its message lists the registry's bilingual descriptions (the run, "No posted depreciation run for …", and the `unregistered_capital_lines` text naming bill and line). No ledger change.
- **R16.** The approval date is the access clock's date; the effective month is the first open period ending on or after it. The event keeps the values it replaced (`previous_values`). A new total useful life is measured from the asset's available-for-use date; for a migrated asset that is the legacy available-for-use date from the workbook (or, when the workbook has none, the day after the cut-over), so the months elapsed under the legacy system count (`life_too_short`, review item 6).
- **R18.** A reversal is requested by a person with the document's submit right (`request_reversal`, reason required) and decided under `fixed_assets.reversal` (`fixed_assets.reverse_document` is P, not A, so it goes through the engine with `require(..., through_engine=True)`). Refused while a later posted run or event depends on it (`reversal_order`); a financed acquisition or CWIP transfer is reversed only while its asset is still a draft.
- **R19.** The impairment is measured on the posted carrying amount on the event date and changes depreciation from the first day of the next month. The reversal cap compares the schedules with and without the impairment through the last posted month.
- **R25.** A write-off dated in an open month derecognises the depreciation scheduled to that date; that month's run then books it, so 1590 shows a debit between the two postings. The register-to-GL report shows that stub as a reconciling line (asset, amount, en/ar note) until the run posts.
- **R33.** A draft's retention date is provisional (its life is unknown); from activation on it is never shortened. The tax period is the entity's VAT return period (quarterly if empty). R33(b): invoicing bills carry no `retention_until` field, so there is nothing to read for bills; disposal documents are slice 2.
- **R34.** `zakat_inputs` counts draft assets too (their bills are in the GL, e.g. a CWIP still being built).
- **R40.** The location is matched on its Arabic name; a new one gets the next `Lnnnn` code. The opening date is the workbook's cut-over date.
- **R46 (a).** `initial_recovery_pct` stays empty when no VAT was charged on the linked lines (the ratio is undefined).
- **R47.** `vat_use` changes on approval; `vat_use_on(asset, date)` answers from the event history.
- **R48.** A row may carry the undue-cost justification without a fair value (`amount` empty).
- **R16, R19 reminders.** The fiscal year end is the end of the entity's last period of the calendar year. Reminders are stored rows (`ReviewReminder`), one per entity, kind and date.
- **Not built (slice 2 or later):** `AssetEvent.sales_document`, sales and intercompany sales (refused `kind_slice_2`; real estate `real_estate_disposal_blocked`), reclassification, the Art. 52 report, the invoicing `blocked` attribution.
- **ATs, test notes.** AT-1 and AT-4: the accountant holds no approval right, so the refusal is `no_membership`; `self_approval` is shown for a maker who holds the right (CA). AT-27: the managing partner gets an accountant membership in ALM to prepare (`prepare_depreciation` is not an MP right in §7). AT-44: invoicing as built recovers a taxable line fully and puts an exempt line's VAT in cost (R104 not built), so the 60% comes from two lines of one asset. AT-15: the vehicle contracts are fixture rows (billing's approval flow is billing's). AT-23: bank-transaction and payroll refusals are shown on the restricted-account allowances. AT-34: no `burn` service exists yet, so that part waits for hr/core; the register has no link to it. AT-9 uses a fixture IT category with a 7-month life and a 1,000.00 residual (fully depreciated by March) to reach FA4's figures with a write-off dated 2027-04-10, since a write-off into a month a posted run depreciated is refused (review item 1). The dry-run fixture's two vehicles now carry AT-32's figures (same sheet totals).

### Review of ccb9cef: team decisions (2026-10-22)

Decided by the team after the code review of ccb9cef; built and tested on `module/fixed-assets` (tests in `fixed_assets/test_review.py` and `fixed_assets/test_pg.py`; AT figures above updated where they changed).

1. **Back-dated events (R25, R19, R47).** An event is refused when it is dated on or before the end of the last month a posted run depreciated the asset (or its opening date for a migrated asset): `event_before_posted_run`, en/ar, naming the first allowed date. This applies to write-offs, impairments and their reversals and the CWIP transfer's target; not to a change of estimate (item 3), a cost adjustment (follow-up P4), a disposal adjustment (item 7) or a `vat_use` change (no depreciation effect; follow-up nit). A write-off derecognises the opening accumulated depreciation plus the scheduled depreciation through the event date only. Example: IT 3,600.00 over 36 months, January to April posted: a write-off dated 2027-02-20 is refused; one dated 2027-05-15 derecognises 448.39 (400.00 posted + 15/31 of May's 100.00), and once May's run posts 1590 nets to zero for the asset.
2. **Linking a line to an existing asset (R4).** `create_asset_from_line(asset=…)` takes `category` only as None or the asset's own category (`line_category` otherwise), and the line's account must be that category's cost account.
3. **Impairment dates and change-of-estimate month (R16, R19).** Impairments and reversals use the guard of item 1. A change of estimate takes effect in the first open month, on or after the approval date, that has no posted run; without one it is refused (`no_unrun_month`).
4. **Opening register (R40, migration W11).** A depreciable row without a method or a remaining life is refused (`opening_row`, en/ar), as is a `reducing_balance` row until the workbook has a rate column; the workbook check reports `depreciation_estimate` and `reducing_balance_rate`. Never a TypeError.
5. **CWIP transfer (R8).** The target's component is dated at the transfer date (its cost enters the schedule from then) and keeps the CWIP's acquisition date in `AssetCostComponent.acquired_on` for retention, which is never shortened.
6. **Changes after the end of life (R16, R4).** A cost change or change of estimate effective after the life has ended, or after the asset reached its residual value, is booked whole in its effective month. A change taking the carrying amount below the residual books the difference in that month. A new useful life must exceed the months already elapsed (`life_too_short`). The NBV is never negative; a write-off stays possible.
7. **Credit or debit note on the bill of a disposed asset.** `record_cost_adjustment` on a disposed asset creates a `disposal_adjustment` event (new `AssetEvent` kind), approved under `fixed_assets.dispose` (the write-off's permission), that books the difference to the disposal result: a credit note Dr cost account / Cr 4800, a debit note the mirror. It clears `pending_adjustments`, so `close_period` is no longer blocked.
8. **Reversed source documents (R4).** Every component whose source document is reversed is flagged, whatever its sign. An approved `subsequent_cost` event (it posts nothing) is corrected by voiding it through the reversal activity (`request_reversal`, decided under `fixed_assets.reversal`): it becomes `reversed`, a reversing component is added prospectively (after the last posted month), and the line may then be linked again (it counts as unregistered for R43 until it is: follow-up P1).
9. **Concurrent links of one bill line (R4).** The purchase line is locked (`select_for_update`, via `invoicing/fixed_assets_port.line_of(…, lock=True)`) before the remaining amount is computed; a second link waits and is refused `line_fully_linked`. PostgreSQL test.
10. **Triggers (R18, R42).** Migration 0004 replaces the generic keep trigger on runs and events. A posted run only goes posted to reversed, with the reversal entry and the reversal request fields; its period, status, total, number, fingerprint, poster and entry are fixed. A posted or approved event only goes to reversed; its amounts (`amount`, `cost_derecognised`, `depreciation_derecognised`, `impairment_derecognised`), dates, links, estimate values and entry are fixed (the entry may be set once). No line is inserted into, changed in or deleted from a posted or reversed run. PostgreSQL tests for each.
11. **Reversal band.** The amount checked against the reversal band is `cost_derecognised` for a write-off (the event's `amount` otherwise).

Smaller decisions:
- The admin does not show or edit `FixedAsset.transferred_from`.
- `draft_policy` refuses floats and excess decimals and quantizes the rate (4 places) and the residual percentage (3 places); the useful life is a positive whole number.
- `update_settings` checks its values: months 1 to 600, credit days empty or a whole number 0 to 3,650, reporting dates ISO month-end dates (`settings_value`, en/ar).
- A missing financing account (2400) gives `account_not_set`, en/ar, naming the account.
- R27: vehicle plates are compared without case, spaces and hyphens.
- `close_period` keeps the code `documents_pending` (R43 and AT-35 amended above).
- Abandoned draft CWIP: OQ-29.

### Follow-up review of acb5701: team decisions (2026-10-22)

- **P1 (R43).** A bill line counts as registered when its net linked amount is not zero, so a voided link puts it back in `unregistered_capital_lines` and `close_period` refuses until it is linked again.
- **P2 (R18, R4).** The void of an approved `subsequent_cost` is refused (`reversal_order`) while the asset is disposed. *Amended by the third review, item 1:* later posted events of an active asset do not block it (the void is prospective, and an impairment can no longer be reversed once a later month is run, so it would block the void for ever).
- **P3 (R18).** A write-off is reversed only after its posted disposal adjustments are reversed (`reversal_order`).
- **P4 (R4).** A cost adjustment keeps its document date (the event date defaults to the credit note's or bill's accounting date, and its component is booked on that date, so the register ties to invoicing's entry); the date guard of item 1 does not apply to it. *Third review, item 2:* the component's accounting date is taken from the purchase document (its accounting date; for the adjustment of a reversed bill, the date of the bill's reversal entry, read through `invoicing/fixed_assets_port.booked_on`), and an event date that differs from it is refused (`adjustment_date`, en/ar), earlier or later. This also applies to disposal adjustments. Its `effective_from` is the month after the document date, pushed past the last posted month, so no run month is ever changed. The adjustment that takes a reversed bill out of the cost (`reverses_component`) is never voided (`void_reversed_bill`, en/ar): the bill stays reversed.
- **P5 (R47).** A `vat_use` change may be dated in a month already run (it has no depreciation effect).
- **Triggers (R18, R42).** Migration 0005 extends 0004: a posted or reversed run also keeps its maker trail (created, submitted by and at); a posted, approved or reversed event also keeps its evidence (cash cost price, financing total and contract, recoverable amount, source journal line, reason, evidence reference), its old VAT use and its maker trail, and `previous_values` is written once (only while it is `{}`). 0004's backwards migration restores 0002's own line-lock function. PostgreSQL tests.
