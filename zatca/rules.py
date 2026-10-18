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
SELLER_NAME_MAX = 1000  # BT-27: BR-KSA-F-06-C10 (SDK R3.4.8); the 127-character BT-CL-27 is gone


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
    """Local checks before any ICV is used (R-16, R-19, R-31, R-39, R-40, R-46, R-48, R-51, BT-27,
    BR-KSA-63/-67, BR-CO-15).

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
    if len(s.name) > SELLER_NAME_MAX:  # BT-27, BR-KSA-F-06-C10 (characters, not bytes)
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
    # R-39: prepayment deductions are checked, but not built until the invoicing prepayment flow exists.
    if data.prepayments or Decimal(data.prepaid_amount or 0) != 0:
        problems.append(local("prepayment_not_built"))
        problems += prepayment_problems(data)
        for p in data.prepayments:
            amounts += [a for a in (p.taxable, p.vat) if a is not None]
        amounts.append(data.prepaid_amount)
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


def _round2(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def prepayment_problems(data):
    """R-39: the prepayment deduction lines of a final 388 and its BT-113, per SDK R3.4.8's KSA rules
    (findings §3.4). Validators only: the module does not build such documents yet.

    BR-KSA-73 (BT-113 given -> deduction line(s) with KSA-26, -28, -29, -30), BR-KSA-74 (KSA-30 = 386),
    BR-KSA-75 (KSA-31 to -34 present), BR-KSA-79 (KSA-32 = round(KSA-31 x KSA-34 / 100, 2); house rule: exact,
    the SDK allows +-0.01 as a warning; 0 for Z/E/O), BR-KSA-80 (BT-113 = sum(KSA-31 + KSA-32); also no
    deduction lines without BT-113), BR-KSA-82 (line price, allowance, charge, net, line VAT and line amount
    with VAT all 0).
    """
    problems = []
    prepaid = Decimal(data.prepaid_amount or 0)
    lines = list(data.prepayments)
    if prepaid != 0 and not lines:
        problems.append(local("prepayment_reference"))
    total = Decimal("0")
    for p in lines:
        if not all((str(v).strip() if v is not None else "") for v in (p.number, p.issue_date, p.issue_time,
                                                                         p.type_code)):
            problems.append(local("prepayment_reference"))  # BR-KSA-73
        if (p.type_code or "").strip() != "386":
            problems.append(local("prepayment_type"))  # BR-KSA-74
        if p.taxable is None or p.vat is None or not (p.category or "").strip() or p.rate is None:
            problems.append(local("prepayment_vat_fields"))  # BR-KSA-75
            continue
        rate = Decimal(p.rate) if p.category == "S" else Decimal("0")
        if p.category != "S" and Decimal(p.rate) != 0:
            problems.append(local("prepayment_vat"))
        elif Decimal(p.vat) != _round2(Decimal(p.taxable) * rate / HUNDRED):
            problems.append(local("prepayment_vat"))  # BR-KSA-79
        total += Decimal(p.taxable) + Decimal(p.vat)
        if any(Decimal(a) != 0 for a in (p.price, p.net, p.allowance, p.charge, p.line_vat,
                                         p.line_amount_with_vat)):
            problems.append(local("prepayment_line_zero"))  # BR-KSA-82
    if lines and (prepaid == 0 or prepaid != total):
        problems.append(local("prepaid_amount"))  # BR-KSA-80
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


# CSR serial ``1-...|2-...|3-...`` (SFIS p.16); no "=" anywhere (SDK R3.4.8 / R4.0.0, findings 2026-10-17 §7.3).
SERIAL_RE = re.compile(r"^1-[^|=]+\|2-[^|=]+\|3-[^|=]+\Z")

# CSR values: the SDK refuses any of these in CN, OU, O, location and industry ("specialCharacterRegex"
# ``[!@#$%&*_<]``, findings 2026-10-17 §7.3). Arabic text is accepted.
CSR_SPECIAL_CHARS = "!@#$%&*_<"
CSR_SPECIAL_RE = re.compile(r"[!@#$%&*_<]")
CSR_FIELDS = (  # (key, English label, Arabic label) in the order of the subject / SAN
    ("common_name", "common name (CN, from the EGS code)", "الاسم الشائع (من رمز وحدة الفوترة)"),
    ("org_unit", "branch name (OU)", "اسم الفرع"),
    ("org_name", "entity legal name (O)", "الاسم القانوني للكيان"),
    ("location", "location", "الموقع"),
    ("industry", "industry", "النشاط"),
)
TIN_RE = re.compile(r"^[0-9]{10}\Z")


def is_vat_group(vat_number):
    """The organization identifier's 11th digit is 1 (SFIS Table 1 p.16; SDK CsrGenerationService)."""
    return len(vat_number or "") == 15 and vat_number[10] == "1"


def csr_value_problems(*, common_name, org_unit, org_name, location, industry, vat_number):
    """Refusals ZATCA's CSR checks would give, checked before ``crypto.build_csr`` (findings 2026-10-17 §3.6, §7.3):
    none of ``! @ # $ % & * _ <`` in CN, OU, O, location and industry; for a VAT group (11th digit 1) OU is the
    member's 10-digit TIN. One message per field, naming it in English and Arabic."""
    values = {"common_name": common_name, "org_unit": org_unit, "org_name": org_name, "location": location,
              "industry": industry}
    problems = []
    for key, en, ar in CSR_FIELDS:
        found = "".join(dict.fromkeys(c for c in (values[key] or "") if c in CSR_SPECIAL_CHARS))
        if found:
            code, msg_en, msg_ar = local("csr_chars", field_en=en, field_ar=ar, chars=" ".join(found))
            problems.append((f"{code}_{key}", msg_en, msg_ar))
    if is_vat_group(vat_number) and not TIN_RE.match(org_unit or ""):
        problems.append(local("vat_group_ou"))
    return problems
