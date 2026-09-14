from django.conf import settings
from django.contrib.auth import login, get_user_model
from django.contrib.auth.backends import BaseBackend

User = get_user_model()

SHIBBOLETH_SESSION_ONLY_PATH_PREFIXES = (
    "/dashboard/live-status",
    "/task-status",
    "/api/",
    "/media/",
    "/static/",
    "/telegram-webhook",
    "/whatsapp-webhook",
    "/Shibboleth.sso",
)


class ShibbolethBackend(BaseBackend):
    def authenticate(self, request, shib_uid=None, **kwargs):
        if not shib_uid:
            return None
        user, _ = User.objects.get_or_create(username=shib_uid, defaults={"is_active": True})
        return user if user.is_active else None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id, is_active=True)
        except User.DoesNotExist:
            return None


class ShibbolethMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not getattr(settings, "SHIBBOLETH_AUTH", False):
            return self.get_response(request)

        if request.path_info.startswith(SHIBBOLETH_SESSION_ONLY_PATH_PREFIXES):
            return self.get_response(request)

        uid = request.META.get("HTTP_X_SHIB_UID", "").strip()
        if uid and not request.user.is_authenticated:
            user = User.objects.filter(username=uid).first() or self._create_from_shib(request, uid)
            if user.is_active:
                login(request, user, backend="bot_engine.middleware.ShibbolethBackend")

        return self.get_response(request)

    @staticmethod
    def _create_from_shib(request, uid):
        meta = request.META
        full_name = meta.get("HTTP_X_SHIB_CN", "").strip()
        first = meta.get("HTTP_X_SHIB_GIVENNAME", "").strip()
        last = meta.get("HTTP_X_SHIB_SN", "").strip()

        parts = full_name.split() if full_name else []
        first = first or (parts[0] if parts else "")
        last = last or (" ".join(parts[1:]) if len(parts) > 1 else "")

        return User.objects.create_user(
            username=uid,
            email=meta.get("HTTP_X_SHIB_MAIL", "").strip(),
            first_name=first,
            last_name=last,
        )
