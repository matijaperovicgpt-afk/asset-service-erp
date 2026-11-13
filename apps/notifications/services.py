"""Business logic for the notifications app.

Outbound e-mails triggered by case events:
- ``notify_case_transition``: client e-mail on entering a status with
  ``notify_client`` set (NTF-02/NTF-03).
- ``notify_new_portal_case``: staff e-mail on every new portal
  submission (NTF-01).

What the client is told automatically is deliberately thin. There is no
client page on this site, so the client's whole view of a case is: the
thank-you page (or the NTF-04 confirmation) when it is received, then
the status mails - "in service" and "finished", as the seeded
``client_visible_name`` / ``notify_client`` data spells out. Prices,
offers, appointments and every other detail reach the client through
the channels staff operate (the SVC-17 completion notice, a phone call,
an SMS, an e-mail staff write), never through an automatic mail.

A notification failure must never break the triggering operation, so
both entry points swallow (and log) every exception. Every attempted
e-mail is recorded as an ``EmailLog`` row (NTF-03).

NTF-05: each ``notify_*`` entry point is a dispatcher that hands primary
keys to a Celery task (``apps/notifications/tasks.py``); the composition
and the send live in the matching ``_deliver_*`` function below and run
in a worker. With no broker configured - the dev box, CI and the test
suite - the task body runs inline at the call site, so behaviour is
unchanged. ``send_client_email`` is deliberately NOT queued: its boolean
return is a delivery receipt two callers record as fact (see its
docstring).
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import translation
from django.utils.translation import gettext as _

from apps.notifications import tasks

logger = logging.getLogger(__name__)


def _recipient_for(case):
    """Return the e-mail address of the case's contact person, or None.

    §7.1: the procedure names three roles - reporter, owner, contact
    person - and this function used to know only the first two, in a
    fixed order: the reporter's raw address first, the client record
    second. That order outlived triage. Once staff matched a report to
    its real owner, every client-facing e-mail this application sends -
    the status updates, the completion notice - still went to whoever
    happened to file the report, and nothing on any screen said so.

    ``Case.contact_details()`` resolves the designation instead, and
    falls back to exactly the old order for a case that has none, so
    rows created before §7.1 was asked keep reaching somebody.
    """
    _name, _phone, email = case.contact_details()
    return email or None


def _send_and_log(case, to_address, subject, body):
    """Send one e-mail and record an EmailLog row for the attempt."""
    from apps.notifications.models import EmailLog

    log = EmailLog(case=case, to_address=to_address, subject=subject, body=body)
    try:
        send_mail(
            subject,
            body,
            getattr(settings, "DEFAULT_FROM_EMAIL", None),
            [to_address],
            fail_silently=False,
        )
        log.success = True
    except Exception as exc:  # noqa: BLE001 - must not propagate
        logger.exception("Failed to send notification for case %s.", case.pk)
        log.success = False
        log.error = str(exc)
    log.save()
    return log.success


def _enqueue(task, *args) -> None:
    """Run a notification task - in a worker, or inline (NTF-05).

    With no broker configured (``CELERY_TASK_ALWAYS_EAGER``: the dev box,
    CI and the test suite) the task body runs immediately at the call
    site, exactly as this code did before Celery existed. Deferring it to
    ``transaction.on_commit`` in that mode would be wrong, not merely
    slower: ``TestCase`` wraps every test in a transaction that never
    commits, so the callbacks would be dropped and the mail would
    silently never be sent.

    With a broker, the job is queued only once the surrounding
    transaction commits (INV-13) - a worker must never be handed the
    primary key of a row that is not written yet.

    Never raises: a broken broker must not break the operation that
    triggered the notification, any more than a broken SMTP host does.
    """
    def _fail(exc_context):
        logger.exception(
            "Failed to %s notification task %s.",
            exc_context,
            getattr(task, "name", task),
        )

    def _dispatch():
        # Runs at commit time, i.e. OUTSIDE the try/except below, so it
        # needs its own: an unreachable broker raises here, and Django
        # would propagate that straight into the caller that just
        # committed - breaking the very operation NTF-03 promises a
        # notification failure cannot break.
        try:
            task.delay(*args)
        except Exception:  # noqa: BLE001 - must not propagate
            _fail("queue")

    try:
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", True):
            task(*args)
        else:
            transaction.on_commit(_dispatch)
    except Exception:  # noqa: BLE001 - must not propagate
        _fail("enqueue")


def client_email_for(case):
    """Public accessor: the best client e-mail address, or None."""
    return _recipient_for(case)


def client_phone_for(case):
    """The contact person's phone number for manual SMS, or None (§7.1)."""
    _name, phone, _email = case.contact_details()
    return phone or None


def send_client_email(case, subject, body) -> bool:
    """Send one already-composed e-mail to the case's client, logged as
    an ``EmailLog`` row (NTF-03). Returns whether the send succeeded;
    False when no address is on file.

    Deliberately synchronous, unlike the ``notify_*`` dispatchers
    (NTF-05). The boolean is a *delivery receipt* that two callers record
    as fact: ``apps.service.services`` stamps ``CompletionNotice.sent_at``
    with it (SVC-17), and ``apps.storage.services`` stamps
    ``expiry_reminder_sent_at``, where a failure must leave the stamp
    unset so the next cron run retries (WNT-03). "Queued" is not "sent",
    so do not move this onto the queue without giving ``EmailLog`` a real
    status field and both callers a way to wait for the outcome. Neither
    caller is on a request path, so there is no latency to reclaim.
    """
    to_address = _recipient_for(case)
    if not to_address:
        return False
    return bool(_send_and_log(case, to_address, subject, body))


def notify_case_transition(case, old_status, new_status):
    """NTF-02/NTF-05: queue the client's status-change e-mail.

    Called by the cases state machine via ``transaction.on_commit`` after
    every transition. Composition and sending happen in
    ``_deliver_case_transition`` below - in a Celery worker when a broker
    is configured, inline here when one is not.
    """
    _enqueue(
        tasks.case_transition,
        getattr(case, "pk", None),
        getattr(old_status, "pk", None),
        getattr(new_status, "pk", None),
    )


def _deliver_case_transition(case, old_status, new_status):
    """Send a client-facing e-mail about a case status transition.

    Called by the cases state machine (via ``transaction.on_commit``)
    after every transition. A status warrants a notification when its
    ``notify_client`` flag is set (NTF-02) - workflow data, not code.

    The mail carries the client-visible status name and nothing else: no
    price, no appointment, no link. Those details are staff's to send
    (see the module docstring), and a mail composed here would name them
    before staff had chosen to.

    Never raises: any failure is logged and swallowed so that a broken
    mail setup cannot break a status transition.

    The e-mail language is pinned to ``settings.LANGUAGE_CODE`` (NTF-03):
    the send runs in the staff request context via ``transaction.on_commit``
    and must not leak the staff user's UI language to the client.
    """
    try:
        if new_status is None or not new_status.notify_client:
            return

        to_address = _recipient_for(case)
        if not to_address:
            logger.info(
                "Skipping notification for case %s: no recipient e-mail.", case.pk
            )
            return

        with translation.override(settings.LANGUAGE_CODE):
            subject = _("Update on your service request: %(status)s") % {
                "status": new_status.client_visible_name
            }
            body_lines = [
                _("Hello,"),
                "",
                _("The status of your service request has changed to: %(status)s.")
                % {"status": new_status.client_visible_name},
                "",
                _("Thank you."),
            ]
            _send_and_log(case, to_address, subject, "\n".join(body_lines))
    except Exception:  # noqa: BLE001 - must not propagate
        logger.exception(
            "Unexpected error while notifying about case transition (case=%s).",
            getattr(case, "pk", None),
        )


def notify_case_created(case):
    """NTF-04/NTF-05: queue the client's creation confirmation.

    Composition and sending happen in ``_deliver_case_created`` below.
    """
    _enqueue(tasks.case_created, getattr(case, "pk", None))


def _deliver_case_created(case):
    """NTF-04: confirm to the client that their report was received.

    Sent for staff-entered cases (phone/e-mail/walk-in reports) right
    after creation, mirroring the portal's thank-you page: a plain
    "received" e-mail when an address is known - no link, there is no
    page to link to. When the case only has a phone number, no SMS
    gateway is configured, so a SYSTEM activity-log entry reminds staff
    to confirm receipt by phone/SMS instead. Never raises.

    The e-mail language is pinned to ``settings.LANGUAGE_CODE`` like the
    transition mail (NTF-03).
    """
    from apps.cases.models import ActivityLog

    try:
        to_address = _recipient_for(case)
        if not to_address:
            # §7.1: the number staff are told to ring is the contact
            # person's, not whichever raw field happens to be filled -
            # the same resolver the e-mail address comes from.
            _name, phone, _email = case.contact_details()
            by_phone = " (%s)" % phone if phone else ""
            ActivityLog.objects.create(
                case=case,
                author=None,
                kind=ActivityLog.Kind.SYSTEM,
                text=(
                    "NTF-04: no e-mail on file - confirmation not sent. "
                    "Confirm receipt with the client by phone/SMS%s." % by_phone
                ),
            )
            return

        with translation.override(settings.LANGUAGE_CODE):
            subject = _("We received your service request")
            body_lines = [
                _("Hello,"),
                "",
                _(
                    "Thank you for contacting us. Your request has been "
                    "recorded and our team will get back to you shortly."
                ),
                "",
                _("Thank you."),
            ]
            _send_and_log(case, to_address, subject, "\n".join(body_lines))
    except Exception:  # noqa: BLE001 - must not propagate
        logger.exception(
            "Unexpected error while sending the creation confirmation (case=%s).",
            getattr(case, "pk", None),
        )


def notify_new_portal_case(case):
    """NTF-01/NTF-05: queue the staff e-mail about a portal submission.

    With a broker (``REDIS_URL``) the portal visitor no longer pays the
    SMTP round trip inside their own request; without one (eager mode)
    the mail is still sent inline, after the commit.
    Composition and sending happen in ``_deliver_new_portal_case`` below.
    """
    _enqueue(tasks.new_portal_case, getattr(case, "pk", None))


def _deliver_new_portal_case(case):
    """NTF-01: notify staff about every new portal submission.

    Recipients come from ``settings.STAFF_NOTIFICATION_EMAILS``. One
    ``EmailLog`` row is written per recipient. Never raises.
    """
    try:
        recipients = getattr(settings, "STAFF_NOTIFICATION_EMAILS", [])
        if not recipients:
            logger.info(
                "Skipping staff notification for case %s: "
                "STAFF_NOTIFICATION_EMAILS is empty.",
                case.pk,
            )
            return

        # Pinned like the client mail: the send runs in the PORTAL
        # visitor's request context and staff mail must not follow it.
        with translation.override(settings.LANGUAGE_CODE):
            _send_staff_mail(case, recipients)
    except Exception:  # noqa: BLE001 - must not propagate
        logger.exception(
            "Unexpected error while notifying staff about a portal case (case=%s).",
            getattr(case, "pk", None),
        )


def notify_transport_price_declared(case):
    """TRP-03/NTF-05: queue the staff e-mail about a carrier price.

    Composition and sending happen in
    ``_deliver_transport_price_declared`` below.
    """
    _enqueue(tasks.transport_price_declared, getattr(case, "pk", None))


def _deliver_transport_price_declared(case):
    """TRP-03: notify staff that an external-carrier price was declared.

    When staff record the found carrier's price for an asset that own
    transport cannot handle (over ~10 m), the transport workflow can
    proceed - so the staff addresses in ``settings.STAFF_NOTIFICATION_EMAILS``
    are e-mailed (there is no SMS gateway). One ``EmailLog`` row per
    recipient. Never raises; the SYSTEM ``ActivityLog`` written by
    ``declare_carrier_price`` guarantees an in-app record regardless.
    """
    try:
        recipients = getattr(settings, "STAFF_NOTIFICATION_EMAILS", [])
        if not recipients:
            logger.info(
                "Skipping carrier-price notification for case %s: "
                "STAFF_NOTIFICATION_EMAILS is empty.",
                case.pk,
            )
            return

        order = getattr(case, "transport_order", None)
        # Pinned like the other staff mail: the send runs in the staff
        # request context and must not follow the sender's UI language.
        with translation.override(settings.LANGUAGE_CODE):
            subject = _("Transport carrier price declared for case #%(id)s") % {
                "id": case.pk
            }
            asset = getattr(case, "asset", None)
            body = _(
                "An external carrier price has been declared for a transport "
                "case; the transport can now be arranged.\n\n"
                "Case: #%(id)s\n"
                "Asset: %(asset)s (%(length)s m)\n"
                "Carrier: %(carrier)s\n"
                "Declared price: %(price)s\n"
            ) % {
                "id": case.pk,
                "asset": asset or _("(not linked)"),
                "length": asset.length_m if asset is not None else _("unknown"),
                "carrier": (order.carrier_name if order else "") or _("(unnamed)"),
                "price": order.carrier_price if order else _("unknown"),
            }
            for to_address in recipients:
                _send_and_log(case, to_address, subject, body)
    except Exception:  # noqa: BLE001 - must not propagate
        logger.exception(
            "Unexpected error while notifying staff about a declared "
            "carrier price (case=%s).",
            getattr(case, "pk", None),
        )


def _send_staff_mail(case, recipients):
    """Compose and send the NTF-01 staff mail (language already pinned)."""
    subject = _("New portal report #%(id)s") % {"id": case.pk}
    body = _(
        "A new service report was submitted via the public portal.\n\n"
        "Case: #%(id)s\n"
        "Reported by: %(name)s, %(phone)s %(email)s\n"
        "Asset: %(asset)s\n"
        "Location: %(location)s\n"
        "Problem: %(symptoms)s\n"
    ) % {
        "id": case.pk,
        "name": case.raw_contact_name,
        "phone": case.raw_phone,
        "email": case.raw_email,
        "asset": case.raw_asset_info,
        "location": case.location,
        "symptoms": case.symptoms_description,
    }
    for to_address in recipients:
        _send_and_log(case, to_address, subject, body)
