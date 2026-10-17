"""``python manage.py invoicing_anonymise [--date YYYY-MM-DD] [--apply]`` (invoicing R52, AT-43).

Dry run by default: lists the sales documents, individual customers, individual vendors and their bills whose retention period has passed and that
would be anonymised. ``--apply`` changes them, and only when the setting ``INVOICING_ANONYMISATION_ENABLED`` is
True (off by default until the retention rules are confirmed; zatca OQ-8, OQ-31). An operator job, never a user
action; journal entries, amounts, VAT, numbers and ZATCA data are never touched.
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from invoicing import services
from invoicing.models import InvoicingError


class Command(BaseCommand):
    help = "Anonymise individuals' personal data after retention (dry run unless --apply). / إخفاء هوية الأفراد بعد مدة الحفظ."

    def add_arguments(self, parser):
        parser.add_argument("--date", type=date.fromisoformat, default=None,
                            help="Retention cut-off date (default: today in Riyadh)")
        parser.add_argument("--apply", action="store_true", help="Change the records (default: dry run)")

    def handle(self, *args, **options):
        try:
            result = services.anonymise_expired_personal_data(today=options["date"], apply=options["apply"])
        except InvoicingError as exc:
            raise CommandError(" ".join(exc.messages)) from None
        mode = "applied" if result["applied"] else "dry run"
        self.stdout.write(f"{mode} ({result['date']}): {len(result['documents'])} document(s), "
                          f"{len(result['customers'])} customer(s), {len(result['vendors'])} vendor(s), "
                          f"{len(result['purchase_documents'])} purchase document(s)")
        for pk in result["documents"]:
            self.stdout.write(f"  sales document {pk}")
        for pk in result["customers"]:
            self.stdout.write(f"  customer {pk}")
        for pk in result["vendors"]:
            self.stdout.write(f"  vendor {pk}")
        for pk in result["purchase_documents"]:
            self.stdout.write(f"  purchase document {pk}")
