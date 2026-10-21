# Web: Arabic/English web interface (Track: web UI)

Django app: `web` (templates, views, forms, URL conf, middleware, template tags; `web/services.py` for UI preferences only). Status: **draft spec**. Written 2026-10-17 by the spec-writer agent.
Depends on: `docs/specs/access.md` (roles, `require` / `scope`, R1 to R3, R20, R21, R24, R29, OQ-4), `docs/ai-agent-gateway.md`, and the service layers of `core`, `ledger`, `invoicing`, `billing`, `zatca`, `hr`, `banking`. `payroll` is not built: its screens come later (OQ-18).

## 1. Purpose

The web module is the human user interface of the ERP: login with MFA, an entity switcher, role dashboards, an approval inbox, data-entry screens for accounting and HR staff, read-only reports and the printable invoice. It works the same for Almiras, every subsidiary, partly-owned and minority entity, and any entity added later as an `Entity` row: every page is scoped to one entity in its URL and to the user's memberships. It holds **no business logic**: views parse input, call the owning module's `services.py` with `request.user`, and render the result. Every text is in English and Arabic (RTL for Arabic), dates can be shown in Hijri and Gregorian, and digits in Latin or Arabic-Indic. Sharia, period, balance, segregation and entity rules stay in the services and are shown, never bypassed. Agent and system users cannot use it (they use the gateway or run as jobs). It posts no journal entries itself.

## 2. Decisions

| # | Decision | Who | Date |
|---|---|---|---|
| W-D1 | Open source only; no paid SaaS, no Odoo/SAP/Dynamics components. | CLAUDE.md | in repo |
| W-D2 | Server-rendered Django templates with HTMX (or minimal vanilla JS), not a heavy SPA unless justified. **Proposal (this spec):** Django templates + HTMX 2.x, vendored, progressive enhancement (every form also works as a plain POST without JS); no SPA, no Node build step. Reason: business logic stays in services, CSRF/CSP/i18n come from Django, fewer moving parts, works on mobile browsers. Needs human approval at merge (OQ-1). | Caller brief (proposal by spec-writer) | 2026-10-17 |
| W-D3 | Views are thin and call services with the request user; never query models directly without `scope()`; every page entity-scoped and permission-checked. | Caller brief; CLAUDE.md; access A5, R1, R2 | 2026-10-17 |
| W-D4 | Arabic RTL and English LTR with a language switch; all texts bilingual; Hijri/Gregorian date display per entity/contract settings; Arabic-Indic or Latin digits preference. | Caller brief; CLAUDE.md | 2026-10-17 |
| W-D5 | Accessibility WCAG 2.1 AA; responsive, mobile for approvers. | Caller brief | 2026-10-17 |
| W-D6 | Security: login with MFA (TOTP, open-source library), required by default for approvers, Managing Partner, Chairman, ZATCA admins and access admins; session timeout; CSRF; content security policy; audit of sensitive views; rate limiting; no personal data in URLs. Confirms the access OQ-4 default, which stays open for the MP's sign-off (OQ-3). | Caller brief; access OQ-4 | 2026-10-17 |
| W-D7 | Django admin stays for superusers only. | Caller brief | 2026-10-17 |
| W-D8 | The printable invoice reuses the HTML render built in invoicing slice 2. | Caller brief | 2026-10-17 |
| W-D9 | Agents connect only through the gateway, act as named service users, create drafts only; agent and system users cannot log in to the UI. | CLAUDE.md; access A6, A7, R20, R21 | in repo / 2026-10-16 |
| W-D10 | Receipts: the recorder may post a customer receipt without a bank charge; otherwise a second person posts. | Project owner (access A16, banking B7) | 2026-10-17 |
| W-D11 | Payroll is approved by the HR officer; loans, bai' ajel and Almiras overtime by the MP. | Project owner (access A21, A22) | 2026-10-17 |

## 3. Data model

The web module holds UI preferences and MFA devices only. These are user-level or entity-level settings, not business records; preference rows have no `entity` FK because they follow the user across entities (justified like `Role`, access 3.1).

### 3.1 `UiPreference` (OneToOne `auth.User`)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| user | OneToOne User, PROTECT | Y | | Human users only (R12) |
| digits | Choice `latin`, `arabic_indic` | Y | | Default `latin` (OQ-7); applies in the Arabic UI only (R33) |
| date_display | Choice `entity_default`, `gregorian`, `gregorian_hijri`, `hijri_gregorian` | Y | | Default `entity_default` |
| last_entity | FK `core.Entity`, null, PROTECT | N | | Re-checked on every use (R10) |
| updated_at | DateTime | Y | | |

Language is **not** stored here: it is `UserProfile.preferred_language` (access 3.2), changed through a logged access service (appendix).

### 3.2 `EntityDisplaySettings` (OneToOne `core.Entity`)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | OneToOne Entity, PROTECT | Y | | Created with defaults by entity setup data (no code path per entity) |
| date_display | Choice `gregorian`, `gregorian_hijri`, `hijri_gregorian` | Y | | Default `gregorian_hijri` (OQ-6) |
| changed_by / changed_at | FK User / DateTime | Y | | Every change writes an `AccessLog` row (R26) |

### 3.3 MFA devices (django-otp, OQ-2)

| Model | Fields used | Notes |
|---|---|---|
| `web.EncryptedTOTPDevice` (subclass of `django_otp.plugins.otp_totp.models.TOTPDevice`) | user, name, confirmed, key (Fernet-encrypted at rest, keys in `WEB_MFA_KEYS` env, like HR/banking), step 30 s, digits 6, tolerance 1, throttling on | One confirmed device per user (OQ-11) |
| `django_otp.plugins.otp_static.StaticDevice` / `StaticToken` | 10 single-use recovery codes, stored hashed (subclass, OQ-2) | Shown once at enrolment |

### 3.4 Logging (no new model)

New `AccessLog.event` values (core change, appendix): `login_succeeded`, `logout`, `mfa_enrolled`, `mfa_verify_failed`, `mfa_reset`, `report_exported`, `document_printed`, `display_settings_changed`. Failed password logins are recorded by the rate limiter (django-axes `AccessAttempt`, OQ-9), not in `AccessLog` (unknown usernames have no actor). Personal-data views are already logged by the services that unmask them (hr `employee_record`, banking `reveal_iban`, billing `view_secondment`, invoicing R51).

## 4. Rules

Enforcement: **V** = view decorator or middleware in `web`; **S** = owning module's service; **Set** = settings; **T** = test that scans code, templates or URLs; **C** = Django system check.

**Architecture and thin views**
1. Views, forms and template tags in `web` call only public functions of `<app>/services.py`, `core.access.services` (`entities_for`, `has`, for menus and the switcher only) and `web/services.py`. They never use `Model.objects`, `_base_manager`, raw SQL, `QuerySet.update` or `.save()`/`.delete()` on business models. **[T: AST scan of `web/`, empty allow-list]**
2. Every service call passes `user=request.user` (or the service's equivalent argument). `web` never calls `system_user()`. **[T scan + review]**
3. Lists come only from service functions that apply `scope` (e.g. `invoicing.services.sales_documents(user, entity)`, `hr.services.employees(user, entity)`) or that `require` the view right for the entity (e.g. `billing.services.contracts(entity, user=...)`). Details come from scoped getters (`access.get_scoped_or_404` inside a service). A record outside the user's scope is 404, never 403 (access R2). **[S; test WT-1, WT-2]**
4. URLs carry the entity: `/e/<entity_code>/…`. The entity is resolved only among entities the user may see (R10); otherwise 404. A detail whose record's entity differs from the URL entity is 404. **[V; test WT-2]**
5. Every change is a POST (never GET), one service call per action, inside the request transaction (`ATOMIC_REQUESTS`), followed by a redirect (post/redirect/get). **[V; T: crawl of every GET URL writes no rows]**
6. Buttons and menu items shown via `has()` are hints only; the service's `require` is the guard. A refused action shows the service's message in the active language (`AccessDenied.message_en/_ar`, or `label_part` of "en / ar" errors) and changes nothing. **[S; test WT-4, WT-6]**
7. Approve, post, return and decide forms carry the document version (or `modified_at`) shown on screen; the service refuses if the document changed since ("reload and review again"). **[S, appendix; test WT-25]**
8. Return and reject need a reason: Arabic reason required, English optional, as the existing services take them (`reason_ar`, `reason_en`; OQ-12). The reason is stored by the service and shown to the creator in the document history. **[S + form; test WT-21]**

**Entities**
9. The entity switcher lists exactly the entities where the user holds at least one active membership-backed permission (union of `entities_for(user, p)` over the catalogue, group roles covering `full` descendants only, access R6), labelled with the entity name in the active language and its code. Inactive entities are marked "Inactive / غير نشط"; their pages are read-only (access R38). **[S; test WT-3, WT-33]**
10. `UiPreference.last_entity` is used only if still visible at the request; otherwise the first visible entity, or a "no access" page when none. A superuser without memberships sees no entity (access R24). **[V; test WT-34]**
11. Switching entity opens the new entity's dashboard; no form data, selection or filter carries over from one entity to another. **[V; test WT-3]**

**Login, MFA and sessions**
12. Only `kind=human`, active users log in (`HumanModelBackend`). A middleware logs out any session whose user is not human or no longer active. Agent and system users get the same generic "wrong username or password" answer. **[backend + V; test WT-11]**
13. MFA (TOTP, RFC 6238) is required for a user who is a superuser or holds an active membership in role `approver`, `managing_partner`, `chairman`, `zatca_admin` or `access_admin` (W-D6). *Proposed extension (OQ-3):* also any user holding a permission flagged **A**, **DC** or **S!** (e.g. `hr_officer`, `department_head`), derived from the catalogue so new roles need no code. **[V middleware; test WT-8, WT-9]**
14. A user who requires MFA reaches no page except enrolment, verification, language switch and logout until the session is OTP-verified. A membership granted mid-session that makes MFA required takes effect at the next request. **[V; test WT-8]**
15. Approve, post, decide and access-administration POSTs are refused unless the session is OTP-verified whenever the user requires MFA (checked at the request, not cached). **[V; test WT-8]**
16. Enrolment: QR code generated on the server as SVG (`qrcode`, BSD), never by an outside service; a correct code confirms the device; 10 recovery codes shown once. Reset of a lost device is done by another person (OQ-11), never by the user for themselves, and logged `mfa_reset`. **[V + web service; test WT-8]**
17. Sessions: idle timeout and absolute timeout (OQ-8); session key rotated at login and at OTP verification; cookies `Secure`, `HttpOnly`, `SameSite=Lax`; logout by POST only. **[Set + V; test WT-17]**
18. Rate limits (OQ-9): login lockout per username + IP after repeated failures; OTP verification throttled (django-otp); per-user limits on approval/post POSTs, statement imports and report exports. A blocked request shows a bilingual message and changes nothing. **[V/Set; test WT-18]**
19. `/admin/` admits only active, OTP-verified superusers. Staff users who are not superusers are refused. Business actions are never done in admin (access R24, `EntityScopedAdmin` stays). Applies once membership screens exist in `web` (J13; OQ-10). **[V admin site; test WT-10]**

**Security headers and privacy**
20. Content Security Policy through Django's built-in CSP (`SECURE_CSP`): `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'`. No inline scripts, inline styles or `on*`/`hx-on` attributes; HTMX configured with `allowEval: false`, `includeIndicatorStyles: false` (meta tag). All JS, CSS and fonts are vendored static files (no CDN). Also HSTS, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`. **[Set; T: template scan; test WT-16]**
21. CSRF on every POST (Django middleware); HTMX sends the token header. No `csrf_exempt` in `web`. **[Set; T scan; test WT-15]**
22. Authenticated responses carry `Cache-Control: no-store, private`. **[V middleware; test WT-36]**
23. No personal data in URLs or query strings: path parameters are entity codes and integer primary keys only; GET filters are enumerated values (status, type, period), dates and document numbers. Free-text search (names, ID numbers, IBANs, phone, e-mail) is sent by POST. Uploaded file names never appear in URLs. **[T: URL pattern scan + crawl with fixture personal data; test WT-19]**
24. Personal data is masked in lists. Unmasking is an explicit POST that calls the logging service (`hr.services.employee_record`, `banking.services.reveal_iban`, `billing.services.view_secondment`, invoicing R51); never on GET, never prefetched. **[S + V; test WT-20]**
25. Error pages (403, 404, 429, 500) are bilingual, show no stack trace and do not reveal whether a record exists in another entity. **[V; test WT-2]**
26. `login_succeeded`, `logout`, MFA events, report exports, invoice prints and display-settings changes write `AccessLog` rows (3.4). Never passwords, OTPs, recovery codes or session keys. **[web service; test WT-8, WT-30]**

**Language, dates, digits, money**
27. The UI language is `UserProfile.preferred_language`; the switch (POST) changes it through the access service. Before login, a cookie or `Accept-Language` chooses. Pages render `<html lang="ar" dir="rtl">` or `<html lang="en" dir="ltr">`; one stylesheet uses CSS logical properties; direction-bearing icons mirror in RTL. **[V; test WT-12]**
28. Every user-facing text exists in English and Arabic: UI texts as pairs in `web/texts.py` (OQ-20), model labels split with `core.models.label_part`, data from `name_en` / `name_ar` (and other `_en`/`_ar` pairs). When one language of a data value is empty, the other is shown with its own `lang` attribute. Templates contain no literal text outside the text tag. **[T: every key has non-empty en and ar; template scan; test WT-13]**
29. Identifiers and figures (document numbers, VAT and CR numbers, IBANs, account codes, amounts with currency) are wrapped in `<bdi dir="ltr">` so they read correctly in RTL. **[template tag; test WT-12]**
30. Dates are stored Gregorian. Display follows `UiPreference.date_display`, else `EntityDisplaySettings.date_display`. Hijri uses the Umm al-Qura table of `hijridate` (already a dependency). An HR contract with `calendar_basis=hijri` shows its contract dates Hijri first whatever the preference (hr R58). Tax, ZATCA and VAT dates show Gregorian first. A date outside the library's range shows Gregorian only. **[template tag; test WT-26]**
31. Date input is Gregorian (`<input type="date">`). Hijri is typed only where the model has a Hijri text field (e.g. hr `expiry_date_hijri`, as printed; hr OQ-9). **[form; test WT-26]**
32. Times are shown in Asia/Riyadh. **[Set]**
33. Digits: `latin` by default; `arabic_indic` changes displayed amounts, dates and counts in the Arabic UI only. Identifiers (rule 29) always use Latin digits. Inputs accept Latin, Arabic-Indic and Extended Arabic-Indic digits and the Arabic decimal separator (٫), normalised before parsing. **[template tag + form field; test WT-27]**
34. Money is `Decimal` end to end: form fields are `DecimalField` with the currency's minor units (functional SAR 2 decimals; transaction currencies up to 3, per the currency); display shows the currency code; no `FloatField`, `float()` or `floatformat` in `web`. **[form; T scan; test WT-27]**

**Accessibility and layout**
35. WCAG 2.1 AA: semantic HTML and landmarks, a skip link, a visible label on every field, errors listed in a summary and linked to fields, visible focus, contrast ≥ 4.5:1, full keyboard use, no status shown by colour only, HTMX updates announced in an `aria-live` region and focus moved to the result. **[T: axe-core scan; test WT-31]**
36. Inbox, document view, approve/return, dual-control decisions and dashboards work at 360 px width without horizontal scrolling. Data-entry screens are desktop-first and usable from 768 px. **[test WT-32]**

**Performance**
37. Lists are paginated (default 50 rows). The number of SQL queries of a list page does not grow with the number of rows and is at most 15 (session, user, memberships, scope and the page included). Services return querysets with the needed `select_related` / `prefetch_related` (appendix). **[test WT-14]**
38. A dashboard runs at most 25 queries; a card is not computed for a module the user cannot view in that entity. **[test WT-14, WT-29]**

**Approval inbox and document view**
39. The inbox lists submitted items of every entity where the user `has` the approving permission (table 4.1), grouped and labelled by entity, excluding items the user created, submitted or last modified (for **A** permissions; access R14, R35). This exclusion is cosmetic; the service decides. Each row: type, number, entity, counterparty, amount and currency, submitted by, submitted at, age, source badge (human / system / agent). **[S via `pending_approvals`; test WT-22]**
40. The document view is read-only: header, lines, totals, VAT breakdown, attachments, ZATCA status where relevant, history (created, submitted, returned with reasons, approved), and `source`. Actions are offered per permission: approve, return/reject, and nothing else. **[V + S; test WT-6]**
41. Dual-control requests (Sharia off, ZATCA production switch) show entity, requested change in words, reason, requester, expiry, and approve/reject; decided through `core.services.decide_sharia_enforcement_off` / `core.access.services.decide_dual_control`. Applying an approved request is a separate action by the requester. **[S; test WT-23, WT-24]**
42. Customer receipts show "Post" to their recorder only where invoicing allows it (W-D10); otherwise they appear in another holder's inbox. **[S; test WT-7]**

**Reports and print**
43. Reports are read-only; parameters (as-of date, period, currency view) are GET. Group reports (intercompany) need `ledger.view` on the group parent and show only pairs the user may see (ledger `intercompany_balances`). CSV export is a POST, logged `report_exported`. **[S + V; test WT-28]**
44. The printable invoice is the invoicing slice 2 HTML render, called through an invoicing service with the user; the web adds only print CSS and the page frame. Printing writes `document_printed`. Not built until that render exists (OQ-14). **[S; test WT-30]**

### 4.1 Approvable items (inbox sources, from the built services)

| # | Item | Waiting state | Approve / post service | Return / reject service | Permission (seed holders) |
|---|---|---|---|---|---|
| I-1 | Sales invoice, credit note, debit note (incl. billing correction notes) | submitted | `invoicing.approve_sales_document` | `invoicing.return_to_draft` (no reason stored: gap, OQ-12) | `invoicing.approve_sales_document` (approver; MP fallback) |
| I-2 | Vendor bill, vendor credit note | submitted | `invoicing.approve_purchase_document` | none (gap, OQ-12) | `invoicing.approve_bill` (approver) |
| I-3 | Vendor payment, refund received | submitted | `invoicing.approve_vendor_payment` | none (gap) | `invoicing.approve_vendor_payment` (approver) |
| I-4 | Customer receipt / refund needing a second person | recorded, unposted | `invoicing.post_receipt` | none (gap) | `invoicing.post_receipt` (receivables_clerk, accountant) |
| I-5 | Billing correction draft to review | draft from `system_billing` | submit via billing review (then I-1) | `billing.discard_adjustment` (reason) | `billing.review_correction` (accountant, receivables_clerk) |
| I-6 | Billing contract | submitted | `billing.approve_contract` | `billing.return_contract` (reason) | `billing.approve_contract` (approver) |
| I-7 | Contract version (amendment, termination, cancellation) | submitted | `billing.approve_version` | `billing.reject_version` (reason) | `billing.approve_version` / `billing.approve_termination` (approver) |
| I-8 | Employment contract | submitted | `hr.approve_contract` | `hr.reject_contract` (reason) | `hr.approve_contract` (approver) |
| I-9 | Assignment / secondment | submitted | `hr.approve_assignment` | none (gap) | `hr.approve_assignment` (approver) |
| I-10 | Leave request | requested | `hr.approve_leave` | `hr.reject_leave` (reason) | `hr.approve_leave` (hr_officer, not own) |
| I-11 | Leave balance adjustment | requested | `hr.approve_leave_adjustment` | `hr.reject_leave_adjustment` (reason) | `hr.adjust_leave_balance` (approver) |
| I-12 | Termination | submitted | `hr.approve_termination` (with `acknowledge_protection` checkbox where the service asks) | none for the approver (gap; `withdraw_termination` is the recorder's) | `hr.approve_termination` (approver) |
| I-13 | Statutory parameter | entered, unverified | `hr.verify_statutory_parameter` | none (gap) | `hr.verify_statutory_parameter` (MP, never `entered_by`) |
| I-14 | Bank account / bank account change | proposed | `banking.approve_bank_account` / `approve_bank_account_change` | none (gap) | `banking.approve_bank_account` (approver) |
| I-15 | Bank transaction | submitted | `banking.approve_bank_transaction` | none (gap) | `banking.approve_bank_transaction` (approver) |
| I-16 | Bank reconciliation | submitted | `banking.approve_reconciliation` | none (gap) | `banking.approve_reconciliation` (approver, not preparer) |
| I-17 | Manual journal entry | submitted (`ledger.submit_entry`) | `ledger.post_entry` after `require(..., "ledger.post_entry", document=draft)` (wrapper needed, appendix) | none (gap) | `ledger.post_entry` (approver) |
| I-18 | FX revaluation run | draft | `ledger.post_revaluation` (grant `ledger.post_entry`) | `ledger.discard_revaluation` | `ledger.post_entry` (approver) |
| I-19 | Sharia enforcement off (dual control) | pending | `core.decide_sharia_enforcement_off(approve=True)` | same, `approve=False`, note | `core.disable_sharia_enforcement` via `chairman` only |
| I-20 | ZATCA EGS production switch (dual control) | pending | `core.access.decide_dual_control` | same, reject | `zatca.switch_to_production` (zatca_admin, MP; not requester) |
| Later | Payroll run, loan, bai' ajel, overtime; purification payout | — | payroll / invoicing services not built | — | access 3.6 (A21, A22) |

## 5. Accounting

None. The web module posts no journal entries and touches no accounts. Postings happen only inside the services it calls (`ledger.services.post_entry` and the document registry).

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| ZATCA | — | None directly. Shows e-invoice status, holds and late-reporting tasks from `zatca.services`; actions call zatca services | Read via services |
| Banks | In (file) | Statement files uploaded by a user, passed to `banking.services.import_statement` | Via banking |
| GOSI, Mudad, Qiwa, Falcon (or other legacy system) | — | None | — |
| Agent gateway (`/api/agents/v1/`) | — | Separate module; not served by `web`; agent users refused at login (R12) | Gateway not built |
| Outside web services (CDNs, fonts, analytics, QR APIs) | — | None: all assets self-hosted, QR generated locally (R16, R20) | — |
| E-mail / SMS notifications | — | None in this module (OQ-19) | — |

## 7. Permissions

The web module adds no business permission; each screen needs the permission of the service it calls (access 3.6, hr §7, banking §7). The table lists the MVP screens.

| Screen | View needs | Actions need (seed holders) |
|---|---|---|
| Dashboard cards | the module's view permission in the entity (`invoicing.view`, `zatca.view`, `billing.view`, `hr.view` / `hr.view_counts`, `banking.view`, `ledger.view`) | — |
| Approval inbox, document view | the approving permission of 4.1 | approve / return per 4.1 |
| Customers / vendors | `invoicing.view` | `invoicing.manage_customer` / `manage_vendor` (clerks, accountant) |
| Sales documents | `invoicing.view` | create/edit `create_sales_document`; submit `submit_sales_document` (receivables_clerk, accountant) |
| Bills | `invoicing.view` | `create_bill`, `submit_bill` (payables_clerk, accountant) |
| Receipts / vendor payments, allocation | `invoicing.view` | `post_receipt`; `create_vendor_payment`, `submit_vendor_payment`; `allocate` |
| Bank statement import, matching, bank transactions | `banking.view` | `import_statement`, `match`, `create_bank_transaction`, `submit_bank_transaction`, `prepare_reconciliation` (accountant, group_treasury) |
| Employees, employment contracts, leave, terminations | `hr.view` (masked) | `hr.manage_employee`, `edit_contract_draft`, `submit_contract`, `request_leave`, `record_termination`, `calculate_eosb` (hr_officer); unmask `hr.view_personal_data` (logged) |
| Trial balance, intercompany, subledger reconciliation | `ledger.view` (intercompany: on the group parent); subledger: `invoicing.view` | export: same view right, logged |
| Printable invoice | `invoicing.view` (personal data per invoicing R51, OQ-21) | — |
| Memberships, delegations (J13) | `core.view_access` | `core.grant_membership`, `core.delegate_approval` (access §7) |
| Entity display settings | — | proposed `core.manage_display_settings` (approver, managing_partner; OQ-15) |
| MFA reset of another user | — | proposed: `managing_partner`, or `access_admin` of an entity where the user is a member; never self (OQ-11) |
| Django admin | active, OTP-verified superuser; no business rights (access R24) | technical only |

| Who | Web UI | MFA |
|---|---|---|
| Human with memberships | Yes, scoped | Required per R13 |
| Superuser without membership | Admin only; ERP pages show no entity | Required |
| System user (`billing`, `zatca_queue`, `fx_import`) | Never | — |
| Agent user | Never (gateway only) | — |

## 8. Acceptance tests

Fixtures: entities ALM (parent), SUB (`full`), PART (`full`, 60%), MIN (`equity`), INACT (inactive child, `full`); users per role; agent `agent:invoice_reader`; system users. Anonymised data only. Run in English and Arabic where text is checked.

| # | Given | When | Then |
|---|---|---|---|
| WT-1 | U is `accountant` in ALM only; SUB has documents of every listed type | U opens every list URL under `/e/ALM/` and `/e/SUB/` | ALM lists show only ALM rows; every `/e/SUB/` URL is 404 (R3, R4) |
| WT-2 | Same U; record X belongs to SUB | U opens `/e/ALM/<type>/<X.pk>/` and `/e/SUB/<type>/<X.pk>/` for every detail type | Both 404 with the same bilingual page; no SUB data in the body (R3, R4, R25) |
| WT-3 | G is `group_viewer` on ALM; U is `accountant` in SUB and `viewer` in MIN | Each opens the switcher; U switches from SUB with a half-filled form | G sees ALM, SUB, PART and INACT (a `full` child, marked inactive), not MIN; U sees SUB, MIN; the form data is not carried over (R9, R11) |
| WT-4 | V is `viewer` in ALM | V POSTs submit, approve, create customer, import statement | Each refused with the bilingual `AccessDenied` text, nothing changed, `permission_denied` row per POST (R6) |
| WT-5 | U has no `hr.view` in ALM | U opens the HR menu URL | 403 bilingual page; menu item not shown (R6) |
| WT-6 | C (`accountant` + `approver` in ALM) created and submitted bill B | C opens B; C POSTs approve with a valid CSRF token | No approve button; POST refused by the service (`self_approval`), B stays submitted, `permission_denied` logged; approver P then approves and B posts (R6, R39, R40) |
| WT-7 | Clerk K records receipt R1 without a bank charge and R2 with one | K opens both | K may post R1 (`posted_by` = K); R2 shows no Post button for K and appears in accountant A's inbox; K's forged POST on R2 refused (R42) |
| WT-8 | P is `approver` in ALM without a device | P logs in; opens `/e/ALM/inbox/`; enrols; enters a wrong code 3 times; enters a right code; approves a bill; session OTP flag removed in a test; approves again | Redirected to enrolment, inbox unreachable; `mfa_enrolled` logged; wrong codes throttled and logged `mfa_verify_failed`; after verify, inbox and approval work; unverified approval POST refused (R13 to R15, R26) |
| WT-9 | K is `receivables_clerk` only | K logs in without a device; K is then granted `approver` in SUB | K reaches pages without MFA; on the next request after the grant K must enrol (R13, R14) |
| WT-10 | S superuser (verified, then unverified); T staff non-superuser | Each opens `/admin/` | Verified S allowed; unverified S sent to OTP; T refused (R19) |
| WT-11 | Agent user and system user `billing` with known passwords set in a test; a session forged with the agent's id | Log in via the login form; request a page with the forged session | Both logins fail with the generic message; forged session logged out, redirected to login (R12) |
| WT-12 | U with `preferred_language="ar"` | U opens the dashboard; switches to English by POST; opens it again | First `<html lang="ar" dir="rtl">`, document numbers and IBANs in `<bdi dir="ltr">`; switch saved to the profile and logged; second `lang="en" dir="ltr"` (R27, R29) |
| WT-13 | Code scan | Run the text test | Every `web/texts.py` key has non-empty en and ar; no template has literal text outside the text tag; a model label renders only the active half via `label_part` (R28) |
| WT-14 | Each list page and the dashboard with 5 and with 50 rows per list | Count queries (`django_assert_max_num_queries`) | Same count for 5 and 50; lists ≤ 15, dashboard ≤ 25 (R37, R38) |
| WT-15 | Logged-in user | POST any action without CSRF token; scan `web/` for `csrf_exempt` | 403, nothing changed; scan finds none (R21) |
| WT-16 | Any page | Read headers; scan templates | CSP header as R20, HSTS, `X-Frame-Options: DENY`; no `<script>` without `src`, no `style=`, `on*=`, `hx-on`; no external URL in static references (R20) |
| WT-17 | Approver session idle beyond the idle timeout; another beyond the absolute timeout | Next request | Redirected to login; previous session key invalid; session key differs after login and after OTP verify; logout by GET refused (R17) |
| WT-18 | Username `u1` | 5 failed logins from one IP, then the right password; 61 approval POSTs in a minute | Locked out with a bilingual 429 page; approval POSTs over the limit refused, nothing changed (R18) |
| WT-19 | Fixture employees, customers, bank accounts with made-up personal data | Reverse every URL pattern; crawl every GET page; submit every search form | Path parameters are codes and integer pks only; no fixture personal value appears in any URL or `Location` header; searches are POST (R23) |
| WT-20 | H is `hr_officer` in ALM; A is `accountant` | H opens employee E, presses "Show personal data"; A opens E; H GETs the unmask URL | H sees data, `personal_data_viewed` row, `Cache-Control: no-store`; A sees masked fields only, no button; GET returns 405 (R22, R24) |
| WT-21 | Approver P; contract C submitted by S; leave request L | P returns C with an empty reason; with reason_ar; P rejects L with reason | Empty reason refused; return stored, shown in S's history in both languages; L rejected with reason (R8) |
| WT-22 | MP in ALM (covers SUB, PART); approver P in SUB; documents submitted in ALM, SUB, MIN, some created by P | Each opens the inbox | MP sees ALM, SUB, PART items grouped by entity, not MIN; P sees SUB items except own; counts match the dashboard card (R39) |
| WT-23 | MP M requested Sharia off for SUB; chairman H; approver P | P, M and H open the inbox; H approves; M applies | P and M do not see a decision action; M's forged decide POST refused (`self_approval`); H's approval logged; M's apply sets `sharia_enforced=False` with the access log rows (R41) |
| WT-24 | Z1 requested production for EGS-1 in ALM; Z2 `zatca_admin` in ALM | Z1 opens the request; Z2 approves | Z1 has no decide action, forged POST refused; Z2's decision recorded (R41) |
| WT-25 | P opens submitted invoice X (version 3); K returns, edits and resubmits X (version 5) | P POSTs approve with version 3 | Refused "document changed, review again"; X unchanged; P reloads and approves version 5 (R7) |
| WT-26 | ALM `date_display=gregorian_hijri`; user pref `hijri_gregorian`; HR contract with `calendar_basis=hijri`; a date outside `hijridate` range | Open invoice, contract, and the out-of-range record | Invoice dates Gregorian first for the ZATCA issue date; user pref applied elsewhere; contract dates Hijri first; out-of-range shows Gregorian only (R30, R31) |
| WT-27 | Arabic UI, `digits=arabic_indic`; KWD document (3 dp) and SAR | View amounts; enter "١٢٣٫٤٥٦" in a KWD amount and in a SAR amount | Amounts in Arabic-Indic, IBAN/VAT numbers in Latin; KWD stored `Decimal("123.456")`; SAR entry refused (more than 2 decimals) with en/ar message; no float in the code path (scan) (R33, R34) |
| WT-28 | G `group_viewer` on ALM; U `accountant` in SUB | Open trial balance, intercompany and subledger reports; export TB to CSV | G sees ALM/SUB/PART; U sees SUB TB and is refused the group intercompany report; export is POST and logs `report_exported` (R43) |
| WT-29 | Viewer V in ALM (HR counts only); HR officer H; accountant A | Open dashboards | V sees head counts only, no employee names; H sees expiry alerts; A sees ZATCA, billing, banking cards; no card for a module without view right; all counts from ALM only (R38) |
| WT-30 | Issued ALM invoice I; user of SUB only | ALM user prints I; SUB user opens I's print URL | Print uses the invoicing render, `document_printed` logged; SUB user 404 (R44) |
| WT-31 | Login, dashboard, inbox, document view, invoice form, employee page | Run axe-core in `ar` and `en`; complete approve/return with keyboard only | No serious or critical violations; keyboard flow completes; HTMX result announced (R35) |
| WT-32 | 360 px viewport | Open inbox, approve a bill, reject a leave request | No horizontal scroll; actions reachable; works with JS off by plain POST (R36, W-D2) |
| WT-33 | INACT inactive; U `accountant` there | U opens INACT pages; POSTs a draft invoice | Pages readable, marked inactive; POST refused by the service (`inactive_entity`) (R9) |
| WT-34 | Superuser S without memberships | S logs in to the ERP UI | "No access" page, no entity in the switcher, lists unreachable (R10) |
| WT-35 | Accountant A in ALM | A uploads a statement over `BANKING_MAX_STATEMENT_BYTES`; a valid one; opens the result | Oversized refused before parsing; valid import via `banking.import_statement`; result URL has the import pk only (R23) |
| WT-36 | Logged-in user | Logs out; presses Back | Logout POST ends the session, `logout` logged; Back shows nothing cached (R22) |
| WT-37 | Code scan of `web/` | Run the AST test | No `.objects`, `_base_manager`, raw SQL, `.update(`, `.save(`, `.delete(` on business models (R1) |

## 9. Open questions

**B** = blocks building that part; **GL** = blocks go-live; **D** = a default exists.

| # | Question | Who | Blocks? | Default |
|---|---|---|---|---|
| OQ-1 | Approve the UI architecture (W-D2): Django templates + HTMX, no SPA, no Node build. | Managing Partner / dev | D | As proposed |
| OQ-2 | MFA library and secret storage. django-otp stores TOTP keys and static tokens in plain text. | Dev | D | django-otp (BSD) + `qrcode` (BSD); subclass devices to Fernet-encrypt keys (`WEB_MFA_KEYS`) and hash recovery codes |
| OQ-3 | MFA scope (access OQ-4): confirm the W-D6 list; extend to every holder of **A**, **DC**, **S!** permissions (this would add `hr_officer` and the proposed `department_head`, not `payroll_officer`)? | Managing Partner | **GL** | W-D6 list plus superusers; extension proposed in R13 |
| OQ-4 | HTMX version. | Dev | D | htmx 2.0.x, vendored under `web/static/`, version pinned, file hash checked in a test |
| OQ-5 | Design system and fonts. | Managing Partner / dev | D | Bootstrap 5.3 (MIT) with its RTL build, vendored, no Bootstrap JS where HTMX suffices. *Resolved 2026-10-18 (project owner supplied the Almiras Visual Identity Guide v1.0):* the UI follows `docs/design/brand.md` (navy/sand/orange palette and tokens, logo rules, HT Rakik + Bricolage Grotesque typography, orange never for small text). Bricolage Grotesque (OFL) is self-hosted; HT Rakik Light/Medium are used under Almiras's Hadi Type webfont licence (2026-10-19), served from host-only files at `/brand-fonts/` (never in git or the image), with IBM Plex Sans Arabic (OFL) as the fallback when the files are absent. |
| OQ-6 | Hijri display defaults per entity; Hijri on documents and reports. | Managing Partner / accountant | D | `gregorian_hijri` for every entity; ZATCA/VAT dates Gregorian first; HR Hijri contracts Hijri first |
| OQ-7 | Digits default; Arabic-Indic also in the English UI? | Managing Partner | D | Latin; Arabic-Indic only as a user choice in the Arabic UI |
| OQ-8 | Session timeouts. | Managing Partner | D | Idle 15 min for MFA-required users, 30 min for others; absolute 10 h |
| OQ-9 | Rate-limit library and thresholds. | Dev | D | django-axes: 5 failures per username + IP → 15 min lock; django-ratelimit: 60 approval/post POSTs per minute, 10 statement imports per hour, 20 exports per hour per user |
| OQ-10 | "Admin for superusers only" (W-D7) removes the only current screen where the MP and `access_admin` grant and revoke memberships (`core/access/admin.py` `MembershipAdmin`, staff users); superusers have no business rights (access R24) and cannot grant. | Managing Partner | **B** for locking admin to superusers | Build J13 (memberships, delegations) in the MVP; lock admin (R19) only when J13 ships |
| OQ-11 | Who resets a lost MFA device; one or several devices per user. | Managing Partner | D | MP, or `access_admin` of an entity where the user is a member, never self, logged; superusers by a management command with a reason; one device |
| OQ-12 | "Return with reason" has no service for bills, vendor payments, receipts, assignments, terminations (approver side), statutory parameters, bank accounts, bank transactions, reconciliations and manual journals; `invoicing.return_to_draft` stores no reason. May the approver send these back, to which state, and is the reason stored? Services take `reason_ar` required: is that right for English-only users? | Managing Partner / accountant | **B** for the return action on those types (approve works) | Each gets `return_<type>(doc, *, user, reason_ar, reason_en="")` → back to draft, reason stored in history, by the approver or the submitter; Arabic reason required as in billing and hr |
| OQ-13 | Inbox: all the user's entities at once, or the current entity only? | Managing Partner | D | All entities, grouped by entity (R39) |
| OQ-14 | The invoicing slice 2 HTML render does not exist yet (no render module in `invoicing/`). | Dev | **B** for the print journey (J7 print) | Build J7 without print; add print when the render lands |
| OQ-15 | Who changes entity display settings (new permission `core.manage_display_settings`)? | Managing Partner | D | `approver` and `managing_partner`, logged |
| OQ-16 | Accessibility and mobile test tooling in CI. | Dev | D | axe-core (MPL-2.0) through Playwright (Apache-2.0), headless Chromium, in a separate CI job |
| OQ-17 | Is a POST-only free-text search acceptable (no bookmarkable search results)? | Managing Partner | D | Yes (R23) |
| OQ-18 | Payroll screens (runs, loans, bai' ajel, overtime) and purification payouts. | Managing Partner | D | Later, when payroll and invoicing slice 2 are built; inbox gets I-21+ |
| OQ-19 | Notify approvers by e-mail or SMS of new inbox items? | Managing Partner | D | No; dashboard and inbox counts only |
| OQ-20 | UI text mechanism: `web/texts.py` en/ar pairs (consistent with `bi()`, no gettext tooling installed) or Django gettext `.po` files. | Dev | D | `web/texts.py` pairs, tested for both languages |
| OQ-21 | Printing a simplified (B2C) invoice shows the buyer's personal data: does printing need `invoicing.view_personal_data`? | Managing Partner + legal (PDPL) | D | As the invoicing render decides; print logged |
| OQ-22 | The chairman's decision view shows the request data only (access OQ-24): enough to decide? | Managing Partner | D | Yes: entity, change, reason, requester, expiry |
| OQ-23 | Step-up MFA (a fresh code) for each approval, or session-level verification? | Managing Partner | D | Session-level (R15) |
| OQ-24 | Arabic UI texts need review. | Arabic reviewer | GL | Marked [AR-review] in `web/texts.py` |
| OQ-25 | Report exports: CSV only, or also XLSX? | Accountant | D | CSV in MVP; XLSX later (openpyxl, MIT) |

**Blocking:** OQ-10 (admin lock-down needs J13), OQ-12 (return action on 10 item types), OQ-14 (print). **Go-live:** OQ-3 (MFA scope, access OQ-4), OQ-24 (Arabic review).

## Appendix A: user journeys and MVP

| # | Journey | Screens | MVP |
|---|---|---|---|
| J1 | Login, MFA enrol/verify, recovery code, logout, session timeout | login, enrol, verify, recovery | **P1** |
| J2 | Language switch, date and digit preferences | header switch, preferences | **P1** |
| J3 | Entity switcher | header control, "no access" page | **P1** |
| J4 | Dashboard per role (cards by permission): pending approvals; ZATCA queue, holds, rejected/failed, late-reporting tasks; billing held/failed items, unresolved differences; HR document expiry alerts, head counts; banking unmatched lines, reconciliation status per account/period; subledger tie-out | dashboard | **P1** |
| J5 | Approval inbox, document view, approve, return/reject with reason, dual-control decisions (4.1 I-1 to I-20) | inbox, document view, decision dialog | **P1** (return only where a service exists, OQ-12) |
| J6 | Customers and vendors | list, detail, form | **P1** |
| J7 | Sales invoices, credit/debit notes: draft, lines, submit, status, print | list, form, lines (HTMX), detail, print | **P1** (print after OQ-14) |
| J8 | Vendor bills and credit notes: draft, lines, attachment, submit | list, form, detail | **P1** |
| J9 | Receipts and vendor payments, allocation | list, form, allocation | **P1** |
| J10 | Bank statement import, matching (suggested, confirm, reject, manual, undo), bank transactions from lines | import, line list, match panel, transaction form | **P1** |
| J11 | Reports: trial balance, intercompany reconciliation, subledger reconciliation | report pages, CSV | **P1** |
| J12 | HR officer: employees (masked, unmask), employment contracts (draft, submit), leave requests, terminations (record, EOSB preview, submit) | lists, forms, detail | **P1** |
| J13 | Access: memberships (list, grant, revoke), CFO delegation, own MFA reset by others | list, forms | **P1** (OQ-10) |
| J14 | Bank reconciliation preparation and submission; bank accounts | forms | P2 |
| J15 | Billing contracts, versions, runs, correction review | forms | P2 |
| J16 | Manual journals, FX rates, revaluation, period close | forms | P2 |
| J17 | ZATCA admin: profile, EGS units, onboarding, archive export, late notices | forms | P2 |
| J18 | Entity settings (MP): Sharia on/off request, other settings | forms | P2 |
| J19 | Payroll | — | P3 (OQ-18) |

## Appendix B: existing code that must change (for the module-builder)

| File | Change |
|---|---|
| `config/settings.py` | Add `web`, `django_otp`, `otp_totp`, `otp_static`, `axes`; middleware `OTPMiddleware`, `axes`, CSP (`django.middleware.csp`), `LocaleMiddleware`, `web.middleware` (human-only session, MFA gate, no-store); `SECURE_CSP`, session and cookie settings, HSTS; `TEMPLATES` context processor for language/direction |
| `config/urls.py` | Include `web.urls`; admin site restricted per R19 |
| `requirements.txt` | `django-otp`, `qrcode`, `django-axes`, `django-ratelimit` (all open source); dev: `playwright`, `axe-core` (OQ-16) |
| `core/access/services.py` | `set_preferred_language(user, language)` (logged `profile_changed`); `visible_entities(user)` (union of `entities_for` over the catalogue) |
| `core/access/models.py` + migration | New `AccessLog.event` values (3.4) |
| `invoicing`, `billing`, `hr`, `banking`, `ledger`, `core` services | `pending_approvals(user)` per app returning scoped, `select_related` rows for 4.1; scoped getters per detail type; `expected_version` (or `expected_modified_at`) on approve/return/post/decide (R7) |
| `ledger/services.py` | `approve_entry(entry, *, user)` wrapping `require(..., "ledger.post_entry", document=entry)` + `post_entry` (I-17) |
| Services listed in OQ-12 | Return/reject services with stored reasons, once decided |
| List services (`customers`, `vendors`, `sales_documents`, `purchase_documents`, `employees`, `statement_lines`, `contracts`, …) | `select_related` / `prefetch_related` so list pages meet R37 |
| Dashboard counts | Count functions per app (`zatca` queue/hold/task counts, `billing` held/failed, `hr` expiry flags in bulk, `banking` unmatched lines and reconciliation status) so the dashboard meets R38 without per-row service calls |

## As built: slice 1 (2026-10-20)

Built by the module-builder on the caller's slice-1 brief (the spec has no slice plan of its own): the frame, sign-in, entity switcher, dashboard and **read-only** lists and details of journal entries and sales documents, with the Almiras brand (`docs/design/brand.md`). Nothing in this slice creates, changes, submits, approves or posts a business record; the only POSTs are sign-in, sign-out, the language switch and the entity switch. Status of the spec stays **draft**; the open questions below still need answers.

**Screens and URLs** (`web/urls.py`; path parameters are entity codes and integer pks only, R23):
- `/login/`, `/logout/` (POST only), `/language/` (POST), `/switch-entity/` (POST), `/` (start page: the remembered entity, else the first visible one, else "no access", R10).
- `/e/<code>/` dashboard: one card per module the user may view in that entity (`ledger.view` → journal entries by status, `invoicing.view` → sales documents by status), each with an empty state; no card at all → "no overview cards" (R38).
- `/e/<code>/ledger/entries/` and `…/<pk>/`; `/e/<code>/sales/documents/` and `…/<pk>/`: GET filters (status, source / type, ZATCA status, date range, document number; invalid values ignored and listed), 50 rows a page with the filters kept in the page links (R37), amounts with their currency code and the currency's minor units in `<bdi dir="ltr">` (R29, R34), status as text + shape + colour (R35).
- Bilingual 400, 403 (with the access layer's own `message_en` / `message_ar`), 404, 405, 429 and 500 pages, the same for "elsewhere" and "nowhere" (R25); CSRF failures show the bilingual 403.

**Rules built** (tests in `web/test_*.py`): R1 to R5 (thin views; AST scan of views, forms, tags, middleware: WT-37), R9 to R12, R17 (cookies Secure outside DEBUG or with `DJANGO_SECURE_COOKIES=1`, HttpOnly, SameSite=Lax; session key rotated at sign-in; idle 30 min, 15 min for holders of an MFA role (R13 list), absolute 10 h; sign-out POST only), R18 for sign-in only (5 failures per username + IP, 20 per username → 15 min lock, bilingual 429), R20 (Django's built-in CSP with the exact policy of R20 plus `font-src 'self'`; `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, HSTS one year on HTTPS; no inline script or style, no outside URL), R21, R22, R23, R24 (lists and GET details mask individual buyers for everybody, write no personal-data row), R25, R26 for `login_succeeded` / `logout` only, R27 to R29, R30 partly (Gregorian ISO dates only), R32, R34 for display, R35 partly, R36 partly, R37, R38.
**Acceptance tests covered:** WT-1, WT-2, WT-3, WT-5, WT-11, WT-12, WT-13, WT-14, WT-15, WT-16, WT-17, WT-18 (sign-in part), WT-19 (URL patterns, GET crawl, no fixture personal value in a link or `Location`), WT-33 (read part), WT-34, WT-36, WT-37. Not applicable to a read-only slice or not built: WT-4, WT-6 to WT-10, WT-20 to WT-32 except as listed, WT-35.

**Data model.** `web.UiPreference` as 3.1 (only `last_entity` is used so far; `digits` and `date_display` are stored defaults); `web.LoginThrottle` (keyed HMAC of lower-cased username + IP, failures, first failure, locked until; no entity: nothing is known before sign-in). `EntityDisplaySettings` (3.2) and the MFA devices (3.3) are not built. `AccessLog.event` gains `login_succeeded` and `logout` (migration `core/0026`).

**Service changes outside `web`** (appendix B, read side only): `core.access.services.visible_entities(user)` (one pass over the user's active memberships; same coverage as `entities_for`, inactive entities included and marked) and `set_preferred_language(user, language)` (own profile only, logged `profile_changed`); `ledger.services.journal_entries(entity, *, user, …filters)`, `journal_entry(entity, pk, *, user)` and `entry_counts(entity, *, user)`; `invoicing.services.list_sales_documents(entity, *, user, …filters)`, `sales_document_detail(entity, pk, *, user)`, `sales_document_counts(entity, *, user)`, `party_list_name(party, language)` and `PRICED_STATUSES`. Each list/detail/count service calls `require(…, "<module>.view")` (refusal → 403, logged) and then filters through `scope` / `get_scoped_or_404` (another entity's record → 404). `core/test_access_scan.py` allow-lists the new helpers with reasons, and now scans paths relative to the repository (a checkout under a dot-folder, such as a git worktree in `.claude/`, made it skip every module).

**Brand.** Bootstrap 5.3.8 (MIT; LTR and RTL builds), Bricolage Grotesque 700 (OFL, Fontsource 5.3.0, Latin and Latin-Ext subsets) and IBM Plex Sans Arabic 300/400/500/700 (OFL, `@ibm/plex-sans-arabic` 1.1.0) are vendored byte for byte under `web/static/web/` with their licence files; `web/test_brand.py` pins their SHA-256. HT Rakik is referenced only by the two `@font-face` rules of brand.md (`/brand-fonts/…`); no HT Rakik file is in git or the static folder (tested), and the stacks fall back to IBM Plex Sans Arabic when the files are absent (checked in a browser: the two `/brand-fonts/` requests 404 and the page renders in Plex). Theme: brand tokens as CSS variables on Bootstrap (navy primary and sidebar, sand page, white cards and inputs, orange only for the large page title, the active-item bar and the focus ring; links and small accent text orange 4; muted navy 2 only on white); CSS logical properties, so one stylesheet serves both directions. Logos: Arabic logo in the RTL UI, English in the LTR UI, never together; the navy sidebar uses the "name in white, symbol keeps its gradient" variant; below 992 px the sidebar becomes a top bar with the symbol alone (less than 140 px available); favicon and empty states use the symbol. The variants are derived from the brand files by `web/brand_assets.py` (fill change or viewBox crop only), and a test keeps the served files equal to the derivation. Contrast pairs used for small text are tested at ≥ 4.5:1.

**Deviations and gaps (for review):**
- **MFA is not built** (R13 to R16, R19; WT-8 to WT-10). *Superseded by slice 2 (below): MFA is built and on by default; `WEB_ALLOW_PASSWORD_ONLY_LOGIN` is removed.* Holders of `approver`, `managing_partner`, `chairman`, `zatca_admin`, `access_admin`, `ceo` (added 2026-10-21 with the access §10 build: a subsidiary's default top authority) and superusers can sign in with a password only. The slice is read-only, but it shows business data: **do not deploy the web UI to real users before the MFA slice** (go-live blocker, with OQ-3). *Review fix 2026-10-20:* the deploy check `web.E001` (`manage.py check --deploy`, run by `release-check`) is an Error while DEBUG is off unless `WEB_ALLOW_PASSWORD_ONLY_LOGIN` (`DJANGO_WEB_ALLOW_PASSWORD_ONLY_LOGIN=1`) is set explicitly; default off in `deploy/docker-compose.yml` and `env.example`, set to 1 only in CI's deploy-check stack. It is a deploy-only check, so `migrate`, scheduled jobs and the test suite are unaffected.
- **Lockout without django-axes** (OQ-9 default named axes): `AxesStandaloneBackend` refuses `authenticate()` calls without a request, which the access tests use (`core/test_access.py`), so slice 1 has its own `LoginThrottle`. *Review fixes 2026-10-20:* `web.services.sign_in` locks the throttle rows (`select_for_update`, created when missing) in one transaction, refuses a locked pair or username without checking the password, counts the attempt **before** checking it, and clears the pair on success, so concurrent requests cannot get more than 5 guesses (PostgreSQL test `web/test_pg_concurrency.py`). Limits: 5 failures per username + IP and **20 per username from all addresses** within 15 minutes, each locking for 15 minutes (bilingual 429 pages; the username lock has its own message). **Trade-off:** the per-username cap bounds guessing spread over many addresses, but anyone who knows a username can lock it for 15 minutes by failing 20 times (a denial of service on that account, not a way in); a right password does not reset it. Stale rows are purged on every attempt. The `HumanModelBackend` runs the password hasher once for unknown, system, agent and inactive accounts alike, so response times do not tell them apart. Per-user limits on approval POSTs, imports and exports wait for those actions.
- **Group roles still cover `full` descendants** in `entities_for` as built (R6 before A27); the switcher follows it, which is what WT-3 expects. When A27 ("no cascade") is built, `visible_entities` follows automatically and WT-3's expectation changes with it.
- **Arabic is the default only before sign-in.** After sign-in the language is `UserProfile.preferred_language`, whose model default is still `en` (OQ-26).
- **Personal data not loaded (review fix).** The list and detail services defer the buyer snapshot and the customer's personal fields (R24 "never prefetched"); business and government names come as annotations, and a business buyer's snapshot is read in one query only when shown. Sign-outs by idle or absolute timeout are logged as `logout` with `reason` (`idle_timeout`, `absolute_timeout`; `sign_out` for the button). Page links carry only the filter form's own valid fields.
- **No unmask action yet.** Individual buyers are masked on every GET page, also for holders of `invoicing.view_personal_data`; the logged "show personal data" POST of R24 comes with a later slice.
- **Totals before issue.** `approve_sales_document` sets the header totals, SAR amounts and buyer snapshot at issue; draft and submitted documents show "calculated at issue" instead of zero totals (their lines are priced and shown).
- **Journal entry status labels** come from `web/texts.py` (`je_status.*`) because `JournalEntry.Status` labels are English only; a later ledger change could make them bilingual `bi()` labels.
- **Last-seen time.** The idle-timeout clock is kept in the session and written at most once a minute, so a GET may update the session row (no business row; the GET crawl test counts business rows).
- **HTMX** is not vendored yet (nothing in slice 1 needs it); `web/static/web/js/app.js` only opens and closes the menu on narrow screens and the page works without it.
- **The supplied brand shape is another company's logo.** `docs/design/brand/almiras-brand-shape.svg` draws a "Rabeez / ربيز" logo, not an Almiras graphic, so it is not served or shown anywhere (tested); the owner should supply the right supporting shape.
- Not built: Hijri dates and Arabic-Indic digits (R30, R33), approval inbox and document actions (R39 to R42), reports and print (R43, R44), admin lock-down (R19), axe-core and 360 px automation in CI (R35, R36; checked by hand with Playwright: no horizontal scroll at 360 px).

**New open questions:**

| # | Question | Who | Blocks? | Default |
|---|---|---|---|---|
| OQ-26 | Should a new user's `preferred_language` default to Arabic (model default is `en`; changing it changes the language of every access-layer message for users created without one)? | Managing Partner | D | Keep `en` until decided; Arabic before sign-in |
| OQ-27 | Behind the TLS proxy, which header carries the client IP for the sign-in lockout? *Resolved as built 2026-10-20 (review):* with `DJANGO_BEHIND_TLS_PROXY=1` the address is `X-Real-IP`, which Caddy writes for the app route (`header_up X-Real-IP {remote_host}`, overwriting any client value); `X-Forwarded-For` is never read; otherwise `REMOTE_ADDR`. | Dev / deployment | — | As built |
| OQ-28 | `docs/design/brand/almiras-brand-shape.svg` is a "Rabeez" logo. Replace with the Almiras supporting shape from the identity guide? | Project owner | D | No supporting shape in the UI |

## As built: slice 2 (2026-10-21)

Built by the module-builder on the owner's slice-2 brief ("proceed on recommendations, no questions"): MFA (R13 to R16, R19) and the approval inbox (R6 to R8, R15, R39, R40) on the access engine (access §10). Status of the spec stays **draft**.

**MFA (R13 to R16, R18, R19, R26).**
- Libraries: `pyotp` 2.10 (MIT) for TOTP (RFC 6238: 6 digits, 30 s steps, one step of drift either way) and `segno` 1.6 (BSD-3-Clause) for the enrolment QR code, drawn on the server as an SVG data URI (no outside service). Both pure Python without dependencies; pinned with hashes in `deploy/requirements.lock`. *Deviation from OQ-2's default (django-otp + `qrcode`):* django-otp stores TOTP keys and recovery tokens in plain text, so it would have needed subclassed models over a plain-text parent table; one small own model with an encrypted field is simpler to review. `qrcode` was not used because its 8.x package metadata carries an "Other/Proprietary License" classifier next to BSD; `segno` is plainly BSD.
- Data: `web.MfaDevice` (one per user, OQ-11; secret **Fernet-encrypted** by `web/crypto.py`, the same design as `hr/crypto.py` and `banking/crypto.py` with its own keys `WEB_MFA_KEYS` (Docker secret `web_mfa_keys`, comma-separated, first = current); `last_counter` so each code works once; throttle fields) and `web.RecoveryCode` (10 per enrolment, keyed HMAC-SHA256 only, single use). No `entity` FK: they follow the person, like `UiPreference`. Migration `web/0003_mfa_devices`.
- Who (R13): superusers and holders of an active membership in `web.services.MFA_ROLES` (`approver`, `managing_partner`, `chairman`, `zatca_admin`, `access_admin`, `ceo`), read at every request (one membership query, shared with the idle timeout). OQ-3's extension (A/DC/S! holders, e.g. `hr_officer`) is **not** applied: still open for the MP. Staff users may enrol too (admin, R19); other people do not.
- Gate (R14): until the session is verified, a signed-in MFA user reaches only `/mfa/…`, the language switch and sign-out; anything else redirects to enrolment or verification (with `next`, same-site only) and does nothing. A membership granted mid-session applies at the next request (WT-9). The session stores the verifying device id and is valid only while that device exists, so a reset ends it at the next request. The session key rotates at verification and at enrolment (R17).
- Enrolment (R16): `/mfa/enrol/` (POST starts or restarts with a new 160-bit secret), QR plus the grouped key for typing; a right code confirms the device, verifies the session and shows the 10 recovery codes on that response only (never stored in clear; `no-store`). `/mfa/` shows the device and the codes left and renews the codes with a fresh code (old ones stop working). A confirmed device cannot be re-enrolled by its owner.
- Verification (`/mfa/verify/`): TOTP or a recovery code. Throttle (R18): each wrong code doubles the wait (1, 2, 4, 8 s); 5 in a row lock the device for 15 minutes (`WEB_MFA_MAX_FAILURES`, `WEB_MFA_LOCK_SECONDS`); a try during the wait or the lock is refused without checking (bilingual 429). The device row is locked for the check. Arabic-Indic digits are accepted (R33).
- **Step-up (R15, owner instruction):** approving in the inbox needs a fresh authenticator code on the approve form, on top of the verified session, from everyone who must use MFA (replaces the OQ-23 session-only default). A recovery code is not accepted for a step-up. The approve and send-back views also check the verified session themselves (defence in depth behind the middleware).
- Reset (R16, OQ-11 default): `web.mfa.reset_device(actor, user, reason=…)`: never oneself; a `managing_partner`, or the `access_admin` of an entity where the user holds an active membership; not for superusers, who are reset by `manage.py web_mfa_reset <username> --reason …` (logged with the OS user). No reset screen yet (J13).
- Logging (R26): `AccessLog.event` gains `mfa_enrolled`, `mfa_verified` (purpose `sign_in` / `step_up` / `renew_recovery_codes`), `mfa_verify_failed` (reason `wrong_code` / `throttled` / `locked`), `mfa_recovery_code_used`, `mfa_recovery_codes_renewed`, `mfa_reset` (core migration `0033_mfa_events_decision_note_en`). No code, secret or recovery code is ever logged (tested).
- Admin (R19): `/admin/` is reached only through the web sign-in (anonymous → `/login/`, so the R18 throttle applies) and only with an OTP-verified session, for staff users and superusers alike; non-staff get the bilingual 403. Limiting admin to superusers (R19 in full) is behind `DJANGO_WEB_ADMIN_SUPERUSERS_ONLY=1`, off by default because OQ-10 (J13) still blocks it: staff access admins grant memberships there. Tests of other modules' admin pages use `force_login`, so `config/settings_test.py` sets `WEB_ADMIN_REQUIRE_MFA = False`; `web/test_mfa.py` tests the gate switched on.
- Deploy checks: `web.E001` now fails (DEBUG off) only when MFA is switched off (`DJANGO_WEB_MFA_ENFORCED=0`; default 1) or the admin gate is off; `web.E002` fails when `WEB_MFA_KEYS` is empty. `WEB_ALLOW_PASSWORD_ONLY_LOGIN` / `DJANGO_WEB_ALLOW_PASSWORD_ONLY_LOGIN` are removed from settings, compose, `env.example`, the deploy README and the CI deploy-check workflow; `init-secrets.sh` generates `web_mfa_keys`, the entrypoint reads `WEB_MFA_KEYS_FILE`, and web, scheduler and migrate mount it.

**Approval inbox (R5 to R8, R15, R23, R24, R39, R40).**
- Service layer: `core/access/inbox.py` (access engine services; the views never read or write requests, decisions or documents): `pending(user, entity)`, `item(user, entity, pk)`, `decide(user, entity, pk, version=…, approve=…, reason_ar=…, reason_en=…)`. Engine additions in `core/access/engine.py`: `can_decide` (a side-effect-free mirror of `authorise`: closed, stale version, beneficiary R82, eligibility by own membership in that entity R64 or by delegation R62/R74/R88, one person per request R75, self-approval only for the single top authority R79 to R81; it writes and logs nothing), `document_of`, `send_back_action`, `current_version`; `engine.decide(..., note_en=…)`, and the reason is now stored on the return / reject decision also when the module's own `require(purpose=…)` closes the request (new field `ApprovalDecision.note_en`).
- URLs: `/e/<code>/inbox/`, `/e/<code>/inbox/<pk>/`, `…/approve/` (POST), `…/send-back/` (POST). Per entity (resolved among the visible ones; another entity's request is 404). The list shows the open requests of that entity the user may decide now, including as a delegate ("on behalf of"), and leaves out their own documents unless the engine would accept a top-authority self-approval (then marked). A user who holds the request's approval right in the entity (e.g. its maker) may open it read-only with the reason they cannot decide; anyone else gets 404.
- Every decision is one `inbox.decide` call → `engine.decide` → the module's approve / return / reject service through its adapter, so four-eyes, matrix steps, delegation, beneficiary and NSA rules stay the engine's and the modules'. A refusal shows the access layer's bilingual 403 (logged `permission_denied` on the separate connection); module validation errors are shown on the page. The form carries the request's document version; a changed document or closed request is refused "reload and review again" (409, R7).
- Send back: return or reject, whichever the module's adapter offers; reason in Arabic required, English optional (R8, OQ-12 default), stored on the engine decision (`note`, `note_en`), passed to the module as its `reason_ar`, and shown in the request history in both languages. Types without a module service (bills, vendor payments, bank items, …; OQ-12) offer approve only.
- PDPL (R24): the inbox shows the engine's labels (type and number, never a person's name), amount, submitter, dates and source; no counterparty or buyer data (tested with an individual buyer). The counterparty column of R39 is therefore not shown.
- Brand: the MFA pages use the sign-in card; tables stack into labelled cards below 576 px (checked with Playwright at 360 px: no horizontal scroll); orange only in the large title and focus ring; status and badges by text, not colour only.

**Tests:** `web/test_mfa.py` (WT-8, WT-9, WT-10; R13 every MFA role; R14 open paths; R15 view check; R16 TOTP drift and replay, encryption at rest, recovery codes hashed and single use, renewal, no self re-enrolment, reset rules and logging, operator command; R17 key rotation; R18 lock and step-up failures; R26 no secrets in logs; R33 digits; MFA switched off), `web/test_inbox.py` (other entities invisible; no group cascade for approvals; viewers; agents; own documents not listed, forged approval refused and logged (WT-6); step-up; delegate path; return with reason (WT-21); stale version (WT-25); POST only and CSRF; GET writes nothing; no personal data; both languages), `web/test_security.py` (E001 / E002, deploy files; scans extended to `web/mfa.py`, the inbox services and the reset command), `core/test_access_scan.py` (new modules allow-listed with reasons). The web fixtures sign MFA users in through the real verification form with codes from a test clock.

**Gaps and deviations (for review):**
- **Sales-document amounts before issue are 0.** The invoicing adapter measures `total_gross`, which `approve_sales_document` computes only at issue, so the inbox shows 0.00 for submitted sales documents, and matrix bands for `invoicing.sales_document` would be resolved on 0 (R71, R73). Needs an invoicing / engine fix (e.g. measure the priced lines at submission); not changed here.
- Dual-control requests (Sharia off, ZATCA production; I-19, I-20, OQ-49) are not in the inbox yet; documents without a submit step (leave requests) appear only once their request opens. The inbox is per entity (the brief), not all entities at once (OQ-13 default).
- The inbox evaluates eligibility per open request, so its query count grows with the open requests of the entity (R37 not met for the inbox; paginated at 50).
- Per-user rate limits on approval POSTs (R18 second half, WT-18) are not built; the step-up code throttle bounds them for MFA users.
- No MFA reset screen (needs J13); service and operator command only.
- `deploy/requirements.lock` also lacks `reportlab` and `openpyxl` (in requirements.txt, used by the access reports and migration); not changed here, flagged for the owner of the lock file.

**New open questions:**

| # | Question | Who | Blocks? | Default |
|---|---|---|---|---|
| OQ-29 | Step-up for every approval also for approvers outside the R13 list (e.g. `hr_officer` for leave)? They have no device unless OQ-3 adds them. | Managing Partner | D | Only users who must use MFA (R13 list) give a step-up code |
| OQ-30 | Lock threshold and length for MFA codes (5 wrong → 15 min); alert the access admin on a lock? | Managing Partner / dev | D | As built; no alert |
