"""Daily access jobs (access.md R77, R86, R87): record matrix versions taking effect, remind delegators and delegates
3 days before a delegation ends, and alert the top authority about governance reports unreviewed after 30 days.
Eligibility never depends on this job: delegations end and versions apply by their dates at decision time."""
from django.core.management.base import BaseCommand

from core.access import delegations, matrix, reports, services as access


class Command(BaseCommand):
    help = "Daily access jobs: matrix effective dates, delegation reminders, unreviewed reports."

    def handle(self, *args, **options):
        versions = matrix.activate_due_versions(access.system_user("governance"))
        reminded = delegations.send_delegation_reminders()
        alerts = reports.send_unreviewed_alerts()
        self.stdout.write(f"matrix versions in effect: {len(versions)}; delegation reminders: {len(reminded)}; "
                          f"unreviewed report alerts: {len(alerts)}")
