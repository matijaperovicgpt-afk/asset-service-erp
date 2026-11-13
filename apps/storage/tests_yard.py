"""WNT-05 yard rules added while making the graphic plan usable.

Three things the plan could not express before, all of which made the
drawing disagree with the real yard:

* two outlines could sit flush against each other, so the plan showed a
  yard that nobody could work in;
* a zone was a bare rectangle, so an asset could be placed across the
  travel-lift lane or on top of the shed;
* a placement was a one-way door - an asset could be moved but never
  taken off the plan, so cancelled contracts kept holding ground.

The dimension floor is here too, because it is the reason none of the
above could be trusted: a negative length produced an inverted footprint
that passed bounds and collided with nothing.
"""

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import (
    ObstacleKind,
    StorageAgreement,
    StorageObstacle,
    StorageOrientation,
    StoragePosition,
    StorageZone,
)
from .services import place_asset, unplace_asset, zone_footprint
from .tests import build_agreement


class YardTestBase(TestCase):
    """A 40 x 25 m yard and two identical 10 x 4 m assets."""

    CLEARANCE = Decimal("0.75")

    def setUp(self):
        self.zone = StorageZone.objects.create(
            name="Yard",
            width_m=Decimal("40"),
            length_m=Decimal("25"),
            clearance_m=self.CLEARANCE,
        )
        self.a = self.asset("a")
        self.b = self.asset("b")

    def asset(self, suffix, length=10, width=4):
        agreement = build_agreement(suffix)
        agreement.asset_length_m = Decimal(str(length))
        agreement.asset_width_m = Decimal(str(width))
        agreement.save()
        return agreement

    def place(self, agreement, x, y, exit_date=None, zone=None):
        return place_asset(
            agreement,
            zone or self.zone,
            Decimal(str(x)),
            Decimal(str(y)),
            StorageOrientation.LENGTHWISE,
            exit_date,
        )


class ClearanceTest(YardTestBase):
    """A outline needs working space around it, not merely its own outline:
    a stand, a ladder and a pressure washer have to fit."""

    def test_flush_outlines_are_refused(self):
        """The observed defect: two 4 m-breadth outlines at x=0 and x=4 were
        accepted with a nought-metre gap between them."""
        self.place(self.a, 0, 0)
        with self.assertRaises(ValidationError) as caught:
            self.place(self.b, 4, 0)
        self.assertIn("0.75", str(caught.exception))

    def test_a_gap_smaller_than_the_clearance_is_refused(self):
        self.place(self.a, 0, 0)
        with self.assertRaises(ValidationError):
            self.place(self.b, 4.5, 0)

    def test_exactly_the_clearance_is_accepted(self):
        """The gap the zone asks for is a gap that fits, not one that is
        one centimetre short of fitting."""
        self.place(self.a, 0, 0)
        self.place(self.b, 4.75, 0)
        self.assertEqual(StoragePosition.objects.count(), 2)

    def test_only_the_candidate_is_inflated(self):
        """Growing both rectangles would silently demand twice the
        configured gap. 4.75 m apart is 0.75 m of free ground, and that
        is what the zone asks for."""
        self.place(self.a, 0, 0)
        self.place(self.b, 4.75, 0)
        first, second = sorted(
            StoragePosition.objects.all(), key=lambda p: p.pos_x_m
        )
        gap = second.pos_x_m - (first.pos_x_m + self.a.asset_width_m)
        self.assertEqual(gap, self.CLEARANCE)

    def test_zero_clearance_lets_outlines_touch(self):
        """A racked hall may legitimately want none; the rule is per
        zone, not a global constant."""
        self.zone.clearance_m = Decimal("0")
        self.zone.save()
        self.place(self.a, 0, 0)
        self.place(self.b, 4, 0)
        self.assertEqual(StoragePosition.objects.count(), 2)

    def test_clearance_is_not_required_against_the_zone_edge(self):
        """A fence is not a neighbour: an asset may sit against the edge,
        and the bounds test is measured on the bare footprint."""
        self.place(self.a, 0, 0)
        self.assertEqual(StoragePosition.objects.get().pos_x_m, Decimal("0"))

    def test_a_real_overlap_still_says_overlap(self):
        """Clearance must not swallow the collision message - staff read
        these to know what to do next."""
        self.place(self.a, 0, 0)
        with self.assertRaises(ValidationError) as caught:
            self.place(self.b, 2, 2)
        self.assertIn("overlaps", str(caught.exception).lower())

    def test_moving_a_asset_does_not_clear_itself(self):
        """Re-placing upserts, so the asset's own old footprint must be
        excluded - otherwise a 0.5 m nudge collides with where it was."""
        self.place(self.a, 0, 0)
        self.place(self.a, 0.5, 0)
        self.assertEqual(StoragePosition.objects.count(), 1)


class ObstacleTest(YardTestBase):
    """Ground an asset may never occupy: lanes, crane tracks, sheds."""

    def setUp(self):
        super().setUp()
        self.lane = StorageObstacle.objects.create(
            zone=self.zone,
            name="Travel-lift lane",
            kind=ObstacleKind.CRANE,
            pos_x_m=Decimal("10"),
            pos_y_m=Decimal("0"),
            width_m=Decimal("6"),
            length_m=Decimal("25"),
        )

    def test_placing_on_reserved_ground_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            self.place(self.a, 11, 0)
        self.assertIn("Travel-lift lane", str(caught.exception))

    def test_flush_against_an_obstacle_is_accepted(self):
        """A lane exists to be driven; a outline parked along its edge is
        exactly right, so obstacles are tested without clearance."""
        self.place(self.a, 6, 0)
        self.assertEqual(StoragePosition.objects.count(), 1)

    def test_an_obstacle_in_another_zone_does_not_reach(self):
        other = StorageZone.objects.create(
            name="Hall", width_m=Decimal("40"), length_m=Decimal("25")
        )
        self.place(self.a, 11, 0, zone=other)
        self.assertEqual(StoragePosition.objects.count(), 1)

    def test_an_obstacle_does_not_trap_a_asset_behind_it(self):
        """Exit order is about assets leaving. An obstacle never leaves,
        so it cannot be "in front of" anything in that sense - it just
        occupies ground."""
        self.place(self.a, 17, 10, datetime.date(2027, 3, 1))
        self.assertEqual(StoragePosition.objects.count(), 1)

    def test_the_footprint_matches_the_asset_shape(self):
        self.assertEqual(
            self.lane.footprint,
            (Decimal("10"), Decimal("0"), Decimal("16"), Decimal("25")),
        )


class UnplaceTest(YardTestBase):
    """"Lift out": the ground goes back to free."""

    def test_unplacing_frees_the_ground(self):
        self.place(self.a, 0, 0)
        self.assertTrue(unplace_asset(self.a))
        self.assertEqual(StoragePosition.objects.count(), 0)
        # And the spot is genuinely reusable, not merely unlisted.
        self.place(self.b, 0, 0)
        self.assertEqual(StoragePosition.objects.count(), 1)

    def test_unplacing_an_unplaced_asset_reports_nothing_done(self):
        self.assertFalse(unplace_asset(self.a))

    def test_a_legacy_hall_placement_is_left_alone(self):
        """Hall/row/slot (WNT-06) is a different record for a different
        kind of yard, and lifting an asset off the graphic plan must not
        delete it."""
        StoragePosition.objects.create(
            agreement=self.a, hall="H1", row="A", slot="3"
        )
        self.place(self.a, 0, 0)
        self.assertTrue(unplace_asset(self.a))
        remaining = StoragePosition.objects.get(agreement=self.a)
        self.assertEqual(remaining.hall, "H1")
        self.assertFalse(remaining.is_coordinate_placed)


class AssetDimensionFloorTest(TestCase):
    """A outline has a positive size or none at all.

    Without the floor, ``zone_footprint(-40, -9, 10, 10, LENGTHWISE)``
    returned ``(10, 10, 1.00, -30.00)`` - an inverted rectangle with x1
    < x0, which passes the bounds test and overlaps nothing. Two assets
    were then placed on the identical square metre.
    """

    def test_a_negative_length_is_refused_by_full_clean(self):
        agreement = build_agreement("neg")
        agreement.asset_length_m = Decimal("-40")
        with self.assertRaises(ValidationError) as caught:
            agreement.full_clean()
        self.assertIn("asset_length_m", caught.exception.error_dict)

    def test_a_negative_width_is_refused_by_full_clean(self):
        agreement = build_agreement("negw")
        agreement.asset_width_m = Decimal("-9")
        with self.assertRaises(ValidationError) as caught:
            agreement.full_clean()
        self.assertIn("asset_width_m", caught.exception.error_dict)

    def test_zero_is_refused_too(self):
        agreement = build_agreement("zero")
        agreement.asset_length_m = Decimal("0")
        with self.assertRaises(ValidationError):
            agreement.full_clean()

    def test_no_dimensions_at_all_stays_legal(self):
        """An inquiry taken by phone is allowed to be incomplete; it is
        the *negative* value that was never meaningful."""
        agreement = build_agreement("none")
        agreement.asset_length_m = None
        agreement.asset_width_m = None
        agreement.full_clean()

    def test_the_inversion_the_floor_prevents(self):
        """Pins why the floor matters, not merely that it exists."""
        x0, y0, x1, y1 = zone_footprint(
            Decimal("-40"),
            Decimal("-9"),
            Decimal("10"),
            Decimal("10"),
            StorageOrientation.LENGTHWISE,
        )
        self.assertLess(x1, x0)
        self.assertLess(y1, y0)

    def test_the_storage_docs_form_refuses_a_negative_length(self):
        """The card that actually writes these values (WNT-01) inherits
        the model validator, so the floor holds at the UI too."""
        from apps.backoffice.forms import StorageAgreementForm

        form = StorageAgreementForm(
            {
                "asset_length_m": "-40",
                "asset_width_m": "-9",
                "accommodation": "OPEN",
                "term": "SEASONAL",
            },
            instance=StorageAgreement(),
        )
        self.assertFalse(form.is_valid())
        self.assertIn("asset_length_m", form.errors)
        self.assertIn("asset_width_m", form.errors)
