"""OPEN-01: the approval-limit ladder (Limiti odobravanja) staff page.

The page is the configuration surface for ``accounts.ApprovalLimit``, the
rows ``quotes.services.required_approval_levels()`` reads to derive the
ACT-02 / INV-04 escalation ladder. What is pinned here:

* who may look (any staff role) and who may change (LADDER_EDIT_ROLES,
  i.e. the DIRECTOR alone, plus superusers) - proven by POSTing directly
  at every mutating endpoint, not by reading buttons out of the template.
  Deliberately NOT the PRICING_ROLES pair that maintains price lists: a
  manager who may rewrite the ladder can delete the director rung, raise
  the manager rung and then approve the resulting quote alone, which is
  the escalation ACT-02 exists to force;
* that every change leaves an append-only attribution row (INV-03), so a
  disputed approval can be traced to whoever moved the rung;
* the form guards: a limit is a positive EUR amount, the currency is not
  configurable (single-currency policy, OPEN-01 2026-08-02), and
  only ladder roles may carry a limit;
* that the ladder the page produces stays one ``required_approval_levels``
  can actually walk - in particular that the MANAGER tier keeps a
  non-empty interval;
* that merely viewing the page never writes.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import ApprovalLimit, ApprovalLimitChange, Role
from apps.cases.management.commands.seed_workflow import APPROVAL_LIMITS
from apps.cases.services import SETTLEMENT_ROLES
from apps.quotes.models import Approval
from apps.quotes.services import required_approval_levels

LIST_URL_NAME = "backoffice:approval-limits"
ADD_URL_NAME = "backoffice:approval-limit-add"
EDIT_URL_NAME = "backoffice:approval-limit-edit"
DELETE_URL_NAME = "backoffice:approval-limit-delete"


def make_user(username, role, **kwargs):
    return get_user_model().objects.create_user(
        username=username,
        password="demo12345",
        is_staff=kwargs.pop("is_staff", True),
        role=role,
        **kwargs,
    )


def seed_ladder(technician="100", manager="1000", director="5000"):
    """Create the three ladder rows; ``None`` leaves a rung unset."""
    rows = {}
    for role, amount in (
        (Role.TECHNICIAN, technician),
        (Role.MANAGER, manager),
        (Role.DIRECTOR, director),
    ):
        if amount is None:
            continue
        rows[role] = ApprovalLimit.objects.create(
            role=role, max_amount=Decimal(amount)
        )
    return rows


def add_payload(role, amount):
    return {"new-role": role, "new-max_amount": amount}


def edit_payload(limit, role=None, amount=None):
    prefix = "limit-%s" % limit.pk
    return {
        "%s-role" % prefix: role if role is not None else limit.role,
        "%s-max_amount" % prefix: (
            amount if amount is not None else limit.max_amount
        ),
    }


def amounts_by_role():
    return {row.role: row.max_amount for row in ApprovalLimit.objects.all()}


class ApprovalLimitsPageAccessTests(TestCase):
    """Any authenticated staff role may read the ladder; anonymous
    visitors are bounced to the login page and non-staff accounts get a
    403 (StaffRequiredMixin)."""

    def setUp(self):
        self.rows = seed_ladder()
        self.page = reverse(LIST_URL_NAME)

    def test_anonymous_get_redirects_to_login(self):
        from django.conf import settings

        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 302)
        self.assertIn(settings.LOGIN_URL, response.url)

    def test_anonymous_post_to_every_mutating_endpoint_redirects_to_login(self):
        from django.conf import settings

        limit = self.rows[Role.TECHNICIAN]
        before = amounts_by_role()
        endpoints = [
            (reverse(ADD_URL_NAME), add_payload(Role.MANAGER, "2000")),
            (
                reverse(EDIT_URL_NAME, args=[limit.pk]),
                edit_payload(limit, amount="777"),
            ),
            (reverse(DELETE_URL_NAME, args=[limit.pk]), {}),
        ]
        for url, payload in endpoints:
            with self.subTest(url=url):
                response = self.client.post(url, payload)
                self.assertEqual(response.status_code, 302)
                self.assertIn(settings.LOGIN_URL, response.url)
        self.assertEqual(amounts_by_role(), before)

    def test_every_staff_role_may_view_the_ladder(self):
        for role in [
            Role.FRONT_DESK,
            Role.TECHNICIAN,
            Role.MANAGER,
            Role.DIRECTOR,
        ]:
            with self.subTest(role=role):
                user = make_user("staff-%s" % role.lower(), role)
                self.client.force_login(user)
                response = self.client.get(self.page)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Approval limits")
                # The configured amounts are on the page for every role.
                self.assertContains(response, "1000.00")
                self.client.logout()

    def test_authenticated_non_staff_user_is_forbidden(self):
        # No role: any role would make the account staff (User.save()).
        outsider = make_user("outsider", "", is_staff=False)
        self.client.force_login(outsider)
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 403)

    def test_view_only_role_gets_no_edit_controls(self):
        self.client.force_login(make_user("front", Role.FRONT_DESK))
        response = self.client.get(self.page)
        self.assertNotContains(response, "limit-form-new")
        self.assertContains(response, "Only the director")

    def test_a_manager_may_read_the_ladder_but_gets_no_edit_controls(self):
        self.client.force_login(make_user("mgr-view", Role.MANAGER))
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1000")
        self.assertNotContains(response, "limit-form-new")

    def test_pricing_roles_are_the_settlement_roles(self):
        from apps.backoffice.views import PRICING_ROLES

        self.assertEqual(tuple(PRICING_ROLES), tuple(SETTLEMENT_ROLES))
        self.assertEqual(tuple(PRICING_ROLES), ("MANAGER", "DIRECTOR"))

    def test_the_ladder_is_not_gated_like_a_price_list(self):
        """Separation of duties: whoever may configure the approval ladder
        can approve past it, so the ladder is the director's alone and must
        not inherit the price-list roles (ACT-02)."""
        from apps.accounts.services import LADDER_EDIT_ROLES
        from apps.backoffice.views import PRICING_ROLES

        self.assertEqual(tuple(LADDER_EDIT_ROLES), (Role.DIRECTOR,))
        self.assertNotEqual(tuple(LADDER_EDIT_ROLES), tuple(PRICING_ROLES))
        self.assertNotIn(Role.MANAGER, LADDER_EDIT_ROLES)


class ApprovalLimitMutationRoleTests(TestCase):
    """Only the director (or a superuser) may add/edit/delete. Proven by
    POSTing at the endpoints directly - the hidden button is not the
    protection."""

    def setUp(self):
        self.rows = seed_ladder()
        self.technician_row = self.rows[Role.TECHNICIAN]
        self.director_row = self.rows[Role.DIRECTOR]

    def _assert_refused(self, response):
        self.assertContains(response, "may not change the approval limits")

    def test_technician_post_to_add_is_refused(self):
        ApprovalLimit.objects.filter(role=Role.DIRECTOR).delete()
        self.client.force_login(make_user("tech", Role.TECHNICIAN))
        response = self.client.post(
            reverse(ADD_URL_NAME),
            add_payload(Role.DIRECTOR, "9000"),
            follow=True,
        )
        self._assert_refused(response)
        self.assertFalse(
            ApprovalLimit.objects.filter(role=Role.DIRECTOR).exists()
        )

    def test_technician_post_to_edit_is_refused(self):
        self.client.force_login(make_user("tech", Role.TECHNICIAN))
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[self.technician_row.pk]),
            edit_payload(self.technician_row, amount="900"),
            follow=True,
        )
        self._assert_refused(response)
        self.technician_row.refresh_from_db()
        self.assertEqual(self.technician_row.max_amount, Decimal("100.00"))

    def test_technician_post_to_delete_is_refused(self):
        self.client.force_login(make_user("tech", Role.TECHNICIAN))
        response = self.client.post(
            reverse(DELETE_URL_NAME, args=[self.technician_row.pk]),
            follow=True,
        )
        self._assert_refused(response)
        self.assertTrue(
            ApprovalLimit.objects.filter(pk=self.technician_row.pk).exists()
        )

    def test_front_desk_post_to_add_is_refused(self):
        ApprovalLimit.objects.filter(role=Role.DIRECTOR).delete()
        self.client.force_login(make_user("front", Role.FRONT_DESK))
        response = self.client.post(
            reverse(ADD_URL_NAME),
            add_payload(Role.DIRECTOR, "9000"),
            follow=True,
        )
        self._assert_refused(response)
        self.assertEqual(ApprovalLimit.objects.count(), 2)

    def test_manager_may_not_change_the_ladder(self):
        """The attack the price-list gate allowed: a manager deletes the
        director rung, raises the manager rung past it and then approves
        the quote alone. Every step must be refused at the endpoint."""
        self.client.force_login(make_user("mgr", Role.MANAGER))

        response = self.client.post(
            reverse(DELETE_URL_NAME, args=[self.director_row.pk]), follow=True
        )
        self._assert_refused(response)
        self.assertTrue(
            ApprovalLimit.objects.filter(pk=self.director_row.pk).exists()
        )

        manager_row = self.rows[Role.MANAGER]
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[manager_row.pk]),
            edit_payload(manager_row, amount="999999"),
            follow=True,
        )
        self._assert_refused(response)
        manager_row.refresh_from_db()
        self.assertEqual(manager_row.max_amount, Decimal("1000.00"))

        ApprovalLimit.objects.filter(role=Role.DIRECTOR).delete()
        response = self.client.post(
            reverse(ADD_URL_NAME),
            add_payload(Role.DIRECTOR, "999999"),
            follow=True,
        )
        self._assert_refused(response)
        self.assertFalse(
            ApprovalLimit.objects.filter(role=Role.DIRECTOR).exists()
        )
        # A 50,000 EUR quote still needs the director.
        self.assertEqual(
            required_approval_levels(Decimal("50000")),
            [Approval.Level.CLIENT, Approval.Level.DIRECTOR],
        )
        self.assertEqual(ApprovalLimitChange.objects.count(), 0)

    def test_director_may_add_edit_and_delete(self):
        ApprovalLimit.objects.filter(role=Role.DIRECTOR).delete()
        self.client.force_login(make_user("dir-full", Role.DIRECTOR))

        response = self.client.post(
            reverse(ADD_URL_NAME),
            add_payload(Role.DIRECTOR, "5000"),
            follow=True,
        )
        self.assertContains(response, "added")
        director = ApprovalLimit.objects.get(role=Role.DIRECTOR)
        self.assertEqual(director.max_amount, Decimal("5000.00"))

        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[self.technician_row.pk]),
            edit_payload(self.technician_row, amount="250"),
            follow=True,
        )
        self.assertContains(response, "updated")
        self.technician_row.refresh_from_db()
        self.assertEqual(self.technician_row.max_amount, Decimal("250.00"))

        response = self.client.post(
            reverse(DELETE_URL_NAME, args=[director.pk]), follow=True
        )
        self.assertContains(response, "deleted")
        self.assertFalse(ApprovalLimit.objects.filter(pk=director.pk).exists())

    def test_every_change_leaves_an_attribution_row(self):
        """INV-03: who moved which rung survives, including the deletion of
        the rung itself - the change worth keeping."""
        actor = make_user("dir-audit", Role.DIRECTOR)
        self.client.force_login(actor)
        self.client.post(
            reverse(EDIT_URL_NAME, args=[self.technician_row.pk]),
            edit_payload(self.technician_row, amount="250"),
        )
        self.client.post(
            reverse(DELETE_URL_NAME, args=[self.director_row.pk])
        )

        updated, deleted = (
            ApprovalLimitChange.objects.get(
                action=ApprovalLimitChange.Action.UPDATED
            ),
            ApprovalLimitChange.objects.get(
                action=ApprovalLimitChange.Action.DELETED
            ),
        )
        self.assertEqual(updated.role, Role.TECHNICIAN)
        self.assertEqual(updated.old_amount, Decimal("100.00"))
        self.assertEqual(updated.new_amount, Decimal("250.00"))
        self.assertEqual(updated.actor, actor)
        self.assertEqual(updated.actor_role, Role.DIRECTOR)
        self.assertEqual(deleted.role, Role.DIRECTOR)
        self.assertEqual(deleted.old_amount, Decimal("5000.00"))
        self.assertIsNone(deleted.new_amount)
        # The trail outlives the row it describes.
        self.assertFalse(
            ApprovalLimit.objects.filter(role=Role.DIRECTOR).exists()
        )

    def test_deleting_the_top_rung_does_not_claim_an_escalation(self):
        """There is no rung above the director, so nothing escalates - the
        director requirement simply keeps applying with no threshold shown."""
        self.client.force_login(make_user("dir-top", Role.DIRECTOR))
        response = self.client.post(
            reverse(DELETE_URL_NAME, args=[self.director_row.pk]), follow=True
        )
        self.assertContains(response, "Nothing escalates")
        self.assertEqual(
            required_approval_levels(Decimal("3000")),
            [Approval.Level.CLIENT, Approval.Level.DIRECTOR],
        )

    def test_director_may_edit(self):
        self.client.force_login(make_user("dir", Role.DIRECTOR))
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[self.director_row.pk]),
            edit_payload(self.director_row, amount="7500"),
            follow=True,
        )
        self.assertContains(response, "updated")
        self.director_row.refresh_from_db()
        self.assertEqual(self.director_row.max_amount, Decimal("7500.00"))

    def test_superuser_without_a_role_may_edit(self):
        root = get_user_model().objects.create_superuser(
            username="root", password="demo12345", email="root@example.com"
        )
        self.assertEqual(root.role, "")
        self.client.force_login(root)
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[self.director_row.pk]),
            edit_payload(self.director_row, amount="6000"),
            follow=True,
        )
        self.assertContains(response, "updated")
        self.director_row.refresh_from_db()
        self.assertEqual(self.director_row.max_amount, Decimal("6000.00"))


class ApprovalLimitFormValidationTests(TestCase):
    """Amount and role validation on the configuration form."""

    def setUp(self):
        self.rows = seed_ladder()
        self.manager_row = self.rows[Role.MANAGER]
        self.client.force_login(make_user("dir", Role.DIRECTOR))

    def test_negative_amount_is_refused(self):
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[self.manager_row.pk]),
            edit_payload(self.manager_row, amount="-1"),
            follow=True,
        )
        self.assertContains(response, "must be a positive amount in EUR")
        self.manager_row.refresh_from_db()
        self.assertEqual(self.manager_row.max_amount, Decimal("1000.00"))

    def test_zero_amount_is_refused(self):
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[self.manager_row.pk]),
            edit_payload(self.manager_row, amount="0"),
            follow=True,
        )
        self.assertContains(response, "must be a positive amount in EUR")
        self.manager_row.refresh_from_db()
        self.assertEqual(self.manager_row.max_amount, Decimal("1000.00"))

    def test_negative_amount_on_add_creates_no_row(self):
        ApprovalLimit.objects.filter(role=Role.DIRECTOR).delete()
        response = self.client.post(
            reverse(ADD_URL_NAME),
            add_payload(Role.DIRECTOR, "-5000"),
            follow=True,
        )
        self.assertContains(response, "must be a positive amount in EUR")
        self.assertFalse(
            ApprovalLimit.objects.filter(role=Role.DIRECTOR).exists()
        )

    def test_blank_amount_is_refused(self):
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[self.manager_row.pk]),
            edit_payload(self.manager_row, amount=""),
            follow=True,
        )
        self.manager_row.refresh_from_db()
        self.assertEqual(self.manager_row.max_amount, Decimal("1000.00"))
        self.assertContains(response, "required")

    def test_currency_is_not_configurable_and_stays_eur(self):
        from apps.backoffice.forms import ApprovalLimitForm

        # Single-currency policy (OPEN-01, 2026-08-02): the model
        # carries no currency column and the form offers no currency input.
        model_fields = {f.name for f in ApprovalLimit._meta.get_fields()}
        self.assertNotIn("currency", model_fields)
        self.assertEqual(list(ApprovalLimitForm().fields), ["role", "max_amount"])
        # A smuggled currency value is simply not a field, so it is dropped.
        payload = edit_payload(self.manager_row, amount="1200")
        payload["limit-%s-currency" % self.manager_row.pk] = "RSD"
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[self.manager_row.pk]),
            payload,
            follow=True,
        )
        self.assertContains(response, "updated")
        self.manager_row.refresh_from_db()
        self.assertEqual(self.manager_row.max_amount, Decimal("1200.00"))
        self.assertFalse(hasattr(self.manager_row, "currency"))

    def test_page_labels_the_amounts_as_eur(self):
        response = self.client.get(reverse(LIST_URL_NAME))
        self.assertContains(response, "EUR")
        self.assertContains(response, "€")

    def test_limit_on_a_non_ladder_role_is_refused(self):
        response = self.client.post(
            reverse(ADD_URL_NAME),
            add_payload(Role.FRONT_DESK, "50"),
            follow=True,
        )
        self.assertFalse(
            ApprovalLimit.objects.filter(role=Role.FRONT_DESK).exists()
        )
        self.assertContains(response, "Select a valid choice")

    def test_second_limit_for_the_same_role_is_refused(self):
        response = self.client.post(
            reverse(ADD_URL_NAME),
            add_payload(Role.MANAGER, "2000"),
            follow=True,
        )
        self.assertEqual(
            ApprovalLimit.objects.filter(role=Role.MANAGER).count(), 1
        )
        self.assertContains(response, "already exists")


class ApprovalLimitLadderCoherenceTests(TestCase):
    """The rows the page writes have to stay a ladder
    ``required_approval_levels()`` can walk (ACT-02 / INV-04).

    That function returns early on ``amount <= technician_limit``, so the
    MANAGER tier only exists while ``technician < manager`` strictly; and
    it never reads the director amount as a ceiling, so the ladder is
    only required to rise.
    """

    def setUp(self):
        self.rows = seed_ladder()
        self.client.force_login(make_user("dir", Role.DIRECTOR))

    def test_baseline_ladder_has_a_reachable_manager_tier(self):
        self.assertEqual(
            required_approval_levels(Decimal("100")), [Approval.Level.CLIENT]
        )
        self.assertEqual(
            required_approval_levels(Decimal("500")),
            [Approval.Level.CLIENT, Approval.Level.MANAGER],
        )
        self.assertEqual(
            required_approval_levels(Decimal("5000")),
            [Approval.Level.CLIENT, Approval.Level.DIRECTOR],
        )

    def test_technician_limit_equal_to_manager_limit_is_refused(self):
        row = self.rows[Role.TECHNICIAN]
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[row.pk]),
            edit_payload(row, amount="1000"),
            follow=True,
        )
        self.assertContains(response, "must stay strictly below")
        row.refresh_from_db()
        self.assertEqual(row.max_amount, Decimal("100.00"))
        # The manager tier is still reachable.
        self.assertEqual(
            required_approval_levels(Decimal("500")),
            [Approval.Level.CLIENT, Approval.Level.MANAGER],
        )

    def test_technician_limit_above_manager_limit_is_refused(self):
        row = self.rows[Role.TECHNICIAN]
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[row.pk]),
            edit_payload(row, amount="2000"),
            follow=True,
        )
        self.assertContains(response, "must stay strictly below")
        row.refresh_from_db()
        self.assertEqual(row.max_amount, Decimal("100.00"))

    def test_manager_limit_below_technician_limit_is_refused_on_add(self):
        ApprovalLimit.objects.filter(role=Role.MANAGER).delete()
        response = self.client.post(
            reverse(ADD_URL_NAME),
            add_payload(Role.MANAGER, "50"),
            follow=True,
        )
        self.assertContains(response, "must stay strictly below")
        self.assertFalse(ApprovalLimit.objects.filter(role=Role.MANAGER).exists())

    def test_manager_limit_above_director_limit_is_refused(self):
        row = self.rows[Role.MANAGER]
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[row.pk]),
            edit_payload(row, amount="6000"),
            follow=True,
        )
        self.assertContains(response, "must not exceed the director limit")
        row.refresh_from_db()
        self.assertEqual(row.max_amount, Decimal("1000.00"))

    def test_partially_configured_ladder_is_accepted(self):
        ApprovalLimit.objects.all().delete()
        response = self.client.post(
            reverse(ADD_URL_NAME),
            add_payload(Role.DIRECTOR, "5000"),
            follow=True,
        )
        self.assertContains(response, "added")
        # No technician/manager rows -> everything escalates (conservative).
        self.assertEqual(
            required_approval_levels(Decimal("10")),
            [Approval.Level.CLIENT, Approval.Level.DIRECTOR],
        )

    def test_editing_the_technician_limit_moves_the_self_approval_boundary(self):
        row = self.rows[Role.TECHNICIAN]
        self.assertEqual(
            required_approval_levels(Decimal("500")),
            [Approval.Level.CLIENT, Approval.Level.MANAGER],
        )
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[row.pk]),
            edit_payload(row, amount="600"),
            follow=True,
        )
        self.assertContains(response, "updated")
        # 500 EUR is now inside the service's own authority.
        self.assertEqual(
            required_approval_levels(Decimal("500")), [Approval.Level.CLIENT]
        )
        # ... and 601 still needs a manager, the tier did not collapse.
        self.assertEqual(
            required_approval_levels(Decimal("601")),
            [Approval.Level.CLIENT, Approval.Level.MANAGER],
        )

    def test_editing_the_manager_limit_moves_the_director_boundary(self):
        row = self.rows[Role.MANAGER]
        self.assertEqual(
            required_approval_levels(Decimal("2000")),
            [Approval.Level.CLIENT, Approval.Level.DIRECTOR],
        )
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[row.pk]),
            edit_payload(row, amount="3000"),
            follow=True,
        )
        self.assertContains(response, "updated")
        self.assertEqual(
            required_approval_levels(Decimal("2000")),
            [Approval.Level.CLIENT, Approval.Level.MANAGER],
        )
        self.assertEqual(
            required_approval_levels(Decimal("3001")),
            [Approval.Level.CLIENT, Approval.Level.DIRECTOR],
        )

    def test_deleting_the_technician_limit_removes_self_approval(self):
        row = self.rows[Role.TECHNICIAN]
        response = self.client.post(
            reverse(DELETE_URL_NAME, args=[row.pk]), follow=True
        )
        self.assertContains(response, "deleted")
        self.assertEqual(
            required_approval_levels(Decimal("10")),
            [Approval.Level.CLIENT, Approval.Level.MANAGER],
        )

    def test_deleting_the_manager_limit_escalates_to_the_director(self):
        row = self.rows[Role.MANAGER]
        response = self.client.post(
            reverse(DELETE_URL_NAME, args=[row.pk]), follow=True
        )
        self.assertContains(response, "deleted")
        self.assertEqual(
            required_approval_levels(Decimal("500")),
            [Approval.Level.CLIENT, Approval.Level.DIRECTOR],
        )

    def test_technician_limit_may_not_swallow_the_director_rung(self):
        """With no manager rung the technician limit is the only gate the
        ladder reads, so it must still not be configurable above the
        director limit: otherwise a 5,500 EUR quote (the director rung is
        5,000 EUR per OPEN-01) is self-approved by the service with
        no internal approval at all."""
        ApprovalLimit.objects.filter(role=Role.MANAGER).delete()
        row = self.rows[Role.TECHNICIAN]
        response = self.client.post(
            reverse(EDIT_URL_NAME, args=[row.pk]),
            edit_payload(row, amount="6000"),
            follow=True,
        )
        self.assertContains(response, "must stay below the director limit")
        row.refresh_from_db()
        self.assertEqual(row.max_amount, Decimal("100.00"))
        self.assertNotEqual(
            required_approval_levels(Decimal("5500")),
            [Approval.Level.CLIENT],
            "a quote above the director limit must still need an internal "
            "approval",
        )


class ApprovalLimitsPageIsReadOnlyTests(TestCase):
    """Rendering the configuration page must not write - the seeded
    placeholder amounts (OPEN-01: technician/manager still pending from
    management) survive a visit untouched."""

    def setUp(self):
        for role, amount in APPROVAL_LIMITS.items():
            ApprovalLimit.objects.create(role=role, max_amount=amount)
        self.page = reverse(LIST_URL_NAME)

    def test_viewing_does_not_change_the_seeded_amounts(self):
        before = amounts_by_role()
        self.assertEqual(
            before,
            {role: Decimal(amount) for role, amount in APPROVAL_LIMITS.items()},
        )
        for username, role in [
            ("front-r", Role.FRONT_DESK),
            ("mgr-r", Role.MANAGER),
        ]:
            self.client.force_login(make_user(username, role))
            self.assertEqual(self.client.get(self.page).status_code, 200)
            self.assertEqual(self.client.get(self.page).status_code, 200)
            self.client.logout()
        self.assertEqual(amounts_by_role(), before)
        self.assertEqual(ApprovalLimit.objects.count(), len(APPROVAL_LIMITS))

    def test_viewing_an_empty_ladder_creates_no_rows(self):
        ApprovalLimit.objects.all().delete()
        self.client.force_login(make_user("mgr-e", Role.MANAGER))
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ApprovalLimit.objects.count(), 0)
        self.assertContains(response, "No approval limits configured yet")

    def test_pending_placeholder_rows_are_badged(self):
        self.client.force_login(make_user("mgr-b", Role.MANAGER))
        response = self.client.get(self.page)
        # Technician + manager amounts are still pending (OPEN-01);
        # the director rung was answered on 2026-08-14 and is not badged.
        self.assertEqual(response.content.decode().count("Placeholder"), 2)
