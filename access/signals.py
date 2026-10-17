"""R19 and compliance: sign-in, failed sign-in and sign-out are audited."""
from .models import AuditLog, User
from .services import Context, audit, system_user


def _ctx(request):
    return Context.from_request(request, channel=AuditLog.Channel.WEB) if request is not None else Context()


def on_sign_in(sender, request, user, **kwargs):
    audit(user, "access.sign_in", user, ctx=_ctx(request),
          after={"second_factor": bool(getattr(user, "otp_device", None))})


def on_sign_in_failed(sender, credentials, request=None, **kwargs):
    from .throttle import is_locked_out

    username = (credentials or {}).get("username", "") or ""
    target = User.objects.filter(username=username).first()
    ctx = _ctx(request)
    # Review E: attempts refused while locked are recorded but do not extend the lockout.
    action = "access.sign_in_locked" if is_locked_out(username, ctx.ip) else "access.sign_in_failed"
    audit(system_user(), action, None, ctx=ctx, object_type="access.user",
          object_id=str(target.pk) if target else "", after={"username": username[:150]})


def on_sign_out(sender, request, user, **kwargs):
    if user is None or not getattr(user, "pk", None):
        return
    audit(user, "access.sign_out", user, ctx=_ctx(request))
