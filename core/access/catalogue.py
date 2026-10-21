"""Permission catalogue (access.md 3.6). Code, not data: flags cannot be edited in admin.

Flags:
- ``A``  approval: creator / submitter may not approve (R14)
- ``S!`` segregation that cannot be switched off (R15)
- ``DC`` dual control (R19)
- ``G``  group-level only (only group-scope roles may hold it)
- ``Ag`` an agent role may hold it (R12)
- ``P``  a grant for it authorises a ledger posting (R23)
- ``I``  also checked on an inactive entity, but only when the caller passes ``require(..., reactivation=True)``;
         that caller then allows only reactivation (R38). ``require()`` on its own does not limit what an
         I permission is used for, so without ``reactivation=True`` an inactive entity refuses it like any
         other non-view permission (``inactive_entity``).
- ``NSA`` never self-approved (access §10, R80): not even the entity's top authority may approve a document of this
         permission that they created, submitted, modified or had drafted for them. A code flag like ``S!``; it
         cannot be changed in admin. ``S!``, ``DC`` and ``G`` imply it (:attr:`Perm.never_self_approved`).
``system`` is set for "Sys:x" permissions: only system user ``x`` may hold them (R13).

Arabic labels are marked for review [AR-review] like the role names in the spec.
"""
from dataclasses import dataclass, field

APPROVAL = "A"
SEGREGATED = "S!"
DUAL_CONTROL = "DC"
GROUP_ONLY = "G"
AGENT_OK = "Ag"
POSTS = "P"
INACTIVE_OK = "I"
NEVER_SELF_APPROVED = "NSA"

FLAGS = frozenset({APPROVAL, SEGREGATED, DUAL_CONTROL, GROUP_ONLY, AGENT_OK, POSTS, INACTIVE_OK, NEVER_SELF_APPROVED})
# Flags an agent user can never use, whatever its role says (R12, A7).
AGENT_FORBIDDEN_FLAGS = frozenset({APPROVAL, SEGREGATED, DUAL_CONTROL, GROUP_ONLY, POSTS})

SYSTEM_CODES = ("billing", "zatca_queue", "fx_import", "deferred_sale", "governance")


@dataclass(frozen=True)
class Perm:
    codename: str
    label_en: str
    label_ar: str
    flags: frozenset = field(default_factory=frozenset)
    system: str | None = None

    @property
    def app_label(self):
        return self.codename.split(".", 1)[0]

    @property
    def is_approval(self):
        return APPROVAL in self.flags

    @property
    def is_segregated(self):
        return SEGREGATED in self.flags

    @property
    def is_dual_control(self):
        return DUAL_CONTROL in self.flags

    @property
    def is_group_only(self):
        return GROUP_ONLY in self.flags

    @property
    def agent_ok(self):
        return AGENT_OK in self.flags and not (self.flags & AGENT_FORBIDDEN_FLAGS) and self.system is None

    @property
    def posts(self):
        return POSTS in self.flags

    @property
    def inactive_ok(self):
        return INACTIVE_OK in self.flags

    @property
    def never_self_approved(self):
        """R80: the NSA flag, explicit or implied by S!, DC or G."""
        return bool(self.flags & {NEVER_SELF_APPROVED, SEGREGATED, DUAL_CONTROL, GROUP_ONLY})

    @property
    def is_view(self):
        """Read-only permissions; these still work on an inactive entity (OQ-8 default)."""
        return self.codename.split(".", 1)[1].startswith("view")


def _p(codename, en, ar, *flags, system=None):
    unknown = set(flags) - FLAGS
    if unknown:
        raise ValueError(f"Unknown flags {unknown} on {codename}")
    if system is not None and system not in SYSTEM_CODES:
        raise ValueError(f"Unknown system code {system} on {codename}")
    return Perm(codename, en, ar, frozenset(flags), system)


_PERMS = [
    # core
    _p("core.view_access", "View memberships and access log", "عرض العضويات وسجل الوصول"),
    _p("core.grant_membership", "Grant and revoke memberships", "منح العضويات وإلغاؤها", SEGREGATED),
    _p("core.delegate_approval", "Delegate approval", "تفويض صلاحية الاعتماد", SEGREGATED),
    # R32, OQ-18 default: managing_partner only.
    # I: an inactive group parent can still be reactivated. require() alone would let this right through on an
    # inactive entity for any change, so it passes there only with require(..., reactivation=True), which only
    # core.services._require_settings_right passes; that function then allows nothing but is_active=True (R38).
    _p("core.change_entity_settings", "Change sensitive entity settings", "تغيير إعدادات الكيان الحساسة", GROUP_ONLY,
       INACTIVE_OK),
    _p("core.manage_users", "Activate and deactivate user accounts", "تفعيل حسابات المستخدمين وإيقافها", GROUP_ONLY),
    # R48 (A17): turning Sharia enforcement off is a two-step four-eyes action on the group parent: a managing
    # partner requests it (first approval), the group's chairman approves it (second). Steps are role-bound below.
    _p("core.disable_sharia_enforcement", "Turn Sharia enforcement off (two-step approval)",
       "إيقاف تطبيق الضوابط الشرعية (اعتماد على مرحلتين)", GROUP_ONLY, DUAL_CONTROL, SEGREGATED),
    # access §10 (R69 to R90): per-entity authority matrix, governance report, assistant assignments. None of them
    # cascades from a group role to another entity (R90, R64): see NO_CASCADE below.
    _p("core.propose_authority_matrix", "Draft and submit authority matrix versions",
       "إعداد إصدارات مصفوفة الصلاحيات وتقديمها"),
    _p("core.approve_authority_matrix", "Approve authority matrix versions (second person)",
       "اعتماد إصدارات مصفوفة الصلاحيات (الشخص الثاني)", DUAL_CONTROL, SEGREGATED, NEVER_SELF_APPROVED),
    _p("core.view_governance_report", "View the monthly governance report", "عرض تقرير الحوكمة الشهري"),
    _p("core.review_governance_report", "Record the review of the governance report", "تسجيل مراجعة تقرير الحوكمة"),
    _p("core.generate_governance_report", "Generate the monthly governance report", "إنشاء تقرير الحوكمة الشهري",
       system="governance"),
    _p("core.manage_assistant_assignment", "Assign personal assistants to their principals",
       "تعيين المساعدين الشخصيين لأصحاب الصلاحية", SEGREGATED),
    # invoicing
    _p("invoicing.manage_customer", "Manage customers", "إدارة العملاء"),
    _p("invoicing.manage_vendor", "Manage vendors", "إدارة الموردين"),
    _p("invoicing.create_sales_document", "Create and edit draft sales documents", "إنشاء مسودات مستندات المبيعات وتعديلها", AGENT_OK),
    _p("invoicing.submit_sales_document", "Submit sales documents", "تقديم مستندات المبيعات"),
    _p("invoicing.approve_sales_document", "Approve and issue sales documents", "اعتماد مستندات المبيعات وإصدارها", APPROVAL, POSTS),
    _p("invoicing.create_from_billing", "Create invoices from billing", "إنشاء الفواتير من نظام الفوترة", system="billing"),
    # billing R43, R46c (A11): draft credit/debit notes from billing corrections, and deleting superseded drafts.
    _p("invoicing.create_adjustment_from_billing", "Create draft correction notes from billing",
       "إنشاء مسودات إشعارات التصحيح من نظام الفوترة", system="billing"),
    _p("invoicing.post_on_clearance", "Post sales on ZATCA clearance", "ترحيل المبيعات عند الاعتماد من الهيئة", POSTS, system="zatca_queue"),
    # invoicing R112 (payroll R47, owner decision E8): the bai' ajel sale invoice is created and issued by the
    # deferred-sale system user once the matrix approver (MP in Almiras) approved the sale in payroll; it posts on
    # ZATCA reporting like any sales document. The profit recognised as instalments are collected (Dr 1241 / Cr 4610)
    # is posted by the same system user after the payroll approval that collected them (owner decision A6).
    _p("invoicing.create_deferred_sale", "Create bai' ajel sale invoices approved in payroll",
       "إنشاء فواتير البيع الآجل المعتمدة في الرواتب", system="deferred_sale"),
    _p("invoicing.post_deferred_profit", "Post bai' ajel profit recognised on collected instalments",
       "ترحيل أرباح البيع الآجل المحققة من الأقساط المحصلة", POSTS, system="deferred_sale"),
    _p("invoicing.create_bill", "Create draft bills", "إنشاء مسودات فواتير الموردين", AGENT_OK),
    _p("invoicing.create_vendor_payment", "Create vendor payments", "إنشاء مدفوعات الموردين"),
    _p("invoicing.submit_bill", "Submit bills", "تقديم فواتير الموردين"),
    _p("invoicing.submit_vendor_payment", "Submit vendor payments", "تقديم مدفوعات الموردين"),
    _p("invoicing.approve_bill", "Approve bills", "اعتماد فواتير الموردين", APPROVAL, POSTS),
    _p("invoicing.approve_vendor_payment", "Approve vendor payments", "اعتماد مدفوعات الموردين", APPROVAL, POSTS),
    _p("invoicing.reverse_document", "Reverse documents", "عكس المستندات", POSTS),
    _p("invoicing.post_receipt", "Post customer receipts", "ترحيل المقبوضات من العملاء", POSTS),
    _p("invoicing.allocate", "Allocate payments", "تخصيص الدفعات"),
    _p("invoicing.enter_wht_data", "Enter withholding tax data", "إدخال بيانات ضريبة الاستقطاع"),
    _p("invoicing.verify_wht_data", "Verify withholding tax data", "التحقق من بيانات ضريبة الاستقطاع"),
    _p("invoicing.manage_settings", "Manage invoicing settings", "إدارة إعدادات الفوترة"),
    _p("invoicing.post_purification_transfer", "Post purification transfers", "ترحيل تحويلات التطهير", POSTS),
    _p("invoicing.approve_purification_payout", "Approve purification payouts", "اعتماد صرف مبالغ التطهير", APPROVAL, POSTS,
       NEVER_SELF_APPROVED),  # R80 g
    # invoicing R54 amendment D31b: charity recipients of purification payouts, added by the accountant and approved
    # by a Managing Partner who did not add them (S!: whatever roles the person holds).
    _p("invoicing.manage_charity_recipient", "Add and deactivate charity recipients",
       "إضافة الجهات الخيرية المستفيدة وإيقافها"),
    _p("invoicing.approve_charity_recipient", "Approve charity recipients", "اعتماد الجهات الخيرية المستفيدة",
       APPROVAL, SEGREGATED),
    # Owner decisions 2026-10-18: C2, the CFO approves (= posts) the funding of the purification account, never its
    # recorder; C4, an accounting user marks purification bank instructions exported or sent.
    _p("invoicing.approve_purification_funding", "Approve purification account fundings",
       "اعتماد تمويل حساب التطهير", APPROVAL, POSTS, NEVER_SELF_APPROVED),  # R80 g
    _p("invoicing.mark_purification_instruction_sent", "Mark purification bank instructions exported or sent",
       "تعليم أوامر صرف التطهير البنكية كمُصدَّرة أو مُرسلة"),
    _p("invoicing.view", "View invoicing", "عرض الفوترة", AGENT_OK),
    _p("invoicing.view_personal_data", "View personal data", "عرض البيانات الشخصية"),
    # billing
    _p("billing.edit_contract_draft", "Edit draft contracts", "تعديل مسودات العقود", AGENT_OK),
    _p("billing.record_contract_event", "Record contract events", "تسجيل أحداث العقود"),
    _p("billing.approve_contract", "Approve contracts", "اعتماد العقود", APPROVAL),
    _p("billing.approve_termination", "Approve contract terminations", "اعتماد إنهاء العقود", APPROVAL),
    _p("billing.start_run", "Start billing runs", "بدء دورات الفوترة"),
    _p("billing.release_period", "Release billing periods", "إطلاق فترات الفوترة"),
    _p("billing.issue_from_contract", "Issue invoices from contracts", "إصدار الفواتير من العقود", system="billing"),
    # billing §7 as amended 2026-10-17 (A11): versions, corrections and recompute. billing.create_credit_note_draft
    # (pre-D9) is withdrawn: billing.review_correction + invoicing.create_adjustment_from_billing replace it.
    _p("billing.edit_version_draft", "Draft and submit contract versions", "إعداد نسخ العقود وتقديمها"),
    _p("billing.approve_version", "Approve contract versions", "اعتماد نسخ العقود", APPROVAL),
    _p("billing.review_correction", "Review, submit or discard billing corrections",
       "مراجعة تصحيحات الفوترة وتقديمها أو استبعادها"),
    _p("billing.recompute_corrections", "Recompute billing corrections", "إعادة احتساب تصحيحات الفوترة"),
    _p("billing.view", "View billing", "عرض العقود والفوترة", AGENT_OK),
    _p("billing.view_employee_ref", "View employee references", "عرض مراجع الموظفين"),
    # zatca
    _p("zatca.manage_profile", "Manage ZATCA profile and EGS units", "إدارة ملف الربط ووحدات الفوترة"),
    _p("zatca.onboard", "Onboard and manage ZATCA certificates", "التسجيل وإدارة شهادات الهيئة", SEGREGATED),
    _p("zatca.switch_to_production", "Switch an EGS unit to production", "تحويل وحدة الفوترة إلى بيئة الإنتاج", DUAL_CONTROL),
    _p("zatca.process_queue", "Process the ZATCA queue", "معالجة طابور الإرسال للهيئة", system="zatca_queue"),
    _p("zatca.retry_failed", "Retry failed ZATCA submissions", "إعادة محاولة الإرسال المتعثر للهيئة"),
    _p("zatca.view", "View ZATCA", "عرض بيانات الربط مع الهيئة"),
    _p("zatca.view_status", "View ZATCA status", "عرض حالة الإرسال للهيئة", AGENT_OK),
    _p("zatca.export_archive", "Export the ZATCA archive", "تصدير أرشيف الفواتير الإلكترونية"),
    _p("zatca.close_late_notice", "Close late-submission notices", "إغلاق إشعارات التأخر في الإرسال"),
    _p("zatca.grant_key_delegation", "Grant ZATCA key custody", "تفويض حفظ مفاتيح الهيئة", GROUP_ONLY, SEGREGATED),
    # ledger
    _p("ledger.create_entry", "Create draft journal entries", "إنشاء مسودات القيود اليومية", AGENT_OK),
    _p("ledger.post_entry", "Post manual journal entries", "ترحيل القيود اليومية اليدوية", APPROVAL, POSTS),
    _p("ledger.reverse_entry", "Reverse journal entries", "عكس القيود اليومية", POSTS),
    _p("ledger.close_period", "Close fiscal periods", "إقفال الفترات المالية"),
    _p("ledger.maintain_global_rate", "Maintain group exchange rates", "إدارة أسعار الصرف للمجموعة", GROUP_ONLY),
    _p("ledger.import_global_rate", "Import group exchange rates", "استيراد أسعار الصرف للمجموعة", GROUP_ONLY, system="fx_import"),
    _p("ledger.maintain_entity_rate", "Maintain entity exchange rates", "إدارة أسعار الصرف للكيان"),
    _p("ledger.use_manual_rate", "Use a manual exchange rate", "استخدام سعر صرف يدوي"),
    _p("ledger.revalue", "Run currency revaluation", "تشغيل إعادة تقييم العملات"),
    _p("ledger.view", "View the ledger", "عرض دفتر الأستاذ", AGENT_OK),
    _p("ledger.view_rates", "View exchange rates", "عرض أسعار الصرف", AGENT_OK),
    _p("ledger.view_translation", "View currency translation", "عرض ترجمة العملات", AGENT_OK),
    _p("ledger.change_functional_currency", "Change functional currency", "تغيير العملة الوظيفية", GROUP_ONLY),
    # hr (hr.md §7; A21). No HR permission is Ag (hr R13). hr.maintain_statutory_parameter is not G: the spec's
    # holder is the group parent's hr_officer (an entity role); hr.services checks it on the group parent entity.
    _p("hr.view", "View HR records (personal data masked)", "عرض سجلات الموارد البشرية (البيانات الشخصية مخفية)"),
    _p("hr.view_counts", "View HR head counts only", "عرض أعداد الموظفين فقط"),
    _p("hr.view_personal_data", "View employees' personal data", "عرض البيانات الشخصية للموظفين"),
    # R80 f: the second person who approves an employee's IBAN change holds this right (hr approve_iban_change).
    _p("hr.manage_employee", "Create and edit employees and their documents", "إنشاء الموظفين ومستنداتهم وتعديلها",
       NEVER_SELF_APPROVED),
    _p("hr.edit_contract_draft", "Edit draft employment contracts", "تعديل مسودات عقود العمل"),
    _p("hr.submit_contract", "Submit employment contracts", "تقديم عقود العمل"),
    _p("hr.approve_contract", "Approve employment contracts", "اعتماد عقود العمل", APPROVAL),
    _p("hr.manage_assignment", "Draft assignments and secondments", "إعداد التكليفات والإعارات"),
    _p("hr.approve_assignment", "Approve assignments and secondments", "اعتماد التكليفات والإعارات", APPROVAL),
    _p("hr.end_assignment", "End or cancel assignments and secondments", "إنهاء التكليفات والإعارات أو إلغاؤها"),
    _p("hr.request_leave", "Record leave requests and balance corrections", "تسجيل طلبات الإجازات وتصحيحات الأرصدة"),
    _p("hr.approve_leave", "Approve leave requests", "اعتماد طلبات الإجازات", APPROVAL),
    _p("hr.adjust_leave_balance", "Approve leave balance adjustments", "اعتماد تسويات أرصدة الإجازات", APPROVAL),
    _p("hr.record_termination", "Record terminations", "تسجيل إنهاء الخدمة"),
    _p("hr.approve_termination", "Approve terminations", "اعتماد إنهاء الخدمة", APPROVAL),
    _p("hr.calculate_eosb", "Preview end-of-service calculations", "معاينة حساب مكافأة نهاية الخدمة"),
    _p("hr.export_personal_data", "Export an employee's personal data", "تصدير البيانات الشخصية للموظف", SEGREGATED),
    _p("hr.maintain_statutory_parameter", "Enter statutory parameters", "إدخال المعايير النظامية"),
    _p("hr.verify_statutory_parameter", "Verify statutory parameters", "التحقق من المعايير النظامية", GROUP_ONLY,
       APPROVAL),
    _p("hr.receive_alerts", "Receive HR alerts", "استلام تنبيهات الموارد البشرية"),
    _p("hr.manage_org", "Manage departments and grades", "إدارة الأقسام والدرجات الوظيفية"),
    _p("hr.set_overtime_route", "Change the overtime approval route", "تغيير مسار اعتماد العمل الإضافي"),
    # payroll (payroll.md §7, P14, P20). No payroll permission is Ag (R38): agents and system users hold none.
    # Approvers come from the entity's authority matrix once it is built (access 3.8); until then
    # payroll.approvals holds the seed rows (runs: hr_officer only, never the MP; P20(a)).
    _p("payroll.view", "View payroll (aggregates, personal data masked)", "عرض الرواتب (المجاميع، البيانات الشخصية مخفية)"),
    _p("payroll.view_payslip", "View individual payslips (logged)", "عرض قسائم الرواتب الفردية (مسجّل)"),
    _p("payroll.view_cost_reports", "View payroll cost reports without personal data",
       "عرض تقارير تكاليف الرواتب دون بيانات شخصية"),
    _p("payroll.manage_settings", "Manage payroll settings", "إدارة إعدادات الرواتب"),
    _p("payroll.manage_inputs", "Enter payroll inputs", "إدخال مدخلات الرواتب"),
    _p("payroll.approve_input", "Approve payroll inputs", "اعتماد مدخلات الرواتب", APPROVAL),
    _p("payroll.approve_overtime", "Approve overtime", "اعتماد العمل الإضافي", APPROVAL),
    _p("payroll.record_maternity_case", "Record GOSI maternity cases", "تسجيل حالات تعويض الأمومة لدى التأمينات"),
    _p("payroll.approve_maternity_clearance", "Approve written legal clearances for maternity pay",
       "اعتماد الإذن القانوني المكتوب لأجر الأمومة", APPROVAL),
    _p("payroll.prepare_run", "Prepare and calculate payroll runs", "إعداد مسيرات الرواتب واحتسابها"),
    _p("payroll.submit_run", "Submit payroll runs for approval", "تقديم مسيرات الرواتب للاعتماد"),
    _p("payroll.approve_run", "Approve and post payroll runs", "اعتماد مسيرات الرواتب وترحيلها", APPROVAL, POSTS),
    _p("payroll.reverse_run", "Reverse payroll runs", "عكس مسيرات الرواتب", POSTS),
    _p("payroll.create_loan", "Draft interest-free employee loans", "إعداد مسودات قروض الموظفين الحسنة"),
    _p("payroll.approve_loan", "Approve and disburse interest-free employee loans",
       "اعتماد قروض الموظفين الحسنة وصرفها", APPROVAL, POSTS),
    _p("payroll.create_deferred_sale", "Draft bai' ajel employee financing", "إعداد مسودات البيع الآجل للموظفين"),
    _p("payroll.approve_deferred_sale_terms", "Approve bai' ajel terms", "اعتماد شروط البيع الآجل", APPROVAL),
    # R47 (P19(k)): approving the sale creates the sale invoice through invoicing, which posts it (invoicing R112).
    _p("payroll.approve_deferred_sale", "Approve bai' ajel sales", "اعتماد البيع الآجل", APPROVAL),
    _p("payroll.record_payment", "Record payroll payments", "تسجيل مدفوعات الرواتب"),
    _p("payroll.approve_payment", "Approve and post payroll payments", "اعتماد مدفوعات الرواتب وترحيلها", APPROVAL,
       POSTS),
    # Final settlements (payroll R34, E5; compliance C-A11): prepared by the payroll officer, approved (= posted) by the
    # approver (OQ-36), never by the preparer.
    _p("payroll.prepare_final_settlement", "Prepare final settlements", "إعداد المخالصات النهائية"),
    _p("payroll.approve_final_settlement", "Approve and post final settlements", "اعتماد المخالصات النهائية وترحيلها",
       APPROVAL, POSTS),
    # Payroll slice 2 (payroll.md §10.8): corrections of approved final settlements (a second person approves and
    # posts), loan write-offs on exit (the entity's top authority, OQ-7 default), employee cost items and the fully
    # loaded secondment cost report (R35, R63; billing R71), GOSI late-payment fines (E14) and payment reversals,
    # bai' ajel profit recognition (E12).
    _p("payroll.prepare_settlement_correction", "Prepare final settlement corrections", "إعداد تصحيحات المخالصات النهائية"),
    _p("payroll.approve_settlement_correction", "Approve and post final settlement corrections",
       "اعتماد تصحيحات المخالصات النهائية وترحيلها", APPROVAL, POSTS),
    _p("payroll.prepare_loan_write_off", "Prepare loan write-offs on exit", "إعداد شطب القروض عند انتهاء الخدمة"),
    _p("payroll.approve_loan_write_off", "Approve and post loan write-offs", "اعتماد شطب القروض وترحيله", APPROVAL,
       POSTS),
    # access 10.5 (coordinator 2026-10-21): bai' ajel balances after exit written off or recovered (DeferredSaleClosure)
    # have their own approval, held like payroll.approve_loan_write_off.
    _p("payroll.approve_deferred_sale_closure", "Approve and post bai' ajel write-offs and recoveries after exit",
       "اعتماد شطب البيع الآجل واسترداده بعد انتهاء الخدمة وترحيلهما", APPROVAL, POSTS),
    _p("payroll.manage_cost_items", "Enter employee cost items", "إدخال بنود تكاليف الموظفين"),
    _p("payroll.approve_cost_item", "Approve employee cost items", "اعتماد بنود تكاليف الموظفين", APPROVAL),
    _p("payroll.prepare_cost_report", "Prepare secondment cost reports", "إعداد تقارير تكلفة الإعارة"),
    _p("payroll.approve_cost_report", "Approve secondment cost reports", "اعتماد تقارير تكلفة الإعارة", APPROVAL),
    _p("payroll.record_gosi_fine", "Record GOSI late-payment fines", "تسجيل غرامات تأخير التأمينات"),
    _p("payroll.approve_gosi_fine", "Approve and post GOSI late-payment fines", "اعتماد غرامات تأخير التأمينات وترحيلها",
       APPROVAL, POSTS),
    _p("payroll.reverse_payment", "Reverse payroll payments", "عكس مدفوعات الرواتب", POSTS),
    _p("payroll.prepare_profit_recognition", "Prepare bai' ajel profit recognition", "إعداد إثبات أرباح البيع الآجل"),
    _p("payroll.approve_profit_recognition", "Approve bai' ajel profit recognition", "اعتماد إثبات أرباح البيع الآجل",
       APPROVAL),
    _p("payroll.generate_wps_file", "Generate salary (WPS) files", "إنشاء ملفات الرواتب (حماية الأجور)"),
    _p("payroll.record_mudad_submission", "Record Mudad submissions", "تسجيل الرفع على منصة مدد"),
    # Payroll slice 3 (payroll.md §11.7): liability reports (nothing posted) and Mudad justifications.
    _p("payroll.prepare_liability_report", "Generate leave and EOSB liability reports",
       "إعداد تقارير التزامات الإجازات ومكافأة نهاية الخدمة"),
    _p("payroll.record_mudad_justification", "Record Mudad justifications", "تسجيل مبررات مدد"),
    _p("payroll.approve_mudad_justification", "Approve Mudad justifications", "اعتماد مبررات مدد", APPROVAL),
    # banking (banking.md §7). Agents may view (masked), suggest matches and draft bank transactions only (R43).
    _p("banking.view", "View banking (account numbers masked)", "عرض البيانات البنكية (أرقام الحسابات مخفية)", AGENT_OK),
    _p("banking.view_iban", "View full IBANs and account numbers", "عرض أرقام الآيبان والحسابات كاملة"),
    _p("banking.manage_bank_account", "Draft and edit bank accounts", "إعداد الحسابات البنكية وتعديلها"),
    _p("banking.approve_bank_account", "Approve bank accounts and their changes", "اعتماد الحسابات البنكية وتعديلاتها",
       APPROVAL, SEGREGATED),
    _p("banking.import_statement", "Import bank statements", "استيراد كشوف الحسابات البنكية"),
    _p("banking.suggest_match", "Suggest bank matches", "اقتراح مطابقات بنكية", AGENT_OK),
    _p("banking.match", "Confirm, reject, build and undo bank matches", "تأكيد المطابقات البنكية ورفضها وإنشاؤها وإلغاؤها"),
    _p("banking.create_bank_transaction", "Draft bank transactions", "إعداد مسودات العمليات البنكية", AGENT_OK),
    _p("banking.submit_bank_transaction", "Submit bank transactions", "تقديم العمليات البنكية"),
    _p("banking.approve_bank_transaction", "Approve and post bank transactions", "اعتماد العمليات البنكية وترحيلها",
       APPROVAL, POSTS),
    _p("banking.reverse_bank_transaction", "Reverse bank transactions", "عكس العمليات البنكية", POSTS),
    _p("banking.prepare_reconciliation", "Prepare and submit bank reconciliations", "إعداد التسويات البنكية وتقديمها"),
    _p("banking.approve_reconciliation", "Approve bank reconciliations", "اعتماد التسويات البنكية", APPROVAL,
       NEVER_SELF_APPROVED),  # R80 i
    _p("banking.reopen_reconciliation", "Reopen an approved bank reconciliation", "إعادة فتح تسوية بنكية معتمدة",
       SEGREGATED),
    _p("banking.manage_settings", "Manage banking settings and CSV layouts", "إدارة إعدادات البنوك وتنسيقات ملفات CSV"),
    # migration (migration.md §7): legacy (Falcon) exports into opening balances and replayed journals. Drafting the
    # entries also needs ledger.create_entry; posting them needs ledger.post_entry (a second person).
    _p("migration.view", "View account maps and imports", "عرض خرائط الحسابات وعمليات الاستيراد"),
    _p("migration.manage_account_map", "Draft and edit legacy account maps", "إعداد خرائط حسابات النظام السابق وتعديلها"),
    _p("migration.approve_account_map", "Approve legacy account maps", "اعتماد خرائط حسابات النظام السابق", APPROVAL,
       NEVER_SELF_APPROVED),  # R80 h
    _p("migration.import", "Validate legacy exports and draft their entries", "فحص ملفات النظام السابق وإعداد مسودات قيودها"),
    # tax, VAT slice (tax-returns.md §7, R8, R14). Agents may read and compute drafts only (R14). Preparer, reviewer
    # and approver are three different people (R8, OQ-7: no exception); tax.services enforces reviewer != preparer
    # and approver != reviewer, the access layer approver != creator / modifier / submitter (A). WHT, Zakat and
    # income-tax rights (tax.prepare_zakat, tax.manage_shareholders) come with those parts.
    _p("tax.view", "View tax returns", "عرض الإقرارات الضريبية", AGENT_OK),
    _p("tax.prepare_return", "Prepare VAT returns and draft tax payments and records", "إعداد الإقرارات الضريبية "
       "ومسودات الدفعات والسجلات الضريبية", AGENT_OK),
    _p("tax.review_return", "Review VAT returns", "مراجعة الإقرارات الضريبية", NEVER_SELF_APPROVED),  # R80 a (G3)
    _p("tax.approve_return", "Approve and lock VAT returns", "اعتماد الإقرارات الضريبية وإقفالها", APPROVAL, POSTS,
       NEVER_SELF_APPROVED),  # R80 a (G3)
    _p("tax.record_filing", "Record the filing of tax returns", "تسجيل تقديم الإقرارات الضريبية"),
    _p("tax.approve_payment", "Approve and post tax payments, refunds and import VAT", "اعتماد الدفعات والاستردادات "
       "الضريبية وضريبة الاستيراد وترحيلها", APPROVAL, POSTS, NEVER_SELF_APPROVED),  # R80 b
    _p("tax.reverse_document", "Reverse tax documents", "عكس المستندات الضريبية", POSTS),
    _p("tax.change_settings", "Change tax settings", "تغيير إعدادات الضرائب", APPROVAL, NEVER_SELF_APPROVED),  # R80 b
    _p("tax.maintain_rates", "Verify statutory tax rates and VAT box definitions",
       "التحقق من النسب الضريبية النظامية وتعريفات خانات الإقرار", NEVER_SELF_APPROVED),  # R80 b (H3)
    # Owner decision H3 (2026-10-19, tax-returns OQ-46): the accountant proposes a box definition as checked; the
    # approver (CFO), holding tax.maintain_rates, verifies it.
    _p("tax.propose_box_verification", "Propose VAT box definitions as checked against the ZATCA form",
       "اقتراح اعتماد تعريفات خانات الإقرار بعد مطابقتها لنموذج الهيئة"),
]

CATALOGUE = {p.codename: p for p in _PERMS}
assert len(CATALOGUE) == len(_PERMS), "duplicate codename in catalogue"

# R19 as amended by A17 (R48): a DC permission may bind its steps to roles. ``request`` opens the request (and is
# its first approval), ``decide`` approves or rejects it, ``use`` applies it. A step that is not listed may be
# taken by any holder of the permission. No "or managing_partner" fallback applies to a bound step.
DUAL_CONTROL_STEPS = {
    "core.disable_sharia_enforcement": {
        "request": frozenset({"managing_partner"}),
        "decide": frozenset({"chairman"}),
        "use": frozenset({"managing_partner"}),
    },
}
DUAL_CONTROL_STEP_NAMES = ("request", "decide", "use")
assert all(CATALOGUE[c].is_dual_control and set(steps) <= set(DUAL_CONTROL_STEP_NAMES)
           for c, steps in DUAL_CONTROL_STEPS.items()), "role-bound steps are for DC permissions only"


def dual_control_roles(codename, step):
    """Role codes that may take ``step`` of DC permission ``codename``; None when any holder may (R19, R48)."""
    if step not in DUAL_CONTROL_STEP_NAMES:
        raise ValueError(f"Unknown dual-control step {step!r}")
    return DUAL_CONTROL_STEPS.get(codename, {}).get(step)


# R80: the activities where self-approval is never allowed, also for the top authority, as rows a to i of the
# spec's table. Held in code (the NSA flag and the flags implying it), never as data. Test AT-85 scans it.
NSA_ROWS = {
    "a": ("tax.review_return", "tax.approve_return"),
    "b": ("tax.approve_payment", "tax.change_settings", "tax.maintain_rates"),
    "c": ("core.disable_sharia_enforcement", "zatca.switch_to_production", "core.approve_authority_matrix"),
    "d": ("core.grant_membership", "core.delegate_approval", "zatca.grant_key_delegation", "zatca.onboard",
          "banking.approve_bank_account", "banking.reopen_reconciliation", "invoicing.approve_charity_recipient",
          "hr.export_personal_data", "core.manage_assistant_assignment"),
    "e": ("core.change_entity_settings", "core.manage_users", "ledger.maintain_global_rate",
          "ledger.change_functional_currency", "hr.verify_statutory_parameter"),
    "f": ("hr.manage_employee",),
    "g": ("invoicing.approve_purification_funding", "invoicing.approve_purification_payout"),
    "h": ("migration.approve_account_map",),
    "i": ("banking.approve_reconciliation",),
}

# R90, R64 (A27): rights of access §10 never come from a group membership on the group parent for another entity.
# A group role (e.g. the managing partner, the chairman) uses them only on the entity it is granted on.
NO_CASCADE = frozenset({
    "core.propose_authority_matrix", "core.approve_authority_matrix", "core.view_governance_report",
    "core.review_governance_report", "core.generate_governance_report", "core.manage_assistant_assignment",
})

# Plain (non-Sys) permissions a system user may also hold (3.6 holders column, R13).
SYSTEM_EXTRA = {
    # billing R67: the run's own right, plus the rate reads create_from_billing needs.
    "billing": frozenset({"billing.start_run", "ledger.view_rates"}),
    "zatca_queue": frozenset(),
    "fx_import": frozenset(),
    "deferred_sale": frozenset(),
    "governance": frozenset(),
}

# Ledger postings (R23, R30): which P grants each ledger service accepts.
# - post_entry (an existing draft): only ledger.post_entry, issued for that draft.
# - create_entry(post=True, document=...): a sub-ledger P grant issued for that document, for a
#   document type registered in ledger.documents with that permission.
# - reverse_entry: ledger.reverse_entry for that entry, or invoicing.reverse_document /
#   banking.reverse_bank_transaction / tax.reverse_document for its document.
#   Reversal grants only reverse: they never post a new entry.
REVERSE_PERMISSIONS = frozenset({"ledger.reverse_entry", "invoicing.reverse_document",
                                 "banking.reverse_bank_transaction", "tax.reverse_document", "payroll.reverse_run",
                                 "payroll.reverse_payment"})
DRAFT_POST_PERMISSIONS = frozenset({"ledger.post_entry"})
DOCUMENT_POST_PERMISSIONS = frozenset(c for c, p in CATALOGUE.items() if p.posts) - DRAFT_POST_PERMISSIONS - REVERSE_PERMISSIONS
POST_PERMISSIONS = DRAFT_POST_PERMISSIONS | DOCUMENT_POST_PERMISSIONS


def get(codename):
    try:
        return CATALOGUE[codename]
    except KeyError:
        raise KeyError(f"Permission {codename!r} is not in the access catalogue.") from None


def allowed_for_system(system_code):
    """Every permission system user ``system_code`` may hold (R13)."""
    own = {c for c, p in CATALOGUE.items() if p.system == system_code}
    return frozenset(own) | SYSTEM_EXTRA.get(system_code, frozenset())


def role_may_hold(kind, scope, system_code, perm):
    """Return None if a role of this kind/scope may hold ``perm``, else a reason code (R12, R13, G)."""
    if perm.is_group_only and scope != "group":
        return "group_only"
    if kind == "agent":
        return None if perm.agent_ok else "agent_forbidden"
    if kind == "system":
        return None if perm.codename in allowed_for_system(system_code) else "system_forbidden"
    if perm.system is not None:
        return "system_only"
    return None
