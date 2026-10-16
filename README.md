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
- **Access** (`core/access/`, `docs/specs/access.md`): roles per entity (user × entity × role), time-bounded memberships, CFO delegation, four-eyes requests and an append-only access log. Services call `core.access.require(...)`; querysets use `core.access.scope(...)`. Posting and reversing need a grant for the entry's entity.

## Run it
```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py setup_almiras        # Almiras entity (registration + address from core/setup_data/almiras.json), 61 accounts, 2027 periods
python manage.py createsuperuser      # technical admin only: no business rights (access.md R24)
python manage.py access_bootstrap --username <user> --entity ALM --reason "Initial setup"  # first managing partner
python manage.py runserver            # admin at /admin
python -m pytest -q                   # 148 tests
```
Set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST` to use PostgreSQL.

## Build agents
Six Claude Code agents in `.claude/agents/` (spec-writer, module-builder, test-engineer, code-reviewer, compliance-checker, migration-agent). Shared rules in `CLAUDE.md`. Future in-ERP AI agents: `docs/ai-agent-gateway.md`.

## Next steps
1. Load the Daftra chart and 31 Dec 2026 trial balance as opening balances.
2. PostgreSQL triggers so bulk updates can't bypass the posting lock.
3. AP/AR and invoices; ZATCA Phase 2 e-invoicing.
4. Bank statement import and reconciliation.
5. Intercompany billing (rentals, IP, secondments, consulting).
6. Approvals and an Arabic/English web UI.
