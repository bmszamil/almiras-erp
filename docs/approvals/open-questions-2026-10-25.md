# Almiras ERP: open questions for the owner and advisers

Prepared 2026-10-25 from every specification in `docs/specs/`, checked against the owner answers of 2026-10-18 (sections A to J), the answered owner/CFO decision pack of 2026-10-17 and the compliance reports in `docs/compliance/`. Code state: branch `claude/funny-davinci-7yd88s`, commit ce82992.

## How to use this document

- Every question has a number (**Q1**, **Q2**, ...). Answer under **Your answer**, or reply with the number and your answer, for example "Q12: option (b)".
- **Who should answer** says which professional the question is for. Each section below can be forwarded to that person on its own. Where a second person is named (for example "Tax adviser; then CFO to approve"), the first gives the advice and the second decides.
- **Priority:** *Blocks build* = the team cannot build that part until it is answered. *Blocks go-live* = needed before real use. *Needed before ...* = needed before that event (first VAT return, first payroll, first annual statements, ...). *Low* = the system already uses a safe default.
- **Part 1** holds the questions that need a real answer. **Part 2** holds the low-priority questions: the system already uses a safe default (most of them the team's recommendation, applied under your instruction "go with your recommendations"), so each one only needs a yes or a change. Part 1 also contains some "confirm the default" questions; they are there because the answer is needed soon. You may confirm Part 2 in bulk, for example "I confirm all of Part 2 except Q201 and Q215".
- Nothing here asks for passwords, keys or real personal data. Do not send those by e-mail or chat; the ZATCA portal login (in the owner section) is entered by you directly in the ZATCA portal.

## Glossary

- **AR / AP:** money customers owe us (receivables) / money we owe suppliers (payables)
- **Control account:** a ledger account (1200 receivables, 1210 due from group companies, 2100 payables, 2110 due to group companies) that only the system's own documents may post to, so its balance always equals the list of open items behind it
- **ZATCA:** the Zakat, Tax and Customs Authority; it receives every e-invoice (Fatoora)
- **VAT:** value added tax, 15% standard rate
- **WHT:** withholding tax deducted from payments to non-resident suppliers
- **RETT:** real estate transaction tax, 5% on real-estate sales
- **GOSI:** General Organization for Social Insurance
- **Mudad / WPS:** the Ministry of Human Resources wage protection system; salary files are uploaded there
- **EOSB:** end-of-service benefit (gratuity) under the Labour Law
- **PDPL:** the Saudi Personal Data Protection Law
- **FX / revaluation:** foreign exchange; at month end, foreign-currency balances are restated at the closing rate
- **SAMA:** the Saudi Central Bank, source of exchange rates
- **Bai' ajel:** a Sharia-compliant sale to an employee at a fixed price paid in instalments
- **Ibra':** a discount given when the buyer pays early
- **Murabaha / tawarruq:** Islamic financing: the bank buys and resells at cost plus an agreed profit
- **Authority matrix:** the per-company table of who approves what, up to which amount
- **Falcon:** the legacy accounting system the opening balances come from
- **IFRS for SMEs:** the accounting framework for most group companies; full IFRS for others

## Summary

| Who should answer | Part 1: needs an answer | Part 2: low priority | Total |
|---|---:|---:|---:|
| Owner / Managing Partner | 21 | 39 | 60 |
| CFO | 26 | 18 | 44 |
| Accountant | 26 | 42 | 68 |
| External auditor | 3 | 2 | 5 |
| Tax adviser | 36 | 8 | 44 |
| Sharia board | 9 | 4 | 13 |
| Legal adviser | 11 | 7 | 18 |
| Egypt adviser (tax and labour) | 4 | 0 | 4 |
| HR / payroll manager | 5 | 0 | 5 |
| IT / security lead | 2 | 3 | 5 |
| Bank relationship managers | 1 | 0 | 1 |
| Other | 1 | 0 | 1 |
| **Total** | **145** | **123** | **268** |

By priority: 11 block building, 35 block go-live, 99 are needed before a specific event, 123 are low (safe default in place).

**Start with the questions marked *Blocks build* and *Blocks go-live*:**

- Q1 (Owner / Managing Partner): Advances deducted from later invoices, and forfeited deposits
- Q2 (Owner / Managing Partner): Bank statement files from each of the five banks
- Q3 (Owner / Managing Partner): A person must check and approve every legal parameter before go-live
- Q4 (Owner / Managing Partner): Approval limits (authority matrix) for fixed assets and consolidation in each company
- Q5 (Owner / Managing Partner): Income tax on the share of foreign shareholders
- Q6 (Owner / Managing Partner): Locking Django admin to superusers (needs a membership screen first)
- Q7 (Owner / Managing Partner): Second approver for authority-matrix changes; naming the board representatives
- Q8 (Owner / Managing Partner): Who approves what in each company (authority matrix content for payroll and HR)
- Q9 (Owner / Managing Partner): Who can act on the ZATCA keys when the Managing Partner is unavailable
- Q10 (Owner / Managing Partner): Who may onboard ZATCA e-invoicing devices
- Q11 (Owner / Managing Partner): ZATCA go-live dates and which companies go live in which wave
- Q12 (Owner / Managing Partner): ZATCA simulation test with the owner's own Fatoora portal login
- Q22 (CFO): Cut-over date per company
- Q23 (CFO): New accounts for fixed assets (and renaming account 1510)
- Q24 (CFO): Useful lives, depreciation rates and residual values per asset category
- Q48 (Accountant): Group consolidation: buying, selling or changing the ownership of a company during the year
- Q49 (Accountant): Group consolidation: translating companies that keep their books in another currency (e.g. Egypt)
- Q50 (Accountant): Sending documents back to the preparer with a reason
- Q51 (Accountant): Where the system gets exchange rates automatically
- Q52 (Accountant): Bank balances and uncleared items at cut-over from Falcon
- Q53 (Accountant): Falcon export formats (chart of accounts and trial balance)
- Q54 (Accountant): Saudi IBAN bank codes and foreign bank account details
- Q77 (Tax adviser): Fixed-asset sales: link from the sales invoice to the asset, and how long to keep capital-asset purchase bills
- Q78 (Tax adviser): Real estate and vehicles: VAT and Real Estate Transaction Tax (RETT) when an asset is sold or transferred
- Q79 (Tax adviser): Input VAT on bills still unpaid after 12 months
- Q80 (Tax adviser): Official VAT return form: check every box
- Q81 (Tax adviser): Prepayment (advance payment) invoices, ZATCA type 386
- Q82 (Tax adviser): Withholding tax on payments to foreign suppliers: remaining points
- Q83 (Tax adviser): Withholding tax on remuneration of people working abroad
- Q84 (Tax adviser): Zakat: how the base is calculated
- Q85 (Tax adviser): Zakat: one combined return for the group
- Q113 (Sharia board): Early-payment discount (ibra') on a bai' ajel contract brought over from the old system
- Q114 (Sharia board): Interest or a penalty charged by a bank to a Sharia-compliant company
- Q122 (Legal adviser): Personal data in bank statements (PDPL)
- Q123 (Legal adviser): Personal data: lawful basis, offshore services, privacy notice, data requests
- Q124 (Legal adviser): Personal-data protection (PDPL) for employee records
- Q125 (Legal adviser): Value of free company housing in the end-of-service benefit
- Q126 (Legal adviser): Who may read the access log
- Q127 (Legal adviser): Withholding tax records of individuals abroad (PDPL)
- Q133 (Egypt adviser (tax and labour)): Egypt payroll and HR rules (all Egypt figures still unverified)
- Q137 (HR / payroll manager): Remaining GOSI rules not found in the law or the Implementing Regulations
- Q138 (HR / payroll manager): What counts as the GOSI wage
- Q142 (IT / security lead): Signing keys: backup, disaster recovery and "non-exportable" keys
- Q143 (IT / security lead): Who must use MFA (two-step sign-in)
- Q144 (Bank relationship managers): Salary (WPS) file formats of the five salary banks
- Q145 (Other): Arabic texts of the screens need a native reviewer

# Part 1: questions that need an answer

## Owner / Managing Partner

### Q1. Advances deducted from later invoices, and forfeited deposits
- **Who should answer:** Owner for question 1; tax adviser for questions 2 and 3; then CFO to approve
- **Priority:** Blocks build (contracts with an advance that is deducted from later invoices)
- **Context:** A contract may require the customer to pay an advance that is later deducted from the monthly invoices. ZATCA expects a special "prepayment invoice" when the advance is received, and the later invoices must show the deduction. The ERP cannot yet build that deduction, and ZATCA's test portal could not confirm the format (sandbox run of 2026-10-19). So any contract with such an advance is refused when someone tries to approve it. Refundable security deposits are allowed: no VAT when received, and VAT follows the rent when applied (decision pack Tax 10). Only the case where the tenant loses (forfeits) the deposit is not decided.
- **Question:** 1. Do any current or planned contracts have an advance that is deducted from later invoices? If none, this can wait.
  2. Confirm that each advance needs its own prepayment tax invoice (within 15 days after the end of the month in which the money arrived) and that later invoices show the deduction.
  3. When a tenant forfeits a refundable deposit: (a) 15% VAT on the forfeited amount for commercial leases and exempt for residential leases (the suggestion in the decision pack), or (b) another treatment?
- **What the system does now:** Contracts with an advance deducted from invoices are refused at approval. Deposits are recorded and applied to rent. Forfeiture is not built.
- **Reference (for the team):** billing OQ-20 (partly resolved by D13(g), (i)); invoicing OQ-16 (deposit forfeiture); zatca OQ-29 / OQ-33 (prepayment invoice type 386); billing R30, R31, R68(b), AT-16, AT-47; invoicing R106.

---

## Priority: Blocks go-live (Billing / tax / banking)
- **Your answer:** 

### Q2. Bank statement files from each of the five banks
- **Who should answer:** Owner (Managing Partner), who obtains them from each bank relationship manager
- **Priority:** Blocks build (the import for each specific bank)
- **Context:** The ERP reads bank statement files and matches each bank line to receipts, payments and salaries already in the books. Readers for the three standard formats are built (MT940, ISO 20022 camt.053, and CSV with a column layout set per bank). Each bank fills these formats in its own way, so no bank is marked "supported" until a real sample from that bank has been tested. Until then the monthly bank reconciliation cannot start for that bank.
- **Question:** 1. For each bank (Riyad Bank, SNB, Al Rajhi, Bank AlJazira, Alinma), which statement file can it give a corporate client: (a) MT940, (b) camt.053 (XML), (c) CSV or Excel export, (d) PDF only? (PDF cannot be imported.)
  2. Please send one sample file per bank and format, with account numbers and names blanked or replaced.
  3. For CSV or Excel files: does the file show the opening and closing balance and a statement number? If not, the person importing types the balances in by hand (see banking OQ-24 below).
- **What the system does now:** Standard test files work. No bank-specific layout is marked supported. PDF is refused.
- **Reference (for the team):** banking OQ-1 (owner answers B4 in the decision pack and E3: "will be shared later"); banking R10 to R13, §6, AT-3, AT-9. (Billing / tax / banking)
- **Your answer:** 

### Q3. A person must check and approve every legal parameter before go-live
- **Who should answer:** Managing Partner (to name the verifier); then HR / payroll manager to do the checking
- **Priority:** Blocks go-live
- **Context:** All legal figures the system uses (GOSI rates, caps and floors, leave days, EOSB bands, overtime caps, deduction caps, Egypt rates) are stored as data rows that start "unverified". In production the system refuses to approve any payroll run, termination or settlement that used an unverified row. The team has checked the sources, but the rules require a named person, other than the one who entered a row, to mark it verified.
- **Question:** 1. Do you confirm the default: the HR officer of the group parent (Almiras) enters the rows and the Managing Partner verifies them after reading the compliance reports?
  2. If not, who enters and who verifies (for example the HR manager enters, the CFO verifies)?
  3. Do you accept that rows the team could not source officially (see the GOSI entries below) are verified on the basis of GOSI's own forms and guides, or must they wait for the official text?
- **What the system does now:** Every row is unverified; approvals in production are refused until each row used is verified by a second person.
- **Reference (for the team):** hr OQ-17 (and hr R7; affects every "unverified" row in hr 3.9 and payroll §11.3); hr R7, 3.9, AT-4; payroll R4, §10.1, §10.8 item 7 (Payroll / HR)
- **Your answer:** 

### Q4. Approval limits (authority matrix) for fixed assets and consolidation in each company
- **Who should answer:** Each company's top authority (Managing Partner in Almiras, the CEO elsewhere)
- **Priority:** Blocks go-live
- **Context:** The owner decided that each company has its own authority matrix: who may approve what, up to which amount. Fixed assets have seven approvable activities: capitalisation, policy, depreciation run, disposal or write-off, impairment, change of estimate, reversal. Consolidation has structure changes plus preparing, reviewing and approving a group consolidation (three different people).
- **Question:** 1. For each company and activity: who approves, and in which amount bands (for example, write-offs above SAR 50,000 to the top authority)? 2. Should write-offs or depreciation-policy approvals be "never self-approved", even by the top authority? 3. For group consolidation in Almiras: who records ownership changes, who prepares, who reviews and who approves?
- **What the system does now:** Where a company has no rows, the code default applies: CFO, CEO, and the Managing Partner in the group parent. No activity is marked "never self-approved". Consolidation proposes accountant prepares, CFO reviews, Managing Partner approves.
- **Reference (for the team):** fixed-assets OQ-21; consolidation OQ-10 (principle answered by owner A2, A10: each company has its own authority matrix; everything goes through one approval engine); fixed-assets R37, §7; consolidation R4, R10, R11, §7; access R70, R71, R76.

---

## Needed before a specific event (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q5. Income tax on the share of foreign shareholders
- **Who should answer:** Owner for question 1; tax adviser for question 2
- **Priority:** Blocks go-live (income tax, only for companies with foreign shareholders)
- **Context:** A company partly owned by non-Saudi, non-GCC shareholders pays income tax on their share of the profit, and Zakat on the Saudi share. Not built yet.
- **Question:** 1. Which group companies have, or may have, foreign shareholders, and what percentage?
  2. Please confirm: rate 20%; one combined Zakat and income tax return; whether advance tax payments, tax losses carried forward and deferred tax must be handled.
- **What the system does now:** Not built. Default design: rate 20%, combined return, no advance payments, losses or deferred tax.
- **Reference (for the team):** tax-returns OQ-26, OQ-2 (income tax part); tax R50, R51 **[CC-verify]**; tax-returns R50, R51, T9, AT-42.

---

## Priority: Needed before go-live (default in use) (Billing / tax / banking)
- **Your answer:** 

### Q6. Locking Django admin to superusers (needs a membership screen first)
- **Who should answer:** Managing Partner; then IT / security lead
- **Priority:** Blocks go-live
- **Context:** The plan is that the technical "admin" pages are for superusers only. But today the only place where the MP and access admins grant and remove people's access is that admin page. Locking it before a proper access screen exists would leave nobody able to grant access.
- **Question:** Confirm the default: build the access-management screen (grants, revocations, delegations, MFA reset) in the first release, and lock the admin pages to superusers only when that screen ships. Or (b) go live with staff access admins still using the admin pages (protected by MFA)?
- **What the system does now:** Admin pages need an MFA-verified session; the superuser-only lock exists but is switched off.
- **Reference (for the team):** web OQ-10; web W-D7, R19, J13, OQ-10; slice 2 "Admin".

---

## Needed before a specific event (named in each entry) (Access / web / ledger / migration)
- **Your answer:** 

### Q7. Second approver for authority-matrix changes; naming the board representatives
- **Who should answer:** Managing Partner; then each company's board (to name its representative)
- **Priority:** Blocks go-live (per company, to name the person)
- **Context:** Changes to a company's matrix need a second person. In Almiras the team proposes the group Chairman; in other companies a new role "board representative", named by that company's board, who can only approve matrix changes and read the governance report. The MP may hold it in a subsidiary as shareholder representative.
- **Question:** 1. Confirm the default: Almiras = Chairman; other companies = a board representative. 2. For each company, who is the board representative?
- **What the system does now:** Built as described; a company with no board representative cannot get a matrix change approved by anyone but the routes above.
- **Reference (for the team):** access OQ-42 (main part; the 2026-10-21 additions are answered by J1, J2, see the end); access R76, §10.10 items 5, 14, 15, OQ-42. (Access / web / ledger / migration)
- **Your answer:** 

### Q8. Who approves what in each company (authority matrix content for payroll and HR)
- **Who should answer:** Each company's CEO; then Managing Partner (project owner) to approve the set
- **Priority:** Blocks go-live (for each company)
- **Context:** The owner decided (A2) that each company has its own authority matrix: who approves each activity. The system ships with seed rows; each company must confirm or change them. In Almiras, overtime, loans and bai' ajel stay with the Managing Partner.
- **Question:** For each company, confirm or change these seed approvers:
  1. Payroll runs: HR officer (decided, P14). Final settlements, salary payments, run reversals: the entity approver (CFO).
  2. Loans and bai' ajel: CEO (Managing Partner in Almiras).
  3. Overtime: department head (Managing Partner in Almiras); when the head is absent, has no head, or asks for their own overtime: the next approver in the matrix. Is a `department_head` role acceptable?
  4. Employment contracts, salary changes, secondment starts and terminations: the entity approver (CFO), or an HR manager, or the CEO?
  5. Should any approval depend on the amount (for example a payroll run above SAR X needs the CEO)? Default: no amount limit for runs, overtime, inputs and clearances; money documents are measured by their SAR amount.
  6. May the payroll officer post a salary payment alone? (Default: no, an approver approves it.)
  7. May an HR officer approve a run that contains inputs they entered or approved themselves? (Default: no.)
  8. Do you accept the role permission list in hr §7 and payroll §7 as drafted?

  *Also raised in the access-web-ledger-migration list:* 1. Please provide, per company, the list of activities, amount bands and approver roles. 2. Confirm the default: a company without a full matrix may go live on the coded defaults (yes / no, must enter a full matrix first).
- **What the system does now:** Uses the seed rows above; the shared approval engine is being built and payroll's link to it (R82 to R84) is not yet connected.
- **Reference (for the team):** payroll OQ-47, OQ-36, OQ-13, OQ-54; hr OQ-3, OQ-30, hr OQ-2 follow-up (§7 permission list); duplicates access OQ-30, OQ-35, OQ-39 (to be answered once, with access); also access OQ-30, OQ-44; payroll R39, R45, R50, R82 to R84, §7; hr R55, §7; access R57, R58 (Payroll / HR)
- **Your answer:** 

### Q9. Who can act on the ZATCA keys when the Managing Partner is unavailable
- **Who should answer:** Owner (Managing Partner); IT / security lead for the restart point
- **Priority:** Blocks go-live
- **Context:** The Managing Partner alone holds the group super key and can delegate custody of each company's master key to named people (logged, revocable). If a restart of the store needs the super key and the Managing Partner cannot be reached, invoice signing stops. Consumer invoices must reach ZATCA within 24 hours, so a stop risks missing that deadline. The owner already accepted this availability risk in an earlier design (D-10).
- **Question:** 1. If you are unavailable, who may grant or renew delegations: (a) nobody (today's default), (b) a named deputy, or (c) each company's CEO for their own company only? 2. Should a delegation have a maximum length (for example 12 months), or run until revoked? 3. May the super key itself be delegated, or only each company's custody? 4. Partly owned and minority companies: are their master keys also under the Almiras super key, or does each hold its own top key?
- **What the system does now:** Managing Partner only. No expiry unless an end date is set. No super-key delegation. Every company with a ZATCA profile, partly owned ones included, sits under the group super key.
- **Reference (for the team):** zatca OQ-25, OQ-34 (b), (c), (e); zatca §3.9, §3.10, R-41, R-47, R-50; access OQ-3. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q10. Who may onboard ZATCA e-invoicing devices
- **Who should answer:** Managing Partner
- **Priority:** Blocks go-live (it contradicts an earlier approved decision)
- **Context:** Connecting the ERP to ZATCA (the tax authority's e-invoicing system) needs certificates per invoicing device. An earlier ZATCA decision (zatca D-9) said "superuser only for onboarding". The access design gives it instead to a ZATCA admin holding a key delegation from the MP, and superusers (technical administrators) get no business rights.
- **Question:** Which applies: (a) ZATCA admin with key delegation, and a technical administrator who onboards gets a ZATCA admin membership, not superuser rights (team default, as built); or (b) superuser only, as in zatca D-9?
- **What the system does now:** (a): onboarding needs the ZATCA admin right and an active key delegation.
- **Reference (for the team):** access OQ-13; access R24, OQ-13, OQ-25; zatca §7, D-9, R-41. (Access / web / ledger / migration)
- **Your answer:** 

### Q11. ZATCA go-live dates and which companies go live in which wave
- **Who should answer:** Owner (Managing Partner), with the tax adviser for ZATCA's wave notices
- **Priority:** Blocks go-live
- **Context:** ZATCA assigns each taxpayer a Phase 2 "wave" date. Before it, invoices only need the simpler Phase 1 QR code. A company that has just registered for VAT must wait 2 business days before it can connect. The system is ready to connect, but it sends nothing to ZATCA for a company until that company's go-live date is entered.
- **Question:** 1. For each VAT-registered group company, what is its ZATCA Phase 2 wave and go-live date? 2. Until then, do you agree that the system issues Phase 1 invoices (QR code only, nothing sent to ZATCA)?
- **What the system does now:** Go-live date empty, so nothing is sent and the invoicing module prints the Phase 1 QR code (default approved, D-9). Almiras is not live yet.
- **Reference (for the team):** zatca OQ-1; zatca D-1, D-9, R-1 to R-3 (go-live gate), §9 "Before go-live". (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q12. ZATCA simulation test with the owner's own Fatoora portal login
- **Who should answer:** Owner (Managing Partner), who holds the Fatoora portal login; IT / security lead to supervise
- **Priority:** Blocks go-live
- **Context:** ZATCA has three environments. (1) The public **developer portal** is free and needs no login. The team has already tested there, and every document type passed. (2) **Simulation** is a rehearsal of the real system that uses the company's real Fatoora portal login and a one-time password (OTP). (3) **Production** is the live system. The developer portal cannot settle three things: whether ZATCA wants a prepayment invoice (type 386) among the test invoices at onboarding; which rule version the live servers run; and the exact simulation and production web addresses, which are configured but have never been called. Only a simulation run settles them. Real logins and passwords must never be given to the AI agents or stored in the code.
- **Question:** 1. Will you (or a person you name) log in to the Fatoora portal for Almiras, create a dedicated **test** EGS unit in the simulation environment (an EGS unit is one invoicing device or system registered with ZATCA), and generate the OTP while the IT lead runs the onboarding? 2. Agents would receive only the non-secret results (pass/fail messages, the test certificate's public part). Do you agree? 3. When should this happen? It is needed before the production onboarding.
- **What the system does now:** Only the developer portal is used. The "simulation" option exists but cannot be selected. Its web address is ZATCA's published value, marked unverified. Production onboarding uses the same code but has not been run.
- **Reference (for the team):** zatca OQ-36, OQ-26, OQ-29, OQ-30 (remainder); sandbox report §6; zatca R-8, R-39, §3.2 (template names), §6 (base URLs), OQ-22 note ("not re-run in simulation"). (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q13. Amount used to choose who approves a contract
- **Who should answer:** Owner (Managing Partner)
- **Priority:** Needed before go-live (setting up each company's authority matrix)
- **Context:** Each company's authority matrix decides who approves by amount (for example up to SAR 1,000,000 the CFO; above that also the Managing Partner). For a contract, "the amount" must be defined.
- **Question:** For contracts: (a) total value over the whole term, 12 months if it has no end date (default), or (b) the yearly value, or (c) the monthly value? For cost-pool periods: the total billed (default)?
- **What the system does now:** Default (a), and the total billed for pool periods.
- **Reference (for the team):** billing OQ-45 = access OQ-35 (same question); billing R79, AT-64; access OQ-35. (Billing / tax / banking)
- **Your answer:** 

### Q14. Automatic draft bills in the buying company (mirror bills)
- **Who should answer:** Owner; then CFO
- **Priority:** Needed before go-live (confirm the default; mirror bills are not built yet)
- **Context:** The owner decided (A8) that when one group company invoices another, the buyer automatically gets a draft supplier bill. The buyer's staff then check and submit it. Some details were not decided.
- **Question:** Confirm the defaults or change them:
  1. Mirror for (a) every intercompany sales invoice (default), or (b) only those produced by contract billing?
  2. May the buyer change copied amounts? (a) No, only reject with a reason (default); (b) yes.
  3. For partly owned or minority-owned buyers: (a) create mirrors when the buyer is on the ERP (default); (b) never.
  4. Create the mirror (a) only after ZATCA has cleared the seller's invoice (default); (b) as soon as it is issued.
- **What the system does now:** Not built; the defaults above are in the spec.
- **Reference (for the team):** billing OQ-39; billing R73, R80, AT-57, AT-58. (Billing / tax / banking)
- **Your answer:** 

### Q15. Employee loans: limits and terms (confirm the default)
- **Who should answer:** Managing Partner; then CFO
- **Priority:** Needed before the first employee loan
- **Context:** Decided: profit-free loans up to 3 basic salaries, approved by the Managing Partner in Almiras (others per their matrix); larger amounts by bai' ajel. The detail is open.
- **Question:** 1. Is the 3-salary limit per loan, or on everything the employee owes (including salary advances)? (Default: on the total outstanding, including advances.)
  2. Basic salary at which date? (Default: at approval.)
  3. Who may borrow: employees on probation, remuneration staff, local-company employees? (Default: Saudi-company employees only, probation not excluded.)
  4. Maximum term? (Default: none.)
  5. Is bai' ajel only for amounts above the loan limit? (Default: no restriction.)
- **What the system does now:** The defaults above; write-off is possible with an approval (account: see "Payroll account choices").
- **Reference (for the team):** payroll OQ-32 and OQ-7 (remaining parts); payroll R27, R28, R46, OQ-7, OQ-32 (Payroll / HR)
- **Your answer:** 

### Q16. GOSI maternity cases: the legal escalation contact
- **Who should answer:** Managing Partner
- **Priority:** Needed before the first maternity leave
- **Context:** Decided: payroll checks GOSI maternity eligibility, never treats the employee as unpaid, pays a top-up to full pay, pays full salary if GOSI refuses, and escalates pending cases to HR and legal. There is no "legal" role in the system.
- **Question:** 1. Who receives the escalation of pending maternity cases (a named legal adviser, the HR manager, the Managing Partner)?
  2. Who may record the written legal clearance that allows full salary next to GOSI compensation, and who approves it?
- **What the system does now:** Escalation to the company's HR officers and the Managing Partner; the HR officer enters the clearance reference and the Managing Partner approves it.
- **Reference (for the team):** hr OQ-44; payroll OQ-46 (remaining part); payroll R59, R61, OQ-46; hr 3.7, OQ-44 (Payroll / HR)
- **Your answer:** 

### Q17. Invoices in 3-decimal currencies with computed amounts
- **Who should answer:** Owner; then CFO
- **Priority:** Needed before the first contract in KWD, BHD, OMR or JOD
- **Context:** Prices in these currencies end in 0 (owner A8). A part-month, a VAT amount or a correction can still produce a non-zero third decimal, which ZATCA's format may not accept. Such invoices are held.
- **Question:** For computed amounts with a non-zero third decimal: (a) hold the invoice until ZATCA confirms 3-decimal invoices (default), or (b) round to 2 decimals (ending in 0)?
- **What the system does now:** (a): the period is held with a bilingual reason; a person releases it later.
- **Reference (for the team):** billing OQ-35 (prices answered by D13(f): prices end in 0); billing R37, R54, R72, AT-45.

---

## Priority: Low (default is safe) (Billing / tax / banking)
- **Your answer:** 

### Q18. Remuneration staff: proof of working abroad, time in Saudi Arabia, terms
- **Who should answer:** Managing Partner; then legal adviser (labour law, PDPL)
- **Priority:** Needed before the first out-of-country payroll
- **Context:** The owner decided the Saudi Labour Law does not apply to people working outside Saudi Arabia (P16), so remuneration staff get no EOSB, Saudi leave or WPS. The system needs a test for "works abroad", and a rule for when such a person works in Saudi Arabia for a while.
- **Question:** 1. Is "country of residence is not Saudi Arabia" enough evidence, or must a work country (and proof) be recorded?
  2. If a remuneration person works in Saudi Arabia for a period, what happens: (a) nothing; (b) after a set number of days they must become a Saudi employee (new contract); (c) case by case?
  3. Notice and termination terms, and a standard remuneration agreement template: will legal supply one?
- **What the system does now:** Residence outside Saudi Arabia required; notice per the agreement, not checked; a change of basis is a termination plus a new employee record.
- **Reference (for the team):** hr OQ-28 (remaining parts); hr R52, R53, OQ-28; payroll R41, R42 (Payroll / HR)
- **Your answer:** 

### Q19. Three different people for WHT, Zakat and income tax returns too
- **Who should answer:** Owner (Managing Partner)
- **Priority:** Needed before the first WHT or Zakat return (and before any small company files with one accountant)
- **Context:** The owner decided VAT returns always need three different people (preparer, reviewer, approver), with no self-approval (G3). A small company with one accountant will need people from outside it to review and approve.
- **Question:** 1. Does the same three-person rule apply to WHT, Zakat and income tax returns? (Default: yes.)
  2. Tax payments need two people (one prepares, another approves). Confirm.
  3. In a company with only one accountant, who should act as reviewer (for example a group accountant given access to that company)?
- **What the system does now:** Three people for VAT returns; two for tax payments; no exception.
- **Reference (for the team):** tax-returns OQ-7 (answered for VAT returns by G3); tax-returns R8, §10.1 segregation. (Billing / tax / banking)
- **Your answer:** 

### Q20. Which Falcon company is which ERP company
- **Who should answer:** Managing Partner (project owner)
- **Priority:** Needed before loading opening balances
- **Context:** The loader expects one export file per company.
- **Question:** Confirm one Falcon company per file, and give the list Falcon company name → ERP company.
- **What the system does now:** One company per file.
- **Reference (for the team):** migration OQ-9; migration OQ-9, §10.

---

## Low (default is safe) (Access / web / ledger / migration)
- **Your answer:** 

### Q21. Which companies may sell assets to employees by bai' ajel
- **Who should answer:** Project owner (Managing Partner)
- **Priority:** Needed before the first bai' ajel sale
- **Context:** A bai' ajel sale is created by a system user, and that user must be switched on separately for each company.
- **Question:** Which companies sell assets to employees by bai' ajel (Almiras only, or other companies too)?
- **What the system does now:** No company is switched on, so every sale is refused.
- **Reference (for the team):** invoicing OQ-76; invoicing R123. (Invoicing)
- **Your answer:** 

## CFO

### Q22. Cut-over date per company
- **Who should answer:** CFO; then Managing Partner
- **Priority:** Blocks go-live (needed before loading opening balances)
- **Context:** The cut-over date is the last day in Falcon; opening balances in the new ERP are taken at that date. The guide suggests 31 December 2026 but nothing is decided.
- **Question:** What is the cut-over date for each company (Almiras and each subsidiary)? Is it the same for all?
- **What the system does now:** No default; the date must be given every time the loader runs.
- **Reference (for the team):** migration OQ-2; migration R14, §10 runbook, workbook sheet "Entities", OQ-2. (Access / web / ledger / migration)
- **Your answer:** 

### Q23. New accounts for fixed assets (and renaming account 1510)
- **Who should answer:** CFO (with the accountant)
- **Priority:** Blocks go-live (real impairments, write-offs and work in progress need 4800, 5420, 1550, 1595 and 1596)
- **Context:** The current chart of accounts has only 1500 PPE, 1510 Investment property, 1520 Vehicles, 1590 Accumulated depreciation and 5400 Depreciation. The module proposes: 1530 Furniture, fixtures and office equipment; 1540 Computer and IT equipment; 1550 Capital work in progress; 1560 Land (own use); 1570 Buildings (own use); 1591 Accumulated depreciation – investment property; 1595 Accumulated impairment – PPE; 1596 Accumulated impairment – investment property; 4800 Net gain (loss) on disposal of fixed assets; 5420 Impairment losses (reversals). 3500 Revaluation surplus and 4810 Fair-value gain (loss) on investment property are reserved, not used. "Restricted" means only bills, fixed-asset documents and the migration opening entry may post to an account, never a manual journal.
- **Question:** 1. Approve the accounts above, or change them. 2. Do you want separate gain (48xx) and loss (54xx) accounts on disposal, or one net account 4800? 3. Confirm that cost and accumulated-depreciation accounts are **restricted** as described. 4. Rename 1510 from "Investment property (rental assets)" to "Investment property (land and buildings) / العقارات الاستثمارية (أراضٍ ومبانٍ)", so that rental cars are not booked there by mistake? 5. Add a RETT expense account for property sales (see the RETT entry)?
- **What the system does now:** The proposed codes exist in test data only. The real chart is unchanged and the codes are pre-registered as restricted, so nothing changes until you approve.
- **Reference (for the team):** fixed-assets OQ-5 (compliance S5, B4); fixed-assets R9, R45, R49, §5 table, D7. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q24. Useful lives, depreciation rates and residual values per asset category
- **Who should answer:** CFO (approved through the approval engine under activity `fixed_assets.policy`)
- **Priority:** Blocks go-live (no real asset can be capitalised until its category's policy is approved)
- **Context:** Depreciation spreads an asset's cost over its useful life. The system holds lives and rates as approved policy data per category (for example vehicles, IT equipment, furniture, buildings, investment property). Tests use made-up values only.
- **Question:** 1. For each category: useful life in months, method (straight line, which is the default, or reducing balance with its rate), and residual value (% of cost or a fixed amount). 2. Should these be (a) one set for the whole group or (b) set per company? 3. Who prepares the policy and who approves it in each company?
- **What the system does now:** No real policy exists. Submitting an asset in a depreciable category without an approved policy is refused.
- **Reference (for the team):** fixed-assets OQ-2; fixed-assets R14, R16, §3.2. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q25. 3-decimal currencies (KWD, BHD, OMR, JOD): computed amounts with a third decimal
- **Who should answer:** CFO (or project owner)
- **Priority:** Needed before the first invoice in KWD, BHD, OMR or JOD. **Confirm the default.**
- **Context:** ZATCA has now confirmed that e-invoices carry 2 decimals for every currency, and prices in these currencies already end in 0. Some amounts can still end with a third decimal other than 0, because the system computes them: VAT (15% of KWD 10.010 = 1.5015, which rounds to 1.502), part-month amounts, and quantities with decimals.
- **Question:** (a) Hold such a document; staff adjust the price or quantity until every amount ends in 0 (current). (b) Round these amounts to 2 decimals for these currencies.
- **What the system does now:** Option (a). Approval is refused with the code `third_decimal_pending`.
- **Reference (for the team):** invoicing OQ-62 (remainder); zatca OQ-35; invoicing R93, R109; zatca R-51. (Invoicing)
- **Your answer:** 

### Q26. Approving a return while some figures are "unverified"
- **Who should answer:** CFO
- **Priority:** Needed before the first VAT return (confirm the default)
- **Context:** Until the tax adviser confirms the box definitions and rates, the return shows "unverified" next to them (see "Official VAT return form" above).
- **Question:** May a return be approved while that mark is shown? (a) Yes, with the mark (default); (b) no, block approval until verified.
- **What the system does now:** Allowed, with "unverified / غير متحقق منه" printed.
- **Reference (for the team):** tax-returns OQ-6; tax-returns R6, AT-6.

---

## Priority: Needed before another specific event (Billing / tax / banking)
- **Your answer:** 

### Q27. Bai' ajel profit: recognised as instalments are paid, or evenly over time
- **Who should answer:** CFO; then Sharia adviser
- **Priority:** Needed before the first bai' ajel sale. **Confirm the default.**
- **Context:** The profit on a bai' ajel sale is earned over the term. Following the owner's instruction of 2026-10-20, the ERP recognises it as instalments are collected. The payroll spec still describes even monthly recognition. Both give the same figures (for example SAR 150 a month) unless an instalment is reduced, skipped or left unpaid when an employee leaves.
- **Question:** Confirm: (a) profit is recognised as instalments are collected (current); or (b) it is recognised evenly each month whatever is collected.
- **What the system does now:** Option (a).
- **Reference (for the team):** invoicing OQ-75 (linked payroll E12, E13, OQ-33); invoicing R122; payroll E12, E13. (Invoicing)
- **Your answer:** 

### Q28. Banking intercompany transfers on the intercompany control accounts
- **Who should answer:** CFO
- **Priority:** Needed before go-live (confirm the default)
- **Context:** The CFO approved "policy A": the accounts for receivables, payables and intercompany balances (1200, 2100, 1210, 2110, 2310, 2320) "can only be posted by the invoicing module, never by manual journal". But when one group company sends cash to another (for example a group payment or settlement), the bank module records it on 1210 or 2110. (Group loans on 1220 are not affected.)
- **Question:** Does "only by the invoicing module" mean:
  (a) never by a manual journal, so the banking module's own approved transfer document is allowed (current); or
  (b) strictly invoicing documents only, so every intercompany cash transfer must be entered as an invoicing document?

  *Also raised in the invoicing list:* Which reading did you mean? (a) "Never by manual journal": named system documents of other modules (banking intercompany transfers) may post there (current). (b) "Invoicing documents only": every intercompany cash transfer must be entered as an invoicing receipt or payment.
- **What the system does now:** (a). The banking intercompany transfer is on the allow-list for 1210 and 2110; manual journals stay refused.
- **Reference (for the team):** banking OQ-27 = invoicing OQ-66 (the migration part of invoicing OQ-66 is answered by owner answer B2); also invoicing OQ-66 (remainder) = banking OQ-27; banking R32 note, B11(b); invoicing R97, AT-115. (Billing / tax / banking)
- **Your answer:** 

### Q29. Benefits that may need accruals: bonuses, air tickets, medical insurance
- **Who should answer:** CFO; then accountant
- **Priority:** Needed before the first annual financial statements
- **Context:** Some benefits are earned over the year but paid later (annual air ticket, annual bonus). Without an accrual, cost is booked only when paid.
- **Question:** Which benefits does each company give (annual air ticket, bonus, commission, medical insurance, others), and should any be accrued monthly? (Default: all are one-off inputs when paid, no accruals.)
- **What the system does now:** One-off inputs, no accruals.
- **Reference (for the team):** payroll OQ-19; payroll R19, OQ-19 (Payroll / HR)
- **Your answer:** 

### Q30. Capitalisation threshold
- **Who should answer:** CFO
- **Priority:** Needed before the first real asset is registered
- **Context:** Many companies expense small purchases (for example anything under SAR 2,000) instead of recording them as assets.
- **Question:** 1. Is there a minimum amount for capitalising an item? 2. Is it measured per item or per invoice? 3. Is it one amount for the whole group or set per company?
- **What the system does now:** No threshold. Any bill line booked to an asset account becomes an asset.
- **Reference (for the team):** fixed-assets OQ-1; fixed-assets R3. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q31. Charity (purification) payment returned by the bank
- **Who should answer:** CFO (with the accountant)
- **Priority:** Needed before the first purification payout
- **Context:** Once a payout instruction is marked "exported" or "sent", the payout can no longer be reversed. A bank sometimes returns a payment, for example because of a wrong IBAN.
- **Question:** 1. How is a returned or failed charity payment booked: (a) back to the charity payable, to be paid again; (b) to the same charity once the details are corrected; (c) another way?
  2. Is matching payouts to the bank statement needed now, or later with the bank statement formats?
- **What the system does now:** No booking exists for a returned payout. Matching waits for the bank statement formats (E3).
- **Reference (for the team):** invoicing OQ-64 (follow-up of (d)); linked banking OQ-2; invoicing R54 (amendment of 2026-10-18), D34 C4. (Invoicing)
- **Your answer:** 

### Q32. Correcting a wrong revaluation, and who may reverse it
- **Who should answer:** CFO
- **Priority:** Needed before the first month-end FX revaluation
- **Context:** If a month-end revaluation was run with a wrong rate, it cannot be fixed with a hand-written journal (control accounts accept no manual journals). The team's default: the run is reversed as a whole (two correcting entries) while that month and the next are still open, and then run again at the right rate. A revaluation is prepared by one person and posted by a different person.
- **Question:** 1. Confirm the default: a wrong revaluation is corrected by reversing the whole run and running it again (yes / no). 2. May the person who prepared or posted the revaluation also reverse it: (a) yes, anyone with the right to reverse entries (as built today); (b) no, the reverser must differ from both the preparer and the poster; (c) the reversal must itself be approved by a second person?

  *Also raised in the invoicing list:* 1. Confirm that a wrong revaluation is corrected by reversing the whole run and running it again at the right rate while the month is still open.
  2. Must the person who reverses the run be different from the person who ran or posted it?
- **What the system does now:** Reverse-and-rerun is built. Any person with the "reverse journal entries" right, which accountants hold, may reverse a run, including the person who prepared it.
- **Reference (for the team):** ledger-fx OQ-9; also invoicing R97 amendment of 2026-10-22, point (2) = ledger-fx OQ-9; ledger-fx R24 (amended 2026-10-22), R26, AT-31, OQ-9; invoicing R97. (Access / web / ledger / migration)
- **Your answer:** 

### Q33. Cost pools: shared costs recharged to group companies
- **Who should answer:** CFO; tax adviser for transfer pricing and VAT
- **Priority:** Needed before go-live (the owner said cost pools are needed at go-live, D14(c))
- **Context:** A "cost pool" collects shared costs in one company (for example shared finance services) and bills a share to other group companies each month, optionally with a markup. The share can be a fixed percentage, by headcount or by revenue.
- **Question:** 1. Which pools does each company need, and which expense accounts go into each (for example: include depreciation? include payroll)?
  2. Which allocation key for each pool: fixed %, headcount, or revenue (and if revenue: which revenue, at which date)?
  3. Tax adviser: do these recharges and markups raise VAT or transfer-pricing points to handle?
- **What the system does now:** Not built. Default: keys as described; drivers entered each month; all posted expense accounts listed on the pool are used; 15% VAT.
- **Reference (for the team):** billing OQ-43; billing R75, R76, §5 B15, AT-60. (Billing / tax / banking)
- **Your answer:** 

### Q34. Cost-based secondment recharges: details
- **Who should answer:** CFO for (b) and (c); tax adviser for (d)
- **Priority:** Needed before the first secondment billed at actual cost or cost plus markup
- **Context:** The owner decided (A4) that a seconded employee can be recharged at actual fully loaded cost (pay, benefits, government fees) or cost plus a markup. Some details are open.
- **Question:** 1. Yearly costs such as iqama renewal or a medical insurance premium: recharge (a) in the month the cost is recorded (default), or (b) spread over the months it covers?
  2. Which costs count beyond payroll's cost report (air tickets, housing provided in kind, training)? Default: only what payroll records against the employee.
  3. Tax adviser: does the markup need transfer-pricing support, and does it affect Zakat?
- **What the system does now:** Defaults (1)(a) and (2); nothing in the ERP for (3). Cost-based lines are refused until payroll's cost report exists.
- **Reference (for the team):** billing OQ-37 (b), (c), (d) ((a) resolved by D14(a)); billing R71, R76, R68(d), AT-54. (Billing / tax / banking)
- **Your answer:** 

### Q35. Credit notes on income billed in advance
- **Who should answer:** CFO
- **Priority:** Needed before the first contract billed quarterly or yearly in advance
- **Context:** If such a contract is cut short, part of the income may still be in deferred income and part already recognised.
- **Question:** Is the credit taken (a) first from the part not yet released, then from income (default), or (b) in proportion?
- **What the system does now:** Not built; default (a).
- **Reference (for the team):** billing OQ-38; billing R70, invoicing R99. (Billing / tax / banking)
- **Your answer:** 

### Q36. Deferred income account 2310: is it a protected control account
- **Who should answer:** CFO
- **Priority:** Needed before quarterly or annual in-advance billing goes live
- **Context:** See "Answered elsewhere" below. The CFO already approved 2310 as a control account in the decision pack, so no new question is needed. The account is not in the chart yet; when it is added it will carry the flag.
- **Question:** None, unless the CFO wants to change the approved policy.
- **What the system does now:** 2310 is not in the chart. When it is added it will carry the control flag, so only invoicing documents (including the monthly deferred-income release) and the migration opening entry can post there.
- **Reference (for the team):** invoicing OQ-81; invoicing R97, R99.

---

## Low (default is safe) (Invoicing)
- **Your answer:** 

### Q37. Depreciation in the month an asset starts or leaves
- **Who should answer:** CFO (with the accountant)
- **Priority:** Needed before the first real depreciation run
- **Context:** An asset put into use on the 20th of a month can be depreciated by days, or by whole months from that month or the next. The same applies in the month it is sold.
- **Question:** Choose: (a) by days at both ends (default); (b) a full month from the month of use, none in the disposal month; (c) from the month after use, full month in the disposal month.
- **What the system does now:** (a) by days at both ends.
- **Reference (for the team):** fixed-assets OQ-3; fixed-assets R11, R25. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q38. Group-only accounts for consolidation
- **Who should answer:** CFO (with the accountant)
- **Priority:** Needed before the first group consolidation run
- **Context:** Consolidation needs three accounts that exist only in the group accounts and are never posted in any company's books: 1610 Goodwill, 3600 Non-controlling interests, 3700 Reserve for transactions with non-controlling interests.
- **Question:** Approve 1610, 3600 and 3700 as group-only ("report only") accounts, or change them.
- **What the system does now:** Used in test data only. The chart is unchanged.
- **Reference (for the team):** consolidation OQ-6; consolidation §5 table, R24, R27. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q39. Make the salaries, GOSI and loans accounts system-only
- **Who should answer:** CFO
- **Priority:** Needed before first payroll
- **Context:** The CFO approved the payroll accounts (P19(g)). Receivables and payables (1200, 2100, etc.) are already "control accounts": only their module may post to them, never a manual journal, so the module's detail always agrees with the ledger. The same question for payroll was not asked.
- **Question:** Should 1230 Employee loans and advances, 2330 Salaries payable and 2340 GOSI payable be control accounts (posted only by payroll, never by manual journal, except the one-time opening-balance migration)? (a) Yes, all three (team recommendation); (b) only some; (c) no.
- **What the system does now:** The spec default is "yes", but the code does not enforce it yet: today a manual journal can post to 1230, 2330 and 2340.
- **Reference (for the team):** payroll OQ-12(b); payroll §5, OQ-12; ledger `control_accounts`; CFO pack 1c item A (Payroll / HR)
- **Your answer:** 

### Q40. Pay day, and the out-of-country run and period close
- **Who should answer:** CFO; then Managing Partner to confirm
- **Priority:** Needed before first payroll
- **Context:** All payroll is monthly (P10). Inputs close on the 27th for staff in Saudi Arabia and local companies, and on the 6th of the next month for out-of-country (remuneration) staff. The pay day is not decided.
- **Question:** 1. Confirm the default pay day of the 27th of each month for in-country staff, or name another day per company.
  2. Out-of-country staff: confirm they are paid on the same pay day of the following month (because their inputs close on the 6th).
  3. Confirm "in-country" means Saudi employees plus the local companies' employees, and "out-of-country" means remuneration staff.
  4. May the accounting month be closed before the out-of-country run for that month is approved? (Default: no, closing is refused while any run of the month is not approved.)
- **What the system does now:** Pay day 27; out-of-country on the 27th of the next month; month close refused while a run is pending.
- **Reference (for the team):** payroll OQ-1 and OQ-31; payroll R44, 3.1, OQ-1, OQ-31 (Payroll / HR)
- **Your answer:** 

### Q41. Reversed opening entry: a reload replaces it
- **Who should answer:** CFO; then external auditor
- **Priority:** Needed before loading opening balances
- **Context:** If a posted opening entry turns out wrong, it can be reversed (cancelled by an equal opposite entry). The team's default: a new, corrected load then replaces it, numbered FALCON-OPENING-2, -3 and so on; the old one stays on record marked "superseded", and the new one again needs the second-person approval and the tie-out checks. Only one opening load per company is "live" at a time.
- **Question:** Confirm the default: a reversed opening entry is replaced by a new versioned load (yes / no). Any limit, e.g. reload allowed only before the first VAT return or before the first month is closed?
- **What the system does now:** As the default; reversing the reversal is refused.
- **Reference (for the team):** migration R14 team default (2026-10-23); migration R14 (amended 2026-10-23), R16, R22, R25; invoicing R97. (Access / web / ledger / migration)
- **Your answer:** 

### Q42. Settling an old (migrated) foreign-currency invoice: who approves, and when it posts
- **Who should answer:** CFO (with the accountant)
- **Priority:** Needed before the first receipt or payment against a foreign-currency invoice or bill brought over from Falcon (just after the cut-over). **Confirm the default.**
- **Context:** Unpaid invoices and bills brought over from Falcon are "migrated open items". When such an item is in foreign currency (for example USD), its settlement creates an exchange gain or loss, because the SAR value at payment differs from the SAR value at cut-over. The system books it as a separate settlement that someone must approve.
- **Question:** 1. Is the CFO the right default approver for these settlements? (The Managing Partner and the CEO may also be named in each company's authority matrix.)
  2. Should the settlement instead post together with the receipt or payment itself, with no separate approval, the same way as for invoices created in the ERP?
- **What the system does now:** It creates a separate pending settlement that goes to the approval inbox. The default approver is the CFO; the person who prepared it cannot approve it. The entry is dated on the approval day and the exchange difference is booked to 4700 (gain) or 5750 (loss).
- **Reference (for the team):** invoicing OQ-79; invoicing R129 (b), (e), (f); R76; migration R23. (Invoicing)
- **Your answer:** 

### Q43. Sharing a group Zakat bill among the companies
- **Who should answer:** CFO
- **Priority:** Needed before the first combined Zakat return (only if one is filed)
- **Context:** If the group files one combined Zakat return, the parent pays it.
- **Question:** Should the parent (a) bear it all (default), or (b) recharge each subsidiary its share through the intercompany accounts?
- **What the system does now:** Not built; default (a).
- **Reference (for the team):** tax-returns OQ-23; tax-returns R44. (Billing / tax / banking)
- **Your answer:** 

### Q44. Should the tax accounts 1310, 2200 and 2220 also refuse manual journals?
- **Who should answer:** CFO (with the accountant)
- **Priority:** Needed before the first VAT return prepared in the ERP
- **Context:** 1310 is VAT we can claim back, 2200 is VAT we owe, and 2220 is withholding tax we owe. The control-account protection now blocks manual journals on the receivable and payable accounts only. The build brief also asked for the tax accounts, but other parts of the system rely on manual journals there. The VAT-return module expects some manual VAT journals, which it flags for review. A filed VAT return is corrected with a correcting journal on these accounts. Journals replayed from Falcon carry VAT lines.
- **Question:** Choose one: (a) Keep allowing manual journals on 1310 / 2200 / 2220. Every one is flagged and must be explained in the VAT return review (current). (b) Refuse manual journals there, except the migration opening entry and an approved VAT-return correction. Replayed Falcon journals with VAT lines would then be refused. (c) Allow them only with CFO approval through the approval engine.
- **What the system does now:** Option (a). Manual journals are allowed and flagged as `untagged_vat_journal` in the VAT return review.
- **Reference (for the team):** invoicing R97 note of 2026-10-22 (no OQ number; linked: tax-returns R16(a), R26, R27; migration R8); invoicing R97; tax-returns R16(a), R26, R27; migration R8. (Invoicing)
- **Your answer:** 

### Q45. Tax penalties account 5940
- **Who should answer:** CFO (code and names); then tax adviser (Zakat / income tax)
- **Priority:** Needed before the first tax penalty is booked; the deductibility point before the first annual Zakat return
- **Context:** The owner decided that tax penalties get their own account, separate from GOSI fines (B3). The team proposed the code and the names.
- **Question:** 1. Approve account 5940 "Tax penalties / غرامات ضريبية", or give another code or name.
  2. For Zakat and income tax: are these penalties deductible, or added back?
- **What the system does now:** 5940 is proposed as shown and deductibility is left open.
- **Reference (for the team):** invoicing OQ-70; invoicing §5 (5940), D33(f). (Invoicing)
- **Your answer:** 

### Q46. Tax penalties: their own account, and the income tax accounts
- **Who should answer:** CFO
- **Priority:** Needed before the first tax penalty is booked (and before income tax is built)
- **Context:** The owner decided (B3) that tax penalties get their own account, separate from 5930 "GOSI late-payment fines", but no code or name was given. Penalties are fines, never interest. Two income tax accounts (2250 Income tax payable, 5810 Income tax expense) were never in the CFO's approval pack.
- **Question:** 1. Approve a new expense account for VAT, WHT, Zakat and income tax penalties: proposed name "Tax penalties / غرامات ضريبية" (the team proposes a free code).
  2. Where is the unpaid penalty held until paid: (a) 2300 Accrued expenses (default), or (b) a new "tax penalties payable" account?
  3. Approve 2250 Income tax payable and 5810 Income tax expense.
- **What the system does now:** Tax penalties are not built; 2250 and 5810 stay "proposed".
- **Reference (for the team):** tax-returns OQ-31 (first part answered by B3), OQ-42, OQ-28; proposed accounts 2250 and 5810; tax-returns R52, §5 T9, T10, §10 (penalties out of scope). (Billing / tax / banking)
- **Your answer:** 

### Q47. Who posts the monthly release of income billed in advance
- **Who should answer:** CFO
- **Priority:** Needed before the first contract billed quarterly or yearly in advance
- **Context:** The owner decided (A3) that income billed for a quarter or a year in advance is spread month by month through "Deferred income" (2310). Each month an entry moves that month's share to income.
- **Question:** Should that monthly entry be (a) drafted by the system and approved by the CFO each month (default), or (b) posted automatically, covered by the contract approval (like the invoices themselves)?
- **What the system does now:** Not built; default (a).
- **Reference (for the team):** billing OQ-36; billing R70, §7 (D13 note); invoicing R99. (Billing / tax / banking)
- **Your answer:** 

## Accountant

### Q48. Group consolidation: buying, selling or changing the ownership of a company during the year
- **Who should answer:** Accountant; then the external auditor confirms
- **Priority:** Blocks build (for any consolidation period in which a company joins, leaves or changes method)
- **Context:** When the group gains control of a company, loses control, or an associate becomes a subsidiary, the accounts need a purchase price allocation, goodwill (proposed account 1610), a measure of the minority shareholders' share (non-controlling interest, NCI), and a gain or loss on disposal. Today only a change in a stake without a change of control (for example 80% to 90%), effective on the first day of a month, is handled.
- **Question:** 1. How is the purchase price allocated and goodwill measured? Who provides the fair values? 2. NCI: measured (a) at its share of net assets or (b) at fair value? Option (b) exists only under full IFRS. 3. A bargain purchase (price below fair value): how is it treated? 4. Is a company included from the exact date of the deal, or from the next month start? 5. On loss of control: how are the disposal gain or loss and the release of the translation reserve worked out?
- **What the system does now:** A consolidation run that contains such an event is refused, with a message citing OQ-5.
- **Reference (for the team):** consolidation OQ-5; consolidation R20, R24, R27, AT-19.

---

## Blocks go-live (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q49. Group consolidation: translating companies that keep their books in another currency (e.g. Egypt)
- **Who should answer:** Accountant; then the external auditor confirms
- **Priority:** Blocks build (consolidating any member whose own currency is not SAR, e.g. the Egyptian company; SAR-only groups are not blocked)
- **Context:** The group reports in SAR. A company that keeps its books in, for example, Egyptian pounds must be converted into SAR for the group accounts. The usual method (IAS 21 / IFRS for SMEs section 30) converts the balance sheet at the closing rate, income and expenses at average rates, and equity at historical rates. The difference goes to a "translation reserve" (account 3400). Egypt may also need to be checked for very high inflation (hyperinflation rules, IAS 29 / section 31).
- **Question:** 1. Average rates: (a) the monthly average, (b) the average for the whole period, or (c) the rate on each transaction date? 2. Where do historical rates for share capital and opening equity come from? 3. For the first group consolidation, what are the opening translated balances? 4. When a foreign company is sold, is the accumulated translation reserve released to profit or loss? 5. Does Egypt count as hyperinflationary for our reporting years? 6. (consolidation OQ-13) A small SAR difference can be left over when two group companies record the same foreign-currency balance. Should it go to (a) the translation reserve 3400 or (b) the group's exchange gain/loss accounts 4710 / 5760?

  *Also raised in the access-web-ledger-migration list:* 1. Income and expenses: convert at (a) the monthly average rate, (b) the average for the whole period, or (c) each transaction's own rate? 2. Equity (capital, reserves): which historical rates, and where do they come from? 3. How is goodwill on acquired subsidiaries converted? 4. When a subsidiary is sold, is its accumulated conversion difference (account 3400) moved to profit and loss? 5. Must the Egyptian company be assessed for hyperinflation (IAS 29), and who does that each year?
- **What the system does now:** A consolidation with a non-SAR member is refused, with a message citing ledger-fx OQ-5. Default for the leftover difference: 3400.
- **Reference (for the team):** consolidation OQ-2 = ledger-fx OQ-5 (related: consolidation OQ-13); also ledger-fx OQ-5; consolidation R15 to R17, AT-11; ledger-fx R30; reporting RD5. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q50. Sending documents back to the preparer with a reason
- **Who should answer:** Accountant; then Managing Partner (project owner) to approve
- **Priority:** Blocks build (the "send back" button for ten document types; approving them already works)
- **Context:** In the approval inbox an approver can approve, or send a document back with a reason. For ten types (supplier bills, supplier payments, receipts, assignments, terminations, statutory parameters, bank accounts, bank transactions, bank reconciliations and manual journals) there is no "send back" yet, so the approver can only approve.
- **Question:** 1. May the approver send these ten types back? 2. Back to which state: (a) draft, editable by the preparer (team proposal), or (b) another state? 3. Must the reason always be stored in the document's history? 4. Must the reason be written in Arabic (as today in billing and HR), or may an English-only user write it in English only?
- **What the system does now:** Approve only for these types. Elsewhere the reason in Arabic is required, English optional.
- **Reference (for the team):** web OQ-12; web R8, §4.1, OQ-12; access §10.10 (engine "return" purpose).

---

## Blocks go-live (Access / web / ledger / migration)
- **Your answer:** 

### Q51. Where the system gets exchange rates automatically
- **Who should answer:** Accountant; then IT / security lead (for the data feeds)
- **Priority:** Blocks build (only the automatic rate import; manual and spreadsheet entry already work)
- **Context:** The ERP needs daily exchange rates for SAR, USD, EUR, CNY, EGP, AED and others. For VAT in SAR, the SAMA (Saudi Central Bank) rate is required by law. Today the accountant types the rates in or uploads a spreadsheet. An automatic daily import can only be built once we know which free official sources to use for which currency.
- **Question:** 1. For each currency, which source is the main one: (a) SAMA for every currency against SAR; (b) SAMA for USD and the main currencies, the European Central Bank for others (it has no SAR, AED or EGP, so those would be derived through USD); (c) the home central bank for local currencies (Central Bank of Egypt for EGP, UAE Central Bank for AED, People's Bank of China for CNY)? 2. At what time of day should rates be imported? 3. If two sources disagree, which one wins? 4. May the IT lead confirm that each chosen source publishes a free machine-readable file and that its terms allow our use?
- **What the system does now:** No automatic import. Rates are entered by hand or uploaded as a spreadsheet; SAMA rows are marked as SAMA.
- **Reference (for the team):** ledger-fx OQ-2; ledger-fx R12, R13, §6, OQ-2, D6(e). (Access / web / ledger / migration)
- **Your answer:** 

### Q52. Bank balances and uncleared items at cut-over from Falcon
- **Who should answer:** Accountant (CFO to sign off the tie-out)
- **Priority:** Blocks go-live
- **Context:** On the day the ERP replaces Falcon, each bank account needs a starting point: the balance on the bank statement, plus any items not yet cleared (cheques written but not yet cashed, deposits not yet on the statement). These must agree with the opening trial balance, or the first reconciliation will not balance.
- **Question:** 1. Confirm the approach: (a) start each account from the bank statement balance on the cut-over date and load the uncleared items as open items (default); or (b) choose a cut-over date on which every account is fully reconciled, so there are no uncleared items.
  2. Which cut-over date is planned?
  3. Who will give the per-account statement balances and the list of uncleared items from Falcon (the team will provide a fill-in template)?
- **What the system does now:** Each bank account has an "opening statement balance" field. Outstanding items are loaded through migration as open book items.
- **Reference (for the team):** banking OQ-17 (owner answer D: "migration later", the team prepares templates); banking §3.2 (`opening_statement_balance`), R12, §6. (Billing / tax / banking)
- **Your answer:** 

### Q53. Falcon export formats (chart of accounts and trial balance)
- **Who should answer:** Accountant (who uses Falcon); Managing Partner to send the files
- **Priority:** Blocks go-live (needed before the first trial load with real data)
- **Context:** The Falcon journal export was received and is "close to the final one" (owner answer D). The chart-of-accounts and trial-balance exports have not been received. The owner's team can also type them into the fillable migration workbook instead.
- **Question:** 1. Will the chart and trial balance come (a) as Falcon exports, or (b) only through the fillable workbook? 2. If (a): please send one sample of each (synthetic or anonymised is fine), and say whether balances are in one signed column or separate debit / credit columns, and whether group (header) accounts appear with leaf accounts.
- **What the system does now:** Readers use a configurable column map whose defaults are guesses; the workbook path is built and tested.
- **Reference (for the team):** migration OQ-1; migration M1, M2, M6, R1 to R7, §11, OQ-1. (Access / web / ledger / migration)
- **Your answer:** 

### Q54. Saudi IBAN bank codes and foreign bank account details
- **Who should answer:** Accountant, with the bank relationship managers
- **Priority:** Blocks go-live (the bank-code check on IBANs)
- **Context:** When a bank account is set up, the ERP checks the IBAN. A Saudi IBAN has 24 characters, and two of them identify the bank. The system holds a guessed code and SWIFT (BIC) code for each of the five banks, marked "unverified", and only warns if they do not match. Foreign subsidiaries may have accounts in countries that do not use IBANs.
- **Question:** 1. Please confirm the 2-digit bank code inside the IBAN and the SWIFT/BIC code for Riyad Bank, SNB, Al Rajhi, Bank AlJazira and Alinma Bank (from a real IBAN of each bank, or the bank's letter).
  2. For accounts in countries without IBAN (for example China or the USA), which details should be stored: (a) account number only, (b) account number plus SWIFT, (c) account number plus local bank or routing code plus SWIFT?
- **What the system does now:** Every IBAN's check digits are always verified. The Saudi bank-code check uses the unverified codes and only warns.
- **Reference (for the team):** banking OQ-4; banking R3 **[CC-verify]**; banking §3.1, R3, AT-1. (Billing / tax / banking)
- **Your answer:** 

### Q55. Approving the financial statement layouts and account mapping
- **Who should answer:** Accountant; then the external auditor confirms
- **Priority:** Needed before the first final (signed) financial statements
- **Context:** The statement layouts (balance sheet, profit and loss, changes in equity, cash flow) are data: each account maps to a line. They must be approved before any statement can be marked final. Two points need specific confirmation. First, the purification bank account 1140 is restricted cash: it appears on its own line and is excluded from "cash and cash equivalents" (owner C6). Second, disposal gains and losses on 4800 are shown outside revenue (IAS 16.68).
- **Question:** 1. Approve the default layouts and account mapping in reporting §5 for IFRS for SMEs and for full IFRS, or mark changes. 2. Confirm the treatment of purification account 1140 as restricted cash, excluded from cash equivalents, with a note. 3. Confirm that 4800 is shown outside revenue.
- **What the system does now:** Only test layouts exist. A run cannot be finalised without approved layouts.
- **Reference (for the team):** reporting OQ-1 (plus [CC-verify] on restricted cash 1140, rule R15; compliance N8); reporting R6, R10, R15, R18, §5 table. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q56. Contracts already running in Falcon
- **Who should answer:** Accountant
- **Priority:** Needed before go-live (confirm the default)
- **Context:** Leases, car rentals and secondments already running must be moved into the ERP so that automatic billing continues without billing any month twice.
- **Question:** Confirm the default: each existing contract is entered as an imported contract; months already invoiced in Falcon are marked "already billed" with no link to the old invoice, and the ERP bills from the first month after cut-over. Or should past invoices be linked one by one?
- **What the system does now:** Default as above (`source=import`, past periods marked generated).
- **Reference (for the team):** billing OQ-18; billing §6, OQ-18. (Billing / tax / banking)
- **Your answer:** 

### Q57. Customer statement entries ("مستخلص عميل") after cut-over
- **Who should answer:** Accountant
- **Priority:** Needed before loading opening balances
- **Context:** Falcon books customer progress billings as "مستخلص عميل" journal lines without customer detail. Open items at the cut-over are loaded from the workbook.
- **Question:** Confirm the default: movements after the cut-over are entered in the ERP as normal invoicing documents, and are not replayed from Falcon.
- **What the system does now:** As the default.
- **Reference (for the team):** migration OQ-5; migration R8, R23, OQ-5. (Access / web / ledger / migration)
- **Your answer:** 

### Q58. Filing financial statements with the Ministry of Commerce (Qawaem)
- **Who should answer:** Accountant
- **Priority:** Needed before the first annual statements are filed
- **Context:** Saudi companies file their audited annual statements on the Ministry of Commerce's Qawaem platform, which may require a structured (XBRL) format. Statements are also uploaded with the Zakat filing.
- **Question:** Is a PDF upload enough, or does Qawaem require an XBRL or another structured format for our companies?
- **What the system does now:** PDF and XLSX exports for manual upload. No XBRL.
- **Reference (for the team):** reporting OQ-23; reporting §6 (integrations), R27. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q59. First group consolidation: opening group equity
- **Who should answer:** Accountant (with the migration agent)
- **Priority:** Needed before the first group consolidation run
- **Context:** The first consolidation needs opening group equity: profits each subsidiary earned before the group bought it are not group profits. The ERP has no history before go-live.
- **Question:** 1. Will you provide an opening consolidation journal from Falcon (or the last audited group accounts), with pre-acquisition reserves per subsidiary? 2. Confirm that income and expense before the run's start date are treated as opening retained earnings.
- **What the system does now:** As in question 2. The first run expects a migrated opening consolidation journal.
- **Reference (for the team):** consolidation OQ-12; consolidation R24, R25, §5. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q60. Foreign-currency balances at cut-over
- **Who should answer:** Accountant
- **Priority:** Needed before loading opening balances
- **Context:** Open customer and supplier items in USD, EUR and so on are loaded with both the original currency amount and the SAR amount at the cut-over. The opening entry itself is in SAR only, so these items are not revalued at the first month end. Foreign-currency bank accounts are not loaded yet.
- **Question:** 1. Keep (a) the original invoice rate, or (b) restate at the cut-over rate? 2. Should migrated foreign items take part in the first month-end revaluation (yes / no)? 3. Foreign-currency bank accounts: may the team build an opening with the currency amount and the SAR amount in banking?
- **What the system does now:** Original currency and amount plus SAR at cut-over kept; tie-out per currency; not revalued; foreign bank accounts not loaded (warning).
- **Reference (for the team):** migration OQ-13, ledger-fx OQ-16 (duplicates); migration R23, OQ-13; ledger-fx OQ-16. (Access / web / ledger / migration)
- **Your answer:** 

### Q61. Functional currency of each foreign subsidiary
- **Who should answer:** Accountant; then external auditor
- **Priority:** Needed before setting up the Egypt, China and UAE companies in the ERP
- **Context:** Each company keeps its books in its "functional currency", the currency of its main economic environment (IAS 21). It cannot be changed once the company has posted entries.
- **Question:** For each subsidiary, which functional currency: Egypt (EGP?), China (CNY?), UAE (AED?), others?
- **What the system does now:** Default SAR; locked after the first posting.
- **Reference (for the team):** ledger-fx OQ-17; ledger-fx R4, R5, OQ-17. (Access / web / ledger / migration)
- **Your answer:** 

### Q62. Income and expense balances at cut-over
- **Who should answer:** Accountant
- **Priority:** Needed before loading opening balances
- **Context:** If the cut-over is not a year end, Falcon still shows income and expenses for the year so far.
- **Question:** (a) Load them to their own income and expense accounts (default), or (b) net them into the opening-balance equity account 3900 (later moved to retained earnings 3300)?
- **What the system does now:** (a); (b) is available as an option.
- **Reference (for the team):** migration OQ-3; migration R14, OQ-3. (Access / web / ledger / migration)
- **Your answer:** 

### Q63. Intercompany accounts with several counterparties
- **Who should answer:** Accountant
- **Priority:** Needed before loading opening balances
- **Context:** Every intercompany balance in the ERP must name the other group company. One Falcon account may hold balances with several group companies.
- **Question:** Confirm the default: each mapping row names one counterparty, so a Falcon account with several counterparties must be (a) split into one Falcon code per counterparty, or (b) split in the trial balance sheet of the workbook?
- **What the system does now:** One counterparty per mapping row; mismatched mirrors between companies are reported as errors.
- **Reference (for the team):** migration OQ-7; migration W17, OQ-7; CLAUDE.md rule 6. (Access / web / ledger / migration)
- **Your answer:** 

### Q64. Leases where a group company is the tenant (right-of-use assets)
- **Who should answer:** Accountant; then the external auditor
- **Priority:** Needed before the first annual financial statements
- **Context:** When a company rents offices, land or cars from others, the standards may require a "right-of-use" asset and a lease liability. Full IFRS (IFRS 16) requires this for all material leases. IFRS for SMEs (section 20) differs, and the 2025 edition moved closer to IFRS 16.
- **Question:** 1. Do any group companies rent property or vehicles as tenant? 2. If so, should the ERP record right-of-use assets and lease liabilities, or are they handled outside the ERP?
- **What the system does now:** Out of scope. Rent paid is an expense.
- **Reference (for the team):** fixed-assets OQ-17; fixed-assets §1 (scope).

---

## Low (default is safe) (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q65. Load closing balances only, or replay the year's journals
- **Who should answer:** Accountant; then CFO
- **Priority:** Needed before loading opening balances
- **Context:** Two ways to migrate: (a) load only the closing trial balance plus open customer, supplier, employee and bai' ajel items at the cut-over; (b) load the opening balance at the start of the year and replay every Falcon journal of the year. Replayed journals cannot touch the customer and supplier control accounts, so (b) would be incomplete.
- **Question:** Confirm the default (a), closing balances plus open items only, and drop the replay of the year's journals?
- **What the system does now:** (a) is built and proved in the dry run.
- **Reference (for the team):** migration OQ-4, OQ-14 (duplicates); migration R14, R15, R23, OQ-4, OQ-14. (Access / web / ledger / migration)
- **Your answer:** 

### Q66. Measuring the EOSB and annual leave liabilities in the accounts
- **Who should answer:** Accountant; then external auditor
- **Priority:** Needed before the first annual financial statements
- **Context:** The system books the EOSB provision monthly at the full award as if every employee left today (Art. 84, fraction 1), undiscounted. Untaken annual leave is not booked; there is only a report. Accounts 2350 / 5130 were approved "only if leave is accrued monthly".
- **Question:** 1. EOSB under IFRS for SMEs: (a) full award as if terminated today (default); (b) the resignation award; (c) an actuarial valuation?
  2. Should the EOSB report show the resignation (Art. 85) amount as a memo and apply any discounting? (Default: memo only, undiscounted.)
  3. Should untaken annual leave be booked monthly (2350 / 5130), or reported only? (Default: report only.)
  4. If reported or booked: include approved future leave; pro rata to the date; leave paid in advance stays in until taken; actual wage at that date; no employer GOSI on top. Confirm.
- **What the system does now:** As the defaults.
- **Reference (for the team):** payroll OQ-11, OQ-52, OQ-53; hr OQ-6; payroll R24, R75 to R78, §5 E4, OQ-11, OQ-52, OQ-53; hr OQ-6 (Payroll / HR)
- **Your answer:** 

### Q67. Migrated assets: keep Falcon's useful lives or re-estimate at go-live
- **Who should answer:** Accountant; then CFO
- **Priority:** Needed before the opening fixed-asset register is loaded from Falcon
- **Context:** Assets come over from Falcon with their cost, accumulated depreciation, method and remaining life. Falcon's workbook has no column for accumulated impairment.
- **Question:** 1. After cut-over, should migrated assets (a) keep Falcon's method and remaining life until a formal change of estimate (default) or (b) be re-estimated under the new approved policy at go-live? 2. Does Falcon's accumulated figure contain any impairment, or is it all depreciation?
- **What the system does now:** (a), and opening impairment is 0 (the whole figure is treated as depreciation).
- **Reference (for the team):** fixed-assets OQ-20; fixed-assets R40, R41, R16. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q68. Opening balances from the old system (Falcon)
- **Who should answer:** Accountant; then CFO
- **Priority:** Needed before the opening-balance migration
- **Context:** At go-live each employee's EOSB balance, leave balance, open loans and year-to-date figures must be loaded so that they agree with the opening ledger balances.
- **Question:** 1. Confirm the default: EOSB per employee loaded as opening rows that add up to account 2500's opening balance; open loans loaded as active with their remaining instalments.
  2. Are there existing bai' ajel receivables to load, and may they go through the one-time opening entry (the only exception to the control-account rule)?
  3. Which year-to-date figures are needed (for Egypt salary tax, YTD pay is needed)?
- **What the system does now:** As above.
- **Reference (for the team):** payroll OQ-26; payroll §10.9 item 11 (bai' ajel opening receivables, "open item for the CFO"); payroll OQ-26; migration templates (Payroll / HR)
- **Your answer:** 

### Q69. Payroll account choices made by the team
- **Who should answer:** Accountant; then CFO to approve
- **Priority:** Needed before first payroll
- **Context:** While building, the team picked accounts for a few cases where the spec gave none.
- **Question:** 1. One-off earnings and deductions that are not linked to a pay component go to 5100 Salaries (a deduction reduces salary cost). Confirm, or name accounts.
  2. Writing off an unrecovered employee loan: which expense account? (Default: 5100 until you name one.)
  3. Corrections of approved settlements are dated on the day they are approved. Confirm.
- **What the system does now:** As above.
- **Reference (for the team):** payroll §10.2 ("to confirm with the accountant"), §10.8 loan write-off ("CFO to confirm the account"), §10.8 slice 2 defaults; payroll R19, §10.2, §10.8 (Payroll / HR)
- **Your answer:** 

### Q70. Prior-year (FY2026) comparatives from Falcon
- **Who should answer:** Accountant (with the migration agent)
- **Priority:** Needed before the FY2027 statements with comparatives
- **Context:** The first ERP statements need the previous year's figures for comparison. They come from Falcon, the legacy system. The owner said migration comes later and that the journal export already sent is close to final (owner D).
- **Question:** For FY2026 comparatives, should we import (a) the full prior-year trial balance month by month or (b) an annual summary only?
- **What the system does now:** Annual summary import is assumed.
- **Reference (for the team):** reporting OQ-17; reporting R17; migration spec. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q71. Quarter boundaries for quarterly VAT
- **Who should answer:** Accountant (check the company's ZATCA portal)
- **Priority:** Needed before the first VAT return of each company
- **Context:** Quarterly filers file for three-month periods. The ERP assumes calendar quarters (Jan–Mar, Apr–Jun, …) with a shorter first period. For Almiras (registered 1 April 2024) this gives the same quarters either way; a subsidiary registered mid-quarter might differ.
- **Question:** Are quarters (a) calendar quarters (default), or (b) counted from each company's registration date as shown in the ZATCA portal? Please check for every subsidiary.
- **What the system does now:** Calendar quarters; a setting allows another starting month per company.
- **Reference (for the team):** tax-returns OQ-5; tax-returns §3.1 `vat_quarter_start_month`, R3, AT-4. (Billing / tax / banking)
- **Your answer:** 

### Q72. Settlement of foreign-currency receipts after posting
- **Who should answer:** Accountant; then CFO
- **Priority:** Needed before go-live if customers pay in foreign currency before the invoice. **Confirm the default.**
- **Context:** If a customer pays in a foreign currency and part of the money is not matched to an invoice when the receipt is posted, that part cannot be matched later. The receipt must be reversed and entered again. Payments in one currency against an invoice in another (cross-currency) are not built.
- **Question:** 1. Is reversing and re-entering acceptable, or is a later matching with an exchange difference entry needed?
  2. Do customers or suppliers pay in a currency different from the invoice's (for example a USD invoice paid in SAR)?
- **What the system does now:** Later matching is refused and cross-currency payments are not built.
- **Reference (for the team):** invoicing OQ-56; invoicing R76 (b) to (d). (Invoicing)
- **Your answer:** 

### Q73. Timesheet billing
- **Who should answer:** Accountant; owner for who enters hours
- **Priority:** Needed before go-live (timesheets are needed at go-live, D14(c))
- **Context:** Some services are billed by the hour: approved hours × hourly rate. The details of entering and approving hours were not decided.
- **Question:** Confirm the defaults or change them:
  1. Who enters hours: (a) accounting staff (default), (b) the worker, (c) self-service later?
  2. When is a month billed: (a) once every submitted entry is approved or rejected (default), (b) on a fixed cut-off day (which)?
  3. Hours with 2 decimals (default)?
  4. One hourly rate per contract line (default), or a rate per worker grade?
- **What the system does now:** Not built; defaults above.
- **Reference (for the team):** billing OQ-42; billing R74, AT-59. (Billing / tax / banking)
- **Your answer:** 

## External auditor

### Q74. Assets bought or sold on long payment terms — confirm the default
- **Who should answer:** External auditor and Sharia board; CFO for each company's normal credit terms
- **Priority:** Needed before the first asset bought on deferred payment terms
- **Context:** When payment for an asset is deferred well beyond normal terms, accounting standards (section 17.13, IAS 16.23) measure the cost at the cash price, and the rest is treated as a financing element. In a Sharia company that element must never be booked as interest.
- **Question:** 1. Confirm the default: such bill lines are only **flagged**, with the full amount as cost and no split. 2. If a split is needed, classify the financing element as (a) part of cost or (b) deferred-sale profit on 5600. 3. CFO: what is each company's normal credit period in days (the trigger for the flag)?
- **What the system does now:** Flag only. The credit period is empty, so nothing is flagged until the CFO sets it.
- **Reference (for the team):** fixed-assets OQ-25 (compliance S13; rule R4a); fixed-assets R4a, §3.9 `normal_credit_days`. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q75. Investment property: fair value or cost model, valuers and valuation frequency — confirm the default
- **Who should answer:** External auditor (with the accountant)
- **Priority:** Needed before the first annual financial statements of any IFRS for SMEs company that holds investment property
- **Context:** Investment property is land or buildings held to earn rent or for capital growth (account 1510). Under IFRS for SMEs (section 16) this is **not a free choice**: fair value through profit or loss is required whenever fair value can be measured reliably without undue cost or effort. Otherwise the cost model applies, with a justification per property. Under full IFRS (IAS 40) it is a choice, but the fair value must still be disclosed. In Saudi Arabia valuers are normally licensed by Taqeem (not verified).
- **Question:** 1. Confirm the default: cost model for all companies, with fair values and a per-property "undue cost or effort" justification recorded. Or should IFRS for SMEs companies with a market value use the fair-value model? 2. Which valuers (Taqeem-licensed?) and how often: each reporting date or once a year? 3. Should full-IFRS companies choose cost or fair value? 4. In the group accounts, must all companies use the same model (consolidation OQ-15)?
- **What the system does now:** Cost model in every company. Fair values with valuer name, licence, evidence and justification can be recorded per property for the disclosure note. Nothing is posted from them.
- **Reference (for the team):** fixed-assets OQ-6 (compliance A5, A6, A15, S8; rules R20, R48); related consolidation OQ-15; fixed-assets R20, R48, §3.8 `IpFairValue`, AT-47; reporting §3.6 (notes data). (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q76. Which edition of the accounting standard each company applies from 2027 — confirm the default
- **Who should answer:** External auditor (with the accountant)
- **Priority:** Needed before the FY2027 final financial statements
- **Context:** The 3rd edition of IFRS for SMEs (2025) applies to years starting 1 January 2027, Almiras's first year on the ERP. IFRS 18 replaces IAS 1 for full-IFRS companies from the same date. Saudi standards body SOCPA endorses standards with its own changes. Whether SOCPA has endorsed the 3rd edition, and from when, was not confirmed. Comparative figures may need re-presenting.
- **Question:** 1. For FY2027, which edition does each company apply: IFRS for SMEs 2015 or 2025; IAS 1 or IFRS 18? 2. Which SOCPA modifications apply? 3. How are the 2026 comparatives re-presented? 4. Please have the paragraph references the team used (17.15B, 27.30, the section 20 lessor paragraph) checked against the 2025 text.
- **What the system does now:** References follow the 2025 edition. Each company's edition is a setting (`framework_edition`). Comparatives are re-presented with the current layout.
- **Reference (for the team):** fixed-assets OQ-28 (compliance S12; framework-edition note); reporting OQ-4; fixed-assets §4 framework note, R19, R21, R27; reporting §3.1, R3, R4, R17. (ZATCA / fixed assets / reporting)
- **Your answer:** 

## Tax adviser

### Q77. Fixed-asset sales: link from the sales invoice to the asset, and how long to keep capital-asset purchase bills
- **Who should answer:** Tax adviser (record retention); then the accountant confirms the income type
- **Priority:** Blocks build (fixed-assets slice 2: selling or transferring an asset)
- **Context:** To sell an asset, staff will raise a normal sales invoice with a line "fixed asset disposal", booked to proposed account 4800 and linked to the asset. ZATCA's record-keeping rules keep capital-asset records longer than ordinary records: at least 15 years for real estate, and for movable assets the adjustment period plus 5 years. So the sales invoice must take its retention class from the asset sold, not only from the account it uses. The team builds the link itself. The open part is what the law requires for the **purchase** bills of capital assets.
- **Question:** 1. Must purchase bills (and their credit notes) for capital assets be kept as long as the asset's own records (15 years for real estate; adjustment period + 5 years for movable assets), rather than the standard 6 years? 2. Accountant: is one income type "fixed asset disposal" on 4800 enough, or do you want separate gain and loss accounts (see the accounts entry, fixed-assets OQ-5)?
- **What the system does now:** Slice 1 refuses asset sales. Purchase bills carry no retention date of their own. All records are kept indefinitely anyway (owner decision D-16(e)), so the retention date only sets when a record may first be proposed for destruction.
- **Reference (for the team):** fixed-assets OQ-14 (related: invoicing OQ-46, zatca OQ-31); fixed-assets R22, R24, R33 (hand-off), AT-10, AT-41; zatca R-38. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q78. Real estate and vehicles: VAT and Real Estate Transaction Tax (RETT) when an asset is sold or transferred
- **Who should answer:** Tax adviser
- **Priority:** Blocks build (real-estate sales and intercompany transfers of real estate stay switched off until answered)
- **Context:** A sale of land or buildings is exempt from VAT (Royal Order A/84). Instead, RETT of 5% of the value is due under the RETT Law (2024) and its Implementing Regulations (in force since 10 April 2025). Charging 15% VAT on a property sale, or missing the RETT, would be a direct tax error, so the system refuses every real-estate disposal for now. Used vehicles are taxed at 15% on the full price. The "profit-margin" scheme does not apply, because Almiras is not a ZATCA-licensed car trader. The compliance check used search extracts only. Nobody opened the regulation texts.
- **Question:** 1. Confirm that a property sale is VAT-exempt and that RETT is 5% of the value. Who pays the RETT, and which exemptions might apply to us? 2. Which ZATCA exemption reason code goes on the e-invoice for a property sale? 3. Confirm that a used vehicle is sold at 15% on the full price, with no margin scheme. 4. Are occasional sales of capital assets left out of the input-VAT recovery ratio for companies with both taxable and exempt income (VAT IR Art. 51)? 5. Once you answer, the team plans this rule: VAT category "exempt" on the sales invoice, plus a RETT record (5% or a cited exemption) with proof of payment before the sale posts, and RETT borne by the seller booked to a new RETT expense account. Is that right?
- **What the system does now:** Real-estate sales and intercompany transfers are refused, with an Arabic and English message citing OQ-10. Other disposals use the VAT category chosen by staff (slice 2). RETT is not built.
- **Reference (for the team):** fixed-assets OQ-10 (compliance A8, A9, B4, N3, N9; rule R22a); fixed-assets R22, R22a, R24, OQ-5 (RETT account), FA3 to FA5, FA7. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q79. Input VAT on bills still unpaid after 12 months
- **Who should answer:** Tax adviser (VAT)
- **Priority:** Blocks go-live (VAT returns)
- **Context:** A 2025 amendment of the VAT rules reportedly says that input VAT claimed on a purchase that is still unpaid 12 months after its due date must be given back to ZATCA, and claimed again when the bill is paid. The team only had the 2021 text and could not check this.
- **Question:** 1. Does this rule apply, and from which date?
  2. Do the 12 months start from (a) the bill's due date, (b) the invoice date, or (c) the date the bill was booked?
  3. Should the ERP (a) only flag such bills for the reviewer (current), or (b) reverse the VAT in the return automatically?
- **What the system does now:** It lists them as an `unpaid_bill_12_months` exception in the return of the period in which 12 months from the booking date end. Nothing is reversed automatically.
- **Reference (for the team):** tax-returns OQ-43 (compliance finding C15); tax-returns §10.1 (tie-out exceptions), §10.4 C15. (Billing / tax / banking)
- **Your answer:** 

### Q80. Official VAT return form: check every box
- **Who should answer:** Tax adviser (VAT)
- **Priority:** Blocks go-live (before the first VAT return is filed from the ERP)
- **Context:** The ERP fills the 16 boxes of the VAT return from the posted invoices and bills. The team built it without the official ZATCA VAT return filing guide (it was never downloaded). So all 16 boxes show "unverified" on screen and in exports until the accountant proposes and the CFO confirms each one (owner answer H3). The figures are typed into the ZATCA portal by hand.
- **Question:** 1. Please provide, or confirm the team may download, the current ZATCA VAT return filing guide, including the Arabic box labels.
  2. Please confirm where each of these goes on the form (the system's current choice is in brackets):
     (a) import VAT paid at customs: is the box 8 amount the customs value plus customs duty plus other charges? [yes];
     (b) reverse-charge VAT on goods supplied inside KSA by a non-resident: box 9 with imported services? [box 9];
     (c) the yearly true-up of partly recoverable input VAT (Art. 51): box 7 "Adjustments" column? [box 7 Adjustments];
     (d) credit notes on invoices from the old 5% period (before July 2020; possible in subsidiaries, not in Almiras): box 1 or a separate line? [box 1];
     (e) a final invoice that deducts an earlier prepayment invoice: reported net of the deduction? [net];
     (f) the SAR 40,000,000 yearly sales level above which a quarterly filer must file monthly? [warning only];
     (g) the two points left open by the compliance check of the VAT slice (C3, C14).
- **What the system does now:** It uses the placements in brackets, shows "unverified" everywhere, and files manually through the portal.
- **Reference (for the team):** tax-returns OQ-2 (VAT part), OQ-41, OQ-44; tax R5, R18, R22, R23, R55 **[CC-verify] / [verify]** markers; tax §3.2 box table; tax-returns §3.2, R5, R18 to R25, R55, §10.1 and §10.4. (Billing / tax / banking)
- **Your answer:** 

### Q81. Prepayment (advance payment) invoices, ZATCA type 386
- **Who should answer:** Tax adviser (numbering question); owner via the simulation run above, or a written question to ZATCA, for the onboarding sample
- **Priority:** Blocks go-live (for every company whose contracts take advance payments)
- **Context:** The owner decided that advance payments are invoiced with a prepayment invoice (type 386). The final invoice later deducts the advance (owner decision invoicing D15). The ZATCA side is ready to check these documents. ZATCA's current software kit (SDK) accepts 386, and a plain 386 passed the developer-portal sandbox. But the final invoice that deducts the advance cannot be produced until the team builds the advance flow in invoicing (team work, not a question). Two points still need an outside answer.
- **Question:** 1. Must a 386 sample be part of the compliance test invoices sent when an EGS unit is onboarded? The simulation run or ZATCA can answer this. 2. Tax adviser: may prepayment invoices have their own number series (prefix `PRE`), or must they share the normal invoice number series? 3. Are other special invoice types used anywhere in the group: export, third-party (billing on behalf of another), nominal (free supplies), summary or self-billed invoices? If not, they stay out of scope (approved default D-9).
- **What the system does now:** Invoices, credit notes and debit notes are built. A 386 is refused with "prepayment invoices (386) wait for the advances flow" (`zatca/messages.py`). Onboarding sends the standard 3 or 6 compliance samples, without a 386.
- **Reference (for the team):** zatca OQ-29 (and R-39; OQ-33 is resolved: the current SDK accepts 386); invoicing OQ-16, OQ-4 remainder; zatca R-8, R-39, AT on 386; invoicing R65, E16, AT-59. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q82. Withholding tax on payments to foreign suppliers: remaining points
- **Who should answer:** Tax adviser; then CFO to approve
- **Priority:** Blocks go-live of payments to non-resident (foreign) suppliers
- **Context:** When a Saudi company pays a foreign supplier for services, it keeps back WHT (for example 5% on consulting, 15% on royalties, 20% on management fees) and pays it to ZATCA by the 10th of the next month. The adviser has given the rates, the treaty relief and the gross-up rule. The rates are loaded but marked "unverified", and payments to foreign suppliers stay blocked until they are verified.
- **Question:** 1. Please confirm each rate against current law: management fees 20%; royalties 15%; services paid to a related party 15%; other payments 15%; rent, technical and consulting services, air tickets and freight, dividends, loan returns, insurance, international telecom 5%.
  2. When is WHT recognised: (a) when the supplier is paid (current), or (b) when the supplier's bill is booked?
  3. Gross-up, for a contract "net of tax": is the formula WHT = amount × rate ÷ (1 − rate)? Example: SAR 10,000 at 5% gives WHT SAR 526.32, paid by the company.
  4. A supplier resident in two countries (dual resident): which treaty certificate counts?
  5. Is there an overall cap on the WHT of one payment that covers several categories?
- **What the system does now:** WHT is calculated at payment with the gross-up formula above. The rates are loaded but marked unverified. Foreign-supplier payments are blocked until the rates are verified.
- **Reference (for the team):** invoicing OQ-8 (remainder); rule markers R62, R63, R105; invoicing R61 to R64, R105, E20 to E22, AT-54 to AT-56.

---

## Needed before a specific event (Invoicing)
- **Your answer:** 

### Q83. Withholding tax on remuneration of people working abroad
- **Who should answer:** Tax adviser (WHT); then CFO to approve
- **Priority:** Blocks go-live (for remuneration payroll only)
- **Context:** Remuneration staff are not Saudi employees; the owner decided that withholding tax (WHT) is "considered" on their pay. The adviser's WHT list for suppliers has no category for individuals working abroad, so the system does not assume a rate (for example the 15% "other payments" rate). The WHT return and account 2220 must tie out.
- **Question:** 1. Is Saudi WHT due at all on a monthly remuneration paid by a Saudi company to a non-resident individual for work done outside Saudi Arabia?
  2. If yes: which category and rate; does a tax treaty reduce it (what certificate is needed); is it due when the pay is accrued or when paid; and which line of the WHT return?
  3. Which SAR exchange rate and date apply to a foreign-currency payment?
  4. When the net pay is too small to cover the WHT, who bears the shortfall: the employee (recovered later) or the company (a gross-up cost)?
  5. Should remuneration cost go to its own expense account rather than salaries (5100)?
- **What the system does now:** Every remuneration payment must carry a WHT category, but no rate is assumed; WHT is computed on the gross, for non-residents only, at payment, borne by the employee, capped at that person's own pay; any part not withheld is flagged on the WHT return for review, not booked.
- **Reference (for the team):** payroll OQ-29 and OQ-59 (same question); payroll R43, §10.9 B2, B3, items 23, 24; payroll R42, R43, R51, §10.9, OQ-29, OQ-59; invoicing R64, R105 (Payroll / HR)
- **Your answer:** 

### Q84. Zakat: how the base is calculated
- **Who should answer:** Tax adviser (Zakat)
- **Priority:** Blocks go-live (Zakat)
- **Context:** Zakat is not built yet. The design computes each company's Zakat once a year from its closed trial balance: a "Zakat base" (what is added and deducted), compared with the adjusted net profit, times the Saudi and GCC share of ownership, times the rate. The lines of the base are only placeholders until the 2024 Zakat Implementing Regulations are checked.
- **Question:** 1. For a holding company like Almiras, which balance-sheet items are added to and deducted from the base? In particular: investments in subsidiaries, associates and foreign companies; provisions (end-of-service 2500); fines (including GOSI fines); charity payable from purification (2600).
  2. Confirm the rate: 2.5% for a Hijri year, 2.5775% for a Gregorian year.
  3. Confirm the floor rule: the amount charged is never lower than Zakat on the adjusted net profit.
  4. Confirm an investment is deducted only when the company invested in files Zakat in KSA and a certificate is held; investments in foreign companies are not deducted.
  5. Confirm the filing deadline: 120 days after the year end.
- **What the system does now:** Not built. Placeholder template; the values above as defaults, marked unverified.
- **Reference (for the team):** tax-returns OQ-20, OQ-2 (Zakat part); tax R39 to R43, R47, R13 (Zakat due date) **[CC-verify]**; tax-returns §3.4, R39 to R43, R47, T6, AT-34 to AT-36, AT-39. (Billing / tax / banking)
- **Your answer:** 

### Q85. Zakat: one combined return for the group
- **Who should answer:** Tax adviser (Zakat); then owner to decide whether to use it
- **Priority:** Blocks go-live (group filing only)
- **Context:** A parent may file one Zakat return for itself and its wholly owned Saudi subsidiaries, with ZATCA's approval. The ERP can support this, but the conditions must be confirmed first.
- **Question:** 1. Does Almiras file, or want to file, one combined Zakat return for the group?
  2. What are the conditions: 100% owned directly or indirectly, Saudi companies only, ZATCA approval needed first?
  3. How does it affect each subsidiary's own Zakat certificate?
- **What the system does now:** Each company files on its own (standalone).
- **Reference (for the team):** tax-returns OQ-22; tax R44 **[CC-verify]**; tax-returns R44, §3.4 `ZakatFilingGroup`, AT-37. (Billing / tax / banking)
- **Your answer:** 

### Q86. A rejected consumer invoice already given to the buyer: is a credit note needed?
- **Who should answer:** Tax adviser
- **Priority:** Needed before ZATCA Phase 2 go-live
- **Context:** A consumer (simplified) invoice is handed to the buyer when it is created and reported to ZATCA afterwards. If ZATCA then rejects it, the owner decided that a corrected invoice is issued under a new number (D-4). ZATCA's FAQ contradicts itself: page 77 says to cancel with a credit note and issue a new invoice, page 71 says no further invoice is needed.
- **Question:** When ZATCA rejects a consumer invoice the buyer already has: (a) issue the corrected invoice only, or (b) also issue a credit note against the rejected one?
- **What the system does now:** (a) re-issue only. Nothing was booked for the rejected invoice, so there is nothing to reverse.
- **Reference (for the team):** zatca OQ-6 (remainder); zatca R-12, R-42, §5 table (simplified rejected after hand-over). (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q87. Art. 52 adjustment period and how long capital-asset records are kept — confirm the default
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first year-end Art. 52 review (end of FY2027); for retention it only moves the date when old records may first be proposed for destruction
- **Context:** Under VAT IR Art. 52, if a capital asset's taxable use changes within its "adjustment period", the VAT recovered on it is adjusted each year. An example is a building moving from commercial lease (15%) to residential lease (exempt). Secondary sources give 6 years for movable assets (or the accounting life if shorter) and 10 years for real estate. ZATCA's record rules keep movable-asset records for the adjustment period plus 5 years, and real estate "not less than 15 years", without saying from when. The two modules disagree today. Fixed assets assumes 72 months for movable assets, but the ZATCA archive's own setting (`ZATCA_MOVABLE_ADJUSTMENT_YEARS`) is still empty, so ZATCA uses the standard 6-year date.
- **Question:** 1. Confirm the adjustment periods: (a) movable assets 6 years (72 months), or the accounting useful life if shorter; (b) real estate 10 years (120 months). 2. Are leasehold improvements, and construction work in progress for a building, real estate for Art. 52 and record keeping? The default says yes. 3. When does the 15-year real-estate retention start: (a) end of the tax period of the document (default), (b) acquisition date, or (c) disposal date? 4. Which documents count as "movable capital asset" records (for example the purchase bill, the sale invoice)? 5. For assets migrated from Falcon: what initial VAT recovery % should be recorded? The default is 100% unless the accountant enters otherwise. 6. What format do you need for the year-end Art. 52 report (assets inside their period whose use changed)? 7. May the team set the ZATCA archive setting to the same 6 years so that both modules agree?

  *Also raised in the invoicing list:* Which sales count as movable capital assets, what is the adjustment period, and where does the acquisition date come from (for example the fixed-assets register)?
- **What the system does now:** Fixed assets uses 72 / 120 months per company and records each asset's VAT use and recovery %. A change of use is recorded as an event. No Art. 52 adjustment is calculated (report in slice 2). The ZATCA archive ignores the adjustment period until its setting is filled. Everything is kept indefinitely (D-16(e)).
- **Reference (for the team):** fixed-assets OQ-27 (compliance S1, S2, S4, A16, D8); zatca OQ-31 (R-38 adjustment-period setting); zatca OQ-8 (remainder: real-estate start point); fixed-assets OQ-10 (the Art. 52 part); also invoicing OQ-46 = zatca OQ-31; fixed-assets R33, R46, R47, §3.1 (retention class), §3.9 (settings); zatca R-38; `config/settings.py` `ZATCA_MOVABLE_ADJUSTMENT_YEARS = None`. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q88. Bank charges with a VAT invoice from the bank
- **Who should answer:** Tax adviser; then accountant
- **Priority:** Needed before the first VAT return (the default is safe but may give up recoverable VAT)
- **Context:** Banks charge fees and usually add 15% VAT, often with a tax invoice. Today the whole charge (fee plus VAT) goes to "Bank charges" (5700) and no VAT is claimed back.
- **Question:** When the bank issues a VAT invoice, should the VAT be claimed back as input VAT? (a) No, book gross (default); (b) yes, split the VAT out when a tax invoice is held.
- **What the system does now:** Gross to 5700, no VAT split.
- **Reference (for the team):** banking OQ-13 **[CC-verify]**; banking R26, B8, §5 B1, B2. (Billing / tax / banking)
- **Your answer:** 

### Q89. Bills from small suppliers that have no VAT number
- **Who should answer:** Tax adviser; then CFO to approve
- **Priority:** Needed before go-live (small unregistered suppliers are common)
- **Context:** A supplier without a VAT number cannot charge VAT, so its bill is booked with 0 VAT and nothing is claimed back. Sometimes such a supplier's paper bill shows VAT anyway.
- **Question:** 1. Which VAT category code should these bill lines carry: (a) "O" (out of scope, current); (b) "Z" (zero-rated); (c) another code?
  2. When an unregistered supplier's bill shows VAT: (a) refuse the bill until the supplier corrects it (current); (b) accept it and book the VAT shown as part of the cost; (c) pay only the amount without the VAT?
- **What the system does now:** It uses category O with VAT 0.00, and it refuses at entry a bill whose VAT total is not 0.00.
- **Reference (for the team):** invoicing OQ-60; invoicing R92, E8a, AT-95, AT-96. (Invoicing)
- **Your answer:** 

### Q90. Bills from suppliers without a VAT number
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first VAT return (confirm the default)
- **Context:** A supplier with no VAT number charges no VAT (owner decision). The question is whether such purchases appear on the VAT return.
- **Question:** Are they (a) left off the return (default), or (b) reported in box 11 (exempt purchases) or another box?
- **What the system does now:** Not reported.
- **Reference (for the team):** tax-returns OQ-12; tax-returns R21, AT-21. (Billing / tax / banking)
- **Your answer:** 

### Q91. Credit or debit note to a government customer: which tax date
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first credit or debit note to a government customer
- **Context:** For invoices to government bodies, the owner decided that the government's payment order date sets the VAT period and the exchange rate date (Art. 20(5)). That decision talks about invoices only.
- **Question:** For a credit or debit note to a government customer: (a) it keeps its own date (current); (b) it uses the original invoice's payment order date; (c) it uses a new payment order date of its own?
- **What the system does now:** Option (a). The payment order date on a note is printed in the e-invoice only.
- **Reference (for the team):** invoicing OQ-73; invoicing R117, R100 (D38 note), R95. (Invoicing)
- **Your answer:** 

### Q92. Government customers: which sales fall under VAT Art. 20(5) — confirm the default
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first standard invoice to a government customer
- **Context:** For certain supplies to government bodies (VAT Implementing Regulations Art. 20(5)), VAT is due on the date of the payment order, not the invoice date. ZATCA's XML then needs that date. The owner decided to add a payment-order date to sales documents and to hold such invoices until it is entered (G1, I3). The team default treats every customer marked "government" this way.
- **Question:** Confirm the default, or say: is every supply to a government customer an Art. 20(5) supply, or only contracts under the Government Tenders and Procurement Law? If only some, do we need a flag per customer or per contract?
- **What the system does now:** Every customer marked "government" is treated as Art. 20(5). A standard invoice to it waits, unissued, until the payment-order date is entered.
- **Reference (for the team):** zatca OQ-37 (b) (decided otherwise by owner G1, I3); zatca R-54, D-18(b); invoicing R117. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q93. How far back a contract correction may go
- **Who should answer:** Tax adviser; then CFO
- **Priority:** Needed before the first backdated contract correction
- **Context:** When a contract is changed or cancelled with a past date, the ERP drafts credit or debit notes for months already invoiced. The note is dated and taxed on its own date (D12). There is no limit on how far back this can reach.
- **Question:** Should there be a limit, for example (a) no limit other than the contract start date (default); (b) not before the last filed VAT return; (c) not before the last audited financial year; (d) another limit?
- **What the system does now:** No limit other than the contract start date.
- **Reference (for the team):** billing OQ-25 (date part resolved by D12) **[Needs expert sign-off: tax]**; billing R39, R42, R43. (Billing / tax / banking)
- **Your answer:** 

### Q94. Input VAT claimed by a group company that has no VAT number
- **Who should answer:** Tax adviser; then CFO to approve
- **Priority:** Needed before any group company without a VAT number starts using the ERP
- **Context:** A company that is not VAT-registered cannot claim back VAT it pays to suppliers. Today the system decides whether VAT on a bill is claimable from the supplier's VAT number and from the use the clerk enters ("taxable", "exempt" or "mixed"), not from whether our own company is registered. So an unregistered company could book claimable VAT by mistake.
- **Question:** For a group company without a VAT number, should VAT on every bill always go to cost (never claimed back), whatever the clerk enters? (a) Yes, always to cost (recommended). (b) No, keep the current logic.
- **What the system does now:** It keeps the current logic and flags it for review.
- **Reference (for the team):** invoicing OQ-61; invoicing R29, R104, R94, R101. (Invoicing)
- **Your answer:** 

### Q95. Input VAT for companies with both taxable and exempt income (Art. 51): remaining points
- **Who should answer:** Tax adviser; then CFO (for the account)
- **Priority:** Needed before the first VAT return of a company with "mixed" costs, and before its first year-end true-up
- **Context:** A company with exempt income (for example residential rent) may claim back only part of the VAT on shared costs. The adviser's method is built: the prior year's ratio of taxable to total sales, and a true-up in the last return of the year. Three details are missing.
- **Question:** 1. Which account takes the true-up difference on the cost side (for example a separate "non-recoverable VAT adjustment" expense account, or the original cost accounts)?
  2. A company with no sales in the prior year (new company): what ratio applies? Today the CFO must enter one with a reason, or "mixed" bills cannot be approved.
  3. Are out-of-scope (category O) sales left out of the ratio, as built? How are sales of capital assets (for example selling a building or a car) treated in the ratio?
- **What the system does now:** Out-of-scope sales are excluded. "Mixed" bills are refused until an approved ratio exists for the year. The true-up account is not chosen yet.
- **Reference (for the team):** invoicing OQ-9 (remainder); rule marker R104 [CC-verify]; invoicing R104, E25, AT-109; tax-returns R55. (Invoicing)
- **Your answer:** 

### Q96. Intercompany sale of an asset: price, VAT value and the buyer's useful life
- **Who should answer:** Tax adviser; then Managing Partner and CFO to decide
- **Priority:** Needed before the first intercompany asset transfer (fixed-assets slice 2)
- **Context:** One group company may sell an asset to another through a normal VAT invoice. The buyer gets an automatic draft "mirror" bill (owner A8). The price matters for VAT, Zakat and transfer pricing. Between related parties, VAT law allows ZATCA to use market value, which matters when the buyer cannot recover all the VAT. For real estate, secondary sources describe a RETT exemption for 100% intra-group transfers with a 5-year lock-up and clawback (said to be RETT IR Art. 3(18)). This has not been checked against the regulation.
- **Question:** 1. Transfer price: (a) net book value, (b) market value, or (c) cost plus a markup? The owner's "cost-plus" answer (A8) was given for services. Does it also apply to assets? 2. Must VAT be charged on market value when that differs from the price? 3. Does the 100%-group RETT exemption exist as described, and what are its conditions? 4. Does the buyer depreciate over (a) a new full life under its own policy (default) or (b) the remaining life?
- **What the system does now:** The seller's staff enter the price, approved under the seller's authority matrix. The buyer's cost is the mirror bill amount and its life comes from the buyer's own policy. Real estate is refused.
- **Reference (for the team):** fixed-assets OQ-11 (compliance S9); fixed-assets R24, R22a, D11, AT-11. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q97. Invoice dating rules from ZATCA (no backdating; the 15th-of-next-month deadline)
- **Who should answer:** Tax adviser (VAT)
- **Priority:** Needed before go-live (confirm the team's advice)
- **Context:** The owner wants monthly invoices dated on the last day of the service month. ZATCA does not allow an invoice date earlier than the day the invoice is created. So the ERP runs the billing on that day (20:00). If the run is missed, the invoice carries the actual run date and shows the service month as the supply period. The team also believes a tax invoice must be issued by the 15th of the following month (VAT Implementing Regulations Art. 53) and alerts staff on the 13th.
- **Question:** Please confirm: (1) an e-invoice can never be dated before the day it is generated; (2) for monthly services (rent, car rental, secondments) the tax invoice is due by the 15th of the following month; (3) an invoice issued later, showing the right service month, is acceptable.
- **What the system does now:** As described: month-end run on the last day, never backdated, alert on the 13th, "late tax invoice" listed after the 15th.
- **Reference (for the team):** billing OQ-44, OQ-5 follow-up (OQ-5 resolved by D14(d)); billing R15, R78 **[verify]**; billing R14, R15, R77, R78, AT-61 to AT-63. (Billing / tax / banking)
- **Your answer:** 

### Q98. Leases with both homes and shops, and extra charges on leases
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first mixed-use lease or the first lease with extra charges
- **Context:** Residential rent is exempt from VAT and commercial rent is taxed at 15%. Some contracts mix both, or add service charges, utilities or parking.
- **Question:** 1. A lease covering residential and commercial parts: must it be split into separate lines with their own VAT?
  2. VAT on service charges, utilities and parking: (a) follow the rent (exempt for residential, 15% for commercial), or (b) always 15%?
- **What the system does now:** One use (residential or commercial) per contract; extras are separate lines with their own VAT category chosen by staff.
- **Reference (for the team):** billing OQ-15 **[CC-verify]**; billing R24 (residential exempt / commercial 15% confirmed by Tax 3); billing R24, R25, AT-12. (Billing / tax / banking)
- **Your answer:** 

### Q99. Prepayment (advance) invoice numbering
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first prepayment (advance) invoice is issued
- **Context:** An advance received from a customer gets a prepayment invoice (ZATCA type 386). ZATCA requires invoice numbers to be unique and in sequence.
- **Question:** May prepayment invoices have their own number series (for example ALM-PRE-2026-000001), or must they share the normal invoice series (ALM-INV-…)?
- **What the system does now:** It uses a separate series.
- **Reference (for the team):** invoicing OQ-4 (remainder); invoicing §3.2 (sequence), R65. (Invoicing)
- **Your answer:** 

### Q100. Reverse-charge VAT on foreign suppliers' bills: which date and rate
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first VAT return with reverse-charge VAT on a foreign-currency bill
- **Context:** When a foreign supplier bills us for services, we declare the VAT ourselves ("reverse charge"). For sales, the owner decided the SAR rate is the SAMA rate on the tax point (G7). For reverse-charge bills, the date and the rate source were never decided.
- **Question:** For reverse-charge VAT on a foreign-currency bill: (a) use the bill's accounting date and the normal rate table (current); (b) use the SAMA rate on the tax point (date of supply or payment, whichever is earlier), the same as for sales?
- **What the system does now:** Option (a).
- **Reference (for the team):** invoicing OQ-51 (c); invoicing R30, R103, R100; compliance report row 10a. (Invoicing)
- **Your answer:** 

### Q101. Security deposit kept by the company (forfeited): VAT
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first deposit is forfeited (for example a tenant leaves early and loses the deposit)
- **Context:** A refundable deposit has no VAT when it is received. When it is used against rent, the VAT follows the rent. The adviser said ZATCA "usually" treats a forfeited deposit as payment for the supply, so 15% applies on commercial leases. He left the final answer open and did not cover residential leases.
- **Question:** 1. Commercial lease: is a forfeited deposit taxed at 15% (an invoice is issued for it)?
  2. Residential lease (exempt rent): is the forfeited deposit exempt as well, or taxed at 15%?
- **What the system does now:** Forfeiture is not built. When it is built, the default will be an invoice at 15% for commercial leases; residential is not decided.
- **Reference (for the team):** invoicing OQ-16 (remainder); rule marker R106; invoicing R66, R106, E16 to E19, E24. (Invoicing)
- **Your answer:** 

### Q102. Shareholder types for Zakat and income tax
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first Zakat computation (confirm the default)
- **Context:** Zakat applies to the Saudi and GCC share of ownership and income tax to the foreign share. The ERP classifies each shareholder.
- **Question:** Are these classes enough: Saudi, GCC, foreign, other exempt? How are listed shares and government-owned holders treated? A mid-year change of shareholders is averaged by days: confirm.
- **What the system does now:** Not built; those four classes, time-weighted.
- **Reference (for the team):** tax-returns OQ-30 **[CC-verify]**; R46; tax-returns §3.4 `ShareholderInterest`, R45, R46, AT-38. (Billing / tax / banking)
- **Your answer:** 

### Q103. Small VAT corrections: the box 14 limit
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first VAT correction
- **Context:** An error found in an earlier return can be corrected in the next return (box 14) if it is under SAR 15,000; larger errors need a voluntary disclosure within 20 days (confirmed). The ZATCA form may limit box 14 to ±SAR 5,000.
- **Question:** Which limit applies to correcting in the next return: (a) SAR 15,000 (default), or (b) SAR 5,000 as the form label suggests?
- **What the system does now:** SAR 15,000; box 14 accepts any amount below it.
- **Reference (for the team):** tax-returns OQ-13 **[CC-verify]**; tax-returns R27, AT-26. (Billing / tax / banking)
- **Your answer:** 

### Q104. Tax depreciation, deferred tax and the Zakat treatment of fixed assets — confirm the default
- **Who should answer:** Tax adviser; accountant for deferred tax in the accounts
- **Priority:** Needed before the first Zakat / income tax return under the ERP (FY2027)
- **Context:** Zakat is calculated on a base from which assets acquired **for use** (not for resale) are deducted at their net book value in the financial statements (Zakat Implementing Regulations 2024). Companies with non-Saudi shareholders also pay income tax, which uses its own pooled tax depreciation (Income Tax Law Art. 17: buildings 5%; machinery, computers and vehicles 25%; others 10%). A 2023 draft law would change this to straight line; whether it was enacted is unknown. Deferred tax (IAS 12 / section 29) arises when book and tax values differ.
- **Question:** 1. Confirm the default: book net value only, with a "held for" tag per asset (own use, rental, capital appreciation, trade) and proof of use, handed to the Zakat return. 2. Do any group companies pay income tax (non-Saudi shareholders)? If so, should the ERP keep the Art. 17 tax-depreciation pools, and is the 2023 draft law in force? 3. Is there a cap on deductible repair costs? 4. Should deferred tax be calculated in the ERP for those companies?
- **What the system does now:** Book values only, with `held_for` and evidence. No tax depreciation and no deferred tax.
- **Reference (for the team):** fixed-assets OQ-15 (compliance A13, A14, S10, N6); reporting OQ-28 (= tax-returns OQ-26); fixed-assets R34, R46; reporting §5 (Zakat lines), OQ-28; tax-returns OQ-26. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q105. VAT and withholding-tax balances at cut-over
- **Who should answer:** Tax adviser
- **Priority:** Needed before loading opening balances
- **Context:** The opening entry carries the VAT and withholding-tax balances (accounts 1310, 2200, 2220). The checks warn on each one.
- **Question:** Confirm the default: each of these balances must agree to the last VAT or withholding-tax return filed before the cut-over (plus any amount still to be filed for the last period). Anything else to check?
- **What the system does now:** Warning `tax_balance_evidence` for each balance.
- **Reference (for the team):** migration W15 team default; migration W15. (Access / web / ledger / migration)
- **Your answer:** 

### Q106. VAT due date on a weekend or holiday
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first VAT return (confirm the default)
- **Context:** The VAT return is due on the last day of the month after the period. The ERP's reminders and late-payment estimates use that date.
- **Question:** If the due date is a Friday, Saturday or public holiday, does it move to the next working day? (a) No shift (default), (b) next working day.
- **What the system does now:** No shift.
- **Reference (for the team):** tax-returns OQ-8; tax-returns R13, §10.1. (Billing / tax / banking)
- **Your answer:** 

### Q107. Vehicles: input VAT on cars (Art. 50) — when it can be recovered
- **Who should answer:** Tax adviser
- **Priority:** Needed before vehicle purchases are booked in the ERP (fixed-assets slice 2)
- **Context:** VAT IR Art. 50 blocks the input VAT on cars that are available for private use, including VAT on their repair, maintenance and fuel. It is recoverable when a car is rented out or used only for business. Blocked VAT becomes part of the car's cost. Example: a car of SAR 120,000.00 plus 18,000.00 VAT would be booked at 138,000.00 if blocked.
- **Question:** 1. Does each of our rental fleets (cars rented out on annual contracts) qualify as "rental" so that VAT is recoverable? 2. What evidence proves "exclusive business use, no private availability" for company cars? 3. Do you agree that company car purchases default to "blocked" unless the car is in a rental-fleet category or marked exclusive business use with evidence?
- **What the system does now:** Staff choose the VAT treatment per bill line in invoicing. No automatic block yet.
- **Reference (for the team):** fixed-assets OQ-26 (compliance A11, S3); fixed-assets FA1, R2, R46 (c); invoicing R29, R104. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q108. Voluntary disclosure: what the corrected return shows
- **Who should answer:** Tax adviser; then accountant
- **Priority:** Needed before the first voluntary disclosure
- **Context:** For a large error the ERP makes a new version of the old return. It copies the old boxes, adds the difference in box 14, and books nothing new at approval (the fix itself is booked by correcting entries and paid separately).
- **Question:** 1. Does ZATCA's voluntary disclosure restate all boxes 1 to 13 with corrected figures, or only show the difference?
  2. Accountant: confirm no extra offset entry is needed when the corrected version is approved.
- **What the system does now:** Box 14 carries the difference; no offset entry for amendments.
- **Reference (for the team):** tax-returns OQ-38; tax-returns R28, §10.1 "Corrections and amendments". (Billing / tax / banking)
- **Your answer:** 

### Q109. Withholding tax deadlines and late-payment rate
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first annual WHT statement
- **Context:** WHT returns are not built yet. The design uses these figures, all marked unverified.
- **Question:** Please confirm: (1) the annual WHT statement is due 120 days after the year end (60 days for partnerships); (2) late payment costs 1% for each 30 days of delay; (3) records are kept at least 10 years (the ERP keeps them forever).
- **What the system does now:** Not built; values above as defaults.
- **Reference (for the team):** tax-returns R13, R30, §3.1 `wht_late_payment_30d`, §3.3 **[CC-verify]** (the monthly due date, the 10th, is confirmed by X12(f)); tax-returns R13, R29 to R32, §3.3. (Billing / tax / banking)
- **Your answer:** 

### Q110. ZATCA servers down: sharing an uncleared invoice, and the "clearance disabled" case
- **Who should answer:** Tax adviser; then CFO to approve
- **Priority:** Needed before ZATCA Phase 2 go-live
- **Context:** The owner decided that if ZATCA's servers are down, a business invoice may be shared with the buyer uncleared after a retry window, flagged "not yet cleared", and re-shared once cleared (D-16(d)). ZATCA's guidance says such an invoice is "considered a VAT invoice", but the buyer cannot deduct its VAT until it is cleared. Separately, ZATCA can switch clearance off. It then answers "303", and business invoices go to reporting instead. The rule is written but not built until it is checked against ZATCA's documents.
- **Question:** 1. How long must the system keep retrying before sharing (ZATCA's guide mentions about 5 minutes, then retries every 15 minutes)? 2. Is the VAT due, and is the sale booked, (a) when the uncleared invoice is shared or (b) only when it is cleared (today)? 3. If clearance later rejects an invoice the buyer already holds: credit note plus new invoice, or new invoice only? 4. When ZATCA has switched clearance off and the invoice was **reported** instead: does it count as issued, and is it booked?

  *Also raised in the invoicing list:* 1. Is the VAT due, and the invoice booked, when it is shared, or only when ZATCA clears it?
  2. If ZATCA later rejects an invoice the buyer already holds, what happens?
  3. How long must the system keep retrying before sharing is allowed (the retry window)?
- **What the system does now:** It never shares an uncleared invoice. It retries and alerts. A "reported because clearance was disabled" invoice is not treated as issued and is not booked.
- **Reference (for the team):** zatca OQ-7 (and R-52) = invoicing OQ-68; also invoicing OQ-68 = zatca OQ-7 (remainder); zatca R-22, R-24, R-44, R-52; invoicing R20, R41, R107. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q111. Zakat and tax treatment of bai' ajel balances and GOSI late fines
- **Who should answer:** Tax adviser (Zakat and VAT)
- **Priority:** Needed before the first Zakat return
- **Context:** VAT on the bai' ajel sale is decided (15% on the sale price, paid by the employee). Zakat treatment of the receivable is not. GOSI late fines (2% a month) are booked on 5930.
- **Question:** 1. How are 1240 bai' ajel receivables and 1241 unearned profit treated in the Zakat base?
  2. Is the input VAT on buying the asset recoverable in full? (Default: yes, as a taxable purchase.)
  3. Are GOSI late fines deductible for Zakat / income tax, or added back? If GOSI later waives a fine, is the reversal treated the same way?
- **What the system does now:** No Zakat adjustment; input VAT recovered; fines on 5930, flagged for the adviser; a waiver reverses the expense.
- **Reference (for the team):** payroll OQ-35 (remaining zakat part and input VAT), payroll OQ-42; compliance `gosi-regulations-2026-10-19.md` (fine waiver); payroll R47, E10 to E14, OQ-35, OQ-42 (Payroll / HR)
- **Your answer:** 

### Q112. Zakat for a short or long first year
- **Who should answer:** Tax adviser
- **Priority:** Needed before the first Zakat year of a new subsidiary
- **Context:** A new subsidiary's first financial year may be shorter or longer than 12 months.
- **Question:** Is the rate scaled by days (rate × days ÷ 354 for Hijri or ÷ 365 for Gregorian, default), or is another method used?
- **What the system does now:** Not built; default as stated, flagged.
- **Reference (for the team):** tax-returns OQ-21 **[CC-verify]**; tax-returns R40. (Billing / tax / banking)
- **Your answer:** 

## Sharia board

### Q113. Early-payment discount (ibra') on a bai' ajel contract brought over from the old system
- **Who should answer:** Sharia board (may a rebate be given at all on such a contract); then tax adviser (VAT on the rebate); then CFO to approve the booking
- **Priority:** Blocks build (the rebate on contracts brought over from Falcon cannot be built until this is answered). Needed before go-live if any employee is still paying off a bai' ajel contract from Falcon at the cut-over.
- **Context:** When an employee bought an asset under bai' ajel in the old system (Falcon), the ERP takes over only the amount still owed and the profit not yet earned; there is no ERP sale invoice. For an ERP sale, an early-payment discount is issued as a credit note against the ERP invoice. The credit note goes to ZATCA and reduces the VAT already declared. A contract from Falcon has no ERP invoice for that credit note to refer to.
- **Question:** 1. May an ibra' be given on a contract brought over from Falcon?
  2. If yes, how is it documented? (a) Refuse it in the ERP. The employee pays the full price, or the rebate is handled outside the ERP. (b) The ERP issues a credit note that refers to the Falcon invoice number. This reduces VAT the same way as for ERP sales. (c) The ERP gives the rebate on the unearned profit only, with no credit note and no VAT reduction. The tax adviser must say whether that is allowed.
  3. If (b): who checks that the Falcon invoice was reported to ZATCA and gives the number to quote?
- **What the system does now:** A rebate on a migrated contract is refused (code `migrated_contract`). Profit on these contracts is still recognised as instalments are collected.
- **Reference (for the team):** invoicing OQ-80 (linked: payroll OQ-33, migration W14); invoicing R112, R128 (W14 amendment), R121; payroll OQ-33; migration W14.

---

## Blocks go-live (Invoicing)
- **Your answer:** 

### Q114. Interest or a penalty charged by a bank to a Sharia-compliant company
- **Who should answer:** Sharia board; then CFO and accountant for the booking
- **Priority:** Blocks build (booking this case)
- **Context:** In companies that follow Sharia, the "interest expense" account (5610) is blocked, so interest can never be booked as a normal cost. But a bank may still take money from the account as interest or a late penalty (for example on an overdraft). The cash has left, yet the ERP has no allowed way to record it. Today the line simply stays unexplained on the bank reconciliation, with a note and a "Sharia exception" flag.
- **Question:** How should such a bank debit be recorded?
  (a) as money owed back by the bank (a claim against the bank), until it is refunded;
  (b) as a separate, clearly named cost account (not "interest"), flagged and reported to the Sharia board;
  (c) another treatment the board prefers.
  Also: must the amount be reported or purified in any way?
- **What the system does now:** It cannot be booked. It stays outstanding on the reconciliation with a note and a `sharia_exception` flag.
- **Reference (for the team):** banking OQ-7; banking R29, §5 "not booked", AT-30. (Billing / tax / banking)
- **Your answer:** 

### Q115. Bai' ajel (deferred sale to employees): Sharia detail points
- **Who should answer:** Sharia board
- **Priority:** Needed before the first bai' ajel sale
- **Context:** The Sharia structure is approved: the company buys and takes possession first, sells at a fixed price, no late-payment increase. Several detail points run on team defaults.
- **Question:** 1. May the employee act as the company's agent (wakala) to buy the asset? (Default: no.)
  2. May the company grant a rebate (ibra') of unearned profit when the employee pays early? (Default: built, but only when approved case by case.)
  3. Profit is recognised as instalments are collected (not straight-line over time). Confirm.
  4. Who bears the asset's risk and insurance (takaful) before and after the sale?
  5. If an employee leaves with a balance: it is recovered from them (profit recognised when recovered) or written off (unearned profit reversed); no ibra' after exit. Confirm.
  6. Showing unearned profit (1241) as a deduction from the receivable (1240) on the balance sheet: is that acceptable under AAOIFI?
- **What the system does now:** The defaults above.
- **Reference (for the team):** payroll OQ-33 (detail points), payroll 3.11 / §5 1241 presentation [Needs expert sign-off: Sharia], §10.9 item 13 (ibra'), items 27, 32; related invoicing OQ-75 (profit timing, held in invoicing); payroll R47 to R49, §5 E10 to E13, 3.11, §10.9 (Payroll / HR)
- **Your answer:** 

### Q116. Interest credited by a bank: book straight to charity
- **Who should answer:** Sharia board (sign-off required by the spec); then owner
- **Priority:** Needed before go-live (confirm the default; Sharia sign-off)
- **Context:** If a conventional bank account earns interest, it must never count as income. The ERP books it straight to "Charity payable (purification)" (2600), to be given to approved charities. The owner decided purification rules apply to all companies (C1).
- **Question:** Confirm: (a) book interest received directly to the charity payable account (default); or (b) book it first to "non-compliant income" (4900), flagged, and then move it to charity. Also confirm the same treatment in companies that do not otherwise apply Sharia rules (default: yes).
- **What the system does now:** (a), in every company. Islamic profit on the purification account also goes to charity (owner C5, built).
- **Reference (for the team):** banking OQ-6; banking R28, §5 B4, AT-17.

---

## Priority: Needed before the first VAT return (Billing / tax / banking)
- **Your answer:** 

### Q117. Legacy balances on accounts Sharia does not allow
- **Who should answer:** Sharia board; then CFO
- **Priority:** Needed before loading opening balances
- **Context:** A Sharia-compliant company may still have balances in Falcon on accounts the ERP blocks (for example a conventional interest-bearing loan). The ERP refuses to load them.
- **Question:** For each such balance found: (a) keep it blocked and settle or restructure it before cut-over; (b) load it to a specific allowed account with a note (which one?); (c) other treatment set by the Sharia board? Is non-compliant income in the legacy balances to be flagged for purification?
- **What the system does now:** Blocked; a person must decide. The workbook cannot change a company's Sharia setting.
- **Reference (for the team):** migration OQ-10; migration W17, R8, OQ-10; CLAUDE.md rule 5. (Access / web / ledger / migration)
- **Your answer:** 

### Q118. Murabaha-financed assets and borrowing costs: what may be added to an asset's cost — confirm the default
- **Who should answer:** Sharia board; external auditor; then owner (Managing Partner) for the group-wide rule
- **Priority:** Needed before the first asset bought with Islamic financing
- **Context:** In a murabaha, the bank buys the asset and sells it to the company at cost plus a profit, paid over time. The asset's cost is the cash price. The bank's profit is a financing cost spread over the term (account 5600). The team default (D19) books the cash price against Islamic financing 2400 and never adds the profit to the asset's cost. Full IFRS (IAS 23.8) would normally require borrowing costs on assets that take a long time to build to be added to their cost. IFRS for SMEs (section 25) expenses them, so there is no conflict there.
- **Question:** 1. Sharia board: confirm that the murabaha deferred profit may never be part of an asset's cost, and how it is split and booked over the term (against the contra account of 2400, expensed to 5600). 2. Auditor: for a full-IFRS company with Islamic financing for a qualifying asset, is not capitalising acceptable, or is it a departure from IAS 23 that must be disclosed? 3. Owner: should "no financing cost is ever capitalised" apply to **every** group company, including full-IFRS companies that do not enforce Sharia? Today it applies only to Sharia-enforcing companies and IFRS for SMEs companies.
- **What the system does now:** Nothing is capitalised. Principal at the cash price on 2400 is allowed. Financing-cost accounts can never be added to an asset. For full-IFRS non-Sharia companies, capitalisation is simply not built.
- **Reference (for the team):** fixed-assets OQ-9, OQ-24 (compliance A1, A2, B1, S11; rule R7); fixed-assets R7, D19, FA8, AT-17. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q119. Sharia board: FX gains and losses, settlement delay, foreign-currency loans
- **Who should answer:** Sharia board
- **Priority:** Needed before go-live with foreign-currency transactions (needs Sharia board sign-off)
- **Context:** Exchange gains and losses on settling invoices and at month end are booked as ordinary income and expense, not as interest. A currency exchange is booked as spot (both sides on one date). Forward contracts and hedging are not built.
- **Question:** 1. Are FX gains and losses on settlement and on month-end revaluation ordinary income / expense (not purification)? 2. Is settlement within the usual two business days (T+2) still a spot exchange? 3. May intercompany interest-free loans (qard) in a foreign currency be revalued at month end? 4. Confirm that forwards and hedging stay out.
- **What the system does now:** 1 yes, 2 yes, 3 revalued like any monetary item, 4 not built.
- **Reference (for the team):** ledger-fx OQ-14 (and R33, R35, R36); ledger-fx R33 to R36, OQ-14. (Access / web / ledger / migration)
- **Your answer:** 

### Q120. Who decides the amount and timing of each charity (purification) payout
- **Who should answer:** Sharia board; then Managing Partner
- **Priority:** Needed before the first purification payout
- **Context:** Income that is not Sharia-compliant is set aside for charity ("purification"). Already decided: the board approves the list of charities; funding the purification bank account needs the CFO's approval; the Managing Partner is the fallback approver; any profit earned on that account also goes to charity.
- **Question:** 1. Who decides how much is paid out and when (for example the board each quarter, or the CFO within the approved list)?
  2. Is there a deadline, for example within a set number of days after the income is received?
- **What the system does now:** An accountant prepares the payout to an approved charity, and the CFO approves the funding. No timing rule exists.
- **Reference (for the team):** invoicing OQ-30 (remainder); invoicing R54, E14, E15; D34. (Invoicing)
- **Your answer:** 

### Q121. Zakat on purified income
- **Who should answer:** Sharia board; then tax adviser
- **Priority:** Needed before the first Zakat computation (confirm the default)
- **Context:** Zakat and purification (giving away non-compliant income) are kept fully separate. The question is whether amounts set aside for charity reduce the Zakat base.
- **Question:** Does purified income (moved from 4900 to the charity payable 2600) reduce the Zakat base, and is the charity payable a liability that may be deducted? (a) No (default); (b) yes.
- **What the system does now:** No; Zakat never reads 4900 or 2600.
- **Reference (for the team):** tax-returns OQ-25; tax-returns R48, AT-40. (Billing / tax / banking)
- **Your answer:** 

## Legal adviser

### Q122. Personal data in bank statements (PDPL)
- **Who should answer:** Legal adviser (PDPL)
- **Priority:** Blocks go-live
- **Context:** Bank statements contain names and IBANs of people: individual customers, employees in salary lines, and suppliers. The ERP keeps statement files encrypted in KSA indefinitely. After 10 years the personal fields are put forward for a human decision to blank them (the "burn" process the owner set). What is still missing is the legal basis for holding this data, and confirmation of who may see it.
- **Question:** 1. Under PDPL, on what legal basis may the company keep counterparty names and IBANs from bank statements (for example a legal obligation to keep accounting records, or legitimate interest)? Does anything need to be written in a privacy notice?
  2. Is it enough to hide names, IBANs and narratives from every user who does not hold the "view IBAN" right, with each unmasked view logged?
- **What the system does now:** Data encrypted and kept in KSA, masked in lists for users without `banking.view_iban`, each unmasked view logged.
- **Reference (for the team):** banking OQ-15 (retention resolved by B11(d); the lawful basis is still open) and banking OQ-23; banking R4, OQ-15 note, §3.4, AT-2. (Billing / tax / banking)
- **Your answer:** 

### Q123. Personal data: lawful basis, offshore services, privacy notice, data requests
- **Who should answer:** Legal adviser (PDPL, the Saudi Personal Data Protection Law); then the Managing Partner to approve
- **Priority:** Blocks go-live with real personal data (individual customers, individual suppliers, employees as bai' ajel buyers)
- **Context:** Invoices to individuals hold personal data: names, national IDs, addresses and IBANs. The adviser has already answered retention: keep everything, and nominate data older than 10 years for destruction. Hosting in Saudi Arabia is decided. Three points have no answer yet.
- **Question:** 1. Is "legal obligation (tax invoicing and record keeping)" the right lawful basis to record for individual customers' and suppliers' data?
  2. May any service outside Saudi Arabia receive personal data (e-mail delivery of invoices, backups, document scanning/OCR, AI models)? If yes, what transfer assessment is needed for each one?
  3. Approve the privacy notice text, in English and Arabic.
  4. How must requests from individuals be handled (access, correction, deletion), given that tax law requires the invoices to be kept?

  *Also raised in the zatca-assets-reporting list:* 1. May any processor outside Saudi Arabia receive personal data, and what transfer assessment is needed for each? 2. Please provide or approve the privacy notice text in English and Arabic. 3. What is the lawful basis for each kind of processing?
- **What the system does now:** It records "legal obligation" as the basis. No service outside Saudi Arabia receives personal data. Names of individuals are masked except for people with the right to see them, and each view is logged.
- **Reference (for the team):** invoicing OQ-31; rule markers R49, R53; also zatca OQ-20 (remainder) = invoicing OQ-31; invoicing R49, R51, R53, R86, R108; OQ-31. (Invoicing)
- **Your answer:** 

### Q124. Personal-data protection (PDPL) for employee records
- **Who should answer:** Legal adviser (PDPL); then IT / security lead for encryption and keys; Managing Partner to name the data protection officer
- **Priority:** Blocks go-live
- **Context:** HR and payroll hold sensitive personal data: IDs, salaries, bank accounts, medical leave reasons, family details. Saudi PDPL sets rules on why data may be processed, how people may ask for their data, transfers outside Saudi Arabia, and who must look after it. Retention is decided (keep forever; data more than 10 years after the end of employment is put up for a human "destroy" decision by the internal audit committee). The rest is not.
- **Question:** 1. What is the lawful basis for each kind of employee data we hold (contract, legal obligation, consent)?
  2. How must we handle an employee's request to see, correct or delete their data, and in what time?
  3. Data of employees of the Egypt (and later China, UAE) companies, and of remuneration staff abroad, is stored in Saudi Arabia: is that a cross-border transfer that needs a basis or safeguards?
  4. Who is the data protection officer?
  5. Which user roles may see unmasked salaries, IDs and bank accounts? (Default: masked unless the user's role needs it.)
  6. Who holds the encryption keys (IT lead, external provider, two people jointly)?
  7. The system screens Mudad justification texts for health words (for example "medical", "إصابة") so that medical details do not go to the Ministry. Is the team's word list acceptable?
- **What the system does now:** Personal data is encrypted and masked by default, hosted in Saudi Arabia, kept forever with a 10-year review; no data-subject request process; health-word list is the team's own.
- **Reference (for the team):** hr OQ-11 (retention part already decided); payroll `mudad.health_words` [verify: DPO] (§11.11 item 8); hr R12 to R15, R67, OQ-11; payroll §11.11 item 8, §11.12 item 6 (Payroll / HR)
- **Your answer:** 

### Q125. Value of free company housing in the end-of-service benefit
- **Who should answer:** Legal adviser (labour law); then HR / payroll manager
- **Priority:** Blocks go-live (for employees who get free company housing)
- **Context:** The EOSB is calculated on the last actual wage, which includes the value of benefits in kind such as free housing. The seed value is 3 months of basic salary a year, while GOSI values the same housing at 2 months a year. Until a person verifies the figure, the system counts the housing as **zero** in the EOSB, which would underpay the employee. Example: basic SAR 10,000: 3 months a year adds SAR 2,500 a month to the EOSB wage, 2 months adds SAR 1,666.67.
- **Question:** 1. What value should free company housing have in the EOSB wage: (a) 2 months of basic a year, the same as GOSI (team recommendation); (b) 3 months of basic a year; (c) the actual rental value or cost to the company; (d) another rule?
  2. Does the same value apply to leave paid out at exit and to pay in lieu of notice (the system uses one wage for all three)?
- **What the system does now:** Counts zero with an English / Arabic warning on the termination, the provision payslip and the report line until the row is verified.
- **Reference (for the team):** payroll OQ-62 (and `eosb.in_kind_housing` [verify: legal], §11.11 C4, §11.12 item 1); payroll R34, R76, §11.11 C4, §11.12 items 1 and 2; hr R39

---

## Needed before first payroll (or the first event named) (Payroll / HR)
- **Your answer:** 

### Q126. Who may read the access log
- **Who should answer:** Legal adviser (PDPL); then Managing Partner
- **Priority:** Blocks go-live
- **Context:** The access log records every sign-in, grant, refusal and approval, with names, so it holds personal data under the Saudi Personal Data Protection Law (PDPL). How long it is kept is decided (kept indefinitely; personal data nominated for destruction after 10 years, decided by the internal audit committee). Who may read it is not.
- **Question:** Who may read the access log: (a) the Managing Partner and each company's access admin for their own company only (team default); (b) also the internal auditor and the external auditor; (c) others (name them)? Must each reading be logged?
- **What the system does now:** No reading screen; the default in the spec is (a).
- **Reference (for the team):** access OQ-10 (readers part); access R45, R55, OQ-10. (Access / web / ledger / migration)
- **Your answer:** 

### Q127. Withholding tax records of individuals abroad (PDPL)
- **Who should answer:** Legal adviser (PDPL)
- **Priority:** Blocks go-live (the withholding tax part)
- **Context:** When the company pays a non-resident (a foreign supplier, or a person paid through payroll), it withholds tax and must give ZATCA a monthly return, an annual list of beneficiaries (name, address, registration number) and a certificate for each beneficiary. For individuals this is personal data, some of it about people outside KSA.
- **Question:** 1. What is the legal basis for holding and sending these details to ZATCA?
  2. Are there limits on sending a certificate to an individual abroad (for example by e-mail)?
  3. Is masking these details from everyone except holders of the "view personal data" right, with each view logged, enough?
- **What the system does now:** Not built yet. The design masks names and IDs unless the user holds `invoicing.view_personal_data`, and logs each view.
- **Reference (for the team):** tax-returns OQ-17; tax-returns R31, R54, AT-45. (Billing / tax / banking)
- **Your answer:** 

### Q128. External auditor's access
- **Who should answer:** Legal adviser (PDPL); then Managing Partner
- **Priority:** Needed before the first external audit
- **Context:** An external-auditor role exists: read-only and time-limited. Customer invoices to individuals (B2C) contain personal data that the ERP masks for viewers.
- **Question:** 1. Maximum length of an external-auditor access (default 12 months). 2. Personal data: (a) masked (default) or (b) visible? 3. Exports (ZATCA invoice archive, CSV): (a) none (default), (b) allowed and logged? 4. Who grants it: the company's top authority (default)?
- **What the system does now:** End date required (max 12 months), masked, no exports, granted by the top authority.
- **Reference (for the team):** access OQ-36, OQ-7 (remaining part); access R51, R60, OQ-7, OQ-36, §10.9 note. (Access / web / ledger / migration)
- **Your answer:** 

### Q129. Salary deductions for loans and bai' ajel: legal remainder
- **Who should answer:** Legal adviser (labour law)
- **Priority:** Needed before the first loan or bai' ajel deduction
- **Context:** Decided: loans may be deducted from final dues including EOSB; monthly deductions respect the 10% (loans) and 50% (all deductions) caps; bai' ajel instalments need written consent and fit in the 50% cap. Two points remain.
- **Question:** 1. The 10% loan cap is measured on the actual wage and the 50% total cap on the wage due for the month. Correct?
  2. A bai' ajel balance at exit when the contract has no signed undertaking: may it be deducted from non-EOSB final dues only (default), and is anything else allowed beyond asking the former employee to pay?
- **What the system does now:** The two bases above; without the undertaking, recovery from non-EOSB final dues only, then a payment by the former employee or a write-off; no penalty.
- **Reference (for the team):** payroll OQ-34 (remaining (e) and recovery) and hr OQ-32 (same question); payroll R18, R28, R49; hr R40 (Payroll / HR)
- **Your answer:** 

### Q130. Saudi remuneration staff under the old GOSI law: hazards and SANED abroad, and fines
- **Who should answer:** Legal adviser (social insurance)
- **Priority:** Needed before the first out-of-country payroll with a Saudi who joined GOSI before 3 July 2024
- **Context:** Decided: subordinate Saudi remuneration staff are registered for GOSI pension; board members and consultants are not. For people under the old law, the old text may also cover occupational hazards and unemployment insurance abroad.
- **Question:** 1. For a subordinate Saudi remuneration person under the old law (M/33), are occupational hazards and SANED also due while abroad, or pension only?
  2. Which fines apply if we register someone who should not be, or fail to register someone who should (old Art. 62, and the August 2024 fine schedule items 1 and 4)?
- **What the system does now:** Pension only, with a warning on the run.
- **Reference (for the team):** payroll OQ-40 (c), (d); payroll R41, R62, OQ-40; hr R69 (Payroll / HR)
- **Your answer:** 

### Q131. Secondment between two Saudi group companies: who pays GOSI
- **Who should answer:** Legal adviser (social insurance)
- **Priority:** Needed before the first payroll with an employee seconded between two Saudi group companies
- **Context:** Group companies second staff to each other and bill for them. The GOSI Regulations say that for a "secondment" (إعارة) the receiving employer registers and pays GOSI. If our billing secondments fall under this, the wrong company is paying GOSI today.
- **Question:** 1. Is a billed secondment between two Saudi group companies an "إعارة" under GOSI Regulations Art. 20(3)(a), so that the receiving company must register and pay GOSI?
  2. Does the same hold for employees under the old law?
- **What the system does now:** The home company keeps paying GOSI; the run and the secondment cost report show a warning.
- **Reference (for the team):** payroll OQ-48 (mirrored in hr "slice 1 additions"); payroll §10.3, R63, OQ-48; billing R71 (Payroll / HR)
- **Your answer:** 

### Q132. Statutory reserve, and each company's articles of association
- **Who should answer:** Legal adviser (Companies Law); accountant; owner to supply each company's articles of association
- **Priority:** Needed before the first year-end close (31 December 2027)
- **Context:** The older Companies Law required companies to transfer 10% of profit each year to a statutory reserve until it reached 30% of capital. The 2022 Companies Law (Royal Decree M/132) may leave this to each company's articles of association. Example: profit 1,148,100.00 × 10% = 114,810.00 moved from retained earnings 3300 to reserve 3200.
- **Question:** 1. Does the current Companies Law still require a statutory reserve, or only the company's articles? 2. Per company: what % and what cap (as % of capital)? 3. Is the transfer dated at the year end or at the shareholders' resolution date? 4. Owner: please provide each company's current articles of association.
- **What the system does now:** 0%, no transfer.
- **Reference (for the team):** reporting OQ-6 (the articles-of-association part of reporting OQ-2); reporting R25, Y2, §3.1. (ZATCA / fixed assets / reporting)
- **Your answer:** 

## Egypt adviser (tax and labour)

### Q133. Egypt payroll and HR rules (all Egypt figures still unverified)
- **Who should answer:** Egypt adviser (Egyptian payroll accountant and labour lawyer); then CFO for sub-question 6
- **Priority:** Blocks go-live (Egypt company only; Saudi payroll not affected)
- **Context:** The owner decided local rules apply and only Egypt is activated. The team researched Egypt's Labour Law 14/2025, Social Insurance Law 148/2019 and salary tax from secondary sources, and found conflicts between them. The Egypt payroll is built, but a run is refused at approval while any Egypt figure it used is unverified. Egypt terminations and off-cycle runs are refused for now.
- **Question:** 1. Please confirm or correct each row of the research table (`docs/compliance/egypt-payroll-2026-10-18.md` §1), and settle the twelve conflicts in §3 (for example: social insurance due on the 1st or 15th of the next month; how the 18.75% employer rate splits by branch; sick pay; first-year leave; who bears the Martyrs' Fund levy; the minimum wage EGP 7,000 or EGP 8,000).
  2. Does the "fewer than 10 workers" minimum-wage exemption apply to our Egypt company?
  3. Termination: is there really no statutory end-of-service payment for an ordinary resignation or contract end? What are the compensation formulas and the employee's notice period?
  4. Overtime: is it calculated on basic or total wage, and what are the rest-day and public-holiday premiums? (Overtime is refused for Egypt until answered.)
  5. Which filings and payment channels apply (NOSI e-filing, ETA monthly or quarterly forms), so the team can add those payments?
  6. CFO: should Egypt social insurance, salary tax, Martyrs' Fund and health insurance have their own accounts, or stay on the group accounts used now (2340 / 5110, 2220, 2300)?
- **What the system does now:** Egypt runs calculate with a warning and are refused at submission and approval while any Egypt row is unverified; the minimum-wage check only warns; overtime, terminations and off-cycle runs for Egypt are refused.
- **Reference (for the team):** payroll OQ-28 and hr OQ-26 (same question); payroll §10.8 item 7 (NOSI / ETA payments not built); hr §"slice 2 additions" (minimum wage, "fewer than 10 workers"); compliance `egypt-payroll-2026-10-18.md` §3 (conflicts C1 to C12) and §4 (questions 1 to 17); owner answers E4, decision pack B3; payroll R4, R41, §10.8 item 7, §11; hr R51, R68, 3.9 (EG rows) (Payroll / HR)
- **Your answer:** 

### Q134. Egypt salary tax method
- **Who should answer:** Egypt adviser (tax)
- **Priority:** Needed before the first Egypt payroll
- **Context:** Egypt salary tax is withheld monthly by projecting the year (cumulative method). The team chose how to treat bonuses and part years.
- **Question:** 1. Regular pay is annualised (year-to-date × 12 / months); bonuses and one-offs are added once and their tax is due in full in the month paid. Correct?
  2. For a mid-year joiner or months without pay, the system annualises over the months paid, which can over-withhold (corrected by the annual employer settlement). Is a months-worked basis required instead?
  3. How is the 13th month taxed? If tax was over-withheld during the year, may it be refunded in a later month, or only in the year-end settlement? (Default: no refund in an off-cycle run; true-up in the next regular run.)
- **What the system does now:** As above.
- **Reference (for the team):** payroll OQ-57, OQ-51 (tax parts); compliance `egypt-payroll-2026-10-18.md` §4 item 6; payroll R73, §11.11 C1, §11.12 item 8 (Payroll / HR)
- **Your answer:** 

### Q135. Egypt social insurance wage
- **Who should answer:** Egypt adviser (social insurance)
- **Priority:** Needed before the first Egypt payroll
- **Context:** Egyptian social insurance is charged on the "insurable wage", within yearly limits.
- **Question:** 1. Is the insurable wage the wage of each month (default), or the wage in force on 1 January (or a later joiner's first day) for the whole year?
  2. Which allowances and bonuses are insurable?
- **What the system does now:** Monthly wage; each component's own flag decides.
- **Reference (for the team):** payroll OQ-58, OQ-51 (insurance part); compliance `egypt-payroll-2026-10-18.md` §4 items 2, 5; payroll §11.11 C10, `si.wage_reference`, `si.insurable_components` (Payroll / HR)
- **Your answer:** 

### Q136. Egypt: value of untaken leave paid at exit
- **Who should answer:** Egypt adviser (labour law)
- **Priority:** Needed before the first Egypt leaver (and the first Egypt leave report)
- **Context:** Egypt's Labour Law 14/2025 pays cash for untaken leave on exit. The system does not know which wage and day divisor to use.
- **Question:** Which wage (basic or total) and which divisor (26, 30 or calendar days) value untaken leave at exit?
- **What the system does now:** Leave days are shown but not valued.
- **Reference (for the team):** payroll OQ-55 (`leave.encashment_basis` EG); payroll R76

---

## Low (default is safe) (Payroll / HR)
- **Your answer:** 

## HR / payroll manager

### Q137. Remaining GOSI rules not found in the law or the Implementing Regulations
- **Who should answer:** HR / payroll manager (ask GOSI directly or a GOSI consultant); legal adviser for sub-question 8
- **Priority:** Blocks go-live (the related rows cannot be verified until answered)
- **Context:** The team read the new Social Insurance Law, its Implementing Regulations and GOSI's booklets. Some points are in none of them, and the system uses a stated assumption for each. A wrong assumption gives wrong GOSI amounts on every payslip it touches, and GOSI fines per employee.
- **Question:** 1. The pension contribution for new-law joiners rises by 1% a year (18%, 19%, 20%, 21%, 22%). Does each step start in **July** or in **August** (2025 to 2028)? (Default: August.)
  2. For the month someone joins or leaves, GOSI is charged for the days worked. Is the month counted as its calendar days, or always 30? (Default: calendar days.)
  3. An employee reaching the legal retirement age: from which month does unemployment insurance (SANED) stop? (Default: the first month after the birthday month.)
  4. Hourly-paid workers: what are GOSI's rules for their contributory wage? (Default: hourly-paid workers are refused until known.)
  5. Do contributions continue during **unauthorised** unpaid absence? (Default: yes, the wage is not reduced.)
  6. Minimum contributory wage: is it SAR 1,500 for Saudis and SAR 400 for non-Saudis, as GOSI's Form 7 says? (Default: yes.)
  7. Has GOSI's board phased in any branch later, or added any compulsory product for non-Saudis? (Default: none.)
  8. For employees under the old law (registered before 3 July 2024 with past contributions), may the new Regulations' procedural rules (abroad periods, commission averaging, joining-month days, the cross-employer cap, corrections) be applied by analogy, or are the old Regulations needed? (Legal.)
  9. For the record only: the number and date of the decision setting SANED at 1.5%, and which law the August 2024 violations fine schedule is issued under.
- **What the system does now:** Uses each default above, stored as unverified rows; the system names the row on each run.
- **Reference (for the team):** payroll OQ-9 (remaining items 1, 5, 9, 11, 12), payroll R66 `gosi.partial_month_divisor`, R65 hourly rule, hr 3.9 `gosi.annuities_step_start`, `gosi.saned_stop_month_rule`, `gosi.min_wage`, `gosi.non_saudi_products`, `gosi.violation_schedule`; compliance reports `gosi-regulations-2026-10-19.md` "Still Unclear" 1, 2, 4 to 7, 10, 11 and "Needs expert sign-off" (legacy by analogy); `gosi-check-legacy-2026-10-17.md` "Still Unclear" 3, 9; payroll R20 to R22, R55, R60, R64 to R68, §10.3, §6 GOSI row; hr 3.9 GOSI rows (Payroll / HR)
- **Your answer:** 

### Q138. What counts as the GOSI wage
- **Who should answer:** HR / payroll manager (confirm with GOSI); then tax adviser for sub-question 3 (exchange rate)
- **Priority:** Blocks go-live
- **Context:** GOSI is charged on basic salary plus cash housing and commissions; other allowances are excluded. A few cases are not spelled out in the texts we have. Each affects GOSI on some payslips.
- **Question:** 1. Free company housing (housing in kind): GOSI's form says it is valued at "two months' basic salary". Does that mean two months **per year** (monthly value = basic × 2 / 12, the default), or something else?
  2. Is GOSI charged on leave paid out at exit, on pay in lieu of notice, or on the EOSB? (Default: no, on none of them.)
  3. A Saudi remuneration employee working abroad and paid in a foreign currency (for example USD): is the whole remuneration the GOSI wage, and which SAR exchange rate and date apply? (Default: the whole remuneration, at the payroll run's rate on its entry date, within the floor and cap.)
  4. Bonuses and one-off payments to such Saudi remuneration staff: are they part of the GOSI wage? (Default: no; only the monthly remuneration counts.)
- **What the system does now:** Each default above.
- **Reference (for the team):** payroll OQ-24, OQ-45, OQ-50, OQ-9 item 3a (in-kind housing "per year") and item 8; hr 3.9 `gosi.housing_in_kind_basic_months`; compliance `gosi-regulations-2026-10-19.md` "Still Unclear" 3, 8; payroll R20, R34, R62, R71, OQ-24, OQ-45, OQ-50; hr 3.3, 3.9 (Payroll / HR)
- **Your answer:** 

### Q139. GOSI leaving reasons for each type of termination
- **Who should answer:** HR / payroll manager (copy GOSI's list from the GOSI portal)
- **Priority:** Needed before the first employee leaves
- **Context:** When an employee leaves, GOSI must be told the reason, chosen from GOSI's list. A wrong reason is fined per employee.
- **Question:** Please send GOSI's list of leaving (exclusion) reasons, and say which reason applies to each termination type in the ERP (resignation, end of contract, dismissal under Art. 80, mutual agreement, retirement, death, closure, and so on).
- **What the system does now:** No mapping; the leaver checklist asks the HR officer to choose the reason on the GOSI portal and record it.
- **Reference (for the team):** hr OQ-42 (`gosi.exclusion_reason_map`); hr R65, 3.9, OQ-42 (Payroll / HR)
- **Your answer:** 

### Q140. Mudad justification options and who approves them
- **Who should answer:** HR / payroll manager (copy the list from the Mudad portal)
- **Priority:** Needed before the first Mudad upload
- **Context:** When a worker is paid less than the contract wage or late, Mudad asks the company to justify it within 10 days by choosing a reason from a drop-down list on the portal. The compliance check confirmed justifications are made on the portal only, not in the bank file. The system keeps an internal reason list and a worksheet for the officer, but it does not have Mudad's official option list.
- **Question:** 1. Please send the current Mudad justification drop-down options (Arabic and English) so the team can map each internal reason to one.
  2. Confirm that the HR officer approves justifications before they are entered on the portal.
- **What the system does now:** Internal reasons with empty Mudad options and a warning; the worksheet export works; the HR officer approves.
- **Reference (for the team):** payroll OQ-56; `wps.mudad.justification_options` [verify: Mudad option list]; §11.6 [verify] categories; payroll R79 to R81, §11.6, §11.11 C2, C3 (Payroll / HR)
- **Your answer:** 

### Q141. Salary components and grades for each company (confirm the default)
- **Who should answer:** HR / payroll manager; then CFO
- **Priority:** Needed before first payroll (for each company)
- **Context:** Each company lists its pay components and, for each, whether it counts for GOSI, EOSB, overtime and unpaid-leave deductions. Grades decide who may get overtime (P11: not for middle management and management).
- **Question:** 1. Confirm the seed components per company: BASIC, HOUSING, TRANSPORT, OTHER, COMMISSION, PROFIT_SHARE, BONUS with their flags (GOSI: basic, cash housing, commission, profit share; EOSB: all regular pay), or list each company's own components.
  2. List each company's grades and mark which are "middle management and management" (no overtime).
  3. Should grades drive anything else (30 days' leave, loan limits)? (Default: no.)
- **What the system does now:** Seed components with the flags above; each company defines grades with an explicit "overtime eligible" flag.
- **Reference (for the team):** hr OQ-8, hr OQ-29; hr 3.3, 3.12, R55; payroll R19, R20, R45 (Payroll / HR)
- **Your answer:** 

## IT / security lead

### Q142. Signing keys: backup, disaster recovery and "non-exportable" keys
- **Who should answer:** IT / security lead
- **Priority:** Blocks go-live
- **Context:** Each EGS unit signs every invoice with a private key held in OpenBao, an open-source secret store hosted in Saudi Arabia (owner decision D-3). The owner chose a key hierarchy: one group "super key" held by the Managing Partner, then one master key per company protecting that company's signing keys (D-12). ZATCA requires signing keys to be "non-exportable" and, when held in software, on encrypted disk. After a certificate renewal, the old key and secret versions are now "retired": blocked but never destroyed. That rule was adopted on the team's recommendation (D-19).
- **Question:** 1. How is OpenBao backed up and restored? When is the first disaster-recovery test? 2. How does the design meet "non-exportable keys": (a) a hardware security module or key service that signs inside it, or (b) keys kept in the store's key-value area, wrapped by the company master key and read out to sign? The second option is what is built. 3. How are the super key and the company master keys rotated, and how often? 4. Confirm the default: retirement is recorded as a version list in the store's metadata, enforced by store permissions so that nothing can be deleted from the ERP. Compliance secrets are retired at the first renewal, not as soon as the unit goes live. Is this acceptable? 5. Transit encryption has no secp256k1 key type (the curve ZATCA uses), so signing keys stay in the key-value store, wrapped. Please confirm.
- **What the system does now:** Tests use an in-memory fake store. The OpenBao adapter is built for retirement only. Reading or unwrapping key material and the key hierarchy refuse to run ("fail closed") until this sign-off.
- **Reference (for the team):** zatca OQ-3 (remainder), OQ-34 (a), (d), (f); zatca R-6, R-47, R-50, R-57 to R-60, §3.10, OQ-34 (f) default text; `deploy/openbao/`. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q143. Who must use MFA (two-step sign-in)
- **Who should answer:** IT / security lead; then Managing Partner to approve
- **Priority:** Blocks go-live
- **Context:** MFA stops someone with a stolen password from signing in. The team has switched it on for everyone who can approve, post or change access, and asks for a fresh code at every approval. Accountants, clerks and read-only viewers sign in with a password only.
- **Question:** Confirm the default: MFA is required for (1) superusers; (2) the roles approver (CFO), managing partner, chairman, CEO, ZATCA admin and access admin; (3) every other person holding any approval right in any company (e.g. HR officer, department head, payroll officer); (4) every person acting as a delegate approver. Should it also apply to (a) accountants and clerks, (b) everyone, or (c) stay as above?
- **What the system does now:** Exactly the list above; a fresh code is asked at every approval in the inbox and before the admin "post" and "reverse" actions. The deploy check refuses to start in production with MFA switched off.
- **Reference (for the team):** access OQ-4, web OQ-3, web OQ-29 (duplicates); access OQ-4; web W-D6, R13 to R16, OQ-3, OQ-29, slice 2 "MFA", verification fixes 1 and 4. (Access / web / ledger / migration)
- **Your answer:** 

## Bank relationship managers

### Q144. Salary (WPS) file formats of the five salary banks
- **Who should answer:** Bank relationship manager at each bank (Riyad Bank, SNB, Al Rajhi Bank, Bank AlJazira, Alinma Bank); then IT / security lead for how the file is sent
- **Priority:** Blocks build (the bank salary file only; the rest of payroll runs)
- **Context:** Each month the salaries are paid through a salary file in the bank's own layout, and the bank's processed file is then uploaded to Mudad. The Ministry's required fields are known, but each bank's exact layout is not. Without the bank layouts the ERP cannot produce a file the bank will accept, so salaries would have to be keyed into the bank portal by hand.
- **Question:** 1. Please obtain from each of the five banks its WPS / payroll file specification: layout, character encoding, file name rules and a sample file.
  2. Does the bank's file have a "total deductions" column, and what is it called? (The system uses a column named `DEDUCTIONS` until told otherwise.)
  3. What bank code (BIC / SWIFT code) does each bank expect for itself and for each beneficiary bank?
  4. What codes does the bank's returned (processed) file use for success and for each kind of rejection?
  5. Is there any upload method other than the bank portal (for example a host-to-host link), and does Mudad offer any API, or is the upload always manual?
- **What the system does now:** It writes a generic file with the Ministry fields, a `DEDUCTIONS` column and an unconfirmed bank-code table; no bank-specific file is produced. Files are kept forever (decided).
- **Reference (for the team):** payroll OQ-10 (also the unverified `wps.format.<bank>` and `wps.bank_codes` parameters, payroll §10.5 WPS note, §11.10 bank return codes); decision pack B2; owner answer E3; payroll R31, R32, R51, R79, §10.5 (WPS), §11.10, hr 3.9 (`wps.format.<bank>`)

---

## Blocks go-live (Payroll / HR)
- **Your answer:** 

## Other

### Q145. Arabic texts of the screens need a native reviewer
- **Who should answer:** Arabic reviewer (to be named by the Managing Partner)
- **Priority:** Blocks go-live
- **Context:** Every screen text exists in English and Arabic. The Arabic was written by the team and is marked for review, including the new role names (e.g. "ممثل مجلس الإدارة" board representative, "مصفوفة الصلاحيات" authority matrix, "تقرير الحوكمة الشهري" monthly governance report, "اعتماد ذاتي" self-approval) and the Arabic in the PDF governance report.
- **Question:** Who reviews the Arabic texts, and by when?
- **What the system does now:** Texts marked [AR-review] in `web/texts.py` and the access spec.
- **Reference (for the team):** web OQ-24; web OQ-24; access §10.9 note, §10.10 item 12. (Access / web / ledger / migration)
- **Your answer:** 

# Part 2: low priority, a safe default is already in use

## Owner / Managing Partner

### Q146. A company with a unified number but no commercial registration number
- **Who should answer:** Project owner (Managing Partner)
- **Priority:** Low (default is safe)
- **Context:** The system requires a CR number before a company can issue invoices. The spec also describes a company that has only a unified national number (starting with 7), but such a company could never issue.
- **Question:** Does any group company have a unified number but no CR? If yes, it needs a rule change.
- **What the system does now:** A CR number is required to issue.
- **Reference (for the team):** compliance report row 4c (no OQ number; linked AT-66); invoicing R72, AT-66; compliance report row 4c. (Invoicing)
- **Your answer:** 

### Q147. AI agents and reports
- **Who should answer:** Owner (Managing Partner)
- **Priority:** Low (default is safe)
- **Context:** Future AI agents inside the ERP may only act through the agent gateway as named service users, and only create drafts.
- **Question:** 1. May agents export reports (CSV, XLSX, PDF)? 2. May agents read approved group consolidations?
- **What the system does now:** Agents may make draft and preview reports and read drill-down. They cannot export, finalise or close. No consolidation rights unless granted, and never prepare or approve.
- **Reference (for the team):** reporting OQ-22, consolidation OQ-22; reporting R36; consolidation R32. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q148. AI agents never post
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Two documents disagree.
- **Question:** Confirm: AI agents never approve, post or issue, so the agent-gateway document (which allows agent posting up to an amount limit) is amended to match.
- **What the system does now:** Agents can only create drafts.
- **Reference (for the team):** access OQ-14; access A7, R20, R21, OQ-14; `docs/ai-agent-gateway.md` rule 3. (Access / web / ledger / migration)
- **Your answer:** 

### Q149. Approval inbox: one company at a time or all together
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** The spec default was (b); the build brief chose (a).
- **Question:** Should the inbox show (a) the current company only (as built), or (b) all the user's companies at once, grouped by company (original proposal)?
- **What the system does now:** (a).
- **Reference (for the team):** web OQ-13; web R39, OQ-13, slice 2 notes. (Access / web / ledger / migration)
- **Your answer:** 

### Q150. Arabic exports: digits and dates
- **Who should answer:** Owner (Managing Partner)
- **Priority:** Low (default is safe)
- **Context:** Arabic reports can use Western digits (123) or Arabic-Indic digits (١٢٣), and may show Hijri dates.
- **Question:** For Arabic exports: (a) Western or Arabic-Indic digits? (b) Gregorian dates with the Hijri date in the header (default), or Hijri throughout?
- **What the system does now:** Western digits; Gregorian, with Hijri in the header.
- **Reference (for the team):** reporting OQ-21; reporting R27. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q151. Chairman role: grant, absence, what they see
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Switching Sharia enforcement off needs the MP and then the Chairman.
- **Question:** Confirm: (1) only the MP grants or removes the Chairman role; (2) no delegate: if the Chairman is away the request waits or expires; (3) the Chairman sees only the request (company, change, reason, requester, expiry), not other data.
- **What the system does now:** As stated.
- **Reference (for the team):** access OQ-24, web OQ-22; access R47, R48, OQ-24; web OQ-22. (Access / web / ledger / migration)
- **Your answer:** 

### Q152. Companies with more than one branch
- **Who should answer:** Project owner (Managing Partner)
- **Priority:** Low (default is safe). **Confirm the default.**
- **Context:** A company with several branches may have a commercial registration (CR) and an e-invoicing unit per branch, and ZATCA wants that branch's CR on its invoices.
- **Question:** Does any group company invoice from more than one branch?
- **What the system does now:** One branch per company, with the CR on the company record.
- **Reference (for the team):** invoicing OQ-35; invoicing §3.0, R72; compliance report row 4c. (Invoicing)
- **Your answer:** 

### Q153. Cost centres from Falcon
- **Who should answer:** Managing Partner (project owner); then CFO
- **Priority:** Low (default is safe)
- **Context:** Falcon lines have a cost centre. The ERP has no cost-centre field yet.
- **Question:** Confirm the default: keep the cost centre only in the line description for now. Are cost centres needed as a reporting dimension later?
- **What the system does now:** Description only.
- **Reference (for the team):** migration OQ-6; migration R15, OQ-6. (Access / web / ledger / migration)
- **Your answer:** 

### Q154. Dates, digits and language defaults
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Display preferences.
- **Question:** Confirm: (1) dates shown Gregorian with Hijri beside it in every company; tax and ZATCA dates Gregorian first; HR Hijri contracts Hijri first; (2) Latin digits by default, Arabic-Indic digits as a personal choice in the Arabic screens only; (3) a new user's language: (a) English until they change it (as now), or (b) Arabic?
- **What the system does now:** (1) and (2) as stated; (3) English after sign-in, Arabic before sign-in.
- **Reference (for the team):** web OQ-6, OQ-7, OQ-26; web R31 to R33, OQ-6, OQ-7, OQ-26. (Access / web / ledger / migration)
- **Your answer:** 

### Q155. Delegation of approval: maximum length
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** An approver may hand their approvals to another person while away.
- **Question:** Confirm: a delegation lasts at most 90 days, renewable as a new logged delegation, with a reminder 3 days before it ends. ZATCA key custody delegations: end date optional.
- **What the system does now:** As stated for approvals.
- **Reference (for the team):** access OQ-45, OQ-16 (merged); access R62, R87, R88, OQ-16, OQ-45. (Access / web / ledger / migration)
- **Your answer:** 

### Q156. Disciplinary fines
- **Who should answer:** Managing Partner; then legal adviser
- **Priority:** Low
- **Context:** The law allows fines (max 5 days' wage per violation and per month), but they must go to a workers' fund used for the workers' benefit, never to company income.
- **Question:** Should the ERP support disciplinary fines at all? (Default: no.)
- **What the system does now:** Not supported.
- **Reference (for the team):** payroll OQ-8; payroll R54, OQ-8 (Payroll / HR)
- **Your answer:** 

### Q157. Does the CFO keep approval rights before a matrix is entered
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Authority lies with the CEO (A1), but the CFO approved most documents before.
- **Question:** Confirm the default: where a company has a CEO and a CFO, the CFO keeps today's approvals and the CEO is added as an equally eligible approver, until the matrix says otherwise.
- **What the system does now:** As the default.
- **Reference (for the team):** access OQ-41; access OQ-41, §10.10 item 8. (Access / web / ledger / migration)
- **Your answer:** 

### Q158. Dual-control requests: expiry and certificate revocation
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Some actions (Sharia switch-off, ZATCA production) need two people.
- **Question:** Confirm: (1) a four-eyes request expires after 72 hours (matrix changes after 30 days); (2) revoking a ZATCA certificate needs no second person; (3) four-eyes requests appear in the approval inbox but keep their own stricter rules.
- **What the system does now:** As stated; not yet shown in the inbox.
- **Reference (for the team):** access OQ-11, OQ-49; access R18, OQ-11, OQ-49, §10.10 item 4. (Access / web / ledger / migration)
- **Your answer:** 

### Q159. Employee transfers, rehires and changes of basis between group companies (confirm the default)
- **Who should answer:** Managing Partner; then accountant
- **Priority:** Low (needed before the first transfer between group companies)
- **Context:** When an employee moves from one group company to another, or changes from remuneration staff to a Saudi employee, the question is whether service continues and who carries the EOSB.
- **Question:** Confirm the default: the person is settled (EOSB paid) on leaving the first company and starts new service in the second; no continuity and no transfer of the provision between companies. Or: (b) service continues and the provision moves to the new company by an intercompany entry.
- **What the system does now:** Settled on leaving; new employee record in the new company.
- **Reference (for the team):** hr OQ-14, hr OQ-27; hr OQ-14, OQ-27, R49 (Payroll / HR)
- **Your answer:** 

### Q160. Governance report recipients
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** The report lists self-approvals, delegations, matrix changes and approvals under default rules.
- **Question:** Confirm: the monthly governance report is read in the ERP by the company's internal auditor, top authority, board representative and (while valid) external auditor; the MP sees a subsidiary's report only with access there; the internal auditor exports PDF/CSV for the audit committee or board on request, each export logged. Should it also be sent automatically to anyone outside the ERP?
- **What the system does now:** As stated, in-app only.
- **Reference (for the team):** access OQ-33, OQ-54; access R84 to R86, OQ-33, OQ-54, §10.10 item 7. (Access / web / ledger / migration)
- **Your answer:** 

### Q161. Group presentation currency
- **Who should answer:** Owner (Managing Partner)
- **Priority:** Low (default is safe)
- **Context:** The group accounts must be presented in one currency.
- **Question:** Confirm that the group presents its accounts in SAR.

  *Also raised in the access-web-ledger-migration list:* Confirm SAR as the group reporting currency.
- **What the system does now:** SAR.
- **Reference (for the team):** consolidation OQ-14 = ledger-fx OQ-7; also ledger-fx OQ-7; consolidation D4, §1; reporting RD5. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q162. Late approval: approver's access ended before ZATCA clearance
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Sales invoices post when ZATCA clears them, which can be later than approval.
- **Question:** Confirm: an invoice approved by someone whose access ended before ZATCA cleared it still posts in their name (the right was checked at approval).
- **What the system does now:** As stated.
- **Reference (for the team):** access OQ-20; access R42, OQ-20. (Access / web / ledger / migration)
- **Your answer:** 

### Q163. Leave policy above the legal minimum (confirm the default)
- **Who should answer:** Managing Partner; then HR / payroll manager; legal for sub-question 4
- **Priority:** Low
- **Context:** Decided: 21 days' annual leave, 30 for some, never more than 30 (P13); no cash for leave during service (the law forbids it).
- **Question:** 1. Who gets 30 days: by grade, by contract, or by years of service? (Default: set per contract and approved with it.)
  2. Can unused leave be carried forward, and is any of it lost? Can an employee go into a negative balance? (Default: carried forward, never lost, no negative balance.)
  3. Does the 30-day ceiling also apply to Egypt employees where local law gives more? (Default: no, Saudi employees only.)
  4. Legal: are leave days counted as calendar days or working days? (Default: calendar days.)
- **What the system does now:** As above.
- **Reference (for the team):** hr OQ-5, hr OQ-31; hr 3.9 `leave.counting_basis` [CC-verify]; hr R30 to R32, R56, 3.9 (Payroll / HR)
- **Your answer:** 

### Q164. Loans and bai' ajel in subsidiaries
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Earlier the MP approved all employee loans and bai' ajel (instalment sales to employees); later authority was put with each company's CEO.
- **Question:** Confirm the default: Almiras = MP; other companies = their CEO, until their matrix says otherwise.
- **What the system does now:** As the default.
- **Reference (for the team):** access OQ-39; access A22, A24, OQ-39. (Access / web / ledger / migration)
- **Your answer:** 

### Q165. Logging changes to a company's ownership %
- **Who should answer:** Owner (Managing Partner)
- **Priority:** Low (default is safe)
- **Context:** A company's ownership % drives the minority share. Some sensitive company settings are logged in a special list.
- **Question:** Should a change of a company's ownership % be in the logged sensitive-settings list?
- **What the system does now:** It can change only through an approved ownership record, and that change is logged.
- **Reference (for the team):** consolidation OQ-19; consolidation R4, AT-1; access R32. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q166. May AI agents read or draft HR and payroll data?
- **Who should answer:** Managing Partner; then legal adviser (PDPL)
- **Priority:** Low
- **Context:** Future AI agents in the ERP can only create drafts. HR data is sensitive personal data.
- **Question:** May agents ever read HR data (for example reading scanned documents) or draft payroll inputs (for example importing overtime)? (Default: no.)
- **What the system does now:** No agent access to HR or payroll.
- **Reference (for the team):** payroll OQ-21, hr OQ-12; payroll OQ-21; hr OQ-12; `docs/ai-agent-gateway.md` (Payroll / HR)
- **Your answer:** 

### Q167. Other HR and payroll settings (confirm the defaults)
- **Who should answer:** Managing Partner; HR / payroll manager for the HR items; accountant for payroll OQ-18 and OQ-25
- **Priority:** Low
- **Context:** Smaller policy points where the team's default is in use.
- **Question:** Confirm each default, or say what to change:
  1. Each company runs and pays its own payroll from its own bank account (no group shared service). (payroll OQ-2)
  2. Payslips are shown and printed in the ERP, English and Arabic on one slip; no e-mail or self-service. (payroll OQ-16; hr OQ-4)
  3. Payroll lines carry no cost centre, department or project in the ledger. (payroll OQ-18)
  4. Rounding per line per employee. (payroll OQ-25)
  5. Leave is recorded and approved by the HR officer, not the line manager; no employee self-service. (hr OQ-4)
  6. No attendance / time tracking; overtime and absences are entered as payroll inputs. (hr OQ-7)
  7. Dates stored Gregorian; iqama Hijri date kept as text as printed. (hr OQ-9)
  8. Document expiry alerts at 90, 60, 30, 7 and 0 days, in the app, to the HR officer. (hr OQ-10)
  9. Secondments: one at a time, full time; billing does not pause during unpaid leave; secondments to third parties allowed; the CFO approves the start. (hr OQ-15)
  10. No Qiwa, Muqeem or Absher connection; references entered by hand; a contract cannot be activated without its Qiwa reference (refuse, not warn); ID number format checked as a warning only. (hr OQ-21; `id.format`)
  11. Ramadan reduced hours are not calculated (the ERP does not schedule hours). (hr OQ-22)
  12. English and Arabic names required for every employee. (hr OQ-24)
- **What the system does now:** Each default as listed.
- **Reference (for the team):** payroll OQ-2, OQ-16, OQ-18, OQ-25; hr OQ-4, OQ-7, OQ-9, OQ-10, OQ-15, OQ-21, OQ-22, OQ-24; payroll OQ-2, OQ-16, OQ-18, OQ-25; hr R9, R61, OQ-4 to OQ-24 (Payroll / HR)
- **Your answer:** 

### Q168. Other approval-engine defaults
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Design choices adopted on the owner's instruction "go with your recommendations".
- **Question:** Confirm: (1) matrix changes are versioned and need a second person; (2) the activities where self-approval is never allowed are fixed in code, not editable data; (3) a matrix version may be drafted by the top authority or the access admin, not by the CFO (who would set their own limits); (4) a new matrix version does not re-route documents already waiting; (5) if a company has two people in the top-authority role (e.g. during a hand-over), neither may self-approve.
- **What the system does now:** All five as stated.
- **Reference (for the team):** access T1 (b) to (d), OQ-31 (replaced), OQ-43, OQ-46, OQ-47; access §10.2 T1, R76 to R81, OQ-43, OQ-46, OQ-47. (Access / web / ledger / migration)
- **Your answer:** 

### Q169. Other sensitive company settings
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** G8 keeps group-level settings at group level.
- **Question:** Confirm: (1) consolidation method, parent company, functional currency and active / inactive are changed by the MP only, without a second person; (2) such group-level rights act on every company of the group (they are governance acts, listed in the governance report), although business rights are granted per company.
- **What the system does now:** As stated.
- **Reference (for the team):** access OQ-18 (remaining part), OQ-38; access R32, R38 to R40, OQ-18, OQ-38. (Access / web / ledger / migration)
- **Your answer:** 

### Q170. Overdue approvals
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Requests can wait if an approver does not act.
- **Question:** Confirm: (1) an overdue request only gets a reminder, it is never moved to another approver; (2) approvers are not e-mailed or texted about new items, they see counts on their dashboard and inbox.
- **What the system does now:** Inbox counts only; escalation reminders not yet built.
- **Reference (for the team):** access OQ-34, web OQ-19; access R18, OQ-34; web OQ-19. (Access / web / ledger / migration)
- **Your answer:** 

### Q171. Overtime approval by department heads
- **Who should answer:** Managing Partner; then HR / payroll manager
- **Priority:** Low (default is safe)
- **Context:** Outside Almiras overtime is approved by the department head.
- **Question:** Confirm the default: a "department head" role granted by the company's top authority; if the head is absent the request waits; a head's own overtime goes to the next approver in the matrix, never self.
- **What the system does now:** As the default.
- **Reference (for the team):** access OQ-28; access OQ-28; hr OQ-30. (Access / web / ledger / migration)
- **Your answer:** 

### Q172. Owner (Managing Partner)
- **Who should answer:** Owner (Managing Partner)
- **Priority:** Low (default is safe)
- **Context:** Billing failures and tax deadlines raise alerts.
- **Question:** 1. Alerts only inside the ERP (default), or also by e-mail or SMS?
  2. Billing failures not fixed after 3 daily runs are also sent to the CFO (default). Confirm.
- **What the system does now:** In-app only; CFO alerted after 3 failed runs.
- **Reference (for the team):** tax-returns OQ-9 and billing OQ-17; billing R34; tax-returns R13. (Billing / tax / banking)
- **Your answer:** 

### Q173. Personal assistant and business analyst rights
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** These roles were requested (A5); their rights were proposed by the team.
- **Question:** Confirm: (1) personal assistant: drafts supplier bills and contract drafts for their principal and adds attachments; (2) business analyst: read-only ledger, invoicing and billing reports, no personal data.
- **What the system does now:** As stated.
- **Reference (for the team):** access OQ-37; access R60, R81, R89, OQ-37. (Access / web / ledger / migration)
- **Your answer:** 

### Q174. Receipt exception and refunds
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** A16 allows the recorder to post their own customer receipt.
- **Question:** Confirm: (1) the person who records a customer receipt (without a bank charge) may still post it, as this is not an approval (A16 stands); (2) this does not extend to refunds to customers or refunds received from suppliers, which keep a second person.
- **What the system does now:** As stated.
- **Reference (for the team):** access OQ-26, OQ-40; access R46, OQ-26, OQ-40. (Access / web / ledger / migration)
- **Your answer:** 

### Q175. Retention decisions and maternity legal clearance: who records them
- **Who should answer:** Managing Partner; then HR / payroll manager (maternity part)
- **Priority:** Low (default is safe)
- **Context:** Destruction of data older than 10 years is decided by an internal audit committee minute (B7). Someone must record that decision in the ERP. HR also needs someone to record "written legal clearance" for maternity pay; there is no legal role.
- **Question:** Confirm: (1) the company's internal auditor records retention decisions (MP in Almiras if there is no internal auditor), although auditors are otherwise read-only; (2) the MP records the maternity legal clearance.
- **What the system does now:** Retention decision right not built yet; legal-clearance default MP.
- **Reference (for the team):** access OQ-29, OQ-50; access R55, R67, OQ-29, OQ-50, §10.10 item 13; hr OQ-44. (Access / web / ledger / migration)
- **Your answer:** 

### Q176. Retirement date alerts and early retirement terminations
- **Who should answer:** Managing Partner; then legal adviser
- **Priority:** Low
- **Context:** Decided: retirement follows each employee's GOSI retirement age, stored as a date.
- **Question:** 1. Alerts 180 and 30 days before the retirement date: confirm or change.
  2. What does retirement mean for non-Saudis (no GOSI pension)? (Default: no date; warning as today.)
  3. A "retirement" termination before the retirement date: warn and require acknowledgement (default), or refuse?
- **What the system does now:** As the defaults.
- **Reference (for the team):** hr OQ-43; hr R37, R66 (Payroll / HR)
- **Your answer:** 

### Q177. Search, exports and later screens
- **Who should answer:** Managing Partner; accountant (exports)
- **Priority:** Low (default is safe)
- **Context:** First-release scope choices.
- **Question:** Confirm: (1) free-text search is sent hidden (so searches cannot be bookmarked, to keep personal data out of web addresses); (2) payroll and purification payout screens come later; (3) report exports are CSV first, Excel later.
- **What the system does now:** As stated.
- **Reference (for the team):** web OQ-17, OQ-18, OQ-25; web R23, OQ-17, OQ-18, OQ-25.

--- (Access / web / ledger / migration)
- **Your answer:** 

### Q178. Selling real estate to an employee by bai' ajel
- **Who should answer:** Project owner; then tax adviser
- **Priority:** Low (default is safe)
- **Context:** A property sale is exempt from VAT but subject to the 5% Real Estate Transaction Tax, with its own steps.
- **Question:** Will the group ever sell real estate to an employee this way?
- **What the system does now:** Refused.
- **Reference (for the team):** invoicing OQ-77; invoicing R125. (Invoicing)
- **Your answer:** 

### Q179. Sharia switch-off: per company
- **Who should answer:** Managing Partner; then Sharia board
- **Priority:** Low (default is safe)
- **Context:** Owner decision A17 applies to "all of Almiras's owned companies and subsidiaries".
- **Question:** Confirm: one request per company (no "switch off several at once"); it covers every company under Almiras, including associates and minority holdings.
- **What the system does now:** One request per company.
- **Reference (for the team):** access OQ-23; access R48, OQ-23. (Access / web / ledger / migration)
- **Your answer:** 

### Q180. Terminations: warnings, withdrawal of an approved termination
- **Who should answer:** Managing Partner; then legal adviser
- **Priority:** Low
- **Context:** Terminations during sick leave (Art. 82) or during maternity / pregnancy (Art. 155) are restricted by law.
- **Question:** 1. Should such terminations be refused, or warned with acknowledgement (default)?
  2. May an approved termination be withdrawn, and by whom? (Default: per the resignation-withdrawal rules already decided.)
- **What the system does now:** Warning with acknowledgement.
- **Reference (for the team):** hr OQ-23 (remaining); hr R41, R59, R60 (Payroll / HR)
- **Your answer:** 

### Q181. Top-authority self-approval: conditions and ceiling
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** The MP (Almiras) and each CEO may approve their own documents; every case appears in the monthly governance report.
- **Question:** Confirm: (1) allowed whenever they are eligible, even if another approver exists; (2) a delegation they gave does not stop them approving; (3) no amount ceiling (a company can limit it through its matrix bands); (4) no immediate alert, the monthly report only.
- **What the system does now:** All four as stated.
- **Reference (for the team):** access OQ-32, OQ-52, OQ-51; access R58, R79 to R83, OQ-32, OQ-51, OQ-52. (Access / web / ledger / migration)
- **Your answer:** 

### Q182. Who changes a company's display settings
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Date and digit defaults per company.
- **Question:** Confirm: the approver (CFO) and the MP, logged.
- **What the system does now:** Not built yet.
- **Reference (for the team):** web OQ-15; web §3.2, OQ-15. (Access / web / ledger / migration)
- **Your answer:** 

### Q183. Who grants access in each company
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Access is granted per company. The access admin is a helper role.
- **Question:** Confirm the default: in each company its top authority grants every role; its access admin grants all roles except CEO, approver, access admin, ZATCA admin, HR officer, payroll officer, external auditor, board representative and internal auditor. (Board representative and internal auditor in subsidiaries: MP only, already confirmed in J1.)
- **What the system does now:** As the default.
- **Reference (for the team):** access OQ-2; access R8, R65, OQ-2, §10.10 item 6. (Access / web / ledger / migration)
- **Your answer:** 

### Q184. ZATCA admin role details
- **Who should answer:** Managing Partner
- **Priority:** Low (default is safe)
- **Context:** ZATCA admin is an add-on role (A19).
- **Question:** Confirm: (1) a technical person may hold the ZATCA admin role alone, without another role in that company; (2) accountants keep "retry failed ZATCA submissions" for ordinary retries, while resubmission stays with ZATCA admins; (3) ZATCA's "sales user" is the ERP's receivables clerk.
- **What the system does now:** As stated.
- **Reference (for the team):** access OQ-25, OQ-27, OQ-6; access R50, OQ-6, OQ-25, OQ-27. (Access / web / ledger / migration)
- **Your answer:** 

## CFO

### Q185. Activities where even the top authority may never self-approve
- **Who should answer:** CFO; then Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Self-approval is allowed only to each company's top authority, and never for VAT returns (G3). The team added five more activities where self-approval is never allowed.
- **Question:** Confirm all five: (1) tax payments, refunds, import VAT and tax settings; (2) employee bank (IBAN) changes; (3) purification funding and payout; (4) the migration account map; (5) bank reconciliation approval.
- **What the system does now:** All five refused for self-approval.
- **Reference (for the team):** access OQ-48; access R80 rows b, f, g, h, i, OQ-48. (Access / web / ledger / migration)
- **Your answer:** 

### Q186. Advances paid to suppliers
- **Who should answer:** CFO (with the accountant)
- **Priority:** Low (default is safe). **Confirm the default.**
- **Context:** The CFO approved account 1330 "Advances to vendors", but paying a supplier before its bill is not built as a feature.
- **Question:** Do group companies pay suppliers in advance often enough to need this in the ERP at go-live?
- **What the system does now:** Not built. A payment can only settle an existing bill.
- **Reference (for the team):** invoicing OQ-40; invoicing §5 (1330). (Invoicing)
- **Your answer:** 

### Q187. Approving a group consolidation while a company's month is still open
- **Who should answer:** CFO
- **Priority:** Low (default is safe)
- **Context:** Monthly management consolidations may be wanted before every company has closed its month.
- **Question:** May a group consolidation be approved while a member company's period is open?
- **What the system does now:** Drafts are allowed. Approval needs every member period closed.
- **Reference (for the team):** consolidation OQ-11; consolidation R14, AT-15. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q188. Bai' ajel invoice layout and the employee's name
- **Who should answer:** CFO (layout); legal adviser / data protection officer (name)
- **Priority:** Low (default is safe)
- **Context:** The bai' ajel invoice shows the cost and the profit as two lines, so the employee sees the margin. The invoice carries the employee code, not the name.
- **Question:** 1. Should the invoice show two lines (current) or a single line at the sale price?
  2. Is the list of people who may see the employee's name right (holders of "view personal data" or "view payslip")?
  3. May the printed invoice show the employee's name?
- **What the system does now:** Two lines; employee code only.
- **Reference (for the team):** invoicing OQ-74; invoicing R118, R51. (Invoicing)
- **Your answer:** 

### Q189. Budgets and budget vs actual
- **Who should answer:** CFO
- **Priority:** Low (default is safe)
- **Context:** A budget vs actual report is planned but not built.
- **Question:** 1. Do you want budgets in the ERP? 2. Loaded how (for example spreadsheet import)? 3. Approved by whom?
- **What the system does now:** Not built. The menu shows "not available".
- **Reference (for the team):** reporting OQ-19; reporting R31, §3.9. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q190. CFO
- **Who should answer:** CFO
- **Priority:** Low (default is safe; none planned)
- **Context:** VAT returns are checked against SAR ledger balances.
- **Question:** Confirm a VAT return is refused for a company whose books are not in SAR (none planned in KSA).
- **What the system does now:** Refused.
- **Reference (for the team):** tax-returns OQ-36; tax-returns §10.1 "Entities". (Billing / tax / banking)
- **Your answer:** 

### Q191. Currencies with no decimals (for example Japanese yen)
- **Who should answer:** CFO
- **Priority:** Low (default is safe). **Confirm the default.**
- **Context:** Invoices are supported in currencies with 2 or 3 decimals.
- **Question:** Will any group company invoice or be billed in a currency without decimals (JPY)?
- **What the system does now:** Refused.
- **Reference (for the team):** invoicing OQ-58; invoicing R93. (Invoicing)
- **Your answer:** 

### Q192. How amounts are measured for approval limits
- **Who should answer:** CFO
- **Priority:** Low (default is safe)
- **Context:** The authority matrix can say "up to SAR 50,000: CFO; above: CEO". The measure must be defined.
- **Question:** Confirm the default: (1) document total including VAT, in SAR at the booking rate; (2) contracts: total value over the term (12 months for open-ended); (3) payroll run: total net pay. Or another measure?
- **What the system does now:** As default; contracts have no total field yet, so they only match rows without an upper limit.
- **Reference (for the team):** access OQ-35; access R72, OQ-35, §10.10 items 11 and 16. (Access / web / ledger / migration)
- **Your answer:** 

### Q193. Intercompany differences: net per pair or per account
- **Who should answer:** CFO
- **Priority:** Low (default is safe)
- **Context:** Before consolidating, balances between two group companies must match. The tolerance is already decided as zero. Still open is whether receivables (1210) and payables (2110) are matched separately or as one net position per pair of companies.
- **Question:** Match intercompany balances (a) as net positions per pair and currency (default), or (b) separately per account (1210 vs 2110)?
- **What the system does now:** Net positions per pair and currency, zero tolerance.
- **Reference (for the team):** consolidation OQ-4 = billing OQ-30 (b) (tolerance itself answered: zero, billing D13(b)); consolidation R19, AT-4; billing R64. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q194. Manual journals, FX journals, revaluations and reversals: who posts
- **Who should answer:** CFO
- **Priority:** Low (default is safe)
- **Context:** One older text let the accountant post journals directly; the owner's rule is "staff prepare, an approver approves, posting follows".
- **Question:** Confirm the default: manual and FX journals are approved (and so posted) by an approver other than the preparer, following the company's matrix; revaluations are prepared by an accountant and posted by an approver.
- **What the system does now:** As the default (activity `ledger.manual_entry` in the approval engine).
- **Reference (for the team):** access OQ-5; access OQ-5; ledger-fx §7, R23, R43. (Access / web / ledger / migration)
- **Your answer:** 

### Q195. May the custodian of an asset approve its write-off?
- **Who should answer:** CFO
- **Priority:** Low (default is safe)
- **Context:** The approval engine can stop people from approving things that benefit them (for example the person responsible for an asset approving its write-off).
- **Question:** Should the asset's custodian be barred from approving its write-off, sale or impairment?
- **What the system does now:** No such bar in slice 1. Sales to employees use invoicing's customer-employee link.
- **Reference (for the team):** fixed-assets OQ-23; fixed-assets R22, R37; access R82. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q196. One credit or debit note correcting several invoices
- **Who should answer:** CFO (with the accountant)
- **Priority:** Low (default is safe)
- **Context:** VAT rules allow one credit or debit note to adjust several invoices of the same customer, for example a year-end volume discount.
- **Question:** Do you need one note to correct several invoices, or is one note per original invoice enough?

  *Also raised in the invoicing list:* Is that needed, or is one note per invoice enough?
- **What the system does now:** One original invoice per note.
- **Reference (for the team):** zatca OQ-27 = invoicing OQ-43; also invoicing OQ-43 = zatca OQ-27; zatca R-19; invoicing R18, R19. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q197. Overtime: rates, time off instead of pay, hour divisor
- **Who should answer:** CFO; then HR / payroll manager
- **Priority:** Low
- **Context:** Overtime is approved in advance, only in special cases, with legal caps (720 hours a year; 10 a day / 60 a week in special cases) and the legal rate (hourly wage plus 50% of basic).
- **Question:** 1. Does any company pay more than the legal rate?
  2. Should paid time off instead of overtime pay be offered (allowed by Regs Art. 22 bis)? (Default: not built.)
  3. Hourly wage = monthly wage / (30 × daily contract hours). Confirm.
- **What the system does now:** Legal rate only, no time off in lieu, divisor as above.
- **Reference (for the team):** payroll OQ-5 (remaining); hr 3.9 `overtime.hour_divisor_rule` [CC-verify]; payroll R45, R52; hr 3.9 (Payroll / HR)
- **Your answer:** 

### Q198. Payroll retro changes, corrections, reversals and off-cycle dates
- **Who should answer:** CFO; then accountant
- **Priority:** Low
- **Context:** Back-dated pay changes and errors must be corrected without editing posted entries.
- **Question:** 1. Retro pay and corrections go into the next regular run (default), not a separate run. Confirm.
  2. How far back may a contract change be dated? (Default: no limit after the contract start.)
  3. Reversal of an approved run: allowed only before it is paid, dated in an open month. Confirm.
  4. An off-cycle run for a closed month is dated the first day of the next open month (with a warning), at that date's exchange rate. Confirm.
- **What the system does now:** As above.
- **Reference (for the team):** payroll OQ-17, OQ-22, OQ-49; hr OQ-20; payroll R13, R70, OQ-17, OQ-22, OQ-49; hr R20 (Payroll / HR)
- **Your answer:** 

### Q199. Physical asset counts and tags
- **Who should answer:** CFO
- **Priority:** Low (default is safe)
- **Context:** Many companies tag assets and count them each year, with the custodian signing off.
- **Question:** Do you want asset tags, periodic physical counts and custodian sign-off in the ERP?
- **What the system does now:** Not built. Only custodian and location are stored.
- **Reference (for the team):** fixed-assets OQ-18; fixed-assets §3.3, §3.7. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q200. Quarterly (interim) statements
- **Who should answer:** CFO
- **Priority:** Low (default is safe)
- **Context:** Final statements can be produced for the year only, or also for quarters (for example for banks).
- **Question:** Do you need final quarterly statements, or only year-end ones?
- **What the system does now:** Any closed range of months may be finalised.
- **Reference (for the team):** reporting OQ-26; reporting R18. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q201. Timesheets and cost pools approval
- **Who should answer:** CFO
- **Priority:** Low (default is safe)
- **Context:** Not built yet.
- **Question:** Confirm: timesheets and cost-pool periods (needed for cost-plus intercompany billing, A8) are approved by the approver (CFO) unless the matrix says otherwise.
- **What the system does now:** Listed as pending activities.
- **Reference (for the team):** access OQ-55; access OQ-55, §10.10 item 9; billing spec. (Access / web / ledger / migration)
- **Your answer:** 

### Q202. Who closes accounting periods
- **Who should answer:** CFO
- **Priority:** Low (default is safe)
- **Context:** Closing a month stops any further posting into it.
- **Question:** Confirm the default: the approver (CFO) closes periods. Or the accountant, or the top authority?
- **What the system does now:** Approver role holds "close periods".
- **Reference (for the team):** access OQ-12; access §3.6, OQ-12. (Access / web / ledger / migration)
- **Your answer:** 

## Accountant

### Q203. Abandoned construction work in progress
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** A construction project (work in progress) may be abandoned after its bills were booked. A write-off normally needs an active asset.
- **Question:** How should an abandoned work-in-progress project be written off, and who approves it?
- **What the system does now:** Not built. The draft stays in the register and in the Zakat inputs until decided.
- **Reference (for the team):** fixed-assets OQ-29; fixed-assets R8, R23, R34. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q204. Access to a company that has been made inactive
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** A dormant company can be set to inactive; nothing can post there.
- **Question:** Confirm the default: people with access may still view it (and the MP may reactivate it).
- **What the system does now:** As the default.
- **Reference (for the team):** access OQ-8; access R38, OQ-8. (Access / web / ledger / migration)
- **Your answer:** 

### Q205. Accountant
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Small working choices in the VAT module.
- **Question:** Confirm or change:
  1. A VAT refund is requested with the return (a tick box), and the refund is recorded when received (OQ-18).
  2. Import VAT records count in the check of VAT accounts against the return (OQ-37).
  3. Is import VAT ever paid by a customs broker who then bills the company? The ERP records import VAT paid directly by bank only (OQ-35).
- **What the system does now:** As stated; customs-broker payment not supported.
- **Reference (for the team):** tax-returns OQ-18, OQ-37, OQ-35; tax-returns §3.2 `ImportVatRecord`, R15, §10.1. (Billing / tax / banking)
- **Your answer:** 

### Q206. Acquiring an asset other than through a bill
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Assets might be paid straight from the bank, built by own staff (payroll cost), or contributed by a shareholder.
- **Question:** Which of these routes do you need: (a) direct bank payment, (b) self-built with staff cost, (c) capital contribution in kind?
- **What the system does now:** None. Only bills, mirror bills, murabaha acquisitions, work-in-progress transfers and the opening entry may post to asset accounts.
- **Reference (for the team):** fixed-assets OQ-22; fixed-assets R9. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q207. Are short Sharia-compliant deposits cash equivalents?
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Deposits of 3 months or less can count as "cash equivalents" in the cash flow statement.
- **Question:** Are Sharia-compliant deposits (1300) of 3 months or less cash equivalents?
- **What the system does now:** No.
- **Reference (for the team):** reporting OQ-11; reporting R15, §5. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q208. Associates making losses
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Under the equity method, the group's share of an associate's losses normally stops once the investment reaches zero, unless the group has long-term loans to it or obligations.
- **Question:** 1. Confirm losses stop at zero. 2. Do any long-term loans or guarantees to associates exist? 3. How is impairment of an investment assessed?
- **What the system does now:** Stops at zero. Impairment entered manually.
- **Reference (for the team):** consolidation OQ-18 ([CC-verify] on R25); consolidation R25. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q209. Associates that do not keep books in the ERP
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** For an associate whose books are elsewhere, the group needs its profit and dividends to apply the equity method.
- **Question:** 1. What is the source: audited or management accounts? 2. May they be for an earlier date (a lag), and by how much? 3. Who approves them?
- **What the system does now:** Approved results for the same period end, approved by the Managing Partner.
- **Reference (for the team):** consolidation OQ-9; consolidation R25, §3.6, AT-9. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q210. Building number check on customer addresses
- **Who should answer:** Accountant
- **Priority:** Low (default is safe). **Confirm the default.**
- **Context:** ZATCA requires a 4-digit building number for our own address only. The system also checks it on Saudi customers' addresses.
- **Question:** Keep this stricter check on customer addresses, or relax it?
- **What the system does now:** It keeps the check.
- **Reference (for the team):** invoicing OQ-45; invoicing §3.1, R69. (Invoicing)
- **Your answer:** 

### Q211. Cash flow classification of dividends, deposit profit and financing cost
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** The standards (IAS 7 / section 7, with IFRS 18 changes) allow some choice in where these appear in the cash flow statement.
- **Question:** Where should we show (a) dividends received, (b) profit on Islamic deposits received, (c) financing cost paid: operating, investing or financing?
- **What the system does now:** Received: investing. Paid: financing.
- **Reference (for the team):** reporting OQ-12; reporting R15. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q212. Changing a company's functional currency later
- **Who should answer:** Accountant; then external auditor
- **Priority:** Low (default is safe)
- **Context:** IAS 21 requires a change of functional currency to be applied from the date of change onwards.
- **Question:** Confirm the default: changing it after postings is blocked; if ever needed, the team designs a separate procedure then.
- **What the system does now:** Blocked.
- **Reference (for the team):** ledger-fx OQ-13; ledger-fx R5, OQ-13. (Access / web / ledger / migration)
- **Your answer:** 

### Q213. Chinese yuan: CNY only
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Offshore yuan (CNH) has a slightly different rate.
- **Question:** Confirm the default: only the official ISO code CNY; no offshore CNH or other non-ISO codes.
- **What the system does now:** CNY only.
- **Reference (for the team):** ledger-fx OQ-12; ledger-fx §3.1, OQ-12. (Access / web / ledger / migration)
- **Your answer:** 

### Q214. Companies on different standards; aligning accounting policies in the group
- **Who should answer:** Accountant; then the external auditor
- **Priority:** Low (default is safe)
- **Context:** One company may use full IFRS while the group uses IFRS for SMEs, or companies may use different policies (for example investment property at cost in one, at fair value in another).
- **Question:** 1. Which standard do the group accounts follow? 2. How are differing policies aligned: manual group adjustments, or the same policy imposed on all companies?
- **What the system does now:** The group follows the parent's standard. Alignment by manual consolidation journals.
- **Reference (for the team):** consolidation OQ-15 (related fixed-assets OQ-6); consolidation R23; fixed-assets R20. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q215. Companies with a different financial year end
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Group consolidation is simplest when all companies close on the same date. Each company's year end is a setting (default 31 December).
- **Question:** Do all group companies have a 31 December year end? If any does not, how should it be consolidated?
- **What the system does now:** All members must have the same year end; otherwise the run is refused.
- **Reference (for the team):** consolidation OQ-16 (business part of reporting OQ-5); consolidation R10 to R14; reporting §3.1 `fiscal_year_end`. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q216. Correcting errors of earlier years
- **Who should answer:** Accountant; then the external auditor
- **Priority:** Low (default is safe)
- **Context:** A material error in a closed year should normally be corrected by restating that year (IAS 8 / section 10). Zakat assessment differences for past years raise the same question.
- **Question:** (a) correct in the current period with a disclosure (default) or (b) build restatement of prior years? How should prior-year Zakat assessment differences be shown?
- **What the system does now:** Current-period correction, disclosed.
- **Reference (for the team):** reporting OQ-16; reporting R26; tax-returns T8. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q217. Current vs non-current split for loans, financing and deposits
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** The balance sheet splits items due within 12 months from later ones. Accounts 1220, 2400 (Islamic financing) and 1300 can hold both, and the split needs maturity data.
- **Question:** Should these be split by maturity (needs repayment schedules), or shown by account as today?
- **What the system does now:** By account, as in the reporting §5 table.
- **Reference (for the team):** reporting OQ-9; reporting §5. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q218. Discarding a draft revaluation
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** A prepared revaluation that is not yet posted can be thrown away and prepared again for the same month.
- **Question:** Confirm the default: a discarded draft is kept for audit, does not block a new run for that month, and any holder of the revaluation right (accountant) may discard it. Or should only (a) the preparer or (b) the approver discard?
- **What the system does now:** As the default.
- **Reference (for the team):** ledger-fx OQ-18; ledger-fx R24, R45, AT-32, AT-35, OQ-18. (Access / web / ledger / migration)
- **Your answer:** 

### Q219. Discount on the whole invoice
- **Who should answer:** Accountant
- **Priority:** Low (default is safe). **Confirm the default.**
- **Context:** A discount on the whole invoice must be split across VAT categories and income accounts.
- **Question:** Is giving the discount on each line enough, or is a whole-invoice discount needed? If it is needed, split it (a) in proportion to the line amounts, or (b) by hand per category?
- **What the system does now:** A whole-invoice discount is refused; discounts go on the lines.
- **Reference (for the team):** invoicing OQ-55; invoicing R9 note. (Invoicing)
- **Your answer:** 

### Q220. Dividends
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Declaring and paying dividends, including dividends between group companies, could have their own documents.
- **Question:** 1. Which account holds dividends payable? 2. Do you want dividend declaration and payment documents? Intercompany dividends would go through 2110 with the counterparty.
- **What the system does now:** Manual journals, shown in "other" in the statement of changes in equity.
- **Reference (for the team):** reporting OQ-24; reporting R14. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q221. Evidence for control and significant influence over group companies
- **Who should answer:** Accountant; then the external auditor
- **Priority:** Low (default is safe)
- **Context:** Whether a company is a subsidiary (controlled) or an associate (significant influence) decides how it is consolidated. It depends on voting rights, board seats and agreements, not only on the % owned.
- **Question:** What criteria and evidence should be recorded for each holding (voting rights, board seats, shareholder agreements)?
- **What the system does now:** Free text plus an evidence reference, approved by the Managing Partner.
- **Reference (for the team):** consolidation OQ-1 (plus the [CC-verify] on IFRS 10 / section 9 and IAS 28 / section 14); consolidation R1, R3, R4, §3.1. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q222. Exchange rates for consolidation: group rates or company rates
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Translation uses exchange rates. The group has one global rate table, and a company may have its own override (for example the Central Bank of Egypt's rates for the Egyptian company).
- **Question:** For consolidation, use (a) the global group rates only (default) or (b) each company's own rates where it has them?
- **What the system does now:** Global group rates.
- **Reference (for the team):** consolidation OQ-3 (= ledger-fx OQ-2, OQ-4); consolidation R16. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q223. Foreign-currency bank accounts in the chart
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** A USD bank account can be a separate sub-account (e.g. 1101 USD) or share 1100 with other currencies.
- **Question:** Confirm the default: one 1100 may hold any currency; sub-accounts per currency are allowed. Or require one sub-account per currency?
- **What the system does now:** As the default. (Note: the migration loader expects a currency sub-account for foreign bank accounts, see "Foreign-currency balances at cut-over".)
- **Reference (for the team):** ledger-fx OQ-11; ledger-fx §3.4, OQ-11; invoicing OQ-20. (Access / web / ledger / migration)
- **Your answer:** 

### Q224. Group cash flow statement method
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** A group cash flow statement can be built from the consolidated figures or by combining each company's cash flow and removing intra-group flows.
- **Question:** Which method do you prefer?
- **What the system does now:** Deferred to the reporting module.
- **Reference (for the team):** consolidation OQ-17; consolidation §6; reporting R15. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q225. Impairment testing: rental property units and who gives recoverable amounts — confirm the default
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** At each reporting date the company must check for signs that an asset is worth less than its book value (IAS 36.9 / section 27.7). Rental properties may be tested building by building or in groups.
- **Question:** 1. Confirm the default: indicator check at each year end plus any period ends you designate as reporting dates, with the recoverable amount entered manually with evidence. 2. Are rental properties tested one by one or in groups? 3. Who supplies recoverable amounts (valuer, accountant)? 4. Which period ends, besides the year end, are reporting dates?
- **What the system does now:** Reminders at the year end and at listed reporting dates. Manual recoverable amount with evidence.
- **Reference (for the team):** fixed-assets OQ-7 (compliance A4); fixed-assets R19, §3.9 `reporting_dates`. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q226. Intercompany balances with partly-owned companies in group reports
- **Who should answer:** Accountant; then CFO (for the consolidation spec)
- **Priority:** Low (default is safe). **Confirm the default.**
- **Context:** Balances with companies the group only partly owns, or holds a minority stake in, are not fully cancelled out in group reports.
- **Question:** Confirm that invoicing books them like any intercompany balance (1210 / 2110 with the other company named), with the group-report treatment left to the consolidation spec.
- **What the system does now:** It books them as stated.
- **Reference (for the team):** invoicing OQ-38; invoicing R4, R68; consolidation spec. (Invoicing)
- **Your answer:** 

### Q227. Investments in subsidiaries and associates in the parent's own statements
- **Who should answer:** Accountant; then the external auditor
- **Priority:** Low (default is safe)
- **Context:** In Almiras's own (separate) statements, its shares in subsidiaries and associates can be carried at cost, at fair value, or by the equity method (IFRS for SMEs 9.26, 14.4).
- **Question:** For Almiras's separate statements: (a) cost (default), (b) fair value, or (c) equity method?
- **What the system does now:** As booked (cost). The equity method is used only in the group consolidation.
- **Reference (for the team):** reporting OQ-15 = consolidation OQ-8; reporting R2; consolidation R25, §5. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q228. Meaning of the Falcon "مسلسل القيد" column
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** The journal export has a column "مسلسل القيد" next to the entry number.
- **Question:** Is it the line number inside the entry or a running serial across all entries?
- **What the system does now:** Kept for information only.
- **Reference (for the team):** migration OQ-8; migration M1, OQ-8. (Access / web / ledger / migration)
- **Your answer:** 

### Q229. Must month-end close wait for depreciation and for unregistered asset purchases?
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Month-end close can check that depreciation was posted and that every bill booked to an asset account is in the register.
- **Question:** Should closing a month be refused when (a) active assets have no posted depreciation run, or (b) asset-account bill lines are not in the register?
- **What the system does now:** Yes to both.
- **Reference (for the team):** fixed-assets OQ-4; fixed-assets R15, R43, AT-35. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q230. Notes to the statements: written in the system or outside
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** The notes contain both numbers and text (policies, explanations).
- **Question:** Should note texts be written in the ERP as bilingual templates, or outside it (for example in Word) using the ERP's figures?
- **What the system does now:** Outside. The system provides the figures.
- **Reference (for the team):** reporting OQ-27; reporting §3.6. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q231. One exchange-rate table for the group
- **Who should answer:** Accountant; then Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Rates are kept in one group table; a company may add its own rates (e.g. Egypt using Central Bank of Egypt rates).
- **Question:** Confirm the default: one global table, maintained by group treasury, with optional company overrides by that company's accountant.
- **What the system does now:** As the default.
- **Reference (for the team):** ledger-fx OQ-4; ledger-fx R28, §7, OQ-4. (Access / web / ledger / migration)
- **Your answer:** 

### Q232. Opening balance account 3900 must reach zero
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** The migration uses 3900 (Opening balance equity) as a temporary account. It should be nil once migration is complete.
- **Question:** From which date should a non-zero 3900 block statements?
- **What the system does now:** Checked from the first final run.
- **Reference (for the team):** reporting OQ-18; reporting R16. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q233. Other comprehensive income items
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Other comprehensive income holds gains and losses kept outside profit (for example a revaluation surplus or certain actuarial gains on end-of-service benefits).
- **Question:** Do any group companies have such items, and on which accounts?
- **What the system does now:** None.
- **Reference (for the team):** reporting OQ-14; reporting R13, §5. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q234. Profit and loss by nature or by function
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** The profit and loss can list expenses by nature (salaries, depreciation, rent) or by function (cost of sales, admin, selling). The chart of accounts is by nature.
- **Question:** For each company: by nature (default) or by function?
- **What the system does now:** By nature.
- **Reference (for the team):** reporting OQ-3; reporting R13, §3.1. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q235. Rounding
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Converting foreign amounts to SAR leaves tiny rounding differences.
- **Question:** Confirm the default: round half up; tolerance 0.01 SAR per foreign line; rounding differences to 5770 (currency rounding).
- **What the system does now:** As the default.
- **Reference (for the team):** ledger-fx OQ-8; ledger-fx R14 to R16, OQ-8. (Access / web / ledger / migration)
- **Your answer:** 

### Q236. Separate entry and verification of withholding tax data
- **Who should answer:** Accountant; then tax adviser
- **Priority:** Low (default is safe)
- **Context:** Withholding-tax details on supplier bills are entered and then verified.
- **Question:** Confirm the default: the same person may enter and verify. Or must they be two people?
- **What the system does now:** No separation rule.
- **Reference (for the team):** access OQ-17; access OQ-17. (Access / web / ledger / migration)
- **Your answer:** 

### Q237. Showing VAT payable and receivable net
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** Input VAT (1310) and output VAT (2200) are owed to and from the same authority, ZATCA.
- **Question:** Show them (a) net, as one line (default), or (b) separately?
- **What the system does now:** Net.
- **Reference (for the team):** reporting OQ-10; reporting §5. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q238. Splitting assets into components; major overhauls; moving assets between categories
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** A building's lifts or a vehicle's fit-out may wear out faster than the main asset and can be depreciated separately.
- **Question:** 1. Which assets should be split into components? 2. How are major overhauls treated? 3. Do you need to move assets between categories?
- **What the system does now:** One asset per bill line. Land and building are kept separate. Later costs are added through an event. Moving between categories comes in slice 2.
- **Reference (for the team):** fixed-assets OQ-16; fixed-assets R2, R4, R8. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q239. Sub-groups with intermediate parents
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** If a subsidiary itself owns subsidiaries, the group can consolidate in one step at the top, or first build sub-group accounts.
- **Question:** 1. Do any intermediate parents exist or are any planned? 2. Do they need their own consolidated statements?
- **What the system does now:** One step at the top parent, using effective ownership %.
- **Reference (for the team):** consolidation OQ-21; consolidation R6.

--- (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q240. Unrealised profit on other intra-group sales, and transactions with associates
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** When one group company sells to another at a profit and the item is still held in the group, that profit must be removed from the group accounts. This is automated for fixed assets only.
- **Question:** 1. Do intra-group sales of stock or capitalised services occur? 2. Should transactions with associates be eliminated at all? For sales to associates, only the group's share is normally removed.
- **What the system does now:** Fixed assets automated. Other items entered manually. Associates not eliminated, except the group's share on asset sales.
- **Reference (for the team):** consolidation OQ-7 (= invoicing OQ-38; [CC-verify] on R21 downstream sales); consolidation R18, R21. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q241. Using the fixed-asset register for the PPE note and cash flow
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** The property, plant and equipment note shows additions, disposals and depreciation. The fixed-asset register (slice 1 now built) can supply these per asset.
- **Question:** Should the PPE note and the cash flow take their figures from the register, instead of ledger movements only?
- **What the system does now:** Ledger movements only.
- **Reference (for the team):** reporting OQ-13; reporting §3.6, R15. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q242. What happens when no rate exists for a date; manual rates
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** For VAT in SAR, the latest published SAMA rate is used (owner answer I2). For ordinary bookings another rule applies.
- **Question:** Confirm the default: for ordinary bookings use the latest earlier rate up to 5 calendar days old, otherwise refuse. Should a manually typed line rate be limited (e.g. within 2 % of the table rate), or only flagged (default)?
- **What the system does now:** 5-day fallback; manual rates flagged, not limited.
- **Reference (for the team):** ledger-fx OQ-3; ledger-fx R12, OQ-3. (Access / web / ledger / migration)
- **Your answer:** 

### Q243. Year-end closing as a real entry or only in reports
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** The year-end close can post a real journal entry moving profit to retained earnings, or the reports can just compute it.
- **Question:** (a) real closing entries (default) or (b) computed in reports only?
- **What the system does now:** Real postings, approved by someone other than the preparer.
- **Reference (for the team):** reporting OQ-8; reporting R21 to R24, Y1. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q244. Year-end closing of accounts held in a fixed foreign currency
- **Who should answer:** Accountant
- **Priority:** Low (default is safe)
- **Context:** At year end, income and expense balances are closed to retained earnings. A few accounts may be fixed to a foreign currency, which the standard closing entry cannot handle.
- **Question:** How should income or expense accounts fixed to a foreign currency be closed at year end?
- **What the system does now:** The close is refused while such an account has a balance.
- **Reference (for the team):** reporting OQ-7; reporting R21, R22. (ZATCA / fixed assets / reporting)
- **Your answer:** 

## External auditor

### Q245. Advances received from customers are not revalued
- **Who should answer:** External auditor
- **Priority:** Low (default is safe)
- **Context:** Money received in advance (deferred income, 2310) is treated as a non-monetary item under IAS 21, so it stays at the rate of the day it was received and is not revalued at month end.
- **Question:** Confirm that advances received (2310) are non-monetary and are not revalued.
- **What the system does now:** 2310 is not on the revaluation list.
- **Reference (for the team):** ledger-fx §5.0 [CC-verify] on account 2310; ledger-fx §5.0, R21; billing R70. (Access / web / ledger / migration)
- **Your answer:** 

### Q246. Revaluation model for property, plant and equipment
- **Who should answer:** External auditor (with the accountant)
- **Priority:** Low (default is safe)
- **Context:** Both IFRS for SMEs (17.15–17.15B) and full IFRS (IAS 16.29) allow some asset classes (for example land and buildings) to be carried at revalued amounts instead of cost.
- **Question:** 1. Do you want the revaluation model for any class? 2. Does SOCPA or Saudi law restrict it for our companies?
- **What the system does now:** Not built. Revaluations are refused in every company.
- **Reference (for the team):** fixed-assets OQ-8 (compliance A7); fixed-assets R21, §5 (3500 reserved). (ZATCA / fixed assets / reporting)
- **Your answer:** 

## Tax adviser

### Q247. Customer address in Arabic
- **Who should answer:** Tax adviser
- **Priority:** Low (default is safe)
- **Context:** Every visible field on an invoice must be in Arabic, but proper names may stay in Latin letters. The system accepts a customer's street, district and city in English only, treating them as proper names.
- **Question:** Is that acceptable, or must the Arabic address fields be required?
- **What the system does now:** It accepts English-only customer addresses.
- **Reference (for the team):** compliance report row 6e (no OQ number); invoicing §3.1, R15; compliance report row 6e. (Invoicing)
- **Your answer:** 

### Q248. Invoices dated before a company's VAT registration
- **Who should answer:** Tax adviser
- **Priority:** Low (default is safe)
- **Context:** A company's VAT applies from its registration date (Almiras: 2024-04-01).
- **Question:** May a company issue a standard-rated (15%) invoice dated before its VAT registration date?
- **What the system does now:** Refused.
- **Reference (for the team):** invoicing OQ-32; invoicing R11. (Invoicing)
- **Your answer:** 

### Q249. Line VAT rate when the supply date and the invoice date fall on either side of a rate change
- **Who should answer:** Tax adviser
- **Priority:** Low (only matters if the VAT rate changes again)
- **Context:** If the VAT rate changed between the supply date and the invoice date (as with 5% to 15% in July 2020), one rate must be chosen.
- **Question:** Which date sets the line's VAT rate: (a) the supply date (current); (b) the tax point (earliest of supply, invoice, payment)?
- **What the system does now:** Option (a).
- **Reference (for the team):** invoicing OQ-51 (b); invoicing R10, R100. (Invoicing)
- **Your answer:** 

### Q250. Mirror bill and when the buying company may claim VAT
- **Who should answer:** Tax adviser
- **Priority:** Low (default is safe)
- **Context:** When one group company invoices another, a draft bill appears automatically in the buyer's books. The seller has already declared the VAT.
- **Question:** May the buyer claim back that VAT before it approves the draft bill, or only after (current)?
- **What the system does now:** It is claimed only when the buyer approves and posts the bill, in the VAT period of the posting date.
- **Reference (for the team):** invoicing OQ-71; invoicing R113, E7. (Invoicing)
- **Your answer:** 

### Q251. Tax adviser
- **Who should answer:** Tax adviser; then IT / security lead
- **Priority:** Low (default is safe)
- **Context:** VAT figures are typed into the ZATCA portal by hand from the ERP's export.
- **Question:** Does ZATCA offer an API for VAT, WHT or Zakat returns that in-house software may use? Default: manual filing.
- **What the system does now:** Manual filing.
- **Reference (for the team):** tax-returns OQ-29 **[CC-verify]**; tax-returns §6. (Billing / tax / banking)
- **Your answer:** 

### Q252. VAT code on invoices of a company without a VAT number
- **Who should answer:** Tax adviser
- **Priority:** Low (nothing goes to ZATCA for these invoices)
- **Context:** A company without a VAT number issues a plain "فاتورة / Invoice" with no VAT, and it is not sent to ZATCA.
- **Question:** Which VAT category code should its lines record internally: (a) "O" (out of scope, current); (b) none?
- **What the system does now:** Option (a), with no exemption reason.
- **Reference (for the team):** invoicing OQ-59 (remainder); invoicing R101. (Invoicing)
- **Your answer:** 

### Q253. VAT on the bai' ajel profit
- **Who should answer:** Tax adviser
- **Priority:** Low (default follows the owner's and adviser's earlier answers B8 and E8)
- **Context:** VAT is charged at 15% on the whole sale price, cost plus profit (for example cost 100 + profit 10 = 110, VAT 16.50, paid by the employee). The compliance review asked whether part of the profit could instead be an exempt financing margin.
- **Question:** Confirm that when a company sells its own goods to an employee on deferred terms, the whole price is a taxable sale of goods at 15%, with no exempt financing part.
- **What the system does now:** It charges 15% on the whole price.
- **Reference (for the team):** invoicing OQ-78; invoicing R112, R118. (Invoicing)
- **Your answer:** 

### Q254. Zakat filing for wholly owned subsidiaries (one consolidated return?)
- **Who should answer:** Tax adviser
- **Priority:** Low (default is safe)
- **Context:** In Saudi Arabia a parent may, under conditions, file one Zakat return covering its wholly owned Saudi subsidiaries. Today each company files its own.
- **Question:** Should Almiras file a consolidated Zakat return for its wholly owned subsidiaries, and if so, under which conditions?
- **What the system does now:** Zakat is per company. Consolidation produces no Zakat figures.
- **Reference (for the team):** consolidation §6 ([CC-verify], tax_returns row); tax-returns spec; consolidation §6; tax-returns spec. (ZATCA / fixed assets / reporting)
- **Your answer:** 

## Sharia board

### Q255. Egypt late-payment surcharges (Sharia)
- **Who should answer:** Sharia board
- **Priority:** Low (no posting built yet)
- **Context:** Egypt's insurance agency (NOSI) and tax authority (ETA) charge surcharges for late payment.
- **Question:** Confirm that such surcharges are booked as a penalty expense, never as interest, with nothing to purify.
- **What the system does now:** Default as stated; nothing posted yet.
- **Reference (for the team):** payroll OQ-61; payroll OQ-61

--- (Payroll / HR)
- **Your answer:** 

### Q256. Interest-bearing arrangements inside the group
- **Who should answer:** Sharia board; then owner (Managing Partner)
- **Priority:** Low (default is safe)
- **Context:** A group company that does not enforce Sharia could lend to another group company with interest. In the group accounts that interest would need eliminating.
- **Question:** Is any interest-bearing arrangement between group companies acceptable? If it exists, how is it reported in the group accounts?
- **What the system does now:** The group consolidation stops with a Sharia exception. Manual lines on interest accounts are refused.
- **Reference (for the team):** consolidation OQ-20; consolidation R23, R28, AT-16. (ZATCA / fixed assets / reporting)
- **Your answer:** 

### Q257. Late-payment charges
- **Who should answer:** Sharia board
- **Priority:** Low (no such feature exists)
- **Context:** Sharia allows no penalty income.
- **Question:** Confirm: no late-payment charges are built. If a contract ever has one, it must be given to charity (booked to 2600) and never treated as income.
- **What the system does now:** It has no late-charge feature.
- **Reference (for the team):** invoicing OQ-15; invoicing D19 / OQ-15.

--- (Invoicing)
- **Your answer:** 

### Q258. Selling a registered asset to an employee on deferred payment (bai' ajel)
- **Who should answer:** Sharia board; then owner (Managing Partner)
- **Priority:** Low (default is safe)
- **Context:** Bai' ajel is a Sharia-compliant sale on deferred payment. Today the company may buy an asset specially to resell to an employee (account 1250). The open question is whether an asset the company already uses (for example a used company car) may be sold to an employee this way.
- **Question:** May an existing company asset be sold to an employee on bai' ajel? If yes, at what price basis (net book value, market value)?
- **What the system does now:** Refused. Only assets bought for the purpose on 1250 can be sold this way.
- **Reference (for the team):** fixed-assets OQ-12; fixed-assets R6; invoicing R120. (ZATCA / fixed assets / reporting)
- **Your answer:** 

## Legal adviser

### Q259. Destroying old personal data: start date, e-invoice files, repeat reviews
- **Who should answer:** Legal adviser; then Managing Partner
- **Priority:** Low (default is safe; nothing is destroyed without an internal audit committee minute)
- **Context:** Personal data older than 10 years is nominated for destruction, and an internal audit committee minute decides.
- **Question:** 1. From when do the 10 years run: (a) the latest related document (current); (b) the end of the business relationship; (c) for employees, the end of employment?
  2. After ZATCA's retention period, may the stored e-invoice files (which hold buyer data) be destroyed too, or only the ERP fields?
  3. When the committee decides to keep a record, is it nominated again (for example every year)?
- **What the system does now:** It counts from the latest related document; e-invoice files are never destroyed; a kept record is nominated again after 1 year.
- **Reference (for the team):** invoicing OQ-67 (b) to (d); invoicing R108, R52. (Invoicing)
- **Your answer:** 

### Q260. EOSB and service counting details
- **Who should answer:** Legal adviser (labour law)
- **Priority:** Low
- **Context:** Unpaid leave of more than 20 days stops service from counting (for EOSB, leave and fixed terms). Part years are paid pro rata by days (decided).
- **Question:** 1. Is the 20-day limit counted across a whole work year (intermittent days added up, default) or per single leave?
  2. In a leap year, is the part-year divided by 366 or always by 365? (Default: 365.)
- **What the system does now:** Per work year; 365.
- **Reference (for the team):** hr OQ-37, hr OQ-36 (remainder); payroll OQ-37 (pointer to these); hr R30, R39, R62, 3.9 `leave.unpaid.suspension_after_days`, `eosb.year_divisor` (Payroll / HR)
- **Your answer:** 

### Q261. GCC nationals and Saudization tracking
- **Who should answer:** Legal adviser (labour law); then HR / payroll manager (GOSI); Managing Partner for Nitaqat
- **Priority:** Low (needed before hiring a national of another GCC state)
- **Context:** Non-Saudis may only have fixed-term contracts; nationals of other GCC states may be treated like Saudis under GCC agreements, both for the Labour Law and for GOSI. The team found no text on this.
- **Question:** 1. Is a GCC national treated as a Saudi or as a non-Saudi for the fixed-term contract rule?
  2. What GOSI applies to a GCC national working here (the GCC insurance extension)? Please supply the instrument if available.
  3. Should the ERP track Saudization (Nitaqat) percentages? (Default: out of scope.)
- **What the system does now:** Treated as non-Saudi for contracts; payroll refuses any employee marked GCC until the GOSI rule is known; Nitaqat not tracked.
- **Reference (for the team):** hr OQ-39, hr OQ-19 (remaining); payroll OQ-9 item 6; hr 3.9 `gosi.gcc`; hr R16, 3.9; payroll R21(h) (Payroll / HR)
- **Your answer:** 

### Q262. GOSI cohort for rehires and returning pensioners
- **Who should answer:** Legal adviser (social insurance); or HR / payroll manager confirming with GOSI
- **Priority:** Low
- **Context:** Whether an employee is under the old or new GOSI rules depends on earlier contribution periods. Rehires, people who got a lump-sum payout, and pensioners returning to work are unclear.
- **Question:** For a rehire, a person who received a partial or lump-sum GOSI payout, or a pensioner who returns to work: which rules (old or new) apply, and is a returning pensioner covered for pension again?
- **What the system does now:** The HR officer enters the cohort from the employee's GOSI record; it is never guessed from the hire date; missing cohort refuses the payslip.
- **Reference (for the team):** payroll OQ-41 and hr OQ-41 (same question); payroll R55, R56; hr 3.2 (Payroll / HR)
- **Your answer:** 

### Q263. Legal adviser
- **Who should answer:** Legal adviser (PDPL); HR manager
- **Priority:** Low (default is safe)
- **Context:** Secondment invoices to another company concern named employees.
- **Question:** Confirm the invoice shows only the role title and a code for the employee (no name, ID number or pay details).
- **What the system does now:** Role title and pseudonymous employee code only.
- **Reference (for the team):** billing OQ-12; billing §3.6, R62, R71.

--- (Billing / tax / banking)
- **Your answer:** 

### Q264. Printing a B2C invoice shows the buyer's personal data
- **Who should answer:** Legal adviser (PDPL)
- **Priority:** Low (default is safe)
- **Context:** A simplified (B2C) invoice printout shows the buyer's name and details.
- **Question:** Must a user hold the "view personal data" right to print it, or is the logged print enough?
- **What the system does now:** Printing is not built yet; the default leaves it to the invoice print design, with every print logged.
- **Reference (for the team):** web OQ-21; web OQ-21; access R51. (Access / web / ledger / migration)
- **Your answer:** 

### Q265. Using ZATCA's SDK (LGPL-3 licence) in automated tests
- **Who should answer:** Legal adviser
- **Priority:** Low (default is safe)
- **Context:** ZATCA's own SDK is used offline in the test pipeline to cross-check our invoices. It is never part of the running ERP. Its licence is LGPL-3, an open-source licence with conditions. The owner's licence confirmation (J5) covered MIT, BSD, Apache-2.0 and OFL, not LGPL.
- **Question:** May the ZATCA SDK (LGPL-3) be used offline in our test pipeline only, never shipped with the ERP?
- **What the system does now:** Used in tests offline, with a legal sign-off note pending.
- **Reference (for the team):** zatca OQ-11 (remainder; §6); zatca §6 (reference validation), OQ-11. (ZATCA / fixed assets / reporting)
- **Your answer:** 

## IT / security lead

### Q266. MFA lock after wrong codes, and resetting a lost phone
- **Who should answer:** IT / security lead; then Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Protects against guessing codes and against misuse of resets.
- **Question:** Confirm: (1) 5 wrong codes lock for 15 minutes; each further lock doubles (30 min, 1 h, up to 24 h); at the second lock the person, their access admins and the group MP get an alert; (2) one phone (device) per person; (3) a lost device is reset only by the group MP (for anyone) or by an access admin (only for people who need no MFA in their own companies), never by oneself, with a reason, logged; superusers only by an operator command.
- **What the system does now:** As stated; no reset screen yet (command and service only).
- **Reference (for the team):** web OQ-30, web OQ-11; web R16, R18, OQ-11, OQ-30; security review items 2, 3; verification items 2. (Access / web / ledger / migration)
- **Your answer:** 

### Q267. Session timeouts
- **Who should answer:** IT / security lead; then Managing Partner
- **Priority:** Low (default is safe)
- **Context:** A session ends after inactivity so an unattended screen cannot be misused.
- **Question:** Confirm: signed out after 15 minutes without activity for people who must use MFA, 30 minutes for others, and after 10 hours in any case (also when the browser closes). Or other values?
- **What the system does now:** As stated.
- **Reference (for the team):** web OQ-8; web R17, OQ-8. (Access / web / ledger / migration)
- **Your answer:** 

### Q268. User interface technology
- **Who should answer:** IT / security lead; then Managing Partner
- **Priority:** Low (default is safe)
- **Context:** Fewer moving parts; works on mobile browsers.
- **Question:** Confirm: server-made web pages (Django) with HTMX, no separate app framework and no Node build step.
- **What the system does now:** Built this way.
- **Reference (for the team):** web OQ-1; web W-D2, OQ-1. (Access / web / ledger / migration)
- **Your answer:** 

# Appendix: for the team

These were found answered in the owner answers or the decision pack, but the spec still lists them as open. Nothing is needed from the owner; the team will tidy the specs.

**Invoicing**

- invoicing OQ-81: answered by the decision pack of 2026-10-17, Part C 1c item A, approved by the CFO (= D32(a), policy A). The approved wording names "2310 and 2320" among the control accounts that "can only be posted by the invoicing module, never by manual journal", so 2310 carries the control flag when it is added. The team default matches this; the row can be marked resolved.
- invoicing OQ-62 ([SDK-confirm] part): answered by evidence in zatca OQ-35 (SDK R3.4.8, `docs/compliance/zatca-sdk-3-findings-2026-10-17.md` §3.5). ZATCA XML amounts are 2 decimals for every currency. The row still says "Still open: the [SDK-confirm] itself". The remaining owner part is listed above.
- invoicing R65 / §9 "Blocking" paragraph (386 prepayment [SDK-confirm], zatca OQ-33): answered by evidence (SDK R3.4.8 accepts type 386; zatca OQ-33 resolved 2026-10-17). The R65 note and the closing paragraph still list it.
- invoicing OQ-2 (ZATCA hand-off contract, marked BLOCKING): built. `config/settings.py` sets `INVOICING_ZATCA_PORT = "zatca.port.ZatcaModulePort"`, but the row and the "Blocking (build)" paragraph were not updated. It is internal, so it is counted under skipped.
- invoicing §9 closing paragraphs ("Tax questions for the CFO (D30c)", "Blocking (build) / (go-live)"): out of date. They still list OQ-44, OQ-50 to OQ-54, OQ-57, OQ-59 and D29, which are all resolved, so they should be trimmed to the entries above.

**Payroll / HR**

- payroll OQ-12(a): answered by owner answers B3 (2026-10-18) (tax penalties get their own account, separate from 5930 GOSI late-payment fines); part (b) remains pending above.
- payroll OQ-14: answered by owner answers J3 and A3 (an approver may not approve a run in which their own payslip carries inputs or retro lines, nor an off-cycle run that pays them; only the top authority may self-approve, logged).
- payroll OQ-60: answered by owner answers A11 and B7 and decision pack E7 (keep everything; data older than 10 years is nominated for destruction by an internal audit committee approval minute), which matches the default for justification worksheets and report lines.
- hr OQ-25: answered by owner answers A3 (self-approval only for the entity's top authority, MP in Almiras, CEO elsewhere; everyone else's own records go to another approver).

**Access / web / ledger / migration**

- access OQ-42 (2026-10-21 additions): answered by owner answers J1 (board representative and internal auditor in a subsidiary are granted and revoked by the group MP, never the subsidiary CEO) and J2 (a change to a subsidiary's self-approval switch or top-authority role is decided at group level: MP, group chairman or group board representative). The row and §10.10 items 14 and 15 still say "owner to confirm". The main part of OQ-42 stays pending above.
- access R82 / §10.10 item 15 (no OQ row): answered by owner answers J3 (an approver may not approve a payroll run in which their own payslip has inputs or retro lines, nor any off-cycle run that pays them; a regular run with only their plain contract salary may be approved).
- web OQ-28: answered by owner answers J4 (the "brand shape" file is a Rabeez logo; the ERP does not use it and uses the Almiras symbol until Almiras supplies the correct shape).
- web OQ-23 (tidy only): the row still shows "session-level" as default, but the slice-2 notes record the owner's instruction for a fresh code at every approval (step-up), as built. Counted as resolved.

**Billing / tax / banking**

- billing OQ-8: answered by owner answers A1 to A3 and billing D14(g) (approvers come from each company's authority matrix; only the top authority may self-approve, logged). R8 is amended, but the OQ row still shows the old default.
- billing OQ-16: answered by owner answers G7 and H2 (SAR rate = SAMA rate on the tax point; the whole document is booked at that rate). The row and the R32 / D14(f) notes still say "invoice date".
- tax-returns OQ-1: answered by owner answer F4 ("Build the VAT filing module"). §10 records it, but the row still says blocking.
- tax-returns OQ-10: answered by owner answer G6 (return period = tax point; R17 amended). The row still says "still open for the period".
- tax-returns OQ-33: answered by owner answer A10 ("all approvable documents go through the same approval engine"). Built: `tax/services.py` submits returns and payments through `core.access.engine`. The row and the §10.3 blocking summary still list it as open.
- *(Partial, remainder listed above)* tax-returns OQ-7: VAT part answered by G3. tax-returns OQ-24: validity date confirmed by F4. tax-returns OQ-31: "own account" answered by B3. banking OQ-27 / invoicing OQ-66: the migration opening-entry part answered by B2. banking OQ-15: retention answered by B11(d).

**ZATCA / fixed assets / reporting**

- reporting OQ-20: answered by owner answers J5 (2026-10-21) (only free / open-source dependencies; MIT, BSD, Apache-2.0 and OFL licences confirmed, so `openpyxl` (MIT), `WeasyPrint` (BSD) and an OFL Arabic font are approved).
- consolidation OQ-4 (tolerance part only): answered by billing D13(b), 2026-10-17 (intercompany tolerance is zero). The spec still says "pending with the owner". The per-account split stays open (listed above).
- consolidation OQ-10 (principle only): answered by owner answers A2, A10 (each company's own authority matrix; all approvals through one engine). The named roles stay open (listed above with fixed-assets OQ-21).
- consolidation D3 and reporting RD6 (decision rows, not OQs): "no automatic bill in the buyer entity / no mirror bills" is superseded by owner answers A8 (2026-10-18: automatic mirror draft bill in the buying company; invoicing D33(c), R113). fixed-assets D4 was already amended; consolidation and reporting were not.
- zatca R-38 vs fixed-assets R33: the 2026-10-22 correction (shorter of the adjustment period and the useful life) is written in zatca R-38, but `zatca.rules.retention_until` still takes only an adjustment period in years, and `ZATCA_MOVABLE_ADJUSTMENT_YEARS` is unset (`config/settings.py`). Team tidy-up after the tax adviser answers (see the Art. 52 entry).

