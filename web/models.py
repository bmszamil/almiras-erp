"""Web UI state (web.md §3): user preferences and the login throttle. No business records live here.

Neither model has an ``entity`` FK: a preference follows the user across entities (web.md 3.1, justified like
``Role``), and a login attempt happens before any entity is known.
"""
from django.conf import settings
from django.db import models

from core.models import Entity, bi


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
