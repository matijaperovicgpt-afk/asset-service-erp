"""Generate thumbnails for photo attachments uploaded before the feature.

New uploads get a thumbnail in ``attach_file``; this command fills in the
gap for existing photos. Safe to re-run - attachments that already have a
thumbnail are skipped, and undecodable images are counted and left alone
(they keep serving their full-size original in grids).

    python manage.py backfill_thumbnails [--dry-run]

Synchronous by design, even when a Celery broker is configured: an
operator running a batch repair wants the count when the command
returns, not thousands of queued jobs. It therefore calls
``generate_thumbnail`` directly rather than ``enqueue_thumbnail``.
"""

from django.core.management.base import BaseCommand

from apps.attachments.models import Attachment
from apps.attachments.services import generate_thumbnail


class Command(BaseCommand):
    help = "Generate missing thumbnails for existing photo attachments."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many photos are missing thumbnails without writing any.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        pending = Attachment.objects.filter(
            kind=Attachment.Kind.PHOTO, thumbnail=""
        )
        total = pending.count()

        if dry_run:
            self.stdout.write(f"{total} photo(s) missing a thumbnail.")
            return

        created = 0
        failed = 0
        for attachment in pending.iterator():
            if generate_thumbnail(attachment):
                created += 1
            else:
                failed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Thumbnails created: {created}; skipped/unreadable: {failed} "
                f"(of {total} candidate photo(s))."
            )
        )
