# Spec: Approvals engine + bilingual web UI foundation

Status: draft for review · Date: 16 Oct 2026 · Apps: `access` (users, roles, entity access, audit), `approvals` (generic engine, notifications, API), `web` (React front end shell)

## 1. Purpose

Gives every entity in the group (Almiras holding first, then subsidiaries as data rows) one way to manage who can see and do what per entity, and one generic approval workflow that any module (ledger, payables, payments, intercompany, purchasing, contracts) plugs into. It also sets up the bilingual (Arabic RTL / English) web UI shell: sign-in through Keycloak, navigation, language and entity switchers, the approvals inbox and a simple home dashboard. It defines the approvals REST API that the web UI uses now and the mobile app will use later. No accounting is posted by this module itself.

## 2. Decisions

All by Badr Alzamil, Managing Partner (MP), 16 Oct 2026.

| # | Decision |
| --- | --- |
| D1 | Start with 1–5 users at holding (Almiras) level. The group will grow to 500+ employees across entities later; HR self-service is a later wave, not this spec. |
| D2 | "Managing Partner approves all payments and spending for now." The design must support a future approval matrix — tiers by amount, entity and document type, and escalation to the Chairman/board above a limit — configured as data, not code. |
| D3 | Arabic and English, switchable per user; full right-to-left layout for Arabic; reports bilingual. |
| D4 | A native mobile app (approvals, alerts, dashboard) comes in a later wave. This spec defines the approvals API it will use and push-notification hooks, not the app. |
| D5 | Hosting in a Saudi cloud region; data stays in Saudi Arabia. |
| D6 | Everything in-house and open source: Django, PostgreSQL, React front end, Keycloak for single sign-on. No paid SaaS. |
| D7 | Every approval and action is logged (who, when, before/after). Segregation of duties: the creator of a payment cannot approve it. |
| D8 | Future AI agents act as named service users through the gateway, may only create drafts, and their records carry `source = "agent:<name>"`. |
| D9 | Approvable documents: manual journal entries, supplier bills, payments, intercompany invoices, purchase requests, contracts — through one generic approval that any module plugs into. |
| D10 | (16 Oct 2026, answers to OQ2/OQ4/OQ5) **MP self-approval allowed, logged:** when the MP creates a document and is the only eligible approver, he may approve it himself. The action is recorded as `self_approval_override` in the audit log and listed in a monthly self-approval report for the Chairman and auditor. No other user may self-approve. Replaces the "never self-approves" outcome in R9. |
| D11 | **Approval posts automatically:** when the final approval is given, the owning module posts/executes the document in the same transaction (e.g. `ledger.services.post_entry`). If posting fails a ledger rule, the approval is rolled back and the approver sees the reason. |
| D13 | (16 Oct 2026) **Go-live users at Almiras:** Managing Partner; Chairman; Accountant; Faisal (PA, prepares drafts and uploads documents); External auditor (read-only); Almiras CFO; Business Analyst (access to some modules only). Roles must allow module-level scoping (e.g. analyst sees investments and deals, not payroll). |
| D14 | (16 Oct 2026) **Two-factor authentication for every user** (password + authenticator app). |
| D12 | **Access is per entity:** a role at Almiras gives no access to subsidiaries; each entity is granted separately through `EntityAccess`. |
| D15 | (17 Oct 2026) **Reversals without approval:** an accountant, or anyone holding `ledger.entry.post` or `ledger.entry.reverse` in that entity, may reverse a posted entry without approval. The permission is required, the reason is mandatory, and every reversal is audit-logged with its reason. |
| D16 | (17 Oct 2026) **Delegation beats self-approval:** if the MP has an active delegation covering the document, the delegate approves; MP self-approval (D10) is not allowed while that delegation exists. |
| D17 | (17 Oct 2026) **Integrations are drafts-only:** `service_integration` users are treated exactly like AI agents: they may create and submit drafts, never post or approve. |
| D18 | (17 Oct 2026) **Retention:** all accounting records, approval records and audit logs are kept forever. No purge code. PDPL storage-limitation review is pending legal sign-off. |
| D19 | (17 Oct 2026) **Managing Partner protection:** only a user holding the Chairman role may deactivate, lock, reset the password of, change roles/privileges of, revoke tokens/TOTP of, or otherwise disable a user holding the Managing Partner role (in any entity). Nobody else may, including other MPs and user managers; setup mode is excepted. This applies in services, admin and API, and every such action is audited. |

## 3. Data model

Conventions: every business record has `entity` FK to `core.Entity`; user-facing labels have `name_en` + `name_ar`; money is `Decimal(18,2)` with explicit `currency`; timestamps are timezone-aware (`Asia/Riyadh`). "Bilingual" = has `_en` and `_ar` fields.

### 3.1 Access (`access` app)

| Model | Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- | --- |
| **User** (custom `AUTH_USER_MODEL`, see OQ1) | `username` | char(150) | Y | | Matches Keycloak `preferred_username` |
| | `keycloak_id` | UUID | Y | | Keycloak `sub`; unique |
| | `full_name_en`, `full_name_ar` | char(200) | Y | Y | |
| | `email` | email | Y | | |
| | `language` | choice `en`/`ar` | Y | | Per-user UI and notification language (D3) |
| | `user_type` | choice `human`/`service_agent`/`service_integration` | Y | | Agents are `service_agent` (D8) |
| | `agent_name` | slug | if agent | | Used in `source = "agent:<name>"` |
| | `default_entity` | FK Entity | N | | Entity selected after sign-in |
| | `is_active` | bool | Y | | Deactivate, never delete |
| **Role** | `code` | slug | Y | | e.g. `managing_partner`, `chairman`, `accountant`, `viewer`, `agent_drafter` |
| | `name_en`, `name_ar` | char(100) | Y | Y | |
| | `permissions` | M2M Permission | Y | | |
| | `is_system` | bool | Y | | System roles cannot be deleted |
| **Permission** | `code` | char(100) | Y | | e.g. `ledger.entry.create`, `approvals.request.act` (list in §7) |
| | `name_en`, `name_ar` | char(200) | Y | Y | |
| **EntityAccess** | `user` | FK User | Y | | One row per user × entity × role |
| | `entity` | FK Entity | Y | | |
| | `role` | FK Role | Y | | Same user can hold different roles in different entities |
| | `valid_from`, `valid_to` | date | Y / N | | Open-ended if `valid_to` empty |
| | `granted_by` | FK User | Y | | |
| | unique | (`user`, `entity`, `role`) | | | |
| **AuditLog** (append-only) | `at` | datetime | Y | | |
| | `actor` | FK User | Y | | Human or service user |
| | `on_behalf_of` | FK User | N | | Set when acting as delegate |
| | `entity` | FK Entity | N | | Null only for global actions (e.g. role edits) |
| | `action` | char(60) | Y | | e.g. `approval.approve`, `entry.create`, `access.grant` |
| | `object_type`, `object_id` | char / char | Y | | Generic reference |
| | `before`, `after` | JSON | N | | Field-level snapshots (D7) |
| | `source` | char(60) | Y | | `human` / `import` / `agent:<name>` |
| | `ip`, `user_agent`, `channel` | char | N | | `channel`: `web` / `api` / `mobile` / `agent_gateway` |
| | `request_id` | UUID | Y | | Ties all rows of one request |

### 3.2 Approvals (`approvals` app)

| Model | Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- | --- |
| **ApprovableType** (registry) | `code` | slug | Y | | `ledger.journal_entry`, `payables.bill`, `payments.payment`, `intercompany.invoice`, `purchasing.request`, `contracts.contract` |
| | `name_en`, `name_ar` | char | Y | Y | |
| | `content_type` | FK ContentType | Y | | Django model of the document |
| | `is_spending` | bool | Y | | True for payments/spending types covered by D2 |
| **ApprovalPolicy** | `entity` | FK Entity | N | | Null = applies to all entities unless an entity-specific policy exists |
| | `document_type` | FK ApprovableType | Y | | |
| | `amount_min`, `amount_max` | decimal(18,2) | N | | Inclusive min, exclusive max; null = open |
| | `currency` | char(3) | Y | | Amounts compared in this currency (OQ6) |
| | `priority` | int | Y | | Most specific / highest priority wins |
| | `name_en`, `name_ar` | char | Y | Y | |
| | `valid_from`, `valid_to` | date | Y / N | | Policy changes are new versions, never edits of used policies |
| | `is_active` | bool | Y | | |
| **ApprovalStep** | `policy` | FK ApprovalPolicy | Y | | |
| | `sequence` | int | Y | | 1, 2, 3 … ; equal sequence = parallel (OQ7) |
| | `approver_role` | FK Role | one of | | Any user holding this role **in the document's entity** |
| | `approver_user` | FK User | one of | | Named user |
| | `min_approvals` | int | Y | | Default 1 |
| | `escalate_after_hours` | int | N | | Null = no timeout |
| | `escalate_to_role` / `escalate_to_user` | FK | N | | e.g. Chairman/board (D2) |
| **ApprovalRequest** | `entity` | FK Entity | Y | | Copied from document |
| | `document_type` | FK ApprovableType | Y | | |
| | `object_id` | char | Y | | Document PK |
| | `document_version` | char(64) | Y | | Hash of the document content at submission |
| | `amount`, `currency` | decimal / char | N | | Snapshot at submission |
| | `summary_en`, `summary_ar` | char(300) | Y | Y | Shown in inbox and push |
| | `policy` | FK ApprovalPolicy | Y | | Policy version used |
| | `status` | choice | Y | | `pending` / `approved` / `rejected` / `returned` / `cancelled` / `expired` |
| | `current_sequence` | int | Y | | |
| | `submitted_by` | FK User | Y | | |
| | `document_created_by` | FK User | Y | | Used for segregation of duties |
| | `source` | char(60) | Y | | `human` / `import` / `agent:<name>` |
| | `submitted_at`, `decided_at`, `expires_at` | datetime | Y / N / N | | |
| **ApprovalAction** (append-only) | `request` | FK ApprovalRequest | Y | | |
| | `step` | FK ApprovalStep | N | | |
| | `actor` | FK User | Y | | |
| | `on_behalf_of` | FK User | N | | Set when delegate acts |
| | `action` | choice | Y | | `submit` / `approve` / `reject` / `return` / `cancel` / `escalate` / `expire` / `comment` |
| | `comment` | text | req. for reject/return | | Stored as typed; not translated |
| | `at`, `channel` | datetime / choice | Y | | `web` / `api` / `mobile` / `email_link` (OQ9) |
| **Delegation** | `delegator` | FK User | Y | | Approver who is away |
| | `delegate` | FK User | Y | | |
| | `entity` | FK Entity | N | | Null = all entities where delegator approves |
| | `document_types` | M2M ApprovableType | N | | Empty = all |
| | `amount_max` | decimal(18,2) | N | | |
| | `starts_at`, `ends_at` | datetime | Y | | |
| | `reason` | char | N | | |
| **Notification** | `user` | FK User | Y | | |
| | `entity` | FK Entity | N | | |
| | `kind` | choice | Y | | `approval_pending`, `approval_decided`, `approval_returned`, `escalated`, `delegation_started`, `alert` |
| | `title_en`, `title_ar`, `body_en`, `body_ar` | char/text | Y | Y | Rendered in user's language |
| | `link` | char | Y | | Deep link (web route; mobile later) |
| | `channels_sent` | JSON | Y | | `{"in_app":…, "email":…, "push":…}` with status |
| | `read_at` | datetime | N | | |
| **PushDevice** (hook, used later) | `user`, `platform` (`ios`/`android`), `token`, `last_seen_at`, `is_active` | | Y | | Registered by mobile app later |

### 3.3 Plug-in contract for modules

A document model becomes approvable by:
1. Registering an `ApprovableType` row (data migration).
2. Having `entity`, `created_by`, `source`, `status` fields and an `approval_status`-compatible state (`draft` → `pending_approval` → `approved` → module-specific next step).
3. Implementing in its `services.py`: `approval_amount(doc)`, `approval_summary(doc)` (en/ar), `on_approved(doc, request)`, `on_rejected(doc, request)`, `on_returned(doc, request)`.
4. Calling `approvals.services.submit(doc, user)` — never creating `ApprovalRequest` rows directly.
5. Recording every person who creates or edits a draft. Any service, API endpoint, admin form or import that creates or edits a draft journal entry **must call `ledger.services.record_editor(entry, user, action)`** (`create_entry` and the ledger admin already do). Other modules provide the same record for their own documents through the adapter's `editor_ids(doc)`. The engine copies these editors to `ApprovalRequest.document_editors` at submission and treats them like the creator: they cannot approve (R8), and an MP editor who is the only approver can approve only as a logged D10 self-approval override. An edit that is not recorded would let its author approve their own change, so a missing call is a segregation-of-duties defect.

Note: `ledger.JournalEntry` today has no `source` field and no `pending_approval`/`approved` status; adding them is part of plugging the ledger in (see OQ3).

## 4. Rules

### 4.1 Access and approval rules

| # | Rule | Enforced by |
| --- | --- | --- |
| R1 | Every query in views/APIs returns only records of entities where the user has an active `EntityAccess` with the needed permission. | `access.services.scope(queryset, user, perm)` used by every view; API permission class; tests per endpoint |
| R2 | A user's permissions in an entity are the union of permissions of their roles in that entity only; roles in entity A give nothing in entity B. | `access.services.has_perm(user, entity, perm)` |
| R3 | Users are deactivated, never deleted, once they have any audit row. | `User.delete` raises; service `deactivate_user` |
| R4 | Sign-in is via Keycloak (OIDC) only; Django local passwords are disabled for humans. Service users authenticate with revocable tokens issued through the gateway. | Auth backend config; tests |
| R5 | Submitting a document picks exactly one active policy: entity-specific beats group-wide; then narrowest amount range; then highest `priority`. No match = submission refused with a bilingual error. | `approvals.services.resolve_policy`; DB check against overlapping ranges at same priority |
| R6 | Initial data: one group-wide policy per spending document type (`is_spending=True`), any amount, one step, approver = role `managing_partner` (D2). | Data migration; test |
| R7 | Changing the approval matrix (tiers, entities, escalation to Chairman/board) needs only data changes in `ApprovalPolicy`/`ApprovalStep`, no code. | Admin/API for policies; test that adds a tier and routes correctly |
| R8 | Segregation of duties: the user who created the document, or who submitted it, cannot approve any step of its request — also not as a delegate. | `approvals.services.act` checks `document_created_by` and `submitted_by` against actor and `on_behalf_of` |
| R9 | If no eligible approver remains after R8 (e.g. MP created a payment and is the only approver), the request stays pending and an alert `no_eligible_approver` goes to the MP; the system never self-approves. Exception: MP self-approval with a logged override (D10). | `act` + `submit` check; alert |
| R10 | Lifecycle: `pending` → `approved` (all steps done) / `rejected` / `returned` / `cancelled` / `expired`. `approved`, `rejected`, `cancelled`, `expired` are final. `returned` sends the document back to `draft`; resubmission creates a new request. | State machine in `approvals.services`; DB check on status |
| R11 | An approved document is locked: no edits to amount, lines, counterparty, dates or entity. Changes require the module's correction path (e.g. reversal for posted entries, or cancel + new document). | Module `save()` guard (like `JournalEntry.save`) checking approval status; test per module |
| R12 | If the document changed after submission (`document_version` hash differs), approval is refused and the request is set to `returned`. | Hash check in `act` |
| R13 | Only the submitter (or a user with `approvals.request.cancel_any`) can cancel, and only while `pending`. | `approvals.services.cancel` |
| R14 | Steps run in `sequence` order; a step completes when `min_approvals` distinct eligible users approve. One reject at any step rejects the whole request. | `act` |
| R15 | If a step is not decided within `escalate_after_hours`, it is escalated to the escalation role/user (logged as `escalate`). If there is no escalation target, the request expires after the policy timeout (OQ8). | Scheduled job every 15 min (Celery or cron, OQ10); test with frozen time |
| R16 | A delegate can act for a delegator only inside the delegation's dates, entity, document types and amount limit; actions record both `actor` and `on_behalf_of`. A delegate needs at least view access to the entity. | `act` + `Delegation` lookup |
| R17 | Reject and return require a comment. | Serializer + service validation |
| R18 | Service agents (`user_type=service_agent`) can create and submit drafts only; they can never approve, reject, cancel others' requests, or post. Their records carry `source="agent:<agent_name>"`. | Role `agent_drafter` has no act permissions; hard check in `act` on `user_type`; gateway layer |
| R19 | Every create, update, submit, approve, reject, return, cancel, escalate, delegation change, role/access change and sign-in writes an `AuditLog` row with before/after. Audit rows cannot be updated or deleted by any app user. | `access.services.audit()` called from services; DB role without UPDATE/DELETE on table; model `save` guard |
| R20 | Approval does not bypass module rules: after approval, posting still goes through `ledger.services.post_entry` (balance, period, Sharia, entity checks). Approval of a document that would fail those checks is still allowed to fail at posting and the error is shown. | Module `on_approved` calls its service; test |
| R21 | Notifications are sent in the recipient's language; in-app always, email if the user has an email, push only if a `PushDevice` exists (later wave). Failure of email/push never blocks the approval. | `approvals.notify` with queue; tests |
| R22 | All amounts on screens and in the API are `Decimal` strings with currency, never floats. | Serializer field type; test |
| R23 | All data, backups, logs and notification queues are stored in a Saudi cloud region. Email and push providers must not receive document content beyond the bilingual summary (OQ11, OQ12). | Deployment checklist; compliance-checker review |

### 4.2 Web UI foundation rules (`web`)

Enforced by front-end tests (component + Playwright, both languages) and API permission checks.

1. **Stack:** React SPA served from the Django host; talks only to the REST API; signs in through Keycloak (OIDC authorization code + PKCE).
2. **Layout:** top bar (logo, entity switcher, language switch, notifications bell, user menu); side navigation (Home, Approvals, Ledger, later modules as they ship); content area.
3. **Navigation items** are shown only if the user has the module's view permission in the selected entity.
4. **Entity switcher:** lists only entities with active `EntityAccess`; switching reloads data for that entity; a "Group" option shows only for users with access to the parent entity and lists combined approvals across their entities. The selected entity is shown on every screen and in every API call (`entity` parameter).
5. **Language switch:** toggles `en`/`ar`, saved to `User.language`; Arabic sets `<html dir="rtl" lang="ar">`, English `dir="ltr" lang="en"`.
6. **Approvals inbox (MP's main screen):** tabs Pending for me / Submitted by me / Decided / Delegated; columns: entity, document type, number, summary, amount + currency, submitted by, submitted at, age, step x of y; filters by entity, type, amount range; detail panel with document preview, attachments, history (`ApprovalAction`s), audit trail, and buttons Approve / Reject / Return (with comment). Buttons are hidden and server-rejected when R8/R16/R18 forbid.
7. **Home dashboard (simple):** cards for pending approvals (count and total by currency, oldest age), requests due to escalate in 24 h, alerts (`no_eligible_approver`, failed postings after approval, expired requests), recent decisions. Full group dashboard is a later wave.
8. **Reports and exports:** bilingual headers; user chooses language; both names shown where a report is shared (OQ15).

#### 4.2.1 RTL and accessibility rules

1. Use CSS logical properties only (`margin-inline-start`, `padding-inline-end`, `inset-inline`); no left/right in styles.
2. Icons with direction (arrows, chevrons, back) mirror in RTL; logos, charts axes and numbers do not.
3. Numbers, amounts, dates, account codes and document numbers display LTR inside RTL text (`<bdi>` / `dir="ltr"` spans). Digit style (Western `123` vs Arabic-Indic `١٢٣`) is OQ16.
4. Dates show Gregorian; Hijri display is OQ17. Time zone `Asia/Riyadh`.
5. Arabic font with full glyph coverage, self-hosted (no external font CDN, D5/D6).
6. All UI strings in translation files; no hard-coded text; missing Arabic string fails CI.
7. WCAG 2.1 AA: contrast ≥ 4.5:1, full keyboard use, visible focus, labelled form fields, screen-reader labels in both languages, status not shown by colour alone.
8. Approve/Reject buttons require a confirm step showing amount, currency and entity.
9. Responsive down to 360 px width so the MP can approve from a phone browser before the native app exists.

## 5. Accounting

This module produces no journal entries. Approving a document only changes its approval status; any posting is done by the owning module through its own service (`ledger.services.post_entry` for journal entries). Final approval triggers the owning module's post in the same transaction (D11).

| Event | Debit | Credit |
| --- | --- | --- |
| Request submitted / approved / rejected / returned / cancelled / expired | — | — |

## 6. Integrations

| System | Direction | What flows |
| --- | --- | --- |
| Keycloak (self-hosted, Saudi region) | Keycloak → ERP | OIDC sign-in, `sub`, username, email, name; MFA policy set in Keycloak (OQ13) |
| | ERP → Keycloak | None in this wave (roles and entity access are kept in the ERP, not Keycloak groups) — confirm in OQ14 |
| Email (SMTP) | ERP → user | Notification email with bilingual summary and link; no attachments |
| Push (APNs/FCM) — hook only | ERP → mobile | Later wave; `PushDevice` model and `notify` channel defined, not active |
| AI agent gateway (`/api/agents/v1/`) | Agent → ERP | Draft create + submit via the same services; never act on approvals |
| ZATCA, banks, GOSI, Mudad, SAP B1 | — | Not touched by this module |

### 6.1 Approvals REST API (`/api/v1/`)

Used by the web UI now and the mobile app later. JSON, bilingual fields returned as `{ "en": …, "ar": … }`, plus `Accept-Language` for messages. Auth: Keycloak bearer token. Every list is filtered by R1.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/me` | User, language, entities with roles and permissions |
| PATCH | `/me` | Change `language`, `default_entity` |
| GET | `/approvals/requests?scope=mine\|submitted\|all&entity=&status=&type=&amount_min=&amount_max=&cursor=` | Inbox lists (cursor pagination) |
| GET | `/approvals/requests/{id}` | Detail: summary, document snapshot, steps, actions, allowed actions for caller |
| POST | `/approvals/requests` | Submit `{document_type, object_id}` (calls module service) |
| POST | `/approvals/requests/{id}/approve` | Body `{comment?, document_version}`; idempotency key header required |
| POST | `/approvals/requests/{id}/reject` | Body `{comment, document_version}` |
| POST | `/approvals/requests/{id}/return` | Body `{comment, document_version}` |
| POST | `/approvals/requests/{id}/cancel` | Body `{comment?}` |
| GET | `/approvals/summary?entity=` | Counts/totals for dashboard and mobile badge |
| GET/POST/PATCH | `/approvals/delegations` | List/create/end delegations |
| GET/POST/PATCH | `/approvals/policies` | Manage matrix (needs `policy.manage`) |
| GET | `/notifications?unread=` · POST `/notifications/{id}/read` | In-app notifications |
| POST/DELETE | `/devices` | Register/unregister push token (hook for mobile; accepted but unused this wave) |
| GET | `/audit?entity=&object_type=&object_id=` | Audit trail (needs `audit.view`) |

Errors: `403` no permission, `409` state conflict or stale `document_version`, `422` rule failure; error body has `code`, `message_en`, `message_ar`.

## 7. Permissions

### 7.1 Permission list

| Code | Meaning |
| --- | --- |
| `core.entity.view` | See the entity in the entity switcher |
| `access.user.manage` | Create/deactivate users (global) |
| `access.entity_access.manage` | Grant/revoke roles in an entity |
| `access.audit.view` | Read audit log for the entity |
| `approvals.request.view` | See requests of the entity |
| `approvals.request.submit` | Submit own drafts for approval |
| `approvals.request.act` | Approve/reject/return when eligible by policy |
| `approvals.request.cancel_any` | Cancel others' pending requests |
| `approvals.policy.manage` | Edit approval matrix for the entity (or group-wide if held at the parent entity, OQ5) |
| `approvals.delegation.manage_own` | Create delegations for oneself |
| `approvals.delegation.manage_any` | Create delegations for others |
| `<module>.<doc>.create` / `.view` | Per module, e.g. `ledger.entry.create`, `payments.payment.view` (defined in each module spec) |
| `<module>.<doc>.post` | Post/execute after approval, per module |

### 7.2 Starting roles (per entity, via `EntityAccess`)

| Permission | managing_partner | chairman | accountant | viewer | agent_drafter |
| --- | --- | --- | --- | --- | --- |
| entity.view | Y | Y | Y | Y | Y |
| user.manage | Y | — | — | — | — |
| entity_access.manage | Y | — | — | — | — |
| audit.view | Y | Y | — | — | — |
| request.view | Y | Y | Y | Y | own only |
| request.submit | Y | — | Y | — | Y |
| request.act | Y | Y (when policy names chairman) | — | — | **never** |
| request.cancel_any | Y | — | — | — | — |
| policy.manage | Y | — | — | — | — |
| delegation.manage_own | Y | Y | — | — | — |
| delegation.manage_any | Y | — | — | — | — |
| module create | Y | — | Y | — | Y (draft only) |
| module post | Y | — | OQ4 | — | — |

The Chairman role exists from day one for escalation, but no policy routes to it until the MP configures a tier (D2). Which other roles exist at go-live (finance manager, CFO, board) is OQ5.

## 8. Acceptance tests

| # | Given | When | Then |
| --- | --- | --- | --- |
| T1 | User U has `accountant` in ALM only | U lists approval requests | Only ALM requests returned; requests of another entity return 404 by id |
| T2 | U is `viewer` in ALM, `accountant` in subsidiary S | U submits an ALM draft | Refused 403 |
| T3 | Initial data loaded | Accountant submits a payment of SAR 1,000 in ALM | Request `pending`, step 1 approver role `managing_partner`, MP gets in-app + email in his language |
| T4 | T3 pending | MP approves | Status `approved`, `ApprovalAction` + `AuditLog` rows with before/after, document locked |
| T5 | Approved payment | Anyone edits its amount | Rejected with bilingual error; no change saved |
| T6 | MP created a payment himself and is the only approver | MP submits and approves | Approved via self-approval override (D10); audit log records `self_approval_override`; appears in monthly self-approval report. Any other user doing the same is refused (R8). |
| T7 | MP delegated to D for ALM payments ≤ SAR 50,000, 1–10 Nov | D approves a SAR 20,000 payment on 5 Nov | Approved; action shows actor D, on_behalf_of MP |
| T8 | Same delegation | D approves SAR 60,000, or on 11 Nov | Refused 403 |
| T9 | D created the payment | D tries to approve it as delegate | Refused (R8) |
| T10 | MP adds a policy: payments ≥ SAR 500,000 → step 1 MP, step 2 chairman | Payment of SAR 600,000 submitted | Two steps; approved only after both; no code change |
| T11 | Step has `escalate_after_hours=48`, escalate to chairman | 49 h pass with no action | Step escalated, chairman notified, `escalate` logged |
| T12 | Pending request | Submitter edits the draft document | Approve call returns 409; request set to `returned` |
| T13 | Pending request | MP rejects without comment | 422; status unchanged |
| T14 | Pending request | MP returns with comment | Status `returned`; document back to `draft`; resubmission creates a new request |
| T15 | Agent user `agent:invoice-reader` | Agent creates and submits a bill draft | Bill and request carry `source="agent:invoice-reader"` |
| T16 | Same agent | Agent calls approve | Refused 403 regardless of roles |
| T17 | Approved manual journal entry that is unbalanced after a data change attempt | Final approval triggers posting (D11) | Posting refused by `post_entry`; whole approval rolled back; approver sees the reason; entry stays unposted and pending |
| T18 | Any audit row | App user tries update/delete | Refused |
| T19 | User language `ar` | Opens inbox | `dir="rtl"`, Arabic labels, amounts LTR with currency; switching to `en` persists after re-login |
| T20 | Two policies for same type with overlapping ranges, same entity and priority | Second saved | Refused |
| T21 | No policy matches a document type | Submit | Refused with bilingual "no approval policy" error |
| T22 | Approve sent twice with same idempotency key | Second call | Same response, one action recorded |
| T23 | Email server down | Request submitted | Request saved, in-app notification created, email retried and failure logged |
| T24 | New subsidiary added as `Entity` row with `EntityAccess` | Its accountant submits a bill | Group-wide policy applies; no code change |

## 9. Open questions

1. **Custom user model now?** `AUTH_USER_MODEL` is still Django's default; switching later is costly. Confirm a custom `access.User` before the first production migration.
2. ~~**Segregation vs "MP approves all":**~~ **Answered — D10.** if the MP creates a payment himself, who approves it — Chairman, a second partner, or a named finance person? Or is MP self-approval allowed with a logged override?
3. **Ledger plug-in:** confirm adding `source` and an approval state (`pending_approval`, `approved`) to `JournalEntry`. Which manual entries need approval — all, or above an amount? Are system-generated entries (from approved bills/payments) exempt?
4. ~~**Approve vs post:**~~ **Answered — D11.** does approval auto-post/execute (journal entry posted, payment released), or does a separate person post? Can an accountant post after MP approval?
5. **Roles at go-live:** which roles beyond MP, Chairman, accountant, viewer (e.g. finance manager, CFO, board member)? Parent-entity access: **answered — per entity (D12).**
6. **Currency for thresholds:** compare amounts in document currency, entity base currency, or SAR at which rate?
7. **Parallel approvals:** needed (e.g. two board members in parallel), or sequential only?
8. **Timeouts:** default escalation and expiry periods; does `expired` apply at all, or do requests wait indefinitely?
9. **Approve from email:** allow one-click approve links in email, or view-only links that open the app?
10. **Background jobs:** Celery + Redis, or a simpler cron/management command for escalations and notifications?
11. **Email provider:** self-hosted SMTP in Saudi region, or a provider — which one, and does it meet data residency?
12. **Push (later wave):** APNs/FCM route data outside Saudi Arabia; is a summary-only payload acceptable?
13. **MFA:** required for all users, or only for approvers? Method (TOTP, SMS, passkey)?
14. **Role source of truth:** roles and entity access in ERP only (proposed), or synced from Keycloak groups?
15. **Bilingual reports:** one language chosen per run, or both languages side by side?
16. **Digits:** Western (123) or Arabic-Indic (١٢٣) digits in Arabic mode?
17. **Hijri dates:** show Hijri alongside Gregorian anywhere?
18. **Audit retention:** how many years to keep audit and approval logs?
19. **Which Saudi cloud provider/region** and backup location?
20. **Document thresholds per type:** do purchase requests and contracts follow the same "MP approves all", or other rules?
21. **REST framework:** confirm Django REST Framework (open source) and the API package choices as allowed dependencies.
22. **Sharia review step:** should any document type (e.g. contracts, financing payments) need a Sharia reviewer step in the matrix?

## Build notes

Backend build on branch `module/approvals` (16 Oct 2026). Where the spec and D1–D14 did not decide, the most conservative choice was taken:

1. **Custom user (OQ1):** `access.User` is `AUTH_USER_MODEL`; `core` and `ledger` initial migrations were regenerated (no production DB exists). `keycloak_id` is nullable until Keycloak is connected.
2. **Sign-in (R4, D14):** Keycloak is not built. Today humans sign in with password + TOTP (django-otp). The admin is an `OTPAdminSite`; every API call needs an OTP-verified session. The seam is `settings.ERP_IDENTITY_ADAPTER` / `access.identity.IdentityAdapter`. Service users use revocable hashed bearer tokens (`manage.py issue_service_token`), never passwords.
3. **CFO role (D13):** not in the §7.2 table. Built as accountant rights plus audit read; no approve or post until a policy names the CFO and the MP grants it.
4. **Auditor / business analyst / PA (D13):** auditor is read-only (entity, requests, ledger, reports, audit); analyst has `investments`, `deals`, `reports` view only; PA (`pa_drafter`) creates drafts, uploads documents, submits, sees only own requests. Permission codes for not-yet-built modules (`investments.*`, `deals.*`, `reports.*`, `documents.*`) are placeholders.
5. **Which journal entries need approval (OQ3):** approval is the default. `create_entry` always makes a draft with `requires_approval=True` unless a module service passes `system=True` (entries from already-approved documents); service users can never pass it (D17). Reversals follow D15. New statuses: `pending_approval`, `approved`, `rejected` (rejected entries are locked and never post). `post_entry` refuses unapproved manual entries and any call by a service user. Bulk `update()`/`delete()`/`bulk_create()` on entries and lines work only on drafts and never on workflow fields; period status changes only through `close_period` / `reopen_period`.
6. **Submit = own drafts only:** a user can submit only documents they created (§7.1 "own drafts").
7. **Self-approval (D10, D16):** allowed only when the approver holds `managing_partner` in the document's entity, created *and* submitted the document, is an eligible approver of the step, no other user is eligible, and he has no active delegation covering the document (D16). With such a delegation, the delegate approves on his behalf (recorded with `on_behalf_of`, not as an override). This is the one case where a delegate may act for the document's creator (R8 otherwise applies). A covering delegation blocks self-approval even if its delegate cannot act; the request then raises `no_eligible_approver`. Recorded as `ApprovalAction.self_approval_override` and audit action `approval.self_approval_override`. Report: `approvals.services.self_approval_report` and `GET /api/v1/approvals/self-approval-report?year=&month=` (needs `access.audit.view`: MP, Chairman, auditor, CFO).
8. **Posting failure (D11):** any exception in the owning module's post rolls back the approval, its action, its audit row and the post together; afterwards an `approval.posting_failed` audit row and a `posting_failed` alert are written in their own transaction, and the API returns 422 with the ledger reasons (unexpected errors are reported by type only, not their internal text). The approve/reject/return/cancel API views opt out of `ATOMIC_REQUESTS` so this works on PostgreSQL.
9. **Changed document (R12):** compared by SHA-256 of document content (for entries: header + lines). The request is set to `returned` (committed), then 409.
10. **Currency (OQ6):** policies match only requests in the same currency; no conversion. Delegation amount limits also apply only in their own currency.
11. **Parallel steps (OQ7):** supported (equal `sequence`); a person counts once per request across all steps.
12. **Timeouts (OQ8):** no default. Escalation only when a step sets `escalate_after_hours` and a target; expiry only when the policy sets `expire_after_hours`. Expired/cancelled requests return the document to draft. Escalation targets join the eligible approvers; original approvers stay eligible.
13. **Jobs (OQ10):** management commands for cron: `run_escalations` (every 15 min) and `send_notifications` (email retry). No Celery.
14. **Email (R21, OQ11):** sent after commit through Django mail (console backend by default); failures are recorded on the notification and retried up to 3 times. Push is a hook only (`PushDevice` stored, `channels_sent.push = hook_inactive`).
15. **Policy overlap (T20):** enforced in `ApprovalPolicy.save` (same entity, type, currency, priority, overlapping amount and date ranges). A PostgreSQL exclusion constraint is left for the Postgres hardening step. Used policies cannot be edited or deleted, only retired (`is_active`, `valid_to`); their steps are frozen.
16. **Group-wide policies and global audit rows:** need the permission in a root (parent) entity. A policy's entity, document type and currency cannot change after creation (API, service and admin); create a new policy instead.
17. **`returned` is final for the request** (resubmission makes a new request), so the model treats it as final alongside the R10 list.
18. **Audit append-only (R19):** enforced in the model and queryset. The database-role restriction (no UPDATE/DELETE grant) is a deployment step. Audited: grants, revokes, user create/update/privilege change/password change/deactivate, role and permission edits, profile changes, approvals, delegations, policies, ledger create/post/reverse, period close/reopen, sign-in, failed sign-in, sign-out, service token issue/revoke, TOTP enrolment, push-device register/unregister, access-review report runs, every admin add/change/delete.
19. **Testing other modules:** a test-only app `testdocs` (sample payment and bill) is installed only by `config.settings_test` to test the plug-in contract until payables/payments exist.

20. **Review fixes, 17 Oct 2026:**
    - **D15 reversals:** `reverse_entry` needs `ledger.entry.reverse` (new; accountant and MP) or `ledger.entry.post` in the entry's entity and a mandatory reason; the audit row carries the reason. `system=True` exists for module services. The CFO does not get reverse rights (D15 names the accountant only).
    - **D17:** every non-human user (agent or integration) loses act, post, reverse, cancel-any, policy, delegation, user/access management, period/account management and audit-security rights, whatever its roles.
    - **Admin:** querysets are filtered by EntityAccess and ERP permissions, not Django model permissions. Superusers bypass scoping only while `ERP_ADMIN_SETUP_MODE=1` (initial setup). Bulk "delete selected" is removed. Entities are setup-only; accounts need `ledger.account.manage`; periods can be added (`ledger.period.manage`) and closed through the service but never edited or reopened in the admin. `reopen_period` exists with `ledger.period.reopen`, which no role holds (default deny). Roles are edited only by holders of `access.user.manage` in a root entity; `is_superuser` is editable only in setup mode. Django groups and the TOTP device admin are removed; enrolment goes through `setup_totp` (audited).
    - **Security settings:** DEBUG is off unless `DJANGO_DEBUG=1`; startup fails without `DJANGO_SECRET_KEY` outside DEBUG (local runs: `export DJANGO_DEBUG=1`). Secure, HttpOnly, SameSite cookies, HTTPS redirect and HSTS outside DEBUG. HSTS preload is left off on purpose (hard to undo; needs a decision). 30-minute idle session timeout. Sign-in lockout after 5 failures per username or 20 per IP in 15 minutes, counted from the audit log (custom backend, no new dependency); TOTP codes are also throttled per device by django-otp. Client IP honours `X-Forwarded-For` only from `ERP_TRUSTED_PROXIES`. SMTP outside DEBUG (host from environment).
    - **Compliance:** service tokens must have an expiry (existing tokens without one are expired by migration). There is a quarterly access review (`access.services.access_review_report`, `GET /api/v1/access/review`, `manage.py access_review --as <user>`). IP address and user agent in `/audit` and the admin need the new `access.audit.view_security` permission (MP only by default; not given to the auditor or Chairman until decided). A push-device token owned by another user is refused (409).
21. **Re-review fixes, 17 Oct 2026:**
    - **Draft editors (A):** only the creator may edit a manual draft in the admin. Every create/edit is recorded in `ledger.JournalEntryEditor` and copied to `ApprovalRequest.document_editors` at submission. Editors count as creators for R8. An MP who edited a draft and is the sole approver can approve only as a logged D10 self-approval override.
    - **User management (B):** privilege, active-status, password, token, TOTP and unlock changes to a user need `access.user.manage` in every entity where that user holds a grant. Nobody changes their own privileges. `user_type`, `agent_name` and `is_superuser` change only in setup mode; superusers are managed only in setup mode.
    - **System entries (C):** `create_entry(system=True)` and `reverse_entry(system=True)` are refused for any human user. Allowed contexts are no user, or the reserved ERP `system` user (created by migration so the name cannot be taken). Module services that post on behalf of an approver therefore record no human poster; the approval record holds the human.
    - **Bulk changes (D):** targeted rows are locked (`select_for_update`) and the change applies only to rows that are still drafts, inside one transaction; any mismatch refuses the whole call.
    - **Lockout (E):** attempts refused while locked are logged as `access.sign_in_locked` and do not extend the lock. `manage.py unlock_user` and an admin action (needs user-management rights over that user) write `access.unlock`. Unlock clears the per-username count only; the per-IP count expires with its window.
    - **Line accounts (F):** admin line account choices are limited to the entry's entity, and the formset refuses lines from another entity.
    - Deployment items are in `docs/deployment-checklist.md`.
22. **Follow-ups, 17 Oct 2026 (branch `fix/approvals-followups`):**
    - **D19:** `access.services.can_manage_user` is the single check used by deactivate, unlock, TOTP enrolment, token issue/revoke, grant/revoke of roles, and the admin user form, password change and unlock action. For a target holding `managing_partner` in any entity (any non-revoked grant), the actor must hold an active `chairman` grant in every entity where the target is MP. Other MPs and `access.user.manage` holders cannot act, and nobody acts on themselves. Setup mode (`ERP_ADMIN_SETUP_MODE` superuser) is excepted through the admin; operator commands with no acting user (`grant_access`, `unlock_user`, `setup_totp`, `issue_service_token`) also bypass it, as server-side bootstrap. Editing a role held by any MP (including `managing_partner` itself) needs the Chairman role in addition to the normal role-edit right, or setup mode. Successful actions are audited as before. Refused attempts raise `mp_protected` and are not written to the audit log, because the refusal rolls back. The API has no endpoint that changes another user (only `/me`, limited to language and default entity).
    - **System user (2a):** the reserved `system` user cannot be renamed, changed, given a password, roles, tokens or TOTP, unlocked or deactivated, even in setup mode. No other account can take the name.
    - **Append-only editors (2b):** `ledger.JournalEntryEditor` and the new `approvals.RequestEditor` (explicit through table of `document_editors`, mapped onto the existing table by a state-only migration) refuse update and delete. Editor rows of a deleted draft go with it by database cascade.
23. **Retention (D18):** there is no purge or anonymisation code. Accounting records, approval records, notifications and audit logs are kept forever. **PDPL storage-limitation review is pending legal sign-off;** nothing is anonymised until then.

Left for later: Keycloak adapter; React web UI (§4.2, T19 front-end part); PostgreSQL triggers, exclusion constraint and DB role grants; mobile push sending; audit retention (OQ18); audit for admin role edits.
