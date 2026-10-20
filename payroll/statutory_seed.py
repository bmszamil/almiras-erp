"""Payroll's ``hr.StatutoryParameter`` rows (hr 3.9; payroll R15 to R22, R31, R51 to R53, R64 to R68; hr R68).

Every row is seeded ``unverified`` with ``verified_by`` empty: the loader never verifies (hr R7); a human holder of
``hr.verify_statutory_parameter`` (the MP, never ``entered_by``) verifies row by row. Sources:

- Labour Law / WPS rows: docs/compliance/hr-payroll-labour-law-2026-10-17.md (HRSD texts downloaded 2026-10-17).
- GOSI rows: docs/compliance/gosi-check-2026-10-17.md, gosi-check-legacy-2026-10-17.md and the Implementing
  Regulations check gosi-regulations-2026-10-19.md (official Arabic texts from uqn.gov.sa, read 2026-10-19).
  [CC-verify] tags name what is still open.
- Egypt rows: docs/compliance/egypt-payroll-2026-10-18.md (search-engine extracts only; nothing read from the
  Gazette). Seeded so the jurisdiction exists as data; Egypt payroll is not calculated in slice 1 (R4).

Values are Decimal strings (no floats). The loader is :func:`hr.statutory_seed.apply_rows` (create missing rows;
refresh only pristine loader rows).
"""

LABOUR_LAW = "https://www.hrsd.gov.sa/en/policies/labor-law"
REGULATIONS = "https://www.hrsd.gov.sa/en/policies/implementing-regulations-labor-law"
WPS_GUIDE = "https://www.hrsd.gov.sa/en/knowledge-centre/wage-protection-system"
SIL = "https://www.uqn.gov.sa/details?p=25239"
SIL_RD = "https://www.uqn.gov.sa/details?p=25238"
SIL_COM = "https://www.uqn.gov.sa/details?p=25237"
SIL_REGS = "https://www.uqn.gov.sa/details?p=25240"
GOSI_BOOKLET = "https://cmsgosi.gosi.gov.sa/SystemsAndRegulations/Documents/SocialInsuranceSystem1421.pdf"
SANED_BOOKLET = "https://cmsgosi.gosi.gov.sa/SystemsAndRegulations/Documents/UnemploymentInsuranceSystem2.pdf"
GOSI_FORM7 = "https://cmsgosi.gosi.gov.sa/SystemsAndRegulations/RequiredDocuments/Documents/form7.pdf"
OWNER = "https://www.almiras.example/decisions/owner-cfo-decision-pack-2026-10-17"

LL_FROM = "2000-01-01"
GOSI_FROM = "2024-07-03"
CHECKED = " | Checked 2026-10-17 against the HRSD text; MP verification pending (hr R7)"
REGS_READ = " | Regs read 2026-10-19 (docs/compliance/gosi-regulations-2026-10-19.md); MP verification pending (hr R7)"

# code: (value, source_url, source_reference), valid from LL_FROM.
SA_PAYROLL = {
    "payroll.month_days": ({"days": "30"}, LABOUR_LAW, "Labour Law Art. 2" + CHECKED),
    "overtime.premium_pct_of_basic": ({"pct": "50"}, LABOUR_LAW,
                                      "Labour Law Art. 107; unified contract cl. 11.4" + CHECKED),
    "overtime.max_hours_year": ({"hours": "720"}, REGULATIONS, "Implementing Regulations Art. 22" + CHECKED),
    "overtime.exceptional_max": ({"daily": "10", "weekly": "60"}, LABOUR_LAW, "Labour Law Art. 106" + CHECKED),
    "overtime.art106_inventory_max_days": ({"days": "30"}, LABOUR_LAW,
                                           "Labour Law Art. 106(1): inventory, balance sheets, closing and seasonal "
                                           "preparation, not more than 30 days a year (compliance C-A7, 2026-10-19); MP "
                                           "verification pending (hr R7)"),
    "overtime.hour_divisor_rule": ({"rule": "day_basis_days_x_daily_hours"}, LABOUR_LAW,
                                   "[CC-verify: hour divisor not stated in HRSD sources]"),
    "deduction.caps": ({"loan_max_pct": "10", "total_max_pct": "50", "court_max_fraction": "1/4",
                        "damage_max_days_month": "5"}, LABOUR_LAW,
                       "Labour Law Art. 91, 92(1), 92(6), 93; bases [Needs expert sign-off: legal] (OQ-34)" + CHECKED),
    "fines.max_days": ({"per_violation": "5", "per_month": "5"}, LABOUR_LAW, "Labour Law Art. 66, 70" + CHECKED),
    "deferred_sale.apply_loan_cap": ({"apply": False}, OWNER, "Owner decision P19(k): written consent, within the 50% "
                                     "cap; no 10% loan cap (payroll R49)"),
    "wps.fields.mol": ({
        "header": ["DEST-ID", "ESTB-ID", "BANK-ACC", "32A-CCY", "32A-VAL", "32A-AMT", "D-CATE", "FILE-REF",
                   "FILE-REJCDE", "MOL-ESTBID"],
        "content": ["32B-AMT", "ACC-59", "NAM-57", "BANK-57", "DET-70", "RET-CDE", "MOL-BAS", "MOL-HALI", "MOL-OEA",
                    "MOL-ALL", "MOL-ID", "TRN-REF", "TRN-STATUS", "TRN-DATE"],
        "bank_filled": ["FILE-REJCDE", "RET-CDE", "TRN-REF", "TRN-STATUS", "TRN-DATE"],
    }, WPS_GUIDE, "HRSD WPS user guide (Ministry header and content fields); deductions field not mapped "
       "[CC-verify: deductions field to confirm with each bank]" + CHECKED),
    "mudad.notice_days": ({"erp_alert_days": ["7", "10", "15"], "inspection_day": "20", "justification_days": "10",
                           "worker_response_days": "3"}, WPS_GUIDE, "HRSD WPS user guide (Mudad e-mails day 10 and 15, "
                          "inspection day 20); ERP day-7 alert is a default" + CHECKED),
}

# Annuities (new_m273): RD M/273 Clause Third(1), (2); steps from the month after 12/24/36/48 months. Default
# reading of the step month: August (payroll R21(b)); gosi.annuities_step_start stays unverified.
_LEGACY = {"employer": "9", "employee": "9"}
ANNUITY_STEPS = (
    ("2024-07-03", "9"), ("2025-08-01", "9.5"), ("2026-08-01", "10"), ("2027-08-01", "10.5"), ("2028-08-01", "11"),
)
ANNUITY_REF = ("RD M/273 Clause Third(1), (2) (Arabic, UQN p=25238); Law Art. 15; legacy_m33: SIL M/33 Art. 18(2)(a), "
               "(3), GOSI booklet 2025 p.11" + REGS_READ)

# code: (value, source_url, source_reference), valid from GOSI_FROM.
SA_GOSI = {
    "gosi.annuities_step_start": ({"default_reading": "august", "alternative": "july",
                                   "july_months": ["2025-07", "2026-07", "2027-07", "2028-07"]}, SIL_RD,
                                  "RD Clause Third(1) [CC-verify: not in Regs (read 2026-10-19); needs GOSI circular on "
                                  "step month]"),
    "gosi.rates.saned": ({"new_m273": {"employer": "0.75", "employee": "0.75"},
                          "legacy_m33": {"employer": "0.75", "employee": "0.75"}}, SIL_COM,
                         "CoM 1022 Third(6); Law Art. 44(2); Regs Art. 74 (triennial reset ±0.5% within 0.5-2%); "
                         "legacy: UIL Art. 7 as amended, SANED booklet 2025 p.4" + REGS_READ),
    "gosi.rates.occupational_hazards": ({"new_m273": {"employer": "2"}, "legacy_m33": {"employer": "2"},
                                         "max_override_pct": "4.00"}, SIL,
                                        "Law Art. 28, 29(1); Regs Art. 22 (surcharge up to double, Governor's dates); "
                                        "legacy: SIL M/33 Art. 18(1)" + REGS_READ),
    "gosi.max_contributory_wage": ({"new_m273": "45000.00", "legacy_m33": "45000.00", "across_employers": True},
                                   SIL, "Law Art. 8(2); Regs Art. 19 (all employers, pro rata); legacy: SIL M/33 Art. "
                                   "19(1)(b)" + REGS_READ),
    "gosi.min_wage": ({"saudi": "1500.00", "non_saudi": "400.00"}, GOSI_FORM7,
                      "GOSI Form 7 item 3 (GOSI guide, secondary) [CC-verify: no floor figure in Law or Regs]"),
    "gosi.saned_max_entry_age": ({"new_m273": {"max_entry_age": "64", "stop": "legal_age"},
                                  "legacy_m33": {"max_entry_age": "59", "stop_age": "60"}}, SIL_REGS,
                                 "Law Art. 43; Regs Art. 73 (re-entry at 64+ after first coverage under 64 keeps "
                                 "SANED); legacy: UIL Art. 3(1), (2), SANED booklet 2025 p.3" + REGS_READ),
    "gosi.saned_stop_month_rule": ({"rule": "month_after_reaching_age"}, SIL_REGS,
                                   "[CC-verify: Regs Art. 73 gives no stop month]"),
    "gosi.partial_month_divisor": ({"divisor": "calendar"}, SIL_REGS,
                                   "Regs Art. 20(2) (days of service in the joining and leaving months); Art. 36(1) "
                                   "by analogy [CC-verify: Regs Art. 20(2) gives no divisor]"),
    "gosi.housing_in_kind_basic_months": ({"months": "2", "per": "year"}, SIL_REGS,
                                          "Regs Art. 17(c) [CC-verify: Regs Art. 17(c) states two months' basic, not "
                                          "the period; needs GOSI circular]"),
    "gosi.variable_wage_basis": ({"method": "prior_year_average", "new_joiner": "similar_contributor",
                                  "daily_multiplier": "30", "hourly": "not_configured"}, SIL_REGS,
                                 "Regs Art. 18(1), (2); hourly [CC-verify: Governor's hourly rules not supplied]"
                                 + REGS_READ),
    "gosi.oh_abroad_suspension": ({"from": "first_of_month_after_exit", "resume": "first_of_return_month",
                                   "except_purposes": ["work"]}, SIL_REGS, "Regs Art. 23(1), (2)" + REGS_READ),
    "gosi.payment_due_day": ({"new_m273": "15", "legacy_m33": "15"}, SIL_REGS,
                             "Regs Art. 24(1); legacy: SIL M/33 Art. 19(5), (8), booklet p.13" + REGS_READ),
    "gosi.late_fine_pct": ({"pct": "2", "full_waiver_max_delay_days": "10", "governor_max_waiver_pct": "50"}, SIL,
                           "Law Art. 9(3); Regs Art. 25; legacy: SIL M/33 Art. 19(5)" + REGS_READ),
    "gosi.registration_deadline": ({"rule": "last_day_of_month", "alert_days": ["25", "last"]}, SIL_REGS,
                                   "Regs Art. 6(1)(b), (2)(a)-(c); legacy: GOSI Form 7 item 1 by analogy" + REGS_READ),
    "gosi.establishment_deadline_days": ({"days": "10"}, SIL_REGS, "Regs Art. 6(1)(a), (2)(d)" + REGS_READ),
    "gosi.wage_reference_date": ({"rule": "wage per joining month; each change by the last day of its month; no "
                                  "correction after that deadline except by the Governor"}, SIL_REGS,
                                 "Regs Art. 6(2)(c), (4)(a), 21 [CC-verify: no start-of-month rule]"),
    "gosi.legacy_increase_after_50_ratio": ({}, GOSI_BOOKLET,
                                            "SIL M/33 Art. 19(2) [CC-verify: old Regulations not supplied]"),
    "gosi.non_saudi_products": ({}, SIL, "Law Art. 5(b) [CC-verify: no CoM Art. 5(b) resolution supplied]"),
    "gosi.gcc": ({"treatment": "refuse"}, SIL_REGS, "[CC-verify: no GCC provision in Law/Regs (read 2026-10-19); GCC "
                 "extension law not found on UQN or gosi.gov.sa]"),
    "gosi.min_pension_months": ({"new_m273": "180"}, SIL_COM, "CoM 1022 Third(5)" + REGS_READ),
    "gosi.early_pension_months": ({"new_m273": "360", "legacy_m33": "300",
                                   "clause_fifth": [{"months_below": "180", "months": "360"},
                                                    {"from": "180", "to": "191", "months": "348"},
                                                    {"from": "192", "to": "203", "months": "336"},
                                                    {"from": "204", "to": "215", "months": "324"},
                                                    {"from": "216", "to": "227", "months": "312"},
                                                    {"from": "228", "to": "239", "months": "300"}]}, SIL_RD,
                                  "Law Art. 16(3); RD Clause Fifth(3), (4); SIL M/33 Art. 38(1)(b)" + REGS_READ),
}

# Clause Fifth schedule (RD M/273 Clause Fifth(1), (2), Arabic; report 2026-10-19 Table B): Gregorian age on
# 2024-07-03 in whole years -> legal age (years, months). Under 29 -> 65; 48 to under 48y6m -> 58y4m.
CLAUSE_FIFTH = [{"age_from": "0", "age_to": "28", "legal_age": "65y0m"}] + [
    {"age": str(age), "legal_age": la} for age, la in (
        (29, "64y8m"), (30, "64y4m"), (31, "64y0m"), (32, "63y8m"), (33, "63y4m"), (34, "63y0m"), (35, "62y8m"),
        (36, "62y4m"), (37, "62y0m"), (38, "61y8m"), (39, "61y4m"), (40, "61y0m"), (41, "60y8m"), (42, "60y4m"),
        (43, "60y0m"), (44, "59y8m"), (45, "59y4m"), (46, "59y0m"), (47, "58y8m"), (48, "58y4m"))]
LEGAL_AGE = ({"new_m273": {"years": "65", "article": "SIL Art. 16(1)"},
              "legacy_m33": {"years": "60", "article": "SIL M/33 Art. 38(1)(a)"},
              "legacy_clause_fifth": {"schedule": CLAUSE_FIFTH, "deemed_50_hijri_at": "48y6m",
                                      "track_rule": "under 240 months and under 48y6m Gregorian on 2024-07-03",
                                      "article": "RD Clause Fifth(1), (2)"}},
             SIL_RD, "Law Art. 16(1); RD M/273 Clause Fifth(1), (2) (Arabic, UQN p=25238); SIL M/33 Art. 38(1)(a), GOSI "
             "booklet 2025 p.21" + REGS_READ)

# hr R68 (H20(d), P19(f)): "activate only Egypt for now". One row per active jurisdiction; no row = not active.
# C-C3 (compliance 2026-10-19): DEST-ID (salary bank) and BANK-57 (each employee's bank) as bank codes. Keyed by the
# two-digit bank identifier of a Saudi IBAN (characters 5 and 6); SWIFT BICs from the banks' public SWIFT codes.
# [CC-verify: confirm each bank's code for the WPS file with the bank (P18, OQ-10)].
SAMA_BANKS = "https://www.sama.gov.sa/en-US/LicenseEntities/Pages/LicensedBanks.aspx"
WPS_BANK_CODES = ({"by_iban_code": {
    "80": {"bic": "RJHISARI", "salary_bank": "alrajhi"},
    "10": {"bic": "NCBKSAJE", "salary_bank": "snb"},
    "20": {"bic": "RIBLSARI", "salary_bank": "riyad"},
    "60": {"bic": "BJAZSAJE", "salary_bank": "aljazira"},
    "05": {"bic": "INMASARI", "salary_bank": "alinma"},
    "45": {"bic": "SABBSARI"}, "55": {"bic": "BSFRSARI"}, "30": {"bic": "ARNBSARI"}, "15": {"bic": "ALBISARI"},
    "65": {"bic": "SIBCSARI"},
}}, SAMA_BANKS, "Bank identifier in the Saudi IBAN and SWIFT BIC per bank [CC-verify: WPS bank code per bank, P18]")
WPS_FROM = "2000-01-01"

ACTIVE_FROM = "2026-10-17"
ACTIVE = {"SA": ({"active": True}, OWNER, "Project owner B3 (2026-10-17): Saudi payroll"),
          "EG": ({"active": True}, OWNER, "Project owner B3 (2026-10-17): \"activate only Egypt for now\"")}

# Egypt (docs/compliance/egypt-payroll-2026-10-18.md §1; all confidence-tagged in source_reference). code:
# (valid_from, value, url, reference).
EG_ANDERSEN = "https://eg.andersen.com/wp-content/uploads/2025/05/Translation-of-Labor-law-No.-14-of-2025.pdf"
EG_ROWS = (
    ("labour.law_in_force", "2025-09-01", {"law": "14/2025", "replaces": ["12/2003", "125/2010"]},
     "https://taxnews.ey.com/news/2025-1555", "Labour Law 14/2025 (H)"),
    ("hours.max_daily", "2025-09-01", {"hours": "8"}, EG_ANDERSEN, "Labour Law 14/2025 Art. 117 (H)"),
    ("hours.max_weekly", "2025-09-01", {"hours": "48"}, EG_ANDERSEN, "Labour Law 14/2025 Art. 117 (H)"),
    ("overtime.premium", "2025-09-01", {"day": "0.35", "night": "0.70", "rest_day": "1.00"},
     "https://globallawexperts.com/how-to-calculate-overtime-in-egypt/", "Labour Law 14/2025 (M)"),
    ("probation.max_days", "2025-09-01", {"days": "90"},
     "https://hnzlaw.com/probation-period-under-labor-law-14-of-2025-what-employers-and-hr-professionals-need-to-know/",
     "Labour Law 14/2025 Art. 90 (H)"),
    ("leave.annual.days", "2025-09-01", {"first_year_after_6_months": "15", "from_year_2": "21",
                                         "after_10_years_or_age_50": "30", "disabled": "45"},
     "https://www.zenhr.com/en/egypt-labor-law", "Labour Law 14/2025 (M)"),
    ("termination.notice_days", "2025-09-01", {"days": "90"}, "https://eg.andersen.com/egypts-labour-law-14-2025/",
     "Labour Law 14/2025 Art. 156 (M)"),
    ("wage.minimum.private_monthly", "2024-05-01", {"amount": "6000.00"}, "https://employsome.com/blog/minimum-wage-egypt/",
     "National Wages Council (M)"),
    ("wage.minimum.private_monthly", "2025-03-01", {"amount": "7000.00"},
     "https://employsome.com/blog/minimum-wage-egypt/", "National Wages Council (M)"),
    ("deduction.loan_max_pct", "2025-09-01", {"pct": "0.10"},
     "https://www.elwatannews.com/news/details/8356604", "Labour Law 14/2025 Art. 113 (H)"),
    ("deduction.debt_max_pct", "2025-09-01", {"debt": "0.25", "nafaqa": "0.50"},
     "https://www.elwatannews.com/news/details/8356604", "Labour Law 14/2025 Art. 113/114 (H)"),
    ("si.rates", "2020-01-01", {"employee": "0.11", "employer": "0.1875"},
     "https://eg.andersen.com/social-insurance-in-egypt/", "Social Insurance Law 148/2019 (H)"),
    ("si.wage_limits", "2025-01-01", {"min_monthly": "2300.00", "max_monthly": "14500.00"},
     "https://www.legal500.com/intelligence/egypt/employment-and-hr/social-insurance-in-egypt", "Law 148/2019 (H)"),
    ("si.wage_limits", "2026-01-01", {"min_monthly": "2700.00", "max_monthly": "16700.00"},
     "https://www.businesstodayegypt.com/Article/1/6950/", "Law 148/2019 (H)"),
    ("si.payment_due", "2020-01-01", {"day_of_following_month": "1", "alternative_in_guides": "15"},
     "https://www.vetogate.com/4526073", "Law 148/2019 Art. 121 (L; sources conflict)"),
    ("tax.salary.personal_exemption_annual", "2024-03-01", {"amount": "20000.00"},
     "https://taxsummaries.pwc.com/egypt/individual/taxes-on-personal-income", "Income Tax Law 91/2005 as amended (H)"),
    ("tax.salary.brackets", "2024-03-01", {"bands": [
        {"to": "40000.00", "rate": "0"}, {"to": "55000.00", "rate": "0.10"}, {"to": "70000.00", "rate": "0.15"},
        {"to": "200000.00", "rate": "0.20"}, {"to": "400000.00", "rate": "0.225"}, {"to": "1200000.00", "rate": "0.25"},
        {"to": None, "rate": "0.275"}]}, "https://taxsummaries.pwc.com/egypt/individual/taxes-on-personal-income",
     "Law 7/2024 amending Art. 8 (H); band exclusion (L)"),
    ("levy.martyrs_fund", "2021-01-01", {"rate": "0.0005", "base": "gross_monthly_salary"},
     "https://taxsummaries.pwc.com/egypt/individual/other-taxes", "Law 4/2021 (M; who bears it disputed)"),
)
# Slice 2 (§10.8 item 7): the rows the Egypt calculation reads beyond slice 1's (research §1.2, §1.3, §2.3, §2.5).
EG_PWC = "https://taxsummaries.pwc.com/egypt/individual/taxes-on-personal-income"
EG_SLICE2_ROWS = (
    ("si.insurable_components", "2020-01-01",
     {"codes": ["BASIC", "HOUSING", "TRANSPORT", "OTHER", "COMMISSION", "PROFIT_SHARE"], "one_off_inputs": False},
     "https://eg.andersen.com/wp-content/uploads/2025/06/Law-No.-148-of-the-Year-2019-and-its-executive-regulation.pdf",
     "Law 148/2019 Art. 1 (insurable wage = cash remuneration for the main job; per-component inclusion needs local "
     "sign-off) (L)"),
    ("si.partial_month", "2020-01-01", {"rule": "prorate_paid_days"}, "https://eg.andersen.com/social-insurance-in-egypt/",
     "Joining and leaving months: insurable wage x paid days / divisor (team reading, not in the research) (L)"),
    ("tax.salary.bracket_exclusion", "2024-03-01", {"columns": [
        {"income_upto": "600000.00", "rates": ["0", "0.10", "0.15", "0.20", "0.225", "0.25", "0.275"]},
        {"income_upto": "700000.00", "rates": ["0.10", "0.10", "0.15", "0.20", "0.225", "0.25", "0.275"]},
        {"income_upto": "800000.00", "rates": ["0.15", "0.15", "0.15", "0.20", "0.225", "0.25", "0.275"]},
        {"income_upto": "900000.00", "rates": ["0.20", "0.20", "0.20", "0.20", "0.225", "0.25", "0.275"]},
        {"income_upto": "1200000.00", "rates": ["0.225", "0.225", "0.225", "0.225", "0.225", "0.25", "0.275"]},
        {"income_upto": None, "rates": ["0.25", "0.25", "0.25", "0.25", "0.25", "0.25", "0.275"]}]},
     "https://www.a-h-g.net/ar/income-tax-brackets-table/", "Law 7/2024 amending Art. 8: high-income band exclusion, "
     "column by annual net taxable income (reconstructed from secondary sources; sources conflict, C7) (L to M)"),
    ("tax.salary.zero_band_residents_only", "2024-03-01", {"apply": True, "non_resident_first_band_rate": "0.10"},
     "https://taxsummaries.pwc.com/egypt/individual/deductions", "PwC: the 0% band applies to residents only (L)"),
    ("tax.salary.method", "2020-01-01", {"method": "cumulative_annualised", "year": "calendar"},
     "https://www.activpayroll.com/global-insights/egypt",
     "Monthly withholding: annualise year-to-date taxable pay, tax the year, withhold the year-to-date share less "
     "what was withheld (standard ETA practice; rounding rule not found) (M)"),
    ("uhi.rates", "2018-07-01", {"employee": "0.01", "employer": "0.04", "employer_min_monthly": "50.00",
                                 "spouse_not_working": "0.03", "per_dependent_child": "0.01",
                                 "base": "si_contribution_wage"},
     "https://manshurat.org/node/63712", "Universal Health Insurance Law 2/2018 Art. 40 and Table 1 (employee and "
     "family rates unconfirmed) (L)"),
    ("uhi.governorates_live", "2018-07-01",
     {"governorates": ["port_said", "ismailia", "luxor", "south_sinai", "suez", "aswan"]},
     "https://www.madamasr.com/en/2026/07/30/feature/politics/cracks-in-the-universal-health-insurance-system-an-accounting-of-phase-1/",
     "UHI phase 1 governorates (live); phase 2 (Minya first, 2026) not dated in the research (L)"),
    ("eosb.monthly_accrual", "2025-09-01", {"accrue": False}, "https://eg.andersen.com/egypts-labour-law-14-2025/",
     "Labour Law 14/2025: no statutory end-of-service gratuity for resignation or expiry; compensation only on a "
     "termination event (research §2.1, §4 item 12) (M)"),
)
# Slice 3 (§11.3.4): seeded empty and unverified. The Mudad justification codes are not in the HRSD sources
# [verify: Mudad code list]; the internal reasons (§11.6) are exported with an empty code and a warning until a person
# fills and verifies this row. Egypt's untaken-leave valuation basis waits for the Egyptian adviser (OQ-55): while it
# is empty or unverified the leave liability report shows Egypt's days, not valued.
MUDAD = "https://mudad.com.sa/"
SLICE3_SA = {
    "wps.mudad.justification_codes": ({"codes": {}}, MUDAD,
                                      "Internal reason to Mudad justification code (payroll §11.6) [verify: Mudad code "
                                      "list, not in HRSD sources] | seeded empty 2026-10-20 (OQ-56)"),
}
SLICE3_EG = {
    "leave.encashment_basis": ({}, EG_ANDERSEN, "Labour Law 14/2025: cash for untaken leave on exit; which wage and "
                               "divisor [verify: Egypt adviser] (OQ-55) | seeded empty 2026-10-20"),
}
EG_NOTE = " | Research 2026-10-18 from search-engine extracts, not the Gazette; Egyptian adviser to confirm (OQ-28)"


def apply(StatutoryParameter):
    """Create (or refresh while pristine) every payroll row. Never verifies (hr R7)."""
    from hr.statutory_seed import apply_rows

    apply_rows(StatutoryParameter, SA_PAYROLL, valid_from=LL_FROM)
    apply_rows(StatutoryParameter, SA_GOSI, valid_from=GOSI_FROM)
    apply_rows(StatutoryParameter, {"gosi.legal_age": LEGAL_AGE}, valid_from=GOSI_FROM)
    for valid_from, pct in ANNUITY_STEPS:
        apply_rows(StatutoryParameter, {"gosi.rates.annuities": (
            {"new_m273": {"employer": pct, "employee": pct}, "legacy_m33": dict(_LEGACY)}, SIL_RD, ANNUITY_REF)},
            valid_from=valid_from)
    apply_rows(StatutoryParameter, {"wps.bank_codes": WPS_BANK_CODES}, valid_from=WPS_FROM)
    for jurisdiction, row in ACTIVE.items():
        apply_rows(StatutoryParameter, {"jurisdiction.active": row}, jurisdiction=jurisdiction, valid_from=ACTIVE_FROM)
    for code, valid_from, value, url, ref in EG_ROWS + EG_SLICE2_ROWS:
        apply_rows(StatutoryParameter, {code: (value, url, ref + EG_NOTE)}, jurisdiction="EG", valid_from=valid_from)
    apply_rows(StatutoryParameter, SLICE3_SA, valid_from=WPS_FROM)
    apply_rows(StatutoryParameter, {code: (value, url, ref + EG_NOTE) for code, (value, url, ref) in SLICE3_EG.items()},
               jurisdiction="EG", valid_from="2025-09-01")
