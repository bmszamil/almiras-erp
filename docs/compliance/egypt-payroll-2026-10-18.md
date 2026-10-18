# Egypt payroll rules: research for the payroll module (2026-10-18)

Prepared by: compliance research agent, 2026-10-18. For: payroll OQ-28 / hr OQ-26 (Egypt is the only foreign jurisdiction to activate, payroll P19(f), R4).
Status: **research, not legal advice.** Every figure below is meant to be seeded as an `hr.StatutoryParameter` row with `jurisdiction = EG`, `status = unverified` (hr 3.9, R6, R7). An Egyptian accountant or lawyer must confirm each row, and the Managing Partner (or another `hr.verify_statutory_parameter` holder who is not `entered_by`) must verify it, before an Egypt run can be approved (`HR_REQUIRE_VERIFIED_PARAMETERS=True`).

## How this was researched, and its limits

- **No official page could be opened.** WebFetch and curl were blocked from this environment (DNS failure, then proxy `403` on CONNECT) for eta.gov.eg, nosi.gov.eg, the Andersen English translations of Law 14/2025 and Law 148/2019, PwC, EY and Deloitte. Every figure therefore comes from **search-engine result extracts** of the pages cited. These are mostly law-firm, Big-4, press and HR-vendor pages, plus Arabic legal-text mirrors (eastlaws, wikisource, lawyeregypt). No figure here was read directly from the Official Gazette (الجريدة الرسمية) or an authority website.
- **Confidence levels.** **H** = at least two independent reputable sources agree and the text of the law is quoted. **M** = reputable sources agree but there is no statute text, or one source quotes the statute. **L** = one secondary source, sources conflict, or this is my reading.
- The source URL given for each row is the best page found. A verifier should replace it with the Gazette or authority reference (`source_reference`, for example "Law 14/2025 Art. 117").
- Currency: EGP, ISO 4217 minor unit 2. Amounts are stored as Decimal strings with 2 decimals (CLAUDE.md rule 7). Egypt is not the group's functional currency, so the payroll entity's `base_currency` = EGP (hr H10, R53).

## 1. Parameter table (seed candidates, jurisdiction `EG`, all `unverified`)

Codes follow the existing SA naming (`hours.max_daily`, `leave.annual.days`, …). Values are JSON with Decimal strings. "valid_from" is the effective date the sources give.

### 1.1 Labour Law No. 14 of 2025

| Code | Value | valid_from | Source URL | Conf. |
|---|---|---|---|---|
| `labour.law_in_force` | `{"law": "14/2025", "replaces": ["12/2003", "125/2010"], "gazette": "Issue 18 (cont.), 2025-05-03"}` | 2025-09-01 | https://taxnews.ey.com/news/2025-1555 ; https://www.eastlaws.com/legislation-full-text/ar/egypt/law/03-05-2025/no-14?type=1&id=2165366&sub=11404110 | H |
| `hours.max_daily` / `hours.max_weekly` | `"8"` / `"48"` actual working hours, excluding meal and rest breaks (Art. 117) | 2025-09-01 | https://eg.andersen.com/wp-content/uploads/2025/05/Translation-of-Labor-law-No.-14-of-2025.pdf | H |
| `hours.break_rules` | `{"min_total_break_hours": "1", "max_continuous_hours": "5"}` | 2025-09-01 | https://www.zenhr.com/en/egypt-labor-law | M |
| `hours.max_presence_daily` | `"10"` (up to `"12"` for intermittent or special work, Art. 119). Sources conflict | 2025-09-01 | https://www.lexismiddleeast.com/news/2026-03-26_45/en | L |
| `rest.weekly_paid_hours` | `"24"` continuous, paid (Art. 120). May be accumulated up to 8 weeks in remote or continuous operations | 2025-09-01 | Andersen translation (above) | M |
| `overtime.premium` | `{"day": "0.35", "night": "0.70", "rest_day": "1.00"}` premium over the hourly wage. Rest-day work also earns a compensatory day the following week | 2025-09-01 | https://globallawexperts.com/how-to-calculate-overtime-in-egypt/ ; https://www.mondaq.com/employee-rights-labour-relations/1791586/ | M |
| `overtime.notification_days` | `"7"`: post-notification to the Ministry within 7 days (replaces prior approval) | 2025-09-01 | globallawexperts (above) | M |
| `probation.max_days` | `"90"` (3 months; once per employer, Art. 90) | 2025-09-01 | https://hnzlaw.com/probation-period-under-labor-law-14-of-2025-what-employers-and-hr-professionals-need-to-know/ | H |
| `leave.annual.days` | `{"first_year_after_6_months": "15", "from_year_2": "21", "after_10_years_or_age_50": "30", "disabled": "45"}` | 2025-09-01 | https://www.paulhastings.com/insights/practice-area-articles/egypt ; https://www.zenhr.com/en/egypt-labor-law ; https://www.shandpartners.com/insights/firm-news/new-labour-law-no-14-of-2025/ | M |
| `leave.sick.tiers` | `[{"days": "90", "pct": "0.75"}, {"days": "90", "pct": "0.85"}]`, max 180 days per year, % of insurable wage. Law 14 Art. 131 refers to the Social Insurance Law. Industrial establishments: 100/85/75 variant | 2020-01-01 (SI Law) | https://www.dostor.org/5300125 ; https://www.atlashxm.com/resources/employee-benefits-in-egypt | L |
| `leave.maternity` | `{"months": "4", "min_post_birth_days": "45", "max_times": "3"}` (Art. 54 per one source). Pay comes from NOSI if the employee is insured | 2025-09-01 | https://www.gloroots.com/leave-policy/egypt ; https://www.shandpartners.com/insights/firm-news/new-labour-law-no-14-of-2025/ | M |
| `termination.notice_days` | `"90"` (3 months, either party, regardless of service, Art. 156). During notice the employee may take 1 day or 8 hours per week to look for work. Waived notice = wage for the remaining period | 2025-09-01 | https://eg.andersen.com/egypts-labour-law-14-2025/ ; https://taxnews.ey.com/news/2025-1555 | M |
| `termination.unjustified_comp_months_per_year` | `"2"` minimum, total wage, indefinite contract (Art. 165) | 2025-09-01 | https://eg.andersen.com/egypts-labour-law-14-2025/ ; https://amereller.com/publication/egypt-new-labor-law-no-14-of-2025/ | M |
| `termination.fixed_term_early_comp_months_per_year` | `"1"` per year or part year, total wage (Art. 154 per one source) | 2025-09-01 | https://hnzlaw.com/fixed-term-contract-rules-under-the-new-egyptian-labor-law-law-no-14-of-2025/ | L |
| `termination.economic_change_comp` | `{"years_1_5_months_per_year": "1", "after_year_5_months_per_year": "1.5"}`: employee terminates after an economic change to terms or a restructuring | 2025-09-01 | https://www.elwatannews.com/news/details/8341012 ; https://taxnews.ey.com/news/2025-1555 | L |
| `wage.minimum.private_monthly` | `"7000.00"` EGP gross (National Wages Council). Firms with fewer than 10 workers exempt | 2025-03-01 | https://employsome.com/blog/minimum-wage-egypt/ ; https://www.elwatannews.com/news/details/8314602 ; https://www.almashhad.com/article/174515324408726-economy/906149631622523 | M |
| `wage.minimum.private_monthly` (prior row) | `"6000.00"` | 2024-05-01 | employsome (above) | M |
| `wage.minimum.part_time_hourly` | `"28.00"` EGP | 2025-03-01 | employsome (above) | L |
| `wage.annual_increment` | `{"min_pct_of_insurable_wage": "0.03", "min_amount_monthly": "250.00"}`. The 3% is in Art. 12; the EGP 250 floor is from the NWC decision of Feb 2025, not the law | 2025-09-01 (3% law) / 2025-03-01 (EGP 250) | https://ar.wikisource.org/wiki/قانون_العمل_14_لسنة_2025_-_مصر ; employsome (above) | M (3%) / L (250) |
| `deduction.loan_max_pct` | `"0.10"` of wage, after income tax (and SI) deducted. Written consent required. No interest (Art. 113) | 2025-09-01 | https://ar.wikisource.org/wiki/قانون_العمل_14_لسنة_2025_-_مصر ; https://www.elwatannews.com/news/details/8356604 | H |
| `deduction.debt_max_pct` / `deduction.nafaqa_max_pct` | `"0.25"` / `"0.50"`. Order when they compete: nafaqa (maintenance) first, then the employer's claims (Art. 113/114, numbering differs between sources) | 2025-09-01 | https://www.elwatannews.com/news/details/8356604 ; https://www.elwatannews.com/news/details/8353424 | H |
| `records.retention_years_after_termination` | `"5"` (employee file) | 2025-09-01 | https://www.paulhastings.com/insights/practice-area-articles/egypt | L |
| `holidays.public_count` | `"13"`, set by decree each year (the dates are data, not one parameter) | yearly | https://www.zenhr.com/en/egypt-labor-law | L |

### 1.2 Social Insurance and Pensions Law No. 148 of 2019 (NOSI)

| Code | Value | valid_from | Source URL | Conf. |
|---|---|---|---|---|
| `si.rates` | `{"employee": "0.11", "employer": "0.1875"}` of the insurable (contribution) wage, within the min/max | 2020-01-01 | https://www.mondaq.com/employee-rights-labour-relations/1810754/social-insurance-in-egypt ; https://eg.andersen.com/social-insurance-in-egypt/ ; https://www.careerians.com/egyptian-labor-law/قانون-التأمينات-الاجتماعية-الجديد-رق/ | H |
| `si.rates_by_branch` | Old age, disability and death: employer `"0.12"`, employee `"0.09"` (Art. 19). The other branches (injury, sickness, unemployment, end-of-service lump sum) **do not reconcile across sources** (see §4) | 2020-01-01 | https://lawyeregypt.net/قانون-التأمينات-الاجتماعية-رقم-148-لسنة-2019 ; https://lexcouncil.com/en/tools/social-insurance-calculator | L |
| `si.wage_min_monthly` / `si.wage_max_monthly` | `"2300.00"` / `"14500.00"` | 2025-01-01 | https://www.youm7.com/story/2024/12/7/التأمينات-القانون-148-وضع-آلية-سنوية-لزيادة-الحد-الأدنى-والأقصى/6803621 ; https://www.legal500.com/intelligence/egypt/employment-and-hr/social-insurance-in-egypt | H |
| `si.wage_min_monthly` / `si.wage_max_monthly` | `"2700.00"` / `"16700.00"` | 2026-01-01 | https://www.businesstodayegypt.com/Article/1/6950/ ; https://www.alborsaanews.com/2025/11/30/1932519 ; https://www.bdo.global/en-gb/insights/tax/expatriate-tax/egypt-maximum-and-minimum-salaries-subject-to-social-insurance-increased | H |
| `si.wage_indexation` | `{"annual_pct": "0.15", "each": "January 1", "through": "2027"}` (Law 148 schedule, 7 years from 2021, per BDO). The rule after 2027 was not found | 2021-01-01 | BDO (above) | M |
| *(not to seed)* 2027 projection | min `"3200.00"`, max `"19300.00"` (one blog and Careerians). Seed only after NOSI announces it (usually late November) | 2027-01-01 | https://www.careerians.com/egyptian-labor-law/الاجر-التأميني-2027/ | L |
| `si.payment_due` | Contributions for month M are due on the **1st of month M+1** (Art. 121, per press summary). Many payroll guides say the 15th instead (§4) | 2020-01-01 | https://egyls.com/حال-تأخر-الاشتراكات-قانون-المعاشات-يلزم-الجهات-وأصحاب-الأعمال-بسداد-مبالغ-إضافية/ ; https://www.vetogate.com/4526073 | L |
| `si.late_additional_rate` | Monthly additional amount = average yield of treasury bills and bonds issued in the previous month **+ 2%**, from the due date to the end of the month of payment (Art. 121) | 2020-01-01 | egyls / vetogate (above) | L |
| `si.foreigners_covered` | `true`: foreigners on local contracts are covered. The new law does not limit coverage to reciprocity countries (old Law 79/1975 did). Bilateral agreements may give exemption or totalisation | 2020-01-01 (regs 2021-09-28, PM Decree 2437/2021) | https://global.lockton.com/us/en/news-insights/egypt-requires-foreign-employees-to-participate-in-the-national-social-security-scheme ; https://www.lexology.com/library/detail.aspx?g=fb071754-b133-4ccb-8b41-7e4ddb7631af | M |
| `si.registration_days` | `"15"` or `"30"` from hire (sources conflict). Form 1 = employee; Form 2 = establishment file | 2020-01-01 | https://www.remotepass.com/country/egypt/employment-laws ; https://asanify.com/global-employer-of-record/egypt/payroll/ | L |

**Insurable wage (أجر الاشتراك):** "the cash remuneration received by the insured person … from the original place of employment, in exchange for the main job". It covers basic and functional wage, incentives, commissions, overtime, cost-of-living and most allowances and some bonuses, with listed exclusions (Law 148 Art. 1 definitions, Andersen translation per search extract). It is one combined wage (no more basic/variable split as under Law 79/1975). It is capped at `si.wage_max_monthly` and floored at `si.wage_min_monthly`. Which ERP salary components count is a component flag (`counts_for_social_insurance`); the classification of each allowance needs local sign-off.

### 1.3 Income Tax Law No. 91 of 2005, as amended (salary tax, ETA)

| Code | Value | valid_from | Source URL | Conf. |
|---|---|---|---|---|
| `tax.salary.personal_exemption_annual` | `"20000.00"` EGP (residents and non-residents), up from 15,000 | 2024-03-01 (Deloitte: "from March 2024 for employees"; PwC: 21 Feb 2024, §4) | https://www.deloitte.com/middle-east/en/services/tax/perspectives/amendment-to-tax-brackets-and-annual-personal-exemption.html ; https://taxsummaries.pwc.com/egypt/individual/taxes-on-personal-income | H |
| `tax.salary.brackets` | Annual net taxable income: 0–40,000 at 0%; 40,000–55,000 at 10%; 55,000–70,000 at 15%; 70,000–200,000 at 20%; 200,000–400,000 at 22.5%; 400,000–1,200,000 at 25%; above 1,200,000 at 27.5% (Law 7/2024 amending Art. 8) | 2024-03-01 | PwC (above); https://eg.andersen.com/income-tax/ ; https://www.dostor.org/4641846 | H |
| `tax.salary.bracket_exclusion` | Lower bands are lost as income rises (see table below). The column is chosen by total annual net taxable income | 2024-03-01 | https://www.a-h-g.net/ar/income-tax-brackets-table/ ; https://mercans.com/resources/statutory-alerts/egypt-income-tax-law-amendment/ ; https://eg.andersen.com/ar/تعديلات-ضريبة-الدخل/ | M (structure) / L (exact bands) |
| `tax.salary.zero_band_residents_only` | `true`: per PwC, the 0% first EGP 40,000 applies only to residents | 2024-03-01 | https://taxsummaries.pwc.com/egypt/individual/deductions | L |
| `tax.salary.insurance_deduction_cap` | `{"pct_of_net": "0.15", "max_annual": "10000.00"}`, whichever is lower, for life/health premiums, private insurance funds and pension premiums. The employee's NOSI share is fully deductible | (in force; date not found) | https://taxsummaries.pwc.com/egypt/individual/deductions | M |
| `tax.salary.remit_due_day` | `"15"`: withheld tax for month M is paid to ETA by the 15th of M+1 | (Law 206/2020 regime) | https://www.activpayroll.com/global-insights/egypt ; https://assets.kpmg.com/content/dam/kpmgsites/xx/pdf/2023/01/TIES-Egypt.pdf.coredownload.inline.pdf | M |
| `tax.salary.quarterly_return` | Form 4 (salary tax return), quarterly, within the month after each quarter (January, April, July, October), via the ETA e-portal, with employee detail and payment receipt | 2020 (Law 206/2020) | https://www.pwc.com/m1/en/tax/documents/2020/egyptian-government-issues-unified-tax-procedures-law-no-206-2020.pdf ; https://countryguides.andersen.com/country-pages/egypt/at-doingbusinessin-egypt-final.pdf | M |
| `tax.salary.annual_settlement` | Annual salary-tax settlement by the employer in **January** following the tax year. One source says 31 March (that is the individual return date) | (Law 206/2020 regime) | activpayroll (above) | L |
| `levy.martyrs_fund` | `{"rate": "0.0005", "base": "gross_monthly_salary", "borne_by": "employee", "withheld_by": "employer"}` (Law 4/2021 amending the Martyrs' and Victims' Fund Law 16/2018). Who bears it is disputed (§4) | 2021 (exact date not found) | https://taxsummaries.pwc.com/egypt/individual/other-taxes ; https://topsourceworldwide.com/global-payroll/egypt/ | M |
| `levy.uhi` | `{"employee": "0.01", "employer": "0.04", "employer_min_monthly": "50.00", "spouse_not_working": "0.03", "per_dependent_child": "0.01", "base": "si_contribution_wage"}`. **Only for employees in governorates where Universal Health Insurance (Law 2/2018) is live** (§2.5). Employer 4% (min EGP 50) is in Art. 40; the employee and family rates come from Table 1 and are unconfirmed | 2018-07 (law), per governorate go-live | https://manshurat.org/node/63712 ; https://www.expatfocus.com/guide/egypt-health-insurance | L |
| `payment.non_cash_required` | Private companies above the employee-count or payroll thresholds in PM Decree 1776/2020 must pay salaries by non-cash means (Law 18/2019). Fine 2% to 10% of the amount, up to EGP 1m. **Threshold not found** | 2021-09-08 (per one source) | https://www.pwc.com/m1/en/tax/documents/2019/egypt-usage-of-non-cash-payments-methods-law-no-18-year-2019.pdf ; https://eg.andersen.com/non-cash-payment-law-no-18-of-2019/ | L |

**Bracket exclusion table (Law 7/2024, Art. 8; reconstructed from secondary sources, confidence L to M).** Choose the column from annual net taxable income (after deducting the employee's SI share, the personal exemption and allowed insurance premiums; the exact order is §4 item 6):

| Slice of income | ≤ 600,000 | 600,001–700,000 | 700,001–800,000 | 800,001–900,000 | 900,001–1,200,000 | > 1,200,000 |
|---|---|---|---|---|---|---|
| 0–40,000 | 0% | 10% | 15% | 20% | 22.5% | 25% |
| 40,000–55,000 | 10% | 10% | 15% | 20% | 22.5% | 25% |
| 55,000–70,000 | 15% | 15% | 15% | 20% | 22.5% | 25% |
| 70,000–200,000 | 20% | 20% | 20% | 20% | 22.5% | 25% |
| 200,000–400,000 | 22.5% | 22.5% | 22.5% | 22.5% | 22.5% | 25% |
| 400,000–1,200,000 | 25% | 25% | 25% | 25% | 25% | 25% |
| above 1,200,000 | — | — | — | — | — | 27.5% |

Seed this as one JSON row `tax.salary.bracket_exclusion` (`[{"income_upto": "600000", "bands": [...]}, …]`) so a correction replaces the whole table. **Do not build on it until a local tax adviser confirms each cell.** Some sources (A-H-G) describe the exclusion with different steps.

## 2. Rules summary (for the spec-writer)

### 2.1 Labour Law 14/2025
- In force **2025-09-01**. It repeals Law 12/2003 (and Law 125/2010). Labour disputes under the new courts run from 2025-10-01. It was published in the Gazette on 2025-05-03 (Issue 18 cont.). Executive matters are expected by ministerial decree rather than a single regulation.
- **Contracts and probation:** probation at most 3 months, once per employer. Fixed-term contracts become indefinite on conditions the law sets (not researched in detail).
- **Hours:** 8 hours a day and 48 a week of actual work. At least 1 hour of breaks; no more than 5 continuous hours. Weekly rest of at least 24 continuous hours, paid.
- **Overtime:** day +35%, night +70%, rest day +100% plus a compensatory day. The Ministry is notified within 7 days after the fact. Overtime base = hourly wage (the base definition still needs confirming: §4).
- **Leave:** annual 15 days in the first year (after 6 months), 21 days from year 2, 30 days after 10 years or at age 50, 45 for disabled workers. Sick pay follows the Social Insurance Law: 75% then 85% of the insurable wage, up to 180 days a year, paid by NOSI under the SI law (who pays which days is §4). Maternity: 4 months, at least 45 days after birth, up to 3 times; paid by NOSI if insured.
- **Pay:** National minimum wage for the private sector **EGP 7,000** gross per month since 2025-03-01. The public sector moved to EGP 8,000 on 2026-07-01, but no decision for the private sector was found as of mid-September 2026. Annual periodic raise of at least **3% of the insurable wage**, due one year after hire or after the previous raise (Art. 12). The NWC decision also sets a floor of EGP 250. Payment by bank transfer is allowed. Payment frequency (at least monthly) was not confirmed in Law 14 (Law 12 Art. 38 said monthly).
- **Deductions:** loans or advances at most 10% of the wage, after income tax, no interest, written consent (Art. 113). All debts together at most 25%, or 50% for nafaqa; nafaqa ranks first. This interacts with payroll's existing "50% deduction cap" for bai' ajel (payroll R47 to R49, P19(k)): for Egypt employees the cap is 10% for employer loans and advances and 25% for all debts. **The Saudi caps must not be reused.**
- **Termination:** notice 3 months either side, regardless of service. Dismissal for misconduct needs a court judgment. Unjustified termination of an indefinite contract: at least 2 months' total wage per year of service. Fixed-term early termination: 1 month per year (one source). Employee-initiated exit after an economic change: 1 month per year for years 1 to 5, then 1.5 months per year.
- **No statutory EOSB gratuity.** Egypt has no Saudi-style end-of-service award for ordinary resignation or expiry. Retirement and lump sums come from NOSI. So the hr `calculate_eosb` path and the payroll account-2500 EOSB accrual should **not** run for `EG` by default. Compensation exists only when a termination event triggers it (a contingent liability, not a monthly accrual). Confirm with local counsel (§4 item 12).

### 2.2 Social insurance (Law 148/2019, in force 2020-01-01; Executive Regulations PM Decree 2437/2021)
- Monthly contributions are **11% employee + 18.75% employer** of the insurable wage, clamped to [min, max]:
  - 2025: EGP 2,300 to 14,500
  - 2026: EGP 2,700 to 16,700 (from 2026-01-01)
  - The limits rise about 15% each January through 2027. NOSI announces the next year's figures around late November.
- **Insurable wage** = total cash remuneration for the main job (one combined wage). Component inclusion is a per-component flag that needs local sign-off.
- **Foreigners** on local contracts are covered on the same terms (no reciprocity condition). Treaty exemptions are possible. A secondee from KSA who stays on GOSI is a question for the secondment design.
- **Payment:** due on the 1st of the following month per Art. 121 (press summaries); many guides say the 15th. Late payment adds a monthly additional amount of (T-bill/bond average yield + 2%).
- **Registration:** the establishment file uses Form 2; each employee uses Form 1 (with Form 111 medical). The registration deadline is 15 or 30 days (conflict). Termination notice to NOSI (Form 6 in practice) was not confirmed.
- Sickness, maternity and work-injury cash benefits come from NOSI (some injury costs from the Health Insurance Authority). Payroll may have to pay first and claim back, or stop pay. Process to confirm.

### 2.3 Salary tax (Law 91/2005 as amended by Laws 30/2023, 175/2023 and 7/2024; procedures under Law 206/2020)
- Taxable base = gross taxable salary minus the employee's NOSI share minus allowed insurance premiums (lower of 15% of net or EGP 10,000) minus the personal exemption of EGP 20,000. Tax = the bracket table, with the high-income exclusion (§1.3).
- **Monthly withholding (standard ETA practice; confirm):** annualise the month's taxable pay (×12), compute the annual tax, divide by 12, and true up cumulatively across the year. The traditional ETA rounding rule (round the annual taxable amount down to the nearest EGP 10) was **not** found in any source this time and needs confirming.
- Non-residents pay on Egyptian-source employment income at the same brackets. The 0% band may be limited to residents (PwC). Treaty relief is outside payroll scope.
- Remit by the 15th of the following month. File a quarterly Form 4 return electronically (within the month after each quarter end). Do the annual employer settlement in January.
- **Martyrs' and Victims' Fund:** 0.05% of gross monthly salary, withheld by the employer (Law 4/2021). It is a separate line and a separate remittance.
- **Universal Health Insurance (Law 2/2018):** employer 4% (min EGP 50 a month) and employee 1% of the contribution wage. The employee also pays 3% for a non-working spouse and 1% per dependent child. This applies **only in governorates where UHI has launched**:
  - Phase 1 (live): Port Said, Ismailia, Luxor, South Sinai, Suez, Aswan.
  - Phase 2 (starting with Minya in 2026): Minya, Matrouh, Damietta, Kafr El-Sheikh, North Sinai.
  - So this is a per-work-location parameter. The governorate-dependent split of the 18.75% employer rate (one table shows 25% employer in UHI governorates) must be resolved locally.
- **Stamp tax on salaries:** no current source confirms that it applies to private-sector salaries (Stamp Duty Law 111/1980; a 2019 proposal for a tiered salary stamp tax was not confirmed as enacted). Default: not modelled; open question.

### 2.4 Payslip, bank payment, year-end
- **Payslip:** no specific mandatory payslip format was found in Law 14/2025. Good practice and the ERP default: a bilingual (ar/en) payslip showing:
  - gross by component, overtime, deductions (SI 11%, salary tax, Martyrs' Fund, UHI if applicable, loans, nafaqa)
  - net pay, insurable wage used, and period.
- **Bank payment:** Law 18/2019 (non-cash payments) and PM Decree 1776/2020 oblige private companies above thresholds to pay salaries non-cash, with fines of 2% to 10% of the amount (up to EGP 1m). Thresholds unknown. Default: all Egypt salaries by bank transfer. Egypt has no WPS/Mudad equivalent: payroll R31/R51/R53 (WPS, Mudad) must be skipped for `EG`. The bank file format per Egyptian bank is still to come, like the KSA formats.
- **Year-end:**
  - January salary-tax settlement (employer).
  - SI wage limits change every 1 January; NOSI publishes them in late November.
  - The annual 3% raise runs on each employee's anniversary, not on 1 January.
  - The Q4 Form 4 is due in January.

### 2.5 Design implications for `docs/specs/hr.md` / `payroll.md` (for the spec-writer, not decided here)
1. `StatutoryParameter.jurisdiction` note "`SA` only for now" → add `EG`.
2. Some EG parameters vary below country level (UHI by governorate). This needs either a `region` qualifier or an employee/work-location flag `uhi_applies` with a dated governorate list parameter (`uhi.governorates_live`).
3. EG payroll lines needed: SI employee/employer (liability account, NOSI), salary tax withheld (ETA), Martyrs' Fund (ETA/Fund), UHI employee/employer (UHIA). Accounts need CFO approval (payroll P6, P19(g)).
4. Turn off for EG: GOSI, EOSB monthly accrual, WPS/Mudad, Saudi 50% deduction cap, Saudi leave ceilings (hr R56).
5. Annual 3% raise: an HR job that proposes a contract version on the anniversary (a draft, never auto-approved).
6. Tax: cumulative year-to-date withholding with the bracket-exclusion table; tests for each column boundary (600k, 700k, 800k, 900k, 1.2m).

## 3. Points that conflict between sources

| # | Point | Conflict |
|---|---|---|
| C1 | SI payment due date | 1st of the following month (Art. 121, press) vs 15th of the following month (payroll guides) |
| C2 | SI employer 18.75% branch split | The Lex Council breakdown sums to 17.75%; Ramco shows 10.75% in one place; aliateck shows 17% (non-UHI) or 25% (UHI governorates) for some categories. Only 12% + 9% old-age is consistent |
| C3 | Sick-leave pay | 75%/85% for 180 days (SI law) vs 100%/85%/75% over 12 months (some guides, industrial establishments) vs 75% for days 1–90; also who pays (employer or NOSI) |
| C4 | Annual leave first year | 15 days "in the first year" vs "after 6 months of service" |
| C5 | Workplace presence cap | 10 hours vs 12 hours |
| C6 | Tax exemption effective date | 2024-02-21 (Gazette date, PwC) vs March 2024 payroll month (Deloitte) |
| C7 | Bracket exclusion mechanics | A-H-G describes exclusion from 700k; Mercans/Toolbase from 600k |
| C8 | Martyrs' Fund bearer and base | Employee-borne on gross (PwC, Topsource) vs employer cost (Hivedesk, Atlas) vs basic + taxable allowances (ZenHR, old) |
| C9 | Annual settlement date | January (employer settlement) vs 31 March (individual returns) |
| C10 | Minimum wage | Press (Youm7, May 2026) says EGP 8,000 for public and private sectors from July 2026; Ministry of Labour (Elwatan) and Almashhad (Sept 2026) say private stays EGP 7,000 |
| C11 | Termination article numbers | Art. 165 / 156 vs Art. 122 (old numbering); dismissal procedure: court ruling vs tripartite committee |
| C12 | SI registration deadline | 15 vs 30 days |

## 4. Open questions for an Egyptian accountant / lawyer (blocking for EG verification)

1. **Minimum wage:** is the private-sector minimum still EGP 7,000 on the run date, or has the NWC adopted EGP 8,000? Does the "fewer than 10 workers" exemption apply to our entity?
2. **Annual raise:** is the EGP 250 floor still binding with the 3% in Law 14 Art. 12? What is "insurable wage" for the 3% when the wage is above the SI ceiling: is the 3% applied to the capped wage?
3. **SI branch split** of 18.75% / 11%, and whether the employer rate differs in UHI governorates (C2). We need the official NOSI table for the GL split.
4. **SI due date and late surcharge** (C1), and the NOSI e-filing channel the entity will use.
5. **Insurable wage components:** which allowances (housing, transport, bonuses, profit share) are included or excluded under Law 148 Art. 1 and the Executive Regulations.
6. **Salary tax computation:** the order of deductions (SI, insurance premiums, EGP 20,000 exemption) before the bracket column is chosen; the exact exclusion table (C7); the rounding rule; the YTD true-up method the ETA accepts; the tax treatment of the 13th month and bonuses.
7. **Tax filings:** the current ETA form set (Form 4 quarterly? monthly e-submission?) and the annual settlement date (C9).
8. **Martyrs' Fund:** who bears it, the base and the remittance channel (C8).
9. **UHI:** confirm the employee 1% and family rates (Table 1), the governorates live on each date, collection channel (NOSI or UHIA) and the employer minimum EGP 50.
10. **Stamp tax on salaries:** does any stamp tax apply to private-sector salaries today?
11. **Non-cash payment thresholds** under PM Decree 1776/2020; any CBE/bank salary-file format.
12. **Termination:** confirm no statutory EOSB for ordinary resignation or contract end. Confirm the compensation formulas (Art. 154, 165, economic change) and whether "total wage" includes allowances. Confirm the notice period for the employee resigning.
13. **Sick and maternity pay:** who pays during the leave (employer advance plus NOSI claim, or NOSI direct), and the employer's top-up obligation, if any (C3).
14. **Foreign employees:** SI for a Saudi or other GCC national seconded to Egypt (GOSI continuation vs NOSI). Work-permit fees as payroll cost (secondment R63 "fully loaded").
15. **Overtime base:** basic wage or total wage? Is rest-day pay +100% on top of the day's wage? What is the public-holiday work premium?
16. **Payment frequency:** does Law 14 keep "at least once a month" for monthly-paid staff? Is there a deadline for final dues after termination?
17. **Labour-law levies:** does Law 14/2025 add any employer payroll levy (training or emergency fund contribution)? None was found in this research.

## 5. Sources used (all via search-engine extracts, 2026-10-18)

- Law 14/2025: [EY alert 22 Jul 2025](https://taxnews.ey.com/news/2025-1555), [Deloitte](https://www.deloitte.com/middle-east/en/services/tax/perspectives/egypt-new-labor-law-entering-into-effect.html), [Andersen overview](https://eg.andersen.com/egypts-labour-law-14-2025/), [Andersen English translation PDF](https://eg.andersen.com/wp-content/uploads/2025/05/Translation-of-Labor-law-No.-14-of-2025.pdf), [Amereller](https://amereller.com/publication/egypt-new-labor-law-no-14-of-2025/), [Shand & Partners](https://www.shandpartners.com/insights/firm-news/new-labour-law-no-14-of-2025/), [Paul Hastings](https://www.paulhastings.com/insights/practice-area-articles/egypt), [Arabic text, Wikisource](https://ar.wikisource.org/wiki/%D9%82%D8%A7%D9%86%D9%88%D9%86_%D8%A7%D9%84%D8%B9%D9%85%D9%84_14_%D9%84%D8%B3%D9%86%D8%A9_2025_-_%D9%85%D8%B5%D8%B1), [Eastlaws Gazette text](https://www.eastlaws.com/legislation-full-text/ar/egypt/law/03-05-2025/no-14?type=1&id=2165366&sub=11404110), [Elwatan, deduction caps](https://www.elwatannews.com/news/details/8356604), [HNZ probation](https://hnzlaw.com/probation-period-under-labor-law-14-of-2025-what-employers-and-hr-professionals-need-to-know/), [HNZ fixed-term](https://hnzlaw.com/fixed-term-contract-rules-under-the-new-egyptian-labor-law-law-no-14-of-2025/), [Global Law Experts overtime](https://globallawexperts.com/how-to-calculate-overtime-in-egypt/), [Lexis ME hours](https://www.lexismiddleeast.com/news/2026-03-26_45/en), [ZenHR](https://www.zenhr.com/en/egypt-labor-law), [Dostor sick leave](https://www.dostor.org/5300125).
- Minimum wage: [Employsome](https://employsome.com/blog/minimum-wage-egypt/), [Elwatan (Ministry of Labour)](https://www.elwatannews.com/news/details/8314602), [Almashhad Sept 2026](https://www.almashhad.com/article/174515324408726-economy/906149631622523-%D8%AD%D8%AF-%D8%A3%D8%AF%D9%86%D9%89-%D9%84%D9%84%D8%A3%D8%AC%D9%88%D8%B1-%D9%81%D9%8A-%D9%85%D8%B5%D8%B1-%D9%82%D8%B1%D8%A7%D8%B1-%D8%AC%D8%AF%D9%8A%D8%AF-%D9%8A%D9%86%D8%B8%D9%85-%D8%A7%D9%84%D9%85%D8%B1%D8%A7%D8%AC%D8%B9%D8%A9-%D8%A7%D9%84%D8%AF%D9%88%D8%B1%D9%8A%D8%A9-%D9%84%D9%84%D8%B1%D9%88%D8%A7%D8%AA%D8%A8/), [Masrawy Aug 2026](https://www.masrawy.com/news/news_economy/details/2026/8/23/3037120/), [Youm7 May 2026](https://www.youm7.com/story/2026/5/4/%D8%B2%D9%8A%D8%A7%D8%AF%D8%A9-%D8%A7%D9%84%D8%AD%D8%AF-%D8%A7%D9%84%D8%A3%D8%AF%D9%86%D9%89-%D9%84%D9%84%D8%A3%D8%AC%D9%88%D8%B1-%D8%A5%D9%84%D9%89-8000-%D8%AC%D9%86%D9%8A%D9%87-%D8%A8%D8%AF%D8%A1%D8%A7-%D9%85%D9%86-%D9%8A%D9%88%D9%84%D9%8A%D9%88/7401367).
- Social insurance: [Business Today Egypt (NOSI 2026 limits)](https://www.businesstodayegypt.com/Article/1/6950/Egypt-s-Social-Insurance-Authority-announces-increase-in-wages-starting), [Alborsaa 2025-11-30](https://www.alborsaanews.com/2025/11/30/1932519), [BDO](https://www.bdo.global/en-gb/insights/tax/expatriate-tax/egypt-maximum-and-minimum-salaries-subject-to-social-insurance-increased), [Andersen](https://eg.andersen.com/social-insurance-in-egypt/), [Mondaq/Andersen Jul 2026](https://www.mondaq.com/employee-rights-labour-relations/1810754/social-insurance-in-egypt), [Legal500](https://www.legal500.com/intelligence/egypt/employment-and-hr/social-insurance-in-egypt), [Lockton (foreigners)](https://global.lockton.com/us/en/news-insights/egypt-requires-foreign-employees-to-participate-in-the-national-social-security-scheme), [Lexology](https://www.lexology.com/library/detail.aspx?g=fb071754-b133-4ccb-8b41-7e4ddb7631af), [Egyls Art. 121](https://egyls.com/%D8%AD%D8%A7%D9%84-%D8%AA%D8%A3%D8%AE%D8%B1-%D8%A7%D9%84%D8%A7%D8%B4%D8%AA%D8%B1%D8%A7%D9%83%D8%A7%D8%AA-%D9%82%D8%A7%D9%86%D9%88%D9%86-%D8%A7%D9%84%D9%85%D8%B9%D8%A7%D8%B4%D8%A7%D8%AA-%D9%8A%D9%84/), [Lex Council calculator](https://lexcouncil.com/en/tools/social-insurance-calculator), [Andersen Law 148 translation PDF](https://eg.andersen.com/wp-content/uploads/2025/06/Law-No.-148-of-the-Year-2019-and-its-executive-regulation.pdf).
- Salary tax: [PwC personal income](https://taxsummaries.pwc.com/egypt/individual/taxes-on-personal-income), [PwC deductions](https://taxsummaries.pwc.com/egypt/individual/deductions), [PwC other taxes](https://taxsummaries.pwc.com/egypt/individual/other-taxes), [Deloitte Law 7/2024](https://www.deloitte.com/middle-east/en/services/tax/perspectives/amendment-to-tax-brackets-and-annual-personal-exemption.html), [Andersen 2024 changes](https://eg.andersen.com/income-tax/), [A-H-G bracket table](https://www.a-h-g.net/ar/income-tax-brackets-table/), [Mercans](https://mercans.com/resources/statutory-alerts/egypt-income-tax-law-amendment/), [Dostor Gazette note](https://www.dostor.org/4641846), [PwC Law 206/2020](https://www.pwc.com/m1/en/tax/documents/2020/egyptian-government-issues-unified-tax-procedures-law-no-206-2020.pdf), [activpayroll](https://www.activpayroll.com/global-insights/egypt), [KPMG TIES 2022](https://assets.kpmg.com/content/dam/kpmgsites/xx/pdf/2023/01/TIES-Egypt.pdf.coredownload.inline.pdf), [Topsource](https://topsourceworldwide.com/global-payroll/egypt/).
- UHI: [Law 2/2018 text, Manshurat](https://manshurat.org/node/63712), [Expat Focus](https://www.expatfocus.com/guide/egypt-health-insurance), [Mada Masr Jul 2026](https://www.madamasr.com/en/2026/07/30/feature/politics/cracks-in-the-universal-health-insurance-system-an-accounting-of-phase-1/), [Zawya Minya phase 2](https://www.zawya.com/en/business/healthcare/egypt-to-begin-second-phase-of-universal-health-insurance-in-minya-kibknl8y).
- Non-cash payment: [PwC Law 18/2019](https://www.pwc.com/m1/en/tax/documents/2019/egypt-usage-of-non-cash-payments-methods-law-no-18-year-2019.pdf), [Andersen](https://eg.andersen.com/non-cash-payment-law-no-18-of-2019/).
