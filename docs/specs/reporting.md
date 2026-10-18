# Reporting: trial balance, financial statements, year-end close and the consolidation hand-off

Django app: `reporting`. Status: **draft spec**, written 2026-10-17. Not built.
Depends on: `ledger.services` (`trial_balance`, `_trial_balance`, `intercompany_balances`, `close_period`, `create_entry(post=True, grant, document)`, `purification_amount`, `fx_exceptions`), `ledger/documents.py` registry, `docs/specs/ledger-fx.md` (functional currency, revaluation, translation R30 / OQ-5), `docs/specs/access.md`, `docs/specs/tax-returns.md` (Zakat provision, tax notes data), `docs/specs/payroll.md` (EOSB sub-ledger), `docs/specs/invoicing.md` (income types, purification). Group consolidation is **`docs/specs/consolidation.md`** (written in parallel); this spec ends at the entity reporting package (§3.8, R32 to R35).

**Tags.** **[CC-verify]** = accounting-framework or legal requirement the compliance-checker (and the accountant/auditor) must confirm before build; the value shown is the default. **[AR-review]** = Arabic label to be checked. No framework or law text was downloaded for this spec (list in OQ-2).

## 1. Purpose

The `reporting` module produces, for **every entity** (Almiras, subsidiaries, associates and minority entities that keep books here), the trial balance and the primary financial statements in the entity's functional currency under the entity's framework (`Entity.reporting_standard`: IFRS for SMEs or IFRS as endorsed in KSA): statement of financial position, statement of profit or loss and other comprehensive income, statement of changes in equity, statement of cash flows (indirect method) and the data for the notes, with comparatives. Statement layouts are **data** (report definitions mapping accounts to lines, bilingual labels), so a new entity or framework is a data change. Every figure drills down to journal entries. It runs the year-end close (closing entries to retained earnings 3300, optional statutory reserve transfer to 3200), exports to CSV, XLSX and PDF, offers management reports (budget vs actual: placeholder), and issues the per-entity **reporting package** that `consolidation` consumes. It never combines entities.

## 2. Decisions

| # | Decision | Source | Date |
|---|---|---|---|
| RD1 | Every business record has an `entity` FK; queries filtered by entity and permission; new subsidiaries are data; every text en/ar; money `Decimal`; posted entries never edited; closed periods reject postings; Sharia per entity. Open source only. | CLAUDE.md | in repo |
| RD2 | Each entity has a reporting standard: IFRS for SMEs (default) or IFRS as endorsed in KSA; a consolidation method (parent / full / equity / none) and an ownership %. | README; `core/models.py` (`Entity.Standard`, `Consolidation`) | in repo |
| RD3 | The chart template is an IFRS for SMEs chart for a Saudi holding company, a draft for the accountant's review; proposed codes stay proposed until the accountant/CFO review. | `ledger/coa_template.py`; project owner (invoicing D18, D29; ledger-fx D10) | 2026-10-16 / 2026-10-17 |
| RD4 | Monthly fiscal periods per entity; reports so far: trial balance and intercompany positions by entity pair. | README | in repo |
| RD5 | Debit/credit are in the functional currency; group presentation currency SAR (to be confirmed, ledger-fx OQ-7); translation is a report, not a posting, and is not built until ledger-fx OQ-5 is answered; 3400 is non-postable, report use only. | ledger-fx D4 (caller brief), D6, R30, R46 | 2026-10-16 |
| RD6 | Every subsidiary and partly owned entity is its own company; intercompany balances carry the counterparty; no mirror bills; consolidation treatment of associates belongs to the consolidation spec. | Project owner (invoicing D11, D23); invoicing OQ-38 | 2026-10-16 |
| RD7 | Roles are per entity; group roles (`managing_partner`, `group_viewer`) cover full descendants only by default; partly owned and minority entities need explicit membership. | access §3.1, OQ-3 default | 2026-10-16 |

**Task brief (caller, 2026-10-17; not a business decision):** the statements and features listed in §1; closing to 3300 and statutory reserve transfer to 3200 subject to verification; exports CSV/XLSX through open-source libraries and PDF via HTML; budget vs actual as a placeholder; consolidation as a separate spec with a defined hand-off.

## 3. Data model

"Bi" = `_en` and `_ar`, both required. Money = `Decimal(18,2)` in the entity's functional currency. *Global* = no entity FK (shared definitions, like `VatRate`).

### 3.1 ReportingSettings (one per entity; changed through `reporting.services.change_settings`, approver, logged)
| Field | Type | Req | Notes |
|---|---|---|---|
| entity | O2O Entity | Y | |
| fiscal_year_end | Char(5) `MM-DD` | Y | Default `12-31`; shared with tax-returns (its OQ-3) |
| pl_presentation | Choice `nature` / `function` | Y | Default `nature` (chart is by nature) **[CC-verify]** both allowed (OQ-3) |
| statutory_reserve_pct | Decimal(5,2) | Y | Default 0.00: no transfer (R25, OQ-6) |
| statutory_reserve_cap_pct_of_capital | Decimal(5,2), null | N | Stop when 3200 reaches this % of 3100 |
| statutory_reserve_basis | Char(300) | Y if pct > 0 | Bi; article of the bylaws or law |
| framework_edition | Choice `sme_2015`, `sme_2025`, `ifrs_ias1`, `ifrs_18` | Y | Edition of the standard applied for the year (OQ-4) **[CC-verify]** |
| rounding_display | Choice `units`, `thousands` | Y | Display only; stored figures stay 2 dp |

### 3.2 ReportDefinition (*global*)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| code | Char(40) | Y | | e.g. `SOFP-SME`, `PL-SME-NATURE`, `PL-IFRS18` |
| statement | Choice `tb`, `sofp`, `pl_oci`, `soce`, `cash_flow`, `notes`, `management` | Y | | |
| framework | Choice `ifrs_sme`, `ifrs_full`, `any` | Y | | Matches `Entity.reporting_standard` (R3) |
| edition | Choice as `framework_edition`, or `any` | Y | | |
| version | SmallInt | Y | | Unique `(code, version)` |
| valid_from / valid_to | Date / Date null | Y / N | | Period start dates it applies to |
| name | Char(200) | Y | Bi | Statement title |
| status | Choice `draft`, `approved`, `retired` | Y | | Approved versions immutable (R6) |
| created_by / approved_by / approved_at | User / DateTime | | | Approver ≠ creator |

### 3.3 ReportLine
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| definition | FK ReportDefinition | Y | | |
| line_code | Char(20) | Y | | Unique per definition |
| order / parent_line / indent | Int / FK self / SmallInt | Y / N / Y | | |
| label | Char(200) | Y | Bi | |
| kind | Choice `header`, `accounts`, `subtotal`, `formula`, `computed` | Y | | `computed` = service-provided value (e.g. current-year profit, R12) |
| selector | JSON: `codes`, `ranges`, `types`, `flags`, `exclude` | Y for `accounts` | | Resolved against the entity's own accounts by code (R7) |
| balance_basis | Choice `closing`, `movement`, `opening` | Y | | SoFP closing; P&L movement |
| sign | Choice `natural`, `negate` | Y | | Display: credits shown positive for income/equity/liabilities |
| formula | Char(300) | Y for `formula` | | Line codes with + / − only |
| cash_flow_class | Choice `profit`, `non_cash`, `working_capital`, `tax_paid`, `investing`, `financing`, `cash` | Y on cash-flow definitions | | R15 |
| soce_column | Choice `share_capital`, `statutory_reserve`, `retained_earnings`, `translation_reserve`, `other_reserve` | Y on SoCE | | |
| note_ref | Char(20) | N | | Note pack code (§3.6) |
| show_if_zero | Bool | Y | | Default False |

### 3.4 EntityAccountMapping (per entity override)
`entity`, `definition`, `account` (FK Account, same entity), `line` (FK ReportLine of that definition), `reason_en/_ar`, `created_by`, `approved_by` (approver ≠ creator). Unique `(entity, definition, account)`. Used for entity-specific accounts that no template selector covers.

### 3.5 StatementRun
| Field | Type | Req | Notes |
|---|---|---|---|
| entity | FK | Y | |
| definition | FK ReportDefinition (approved) | Y | Draft definitions only in `preview` runs |
| kind | Choice `preview`, `draft`, `final` | Y | `final` = R18 |
| period_from / period_to / as_of | Date | Y | |
| comparative_from / comparative_to / comparative_as_of | Date | N | R17 |
| currency | FK Currency | Y | = `entity.base_currency` (R5) |
| status | Choice `ok`, `checks_failed`, `final`, `superseded` | Y | |
| checks | JSON | Y | Results of R10, R16, R20 |
| output | JSON | Y | Lines × (current, comparative), labels en/ar, frozen at run time |
| source_hash | Char(64) | Y | SHA-256 of contributing line ids and amounts |
| generated_by / generated_at / finalised_by / finalised_at | User / DateTime | | |

### 3.6 Notes data packs (computed, frozen into `StatementRun.output` for `statement=notes`)
| Pack | Content | Source |
|---|---|---|
| `ppe` | Opening, movements, closing per account 1500–1600 and 1590 | GL only (no asset register, OQ-13) |
| `investments` | 1400 / 1410 / 1420 balances per investee | GL + investee tag (OQ-25) |
| `related_parties` | Balances and transactions per counterparty entity (1210, 1220, 2110 and documents with `is_intercompany`) | `intercompany_balances(by_currency=True)`, invoicing |
| `revenue` | Revenue per `IncomeType` and VAT category | invoicing lines |
| `employee_benefits` | EOSB opening, charge, payments, closing (2500, 5120) | payroll `EosbProvisionEntry` |
| `zakat_tax` | Zakat base summary, charge, payments, payable movement, certificate status; income tax | tax-returns `ZakatYear`, `IncomeTaxYear` |
| `purification` | Non-compliant income flagged, transferred to 2600, paid out, closing 2600 | `purification_amount`, 4900, 2600 |
| `fx` | Realised / unrealised FX (4700, 4710, 5750, 5760, 5770), balances by currency | `trial_balance(by_currency=True)` |
| `financing` | Islamic financing (2400) and its cost (5600) | GL |
Note texts are written outside the system (OQ-27); packs give the numbers, bilingual captions.

### 3.7 YearEndClose and ReserveTransfer (registered ledger documents)
**YearEndClose**: `entity`, `fiscal_year_start` / `_end`, `status` (`draft`, `submitted`, `posted`), `checks` (JSON, R21), `zakat_acknowledgement_en/_ar` (if no Zakat provision, R21), `closing_entry` (FK JournalEntry, PROTECT), `prepared_by`, `approved_by`. Unique `(entity, fiscal_year_start)`.
**ReserveTransfer**: `entity`, `fiscal_year_start`, `date`, `profit_basis` (Money, from the final P&L), `pct`, `cap_amount`, `amount`, `basis_en/_ar`, `resolution_reference` (shareholders' resolution, N), `status`, `journal_entry`.

### 3.8 ReportingPackage (hand-off to consolidation)
| Field | Type | Req | Notes |
|---|---|---|---|
| entity | FK | Y | |
| as_of / period_from | Date | Y | Month, quarter or year end |
| version | SmallInt | Y | Unique `(entity, as_of, version)` |
| framework / framework_edition | Choice | Y | Snapshot |
| functional_currency | FK Currency | Y | |
| tb | JSON | Y | Per account: code, name_en/_ar, type, flags, debit, credit, `by_currency` (functional + transaction) |
| monthly_movements | JSON | Y | Per account and month, for average-rate translation (ledger-fx R30) |
| equity_movements | JSON | Y | Per equity account: opening, movements by entry and date, closing (historical rates) |
| intercompany | JSON | Y | Per (counterparty, currency): transaction and functional amounts, per account (1210, 1220, 2110) |
| intercompany_pl | JSON | Y | Income/expense lines with an intercompany document (e.g. 4110, 4210, 4310) per counterparty |
| investments | JSON | Y | 1400 / 1410 / 1420 per investee (OQ-25) |
| mapping_version | JSON | Y | Definitions and entity mappings used |
| statement_runs | M2M StatementRun (final) | Y for year end | |
| status | Choice `draft`, `issued`, `superseded` | Y | |
| content_hash | Char(64) | Y | |
| issued_by / issued_at | User / DateTime | Y at issue | Approver of the entity |

### 3.9 Budget (placeholder; **not built**, OQ-19)
`Budget`: `entity`, `fiscal_year_start`, `version`, `name_en/_ar`, `status` (`draft`, `approved`), `approved_by`. `BudgetLine`: `budget`, `account` (same entity) or `report_line`, `month`, `amount`. Only the interface of the budget-vs-actual report is defined (R31).

## 4. Rules

"svc" = `reporting.services`; "L" = ledger; "T" = acceptance test.

### Entity separation and framework
1. A statement run, close, transfer or package is for exactly one entity; its user needs `reporting.view` (runs) or the action's permission **in that entity**; every query is filtered by that entity. No service in this module sums several entities. **[svc + access; T AT-1]**
2. The parent entity's statements are its **separate** statements (investments carried as booked in 1400 / 1410 / 1420); group statements come only from consolidation.md. Measurement policy for investments in separate statements: OQ-15 **[CC-verify]**. **[svc; T AT-2]**
3. A run uses only definitions whose `framework` equals `Entity.reporting_standard` (or `any`) and whose edition and validity cover `period_from`. Using another framework's definition is refused (`framework_mismatch`, en/ar). **[svc; T AT-3]**
4. `Entity.reporting_standard` changes only through `core.services.change_entity_settings` (logged), effective from a fiscal year start; the first run under the new framework is marked "transition year" and comparatives use the new definitions. Transition rules (IFRS 1 / SME section 35) are not automated (OQ-4). **[svc; T AT-4]**
5. Amounts are in the entity's functional currency as booked (debit/credit). No translation here; a foreign-functional entity's statements are in its functional currency (RD5). **[svc; T AT-5]**

### Report definitions
6. Definitions are global. A `draft` definition is editable; `approved` is immutable (new version for any change); approval needs `reporting.approve_definition` at the top-level group parent, approver ≠ creator. Only approved definitions give `draft` or `final` runs. **[svc + DB trigger; T AT-6]**
7. Selectors resolve against the run entity's accounts by code, type and flags; never against another entity's accounts. Header accounts (`is_postable=False`) are never selected. **[svc; T AT-7]**
8. `EntityAccountMapping` adds an entity account to a line of a definition; it may not move an account already selected by the template (refused: map once). **[svc; T AT-8]**
9. Every line and every definition has both labels; Arabic and English runs use the same definition. Account names in drill-down come from `Account.name_en` / `name_ar`. **[svc + model; T AT-9]**
10. **Completeness.** For each statement, every account of the entity with a non-zero closing balance (SoFP) or movement (P&L) maps to exactly one line. Unmapped or doubly mapped accounts → `checks_failed` listing them; such a run cannot become `final`. 3400 is excluded (non-postable, consolidation only). **[svc; T AT-10]**

### Computation
11. Balances come from posted and reversed entries dated in range (same basis as `ledger.services._trial_balance`; reversals net to zero). The trial balance report calls `trial_balance(entity, as_of, user=..., by_currency=...)` and adds opening, movement and closing columns. **[svc; T AT-11]**
12. **SoFP** at `as_of`. Before the year's `YearEndClose` is posted, equity shows a `computed` line "Profit for the period" = Σ income − Σ expense of the current fiscal year to `as_of`, so the statement balances at any month end. **[svc; T AT-12]**
13. **P&L and OCI** over `period_from`–`period_to`, excluding the lines of the entry linked to a `YearEndClose` document. Lines ordered by nature (default) or function (OQ-3); profit before Zakat and income tax is a subtotal; Zakat (5800) and income tax (5810, proposed in tax-returns) follow it. The template has no OCI account; OCI lines show 0.00 unless an account is mapped (OQ-14). Purification income (4900) is a separate line, never netted with revenue. **[svc; T AT-13]**
14. **SoCE**: per `soce_column`, opening, profit (from R13), OCI, transfers to statutory reserve (`ReserveTransfer` entries), dividends (debits to 3300 by documents of a dividend type, OQ-24), capital changes (3100), other movements (any other manual journal on an equity account, listed by entry number). Closing equals the SoFP equity lines. **[svc; T AT-14]**
15. **Cash flows, indirect.** Start from profit before Zakat and income tax; add `non_cash` lines (5400 depreciation, movement on 2500 net of EOSB paid, 4710 / 5760 unrealised FX, 4500 share of associates, gains on disposals); `working_capital` = movements on 1200, 1210, 1310, 1320, 2100, 2110, 2200, 2300, 2310, 2320, 2330, 2340, 2600; `tax_paid` = Zakat and income tax paid (5800 + 5810 − movement on 2210 + 2250); `investing` = movements on 1220, 1300 (when not cash equivalents), 1400–1600; dividends and Islamic deposit profit received (classification OQ-12); `financing` = 2400, 3100, dividends paid, Islamic financing cost paid; `cash` = 1100, 1110. Default class per account is in the definition, not in code. **[svc; T AT-15]**
    *Note 2026-10-18 (invoicing D34, owner decision C6):* 1140 Bank – purification account (a sub-account of 1100) is **restricted cash**: it is shown on its own SoFP line ("Restricted cash – purification account / نقد مقيد - حساب التطهير") and is **excluded** from `cash` and cash equivalents; its movements (fundings from operating bank accounts, returns credited to it, payouts to charity) are reconciling items with a disclosure note (IAS 7 / IFRS for SMEs section 7) **[CC-verify]**. The ledger names such accounts in `ledger.restricted.restricted_cash_codes()` with their en/ar note; the default definitions must leave them out of the `cash` lines and of "Cash and bank".
16. **Integrity checks** on every run: TB debits = credits; SoFP assets = equity + liabilities; P&L profit = SoCE profit; SoCE closing = SoFP equity; cash-flow net change = change in `cash` lines; opening of the period = closing of the prior final run (if any); 3900 Opening balance equity is 0.00 at `as_of` after the first fiscal year (OQ-18). A failed check sets `checks_failed` with the difference. **[svc; T AT-16]**
17. **Comparatives.** Default: the same period of the prior fiscal year (P&L, cash flow, SoCE) and the prior fiscal year end (SoFP). They are recomputed from the ledger with the **current** definition; if the prior period's final run used another definition version, the run shows "re-presented / أعيد عرضها" and keeps a link to that run. Comparatives before the entity's first posted entry show blank, not 0.00. **[svc; T AT-17]**
18. **Final runs.** A run can become `final` (`reporting.finalise_run`, approver, not the generator) only when every fiscal period in the run's range (and comparative range) is `closed` and all checks pass. A final run is immutable; its `source_hash` must match a fresh recomputation at finalisation. A later final run for the same entity, definition and range supersedes it (`superseded`, kept). **[svc + DB trigger; T AT-18]**
19. **Drill-down.** Every figure → contributing accounts → journal lines (entry number, date, description, reference, counterparty, currency) → source document (`document_type`, `document_id`). Each step re-checks `ledger.view` in the entity. Names of individual customers and vendors are masked without `invoicing.view_personal_data` (invoicing R51, R86). **[svc; T AT-19]**
20. **Sharia.** In an entity with `sharia_enforced`, a non-zero balance on a Sharia-prohibited account (possible only from migration) fails the run's checks. Zakat (5800 / 2210) and purification (4900 / 2600) are shown on separate lines and notes, never combined. **[svc; T AT-20]**

### Year-end close
21. `YearEndClose` can be submitted only when: every period of the fiscal year except the last is `closed`; the last period is open with no drafts and no pending documents (`close_period` preconditions); the revaluation run for the last period is posted or `fx_exceptions` shows no monetary foreign balance; the entity's `ZakatYear` (tax-returns) is `provisioned`, or the preparer records a bilingual `zakat_acknowledgement` (e.g. entity not subject to Zakat) that the approver accepts; and the completeness check (R10) passes for the P&L. **[svc; T AT-21]**
22. The closing entry is built by the registered `build_lines`: for each income and expense account with a non-zero balance for the fiscal year, one line on the opposite side for that balance, in the functional currency at rate 1; the net goes to 3300 Retained earnings. Dated the fiscal year end. Accounts with a fixed non-functional `Account.currency` make the close fail (en/ar, OQ-7). **[svc + L; T AT-22]**
23. The closing entry is posted with `create_entry(..., post=True, grant=<reporting.post_year_end grant>, document=close)` by an approver who is not the preparer, then the last period is closed with `close_period`. One closing entry per entity and year (ledger one-entry-per-document). A wrong close is reversed (`reverse_entry`, dated in an open period) only while the last period is open; otherwise a correcting entry is made in the next open period (OQ-16). **[L + svc; T AT-23]**
24. The closing entry does not change `purification_amount`: its 4900 line is a debit and not flagged (invoicing R6 note). **[L; T AT-24]**
25. **Statutory reserve.** If `statutory_reserve_pct` > 0, after the P&L is final the service proposes `amount` = round(pct × profit for the year, 2), limited so 3200 does not exceed the cap; zero or a loss gives no transfer. The approver posts it (Dr 3300 / Cr 3200), dated the fiscal year end if that period is open, else the shareholders' resolution date in an open period. Whether the Companies Law still requires a reserve, or only the bylaws: **[CC-verify]** (OQ-6); default 0.00, no transfer. **[svc + L; T AT-25]**
26. No period reopen service exists; reporting never reopens. Corrections after close are entries in the next open period; restatement of comparatives is not built (OQ-16). **[L; T AT-23]**

### Exports
27. Every run exports to CSV (UTF-8 with BOM, one row per line, amounts as plain decimal strings), XLSX (`openpyxl`, MIT) and PDF (HTML template rendered by `WeasyPrint`, BSD, with an embedded OFL Arabic font). Languages: `en`, `ar` (RTL layout) or bilingual side by side. Only open-source libraries (RD1). **[svc; T AT-26]**
28. Exported amounts equal the run's stored `Decimal` values to the cent: XLSX numeric cells round-trip to the same 2-dp value; CSV never uses float formatting; thousands display (§3.1 `rounding_display`) adds a rounding line so totals agree. **[svc; T AT-27]**
29. Each export writes an `AccessLog` row (`report_export`: user, entity, run, format, language). Exports of draft or `checks_failed` runs carry a "DRAFT / مسودة" watermark. **[svc + access; T AT-28]**

### Management reports
30. Management reports (any `statement=management` definition, e.g. monthly P&L by income type, entity KPIs) are never `final` and print "Management information, unaudited / معلومات إدارية غير مدققة". **[svc; T AT-29]**
31. Budget vs actual: not built. The report interface is `budget_vs_actual(entity, budget, period_from, period_to, *, user)` returning per line: actual, budget, variance, variance %. Until `Budget` exists the menu item shows "not available / غير متاح". **[svc stub; T AT-29]**

### Hand-off to consolidation
32. A `ReportingPackage` can be issued (`reporting.issue_package`, approver of the entity) only when every period up to `as_of` is closed, a TB run at `as_of` passes R10 and R16, and, at a fiscal year end, the year's `YearEndClose` is posted and final runs exist for SoFP, P&L, SoCE and cash flow. **[svc; T AT-30]**
33. Package content (§3.8) is computed from the closed ledger in the **functional currency**; intercompany positions come from `_intercompany_balances(as_of, [entity], by_currency=True)` and the functional-amount variant, per counterparty and currency. No translation, elimination or ownership weighting is applied: consolidation.md does those. **[svc; T AT-31]**
34. An issued package is immutable and identified by `content_hash`. Re-issuing for the same `as_of` (e.g. after a definition change) creates version + 1 and marks the old one `superseded`; consolidation.md must state which version it used. **[svc + DB trigger; T AT-31]**
35. Packages are readable by holders of `reporting.view_package` in the **group parent** and also in the entity (access R6 pattern: group right checked on the parent, rows limited to entities the user may view; equity and minority entities need explicit membership, RD7). **[access; T AT-32]**

### Agents
36. Agents may create `preview` and `draft` runs and read drill-down (scoped, `reporting.view` **Ag**); they never finalise, close, transfer, issue packages or export (default, OQ-22). **[access; T AT-33]**

## 5. Accounting

Statements, packages and exports post nothing. Translation (ledger-fx F12) posts nothing. Two registered documents post:

**Y1. Year-end closing entry** (ALM FY2027, dated 2027-12-31; balances: 4100 1,200,000.00 Cr; 4400 500,000.00 Cr; 4900 1,200.00 Cr; 5100 400,000.00 Dr; 5400 50,000.00 Dr; 5800 103,100.00 Dr)
| Account | Dr | Cr |
|---|---|---|
| 4100 Rental income | 1,200,000.00 | |
| 4400 Dividend income | 500,000.00 | |
| 4900 Non-compliant income (to purify) | 1,200.00 | |
| 5100 Salaries and benefits | | 400,000.00 |
| 5400 Depreciation | | 50,000.00 |
| 5800 Zakat | | 103,100.00 |
| 3300 Retained earnings | | 1,148,100.00 |
Loss year: 3300 on the debit side. Zakat is posted before the close (tax-returns T6). The 4900 debit is not a purification transfer (that is invoicing E14 to 2600) and does not reduce the amount still to purify (R24).

**Y2. Statutory reserve transfer** (only if bylaws set 10% **[CC-verify]**, OQ-6): Dr 3300 114,810.00 / Cr 3200 Statutory reserve 114,810.00.

No new accounts are proposed. 5810 and 2250 are proposed in tax-returns.md; 2220, 2310, 2320, 2330, 2340, 5120, 5920, 5930 in invoicing.md and payroll.md (all stay proposed, RD3).

**Default line mapping (definitions `*-SME`, draft, accountant to approve, OQ-1)**
| Statement line | Accounts |
|---|---|
| PPE | 1500, 1520, 1590 |
| Investment property | 1510 |
| Intangible assets | 1600 |
| Investments in subsidiaries / associates / other | 1400 / 1410 / 1420 |
| Loans to group entities (non-current, OQ-9) | 1220 |
| Trade and other receivables | 1200, 1320 |
| Due from group entities | 1210 |
| VAT receivable or payable (net, OQ-10) | 1310 with 2200 |
| Sharia-compliant deposits (not cash equivalents, OQ-11) | 1300 |
| Cash and bank | 1100, 1110 |
| Share capital / Statutory reserve / Retained earnings | 3100 / 3200 / 3300 + R12 profit |
| Opening balance equity (must be nil, OQ-18) | 3900 |
| Islamic financing (non-current, OQ-9) | 2400 |
| Conventional loans (non-Sharia entities only) | 2410 |
| End-of-service provision | 2500 |
| Trade and other payables | 2100, 2300, 2320, 2330, 2340 |
| Due to group entities | 2110 |
| Deferred income | 2310 |
| Zakat and income tax payable | 2210, 2250 |
| WHT payable | 2220 |
| Charity payable (purification) | 2600 |
| Revenue (by income type in notes) | 4100, 4110, 4120, 4200, 4210, 4220, 4230, 4240, 4300, 4310 |
| Dividend income / Share of profit of associates / Islamic deposit profit | 4400 / 4500 / 4600 |
| Net foreign exchange gain (loss) | 4700, 4710, 5750, 5760, 5770 |
| Non-compliant income (to purify) | 4900 |
| Employee benefits | 5100, 5110, 5120 |
| Rent; professional fees; depreciation; repairs and vehicles; bank charges | 5200; 5300; 5400; 5500, 5510; 5700 |
| Islamic financing cost / conventional interest (non-Sharia only) | 5600 / 5610 |
| Other expenses | 5900, 5920, 5930 |
| Zakat / Income tax | 5800 / 5810 |

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| `ledger` | In | Balances, lines, entries, periods; closing and reserve entries out | Existing services; `_trial_balance` used internally after the rights check |
| `consolidation` (consolidation.md) | Out | Issued `ReportingPackage` per entity (§3.8) | Contract R32 to R35; content to be agreed (OQ-25) |
| `tax` (tax-returns.md) | Both | In: Zakat and tax data for notes, Zakat provision status for R21. Out: profit before Zakat for Zakat R41 | |
| `payroll` | In | EOSB sub-ledger for the employee-benefits note | |
| `invoicing` | In | Income types, intercompany documents, purification flags | |
| Ministry of Commerce Qawaem / ZATCA Zakat filing (financial statements upload) | Out (manual) | PDF/XLSX exports of final statements | Format requirements **[CC-verify]**; no XBRL (OQ-23) |
| Auditors | Out | Exports and drill-down listings; viewer role | |
| Falcon (or other legacy system) | In (one-off) | 2026 comparatives: closing TB 31 Dec 2026 loaded as opening balances (README next step 1); prior-year P&L for comparatives as a summary import | migration-agent; comparatives method OQ-17 |
| Banks, GOSI, Mudad, ZATCA e-invoicing | none | | |

## 7. Permissions

New catalogue permissions: `reporting.view` (Ag scoped), `reporting.export`, `reporting.maintain_definition` (group-only), `reporting.approve_definition` (A, group-only), `reporting.map_entity_account`, `reporting.approve_mapping` (A), `reporting.finalise_run` (A), `reporting.prepare_year_end`, `reporting.post_year_end` (A, P), `reporting.post_reserve_transfer` (A, P), `reporting.issue_package` (A), `reporting.view_package`, `reporting.change_settings` (A).

| Action | Accountant | Approver (CFO) | Managing Partner | Group viewer | Viewer / auditor | Agent |
|---|---|---|---|---|---|---|
| Run preview / draft statements, drill-down | Y | Y | Y | Packages only | Y | Draft only |
| Export | Y | Y | Y | Packages | Y | No |
| Draft definitions (at group parent) | Y | | | | | No |
| Approve definitions | | Y (group parent, not own) | Y | | | No |
| Entity account mapping | Y (draft) | Y (approve, not own) | | | | No |
| Finalise runs | | Y (not generator) | | | | No |
| Prepare year-end close / reserve proposal | Y | | | | | No |
| Post close / reserve transfer | | Y (not preparer) | | | | No |
| Issue reporting package | | Y | | | | No |
| Read packages | Y | Y | Y | Y (full descendants; others need membership) | Y | Scoped read |
| Reporting settings | | Y | | | | No |

## 8. Acceptance tests

Fixtures: ALM (parent, IFRS for SMEs, SAR, Sharia), SUB (100%, IFRS for SMEs), FUL (100%, full IFRS), EGY (100%, functional EGP), MIN (60%, equity). Template chart, 2026–2028 periods, approved fixture definitions. Users: A1, A2 (accountant ALM), C1 (approver ALM), V1 (viewer SUB only), G1 (group viewer), agent AG.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | V1 (SUB only) | Run ALM SoFP; run SUB SoFP | ALM refused, logged; SUB allowed, no ALM data |
| AT-2 | ALM with 1400 balance 5,000,000.00 | Run ALM SoFP | Investments in subsidiaries 5,000,000.00; no SUB lines |
| AT-3 | FUL (full IFRS) | Run with `SOFP-SME` | Refused `framework_mismatch` (en/ar) |
| AT-4 | SUB switches to full IFRS from 2028-01-01 | Run FY2028 | Uses IFRS definitions; "transition year"; 2027 comparatives under new definitions |
| AT-5 | EGY | Run SoFP | Amounts in EGP; no translation reserve line |
| AT-6 | Approved definition | Edit a line; approve own draft | Edit refused (new version needed); own approval refused |
| AT-7 | Selector `ranges: 1500–1599`; header 1000 | Run | Only postable ALM accounts selected; header never |
| AT-8 | Template maps 1320 | Entity mapping moves 1320 | Refused (map once) |
| AT-9 | Line with empty `label_ar` | Save | Refused |
| AT-10 | ALM posts to new account 1530 not in any selector | Run SoFP; finalise | `checks_failed` lists 1530; finalise refused; after mapping → ok |
| AT-11 | Entries and a reversed entry in 2027-03 | TB for March | Reversal nets to zero; opening + movement = closing; totals balance |
| AT-12 | 2027-06-30, no close yet | SoFP | "Profit for the period" = Σ I − Σ X Jan–Jun; statement balances |
| AT-13 | FY2027 after Y1 | P&L FY2027 | Closing entry ignored; profit 1,148,100.00; 4900 on its own line; Zakat after profit before Zakat |
| AT-14 | Manual journal Dr 3300 / Cr 3100 | SoCE | Shown under capital changes / other with entry number; closing = SoFP |
| AT-15 | Year with depreciation 50,000.00, receivables +100,000.00, Zakat paid 90,000.00, PPE purchase 200,000.00 | Cash flow | Classified per R15; net change = cash movement |
| AT-16 | Manual journal on 1100 to an unclassified account 1530 in the cash-flow definition | Cash flow | `checks_failed` with the unexplained difference |
| AT-17 | FY2027 run; FY2026 final run used definition v1, now v2 | Run FY2027 | Comparatives recomputed with v2, flagged "re-presented"; pre-ledger months blank |
| AT-18 | 2027-12 open | Finalise FY2027 SoFP | Refused (periods open); after close: final; second final run supersedes first |
| AT-19 | Invoice to individual customer in drill-down | V1 without personal-data right | Name masked; with right shown and logged |
| AT-20 | Migrated balance on 2410 in Sharia-enforced ALM | Run | `checks_failed` (prohibited account balance) |
| AT-21 | Nov 2027 open; or Zakat not provisioned and no acknowledgement; or open foreign balance not revalued | Submit close | Refused with the reason (en/ar) each time |
| AT-22 | Balances as Y1 | Build close | Lines as Y1 exactly; 2-line minimum met; foreign-currency-fixed income account → refused |
| AT-23 | A1 prepares close | A1 posts; C1 posts; close 2027-12; C1 tries reverse | A1 refused; C1 posts Y1 and period closes; reverse refused (period closed) |
| AT-24 | `purification_amount` 1,200.00 before close | Post Y1 | Still 1,200.00 |
| AT-25 | Bylaws 10%, cap 30% of 3100 = 300,000.00, 3200 = 250,000.00 | Propose transfer | 50,000.00 (cap), not 114,810.00; loss year → no transfer; pct 0 → none |
| AT-26 | Final SoFP | Export CSV, XLSX, PDF in en, ar, bilingual | Files produced; Arabic PDF RTL with Arabic font; labels from definition |
| AT-27 | Amount 1,234,567,890.15 | Export XLSX and CSV; read back | Same Decimal to the cent; thousands view totals agree with rounding line |
| AT-28 | Draft run | Export | Watermark "DRAFT / مسودة"; `AccessLog` row |
| AT-29 | Management P&L; budget vs actual | Run; open | "unaudited" footer; budget item "not available" |
| AT-30 | ALM FY2027 without Y1 | Issue package | Refused; after Y1 and final runs: issued |
| AT-31 | Issued package; definition v3 approved | Recompute hash; re-issue | Hash stable; version 2 issued, version 1 `superseded`; intercompany positions equal `intercompany_balances` for ALM |
| AT-32 | G1 group viewer; MIN (equity) | Read SUB and MIN packages | SUB allowed; MIN refused without membership |
| AT-33 | Agent AG | Draft run; finalise; export | Draft allowed; finalise and export refused |

## 9. Open questions

**B** = blocking for that part, **GL** = blocking for go-live, **D** = default stands.

| # | Question | Owner | Status | Default |
|---|---|---|---|---|
| OQ-1 | Approve the default definitions and line mapping (§5 table) per framework. | Accountant / auditor | **B** for final runs | Fixture definitions only |
| OQ-2 | Documents to fetch: IFRS for SMEs (2015) and third edition (2025, effective 2027-01-01 **[CC-verify]**) as endorsed by SOCPA; IFRS 18 (effective 2027-01-01 **[CC-verify]**) and SOCPA endorsement; SOCPA endorsement additions (Zakat presentation, statutory reserve); Companies Law (Royal Decree M/132, 1443H) and Implementing Regulations (reserve, financial statement filing); Qawaem filing requirements; each entity's articles of association. | compliance-checker | **B** for every [CC-verify] | Values shown |
| OQ-3 | P&L by nature or function per entity. | Accountant | D | Nature |
| OQ-4 | Which edition applies from FY2027 (SME third edition; IFRS 18 for full-IFRS entities) and how comparatives are re-presented. | Accountant / auditor | **B** for FY2027 final runs | Edition as setting; re-presented comparatives (R17) |
| OQ-5 | Fiscal year end per entity: field on `Entity` or settings (shared with tax-returns OQ-3). | Dev + accountant | D | `ReportingSettings.fiscal_year_end` 12-31 |
| OQ-6 | Statutory reserve: required by the Companies Law or only by bylaws; % and cap per entity; dated year end or resolution date. **[CC-verify]** | Accountant + legal | D | 0%, no transfer |
| OQ-7 | Closing entries for accounts with a fixed foreign `Account.currency`. | Accountant + Dev | D | Close refused |
| OQ-8 | Closing entries as real postings (Y1) or computed only in reports. | Accountant | D | Real postings |
| OQ-9 | Current / non-current split for 1220, 2400, 1300 (needs maturity data). | Accountant | D | By account as in §5 |
| OQ-10 | Present VAT input and output net (same authority) **[CC-verify]**. | Accountant | D | Net |
| OQ-11 | Are Sharia-compliant deposits (1300) of 3 months or less cash equivalents? | Accountant | D | No |
| OQ-12 | Cash-flow classification of dividends and Islamic deposit profit received and financing cost paid (IAS 7 / SME section 7; IFRS 18 amendments) **[CC-verify]**. | Accountant | D | Received: investing; paid: financing |
| OQ-13 | Fixed-asset register (additions, disposals, depreciation by asset) for PPE note and cash flow. | Accountant | D | GL movements only |
| OQ-14 | OCI items and accounts (none in template). | Accountant | D | None |
| OQ-15 | Separate statements: investments in subsidiaries and associates at cost, fair value or equity method **[CC-verify]**. | Accountant / auditor | D | As booked (cost) |
| OQ-16 | Prior-period errors (IAS 8 / SME section 10): restatement layer or current-period correction; Zakat assessment differences of prior years (tax-returns T8). | Accountant / auditor | D | Current period, disclosed |
| OQ-17 | FY2026 comparatives from Falcon (or other legacy system): full prior-year TB by month, or annual summary only. | Accountant + migration-agent | **B** for FY2027 comparatives | Annual summary import |
| OQ-18 | 3900 must be nil after migration; enforce as a check from which date? | Accountant | D | From the first final run |
| OQ-19 | Budget model, source (spreadsheet import) and approval. | CFO | D | Not built |
| OQ-20 | Approve `openpyxl`, `WeasyPrint` and an OFL Arabic font as dependencies. | Dev + Managing Partner | D | Approved if open source licences confirmed |
| OQ-21 | Arabic exports: Western or Arabic-Indic digits; Hijri date shown. | Managing Partner | D | Western digits; Gregorian with Hijri in header |
| OQ-22 | May agents export? | Managing Partner | D | No |
| OQ-23 | Qawaem / XBRL filing format. **[CC-verify]** | Accountant | D | Manual upload of PDF |
| OQ-24 | Dividend declaration and payment documents (account for dividends payable; intercompany dividends via 2110 with counterparty). | Accountant | D | Manual journals, listed in SoCE "other" |
| OQ-25 | Package content agreed with consolidation.md: investee tag on 1400/1410/1420 lines (counterparty or sub-ledger), intercompany P&L per counterparty, monthly movements. | consolidation.md author + accountant | **B** for consolidation build | §3.8 as written; investee by manual tag |
| OQ-26 | Interim final statements (quarterly) or year end only. | CFO | D | Any closed range may be finalised |
| OQ-27 | Note texts: authored in the system (bilingual templates) or outside. | Accountant | D | Outside; packs give numbers |
| OQ-28 | Deferred tax (IAS 12 / SME section 29) for entities with income tax (tax-returns OQ-26). | Accountant | D | Not built |

**Blocking summary.** Build: none for drafts; final runs wait on OQ-1 and OQ-4; consolidation hand-off on OQ-25; comparatives on OQ-17. All [CC-verify] items need OQ-2 documents.
