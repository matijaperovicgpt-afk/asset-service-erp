"""Domain services for scheduling.

Booking of intake/handover/transport/storage appointments for cases.
Booked appointments feed the workflow guards: an INTAKE appointment is
the SVC-04 precondition for ``intake_scheduled``; a TRANSPORT_DEPARTURE
appointment is the TRP-04 departure-date source for
``departure_scheduled``.
"""
from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import Appointment
from .workinghours import as_local, is_working_time
from .workinghours import summary as working_hours_summary


def book_appointment(case, kind: str, scheduled_for, user, note: str = "") -> Appointment:
    """
    SVC-04: appointments are agreed with the client and recorded on the
    case. Refuses an unknown kind and a missing or past datetime.

    SVC-04: the time must also fall inside the yard's opening hours
    (``workinghours``) - an appointment nobody is there to keep is not a
    slot. Double-booking is still staff's own judgement: the spec defines
    no capacity rule and none is invented here.
    """
    if kind not in Appointment.Kind.values:
        raise ValidationError(
            _("Unknown appointment kind '%(kind)s' (allowed: %(allowed)s).")
            % {"kind": kind or "-", "allowed": ", ".join(Appointment.Kind.values)}
        )
    if scheduled_for is None:
        raise ValidationError(_("An appointment needs a date and time."))
    if scheduled_for <= timezone.now():
        raise ValidationError(
            _(
                "Appointment time %(when)s is in the past (SVC-04: "
                "appointments are agreed forward in time)."
            ) % {"when": scheduled_for}
        )
    if not is_working_time(scheduled_for):
        raise ValidationError(
            _(
                "%(when)s is outside the site's working hours (SVC-04). "
                "%(hours)s"
            ) % {
                "when": as_local(scheduled_for).strftime("%d.%m.%Y %H:%M"),
                "hours": working_hours_summary(),
            }
        )
    appointment = Appointment.objects.create(
        case=case, kind=kind, scheduled_for=scheduled_for, note=note
    )
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-04: %s appointment #%s booked for %s."
            % (
                appointment.get_kind_display(),
                appointment.pk,
                timezone.localtime(scheduled_for).strftime("%d.%m.%Y %H:%M"),
            )
        ),
    )

    # TRP-04: the advance is due "at least seven days before the planned
    # departure", and until this moment there may have been no planned
    # departure to count back from - the desired term on the inquiry is
    # optional, so the advance can be derived with no due date at all and
    # the rule silently evaluates to nothing. Re-derive now that the date
    # exists. ``ensure_transport_advance`` is idempotent and returns a
    # paid advance untouched, so on every other path this is a no-op.
    if kind == Appointment.Kind.TRANSPORT_DEPARTURE:
        from apps.transport.services import (
            ensure_transport_advance,
            warn_off_standard_departure,
        )

        # TRP-05: the standard days were checked against the desired term
        # and not against the term actually booked here.
        warn_off_standard_departure(case, as_local(scheduled_for).date())
        ensure_transport_advance(case, user=user)

    return appointment
