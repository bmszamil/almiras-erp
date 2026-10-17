# Payroll: monthly runs, GOSI, EOSB provision, loans, WPS and final settlement

Django app: `payroll`. Status: **draft spec**. Written 2026-10-17 by the spec-writer agent. Amended 2026-10-17 (project-owner decisions P8 to P15 = hr H9 to H16: employment basis, foreign-currency remuneration with WHT, cut-offs, overtime approval, loan cap and bai' ajel, HR officer approves runs, salary banks; 3.1 to 3.4, 3.6, 3.11, R4, R5, R9, R11, R15, R27, R41 to R51, §5 E8 to E13 and proposed 1240, 1241, 1250, 4610, §7, AT-25 to AT-34, OQ-1 to OQ-5, OQ-7, OQ-10, OQ-12, OQ-14, OQ-20, OQ-28 to OQ-36). Amended 2026-10-17 (Labour Law / WPS check, `docs/compliance/hr-payroll-labour-law-2026-10-17.md` §3 items 9 to 14: 3.1, 3.2, 3.4, 3.9, R10, R15 to R18, R20 to R22, R28, R31, R32, R34, R45, R49, R51, new R52 to R54, §5 E5 note, AT-10, AT-12, AT-14, AT-20, new AT-35 to AT-39, OQ-5 to OQ-10, OQ-23, OQ-24, OQ-34, new OQ-37, OQ-38; also 3.11 `consent_ref`, R42 note; payroll parameter seed rows are in hr 3.9). Amended 2026-10-17 (project-owner decisions P16 to P18 = hr H17 to H19: remuneration staff outside the Saudi Labour Law and WPS, loans deductible from final dues without the monthly caps, bank WPS formats to follow; 3.11, R18, R28, R41, R42, R49, R51, R54, AT-32, new AT-40, AT-41, OQ-9, OQ-10, OQ-13, OQ-34, OQ-38, blocking list).
**Compliance status:** Labour Law / WPS items checked 2026-10-17 against HRSD documents (Labour Law Arabic consolidated text with M/44 and English text; Implementing Regulations, Decision 115921, incl. Appendix 5 unified contract; HRSD WPS user guide and WPS upload page; awareness guide; 2025 amendments FAQ), downloaded 2026-10-17; the Arabic text governs (Art. 9). **GOSI not yet verified** (gosi.gov.sa blocked): every GOSI item carries **[CC-verify: GOSI source blocked]** and its parameters stay `unverified`. Legal points carry **[Needs expert sign-off: legal]** (OQ-34, OQ-37, OQ-38; hr OQ-32 to OQ-39). Overtime hour divisor, per-bank WPS layout/encoding, a WPS deductions field and a Mudad API have no official source and keep specific tags. WHT items were outside the check.
Depends on: `docs/specs/hr.md` (employees, contracts, components, leave, EOSB calculation, `StatutoryParameter`), `core.access` (`docs/specs/access.md`: `require`, segregation, document registry R34 to R36), `ledger.services` (`create_entry(post=True, grant, document)`, `reverse_entry`, `close_period` + `pending_documents`), `docs/specs/ledger-fx.md` (functional currency), `docs/specs/billing.md` (secondment recharge).
**[CC-verify]** = Saudi regulatory figure or format (GOSI, Labour Law, WPS, Mudad) written from memory, not checked against gosi.gov.sa, hrsd.gov.sa or mudad.com.sa (not reachable from this environment). Every such figure is an `hr.StatutoryParameter` row, seeded `unverified` (hr R6, R7). The compliance-checker verifies each before go-live.

## 1. Purpose

The payroll module runs monthly payroll **per entity** for every employing entity in the group (Almiras, subsidiaries and any entity added later as data): earnings from approved HR contracts, overtime, unpaid and sick-leave deductions, retro differences, deductions for interest-free employee loans and advances, GOSI contributions (employer and employee shares, Saudi and non-Saudi), the monthly end-of-service (EOSB) provision accrual on account 2500, final settlements, bilingual payslips, and WPS/Mudad salary files. Payroll officers prepare and submit; the entity's CFO approves; approval posts automatically through the ledger document registry, each document building its own lines. Payroll posts only to the employing entity's accounts, never intercompany: seconded staff cost is recharged by billing. Loans carry no interest in any entity (Sharia). Personal data stays masked and out of journal entries. Only the Saudi jurisdiction is built; payroll in Egypt, China or UAE entities and pay in foreign currencies are open questions.
*Amended 2026-10-17 (P8 to P15):* payroll follows each person's employment basis (hr R49): `ksa_employee` (all Saudi rules above), `local_entity_employee` (paid by the local group company in its functional currency under its country's parameters, not yet supplied, OQ-28) and `remuneration` (monthly remuneration from a Saudi entity, in a foreign currency if agreed, net of withholding tax; no GOSI, EOSB, overtime or leave). Runs are monthly in two groups with their own cut-off (27th in-country, 6th of the next month out-of-country). The `hr_officer` approves runs; the Managing Partner approves loans (profit-free, max 3 basic salaries) and Sharia-compliant bai' ajel financing. WPS files per salary bank (five banks).

## 2. Decisions

P1 to P7 come from existing documents (detail in hr.md §2); P8 to P15 are the project owner's decisions of 2026-10-17 (= hr H9 to H16); P16 to P18 are the project owner's further decisions of 2026-10-17 (= hr H17 to H19):

| # | Decision | Source | Date |
|---|---|---|---|
| P1 | CLAUDE.md rules: posting only via `ledger.services`, posted entries immutable (reversals only), balanced 2+ one-sided lines, closed periods reject postings, no posting to another entity's accounts, Sharia per entity, intercompany lines carry a counterparty, `Decimal` money (SAR 2 dp), `entity` FK everywhere, en/ar text, new entities are data. | CLAUDE.md | in repo |
| P2 | Staff create, the CFO approves, posting is automatic on approval; roles per entity. | Project owner (access A1) | 2026-10-16 |
| P3 | Agents create drafts only; never approve, post or issue. | CLAUDE.md; access A6, A7 | 2026-10-16 |
| P4 | Seconded staff billed to the receiving entity until HR ends the secondment (billing contracts, not payroll). | Project owner (billing D1(c)) | 2026-10-16 |
| P5 | Sub-ledger documents post through the registry: registered type, `build_lines`, grant for that document, creator/submitter/modifier cannot post (except receipts, which do not apply here). | Build decisions approved at merge (access A14, A15, A16) | 2026-10-16/17 |
| P6 | Proposed account codes stay proposed until the accountant/CFO reviews them. | Project owner (ledger-fx D10; invoicing D29) | 2026-10-17 |
| P7 | Hosting in KSA. | Project owner (invoicing D12) | 2026-10-16 |
| P8 | All group companies have employees, not all in KSA. Outside KSA: (a) employees of a locally registered group company follow that country's rules (local labour law, local payroll, held per entity with country parameters; Saudi rules never apply); (b) "remuneration" staff paid from a Saudi entity as a monthly remuneration with withholding tax considered: no labour regulation (no GOSI, no EOSB, no Saudi leave), a monthly remuneration net of WHT. Basis per person: `ksa_employee`, `local_entity_employee`, `remuneration`. | Project owner | 2026-10-17 |
| P9 | Foreign-currency pay: no for locally registered entities (own functional currency); yes for remuneration staff (paid in a foreign currency from the Saudi entity). | Project owner | 2026-10-17 |
| P10 | All payroll monthly. Cut-off for changes: the 27th of the month for local (in-country) payroll; the 6th of the following month for out-of-country payroll. | Project owner | 2026-10-17 |
| P11 | Overtime only in very special circumstances; not for certain grades (e.g. middle management and management); approved by the Managing Partner in Almiras, by the department head in other companies. | Project owner | 2026-10-17 |
| P12 | Loans: several options, all approved by the Managing Partner. Profit-free loan up to 3 basic monthly salaries. Larger amounts: the company may accept bai' ajel (deferred-payment sale) to finance an asset, with a profit margin decided per term and value, secured by the employee's salary and end-of-service benefit; must be Sharia compliant. Interest and late fees never allowed. | Project owner | 2026-10-17 |
| P13 | Annual leave 21 days minimum, 30 for some, never above 30 (hr R56; affects leave encashment R34). | Project owner | 2026-10-17 |
| P14 | The HR officer approves the payroll (not the CFO). The preparer (payroll officer) cannot approve; posting is automatic on approval. Roles `hr_officer`, `payroll_officer`, granted by the MP. | Project owner | 2026-10-17 |
| P15 | Salary banks: Riyad Bank, Saudi National Bank (SNB), Al Rajhi Bank, Bank AlJazira, Alinma Bank. WPS/salary file per bank. | Project owner | 2026-10-17 |
| P16 | "The Saudi Labour Law does not apply to people working outside Saudi Arabia." Remuneration staff paid from a Saudi entity while working abroad are not workers under the Saudi Labour Law: no EOSB, no Saudi leave, no SAR-pay requirement; WHT considered. They are outside WPS (not in the Saudi labour system). GOSI coverage for them is to be confirmed by the GOSI check. Confirms P8/P9. Resolves OQ-38. | Project owner | 2026-10-17 |
| P17 | Employee loans may be deducted from final dues (final settlement, including EOSB). The 10% (Art. 92) and 50% (Art. 93) caps apply only to monthly pay deductions, not to the final settlement. `PAYROLL_EOSB_DEDUCTION_ALLOWED` defaults to True for employee loans. Bai' ajel balances get the same treatment only if the bai' ajel contract includes the employee's signed undertaking; the Sharia sign-off on the structure stays (OQ-33). Resolves the legal part of OQ-34 for loans. | Project owner | 2026-10-17 |
| P18 | The salary (WPS) file formats of the five banks (P15) will be shared later. OQ-10 stays open; it blocks WPS file generation only, not the rest of payroll. | Project owner | 2026-10-17 |

Everything else (pay day, final-settlement and payment approvers, loan details, bai' ajel details, local-country payroll, WHT details) is in §9 with a safe default.

## 3. Data model

Conventions as hr.md §3: `entity` FK (employing entity, `PROTECT`), `created_by` (non-null), `created_at`, `modified_by`, `modified_at`, `source` (`human` / `import` / `system`); money `DecimalField(18, 2)` in `entity.base_currency`; Bi = `_en`/`_ar`; documents registered in the ledger registry are marked **[Reg]**.

### 3.1 `PayrollSettings` (one per entity)
| Field | Type | Req | Notes |
|---|---|---|---|
| entity | OneToOne Entity | Y | |
| enabled | Bool | Y | Default False (OQ-2) |
| pay_period | Choice `calendar_month` | Y | Only value built (OQ-1) |
| default_pay_day | PositiveSmallInt (1–31) | Y | Default 27 (OQ-1); a run's `pay_date` may differ |
| input_cutoff_day | PositiveSmallInt | Y | Default 20 (OQ-20). *Amended 2026-10-17 (P10):* 27, for the `in_country` group (R44) |
| out_of_country_cutoff_day | PositiveSmallInt | Y | *Added 2026-10-17 (P10):* 6, day of the **following** month, for the `out_of_country` group (R44) |
| salary_bank | Choice `riyad`, `snb`, `alrajhi`, `aljazira`, `alinma`, null | Y to generate WPS | *Added 2026-10-17 (P15, R51)* |
| day_basis | Choice `thirty`, `calendar` | Y | Default `thirty`: "Month: thirty days unless otherwise stipulated in the employment contract or the establishment's work regulations" (Labour Law Art. 2; downloaded 2026-10-17). *Amended 2026-10-17:* `calendar` refused without `day_basis_ref` |
| day_basis_ref | Char(200) | Y if `calendar` | *Added 2026-10-17 (R10).* Work-regulations article or contract clause stipulating calendar days |
| salary_bank_account | FK ledger.Account (same entity, asset, postable) | Y | Default 1100 |
| wps_format | Char(30), null | N | Format code (OQ-10); WPS generation refused while null. *Amended 2026-10-17 (P15):* must be the format parameter of `salary_bank` (R51) |
| wps_bank_code / employer_bank_iban | Char | N | PD-like, encrypted |

### 3.2 `PayrollInput` (unique per (entity, employee, period, kind, reference))
entity, employee, period (`YYYY-MM`), kind (`overtime_hours`, `unpaid_days`, `one_off_earning`, `one_off_deduction`, `notice_pay`, `bonus`), quantity (Decimal(8,2)) or amount (Money), component (FK hr.SalaryComponent, for earnings/deductions), reason (Bi), status (`draft`, `approved`, `rejected`), approved_by (`payroll.approve_input`, not own). Leave-driven inputs (unpaid days, sick pay %) are read from hr, not entered (R16, R17).
*Amended 2026-10-17 (P11):* `overtime_hours` inputs also carry `overtime_reason` (Bi, Arabic required), `overtime_approved_by` / `overtime_approved_at` (`payroll.approve_overtime`, routed per hr R55; R45). Kind `deferred_sale_installment` is generated from 3.11, never entered.
*Amended 2026-10-17 (Labour Law check):* `one_off_deduction` adds `deduction_basis` (`written_consent`, `court_order`, `damage_art91`, `statutory_other`) and `consent_ref` (Char(200), Y for `written_consent`) (R18; Art. 92); `overtime_hours` adds `overtime_date` and `worker_consent_ref` (Char(200), Y when the year's hours would exceed 720, R52).

### 3.3 `PayrollRun` **[Reg]** (unique `(entity, period, kind)` for `kind=regular`; *amended 2026-10-17 (P10):* unique `(entity, period, kind, payroll_group)`)
| Field | Type | Req | Notes |
|---|---|---|---|
| payroll_group | Choice `in_country`, `out_of_country` | Y | *Added 2026-10-17 (P10, R44).* `in_country` = `ksa_employee` and `local_entity_employee`; `out_of_country` = `remuneration` |
| number | Char(40) | Y at approval | Gap-free `{entity.code}-PAY-{year}-{000001}` (OQ-27) |
| period | Char(7) `YYYY-MM` | Y | |
| kind | Choice `regular`, `off_cycle` | Y | Off-cycle needs `reason_ar` |
| pay_date | Date | Y | Default from settings |
| entry_date | Date | computed | Last day of `period` (R12) |
| status | Choice `draft`, `calculated`, `submitted`, `returned`, `approved`, `paid`, `reversed` | Y | R7 |
| calc_version | PositiveInt | Y | +1 on every recalculation (R8) |
| inputs_hash | Char(64) | Y | Hash of every source row used (R8) |
| submitted_by / approved_by / approved_at | | | |
| journal_entry | FK ledger.JournalEntry, null | N | Set by the ledger at approval |
| warnings | JSON (en/ar) | Y | Missing GOSI number, expired documents, etc. |

### 3.4 `PayslipLine` and `Payslip`
`Payslip` (one per employee per run): entity, run, employee, contract_version, paid_days, gross, total_deductions, net, employer_gosi, eosb_accrual, gosi_wage, nationality_class (`saudi`, `non_saudi`, `gcc`), cohort (GOSI cohort code **[CC-verify: GOSI source blocked]**), leave_balance_snapshot, document_flag.
`PayslipLine`: entity, payslip, line_type (`earning`, `deduction`, `employee_gosi`, `employer_gosi`, `eosb_accrual`, `loan_installment`, `retro`), component (null), gosi_branch (`annuities`, `saned`, `occupational_hazards`, null), quantity, rate, amount (Money ≥ 0), account (FK ledger.Account, same entity), description (Bi), source_ref.
*Amended 2026-10-17 (P9, P12):* line types add `deferred_sale_installment` and `wht` (informational on the payslip; WHT is booked at payment, R43); lines add `currency` and `amount_txn` (`DecimalField(18, 3)`, checked against the currency's own decimals) for `remuneration`; `amount` stays the SAR/functional amount at 2 dp. `Payslip` adds `employment_basis` (copied) and `currency`.

### 3.5 `EosbProvisionEntry` (append-only sub-ledger of 2500)
entity, employee, date, kind (`opening`, `accrual`, `release`, `utilisation`), amount (signed Money), document_type/document_id (run or final settlement), eosb_calculation (FK hr.EosbCalculation). Employee balance = Σ amount.

### 3.6 `EmployeeLoan` **[Reg]** and `LoanInstallment`
| Field | Type | Req | Notes |
|---|---|---|---|
| employee | FK hr.Employee | Y | Same entity |
| kind | Choice `loan`, `salary_advance` | Y | Advance = one installment in the next run |
| principal | Money > 0 | Y | |
| disbursement_date | Date | Y | |
| purpose | Text | Y | Bi |
| status | Choice `draft`, `submitted`, `approved`, `active`, `settled`, `written_off` | Y | |
| schedule_version | PositiveInt | Y | Rescheduling = new approved version (R27) |
| interest_free | Bool, always True | Y | Shown on the loan agreement (R26) |

No interest, profit, fee or penalty field exists. `LoanInstallment`: entity, loan, schedule_version, period, amount, status (`planned`, `deducted`, `skipped`, `settled_in_final`), payslip_line (null).
*Amended 2026-10-17 (P12):* `cap_basic_at_approval` (Money, BASIC in force at approval) and `cap_amount` (= 3 × that BASIC) are stored on approval (R46); approved by `managing_partner`.

### 3.7 `PayrollPayment` **[Reg]**
entity, kind (`net_salaries`, `gosi`, `final_settlement`), run (null) / final_settlement (null), payment_date, bank_account (FK Account), amount, bank_reference, status (`draft`, `submitted`, `approved`, `reversed`), submitted_by, approved_by.

### 3.8 `FinalSettlement` **[Reg]**
entity, employee, termination (FK hr.Termination), eosb_calculation (FK, latest non-superseded), lines (`FinalSettlementLine`: line_type `salary_to_last_day`, `leave_encashment`, `eosb`, `notice_pay`, `other_earning`, `loan_recovery`, `employee_gosi`, `employer_gosi`, `other_deduction`; quantity, rate, amount, account, description Bi), gross, deductions, net, due_date (R34), status (`draft`, `submitted`, `approved`, `paid`, `reversed`), submitted_by, approved_by.

### 3.9 `WpsFile` and `MudadSubmission`
`WpsFile`: entity, run, format_code, format_parameter (FK hr.StatutoryParameter), generated_by, generated_at, sha256, employee_count, total_amount, exceptions (JSON: employee_code + reason), file (encrypted storage, purged after `WPS_FILE_RETENTION_DAYS`, default 30, OQ-10), downloads (via `AccessLog`).
`MudadSubmission`: entity, run, wps_file, submitted_on, reference, status (`submitted`, `accepted`, `rejected`), message (Bi), recorded_by. Manual entry (R32).
*Amended 2026-10-17 (R32, R53):* `WpsFile` adds `bank_processed_file` (encrypted storage), `bank_processed_sha256`, `bank_processed_received_at` and per-employee bank results (`TRN-REF`, `TRN-STATUS`, `TRN-DATE`, `RET-CDE`, `FILE-REJCDE`). `MudadSubmission.wps_file` must have `bank_processed_sha256` set: the file uploaded to Mudad is the bank-processed one. `MudadSubmission` adds `justifications` (JSON: employee_code, reason Bi, submitted_on, worker_response_due).

### 3.10 `GosiReconciliation`
entity, period, gosi_invoice_amount (entered), erp_amount (Σ employer + employee GOSI), difference, note (Bi), recorded_by. Report only (OQ-9).

### 3.11 Bai' ajel employee financing *(added 2026-10-17, P12; whole structure **[Needs expert sign-off: Sharia]**, salary/EOSB security **[Needs expert sign-off: legal]**)*
`DeferredSaleContract` **[Reg]** (unique `(entity, number)`):
| Field | Type | Req | Notes |
|---|---|---|---|
| number | Char(40) | Y at sale approval | Gap-free `{entity.code}-BAJ-{year}-{000001}` (default) |
| employee | FK hr.Employee | Y | Same entity; `ksa_employee` only (default, OQ-32) |
| asset_description | Text | Y | Bi |
| supplier | FK invoicing.Vendor, null | Y before purchase | |
| term_months | PositiveSmallInt | Y | Decided by the MP with the margin (R47) |
| cost | Money > 0 | Y at sale | = posted purchase cost of the asset (R47) |
| profit_amount | Money ≥ 0 | Y | Fixed at terms approval; never recomputed, never time-accruing (R48) |
| sale_price | Money | computed | cost + profit_amount; immutable after sale approval |
| profit_method | Choice `straight_line`, `proportional_outstanding` | Y | Default `straight_line` (OQ-33) |
| purchase_bill | FK invoicing bill, null | Y before sale | Posted, debit 1250, same entity |
| possession_date / possession_evidence_ref | Date / Char(200) | Y before sale | Company's ownership and possession before sale (R47) |
| sale_date | Date | Y at sale | ≥ possession_date |
| status | Choice `draft`, `terms_submitted`, `terms_approved`, `asset_acquired`, `sale_submitted`, `sold`, `settled`, `cancelled` | Y | R47 |
| schedule_version | PositiveInt | Y | Rescheduling = new MP-approved version, same sale_price (R48) |
| created_by / submitted_by / terms_approved_by / sale_approved_by | FK User | | |
| consent_ref | Char(200) | Y before `sale_submitted` | *Added 2026-10-17 (R49).* Employee's signed consent to salary deductions and undertaking (Art. 92) **[Needs expert sign-off: legal]** |
| final_dues_undertaking | Bool | Y (default False) | *Added 2026-10-17 (P17, R49).* True = the signed contract referenced by `consent_ref` includes the employee's undertaking that the outstanding balance may be deducted from final dues and EOSB; True refused while `consent_ref` is empty; immutable after sale approval |

No interest, penalty, late-fee, rate or "per annum" field exists. `DeferredSaleInstallment`: entity, contract, schedule_version, period, amount, status (`planned`, `deducted`, `skipped`, `settled_in_final`, `outstanding_after_exit`), payslip_line (null). `DeferredProfitRecognition` **[Reg]** (one per entity per period): lines per contract (amount recognised, Bi description), status (`draft`, `submitted`, `approved`), created_by, approved_by.

## 4. Rules

Enforcement: **svc** = `payroll/services.py`; **M** = model; **DB** = constraint/trigger; **Q** = `scope`; **L** = ledger (`validate_entry`, registry); **T** = scan test; **job** = command.

### Entity separation and set-up
1. A run, its payslips, lines, inputs, loans, payments, files and settlements share the employing `entity`; only that entity's employees and accounts are used. The ledger refuses other entities' accounts again at posting. **[M clean + svc + L; AT-1]**
2. Lists, payslips, files and reports go through `scope`; payslip detail needs `payroll.view_payslip` (logged, PDPL). Another entity's record → 404. **[Q + svc; AT-1]**
3. A run needs `PayrollSettings.enabled` and `HrSettings.jurisdiction = SA`. A new entity runs payroll once its HR set-up, payroll settings and accounts exist: no code change. **[svc; AT-2]**
4. Entities with another jurisdiction (Egypt, China, UAE) cannot run payroll (bilingual refusal) until OQ-3 is answered. **[svc; AT-2]**
    *Amended 2026-10-17 (P8):* OQ-3 is answered at decision level (R41): such entities run payroll for their `local_entity_employee` staff under their own parameters; this refusal stays until those parameters exist and are verified (OQ-28).
5. All amounts are in `entity.base_currency`, `Decimal`, rounded `ROUND_HALF_UP` to 2 dp per line per employee (OQ-25); float refused. Contracts in another currency are refused upstream (hr R18; OQ-4). **[svc + M; AT-3]**
    *Amended 2026-10-17 (P9):* except `remuneration` lines, which carry the contract currency (R42).

### Run lifecycle and posting
6. One `regular` run per (entity, period); `off_cycle` runs need an Arabic reason and cover only named employees. `number` is assigned at approval. **[DB unique + svc; AT-4]**
7. Status: `draft → calculated → submitted → approved → paid`; `submitted → returned → calculated` (recalculate); `approved → reversed` (R13). No other transition; on PostgreSQL a trigger refuses status changes outside the services (as invoicing). **[svc + M + DB; AT-4]**
8. `calculate_run` is deterministic from: approved contract versions in force (hr R20), approved hr leave and sick days, approved inputs up to cut-off, active loan installments, and parameters valid in the period. It replaces the run's lines and sets `calc_version` and `inputs_hash`. Approval recomputes the hash; if any source changed since calculation, approval is refused ("recalculate", en/ar) and the grant stays unused. **[svc; AT-5]**
9. Inputs approved after `input_cutoff_day` of the period are taken by the next regular run (default, OQ-20). Late leave or contract changes for a calculated period become retro lines in the next run (R14). **[svc; AT-8]**
    *Amended 2026-10-17 (P10):* cut-off days are decided and differ per payroll group: R44.
10. Included employees: an approved contract in force on any day of the period. Joiners and leavers are prorated: monthly amount × paid days / 30 (`day_basis=thirty`) or / calendar days (*amended 2026-10-17:* `calendar` only with `day_basis_ref`; Labour Law Art. 2; downloaded 2026-10-17). An employee whose termination is approved and whose `last_working_day` is in the period is paid through the final settlement, not the run (default). Document-expiry and missing-GOSI-number flags are warnings only (hr R44). **[svc; AT-6]**
11. Approval: `payroll.approve_run` (**A**, **P**) by the entity's approver, not the run's creator, submitter or last modifier (checked on the stored run, access R35). In one transaction the service calls `create_entry(entity, entry_date, description, lines=None, post=True, grant=grant, document=run)`; the registered `build_lines` builds the lines (§5 E1). If the ledger refuses (closed period, Sharia-blocked account, missing account), approval fails and the run stays `submitted`. **[svc + L registry; AT-7]**
    *Amended 2026-10-17 (P14):* "by the entity's approver" is **withdrawn — see P14, R50**: the run is approved by the entity's `hr_officer`; the posting mechanism above is unchanged.
12. The run entry is dated the last day of the period (salaries accrued in their month). A closed period refuses approval; the app registers `pending_documents` so `close_period` refuses while a run for that period is `submitted`. **[L + svc; AT-7]**
13. Approved runs are immutable. Correction: (a) retro or adjustment lines in a later run (default); or (b) `payroll.reverse_run` (**P**) by an approver who did not prepare it, only while no `PayrollPayment` for the run is approved: `reverse_entry` on the run's entry dated in an open period, run `reversed`, loan installments back to `planned`, EOSB sub-ledger rows mirrored. A reversed run is not re-approved; a new off-cycle or regular run is prepared. **[svc + L; AT-9]**

### Earnings and deductions
14. Earnings per employee = prorated fixed components + overtime + approved one-off earnings + retro differences (new terms − paid terms for past periods, per component, after hr R20) − unpaid-leave deduction − sick-leave reduction. **[svc; AT-6, AT-8]**
15. Overtime: hours from approved inputs only (no attendance, hr OQ-7). Pay per hour = (monthly wage / `day_basis` days / `daily_hours`) + 50% of (monthly BASIC / days / hours), i.e. hourly wage plus half the hourly basic (Labour Law Art. 107; unified contract cl. 11.4: "total hourly wage plus (50%) of the basic hourly wage"; downloaded 2026-10-17), using components flagged per hr 3.3. Caps and pre-approval: OQ-5. **[svc; AT-10]**
    *Amended 2026-10-17:* (a) the hour divisor (`day_basis` days × `daily_hours`) is not stated in the law or regulations; it is parameter `overtime.hour_divisor_rule`, `unverified` **[CC-verify: hour divisor not stated in HRSD sources]**; (b) hours worked on weekly rest days, Eids, National Day, Founding Day and official leave days are overtime (Art. 107); (c) paid compensatory leave instead of overtime pay (worker consent; ≥ 1.5 hours of leave per hour; ≤ 30 days a year; Regs Art. 22 bis) is not built (OQ-5); (d) caps: R52.
    *Amended 2026-10-17 (P11):* eligibility and approval are decided: R45. The formula applies to `ksa_employee`; `local_entity_employee` uses its jurisdiction's parameters; `remuneration` has no overtime.
16. Unpaid days (hr R34 and inputs): days × Σ components with `deducted_for_unpaid_leave` / 30 (OQ-6; month = 30 days, Labour Law Art. 2; downloaded 2026-10-17). **[svc; AT-6]**
17. Sick leave: for each `SickLeaveDay` with `pay_pct` < 100, deduct daily wage × (100 − pay_pct)% (tiers hr R32; Labour Law Art. 117; downloaded 2026-10-17). **[svc; AT-11]**
    *Amended 2026-10-17:* daily wage = Σ actual-wage components (those with `counts_for_eosb`, hr 3.3) / 30 ("with pay" / "three-quarters of the wage" = actual wage, Art. 2, 117). An official holiday inside sick leave is paid at 100% whatever the tier (Regs Art. 24; hr R32). **[svc; AT-11, AT-38]**
18. Deductions: employee GOSI, loan installments, approved one-off deductions. Limits from parameters: a loan installment deduction ≤ 10% of the wage, and total deductions ≤ 50% of the wage (Labour Law Art. 92(1), 93; downloaded 2026-10-17); an installment that would break a limit is reduced and the rest re-planned (warning). Net pay < 0 refuses calculation. Disciplinary fines are not supported (OQ-8). **[svc; AT-12]**
    *Amended 2026-10-17 (Art. 91 to 93, 66, 70; downloaded 2026-10-17):* (a) bases: the 10% loan cap is on the **actual wage** (Art. 92(1) "his wage"); the 50% total cap is on the **wage due for the period**, "in all circumstances" (Art. 93) **[Needs expert sign-off: legal]** on the bases (OQ-34); (b) a `one_off_deduction` for a private claim needs `deduction_basis=written_consent` with `consent_ref`, unless statutory: social insurance, court-ordered debt (≤ 1/4 of the wage, Art. 92(6)), damage recovery (≤ 5 days' wage a month, Art. 91), otherwise refused (en/ar); (c) disciplinary fines, if ever supported, ≤ 5 days' wage per violation and ≤ 5 days' wage deducted a month (Art. 66, 70); (d) the WPS guide also warns when deductions exceed 50% (WPS-G). **[svc; AT-12, AT-37]**
    *Amended 2026-10-17 (P17):* the 10% and 50% caps apply to monthly runs (regular and off-cycle payslips) only; a `FinalSettlement` is never capped (R28, R49). **[svc; AT-40]**
19. Each line has an account of the entity: earnings → component `expense_account` (default 5100); employer GOSI → 5110; deductions → their liability or asset account (§5). None may be Sharia-prohibited where enforced (checked at calculation; ledger again at posting). **[svc + L; AT-13]**

### GOSI (all figures **[CC-verify: GOSI source blocked]**; parameters stay `unverified`)
20. Contribution wage = Σ components with `counts_for_gosi` (basic + housing **[CC-verify: GOSI source blocked]**; housing deemed at 25% of basic when not paid in cash **[CC-verify: GOSI source blocked]**), as registered at the start of the month **[CC-verify: GOSI source blocked]**, bounded by floor SAR 1,500 and cap SAR 45,000 per month **[CC-verify: GOSI source blocked]**. **[svc; AT-14]**
21. Rates from parameter `gosi.rates` by nationality class, cohort and branch. Seed (unverified): Saudi annuities 9% employer + 9% employee for employees insured before the new Social Insurance Law date (2024-07-03 **[CC-verify: GOSI source blocked]**), with stepped higher annuity rates for new entrants **[CC-verify: GOSI source blocked]**; SANED 0.75% employer + 0.75% employee (Saudi only) **[CC-verify: GOSI source blocked]**; occupational hazards 2% employer (Saudi and non-Saudi) **[CC-verify: GOSI source blocked]**; non-Saudi: occupational hazards only, no employee share **[CC-verify: GOSI source blocked]**. GCC nationals: OQ-9. **[svc + parameter; AT-14]**
22. Each branch and party is a separate line per employee, rounded per R5; employee share is a deduction, employer share an expense. Both credit 2340 (proposed). **[svc; AT-14]**
23. `GosiReconciliation` compares ERP contributions with the GOSI monthly invoice entered by the payroll officer; differences are reported, never posted automatically (OQ-9). **[svc; AT-15]**

### EOSB provision (account 2500)
24. Each regular run adds, per employee, `eosb_accrual` = required provision at period end − the employee's `EosbProvisionEntry` balance. Required provision = `hr.calculate_eosb(employee, as_of=period end, kind=measurement basis)`; default basis = full award as on employer termination (fraction 1, OQ-11). Positive → Dr 5120 (proposed) / Cr 2500; negative → reverse sides. One `accrual` sub-ledger row per employee. **[svc; AT-16]**
25. Tie-out: Σ `EosbProvisionEntry` per entity = ledger balance of 2500 at every run approval; a difference (e.g. a manual journal on 2500) is a run warning and appears on the EOSB report (en/ar). **[svc + report; AT-16]**

### Loans and advances (Sharia)
26. Loans and advances are interest-free in **every** entity, whatever `sharia_enforced` says: the model has no interest/profit/fee field; Σ installments of each schedule version = outstanding principal exactly; no line, input or account may add a charge for a loan; a late or skipped installment adds nothing. Loan agreements and payslips state "interest-free / قرض حسن بدون فوائد" **[AR-review]**. Any attempt is refused with a Sharia message (en/ar). **[svc + M + T; AT-17]**
27. Lifecycle: drafted by `payroll.create_loan`, approved by `payroll.approve_loan` (**A**, **P**): approval posts the disbursement (§5 E4). Installments are deducted by runs (R18); rescheduling or skipping needs a new approved schedule version. Policy limits (amount, count, eligibility): OQ-7. **[svc + L; AT-17]**
    *Amended 2026-10-17 (P12):* the approver is `managing_partner` (not the entity approver) and the amount cap is R46; other limits stay OQ-32.
28. On termination, the outstanding balance becomes a `loan_recovery` line in the final settlement (deduction from EOSB and dues: Art. 88 lets the employer deduct "any debt due to him because of the work" from amounts owed to the worker; whether that covers an employer loan or bai' ajel receivable and whether the 10% / 50% caps apply at final settlement is not stated **[Needs expert sign-off: legal]**, OQ-34; `PAYROLL_EOSB_DEDUCTION_ALLOWED` stays False, R49); an unrecovered remainder needs an approved write-off (OQ-7; account not yet decided, so write-off is refused until then). **[svc; AT-20]**
    *Amended 2026-10-17 (P17):* decided for employee loans (`loan` and `salary_advance`): the final settlement nets the full outstanding balance against all final dues, EOSB included, without the 10% / 50% caps; `PAYROLL_EOSB_DEDUCTION_ALLOWED` defaults to **True** (it stays a setting so it can be switched off). If the dues are lower than the balance, the remainder follows the write-off rule above. **[svc + setting; AT-20, AT-40]**

### Payments, WPS, Mudad
29. `PayrollPayment` (net salaries, GOSI, final settlement) is drafted by the payroll officer after the bank release and approved by the approver (**A**, **P**), which posts §5 E3. Amount ≤ open payable for that run or settlement; partial payments allowed; run `paid` when net is fully paid. **[svc + L; AT-18]**
30. Payroll never pays or posts for another entity; no shared-payroll intercompany flow (OQ-2). **[svc + L; AT-1]**
31. WPS file: generated only from an `approved` run with `PayrollSettings.wps_format` set; content and layout from the format parameter (OQ-10: per-bank layout and encoding **[CC-verify: per-bank format not in HRSD sources]**). Employees without a valid IBAN are listed as exceptions, not included. Generation writes nothing to the run or ledger; sha256, generator and each download are logged; the file is purged after the retention days. **[svc; AT-19]**
    *Amended 2026-10-17 (WPS-G; downloaded 2026-10-17):* the Ministry field set is seeded as parameter `wps.fields.mol` (`verified`): **header** (once): DEST-ID (bank code), ESTB-ID (establishment ID at the bank), BANK-ACC, 32A-CCY, 32A-VAL (transfer date), 32A-AMT (total), D-CATE (optional debit date), FILE-REF, FILE-REJCDE (bank fills), MOL-ESTBID (ministry establishment no., e.g. 1-1001); **content** (per employee): 32B-AMT, ACC-59 (IBAN or card), NAM-57 (name per bank), BANK-57 (bank code; SWIFT outside KSA), DET-70 (optional), RET-CDE (bank), MOL-BAS, MOL-HALI, MOL-OEA, MOL-ALL, MOL-ID (national ID or iqama), TRN-REF / TRN-STATUS / TRN-DATE (bank). Mapping: MOL-BAS = `BASIC` only (0 for adjustment files); MOL-HALI = `HOUSING` monthly cash amount (0 when the contract has no cash `HOUSING`, i.e. housing in kind, or housing is paid annually in advance); MOL-OEA = other earnings; bank-filled fields are left empty. Mapping deductions is **not** done until confirmed: the guide mentions a "deductions column" but its field table has none (MOL-ALL is total entitlements) **[CC-verify: deductions field to confirm with each bank]**. `wps.format.<bank>` (layout, encoding) stays `unverified`.
32. Mudad submission is manual by default: the officer uploads the file in Mudad and records reference and status. No API call (OQ-10). **[svc; AT-19]**
    *Amended 2026-10-17 (WPS-P; WPS-G; FAQ; downloaded 2026-10-17):* the flow is ERP file → bank → bank-processed file (with TRN-* and RET-CDE) → Mudad compliance system, which is now the **only** upload route. The officer stores the bank-processed file on the `WpsFile` (both hashes recorded); a `MudadSubmission` is refused without it. No Mudad API is documented **[CC-verify: no Mudad API in HRSD sources]**. Alerts: R53.

### Payslips
33. One payslip per employee per approved run, rendered from stored lines (never recalculated), bilingual (English and Arabic on the same slip, default OQ-16): entity legal names, period, employee code and name, earnings, deductions, employee GOSI, net, loan balance, annual-leave balance. Only `payroll.view_payslip` holders and (later) the employee see it; each view or print logged. **[svc; AT-21]**

### Final settlement
34. Created as a draft by hr termination approval (hr R38) with lines: salary to `last_working_day`, leave encashment (balance × daily wage of `counts_for_eosb` components / 30: wage for accrued untaken leave plus pro rata for the part year, on the actual wage; Labour Law Art. 2, 111; downloaded 2026-10-17), EOSB = hr snapshot award, notice pay input, other approved earnings, GOSI on the salary part **[CC-verify: GOSI source blocked]**, loan recovery, other deductions. `due_date` = `last_working_day` + 7 days (employer ends) or + 14 days (employee resigns) (Labour Law Art. 88; unified contract cl. 11.9; downloaded 2026-10-17); an alert is sent 3 days before. *Amended 2026-10-17:* the 14 days apply whenever **the worker** ended the contract: hr kinds `resignation`, `employee_notice_art75`, `art81_employee_leaves`, `female_marriage_childbirth` (hr R64); every other kind 7 days. Leave encashment exists only in the final settlement: cash instead of leave during service is refused (Art. 109(2)). Approval (**A**, **P**) posts §5 E5: EOSB is charged first to the employee's 2500 balance, the shortfall to 5120, an excess released to 5120 (`utilisation` + `release` sub-ledger rows). **[svc + L; AT-20]**

### Intercompany, privacy, Sharia, agents
35. Payroll posts no intercompany lines (never 1210/2110): seconded staff cost stays in the employing entity. Recharge is billing's contract invoice (billing B3, P4). Payroll provides a monthly **secondment cost report** per assignment (`employee_code`, gross, employer GOSI, EOSB accrual, days on secondment) for the accountant pricing or checking billing contracts; whether recharge follows actual cost: OQ-15. **[svc + report; AT-22]**
36. Journal entries are summarised per account per document; descriptions carry the document number and period only, never names, IDs or individual pay (PDPL). Per-employee detail stays in payroll. **[svc build_lines; T; AT-7]**
37. No payroll account may be Sharia-prohibited where enforced; no interest arises on late salary, loans or GOSI (late GOSI fines, if any, are a manual matter, OQ-9). **[svc + L; AT-13]**
38. No payroll permission is **Ag** (default, OQ-21); system users have none. **[catalogue; AT-23]**
39. An approver who is also an employee in the run may approve it only if their own payslip has no inputs (overtime, one-off, retro) in that run; otherwise the approval is refused for them and goes to another approver or the `managing_partner` fallback (default, OQ-14). **[svc; AT-24]**
40. All messages, payslips, reports and file exception lists are en/ar. **[T; AT-21]**

### Project-owner decisions 2026-10-17 (P8 to P15)
41. **Employment basis (P8).** Each payslip follows the employee's basis (hr R49):
    - `ksa_employee`: R10 to R28 and R34 as written (GOSI, EOSB provision, Saudi overtime, leave, deduction caps).
    - `local_entity_employee`: paid by the employing local entity in its `base_currency`, with that jurisdiction's parameters only; no `SA` parameter is read (hr R51). Refused until those parameters exist and are verified (R4, OQ-28).
    - `remuneration`: only the monthly `REMUNERATION` amount (prorated by R10 for the joining and leaving months, default), approved one-off earnings/deductions and loan or bai' ajel installments if any (OQ-32); never GOSI (R20 to R23), EOSB accrual (R24), overtime, unpaid or sick-leave deductions, leave encashment or the Saudi deduction caps of R18 (net ≥ 0 still required). WHT per R43.
    **[svc; AT-25]**
    *Amended 2026-10-17 (P16):* the `remuneration` exclusions from the Labour Law (EOSB, leave, overtime, caps, SAR pay, WPS) are decided: these persons work outside Saudi Arabia and are not workers under the Saudi Labour Law. The GOSI exclusion is **not** covered by P16 and stays a default until the GOSI check confirms it **[CC-verify: GOSI source blocked]** (OQ-9).
42. **Foreign-currency remuneration (P9).** A `remuneration` payslip is in the contract currency: `amount_txn` at that currency's decimals (≤ 3), and `amount` in SAR at 2 dp at the ledger rate for the run's `entry_date` (ledger-fx rate rules). The run entry carries those lines with currency, transaction amount and rate; the 2330 line is a foreign-currency monetary line (revalued and settled per ledger-fx). `ksa_employee` and `local_entity_employee` lines are in `entity.base_currency` only. **[svc + L; AT-26]**
    *Note 2026-10-17:* Labour Law Art. 90(1) requires a worker's wage and all dues to be paid in the official currency, Art. 90(2) through banks accredited in the Kingdom; this rule stands only if `remuneration` persons are not workers **[Needs expert sign-off: legal]** (OQ-38).
    *Amended 2026-10-17 (P16):* decided: remuneration staff working abroad are not workers under the Saudi Labour Law, so Art. 90 does not apply to them; this rule stands (OQ-38 resolved).
43. **WHT on remuneration (P8).** WHT is "considered" on every remuneration payment: the run credits 2330 with the gross remuneration; the payment (`PayrollPayment`, kind `net_salaries`) pays the net and credits WHT to 2220 (proposed in invoicing.md §5), using the global `invoicing.WhtCategory` table (category for remuneration, rate, treaty certificate, return line) **[Needs expert sign-off: tax]**, **[CC-verify]** applicability to non-resident individuals' remuneration, rate and the SAR rate date. The payslip shows gross, WHT and net ("net of WHT"). The employee bears WHT (no gross-up). Payroll WHT feeds the monthly WHT return data (needs `invoicing.wht_return_data` to include payroll payments, OQ-30). **[svc + L; AT-26]**
44. **Payroll groups and cut-off (P10).** Every run is monthly (`calendar_month`) and belongs to one group: `in_country` (`ksa_employee`, `local_entity_employee`) or `out_of_country` (`remuneration`) (mapping: default reading of "local / out-of-country", OQ-31). Inputs and contract or leave changes approved on or before the 27th of the period (`input_cutoff_day`) go into that period's `in_country` run; on or before the 6th of the following month (`out_of_country_cutoff_day`) into that period's `out_of_country` run; later ones go to the next run as retro or inputs (R9, R14). Both runs are dated the last day of the period (R12); an `out_of_country` run approved after month end needs the period still open (OQ-31). **[svc + DB unique; AT-27]**
45. **Overtime (P11).** An `overtime_hours` input is refused when: the employee is `remuneration`; or the grade on the contract version in force (hr R54) has `overtime_eligible` False; or `overtime_reason_ar` is empty. It enters a run only after `payroll.approve_overtime` (**A**) by the route of hr R55: `managing_partner` where the route is `managing_partner` (ALM); else the user linked to the head of the employee's department, holding that permission in the entity (proposed role `department_head`, access OQ-28). Never the employee themself, never the input's creator; a head's own overtime or a department without head goes to `managing_partner` (default, hr OQ-30). Unapproved overtime is left out with a run warning (en/ar). **[svc + `require(..., document=input)`; AT-28]**
    *Note 2026-10-17:* excluding management grades is consistent with the law: high-level management posts with employer-like authority are outside Arts. 98 and 101 (Labour Law Art. 108; downloaded 2026-10-17). Statutory caps: R52.
46. **Profit-free loan cap (P12).** `payroll.approve_loan` (**A**, **P**) is held by `managing_partner` only (not the entity approver, not the payroll officer). At approval, the principal plus the employee's outstanding loan and advance balances must be ≤ 3 × the monthly BASIC in force on the approval date (default: total outstanding, advances included, OQ-32); otherwise refused with an en/ar message naming the cap and pointing to bai' ajel (R47). No profit, fee or charge ever (R26). **[svc + M; AT-29]**
47. **Bai' ajel (P12) [Needs expert sign-off: Sharia].** A Sharia-compliant deferred sale, never a loan: (1) `payroll_officer` drafts the request (asset, supplier, quote, term); (2) `managing_partner` approves the terms: `term_months` and a fixed `profit_amount` decided per term and value (`payroll.approve_deferred_sale_terms`, **A**); (3) the company buys the asset in its own name through an invoicing bill debiting 1250 (E9) and records possession (date and evidence); (4) only then the sale is submitted and approved by `managing_partner` (`payroll.approve_deferred_sale`, **A**, **P**), which posts E10 at `sale_price` = posted cost + `profit_amount`, with an installment schedule whose sum = `sale_price` exactly; (5) installments are deducted by the runs (`deferred_sale_installment`, Cr 1240); (6) deferred profit is recognised per period per `profit_method` through `DeferredProfitRecognition` (E12). The sale is refused before the purchase bill is posted, before possession is recorded, or if `sale_date` < `possession_date`. Applies in **every** entity whatever `sharia_enforced`. **[svc + M + L registry; AT-30]**
48. **No increase ever (P12).** Bai' ajel and loans have no interest, penalty, late-fee or rate field; `profit_amount` and `sale_price` are immutable after approval; a late, skipped or rescheduled installment (new schedule version, MP-approved) never changes `sale_price`; any input, line or account adding a charge is refused with a Sharia message (en/ar). Profit is fixed at contract and recognised over the term, never accrued by time elapsed after due dates. Early-settlement rebate (ibra'): OQ-33. **[svc + M + T; AT-31]**
49. **Salary and EOSB security (P12) [Needs expert sign-off: legal].** Installments are deductions; whether they count in the R18 caps (10% / 50%; Labour Law Art. 92, 93; downloaded 2026-10-17) is a parameter (default: they count, like loans). *Amended 2026-10-17:* a bai' ajel installment is not a "loan from the employer"; it may fall under Art. 92(4) ("any other benefit": no 10% cap) or need written consent **[Needs expert sign-off: legal]** (OQ-34). Default until sign-off: installments count within the Art. 93 50% total cap (applies "in all circumstances"); the sale needs a signed consent and undertaking reference (`consent_ref`) before `sale_submitted`; the 10% cap stays a parameter (`deferred_sale.apply_loan_cap`, default True). On termination the outstanding `sale_price` balance becomes a `deferred_sale_recovery` line in the final settlement (with `loan_recovery`, R28). Recovery from the EOSB award is applied only while setting `PAYROLL_EOSB_DEDUCTION_ALLOWED` is True (default False until legal sign-off, OQ-34); while False, recovery is limited to the other dues and the rest stays on 1240 as `outstanding_after_exit`, with an en/ar warning. Unrecognised deferred profit on an exit is recognised as in E13 (default, OQ-33). **[svc + setting; AT-32]**
    *Amended 2026-10-17 (P17):* the setting now defaults to True (R28). A bai' ajel balance is recovered from all final dues, EOSB included, without the 10% / 50% caps, **only** when `final_dues_undertaking` is True on the contract; otherwise recovery is limited to the non-EOSB dues and the rest stays on 1240 as `outstanding_after_exit` with an en/ar warning (default, OQ-34). Bai' ajel as a whole stays blocked by the Sharia sign-off (OQ-33) and tax (OQ-35). **[svc + setting; AT-32, AT-41]**
50. **HR officer approves runs (P14).** `payroll.approve_run` (**A**, **P**) is held by `hr_officer`, not by `approver`. The run's creator, submitter or last modifier (the `payroll_officer`) is refused (access R14, R35); approval posts automatically (R11). Default safeguards (OQ-36): an `hr_officer` who created or approved any input in the run is refused for that run; R39 applies to the `hr_officer` (own pay with inputs → another `hr_officer` or `managing_partner`). Final settlements, payments and run reversals stay with `approver` (default, OQ-36). **[svc + access; AT-33]**
51. **Salary banks and WPS (P15).** `PayrollSettings.salary_bank` is one of Riyad Bank, SNB, Al Rajhi Bank, Bank AlJazira, Alinma Bank; another bank is refused. Each bank has its own WPS format parameter (`wps.format.<bank>`, layout and encoding per bank **[CC-verify: per-bank format not in HRSD sources]**; Ministry fields verified, R31); `wps_format` must be that bank's; generation is refused while that parameter is missing (or unverified with `HR_REQUIRE_VERIFIED_PARAMETERS=True`). `remuneration` staff are never put in a WPS file (no labour regulation); they are paid by bank transfer recorded as `PayrollPayment`. **[svc; AT-34]**
    *Amended 2026-10-17 (P16, P18):* the WPS exclusion of `remuneration` staff is decided (not in the Saudi labour system). The five banks' file formats will be supplied by the project owner; until then the `wps.format.<bank>` parameters cannot be completed and generation is refused as above (OQ-10); nothing else in payroll waits for them.

### Labour Law / WPS check 2026-10-17 (`docs/compliance/hr-payroll-labour-law-2026-10-17.md`)
52. **Overtime statutory caps.** For `ksa_employee`: (a) Σ approved overtime hours in the employee's year (per hr `calendar_basis`) ≤ `overtime.max_hours_year` = 720; an input that would exceed it is refused unless `worker_consent_ref` is stored (Regs Art. 22; downloaded 2026-10-17); (b) in the exceptional cases of Art. 106, hours per `overtime_date` ≤ 10 a day and ≤ 60 in the week, refused above (Labour Law Art. 106; downloaded 2026-10-17). Inputs need `overtime_date` for (b). **[svc; AT-35]**
53. **Mudad deadlines and alerts.** For each approved `in_country` run of an entity with WPS, if no `MudadSubmission` is recorded, alerts (en/ar) go to the payroll officer and hr_officer on day 7 of the following month (default, before Mudad's own day-10 e-mail), on day 10 and on day 15; Mudad refers to inspection on day 20 (WPS-G). A justification recorded in `MudadSubmission.justifications` is due within 10 days of Mudad's notice; the worker's response is due within 3 days (WPS-G; downloaded 2026-10-17). **[job + svc; AT-39]**
54. **Fines fund (if ever supported, OQ-8).** Disciplinary fines would credit a workers' fines-fund liability, never income, kept in a fines register and used only for the benefit of the establishment's workers (Labour Law Art. 73; Regs Art. 70; downloaded 2026-10-17). Not built: any fine input is refused today. **[svc; AT-37]**
    *Amended 2026-10-17:* WPS covers all private-sector workers, Saudi and expatriate; only "expatriate workers outside the Kingdom" and workers on probation are excluded from the compliance rate (WPS-G; downloaded 2026-10-17). Whether `remuneration` persons are workers at all (Labour Law Art. 2, 7, 90) is **[Needs expert sign-off: legal]** (OQ-38; hr OQ-38); if they are, SAR pay through accredited banks (Art. 90), WPS, EOSB and leave apply and P8/P9 must be revisited by the Managing Partner. The exclusion above stays as built default until then.
    *Amended 2026-10-17 (P16):* decided: remuneration staff work outside Saudi Arabia and are not workers under the Saudi Labour Law; the WPS exclusion is final (consistent with WPS-G excluding "expatriate workers outside the Kingdom"). OQ-38 resolved.

## 5. Accounting

Codes from `ledger/coa_template.py`. Existing: 1100 Cash and bank, 2300 Accrued expenses, 2500 End-of-service provision, 5100 Salaries and benefits, 5110 GOSI, 5700 Bank charges. **Proposed, CFO to approve (not added to the template until approved; P6, OQ-12).** Codes chosen to avoid every code in `ledger/coa_template.py` and those already proposed in invoicing.md §5 (1330, 2220, 2310 deferred income, 2320 security deposits, 4120 to 4310, 5910, 5920) and ledger-fx.md (3400, 4700, 4710, 5750, 5760, 5770; 1101 example sub-account):

| Code | name_en | name_ar [AR-review] | Type | Flags | Used for |
|---|---|---|---|---|---|
| 1230 | Employee loans and advances | قروض وسلف الموظفين | asset | monetary | Loans (E4) |
| 2330 | Salaries payable | رواتب مستحقة الدفع | liability | monetary | Net pay (E1, E5). Alternative if rejected: 2300 |
| 2340 | GOSI payable | اشتراكات التأمينات الاجتماعية المستحقة | liability | monetary | Employer + employee GOSI (E1) |
| 5120 | End-of-service benefit expense | مصروف مكافأة نهاية الخدمة | expense | | EOSB accrual (E1, E5). Alternative: 5100 |
| 2350 / 5130 | Accrued leave / Leave expense | مخصص الإجازات / مصروف الإجازات | liability / expense | | Only if hr OQ-6 decides leave is accrued |
| 1240 | Employee deferred-sale (bai' ajel) receivable | ذمم البيع الآجل للموظفين | asset | monetary | Sale price receivable (E10, E11, E13). *Added 2026-10-17 (P12)* |
| 1241 | Deferred bai' ajel profit (contra 1240) | أرباح البيع الآجل المؤجلة | asset (contra, credit balance) | monetary | Unearned profit, shown as a deduction from 1240 (E10, E12, E13) **[Needs expert sign-off: Sharia]** presentation (AAOIFI). *Added 2026-10-17* |
| 1250 | Assets acquired for employee deferred sale | أصول مشتراة للبيع الآجل للموظفين | asset | | Cost of the asset owned by the company between purchase and sale (E9, E10). *Added 2026-10-17* |
| 4610 | Deferred-sale (bai' ajel) profit income | أرباح البيع الآجل | income | none (Sharia-compliant sale profit; never `purify`, never interest) | Profit recognised (E12, E13). *Added 2026-10-17* |

Free-code check (2026-10-17): 1240, 1241, 1250 and 4610 are not in `ledger/coa_template.py` and not proposed in invoicing.md §5, ledger-fx.md or the codes above. Existing proposed 2220 (WHT payable, invoicing.md §5) is used for remuneration WHT (E8); FX differences use ledger-fx proposed 4700 / 5750. All stay **proposed, CFO to approve** (OQ-12).

1230, 2330 and 2340 should carry the `is_control` flag proposed in invoicing.md §5 (payroll documents only, no manual journals), if that flag is adopted.

Registered types (`PayrollConfig.ready()`): `PayrollRun` (post `payroll.approve_run`, reverse `payroll.reverse_run`), `PayrollPayment` (`payroll.approve_payment`), `EmployeeLoan` (`payroll.approve_loan`), `FinalSettlement` (`payroll.approve_final_settlement`); *added 2026-10-17:* `DeferredSaleContract` (`payroll.approve_deferred_sale`), `DeferredProfitRecognition` (`payroll.approve_profit_recognition`); `PayrollRun` posting permission held by `hr_officer` (R50), `EmployeeLoan` by `managing_partner` (R46); each with `build_lines`, `creator_field="created_by"`, and `pending_documents` for submitted runs. Figures below use the **unverified** seed GOSI rates **[CC-verify: GOSI source blocked]** and the EOSB table verified 2026-10-17 (Labour Law Art. 84, 85); GOSI amounts change when those parameters are verified.

**E1. Monthly run, ALM, March 2027, dated 2027-03-31.** Saudi employee S1: BASIC 10,000.00, HOUSING 2,500.00, TRANSPORT 1,000.00 (GOSI wage 12,500.00; employee 9% + 0.75% = 1,218.75; employer 9% + 0.75% + 2% = 1,468.75). Non-Saudi N1: BASIC 6,000.00, HOUSING 1,500.00, TRANSPORT 500.00 (GOSI wage 7,500.00; employer 2% = 150.00); loan installment 500.00. EOSB accrual: S1 562.50, N1 333.33.

| Account | Dr | Cr |
|---|---|---|
| 5100 Salaries and benefits | 21,500.00 | |
| 5110 GOSI (employer) | 1,618.75 | |
| 5120 EOSB expense (proposed) | 895.83 | |
| 2340 GOSI payable (proposed) | | 2,837.50 |
| 1230 Employee loans and advances (proposed) | | 500.00 |
| 2330 Salaries payable (proposed) | | 19,781.25 |
| 2500 End-of-service provision | | 895.83 |
| **Total** | **24,014.58** | **24,014.58** |

A negative EOSB accrual swaps 5120 and 2500. Retro, overtime and one-off earnings add to the 5100 (or component account) line; unpaid-leave and sick reductions lower it.

**E2. GOSI payment** (2,837.50): Dr 2340 2,837.50 / Cr 1100 2,837.50.

**E3. Net salary payment** (19,781.25): Dr 2330 19,781.25 / Cr 1100 (settings bank account) 19,781.25. Bank charges, if any, are a separate bank document (Dr 5700).

**E4. Loan disbursement** (3,000.00, interest-free): Dr 1230 3,000.00 / Cr 1100 3,000.00. Repayment through E1 (Cr 1230). No interest or fee line ever exists.

**E5. Final settlement, N1 resigns after 4 years** (wage basis 8,000.00; full award 16,000.00 = provision balance; resignation fraction 1/3 → EOSB 5,333.33 (Labour Law Art. 85; downloaded 2026-10-17; N1 is on a fixed-term contract, hr R16); 10 days leave 2,666.67; loan balance 1,000.00):

| Account | Dr | Cr |
|---|---|---|
| 2500 End-of-service provision (utilise 5,333.33, release 10,666.67) | 16,000.00 | |
| 5100 Salaries and benefits (leave encashment) | 2,666.67 | |
| 5120 EOSB expense (release of excess provision) | | 10,666.67 |
| 1230 Employee loans and advances | | 1,000.00 |
| 2330 Salaries payable | | 7,000.00 |
| **Total** | **18,666.67** | **18,666.67** |

Payment as E3 (kind `final_settlement`). If the award exceeds the provision, the shortfall is Dr 5120.

**E6. Run reversal** (R13): `reverse_entry` mirror of E1, dated in an open period, before any payment.

**E7. Secondment recharge**: none in payroll. Billing invoices the receiving entity (billing B3: Dr 1210 counterparty / Cr 4210 / Cr 2200 in the seconding entity).

**No entry** for: inputs, calculation, submission, payslips, WPS files, Mudad records, GOSI reconciliation, draft loans or settlements, leave accrual (until hr OQ-6).

*Added 2026-10-17 (P8, P9, P12).* Rates and WHT % below are illustrative only **[CC-verify]**; bai' ajel figures are illustrative, the MP decides the margin.

**E8. Remuneration, ALM out-of-country run March 2027.** R1 (`remuneration`, USD): USD 5,000.00; rate USD→SAR 3.7500 on 2027-03-31.

E8a, run approval (dated 2027-03-31):
| Account | Currency | Transaction | Rate | Dr SAR | Cr SAR |
|---|---|---|---|---|---|
| 5100 Salaries and benefits (component `REMUNERATION` account; separate account: OQ-29) | USD | 5,000.00 | 3.7500 | 18,750.00 | |
| 2330 Salaries payable (proposed) | USD | 5,000.00 | 3.7500 | | 18,750.00 |

No GOSI, EOSB (2500) or 2340 line. E8b, payment 2027-04-08 at 3.7600, WHT 5% (illustrative) = USD 250.00 = SAR 940.00:
| Account | Currency | Transaction | Rate | Dr SAR | Cr SAR |
|---|---|---|---|---|---|
| 2330 Salaries payable | USD | 5,000.00 | 3.7500 (carried) | 18,750.00 | |
| 1100 Cash and bank (USD) | USD | 4,750.00 | 3.7600 | | 17,860.00 |
| 2220 WHT payable (proposed, invoicing) | SAR | 940.00 | | | 940.00 |
| 5750 FX loss – realised (proposed, ledger-fx) | SAR | 50.00 | | 50.00 | |
| **Total** | | | | **18,800.00** | **18,800.00** |

WHT remittance is invoicing E22 (CFO-approved payment).

**E9. Bai' ajel purchase** (invoicing bill, posted by invoicing's approval flow; cost 30,000.00, VAT treatment OQ-35): Dr 1250 30,000.00 / Cr 2100 30,000.00 (then the vendor payment, invoicing).

**E10. Bai' ajel sale** (MP approves, `DeferredSaleContract`; profit 3,600.00, 24 months, sale price 33,600.00):
| Account | Dr | Cr |
|---|---|---|
| 1240 Employee deferred-sale receivable (proposed) | 33,600.00 | |
| 1250 Assets acquired for deferred sale (proposed) | | 30,000.00 |
| 1241 Deferred bai' ajel profit (proposed) | | 3,600.00 |
| **Total** | **33,600.00** | **33,600.00** |

**E11. Installment** in E1 (monthly run): Cr 1240 1,400.00 (33,600.00 / 24), reducing the 2330 credit by the same amount.

**E12. Monthly profit recognition** (`DeferredProfitRecognition`, `straight_line`: 3,600.00 / 24): Dr 1241 150.00 / Cr 4610 150.00.

**E13. Exit after 10 installments** (outstanding 19,600.00; unrecognised profit 2,100.00), in the final settlement (E5) when dues cover it (R49): Cr 1240 19,600.00 as a deduction (2330 lower by 19,600.00); and Dr 1241 2,100.00 / Cr 4610 2,100.00 (sale price unchanged; rebate OQ-33). Not recovered: stays on 1240; profit recognition continues per schedule (default, OQ-33).

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| `hr` (internal) | In | Contracts, components, leave, sick tiers, EOSB calculation, terminations, statutory parameters | hr.md |
| `ledger` (internal) | Out | `create_entry(post=True, grant, document)` for 4 registered types; `reverse_entry`; `pending_documents` | access R34 to R36 |
| `billing` (internal) | Out (report) | Secondment cost report per assignment (no PD beyond `employee_code`) | R35; OQ-15 |
| GOSI **[CC-verify: GOSI source blocked]** | none / manual | Contribution report per month; GOSI invoice amount entered for reconciliation; employee registration on the GOSI portal | OQ-9 |
| Mudad / WPS | Out (file to bank) / In (bank-processed file) / Out (manual upload to Mudad) | *Amended 2026-10-17 (WPS-G, WPS-P; downloaded 2026-10-17):* ERP salary file (Ministry header + content fields, R31) to the bank; bank-processed file back; that file uploaded manually in the Mudad compliance system (only route); reference, status and justifications entered manually (R32, R53). No API documented **[CC-verify: no Mudad API in HRSD sources]** | OQ-10 **B** for file generation (per-bank layout) |
| Banks | Out (file via Mudad or bank portal) / In (manual) | Salary file; payment confirmation recorded as `PayrollPayment` | Bank module later. *Amended 2026-10-17 (P15):* Riyad Bank, SNB, Al Rajhi Bank, Bank AlJazira, Alinma Bank, one format each **[CC-verify: per-bank format not in HRSD sources]** (R51); remuneration paid by (foreign-currency) transfer, outside WPS |
| ZATCA (WHT) | Out (data) | Remuneration WHT in the monthly WHT return data via invoicing (R43) | OQ-29, OQ-30. *Added 2026-10-17* |
| `invoicing` (internal) | In / Out | Bai' ajel asset purchase as a vendor bill (1250); VAT/e-invoice for the sale to the employee if required (OQ-35); WHT categories and return data (R43) | *Added 2026-10-17* |
| Qiwa | none | — (contract documentation is HR's, hr R61; Regs Art. 18) | hr OQ-21 |
| ZATCA | none | Payroll is not a VAT supply; secondment invoices go through billing/invoicing | — (*amended 2026-10-17:* except WHT data and possibly the bai' ajel sale, rows below) |
| Daftra / SAP B1 | In (one-off) | Opening EOSB provision per employee (`opening` rows tied to 2500), open loans, YTD | migration-agent; OQ-26 |

## 7. Permissions

Roles `payroll_officer` and `hr_officer` are **proposed** (hr OQ-2). No permission is **Ag** (R38).
*Amended 2026-10-17 (P14, P12, P11):* both roles are **decided**, granted by `managing_partner` (hr R57, access A21). Changed holders: `payroll.approve_run` → `hr_officer` (R50); `payroll.approve_loan` → `managing_partner` (R46). New: `payroll.approve_overtime` (A): `managing_partner` or proposed `department_head` per hr R55 (R45); `payroll.create_deferred_sale` / `payroll.submit_deferred_sale`: payroll_officer; `payroll.approve_deferred_sale_terms` (A) and `payroll.approve_deferred_sale` (A, P): `managing_partner`; `payroll.prepare_profit_recognition` / `payroll.approve_profit_recognition` (A, P): payroll_officer or accountant / approver (default, OQ-36). The tables below are amended accordingly.

| Codename | Flags | Holders (default) |
|---|---|---|
| `payroll.view` (aggregates, masked) | | payroll_officer, approver, accountant, viewer |
| `payroll.view_payslip` (individual pay) | | payroll_officer, approver (logged) |
| `payroll.manage_inputs` / `payroll.approve_input` | — / A | payroll_officer, hr_officer / payroll_officer (not own entry, not own pay) |
| `payroll.prepare_run` / `payroll.submit_run` | | payroll_officer |
| `payroll.approve_run` | A, P | ~~approver~~ hr_officer (P14, R50) |
| `payroll.reverse_run` | P | approver (not the preparer) |
| `payroll.create_loan` / `payroll.approve_loan` | — / A, P | payroll_officer / ~~approver~~ managing_partner (P12, R46) |
| `payroll.approve_overtime` | A | managing_partner (route `managing_partner`) / department_head, proposed (route `department_head`) (R45) |
| `payroll.create_deferred_sale` / `payroll.approve_deferred_sale_terms` / `payroll.approve_deferred_sale` | — / A / A, P | payroll_officer / managing_partner / managing_partner (R47) |
| `payroll.prepare_profit_recognition` / `payroll.approve_profit_recognition` | — / A, P | payroll_officer, accountant / approver (default, OQ-36) |
| `payroll.prepare_final_settlement` / `payroll.approve_final_settlement` | — / A, P | payroll_officer / approver |
| `payroll.record_payment` / `payroll.approve_payment` | — / A, P | payroll_officer / approver |
| `payroll.generate_wps_file` / `payroll.record_mudad_submission` | S! (file holds PD) / — | payroll_officer |
| `payroll.manage_settings` | | approver |
| `payroll.view_cost_reports` (secondment cost, GOSI, EOSB reports without PD) | | accountant, approver, viewer |

| Action | payroll_officer | hr_officer | approver (CFO) | accountant | viewer | managing_partner | Agent / system |
|---|---|---|---|---|---|---|---|
| Enter inputs | Y | Y | | | | | No |
| Prepare, calculate, submit run | Y | | | | | | No |
| Approve (= post) ~~run, loan,~~ settlement, payment (*amended 2026-10-17*: run → next row, loan → row after) | | | Y (not preparer/submitter/modifier; R39) | | | Fallback (access OQ-1) | No |
| *Amended 2026-10-17:* approve (= post) run (R50) | No | Y (not preparer; not own inputs) | No | | | Fallback | No |
| *Amended 2026-10-17:* approve loan, bai' ajel terms and sale (R46, R47) | No | | No | | | Y (not creator) | No |
| *Added 2026-10-17:* approve overtime (R45) | No | | | | | Y where route = `managing_partner`; elsewhere the department head (proposed role `department_head`) | No |
| Reverse run | | | Y | | | | No |
| Generate WPS file | Y | | | | | | No |
| See payslips | Y | | Y | | | | No |
| See cost reports | | | Y | Y | Y | | No |

## 8. Acceptance tests

Fixtures: ALM (SA, SAR, Sharia enforced), SUB (SA, SAR), EGY (EG, EGP); template chart plus proposed accounts 1230, 2330, 2340, 5120 (test fixture only); 2027 periods open unless stated; hr employees S1 (Saudi, as E1) and N1 (non-Saudi, as E1) in ALM, S2 in SUB; unverified seed parameters with `HR_REQUIRE_VERIFIED_PARAMETERS=False` unless stated. Users: P (payroll_officer ALM), P2 (payroll_officer ALM), C (approver ALM), C2 (approver SUB), A (accountant ALM), V (viewer), MP, agent. Anonymised data only.
*Amended 2026-10-17 (P12, P14):* fixtures add H and H2 (hr_officer ALM), DH (department head of SUB's OPS department, proposed `department_head` role in SUB), remuneration employee R1 in ALM (USD, residence EG), proposed accounts 1240, 1241, 1250, 4610 and 2220. Where AT-5, AT-7, AT-13 and AT-24 say "C approves" a **run**, read "H approves" (C is refused, AT-33); in AT-24 the employee-approver is H instead of C. In AT-17, "C approves" the loan reads "MP approves". Payments, final settlements and reversals stay with C.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | Runs in ALM and SUB | P lists runs; opens SUB run; a build_lines variant targets SUB's 2330; ALM run tries to include S2 | ALM only; SUB → 404; ledger refuses other-entity account; S2 refused (R1, R2, R30) |
| AT-2 | NEW entity set up as data with payroll enabled; EGY enabled | Run March for NEW; for EGY | NEW runs with no code change; EGY refused "jurisdiction not configured" (en/ar) (R3, R4) |
| AT-3 | Input amount as float; S1 contract in USD (fixture forced) | Save input; calculate | Float refused; calculation refused naming the currency (R5) |
| AT-4 | March regular run approved | Create a second regular March run; off-cycle without `reason_ar`; set `status="approved"` directly on a draft | Refused; refused; refused (model, and DB trigger on PostgreSQL) (R6, R7) |
| AT-5 | Run `submitted` | Approve after an hr leave approval changed S1's unpaid days | Refused "recalculate" (en/ar), grant unused; after recalculation and resubmission C approves (R8) |
| AT-6 | N1 joins 2027-03-11; S1 has 2 unpaid days | Calculate March | N1 paid 20/30 of each component (`thirty` basis); S1 deduction 2 × 13,500.00 / 30 = 900.00; expired-iqama warning shown, run not blocked (R10, R14, R16) |
| AT-7 | Run as E1, prepared and submitted by P | P approves; C approves; repeat with 2027-03 closed; `close_period(2027-03)` while submitted | P refused (A); C: entry as E1, dated 2027-03-31, `document_type`/`id` = run, number `ALM-PAY-2027-000001`, descriptions without names; closed: approval refused, run stays `submitted`; close refused (`documents_pending`) (R11, R12, R36) |
| AT-8 | S1 v2 (BASIC +1,000.00 effective 2027-03-15) approved 2027-04-10 after March approved | Calculate April | April full new BASIC + retro line for 2027-03-15..31 = 1,000.00 × 17/30 = 566.67 (`thirty`), GOSI and EOSB recomputed on the new wage (R9, R14) |
| AT-9 | March run approved, no payment | C reverses; then approves a payment and tries reversing another run with an approved payment; P reverses | Reversal posts mirror (E6), run `reversed`, installments `planned`, EOSB rows mirrored; reversal after payment refused; P refused (R13) |
| AT-10 | S1 with 10 approved overtime hours, `daily_hours` 8 | Calculate | Pay = 10 × (13,500.00/30/8 + 0.5 × 10,000.00/30/8) = 10 × (56.25 + 20.833…) = 770.83 (formula: Labour Law Art. 107; downloaded 2026-10-17; hour divisor 30 × 8 is the unverified `overtime.hour_divisor_rule`) (R15) |
| AT-11 | N1 with 5 sick days at 75% | Calculate | Deduction 5 × 8,000.00/30 × 25% = 333.33 (R17) |
| AT-12 | N1 wage 8,000.00, installment 1,000.00; one-off deduction making net negative | Calculate | Installment capped at 800.00 (10% of the actual wage, Labour Law Art. 92(1); downloaded 2026-10-17), 200.00 re-planned, warning; negative-net case refused (R18) |
| AT-13 | Component account set to 5610 in ALM (Sharia) | Calculate; force through to approval | Refused at calculation; ledger refuses at posting (R19, R37) |
| AT-14 | S1 GOSI wage 12,500.00; Saudi S3 with BASIC 50,000.00, HOUSING 12,500.00; N1 | Calculate | S1 employee 1,125.00 + 93.75, employer 1,125.00 + 93.75 + 250.00; S3 capped at 45,000.00; N1 employer OH 150.00 only, no employee share. **All rates [CC-verify: GOSI source blocked]** (R20 to R22) |
| AT-15 | GOSI invoice entered 2,900.00 for March | Reconcile | Difference 62.50 reported (en/ar), nothing posted (R23) |
| AT-16 | S1 provision balance 0 at start; manual journal of 100.00 to 2500 posted by C | March run; April run | March accrual = required provision − 0, Dr 5120 / Cr 2500, one `accrual` row per employee; April run warns "2500 differs from EOSB sub-ledger by 100.00" (R24, R25) |
| AT-17 | Loan drafted by P: principal 3,000.00, 3 installments of 1,100.00; another 3 × 1,000.00 with a 50.00 "admin fee" input; valid 3 × 1,000.00 | Submit; C approves valid; skip March without a new schedule | Sum ≠ principal refused; fee refused (Sharia message en/ar, also in a non-Sharia entity); valid posts E4; skip refused (R26, R27) |
| AT-18 | E1 approved | P records salary payment 19,781.25 and C approves; P records 20,000.00; P approves own | E3 posted, run `paid`; over-payment refused; self-approval refused (R29) |
| AT-19 | Approved run; `wps_format` null; then set; N1 without IBAN | Generate; generate again after set; download | First refused; file with S1 only, N1 in exceptions, sha256 stored, generation and download logged; run and ledger unchanged; Mudad reference recorded manually (R31, R32) |
| AT-20 | N1 resignation approved (hr AT-20), 4 years, balance 10 days, loan 1,000.00, provision 16,000.00 | P prepares; C approves; check alert | Lines and entry as E5 (`utilisation` 5,333.33, `release` 10,666.67); `due_date` = last day + 14 (worker-initiated; Labour Law Art. 88; downloaded 2026-10-17); alert 3 days before; loan `settled_in_final` (R28, R34) |
| AT-21 | Approved run; Arabic-preference user | Render S1 payslip; V opens it; P opens it | Both languages, stored values (identical after a parameter change); V refused; P allowed, view logged; all messages en/ar (R33, R40) |
| AT-22 | S1 seconded to SUB for 2027-03-01..10 | Approve March run; open secondment cost report | No 1210/2110 line in the run entry; report shows `employee_code`, 10 days, prorated gross, employer GOSI, EOSB accrual; no name (R35) |
| AT-23 | Agent user; billing system user | Agent drafts an input; system user calculates a run | Both refused (R38) |
| AT-24 | C is employee C-E in ALM; March run includes C-E with an overtime input | C approves; MP approves | C refused (own pay inputs); MP allowed. Without inputs on C-E, C allowed and logged (R39) |
| AT-25 | *Added 2026-10-17 (P8).* ALM: S1 (`ksa_employee`), R1 (`remuneration`); EGY: local employee L1, no verified EG parameters | Calculate March `in_country` and `out_of_country` runs in ALM; run March in EGY; give R1 an overtime input and an unpaid-days input | `in_country` has S1 only; `out_of_country` has R1 only, with no GOSI, EOSB accrual or 2340/2500 lines; EGY refused "jurisdiction not configured" (en/ar), no SA parameter read; R1 overtime and unpaid inputs refused (R41) |
| AT-26 | *Added 2026-10-17 (P8, P9).* R1 USD 5,000.00; rate 3.7500 on 2027-03-31, 3.7600 on 2027-04-08; WHT category rate 5% (test fixture) | H approves the out-of-country run; P records and C approves the payment on 2027-04-08; amount USD 5,000.005 entered; run S1 in USD | Entries as E8a and E8b (2220 940.00, 5750 50.00); payslip shows gross USD 5,000.00, WHT 250.00, net 4,750.00 (en/ar); WHT row appears in the WHT return data stub; 3-dp USD refused; S1 USD refused (R42, R43) |
| AT-27 | *Added 2026-10-17 (P10).* March 2027 | Approve an in-country input on 03-27 and one on 03-28; approve R1 one-off on 04-06 and one on 04-07; create a second `out_of_country` regular run for March | 03-27 in March run, 03-28 in April; 04-06 in March out-of-country run, 04-07 in April; second run refused (unique); both March runs dated 2027-03-31 (R44) |
| AT-28 | *Added 2026-10-17 (P11).* ALM route `managing_partner`; SUB route `department_head`; S1 grade G-STAFF (eligible), M1 grade G-MGMT (not eligible); SUB employee S2 in OPS (head DH) | Overtime input for M1; for S1 without `overtime_reason_ar`; S1 valid, approved by DH, by C, by MP; S2 valid, approved by another department's head, by DH; DH's own overtime approved by DH, by MP; calculate with an unapproved input | M1 refused; no reason refused; S1: DH and C refused, MP accepted; S2: other head refused, DH accepted; DH own: DH refused, MP accepted; unapproved input left out with warning (R45) |
| AT-29 | *Added 2026-10-17 (P12).* S1 BASIC 10,000.00 | P drafts loan 30,000.00; C approves; MP approves; P drafts 30,000.01; with 30,000.00 outstanding P drafts an advance of 1,000.00, MP approves | C refused (no permission); MP approves, cap 30,000.00 stored; 30,000.01 refused (message names cap and bai' ajel); advance refused (total > cap) (R46) |
| AT-30 | *Added 2026-10-17 (P12).* S1 bai' ajel request: cost quote 30,000.00, 24 months, profit 3,600.00 | MP approves terms; P submits the sale before the purchase bill is posted; after posting E9 but before possession; with `sale_date` < possession; valid; P approves sale; MP approves; schedule summing to 33,599.99; monthly recognition approved by C | Sale refused in the first three cases; P refused (A); MP approval posts E10 with number `ALM-BAJ-2027-000001`; bad schedule refused; recognition posts E12 (150.00); run deducts 1,400.00 Cr 1240 (E11) (R47) |
| AT-31 | *Added 2026-10-17 (P12).* E10 posted; SUB has `sharia_enforced=False` with the same contract | Skip an installment; MP approves a 30-month reschedule; enter a "late fee" one-off deduction linked to the contract; edit `profit_amount`; repeat in SUB | Skip adds nothing; reschedule keeps sale price 33,600.00 (installments re-spread); late fee refused (Sharia message en/ar); edit refused; same refusals in SUB (R48) |
| AT-32 | *Added 2026-10-17 (P12).* S1 leaves after 10 installments (outstanding 19,600.00, unrecognised profit 2,100.00); other dues 5,000.00, EOSB 40,000.00 | Final settlement with `PAYROLL_EOSB_DEDUCTION_ALLOWED` False; then True (test) | False: recovery 5,000.00 from other dues, 14,600.00 stays on 1240 (`outstanding_after_exit`), warning en/ar; True: full 19,600.00 recovered and 2,100.00 recognised as E13 (R49). *Amended 2026-10-17 (P17):* the True case needs `final_dues_undertaking` True on the contract; True is now the default setting |
| AT-40 | *Added 2026-10-17 (P17).* N1 leaves; outstanding loan 9,000.00; other dues 2,000.00, EOSB 16,000.00; default settings | Prepare and approve the final settlement; then the same with `PAYROLL_EOSB_DEDUCTION_ALLOWED=False` (test); then a case with loan 20,000.00 against total dues 18,000.00 | Default: `loan_recovery` 9,000.00 in full, no 10% / 50% cap warning, loan `settled_in_final`, 1230 credited 9,000.00; False: recovery limited to 2,000.00 with en/ar warning; 20,000.00 case: 18,000.00 recovered, net 0, remainder 2,000.00 needs write-off (refused until OQ-7), en/ar message (R18, R28) |
| AT-41 | *Added 2026-10-17 (P17).* Two sold bai' ajel contracts (test, OQ-33 pending): K1 with `final_dues_undertaking` True, K2 False; each outstanding 19,600.00; other dues 5,000.00, EOSB 40,000.00; default settings | Final settlement for each; set K2's flag True after sale approval; set the flag True on a draft with empty `consent_ref` | K1: 19,600.00 recovered, no cap; K2: 5,000.00 recovered, 14,600.00 `outstanding_after_exit`, warning en/ar; both edits refused (R49, 3.11) |
| AT-33 | *Added 2026-10-17 (P14).* March run prepared and submitted by P; H2 entered an input in it | P approves; C approves; H2 approves; H approves | P refused (A, preparer); C refused (no `payroll.approve_run`); H2 refused (own input, default); H approves, entry posted (R50) |
| AT-34 | *Added 2026-10-17 (P15).* ALM | Set `salary_bank` to an unlisted bank; set `alrajhi` with `wps.format.alrajhi` unverified and `HR_REQUIRE_VERIFIED_PARAMETERS=True`; verified; generate WPS for an approved run containing S1 and R1 | Unlisted refused; unverified refused naming the parameter; verified: file contains S1 only, R1 not listed as exception but excluded by basis (R51) |
| AT-35 | *Added 2026-10-17 (R52).* S1 (G-STAFF) with 715 approved overtime hours in the current year | Input of 10 hours without `worker_consent_ref`; same with it; an Art. 106 exceptional-case input of 11 hours on one date; 6 × 10 hours on six days of one week plus 1 more hour | First refused naming the 720-hour cap (Regs Art. 22, en/ar); second accepted; 11-hour day refused; the 61st hour of the week refused (Art. 106) |
| AT-36 | *Added 2026-10-17 (R10).* ALM settings | Set `day_basis=calendar` without `day_basis_ref`; with a work-regulations reference | Refused (Art. 2, en/ar); accepted, proration then uses calendar days |
| AT-37 | *Added 2026-10-17 (R18, R54).* N1 actual wage 8,000.00 (due wage 8,000.00 for March), loan installment 800.00 | One-off private deduction 500.00 without `consent_ref`; with it; `damage_art91` 1,500.00; `court_order` 2,500.00; one-off deductions bringing the total to 4,500.00; a disciplinary fine input | Refused; accepted; refused (> 5 days' wage 1,333.33, Art. 91); refused (> 1/4 = 2,000.00, Art. 92(6)); total above 4,000.00 (50%, Art. 93) refused, warning en/ar; fine refused (not supported, R54) |
| AT-38 | *Added 2026-10-17 (R17, R34; hr R64).* N1 sick days in the 75% tier including Eid al-Adha; terminations of S1 variants (open-ended) by `employee_notice_art75`, `art81_employee_leaves`, `employer_termination`; an `one_off_earning` labelled leave encashment for an active employee | Calculate; prepare final settlements | Eid day paid 100%, other days 75%; `due_date` + 14, + 14, + 7 (Art. 88); in-service encashment refused (Art. 109(2), en/ar) |
| AT-39 | *Added 2026-10-17 (R31, R32, R53).* `wps.fields.mol` verified, `wps.format.alrajhi` verified (test); S1; S4 with no `HOUSING` component (housing provided in kind); approved March run | Generate WPS; record a Mudad submission without a bank-processed file; attach the bank-processed file and record it; run the alert job on 04-07, 04-10, 04-15 with no submission (variant) | File has one header group (DEST-ID … MOL-ESTBID) and one content group per employee; MOL-BAS = BASIC only; MOL-HALI = 2,500.00 for S1 and 0 for S4; no deductions field mapped; submission without processed file refused; with it accepted and both sha256 stored; alerts en/ar on each of the three days, once each |

## 9. Open questions

**B** = blocks building that part; **GL** = blocks go-live; **D** = default exists.

| # | Question | Owner | Blocks? | Default |
|---|---|---|---|---|
| OQ-1 | Pay period and pay day per entity. | CFO | D | Calendar month; pay day 27. *Partly resolved by P10 (2026-10-17):* monthly for all. Pay day (especially out-of-country): OQ-31 |
| OQ-2 | Is payroll run in-house per entity, or does Almiras run and pay payroll for subsidiaries (shared service, intercompany)? Which entities have employees (hr OQ-1)? | Managing Partner | D | Per entity, own bank account; `enabled` False until set. *Partly resolved by P8:* all group companies have employees; local companies pay their own staff; remuneration staff are paid by the Saudi entity (not intercompany). Shared-service question stays open |
| OQ-3 | Payroll for non-Saudi entities (Egypt, China, UAE subsidiaries): local law, social insurance, income tax withholding, local payroll provider? | Managing Partner | — | **Resolved by P8 (2026-10-17)** at decision level (R41). Country parameters: OQ-28 |
| OQ-4 | Staff of Saudi entities paid in foreign currency (e.g. abroad): allowed? rate, account, WPS treatment? | Managing Partner + CFO | — | **Resolved by P9 (2026-10-17):** yes for remuneration staff only (R42); rate per ledger-fx; outside WPS (R51) |
| OQ-5 | Overtime policy: pre-approval, caps, rates above statutory. | CFO | D | Approved hours only; statutory formula; ~~no cap~~. **Resolved by P11 (2026-10-17)** for eligibility and approval (R45). *Amended 2026-10-17:* statutory caps exist: 720 hours a year (more only with worker consent, Regs Art. 22) and 10 a day / 60 a week in Art. 106 cases (R52); rate verified (Art. 107). Open: rates above statutory; paid compensatory leave instead of pay (Regs Art. 22 bis; default not built); hour divisor **[CC-verify: not stated in HRSD sources]** |
| OQ-6 | Daily-rate basis for proration, unpaid leave and overtime (/30 vs calendar days). **Resolved 2026-10-17:** month = 30 days unless the contract or work regulations say otherwise (Labour Law Art. 2); `calendar` needs `day_basis_ref`. | CFO | — | /30 |
| OQ-7 | Loan policy: who is eligible, max amount and term, approval thresholds, write-off on exit and its account, deduction cap (*2026-10-17:* 10% / 50% verified, Art. 92, 93; bases OQ-34). | CFO + Managing Partner | D | Any active employee; no max; CFO approves; write-off refused until decided. *Partly resolved by P12 (2026-10-17):* MP approves all loans; profit-free max 3 basic salaries (R46). Rest: OQ-32 |
| OQ-8 | Disciplinary fines and their treatment. *Amended 2026-10-17:* verified: fines go to a register and a workers' fund liability, used only for the workers' benefit, never income (Art. 73; Regs Art. 70); caps 5 days' wage per violation and per month (Art. 66, 70) (R54). Open: whether to support fines at all. | Managing Partner + legal | D | Not supported |
| OQ-9 | GOSI details **[CC-verify: GOSI source blocked]**: annuity rates by cohort under the new Social Insurance Law, GCC nationals, cap/floor, wage composition and reference date, housing deemed value, late-payment fines, reconciliation import format. *Added 2026-10-17 (P16):* also confirm whether `remuneration` staff (working abroad, paid by a Saudi entity) have any GOSI coverage; P16 does not decide it. | compliance-checker | **GL** (real runs); build on parameters | Seed per R20, R21; manual reconciliation; no GOSI for `remuneration` (R41) |
| OQ-10 | WPS / Mudad **[CC-verify]**: file format(s) per bank, fields, encoding, Mudad upload vs API, which banks each entity uses, file retention. | Project owner (supplies the five banks' formats, P18) + compliance-checker + CFO | **B** for WPS file generation only (P18: the rest of payroll goes ahead) | No file until a format is configured; manual upload; 30 days retention. *Narrowed by P15 (2026-10-17):* banks are Riyad Bank, SNB, Al Rajhi Bank, Bank AlJazira, Alinma Bank; open = each bank's file format **[CC-verify]**, encoding, Mudad upload vs API, retention. *Narrowed 2026-10-17 (WPS-G, WPS-P):* Ministry header/content fields verified (R31); Mudad upload is manual and only via the compliance system, with the bank-processed file (R32). Still open: per-bank byte layout and encoding, whether a deductions field exists **[CC-verify: confirm with each of the five banks]**, Mudad API **[CC-verify: none documented]**, retention. *Amended 2026-10-17 (P18):* the project owner will share the formats later; stays open |
| OQ-11 | EOSB provision measurement (IFRS for SMEs s28 **[CC-verify]**): vested amount on termination basis, resignation basis, or actuarial. | Accountant | D | Termination basis (fraction 1) |
| OQ-12 | Approve proposed accounts 1230, 2330, 2340, 5120 (and 2350/5130 if leave is accrued); or use 2300 / 5100 instead. *Amended 2026-10-17 (P12):* also 1240, 1241, 1250, 4610 (bai' ajel) and use of 2220, 5750 for remuneration. | CFO / accountant | **GL** | Build with the proposed codes in fixtures; template unchanged until approved |
| OQ-13 | Payment recording: may the payroll officer post payments alone (like receipts, access A16)? Link to future bank reconciliation. *Note 2026-10-17:* the receipts exception is being narrowed: a receipt's recorder may post it only when it has no bank charge, otherwise a second person posts (invoicing R46). No change for payroll. | Managing Partner | D | No: approver approves payments |
| OQ-14 | Approver who is an employee in the run (own pay). | Managing Partner | D | R39. *Amended 2026-10-17 (P14):* the run approver is now the `hr_officer`; R39 applies to them (R50) |
| OQ-15 | Secondment recharge basis: billing contract price (fixed) or actual payroll cost (cost-plus, monthly variable); does payroll feed billing automatically? | Managing Partner + CFO | D | Billing contract price; payroll gives a report only |
| OQ-16 | Payslip delivery (print, e-mail, self-service) and PDF tool (open source); one bilingual slip or per language. | HR | D | In-app HTML/print, both languages on one slip |
| OQ-17 | Retro pay and corrections: next-run lines vs off-cycle run; limits. | CFO | D | Next regular run |
| OQ-18 | Cost centres / departments / projects on payroll lines. | Accountant | D | None |
| OQ-19 | Bonuses, commissions, annual air-ticket allowance, medical insurance, other benefits and their accruals. | CFO | D | One-off inputs; no accruals |
| OQ-20 | Input cut-off day and late inputs. | CFO | — | **Resolved by P10 (2026-10-17):** 27th of the month in-country, 6th of the following month out-of-country; later inputs next run (R44) |
| OQ-21 | May agents draft payroll inputs (e.g. overtime import)? | Managing Partner | D | No |
| OQ-22 | Reversal of approved runs: allowed at all, by whom, after payment? | CFO | D | R13 |
| OQ-23 | Final settlement deadlines and alerts. **Resolved 2026-10-17:** 7 days; 14 days when the worker ended the contract (Labour Law Art. 88; unified contract cl. 11.9) (R34). Alert timing stays a default. | compliance-checker | — | 7 / 14 days; alert 3 days before |
| OQ-24 | GOSI on leave encashment and notice pay **[CC-verify: GOSI source blocked]**; leave-encashment wage basis (*resolved 2026-10-17:* actual wage, Art. 2, 111). | compliance-checker | D | No GOSI on encashment; basis = `counts_for_eosb` components |
| OQ-25 | Rounding level: per line per employee vs per employee total. | Accountant | D | Per line per employee |
| OQ-26 | Migration: opening EOSB per employee, leave balances, open loans, YTD figures from Daftra / SAP B1. | Accountant + migration-agent | D | `opening` rows tied to the 2500 opening balance; loans as `active` with remaining schedule |
| OQ-27 | Run number format. | Accountant | D | `{entity.code}-PAY-{year}-{000001}` |
| OQ-28 | *Added 2026-10-17 (P8).* Local payroll parameters for Egypt, China, UAE entities: social insurance, income tax withholding, end-of-service/gratuity, leave pay, payslip and bank-file rules, local payroll provider (same as hr OQ-26). | Managing Partner + local advisers + compliance-checker | **B** for `local_entity_employee` payroll in those entities; not for KSA | Refused (R4, R41) |
| OQ-29 | *Added 2026-10-17 (P8).* WHT on remuneration **[Needs expert sign-off: tax] [CC-verify]**: is WHT due on remuneration of non-resident individuals for work abroad; category and rate; treaty relief; at accrual or payment; SAR rate date; return line; separate expense account for remuneration vs 5100. | Tax adviser + compliance-checker | **GL** for remuneration payroll (build on `WhtCategory` data) | WHT at payment, employee bears it, 5100 |
| OQ-30 | *Added 2026-10-17 (P8).* invoicing amendment: `wht_return_data` and the 2220 tie-out (invoicing R64) must include payroll remuneration WHT. | Invoicing spec owner | **B** for R43 wiring (build against a stub) | Payroll writes WHT rows in invoicing's `WhtDeduction` shape via an invoicing service |
| OQ-31 | *Added 2026-10-17 (P10).* Confirm "local (in-country)" = `ksa_employee` + `local_entity_employee` and "out-of-country" = `remuneration`; out-of-country pay day; may the period be closed before the out-of-country run (cut-off on the 6th) is approved? | Managing Partner + CFO | D | Mapping as R44; pay day = default_pay_day of the following month for out-of-country; `close_period` refuses while any run of the period is not approved |
| OQ-32 | *Added 2026-10-17 (P12).* Loan details: cap per loan or on total outstanding; advances included; BASIC at which date; eligibility (probation, `remuneration`, local employees); maximum term; write-off on exit and its account; eligibility for bai' ajel (only above the cap?). | Managing Partner | D | Total outstanding incl. advances ≤ 3 × BASIC at approval; loans and bai' ajel for `ksa_employee` only; no max term; write-off refused |
| OQ-33 | *Added 2026-10-17 (P12).* Bai' ajel structure **[Needs expert sign-off: Sharia]**: ownership and possession evidence; may the employee act as the company's agent to buy (wakala); profit method (straight-line vs proportional to outstanding); early-settlement rebate (ibra'); default without any penalty; asset risk and insurance (takaful) until and after sale; recognition when an exit leaves a balance. | Sharia adviser + Managing Partner | **B** for building bai' ajel (R47 to R49) | As R47 to R49; no agency; straight-line; no automatic rebate; remaining profit recognised on recovery |
| OQ-34 | *Added 2026-10-17 (P12).* Salary and EOSB as security **[Needs expert sign-off: legal]**: may an outstanding financing balance be deducted from EOSB and final dues; deduction caps per month and at exit (Labour Law deduction article); signed undertaking required. *Amended 2026-10-17 (Labour Law check):* the articles are Art. 88 (deduct "any debt due because of the work" from amounts owed), Art. 92 (written consent; (1) employer loans ≤ 10% of wage; (4) "any other benefit"), Art. 93 (total ≤ 50% "in all circumstances"). Questions for legal: (a) does Art. 88 cover a bai' ajel receivable and an employer loan; (b) do the 10% and 50% caps apply at final settlement; (c) are bai' ajel installments Art. 92(4) or do they need written consent; (d) is a signed undertaking enforceable; (e) cap bases: actual wage vs due wage (R18). Same as hr OQ-32. **Resolved by P17 (2026-10-17)** for employee loans: (a) and (b) answered: loans are deducted from final dues including EOSB, and the 10% / 50% caps apply to monthly pay only, not the final settlement (R18, R28); `PAYROLL_EOSB_DEDUCTION_ALLOWED` defaults to True. Bai' ajel: same treatment only with the signed undertaking in the contract (`final_dues_undertaking`, R49), answering (d) as a condition. Still open: (c) monthly bai' ajel installments under Art. 92(4) or the 10% cap; (e) cap bases; recovery of a bai' ajel balance without the undertaking. | Legal + Managing Partner | D (was **B**) | Loans: full netting at final settlement (R28); installments count in the 50% cap with signed consent (`consent_ref`); 10% cap a parameter; bai' ajel without undertaking: non-EOSB dues only (R49) |
| OQ-37 | *Added 2026-10-17 (Labour Law check).* **[Needs expert sign-off: legal]** Other legal points affecting payroll, held in hr: Art. 85 for Art. 75 terminations (hr OQ-33; EOSB award and provision basis), mutual-agreement fraction (hr OQ-34), resignation withdrawal after 7 days (hr OQ-35; pay during the request period), Hijri/Gregorian EOSB divisor (hr OQ-36; monthly provision R24), unpaid-leave aggregation (hr OQ-37; unpaid deductions and service), GCC nationals and Art. 37 (hr OQ-39; with GOSI OQ-9). | Legal | As in hr | As in hr |
| OQ-38 | *Added 2026-10-17 (Labour Law check).* **[Needs expert sign-off: legal]** Are `remuneration` persons workers under the Labour Law (Art. 2 "even if away from their direct oversight"; Art. 7(1)(e) exempts only non-Saudis on a specific task up to 2 months; Art. 90 SAR pay through accredited banks)? If yes, **P8/P9 (hr H9/H10) would be undone** for them: SAR pay, EOSB, leave and WPS apply, and R41 to R43 and R51 change. Must go to the Managing Partner and legal. Same as hr OQ-38. **Resolved by P16 (2026-10-17):** the Saudi Labour Law does not apply to people working outside Saudi Arabia; remuneration staff are not workers under it (no EOSB, Saudi leave, SAR pay, WPS); P8/P9 stand. GOSI coverage: OQ-9. | Managing Partner + legal | — | As P8/P9 (R41 to R43, R51) |
| OQ-35 | *Added 2026-10-17 (P12).* VAT and ZATCA on the bai' ajel sale to an employee (taxable supply? e-invoice from invoicing?), input VAT on the purchase, zakat treatment of 1240/1241. **[Needs expert sign-off: tax]** | Tax adviser | **B** for posting bai' ajel sales | No sale posted until decided |
| OQ-36 | *Added 2026-10-17 (P14).* With the HR officer approving runs: who approves final settlements, payments, run reversals and deferred-profit recognition; may an `hr_officer` approve a run containing inputs they entered or approved? | Managing Partner | D | Settlements, payments, reversals, recognition: `approver` (CFO); `hr_officer` refused for runs with own inputs |

**Blocking:** ~~OQ-3~~ (resolved by P8; replaced by OQ-28 for non-SA local payroll), ~~OQ-4~~ (resolved by P9), OQ-10 (WPS file formats of the five banks; owner to supply, P18; blocks WPS file generation only), OQ-30 (WHT return wiring), OQ-33 / OQ-35 (bai' ajel: Sharia, tax sign-off), ~~OQ-34~~ (resolved for loans by P17; rest default), hr OQ-16 (billing interface), *added 2026-10-17:* ~~OQ-38~~ (resolved by P16). **Go-live:** OQ-9 (GOSI parameters verified **[CC-verify: GOSI source blocked]**), OQ-12 (accounts approved), OQ-29 (remuneration WHT), hr OQ-11 (PDPL), hr OQ-18 / OQ-36 (EOSB divisor), hr OQ-33 (Art. 75 awards). **Before building:** ~~the compliance-checker verifies every [CC-verify] item~~ Labour Law / WPS items verified 2026-10-17; remaining tags: GOSI (all), per-bank WPS layout and deductions field, overtime hour divisor, Mudad API, WHT, and the legal sign-offs (OQ-34 remainder, OQ-37; OQ-38 resolved by P16). (Roles approved by P14, 2026-10-17.)
