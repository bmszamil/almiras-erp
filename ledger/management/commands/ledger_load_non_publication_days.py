"""``python manage.py ledger_load_non_publication_days --user USERNAME [--group CODE] [--file PATH] [--apply]``.

Owner decision H1 (ledger-fx R12 amendment, AT-44): loads the proposed SAMA non-publication days (Fridays,
Saturdays and KSA public holidays of 2026 and 2027, ``ledger/seed/sama_non_publication_days_2026_2027.json``)
**unverified**. Dry run by default: lists what would be created and what is already recorded. ``--apply`` writes
the new rows. ``--user`` is the person loading them: a human holding ``ledger.maintain_global_rate`` at the
top-level group parent (``--group``, default the only one). Recorded dates are never changed. A second person then
verifies each row against the SAMA / official announcement (``ledger.services.verify_non_publication_day``).
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError

from core.models import Entity
from ledger import services
from ledger.models import LedgerError
from ledger.non_publication_seed import SEED_FILE, load_seed_file


class Command(BaseCommand):
    help = ("Load proposed SAMA non-publication days, unverified (dry run unless --apply). / "
            "تحميل أيام عدم نشر أسعار البنك المركزي المقترحة دون تحقق (تجربة دون --apply).")

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True, help="Username of the person loading the days")
        parser.add_argument("--group", default=None, help="Code of the top-level group parent entity")
        parser.add_argument("--file", default=str(SEED_FILE), help="Seed file (JSON list)")
        parser.add_argument("--apply", action="store_true", help="Write the rows (default: dry run)")

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(username=options["user"], is_active=True).first()
        if user is None:
            raise CommandError(f"No active user {options['user']!r}. / لا يوجد مستخدم نشط بهذا الاسم.")
        group = None
        if options["group"]:
            group = Entity.objects.filter(code=options["group"]).first()
            if group is None:
                raise CommandError(f"No entity {options['group']!r}. / لا يوجد كيان بهذا الرمز.")
        try:
            rows = load_seed_file(options["file"])
            new, skipped = services.load_non_publication_days(rows, user=user, group=group, apply=options["apply"])
        except (OSError, ValueError) as exc:
            raise CommandError(f"Cannot read the seed file: {exc} / تعذرت قراءة ملف البيانات.") from None
        except LedgerError as exc:
            raise CommandError(" ".join(exc.messages)) from None
        except PermissionDenied as exc:
            raise CommandError(str(exc)) from None
        mode = "applied" if options["apply"] else "dry run"
        self.stdout.write(f"{mode}: {len(new)} new day(s), unverified; {len(skipped)} already recorded (unchanged).")
        for on in new:
            self.stdout.write(f"  + {on}")
        for on in skipped:
            self.stdout.write(f"  = {on}")
        if not options["apply"] and new:
            self.stdout.write("Nothing was written; run again with --apply. / لم يُكتب شيء؛ أعد التشغيل مع --apply.")
