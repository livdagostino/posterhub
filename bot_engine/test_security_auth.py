import hashlib
import hmac
import json
from unittest.mock import patch

from django.contrib.auth import SESSION_KEY, get_user_model
from django.http import JsonResponse
from django.test import Client, TestCase, override_settings
from django.urls import re_path, reverse

from . import views
from .middleware import ShibbolethBackend


SHIBBOLETH_BACKEND = "bot_engine.middleware.ShibbolethBackend"


def identity_probe(request):
    return JsonResponse({
        "authenticated": request.user.is_authenticated,
        "username": request.user.get_username(),
    })


urlpatterns = [re_path(r"^.*$", identity_probe)]


@override_settings(
    ROOT_URLCONF=__name__,
    SHIBBOLETH_AUTH=True,
    AUTHENTICATION_BACKENDS=[SHIBBOLETH_BACKEND],
    ALLOWED_HOSTS=["testserver"],
    SECURE_SSL_REDIRECT=False,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class ShibbolethSecurityTests(TestCase):
    session_only_paths = (
        "/dashboard/live-status", "/dashboard/live-status/", "/dashboard/live-status-extra/",
        "/task-status", "/task-status/task-id/", "/task-status-extra/",
        "/api/", "/api/conference-search/",
        "/media/", "/media/posters/example.jpg",
        "/static/", "/static/missing-security-probe.css",
        "/telegram-webhook", "/telegram-webhook/", "/telegram-webhook-extra/",
        "/whatsapp-webhook", "/whatsapp-webhook/", "/whatsapp-webhook-extra/",
        "/Shibboleth.sso", "/Shibboleth.sso/Login",
    )

    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.admin = cls.User.objects.create_user(username="security-admin", is_superuser=True)
        cls.member = cls.User.objects.create_user(username="security-member")
        cls.inactive = cls.User.objects.create_user(
            username="security-inactive", is_active=False, is_superuser=True,
        )

    def assert_anonymous(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["authenticated"])

    def test_forged_admin_headers_cannot_create_sessions_on_unprotected_routes(self):
        for path in self.session_only_paths:
            for method in ("get", "post"):
                with self.subTest(path=path, method=method):
                    client = Client()
                    response = getattr(client, method)(path, HTTP_X_SHIB_UID=self.admin.username)
                    self.assert_anonymous(response)
                    self.assertNotIn("sessionid", response.cookies)
                    self.assertNotIn(SESSION_KEY, client.session)

    def test_unprotected_routes_cannot_provision_users_from_identity_headers(self):
        username = "forged-new-user"
        for path in self.session_only_paths:
            with self.subTest(path=path):
                response = self.client.get(
                    path, HTTP_X_SHIB_UID=username, HTTP_X_SHIB_MAIL="forged@example.test",
                )
                self.assert_anonymous(response)
                self.assertFalse(self.User.objects.filter(username=username).exists())

    def test_protected_route_accepts_legitimate_sso_identity(self):
        response = self.client.get("/groups/", HTTP_X_SHIB_UID=self.admin.username)
        self.assertEqual(response.json(), {"authenticated": True, "username": self.admin.username})
        self.assertEqual(self.client.session[SESSION_KEY], str(self.admin.pk))

    def test_protected_route_provisions_the_sso_profile(self):
        response = self.client.get(
            "/", HTTP_X_SHIB_UID=" new-sso-user ",
            HTTP_X_SHIB_MAIL=" new@example.test ",
            HTTP_X_SHIB_CN="New Sso User", HTTP_X_SHIB_GIVENNAME=" New ",
            HTTP_X_SHIB_SN=" Sso User ",
        )
        self.assertEqual(response.json(), {"authenticated": True, "username": "new-sso-user"})
        user = self.User.objects.get(username="new-sso-user")
        self.assertEqual((user.email, user.first_name, user.last_name),
                         ("new@example.test", "New", "Sso User"))
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.has_usable_password())

    def test_existing_session_is_preserved_on_unprotected_routes(self):
        self.client.force_login(self.member, backend=SHIBBOLETH_BACKEND)
        for path in self.session_only_paths:
            with self.subTest(path=path):
                response = self.client.get(path, HTTP_X_SHIB_UID=self.admin.username)
                self.assertEqual(response.json(), {"authenticated": True, "username": self.member.username})
                self.assertEqual(self.client.session[SESSION_KEY], str(self.member.pk))

    def test_existing_session_works_on_api_without_sso_headers(self):
        self.client.force_login(self.member, backend=SHIBBOLETH_BACKEND)
        response = self.client.get("/api/conference-search/")
        self.assertEqual(response.json(), {"authenticated": True, "username": self.member.username})

    def test_inactive_sso_user_is_refused_without_duplicate_provisioning(self):
        count = self.User.objects.count()
        response = self.client.get("/groups/", HTTP_X_SHIB_UID=self.inactive.username)
        self.assert_anonymous(response)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(self.User.objects.count(), count)
        self.inactive.refresh_from_db()
        self.assertFalse(self.inactive.is_active)

    def test_backend_authenticate_refuses_inactive_users(self):
        count = self.User.objects.count()
        self.assertIsNone(ShibbolethBackend().authenticate(None, shib_uid=self.inactive.username))
        self.assertEqual(self.User.objects.count(), count)

    def test_backend_preserves_active_authentication_and_provisioning(self):
        backend = ShibbolethBackend()
        self.assertEqual(backend.authenticate(None, shib_uid=self.member.username), self.member)
        new_user = backend.authenticate(None, shib_uid="backend-new-user")
        self.assertTrue(new_user.is_active)
        self.assertEqual(backend.get_user(new_user.pk), new_user)
        self.assertIsNone(backend.authenticate(None))
        self.assertIsNone(backend.get_user(-1))

    def test_backend_get_user_refuses_inactive_users(self):
        self.assertIsNone(ShibbolethBackend().get_user(self.inactive.pk))

    def test_disabling_user_revokes_existing_sso_session_access(self):
        self.client.force_login(self.member, backend=SHIBBOLETH_BACKEND)
        self.User.objects.filter(pk=self.member.pk).update(is_active=False)
        for path in ("/groups/", "/api/conference-search/"):
            with self.subTest(path=path):
                response = self.client.get(path, HTTP_X_SHIB_UID=self.member.username)
                self.assert_anonymous(response)

    @override_settings(SHIBBOLETH_AUTH=False)
    def test_disabled_sso_does_not_trust_headers_on_protected_routes(self):
        response = self.client.get("/groups/", HTTP_X_SHIB_UID=self.admin.username)
        self.assert_anonymous(response)


@override_settings(ALLOWED_HOSTS=["testserver"], SHIBBOLETH_AUTH=False, SECURE_SSL_REDIRECT=False)
class WebhookAuthenticationTests(TestCase):
    telegram_payload = json.dumps({"update_id": 1})
    whatsapp_payload = json.dumps({"entry": [{"changes": [{"value": {}}]}]})

    def post_telegram(self, **headers):
        return self.client.post(
            reverse("telegram_webhook"), data=self.telegram_payload,
            content_type="application/json", **headers,
        )

    def post_whatsapp(self, **headers):
        return self.client.post(
            reverse("whatsapp_webhook"), data=self.whatsapp_payload,
            content_type="application/json", **headers,
        )

    def sign(self, secret, body):
        return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    @override_settings(TELEGRAM_WEBHOOK_SECRET="expected-telegram-secret")
    def test_telegram_rejects_calls_without_the_configured_secret(self):
        self.assertEqual(self.post_telegram().status_code, 403)
        self.assertEqual(
            self.post_telegram(HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="wrong").status_code, 403,
        )
        self.assertEqual(
            self.post_telegram(HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="expected-telegram-secret").status_code, 200,
        )

    @override_settings(TELEGRAM_WEBHOOK_SECRET="")
    def test_telegram_stays_open_when_no_secret_is_configured(self):
        self.assertEqual(self.post_telegram().status_code, 200)

    @override_settings(WHATSAPP_APP_SECRET="expected-app-secret")
    def test_whatsapp_rejects_unsigned_and_forged_payloads(self):
        self.assertEqual(self.post_whatsapp().status_code, 403)
        self.assertEqual(self.post_whatsapp(HTTP_X_HUB_SIGNATURE_256="sha256=deadbeef").status_code, 403)
        self.assertEqual(self.post_whatsapp(HTTP_X_HUB_SIGNATURE_256="not-a-signature").status_code, 403)
        forged = self.sign("expected-app-secret", '{"entry": []}')
        self.assertEqual(self.post_whatsapp(HTTP_X_HUB_SIGNATURE_256=forged).status_code, 403)

    @override_settings(WHATSAPP_APP_SECRET="expected-app-secret")
    def test_whatsapp_accepts_a_correctly_signed_payload(self):
        signature = self.sign("expected-app-secret", self.whatsapp_payload)
        self.assertEqual(self.post_whatsapp(HTTP_X_HUB_SIGNATURE_256=signature).status_code, 200)

    @override_settings(WHATSAPP_APP_SECRET="")
    def test_whatsapp_stays_open_when_no_app_secret_is_configured(self):
        self.assertEqual(self.post_whatsapp().status_code, 200)

    def test_whatsapp_subscription_handshake_checks_the_verify_token(self):
        with patch.object(views, "WHATSAPP_VERIFY_TOKEN", "expected-verify-token"):
            url = reverse("whatsapp_webhook")
            response = self.client.get(url, {
                "hub.mode": "subscribe", "hub.verify_token": "expected-verify-token",
                "hub.challenge": "challenge-value",
            })
            self.assertEqual(response.content, b"challenge-value")
            for params in (
                {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "c"},
                {"hub.mode": "unsubscribe", "hub.verify_token": "expected-verify-token", "hub.challenge": "c"},
                {"hub.challenge": "c"},
            ):
                with self.subTest(params=params):
                    self.assertEqual(self.client.get(url, params).status_code, 403)

    def test_whatsapp_handshake_is_refused_when_no_verify_token_is_configured(self):
        with patch.object(views, "WHATSAPP_VERIFY_TOKEN", ""):
            response = self.client.get(reverse("whatsapp_webhook"), {
                "hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "c",
            })
            self.assertEqual(response.status_code, 403)
