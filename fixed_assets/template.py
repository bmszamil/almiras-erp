"""Group template of asset categories and the asset account codes (fixed-assets.md §3.1, §5, R9, R31, R45).

Data, not code paths: ``setup_entity`` copies these rows into an entity's categories with the accounts its chart
has. An account the chart does not have yet (a proposed code awaiting the CFO, OQ-5, D7) stays empty on the category
(R45); a category whose **cost** account is missing is not created until the account exists (reported by the setup).
Nothing falls back to another account.
"""

# Accounts in the chart template (ledger/coa_template.py, D1).
TEMPLATE_COST_CODES = ("1500", "1510", "1520")
TEMPLATE_ACCUMULATED_CODES = ("1590",)
# Proposed, CFO to approve (§5, OQ-5). Restricted now (R9): they exist in no real chart until approved, so restricting
# them changes nothing until then and needs no code change when they are added.
PROPOSED_COST_CODES = ("1530", "1540", "1550", "1560", "1570")
PROPOSED_ACCUMULATED_CODES = ("1591", "1595", "1596")
COST_CODES = TEMPLATE_COST_CODES + PROPOSED_COST_CODES
ACCUMULATED_CODES = TEMPLATE_ACCUMULATED_CODES + PROPOSED_ACCUMULATED_CODES
RESTRICTED_CODES = COST_CODES + ACCUMULATED_CODES  # R9: posted only by fixed-asset documents, bills and the opening
IP_COST_CODES = ("1510",)  # R49: 1510 for investment property only (or an approved IP sub-account)
BAI_AJEL_ASSET = "1250"  # R6: never a category account
FINANCING = "2400"  # FA8: Islamic financing (principal at the cash cost price)
DEPRECIATION = "5400"
IMPAIRMENT_EXPENSE = "5420"  # proposed
DISPOSAL = "4800"  # proposed

# code, name_en, name_ar, asset_class, cost, accumulated depreciation, accumulated impairment, rental_fleet
CATEGORY_TEMPLATE = (
    ("LAND", "Land (own use)", "الأراضي (للاستخدام الخاص)", "land", "1560", None, "1595", False),
    ("BLDG", "Buildings (own use)", "المباني (للاستخدام الخاص)", "building", "1570", "1590", "1595", False),
    ("IPL", "Investment property: land", "عقارات استثمارية: أراضٍ", "ip_land", "1510", None, "1596", False),
    ("IPB", "Investment property: buildings", "عقارات استثمارية: مبانٍ", "ip_building", "1510", "1591", "1596", False),
    ("VEH", "Vehicles (own use)", "السيارات (للاستخدام الخاص)", "vehicle", "1520", "1590", "1595", False),
    ("VEHR", "Vehicles let on rental contracts", "السيارات المؤجرة بعقود", "vehicle", "1520", "1590", "1595", True),
    ("FURN", "Furniture, fixtures and office equipment", "الأثاث والتجهيزات والمعدات المكتبية", "furniture_office",
     "1530", "1590", "1595", False),
    ("IT", "Computer and IT equipment", "أجهزة الحاسب وتقنية المعلومات", "it_equipment", "1540", "1590", "1595", False),
    ("MACH", "Machinery and equipment", "الآلات والمعدات", "machinery", "1500", "1590", "1595", False),
    ("LHI", "Leasehold improvements", "تحسينات على عقارات مستأجرة", "leasehold_improvement", "1500", "1590", "1595",
     False),
    ("CWIP", "Capital work in progress", "مشروعات تحت التنفيذ", "cwip", "1550", None, None, False),
    ("OTHER", "Other property, plant and equipment", "ممتلكات وآلات ومعدات أخرى", "other", "1500", "1590", "1595", False),
)

# Proposed accounts of §5 (OQ-5): fixture data for tests and dry runs only. The chart template is unchanged until
# the CFO approves them (D7). (code, name_en, name_ar, type)
PROPOSED_ACCOUNTS = (
    ("1530", "Furniture, fixtures and office equipment", "الأثاث والتجهيزات والمعدات المكتبية", "asset"),
    ("1540", "Computer and IT equipment", "أجهزة الحاسب وتقنية المعلومات", "asset"),
    ("1550", "Capital work in progress", "مشروعات تحت التنفيذ", "asset"),
    ("1560", "Land (own use)", "الأراضي (للاستخدام الخاص)", "asset"),
    ("1570", "Buildings (own use)", "المباني (للاستخدام الخاص)", "asset"),
    ("1591", "Accumulated depreciation – investment property", "مجمع استهلاك العقارات الاستثمارية", "asset"),
    ("1595", "Accumulated impairment – property, plant and equipment",
     "مجمع خسائر هبوط قيمة الممتلكات والآلات والمعدات", "asset"),
    ("1596", "Accumulated impairment – investment property", "مجمع خسائر هبوط قيمة العقارات الاستثمارية", "asset"),
    ("4800", "Net gain (loss) on disposal of fixed assets", "صافي أرباح (خسائر) استبعاد الأصول الثابتة", "income"),
    ("5420", "Impairment losses (reversals) on fixed assets", "خسائر (عكس) هبوط قيمة الأصول الثابتة", "expense"),
)
