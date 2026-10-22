# Compliance check: draft spec `docs/specs/fixed-assets.md` (fixed assets)

Checked on 2026-10-22 by the compliance-checker agent, which is read-only. No files were edited.
Inputs read: `/home/user/almiras-erp/docs/specs/fixed-assets.md`, `/home/user/almiras-erp/CLAUDE.md`, `/home/user/almiras-erp/docs/specs/zatca.md` (R-30, R-38, OQ-31), `/home/user/almiras-erp/docs/compliance/zatca-verification-2026-10-16.md` (Z38, Z39), `/home/user/almiras-erp/docs/specs/invoicing.md` (R29), `/home/user/almiras-erp/docs/specs/tax-returns.md`, `/home/user/almiras-erp/ledger/coa_template.py`.

## How far this was verified

I could not open any primary document directly in this session. WebFetch failed with `getaddrinfo ENOTFOUND` for every host I tried: zatca.gov.sa, ifrs.org, cdn.ifrs.org, iasplus.com, cfrr.worldbank.org and gov.uk.

Every verdict below rests on web-search extracts. Most come from the regulators' own documents (ZATCA PDFs, IFRS Foundation modules and staff papers), all searched on 2026-10-22, and some from Big-4 or other secondary sources. Each source line says which kind it is. Before sign-off, someone should open the listed PDFs and confirm the article and paragraph numbers.

None of this is a legal, tax, audit or Sharia opinion. Items marked **Needs expert sign-off** need a licensed tax adviser, the auditor or the Sharia board.

---

## Part A: the [CC-verify] items

| # | Spec location | Statement | Verdict | Source (searched 2026-10-22) | Correction to make |
|---|---|---|---|---|---|
| A1 | R7 | Under full IFRS, IAS 23 requires borrowing costs on qualifying assets to be capitalised | **Correct** | IAS 23.8. Confirmed by summaries (ICAEW IAS 23 tracker https://www.icaew.com/technical/corporate-reporting/ifrs/ifrs-accounting-standards-tracker/ias-23-borrowing-costs; BDO https://www.bdo.com.au/en-au/content/ifrstopics/borrowing-costs). Primary IFRS text not opened. | Replace the tag with "(IAS 23.8)". Keep OQ-9. See B1 and S11 for the consequences. |
| A2 | R7 | IFRS for SMEs Section 25 expenses all borrowing costs | **Correct**, for both the 2015 and 2025 editions | IFRS for SMEs 25.2. IASB staff paper AP30D, October 2023, "Recognition of borrowing costs": the Board voted 10 of 13 to keep expensing (https://www.ifrs.org/content/dam/ifrs/meetings/2023/october/iasb/ap30d-recognition-of-borrowing-costs.pdf). The final 2025 text of s25 was not opened. | Replace the tag with "(IFRS for SMEs 25.2, unchanged in the 3rd edition)". |
| A3 | R16 | Changes of estimate are prospective (IAS 8 / s10) | **Partly correct** | IAS 8.36–38; IFRS for SMEs 10.16–10.17; 17.19 and 17.23 say a change in residual value, useful life or method is a change in estimate. These paragraph numbers are from memory; the primary text was not opened. | "Prospective" is right. "From the first day of an open month" is a house convention: the standards apply the change from the period in which it is made. Reword: "Changes of estimate are prospective (IAS 8.36; IFRS for SMEs 10.16, 17.23). House convention: effective from the first day of the earliest open month on or after the approval date." Also add: "Full-IFRS entities: review residual value, useful life and method at least at each financial year-end (IAS 16.51), with a year-end reminder. IFRS for SMEs: review when indicators exist (s17.19)." |
| A4 | R19 | IAS 36 / s27 impairment, with reversal capped at the carrying amount there would have been without the impairment | **Correct** on the mechanics and the cap. **Partly correct** on review frequency. | IAS 36.117 (cap) and 36.114 (when a reversal is allowed), from the AASB copy of the standard (https://standards.aasb.gov.au/node/924). s27.30 was **not verified** (no search hit). Frequency: IAS 36.9 and s27.7 require an indicator assessment **at each reporting date**; paragraph numbers are from memory. | Replace "Annual indicator review: reminder only" with "Indicator assessment at each reporting date (IAS 36.9; s27.7); the system raises a reminder at each period-end the entity designates as a reporting date (OQ-7)." Keep the cap wording. Confirm the s27.30 number against the 2025 edition. |
| A5 | R20 | IFRS for SMEs s16 requires fair value through profit or loss when fair value can be measured reliably without undue cost or effort; otherwise the cost model | **Correct** | IFRS Foundation supporting material, Module 16 (2015 edition), paragraphs 16.7–16.8 (https://www.ifrs.org/content/dam/ifrs/supporting-implementation/smes/module-16.pdf). Not checked against the 2025 edition text. | Remove the tag. Add: "s16 is **not** a policy choice. An entity using the cost model for investment property must be able to show undue cost or effort, property by property, and disclose that fact and its reasons." See S8. Today's "cost model for every entity" default may be a departure for IFRS for SMEs entities that hold IP with an available market value. **Needs expert sign-off: auditor.** |
| A6 | R20 | Under full IFRS, IAS 40 is a policy choice | **Correct** | IAS 40.30 (choice), 40.32A, 40.31. Secondary summary https://www.iasplus.com/standard/ias40.htm. IFRS text not opened. | Remove the tag. Add: "Under the cost model, IAS 40.79(e) still requires the fair value of investment property to be disclosed." The register needs a field for this (S8). |
| A7 | R21, OQ-8 | "IFRS for SMEs s17.15B *may also* allow revaluation" | **Correct, but worded too weakly.** s17.15 offers cost (17.15A) or revaluation (17.15B) **per class** of PPE. This option came with the 2015 amendments, effective 1 January 2017. | IFRS Foundation Module 17 (https://www.ifrs.org/content/dam/ifrs/supporting-implementation/smes/module-17.pdf); 2015 Project Summary and Feedback Statement (https://www.ifrs.org/content/dam/ifrs/project/2015-comprehensive-review-of-ifrs-for-smes/amendments/published-documents/ifrs-for-smes-project-summary-feedback-statement.pdf) | Replace with: "IFRS for SMEs s17.15–17.15B and IAS 16.29 both allow the revaluation model per class of PPE. Not built until OQ-8." Check the paragraph numbers against the 2025 edition. On local restrictions: the CMA cost-model requirement covered **listed** companies only, for fiscal years starting before 2022. I could not find SOCPA's own endorsement text for SMEs (https://www.ifrs.org/use-around-the-world/use-of-ifrs-standards-by-jurisdiction/view-jurisdiction/saudi-arabia/). **Needs expert sign-off: auditor.** |
| A8 | R22 | Sale of a used movable asset is standard-rated | **Correct**: 15% on the full consideration | VAT at 15% from 1 July 2020. The used-car profit-margin method is only for licensed car traders with ZATCA approval, applies to qualified used cars, and is optional (ZATCA e-service 090, https://zatca.gov.sa/en/eServices/Pages/eServices-090.aspx; PwC 2023 alert https://www.pwc.com/m1/en/tax/documents/2023/saudi-arabia-eligible-used-goods-and-the-profit-margin-method-effective-date-and-legal-requirements.pdf). | Replace the tag with: "Standard rate 15% on the full price. The profit-margin method is not available, since Almiras entities are not licensed car traders with ZATCA approval." |
| A9 | R22, OQ-10 | Real estate: VAT exempt, with Real Estate Transaction Tax (RETT) instead | **Correct** | Royal Order A/84 of 14/02/1442H: sales of real estate are VAT-exempt, and RETT of 5% applies to the value. Commercial leases stay at 15%; residential leases are exempt (ZATCA guideline "VAT Exemption of Real Estate Supplies", https://zatca.gov.sa/en/MediaCenter/Publications/Documents/Exemption%20of%20Real%20Estate%20Supplies%20-%20English.pdf). The new RETT Law was published 11 October 2024, and its new Implementing Regulations apply from 10 April 2025 (https://zatca.gov.sa/en/MediaCenter/News/Pages/RETT-Implementing-Regulations.aspx; RETT detailed guideline v6, https://zatca.gov.sa/en/HelpCenter/guidelines/Documents/Detailed-Guideline-for-RETT-In-accordance-with-provision-of-RETT-Law-and-its-Implementing-Regulations.pdf). | Replace with: "Real estate (land, buildings, IP): VAT exempt (Royal Order A/84). RETT at 5% of the value is due under the RETT Law (2024) and its Implementing Regulations (in force 10 April 2025). RETT is not handled by this module; see B4." **Needs expert sign-off: tax.** |
| A10 | R27 | Lessor operating lease: the asset stays in the register and is depreciated (s20 / IFRS 16) | **Likely correct, but not verified this session** | From memory: IFRS 16.88 (lessor applies IAS 16) and s20.29 (2015 numbering). No search hit was obtained. | Keep the tag with the note "verify s20 paragraph in the 2025 edition". |
| A11 | FA1 | Input VAT restrictions on passenger cars | **Correct that a restriction exists** | VAT Implementing Regulations Art. 50, as described in ZATCA's Input Tax Deduction guideline (July 2020, https://zatca.gov.sa/en/HelpCenter/guidelines/Documents/Input%20Tax%20Deduction.pdf). Input VAT on a "Restricted Motor Vehicle" is not deductible, and neither is VAT on its repair, maintenance or fuel. Exceptions: (a) the vehicle is used exclusively for business, with no availability for private use; (b) it is held for resale or used in an economic activity that supplies the vehicle, such as rental. There is no apportionment. | Replace the tag with: "(Art. 50: input VAT is blocked on vehicles available for private use; recoverable for vehicles let on rental contracts or used exclusively for business)." Invoicing R29 needs a `blocked` attribution (S3). Whether a given rental fleet qualifies under exception (b) is **Needs expert sign-off: tax.** |
| A12 | FA3, FA7 | VAT at 15% | **Correct** | Standard rate 15% since 1 July 2020 (ZATCA). | Remove the tag. |
| A13 | R34, OQ-15 | Zakat treatment of fixed assets, IP and land held for trade | **Correct that it is deferred; the rule itself is only partly verified.** | Zakat Implementing Regulations 1445H (2024), Ministerial Resolution 1007, for years starting on or after 1 January 2024 (https://zatca.gov.sa/en/RulesRegulations/Documents/ZAKAT%20COLLECTION.pdf). Search extracts say: non-current assets are deducted **only if acquired for use, not for resale**, at **net value in the financial statements**. ZATCA's Zakat General Simplified Guideline lists net fixed assets, capital work in progress and intangibles as deductions (https://zatca.gov.sa/en/HelpCenter/guidelines/Documents/Zakat%20General%20Simplified%20Guideline.pdf). ZATCA's disputed-items guidance requires proof that an asset is used in the activity. The article numbers could not be read. | Keep "never decides". Add to R34: "`zakat_inputs` also returns, per asset, `held_for` (own use, rental, capital appreciation, trade) and evidence of use (S10). Book NBV is the basis (Zakat IR 2024, 'net value in the financial statements')." **Needs expert sign-off: tax.** |
| A14 | R34, OQ-15 | Income Tax Law Art. 17 tax depreciation groups | **Correct as a reference** | Income Tax Law (Royal Decree M/1, 15/1/1425H) Art. 17. Groups: fixed buildings 5%; movable industrial and agricultural buildings 10%; factories, machinery, computers, software and passenger and cargo vehicles 25%; geological and exploration costs 20%; all other assets 10%. Pooled declining balance on each group's remaining base. Sources: WTO copy of the law (https://www.wto.org/english/thewto_e/acc_e/sau_e/wtaccsau59a2_leg_8.pdf); PwC tax summaries (https://taxsummaries.pwc.com/saudi-arabia/corporate/deductions). The October 2023 draft new Income Tax Law would move to straight line; I could **not confirm** whether it has been enacted (EY: https://taxnews.ey.com/news/2023-1877-saudi-arabia-issues-drafts-of-new-income-tax-law-and-zakat-and-tax-procedures-law-for-public-consultation). | Reword: "Tax depreciation (Income Tax Law Art. 17: pooled declining balance by group; status of the 2023 draft law to be checked) is not built (OQ-15). If built, it is per **group pool**, not per asset." **Needs expert sign-off: tax.** |
| A15 | OQ-6 | Investment property valuer and valuation frequency | **Cannot verify** | Under s16.7 fair value is measured at each reporting date. Valuer accreditation in KSA (Taqeem, the Saudi Authority for Accredited Valuers) was not searched. | Keep the tag and add **Needs expert sign-off: auditor**. |
| A16 | OQ-10 | Art. 52 capital-asset adjustment when use changes | **Correct that the obligation exists; the period length is only partly verified.** | VAT IR Art. 52: an annual adjustment applies when taxable use changes from the initial recovery percentage (ZATCA Input Tax Deduction guideline 2020, §5). Adjustment period from secondary sources only: 6 years for movable assets, or the accounting life if shorter; 10 years for real estate (Crowe and APMH, https://apmh.in/insights/saudi-arabia-vat-guide; an extract of the ZATCA Capital Assets Guideline v1 gives 10 years for immovable assets). I found **no** Saudi value threshold; the AED 5m figure is UAE law. Example: a building moving from commercial lease (15%) to residential lease (exempt) triggers an adjustment. | Replace with: "Art. 52 adjustment applies to capital assets within the adjustment period (secondary sources: 6 years movable or shorter accounting life; 10 years real estate) when the taxable-use percentage changes, e.g. IP moving from commercial to residential lease." See S4. **Needs expert sign-off: tax.** |

Two untagged statements were also checked.

**D8 / R33 (retention).** This is **partly correct**. The ZATCA Guideline for Tax Invoicing and Records (TIR §8.3.3, https://zatca.gov.sa/en/HelpCenter/guidelines/Documents/Guideline-for-Tax-Invoicing-and-Records-under-VAT-Provisions.pdf) says movable capital assets are kept for the Art. 52 adjustment period plus 5 years from acquisition. **Where the asset's accounting useful life is under six years, the period is the useful life plus 5 years.** Real estate is "not less than 15 years in all cases". An older ZATCA Agents Guideline gives 11 years and 15 years, which is consistent with 6 + 5. The spec is missing the useful-life rule (fix in S1).

**Framework edition.** The IFRS for SMEs third edition is effective for periods beginning on or after 1 January 2027, which is Almiras' first year on the new system. I could not confirm whether SOCPA has endorsed the third edition or set its own date (S12).

---

## Part B: other findings

### Blocking

**B1. R7 and AT-17 would block legitimate Sharia-compliant asset financing.**
R7 refuses "a journal crediting an asset cost account against a financing account (2400, …)". AT-17 refuses "Dr 1570 / Cr 2410".
In `coa_template.py`, 2400 is "Islamic financing (murabaha, tawarruq)", a liability that holds principal. A murabaha purchase of an asset is normally booked as Dr asset (cash cost price) / Cr 2400. Under R7 as written, that entry is refused.
The Sharia rule targets the financing **cost or profit**, not the principal.
Proposed wording for R7:
> "Sharia, every entity: no financing cost is capitalised. A cost component, or a journal debiting an asset cost account, is refused when its source line is on a financing-cost account (5600, 5610, any `prohibited` account) or is the deferred-profit portion of an Islamic financing (murabaha/tawarruq/ijara deferred profit). Crediting 2400 for the **cash cost price** of an asset bought on murabaha is allowed. Conventional loans (2410) are already blocked where Sharia is enforced (ledger). AT-17: refusing Dr 1570 / Cr 5600 or 5610 in ALM and NSH; allowing Dr 1570 (cash price) / Cr 2400 with the deferred profit posted to the financing liability's contra and expensed to 5600 over the term."
How murabaha deferred profit is split between cost and expense is **Needs expert sign-off: Sharia board and auditor** (fold into OQ-9).

**B4. Disposals of real estate (and intercompany transfers of real estate) have no safe path.**
R22 and R24 leave the VAT category to staff, and RETT is "not built". Charging 15% VAT on a real-estate sale, or missing RETT, is a direct tax error (A9).
Proposed new rule R22a:
> "A `disposal_sale` or `intercompany_sale` event for an asset whose `retention_class` is `capital_asset_real_estate` is refused (en/ar, citing OQ-10) until OQ-10 is signed off. Once enabled: the linked sales document must use VAT category E (real estate exemption) with the matching exemption reason; the event cannot post until a RETT record (amount = 5% of the value, or a documented exemption article) and its payment evidence are attached; RETT borne by the seller posts to a proposed RETT expense account within the disposal result."
Add an AT for this. **Needs expert sign-off: tax** (RETT exemptions under the 2025 regulations; see S9).

### Should fix

**S1. R33 / D8: movable-asset retention formula (TIR §8.3.3).**
Replace part (a) of R33 for movable assets with:
> "movable: `acquisition_date` + min(Art. 52 adjustment period setting, accounting useful life in months) + 5 years, never earlier than the standard date. The setting defaults to 6 years (secondary sources; **Needs expert sign-off: tax**, zatca OQ-31)."
Note in AT-10 that 5 years is a fixture value. Because records are kept indefinitely (D-16(e)), this only changes the nomination date. Make the same correction in zatca R-38.

**S2. §3.1 retention class by asset class.**
`leasehold_improvement`, and `cwip` that will become a building, are improvements attached to real estate. The extract of the ZATCA Capital Assets Guideline treats assets "permanently attached to land or real estate" as immovable (10-year adjustment). The spec defaults them to movable.
Proposed wording:
> "`land`, `building`, `ip_*`, `leasehold_improvement` → `capital_asset_real_estate`; `cwip` → `capital_asset_real_estate` by default (conservative), re-derived from the target category at `capitalise_cwip` but never shortened; others movable."
**Needs expert sign-off: tax.**

**S3. FA1 and invoicing R29: blocked input VAT on vehicles (Art. 50).**
Add a `blocked` value to `vat_attribution` in invoicing. Its VAT is added to the asset's cost component.
Add an `FixedAsset.vat_recovery_pct` (initial recovery %) snapshot taken from the bill.
Proposed text for R2:
> "Vehicle cost lines default to attribution `blocked` unless the category is a rental-fleet category or the bill line is marked 'exclusive business use, no private availability' with evidence."
**Needs expert sign-off: tax.**

**S4. Art. 52 support data.**
Accepting the "no Art. 52 adjustment computed" default is only safe if the data needed later is captured now. Add to `FixedAsset`: `vat_use` (`taxable`, `exempt`, `mixed`), `initial_recovery_pct`, `input_vat_amount` (from the bill) and `adjustment_period_end`.
Add a report: "Capital assets inside their adjustment period whose `vat_use` or `usage` changed during the year", for example IP moving from commercial to residential lease, or a rental vehicle moving to private use.
Proposed wording:
> "R36: Changes of `vat_use` are recorded by an `AssetEvent(kind=vat_use_change)` (no GL entry); the year-end Art. 52 report lists affected assets for the tax adviser."
**Needs expert sign-off: tax.**

**S5. Account 1510 naming.**
In the template, 1510 is called "Investment property (rental assets)". That invites rental **vehicles** to be booked there, which would wrongly classify them as IP and wrongly give them 15-year real-estate retention.
Proposed wording:
> "1510 is restricted to `ip_land` / `ip_building` categories; rename to 'Investment property (land and buildings)' / 'العقارات الاستثمارية (أراضٍ ومبانٍ)' (CFO, D7)."

**S6. R16: annual review of estimates.**
Add the year-end review reminder from A3: IAS 16.51 for full-IFRS entities, and indicator-based review under s17.19.

**S7. R19: indicator assessment at each reporting date (A4).**

**S8. R20: investment property data.**
Add `IpFairValue` records (entity, asset, date, amount, valuer, evidence) for:
- IAS 40.79(e) disclosure under the cost model;
- the s16 undue-cost-or-effort justification, per entity and per property, needed if the cost-model default is kept for IFRS for SMEs entities.
Upgrade OQ-6 to **BLOCKING for the first annual financial statements of any IFRS for SMEs entity holding IP**. **Needs expert sign-off: auditor.**

**S9. R24 / OQ-11: intercompany transfers.**
Add to OQ-11:
- (a) VAT value between related persons: the VAT Law allows fair-market-value substitution. This matters where the buyer cannot fully recover VAT, for example residential IP. The article number was not verified.
- (b) Real estate: secondary sources describe a RETT exemption for 100% intra-group transfers (Art. 3(18) of the 2025 RETT Implementing Regulations) with a 5-year lock-up and retroactive clawback (https://www.pwc.com/m1/en/tax/documents/2025/saudi-arabia-rett-implementing-regulations-april-2025.pdf). Not verified against the regulation text.
- (c) Transfer pricing and Zakat view of the price.
Add an AT for an intercompany sale of real estate, refused until OQ-10.
**Needs expert sign-off: tax.**

**S10. R34: Zakat "acquired for use, not resale" (A13).**
Add `FixedAsset.held_for` (`own_use`, `rental`, `capital_appreciation`, `trade`), with evidence of use available to `zakat_inputs`. **Needs expert sign-off: tax.**

**S11. R7 scope across entities.**
R7 applies the "no financing cost capitalised" rule to every entity, "whatever `sharia_enforced`". CLAUDE.md rule 5 says Sharia is enforced **per entity**, and §2 records no owner decision extending it.
For a full-IFRS entity with `sharia_enforced=False` and conventional borrowing for a qualifying asset, this is a departure from IAS 23.8. For IFRS for SMEs entities there is no conflict, since s25 requires expensing (A2).
Proposed wording:
> "Add D10: the group rule 'no financing cost is capitalised in any entity' (project owner, date). Until recorded, R7 applies only where `sharia_enforced=True` and to every IFRS for SMEs entity; full-IFRS non-Sharia entities: OQ-9."
**Needs expert sign-off: owner, auditor and Sharia board.**

**S12. Framework edition.**
Add to §2 or OQ-6:
> "Paragraph references are to IFRS for SMEs (2025, 3rd edition, effective 1 January 2027) and IFRS as endorsed by SOCPA. Which edition each entity applies in FY2027, and any SOCPA modifications, is confirmed by the auditor (reporting.md `framework_edition`)."
Re-check 17.15B, 27.30 and the s20 paragraph numbers against the 2025 text. **Needs expert sign-off: auditor.**

**S13. Deferred-payment purchases and disposals (gap).**
When payment is deferred beyond normal credit terms, s17.13 and IAS 16.23 measure cost at the cash price equivalent and treat the difference as a finance cost. Likewise, a significant financing component on a deferred-payment sale (s23 / IFRS 15) is booked as finance income. R4 instead takes the full bill amount as cost.
In a Sharia entity, this imputed amount must never post to an interest account.
Proposed wording:
> "R4a: A bill line whose payment terms exceed the entity's normal credit terms (setting) is flagged; the builder does not split it. Classification of any imputed financing component (cost vs 5600 deferred-sale profit) is OQ-20 (new). Deferred-payment disposals: same flag on the sales document."
**Needs expert sign-off: auditor and Sharia board.**

### Notes

- **N1. Data location.** The register and events are VAT records for capital assets (Art. 52, TIR §8.3.3). They must be hosted in KSA (TIR §8.3.1; zatca R-43, Z39). Add one line to §6.
- **N2. Used cars.** State explicitly in R22 that the profit-margin method is not used (A8), so staff cannot pick it.
- **N3.** Whether occasional sales of capital assets are left out of the Art. 51 recovery ratio (invoicing R104) was not verified. Add to OQ-10. **Needs expert sign-off: tax.**
- **N4. Sharia.** Rent from IP let to tenants whose activity is non-compliant should be flagged for purification (4900 → 2600). That belongs to billing, not this module. Cross-reference it in §6.
- **N5.** Decommissioning provisions (IAS 16.16(c), s17.10(c)) are out of scope. If they are added later, the unwinding of the discount is "interest-like" and needs a Sharia classification.
- **N6.** If OQ-15 is ever built, Art. 17 works on group pools. A cap on deductible repair costs is also believed to apply; this was not verified.
- **N7.** The start point for real-estate retention remains an assumption (zatca R-38). There is no purge job, which is consistent with D-16(e).
- **N8.** Gains and losses on 4800 must map outside revenue in reporting.md (IAS 16.68).
- **N9.** For real-estate disposals, confirm the exemption reason code with `/home/user/almiras-erp/docs/specs/zatca.md` and the ZATCA XML rules before B4 is enabled.

## Sign-off summary

- **Tax adviser:** A9, A11, A13, A14, A16, S1–S4, S9, S10, N3, B4.
- **Auditor:** A5, A7, A15, S8, S11, S12, S13.
- **Sharia board:** B1 (murabaha deferred profit), S11, S13, and the existing OQ-9 and OQ-12.
- **Re-verification:** every source above was read through search extracts, not by opening the document. Before the spec is approved, someone should open the ZATCA VAT Implementing Regulations PDF (https://zatca.gov.sa/en/RulesRegulations/Taxes/Documents/Implmenting%20Regulations%20of%20the%20VAT%20Law_EN.pdf), Art. 50, 52 and 66, plus the Zakat IR 2024 PDF and the IFRS for SMEs 2025 text, and confirm the article and paragraph numbers.
