"""Admin wiring regression tests for the storage app."""
from django.contrib.admin.sites import AdminSite
from django.test import TestCase

from .admin import SitePickupInline, StorageAgreementAdmin
from .models import SitePickup, StorageAgreement


class StorageAgreementAdminInlineTests(TestCase):
    """WNT rule 4: the site pickup stays editable from its agreement."""

    def setUp(self):
        self.model_admin = StorageAgreementAdmin(StorageAgreement, AdminSite())

    def test_site_pickup_inline_is_registered(self):
        self.assertIn(SitePickupInline, self.model_admin.inlines)

    def test_site_pickup_inline_targets_the_pickup_model(self):
        self.assertIs(SitePickupInline.model, SitePickup)
