"""Pure ZATCA rule checks and computations (no database writes, no rights): used by ``zatca.services``.

Each check returns a list of ``(code, english, arabic)`` problems (``zatca.messages.LOCAL``); an empty list
means the rule holds. Nothing here consumes an ICV: services run these before the stamping transaction.
"""
import calendar
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .messages import local

CENT = Decimal("0.01")
HUNDRED = Decimal("100")
SUBTYPE_RE = re.compile(r"^0[12][01]{5}\Z")
VAT_RE = re.compile(r"^3[0-9]{13}3\Z")
SELLER_SCHEMES = ("CRN", "MOM", "MLS", "700", "SAG", "OTH")  # BR-KSA-08 priority order
SUPPORTED_TYPES = ("388", "381", "383")


def _digits(value, n):
    return bool(value) and len(value) == n and value.isascii() and value.isdigit()


def vat_number_ok(value):
    return bool(value) and bool(VAT_RE.match(value))


def seller_address_problems(*, vat_number, street, building_no, postal_code, city, district, country,
                            additional_no=""):
    """R-4 / AT-48: VAT number (BR-KSA-40), street, building number (4 digits), postal code (5 digits), city,
    district and country present (BR-KSA-09, -37, -66). Additional number optional, 4 digits when given."""
    problems = []
    if not vat_number_ok(vat_number or ""):
        problems.append(local("vat_number"))
    if not (street or "").strip():
        problems.append(local("street"))
    if not _digits(building_no or "", 4):
        problems.append(local("building_no"))
    if not _digits(postal_code or "", 5):
        problems.append(local("postal_code"))
    if not (city or "").strip():
        problems.append(local("city"))
    if not (district or "").strip():
        problems.append(local("district"))
    if not (country or "").strip():
        problems.append(local("country"))
    if additional_no and not _digits(additional_no, 4):
        problems.append(local("additional_no"))
    return problems


def entity_problems(entity):
    """R-4 on the entity's own fields (profile enable)."""
    problems = seller_address_problems(
        vat_number=entity.vat_number, street=entity.street_ar or entity.street_en, building_no=entity.building_no,
        postal_code=entity.postal_code, city=entity.city_ar or entity.city_en,
        district=entity.district_ar or entity.district_en, country=entity.country, additional_no=entity.additional_no)
    if not (entity.legal_name_ar or "").strip():
        problems.append(local("seller_name_ar"))
    return problems


def seller_id(cr_number, unified_number, profile_scheme="", profile_value=""):
    """R-48: exactly one seller ID, first in the order CRN, MOM, MLS, 700, SAG, OTH; ("", "") when none."""
    candidates = {"CRN": cr_number or "", "700": unified_number or ""}
    if profile_scheme in ("MOM", "MLS", "SAG", "OTH") and profile_value:
        candidates[profile_scheme] = profile_value
    for scheme in SELLER_SCHEMES:
        value = (candidates.get(scheme) or "").strip()
        if value:
            return scheme, value
    return "", ""


def _third_decimal(value):
    value = Decimal(value)
    return value != value.quantize(CENT)


def document_problems(data):
    """Local checks before any ICV is used (R-16, R-19, R-31, R-40, R-46, R-48, R-51, BT-27, BR-KSA-63/-67,
    BR-CO-15).

    ``data`` is a :class:`zatca.ubl.InvoiceData`.
    """
    problems = []
    s, b = data.seller, data.buyer
    if data.type_code not in SUPPORTED_TYPES:
        problems.append(local("type_code"))
    problems += seller_address_problems(vat_number=s.vat_number, street=s.street, building_no=s.building_no,
                                        postal_code=s.postal_code, city=s.city, district=s.district,
                                        country=s.country, additional_no=s.additional_no)
    if not s.name.strip():
        problems.append(local("seller_name_ar"))
    if len(s.name) > 127:  # BT-27 (SDK BT-CL-27)
        problems.append(local("seller_name_length"))
    if not (s.id_scheme and s.id_value):
        problems.append(local("seller_id"))
    # R-46: subtype and flags.
    code = data.subtype_code or ""
    if not SUBTYPE_RE.match(code) or code[:2] != ("01" if data.category == "standard" else "02"):
        problems.append(local("subtype"))
    else:
        export, self_billed = code[4] == "1", code[6] == "1"
        if export and self_billed:
            problems.append(local("self_billed_export"))
        if code[:2] == "02" and (export or self_billed):
            problems.append(local("simplified_flags"))
        if export and b.vat_number:
            problems.append(local("export_buyer_vat"))
    if b.vat_number and not vat_number_ok(b.vat_number):
        problems.append(local("buyer_vat"))
    if data.category == "standard":
        if not (b.vat_number or (b.id_scheme and b.id_value)):
            problems.append(local("buyer_missing"))
        if not b.name.strip():
            problems.append(local("buyer_name"))
        if not (b.street.strip() and b.city.strip() and b.country.strip()):
            problems.append(local("buyer_address"))
        if (b.country or "").strip().upper() == "SA":  # BR-KSA-63 (XML v1.2 p.57), BR-KSA-67 (p.58)
            if not ((b.building_no or "").strip() and (b.postal_code or "").strip() and (b.district or "").strip()):
                problems.append(local("buyer_address_sa"))
            elif not _digits(b.postal_code.strip(), 5):
                problems.append(local("buyer_postal_code"))
        if data.type_code == "388" and not data.supply_date:
            problems.append(local("supply_date"))
    # R-19: notes.
    if data.type_code in ("381", "383"):
        if data.billing_reference_pre_go_live:
            problems.append(local("original_pre_go_live"))
        elif not data.billing_reference or not data.billing_reference_entity_ok:
            problems.append(local("billing_reference"))
        elif data.billing_reference_category and data.billing_reference_category != data.category:
            problems.append(local("category_follows"))
        if not (1 <= len(data.reason.strip()) <= 1000):
            problems.append(local("reason"))
        if not data.payment_means_code:
            problems.append(local("payment_means"))
    # R-31: sums, in the document currency.
    line_net = sum((Decimal(line.net) for line in data.lines), Decimal("0"))
    if Decimal(data.total_net) != line_net:
        problems.append(local("header_net"))
    group_vat = Decimal("0")
    for g in data.groups:
        expected = (Decimal(g.taxable) * Decimal(g.rate) / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)
        if Decimal(g.vat) != expected:
            problems.append(local("category_vat"))
        group_vat += Decimal(g.vat)
        if g.category != "S":
            if len(g.exemption_codes) > 1:
                problems.append(local("exemption_reasons"))
            elif not g.exemption_codes or not g.exemption_text:
                problems.append(local("exemption_reason"))
    if Decimal(data.total_vat) != group_vat:
        problems.append(local("total_vat"))
    if Decimal(data.total_gross) != Decimal(data.total_net) + Decimal(data.total_vat):  # BR-CO-15
        problems.append(local("total_gross"))
    for line in data.lines:
        if line.amount_with_vat is not None and Decimal(line.amount_with_vat) != Decimal(line.net) + Decimal(line.vat):
            problems.append(local("line_total"))
            break
    # R-40: SAR VAT total.
    if data.currency != "SAR" and Decimal(data.total_vat) != 0 and Decimal(data.total_vat_sar) == 0:
        problems.append(local("sar_vat"))
    if data.currency == "SAR" and Decimal(data.total_vat_sar) != Decimal(data.total_vat):
        problems.append(local("sar_vat"))
    # R-51: no non-zero third decimal anywhere.
    amounts = [data.total_net, data.total_vat, data.total_gross]
    for line in data.lines:
        amounts += [line.net, line.vat, line.discount, line.unit_price]
    for g in data.groups:
        amounts += [g.taxable, g.vat]
    if data.currency_places > 2 and any(_third_decimal(a) for a in amounts):
        problems.append(local("third_decimal"))
    if data.retention_class == "movable_capital_asset" and not data.acquisition_date:
        problems.append(local("movable_acquisition"))
    seen, unique = set(), []
    for p in problems:
        if p[0] not in seen:
            seen.add(p[0])
            unique.append(p)
    return unique


# --- retention (R-38) and archive names (R-30) ------------------------------------------------------

def _add_years(d, years):
    year = d.year + years
    return date(year, d.month, min(d.day, calendar.monthrange(year, d.month)[1]))


def tax_period_end(day, period):
    """End of the monthly or quarterly VAT period containing ``day`` (``Entity.vat_return_period``)."""
    month = ((day.month - 1) // 3 + 1) * 3 if period == "quarterly" else day.month
    return date(day.year, month, calendar.monthrange(day.year, month)[1])


def retention_until(retention_class, issue_date, period, *, acquisition_date=None, adjustment_years=None):
    """R-38: standard = end of tax period + 6 years; real estate = end of tax period + 15 years (start point an
    assumption, tax sign-off); movable = acquisition + adjustment period + 5 years, never before the standard
    date. ``adjustment_years`` None (not configured, OQ-31) keeps the standard date."""
    end = tax_period_end(issue_date, period)
    standard = _add_years(end, 6)
    if retention_class == "capital_asset_real_estate":
        return _add_years(end, 15)
    if retention_class == "movable_capital_asset" and acquisition_date and adjustment_years is not None:
        return max(standard, _add_years(acquisition_date, int(adjustment_years) + 5))
    return standard


SAFE_NAME = re.compile(r"[^A-Za-z0-9_.\-]")


def archive_file_name(vat_number, issue_date_text, issue_time_text, number):
    """R-30 / AT-59: ``<vat>_<yyyymmdd>_<hhmmss>_<IRN>.xml`` (house convention; DG p.26 names the parts)."""
    irn = SAFE_NAME.sub("-", number)
    return f"{vat_number}_{issue_date_text.replace('-', '')}_{issue_time_text.replace(':', '')[:6]}_{irn}.xml"


def otp_ok(otp):
    """R-7: exactly 6 ASCII digits."""
    return isinstance(otp, str) and len(otp) == 6 and otp.isascii() and otp.isdigit()


SERIAL_RE = re.compile(r"^1-[^|]+\|2-[^|]+\|3-[^|]+\Z")
