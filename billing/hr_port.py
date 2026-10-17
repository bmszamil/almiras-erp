"""``BillingHrPort``: billing's implementation of ``hr.billing_port.BillingPort`` (billing.md R57).

The deployment sets ``HR_BILLING_PORT = "billing.hr_port.BillingHrPort"``. HR's delivery job calls it with the HR
user who ended the assignment. Any exception means "not delivered": HR keeps the event pending and retries
(hr R25), so this port raises on every refusal and never swallows an error; ``billing.services.end_secondment``
runs in one transaction, so a refusal leaves nothing behind.
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
