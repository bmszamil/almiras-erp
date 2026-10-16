# AI agent gateway (design, not built yet)

Goal: let open-source AI agents (self-hosted models such as Llama, Qwen, Mistral or DeepSeek, run with tools like Ollama or vLLM) work with the ERP later, without making the ERP depend on any of them. The ERP must run fully without agents.

## Rules
1. **One door.** Agents reach the ERP only through the gateway API (`/api/agents/v1/`). No database access, no admin access.
2. **Named service users.** Each agent has its own user account, scoped to specific entities and actions. Tokens rotate and can be revoked instantly.
3. **Drafts only by default.** Agents create drafts (journal entries, bills, bank matches, reminders). A human approves and posts. Any agent permission to post is granted per action type, per entity, with an amount limit.
4. **Everything logged.** Every agent call records agent, model and version, prompt reference, input, output and the records touched. Agent-made records carry `source = "agent:<name>"`.
5. **Same rules as people.** Agents go through `services.py`, so all ledger, Sharia, period and entity rules apply unchanged.
6. **Data stays home.** Models run on Almiras servers in Saudi Arabia. Sending ERP data to an outside AI service needs an explicit, per-agent setting, off by default.
7. **Standard tool interface.** The gateway also exposes its actions as a Model Context Protocol (MCP) server, so any agent framework that speaks MCP can use the same tools and permissions.

## First candidate agents (later)
| Agent | Does | Posts? |
| --- | --- | --- |
| Invoice reader | Reads supplier PDFs, drafts bills | No, draft only |
| Bank matcher | Proposes matches for statement lines | No, accountant confirms |
| Expiry watcher | Flags CR, Iqama, licence and contract expiries | Creates reminders only |
| Month-end assistant | Drafts accruals and variance notes | No, draft only |
| Portfolio KPI collector | Pulls subsidiary KPIs into the investment module | Writes KPI data only |

## What to build now so this works later
- Add a `source` field (human / import / agent) to business records as each module is built.
- Keep business logic in `services.py` so the gateway can call it the same way the UI does.
- Build the REST API with per-entity permissions; the gateway is a restricted layer on top of it.
