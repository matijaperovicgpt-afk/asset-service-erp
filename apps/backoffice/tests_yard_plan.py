"""WNT-05: the yard plan as a planning board rather than a click target.

What these cover, in the order the page was rebuilt:

* a refused placement keeps the planner's aim instead of redirecting to
  an empty form (they used to re-pick the zone, re-set the orientation
  and retype the exit date before they could try again);
* the page answers "what is still waiting for a spot?", so the yard can
  be worked from the yard rather than one case at a time;
* an asset can be taken back off the plan;
* the browser is handed the facts the rules need, so the outline can
  refuse a spot at the moment of aiming;
* the plan reads without relying on colour.
"""

import datetime
import json
import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.cases.models import Case, CaseType, ReportChannel, Status
from apps.storage.models import (
    ObstacleKind,
    StorageAgreement,
    StorageObstacle,
    StoragePosition,
    StorageZone,
)
from apps.storage.services import place_asset

_YARD_DATA_RE = re.compile(
    r'<script id="yard-data" type="application/json">(.*?)</script>', re.S
)


class YardPlanTestBase(TestCase):
    def setUp(self):
        # QA S-H3: only a contracted case takes ground, and "contracted"
        # is judged by sort order, so the fixture needs it below.
        Status.objects.create(
            case_type=CaseType.STORAGE,
            code="contracted",
            name="Contracted",
            client_visible_name="Contracted",
            sort_order=1,
        )
        self.open_status = Status.objects.create(
            case_type=CaseType.STORAGE,
            code="in_storage",
            name="In storage",
            client_visible_name="In storage",
            sort_order=4,
        )
        self.closed_status = Status.objects.create(
            case_type=CaseType.STORAGE,
            code="closed",
            name="Closed",
            client_visible_name="Closed",
            is_terminal=True,
        )
        self.zone = StorageZone.objects.create(
            name="Otvoreno dvoriste",
            width_m=Decimal("40"),
            length_m=Decimal("25"),
            clearance_m=Decimal("0.75"),
        )
        self.case = self.make_case()
        self.agreement = self.case.storage_agreement
        self.user = get_user_model().objects.create_user(
            username="yardplan", password="demo12345", is_staff=True
        )
        self.client.force_login(self.user)

    def make_case(self, length="10", width="4", status=None):
        case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=status or self.open_status,
        )
        StorageAgreement.objects.create(
            case=case,
            asset_length_m=Decimal(length) if length is not None else None,
            asset_width_m=Decimal(width) if width is not None else None,
        )
        return case

    def layout(self, **params):
        return self.client.get(reverse("backoffice:storage-layout"), params)

    def post_place(self, case=None, **over):
        payload = {
            "zone": str(self.zone.pk),
            "pos_x_m": "0.00",
            "pos_y_m": "0.00",
            "orientation": "LENGTHWISE",
            "planned_exit_date": "2027-03-01",
        }
        payload.update(over)
        return self.client.post(
            reverse("backoffice:storage-place", args=[(case or self.case).pk]),
            payload,
        )

    def yard_data(self, response):
        match = _YARD_DATA_RE.search(response.content.decode())
        return json.loads(match.group(1)) if match else []


class RefusalKeepsTheAimTests(YardPlanTestBase):
    """Stage 1. The most expensive moment in the old flow was a refusal:
    it redirected to ``?case=N``, whose GET rebuilt an empty form."""

    def setUp(self):
        super().setUp()
        self.other = self.make_case()
        place_asset(
            self.other.storage_agreement,
            self.zone,
            Decimal("0"),
            Decimal("0"),
            "LENGTHWISE",
            datetime.date(2027, 3, 1),
        )

    def test_a_refusal_renders_in_place_rather_than_redirecting(self):
        response = self.post_place(pos_x_m="2.00", pos_y_m="2.00")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "overlaps")

    def test_the_submitted_values_come_back_in_the_form(self):
        response = self.post_place(
            pos_x_m="2.00",
            pos_y_m="2.00",
            orientation="CROSSWISE",
            planned_exit_date="2027-04-15",
        )
        html = response.content.decode()
        self.assertIn('value="2.00"', html)
        self.assertIn('value="2027-04-15"', html)
        self.assertIn('<option value="CROSSWISE" selected>', html)
        self.assertIn(
            '<option value="%s" selected>' % self.zone.pk, html
        )

    def test_nothing_is_written_by_a_refused_placement(self):
        self.post_place(pos_x_m="2.00", pos_y_m="2.00")
        self.assertEqual(
            StoragePosition.objects.filter(agreement=self.agreement).count(), 0
        )

    def test_an_invalid_field_also_keeps_the_aim(self):
        """A field-level error and a rule-level refusal cost the same:
        one correction, not a retyped form."""
        response = self.post_place(pos_x_m="", pos_y_m="7.00")
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="7.00"', response.content.decode())

    def test_a_clearance_refusal_names_the_gap(self):
        response = self.post_place(pos_x_m="4.00", pos_y_m="0.00")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0.75")

    def test_success_still_redirects(self):
        """Only success redirects, so a reload cannot re-post it."""
        response = self.post_place(pos_x_m="20.00", pos_y_m="0.00")
        self.assertEqual(response.status_code, 302)
        self.assertIn("?placed=%s" % self.case.pk, response["Location"])

    def test_a_malformed_case_parameter_does_not_500(self):
        """``?case=abc`` reached ``get_object_or_404(pk="abc")``, which
        raises ValueError, not Http404 - a crash anyone could produce from
        the address bar. A non-numeric case is not a case."""
        response = self.layout(case="abc")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["place_case"])

    def test_an_unknown_case_still_404s(self):
        """Not-a-number and not-found are different answers."""
        self.assertEqual(self.layout(case="999999").status_code, 404)

    def test_the_place_endpoint_takes_no_get(self):
        response = self.client.get(
            reverse("backoffice:storage-place", args=[self.case.pk])
        )
        self.assertEqual(response.status_code, 405)


class AfterPlacingTests(YardPlanTestBase):
    """Reported from real use: "when I placed one asset, another one was
    initiated - another asset outline that is unidentified appeared ready
    for placing."

    A successful placement redirected back to ``?case=N``, which is
    placing mode, so the placer stayed armed for the same asset and the
    next sweep of the pointer drew an outline. Unlabelled, that reads as a
    second asset rather than as the one just placed.
    """

    def test_a_successful_placement_leaves_placing_mode(self):
        response = self.post_place(pos_x_m="20.00", pos_y_m="0.00")
        page = self.client.get(response["Location"])
        self.assertIsNone(page.context["place_case"])
        self.assertNotContains(page, 'id="place-form"')

    def test_the_asset_just_placed_is_still_highlighted(self):
        """Leaving placing mode must not mean losing sight of the asset."""
        self.post_place(pos_x_m="20.00", pos_y_m="0.00")
        page = self.layout(placed=self.case.pk)
        assets = page.context["zone_views"][0]["assets"]
        current = [b for b in assets if b["is_current"]]
        self.assertEqual([b["case"].pk for b in current], [self.case.pk])

    def test_a_malformed_placed_parameter_is_ignored(self):
        page = self.layout(placed="abc")
        self.assertEqual(page.status_code, 200)
        self.assertIsNone(page.context["place_case"])

    def test_highlighting_never_arms_the_placer(self):
        self.post_place(pos_x_m="20.00", pos_y_m="0.00")
        page = self.layout(placed=self.case.pk)
        self.assertNotContains(page, 'id="place-form"')
        self.assertNotContains(page, "yard-labels")

    def test_lifting_out_also_leaves_placing_mode(self):
        self.post_place(pos_x_m="20.00", pos_y_m="0.00")
        response = self.client.post(
            reverse("backoffice:storage-unplace", args=[self.case.pk])
        )
        self.assertNotIn("case=", response["Location"])

    def test_a_placed_asset_can_be_picked_up_again_from_the_plan(self):
        """Leaving placing mode is only safe if moving stays reachable."""
        self.post_place(pos_x_m="20.00", pos_y_m="0.00")
        page = self.layout()
        self.assertContains(
            page,
            "%s?case=%s"
            % (reverse("backoffice:storage-layout"), self.case.pk),
        )

    def test_the_form_carries_the_placer_config(self):
        """It used to ride on the script tag and be read with
        document.currentScript - which is null when htmx inserts the
        script, because dynamically added scripts are async."""
        page = self.layout(case=self.case.pk)
        html = page.content.decode()
        self.assertIn('data-case="%s"' % self.case.pk, html)
        self.assertIn('data-length="10.00"', html)
        self.assertIn('data-width="4.00"', html)

    def test_only_one_script_is_loaded(self):
        """Two external scripts with an ordering dependency raced inside
        an htmx swap: the binding could run before the engine existed and
        silently do nothing."""
        html = self.layout(case=self.case.pk).content.decode()
        self.assertEqual(html.count("js/yard"), 1)
        self.assertNotIn("yard-place.js", html)


class WaitingListTests(YardPlanTestBase):
    """Stage 6. The planner's actual October question was the one the
    page could not take: which assets still have nowhere to go?"""

    def test_an_unplaced_asset_is_listed(self):
        response = self.layout()
        self.assertContains(response, "Waiting for a spot")
        self.assertContains(response, "#%s" % self.case.pk)

    def test_a_placed_asset_leaves_the_list(self):
        place_asset(
            self.agreement,
            self.zone,
            Decimal("0"),
            Decimal("0"),
            "LENGTHWISE",
            None,
        )
        response = self.layout()
        self.assertEqual(
            [w["case"].pk for w in response.context["waiting"]], []
        )

    def test_a_closed_case_is_not_waiting_for_anything(self):
        closed = self.make_case(status=self.closed_status)
        response = self.layout()
        waiting = [w["case"].pk for w in response.context["waiting"]]
        self.assertIn(self.case.pk, waiting)
        self.assertNotIn(closed.pk, waiting)

    def test_a_asset_with_no_length_is_listed_but_flagged(self):
        """These are exactly the assets that cannot be placed; hiding them
        is how they get forgotten until October."""
        unmeasured = self.make_case(length=None, width=None)
        response = self.layout()
        entry = next(
            w
            for w in response.context["waiting"]
            if w["case"].pk == unmeasured.pk
        )
        self.assertFalse(entry["placeable"])
        self.assertContains(response, "no length recorded")

    def test_each_waiting_row_can_start_a_placement(self):
        response = self.layout()
        self.assertContains(
            response,
            "%s?case=%s"
            % (reverse("backoffice:storage-layout"), self.case.pk),
        )


class LiftOutTests(YardPlanTestBase):
    """Stage 7. A placement used to be a one-way door."""

    def place_it(self):
        return place_asset(
            self.agreement,
            self.zone,
            Decimal("0"),
            Decimal("0"),
            "LENGTHWISE",
            None,
        )

    def test_lifting_out_frees_the_ground(self):
        self.place_it()
        response = self.client.post(
            reverse("backoffice:storage-unplace", args=[self.case.pk]),
            follow=True,
        )
        self.assertContains(response, "lifted out")
        self.assertEqual(StoragePosition.objects.count(), 0)

    def test_the_button_appears_only_for_a_placed_asset(self):
        without = self.layout(case=self.case.pk)
        self.assertNotContains(without, "Lift out")
        self.place_it()
        with_position = self.layout(case=self.case.pk)
        self.assertContains(with_position, "Lift out")

    def test_lifting_out_an_unplaced_asset_says_so(self):
        response = self.client.post(
            reverse("backoffice:storage-unplace", args=[self.case.pk]),
            follow=True,
        )
        self.assertContains(response, "not placed on the yard")


class YardPayloadTests(YardPlanTestBase):
    """Stage 3. The browser held every fact the rules need and used none
    of them; the ghost was blue wherever the pointer went."""

    def setUp(self):
        super().setUp()
        StorageObstacle.objects.create(
            zone=self.zone,
            name="Travel-lift lane",
            kind=ObstacleKind.CRANE,
            pos_x_m=Decimal("10"),
            pos_y_m=Decimal("0"),
            width_m=Decimal("6"),
            length_m=Decimal("25"),
        )
        place_asset(
            self.agreement,
            self.zone,
            Decimal("0"),
            Decimal("0"),
            "LENGTHWISE",
            datetime.date(2027, 3, 1),
        )

    def test_the_placer_is_handed_outlines_obstacles_and_clearance(self):
        payload = self.yard_data(self.layout(case=self.case.pk))
        self.assertEqual(len(payload), 1)
        zone = payload[0]
        self.assertEqual(float(zone["clearance"]), 0.75)
        self.assertEqual(len(zone["assets"]), 1)
        self.assertEqual(zone["assets"][0]["case"], self.case.pk)
        self.assertEqual(zone["assets"][0]["exit"], "2027-03-01")
        self.assertEqual(len(zone["obstacles"]), 1)
        self.assertEqual(zone["obstacles"][0]["name"], "Travel-lift lane")

    def test_a_asset_with_no_exit_date_is_marked_as_leaving_last(self):
        """The same rule ``_exit_sort_key`` applies server-side, stated
        rather than left to a null the script would have to guess at."""
        other = self.make_case()
        place_asset(
            other.storage_agreement,
            self.zone,
            Decimal("20"),
            Decimal("0"),
            "LENGTHWISE",
            None,
        )
        payload = self.yard_data(self.layout(case=self.case.pk))
        exits = {b["case"]: b["exit"] for b in payload[0]["assets"]}
        self.assertEqual(exits[other.pk], "9999-12-31")

    def test_the_placer_is_loaded_in_placing_mode_only(self):
        """The plain plan is a picture; only ?case= makes it a tool."""
        self.assertNotContains(self.layout(), "js/yard.js")
        self.assertContains(self.layout(case=self.case.pk), "js/yard.js")

    def test_the_space_check_shares_the_same_payload(self):
        """Both pages judge a spot by identical rules - they used to
        carry two copies, and only one of them knew any rules at all."""
        response = self.client.get(reverse("backoffice:storage-fit-check"))
        payload = self.yard_data(response)
        self.assertEqual(len(payload[0]["obstacles"]), 1)
        self.assertEqual(float(payload[0]["clearance"]), 0.75)

    def test_the_verdict_strings_are_translated_server_side(self):
        response = self.layout(case=self.case.pk)
        labels = json.loads(
            re.search(
                r'<script id="yard-labels" type="application/json">(.*?)</script>',
                response.content.decode(),
                re.S,
            ).group(1)
        )
        self.assertIn("{case}", labels["collides"])
        self.assertIn("{gap}", labels["tooClose"])
        self.assertIn("{name}", labels["onObstacle"])


class ReadableAtAGlanceTests(YardPlanTestBase):
    """Stage 8. Exit date was encoded only as a red-to-green ramp - the
    one pair deuteranopia cannot separate, and the same pair the ghost
    needs for legal/illegal."""

    def setUp(self):
        super().setUp()
        place_asset(
            self.agreement,
            self.zone,
            Decimal("0"),
            Decimal("0"),
            "LENGTHWISE",
            datetime.date(2027, 3, 1),
        )

    def test_the_exit_month_is_printed_inside_the_outline(self):
        asset = self.layout().context["zone_views"][0]["assets"][0]
        self.assertTrue(asset["exit_label"])
        self.assertContains(self.layout(), asset["exit_label"])

    def test_the_ramp_is_not_red_and_green(self):
        asset = self.layout().context["zone_views"][0]["assets"][0]
        self.assertNotIn(asset["fill"].lower(), {"#2e7d32", "#e53935"})

    def test_each_outline_carries_its_own_ink(self):
        """A label in one fixed dark colour is unreadable on the dark end
        of any ordered ramp."""
        asset = self.layout().context["zone_views"][0]["assets"][0]
        self.assertIn(asset["ink"], {"#ffffff", "#06231c", "#10202a"})

    def test_the_label_is_scaled_to_the_narrow_dimension(self):
        """A fixed size wrote the label about six metres wide across a
        four-metre breadth."""
        asset = self.layout().context["zone_views"][0]["assets"][0]
        self.assertLessEqual(asset["label_size"], float(asset["w"]))

    def test_occupancy_is_stated(self):
        zv = self.layout().context["zone_views"][0]
        self.assertEqual(zv["asset_count"], 1)
        # 10 x 4 m of a 40 x 25 m yard.
        self.assertEqual(zv["used_pct"], 4)

    def test_obstacles_count_as_used_ground(self):
        StorageObstacle.objects.create(
            zone=self.zone,
            name="Shed",
            kind=ObstacleKind.BUILDING,
            pos_x_m=Decimal("30"),
            pos_y_m=Decimal("0"),
            width_m=Decimal("10"),
            length_m=Decimal("25"),
        )
        zv = self.layout().context["zone_views"][0]
        # 40 m2 of outline plus 250 m2 of shed, out of 1000 m2.
        self.assertEqual(zv["used_pct"], 29)

    def test_reserved_ground_is_drawn_and_named(self):
        StorageObstacle.objects.create(
            zone=self.zone,
            name="Travel-lift lane",
            kind=ObstacleKind.CRANE,
            pos_x_m=Decimal("10"),
            pos_y_m=Decimal("0"),
            width_m=Decimal("6"),
            length_m=Decimal("25"),
        )
        response = self.layout()
        self.assertContains(response, "Travel-lift lane")
        self.assertContains(response, "hatch-%s" % self.zone.pk)

    def test_the_clearance_the_yard_keeps_is_stated_on_the_page(self):
        """Stated, not folklore held by whoever is placing. (The suite
        runs under LANGUAGE_CODE="en", so the decimal is a point here and
        a comma on the Serbian page.)"""
        self.assertContains(
            self.layout(), "keeps 0.75 m of working space between outlines"
        )
