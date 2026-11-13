"""Procedure §7 phase 4: *asset data* — the six asset data.

§7's second bullet names six: name, type, manufacturer, model,
registration, length. Only the length was ever enforced — it is the one
NOT NULL column — and the two fields declared without ``blank=True``
are enforced by forms rather than by the database, so the triage path
that writes ``asset_type=""`` straight through ``objects.create()``
got away with it.

Worse than unenforced: three of the six were **unfillable**. The
backoffice could view an asset and create a partial one at triage, and
nothing anywhere could complete it — the Django admin was the only way.

Phases 1 and 3 live in ``apps/cases/tests_section7.py``, phase 2 in
``apps/intake/tests_section7.py``.
"""
import tempfile

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.attachments.models import Attachment
from apps.attachments.services import attach_file
from apps.cases.models import Case, CaseType, ReportChannel
from apps.cases.tests import build_service_workflow
from apps.intake.models import IntakeRecord
from apps.intake.services import sign_intake_record

from .models import Client
from .services import record_asset_identification
from .tests import identified_asset


class AssetIdentificationTests(TestCase):
    """The predicate every consumer reads."""

    def setUp(self):
        self.client_record = Client.objects.create(name="Owner")

    def _asset(self, **overrides):
        return identified_asset(client=self.client_record, **overrides)

    def test_all_six_answered_is_identified(self):
        self.assertTrue(self._asset().is_fully_identified)

    def test_each_missing_datum_is_named(self):
        for field in ("name", "asset_type", "manufacturer", "model"):
            with self.subTest(field=field):
                asset = self._asset(**{field: ""})
                self.assertFalse(asset.is_fully_identified)
                self.assertEqual(len(asset.missing_identification()), 1)

    def test_whitespace_is_not_an_answer(self):
        self.assertFalse(self._asset(manufacturer="   ").is_fully_identified)

    def test_a_blank_registration_is_missing(self):
        asset = self._asset(registration_no="")
        self.assertFalse(asset.registration_recorded)
        self.assertFalse(asset.is_fully_identified)

    def test_not_registered_is_an_answer(self):
        # A trailer, a forklift, a machine still waiting for its papers -
        # "no registration" is a fact about the asset, not a gap in the
        # record, and the two must not look the same.
        asset = self._asset(registration_no="", is_unregistered=True)
        self.assertTrue(asset.registration_recorded)
        self.assertTrue(asset.is_fully_identified)


class RecordAssetIdentificationTests(TestCase):
    """The service that writes it — the half that did not exist."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="s7p4", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.asset = identified_asset(manufacturer="", model="")
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["intake_scheduled"],
            client=self.asset.client,
            asset=self.asset,
        )

    def test_completing_the_data_logs_on_the_case(self):
        record_asset_identification(
            self.asset,
            self.user,
            case=self.case,
            manufacturer="Bavaria",
            model="S33",
        )
        self.asset.refresh_from_db()
        self.assertTrue(self.asset.is_fully_identified)
        from apps.cases.models import ActivityLog

        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("manufacturer", entry.text)
        self.assertIn("model", entry.text)

    def test_a_partial_fill_says_what_is_still_missing(self):
        record_asset_identification(
            self.asset, self.user, case=self.case, manufacturer="Bavaria"
        )
        from apps.cases.models import ActivityLog

        entry = ActivityLog.objects.get(case=self.case)
        self.assertIn("still missing", entry.text)
        self.assertIn("model", entry.text)

    def test_writing_nothing_new_is_silent(self):
        record_asset_identification(
            self.asset, self.user, case=self.case, name=self.asset.name
        )
        from apps.cases.models import ActivityLog

        self.assertFalse(ActivityLog.objects.filter(case=self.case).exists())

    def test_a_registration_and_the_not_registered_tick_contradict(self):
        with self.assertRaises(ValidationError) as ctx:
            record_asset_identification(
                self.asset,
                self.user,
                case=self.case,
                registration_no="TV-1",
                is_unregistered=True,
            )
        self.assertIn("contradict", str(ctx.exception))

    def test_it_refuses_to_write_anything_but_the_six(self):
        with self.assertRaises(ValueError):
            record_asset_identification(
                self.asset, self.user, case=self.case, client_id=1
            )

    def test_it_works_without_a_case(self):
        record_asset_identification(
            self.asset, self.user, manufacturer="Bavaria", model="S33"
        )
        self.asset.refresh_from_db()
        self.assertTrue(self.asset.is_fully_identified)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class IntakeNamesItsAssetTests(TestCase):
    """§7.2 at the gate: a receipt has to say which asset it is for."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="s7p4b", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["intake_scheduled"],
        )
        self.record = IntakeRecord.objects.create(
            case=self.case,
            mount_status="OFF_MOUNT",
            cleanliness_status="CLEAN",
            storage_location="Hall 1",
            damage_note="none",
            no_items_received=True,
            energy_level="HALF",  # §7.6
        )
        attach_file(
            self.case,
            SimpleUploadedFile("arrival.jpg", b"fake-jpeg"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.INTAKE,
            user=self.user,
        )

    def _link(self, **overrides):
        asset = identified_asset(**overrides)
        self.case.client = asset.client
        self.case.asset = asset
        self.case.save(update_fields=["client", "asset"])
        return asset

    def test_signing_refused_with_no_asset_at_all(self):
        # Triage's job (GEN-03), and the case page has carried a triage
        # banner for it from the start - what is new is that the banner
        # can no longer be ignored all the way to a signed document.
        with self.assertRaises(ValidationError) as ctx:
            sign_intake_record(self.record, self.user)
        self.assertIn("no asset record yet", str(ctx.exception))
        self.record.refresh_from_db()
        self.assertIsNone(self.record.signed_at)

    def test_signing_refused_while_the_asset_is_half_identified(self):
        self._link(manufacturer="", model="")
        with self.assertRaises(ValidationError) as ctx:
            sign_intake_record(self.record, self.user)
        message = str(ctx.exception)
        self.assertIn("not fully identified", message)
        self.assertIn("manufacturer", message)
        self.assertIn("model", message)

    def test_the_missing_registration_is_named_too(self):
        self._link(registration_no="")
        with self.assertRaises(ValidationError) as ctx:
            sign_intake_record(self.record, self.user)
        self.assertIn("registration", str(ctx.exception))

    def test_an_unregistered_asset_can_still_be_signed_for(self):
        self._link(registration_no="", is_unregistered=True)
        sign_intake_record(self.record, self.user)
        self.record.refresh_from_db()
        self.assertIsNotNone(self.record.signed_at)

    def test_a_fully_identified_asset_signs(self):
        self._link()
        sign_intake_record(self.record, self.user)
        self.record.refresh_from_db()
        self.assertIsNotNone(self.record.signed_at)


class AssetIdentityScreenTests(TestCase):
    """The half that did not exist: an editable asset in the backoffice."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="s7p4c", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.asset = identified_asset(manufacturer="", model="")
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["intake_scheduled"],
            client=self.asset.client,
            asset=self.asset,
        )
        self.client.force_login(self.user)

    def _url(self):
        return reverse("backoffice:case-asset-identity", args=[self.case.pk])

    def _payload(self, **overrides):
        data = {
            "name": self.asset.name,
            "asset_type": self.asset.asset_type,
            "manufacturer": "Bavaria",
            "model": "S33",
            "registration_no": self.asset.registration_no,
            "length_m": "9.50",
        }
        data.update(overrides)
        return data

    def test_the_card_completes_the_asset(self):
        response = self.client.post(self._url(), self._payload(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertTrue(self.asset.is_fully_identified)

    def test_the_not_registered_tick_is_accepted(self):
        response = self.client.post(
            self._url(),
            self._payload(registration_no="", is_unregistered="on"),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertTrue(self.asset.registration_recorded)

    def test_a_registration_and_the_tick_together_come_back_as_an_error(self):
        response = self.client.post(
            self._url(),
            self._payload(is_unregistered="on"),
            follow=True,
        )
        self.assertContains(response, "invalid-feedback")
        self.asset.refresh_from_db()
        self.assertFalse(self.asset.is_unregistered)

    def test_neither_a_registration_nor_the_tick_is_an_error(self):
        response = self.client.post(
            self._url(), self._payload(registration_no=""), follow=True
        )
        self.assertContains(response, "invalid-feedback")

    def test_the_card_renders_with_what_is_still_missing(self):
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, "asset-identity/")
        self.assertContains(response, "Still missing")

    def test_the_card_is_absent_on_a_case_with_no_asset(self):
        self.case.asset = None
        self.case.save(update_fields=["asset"])
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertNotContains(response, "asset-identity/")


class AssetIdentityGuideTests(TestCase):
    """The wizard mirrors the guard rather than contradicting it."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="s7p4d", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.asset = identified_asset(manufacturer="", model="")
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["intake_scheduled"],
            client=self.asset.client,
            asset=self.asset,
        )

    def _step(self):
        from apps.backoffice.guide import build_guide
        from apps.backoffice.tests_guide import ctx_for

        self.case.refresh_from_db()
        guide = build_guide(self.case, self.user, ctx_for(self.case))
        for step in guide["steps"]:
            if step.key == "identify_asset":
                return step
        return None

    def test_the_step_is_listed_and_blocking_until_complete(self):
        step = self._step()
        self.assertIsNotNone(step)
        self.assertFalse(step.done)
        self.assertTrue(step.blocking)

    def test_the_step_ticks_once_complete(self):
        record_asset_identification(
            self.asset, self.user, manufacturer="Bavaria", model="S33"
        )
        self.assertTrue(self._step().done)

    def test_the_step_stays_on_a_case_worked_where_the_asset_lies(self):
        # This test used to assert the opposite, and the opposite was
        # the bug: §7.2's "asset data" are obligatory data of the
        # TICKET, not of the intake record. Because the gate was written
        # inside ``sign_intake_record``, an SVC-03 case - which never
        # signs one - went through the whole intake phase with the six
        # unanswered, and the wizard obligingly agreed by dropping the
        # step. The gate for such a case is now the documentation check
        # in ``_guard_asset_received``, so the step stays.
        from apps.cases.models import WorkLocation
        from apps.cases.services import record_work_location_consult

        record_work_location_consult(self.case, WorkLocation.ON_SITE, self.user)
        step = self._step()
        self.assertIsNotNone(step)
        self.assertTrue(step.blocking)
