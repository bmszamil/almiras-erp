"""Bilingual messages of the fixed-asset module (fixed-assets.md §7 key messages, R32). [AR-review]

``error(code, **values)`` raises nothing: it returns a :class:`FixedAssetError` with the English and Arabic text joined
by " / " (the format every module uses), whose ``code`` tests and callers read.
"""
from django.core.exceptions import ValidationError

MESSAGES = {
    # §7 key messages
    "no_policy": ("Category {category} has no approved depreciation policy in force.",
                  "لا توجد سياسة استهلاك معتمدة سارية للفئة {category}."),
    "run_changed": ("The run's assets or events changed after it was prepared: discard and re-prepare.",
                    "تغيّرت أصول المسيّر أو أحداثه بعد إعداده: ألغِه وأعد إعداده."),
    "unregistered_capital_lines": (
        "Period {period} has posted bill lines on asset accounts that are not in the register: {lines}.",
        "في الفترة {period} بنود فواتير مرحّلة على حسابات الأصول غير مسجلة في سجل الأصول: {lines}."),
    "account_not_set": ("Category {category} has no {role} account; it is set once the account is approved.",
                        "لم يُحدَّد حساب {role_ar} للفئة {category}؛ يُحدَّد بعد اعتماد الحساب."),
    "vehicle_on_contract": ("Vehicle {code} is on active contract {contract}; it cannot be disposed of.",
                            "السيارة {code} مرتبطة بالعقد الساري {contract}؛ لا يمكن استبعادها."),
    "bai_ajel_asset": ("Lines on 1250 are held for an employee's deferred sale and are not registered.",
                       "البنود على الحساب 1250 محتفظ بها للبيع الآجل للموظف ولا تُسجَّل أصولًا."),
    "opening_asset_changed": ("Asset {legacy_code} was loaded with other values; a re-run does not change it.",
                              "سبق تحميل الأصل {legacy_code} بقيم مختلفة؛ ولا تغيّره إعادة التشغيل."),
    "opening_not_posted": ("The migrated register of {entity} counts only once its opening entry is posted.",
                           "لا يُعتدّ بسجل الأصول المرحّل للكيان {entity} إلا بعد ترحيل القيد الافتتاحي."),
    "financing_cost_capitalised": (
        "Financing cost or deferred profit cannot be part of an asset's cost (Sharia).",
        "لا يجوز أن تكون تكلفة التمويل أو الربح المؤجل جزءًا من تكلفة الأصل (الضوابط الشرعية)."),
    "not_built_oq9": ("Capitalising borrowing costs is not built (OQ-9).",
                      "رسملة تكاليف الاقتراض غير متاحة (السؤال المفتوح 9)."),
    "real_estate_disposal_blocked": (
        "Disposals of real estate wait for the tax adviser's sign-off on VAT and RETT (OQ-10).",
        "استبعاد العقارات بانتظار اعتماد المستشار الضريبي لضريبة القيمة المضافة وضريبة التصرفات العقارية."),
    "vat_use_by_event": ("VAT use changes only through a change-of-VAT-use event.",
                         "لا يتغير استخدام الضريبة إلا من خلال حدث تغيير استخدام الضريبة."),
    "ip_account_class": ("Account 1510 is for investment property categories only.",
                         "الحساب 1510 مخصص لفئات العقارات الاستثمارية فقط."),
    # other refusals
    "amount_decimals": ("{what} {amount} has more than 2 decimals (SAR, CLAUDE.md rule 7).",
                        "المبلغ {amount} ({what_ar}) بأكثر من خانتين عشريتين (الريال)."),
    "amount_positive": ("{what} must be above zero.", "يجب أن يكون {what_ar} أكبر من صفر."),
    "service_only": ("Fixed-asset records change only through fixed_assets.services.",
                     "لا تتغير سجلات الأصول الثابتة إلا من خلال خدمات وحدة الأصول."),
    "never_deleted": ("Fixed-asset records are never deleted (R42); they are kept indefinitely.",
                      "لا تُحذف سجلات الأصول الثابتة أبدًا؛ تُحفظ دون حد زمني."),
    "entity_immutable": ("A record's entity never changes (R30).", "لا يتغير كيان السجل أبدًا."),
    "other_entity": ("{what} belongs to another entity (R1).", "{what_ar} يتبع كيانًا آخر."),
    "not_draft": ("Only a draft can be changed or submitted.", "يمكن تعديل المسودة أو تقديمها فقط."),
    "status": ("This is not possible while the {what} is {status}.", "هذا غير ممكن و{what_ar} في الحالة {status}."),
    "agent_submit": ("AI agents draft only; a person submits, decides, posts or reverses (R35).",
                     "وكلاء الذكاء الاصطناعي يعدّون المسودات فقط؛ يقدّمها ويقررها ويرحّلها ويعكسها شخص."),
    "approval_through_engine": (
        "Fixed-asset approvals are decided through the approval engine inbox only (access R61).",
        "تُقرَّر اعتمادات الأصول الثابتة من خلال صندوق محرك الاعتماد فقط."),
    "category_account": ("Account {code} cannot be the {role} account of a category: {why}.",
                         "لا يكون الحساب {code} حساب {role_ar} لفئة: {why_ar}."),
    "ip_usage": ("Investment-property usage goes with investment-property classes only (§3.3).",
                 "استخدام العقار الاستثماري يكون مع فئات العقار الاستثماري فقط."),
    "line_not_capitalisable": ("Bill line {line} is not a posted line on an asset cost account of {entity}.",
                               "بند الفاتورة {line} ليس بندًا مرحّلًا على حساب تكلفة أصول للكيان {entity}."),
    "line_fully_linked": ("Bill line {line} already feeds its whole posted amount {amount} to the register (R4).",
                          "بند الفاتورة {line} مسجّل بكامل مبلغه المرحّل {amount} في السجل."),
    "line_category": ("Bill line {line} is on account {code}, not the cost account of category {category}.",
                      "بند الفاتورة {line} على الحساب {code} وليس حساب تكلفة الفئة {category}."),
    "active_through_event": ("An active asset takes more cost only through a subsequent-cost event (R2).",
                             "لا يُضاف إلى أصل نشط تكلفة إلا من خلال حدث تكلفة لاحقة."),
    "pending_adjustment": ("{lines} changed the cost of asset {asset} after it was registered; record a cost "
                           "adjustment event before the next run (R4).",
                           "{lines} غيّر تكلفة الأصل {asset} بعد تسجيله؛ سجّل حدث تسوية تكلفة قبل المسيّر التالي."),
    "missing_submission_data": ("Before submission the asset needs: {fields} (R5, R46).",
                                "يحتاج الأصل قبل تقديمه إلى: {fields_ar}."),
    "acquisition_after_available": ("The acquisition date {acquired} is after the available-for-use date {available} "
                                    "(R5).", "تاريخ الاقتناء {acquired} بعد تاريخ الجاهزية للاستخدام {available}."),
    "no_cost": ("The asset has no cost yet: link a posted bill line or post its acquisition first.",
                "لا تكلفة للأصل بعد: اربط بند فاتورة مرحّلًا أو رحّل اقتناءه أولًا."),
    "policy_values": ("{why}", "{why_ar}"),
    "policy_overlap": ("Category {category} already has an approved policy effective {effective}.",
                       "للفئة {category} سياسة معتمدة سارية من {effective}."),
    "run_exists": ("{entity} already has a {status} depreciation run for {period}.",
                   "لدى {entity} مسيّر استهلاك ({status}) للفترة {period}."),
    "earlier_run_missing": ("Period {period} has depreciation to post and no posted run; post it first.",
                            "للفترة {period} استهلاك لم يُرحَّل بمسيّر؛ رحّله أولًا."),
    "nothing_to_depreciate": ("Nothing to depreciate in {entity} for {period}.",
                              "لا يوجد ما يُستهلك في {entity} للفترة {period}."),
    "period_closed": ("Period {period} of {entity} is closed.", "الفترة {period} للكيان {entity} مقفلة."),
    "no_period": ("{entity} has no fiscal period for {on}.", "لا توجد فترة مالية للكيان {entity} تشمل {on}."),
    "kind_refused": ("{kind} events are not built: {why}.", "أحداث {kind_ar} غير متاحة: {why_ar}."),
    "kind_slice_2": ("{kind} events come with slice 2 (OQ-14).", "أحداث {kind_ar} تأتي مع المرحلة الثانية."),
    "event_values": ("{why}", "{why_ar}"),
    "not_ip": ("Fair values are recorded for investment property only (R48).",
               "تُسجَّل القيم العادلة للعقارات الاستثمارية فقط."),
    "nothing_to_reverse": ("No impairment can be reversed: the carrying amount is already at the amount it would have "
                           "been without impairment (R19).",
                           "لا يوجد هبوط قيمة قابل للعكس: القيمة الدفترية تساوي ما كانت ستكون عليه دون هبوط."),
    "not_impaired": ("The recoverable amount {recoverable} is not below the carrying amount {carrying}.",
                     "المبلغ القابل للاسترداد {recoverable} ليس أقل من القيمة الدفترية {carrying}."),
    "reason_required": ("A reason in Arabic is required.", "السبب بالعربية مطلوب."),
    "not_posted": ("Only a posted run or event is reversed.", "يُعكس المسيّر أو الحدث المرحّل فقط."),
    "reversal_order": ("A later posted run or event of this asset depends on it; reverse that first.",
                       "يعتمد عليه مسيّر أو حدث مرحّل لاحق لهذا الأصل؛ اعكسه أولًا."),
    "cwip_amount": ("The transfer {amount} is above the CWIP cost {cost} (R44).",
                    "مبلغ التحويل {amount} أعلى من تكلفة المشروعات تحت التنفيذ {cost}."),
    "cwip_target": ("The target must be a draft or active asset of a depreciable category of the same entity (R44).",
                    "يجب أن يكون الهدف أصلًا مسودة أو نشطًا من فئة قابلة للاستهلاك في الكيان نفسه."),
    "opening_posted": ("The migration opening entry of {entity} is posted; its register was tied out to it and is not "
                       "loaded again (migration R25).",
                       "القيد الافتتاحي للترحيل للكيان {entity} مرحّل؛ وقد طوبق سجل أصوله معه ولا يُحمَّل مرة أخرى."),
    "opening_category": ("No category of {entity} has class {asset_class} and cost account {code} (R40); set it up "
                         "first.", "لا توجد فئة للكيان {entity} بالفئة {asset_class} وحساب التكلفة {code}؛ أعدّها أولًا."),
    "not_visible": ("Not visible: you have no fixed-asset view right in the asset's origin entity (R29).",
                    "غير ظاهر: ليست لديك صلاحية عرض الأصول في كيان المصدر."),
    "setup_missing": ("{entity} has no fixed-asset settings; run the setup first (R31).",
                      "لا توجد إعدادات أصول ثابتة للكيان {entity}؛ شغّل الإعداد أولًا."),
}

ROLE_NAMES = {
    "cost": ("cost", "التكلفة"),
    "accumulated depreciation": ("accumulated depreciation", "مجمع الاستهلاك"),
    "depreciation expense": ("depreciation expense", "مصروف الاستهلاك"),
    "accumulated impairment": ("accumulated impairment", "مجمع هبوط القيمة"),
    "impairment expense": ("impairment expense", "خسائر هبوط القيمة"),
    "disposal": ("disposal gain or loss", "أرباح وخسائر الاستبعاد"),
}


class FixedAssetError(ValidationError):
    """A refused fixed-asset action; ``messages`` are "English / العربية" texts and ``code`` names the rule."""

    def __init__(self, message, code=None):
        super().__init__(message, code=code)
        self.code = code


def text(code, /, **values):
    """(English, Arabic) of ``code`` with ``values``; ``<name>_ar`` falls back to ``<name>``."""
    en, ar = MESSAGES[code]
    filled = dict(values)
    for key, value in values.items():
        filled.setdefault(f"{key}_ar", value)
    return en.format(**filled), ar.format(**filled)


def bi(code, /, **values):
    en, ar = text(code, **values)
    return f"{en} / {ar}"


def error(code, /, **values):
    return FixedAssetError(bi(code, **values), code=code)


def problems_error(problems, code):
    exc = FixedAssetError(list(problems), code=code)
    exc.code = code
    return exc
