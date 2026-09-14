import csv
import io
from codecs import BOM_UTF8
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escapejs

from .models import ResearchGroup, ResearchPoster, UserGroupMembership


class _ButtonHandlers(HTMLParser):
    def __init__(self):
        super().__init__()
        self.handlers = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "button" and attrs.get("class") == "subfield-tag-remove":
            self.handlers.append(attrs.get("onclick", ""))


@override_settings(ALLOWED_HOSTS=["testserver"])
class SecurityOutputTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="output_security_member")
        cls.group = ResearchGroup.objects.create(name="Output safety")
        UserGroupMembership.objects.create(user=cls.user, group=cls.group, is_primary=True)
        cls.poster = ResearchPoster.objects.create(
            title="Original title", authors="Research Author", summary="A summary.",
            validation_status="approved", uploaded_by=cls.user,
        )
        cls.poster.groups.add(cls.group)

    def setUp(self):
        self.client.force_login(self.user)
        self.edit_url = reverse("edit_poster", args=[self.poster.pk])
        self.form_data = {
            "title": "Updated title", "authors": "Research Author", "summary": "A summary.",
            "category": "other", "validation_status": "approved",
        }

    def test_edit_rejects_external_and_unsafe_next_urls_after_saving(self):
        targets = (
            "https://evil.example/", "//evil.example/", "///evil.example/",
            r"/\evil.example/", "javascript:alert(1)", "http://testserver/dashboard/",
            "https://testserver@evil.example/",
        )
        for target in targets:
            with self.subTest(target=target):
                response = self.client.post(self.edit_url, {**self.form_data, "next": target}, secure=True)
                self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.poster.refresh_from_db()
        self.assertEqual(self.poster.title, "Updated title")

    def test_edit_preserves_local_targets_and_https_same_host(self):
        for target in ("/dashboard/?status=approved&search=vision", "https://testserver/my-groups/"):
            with self.subTest(target=target):
                response = self.client.post(self.edit_url, {**self.form_data, "next": target}, secure=True)
                self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_edit_validates_query_next_and_referer_before_rendering(self):
        for kwargs in (
            {"data": {"next": "https://evil.example/"}},
            {"HTTP_REFERER": "https://evil.example/"},
        ):
            with self.subTest(kwargs=kwargs):
                response = self.client.get(self.edit_url, secure=True, **kwargs)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["next_url"], "")
        response = self.client.get(
            self.edit_url, HTTP_REFERER="https://testserver/dashboard/?status=approved", secure=True,
        )
        self.assertEqual(response.context["next_url"], "https://testserver/dashboard/?status=approved")

    def test_edit_keeps_safe_next_when_validation_fails(self):
        response = self.client.post(
            self.edit_url, {"title": "", "next": "/dashboard/?search=vision"}, secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["next_url"], "/dashboard/?search=vision")

    def test_csv_neutralizes_formulas_without_changing_json_or_database(self):
        values = {
            "title": '=HYPERLINK("https://example.invalid";"paper")',
            "authors": "+SUM(1;2)", "tags": "@SUM(1;2)",
            "summary": "  =1+1", "why_useful": "\t=1+1",
        }
        ResearchPoster.objects.filter(pk=self.poster.pk).update(**values)
        response = self.client.get(reverse("export_approved_csv"))
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
        row = dict(zip(rows[0], rows[1]))
        for column, field in (
            ("Title", "title"), ("Authors", "authors"), ("Tags", "tags"),
            ("Summary", "summary"), ("Why Useful", "why_useful"),
        ):
            self.assertEqual(row[column], "'" + values[field])
        item = self.client.get(reverse("export_approved_json")).json()["items"][0]
        self.poster.refresh_from_db()
        exported_as = {"summary": "description"}
        for field, value in values.items():
            self.assertEqual(item[exported_as.get(field, field)], value)
            self.assertEqual(getattr(self.poster, field), value)

    def test_csv_carries_a_single_byte_order_mark_for_spreadsheets(self):
        response = self.client.get(reverse("export_approved_csv"))
        self.assertEqual(response.status_code, 200)
        body = response.content
        self.assertTrue(body.startswith(BOM_UTF8))
        self.assertEqual(body.count(BOM_UTF8), 1)
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[0][0], "ID")
        self.assertEqual(rows[1][0], str(self.poster.pk))

    def test_csv_preserves_ordinary_text_and_escapes_other_formula_prefixes(self):
        for title in ("-1+1", "\r=1+1", "\n=1+1", "Paper; title with commas, and quotes \"ok\""):
            with self.subTest(title=title):
                ResearchPoster.objects.filter(pk=self.poster.pk).update(title=title)
                response = self.client.get(reverse("export_approved_csv"))
                rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
                expected = title if title.startswith("Paper") else "'" + title
                self.assertEqual(rows[1][1], expected)
                self.assertEqual(rows[1][0], str(self.poster.pk))

    def test_dashboard_subfield_removal_cannot_break_out_of_javascript_string(self):
        payload = "');globalThis.__output_xss=1;//"
        response = self.client.get(reverse("dashboard"), {"subfield": payload})
        self.assertEqual(response.status_code, 200)
        parser = _ButtonHandlers()
        parser.feed(response.content.decode())
        self.assertEqual(parser.handlers, [f"removeSubfield('{escapejs(payload)}')"])
        self.assertNotIn("');globalThis", parser.handlers[0])

    def test_dashboard_preserves_normal_subfield_removal(self):
        response = self.client.get(reverse("dashboard"), {"subfield": "machine_learning"})
        parser = _ButtonHandlers()
        parser.feed(response.content.decode())
        self.assertEqual(parser.handlers, ["removeSubfield('machine_learning')"])
