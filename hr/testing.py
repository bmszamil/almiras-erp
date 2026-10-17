"""Test helpers for HR: fake billing and payroll ports. Never used outside tests; nothing external is called."""
from contextlib import contextmanager

from . import billing_port, payroll_port


class FakeBillingPort:
    """Linked billing assignments by HR reference; ``fail_times`` makes the next calls raise."""

    def __init__(self):
        self.links = {}
        self.calls = []
        self.fail_times = 0

    def link(self, reference, billing_id="SA-1"):
        self.links[reference] = billing_id

    def linked_assignment(self, entity, hr_assignment_ref):
        return self.links.get(hr_assignment_ref)

    def end_secondment(self, entity, billing_assignment, end_date, user, source):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("billing unavailable (test)")
        self.calls.append((entity.code, billing_assignment, end_date, user.pk, source))

    def assignment_id(self, billing_assignment):
        return billing_assignment


class FakePayrollPort:
    def __init__(self):
        self.drafts = []
        self.approved = set()
        self.discarded = []
        self.balances = {}  # employee pk -> LoanBalances
        self.additional_wage = {}  # employee pk -> AdditionalWage

    def create_draft(self, settlement_input, user):
        self.drafts.append(settlement_input.pk)

    def is_approved(self, termination):
        return termination.pk in self.approved

    def discard_draft(self, termination, user):
        self.discarded.append(termination.pk)

    def outstanding_balances(self, employee, as_of):
        return self.balances.get(employee.pk, payroll_port.LoanBalances())

    def eosb_additional_wage(self, employee, as_of):
        return self.additional_wage.get(employee.pk, payroll_port.AdditionalWage())


@contextmanager
def fake_ports():
    billing, payroll = FakeBillingPort(), FakePayrollPort()
    prev = billing_port._override, payroll_port._override
    billing_port._override, payroll_port._override = billing, payroll
    try:
        yield billing, payroll
    finally:
        billing_port._override, payroll_port._override = prev
