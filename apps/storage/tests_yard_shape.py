"""WNT-09: the outline's shape, and what a changed rectangle does to parked assets.

An asset used to be a rounded box. Now every asset is drawn as a outline whose
bow says which way it will be craned out. The rectangle stays what the
rules judge; the outline is how the plan reads.
"""

from decimal import Decimal

from django.test import TestCase

from .models import ObstacleKind, StorageObstacle, StorageOrientation
from .services import outline_path, zone_conflicts
from .tests_yard import YardTestBase


def _numbers(path):
    return [float(tok) for tok in path.split() if tok[0] in "0123456789-."]


class OutlinePathTest(TestCase):
    def test_lengthwise_bow_points_at_the_exit(self):
        d = outline_path(10, 5, 14, 15, StorageOrientation.LENGTHWISE)
        # The path starts at the bow tip: mid-breadth on the exit-side edge.
        self.assertTrue(d.startswith("M 12.00 5.00 "), d)
        self.assertTrue(d.endswith(" Z"))

    def test_crosswise_bow_points_left(self):
        d = outline_path(10, 5, 20, 9, StorageOrientation.CROSSWISE)
        self.assertTrue(d.startswith("M 10.00 7.00 "), d)

    def test_outline_stays_inside_its_footprint(self):
        for orientation, box in (
            (StorageOrientation.LENGTHWISE, (3, 2, 7, 12)),
            (StorageOrientation.CROSSWISE, (3, 2, 13, 6)),
        ):
            d = outline_path(*box, orientation)
            nums = _numbers(d)
            xs, ys = nums[0::2], nums[1::2]
            x0, y0, x1, y1 = box
            self.assertGreaterEqual(min(xs), x0 - 1e-9)
            self.assertLessEqual(max(xs), x1 + 1e-9)
            self.assertGreaterEqual(min(ys), y0 - 1e-9)
            self.assertLessEqual(max(ys), y1 + 1e-9)

    def test_path_data_is_locale_proof(self):
        """No commas, ever: a Serbian decimal comma inside ``d`` would be
        read as a separator and shift every coordinate after it."""
        d = outline_path(
            Decimal("1.5"), Decimal("2.25"), Decimal("5.5"), Decimal("12.75"), "LENGTHWISE"
        )
        self.assertNotIn(",", d)


class ZoneConflictsTest(YardTestBase):
    def test_new_lane_across_a_parked_asset(self):
        self.place(self.a, 2, 2)
        StorageObstacle.objects.create(
            zone=self.zone,
            name="Lane",
            kind=ObstacleKind.LANE,
            pos_x_m=0,
            pos_y_m=5,
            width_m=40,
            length_m=3,
        )
        conflicts = zone_conflicts(self.zone)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("Lane", conflicts[0][1])

    def test_box_shrunk_under_a_parked_asset(self):
        self.place(self.a, 30, 2)
        self.place(self.b, 2, 2)
        self.zone.width_m = Decimal("20")
        self.zone.save()
        conflicts = zone_conflicts(self.zone)
        self.assertEqual([p.agreement for p, _ in conflicts], [self.a])
        self.assertIn("edge", conflicts[0][1])
