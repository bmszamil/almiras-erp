# Spec: Intercompany billing (`billing` app)

Status: draft for review · Written 16 Oct 2026 · Depends on: `core`, `ledger`, ZATCA module (separate spec)

## 1. Purpose
Lets any group entity (first user: Almiras, code `ALM`) bill other group entities for shared services, asset rentals, IP licences, secondments and consultations. Each recharge is a formal ZATCA-compliant VAT invoice issued from a service agreement, priced by timesheet, fixed fee or cost-plus. Invoices post through `ledger.services`. When the counterparty keeps its books in this ERP, a matching draft bill is created in its books so intercompany balances mirror for elimination; when it does not, only the issuer's side is booked. Works for any issuing or receiving entity, including equity-method associates.

## 2. Decisions
All by Badr Alzamil, Managing Partner, 16 Oct 2026.

| # | Decision |
| --- | --- |
| D1 | Almiras provides shared services to subsidiaries: finance and accounting, HR and payroll, legal and governance. It also rents out assets (staff accommodation, warehouse space, machines, vehicles, office space), licenses IP, seconds experts and charges for consultations. |
| D2 | "Every recharge is a formal ZATCA-compliant VAT invoice to the subsidiary", not an internal allocation. |
| D3 | Pricing depends on the case: timesheets (hours x rate), fixed monthly fee, or actual cost plus a markup %. All three must be supported per agreement. |
| D4 | The group has mixed ownership including minority stakes (equity-method associates); a counterparty may or may not be fully consolidated. |
| D5 | Sharia rules are enforced: no interest. Late-payment penalties must not be booked as income to Almiras; if used, they go to charity. Needs Sharia sign-off. |
| D6 | Reporting standard is IFRS for SMEs now, full IFRS later. Zakat applies. |
| D7 | The Managing Partner approves everything for now; the design must allow an approval matrix later. |
| D8 | Subsidiaries join the same ERP later. When both entities are in the ERP, the receiving entity's bill is created automatically as a draft mirroring the invoice. When the counterparty is not on the ERP yet, only Almiras's side is booked. |
| D9 | The rental assets module (leases, vehicles with fuel/mileage logs) is a later wave. Billing agreements must be able to take lines from rental leases later. |
| D10 | Records created by future AI agents carry a `source` field (human / import / agent) and are drafts only. |

## 3. Data model
All models: `entity` FK (the issuing entity unless stated), `source` (choice: `human`, `import`, `agent`; plus `source_agent` char, required when `agent`), `created_by`, `created_at`. Money is `Decimal(18,2)` with explicit `currency` (default entity `base_currency`). "Bilingual" = `_en` and `_ar` fields, both required.

### 3.1 Change to `core.Entity` (proposed)
| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `books_in_erp` | bool | yes, default False | True when the entity keeps its own ledger here. Drives D8 mirroring. Almiras = True. |
| `books_in_erp_from` | date | when True | First date the mirror is created. |

### 3.2 `ServiceType` (reference data, group-wide)
| Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- |
| `code` | char 20, unique | yes | | e.g. `FIN`, `HR`, `LEGAL`, `RENT_ACCOM`, `RENT_WH`, `RENT_MACH`, `RENT_VEH`, `RENT_OFFICE`, `IP`, `SECOND`, `CONSULT` |
| `name` | char 200 | yes | yes | |
| `category` | choice | yes | | `service`, `rental`, `ip`, `secondment`, `consultation` |
| `income_account_code` | char 20 | yes | | Default for issuer, e.g. 4210 / 4110 / 4300 |
| `expense_account_code` | char 20 | yes | | Default for receiver (see 5.6) |
| `is_active` | bool | yes | | |

### 3.3 `ServiceAgreement` (per issuing entity)
| Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- |
| `entity` | FK Entity | yes | | Issuer |
| `counterparty` | FK Entity | yes | | Receiver; must differ from `entity` |
| `number` | char 40 | yes | | `<ENTITY>-AGR-<YYYY>-<nnnn>`, unique per entity |
| `service_type` | FK ServiceType | yes | | |
| `description` | text | yes | yes | Printed on invoice |
| `pricing_method` | choice | yes | | `timesheet`, `fixed_fee`, `cost_plus` |
| `hourly_rate` | Decimal | if timesheet | | Per agreement; optional per-role rates via `AgreementRate` |
| `fixed_fee` | Decimal | if fixed_fee | | Per billing period |
| `markup_pct` | Decimal(6,3) | if cost_plus | | ≥ 0 |
| `cost_pool` | FK CostPool | if cost_plus | | |
| `allocation_pct` | Decimal(6,3) | if cost_plus | | Share of pool billed to this counterparty (see Q5) |
| `vat_treatment` | choice | yes | | `standard`, `zero_rated`, `exempt`, `out_of_scope` |
| `vat_rate` | Decimal(5,2) | if standard | | Taken from tax settings at billing date, not typed in |
| `billing_frequency` | choice | yes | | `monthly`, `quarterly`, `annual`, `one_off` |
| `payment_terms_days` | int | yes | | |
| `start_date` / `end_date` | date | start yes | | end optional (open-ended) |
| `income_account_code` / `expense_account_code` | char 20 | no | | Override service-type defaults |
| `tp_method` | choice | no | | `cup`, `cost_plus`, `tnmm`, `other`, blank (see Q9) |
| `tp_documentation` | FK file | no | | Benchmark / TP file |
| `status` | choice | yes | | `draft`, `pending_approval`, `active`, `suspended`, `ended` |
| `source_type` / `source_ref` | char | no | | Hook for D9: e.g. `rental.lease` + lease id; feeding module supplies lines (3.6) |
| `source` | choice | yes | | D10 |

`AgreementRate` (optional): `agreement` FK, `role_en`/`role_ar`, `hourly_rate`, `valid_from`, `valid_to`.

### 3.4 `TimesheetEntry` (in the issuing entity)
| Field | Type | Req | Bilingual | Notes |
| --- | --- | --- | --- | --- |
| `entity` | FK Entity | yes | | Issuer |
| `agreement` | FK ServiceAgreement | yes | | Must be `timesheet` method |
| `user` | FK User | yes | | Person who worked |
| `rate` | FK AgreementRate | no | | Else agreement `hourly_rate` |
| `date` | date | yes | | Within agreement dates |
| `hours` | Decimal(6,2) | yes | | > 0, ≤ 24 per user per day across entries |
| `description` | char 300 | yes | yes | |
| `status` | choice | yes | | `draft`, `submitted`, `approved`, `billed`, `rejected` |
| `invoice_line` | FK InvoiceLine | no | | Set when billed |

### 3.5 `CostPool` and `CostPoolPeriod` (issuer)
`CostPool`: `entity`, `code`, `name_en`/`name_ar`, `accounts` (M2M `ledger.Account`, same entity, expense type only), `is_active`.
`CostPoolPeriod`: `cost_pool`, `period` (FK FiscalPeriod), `amount` (computed from posted ledger lines on `accounts` in the period), `computed_at`, `locked` (bool; true once billed).

### 3.6 `BillableItem` (generic feed, D9)
For lines supplied by other modules (later: rental leases, fuel/mileage recharges). `entity`, `agreement`, `period_start`, `period_end`, `description_en`/`description_ar`, `quantity`, `unit_price`, `source_type`, `source_ref`, `status` (`open`, `billed`), `invoice_line`. The rental module itself is out of scope.

### 3.7 `IntercompanyInvoice` and `InvoiceLine` (issuer)
| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `entity` | FK Entity | yes | Issuer |
| `counterparty` | FK Entity | yes | |
| `kind` | choice | yes | `invoice`, `credit_note` |
| `credit_of` | FK self | if credit_note | Original invoice |
| `credit_reason_en` / `_ar` | char 300 | if credit_note | ZATCA requires a reason |
| `number` | char 40 | on issue | Gap-free per entity and year: `<ENTITY>-ICI-<YYYY>-<nnnnnn>` / `-ICC-` for credit notes |
| `issue_date`, `supply_date_from`, `supply_date_to`, `due_date` | date | yes | |
| `billing_period` | char 7 | yes | `YYYY-MM` (or quarter/year key) |
| `currency` | char 3 | yes | |
| `net_total`, `vat_total`, `gross_total` | Decimal | yes | Sum of lines |
| `status` | choice | yes | `draft`, `pending_approval`, `approved`, `issued` (posted), `cancelled_draft` |
| `journal_entry` | FK JournalEntry | on issue | |
| `zatca_status` | choice | yes | Set by ZATCA module only |
| `mirror_bill` | FK IntercompanyBill | no | Set when D8 applies |
| `billing_run` | FK BillingRun | no | |
| `source` | choice | yes | D10 |

`InvoiceLine`: `invoice`, `agreement`, `description_en`/`_ar`, `quantity`, `unit_price`, `net_amount`, `vat_treatment`, `vat_rate`, `vat_amount`, `income_account` (FK Account, issuer), `pricing_basis` (char, e.g. "12.5 h x 450" or "cost 80,000 + 10%").

### 3.8 `IntercompanyBill` (receiver, D8)
`entity` (receiver), `counterparty` (issuer), `invoice` (FK IntercompanyInvoice, unique), `number` (receiver's sequence `<ENTITY>-ICB-<YYYY>-<nnnnnn>`), `bill_date`, `due_date`, totals copied, `status` (`draft`, `approved`, `posted`, `disputed`), `journal_entry`, `source` = `import` with `source_agent` blank (system-created). Lines copy invoice lines, plus `expense_account` (receiver's Account) and optional cost centre. Lives in this app until a general AP module exists (Q11).

### 3.9 `BillingRun`
`entity`, `period` (FK FiscalPeriod), `run_date`, `status` (`running`, `completed`, `failed`), `invoices_created` (int), `log` (JSON), `started_by`, `source`.

### 3.10 `Approval` (generic, D7)
`content_type` + `object_id` (agreement, invoice, credit note, bill), `entity`, `level` (int), `approver` (User), `decision` (`approved`, `rejected`), `comment`, `decided_at`. Required approvers come from `billing.services.required_approvals(obj)`; today it returns one level: the Managing Partner. A later approval matrix replaces this function only.

## 4. Rules
| # | Rule | Enforced by |
| --- | --- | --- |
| R1 | Issuer and counterparty must be different active entities. | `ServiceAgreement.clean()`; service check |
| R2 | An agreement has exactly the pricing fields its method needs (rate / fee / markup + pool + allocation) and no others. | `clean()` + test per method |
| R3 | Only `active` agreements whose dates cover the supply period are billed. | `billing.services.run_billing` |
| R4 | One invoice per agreement per billing period. Re-running a billing run creates nothing new for already-billed agreements. | DB unique constraint (agreement, billing_period) on a draft/issued invoice line; idempotency test |
| R5 | Timesheet method bills only `approved`, unbilled entries dated in the period; they become `billed` and locked. | Service; `TimesheetEntry.save()` blocks edits when `billed` |
| R6 | Cost-plus amount = `CostPoolPeriod.amount x allocation_pct x (1 + markup_pct)`, rounded to 2 decimals half-up; the pool period locks when billed. | Service; unit test with fixture numbers |
| R7 | Cost pools include only posted entries of the issuer, only expense accounts, never Sharia-prohibited accounts. | `CostPool` validation + service |
| R8 | VAT per line = `net x rate`, 2 decimals, half-up; invoice totals = sum of lines. Rate comes from tax settings at supply date. | Service; test (rounding method to be confirmed by compliance-checker) |
| R9 | The billing run creates invoices as `draft` only. Nothing is issued without approval. | Service |
| R10 | An invoice, credit note, agreement or mirror bill moves past `pending_approval` only when every approval from `required_approvals()` is recorded. Today: Managing Partner. | `billing.services.approve`; test |
| R11 | Issuing an invoice assigns its number, builds the journal entry with `ledger.services.create_entry` and posts it with `ledger.services.post_entry`, in one transaction. Never sets `status="posted"` directly. | Service; test that a ledger failure rolls back the number |
| R12 | Every journal line created by this module carries `counterparty_entity` (all lines, not only 12xx/21xx), so elimination can match income to expense. | Service; test |
| R13 | An issued invoice is never edited or deleted. Corrections are credit notes (full or partial) linked by `credit_of`; a credit note cannot exceed the original's uncredited balance. | `save()`/`delete()` guards; service check |
| R14 | Drafts may be cancelled (`cancelled_draft`); they release timesheets, billable items and pool locks. Issued numbers are never reused. | Service |
| R15 | On issue, if `counterparty.books_in_erp` and issue date ≥ `books_in_erp_from`: create a draft `IntercompanyBill` in the counterparty with identical net, VAT and gross per line. Otherwise create no bill and post nothing in the counterparty. | Service; two tests |
| R16 | The receiver may change expense account and cost centre on the mirror bill, never amounts, dates or VAT. A disagreement is set `disputed` and resolved by a credit note from the issuer. | `IntercompanyBill.clean()`; service |
| R17 | A credit note on an invoice that has a mirror bill creates a mirror draft debit-side document (bill credit) the same way. | Service |
| R18 | No interest, finance charge or time-value uplift can be configured on agreements. Late-payment penalty is disabled until Q1 is answered; if enabled, it posts only to 2600 Charity payable, never to income. | No field for interest; penalty setting off by default; Sharia account checks in `ledger.services.validate_entry` |
| R19 | Users see and act only on agreements, invoices and bills of entities they have permission for. The issuer cannot see the receiver's bill expense coding unless it also has rights on the receiver. | Queryset filter by entity + permission; test |
| R20 | Agent-created records (`source=agent`) can only be `draft` (agreements, timesheets, invoices). Agents cannot approve or issue. | Gateway permissions + service check on `source` |
| R21 | All printed text (agreement, invoice, credit note) is bilingual Arabic/English. | Required `_en`/`_ar` fields; ZATCA module renders both |
| R22 | Invoices hand over to the ZATCA module after posting; `zatca_status` is written only by that module. | Field read-only in this app |

## 5. Accounting
Codes from `ledger/coa_template.py`. "cp" = `counterparty_entity` on the line. Example: Almiras bills Leafan SAR 10,000 + 15% VAT for finance services.

### 5.1 Invoice issued (issuer, e.g. ALM)
| Account | Dr | Cr | cp |
| --- | --- | --- | --- |
| 1210 Due from group entities | 11,500 | | LFN |
| 4210 Intercompany service income (4110 for rentals, 4300 for IP) | | 10,000 | LFN |
| 2200 VAT output | | 1,500 | LFN |

Zero-rated / exempt / out of scope: no 2200 line; 1210 = net.

### 5.2 Mirror bill posted (receiver, only when R15 applies)
| Account | Dr | Cr | cp |
| --- | --- | --- | --- |
| Expense per 5.6 (e.g. 5300 Professional fees) | 10,000 | | ALM |
| 1310 VAT input | 1,500 | | ALM |
| 2110 Due to group entities | | 11,500 | ALM |

Input VAT recoverability for the receiver is not assumed (Q7).

### 5.3 Credit note (issuer) — mirror of 5.1
| Account | Dr | Cr | cp |
| --- | --- | --- | --- |
| 4210 / 4110 / 4300 (same as original line) | net | | LFN |
| 2200 VAT output | VAT | | LFN |
| 1210 Due from group entities | | gross | LFN |

Receiver bill credit: Dr 2110, Cr expense, Cr 1310 (all cp = issuer).

### 5.4 Settlement (if recorded in this module, see Q12)
| Entity | Dr | Cr |
| --- | --- | --- |
| Issuer | 1100 Cash and bank | 1210 (cp receiver) |
| Receiver | 2110 (cp issuer) | 1100 Cash and bank |

### 5.5 Late-payment penalty (disabled; only if Sharia board approves, Q1)
| Account | Dr | Cr | cp |
| --- | --- | --- | --- |
| 1210 Due from group entities | amount | | receiver |
| 2600 Charity payable (purification) | | amount | receiver |

Never to any 4xxx account.

### 5.6 Default receiver expense accounts (proposed, accountant to confirm, Q8)
| Category | Issuer income | Receiver expense |
| --- | --- | --- |
| Finance, HR/payroll, legal/governance services | 4210 | 5300 Professional fees |
| Consultation | 4210 | 5300 Professional fees |
| Secondment | 4210 | 5100 Salaries and benefits |
| Rentals (accommodation, warehouse, office, machines, vehicles) | 4110 | 5200 Rent |
| IP licence | 4300 | 5900 General and administrative |

### 5.7 Cost-plus
Underlying costs stay in the issuer's expense accounts; the recharge is booked gross as income (5.1). Whether IFRS presentation should net recharged cost against expense is Q6.

### 5.8 Consolidation note
`ledger.services.intercompany_balances` must show ALM→X debit equal to X→ALM credit. Full consolidation: eliminate 1210/2110 and income/expense pairs. Equity-method counterparty: no line elimination; balances shown as related-party; unrealised profit treatment is for the consolidation module (Q4).

## 6. Integrations
| System | Direction | What |
| --- | --- | --- |
| `ledger` | out | Journal entries via `create_entry` / `post_entry`; reads posted lines for cost pools; `intercompany_balances` for reconciliation |
| ZATCA module (separate spec) | out / in | After issue: invoice/credit note handed to `zatca.services` for generation, signing, QR and clearance/reporting. In: `zatca_status`, UUID, hash. This spec defines no XML. |
| Rental module (later wave) | in | Creates `BillableItem` rows and agreements with `source_type="rental.lease"` |
| HR/payroll (later), GOSI, Mudad | in (later) | Payroll cost per seconded employee for cost pools; no direct GOSI/Mudad calls from this module |
| AP module (later) | out | Mirror bills move there when it exists (Q11) |
| Agent gateway | in | Agents may draft agreements, timesheets, invoices (`source=agent`); never approve/issue |
| SAP B1 / other subsidiary systems | none now | Counterparty not on ERP receives the ZATCA invoice PDF/XML only (Q13) |
| Banks | none | Settlement via bank module (Q12) |

### Intercompany reconciliation report
Per pair of entities and as-of date: issuer 1210 balance, receiver 2110 balance (or "not on ERP"), difference, list of unmatched items (invoice without posted bill, disputed bills, bills dated in another period), counterparty consolidation type and ownership %. Bilingual headings; export to XLSX.

## 7. Permissions
Per entity; a role on ALM gives nothing on LFN.

| Action | Billing clerk (issuer) | Accountant (issuer) | Managing Partner | Receiver accountant | Agent service user | Auditor |
| --- | --- | --- | --- | --- | --- | --- |
| Create/edit draft agreement | yes | yes | yes | no | draft only | no |
| Record timesheet | own entries | yes | yes | no | draft only | no |
| Approve timesheet | no | yes | yes | no | no | no |
| Run billing (drafts) | yes | yes | yes | no | no | no |
| Approve agreement / invoice / credit note | no | no | yes (today, all) | no | no | no |
| Issue (post) invoice / credit note | no | yes, after approval | yes | no | no | no |
| Edit mirror bill coding / dispute | no | no | yes | yes | no | no |
| Approve and post mirror bill | no | no | yes | yes, after approval | no | no |
| View reconciliation report | no | yes | yes | own entity pairs | no | read-only |

## 8. Acceptance tests
| # | Given | When | Then |
| --- | --- | --- | --- |
| T1 | Active fixed-fee agreement ALM→LFN, SAR 10,000/month, VAT standard 15% | Billing run for 2026-11 | One draft invoice: net 10,000, VAT 1,500, gross 11,500; no journal entry |
| T2 | T1 completed | Billing run for 2026-11 again | No new invoice; run log says "already billed" |
| T3 | Timesheet agreement at 450/h; approved entries 10h and 2.5h in Nov, one submitted entry 3h | Billing run | Line 12.5h x 450 = 5,625; approved entries `billed`; submitted entry untouched |
| T4 | Billed timesheet entry | User edits hours | Rejected |
| T5 | Cost pool Nov = 80,000; allocation 25%; markup 10% | Billing run | Net 22,000.00; pool period locked |
| T6 | Cost pool includes account 5610 (prohibited) | Save pool | Rejected |
| T7 | Draft invoice T1 | Accountant issues without MP approval | Rejected |
| T8 | T1 approved by MP | Accountant issues | Number `ALM-ICI-2026-000001`; posted JE per 5.1; every line has cp = LFN |
| T9 | LFN `books_in_erp=True` | T8 issued | Draft bill in LFN, 11,500 gross, per 5.2; nothing posted in LFN yet |
| T10 | LFN `books_in_erp=False` | T8 issued | No bill, no LFN entries; recon report shows "counterparty not on ERP" |
| T11 | Mirror bill draft | LFN accountant changes amount | Rejected; changing expense account allowed |
| T12 | Issued invoice | Any user edits or deletes it | Rejected; error says to raise a credit note |
| T13 | Issued invoice 11,500 | Credit note 5,750 approved and issued | JE per 5.3; mirror bill credit draft in LFN; uncredited balance 5,750 |
| T14 | T13 | Second credit note 6,000 | Rejected (exceeds balance) |
| T15 | Nov period closed for ALM | Issue invoice dated in Nov | Rejected by ledger; invoice number not consumed |
| T16 | Agreement with counterparty = issuer | Save | Rejected |
| T17 | Agent creates draft invoice (`source=agent`) | Agent tries to approve or issue | Rejected; action logged |
| T18 | User with rights on ALM only | Lists bills | Sees none of LFN's bills |
| T19 | Penalty setting off | Overdue invoice | No penalty created; no 4xxx posting possible for penalty |
| T20 | Associate counterparty (equity method, 30%) on ERP | Invoice issued and bill posted | Recon report balances match and shows "equity method 30%" |
| T21 | Invoice and bill posted ALM/LFN | `intercompany_balances` as-of month end | ALM→LFN + LFN→ALM = 0 |
| T22 | Invoice missing Arabic description | Approve | Rejected |
| T23 | `required_approvals()` patched to return two levels | One approval recorded | Still `pending_approval` (matrix-ready) |

## 9. Open questions
| # | Question | Owner |
| --- | --- | --- |
| Q1 | Are late-payment penalties used at all? If yes: amount basis, and confirmation they go to 2600 Charity payable and then to which charity, and whether a fixed penalty (not time-based) is acceptable. | Sharia adviser + MP |
| Q2 | Are any group entities in, or planned for, a ZATCA VAT group? Intra-group supplies inside a VAT group are outside VAT, which conflicts with "VAT invoice on every recharge". | Tax adviser |
| Q3 | Saudi transfer pricing bylaws: do they apply to these transactions given Zakat (not income tax) status of entities and any foreign shareholders? Required methods, documentation, benchmark and disclosure form. | Tax adviser |
| Q4 | Equity-method associates: how to treat unrealised profit on recharges in consolidation, and do associates get the same pricing as subsidiaries? | Accountant / auditor |
| Q5 | Cost-plus allocation: fixed % per agreement, or a driver (headcount, revenue, hours)? Must allocations across a pool sum to 100%, and is Almiras's own share left unbilled? | Accountant + MP |
| Q6 | Which costs are excluded from cost pools (Zakat, depreciation, MP/shareholder costs, stewardship costs)? Present recharge gross as income or net against cost? | Accountant / auditor |
| Q7 | Can each receiver recover input VAT in full (e.g. exempt activities)? | Tax adviser |
| Q8 | Confirm the receiver expense mapping in 5.6; should dedicated intercompany expense accounts be added to the chart for easier elimination? | Accountant |
| Q9 | Markup % and hourly rates per service type: who sets them and is a benchmark needed per agreement? | MP + tax adviser |
| Q10 | Foreign counterparties (e.g. non-Saudi portfolio companies): VAT zero-rating for exported services, invoice currency and FX rate source. | Tax adviser + accountant |
| Q11 | Should mirror bills move to a general AP module when built, and who in each subsidiary approves its own bill once it has its own staff? | MP |
| Q12 | Settlement: cash payment, periodic netting of balances, or offset against loans (1220)? Which module records it? | MP + accountant |
| Q13 | For counterparties not on the ERP, do they need a data export (CSV/Excel or SAP B1 format) in addition to the ZATCA invoice? | MP |
| Q14 | Billing frequency defaults per service type, and invoice issue date (last day of month vs first working day after). | Accountant |
| Q15 | IFRS 16 under full IFRS will change how receivers book rentals (right-of-use asset, not rent expense). When does the group move to full IFRS, and should rental agreements already record lease terms for that? | Accountant / auditor |
| Q16 | Approval matrix later: amount thresholds, per entity, per service type? Not needed now; confirm `required_approvals()` hook is enough. | MP |
| Q17 | Secondment: is the seconded employee's cost billed at cost, cost-plus, or a day rate, and are there Mudad/Ajeer labour-law requirements for seconding staff between group companies? | HR / legal |
