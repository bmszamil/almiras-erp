from django.apps import AppConfig


class AccessConfig(AppConfig):
    name = "access"
    verbose_name = "Access / الصلاحيات"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed

        from . import signals

        user_logged_in.connect(signals.on_sign_in, dispatch_uid="access_sign_in")
        user_login_failed.connect(signals.on_sign_in_failed, dispatch_uid="access_sign_in_failed")
        user_logged_out.connect(signals.on_sign_out, dispatch_uid="access_sign_out")
