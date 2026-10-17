# Spec: group consolidation (`consolidation`)

Status: draft for review · Written: 2026-10-17 · Author: spec-writer agent
Django app: `consolidation` (new). Reads: `core` (Entity), `ledger` (trial balance, intercompany balances, rates, translation), `billing` (intercompany reconciliation), `fixed_assets` (intercompany asset transfers). Feeds: `docs/specs/reporting.md` (consolidated statements; written in parallel).
**[CC-verify]** = standards or regulatory statement written from memory; the compliance-checker must check it against the official text before module-builder starts.

## 1. Purpose

Produces consolidated figures for **any group parent entity** (`Entity.consolidation = parent`; Almiras today, any other parent added as data) for a period, in the group presentation currency (SAR, ledger-fx OQ-7). The group structure (who owns what, from when, with control or significant influence) is stored as dated data. A consolidation run is a document that snapshots each member entity's trial balance, translates foreign-functional members into SAR (current-rate method, IAS 21 / IFRS for SMEs s30), aggregates fully consolidated subsidiaries, eliminates intercompany balances, income and expenses, unrealised profit and intra-group dividends, computes non-controlling interests (NCI), and applies the equity method to associates. Adjustments live in a separate **consolidation journal**; nothing is ever posted into an entity's ledger. Runs are prepared, reviewed and approved by different people. Consolidated statements are rendered by `reporting` from approved runs.

## 2. Decisions

| # | Date | Who | Decision |
|---|---|---|---|
| D1 | in repo (2026-10-16) | CLAUDE.md, README | Almiras is the parent; subsidiaries and minority stakes are `Entity` rows with ownership %, full / equity / none consolidation and IFRS for SMEs or full IFRS. New subsidiaries are data, never new code paths. Intercompany lines always carry a counterparty entity "for elimination". |
| D2 | 2026-10-16 | Project owner (invoicing D11, access A4) | Subsidiaries are separate companies; some are partly owned; minority holdings are separate taxpayers. No entity is in a VAT group; intercompany invoices are normal VAT invoices carrying the counterparty. |
| D3 | 2026-10-16 | Project owner (billing D10) | No automatic bill in the buyer entity for intercompany invoices; the buyer's staff enter it. (Differences in the reconciliation are therefore expected until bills are entered.) |
| D4 | 2026-10-16 | Caller brief, not a business decision (ledger-fx D4) | Group consolidation currency SAR, to be confirmed (ledger-fx OQ-7). |
| D5 | 2026-10-16 | Build decision (ledger-fx D6(e), R30) | Translation of foreign-functional entities is not built until ledger-fx OQ-5 is answered; `translate_trial_balance` raises for a foreign-functional entity. Account 3400 is non-postable in every entity (ledger-fx R46). |
| D6 | 2026-10-17 | Project owner (ledger-fx D10, invoicing D29, payroll P6) | Proposed account codes stay **proposed** until the accountant/CFO reviews them. |

Nothing else is decided. The tolerance for intercompany differences is pending with the owner (billing OQ-30, default 0). All other choices are in §9; defaults in rules are marked "default (OQ-n)".

## 3. Data model

Conventions: every model has `entity` FK → `core.Entity` (PROTECT, required) = the **group parent** that owns the record, unless stated; `created_by`, `created_at`, `updated_at`, `source` (`human` / `import` / `agent:<name>` / `system:<name>`). Money `DecimalField(18, 2)` in the stated currency; rates `DecimalField(20, 10)`; percentages `DecimalField(6, 3)`. "Bi" = `_en` and `_ar` fields, Arabic required.

### 3.1 `OwnershipInterest` (dated group structure)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Investor (direct holder) |
| investee | FK Entity | Y | | ≠ `entity`; same group tree |
| effective_from / effective_to | Date / Date null | Y / N | | Half-open; null = open-ended. No overlap per (entity, investee) (R2) |
| ownership_pct | Decimal(6,3) | Y | | > 0, ≤ 100 |
| voting_pct | Decimal(6,3) | Y | | Default = ownership_pct |
| relationship | Choice `subsidiary`, `associate`, `joint_venture`, `other_investment` | Y | | |
| control_basis | Text | Y | Bi | Why control / significant influence exists (IFRS 10 / s9; IAS 28 / s14 **[CC-verify]**) |
| method | Choice `full`, `equity`, `none` | Y | | Must be consistent with `relationship` (R3) |
| change_kind | Choice `formation`, `acquisition`, `increase`, `decrease`, `disposal`, `reclassification` | Y | | Drives R20 |
| consideration / consideration_currency | Money / FK Currency | N | | For acquisitions and changes in stake |
| evidence_ref | Char(200) | Y | | Shareholder register, SPA, CR extract |
| status | Choice `draft`, `approved` | Y | | Approved rows are immutable (R4) |
| approved_by / approved_at | FK User / DateTime | at approval | | Not the creator |

`Entity.parent`, `ownership_pct` and `consolidation` stay as the **current-state cache** of the approved row in force today, written only through `core.services.change_entity_settings` (logged, access R32). History lives here.

### 3.2 `ConsolidationRun` (document; unique `(entity, number)`)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Group parent (`consolidation=parent`) |
| number | Char(40) | Y | | `{code}-CON-{yyyy}-{000001}`, gap-free per parent and year |
| period_start / period_end | Date / Date | Y | | Default: financial-year start to a month end (year-to-date) |
| presentation_currency | FK Currency | Y | | `entity.presentation_currency`, default SAR |
| group_standard | Choice (Entity.Standard) | Y | | Default: parent's `reporting_standard` (OQ-15) |
| status | Choice `draft`, `prepared`, `reviewed`, `approved`, `superseded` | Y | | R10 |
| prepared_by/at, reviewed_by/at, approved_by/at | FK User / DateTime | per status | | Three different people (R11) |
| input_fingerprint | Char(64) | Y at `prepared` | | Hash of member TB snapshots, rates, structure rows, manual adjustments (R12) |
| supersedes | FK self, null | N | | A correction is a new run (R13) |
| notes | Text | N | Bi | |

### 3.3 `RunMember` (one row per entity in scope)

| Field | Type | Req | Notes |
|---|---|---|---|
| run | FK ConsolidationRun | Y | |
| member | FK Entity | Y | Includes the parent itself |
| method | Choice `parent`, `full`, `equity`, `none` | Y | From 3.1 in force (R5) |
| direct_pct / effective_pct / nci_pct | Decimal(6,3) | Y | effective = product along the chain (R6); nci = 100 − effective for `full` |
| included_from / included_to | Date | Y | Within the run window (R20) |
| functional_currency | FK Currency | Y | Snapshot of `base_currency` |
| closing_rate / average_rates | FK ExchangeRate / JSON {month: rate id} | Y if functional ≠ presentation | R15, R16 |
| tb_snapshot | JSON | Y for `parent`/`full` | Rows from `ledger.services.trial_balance(member, period_end, by_currency=True)` mapped to group codes (R8) |

### 3.4 `ConsolidationJournal` and `ConsolidationJournalLine` (separate from `ledger.JournalEntry`)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| run | FK ConsolidationRun | Y | | Same `entity` |
| number | Char(40) | Y | | `{code}-CJ-{yyyy}-{000001}` |
| kind | Choice `translation`, `investment_elimination`, `balance_elimination`, `income_expense_elimination`, `unrealised_profit`, `dividend_elimination`, `nci`, `equity_method`, `ownership_change`, `manual` | Y | | |
| generated | Bool | Y | | True = computed by the run; False = manual (R23) |
| description | Char(300) | Y | Bi | |
| evidence_ref | Char(200) | Y if manual | | |
| **Line:** group_account_code | Char(20) | Y | | Group template code (incl. `report_only` accounts) |
| **Line:** member / counterparty | FK Entity / FK Entity null | Y / N | | Member the adjustment belongs to; counterparty for eliminations |
| **Line:** debit / credit | Money (presentation currency) | one of | | Never both (R9) |
| **Line:** currency / txn_amount | FK Currency / Decimal(21,3) null | N | | Transaction currency of the eliminated pair, for audit |

### 3.5 `IntercompanyDifference`

| Field | Type | Notes |
|---|---|---|
| run, entity_a, entity_b, currency | FKs / Char(3) | Each unordered pair once (lower code = `entity_a`), per transaction currency (ledger-fx R29) |
| position_a / position_b / difference | Decimal(21,3) | From `intercompany_balances(..., by_currency=True)`; difference = a + b |
| billing_open / billing_unposted | Decimal(21,3) | From `billing.services.intercompany_reconciliation` (billing 3.12) |
| tolerance | Decimal(21,3) | Default 0 (OQ-4) |
| status | Choice `matched`, `adjusted`, `unexplained` | `adjusted` = cleared by a manual journal of the run (R19) |
| explanation | Text, Bi | Y if `adjusted` |

### 3.6 `AssociateResult` (equity-method input when the investee keeps no books in the ERP)

`entity` (investor), `investee`, `period_start`, `period_end`, `currency`, `profit_after_zakat_tax`, `oci`, `dividends_declared`, `net_assets`, `source_kind` (`audited`, `reviewed`, `management`), `evidence_ref`, `status` (`draft`, `approved`), `approved_by` (not creator). Default: if the investee has posted entries in the ERP for the window and the user can see it, its TB is used instead (OQ-9).

### 3.7 `UnrealisedProfitItem`

`entity` (group parent), `seller`, `buyer` (FK Entity), `source_kind` (`fixed_asset`, `other`), `fixed_asset` (FK `fixed_assets.FixedAsset` of the buyer, null), `sales_document` (FK invoicing.SalesDocument of the seller, null), `transfer_date`, `profit_at_transfer` (seller functional), `realisation_basis` (`depreciation`, `onward_sale`, `manual`), `status`. Remaining unrealised profit at a date is computed (R21), not stored.

### 3.8 `HistoricalEquityRate` (only after OQ-2)

`entity` (member), `group_account_code`, `date`, `rate`, `source_ref`, `approved_by`. Rates for share capital and other equity at the transaction date (R16).

## 4. Rules

### Structure
1. A run uses only `approved` `OwnershipInterest` rows; `Entity.consolidation` alone never drives a run. **[svc; AT-1]**
2. Rows for the same (investor, investee) never overlap in time; the investee's rows, all investors combined, never exceed 100% on any date. **[M clean + svc; AT-1]**
3. `subsidiary` → `full`; `associate` / `joint_venture` → `equity`; `other_investment` → `none`. Another combination is refused (en/ar). Control assessment is recorded in `control_basis`, not computed. **[M clean; AT-1]**
4. Approved structure rows are never edited or deleted; a change is a new row closing the previous one (`effective_to`). Approval by someone other than the creator, with `consolidation.approve_structure` (default holder `managing_partner`, OQ-10). Approval updates the Entity cache through `change_entity_settings` in the same transaction. **[svc + access; AT-1]**
5. A member's method in a run is the row in force on `period_end`; members whose method changed inside the window follow R20. **[svc; AT-19]**
6. Effective ownership = product of `ownership_pct` along the chain from the parent; NCI % of a `full` member = 100 − effective %. Multi-tier groups use this one-step approach (default, OQ-21). **[svc; AT-8]**
7. Adding a subsidiary, associate or new parent needs only data (Entity row, chart, periods, structure row); no code path names an entity. **[code-reviewer; AT-21]**

### Inputs and mapping
8. Member balances are read only through `ledger.services.trial_balance(..., by_currency=True)` and `intercompany_balances(..., by_currency=True)`. Each member account maps to a group template code: same code, else the nearest ancestor (`Account.parent`) with a template code. An unmapped account with a non-zero balance stops the run, naming member and account (en/ar). **[svc; AT-24]**
9. Every consolidation journal balances in presentation currency and has 2+ lines; each line is debit or credit, never both. **[M clean + svc; AT-12]**

### Run lifecycle (never posts to entity books)
10. Status moves only `draft → prepared → reviewed → approved`, or `approved → superseded` when a later run with `supersedes` is approved. A `prepared` or `reviewed` run returned for rework goes back to `draft` and loses its sign-offs. Status changes only through services (`prepare_run`, `review_run`, `approve_run`, `return_run`). **[svc + M guard; AT-13, AT-22]**
11. Preparer, reviewer and approver are three different users; each needs its right on the group parent and `ledger.view` on **every** member in scope (no partial consolidation: a member the user cannot see stops the action, naming the member, never its figures). **[svc + access; AT-13, AT-18]**
12. `review_run` and `approve_run` recompute the inputs and refuse if `input_fingerprint` changed (backdated posting, new rate, structure change, edited manual journal): "inputs changed; return and re-prepare" (en/ar). **[svc; AT-14]**
13. Approved runs, their members, journals and differences are immutable. Corrections are a new run that supersedes the old one. **[M guard + svc; AT-22]**
14. A consolidation run never creates, edits or posts a `ledger.JournalEntry` in any entity, and is not registered in `ledger/documents.py`. Approval requires every member period in the window to be `closed` (default, OQ-11). **[svc + code-reviewer; AT-12, AT-15]**

### Translation (target method, IAS 21 / s30 current-rate method **[CC-verify]**; blocked by OQ-2)
15. For a member whose functional currency ≠ presentation currency: assets and liabilities at the closing `spot` rate on `period_end`; income and expenses at the `average` rate of each month applied to that month's movements (or transaction-date rates, OQ-2); equity at historical rates (3.8); opening retained earnings carried from the previous approved run; the difference is booked by a `translation` journal to 3400 (OCI), with the NCI share to 3600. **[svc; AT-11]**
16. Rates come from `ledger.ExchangeRate` global group rates (not entity overrides) by default (OQ-3). A missing closing, average or historical rate stops the run (en/ar, naming pair and date). **[svc; AT-11]**
17. Until OQ-2 (= ledger-fx OQ-5) is answered, a run with any `full` or `equity` member whose functional currency ≠ presentation currency is refused at `prepare_run` (en/ar, citing OQ-5). SAR-only groups consolidate. **[svc; AT-11]**

### Eliminations
18. Balance and income/expense eliminations apply only between members consolidated `parent`/`full`. Balances and transactions with `equity` and `none` members are not eliminated; they stay as related-party balances (default, OQ-7; invoicing OQ-38). **[svc; AT-23]**
19. Intercompany balances are compared per (pair, transaction currency) using `intercompany_balances(..., by_currency=True)` and billing's reconciliation columns. Each mirrored amount is eliminated (1210/1220 against 2110, by counterparty). A difference above tolerance (default 0, OQ-4) is `unexplained` and blocks `prepare_run` until a manual journal of the run (e.g. unbooked buyer bill, D3) clears it with evidence and explanation (status `adjusted`). A SAR residual from translating a pair that mirrors exactly in transaction currency is not a difference; it goes to 3400 (default, OQ-13). **[svc; AT-3, AT-4]**
20. Intercompany income and expense (e.g. 4110, 4210, 4310, 4220 to 4240 against the buyer's expense or asset account) are eliminated by pair and amount, matched through the invoicing documents carrying the counterparty. A mismatch follows R19. Intercompany sales capitalised by the buyer (fixed assets) are eliminated through R21, not as expense. **[svc; AT-5]**
21. Unrealised profit on intra-group transfers of assets (fixed_assets transfer, R-FA intercompany) is eliminated in full from the carrying amount and the seller's gain (4800 proposed in fixed-assets.md), and released as the buyer depreciates (excess depreciation) or sells onward. Upstream profit (seller = partly-owned subsidiary) is shared with NCI by `nci_pct`. Downstream to associates: investor's share only **[CC-verify]**. Inventory: no inventory module, `manual` items only (OQ-7). **[svc; AT-6]**
22. Intra-group dividends: the parent's 4400 income from a `full` member is eliminated against that member's distribution (3300); the NCI share reduces 3600. Dividends from `equity` members reduce 1410 (R25). **[svc; AT-7, AT-9]**
23. Manual consolidation journals (kind `manual`) need `evidence_ref` and bilingual description, are made by the preparer, and are reviewed with the run. They cannot use Sharia-prohibited accounts (R28). **[svc; AT-4, AT-16]**

### Investment elimination, NCI, ownership changes
24. For each `full` member: the parent's investment (1400) is eliminated against the member's equity at acquisition. For members created by the group (`change_kind=formation`), investment must equal the contributed share capital × direct %; any difference stops the run (no goodwill without an approved acquisition record, OQ-5). NCI at each `period_end` = `nci_pct` × member net assets after the run's adjustments that belong to that member (incl. upstream unrealised profit); NCI share of profit and of OCI is reported by `reporting`. Negative NCI is allowed **[CC-verify]**. **[svc; AT-8]**
25. Equity method (`equity` members): cumulative share since acquisition of profit after zakat/tax and of OCI × `effective_pct` is added to 1410: prior years to 3300, current window to 4500 (share of profit of associates) and 3400 (share of translation OCI); dividends received (in the investor's books on 4400) are reclassified to reduce 1410. Losses stop at a carrying amount of zero (default, OQ-18) **[CC-verify]**. Source: investee TB if available, else an approved `AssociateResult` (OQ-9). The investor's own books keep the investment at cost (default, OQ-8). **[svc; AT-9]**
26. `none` members are not aggregated; the investor's 1420 balance stays as in its books. **[svc; AT-10]**
27. Mid-window events: a change in ownership **without** change of control or method (e.g. 80% → 90%) effective on the first day of a month is built: NCI is re-measured and the difference goes to 3700 (equity, IFRS 10.23 / s22.19 **[CC-verify]**). Acquisition (gain of control), loss of control, change of method (e.g. associate → subsidiary) or any change not effective on a month's first day, inside the window: `prepare_run` is refused until OQ-5 is decided. **[svc; AT-19, AT-25]**

### Sharia, bilingual, entity separation
28. No interest elimination entries should arise: intercompany financing is profit-free (qard) or Sharia-compliant. Consolidation journal lines on Sharia-prohibited accounts (2410, 5610, any `prohibited` code) are refused for every group. If a `full` member's TB has a balance on a prohibited account with a group counterparty, `prepare_run` stops with a Sharia exception (en/ar) for the Managing Partner; it is never netted away. Profit on Sharia-compliant intra-group financing (e.g. 4600 / 5600, 4610) is eliminated like other income/expense. **[svc; AT-16]**
29. Purification is never eliminated: 4900 and 2600 balances stay as booked by each entity (the obligation is to charity, not to a group entity). The run lists members with `sharia_enforced=False` and members with 4900 balances. **[svc; AT-17]**
30. Every label, message, journal description and export exists in English and Arabic. **[svc + templates; AT-20]**
31. Runs, journals and differences are visible only to users with `consolidation.view` on the group parent; member drill-down only where the user holds `ledger.view` on that member (access R2, R6). Consolidated totals alone may be shown to `group_viewer`. **[svc + `access.scope`; AT-18]**
32. Agents may read approved runs through the gateway only if granted `consolidation.view`; they cannot prepare, review, approve, or create journals (default, OQ-22). **[access; AT-18]**

## 5. Accounting

Consolidation journals only (presentation currency SAR, group template codes). **No ledger entry in any entity.** Proposed group-only accounts (**PROPOSED**, CFO to approve, OQ-6; `report_only`, never postable in an entity ledger, like 3400):

| Code | name_en | name_ar [AR-review] | Type | Used by |
|---|---|---|---|---|
| 1610 | Goodwill | الشهرة | asset | Acquisitions (OQ-5) |
| 3600 | Non-controlling interests | الحصص غير المسيطرة | equity | CE1, CE5, CE6 |
| 3700 | Reserve – transactions with non-controlling interests | احتياطي المعاملات مع الحصص غير المسيطرة | equity | CE8 |

Free-code check (2026-10-17): 1610, 3600, 3700 are not in `ledger/coa_template.py`, nor proposed in invoicing.md §5, ledger-fx.md §5.0, payroll.md §5, banking.md or fixed-assets.md §5.

Fixture group: ALM parent (SAR); SUB 100% full; PRT 80% full (NCI 20%); ASC 30% equity; all SAR.

**CE1. Investment elimination** (PRT formed with capital 1,000,000.00; ALM paid 800,000.00)
| Account | Member | Dr | Cr |
|---|---|---|---|
| 3100 Share capital | PRT | 1,000,000.00 | |
| 1400 Investments in subsidiaries | ALM | | 800,000.00 |
| 3600 NCI (proposed) | PRT | | 200,000.00 |

**CE2. Intercompany balance** (ALM 1210 +11,500.00 with SUB; SUB 2110 −11,500.00 with ALM)
| Account | Member / counterparty | Dr | Cr |
|---|---|---|---|
| 2110 Due to group entities | SUB / ALM | 11,500.00 | |
| 1210 Due from group entities | ALM / SUB | | 11,500.00 |
Loans (1220 vs 2110) the same way.

**CE3. Intercompany income / expense** (ALM rent to SUB 10,000.00 net)
| Account | Member | Dr | Cr |
|---|---|---|---|
| 4110 Intercompany rental income | ALM | 10,000.00 | |
| 5200 Rent | SUB | | 10,000.00 |

**CE4. Unrealised profit on asset transfer** (ALM sells a vehicle, NBV 60,000.00, to SUB at 80,000.00; SUB depreciates over 48 months; at 12 months)
| Account | Member | Dr | Cr |
|---|---|---|---|
| 4800 Net gain on disposal of fixed assets (proposed, fixed-assets.md) | ALM | 20,000.00 | |
| 1520 Vehicles | SUB | | 20,000.00 |
| 1590 Accumulated depreciation | SUB | 5,000.00 | |
| 5400 Depreciation | SUB | | 5,000.00 |
In later years the opening part goes to 3300 instead of 4800 / 5400. Upstream (seller PRT): NCI share 20% → Dr 3600 / Cr 3300 or P&L attribution (R21).

**CE5. Intra-group dividend** (PRT pays 100,000.00; ALM books 80,000.00 on 4400)
| Account | Member | Dr | Cr |
|---|---|---|---|
| 4400 Dividend income | ALM | 80,000.00 | |
| 3600 NCI (proposed) | PRT | 20,000.00 | |
| 3300 Retained earnings | PRT | | 100,000.00 |

**CE6. NCI share of profit** (PRT profit 50,000.00): no line inside the current window; `reporting` shows profit attributable to NCI 10,000.00 and to owners of the parent. Cumulative prior-year NCI shares are carried by the run as Dr 3300 / Cr 3600 (member PRT).

**CE7. Equity method** (ASC profit 200,000.00; dividend declared 30,000.00; ALM received 9,000.00 on 4400)
| Account | Member | Dr | Cr |
|---|---|---|---|
| 1410 Investments in associates | ALM | 60,000.00 | |
| 4500 Share of profit of associates | ALM | | 60,000.00 |
| 4400 Dividend income | ALM | 9,000.00 | |
| 1410 Investments in associates | ALM | | 9,000.00 |

**CE8. Increase in stake without change of control** (PRT 80% → 90%; ALM pays 130,000.00; PRT net assets 1,200,000.00 → NCI carrying 240,000.00, transferred 120,000.00)
| Account | Member | Dr | Cr |
|---|---|---|---|
| 3600 NCI (proposed) | PRT | 120,000.00 | |
| 3700 Reserve – transactions with NCI (proposed) | ALM | 10,000.00 | |
| 1400 Investments in subsidiaries | ALM | | 130,000.00 |

**CE9. Translation** (blocked, R17): Dr/Cr 3400 for the translation difference; NCI share Dr/Cr 3600.

## 6. Integrations

| System | Direction | What |
|---|---|---|
| `ledger` (internal) | In | `trial_balance(by_currency=True)`, `intercompany_balances(by_currency=True)`, `ExchangeRate` (spot closing, average), `translate_trial_balance` (OQ-2). Read-only. |
| `billing` (internal) | In | `intercompany_reconciliation(as_of, user=, group=)` columns `billing_open`, `billing_unposted` (billing R64, 3.12). |
| `invoicing` (internal) | In | Intercompany sales documents and vendor bills (counterparty) for R20 matching and drill-down. |
| `fixed_assets` (internal) | In | Intercompany transfers (seller asset, buyer asset, gain, buyer depreciation) for R21. |
| `reporting` (internal) | Out | `consolidated_trial_balance(run, *, user)` → rows (group code, amount, NCI attribution, member drill-down per R31); `reporting` renders consolidated statements from **approved** runs (drafts watermarked). Entity statements → consolidation hand-off: account-to-statement-line mapping owned by `reporting`. |
| `tax_returns` (internal) | none | Zakat is per entity; any consolidated zakat filing for wholly owned subsidiaries **[CC-verify]** is for `docs/specs/tax-returns.md`. |
| ZATCA, banks, GOSI, Mudad | none | Consolidation is not a tax, payment or labour filing. |
| SAP B1 / Daftra | none | Opening structure and historical equity rates may be loaded by migration-agent (OQ-12). |

## 7. Permissions (proposed codenames; roles per access.md §3.1)

| Action | Codename | Default holder (OQ-10) | Scope | Agent |
|---|---|---|---|---|
| Draft structure row | `consolidation.maintain_structure` | accountant (group parent) | group parent | No |
| Approve structure row | `consolidation.approve_structure` | managing_partner | group | No |
| Prepare run, manual journals, associate results | `consolidation.prepare_run` | accountant (group parent) + `ledger.view` on every member | group parent | No |
| Review run | `consolidation.review_run` | approver (CFO) of the group parent; not preparer | group parent | No |
| Approve run | `consolidation.approve_run` | managing_partner; not preparer or reviewer | group | No |
| Approve `AssociateResult` | `consolidation.approve_structure` | managing_partner | group | No |
| View runs and journals | `consolidation.view` | accountant, approver, viewer (group parent), managing_partner | group parent; drill-down per member `ledger.view` | Read only if granted |
| View consolidated totals | `consolidation.view_totals` | group_viewer | group | Read only if granted |

## 8. Acceptance tests

Fixtures: ALM (parent, SAR, Sharia enforced), SUB (100% full, SAR), PRT (80% full, SAR), ASC (30% equity, SAR, own ledger), MIN (5% none), EGY (100% full, EGP), 2027 monthly periods, template chart incl. proposed 1610/3600/3700 and fixed-assets 4800, users A (accountant), C (approver), M (managing partner), V (group_viewer). All amounts `Decimal`.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | PRT row 80% from 2027-01-01 approved | A drafts 90% from 2027-07-01; A approves it; M approves; A drafts a second overlapping 80% row; A drafts ASC `associate` with method `full`; A edits the approved row | A's approval refused (creator); M's approval closes the old row at 2027-06-30 and updates `Entity.ownership_pct` with an AccessLog row; overlap, method mismatch and edit refused (en/ar) |
| AT-2 | SAR-only group, no intercompany | Prepare run 2027-01-01..2027-03-31 | Consolidated TB = sum of ALM, SUB, PRT TBs by group code; ASC, MIN not aggregated |
| AT-3 | ALM invoiced SUB 11,500.00 SAR; SUB booked the bill | Prepare | Difference row `matched`; CE2 generated; pair's 1210/2110 net 0 in consolidated TB |
| AT-4 | Plus USD 1,150.00 invoice not booked by SUB | Prepare; then A adds a manual journal with evidence and en/ar explanation; prepare again | First refused listing pair, USD, 1,150.000, `billing_open` 1,150.00; second succeeds, status `adjusted` |
| AT-5 | CE3 data | Prepare | CE3 generated; consolidated 4110 and SUB 5200 reduced by 10,000.00; mismatched amount 9,000.00 vs 10,000.00 → treated as AT-4 |
| AT-6 | CE4 transfer on 2027-01-01 | Run to 2027-12-31; run to 2028-12-31 | 2027: CE4 as shown; 2028: opening 15,000.00 against 3300, current-year release 5,000.00; upstream variant from PRT allocates 20% to NCI |
| AT-7 | CE5 data | Prepare | CE5 generated; consolidated 4400 excludes 80,000.00 |
| AT-8 | PRT net assets 1,250,000.00 incl. current profit 50,000.00 | Prepare | NCI total (3600 + current-year attribution) = 250,000.00; NCI share of profit 10,000.00 (CE6); SUB has NCI 0 |
| AT-9 | ASC profit 200,000.00, dividend 30,000.00 | Prepare; repeat with ASC having no ERP ledger and an approved `AssociateResult`; with only a draft one | CE7 generated both ways; draft-only refused naming ASC |
| AT-10 | MIN 5% at 1420 50,000.00 | Prepare | MIN not aggregated; 1420 50,000.00 unchanged |
| AT-11 | EGY (EGP) in scope | Prepare | Refused citing ledger-fx OQ-5 (en/ar). *Deferred until OQ-2:* with closing/average/historical rates, translated TB balances and the difference sits on 3400 (NCI share 3600); a missing average rate stops the run naming EGP→SAR and month |
| AT-12 | Any run | Prepare, review, approve | Count and content of `ledger.JournalEntry` in every entity unchanged; every consolidation journal balances; a journal line with both debit and credit refused |
| AT-13 | Run prepared by A | A reviews; C reviews; C approves; M approves | A refused (preparer); C reviews; C's approval refused (reviewer); M approves; status `approved` |
| AT-14 | Run `reviewed` | A backdated entry is posted in SUB (period open in test) or a closing rate is added; M approves | Refused "inputs changed" (en/ar); status unchanged |
| AT-15 | SUB 2027-03 period open | M approves run to 2027-03-31 | Refused naming SUB and period (OQ-11 default) |
| AT-16 | SUB with `sharia_enforced=False` has 5610 with counterparty ALM; separately A adds a manual line on 2410 | Prepare; save manual journal | Prepare stops with Sharia exception (en/ar); manual line refused in every group; no interest elimination created |
| AT-17 | ALM 4900 3,000.00 / 2600 3,000.00 from a group counterparty | Prepare | Not eliminated; members with 4900 balances listed |
| AT-18 | User U holds accountant on ALM (group) but no membership on ASC; V group_viewer | U prepares; V opens run; agent service user prepares | U refused naming ASC, no ASC figure shown; V sees consolidated totals, member drill-down refused; agent refused |
| AT-19 | PRT acquired (gain of control) on 2027-05-15, or ASC becomes subsidiary on 2027-06-01 | Prepare YTD 2027-06 | Refused citing OQ-5 (en/ar) |
| AT-20 | `ar` locale | Run screens, errors, export | Arabic labels and messages |
| AT-21 | New subsidiary NEW (100%, SAR) added as data only | Prepare next run | NEW included with no code change |
| AT-22 | Run R1 approved | Edit a journal line; delete R1; approve R2 with `supersedes=R1` | Edit and delete refused; R1 `superseded`, kept |
| AT-23 | ALM 1210 20,000.00 with ASC | Prepare | Not eliminated; stays in consolidated 1210 as related-party balance |
| AT-24 | SUB has account 1599 with balance, no template ancestor | Prepare | Refused naming SUB and 1599 |
| AT-25 | CE8 data effective 2027-07-01 | Prepare YTD 2027-12 | CE8 generated; NCI % 20 before July and 10 after for profit attribution |

## 9. Open questions

**BLOCKING** = builder cannot build that part. **Default** = safe default used in the rules.

| # | Question | Owner | Status |
|---|---|---|---|
| OQ-1 | Control and significant-influence criteria to record (voting rights, board seats, agreements); evidence required. | Accountant / auditor | Default: free text + evidence ref, approved by MP |
| OQ-2 | Translation method details = ledger-fx OQ-5: monthly vs period average or transaction-date rates; historical equity rates source; opening balances for the first run; goodwill and fair-value adjustments of foreign members; recycling of 3400 on disposal (IAS 21 / s30 **[CC-verify]**); EGP hyperinflation (IAS 29 / s31 **[CC-verify]**). | Accountant / auditor | **BLOCKING** for foreign-functional members (R17). SAR-only runs not blocked |
| OQ-3 | Rate sources for translation (ledger-fx OQ-2, OQ-4): global group rates only, or the member's entity overrides (e.g. CBE for EGY)? | Accountant | Default: global group rates |
| OQ-4 | Tolerance for intercompany differences, and per-account split (1210 vs 2110) (= billing OQ-30, pending with owner). | Managing Partner + CFO | Default: 0; net positions per pair and currency |
| OQ-5 | Acquisitions, loss of control, method changes and changes not on a month start: purchase price allocation, goodwill (1610), NCI measurement (proportionate vs fair value; full IFRS option **[CC-verify]**), bargain purchase, inclusion from the exact date, disposal gain/loss, 3400 recycling. | Accountant / auditor | **BLOCKING** for runs containing such events (R27) |
| OQ-6 | Approve proposed group-only accounts 1610, 3600, 3700 (`report_only`). | CFO / accountant | Default: used in fixtures; template unchanged until approved |
| OQ-7 | Unrealised profit: inventory (no inventory module), service costs capitalised by buyers, downstream sales to associates, NCI share of upstream profit; eliminate transactions with associates at all? (invoicing OQ-38) | Accountant | Default: fixed assets only automated; others manual; associates not eliminated except R21 share |
| OQ-8 | Investor's separate statements: investments in subsidiaries and associates at cost, fair value or equity method (s9.26, s14.4 **[CC-verify]**). | Accountant / auditor | Default: cost in entity books; equity method only in consolidation |
| OQ-9 | Associates without ERP books: data source (audited / management accounts), reporting-date lag allowed, who approves. | Accountant | Default: approved `AssociateResult` for the same period end; MP approves |
| OQ-10 | Who drafts and approves structure changes, prepares, reviews and approves runs. | Managing Partner | Default: accountant / CFO / MP as §7 |
| OQ-11 | May a run be approved while a member period is open (e.g. monthly management consolidation)? | CFO | Default: approval needs closed periods; drafts allowed on open periods |
| OQ-12 | Year-end close in the ledger: no closing entry is specified; snapshot treats income/expense before `period_start` as opening 3300. Opening consolidated equity for the first run (Daftra history, pre-acquisition reserves). | Accountant + migration-agent | Default: as stated; first run needs a migrated opening consolidation journal |
| OQ-13 | SAR residual from translating mirrored foreign-currency pairs: 3400, or consolidated FX gain/loss (4710 / 5760)? | Accountant | Default: 3400 |
| OQ-14 | Group presentation currency SAR (ledger-fx OQ-7). | Managing Partner | Default SAR |
| OQ-15 | Mixed standards (a full-IFRS member in an IFRS-for-SMEs group or the reverse) and accounting-policy alignment (e.g. investment property cost vs fair value, fixed-assets OQ-6). | Accountant / auditor | Default: group standard = parent's; alignment by manual journals |
| OQ-16 | Members with a different financial-year end. | Accountant | Default: all members same year end; otherwise refused |
| OQ-17 | Consolidated cash flow statement: built from consolidated movements or from member cash flows with eliminations (with `reporting.md`). | Accountant | Default: deferred to reporting.md |
| OQ-18 | Equity-method losses beyond zero, long-term interests, impairment of the investment (s14.8 / IAS 28 **[CC-verify]**). | Accountant | Default: stop at zero; impairment manual |
| OQ-19 | `Entity.ownership_pct` is not in the logged sensitive-settings list (access R32, A13(c)). Add it? | Dev + Managing Partner | Default: changed only by R4 approval through `change_entity_settings` |
| OQ-20 | Sharia: is any intra-group arrangement (e.g. a non-Sharia member lending to the group with interest) acceptable in the group at all, and how is it reported? **Needs Sharia board sign-off.** | Managing Partner + Sharia board | Default: run stops (R28) |
| OQ-21 | Sub-groups with intermediate parents: one-step (effective %) or step-by-step consolidation; sub-group consolidated statements. | Accountant | Default: one-step at the top parent |
| OQ-22 | Agent access to consolidation (read approved runs only?). | Managing Partner | Default: no rights unless granted; never prepare/approve |

**Blocking:** OQ-2 (translation of foreign-functional members), OQ-5 (acquisitions, disposals, method changes inside a run window). **Owner pending:** OQ-4 (tolerance, default 0). **Sharia board:** OQ-20.
