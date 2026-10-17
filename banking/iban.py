"""IBAN checks (banking R3). Pure functions: no database, no rights.

ISO 13616: two-letter country code, two check digits, mod-97 of the rearranged number = 1, characters A-Z and
0-9 only. Lengths per country come from the registry extract below **[CC-verify: against the current SWIFT IBAN
registry]**. Saudi IBANs: 24 characters, positions 5-6 the bank identifier, the remaining 18 alphanumeric
**[CC-verify: SA IBAN structure]**. Countries not listed here do not use IBANs for this module (OQ-4): such
accounts carry ``account_number`` instead.
"""
import re

# ISO 13616 registry extract: country -> IBAN length [CC-verify]. Data, not code paths: add a country here when
# an entity banks there (or move to a table if the list grows).
IBAN_LENGTHS = {
    "SA": 24, "AE": 23, "BH": 22, "KW": 30, "QA": 29, "OM": 23, "JO": 30, "EG": 29, "LB": 28, "TR": 26, "PK": 24,
    "GB": 22, "DE": 22, "FR": 27, "NL": 18, "CH": 21, "BE": 16, "ES": 24, "IT": 27, "LU": 20, "IE": 22, "AT": 20,
}

_ALLOWED = re.compile(r"^[A-Z0-9]+$")


def normalise(value):
    """Upper case without spaces (how IBANs are stored, 3.2)."""
    return "".join(str(value or "").split()).upper()


def uses_iban(country):
    return country in IBAN_LENGTHS


def mod97_ok(iban):
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def problems(value, *, country, bank_code=None):
    """Bilingual problems of IBAN ``value`` for an account at a bank in ``country`` with ``bank_code`` (SA)."""
    iban = normalise(value)
    out = []
    if not iban:
        return ["An IBAN is required for an account in this country. / رقم الآيبان مطلوب لحساب في هذه الدولة."]
    if not _ALLOWED.match(iban):
        return ["An IBAN has only letters A-Z and digits. / يتكون رقم الآيبان من الأحرف A-Z والأرقام فقط."]
    if iban[:2] != country:
        out.append(f"The IBAN starts with {iban[:2]}, not the bank's country {country}. / "
                   f"يبدأ رقم الآيبان بـ {iban[:2]} وليس برمز دولة البنك {country}.")
        return out
    length = IBAN_LENGTHS.get(country)
    if length is None:
        return [f"{country} accounts have no IBAN here; enter the account number (OQ-4). / "
                f"حسابات {country} بلا آيبان هنا؛ أدخل رقم الحساب."]
    if len(iban) != length:
        out.append(f"A {country} IBAN has {length} characters; this one has {len(iban)}. / "
                   f"يتكون رقم الآيبان في {country} من {length} خانة؛ وهذا من {len(iban)}.")
        return out
    if not iban[2:4].isdigit() or not mod97_ok(iban):
        out.append("The IBAN check digits are wrong (ISO 13616 mod-97). / خانتا التحقق في رقم الآيبان غير صحيحتين.")
    if country == "SA" and bank_code and iban[4:6] != bank_code:
        out.append(f"Positions 5-6 of this IBAN ({iban[4:6]}) are not the bank's code {bank_code}. / "
                   f"الخانتان 5-6 في رقم الآيبان ({iban[4:6]}) لا تطابقان رمز البنك {bank_code}.")
    return out


def last4(value):
    return normalise(value)[-4:]


def masked(country_or_iban, last_four):
    """``SA•• •••• … 1234`` (R4): never more than the country code and the last four characters."""
    prefix = (country_or_iban or "")[:2] if country_or_iban else "••"
    return f"{prefix}•• •••• … {last_four}" if last_four else ""


_SAUDI_DIGITS = re.compile(r"^SA[0-9]{22}$")


def saudi_problems(value):
    """Bilingual problems of a **Saudi** IBAN for purification money (invoicing R54 amendment D31b): ``SA`` followed
    by 22 digits, with a valid ISO 13616 mod-97 check. Stricter than :func:`problems` for ``SA`` (which allows
    letters in the account part, R3): the purification account and charity recipients need the all-digit form."""
    iban = normalise(value)
    if not iban:
        return ["A Saudi IBAN is required. / رقم آيبان سعودي مطلوب."]
    if not _SAUDI_DIGITS.match(iban):
        return ["A Saudi IBAN is SA followed by 22 digits. / الآيبان السعودي يتكون من SA متبوعة بـ 22 رقمًا."]
    if not mod97_ok(iban) or not 2 <= int(iban[2:4]) <= 98:  # ISO 13616 check digits are 02 to 98
        return ["The IBAN check digits are wrong (ISO 13616 mod-97). / خانتا التحقق في رقم الآيبان غير صحيحتين."]
    return []
