"""Reference data of the VAT slice (tax-returns.md §3.2 box table, §3.1 statutory rates). Loaded by migration 0002.

Every box number and label below is **[verify]** against the current ZATCA VAT return form: the ZATCA VAT return
filing guide was not downloaded (OQ-2), so the rows are seeded unverified and every return shows "unverified"
(R6) until a person verifies them (``tax.services.verify_box_definition``). Arabic labels are [AR-review].
Only box 9 = imported services under reverse charge is confirmed (PSG §4.6 p.20; FAQ 1 p.59).
"""
from datetime import date

# box_no: (label_en, label_ar, side, has_amount, has_adjustment, has_vat, mapping_key, verify_note)
BOXES = {
    1: ("Standard rated sales", "المبيعات الخاضعة للنسبة الأساسية", "output", True, True, True, "R18",
        "[verify] label; whether 5% notes on pre-July-2020 invoices have their own row"),
    2: ("Special cases: private healthcare / private education to citizens",
        "الحالات الخاصة: الخدمات الصحية والتعليمية الخاصة للمواطنين", "output", True, True, True, "R19",
        "[verify] label and reasons VATEX-SA-HEA / VATEX-SA-EDU"),
    3: ("Zero rated domestic sales", "المبيعات المحلية الخاضعة للنسبة الصفرية", "output", True, True, True, "R19",
        "[verify] label and reasons"),
    4: ("Exports", "الصادرات", "output", True, True, True, "R19", "[verify] label; reasons VATEX-SA-32 / -33"),
    5: ("Exempt sales", "المبيعات المعفاة", "output", True, True, True, "R19", "[verify] label"),
    6: ("Total sales", "إجمالي المبيعات", "summary", True, True, True, "R25", "[verify] label"),
    7: ("Standard rated domestic purchases", "المشتريات المحلية الخاضعة للنسبة الأساسية", "input", True, True, True,
        "R21", "[verify] label; non-recoverable VAT and unregistered vendors; Art. 51 true-up placement (R55)"),
    8: ("Imports subject to VAT paid at customs", "الاستيرادات الخاضعة للضريبة المدفوعة في الجمارك", "input", True,
        True, True, "R22", "[verify] label and VAT base"),
    9: ("Imports subject to VAT accounted for through reverse charge",
        "الاستيرادات الخاضعة للضريبة وفق آلية الاحتساب العكسي", "input", True, True, True, "R23",
        "number confirmed (PSG §4.6); [verify] how the output side shows and goods supplied in KSA"),
    10: ("Zero rated purchases", "المشتريات الخاضعة للنسبة الصفرية", "input", True, True, True, "R24",
         "[verify] label"),
    11: ("Exempt purchases", "المشتريات المعفاة", "input", True, True, True, "R24", "[verify] label"),
    12: ("Total purchases", "إجمالي المشتريات", "summary", True, True, True, "R25", "[verify] label"),
    13: ("Total VAT due for current period", "إجمالي ضريبة القيمة المضافة المستحقة عن الفترة الحالية", "summary",
         False, False, True, "R25", "[verify] label and formula"),
    14: ("Corrections from previous periods", "تصحيحات من الفترات السابقة", "summary", False, False, True, "R27",
         "[verify] label and the form's limit (OQ-13)"),
    15: ("VAT credit carried forward from previous period(s)", "الرصيد الدائن المرحل من الفترات السابقة", "summary",
         False, False, True, "R25", "[verify] label"),
    16: ("Net VAT due (or claim)", "صافي الضريبة المستحقة (أو المطالب باستردادها)", "summary", False, False, True,
         "R25", "[verify] label and formula"),
}
BOX_LABELS = {n: (row[0], row[1]) for n, row in BOXES.items()}
BOXES_VALID_FROM = date(2018, 1, 1)

# code: (rate %, valid_from, citation). Display only (R13 estimates); seeded without a verifying person, so shown as
# "unverified" (R6) until someone holding tax.maintain_rates verifies the row. The compliance check of 2026-10-18
# found the 5% per month met against PSG §14 (C16).
STATUTORY_RATES = {
    "vat_late_payment_monthly": ("5.0000", date(2018, 1, 1), "PSG §14 p.54 (VAT Law Art. 43)"),
}
# Added by migration 0005 (C17): late filing 5% to 25% of the tax that should have been reported; an incorrect or
# amended return 25% of the difference (ZATCA may raise it to 50% or reduce it to 0%).
STATUTORY_RATES_0005 = {
    "vat_late_filing_min": ("5.0000", date(2018, 1, 1), "PSG §14 p.54 (VAT Law Art. 41)"),
    "vat_late_filing_max": ("25.0000", date(2018, 1, 1), "PSG §14 p.54 (VAT Law Art. 41)"),
    "vat_incorrect_return": ("25.0000", date(2018, 1, 1), "PSG §14 p.54-55 (VAT Law Art. 42)"),
}
