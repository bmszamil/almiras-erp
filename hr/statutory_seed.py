"""Seed rows of ``hr.StatutoryParameter`` for jurisdiction SA (hr R6, R7). The only place regulatory figures are
written down in code; every service reads them from the database rows valid on the relevant date.

Every row is seeded ``unverified`` with ``verified_by`` empty: the seed loader never verifies (R7, hr.md 3.9); the
managing partner verifies row by row through ``hr.services.verify_statutory_parameter``. Figures follow
docs/compliance/hr-payroll-labour-law-2026-10-17.md (official HRSD texts downloaded 2026-10-17: Labour Law as
amended by M/44, in force 2025-02-19, and the Implementing Regulations). Rows the compliance check found verified
(``CHECKED``) carry that check, with its source and date, in ``source_reference``; [CC-verify] and [Needs expert
sign-off] mark figures the check left open.

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
                      "count": "calendar"},
                     LABOUR_LAW, "Labour Law Art. 109; Regs Art. 24-26 [CC-verify counting basis]"),
    # R62: unpaid leave beyond 20 days suspends the contract (no accrual, no EOSB service; a fixed term is extended
    # unless agreed otherwise). Aggregation per work year (Art. 2) or per leave (Art. 116): OQ-37, default work year.
    "leave.unpaid.suspension_after_days": ({"days": "20", "aggregation": "work_year"}, LABOUR_LAW,
                                           "Labour Law Art. 2, 116; Regs Art. 25 [Needs expert sign-off: legal, "
                                           "aggregation OQ-37]"),
    # R32: Art. 117, one year from the first sick day of the cycle.
    # An official holiday inside sick leave is paid in full whatever the tier (Regs Art. 24; R64, payroll R17).
    "leave.sick": ({"tiers": [{"days": "30", "pay_pct": "100"}, {"days": "60", "pay_pct": "75"},
                              {"days": "30", "pay_pct": "0"}], "count": "calendar", "holiday_pay_pct": "100"},
                   LABOUR_LAW, "Labour Law Art. 117; Regs Art. 24"),
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
    # R39: Art. 84 award (suspended unpaid days are not service: leave.unpaid.suspension_after_days).
    "eosb.award_table": ({"tiers": [{"up_to_years": "5", "months_per_year": "1/2"},
                                    {"up_to_years": None, "months_per_year": "1"}]}, LABOUR_LAW, "Labour Law Art. 84"),
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
    # R37 (d): a manual Art. 77 amount below 2 months' wage needs a court judgment or contract clause.
    "compensation.art77_min_months": ({"months": "2"}, LABOUR_LAW, "Labour Law Art. 77"),
    # R37: the Art. 80 cases (9; case 7 absence needs written warnings) and the Art. 81 cases (7).
    "termination.article_paragraphs": ({"art80": ["1", "2", "3", "4", "5", "6", "7", "8", "9"],
                                        "art81": ["1", "2", "3", "4", "5", "6", "7"],
                                        "art80_absence_paragraph": "7"}, LABOUR_LAW, "Labour Law Art. 80, 81"),
    # R37: Art. 87 windows for a female worker: 6 months from the marriage contract, 3 months from delivery.
    "termination.art87_windows": ({"marriage_months": "6", "delivery_months": "3"}, LABOUR_LAW, "Labour Law Art. 87"),
    # R59, R60: no dismissal for illness before the sick-leave periods are used up (Art. 82); no dismissal or notice
    # during pregnancy, maternity leave or related illness up to 180 days (Art. 155).
    "termination.protection": ({"pregnancy_illness_days": "180"}, LABOUR_LAW, "Labour Law Art. 82, 155"),
    # R64, payroll R34: final settlement within 7 days, or 14 days when the worker ended the contract (Art. 88).
    "final_settlement.due_days": ({"employer": "7", "worker": "14"}, LABOUR_LAW, "Labour Law Art. 88"),
    # Payroll R34 / Art. 111: encashment = balance x daily wage, month = 30 days (Art. 2).
    "leave.encashment": ({"day_divisor": "30"}, LABOUR_LAW, "Labour Law Art. 111, 2"),
}


# Rows the compliance check of 2026-10-17 found verified against the HRSD text (hr.md 3.9 "verified"). The
# others stay open: id.format [CC-verify], calendar.year_days (EOSB divisor, OQ-36 legal sign-off).
UNCHECKED = frozenset({"id.format", "calendar.year_days"})
CHECKED = frozenset(SA_PARAMETERS) - UNCHECKED
CHECK_NOTE = " | Checked 2026-10-17 against the HRSD text (docs/compliance/hr-payroll-labour-law-2026-10-17.md); MP verification pending (R7)"


def reference(code):
    ref = SA_PARAMETERS[code][2]
    return ref + CHECK_NOTE if code in CHECKED else ref


def apply(StatutoryParameter):
    """Create the SA rows that do not exist yet and bring the seeded rows nobody has verified or used up to date
    (idempotent; takes the model so a migration can pass its own). Never verifies a row and never touches a
    verified, superseded or used row: a change to one of those is a new row entered and verified by people (R7)."""
    for code, (value, url, _ref) in SA_PARAMETERS.items():
        row, created = StatutoryParameter.objects.get_or_create(
            jurisdiction="SA", code=code, valid_from=VALID_FROM,
            defaults=dict(value=value, source_url=url, source_reference=reference(code), status="unverified"),
        )
        if created or row.status != "unverified" or row.used_at is not None or row.verified_by_id is not None:
            continue
        if (row.value, row.source_url, row.source_reference) != (value, url, reference(code)):
            row.value, row.source_url, row.source_reference = value, url, reference(code)
            row.save()
