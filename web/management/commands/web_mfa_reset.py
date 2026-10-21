"""Reset a person's MFA device (web.md R16, OQ-11 default: the operator path, also for superusers).

    python manage.py web_mfa_reset <username> --reason "Phone lost, ticket 123"

The device and the recovery codes are removed; the person enrols again at the next sign-in. Logged ``mfa_reset`` with
the reason and the operating-system user. People are normally reset by their Managing Partner or access admin
(``web.mfa.reset_device``); this command is for superusers and for recovery.
"""
import getpass

from django.core.management.base import BaseCommand, CommandError

from web import mfa


class Command(BaseCommand):
    help = "Reset a user's MFA device and recovery codes (web.md R16); logged mfa_reset."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **opts):
        try:
            had = mfa.reset_device_by_operator(opts["username"], reason=opts["reason"], os_user=getpass.getuser())
        except mfa.MfaError as exc:
            raise CommandError(f"Refused: {exc.code}") from None
        self.stdout.write("MFA reset. / تمت إعادة التعيين." if had else "No device; nothing to reset (logged).")
