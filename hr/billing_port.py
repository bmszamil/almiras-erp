"""The hand-off from HR to the (not yet built) ``billing`` module for ended secondments (hr R24 to R26;
billing.md 3.6).

billing.md names ``billing.services.end_secondment(entity, assignment, end_date, user, source)``, idempotent
(the same end date twice is a no-op). Until billing exists, HR talks to a port of that shape:

- ``linked_assignment(entity, hr_assignment_ref)``: the billing ``SecondmentAssignment`` whose
  ``hr_assignment_ref`` equals the HR reference, or None when nothing is linked.
- ``end_secondment(entity, billing_assignment, end_date, user, source)``: applies the end date in billing.
  Raising any exception means "not delivered, retry later" (R25).
- ``assignment_id(billing_assignment)``: the id stored on the HR event.

The port is chosen by the setting ``HR_BILLING_PORT`` (dotted path). The default,
:class:`UnlinkedBillingPort`, has no billing records, so every event is delivered with nothing to do (R25:
"no billing link"). When billing is built, its port replaces the default; which user and permission billing
accepts for ``source="hr_event"`` is OQ-16. Tests use ``hr.testing.FakeBillingPort``.
"""
from typing import Protocol

from django.conf import settings
from django.utils.module_loading import import_string

DEFAULT_PORT = "hr.billing_port.UnlinkedBillingPort"


class BillingPort(Protocol):
    def linked_assignment(self, entity, hr_assignment_ref): ...

    def end_secondment(self, entity, billing_assignment, end_date, user, source) -> None: ...

    def assignment_id(self, billing_assignment) -> str: ...


class UnlinkedBillingPort:
    """Billing is not built: no secondment is linked, nothing is called."""

    def linked_assignment(self, entity, hr_assignment_ref):
        return None

    def end_secondment(self, entity, billing_assignment, end_date, user, source):  # pragma: no cover - never linked
        raise RuntimeError("billing is not built")

    def assignment_id(self, billing_assignment):  # pragma: no cover
        return ""


_override = None  # tests only: hr.testing.fake_billing installs itself here


def get_port():
    if _override is not None:
        return _override
    return import_string(getattr(settings, "HR_BILLING_PORT", DEFAULT_PORT))()
