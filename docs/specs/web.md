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
| OQ-5 | Design system and fonts. | Managing Partner / dev | D | Bootstrap 5.3 (MIT) with its RTL build, vendored, no Bootstrap JS where HTMX suffices. *Resolved 2026-10-18 (project owner supplied the Almiras Visual Identity Guide v1.0):* the UI follows `docs/design/brand.md` (navy/sand/orange palette and tokens, logo rules, HT Rakik + Bricolage Grotesque typography, orange never for small text). Bricolage Grotesque (OFL) is self-hosted; HT Rakik is used only after Almiras confirms a web licence that allows self-hosting, with IBM Plex Sans Arabic (OFL) as the fallback until then. |
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
