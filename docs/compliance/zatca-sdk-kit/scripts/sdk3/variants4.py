"""Make fake variants of the kit's unsigned std-388 for the SDK R4.0.0 run of 2026-10-18: B2G (KSA-40
`GOV`, BT-7 tax point date), the R3.4.8 9-position KSA-2, and price-level allowance/charge (BG-29).
Usage: python3 -I variants4.py OUTDIR kit/inv/std-388.xml
Outputs (unsigned; sign with the SDK -sign):
  std-388-gov-notpd      buyer CompanyLegalForm GOV, no TaxPointDate          (R4: BR-KSA-92 warning expected)
  std-388-gov-tpd        GOV + TaxPointDate                                    (R4: clean)
  std-388-llc-tpd        CompanyLegalForm LLC + TaxPointDate                   (R4: BR-KSA-91 warning expected)
  std-388-ksa2-9pos-g    KSA-2 = 010000001 (R3.4.8 B2G position G)
  std-388-price-charge0  Price/AllowanceCharge ChargeIndicator true, Amount 0.00 (as the R4.0.0 samples)
  std-388-price-charge1  ChargeIndicator true, Amount 1.00, BaseAmount 101.00 (net price 100.00)
  std-388-price-allow1   ChargeIndicator false, Amount 1.00, BaseAmount 101.00 (EN 16931 price discount)
"""
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
std = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
BUYER_RN = "<cbc:RegistrationName>Fake Buyer Co</cbc:RegistrationName>"
PRICE = '<cbc:PriceAmount currencyID="SAR">100.00</cbc:PriceAmount>'
assert BUYER_RN in std and PRICE in std and "<cbc:DocumentCurrencyCode>" in std


def w(name, text, base):
    assert text != base, name
    (out / f"{name}.xml").write_text(text.replace("TEST-std-388", f"TEST-{name}"),
                                     encoding="utf-8")


def legal_form(text, form):
    return text.replace(BUYER_RN, BUYER_RN + f"<cbc:CompanyLegalForm>{form}</cbc:CompanyLegalForm>", 1)


def tax_point(text):
    return text.replace("<cbc:DocumentCurrencyCode>", "<cbc:TaxPointDate>2022-08-16</cbc:TaxPointDate>\n  <cbc:DocumentCurrencyCode>", 1)


def price(text, indicator, amount, base_amount=None):
    ac = (f"<cac:AllowanceCharge><cbc:ChargeIndicator>{indicator}</cbc:ChargeIndicator>"
          f"<cbc:AllowanceChargeReason>discount</cbc:AllowanceChargeReason>"
          f'<cbc:Amount currencyID="SAR">{amount}</cbc:Amount>'
          + (f'<cbc:BaseAmount currencyID="SAR">{base_amount}</cbc:BaseAmount>' if base_amount else "")
          + "</cac:AllowanceCharge>")
    return text.replace(PRICE, PRICE + ac, 1)


w("std-388-gov-notpd", legal_form(std, "GOV"), std)
w("std-388-gov-tpd", tax_point(legal_form(std, "GOV")), std)
w("std-388-llc-tpd", tax_point(legal_form(std, "LLC")), std)
w("std-388-ksa2-9pos-g", std.replace('name="0100000"', 'name="010000001"', 1), std)
w("std-388-price-charge0", price(std, "true", "0.00"), std)
w("std-388-price-charge1", price(std, "true", "1.00", "101.00"), std)
w("std-388-price-allow1", price(std, "false", "1.00", "101.00"), std)
print("ok", sorted(p.name for p in out.iterdir()))
