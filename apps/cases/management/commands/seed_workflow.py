"""
Idempotent seed command for the case workflow.

Seeds statuses and transitions for all case types, per-role approval
limit placeholders, the standard departure days and a short generic
inspection checklist. No price lists, no demo users and no sample
clients: create staff with ``createsuperuser`` and the admin, and enter
your own price lists on the backoffice pricing pages.

Safe to run repeatedly (get_or_create everywhere). Statuses and
transitions are kept in sync with the definitions below on every run;
approval limits, departure days and inspection points are only created
when missing, so staff edits survive re-runs.
"""
from decimal import Decimal

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.cases.models import Case, CaseType, Status, StatusHistory, StatusTransition


# (code, name, client_visible_name, is_terminal, notify_client)
# notify_client (NTF-02): client is e-mailed when a case enters the status.
#
# The client has no page on this site, so what the client is told
# automatically is deliberately short: "Received" (the thank-you page /
# NTF-04 confirmation say it), then one mail when the work is under way
# and one when it is done. notify_client is set only on the first status
# of each label, so consecutive statuses sharing a label never repeat the
# mail.
SERVICE_STATUSES = [
    ("new_report", "New report", "Received", False, False),
    ("intake_scheduled", "Intake scheduled", "Received", False, False),
    ("asset_received", "Item received", "In service", False, True),
    ("diagnostics", "Diagnostics", "In service", False, False),
    ("awaiting_approval", "Awaiting approval", "In service", False, False),
    ("awaiting_part", "Awaiting part", "In service", False, False),
    ("in_progress", "In progress", "In service", False, False),
    ("quality_check", "Quality check", "In service", False, False),
    ("ready_for_handover", "Ready for handover", "In service", False, False),
    ("closed", "Closed", "Completed", True, True),
]

# Extra edges beyond the linear chain.
SERVICE_EXTRA_TRANSITIONS = [
    # Consumable part in stock within approval limit - skips both waiting states.
    ("diagnostics", "in_progress"),
    # Approved, no part to order.
    ("awaiting_approval", "in_progress"),
    # Additional work discovered mid-job.
    ("in_progress", "awaiting_approval"),
    # Test failed, rework.
    ("quality_check", "in_progress"),
]

# Transport: the client is told "In progress" once the advance is in and
# the job is really ours to do.
TRANSPORT_STATUSES = [
    ("inquiry", "Inquiry", "Received", False, False),
    ("quote_sent", "Quote sent", "Received", False, False),
    ("advance_paid", "Advance paid", "In progress", False, True),
    ("departure_scheduled", "Departure scheduled", "In progress", False, False),
    ("in_transit", "In transit", "In progress", False, False),
    ("delivered", "Delivered", "In progress", False, False),
    ("closed", "Closed", "Completed", True, True),
]

# WNT-01/WNT-03: the storage is contracted *before* the priced estimate
# goes out. The client is mailed once the item is on site and once the
# case is closed.
STORAGE_STATUSES = [
    ("inquiry", "Inquiry", "Received", False, False),
    ("contracted", "Contracted", "Received", False, False),
    ("estimate_sent", "Estimate sent", "Received", False, False),
    ("asset_received", "Item received", "In storage", False, True),
    ("in_storage", "In storage", "In storage", False, False),
    ("ready_for_pickup", "Ready for pickup", "In storage", False, False),
    ("closed", "Closed", "Completed", True, True),
]

# STM-06 / ACT-01: roles allowed on restricted transitions. Releasing work
# after client consent (awaiting_approval -> in_progress) is a manager
# decision, as is closing a case; everything else stays unrestricted ([]).
MANAGERIAL_ROLES = ["MANAGER", "DIRECTOR"]


def _allowed_roles_for(from_code, to_code):
    """Return the allowed_roles list for a transition edge."""
    if to_code == "closed":
        return list(MANAGERIAL_ROLES)
    if (from_code, to_code) == ("awaiting_approval", "in_progress"):
        return list(MANAGERIAL_ROLES)
    # The detour through "Awaiting part" releases the work just the same
    # (its onward edge auto-fires), so it takes the same decision-maker.
    if (from_code, to_code) == ("awaiting_approval", "awaiting_part"):
        return list(MANAGERIAL_ROLES)
    return []


# Approval limits per role, in the default currency. Placeholders - adjust
# them on the backoffice approval-limits page.
APPROVAL_LIMITS = {
    "TECHNICIAN": Decimal("100"),
    "MANAGER": Decimal("1000"),
    "DIRECTOR": Decimal("5000"),
}

# TRP-05: standard departure days (code, name, weekday 0=Mon..6=Sun).
# Editable as data - staff edits (rename, deactivate) survive re-runs.
STANDARD_DEPARTURE_DAYS = [
    ("TUE", "Tuesday", 1),
    ("THU", "Thursday", 3),
    ("SAT", "Saturday", 5),
]

# SVC-08: a short, generic new-client inspection checklist so the
# inspection step has something to sign. Edit or extend it in the admin
# (InspectionPoint). (system, label, sort_order)
INSPECTION_POINTS = [
    ("ELECTRICAL", "Electrical system - batteries, wiring, switches", 0),
    ("ENGINE", "Mechanical parts - fluids, leaks, belts, wear", 1),
    ("SAFETY", "Safety equipment present and in date", 2),
    ("INTERIOR", "General condition - visible damage, cleanliness", 3),
]


class Command(BaseCommand):
    help = (
        "Seed workflow statuses/transitions, approval limits, departure "
        "days and a generic inspection checklist (idempotent)."
    )

    def handle(self, *args, **options):
        created_statuses = 0
        created_transitions = 0

        for case_type, definitions, extra in [
            (CaseType.SERVICE, SERVICE_STATUSES, SERVICE_EXTRA_TRANSITIONS),
            (CaseType.TRANSPORT, TRANSPORT_STATUSES, []),
            (CaseType.STORAGE, STORAGE_STATUSES, []),
        ]:
            statuses = {}
            for sort_order, (code, name, visible, is_terminal, notify) in enumerate(
                definitions
            ):
                wanted = {
                    "name": name,
                    "client_visible_name": visible,
                    "sort_order": sort_order,
                    "is_terminal": is_terminal,
                    "notify_client": notify,
                }
                status, created = Status.objects.get_or_create(
                    case_type=case_type, code=code, defaults=wanted
                )
                # The built-in workflow definition is authoritative: keep the
                # ordering, terminal/notify flags and display names in sync on
                # re-runs so a reordered or renamed chain converges.
                changed = [f for f, v in wanted.items() if getattr(status, f) != v]
                if not created and changed:
                    for field in changed:
                        setattr(status, field, wanted[field])
                    status.save(update_fields=changed)
                statuses[code] = status
                created_statuses += int(created)

            # Prune built-in statuses dropped from the definition, with their
            # transitions - but only when no case still sits there, so live
            # data is never destroyed.
            defined_codes = {row[0] for row in definitions}
            for status in Status.objects.filter(case_type=case_type).exclude(
                code__in=defined_codes
            ):
                if Case.objects.filter(status=status).exists():
                    continue
                # History rows keep (PROTECT) the statuses they name, so a
                # status someone once passed through stays as a row - but
                # with no edges it is unreachable and can leak nowhere.
                StatusTransition.objects.filter(
                    Q(from_status=status) | Q(to_status=status)
                ).delete()
                if not StatusHistory.objects.filter(
                    Q(from_status=status) | Q(to_status=status)
                ).exists():
                    status.delete()

            # Linear chain in definition order.
            chain = [
                (definitions[i][0], definitions[i + 1][0])
                for i in range(len(definitions) - 1)
            ]
            desired_edges = set(chain) | set(extra)
            for from_code, to_code in chain + list(extra):
                allowed_roles = _allowed_roles_for(from_code, to_code)
                transition, created = StatusTransition.objects.get_or_create(
                    from_status=statuses[from_code],
                    to_status=statuses[to_code],
                    defaults={"allowed_roles": allowed_roles},
                )
                # STM-06 role list is kept in sync on re-runs.
                if not created and transition.allowed_roles != allowed_roles:
                    transition.allowed_roles = allowed_roles
                    transition.save(update_fields=["allowed_roles"])
                created_transitions += int(created)

            # Prune stale built-in chain edges (both endpoints still defined
            # but the pair is no longer wanted); edges touching custom
            # statuses are left alone.
            for transition in StatusTransition.objects.filter(
                from_status__case_type=case_type
            ).select_related("from_status", "to_status"):
                pair = (transition.from_status.code, transition.to_status.code)
                if (
                    pair not in desired_edges
                    and transition.from_status.code in defined_codes
                    and transition.to_status.code in defined_codes
                ):
                    transition.delete()

        ApprovalLimit = django_apps.get_model("accounts", "ApprovalLimit")
        created_limits = 0
        for role, amount in APPROVAL_LIMITS.items():
            _, created = ApprovalLimit.objects.get_or_create(
                role=role, defaults={"max_amount": amount}
            )
            created_limits += int(created)

        StandardDepartureDay = django_apps.get_model(
            "transport", "StandardDepartureDay"
        )
        created_days = 0
        for code, name, weekday in STANDARD_DEPARTURE_DAYS:
            _, created = StandardDepartureDay.objects.get_or_create(
                code=code, defaults={"name": name, "weekday": weekday}
            )
            created_days += int(created)

        InspectionPoint = django_apps.get_model("service", "InspectionPoint")
        created_points = 0
        for system, label, sort_order in INSPECTION_POINTS:
            _, created = InspectionPoint.objects.get_or_create(
                system=system,
                label=label,
                defaults={"sort_order": sort_order, "is_active": True},
            )
            created_points += int(created)

        self.stdout.write(
            "Seeded workflow: %d new statuses, %d new transitions, "
            "%d new approval limits, %d new departure days, "
            "%d new inspection points."
            % (
                created_statuses,
                created_transitions,
                created_limits,
                created_days,
                created_points,
            )
        )
