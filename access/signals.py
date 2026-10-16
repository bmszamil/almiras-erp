"""R19: sign-ins are audited."""
from .models import AuditLog, User
from .services import Context, audit, system_user


def on_sign_in(sender, request, user, **kwargs):
    ctx = Context.from_request(request, channel=AuditLog.Channel.WEB) if request is not None else Context()
    audit(user, "access.sign_in", user, ctx=ctx, after={"second_factor": bool(getattr(user, "otp_device", None))})


def on_sign_in_failed(sender, credentials, request=None, **kwargs):
    username = (credentials or {}).get("username", "")
    ctx = Context.from_request(request, channel=AuditLog.Channel.WEB) if request is not None else Context()
    target = User.objects.filter(username=username).first()
    audit(system_user(), "access.sign_in_failed", target, ctx=ctx, object_type="access.user",
          object_id=str(target.pk) if target else "", after={"username": username[:150]})
