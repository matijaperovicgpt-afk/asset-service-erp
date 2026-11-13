"""
Business logic for the accounts app.

Currently one concern: keeping the approval ladder (``ApprovalLimit``
rows) coherent, so that the amounts staff enter in the backoffice really
produce the escalation ACT-02 describes.

- ``LADDER_ROLES``: the roles the ladder is made of, in escalation order.
- ``validate_approval_limit(role, max_amount, exclude_pk=None)``: the
  cross-row guard used by the backoffice configuration page (OPEN-01).
- ``may_edit_approval_ladder(user)`` / ``LADDER_EDIT_ROLES``: who may
  change the ladder at all - the director only, because whoever
  configures the ladder can otherwise approve past it (ACT-02).
- ``record_approval_limit_change(...)``: the append-only attribution row
  for every such change (INV-03).
"""
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from .models import ApprovalLimit, ApprovalLimitChange, Role

#: The roles ``quotes.services.required_approval_levels()`` actually
#: reads, in escalation order (ACT-02): up to the technician limit the
#: service decides on its own authority, above it a manager approval is
#: required, and above the manager limit a director approval is. A limit
#: on any other role would be dead configuration.
LADDER_ROLES = (Role.TECHNICIAN, Role.MANAGER, Role.DIRECTOR)


def validate_approval_limit(role, max_amount, exclude_pk=None) -> None:
    """
    OPEN-01 / ACT-02 / INV-04: validate one approval-limit row against
    the other rows, so the ladder ``required_approval_levels()`` derives
    stays coherent.

    Raises ``ValidationError`` when

    * ``role`` is not one of ``LADDER_ROLES`` (the approval ladder knows
      only technician self-authority, MANAGER and DIRECTOR, so a limit on
      any other role would never be read),
    * ``max_amount`` is missing or not positive,
    * the technician limit is not strictly below the manager limit -
      ``required_approval_levels()`` returns early on
      ``amount <= technician_limit``, so an equal or higher technician
      limit leaves the interval ``(technician, manager]`` empty and the
      MANAGER tier unreachable: every manager-tier quote would silently
      become a director-tier one,
    * the manager limit is above the director limit (keeps the displayed
      ladder monotonic; the director amount itself is never read as a
      ceiling),
    * the technician limit is not strictly below the director limit -
      with the manager row absent the technician limit is the only gate
      the ladder reads, so an equal or higher one would self-approve the
      amounts the director rung exists to catch.

    A partially configured ladder is accepted: a missing row is
    legitimate configuration and ``required_approval_levels()`` treats it
    conservatively by escalating to the next level up.

    ``exclude_pk`` allows re-validating an existing row (the
    ``storage.services.validate_band`` convention).
    """
    if role not in LADDER_ROLES:
        raise ValidationError(
            _(
                "Role '%(role)s' holds no approval limit: the approval "
                "ladder reads only %(allowed)s (ACT-02)."
            ) % {
                "role": role or _("(none)"),
                "allowed": ", ".join(str(r) for r in LADDER_ROLES),
            }
        )
    if max_amount is None or max_amount <= 0:
        raise ValidationError(
            _("An approval limit must be a positive amount in EUR, not %(amount)s.")
            % {"amount": max_amount if max_amount is not None else _("(empty)")}
        )

    rows = ApprovalLimit.objects.all()
    if exclude_pk is not None:
        rows = rows.exclude(pk=exclude_pk)
    limits = {row.role: row.max_amount for row in rows}
    limits[role] = max_amount

    technician = limits.get(Role.TECHNICIAN)
    manager = limits.get(Role.MANAGER)
    director = limits.get(Role.DIRECTOR)

    if technician is not None and manager is not None and technician >= manager:
        raise ValidationError(
            _(
                "Technician limit %(technician)s must stay strictly below the "
                "manager limit %(manager)s, otherwise the interval the manager "
                "tier covers is empty and no amount ever needs a manager "
                "approval. To remove the manager tier, delete the manager "
                "limit - amounts above the technician limit then escalate "
                "straight to the director. To remove the service's own "
                "self-approval authority, delete the technician limit - every "
                "quote then needs an internal approval."
            ) % {"technician": technician, "manager": manager}
        )
    if manager is not None and director is not None and manager > director:
        raise ValidationError(
            _(
                "Manager limit %(manager)s must not exceed the director limit "
                "%(director)s - the ladder has to rise with authority."
            ) % {"manager": manager, "director": director}
        )
    if technician is not None and director is not None and technician >= director:
        # With no manager row the technician limit is the only gate
        # ``required_approval_levels()`` reads, so a technician limit at or
        # above the director limit self-approves exactly the amounts the
        # director rung exists to catch - the same empty-interval mistake
        # as the technician/manager case, one rung further up.
        raise ValidationError(
            _(
                "Technician limit %(technician)s must stay below the director "
                "limit %(director)s: amounts the director rung is meant to "
                "catch would otherwise proceed on the service's own authority "
                "with no internal approval at all."
            ) % {"technician": technician, "director": director}
        )


#: ACT-02: who may change the approval ladder itself.
#:
#: Deliberately NOT the price-list roles (MANAGER/DIRECTOR). The ladder is
#: an authorization control, not a price list: a manager who may rewrite it
#: can raise or delete the very rung that forces a quote to the director
#: and then approve that quote alone, which is precisely what ACT-02
#: ("anything above the agreed limit requires the director's consent")
#: forbids. So the ladder is the director's (and a superuser's) to change,
#: while every staff role may read it.
LADDER_EDIT_ROLES = (Role.DIRECTOR,)


def may_edit_approval_ladder(user) -> bool:
    """True when ``user`` may add/edit/delete ``ApprovalLimit`` rows."""
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "role", None) in LADDER_EDIT_ROLES
    )


def record_approval_limit_change(
    *, role, action, actor=None, old_amount=None, new_amount=None
) -> ApprovalLimitChange:
    """
    Append one attribution row for a change to the approval ladder
    (INV-03: written, never updated or deleted).

    Called by the backoffice configuration views. Kept here rather than in
    a signal so that the acting user - which a model signal cannot see -
    is recorded.
    """
    return ApprovalLimitChange.objects.create(
        role=role,
        action=action,
        old_amount=old_amount,
        new_amount=new_amount,
        actor=actor if getattr(actor, "pk", None) else None,
        actor_role=getattr(actor, "role", "") or "",
    )
