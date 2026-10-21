"""Owner decisions of 2026-10-19 (`docs/approvals/owner-answers-2026-10-18.md` section H) in the VAT slice:
H2 (sales booked at the SAMA tax-point rate: the output rate difference is for legacy rows only), H3 (box
verification: the accountant proposes, the approver verifies; OQ-46) and H4 (a correction without a correcting
entry accepted by the reviewer with a written bilingual reason; OQ-45). Anonymised data."""
from datetime import date

import pytest

from core import access
from core.access.models import AccessLog, Role, RolePermission
from core.access.seed import ROLE_SEED, ROLE_SEED_V11, ROLE_SEED_V12, apply_seed
from core.access.testing import make_agent, make_user, member
from tax import services
from tax.conftest import D, sale
from tax.models import TaxError, VatBoxDefinition

pytestmark = pytest.mark.django_db
Q1 = date(2027, 1, 1)


# --- H2: one rate per sales document; the output rate difference only for legacy rows (§10.5, §10.6) ----------

def _usd_sale_supplied_three_weeks_before_issue(t):
    from invoicing.conftest import usd_rate

    usd_rate(t, "3.7500", on=date(2027, 3, 10))
    usd_rate(t, "3.7510", on=date(2027, 3, 31))
    return sale(t, [("rent_commercial", "1", "1000.00")], on=date(2027, 3, 31), currency="USD",
                supply_date=date(2027, 3, 10))


def test_h2_a_sale_booked_at_its_sama_tax_point_row_ties_out_without_a_rate_difference(t):
    from tax.conftest import box

    doc = _usd_sale_supplied_three_weeks_before_issue(t)
    assert doc.fx_exchange_rate_id == doc.vat_sar_exchange_rate_id and doc.fx_rate_source == "sama_tp"
    q1 = services.compute_draft(t.alm, Q1, user=t.a1)
    assert box(q1, 1)[2] == D("562.50") == doc.total_vat_sar
    tie = services.tie_out(q1, user=t.a1)
    assert (tie["rate_difference_output"], tie["ledger_output"], tie["difference_output"]) == (
        D("0"), D("562.50"), D("0"))


def test_h2_a_legacy_sale_booked_at_another_rate_row_keeps_the_explained_rate_difference(t, monkeypatch):
    """A document approved before H2 booked at the issue-date row (3.7510) while its VAT used the SAMA tax-point
    row (3.7500): the tie-out still explains the 0.15 on 2200 (§10.5), for such rows only."""
    from invoicing import services as invoicing

    def before_h2(entity, doc, sama_row, user):  # the booking rule as it was until 2026-10-18
        return invoicing._fx_for(entity, doc.currency_id, doc.issue_date, user)

    monkeypatch.setattr(invoicing, "_booking_rate", before_h2)
    doc = _usd_sale_supplied_three_weeks_before_issue(t)
    assert doc.fx_exchange_rate_id != doc.vat_sar_exchange_rate_id and doc.fx_rate == D("3.7510")
    q1 = services.compute_draft(t.alm, Q1, user=t.a1)
    tie = services.tie_out(q1, user=t.a1)
    assert (tie["rate_difference_output"], tie["difference_output"]) == (D("0.15"), D("0"))


def test_h2_no_rate_difference_is_explained_for_a_document_booked_at_its_sama_row(t):
    """The difference is computed only when the booking row differs from the SAMA row; otherwise any 2200 gap
    stays a blocking tie-out difference."""
    doc = _usd_sale_supplied_three_weeks_before_issue(t)
    comp = services.Computation()
    services._sales_sources(comp, t.alm, doc.journal_entry, doc, 1)
    assert comp.output_rate_differences == []


# --- H3: VAT box verification (OQ-46) ---------------------------------------------------------------------------

def test_h3_seed_v12_gives_the_accountant_the_proposal_and_keeps_verification_with_the_approver():
    assert set(ROLE_SEED_V12["accountant"][5]) <= set(ROLE_SEED["accountant"][5])  # V13 (payroll) keeps V12
    perms = {code: set(row[5]) for code, row in ROLE_SEED.items()}
    assert "tax.propose_box_verification" in perms["accountant"]
    assert "tax.maintain_rates" not in perms["accountant"]
    assert "tax.maintain_rates" in perms["approver"] and "tax.propose_box_verification" not in perms["approver"]
    # access §10 seed V17 (A28): the ceo also holds the accountant's rights; it may hold both steps, but tax.services
    # still refuses a verifier who proposed (H3) and the verification is never self-approved (NSA, R80 b).
    assert {c for c, p in perms.items() if "tax.propose_box_verification" in p} == {"accountant", "ceo"}
    stored = set(RolePermission.objects.filter(role__code="accountant").values_list("codename", flat=True))
    assert "tax.propose_box_verification" in stored  # migration core.0021 applied the seed
    apply_seed(Role, RolePermission, ROLE_SEED_V11)  # what core.0021 reverses to (roles of V11; V17's ceo is later)
    assert not RolePermission.objects.filter(codename="tax.propose_box_verification",
                                             role__code__in=ROLE_SEED_V11).exists()
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


# --- H4: correction without a correcting entry, ledger already correct (OQ-45) ----------------------------------

def _q4_2026_filed(t):
    from tax.conftest import imported

    return imported(t, date(2026, 10, 1), ref="OLD", filed_on=date(2027, 1, 20))


def _correction(t, user=None):
    return services.record_correction(_q4_2026_filed(t), user=user or t.a1, discovered_on=date(2027, 2, 1),
                                      description_en="Filed return left out VAT already booked on 2200",
                                      description_ar="الإقرار المقدم أغفل ضريبة مقيدة في 2200",
                                      tax_difference_sar="100.00")


def _accept(t, ret, corr, user, **reasons):
    reasons.setdefault("reason_en", "2200 already holds the VAT; only the filed return was wrong")
    reasons.setdefault("reason_ar", "الحساب 2200 يتضمن الضريبة؛ الخطأ في الإقرار المقدم فقط")
    return services.acknowledge_exception(ret, user=user, kind="correction_without_entry",
                                          reference=f"corr:{corr.pk}", decision="ledger_already_correct", **reasons)


def test_h4_the_reviewer_accepts_a_correction_without_entry_with_a_bilingual_reason_and_it_is_logged(t):
    from tax.conftest import close_q1, confirm_tax_points, prepared

    corr = _correction(t)
    ret = prepared(t)
    found = {e["kind"]: e for e in services.exceptions(ret, user=t.a2)}
    assert found["correction_without_entry"]["ack"] is None
    with pytest.raises(TaxError) as exc:
        services.complete_review(ret, user=t.a2)
    assert exc.value.code == "exceptions_open"
    with pytest.raises(TaxError) as exc:
        _accept(t, ret, corr, t.a2, reason_en="")
    assert exc.value.code == "reason_bilingual_required" and "بالإنجليزية والعربية" in " ".join(exc.value.messages)
    with pytest.raises(TaxError) as exc:  # the Arabic reason is required for every acknowledgement
        _accept(t, ret, corr, t.a2, reason_ar=" ")
    assert exc.value.code == "reason_required"
    ack = _accept(t, ret, corr, t.a2)
    assert (ack.decision, ack.acknowledged_by, ack.reason_en[:4], ack.reason_ar[:6]) == (
        "ledger_already_correct", t.a2, "2200", "الحساب")
    log = AccessLog.objects.get(actor=t.a2, details__action="accept_correction_without_entry")
    assert log.details["correction"] == corr.pk and log.details["reason_ar"] == ack.reason_ar
    assert log.details["difference"] == "100.00"
    confirm_tax_points(t, ret)
    assert services.complete_review(ret, user=t.a2).status == "reviewed"
    tie = services.tie_out(ret, user=t.a2)
    assert (tie["difference_output"], tie["difference_input"]) == (0, 0)
    close_q1(t)
    done = services.approve_return(ret, user=t.c1)
    assert done.status == "approved" and done.boxes.get(box_no=14).vat_sar == D("100.00")


def test_h4_the_recorder_of_the_correction_never_accepts_or_reviews_it(t):
    from tax.conftest import prepared

    corr = _correction(t, user=t.a2)  # recorded by a2; the return is prepared by a1
    ret = prepared(t)
    with pytest.raises(access.AccessDenied) as exc:
        _accept(t, ret, corr, t.a2)
    assert exc.value.code == "self_approval"
    _accept(t, ret, corr, t.c1)  # the approver role may review; then it may not approve
    with pytest.raises(access.AccessDenied) as exc:
        services.complete_review(ret, user=t.a2)
    assert exc.value.code == "self_approval"


def test_h4_three_people_the_approver_is_neither_the_recorder_nor_the_acceptor(t):
    from tax.conftest import close_q1, confirm_tax_points, prepared

    c2 = make_user("c2")
    member(c2, t.alm, "accountant")
    member(c2, t.alm, "approver")
    corr = _correction(t, user=c2)  # c2 records the correction; a1 prepares; c1 accepts; a2 completes the review
    ret = prepared(t)
    _accept(t, ret, corr, t.c1)
    confirm_tax_points(t, ret)
    ret = services.complete_review(ret, user=t.a2)
    close_q1(t)
    for user in (c2, t.c1):  # the recorder, then the acceptor
        with pytest.raises(access.AccessDenied) as exc:
            services.approve_return(ret, user=user)
        assert exc.value.code == "self_approval"
    c3 = make_user("c3")
    member(c3, t.alm, "approver")
    assert services.approve_return(ret, user=c3).status == "approved"


def test_h4_only_the_ledger_already_correct_decision_and_never_by_an_agent_or_preparer(t):
    from tax.conftest import prepared

    corr = _correction(t)
    ret = prepared(t)
    with pytest.raises(TaxError) as exc:
        services.acknowledge_exception(ret, user=t.a2, kind="correction_without_entry", reference=f"corr:{corr.pk}",
                                       decision="not_return_relevant", reason_ar="سبب", reason_en="reason")
    assert exc.value.code == "decision_invalid"
    with pytest.raises(access.AccessDenied):
        _accept(t, ret, corr, t.a1)  # the preparer
    with pytest.raises(access.AccessDenied):
        _accept(t, ret, corr, make_agent("vat_reviewer"))
    assert not ret.exception_acks.exists()
