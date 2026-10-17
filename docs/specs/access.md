# Access: users, roles and permissions per entity (user × entity × role)

Django app: `core` (package `core/access/`: `models.py`, `services.py`, `catalogue.py`, `backends.py`). Status: **draft spec**. Written 2026-10-16 by the spec-writer agent. Revised 2026-10-16 (A11, A12: billing versions/corrections, unseal delegation). Amended 2026-10-16 (A13, R27, R30 to R33, AT-29 to AT-32: build decisions and reviewer hardening). Amended 2026-10-16 (A14, R23, R30, R32, R34 to R42, AT-33 to AT-40, OQ-19, OQ-20: ledger document registry, entity lifecycle, clearance approver; code on branch `claude/funny-davinci-7yd88s`, commits aad0126..b690c12). Amended 2026-10-16 (A15, 3.6 flag I, 3.7, R34, R35, R37 to R39, R43, R44, AT-33, AT-37, AT-41 to AT-46, OQ-21, OQ-22: registry creator and line hooks, reversal integrity, explicit reactivation, recovery command scope, SQLite limitation; commits b6ed6e1..497cb09). Amended 2026-10-17 (project-owner decisions A16 to A20: receipts posted by their recorder, `chairman` role and two-step Sharia switch-off, inactive subsidiaries, `zatca_admin` as add-on role, hierarchical ZATCA keys; 3.1, 3.6, R17, R19, R35, R38, R46 to R51, AT-28, AT-37, AT-49 to AT-55, OQ-18, OQ-19, OQ-21, OQ-23 to OQ-27). Amended 2026-10-17 (project-owner HR/payroll decisions A21, A22: roles `hr_officer` and `payroll_officer`, HR officer approves payroll, MP approves loans and bai' ajel, overtime approval routing; 3.1, 3.6, R52 to R54, AT-55, AT-56, OQ-2, OQ-28; detail in hr.md H9 to H16 and payroll.md P8 to P15). Amended 2026-10-17 (A23, project owner / CFO decision pack: receipts posted by their recorder only without a bank charge, records kept indefinitely with human "burn" decisions, new permissions from the pack, legacy system Falcon; R46 note, new R55, R56, §6, AT-57, AT-58, OQ-10, new OQ-29).
Depended on by: `docs/specs/invoicing.md` (OQ-25, OQ-42), `docs/specs/billing.md` §7, `docs/specs/zatca.md` §7, R-41, OQ-14, OQ-25, `docs/specs/ledger-fx.md` §7. Gateway design: `docs/ai-agent-gateway.md`.
Role names and permission codenames in this spec replace the provisional names in those four specs (reconciliation table in §3.1).

## 1. Purpose

The access module decides who may do what, in which entity. It holds the role catalogue, each user's memberships (user × entity × role, time-bounded, granted by a named person), CFO delegations, dual-control requests (four-eyes approval) and an append-only access log. It gives every module one way to check rights: `core.access.require(...)` in services and `core.access.scope(...)` for entity-filtered querysets. It covers human users, ERP system users (billing, ZATCA queue, FX import) and AI agent users who use the gateway. It works the same for Almiras, every subsidiary, every partly-owned or minority entity, and any entity added later as an `Entity` row. A right in one entity never gives a right in another. Role names, permission labels and error messages are in English and Arabic. No role can override Sharia, period, balance or entity checks in the ledger: `require` adds to those checks and never replaces them. It posts no journal entries.

## 2. Decisions

| # | Decision | Who | Date |
|---|---|---|---|
| A1 | Accounting staff create documents; the CFO approves; posting is automatic on approval. Roles are assigned per entity. | Project owner (invoicing D17, billing D5) | 2026-10-16 |
| A2 | Some invoices are generated automatically from contracts by a system user (`source="system"`). | Project owner (invoicing D17, billing D1) | 2026-10-16 |
| A3 | The Managing Partner holds the ZATCA master key and delegates key custody to named custodians, per entity, logged and revocable. | Project owner (zatca D-3, R-41) | 2026-10-16 |
| A4 | Subsidiaries are separate companies; some are partly owned; minority holdings are separate taxpayers. | Project owner (invoicing D11) | 2026-10-16 |
| A5 | Every query in views and APIs is filtered by entity and user permission. New subsidiaries are data, not code. All user-facing text in English and Arabic. | CLAUDE.md | in repo |
| A6 | AI agents act as named service users through the gateway only, create drafts only, and every action is logged. | CLAUDE.md, `docs/ai-agent-gateway.md`, invoicing D8 | in repo / 2026-10-16 |
| A7 | Agent users can never approve, post or issue. | Project owner (task brief) | 2026-10-16 |
| A8 | Creator ≠ approver is on by default, switchable per entity per module. | Project owner (invoicing D19 / OQ-5) | 2026-10-16 |
| A9 | Switching a ZATCA EGS unit to production needs a second approver. | Project owner (zatca D-9 approving OQ-14 default; task brief) | 2026-10-16 |
| A10 | Grants and revocations of access are logged. | Project owner (task brief), zatca R-41 | 2026-10-16 |
| A11 | Contracts are amended, terminated or cancelled (also retroactively) through CFO-approved versions; correction credit/debit notes are drafted by the billing system user, reviewed and submitted by a person, and approved by the CFO. Auto-generated invoices from a CFO-approved contract need no per-invoice approval. | Project owner (billing D8, D9; invoicing D20, D21) | 2026-10-16 |
| A12 | The ZATCA master key stays with the Managing Partner; no Shamir shares. The MP may delegate key access, including unsealing the secret store, to named custodians. *Unseal part withdrawn 2026-10-17 — see A20 (hierarchical keys).* | Project owner (zatca D-10) | 2026-10-16 |
| A13 | *Added 2026-10-16.* Hardening adopted during the build from code-reviewer findings: (a) posting grants are tied to one document and are single-use; (b) a grant is verified against its `AccessLog` row before use; (c) sensitive entity settings (`sharia_enforced`, `consolidation`, `parent`, `base_currency`, `is_active`) change only through logged, permission-checked services, never Django admin; (d) `AccessLog` and `Membership` are append-only, also at database level on PostgreSQL; (e) `permission_denied` rows are written durably outside the caller's transaction (R27). | Build decision (code-reviewer, caller brief); human approval at merge | 2026-10-16 |
| A14 | *Added 2026-10-16.* Further hardening adopted during the build: (a) sub-ledger postings go through a ledger **document registry**; a document books once (R34 to R36); (b) `invoicing.reverse_document` only reverses (R37); (c) entity lifecycle: catalogue flag **I**, the `reactivate_entity` recovery command, the top-level group parent cannot be deactivated, new entities only inside an existing group (R38 to R40); (d) revocation time is the current time, within 10 s (R41); (e) the `posted_by` of a posting on ZATCA clearance is a current, used approval (R42). | Build decision (code-reviewer, caller brief); human approval at merge | 2026-10-16 |
| A15 | *Added 2026-10-16.* Hardening from the verification review (commits b6ed6e1..497cb09): (a) a registered document type has a non-null creator FK to the user model, and posting refuses a document with no recorded creator; (b) a registered type names a line hook (`build_lines` or `validate_lines`); the caller states this is becoming mandatory now (R34); (c) only the model's own app registers it, and the registry is frozen once apps are ready; (d) reversal links are written only by the ledger, reversal lines mirror the original, the original must be posted and of the same entity and document; PostgreSQL triggers keep links unchanged (R43); (e) `require()` lets an **I** permission through on an inactive entity only with `reactivation=True` (R38); (f) `reactivate_entity` only acts where nobody can reactivate through the service, and logs the OS user from the process uid (R39); (g) known limitation on SQLite (R44). | Build decision (code-reviewer, caller brief); human approval at merge | 2026-10-16 |
| A16 | Receipts: "a second person is NOT required": the person who records a customer receipt may post it. This is an exception to creator ≠ poster **for receipts only**; segregation stays for every other document (resolves OQ-19 for receipts; invoicing OQ-49). | Project owner | 2026-10-17 |
| A17 | Turning Sharia enforcement **off** needs approval by the Managing Partner and then by the Chairman of Almiras; this applies to all of Almiras's owned companies and subsidiaries. A `chairman` group-level role (human, held by one person) is added. Turning Sharia **on** stays Managing Partner only. Every step is logged. (Answers the dual-control part of OQ-18.) | Project owner | 2026-10-17 |
| A18 | Inactive subsidiaries: the Managing Partner can still change their settings (resolves OQ-21; AT-37 as built). | Project owner | 2026-10-17 |
| A19 | ZATCA admin rights can be "activated" and added to any basic user, such as an accountant or the CFO: `zatca_admin` is an add-on role, granted as a second membership next to the user's main role, by the MP (or an `access_admin` where the catalogue allows it, OQ-2). Resubmitting to ZATCA (invoicing OQ-57) belongs to `zatca_admin` holders and the ZATCA queue system user. | Project owner | 2026-10-17 |
| A20 | ZATCA keys are hierarchical: one **group super key** for the whole group, held by the Almiras Managing Partner; under it, one **master key per company**. The super key protects the per-entity master keys; each per-entity master key protects that entity's EGS signing keys. The MP may delegate custody of a per-entity master key. Supersedes the single-store unseal model of A12 (zatca D-12; resolves zatca OQ-32). | Project owner | 2026-10-17 |

| A21 | Roles `hr_officer` and `payroll_officer` exist (entity, human), granted by the Managing Partner. "The HR officer approves the payroll (not the CFO)": the preparer (payroll officer) cannot approve; HR officer approves; posting automatic on approval. | Project owner (hr H15, payroll P14) | 2026-10-17 |
| A22 | Employee loans and bai' ajel financing are all approved by the Managing Partner. Overtime is approved by the Managing Partner in Almiras and by the department head in other companies. | Project owner (payroll P11, P12) | 2026-10-17 |
| A23 | **Project owner / CFO decision pack** (`docs/approvals/owner-cfo-decision-pack-2026-10-17-answered.docx`). (a) A9: a clerk may post their own receipt only when it has **no bank charge**; small charges (e.g. up to SAR 50) are not allowed: "No bank charge allowed" (R46 note). (b) Tax 12 + legal E7: records are kept indefinitely; personal data over 10 years is "nominated for burning", decided by a person, never automatically (R55; invoicing R108, hr R67). (c) The pack's other decisions add approvals that use the existing **A** segregation: Art. 51 ratios and true-ups, deferred-income releases, deposit applications, employee cost items and maternity clearances (R56). (d) A10: Almiras's legacy system is Falcon, not Daftra (§6). | Project owner (Part A), CFO / tax adviser (Part D), legal adviser (Part E) | 2026-10-17 |

Not decided (in §9): who approves when the CFO is absent or is the creator (OQ-1), who may grant memberships (OQ-2), which entities group-wide roles cover (OQ-3), and others.

## 3. Data model

User model: Django `auth.User` stays (`AUTH_USER_MODEL` is not swapped; OQ-9). Every model below is append-only except where stated.

### 3.1 Role catalogue (seeded data, `Role` rows)

Roles are a group-wide catalogue, so a role means the same thing in every entity. Which entity it applies to is set on the `Membership`. New roles are data. Seeded roles have `is_seeded=True`; their permission sets change only by migration.

| Code | name_en | name_ar [AR-review] | Scope | Kind | Replaces (spec: name) |
|---|---|---|---|---|---|
| `receivables_clerk` | Receivables clerk | محاسب الذمم المدينة | entity | human | invoicing: AR Clerk; zatca: Sales user (OQ-6) |
| `payables_clerk` | Payables clerk | محاسب الذمم الدائنة | entity | human | invoicing: AP Clerk |
| `accountant` | Accountant | محاسب | entity | human | invoicing/zatca/ledger-fx: Accountant; billing: Accounting staff **and** Accountant (invoicing) |
| `approver` | Approver (CFO) | المعتمد (المدير المالي) | entity | human | invoicing: CFO (approver); billing: CFO; ledger-fx: Approver |
| `viewer` | Viewer / auditor | مطّلع / مدقق | entity | human | all four: Viewer / Auditor |
| `zatca_admin` | ZATCA administrator | مسؤول الربط مع هيئة الزكاة والضريبة والجمارك | entity | human | zatca: ZATCA admin (entity) |
| `access_admin` | Access administrator | مسؤول الصلاحيات | entity | human | new (OQ-2) |
| `managing_partner` | Managing Partner | الشريك المدير | group | human | zatca D-3, ledger-fx: Managing Partner |
| `chairman` | Chairman | رئيس مجلس الإدارة | group | human | new (A17). One active holder per group parent (R47). Holds only `core.disable_sharia_enforcement` (second approval); any other right: OQ-24 |
| `group_treasury` | Group treasury | خزينة المجموعة | group | human | ledger-fx: Group treasury |
| `hr_officer` | HR officer | مسؤول الموارد البشرية | entity | human | new (A21; hr.md §7). Approves payroll runs (`payroll.approve_run`, R52). Granted by `managing_partner` only |
| `payroll_officer` | Payroll officer | مسؤول الرواتب | entity | human | new (A21; payroll.md §7). Prepares and submits runs; never approves them (R52). Granted by `managing_partner` only |
| `department_head` | Department head | رئيس القسم | entity | human | **proposed**, not decided (OQ-28; A22). Holds only `payroll.approve_overtime` (and its view) for employees of the department the user heads (hr 3.11) |
| `group_viewer` | Group / consolidation viewer | مطّلع على المجموعة (التوحيد) | group | human | new (consolidation reporting) |
| `system_billing` | Billing system user | مستخدم نظام الفوترة | entity | system | invoicing: Billing system user; billing: Billing service user |
| `system_zatca_queue` | ZATCA queue worker | مستخدم نظام طابور فاتورة | entity | system | zatca: Queue worker (system user) |
| `system_fx_import` | FX rate import user | مستخدم نظام استيراد أسعار الصرف | group | system | ledger-fx: Import service user |
| `agent_<name>` (one per agent) | e.g. Agent: invoice reader | مثال: وكيل قراءة الفواتير | entity | agent | invoicing: Agent service user; zatca: AI agent (gateway); billing/ledger-fx: Agent |

*Note 2026-10-17 (A19):* `zatca_admin` is an **add-on** role: it is granted as a second membership next to the user's main role in the same entity (e.g. `accountant` + `zatca_admin`, or `approver` + `zatca_admin`). The model already allows several memberships of different roles per user and entity (R9); whether a `zatca_admin` membership requires another active role in that entity: OQ-25.

**Not roles:**
- *Key custodian*: a `zatca.KeyCustodianDelegation` row (zatca 3.9), granted by `managing_partner`, checked by zatca R-41 on top of `zatca_admin`. *Amended 2026-10-17 (A20):* custody is of a **per-entity master key** (zatca D-12, R-50); the group super key is held by the MP and is not delegated by this model.
- *CFO delegate*: an `approver` membership with `delegated_from` set (3.3, R16).

**`Role`**

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| code | Slug(40), unique | Y | | Stable; used in fixtures and tests |
| name_en / name_ar | Char(120) | Y | Y | |
| description_en / description_ar | Text | N | Y | |
| scope | Choice `entity`, `group` | Y | | R6 |
| kind | Choice `human`, `system`, `agent` | Y | | Must match the user's `UserProfile.kind` (R11) |
| permissions | M2M `auth.Permission` | Y | | Only codenames in the catalogue (3.6); kind limits in R12, R13 |
| is_seeded | Bool | Y | | Seeded roles are read-only in admin |
| is_active | Bool | Y | | Inactive role grants nothing |

No `entity` FK: catalogue data shared by all entities (exception to "every business record has an entity", justified by A5 "subsidiaries are data").

### 3.2 `UserProfile` (OneToOne `auth.User`)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| user | OneToOne User, PROTECT | Y | | Created by signal for every user |
| kind | Choice `human`, `system`, `agent` | Y | | Set once; never changed (R10) |
| display_name_en / display_name_ar | Char(200) | Y | Y | |
| preferred_language | Choice `en`, `ar` | Y | | Language of messages |
| system_code | Choice `billing`, `zatca_queue`, `fx_import`, null | Y if system | | Unique |
| agent_name | Slug(40), null | Y if agent | | Unique; `source = "agent:<agent_name>"` |
| agent_model / agent_version | Char(100) | Y if agent | | Logged per call (gateway rule 4) |
| allow_external_ai | Bool, default False | Y if agent | | Gateway rule 6; changed only by `managing_partner`, logged |
| created_by | FK User | Y | | |

### 3.3 `Membership` (user × entity × role)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| user | FK User, PROTECT | Y | | |
| entity | FK `core.Entity`, PROTECT | Y | | For `scope=group`: the group parent entity (`consolidation=parent`), R6 |
| role | FK Role, PROTECT | Y | | |
| valid_from | DateTime (aware) | Y | | Inclusive |
| valid_to | DateTime, null | N | | Exclusive; null = open. Required when `delegated_from` is set |
| granted_by | FK User, PROTECT | Y | | Never the same as `user` (R8) |
| granted_at | DateTime | Y | | |
| reason | Char(500) | Y | | e.g. "Hired as AP clerk" |
| delegated_from | FK Membership, null, PROTECT | N | | The CFO approver membership being covered (R16) |
| revoked_at / revoked_by / revoke_reason | DateTime / FK User / Char(500) | N | | One write only; final |

Active at time *t* ⇔ `valid_from ≤ t < valid_to` (or `valid_to` null) and `revoked_at` null or `> t`, and the role, user (`is_active`) and entity are active (OQ-8 for inactive entities). Changing a role or dates = revoke + new grant. Rows are never deleted.

### 3.4 `DualControlRequest` (four-eyes approval)

| Field | Type | Req | Notes |
|---|---|---|---|
| entity | FK Entity | Y | |
| permission | Char(100) | Y | Catalogue codename flagged `dual_control` (e.g. `zatca.switch_to_production`) |
| target_type / target_id | FK ContentType / PositiveBigInt | Y | e.g. the EGS unit |
| payload_hash | Char(64) | Y | SHA-256 of the requested change; approval is valid only for this payload |
| requested_by / requested_at | FK User / DateTime | Y | |
| status | Choice `pending`, `approved`, `rejected`, `expired`, `used` | Y | |
| decided_by / decided_at / decision_note | FK User / DateTime / Char | N | `decided_by ≠ requested_by` (R19) |
| expires_at | DateTime | Y | Default requested_at + 72 h (OQ-11) |

### 3.5 `AccessLog` (append-only)

| Field | Type | Req | Notes |
|---|---|---|---|
| at | DateTime | Y | |
| actor | FK User, null | Y* | Null only for `membership_expired` reporting rows; never null on grants |
| actor_kind | Choice human/system/agent | Y | |
| event | Choice: `membership_granted`, `membership_revoked`, `delegation_granted`, `delegation_revoked`, `role_changed`, `profile_changed`, `dual_control_requested`, `dual_control_decided`, `dual_control_used`, `permission_denied`, `superuser_flag_changed`, `staff_flag_changed`, `agent_token_issued`, `agent_token_revoked`, `bootstrap` | Y | |
| entity | FK Entity, null | N | Null only for user-level events (superuser/staff flags, tokens) |
| subject_user / membership / role | FKs, null | N | |
| permission | Char(100) | N | |
| details | JSON | Y | Before/after values; never passwords, tokens, keys or OTPs |
| request_id / ip / source | Char | N | `source` = `human`, `system:<code>`, `agent:<name>` |

`save` refuses updates, `delete` refuses always; the DB role of the app has no UPDATE/DELETE on this table (OQ-10). *Amended 2026-10-16 (A13):* append-only is also enforced at database level on PostgreSQL for `AccessLog` and `Membership` (R33). `permission_denied` rows are written through a separate `access_log` database connection (R27). *Amended 2026-10-16 (A14):* events also include `entity_settings_changed` (R32, R38 to R40; actor null only for the `reactivate_entity` command, which records the OS user) and the ledger's `post_on_clearance` row (R42).

### 3.6 Permission catalogue (`core/access/catalogue.py`, code, not data)

Each codename is declared in its app's `Meta.permissions` (label in English, Arabic through Django translations) and listed in the catalogue with flags. Flags live in code so nobody can edit them in admin.

Flags: **A** = approval (creator ≠ approver, R14); **S!** = segregation that cannot be switched off; **DC** = dual control (R19); **G** = group-level only; **Ag** = an agent role may hold it; **Sys:x** = only system user *x* may hold it; **P** = the grant authorises a ledger posting (R23); **I** = still usable on an inactive entity, but only to reactivate it (R38; *added 2026-10-16, A14*). *Amended 2026-10-16 (A15):* an **I** permission passes on an inactive entity only when the caller passes `require(..., reactivation=True)`; without it, it is refused like any other non-view permission (`inactive_entity`). `reactivation=True` does nothing for permissions without **I**. Only `core.services._require_settings_right` passes it, and that caller then allows nothing but `is_active=True`.

| Codename | Flags | Holders (seed) |
|---|---|---|
| `core.view_access` | | access_admin, managing_partner, viewer (own entity) |
| `core.grant_membership` | S! | access_admin, managing_partner |
| `core.delegate_approval` | S! | approver (own approver membership only), managing_partner |
| `core.change_entity_settings` (R32, R38 to R40) | G, I | managing_partner (OQ-18 default). *Added 2026-10-16 (A14)* |
| `core.manage_users` (activate / deactivate user accounts) | G | managing_partner. *Added 2026-10-16 (A14)* |
| `core.disable_sharia_enforcement` (R48) | G, DC, S! | managing_partner (request = first approval), chairman (second approval only). *Added 2026-10-17 (A17)*. The `managing_partner` fallback of R19 does not apply |
| `invoicing.manage_customer` / `invoicing.manage_vendor` | | receivables_clerk / payables_clerk; accountant both |
| `invoicing.create_sales_document` (draft create/edit) | Ag | receivables_clerk, accountant, agent (if granted) |
| `invoicing.submit_sales_document` | | receivables_clerk, accountant |
| `invoicing.approve_sales_document` (= issue, R57) | A, P | approver |
| `invoicing.create_from_billing` (R60) | Sys:billing | system_billing |
| `invoicing.post_on_clearance` (deferred posting, zatca D-8) | Sys:zatca_queue, P | system_zatca_queue |
| `invoicing.create_bill` / `invoicing.create_vendor_payment` | Ag (bills only) | payables_clerk, accountant |
| `invoicing.submit_bill` / `invoicing.submit_vendor_payment` | | payables_clerk, accountant |
| `invoicing.approve_bill` / `invoicing.approve_vendor_payment` (R58, R59) | A, P | approver |
| `invoicing.reverse_document` | P | accountant. *Amended 2026-10-16 (A14):* reverses a document's entry only; never books one (R37) |
| `invoicing.post_receipt` (R33) | P | receivables_clerk, accountant. *Amended 2026-10-17 (A16):* the recorder of a receipt may post it (R46) |
| `invoicing.allocate` | | receivables_clerk, payables_clerk, accountant |
| `invoicing.enter_wht_data` / `invoicing.verify_wht_data` | | payables_clerk, accountant / accountant |
| `invoicing.manage_settings` | | approver |
| `invoicing.post_purification_transfer` / `invoicing.approve_purification_payout` (R54) | P / A, P | accountant, approver / approver |
| `invoicing.view` | Ag (scoped) | all human entity roles |
| `invoicing.view_personal_data` (R51) | | receivables_clerk, payables_clerk, accountant, approver (every view logged) |
| `billing.edit_contract_draft` | Ag | accountant, approver, agent (if granted) |
| `billing.record_contract_event` (termination, secondment end) | | accountant, approver |
| `billing.approve_contract` / `billing.approve_termination` | A | approver |
| `billing.start_run` | | accountant, approver, system_billing |
| `billing.release_period` | | accountant, approver |
| `billing.issue_from_contract` | Sys:billing | system_billing |
| `billing.create_credit_note_draft` | | accountant, approver |
| `billing.edit_version_draft` (amendment, termination, cancellation; billing R39, R40) | | accountant, approver (not Ag: billing OQ-28 default). *Added 2026-10-16 (A11)* |
| `billing.approve_version` (billing R39, R40) | A | approver. Termination and cancellation versions also accept `billing.approve_termination`. *Added 2026-10-16 (A11)* |
| `billing.review_correction` (complete, submit or discard a correction draft; billing R44, R48) | | accountant, receivables_clerk. *Added 2026-10-16 (A11)* |
| `billing.recompute_corrections` | | accountant, approver. *Added 2026-10-16 (A11)* |
| `invoicing.create_adjustment_from_billing` (draft CN/DN only; invoicing R73) | Sys:billing | system_billing. *Added 2026-10-16 (A11)*. Issuing the note is `invoicing.approve_sales_document` (A, P): approver, not the reviewer (R14) |
| `billing.view` / `billing.view_employee_ref` | Ag (view only) / | accountant, approver, viewer / accountant, approver |
| `zatca.manage_profile` (profile, EGS unit) | | zatca_admin |
| `zatca.onboard` (CSR, OTP, renew, revoke, restore; plus R-41 delegation) | S! | zatca_admin |
| `zatca.switch_to_production` | DC | zatca_admin (request), zatca_admin or managing_partner (second approver) |
| `zatca.process_queue` | Sys:zatca_queue | system_zatca_queue |
| `zatca.retry_failed` | | zatca_admin, accountant. *Amended 2026-10-17 (as built; invoicing R79, OQ-57 interim default):* for `invoicing.services.resubmit_to_zatca` the service also requires the grant to come through a `zatca_admin` membership in that entity; an `accountant` holding it is refused there (`no_membership`). Not **Ag**: agents never. The queue system user resubmits with `zatca.process_queue`. Whether accountants keep the permission for other zatca retries: zatca.md. *Confirmed 2026-10-17 (A19; resolves invoicing OQ-57):* resubmit belongs to `zatca_admin` holders and the ZATCA queue system user; an accountant or CFO who needs it is given the `zatca_admin` add-on membership. Whether `accountant` keeps `zatca.retry_failed` at all: OQ-27 |
| `zatca.view` / `zatca.view_status` | — / Ag | zatca_admin, accountant, viewer / agent (if granted) |
| `zatca.export_archive` | | zatca_admin, accountant, viewer (OQ-7) |
| `zatca.close_late_notice` | | zatca_admin |
| `zatca.grant_key_delegation` | G, S! | managing_partner. *Amended 2026-10-17 (A20):* delegates custody of a per-entity master key (zatca R-50); the `unseal` scope is withdrawn |
| `ledger.create_entry` (draft) | Ag | accountant, agent (table rates only, ledger-fx) |
| `ledger.post_entry` (manual journals) | A, P | approver; accountant pending OQ-5 |
| `ledger.reverse_entry` (manual journals) | P | accountant (OQ-5) |
| `ledger.close_period` | | approver (OQ-12) |
| `ledger.maintain_global_rate` | G | group_treasury |
| `ledger.import_global_rate` | G, Sys:fx_import | system_fx_import |
| `ledger.maintain_entity_rate` / `ledger.use_manual_rate` / `ledger.revalue` | | accountant (+ group_treasury for entity rates) / accountant / accountant |
| `ledger.view` / `ledger.view_rates` / `ledger.view_translation` | Ag (scoped) | all human roles, incl. group_viewer |
| `ledger.change_functional_currency` | G | managing_partner (before first posting) |

*Added 2026-10-17 (A21, A22).* HR and payroll codenames are listed in hr.md §7 and payroll.md §7; the ones carrying decided approval rights:

| Codename | Flags | Holders (seed) |
|---|---|---|
| `payroll.prepare_run` / `payroll.submit_run` | | payroll_officer |
| `payroll.approve_run` (R52) | A, P | hr_officer (not `approver`) |
| `payroll.approve_loan` (R53) | A, P | managing_partner only |
| `payroll.approve_deferred_sale_terms` / `payroll.approve_deferred_sale` (R53) | A / A, P | managing_partner only |
| `payroll.approve_overtime` (R54) | A | managing_partner; proposed `department_head` (OQ-28) |
| `hr.set_overtime_route` | | managing_partner |

`managing_partner` also holds every **A** permission as fallback approver in the entities it covers (invoicing OQ-42 default; this spec's OQ-1).

### 3.7 Service API (`core/access/services.py`)

```python
require(user, entity, permission, *, document=None, allow_self_approval=False, reactivation=False) -> Grant   # raises AccessDenied; reactivation: R38 (amended 2026-10-16, A15)
has(user, entity, permission) -> bool
entities_for(user, permission) -> QuerySet[Entity]
scope(queryset, user, permission, entity_field="entity") -> QuerySet
require_dual_control(user, entity, permission, request: DualControlRequest, payload) -> Grant
system_user(code) -> User
grant_membership(actor, user, entity, role, valid_from, valid_to=None, reason="", delegated_from=None) -> Membership
revoke_membership(actor, membership, reason) -> Membership
delegate_approval(actor, delegate, entity, valid_from, valid_to, reason) -> Membership
```

*Added 2026-10-16 (A14), entity services in `core/services.py`:* `change_entity_settings(entity, *, user, reason, **changes)`, `create_entity(entity, *, user, reason)`, `reactivate_entity_by_operator(entity, reason, os_user)` (called only by the `reactivate_entity` management command). Ledger side: `ledger.documents.register(model, *, post_permissions, reverse_permissions=(), entity_field="entity", changed_at_field="modified_at")`, called in the owning app's `AppConfig.ready()`. *Amended 2026-10-16 (A15):* `register(model, *, post_permissions, reverse_permissions=(), entity_field="entity", changed_at_field="modified_at", creator_field="created_by", build_lines=None, validate_lines=None)`; hooks `build_lines(document) -> list[line dict]` and `validate_lines(document, lines) -> list[problem "English / العربية"]` (R34, R35).

`Grant` is a frozen dataclass: `user, entity, permission, membership_ids, checked_at, log_id`. `AccessDenied` subclasses `django.core.exceptions.PermissionDenied` and carries `message_en` / `message_ar`. *Amended 2026-10-16 (A13):* a grant for a **P** permission also records the document it was issued for and can be used once only (R30, R31).

## 4. Rules

Enforcement: **S** = `core/access/services.py`; **M** = model `save`/`delete`/`clean`; **DB** = constraint; **Q** = queryset helper; **T** = test that scans code or data; **C** = Django system check at start-up.

**Checking rights**
1. Every service that creates, changes, approves, posts, issues, exports or shows business data calls `require(user, entity, permission)` first, for the entity that owns the record. Views and APIs call services and never check roles themselves. **[S; T: test lists every public function in `*/services.py` without `require` against an allow-list]**
2. Every view, API and admin queryset on a model with an `entity` FK goes through `scope(qs, user, permission)`, which filters to `entities_for(user, permission)`. A record in an entity outside that set is answered as "not found" (404), never 403, so its existence is not revealed. **[Q; T: test calls every registered API list/detail URL as a user of another entity]**
3. `require` grants a permission only if the user has an active membership (3.3) at check time, in that entity (or a group membership covering it, R6), whose role holds the permission. Nothing else gives rights: not `is_superuser`, `is_staff`, Django groups or `user_permissions`. **[S; C: non-superuser users with Django groups or user_permissions fail the check]**
4. Rights are checked again at every action. Nothing is cached across requests; a revocation or expiry takes effect at the next check. **[S]**
5. A right in entity A gives nothing in entity B, also when B is A's parent or child. **[S; test AT-1, AT-2]**
6. A `scope=group` role may be granted only on an entity with `consolidation=parent`. It covers that entity and its descendants with `consolidation=full` (recursive). Entities with `equity` or `none` consolidation (associates, minority holdings) need an explicit per-entity membership (OQ-3). Group-level records without an entity (ledger-fx global rates) are checked against the group parent entity. **[M clean + S; test AT-3]**
7. `require` never relaxes another module rule. No permission exists that bypasses Sharia, closed-period, balance, intercompany or entity checks in `ledger.services`. **[T: catalogue scan; test AT-24]**

**Memberships**
8. Only a user with `core.grant_membership` in the entity grants or revokes memberships there; nobody grants to or revokes from themselves. **[S; test AT-15]**
9. Memberships are never deleted or edited. The only change allowed is a single revocation write. No two active, overlapping memberships for the same (user, entity, role). **[M; S; test AT-16]**
10. `UserProfile.kind` is set at creation and never changes. **[M]**
11. A membership's role kind must equal the user's kind (human roles to humans, system roles to the matching `system_code`, agent roles to agent users). **[M clean + S; test AT-12]**
12. An agent role may hold only permissions flagged **Ag**. Every **A**, **P**, **DC**, **S!**, **G** and **Sys** permission is refused for agent roles at save time, and `require` refuses them again for agent users whatever the role says (A7). **[M + S; test AT-9, AT-10]**
13. A **Sys:x** permission may only sit on the role of system user *x*, and system users hold only their own **Sys** permissions plus the plain permissions listed for them in 3.6. `system_billing` cannot approve, post manual journals, change ZATCA or see another module's data beyond what `create_from_billing` reads. **[M + S; test AT-11]**

**Segregation of duties**
14. For **A** permissions, `require(..., document=doc)` refuses if the user is `doc.created_by` or `doc.submitted_by`. A module may pass `allow_self_approval=True` only when its per-entity switch allows it (invoicing `InvoicingSettings.segregation_required = False`); this is logged in the grant. **A** permissions called without `document` raise a programming error. **[S; test AT-6, AT-7]**
15. For **S!** permissions `allow_self_approval` is ignored: no self-grant, no self-delegation, no approval of one's own dual-control request. **[S; test AT-15, AT-19]**

**Delegation**
16. A CFO delegate is an `approver` membership with `delegated_from` = the delegator's active approver membership, the same entity, and `valid_to` required and no later than the delegator's own `valid_to`. It is created by the delegator (`core.delegate_approval`) or by `managing_partner`. Revoking or expiring the source membership ends the delegation at the same moment. A delegate cannot delegate further. **[S; test AT-17]**
17. Key custody delegation stays in `zatca.KeyCustodianDelegation` (zatca 3.9, R-41). Granting needs `zatca.grant_key_delegation` (managing_partner only); the custodian must hold an active `zatca_admin` membership in that entity; grant and revoke also write `delegation_granted` / `delegation_revoked` in `AccessLog`. ZATCA key actions need **both** `require(user, entity, "zatca.onboard")` and an active delegation. Exception (added 2026-10-16, A12; zatca R-49): an `unseal` delegation is recorded on the group parent entity, because unsealing opens the store for all entities; the custodian must hold `zatca_admin` on the group parent (this rule applied to the parent), and the ERP performs no unseal action, it only records who may do it (zatca OQ-32). **[S in zatca + core; test AT-18, AT-28]**
    *Amended 2026-10-17 (A20):* the `unseal` exception is **withdrawn — see R51** (hierarchical keys, zatca R-50). Every key custody delegation is per entity again; there is no store-wide delegation on the group parent.
18. When the user who should approve is absent, the document waits; the system never routes approval to another person on its own. **[S]**

**Dual control**
19. A **DC** action runs only with a `DualControlRequest` in status `approved`, not expired, for the same entity, permission, target and `payload_hash`, decided by a different user who holds the permission (or `managing_partner`). Using it sets `used`; it is single-use. **[S; test AT-19, AT-20]**
    *Amended 2026-10-17 (A17):* a DC permission may name which role requests and which role decides. For `core.disable_sharia_enforcement` the requester must hold it through `managing_partner` and the decider through `chairman`; the "or `managing_partner`" fallback does not apply (R48).

**Service users**
20. System users are created by data migration, one per `system_code`, with an unusable password; they cannot log in through any view, API or the gateway. Code obtains them only through `system_user(code)`. Each acts only in entities where it has a membership; adding an entity to billing means adding a `system_billing` membership (setup data; CLAUDE.md "new subsidiaries are data"). **[M + S; test AT-11, AT-13]**
21. Agent users authenticate only by gateway tokens (`/api/agents/v1/`). Tokens are stored hashed, rotate, and are revoked instantly; issue and revocation are logged. Agent users cannot use the web UI, Django admin or session login. Records they create carry `source="agent:<agent_name>"`. **[S + gateway; test AT-14]**
22. Work started by a human that runs as a system user (e.g. a manual billing run) logs both the human (`initiated_by`) and the system user. **[S]**

**Ledger posting**
23. `ledger.services.post_entry` and `reverse_entry` require a `Grant` for a **P** permission in the entry's entity, or the user's own `ledger.post_entry` / `ledger.reverse_entry` right. A grant for another entity is refused. For deferred posting (sales posted on ZATCA clearance, zatca D-8), `system_zatca_queue` posts with `invoicing.post_on_clearance`; `posted_by` is the approver recorded on the document. **[S in ledger; test AT-21]**
    *Amended 2026-10-16 (A14):* a sub-ledger document is booked by `create_entry(..., post=True, grant=..., document=...)`, where the grant is a sub-ledger **P** permission issued for that document and registered for its type (R34). `post_entry` takes only `ledger.post_entry` grants for manual drafts; a sub-ledger grant passed to it is refused. Which `posted_by` is accepted on clearance: R42.

**Superusers and staff**
24. `is_superuser` gives no business permission in `require` or `scope`. Superusers use Django admin for technical tasks; admin for business models subclasses `core.admin.EntityScopedAdmin`, which applies `scope` and blocks status changes. **[S + admin; test AT-22]**
25. System and agent users can never be superuser or staff. Changes to `is_superuser` / `is_staff` are logged. **[M + signal; test AT-22]**
26. Bootstrap: the management command `access_bootstrap` grants the first `managing_partner` membership only while no active `managing_partner` membership exists; it logs a `bootstrap` event with the operator's OS user. **[S; test AT-23]**

**Logging, language, privacy**
27. Every grant, revocation, delegation, dual-control step, profile change and denied `require` writes one `AccessLog` row. **[S + M; test AT-15, AT-16, AT-29]**
    *Amended 2026-10-16 (A13):* grant, membership, delegation, dual-control, profile and all other rows are written in the caller's transaction (they roll back with the change). `permission_denied` rows are written durably **outside** the caller's transaction, through a separate `access_log` database connection, so a refusal survives a rollback of the caller's work. Fallback: if that write cannot happen (the actor or entity row is not yet committed and so not visible to the other connection, or a lock wait exceeds 2 s), the row is written in the caller's transaction instead. **Known limitation:** a refusal written by the fallback is lost if the caller's transaction rolls back.
28. `AccessDenied` messages, role names and permission labels exist in English and Arabic; the user's `preferred_language` is used. **[T: catalogue and role fixture scan; test AT-25]**
29. Personal data fields (R51 in invoicing, `employee_ref` in billing) are masked unless the user holds the matching `view_personal_data` / `view_employee_ref` permission; each unmasked view is logged (PDPL). **[S in modules using `has`]**

**Hardening (added 2026-10-16, A13)**
30. A grant for a **P** permission is tied to one document: `post_entry` / `reverse_entry` refuse it for any other document (or entry derived from another document). It is single-use: a second use is refused. **[S in core + ledger; test AT-30]**
    *Amended 2026-10-16 (A14):* "the document" is the stored row of a registered document type (R34, R35); the entry records it (`document_type`, `document_id`), and its reversal carries the same reference.
31. Before a grant is honoured, it is verified against its `AccessLog` row (`log_id`): the row must exist and match user, entity, permission and document. A forged or altered `Grant` is refused. **[S; test AT-30]**
32. `Entity.sharia_enforced`, `consolidation`, `parent`, `base_currency` and `is_active` change only through services in `core` that call `require` and write an `AccessLog` row with before/after values. Django admin shows these fields read-only. Permission codename and holders: OQ-18. **[S + admin; test AT-31]**
    *Amended 2026-10-16 (A14):* the codename is `core.change_entity_settings` (flags G, I), checked on the entity's group parent; moving an entity to another group needs it on that group too. `base_currency` changes only through `ledger.services.set_functional_currency` (ledger-fx R5). Lifecycle rules: R38 to R40.
33. `AccessLog` and `Membership` are append-only: model guards (R9, 3.5) and, on PostgreSQL, a database-level guard that rejects UPDATE (except the single revocation write on `Membership`) and DELETE. **[M + DB; test AT-32 (PostgreSQL only)]**

**Ledger document registry (added 2026-10-16, A14)**
34. Each sub-ledger app registers, in its `AppConfig.ready()`, the document models that book journal entries and, per model, the **P** permissions that may post it and the permissions that may reverse it (`ledger.documents.register`). Registration is refused for non-concrete models, ledger models, models without an `entity` FK to `core.Entity`, an empty posting set, or permissions that are not sub-ledger posting / reversal permissions. The ledger registers nothing itself: until a module registers its types, every sub-ledger posting (`create_entry(post=True)` with a sub-ledger grant) is refused. **[S in ledger; test AT-33]**
    *Amended 2026-10-16 (A15):* registration is also refused (`DocumentRegistryError`) when (a) the model has no `creator_field` (default `created_by`) that is a **non-null** FK to the user model, because without a stored creator the ledger cannot keep the creator from posting (R35); (b) a hook is given that is not callable; (c) `register` is called from code outside the model's own app (only that app's modules, e.g. its `AppConfig.ready()`, may register it); (d) the registry is frozen (`apps.ready`): no registration after start-up. Re-registering a type with other settings is refused; there is no public unregister. **Line hook:** each type names a line hook, `build_lines` (the type builds the entry's lines; the caller passes `lines=None`, else `document_builds_lines`) or `validate_lines` (receives the **stored** document and the priced, unsaved lines, rounding line included, before anything is written; any problem refuses the posting, `document_lines_rejected`, en/ar). A hook is mandatory (A15 b); at 497cb09 the registry still accepts a type with neither hook and the `ledger/documents.py` docstring calls hooks optional, so the builder must add the check (AT-41 tests it). **[S in ledger; test AT-33, AT-41]**
35. The document passed with a sub-ledger posting must be a saved model instance (never a tuple or stand-in) of a registered type whose posting set lists the grant's permission, must exist in the database, and must belong to the entity being posted. Otherwise refused (`grant_needs_document`, `document_not_registered`, `document_not_found`, `document_other_entity`), logged as `permission_denied`. Segregation is checked on the **stored** document, not the object the caller passed: the grant's user must not be its `created_by`, `submitted_by` or `modified_by` (`self_approval`). The ledger applies this to every sub-ledger posting, whatever the module's segregation switch (OQ-19). **[S in ledger; test AT-33, AT-35]**
    *Amended 2026-10-16 (A15):* posting refuses a stored document whose creator reads empty (rows from before the NOT NULL rule, or a type whose creator is empty), `document_no_creator`, logged as `permission_denied`; the grant stays unused and nothing is written. The creator is always among the people who cannot post it. **[S in ledger; test AT-42]**
    *Amended 2026-10-17 (A16):* one exception, R46: customer receipts (`CustomerReceipt` with `kind = receipt`) may be posted by their creator, submitter or last modifier. Every other registered type, refunds included, keeps the rule (OQ-26).
36. One booking per document: a document has at most one non-reversal entry (DB partial unique on `(entity, document_type, document_id)` where `reversal_of` is null, plus a service check, `document_booked`). A reversed document is not booked again; the correction is a **new document** with its own entry. **[S + DB; test AT-34]**
37. `invoicing.reverse_document` (and any registered reversal permission) only reverses the entry of the document it was issued for; it never books a document's entry. **[S in ledger; test AT-36]** *Amended 2026-10-16 (A15):* what a reversal must be is R43.

**Entity lifecycle (added 2026-10-16, A14)**
38. On an inactive entity, rights are refused except view permissions (OQ-8 default) and permissions flagged **I**. An **I** permission on an inactive entity allows only reactivation (`change_entity_settings(..., is_active=True)` and nothing else); any other change is refused (`inactive_entity_reactivate_only`). **[S; test AT-37]**
    *Amended 2026-10-16 (A15):* `require()` alone does not limit what an **I** permission is used for, so on an inactive entity it refuses an **I** permission (`inactive_entity`; `has()` returns False) unless the caller passes `reactivation=True`. `reactivation=True` has no effect on permissions without **I**. The only caller that passes it is `core.services._require_settings_right`, which then refuses any change other than exactly `{is_active: True}`. Entity settings are checked on the entity's **group parent** (R32), so this limit applies when the group parent is inactive: then no setting of any entity in that group changes except reactivating the group parent. An inactive subsidiary of an active group is not limited by R38 (OQ-21). `create_entity` does not pass `reactivation=True`. **[S; test AT-37, AT-43]**
    *Confirmed 2026-10-17 (A18; resolves OQ-21):* the MP may change any setting of an inactive subsidiary while its group parent is active, as built. Turning Sharia off on it still needs R48.
39. Recovery: when nobody can reactivate an entity, an operator runs `manage.py reactivate_entity` with a reason. It sets `is_active=True` only on an inactive entity and writes an `entity_settings_changed` row (no actor; `source="command"`, OS user, reason, before/after). The top-level group parent (no `parent`) cannot be deactivated, also not by making it top-level and inactive in one change, because every group right is checked on it. **[S + command; test AT-37]**
    *Amended 2026-10-16 (A15):* the command (`core.services.reactivate_entity_by_operator`) only accepts an entity that is inactive **and** either top-level (no `parent`) or in a group whose group parent is inactive, i.e. where nobody can reactivate it through `change_entity_settings`. An inactive subsidiary of an active group is refused with a message naming the group (its managing partner reactivates it). A reason is required. The logged OS user comes from the process uid (`pwd.getpwuid(os.getuid())`), never from `USER` / `LOGNAME`. The row holds `command="reactivate_entity"`. **[S + command; test AT-37, AT-44]**
40. `core.services.create_entity` creates an entity only under a parent in an existing group: the user needs `core.change_entity_settings` on that group's parent, which must be active. It never creates a new top-level group (an operator task). It writes one `entity_settings_changed` row with the new entity's sensitive settings and `base_currency`. **[S; test AT-38]**

**Revocation time and clearance approver (added 2026-10-16, A14)**
41. A membership revocation's `revoked_at` must be within 10 seconds of the current time when written (model `save`, and the PostgreSQL trigger of R33): no backdated or future-dated revocation. **[M + DB; test AT-39]**
42. On a posting with `invoicing.post_on_clearance`, `posted_by` is required and must be a human who is not the document's creator, submitter or last modifier, and whose `invoicing.approve_sales_document` grant for this document was **used** and was issued **after the document's last change** (`modified_at`); earlier or unused approvals do not count. The approver need not still be active at clearance: the right was checked at approval (OQ-20). The posting writes an `AccessLog` row (`post_on_clearance`, document, entry, `posted_by`). **[S in ledger; test AT-40]**
    *Amended 2026-10-17 (pending fix, being built; invoicing R87):* "after the document's last change" is tested with a document version counter recorded on the approval, not by comparing `modified_at` with the grant time; an approval of an earlier version does not count, whatever the clocks say. **[S; test AT-40, invoicing AT-89]**

**Reversal integrity and database guards (added 2026-10-16, A15)**
43. Reversal and document links (`reversal_of`, `document_type`, `document_id`) are written only by the ledger: by `ledger.services._create` (document link) and `_reverse` (reversal link plus the original's document reference), before the entry's first save. They never change after insert, also not on a draft (model `save`, `QuerySet.update`, `bulk_update`, `create` and `bulk_create` refuse). Because Sharia, inactive-currency and rate-date checks are skipped for reversals, `validate_entry` refuses a reversal unless (a) the original exists and is `posted`, (b) it has the same entity, `document_type` and `document_id`, and (c) its lines mirror the original's one to one in id order: same account, debit and credit swapped, same currency, transaction amount, rate, counterparty, `rate_source`, `exchange_rate`, `settles_line` and `line_kind`. `post_entry` refuses any draft that carries a link (`draft_has_links`): reversals post in `reverse_entry`, documents in `create_entry(post=True)`. On PostgreSQL, triggers (migration `ledger/0012_entry_link_triggers`) refuse a link change on UPDATE, a reversal INSERT whose original is not posted or not of the same entity and document, and posting a reversal whose lines do not mirror the original (account, sides, currency, transaction amount, rate, counterparty). **[S in ledger + M + DB; test AT-45]**
44. **Known limitation (SQLite).** The database triggers (status transitions, links, append-only, rate lock) exist only on PostgreSQL. On SQLite, raw SQL and the base `django.db.models.QuerySet.update` (called directly, bypassing the model's queryset) can still change entry status and links; only the model and queryset guards apply. PostgreSQL triggers are the production guard; on PostgreSQL, raw SQL that forges a complete, correct reversal (or sets status with number, period, `posted_at` and `posted_by` on a balanced draft) is still accepted, and the database role's privileges are the guard there (OQ-10). CI runs the suite on PostgreSQL 16 with `REQUIRE_POSTGRES=1`: the run fails unless the database is PostgreSQL and **no test is skipped**; a second job runs on SQLite. SQLite is for local runs and tests only, never production. **[CI + `conftest.py`; test AT-46]**

**Database role (added 2026-10-17; go-live requirement, from OQ-10)**
45. The application's PostgreSQL role (the one Django connects with in production) must not be able to bypass or alter triggers or run arbitrary DDL: it is not a superuser, not the owner of the application tables, functions or triggers, has no `CREATEROLE`/`CREATEDB`/`BYPASSRLS`/`REPLICATION`, cannot `SET session_replication_role`, and cannot `ALTER TABLE ... DISABLE TRIGGER`, `DROP`/`CREATE OR REPLACE FUNCTION` or `TRUNCATE`. Migrations run under a separate owner role, used only at deployment. Reason: the ledger, access and invoicing triggers (ledger 0009/0012, invoicing `0007_document_lock_triggers`) accept raw SQL that performs an allowed transition with plausible values and do not check every referenced row (R44), so role privileges are the final guard. **[deployment check + go-live checklist; test AT-47]**

**Project-owner decisions 2026-10-17 (A16 to A20)**
46. **Receipts posted by their recorder (A16).** For `CustomerReceipt` with `kind = receipt` the ledger does not refuse the posting user for being the receipt's creator, submitter or last modifier; the user still needs an `invoicing.post_receipt` grant for that receipt, in that entity (R23, R30, R31). The exception is declared at registration (a per-type option of `ledger.documents.register`, e.g. `creator_may_post`, accepted only for this type and kind), never by a caller flag, and the grant's `AccessLog` row records `self_post=true`. Customer refunds, bills, vendor payments (including refunds received), sales documents and manual journals keep creator ≠ poster (R35; OQ-26). **[S in ledger + registry; test AT-49]**
    *Confirmed 2026-10-17 (A23(a); no code change):* the exception covers only a receipt with **no bank charge**, as built (`invoicing.services._receipt_creator_may_post`); a receipt with any bank charge needs a second person (invoicing R110, AT-106).
47. **Chairman role (A17).** `chairman` is a `scope=group`, `kind=human` role. At most one active `chairman` membership exists per group parent at any time (a second overlapping grant is refused); it is granted only by `managing_partner` (`core.grant_membership`, never to self, R8; default, OQ-24), and the chairman and the requesting MP are different users. **[M clean + S + DB partial unique; test AT-50]**
48. **Turning Sharia enforcement off (A17).** `Entity.sharia_enforced` True → False, for any entity of the group (any `consolidation`, the group parent included), runs only in two steps: (1) a `managing_partner` of the group opens a `DualControlRequest` for `core.disable_sharia_enforcement` on the group parent, target = the entity, payload = {entity, `sharia_enforced: False`, reason}; opening it is the MP's approval; (2) the group's `chairman` approves it (R19; the decider must hold the permission through `chairman`). Then the MP applies it through `change_entity_settings(entity, sharia_enforced=False, dual_control_request=...)`, which calls `require_dual_control` (single-use, same payload, not expired). The change must be alone in its call (no other setting with it). Without an approved request the change is refused (`dual_control`), also for an inactive subsidiary (R38, A18). Requested, decided (approved or rejected), used and the `entity_settings_changed` row are each logged in `AccessLog`, en/ar. `create_entity` with `sharia_enforced=False` needs the same approved request (target = the parent entity, payload names the new entity's code). **[S in core + access; test AT-51, AT-52]**
49. **Turning Sharia enforcement on** (False → True) stays a single step by `managing_partner` with `core.change_entity_settings` (R32), logged. **[S; test AT-51]**
50. **`zatca_admin` add-on (A19).** A user may hold `zatca_admin` and another entity role (e.g. `accountant`, `approver`) in the same entity as two memberships. Each right is checked on the membership that carries it: `invoicing.services.resubmit_to_zatca` accepts `zatca.retry_failed` only through a `zatca_admin` membership in that entity (invoicing R79), plus the ZATCA queue system user with `zatca.process_queue`. Segregation rules (R14, R35) apply to the person whatever roles they hold. **[S; test AT-53]**
51. **Per-entity key custody (A20).** A `KeyCustodianDelegation` always names one entity and covers only that entity's master key and EGS keys (zatca R-50). Granting needs `zatca.grant_key_delegation` (`managing_partner`) and an active `zatca_admin` membership of the custodian in that entity (R17). No delegation is recorded on the group parent for other entities; the group super key is not delegated through the ERP (zatca OQ-34). **[S in zatca + core; test AT-54]**

**Project-owner decisions 2026-10-17 (A21, A22)**
52. **Payroll approval (A21).** `hr_officer` and `payroll_officer` are seeded entity roles, granted only by `managing_partner` (an `access_admin` grant is refused). `payroll.approve_run` (A, P) sits on `hr_officer`, not on `approver`; the run's creator, submitter or last modifier is refused (R14, R35) whatever roles that person holds, so a user holding both roles cannot approve a run they prepared. Posting follows approval through the registry (R34). **[catalogue + S + ledger; test AT-55]**
53. **MP approves loans and bai' ajel (A22).** `payroll.approve_loan`, `payroll.approve_deferred_sale_terms` and `payroll.approve_deferred_sale` are held only by `managing_partner` (group role; covers `full` descendants, R6; `equity` / `none` entities need an explicit MP membership, OQ-3). Never the creator (R14). **[catalogue + S; test AT-56]**
54. **Overtime approval routing (A22).** `payroll.approve_overtime` (A) is checked with the employee's entity and `HrSettings.overtime_approval_route` (hr R55): route `managing_partner` accepts only a `managing_partner` grant; route `department_head` accepts only the user linked to the head of the employee's department, through a membership holding the permission in that entity (proposed role, OQ-28). The route is per entity data, never a code path. **[S in payroll + core; test payroll AT-28]**

**Project owner / CFO decision pack 2026-10-17 (A23; to build)**
55. **Retention decisions (A23(b)).** New permission `core.decide_retention` (flags **A**, **S!**; human only; never **Ag**), held by `managing_partner` (default, invoicing OQ-67). It decides `RetentionNomination` rows of an entity (`burn` / `keep`, bilingual reason for `keep`), never for a record the decider created or is the data subject of (R14). No service, job, agent or system user deletes or anonymises personal data without such a decision. `AccessLog` itself is kept indefinitely; its personal-data columns are nominated like any record (OQ-10). **[catalogue + S in core; test AT-57]**
56. **New approvals from the decision pack (A23(c)).** Added to the catalogue with flag **A** (creator ≠ approver, R14) and, where they post, **P** and the document registry (R34 to R36): `invoicing.approve_recovery_ratio` (approver), `invoicing.approve_art51_true_up` (A, P; approver), `invoicing.create_deferred_release` (Sys:billing) and `invoicing.approve_deferred_release` (A, P; approver), `invoicing.approve_deposit_application` (A, P; approver), `invoicing.approve_non_vat_document` (posting permission for sales documents of an entity without a VAT number, invoicing R101; approver), `payroll.approve_cost_item` and `payroll.approve_secondment_cost_report` (A; approver), `payroll.approve_maternity_clearance` (A; approver; legal contact per hr OQ-44). None is **Ag**. **[catalogue + seed migration; test AT-58]**

*Note 2026-10-17 (invoicing pending fixes):* personal-data masking (R29) also covers admin labels (invoicing R86); the FX settlement helper is removed from the public service functions scanned by R1 / AT-26 (invoicing R90).

## 5. Accounting

None. The access module posts no journal entries and touches no accounts. It only controls who may call the ledger and sub-ledger services (R23).

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| Agent gateway (`/api/agents/v1/`, MCP) | In | Token → agent user; every call checked with `require` | Gateway not built |
| OpenBao (via zatca) | Out | Nothing from `core`. zatca applies the custodian's OpenBao policy on delegation (R-41) | zatca spec |
| ZATCA, banks, GOSI, Mudad, Falcon (or other legacy system) | — | None. Later HR (GOSI/Mudad) and integrations use this same model | — |
| Directory / SSO / MFA | In | None now; Django login. MFA: OQ-4 | Open |

## 7. Permissions

Rights over access itself. Business permissions are in 3.6.

| Action | managing_partner (group) | access_admin (entity) | approver (entity) | viewer | Superuser (no membership) | System | Agent |
|---|---|---|---|---|---|---|---|
| Grant / revoke membership (not to self) | Y, covered entities | Y, own entity; not `managing_partner`, `access_admin` (OQ-2) | | | No (only `access_bootstrap`, R26) | No | No |
| Delegate approval | Y | | Y, own membership | | No | No | No |
| Grant / revoke key custodian delegation | Y | | | | No | No | No |
| Create / edit non-seeded role | Y | | | | Via migration only | No | No |
| Create agent user, issue / revoke token | Y | | | | No | No | No |
| Set `allow_external_ai` on an agent | Y | | | | No | No | No |
| Request / approve dual-control item | per 3.6 | | | | No | No | No |
| Turn Sharia enforcement off (R48) | Request (first approval) and apply after chairman approval | No | No | No | No | No | No |
| Approve Sharia switch-off, second step (R48) | No (chairman only) | No | No | No | No | No | No |
| Turn Sharia enforcement on (R49) | Y | No | No | No | No | No | No |
| Grant `chairman` membership (R47) | Y (default, OQ-24) | No | No | No | No | No | No |
| View memberships and access log | Y | Y, own entity | | Y, own entity (OQ-10) | No | No | No |

## 8. Acceptance tests

Fixtures: entities ALM (parent), SUB (child, `full`, 100%), PART (child, `full`, 60%), MIN (child, `equity`, 25%); users per role; system users `billing`, `zatca_queue`, `fx_import`; agent user `agent:invoice_reader`. All anonymised; no real names or identifiers.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | U is `accountant` in ALM only | U lists invoices and opens a SUB invoice by id | List has ALM only; SUB detail returns 404; `require(U, SUB, "invoicing.view")` raises `AccessDenied`; `permission_denied` logged (R2, R5) |
| AT-2 | U is `approver` in SUB | U approves an ALM bill | Refused; bill stays `submitted` (R5) |
| AT-3 | G is `group_viewer` on ALM | G views ALM, SUB, PART, MIN trial balances | ALM, SUB, PART allowed; MIN refused until a per-entity `viewer` membership exists (R6) |
| AT-4 | U is `accountant` in ALM, `valid_to` = yesterday | U creates a draft invoice | Refused (expired membership, R3) |
| AT-5 | U's membership `valid_from` = tomorrow; another membership revoked 1 min ago | U acts in each entity | Both refused; next request after revoke refused (R3, R4) |
| AT-6 | C (`accountant` + `approver` in ALM) created and submitted invoice X; segregation on | C approves X | Refused; X stays `submitted` (R14) |
| AT-7 | Same, ALM `InvoicingSettings.segregation_required = False` | C approves X | Allowed; grant logged with `self_approval=true` (R14) |
| AT-8 | Approver P (not creator) | P approves X | Issued; `Grant` logged (R14, R27) |
| AT-9 | `agent:invoice_reader` with role holding `invoicing.create_bill` in ALM | Agent creates a bill, then calls submit, approve and `post_entry` | Draft created with `source="agent:invoice_reader"`; submit, approve and post refused (R12) |
| AT-10 | An admin adds `invoicing.approve_bill` to an agent role | Save role | `ValidationError`; and even if forced in the DB, `require` refuses (R12) |
| AT-11 | `system_billing` member of ALM | It calls `create_from_billing` in ALM; then in SUB (no membership); then `approve_bill`, `ledger.post_entry`, `zatca.manage_profile` in ALM | First allowed; all others refused (R13, R20) |
| AT-12 | Human U | Grant U role `system_billing`; grant agent user role `accountant` | Both refused (R11) |
| AT-13 | System user `billing` | Log in via the login view and via a gateway token | Both refused (R20) |
| AT-14 | Agent token revoked | Agent calls the gateway | 401; `agent_token_revoked` exists earlier in the log (R21) |
| AT-15 | A is `access_admin` in ALM | A grants B `payables_clerk` in ALM; A grants itself `approver` in ALM; A grants B a role in SUB | First allowed with `membership_granted` row (actor A, subject B, role, entity, dates); second and third refused with `permission_denied` rows (R8, R15, R27) |
| AT-16 | B's membership exists | A revokes it with reason; then tries to edit `valid_to` or delete the row | Revoke logged as `membership_revoked`; edit and delete refused; a second overlapping grant of the same role refused (R9, R27) |
| AT-17 | CFO F is `approver` in ALM until 2027-12-31 | F delegates to D for 2027-03-01 to 2027-03-15; D approves a bill on 03-10 and on 03-16; F's membership is revoked on 03-12 and D tries again | 03-10 allowed before F's revocation; after revocation refused; 03-16 refused; delegation without `valid_to` refused; D delegating further refused (R16) |
| AT-18 | Z is `zatca_admin` in ALM with no key delegation | Z generates a CSR; the MP grants delegation; Z retries; an accountant tries to grant a delegation | First refused; after grant allowed; accountant refused; `delegation_granted` in `AccessLog` and zatca events (R17) |
| AT-19 | Z1 requests production for EGS-1 in ALM | Z1 approves own request; Z2 (`zatca_admin` ALM) approves; the switch runs twice | Self-approval refused; Z2 approval accepted; first switch succeeds and marks `used`; second refused (R15, R19) |
| AT-20 | Approved request for payload P | Switch is called with payload P′ or after `expires_at` | Refused (R19) |
| AT-21 | Grant for ALM `invoicing.approve_bill` | Pass it to `post_entry` for a SUB entry | Refused; ALM entry posts (R23) |
| AT-22 | S is superuser with no membership | S lists invoices in the ERP UI and changes a bill status in admin; someone sets `is_superuser` on the billing system user | List empty; admin status change refused; superuser flag on system user refused; flag changes logged (R24, R25) |
| AT-23 | No managing_partner membership exists | Run `access_bootstrap` twice | First creates the membership and a `bootstrap` log row; second refused (R26) |
| AT-24 | MP in ALM (Sharia enforced) | MP posts an entry to a Sharia-prohibited account and into a closed period | Both refused by ledger rules despite full rights (R7) |
| AT-25 | User with `preferred_language="ar"` | Is refused | `AccessDenied.message_ar` shown; every seeded role and catalogue permission has non-empty en and ar labels (R28) |
| AT-26 | Code scan | Run the test listing public service functions | Every function calls `require` or is on the reviewed allow-list (R1) |
| AT-27 | *Added 2026-10-16 (A11).* In ALM: accountant S drafts and submits a contract version; approver C; `system_billing`; agent user | C approves S's version; S approves it; `system_billing` creates a correction draft, then submits it; S submits it; S approves the note; C approves it; agent edits a version draft | C's approval allowed; S refused (A, R14). Draft creation allowed; `system_billing` submit refused (R13); S submit allowed; S approve refused (submitter, R14); C approve allowed. Agent refused (R12). |
| AT-28 | *Withdrawn 2026-10-17 — see A20, R51 (now AT-54).* *Added 2026-10-16 (A12).* Group parent ALM, child SUB; K is `zatca_admin` in ALM and SUB; K2 has no `zatca_admin` | MP grants K `unseal` on ALM; MP grants K `unseal` on SUB; MP grants K2 `unseal` on ALM; an `access_admin` grants `unseal` on ALM | First allowed, `delegation_granted` in `AccessLog`; second refused (not the group parent); third refused (no `zatca_admin`); fourth refused (`zatca.grant_key_delegation` is MP only) (R17) |
| AT-29 | *Added 2026-10-16 (R27).* U is `accountant` in ALM only | Inside `transaction.atomic()`, U calls a service needing `require(U, SUB, ...)`; the caller catches `AccessDenied` and the block is rolled back | `permission_denied` row for U, SUB exists after the rollback (written on the `access_log` connection). A `membership_granted` row written in a rolled-back block does not exist. Fallback case (actor created in the same uncommitted transaction): refusal row written in the caller's transaction and gone after rollback (known limitation) |
| AT-30 | *Added 2026-10-16 (R30, R31).* Approver P holds a grant from approving bill X in ALM | Pass it to `post_entry` for X; again for X; for bill Y; pass a `Grant` with changed `log_id` or permission | First posts; second refused (used); Y refused (other document); altered grant refused (no matching `AccessLog` row) |
| AT-31 | *Added 2026-10-16 (R32).* Superuser S; MP in ALM; accountant A | S edits `sharia_enforced` on ALM in admin; A calls the entity-settings service; MP calls it | Admin edit refused (field read-only); A refused with `permission_denied` row; MP change applied with an `AccessLog` row holding before/after |
| AT-32 | *Added 2026-10-16 (R33), PostgreSQL only.* Existing `AccessLog` and `Membership` rows | Raw SQL UPDATE and DELETE on both tables; raw UPDATE of `Membership.revoked_at` on an unrevoked row | UPDATE/DELETE rejected by the database; the single revocation write allowed once |
| AT-33 | *Added 2026-10-16 (R34, R35).* No module has registered `Receipt`; then it is registered with `post_permissions={"invoicing.post_receipt"}`; clerk K holds `invoicing.post_receipt` in ALM | K books receipt R (created by another user) before registration; after registration with R; with a tuple `("invoicing.receipt", 1)`; with an unsaved receipt; with a SUB receipt; with a manual draft entry as "document"; with a `post_receipt` grant on a bill type that does not list it; registering a ledger model, an abstract model or a model with `post_permissions={"invoicing.reverse_document"}` | Before registration refused (`document_not_registered`); after: R posts once with `document_type`/`document_id` set, `posted_by` = K; tuple, unsaved, other-entity, manual-draft and wrong-type postings refused with `permission_denied` rows; the three registrations raise `DocumentRegistryError`. *Amended 2026-10-16 (A15):* `Receipt` has a non-null `created_by` and a `validate_lines` hook; also refused with `DocumentRegistryError`: a model whose `created_by` is nullable, is not a user FK, or is missing; a non-callable hook; `register` called from a ledger or `core.access` module for a `testdocs` model; `register` after start-up ("frozen"). No public `unregister` exists |
| AT-34 | *Added 2026-10-16 (R36).* Receipt R booked | Book R again with a fresh grant; reverse R's entry; book R again | Second booking refused (`document_booked`); reversal posts with R's reference; re-booking R refused again; a new receipt R2 books |
| AT-35 | *Added 2026-10-16 (R35).* Receipt R created by K; caller passes an in-memory copy of R with `created_by` changed to another user | K books R | Refused (`self_approval`): checked on the stored row; same when K is R's submitter or last modifier |
| AT-36 | *Added 2026-10-16 (R37).* Accountant A holds `invoicing.reverse_document`; bills B7 (booked) and B8 (booked) | A books a new entry for B7 with the reversal grant; reverses B8's entry with a grant issued for B7; reverses B7's entry with the B7 grant; type registered without a reversal permission | Booking refused; B8 reversal refused; B7 reversal posts; reversal for the type without a registered reversal permission refused |
| AT-37 | *Added 2026-10-16 (R38, R39).* SUB inactive; ALM is the top-level group parent; MP in ALM | MP changes SUB `sharia_enforced`; MP reactivates SUB; MP deactivates ALM; MP sets ALM `parent=None, is_active=False`; ALM made inactive in a fixture, MP changes its `consolidation`, then reactivates it; operator runs `reactivate_entity` on an active entity, then on an inactive one with a reason | SUB settings change refused while inactive; reactivation allowed and logged; ALM deactivation refused (both forms, en/ar); ALM consolidation refused (`inactive_entity_reactivate_only`), reactivation allowed; command refuses the active entity, reactivates the inactive one with an `entity_settings_changed` row (no actor, OS user, reason). *Amended 2026-10-16 (A15), replaces the SUB expectations:* rights are checked on the group parent (R32, R38), so with ALM active the "SUB settings change refused" expectation is not what is built (OQ-21); with ALM inactive, changes to SUB (including `is_active=False`) are refused. The command on inactive SUB while ALM is active is refused ("active group ALM"); the "inactive one" in the last step is a top-level entity or one whose group parent is inactive (AT-44). *Confirmed 2026-10-17 (A18):* the A15 expectations are the decided behaviour: with ALM active, the MP's change to inactive SUB's settings is **allowed** and logged (except Sharia off, which needs R48) |
| AT-38 | *Added 2026-10-16 (R40).* MP in ALM; accountant A | MP creates NEW under SUB; MP creates an entity with no parent; A creates one under ALM; MP creates one under an inactive group parent | NEW created with one `entity_settings_changed` row holding its settings and `base_currency`; no-parent creation refused; A refused; inactive group refused |
| AT-39 | *Added 2026-10-16 (R41).* Active membership M | Revoke via `revoke_membership`; write `revoked_at` = now − 1 h, now + 1 h through the model (and raw SQL on PostgreSQL) | Service revocation within 10 s of now accepted; backdated and future-dated revocations refused |
| AT-40 | *Added 2026-10-16 (R42).* Sales document S (created by K); approver P approves (grant used); S then modified; approver Q approves again (used); P's membership later revoked | Queue posts S with `post_on_clearance` and `posted_by` = none; = P; = Q; = K; = an approver whose grant was issued but not used | None, P (approval before last change), K and the unused approver refused (`posted_by_not_approver`); Q accepted even after Q's membership is revoked; `post_on_clearance` row in `AccessLog` |
| AT-41 | *Added 2026-10-16 (R34, A15).* Receipt R (amount 10.00, created by the CFO) registered with `validate_lines` checking debits = amount; type U registered with `build_lines` and `validate_lines`; type N registered with neither hook | K books R with unrelated lines (50.00); with matching lines; passes an in-memory R with amount changed to 50.00 and 50.00 lines; books U with lines; with `lines=None`; register N | Unrelated lines refused (`document_lines_rejected`, en/ar), nothing written, grant unused; matching lines post; in-memory change refused (hook sees the stored row); U with lines refused (`document_builds_lines`); U with `lines=None` posts the built lines, `validate_lines` called once with them; N refused with `DocumentRegistryError` (**fails at 497cb09**: hook not yet mandatory) |
| AT-42 | *Added 2026-10-16 (R35, A15).* Receipt R stored with a creator; its type's creator reads empty | K books R | Refused `document_no_creator` (en/ar), `permission_denied` row; no entry; grant unused. Storing a receipt with `created_by=None` fails with `IntegrityError` |
| AT-43 | *Added 2026-10-16 (R38, A15).* ALM inactive (fixture); MP in ALM | `require(MP, ALM, "core.change_entity_settings")`; `has(...)`; same with `reactivation=True`; `require(MP, ALM, "core.grant_membership", reactivation=True)`; `change_entity_settings(ALM, consolidation="none")`; `(ALM, is_active=True, sharia_enforced=False)`; `(SUB, is_active=False)`; accountant reactivates ALM; MP reactivates ALM | First refused `inactive_entity`, `has` False; with `reactivation=True` granted; `grant_membership` refused `inactive_entity`; both mixed changes refused `inactive_entity_reactivate_only`; SUB change refused; accountant refused; MP reactivation applied with one `entity_settings_changed` row (before `is_active=False`, after `True`) |
| AT-44 | *Added 2026-10-16 (R39, A15).* SUB inactive, ALM active; environment `USER` and `LOGNAME` set to `someone-else` | Run `reactivate_entity --entity SUB`; set ALM inactive; run it for SUB, then ALM; run it with an empty reason; for an unknown code | First refused naming the active group ALM, SUB stays inactive; then SUB and ALM reactivated; each row has no actor, `source="command"`, reason, and `os_user` = the process uid's name, not `someone-else`; empty reason and unknown code refused |
| AT-45 | *Added 2026-10-16 (R43, A15).* Posted entry O (Dr 1100 10 / Cr 3100 10); draft D with a Sharia-prohibited line (2410) | Set `reversal_of=O` on D by `QuerySet.update`, `bulk_update`, `save`; set a document reference on D or on a booked entry; `create`/`bulk_create` an entry with links; validate reversals of O on 2410, with extra lines, other amounts, same sides; a reversal of a draft; `post_entry` on a correct mirror draft with a link; `reverse_entry` with a proper grant; on PostgreSQL: raw UPDATE of links on a draft, raw INSERT of a reversal of a draft, raw posting of a non-mirroring reversal | All link writes refused ("links"), D stays unlinked and its posting is refused as Sharia-prohibited; the four forged reversals refused ("mirrors every line"); reversal of a draft refused; `post_entry` refused `draft_has_links`; `reverse_entry` posts the mirror and marks O `reversed`; the three raw SQL writes refused by the database |
| AT-46 | *Added 2026-10-16 (R44, A15).* CI PostgreSQL job (`REQUIRE_POSTGRES=1`) | Run with a SQLite test database; run with one PostgreSQL-only test skipped | First fails at session start; second exits as failed listing the skipped test. The SQLite job runs and skips only the PostgreSQL-only tests |
| AT-47 | *Added 2026-10-17 (R45; PostgreSQL, deployment check).* Database connected with the application role (not the migration owner) | Run `ALTER TABLE invoicing_salesdocument DISABLE TRIGGER ALL`; `SET session_replication_role = replica`; `CREATE OR REPLACE FUNCTION invoicing_salesdocument_lock() ...`; `DROP TRIGGER`; `TRUNCATE` on the `AccessLog` table; check `rolsuper`, `rolcreaterole`, `rolbypassrls` and table ownership | Every statement refused with a privilege error; role attributes false; the role owns no application table or function. Go-live fails if any passes |
| AT-48 | *Added 2026-10-17 (invoicing R79, OQ-57 interim default).* Accountant A (holds `zatca.retry_failed`) and `zatca_admin` Z in ALM; agent user with a role in ALM | Each calls `resubmit_to_zatca` on an ALM document never handed to ZATCA | Z allowed with an `AccessLog` row (`zatca_resubmit`); A refused (`no_membership`) and agent refused, each with a `permission_denied` row (invoicing AT-87) |
| AT-49 | *Added 2026-10-17 (A16, R46).* Clerk K (`receivables_clerk` in ALM) records receipt R; K records refund F; user C (`accountant` + `approver` in ALM) creates and submits bill B; ALM `segregation_required` False | K posts R; K posts F; C approves B; K posts a receipt of SUB | R posts, `posted_by` = K, grant log row `self_post=true`; F refused (`self_approval`); B refused (`self_approval`, ledger R35 despite the switch); SUB receipt refused (no membership) |
| AT-50 | *Added 2026-10-17 (A17, R47).* MP M in ALM; users H1, H2 | M grants H1 `chairman` on ALM; M grants H2 `chairman` on ALM while H1's is active; M grants H1 `chairman` on SUB (`consolidation=full`); an `access_admin` grants H2 `chairman`; M grants itself `chairman` | First allowed, `membership_granted` logged; second refused (one holder); third refused (group role only on a group parent, R6); fourth and fifth refused |
| AT-51 | *Added 2026-10-17 (A17, R48, R49).* ALM group parent, SUB (`full`), MIN (`equity`), all Sharia enforced; MP M; chairman H | M calls `change_entity_settings(SUB, sharia_enforced=False)` with no request; M requests; M decides own request; an approver decides it; H approves; M applies it; M applies it again; M turns SUB back on alone | No-request change refused (`dual_control`); M's self-decision refused (`self_approval`); approver refused; H's approval accepted; M's apply sets `sharia_enforced=False` with `dual_control_used` and `entity_settings_changed` rows; second apply refused (`used`); turning on allowed with one `entity_settings_changed` row. Same two-step result for MIN and for ALM itself |
| AT-52 | *Added 2026-10-17 (R48).* Approved request for SUB payload {sharia off, reason "X"} | M applies with reason "Y"; applies with `sharia_enforced=False, consolidation="none"`; applies after `expires_at`; H rejects a second request, M applies it; M creates entity NEW under ALM with `sharia_enforced=False` with no request; SUB is inactive and M applies an approved request | Payload mismatch, mixed change, expired and rejected all refused, SUB unchanged, en/ar messages; NEW refused; inactive SUB change applied (A18) |
| AT-53 | *Added 2026-10-17 (A19, R50).* User A holds `accountant` and `zatca_admin` memberships in ALM; user C holds `approver` + `zatca_admin` in ALM; A's `zatca_admin` membership is then revoked | A resubmits an ALM document; C resubmits one; after the revocation A resubmits again; C approves a sales document C created | A and C allowed (each with `zatca_resubmit` row); after revocation A refused (`no_membership`) though A still holds `accountant`; C's approval of own document refused (R14) |
| AT-54 | *Added 2026-10-17 (A20, R51; replaces AT-28).* ALM group parent, SUB child; K is `zatca_admin` in SUB only | MP grants K custody of SUB's master key; MP grants K custody of ALM's; MP grants a store-wide or `unseal` delegation on ALM; an `access_admin` grants K custody of SUB's | First allowed, `delegation_granted` row names SUB only; second refused (no `zatca_admin` in ALM); third refused (scope withdrawn); fourth refused (MP only) |
| AT-55 | *Added 2026-10-17 (A21, R52).* MP; access_admin AA in ALM; P (`payroll_officer` ALM); H (`hr_officer` ALM); C (`approver` ALM); U holds both `payroll_officer` and `hr_officer` in ALM | MP grants P and H; AA grants a `payroll_officer`; P prepares and submits a run; P approves it; C approves it; H approves it; U prepares a run and approves it | MP grants logged; AA refused; P refused (A); C refused (no permission); H approves, entry posted; U refused (`self_approval`) (payroll AT-33) |
| AT-56 | *Added 2026-10-17 (A22, R53).* Loan drafted by P in ALM; C (`approver`); MP; MIN (`equity`) loan | C approves; MP approves; MP approves the MIN loan without a MIN membership | C refused; MP allowed; MIN refused until an explicit MP membership exists (payroll AT-29) |
| AT-57 | *Added 2026-10-17 (A23(b), R55).* Nomination N for a customer of ALM; MP M; approver C; agent G; the nomination system user | C decides N; G decides N; the system user decides N; M decides `keep` without a reason; M decides `burn` | C, G and the system user refused (`permission_denied` rows); `keep` without reason refused; `burn` applied and logged in `AccessLog` with M, entity and record |
| AT-58 | *Added 2026-10-17 (A23(c), R56).* Catalogue after migration | Scan the new codenames' flags and holders; an agent role given one of them | Each has **A** (and **P** where it posts), none **Ag**; the agent role save refused (R12) |

## 9. Open questions

**B** = blocks building; **GL** = blocks go-live; **D** = a default exists, so building can go ahead with it.

| # | Question | Who | Blocks? | Default |
|---|---|---|---|---|
| OQ-1 | Who approves when the CFO is absent or is the creator? (invoicing OQ-42) Is the Managing Partner fallback approver in every covered entity, including partly-owned ones? | Managing Partner | D | Named delegate per entity (R16); `managing_partner` holds **A** permissions in entities covered by R6 |
| OQ-2 | Who grants memberships per entity? May an `access_admin` grant `approver` and `zatca_admin`? | Managing Partner | D | MP grants all roles; `access_admin` grants every entity role except `access_admin`, `approver`, `zatca_admin`. *Amended 2026-10-17 (A21):* `hr_officer` and `payroll_officer` are granted by the MP only (decided) |
| OQ-3 | Which entities do group roles (`managing_partner`, `group_viewer`, `group_treasury`) cover? Partly-owned and minority entities are separate companies (A4); other shareholders may limit what group staff may see. | Managing Partner + legal | **GL** for partly-owned/minority entities with real data | `full` descendants only; `equity`/`none` need explicit membership |
| OQ-4 | Is MFA required, and for whom? | Managing Partner | GL | TOTP (open source, e.g. `django-otp`) required for `approver`, `zatca_admin`, `access_admin`, `managing_partner` and superusers |
| OQ-5 | Manual journals: ledger-fx §7 lets the Accountant post directly; A1 says the CFO approves and posting follows. Which applies to manual and FX journals, revaluation and reversals? | Managing Partner / CFO | D | A1: `ledger.post_entry` = approver, not own; revaluation run by accountant creates drafts for approval |
| OQ-6 | zatca's "Sales user" is mapped to `receivables_clerk`. Correct? | Managing Partner | D | Yes |
| OQ-7 | Viewer/auditor: may they export the ZATCA archive (zatca §7 yes), even though B2C XML holds personal data that invoicing masks for viewers (R51)? Separate `external_auditor` role? | Managing Partner + legal (PDPL) | D | Export allowed only with `invoicing.view_personal_data`; one `viewer` role |
| OQ-8 | Memberships on an inactive entity: read-only or nothing? | Accountant | D | View permissions still work; ledger already blocks posting. *Amended 2026-10-16 (A14):* plus **I** permissions, for reactivation only (R38) |
| OQ-9 | Keep `auth.User` + `UserProfile`, or swap to a custom user model before the first production migration? | Dev | D | Keep `auth.User` + profile |
| OQ-10 | AccessLog retention, who may read it, DB-level UPDATE/DELETE revoke (PDPL: it holds personal data). | Managing Partner + legal | GL | Keep 10 years; readable by MP and `access_admin` (own entity); DB grants revoked by migration. *Amended 2026-10-17:* the database-role part is no longer open: it is go-live requirement R45 (no trigger bypass, no arbitrary DDL; AT-47). Retention and readers stay open here. **Retention resolved by A23(b) (2026-10-17):** kept indefinitely; personal data in it nominated after 10 years for a human decision (R55). Readers stay open |
| OQ-29 | *Added 2026-10-17 (A23(b), (c)).* Who holds `core.decide_retention` (MP only, or also legal or an entity approver), and who records "written legal clearance" for maternity pay (no legal role exists; hr OQ-44)? | Managing Partner | D | `managing_partner` for both |
| OQ-11 | Dual-control request expiry; does revoking a ZATCA certificate also need dual control (zatca OQ-14 left it open)? | Managing Partner | D | 72 h; revoke does not need dual control |
| OQ-12 | Who closes fiscal periods? Not in any spec. | Accountant / CFO | D | `approver` |
| OQ-13 | **Conflict with an approved decision:** zatca D-9 approved the OQ-14 default "Superuser only for onboarding"; zatca §7 and R24 here give onboarding to `zatca_admin` + key delegation, and superusers get no business rights. | Managing Partner | **B** for zatca onboarding permission wiring | `zatca_admin` + delegation; a technical admin who onboards gets a `zatca_admin` membership, not superuser rights |
| OQ-14 | **Conflict:** `docs/ai-agent-gateway.md` rule 3 allows agent posting per action/entity with an amount limit; A7 says never. | Managing Partner | D | A7 holds (never); gateway doc to be amended by its owner |
| OQ-15 | Should `billing.approve_termination` be "not own"? billing §7 does not say so; invoicing does for all approvals. | Managing Partner | D | Yes, all **A** permissions are segregated. Billing R39/R40 (D9) now state CFO ≠ creator/submitter for every version, terminations included |
| OQ-16 | Maximum delegation length (CFO and key custody; zatca OQ-25). | Managing Partner | D | No maximum; `valid_to` required for CFO delegates, optional for key custody (zatca default) |
| OQ-17 | Should WHT entry and verification be separate people (enter ≠ verify)? | Accountant | D | No rule; same person may do both |
| OQ-18 | *Added 2026-10-16.* Who may change sensitive entity settings (R32): `sharia_enforced`, `consolidation`, `parent`, `base_currency`, `is_active`? Should turning Sharia enforcement off need dual control? | Managing Partner | D | `managing_partner` only (`base_currency` also per ledger-fx R5); no dual control. *Note 2026-10-16:* built as `core.change_entity_settings` (G, I), R32, R38 to R40. **Resolved in part by A17 (2026-10-17):** turning Sharia off is two-step, MP then Chairman (R48); turning it on stays MP only (R49). Other settings: MP only, no dual control, as built |
| OQ-19 | *Added 2026-10-16.* The ledger refuses every sub-ledger posting by the document's creator, submitter or last modifier (R35), also when the module's per-entity switch allows self-approval (R14, AT-7; invoicing `segregation_required = False`). Should the ledger honour the module switch, or is segregation on postings always on? Affects invoicing receipts (invoicing R33, OQ-49). | Managing Partner | D | Always on, as built; R14's switch then only affects approvals that do not post. **Resolved by A16 (2026-10-17)** for receipts: the recorder may post a customer receipt (R46). Always on for every other document, as built |
| OQ-20 | *Added 2026-10-16.* A sales document posts on ZATCA clearance with `posted_by` = an approver whose membership may have ended since approval (R42). Acceptable, or should clearance require the approver to be still active? | Managing Partner | D | Acceptable, as built: the right is checked at approval |
| OQ-21 | *Added 2026-10-16 (A15).* Entity-settings rights are checked on the group parent (R32), so while the group parent is active the MP can change `sharia_enforced`, `consolidation` or `parent` of an **inactive** subsidiary, not only reactivate it. AT-37 (A14) expected a refusal. Should an inactive subsidiary also be reactivate-only? | Managing Partner | D | As built: only an inactive group parent is reactivate-only. **Resolved by A18 (2026-10-17):** the MP may still change an inactive subsidiary's settings (AT-37 as built) |
| OQ-22 | *Added 2026-10-16 (A15).* Is a mandatory line hook (R34) enough to tie a document's entry to it, or must every type use `build_lines` (the ledger builds the lines) rather than only `validate_lines`? | Managing Partner + accountant | D | Either hook; each module's spec says which and what it checks |
| OQ-23 | *Added 2026-10-17 (A17).* "Applies to all of Almiras's owned companies and subsidiaries": is a Sharia switch-off requested and approved **per entity** (as R48 is written), or does one approval switch off a set of entities at once? Does it also cover associates and minority holdings (`equity` / `none`), whose other shareholders may have a say (OQ-3)? | Managing Partner | D | Per entity, one request per entity; covers every entity whose group parent is Almiras, any consolidation |
| OQ-24 | *Added 2026-10-17 (A17).* Chairman: who grants and revokes the `chairman` membership; what happens when the Chairman is absent (no delegate is allowed by R47 as written); does the Chairman need any view rights (e.g. the entity's Sharia report) to decide? | Managing Partner | D | Granted by `managing_partner` only; no delegate (the request waits or expires, R18); no other rights |
| OQ-25 | *Added 2026-10-17 (A19).* Must a `zatca_admin` membership sit next to another active role of the same user in that entity ("add-on"), or may a technical admin hold `zatca_admin` alone (OQ-13)? | Managing Partner | D | No prerequisite, as built |
| OQ-26 | *Added 2026-10-17 (A16).* Does the receipt exception also cover customer refunds (money out, `CustomerReceipt.kind = refund`) and refunds received from vendors (`VendorPayment.kind = refund_received`, money in)? | Managing Partner | D | No: only customer receipts (`kind = receipt`); refunds keep a second person |
| OQ-27 | *Added 2026-10-17 (A19).* The catalogue still gives `accountant` the `zatca.retry_failed` permission (zatca §7 "manually retry `failed_*`"), but resubmission is for `zatca_admin` holders only. Remove it from `accountant`, or keep it for zatca retries other than resubmission? | Managing Partner | D | Keep as built until zatca is built; resubmit refuses it (R50) |
| OQ-28 | *Added 2026-10-17 (A22).* Overtime in non-Almiras companies is approved by the department head: is a `department_head` entity role (only `payroll.approve_overtime` for the department the user heads) acceptable, who grants it, and what if the head is absent or it is their own overtime (hr OQ-30)? | Managing Partner | D | Proposed role as 3.1, granted by `managing_partner`; absent → request waits (R18); own overtime → `managing_partner` |

**Blocking:** OQ-13 blocks wiring zatca onboarding permissions (not the access module itself). **Go-live:** OQ-3, OQ-4, OQ-10, and requirement R45 (application database role privileges, AT-47; added 2026-10-17). This spec also unblocks invoicing OQ-25 (views/APIs) once built.

## Appendix: existing code that must change

| File | Change |
|---|---|
| `core/models.py` → `core/access/models.py` | New `Role`, `UserProfile`, `Membership`, `DualControlRequest`, `AccessLog`; seed migration for roles, system users and catalogue permissions |
| `config/settings.py` | `AUTHENTICATION_BACKENDS` adds `core.access.backends.EntityRoleBackend` (`has_perm(user, perm, obj=Entity)` delegates to `has`; returns False without an entity) |
| `core/admin.py` | `EntityScopedAdmin` base class (R24) |
| `ledger/services.py` | `create_entry`, `post_entry`, `reverse_entry`, `close_period` take a required `user` (and `grant` where R23 applies) and call `require`; `trial_balance` callers use `require(..., "ledger.view")` |
| Specs invoicing, billing, zatca, ledger-fx | §7 role names → codes in 3.1; zatca §7 "Submit" row: issuing is by `approver` (A1), not accountant/sales user |
| *Added 2026-10-17 (A16 to A20):* `ledger/documents.py`, `ledger/services.py` (`_authorise_document_post`), `invoicing/apps.py` | Per-type "creator may post" option for `CustomerReceipt` (`kind = receipt` only); the creator/submitter/modifier refusal at `_authorise_document_post` skips only that case; grant log `self_post=true` (R46) |
| `invoicing/services.py` (`post_receipt`), invoicing tests for AT-70 / AT-73 | The recorder may post; tests expecting `self_approval` for receipts change (invoicing R33) |
| `core/access/catalogue.py`, `core/access/seed.py`, new migration | `chairman` role (group, human, one active holder per group parent: partial unique), `core.disable_sharia_enforcement` (G, DC, S!) |
| `core/access/services.py` (`request_dual_control`, `decide_dual_control`, `_require_dc_holder`, `require_dual_control`) | Role-bound steps for `core.disable_sharia_enforcement`: request through `managing_partner`, decision through `chairman`, no MP fallback (R19, R48) |
| `core/services.py` (`change_entity_settings`, `create_entity`) | `sharia_enforced` True → False (and creation with False) only with an approved, unused request, alone in the call (R48); MP alone for False → True (R49) |
| `core/test_access_hardening.py` (test around lines 128–135: MP turns SUB Sharia off alone) and tests that create entities with `sharia_enforced=False` through `create_entity` | Expectation changes: refused without the two-step request |
