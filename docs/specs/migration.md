# Migration: Falcon (or other legacy system) exports into opening balances and replayed journals

Django app: `migration`. Status: **draft spec**. Written 2026-10-17 by the migration-agent. Amended 2026-10-18 by the spec-writer agent (M7, project-owner answers `docs/approvals/owner-answers-2026-10-18.md` A4, A10, with M6: replayed journals refused on control accounts, approvals through the engine, per-entity grants, posting gate on the open-item tie-out; R8, R13, R16 notes, new R21 to R24, AT-23 to AT-26, OQ-5, OQ-11, new OQ-14; to build). Amended 2026-10-21 by the migration-agent (opening-balance dry run, `docs/migration/dry-run-2026-10-21.md`: R8, R16 and R22 as built, the open-item source of R23, W14; AT-23 and AT-24 built; R13 / R16 engine requests, R23 loader and AT-25 still to build).
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
| M6 | Migration data is filled later by the owner's team in a fillable workbook prepared by the team "to simplify the process and avoid any issues"; customer and vendor open items are loaded via the migration template and the opening entry may post to control accounts as the one exception to policy A (§11). | Project owner, `docs/approvals/owner-answers-2026-10-18.md` D, B2 | 2026-10-18 |
| M7 | (owner A10, A4) All approvable documents go through the same approval engine: account-map approval and the posting approval of imported drafts (R13, R16 notes; access A25, R61). Each company is granted separately: a user migrating several entities needs a membership in each (R24). Consequence of M6 recorded by the spec-writer: since the opening entry is the **one** exception, replayed legacy journals may not post to control accounts (R8 note; invoicing R97 amended). | Project owner | 2026-10-18 |

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
   *Amended 2026-10-18 (M6, M7; to build):* for the **opening** entry a control-account code is allowed (no warning) and its balance must tie out to the open items of the workbook (§11 W8, R22). For **replayed** journals a line on a control account is an **error** (blocks loading), en/ar: "replayed journals cannot post to control accounts; load open items through the template".
   *Amended 2026-10-21 (as built):* opening reports carry one `control_account_open_items` **info** listing the Falcon → ERP control codes (with counterparty) and naming the R22 gate; replay reports carry one `control_account` error per entry (entry number, row, Falcon code → ERP code); an entry imported unchanged earlier is skipped as before. **[tests `test_validation.py::test_at23_*`]**
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
    *Amended 2026-10-18 (M7; to build):* approval is an engine request (activity `migration.account_map`), approvers
    from the entity's matrix (access R57, R61); self-approval only by the entity's top authority (access R58).

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
    *Amended 2026-10-17 (invoicing R54 D31b code-review fixes, as built):* restricted accounts (`ledger.restricted`;
    today GL 1140, the purification account) accept lines of the **opening** entry only (`source="import"` with its
    `ImportedEntry` of kind `opening`, registered by this app); a replayed journal with a 1140 line is drafted but
    refused when posted (`restricted_account`, listed in the posting dry run). PostgreSQL: `invoicing/0023`.
    **[ledger hook + DB; tests `test_restricted_accounts.py`]**
    *Amended 2026-10-18 (M6, M7; to build):* posting an imported draft is an engine request (activity `ledger.manual_entry`, matrix approvers); the opening entry is posted only after the open-item tie-out (R22) shows zero differences; a replayed draft with a control-account line is refused (`control_account`, invoicing R97 amended).
    *Amended 2026-10-21 (as built, except the engine request):* the R22 gate and the replayed-draft refusal run in `post_imported_entries`, in the dry run too, and are listed per draft (en/ar); all or nothing as before. Posting still uses the `ledger.post_entry` grant directly; the engine request (`ledger.manual_entry`) is still to build. **[tests `test_loading.py::test_r16_*`, `test_at24_*`]**
17. Discard: import drafts may be discarded (link and draft deleted) by `migration.import`; posted entries are only
    reversed in the ledger (CLAUDE.md rule 1).
18. Post-load tie-out: the ERP trial balance at the as-of date (posted entries, optionally plus import drafts),
    by ERP account, equals the mapped legacy TB closing balances aggregated per ERP account, to the halala.

**General**
19. Every service checks the user's right in the entity (`core.access.require`); lists are scoped. Agents and system
    users cannot import, approve or post (human users only).
20. Messages are English and Arabic. Raw exports stay out of git (`.gitignore`: `exports/`, `migration/exports/`);
    commit only scripts, maps and reports without personal data.

**Open items and the opening entry (added 2026-10-18, M6, M7; to build)**
21. The fillable template is the workbook of §11 (Customers, Vendors, Open receivables, Open payables sheets); no
    separate template set exists.
22. Posting gate: the opening entry is posted (R16) only when the §11 W8 tie-out for that entity is zero for every
    control account (1200, 2100; 1210 and 2110 per counterparty entity; 2310 and 2320 if any) **against the open items
    actually loaded into invoicing** (R23); any difference refuses the posting and is listed (account, counterparty,
    amount, en/ar).
    *Amended 2026-10-21 (as built):* `_open_item_tie_out(entry)` compares the entry's net per (control code,
    counterparty) with the open items reported by the sources registered with
    `migration.services.register_open_items_source(fn)`; `fn(entity)` returns {(control code, counterparty entity
    code or ""): signed SAR, debit positive}. An open item on a control account the entry does not carry is a
    difference too. **While no source is registered (the R23 loader is not built) an opening entry with any
    control-account balance is refused** ("no open items are loaded into invoicing yet"); an entry without one posts.
    The posting result lists the per-account rows (`open_items`). **[tests `test_loading.py::test_r22_*`,
    `test_at24_*`; `test_dry_run.py`]**
23. Open items are loaded into invoicing as `source="import"` documents, status `final`, no ZATCA call, **no journal
    entry** (invoicing R115, OQ-24), by the loader of OQ-12, in the dry-run / `--apply` model; a re-run skips existing
    items (unique on entity, party, document type, legacy number). Intercompany items need the party's group entity and
    must match the counterparty on the 1210 / 2110 line.
24. Each entity is loaded with the user's own membership in that entity (no cascade, M7); workbook, checks and
    messages are the same for every entity (new entities are data).

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
| AT-23 | *Added 2026-10-18 (M6, M7, R8; built 2026-10-21).* Approved map with Falcon 1201 → 1200; TB 1200 debit 50,000.00; a replayed journal with a 1200 line | Validate opening; validate replay | Opening: no warning for 1200; replay: error (en/ar) naming the entry and the control account, load refused |
| AT-24 | *Added 2026-10-18 (R22, R23; R22 built 2026-10-21 with a test source standing in for the R23 loader).* Opening draft with 1200 50,000.00 and 1210 (SUB) 20,000.00; open items loaded 50,000.00 and SUB 19,000.00 | P posts the opening entry; load the missing 1,000.00 SUB item; P posts | First refused (1210 SUB difference 1,000.00, en/ar), nothing posted; then posted; open items have no journal entries |
| AT-25 | *Added 2026-10-18 (R13, R16, M7).* ALM matrix `migration.account_map` → `approver`; `ledger.manual_entry` → `approver` | A approves the map through the service directly; P approves through the engine; P posts the opening draft through the engine | Direct call refused (`approval_request_required`); engine approvals accepted, each logged with the rule used |
| AT-26 | *Added 2026-10-18 (R24).* Accountant A with an ALM membership only | A loads SUB's workbook rows | Refused for SUB (`permission_denied`); ALM rows unaffected |

## 9. Open questions

| # | Question | Owner | Default |
|---|---|---|---|
| OQ-1 | Falcon chart and TB export formats (columns, header names, header/leaf rows, signed or debit/credit columns). | Owner / accountant | Column map; defaults are guesses |
| OQ-2 | Cut-over date per entity (README: 31 Dec 2026). | Owner / CFO | None: required argument |
| OQ-3 | Income and expense balances at cut-over: loaded to their accounts, or netted to 3900 (then reclassified to 3300). | Accountant | Loaded to their accounts |
| OQ-4 | Replay the year's journals (opening TB at year start + journals) or load the closing TB only. | Accountant | Closing TB only |
| OQ-5 | "مستخلص عميل" entries hit receivables without customer detail; open AR/AP items are a separate import (invoicing OQ-24). | Accountant | Replayed as journals; subledger tie-out pending. *Amended 2026-10-18 (M6, M7):* replayed entries cannot touch control accounts; AR/AP detail comes from the workbook's open items (R21 to R23); how "مستخلص عميل" movements after cut-over are replayed (as invoicing documents, or not at all) stays open |
| OQ-6 | Cost centres: the ledger has no dimension yet; kept in line descriptions. | Owner | Description only |
| OQ-7 | Falcon intercompany accounts with several counterparties: one Falcon code per counterparty needed, or split in the TB. | Accountant | One counterparty per map row |
| OQ-8 | Meaning of مسلسل القيد (line number or global serial). | Accountant | Informational only |
| OQ-9 | One Falcon company per export file, and which Falcon company is which entity. | Owner | One entity per file |
| OQ-10 | Legacy balances on accounts that are Sharia-prohibited in the ERP (e.g. conventional loans in a Sharia entity). | Sharia board / CFO | Blocked; human decision |
| OQ-11 | Invoicing R97 (policy A) forbids manual journals on control accounts 1200, 2100, 1210, 2110, 2310, 2320, including opening-balance journals (invoicing AT-115). Opening balances and replayed Falcon entries on them then need invoicing import documents (open items per customer/vendor, invoicing OQ-24), or an allow-listed migration document type. | CFO + invoicing builder | **GL** when R97 is built; until then they load as manual drafts with a warning. **Resolved by M6 (2026-10-18):** the opening entry may post to control accounts (one exception); open items from the workbook; replayed journals on control accounts refused (M7, R8) |

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

## 11. Fillable migration workbook (amended 2026-10-18, decision M6)

`docs/migration/almiras-migration-workbook.xlsx` is the template the owner's team fills; `docs/migration/README.md`
is the bilingual guide. Code: `migration/workbook_spec.py` (sheets, columns, lists, examples: data),
`migration/workbook.py` (generator, reader, adapters), `migration/workbook_checks.py` (validation).

**Rules**

- W1 *Template.* Generated by `manage.py migration_template [--output f]` from `workbook_spec.py`; never edited by
  hand (a test compares the committed file with the generator). Sheets: Instructions (en + ar), Entities (cut-over
  date per entity), Falcon chart, Trial balance (closing balance at cut-over, debit or credit), Account mapping
  (Falcon code → ERP code, counterparty entity, note), Customers, Vendors, Open receivables, Open payables, Bank
  accounts, Employees, Fixed assets, Checklist (who fills, who approves, sign-off), ERP chart (group template,
  reference) and Lists (dropdown values). Data sheets: row 1 title, row 2 legend, row 3 English headers, row 4
  Arabic headers, row 5 field keys ("do not change"), data from row 6. Required columns end in `*` and are
  coloured; personal-data columns carry `[PD]`; dropdowns (types, entity codes from the Entities sheet, Yes/No,
  currencies, countries, banks, WHT categories, HR and asset choices), dates `yyyy-mm-dd`, SAR `#,##0.00`, foreign
  amounts `#,##0.000`, codes and numbers that are identifiers as text. Each data sheet holds 2–3 grey, clearly fake
  example rows.
- W2 *Data handling.* A filled workbook holds legacy and personal data (PDPL): it stays under `exports/` (ignored by
  git) or on restricted company storage, is checked and loaded by an authorised person, and is never committed or
  given to an agent (M3, R20).
- W3 *Reader* (`read_workbook`; values only, read-only). Sheets are found by their English name. In each sheet the
  header row is the field-key row, else the Arabic, else the English header row (markers `*` and `[PD]`, spacing and
  letter variants ignored, R1). A missing required sheet (Entities, Trial balance, Account mapping) or required
  column refuses the file, naming it in English and Arabic; other missing sheets are warnings. Values are converted
  as in R2; dropdown values are accepted as code, English, Arabic or the bilingual label. Blank rows are skipped;
  unreadable cells are problems on their row. A row identical to a template example is an error ("delete the
  example rows") and takes no part in the other checks. Messages and reports never repeat a personal-data value.
- W4 *Row checks.* Required values present; entity codes listed on Entities; duplicates refused (entity + code per
  sheet; IBAN; employee ID number); SAR amounts ≥ 0 with ≤ 2 decimals; transaction-currency amounts with at most the
  currency's minor units (CLAUDE.md rule 7); currencies and countries known (ISO 4217 seed, ISO 3166); VAT number
  15 digits starting and ending with 3; IBANs per banking R3 (Saudi bank code mismatch is a warning while banks are
  unverified).
- W5 *Entities.* Code 2–8 capitals or digits; cut-over date required; functional currency SAR; parent blank or
  listed.
- W6 *Chart and trial balance.* Chart code 3–5 digits, type consistent with the first digit (R7), parent listed.
  Per entity the TB debits equal the credits to the halala; a row is debit or credit, not both; TB codes are in the
  entity's Falcon chart and postable; a TB code that is the prefix of another TB code is refused (a header). An
  entity without TB rows is a warning.
- W7 *Mapping completeness.* Every TB code with a balance is mapped (error); every other postable chart code too
  (warning, needed for replayed journals). The ERP code exists in the entity's ERP chart (with `--user`: the chart
  in the ERP, right `migration.view`; else the group template), is postable, matches the Falcon class (R7); an
  intercompany ERP account needs a counterparty entity other than the entity, a non-intercompany one none; a
  Sharia-prohibited ERP account with a balance in a Sharia-enforcing entity is an error (OQ-10); a code mapped to
  3900 is a warning.
- W8 *Open items tie to control accounts.* Signed SAR sums (receivables: invoice and debit note +, credit note and
  unapplied advance −; payables: bill +, vendor credit note and unapplied advance −). Per entity, open receivables of
  customers without a group entity equal the TB on Falcon codes mapped to 1200; those of a group customer equal the
  codes mapped to 1210 with that counterparty; payables likewise against 2100 / 2110 (credit balances). Each
  difference is an error with its amount. The party is on the Customers / Vendors sheet of the entity; document date
  on or before the cut-over, due date not before the document date; for SAR items amount = SAR amount.
- W9 *Banks.* Per Falcon code, the SAR book balances of the bank accounts equal the TB balance of that code; a SAR
  account's book balance equals its SAR amount; the mapped ERP account should be 1100 or under it (warning); at most
  one purification account per entity, in SAR, mapped to 1140 (warning).
- W10 *Employees.* When any end-of-service provision is given, their sum equals the TB on codes mapped to 2500
  (credit); none given but a 2500 balance is a warning. A Saudi without GOSI cohort is a warning (payroll R55).
- W11 *Fixed assets* (fixed-assets OQ-19). Accumulated depreciation ≤ cost; per Falcon cost code Σ cost = TB
  balance; per accumulated-depreciation code Σ accumulated = − TB balance; codes mapped to 1500, 1510, 1520 or 1590
  with a balance and no asset row are differences.
- W12 *Command* `migration_workbook_check --workbook f [--user u] [--entity E …] [--report out.csv]`: read-only,
  prints the report (summary per entity, problems, tie-outs), exit status 1 when it has an error.
- W13 *Existing commands* accept `--workbook f` instead of `--tb` / `--import`: `migration_map` drafts the map from
  the Account mapping sheet; `migration_validate`, `migration_load` and `migration_tieout` use the entity's TB as
  closing balances at its cut-over date (`--cutover` defaults to the sheet's date and must equal it). Rules R12 to
  R18 apply unchanged.
- W14 *Bai' ajel* (added 2026-10-21, as built; payroll.md slice 2 item 3, invoicing R127). Codes mapped to 1240,
  1241 or 4610 load through the opening entry (the migration allowance on restricted accounts). The workbook has no
  sheet for the contracts behind them, so a 1240 or 1241 balance is a warning (`bai_ajel_contracts`, the CFO decides
  how the open contracts are set up in invoicing before payroll collects them); a 1241 (contra 1240) debit balance
  or a 1241 credit larger than 1240 is an error (`bai_ajel_contra`). **[test `test_dry_run.py`]**

**Dry run** (added 2026-10-21): `migration/fixtures/dry_run.py` is a fictional two-entity fixture set (workbook,
Falcon journal sample, TB with opening / movement / closing, map CSVs); `migration/test_dry_run.py` runs it end to end
through the commands and services; the tie-out report is `docs/migration/dry-run-2026-10-21.md`.

**Acceptance tests** (synthetic workbooks built in the test; `migration/test_workbook*.py`)

| # | Given | When | Then |
|---|---|---|---|
| AT-16 | Generator | Build | All sheets in order; bilingual headers; key row; `*` / `[PD]`; dropdowns, formats; fake examples; committed file equal |
| AT-17 | Filled workbook with Arabic text, bilingual or code dropdown values, codes typed as numbers | Read | Typed values, Arabic intact; key row deleted still reads |
| AT-18 | Missing required column; example rows left | Read / check | Refused en/ar; `example_row` errors |
| AT-19 | Coherent fake data for two entities | Check | No errors; every tie-out zero |
| AT-20 | TB off by 0.01; unmapped code; AR, AP, bank, EOSB, asset each off by 0.01; IC map without counterparty; prohibited account; bad VAT, IBAN, decimals | Check | Each reported as an error with sheet, row and amount; no personal value in messages |
| AT-21 | Workbook map and TB | `migration_map --workbook`, approve, `migration_load --workbook` dry run then apply, post, `migration_tieout --workbook` | Map drafted; dry run writes nothing; opening draft; ties out |
| AT-22 | `migration_workbook_check` | Run on a good and a bad workbook | Exit 0 / CommandError; CSV report without personal values |

**Open questions**

| # | Question | Owner | Default |
|---|---|---|---|
| OQ-12 | Loaders from the workbook into invoicing (customers, vendors, open items as `source=import` documents, invoicing OQ-24), banking (bank accounts), hr (employees) and fixed assets (opening register): built with each module's own services and rules. | Builders per module | Not built; the workbook is checked and tied out now |
| OQ-13 | Foreign-currency balances (bank accounts, open items) carry transaction-currency amounts; the opening entry is SAR only. How are their FX amounts carried for revaluation (ledger-fx)? | Accountant | SAR at cut-over; transaction amounts kept in the workbook |
| OQ-14 | *Added 2026-10-18 (M7).* With replayed journals refused on control accounts, should the year-to-date replay (OQ-4) be dropped altogether, i.e. closing TB plus open items only? | Accountant / CFO | Closing TB only (OQ-4 default) |
