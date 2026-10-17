from django.core.management.base import BaseCommand, CommandError

from hr import jobs
from hr.models import HrError


class Command(BaseCommand):
    help = ("Re-hash ID numbers and re-encrypt HR personal data under the current keys (hr R9, R10), after a key "
            "rotation or once HR_PERSONAL_DATA_HASH_KEY is set. / إعادة تجزئة أرقام الهوية وإعادة تشفير البيانات "
            "الشخصية بالمفاتيح الحالية بعد تدوير المفاتيح.")

    def handle(self, *args, **opts):
        try:
            counts = jobs.rehash_personal_data()
        except HrError as exc:
            raise CommandError(exc.messages[0]) from None
        self.stdout.write(self.style.SUCCESS(
            f"{counts['employees']} employee(s) and {counts['documents']} document(s) re-hashed and re-encrypted. / "
            f"أُعيدت تجزئة وتشفير {counts['employees']} موظف و{counts['documents']} مستند."))
