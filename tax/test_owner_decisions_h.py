"""Owner decisions of 2026-10-19 (`docs/approvals/owner-answers-2026-10-18.md` section H) in the VAT slice:
H3 (box verification: the accountant proposes, the approver verifies; OQ-46). Anonymised data."""
import pytest

from core import access
from core.access.models import AccessLog, Role, RolePermission
from core.access.seed import ROLE_SEED, ROLE_SEED_V11, ROLE_SEED_V12, apply_seed
from core.access.testing import make_agent, make_user, member
from tax import services
from tax.models import TaxError, VatBoxDefinition

pytestmark = pytest.mark.django_db


# --- H3: VAT box verification (OQ-46) ---------------------------------------------------------------------------

def test_h3_seed_v12_gives_the_accountant_the_proposal_and_keeps_verification_with_the_approver():
    assert ROLE_SEED is ROLE_SEED_V12
    perms = {code: set(row[5]) for code, row in ROLE_SEED.items()}
    assert "tax.propose_box_verification" in perms["accountant"]
    assert "tax.maintain_rates" not in perms["accountant"]
    assert "tax.maintain_rates" in perms["approver"] and "tax.propose_box_verification" not in perms["approver"]
    assert {c for c, p in perms.items() if "tax.propose_box_verification" in p} == {"accountant"}
    stored = set(RolePermission.objects.filter(role__code="accountant").values_list("codename", flat=True))
    assert "tax.propose_box_verification" in stored  # migration core.0021 applied the seed
    apply_seed(Role, RolePermission, ROLE_SEED_V11)  # what core.0021 reverses to
    assert not RolePermission.objects.filter(codename="tax.propose_box_verification").exists()
    apply_seed(Role, RolePermission, ROLE_SEED_V12)
    assert RolePermission.objects.filter(role__code="accountant", codename="tax.propose_box_verification").exists()


def test_h3_accountant_proposes_and_cfo_verifies_logged(t):
    definition = VatBoxDefinition.objects.get(box_no=2)
    proposed = services.propose_box_verification(definition, user=t.a1, entity=t.alm)
    assert proposed.proposed_by == t.a1 and proposed.verified_at is None
    log = AccessLog.objects.get(actor=t.a1, details__action="propose_box_verification")
    assert log.permission == "tax.propose_box_verification" and log.details["box"] == 2
    done = services.verify_box_definition(definition, user=t.c1, entity=t.alm)
    assert (done.proposed_by, done.verified_by) == (t.a1, t.c1)
    assert AccessLog.objects.get(actor=t.c1, details__action="verify_box_definition").permission == "tax.maintain_rates"
    with pytest.raises(TaxError) as exc:
        services.propose_box_verification(definition, user=t.a2, entity=t.alm)
    assert exc.value.code == "already_verified"


def test_h3_the_approver_cannot_propose_and_the_accountant_cannot_verify(t):
    definition = VatBoxDefinition.objects.get(box_no=3)
    with pytest.raises(access.AccessDenied) as exc:
        services.propose_box_verification(definition, user=t.c1, entity=t.alm)
    assert exc.value.code != "self_approval"
    services.propose_box_verification(definition, user=t.a1, entity=t.alm)
    for user in (t.a1, t.a2):
        with pytest.raises(access.AccessDenied):
            services.verify_box_definition(definition, user=user, entity=t.alm)
    assert VatBoxDefinition.objects.get(pk=definition.pk).verified_at is None


def test_h3_one_person_holding_both_roles_never_verifies_their_own_proposal(t):
    both = make_user("both")
    member(both, t.alm, "accountant")
    member(both, t.alm, "approver")
    definition = VatBoxDefinition.objects.get(box_no=4)
    services.propose_box_verification(definition, user=both, entity=t.alm)
    with pytest.raises(access.AccessDenied) as exc:
        services.verify_box_definition(definition, user=both, entity=t.alm)
    assert exc.value.code == "self_approval"
    assert services.verify_box_definition(definition, user=t.c1, entity=t.alm).verified_by == t.c1


def test_h3_proposals_only_at_the_group_parent_and_never_by_an_agent(t):
    definition = VatBoxDefinition.objects.get(box_no=5)
    with pytest.raises(TaxError) as exc:
        services.propose_box_verification(definition, user=t.sub_a, entity=t.sub)
    assert exc.value.code == "group_parent_only"
    agent = make_agent("box_reader")
    with pytest.raises(access.AccessDenied):
        services.propose_box_verification(definition, user=agent, entity=t.alm)
    assert VatBoxDefinition.objects.get(pk=definition.pk).proposed_by is None
