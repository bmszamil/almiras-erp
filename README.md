# Almiras ERP — finance core (wave 1, step 1)

In-house ERP for the Almiras group. Python / Django / PostgreSQL, all open source.

## What's in this step
- **Entities** (`core`): Almiras as parent; subsidiaries and minority stakes as rows (ownership %, full / equity consolidation, IFRS for SMEs or full IFRS).
- **General ledger** (`ledger`): bilingual chart of accounts from a group template, monthly periods, journal entries.
- **Rules enforced**
  - Posted entries can't be edited or deleted; corrections are reversals.
  - Entries must balance and have 2+ lines; each line is debit *or* credit.
  - Closed or missing periods reject postings.
  - Sharia-prohibited accounts (conventional interest) are blocked where the entity enforces Sharia.
  - Intercompany accounts require a counterparty entity (for elimination).
  - Gap-free numbering per entity and year: `ALM-JE-2027-000001`.
- **Reports**: trial balance, intercompany positions by entity pair.
- **Invoicing, slice 1** (`invoicing`, `docs/specs/invoicing.md`): customers and vendors per entity, dated VAT rates, VATEX and note reasons, income types; tax invoices, credit and debit notes with VAT per category at document level and SAR VAT totals, gap-free numbers (`ALM-INV-2027-000001`), frozen seller and buyer snapshots; vendor bills and credit notes with input-VAT attribution and reverse charge on imported services; receipts and vendor payments with allocation and foreign-currency settlement. Staff draft and submit, the approver issues or posts (never their own document); sales documents post only when ZATCA clears or reports them, through `invoicing/zatca_port.py` (default: they wait as pending until the zatca module exists). Every document builds its own journal lines (`build_lines`). Not built yet: withholding tax, advances and deposits, billing hooks, purification transfers, PDF rendering, PDPL masking and anonymisation.
- **Multi-currency** (`docs/specs/ledger-fx.md`): ISO 4217 currencies (`core.Currency`), each entity's functional currency (`Entity.base_currency`, 2 dp, locked after the first posting), per-line transaction currency, amount (up to 3 dp per the currency's minor units) and rate; debit/credit stay functional and must balance. Group-wide exchange rates with entity overrides (manual entry and CSV import; no automatic feeds yet, OQ-2), realised FX on settlement (`fx_settlement_lines`), spot currency exchange, and period-end revaluation (`revalue_period`, posted by an approver with `post_revaluation`, reversed automatically on day 1 of the next period). FX accounts 3400, 4700, 4710, 5750, 5760, 5770 are proposed in the chart template pending accountant review (OQ-6). Translation of foreign-functional entities is not built yet (OQ-5).
- **Access** (`core/access/`, `docs/specs/access.md`): roles per entity (user × entity × role), time-bounded memberships, CFO delegation, four-eyes requests and an append-only access log. Services call `core.access.require(...)`; querysets use `core.access.scope(...)`. Posting and reversing need a grant issued for that one entry or document, verified against its access-log row and usable once: a manual journal is drafted, then posted with `require(approver, entity, "ledger.post_entry", document=draft)` by someone other than its creator, submitter or last modifier; a sub-ledger document posts with `create_entry(..., post=True, grant=..., document=doc)`, where `doc` is a stored document of a type its own app registered in `ledger/documents.py` from its `AppConfig.ready()` (the registry is frozen after start-up), with a recorded creator, posted once and not by its creator, submitter or last modifier; every type registers a `build_lines` and/or `validate_lines` hook that supplies or checks the entry's lines, and may register `pending_documents` so `close_period` refuses a period with documents still waiting to be booked. Sensitive entity settings change only through `core.services.change_entity_settings` (logged). On PostgreSQL, triggers also reject status changes outside the posting services, changes to posted entries, to rates used by them and to the access log.
- **Migration** (`migration`, `docs/specs/migration.md`): readers for the Falcon general-journal export (Arabic headers) and column-mapped chart / trial-balance exports, account maps per entity approved by a second person, a validation and tie-out report, and opening balances (one entry per entity against 3900) and replayed journals drafted through `ledger.services` and posted by a second person. Commands `migration_map`, `migration_validate`, `migration_load`, `migration_post`, `migration_discard`, `migration_tieout`; dry run unless `--apply`. Raw exports stay in `exports/` (git-ignored).

## Run it
```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py setup_almiras        # Almiras entity (registration + address from core/setup_data/almiras.json), 66 accounts, 2027 periods
python manage.py createsuperuser      # technical admin only: no business rights (access.md R24)
python manage.py access_bootstrap --username <user> --entity ALM --reason "Initial setup"  # first managing partner
python manage.py runserver            # admin at /admin
python -m pytest -q                   # tests (config.settings_test adds the test-only testdocs app)
```
Set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST` (and `POSTGRES_PORT`) to use PostgreSQL; the PostgreSQL-only tests (triggers, row locks) are skipped on SQLite. CI (`.github/workflows/tests.yml`) runs the migration check, `manage.py check` and the tests on PostgreSQL 16 and on SQLite.

## Build agents
Six Claude Code agents in `.claude/agents/` (spec-writer, module-builder, test-engineer, code-reviewer, compliance-checker, migration-agent). Shared rules in `CLAUDE.md`. Future in-ERP AI agents: `docs/ai-agent-gateway.md`.

## Next steps
1. Load the Falcon (or other legacy system) chart and 31 Dec 2026 trial balance as opening balances (`migration` app, `docs/specs/migration.md`).
2. Invoicing slice 2 (withholding tax, advances, billing hooks) and ZATCA Phase 2 e-invoicing (`zatca` behind `invoicing/zatca_port.py`).
3. Bank statement import and reconciliation.
4. Intercompany billing (rentals, IP, secondments, consulting).
5. Approvals and an Arabic/English web UI.
