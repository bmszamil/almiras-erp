"""Billing permissions in the access catalogue and seed V6 (billing §7 as amended 2026-10-17; access A11, R12, R13)."""
from core.access import catalogue
from core.access.models import Role, RolePermission
from core.access.seed import ROLE_SEED


def _held(role):
    return set(RolePermission.objects.filter(role__code=role).values_list("codename", flat=True))


def test_billing_permissions_in_catalogue(db):
    assert catalogue.get("billing.approve_version").is_approval
    assert catalogue.get("invoicing.create_adjustment_from_billing").system == "billing"
    assert "billing.create_credit_note_draft" not in catalogue.CATALOGUE  # withdrawn (pre-D9)
    for code in ("billing.edit_version_draft", "billing.review_correction", "billing.recompute_corrections"):
        perm = catalogue.get(code)
        assert not perm.agent_ok and perm.label_ar  # agents never (OQ-28 default); bilingual labels
    assert "ledger.view_rates" in catalogue.allowed_for_system("billing")


def test_seed_v6_holders(db):
    assert {"billing.edit_version_draft", "billing.review_correction", "billing.recompute_corrections"} <= _held(
        "accountant")
    assert {"billing.approve_version", "billing.approve_contract", "billing.approve_termination",
            "billing.recompute_corrections"} <= _held("approver")
    assert "billing.review_correction" not in _held("approver")  # the reviewer is not the approver (R44)
    assert "billing.review_correction" in _held("receivables_clerk")
    assert "billing.approve_version" in _held("managing_partner")
    assert _held("system_billing") == {"invoicing.create_from_billing", "invoicing.create_adjustment_from_billing",
                                       "billing.issue_from_contract", "billing.start_run", "ledger.view_rates"}
    assert not RolePermission.objects.filter(codename="billing.create_credit_note_draft").exists()
    assert set(ROLE_SEED) <= set(Role.objects.values_list("code", flat=True))
