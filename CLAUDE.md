# Almiras ERP — rules for every agent and developer

In-house ERP for the Almiras group. Python 3.13, Django 6.1, PostgreSQL. Open source only.
No Odoo, SAP, Microsoft Dynamics or paid SaaS as dependencies; we may *connect* to them via the integration layer only.

## Non-negotiable accounting rules
1. Posted journal entries are never edited or deleted. Corrections are reversals (`ledger.services.reverse_entry`).
2. Every entry balances and has 2+ lines; each line is debit or credit, never both.
3. Posting goes through `ledger.services.post_entry` only. Never set `status="posted"` directly.
4. Closed periods reject postings. Entities never post to another entity's accounts.
5. Sharia is enforced per entity: Sharia-prohibited accounts are blocked; non-compliant income is flagged for purification.
6. Intercompany lines always carry a counterparty entity.
7. Money is `Decimal`, never float, currency explicit. Functional-currency (SAR) amounts are stored with 2 decimals; foreign transaction-currency amounts with up to 3 decimals, checked against that currency's own decimals (project owner, 2026-10-16).

## Multi-entity rules
- Every business record has an `entity` foreign key. Queries in views and APIs are always filtered by entity and user permission.
- New subsidiaries are data (an `Entity` row plus setup), never new code paths.
- Every user-facing text has English and Arabic (`name_en` / `name_ar`, or Django translations).

## How work is done
- One module per Django app. Business logic lives in `services.py`, not in views or admin.
- Every rule gets a test. `python -m pytest -q` must pass before any merge.
- Specs live in `docs/specs/<module>.md` and are written before code.
- Builders work on a branch per module; the reviewer agent and a human approve before merge to `main`.
- Agents never connect to production data or real credentials. Use fixtures and anonymised samples.
- Commit messages: imperative, one line summary, then why.

## Agents (in `.claude/agents/`)
| Agent | Job |
| --- | --- |
| spec-writer | Turns decisions and documents into a module spec with acceptance tests |
| module-builder | Builds one module from its spec, with tests |
| test-engineer | Turns rules and specs into automated tests; finds untested paths |
| code-reviewer | Independent review: accounting, security, entity separation (read-only) |
| compliance-checker | Checks against ZATCA, VAT, Zakat, GOSI, Mudad, PDPL (read-only) |
| migration-agent | Maps Daftra (or other) exports into opening balances and proves they tie out |

Typical flow for a module: spec-writer → module-builder + test-engineer → code-reviewer + compliance-checker → human approval → merge.

## Future AI agents inside the ERP
See `docs/ai-agent-gateway.md`. In short: agents connect only through the agent gateway API, act as a named service user, create drafts only, and every action is logged.
