"""Celery tasks for the notifications app (NTF-05).

Each task is a thin shim: it turns primary keys back into model
instances and hands off to the matching ``_deliver_*`` function in
``services.py``, where the composition, the NTF-03 language pinning, the
``EmailLog`` write and the never-raise contract still live. The split is
deliberate:

- the queue only ever carries JSON (integers and short strings), never
  pickled models;
- the worker re-reads the case, so a job can never send stale data;
- the ``apps.notifications.services`` logger and its ``try/except`` stay
  where the existing tests patch and assert them.

Tasks are fire-and-forget (``ignore_result=True``) and are NOT retried:
``_send_and_log`` records every attempt as an ``EmailLog`` row (NTF-03),
so a retry would multiply the audit trail. A failed notification is
visible in the log, in ``EmailLog`` and in Sentry; re-sending is a human
decision.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="notifications.case_transition", ignore_result=True)
def case_transition(case_id, old_status_id, new_status_id):
    """NTF-02: deliver the client's status-change e-mail."""
    from apps.cases.models import Case, Status
    from apps.notifications.services import _deliver_case_transition

    case = Case.objects.filter(pk=case_id).first()
    if case is None:
        logger.info("Case %s is gone; transition mail skipped.", case_id)
        return
    _deliver_case_transition(
        case,
        Status.objects.filter(pk=old_status_id).first(),
        Status.objects.filter(pk=new_status_id).first(),
    )


@shared_task(name="notifications.case_created", ignore_result=True)
def case_created(case_id):
    """NTF-04: deliver the client's creation confirmation."""
    from apps.cases.models import Case
    from apps.notifications.services import _deliver_case_created

    case = Case.objects.filter(pk=case_id).first()
    if case is None:
        logger.info("Case %s is gone; creation confirmation skipped.", case_id)
        return
    _deliver_case_created(case)


@shared_task(name="notifications.new_portal_case", ignore_result=True)
def new_portal_case(case_id):
    """NTF-01: deliver the staff e-mail about a new portal submission."""
    from apps.cases.models import Case
    from apps.notifications.services import _deliver_new_portal_case

    case = Case.objects.filter(pk=case_id).first()
    if case is None:
        logger.info("Case %s is gone; staff notification skipped.", case_id)
        return
    _deliver_new_portal_case(case)


@shared_task(name="notifications.transport_price_declared", ignore_result=True)
def transport_price_declared(case_id):
    """TRP-03: deliver the staff e-mail about a declared carrier price."""
    from apps.cases.models import Case
    from apps.notifications.services import _deliver_transport_price_declared

    case = Case.objects.filter(pk=case_id).first()
    if case is None:
        logger.info("Case %s is gone; carrier-price mail skipped.", case_id)
        return
    _deliver_transport_price_declared(case)
