"""Notifications (R21): in-app always, email if the user has an address, push only as a hook.

Email is sent after the transaction commits, so a rolled-back approval never emails anyone, and
email failure never blocks the approval. Failed emails are retried by `retry_failed_emails`
(management command `send_notifications`, run by cron). Email carries only the bilingual summary
and a link (R23).
"""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import Notification, PushDevice

log = logging.getLogger("approvals.notify")

MAX_EMAIL_ATTEMPTS = 3


def notify(user, kind, *, title_en, title_ar, body_en="", body_ar="", link="", entity=None, alert_code=""):
    channels = {"in_app": {"status": "sent"}}
    channels["email"] = {"status": "pending", "attempts": 0} if user.email else {"status": "skipped"}
    has_device = PushDevice.objects.filter(user=user, is_active=True).exists()
    # Push is a hook only in this wave (D4): recorded, never sent.
    channels["push"] = {"status": "hook_inactive" if has_device else "skipped"}
    n = Notification.objects.create(
        user=user, entity=entity, kind=kind, alert_code=alert_code,
        title_en=title_en, title_ar=title_ar, body_en=body_en, body_ar=body_ar,
        link=link, channels_sent=channels,
    )
    if user.email:
        transaction.on_commit(lambda: send_email(n.pk))
    return n


def send_email(notification_id):
    n = Notification.objects.select_related("user").get(pk=notification_id)
    email = dict(n.channels_sent.get("email", {}))
    if email.get("status") not in {"pending", "failed"}:
        return n
    lang = n.user.language
    attempts = email.get("attempts", 0) + 1
    try:
        send_mail(
            subject=n.title(lang),
            message=f"{n.body(lang)}\n\n{getattr(settings, 'ERP_BASE_URL', '')}{n.link}",
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[n.user.email],
        )
        email = {"status": "sent", "attempts": attempts, "at": timezone.now().isoformat()}
    except Exception as exc:  # never block on email (R21)
        status = "failed" if attempts < MAX_EMAIL_ATTEMPTS else "gave_up"
        email = {"status": status, "attempts": attempts, "error": str(exc)[:300], "at": timezone.now().isoformat()}
        log.warning("Email for notification %s failed (attempt %s): %s", n.pk, attempts, exc)
    channels = dict(n.channels_sent)
    channels["email"] = email
    Notification.objects.filter(pk=n.pk).update(channels_sent=channels)
    n.channels_sent = channels
    return n


def retry_failed_emails():
    sent = 0
    for n in Notification.objects.filter(channels_sent__email__status__in=["failed", "pending"]):
        if send_email(n.pk).channels_sent["email"]["status"] == "sent":
            sent += 1
    return sent


def mark_read(notification, user):
    if notification.user_id != user.pk:
        raise PermissionError
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])
    return notification
