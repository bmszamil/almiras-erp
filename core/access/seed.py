"""Seeded role catalogue (access.md 3.1, holders column of 3.6).

``ROLE_SEED_V1`` is applied by migration ``core.0004_seed_access``. Seeded
roles' permission sets change only by migration: to change them, add a
``ROLE_SEED_V2`` (or a delta) and a new data migration; never edit V1 after
it has been applied anywhere.
"""

_LEDGER_VIEW = ("ledger.view", "ledger.view_rates", "ledger.view_translation")

# Every A permission; managing_partner holds them as fallback approver (OQ-1 default).
_APPROVALS = (
    "invoicing.approve_sales_document",
    "invoicing.approve_bill",
    "invoicing.approve_vendor_payment",
    "invoicing.approve_purification_payout",
    "billing.approve_contract",
    "billing.approve_termination",
    "ledger.post_entry",
)

# code: (name_en, name_ar, scope, kind, system_code, permissions)
ROLE_SEED_V1 = {
    "receivables_clerk": (
        "Receivables clerk", "محاسب الذمم المدينة", "entity", "human", None,
        (
            "invoicing.manage_customer", "invoicing.create_sales_document", "invoicing.submit_sales_document",
            "invoicing.post_receipt", "invoicing.allocate", "invoicing.view", "invoicing.view_personal_data",
            *_LEDGER_VIEW,
        ),
    ),
    "payables_clerk": (
        "Payables clerk", "محاسب الذمم الدائنة", "entity", "human", None,
        (
            "invoicing.manage_vendor", "invoicing.create_bill", "invoicing.create_vendor_payment",
            "invoicing.submit_bill", "invoicing.submit_vendor_payment", "invoicing.allocate",
            "invoicing.enter_wht_data", "invoicing.view", "invoicing.view_personal_data",
            *_LEDGER_VIEW,
        ),
    ),
    "accountant": (
        "Accountant", "محاسب", "entity", "human", None,
        (
            "invoicing.manage_customer", "invoicing.manage_vendor", "invoicing.create_sales_document",
            "invoicing.submit_sales_document", "invoicing.create_bill", "invoicing.create_vendor_payment",
            "invoicing.submit_bill", "invoicing.submit_vendor_payment", "invoicing.reverse_document",
            "invoicing.post_receipt", "invoicing.allocate", "invoicing.enter_wht_data", "invoicing.verify_wht_data",
            "invoicing.post_purification_transfer", "invoicing.view", "invoicing.view_personal_data",
            "billing.edit_contract_draft", "billing.record_contract_event", "billing.start_run",
            "billing.release_period", "billing.create_credit_note_draft", "billing.view", "billing.view_employee_ref",
            "zatca.retry_failed", "zatca.view", "zatca.export_archive",
            # OQ-5 default: accountant creates drafts and reverses; does not post manual journals.
            "ledger.create_entry", "ledger.reverse_entry", "ledger.maintain_entity_rate", "ledger.use_manual_rate",
            "ledger.revalue", *_LEDGER_VIEW,
        ),
    ),
    "approver": (
        "Approver (CFO)", "المعتمد (المدير المالي)", "entity", "human", None,
        (
            "core.delegate_approval",
            "invoicing.approve_sales_document", "invoicing.approve_bill", "invoicing.approve_vendor_payment",
            "invoicing.manage_settings", "invoicing.post_purification_transfer",
            "invoicing.approve_purification_payout", "invoicing.view", "invoicing.view_personal_data",
            "billing.edit_contract_draft", "billing.record_contract_event", "billing.approve_contract",
            "billing.approve_termination", "billing.start_run", "billing.release_period",
            "billing.create_credit_note_draft", "billing.view", "billing.view_employee_ref",
            "ledger.post_entry", "ledger.close_period", *_LEDGER_VIEW,
        ),
    ),
    "viewer": (
        "Viewer / auditor", "مطّلع / مدقق", "entity", "human", None,
        ("core.view_access", "invoicing.view", "billing.view", "zatca.view", "zatca.export_archive", *_LEDGER_VIEW),
    ),
    "zatca_admin": (
        "ZATCA administrator", "مسؤول الربط مع هيئة الزكاة والضريبة والجمارك", "entity", "human", None,
        (
            "zatca.manage_profile", "zatca.onboard", "zatca.switch_to_production", "zatca.retry_failed",
            "zatca.view", "zatca.export_archive", "zatca.close_late_notice", "invoicing.view", *_LEDGER_VIEW,
        ),
    ),
    "access_admin": (
        "Access administrator", "مسؤول الصلاحيات", "entity", "human", None,
        ("core.view_access", "core.grant_membership", "invoicing.view", *_LEDGER_VIEW),
    ),
    "managing_partner": (
        "Managing Partner", "الشريك المدير", "group", "human", None,
        (
            "core.view_access", "core.grant_membership", "core.delegate_approval",
            "zatca.switch_to_production", "zatca.grant_key_delegation", "ledger.change_functional_currency",
            *_APPROVALS, *_LEDGER_VIEW,
        ),
    ),
    "group_treasury": (
        "Group treasury", "خزينة المجموعة", "group", "human", None,
        ("ledger.maintain_global_rate", "ledger.maintain_entity_rate", *_LEDGER_VIEW),
    ),
    "group_viewer": (
        "Group / consolidation viewer", "مطّلع على المجموعة (التوحيد)", "group", "human", None,
        _LEDGER_VIEW,
    ),
    "system_billing": (
        "Billing system user", "مستخدم نظام الفوترة", "entity", "system", "billing",
        ("invoicing.create_from_billing", "billing.issue_from_contract", "billing.start_run"),
    ),
    "system_zatca_queue": (
        "ZATCA queue worker", "مستخدم نظام طابور فاتورة", "entity", "system", "zatca_queue",
        ("invoicing.post_on_clearance", "zatca.process_queue"),
    ),
    "system_fx_import": (
        "FX rate import user", "مستخدم نظام استيراد أسعار الصرف", "group", "system", "fx_import",
        ("ledger.import_global_rate",),
    ),
}

# One system user per system_code (R20): code -> (username, display_name_en, display_name_ar)
SYSTEM_USERS_V1 = {
    "billing": ("system.billing", "Billing system user", "مستخدم نظام الفوترة"),
    "zatca_queue": ("system.zatca_queue", "ZATCA queue worker", "مستخدم نظام طابور فاتورة"),
    "fx_import": ("system.fx_import", "FX rate import user", "مستخدم نظام استيراد أسعار الصرف"),
}


def apply_seed(Role, RolePermission, roles=ROLE_SEED_V1):
    """Create or update the seeded roles and their permission rows. Idempotent.

    Takes model classes so a migration can pass historical models.
    """
    for code, (name_en, name_ar, scope, kind, system_code, perms) in roles.items():
        role, _ = Role.objects.update_or_create(
            code=code,
            defaults=dict(
                name_en=name_en, name_ar=name_ar, scope=scope, kind=kind, system_code=system_code,
                is_seeded=True, is_active=True,
            ),
        )
        RolePermission.objects.filter(role=role).exclude(codename__in=perms).delete()
        for codename in perms:
            RolePermission.objects.get_or_create(role=role, codename=codename)


def apply_system_users(User, UserProfile, users=SYSTEM_USERS_V1):
    """Create one system user per system code, with an unusable password (R20). Idempotent."""
    from django.contrib.auth.hashers import make_password

    for code, (username, en, ar) in users.items():
        user, created = User.objects.get_or_create(
            username=username,
            defaults=dict(password=make_password(None), is_active=True, is_staff=False, is_superuser=False),
        )
        UserProfile.objects.get_or_create(
            user=user,
            defaults=dict(kind="system", system_code=code, display_name_en=en, display_name_ar=ar),
        )
