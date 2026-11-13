"""Regressions from the 2026-11-04 module code review (attachments)."""

from io import BytesIO
from unittest.mock import patch

from django.test import SimpleTestCase

from .imaging import build_thumbnail_bytes
from .tests import _jpeg_bytes


class SourceSizeCapTests(SimpleTestCase):
    """A source above the pixel cap gets no thumbnail instead of being
    decoded into gigabytes of bitmap inside the request worker."""

    def test_an_oversized_source_yields_no_thumbnail(self):
        with patch("apps.attachments.imaging.MAX_SOURCE_PIXELS", 10_000):
            self.assertIsNone(build_thumbnail_bytes(BytesIO(_jpeg_bytes(200, 100))))

    def test_a_source_within_the_cap_still_thumbnails(self):
        with patch("apps.attachments.imaging.MAX_SOURCE_PIXELS", 10_000):
            self.assertIsNotNone(build_thumbnail_bytes(BytesIO(_jpeg_bytes(90, 90))))
