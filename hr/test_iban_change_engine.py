"""The hr IBAN change through the one approval engine (access R61, R80 f, R82; review of 92c35c2, item 10).

``hr.manage_employee`` is not an **A** right, but its use as the IBAN change's second person goes through the engine
(activity ``hr.iban_change``) and is on the NSA list (row f): never self-approved, the top authority included. Made-up
names and IBANs."""
import pytest
from django.core.exceptions import PermissionDenied

from core.access import catalogue
from core.access.authority_models import ApprovalDecision, ApprovalRequest
from core.access.testing import make_user, member
from hr import services
from hr.conftest import hired
from hr.models import Employee, IbanChangeRequest

OLD = "SA0380000000608010167519"
NEW = "GB82WEST12345698765432"


def _request(w, by, emp=None):
    emp = emp or hired(w, iban=OLD, bank_name="Al Rajhi")
    return emp, services.request_iban_change(emp, user=by, iban=NEW, bank_name="West Bank", reason_ar="تغيير البنك")


def test_r61_the_iban_change_opens_an_engine_request_and_records_the_decision(w):
    emp, req = _request(w, w.h)
    open_ = ApprovalRequest.objects.get(document_type="hr.ibanchangerequest", document_id=req.pk)
    assert (open_.activity.code, open_.status, open_.rule_source) == ("hr.iban_change", "open", "default")
    services.approve_iban_change(req, user=w.h2)
    decision = ApprovalDecision.objects.get(request=open_)
    assert (decision.user, decision.decision) == (w.h2, "approved")
    assert ApprovalRequest.objects.get(pk=open_.pk).status == "approved"
    assert Employee.objects.get(pk=emp.pk).iban == NEW


def test_r80f_the_top_authority_never_approves_their_own_iban_request(w):
    """NSA row f: the MP (ALM's top authority) who requested the change is refused like anyone else."""
    assert "hr.manage_employee" in catalogue.NSA_ROWS["f"]
    member(w.mp, w.alm, "hr_officer")
    emp, req = _request(w, w.mp)
    with pytest.raises(PermissionDenied) as exc:
        services.approve_iban_change(req, user=w.mp)
    assert exc.value.code == "self_approval"
    assert IbanChangeRequest.objects.get(pk=req.pk).status == "pending"
    assert Employee.objects.get(pk=emp.pk).iban == OLD


def test_r82_the_employee_never_approves_their_own_iban_change(w):
    own = make_user("own_hr")
    member(own, w.alm, "hr_officer")
    emp = hired(w, iban=OLD, bank_name="Al Rajhi", linked_user=own)
    _, req = _request(w, w.h, emp)
    with pytest.raises(PermissionDenied) as exc:
        services.approve_iban_change(req, user=own)
    assert exc.value.code == "beneficiary"
    assert Employee.objects.get(pk=emp.pk).iban == OLD
