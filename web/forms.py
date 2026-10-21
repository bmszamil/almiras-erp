"""Forms of the web UI. Labels come from web/texts.py in the active language (R28); no business rule lives here.

List filters are GET forms of enumerated values, dates and document numbers only (web.md R23).
"""
from django import forms
from django.core.validators import RegexValidator

from core.models import label_part
from invoicing.models import SalesDocument
from ledger.models import JournalEntry

from .texts import text

NUMBER = RegexValidator(r"^[A-Za-z0-9/_.\-]*\Z", "Document numbers only. / أرقام المستندات فقط.")


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class LoginForm(forms.Form):
    username = forms.CharField(max_length=150)
    password = forms.CharField(max_length=4096, strip=False, widget=forms.PasswordInput)

    def __init__(self, *args, lang="en", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = text("username", lang)
        self.fields["password"].label = text("password", lang)
        self.fields["username"].widget.attrs = {"autocomplete": "username", "autocapitalize": "none",
                                                "class": "form-control", "dir": "ltr", "maxlength": "150"}
        self.fields["password"].widget.attrs = {"autocomplete": "current-password", "class": "form-control",
                                                "dir": "ltr"}
        required = text("login_required_fields", lang)
        for field in self.fields.values():
            field.error_messages["required"] = required


def _choices(lang, values):
    return [("", text("any", lang)), *values]


class _FilterForm(forms.Form):
    date_from = forms.DateField(required=False, widget=DateInput())
    date_to = forms.DateField(required=False, widget=DateInput())
    number = forms.CharField(required=False, max_length=40, validators=[NUMBER])

    def __init__(self, *args, lang="en", **kwargs):
        super().__init__(*args, **kwargs)
        self.lang = lang
        for name, field in self.fields.items():
            field.label = text(name, lang)
            css = "form-select form-select-sm" if isinstance(field, forms.ChoiceField) else "form-control form-control-sm"
            field.widget.attrs["class"] = css
        self.fields["number"].widget.attrs["dir"] = "ltr"

    def filters(self):
        """The valid filter values (invalid ones are ignored, and the page says so)."""
        if not self.is_bound:
            return {}
        self.is_valid()
        return {k: v for k, v in self.cleaned_data.items() if v not in (None, "")}


class JournalEntryFilterForm(_FilterForm):
    status = forms.ChoiceField(required=False)
    source = forms.ChoiceField(required=False)
    field_order = ["status", "source", "date_from", "date_to", "number"]

    def __init__(self, *args, lang="en", **kwargs):
        super().__init__(*args, lang=lang, **kwargs)
        self.fields["status"].choices = _choices(lang, [(v, text(f"je_status.{v}", lang))
                                                        for v in JournalEntry.Status.values])
        self.fields["source"].choices = _choices(lang, [(v, label_part(label, lang))
                                                        for v, label in JournalEntry.Source.choices])


class SalesDocumentFilterForm(_FilterForm):
    doc_type = forms.ChoiceField(required=False)
    status = forms.ChoiceField(required=False)
    zatca_status = forms.ChoiceField(required=False)
    field_order = ["doc_type", "status", "zatca_status", "date_from", "date_to", "number"]

    def __init__(self, *args, lang="en", **kwargs):
        super().__init__(*args, lang=lang, **kwargs)
        for name, enum in (("doc_type", SalesDocument.DocType), ("status", SalesDocument.Status),
                           ("zatca_status", SalesDocument.ZatcaStatus)):
            self.fields[name].choices = _choices(lang, [(v, label_part(label, lang)) for v, label in enum.choices])


# --- MFA (R13 to R16) and the approval inbox (R7, R8, R15) ------------------------------------------------------

class _LabelledForm(forms.Form):
    """Labels and the "required" message from web/texts.py in the active language (R28)."""

    labels = {}
    required_key = "field_required"

    def __init__(self, *args, lang="en", **kwargs):
        super().__init__(*args, **kwargs)
        self.lang = lang
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.HiddenInput):
                continue  # carried, not shown (the document version, R7)
            field.label = text(self.labels.get(name, name), lang)
            field.error_messages["required"] = text(self.required_key, lang)
            field.widget.attrs.setdefault("class", "form-control")


class OtpCodeForm(_LabelledForm):
    """A 6-digit authenticator code. Arabic-Indic digits are accepted (R33); the service normalises them."""

    code = forms.CharField(max_length=16, strip=True)
    labels = {"code": "mfa_code"}
    required_key = "mfa_code_required"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        widget = self.fields["code"].widget
        widget.attrs = {**widget.attrs, "autocomplete": "one-time-code", "inputmode": "numeric", "dir": "ltr",
                        "maxlength": "16", "class": "form-control alm-otp"}


class RecoveryCodeForm(_LabelledForm):
    recovery_code = forms.CharField(max_length=32, strip=True)
    labels = {"recovery_code": "mfa_recovery_code"}
    required_key = "mfa_code_required"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        widget = self.fields["recovery_code"].widget
        widget.attrs = {**widget.attrs, "autocomplete": "off", "autocapitalize": "none", "dir": "ltr",
                        "maxlength": "32"}


VERSION = RegexValidator(r"^[\w:.+\-]{0,64}\Z", "Not a document version. / ليس إصدار مستند.")


class ApproveForm(OtpCodeForm):
    """Approve in the inbox: the document version shown (R7) and a fresh authenticator code (step-up, R15)."""

    version = forms.CharField(max_length=64, required=False, validators=[VERSION], widget=forms.HiddenInput)
    labels = {"code": "mfa_code_step_up"}


class SendBackForm(_LabelledForm):
    """Return or reject in the inbox: the version shown (R7) and the reason, Arabic required, English optional (R8)."""

    version = forms.CharField(max_length=64, required=False, validators=[VERSION], widget=forms.HiddenInput)
    reason_ar = forms.CharField(max_length=500, widget=forms.Textarea(attrs={"rows": 3, "dir": "rtl", "lang": "ar"}))
    reason_en = forms.CharField(max_length=500, required=False,
                                widget=forms.Textarea(attrs={"rows": 3, "dir": "ltr", "lang": "en"}))
    labels = {"reason_ar": "reason_ar", "reason_en": "reason_en"}
    required_key = "reason_ar_required"
