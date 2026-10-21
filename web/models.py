"""Web UI state (web.md §3): user preferences, the login throttle and MFA devices. No business records live here.

No model here has an ``entity`` FK: a preference and a person's MFA device follow the user across entities
(web.md 3.1, justified like ``Role``), and a login attempt happens before any entity is known.
"""
from django.conf import settings
from django.db import models

from core.models import Entity, bi

from .crypto import EncryptedSecretField


class UiPreference(models.Model):
    """One row per human user (web.md 3.1). Language is not here: it is ``UserProfile.preferred_language``."""

    class Digits(models.TextChoices):
        LATIN = "latin", bi("Latin (0-9)", "لاتينية (0-9)")
        ARABIC_INDIC = "arabic_indic", bi("Arabic-Indic (٠-٩)", "عربية هندية (٠-٩)")

    class DateDisplay(models.TextChoices):
        ENTITY_DEFAULT = "entity_default", bi("Entity default", "الإعداد الافتراضي للكيان")
        GREGORIAN = "gregorian", bi("Gregorian", "ميلادي")
        GREGORIAN_HIJRI = "gregorian_hijri", bi("Gregorian, then Hijri", "ميلادي ثم هجري")
        HIJRI_GREGORIAN = "hijri_gregorian", bi("Hijri, then Gregorian", "هجري ثم ميلادي")

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="ui_preference",
                                verbose_name=bi("User", "المستخدم"))
    digits = models.CharField(bi("Digits", "الأرقام"), max_length=12, choices=Digits.choices, default=Digits.LATIN)
    date_display = models.CharField(bi("Date display", "عرض التاريخ"), max_length=16, choices=DateDisplay.choices,
                                    default=DateDisplay.ENTITY_DEFAULT)
    last_entity = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Last entity", "آخر كيان"),
        help_text=bi("Re-checked on every use (R10)", "يُتحقق منه عند كل استخدام"),
    )
    updated_at = models.DateTimeField(bi("Updated at", "تاريخ التحديث"), auto_now=True)

    class Meta:
        verbose_name = bi("UI preference", "تفضيلات الواجهة")
        verbose_name_plural = bi("UI preferences", "تفضيلات الواجهة")

    def __str__(self):
        return f"ui preference #{self.pk}"


class LoginThrottle(models.Model):
    """Failed sign-ins per (username, IP), for the lockout of web.md R18 (OQ-9 thresholds).

    ``key`` is a keyed hash of the lower-cased username and the client IP (or of the username alone, for the
    per-username cap), so neither is stored in clear. Rows whose window and lock have run out are purged on every
    sign-in attempt (web.services.sign_in).
    """

    key = models.CharField(bi("Key (keyed hash)", "المفتاح (تجزئة)"), max_length=64, unique=True)
    failures = models.PositiveIntegerField(bi("Failed sign-ins", "محاولات الدخول الفاشلة"), default=0)
    first_failure_at = models.DateTimeField(bi("First failure at", "وقت أول محاولة فاشلة"), db_index=True)
    locked_until = models.DateTimeField(bi("Locked until", "مقفل حتى"), null=True, blank=True)

    class Meta:
        verbose_name = bi("login throttle", "تقييد تسجيل الدخول")
        verbose_name_plural = bi("login throttles", "تقييد تسجيل الدخول")

    def __str__(self):
        return f"login throttle #{self.pk}"


class MfaDevice(models.Model):
    """A person's TOTP authenticator (RFC 6238; web.md 3.3, R13 to R16). One per user (OQ-11).

    No ``entity`` FK: sign-in security follows the person across entities, like ``UiPreference``. The shared secret
    is encrypted at rest (``web.crypto``, keys in ``WEB_MFA_KEYS``). ``last_counter`` is the last accepted 30-second
    time step: a code is accepted once only (no replay). ``failures`` / ``last_failure_at`` / ``locked_until`` are
    the code throttle (R18): every wrong code doubles the wait before the next try, and 5 in a row lock the device
    for 15 minutes. Written only by ``web.mfa``.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="web_mfa_device",
                                verbose_name=bi("User", "المستخدم"))
    secret = EncryptedSecretField(bi("Secret (encrypted)", "السر (مشفّر)"))
    confirmed = models.BooleanField(bi("Confirmed", "مؤكَّد"), default=False)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    confirmed_at = models.DateTimeField(bi("Confirmed at", "تاريخ التأكيد"), null=True, blank=True)
    last_counter = models.BigIntegerField(bi("Last accepted time step", "آخر خطوة زمنية مقبولة"), null=True, blank=True)
    failures = models.PositiveIntegerField(bi("Wrong codes in a row", "رموز خاطئة متتالية"), default=0)
    last_failure_at = models.DateTimeField(bi("Last wrong code at", "وقت آخر رمز خاطئ"), null=True, blank=True)
    locked_until = models.DateTimeField(bi("Locked until", "مقفل حتى"), null=True, blank=True)

    class Meta:
        verbose_name = bi("MFA device", "جهاز المصادقة الثنائية")
        verbose_name_plural = bi("MFA devices", "أجهزة المصادقة الثنائية")

    def __str__(self):
        return f"mfa device #{self.pk}"


class RecoveryCode(models.Model):
    """A single-use recovery code (web.md R16): 10 per enrolment, shown once, stored only as a keyed hash."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="web_recovery_codes",
                             verbose_name=bi("User", "المستخدم"))
    code_hash = models.CharField(bi("Code (keyed hash)", "الرمز (تجزئة)"), max_length=64)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    used_at = models.DateTimeField(bi("Used at", "تاريخ الاستخدام"), null=True, blank=True)

    class Meta:
        verbose_name = bi("MFA recovery code", "رمز استرداد")
        verbose_name_plural = bi("MFA recovery codes", "رموز الاسترداد")
        constraints = [models.UniqueConstraint(fields=["user", "code_hash"], name="web_recovery_code_unique")]

    def __str__(self):
        return f"recovery code #{self.pk}"
