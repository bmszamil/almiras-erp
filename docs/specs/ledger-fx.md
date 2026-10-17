# Spec: `ledger` multi-currency extension (ledger-fx)

Status: draft for review · Written: 2026-10-16 · Revised: 2026-10-16 (project-owner decision D5 on decimals; CLAUDE.md rule 7 amended) · Amended: 2026-10-16 (D6: build decisions, two-step revaluation, permission names) · Amended: 2026-10-16 (D7: R15, R18, R24, R28, R44, R45, R49, AT-34 to AT-38, OQ-18; code on branch `claude/funny-davinci-7yd88s`, commits aad0126..b690c12) · Amended: 2026-10-16 (D8: 3.6, R50, AT-39, OQ-19; revaluation run status; commits b6ed6e1..497cb09) · Amended: 2026-10-17 (project-owner decisions D9, D10: 3-decimal document currencies, proposed accounts; OQ-1, OQ-6 notes) · Author: spec-writer agent
Extends: `ledger` (models, services, tests). Touches: `core.Entity`, `ledger/coa_template.py`, `ledger/services_setup.py`. Unblocks the ledger side of `docs/specs/invoicing.md` OQ-6 (that spec is not changed here).
**[CC-verify]** = standards, Sharia or rate-source statement written from memory; the compliance-checker must check it against the official text before module-builder starts.

## 1. Purpose

Lets **every entity** in the group (Almiras, subsidiaries, and any entity added later as an `Entity` row) record transactions in any ISO 4217 currency while its ledger stays in its own **functional currency** (default SAR). Each journal line keeps the transaction currency, the transaction amount and the rate used; `debit`/`credit` stay in functional currency, so the existing balancing, posting, immutability, period, Sharia and entity-separation rules in `ledger.services` keep working unchanged. The extension adds a currency master, an exchange-rate table (manual entry and import from free public sources), realised FX gain/loss on settlement, period-end revaluation of open monetary items with automatic reversal through `reverse_entry`, FX accounts in the chart template, and a non-posting translation of foreign-functional-currency entities into the group presentation currency (SAR). All posting goes through `ledger.services.post_entry`; corrections are reversals.

## 2. Decisions

| # | Date | Who | Decision |
|---|---|---|---|
| D1 | 2026-10-16 | Project owner | "Multi-currency is needed now." Group entities deal in many currencies: SAR, CNY (RMB), EGP, GBP, EUR, USD, AED, and others. |
| D2 | 2026-10-16 | Project owner | The missing FX accounts may be added to the chart template (`ledger/coa_template.py`). |
| D3 | in repo | CLAUDE.md | Posted entries immutable; corrections by `reverse_entry`; posting only through `post_entry`; entries balance with 2+ one-sided lines; closed periods reject postings; no posting to another entity's accounts; Sharia enforced per entity; intercompany lines carry a counterparty; money is `Decimal`, stored with 2 decimals, currency explicit; open source only; bilingual text; new entities are data. *(Rule 7 amended 2026-10-16: see D5.)* |
| D4 | 2026-10-16 | Caller (task brief, not a business decision) | Scope as listed in this spec. Debit/credit stay in functional currency. Existing entries migrate to functional currency at rate 1. Group consolidation currency SAR (to be confirmed, OQ-7). Rate table global by default (OQ-4). No paid rate feeds. Forwards and other deferred exchange are out of scope. |
| D5 | 2026-10-16 | Project owner | Decimals (resolves OQ-1): functional-currency (SAR) amounts are stored with 2 decimals; foreign transaction-currency amounts are stored with up to 3 decimals and validated against that currency's own minor units. CLAUDE.md rule 7 amended accordingly. The rule "functional currency must be a 2-decimal currency" (R4) stays. |
| D6 | 2026-10-16 | Build decisions (caller brief; follows access A1 / OQ-5 default) | (a) Revaluation is two-step: `revalue_period` prepares a draft, `post_revaluation` posts it with a `ledger.post_entry` grant, so the preparer cannot approve; `discard_revaluation` drops a draft run (R23, R43 to R45). (b) Account 3400 is non-postable (report-only) pending accountant confirmation (OQ-6). (c) Permission names follow access §3.6: `fx_rate_maintain` → `ledger.maintain_entity_rate`, `fx_manual_rate` → `ledger.use_manual_rate`, `fx_revalue` → `ledger.revalue`. (d) Rate age default 5 calendar days, setting `FX_MAX_RATE_AGE_DAYS` (R12, OQ-3). (e) Not built: translation (R30, OQ-5) and automatic rate feeds (OQ-2); rates come from manual entry and CSV upload only. |
| D7 | 2026-10-16 | Build decisions (code-reviewer hardening, caller brief); human approval at merge | (a) `post_revaluation` recomputes the revaluation and refuses if anything changed since the draft (R44). (b) A discarded run is kept for audit; its never-posted draft entry is deleted and no link is kept; a discarded run does not count toward R24 (OQ-18 default adopted, R45). (c) Revaluation drafts are posted only by `post_revaluation`, never `post_entry`, and are not edited (R49). (d) Global rates are written only against the top-level group parent (R28). (e) `rerate_entry` refuses settlement drafts (R15). (f) Settlements of one item are serialised; the final one leaves no functional residue (R18). |
| D8 | 2026-10-16 | Build decision (verification review, caller brief); human approval at merge | A `RevaluationRun` changes status only through `post_revaluation` (draft → posted) and `discard_revaluation` (draft → discarded), each with its required audit fields; it is created as a draft (R50). |
| D9 | 2026-10-17 | Project owner | 3-decimal currencies (KWD, BHD, OMR, JOD): "yes, Almiras invoices in them"; documents and contracts must support 3-dp transaction amounts; SAR VAT totals stay 2 dp (invoicing D27, R93; billing D11). The ledger already stores and validates 3-dp `txn_amount` (D5, R8, R40); no ledger rule changes. Functional currency stays 2 dp (R4). |
| D10 | 2026-10-17 | Project owner | Proposed account codes (3400, 4700, 4710, 5750, 5760, 5770 and the `monetary` flag list, OQ-6) stay **proposed**; the accountant/CFO reviews them later in a Word document being prepared. |

Nothing else is decided. Every other choice is in section 9; defaults used in the rules are marked "default (OQ-n)".

## 3. Data model

Money conventions: functional amounts `DecimalField(max_digits=18, decimal_places=2)` (unchanged). Rates `DecimalField(max_digits=20, decimal_places=10)`, > 0, meaning **units of `to` currency per 1 unit of `from` currency**. Transaction amounts: `DecimalField(max_digits=21, decimal_places=3)`, with the decimals actually used limited to the currency's `minor_units` (D5, R8, R40).

### 3.1 `core.Currency` (new; global reference data, like `VatRate` in invoicing)

No `entity` FK: ISO 4217 is the same for every entity. Activation per entity is not needed (any active currency may be used by any entity).

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| code | Char(3), primary key | Y | | ISO 4217 alpha, upper case (`CNY`, not `RMB`; `CNH` is not ISO and is not loaded, OQ-12) |
| numeric_code | Char(3) | Y | | ISO 4217 numeric, e.g. `682` |
| name_en / name_ar | Char(100) | Y / Y | Bi | e.g. Saudi riyal / ريال سعودي |
| minor_units | PositiveSmallInt (0 to 4) | Y | | ISO exponent: SAR, USD, EUR, GBP, CNY, EGP, AED = 2; JPY = 0; KWD, BHD, OMR, JOD = 3. A currency with `minor_units` > 3 (e.g. ISO CLF, UYW) is not loaded and cannot be used (D5, R40) |
| symbol | Char(8) | N | | Display only |
| is_active | Bool | Y | | Inactive currencies cannot be used on new lines or rates |

Seed fixture: at least SAR, USD, EUR, GBP, CNY, EGP, AED, KWD, BHD, OMR, QAR, JOD, JPY, CHF, INR, PKR, TRY. Arabic names reviewed by the accountant.

### 3.2 `core.Entity` (changed)

| Field | Change | Notes |
|---|---|---|
| base_currency | Becomes the entity's **functional currency**. Recommended: keep the column name, convert to `FK(Currency, to_field="code", on_delete=PROTECT)`, default `SAR` (OQ-10). | `invoicing.md` already reads `entity.base_currency`. Must reference a currency with `minor_units = 2` (R4). Locked once the entity has any posted entry (R5). |
| presentation_currency | New, `FK(Currency)`, null | Used only on the parent (reporting) entity; default `SAR` (OQ-7). |

### 3.3 `ledger.ExchangeRate` (new)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity, null, PROTECT | N | | Null = group-wide (global) rate (default). Set = entity-specific override (OQ-4). |
| date | Date | Y | | Rate date (the value date the source publishes) |
| from_currency / to_currency | FK Currency | Y | | Must differ (DB check) |
| rate | Decimal(20,10) | Y | | > 0 (DB check) |
| rate_type | Choice: `spot`, `average` | Y | | `spot` for transactions and period-end (closing) rates; `average` only for translation (R30). No `forward` type exists (R36). |
| source | Choice: `manual`, `sama`, `ecb`, `cbe`, `cbuae`, `pboc`, `derived`, `file` | Y | | Which sources are enabled: OQ-2 |
| source_reference | Char(200) | N | | File name, URL or batch id; for `derived`, the base rates used |
| period_start / period_end | Date | Y if `average` | | Averaging window |
| created_by, created_at, import_batch | | Y / Y / N | | `created_by` is a human or the named import service user |

Unique `(entity, date, from_currency, to_currency, rate_type, source)` (null entity treated as one value: use `nulls_distinct=False`).
A rate row referenced by any posted line is locked (cannot be edited or deleted); corrections are a new row (R11).

### 3.4 `ledger.Account` (changed)

| Field | Type | Req | Notes |
|---|---|---|---|
| is_monetary | Bool, default False | Y | Included in revaluation (R21). Template default list in section 5.0, to be confirmed by the accountant (OQ-6). |
| currency | FK Currency, null | N | Null = any currency. Set = lines must be in this currency (e.g. a USD bank sub-account). Bank sub-account layout: OQ-11. |

### 3.5 `ledger.JournalLine` (changed)

`debit` / `credit` keep their meaning and constraints (`line_amounts_not_negative`, `line_one_side_only`) and are now explicitly **functional-currency amounts**.

| Field | Type | Req | Notes |
|---|---|---|---|
| currency | FK Currency (`to_field="code"`), PROTECT | Y | Transaction currency. Migration default: entry entity's functional currency. |
| txn_amount | Decimal(21,3) (D5) | Y | ≥ 0, same side as debit/credit. 0 only for `revaluation` lines. Decimals used ≤ `currency.minor_units` (R8). |
| fx_rate | Decimal(20,10), null | Y unless `revaluation` | Functional units per 1 transaction unit. Exactly 1 when `currency` = functional currency. |
| rate_source | Choice: `functional`, `table`, `manual`, `carrying` | Y | `carrying` = clearing an earlier line at its original rate (settlement). |
| exchange_rate | FK ExchangeRate, null, PROTECT | Y if `rate_source=table` | Provenance; locks the rate row (R11). |
| settles_line | FK JournalLine, null, PROTECT | Y if `rate_source=carrying` | The line being cleared (e.g. the invoice's AR line). Same entity, account, currency. |
| manual_rate_reason | Char(300) | Y if `rate_source=manual` | |
| line_kind | Choice: `normal`, `settlement`, `realised_fx`, `revaluation`, `rounding` | Y | Default `normal` |

DB checks: `txn_amount >= 0`; `fx_rate IS NULL OR fx_rate > 0`; `line_kind='revaluation'` ⇔ (`txn_amount = 0` and `fx_rate IS NULL`).
Checks needing the entity (functional currency) live in `validate_entry` (R6 to R10).

### 3.6 `ledger.RevaluationRun` (new)

| Field | Type | Req | Notes |
|---|---|---|---|
| entity | FK Entity | Y | |
| period | FK FiscalPeriod (same entity) | Y | Unique `(entity, period)` (amended 2026-10-16: see R24 and OQ-18 for discarded runs). *Amended 2026-10-16 (D7):* DB partial unique `one_revaluation_per_period` among runs whose status is not `discarded` |
| rate_date | Date | Y | = period end date |
| status | Choice `draft`, `posted`, `discarded` | Y | *Added 2026-10-16 (D6).* `draft` after `revalue_period`; `posted` after `post_revaluation`; `discarded` after `discard_revaluation` |
| entry | FK JournalEntry, null | N | Draft revaluation entry dated period end; null when no differences. *Amended 2026-10-16 (D7):* set to null when the run is discarded (the draft is deleted, R45) |
| reversal | FK JournalEntry, null | N | Auto-reversal dated day 1 of the next period; set by `post_revaluation` |
| run_by, run_at | FK User, DateTime | Y | Preparer; human user only (R26) |
| posted_by, posted_at | FK User, DateTime | N | *Added 2026-10-16.* User of the `ledger.post_entry` grant; never `run_by` (R44) |
| discarded_by, discarded_at | FK User, DateTime | N | *Added 2026-10-16.* |

*Amended 2026-10-16 (D7):* runs are never deleted; `status` and the fields above change only through `ledger.services`.
*Amended 2026-10-16 (D8):* DB check `revaluation_status_known` (status in draft/posted/discarded). Required fields per status (model `save`): `draft` has no `posted_by`/`posted_at`/`discarded_by`/`discarded_at`; `posted` has `posted_by`, `posted_at` and `reversal`, no discard fields; `discarded` has `discarded_by`, `discarded_at`, `entry` null, no post fields (R50).

## 4. Rules

"svc" = `ledger.services`; "DB" = database constraint; "AT-n" = acceptance test.

### Currency and functional currency
1. Every journal line has a transaction currency, a transaction amount and (except revaluation lines) a rate. `create_entry` called without currency fields produces `currency` = entity functional currency, `fx_rate` = 1, `rate_source=functional`, `txn_amount` = debit + credit. **[svc `create_entry` defaults; AT-1]**
2. `debit`/`credit` are always functional-currency amounts. The existing check "debits = credits" in `validate_entry` is the balancing rule and is unchanged: **an entry must balance in functional currency**. **[svc `validate_entry`; AT-13]**
3. An entry is **not** required to balance per transaction currency (cross-currency settlements and FX lines legitimately do not). The reversal of an entry nets to zero per account in both functional and transaction currency. **[svc; AT-6, AT-18]**
4. An entity's functional currency must have `minor_units = 2`. *(Amended 2026-10-16: kept by D5; previously "until OQ-1 is decided".)* **[`Entity.clean` + svc at entity setup; AT-20]**
5. An entity's functional currency cannot change once the entity has a posted entry. A change of functional currency (IAS 21 / IFRS for SMEs s30, prospective **[CC-verify]**) needs a separate procedure (OQ-13). **[`Entity.clean` + svc; AT-23]**
6. For a line whose currency equals the functional currency: `fx_rate` = 1 and `txn_amount` = debit + credit. **[svc `validate_entry`; AT-15]**
7. For a foreign-currency `normal` line: functional amount = `round_half_up(txn_amount × fx_rate, 2)`. For a `settlement` line: either the same formula with the carrying rate, or exactly the remaining functional balance of `settles_line` (final clearing, R18). Any other value is rejected. **[svc `validate_entry`; AT-14]**
8. `txn_amount` must have no more decimals than the currency's `minor_units`, and never more than 3 (D5): JPY 1.5 rejected; KWD 1.234 accepted; KWD 1.2345 rejected; USD 1.005 rejected. Functional `debit`/`credit` always have 2 decimals. Float input is rejected, not converted. *(Amended 2026-10-16 by D5.)* **[svc input validation; DB `decimal_places=3`; AT-20, AT-30]**
9. A line on an account with `currency` set must be in that currency. Inactive currencies are rejected. **[svc `validate_entry`; AT-21, AT-22]**
10. Revaluation lines (`txn_amount = 0`, functional amount ≠ 0) are allowed only in entries created by `revalue_period` or by its reversal. **[svc `validate_entry` checks entry origin; DB check on kind; AT-8]**

### Exchange rates
11. Rates are entered manually or imported. A rate row used by a posted line is never edited or deleted; a wrong rate is fixed by a new row and, where entries already used it, by reversal and re-entry. Posted lines keep their stored rate whatever later happens to the table. **[model `save/delete` guard; svc; AT-17]**
12. Rate lookup `get_rate(entity, from, to, date, rate_type="spot")`: entity-specific rows first, then global rows; exact date first, then the latest earlier date not older than `FX_MAX_RATE_AGE_DAYS` (default 5 calendar days, covers the Fri/Sat weekend; OQ-3). Between sources on the same date: `manual` first, then the configured primary source (OQ-2). If only `to→from` exists, the inverse `1/rate` (rounded to 10 dp) is used. No other triangulation at lookup time. **[svc `get_rate`; AT-11, AT-16]**
13. Cross rates (e.g. EUR→SAR from ECB EUR→USD and the SAR/USD peg) are created only by the import job as rows with `source=derived` and `source_reference` naming the base rows. **[svc `import_rates`; AT-28]**
14. `create_entry` resolves the rate for each foreign-currency line on the entry date. **No rate → no entry**: `create_entry` raises `LedgerError` naming currency pair and date (en/ar) and creates nothing. **[svc; AT-11]**
15. `post_entry` (through `validate_entry`) rejects a foreign-currency line that has no rate, and a `table` line whose rate row is not the one `get_rate` would return for the entry's current date (the draft's date was changed after rating). The fix is to re-rate the draft. This check is skipped for reversal entries (`reversal_of` set), which copy the original rates. **[svc `validate_entry`; AT-11, AT-12]**
    *Amended 2026-10-16 (D7):* `rerate_entry(entry, user)` refuses a draft that has `settlement` or `realised_fx` lines (re-rating the counter line alone would leave the realised FX line wrong); such a draft is discarded and its lines built again with `fx_settlement_lines` for the new date (en/ar message). **[svc `rerate_entry`; AT-36]**
16. A `manual` rate on a line needs `manual_rate_reason` and the `ledger.use_manual_rate` permission (was `fx_manual_rate`; renamed 2026-10-16, D6). Allowed deviation from the table rate: OQ-3 (default: no limit, flagged on the FX exceptions report). **[svc + permission; AT-12]**

### Rounding
17. Functional amounts use `ROUND_HALF_UP` to 2 dp (same default as invoicing R9; OQ-8). If the lines of a draft balance in their transaction currency but their rounded functional amounts differ by no more than 0.01 × (number of foreign lines), `create_entry` adds one `rounding` line (functional currency) on account 5770 for the difference. A larger difference is not auto-fixed: the entry stays unbalanced and `post_entry` rejects it. `post_entry` never adds lines. **[svc `create_entry`; AT-13]**

### Settlement and realised FX
18. A settlement (payment, receipt, offset) clears the foreign-currency open item at its **carrying rate** (`rate_source=carrying`, `settles_line` set) and records the settling cash or counter item at the **settlement-date rate**. The functional difference goes to 4700 (gain) or 5750 (loss) as a `realised_fx` line in functional currency. Partial settlements clear `round(txn × carrying rate, 2)`; the settlement that brings the open transaction amount to zero clears exactly the remaining functional balance, so no residue is left. **[svc `fx_settlement_lines(...)` used by invoicing and manual journals; AT-4, AT-5, AT-7]**
    *Amended 2026-10-16 (D7):* after the final settlement both the transaction and the functional balance of the settled item are exactly 0. Concurrent settlements of the same item are serialised: posting locks the entry's period, its own lines, the lines it settles (in pk order) and its rate rows (FOR SHARE on PostgreSQL), so two settlements together cannot clear more than is open. **[svc `_lock_for_posting`; AT-7, AT-37]**
19. Because revaluations are reversed on the first day of the next period (R23), the carrying rate is always the original transaction rate. **[follows from R23; AT-8]**
20. A currency exchange (e.g. SAR bank → USD bank) is one entry with both legs on the same date. The foreign leg's rate is the actual conversion rate (functional paid ÷ foreign received, `rate_source=manual`, reason "actual bank rate"); no FX gain/loss arises at the exchange itself. **[svc `record_currency_exchange(entity, date, from_account, from_amount, to_account, to_amount, user)`; AT-19]**

### Unrealised revaluation
21. `revalue_period(entity, period, user)` revalues, at the period-end `spot` rate, the balance of every `is_monetary` account per (account, currency, counterparty_entity) where currency ≠ functional currency. Difference = `round(foreign balance × closing rate, 2)` − functional balance. Gains go to 4710, losses to 5760; the account side uses a `revaluation` line in that currency with `txn_amount = 0`. **[svc; AT-8]**
22. A missing closing rate for any currency with an open balance fails the whole run; nothing is posted. **[svc atomic; AT-11]**
23. The revaluation entry is dated the period end and posted with `post_entry`. In the same transaction it is reversed with `reverse_entry(entry, date=<next period start>, user, reason="Automatic reversal of FX revaluation")`. If the next period is missing or closed, the whole run fails. **[svc atomic; AT-8, AT-10]**
    *Amended 2026-10-16 (D6):* two steps. `revalue_period` creates the **draft** revaluation entry dated period end and a `RevaluationRun` in `draft`; it posts nothing. `post_revaluation(run, grant)` posts the draft with `post_entry` and, in the same transaction, posts its automatic reversal dated day 1 of the next period with `reverse_entry`. If the next period is missing or closed, nothing posts (neither entry nor reversal) and the run stays `draft`. Details in R43 to R45. **[svc atomic; AT-8, AT-10, AT-31]**
24. One revaluation run per (entity, period). A second run is rejected. *(Amended 2026-10-16: a `discarded` run's effect on this rule is OQ-18.)* Re-running after a rate correction: OQ-9 (default: not possible; correct with a manual journal before closing). **[DB unique + svc; AT-9]**
    *Amended 2026-10-16 (D7):* a `discarded` run does not count: the period may be revalued again after a draft is discarded (OQ-18 default). A posted run still blocks a second run (OQ-9). **[DB partial unique excluding `discarded`; AT-32, AT-35]**
25. A revaluation into a closed period is rejected (existing `_find_open_period`). **[ledger; AT-10]**
26. Only a human user with `ledger.revalue` permission (was `fx_revalue`; renamed 2026-10-16, D6) runs `revalue_period`; agents and system users cannot. **[svc permission; AT-29]**

### Entity separation
27. Every line's account belongs to the entry's entity (unchanged). Entity-specific rates are used only by that entity; one entity's override never changes another entity's lookup. **[svc `validate_entry`, `get_rate`; AT-16]**
28. Users maintain entity-specific rates only for entities they hold `ledger.maintain_entity_rate` on (was `fx_rate_maintain`; renamed 2026-10-16, D6); global rates need the group-level permission (OQ-4). Rate list views and APIs are filtered by entity permission (global rows are visible to all ledger users). **[svc + permission; AT-16]**
    *Amended 2026-10-16 (D7):* global rates are used by every entity, so they are written only with `ledger.maintain_global_rate` (or `ledger.import_global_rate` for the import system user) on the **top-level** group parent (`consolidation=parent`, no `parent`). The right held on a lower parent entity is refused. **[svc `_authorise_rate_write`; AT-38]**
29. Intercompany mirror checks compare positions in **transaction currency** per (entity, counterparty, currency); functional amounts of two entities with different functional currencies are not expected to mirror. **[svc `intercompany_balances(as_of, by_currency=True)`; AT-25]**

### Translation and consolidation
30. `translate_trial_balance(entity, as_of, period_from, target_currency)` is a **report, not a posting**: assets and liabilities at the closing spot rate, income and expense at the `average` rate of the period (or transaction-date rates), equity at historical rates; the balancing difference is shown as foreign currency translation reserve (3400). It never writes journal entries in the subsidiary. Method details: OQ-5. **[svc; AT-26]** *Amended 2026-10-16 (D6): not built until OQ-5 is answered; AT-26 deferred.*
31. An entity whose functional currency equals the target currency is returned unchanged by translation. **[svc; AT-26]**

### Sharia
32. Sharia account blocks apply to foreign-currency lines exactly as to functional ones (unchanged `validate_entry`). **[ledger; AT-19]**
33. FX gains and losses from spot settlement and revaluation are not interest and post to ordinary (not purification) accounts 4700/4710 **[CC-verify; Sharia board to confirm, OQ-14]**.
34. Settling a debt in a different currency from the one it is denominated in uses the spot rate of the settlement day, not a pre-agreed rate (AAOIFI Sharia Standard No. 1 **[CC-verify]**). R18 and R20 implement this. **[svc]**
35. Currency exchange is spot only: both legs in one entry on one date (R20). Settlement within the customary spot delay (e.g. T+2) is treated as spot **[CC-verify; Needs Sharia board sign-off, OQ-14]**.
36. Forward contracts, futures, swaps, options and any deferred currency exchange are **out of scope and not built**: no `forward` rate type, no hedge accounting, no forward-points account. Any future request **Needs Sharia board sign-off**. **[model choices; code-reviewer; AT-19]**

### Bilingual
37. Currency names, new account names and every new `LedgerError` message exist in English and Arabic. Reversal descriptions created by `revalue_period` carry both (`description` en, Arabic in the reason text, until `JournalEntry` gets `description_ar`, OQ-15). **[svc; AT-27]**

### Backward compatibility
38. The schema migration adds the new fields; a data migration sets, for every existing line, `currency` = entry entity's `base_currency`, `fx_rate` = 1, `rate_source=functional`, `txn_amount` = debit + credit, `line_kind=normal`. It uses `QuerySet.update()` (the `JournalLine.save` lock is bypassed only here, because no amount, account or status changes). Functional amounts and trial balances are identical before and after. **[migration 0002; code-reviewer must approve the bypass; AT-2]**
39. Every existing test in `ledger/tests.py` passes unchanged. **[test suite; AT-1]**

### Decimals (added 2026-10-16, D5)
40. A currency whose `minor_units` is greater than 3 cannot be loaded as active or used on a line or a rate (storage holds at most 3 decimals). **[`Currency.clean` + svc `validate_entry`; AT-30]**
41. Conversion of a 3-decimal transaction amount to functional currency uses R7 unchanged: `round_half_up(txn_amount × fx_rate, 2)`. Revaluation (R21), settlement (R18) and rounding lines (R17) work on 2-decimal functional amounts only. **[svc; AT-30]**
42. The R38 migration changes `txn_amount` to `Decimal(21,3)`; existing values (all functional, 2 decimals) are copied unchanged. **[migration 0002; AT-2]**

### Two-step revaluation and build decisions (added 2026-10-16, D6)
43. `revalue_period(entity, period, user)` needs `require(user, entity, "ledger.revalue")` and a human user. It computes R21, creates the draft entry dated period end through `create_entry`, and creates the `RevaluationRun` (`draft`). R22 applies: a missing closing rate creates nothing. **[svc atomic; AT-8, AT-11, AT-29]**
44. `post_revaluation(run, grant)` requires a `ledger.post_entry` grant (access R14, R23, R30) for that run's draft entry, in the run's entity. The preparer (`run_by`) cannot be the grant's user: segregation is the access **A** rule, with the draft's `created_by` = preparer. It posts the entry and its reversal (dated day 1 of the next period) in one transaction and sets `posted`, `posted_by`, `posted_at`, `reversal`. Missing or closed next period, closed revaluation period, or a run not in `draft`: nothing posts. **[svc atomic + access; AT-10, AT-31]**
    *Amended 2026-10-16 (D7):* before posting, `post_revaluation` locks every period of the entity up to the next one and recomputes R21. If the computed lines (account, currency, counterparty, amounts, closing rate) differ from the draft (a backdated posting, a new closing rate), nothing posts, the grant stays unused and the error says "discard it and re-run the revaluation" (en/ar). **[svc; AT-34]**
45. `discard_revaluation(run, user)` applies only to a `draft` run: it sets `discarded` and leaves the draft entry unposted (drafts are not journal postings; CLAUDE.md rule 1 is not affected). A `posted` run cannot be discarded; corrections follow OQ-9. Permission: `ledger.revalue` (default; OQ-18). **[svc; AT-32]**
    *Amended 2026-10-16 (D7), replaces "leaves the draft entry unposted":* the run is kept with `status=discarded`, `discarded_by`, `discarded_at`; its draft entry, never posted, is **deleted** and `run.entry` is set to null (no link kept). CLAUDE.md rule 1 is not affected (only drafts are deleted). Discarding an already discarded run is refused. **[svc; AT-32, AT-35]**
46. Account 3400 (translation reserve) is non-postable: `validate_entry` rejects any line on it in every entity. It is used by the translation report only. Pending accountant confirmation (OQ-6). **[svc `validate_entry` + account flag; AT-33]**
47. `FX_MAX_RATE_AGE_DAYS` (Django setting, default 5) is the R12 fallback window. **[svc `get_rate`; AT-11]**
48. Rates enter only by manual entry and CSV upload (`source=manual` / `file`). `import_rates` for SAMA, ECB, CBE, CBUAE, PBOC is not built (OQ-2); R13 and AT-28 are deferred. **[scope; code-reviewer]**

### Build hardening (added 2026-10-16, D7)
49. A revaluation draft is posted only by `post_revaluation` (with its reversal, after the R44 recheck): `post_entry` refuses it (`use_post_revaluation`). It is never edited: `update_draft` refuses it; the change is discard and re-run. **[svc `post_entry`, `_update_draft`; AT-34]**

The other D7 changes are amendment notes on R15, R18, R24, R28, R44 and R45.

### Revaluation run status (added 2026-10-16, D8)
50. A `RevaluationRun` is created only as `draft` (`save`, `bulk_create`). Its status changes only draft → `posted` in `post_revaluation` and draft → `discarded` in `discard_revaluation`; a posted or discarded run never changes again, and no run is deleted. Setting `status` on the model and saving is refused, also with plausible audit fields; `QuerySet.update` and `delete` on runs are refused. The audit fields of 3.6 must match the status (a draft never carries a poster or discarder). Messages en/ar. **Known limitation:** these are model and queryset guards only; no database trigger guards `ledger_revaluationrun` on PostgreSQL or SQLite, so raw SQL or the base `django.db.models.QuerySet.update` can still change a run's status and fields (OQ-19; access.md R44 for the entry-level triggers). **[M `save`/queryset + DB check; svc; AT-39]**

## 5. Accounting

Codes from `ledger/coa_template.py`. Example entity ALM, functional SAR. Rates are illustrative, not real.

### 5.0 Template changes (all **PROPOSED**, accountant to approve, OQ-6)

| Code | English | Arabic | Type | Flags |
|---|---|---|---|---|
| 3400 | Foreign currency translation reserve | احتياطي ترجمة العملات الأجنبية | equity | consolidation/report use only; *amended 2026-10-16 (D6, R46): non-postable in every entity, pending accountant confirmation* |
| 4700 | Foreign exchange gain – realised | أرباح فروقات العملة المحققة | income | |
| 4710 | Foreign exchange gain – unrealised | أرباح فروقات العملة غير المحققة | income | |
| 5750 | Foreign exchange loss – realised | خسائر فروقات العملة المحققة | expense | |
| 5760 | Foreign exchange loss – unrealised | خسائر فروقات العملة غير المحققة | expense | |
| 5770 | Currency rounding differences | فروقات تقريب العملات | expense | may carry credits |

New template flag `monetary` (default proposal): 1100, 1110, 1200, 1210, 1220, 1300, 1310, 2100, 2110, 2200, 2210, 2300, 2400, 2500, 2600. Not monetary: 1320, 14xx, 15xx, 1600, equity, income, expense.
`install_chart_of_accounts` uses `get_or_create`, so re-running it adds the new accounts to existing entities, but it **does not update** `is_monetary` on existing accounts: a data migration sets the flag.

### 5.1 Events

**F1. Foreign-currency sales invoice** (2027-01-15, EUR 10,000.00, spot EUR→SAR 4.1000; VAT per invoicing.md, omitted)
| Account | Currency | Txn amount | Rate | Dr SAR | Cr SAR |
|---|---|---|---|---|---|
| 1200 Trade receivables | EUR | 10,000.00 | 4.1000 | 41,000.00 | |
| 4100 Rental income | EUR | 10,000.00 | 4.1000 | | 41,000.00 |

**F2. Month-end revaluation** (2027-01-31, closing 4.0800; target 40,800.00; loss 200.00)
| Account | Currency | Txn amount | Kind | Dr SAR | Cr SAR |
|---|---|---|---|---|---|
| 5760 FX loss – unrealised | SAR | 200.00 | normal | 200.00 | |
| 1200 Trade receivables | EUR | 0 | revaluation | | 200.00 |

*Amended 2026-10-16 (D6):* F2 is created as a draft by `revalue_period` and posted by `post_revaluation` together with F3.

**F3. Automatic reversal** (2027-02-01, by `reverse_entry`): 1200 EUR revaluation Dr 200.00 / 5760 Cr 200.00.

**F4. Receipt, realised gain** (2027-02-20, EUR 10,000.00 at 4.1250 = 41,250.00)
| Account | Currency | Txn amount | Rate | Kind | Dr SAR | Cr SAR |
|---|---|---|---|---|---|---|
| 1100 Cash and bank (EUR) | EUR | 10,000.00 | 4.1250 (table) | normal | 41,250.00 | |
| 1200 Trade receivables | EUR | 10,000.00 | 4.1000 (carrying) | settlement | | 41,000.00 |
| 4700 FX gain – realised | SAR | 250.00 | 1 | realised_fx | | 250.00 |

**F5. Vendor bill and payment, realised loss** (bill GBP 2,000.00 at 4.7000 = 9,400.00; paid at 4.7600 = 9,520.00)
| Account | Currency | Txn amount | Rate | Dr SAR | Cr SAR |
|---|---|---|---|---|---|
| 2100 Trade payables | GBP | 2,000.00 | 4.7000 (carrying) | 9,400.00 | |
| 5750 FX loss – realised | SAR | 120.00 | 1 | 120.00 | |
| 1100 Cash and bank (GBP) | GBP | 2,000.00 | 4.7600 | | 9,520.00 |

**F6. Cross-currency payment** (same bill paid from the SAR account): as F5 with the credit line `1100 SAR 9,520.00 rate 1`. Balanced in SAR; not balanced per currency (R3).

**F7. Revaluation of a liability** (GBP 2,000.00 open at 31 Jan, carrying 9,400.00, closing 4.7500 → 9,500.00): Dr 5760 100.00 / Cr 2100 (GBP, revaluation) 100.00; reversed 1 Feb.

**F8. Currency exchange** (SAR → USD, bank pays USD 10,000.00 for SAR 37,510.00)
| Account | Currency | Txn amount | Rate | Dr SAR | Cr SAR |
|---|---|---|---|---|---|
| 1100 Cash and bank (USD) | USD | 10,000.00 | 3.7510 (manual, actual) | 37,510.00 | |
| 1100 Cash and bank (SAR) | SAR | 37,510.00 | 1 | | 37,510.00 |

**F9. Rounding line** (EUR lines at 4.1235: Dr 5300 33.33, 33.33, 33.34 = SAR 137.44 + 137.44 + 137.48 = 412.36; Cr 2100 EUR 100.00 = 412.35)
| Account | Currency | Dr SAR | Cr SAR |
|---|---|---|---|
| 5300 × 3 | EUR | 412.36 | |
| 2100 | EUR | | 412.35 |
| 5770 Currency rounding differences | SAR | | 0.01 |

**F10. Partial settlements** (F1 invoice, two receipts EUR 3,333.33 and EUR 6,666.67): first clears round(3,333.33 × 4.1000) = 13,666.65; final clears the remaining 27,333.35 exactly (R18). Realised FX per receipt against its own settlement rate.

**F11. Reversal of any foreign entry**: `reverse_entry` swaps debit/credit and copies `currency`, `txn_amount`, `fx_rate`, `rate_source`, `exchange_rate`, `settles_line`, `line_kind` and counterparty.

**F12. Translation** (report only, R30): no journal entry. Example: EGY (functional EGP) TB translated to SAR; difference shown on 3400 in the consolidated view.

## 6. Integrations

All through the integration layer; open source parsers only; tests use stored fixture files, never live calls.

| System | Direction | What flows | Status |
|---|---|---|---|
| SAMA (Saudi Central Bank) published rates | In | Daily rates vs SAR, if a machine-readable free publication exists **[CC-verify]** | Option, OQ-2 |
| ECB euro foreign exchange reference rates | In | Daily XML/CSV, EUR base, about 30 currencies; does **not** include SAR, AED or EGP **[CC-verify]**; SAR crosses derived via USD (R13) | Option, OQ-2 |
| Central Bank of Egypt, CBUAE, PBOC/CFETS | In | Official local rates for EGP, AED, CNY entities | Option, OQ-2 |
| Manual entry / CSV upload | In | Any pair; `source=manual` or `file` | Always available; *the only rate input built (2026-10-16, D6); the feed rows above are not built* |
| ZATCA (via `invoicing`/`zatca`) | Out | Foreign-currency invoices must also show VAT total in SAR **[CC-verify]**; uses the line rate from this module | Hand-off to invoicing/zatca specs |
| Banks | In | Foreign-currency statements; actual conversion rates for R20 | Bank module, not built here |
| SAP B1 / Daftra (migration) | In (one-off) | Open foreign-currency items with original currency, amount and rate; must tie to opening TB in SAR and per currency | migration-agent, OQ-16 |
| GOSI / Mudad | none | Payroll is SAR | n/a |

## 7. Permissions

No entity-scoped role model exists in `core` yet (same gap as invoicing OQ-25). Proposed permissions, granted **per entity** unless marked group:

| Action | Accountant | Group treasury (group-level) | Approver | Viewer / Auditor | Agent service user | Import service user |
|---|---|---|---|---|---|---|
| Create/edit global rate | | Y | | | No | Y (import only, logged) |
| Create/edit entity rate (`ledger.maintain_entity_rate`) | Y (own entity) | Y | | | No | No |
| Use manual line rate (`ledger.use_manual_rate`) | Y | | | | No | No |
| Create foreign-currency draft entry | Y | | | | Draft only, table rates only | No |
| Post foreign-currency entry | Y | | Y | | No | No |
| Prepare revaluation (`ledger.revalue`, `revalue_period`) and discard a draft run | Y | | | | No | No |
| Post revaluation (`ledger.post_entry` grant, `post_revaluation`; not the preparer) | | | Y | | No | No |

*Amended 2026-10-16 (D6):* codenames are those of access §3.6; the role columns map to access roles `accountant`, `group_treasury`, `approver`, `viewer`, agent roles and `system_fx_import`.
| Run translation report | Y | Y | Y | Y | Scoped | No |
| View rates | Y | Y | Y | Y | Y | |
| Change entity functional currency | | Managing Partner only, before first posting | | | No | No |

## 8. Acceptance tests

Fixtures: ALM (parent, SAR, Sharia enforced), LZM (child, SAR), EGY (child, EGP functional, created only as data), template chart incl. section 5.0 accounts, 2027 monthly periods, currency seed, anonymised rate fixtures. All amounts `Decimal`.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | Current `ledger/tests.py` unchanged | Run `pytest -q` | All existing tests pass. `create_entry` without currency fields gives lines SAR, rate 1, `rate_source=functional`, txn = amount. |
| AT-2 | DB with posted SAR entries created before migration 0002 | Migrate | Lines have currency = entity base currency, rate 1, txn = debit + credit. `trial_balance` identical before and after. No entry status or number changed. |
| AT-3 | EUR→SAR spot 4.1000 on 2027-01-15 | Create and post F1 | Lines as F1; `exchange_rate` set; rate row now locked. |
| AT-4 | F1 posted; EUR→SAR 4.1250 on 2027-02-20 | Post receipt via `fx_settlement_lines` | Entry as F4; 4700 credit 250.00; 1200 EUR balance 0 and SAR balance 0. |
| AT-5 | GBP bill at 4.7000 | Pay at 4.7600 | Entry as F5; 5750 debit 120.00. |
| AT-6 | Same bill | Pay from SAR account | Entry as F6 posts (balanced in SAR, not per currency). |
| AT-7 | F1 posted | Two partial receipts per F10 | 1200 EUR and SAR balances both exactly 0 after the second; no residue. |
| AT-8 | F1 posted; closing 4.0800 on 2027-01-31 | `revalue_period(ALM, 2027-01)` by accountant A, then `post_revaluation(run, grant)` with approver P's `ledger.post_entry` grant *(amended 2026-10-16, D6)* | After step 1: draft F2 and run `draft`, nothing posted. After step 2: F2 posted dated 2027-01-31, status `reversed`; F3 posted dated 2027-02-01. TB at 31 Jan shows 5760 200.00; TB at 28 Feb after AT-4 shows 5760 0 and 4700 250.00. A manual entry with a revaluation line is rejected. |
| AT-9 | AT-8 done | Run revaluation for 2027-01 again | Rejected "already revalued". |
| AT-10 | 2027-01 closed, or 2027-02 closed, or 2028-01 periods missing for a December run | Run revaluation; *(amended 2026-10-16)* or prepare a draft while open, then close 2027-02 (or 2027-01) and call `post_revaluation` | Rejected; nothing posted (atomic): neither the entry nor the reversal; run stays `draft`. |
| AT-11 | No USD→SAR rate within 5 days before 2027-03-10 | Create an entry with a USD line; or run revaluation with an open USD balance | Rejected naming USD→SAR and the date (en/ar); nothing created. With a rate dated 2027-03-06 (4 days before): accepted, that rate used. With only SAR→USD: inverse used. |
| AT-12 | USD draft rated on 2027-03-10; draft date changed to 2027-03-20 (different rate) | Post | Rejected "rate does not match entry date". Re-rate, then post succeeds. A manual rate without reason: rejected; with reason and permission: posts and is flagged. |
| AT-13 | Entry with SAR debits ≠ credits but each currency balanced | Post | Rejected "does not balance". F9 lines via `create_entry`: rounding line 0.01 on 5770 added, posts. Difference 0.05 on 3 foreign lines: no line added, post rejected. |
| AT-14 | EUR line txn 100.00, rate 4.1000, debit 411.00 | Post | Rejected (expected 410.00). |
| AT-15 | SAR line with rate 1.0100, or txn ≠ debit | Post | Rejected. |
| AT-16 | Global EUR→SAR 4.1000; LZM override 4.1100 same date | ALM and LZM create EUR entries | ALM uses 4.1000, LZM 4.1100. ALM-only user creating an LZM rate: rejected. Non-group user creating a global rate: rejected. |
| AT-17 | F1 posted using a rate row | Edit or delete that row; add a corrected row; close 2027-01 then import rates for January | Edit/delete rejected. F1 lines unchanged. Import succeeds; posted lines unchanged. A new EUR entry dated in closed 2027-01: rejected "closed". |
| AT-18 | F4 posted | `reverse_entry(F4, date=2027-02-25)` | Reversal copies currency, txn, rate, kind, settles_line; posts; per-account balances net to zero in SAR and in EUR. |
| AT-19 | ALM Sharia enforced | EUR line on 5610; try to create an `ExchangeRate` with `rate_type="forward"`; `record_currency_exchange` with legs on two dates | All rejected. Same-date exchange posts as F8 with no FX line. |
| AT-20 | Currency seed | KWD line 1.234; JPY line 1.5; float 0.1 input; entity created with KWD functional currency | KWD accepted (D5); JPY and float rejected; KWD functional rejected (R4 kept by D5). *(Amended 2026-10-16.)* |
| AT-21 | Account 1100-USD with `currency=USD` | Post EUR line to it | Rejected. |
| AT-22 | Currency XXX `is_active=False` | Use on a line or rate | Rejected. |
| AT-23 | ALM has a posted entry | Change `base_currency` to USD | Rejected. A new entity with no postings: allowed. |
| AT-24 | EGY added as data (EGP, chart, periods), SAR→EGP rate | EGY posts a SAR-denominated payable and pays it at a new rate | Works with no code change; FX lines in EGP functional; numbers prefixed `EGY`. |
| AT-25 | ALM lends LZM USD 10,000 on 1220/2110 at different dates/rates | `intercompany_balances(as_of, by_currency=True)` | (ALM, LZM, USD) = −(LZM, ALM, USD) = 10,000.00 in USD. |
| AT-26 | *Deferred 2026-10-16 (D6, OQ-5): translation not built.* EGY TB in EGP; closing and average EGP→SAR rates | `translate_trial_balance(EGY, ..., "SAR")` | Translated TB balances in SAR with the difference on 3400; no journal entry created in EGY. ALM translated to SAR returns unchanged. |
| AT-27 | `ar` locale | Missing-rate error, currency names, new account names | Arabic text shown. |
| AT-28 | *Deferred 2026-10-16 (D6, OQ-2): feeds not built; CSV upload is tested instead.* ECB fixture file (EUR base, includes USD) and SAR/USD peg rate | `import_rates(source="ecb", file)` twice | Rows stored with `source=ecb`; EUR→SAR row stored with `source=derived` and reference; second import creates no duplicates. |
| AT-29 | Agent service user | Create a rate; post an entry; run revaluation | All rejected; creating a draft with table rates allowed and logged. |
| AT-30 | *Added 2026-10-16 (D5).* ALM (SAR); fixture KWD→SAR spot 12.2000; a test currency with `minor_units=4` | Post Dr 1200 KWD 1,234.567 / Cr 4100 KWD 1,234.567; then lines KWD 1.2345, USD 1.005, SAR txn 10.005; activate or use the 4-dp currency | First posts: txn stored as 1,234.567 (3 dp, read back unchanged), functional Dr/Cr 15,061.72 (= round_half_up(1,234.567 × 12.2000, 2)). KWD 1.2345, USD 1.005, SAR 10.005 rejected naming currency and allowed decimals (en/ar). 4-dp currency rejected (R8, R40, R41). |
| AT-31 | *Added 2026-10-16 (D6).* Draft run for ALM 2027-01 prepared by accountant A; A also holds `approver` in ALM; approver P | A calls `post_revaluation` with A's own `ledger.post_entry` grant; P calls it with a grant for another entry; P with a grant for an LZM entry; P with a grant for this draft; P calls it again | A refused (preparer, access R14); other-entry and LZM grants refused (access R23, R30); P's grant posts F2 and F3 in one transaction, run `posted`, `posted_by` = P; second call refused (run not `draft`, grant used) |
| AT-32 | *Added 2026-10-16 (D6).* Draft run for 2027-01; posted run for 2027-02 | `discard_revaluation` on each; `post_revaluation` on the discarded run | 2027-01 run `discarded`, draft entry not posted, TB unchanged; 2027-02 discard refused; posting a discarded run refused |
| AT-33 | *Added 2026-10-16 (R46).* Account 3400 in ALM and EGY | Create and post an entry with a line on 3400, as accountant and as MP | Rejected in both entities, en/ar message |
| AT-34 | *Added 2026-10-16 (D7; R44, R49).* Draft run for ALM 2027-01 prepared by A; approver P | P calls `post_entry` on the draft; A calls `update_draft` on it; a backdated EUR entry dated 2027-01-20 is posted, then P calls `post_revaluation`; a new closing-rate row is added instead, then P calls it | `post_entry` refused (`use_post_revaluation`); edit refused; both `post_revaluation` calls refused "discard it and re-run the revaluation" (en/ar), nothing posted, run `draft`, grant unused |
| AT-35 | *Added 2026-10-16 (D7; R24, R45).* Draft run X for 2027-01 | A discards X; discards it again; runs `revalue_period(ALM, 2027-01)` again | X `discarded` with `discarded_by` = A and `discarded_at`; its draft entry no longer exists and `X.entry` is null; second discard refused; new run created (`draft`); X still exists |
| AT-36 | *Added 2026-10-16 (D7; R15).* USD draft with `settlement` and `realised_fx` lines; plain USD draft | `rerate_entry` on each | First refused (en/ar, `rerate_settlement`), draft unchanged; second re-rated |
| AT-37 | *Added 2026-10-16 (D7; R18), PostgreSQL.* F1 open EUR 10,000.00 | Two sessions each post a settlement of EUR 10,000.00 at the same time; then partial receipts per F10 | Postings are serialised on the settled line's lock; together they never clear more than is open (the second is refused); after F10 both EUR and SAR balances of the 1200 item are exactly 0.00 |
| AT-38 | *Added 2026-10-16 (D7; R28).* ALM top-level parent; LZM child; T2 is `group_treasury` on ALM | T2 creates a global rate with `group=LZM`; with `group=ALM`; with no `group` when exactly one top-level parent exists | First refused ("not the top-level group parent entity", en/ar); second and third allowed |
| AT-39 | *Added 2026-10-16 (D8; R50).* Draft run X for ALM 2027-01 run by A | Set `status` to `posted` or `discarded` and save; set `posted`, `posted_by`, `posted_at` and save; set `discarded_by`/`discarded_at` on the draft and save; `QuerySet.update(status="posted")`; `delete()`; create or `bulk_create` a run as `posted` or `discarded`; `discard_revaluation(X, user=A)` | All direct writes refused (`LedgerError`, en/ar), X stays `draft`, no run created as non-draft; discard sets `discarded`, `discarded_by` = A, `discarded_at` within 10 s of now |

## 9. Open questions

**BLOCKING** = builder cannot build that part. **Default** = safe default used in the rules.

| # | Question | Owner | Status |
|---|---|---|---|
| OQ-1 | **Transaction amount decimals.** CLAUDE.md rule 7 said amounts are stored with 2 decimals, but ISO 4217 has 0-dp (JPY) and 3-dp (KWD, BHD, OMR, JOD) currencies. | Managing Partner / accountant + Dev | **Resolved by D5** (recommendation adopted; CLAUDE.md rule 7 amended): `txn_amount` `Decimal(21,3)`, validated against `minor_units` (R8, R40); functional 2 dp (R4 kept). Effect on invoicing/billing document amounts: invoicing OQ-48, billing OQ-29. *Note 2026-10-17 (D9):* both resolved; documents and contracts use 3-dp amounts for 3-decimal currencies. 0-decimal currencies on documents: invoicing OQ-58. |
| OQ-2 | Rate sources: which free source is primary per currency pair (SAMA, ECB, CBE, CBUAE, PBOC), whether machine-readable feeds exist and their licence terms **[CC-verify]**, import time of day, and source precedence. | Accountant | **BLOCKING** for `import_rates`. Manual entry and CSV upload are not blocked. |
| OQ-3 | Fallback when no rate on the date (default: latest earlier rate within 5 calendar days); limit on manual line rate deviation from the table rate (default: none, flagged). | Accountant | Default |
| OQ-4 | Are rates global or per entity? Default: one global table, optional entity override (e.g. EGY using CBE). Who maintains the global table. | Accountant + Managing Partner | Default |
| OQ-5 | Translation method for foreign-functional entities: average rate (monthly or period) vs transaction-date rates; historical equity rates source; goodwill; recycling of reserve on disposal (IAS 21 / IFRS for SMEs s30 **[CC-verify]**). EGP hyperinflation assessment (IAS 29 / s31) **[CC-verify]**. | Accountant / auditor | **BLOCKING** for translation (R30), not for posting. |
| OQ-6 | Approve proposed accounts 3400, 4700, 4710, 5750, 5760, 5770 (or one net FX account) and the `monetary` flag list. | Accountant | **BLOCKING** for FX gain/loss, rounding and revaluation postings. *Note 2026-10-17 (D10):* still proposed; review by the accountant/CFO in the Word document being prepared. Built code already uses 4700 / 5750 (migration `ledger/0005_fx_accounts`); they stay marked proposed until that review. |
| OQ-7 | Confirm group presentation currency SAR. | Managing Partner | Default SAR |
| OQ-8 | Rounding mode `ROUND_HALF_UP` vs half-even; rounding tolerance per entry; whether rounding goes to 5770 or the realised FX accounts. | Accountant | Default half-up, 0.01 × foreign lines, 5770 |
| OQ-9 | Re-running a revaluation after a rate correction (cancel procedure, given the original becomes `reversed`). | Accountant + Dev | Default: no re-run; manual correcting journal before close |
| OQ-10 | Keep `Entity.base_currency` (as functional currency, converted to FK) or rename to `functional_currency`. | Dev | Default: keep name (invoicing.md uses it) |
| OQ-11 | Foreign-currency bank accounts: sub-accounts per currency under 1100 (e.g. 1101 USD) with `Account.currency` set, or one 1100 holding many currencies. Links invoicing OQ-20. | Accountant | Default: one 1100, any currency; sub-accounts allowed |
| OQ-12 | Offshore CNH vs CNY for China dealings; any non-ISO currency codes needed. | Accountant | Default: ISO only (CNY) |
| OQ-13 | Procedure for changing an entity's functional currency after postings. | Accountant / auditor | Default: blocked (R5) |
| OQ-14 | Sharia board: confirm FX gain/loss classification (R33), T+2 as spot (R35), revaluation of intercompany qard/loans in foreign currency. Forwards/hedging stay out unless approved. **Needs Sharia board sign-off.** | Managing Partner + Sharia board | Default: R33 to R36 as written |
| OQ-15 | `JournalEntry` has only an English `description`; add `description_ar` for system-generated entries (reversals, revaluations)? | Dev | Default: Arabic in reason text |
| OQ-16 | Migration of foreign open items from Daftra/SAP B1: original rate kept, or restated at cutover rate. | Accountant + migration-agent | Default: original currency, amount and rate; tie out per currency |
| OQ-17 | Functional currency of each subsidiary (Egypt EGP?, China CNY?, UAE AED?) under IAS 21 indicators **[CC-verify]**. | Accountant / auditor | **BLOCKING** for setting up those entities, not for the build |
| OQ-18 | *Added 2026-10-16 (D6).* Does a `discarded` revaluation run free the period for a new run (R24)? Who may discard a draft run (preparer, approver, both)? | Accountant + Dev | Default: discarded run kept for audit and does not count toward R24; `ledger.revalue` holders may discard. *Note 2026-10-16 (D7):* built with this default; the discarded run's draft entry is deleted (R45). Still for the accountant to confirm |
| OQ-19 | *Added 2026-10-16 (D8).* Should `ledger_revaluationrun` get a PostgreSQL trigger (as journal entries have, access.md R43, R44) so raw SQL cannot change a run's status or audit fields? | Dev + code-reviewer | Default: model and queryset guards only, as built; DB role privileges (access.md OQ-10) |

**Blocking:** OQ-2 (automatic import; not built, CSV only, D6), OQ-5 (translation; not built, D6), OQ-6 (FX postings; 3400 non-postable meanwhile, R46), OQ-17 (subsidiary setup). **Sharia board sign-off:** OQ-14 and any forward contract.

## Appendix: existing code that must change

| File | Change |
|---|---|
| `core/models.py` | New `Currency`; `Entity.base_currency` → FK Currency (functional, 2-dp check, locked after first posting); `presentation_currency`. |
| `ledger/models.py` | New `ExchangeRate`, `RevaluationRun`; `Account.is_monetary`, `Account.currency`; `JournalLine` fields and checks (3.5), `txn_amount` `Decimal(21,3)` (D5). |
| `ledger/services.py` | `create_entry`: currency/rate inputs, rate resolution, rounding line. `validate_entry`: R6 to R10, R15, account currency. `reverse_entry`: **must copy the new line fields** (today it copies only account, description, debit, credit, counterparty). `intercompany_balances`: per currency. `trial_balance`: optional per-currency columns (functional result unchanged). New: `get_rate`, `import_rates`, `fx_settlement_lines`, `record_currency_exchange`, `revalue_period`, `translate_trial_balance`. `post_entry` itself unchanged. |
| `ledger/coa_template.py`, `ledger/services_setup.py` | Section 5.0 accounts and `monetary` flag; installer sets `is_monetary`/`currency`. |
| `ledger/migrations/0002_*` | Schema + data migration (R38), plus data migration for new accounts and monetary flags on existing entities. |
| `ledger/tests.py` | Unchanged; new tests in a separate file (e.g. `ledger/tests_fx.py`). |
