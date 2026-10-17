"""Seed rows of ``hr.StatutoryParameter`` for jurisdiction SA (hr R6, R7). The only place regulatory figures are
written down in code; every service reads them from the database rows valid on the relevant date.

Every row is seeded ``unverified``. Figures follow docs/compliance/hr-payroll-labour-law-2026-10-17.md (official
HRSD texts read 2026-10-17: Labour Law as amended by M/44, in force 2025-02-19, and the Implementing
Regulations), which corrected the spec's from-memory figures; they still need the managing partner's
verification row by row (R7). [CC-verify] marks figures whose source the review left unclear.

``valid_from`` is 2000-01-01 so contracts and service periods that started before the 2025 amendments can be
computed; the rows were checked only against the text in force since 2025-02-19 (earlier versions of the law
are not verified).

Values use Decimal strings (R6: no floats); fractions are exact rationals such as "1/3".
"""
LABOUR_LAW = "https://www.hrsd.gov.sa/en/policies/labor-law"
REGULATIONS = "https://www.hrsd.gov.sa/en/policies/implementing-regulations-labor-law"
VALID_FROM = "2000-01-01"

_RESIGNATION_BANDS = [  # Art. 85: <2 y 0; 2 to 5 y inclusive 1/3; >5 and <10 2/3; >=10 full
    {"from": "0", "fraction": "0"},
    {"from": "2", "fraction": "1/3"},
    {"above": "5", "fraction": "2/3"},
    {"from": "10", "fraction": "1"},
]
_FULL = [{"from": "0", "fraction": "1"}]
_NONE = [{"from": "0", "fraction": "0"}]

# code: (value, source_url, source_reference)
SA_PARAMETERS = {
    # R9 [compliance: format not stated in any official source] — a warning, never a hard check.
    "id.format": ({"national_id": {"length": "10", "prefix": "1"}, "iqama": {"length": "10", "prefix": "2"}},
                  LABOUR_LAW, "[CC-verify] no official digit format found (WPS guide MOL-ID); Absher/NIC to confirm"),
    # R17 as corrected: one maximum of 180 days, split periods allowed (Art. 53, Regs Art. 19).
    "probation.max_days": ({"days": "180"}, LABOUR_LAW, "Labour Law Art. 53; Regs Art. 19"),
    # Art. 54: one probation per employer unless another job or 6+ months since the last employment ended.
    "probation.repeat": ({"months_since_last_employment": "6"}, LABOUR_LAW, "Labour Law Art. 54"),
    # Official holidays not counted in probation and extending annual leave (Regs Art. 19, 24). Eid dates are
    # Hijri and entered per year as date ranges by the parameter maintainer.
    "calendar.official_holidays": ({"fixed_month_days": ["02-22", "09-23"], "date_ranges": []}, REGULATIONS,
                                   "Regs Art. 19, 24 (Founding Day, National Day; Eid al-Fitr and Eid al-Adha per year)"),
    # Art. 10: Hijri unless the contract states Gregorian. Divisor for pro-rata [CC-verify: 365 / 365.25 / 354].
    "calendar.year_days": ({"gregorian": "365", "hijri": "354"}, LABOUR_LAW, "Labour Law Art. 10 [CC-verify divisor]"),
    # R19.
    "hours.max_daily": ({"hours": "8"}, LABOUR_LAW, "Labour Law Art. 98"),
    "hours.max_weekly": ({"hours": "48"}, LABOUR_LAW, "Labour Law Art. 98"),
    "hours.max_daily_extended": ({"hours": "9"}, LABOUR_LAW, "Labour Law Art. 99 (by ministerial decision)"),
    "hours.ramadan": ({"daily": "6", "weekly": "36"}, LABOUR_LAW, "Labour Law Art. 98 (not computed, OQ-22)"),
    # R16 as corrected: Saudis' fixed-term contract becomes open-ended after 3 renewals or 4 years (Art. 55).
    "contract.saudi_open_ended": ({"renewals": "3", "years": "4"}, LABOUR_LAW, "Labour Law Art. 55"),
    # R37 notice (Art. 75): open-ended contracts only.
    "notice.min_days": ({"employer": "60", "employee": "30", "not_monthly": "30"}, LABOUR_LAW, "Labour Law Art. 75"),
    # R30, R56: annual leave tiers; the 30-day rate from the day after the 5-year anniversary (Art. 109).
    # Unpaid leave beyond 20 days in a work year suspends service and accrual (Art. 2, 116; Regs Art. 25).
    "leave.annual": ({"tiers": [{"from_years": "0", "days": "21"}, {"from_years": "5", "days": "30"}],
                      "count": "calendar", "unpaid_suspends_after_days": "20"},
                     LABOUR_LAW, "Labour Law Art. 109, 2, 116; Regs Art. 24-26 [CC-verify counting basis]"),
    # R32: Art. 117, one year from the first sick day of the cycle.
    "leave.sick": ({"tiers": [{"days": "30", "pay_pct": "100"}, {"days": "60", "pay_pct": "75"},
                              {"days": "30", "pay_pct": "0"}], "count": "calendar"},
                   LABOUR_LAW, "Labour Law Art. 117"),
    "leave.marriage": ({"days": "5"}, LABOUR_LAW, "Labour Law Art. 113"),
    "leave.bereavement": ({"days": "5"}, LABOUR_LAW, "Labour Law Art. 113 (spouse, ascendant, descendant)"),
    "leave.bereavement_sibling": ({"days": "3"}, LABOUR_LAW, "Labour Law Art. 113 (brother or sister)"),
    "leave.paternity": ({"days": "3", "within_days_of_event": "7"}, LABOUR_LAW, "Labour Law Art. 113"),
    # Art. 151 as amended: 12 weeks fully paid; 6 weeks after birth mandatory; start no earlier than 4 weeks
    # before the expected date; +1 month unpaid on request; sick or disabled child +1 month paid +1 unpaid.
    "leave.maternity": ({"days": "84", "post_birth_mandatory_days": "42", "earliest_days_before_due": "28"},
                        LABOUR_LAW, "Labour Law Art. 151"),
    "leave.maternity_unpaid": ({"days": "30"}, LABOUR_LAW, "Labour Law Art. 151"),
    "leave.maternity_child": ({"days": "30"}, LABOUR_LAW, "Labour Law Art. 151 (sick or disabled child)"),
    "leave.maternity_child_unpaid": ({"days": "30"}, LABOUR_LAW, "Labour Law Art. 151 (sick or disabled child)"),
    # Art. 160: 4 months 10 days minimum (Muslim widow; 15 days otherwise); religion is not stored (OQ-22).
    "leave.iddah": ({"days": "130", "min_days": "15"}, LABOUR_LAW, "Labour Law Art. 160"),
    "leave.iddah_extension": ({"until_event": True}, LABOUR_LAW, "Labour Law Art. 160 (unpaid until delivery)"),
    # Art. 114: 10 to 15 days including Eid al-Adha, once, not performed before, after 2 years.
    "leave.hajj": ({"min_days": "10", "max_days": "15", "min_service_years": "2"}, LABOUR_LAW, "Labour Law Art. 114"),
    # Art. 115: paid for an employer-approved, non-repeated year; requested 15 days ahead.
    "leave.exam": ({"request_lead_days": "15"}, LABOUR_LAW, "Labour Law Art. 115"),
    # R39: Art. 84 award; unpaid days beyond 20 per work year are not service (Art. 2, 116).
    "eosb.award_table": ({"tiers": [{"up_to_years": "5", "months_per_year": "1/2"},
                                    {"up_to_years": None, "months_per_year": "1"}],
                          "exclude_unpaid_days_beyond": "20"}, LABOUR_LAW, "Labour Law Art. 84, 2, 116"),
    # R37 as corrected. employee_notice_art75 uses the Art. 85 bands pending legal sign-off (compliance §3).
    "eosb.fraction_table": ({
        "resignation": _RESIGNATION_BANDS,
        "employee_notice_art75": _RESIGNATION_BANDS,
        "employer_termination": _FULL, "end_of_fixed_term": _FULL, "mutual_agreement": _FULL,
        "art80_dismissal": _NONE, "art81_employee_leaves": _FULL, "force_majeure": _FULL,
        "female_marriage_childbirth": _FULL, "death": _FULL, "incapacity": _FULL, "retirement": _FULL,
        "closure": _FULL, "activity_ended": _FULL, "bankruptcy": _FULL, "probation_termination": _NONE,
    }, LABOUR_LAW, "Labour Law Art. 84, 85, 87, 80, 81, 74, 79, 54 [CC-verify: Art. 85 for Art. 75 notice]"),
    # Art. 79 bis resignation timing.
    "termination.resignation": ({"deemed_acceptance_days": "30", "max_postponement_days": "60",
                                 "withdrawal_days": "7"}, LABOUR_LAW, "Labour Law Art. 79 bis"),
    # Payroll R34 / Art. 111: encashment = balance x daily wage, month = 30 days (Art. 2).
    "leave.encashment": ({"day_divisor": "30"}, LABOUR_LAW, "Labour Law Art. 111, 2"),
}


def apply(StatutoryParameter):
    """Create the SA rows that do not exist yet (idempotent; takes the model so a migration can pass its own)."""
    for code, (value, url, ref) in SA_PARAMETERS.items():
        StatutoryParameter.objects.get_or_create(
            jurisdiction="SA", code=code, valid_from=VALID_FROM,
            defaults=dict(value=value, source_url=url, source_reference=ref, status="unverified"),
        )
