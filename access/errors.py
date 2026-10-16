"""Bilingual errors shared by access, approvals and the API.

The API maps them to: 403 AccessDenied, 404 NotFound, 409 Conflict, 422 RuleError.
"""


class BilingualError(Exception):
    status = 422

    def __init__(self, code, message_en, message_ar, detail=None):
        self.code = code
        self.message_en = message_en
        self.message_ar = message_ar
        self.detail = detail
        super().__init__(message_en)

    def message(self, language="en"):
        return self.message_ar if language == "ar" else self.message_en

    def as_dict(self):
        body = {"code": self.code, "message_en": self.message_en, "message_ar": self.message_ar}
        if self.detail is not None:
            body["detail"] = self.detail
        return body


class RuleError(BilingualError):
    status = 422


class AccessDenied(BilingualError):
    status = 403


class NotFound(BilingualError):
    status = 404


class Conflict(BilingualError):
    status = 409
