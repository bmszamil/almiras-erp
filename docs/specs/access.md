# Access: users, roles and permissions per entity (user × entity × role)

Django app: `core` (package `core/access/`: `models.py`, `services.py`, `catalogue.py`, `backends.py`). Status: **draft spec**. Written 2026-10-16 by the spec-writer agent. Revised 2026-10-16 (A11, A12: billing versions/corrections, unseal delegation). Amended 2026-10-16 (A13, R27, R30 to R33, AT-29 to AT-32: build decisions and reviewer hardening). Amended 2026-10-16 (A14, R23, R30, R32, R34 to R42, AT-33 to AT-40, OQ-19, OQ-20: ledger document registry, entity lifecycle, clearance approver; code on branch `claude/funny-davinci-7yd88s`, commits aad0126..b690c12).
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
| A12 | The ZATCA master key stays with the Managing Partner; no Shamir shares. The MP may delegate key access, including unsealing the secret store, to named custodians. | Project owner (zatca D-10) | 2026-10-16 |
| A13 | *Added 2026-10-16.* Hardening adopted during the build from code-reviewer findings: (a) posting grants are tied to one document and are single-use; (b) a grant is verified against its `AccessLog` row before use; (c) sensitive entity settings (`sharia_enforced`, `consolidation`, `parent`, `base_currency`, `is_active`) change only through logged, permission-checked services, never Django admin; (d) `AccessLog` and `Membership` are append-only, also at database level on PostgreSQL; (e) `permission_denied` rows are written durably outside the caller's transaction (R27). | Build decision (code-reviewer, caller brief); human approval at merge | 2026-10-16 |
| A14 | *Added 2026-10-16.* Further hardening adopted during the build: (a) sub-ledger postings go through a ledger **document registry**; a document books once (R34 to R36); (b) `invoicing.reverse_document` only reverses (R37); (c) entity lifecycle: catalogue flag **I**, the `reactivate_entity` recovery command, the top-level group parent cannot be deactivated, new entities only inside an existing group (R38 to R40); (d) revocation time is the current time, within 10 s (R41); (e) the `posted_by` of a posting on ZATCA clearance is a current, used approval (R42). | Build decision (code-reviewer, caller brief); human approval at merge | 2026-10-16 |

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
| `group_treasury` | Group treasury | خزينة المجموعة | group | human | ledger-fx: Group treasury |
| `group_viewer` | Group / consolidation viewer | مطّلع على المجموعة (التوحيد) | group | human | new (consolidation reporting) |
| `system_billing` | Billing system user | مستخدم نظام الفوترة | entity | system | invoicing: Billing system user; billing: Billing service user |
| `system_zatca_queue` | ZATCA queue worker | مستخدم نظام طابور فاتورة | entity | system | zatca: Queue worker (system user) |
| `system_fx_import` | FX rate import user | مستخدم نظام استيراد أسعار الصرف | group | system | ledger-fx: Import service user |
| `agent_<name>` (one per agent) | e.g. Agent: invoice reader | مثال: وكيل قراءة الفواتير | entity | agent | invoicing: Agent service user; zatca: AI agent (gateway); billing/ledger-fx: Agent |

**Not roles:**
- *Key custodian*: a `zatca.KeyCustodianDelegation` row (zatca 3.9), granted by `managing_partner`, checked by zatca R-41 on top of `zatca_admin`.
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

Flags: **A** = approval (creator ≠ approver, R14); **S!** = segregation that cannot be switched off; **DC** = dual control (R19); **G** = group-level only; **Ag** = an agent role may hold it; **Sys:x** = only system user *x* may hold it; **P** = the grant authorises a ledger posting (R23); **I** = still usable on an inactive entity, but only to reactivate it (R38; *added 2026-10-16, A14*).

| Codename | Flags | Holders (seed) |
|---|---|---|
| `core.view_access` | | access_admin, managing_partner, viewer (own entity) |
| `core.grant_membership` | S! | access_admin, managing_partner |
| `core.delegate_approval` | S! | approver (own approver membership only), managing_partner |
| `core.change_entity_settings` (R32, R38 to R40) | G, I | managing_partner (OQ-18 default). *Added 2026-10-16 (A14)* |
| `core.manage_users` (activate / deactivate user accounts) | G | managing_partner. *Added 2026-10-16 (A14)* |
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
| `invoicing.post_receipt` (R33) | P | receivables_clerk, accountant |
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
| `zatca.retry_failed` | | zatca_admin, accountant |
| `zatca.view` / `zatca.view_status` | — / Ag | zatca_admin, accountant, viewer / agent (if granted) |
| `zatca.export_archive` | | zatca_admin, accountant, viewer (OQ-7) |
| `zatca.close_late_notice` | | zatca_admin |
| `zatca.grant_key_delegation` | G, S! | managing_partner |
| `ledger.create_entry` (draft) | Ag | accountant, agent (table rates only, ledger-fx) |
| `ledger.post_entry` (manual journals) | A, P | approver; accountant pending OQ-5 |
| `ledger.reverse_entry` (manual journals) | P | accountant (OQ-5) |
| `ledger.close_period` | | approver (OQ-12) |
| `ledger.maintain_global_rate` | G | group_treasury |
| `ledger.import_global_rate` | G, Sys:fx_import | system_fx_import |
| `ledger.maintain_entity_rate` / `ledger.use_manual_rate` / `ledger.revalue` | | accountant (+ group_treasury for entity rates) / accountant / accountant |
| `ledger.view` / `ledger.view_rates` / `ledger.view_translation` | Ag (scoped) | all human roles, incl. group_viewer |
| `ledger.change_functional_currency` | G | managing_partner (before first posting) |

`managing_partner` also holds every **A** permission as fallback approver in the entities it covers (invoicing OQ-42 default; this spec's OQ-1).

### 3.7 Service API (`core/access/services.py`)

```python
require(user, entity, permission, *, document=None, allow_self_approval=False) -> Grant   # raises AccessDenied
has(user, entity, permission) -> bool
entities_for(user, permission) -> QuerySet[Entity]
scope(queryset, user, permission, entity_field="entity") -> QuerySet
require_dual_control(user, entity, permission, request: DualControlRequest, payload) -> Grant
system_user(code) -> User
grant_membership(actor, user, entity, role, valid_from, valid_to=None, reason="", delegated_from=None) -> Membership
revoke_membership(actor, membership, reason) -> Membership
delegate_approval(actor, delegate, entity, valid_from, valid_to, reason) -> Membership
```

*Added 2026-10-16 (A14), entity services in `core/services.py`:* `change_entity_settings(entity, *, user, reason, **changes)`, `create_entity(entity, *, user, reason)`, `reactivate_entity_by_operator(entity, reason, os_user)` (called only by the `reactivate_entity` management command). Ledger side: `ledger.documents.register(model, *, post_permissions, reverse_permissions=(), entity_field="entity", changed_at_field="modified_at")`, called in the owning app's `AppConfig.ready()`.

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
18. When the user who should approve is absent, the document waits; the system never routes approval to another person on its own. **[S]**

**Dual control**
19. A **DC** action runs only with a `DualControlRequest` in status `approved`, not expired, for the same entity, permission, target and `payload_hash`, decided by a different user who holds the permission (or `managing_partner`). Using it sets `used`; it is single-use. **[S; test AT-19, AT-20]**

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
35. The document passed with a sub-ledger posting must be a saved model instance (never a tuple or stand-in) of a registered type whose posting set lists the grant's permission, must exist in the database, and must belong to the entity being posted. Otherwise refused (`grant_needs_document`, `document_not_registered`, `document_not_found`, `document_other_entity`), logged as `permission_denied`. Segregation is checked on the **stored** document, not the object the caller passed: the grant's user must not be its `created_by`, `submitted_by` or `modified_by` (`self_approval`). The ledger applies this to every sub-ledger posting, whatever the module's segregation switch (OQ-19). **[S in ledger; test AT-33, AT-35]**
36. One booking per document: a document has at most one non-reversal entry (DB partial unique on `(entity, document_type, document_id)` where `reversal_of` is null, plus a service check, `document_booked`). A reversed document is not booked again; the correction is a **new document** with its own entry. **[S + DB; test AT-34]**
37. `invoicing.reverse_document` (and any registered reversal permission) only reverses the entry of the document it was issued for; it never books a document's entry. **[S in ledger; test AT-36]**

**Entity lifecycle (added 2026-10-16, A14)**
38. On an inactive entity, rights are refused except view permissions (OQ-8 default) and permissions flagged **I**. An **I** permission on an inactive entity allows only reactivation (`change_entity_settings(..., is_active=True)` and nothing else); any other change is refused (`inactive_entity_reactivate_only`). **[S; test AT-37]**
39. Recovery: when nobody can reactivate an entity, an operator runs `manage.py reactivate_entity` with a reason. It sets `is_active=True` only on an inactive entity and writes an `entity_settings_changed` row (no actor; `source="command"`, OS user, reason, before/after). The top-level group parent (no `parent`) cannot be deactivated, also not by making it top-level and inactive in one change, because every group right is checked on it. **[S + command; test AT-37]**
40. `core.services.create_entity` creates an entity only under a parent in an existing group: the user needs `core.change_entity_settings` on that group's parent, which must be active. It never creates a new top-level group (an operator task). It writes one `entity_settings_changed` row with the new entity's sensitive settings and `base_currency`. **[S; test AT-38]**

**Revocation time and clearance approver (added 2026-10-16, A14)**
41. A membership revocation's `revoked_at` must be within 10 seconds of the current time when written (model `save`, and the PostgreSQL trigger of R33): no backdated or future-dated revocation. **[M + DB; test AT-39]**
42. On a posting with `invoicing.post_on_clearance`, `posted_by` is required and must be a human who is not the document's creator, submitter or last modifier, and whose `invoicing.approve_sales_document` grant for this document was **used** and was issued **after the document's last change** (`modified_at`); earlier or unused approvals do not count. The approver need not still be active at clearance: the right was checked at approval (OQ-20). The posting writes an `AccessLog` row (`post_on_clearance`, document, entry, `posted_by`). **[S in ledger; test AT-40]**

## 5. Accounting

None. The access module posts no journal entries and touches no accounts. It only controls who may call the ledger and sub-ledger services (R23).

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| Agent gateway (`/api/agents/v1/`, MCP) | In | Token → agent user; every call checked with `require` | Gateway not built |
| OpenBao (via zatca) | Out | Nothing from `core`. zatca applies the custodian's OpenBao policy on delegation (R-41) | zatca spec |
| ZATCA, banks, GOSI, Mudad, SAP B1, Daftra | — | None. Later HR (GOSI/Mudad) and integrations use this same model | — |
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
| AT-28 | *Added 2026-10-16 (A12).* Group parent ALM, child SUB; K is `zatca_admin` in ALM and SUB; K2 has no `zatca_admin` | MP grants K `unseal` on ALM; MP grants K `unseal` on SUB; MP grants K2 `unseal` on ALM; an `access_admin` grants `unseal` on ALM | First allowed, `delegation_granted` in `AccessLog`; second refused (not the group parent); third refused (no `zatca_admin`); fourth refused (`zatca.grant_key_delegation` is MP only) (R17) |
| AT-29 | *Added 2026-10-16 (R27).* U is `accountant` in ALM only | Inside `transaction.atomic()`, U calls a service needing `require(U, SUB, ...)`; the caller catches `AccessDenied` and the block is rolled back | `permission_denied` row for U, SUB exists after the rollback (written on the `access_log` connection). A `membership_granted` row written in a rolled-back block does not exist. Fallback case (actor created in the same uncommitted transaction): refusal row written in the caller's transaction and gone after rollback (known limitation) |
| AT-30 | *Added 2026-10-16 (R30, R31).* Approver P holds a grant from approving bill X in ALM | Pass it to `post_entry` for X; again for X; for bill Y; pass a `Grant` with changed `log_id` or permission | First posts; second refused (used); Y refused (other document); altered grant refused (no matching `AccessLog` row) |
| AT-31 | *Added 2026-10-16 (R32).* Superuser S; MP in ALM; accountant A | S edits `sharia_enforced` on ALM in admin; A calls the entity-settings service; MP calls it | Admin edit refused (field read-only); A refused with `permission_denied` row; MP change applied with an `AccessLog` row holding before/after |
| AT-32 | *Added 2026-10-16 (R33), PostgreSQL only.* Existing `AccessLog` and `Membership` rows | Raw SQL UPDATE and DELETE on both tables; raw UPDATE of `Membership.revoked_at` on an unrevoked row | UPDATE/DELETE rejected by the database; the single revocation write allowed once |
| AT-33 | *Added 2026-10-16 (R34, R35).* No module has registered `Receipt`; then it is registered with `post_permissions={"invoicing.post_receipt"}`; clerk K holds `invoicing.post_receipt` in ALM | K books receipt R (created by another user) before registration; after registration with R; with a tuple `("invoicing.receipt", 1)`; with an unsaved receipt; with a SUB receipt; with a manual draft entry as "document"; with a `post_receipt` grant on a bill type that does not list it; registering a ledger model, an abstract model or a model with `post_permissions={"invoicing.reverse_document"}` | Before registration refused (`document_not_registered`); after: R posts once with `document_type`/`document_id` set, `posted_by` = K; tuple, unsaved, other-entity, manual-draft and wrong-type postings refused with `permission_denied` rows; the three registrations raise `DocumentRegistryError` |
| AT-34 | *Added 2026-10-16 (R36).* Receipt R booked | Book R again with a fresh grant; reverse R's entry; book R again | Second booking refused (`document_booked`); reversal posts with R's reference; re-booking R refused again; a new receipt R2 books |
| AT-35 | *Added 2026-10-16 (R35).* Receipt R created by K; caller passes an in-memory copy of R with `created_by` changed to another user | K books R | Refused (`self_approval`): checked on the stored row; same when K is R's submitter or last modifier |
| AT-36 | *Added 2026-10-16 (R37).* Accountant A holds `invoicing.reverse_document`; bills B7 (booked) and B8 (booked) | A books a new entry for B7 with the reversal grant; reverses B8's entry with a grant issued for B7; reverses B7's entry with the B7 grant; type registered without a reversal permission | Booking refused; B8 reversal refused; B7 reversal posts; reversal for the type without a registered reversal permission refused |
| AT-37 | *Added 2026-10-16 (R38, R39).* SUB inactive; ALM is the top-level group parent; MP in ALM | MP changes SUB `sharia_enforced`; MP reactivates SUB; MP deactivates ALM; MP sets ALM `parent=None, is_active=False`; ALM made inactive in a fixture, MP changes its `consolidation`, then reactivates it; operator runs `reactivate_entity` on an active entity, then on an inactive one with a reason | SUB settings change refused while inactive; reactivation allowed and logged; ALM deactivation refused (both forms, en/ar); ALM consolidation refused (`inactive_entity_reactivate_only`), reactivation allowed; command refuses the active entity, reactivates the inactive one with an `entity_settings_changed` row (no actor, OS user, reason) |
| AT-38 | *Added 2026-10-16 (R40).* MP in ALM; accountant A | MP creates NEW under SUB; MP creates an entity with no parent; A creates one under ALM; MP creates one under an inactive group parent | NEW created with one `entity_settings_changed` row holding its settings and `base_currency`; no-parent creation refused; A refused; inactive group refused |
| AT-39 | *Added 2026-10-16 (R41).* Active membership M | Revoke via `revoke_membership`; write `revoked_at` = now − 1 h, now + 1 h through the model (and raw SQL on PostgreSQL) | Service revocation within 10 s of now accepted; backdated and future-dated revocations refused |
| AT-40 | *Added 2026-10-16 (R42).* Sales document S (created by K); approver P approves (grant used); S then modified; approver Q approves again (used); P's membership later revoked | Queue posts S with `post_on_clearance` and `posted_by` = none; = P; = Q; = K; = an approver whose grant was issued but not used | None, P (approval before last change), K and the unused approver refused (`posted_by_not_approver`); Q accepted even after Q's membership is revoked; `post_on_clearance` row in `AccessLog` |

## 9. Open questions

**B** = blocks building; **GL** = blocks go-live; **D** = a default exists, so building can go ahead with it.

| # | Question | Who | Blocks? | Default |
|---|---|---|---|---|
| OQ-1 | Who approves when the CFO is absent or is the creator? (invoicing OQ-42) Is the Managing Partner fallback approver in every covered entity, including partly-owned ones? | Managing Partner | D | Named delegate per entity (R16); `managing_partner` holds **A** permissions in entities covered by R6 |
| OQ-2 | Who grants memberships per entity? May an `access_admin` grant `approver` and `zatca_admin`? | Managing Partner | D | MP grants all roles; `access_admin` grants every entity role except `access_admin`, `approver`, `zatca_admin` |
| OQ-3 | Which entities do group roles (`managing_partner`, `group_viewer`, `group_treasury`) cover? Partly-owned and minority entities are separate companies (A4); other shareholders may limit what group staff may see. | Managing Partner + legal | **GL** for partly-owned/minority entities with real data | `full` descendants only; `equity`/`none` need explicit membership |
| OQ-4 | Is MFA required, and for whom? | Managing Partner | GL | TOTP (open source, e.g. `django-otp`) required for `approver`, `zatca_admin`, `access_admin`, `managing_partner` and superusers |
| OQ-5 | Manual journals: ledger-fx §7 lets the Accountant post directly; A1 says the CFO approves and posting follows. Which applies to manual and FX journals, revaluation and reversals? | Managing Partner / CFO | D | A1: `ledger.post_entry` = approver, not own; revaluation run by accountant creates drafts for approval |
| OQ-6 | zatca's "Sales user" is mapped to `receivables_clerk`. Correct? | Managing Partner | D | Yes |
| OQ-7 | Viewer/auditor: may they export the ZATCA archive (zatca §7 yes), even though B2C XML holds personal data that invoicing masks for viewers (R51)? Separate `external_auditor` role? | Managing Partner + legal (PDPL) | D | Export allowed only with `invoicing.view_personal_data`; one `viewer` role |
| OQ-8 | Memberships on an inactive entity: read-only or nothing? | Accountant | D | View permissions still work; ledger already blocks posting. *Amended 2026-10-16 (A14):* plus **I** permissions, for reactivation only (R38) |
| OQ-9 | Keep `auth.User` + `UserProfile`, or swap to a custom user model before the first production migration? | Dev | D | Keep `auth.User` + profile |
| OQ-10 | AccessLog retention, who may read it, DB-level UPDATE/DELETE revoke (PDPL: it holds personal data). | Managing Partner + legal | GL | Keep 10 years; readable by MP and `access_admin` (own entity); DB grants revoked by migration |
| OQ-11 | Dual-control request expiry; does revoking a ZATCA certificate also need dual control (zatca OQ-14 left it open)? | Managing Partner | D | 72 h; revoke does not need dual control |
| OQ-12 | Who closes fiscal periods? Not in any spec. | Accountant / CFO | D | `approver` |
| OQ-13 | **Conflict with an approved decision:** zatca D-9 approved the OQ-14 default "Superuser only for onboarding"; zatca §7 and R24 here give onboarding to `zatca_admin` + key delegation, and superusers get no business rights. | Managing Partner | **B** for zatca onboarding permission wiring | `zatca_admin` + delegation; a technical admin who onboards gets a `zatca_admin` membership, not superuser rights |
| OQ-14 | **Conflict:** `docs/ai-agent-gateway.md` rule 3 allows agent posting per action/entity with an amount limit; A7 says never. | Managing Partner | D | A7 holds (never); gateway doc to be amended by its owner |
| OQ-15 | Should `billing.approve_termination` be "not own"? billing §7 does not say so; invoicing does for all approvals. | Managing Partner | D | Yes, all **A** permissions are segregated. Billing R39/R40 (D9) now state CFO ≠ creator/submitter for every version, terminations included |
| OQ-16 | Maximum delegation length (CFO and key custody; zatca OQ-25). | Managing Partner | D | No maximum; `valid_to` required for CFO delegates, optional for key custody (zatca default) |
| OQ-17 | Should WHT entry and verification be separate people (enter ≠ verify)? | Accountant | D | No rule; same person may do both |
| OQ-18 | *Added 2026-10-16.* Who may change sensitive entity settings (R32): `sharia_enforced`, `consolidation`, `parent`, `base_currency`, `is_active`? Should turning Sharia enforcement off need dual control? | Managing Partner | D | `managing_partner` only (`base_currency` also per ledger-fx R5); no dual control. *Note 2026-10-16:* built as `core.change_entity_settings` (G, I), R32, R38 to R40 |
| OQ-19 | *Added 2026-10-16.* The ledger refuses every sub-ledger posting by the document's creator, submitter or last modifier (R35), also when the module's per-entity switch allows self-approval (R14, AT-7; invoicing `segregation_required = False`). Should the ledger honour the module switch, or is segregation on postings always on? Affects invoicing receipts (invoicing R33, OQ-49). | Managing Partner | D | Always on, as built; R14's switch then only affects approvals that do not post |
| OQ-20 | *Added 2026-10-16.* A sales document posts on ZATCA clearance with `posted_by` = an approver whose membership may have ended since approval (R42). Acceptable, or should clearance require the approver to be still active? | Managing Partner | D | Acceptable, as built: the right is checked at approval |

**Blocking:** OQ-13 blocks wiring zatca onboarding permissions (not the access module itself). **Go-live:** OQ-3, OQ-4, OQ-10. This spec also unblocks invoicing OQ-25 (views/APIs) once built.

## Appendix: existing code that must change

| File | Change |
|---|---|
| `core/models.py` → `core/access/models.py` | New `Role`, `UserProfile`, `Membership`, `DualControlRequest`, `AccessLog`; seed migration for roles, system users and catalogue permissions |
| `config/settings.py` | `AUTHENTICATION_BACKENDS` adds `core.access.backends.EntityRoleBackend` (`has_perm(user, perm, obj=Entity)` delegates to `has`; returns False without an entity) |
| `core/admin.py` | `EntityScopedAdmin` base class (R24) |
| `ledger/services.py` | `create_entry`, `post_entry`, `reverse_entry`, `close_period` take a required `user` (and `grant` where R23 applies) and call `require`; `trial_balance` callers use `require(..., "ledger.view")` |
| Specs invoicing, billing, zatca, ledger-fx | §7 role names → codes in 3.1; zatca §7 "Submit" row: issuing is by `approver` (A1), not accountant/sales user |
