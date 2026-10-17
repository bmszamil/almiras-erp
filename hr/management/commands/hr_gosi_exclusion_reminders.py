from django.core.management.base import BaseCommand

from hr import jobs


class Command(BaseCommand):
    help = ("R65: remind HR officers to exclude leavers on GOSI by the end of the leaving month. / تذكير مسؤولي "
            "الموارد البشرية باستبعاد المغادرين من التأمينات قبل نهاية شهر المغادرة.")

    def handle(self, *args, **opts):
        sent = jobs.gosi_exclusion_reminders()
        self.stdout.write(f"{sent} reminder(s) sent. / أُرسل {sent} تذكير.")
