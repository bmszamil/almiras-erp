"""ISO 4217 currency seed (ledger-fx.md 3.1). Global reference data, no entity.

Applied by migration ``core.0006_currency``. Arabic names are for review by the
accountant [AR-review]. Only currencies with at most 3 minor units are loaded
(D5, R40); CNH is not ISO 4217 and is not loaded (OQ-12).
To add or change a currency, add a new data migration; never edit V1 after
it has been applied anywhere.
"""

# code: (numeric_code, name_en, name_ar, minor_units, symbol)
CURRENCY_SEED_V1 = {
    "SAR": ("682", "Saudi riyal", "ريال سعودي", 2, "SAR"),
    "USD": ("840", "US dollar", "دولار أمريكي", 2, "$"),
    "EUR": ("978", "Euro", "يورو", 2, "€"),
    "GBP": ("826", "Pound sterling", "جنيه إسترليني", 2, "£"),
    "CNY": ("156", "Chinese yuan (renminbi)", "يوان صيني (رنمينبي)", 2, "¥"),
    "EGP": ("818", "Egyptian pound", "جنيه مصري", 2, "E£"),
    "AED": ("784", "UAE dirham", "درهم إماراتي", 2, "AED"),
    "KWD": ("414", "Kuwaiti dinar", "دينار كويتي", 3, "KD"),
    "BHD": ("048", "Bahraini dinar", "دينار بحريني", 3, "BD"),
    "OMR": ("512", "Omani rial", "ريال عماني", 3, "OMR"),
    "QAR": ("634", "Qatari riyal", "ريال قطري", 2, "QR"),
    "JOD": ("400", "Jordanian dinar", "دينار أردني", 3, "JD"),
    "JPY": ("392", "Japanese yen", "ين ياباني", 0, "¥"),
    "CHF": ("756", "Swiss franc", "فرنك سويسري", 2, "CHF"),
    "INR": ("356", "Indian rupee", "روبية هندية", 2, "₹"),
    "PKR": ("586", "Pakistani rupee", "روبية باكستانية", 2, "Rs"),
    "TRY": ("949", "Turkish lira", "ليرة تركية", 2, "₺"),
}


def apply_currency_seed(Currency, seed=CURRENCY_SEED_V1):
    """Create the seeded currencies if missing. Idempotent; takes the model so migrations pass a historical one.

    Existing rows are left as they are (an operator may have deactivated one).
    """
    for code, (numeric, en, ar, minor, symbol) in seed.items():
        Currency.objects.get_or_create(
            code=code,
            defaults=dict(numeric_code=numeric, name_en=en, name_ar=ar, minor_units=minor, symbol=symbol, is_active=True),
        )
