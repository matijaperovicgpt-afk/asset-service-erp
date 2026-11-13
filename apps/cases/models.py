"""
Case models: the central case (ticket) plus its data-driven workflow.

Statuses and allowed transitions are stored as data (``Status`` /
``StatusTransition``) rather than hardcoded enums, so the workflow can be
adjusted per case type without code changes.
"""
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def generate_public_token() -> str:
    """
    Generate an unguessable token for the public tracking page.

    Numeric primary keys must never be exposed publicly; clients track
    their case via this token only. ``token_urlsafe(32)`` yields a
    ~43-character URL-safe string.
    """
    return secrets.token_urlsafe(32)


class CaseType(models.TextChoices):
    """Business lines a case can belong to."""

    SERVICE = "SERVICE", _("Service")
    STORAGE = "STORAGE", _("Storage")
    TRANSPORT = "TRANSPORT", _("Transport")


class ContactRole(models.TextChoices):
    """Who we ring about this case (procedure §7.1).

    §7.1 names three roles in one breath - *reporter, owner and contact
    person* - and the application had fields for two of them: the
    ``raw_*`` trio is the reporter, ``Case.client`` is the owner, and the
    contact person was nowhere. In the ordinary case all three are the
    same person, which is exactly why the distinction was easy to miss;
    in the awkward case a site employee reports a fault on an asset whose
    owner is in another country and whose skipper is the one who will
    actually meet the technician.

    This is a designation, not a second address book: the first two
    answers point at data the case already holds, and only the last two
    carry a name and number of their own. ``Case.contact_details()``
    resolves whichever it is.

    REPRESENTATIVE is not decoration beside OTHER. §6.8 already draws
    exactly this line for the delivery protocol - *the owner or their
    authorized representative* - and ``transport.DeliveryConfirmation``
    models it; the same distinction matters here, because §7.9's
    *client consent* can be given by a representative and cannot be
    given by the site employee who happened to notice the smoke.

    Blank means nobody has designated anyone yet.
    """

    REPORTER = "REPORTER", _("The person who reported it")
    OWNER = "OWNER", _("The owner")
    REPRESENTATIVE = "REPRESENTATIVE", _("The owner's authorized representative")
    OTHER = "OTHER", _("Someone else")


#: The two designations that carry their own name and number, because
#: they point at a person the case has no other record of. The other two
#: resolve to data already on the case, and their override fields are
#: cleared when they are chosen so a stale number cannot outlive them.
CONTACT_ROLES_WITH_OWN_DETAILS = frozenset(
    {ContactRole.REPRESENTATIVE, ContactRole.OTHER}
)


class Status(models.Model):
    """
    A workflow status for a given case type.

    ``client_visible_name`` is the simplified label the client is told in
    the automatic status e-mail (internal granularity is hidden from
    clients). There is no client page: the seeded workflow collapses the
    chain to "received" / "in service" / "finished" and sets
    ``notify_client`` only where that label changes, so a client gets
    exactly those mails and hears everything else from staff.
    """

    case_type = models.CharField(
        _("case type"), max_length=20, choices=CaseType.choices
    )
    code = models.SlugField(_("code"), max_length=50)
    name = models.CharField(_("name"), max_length=100)
    client_visible_name = models.CharField(
        _("client-visible name"),
        max_length=100,
        help_text=_("Simplified label the client sees in status e-mails."),
    )
    sort_order = models.PositiveIntegerField(_("sort order"), default=0)
    is_terminal = models.BooleanField(_("is terminal"), default=False)
    # NTF-02: entering this status sends the client an e-mail. Data-driven
    # (like the rest of the workflow), not a hardcoded code list.
    notify_client = models.BooleanField(
        _("notify client"),
        default=False,
        help_text=_("Send the client an e-mail when a case enters this status."),
    )

    class Meta:
        verbose_name = _("status")
        verbose_name_plural = _("statuses")
        ordering = ["case_type", "sort_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["case_type", "code"], name="unique_status_per_case_type"
            ),
        ]

    def __str__(self) -> str:
        return self.code or self.name


class StatusTransition(models.Model):
    """
    An allowed edge in the status graph.

    The workflow is data, not hardcoded enums: which status can move to
    which is defined entirely by these rows, per case type.
    """

    from_status = models.ForeignKey(
        "cases.Status",
        on_delete=models.CASCADE,
        related_name="transitions_from",
        verbose_name=_("from status"),
    )
    to_status = models.ForeignKey(
        "cases.Status",
        on_delete=models.CASCADE,
        related_name="transitions_to",
        verbose_name=_("to status"),
    )
    # STM-06 / ACT-01: roles (accounts.Role values) allowed to take this
    # transition. An empty list means any staff role may take it. Kept as
    # data so the workflow stays data-driven (STM-01/INV-10).
    allowed_roles = models.JSONField(
        _("allowed roles"),
        default=list,
        blank=True,
        help_text=_(
            "accounts.Role values allowed to take this transition; "
            "empty list means any staff role."
        ),
    )

    class Meta:
        verbose_name = _("status transition")
        verbose_name_plural = _("status transitions")
        constraints = [
            models.UniqueConstraint(
                fields=["from_status", "to_status"], name="unique_status_transition"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.from_status} -> {self.to_status}"


class ReportChannel(models.TextChoices):
    """How the case was reported to us."""

    PHONE = "PHONE", _("Phone")
    EMAIL = "EMAIL", _("Email")
    PORTAL = "PORTAL", _("Portal")
    IN_PERSON = "IN_PERSON", _("In person")


class WorkLocation(models.TextChoices):
    """Where the intervention is carried out (SVC-03).

    The procedure asks the responsible person to consult a technician,
    before the asset arrives, on whether the job can be done in the
    water, in the field, or in the service centre. Blank means the
    consult has not happened (or was not needed) - the procedure says
    "may", so an unanswered case behaves exactly as it always did and
    the asset is expected in.
    """

    ON_SITE = "ON_SITE", _("At the client's site")
    IN_FIELD = "IN_FIELD", _("In the field")
    SERVICE_CENTER = "SERVICE_CENTER", _("In the service centre")


class DeliveryMethod(models.TextChoices):
    """How the asset reaches the yard (SVC-04, procedure §7 and §8).

    The procedure names this twice - "agreed appointment and **delivery
    method**" among the mandatory ticket data, and again as what the
    status "Intake scheduled" means: "date **and arrival method**
    agreed". Both halves are agreed in the same conversation, so both
    are recorded before the case may claim that status.

    The two transported answers deliberately reuse the TRP-03 vocabulary
    (own vehicle vs specialized carrier), because it is the same
    distinction and the same yard doing it - a service case whose asset is
    collected by us is the transport process seen from the service side.

    Blank means the question has not been answered yet.
    """

    CLIENT_DROP_OFF = "CLIENT_DROP_OFF", _("Client drops it off")
    CLIENT_SHIPS = "CLIENT_SHIPS", _("Client ships it to us")
    SERVICE_PICKUP = "SERVICE_PICKUP", _("We collect it (own vehicle)")
    EXTERNAL_CARRIER = "EXTERNAL_CARRIER", _("Specialized carrier brings it")
    ALREADY_ON_SITE = "ALREADY_ON_SITE", _("Already at our site")


#: The answers under which the asset never enters the yard, so there is
#: nothing to receive and nothing to hand back. Kept as one set because
#: three guards and the guide all have to agree on it; if the business
#: decides a drive-leg job is done lifted out after all, this is the one
#: line to change.
ON_SITE_WORK_LOCATIONS = frozenset(
    {WorkLocation.ON_SITE, WorkLocation.IN_FIELD}
)


class Case(models.Model):
    """
    The central case (ticket) record.

    Public submissions land as raw data in the ``raw_*`` fields; during
    triage the staff match or create real ``clients.Client`` /
    ``clients.Asset`` records and link them via the ``client`` /
    ``asset`` FKs. The raw fields are kept for audit.
    """

    case_type = models.CharField(
        _("case type"), max_length=20, choices=CaseType.choices
    )
    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cases",
        verbose_name=_("client"),
    )
    asset = models.ForeignKey(
        "clients.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cases",
        verbose_name=_("asset"),
    )
    # Raw data from public submissions, matched to real records at triage.
    # These are the REPORTER's details (§7.1's "reporter") and
    # nothing later overwrites them - they are what was said when the
    # report came in, kept as said.
    raw_contact_name = models.CharField(_("raw contact name"), max_length=255, blank=True)
    raw_phone = models.CharField(_("raw phone"), max_length=50, blank=True)
    raw_email = models.CharField(_("raw email"), max_length=254, blank=True)
    raw_asset_info = models.TextField(_("raw asset info"), blank=True)

    # §7.1: the third role. Blank until somebody designates one; see
    # ``ContactRole`` and ``contact_details``.
    contact_role = models.CharField(
        _("contact person"), max_length=20, choices=ContactRole.choices, blank=True
    )
    contact_name = models.CharField(_("contact name"), max_length=255, blank=True)
    contact_phone = models.CharField(_("contact phone"), max_length=50, blank=True)
    contact_email = models.EmailField(_("contact email"), max_length=254, blank=True)

    report_channel = models.CharField(
        _("report channel"), max_length=20, choices=ReportChannel.choices
    )
    reported_at = models.DateTimeField(_("reported at"), default=timezone.now)
    location = models.CharField(_("location"), max_length=255, blank=True)
    symptoms_description = models.TextField(_("symptoms description"), blank=True)
    # SVC-01 / procedure §7.4: "symptom description AND the client's
    # assessment of the fault" are two data, not one. What the asset does is observation;
    # what the owner thinks is wrong is a hypothesis, and a diagnosis
    # that contradicts it is worth knowing about. Blank on the column
    # (a report is taken in seconds), demanded at the diagnostics gate.
    client_diagnosis = models.TextField(
        _("client's assessment of the fault"), blank=True
    )
    #: The explicit "we asked and they had no idea" answer, so that a
    #: question nobody asked is distinguishable from one that was asked
    #: and came back empty (the SVC-06 ``no_items_received`` shape).
    client_gave_no_assessment = models.BooleanField(
        _("client gave no assessment"), default=False
    )
    assigned_technician = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_cases",
        verbose_name=_("assigned technician"),
    )
    status = models.ForeignKey(
        "cases.Status",
        on_delete=models.PROTECT,
        related_name="cases",
        verbose_name=_("status"),
    )
    # An unguessable public identifier for the case, so that nothing
    # client-facing ever has to name a numeric ID. Since the tracking page
    # was removed nothing renders it; it stays as the case's public
    # identity should a client-facing channel ever need one.
    public_token = models.CharField(
        _("public token"),
        max_length=64,
        unique=True,
        db_index=True,
        editable=False,
        default=generate_public_token,
    )
    # SVC-03: the outcome of the pre-arrival consult with a technician.
    # Blank = not consulted, which is a valid state (the procedure says
    # "may") and the one every case created before this field was added
    # is in.
    work_location = models.CharField(
        _("work location"),
        max_length=20,
        choices=WorkLocation.choices,
        blank=True,
    )
    # SVC-04: blank on the column so cases opened before this rule keep
    # loading, and demanded by the ``intake_scheduled`` guard instead -
    # the record tolerates the gap, the gate does not.
    delivery_method = models.CharField(
        _("delivery method"),
        max_length=20,
        choices=DeliveryMethod.choices,
        blank=True,
        help_text=_("How the asset reaches the yard."),
    )
    financially_settled = models.BooleanField(_("financially settled"), default=False)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    @property
    def contact_person_recorded(self) -> bool:
        """§7.1: somebody has said who we ring - and it still reaches them.

        Asking only whether a role was chosen was one step short. A
        designation points at data that can change after it is made:
        ``record_contact_person`` refuses OWNER on a client with neither
        a phone nor an address, but that is a check made once, and the
        client it names can be edited or replaced afterwards. The case
        then said a contact was recorded while nothing could be sent
        anywhere - which is the state this field exists to make
        impossible, not to describe.

        So the question is asked of the resolved answer, and the bar is
        the same one ``record_contact_person`` applies: a way to reach
        them. A name alone is not a contact.
        """
        if not self.contact_role:
            return False
        _name, phone, email = self.contact_details()
        return bool(phone or email)

    def contact_details(self):
        """Return ``(name, phone, email)`` for whoever we contact (§7.1).

        The single place that answers "who do we write to about this
        case". Every client-facing e-mail and every manual phone call
        resolves through here, so a designation made once is honoured
        everywhere afterwards.

        The undesignated branch reproduces the behaviour this application
        had before §7.1 was asked - the reporter's details first, the
        client record second. It is kept for the cases that already exist
        (thousands of rows cannot retroactively grow a designation) and
        is deliberately the *only* branch that guesses.
        """
        client = self.client
        if self.contact_role in CONTACT_ROLES_WITH_OWN_DETAILS:
            return (self.contact_name, self.contact_phone, self.contact_email)
        if self.contact_role == ContactRole.OWNER:
            # Empty rather than a fallback when the client FK is gone.
            # Guarding this branch with "and client is not None" dropped
            # such a case into the guessing branch below, which answered
            # with the REPORTER while the screen still said OWNER - the
            # precise misrouting this field exists to stop, restored by
            # the one state the designation cannot describe.
            if client is None:
                return ("", "", "")
            return (client.name, client.phone, client.email)
        if self.contact_role == ContactRole.REPORTER:
            return (self.raw_contact_name, self.raw_phone, self.raw_email)
        return (
            self.raw_contact_name or (client.name if client else ""),
            self.raw_phone or (client.phone if client else ""),
            self.raw_email or (client.email if client else ""),
        )

    @property
    def client_assessment_recorded(self) -> bool:
        """SVC-01: has the client been asked what they think is wrong?

        Either they had a view and it is written down, or they were
        asked and had none. Silence is not an answer.
        """
        return bool(self.client_diagnosis.strip()) or self.client_gave_no_assessment

    @property
    def delivery_method_recorded(self) -> bool:
        """SVC-04: has "how does it get here" been answered?

        An on-site job answers it by not applying: nothing is delivered
        anywhere, so there is no method to agree and the guide drops the
        step rather than showing it as outstanding.
        """
        return bool(self.delivery_method) or self.works_on_site

    @property
    def works_on_site(self) -> bool:
        """Is the job done where the asset lies, rather than at the yard?"""
        return self.work_location in ON_SITE_WORK_LOCATIONS

    @property
    def requires_asset_intake(self) -> bool:
        """Does this case involve physically receiving the asset?

        Everything the intake phase produces - the intake record, the
        receipt confirmation, the handover record it is compared against
        - presumes the asset changed hands. An in-water or drive-leg job
        never does, so those records are not merely optional there, they
        are unwritable: there is no mount status that is true and no
        receipt anyone could sign. Blank (not consulted) keeps the
        original behaviour.
        """
        return not self.works_on_site

    class Meta:
        verbose_name = _("case")
        verbose_name_plural = _("cases")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        who = self.client or self.raw_contact_name or _("unknown")
        return f"#{self.pk} {self.get_case_type_display()} - {who}"


class AppendOnlyModel(models.Model):
    """
    Abstract base for audit records: rows may be created but never
    updated or deleted (INV-03). Cascade deletes of the parent case
    still work because Django's collector deletes at the SQL level
    without calling ``delete()`` on each instance.
    """

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            from .exceptions import AppendOnlyViolation

            raise AppendOnlyViolation(
                "%s records are append-only and cannot be updated (INV-03)."
                % type(self).__name__
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from .exceptions import AppendOnlyViolation

        raise AppendOnlyViolation(
            "%s records are append-only and cannot be deleted (INV-03)."
            % type(self).__name__
        )


class StatusHistory(AppendOnlyModel):
    """Audit trail of status changes for a case."""

    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="status_history",
        verbose_name=_("case"),
    )
    from_status = models.ForeignKey(
        "cases.Status",
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
        verbose_name=_("from status"),
    )
    to_status = models.ForeignKey(
        "cases.Status",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("to status"),
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("changed by"),
    )
    changed_at = models.DateTimeField(_("changed at"), default=timezone.now)
    note = models.TextField(_("note"), blank=True)

    class Meta:
        verbose_name = _("status history entry")
        verbose_name_plural = _("status history entries")
        ordering = ["-changed_at"]

    def __str__(self) -> str:
        return f"{self.case_id}: {self.from_status} -> {self.to_status}"


class ActivityLog(AppendOnlyModel):
    """
    Free-form activity entries on a case.

    ``PHONE_AGREEMENT`` exists because agreements made by phone that
    affect price, scope or deadline must be documented after the fact
    (maps to the Serbian practice of logging "telefonski negotiable").
    """

    class Kind(models.TextChoices):
        NOTE = "NOTE", _("Note")
        PHONE_AGREEMENT = "PHONE_AGREEMENT", _("Phone agreement")
        SYSTEM = "SYSTEM", _("System")

    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="activity_log",
        verbose_name=_("case"),
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("author"),
    )
    kind = models.CharField(
        _("kind"), max_length=20, choices=Kind.choices, default=Kind.NOTE
    )
    text = models.TextField(_("text"))
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("activity log entry")
        verbose_name_plural = _("activity log entries")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.case_id}: {self.get_kind_display()}"
