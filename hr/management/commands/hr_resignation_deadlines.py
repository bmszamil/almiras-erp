from django.core.management.base import BaseCommand

from hr import jobs


class Command(BaseCommand):
    help = ("Art. 79 bis: remind approvers on day 20 and 27 of a resignation and accept it by law on day 30 (hr R41). "
            "/ المادة 79 مكرر: تذكير المعتمدين في اليومين 20 و27 من الاستقالة وقبولها بحكم النظام في اليوم 30.")

    def handle(self, *args, **opts):
        report = jobs.resignation_deadlines()
        self.stdout.write(f"{report['reminders']} reminder(s), {len(report['deemed'])} resignation(s) accepted by law. / "
                          f"{report['reminders']} تذكير، و{len(report['deemed'])} استقالة قُبلت بحكم النظام.")
        for code, why in report["skipped"]:
            self.stdout.write(f"Skipped / تخطي {code}: {why}")
