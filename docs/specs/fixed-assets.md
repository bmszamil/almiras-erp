# Spec: fixed assets (`fixed_assets`)

Status: draft for review · Written: 2026-10-17 · Author: spec-writer agent
Django app: `fixed_assets` (new). Reads: `invoicing` (vendor bill lines, sales documents), `ledger` (posting, periods, accounts), `billing` (vehicle rental contracts), `payroll` (bai' ajel boundary). Feeds: `ledger` (depreciation and event entries), `zatca`/`invoicing` (retention class, acquisition date), `consolidation` (intercompany transfers), `docs/specs/tax-returns.md` (Zakat base inputs), `docs/specs/reporting.md` (PPE / investment property notes).
**[CC-verify]** = standards, tax or Sharia statement written from memory; the compliance-checker must check it before module-builder starts.

## 1. Purpose

Keeps an **asset register per entity** (every entity, Almiras or any subsidiary added as data) for property, plant and equipment, vehicles (including vehicles rented out on annual contracts), investment property (1510) and capital work in progress (tangible assets only; intangibles on 1600 are out of scope). Assets are created from posted vendor bill lines (or migration), depreciated by a monthly depreciation run that posts through `ledger.services` (straight line by default, reducing balance optional) using useful lives approved by the CFO as policy data, and leave the register by disposal, write-off or intercompany sale (a normal VAT invoice). The module records impairment, supplies retention classes and acquisition dates to the ZATCA archive, and supplies net book values for the Zakat base. Sharia: no interest is ever capitalised. All texts are English and Arabic.

## 2. Decisions

| # | Date | Who | Decision |
|---|---|---|---|
| D1 | in repo | `ledger/coa_template.py` (accountant draft) | Accounts 1500 PPE, 1510 Investment property (rental assets), 1520 Vehicles, 1590 Accumulated depreciation, 5400 Depreciation exist in the group template. |
| D2 | in repo | CLAUDE.md | Posting only via `ledger.services.post_entry` / `create_entry`; posted entries never edited, corrections are reversals; closed periods reject postings; entity FK on every record; Sharia enforced per entity; `Decimal` money; en/ar; new entities are data. |
| D3 | 2026-10-16 | Project owner (billing D1) | Cars rented on annual contracts are invoiced automatically from billing contracts. |
| D4 | 2026-10-16 | Project owner (invoicing D11, billing D2, D10) | No VAT group: intercompany sales are normal VAT invoices with the counterparty; no automatic bill in the buyer entity. |
| D5 | 2026-10-16 | Project owner (billing D5) | Accounting staff create; the CFO approves. |
| D6 | 2026-10-17 | Project owner (payroll P12) | Bai' ajel may finance an asset for an employee; margin decided by the Managing Partner per term and value; Sharia compliant; no interest or late fees. |
| D7 | 2026-10-17 | Project owner (ledger-fx D10, invoicing D29, payroll P6) | Proposed account codes stay **proposed** until the accountant/CFO reviews them. |
| D8 | in repo (2026-10-16) | zatca.md R-38 (from ZATCA documents) | Retention: `capital_asset_real_estate` ≥ 15 years; `movable_capital_asset` = acquisition date + Art. 52 adjustment period + 5 years, never earlier than the standard 6 years. |
| D9 | caller brief 2026-10-17, not a business decision | Spec scope | Useful lives are policy data approved by the CFO; straight line default, reducing balance optional. |

Nothing else is decided (capitalisation threshold, lives, residual values, investment property model, revaluation, transfer prices: §9).

## 3. Data model

Conventions as invoicing.md: `entity` FK → `core.Entity` (PROTECT, required) on every model, immutable after creation; functional money `DecimalField(18, 2)`; `created_by`, `created_at`, `updated_at`, `source` (`human` / `import` / `agent:<name>` / `system:<name>`). "Bi" = `_en` and `_ar`, Arabic required.

### 3.1 `AssetCategory` (per entity, seeded from a group template)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity, code | FK / Char(20) | Y | | Unique `(entity, code)` |
| name | Char(100) | Y | Bi | |
| asset_class | Choice `land`, `building`, `ip_land`, `ip_building`, `vehicle`, `furniture_office`, `it_equipment`, `machinery`, `leasehold_improvement`, `cwip`, `other` | Y | | `ip_*` = investment property |
| cost_account / accum_depr_account / depr_expense_account / accum_impairment_account | FK ledger.Account (same entity, postable) | Y (depr. accounts N for non-depreciable) | | Defaults §5 |
| depreciable | Bool | Y | | False for `land`, `ip_land`, `cwip` (R12) |
| retention_class | Choice `capital_asset_real_estate`, `movable_capital_asset` | Y | | `land`, `building`, `ip_*` → real estate; others movable (R33) |

### 3.2 `DepreciationPolicy` (versioned policy data; CFO approves)

`entity`, `category` (FK), `effective_from`, `method` (`straight_line`, `reducing_balance`), `useful_life_months` (Y for straight line), `annual_rate` (Decimal(7,4), Y for reducing balance), `residual_pct` (Decimal(6,3), default 0), `status` (`draft`, `approved`), `approved_by`/`approved_at` (approver, not creator), `basis_note` (Bi). No overlapping approved versions per category.

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
| acquisition_date | Date | Y | | Accounting date of the first cost component (bill), or transfer date (R5, R33) |
| available_for_use_date | Date, null | Y at activation | | Depreciation starts here (R11) |
| cost | Money (functional) | computed | | Σ `AssetCostComponent.amount` |
| residual_value | Money | Y | | Default from policy `residual_pct` × cost |
| method / useful_life_months / annual_rate | as 3.2 | Y | | Copied from policy at activation; changes only by R16 |
| accumulated_depreciation / accumulated_impairment / nbv | Money | computed | | From posted runs and events |
| status | Choice `draft`, `active`, `fully_depreciated`, `disposed` | Y | | R8 |
| transferred_from | FK FixedAsset (other entity), null | N | | Intercompany origin (R24); shown only to users with rights in both entities |
| retention_until | Date | computed | | R33 |
| approved_by / approved_at | FK User / DateTime | at activation | | Not the creator |

### 3.4 `AssetCostComponent`

`entity`, `asset`, `kind` (`bill_line`, `cwip_transfer`, `opening_balance`, `subsequent_cost`), `purchase_line` (FK invoicing `PurchaseDocumentLine`, same entity, posted; unique across components unless split, R4), `amount` (functional, = the line's posted functional debit or a split of it), `source_event` (FK AssetEvent, null). Foreign-currency bills: functional amount at the bill rate; no later revaluation (non-monetary, ledger-fx R21).

### 3.5 `DepreciationRun` (document; registered in `ledger/documents.py`)

`entity`, `period` (FK FiscalPeriod), `number` (`{code}-DEP-{yyyy}-{000001}`), `status` (`draft`, `posted`, `discarded`), `prepared_by`/`at`, `posted_by`/`at`, `journal_entry` (FK JournalEntry, PROTECT, null), `fingerprint` (hash of assets, policies, events in scope). **`DepreciationLine`**: `run`, `asset`, `days`, `amount`, `catch_up` (Bool), `opening_nbv`.

### 3.6 `AssetEvent` (document; registered in `ledger/documents.py`)

`entity`, `asset`, `kind` (`capitalise_cwip`, `subsequent_cost`, `change_of_estimate`, `impairment`, `impairment_reversal`, `disposal_sale`, `write_off`, `intercompany_sale`, `reclassification`, `revaluation`, `fair_value_change`), `event_date`, `amounts` (fields per kind: proceeds, recoverable_amount, new life, etc.), `sales_document` (FK invoicing SalesDocument, null; Y for `disposal_sale`, `intercompany_sale`), `evidence_ref`, `reason` (Bi), `status` (`draft`, `submitted`, `posted`), `created_by`, `submitted_by`, `posted_by`, `journal_entry`. `revaluation` and `fair_value_change` exist as choices but are refused (R20, R21).

### 3.7 `AssetLocation`

`entity`, `code`, `name` (Bi), `property_ref` (billing `property_ref` / `unit_ref` text), `city` (Bi).

## 4. Rules

### Register and acquisition
1. Every asset belongs to one entity; its category, location, cost components and accounts belong to the same entity. **[M clean + svc; AT-18]**
2. Posting a vendor bill line (invoicing) to an account used as `cost_account` of a category (1500, 1510, 1520 and proposed 1530 to 1570) creates a **draft** asset per line (or requires an existing draft/active asset to be linked) through an invoicing post hook; the asset becomes `active` only when an approver (not its creator) activates it with `available_for_use_date`. Lines on CWIP (1550) create or extend a `cwip` asset. **[svc + invoicing hook; AT-1]**
3. Capitalisation threshold: none (every line on a cost account is registered) until the CFO sets one (default, OQ-1). Below-threshold items are a staff choice of an expense account at bill entry, not a register rule. **[svc; AT-1]**
4. A bill line feeds at most its own posted functional amount across components; reversing or crediting the bill (vendor credit note) reduces the component and needs an `AssetEvent` before the next run. **[svc + DB unique; AT-1]**
5. `acquisition_date` = accounting date of the earliest cost component (bill) or the transfer date (R24); never later than `available_for_use_date`. It is fixed at activation. **[M clean; AT-10]**
6. Bill lines on 1250 (assets acquired for employee bai' ajel, payroll proposed) are never registered or depreciated: they are held for sale to the employee (payroll R47). An active register asset cannot be sold on bai' ajel (default, OQ-12). **[svc; AT-14]**
7. Sharia, every entity: no financing cost is capitalised. A cost component from, or a journal crediting an asset cost account against, a financing account (2400, 2410, 5600, 5610, any `prohibited`) is refused, whatever `sharia_enforced`. Conventional-interest accounts are already blocked where Sharia is enforced (ledger). Under full IFRS, IAS 23 would require capitalising borrowing costs on qualifying assets **[CC-verify]**; IFRS for SMEs s25 expenses them **[CC-verify]**: conflict for Islamic financing cost in full-IFRS entities is OQ-9. **[svc; AT-17]**
8. Status: `draft → active → fully_depreciated → disposed` (or `active → disposed`). An `active` asset's category, cost, method and life change only through posted events (R16). Disposed assets are kept, read-only. **[svc + M guard; AT-22]**
9. Cost accounts, accumulated depreciation and impairment accounts of categories carry `is_control` (invoicing §5 proposal): manual journals on them are refused; only this module's documents post there. A register-to-GL reconciliation report (per entity, account, date) must show zero difference. **[ledger validate hook + report; AT-23]**

### Depreciation
10. Methods: `straight_line` (default) = (cost − residual − accumulated impairment allocation) spread evenly over the remaining life in months; `reducing_balance` = opening NBV × `annual_rate` / 12, never below residual. **[svc; AT-2, AT-3]**
11. Depreciation starts on `available_for_use_date` and stops on disposal date or when NBV = residual. First and last months are prorated by days (actual days / days in month) (default, OQ-3). **[svc; AT-2]**
12. `land`, `ip_land` and `cwip` are never depreciated. **[svc; AT-20]**
13. Per asset per month the amount is rounded half up to 2 dp; the last month absorbs the residue so total depreciation = cost − residual exactly. **[svc; AT-2]**
14. An asset whose category has no **approved** policy in force cannot be activated; a run refuses to include it (en/ar naming the category). Useful lives and rates are not defaulted in code (OQ-2). **[svc; AT-8]**
15. **Monthly run.** `prepare_depreciation(entity, period, *, user)` (`fixed_assets.prepare_depreciation`) builds a draft run with one line per active depreciable asset; `post_depreciation(run, grant)` posts it through `ledger.services.create_entry(..., post=True, grant=..., document=run)` with the run's registered `build_lines` (§5 FA2), by an approver who is not the preparer (access R14). It recomputes and refuses if the fingerprint changed ("discard and re-prepare", en/ar). One posted run per entity and period; a second is refused. `discard_depreciation` drops a draft. Runs are posted only in open periods; the registered `pending_documents` makes `close_period` refuse a period with a draft run, or with active depreciable assets and no posted run (default, OQ-4). **[svc + ledger registry; AT-4, AT-5, AT-6, AT-7]**
16. Changes of estimate (life, residual, method) are prospective from the first day of an open month (IAS 8 / s10 **[CC-verify]**), through a `change_of_estimate` event approved by the approver; a new approved policy version applies to assets activated after its `effective_from` only, unless an event re-applies it. **[svc; AT-21]**
17. Assets activated late (available-for-use month already closed) get a `catch_up` line in the next open run; nothing posts to a closed period. **[svc; AT-5]**
18. Reversing a posted run uses `ledger.services.reverse_entry` with an approver grant; the run is marked reversed and the period may be re-run while open. **[svc; AT-4]**

### Impairment, investment property, revaluation
19. **Impairment** (IAS 36 / s27 **[CC-verify]**): an `impairment` event records recoverable amount and evidence; posts Dr impairment expense / Cr accumulated impairment for carrying amount − recoverable amount; later depreciation uses the new carrying amount over the remaining life. A reversal is capped at the carrying amount that would have existed without the impairment. Approver posts, not the creator. Annual indicator review: reminder only (OQ-7). **[svc; AT-12]**
20. **Investment property (1510).** IFRS for SMEs s16 requires fair value through profit or loss when fair value is reliably measurable without undue cost or effort, otherwise the cost–depreciation–impairment model **[CC-verify]**; full IFRS (IAS 40) is a policy choice **[CC-verify]**. Until OQ-6 is decided, every entity uses the cost model: IP buildings are depreciated, IP land is not, and `fair_value_change` events are refused (en/ar). Land and building are separate assets. **[svc; AT-13]**
21. **Revaluation** of PPE (caller brief: full IFRS only; IFRS for SMEs s17.15B may also allow it **[CC-verify]**): not built; `revaluation` events are refused in every entity until OQ-8. **[svc; AT-13]**

### Disposal and transfers
22. **Sale to a third party**: staff draft an invoicing sales document (tax invoice, ZATCA clearance per invoicing/zatca) with a line of income type "fixed asset disposal" on 4800 (proposed) and the `fixed_asset` reference, and a `disposal_sale` event linked to it. The event posts when the sales document posts (after ZATCA clearance/reporting, invoicing R41), dated the sales document's posting date; it derecognises cost, accumulated depreciation and impairment against 4800. VAT category per invoicing (used movable asset: standard rate **[CC-verify]**; real estate: VAT exempt and real estate transaction tax instead **[CC-verify]**, OQ-10). **[svc + invoicing; AT-9]**
23. **Write-off / scrap** without proceeds: `write_off` event, Dr 4800 / Cr cost, Dr accumulated accounts; approver posts. **[svc; AT-9]**
24. **Intercompany sale** (assets never move between entities): the seller issues a normal VAT invoice to the buyer entity's customer record (counterparty set, AR on 1210, D4) with an `intercompany_sale` event (as R22). The buyer's staff enter the vendor bill (D4: no automatic bill), Dr the buyer's cost account / Cr 2110 with the seller as counterparty, which creates a new draft asset in the buyer with `transferred_from` set, `acquisition_date` = buyer bill date, cost = buyer's bill line, and life from the buyer's policy (OQ-11). The price is entered by staff and approved by the seller's CFO (default, OQ-11). `consolidation` reads the pair for unrealised profit (consolidation R21). **[svc + invoicing; AT-11]**
25. A disposal or intercompany sale event dated in a month already depreciated posts no extra depreciation; one dated in an open month includes depreciation up to the disposal date in that month's run (default, OQ-3). **[svc; AT-9]**
26. Gain/loss = proceeds (net of VAT) − NBV, shown net on 4800 (may carry a debit balance). Separate gain and loss accounts: OQ-5. **[svc; AT-9]**

### Vehicles, bai' ajel, retention, Zakat
27. **Rental vehicles.** Vehicles let on annual contracts (billing `vehicle_rental`) stay in the lessor's register as `usage=rental_operating_lease` and are depreciated (lessor operating lease, s20 / IFRS 16 **[CC-verify]**). Hand-off to billing: `vehicle_ref` should hold the asset `code` of an active vehicle of the same entity (billing amendment, OQ-13; free text until then). Once linked, a disposal or intercompany sale of a vehicle with an `active` billing contract on it is refused (en/ar). **[svc + billing query; AT-15]**
28. **Bai' ajel boundary** (payroll R47 to R49): purchase on 1250, sale on 1240/1241/4610 are payroll's; this module only refuses registration (R6). **[svc; AT-14]**
29. **Entity separation.** All views and APIs are filtered by entity and permission (`access.scope`). A user sees an asset's `transferred_from` only with `fixed_assets.view` in both entities; otherwise "not visible" (en/ar). **[svc + access; AT-18]**
30. `entity` on assets, components, runs and events never changes. **[M guard; AT-22]**
31. Each new subsidiary gets categories and accounts from the group template as data (setup command); no code path names an entity. **[setup + code-reviewer; AT-25]**
32. Every label, message, event reason, journal description (en, Arabic in reason until ledger OQ-15) exists in English and Arabic. **[svc; AT-19]**
33. **Retention** (zatca R-38, D8). Each asset has `retention_until` = the later of (a) R-38 computed with its category's `retention_class` and `acquisition_date` (real estate: end of the tax period of acquisition + 15 years; movable: `acquisition_date` + Art. 52 adjustment period setting + 5 years, never earlier than the standard date), (b) the `retention_until` of its bills and disposal documents. Never shortened; no purge job. Hand-off: a disposal or intercompany sales document linked to an asset gets `retention_class` from the category (real estate even though the line is on 4800, not 1510) and, for movable assets, the explicit `movable_capital_asset` flag with `acquisition_date` (invoicing OQ-46, zatca OQ-31). Needs an invoicing change: the class is taken from the linked asset, not only from account 1510 (OQ-14). **[svc + invoicing; AT-10]**
34. **Zakat inputs.** `zakat_inputs(entity, as_of, *, user)` returns, per category and `asset_class`: cost, accumulated depreciation, impairment, NBV; CWIP separately; investment property separately; assets disposed in the year with proceeds. It never decides zakat treatment (deductibility of fixed assets and IP, land held for trade) **[CC-verify]**; `tax-returns.md` owns that. Tax depreciation (Income Tax Law Art. 17 groups **[CC-verify]**) for entities with non-Saudi shareholders: not built (OQ-15). **[svc; AT-16]**
35. Agents may draft assets from posted bills and draft runs (draft only, logged, gateway); never activate, post or approve. **[access; AT-24]**

## 5. Accounting

Existing: 1500, 1510, 1520, 1590, 5400, 1210, 1310, 2110, 2200, 1200, 2100. Taken codes checked (2026-10-17): `ledger/coa_template.py` (all codes incl. 1500–1600, 3400, 4500, 4700, 4710, 5750–5770, 5900); invoicing.md §5 (1330, 2220, 2310, 2320, 4120–4310, 5910, 5920); ledger-fx.md (3400, 4700, 4710, 5750, 5760, 5770, 1101); payroll.md (1230, 1240, 1241, 1250, 2330, 2340, 2350, 5120, 5130, 4610, 5930); banking.md (1101–1104 examples); consolidation.md (1610, 3600, 3700). **Proposed, CFO to approve (OQ-5; D7):**

| Code | name_en | name_ar [AR-review] | Type | Flags | Used for |
|---|---|---|---|---|---|
| 1530 | Furniture, fixtures and office equipment | الأثاث والتجهيزات والمعدات المكتبية | asset | control | Cost |
| 1540 | Computer and IT equipment | أجهزة الحاسب وتقنية المعلومات | asset | control | Cost |
| 1550 | Capital work in progress | مشروعات تحت التنفيذ | asset | control | Not depreciated |
| 1560 | Land (own use) | الأراضي (للاستخدام الخاص) | asset | control | Not depreciated |
| 1570 | Buildings (own use) | المباني (للاستخدام الخاص) | asset | control | Cost |
| 1591 | Accumulated depreciation – investment property | مجمع استهلاك العقارات الاستثمارية | asset (contra) | control | IP shown separately from PPE |
| 1595 | Accumulated impairment – property, plant and equipment | مجمع خسائر هبوط قيمة الممتلكات والآلات والمعدات | asset (contra) | control | FA6 |
| 1596 | Accumulated impairment – investment property | مجمع خسائر هبوط قيمة العقارات الاستثمارية | asset (contra) | control | FA6 |
| 4800 | Net gain (loss) on disposal of fixed assets | صافي أرباح (خسائر) استبعاد الأصول الثابتة | income | may carry debits | FA3–FA5, FA7 |
| 5420 | Impairment losses (reversals) on fixed assets | خسائر (عكس) هبوط قيمة الأصول الثابتة | expense | may carry credits | FA6 |
| 3500 | Revaluation surplus | فائض إعادة التقييم | equity | | Reserved; not used until OQ-8 |
| 4810 | Fair value gain (loss) on investment property | أرباح (خسائر) القيمة العادلة للعقارات الاستثمارية | income | may carry debits | Reserved; not used until OQ-6 |

Existing 1590 stays for accumulated depreciation of all PPE (1500, 1520, 1530–1570). Splitting 1590 by class: OQ-5.

**FA1. Acquisition** (posted by invoicing, E7; no entry here): Dr 1520 Vehicles 120,000.00 / Dr 1310 VAT input 18,000.00 (if recoverable, invoicing R29; passenger-car input VAT restrictions **[CC-verify]**) / Cr 2100 138,000.00. The register links the line (R2).

**FA2. Monthly depreciation run** (vehicle cost 120,000.00, residual 20,000.00, 60 months, available 2027-01-10; January 22/31 days)
| Account | Dr | Cr |
|---|---|---|
| 5400 Depreciation | 1,182.80 | |
| 1590 Accumulated depreciation | | 1,182.80 |
Full months: 1,666.67; IP buildings credit 1591. One line pair per (expense account, accumulated account); per-asset detail in `DepreciationLine`.

**FA3. Sale to a third party** (cost 120,000.00, accumulated 70,000.00, NBV 50,000.00; proceeds 55,000.00 + VAT 15% **[CC-verify]**)
Sales document (invoicing, after ZATCA): Dr 1200 63,250.00 / Cr 4800 55,000.00 / Cr 2200 8,250.00.
Disposal event:
| Account | Dr | Cr |
|---|---|---|
| 1590 Accumulated depreciation | 70,000.00 | |
| 4800 Net gain (loss) on disposal (proposed) | 50,000.00 | |
| 1520 Vehicles | | 120,000.00 |
Net 4800: credit 5,000.00 (gain).

**FA4. Write-off** (cost 8,000.00, accumulated 7,000.00): Dr 1590 7,000.00 / Dr 4800 1,000.00 / Cr 1540 8,000.00.

**FA5. CWIP completion**: Dr 1570 Buildings 2,000,000.00 / Cr 1550 CWIP 2,000,000.00 (`capitalise_cwip`).

**FA6. Impairment** (IP building carrying 900,000.00, recoverable 850,000.00): Dr 5420 50,000.00 / Cr 1596 50,000.00. Reversal: reverse sides, capped (R19).

**FA7. Intercompany sale** (ALM vehicle cost 100,000.00, accumulated 40,000.00, sold to SUB at 80,000.00 + VAT 12,000.00)
| Entity | Account | Dr | Cr | Counterparty |
|---|---|---|---|---|
| ALM | 1210 Due from group entities | 92,000.00 | | SUB |
| ALM | 4800 (proposed) | | 80,000.00 | |
| ALM | 2200 VAT output | | 12,000.00 | |
| ALM | 1590 / 4800 / 1520 (event) | 40,000.00 / 60,000.00 | 100,000.00 | |
| SUB (bill entered by SUB staff) | 1520 Vehicles | 80,000.00 | | |
| SUB | 1310 VAT input | 12,000.00 | | |
| SUB | 2110 Due to group entities | | 92,000.00 | ALM |
Consolidation eliminates the 20,000.00 gain and excess depreciation (consolidation CE4).

**No entry** for: categories, policies, draft assets, activation (cost already posted by the bill), locations, change of estimate (prospective), reconciliation and Zakat reports.

## 6. Integrations

| System | Direction | What |
|---|---|---|
| `invoicing` (internal) | In / Out | In: posted `PurchaseDocumentLine` (account, functional amount, date, vendor) → draft asset (post hook). Out: income type "fixed asset disposal" (4800) and `fixed_asset` reference on sales lines; retention class and `acquisition_date` for disposal documents (R33; invoicing change, OQ-14). |
| `ledger` (internal) | Out | `create_entry(post=True, grant, document=run/event)`; document types registered in `ledger/documents.py` with `build_lines` and `pending_documents`; `reverse_entry`. |
| `zatca` (internal) | Out (via invoicing) | `retention_class`, `acquisition_date` (zatca 3.x, R-38). |
| `billing` (internal) | In | Active `vehicle_rental` contracts by `vehicle_ref` (R27); `property_ref` for locations. |
| `payroll` (internal) | none / boundary | Bai' ajel assets stay on 1250 (R6, R28). |
| `consolidation` (internal) | Out | Intercompany sale pairs, buyer depreciation (consolidation R21). |
| `tax_returns` (internal) | Out | `zakat_inputs` (R34). |
| `reporting` (internal) | Out | Movement schedule per class (opening, additions, disposals, depreciation, impairment, closing) for PPE and IP notes. |
| ZATCA (external) | none direct | Through invoicing / zatca only. |
| Banks, GOSI, Mudad | none | |
| SAP B1 / Daftra | In (migration) | Opening register: cost, accumulated depreciation, acquisition date, remaining life per asset, tied out to the 31 Dec 2026 TB by migration-agent (`source=import`, component `opening_balance`). |

## 7. Permissions (proposed codenames; per entity)

| Action | Codename | accountant | approver (CFO) | viewer | managing_partner | Agent |
|---|---|---|---|---|---|---|
| Draft categories, policies | `fixed_assets.maintain_policy` | Y | | | | No |
| Approve policy (lives, rates) | `fixed_assets.approve_policy` | | Y (not creator) | | | No |
| Draft asset, link bill lines | `fixed_assets.create_asset` | Y | | | | Draft only, logged |
| Activate asset | `fixed_assets.activate_asset` | | Y (not creator) | | | No |
| Prepare / discard depreciation run | `fixed_assets.prepare_depreciation` | Y | | | | Draft only |
| Post run (grant `ledger.post_entry` for that run) | — | | Y (not preparer) | | | No |
| Draft events | `fixed_assets.record_event` | Y | | | | No |
| Post events (grant per event) | — | | Y (not creator/submitter) | | | No |
| Approve intercompany sale price | `fixed_assets.record_event` + approver | | Y (seller) | | | No |
| View register, reports, Zakat inputs | `fixed_assets.view` | Y | Y | Y | Y | Read if granted |

## 8. Acceptance tests

Fixtures: ALM (parent, SAR, Sharia enforced), SUB (100%, SAR), NSH (SAR, `sharia_enforced=False`), categories and **test-only** approved policies (vehicle 60 months, IT 36 months, IP building 480 months; fixture values, not policy), proposed accounts §5, 2027 periods, users A (accountant), A2 (accountant), C (approver), V (viewer). Amounts `Decimal`.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | Bill line Dr 1520 120,000.00 drafted by A | C posts the bill; A activates the draft asset; C activates; a vendor credit note reduces the line by 10,000.00 | Draft asset created with component 120,000.00; A refused (creator); C activates; credit note leaves a pending adjustment and the next run refuses until an `AssetEvent` is posted |
| AT-2 | FA2 vehicle | Runs Jan 2027 to the last month | Jan 1,182.80; full months 1,666.67; total = 100,000.00 exactly; last month absorbs residue |
| AT-3 | IT asset 10,000.00, reducing balance 40%/yr | Run Jan, Feb | 333.33, then 322.22; never below residual |
| AT-4 | Draft run for ALM 2027-01 prepared by A | A posts with own grant; C posts; C posts again; C reverses with grant | A refused; C posts FA2 lines; second refused; reversal posted and period re-runnable |
| AT-5 | 2027-01 closed; asset available 2027-01-10 activated in February | Prepare Feb run | Catch-up line for January days in February run; nothing dated January |
| AT-6 | Draft run for 2027-02; also active assets and no run for 2027-03 | `close_period` 2027-02; 2027-03 | Both refused (en/ar) naming the run / missing run |
| AT-7 | Draft run; then an asset is activated or an event posted | C posts the run | Refused "discard and re-prepare" (en/ar); nothing posted |
| AT-8 | Category "Furniture" without approved policy | Activate asset; prepare run including it | Both refused naming the category |
| AT-9 | FA3 asset | Draft sales document + disposal event; post before ZATCA clearance; after clearance; write-off FA4 | Event waits while the sales document is pending; after clearance FA3 posts, 4800 net credit 5,000.00, asset `disposed`; FA4 posts |
| AT-10 | IP building (1510) acquired 2027-03-10; movable vehicle acquired 2027-03-10; Art. 52 setting 5 years (test value) | Compute `retention_until`; draft disposal invoices for both | IP: 2042-03-31, invoice class `capital_asset_real_estate` though its line is on 4800; vehicle: 2037-03-10, invoice flagged `movable_capital_asset` with `acquisition_date` 2027-03-10; changing the setting lower never shortens the date |
| AT-11 | FA7 data | ALM issues invoice with event; SUB staff enter bill | ALM: 1210 with counterparty SUB, 4800 net credit 20,000.00; no record created in SUB automatically; SUB's bill creates a draft asset with `transferred_from` = ALM asset, `acquisition_date` = bill date; ALM-only user sees "not visible" for the origin in SUB |
| AT-12 | FA6 | Post impairment; then a reversal of 80,000.00 | Impairment 50,000.00 posted; depreciation recomputed on 850,000.00; reversal capped at the no-impairment carrying amount |
| AT-13 | IP asset; PPE asset | Post `fair_value_change`; post `revaluation` | Both refused in ALM and SUB (en/ar, citing OQ-6 / OQ-8) |
| AT-14 | Bill line on 1250 (bai' ajel) | Post bill; try to create an asset from it; try a bai' ajel sale of an active register asset | No draft asset; manual creation refused; bai' ajel sale refused |
| AT-15 | Vehicle linked by `vehicle_ref` to an active billing contract | Draft disposal; contract ends; draft again | First refused (en/ar, naming the contract); second allowed |
| AT-16 | ALM register at 2027-12-31 | `zakat_inputs(ALM, 2027-12-31)` | NBV per category = cost − accumulated depreciation − impairment; CWIP and IP separate; ties to GL accounts 15xx; V allowed, user without `fixed_assets.view` refused |
| AT-17 | Asset in ALM and in NSH | Add a cost component from a 5600 or 5610 journal line, or a journal Dr 1570 / Cr 2410 | Refused in both entities (Sharia message en/ar) |
| AT-18 | User of ALM only | List assets; link an SUB bill line to an ALM asset; set an SUB location on an ALM asset | Only ALM assets listed; cross-entity links refused |
| AT-19 | `ar` locale | Register screens, run errors, event reasons | Arabic shown |
| AT-20 | Land asset (1560) and IP land | Prepare run | No depreciation lines for them |
| AT-21 | Vehicle 60 months, after 12 months | `change_of_estimate` to 48 months total, effective 2028-01-01, approved | Remaining NBV spread over 36 months from January 2028; earlier runs untouched |
| AT-22 | Active asset | Change `entity`, cost or category directly; delete a disposed asset | Refused |
| AT-23 | Manual journal Dr 5400 / Cr 1590 | Post | Refused (control account); register-to-GL report shows zero difference after FA2 to FA7 |
| AT-24 | Agent service user | Draft asset from a posted bill; activate; post a run | Draft allowed and logged; activation and posting refused |
| AT-25 | New subsidiary NEW added as data, setup run | Bill to 1520, activate, run | Works with no code change; numbers prefixed `NEW` |
| AT-26 | USD bill line USD 10,000.000 at 3.7500 on 1540 | Post bill; period-end revaluation | Component 37,500.00 SAR; revaluation does not touch 1540 or the asset |

## 9. Open questions

**BLOCKING** = builder cannot build that part. **Default** = safe default used in the rules.

| # | Question | Owner | Status |
|---|---|---|---|
| OQ-1 | Capitalisation threshold (amount, per item or per invoice; group-wide or per entity). | CFO | Default: none (R3) |
| OQ-2 | Useful lives, reducing-balance rates and residual values per category; group-wide or per entity. | CFO | **BLOCKING** for activating real assets (R14); tests use fixture values only |
| OQ-3 | Depreciation convention: daily pro-rata vs full month (from in-service month or month after); disposal month. | CFO / accountant | Default: daily pro-rata both ends (R11, R25) |
| OQ-4 | Must `close_period` refuse a period with active assets and no posted run? | Accountant | Default: yes (R15) |
| OQ-5 | Approve proposed accounts 1530, 1540, 1550, 1560, 1570, 1591, 1595, 1596, 4800, 5420 (3500, 4810 reserved); split 1590 per class; separate gain (48xx) and loss (54xx) accounts; `is_control` on asset accounts. | CFO / accountant | Default: proposed codes in fixtures; template unchanged until approved |
| OQ-6 | Investment property model per entity: s16 fair value through P&L where reliably measurable without undue cost or effort vs cost model; IAS 40 choice for full-IFRS entities; valuer and frequency **[CC-verify]**. Group alignment (consolidation OQ-15). | Accountant / auditor | Default: cost model for all (R20) |
| OQ-7 | Impairment: indicator review frequency, cash-generating units for rental property, who supplies recoverable amounts. | Accountant | Default: annual reminder; manual recoverable amount with evidence |
| OQ-8 | Revaluation model for PPE: allowed for full-IFRS entities only, or also IFRS for SMEs (s17.15B **[CC-verify]**); which classes. | Accountant / auditor | Default: not built (R21) |
| OQ-9 | Full-IFRS entities with Islamic financing (murabaha/tawarruq on 2400/5600) for a qualifying asset: IAS 23 capitalisation vs Sharia rule "no interest capitalisation". **Needs Sharia board sign-off** and auditor view. | Managing Partner + Sharia board + auditor | Default: nothing capitalised (R7) |
| OQ-10 | VAT and real estate transaction tax on disposals and intercompany transfers of real estate; VAT on used vehicles; input VAT on passenger cars and the Art. 52 capital-asset adjustment when use changes (e.g. commercial → residential lease) **[CC-verify]**. **[Needs expert sign-off: tax]** | Tax adviser | Default: VAT category chosen by staff per invoicing; RETT not built; no Art. 52 adjustment computed |
| OQ-11 | Intercompany transfer price (NBV, market value, cost-plus; transfer pricing / Zakat view) and the buyer's useful life (new life vs remaining life). | Managing Partner + CFO | Default: price entered by staff, seller CFO approves; buyer's policy life |
| OQ-12 | May an existing register asset be sold to an employee on bai' ajel (payroll R47)? **Needs Sharia board sign-off.** | Managing Partner + Sharia board | Default: refused (R6) |
| OQ-13 | Billing `vehicle_ref` to become a FK to `FixedAsset` (billing change). | Dev + accountant | Default: free text; validated once billing adopts it |
| OQ-14 | Invoicing change: sales line `fixed_asset` reference, "fixed asset disposal" income type on 4800, retention class from the linked asset (R33). Retention of purchase bills for capital assets. | Dev + tax adviser | **BLOCKING** for disposals through invoicing (R22, R24, R33) |
| OQ-15 | Tax depreciation (Income Tax Law Art. 17) and deferred tax for entities with non-Saudi shareholders; Zakat treatment of NBV, IP and land (owned by tax-returns.md) **[CC-verify]**. | Tax adviser | Default: book NBV only (R34) |
| OQ-16 | Componentisation (e.g. building structure vs lifts, vehicle vs fit-out) and major overhauls (subsequent cost). | Accountant | Default: one asset per bill line; land and building separate; subsequent cost via event |
| OQ-17 | Leases as lessee (right-of-use assets, IFRS 16 / s20) in scope? | Accountant | Default: out of scope |
| OQ-18 | Physical verification (asset tags, counts) and custodian sign-off. | CFO | Default: not built; `custodian_ref` and location only |
| OQ-19 | Opening register from Daftra: per-asset detail available, or category totals only? | Accountant + migration-agent | Default: per asset; category-level opening assets allowed with `source=import` if detail missing |

**Blocking:** OQ-2 (lives, before real assets are activated), OQ-14 (invoicing changes for disposals and retention). **Sharia board:** OQ-9, OQ-12. **Tax sign-off:** OQ-10, OQ-15.
