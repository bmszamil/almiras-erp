"""2FA self-enrolment (D21 b and review): after a dual-controlled 2FA reset, or after being granted the
Managing Partner or Chairman role, the user signs in here with his current password, sets his own
password if others set the current one, then enrols a new authenticator app. The secret is shown only
to him (Cache-Control: no-store). He is not signed in here; afterwards he signs in normally.
"""
from django.contrib.auth import authenticate
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

from . import services
from .errors import AccessDenied, RuleError
from .models import AuditLog, User

SESSION_KEY = "erp_totp_enrolment"
NOT_PENDING = "Sign-in failed or no enrolment is pending. / فشل تسجيل الدخول أو لا يوجد تسجيل معلق."


def _ctx(request):
    return services.Context.from_request(request, channel=AuditLog.Channel.WEB)


def _page(request, **data):
    return render(request, "access/enrol_totp.html", data)


def _start_device(request, user):
    device = services.start_self_enrolment(user, ctx=_ctx(request))
    request.session[SESSION_KEY] = {"user": user.pk, "stage": "device", "device": device.pk}
    return _page(request, device=device)


@never_cache
@sensitive_post_parameters("password", "new_password1", "new_password2", "token")
def enrol_totp(request):
    pending = request.session.get(SESSION_KEY) or {}
    user = User.objects.filter(pk=pending.get("user"), is_active=True).first() if pending else None
    if request.method != "POST":
        return _page(request)

    if user is not None and pending.get("stage") == "password" and "new_password1" in request.POST:
        if request.POST.get("new_password1") != request.POST.get("new_password2"):
            return _page(request, set_password=True, error="The passwords do not match. / كلمتا المرور غير متطابقتين.")
        try:
            services.set_own_password(user, request.POST["new_password1"], ctx=_ctx(request))
        except RuleError as exc:
            return _page(request, set_password=True, error=f"{exc.message_en} / {exc.message_ar}")
        user.refresh_from_db()
        try:
            return _start_device(request, user)
        except AccessDenied:
            request.session.pop(SESSION_KEY, None)
            return _page(request, error=NOT_PENDING)

    if user is not None and pending.get("stage") == "device" and "token" in request.POST:
        from django_otp.plugins.otp_totp.models import TOTPDevice

        device = TOTPDevice.objects.filter(pk=pending.get("device"), user=user, confirmed=False).first()
        if device is None:
            request.session.pop(SESSION_KEY, None)
            return _page(request, error="This enrolment is no longer valid. Start again. / انتهت صلاحية هذا التسجيل.")
        try:
            ok = services.finish_self_enrolment(user, device, request.POST.get("token", "").strip(), ctx=_ctx(request))
        except AccessDenied:
            request.session.pop(SESSION_KEY, None)
            return _page(request, error=NOT_PENDING)
        if ok:
            request.session.pop(SESSION_KEY, None)
            return _page(request, done=True)
        return _page(request, device=device, error="The code is not correct. / الرمز غير صحيح.")

    # Step 0: prove the current password.
    request.session.pop(SESSION_KEY, None)
    try:
        user = authenticate(request, username=request.POST.get("username", ""),
                            password=request.POST.get("password", ""), enrolment=True)
    except PermissionDenied:
        user = None
    if user is None or not user.is_human or not user.totp_enrolment_required:
        return _page(request, error=NOT_PENDING)  # same message either way: reveals nothing
    request.session.cycle_key()
    if user.password_set_by_others:
        request.session[SESSION_KEY] = {"user": user.pk, "stage": "password"}
        return _page(request, set_password=True)
    try:
        return _start_device(request, user)
    except AccessDenied:
        return _page(request, error=NOT_PENDING)
