"""Re-encrypt MFA secrets under the newest key after a key rotation (web.md R16; security review 2026-10-21, item 4).

    WEB_MFA_KEYS=new,old python manage.py web_mfa_rekey [--expire-old-recovery-codes]

1. Put the new key first in ``WEB_MFA_KEYS`` and keep the old one after it; run this command.
2. Every device secret is re-encrypted under the new key. Recovery codes are one-way hashes and cannot be re-hashed:
   the command lists the people whose unused codes are still hashed under an older key. They renew their codes
   (``/mfa/``), or ``--expire-old-recovery-codes`` retires those codes.
3. Remove the old key. Logged ``mfa_rekeyed``.
"""
from django.core.management.base import BaseCommand

from web import mfa


class Command(BaseCommand):
    help = "Re-encrypt MFA secrets under the current WEB_MFA_KEYS key; report or retire older recovery codes."

    def add_arguments(self, parser):
        parser.add_argument("--expire-old-recovery-codes", action="store_true")

    def handle(self, *args, **opts):
        result = mfa.rekey(expire_old_recovery_codes=opts["expire_old_recovery_codes"])
        self.stdout.write(f"Secrets re-encrypted: {result['reencrypted']}. Recovery codes under an older key: "
                          f"{result['old_recovery_codes']}{' (retired)' if result['expired'] else ''}.")
        for username in result["users"]:
            self.stdout.write(f"  renew recovery codes: {username}")
