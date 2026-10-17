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

## Run it
```bash
pip install -r requirements.txt
export DJANGO_DEBUG=1                 # local only; otherwise DJANGO_SECRET_KEY is required
python manage.py migrate
python manage.py setup_almiras        # Almiras entity, 55 accounts, 2027 periods
python manage.py createsuperuser      # setup only: run admin with ERP_ADMIN_SETUP_MODE=1
python manage.py grant_access <user> ALM managing_partner
python manage.py setup_totp <user>    # authenticator app; admin and API need password + code
python manage.py runserver            # admin at /admin, API at /api/v1/
python -m pytest -q
```
Set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST` to use PostgreSQL.

## Approvals and access (wave 1, step 2)
- `access`: custom user, roles and permissions, per-entity access, append-only audit log, TOTP two-factor.
- `approvals`: approval matrix as data, request lifecycle, delegation, escalation, in-app notifications, REST API.
- Manual journal entries are posted by approval only. Cron: `run_escalations` every 15 min, `send_notifications`.
- Details and build choices: `docs/specs/approvals-and-web-ui.md` (Build notes).

## Build agents
Six Claude Code agents in `.claude/agents/` (spec-writer, module-builder, test-engineer, code-reviewer, compliance-checker, migration-agent). Shared rules in `CLAUDE.md`. Future in-ERP AI agents: `docs/ai-agent-gateway.md`.

## Next steps
1. Load the Daftra chart and 31 Dec 2026 trial balance as opening balances.
2. PostgreSQL triggers so bulk updates can't bypass the posting lock.
3. AP/AR and invoices; ZATCA Phase 2 e-invoicing.
4. Bank statement import and reconciliation.
5. Intercompany billing (rentals, IP, secondments, consulting).
6. Approvals and an Arabic/English web UI.

## Running the tests on PostgreSQL

`python -m pytest -q` uses SQLite by default. Production runs on PostgreSQL, so run the suite there too
before merging changes that touch locking (`select_for_update`) or queries:

```bash
# once, as the postgres superuser: a throwaway role that may create the test database
sudo -u postgres psql -c "CREATE ROLE erp_scratch LOGIN PASSWORD 'scratch-only' CREATEDB;" \
                      -c "CREATE DATABASE erp_scratch OWNER erp_scratch;"

POSTGRES_DB=erp_scratch POSTGRES_USER=erp_scratch POSTGRES_PASSWORD=scratch-only \
POSTGRES_HOST=localhost python -m pytest -q

# afterwards
sudo -u postgres psql -c "DROP DATABASE erp_scratch;" -c "DROP ROLE erp_scratch;"
```

`config.settings_test` uses the same `POSTGRES_*` variables as `config.settings`; pytest creates and drops
`test_erp_scratch` itself. Use local scratch credentials only, never real ones. PostgreSQL refuses
`FOR UPDATE` on the nullable side of an outer join, so lock with `select_for_update(of=("self",))` when a
locked query uses `select_related` on a nullable foreign key.
