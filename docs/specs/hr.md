# HR: employees, contracts, secondments, leave, termination and end-of-service

Django app: `hr`. Status: **draft spec**. Written 2026-10-17 by the spec-writer agent. Amended 2026-10-17 (project-owner decisions H9 to H16: employment basis per person, currency, cut-off, departments/grades and overtime approval, loans, annual leave 21/30, `hr_officer` / `payroll_officer` roles, salary banks; 3.1, 3.2, 3.4, 3.11, 3.12, R5, R18, R21, R49 to R57, AT-26 to AT-32, OQ-1, OQ-2, OQ-3, OQ-5, OQ-26 to OQ-31).
Depends on: `core` (Entity, Currency, access: `docs/specs/access.md`), `docs/specs/billing.md` 3.6 (SecondmentAssignment interface this spec fills), `docs/specs/payroll.md` (pay, GOSI, final settlement, all postings). Companion: `docs/specs/payroll.md`.
**[CC-verify]** = Saudi regulatory figure or statement (Labour Law, GOSI, Mudad/Qiwa, PDPL) written from memory, not checked against gosi.gov.sa, hrsd.gov.sa, mudad.com.sa or sdaia.gov.sa (not reachable from this environment). The compliance-checker must verify each one before module-builder relies on it. Every such figure is stored as a `StatutoryParameter` row (3.9), never a code constant, so a correction is data.

## 1. Purpose

The HR module keeps the employee master and employment records for **every entity** in the group (Almiras, subsidiaries, partly-owned entities, and any entity added later as an `Entity` row): employees (Saudi and non-Saudi), versioned employment contracts with salary components, assignments and secondments to other group entities (feeding billing's `SecondmentAssignment` and its `end_secondment` event), leave entitlements and balances, terminations, the end-of-service benefit (EOSB) calculation, and expiry tracking of iqama, passport and work-permit documents. Statutory figures are dated data per jurisdiction; only the Saudi (`SA`) jurisdiction is built. HR posts no journal entries: payroll (`docs/specs/payroll.md`) turns HR data into pay, GOSI, EOSB accruals and final settlements through `ledger.services`. Personal data is handled under PDPL (masked, encrypted, logged). Every record has an `entity`; no HR record is visible across entities; every user-facing text is in English and Arabic.
*Amended 2026-10-17 (H9):* every person has an **employment basis**: `ksa_employee` (Saudi rules), `local_entity_employee` (employee of a group company registered outside KSA, that country's rules, never Saudi rules) or `remuneration` (cross-border person paid a monthly remuneration net of WHT by a Saudi entity; no labour regulation). Non-SA statutory parameters are not yet supplied (OQ-26).

## 2. Decisions

H1 to H8 come from existing documents; H9 to H16 are the project owner's HR/payroll decisions of 2026-10-17 (same numbers as payroll P8 to P15).

| # | Decision | Source | Date |
|---|---|---|---|
| H1 | Every business record has an `entity` FK; views/APIs filtered by entity and permission; new subsidiaries are data; all user-facing text en/ar; `Decimal` money with explicit currency (SAR 2 dp); business logic in `services.py`; every rule has a test; agents never touch production data. | CLAUDE.md | in repo |
| H2 | Staff create documents, the CFO approves, posting is automatic on approval; roles are assigned per entity. | Project owner (access A1; invoicing D17; billing D5) | 2026-10-16 |
| H3 | AI agents act as named service users through the gateway only, create drafts only, never approve, post or issue; every action logged. | CLAUDE.md; access A6, A7 | 2026-10-16 |
| H4 | Seconded staff are billed to the receiving entity **until HR ends the secondment**. | Project owner (billing D1(c)) | 2026-10-16 |
| H5 | Until the HR module exists, accounting staff record the secondment end date (no CFO approval for secondment end). | Project owner (billing D6) | 2026-10-16 |
| H6 | Subsidiaries are separate companies; partly-owned and minority entities are separate legal persons (so each is its own employer). | Project owner (access A4; invoicing D11) | 2026-10-16 |
| H7 | All hosting (DB, backups, logs, secret store) is in KSA. | Project owner (invoicing D12) | 2026-10-16 |
| H8 | Proposed account codes stay proposed until the accountant/CFO reviews them. | Project owner (ledger-fx D10; invoicing D29) | 2026-10-17 |
| H9 | "All group companies have employees, not all in Saudi Arabia." Staff outside KSA are either (a) employees of a locally registered group company, under that country's labour law and local payroll, held per entity with country-specific statutory parameters; Saudi rules never apply to them; or (b) "remuneration" staff paid from a Saudi entity as a monthly remuneration with withholding tax considered: no labour regulation (no GOSI, no EOSB, no Saudi leave), just a monthly remuneration net of WHT. Basis per person: `ksa_employee`, `local_entity_employee`, `remuneration`. Egypt/China/UAE parameters to be supplied. | Project owner | 2026-10-17 |
| H10 | Foreign-currency pay: no for locally registered entities (they pay in their own functional currency); yes for remuneration staff (paid in a foreign currency from the Saudi entity). | Project owner | 2026-10-17 |
| H11 | Pay cycle monthly for all. Cut-off for changes: the 27th of the month for local (in-country) payroll; the 6th of the following month for out-of-country payroll. | Project owner | 2026-10-17 |
| H12 | Overtime only in very special circumstances; not allowed for certain grades (e.g. middle management and management); approved by the Managing Partner in Almiras and by the department head in other companies. | Project owner | 2026-10-17 |
| H13 | Loans: all approved by the Managing Partner; profit-free loan up to 3 basic monthly salaries; larger amounts by Sharia-compliant bai' ajel (deferred-payment sale) secured by salary and EOSB. Interest and late fees never allowed. Detail in payroll P12. | Project owner | 2026-10-17 |
| H14 | Annual leave: statutory minimum 21 days; some employees get 30; never more than 30. | Project owner | 2026-10-17 |
| H15 | The HR officer approves the payroll (not the CFO); the preparer (payroll officer) cannot approve; posting automatic on approval. Roles `hr_officer` and `payroll_officer` exist; the Managing Partner grants them. | Project owner | 2026-10-17 |
| H16 | Salary banks: Riyad Bank, Saudi National Bank (SNB), Al Rajhi Bank, Bank AlJazira, Alinma Bank; WPS/salary file per bank. | Project owner | 2026-10-17 |

Everything else (approvers for contracts, assignments and terminations, leave policy beyond H14, non-Saudi parameters, alert thresholds, PDPL retention) is in section 9 with a safe default.

## 3. Data model

Conventions: every model has `entity` FK → `core.Entity` (`PROTECT`, required) = the **employing** entity, plus `created_by` (non-null FK user), `created_at`, `modified_by`, `modified_at`, `source` (`human` / `import` / `system`; no `agent`, R13). Money `DecimalField(18, 2)` in `entity.base_currency`. "Bi" = `_en` and `_ar` fields. "PD" = personal data: encrypted at rest, masked without `hr.view_personal_data` (R10).

### 3.1 `HrSettings` (one per entity)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | OneToOne Entity | Y | | |
| jurisdiction | Char(2) ISO 3166 | Y | | Default = `entity.country`. Only `SA` has parameters (R5, OQ-26) |
| has_employees | Bool | Y | | HR records refused while False. *Amended 2026-10-17 (H9):* every group company has employees; `hr_setup_entity` sets True |
| overtime_approval_route | Choice `managing_partner`, `department_head` | Y | | *Added 2026-10-17 (H12, R55).* Set by `hr_setup_entity`: `managing_partner` for Almiras (ALM), `department_head` for every other entity; changed only by `managing_partner`, logged |
| employee_code_prefix | Char(8) | Y | | Default `entity.code` (OQ-13) |
| alert_days | JSON list of int | Y | | Default `[90, 60, 30, 7, 0]` (OQ-10) |
| work_week_days / standard_daily_hours | PositiveSmallInt / Decimal(4,2) | Y | | Defaults 5 / 8.00 (business choice, OQ-22) |
| mudad_establishment_no / gosi_establishment_no / qiwa_establishment_no | Char(30) | N | | Recorded only; used by payroll |

### 3.2 `Employee` (unique `(entity, employee_code)`)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| employee_code | Char(20) | Y | | Pseudonymous, gap-free `{prefix}-EMP-{000001}` (OQ-13). The **only** identifier given to billing (`employee_ref`) and ledger |
| full_name_en / full_name_ar | Char(200) | Y | Bi | PD. Both required (OQ-24) |
| nationality | Char(2) ISO 3166 | Y | | `is_saudi` computed = `SA`. GCC nationals: OQ-19 / payroll OQ-9 |
| id_type | Choice `national_id`, `iqama`, `gcc_id`, `passport` | Y | | Saudi → `national_id`; non-Saudi resident → `iqama` (R9) |
| id_number | Char(20) | Y | | PD, encrypted. Format R9 |
| date_of_birth | Date | Y | | PD |
| gender | Choice `male`, `female` | Y | | PD. Needed only for gender-specific statutory leave (3.7) |
| phone / email / address | Char / Email / Text | N | | PD |
| bank_name / iban | Char(100) / Char(34) | N | | PD. IBAN checksum validated; used by WPS (payroll R31) |
| gosi_number / gosi_registered_on | Char(20) / Date | N | | Recorded only (GOSI registration is done on the GOSI portal) |
| hire_date | Date | Y | | Start of continuous service (rehire: OQ-14) |
| status | Choice `draft`, `active`, `on_notice`, `terminated` | Y | | R14 |
| document_flag | Choice `ok`, `expiring`, `expired` | computed | | From 3.8 (R46) |
| user | OneToOne auth.User, null | N | | Self-service later (OQ-4). Required for a department head who approves overtime (R55) |
| employment_basis | Choice `ksa_employee`, `local_entity_employee`, `remuneration` | Y | | *Added 2026-10-17 (H9, R49).* Fixed once a contract is approved (OQ-27) |
| country_of_residence | Char(2) ISO 3166 | Y if `remuneration` | | PD. ≠ `SA` for `remuneration` (default, OQ-28); used for WHT (payroll R43) |

No religion, marital status, health or family fields are stored (PDPL minimisation; OQ-11, OQ-22).

### 3.3 `SalaryComponent` (per entity catalogue; unique `(entity, code)`)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| code | Slug(20) | Y | | Seeded: `BASIC`, `HOUSING`, `TRANSPORT`, `OTHER` (OQ-8) |
| name | Char(100) | Y | Bi | |
| kind | Choice `earning`, `deduction` | Y | | Contract components are earnings |
| counts_for_gosi | Bool | Y | | Seed: BASIC, HOUSING True **[CC-verify]** |
| counts_for_eosb | Bool | Y | | Seed: all fixed earnings True ("wage" = basic + allowances) **[CC-verify]** |
| counts_for_overtime_base | Bool | Y | | Seed: BASIC only (R15 payroll) **[CC-verify]** |
| deducted_for_unpaid_leave | Bool | Y | | Seed: all True (business, payroll OQ-6) |
| expense_account | FK ledger.Account (same entity, expense, postable, not Sharia-prohibited) | Y | | Default 5100 |
| is_active | Bool | Y | | |

### 3.4 `EmploymentContract` (versioned; unique `(entity, employee, version_no)`)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| employee | FK Employee | Y | | Same entity |
| version_no | PositiveInt | Y | | 1 = original; gap-free per employee |
| kind | Choice `original`, `amendment` | Y | | |
| effective_date | Date | Y | | First day this version applies; may be in the past (R20) |
| contract_type | Choice `fixed`, `unlimited` | Y | | R16 |
| start_date / end_date | Date / Date null | Y / Y if fixed | | |
| probation_days | PositiveSmallInt | Y | | R17 |
| probation_extended_in_writing | Bool | Y | | R17 |
| job_title | Char(200) | Y | Bi | |
| occupation_code | Char(20) | N | | Official occupation classification **[CC-verify]** which list Qiwa/GOSI use |
| weekly_hours / daily_hours | Decimal(5,2) / Decimal(4,2) | Y | | R19 |
| annual_leave_days | PositiveSmallInt | Y | | ≥ statutory (R21). *Amended 2026-10-17 (H14):* 21 or 30 for `ksa_employee`, never > 30; 0 for `remuneration` (R56) |
| notice_days_employer / notice_days_employee | PositiveSmallInt | Y | | R37 (statutory check only for `ksa_employee`) |
| currency | FK Currency | Y | | Must equal `entity.base_currency` (R18). *Amended 2026-10-17 (H10):* any active currency for `remuneration` (R53) |
| department | FK Department, null | Y unless `remuneration` | | *Added 2026-10-17 (H12, R54).* Same entity |
| grade | FK Grade, null | Y unless `remuneration` | | *Added 2026-10-17 (H12, R54).* Same entity; drives overtime eligibility |
| status | Choice `draft`, `submitted`, `approved`, `rejected`, `withdrawn` | Y | | R15 |
| submitted_by / approved_by / approved_at / rejected_reason (Bi) | | | | |
| qiwa_contract_ref | Char(40) | N | | Recorded only (OQ-21) |

`ContractComponent`: entity, contract (version), component (FK SalaryComponent, same entity), monthly_amount (Money > 0). Unique `(contract, component)`.

### 3.5 `Assignment` (secondments; unique `(entity, reference)`)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| reference | Char(40) | Y at approval | | Gap-free `{entity.code}-ASG-{year}-{000001}`; = billing `hr_assignment_ref` |
| employee | FK Employee | Y | | Same entity |
| kind | Choice `secondment`, `assignment` | Y | | `secondment` = billable to receiver; `assignment` = non-billable work for another entity (OQ-15) |
| receiving_entity | FK Entity, null | Y if group | | ≠ `entity`; same group parent (R22) |
| receiving_party_en / _ar | Char(200) | Y if no receiving_entity | Bi | Third-party receiver (billing customer) |
| role_title | Char(200) | Y | Bi | Copied to billing `role_title` |
| start_date / end_date | Date / Date null | Y / N | | |
| status | Choice `draft`, `submitted`, `approved`, `active`, `ended`, `cancelled` | Y | | |
| approved_by / approved_at | | | | R23 |
| end_reason | Text | Y when ended | Bi | Arabic required |
| ended_by / ended_at | FK User / DateTime | | | |

`AssignmentEvent` (outbox, append-only): entity, assignment, kind (`ended`, `end_changed`, `cancelled`), end_date, actor, created_at, delivery_status (`pending`, `delivered`, `failed`), attempts, last_error_en/ar, billing_assignment_id (null if no billing link). R24, R25.

### 3.6 Leave
| Model | Fields | Notes |
|---|---|---|
| `LeaveType` (per entity, unique `(entity, code)`) | code, name (Bi), statutory (Bool), parameter_code (FK-by-code to 3.9, null), paid_rule (`full`, `tiered`, `unpaid`), deducts_balance (Bool), requires_document (Bool), gender (null/`male`/`female`), once_per_service (Bool) | Seeded per jurisdiction from 3.7 |
| `LeaveRequest` | entity, employee, leave_type, start_date, end_date, days (Decimal(6,2), computed per R31), status (`draft`, `submitted`, `approved`, `rejected`, `cancelled`, `taken`), approved_by, document_ref, reason (Bi) | |
| `LeaveLedger` (append-only) | entity, employee, leave_type, date, kind (`opening`, `accrual`, `taken`, `adjustment`, `encashment`, `reversal`), days (signed Decimal(7,2)), source_type/source_id, created_by | Balance = Σ days (R29) |
| `SickLeaveDay` (derived, append-only) | entity, employee, date, leave_request, tier_no, pay_pct | Used by payroll (R32) |

### 3.7 Statutory leave seed (SA jurisdiction, every figure **[CC-verify]**)
| Code | Entitlement (memory, unverified) | Pay |
|---|---|---|
| `ANNUAL` | 21 days/year; 30 days/year after 5 years' continuous service **[CC-verify]** | full |
| `SICK` | Within one year: first 30 days full pay, next 60 days 75%, next 30 days unpaid **[CC-verify]** ("one year": contract year vs rolling **[CC-verify]**) | tiered |
| `MARRIAGE` | 5 days **[CC-verify]** | full |
| `BEREAVEMENT` | 5 days on death of spouse, ascendant or descendant **[CC-verify]** | full |
| `PATERNITY` | 3 days on birth of a child (male employee) **[CC-verify]** | full |
| `MATERNITY` | 12 weeks (amended 2025 from 10 weeks **[CC-verify]**), split before/after birth **[CC-verify]**, pay depends on service length **[CC-verify]** | per parameter |
| `IDDAH` | Muslim widow: 4 months 10 days paid; non-Muslim widow 15 days **[CC-verify]**. Requested by the employee; religion is not stored (OQ-22) | per parameter |
| `HAJJ` | 10 to 15 days, once, after 2 years' service **[CC-verify]** | full |
| `EXAM` | Exam days for approved study **[CC-verify]** | per parameter |
| `UNPAID` | By agreement (business) | unpaid |

### 3.8 `EmployeeDocument`
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| employee | FK Employee | Y | | Same entity |
| doc_type | Choice `iqama`, `passport`, `work_permit`, `visa`, `national_id`, `driving_licence`, `medical_insurance`, `other` | Y | | |
| number | Char(40) | Y | | PD, encrypted |
| issuing_country | Char(2) | N | | |
| issue_date / expiry_date | Date / Date | N / Y for iqama, passport, work_permit | | Gregorian stored; Hijri entry/display OQ-9 |
| expiry_date_hijri | Char(10) | N | | As printed, if entered |
| attachment | File (encrypted storage) | N | | PD |
| superseded_by | FK self, null | N | | Renewal (R47) |
| alerts_sent | JSON (threshold → datetime) | Y | | R45 idempotency |

### 3.9 `StatutoryParameter` (jurisdiction data; no entity FK, checked on the group parent like global FX rates)
| Field | Type | Req | Notes |
|---|---|---|---|
| jurisdiction | Char(2) | Y | `SA` only for now |
| code | Slug(60) | Y | e.g. `leave.annual.days`, `eosb.award_table`, `gosi.rates`, `probation.max_days` |
| value | JSON | Y | Typed per code (Decimal strings only, no floats) |
| valid_from / valid_to | Date / Date null | Y / N | Unique `(jurisdiction, code, valid_from)` |
| source_url / source_reference | URL / Char(300) | Y | Official page or article number |
| status | Choice `unverified`, `verified`, `superseded` | Y | Seed rows `unverified` |
| entered_by / verified_by / verified_at | FK User / FK User / DateTime | | `verified_by ≠ entered_by` (R7) |

### 3.10 Termination and EOSB
`Termination`: entity, employee, contract (approved version in force), kind (choice, table R37), article_ref (Char(20)), notice_given_on (Date, null), last_working_day (Date), reason (Bi, Arabic required), status (`draft`, `submitted`, `approved`, `withdrawn`), submitted_by, approved_by, approved_at.

`EosbCalculation` (append-only snapshot): entity, employee, termination (null for provision runs from payroll), as_of, service_start, service_end, service_days, excluded_days (unpaid leave per parameter), wage_basis (Money), wage_components (JSON), full_award (Money), fraction (Decimal(5,4)), award (Money), parameter_ids (JSON), contract_version, computed_by, supersedes (FK self, null).

*Added 2026-10-17 (H9):* `Termination.kind` adds `end_of_remuneration` (only kind allowed for `remuneration`; no EOSB, R52).

### 3.11 `Department` (unique `(entity, code)`) *(added 2026-10-17, H12)*
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| code | Slug(20) | Y | | |
| name | Char(150) | Y | Bi | |
| head | FK Employee, null | N | | Same entity, `active`, with `user` set (R54) |
| parent | FK self, null | N | | Optional hierarchy |
| is_active | Bool | Y | | |

### 3.12 `Grade` (unique `(entity, code)`) *(added 2026-10-17, H12)*
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| code | Slug(20) | Y | | |
| name | Char(150) | Y | Bi | e.g. "Middle management / الإدارة الوسطى" [AR-review] |
| overtime_eligible | Bool | Y | | No default value: must be set explicitly on creation (R54) |
| sort_order | PositiveSmallInt | Y | | Display only |
| is_active | Bool | Y | | |

No grade list is seeded (which grades exist: OQ-29).

## 4. Rules

Enforcement: **svc** = `hr/services.py`; **M** = model `save`/`clean`/`delete`; **DB** = constraint/trigger; **Q** = `core.access.scope`; **T** = code/data scan test; **job** = scheduled command.

### Entity separation and multi-entity
1. Every HR record has the employing `entity`; an employee's contracts, components, assignments, leave, documents, terminations and EOSB snapshots all share it; component accounts belong to it. **[M clean + svc; AT-1]**
2. Every list, detail, export and API goes through `scope(qs, user, permission)`; another entity's record answers 404. **[Q; AT-1]**
3. A person employed by two group entities is two `Employee` rows; no service reads or matches employees across entities (OQ-14). **[svc; T; AT-2]**
4. A new entity uses HR with no code change: `hr_setup_entity <code>` (setup data) creates `HrSettings`, the seeded `SalaryComponent` and `LeaveType` rows for its jurisdiction. **[svc + command; AT-3]**
5. Statutory calculations (leave accrual, sick tiers, EOSB, probation and hours limits) exist only for jurisdiction `SA`. An entity with another jurisdiction (e.g. Egypt, China, UAE subsidiaries) may hold employees and documents, but contract approval, leave accrual, termination approval and EOSB are refused with a bilingual "jurisdiction not configured" message (OQ-1). HR records are refused while `has_employees` is False. **[svc; AT-3]**
    *Amended 2026-10-17 (H9):* the question "which entities, which rules" is decided (R49 to R52): SA calculations apply to `ksa_employee` only; `local_entity_employee` uses the entity's own jurisdiction's parameters, and this refusal stays only until those parameters exist and are verified (OQ-26); `remuneration` has no statutory calculations at all.

### Statutory parameters
6. Every regulatory figure used by HR or payroll is read from `StatutoryParameter` valid on the relevant date (contract date, leave date, termination date). No regulatory figure appears as a code literal. **[svc `param(jurisdiction, code, on)`; T scans `hr/` and `payroll/` for known literals; AT-4]**
7. Seed rows are `unverified`. Verification needs `hr.verify_statutory_parameter` by a user other than `entered_by`. With setting `HR_REQUIRE_VERIFIED_PARAMETERS=True` (production default; False in tests), any approval whose calculation used an unverified row is refused, naming the parameter (en/ar). A row used by an approved record is never edited; a correction is a new row with a later `valid_from` or a `superseded` status plus replacement. **[svc + M; AT-4]**

### Employees and personal data (PDPL **[CC-verify]**)
8. `full_name_en` and `full_name_ar` are required (OQ-24). An employee becomes `active` only with an approved contract version whose `start_date` ≤ today. **[M + svc; AT-5]**
9. Identity: Saudi → `id_type=national_id`; non-Saudi resident → `iqama`; GCC → `gcc_id` or `iqama`; `passport` only for not-yet-resident hires. National ID and iqama are 10 digits, national ID starting 1, iqama starting 2 **[CC-verify]**. `id_number` unique per entity among non-terminated employees (DB partial unique on a keyed hash, since the value is encrypted). **[M + DB; AT-5]**
10. PD fields are encrypted at rest (open-source field encryption, key outside the DB; library choice OQ-11) and masked unless the user holds `hr.view_personal_data` in that entity; each unmasked view or export writes an `AccessLog` row (`personal_data_viewed`: user, employee_code, fields). **[svc + M; access R29; AT-6]**
11. PD never leaves HR/payroll, except: billing receives `employee_code`, role title and dates (R28); payslips go to the employee; WPS files go to the bank (payroll R31). Journal entries, billing records, logs and notifications carry `employee_code`, never names or ID numbers. **[svc; T; AT-7]**
12. No field exists for religion, health condition, marital status or family members. Sick-leave evidence is a document reference only. Retention after termination and data-subject requests: OQ-11; `hr.export_personal_data` produces the employee's own data (en/ar), logged. **[model; AT-6]**
13. No HR permission is flagged **Ag**: agent users cannot read or draft HR records (default, OQ-12). `source` has no `agent` value in HR models. **[catalogue + access R12; AT-8]**
14. Employees are never deleted once `active`; `terminated` employees are read-only except documents' archival flags. A `draft` employee with no approved record may be deleted by its creator. **[M; AT-5]**

### Employment contracts
15. Versions run `draft → submitted → approved` (or `rejected`, `withdrawn`). Only `draft` is editable; `approved` is immutable (model guard + PostgreSQL trigger). Approval needs `hr.approve_contract` (**A**): not the creator, submitter or last modifier, and never the approver's own contract (the employee's `user` ≠ approver; OQ-25). **[svc + `require(..., document=)` + M + DB; AT-9]**
16. `fixed` needs `end_date` > `start_date`; `unlimited` has no `end_date`. A non-Saudi on an `unlimited` contract gets a warning (fixed-term rule for non-Saudis **[CC-verify]**, changed by the 2025 Labour Law amendments **[CC-verify]**). **[M clean + svc; AT-10]**
17. `probation_days` ≤ parameter `probation.max_days` (90 days **[CC-verify]**), or ≤ `probation.max_days_extended` (180 days **[CC-verify]**) only when `probation_extended_in_writing`. **[svc; AT-10]**
18. Components: exactly one `BASIC` > 0; components from the entity's active catalogue; amounts `Decimal` 2 dp; float refused; `currency` = `entity.base_currency` (payroll OQ-4). **[svc + M; AT-10]**
    *Amended 2026-10-17 (H9, H10):* applies to `ksa_employee` and `local_entity_employee`. For `remuneration`, R52 and R53 replace the component and currency parts.
19. `daily_hours` ≤ `hours.max_daily` (8 **[CC-verify]**) and `weekly_hours` ≤ `hours.max_weekly` (48 **[CC-verify]**). Reduced Ramadan hours (6/36 for Muslim workers **[CC-verify]**) are not computed (OQ-22). **[svc; AT-10]**
20. The terms on a day are those of the approved version with the latest `effective_date` ≤ that day (ties: higher `version_no`). `effective_date` ≥ `start_date`; it may be in the past: payroll then computes a retro difference in its next run (payroll R14). Amendments cannot change `employee` or `entity` (a transfer is a termination + new hire, OQ-14). **[svc `terms_on(employee, day)`; AT-11]**
21. `annual_leave_days` ≥ the statutory entitlement for the employee's service length (R30). **[svc; AT-10]**
    *Amended 2026-10-17 (H14):* kept as the minimum check for `ksa_employee`; the allowed values and the 30-day ceiling are R56.

### Assignments and secondments (billing.md 3.6 interface)
22. `receiving_entity` ≠ `entity`, and both have the same top-level group parent; or no `receiving_entity` and a bilingual third-party name. The assignment lies within an approved contract's dates. No two `secondment` assignments of one employee overlap (default, OQ-15). **[svc; AT-12]**
23. Starting: drafted by `hr.manage_assignment`, approved by `hr.approve_assignment` (**A**, default approver = CFO, OQ-3); approval assigns `reference`. Status is `active` from `start_date`. Approval creates nothing in billing: the accountant links billing's `SecondmentAssignment` by `hr_assignment_ref = reference` and copies `employee_ref = employee_code`, `role_title`, `start_date` (billing R1 to R5 unchanged). **[svc; AT-12]**
24. Ending: `hr.services.end_assignment(entity, assignment, end_date, reason, user)` needs `hr.end_assignment`; no CFO approval (H4, H5). `end_date` ≥ `start_date`; it may be earlier or later than a previous end date (billing R49 handles both). In the same transaction it writes one `AssignmentEvent` (`ended` / `end_changed`). The same end date again is a no-op (no event). **[svc; AT-13]**
25. Delivery: on commit and then by job `hr_deliver_assignment_events` (every 5 min, default), each pending event calls `billing.services.end_secondment(entity, billing_assignment, end_date, user, source="hr_event")` with `user` = the HR user who ended it. Idempotent (billing 3.6). A billing error never rolls back the HR end: the event stays `pending` with the bilingual error and is retried; after 5 failed attempts (default) it is `failed` and the entity's accountants and HR officers are alerted (en/ar). No billing link → event `delivered` with nothing to do. **[svc + job; AT-14]**
26. Once a billing `SecondmentAssignment` has `hr_assignment_ref` set, billing refuses a manual end for it (`source="manual"`): HR is the source (supersedes H5 for linked assignments; needs a billing amendment, OQ-16). **[billing svc; AT-14]**
27. Termination approval ends every active assignment of the employee at `last_working_day` through R24. Cancelling a not-yet-started assignment sets `cancelled` and emits a `cancelled` event (billing end date = start date − 1 → nothing billable; OQ-16). **[svc; AT-20]**
28. Data sent to billing: `reference`, `employee_code`, `role_title_en/ar`, dates, receiving entity. Never names, ID numbers or salary. **[svc; T; AT-7]**

### Leave
29. Balance = Σ `LeaveLedger.days` per (employee, leave type). Ledger rows are never edited or deleted; a correction is an `adjustment` row created by `hr.adjust_leave_balance` and approved (**A**) by the approver. **[M + DB + svc; AT-15]**
30. Annual leave accrues at month end (job `hr_accrue_leave`, idempotent per employee and month): entitlement/12 per full month of service, pro rata by days for partial months; the 30-day tier applies from the month after 5 years' continuous service **[CC-verify]**. Whether unpaid leave pauses accrual **[CC-verify]**: parameter, default yes for unpaid leave over 20 days in a year **[CC-verify]**. Carry-forward and forfeiture: none (default, OQ-5). **[job + svc; AT-15]**
31. A request counts days per its type's parameter (calendar vs working days **[CC-verify]**); overlapping approved requests are refused; annual leave cannot exceed the balance at `start_date` (no negative balance, OQ-5). Approval needs `hr.approve_leave` (**A**): not the requester's own, not the approver's own leave. Approval writes a `taken` row at approval (reversed on cancellation for unused days). **[svc; AT-16]**
32. Sick leave: on approval, each day gets a tier from the employee's prior sick days in the parameter window (30 days 100%, 60 days 75%, 30 days 0% **[CC-verify]**); days beyond the last tier are refused as sick leave (to be recorded as unpaid). `SickLeaveDay.pay_pct` is payroll's input. A document reference is required. **[svc; AT-17]**
33. Statutory leave types (3.7) check gender, once-per-service and service-length conditions from parameters; their entitlements are per event, not accrued. **[svc; AT-18]**
34. Unpaid leave days are passed to payroll as unpaid days for the months they fall in (payroll R16). **[svc; payroll AT-6]**
35. At termination approval, approved leave after `last_working_day` is cancelled and its days returned. The remaining annual balance at `last_working_day` goes to the final settlement (payroll R34). **[svc; AT-20]**

### Termination and end-of-service benefit
36. A termination is drafted by `hr.record_termination` and approved by `hr.approve_termination` (**A**; default approver = CFO, OQ-3), never by the employee themself. `last_working_day` ≥ contract `start_date`; `reason_ar` required. **[svc; AT-19]**
37. Kinds and their EOSB fraction (all **[CC-verify]**, as `eosb.fraction_table` parameter):

| Kind | Article (memory) | Fraction of full award |
|---|---|---|
| `resignation` | Art. 85 **[CC-verify]** | < 2 years 0; 2 to < 5 years 1/3; 5 to < 10 years 2/3; ≥ 10 years 1 **[CC-verify]** |
| `employer_termination` (with notice, unlimited contract) | Art. 75, 84 **[CC-verify]** | 1 |
| `end_of_fixed_term` | Art. 84 **[CC-verify]** | 1 |
| `mutual_agreement` | **[CC-verify]** | 1 unless agreed otherwise (OQ-23) |
| `art80_dismissal` (no notice, no award) | Art. 80 **[CC-verify]** | 0 |
| `art81_employee_leaves` (employee leaves keeping rights) | Art. 81 **[CC-verify]** | 1 |
| `force_majeure` / `female_marriage_childbirth` (within 6 months of marriage or 3 months of childbirth) | Art. 87 **[CC-verify]** | 1 |
| `death`, `incapacity`, `retirement` | Art. 84, 87 **[CC-verify]** | 1 |
| `probation_termination` | Art. 54 **[CC-verify]** | 0 |

Notice period (60 days employer / 30 days employee for monthly-paid unlimited contracts **[CC-verify]**) is checked against `notice_given_on`: shortfall is a warning and a notice-pay input for payroll, not a refusal. Compensation for unjustified termination (Art. 77 **[CC-verify]**) is entered manually as a final-settlement earning, never computed. **[svc; AT-19, AT-21]**
38. Approval, in one transaction: termination `approved`; employee `terminated` (or `on_notice` until `last_working_day`); contract marked ended; R27 assignment ends (events); R35 leave; an `EosbCalculation` snapshot; a draft `payroll.FinalSettlement` (payroll R34). Any failure rolls back all but already-committed billing deliveries. **[svc atomic; AT-20]**
39. EOSB formula (parameter `eosb.award_table`, all **[CC-verify]**): service = (service_end − service_start + 1 − excluded_days) in years as days/365 **[CC-verify: 365 vs Hijri year]**; wage basis = last monthly amounts of components with `counts_for_eosb` in the version in force on `last_working_day` **[CC-verify]**; full award = ½ month's wage per year for the first 5 years + 1 month's wage per year after, part years pro rata **[CC-verify]**; award = full award × fraction (R37). Computed in `Decimal`, rounded half-up to 2 dp only at the award. **[svc `calculate_eosb(employee, as_of, kind)`; AT-21]**
40. `calculate_eosb` is pure (no write) and is also used by payroll for the monthly provision (payroll R24). Snapshots are immutable; a recalculation before the final settlement is approved writes a new snapshot with `supersedes`; after approval it is refused. **[svc + M; AT-21]**
41. An approved termination is not deleted. Withdrawing it (e.g. resignation withdrawn) is allowed only before `last_working_day` and before the final settlement is approved, by a new approved `withdrawn` step that restores the contract, re-emits assignment events (end date removed is not supported by billing: OQ-16) and supersedes the snapshot (default, OQ-23). **[svc; AT-22]**

### Document expiry
42. `iqama`, `passport` and `work_permit` need `expiry_date`; a non-Saudi `active` employee without a current iqama gets a warning flag. **[M + svc; AT-23]**
43. Job `hr_document_alerts` runs daily (Asia/Riyadh) per entity: for each current document and each threshold in `HrSettings.alert_days`, the first run on or after (expiry − threshold) sends one in-app alert (en/ar, per user's language) to the entity's `hr_officer` holders; `alerts_sent` makes it idempotent. Alert text carries `employee_code`, document type and date, not the number. **[job + svc; AT-23]**
44. `document_flag`: `expired` if any required document is past expiry, `expiring` if within the largest threshold. Payroll shows the flag as a run warning, never a block (payroll R10). **[svc; AT-23]**
45. Renewal = new document row; the old one gets `superseded_by` and stops alerting. Documents are never deleted while the employee exists (retention OQ-11). **[M + svc; AT-23]**

### Language, Sharia, permissions
46. Every label, choice, message, alert, letter and report exists in English and Arabic; the user's `preferred_language` is used for messages. **[T; AT-24]**
47. HR posts nothing and touches no Sharia-flagged account; `SalaryComponent.expense_account` must not be Sharia-prohibited where the entity enforces Sharia (checked at save; the ledger checks again at posting). Employee loans (interest-free) are in payroll R26. *Amended 2026-10-17 (H13):* loan cap and bai' ajel financing are payroll R46 to R49. **[svc; AT-25]**
48. Every service calls `require(user, entity, permission)` (access R1); approval services pass `document=` so segregation is checked on the stored row (access R14, R35). **[svc; T access AT-26]**

### Project-owner decisions 2026-10-17 (H9 to H16)
49. **Employment basis (H9).** Every `Employee` has `employment_basis`. Allowed: entity jurisdiction `SA` → `ksa_employee` or `remuneration`; any other jurisdiction → `local_entity_employee` only. The basis cannot change once a contract version is approved (a change is a termination plus a new `Employee` row, default OQ-27). **[M clean + svc; AT-26]**
50. **`ksa_employee`.** All Saudi statutory rules of this spec (R6, R7, R9, R16, R17, R19, R21, R29 to R41, R42) apply, with `SA` parameters. **[svc; existing ATs]**
51. **`local_entity_employee`.** Statutory figures are read only from `StatutoryParameter` rows of the employing entity's jurisdiction; `param()` refuses an `SA` row for such an employee, so a Saudi rule can never apply. Until the entity's jurisdiction has verified parameters for a calculation (contract checks, leave, termination benefit), that calculation is refused as in R5 (OQ-26). Master data, documents, departments and grades are kept. Contract currency = `entity.base_currency` (H10). **[svc `param(jurisdiction, code, on)`; AT-26]**
52. **`remuneration`.** Held only in an `SA` entity. No labour regulation applies: the contract version has exactly one component `REMUNERATION` (seeded per entity: earning; `counts_for_gosi`, `counts_for_eosb`, `counts_for_overtime_base`, `deducted_for_unpaid_leave` all False); `probation_days` 0; `annual_leave_days` 0; no statutory probation, hours, notice or fixed-term checks; leave requests, `calculate_eosb`, sick tiers and statutory leave types are refused; termination only as `end_of_remuneration` (no `EosbCalculation`; the draft final settlement has no EOSB, leave or GOSI lines). Iqama and work permit are not required and the R42 warning does not apply; `id_type` `passport` is allowed. `country_of_residence` required and ≠ `SA` (default, OQ-28). **[M clean + svc; AT-27]**
53. **Currency (H10).** `ksa_employee` and `local_entity_employee` contracts are in `entity.base_currency`. A `remuneration` contract may be in any active currency; its amount is stored with that currency's own decimals (up to 3, CLAUDE.md rule 7); more decimals than the currency allows, or a float, is refused. **[M clean + svc; AT-27, AT-28]**
54. **Departments and grades (H12).** `Department` and `Grade` are per-entity catalogues (3.11, 3.12) maintained by `hr.manage_org`. Every approved contract version of a `ksa_employee` or `local_entity_employee` names an active department and grade of the same entity. A department head is an `active` employee of the same entity with a linked `user`; a head from another entity, a terminated head or a head with no user is refused. `Grade.overtime_eligible` must be set explicitly; payroll refuses overtime for a grade where it is False (payroll R45). **[M clean + svc; AT-29]**
55. **Overtime approval route (H12).** `HrSettings.overtime_approval_route` decides who approves overtime (payroll R45): `managing_partner` (seeded for ALM) or `department_head` (seeded for every other entity, also for new entities created as data). Only `managing_partner` changes the route, logged in `AccessLog` (en/ar). **[svc + setup command; AT-30]**
56. **Annual leave (H14).** For `ksa_employee`, `annual_leave_days` is 21 or 30 (any other value refused). Entitlement on a day = the higher of the contract value and the statutory value for the service length (21, or 30 after 5 years' continuous service **[CC-verify]**, R30), and never more than 30. No adjustment, accrual or contract can give more than 30 days for one service year. `remuneration`: 0 and no leave. `local_entity_employee`: per that jurisdiction's parameters (whether the 30-day ceiling applies: OQ-31). **[M clean + svc + job; AT-31]**
57. **Roles (H15).** `hr_officer` and `payroll_officer` are seeded roles (access 3.1, A21), granted only by `managing_partner` (an `access_admin` grant is refused; access R52). Holders as §7. **[seed migration + access R8; AT-32]**

## 5. Accounting

None in HR. HR creates no journal entries. All HR-driven postings (salaries, GOSI, EOSB provision and its use, leave encashment, final settlement) are made by payroll through the ledger document registry: see `docs/specs/payroll.md` §5. Intercompany billing of secondments is billing's (billing.md B3).

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| `billing` (internal) | Out | `AssignmentEvent` → `billing.services.end_secondment(..., source="hr_event")` (R24 to R27); `employee_code` and role title copied when the accountant links a contract line | Interface per billing 3.6; user/permission for `hr_event` and manual-end refusal: OQ-16 |
| `payroll` (internal) | Out | Contract terms, components, leave (unpaid days, sick pay %), EOSB calculation, termination → draft final settlement | payroll.md |
| `core.access` | In | `require`, `scope`, `AccessLog` for PD views | access.md |
| Qiwa (contract documentation, work permits) **[CC-verify]** | none | Contract reference recorded manually | OQ-21 |
| Muqeem / Absher (iqama data) **[CC-verify]** | none | Expiry dates entered manually | OQ-21 |
| GOSI | none from HR | Registration done on the GOSI portal; number and date recorded | Payroll computes contributions |
| Mudad / WPS | none from HR | Payroll | payroll.md |
| Daftra / SAP B1 | In (one-off) | Employees, contracts, leave balances (`opening` rows), service start dates; `source=import` | migration-agent, payroll OQ-26 |
| ZATCA, banks | none | — | — |

## 7. Permissions

Roles `hr_officer` (entity, human) and `payroll_officer` (entity, human); `name_ar` [AR-review]: مسؤول الموارد البشرية / مسؤول الرواتب. *Amended 2026-10-17 (H15):* **decided**, no longer proposed; granted by `managing_partner` (R57, access A21). `hr_officer` also approves payroll runs (payroll R50). No HR permission is **Ag** (R13). Flags as access 3.6.

*Added 2026-10-17 (H12):* `hr.manage_org` (departments, grades, department heads): hr_officer. `hr.set_overtime_route`: managing_partner only, per entity (R55). Overtime approval itself is `payroll.approve_overtime` (payroll §7).

| Codename | Flags | Holders (default) |
|---|---|---|
| `hr.view` (masked) | | hr_officer, payroll_officer, approver; viewer: counts only (OQ-11) |
| `hr.view_personal_data` | | hr_officer; payroll_officer (bank/IBAN, ID for WPS) — every view logged |
| `hr.manage_employee` (create/edit employee, documents) | | hr_officer |
| `hr.edit_contract_draft` / `hr.submit_contract` | | hr_officer |
| `hr.approve_contract` | A | approver (OQ-3) |
| `hr.manage_assignment` / `hr.approve_assignment` | — / A | hr_officer / approver |
| `hr.end_assignment` | | hr_officer (no approval, H4/H5) |
| `hr.request_leave` / `hr.approve_leave` | — / A | hr_officer (on behalf of employees) / hr_officer, not own (OQ-4) |
| `hr.adjust_leave_balance` (approve) | A | approver |
| `hr.record_termination` / `hr.approve_termination` | — / A | hr_officer / approver |
| `hr.calculate_eosb` (preview) | | hr_officer, payroll_officer, approver |
| `hr.export_personal_data` | S! | hr_officer (logged) |
| `hr.maintain_statutory_parameter` | G | hr_officer on the group parent (OQ-17) |
| `hr.verify_statutory_parameter` | G, A | managing_partner (OQ-17); never the row's `entered_by` |
| `hr.receive_alerts` | | hr_officer |

| Action | hr_officer | payroll_officer | approver (CFO) | accountant | viewer | managing_partner | Agent / system |
|---|---|---|---|---|---|---|---|
| Create/edit employee, documents | Y | | | | | | No |
| Approve contract, assignment, termination, leave adjustment | | | Y (not own/creator) | | | Fallback (access OQ-1) | No |
| End secondment | Y | | Y | | | | No |
| Approve leave | Y (not own) | | | | | | No |
| See unmasked PD | Y (logged) | Bank/ID only (logged) | Masked | Masked | Masked | Masked unless granted | No |
| Verify statutory parameter | | | | | | Y | No |

## 8. Acceptance tests

Fixtures: ALM (parent, SA, SAR, Sharia enforced), SUB (child, `full`, SA, SAR), EGY (child, country EG, EGP); seeded unverified SA parameters (tests set `HR_REQUIRE_VERIFIED_PARAMETERS=False` except AT-4); users H (hr_officer ALM), P (payroll_officer ALM), C (approver ALM), C2 (approver SUB), A (accountant ALM), V (viewer ALM), MP; agent user. Anonymised employees only (fake names, IDs like `1000000001`).

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | Employee E1 in ALM, E2 in SUB | H lists employees; opens E2 by id; saves an ALM contract component on SUB's 5100 | Only E1 listed; E2 → 404; component refused (R1, R2) |
| AT-2 | Same anonymised ID number active in ALM and SUB | Create both | Both allowed (per-entity uniqueness); no service returns the other entity's row (R3) |
| AT-3 | New entity NEW (SA) created as data; EGY with `has_employees=True` | Run `hr_setup_entity NEW`; create and approve a contract in NEW; approve a contract in EGY | NEW works with seeded components and leave types, no code change; EGY approval refused "jurisdiction not configured" (en/ar); EGY employee master data saved (R4, R5) |
| AT-4 | `HR_REQUIRE_VERIFIED_PARAMETERS=True`; `eosb.award_table` unverified; MP verifies a row MP entered; MP verifies a row H entered | C approves a termination; verify attempts; C approves again | First approval refused naming the parameter; MP self-verification refused; second verification accepted; approval then succeeds; editing the used row refused (R6, R7) |
| AT-5 | Saudi employee with `iqama`; non-Saudi with national ID `1…`; ID with 9 digits; no `full_name_ar` | Save each | All refused (en/ar); valid Saudi (`1xxxxxxxxx`, national_id) and non-Saudi (`2xxxxxxxxx`, iqama) accepted; second active employee with same ID in ALM refused; deleting an active employee refused (R8, R9, R14) |
| AT-6 | E1 with IBAN and ID | A and V open E1; H opens E1; H exports E1's data | A, V see masked values, no log; H sees values, `personal_data_viewed` row; export en/ar, logged; DB column holds ciphertext (R10, R12) |
| AT-7 | E1 seconded to SUB; payroll run approved | Inspect billing assignment, journal entry descriptions, AssignmentEvent, alerts | Only `employee_code`, role title, dates appear; no name, ID or salary (R11, R28) |
| AT-8 | Agent user with any role | Agent lists employees; drafts a leave request; admin adds `hr.view` to an agent role | All refused; role save refused (R13; access R12) |
| AT-9 | Contract v1 drafted and submitted by H | H approves; C approves; C edits v1; C approves a contract whose employee's `user` is C | H refused (A); C approves; edit refused; own-contract approval refused (R15) |
| AT-10 | Drafts: fixed without end date; probation 120 days without written extension; 120 with; no BASIC; float amount; 50 weekly hours; USD currency in ALM; annual leave 15 days | Submit each | All refused except probation 120 with written extension (accepted ≤ 180) and the non-Saudi unlimited contract (accepted with warning) (R16 to R19, R21) |
| AT-11 | v1 BASIC 10,000.00 from 2027-01-01; v2 BASIC 11,000.00 effective 2027-03-15 approved on 2027-04-10 | `terms_on` for 2027-03-14 and 2027-03-15; try to amend `entity` | 10,000.00 then 11,000.00; payroll May run shows retro lines (payroll AT-8); entity change refused (R20) |
| AT-12 | E1 in ALM | Secondment to ALM itself; to an entity of another group; overlapping second secondment; outside contract dates; valid one to SUB drafted by H, approved by C | First four refused; valid one gets `ALM-ASG-2027-000001`, `active` on start date; no billing record created (R22, R23) |
| AT-13 | Active secondment linked to billing | H ends it 2027-03-10; ends again 2027-03-10; changes to 2027-03-20 | One `ended` event; repeat no-op (no event); `end_changed` event; no CFO approval needed (R24) |
| AT-14 | Billing stub raises on first call, succeeds on second; a second assignment with no billing link; linked billing assignment | End both; run delivery job twice; accountant tries manual end in billing on the linked one | HR end committed despite failure; event `pending` with en/ar error, then `delivered`, billing `end_secondment` called once with `source="hr_event"`; unlinked → `delivered`, nothing called; after 5 failures (variant) `failed` + alert; manual billing end refused (R25, R26) |
| AT-15 | Hire 2025-01-01, `annual_leave_days` 21 | Accrual job for 2027-01 twice; then for an employee hired 2022-01-01 (5 years on 2027-01-01), months 2027-01 and 2027-02; H edits a ledger row; H adjusts +2 days, C approves | 1.75 days once (21/12); five-year employee accrues 1.75 for 2027-01 and 2.50 (30/12) for 2027-02, the month after the anniversary **[CC-verify]**; edit refused; adjustment row after C approval only (R29, R30) |
| AT-16 | Balance 5 days | Request 6 annual days; request 5; overlapping request; H approves own leave; another hr_officer approves | 6 refused; overlap refused; self-approval refused; other approves, `taken` −5 (R31) |
| AT-17 | 25 sick days already in window | Request 40 sick days; request 100 more | Days 26–30 at 100%, 31–65 at 75% **[CC-verify]**; days past the last tier refused as sick leave; `SickLeaveDay` rows created (R32) |
| AT-18 | Male employee; female employee with 1 year service | Male requests `MATERNITY`; female requests `HAJJ`; employee requests `HAJJ` twice after 3 years | Refused; refused (service); second Hajj refused (once per service) (R33) |
| AT-19 | Unlimited contract, hire 2020-01-01 | H drafts resignation without `reason_ar`; with it; H approves; C approves; employee-user C approves own termination (variant) | Missing reason refused; H refused (A); C approves; own refused (R36) |
| AT-20 | E1 seconded to SUB, approved leave after the last day, annual balance 10 days | C approves termination, `last_working_day` 2027-06-30 | Contract ended; assignment end event 2027-06-30; future leave cancelled and returned; `EosbCalculation` snapshot; draft `FinalSettlement` in payroll with 10 days encashment input; all in one transaction (R27, R35, R38) |
| AT-21 | Wage basis 8,000.00 (all components `counts_for_eosb`); seed parameters | `calculate_eosb` for: 4 years resignation; 4 years employer termination; 12 years resignation; 1 year resignation; Art. 80; 7.5 years end of fixed term | Using the unverified seed: 5,333.33; 16,000.00; 1×(5×4,000 + 7×8,000) = 76,000.00; 0.00; 0.00; 5×4,000 + 2.5×8,000 = 40,000.00. Snapshot immutable; recalculation creates a superseding snapshot; after final settlement approval refused. **All expected figures [CC-verify]** (R37, R39, R40) |
| AT-22 | Approved resignation, `last_working_day` in future, final settlement draft | C withdraws it; then withdraws one whose final settlement is approved | First restores contract and employee status, supersedes snapshot, discards draft settlement; second refused (R41) |
| AT-23 | Iqama expiring in 59 days; passport expired; `alert_days` default | Run alert job twice the same day; renew the iqama | One 60-day alert (en/ar) to ALM hr_officers only, no number in text; nothing on second run; employee `document_flag=expired`; after renewal, old row superseded and silent (R42 to R45) |
| AT-24 | Arabic-preference user | Render employee, contract, leave, termination, alerts, errors | Arabic shown; every choice and message has en/ar (R46) |
| AT-25 | ALM Sharia enforced | Set a component's `expense_account` to 5610 | Refused (R47) |
| AT-26 | *Added 2026-10-17 (H9).* ALM (SA), EGY (EG, `has_employees=True`) | Create in ALM a `ksa_employee`, a `remuneration` (residence EG) and a `local_entity_employee`; in EGY a `ksa_employee` and a `local_entity_employee`; approve the EGY local contract; change the ALM `ksa_employee`'s basis after contract approval | ALM ksa and remuneration saved; ALM local refused; EGY ksa refused; EGY local saved; its contract approval refused "jurisdiction not configured" (en/ar) and no `SA` parameter is read (test spies on `param`); basis change refused (R49, R51) |
| AT-27 | *Added 2026-10-17 (H9, H10).* ALM remuneration employee R1, residence EG | Contract with `REMUNERATION` USD 5,000.00; add `HOUSING`; amount USD 5,000.005; a KWD contract 1,250.125; R1 requests annual leave; `calculate_eosb(R1)`; record `resignation` for R1; record and approve `end_of_remuneration` | USD contract approved without probation/hours/notice checks and no iqama warning; HOUSING refused; USD 3 dp refused; KWD 3 dp accepted; leave refused; EOSB refused; `resignation` refused; `end_of_remuneration` approved with no `EosbCalculation`, draft final settlement without EOSB/leave/GOSI lines (R52, R53) |
| AT-28 | *Added 2026-10-17 (H10).* ALM `ksa_employee`; EGY `local_entity_employee` (EGP base) | Contract in USD for each; EGY contract in EGP | Both USD refused (en/ar); EGP accepted at master-data level (R53) |
| AT-29 | *Added 2026-10-17 (H12).* ALM departments FIN (head = E1 with user), OPS; grades G-STAFF (`overtime_eligible` True), G-MGMT (False) | Approve a ksa contract with no grade; with no department; set FIN head to a SUB employee, to a terminated employee, to an employee without user; create a grade without `overtime_eligible` | First two refused; the three head changes refused; grade without the flag refused; valid contract with FIN / G-MGMT approved (R54) |
| AT-30 | *Added 2026-10-17 (H12).* | `hr_setup_entity` for ALM, SUB and NEW (created as data); H changes SUB's route; MP changes it | ALM `managing_partner`, SUB and NEW `department_head`; H refused; MP change applied with an `AccessLog` row (R55) |
| AT-31 | *Added 2026-10-17 (H14).* ALM ksa employees | Contract with `annual_leave_days` 25; 31; 30; employee on 21 days hired 2022-01-01, accrual for 2027-02; employee on 30 days after 5 years; adjustment that would make one service year exceed 30 days | 25 and 31 refused; 30 accepted; 21-day employee accrues 2.50 (30/12) for 2027-02 **[CC-verify]**; 30-day employee stays at 2.50/month; adjustment refused (R56) |
| AT-32 | *Added 2026-10-17 (H15).* MP, access_admin AA in ALM | MP grants H `hr_officer` and P `payroll_officer` in ALM; AA grants another user `hr_officer`; render role names in Arabic | MP grants logged; AA refused; names en/ar present (R57; access R52) |

## 9. Open questions

**B** = blocks building that part; **GL** = blocks go-live; **D** = default exists, building goes ahead.

| # | Question | Owner | Blocks? | Default |
|---|---|---|---|---|
| OQ-1 | Which entities have employees? Which jurisdictions apply (Egypt, China, UAE subsidiaries): local labour law, leave, end-of-service, social insurance? Is HR for them in this ERP at all? | Managing Partner | — | **Resolved by H9 (2026-10-17)** at decision level: all group companies have employees; per-person basis (R49 to R52); the ERP holds local employees per entity with country parameters. Parameters themselves: OQ-26 |
| OQ-2 | Approve new roles `hr_officer`, `payroll_officer` and the permission catalogue in §7 (access catalogue change). | Managing Partner | — | **Resolved by H15 (2026-10-17)** at decision level: both roles exist, MP grants them, `hr_officer` approves payroll. The permission-by-permission list in §7 is still the spec default |
| OQ-3 | Who approves contracts, salary changes, secondment starts and terminations: CFO, an HR manager, or the Managing Partner? | Managing Partner | D | Entity `approver` (CFO), per H2. *Note 2026-10-17:* H12 decides only overtime approval (R55), H13 loans, H15 payroll runs; the items in this question stay open |
| OQ-4 | Leave approval by line manager? Employee self-service (requests, payslips, documents)? | Managing Partner | D | hr_officer records and approves; no self-service |
| OQ-5 | Leave policy beyond the statutory minimum: extra days, carry-forward, forfeiture, negative balance, encashment during service. | Managing Partner / CFO | D | Statutory only; no forfeiture; no negative balance; encashment only at exit. *Partly resolved by H14 (2026-10-17):* entitlement 21 or 30, never above 30 (R56). Who gets 30 days (criterion) and carry-forward, forfeiture, negative balance, encashment stay open |
| OQ-6 | Is unused annual leave accrued in the books monthly (compensated absences, IFRS for SMEs s28 **[CC-verify]**)? | Accountant | D | Not posted; liability report only (payroll OQ-12 has proposed accounts) |
| OQ-7 | Attendance and time tracking: in scope? | Managing Partner | D | Out of scope; overtime and absences entered as payroll inputs |
| OQ-8 | Allowance structure per entity (which components; which count for GOSI, EOSB, overtime, unpaid deduction **[CC-verify]** for the statutory parts). | CFO + compliance-checker | D | BASIC, HOUSING, TRANSPORT, OTHER with seed flags (3.3) |
| OQ-9 | Hijri dates: enter/display iqama expiry in Hijri (Umm al-Qura)? Library (open source). | HR / Dev | D | Gregorian stored; optional Hijri text as printed |
| OQ-10 | Document alert thresholds, recipients, channel (in-app, e-mail). | HR | D | 90/60/30/7/0 days, in-app to hr_officer |
| OQ-11 | PDPL **[CC-verify]**: lawful basis, retention after exit, data-subject requests, encryption library and key custody, cross-border transfer for non-SA entities' data, DPO, what viewers may see. | Legal + Managing Partner | **GL** | Encrypt PD; mask by default; keep while employed + 10 years, then anonymise; KSA hosting (H7) |
| OQ-12 | May agents ever read or draft HR data (e.g. document OCR)? | Managing Partner | D | No |
| OQ-13 | Employee code format; per entity or group-wide. | HR | D | `{entity.code}-EMP-{000001}` per entity |
| OQ-14 | Transfers and rehires between group entities: is service continuous, is the EOSB provision transferred (intercompany) or settled? | Managing Partner + Accountant | D | Settled on leaving; new service in the new entity |
| OQ-15 | Secondments: partial/concurrent secondments, non-billable assignments, billing pause during unpaid leave, third-party secondments, approval to start. | Managing Partner | D | One secondment at a time, full-time; no pause; third parties allowed; CFO approves start |
| OQ-16 | Billing amendment needed: which user and permission `end_secondment` accepts for `source="hr_event"` (HR officers lack `billing.record_contract_event`); refusing manual end on linked assignments; how a termination withdrawal un-ends a secondment; cancellation before start. | Billing spec owner + Managing Partner | **B** for R25/R26 wiring (build against a stub) | Billing accepts an `hr.end_assignment` grant for `hr_event`; manual end refused when linked; un-ending = new end date later (billing R49) |
| OQ-17 | Who enters and who verifies statutory parameters? | Managing Partner | D | Group-parent hr_officer enters; MP verifies after compliance-checker report |
| OQ-18 | EOSB details **[CC-verify]**: year basis (365 days vs Hijri), part-year pro rata, unpaid leave exclusion, wage definition, probation, cap if any. | compliance-checker | **GL** for EOSB | Parameters as R39 |
| OQ-19 | Non-Saudi fixed-term rule after the 2025 amendments, GCC nationals' treatment, Saudization (Nitaqat) tracking **[CC-verify]**. | compliance-checker / Managing Partner | D | Warning only; Nitaqat out of scope |
| OQ-20 | Limits on retroactive contract amendments (how far back). | CFO | D | No limit before `start_date`; payroll retro in next run |
| OQ-21 | Integrations with Qiwa, Muqeem, Absher **[CC-verify]**. | Managing Partner | D | None; manual references |
| OQ-22 | Ramadan reduced hours and work-week per entity, without storing religion. | HR + legal | D | Not computed; hours from contract |
| OQ-23 | Mutual-agreement terminations (fraction), withdrawal of an approved termination. | Managing Partner + legal | D | Fraction 1 unless approver enters another with reason; withdrawal per R41 |
| OQ-24 | Must both English and Arabic names be entered for every employee? | HR | D | Yes |
| OQ-25 | Records about the approver themself (own contract, leave, termination): who approves? | Managing Partner | D | Refused for self; `managing_partner` fallback (access OQ-1) |
| OQ-26 | *Added 2026-10-17 (H9).* Local statutory parameters for Egypt, China and UAE entities (labour law, leave, end-of-service/gratuity, social insurance, income tax, hours, probation), with sources, and who verifies them (local adviser). Shared with payroll OQ-28. | Managing Partner + local advisers + compliance-checker | **B** for `local_entity_employee` calculations in those entities only; not for KSA | R5 refusal stays for those calculations; master data kept |
| OQ-27 | *Added 2026-10-17 (H9).* A person changes basis (e.g. remuneration → KSA employee on relocation, or transfer to a local entity): service continuity, settlement? | Managing Partner | D | Termination + new `Employee` row; no continuity (as OQ-14) |
| OQ-28 | *Added 2026-10-17 (H9).* Remuneration staff: must they live outside KSA? Saudi nationals possible? Termination/notice terms, agreement template, PDPL cross-border transfer of their data. | Managing Partner + legal | D | Residence ≠ SA required; notice per agreement, not checked; data handled as other PD (OQ-11) |
| OQ-29 | *Added 2026-10-17 (H12).* Grade list per entity; which grades are "middle management and management"; do grades drive anything else (leave 30 days, loans)? | Managing Partner / HR | D | Each entity defines grades with an explicit `overtime_eligible`; no other effect |
| OQ-30 | *Added 2026-10-17 (H12).* Department head absent, approving their own overtime, department without a head; is a `department_head` access role acceptable (access OQ-28)? | Managing Partner | D | Request waits (access R18); own overtime and no-head cases go to `managing_partner`; proposed role `department_head` |
| OQ-31 | *Added 2026-10-17 (H14).* Does the 30-day ceiling also apply to local entity employees where local law gives more? Which criterion gives 30 days in KSA (grade, contract, seniority)? | Managing Partner | D | Ceiling for `ksa_employee` only; 30 days set per contract and approved with it |

**Blocking:** OQ-26 (non-SA local calculations; replaces OQ-1, resolved by H9), OQ-16 (billing interface wiring). **Go-live:** OQ-11 (PDPL), OQ-18 (EOSB), every [CC-verify] parameter verified. **Before building:** the compliance-checker verifies every [CC-verify] item. (Role additions approved by H15, 2026-10-17.)
