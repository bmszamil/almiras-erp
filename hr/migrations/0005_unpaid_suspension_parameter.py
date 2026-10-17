"""hr R62: the unpaid-leave suspension threshold becomes its own parameter ``leave.unpaid.suspension_after_days``
(hr.md 3.9). Until now it sat inside ``leave.annual`` (``unpaid_suspends_after_days``) and ``eosb.award_table``
(``exclude_unpaid_days_beyond``).

1. For every existing ``leave.annual`` (or, failing that, ``eosb.award_table``) row carrying the threshold, a
   ``leave.unpaid.suspension_after_days`` row is created with the same jurisdiction, dates and figure, and the
   same verification state (status, entered_by, verified_by, verified_at are copied, so an unverified row stays
   unverified and nothing is verified by this migration).
2. The old keys are removed from rows no approval has used; a used row is never edited (R7) and keeps the key,
   which no service reads any more.
3. ``statutory_seed.apply`` then adds the new seed codes and refreshes seeded rows that are still unverified and
   unused (it never touches verified or used rows).
"""
from django.db import migrations

from hr import statutory_seed

NEW_CODE = "leave.unpaid.suspension_after_days"
OLD_KEYS = (("leave.annual", "unpaid_suspends_after_days"), ("eosb.award_table", "exclude_unpaid_days_beyond"))


def forward(apps, schema_editor):
    Parameter = apps.get_model("hr", "StatutoryParameter")
    for code, key in OLD_KEYS:
        for row in Parameter.objects.filter(code=code).order_by("id"):
            value = row.value or {}
            if key not in value:
                continue
            exists = Parameter.objects.filter(jurisdiction=row.jurisdiction, code=NEW_CODE,
                                              valid_from=row.valid_from).exists()
            if not exists:
                ref = (statutory_seed.reference(NEW_CODE) if row.jurisdiction == "SA"
                       else f"Moved from {code} ({row.source_reference})"[:300])
                Parameter.objects.create(
                    jurisdiction=row.jurisdiction, code=NEW_CODE,
                    value={"days": str(value[key]), "aggregation": "work_year"},
                    valid_from=row.valid_from, valid_to=row.valid_to, source_url=row.source_url,
                    source_reference=ref, status=row.status, entered_by_id=row.entered_by_id,
                    verified_by_id=row.verified_by_id, verified_at=row.verified_at,
                )
            if row.used_at is None:
                row.value = {k: v for k, v in value.items() if k != key}
                row.save(update_fields=["value"])
    statutory_seed.apply(Parameter)


class Migration(migrations.Migration):
    dependencies = [("hr", "0004_contract_calendar_basis_ref")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
