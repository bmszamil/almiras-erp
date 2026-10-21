# Access: users, roles and permissions per entity (user × entity × role)

Django app: `core` (package `core/access/`: `models.py`, `services.py`, `catalogue.py`, `backends.py`). Status: **draft spec**. Written 2026-10-16 by the spec-writer agent. Revised 2026-10-16 (A11, A12: billing versions/corrections, unseal delegation). Amended 2026-10-16 (A13, R27, R30 to R33, AT-29 to AT-32: build decisions and reviewer hardening). Amended 2026-10-16 (A14, R23, R30, R32, R34 to R42, AT-33 to AT-40, OQ-19, OQ-20: ledger document registry, entity lifecycle, clearance approver; code on branch `claude/funny-davinci-7yd88s`, commits aad0126..b690c12). Amended 2026-10-16 (A15, 3.6 flag I, 3.7, R34, R35, R37 to R39, R43, R44, AT-33, AT-37, AT-41 to AT-46, OQ-21, OQ-22: registry creator and line hooks, reversal integrity, explicit reactivation, recovery command scope, SQLite limitation; commits b6ed6e1..497cb09). Amended 2026-10-17 (project-owner decisions A16 to A20: receipts posted by their recorder, `chairman` role and two-step Sharia switch-off, inactive subsidiaries, `zatca_admin` as add-on role, hierarchical ZATCA keys; 3.1, 3.6, R17, R19, R35, R38, R46 to R51, AT-28, AT-37, AT-49 to AT-55, OQ-18, OQ-19, OQ-21, OQ-23 to OQ-27). Amended 2026-10-17 (project-owner HR/payroll decisions A21, A22: roles `hr_officer` and `payroll_officer`, HR officer approves payroll, MP approves loans and bai' ajel, overtime approval routing; 3.1, 3.6, R52 to R54, AT-55, AT-56, OQ-2, OQ-28; detail in hr.md H9 to H16 and payroll.md P8 to P15). Amended 2026-10-17 (A23, project owner / CFO decision pack: receipts posted by their recorder only without a bank charge, records kept indefinitely with human "burn" decisions, new permissions from the pack, legacy system Falcon; R46 note, new R55, R56, §6, AT-57, AT-58, OQ-10, new OQ-29). Amended 2026-10-18 (project-owner answers A24 to A32, `docs/approvals/owner-answers-2026-10-18.md` §A, B7: authority per company, per-entity authority matrix as data, self-approval only by the entity's top authority, no cascade of group roles, CEO and new roles, system users issue/post after prior human approval, one approval engine (option C), custom user model, delegation, retention by internal audit committee minute; notes on A1, A8, A21, A22, 3.1, 3.2, R6, R14, R16, R18, R53, R54, R55; new 3.8 to 3.10, R57 to R68, AT-59 to AT-72, OQ updates, new OQ-30 to OQ-41; all **to build**). Amended 2026-10-20 (new §10 "Authority matrix, top-authority self-approval and governance report (to build)": owner answers A1 to A5, A10, A13, G3, G8 and payroll §10.4; matrix rows with currency, named approver and delegation flag, code defaults where no row exists, matrix versions under dual control, the full list of approvable documents routed through the one engine, the list of activities where self-approval is never allowed, the monthly governance report, delegation with expiry, rights of the five new roles; R69 to R90, AT-73 to AT-96, OQ-42 to OQ-55; replaces the "missing row is refused" part of R57 and the OQ-31 default; all **to build**). Built 2026-10-20 (§10.10 "As built": engine, matrix versions, delegation, assistants, governance report, seed V17, migrations core 0026 to 0028).
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

*Added 2026-10-18 (project-owner answers, `docs/approvals/owner-answers-2026-10-18.md`).* Note on wording: in the owner's questions 1 and 13, "branch" meant the code branch, not a company branch; the answers describe **company governance** and are recorded as such. Where they differ from earlier decisions on either code line (`main`'s MP decisions or this spec's A1 to A23), the 2026-10-18 answers prevail (A24).

| # | Decision | Who | Date |
|---|---|---|---|
| A24 | (owner A1) Authority in each company lies with **that company's CEO or General Manager**. The Managing Partner of Almiras sits on the board or acts as shareholder representative only in the other companies. The project owner's current answers prevail over earlier decisions on either code line. *Effect:* A1 ("the CFO approves") and A22 (MP approves loans, bai' ajel, overtime everywhere) no longer fix the approver; the approver comes from each entity's authority matrix (A25). | Project owner | 2026-10-18 |
| A25 | (owner A2, A10) The Managing Partner does **not** approve everything. Each company has its own **authority matrix** for every task and activity: activity / document type × amount band × role → approver role(s), held as **data per entity** (new subsidiaries are data, CLAUDE.md). Examples given: contracted payroll needs no Managing Partner approval; overtime in Almiras needs the Managing Partner; other companies have their own approval structures. **All approvable documents go through the same approval engine** (option C: `main`'s approvals engine as the workflow and inbox layer; `core.access` stays the authorisation layer: roles, memberships, `require`, single-use document-bound grants, dual control). | Project owner | 2026-10-18 |
| A26 | (owner A3) Self-approval is allowed **only** to the highest authority of each entity: the Managing Partner in Almiras, the CEO in each other company. The top authority is a **configurable setting per entity** (data). No other role may ever self-approve. Every self-approval is logged and listed in the **monthly governance / internal-audit report**. | Project owner | 2026-10-18 |
| A27 | (owner A4) Each company is granted separately: **no cascade of group roles** to subsidiaries (replaces R6's coverage of `full` descendants). | Project owner | 2026-10-18 |
| A28 | (owner A5) The **CEO** can approve and post, and can also act as accountant. Roles also needed: **internal auditor**, **personal assistant**, **business analyst**, **external auditor** (read-only, time-limited). | Project owner | 2026-10-18 |
| A29 | (owner A6) **System users may issue and post after a person's prior approval** (billing auto-issue under an approved contract; ZATCA queue posting on clearance under an approved document). `main`'s D17 ("integrations are drafts only") applies to external integration and agent users, not to these internal system users. | Project owner | 2026-10-18 |
| A30 | (owner A12) User model: follow best practice for large groups and large ERP systems. *Interpretation (recorded by the team, to be confirmed if wrong):* a **custom user model** (Django's recommended practice for new projects): `main`'s `access.User(AbstractUser)` with this spec's `UserProfile` fields folded in, swapped in before any production database exists (resolves OQ-9). | Project owner | 2026-10-18 |
| A31 | (owner A13) Option C confirmed. The CEO of the company approves and reviews, and **may delegate** this to another user. *Interpretation:* the project owner (or their delegate) reviews and approves the integration before it replaces `main` (integration doc §7). In the ERP: **delegation of approval authority** is general (any approver, not only the CFO), dated (`valid_from`, `valid_to` required) and logged (R62; replaces R16). | Project owner | 2026-10-18 |
| A32 | (owner A11, B7) Retention confirmed: keep everything; data older than 10 years is nominated for destruction, and destruction is decided by an **internal audit committee approval minute**; the minute reference is recorded on each decision (R55 amended, R67). | Project owner | 2026-10-18 |

Notes on earlier decisions (2026-10-18): **A1** superseded for "who approves" by A24/A25 (staff create; the approver comes from the entity's matrix; posting stays automatic on approval). **A8** superseded by A26: creator ≠ approver is no longer switchable per entity per module; only the entity's top authority may self-approve. **A21** stands as the default payroll-run matrix row (HR officer, no MP: "contracted payroll needs no Managing Partner approval"). **A22** stands for Almiras (MP approves overtime, loans, bai' ajel); for other entities it is replaced by their matrix (OQ-39). **A16** (receipts posted by their recorder) is not an approval step and stays (OQ-40).

Not decided (in §9): who approves when the CFO is absent or is the creator (OQ-1), who may grant memberships (OQ-2), which entities group-wide roles cover (OQ-3), and others.

## 3. Data model

User model: Django `auth.User` stays (`AUTH_USER_MODEL` is not swapped; OQ-9). Every model below is append-only except where stated.
*Amended 2026-10-18 (A30; to build):* the user model becomes the custom `access.User(AbstractUser)` from `main` (`AUTH_USER_MODEL = "access.User"`), set before the first production migration on a fresh database. The `UserProfile` fields of 3.2 move onto `User` (default; keeping a OneToOne profile on the new user is the alternative, OQ-9). All FKs already use `settings.AUTH_USER_MODEL`.

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
| `ceo` | Chief executive officer / General manager | الرئيس التنفيذي / المدير العام | entity | human | new (A24, A28; to build). Approve and post (every **A**, **P** permission the entity's matrix assigns to it) and the `accountant` create/submit permissions. Default top authority of every entity except the group parent (R58) |
| `internal_auditor` | Internal auditor | المدقق الداخلي | entity | human | new (A28; to build; `main`: `auditor`). Read-only: every view permission of the entity, `AccessLog`, the governance report (R59). Never **A**, **P**, **Ag** create. Records retention decisions with the committee minute (R55, default OQ-29) |
| `personal_assistant` | Personal assistant | مساعد شخصي | entity | human | new (A28; to build; `main`: `pa_drafter`). Drafts and uploads only (create permissions, no submit, approve or post); permission set OQ-37 |
| `business_analyst` | Business analyst | محلل أعمال | entity | human | new (A28; to build). Read-only on the modules granted (reports); no personal data by default; permission set OQ-37 |
| `external_auditor` | External auditor | المدقق الخارجي | entity | human | new (A28; to build). Read-only; membership must have `valid_to` (time-limited, R60); never **A**, **P**, create or export of personal data unless OQ-36 decides otherwise. Replaces the "auditor" half of `viewer` (OQ-7) |
| `system_intercompany` | Intercompany system user | مستخدم نظام المعاملات بين شركات المجموعة | entity | system | new (billing D14(b), R73; to build). Holds only `invoicing.create_mirror_bill` (Sys:intercompany) in each buyer entity where it has a membership |

*Note 2026-10-18 (A27):* `scope=group` no longer means "covers descendants". Group roles (`managing_partner`, `chairman`, `group_treasury`, `group_viewer`) are granted on the group parent and act there, on group-level records and on **G** permissions checked on the group parent (R6 amended). Every other entity, subsidiaries included, needs its own membership. *Note 2026-10-18 (A24, A25):* `approver` (CFO) keeps its permissions as data, but which approvals it may give in an entity is set by that entity's authority matrix (3.8).

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
| `invoicing.approve_purification_funding` (invoicing R54, owner decision C2, 2026-10-18; seed V10) | A, P | approver; managing_partner (fallback, OQ-1) |
| `invoicing.mark_purification_instruction_sent` (invoicing R54, owner decision C4; seed V10) | | accountant |
| `tax.propose_box_verification` (tax-returns §10.6, owner decision H3, 2026-10-19; seed V12, `core/0021`) | | accountant (proposes a VAT box definition as checked; the approver verifies with `tax.maintain_rates`) |
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

### 3.8 Authority matrix (added 2026-10-18, A25; to build)

Per-entity data, read by the approval engine (3.10). A new subsidiary gets its matrix as setup data, never code.
*Note 2026-10-20:* extended and partly replaced by §10.3 (matrix versions, currency, named approver, delegation flag, code defaults). Where §10 and this subsection differ, §10 applies.

**`EntityGovernance`** (one per entity)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | OneToOne `core.Entity`, PROTECT | Y | | |
| top_authority_role | FK Role, PROTECT | Y | | Default `managing_partner` for the group parent (Almiras), `ceo` for every other entity (A26, R58). Changed only through `change_entity_settings` (R32), logged |
| governance_report_recipients | M2M Role | Y | | Default `internal_auditor`, the top-authority role (OQ-33) |

**`ApprovalActivity`** (catalogue data, global; like `Role`, no entity FK)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| code | Slug(60), unique | Y | | e.g. `ledger.manual_entry`, `invoicing.bill`, `invoicing.vendor_payment`, `invoicing.sales_document`, `billing.contract`, `billing.version`, `billing.correction_note`, `billing.timesheet`, `billing.cost_pool_period`, `payroll.run`, `payroll.overtime`, `payroll.loan`, `payroll.deferred_sale_terms`, `payroll.deferred_sale`, `payroll.cost_item`, `payroll.maternity_clearance`, `invoicing.recovery_ratio`, `invoicing.art51_true_up`, `invoicing.deferred_release`, `invoicing.deposit_application`, `migration.account_map`; one per approvable document type (R61) |
| name_en / name_ar | Char(120) | Y | Y | |
| permission | Char(100) | Y | | The catalogue **A** permission checked at approval (3.6) |
| amount_basis | Choice `document_total`, `none`, other per type | Y | | What the band is measured on (OQ-35) |

**`AuthorityRule`** (per entity; append-only; a change = end-date + new row)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK `core.Entity`, PROTECT | Y | | |
| activity | FK ApprovalActivity, PROTECT | Y | | |
| amount_from / amount_to | Decimal(18,2), `amount_to` null = no upper limit | Y / N | | In the entity's functional currency (SAR); bands of one activity may not overlap at any date (R57) |
| step_no | PositiveSmallInt | Y | | 1..n; steps run in order; equal `step_no` = parallel |
| approver_roles | M2M Role | Y | | Any active holder of one of these roles in the entity, holding `activity.permission`, is eligible (R57) |
| min_approvals | PositiveSmallInt, default 1 | Y | | Per step |
| valid_from / valid_to | Date / Date, null | Y / N | | |
| created_by / created_at, reason | FK User / DateTime / Char(500) | Y | | Who maintains rows: OQ-31 |

### 3.9 `ApprovalDelegation` (added 2026-10-18, A31; to build; replaces the CFO-only delegate of 3.3 / R16)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK `core.Entity`, PROTECT | Y | | |
| delegator / delegate | FK User, PROTECT | Y | | Different users; both human |
| activities | M2M ApprovalActivity, empty = all the delegator may approve | N | | |
| amount_max | Decimal(18,2), null | N | | Within the delegator's own bands |
| valid_from / valid_to | DateTime (aware) | Y / Y | | `valid_to` required (A31) |
| reason | Char(500) | Y | Bi | |
| created_by / created_at; revoked_at / revoked_by / revoke_reason | | Y / N | | One revocation write only; rows never deleted |

### 3.10 Approval engine (added 2026-10-18, A25, option C; to build)

`main`'s `approvals` app (approvable-type registry and adapters, `ApprovalRequest` with steps, document-version hash, idempotency keys, notifications, DRF API `/api/v1/` and inbox) becomes the workflow layer for every approvable document. Its policy resolution is replaced by `AuthorityRule` (3.8); its eligibility check calls `core.access.has` / `require`; its `Delegation` is 3.9. `main`'s `EntityAccess`, `Role` and `Permission` tables are not used (integration doc §5 option C). Each request stores `entity`, `activity`, `document` (content type + id), `document_version`, `amount_basis_value`, `rule_ids` used, steps and decisions (`approved`, `rejected`, `returned`), `self_approval` (bool), `on_behalf_of` (delegator, when a delegate decides).

## 4. Rules

Enforcement: **S** = `core/access/services.py`; **M** = model `save`/`delete`/`clean`; **DB** = constraint; **Q** = queryset helper; **T** = test that scans code or data; **C** = Django system check at start-up.

**Checking rights**
1. Every service that creates, changes, approves, posts, issues, exports or shows business data calls `require(user, entity, permission)` first, for the entity that owns the record. Views and APIs call services and never check roles themselves. **[S; T: test lists every public function in `*/services.py` without `require` against an allow-list]**
2. Every view, API and admin queryset on a model with an `entity` FK goes through `scope(qs, user, permission)`, which filters to `entities_for(user, permission)`. A record in an entity outside that set is answered as "not found" (404), never 403, so its existence is not revealed. **[Q; T: test calls every registered API list/detail URL as a user of another entity]**
3. `require` grants a permission only if the user has an active membership (3.3) at check time, in that entity (or a group membership covering it, R6), whose role holds the permission. Nothing else gives rights: not `is_superuser`, `is_staff`, Django groups or `user_permissions`. **[S; C: non-superuser users with Django groups or user_permissions fail the check]**
4. Rights are checked again at every action. Nothing is cached across requests; a revocation or expiry takes effect at the next check. **[S]**
5. A right in entity A gives nothing in entity B, also when B is A's parent or child. **[S; test AT-1, AT-2]**
6. A `scope=group` role may be granted only on an entity with `consolidation=parent`. It covers that entity and its descendants with `consolidation=full` (recursive). Entities with `equity` or `none` consolidation (associates, minority holdings) need an explicit per-entity membership (OQ-3). Group-level records without an entity (ledger-fx global rates) are checked against the group parent entity. **[M clean + S; test AT-3]**
    *Replaced 2026-10-18 (A27; to build):* a `scope=group` role may be granted only on an entity with `consolidation=parent`. It gives rights **only on that entity**: its own records, group-level records without an entity (global rates) and **G** permissions checked on the group parent (R32, R48). It covers **no** descendant, whatever its consolidation: every subsidiary, partly-owned, associate or minority entity is granted separately with its own membership. **[M clean + S; test AT-3 amended, AT-63]**
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
    *Amended 2026-10-18 (A26; to build):* the per-entity switch is **withdrawn** (invoicing `segregation_required` removed). `allow_self_approval=True` is accepted only through the approval engine's top-authority path (R58); every other caller passing it is refused. Creator, submitter and last modifier stay refused for everybody else. **[S; test AT-7 amended, AT-60]**
15. For **S!** permissions `allow_self_approval` is ignored: no self-grant, no self-delegation, no approval of one's own dual-control request. **[S; test AT-15, AT-19]**

**Delegation**
16. A CFO delegate is an `approver` membership with `delegated_from` = the delegator's active approver membership, the same entity, and `valid_to` required and no later than the delegator's own `valid_to`. It is created by the delegator (`core.delegate_approval`) or by `managing_partner`. Revoking or expiring the source membership ends the delegation at the same moment. A delegate cannot delegate further. **[S; test AT-17]**
    *Replaced 2026-10-18 (A31; to build):* by R62 (general dated delegation, 3.9). Existing `delegated_from` memberships are no longer created; AT-17 is rewritten as AT-66.
17. Key custody delegation stays in `zatca.KeyCustodianDelegation` (zatca 3.9, R-41). Granting needs `zatca.grant_key_delegation` (managing_partner only); the custodian must hold an active `zatca_admin` membership in that entity; grant and revoke also write `delegation_granted` / `delegation_revoked` in `AccessLog`. ZATCA key actions need **both** `require(user, entity, "zatca.onboard")` and an active delegation. Exception (added 2026-10-16, A12; zatca R-49): an `unseal` delegation is recorded on the group parent entity, because unsealing opens the store for all entities; the custodian must hold `zatca_admin` on the group parent (this rule applied to the parent), and the ERP performs no unseal action, it only records who may do it (zatca OQ-32). **[S in zatca + core; test AT-18, AT-28]**
    *Amended 2026-10-17 (A20):* the `unseal` exception is **withdrawn — see R51** (hierarchical keys, zatca R-50). Every key custody delegation is per entity again; there is no store-wide delegation on the group parent.
18. When the user who should approve is absent, the document waits; the system never routes approval to another person on its own. **[S]**
    *Note 2026-10-18 (A25, A31):* the engine's escalation sends reminders only (en/ar); it never adds an approver. Another person approves only as an eligible holder under the matrix or as a recorded delegate (R62). Whether escalation may reroute: OQ-34.

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
    *Note 2026-10-18 (A26):* the second and last exception to R35 is the top-authority self-approval of R58 (grant row `self_approval=true`, `top_authority=true`); `_authorise_document_post` accepts it for every registered type, manual journals included, and for nothing else (to build).
    *Confirmed 2026-10-17 (A23(a); no code change):* the exception covers only a receipt with **no bank charge**, as built (`invoicing.services._receipt_creator_may_post`); a receipt with any bank charge needs a second person (invoicing R110, AT-106).
47. **Chairman role (A17).** `chairman` is a `scope=group`, `kind=human` role. At most one active `chairman` membership exists per group parent at any time (a second overlapping grant is refused); it is granted only by `managing_partner` (`core.grant_membership`, never to self, R8; default, OQ-24), and the chairman and the requesting MP are different users. **[M clean + S + DB partial unique; test AT-50]**
48. **Turning Sharia enforcement off (A17).** `Entity.sharia_enforced` True → False, for any entity of the group (any `consolidation`, the group parent included), runs only in two steps: (1) a `managing_partner` of the group opens a `DualControlRequest` for `core.disable_sharia_enforcement` on the group parent, target = the entity, payload = {entity, `sharia_enforced: False`, reason}; opening it is the MP's approval; (2) the group's `chairman` approves it (R19; the decider must hold the permission through `chairman`). Then the MP applies it through `change_entity_settings(entity, sharia_enforced=False, dual_control_request=...)`, which calls `require_dual_control` (single-use, same payload, not expired). The change must be alone in its call (no other setting with it). Without an approved request the change is refused (`dual_control`), also for an inactive subsidiary (R38, A18). Requested, decided (approved or rejected), used and the `entity_settings_changed` row are each logged in `AccessLog`, en/ar. `create_entity` with `sharia_enforced=False` needs the same approved request (target = the parent entity, payload names the new entity's code). **[S in core + access; test AT-51, AT-52]**
49. **Turning Sharia enforcement on** (False → True) stays a single step by `managing_partner` with `core.change_entity_settings` (R32), logged. **[S; test AT-51]**
50. **`zatca_admin` add-on (A19).** A user may hold `zatca_admin` and another entity role (e.g. `accountant`, `approver`) in the same entity as two memberships. Each right is checked on the membership that carries it: `invoicing.services.resubmit_to_zatca` accepts `zatca.retry_failed` only through a `zatca_admin` membership in that entity (invoicing R79), plus the ZATCA queue system user with `zatca.process_queue`. Segregation rules (R14, R35) apply to the person whatever roles they hold. **[S; test AT-53]**
51. **Per-entity key custody (A20).** A `KeyCustodianDelegation` always names one entity and covers only that entity's master key and EGS keys (zatca R-50). Granting needs `zatca.grant_key_delegation` (`managing_partner`) and an active `zatca_admin` membership of the custodian in that entity (R17). No delegation is recorded on the group parent for other entities; the group super key is not delegated through the ERP (zatca OQ-34). **[S in zatca + core; test AT-54]**

**Project-owner decisions 2026-10-17 (A21, A22)**
52. **Payroll approval (A21).** `hr_officer` and `payroll_officer` are seeded entity roles, granted only by `managing_partner` (an `access_admin` grant is refused). `payroll.approve_run` (A, P) sits on `hr_officer`, not on `approver`; the run's creator, submitter or last modifier is refused (R14, R35) whatever roles that person holds, so a user holding both roles cannot approve a run they prepared. Posting follows approval through the registry (R34). **[catalogue + S + ledger; test AT-55]**
53. **MP approves loans and bai' ajel (A22).** `payroll.approve_loan`, `payroll.approve_deferred_sale_terms` and `payroll.approve_deferred_sale` are held only by `managing_partner` (group role; covers `full` descendants, R6; `equity` / `none` entities need an explicit MP membership, OQ-3). Never the creator (R14). **[catalogue + S; test AT-56]**
    *Amended 2026-10-18 (A24, A25, A27; to build):* in Almiras the matrix rows for `payroll.loan`, `payroll.deferred_sale_terms` and `payroll.deferred_sale` name `managing_partner` (A22 stands there). In every other entity the approver is that entity's matrix row; the seed default is the entity's top authority (`ceo`), pending OQ-39. The MP approves in another entity only with an explicit membership there and only if that entity's matrix names the role (no cascade, R6).
54. **Overtime approval routing (A22).** `payroll.approve_overtime` (A) is checked with the employee's entity and `HrSettings.overtime_approval_route` (hr R55): route `managing_partner` accepts only a `managing_partner` grant; route `department_head` accepts only the user linked to the head of the employee's department, through a membership holding the permission in that entity (proposed role, OQ-28). The route is per entity data, never a code path. **[S in payroll + core; test payroll AT-28]**
    *Replaced 2026-10-18 (A25; to build):* overtime approval is the matrix activity `payroll.overtime`. Almiras: `managing_partner` (decided, A25). Other entities: their own matrix rows (decided: "other companies have their own approval structures"); the seed copies today's `HrSettings.overtime_approval_route` (`department_head`) as a default only, until each entity's matrix is supplied (OQ-28, OQ-30). `HrSettings.overtime_approval_route` is retired once the matrix is built (hr R55).

**Project owner / CFO decision pack 2026-10-17 (A23; to build)**
55. **Retention decisions (A23(b)).** New permission `core.decide_retention` (flags **A**, **S!**; human only; never **Ag**), held by `managing_partner` (default, invoicing OQ-67). It decides `RetentionNomination` rows of an entity (`burn` / `keep`, bilingual reason for `keep`), never for a record the decider created or is the data subject of (R14). No service, job, agent or system user deletes or anonymises personal data without such a decision. `AccessLog` itself is kept indefinitely; its personal-data columns are nominated like any record (OQ-10). **[catalogue + S in core; test AT-57]**
    *Amended 2026-10-18 (A32; to build):* destruction (`burn`) of records older than 10 years is decided by an **internal audit committee approval minute**. A `burn` decision is refused without `committee_minute_ref` (Char(100), the minute number) and `committee_minute_date`; both are stored on the `RetentionNomination` decision and in its `AccessLog` row. One minute may cover many nominations. `keep` needs no minute (bilingual reason, as before). The ERP user who records the decision holds `core.decide_retention` (default holder now `internal_auditor`, OQ-29); the decision is the committee's, never the recorder's own judgment, and never self-approved (S!, R58 does not apply). **[catalogue + S in core; test AT-57 amended, AT-72]**
56. **New approvals from the decision pack (A23(c)).** Added to the catalogue with flag **A** (creator ≠ approver, R14) and, where they post, **P** and the document registry (R34 to R36): `invoicing.approve_recovery_ratio` (approver), `invoicing.approve_art51_true_up` (A, P; approver), `invoicing.create_deferred_release` (Sys:billing) and `invoicing.approve_deferred_release` (A, P; approver), `invoicing.approve_deposit_application` (A, P; approver), `invoicing.approve_non_vat_document` (posting permission for sales documents of an entity without a VAT number, invoicing R101; approver), `payroll.approve_cost_item` and `payroll.approve_secondment_cost_report` (A; approver), `payroll.approve_maternity_clearance` (A; approver; legal contact per hr OQ-44). None is **Ag**. **[catalogue + seed migration; test AT-58]**

**Project-owner answers 2026-10-18 (A24 to A32; all to build)**
57. **Authority matrix (A25).** Every approvable document is approved only by users eligible under its entity's `AuthorityRule` rows for (activity, amount basis value, date): for each step, an active human membership in **that entity** whose role is in `approver_roles` and holds `activity.permission`. Bands of one activity and entity may not overlap on any date; a gap or missing row means **no rule**: submission is refused (`no_authority_rule`, en/ar, naming entity, activity and amount) and the document stays draft; nothing falls back to a group role or the MP. The seed for each entity copies today's decided holders (catalogue 3.6, A21, A22 for Almiras) as rows, marked `seed`, until the entity's own matrix is entered (OQ-30). The amount is measured in the entity's functional currency at the document's booking rate (default, OQ-35). **[S in approvals + core; DB exclusion on overlapping bands (PostgreSQL); test AT-59]**
    *Amended 2026-10-20 (§10, R70, R71):* when an entity has **no row at all** for an activity, the code default (today's behaviour) applies instead of a refusal; only a gap **inside** an entity's own bands is refused. Rows are no longer copied as seed data. Named-user approvers, band currency and the delegation flag: R72 to R74.
58. **Self-approval by the top authority only (A26).** The engine accepts a decision by a user who is the document's creator, submitter or last modifier **only** when (a) the user holds the entity's `EntityGovernance.top_authority_role` in that entity, (b) that role is an eligible approver for the step under R57, and (c) the permission is **A** (or **A, P**) and not **S!**, **DC** or **G**. It then calls `require(..., document=doc, allow_self_approval=True)`; the ledger registry accepts that grant for posting only when its `AccessLog` row has `self_approval=true` and `top_authority=true` (R35 amended). Any other self-approval is refused (`self_approval`, en/ar), for every role, including CEO in Almiras and MP outside Almiras. **[S in approvals + core + ledger registry; test AT-60, AT-61]**
59. **Monthly governance report (A26).** `core.services.governance_report(entity, month, *, user)` lists, per entity, every self-approval (document, activity, amount, user, time, grant log id), every decision by a delegate (`on_behalf_of`), every approval outside the seed rows' bands, and every `AuthorityRule` and `EntityGovernance` change in the month; en/ar; CSV/PDF export. It is generated on the 1st of each month for the previous month by a system job and readable by the roles in `governance_report_recipients` with a membership in that entity (default OQ-33). Viewing it is logged. **[svc + job; test AT-62]**
60. **New roles (A28).** `ceo`, `internal_auditor`, `personal_assistant`, `business_analyst`, `external_auditor` are seeded entity roles (3.1). `internal_auditor`, `business_analyst` and `external_auditor` hold view permissions only (never **A**, **P**, **DC**, **S!** or create). An `external_auditor` membership without `valid_to` is refused (`valid_to_required`, en/ar); its maximum length: OQ-36. `personal_assistant` holds create (draft) permissions only, never submit, approve or post. `ceo` may hold the `accountant` permissions together with **A**/**P** ones; segregation still applies to the CEO except under R58. **[catalogue + M clean + S; test AT-64, AT-65]**
61. **One approval engine (A25, option C).** Every module's approve functions (invoicing, billing, payroll, hr, banking, ledger manual journals, migration map, and every later module) are reached only through the engine: `approvals.submit(document, user)` resolves the rules (R57) and locks the document (no edit while a request is open; the ledger keeps its states `draft/posted/reversed`, no `pending_approval` status, integration doc §5); the final eligible decision calls the module's own service with the approver's single-use, document-bound grant (R30, R31); the engine never posts without that grant and never posts itself. A failure in the module service rolls back the decision. Documents approved by a person before go-live are not re-routed. **[S in approvals + every module; T: test lists every **A** permission and checks it is reached only from an engine adapter; test AT-67]**
62. **Delegation of approval authority (A31).** An `ApprovalDelegation` is created by the delegator (or by the entity's top authority) for one entity, optional activities and `amount_max`, with `valid_from` and a required `valid_to`. While active, the delegate is eligible for exactly the steps the delegator is eligible for, within `amount_max`; the decision records `on_behalf_of` = delegator. The delegate needs an active human membership in that entity; a delegate cannot delegate further; delegating to oneself is refused; grant and revocation write `delegation_granted` / `delegation_revoked` in `AccessLog`. A delegate is never the top authority for R58 (no self-approval through delegation). Whether an active delegation stops the delegator approving the same activity: OQ-32. **[S; test AT-66]**
63. **System users after prior human approval (A29).** A system user issues or posts a document only when the engine (or, for documents approved before the engine, the module) holds a used human approval for it or for the contract it comes from: `system_billing` issues invoices of an approved contract version (billing R35, D8 confirmed by billing D14(a)); `system_zatca_queue` posts on clearance with `posted_by` = that approver (R42). A system user never approves (R13). Agent and external integration users stay drafts-only (A7). **[S + catalogue; test AT-11, AT-68]**
64. **No cascade (A27).** `entities_for(user, permission)` returns only entities where the user holds a membership carrying the permission; a group-parent membership adds no other entity. Reports across entities (consolidation, group viewer, intercompany reconciliation) show only the entities where the user has memberships; the others are "not visible" (en/ar). **[S + Q; test AT-3 amended, AT-63]**
65. **Who grants memberships per entity (A27).** Because group roles do not cascade, memberships in an entity are granted by holders of `core.grant_membership` **in that entity** (default: its top authority and its `access_admin`, OQ-2). Rules that said "granted by `managing_partner` only" (R47 `chairman` on the group parent, R52 `hr_officer` / `payroll_officer`) read "by the entity's top authority" outside the group parent (default, OQ-2). **[S; test AT-69]**
66. **Custom user model (A30).** `AUTH_USER_MODEL = "access.User"`; `User.kind` (`human`, `system`, `agent`) replaces `UserProfile.kind` with the same immutability (R10); R11 to R13, R20, R21, R25 apply to `User`. TOTP for humans (`main` D14) is not decided by the owner (OQ-4). **[M + C + migration on a fresh DB; test AT-70]**
67. **Top authority setting (A26).** `EntityGovernance.top_authority_role` is set at entity creation (`create_entity`: `managing_partner` for a top-level group parent, `ceo` otherwise) and changed only through `change_entity_settings` (R32, logged before/after). An entity whose top-authority role has no active holder has no self-approval; documents wait for other eligible approvers (R18). **[S; test AT-71]**
68. **Bilingual and per entity.** Matrix rows, activity names, delegation reasons, the governance report and every new refusal message are en/ar; every query is scoped by entity and permission (R2). **[S + T; test AT-25 extended]**

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

*Amended 2026-10-18 (A24 to A31; to build).* "managing_partner (group)" above acts on the group parent only (R6 replaced): in any other entity, read "the entity's top authority (`ceo` by default)". Added rights:

| Action | Top authority of the entity | access_admin (entity) | internal_auditor | external_auditor | Any approver | System | Agent |
|---|---|---|---|---|---|---|---|
| Maintain `AuthorityRule` rows | Y (default, OQ-31) | No | No | No | No | No | No |
| Change `top_authority_role` | via `change_entity_settings` (group parent's MP, R32) | No | No | No | No | No | No |
| Self-approve an **A** document (R58) | Y, logged and reported | No | No | No | No | No | No |
| Delegate own approval authority (R62) | Y | No | No | No | Y, own only | No | No |
| Read the governance report (R59) | Y (default) | No | Y | Y while valid | No | No | No |
| Record a retention decision with the committee minute (R55) | No (default) | No | Y (default, OQ-29) | No | No | No | No |

## 8. Acceptance tests

Fixtures: entities ALM (parent), SUB (child, `full`, 100%), PART (child, `full`, 60%), MIN (child, `equity`, 25%); users per role; system users `billing`, `zatca_queue`, `fx_import`; agent user `agent:invoice_reader`. All anonymised; no real names or identifiers.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | U is `accountant` in ALM only | U lists invoices and opens a SUB invoice by id | List has ALM only; SUB detail returns 404; `require(U, SUB, "invoicing.view")` raises `AccessDenied`; `permission_denied` logged (R2, R5) |
| AT-2 | U is `approver` in SUB | U approves an ALM bill | Refused; bill stays `submitted` (R5) |
| AT-3 | G is `group_viewer` on ALM | G views ALM, SUB, PART, MIN trial balances | ALM, SUB, PART allowed; MIN refused until a per-entity `viewer` membership exists (R6). *Amended 2026-10-18 (A27, R6 replaced, R64):* ALM allowed; SUB, PART and MIN all refused until each has its own `viewer` membership for G |
| AT-4 | U is `accountant` in ALM, `valid_to` = yesterday | U creates a draft invoice | Refused (expired membership, R3) |
| AT-5 | U's membership `valid_from` = tomorrow; another membership revoked 1 min ago | U acts in each entity | Both refused; next request after revoke refused (R3, R4) |
| AT-6 | C (`accountant` + `approver` in ALM) created and submitted invoice X; segregation on | C approves X | Refused; X stays `submitted` (R14) |
| AT-7 | Same, ALM `InvoicingSettings.segregation_required = False` | C approves X | Allowed; grant logged with `self_approval=true` (R14). *Amended 2026-10-18 (A26):* the switch no longer exists; C refused (`self_approval`) unless C holds ALM's top-authority role (AT-60) |
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
| AT-56 | *Added 2026-10-17 (A22, R53).* Loan drafted by P in ALM; C (`approver`); MP; MIN (`equity`) loan | C approves; MP approves; MP approves the MIN loan without a MIN membership | C refused; MP allowed; MIN refused until an explicit MP membership exists (payroll AT-29). *Amended 2026-10-18 (R53 note):* a SUB loan is approved per SUB's matrix (seed: SUB's `ceo`); MP refused in SUB without a SUB membership named by SUB's matrix |
| AT-57 | *Added 2026-10-17 (A23(b), R55).* Nomination N for a customer of ALM; MP M; approver C; agent G; the nomination system user | C decides N; G decides N; the system user decides N; M decides `keep` without a reason; M decides `burn` | C, G and the system user refused (`permission_denied` rows); `keep` without reason refused; `burn` applied and logged in `AccessLog` with M, entity and record. *Amended 2026-10-18 (A32):* `burn` without a committee minute reference refused (AT-72) |
| AT-58 | *Added 2026-10-17 (A23(c), R56).* Catalogue after migration | Scan the new codenames' flags and holders; an agent role given one of them | Each has **A** (and **P** where it posts), none **Ag**; the agent role save refused (R12) |

*Added 2026-10-18 (A24 to A32; all to build).* Extra fixtures: `EntityGovernance` ALM → `managing_partner`, SUB → `ceo`; users MP (managing_partner on ALM), CE (`ceo` SUB), CF (`approver` SUB), CA (`ceo` ALM), IA (`internal_auditor` SUB), EA (`external_auditor` SUB), PA (`personal_assistant` SUB), AC (`accountant` SUB). SUB matrix (anonymised fixture): `invoicing.bill` 0.00–50,000.00 → step 1 `approver`; 50,000.01+ → step 1 `approver`, step 2 `ceo`; `payroll.run` → `hr_officer`; `payroll.overtime` → `ceo`. ALM: `payroll.overtime` → `managing_partner`; `payroll.run` → `hr_officer`.

| # | Given | When | Then |
|---|---|---|---|
| AT-59 | SUB matrix above; AC submits bill B1 20,000.00 and B2 80,000.00; activity `invoicing.vendor_payment` has no SUB row | CF approves B1; CF approves B2; CE approves B2; AC submits a vendor payment | B1 posted on CF's approval; B2 needs both steps: after CF, still open; after CE, posted (one grant per step, final one posts); the payment submit refused `no_authority_rule` (en/ar), payment stays draft (R57, R61) |
| AT-60 | CE creates and submits SUB bill B3 30,000.00; SUB matrix adds `ceo` to the 0–50,000.00 step | CE approves B3; CF creates and submits B4, then approves it; CA (`ceo` in ALM, top authority is MP) creates and approves an ALM bill | B3 posted, grant row `self_approval=true`, `top_authority=true`; B4 refused (`self_approval`); CA refused (not ALM's top authority) (R58) |
| AT-61 | MP creates and submits an ALM manual journal; ALM matrix names `managing_partner` for `ledger.manual_entry`; MP holds no SUB membership | MP approves it; MP submits a dual-control Sharia-off request and decides it; MP self-grants a membership; MP approves a SUB document | Journal posted, self-approval logged; DC self-decision refused (R15, R19); self-grant refused (S!, R8); SUB refused (no membership, R64) |
| AT-62 | AT-60 and AT-61 done in March 2027; a delegate decision exists | Job runs 2027-04-01; IA, EA, AC open SUB's report | SUB report lists B3 with user, amount, time and log id, and the delegate decision with `on_behalf_of`; ALM report lists MP's journal; IA and EA (valid) allowed, each view logged; AC refused; Arabic and English labels (R59) |
| AT-63 | MP on ALM (group parent); `group_viewer` G on ALM | MP lists SUB invoices; G runs the intercompany reconciliation for the group | MP sees none in SUB (no membership); G sees ALM only, SUB and PART sides "not visible" (en/ar) (R6, R64) |
| AT-64 | EA membership with `valid_to` 2027-06-30; a grant of `external_auditor` with no `valid_to` | EA lists SUB trial balance on 2027-06-01 and on 2027-07-01; EA tries to create a draft bill; grant without `valid_to` | June allowed; July refused (expired); draft refused; grant refused `valid_to_required` (R60) |
| AT-65 | PA in SUB | PA creates a draft bill with an attachment; submits it; approves one | Draft allowed (`created_by` = PA); submit and approve refused (R60) |
| AT-66 | CF delegates `invoicing.bill` up to 10,000.00 to AC from 2027-03-01 to 2027-03-15 | AC approves a 5,000.00 bill created by PA on 03-10; a 20,000.00 bill on 03-10; a bill on 03-16; AC delegates further; CF delegates without `valid_to`; CF delegates to self | 5,000.00 allowed, `on_behalf_of` = CF, `delegation_granted` logged earlier; 20,000.00 refused (over `amount_max`); 03-16 refused; further delegation, no `valid_to` and self-delegation refused (R62) |
| AT-67 | Code scan; a module function approving an **A** document without an engine request | Run the scan; call the function directly | Every **A** permission is reached only through an engine adapter; the direct call is refused (`approval_request_required`, en/ar) (R61) |
| AT-68 | Contract approved by CF (engine); `system_billing` and `system_zatca_queue` with SUB memberships | Run billing; ZATCA fake `cleared`; `system_billing` calls approve on a bill | Invoice issued by the system user with `approved_by` = CF; posted on clearance with `posted_by` = CF; the approve call refused (R13, R63) |
| AT-69 | SUB without an MP membership; CE (top authority SUB) | CE grants AC `hr_officer` in SUB; MP grants someone `hr_officer` in SUB | CE allowed, logged; MP refused (no membership in SUB) (R65) |
| AT-70 | Fresh database | Run migrations; check `AUTH_USER_MODEL`; create a system user and try to change its `kind` | `access.User` in use; all FKs resolve; `kind` change refused (R66, R10) |
| AT-71 | NEW created under ALM as data; SUB's `ceo` membership revoked | Read NEW's `top_authority_role`; CE's former document approved by CE after revocation; MP changes SUB's top authority to `approver` | NEW = `ceo`; refused (no active membership); change applied with an `entity_settings_changed` row (R67) |
| AT-72 | Nominations N1, N2 of SUB older than 10 years; IA records decisions | `burn` N1 without `committee_minute_ref`; `burn` N1 and N2 citing minute "IAC-2027-03" dated 2027-03-15; `keep` N3 with a reason and no minute | First refused (en/ar); N1 and N2 burned, each decision and `AccessLog` row holds the minute reference and date; `keep` accepted (R55) |

## 9. Open questions

**B** = blocks building; **GL** = blocks go-live; **D** = a default exists, so building can go ahead with it.

| # | Question | Who | Blocks? | Default |
|---|---|---|---|---|
| OQ-1 | Who approves when the CFO is absent or is the creator? (invoicing OQ-42) Is the Managing Partner fallback approver in every covered entity, including partly-owned ones? | Managing Partner | D | Named delegate per entity (R16); `managing_partner` holds **A** permissions in entities covered by R6. **Resolved by A25, A31 (2026-10-18):** approvers per the entity's matrix (R57); absence covered by dated delegation (R62); no MP fallback and no cascade |
| OQ-2 | Who grants memberships per entity? May an `access_admin` grant `approver` and `zatca_admin`? | Managing Partner | D | MP grants all roles; `access_admin` grants every entity role except `access_admin`, `approver`, `zatca_admin`. *Amended 2026-10-17 (A21):* `hr_officer` and `payroll_officer` are granted by the MP only (decided). *Amended 2026-10-18 (A27, R65):* with no cascade the MP has no membership outside the group parent; new default: in each entity, its top authority and its `access_admin` grant (the `access_admin` not `ceo`, `approver`, `access_admin`, `zatca_admin`, `external_auditor`); "MP only" grants (A21) read "the entity's top authority" outside Almiras. Owner to confirm |
| OQ-3 | Which entities do group roles (`managing_partner`, `group_viewer`, `group_treasury`) cover? Partly-owned and minority entities are separate companies (A4); other shareholders may limit what group staff may see. | Managing Partner + legal | **GL** for partly-owned/minority entities with real data | `full` descendants only; `equity`/`none` need explicit membership. **Resolved by A27 (2026-10-18):** no entity is covered by a group role; each is granted separately (R6, R64) |
| OQ-4 | Is MFA required, and for whom? | Managing Partner | GL | TOTP (open source, e.g. `django-otp`) required for `approver`, `zatca_admin`, `access_admin`, `managing_partner` and superusers |
| OQ-5 | Manual journals: ledger-fx §7 lets the Accountant post directly; A1 says the CFO approves and posting follows. Which applies to manual and FX journals, revaluation and reversals? | Managing Partner / CFO | D | A1: `ledger.post_entry` = approver, not own; revaluation run by accountant creates drafts for approval |
| OQ-6 | zatca's "Sales user" is mapped to `receivables_clerk`. Correct? | Managing Partner | D | Yes |
| OQ-7 | Viewer/auditor: may they export the ZATCA archive (zatca §7 yes), even though B2C XML holds personal data that invoicing masks for viewers (R51)? Separate `external_auditor` role? | Managing Partner + legal (PDPL) | D | Export allowed only with `invoicing.view_personal_data`; one `viewer` role. *Partly resolved by A28 (2026-10-18):* a separate `external_auditor` role exists (read-only, time-limited, R60). Export and personal-data access for it: OQ-36 |
| OQ-8 | Memberships on an inactive entity: read-only or nothing? | Accountant | D | View permissions still work; ledger already blocks posting. *Amended 2026-10-16 (A14):* plus **I** permissions, for reactivation only (R38) |
| OQ-9 | Keep `auth.User` + `UserProfile`, or swap to a custom user model before the first production migration? | Dev | D | Keep `auth.User` + profile. **Resolved by A30 (2026-10-18):** custom `access.User` (R66). Still open (Dev): fold the profile fields into `User` (default) or keep a OneToOne profile |
| OQ-10 | AccessLog retention, who may read it, DB-level UPDATE/DELETE revoke (PDPL: it holds personal data). | Managing Partner + legal | GL | Keep 10 years; readable by MP and `access_admin` (own entity); DB grants revoked by migration. *Amended 2026-10-17:* the database-role part is no longer open: it is go-live requirement R45 (no trigger bypass, no arbitrary DDL; AT-47). Retention and readers stay open here. **Retention resolved by A23(b) (2026-10-17):** kept indefinitely; personal data in it nominated after 10 years for a human decision (R55). Readers stay open |
| OQ-29 | *Added 2026-10-17 (A23(b), (c)).* Who holds `core.decide_retention` (MP only, or also legal or an entity approver), and who records "written legal clearance" for maternity pay (no legal role exists; hr OQ-44)? | Managing Partner | D | `managing_partner` for both. *Amended 2026-10-18 (A32):* the retention decision itself is the internal audit committee's minute; new default recorder: `internal_auditor` of the entity (MP of the group parent for Almiras records if no internal auditor exists). Legal-clearance recorder for maternity: unchanged default |
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
| OQ-28 | *Added 2026-10-17 (A22).* Overtime in non-Almiras companies is approved by the department head: is a `department_head` entity role (only `payroll.approve_overtime` for the department the user heads) acceptable, who grants it, and what if the head is absent or it is their own overtime (hr OQ-30)? | Managing Partner | D | Proposed role as 3.1, granted by `managing_partner`; absent → request waits (R18); own overtime → `managing_partner`. *Amended 2026-10-18 (A25):* Almiras: MP (decided). Other entities: their own matrix (decided); which role each names is for that entity's CEO (OQ-30); seed default `department_head` stays, granted by the entity's top authority (R65); own overtime → the next step in the matrix, never self (R58 applies only to the top authority) |
| OQ-30 | *Added 2026-10-18 (A25).* The content of each entity's authority matrix: activities, amount bands and approver roles (Almiras from the MP; each other company from its CEO / GM). | Project owner + each CEO | **GL** per entity | Seed rows copy today's decided holders (R57); Almiras overtime, loans, bai' ajel → MP; payroll runs → `hr_officer` everywhere |
| OQ-31 | *Added 2026-10-18 (A25).* Who may create or change an entity's `AuthorityRule` rows, and must a second person (or the board) approve the change? | Project owner | D | The entity's top authority only, logged, listed in the governance report (R59); no second approval |
| OQ-32 | *Added 2026-10-18 (A26, A31).* Conditions on top-authority self-approval: only when no other eligible approver exists (`main` D10), or always? Does an active delegation stop the delegator self-approving or approving the delegated activity (`main` D16)? | Project owner | D | Always allowed when eligible (R58); an active delegation does not stop the delegator |
| OQ-33 | *Added 2026-10-18 (A26).* Who receives the monthly governance report (internal auditor, board, the Almiras MP as shareholder representative, external auditor) and is it also sent outside the ERP? | Project owner | D | `internal_auditor` and the entity's top authority, in-app only; the MP sees a subsidiary's report only with a membership there |
| OQ-34 | *Added 2026-10-18 (A25).* `main`'s engine escalates overdue requests. Reminder only (R18), or may the matrix name an escalation approver after N days? | Project owner | D | Reminder only; no rerouting |
| OQ-35 | *Added 2026-10-18 (A25).* Amount bands: which amount (gross or net of VAT; contract total, annual value or monthly amount for contracts; run total for payroll), and conversion of foreign-currency documents. | Project owner / CFO | D | Document total gross of VAT in functional currency at the booking rate; contracts: total value over the term (12 months for open-ended); payroll run: total net pay |
| OQ-36 | *Added 2026-10-18 (A28).* External auditor: maximum membership length, access to personal data (masked or not), exports (ZATCA archive, CSV), and who grants the membership. | Project owner + legal (PDPL) | D | `valid_to` required, no maximum; personal data masked; no exports; granted by the entity's top authority |
| OQ-37 | *Added 2026-10-18 (A28).* Exact permission sets of `personal_assistant` (whose drafts, which modules, uploads to which documents) and `business_analyst` (which modules and reports). | Project owner | D | PA: create drafts in invoicing (bills), billing (contract drafts) and attachments; BA: `*.view` of ledger, invoicing, billing reports, no personal data |
| OQ-38 | *Added 2026-10-18 (A27).* **G** permissions (entity settings, Sharia switch-off, global rates, users) are checked on the group parent and so act on every entity of the group. Is that compatible with "each company granted separately", or must each entity's settings also need a right in that entity? | Project owner | D | As built: G rights on the group parent (governance acts, not business rights); listed in the governance report |
| OQ-39 | *Added 2026-10-18 (A24 vs A22).* A22 (2026-10-17) said loans and bai' ajel are "all approved by the Managing Partner"; A24 puts authority in each company with its CEO. In subsidiaries, who approves loans and bai' ajel? | Project owner | D | Almiras: MP; other entities: their top authority (`ceo`) until their matrix says otherwise |
| OQ-40 | *Added 2026-10-18 (A26 vs A16).* "No other role may ever self-approve": does this withdraw A16 (the recorder posts their own receipt without a bank charge)? | Project owner | D | No: posting a receipt is not an approval step (no matrix row); A16 stands |
| OQ-41 | *Added 2026-10-18 (A24).* Where both a CEO and a CFO (`approver`) exist in an entity, does the CFO keep any approval by default before the matrix is entered? | Project owner | D | Yes: seed rows keep the `approver` holders of 3.6, with `ceo` added as a parallel eligible role |

*Update 2026-10-18 (A24 to A32):* resolved OQ-1, OQ-3, OQ-9 (user model), OQ-7 in part; amended OQ-2, OQ-28, OQ-29; new OQ-30 (GL per entity: matrix content) to OQ-41 (defaults).
**Blocking:** OQ-13 blocks wiring zatca onboarding permissions (not the access module itself). **Go-live:** OQ-30 (each entity's matrix, added 2026-10-18), OQ-4, OQ-10, and requirement R45 (application database role privileges, AT-47; added 2026-10-17). This spec also unblocks invoicing OQ-25 (views/APIs) once built.

## 10. Authority matrix, top-authority self-approval and governance report (to build)

*Built 2026-10-20 by the module-builder agent: see 10.10 "As built" for what was built, where, and the decisions taken while building.*

*Added 2026-10-20 by the spec-writer agent.* Sources: `docs/approvals/owner-answers-2026-10-18.md` A1 to A5, A10, A13, G3, G8; this spec's A24 to A32, 3.8 to 3.10, R57 to R68, OQ-28 to OQ-41; `docs/specs/payroll.md` §10.4 (still not built: "the authority matrix, top-authority self-approval and the governance report (access build); the `ceo` role"). Nothing in §10 is built. It extends §§3.8 to 3.10 and R57 to R68; where they differ, §10 applies.

### 10.1 Purpose

Each entity (Almiras, every subsidiary, partly-owned or minority entity, and any entity added later as data) has its own **authority matrix**. The matrix says who approves each activity or document type, optionally by amount band and currency, and whether a delegate may act. Where an entity has no row for an activity, today's coded behaviour applies. Every approvable document goes through one approval engine. Only the entity's top authority may approve their own document, and only for activities where no law or accounting rule requires separation of duties. Every self-approval, delegation and matrix change is logged and appears in a monthly bilingual governance / internal-audit report. Matrix changes are versioned and need dual control. Rights are granted per entity only. Group-level settings stay group-level. Nothing here relaxes Sharia, period, balance or entity checks (R7).

### 10.2 Decisions

| # | Decision (owner's words condensed) | Source | Date |
|---|---|---|---|
| A1 → A24 | Authority in each company lies with its CEO or General Manager. The Almiras Managing Partner sits on the board or acts as shareholder representative only. | Owner | 2026-10-18 |
| A2 → A25 | The MP does not approve everything. Each company has its own authority matrix for every task and activity. "Contracted payroll needs no MP approval; overtime in Almiras needs the MP; other companies have their own approval structures." | Owner | 2026-10-18 |
| A3 → A26 | Self-approval only by the highest authority of each entity: the MP in Almiras, the CEO in every other company. "No other role may ever self-approve." Every self-approval is logged and listed in the monthly governance / internal-audit report. | Owner | 2026-10-18 |
| A4 → A27 | Each company is granted separately. Group roles do not cascade. | Owner | 2026-10-18 |
| A5 → A28 | The CEO can approve, post and act as accountant. Roles also needed: internal audit, personal assistant, business analyst, external auditor. | Owner | 2026-10-18 |
| A10 → A25 | All approvable documents go through the same approval engine. | Owner | 2026-10-18 |
| A13 → A31 | The CEO approves and reviews, and may delegate this to another user. | Owner | 2026-10-18 |
| G3 | VAT returns keep three different people (preparer, reviewer, approver). Top-authority self-approval (A3) does **not** apply to VAT returns. | Owner, on team recommendation | 2026-10-18 |
| G8 | Group-level settings (global exchange rates, the Sharia switch under dual control) stay group-level. Everything else is granted per company. | Owner, on team recommendation | 2026-10-18 |
| T1 | Team design, adopted under the owner's standing instruction "go with your recommendations" (G, 2026-10-18; I1, 2026-10-19). Owner to confirm. (a) Where no matrix row exists, today's coded behaviour applies. (b) Matrix changes are versioned and need dual control (replaces the OQ-31 default "no second approval"). (c) Delegations always expire. (d) The activities where self-approval is never allowed are flags in code, not data. | Coordinator brief | 2026-10-20 |

### 10.3 Data model

Every model has an `entity` FK to `core.Entity` (PROTECT), except the global catalogue `ApprovalActivity` (3.8). All models are append-only except where stated. User-facing text is en/ar.

**`AuthorityMatrixVersion`** (new; one entity has many versions)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | |
| version_no | PositiveInt | Y | | 1..n per entity; unique (entity, version_no) |
| status | Choice `draft`, `submitted`, `approved`, `rejected`, `withdrawn`, `superseded` | Y | | Only `draft` rows can be edited (R76) |
| effective_from | Date | Y | | No earlier than the approval date (R77) |
| summary_en / summary_ar | Text | Y | Y | What changes and why |
| rows_hash | Char(64) | Y at submit | | SHA-256 of the version's rows. It is the `DualControlRequest.payload_hash` |
| dual_control_request | FK DualControlRequest, null | Y at submit | | Permission `core.approve_authority_matrix` |
| proposed_by / proposed_at | FK User / DateTime | Y | | |
| decided_by / decided_at | FK User / DateTime | N | | Written from the DC decision. `decided_by` ≠ `proposed_by` (R19) |
| superseded_by | FK self, null | N | | Set when a later version takes effect |

**`AuthorityRule`** (3.8, amended): add `version` (FK AuthorityMatrixVersion, PROTECT, required). `valid_from`/`valid_to` come from the version's period and are no longer set per row. Added fields:

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| version | FK AuthorityMatrixVersion | Y | | Rows of a non-draft version never change (DB trigger on PostgreSQL) |
| band_currency | Char(3), null | N | | Null = the entity's functional currency (SAR). Otherwise the row applies only to documents in that transaction currency, measured in it (R73) |
| amount_from / amount_to | Decimal(18,3) | Y / N | | Decimal places checked against the band currency's own decimals (CLAUDE.md rule 7). Null bands on both = any amount |
| approver_roles | M2M Role | one of | | Any eligible holder (R57) |
| approver_user | FK User, null | one of | | Named user. Exactly one of `approver_roles`/`approver_user` is set (R72) |
| delegation_allowed | Bool, default True | Y | | False = a delegate is not eligible for this step (R74) |
| note_en / note_ar | Char(300) | N | Y | e.g. "Board resolution 2027/04" |

**`EntityGovernance`** (3.8, amended; changes only through `change_entity_settings`, R32)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| top_authority_role | FK Role | Y | | Default `managing_partner` for the group parent (Almiras), `ceo` for every other entity (R67) |
| self_approval_enabled | Bool, default True | Y | | Per-entity data. False = nobody self-approves in that entity (R79) |
| governance_report_recipients | M2M Role | Y | | Default `internal_auditor` and the top-authority role (OQ-33) |

**`ApprovalDelegation`** (3.9, amended): `valid_to` required and at most `valid_from` + 90 days (OQ-45). Add `notified_expiry_at` (DateTime, null, written once by the reminder job) and `used_count` (derived, not stored; read from approval decisions).

**`AssistantAssignment`** (new; links a `personal_assistant` to the principal they draft for)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | |
| assistant / principal | FK User | Y | | Both must hold active human memberships in the entity: the assistant as `personal_assistant` |
| valid_from / valid_to | DateTime | Y / Y | | |
| created_by, reason, revoked_* | | Y / N | | Created by the principal or the entity's top authority |

Documents drafted by an assistant store `drafted_for` (FK User, null) on the approval request. For segregation the principal counts as a creator (R81).

**`ApprovalRequest`** (3.10, amended): add `matrix_version` (FK, null when `rule_source = default`), `rule_source` (`matrix` / `default`), `self_approval` (bool), `top_authority` (bool), `drafted_for`. The rules are resolved and stored at submission (R77).

**`GovernanceReport`** (new; one per entity and month; immutable once generated)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | |
| month | Date (1st of month) | Y | | Unique (entity, month, revision) |
| revision | PositiveSmallInt | Y | | 1, or higher when re-issued (R85) |
| generated_at / generated_by | DateTime / FK User | Y | | `system_governance` system user |
| content | JSON | Y | | Snapshot of the sections in R84 |
| content_hash | Char(64) | Y | | SHA-256 of `content` |
| reviewed_by / reviewed_at / review_note_en / review_note_ar | FK User / DateTime / Text | N | Y | Written once by an `internal_auditor` (R86) |

**Catalogue (code, 3.6) additions**

| Codename | Flags | Holders (seed) |
|---|---|---|
| `core.propose_authority_matrix` | | top-authority role of the entity, `access_admin` (OQ-43) |
| `core.approve_authority_matrix` | DC, S!, NSA | see R76 (OQ-42) |
| `core.view_governance_report` | | roles in `governance_report_recipients`, `external_auditor` while valid (OQ-33) |
| `core.review_governance_report` | | `internal_auditor` |
| `core.generate_governance_report` | Sys:governance | `system_governance` (new system user, entity-scoped memberships) |
| `core.manage_assistant_assignment` | S! | top-authority role, the principal for their own assistant |

New flag **NSA** = never self-approved. It is a code flag on a catalogue permission (like **S!**) and cannot be changed in admin (R80). **S!**, **DC** and **G** imply **NSA**.

### 10.4 Rules

Enforcement codes as in §4. "Engine" = the approval engine of 3.10.

**Matrix**
69. **Per-entity matrix rows.** A row names: entity, activity, optional amount band and band currency, step number, minimum approvals, and **either** approver role(s) **or** one named user, plus `delegation_allowed`. A row can name only roles or users that have, or could hold, a membership **in that entity**. A role or user of another entity grants nothing (R5). **[M clean + S; test AT-73]**
70. **Code default where no row exists.** If the effective matrix version of an entity has no row for an activity, the engine uses the **code default** for that activity (`core/access/authority_defaults.py`, 10.5 table). The default reproduces today's behaviour: the seeded holders of the activity's **A** permission in that entity, plus each module's own extra checks (e.g. payroll runs: `hr_officer` only, never the MP; Almiras overtime, loans and bai' ajel: `managing_partner`; VAT return: three people). Since there is no cascade (R64), the MP fallback of 3.6 applies only in the group parent. The request records `rule_source = default`. The governance report lists such approvals (R84). **[S; T: every activity in the catalogue has a code default; test AT-74]**
71. **Gaps inside explicit bands are refused.** If an entity has rows for an activity but none covers the document's amount (or currency) on the submission date, submission is refused (`no_authority_rule`, en/ar, naming entity, activity and amount). The code default does **not** fill a gap. Bands of one activity, currency and version may not overlap. **[S + DB exclusion; test AT-75]**
72. **Named-user approver.** A named user is eligible only while they hold an active human membership in the entity with the activity's permission. If they lose it, the step has no eligible approver: the request waits (R18), and the top authority and the `access_admin` get a bilingual alert. Nobody else is substituted automatically. **[S; test AT-76]**
73. **Band currency.** A row with `band_currency` = X (not the functional currency) applies only to documents whose transaction currency is X. The amount is compared in X, at up to 3 decimals, checked against X's own decimals. Documents in any other currency use the functional-currency rows, with the amount converted at the document's booking rate (OQ-35 default). If an X-currency document finds no X rows, it uses the functional-currency rows. **[S; test AT-77]**
74. **Delegation flag.** When `delegation_allowed = False`, an `ApprovalDelegation` does not make the delegate eligible for that step. The delegator must decide in person. **[S; test AT-78]**
75. **One person per request.** A person decides at most one step of one approval request, as approver or as delegate. `min_approvals > 1` needs that many different persons. **[S; test AT-79]**

**Matrix versions and dual control**
76. **Dual control on every change.** Matrix rows change only through a new `AuthorityMatrixVersion`. A holder of `core.propose_authority_matrix` in the entity drafts it and submits it. Submitting opens a `DualControlRequest` for `core.approve_authority_matrix`, with `payload_hash = rows_hash`. A **different** person holding that permission in the entity approves it. Recommended deciders (OQ-42): when someone other than the top authority proposes, the entity's top authority decides. When the top authority proposes: in Almiras the group's `chairman`; in other entities a `board_representative` membership in that entity. Self-decision is refused (S!, R15). Rejecting it leaves the current version in force. The same applies to changes of `EntityGovernance.top_authority_role` and `self_approval_enabled` (R32 + R19). **[S + DC; test AT-80, AT-81]**
77. **Versioned, never retroactive.** An approved version takes effect on `effective_from`, which is on or after the approval date. The previous version becomes `superseded` from that date. Rows of a non-draft version never change (model + PostgreSQL trigger). An approval request resolves its rules at **submission** and stores `matrix_version` and `rule_ids`. A later version does not re-route open requests (OQ-46). Returned and resubmitted documents resolve again. **[M + DB + S; test AT-82]**
78. **Logged.** Drafting, submitting, approving, rejecting, withdrawing and the version taking effect each write an `AccessLog` row (`authority_matrix_*` events). The row holds before/after rows (diff) and the DC request id. **[S; test AT-80]**

**Self-approval**
79. **Only the top authority, configured as data.** A decision by the document's creator, submitter, last modifier or `drafted_for` principal is accepted **only** under R58 and only if all of these hold: (a) the user holds the entity's `top_authority_role` in that entity; (b) `self_approval_enabled` is True; (c) the user is eligible for that step under the matrix or the code default; (d) the activity's permission is not **NSA**; (e) the user is not the **beneficiary** of the document (R82). Every other self-approval is refused (`self_approval`, en/ar) for every role, including the CEO in Almiras, the MP outside Almiras and any delegate. If the top-authority role has more than one active holder in the entity, none of them may self-approve (OQ-47). **[S in engine + core + ledger registry; test AT-83, AT-84]**
80. **Never self-approved (NSA): the exclusion list.** Self-approval is refused, also for the top authority, for every activity in this list. The list is held in code as the **NSA** flag; it is not data.

| # | Activity (permission) | Why separation of duties is required | Status |
|---|---|---|---|
| a | VAT return review and approval (`tax.review_return`, `tax.approve_return`) | Three different people | **Decided (G3)** |
| b | Tax payments, refunds and import VAT (`tax.approve_payment`), tax settings (`tax.change_settings`), VAT box verification (`tax.maintain_rates` verify; H3 proposer ≠ verifier) | Same VAT process as G3; H3 gives proposer and verifier to two different people | Recommended (OQ-48) |
| c | Every **DC** permission: Sharia switch-off (`core.disable_sharia_enforcement`), ZATCA production switch (`zatca.switch_to_production`), matrix versions (`core.approve_authority_matrix`) | Dual control means two people by definition (R19, A17) | Decided (A17, A9) |
| d | Every **S!** permission: memberships (`core.grant_membership`), approval delegation (`core.delegate_approval`), key custody (`zatca.grant_key_delegation`, `zatca.onboard`), retention decisions (`core.decide_retention`), bank accounts and their changes (`banking.approve_bank_account`), reopening a reconciliation (`banking.reopen_reconciliation`), charity recipients (`invoicing.approve_charity_recipient`), personal-data export (`hr.export_personal_data`), assistant assignments | Already "segregation that cannot be switched off" (R15); a retention burn needs the internal audit committee minute (A32); charities need the board (C5) | Decided (existing S!) |
| e | Every **G** permission (entity settings, users, global rates, functional currency, statutory parameter verification `hr.verify_statutory_parameter`, non-publication day verification) | Group-level settings stay group-level (G8); they are not entity approvals | Decided (G8) for the scope; NSA recommended |
| f | Employee IBAN changes (`hr.approve_iban_change`, second person) | Payment-fraud control on bank details; same reason as banking S! | Recommended (OQ-48) |
| g | Purification: funding (`invoicing.approve_purification_funding`) and payout (`invoicing.approve_purification_payout`) | Sharia control: "funding needs the CFO's approval, never its recorder" (C2); money leaves the company irrevocably (C6) | Recommended (OQ-48) |
| h | Migration account map (`migration.approve_account_map`) | Opening balances must be shown to tie out to the legacy system for the external auditor | Recommended (OQ-48) |
| i | Bank reconciliation approval (`banking.approve_reconciliation`) | Classic separation: the preparer of a reconciliation does not approve it | Recommended (OQ-48) |
| j | Any document where the approver is the beneficiary (R82) | Conflict of interest; payroll R39/R50 | Recommended (R82) |

**[catalogue + S; T: every permission in rows a to i carries NSA; test AT-85]**
81. **Assistant drafts count as the principal's.** When a `personal_assistant` drafts a document under an active `AssistantAssignment`, the principal counts as a creator for R14, R35 and R79. A principal who approves a PA-drafted document is self-approving: allowed only as top authority and logged; refused for anyone else. **[S; test AT-86]**
82. **No approval for one's own benefit.** Nobody, including the top authority, approves a document of which they are the beneficiary or data subject: own payslip line, overtime, leave, loan, bai' ajel, final settlement, cost item, employment contract or assignment, own IBAN, own expense or vendor record, own membership or delegation. Such a document goes to the next eligible approver under the matrix. If there is none, it waits (R18). **[S in engine with each module's `beneficiary_of(document)` adapter; test AT-87]**
83. **Every self-approval logged.** A self-approval writes an `AccessLog` row (`self_approval`) with entity, activity, document type and id, document version, amount and currency, matrix version or `default`, rule id, user, role and time. The single-use grant row carries `self_approval=true`, `top_authority=true` (R58). An attempted self-approval that is refused writes a `permission_denied` row with reason `self_approval`. **[S + M; test AT-83]**

**Governance report**
84. **Monthly, per entity, bilingual.** On the 1st of each month at 02:00 Riyadh time, `system_governance` generates one report per active entity for the previous month (R59). Each report has the same sections in English and Arabic: (1) self-approvals (R83); (2) delegations granted, revoked, expired and every decision made under a delegation (`on_behalf_of`); (3) matrix versions drafted, submitted, approved, rejected and taking effect, with the diff, plus changes to the top authority and `self_approval_enabled`; (4) approvals under code defaults (`rule_source = default`); (5) refused self-approval attempts; (6) assistant assignments; (7) for the group parent only, G actions (entity settings, Sharia switch requests and decisions, global-rate overrides). A month with no events produces a report saying "nothing to report / لا يوجد ما يُبلَّغ عنه". Exports are CSV and PDF. **[S + job; test AT-88, AT-89]**
85. **Immutable.** A generated report never changes (`content_hash`). If it needs correcting (e.g. a late-committed event), a new `revision` is issued that names the earlier one. **[M + DB; test AT-90]**
86. **Readers and review.** The report is readable only by users with `core.view_governance_report` **in that entity** (no cascade). An `internal_auditor` of the entity records the review once, with a bilingual note. Each view and export is logged. If a report is not reviewed within 30 days, the top authority gets an alert. **[S; test AT-89]**

**Delegation (A13 → A31)**
87. **Delegation always expires.** An `ApprovalDelegation` (R62) needs `valid_to`, at most 90 days after `valid_from` (OQ-45). It ends at `valid_to` without any job running: eligibility is checked at decision time. The delegator and the delegate get a bilingual reminder 3 days before expiry. A renewal is a new delegation, logged. Revoking the delegator's membership ends the delegation at once. **[S + job (reminder only); test AT-91]**
88. **Delegation limits.** A delegate gets no more than the delegator holds in that entity on the decision date: same steps, within `amount_max`, never for steps with `delegation_allowed = False`, never self-approval (R62, R79), never **NSA** permissions in row c or d of R80. A delegate must not be a `personal_assistant`, `business_analyst`, `internal_auditor` or `external_auditor` membership holder only, nor a system or agent user. **[S; test AT-91, AT-92]**

**Roles, per-entity grants and group-level settings**
89. **Rights of the new roles (A5 → A28).** Rights per 10.7, R60 and 3.1. `internal_auditor` and `external_auditor` are **read-only** on business data: they never create, submit, approve, post or reverse. The internal auditor's only writes are the governance-report review (R86) and recording a retention decision with the committee minute (R55, OQ-29), which records the committee's decision and is not a business approval (OQ-50). An `external_auditor` membership needs `valid_to`, at most 12 months (OQ-36 amended). `business_analyst`: view and reports only, no personal data. `personal_assistant`: drafts and attachments only. `ceo`: the accountant's create/submit rights plus **A**/**P** as the matrix names it. Agents can never hold any of these roles (R11). **[catalogue + M clean + S; test AT-93]**
90. **Per entity only; group-level stays group-level (A4, G8).** Matrix versions, rules, delegations, assistant assignments, `EntityGovernance` and governance reports exist per entity and are queried through `scope` (R2). A group-parent membership adds nothing in another entity (R64). Only the settings G8 names, plus the existing **G** permissions, stay group-level and are checked on the group parent: global exchange rates, the Sharia switch under dual control (R48), entity settings, users, functional currency and key-custody delegation. An entity's matrix is never a **G** setting. Intercompany documents are approved in each side's own entity under that entity's matrix. The buyer's mirror bill (A8) uses the buyer's matrix. **[S + Q; test AT-94, AT-95]**

### 10.5 Approvable documents that must route through the one engine (A10, R61)

Every row becomes an `ApprovalActivity` with an engine adapter. "Default" = the code default of R70 (today's behaviour). **NSA** = never self-approved (R80). Module functions are reached only through the adapter (AT-67).

| Activity code | Module service (current) | Permission | Default approver today | Posts | NSA |
|---|---|---|---|---|---|
| `ledger.manual_entry` | `ledger.services.post_entry` (manual drafts) | `ledger.post_entry` | `approver`; MP in group parent | Y | |
| `invoicing.sales_document` (incl. credit/debit notes, billing corrections, non-VAT documents) | `invoicing.services.approve_sales_document` | `invoicing.approve_sales_document` (`approve_non_vat_document`) | `approver` | Y (on clearance) | |
| `invoicing.bill` (incl. vendor credit notes) | `approve_purchase_document` | `invoicing.approve_bill` | `approver` | Y | |
| `invoicing.vendor_payment` | `approve_vendor_payment` | `invoicing.approve_vendor_payment` | `approver` | Y | |
| `invoicing.purification_funding` | (invoicing R54, C2) | `invoicing.approve_purification_funding` | `approver`; MP fallback | Y | rec. |
| `invoicing.purification_payout` | (invoicing R54) | `invoicing.approve_purification_payout` | `approver` | Y | rec. |
| `invoicing.charity_recipient` | `approve_charity_recipient` | `invoicing.approve_charity_recipient` (S!) | MP, with board minute | | Y |
| `invoicing.recovery_ratio`, `invoicing.art51_true_up`, `invoicing.deferred_release`, `invoicing.deposit_application` | R56 (to build) | `invoicing.approve_*` | `approver` | per R56 | |
| `billing.contract` | `billing.services.approve_contract` | `billing.approve_contract` | `approver` | | |
| `billing.version` (amendment, termination, cancellation) | `approve_version` | `billing.approve_version` / `approve_termination` | `approver` | | |
| `billing.timesheet`, `billing.cost_pool_period` | (A8, go-live; to build) | to be named in billing.md | `approver` (default) | | |
| `hr.contract` | `hr.services.approve_contract` | `hr.approve_contract` | `approver`, MP (group parent) | | R82 |
| `hr.assignment` | `approve_assignment` | `hr.approve_assignment` | `approver` | | R82 |
| `hr.leave` | `approve_leave` | `hr.approve_leave` | seeded holders (hr §7) | | R82 |
| `hr.leave_adjustment` | `approve_leave_adjustment` | `hr.adjust_leave_balance` | `approver` | | R82 |
| `hr.termination` | `approve_termination` | `hr.approve_termination` | `approver` | | R82 |
| `hr.iban_change` | `approve_iban_change` | `hr.manage_employee` (second person) | another `hr.manage_employee` holder | | rec. |
| `hr.statutory_parameter` (G, on group parent) | `verify_statutory_parameter` | `hr.verify_statutory_parameter` | MP | | Y (G) |
| `payroll.input` | `payroll.services.approve_input` / `approve_maternity_support` | `payroll.approve_input` | seeded (payroll §7) | | R82 |
| `payroll.overtime` | `approve_overtime` | `payroll.approve_overtime` | Almiras: MP (**decided A2**); others: `HrSettings.overtime_approval_route` (`department_head`) | | R82 |
| `payroll.maternity_clearance` | `approve_maternity_clearance` | `payroll.approve_maternity_clearance` | `approver` | | |
| `payroll.run` | `approve_run` | `payroll.approve_run` | `hr_officer`, never MP (**decided A2**) | Y | |
| `payroll.payment` | `approve_payment` | `payroll.approve_payment` | `approver` | Y | |
| `payroll.loan`, `payroll.loan_schedule` | `approve_loan`, `approve_schedule` | `payroll.approve_loan` | Almiras: MP; others: top authority (OQ-39) | Y | R82 |
| `payroll.deferred_sale_terms`, `payroll.deferred_sale`, `payroll.deferred_sale_rebate` | `approve_deferred_sale_terms`, `approve_deferred_sale`, `approve_deferred_sale_rebate` | `payroll.approve_deferred_sale_terms` / `_sale` | Almiras: MP; others: top authority (OQ-39) | via invoicing | R82 |
| `payroll.final_settlement`, `payroll.settlement_correction` | `approve_final_settlement`, `approve_settlement_correction` | `payroll.approve_final_settlement` / `_settlement_correction` | `approver` | Y | R82 |
| `payroll.loan_write_off` | `approve_loan_write_off` | `payroll.approve_loan_write_off` | top authority (payroll OQ-7) | Y | R82 |
| `payroll.cost_item`, `payroll.cost_report` | `approve_cost_item`, `approve_cost_report` | `payroll.approve_cost_item` / `_cost_report` | `approver` | | |
| `payroll.gosi_fine` | `approve_gosi_fine` | `payroll.approve_gosi_fine` | `approver` | Y | |
| `payroll.profit_recognition` | (bai' ajel E12) | `payroll.approve_profit_recognition` | `approver` | via invoicing | |
| `banking.bank_account`, `banking.bank_account_change` | `approve_bank_account`, `approve_bank_account_change` | `banking.approve_bank_account` (S!) | `approver` | | Y |
| `banking.bank_transaction` | `approve_bank_transaction` | `banking.approve_bank_transaction` | `approver` | Y | |
| `banking.reconciliation` | `approve_reconciliation` | `banking.approve_reconciliation` | `approver` | | rec. |
| `tax.vat_return` (review step + approval step) | `tax.services.complete_review`, `approve_return` | `tax.review_return`, `tax.approve_return` | three people (tax R8) | Y | **Y (G3)** |
| `tax.tax_document` (payment, refund, import VAT) | `approve_tax_document` | `tax.approve_payment` | `approver` | Y | rec. |
| `tax.settings` | tax settings change | `tax.change_settings` | `approver` | | rec. |
| `tax.box_definition` | `verify_box_definition` (H3) | `tax.maintain_rates` | `approver` verifies the accountant's proposal | | rec. |
| `migration.account_map` | `migration.services.approve_account_map` | `migration.approve_account_map` | `approver`, MP | | rec. |
| `core.authority_matrix` (DC) | new (R76) | `core.approve_authority_matrix` | R76 | | Y |

**Shown in the engine inbox but kept as dual-control requests (3.4):** Sharia switch-off (R48) and ZATCA production switch. Steps stay role-bound. The engine adds the inbox and notifications only (OQ-49). **Not approvals (stay in their module, no engine request):** receipt posting by its recorder (A16, OQ-40), WHT data verification (OQ-17), treaty-certificate verification, billing period release, bank match confirmation, non-publication-day verification (G), system-user issue/post after a prior human approval (A6 → A29, R63).

### 10.6 Accounting

None. The engine and the matrix post nothing. Postings happen in the module service, called with the final approver's single-use, document-bound grant (R30, R31, R61). Self-approved postings go through `ledger.services` like any other, with the same Sharia, closed-period, balance and entity checks (R7). Account codes are unchanged.

### 10.7 Permissions

| Action (per entity) | Top authority (MP in Almiras, `ceo` elsewhere) | `ceo` (not top authority) | `approver` (CFO) | `access_admin` | `internal_auditor` | `external_auditor` | `business_analyst` | `personal_assistant` | System | Agent |
|---|---|---|---|---|---|---|---|---|---|---|
| Draft / submit a matrix version | Y | No | No (OQ-43) | Y | No | No | No | No | No | No |
| Approve a matrix version (DC second step) | Y if not proposer (R76) | No | No | No | No | No | No | No | No | No |
| Approve under the matrix | as rows / default name it | as rows / default | as rows / default | No | No | No | No | No | No | No |
| Self-approve (R79) | Y, non-NSA, logged | No | No | No | No | No | No | No | No | No |
| Delegate own approval authority | Y | Y | Y | No | No | No | No | No | No | No |
| Create assistant assignment | Y | own assistant | own assistant | No | No | No | No | No | No | No |
| Create drafts | Y (as accountant, A5) | Y | per 3.6 | No | No | No | No | Y (drafts, attachments) | per 3.6 | per 3.6 (Ag) |
| View business data | Y | Y | Y | access data only | Y, all modules, read-only | Y, read-only, personal data masked, no export (OQ-36) | reports, no personal data (OQ-37) | own drafts | No | Ag only |
| View governance report | Y | No (OQ-33) | No | No | Y | Y while valid | No | No | No | No |
| Review governance report | No | No | No | No | Y | No | No | No | No | No |
| Generate governance report | No | No | No | No | No | No | No | No | `system_governance` | No |
| Change top authority / `self_approval_enabled` | via `change_entity_settings` + DC (R76) | No | No | No | No | No | No | No | No | No |

Group level (G8, checked on the group parent): global exchange rates (`group_treasury`, `system_fx_import`), Sharia switch-off (MP then chairman, R48), entity settings and users (MP). No one holds rights in an entity without a membership in that entity (R64).

### 10.8 Acceptance tests

Fixtures as in §8: ALM (group parent, top authority `managing_partner`), SUB (`ceo`). Users: MP (MP on ALM), CH (`chairman` on ALM), CE (`ceo` SUB), CE2 (second `ceo` SUB, only where stated), CF (`approver` SUB), AA (`access_admin` SUB), AC (`accountant` SUB), PA (`personal_assistant` SUB, assigned to CE), IA (`internal_auditor` SUB), EA (`external_auditor` SUB), BA (`business_analyst` SUB), BR (`board_representative` SUB), HO (`hr_officer` SUB and ALM). All data anonymised.

| # | Given | When | Then |
|---|---|---|---|
| AT-73 | SUB version 1 draft by AA | AA adds a row naming ALM's `approver` user U (no SUB membership); a row naming role `approver` | U row refused (`not_member_of_entity`, en/ar); role row accepted (R69) |
| AT-74 | SUB has no rows for `invoicing.bill`; ALM has no rows for `payroll.run` or `payroll.overtime` | AC submits a SUB bill; HO prepares and MP approves an ALM run; MP approves ALM overtime | Bill routed to SUB `approver` holders, `rule_source=default`; ALM run: MP refused, HO allowed; ALM overtime: MP allowed. All three listed under "code defaults" in the governance report (R70) |
| AT-75 | SUB rows for `invoicing.bill`: 0–50,000.00 only | AC submits a bill of 60,000.00 | Refused `no_authority_rule` (en/ar, entity, activity, amount); bill stays draft; the code default is not used (R71) |
| AT-76 | SUB row names user CF for `invoicing.vendor_payment`; CF's membership is revoked | AC submits a payment; CE tries to approve | Request open, step has no eligible approver; CE refused; alert to CE and AA (en/ar) (R72, R18) |
| AT-77 | SUB rows: SAR 0–50,000.00 → CF; USD 0–10,000.000 → CE | AC submits a USD 9,000.000 bill; a USD 12,000.000 bill; a EUR bill worth SAR 40,000.00; a USD row with amount 1.0005 | USD 9,000 → CE; USD 12,000 refused (gap inside USD bands); EUR → CF on SAR rows; 4-decimal amount refused (R73) |
| AT-78 | Row with `delegation_allowed=False` for `invoicing.vendor_payment`; CF delegated payments to AC | AC approves a payment | Refused (`delegation_not_allowed`); CF approves in person (R74) |
| AT-79 | Two-step SUB bill: step 1 `approver`, step 2 `ceo`; CF holds both roles | CF approves step 1 and then step 2 | Step 2 refused (`same_person_two_steps`) (R75) |
| AT-80 | AA drafts SUB version 2 | AA submits; AA decides own request; CF decides; CE approves | Submit opens a DC request with `rows_hash`; AA self-decision refused; CF refused (no permission); CE approval accepted; `authority_matrix_*` rows in `AccessLog` with the diff (R76, R78) |
| AT-81 | CE drafts SUB version 3; MP drafts ALM version 2 | CE decides own; BR decides SUB; MP decides own; CH decides ALM | Self-decisions refused; BR and CH accepted (R76, OQ-42 default) |
| AT-82 | SUB version 2 approved 2027-03-10 with `effective_from` 2027-03-01; version 3 with `effective_from` 2027-04-01; a bill submitted 2027-03-30 under version 2 | Approve version 2; edit a row of version 3 after approval; approve the open bill on 2027-04-02 | Backdated `effective_from` refused; row edit refused (also raw SQL on PostgreSQL); the open bill keeps version 2 rules (R77) |
| AT-83 | SUB `ceo` CE is top authority; matrix gives `ceo` 0–50,000.00 bills | CE creates, submits and approves bill B 30,000.00 | Posted; grant `self_approval=true`, `top_authority=true`; `self_approval` `AccessLog` row with all R83 fields (R79, R83) |
| AT-84 | Same; plus CE2 granted `ceo` in SUB | CE self-approves another bill; CF self-approves own bill; delegate AC (of CE) approves a bill AC created; ALM `ceo` CA self-approves in ALM; SUB `self_approval_enabled=False` and CE self-approves | All refused (`self_approval`): two holders (OQ-47), not top authority, delegate, not ALM's top authority, switched off; each with a `permission_denied` row (R79) |
| AT-85 | CE is preparer of SUB's VAT return | CE reviews and approves it; CE approves own tax payment; CE approves own bank account change; CE decides own matrix version; MP self-approves the ALM account map | All refused (NSA); catalogue scan finds NSA on every R80 a–i permission (R80) |
| AT-86 | PA drafts a SUB bill for principal CE | CE approves it; CF approves it; PA submits it | CE: allowed only as top-authority self-approval, logged with `drafted_for=CE`; CF allowed (not self); PA submit refused (R81, R60) |
| AT-87 | CE has an overtime input and a loan in SUB; SUB matrix `payroll.overtime` → `ceo`, then `approver` | CE approves own overtime and own loan | Refused (`beneficiary`); overtime goes to the next step (`approver`); with no next step it waits (R82) |
| AT-88 | March 2027 events in SUB: AT-83 self-approval, a delegation granted and used, version 2 approved, a default-rule approval, a refused self-approval | Job runs 2027-04-01 02:00 Riyadh | One SUB report, revision 1, sections 1–6 filled, en and ar labels, CSV and PDF, `content_hash` stored; ALM report includes section 7 G actions; an entity with no events gets "nothing to report" (R84) |
| AT-89 | SUB report for March | IA, EA (valid), CE, AC, BA, MP (no SUB membership) open it; IA reviews it twice | IA, EA, CE allowed and logged; AC, BA, MP refused; first review saved, second refused; unreviewed report after 30 days alerts CE (R86) |
| AT-90 | Generated report | Raw UPDATE on `content` (PostgreSQL); re-issue | Update refused; revision 2 created naming revision 1 (R85) |
| AT-91 | CF delegates bills to AC 2027-03-01 to 2027-06-15; then 2027-03-01 to 2027-05-29 | Save both; AC approves on 2027-05-30; reminders job on 2027-05-26 | First refused (over 90 days); second saved; 05-30 refused (expired); reminder sent to CF and AC on 05-26, en/ar (R87) |
| AT-92 | CF delegates to IA; CE delegates matrix approval to CF; CF delegates to a system user | Save each | All refused (R88) |
| AT-93 | IA, EA, BA in SUB | IA creates a draft bill; EA exports the ZATCA archive; EA membership granted with `valid_to` 13 months away; BA opens an employee's IBAN; agent user granted `internal_auditor` | All refused (R89, R11) |
| AT-94 | MP on ALM only; SUB matrix and reports | MP lists SUB matrix versions, delegations and reports | "Not found" (404) for each (R90, R64) |
| AT-95 | Intercompany: SUB sells to ALM; mirror bill created by `system_intercompany` in ALM | Approve the mirror bill | Routed under ALM's matrix (or ALM default); SUB's matrix not used; MP may self-approve only if ALM's rules make MP eligible and the bill is not NSA (R90) |
| AT-96 | Code scan | List every **A** permission in the catalogue | Each has an `ApprovalActivity`, an engine adapter and a code default (R70, R61) |

### 10.9 Open questions (with recommended defaults)

The owner asked for recommendations. Each default below is what the builder implements unless the owner says otherwise. **D** = default in force; **GL** = must be confirmed before go-live.

| # | Question | Recommended default | Blocks? |
|---|---|---|---|
| OQ-42 | Who is the second person for a matrix change when the top authority proposes it? | Almiras: the group's `chairman` (as for Sharia switch-off). Other entities: a new entity role `board_representative` (human; holds only `core.approve_authority_matrix` and `core.view_governance_report`), named by that company's board and granted by its top authority under R8 (never to self). The Almiras MP may hold it in a subsidiary as shareholder representative (A1) through an explicit membership. When someone else proposes: the top authority decides | D; GL per entity to name the holder |
| OQ-43 | Who may draft a matrix version? | Top authority and `access_admin` of the entity; not the CFO (it would set its own limits) | D |
| OQ-44 | Should code defaults stay forever, or must each entity enter a full matrix before go-live? | Defaults stay, so a new subsidiary works on day one. The governance report shows every default-rule approval, so the gap stays visible. Replaces OQ-30's GL status with "recommended before go-live" | D (was GL) |
| OQ-45 | Maximum length of an approval delegation (A13; OQ-16 had no maximum) | 90 days, renewable as a new logged delegation; reminder 3 days before expiry | D |
| OQ-46 | Does a new matrix version re-route requests that are already open? | No: rules are fixed at submission; a returned and resubmitted document resolves again | D |
| OQ-47 | If an entity has two active holders of its top-authority role (e.g. two `ceo` memberships during a hand-over), may either self-approve? | Neither: self-approval needs exactly one active holder; the hand-over is a revoke + grant | D |
| OQ-48 | Exclusion list R80 rows b, f, g, h, i (team recommendations, not owner decisions) | Adopt all five as NSA. They follow from G3 (tax), payment-fraud control (IBAN), C2/C6 (purification), the opening-balance tie-out (migration) and reconciliation control (banking) | D; owner to confirm |
| OQ-49 | A10 says "all approvable documents" use one engine. Do dual-control requests (Sharia switch-off, ZATCA production, matrix versions) become engine requests too? | They appear in the engine's inbox and notifications. Their rules stay `DualControlRequest` (payload hash, role-bound steps, single use, expiry) | D |
| OQ-50 | "Auditors read-only": may the internal auditor still record retention decisions (R55, OQ-29) and the report review? | Yes. Both record a committee decision or an audit fact, not a business transaction. No other write | D |
| OQ-51 | Should a self-approval also trigger an immediate notification (not only the monthly report)? | No immediate notification; the monthly report only. An entity may add `internal_auditor` notification later as data | D |
| OQ-52 | Is there an amount ceiling on top-authority self-approval? | No ceiling (A3 sets none). An entity may limit it in effect by not making the top authority eligible above a band | D |
| OQ-53 | `internal_audit` (owner's wording) vs the seeded code `internal_auditor` | Keep `internal_auditor` (already in 3.1); `name_en` "Internal auditor", `name_ar` "المدقق الداخلي" | D |
| OQ-54 | Does the governance report go to the board or to external parties outside the ERP? | In-app only. The internal auditor exports PDF/CSV for the internal audit committee or the board when they ask, and every export is logged | D |
| OQ-55 | Timesheets and cost pools (A8, needed at go-live) have no approval permissions yet | billing.md names `billing.approve_timesheet` and `billing.approve_cost_pool_period` (A); default approver `approver`; added to 10.5 when that spec is amended | D |

Earlier OQs changed by §10: **OQ-16** delegation maximum → OQ-45. **OQ-30** → OQ-44 (no longer GL). **OQ-31** "no second approval" → replaced by R76 (dual control), OQ-42, OQ-43. **OQ-32** stands (an active delegation does not stop the delegator). **OQ-33** recipients stand; `external_auditor` added as a reader while valid. **OQ-36** external auditor maximum → 12 months.

Arabic labels of new roles, events and report sections are marked [AR-review] for the Arabic reviewer: `board_representative` "ممثل مجلس الإدارة", "مصفوفة الصلاحيات" (authority matrix), "تقرير الحوكمة الشهري" (monthly governance report), "اعتماد ذاتي" (self-approval).

### 10.10 As built (2026-10-20, module-builder; every OQ-42 to OQ-55 default adopted on the owner's instruction)

**Where.** Models `core/access/authority_models.py` (app `core`): `ApprovalActivity`, `EntityGovernance`, `AuthorityMatrixVersion`, `AuthorityRule`, `ApprovalDelegation`, `AssistantAssignment`, `ApprovalRequest`, `ApprovalDecision`, `AccessAlert` (bilingual in-app alerts, written once per key), `GovernanceReport`. Code catalogue of activities and defaults: `core/access/authority_defaults.py`. Engine: `core/access/engine.py`. Services: `core/access/matrix.py` (versions, rows, R76 decisions, effective dates, top-authority / self-approval switch changes), `core/access/delegations.py` (R62, R81, R87, R88), `core/access/reports.py` (R84 to R86; CSV and PDF). Migrations `core/0026_authority_matrix` (schema), `core/0027_seed_access_v17_authority_matrix` (ROLE_SEED_V17, `system.governance` user, activity rows, one `EntityGovernance` per existing entity), `core/0028_authority_matrix_triggers` (PostgreSQL: frozen non-draft versions and rows, band overlap, immutable reports, append-only decisions and alerts). Jobs: `manage.py governance_reports` (1st of the month from 02:00 Riyadh) and `manage.py access_daily_jobs` (versions taking effect, delegation reminders, unreviewed reports). Adapters: `<app>/approval_adapters.py` in invoicing, billing, hr, payroll, banking, tax, migration and ledger, registered in each `AppConfig.ready()`.

**How every approval reaches the engine (R61).** `core.access.require()` hands every **A** permission checked for a document to `engine.authorise`, so no module approves outside the engine and existing module services keep working unchanged as the adapters' approve services. The engine finds or opens the document version's `ApprovalRequest` (rules resolved and stored when it opens, R77), applies the matrix rows or the code default (R70), eligibility through own memberships **in that entity only** (no group cascade for approvals, R64, R70: the managing partner's fallback applies only in the group parent), delegation (R62, R74, R88), one person per request (R75), beneficiary (R82, before anything else), and self-approval only for the single active holder of the entity's top-authority role, with the switch on, for a non-NSA permission (R79, R80, R81); a self-approval writes the `self_approval` AccessLog row (R83), a refused one a `permission_denied` row with reason `self_approval` and `why`. The grant's log row carries `top_authority`, `rule_source`, `approval_request`, `delegation` / `on_behalf_of`; `verify_grant` and the ledger registry accept a self-approval grant only when its log row says `top_authority` (R58). `require(..., purpose="return" | "reject" | "check")` marks module uses of an approval permission that are not approvals (returns, rejections, closing a bank account, the VAT reviewer step): eligibility is the same, nothing is recorded as approved, returns and rejections close the open request. `engine.decide(document, user)` is the inbox entry point: it calls the module's approve service through the adapter; a decision that is not the last is recorded after the module's work is rolled back, so the module never acts on a partial approval. `engine.submit` is called by invoicing's submit services (sales documents, bills, vendor payments), so gaps are refused at submission (R71, AT-75); for other modules the request opens at the first decision (same rules, resolved on that day).

**Decisions taken while building (for review).**
1. A module service called directly (not through `engine.decide`) is accepted when the call completes the request; it is refused `approval_request_required` (en/ar) while steps remain (AT-67 as built). Under code defaults every request has one step, so existing callers are unchanged.
2. No cascade (R64) is applied to approvals (engine eligibility) and to the §10 rights (`catalogue.NO_CASCADE`); view and other rights still follow R6 as built before. The custom user model (R66) is not built.
3. The R16 CFO delegate (`delegate_approval`, a `delegated_from` membership) still works for existing callers; new delegations are `ApprovalDelegation` (R62, R87, R88).
4. A matrix or top-authority change request stays open 30 days (`matrix.MATRIX_DECISION_TTL`; the 72 h of OQ-11 stays for other dual-control requests).
5. Top authority and self-approval switch (R67, R76): requested by a holder of `core.change_entity_settings` on the group parent (`matrix.request_governance_change`), decided by the R76 decider (`decide_governance_change`), applied alone with `change_entity_settings(..., dual_control_request=...)`, logged before/after. `create_entity` writes the R67 default row; an entity without a row reads the R67 defaults.
6. R65 in part: in an entity, holders of its top-authority role grant memberships like the managing partner (an access admin still may not grant `ceo`, `approver`, `access_admin`, `zatca_admin`, `hr_officer`, `payroll_officer`, `external_auditor`, `board_representative`, `internal_auditor`).
7. Report readers: holders of `core.view_governance_report` in that entity through a recipient role (default internal auditor and the top-authority role), `external_auditor` while valid, or `board_representative` (OQ-42 gives it the right). The `ceo` of the group parent is not a reader there.
8. `approver` gains `payroll.approve_overtime` (V17) so a matrix can name the CFO for overtime (AT-87); under the code default payroll's hr route still decides. `ceo` holds the accountant's rights and every non-G approval (OQ-41).
9. Activities whose module has not built the document yet are in the catalogue as `pending` (no adapter): `invoicing.recovery_ratio`, `invoicing.art51_true_up`, `invoicing.deferred_release`, `invoicing.deposit_application`, `billing.timesheet`, `billing.cost_pool_period`, `payroll.mudad_justification` (payroll §11, slice 3 builds the document and its adapter). `payroll.approve_profit_recognition` has an activity and a default but no adapter: no service checks it yet (AT-96 lists it as reviewed).
10. Payroll activity codes follow payroll §11 R82 (`payroll.run_reversal`, `payroll.secondment_cost_report`, `payroll.payment_reversal`); 10.5's `payroll.loan_schedule` is approved under `payroll.loan`.
11. Amount measures per adapter (OQ-35, OQ-54): bills the vendor document total; contracts have no total-value field yet, so a contract matches only rows without an upper band; payroll per the R82 table.
12. The PDF export draws Arabic with a minimal built-in shaper (contextual forms, lam-alef, right-to-left runs) and a TrueType font with Arabic glyphs (`GOVERNANCE_REPORT_FONT`, else DejaVu Sans); [AR-review] the rendering.
13. Not built: the `system_intercompany` mirror bill (AT-95 is tested on an intercompany bill of the group parent), the retention decision right `core.decide_retention` (R55; so the internal auditor's retention write of OQ-50 is not seeded), escalation reminders of open requests (R18 note), admin pages for the new models, and removal of the inert `InvoicingSettings.segregation_required` field (it no longer affects any approval, R14 amended).

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
| *To build (2026-10-18, A24 to A32), integration line per option C:* `config/settings.py`, new `access` app from `main` | `AUTH_USER_MODEL = "access.User"`; fold `UserProfile` fields; drop `main`'s `EntityAccess`/`Role`/`Permission`; fresh-DB migrations (R66) |
| `approvals` app from `main` | Policies replaced by `AuthorityRule`; eligibility via `core.access.has`; `Delegation` → `ApprovalDelegation`; adapters per registered document type calling module services with the approver's grant; no ledger status changes (R57, R61, R62) |
| `core/access/models.py`, `catalogue.py`, `seed.py`, new migrations | `EntityGovernance`, `ApprovalActivity`, `AuthorityRule`, `ApprovalDelegation`; roles `ceo`, `internal_auditor`, `personal_assistant`, `business_analyst`, `external_auditor`, `system_intercompany`; seed matrix rows per entity (R57, R60) |
| `core/access/services.py` (`require`, `entities_for`, `scope`, `grant_membership`) | No group cascade (R6, R64); `allow_self_approval` only from the engine's top-authority path (R14, R58); `valid_to` required for `external_auditor` (R60); grants per entity top authority (R65) |
| `ledger/documents.py`, `ledger/services.py` (`_authorise_document_post`, `post_entry`) | Accept a grant row with `self_approval=true` and `top_authority=true`; refuse every other self-approval (R35 note, R58) |
| `invoicing` (`InvoicingSettings.segregation_required`) | Remove the switch and its tests (R14 amended) |
| `core/services.py` (`governance_report`, `create_entity`), job | Monthly report (R59); top authority at creation (R67) |
| `core` `RetentionNomination` | `committee_minute_ref`, `committee_minute_date` required for `burn` (R55) |
| `hr` (`HrSettings.overtime_approval_route`), `payroll` overtime/loan approval | Read the matrix instead (R53, R54) |
| Tests built on cascade (AT-3, AT-56, payroll AT-29) and on `segregation_required=False` (AT-7, AT-49 part) | Expectations change as amended above |
| *To build (2026-10-20, §10):* `core/access/models.py`, migrations | `AuthorityMatrixVersion`, `AssistantAssignment`, `GovernanceReport`; `AuthorityRule.version`, `band_currency`, `approver_user`, `delegation_allowed`; `EntityGovernance.self_approval_enabled`; PostgreSQL triggers freezing non-draft versions and generated reports (R77, R85) |
| `core/access/authority_defaults.py` (new) | Code default per activity reproducing today's holders and module checks (R70); test that every **A** permission has one (AT-96) |
| `core/access/catalogue.py` | Flag **NSA** and the R80 list; new codenames of 10.3; `system_governance` system user; recommended `board_representative` role (OQ-42) |
| `payroll/approvals.py`, `hr` overtime route, `tax` three-people checks | Become adapters / code defaults of the engine; module-level checks stay (R70) |
| Every module `approve_*` service in 10.5 | Reached only through its engine adapter; `beneficiary_of(document)` adapter per type (R82) |
