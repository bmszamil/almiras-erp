"""Retire the R16 CFO delegate memberships (access §10, R62, R87, R88; review of 92c35c2, item 2).

Every still-running membership with ``delegated_from`` becomes an ``ApprovalDelegation`` from the source
membership's holder to the delegate, ending no later than 90 days after it starts (OQ-45), and the membership is
revoked now with a reason (its single revocation write, allowed by the R33 trigger). Each conversion is logged
(``delegation_granted`` and ``delegation_revoked``). Ended or revoked delegate memberships are left as they are.
Nothing is converted back on reversal (the delegations stay as records).
"""
from datetime import timedelta

from django.db import migrations
from django.utils import timezone

MAX_DAYS = 90


def convert(apps, schema_editor):
    Membership = apps.get_model("core", "Membership")
    ApprovalDelegation = apps.get_model("core", "ApprovalDelegation")
    AccessLog = apps.get_model("core", "AccessLog")
    now = timezone.now()
    legacy = Membership.objects.filter(delegated_from__isnull=False, revoked_at__isnull=True, valid_to__gt=now)
    for m in legacy.select_related("delegated_from"):
        start = m.valid_from
        end = min(m.valid_to, max(start, now) + timedelta(days=MAX_DAYS))
        d = ApprovalDelegation.objects.create(
            entity_id=m.entity_id, delegator_id=m.delegated_from.user_id, delegate_id=m.user_id, valid_from=start,
            valid_to=end, reason_en=m.reason or "Converted R16 delegation", reason_ar=m.reason or "تفويض محوَّل",
            created_by_id=m.granted_by_id or m.delegated_from.user_id, created_at=now)
        reason = "Converted to an approval delegation (access R62, core.0031) / حُوِّل إلى تفويض اعتماد"
        Membership.objects.filter(pk=m.pk).update(revoked_at=timezone.now(), revoked_by_id=d.created_by_id,
                                                  revoke_reason=reason)
        for event, details in (("delegation_granted", {"approval_delegation": d.pk, "converted_from": m.pk}),
                               ("delegation_revoked", {"membership": m.pk, "reason": reason})):
            AccessLog.objects.create(at=now, actor_id=None, actor_kind="human", event=event, entity_id=m.entity_id,
                                     subject_user_id=m.user_id, permission="core.delegate_approval", details=details,
                                     source="migration")


class Migration(migrations.Migration):
    dependencies = [("core", "0030_authority_matrix_triggers")]

    operations = [migrations.RunPython(convert, migrations.RunPython.noop)]
