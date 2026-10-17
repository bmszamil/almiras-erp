# Banking: bank accounts, statement import and reconciliation (Track D)

Django app: `banking`. Status: **draft spec**. Written 2026-10-17 by the spec-writer agent.
Depends on: `core` (`Entity`, `Currency`), `core.access` (`docs/specs/access.md`: `require`, `scope`, segregation R14/R35, document registry R34 to R36, masking R29), `ledger.services` (`create_entry(post=True, grant, document)`, `reverse_entry`, `close_period` + `pending_documents`, `trial_balance(by_currency=True)`, `fx_settlement_lines`, R20 currency exchange), `docs/specs/ledger-fx.md` (rates, decimals, realised FX), `docs/specs/invoicing.md` (`CustomerReceipt`, `VendorPayment`, D26/R46), `docs/specs/payroll.md` (`PayrollPayment`, `WpsFile`).
**[CC-verify]** = format or regulatory statement written from memory; the compliance-checker verifies it before the module-builder relies on it.

## 1. Purpose

The banking module keeps a register of each entity's bank accounts, imports bank statements (MT940, ISO 20022 camt.053, bank CSV exports), matches statement lines to what the ledger already holds (customer receipts, vendor payments, payroll payments, bank documents, manual journals), lets staff create the missing bank-originated transactions (bank charges, Islamic deposit profit, conventional interest to purify, transfers between own accounts, intercompany transfers) as **draft documents** that are approved and posted through the ledger document registry, and produces a monthly reconciliation statement per bank account that an approver signs off, locking that period's matches. It works the same for Almiras, every subsidiary, partly-owned and minority entity, and any entity added later as an `Entity` row plus setup data. Every record belongs to one entity; one entity never sees, matches or posts to another entity's bank accounts. Sharia is enforced per entity: no bank credit labelled as conventional interest becomes income, and prohibited accounts are never used. Matching and reconciliation post nothing; only approved `BankTransaction` documents post. All user-facing text is English and Arabic. No payment is initiated from the ERP and no bank is connected automatically in this spec.

## 2. Decisions

| # | Decision | Who | Date |
|---|---|---|---|
| B1 | The group banks with Riyad Bank, Saudi National Bank (SNB), Al Rajhi Bank, Bank AlJazira and Alinma Bank (given as the salary banks). | Project owner (payroll P15) | 2026-10-17 |
| B2 | CLAUDE.md rules: posted entries never edited (reversals only); balanced 2+ one-sided lines; posting only through `ledger.services`; closed periods reject postings; no posting to another entity's accounts; Sharia per entity (prohibited accounts blocked, non-compliant income flagged for purification); intercompany lines carry a counterparty; money `Decimal`, currency explicit, SAR 2 dp, foreign up to 3 dp per currency; every record has `entity`; bilingual text; new entities are data; business logic in `services.py`; agents never use production data. | CLAUDE.md | in repo |
| B3 | Access model: staff create, approvers approve, posting is automatic on approval through the ledger document registry, where each document builds its own lines; creator ≠ approver; agents create drafts only and never approve or post; every action logged. | Project owner (access A1, A6, A7, A13 to A15, R34 to R36) | 2026-10-16 |
| B4 | Multi-currency is built: per-line transaction currency/amount/rate, rate table, realised FX on settlement, spot currency exchange (no FX at the exchange itself), period-end revaluation. 3-decimal currencies are used. | Project owner (ledger-fx D1, D5, D9) | 2026-10-16 / 2026-10-17 |
| B5 | Hosting in KSA. | Project owner (caller brief) | 2026-10-17 |
| B6 | Open source only. Bank APIs, SWIFT and host-to-host links are reached only through the integration layer, never as a dependency of this module. | CLAUDE.md; project owner (caller brief) | in repo / 2026-10-17 |
| B7 | A customer receipt may be posted by its recorder only when it has no bank charge; otherwise a second person posts. Every other document keeps creator ≠ poster. | Project owner (invoicing D26, access A16, R46; narrowed as built in `invoicing/services.py` `_receipt_creator_may_post`) | 2026-10-17 |
| B8 | Bank charges are booked gross to 5700, no VAT split. Bank account on receipts/payments: 1100 or a selectable asset account. Bank payment files are not in invoicing's scope. | Project owner (invoicing D19: OQ-20, OQ-21, OQ-23) | 2026-10-16 |
| B9 | The five banks' WPS/salary file formats will be shared later. | Project owner (payroll P18) | 2026-10-17 |
| B10 | Proposed account codes stay proposed until the accountant/CFO review (Word document). Applies to FX accounts used here (4700, 5750, 5770). | Project owner (ledger-fx D10, invoicing D29) | 2026-10-17 |

Nothing else is decided. Every other choice is a default marked "(OQ-n)" and listed in §9.

## 3. Data model

Money: account-currency amounts `Decimal(21,3)`, decimals used ≤ the currency's `minor_units` (ledger-fx R8); functional amounts `Decimal(18,2)`. Every model below has `entity` (FK `core.Entity`, PROTECT) unless stated. "Bi" = English and Arabic fields. "PD" = masked personal/sensitive data (access R29).

### 3.1 `Bank` (global reference data; no `entity`, like `Currency`)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| code | Slug(20), unique | Y | | `riyad`, `snb`, `alrajhi`, `aljazira`, `alinma` (same codes as payroll `salary_bank`) |
| name_en / name_ar | Char(120) | Y | Bi | Arabic names reviewed by the accountant |
| country | Char(2) ISO 3166 | Y | | `SA` for B1 banks |
| sa_bank_code | Char(2), null | N | | 2-digit bank identifier inside a Saudi IBAN (R3) **[CC-verify]**; seeded `unverified` |
| swift_bic | Char(11) | N | | **[CC-verify]**, seeded `unverified` |
| verified | Bool | Y | | Set by the accountant after the compliance check |
| is_active | Bool | Y | | |

Seed: the five B1 banks. Other banks (e.g. for foreign subsidiaries) are data.

### 3.2 `BankAccount`

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Owner; never changes |
| code | Slug(30), unique per entity | Y | | e.g. `RIYAD-SAR-01` |
| name_en / name_ar | Char(120) | Y | Bi | |
| bank | FK Bank, PROTECT | Y | | |
| iban | Char(34), encrypted at rest | Y if `bank.country` uses IBAN | | **PD**. Stored normalised (no spaces, upper case). Validated R3 |
| iban_hash | Char(64) | Y | | HMAC-SHA256 of the IBAN (key outside the DB); used for uniqueness and lookup |
| iban_last4 | Char(4) | Y | | Shown in masked form `SA•• •••• … 1234` |
| account_number | Char(40), encrypted | Y if no IBAN | | **PD**. Non-IBAN countries (OQ-4) |
| currency | FK Currency | Y | | One currency per row. A multi-currency IBAN = one row per currency |
| gl_account | FK `ledger.Account`, PROTECT | Y | | Same entity, type asset, postable, active; code 1100 or a child of 1100 (R5); `Account.currency` = `currency` (R6) |
| sharia_basis | Choice `islamic`, `conventional` | Y | | Governs profit vs interest classification (R27, R28) |
| statement_format | Choice `mt940`, `camt053`, `csv_<bank>` | Y | | Per-bank variants OQ-1 |
| opening_statement_date / opening_statement_balance | Date / Decimal(21,3) | Y | | Bank balance the first imported statement must open with (R12) |
| active_from / active_to | Date / Date null | Y / N | | No statement line or document outside this range |
| status | Choice `draft`, `active`, `closed` | Y | | Activation and changes to IBAN, currency, GL account: R7 (OQ-9) |
| created_by / modified_by / modified_at / approved_by / approved_at | FK User / DateTime | Y / N | | `created_by` non-null |

Constraints: unique `(iban_hash, currency)` across **all** entities (one IBAN belongs to one entity); unique active `gl_account` (one GL account per active bank account, R5).

### 3.3 `StatementImport` (one per accepted file; append-only)

| Field | Type | Req | Notes |
|---|---|---|---|
| entity, bank_account | FK | Y | Same entity |
| file_sha256 | Char(64) | Y | Unique per bank account (R9) |
| file | Encrypted file storage (KSA) | Y | Original bytes kept (retention OQ-15) |
| file_name | Char(200) | Y | |
| format | Choice as 3.2 | Y | |
| statement_id / sequence_no | Char(35) / Int null | Y / N | MT940 `:20:`/`:28C:`; camt.053 `Stmt/Id`, `ElctrncSeqNb` **[CC-verify]**; CSV: entered or derived (OQ-1) |
| period_start / period_end | Date | Y | |
| currency | FK Currency | Y | = bank account currency |
| opening_balance / closing_balance | Decimal(21,3), signed | Y | From the file; CSV without balances: entered by the importer (OQ-24) |
| line_count, total_credits, total_debits | Int / Decimal(21,3) | Y | |
| accepted_duplicates | JSON | N | Lines accepted despite R13, with reason |
| imported_by / imported_at / source | FK User / DateTime / Char | Y | `source` = `human` (agents cannot import, R40) |

Refused imports store no `StatementImport`; the refusal is written to `BankingEvent` (3.9) with the hash and reason.

### 3.4 `StatementLine` (immutable after import)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity, bank_account, statement | FK | Y | | Same entity |
| line_no | Int | Y | | Order in the file; unique per statement |
| booking_date / value_date | Date | Y / N | | |
| direction | Choice `credit` (money in), `debit` (money out) | Y | | |
| amount | Decimal(21,3) > 0 | Y | | Account currency; decimals ≤ `minor_units` |
| currency | FK Currency | Y | | = bank account currency |
| original_amount / original_currency | Decimal(21,3) / FK, null | N | | When the bank shows a converted foreign amount |
| bank_reference | Char(35) | N | | Bank's own reference |
| end_to_end_id / customer_reference | Char(35) | N | | MT940 `:61:` subfield / camt `EndToEndId` **[CC-verify]** |
| transaction_code | Char(10) | N | | MT940 type code / camt BTC **[CC-verify]** |
| counterparty_name / counterparty_iban | Char(140) / Char(34), encrypted | N | | **PD**, masked |
| narrative | Text | N | | Raw text, Arabic and/or English as sent; **PD**-masked in lists |
| line_hash | Char(64) | Y | | SHA-256 of (bank account, booking date, direction, amount, bank reference, normalised narrative) for R13 |
| duplicate_of | FK self, null | N | | Set only for lines accepted under R13 |

No status field: a line's state (`unmatched`, `partly_matched`, `matched`, `locked`) is computed from `MatchPart` rows (R17). `save` after insert, `delete`, `QuerySet.update`/`delete` refused; PostgreSQL trigger refuses UPDATE/DELETE (R15).

### 3.5 `Match` and `MatchPart`

`Match` (one match group):

| Field | Type | Req | Notes |
|---|---|---|---|
| entity, bank_account | FK | Y | |
| rule | Choice `M0`, `M1`, `M2`, `M3`, `manual` | Y | §4 R19 to R23 |
| confidence | SmallInt 0 to 100 | Y | 100 for M0/M1; manual = null-equivalent 0 |
| status | Choice `proposed`, `confirmed`, `rejected`, `undone` | Y | `proposed` = suggestion |
| proposed_by / proposed_at | FK User / DateTime | Y | Human, agent or the importing user (auto) |
| confirmed_by / confirmed_at | FK User / DateTime | N | Human only (R40). M1 auto-confirm records the importing user and `auto=true` |
| undone_by / undone_at / undo_reason | FK / DateTime / Char(300) | N | One write only |
| reconciliation | FK Reconciliation, null | N | Set at approval; then frozen (R35) |

`MatchPart`: match (FK), `statement_line` (FK, null) **or** `journal_line` (FK `ledger.JournalLine`, null) — exactly one; `amount` Decimal(21,3) > 0 in account currency; `side` (`bank_in`, `bank_out` for statement lines; `book_debit`, `book_credit` for journal lines). Append-only.

### 3.6 `BankTransaction` (registered ledger document)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity, bank_account | FK | Y | | |
| number | Char(40) | on posting | | `{entity.code}-BTX-{year}-{000001}`, gap-free per entity and year |
| kind | Choice `bank_charge`, `islamic_profit`, `conventional_interest_credit`, `own_transfer`, `intercompany_transfer` | Y | | Each kind builds fixed lines (§5) |
| statement_line | FK StatementLine, null | N | | The line it was created from (same entity, bank account) |
| date | Date | Y | | Default = line booking date |
| direction | Choice `in`, `out` | Y | | Fixed per kind except transfers |
| amount / currency | Decimal(21,3) / FK | Y | | Currency = bank account currency |
| to_bank_account | FK BankAccount, null | Y if `own_transfer` | | Same entity, active, ≠ `bank_account` |
| to_amount | Decimal(21,3), null | Y if currencies differ | | Amount received in the other currency (R31) |
| counterparty_entity | FK Entity, null | Y if `intercompany_transfer` | | Same group, ≠ entity (R32) |
| intercompany_account | FK `ledger.Account`, null | Y if `intercompany_transfer` | | Same entity, `is_intercompany` (1210, 1220, 2110) |
| settles_line | FK `ledger.JournalLine`, null | N | | Open intercompany item cleared (realised FX, R33) |
| description_en / description_ar | Char(300) | Y / Y | Bi | |
| requires_purification | Bool | Y | | Set true for `conventional_interest_credit` (R28) |
| status | Choice `draft`, `submitted`, `posted`, `reversed`, `discarded` | Y | | |
| source | Char | Y | | `human`, `agent:<name>` |
| created_by (non-null), modified_by, modified_at, submitted_by/at, approved_by/at, posted_by/at | FK User / DateTime | | | Registry needs non-null `created_by` (access R34) |
| fx_exchange_rate | FK ExchangeRate, null | N | | Pinned at approval (as invoicing `MoneyMovement`) |
| journal_entry / reversal_entry | FK JournalEntry, null | N | | Written by the posting service only |

Registered in `banking/apps.py`: `documents.register(BankTransaction, post_permissions={"banking.approve_bank_transaction"}, reverse_permissions={"banking.reverse_bank_transaction"}, build_lines=bank_transaction_lines, validate_lines=check_bank_transaction_lines, pending_documents=submitted_bank_transactions)`. No `creator_may_post`.

### 3.7 `Reconciliation`

| Field | Type | Req | Notes |
|---|---|---|---|
| entity, bank_account | FK | Y | |
| period | FK `ledger.FiscalPeriod` (same entity) | Y | Unique `(bank_account, period)` among non-`superseded` rows |
| as_of | Date | Y | = period end |
| bank_balance | Decimal(21,3) | Y | From statements (R36) |
| book_balance / book_balance_functional | Decimal(21,3) / Decimal(18,2) | Y | GL account balance (R37) |
| outstanding_book_debits / outstanding_book_credits / unmatched_bank_credits / unmatched_bank_debits | Decimal(21,3) | Y | |
| difference | Decimal(21,3) | Y | Must be 0 to approve (R38) |
| items | JSON snapshot | Y | Each outstanding item: type, id, date, amount, reference, note |
| snapshot_sha256 | Char(64) | Y | Of figures + items; recomputed and compared at approval |
| status | Choice `draft`, `submitted`, `approved`, `superseded` | Y | |
| prepared_by, submitted_by, approved_by (+ `_at`) | FK User / DateTime | | `prepared_by` non-null |
| reopened_by / reopened_at / reopen_reason | | N | OQ-11 |

`ReconciliationNote`: entity, reconciliation, statement_line or journal_line, note_en / note_ar (one required), created_by. Explains an outstanding item (R38).

### 3.8 `BankingSettings` (one per entity)

| Field | Type | Default | Notes |
|---|---|---|---|
| m1_date_window_days | SmallInt | 7 | OQ-8 |
| m2_date_window_days | SmallInt | 3 | OQ-8 |
| m3_max_items | SmallInt | 5 | OQ-8 |
| auto_confirm_m1 | Bool | True | OQ-8 |
| changed only by | | | `approver` (`banking.manage_settings`), logged |

### 3.9 `BankingEvent` (append-only audit log)

entity, at, actor (FK User, null only for none), actor_kind, event (`bank_account_created`, `bank_account_changed`, `bank_account_activated`, `iban_viewed`, `import_accepted`, `import_refused`, `import_duplicate_file`, `duplicates_accepted`, `match_proposed`, `match_confirmed`, `match_rejected`, `match_undone`, `bank_transaction_created`, `…_submitted`, `…_approved`, `…_discarded`, `…_reversed`, `reconciliation_prepared`, `…_submitted`, `…_approved`, `…_reopened`, `settings_changed`), object type/id, details JSON (before/after; **never** a full IBAN, account number or counterparty IBAN: last 4 only), request_id, source. Same guards as `AccessLog` (model + PostgreSQL trigger). Denied `require` calls are logged by `core.access` (`permission_denied`); unmasked IBAN views also write an `AccessLog` row (access R29).

## 4. Rules

Enforcement: **S** = `banking/services.py`; **M** = model `save`/`delete`/`clean`; **DB** = constraint or PostgreSQL trigger; **L** = ledger (`validate_entry`, registry); **Q** = `core.access.scope`; **T** = test that scans code.

**Entity separation and access**
1. Every banking service calls `core.access.require(user, entity, permission)` for the entity that owns the record; every list, detail, API and admin queryset goes through `scope(...)`; another entity's record answers 404. **[S, Q; AT-24]**
2. A bank account, its statements, lines, matches, documents and reconciliations all belong to one entity. A match part, `BankTransaction` line, `to_bank_account`, `gl_account` or reconciliation from another entity is refused. **[S + M clean; L rejects foreign accounts; AT-24]**

**Bank accounts**
3. IBAN validation: ISO 13616 (country code, 2 check digits, mod-97 = 1), allowed characters A–Z0–9. For `SA`: total length 24, positions 5–6 = the bank's `sa_bank_code`, remaining 18 characters alphanumeric **[CC-verify: SA IBAN structure and bank codes]**. Non-SA IBANs: length per country from an ISO 13616 registry fixture **[CC-verify]**; non-IBAN countries use `account_number` (OQ-4). Errors en/ar. **[M clean + S; AT-1]**
4. IBAN and account numbers are stored encrypted and shown masked (last 4) everywhere (UI, API, admin labels, exports, `BankingEvent`, error messages). Unmasked only for users holding `banking.view_iban`; each unmasked view is logged (`iban_viewed` + `AccessLog`). Statement counterparty names, IBANs and narratives are masked in lists for users without `banking.view_iban`. **[S + admin; AT-2]**
5. `gl_account` is in the same entity, asset, postable, active, not Sharia-prohibited, coded 1100 or a child of 1100; one active bank account per GL account, so the GL balance is that account's book balance. Default layout: one sub-account per bank account under 1100 (OQ-5). **[M clean + DB partial unique; AT-1]**
6. `gl_account.currency` must equal the bank account currency (ledger-fx R9 then keeps every line on it in that currency). A sub-account without currency is set to it at setup, before any posting. **[S; AT-1, AT-18]**
7. A bank account is created as `draft` by a preparer and becomes `active` only when an approver who is not its creator or last modifier approves it (`banking.approve_bank_account`, A). A change to IBAN, currency or GL account of an active account is a new draft version approved the same way; currency and GL account cannot change once a statement is imported or a line posted on the GL account. Default (OQ-9). **[S + access R14; AT-1]**
    *Amended 2026-10-17 (invoicing D31b, as built):* `is_purification_account` designates the entity's **purification account**: at most one draft or active per entity; Saudi bank, SAR, IBAN `SA` + 22 digits with valid ISO 13616 check digits (`iban.saudi_problems`), GL account **1140** (proposed, a sub-account of 1100, so R5 holds); GL 1140 is used by no other bank account. The designation takes effect on the R7 approval, and changes only through an approved change request (`BankAccountChange.purification_account`); an account still holding money on 1140 is not closed (`purification_balance`). Codes `purification_account_invalid`, `nothing_to_change`. `purification_account(entity, user)` returns the active one (`banking.view`) for invoicing's funding and payout rows (invoicing R54 amendment D31b). PostgreSQL: migration `0004` (trigger + partial unique constraint). **[S + DB; tests `test_purification_account.py`]**
8. Statements, documents and matches are refused for a bank account that is not `active`, or for dates outside `active_from`–`active_to`. **[S; AT-1]**

**Statement import**
9. Import is idempotent: the SHA-256 of the file bytes is computed first; a file already imported for that bank account returns the existing `StatementImport`, creates nothing and logs `import_duplicate_file`. **[S + DB unique; AT-4]**
10. Parsing happens in `banking/parsers/` (one parser per format, open-source libraries only, licence checked by the code-reviewer), called from services. A parser returns header (statement id, sequence, period, currency, opening/closing balance) and lines, or fails; nothing is stored until all checks R11 to R14 pass, in one transaction. Unknown or unsupported format: refused. **[S; AT-3, AT-9]**
11. Internal consistency: opening balance + credits − debits = closing balance, exactly; the statement currency equals the bank account currency; every amount respects the currency's decimals; every line date is within the statement period. Else refused, nothing stored. **[S; AT-6]**
12. Continuity: the statement's opening balance equals the closing balance of the account's latest imported statement (or `opening_statement_balance` for the first); its `period_start` ≥ the latest `period_end` (same day allowed only with a higher `sequence_no`); a statement older than the latest is refused ("import in order"). The same `statement_id` (+ sequence) with a different file hash is refused. **[S; AT-5, AT-6, AT-7]**
13. Duplicate lines: a line whose `line_hash` equals that of an already imported line of the same account, or whose non-empty `bank_reference` + amount + direction match one, refuses the whole import listing those lines. The importer may re-submit with `accept_duplicates=[line numbers]` and a reason; those lines are stored with `duplicate_of` set and logged (`duplicates_accepted`). Identical lines inside one file are not duplicates. **[S; AT-8]**
14. A statement for a period already covered by an `approved` reconciliation of that account is refused. **[S; AT-22]**
15. Statement lines and imports are immutable after import: no edit, no delete, by model, queryset, admin or (on PostgreSQL) raw SQL. A wrong statement is handled by matching/notes, never by changing lines. **[M + DB trigger; AT-10]**

**Matching**
16. Book items are posted journal lines (entry status `posted` or `reversed`) on the bank account's `gl_account`, dated within the account's active range. Their amount is `txn_amount` (account currency); debit = money in, credit = money out. Matching never creates, changes or posts a journal entry. **[S; AT-11]**
17. A match group balances in the account currency: Σ statement credits − Σ statement debits = Σ book debits − Σ book credits, using the `MatchPart.amount`s. Each part's amount ≤ the open (not yet confirmed-matched) amount of its line. A line can be split across several groups (partial match); its remainder stays open. Only `confirmed` groups consume open amounts. **[S with row locks on the lines; AT-14]**
18. Matching runs after each import and on demand, in rule order M0, M1, M2, M3. It only proposes; rules R19 to R23 say which proposals are confirmed automatically. **[S; AT-11 to AT-14]**
19. **M0 — book reversal pair:** a posted line and the line of its reversal entry, neither matched, form a group with no statement line; confidence 100; auto-confirmed. **[S; AT-27]**
20. **M1 — exact amount + reference:** one open statement line and one open book item with the same amount and direction, booking date within ±`m1_date_window_days`, where the line's `bank_reference`, `end_to_end_id`, `customer_reference` or narrative contains, as a whole token, one of the book item's references: the source document number (`CustomerReceipt.number`, `VendorPayment.number`, `PayrollPayment` id/number, `BankTransaction.number`), its `bank_reference`, the numbers of the invoices allocated to a receipt, the journal entry number, or (for a `BankTransaction` created from this very line) the `statement_line` link. Confidence 100. Auto-confirmed only if `auto_confirm_m1` is on and exactly one candidate exists on each side; otherwise proposed. **[S; AT-11, AT-12, AT-29]**
21. **M2 — amount + date window:** same amount and direction, booking date within ±`m2_date_window_days`, no reference match. Confidence 70 if one candidate on each side, 40 if several (each proposed). Never auto-confirmed. **[S; AT-13]**
22. **M3 — grouped (one-to-many, many-to-one):** one statement line = sum of 2 to `m3_max_items` open book items (or the reverse), within the M2 window; confidence 90 if every item's reference is found in the line(s), else 50. Never auto-confirmed. **[S; AT-14]**
23. **Manual match:** a user with `banking.match` builds any group meeting R17 (including mixed directions, e.g. a gross receipt plus a bank-charge document against one net credit). Confirming a proposal, rejecting it, or undoing a confirmed group is logged; undo needs a reason and is refused once the group is locked (R35). **[S; AT-14, AT-22]**
24. No rule auto-creates or auto-posts a document. Creating a `BankTransaction`, a receipt or a payment from an unmatched line is always an explicit user (or agent draft) action (OQ-18). **[S; T: no `create_entry`/`BankTransaction` creation reachable from the matching engine; AT-11]**

**Bank transactions (missing transactions)**
25. A `BankTransaction` is created as `draft` (from an unmatched statement line or manually), edited and submitted by a preparer (`banking.create_bank_transaction`, `banking.submit_bank_transaction`), and approved by a different user with `banking.approve_bank_transaction` (A, P). Approval posts at once with `create_entry(entity, date, …, lines=None, post=True, grant=<approval grant>, document=<the transaction>)`; the registered `build_lines` builds the lines of §5; nothing else may supply lines. The creator, submitter and last modifier cannot approve (access R14, R35; no `creator_may_post`). A document books once (access R36). **[S + L registry; AT-15]**
26. `bank_charge`: direction `out`; Dr 5700 / Cr bank GL, gross, no VAT split (B8; OQ-13). **[S build_lines; AT-15]**
27. `islamic_profit`: direction `in`; allowed only on a bank account with `sharia_basis = islamic`; Dr bank GL / Cr 4600. **[S; AT-16]**
28. `conventional_interest_credit`: direction `in`; any bank credit described by the bank as interest, and any return credited on a `conventional` account, must use this kind, never `islamic_profit` or any income account. It books Dr bank GL / Cr 2600 Charity payable (purification) (default, OQ-6), sets `requires_purification = True`, and appears on the purification report; payout is invoicing E15 (approved, human only). Applies in every entity; in a Sharia-enforcing entity no other treatment is offered. **[S build_lines + validate_lines (no income line); AT-17]**
29. Sharia-prohibited accounts (`sharia_prohibited`, e.g. 2410, 5610) are never in `build_lines` output, never a `gl_account`, and the ledger refuses them anyway in Sharia-enforcing entities (`validate_entry`). A conventional interest or penalty **debit** charged by a bank has no `BankTransaction` kind: in a Sharia-enforcing entity it cannot be booked (5610 is blocked) and stays an outstanding, noted item flagged `sharia_exception` on the reconciliation until OQ-7 is decided. **[L + S; AT-30]**
30. `own_transfer`, same currency: Dr `to_bank_account` GL / Cr `bank_account` GL (direction `out`), or the mirror when created from the receiving line. Both accounts in the same entity; a transfer to another entity's account is an `intercompany_transfer`. One document only: the other account's statement line is matched (M1/M2) to the same entry's other line; no second document. **[S; AT-21]**
31. `own_transfer`, different currencies: one entry, both legs on the same date, priced exactly as ledger-fx R20 (`record_currency_exchange`): the foreign leg carries the actual bank rate (`rate_source=manual`, reason "Actual bank rate / سعر البنك الفعلي"); when neither leg is functional, the sold leg is at the day's table rate and the bought leg at the implied rate; no FX gain/loss at the exchange itself (ledger-fx R20, R35). Legs on two dates are refused (spot only). The difference against the foreign account's carrying value is picked up by revaluation (ledger-fx R21). Builder note: `build_lines` reuses the R20 pricing as a pure helper, not by calling `record_currency_exchange` (which creates a manual draft). Default (OQ-20). **[S + L; AT-18]**
32. `intercompany_transfer`: one line on `intercompany_account` (1210, 1220 or 2110) with `counterparty_entity` set; the counterparty is another active entity of the same group (`counterparty_choices`), never the entity itself. It books only in the entity that owns the bank account; the counterparty records its side with its own document from its own statement (no automatic mirror, by analogy with invoicing D23; OQ-25). Mirror positions are checked with `intercompany_balances(by_currency=True)`. **[S + L rule 6; AT-20]**
33. Foreign-currency transfers that settle an open intercompany item (`settles_line` set) clear it at its carrying rate and book the difference to 4700 / 5750 as a `realised_fx` line (ledger-fx R18, `_settlement_for_document` inside `build_lines`). Without `settles_line`, the intercompany line is at the spot table rate of the date (no FX line). **[L + S; AT-19]**
34. Foreign lines without a rate on the document date make approval fail; nothing posts, the grant stays unused, the document stays `submitted` (ledger-fx R14). The rate row used is pinned on the document at approval. Approval into a closed or missing period fails the same way (ledger R4). **[L + S; AT-18, AT-26]**
35. A posted `BankTransaction` is corrected only by `reverse_entry` with a `banking.reverse_bank_transaction` grant (status → `reversed`) and, if needed, a new document. Reversal is refused while its bank line is in a `confirmed` match (undo the match first) and always once that match is locked; then the correction is a new document in an open period. A draft may be discarded (kept for audit, status `discarded`). **[S + L; AT-27]**

**Reconciliation**
36. Bank balance at period end = opening balance of the statement covering the period end + net of its lines booked ≤ period end. The account's statements must be continuous up to a statement whose period includes or ends on the period end; otherwise preparation is refused naming the missing dates. **[S; AT-22]**
37. Book balance = net `txn_amount` (and functional) of posted/reversed lines on `gl_account` dated ≤ period end (`trial_balance(..., by_currency=True)` figures). Outstanding items: book items dated ≤ period end not matched (confirmed) to statement lines dated ≤ period end; statement lines dated ≤ period end not matched to book items dated ≤ period end. **[S; AT-22]**
38. `difference = book_balance − (bank_balance + outstanding_book_debits − outstanding_book_credits − unmatched_bank_credits + unmatched_bank_debits)` must be exactly 0 to submit or approve. Every unmatched statement line on the statement needs a `ReconciliationNote` (default: allowed outstanding with a note, OQ-10). No draft or submitted `BankTransaction` linked to a line of the period may remain (post or discard it first). **[S; AT-22, AT-23]**
39. Reconciliations go period by period: period N needs period N−1 `approved` (except the account's first period). Prepared and submitted by `banking.prepare_reconciliation`; approved by a different user with `banking.approve_reconciliation` (A), not the preparer or submitter. At approval the service recomputes the figures; if they differ from `snapshot_sha256` (a posting, match or import happened since), approval is refused ("re-prepare"). **[S + access R14; AT-22]**
40. Approval locks: every `confirmed` match whose parts are all dated ≤ period end gets `reconciliation` set and can no longer be undone; the snapshot is frozen; later imports for those dates are refused (R14). A line posted later on the GL account and dated in the reconciled period does not change the approved reconciliation; it appears in the next one flagged `posted_after_reconciliation`. Reopening: OQ-11 (default: only the latest approved reconciliation of the account, by a `managing_partner` or an approver who did not prepare it, with reason; status `superseded`, its locks released; logged). **[S + M; AT-22]**
41. Reconciliation posts nothing. Default: `close_period` does not require approved bank reconciliations (OQ-12); it is refused while a `submitted` `BankTransaction` dated in the period exists (registered `pending_documents`). **[L; AT-26]**

**FX and currency**
42. Statement amounts, match parts and reconciliation figures are in the bank account's currency; reconciliations also show functional figures. Amount decimals follow the currency (e.g. KWD 3, JPY 0); float input refused (ledger-fx R8). Revaluation of foreign bank accounts is ledger-fx `revalue_period` (1100 sub-accounts are `is_monetary`); banking does not revalue. **[S + L; AT-18]**

**Agents, system users, audit, language**
43. Agent users (gateway only) may hold `banking.view` (masked), `banking.suggest_match` (creates `proposed` matches only) and `banking.create_bank_transaction` (drafts, `source="agent:<name>"`). They cannot import, confirm/reject/undo matches, submit, approve, reverse, prepare or approve reconciliations, change bank accounts or settings, or unmask IBANs (access R12). Every agent action writes a `BankingEvent`. **[S + access catalogue; AT-25]**
44. No system user is created by this spec; a future bank-feed import user is an open question (OQ-3). **[catalogue]**
45. Every create, change, import (accepted, refused, duplicate), match step, document step and reconciliation step writes one `BankingEvent` in the same transaction; refusals of `require` are logged by `core.access`. `BankingEvent` is append-only (model + PostgreSQL trigger). **[S + M + DB; AT-31]**
46. Every message, choice label, bank name, report heading and reconciliation statement exists in English and Arabic; the reconciliation statement prints both. **[T: label scan; AT-28]**
47. A new entity uses banking with data only: `Bank` rows if needed, `BankAccount` rows, GL sub-accounts, `BankingSettings` defaults, memberships. No entity code appears in `banking` code. **[T: scan for entity codes; AT-32]**

## 5. Accounting

Codes from `ledger/coa_template.py`. "Bank GL" = the bank account's `gl_account` (1100 or a 1100 child, OQ-5). ALM functional SAR. Rates illustrative. Matching, imports and reconciliations produce **no entries**.

**B1. Bank charge** (`bank_charge`, SAR 25.00)
| Account | Dr | Cr |
|---|---|---|
| 5700 Bank charges | 25.00 | |
| Bank GL (SAR) | | 25.00 |

**B2. Bank charge on a USD account** (USD 15.00, table USD→SAR 3.7500)
| Account | Currency | Txn | Rate | Dr SAR | Cr SAR |
|---|---|---|---|---|---|
| 5700 Bank charges | USD | 15.00 | 3.7500 | 56.25 | |
| Bank GL (USD) | USD | 15.00 | 3.7500 | | 56.25 |

**B3. Islamic deposit profit** (`islamic_profit`, islamic account, SAR 1,200.00)
| Account | Dr | Cr |
|---|---|---|
| Bank GL | 1,200.00 | |
| 4600 Islamic deposit profit | | 1,200.00 |

**B4. Conventional interest credited** (`conventional_interest_credit`, SAR 300.00; default OQ-6) — never income
| Account | Dr | Cr |
|---|---|---|
| Bank GL | 300.00 | |
| 2600 Charity payable (purification) | | 300.00 |
Payout: invoicing E15 (Dr 2600 / Cr bank GL), approved, human only.

**B5. Own transfer, same currency** (SAR 50,000.00 from Riyad to Al Rajhi)
| Account | Dr | Cr |
|---|---|---|
| Bank GL Al Rajhi (SAR) | 50,000.00 | |
| Bank GL Riyad (SAR) | | 50,000.00 |

**B6. Own transfer, SAR → USD** (bank debits SAR 37,510.00, credits USD 10,000.00; ledger-fx F8)
| Account | Currency | Txn | Rate | Dr SAR | Cr SAR |
|---|---|---|---|---|---|
| Bank GL (USD) | USD | 10,000.00 | 3.7510 (manual, actual) | 37,510.00 | |
| Bank GL (SAR) | SAR | 37,510.00 | 1 | | 37,510.00 |
USD → SAR is the mirror (USD leg at the implied actual rate). No FX line (ledger-fx R20).

**B7. Intercompany transfer** (ALM sends SAR 100,000.00 to SUB as a group loan)
In ALM (ALM's document):
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1220 Loans to group entities | 100,000.00 | | SUB |
| Bank GL (ALM) | | 100,000.00 | |
In SUB (SUB's own document from SUB's statement): Dr Bank GL (SUB) 100,000.00 / Cr 2110 Due to group entities 100,000.00, counterparty ALM. Loans carry no interest.

**B8. Intercompany settlement in USD with realised FX** (ALM owes SUB USD 10,000.00 on 2110, carried at 3.7500 = 37,500.00; paid from the USD account at 3.7600)
| Account | Currency | Txn | Rate | Kind | Dr SAR | Cr SAR | Counterparty |
|---|---|---|---|---|---|---|---|
| 2110 Due to group entities | USD | 10,000.00 | 3.7500 (carrying) | settlement | 37,500.00 | | SUB |
| 5750 FX loss – realised (proposed) | SAR | 100.00 | 1 | realised_fx | 100.00 | | |
| Bank GL (USD) | USD | 10,000.00 | 3.7600 (table) | normal | | 37,600.00 | |
A gain goes to 4700 (proposed). Rounding differences, if any, to 5770 (ledger-fx R17).

**B9. Reversal** of any of the above: `reverse_entry` mirrors every line (ledger F11); the document becomes `reversed`.

**Not booked by banking:** customer receipts (invoicing E5/E6), vendor payments (E9 ff.), payroll payments (payroll E3) and purification transfers (E14/E15) are booked by their own modules and only matched here. Conventional interest/penalty debits in a Sharia-enforcing entity: no entry (R29, OQ-7).

## 6. Integrations

None automated in this spec. All external links, when added, go through the integration layer; tests use anonymised fixture files only.

| System | Direction | What flows | Status |
|---|---|---|---|
| Banks (Riyad, SNB, Al Rajhi, AlJazira, Alinma, others as data) | In | Statement files uploaded by a user: MT940, camt.053, bank CSV | Manual upload. Per-bank layouts **OQ-1 (blocking per bank)** |
| Bank APIs, SWIFT, host-to-host | In / Out | Automatic statements; payment initiation | Not built (OQ-2, OQ-3) |
| SAMA open banking framework **[CC-verify]** | In | Account information | Not built (OQ-3) |
| `invoicing` | Read | Posted `CustomerReceipt` / `VendorPayment` numbers, `bank_reference`, allocated invoice numbers (M1). "Create receipt from line" calls `invoicing.services` with the caller's invoicing rights (B7 applies) | Built when banking is built (OQ-22) |
| `payroll` | Read | `PayrollPayment` id/number, amount, `bank_reference` (M1); WPS bank-processed files are not used for matching (OQ-26) | When payroll is built |
| `ledger` | Out (posting) / Read | `BankTransaction` postings via the registry; GL lines on bank GL accounts; rates | Built |
| ZATCA, GOSI, Mudad, SAP B1 | — | None | n/a |
| Daftra / SAP B1 migration | In (one-off) | Opening bank balances and outstanding items at cutover | migration-agent (OQ-17) |

## 7. Permissions

Codenames are added to `core/access/catalogue.py` (flags as access 3.6). Rights are per entity; `group_treasury` (group role) covers `full` descendants only (access R6, OQ-3).

| Codename | Flags | accountant | group_treasury | approver | viewer | agent | managing_partner |
|---|---|---|---|---|---|---|---|
| `banking.view` | Ag (scoped, masked) | Y | Y | Y | Y | Y if granted | Y |
| `banking.view_iban` | | Y | Y | Y | | No | Y |
| `banking.manage_bank_account` (draft/edit) | | Y | Y | | | No | |
| `banking.approve_bank_account` (R7) | A, S! | | | Y (not own) | | No | Y (fallback) |
| `banking.import_statement` | | Y | Y | | | No | |
| `banking.suggest_match` | Ag | Y | Y | | | Y if granted | |
| `banking.match` (confirm, reject, manual, undo) | | Y | Y | | | No | |
| `banking.create_bank_transaction` (draft) | Ag | Y | Y | | | Y if granted | |
| `banking.submit_bank_transaction` | | Y | Y | | | No | |
| `banking.approve_bank_transaction` (R25) | A, P | | | Y (not own) | | No | Y (fallback) |
| `banking.reverse_bank_transaction` (R35) | P, reversal | | | Y (OQ-19) | | No | |
| `banking.prepare_reconciliation` | | Y | Y | | | No | |
| `banking.approve_reconciliation` (R39) | A | | | Y (not preparer/submitter) | | No | Y (fallback) |
| `banking.reopen_reconciliation` (R40) | S! | | | Y (not preparer, OQ-11) | | No | Y |
| `banking.manage_settings` | | | | Y | | No | |

`banking.approve_bank_transaction` is added to the ledger's `DOCUMENT_POST_PERMISSIONS` (posting flag) and `banking.reverse_bank_transaction` to `REVERSE_PERMISSIONS`. Segregation applies to the person whatever roles they hold (access R14, R35). A "treasury" entity role does not exist; default: `accountant` and `group_treasury` prepare (OQ-14).

## 8. Acceptance tests

Fixtures (anonymised): ALM (group parent, SAR, Sharia enforced), SUB (`full`, SAR, Sharia enforced), NSH (`full`, SAR, Sharia **not** enforced), MIN (`equity`), OTHER (top-level entity of a second group). Banks: the five B1 banks (test codes). ALM bank accounts: `RIYAD-SAR` (GL 1101, conventional), `RAJHI-SAR` (GL 1103, islamic), `SNB-USD` (GL 1102, USD, conventional), `ALINMA-KWD` (GL 1104, KWD, islamic). SUB: `ALINMA-SAR` (SUB GL 1101). Users: accountants A, A2 (ALM); approver P (ALM); accountant S, approver PS (SUB); group_treasury T (ALM); viewer V (ALM); agent G (ALM, `banking.view`, `suggest_match`, `create_bank_transaction`). Fixture statements for each format with synthetic IBANs. Periods 2027-01 to 2027-03. Rates USD→SAR 3.7500 (01-15), 3.7600 (01-20); KWD→SAR 12.2000.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | A in ALM | Create `RIYAD-SAR` with a valid SA IBAN; with a wrong check digit; with 23 characters; with a bank code of another bank; with GL 1101 of SUB; with GL 2410; with GL 1200; with GL 1102 (USD) for a SAR account; with GL 1101 already used by an active account; A approves own draft; P approves it; a statement import before approval | Valid draft created; each invalid case refused with en/ar message (R3, R5, R6); A's approval refused (`self_approval`); P activates it (`bank_account_activated`); import before activation refused (R8) |
| AT-2 | Active account with IBAN ending 1234 | V lists accounts; A2 (no `view_iban` in fixture variant) opens it; A opens it; check admin label, API, `BankingEvent.details`, error text of a refused import | V and A2 see `SA•• … 1234`; A sees full IBAN, `iban_viewed` + `AccessLog` rows written; no full IBAN anywhere in logs or messages (R4) |
| AT-3 | `RIYAD-SAR` opening statement balance 100,000.00 | A imports MT940 fixture (opening 100,000.00, 6 lines, closing 112,450.00) | One `StatementImport` with hash, statement id, balances; 6 immutable lines; `import_accepted` logged; matching run (R18) |
| AT-4 | AT-3 done | A imports the same file again; T imports it again | No new import, line, match or event other than `import_duplicate_file`; existing import returned (R9) |
| AT-5 | AT-3 done | Import a file with the same statement id and sequence but one changed line | Refused, nothing stored, `import_refused` with reason (R12) |
| AT-6 | AT-3 done (closing 112,450.00) | Import next statement opening 112,400.00; import one whose lines do not sum to its closing; import one in USD for the SAR account | All refused with en/ar reason naming expected vs found; nothing stored (R11, R12) |
| AT-7 | Statements for 01-01..01-15 and 01-16..01-31 imported | Import a statement for 01-10..01-20; one for 01-01..01-05 | Both refused ("overlapping" / "import in order") (R12) |
| AT-8 | AT-3 done; a CSV export for the next days that repeats one line of AT-3 (same bank reference, amount, direction) | Import; re-import with `accept_duplicates=[3]` and reason; a file with two identical 25.00 charges on one day | First refused listing line 3; second accepted, line 3 has `duplicate_of`, `duplicates_accepted` logged; identical lines inside one file accepted (R13) |
| AT-9 | Same movements as camt.053 and as CSV fixture | Import camt.053 on a second test account; CSV without balances, then with entered opening/closing; a PDF file | Parsed lines equal the MT940 ones; CSV without balances refused, with balances accepted; PDF refused "unsupported format" (R10; OQ-1, OQ-24) |
| AT-10 | Imported line L | Change L's amount via `save`, `QuerySet.update`, admin; delete L; on PostgreSQL raw UPDATE/DELETE | All refused; L unchanged (R15) |
| AT-11 | Receipt R (ALM-RCT-2027-000012, `bank_reference` "TRX88", posted 01-14, bank GL debit 11,475.00); statement credit 11,475.00 on 01-15, reference "TRX88" | Import | One M1 match, confidence 100, `confirmed`, `auto=true`; line `matched`; journal entry count unchanged; no document created (R16, R20, R24) |
| AT-12 | Two posted receipts of 5,000.00 both referenced "INV-ALM-2027-000040" in narrative; one statement credit 5,000.00 with that reference | Import | No auto-confirm; two M1 proposals; A confirms one; the other proposal is rejected automatically (amount consumed) (R17, R20) |
| AT-13 | Vendor payment 8,000.00 posted 01-10 with no reference in the bank line; statement debit 8,000.00 on 01-12; another on 01-20 | Import | 01-12 line: M2 proposal confidence 70, not confirmed until A confirms; 01-20 line: no proposal (outside 3 days) (R21) |
| AT-14 | Receipts 10,000.00 ×3 posted; one statement credit 30,000.00. Vendor payment 50,000.00; statement debits 30,000.00 and 20,000.00. Receipt 10,000.00 recorded gross; statement credit 9,975.00. Statement credit 15,000.00; receipts 10,000.00 and 5,000.00 posted on different days | M3 runs; A confirms; A builds manual groups: (a) 9,975.00 line + 10,000.00 receipt + a posted 25.00 bank-charge document; (b) 10,000.00 of the 15,000.00 line with the 10,000.00 receipt, later the remaining 5,000.00 with the 5,000.00 receipt; (c) a group that does not balance; (d) a part larger than the open amount | One-to-many and many-to-one proposed (M3) and confirmed; (a) confirmed (mixed directions balance); (b) line `partly_matched` then `matched`; (c) and (d) refused (R17, R22, R23) |
| AT-15 | Unmatched statement debit 25.00 "CHARGES" on `RIYAD-SAR` 01-20 | A creates a `bank_charge` draft from the line, submits; A approves; P approves; P approves again with the same grant; agent G approves | Draft (`source=human`); A refused (`self_approval`); P's approval posts B1 through `create_entry(post=True)` with `build_lines` (document link, `ALM-BTX-2027-000001`, `posted_by` = P); line auto-matched M1 to the new bank GL credit; second use refused (grant used / `document_booked`); G refused (R25, R26) |
| AT-16 | Credit 1,200.00 "PROFIT" on `RAJHI-SAR`; same on `RIYAD-SAR` | A drafts `islamic_profit` on each; P approves | RAJHI: B3 posts; RIYAD: draft refused (`conventional` account, use `conventional_interest_credit`) (R27, R28) |
| AT-17 | Credit 300.00 "INTEREST" on `RIYAD-SAR` in ALM; same in NSH | A drafts `islamic_profit` (refused); drafts `conventional_interest_credit`; P approves; read `purification_amount`/purification report; a `validate_lines` test passing a 4xxx income line for this document | B4 posts Cr 2600; document `requires_purification=True`, listed on purification report; no income account in the entry; income-line variant refused (`document_lines_rejected`); NSH gets the same B4 (R28, OQ-6) |
| AT-18 | `SNB-USD` (GL 1102, USD); USD statement | Import USD statement (amounts 2 dp); USD 15.00 charge approved on 01-15; own transfer SAR 37,510.00 → USD 10,000.00 on 01-20; a transfer with legs on 01-20 and 01-21; approval of a USD charge dated 03-10 with no USD rate within 5 days; `ALINMA-KWD` charge KWD 1.234; a KWD line of 1.2345; reconciliation of `SNB-USD` | B2 posts at 3.7500; B6 posts with manual actual rate, no FX line; two-date transfer refused (spot only); missing rate: approval refused, nothing posted, grant unused, document `submitted`; KWD 1.234 accepted, 1.2345 refused; reconciliation shows USD figures and SAR book balance (R31, R34, R42) |
| AT-19 | ALM owes SUB USD 10,000.00 on 2110 at 3.7500 (posted) | A drafts `intercompany_transfer` from `SNB-USD`, counterparty SUB, account 2110, `settles_line` = that item, on 01-20 (3.7600); P approves | B8 posts: settlement at carrying rate, 5750 debit 100.00 `realised_fx`; open USD and SAR balances of the item 0 (R33) |
| AT-20 | ALM `RIYAD-SAR` debit 100,000.00 to SUB | A drafts `intercompany_transfer` with no counterparty; with counterparty ALM; with OTHER (an entity of another group); with SUB on 1220; P approves; then S drafts SUB's side from SUB's statement and PS approves | First three refused (R32, rule 6); ALM B7 posts in ALM only; nothing created in SUB by ALM; SUB's own document posts Dr 1101 / Cr 2110 cp ALM; `intercompany_balances(by_currency=True)` shows ALM/SUB mirror 100,000.00 (R32) |
| AT-21 | `RIYAD-SAR` debit 50,000.00 and `RAJHI-SAR` credit 50,000.00 two days later | A drafts `own_transfer` from the RIYAD line to RAJHI; P approves; RAJHI statement imported; A drafts an `own_transfer` to SUB's `ALINMA-SAR` | B5 posts once; RAJHI credit matched to the same entry's RAJHI line (M1 by document number or M2); transfer to SUB's account refused ("use intercompany transfer") (R30) |
| AT-22 | ALM `RIYAD-SAR` Jan: statements cover 01-01..01-31; one unpresented vendor payment 4,000.00 dated 01-30; one unmatched bank credit noted | A prepares Jan; A approves; A2 submits, P approves; A undoes a locked match; A imports a statement dated 01-25..01-31; A prepares Feb before Jan approval (fixture variant); after approval a payment dated 01-29 is posted, then Feb is prepared; T reopens | Jan difference 0, outstanding list as given; A's approval refused (preparer); P approves, matches get `reconciliation`, snapshot frozen; undo refused; import refused (R14); Feb before Jan refused; Feb lists the 01-29 payment flagged `posted_after_reconciliation`, Jan unchanged; T reopen refused (no right); P (not preparer) reopens with reason → Jan `superseded` (R36 to R40, OQ-11) |
| AT-23 | Jan prepared with a book item missing from the bank (difference 25.00); variant with an unmatched line without note; variant with a submitted `BankTransaction` linked to a Jan line; variant: a posting on the GL after preparation | Submit / approve | Refused: difference ≠ 0; note missing; pending document; snapshot changed ("re-prepare") (R38, R39) |
| AT-24 | A is accountant in ALM only; T is group_treasury on ALM | A lists bank accounts, statements, reconciliations; opens a SUB line by id; imports a file to SUB's account; matches a SUB line with an ALM receipt; drafts an ALM `BankTransaction` with `to_bank_account` = SUB account; T lists SUB and MIN accounts | A sees ALM only, SUB detail 404, each SUB action refused with `permission_denied` row; cross-entity match and transfer refused (R1, R2); T sees SUB, not MIN (access R6) |
| AT-25 | Agent G | G lists accounts; views full IBAN; imports a statement; proposes a match; confirms it; drafts a `bank_charge`; submits it; approves it; prepares a reconciliation | List masked; IBAN, import, confirm, submit, approve, prepare refused; proposal created `proposed`; draft created with `source="agent:G"`; every call logged in `BankingEvent` / `AccessLog` (R43) |
| AT-26 | 2027-01 closed in ALM; a submitted `BankTransaction` dated 2027-02-10 | P approves a draft dated 01-25; A2 closes 2027-02 | Approval refused ("period closed"), nothing posted, grant unused; close refused (`documents_pending`) (R34, R41) |
| AT-27 | Posted charge document D, matched (confirmed, not locked); a posted receipt later reversed in invoicing | Reverse D; undo D's match, reverse D; M0 run | First refused (matched); after undo, reversal posts, D `reversed`; M0 pairs D's line with its reversal and the receipt with its reversal, both auto-confirmed (R19, R35) |
| AT-28 | User with `preferred_language="ar"` | Trigger each refusal in AT-1, AT-6, AT-15, AT-22; print the Jan reconciliation statement | Arabic messages; statement has Arabic and English headings and bank names (R46) |
| AT-29 | Payroll payment (net salaries 19,781.25, `bank_reference` "WPS-0127") posted on `RIYAD-SAR` | Import statement debit 19,781.25 reference "WPS-0127" | M1 auto-confirmed (R20) |
| AT-30 | ALM (Sharia enforced): statement debit 80.00 "INTEREST CHARGED" | A looks for a `BankTransaction` kind; A drafts a manual journal Dr 5610 / Cr 1101; A prepares Jan with a note on the line | No kind offered; journal posting refused (Sharia-prohibited); reconciliation lists the line as outstanding flagged `sharia_exception`, difference 0 with it outstanding (R29, OQ-7) |
| AT-31 | Steps of AT-3, AT-11, AT-15, AT-22 | Read `BankingEvent`; try UPDATE/DELETE on it (model, queryset; raw SQL on PostgreSQL) | One event per step with actor and kind; no full IBAN; writes refused (R45) |
| AT-32 | New entity NEW added under ALM as data (chart, periods, bank account at a new bank row, memberships) | Run AT-3, AT-11, AT-15 for NEW | All pass with no code change; numbers `NEW-BTX-2027-000001` (R47) |

## 9. Open questions

**B** = blocks building that part; **GL** = blocks go-live; **D** = default used above, building can go ahead.

| # | Question | Who | Blocks? | Default |
|---|---|---|---|---|
| OQ-1 | Statement formats per bank (Riyad, SNB, Al Rajhi, AlJazira, Alinma): which of MT940 / camt.053 / CSV each provides to corporate clients, field mapping (references, transaction codes, Arabic narratives), encoding, balances in CSV, statement ids. Sample anonymised files needed. | Project owner + each bank | **B** for each bank-specific parser; generic MT940 and camt.053 parsers can be built on standard fixtures | No bank parser marked supported until its sample passes |
| OQ-2 | Are payments ever initiated from the ERP (payment files, bulk transfers, WPS upload to bank)? | Managing Partner | **B** for any payment file or initiation | No: the ERP records payments made in the bank portal |
| OQ-3 | Automatic connections: bank APIs, SWIFT (MT940 via SWIFT service bureau), host-to-host SFTP, SAMA open banking (account information) **[CC-verify: framework scope and availability]**; a `system_bank_import` user if built. | Managing Partner + Dev | D | Manual upload only; any link via the integration layer |
| OQ-4 | IBAN rules: confirm SA IBAN structure (24 chars, bank code in positions 5–6) and the five banks' codes and BICs **[CC-verify]**; foreign accounts in non-IBAN countries (China, USA, Egypt status **[CC-verify]**): which identifiers to store. | Accountant + compliance-checker | **GL** for bank-code check | ISO 13616 mod-97 always; SA bank-code check uses seeded `unverified` codes and warns until verified |
| OQ-5 | GL layout: one 1100 child per bank account (codes e.g. 1101…, `currency` set), confirmed by the accountant in the Word review (ledger-fx OQ-11, invoicing OQ-20). | Accountant | D | One sub-account per bank account under 1100, required by R5 |
| OQ-6 | Conventional interest credited: book directly to 2600 (B4) or credit 4900 flagged and transfer by E14? Same treatment in entities not enforcing Sharia? **Needs Sharia board sign-off.** | Managing Partner + Sharia board | D | Direct to 2600, never income, every entity |
| OQ-7 | Conventional interest or penalty **debited** by a bank in a Sharia-enforcing entity (5610 blocked): how is the cash outflow booked (e.g. a receivable from the bank, a non-prohibited expense with a flag)? **Needs Sharia board sign-off.** | Managing Partner + accountant + Sharia board | **B** for booking that case | Not bookable; outstanding with note, flagged `sharia_exception` (R29) |
| OQ-8 | Matching parameters: M1 window 7 days, M2 window 3 days, M3 max 5 items, confidence values, auto-confirm of M1 on. | Accountant | D | As 3.8 |
| OQ-9 | Bank account master: who creates and who approves activation and IBAN/GL changes; is dual control needed for IBAN changes? | Managing Partner | D | Accountant/treasury drafts, approver (not own) activates, R7 |
| OQ-10 | May a reconciliation be approved with unrecorded bank items outstanding (with notes)? Any age or amount limit? | Accountant / CFO | D | Allowed with a note per item, no limit |
| OQ-11 | Reopening an approved reconciliation: who, which ones, effect on later periods. | Managing Partner / CFO | D | Latest only; MP or approver not the preparer; reason; `superseded` |
| OQ-12 | Should `close_period` require approved bank reconciliations of all active accounts? | Accountant / CFO | D | No; only submitted bank documents block (R41) |
| OQ-13 | VAT on bank charges where the bank issues a tax invoice: recover input VAT? **[CC-verify]** | Accountant / tax adviser | D | Gross to 5700, no VAT split (B8) |
| OQ-14 | Is a per-entity treasury role needed, or do accountants and group treasury prepare? | Managing Partner | D | `accountant` + `group_treasury` |
| OQ-15 | Retention of statement files and line data containing personal data (PDPL); retention period **[CC-verify: 10-year accounting records]**. | Managing Partner + legal | **GL** | Keep encrypted in KSA for 10 years; masked in lists |
| OQ-16 | Islamic deposits (murabaha, wakala) at banks: may a `BankAccount` point to 1300, and how is deposit profit accrued vs received? | Accountant | D | No; 1100 children only; profit when credited (B3) |
| OQ-17 | Cutover: opening bank balances per account and outstanding items at go-live from Daftra; must tie to the opening TB. | Accountant + migration-agent | **GL** | `opening_statement_balance` per account; outstanding items loaded as migration book items |
| OQ-18 | Should a system user draft `BankTransaction`s automatically from bank transaction codes (e.g. every "CHG" line)? | Accountant | D | No; user or agent drafts only (R24) |
| OQ-19 | Who may reverse a posted `BankTransaction`? | CFO | D | Approver |
| OQ-20 | Cross-currency own transfers: keep the ledger-fx R20 actual-rate method (no realised FX; difference via revaluation), or realise FX against the foreign account's carrying rate? | Accountant | D | R20 method (R31) |
| OQ-21 | May manual journals post to bank GL accounts at all (they become book items to match)? | Accountant / CFO | D | Allowed; matched like any book item |
| OQ-22 | "Create receipt from line": may banking start a `CustomerReceipt` draft prefilled from a statement credit (customer guessed from counterparty name/IBAN)? | Accountant | D | Yes, a draft through `invoicing.services` with the user's invoicing rights; customer chosen by the user |
| OQ-23 | Counterparty names and IBANs in statement lines: are they personal data to mask (individual customers, employees in salary lines)? | Managing Partner + legal | D | Masked without `banking.view_iban` (R4) |
| OQ-24 | CSV exports without opening/closing balances: may the importer enter them, and who checks them? | Accountant | D | Importer enters both; R11/R12 apply |
| OQ-25 | Intercompany transfers: should the receiving entity get a draft mirror document automatically? | Managing Partner | D | No (as invoicing D23); each entity drafts its own side |
| OQ-26 | Should payroll's bank-processed WPS file (per-employee results) be used to match or flag failed salary transfers? | Payroll + accountant | D | No; match on the payment amount and reference only |

**Blocking:** OQ-1 (each bank-specific parser), OQ-2 (any payment initiation or payment file), OQ-7 (booking conventional interest debits in Sharia-enforcing entities). **Go-live:** OQ-4 (verified SA bank codes), OQ-15 (retention/PDPL), OQ-17 (cutover balances). **Sharia board sign-off:** OQ-6, OQ-7.

## Appendix: existing code that must change (for the module-builder)

| File | Change |
|---|---|
| `core/access/catalogue.py` + seed migration | `banking.*` codenames of §7 with flags; `banking.approve_bank_transaction` posts; `banking.reverse_bank_transaction` in `REVERSE_PERMISSIONS` |
| `ledger/services.py` | Factor the R20 leg pricing of `record_currency_exchange` into a pure helper usable by `build_lines` (no behaviour change) |
| `ledger/services_setup.py` / setup data | Creating 1100 child accounts per bank account (with `currency`, `is_monetary`) as data |
| New app `banking` | Models §3, services, parsers, registry registration in `banking/apps.py`, PostgreSQL triggers for `StatementLine`, `StatementImport`, `MatchPart`, `BankingEvent` |
