# Main vs branch: comparison for reconciliation (17 Oct 2026)

Status: analysis for the project owner. Read-only comparison; nothing was merged or changed.
Lines compared, both from base `23120d2`:

- **Main** = `origin/main` at `e16cd9d` (commits `a8019d4..e16cd9d`, 11 commits, 83 files, +9,341 lines; 211 tests). Approved by the Managing Partner (MP) on 17 Oct 2026.
- **Branch** = `claude/funny-davinci-7yd88s` at local `5f78e37` (182 commits; `origin/claude/funny-davinci-7yd88s` holds 160 of them, the rest are not pushed yet; 307 files, about +77,000 lines; 1,073 tests). Another session also has uncommitted work in the working tree (hr, invoicing, zatca, `core/access/services.py`). It is not counted here.

## 1. Summary

**Main** built one cross-cutting layer:
- an `access` app with a custom user model (`AUTH_USER_MODEL = "access.User"`), roles and permissions stored as data, and per-entity `EntityAccess`;
- a general `AuditLog`, TOTP two-factor sign-in for every human (django-otp, with `OTPAdminSite`), lockout, service tokens and production-safe settings;
- a generic `approvals` engine: policies, steps, delegation, escalation and notifications, with a DRF REST API at `/api/v1/`;
- a ledger plug-in for manual journals: `draft → pending_approval → approved → posted`, where the final approval posts the entry automatically.

Main has no business modules. Its specs for billing (`intercompany-billing.md`) and for approvals/web UI are written, but only the approvals part is built.

**Branch** built the business system:
- `core.access`: Django `auth.User` + `UserProfile`, `Membership`, a role catalogue with flags kept in code, and `require()`/`scope()`. Grants are single-use and tied to one document. Dual control covers the ZATCA production switch and Sharia switch-off (MP, then Chairman). `AccessLog` is append-only, with PostgreSQL triggers.
- In the ledger: multi-currency (ledger-fx), a document registry for sub-ledger postings, and status, link and append-only triggers.
- The apps `invoicing`, `zatca`, `hr`, `billing` and `banking`.
- Specs for all of these, plus payroll, consolidation, fixed assets and tax returns (some not built yet).

Approvals are **per-module services**. Each module has its own submit/approve functions, and an approval is a document-bound grant that the ledger checks when it posts. There is no generic engine, no REST API, no MFA, no custom user model, and settings are not hardened (`DEBUG` still defaults to on).

**Core incompatibilities**

1. **Two access models for the same job.** Main has `access.User` + `EntityAccess` + DB-stored `Permission` with codes like `ledger.entry.create`. Branch has `auth.User` + `UserProfile` + `Membership` + a code catalogue with codes like `ledger.create_entry` and flags A/P/DC/S!/G/Sys/Ag/I.
2. **Ledger posting API and states differ.**
   - Main: `post_entry(entry, user)`, with the states `pending_approval`/`approved`/`rejected`, a free-text `source`, and `requires_approval`.
   - Branch: `post_entry(entry, grant, posted_by=)`, `create_entry(..., grant=, document=)`, only the states draft/posted/reversed (enforced by PostgreSQL triggers), and `source` as a 6-character choice.
   - Both sides add a `source` column to `JournalEntry`, with different definitions.
3. **Who approves.**
   - Main (MP decisions): the MP approves everything; logged MP self-approval is allowed; the CFO has no approve or post rights.
   - Branch (project-owner decisions): the CFO (`approver`) approves; the MP is the fallback; self-approval never happens on postings; the HR officer approves payroll.
4. **Intercompany billing.** Main has a mirror draft bill in the buyer, timesheets and cost-plus, and invoices dated on the last day of the month. Branch has no mirror bill, contract-based pricing, and invoices dated on the run date.
5. **Entity scope and retention.**
   - Scope: main is strictly per entity. On the branch, group roles cover fully consolidated descendants.
   - Retention: main keeps everything forever. The branch specs have an (off-by-default) personal-data anonymisation job and a 10-year AccessLog default.

## 2. Architecture comparison

| Area | Main (`origin/main`) | Branch | Fit |
|---|---|---|---|
| User model | Custom `access.User(AbstractUser)`: `full_name_en/ar`, `language`, `user_type` (human / service_agent / service_integration), `agent_name`, `default_entity`, `keycloak_id`. Core and ledger 0001 migrations regenerated. | `auth.User` + `UserProfile` (OneToOne): `kind` (human/system/agent), `display_name_en/ar`, `preferred_language`, `system_code`, `agent_name/model/version`, `allow_external_ai`. All FKs use `settings.AUTH_USER_MODEL`. | Fields map almost 1:1. A swap is feasible because the branch never hard-codes `auth.User` (except one test). |
| Roles / permissions | `Permission` and `Role` rows in the DB (`access/catalogue.py` seeds them). Roles: managing_partner, chairman, cfo, accountant, business_analyst, pa_drafter, auditor, viewer, agent_drafter. Service users lose `SERVICE_FORBIDDEN` codes. | Catalogue in code with flags (A approval, P posting, DC dual control, S! hard segregation, G group, Sys:x, Ag agent-allowed, I inactive-entity). Roles: receivables/payables clerk, accountant, approver (CFO), viewer, zatca_admin (add-on), access_admin, managing_partner (G), chairman (G), group_treasury, hr_officer, payroll_officer, department_head (proposed), group_viewer, 3 system roles, agent roles. Seeded through 7 migrations. | Different codes and semantics. The branch catalogue is richer (flags are enforced at role save and in `require`). |
| Entity access | `EntityAccess` (user × entity × role, valid dates, revocable). **Strictly per entity (D12).** Superuser bypass only in `ERP_ADMIN_SETUP_MODE`. | `Membership` (user × entity × role, aware datetimes, append-only, revocation within 10 s, DB triggers). **Group roles on the parent cover `full` descendants (R6)**; equity/none entities need explicit membership. Superuser gets no business rights. | Conflicting on group coverage. |
| Check API | `has_perm(user, entity, perm)`, `require_perm`, `scope(qs, user, perm)` | `require(user, entity, perm, document=, allow_self_approval=)` → `Grant` (logged); `has`, `scope`, `verify_grant`, `use_grant`, `require_dual_control` | `scope` has the same shape. `require` is stronger: it returns a logged, single-use, document-bound grant. |
| Approvals | Generic engine. `ApprovableType` registry and adapters (`APPROVAL_ADAPTERS`). Policy resolution by entity, amount and priority. Sequential and parallel steps. `min_approvals`, escalation, expiry, `Delegation` (entity/type/amount/dates), document-version hash, idempotency keys, notifications (in-app and email), `PushDevice` hook. MP self-approval override (D10/D16). | Per-module services (`submit_*`/`approve_*` in invoicing, billing, hr/payroll, banking, ledger). An approval is `require(approver, entity, A-perm, document=doc)`, and the grant is consumed by the posting. CFO delegation = time-bounded `approver` membership (R16). `DualControlRequest` for DC permissions. No escalation, inbox or notifications. A waiting document stays waiting (R18). | Main has the workflow/inbox layer. The branch has the authorisation and posting binding. They are complementary but overlap on "who may approve". |
| MFA | TOTP for every human (D14): `OTPAdminSite`, `IsTwoFactorAuthenticated` on the API, `setup_totp` command, lockout after 5 failures per user or 20 per IP. | None. OQ-4 is open (default: TOTP for approver/zatca_admin/access_admin/MP/superusers). | Main is ahead. Can be ported, since django-otp works with any user model. |
| Audit log | General `AuditLog` (actor, on_behalf_of, entity, action, object, before/after, source, ip, user agent, channel, request_id). Append-only in the model only (DB grants pending). Covers ledger create/post/reverse, periods, sign-in, admin changes. | `AccessLog` for access events (grants, denials written durably on a second DB connection, dual control, entity settings, grant use, `post_on_clearance`). Each module has its own append-only event tables (e.g. `ContractEvent`). PostgreSQL append-only triggers. | Different scope. They could coexist: AuditLog as the general trail, AccessLog for authorisation. |
| Ledger posting flow | Manual entry: `create_entry` → draft (`requires_approval=True`) → `approvals.submit` → `pending_approval` → `approved` → `post_entry(entry, user)` in the same transaction. `system=True` entries (module services, no human) post directly. Reversal: permission + mandatory reason (D15). `JournalEntryEditor` counts editors as creators. Bulk-update guards. | Manual: draft → `submit_entry` → `post_entry(entry, grant)` with a `ledger.post_entry` grant from a non-creator/submitter/modifier. Sub-ledger: `create_entry(post=True, grant=, document=)` through the document registry (one booking per document, line hooks, reversal must mirror the original). FX lines, revaluation runs. PostgreSQL triggers on status, links and rate locks. Reversal by grant; reason optional. | Incompatible signatures and state sets. The branch ledger is much larger (≈1,850 changed lines in `services.py`). |
| Migrations | `core` 0001 (header only), `ledger` 0001 regenerated (adds status values, `source`, `requires_approval`), `ledger` 0002–0003, `access` 0001–0005, `approvals` 0001–0003, `testdocs` 0001. | `core` 0002–0016, `ledger` 0002–0015 (+0016 uncommitted), invoicing 0001–0015, hr, billing, banking, zatca, `testdocs` 0001–0002. 38 migrations contain RunSQL triggers. | Two `ledger` leaf chains, a duplicate `source` column and two different `testdocs` apps. |
| REST API | DRF `/api/v1/`: `me`, approval requests (list/detail/approve/reject/return/cancel), summary, self-approval report, delegations, policies, notifications, devices, audit, access review. Service-token auth. | None (admin only; `core/views.py` and `ledger/views.py` are minimal). | Main only. |
| Admin | `OTPAdminSite`. Querysets scoped by EntityAccess. Bulk delete removed. Periods closed only through the service. | `EntityScopedAdmin` applies `scope`. Entity sensitive fields are read-only (changed through `change_entity_settings`). Seeded roles are read-only. | Both are scoped. They need merging, not a choice. |
| Agents / service users | `user_type` service_agent / service_integration. Both are drafts-only (D17). Hashed expiring `ServiceToken` + DRF. Reserved `system` user for `system=True` entries. | Kinds `agent` (Ag permissions only, never A/P/DC/S!/G/Sys) and `system` (`system_billing`, `system_zatca_queue`, `system_fx_import`, each holding only its Sys permissions; billing auto-issues and the ZATCA queue posts on clearance). Gateway tokens are designed but not built. | Main's "integration" ≈ an external system. The branch's "system" users are internal module workers that do issue/post. |
| Security settings | `DEBUG` off by default, secret key required, HSTS, secure cookies, 30-min idle session, SMTP outside DEBUG, trusted proxies. | `DEBUG` defaults to on; dev secret key default. Adds a second `access_log` DB connection, grant max age, HR/banking encryption keys, ZATCA/OpenBao settings. | Take main's settings and add the branch's. |

## 3. Decision conflicts

Main decisions: `approvals-and-web-ui.md` D1–D18 (labelled A-Dn below) and `intercompany-billing.md` D1–D12 (B-Dn), all by the MP. Branch decisions: the project owner's, in each spec's Decisions table (access A1–A22, billing D1–D12, invoicing D17–D30, hr/payroll H/P, zatca D-n).

| Topic | Main | Branch | Status | Owner must choose |
|---|---|---|---|---|
| Who approves | A-D2 / B-D7: MP approves all payments and spending; matrix later as data. | A1, invoicing D17, billing D5: the CFO (`approver`) approves; MP is the fallback approver (OQ-1 default). | **Conflicting** | MP-only at go-live, or CFO with MP fallback, or MP as policy step 1 with CFO later. |
| MP self-approval | A-D10: allowed as a logged override when the MP is creator and sole eligible approver; monthly report. | R14/R35: creator, submitter and last modifier can never approve or post (ledger always on). Only switch: invoicing `segregation_required` for non-posting approvals. Exception: receipts (A16). | **Conflicting** | Keep D10 (then the ledger registry needs an explicit, logged MP override) or forbid it. |
| Auto-post on approval | A-D11: final approval posts in the same transaction; a failure rolls back the approval. | A1/D5: posting is automatic on approval; sales post on ZATCA clearance (zatca D-8). | **Same** (different mechanism) | Confirm that sales wait for ZATCA clearance under main's engine too. |
| Per-entity access | A-D12: a role at Almiras gives nothing in subsidiaries. | R5 same for entity roles, but R6: group roles (MP, group_viewer, treasury, chairman) cover `full` descendants. | **Conflicting** (group roles) | Do group roles cascade to fully owned subsidiaries, or is every entity granted separately? |
| Go-live users | A-D13: MP, Chairman, Accountant, PA (drafts and uploads), external auditor (read-only), CFO (main build: no approve/post), business analyst (some modules). | No PA, analyst or external-auditor roles (`viewer` = viewer/auditor). CFO = `approver` with approve/post. Adds clerks, zatca_admin, hr/payroll officers, department_head. | **Conflicting** (CFO powers) / **Compatible** (missing roles can be added as data) | CFO rights; whether PA and analyst roles are needed on the branch catalogue. |
| 2FA | A-D14: every human, password + authenticator app. | OQ-4 open; default TOTP for privileged roles only; not built. | **Compatible** (the branch has no decision) | Confirm that D14 applies to the whole system. |
| Reversals | A-D15: no approval; needs `ledger.entry.reverse` or `.post`; reason mandatory; audited. | `ledger.reverse_entry` grant (accountant), single-use, document-bound; reason optional; sub-ledger reversals by module permission. | **Compatible** (make the reason mandatory) | — |
| Delegation vs self-approval | A-D16: an active delegation blocks MP self-approval; the delegate approves. | R16: CFO delegate = time-bounded approver membership; no self-approval exists; R18: no automatic rerouting. | **Compatible** if D10 is kept; otherwise moot | Whether main's general `Delegation` (by type and amount) replaces the branch's CFO-only delegation. |
| Integrations drafts-only | A-D17: `service_integration` = agents: drafts only, never post or approve. | System users `system_billing` (auto-issues invoices from approved contracts, billing D8 / invoicing D20) and `system_zatca_queue` (posts on clearance) do issue/post. | **Conflicting** unless "integration" ≠ internal system user | Are internal module workers allowed to issue/post under a prior human approval? |
| Retention | A-D18: keep all accounting, approval and audit records forever; no purge code. | invoicing R52: anonymise personal data after `retention_until` (job disabled by default); access OQ-10 default 10 years. | **Conflicting** (defaults) | Forever vs a PDPL-driven anonymisation job (legal sign-off). |
| Generic approval for all document types | A-D9: one generic approval for JEs, bills, payments, IC invoices, purchase requests, contracts. | Approval per module service, enforced through grants. | **Conflicting** (architecture) | Whether all modules must route through the engine. |
| Agents | A-D8 / B-D10: named service users, drafts only, `source="agent:<name>"`. | A6/A7 same; agent roles limited to Ag permissions. | **Same** | — |
| Bilingual / Saudi hosting / open source / React + Keycloak | A-D3, D5, D6 | Same principles; Django login, Keycloak not mentioned. | **Same / Compatible** | — |
| Every recharge is a VAT invoice | B-D2 (VAT group question left open, Q2) | billing D2: no entity is in a VAT group; normal VAT invoices. | **Same** (the branch answers main's Q2) | — |
| No late-payment penalties | B-D11: no penalty feature. | billing R29, invoicing R7 / OQ-15: no late-charge feature; never income. | **Same** | — |
| Mirror bill in the buyer | B-D8: automatic draft bill when both entities are on the ERP. | billing D10, invoicing D23: **no** automatic bill; buyer staff enter it. | **Conflicting** | Mirror draft bill or manual bill entry. |
| Pricing methods | B-D3: timesheet, fixed fee, cost-plus per agreement. | Contract lines with entered amounts and price steps; no timesheets; cost-plus not built (billing OQ-30). | **Conflicting / gap** | Are timesheets and cost-plus needed at go-live? |
| Cost-plus basis | B-D12: per agreement, fixed %, headcount or revenue. | Not built; secondment at the entered contract price (R65). | **Compatible** (branch gap) | Priority of building cost pools. |
| Invoice dating | B-D12: monthly invoices dated the **last day of the service month**. | billing R15: issue date = run date, never backdated; supply period = the month. Notes dated on their own date (D12). | **Conflicting** | Last day of month (needs a run on or after month end and a compliance check on backdating) or the run date. |
| Auto-generated invoices | B-R9/R10: the billing run creates drafts; MP approves each invoice. | billing D8 / invoicing D20: invoices from a CFO-approved contract need **no per-invoice approval**. | **Conflicting** | Per-invoice approval or per-contract approval. |
| Payroll / loans approver | A-D2: MP approves all spending. | A21: the HR officer approves payroll; A22: the MP approves loans and bai' ajel; overtime by MP (Almiras) or department head. | **Conflicting** for payroll | Does D2 cover payroll? |
| Sharia switch-off | No decision; Chairman only as an escalation target. | A17: two steps, MP request then Chairman approval; Sharia on = MP alone. | **Compatible** (main is silent) | Confirm A17; the Chairman's role covers both. |
| Receipts by their recorder | No decision (D7: creator of a payment cannot approve it). | A16 / invoicing D26: the recorder may post a customer receipt (no bank charge, banking B7). | **Compatible** (receipts ≠ payments), but confirm | — |
| Custom user model | Build note 1 / OQ1: custom user done. | access OQ-9 default: keep `auth.User` + profile. | **Conflicting** (main has built it) | Custom user (recommended, before any production DB). |

## 4. Overlapping specs

### 4.1 `intercompany-billing.md` (main) vs `billing.md` (branch)

| Aspect | Main `intercompany-billing.md` | Branch `billing.md` |
|---|---|---|
| Scope | Intercompany only: shared services, rentals, IP, secondments, consultations. | Any contract-based recurring invoicing (car rentals, leases, secondments), external and intercompany. |
| Core object | `ServiceAgreement` + `TimesheetEntry` + `CostPool`/`CostPoolPeriod` + `BillableItem` | `Contract` + `ContractLine` + `ContractPriceStep` + `BillingPeriod` + `ContractVersion` + `BillingAdjustment` + `SecondmentAssignment` (HR link) |
| Invoice | Own `IntercompanyInvoice`/`InvoiceLine` with `ICI`/`ICC` numbering; posts itself through `create_entry`/`post_entry`. | No own invoice: calls `invoicing.create_from_billing`; the invoicing app owns numbering, ZATCA and posting (on clearance). |
| Buyer side | `IntercompanyBill` mirror draft (D8). | None (D10). Reconciliation report flags the differences. |
| Corrections | Credit notes on `credit_of`, capped at the uncredited balance. | Versioned amendments/terminations (also retroactive), auto-drafted credit/debit notes reviewed by a person, approved by the CFO (D9). |
| Approval | Generic `Approval` model, `required_approvals()` = MP. | CFO approves contracts and versions; auto-issue without per-invoice approval (D8). |
| Dating | Last day of service month (D12). | Run date; never backdated (R15). |
| Currency | `Decimal(18,2)`, currency explicit. | Multi-currency, 3-decimal contract currencies (D11), rates from ledger-fx. |
| Overlap verdict | The branch spec is the broader one and is built. Main's unique items are mirror bills, timesheets, cost pools and `BillableItem`. They could be added to the branch's billing as features if the owner keeps B-D3/B-D8/B-D12. |

### 4.2 `approvals-and-web-ui.md` (main) vs `access.md` (branch)

`web.md` does not exist in the branch working tree. The branch has no web UI spec.

| Aspect | Main | Branch `access.md` |
|---|---|---|
| Purpose | Users, roles, entity access, audit, **and** a generic approvals engine, notifications, REST API and the React/RTL UI shell. | Users, roles, memberships, permission catalogue, segregation, dual control, delegation, AccessLog, ledger posting authorisation, entity lifecycle. |
| Approval routing | Data-driven policies and steps (R5–R7), escalation (R15), expiry, delegation (R16). | None. The module decides who approves; R18: no automatic routing. |
| Segregation | R8: creator and submitter cannot approve; editors count as creators; D10 MP exception. | R14: creator, submitter and modifier cannot approve (A); R35: ledger posting always segregated; S! permissions can never be switched; A16 receipts exception. |
| Posting binding | `on_approved` calls `post_entry`; approval rolled back on failure. | Grant tied to one document, single-use, verified against its AccessLog row (R30/R31); ledger document registry (R34–R37). |
| Sensitive settings | Not covered (entities setup-only in admin). | `change_entity_settings` (logged), dual-control Sharia off (R48), entity lifecycle (R38–R40), DB role hardening (R45). |
| Auth | Keycloak later; local password + TOTP now; service tokens. | Django login; MFA open; agent tokens via gateway (not built). |
| UI/API | DRF API; React SPA rules (RTL, WCAG, entity switcher, inbox). | None. |
| Overlap verdict | The two specs are complementary in layers. Main = workflow, identity and channel. Branch = authorisation and posting integrity. They conflict on role semantics, group coverage, self-approval and the user model. |

## 5. Migration and merge impact

**Textual conflicts** (`git merge-tree HEAD origin/main`), 13 files:
- `README.md`, `config/settings.py`, `requirements.txt`
- `config/settings_test.py`, `conftest.py` (both add/add)
- `core/admin.py`, `ledger/admin.py`, `ledger/models.py`, `ledger/services.py`, `ledger/tests.py`
- `testdocs/apps.py`, `testdocs/models.py`, `testdocs/migrations/0001_initial.py` (all add/add)

**Semantic conflicts that git does not show:**
- **Migration graph.**
  - Two `ledger` leaf chains: main has `0002_requires_approval_help`/`0003_review_a_editors`, the branch has `0002_fx_schema`…`0015`.
  - Main's regenerated `ledger/0001` already contains `source` (CharField 60) and `requires_approval`, and branch `0006` adds `source` again (choices, max 6), so a fresh migrate fails on the duplicate column.
  - Two unrelated `testdocs` apps.
- **Triggers vs states.** Branch PostgreSQL triggers (`ledger/0009`) allow only draft→posted→reversed. Main's `pending_approval`/`approved`/`rejected` are refused at DB level.
- **API break.** Every branch caller uses `post_entry(entry, grant)`, `reverse_entry(entry, grant, ...)` and `create_entry(..., grant=, document=)`. Main's signatures are `(entry, user)` and `(..., user, system=)`.
- **Two auth systems.** `AUTHENTICATION_BACKENDS`, two permission vocabularies, two `system` user concepts, two audit tables.
- **Mechanically:** a merge commit can be produced, but it will not import or migrate without picking one side for access, ledger, settings and testdocs. A plain merge is **not feasible**; it is a port either way.

Scale of call sites on the branch: about 220 `require(...)` calls across `banking`, `billing`, `hr`, `invoicing`, `ledger`, `zatca` and `core` services. 87 files reference `core.access`. 42 test files build memberships. The branch has 1,073 tests and main has 211.

| Option | What gets rewritten (approx.) | Security rules at risk | Preserved | Effort |
|---|---|---|---|---|
| **A. Port branch modules onto main's access + approvals** | Every module's authorisation (~220 `require` sites, 6 apps); ledger posting/registry rebuilt on `post_entry(entry, user)` + engine states; branch triggers rewritten for new states; `core/access` (~2,500 LOC) and 15 `core` migrations (7 of them seeds) dropped or rewritten; flag semantics (A/P/DC/S!/Sys/Ag/I) re-implemented in main's `Permission` rows; ~40–65 test files (several hundred tests) reworked; module-specific approvals (CFO, HR officer, ZATCA DC, clearance posting, auto-issue) expressed as policies or adapters. | Grants tied to one document and single-use (R30/R31), grant verification against the log, durable denial logging (R27), dual control (ZATCA production, Sharia off with Chairman), Sys-only permissions for system users, the agent permission whitelist enforced at role save, one booking per document (R36), reversal mirror checks, append-only Membership triggers. All must be rebuilt. Some, like single-use document binding, have no equivalent in main's engine today. | Main as approved by the MP; its API, MFA and engine untouched. | **Very high.** Largest rewrite, highest regression risk on the 1,073 branch tests. |
| **B. Keep branch access, port main's approvals engine** | Approvals engine (~950 LOC services, ~380 views, ~150 serializers, ~370 models) re-plumbed on `Membership`/catalogue and `require` (eligibility = holder of the A permission in the entity, `on_approved` obtains the approver's grant and calls the module service); main's access features re-implemented for `auth.User` + `UserProfile`: TOTP/OTPAdminSite, lockout, service tokens, access review, security settings (~1,100 LOC + ~900 test LOC); the ledger adapter rewritten so a manual JE stays `draft` (locked while a request is pending) instead of new statuses; ~150 of main's 211 tests rewritten. | Main's custom user (OQ1, approved) is dropped; D10 self-approval must be added explicitly to `require`/registry; main's general `AuditLog` coverage (sign-in, admin changes) must be re-added. | All branch modules and triggers; branch security model; main's workflow features. | **Medium-high.** |
| **C. Hybrid (recommended shape)**: branch as the base; main's custom `access.User` (merged with `UserProfile` fields), TOTP, lockout, service tokens, hardened settings, DRF API and approvals engine as the workflow/inbox layer; branch `core.access` stays the authorisation layer (roles/catalogue/Membership/require/grants/dual control) | Swap `AUTH_USER_MODEL` (branch code already uses `settings.AUTH_USER_MODEL`; fresh DB, no production data); fold `UserProfile` into `User` or keep it as a profile of the new user; drop main's `EntityAccess`/`Role`/`Permission` and map its role codes to the branch catalogue (add `cfo`/`pa_drafter`/`business_analyst`/`auditor` as data if wanted); engine eligibility through `core.access.has`, posting through module services with the approver's grant; ledger keeps branch states, and the approval request locks the draft; keep `AuditLog` for general events and `AccessLog` for authorisation; drop main's `ledger/0001` changes and `0002/0003`, and re-add `JournalEntryEditor` as a branch migration if wanted; merge the two `testdocs` apps. Approx.: main `access/services.py` ~50% rewritten, `approvals/services.py` ~30% touched, ~120 of main's tests adapted; branch: test fixtures centralised in `core/access/testing.py` plus ~40 files of user-creation edits. | Low, provided that: (1) the engine never posts without a grant (keep R30/R31); (2) D10 is decided before wiring; (3) system users stay outside the engine's "integration" class or D17 is reworded. | All branch modules, triggers, document registry, dual control and grants; main's MFA, API, engine, delegation/escalation/notifications, security settings. | **Medium.** Most code survives on both sides; main's role/access tables are the main loss. |

## 6. Recommendation

**Take option C, with the branch as the base line**, after the owner settles the conflicting decisions below. Reasons:
- The branch carries six built modules, about 220 authorisation call sites and 1,073 tests. Its access layer encodes rules main does not have: single-use document-bound grants, dual control with the Chairman, system and agent permission limits, posting integrity triggers. Rebuilding those on main (A) is the costliest path and the one most likely to lose a security rule.
- Main's unique value sits in separate layers: the custom user, MFA, hardened settings, the REST API and the generic approval workflow. These sit **on top of** the branch's authorisation layer rather than replacing it, so C keeps nearly all of both.
- The custom user must be decided now, while no production database exists. Main has already done it and the branch code is ready for it.
- Main remains the approved record of the MP's decisions. The reconciled line should be reviewed again by the code-reviewer and the compliance-checker, then approved by a human, before it replaces `main`.

Merge mechanics for C:
1. Create a new integration branch from the branch.
2. Bring main's `access`/`approvals` code in file by file. Do not use `git merge` resolution on the ledger.
3. Regenerate or renumber the affected migrations on a fresh DB.
4. Run both test suites on SQLite and PostgreSQL.

**Questions the owner must answer first**

1. **Authority:** are the "project owner" decisions on the branch and the MP's decisions on main from the same person? Where they differ, which one prevails?
2. **Approver:** at go-live, does the MP approve all spending and documents (main D2), or the CFO with the MP as fallback (branch A1)? Does payroll go to the HR officer (A21) or the MP?
3. **Self-approval:** keep the logged MP override (D10/D16), including for ledger postings, or forbid it as the branch does?
4. **Group roles:** do MP, group viewer, treasury and chairman roles cascade to fully owned subsidiaries (branch R6), or is each entity granted separately (main D12)?
5. **CFO powers and extra roles:** does the CFO approve and post (branch), or is the CFO accountant-plus-audit (main)? Are PA, business analyst and external auditor roles needed?
6. **System users vs D17:** may internal system users (billing auto-issue, ZATCA queue posting on clearance) issue and post after a human's prior approval?
7. **Auto-generated invoices:** per-contract approval (branch D8) or per-invoice approval (main R9/R10)?
8. **Intercompany:** mirror draft bill (main B-D8) or manual bill entry (branch D10)? Are timesheets and cost-plus cost pools (main B-D3/B-D12) needed at go-live?
9. **Invoice dating:** last day of the service month (main B-D12) or the run date with supply period (branch R15), subject to compliance-checker review?
10. **Generic engine scope:** must every approvable document go through the engine (main D9), or only manual journals and future payables, payments and purchasing, with existing modules keeping their own services?
11. **Retention:** keep everything forever (D18), or allow the PDPL anonymisation job (invoicing R52) after legal sign-off?
12. **Custom user model:** confirm the custom `access.User` (main OQ1) over `auth.User` + profile (branch OQ-9).
13. **Reconciliation plan:** confirm option C and the branch-based integration line, and who reviews and approves it before it replaces `main`.

## 7. Owner answers 2026-10-18

Source: `docs/approvals/owner-answers-2026-10-18.md` §A (and B1, B7). Recorded in the specs as dated decisions "project owner, 2026-10-18".

**Wording note.** In questions 1 and 13, "branch" meant the **code branch** (`claude/funny-davinci-7yd88s`), not a company branch. The owner answered about **company governance** (who holds authority in each company). The answers are recorded as company governance; their effect on the code lines is the team's reading, listed under "Integration effect".

| Q | Topic | Owner's answer (condensed) | Recorded as | Integration effect |
|---|---|---|---|---|
| 1 | Authority | Authority in each company lies with that company's CEO / GM; the Almiras MP is board member or shareholder representative only. *Team:* the owner's current answers prevail over earlier decisions on either line. | access A24 | Neither `main`'s "MP approves everything" (A-D2, B-D7) nor the branch's "CFO approves" (A1) stands as such; approvers come from data (Q2) |
| 2 | Approver | MP does not approve everything; each company has its own **authority matrix** for every task and activity; contracted payroll needs no MP; overtime in Almiras needs the MP; other companies have their own structures. | access A25, R57; payroll P20; hr H21 | New per-entity `AuthorityRule` data (activity × amount band × role), replacing `main`'s policies; seed rows copy today's decided holders; each entity's content is open (access OQ-30, go-live) |
| 3 | Self-approval | Only the highest authority of each entity (MP in Almiras, CEO elsewhere) may self-approve; nobody else ever; every such approval logged and in the monthly governance / internal-audit report. | access A26, R58, R59 | Keeps a narrowed `main` D10 (per-entity configurable top authority); the branch ledger registry must accept a logged top-authority grant (R35 note); invoicing `segregation_required` switch removed |
| 4 | Group roles | Each company granted separately; no cascade. | access A27, R6 replaced, R64, R65 | `main` D12 wins; branch R6 coverage of `full` descendants is removed; tests built on cascade change |
| 5 | CFO powers, extra roles | CEO approves and posts and can act as accountant; roles internal auditor, PA, business analyst, external auditor needed. | access A28, R60 | New roles as data (`ceo`, `internal_auditor`, `personal_assistant`, `business_analyst`, `external_auditor`); CFO (`approver`) rights set per entity by its matrix |
| 6 | System users vs D17 | Yes: system users may issue and post after a person's prior approval. | access A29, R63 | Branch behaviour kept (billing auto-issue, ZATCA queue posting); `main` D17 reworded to apply to external integration and agent users only |
| 7 | Auto-generated invoices | One approval per contract. | billing D14(a), invoicing D33(b) | Branch D8 kept; `main` B-R9/R10 per-invoice approval dropped |
| 8 | Intercompany | Automatic mirror bill in the buying company; cost-plus, timesheets and cost pools at go-live. | billing D14(b), (c), R73 to R76; invoicing R113 | `main` B-D8, B-D3, B-D12 adopted into the branch's billing (mirror draft via `invoicing.create_mirror_bill`, `TimesheetEntry`, `CostPool`); branch D10 / invoicing D23 superseded |
| 9 | Invoice dating | Last day of the service month, unless ZATCA requires otherwise (team to advise); earlier end date when the service ends earlier. | billing D14(d), R77, R78; zatca D-17(b), R-53 | Team advice: ZATCA `IssueDate` = generation date, never backdated, so the run happens **on** that day; a later run carries its own date with the supply period (KSA-5/KSA-24) for the month; tax invoice due by the 15th of the following month (VAT IR Art. 53 **[verify]**). Branch R15 no-backdating kept; `main` B-D12 date achieved by scheduling |
| 10 | Generic engine scope | All approvable documents go through the same engine. | access A25, R61; billing R79; invoicing R114; migration M7 | `main`'s approvals engine becomes the single workflow layer for every module; module approve functions are reached only through engine adapters, with the branch's single-use grants |
| 11 | Retention | Keep everything; data older than 10 years nominated for destruction; destruction decided by an internal audit committee approval minute (B7). | access A32, R55; invoicing R108; hr R67; payroll R31 | `main` D18 "keep forever" and the branch nomination model combine: no automatic deletion; `burn` needs the committee minute reference |
| 12 | User model | Follow best practice for large groups / large ERPs. *Team:* custom user model. | access A30, R66 | `main`'s `access.User` adopted, `UserProfile` fields folded in, on a fresh database |
| 13 | Reconciliation plan | Option C confirmed; the company CEO approves and reviews, and may delegate. *Team:* the project owner (or their delegate) reviews and approves the integration before it replaces `main`. | access A31, R62 | Option C, branch as base; human approval by the project owner or a named delegate (delegation recorded with dates) |

**Resulting integration plan (option C, updated 2026-10-18)**

1. New integration branch from the code branch (§6 step 1).
2. Bring in `main`'s `access.User`, TOTP, lockout, service tokens, hardened settings and DRF API; set `AUTH_USER_MODEL = "access.User"` and fold `UserProfile` (Q12). Drop `main`'s `EntityAccess`/`Role`/`Permission`; map its roles to the branch catalogue plus the new roles (Q5).
3. Bring in `main`'s `approvals` engine as the only workflow layer (Q10): replace its policies with `EntityGovernance` + `AuthorityRule` (Q2, Q3), its `Delegation` with `ApprovalDelegation` (Q13), eligibility through `core.access.has`, final decisions calling module services with single-use document-bound grants; ledger keeps `draft/posted/reversed`; the open request locks the draft.
4. Remove group cascade from `core.access` (Q4); restrict `allow_self_approval` to the top-authority path and teach the ledger registry to accept it (Q3); remove invoicing's `segregation_required`.
5. Billing: mirror draft bills, timesheets, cost pools, cost-plus, month-end dating run (Q7 to Q9).
6. Seed each entity's matrix from today's decided holders; collect real matrices per entity before go-live (access OQ-30).
7. Migrations regenerated on a fresh DB; both test suites run on SQLite and PostgreSQL (§6 steps 3, 4).
8. Code-reviewer and compliance-checker review; the project owner (or a named delegate) approves before the integration replaces `main` (Q13).

**Still open after these answers** (detail in the specs): each entity's matrix content (access OQ-30), who maintains it (OQ-31), self-approval conditions and delegation interplay (OQ-32), report recipients (OQ-33), escalation (OQ-34), amount bases (OQ-35), external auditor scope (OQ-36), PA / analyst permission sets (OQ-37), G permissions on the group parent vs no cascade (OQ-38), loan and bai' ajel approvers outside Almiras (OQ-39), receipts vs "no self-approval" (OQ-40), CFO alongside CEO (OQ-41); billing mirror scope and run time (billing OQ-39 to OQ-45); MFA scope (`main` D14 vs access OQ-4: not answered).
