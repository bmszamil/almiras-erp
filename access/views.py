"""2FA self-enrolment after a dual-controlled reset (D21 b).

Step 1: the user proves his password (lockout backend, failures audited). Step 2: the page shows the new
authenticator secret to him only (Cache-Control: no-store); he confirms with a code. He is not signed in
here; afterwards he signs in normally with password + code. No approver ever sees the secret.
"""
from django.contrib.auth import authenticate
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

from . import services
from .errors import AccessDenied
from .models import AuditLog, User

SESSION_KEY = "erp_totp_enrolment"


def _ctx(request):
    return services.Context.from_request(request, channel=AuditLog.Channel.WEB)


@never_cache
@sensitive_post_parameters("password", "token")
def enrol_totp(request):
    data = {"error": ""}
    pending = request.session.get(SESSION_KEY)
    if request.method == "POST" and "token" in request.POST and pending:
        from django_otp.plugins.otp_totp.models import TOTPDevice

        user = User.objects.filter(pk=pending["user"]).first()
        device = TOTPDevice.objects.filter(pk=pending["device"], user=user, confirmed=False).first()
        if user is None or device is None:
            request.session.pop(SESSION_KEY, None)
            data["error"] = "This enrolment is no longer valid. Start again. / انتهت صلاحية هذا التسجيل. ابدأ من جديد."
        else:
            try:
                ok = services.finish_self_enrolment(user, device, request.POST.get("token", "").strip(), ctx=_ctx(request))
            except AccessDenied:
                ok = None
            if ok:
                request.session.pop(SESSION_KEY, None)
                return render(request, "access/enrol_totp.html", {"done": True})
            if ok is None:
                request.session.pop(SESSION_KEY, None)
                data["error"] = "No enrolment is pending. / لا يوجد تسجيل معلق."
            else:
                data.update(device=device, error="The code is not correct. / الرمز غير صحيح.")
                return render(request, "access/enrol_totp.html", data)
    elif request.method == "POST":
        request.session.pop(SESSION_KEY, None)
        try:
            user = authenticate(request, username=request.POST.get("username", ""),
                                password=request.POST.get("password", ""))
        except PermissionDenied:
            user = None
        try:
            if user is None:
                raise AccessDenied("bad_credentials", "", "")
            device = services.start_self_enrolment(user, ctx=_ctx(request))
        except AccessDenied:
            data["error"] = ("Sign-in failed or no enrolment is pending. / "
                             "فشل تسجيل الدخول أو لا يوجد تسجيل معلق.")  # same message: reveals nothing
        else:
            request.session.cycle_key()
            request.session[SESSION_KEY] = {"user": user.pk, "device": device.pk}
            data["device"] = device
    return render(request, "access/enrol_totp.html", data)
