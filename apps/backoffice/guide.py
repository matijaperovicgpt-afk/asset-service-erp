"""Guided workflow ("wizard") for the case detail page.

Pure read-model: for the case's current status it computes an ordered
checklist of preconditions (mirroring the ``GUARDS`` in
``apps.cases.services``) plus the outgoing transitions, each annotated
with done/actionable/waiting state per the viewing user's role. The
guide only *suggests* - every action still runs through the domain
services and ``transition_case()``, so a stale predicate can never
bypass a guard (the POST would surface the guard error, STM-04).

Statuses not in the registry (e.g. added later via the admin) fall back
to a plain list of the DB transitions.
"""
from dataclasses import dataclass, replace

from django.apps import apps as django_apps
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role
from apps.cases.models import Status, StatusTransition
from apps.cases.services import SETTLEMENT_ROLES, _role_allows

MANAGERIAL = ("MANAGER", "DIRECTOR")  # mirrors seed_workflow.MANAGERIAL_ROLES


@dataclass
class StepDef:
    """Static registry entry: one checklist item for a status."""

    key: str
    label: str
    predicate: callable  # (GuideContext) -> bool: is it done?
    roles: tuple | None = None  # None = any staff role
    anchor: str | None = None  # accordion section id on the detail page
    admin: tuple | None = None  # (app_label, model) -> admin add page
    blocking: bool = True  # mirrors a guard precondition; False = advisory
    url_name: str | None = None  # dedicated page, reversed with the case pk
    #: (GuideContext) -> bool: does this step exist for this case at all?
    #: None = always. A step that does not apply is dropped from the list
    #: rather than shown as done - an in-water job has no intake record to
    #: tick off, and rendering one as "completed" would be a lie in the
    #: opposite direction (SVC-03).
    applies: callable = None


@dataclass
class TransitionDef:
    """Static registry entry: an outgoing edge worth guiding towards."""

    code: str
    primary: bool = True
    hint: str = ""
    gate_keys: tuple | None = None  # step keys gating this edge; None = all blocking


@dataclass
class GuideStep:
    key: str
    label: str
    done: bool
    blocking: bool
    actionable: bool
    waiting_role: str | None
    anchor: str | None
    admin_url: str | None
    url: str | None = None  # direct link (triage, dedicated form pages)


@dataclass
class GuideTransition:
    code: str
    label: str
    primary: bool
    hint: str
    role_allowed: bool
    enabled: bool
    waiting_role: str | None


def _role_names(roles) -> str:
    labels = dict(Role.choices)
    return " / ".join(str(labels.get(r, r)) for r in roles)


class GuideContext:
    """Lazy predicate inputs, fed from data the detail view already
    fetched; only Diagnostic/TestRun add queries, and only for the
    SERVICE statuses that evaluate them."""

    def __init__(
        self,
        case,
        *,
        appointments=(),
        work_order=None,
        intake_records=(),
        quotes=(),
        advance_payments=(),
        part_orders=(),
        handover=None,
        transport_order=None,
        attachments=(),
    ):
        self.case = case
        self.appointments = list(appointments)
        self.work_order = work_order
        self.intake_records = list(intake_records)
        self.attachments = list(attachments)
        self.quotes = list(quotes)
        self.advance_payments = list(advance_payments)
        self.part_orders = list(part_orders)
        self.handover = handover
        self.transport_order = transport_order

    @property
    def latest_quote(self):
        return self.quotes[0] if self.quotes else None  # view orders -created_at

    def _has_appointment(self, kind) -> bool:
        return any(a.kind == kind for a in self.appointments)

    @property
    def has_intake_appointment(self) -> bool:
        """SVC-04, widened by SVC-03: an on-site job agrees a visit, not a
        delivery, and either kind proves a time was agreed - the same
        rule ``_guard_intake_scheduled`` applies."""
        if self.case.works_on_site:
            return self._has_appointment("ON_SITE_VISIT") or self._has_appointment(
                "INTAKE"
            )
        return self._has_appointment("INTAKE")

    @cached_property
    def within_approved_amount(self) -> bool:
        """SVC-14: does the work recorded so far still fit the approved
        amount? A cost that cannot be read counts as NOT within it - the
        guard refuses it too, and a checklist that said otherwise would
        promise a transition that is about to fail."""
        from apps.service.services import (
            CostUnknown,
            approved_amount,
            work_cost_so_far,
        )

        try:
            spent = work_cost_so_far(self.case)
        except CostUnknown:
            return False
        approved = approved_amount(self.case)
        return approved is not None and spent <= approved

    @property
    def work_location_recorded(self) -> bool:
        """SVC-03: has the pre-arrival consult been answered?"""
        return bool(self.case.work_location)

    @property
    def asset_identified(self) -> bool:
        """§7.2: all six asset data answered on the linked asset."""
        asset = self.case.asset
        return asset is not None and asset.is_fully_identified

    @property
    def contact_person_recorded(self) -> bool:
        """§7.1: somebody said who we ring about this case."""
        return self.case.contact_person_recorded

    @property
    def report_location_recorded(self) -> bool:
        """§7.3: somebody wrote down where the asset is."""
        return bool(self.case.location.strip())

    @property
    def energy_level_read(self) -> bool:
        """§7.6: the gauge was read, or declared absent."""
        return any(bool(r.energy_level) for r in self.intake_records)

    @property
    def client_assessment_recorded(self) -> bool:
        """SVC-01: the client was asked what they think is wrong."""
        return self.case.client_assessment_recorded

    @property
    def symptoms_recorded(self) -> bool:
        """SVC-01: something describes what the asset is doing."""
        return bool(self.case.symptoms_description.strip())

    @property
    def delivery_method_recorded(self) -> bool:
        """SVC-04: has the way the asset gets here been agreed?"""
        return self.case.delivery_method_recorded

    @cached_property
    def needs_asset_intake(self) -> bool:
        """Does the asset physically come in on this case?"""
        return self.case.requires_asset_intake

    @property
    def has_departure_appointment(self) -> bool:
        return self._has_appointment("TRANSPORT_DEPARTURE")

    @property
    def has_storage_in_appointment(self) -> bool:
        return self._has_appointment("STORAGE_IN")

    @property
    def has_storage_out_appointment(self) -> bool:
        return self._has_appointment("STORAGE_OUT")

    @property
    def signed_intake(self) -> bool:
        return any(r.signed_at for r in self.intake_records)

    @cached_property
    def has_intake_photos(self) -> bool:
        """SVC-07: at least one arrival photo (INTAKE-stage) on the case."""
        from apps.attachments.models import Attachment

        return any(
            a.stage == Attachment.Stage.INTAKE and a.kind == Attachment.Kind.PHOTO
            for a in self.attachments
        )

    @cached_property
    def work_order_client_approval(self) -> bool:
        """SVC-05: the client-signed scan is attached to the work order."""
        if self.work_order is None:
            return False
        from apps.service.services import has_client_approval

        return has_client_approval(self.work_order)

    @cached_property
    def has_handover_photos(self) -> bool:
        """§7/SVC-07: at least one photo of THIS departure.

        Asked of the handover record, the way
        ``handover_client_approval`` below already asks - a case can hold
        several handovers and the prefetched case-level list cannot tell
        which delivery a photo belongs to.
        """
        if self.handover is None:
            return False
        from apps.intake.services import has_handover_photos

        return has_handover_photos(self.handover)

    @cached_property
    def handover_client_approval(self) -> bool:
        """SVC-18: the client-signed handover scan is on the record."""
        if self.handover is None:
            return False
        from apps.intake.services import has_handover_approval

        return has_handover_approval(self.handover)

    @cached_property
    def inquiry_documented(self) -> bool:
        """TRP-01: enough of an inquiry on file to build an offer on.

        Not merely "a transport order row exists" - that ticked for a
        wholly blank order, which is how a case could claim an offer had
        been issued on an inquiry nobody took.
        """
        from apps.transport.services import inquiry_is_documented

        return inquiry_is_documented(self.transport_order)

    @cached_property
    def loading_plan_agreed(self) -> bool:
        """TRP-02: the loading/unloading plan is agreed, not just present."""
        from apps.transport.services import loading_plan_is_agreed

        return loading_plan_is_agreed(self.transport_order)

    @cached_property
    def carrier_quote_needed(self) -> bool:
        """TRP-03: this asset goes with a specialized carrier."""
        from apps.transport.services import carrier_quote_required

        return carrier_quote_required(self.transport_order)

    @cached_property
    def carrier_price_declared(self) -> bool:
        """TRP-03: the carrier's own offer is on file."""
        order = self.transport_order
        return order is not None and order.carrier_price_declared_at is not None

    @cached_property
    def loading_confirmation(self):
        """TRP-07: the internal at-loading sign-off."""
        return getattr(self.transport_order, "loading_confirmation", None)

    @cached_property
    def delivery_confirmation(self):
        """TRP-08: the delivery protocol on the transport order."""
        return getattr(self.transport_order, "delivery_confirmation", None)

    @cached_property
    def has_delivery_protocol(self) -> bool:
        """TRP-08: the signed delivery-protocol scan is on the confirmation."""
        if self.delivery_confirmation is None:
            return False
        from apps.transport.services import has_delivery_protocol

        return has_delivery_protocol(self.delivery_confirmation)

    @cached_property
    def has_work_log(self) -> bool:
        """SVC-15: at least one performed-work entry on the work order."""
        return (
            self.work_order is not None
            and self.work_order.work_log.exists()
        )

    @cached_property
    def completion_notified(self) -> bool:
        """SVC-17: the client got (or has drafted) a completion notice."""
        return self.case.completion_notices.exists()

    @cached_property
    def has_diagnostic(self) -> bool:
        # Mirrors the SVC-09 guard: only a diagnostic with all four
        # mandatory pieces counts, so the checkbox never ticks while the
        # transition would still be refused.
        Diagnostic = django_apps.get_model("service", "Diagnostic")
        return Diagnostic.complete_for_case(self.case).exists()

    @cached_property
    def is_new_client(self) -> bool:
        from apps.service.services import is_new_client

        return is_new_client(self.case)

    @cached_property
    def inspection_signed(self) -> bool:
        from apps.service.services import inspection_complete_for_case

        return inspection_complete_for_case(self.case)

    @cached_property
    def latest_test_run(self):
        # Mirrors the SVC-16 guard: only a run of the current control counts.
        from apps.cases.services import latest_quality_control_run

        return latest_quality_control_run(self.case)

    @cached_property
    def estimate_quotes(self) -> list:
        """WNT-03: the estimate is the quote drafted AFTER contracting; an
        offer sent during the inquiry does not count (mirrors
        ``_guard_estimate_sent``)."""
        from apps.cases.services import status_reached_at

        since = status_reached_at(self.case, "contracted")
        return [q for q in self.quotes if since is None or q.created_at >= since]

    @property
    def open_part_orders(self):
        return [o for o in self.part_orders if o.received_at is None]

    @property
    def paid_advance(self) -> bool:
        return any(a.paid_at for a in self.advance_payments)

    @property
    def any_advance(self) -> bool:
        return bool(self.advance_payments)

    def client_approved(self) -> bool:
        quote = self.latest_quote
        if quote is None:
            return False
        return any(
            ap.level == "client" and ap.decision == "approved"
            for ap in quote.approvals.all()
        )

    def internal_missing(self) -> list:
        quote = self.latest_quote
        if quote is None:
            return ["manager"]
        from apps.quotes.services import missing_approval_levels

        return [lv for lv in missing_approval_levels(quote) if lv != "client"]

    def unbacked_nonstock_orders(self) -> bool:
        from apps.parts.services import is_stock_part

        return any(
            not is_stock_part(o.part) and o.advance_payment_id is None
            for o in self.open_part_orders
        )

    @cached_property
    def storage_agreement(self):
        return getattr(self.case, "storage_agreement", None)

    @cached_property
    def storage_docs_complete(self) -> bool:
        """WNT-01: part of the contracted guard - a client contact plus
        the permit number and asset length on the agreement."""
        contact = bool(self.case.raw_phone or self.case.raw_email)
        if not contact and self.case.client_id is not None:
            contact = bool(self.case.client.phone or self.case.client.email)
        agreement = self.storage_agreement
        return (
            contact
            and agreement is not None
            and bool(agreement.permit_no)
            and agreement.asset_length_m is not None
        )

    @cached_property
    def storage_pricing_basis_complete(self) -> bool:
        """WNT-03: the accommodation and storage term are recorded, so the
        value estimate can be computed dynamically once contracted (mirrors
        the extra inputs the ``contracted`` guard requires)."""
        agreement = self.storage_agreement
        return (
            agreement is not None
            and bool(agreement.accommodation)
            and bool(agreement.term)
        )

    @cached_property
    def storage_database_entry_complete(self) -> bool:
        """WNT-02 (procedure §5.2): the site the asset comes from, its
        ownership and the services the client chose. Part of the
        ``contracted`` guard, so it belongs in the checklist too - a
        blocking step the wizard shows as outstanding beats a transition
        that looks available and then refuses."""
        agreement = self.storage_agreement
        return (
            agreement is not None
            and bool(agreement.origin_site)
            and bool(agreement.ownership)
            and agreement.selected_services.exists()
        )

    @cached_property
    def storage_permit_on_file(self) -> bool:
        """WNT-01: the operating permit itself, not only its number.
        Advisory - the scan routinely follows the inquiry by days."""
        agreement = self.storage_agreement
        if agreement is None:
            return False
        from apps.storage.services import has_permit

        return has_permit(agreement)

    @cached_property
    def has_storage_position(self) -> bool:
        agreement = self.storage_agreement
        return agreement is not None and agreement.positions.exists()


# ---------------------------------------------------------------------------
# Step registry - one entry per (case_type, status code)
# ---------------------------------------------------------------------------

QUOTE_STEPS = [
    StepDef(
        "create_quote",
        _("Create a quote for the client"),
        lambda c: c.latest_quote is not None,
        anchor="acc-quotes",
        blocking=False,
    ),
    StepDef(
        "send_quote",
        _("Send the quote"),
        lambda c: c.latest_quote is not None and c.latest_quote.status != "draft",
        anchor="acc-quotes",
        blocking=False,
    ),
]

#: SVC-13 / INV-04: what ``_guard_quote_approved`` asks of the latest
#: quote - on ``awaiting_approval`` and on ``awaiting_part`` alike.
APPROVAL_STEPS = [
    StepDef(
        "client_approval",
        _("Record the client's approval"),
        lambda c: c.client_approved(),
        anchor="acc-quotes",
    ),
    StepDef(
        "internal_approvals",
        _("Record internal approval(s) required by the amount"),
        lambda c: not c.internal_missing(),
        roles=MANAGERIAL,
        anchor="acc-quotes",
    ),
]

#: SVC-03: the four handover steps document *returning* an asset and are
#: compared against the intake record, so they do not exist on a case worked
#: on site or in the field. Non-SERVICE cases never set a work
#: location, so this reads True for them and nothing changes.
_ASSET_CAME_IN = lambda c: c.needs_asset_intake  # noqa: E731

CLOSING_STEPS = [
    StepDef(
        "create_handover",
        _("Create the handover record"),
        lambda c: c.handover is not None,
        anchor="acc-handover",
        applies=_ASSET_CAME_IN,
    ),
    StepDef(
        "upload_handover_photos",
        _("Photograph the asset as it is handed over"),
        lambda c: c.has_handover_photos,
        anchor="acc-handover",
        applies=_ASSET_CAME_IN,
    ),
    StepDef(
        "handover_tech_sign",
        _("Worker signs the handover after the comparison"),
        lambda c: c.handover is not None
        and c.handover.technician_signed_at is not None,
        anchor="acc-handover",
        applies=_ASSET_CAME_IN,
    ),
    StepDef(
        "handover_client_scan",
        _("Upload the client-signed handover approval (scan)"),
        lambda c: c.handover_client_approval,
        anchor="acc-handover",
        applies=_ASSET_CAME_IN,
    ),
    StepDef(
        "sign_handover",
        _("Compare with intake and capture the receiving signature"),
        lambda c: c.handover is not None and c.handover.signed_at is not None,
        anchor="acc-handover",
        applies=_ASSET_CAME_IN,
    ),
    StepDef(
        "mark_settled",
        _("Mark the case financially settled"),
        lambda c: c.case.financially_settled,
        roles=SETTLEMENT_ROLES,
        anchor="acc-closing",
    ),
]

STORAGE_DOCS_STEP = StepDef(
    "record_documentation",
    _("Record the inquiry documentation (contact, permit, asset length)"),
    lambda c: c.storage_docs_complete,
    anchor="acc-storage-docs",
)

STORAGE_DATABASE_ENTRY_STEP = StepDef(
    "record_database_entry",
    _("Record the database entry (site, ownership, selected services)"),
    lambda c: c.storage_database_entry_complete,
    anchor="acc-storage-docs",
    url_name="backoffice:storage-intake",
)

STORAGE_PERMIT_STEP = StepDef(
    "file_permit",
    _("File the permit document"),
    lambda c: c.storage_permit_on_file,
    anchor="acc-storage-docs",
    url_name="backoffice:storage-intake",
    blocking=False,
)

STORAGE_PRICING_BASIS_STEP = StepDef(
    "record_pricing_basis",
    _("Record the pricing basis (accommodation and storage term)"),
    lambda c: c.storage_pricing_basis_complete,
    anchor="acc-storage-docs",
)

STORAGE_LINK_ASSET_STEP = StepDef(
    "link_asset",
    _("Link the asset to the case - length drives storage pricing"),
    lambda c: c.case.asset_id is not None,
    anchor="acc-asset-link",
)

# WNT-03: after contracting, the value estimate is drafted (dynamically
# priced), adjusted by staff, and sent - mirroring the estimate_sent guard.
STORAGE_ESTIMATE_STEPS = [
    StepDef(
        "create_estimate",
        _("Draft the value estimate for the client"),
        lambda c: bool(c.estimate_quotes),
        anchor="acc-quotes",
    ),
    StepDef(
        "send_estimate",
        _("Send the value estimate"),
        lambda c: any(q.status != "draft" for q in c.estimate_quotes),
        anchor="acc-quotes",
    ),
]

# WNT-05: ``_guard_in_storage`` refuses the move without a placement, so
# the step blocks on ``asset_received``; later phases list it advisory.
STORAGE_POSITION_STEP = StepDef(
    "assign_position",
    _("Assign a storage position (hall/row/slot)"),
    lambda c: c.has_storage_position,
    anchor="acc-storage-position",
)

# Which workflow phase group on the case page hosts each section anchor.
# Drives the "current phase" accent; anchors outside the work zone
# (transitions, case-file sections) deliberately map to no phase.
ANCHOR_PHASES = {
    "acc-contact-person": "intake",
    "acc-fault-report": "intake",
    "acc-asset-identity": "intake",
    "acc-work-location": "intake",
    "acc-appointments": "intake",
    "acc-work-order": "intake",
    "acc-intake": "intake",
    "acc-inspection": "approval",
    "acc-diagnostics": "approval",
    "acc-quotes": "approval",
    "acc-advances": "approval",
    "acc-part-orders": "approval",
    "acc-work-execution": "work",
    "acc-quality": "work",
    "acc-transport": "work",
    "acc-storage-docs": "intake",
    "acc-asset-link": "intake",
    "acc-storage-position": "work",
    "acc-completion": "closing",
    "acc-handover": "closing",
    "acc-closing": "closing",
}


REGISTRY = {
    # ---- SERVICE ---------------------------------------------------------
    ("SERVICE", "new_report"): {
        "steps": [
            StepDef(
                "work_location_consult",
                _(
                    "Consult a technician: on site, in the field, "
                    "or in the service centre?"
                ),
                lambda c: c.work_location_recorded,
                anchor="acc-work-location",
                # SVC-03 says the consult happens "by need" - advisory, so
                # a case that plainly comes into the yard is never stalled
                # by it. What it changes when answered is whether the rest
                # of the case expects to receive the asset at all.
                blocking=False,
            ),
            StepDef(
                "book_intake_appointment",
                _("Book an intake appointment with the client"),
                lambda c: c.has_intake_appointment,
                anchor="acc-appointments",
                applies=lambda c: c.needs_asset_intake,
            ),
            # SVC-04: the second half of the same conversation, and a real
            # gate - ``_guard_intake_scheduled`` refuses without it. Drops
            # off an on-site case, where nothing is delivered anywhere.
            # SVC-01: asked while the client is still on the phone,
            # refused only at the diagnostics gate - so it is advisory
            # here and blocking there. The same question, at the two
            # moments that matter for it.
            StepDef(
                "record_client_assessment",
                _("Ask the client what they think is wrong"),
                lambda c: c.client_assessment_recorded,
                anchor="acc-fault-report",
                blocking=False,
            ),
            # §7.1: the first of the four, because the other three
            # are things you learn from whoever this names.
            StepDef(
                "record_contact_person",
                _("Say who we contact about this case"),
                lambda c: c.contact_person_recorded,
                anchor="acc-contact-person",
            ),
            # §7.3: asked before the delivery method, because the
            # delivery method is a function of it.
            StepDef(
                "record_report_location",
                _("Record where the asset is"),
                lambda c: c.report_location_recorded,
                anchor="acc-fault-report",
            ),
            StepDef(
                "record_delivery_method",
                _("Agree how the asset gets here"),
                lambda c: c.delivery_method_recorded,
                anchor="acc-work-location",
                applies=lambda c: c.needs_asset_intake,
            ),
            StepDef(
                "book_on_site_visit",
                _("Agree an on-site visit with the client"),
                lambda c: c.has_intake_appointment,
                anchor="acc-appointments",
                applies=lambda c: not c.needs_asset_intake,
            ),
        ],
        "transitions": [TransitionDef("intake_scheduled")],
    },
    ("SERVICE", "intake_scheduled"): {
        "steps": [
            StepDef(
                "create_work_order",
                _("Open the work order"),
                lambda c: c.work_order is not None,
                anchor="acc-work-order",
            ),
            StepDef(
                "finalize_work_order",
                _("Mark the work order final"),
                lambda c: c.work_order is not None and not c.work_order.is_preliminary,
                anchor="acc-work-order",
            ),
            StepDef(
                "upload_client_approval",
                _("Upload the client-signed work order (scan)"),
                lambda c: c.work_order_client_approval,
                anchor="acc-work-order",
            ),
            StepDef(
                "sign_work_order",
                _("Technician signs the work order"),
                lambda c: c.work_order is not None and c.work_order.signed_at,
                roles=("TECHNICIAN",),
                anchor="acc-work-order",
            ),
            # §7.2: the intake record is a receipt for a particular
            # asset, and ``sign_intake_record`` refuses one that cannot
            # say which. Listed before the record itself, because it is
            # the question the record answers.
            #
            # It has NO ``applies``: "asset data" are ticket data,
            # so an on-site case is asked too, where the gate is the
            # documentation check inside ``_guard_asset_received``
            # rather than the receipt. Dropping the step there was how
            # the wizard came to agree with a rule that was not enforced.
            StepDef(
                "identify_asset",
                _("Complete the asset's identification"),
                lambda c: c.asset_identified,
                anchor="acc-asset-identity",
            ),
            StepDef(
                "create_intake_record",
                _("Create the intake record"),
                lambda c: bool(c.intake_records),
                anchor="acc-intake",
                applies=lambda c: c.needs_asset_intake,
            ),
            StepDef(
                "upload_arrival_photos",
                _("Photograph the asset on arrival (wash it first if dirty)"),
                lambda c: c.has_intake_photos,
                anchor="acc-intake",
                applies=lambda c: c.needs_asset_intake,
            ),
            StepDef(
                "sign_intake_record",
                _("Confirm asset receipt (sign the intake record)"),
                lambda c: c.signed_intake,
                anchor="acc-intake",
                applies=lambda c: c.needs_asset_intake,
            ),
            # SVC-03/SVC-07: nothing arrives, but the asset is still
            # photographed before anyone touches it - that half of the
            # arrival documentation is what the guard keeps.
            StepDef(
                "upload_visit_photos",
                _("Photograph the asset at the visit, before starting work"),
                lambda c: c.has_intake_photos,
                anchor="acc-intake",
                applies=lambda c: not c.needs_asset_intake,
            ),
        ],
        "transitions": [TransitionDef("asset_received")],
    },
    ("SERVICE", "asset_received"): {
        "steps": [
            # SVC-01 / §7.4: the technician about to open the engine is
            # the reader of both halves of the fault report, so this is
            # where they stop being advice.
            StepDef(
                "record_symptoms",
                _("Describe what the asset is doing"),
                lambda c: c.symptoms_recorded,
                anchor="acc-fault-report",
            ),
            StepDef(
                "record_client_assessment",
                _("Record the client's assessment of the fault"),
                lambda c: c.client_assessment_recorded,
                anchor="acc-fault-report",
            ),
        ],
        "transitions": [TransitionDef("diagnostics", hint=_("Start diagnostics"))],
    },
    ("SERVICE", "diagnostics"): {
        "steps": [
            StepDef(
                "new_client_inspection",
                _("Perform the new-client full inspection"),
                lambda c: not c.is_new_client or c.inspection_signed,
                anchor="acc-inspection",
            ),
            StepDef(
                "record_diagnostics",
                _("Record diagnostic findings"),
                lambda c: c.has_diagnostic,
                anchor="acc-diagnostics",
            ),
        ],
        "transitions": [
            TransitionDef(
                "awaiting_approval",
                hint=_("Quote needed - client consent required"),
            ),
            TransitionDef(
                "in_progress",
                primary=False,
                gate_keys=("record_diagnostics",),
                hint=_(
                    "Shortcut: a stock consumable within the service's own "
                    "approval limit - the limit is enforced"
                ),
            ),
        ],
    },
    ("SERVICE", "awaiting_approval"): {
        "steps": [
            *QUOTE_STEPS,
            *APPROVAL_STEPS,
            StepDef(
                "order_part",
                _("Record a part order with an expected delivery date"),
                lambda c: any(o.expected_delivery for o in c.open_part_orders),
                anchor="acc-part-orders",
                blocking=False,
            ),
            StepDef(
                "part_advance",
                _("Link a paid 50% advance to non-stock part orders"),
                lambda c: not c.unbacked_nonstock_orders(),
                anchor="acc-advances",
                blocking=False,
            ),
        ],
        "transitions": [
            TransitionDef(
                "in_progress",
                gate_keys=("create_quote", "send_quote", "client_approval", "internal_approvals"),
            ),
            TransitionDef(
                "awaiting_part",
                primary=False,
                hint=_("A part must be ordered first"),
                gate_keys=("order_part", "part_advance"),
            ),
        ],
    },
    ("SERVICE", "awaiting_part"): {
        # A case waiting on a part is still a case waiting on consent:
        # ``_guard_quote_approved`` runs on this edge too (QA SV-C1).
        "steps": [
            *QUOTE_STEPS,
            *APPROVAL_STEPS,
            StepDef(
                "receive_parts",
                _("Mark the ordered part(s) received"),
                lambda c: not c.open_part_orders,
                anchor="acc-part-orders",
                blocking=False,
            ),
        ],
        "transitions": [
            TransitionDef(
                "in_progress",
                gate_keys=("create_quote", "send_quote", "client_approval", "internal_approvals"),
            ),
        ],
    },
    ("SERVICE", "in_progress"): {
        "steps": [
            # SVC-15 (procedure §4.1): "according to the approved order". Since
            # SVC-02 opens a preliminary order at triage, the work card is
            # on screen from the first minute of a case - this says which
            # order it is waiting for, rather than leaving the technician
            # to discover the refusal by posting the form.
            StepDef(
                "work_order_endorsed",
                _("Have the work order endorsed before recording work"),
                lambda c: c.work_order is not None
                and not c.work_order.is_preliminary,
                anchor="acc-work-execution",
                blocking=False,
            ),
            StepDef(
                "log_work",
                _("Record performed works, installed parts and time spent"),
                lambda c: c.has_work_log,
                anchor="acc-work-execution",
                blocking=False,
            ),
            StepDef(
                "within_approved_amount",
                _("Keep the work within the approved amount"),
                lambda c: c.within_approved_amount,
                anchor="acc-work-execution",
            ),
        ],
        "transitions": [
            TransitionDef(
                "quality_check",
                hint=_("Work finished - send to quality control"),
                gate_keys=("within_approved_amount",),
            ),
            # Going back for a new quote is the remedy for work that has
            # outgrown the approved amount, so it must never be gated on
            # the within-amount step - otherwise an over-budget case has
            # both edges disabled and is locked in place (the same trap
            # the quality_check rework edge avoids).
            TransitionDef(
                "awaiting_approval",
                primary=False,
                hint=_("Additional work discovered - new quote needed"),
                gate_keys=(),
            ),
        ],
    },
    ("SERVICE", "quality_check"): {
        "steps": [
            StepDef(
                "record_pass_test_run",
                _("Record a passing quality-control test run"),
                lambda c: c.latest_test_run is not None
                and c.latest_test_run.result == "PASS",
                anchor="acc-quality",
            ),
        ],
        "transitions": [
            TransitionDef("ready_for_handover"),
            # Rework is the failed-test path itself, so it must never be
            # gated on the passing-test step - otherwise a FAIL leaves both
            # quality_check edges disabled and the case is locked in place.
            TransitionDef(
                "in_progress",
                primary=False,
                hint=_("Test failed - rework"),
                gate_keys=(),
            ),
        ],
    },
    ("SERVICE", "ready_for_handover"): {
        "steps": [
            StepDef(
                "notify_completion",
                _("Notify the client: works, final cost, pickup/delivery"),
                lambda c: c.completion_notified,
                anchor="acc-completion",
                blocking=False,
            ),
        ] + list(CLOSING_STEPS),
        "transitions": [TransitionDef("closed")],
    },
    ("SERVICE", "closed"): {"steps": [], "transitions": []},
    # ---- TRANSPORT -------------------------------------------------------
    ("TRANSPORT", "inquiry"): {
        # TRP-01: the inquiry form is the only place a TransportOrder is
        # created, and every later transport card (offer, loading,
        # delivery) hangs off that order - so recording it blocks the
        # guided quote_sent transition even though no DB guard needs it.
        #
        # The offer steps block here (unlike SERVICE, where they are
        # advisory): TRP-01 issues the quote BEFORE transport is organized,
        # and TRP-04 derives the mandatory advance from it - so a case that
        # reaches ``advance_paid`` with no sent offer has nothing to compute
        # the advance from and dead-ends against the INV-06 guard.
        "steps": [
            StepDef(
                "record_transport_inquiry",
                _("Record the transport inquiry (TRP-01)"),
                lambda c: c.inquiry_documented,
                url_name="backoffice:transport-intake",
            ),
            # TRP-03: the specialized carrier's own offer is what the
            # client offer should be priced from, so it is prompted here -
            # advisory, because it only has to exist by the time the
            # departure is organized, which is where the guard is.
            StepDef(
                "obtain_carrier_quote",
                _("Obtain the external carrier's offer (TRP-03)"),
                lambda c: c.carrier_price_declared,
                anchor="acc-transport",
                blocking=False,
                applies=lambda c: c.carrier_quote_needed,
            ),
        ] + [replace(step, blocking=True) for step in QUOTE_STEPS],
        "transitions": [TransitionDef("quote_sent")],
    },
    ("TRANSPORT", "quote_sent"): {
        "steps": [
            StepDef(
                "client_approval",
                _("Record the client's acceptance of the quote"),
                lambda c: c.client_approved(),
                anchor="acc-quotes",
                blocking=False,
            ),
            # TRP-04: sending the offer derives the 50% advance
            # (``transport.services.ensure_transport_advance``), so this
            # step normally ticks itself; it blocks because ``advance_paid``
            # has nothing to confirm without it and the INV-06 guard would
            # dead-end the case.
            StepDef(
                "request_advance",
                _("Calculate the 50% advance from the offer"),
                lambda c: c.any_advance,
                anchor="acc-advances",
            ),
            # The status says the advance is PAID, and so does the guard
            # (QA T-M1): an unpaid row does not carry the case there.
            StepDef(
                "mark_advance_paid",
                _("Confirm the advance was paid"),
                lambda c: c.paid_advance,
                anchor="acc-advances",
            ),
        ],
        "transitions": [TransitionDef("advance_paid")],
    },
    ("TRANSPORT", "advance_paid"): {
        "steps": [
            StepDef(
                "mark_advance_paid",
                _("Confirm the advance was paid"),
                lambda c: c.paid_advance,
                anchor="acc-advances",
            ),
            # TRP-05: "Departure scheduled" names a date - a booked departure
            # or at least the date agreed on the inquiry; the guard refuses
            # the move with neither (QA T-M2).
            StepDef(
                "book_departure",
                _("Book the transport departure appointment"),
                lambda c: c.has_departure_appointment
                or bool(getattr(c.transport_order, "preferred_date", None)),
                anchor="acc-appointments",
            ),
            # TRP-03: "from whom a separate quote is obtained" - scheduling
            # the departure IS organizing the transport, so by here the
            # carrier's price is a hard precondition
            # (``_guard_carrier_quote``).
            StepDef(
                "declare_carrier_price",
                _("Declare the external carrier's price (TRP-03)"),
                lambda c: c.carrier_price_declared,
                anchor="acc-transport",
                applies=lambda c: c.carrier_quote_needed,
            ),
        ],
        "transitions": [TransitionDef("departure_scheduled")],
    },
    ("TRANSPORT", "departure_scheduled"): {
        # TRP-06: before loading, the pre-transport intake record
        # is made and signed - it follows the asset to
        # final delivery. Mirrors the STORAGE intake steps and is
        # enforced by the ``in_transit`` guard.
        "steps": [
            # §7.2: the intake record is a receipt for a particular
            # asset, and ``sign_intake_record`` refuses one that cannot
            # say which. Listed before the record itself, because it is
            # the question the record answers.
            StepDef(
                "identify_asset",
                _("Complete the asset's identification"),
                lambda c: c.asset_identified,
                anchor="acc-asset-identity",
            ),
            StepDef(
                "create_intake_record",
                _("Create the intake record with item inventory"),
                lambda c: bool(c.intake_records),
                anchor="acc-intake",
            ),
            StepDef(
                "upload_arrival_photos",
                _("Photograph the asset on arrival (wash it first if dirty)"),
                lambda c: c.has_intake_photos,
                anchor="acc-intake",
            ),
            StepDef(
                "sign_intake_record",
                _("Confirm asset receipt (sign the intake record)"),
                lambda c: c.signed_intake,
                anchor="acc-intake",
            ),
            # TRP-02: agreed before the asset moves, and ``_guard_in_transit``
            # refuses the departure without it.
            StepDef(
                "record_loading_plan",
                _("Agree the loading and unloading plan (TRP-02)"),
                lambda c: c.loading_plan_agreed,
                anchor="acc-transport",
            ),
            # TRP-07: the asset leaves once it is loaded and signed off
            # as ready for the road - ``_guard_in_transit`` refuses the
            # departure otherwise (QA T-H1). Listed after the intake record
            # steps because that is the order the service enforces.
            StepDef(
                "confirm_loading",
                _("Confirm loading and road-readiness (TRP-07)"),
                lambda c: c.loading_confirmation is not None
                and c.loading_confirmation.signed_at is not None
                and c.loading_confirmation.readiness == "READY",
                anchor="acc-transport",
            ),
        ],
        "transitions": [TransitionDef("in_transit", hint=_("Asset has departed"))],
    },
    ("TRANSPORT", "in_transit"): {
        # TRP-08: the delivery protocol (attendee, accompanying employee,
        # staff sign-off) gates the ``delivered`` transition; the signed
        # scan stays advisory (re-uploads allowed at any time).
        "steps": [
            StepDef(
                "confirm_delivery",
                _("Record the delivery protocol (attendee, sign-off)"),
                lambda c: c.delivery_confirmation is not None,
                anchor="acc-transport",
            ),
            StepDef(
                "delivery_protocol_scan",
                _("Upload the signed delivery protocol scan"),
                lambda c: c.has_delivery_protocol,
                anchor="acc-transport",
                blocking=False,
            ),
        ],
        "transitions": [TransitionDef("delivered")],
    },
    ("TRANSPORT", "delivered"): {
        "steps": list(CLOSING_STEPS),
        "transitions": [TransitionDef("closed")],
    },
    ("TRANSPORT", "closed"): {"steps": [], "transitions": []},
    # ---- STORAGE --------------------------------------------------
    ("STORAGE", "inquiry"): {
        # WNT-01/WNT-03: the storage is contracted before the priced
        # estimate goes out, so the inquiry must be documented and
        # price-ready first - all three steps are blocking and mirror the
        # contracted guard (docs, pricing basis, linked asset).
        "steps": [
            STORAGE_DOCS_STEP,
            STORAGE_DATABASE_ENTRY_STEP,
            STORAGE_PERMIT_STEP,
            STORAGE_PRICING_BASIS_STEP,
            STORAGE_LINK_ASSET_STEP,
        ],
        "transitions": [TransitionDef("contracted")],
    },
    ("STORAGE", "contracted"): {
        # WNT-03: draft the dynamically computed value estimate, adjust it
        # and send it; sending gates the move to estimate_sent.
        "steps": list(STORAGE_ESTIMATE_STEPS),
        "transitions": [TransitionDef("estimate_sent")],
    },
    ("STORAGE", "estimate_sent"): {
        "steps": [
            StepDef(
                "client_approval",
                _("Record the client's acceptance of the estimate"),
                lambda c: c.client_approved(),
                anchor="acc-quotes",
                blocking=False,
            ),
            StepDef(
                "book_storage_in",
                _("Book the storage intake appointment"),
                lambda c: c.has_storage_in_appointment,
                anchor="acc-appointments",
                blocking=False,
            ),
            # §7.2: the intake record is a receipt for a particular
            # asset, and ``sign_intake_record`` refuses one that cannot
            # say which. Listed before the record itself, because it is
            # the question the record answers.
            StepDef(
                "identify_asset",
                _("Complete the asset's identification"),
                lambda c: c.asset_identified,
                anchor="acc-asset-identity",
            ),
            StepDef(
                "create_intake_record",
                _("Create the intake record with item inventory"),
                lambda c: bool(c.intake_records),
                anchor="acc-intake",
            ),
            StepDef(
                "upload_arrival_photos",
                _("Photograph the asset on arrival (wash it first if dirty)"),
                lambda c: c.has_intake_photos,
                anchor="acc-intake",
            ),
            StepDef(
                "sign_intake_record",
                _("Confirm asset receipt (sign the intake record)"),
                lambda c: c.signed_intake,
                anchor="acc-intake",
            ),
        ],
        "transitions": [TransitionDef("asset_received")],
    },
    ("STORAGE", "asset_received"): {
        # WNT-05/WNT-06: the asset is placed when it arrives, and
        # ``_guard_in_storage`` refuses the move without a placement.
        "steps": [STORAGE_POSITION_STEP],
        "transitions": [
            TransitionDef("in_storage", hint=_("Asset moved into storage"))
        ],
    },
    ("STORAGE", "in_storage"): {
        "steps": [
            replace(STORAGE_POSITION_STEP, blocking=False),
            StepDef(
                "book_storage_out",
                _("Book the pickup appointment"),
                lambda c: c.has_storage_out_appointment,
                anchor="acc-appointments",
                blocking=False,
            ),
        ],
        "transitions": [TransitionDef("ready_for_pickup")],
    },
    ("STORAGE", "ready_for_pickup"): {
        "steps": list(CLOSING_STEPS),
        "transitions": [TransitionDef("closed")],
    },
    ("STORAGE", "closed"): {"steps": [], "transitions": []},
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def stepper_data(case) -> list:
    """Status chain of the case's type with done/current/upcoming states."""
    statuses = Status.objects.filter(case_type=case.case_type).order_by(
        "sort_order", "pk"
    )
    items = []
    for status in statuses:
        if status.pk == case.status_id:
            state = "current"
        elif status.sort_order < case.status.sort_order:
            state = "done"
        else:
            state = "upcoming"
        items.append({"status": status, "state": state})
    return items


def _build_step(step_def: StepDef, ctx: GuideContext, user) -> GuideStep:
    done = bool(step_def.predicate(ctx))
    role_ok = (
        step_def.roles is None
        or user.is_superuser
        or getattr(user, "role", "") in step_def.roles
    )
    admin_url = None
    if step_def.admin is not None:
        admin_url = reverse("admin:%s_%s_add" % step_def.admin)
    url = None
    if step_def.url_name is not None:
        url = reverse(step_def.url_name, args=[ctx.case.pk])
    return GuideStep(
        key=step_def.key,
        label=step_def.label,
        done=done,
        blocking=step_def.blocking,
        actionable=(not done) and role_ok,
        waiting_role=None if (done or role_ok) else _role_names(step_def.roles),
        anchor=step_def.anchor,
        admin_url=admin_url,
        url=url,
    )


def build_guide(case, user, ctx: GuideContext) -> dict:
    """Compute the wizard state for the case as seen by ``user``."""
    entry = REGISTRY.get((case.case_type, case.status.code))
    known = entry is not None

    steps = []
    if known:
        steps = [
            _build_step(sd, ctx, user)
            for sd in entry["steps"]
            if sd.applies is None or sd.applies(ctx)
        ]

    # GEN-03: raw portal data without a linked client - triage comes first.
    if case.client_id is None and any(
        (case.raw_contact_name, case.raw_phone, case.raw_email, case.raw_asset_info)
    ):
        steps.insert(
            0,
            GuideStep(
                key="triage",
                label=_("Link or create the client"),
                done=False,
                blocking=False,
                actionable=True,
                waiting_role=None,
                anchor=None,
                admin_url=None,
                url=reverse("backoffice:case-triage", args=[case.pk]),
            ),
        )

    db_edges = {
        t.to_status.code: t
        for t in StatusTransition.objects.filter(
            from_status=case.status
        ).select_related("to_status")
    }
    steps_by_key = {s.key: s for s in steps}
    transitions = []
    covered = set()
    defs = entry["transitions"] if known else []
    for tdef in defs:
        row = db_edges.get(tdef.code)
        if row is None:
            continue  # the data-driven workflow is authoritative
        covered.add(tdef.code)
        role_allowed = _role_allows(row, user)
        if tdef.gate_keys is None:
            gate = [s for s in steps if s.blocking]
        else:
            gate = [steps_by_key[k] for k in tdef.gate_keys if k in steps_by_key]
        transitions.append(
            GuideTransition(
                code=tdef.code,
                label=row.to_status.name,
                primary=tdef.primary,
                hint=tdef.hint,
                role_allowed=role_allowed,
                enabled=role_allowed and all(s.done for s in gate),
                waiting_role=None
                if role_allowed
                else _role_names(row.allowed_roles or []),
            )
        )
    # DB edges outside the registry stay reachable (custom workflows).
    for code, row in db_edges.items():
        if code in covered:
            continue
        role_allowed = _role_allows(row, user)
        transitions.append(
            GuideTransition(
                code=code,
                label=row.to_status.name,
                primary=False,
                hint="",
                role_allowed=role_allowed,
                enabled=role_allowed,
                waiting_role=None
                if role_allowed
                else _role_names(row.allowed_roles or []),
            )
        )

    # Hero selection: what should this user look at first?
    # Priority: terminal > triage > a blocking step you can do > "ready,
    # waiting for <role>" > a blocking step someone else must do >
    # an advisory step you can do > the enabled transition.
    hero = None
    hero_kind = "fallback" if not known else None
    undone = [s for s in steps if not s.done]
    undone_blocking = [s for s in undone if s.blocking]
    primary = next((t for t in transitions if t.primary), None)
    primary_gate_done = primary is not None and not undone_blocking
    if primary is not None:
        tdef = next((d for d in defs if d.code == primary.code), None)
        if tdef is not None and tdef.gate_keys is not None:
            primary_gate_done = all(
                steps_by_key[k].done
                for k in tdef.gate_keys
                if k in steps_by_key
            )
    if case.status.is_terminal:
        hero_kind = "done"
    elif steps and steps[0].key == "triage":
        hero, hero_kind = steps[0], "triage"
    elif known:
        blocking_actionable = next(
            (s for s in undone_blocking if s.actionable), None
        )
        advisory_actionable = next(
            (s for s in undone if not s.blocking and s.actionable), None
        )
        if blocking_actionable is not None:
            hero, hero_kind = blocking_actionable, "step"
        elif primary is not None and primary_gate_done and not primary.role_allowed:
            hero, hero_kind = primary, "waiting"
        elif undone_blocking:
            hero, hero_kind = undone_blocking[0], "waiting"
        elif advisory_actionable is not None:
            hero, hero_kind = advisory_actionable, "step"
        elif primary is not None:
            hero, hero_kind = primary, "transition"

    # Focus mode: sections the current status does not need are hidden
    # behind a "show all" toggle. Only meaningful for registry statuses -
    # custom workflows keep every section visible.
    relevant_anchors = {s.anchor for s in steps if s.anchor and not s.done}
    if hero is not None and getattr(hero, "anchor", None):
        relevant_anchors.add(hero.anchor)
    # A transport order carries persistent, editable records (offer, loading,
    # carrier price, delivery protocol) staff may revisit after its steps are
    # done. Keep the transport section reachable in focus mode on every
    # transport case - with an order for its editable records, without one
    # for the empty-state card linking to the TRP-01 inquiry form.
    if ctx.transport_order is not None or case.case_type == "TRANSPORT":
        relevant_anchors.add("acc-transport")
    # WNT rule 4: once the storage is contracted, the site-pickup card
    # (lift-out organizer, delivery) is in play for the whole stay. No guide
    # step owns its anchor, so without this it would sit permanently hidden
    # behind "show all sections" in focus mode - unreachable like the
    # transport section was.
    if ctx.storage_agreement is not None:
        relevant_anchors.add("acc-site-pickup")

    # The work-zone phase group hosting the next actionable thing.
    current_phase = None
    if hero is not None and getattr(hero, "anchor", None):
        current_phase = ANCHOR_PHASES.get(hero.anchor)
    if current_phase is None:
        current_phase = next(
            (
                ANCHOR_PHASES[s.anchor]
                for s in steps
                if not s.done and s.anchor in ANCHOR_PHASES
            ),
            None,
        )

    return {
        "stepper": stepper_data(case),
        "steps": steps,
        "todo_steps": [s for s in steps if not s.done],
        "done_steps": [s for s in steps if s.done],
        "transitions": transitions,
        "hero": hero,
        "hero_kind": hero_kind,
        "known": known,
        "focus": known,
        "relevant_anchors": relevant_anchors,
        "current_phase": current_phase,
    }
