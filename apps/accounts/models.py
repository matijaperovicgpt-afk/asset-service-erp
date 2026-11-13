"""
Account models: the custom user with a staff role, and per-role approval limits.

The ``role`` field drives what a staff member can see and do in the
back office (front desk intake, technician work orders, manager quote
approval, director-level overrides). Approval limits cap the quote
amount a given role may approve on its own.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    """Staff roles within the ERP organization."""

    FRONT_DESK = "FRONT_DESK", _("Front desk")
    TECHNICIAN = "TECHNICIAN", _("Technician")
    MANAGER = "MANAGER", _("Manager")
    DIRECTOR = "DIRECTOR", _("Director")


class User(AbstractUser):
    """
    Custom user model for site staff.

    Extends Django's ``AbstractUser`` with a ``role`` field. The role is
    optional (blank) so that superusers created via ``createsuperuser``
    do not need one; regular staff accounts should always have a role.
    """

    role = models.CharField(
        _("role"),
        max_length=20,
        choices=Role.choices,
        blank=True,
        help_text=_("Staff role. May be left blank for superusers."),
    )

    # Role-based approval authority lives in the quotes services:
    # ``quotes.services.required_approval_levels(amount)`` derives the
    # approval ladder from ``ApprovalLimit`` (ACT-02, INV-04).

    class Meta(AbstractUser.Meta):
        verbose_name = _("user")
        verbose_name_plural = _("users")

    def __str__(self) -> str:
        return self.get_full_name() or self.username

    def save(self, *args, **kwargs):
        """Every role is a staff role, so an account with one is staff.

        The backoffice admits ``is_staff`` users only, and the admin's
        "Add user" form asks for a role but has no "Staff status" box -
        so accounts created there could sign in and then got 403 on every
        page. To shut someone out, deactivate the account (``is_active``)
        instead of clearing staff status; a role re-sets it on save.
        """
        if self.role and not self.is_staff:
            self.is_staff = True
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "is_staff"}
        super().save(*args, **kwargs)


class ApprovalLimit(models.Model):
    """
    Maximum quote amount a given role may approve without escalation.

    One row per role. Quotes above the limit must be escalated to a role
    with a higher (or unset) limit.
    """

    role = models.CharField(
        _("role"),
        max_length=20,
        choices=Role.choices,
        unique=True,
    )
    max_amount = models.DecimalField(
        _("maximum amount"),
        max_digits=12,
        decimal_places=2,
        help_text=_("Highest quote total this role may approve."),
    )

    # Consumed by ``quotes.services.required_approval_levels()`` (INV-04);
    # rows are seeded with placeholder amounts by ``seed_workflow``
    # (real values pending, OPEN-01).

    class Meta:
        verbose_name = _("approval limit")
        verbose_name_plural = _("approval limits")

    def __str__(self) -> str:
        return f"{self.get_role_display()}: {self.max_amount}"


class ApprovalLimitChange(models.Model):
    """
    Append-only record of every change to the approval ladder (ACT-02).

    ``ApprovalLimit`` rows decide who has to approve a quote, so editing
    them is an authorization act, not price maintenance: without a trace
    a disputed self-approval cannot be reconstructed. While the ladder
    lived in the Django admin, ``django.contrib.admin.LogEntry`` provided
    that attribution; moving the rows onto a backoffice page (OPEN-01)
    took it away, so it is recorded here instead.

    Append-only like ``StatusHistory``/``ActivityLog`` (INV-03): the app
    exposes no update and no delete, and the row survives the deletion of
    the ``ApprovalLimit`` it describes - which is exactly the change worth
    keeping. ``actor`` is nullable only so that removing a staff account
    never erases the history it authored.
    """

    class Action(models.TextChoices):
        CREATED = "created", _("Created")
        UPDATED = "updated", _("Updated")
        DELETED = "deleted", _("Deleted")

    role = models.CharField(_("role"), max_length=20, choices=Role.choices)
    action = models.CharField(_("action"), max_length=10, choices=Action.choices)
    old_amount = models.DecimalField(
        _("previous amount"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Empty when the limit was created."),
    )
    new_amount = models.DecimalField(
        _("new amount"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Empty when the limit was deleted."),
    )
    actor = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approval_limit_changes",
        verbose_name=_("changed by"),
    )
    actor_role = models.CharField(_("role at the time"), max_length=20, blank=True)
    changed_at = models.DateTimeField(_("changed at"), auto_now_add=True)

    class Meta:
        verbose_name = _("approval limit change")
        verbose_name_plural = _("approval limit changes")
        ordering = ("-changed_at", "-pk")

    def __str__(self) -> str:
        return f"{self.get_role_display()} {self.action}: {self.old_amount} -> {self.new_amount}"
