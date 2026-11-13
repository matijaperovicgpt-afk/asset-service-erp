"""Business logic for the public client portal."""
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction

from apps.attachments.models import VIDEO_EXTENSIONS, Attachment
from apps.attachments.services import attach_file
from apps.cases.models import Case, CaseType, ReportChannel, Status


def _attachment_kind(upload) -> str:
    """PHOTO or VIDEO, decided by the upload's file extension."""
    name = getattr(upload, "name", "") or ""
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension in VIDEO_EXTENSIONS:
        return Attachment.Kind.VIDEO
    return Attachment.Kind.PHOTO


def _get_initial_service_status() -> Status:
    """Return the initial Status for a new SERVICE case.

    The "new_report" status is seeded by a management command; fail loudly
    if it is missing so the misconfiguration is obvious.
    """
    try:
        return Status.objects.get(case_type=CaseType.SERVICE, code="new_report")
    except Status.DoesNotExist as exc:
        raise ImproperlyConfigured(
            'Initial SERVICE status with code "new_report" does not exist. '
            "Run the status seeding management command first."
        ) from exc


def create_public_report(
    *,
    contact_name,
    phone,
    asset_info,
    symptoms_description,
    email="",
    location="",
    client_diagnosis="",
    photos=(),
) -> Case:
    """
    Create a SERVICE case from an anonymous portal submission (POR-01)
    with its photo attachments, and notify staff about it (NTF-01). The
    staff e-mail is scheduled via ``transaction.on_commit`` so a rolled
    back submission never notifies anyone.
    """
    with transaction.atomic():
        case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PORTAL,
            status=_get_initial_service_status(),
            raw_contact_name=contact_name,
            raw_phone=phone,
            raw_email=email or "",
            raw_asset_info=asset_info,
            location=location or "",
            symptoms_description=symptoms_description,
            client_diagnosis=client_diagnosis or "",
        )
        for photo in photos or []:
            attach_file(
                case, photo, _attachment_kind(photo), Attachment.Stage.REPORT
            )

        def _notify():
            from apps.notifications.services import notify_new_portal_case

            notify_new_portal_case(case)

        transaction.on_commit(_notify)
        return case
