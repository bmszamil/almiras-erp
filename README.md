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
- **Multi-currency** (`docs/specs/ledger-fx.md`): ISO 4217 currencies (`core.Currency`), each entity's functional currency (`Entity.base_currency`, 2 dp, locked after the first posting), per-line transaction currency, amount (up to 3 dp per the currency's minor units) and rate; debit/credit stay functional and must balance. Group-wide exchange rates with entity overrides (manual entry and CSV import; no automatic feeds yet, OQ-2), realised FX on settlement (`fx_settlement_lines`), spot currency exchange, and period-end revaluation (`revalue_period`, posted by an approver with `post_revaluation`, reversed automatically on day 1 of the next period). FX accounts 3400, 4700, 4710, 5750, 5760, 5770 are proposed in the chart template pending accountant review (OQ-6). Translation of foreign-functional entities is not built yet (OQ-5).
- **Access** (`core/access/`, `docs/specs/access.md`): roles per entity (user × entity × role), time-bounded memberships, CFO delegation, four-eyes requests and an append-only access log. Services call `core.access.require(...)`; querysets use `core.access.scope(...)`. Posting and reversing need a grant issued for that one entry or document, verified against its access-log row and usable once: a manual journal is drafted, then posted with `require(approver, entity, "ledger.post_entry", document=draft)` by someone other than its creator, submitter or last modifier; a sub-ledger document posts with `create_entry(..., post=True, grant=..., document=doc)`, where `doc` is a stored document of a type its own app registered in `ledger/documents.py` from its `AppConfig.ready()` (none is registered until invoicing exists; the registry is frozen after start-up), with a recorded creator, posted once and not by its creator, submitter or last modifier; a type may register `build_lines` / `validate_lines` hooks that supply or check the entry's lines. Sensitive entity settings change only through `core.services.change_entity_settings` (logged). On PostgreSQL, triggers also reject status changes outside the posting services, changes to posted entries, to rates used by them and to the access log.

## Run it
```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py setup_almiras        # Almiras entity (registration + address from core/setup_data/almiras.json), 61 accounts, 2027 periods
python manage.py createsuperuser      # technical admin only: no business rights (access.md R24)
python manage.py access_bootstrap --username <user> --entity ALM --reason "Initial setup"  # first managing partner
python manage.py runserver            # admin at /admin
python -m pytest -q                   # tests (config.settings_test adds the test-only testdocs app)
```
Set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST` (and `POSTGRES_PORT`) to use PostgreSQL; the PostgreSQL-only tests (triggers, row locks) are skipped on SQLite. CI (`.github/workflows/tests.yml`) runs the migration check, `manage.py check` and the tests on PostgreSQL 16 and on SQLite.

## Build agents
Six Claude Code agents in `.claude/agents/` (spec-writer, module-builder, test-engineer, code-reviewer, compliance-checker, migration-agent). Shared rules in `CLAUDE.md`. Future in-ERP AI agents: `docs/ai-agent-gateway.md`.

## Next steps
1. Load the Daftra chart and 31 Dec 2026 trial balance as opening balances.
2. AP/AR and invoices (registering their documents in `ledger/documents.py`); ZATCA Phase 2 e-invoicing.
3. Bank statement import and reconciliation.
4. Intercompany billing (rentals, IP, secondments, consulting).
5. Approvals and an Arabic/English web UI.
