# Migration: Falcon (or other legacy system) exports into opening balances and replayed journals

Django app: `migration`. Status: **draft spec**. Written 2026-10-17 by the migration-agent.
Depends on: `core` (`Entity`), `core.access` (`require`, `scope`, segregation R14), `ledger.services` (`create_entry`
as draft with `source="import"`, `post_entry` with a `ledger.post_entry` grant, `trial_balance`), the chart template
(`ledger/coa_template.py`, account 3900 *Opening balance equity (migration)*), `.claude/agents/migration-agent.md`.

## 1. Purpose

Almiras keeps its books in **Falcon** today (not Daftra). This module reads Falcon exports, maps Falcon account codes
to each entity's ERP chart, proves the data is complete and consistent, and loads it through `ledger.services` only:
one opening entry per entity at the cut-over date from a trial balance, and optionally the year-to-date Falcon journal
entries replayed as drafts. Nothing posts without a second person. The same path works for any other legacy system
whose exports can be described with a column map, and for every entity, which is data (an `Entity` row plus setup).

## 2. Decisions

| # | Decision | Who | Date |
|---|---|---|---|
| M1 | The legacy system is Falcon. Journal export structure: one xlsx sheet, Arabic headers, one row per line, columns مدين, دائن, رقم القيد (yy + 6-digit sequence), مسلسل القيد, اسم الحساب, بيان, نوع الحركه, التاريخ, مركز التكلفة, الحركة ("Journal" or "مستخلص عميل"), رقم الحركة, كود الحساب (3 to 5 digits; first digit 1 assets, 2 liabilities/equity, 4 income, 5 expense). Lines are debit-only or credit-only, ≤ 2 dp, SAR. | Project owner (via caller brief); decision pack A10 (`docs/approvals/owner-cfo-decision-pack-2026-10-17-answered.docx`, = invoicing D32(p)): Almiras does not use Daftra | 2026-10-17 |
| M2 | Chart-of-accounts and trial-balance exports come later in an unknown format: readers take a configurable column map. | Project owner (via caller brief) | 2026-10-17 |
| M3 | Agents never read real Almiras data; development and tests use synthetic fixtures only. | Project owner (via caller brief); CLAUDE.md | 2026-10-17 |
| M4 | Opening balances: one entry per entity at the cut-over date, against 3900, drafted then posted by a second person, through `ledger.services` only. Account mapping approved by a second person before use. Commands dry-run by default; `--apply` and a human user are needed to write. | Project owner (via caller brief); migration-agent rules | 2026-10-17 |
| M5 | CLAUDE.md rules apply unchanged (posted entries never edited, balanced 2+ one-sided lines, `post_entry` only, closed periods, entity separation, Sharia per entity, intercompany counterparty, Decimal money, bilingual text). | CLAUDE.md | in repo |

Everything else below is a default; open points are in §9.

## 3. Data model

Every model has `entity` (FK `core.Entity`, PROTECT) directly or through its parent.

### 3.1 `AccountMap`
`entity`, `source_system` (default `falcon`), `version` (1, 2, … per entity and system), `status` (`draft`,
`approved`, `superseded`), `note`, `created_by/at`, `modified_by/at`, `approved_by/at`. At most one draft per entity
and system. An approved map never changes; a change is a new version, which supersedes the old one when approved.

### 3.2 `AccountMapRow`
`account_map`, `legacy_code` (unique in the map), `legacy_name` (as exported, Arabic), `account` (FK
`ledger.Account`, same entity), `counterparty` (FK `core.Entity`, null; required when `account` is intercompany),
`note`. Several legacy codes may map to one ERP account; one legacy code maps to exactly one ERP account.

### 3.3 `ImportedEntry`
The link from a legacy item to the ledger draft it created: `entity`, `source_system`, `kind` (`opening`,
`journal`), `legacy_key` (Falcon entry number, or `opening`), `journal_entry` (one-to-one `ledger.JournalEntry`,
PROTECT), `account_map` (the approved version used), `file_sha256`, `lines_hash`, `created_by/at`. Unique on
(`entity`, `source_system`, `kind`, `legacy_key`): an item is never imported twice.

The validation report and profiles are not stored: they are printed and optionally written to a CSV outside git.

## 4. Rules

**Readers**
1. Falcon journal reader (openpyxl, read-only, values only). Headers are matched by normalised Arabic name: trimmed,
   inner spaces collapsed, tatweel and diacritics removed, ة→ه, ى→ي, أ/إ/آ→ا, Latin case-folded. The header row is the
   first row (of the first 20) that holds every required column. Only the first sheet is read; other sheets are
   reported. A missing required column refuses the file naming it in English and Arabic; extra columns are listed.
2. Values: amounts become `Decimal` at once (numbers, or text with thousands separators and Arabic-Indic digits);
   blank = 0; a binary-float cell within 1e-9 of a 2-dp value is read as that value (counted in the profile).
   Dates: Excel date/datetime, ISO text or dd/mm/yyyy. Account codes are 3 to 5 digits. Unreadable cells are
   problems on their row, never silently dropped.
3. Chart and trial-balance readers (CSV in UTF-8, UTF-8 with BOM or Windows-1256, or xlsx) take a **column map**
   (JSON): canonical key → header text, list of alternative headers, or 1-based column number; optional `sheet`,
   `header_row`. Trial balance keys: `code` (required), `name`, `opening_debit`/`opening_credit` or `opening`
   (signed, debit positive), `movement_debit`/`movement_credit`, `closing_debit`/`closing_credit` or `closing`
   (one closing form required). Chart keys: `code`, `name` (required), `name_en`, `type`, `parent`. The default maps
   are guesses until the exports are seen (OQ-1).

**Validation report** (`validate_journal`, `validate_trial_balance`; no writes)
4. Each line is debit-only or credit-only, not negative, not zero, ≤ 2 dp.
5. Lines are grouped by entry number: each entry balances, has 2+ lines and one date. Warnings: the yy prefix of the
   entry number differs from the date's year; gaps in the entry sequence (a possibly incomplete export); identical
   duplicate rows.
6. Every entry date lies in an **open** ERP period of the entity. With a cut-over date, replayed entries must be
   dated after it (on or before it they would double the opening balances).
7. Every account code is in the map used. Unmapped codes are listed with name, row count and totals; any unmapped
   code with an amount blocks loading. Codes whose first digit contradicts the ERP account type are refused when the
   map row is written (1 → asset, 2 → liability or equity, 4 → income, 5 → expense; other digits: warning).
8. The mapped ERP account is postable, active, not report-only (3400), not Sharia-prohibited where the entity
   enforces Sharia; an intercompany account needs the row's counterparty (another entity). A code mapped to a
   control account (1200, 2100, 1210, 2110, 2310, 2320; invoicing R97, policy A) is a warning: once R97 is built the
   ledger refuses manual-journal lines on them, so their balances must come from invoicing import documents (OQ-11).
9. Totals by Falcon account and by ERP account (debit, credit, net), entries by source (`Journal`,
   `مستخلص عميل`), date range, and entries already imported are part of the report.
10. Trial balance: duplicate codes refused; rows whose code is a prefix of another row's code are treated as
    headers, excluded, and their subtotal checked against their children (warning); opening, movement and closing
    columns each balance; per row opening + movement = closing when those columns exist.
11. Tie-out (when a TB is supplied with the journal): per Falcon code, TB opening + journal net movement = TB closing,
    to the halala; when the TB has movement columns, journal net = TB movement too. Codes on one side only are
    differences. Every difference is listed with its amount.

**Mapping and approval**
12. Maps are created (from a CSV with `legacy_code, legacy_name, erp_code, counterparty, note`, the
    `docs/migration/<entity>-account-map.csv` layout) and edited as drafts by `migration.manage_account_map`; the map's
    accounts must belong to its entity (rule 4).
13. A draft map is approved with `migration.approve_account_map` (A) by a person who is neither its creator nor its
    last modifier. Approval supersedes the previous approved version. Loading uses only the approved version;
    validation against a draft map reports "map not approved" as blocking. Matches are never guessed.

**Loading** (dry run unless applied; human users only; nothing is created when the report has an error)
14. Opening entry: one per entity and source system (a re-run with the same balances is skipped; other balances are
    refused until the draft is discarded or the entry reversed), dated the cut-over date, drafted with
    `ledger.services.create_entry(..., source="import")`, reference `FALCON-OPENING`. One line per Falcon code with a
    balance (mapped account, description "Falcon <code> <name>"); zero balances skipped. The TB must balance. Balance
    basis: `closing` (default, no replay) or `opening` (when the year's journals are replayed after it). With
    `pl_to_opening_equity`, income and expense codes are not loaded to their accounts and their net goes to 3900
    (OQ-3). Any remaining net of the lines goes to 3900; a TB that does not balance is refused, never plugged.
15. Replay: each Falcon entry becomes one draft, reference `FALCON-<entry number>`, description from its first
    line; line descriptions keep بيان, cost centre, source and source number. An entry already imported is skipped;
    the same entry number with different content is refused. All entries of one run are created in one transaction.
16. Posting: each imported draft is posted by a person other than its preparer (ledger R14) through
    `ledger.services.post_entry` with `require(approver, entity, "ledger.post_entry", document=draft)`. Before
    posting, the draft's lines are compared with the `lines_hash` recorded at import; a changed draft is refused
    (discard and re-import). Ledger rules (period, balance, Sharia, intercompany) apply as for any entry.
17. Discard: import drafts may be discarded (link and draft deleted) by `migration.import`; posted entries are only
    reversed in the ledger (CLAUDE.md rule 1).
18. Post-load tie-out: the ERP trial balance at the as-of date (posted entries, optionally plus import drafts),
    by ERP account, equals the mapped legacy TB closing balances aggregated per ERP account, to the halala.

**General**
19. Every service checks the user's right in the entity (`core.access.require`); lists are scoped. Agents and system
    users cannot import, approve or post (human users only).
20. Messages are English and Arabic. Raw exports stay out of git (`.gitignore`: `exports/`, `migration/exports/`);
    commit only scripts, maps and reports without personal data.

## 5. Accounting

Opening entry (R14), at the cut-over date, entity E:

| Line | Debit | Credit |
|---|---|---|
| Each mapped asset / expense code with a debit balance | balance | |
| Each mapped liability / equity / income code with a credit balance | | balance |
| 3900 Opening balance equity (net of the lines above: zero for a full balanced TB; the P&L net with `pl_to_opening_equity`) | as needed | as needed |

3900 must be nil after the first fiscal year (reporting OQ-18); the accountant reclassifies it. Replayed entries keep
Falcon's debit and credit per line on the mapped account.

## 6. Integrations

| System | Direction | What |
|---|---|---|
| Falcon | In (one-off, files) | General-journal xlsx (§2 M1); chart and TB exports (format pending, OQ-1) |
| Other legacy systems (e.g. SAP B1 at a subsidiary) | In (one-off, files) | Same readers with a column map |

## 7. Permissions

| Codename | Flags | accountant | approver | viewer | managing_partner |
|---|---|---|---|---|---|
| `migration.view` | | Y | Y | Y | Y |
| `migration.manage_account_map` | | Y | | | |
| `migration.approve_account_map` | A | | Y (not own) | | Y (fallback) |
| `migration.import` (validate, draft, discard) | | Y | | | |

Drafting entries also needs `ledger.create_entry`; posting needs `ledger.post_entry` (approver). Added by role seed
V9 (core migration 0018).

## 8. Acceptance tests

Fixtures: entities ALM (parent, Sharia enforced), SUB (full); accountant A and A2, approver P in ALM; accountant S in
SUB; synthetic Falcon files with fake names and amounts, built in the test.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | Falcon xlsx with Arabic headers, a title row, spacing and ة/ه variants, columns reordered | Read | All rows read; Decimal amounts; dates; codes as text |
| AT-2 | File without كود الحساب | Read | Refused naming the column in en/ar |
| AT-3 | Entry with debits 1,000.00 vs credits 900.00; a line with both sides; a line of 10.005 | Validate | Each reported as an error with entry and row |
| AT-4 | Code 1199 not in the approved map | Validate; load | Listed as unmapped with totals; load refused |
| AT-5 | A drafts the map | A approves it; P approves it | A refused (self-approval); P approves; rows then locked |
| AT-6 | Map row for code 4101 to an asset account; to SUB's account; to 2410 in ALM; to 1210 without counterparty | Write row / validate | Refused / reported |
| AT-7 | Journal replayed once | Replay again; replay a file where an imported entry changed | No duplicate; changed entry refused |
| AT-8 | TB with opening, movement and closing; journal for the year | Validate with TB | Ties out; changing one journal amount by 0.01 shows the difference on that code |
| AT-9 | TB at cut-over | A loads opening (dry run, then apply); A posts; P posts | Dry run writes nothing; draft with source import, reference FALCON-OPENING; A refused; P posts; ERP TB = legacy TB |
| AT-10 | Opening draft changed after import | P posts | Refused (lines hash) |
| AT-11 | Unbalanced TB | Load opening | Refused, nothing created |
| AT-12 | Entry dated in a closed period or on/before the cut-over | Validate | Error |
| AT-13 | S (SUB only) | Validate or load for ALM | Refused (`permission_denied`) |
| AT-14 | Agent user | Load | Refused |
| AT-15 | Commands without `--apply` | Run | Nothing written |

## 9. Open questions

| # | Question | Owner | Default |
|---|---|---|---|
| OQ-1 | Falcon chart and TB export formats (columns, header names, header/leaf rows, signed or debit/credit columns). | Owner / accountant | Column map; defaults are guesses |
| OQ-2 | Cut-over date per entity (README: 31 Dec 2026). | Owner / CFO | None: required argument |
| OQ-3 | Income and expense balances at cut-over: loaded to their accounts, or netted to 3900 (then reclassified to 3300). | Accountant | Loaded to their accounts |
| OQ-4 | Replay the year's journals (opening TB at year start + journals) or load the closing TB only. | Accountant | Closing TB only |
| OQ-5 | "مستخلص عميل" entries hit receivables without customer detail; open AR/AP items are a separate import (invoicing OQ-24). | Accountant | Replayed as journals; subledger tie-out pending |
| OQ-6 | Cost centres: the ledger has no dimension yet; kept in line descriptions. | Owner | Description only |
| OQ-7 | Falcon intercompany accounts with several counterparties: one Falcon code per counterparty needed, or split in the TB. | Accountant | One counterparty per map row |
| OQ-8 | Meaning of مسلسل القيد (line number or global serial). | Accountant | Informational only |
| OQ-9 | One Falcon company per export file, and which Falcon company is which entity. | Owner | One entity per file |
| OQ-10 | Legacy balances on accounts that are Sharia-prohibited in the ERP (e.g. conventional loans in a Sharia entity). | Sharia board / CFO | Blocked; human decision |
| OQ-11 | Invoicing R97 (policy A) forbids manual journals on control accounts 1200, 2100, 1210, 2110, 2310, 2320, including opening-balance journals (invoicing AT-115). Opening balances and replayed Falcon entries on them then need invoicing import documents (open items per customer/vendor, invoicing OQ-24), or an allow-listed migration document type. | CFO + invoicing builder | **GL** when R97 is built; until then they load as manual drafts with a warning |

## 10. Runbook (per entity; raw exports under `exports/`, which git ignores)

1. `migration_map --entity E --user <accountant> --import docs/migration/<entity>-account-map.csv --apply`
   (layout: `docs/migration/account-map-template.csv`); the approver runs `--approve <version> --apply`.
2. `migration_validate --entity E --user <accountant> --journal exports/journal.xlsx --tb exports/tb.csv
   --tb-columns docs/migration/falcon-tb-columns.example.json --cutover <date> --report exports/report.csv`
   until it has no error; warnings are reviewed and decided by a person.
3. `migration_load ... --cutover <date> --tb ... [--journal ...]` (dry run), then with `--apply` (drafts).
4. `migration_tieout --entity E --user <approver> --tb ... --as-of <date>`: the accountant signs off this report.
5. `migration_post --entity E --user <approver> --kind opening --apply`, then `--kind journal --apply`.
6. `migration_tieout ... --posted-only` again: zero differences to the halala.
