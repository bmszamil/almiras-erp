"""API error body: {code, message_en, message_ar} (spec §6.1)."""
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .errors import BilingualError


def exception_handler(exc, context):
    if isinstance(exc, BilingualError):
        return Response(exc.as_dict(), status=exc.status)
    response = drf_exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict) and "detail" in response.data:
        code = getattr(response.data["detail"], "code", "error")
        msg = str(response.data["detail"])
        response.data = {"code": code, "message_en": msg, "message_ar": _AR.get(code, msg)}
    return response


_AR = {
    "not_authenticated": "يجب تسجيل الدخول مع التحقق الثنائي.",
    "authentication_failed": "فشل التحقق من الهوية.",
    "permission_denied": "ليس لديك صلاحية.",
    "not_found": "غير موجود.",
    "method_not_allowed": "الطريقة غير مسموحة.",
    "parse_error": "طلب غير صالح.",
}
