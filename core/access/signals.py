"""User signals: profiles for every user (3.2) and superuser / staff flag rules (R25)."""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import AccessLog, Kind, UserProfile

User = get_user_model()


def _pending_kind(user):
    if user.pk:
        profile = UserProfile.objects.filter(user_id=user.pk).first()
        if profile:
            return profile.kind
    return getattr(user, "_access_profile", {}).get("kind", Kind.HUMAN)


@receiver(pre_save, sender=User)
def guard_flags(sender, instance, raw=False, **kwargs):
    if raw:
        return
    if _pending_kind(instance) != Kind.HUMAN and (instance.is_superuser or instance.is_staff):
        raise ValidationError(  # R25
            "System and agent users can never be superuser or staff. / "
            "لا يجوز أن يكون مستخدمو النظام والوكلاء مشرفين أو من فريق الإدارة."
        )
    before = None
    if instance.pk:
        before = User.objects.filter(pk=instance.pk).values("is_superuser", "is_staff").first()
    instance._access_flags_before = before


@receiver(post_save, sender=User)
def profile_and_flag_log(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    if created and not UserProfile.objects.filter(user_id=instance.pk).exists():
        extra = dict(getattr(instance, "_access_profile", {}))
        extra.setdefault("kind", Kind.HUMAN)
        UserProfile.objects.create(user=instance, **extra)
    before = getattr(instance, "_access_flags_before", None) or {"is_superuser": False, "is_staff": False}
    actor = getattr(instance, "_access_actor", None)
    from . import services

    for flag, event in (("is_superuser", AccessLog.Event.SUPERUSER_FLAG_CHANGED), ("is_staff", AccessLog.Event.STAFF_FLAG_CHANGED)):
        new = getattr(instance, flag)
        if before[flag] != new:
            services.log(event, actor=actor, subject_user=instance, details={"field": flag, "before": before[flag], "after": new})
