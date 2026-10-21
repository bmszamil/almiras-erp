"""Web middleware (web.md R12, R17, R22, R27).

Runs after ``AuthenticationMiddleware``:

1. **Human-only sessions (R12).** A session that names a user the backend refuses (agent, system or inactive user;
   ``HumanModelBackend.get_user`` returns None) is flushed, so a forged session gives nothing.
2. **Timeouts (R17, OQ-8).** Idle timeout (15 min for users who need MFA, 30 min otherwise) and absolute timeout
   (10 h): the session is ended and the user signs in again.
3. **Language (R27).** The profile language once signed in; before, the cookie, then Accept-Language, then
   Arabic. Activated for the request; ``<html lang dir>`` comes from it. Django admin keeps its own language.
4. **MFA gate (R13, R14, slice 2).** A signed-in user who needs MFA (``web.mfa.gate``, read at every request) reaches
   only the enrolment and verification pages, the language switch and sign-out until the session is OTP-verified;
   anything else redirects to enrolment (no device yet) or verification. Nothing else runs, so nothing changes.
5. **Admin gate (R19).** ``/admin/`` is reached only through the web sign-in (an anonymous request goes to
   ``/login/``) and only with an OTP-verified session, for staff users and superusers alike; with
   ``WEB_ADMIN_SUPERUSERS_ONLY`` staff users who are not superusers are refused (OQ-10).
6. **No caching (R22).** Responses to signed-in users carry ``Cache-Control: no-store, private``.
"""
import time

from django.conf import settings
from django.contrib.auth import SESSION_KEY, logout
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import translation
from django.utils.cache import patch_cache_control
from django.utils.http import urlencode

from . import mfa, services

LOGIN_AT = "web_login_at"
SEEN_AT = "web_seen_at"
SEEN_RESOLUTION = 60  # seconds: the last-seen time is written at most once a minute, not on every request
# Paths a signed-in user who still owes an MFA code may reach (R14).
MFA_OPEN_PREFIXES = ("/mfa/", "/language/", "/logout/")


class WebMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        self._human_only(request)
        # R13: read once per request (one membership query) for the idle timeout and the MFA gate.
        request.web_mfa_required = mfa.requires_mfa(request.user) if request.user.is_authenticated else False
        self._timeouts(request)
        signed_in = request.user.is_authenticated
        lang = services.language_for(request.user, request.COOKIES.get(services.LANGUAGE_COOKIE),
                                     request.META.get("HTTP_ACCEPT_LANGUAGE", ""))
        request.web_lang = lang
        if request.path_info.startswith("/admin/"):
            response = self._admin_gate(request) or self.get_response(request)
        else:
            with translation.override(lang):
                request.LANGUAGE_CODE = lang
                response = self._mfa_gate(request) or self.get_response(request)
            response.headers.setdefault("Content-Language", lang)
        if signed_in or request.user.is_authenticated:
            patch_cache_control(response, no_store=True, private=True)
        return response

    @staticmethod
    def _to_mfa(request, step):
        target = reverse("web:mfa_enrol" if step == "enrol" else "web:mfa_verify")
        if request.method in ("GET", "HEAD"):
            target += "?" + urlencode({"next": request.get_full_path()})
        return HttpResponseRedirect(target)

    def _mfa_gate(self, request):
        """R14: until the session is OTP-verified, only the MFA pages, the language switch and sign-out."""
        if not request.user.is_authenticated or request.path_info.startswith(MFA_OPEN_PREFIXES):
            return None
        step = mfa.gate(request.user, request.session, request.web_mfa_required)
        return self._to_mfa(request, step) if step else None

    def _admin_gate(self, request):
        """R19: Django admin only through the web sign-in and an OTP-verified session (and superusers only, once
        ``WEB_ADMIN_SUPERUSERS_ONLY`` is on)."""
        if not getattr(settings, "WEB_ADMIN_REQUIRE_MFA", True) or not mfa.enforced():
            return None
        user = request.user
        if not user.is_authenticated:
            return HttpResponseRedirect(reverse("web:login") + "?" + urlencode({"next": request.get_full_path()}))
        from . import views  # the bilingual 403 page (views imports this module)

        if not (user.is_staff or user.is_superuser):
            return views.refused(request)
        if getattr(settings, "WEB_ADMIN_SUPERUSERS_ONLY", False) and not user.is_superuser:
            return views.refused(request)
        device = mfa.device_of(user)
        if device is None:
            return self._to_mfa(request, "enrol")
        if request.session.get(mfa.VERIFIED_KEY) != device.pk:
            return self._to_mfa(request, "verify")
        return None

    @staticmethod
    def _human_only(request):
        if SESSION_KEY in request.session and not services.is_web_user(request.user):
            request.session.flush()
            from django.contrib.auth.models import AnonymousUser

            request.user = AnonymousUser()

    @staticmethod
    def _timeouts(request):
        user = request.user
        if not user.is_authenticated:
            return
        now = time.time()
        session = request.session
        login_at = session.get(LOGIN_AT)
        seen_at = session.get(SEEN_AT)
        if login_at is None or seen_at is None:  # e.g. a session opened by the admin login
            session[LOGIN_AT] = login_at = login_at or now
            session[SEEN_AT] = seen_at = now
        reason = None
        if now - login_at > services.absolute_timeout().total_seconds():
            reason = "absolute_timeout"
        elif now - seen_at > services.idle_timeout(user, request.web_mfa_required).total_seconds():
            reason = "idle_timeout"
        if reason:
            services.log_logout(user, reason=reason)  # R26: why the session ended
            logout(request)  # flushes the session: the old key is no longer valid
            return
        if now - seen_at >= SEEN_RESOLUTION:
            session[SEEN_AT] = now


def start_session(request):
    """Called right after ``login()``: the timeout clocks start (login() has already rotated the session key)."""
    now = time.time()
    request.session[LOGIN_AT] = now
    request.session[SEEN_AT] = now
