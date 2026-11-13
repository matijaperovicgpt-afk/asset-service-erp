"""
Invariant regression suite: one test per revision-checklist rule
(INV-01..INV-12).

Each test names its rule ID in the docstring so a failing test points
straight at the violated business rule. Workflow fixtures reuse
``build_workflow`` / ``WorkflowTestBase`` from ``apps.cases.tests``;
INV-08 and INV-09 are verified at the view level (protected media URLs,
client status e-mail, no public page).
"""
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.apps import apps as django_apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from .exceptions import AppendOnlyViolation, GuardCheckFailed
from .models import (
    ActivityLog,
    CaseType,
    Status,
    StatusHistory,
    StatusTransition,
)
from .services import available_transitions
from .tests import WorkflowTestBase, build_service_workflow, build_workflow

TRANSPORT_CODES = [
    "inquiry",
    "quote_sent",
    "advance_paid",
    "departure_scheduled",
    "in_transit",
    "delivered",
    "closed",
]
WINTER_CODES = [
    "inquiry",
    "contracted",
    "estimate_sent",
    "asset_received",
    "in_storage",
    "ready_for_pickup",
    "closed",
]




def _service_type(name="Storage"):
    """WNT-02 (§5.2): the contracted guard wants at least one selected
    service, so a fixture that expects to reach ``contracted`` needs one
    in the catalogue."""
    from django.apps import apps as _apps

    StorageServiceType = _apps.get_model("storage", "StorageServiceType")
    service_type, _created = StorageServiceType.objects.get_or_create(name=name)
    return service_type

class InvariantRegressionTests(WorkflowTestBase):
    """Walks INV-01..INV-12, one test each."""

    # ------------------------------------------------------------------
    def test_inv_01_only_transition_case_sets_case_status(self):
        """INV-01: no code path sets ``Case.status`` except
        ``transition_case()`` (STM-02).

        Static scan: outside of test modules and migrations, the only
        assignment to a case's ``status`` in the whole ``apps/`` tree
        must be the one inside ``apps/cases/services.py``.
        """
        pattern = re.compile(r"\bcase\.status(?:_id)?\s*=[^=]")
        allowed = Path(settings.BASE_DIR) / "apps" / "cases" / "services.py"
        offenders = []
        for path in (Path(settings.BASE_DIR) / "apps").rglob("*.py"):
            if "migrations" in path.parts or path.name.startswith("tests"):
                continue
            if path == allowed:
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if pattern.search(line):
                    offenders.append("%s:%s: %s" % (path, lineno, line.strip()))
        self.assertEqual(
            offenders,
            [],
            "Case.status assigned outside transition_case() (INV-01):\n%s"
            % "\n".join(offenders),
        )

    # ------------------------------------------------------------------
    def test_inv_02_every_status_change_writes_exactly_one_history_row(self):
        """INV-02: every status change has exactly one ``StatusHistory``
        row (STM-03); a refused transition writes none."""
        statuses = build_service_workflow()
        case = self._make_case(statuses, "new_report")
        self._add_intake_appointment(case)

        self._transition(case, "intake_scheduled")
        self.assertEqual(case.status_history.count(), 1)
        row = case.status_history.get()
        self.assertEqual(row.from_status, statuses["new_report"])
        self.assertEqual(row.to_status, statuses["intake_scheduled"])

        # A failed guard (no signed work order / intake record) must not
        # write history nor move the case (STM-04: never partially applies).
        with self.assertRaises(GuardCheckFailed):
            self._transition(case, "asset_received")
        case.refresh_from_db()
        self.assertEqual(case.status, statuses["intake_scheduled"])
        self.assertEqual(case.status_history.count(), 1)

    # ------------------------------------------------------------------
    def test_inv_03_audit_models_are_append_only(self):
        """INV-03: ``StatusHistory``, ``ActivityLog`` (and
        ``IntakeAmendment``) refuse update and delete."""
        statuses = build_service_workflow()
        case = self._make_case(statuses, "new_report")

        history = StatusHistory.objects.create(
            case=case, from_status=None, to_status=statuses["new_report"]
        )
        log = ActivityLog.objects.create(case=case, text="Called the client.")
        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        IntakeAmendment = django_apps.get_model("intake", "IntakeAmendment")
        record = IntakeRecord.objects.create(case=case, received_by=self.user)
        amendment = IntakeAmendment.objects.create(
            record=record, text="Scratch found on outline.", author=self.user
        )

        for entry in (history, log, amendment):
            with self.assertRaises(AppendOnlyViolation):
                entry.save()
            with self.assertRaises(AppendOnlyViolation):
                entry.delete()

    # ------------------------------------------------------------------
    def test_inv_04_accepted_quote_has_required_approvals(self):
        """INV-04: an accepted quote has the ``Approval`` rows its amount
        requires per ``ApprovalLimit`` (ACT-02, SVC-12).

        With no ``ApprovalLimit`` rows the ladder escalates
        conservatively to DIRECTOR: client consent alone must not accept
        the quote.
        """
        from apps.quotes.models import Approval, Quote
        from apps.quotes.services import (
            missing_approval_levels,
            record_approval,
        )

        statuses = build_service_workflow()
        case = self._make_case(statuses, "awaiting_approval")
        quote = Quote.objects.create(
            case=case,
            description="Engine overhaul",
            amount=Decimal("5000"),
            status=Quote.Status.SENT,
        )

        record_approval(quote, Approval.Level.CLIENT, Approval.Decision.APPROVED)
        quote.refresh_from_db()
        self.assertNotEqual(quote.status, Quote.Status.ACCEPTED)
        self.assertEqual(missing_approval_levels(quote), [Approval.Level.DIRECTOR])

        director = self._make_staff("director-inv04", role="DIRECTOR")
        record_approval(
            quote, Approval.Level.DIRECTOR, Approval.Decision.APPROVED, user=director
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.ACCEPTED)
        self.assertEqual(missing_approval_levels(quote), [])

    # ------------------------------------------------------------------
    def test_inv_05_non_stock_part_order_requires_50_percent_advance(self):
        """INV-05: a ``PartOrder`` for a non-stock part has a linked 50%
        ``AdvancePayment`` (SVC-11)."""
        from apps.parts.models import Part
        from apps.parts.services import order_part
        from apps.quotes.models import AdvancePayment

        statuses = build_service_workflow()
        case = self._make_case(statuses, "diagnostics")
        rare_part = Part.objects.create(
            code="RARE-01", name="Rare gearbox", is_consumable=False, stock_qty=0
        )

        with self.assertRaisesMessage(ValidationError, "advance"):
            order_part(case, rare_part)

        small_advance = AdvancePayment.objects.create(
            case=case, percent=30, amount=Decimal("300")
        )
        with self.assertRaisesMessage(ValidationError, "50"):
            order_part(case, rare_part, advance_payment=small_advance)

        advance = AdvancePayment.objects.create(
            case=case, percent=50, amount=Decimal("500")
        )
        order = order_part(case, rare_part, advance_payment=advance)
        self.assertEqual(order.advance_payment, advance)

    # ------------------------------------------------------------------
    def test_inv_06_transport_departure_requires_paid_advance(self):
        """INV-06: a transport case in ``departure_scheduled`` or later
        has a paid advance (TRP-04)."""
        from apps.quotes.models import AdvancePayment

        statuses = build_workflow(CaseType.TRANSPORT, TRANSPORT_CODES)
        case = self._make_case(
            statuses, "advance_paid", case_type=CaseType.TRANSPORT
        )

        with self.assertRaisesMessage(GuardCheckFailed, "advance"):
            self._transition(case, "departure_scheduled")

        AdvancePayment.objects.create(
            case=case, percent=50, amount=Decimal("300"), paid_at=timezone.now()
        )
        # QA T-M2: the departure date is agreed before it is scheduled.
        django_apps.get_model("transport", "TransportOrder").objects.create(
            case=case, origin="North depot", destination="South depot",
            preferred_date=timezone.localdate() + timedelta(days=30),
        )
        case = self._transition(case, "departure_scheduled")
        self.assertEqual(case.status.code, "departure_scheduled")

    # ------------------------------------------------------------------
    def test_inv_07_closed_needs_signed_handover_and_settlement(self):
        """INV-07: a closed case has a signed handover record and
        ``financially_settled=True`` (SVC-19)."""
        HandoverRecord = django_apps.get_model("intake", "HandoverRecord")

        statuses = build_service_workflow()
        case = self._make_case(statuses, "ready_for_handover")

        with self.assertRaisesMessage(GuardCheckFailed, "handover"):
            self._transition(case, "closed")

        HandoverRecord.objects.create(
            case=case, received_by_name="Test Contact", signed_at=timezone.now()
        )
        with self.assertRaisesMessage(GuardCheckFailed, "financial"):
            self._transition(case, "closed")

        case.financially_settled = True
        case.save()
        case = self._transition(case, "closed")
        self.assertEqual(case.status.code, "closed")

    # ------------------------------------------------------------------
    def test_inv_08_attachments_bound_to_case_and_served_behind_login(self):
        """INV-08: every attachment is bound to a case and carries a
        stage; view level, its media URL requires a login (POR-04)."""
        from django.contrib.contenttypes.models import ContentType

        from apps.attachments.models import Attachment

        statuses = build_service_workflow()
        case = self._make_case(statuses, "new_report")
        attachment = Attachment.objects.create(
            content_type=ContentType.objects.get_for_model(case),
            object_id=case.pk,
            kind=Attachment.Kind.PHOTO,
            stage=Attachment.Stage.REPORT,
            file="cases/%s/report/deadbeef.jpg" % case.pk,
        )
        self.assertEqual(attachment.content_object, case)
        self.assertIn(attachment.stage, Attachment.Stage.values)

        # Anonymous requests for the file are redirected to the login
        # page - media is never publicly served.
        response = self.client.get(attachment.file.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(settings.LOGIN_URL, response.url)

    # ------------------------------------------------------------------
    def _leaky_case_with_quotes(self):
        """A case seeded with one sentinel per leak class POR-03 forbids.

        Every "must not appear" string asserted by the INV-09 tests is
        really present in the database on the very case under test, so the
        negative assertions cannot pass vacuously.
        """
        ApprovalLimit = django_apps.get_model("accounts", "ApprovalLimit")
        ApprovalLimit.objects.get_or_create(
            role="TECHNICIAN", defaults={"max_amount": Decimal("5000")}
        )
        Quote = django_apps.get_model("quotes", "Quote")
        Approval = django_apps.get_model("quotes", "Approval")
        User = django_apps.get_model(settings.AUTH_USER_MODEL)

        from apps.quotes.services import create_quote, send_quote

        statuses = build_service_workflow()
        status = statuses["awaiting_approval"]
        status.name = "INTERNAL awaiting approval"
        status.client_visible_name = "Request received"
        status.save()

        technician = User.objects.create_user(
            username="zach",
            password="demo12345",
            first_name="Zach",
            last_name="InternalStaff",
        )
        case = self._make_case(
            statuses, "awaiting_approval", assigned_technician=technician
        )
        ActivityLog.objects.create(
            case=case,
            author=technician,
            kind=ActivityLog.Kind.SYSTEM,
            text="SENTINEL internal note do not leak",
        )
        # A draft revision and a settled one: neither is an open offer, so
        # neither may appear on the public page.
        Quote.objects.create(
            case=case,
            description="SENTINEL draft scope",
            amount=Decimal("777.77"),
            status=Quote.Status.DRAFT,
        )
        settled = Quote.objects.create(
            case=case,
            description="SENTINEL settled scope",
            amount=Decimal("888.88"),
            status=Quote.Status.REJECTED,
        )
        Approval.objects.create(
            quote=settled,
            level=Approval.Level.CLIENT,
            decision=Approval.Decision.REJECTED,
            decided_at=timezone.now(),
            note="SENTINEL internal approval note",
        )
        # The open offer, created last so it is the latest quote.
        quote = create_quote(
            case,
            "Replace impeller and service the raw-water pump",
            Decimal("1234.56"),
        )
        send_quote(quote, technician)
        quote.refresh_from_db()
        return case, quote

    def test_inv_09_client_status_mail_shows_client_visible_names_only(self):
        """INV-09: the client's only view of a case is the status e-mail,
        and it exposes the client-visible status name and nothing else
        (STM-05, POR-02).

        Every leak class the old tracking page was audited for is asserted
        absent from the mail: internal status name and code, staff names,
        internal notes, numeric IDs, backoffice URLs - and now every quote
        too, open or not: prices are staff's to communicate, never an
        automatic mail's.
        """
        from django.core import mail

        from apps.notifications.services import notify_case_transition

        case, quote = self._leaky_case_with_quotes()
        self.assertGreaterEqual(len(case.public_token), 32)
        status = case.status
        status.notify_client = True
        status.save(update_fields=["notify_client"])
        case.raw_email = "client@example.com"
        case.save(update_fields=["raw_email"])

        notify_case_transition(case, None, status)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].subject + "\n" + mail.outbox[0].body
        # Permitted: the client-visible status name.
        self.assertIn("Request received", body)
        # Forbidden.
        for forbidden in (
            "INTERNAL awaiting approval",
            "awaiting_approval",
            "Zach InternalStaff",
            "SENTINEL internal note",
            "SENTINEL internal approval note",
            "SENTINEL draft scope",
            "777.77",
            "SENTINEL settled scope",
            "888.88",
            "Replace impeller",
            "1234.56",
            "/backoffice/",
            "/track/",
            case.public_token,
            # POR-02: no numeric IDs. Assert the shapes the code really
            # emits - "Case #<pk>" is a string nothing in this project
            # produces, so it guarded nothing.
            "Case #",
            "Quote #",
            "INV-0",
            "ACT-0",
            "POR-0",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_inv_09_there_is_no_public_page_for_a_case(self):
        """The tracking page and the online quote decision are gone: the
        case's public token opens nothing, so nothing can leak through a
        forwarded link. 404, not 403 - the response says nothing about
        whether such a case exists."""
        case, quote = self._leaky_case_with_quotes()
        for url in (
            f"/track/{case.public_token}/",
            f"/track/{case.public_token}/quote/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(
                    self.client.post(url, {"decision": "accept"}).status_code,
                    404,
                )

    # ------------------------------------------------------------------
    def test_inv_10_schema_by_migration_workflow_by_data(self):
        """INV-10: schema changes happen only through migrations (no
        unmigrated model changes), and workflow changes are pure data --
        a new status + transition row becomes available with no code
        change (STM-01)."""
        # Raises SystemExit if any model change lacks a migration.
        call_command(
            "makemigrations", "--check", "--dry-run", verbosity=0
        )

        statuses = build_service_workflow()
        case = self._make_case(statuses, "new_report")
        on_hold = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="on_hold",
            name="On hold",
            client_visible_name="On hold",
            sort_order=99,
        )
        StatusTransition.objects.create(
            from_status=statuses["new_report"], to_status=on_hold
        )
        self.assertIn(on_hold, available_transitions(case, self.user))

    # ------------------------------------------------------------------
    def test_inv_11_storage_contract_requires_asset_length(self):
        """INV-11: ``Asset.length_m`` is present on every storage
        case (WNT-01) - ``contracted`` requires a linked asset, whose
        ``length_m`` is mandatory at the model level."""
        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")

        from apps.storage.services import upsert_agreement

        statuses = build_workflow(CaseType.STORAGE, WINTER_CODES)
        case = self._make_case(
            statuses, "inquiry", case_type=CaseType.STORAGE
        )

        with self.assertRaisesMessage(GuardCheckFailed, "asset"):
            self._transition(case, "contracted")

        client = Client.objects.create(
            name="Winter Client", phone="+44 67 000 000"
        )
        asset = Asset.objects.create(
            client=client, name="Bora", length_m=Decimal("9.50")
        )
        case.client = client
        case.asset = asset
        case.save()
        upsert_agreement(
            case,
            permit_no="SP-1",
            asset_length_m=Decimal("9.50"),
            origin_site="Customer site",
            ownership="John Smith",
            selected_services=[_service_type()],
            accommodation="OPEN",
            term="SEASONAL",
        )
        case.refresh_from_db()
        case = self._transition(case, "contracted")
        self.assertEqual(case.status.code, "contracted")
        self.assertIsNotNone(case.asset.length_m)

    # ------------------------------------------------------------------
    def test_inv_12_later_condition_is_appended_never_edited(self):
        """INV-12: later-discovered asset condition is appended to the
        intake record, never edited over existing content (SVC-07)."""
        from apps.intake.services import append_intake_amendment

        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")

        statuses = build_service_workflow()
        case = self._make_case(statuses, "asset_received")
        record = IntakeRecord.objects.create(
            case=case,
            received_by=self.user,
            general_condition_note="Clean outline on arrival.",
        )

        amendment = append_intake_amendment(
            record, "Gelcoat crack found under the waterline.", self.user
        )
        record.refresh_from_db()
        self.assertEqual(record.general_condition_note, "Clean outline on arrival.")
        self.assertEqual(record.amendments.get(), amendment)

        amendment.text = "tampered"
        with self.assertRaises(AppendOnlyViolation):
            amendment.save()

    # ------------------------------------------------------------------
    def _make_staff(self, username, role):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            username=username, password="demo12345", is_staff=True, role=role
        )
