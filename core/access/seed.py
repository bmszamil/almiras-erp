"""Seeded role catalogue (access.md 3.1, holders column of 3.6).

``ROLE_SEED_V1`` is applied by migration ``core.0004_seed_access``, V2 by ``core.0009``, V3 by ``core.0012``. Seeded
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

# V2 (migration core.0009): managing_partner also holds the R32 entity-settings and user-activation rights (OQ-18).
ROLE_SEED_V2 = {
    **ROLE_SEED_V1,
    "managing_partner": (
        *ROLE_SEED_V1["managing_partner"][:5],
        (*ROLE_SEED_V1["managing_partner"][5], "core.change_entity_settings", "core.manage_users"),
    ),
}
# V3 (migration core.0012, A17 / R47 / R48): the group-level ``chairman`` role, which holds only the second
# approval of a Sharia switch-off; managing_partner requests (first approval) and applies it.
ROLE_SEED_V3 = {
    **ROLE_SEED_V2,
    "managing_partner": (
        *ROLE_SEED_V2["managing_partner"][:5],
        (*ROLE_SEED_V2["managing_partner"][5], "core.disable_sharia_enforcement"),
    ),
    "chairman": (
        "Chairman", "رئيس مجلس الإدارة", "group", "human", None,
        ("core.disable_sharia_enforcement",),
    ),
}
# V4 (migration core.0013, A21 / hr.md §7): roles ``hr_officer`` and ``payroll_officer`` (granted by the managing
# partner only, R52) and the HR permissions on existing roles. accountant and viewer get the masked ``hr.view``
# (hr AT-6: they open an employee and see masked values). payroll.* rights come with the payroll module.
_HR_APPROVALS = ("hr.approve_contract", "hr.approve_assignment", "hr.approve_leave", "hr.adjust_leave_balance",
                 "hr.approve_termination")


def _plus(seed, code, *perms):
    return (*seed[code][:5], (*seed[code][5], *perms))


ROLE_SEED_V4 = {
    **ROLE_SEED_V3,
    "accountant": _plus(ROLE_SEED_V3, "accountant", "hr.view"),
    "viewer": _plus(ROLE_SEED_V3, "viewer", "hr.view"),
    "approver": _plus(
        ROLE_SEED_V3, "approver", "hr.view", "hr.approve_contract", "hr.approve_assignment", "hr.end_assignment",
        "hr.adjust_leave_balance", "hr.approve_termination", "hr.calculate_eosb",
    ),
    "managing_partner": _plus(
        ROLE_SEED_V3, "managing_partner", "hr.view", *_HR_APPROVALS, "hr.verify_statutory_parameter",
        "hr.set_overtime_route",
    ),
    "hr_officer": (
        "HR officer", "مسؤول الموارد البشرية", "entity", "human", None,
        (
            "hr.view", "hr.view_personal_data", "hr.manage_employee", "hr.edit_contract_draft", "hr.submit_contract",
            "hr.manage_assignment", "hr.end_assignment", "hr.request_leave", "hr.approve_leave",
            "hr.record_termination", "hr.calculate_eosb", "hr.export_personal_data",
            "hr.maintain_statutory_parameter", "hr.receive_alerts", "hr.manage_org", *_LEDGER_VIEW,
        ),
    ),
    "payroll_officer": (
        "Payroll officer", "مسؤول الرواتب", "entity", "human", None,
        # hr.view_personal_data: bank, IBAN and ID only (hr §7); hr.services limits the fields for this role.
        ("hr.view", "hr.view_personal_data", "hr.calculate_eosb", *_LEDGER_VIEW),
    ),
}
# V5 (migration core.0014, hr.md §7): viewers see HR counts only (``hr.view`` holders: hr_officer, payroll_officer,
# approver; viewer: counts only). The accountant keeps the masked ``hr.view``: hr AT-6 and the §7 action table have
# the accountant open an employee and see masked values.
ROLE_SEED_V5 = {
    **ROLE_SEED_V4,
    "viewer": (*ROLE_SEED_V4["viewer"][:5],
               tuple(p for p in ROLE_SEED_V4["viewer"][5] if p != "hr.view") + ("hr.view_counts",)),
}
# The current catalogue: what a fresh (or flushed test) database is seeded with.
ROLE_SEED = ROLE_SEED_V5

# One system user per system_code (R20): code -> (username, display_name_en, display_name_ar)
SYSTEM_USERS_V1 = {
    "billing": ("system.billing", "Billing system user", "مستخدم نظام الفوترة"),
    "zatca_queue": ("system.zatca_queue", "ZATCA queue worker", "مستخدم نظام طابور فاتورة"),
    "fx_import": ("system.fx_import", "FX rate import user", "مستخدم نظام استيراد أسعار الصرف"),
}


def apply_seed(Role, RolePermission, roles=ROLE_SEED):
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


class SeedConflict(RuntimeError):
    """A system username is taken by a user that is not that system user (R20)."""


def apply_system_users(User, UserProfile, users=SYSTEM_USERS_V1):
    """Create one system user per system code (R20). Idempotent.

    A username that already exists must belong to the matching system
    profile; anything else (a human, an agent, another system code, a user
    with no profile) fails loudly instead of being adopted. Every system user
    is left with an unusable password and without superuser or staff flags,
    whatever it had before.
    """
    from django.contrib.auth.hashers import make_password

    for code, (username, en, ar) in users.items():
        user = User.objects.filter(username=username).first()
        if user is None:
            user = User(username=username, password=make_password(None), is_active=True, is_staff=False, is_superuser=False)
            # With the real User model the profile signal reads this (3.2); historical models ignore it.
            user._access_profile = dict(kind="system", system_code=code, display_name_en=en, display_name_ar=ar)
            user.save()
            profile = UserProfile.objects.filter(user=user).first()
            if profile is None:
                UserProfile.objects.create(
                    user=user, kind="system", system_code=code, display_name_en=en, display_name_ar=ar,
                )
            elif (profile.kind, profile.system_code) != ("system", code):
                raise SeedConflict(f"New system user {username!r} got a {profile.kind} profile; expected system:{code}.")
        else:
            profile = UserProfile.objects.filter(user=user).first()
            if profile is None or (profile.kind, profile.system_code) != ("system", code):
                found = f"{profile.kind}:{profile.system_code or ''}" if profile else "no profile"
                raise SeedConflict(
                    f"Username {username!r} already exists and is not the {code} system user ({found}). "
                    "Rename that user before migrating; system users are never adopted from existing accounts."
                )
        # update(), not save(): no signals with historical models, and the flags are forced, not compared.
        User.objects.filter(pk=user.pk).update(password=make_password(None), is_staff=False, is_superuser=False)
