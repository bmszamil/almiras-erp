# Tax returns: VAT return, withholding tax returns, Zakat and income tax

Django app: `tax`. Status: **draft spec**, written 2026-10-17. Not built.
Depends on: `ledger.services` (`create_entry(post=True, grant, document)`, `reverse_entry`, `close_period`, `trial_balance`, `ledger/documents.py` registry), `docs/specs/invoicing.md` (VAT categories, `vat_breakdown`, `total_vat_sar`, reverse charge R30, attribution R29, WHT R61 to R64), `docs/specs/payroll.md` (payroll WHT rows, OQ-30; GOSI fine 5930), `docs/specs/access.md` (`require`, segregation, grants), `docs/specs/reporting.md` (fiscal-year statements; Zakat and tax notes data), `docs/specs/banking.md` (payments matched to statements).

**Tags.** **[CC-verify]** = regulatory figure or rule the compliance-checker must confirm against an official source before build; the value shown is the default. A citation in parentheses points to an official ZATCA document downloaded on 2026-10-16 (abbreviations as in the Sources table of `docs/compliance/zatca-verification-2026-10-16.md`: TIR, PSG, WHT, DTG, XML). The ZATCA VAT return guide, the Zakat Implementing Regulations and the Income Tax Regulations were **not** downloaded (list in OQ-2). **[AR-review]** = Arabic label to be checked by an Arabic reviewer.

## 1. Purpose

The `tax` module prepares, reviews, approves, locks and records the filing and payment of the tax returns of **every entity** in the group (Almiras, subsidiaries, partly owned and minority entities added as `Entity` rows): (a) the VAT return per entity and return period (quarterly or monthly per `Entity.vat_return_period`), built box by box from posted invoicing documents, customs import VAT records and approved corrections; (b) the monthly WHT statement and the annual WHT statement with its beneficiaries list, built from `WhtDeduction` rows (invoicing R64, payroll OQ-30); (c) the annual Zakat computation per entity (or per wholly owned group when consolidated filing is approved), the Zakat provision and payment, assessments and the Zakat certificate; (d) income tax on the non-Saudi share of partly owned entities. It posts tax settlement, payment and provision entries only through `ledger.services` as registered documents. Zakat is kept separate from Sharia purification (4900 / 2600). Every user-facing text is in English and Arabic.

## 2. Decisions

Only recorded decisions. Items from the task brief are listed separately; they are scope, not business decisions.

| # | Decision | Source | Date |
|---|---|---|---|
| X1 | Every business record has an `entity` FK; queries filtered by entity and permission; new subsidiaries are data; every text en/ar; money `Decimal`, SAR 2 dp; posted entries never edited; posting only through `post_entry` / `create_entry`; closed periods reject postings; Sharia per entity, non-compliant income flagged for purification. | CLAUDE.md | in repo |
| X2 | Almiras: VAT number per setup data (core/setup_data/almiras.json), effective 2024-04-01, **quarterly VAT returns** (setup data only, never in fixtures). | Project owner (invoicing D10) | 2026-10-16 |
| X3 | No entity is in a VAT group; each entity is a separate VAT taxpayer with its own VAT number; intercompany invoices carry VAT. | Project owner (invoicing D11) | 2026-10-16 |
| X4 | WHT is in scope: Almiras pays non-resident suppliers; WHT per category, WHT payable account, treaty certificates, gross-up option, **monthly WHT return data**. | Project owner (invoicing D14) | 2026-10-16 |
| X5 | Income types incl. residential rent (VAT-exempt) feed Art. 51 apportionment; ratio method needs tax-adviser sign-off. | Project owner (invoicing D16) | 2026-10-16 |
| X6 | Accounting staff create; the CFO approves; posting is automatic on approval; roles per entity. | Project owner (invoicing D17; access A1) | 2026-10-16 |
| X7 | "No VAT-return report for now; customs import VAT as a manual JE outside [invoicing]" (OQ-29 default approved). **This spec proposes that report; superseding X7 needs the owner's confirmation (OQ-1).** | Project owner (invoicing D19) | 2026-10-16 |
| X8 | A resident vendor without a VAT number charges no VAT (0 VAT bills). | Project owner (invoicing D24) | 2026-10-17 |
| X9 | The tax date of a credit or debit note is the note's **own** date. Entities without a VAT number charge no VAT. | Project owner (invoicing D30a, D30b) | 2026-10-17 |
| X10 | Proposed account codes stay proposed until the accountant/CFO review (Word document). | Project owner (invoicing D29, ledger-fx D10) | 2026-10-17 |
| X11 | Payroll writes remuneration WHT as `WhtDeduction` rows (shape of invoicing). | Payroll spec P8 / OQ-30 default | 2026-10-17 |

**Task brief (caller, 2026-10-17; not a business decision):** return lifecycle prepared → reviewed → CFO-approved → locked; VAT payment Dr 2200 / Cr bank; corrections above a threshold by voluntary disclosure; deadline alerts; WHT monthly and annual; Zakat per entity with consolidated option; Zakat certificate tracking, "Almiras certificate valid until 2027-04-30" (to be entered as data and confirmed, OQ-24); income tax on foreign shares; Zakat kept apart from purification.

## 3. Data model

All models carry `entity` (FK `core.Entity`, PROTECT) unless marked *global*. "Bi" = `_en` and `_ar` fields, both required for user-facing labels. Money = `Decimal(18,2)` SAR. Status changes only in `tax.services` (model `save` guard + PostgreSQL trigger as in invoicing `0007`).

### 3.1 Settings and reference data

**TaxSettings** (one per entity; changes through `tax.services.change_tax_settings`, approver, logged in `AccessLog`)
| Field | Type | Req | Notes |
|---|---|---|---|
| entity | O2O Entity | Y | |
| fiscal_year_end | Char(5) `MM-DD` | Y | Default `12-31`. Shared with reporting.md (OQ-3 proposes moving it to `Entity`) |
| zakat_year_basis | Choice `gregorian` / `hijri` | Y | Default `gregorian`; selects the Zakat rate (R40) |
| vat_quarter_start_month | SmallInt 1–12 | Y | Default 1 (calendar quarters, OQ-5) |
| alert_days | JSON list of ints | Y | Default `[15, 5, 1]` days before due date (R13) |
| vat_correction_threshold_sar | Money | Y | Default 15,000.00 **[CC-verify]** (PSG §13.5 p.53) |
| legal_form_for_wht | Choice `company` / `partnership` | Y | Drives annual WHT deadline (R30); default from `Entity.legal_form` |
| income_tax_applies | Bool | computed | True when any active `ShareholderInterest` is `foreign` (R48) |

**StatutoryTaxRate** (*global*; rates are data with dates, never constants)
| Field | Type | Req | Notes |
|---|---|---|---|
| code | Choice `zakat_hijri`, `zakat_gregorian`, `income_tax_foreign_share`, `vat_late_payment_monthly`, `wht_late_payment_30d` | Y | |
| rate | Decimal(7,4) % | Y | Seeds: 2.5000, 2.5775 **[CC-verify]**, 20.0000 **[CC-verify]**, 5.0000 (PSG §14 p.54) **[CC-verify]**, 1.0000 (WHT §5 p.32) **[CC-verify]** |
| valid_from / valid_to | Date / Date null | Y / N | |
| source_citation | Char(300) | Y | Document and article |
| verified_by / verified_at | User / DateTime, null | N | A rate with `verified_at` null shows "unverified" (en/ar) on every return that uses it (R6) |

**VatBoxDefinition** (*global*, versioned by `valid_from`)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| box_no | SmallInt | Y | | 1–16 (§3.2 table) |
| label | Char(200) | Y | Bi | Copied from the ZATCA form once the guide is fetched (OQ-2) |
| side | Choice `output`, `input`, `summary` | Y | | |
| has_amount / has_adjustment / has_vat | Bool ×3 | Y | | Columns shown on the form (TIR §7.1.1 p.38 names "Amount" and "Adjustments" fields) |
| mapping_key | Char(40) | Y | | Rule in R17 to R26 that fills it |
| valid_from / valid_to | Date | Y / N | | |

### 3.2 VAT return

**VAT return boxes (default mapping; whole table [CC-verify] against the ZATCA VAT return guide, OQ-2).** Box 9 = imported services under reverse charge is confirmed (PSG §4.6 p.20; FAQ 1 p.59).

| Box | Label (en) | Label (ar) [AR-review] | Filled from |
|---|---|---|---|
| 1 | Standard rated sales | المبيعات الخاضعة للنسبة الأساسية | Sales category S (R18) |
| 2 | Special cases: private healthcare / private education to citizens | الحالات الخاصة: الخدمات الصحية والتعليمية الخاصة للمواطنين | Sales category Z with VATEX-SA-HEA / VATEX-SA-EDU (XML §11.2.4) (R19) |
| 3 | Zero rated domestic sales | المبيعات المحلية الخاضعة للنسبة الصفرية | Other category Z reasons (VATEX-SA-34-1…5, -35, -36, -MLTRY) (R19) |
| 4 | Exports | الصادرات | Category Z with VATEX-SA-32 / -33 (R19) |
| 5 | Exempt sales | المبيعات المعفاة | Category E (VATEX-SA-29, -29-7, -30) (R19) |
| 6 | Total sales | إجمالي المبيعات | 1 to 5 |
| 7 | Standard rated domestic purchases | المشتريات المحلية الخاضعة للنسبة الأساسية | Resident bills with VAT (R21) |
| 8 | Imports subject to VAT paid at customs | الاستيرادات الخاضعة للضريبة المدفوعة في الجمارك | `ImportVatRecord` (R22) |
| 9 | Imports subject to VAT accounted for through reverse charge | الاستيرادات الخاضعة للضريبة وفق آلية الاحتساب العكسي | Reverse-charge bill lines (invoicing R30) (R23) |
| 10 | Zero rated purchases | المشتريات الخاضعة للنسبة الصفرية | Bill lines category Z (R24) |
| 11 | Exempt purchases | المشتريات المعفاة | Bill lines category E (R24) |
| 12 | Total purchases | إجمالي المشتريات | 7 to 11 |
| 13 | Total VAT due for current period | إجمالي ضريبة القيمة المضافة المستحقة عن الفترة الحالية | R25 |
| 14 | Corrections from previous periods | تصحيحات من الفترات السابقة | `VatCorrection` with treatment `next_return` (R27) |
| 15 | VAT credit carried forward from previous period(s) | الرصيد الدائن المرحل من الفترات السابقة | Prior return net claim not refunded (R25) |
| 16 | Net VAT due (or claim) | صافي الضريبة المستحقة (أو المطالب باستردادها) | 13 + 14 − 15 (R25) |

**VatReturn**
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Must have `vat_number` and `vat_return_period` (R2) |
| period_start / period_end | Date | Y | | From `VatReturnSchedule` (R3) |
| frequency | Choice `monthly` / `quarterly` | Y | | Snapshot of `Entity.vat_return_period` |
| version | SmallInt | Y | | 0 = original; 1+ = amendment (R28). Unique `(entity, period_start, version)` |
| amends | FK self, null | Y if version > 0 | | |
| due_date | Date | Y | | Last day of the month after `period_end` (PSG §13.2 p.52) **[CC-verify]** |
| status | Choice `draft`, `in_review`, `reviewed`, `approved`, `filed`, `settled`, `amended` | Y | | R7 |
| prepared_by/at, reviewed_by/at, approved_by/at, filed_by/at | User / DateTime | per status | | Three different people (R8) |
| zatca_filing_reference | Char(60) | Y at `filed` | | From the ZATCA portal |
| filed_on | Date | Y at `filed` | | |
| acknowledgement | File | Y at `filed` | | Portal receipt (PDF) |
| sadad_bill_number | Char(30) | N | | Payment reference |
| net_vat_sar | Money (signed) | computed | | Box 16; negative = claim |
| refund_requested | Bool | Y | | Default False; False → claim carried forward to box 15 (R25) |
| source_hash | Char(64) | Y at `approved` | | SHA-256 of the frozen source lines (R10) |
| settlement_entry | FK JournalEntry, null, PROTECT | | | Offset entry T1 (R33) |
| notes | Text | N | Bi | |
| source | Choice `human`, `import`, `agent` | Y | | `import` for returns filed before go-live (R12) |

**VatReturnBox**: `return`, `box_no`, `label_en/_ar` (snapshot), `amount_sar`, `adjustment_sar`, `vat_sar` (all signed Money). Computed; never edited by users.

**VatReturnSourceLine** (frozen at approval, immutable): `return`, `box_no`, `column` (`amount` / `adjustment` / `vat`), `source_type` (`sales_document`, `purchase_document`, `import_record`, `correction`, `manual_adjustment`), `source_id`, `document_number`, `journal_entry` (FK), `date`, `amount_sar`. Drill-down target (R11).

**VatManualAdjustment** (only on a `draft`): `return`, `box_no`, `column`, `amount_sar` (signed), `reason_en/_ar` (Bi, `_ar` required), `evidence` (File, required), `journal_lines` (M2M ledger lines it explains, may be empty), `created_by`.

**VatExceptionAck**: `return`, `kind` (`untagged_vat_journal`, `rejected_document`, `tie_out_difference`), `reference`, `decision` (`tagged`, `not_return_relevant`, `included_by_adjustment`), `reason_en/_ar`, `acknowledged_by` (reviewer). Needed before review completes (R15, R16).

**ImportVatRecord** (registered ledger document; replaces the invoicing OQ-29 manual JE)
| Field | Type | Req | Notes |
|---|---|---|---|
| customs_declaration_no | Char(40) | Y | Unique per entity |
| declaration_date | Date | Y | JE date; the return period that contains it (R22) |
| customs_value_sar | Money | Y | VAT base per the declaration **[CC-verify]** |
| vat_paid_sar | Money | Y | |
| attribution | Choice `taxable` / `exempt` / `mixed` | Y | As invoicing R29; `mixed` → cost by default |
| debit_account | FK Account (same entity) | Y if not `taxable` | Cost or asset account for non-recoverable VAT |
| paid_via | Choice `bank`, `customs_broker` | Y | `customs_broker` → credit 2100 on a vendor (same entity) |
| vendor / bank_account | FK Vendor / FK Account | per `paid_via` | |
| declaration_file | File | Y | Evidence of input VAT (PSG §12.1) |
| status / journal_entry | `draft`, `submitted`, `posted`, `reversed` / FK | Y | |

**VatCorrection**
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| original_return | FK VatReturn (`filed`/`settled`) | Y | | Same entity |
| discovered_on | Date | Y | | |
| description | Text | Y | Bi | |
| tax_difference_sar | Money signed | Y | | + = VAT underdeclared |
| treatment | Choice `next_return`, `amendment`, `overstatement_next_return` | computed | | R27 |
| notify_by | Date | computed | | `discovered_on` + 20 days when `amendment` (PSG §13.5 p.53) **[CC-verify]** |
| correcting_entries | M2M JournalEntry (same entity) | N | | Excluded from regular boxes (R26) |
| target_return | FK VatReturn, null | | | Return carrying it in box 14, or the amendment |
| voluntary_disclosure_ref | Char(60) | Y when amendment filed | | ZATCA reference |
| status | Choice `open`, `assigned`, `filed`, `cancelled` | Y | | |

**VatReturnSchedule** (one row per entity and period; generated, editable only while no return exists for it): `entity`, `period_start`, `period_end`, `due_date`, `frequency`.

### 3.3 Withholding tax

**WhtReturn** (monthly statement): `entity`, `month` (first day), `due_date` (10th of the next month, WHT §5 p.30 **[CC-verify]**), `status` (as VatReturn), people and filing fields as VatReturn, `total_wht_sar`, `rows` (frozen from `wht_return_data`, R29), `source_hash`, `payment` (FK TaxPayment).

**WhtBeneficiaryCertificate**: `entity`, `wht_return`, `vendor` (or payroll payee ref), `amount_paid_sar`, `wht_sar`, `issued_on`, `pdf` (File). One per beneficiary per month (WHT §5 p.30). Personal data when the beneficiary is an individual (R54).

**WhtAnnualStatement**: `entity`, `fiscal_year`, `due_date` (FY end + 120 days; partnerships + 60 days; WHT §5 p.30 **[CC-verify]**), `status`, `beneficiaries` (frozen rows: name en/ar, address, registration number, country, category, gross paid, WHT; WHT §5 p.31), `monthly_returns` (M2M WhtReturn), filing fields.

### 3.4 Zakat and income tax

**ShareholderInterest**: `entity`, `holder_entity` (FK Entity, null) or `holder_name_en/_ar`, `holder_class` (`saudi`, `gcc`, `foreign`, `other_exempt`) **[CC-verify classes]**, `pct` Decimal(6,3), `valid_from` / `valid_to`, `evidence` (CR / articles extract). Active rows on any date sum to 100.000 (R45). A group entity holder's class comes from that entity's own classification (Almiras: `saudi`).

**ZakatYear**
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK | Y | | Unique `(entity, fiscal_year_start)` |
| fiscal_year_start / _end | Date | Y | | From `TaxSettings.fiscal_year_end` |
| year_basis | Choice `gregorian`/`hijri` | Y | | Snapshot |
| filing_mode | Choice `standalone`, `group_parent`, `group_member` | Y | | R44 |
| filing_group | FK ZakatFilingGroup, null | Y if not standalone | | |
| zakatable_share_pct | Decimal(6,3) | computed | | Saudi + GCC share, time-weighted over the year (R46) **[CC-verify]** |
| adjusted_net_profit_sar | Money | computed | | Section `profit` lines (R41) |
| zakat_base_sar | Money | computed | | Section `base` lines (R41) |
| base_applied_sar | Money | computed | | R42 |
| rate_applied | Decimal(7,4) | computed | | From `StatutoryTaxRate` on `fiscal_year_end` (R40) |
| zakat_due_sar | Money | computed | | R43 |
| status | Choice `draft`, `in_review`, `reviewed`, `approved`, `provisioned`, `filed`, `assessed`, `settled` | Y | | Same people rules as VAT (R8) |
| template | FK ZakatBaseTemplate | Y | | Version used |
| provision_entry | FK JournalEntry, null | | | T6 |
| filing fields | as VatReturn | | | `due_date` = FY end + 120 days **[CC-verify]** |

**ZakatBaseTemplate** (*global*, versioned, `verified_by/at`): lines `section` (`base_add`, `base_deduct`, `profit_add`, `profit_deduct`), `code`, `label_en/_ar`, `selector` (account codes, ranges or flags, as reporting.md `ReportLine` selector), `requires_evidence` (Bool), `source_citation`. The default lines are a placeholder until the Zakat Implementing Regulations are fetched and checked (OQ-2, OQ-20) **[CC-verify]**.

**ZakatBaseLine**: `zakat_year`, `template_line` (null for manual), `section`, `label_en/_ar`, `amount_sar` (signed), `source` (`ledger` / `manual`), `account_detail` (frozen TB rows), `evidence` (File), `note_en/_ar`.

**ZakatInvestment**: `zakat_year`, `investee_entity` (FK Entity, null) or `investee_name_en/_ar`, `investee_country`, `investee_files_zakat_in_ksa` (Bool), `investee_zakat_certificate` (FK ZakatCertificate, null), `carrying_amount_sar`, `deductible` (computed, R47), `basis_en/_ar`.

**ZakatFilingGroup**: `parent` (FK Entity), `fiscal_year_start`, `members` (M2M Entity), `zatca_approval_reference`, `approved_on`. Every member must be ≥ 100.000% owned directly or indirectly by `parent` through `ShareholderInterest` for the whole year (R44) **[CC-verify]**.

**ZakatAssessment**: `zakat_year`, `kind` (`zatca_assessment`, `objection_outcome`, `appeal_outcome`), `reference`, `date`, `zakat_amount_sar`, `difference_sar` (vs provision), `adjustment` (FK TaxAdjustment, null), `document` (File).

**ZakatCertificate**: `entity` (or `filing_group`), `zakat_year` (FK, null for imported), `certificate_no`, `issued_on`, `valid_until`, `kind` (Char; types **[CC-verify]**), `file`, `source` (`human` / `import`). Unique `(entity, certificate_no)`. Alerts R49.

**IncomeTaxYear**: `entity`, `fiscal_year_start/_end`, `foreign_share_pct` (time-weighted, R46), `taxable_profit_sar` (adjusted profit; lines as ZakatBaseLine with section `tax_add` / `tax_deduct`), `rate_applied`, `tax_due_sar`, `wht_credit_sar` (WHT suffered, if any, WHT p.31 "Paragraph G of Article 68"), `status`, `provision_entry`, filing fields **[CC-verify all]**.

### 3.5 Payments and adjustments

**TaxPayment** (registered ledger document): `entity`, `kind` (`vat`, `wht`, `zakat`, `income_tax`, `penalty`), one of `vat_return` / `wht_return` / `zakat_year` / `income_tax_year` / `penalty`, `amount_sar` (> 0), `payment_date`, `bank_account` (FK Account, same entity, asset), `sadad_bill_number` / `bank_reference`, `status` (`draft`, `submitted`, `posted`, `reversed`), `journal_entry`, `created_by`, `approved_by`.

**TaxRefund** (registered): `entity`, `vat_return`, `amount_sar`, `received_on`, `bank_account`, `zatca_reference`, `journal_entry`.

**TaxAdjustment** (registered): `entity`, `kind` (`zakat_true_up`, `income_tax_true_up`), `source` (FK ZakatAssessment / IncomeTaxYear), `amount_sar` signed, `date`, `reason_en/_ar`, `journal_entry`.

**TaxPenalty** (registered): `entity`, `tax` (`vat`, `wht`, `zakat`, `income_tax`), `zatca_reference`, `assessed_on`, `amount_sar`, `account` (default 5930, proposed in payroll.md), `journal_entry`, `objection_status`.

## 4. Rules

"svc" = `tax.services`; "L" = existing ledger enforcement; "DB" = constraint or trigger; "T" = acceptance test.

### Entity separation and set-up
1. Every `tax` record has an `entity`; all its sources (documents, journal lines, accounts, vendors, shareholder rows) belong to that entity. Querysets in views and APIs use `core.access.scope(...)`. A consolidated Zakat computation (R44) reads member data only through `ledger.services._trial_balance` after checking `tax.prepare_zakat` in **each** member. **[svc + access; T AT-1, AT-2]**
2. A VAT return can be created only for an entity with a `vat_number` and a `vat_return_period`. An entity without a VAT number has no VAT return (X9). **[svc; T AT-3]**
3. `VatReturnSchedule` is generated from `vat_effective_date` (first period starts on that date, partial) and then per `vat_return_period` and `vat_quarter_start_month`. Periods are contiguous, never overlap, and change only by `change_tax_settings` for periods after the last return that exists. **[svc + DB exclusion; T AT-4]**
4. A frequency change (`Entity.vat_return_period`, through `core.services.change_entity_settings`) applies from the first schedule period that starts after the change and has no return. **[svc; T AT-4]**
5. When the sum of boxes 1 to 5 amounts over the last 12 months exceeds SAR 40,000,000 **[CC-verify]** (PSG §13.2 p.52) for a quarterly entity, the approver sees a bilingual warning on every return and the dashboard. No automatic change. **[svc; T AT-5]**
6. Every computed figure that depends on a `StatutoryTaxRate` or `VatBoxDefinition` without `verified_at` shows "unverified / غير متحقق منه" on screen and in exports; approval is still allowed (default, OQ-6). **[svc; T AT-6]**

### Return lifecycle (VAT, WHT, Zakat, income tax)
7. Statuses move only: `draft → in_review → reviewed → approved → filed → settled`; `in_review → draft` and `reviewed → draft` (returned with a reason). An `approved` or later return is **locked**: no recompute, no edit, no delete. `filed → amended` happens only when an amendment (R28) is filed. **[svc state machine + DB trigger; T AT-7]**
8. The preparer (creator, last modifier, submitter), the reviewer and the approver are three different people; the approver holds `tax.approve_return` (approver role) through a grant issued for that return (access R34 pattern). **[svc + access; T AT-8]**
9. Approval needs every `FiscalPeriod` of the entity that overlaps the return period to be `closed`, so no later posting can change the sources. For Zakat and income tax: every period of the fiscal year. **[svc; T AT-9]**
10. On approval the service recomputes all boxes, freezes `VatReturnSourceLine` rows and stores `source_hash`. If the recomputed boxes differ from those reviewed, approval is refused (`sources_changed`, en/ar) and the return goes back to `draft`. **[svc; T AT-10]**
11. Each box and column drills down to its source lines, and each source line to its journal entry and document, with the same entity and permission checks as `ledger.view`. **[svc; T AT-11]**
12. Returns filed before go-live (e.g. Almiras since 2024-04-01 via Daftra) are loaded with `source=import`, status `filed`, box values and filing reference, no source lines; they serve as `original_return` for corrections and as the box 15 source. **[svc import; T AT-12]**
13. Deadline alerts: for each return not `filed` (or payment not `posted`), the entity's accountant and approver see a bilingual alert at each `alert_days` before `due_date`, and daily once overdue with the late-penalty exposure estimate from `StatutoryTaxRate` (display only). Due dates: VAT `VatReturn.due_date`; WHT monthly 10th of the next month; WHT annual and Zakat FY end + 120 days **[CC-verify all]**. No weekend/holiday shift (default, OQ-8). Channel: in-app list (OQ-9). **[svc scheduled job, system user `system_tax_alerts`; T AT-13]**
14. Agents (gateway users) may call `compute_draft` (create or recompute a `draft`) and read; they never submit, review, approve, file or post. **[access Ag flags; T AT-14]**

### VAT return computation
15. **Tie-out.** For the period, Σ output VAT in the boxes (incl. reverse-charge output, R23) must equal the net credits on 2200, and Σ recoverable input VAT the net debits on 1310, excluding entries of `tax` documents (T1 to T4) and entries listed in `VatCorrection.correcting_entries`. Each difference is a `tie_out_difference` exception; review cannot complete until every difference is explained by a `VatManualAdjustment` or acknowledged (`VatExceptionAck`) and the remaining difference is 0.00. **[svc; T AT-15]**
16. **Exceptions.** (a) Manual journal lines on 2200 or 1310 not linked to an invoicing or tax document → `untagged_vat_journal`. (b) Sales documents of the entity with `status=rejected` and issue date in the period → `rejected_document`: VAT may still be due (DTG FAQ p.72); the reviewer records whether a replacement document exists or adds a `VatManualAdjustment`. Review cannot complete while an exception is unacknowledged. **[svc; T AT-16]**
17. **Which period.** A document counts in the return whose period contains the date of its journal entry: `issue_date` for sales documents and notes (X9), `accounting_date` for bills, `declaration_date` for import records. A reversal counts negative in the period of the reversal entry. Only entries in `posted` / `reversed` status count. Tax-point rules other than these dates (earliest of supply, invoice, payment): OQ-10 **[CC-verify]**. **[svc; T AT-17]**
18. **Box 1.** Amount = Σ taxable SAR of category S; VAT = Σ `vat_breakdown.vat_sar` of category S. Taxable SAR = round(category taxable × the SAR rate used for `vat_sar`, 2) (OQ-11). Intercompany documents count like any other (X3). A 388 that deducts a 386 counts net of the deducted amounts (invoicing E17) **[CC-verify]**. **[svc; T AT-18]**
19. **Boxes 2 to 5.** Lines are grouped by category and exemption reason (line `net_amount`, SAR at the document's SAR rate): Z + VATEX-SA-HEA/-EDU → box 2; Z + VATEX-SA-32/-33 → box 4; other Z reasons → box 3; E → box 5; O (VATEX-SA-OOS) → not reported. VAT column 0.00. **[CC-verify]** **[svc; T AT-19]**
20. **Credit and debit notes.** A credit note goes to the **Adjustment** column of its original's box when the return of the original invoice's period was `filed` before the note's `issue_date`; otherwise it reduces the **Amount** column. A debit note always increases the Amount column of the period of its own date (TIR §7.1.1 p.38; X9). **[svc; T AT-20]**
21. **Box 7.** Bills and vendor credit notes from resident vendors with VAT > 0, category S: Amount = net SAR; VAT = recoverable VAT only (lines with VAT on 1310 per invoicing R29). Non-recoverable (exempt / mixed default) VAT is not in the VAT column **[CC-verify]**. Bills from unregistered vendors (0 VAT, X8, invoicing R92) are not reported (default, OQ-12) **[CC-verify]**. **[svc; T AT-21]**
22. **Box 8.** Posted `ImportVatRecord` rows: Amount = `customs_value_sar`, VAT = recoverable `vat_paid_sar` (attribution `taxable`). **[CC-verify base]** **[svc; T AT-22]**
23. **Box 9.** Reverse-charge bill lines (non-resident, services, invoicing R30): Amount = net SAR; the reverse-charge VAT counts as output VAT in box 13, and its recoverable part (1310 debit) as input VAT. How the form shows the two sides: **[CC-verify]** (PSG §4.6 p.20: "Input Tax shall be automatically offset"). Goods from non-residents are not in box 9 (customs, box 8). **[svc; T AT-23]**
24. **Boxes 10, 11.** Resident and non-resident bill lines with category Z → box 10, E → box 11; VAT 0.00. **[CC-verify]** **[svc; T AT-21]**
25. **Totals.** Box 6 = Σ 1–5 amounts and adjustments; box 12 = Σ 7–11; box 13 = Σ output VAT (1–5 and reverse-charge output) − Σ recoverable input VAT (7–11); box 15 = the latest earlier return's negative box 16 when `refund_requested` is False and no `TaxRefund` covers it; box 16 = 13 + 14 − 15. All in SAR, 2 dp, ROUND_HALF_UP. **[CC-verify formula]** **[svc; T AT-24]**
26. A journal entry listed in a `VatCorrection.correcting_entries` is excluded from boxes 1 to 13 of every return and reported only through that correction. **[svc; T AT-25]**
27. **Corrections.** `treatment` is computed from `tax_difference_sar` and `vat_correction_threshold_sar`: |difference| < threshold → `next_return` (box 14 of the next return not yet approved); underdeclared ≥ threshold → `amendment` with `notify_by` = discovered + 20 days and a dashboard alert; overdeclared ≥ threshold → `overstatement_next_return`, allowed until 5 years after the end of the calendar year of the original period (PSG §13.5 p.53, Art. 63) **[CC-verify all]**. Box 14 on the ZATCA form may be limited to ±SAR 5,000 (form label) **[CC-verify]**, OQ-13. **[svc; T AT-26, AT-27]**
28. **Amendment (voluntary disclosure).** An amendment is a new `VatReturn` with `version = original + 1`, `amends` set, same period, full lifecycle (R7 to R10), boxes = original sources + assigned corrections. Filing it requires `voluntary_disclosure_ref`; the original becomes `amended` and stays immutable. The tax difference is paid by a `TaxPayment` linked to the amendment. **[svc; T AT-27]**

### WHT returns
29. `WhtReturn` rows come from `invoicing.services.wht_return_data(entity, month)` (invoicing R64), payroll rows included (X11). The return total must equal the 2220 credits of the month (tie-out as R15). No monthly return is required for a month without `WhtDeduction` rows (WHT §5 p.30); the schedule shows "nothing to file". **Build note:** invoicing R61 to R64 are not built (non-resident payments refused, invoicing R61 note), so this part is built against a stub. **[svc; T AT-28]**
30. The annual statement is required when any `WhtDeduction` exists in the fiscal year, even if the total is 0.00 (WHT §5 p.30); its rows equal the sum of the year's monthly returns per beneficiary; the beneficiaries list holds name, address and registration number where available (WHT §5 p.31). **[svc; T AT-29]**
31. One `WhtBeneficiaryCertificate` (PDF, en/ar) is produced per beneficiary per filed monthly return (WHT §5 p.30). **[svc; T AT-28]**
32. WHT records (returns, certificates, deductions) are kept at least 10 years after payment (WHT §5 p.31); no purge job exists. **[svc: no delete path; DB trigger]**

### Posting
33. On VAT return approval the service posts **T1** (offset) in one step: `create_entry(..., post=True, grant=<tax.approve_return grant>, document=return)`, dated the approval date (must be in an open period), offset = min(output VAT, input VAT + box 15 credit). Zero offset → no entry. **[svc + L; T AT-30]**
34. Tax payments, refunds, adjustments, penalties and import records post only as registered documents (`ledger.documents.register` from `TaxConfig.ready()`), with `build_lines` building T2 to T10; posting needs the type's permission and a person other than the creator, submitter or last modifier (access R35). **[L + svc; T AT-31]**
35. A `TaxPayment` cannot exceed the open amount of its return (VAT: box 16 of the return and its amendments minus posted payments; Zakat: `zakat_due_sar` plus adjustments minus payments). Overpayment refused (en/ar). **[svc; T AT-32]**
36. A posted tax document is corrected only by reversal (`reverse_entry` with `tax.reverse_document`) dated in an open period, then a new document. **[L + svc; T AT-31]**
37. WHT remittance is a `TaxPayment(kind=wht)` building invoicing E22 (Dr 2220 / Cr bank); invoicing does not build a second remittance path (OQ-16). **[svc]**

### Zakat
38. One `ZakatYear` per entity and fiscal year; entities with `zakatable_share_pct` = 0 have none. **[DB unique + svc; T AT-33]**
39. Zakat lines come from the entity's closed-year trial balance through the template selectors, plus manual lines with evidence where `requires_evidence`. Every TB account is either used by a line or listed as "not in base" by the template; an unlisted account blocks review (completeness, same method as reporting.md R10). **[svc; T AT-34]**
40. Rate = `zakat_hijri` (2.5%) for a Hijri year or `zakat_gregorian` (2.5775% **[CC-verify]**) for a Gregorian year, effective on the year end. A year whose length is not a full Hijri or Gregorian year: OQ-21. **[svc; T AT-35]**
41. `adjusted_net_profit_sar` = profit before Zakat and income tax (reporting.md P&L, closing entries excluded) ± `profit_add` / `profit_deduct` lines. `zakat_base_sar` = Σ `base_add` − Σ `base_deduct`. Line content per the Zakat Implementing Regulations **[CC-verify]** (OQ-20). **[svc; T AT-34]**
42. `base_applied_sar` = the higher of `zakat_base_sar` and `adjusted_net_profit_sar` (floor rule) **[CC-verify]**; any cap or other rule waits for the regulations (OQ-20). A negative base gives 0. **[svc; T AT-36]**
43. `zakat_due_sar` = round(`base_applied_sar` × `zakatable_share_pct` / 100 × `rate_applied` / 100, 2). **[CC-verify]** **[svc; T AT-35]**
44. **Consolidated filing.** `filing_mode = group_parent` is allowed only with an approved `ZakatFilingGroup` whose members are wholly owned (direct or indirect, 100.000% for the whole year) Saudi entities **[CC-verify]** (OQ-22). The parent's computation aggregates member TBs with intra-group investments and balances eliminated; each member gets a `ZakatYear` with `group_member` and no own filing. Allocation of the group Zakat to members: OQ-23 (default: none; the parent provides and pays all). **[svc; T AT-37]**
45. Active `ShareholderInterest` rows of an entity sum to 100.000% on every date of the year; otherwise Zakat and income tax computations are refused (en/ar). **[svc; T AT-38]**
46. `zakatable_share_pct` = time-weighted Σ `saudi` + `gcc` holdings over the year; `foreign_share_pct` = time-weighted Σ `foreign` **[CC-verify]**. A group-entity holder counts as its own class. **[svc; T AT-38]**
47. A `ZakatInvestment` is deductible only when `investee_files_zakat_in_ksa` is True and a valid investee certificate or filing evidence is attached; foreign investees are not deducted by default **[CC-verify]** (OQ-20). **[svc; T AT-39]**
48. Zakat is not purification: Zakat uses 5800 / 2210 only; purification uses 4900 / 2600 only (invoicing E14, E15). No `tax` document reads, posts to or nets against 4900 or 2600, and the purification amount (`ledger.services.purification_amount`) is never a deduction in the Zakat base unless the Sharia board decides (OQ-25). Reports show the two separately. **[svc + code-review; T AT-40]**
49. **Certificates.** Each entity's latest `ZakatCertificate` shows on the dashboard with days to `valid_until`; alerts at 60, 30, 7 days before expiry (default, OQ-24) to the accountant, approver and managing partner. An entity with no valid certificate is flagged "no valid Zakat certificate / لا توجد شهادة زكاة سارية". **[svc job; T AT-41]**

### Income tax
50. `IncomeTaxYear` exists only where `foreign_share_pct` > 0. `tax_due_sar` = round(`taxable_profit_sar` × `foreign_share_pct` / 100 × rate / 100, 2) − `wht_credit_sar` (not below 0), rate = `income_tax_foreign_share` (20% **[CC-verify]**). Advance tax payments, losses carried forward and deferred tax are not built (OQ-26). **[svc; T AT-42]**
51. An entity with both shares files one combined Zakat and income tax return **[CC-verify]**; the system produces both computations for the same year and refuses approval of one without the other. **[svc; T AT-42]**

### Sharia, language, privacy
52. Penalties are fines, not interest: they post to 5930 (proposed, payroll OQ-12), never to 5600 / 5610, and carry no purification flag. Tax and Zakat deductibility of fines: payroll OQ-42. **[svc build_lines; T AT-43]**
53. All labels, statuses, exception texts, alerts and exports (box labels, certificates) are bilingual; Arabic texts on WHT certificates and the beneficiaries list are required. **[svc + T AT-44]**
54. Beneficiary names, IDs and addresses of individuals in WHT records are personal data: shown and exported only with `invoicing.view_personal_data` in that entity, each view or export logged (invoicing R51). **[svc + access log; T AT-45]**

## 5. Accounting

SAR. Accounts from `ledger/coa_template.py`; **proposed** codes need the accountant/CFO review (X10). Example period: ALM Q1 2027.

| Event | Entry | Posted by |
|---|---|---|
| VAT return approved | T1 | Approver (approval grant) |
| VAT payment approved | T2 | Approver, not creator |
| Import VAT record approved | T3 | Approver, not creator |
| VAT refund received | T4 | Approver |
| WHT remittance | T5 | Approver |
| Zakat year provisioned | T6 | Approver |
| Zakat / income tax payment | T7, T9 | Approver |
| Assessment difference | T8 | Approver |
| Penalty recorded / paid | T10 | Approver |

**T1. VAT offset at approval** (output 31,500.00 = box 1 30,000.00 + reverse charge 1,500.00; recoverable input 12,750.00 = 9,000.00 + import 2,250.00 + RC 1,500.00; box 16 = 18,750.00)
| Account | Dr | Cr |
|---|---|---|
| 2200 VAT output | 12,750.00 | |
| 1310 VAT input | | 12,750.00 |
After T1, 2200 holds the net due 18,750.00 for Q1. Net claim case: offset = output; 1310 keeps the claim (box 15 next time or T4).

**T2. VAT payment**: Dr 2200 18,750.00 / Cr 1100 (or bank sub-account per banking.md) 18,750.00.

**T3. Import VAT at customs** (attribution `taxable`, VAT 2,250.00, paid by bank): Dr 1310 2,250.00 / Cr 1100 2,250.00. `exempt` / `mixed` (default): Dr `debit_account` / Cr 1100. Via broker: Cr 2100 (vendor).

**T4. VAT refund received**: Dr 1100 / Cr 1310.

**T5. WHT remittance** (invoicing E22): Dr 2220 Withholding tax payable (proposed, invoicing §5) / Cr 1100.

**T6. Zakat provision** (Gregorian year, base applied 4,000,000.00, share 100%, rate 2.5775% **[CC-verify]**: 103,100.00), dated the fiscal year end, before the year close (reporting.md R21):
| Account | Dr | Cr |
|---|---|---|
| 5800 Zakat | 103,100.00 | |
| 2210 Zakat payable | | 103,100.00 |
Hijri year at 2.5%: 100,000.00. Interim (monthly/quarterly) accruals: OQ-27 (default: none).

**T7. Zakat payment**: Dr 2210 / Cr 1100.

**T8. Assessment difference** (ZATCA assesses 108,000.00; difference 4,900.00), dated in the open period of the assessment: Dr 5800 4,900.00 / Cr 2210 4,900.00 (reverse sides for a decrease). Prior-year presentation: reporting.md OQ.

**T9. Income tax** (partly owned MIN: taxable profit 1,000,000.00 × foreign 40% × 20% **[CC-verify]** = 80,000.00): Dr 5810 Income tax expense (**proposed**, مصروف ضريبة الدخل [AR-review]) / Cr 2250 Income tax payable (**proposed**, ضريبة الدخل المستحقة [AR-review]); payment Dr 2250 / Cr 1100. The Zakat on MIN's 60% share uses T6.

**T10. Penalty** (VAT late payment 5% of 18,750.00 = 937.50): Dr 5930 Government fines and penalties (proposed, payroll.md E14) / Cr 2300 Accrued expenses (default, OQ-28); payment Dr 2300 / Cr 1100.

**Proposed accounts** (X10; free codes checked in `coa_template.py` and all specs on 2026-10-17)
| Code | English | Arabic [AR-review] | Type | Flags | Used by |
|---|---|---|---|---|---|
| 2250 | Income tax payable | ضريبة الدخل المستحقة | liability | monetary | T9 |
| 5810 | Income tax expense | مصروف ضريبة الدخل | expense | | T9 |
2200, 1310, 2210, 5800, 2220 (invoicing proposal) and 5930 (payroll proposal) are reused. No separate "VAT payable (net)" account (brief: Dr 2200 / Cr bank).

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| ZATCA portal (VAT, WHT, Zakat, income tax) | Out (manual) | Return forms per box (en/ar screen and PDF/XLSX export) keyed in by the accountant | No filing API assumed **[CC-verify: whether ZATCA offers a return API]** (OQ-29) |
| ZATCA portal | In (manual) | Filing reference, acknowledgement PDF, SADAD bill number, assessments, refund decisions, Zakat certificates | Entered by the accountant with files |
| Banks | Out / In | Payments made in online banking or SADAD; statement lines matched to T2/T5/T7/T9 by banking.md | No payment file (invoicing OQ-21) |
| `invoicing` | In | Posted sales/purchase documents, `vat_breakdown`, line categories and reasons, `WhtDeduction`, `wht_return_data` | Change requests: store the SAR VAT rate per document (OQ-11); `replaced_by` on rejected documents (OQ-14) |
| `payroll` | In | Remuneration WHT rows (X11) | payroll OQ-30 |
| `ledger` | Both | TB, 2200/1310/2220/2210 movements; T1 to T10 entries | Registered document types |
| `reporting` | Out | Zakat, VAT and income tax note data; profit before Zakat for R41 | reporting.md §6 |
| GOSI / Mudad | none | GOSI fines are booked by payroll (5930) | Their Zakat/tax treatment: payroll OQ-42 |
| SAP B1 / Daftra | In (one-off) | Filed VAT returns since 2024-04-01, WHT statements, last Zakat filing and certificate, credit carried forward | `source=import` (R12); migration-agent ties them to opening balances of 2200/1310/2210 |
| Agent gateway | In | `compute_draft` only | R14 |

## 7. Permissions

Per entity (access.md roles). New permissions to add to the access catalogue: `tax.view`, `tax.prepare_return`, `tax.review_return`, `tax.approve_return` (A, P), `tax.record_filing`, `tax.approve_payment` (A, P), `tax.reverse_document` (P), `tax.prepare_zakat`, `tax.manage_shareholders` (A), `tax.change_settings` (A), `tax.maintain_rates` (global, group parent).

| Action | Accountant | Approver (CFO) | Managing Partner | Viewer / auditor | `system_tax_alerts` | Agent |
|---|---|---|---|---|---|---|
| Prepare / recompute draft (VAT, WHT, Zakat, income tax) | Y | | | | | Draft only (R14) |
| Submit for review | Y (preparer) | | | | | No |
| Review, acknowledge exceptions | Y (not preparer) | | | | | No |
| Approve (locks, posts T1/T6/T9) | | Y (not preparer/reviewer) | | | | No |
| Record filing, upload acknowledgement and certificates | Y | Y | | | | No |
| Create / submit tax payment, refund, penalty, import record | Y | | | | | No |
| Approve (post) them | | Y (not own) | | | | No |
| Reverse a tax document | Y | | | | | No |
| Shareholder interests, `ZakatFilingGroup` | Y (draft) | Y (approve) | Y (group filing approval) | | | No |
| Tax settings | | Y | | | | No |
| Statutory rates and box definitions (global) | Y (draft at group parent) | Y (approve at group parent, not own) | | | | No |
| View returns, drill-down, exports | Y | Y | Y | Y | Due dates only | Scoped read |
| View individuals' WHT data | with `invoicing.view_personal_data`, logged | same | same | masked | No | No |

Partly owned and minority entities need an explicit membership (access OQ-3 default).

## 8. Acceptance tests

Fixtures: ALM (parent, quarterly, Sharia enforced, Gregorian year), SUB (100% ALM, monthly), MIN (60% ALM, 40% foreign holder `FX1`), NOV (no VAT number). Made-up VAT and CR numbers, never X2 values. Users: A1, A2 (accountant), C1 (approver), V1 (viewer), agent G1. Statutory rows seeded as above, unverified.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | A1 accountant in ALM only | List returns; open SUB's Q1 return by id | Only ALM rows; SUB refused, logged |
| AT-2 | A1 has `tax.prepare_zakat` in ALM but not in SUB; group filing ALM+SUB | Compute ALM `group_parent` | Refused (`no_membership` for SUB), nothing created |
| AT-3 | NOV | Create VAT return | Refused (en/ar "entity not VAT-registered") |
| AT-4 | Entity with `vat_effective_date` 2027-02-10, quarterly | Generate schedule; switch to monthly on 2027-05-15 | Periods 2027-02-10..03-31, 04-01..06-30, then monthly from 07-01; overlap insert refused |
| AT-5 | Quarterly entity, 12-month boxes 1–5 = 40,000,000.01 | Open return | Warning shown; frequency unchanged |
| AT-6 | Rate `zakat_gregorian` unverified | Export Zakat computation | "unverified / غير متحقق منه" printed; approval allowed |
| AT-7 | Approved return | Edit box, recompute, delete; raw SQL UPDATE on PostgreSQL | All refused; values unchanged |
| AT-8 | A1 prepared | A1 reviews; C1 reviews then approves; A2 reviews, A2 approves | First refused; C1 cannot approve own review; A2 cannot approve (no right); A2 review + C1 approve succeeds |
| AT-9 | Q1 return reviewed; 2027-03 period open | Approve | Refused (`periods_open`, lists 2027-03) |
| AT-10 | Reviewed return; a reversal is posted in 2027-03 before close, then periods closed | Approve | Refused `sources_changed`; return back to `draft` |
| AT-11 | Approved return | Drill box 1 VAT | Lines sum to box; each opens its JE and invoice; V1 without SUB rights sees no SUB data |
| AT-12 | Daftra export of 2026-Q4 return (box 16 −4,000.00, no refund) | Import; prepare 2027-Q1 | Imported `filed`, `source=import`; Q1 box 15 = 4,000.00 |
| AT-13 | Q1 due 2027-04-30, alert_days [15,5,1] | Run job on 04-15, 04-25, 04-29, 05-01 | Alerts on 04-15/25/29 to A1 and C1; 05-01 overdue alert with exposure; nothing after filing |
| AT-14 | Agent G1 | `compute_draft`; then submit | Draft created (`source=agent`); submit refused |
| AT-15 | Manual JE Cr 2200 100.00 in Q1 not tagged | Submit for review / review | Exception `untagged_vat_journal` and tie-out 100.00; review refused until tagged or acknowledged with reason |
| AT-16 | Simplified invoice rejected by ZATCA in Q1, no replacement | Review | `rejected_document` exception; review blocked until acknowledged or adjusted |
| AT-17 | Bill accounting_date 2027-03-31 reversed 2027-04-05 | Prepare Q1 and Q2 | Q1 box 7 includes it; Q2 box 7 shows the negative amount |
| AT-18 | ALM S invoice net 10,000.00 VAT 1,500.00; USD invoice net USD 1,000.000, SAR rate 3.7500 | Prepare | Box 1 amount 13,750.00, VAT 1,500.00 + 562.50 |
| AT-19 | Lines Z/VATEX-SA-32, Z/VATEX-SA-HEA, Z/VATEX-SA-35, E/VATEX-SA-30, O/VATEX-SA-OOS | Prepare | Boxes 4, 2, 3, 5; O nowhere; VAT column 0.00 |
| AT-20 | Q1 invoice; Q1 return filed 2027-04-20; credit note issued 2027-04-25; another CN issued 2027-03-30 on a Q1 invoice | Prepare Q1 / Q2 | 03-30 CN reduces Q1 amount; 04-25 CN in Q2 box 1 Adjustment column; a Q2 debit note on a Q1 invoice in Q2 Amount |
| AT-21 | Bills: resident S taxable 1,500.00 VAT; resident S exempt-attributed 300.00 VAT; unregistered vendor 0 VAT; Z bill; E bill | Prepare | Box 7 amount both nets, VAT 1,500.00; unregistered not reported; Z box 10; E box 11 |
| AT-22 | Import record VAT 2,250.00 `taxable`; another 450.00 `mixed` | Post; prepare | T3 lines (1310 / cost); box 8 VAT 2,250.00, amounts both |
| AT-23 | Non-resident services bill 10,000.00 RC 1,500.00 taxable; non-resident goods bill | Prepare | Box 9 amount 10,000.00; RC 1,500.00 in output and input; goods bill not in box 9 |
| AT-24 | AT-21..23 values + box 15 4,000.00 | Prepare | Box 13 and 16 per R25; rounding half-up |
| AT-25 | Correction with correcting JE in Q2 (missed Q1 invoice, VAT 2,000.00) | Prepare Q2 | Entry excluded from Q2 box 1; box 14 = 2,000.00 |
| AT-26 | Underdeclared 14,999.99 / 15,000.00 / overdeclared 20,000.00 | Save corrections | `next_return` / `amendment` with notify_by +20 days and alert / `overstatement_next_return` |
| AT-27 | `amendment` correction on filed Q1 | Create amendment; file without disclosure ref; with ref | Version 1 created; filing refused, then accepted; Q1 v0 `amended`; payment links v1 |
| AT-28 | WHT stub: March 2 vendors, 2 categories, total 1,250.00 = 2220 credits; April none | Prepare March; April | March rows and 2 certificates (en/ar PDF); April "nothing to file"; 2220 mismatch 0.01 blocks review |
| AT-29 | Year with deductions netting to 0.00 | Prepare annual statement | Required; rows per beneficiary = Σ monthly; due FY end + 120 days |
| AT-30 | Q1 approved (AT-24 values); claim case: output 5,000.00, input 7,000.00 | Approve | T1 Dr 2200 / Cr 1310 12,750.00; claim: offset 5,000.00; zero case: no entry |
| AT-31 | Payment drafted by A1 | A1 approves; C1 approves; A1 reverses with closed original period | A1 refused; T2 posted by C1; reversal dated in open period |
| AT-32 | Box 16 18,750.00; paid 18,000.00 | Pay 751.00 | Refused (overpayment); 750.00 allowed |
| AT-33 | SUB 100% Saudi-held; FX-only foreign entity | Create Zakat years | SUB created; foreign-only refused (share 0) |
| AT-34 | Template without account 1520 | Prepare ALM Zakat | Review blocked listing 1520 |
| AT-35 | Base applied 4,000,000.00, share 100% | Gregorian / Hijri | 103,100.00 / 100,000.00 |
| AT-36 | Base 500,000.00, adjusted profit 900,000.00; negative base case | Compute | Base applied 900,000.00; negative → 0 |
| AT-37 | Group ALM+SUB; then MIN added | Approve group | ALM+SUB allowed; MIN refused (60%) |
| AT-38 | MIN rows 60% + 30% | Compute | Refused (sum 90%); with 60/40: zakatable 60.000, foreign 40.000; mid-year change time-weighted |
| AT-39 | Investment in SUB with certificate; foreign investee | Compute | SUB deductible; foreign not |
| AT-40 | ALM with flagged 4900 income 1,200.00 | Compute and provision Zakat | Base excludes the purification amount; no line on 4900/2600; reports show Zakat and purification apart |
| AT-41 | ALM certificate valid_until 2027-04-30 (fixture value) | Run job on 03-01, 03-31, 04-23, 05-01 | Alerts at 60/30/7 days; 05-01 "no valid certificate" |
| AT-42 | MIN taxable profit 1,000,000.00, WHT credit 5,000.00 | Compute; approve Zakat only | Tax 75,000.00; approving Zakat alone refused |
| AT-43 | VAT late penalty 937.50 | Post; try account 5610 | T10 on 5930; 5610 refused (Sharia) |
| AT-44 | Any return | Export en, ar | All labels present in both; Arabic export RTL |
| AT-45 | Individual beneficiary | V1 opens annual statement; export | Name masked; with right shown and logged |

## 9. Open questions

**B** = blocking for build of that part, **GL** = blocking for go-live, **D** = default stands.

| # | Question | Owner | Status | Default |
|---|---|---|---|---|
| OQ-1 | Does the owner confirm building a VAT return module, superseding invoicing D19 / OQ-29 ("no VAT-return report for now")? | Project owner | **B** (whole VAT part) | Spec only; no build |
| OQ-2 | Documents to fetch: ZATCA VAT return filing guide (box definitions, Arabic labels, box 2/9/14 rules); VAT Implementing Regulations (Arts. 40, 49–51, 59–63, 69); Input Tax Deduction guideline; RCM / imported services guidance; VAT on imports guide; Zakat Implementing Regulations (2024) and ZATCA Zakat guides (base, investments, consolidated return for wholly owned groups, certificates); Income Tax Law and Implementing Regulations (mixed ownership, rate, deadlines); WHT annual form spec; ZATCA penalties guideline. | compliance-checker | **B** for every [CC-verify] rule | Values as shown, unverified |
| OQ-3 | Fiscal year end: field on `Entity` (shared with reporting) or `TaxSettings`? | Dev + accountant | D | `TaxSettings.fiscal_year_end` 12-31 |
| OQ-4 | Is there a ZATCA requirement to keep any return data longer than the 6-year basic period (PSG §13.3 p.53: 6 / 11 / 15 years)? | compliance-checker | D | No purge job |
| OQ-5 | Are quarterly periods calendar quarters or assigned from the registration date? Almiras registered 2024-04-01. | Accountant | D | Calendar quarters, partial first period |
| OQ-6 | May a return be approved while a rate or box definition is unverified? | CFO | D | Allowed with "unverified" mark |
| OQ-7 | Segregation for one-accountant entities (preparer ≠ reviewer ≠ approver). | Managing Partner | D | Enforced: three different people, no exception |
| OQ-8 | Due dates on weekends or holidays move to the next working day? | compliance-checker | D | No shift |
| OQ-9 | Alert channel (e-mail, SMS) beyond in-app. | Managing Partner | D | In-app list only |
| OQ-10 | Tax point: use earliest of supply, invoice, payment (and receipt date for advances) instead of the JE date? Linked: invoicing OQ-51. **[CC-verify]** | Tax adviser | D | JE date (R17) |
| OQ-11 | SAR rate for taxable amounts: invoicing stores `total_vat_sar` but not the rate; store `vat_sar_rate` on documents? | Dev + invoicing owner | **B** for non-SAR documents | Recompute from the ledger rate on `issue_date` |
| OQ-12 | Unregistered-vendor bills (0 VAT, category O default, invoicing OQ-60): reported in box 11 or not at all? | Tax adviser | D | Not reported |
| OQ-13 | Box 14 limit on the form (±5,000) vs PSG threshold 15,000; which threshold applies to self-correction? | Tax adviser | D | 15,000.00 threshold, box 14 any amount below it |
| OQ-14 | Rejected sales documents: add `replaced_by` link in invoicing so R16 is automatic? | Invoicing owner | D | Manual acknowledgement |
| OQ-15 | Art. 51 apportionment (`mixed`) and annual true-up: in which box and period does the true-up go? | Tax adviser | D | Mixed VAT to cost (invoicing R29); no true-up |
| OQ-16 | Confirm `TaxPayment(kind=wht)` is the only WHT remittance path (invoicing R64 / E22). | Invoicing owner | D | Yes |
| OQ-17 | WHT for non-residents paid by payroll: certificates and beneficiary list for individuals abroad (PDPL). | Legal | GL | Masked as R54 |
| OQ-18 | Is a VAT refund request filed per return or separately; record its status? | Accountant | D | `refund_requested` + `TaxRefund` |
| OQ-19 | Bad-debt VAT relief (PSG FAQ 4) and its box. | Tax adviser | D | Not built |
| OQ-20 | Zakat base method for holding companies: base components, adjusted net profit add-backs (provisions, fines, GOSI fines payroll OQ-42), investments in subsidiaries/associates/foreign entities, cap rules, treatment of 2600 and 2500. **[CC-verify]** | Tax adviser | **B** for Zakat go-live | Template placeholder; floor rule R42 |
| OQ-21 | Short or long first year (new subsidiary): rate pro-rata by days? | Tax adviser | D | Rate × days / 354 or 365 **[CC-verify]**; flagged |
| OQ-22 | Consolidated Zakat return: conditions (100% direct/indirect, Saudi only, ZATCA approval), and effect on members' certificates. **[CC-verify]** | Tax adviser | **B** for group filing | Standalone only |
| OQ-23 | Group Zakat allocation to members (recharge via 1210/2110 or none). | CFO | D | None |
| OQ-24 | Confirm Almiras certificate number, kind and `valid_until` 2027-04-30 (from the brief) to load as setup data; alert days. | Accountant | D | Entered by user; 60/30/7 days |
| OQ-25 | Does purified income (4900 → 2600) reduce the Zakat base, and is 2600 a deductible liability? | Sharia board + tax adviser | D | No (R48) |
| OQ-26 | Income tax: advance payments, losses, deferred tax (IAS 12 / SME s29), combined return form. **[CC-verify]** | Tax adviser | **B** for income tax go-live | Not built |
| OQ-27 | Interim Zakat accruals (monthly/quarterly) for management accounts. | CFO | D | Year-end only |
| OQ-28 | Penalty credit account (2300, or a new tax penalties payable). | Accountant | D | 2300 |
| OQ-29 | Does ZATCA offer a filing API for VAT/WHT/Zakat returns usable by in-house software? **[CC-verify]** | compliance-checker | D | Manual filing |
| OQ-30 | Shareholder classes for Zakat/tax (GCC nationals, listed shares, government) **[CC-verify]**. | Tax adviser | D | `saudi`, `gcc`, `foreign`, `other_exempt` |

**Blocking summary.** Build: OQ-1 (owner confirmation), OQ-2 (documents for every [CC-verify]), OQ-11 (non-SAR documents), WHT part waits for invoicing R61 to R64 (stub). Go-live: OQ-20 (Zakat base), OQ-22 (group filing), OQ-26 (income tax), OQ-17 (PDPL). Review before merge: proposed accounts 2250, 5810 (X10).
