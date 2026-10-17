"""hr R58 / AT-37: Labour Law Art. 10 calendar basis. Periods are counted in the Umm al-Qura calendar (open-source
``hijridate`` library) unless the contract or work regulations state Gregorian, which needs a reference."""
from datetime import date, timedelta

import pytest
from hijridate import Gregorian

from hr import services
from hr.conftest import contract, employee, hired
from hr.models import EmploymentContract, HrError

HIRE = date(2022, 1, 16)


def _hijri_plus(day, years):
    h = Gregorian(day.year, day.month, day.day).to_hijri()
    g = type(h)(h.year + years, h.month, h.day).to_gregorian()
    return date(g.year, g.month, g.day)


def test_at37_gregorian_without_reference_refused(w):
    emp = employee(w)
    with pytest.raises(HrError) as exc:
        contract(w, emp, calendar_basis="gregorian", calendar_basis_ref="")
    assert "cl. 14.6" in exc.value.messages[0] and "المادة 10" in exc.value.messages[0]
    c = contract(w, emp, calendar_basis="gregorian", calendar_basis_ref="Work regulations art. 3")
    assert c.status == "approved" and c.calendar_basis_ref == "Work regulations art. 3"


def test_at37_hijri_needs_no_reference_and_is_the_default(w):
    emp = employee(w)
    c = contract(w, emp, calendar_basis="hijri", calendar_basis_ref="")
    assert c.status == "approved"
    assert EmploymentContract._meta.get_field("calendar_basis").default == "hijri"


def test_r58_remuneration_contract_needs_no_calendar_reference(w):
    emp = employee(w, basis="remuneration")
    assert contract(w, emp, calendar_basis="gregorian", calendar_basis_ref="").status == "approved"


def test_at37_hijri_five_years_is_the_umm_al_qura_anniversary_earlier_than_gregorian(w):
    hijri = hired(w, hire_date=HIRE, contract_kwargs={"calendar_basis": "hijri", "calendar_basis_ref": ""})
    greg = hired(w, hire_date=HIRE)
    h5 = services.service_anniversary(hijri, 5, user=w.h)
    g5 = services.service_anniversary(greg, 5, user=w.h)
    assert h5 == _hijri_plus(HIRE, 5) == date(2026, 11, 23)  # 13 Jumada II 1443 -> 13 Jumada II 1448
    assert g5 == date(2027, 1, 16)
    assert h5 < g5


def test_at37_hijri_eosb_band_changes_on_the_hijri_anniversary(w):
    """Art. 85 bands (via employee_notice_art75, OQ-33 default) with the 5-year threshold in each calendar."""
    hijri = hired(w, hire_date=HIRE, contract_kwargs={"calendar_basis": "hijri", "calendar_basis_ref": ""})
    greg = hired(w, hire_date=HIRE)
    anniversary = date(2026, 11, 23)
    on_eve = services.calculate_eosb(hijri, anniversary - timedelta(days=1), "employee_notice_art75", user=w.h)
    after = services.calculate_eosb(hijri, anniversary, "employee_notice_art75", user=w.h)
    assert on_eve.service_years == 5 and str(on_eve.fraction) == "1/3"  # exactly 5 Hijri years: inclusive band
    assert after.service_years > 5 and str(after.fraction) == "2/3"
    assert after.calendar_basis == "hijri"
    g = services.calculate_eosb(greg, anniversary, "employee_notice_art75", user=w.h)
    assert g.service_years < 5 and str(g.fraction) == "1/3"


def test_r58_hijri_sick_year_is_an_umm_al_qura_year(w):
    emp = hired(w, contract_kwargs={"calendar_basis": "hijri", "calendar_basis_ref": ""})
    first = services.request_leave(emp, "SICK", user=w.h, start_date=date(2027, 1, 10), end_date=date(2027, 1, 11),
                                   document_ref="MED-1")
    services.approve_leave(first, user=w.h2)
    # 2027-12-31 is inside a Gregorian year from 2027-01-10 but after the Hijri year (354 or 355 days) ends.
    window_end = _hijri_plus(date(2027, 1, 10), 1) - timedelta(days=1)
    assert window_end < date(2027, 12, 31)
    later = services.request_leave(emp, "SICK", user=w.h, start_date=date(2027, 12, 31), end_date=date(2027, 12, 31),
                                   document_ref="MED-2")
    services.approve_leave(later, user=w.h2)
    day = later.sick_days.get()
    assert day.cycle_start == date(2027, 12, 31) and day.tier_no == 1


def test_r58_outside_the_umm_al_qura_table_is_refused():
    with pytest.raises(HrError) as exc:
        services._anniversary(date(2075, 1, 1), 5, "hijri")
    assert exc.value.code == "hijri_out_of_range"
