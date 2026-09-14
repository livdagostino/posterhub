import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .access import GROUP_MANAGER_ROLE
from .models import (
    ActivityLog, PendingAssignmentDismissal, ResearchGroup, ResearchInterest,
    ResearchPoster, UserGroupMembership,
)


@override_settings(
    SHIBBOLETH_AUTH=False,
    SECURE_SSL_REDIRECT=False,
    ALLOWED_HOSTS=["testserver"],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class GroupManagementAccessTests(TestCase):

    entry_pages = ("upload", "dashboard", "conference", "my_groups")
    superusers = ("super_no_membership", "super_member")
    managers = ("manager_no_membership", "manager_member")
    ordinary_users = ("member", "no_membership")

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.manager_role, _ = Group.objects.get_or_create(name=GROUP_MANAGER_ROLE)
        cls.group = ResearchGroup.objects.create(name="Access regression research group")
        cls.interest = ResearchInterest.objects.create(group=cls.group, text="Original research interest")
        cls.users = {}
        for role in cls.superusers + cls.managers + cls.ordinary_users:
            user = User.objects.create_user(
                username=role,
                is_superuser=role in cls.superusers,
                is_staff=role == "super_member",
            )
            cls.users[role] = user
            if role in cls.managers:
                user.groups.add(cls.manager_role)
            if role in ("super_member", "manager_member", "member"):
                UserGroupMembership.objects.create(user=user, group=cls.group, is_primary=True)

    def sign_in(self, role, client=None):
        client = client or self.client
        client.force_login(self.users[role], backend="django.contrib.auth.backends.ModelBackend")
        return client

    def assert_management_navigation(self, response, *, groups, users):
        self.assertEqual(response.status_code, 200)
        for route, visible in (("group_list", groups), ("user_admin_list", users)):
            href = f'href="{reverse(route)}"'
            if visible:
                self.assertContains(response, href)
            else:
                self.assertNotContains(response, href)
        if groups:
            self.assertContains(response, "Manage Groups")

    def test_superusers_can_find_management_from_every_entry_page(self):
        for role in self.superusers:
            self.sign_in(role)
            for page in self.entry_pages:
                with self.subTest(role=role, page=page):
                    self.assert_management_navigation(self.client.get(reverse(page)), groups=True, users=True)

    def test_group_managers_can_find_management_without_research_membership(self):
        for role in self.managers:
            self.sign_in(role)
            for page in self.entry_pages:
                with self.subTest(role=role, page=page):
                    self.assert_management_navigation(self.client.get(reverse(page)), groups=True, users=False)

    def test_ordinary_users_do_not_see_management_links(self):
        for role in self.ordinary_users:
            self.sign_in(role)
            for page in self.entry_pages:
                with self.subTest(role=role, page=page):
                    self.assert_management_navigation(self.client.get(reverse(page)), groups=False, users=False)

    def test_anonymous_visitors_must_sign_in(self):
        for page in self.entry_pages + ("group_list", "user_admin_list"):
            with self.subTest(page=page):
                url = reverse(page)
                self.assertRedirects(
                    self.client.get(url), f"{reverse('login')}?next={url}", fetch_redirect_response=False
                )
        self.assertRedirects(
            self.client.post(reverse("group_create"), {"name": "Anonymous creation"}),
            f"{reverse('login')}?next={reverse('group_create')}",
            fetch_redirect_response=False,
        )
        self.assertFalse(ResearchGroup.objects.filter(name="Anonymous creation").exists())

    def test_personal_group_cards_offer_editing_only_to_group_managers(self):
        edit_href = f'href="{reverse("group_edit", args=[self.group.pk])}"'
        for role in ("super_member", "manager_member", "member"):
            with self.subTest(role=role):
                self.sign_in(role)
                response = self.client.get(reverse("my_groups"))
                self.assertContains(response, self.interest.text)
                if role == "member":
                    self.assertNotContains(response, edit_href)
                else:
                    self.assertContains(response, edit_href)
                    self.assertContains(response, "Edit group &amp; interests")

    def test_authorized_users_can_open_group_and_interest_forms(self):
        for role in self.superusers + self.managers:
            with self.subTest(role=role):
                self.sign_in(role)
                response = self.client.get(reverse("group_list"))
                self.assertContains(response, f'action="{reverse("group_create")}"')
                self.assertContains(response, 'name="research_interests"')
                response = self.client.get(reverse("group_edit", args=[self.group.pk]))
                self.assertContains(response, f'action="{reverse("interest_add", args=[self.group.pk])}"')
                self.assertContains(response, f'action="{reverse("interest_edit", args=[self.interest.pk])}"')

    def test_authorized_users_can_create_edit_and_delete_groups_and_interests(self):
        for role in self.superusers + self.managers:
            with self.subTest(role=role):
                self.sign_in(role)
                group_name = f"Created by {role}"
                response = self.client.post(reverse("group_create"), {
                    "name": group_name,
                    "research_interests": "Computer vision\n\n  Medical imaging  \n",
                })
                self.assertRedirects(response, reverse("group_list"), fetch_redirect_response=False)
                group = ResearchGroup.objects.get(name=group_name)
                self.assertEqual(list(group.interests.values_list("text", flat=True)), ["Computer vision", "Medical imaging"])

                edit_url = reverse("group_edit", args=[group.pk])
                response = self.client.post(edit_url, {"name": f"Renamed by {role}"})
                self.assertRedirects(response, edit_url, fetch_redirect_response=False)
                group.refresh_from_db()
                self.assertEqual(group.name, f"Renamed by {role}")

                response = self.client.post(reverse("interest_add", args=[group.pk]), {
                    "text": "Representation learning\nRobotics",
                })
                self.assertRedirects(response, edit_url, fetch_redirect_response=False)
                self.assertEqual(group.interests.count(), 4)
                interest = group.interests.get(text="Representation learning")
                response = self.client.post(reverse("interest_edit", args=[interest.pk]), {"text": "Self-supervised learning"})
                self.assertRedirects(response, edit_url, fetch_redirect_response=False)
                interest.refresh_from_db()
                self.assertEqual(interest.text, "Self-supervised learning")

                response = self.client.post(reverse("interest_delete", args=[interest.pk]))
                self.assertRedirects(response, edit_url, fetch_redirect_response=False)
                self.assertFalse(ResearchInterest.objects.filter(pk=interest.pk).exists())
                response = self.client.post(reverse("group_delete", args=[group.pk]))
                self.assertRedirects(response, reverse("group_list"), fetch_redirect_response=False)
                self.assertFalse(ResearchGroup.objects.filter(pk=group.pk).exists())
                self.assertFalse(ResearchInterest.objects.filter(group_id=group.pk).exists())

    def test_authorized_users_can_add_and_remove_group_members(self):
        target = self.users["no_membership"]
        for role in self.superusers + self.managers:
            with self.subTest(role=role):
                self.sign_in(role)
                response = self.client.post(reverse("group_add_member", args=[self.group.pk]), {"user_ids": [target.pk]})
                self.assertRedirects(response, reverse("group_edit", args=[self.group.pk]), fetch_redirect_response=False)
                self.assertTrue(UserGroupMembership.objects.get(user=target, group=self.group).is_primary)
                response = self.client.post(reverse("group_remove_member", args=[self.group.pk, target.pk]))
                self.assertRedirects(response, reverse("group_edit", args=[self.group.pk]), fetch_redirect_response=False)
                self.assertFalse(UserGroupMembership.objects.filter(user=target, group=self.group).exists())

    def test_ordinary_users_cannot_open_management_pages_directly(self):
        for role in self.ordinary_users:
            self.sign_in(role)
            for url in (reverse("group_list"), reverse("group_edit", args=[self.group.pk])):
                with self.subTest(role=role, url=url):
                    self.assertRedirects(self.client.get(url), reverse("dashboard"), fetch_redirect_response=False)

    def test_ordinary_users_cannot_mutate_groups_interests_or_memberships(self):
        original_memberships = list(UserGroupMembership.objects.order_by("pk").values_list("user_id", "group_id", "is_primary"))
        requests = (
            ("group_create", [], {"name": "Forbidden group", "research_interests": "Forbidden interest"}),
            ("group_edit", [self.group.pk], {"name": "Forbidden rename"}),
            ("interest_add", [self.group.pk], {"text": "Forbidden interest"}),
            ("interest_edit", [self.interest.pk], {"text": "Forbidden edit"}),
            ("interest_delete", [self.interest.pk], {}),
            ("group_add_member", [self.group.pk], {"user_ids": [self.users["no_membership"].pk]}),
            ("group_remove_member", [self.group.pk, self.users["member"].pk], {}),
            ("group_set_primary", [self.group.pk, self.users["member"].pk], {}),
            ("dismiss_pending_user", [self.users["no_membership"].pk], {}),
            ("group_delete", [self.group.pk], {}),
        )
        for role in self.ordinary_users:
            self.sign_in(role)
            for ajax in (False, True):
                headers = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if ajax else {}
                for route, args, data in requests:
                    with self.subTest(role=role, route=route, ajax=ajax):
                        response = self.client.post(reverse(route, args=args), data, **headers)
                        if ajax:
                            self.assertEqual(response.status_code, 403)
                        else:
                            self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.group.refresh_from_db()
        self.interest.refresh_from_db()
        self.assertEqual(self.group.name, "Access regression research group")
        self.assertEqual(self.interest.text, "Original research interest")
        self.assertEqual(ResearchGroup.objects.count(), 1)
        self.assertEqual(ResearchInterest.objects.count(), 1)
        self.assertEqual(list(UserGroupMembership.objects.order_by("pk").values_list("user_id", "group_id", "is_primary")), original_memberships)
        self.assertFalse(PendingAssignmentDismissal.objects.exists())

    def test_user_administration_is_reserved_to_superusers(self):
        for role in self.superusers + self.managers + self.ordinary_users:
            with self.subTest(role=role):
                self.sign_in(role)
                response = self.client.get(reverse("user_admin_list"))
                if role in self.superusers:
                    self.assertContains(response, self.users["member"].username)
                else:
                    self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_group_managers_and_ordinary_users_cannot_change_user_roles(self):
        target = self.users["no_membership"]
        for role in self.managers + self.ordinary_users:
            self.sign_in(role)
            for route in ("user_toggle_superuser", "user_toggle_group_manager", "user_delete"):
                with self.subTest(role=role, route=route):
                    response = self.client.post(reverse(route, args=[target.pk]))
                    self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
                    target.refresh_from_db()
                    self.assertFalse(target.is_superuser)
                    self.assertFalse(target.is_staff)
                    self.assertFalse(target.groups.exists())

    def test_management_submissions_still_require_csrf(self):
        client = self.sign_in("super_no_membership", Client(enforce_csrf_checks=True))
        response = client.get(reverse("group_list"))
        self.assertEqual(response.status_code, 200)
        response = client.post(reverse("group_create"), {"name": "Missing CSRF token"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ResearchGroup.objects.filter(name="Missing CSRF token").exists())

    def test_role_changes_are_visible_in_an_existing_browser_session(self):
        user = self.users["no_membership"]
        self.sign_in("no_membership")
        self.assert_management_navigation(self.client.get(reverse("my_groups")), groups=False, users=False)
        user.groups.add(self.manager_role)
        self.assert_management_navigation(self.client.get(reverse("my_groups")), groups=True, users=False)
        self.assertEqual(self.client.get(reverse("group_list")).status_code, 200)
        user.groups.remove(self.manager_role)
        self.assert_management_navigation(self.client.get(reverse("my_groups")), groups=False, users=False)
        self.assertRedirects(self.client.get(reverse("group_list")), reverse("dashboard"), fetch_redirect_response=False)
        user.is_superuser = True
        user.save(update_fields=["is_superuser"])
        self.assert_management_navigation(self.client.get(reverse("my_groups")), groups=True, users=True)
        self.assertEqual(self.client.get(reverse("user_admin_list")).status_code, 200)

    def test_mutation_only_routes_reject_get_requests(self):
        routes = (
            ("group_create", []),
            ("group_delete", [self.group.pk]),
            ("interest_add", [self.group.pk]),
            ("interest_edit", [self.interest.pk]),
            ("interest_delete", [self.interest.pk]),
        )
        for role in ("super_no_membership", "manager_no_membership"):
            self.sign_in(role)
            for route, args in routes:
                with self.subTest(role=role, route=route):
                    self.assertEqual(self.client.get(reverse(route, args=args)).status_code, 405)
        self.assertTrue(ResearchGroup.objects.filter(pk=self.group.pk).exists())
        self.assertTrue(ResearchInterest.objects.filter(pk=self.interest.pk).exists())


@override_settings(
    SHIBBOLETH_AUTH=False,
    SECURE_SSL_REDIRECT=False,
    ALLOWED_HOSTS=["testserver"],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class PosterGroupScopeTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="scope_owner")
        cls.teammate = User.objects.create_user(username="scope_teammate")
        cls.outsider = User.objects.create_user(username="scope_outsider")
        cls.admin = User.objects.create_user(username="scope_admin", is_superuser=True)

        cls.team = ResearchGroup.objects.create(name="Scope team")
        cls.other_team = ResearchGroup.objects.create(name="Scope other team")
        ResearchInterest.objects.create(group=cls.team, text="Medical imaging")
        for user in (cls.owner, cls.teammate):
            UserGroupMembership.objects.create(user=user, group=cls.team, is_primary=True)
        UserGroupMembership.objects.create(user=cls.outsider, group=cls.other_team, is_primary=True)

        cls.shared = cls._poster("Shared team paper", cls.owner)
        cls.shared.groups.add(cls.team)
        cls.foreign = cls._poster("Outsider paper", cls.outsider)
        cls.foreign.groups.add(cls.other_team)
        cls.ungrouped = cls._poster("Personal upload", cls.owner)

    @staticmethod
    def _poster(title, uploader):
        return ResearchPoster.objects.create(
            title=title, authors="An Author", summary="A summary.",
            validation_status="approved", uploaded_by=uploader,
        )

    def sign_in(self, user):
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

    def read_routes(self, poster):
        return (("poster_detail", [poster.pk]), ("edit_poster", [poster.pk]))

    def write_routes(self, poster):
        return (
            ("update_status", [poster.pk], {"status": "rejected"}),
            ("update_notes", [poster.pk], {"notes": "injected"}),
            ("update_tags", [poster.pk], {"tags": "injected"}),
            ("toggle_favorite", [poster.pk], {}),
            ("retry_analysis", [poster.pk], {}),
            ("stop_analysis", [poster.pk], {}),
            ("update_poster_groups", [poster.pk], {}),
            ("delete_poster", [poster.pk], {}),
        )

    def test_teammate_may_read_and_edit_a_paper_uploaded_by_someone_else(self):
        self.sign_in(self.teammate)
        for route, args in self.read_routes(self.shared):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route, args=args)).status_code, 200)

        response = self.client.post(
            reverse("update_status", args=[self.shared.pk]), {"status": "rejected"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.validation_status, "rejected")

        response = self.client.post(
            reverse("edit_poster", args=[self.shared.pk]),
            {"title": "Renamed by teammate", "authors": "An Author",
             "summary": "A summary.", "category": "other", "validation_status": "approved"},
        )
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.title, "Renamed by teammate")

    def test_teammate_may_annotate_and_requeue_a_paper_uploaded_by_someone_else(self):
        self.sign_in(self.teammate)
        for route, payload, field, expected in (
            ("update_notes", {"notes": "Useful for our work"}, "notes", "Useful for our work"),
            ("update_tags", {"tags": "segmentation"}, "tags", "segmentation"),
        ):
            with self.subTest(route=route):
                response = self.client.post(
                    reverse(route, args=[self.shared.pk]),
                    data=json.dumps(payload), content_type="application/json",
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                )
                self.assertEqual(response.status_code, 200)
                self.shared.refresh_from_db()
                self.assertEqual(getattr(self.shared, field), expected)

        response = self.client.post(
            reverse("toggle_favorite", args=[self.shared.pk]), HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(response.json()["is_favorite"])

        with patch("bot_engine.views.process_poster_task.delay") as queued:
            queued.return_value.id = "task-id"
            response = self.client.post(reverse("retry_analysis", args=[self.shared.pk]))
        self.assertEqual(response.status_code, 200)
        queued.assert_called_once()
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.analysis_status, "processing")

        response = self.client.post(reverse("stop_analysis", args=[self.shared.pk]))
        self.assertEqual(response.status_code, 200)
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.analysis_status, "failed")

    def test_teammate_may_delete_a_paper_uploaded_by_someone_else(self):
        victim = self._poster("Deletable team paper", self.owner)
        victim.groups.add(self.team)
        self.sign_in(self.teammate)
        response = self.client.post(
            reverse("delete_poster", args=[victim.pk]), HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ResearchPoster.objects.filter(pk=victim.pk).exists())

    def test_outsider_cannot_reach_a_paper_from_another_group(self):
        self.sign_in(self.outsider)
        for route, args in self.read_routes(self.shared):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route, args=args)).status_code, 404)
        for route, args, payload in self.write_routes(self.shared):
            with self.subTest(route=route):
                self.assertEqual(self.client.post(reverse(route, args=args), payload).status_code, 404)
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.title, "Shared team paper")
        self.assertEqual(self.shared.validation_status, "approved")
        self.assertTrue(ResearchPoster.objects.filter(pk=self.shared.pk).exists())
        self.assertEqual(set(self.shared.groups.values_list("pk", flat=True)), {self.team.pk})

    def test_uploader_keeps_access_to_a_paper_with_no_group(self):
        self.sign_in(self.owner)
        self.assertEqual(self.client.get(reverse("poster_detail", args=[self.ungrouped.pk])).status_code, 200)
        self.sign_in(self.outsider)
        self.assertEqual(self.client.get(reverse("poster_detail", args=[self.ungrouped.pk])).status_code, 404)

    def test_superuser_reaches_every_paper(self):
        self.sign_in(self.admin)
        for poster in (self.shared, self.foreign, self.ungrouped):
            with self.subTest(poster=poster.title):
                self.assertEqual(self.client.get(reverse("poster_detail", args=[poster.pk])).status_code, 200)

    def test_bulk_actions_ignore_papers_outside_the_caller_groups(self):
        self.sign_in(self.teammate)
        response = self.client.post(
            reverse("bulk_action"),
            data=json.dumps({"ids": [self.shared.pk, self.foreign.pk], "action": "rejected"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "1 papers set to Rejected")
        self.shared.refresh_from_db()
        self.foreign.refresh_from_db()
        self.assertEqual(self.shared.validation_status, "rejected")
        self.assertEqual(self.foreign.validation_status, "approved")

    def test_bulk_delete_cannot_remove_papers_from_another_group(self):
        self.sign_in(self.teammate)
        response = self.client.post(
            reverse("bulk_action"),
            data=json.dumps({"ids": [self.foreign.pk], "action": "delete"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "0 papers deleted")
        self.assertTrue(ResearchPoster.objects.filter(pk=self.foreign.pk).exists())

    def test_exports_only_contain_papers_from_the_caller_groups(self):
        self.sign_in(self.teammate)
        titles = [item["title"] for item in self.client.get(reverse("export_approved_json")).json()["items"]]
        self.assertIn("Shared team paper", titles)
        self.assertNotIn("Outsider paper", titles)
        csv_body = self.client.get(reverse("export_approved_csv")).content.decode("utf-8-sig")
        self.assertIn("Shared team paper", csv_body)
        self.assertNotIn("Outsider paper", csv_body)

    def test_group_evaluation_is_refused_for_groups_the_caller_is_not_in(self):
        self.foreign.groups.add(self.team)
        self.sign_in(self.teammate)
        url = reverse("poster_why_useful_for_group", args=[self.foreign.pk])
        self.assertEqual(self.client.get(url, {"group_id": self.other_team.pk}).status_code, 403)
        self.sign_in(self.outsider)
        self.assertEqual(
            self.client.get(reverse("poster_why_useful_for_group", args=[self.shared.pk])).status_code, 404,
        )

    def assign_groups(self, poster, group_ids):
        return self.client.post(
            reverse("update_poster_groups", args=[poster.pk]),
            data=json.dumps({"group_ids": group_ids}),
            content_type="application/json",
        )

    def test_a_caller_cannot_add_a_paper_to_a_group_they_do_not_belong_to(self):
        self.sign_in(self.teammate)
        response = self.assign_groups(self.shared, [self.other_team.pk])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(self.shared.groups.values_list("pk", flat=True)), {self.team.pk})

    def test_a_caller_may_detach_their_own_group_but_never_a_foreign_one(self):
        self.foreign.groups.add(self.team)
        self.sign_in(self.teammate)
        response = self.assign_groups(self.foreign, [self.other_team.pk])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(self.foreign.groups.values_list("pk", flat=True)), {self.other_team.pk})
        self.assertEqual([g["id"] for g in response.json()["user_groups"]], [])

    def test_clearing_the_activity_log_is_reserved_to_managers(self):
        ActivityLog.objects.create(action="created", poster_title="Shared team paper")
        for user in (self.owner, self.teammate, self.outsider):
            with self.subTest(user=user.username):
                self.sign_in(user)
                self.assertRedirects(
                    self.client.post(reverse("delete_all_activities")),
                    reverse("dashboard"), fetch_redirect_response=False,
                )
        self.assertTrue(ActivityLog.objects.exists())
        self.sign_in(self.admin)
        self.assertRedirects(
            self.client.post(reverse("delete_all_activities")),
            reverse("dashboard"), fetch_redirect_response=False,
        )
        self.assertFalse(ActivityLog.objects.exists())
