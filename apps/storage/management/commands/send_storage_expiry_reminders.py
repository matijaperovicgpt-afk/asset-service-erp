"""Remind clients whose storage period is about to end (WNT-03).

Meant to run daily from host cron - no Celery, no new infrastructure::

    0 8 * * *  docker compose exec -T web python manage.py \
                   send_storage_expiry_reminders

Each agreement is reminded once: the send stamps
``StorageAgreement.expiry_reminder_sent_at``, so re-running the same day
(or every day) mails nobody twice. Use ``--dry-run`` to see what a run
would do before wiring up the cron entry.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.storage.services import (
    EXPIRY_REMINDER_DAYS,
    agreements_due_for_expiry_reminder,
    send_expiry_reminders,
)


class Command(BaseCommand):
    help = (
        "E-mail clients whose storage period ends within the lead "
        "time, so a renewal offer can be prepared in time (WNT-03)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=EXPIRY_REMINDER_DAYS,
            help=(
                "Lead time in days: remind about periods ending within this "
                "many days (default: %(default)s)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be sent without mailing, logging or stamping.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days < 0:
            raise CommandError("--days must be zero or positive.")
        dry_run = options["dry_run"]

        due = agreements_due_for_expiry_reminder(days=days)
        if not due.exists():
            self.stdout.write(
                f"No storage agreements end within {days} day(s) without a "
                "reminder already sent."
            )
            return

        counts = send_expiry_reminders(days=days, dry_run=dry_run)
        prefix = "Would send" if dry_run else "Sent"
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}: {counts['emailed']} e-mail(s); "
                f"{counts['to_phone']} client(s) have no e-mail on file "
                f"(logged for a phone call); {counts['failed']} failed."
            )
        )
