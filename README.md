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
python manage.py migrate
python manage.py setup_almiras        # Almiras entity, 55 accounts, 2027 periods
python manage.py createsuperuser
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
