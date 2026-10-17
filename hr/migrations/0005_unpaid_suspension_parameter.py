"""hr R62: the unpaid-leave suspension threshold becomes its own parameter ``leave.unpaid.suspension_after_days``
(hr.md 3.9). Until now it sat inside ``leave.annual`` (``unpaid_suspends_after_days``) and ``eosb.award_table``
(``exclude_unpaid_days_beyond``).

1. For every existing ``leave.annual`` (or, failing that, ``eosb.award_table``) row carrying the threshold, a
   ``leave.unpaid.suspension_after_days`` row is created with the same jurisdiction, dates and figure. The new row
   is a new figure entered by this migration: it starts ``unverified`` with no ``entered_by`` and no verifier,
   whatever the old row's state (R7: nothing is verified by a loader; the managing partner verifies it).
2. The old keys are removed only from rows that are still pristine loader rows (unverified, unused, never
   verified); a verified or used row is never edited (R7) and keeps the key, which no service reads any more.

The migration is self-contained: it does not import the live seed module (``hr.statutory_seed``), so it does the
same thing whenever it runs. Seed rows themselves come from 0002 and the later seed migrations.
"""
from django.db import migrations

NEW_CODE = "leave.unpaid.suspension_after_days"
OLD_KEYS = (("leave.annual", "unpaid_suspends_after_days"), ("eosb.award_table", "exclude_unpaid_days_beyond"))
# Frozen copy of the SA source reference as it stood when this migration was written (2026-10-17).
SA_REFERENCE = ("Labour Law Art. 2, 116; Regs Art. 25 [Needs expert sign-off: legal, aggregation OQ-37] | Checked "
                "2026-10-17 against the HRSD text (docs/compliance/hr-payroll-labour-law-2026-10-17.md); MP "
                "verification pending (R7)")


def forward(apps, schema_editor):
    Parameter = apps.get_model("hr", "StatutoryParameter")
    for code, key in OLD_KEYS:
        for row in Parameter.objects.filter(code=code).exclude(status="superseded").order_by("id"):
            value = row.value or {}
            if key not in value:
                continue
            exists = (Parameter.objects.filter(jurisdiction=row.jurisdiction, code=NEW_CODE, valid_from=row.valid_from)
                      .exclude(status="superseded").exists())
            if not exists:
                ref = SA_REFERENCE if row.jurisdiction == "SA" else f"Moved from {code} ({row.source_reference})"[:300]
                Parameter.objects.create(
                    jurisdiction=row.jurisdiction, code=NEW_CODE,
                    value={"days": str(value[key]), "aggregation": "work_year"},
                    valid_from=row.valid_from, valid_to=row.valid_to, source_url=row.source_url,
                    source_reference=ref, status="unverified",
                )
            pristine = (row.status == "unverified" and row.used_at is None and row.verified_by_id is None
                        and row.verified_at is None)
            if pristine:
                row.value = {k: v for k, v in value.items() if k != key}
                row.save(update_fields=["value"])


class Migration(migrations.Migration):
    dependencies = [("hr", "0004_contract_calendar_basis_ref")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
