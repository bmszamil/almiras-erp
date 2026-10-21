"""Retire the R16 CFO delegate memberships (access §10, R62, R87, R88; review of 92c35c2, item 2; verification review
of 49e1abd, item 4).

Every still-running membership with ``delegated_from`` whose source membership is itself still active becomes an
``ApprovalDelegation`` from the source membership's holder to the delegate:

- it starts now at the earliest (``max(valid_from, now)``) and ends no later than 90 days after that (OQ-45);
- it covers only the approval activities whose permission the source role holds (R16 delegated that role's
  approvals, nothing more);
- the membership is revoked now with a reason (its single revocation write, allowed by the R33 trigger).

A delegate membership whose source was already revoked or ended gave nothing before this migration and is left as
it is (not revived). Each conversion is logged (``delegation_granted`` and ``delegation_revoked``) with the system
actor kind and source ``migration:core.0031``; the delegation's ``created_by`` is the user who granted the old
membership (else the delegator), because the field needs a person. Ended or revoked delegate memberships are left as
they are. Nothing is converted back on reversal (the delegations stay as records).
"""
from datetime import timedelta

from django.db import migrations
from django.db.models import Q
from django.utils import timezone

MAX_DAYS = 90
SOURCE = "migration:core.0031"


def _active(qs, at):
    return qs.filter(revoked_at__isnull=True, valid_from__lte=at).filter(Q(valid_to__isnull=True) | Q(valid_to__gt=at))


def convert(apps, schema_editor):
    Membership = apps.get_model("core", "Membership")
    ApprovalDelegation = apps.get_model("core", "ApprovalDelegation")
    ApprovalActivity = apps.get_model("core", "ApprovalActivity")
    RolePermission = apps.get_model("core", "RolePermission")
    AccessLog = apps.get_model("core", "AccessLog")
    now = timezone.now()
    legacy = Membership.objects.filter(delegated_from__isnull=False, revoked_at__isnull=True, valid_to__gt=now)
    for m in legacy.select_related("delegated_from"):
        src = m.delegated_from
        if not _active(Membership.objects.filter(pk=src.pk), now).exists():
            continue  # its source had ended: the delegate held nothing (verification review item 4)
        start = max(m.valid_from, now)
        end = min(m.valid_to, start + timedelta(days=MAX_DAYS))
        if end <= start:
            continue
        perms = set(RolePermission.objects.filter(role_id=src.role_id).values_list("codename", flat=True))
        activities = list(ApprovalActivity.objects.filter(permission__in=perms))
        d = ApprovalDelegation.objects.create(
            entity_id=m.entity_id, delegator_id=src.user_id, delegate_id=m.user_id, valid_from=start, valid_to=end,
            reason_en=m.reason or "Converted R16 delegation", reason_ar=m.reason or "تفويض محوَّل",
            created_by_id=m.granted_by_id or src.user_id, created_at=now)
        if activities:
            d.activities.set(activities)
        reason = "Converted to an approval delegation (access R62, core.0031) / حُوِّل إلى تفويض اعتماد"
        Membership.objects.filter(pk=m.pk).update(revoked_at=timezone.now(), revoked_by_id=d.created_by_id,
                                                  revoke_reason=reason)
        for event, details in (("delegation_granted", {"approval_delegation": d.pk, "converted_from": m.pk,
                                                       "activities": sorted(a.code for a in activities)}),
                               ("delegation_revoked", {"membership": m.pk, "reason": reason})):
            AccessLog.objects.create(at=now, actor_id=None, actor_kind="system", event=event, entity_id=m.entity_id,
                                     subject_user_id=m.user_id, permission="core.delegate_approval", details=details,
                                     source=SOURCE)


class Migration(migrations.Migration):
    dependencies = [("core", "0030_authority_matrix_triggers")]

    operations = [migrations.RunPython(convert, migrations.RunPython.noop)]
