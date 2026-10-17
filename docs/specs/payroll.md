# Payroll: monthly runs, GOSI, EOSB provision, loans, WPS and final settlement

Django app: `payroll`. Status: **draft spec**. Written 2026-10-17 by the spec-writer agent.
Depends on: `docs/specs/hr.md` (employees, contracts, components, leave, EOSB calculation, `StatutoryParameter`), `core.access` (`docs/specs/access.md`: `require`, segregation, document registry R34 to R36), `ledger.services` (`create_entry(post=True, grant, document)`, `reverse_entry`, `close_period` + `pending_documents`), `docs/specs/ledger-fx.md` (functional currency), `docs/specs/billing.md` (secondment recharge).
**[CC-verify]** = Saudi regulatory figure or format (GOSI, Labour Law, WPS, Mudad) written from memory, not checked against gosi.gov.sa, hrsd.gov.sa or mudad.com.sa (not reachable from this environment). Every such figure is an `hr.StatutoryParameter` row, seeded `unverified` (hr R6, R7). The compliance-checker verifies each before go-live.

## 1. Purpose

The payroll module runs monthly payroll **per entity** for every employing entity in the group (Almiras, subsidiaries and any entity added later as data): earnings from approved HR contracts, overtime, unpaid and sick-leave deductions, retro differences, deductions for interest-free employee loans and advances, GOSI contributions (employer and employee shares, Saudi and non-Saudi), the monthly end-of-service (EOSB) provision accrual on account 2500, final settlements, bilingual payslips, and WPS/Mudad salary files. Payroll officers prepare and submit; the entity's CFO approves; approval posts automatically through the ledger document registry, each document building its own lines. Payroll posts only to the employing entity's accounts, never intercompany: seconded staff cost is recharged by billing. Loans carry no interest in any entity (Sharia). Personal data stays masked and out of journal entries. Only the Saudi jurisdiction is built; payroll in Egypt, China or UAE entities and pay in foreign currencies are open questions.

## 2. Decisions

No payroll-specific business decision has been recorded yet. Applicable decisions (detail in hr.md §2):

| # | Decision | Source | Date |
|---|---|---|---|
| P1 | CLAUDE.md rules: posting only via `ledger.services`, posted entries immutable (reversals only), balanced 2+ one-sided lines, closed periods reject postings, no posting to another entity's accounts, Sharia per entity, intercompany lines carry a counterparty, `Decimal` money (SAR 2 dp), `entity` FK everywhere, en/ar text, new entities are data. | CLAUDE.md | in repo |
| P2 | Staff create, the CFO approves, posting is automatic on approval; roles per entity. | Project owner (access A1) | 2026-10-16 |
| P3 | Agents create drafts only; never approve, post or issue. | CLAUDE.md; access A6, A7 | 2026-10-16 |
| P4 | Seconded staff billed to the receiving entity until HR ends the secondment (billing contracts, not payroll). | Project owner (billing D1(c)) | 2026-10-16 |
| P5 | Sub-ledger documents post through the registry: registered type, `build_lines`, grant for that document, creator/submitter/modifier cannot post (except receipts, which do not apply here). | Build decisions approved at merge (access A14, A15, A16) | 2026-10-16/17 |
| P6 | Proposed account codes stay proposed until the accountant/CFO reviews them. | Project owner (ledger-fx D10; invoicing D29) | 2026-10-17 |
| P7 | Hosting in KSA. | Project owner (invoicing D12) | 2026-10-16 |

Everything else (pay day, period, cut-off, overtime policy, loan policy, which entities run payroll, foreign payroll) is in §9 with a safe default.

## 3. Data model

Conventions as hr.md §3: `entity` FK (employing entity, `PROTECT`), `created_by` (non-null), `created_at`, `modified_by`, `modified_at`, `source` (`human` / `import` / `system`); money `DecimalField(18, 2)` in `entity.base_currency`; Bi = `_en`/`_ar`; documents registered in the ledger registry are marked **[Reg]**.

### 3.1 `PayrollSettings` (one per entity)
| Field | Type | Req | Notes |
|---|---|---|---|
| entity | OneToOne Entity | Y | |
| enabled | Bool | Y | Default False (OQ-2) |
| pay_period | Choice `calendar_month` | Y | Only value built (OQ-1) |
| default_pay_day | PositiveSmallInt (1–31) | Y | Default 27 (OQ-1); a run's `pay_date` may differ |
| input_cutoff_day | PositiveSmallInt | Y | Default 20 (OQ-20) |
| day_basis | Choice `thirty`, `calendar` | Y | Default `thirty` (OQ-6 **[CC-verify]**) |
| salary_bank_account | FK ledger.Account (same entity, asset, postable) | Y | Default 1100 |
| wps_format | Char(30), null | N | Format code (OQ-10); WPS generation refused while null |
| wps_bank_code / employer_bank_iban | Char | N | PD-like, encrypted |

### 3.2 `PayrollInput` (unique per (entity, employee, period, kind, reference))
entity, employee, period (`YYYY-MM`), kind (`overtime_hours`, `unpaid_days`, `one_off_earning`, `one_off_deduction`, `notice_pay`, `bonus`), quantity (Decimal(8,2)) or amount (Money), component (FK hr.SalaryComponent, for earnings/deductions), reason (Bi), status (`draft`, `approved`, `rejected`), approved_by (`payroll.approve_input`, not own). Leave-driven inputs (unpaid days, sick pay %) are read from hr, not entered (R16, R17).

### 3.3 `PayrollRun` **[Reg]** (unique `(entity, period, kind)` for `kind=regular`)
| Field | Type | Req | Notes |
|---|---|---|---|
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
`Payslip` (one per employee per run): entity, run, employee, contract_version, paid_days, gross, total_deductions, net, employer_gosi, eosb_accrual, gosi_wage, nationality_class (`saudi`, `non_saudi`, `gcc`), cohort (GOSI cohort code **[CC-verify]**), leave_balance_snapshot, document_flag.
`PayslipLine`: entity, payslip, line_type (`earning`, `deduction`, `employee_gosi`, `employer_gosi`, `eosb_accrual`, `loan_installment`, `retro`), component (null), gosi_branch (`annuities`, `saned`, `occupational_hazards`, null), quantity, rate, amount (Money ≥ 0), account (FK ledger.Account, same entity), description (Bi), source_ref.

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

### 3.7 `PayrollPayment` **[Reg]**
entity, kind (`net_salaries`, `gosi`, `final_settlement`), run (null) / final_settlement (null), payment_date, bank_account (FK Account), amount, bank_reference, status (`draft`, `submitted`, `approved`, `reversed`), submitted_by, approved_by.

### 3.8 `FinalSettlement` **[Reg]**
entity, employee, termination (FK hr.Termination), eosb_calculation (FK, latest non-superseded), lines (`FinalSettlementLine`: line_type `salary_to_last_day`, `leave_encashment`, `eosb`, `notice_pay`, `other_earning`, `loan_recovery`, `employee_gosi`, `employer_gosi`, `other_deduction`; quantity, rate, amount, account, description Bi), gross, deductions, net, due_date (R34), status (`draft`, `submitted`, `approved`, `paid`, `reversed`), submitted_by, approved_by.

### 3.9 `WpsFile` and `MudadSubmission`
`WpsFile`: entity, run, format_code, format_parameter (FK hr.StatutoryParameter), generated_by, generated_at, sha256, employee_count, total_amount, exceptions (JSON: employee_code + reason), file (encrypted storage, purged after `WPS_FILE_RETENTION_DAYS`, default 30, OQ-10), downloads (via `AccessLog`).
`MudadSubmission`: entity, run, wps_file, submitted_on, reference, status (`submitted`, `accepted`, `rejected`), message (Bi), recorded_by. Manual entry (R32).

### 3.10 `GosiReconciliation`
entity, period, gosi_invoice_amount (entered), erp_amount (Σ employer + employee GOSI), difference, note (Bi), recorded_by. Report only (OQ-9).

## 4. Rules

Enforcement: **svc** = `payroll/services.py`; **M** = model; **DB** = constraint/trigger; **Q** = `scope`; **L** = ledger (`validate_entry`, registry); **T** = scan test; **job** = command.

### Entity separation and set-up
1. A run, its payslips, lines, inputs, loans, payments, files and settlements share the employing `entity`; only that entity's employees and accounts are used. The ledger refuses other entities' accounts again at posting. **[M clean + svc + L; AT-1]**
2. Lists, payslips, files and reports go through `scope`; payslip detail needs `payroll.view_payslip` (logged, PDPL). Another entity's record → 404. **[Q + svc; AT-1]**
3. A run needs `PayrollSettings.enabled` and `HrSettings.jurisdiction = SA`. A new entity runs payroll once its HR set-up, payroll settings and accounts exist: no code change. **[svc; AT-2]**
4. Entities with another jurisdiction (Egypt, China, UAE) cannot run payroll (bilingual refusal) until OQ-3 is answered. **[svc; AT-2]**
5. All amounts are in `entity.base_currency`, `Decimal`, rounded `ROUND_HALF_UP` to 2 dp per line per employee (OQ-25); float refused. Contracts in another currency are refused upstream (hr R18; OQ-4). **[svc + M; AT-3]**

### Run lifecycle and posting
6. One `regular` run per (entity, period); `off_cycle` runs need an Arabic reason and cover only named employees. `number` is assigned at approval. **[DB unique + svc; AT-4]**
7. Status: `draft → calculated → submitted → approved → paid`; `submitted → returned → calculated` (recalculate); `approved → reversed` (R13). No other transition; on PostgreSQL a trigger refuses status changes outside the services (as invoicing). **[svc + M + DB; AT-4]**
8. `calculate_run` is deterministic from: approved contract versions in force (hr R20), approved hr leave and sick days, approved inputs up to cut-off, active loan installments, and parameters valid in the period. It replaces the run's lines and sets `calc_version` and `inputs_hash`. Approval recomputes the hash; if any source changed since calculation, approval is refused ("recalculate", en/ar) and the grant stays unused. **[svc; AT-5]**
9. Inputs approved after `input_cutoff_day` of the period are taken by the next regular run (default, OQ-20). Late leave or contract changes for a calculated period become retro lines in the next run (R14). **[svc; AT-8]**
10. Included employees: an approved contract in force on any day of the period. Joiners and leavers are prorated: monthly amount × paid days / 30 (`day_basis=thirty`) or / calendar days (OQ-6 **[CC-verify]**). An employee whose termination is approved and whose `last_working_day` is in the period is paid through the final settlement, not the run (default). Document-expiry and missing-GOSI-number flags are warnings only (hr R44). **[svc; AT-6]**
11. Approval: `payroll.approve_run` (**A**, **P**) by the entity's approver, not the run's creator, submitter or last modifier (checked on the stored run, access R35). In one transaction the service calls `create_entry(entity, entry_date, description, lines=None, post=True, grant=grant, document=run)`; the registered `build_lines` builds the lines (§5 E1). If the ledger refuses (closed period, Sharia-blocked account, missing account), approval fails and the run stays `submitted`. **[svc + L registry; AT-7]**
12. The run entry is dated the last day of the period (salaries accrued in their month). A closed period refuses approval; the app registers `pending_documents` so `close_period` refuses while a run for that period is `submitted`. **[L + svc; AT-7]**
13. Approved runs are immutable. Correction: (a) retro or adjustment lines in a later run (default); or (b) `payroll.reverse_run` (**P**) by an approver who did not prepare it, only while no `PayrollPayment` for the run is approved: `reverse_entry` on the run's entry dated in an open period, run `reversed`, loan installments back to `planned`, EOSB sub-ledger rows mirrored. A reversed run is not re-approved; a new off-cycle or regular run is prepared. **[svc + L; AT-9]**

### Earnings and deductions
14. Earnings per employee = prorated fixed components + overtime + approved one-off earnings + retro differences (new terms − paid terms for past periods, per component, after hr R20) − unpaid-leave deduction − sick-leave reduction. **[svc; AT-6, AT-8]**
15. Overtime: hours from approved inputs only (no attendance, hr OQ-7). Pay per hour = (monthly wage / `day_basis` days / `daily_hours`) + 50% of (monthly BASIC / days / hours), i.e. hourly wage plus half the hourly basic **[CC-verify]**, using components flagged per hr 3.3. Caps and pre-approval: OQ-5. **[svc; AT-10]**
16. Unpaid days (hr R34 and inputs): days × Σ components with `deducted_for_unpaid_leave` / 30 (OQ-6). **[svc; AT-6]**
17. Sick leave: for each `SickLeaveDay` with `pay_pct` < 100, deduct daily wage × (100 − pay_pct)% **[CC-verify tiers in hr R32]**. **[svc; AT-11]**
18. Deductions: employee GOSI, loan installments, approved one-off deductions. Limits from parameters: a loan installment deduction ≤ 10% of the wage, and total deductions ≤ 50% of the wage **[CC-verify both]**; an installment that would break a limit is reduced and the rest re-planned (warning). Net pay < 0 refuses calculation. Disciplinary fines are not supported (OQ-8). **[svc; AT-12]**
19. Each line has an account of the entity: earnings → component `expense_account` (default 5100); employer GOSI → 5110; deductions → their liability or asset account (§5). None may be Sharia-prohibited where enforced (checked at calculation; ledger again at posting). **[svc + L; AT-13]**

### GOSI (all figures **[CC-verify]**)
20. Contribution wage = Σ components with `counts_for_gosi` (basic + housing **[CC-verify]**; housing deemed at 25% of basic when not paid in cash **[CC-verify]**), as registered at the start of the month **[CC-verify]**, bounded by floor SAR 1,500 and cap SAR 45,000 per month **[CC-verify]**. **[svc; AT-14]**
21. Rates from parameter `gosi.rates` by nationality class, cohort and branch. Seed (unverified): Saudi annuities 9% employer + 9% employee for employees insured before the new Social Insurance Law date (2024-07-03 **[CC-verify]**), with stepped higher annuity rates for new entrants **[CC-verify]**; SANED 0.75% employer + 0.75% employee (Saudi only) **[CC-verify]**; occupational hazards 2% employer (Saudi and non-Saudi) **[CC-verify]**; non-Saudi: occupational hazards only, no employee share **[CC-verify]**. GCC nationals: OQ-9. **[svc + parameter; AT-14]**
22. Each branch and party is a separate line per employee, rounded per R5; employee share is a deduction, employer share an expense. Both credit 2320 (proposed). **[svc; AT-14]**
23. `GosiReconciliation` compares ERP contributions with the GOSI monthly invoice entered by the payroll officer; differences are reported, never posted automatically (OQ-9). **[svc; AT-15]**

### EOSB provision (account 2500)
24. Each regular run adds, per employee, `eosb_accrual` = required provision at period end − the employee's `EosbProvisionEntry` balance. Required provision = `hr.calculate_eosb(employee, as_of=period end, kind=measurement basis)`; default basis = full award as on employer termination (fraction 1, OQ-11). Positive → Dr 5120 (proposed) / Cr 2500; negative → reverse sides. One `accrual` sub-ledger row per employee. **[svc; AT-16]**
25. Tie-out: Σ `EosbProvisionEntry` per entity = ledger balance of 2500 at every run approval; a difference (e.g. a manual journal on 2500) is a run warning and appears on the EOSB report (en/ar). **[svc + report; AT-16]**

### Loans and advances (Sharia)
26. Loans and advances are interest-free in **every** entity, whatever `sharia_enforced` says: the model has no interest/profit/fee field; Σ installments of each schedule version = outstanding principal exactly; no line, input or account may add a charge for a loan; a late or skipped installment adds nothing. Loan agreements and payslips state "interest-free / قرض حسن بدون فوائد" **[AR-review]**. Any attempt is refused with a Sharia message (en/ar). **[svc + M + T; AT-17]**
27. Lifecycle: drafted by `payroll.create_loan`, approved by `payroll.approve_loan` (**A**, **P**): approval posts the disbursement (§5 E4). Installments are deducted by runs (R18); rescheduling or skipping needs a new approved schedule version. Policy limits (amount, count, eligibility): OQ-7. **[svc + L; AT-17]**
28. On termination, the outstanding balance becomes a `loan_recovery` line in the final settlement (deduction from EOSB and dues **[CC-verify]** legality); an unrecovered remainder needs an approved write-off (OQ-7; account not yet decided, so write-off is refused until then). **[svc; AT-20]**

### Payments, WPS, Mudad
29. `PayrollPayment` (net salaries, GOSI, final settlement) is drafted by the payroll officer after the bank release and approved by the approver (**A**, **P**), which posts §5 E3. Amount ≤ open payable for that run or settlement; partial payments allowed; run `paid` when net is fully paid. **[svc + L; AT-18]**
30. Payroll never pays or posts for another entity; no shared-payroll intercompany flow (OQ-2). **[svc + L; AT-1]**
31. WPS file: generated only from an `approved` run with `PayrollSettings.wps_format` set; content and layout from the format parameter (OQ-10 **[CC-verify]**: bank SIF / Mudad format, fields, encoding). Employees without a valid IBAN are listed as exceptions, not included. Generation writes nothing to the run or ledger; sha256, generator and each download are logged; the file is purged after the retention days. **[svc; AT-19]**
32. Mudad submission is manual by default: the officer uploads the file in Mudad **[CC-verify]** and records reference and status. No API call (OQ-10). **[svc; AT-19]**

### Payslips
33. One payslip per employee per approved run, rendered from stored lines (never recalculated), bilingual (English and Arabic on the same slip, default OQ-16): entity legal names, period, employee code and name, earnings, deductions, employee GOSI, net, loan balance, annual-leave balance. Only `payroll.view_payslip` holders and (later) the employee see it; each view or print logged. **[svc; AT-21]**

### Final settlement
34. Created as a draft by hr termination approval (hr R38) with lines: salary to `last_working_day`, leave encashment (balance × daily wage of `counts_for_eosb` components / 30 **[CC-verify]**), EOSB = hr snapshot award, notice pay input, other approved earnings, GOSI on the salary part **[CC-verify]**, loan recovery, other deductions. `due_date` = `last_working_day` + 7 days (employer ends) or + 14 days (employee resigns) **[CC-verify, Art. 88]**; an alert is sent 3 days before. Approval (**A**, **P**) posts §5 E5: EOSB is charged first to the employee's 2500 balance, the shortfall to 5120, an excess released to 5120 (`utilisation` + `release` sub-ledger rows). **[svc + L; AT-20]**

### Intercompany, privacy, Sharia, agents
35. Payroll posts no intercompany lines (never 1210/2110): seconded staff cost stays in the employing entity. Recharge is billing's contract invoice (billing B3, P4). Payroll provides a monthly **secondment cost report** per assignment (`employee_code`, gross, employer GOSI, EOSB accrual, days on secondment) for the accountant pricing or checking billing contracts; whether recharge follows actual cost: OQ-15. **[svc + report; AT-22]**
36. Journal entries are summarised per account per document; descriptions carry the document number and period only, never names, IDs or individual pay (PDPL). Per-employee detail stays in payroll. **[svc build_lines; T; AT-7]**
37. No payroll account may be Sharia-prohibited where enforced; no interest arises on late salary, loans or GOSI (late GOSI fines, if any, are a manual matter, OQ-9). **[svc + L; AT-13]**
38. No payroll permission is **Ag** (default, OQ-21); system users have none. **[catalogue; AT-23]**
39. An approver who is also an employee in the run may approve it only if their own payslip has no inputs (overtime, one-off, retro) in that run; otherwise the approval is refused for them and goes to another approver or the `managing_partner` fallback (default, OQ-14). **[svc; AT-24]**
40. All messages, payslips, reports and file exception lists are en/ar. **[T; AT-21]**

## 5. Accounting

Codes from `ledger/coa_template.py`. Existing: 1100 Cash and bank, 2300 Accrued expenses, 2500 End-of-service provision, 5100 Salaries and benefits, 5110 GOSI, 5700 Bank charges. **Proposed, CFO to approve (not added to the template until approved; P6, OQ-12):**

| Code | name_en | name_ar [AR-review] | Type | Flags | Used for |
|---|---|---|---|---|---|
| 1230 | Employee loans and advances | قروض وسلف الموظفين | asset | monetary | Loans (E4) |
| 2310 | Salaries payable | رواتب مستحقة الدفع | liability | monetary | Net pay (E1, E5). Alternative if rejected: 2300 |
| 2320 | GOSI payable | اشتراكات التأمينات الاجتماعية المستحقة | liability | monetary | Employer + employee GOSI (E1) |
| 5120 | End-of-service benefit expense | مصروف مكافأة نهاية الخدمة | expense | | EOSB accrual (E1, E5). Alternative: 5100 |
| 2330 / 5130 | Accrued leave / Leave expense | مخصص الإجازات / مصروف الإجازات | liability / expense | | Only if hr OQ-6 decides leave is accrued |

Registered types (`PayrollConfig.ready()`): `PayrollRun` (post `payroll.approve_run`, reverse `payroll.reverse_run`), `PayrollPayment` (`payroll.approve_payment`), `EmployeeLoan` (`payroll.approve_loan`), `FinalSettlement` (`payroll.approve_final_settlement`); each with `build_lines`, `creator_field="created_by"`, and `pending_documents` for submitted runs. Figures below use the **unverified** seed GOSI rates and EOSB table **[CC-verify]**; amounts change when parameters are verified.

**E1. Monthly run, ALM, March 2027, dated 2027-03-31.** Saudi employee S1: BASIC 10,000.00, HOUSING 2,500.00, TRANSPORT 1,000.00 (GOSI wage 12,500.00; employee 9% + 0.75% = 1,218.75; employer 9% + 0.75% + 2% = 1,468.75). Non-Saudi N1: BASIC 6,000.00, HOUSING 1,500.00, TRANSPORT 500.00 (GOSI wage 7,500.00; employer 2% = 150.00); loan installment 500.00. EOSB accrual: S1 562.50, N1 333.33.

| Account | Dr | Cr |
|---|---|---|
| 5100 Salaries and benefits | 21,500.00 | |
| 5110 GOSI (employer) | 1,618.75 | |
| 5120 EOSB expense (proposed) | 895.83 | |
| 2320 GOSI payable (proposed) | | 2,837.50 |
| 1230 Employee loans and advances (proposed) | | 500.00 |
| 2310 Salaries payable (proposed) | | 19,781.25 |
| 2500 End-of-service provision | | 895.83 |
| **Total** | **24,014.58** | **24,014.58** |

A negative EOSB accrual swaps 5120 and 2500. Retro, overtime and one-off earnings add to the 5100 (or component account) line; unpaid-leave and sick reductions lower it.

**E2. GOSI payment** (2,837.50): Dr 2320 2,837.50 / Cr 1100 2,837.50.

**E3. Net salary payment** (19,781.25): Dr 2310 19,781.25 / Cr 1100 (settings bank account) 19,781.25. Bank charges, if any, are a separate bank document (Dr 5700).

**E4. Loan disbursement** (3,000.00, interest-free): Dr 1230 3,000.00 / Cr 1100 3,000.00. Repayment through E1 (Cr 1230). No interest or fee line ever exists.

**E5. Final settlement, N1 resigns after 4 years** (wage basis 8,000.00; full award 16,000.00 = provision balance; resignation fraction 1/3 → EOSB 5,333.33 **[CC-verify]**; 10 days leave 2,666.67; loan balance 1,000.00):

| Account | Dr | Cr |
|---|---|---|
| 2500 End-of-service provision (utilise 5,333.33, release 10,666.67) | 16,000.00 | |
| 5100 Salaries and benefits (leave encashment) | 2,666.67 | |
| 5120 EOSB expense (release of excess provision) | | 10,666.67 |
| 1230 Employee loans and advances | | 1,000.00 |
| 2310 Salaries payable | | 7,000.00 |
| **Total** | **18,666.67** | **18,666.67** |

Payment as E3 (kind `final_settlement`). If the award exceeds the provision, the shortfall is Dr 5120.

**E6. Run reversal** (R13): `reverse_entry` mirror of E1, dated in an open period, before any payment.

**E7. Secondment recharge**: none in payroll. Billing invoices the receiving entity (billing B3: Dr 1210 counterparty / Cr 4210 / Cr 2200 in the seconding entity).

**No entry** for: inputs, calculation, submission, payslips, WPS files, Mudad records, GOSI reconciliation, draft loans or settlements, leave accrual (until hr OQ-6).

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| `hr` (internal) | In | Contracts, components, leave, sick tiers, EOSB calculation, terminations, statutory parameters | hr.md |
| `ledger` (internal) | Out | `create_entry(post=True, grant, document)` for 4 registered types; `reverse_entry`; `pending_documents` | access R34 to R36 |
| `billing` (internal) | Out (report) | Secondment cost report per assignment (no PD beyond `employee_code`) | R35; OQ-15 |
| GOSI **[CC-verify]** | none / manual | Contribution report per month; GOSI invoice amount entered for reconciliation; employee registration on the GOSI portal | OQ-9 |
| Mudad / WPS **[CC-verify]** | Out (file, manual upload) | Salary file per approved run; submission reference and status entered manually | OQ-10 **B** for file generation |
| Banks | Out (file via Mudad or bank portal) / In (manual) | Salary file; payment confirmation recorded as `PayrollPayment` | Bank module later |
| Qiwa **[CC-verify]** | none | — | hr OQ-21 |
| ZATCA | none | Payroll is not a VAT supply; secondment invoices go through billing/invoicing | — |
| Daftra / SAP B1 | In (one-off) | Opening EOSB provision per employee (`opening` rows tied to 2500), open loans, YTD | migration-agent; OQ-26 |

## 7. Permissions

Roles `payroll_officer` and `hr_officer` are **proposed** (hr OQ-2). No permission is **Ag** (R38).

| Codename | Flags | Holders (default) |
|---|---|---|
| `payroll.view` (aggregates, masked) | | payroll_officer, approver, accountant, viewer |
| `payroll.view_payslip` (individual pay) | | payroll_officer, approver (logged) |
| `payroll.manage_inputs` / `payroll.approve_input` | — / A | payroll_officer, hr_officer / payroll_officer (not own entry, not own pay) |
| `payroll.prepare_run` / `payroll.submit_run` | | payroll_officer |
| `payroll.approve_run` | A, P | approver |
| `payroll.reverse_run` | P | approver (not the preparer) |
| `payroll.create_loan` / `payroll.approve_loan` | — / A, P | payroll_officer / approver |
| `payroll.prepare_final_settlement` / `payroll.approve_final_settlement` | — / A, P | payroll_officer / approver |
| `payroll.record_payment` / `payroll.approve_payment` | — / A, P | payroll_officer / approver |
| `payroll.generate_wps_file` / `payroll.record_mudad_submission` | S! (file holds PD) / — | payroll_officer |
| `payroll.manage_settings` | | approver |
| `payroll.view_cost_reports` (secondment cost, GOSI, EOSB reports without PD) | | accountant, approver, viewer |

| Action | payroll_officer | hr_officer | approver (CFO) | accountant | viewer | managing_partner | Agent / system |
|---|---|---|---|---|---|---|---|
| Enter inputs | Y | Y | | | | | No |
| Prepare, calculate, submit run | Y | | | | | | No |
| Approve (= post) run, loan, settlement, payment | | | Y (not preparer/submitter/modifier; R39) | | | Fallback (access OQ-1) | No |
| Reverse run | | | Y | | | | No |
| Generate WPS file | Y | | | | | | No |
| See payslips | Y | | Y | | | | No |
| See cost reports | | | Y | Y | Y | | No |

## 8. Acceptance tests

Fixtures: ALM (SA, SAR, Sharia enforced), SUB (SA, SAR), EGY (EG, EGP); template chart plus proposed accounts 1230, 2310, 2320, 5120 (test fixture only); 2027 periods open unless stated; hr employees S1 (Saudi, as E1) and N1 (non-Saudi, as E1) in ALM, S2 in SUB; unverified seed parameters with `HR_REQUIRE_VERIFIED_PARAMETERS=False` unless stated. Users: P (payroll_officer ALM), P2 (payroll_officer ALM), C (approver ALM), C2 (approver SUB), A (accountant ALM), V (viewer), MP, agent. Anonymised data only.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | Runs in ALM and SUB | P lists runs; opens SUB run; a build_lines variant targets SUB's 2310; ALM run tries to include S2 | ALM only; SUB → 404; ledger refuses other-entity account; S2 refused (R1, R2, R30) |
| AT-2 | NEW entity set up as data with payroll enabled; EGY enabled | Run March for NEW; for EGY | NEW runs with no code change; EGY refused "jurisdiction not configured" (en/ar) (R3, R4) |
| AT-3 | Input amount as float; S1 contract in USD (fixture forced) | Save input; calculate | Float refused; calculation refused naming the currency (R5) |
| AT-4 | March regular run approved | Create a second regular March run; off-cycle without `reason_ar`; set `status="approved"` directly on a draft | Refused; refused; refused (model, and DB trigger on PostgreSQL) (R6, R7) |
| AT-5 | Run `submitted` | Approve after an hr leave approval changed S1's unpaid days | Refused "recalculate" (en/ar), grant unused; after recalculation and resubmission C approves (R8) |
| AT-6 | N1 joins 2027-03-11; S1 has 2 unpaid days | Calculate March | N1 paid 20/30 of each component (`thirty` basis); S1 deduction 2 × 13,500.00 / 30 = 900.00; expired-iqama warning shown, run not blocked (R10, R14, R16) |
| AT-7 | Run as E1, prepared and submitted by P | P approves; C approves; repeat with 2027-03 closed; `close_period(2027-03)` while submitted | P refused (A); C: entry as E1, dated 2027-03-31, `document_type`/`id` = run, number `ALM-PAY-2027-000001`, descriptions without names; closed: approval refused, run stays `submitted`; close refused (`documents_pending`) (R11, R12, R36) |
| AT-8 | S1 v2 (BASIC +1,000.00 effective 2027-03-15) approved 2027-04-10 after March approved | Calculate April | April full new BASIC + retro line for 2027-03-15..31 = 1,000.00 × 17/30 = 566.67 (`thirty`), GOSI and EOSB recomputed on the new wage (R9, R14) |
| AT-9 | March run approved, no payment | C reverses; then approves a payment and tries reversing another run with an approved payment; P reverses | Reversal posts mirror (E6), run `reversed`, installments `planned`, EOSB rows mirrored; reversal after payment refused; P refused (R13) |
| AT-10 | S1 with 10 approved overtime hours, `daily_hours` 8 | Calculate | Pay = 10 × (13,500.00/30/8 + 0.5 × 10,000.00/30/8) = 10 × (56.25 + 20.833…) = 770.83 **[CC-verify formula]** (R15) |
| AT-11 | N1 with 5 sick days at 75% | Calculate | Deduction 5 × 8,000.00/30 × 25% = 333.33 (R17) |
| AT-12 | N1 wage 8,000.00, installment 1,000.00; one-off deduction making net negative | Calculate | Installment capped at 800.00 (10% **[CC-verify]**), 200.00 re-planned, warning; negative-net case refused (R18) |
| AT-13 | Component account set to 5610 in ALM (Sharia) | Calculate; force through to approval | Refused at calculation; ledger refuses at posting (R19, R37) |
| AT-14 | S1 GOSI wage 12,500.00; Saudi S3 with BASIC 50,000.00, HOUSING 12,500.00; N1 | Calculate | S1 employee 1,125.00 + 93.75, employer 1,125.00 + 93.75 + 250.00; S3 capped at 45,000.00; N1 employer OH 150.00 only, no employee share. **All rates [CC-verify]** (R20 to R22) |
| AT-15 | GOSI invoice entered 2,900.00 for March | Reconcile | Difference 62.50 reported (en/ar), nothing posted (R23) |
| AT-16 | S1 provision balance 0 at start; manual journal of 100.00 to 2500 posted by C | March run; April run | March accrual = required provision − 0, Dr 5120 / Cr 2500, one `accrual` row per employee; April run warns "2500 differs from EOSB sub-ledger by 100.00" (R24, R25) |
| AT-17 | Loan drafted by P: principal 3,000.00, 3 installments of 1,100.00; another 3 × 1,000.00 with a 50.00 "admin fee" input; valid 3 × 1,000.00 | Submit; C approves valid; skip March without a new schedule | Sum ≠ principal refused; fee refused (Sharia message en/ar, also in a non-Sharia entity); valid posts E4; skip refused (R26, R27) |
| AT-18 | E1 approved | P records salary payment 19,781.25 and C approves; P records 20,000.00; P approves own | E3 posted, run `paid`; over-payment refused; self-approval refused (R29) |
| AT-19 | Approved run; `wps_format` null; then set; N1 without IBAN | Generate; generate again after set; download | First refused; file with S1 only, N1 in exceptions, sha256 stored, generation and download logged; run and ledger unchanged; Mudad reference recorded manually (R31, R32) |
| AT-20 | N1 resignation approved (hr AT-20), 4 years, balance 10 days, loan 1,000.00, provision 16,000.00 | P prepares; C approves; check alert | Lines and entry as E5 (`utilisation` 5,333.33, `release` 10,666.67); `due_date` = last day + 14 **[CC-verify]**; alert 3 days before; loan `settled_in_final` (R28, R34) |
| AT-21 | Approved run; Arabic-preference user | Render S1 payslip; V opens it; P opens it | Both languages, stored values (identical after a parameter change); V refused; P allowed, view logged; all messages en/ar (R33, R40) |
| AT-22 | S1 seconded to SUB for 2027-03-01..10 | Approve March run; open secondment cost report | No 1210/2110 line in the run entry; report shows `employee_code`, 10 days, prorated gross, employer GOSI, EOSB accrual; no name (R35) |
| AT-23 | Agent user; billing system user | Agent drafts an input; system user calculates a run | Both refused (R38) |
| AT-24 | C is employee C-E in ALM; March run includes C-E with an overtime input | C approves; MP approves | C refused (own pay inputs); MP allowed. Without inputs on C-E, C allowed and logged (R39) |

## 9. Open questions

**B** = blocks building that part; **GL** = blocks go-live; **D** = default exists.

| # | Question | Owner | Blocks? | Default |
|---|---|---|---|---|
| OQ-1 | Pay period and pay day per entity. | CFO | D | Calendar month; pay day 27 |
| OQ-2 | Is payroll run in-house per entity, or does Almiras run and pay payroll for subsidiaries (shared service, intercompany)? Which entities have employees (hr OQ-1)? | Managing Partner | D | Per entity, own bank account; `enabled` False until set |
| OQ-3 | Payroll for non-Saudi entities (Egypt, China, UAE subsidiaries): local law, social insurance, income tax withholding, local payroll provider? | Managing Partner | **B** for those entities | Refused (R4) |
| OQ-4 | Staff of Saudi entities paid in foreign currency (e.g. abroad): allowed? rate, account, WPS treatment? | Managing Partner + CFO | **B** for any such employee | Refused; functional currency only |
| OQ-5 | Overtime policy: pre-approval, caps, rates above statutory **[CC-verify]** statutory caps. | CFO | D | Approved hours only; statutory formula; no cap |
| OQ-6 | Daily-rate basis for proration, unpaid leave and overtime (/30 vs calendar days) **[CC-verify]** whether law prescribes. | CFO + compliance-checker | D | /30 |
| OQ-7 | Loan policy: who is eligible, max amount and term, approval thresholds, write-off on exit and its account, deduction cap **[CC-verify]**. | CFO + Managing Partner | D | Any active employee; no max; CFO approves; write-off refused until decided |
| OQ-8 | Disciplinary fines and their treatment (labour-law fund, never income **[CC-verify]**). | Legal | D | Not supported |
| OQ-9 | GOSI details **[CC-verify]**: annuity rates by cohort under the new Social Insurance Law, GCC nationals, cap/floor, wage composition and reference date, housing deemed value, late-payment fines, reconciliation import format. | compliance-checker | **GL** (real runs); build on parameters | Seed per R20, R21; manual reconciliation |
| OQ-10 | WPS / Mudad **[CC-verify]**: file format(s) per bank, fields, encoding, Mudad upload vs API, which banks each entity uses, file retention. | compliance-checker + CFO | **B** for WPS file generation | No file until a format is configured; manual upload; 30 days retention |
| OQ-11 | EOSB provision measurement (IFRS for SMEs s28 **[CC-verify]**): vested amount on termination basis, resignation basis, or actuarial. | Accountant | D | Termination basis (fraction 1) |
| OQ-12 | Approve proposed accounts 1230, 2310, 2320, 5120 (and 2330/5130 if leave is accrued); or use 2300 / 5100 instead. | CFO / accountant | **GL** | Build with the proposed codes in fixtures; template unchanged until approved |
| OQ-13 | Payment recording: may the payroll officer post payments alone (like receipts, access A16)? Link to future bank reconciliation. | Managing Partner | D | No: approver approves payments |
| OQ-14 | Approver who is an employee in the run (own pay). | Managing Partner | D | R39 |
| OQ-15 | Secondment recharge basis: billing contract price (fixed) or actual payroll cost (cost-plus, monthly variable); does payroll feed billing automatically? | Managing Partner + CFO | D | Billing contract price; payroll gives a report only |
| OQ-16 | Payslip delivery (print, e-mail, self-service) and PDF tool (open source); one bilingual slip or per language. | HR | D | In-app HTML/print, both languages on one slip |
| OQ-17 | Retro pay and corrections: next-run lines vs off-cycle run; limits. | CFO | D | Next regular run |
| OQ-18 | Cost centres / departments / projects on payroll lines. | Accountant | D | None |
| OQ-19 | Bonuses, commissions, annual air-ticket allowance, medical insurance, other benefits and their accruals. | CFO | D | One-off inputs; no accruals |
| OQ-20 | Input cut-off day and late inputs. | CFO | D | Day 20; later inputs next run |
| OQ-21 | May agents draft payroll inputs (e.g. overtime import)? | Managing Partner | D | No |
| OQ-22 | Reversal of approved runs: allowed at all, by whom, after payment? | CFO | D | R13 |
| OQ-23 | Final settlement deadlines and alerts **[CC-verify]**. | compliance-checker | D | 7 / 14 days; alert 3 days before |
| OQ-24 | GOSI on leave encashment and notice pay; leave-encashment wage basis **[CC-verify]**. | compliance-checker | D | No GOSI on encashment; basis = `counts_for_eosb` components |
| OQ-25 | Rounding level: per line per employee vs per employee total. | Accountant | D | Per line per employee |
| OQ-26 | Migration: opening EOSB per employee, leave balances, open loans, YTD figures from Daftra / SAP B1. | Accountant + migration-agent | D | `opening` rows tied to the 2500 opening balance; loans as `active` with remaining schedule |
| OQ-27 | Run number format. | Accountant | D | `{entity.code}-PAY-{year}-{000001}` |

**Blocking:** OQ-3 (non-SA entities), OQ-4 (foreign-currency pay), OQ-10 (WPS file), hr OQ-16 (billing interface). **Go-live:** OQ-9 (GOSI parameters verified), OQ-12 (accounts approved), hr OQ-11 (PDPL), hr OQ-18 (EOSB). **Before building:** the compliance-checker verifies every [CC-verify] item; MP approves the roles (hr OQ-2).
