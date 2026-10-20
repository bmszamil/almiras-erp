"""Web middleware (web.md R12, R17, R22, R27).

Runs after ``AuthenticationMiddleware``:

1. **Human-only sessions (R12).** A session that names a user the backend refuses (agent, system or inactive user;
   ``HumanModelBackend.get_user`` returns None) is flushed, so a forged session gives nothing.
2. **Timeouts (R17, OQ-8).** Idle timeout (15 min for users who need MFA, 30 min otherwise) and absolute timeout
   (10 h): the session is ended and the user signs in again.
3. **Language (R27).** The profile language once signed in; before, the cookie, then Accept-Language, then
   Arabic. Activated for the request; ``<html lang dir>`` comes from it. Django admin keeps its own language.
4. **No caching (R22).** Responses to signed-in users carry ``Cache-Control: no-store, private``.
"""
import time

from django.contrib.auth import SESSION_KEY, logout
from django.utils import translation
from django.utils.cache import patch_cache_control

from . import services

LOGIN_AT = "web_login_at"
SEEN_AT = "web_seen_at"
SEEN_RESOLUTION = 60  # seconds: the last-seen time is written at most once a minute, not on every request


class WebMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        self._human_only(request)
        self._timeouts(request)
        signed_in = request.user.is_authenticated
        lang = services.language_for(request.user, request.COOKIES.get(services.LANGUAGE_COOKIE),
                                     request.META.get("HTTP_ACCEPT_LANGUAGE", ""))
        request.web_lang = lang
        if request.path_info.startswith("/admin/"):
            response = self.get_response(request)
        else:
            with translation.override(lang):
                request.LANGUAGE_CODE = lang
                response = self.get_response(request)
            response.headers.setdefault("Content-Language", lang)
        if signed_in or request.user.is_authenticated:
            patch_cache_control(response, no_store=True, private=True)
        return response

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
        if (now - login_at > services.absolute_timeout().total_seconds()
                or now - seen_at > services.idle_timeout(user).total_seconds()):
            logout(request)  # flushes the session: the old key is no longer valid
            return
        if now - seen_at >= SEEN_RESOLUTION:
            session[SEEN_AT] = now


def start_session(request):
    """Called right after ``login()``: the timeout clocks start (login() has already rotated the session key)."""
    now = time.time()
    request.session[LOGIN_AT] = now
    request.session[SEEN_AT] = now
