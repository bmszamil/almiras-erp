"""Proposed SAMA non-publication days (owner decision H1, ledger-fx R12 amendment, AT-44).

SAMA publishes no exchange rates on the weekend (Friday and Saturday) or on KSA public holidays. This module
proposes those days for whole Gregorian years; ``ledger/seed/sama_non_publication_days_2026_2027.json`` holds the
output for 2026 and 2027, which ``python manage.py ledger_load_non_publication_days`` loads **unverified**. A second
person checks each row against the SAMA / official announcement (``ledger.services.verify_non_publication_day``).
Since owner decision I2 the days are information only: ``get_sama_rate`` uses the latest earlier SAMA row across
any day without a published rate.

Rules proposed (team, to confirm; none of them is taken from a SAMA announcement):

- Weekend: every Friday and Saturday.
- Founding Day 22 February and National Day 23 September (Labour Law Implementing Regulations Art. 24). Neither
  falls on a Friday or Saturday in 2026 or 2027, so no weekday substitute is proposed.
- Eid al-Fitr: four days starting the day after 29 Ramadan; Eid al-Adha: four days starting on 9 Dhu al-Hijjah
  (day of Arafah), as Regs Art. 24 and the HR calendar (``hr.statutory_seed.eid_ranges``). Hijri dates from the
  Umm al-Qura table of the open-source ``hijridate`` library. The announced dates (moon sighting) and SAMA's own
  bank-holiday announcements can differ by a day or be longer; a longer announced holiday makes the lookup fail
  closed (``sama_rate_missing``) until the extra days are added by hand.
"""
import json
from datetime import date, timedelta
from pathlib import Path

from hijridate import Hijri

SEED_FILE = Path(__file__).resolve().parent / "seed" / "sama_non_publication_days_2026_2027.json"
SEED_YEARS = (2026, 2027)
EID_HOLIDAY_DAYS = 4
PROPOSED = ("proposed — confirm against the SAMA/official announcement / "
            "مقترح — يُؤكَّد من إعلان البنك المركزي السعودي أو الإعلان الرسمي")

_WEEKEND = {4: ("Friday", "الجمعة"), 5: ("Saturday", "السبت")}  # date.weekday(): Monday = 0
_FIXED = (((2, 22), "Founding Day", "يوم التأسيس", "Regs Art. 24"),
          ((9, 23), "National Day", "اليوم الوطني", "Regs Art. 24"))


def _gregorian(year, month, day):
    g = Hijri(year, month, day).to_gregorian()
    return date(g.year, g.month, g.day)


def _eid_days(year):
    """{date: (name_en, name_ar, rule)} of the Eid holidays falling in Gregorian ``year``."""
    out = {}
    for hijri in range(year - 580, year - 577):  # the Hijri years whose Eids can fall in ``year``
        try:
            fitr = _gregorian(hijri, 9, 29) + timedelta(days=1)
            adha = _gregorian(hijri, 12, 9)
        except (OverflowError, ValueError):
            continue
        for start, en, ar, rule in (
            (fitr, f"Eid al-Fitr holiday {hijri} AH", f"إجازة عيد الفطر {hijri}هـ",
             f"Eid al-Fitr: 4 days from the day after 29 Ramadan {hijri} (Umm al-Qura, hijridate)"),
            (adha, f"Eid al-Adha holiday {hijri} AH", f"إجازة عيد الأضحى {hijri}هـ",
             f"Eid al-Adha: 4 days from 9 Dhu al-Hijjah {hijri} (Umm al-Qura, hijridate)"),
        ):
            for offset in range(EID_HOLIDAY_DAYS):
                day = start + timedelta(days=offset)
                if day.year == year:
                    out[day] = (en, ar, rule)
    return out


def proposed_days(years=SEED_YEARS):
    """The proposed rows for ``years``, one per date, sorted: dicts with date (ISO), name_en, name_ar, source.

    A holiday on a Friday or Saturday keeps the holiday's name with the weekday added.
    """
    rows = []
    for year in years:
        holidays = _eid_days(year)
        for (month, day), en, ar, rule in _FIXED:
            holidays[date(year, month, day)] = (en, ar, rule)
        day = date(year, 1, 1)
        while day.year == year:
            weekend = _WEEKEND.get(day.weekday())
            if day in holidays:
                en, ar, rule = holidays[day]
                if weekend:
                    en, ar = f"{en} ({weekend[0]})", f"{ar} ({weekend[1]})"
            elif weekend:
                en, ar, rule = (f"Weekend ({weekend[0]})", f"عطلة نهاية الأسبوع ({weekend[1]})",
                                "Weekend: Friday and Saturday")
            else:
                day += timedelta(days=1)
                continue
            rows.append({"date": day.isoformat(), "name_en": en, "name_ar": ar, "source": f"{PROPOSED}; {rule}"})
            day += timedelta(days=1)
    return rows


def load_seed_file(path=SEED_FILE):
    """The rows of a seed file (a JSON list of objects with date, name_en, name_ar, source)."""
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)
    if not isinstance(rows, list):
        raise ValueError("The seed file must hold a JSON list of days.")
    return rows


def write_seed_file(path=SEED_FILE, years=SEED_YEARS):
    """Regenerate the seed file (developers; the test ``test_seed_file_matches_the_rules`` keeps it in step)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(proposed_days(years), handle, ensure_ascii=False, indent=1)
        handle.write("\n")
