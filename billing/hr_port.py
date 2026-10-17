"""``BillingHrPort``: billing's implementation of ``hr.billing_port.BillingPort`` (billing.md R57).

The deployment sets ``HR_BILLING_PORT = "billing.hr_port.BillingHrPort"``. HR's delivery job calls it with the HR
user who ended the assignment. Any exception means "not delivered": HR keeps the event pending and retries
(hr R25), so this port raises on every refusal and never swallows an error; ``billing.services.end_secondment``
runs in one transaction, so a refusal leaves nothing behind.

Refusals are ``BillingError`` (en/ar message, ``code``) or ``core.access.AccessDenied``. One of them is not a
refusal of the event but "come back later": while a billing run for the entity is in progress (it holds the
entity's lock, R41), ``end_secondment`` raises ``BillingError`` with ``code="run_in_progress"`` and
``retryable=True``. A run can take longer than HR's 5 attempts at 5-minute intervals, so the HR delivery job
should retry such an error without counting it toward ``HR_ASSIGNMENT_EVENT_MAX_ATTEMPTS``. As built, hr counts
every exception (hr R25): that needs a change in ``hr.services.deliver_assignment_events`` (reported to the hr
owner, not made here). An assignment not linked to HR is refused with ``code="not_linked"``.
"""


class BillingHrPort:
    def linked_assignment(self, entity, hr_assignment_ref):
        """The ``SecondmentAssignment`` of ``entity`` linked to HR reference ``hr_assignment_ref``, or None."""
        from .models import SecondmentAssignment

        if not hr_assignment_ref:
            return None
        return SecondmentAssignment.objects.filter(entity=entity, hr_assignment_ref=hr_assignment_ref).first()

    def end_secondment(self, entity, billing_assignment, end_date, user, source):
        from . import services

        services.end_secondment(entity, billing_assignment, end_date, user=user, source=source)

    def assignment_id(self, billing_assignment):
        return str(billing_assignment.pk)
