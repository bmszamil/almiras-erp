"""Signals other modules listen to without core importing them.

``entity_settings_changed`` is sent inside the transaction of ``core.services.change_entity_settings`` after the
entity row is saved, with ``entity``, ``user``, ``before`` and ``after`` ({field: value} of the changed fields
only). A receiver that raises rolls the change back. ``tax`` uses it to date a VAT return frequency change and
regenerate the return schedule (tax-returns R4, review item 6).
"""
from django.dispatch import Signal

entity_settings_changed = Signal()
