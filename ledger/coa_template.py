"""Group chart-of-accounts template (IFRS for SMEs, Saudi holding company).

Draft for review by the Almiras accountant; to be reconciled with the Daftra chart
before opening balances are loaded. Format: (code, English, Arabic, type, flags).
Header accounts end in "000" and are not postable.

Flags: header, intercompany, prohibited (Sharia), purify, monetary (included in FX
revaluation, ledger-fx.md R21), report_only (not postable; used by reports such as
currency translation, never posted to in an entity's own ledger), parent (code of the parent account when it is
not the type's header, e.g. bank sub-accounts under 1100).
"""

A, L, E, I, X = "asset", "liability", "equity", "income", "expense"

TEMPLATE = [
    ("1000", "Assets", "الأصول", A, {"header": True}),
    ("1100", "Cash and bank", "النقد وما في حكمه", A, {"monetary": True}),
    ("1110", "Petty cash", "العهد النقدية", A, {"monetary": True}),
    # PROPOSED (invoicing.md R54 amendment D31b, D29): the designated purification bank account (Saudi IBAN, SAR).
    # A sub-account of 1100 so banking R5 accepts it as a bank GL account; only the purification account uses it.
    ("1140", "Bank – purification account", "البنك – حساب التطهير", A, {"monetary": True, "parent": "1100"}),
    ("1200", "Trade receivables", "الذمم المدينة التجارية", A, {"monetary": True}),
    ("1210", "Due from group entities", "مستحق من شركات المجموعة", A, {"intercompany": True, "monetary": True}),
    ("1220", "Loans to group entities", "قروض لشركات المجموعة", A, {"intercompany": True, "monetary": True}),
    ("1300", "Sharia-compliant deposits (e.g. murabaha, wakala)", "ودائع متوافقة مع الشريعة", A, {"monetary": True}),
    ("1310", "VAT input", "ضريبة القيمة المضافة - مدخلات", A, {"monetary": True}),
    ("1320", "Prepayments", "مصروفات مدفوعة مقدماً", A, {}),
    ("1400", "Investments in subsidiaries", "استثمارات في شركات تابعة", A, {}),
    ("1410", "Investments in associates (equity method)", "استثمارات في شركات زميلة", A, {}),
    ("1420", "Other equity investments", "استثمارات حقوق ملكية أخرى", A, {}),
    ("1500", "Property, plant and equipment", "الممتلكات والآلات والمعدات", A, {}),
    ("1510", "Investment property (rental assets)", "عقارات استثمارية", A, {}),
    ("1520", "Vehicles", "السيارات", A, {}),
    ("1590", "Accumulated depreciation", "مجمع الاستهلاك", A, {}),
    ("1600", "Intangible assets and IP", "الأصول غير الملموسة والملكية الفكرية", A, {}),
    ("2000", "Liabilities", "الالتزامات", L, {"header": True}),
    ("2100", "Trade payables", "الذمم الدائنة التجارية", L, {"monetary": True}),
    ("2110", "Due to group entities", "مستحق لشركات المجموعة", L, {"intercompany": True, "monetary": True}),
    ("2200", "VAT output", "ضريبة القيمة المضافة - مخرجات", L, {"monetary": True}),
    ("2210", "Zakat payable", "الزكاة المستحقة", L, {"monetary": True}),
    # PROPOSED (invoicing.md §5, D14, D18, D29): accountant / CFO to review; WHT on payments to non-residents (E20 to E22).
    ("2220", "Withholding tax payable", "ضريبة الاستقطاع المستحقة", L, {"monetary": True}),
    ("2300", "Accrued expenses", "مصروفات مستحقة", L, {"monetary": True}),
    ("2400", "Islamic financing (murabaha, tawarruq)", "تمويل إسلامي", L, {"monetary": True}),
    ("2410", "Conventional interest-bearing loans", "قروض تقليدية بفائدة", L, {"prohibited": True}),
    ("2500", "End-of-service provision", "مخصص مكافأة نهاية الخدمة", L, {"monetary": True}),
    ("2600", "Charity payable (purification)", "مبالغ مستحقة للتطهير", L, {"monetary": True}),
    ("3000", "Equity", "حقوق الملكية", E, {"header": True}),
    ("3100", "Share capital", "رأس المال", E, {}),
    ("3200", "Statutory reserve", "الاحتياطي النظامي", E, {}),
    ("3300", "Retained earnings", "الأرباح المبقاة", E, {}),
    # PROPOSED (ledger-fx.md 5.0, OQ-6): FX accounts 3400, 4700, 4710, 5750, 5760, 5770 and the
    # "monetary" flag list above are pending the accountant's approval.
    ("3400", "Foreign currency translation reserve", "احتياطي ترجمة العملات الأجنبية", E, {"report_only": True}),
    ("3900", "Opening balance equity (migration)", "حقوق ملكية الأرصدة الافتتاحية", E, {}),
    ("4000", "Income", "الإيرادات", I, {"header": True}),
    ("4100", "Rental income", "إيرادات الإيجار", I, {}),
    ("4110", "Intercompany rental income", "إيرادات إيجار من شركات المجموعة", I, {}),
    # PROPOSED (invoicing.md §5, D16, D18): accountant to review before this merges.
    ("4120", "Residential rental income (VAT-exempt)", "إيرادات الإيجار السكني (معفاة)", I, {}),
    ("4200", "Secondment and consulting income", "إيرادات الإعارة والاستشارات", I, {}),
    ("4210", "Intercompany service income", "إيرادات خدمات من شركات المجموعة", I, {}),
    ("4220", "Management fee income", "إيرادات رسوم الإدارة", I, {}),  # PROPOSED (invoicing D16, D18)
    ("4230", "Service charge income", "إيرادات رسوم الخدمات المشتركة", I, {}),  # PROPOSED (invoicing D16, D18)
    ("4240", "Service fee income", "إيرادات أتعاب الخدمات", I, {}),  # PROPOSED (invoicing D16, D18)
    ("4300", "IP and licence income", "إيرادات الملكية الفكرية والتراخيص", I, {}),
    ("4310", "Intercompany IP and licence income", "إيرادات ملكية فكرية من شركات المجموعة", I, {}),  # PROPOSED (E2, D18)
    ("4400", "Dividend income", "إيرادات توزيعات الأرباح", I, {}),
    ("4500", "Share of profit of associates", "الحصة من أرباح الشركات الزميلة", I, {}),
    ("4600", "Islamic deposit profit", "أرباح الودائع الإسلامية", I, {}),
    ("4700", "Foreign exchange gain – realised", "أرباح فروقات العملة المحققة", I, {}),  # PROPOSED (OQ-6)
    ("4710", "Foreign exchange gain – unrealised", "أرباح فروقات العملة غير المحققة", I, {}),  # PROPOSED (OQ-6)
    ("4900", "Non-compliant income (to purify)", "إيرادات غير متوافقة (للتطهير)", I, {"purify": True}),
    ("5000", "Expenses", "المصروفات", X, {"header": True}),
    ("5100", "Salaries and benefits", "الرواتب والمزايا", X, {}),
    ("5110", "GOSI", "التأمينات الاجتماعية", X, {}),
    ("5200", "Rent", "الإيجار", X, {}),
    ("5300", "Professional fees", "الأتعاب المهنية", X, {}),
    ("5400", "Depreciation", "الاستهلاك", X, {}),
    ("5500", "Repairs and maintenance", "الإصلاح والصيانة", X, {}),
    ("5510", "Vehicle fuel and running costs", "وقود ومصروفات السيارات", X, {}),
    ("5600", "Islamic financing cost", "تكلفة التمويل الإسلامي", X, {}),
    ("5610", "Conventional interest expense", "مصروف فوائد تقليدية", X, {"prohibited": True}),
    ("5700", "Bank charges", "رسوم بنكية", X, {}),
    ("5750", "Foreign exchange loss – realised", "خسائر فروقات العملة المحققة", X, {}),  # PROPOSED (OQ-6)
    ("5760", "Foreign exchange loss – unrealised", "خسائر فروقات العملة غير المحققة", X, {}),  # PROPOSED (OQ-6)
    ("5770", "Currency rounding differences", "فروقات تقريب العملات", X, {}),  # PROPOSED (OQ-6); may carry credits
    ("5800", "Zakat", "الزكاة", X, {}),
    ("5900", "General and administrative", "مصروفات عمومية وإدارية", X, {}),
    # PROPOSED (invoicing.md §5, E21, D18, D29): WHT the entity bears when it grosses up a payment (R63).
    ("5920", "Withholding tax borne (gross-up)", "ضريبة استقطاع محملة على الشركة", X, {}),
]
