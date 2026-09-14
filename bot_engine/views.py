import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
from codecs import BOM_UTF8
from functools import wraps

import requests
from celery import current_app as celery_app
from celery.result import AsyncResult
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIF_AVAILABLE = True
except ImportError:
    _HEIF_AVAILABLE = False

from .access import (
    accessible_posters, can_access_group, get_accessible_poster_or_404,
    is_group_manager, user_can_interact,
)
from .forms import PosterUploadForm, PosterEditForm
from .models import (
    ResearchPoster, ActivityLog, Favorite,
    ResearchGroup, ResearchInterest, UserGroupMembership, PosterGroupWhyUseful,
    BotAccount, PendingAssignmentDismissal,
)
from .utils_ai import analyze_and_enrich, generate_why_useful
from .tasks import process_poster_task, process_bot_poster_task, download_and_handle_media_task

logger = logging.getLogger(__name__)


def _is_ajax(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _remove_poster_image(poster):
    if not poster.image:
        return
    try:
        path = poster.image.path
    except (NotImplementedError, ValueError):
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as e:
        logger.warning("Could not remove image for poster %s: %s", poster.pk, type(e).__name__)


def _forbidden(request, redirect_to="dashboard"):
    if _is_ajax(request):
        return JsonResponse({"error": "Forbidden"}, status=403)
    return redirect(redirect_to)


def _gate(predicate, on_fail=None):
    on_fail = on_fail or (lambda req: _forbidden(req))

    def decorator(view_func):
        @wraps(view_func)
        @login_required(login_url="login")
        def wrapper(request, *args, **kwargs):
            if not predicate(request.user):
                return on_fail(request)
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def _no_groups_response(request):
    msg = "Your account is not in any research group yet. Ask an administrator to add you before using this feature."
    if _is_ajax(request) or request.method != "GET":
        return JsonResponse({"error": "no_groups", "message": msg}, status=403)
    messages.warning(request, msg)
    return redirect("dashboard")


_groups_required = _gate(user_can_interact, on_fail=_no_groups_response)
_admin_required = _gate(is_group_manager)
_superuser_required = _gate(lambda u: u.is_superuser)

BOT_TOKEN             = settings.BOT_TOKEN
WHATSAPP_TOKEN        = settings.WHATSAPP_TOKEN
WHATSAPP_PHONE_ID     = settings.WHATSAPP_PHONE_ID
WHATSAPP_VERIFY_TOKEN = settings.WHATSAPP_VERIFY_TOKEN

BOT_STATE_TTL     = 7200
WELCOMED_TTL      = 86400
BOT_RATELIMIT_TTL = 300


def _get_pending(platform, recipient):
    return cache.get(f"bot:{platform}:{recipient}")

def _set_pending(platform, recipient, data):
    cache.set(f"bot:{platform}:{recipient}", data, timeout=BOT_STATE_TTL)

def _del_pending(platform, recipient):
    cache.delete(f"bot:{platform}:{recipient}")

def _is_welcomed(platform, recipient):
    return bool(cache.get(f"bot:welcomed:{platform}:{recipient}"))

def _set_welcomed(platform, recipient):
    cache.set(f"bot:welcomed:{platform}:{recipient}", 1, timeout=WELCOMED_TTL)

def _check_ratelimit(platform, recipient):
    return bool(cache.get(f"bot:ratelimit:{platform}:{recipient}"))

def _set_ratelimit(platform, recipient):
    cache.set(f"bot:ratelimit:{platform}:{recipient}", 1, timeout=BOT_RATELIMIT_TTL)

def _clear_ratelimit(platform, recipient):
    cache.delete(f"bot:ratelimit:{platform}:{recipient}")


def _get_bot_user(platform, recipient):
    acc = (
        BotAccount.objects
        .filter(platform=platform, recipient=str(recipient))
        .select_related("user")
        .first()
    )
    return acc.user if acc else None


def _link_bot_account(platform, recipient, email):
    User = get_user_model()
    email = (email or "").strip().lower()
    if not email:
        return None
    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if not user:
        return None
    BotAccount.objects.update_or_create(
        platform=platform,
        recipient=str(recipient),
        defaults={"user": user},
    )
    return user


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LINK_CMD_RE_TELEGRAM = re.compile(r"^/link(?:@\w+)?\b\s*(.*)$", re.IGNORECASE)
_LINK_CMD_RE_WHATSAPP = re.compile(r"^link\b\s*(.*)$", re.IGNORECASE)


def _extract_link_payload(text, platform):
    if not text:
        return None
    pattern = _LINK_CMD_RE_TELEGRAM if platform == "telegram" else _LINK_CMD_RE_WHATSAPP
    m = pattern.match(text.strip())
    return m.group(1).strip() if m else None


def _handle_link_command(platform, recipient, payload):
    email = (payload or "").strip()
    if not email or not EMAIL_RE.match(email):
        send_message(platform, recipient, MESSAGE_TEMPLATES["link_bad_format"][platform])
        return True

    user = _link_bot_account(platform, recipient, email)
    if not user:
        send_message(platform, recipient, MESSAGE_TEMPLATES["link_no_match"][platform])
        return True

    primary = (
        UserGroupMembership.objects
        .filter(user=user, is_primary=True)
        .select_related("group")
        .first()
    )
    if primary:
        _bold, italic, _link = _fmt(platform)
        group_line = f"Primary group: {italic(primary.group.name)}\n"
    else:
        group_line = MESSAGE_TEMPLATES["link_no_group"][platform] + "\n"

    display_name = user.get_full_name() or user.username
    send_message(
        platform, recipient,
        MESSAGE_TEMPLATES["link_ok"][platform].format(name=display_name, group_line=group_line),
    )
    return True


def _require_link_or_notify(platform, recipient):
    user = _get_bot_user(platform, recipient)
    if user is None:
        send_message(platform, recipient, MESSAGE_TEMPLATES["link_required"][platform])
    return user


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect("dashboard")
    return render(request, "login.html", {"form": form})


def logout_view(request):
    logout(request)
    if getattr(settings, "SHIBBOLETH_AUTH", False):
        return redirect(
            "https://services-host.ing.unimore.it/Shibboleth.sso/Logout"
            "?return=https://posterhub.ing.unimore.it/login/"
        )
    return redirect("login")


MEDIA_DOWNLOAD_TIMEOUT = (5, 20)
MAX_BOT_MEDIA_BYTES = PosterUploadForm.MAX_UPLOAD_SIZE


def _download_media_bytes(url, headers=None):
    with requests.get(url, headers=headers, timeout=MEDIA_DOWNLOAD_TIMEOUT, stream=True) as response:
        if response.status_code != 200:
            logger.warning("Media download returned HTTP %s", response.status_code)
            return None
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > MAX_BOT_MEDIA_BYTES:
                logger.warning("Media download exceeds the %d byte limit", MAX_BOT_MEDIA_BYTES)
                return None
        return bytes(body)


def download_telegram_file(file_id):
    try:
        info = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=MEDIA_DOWNLOAD_TIMEOUT,
        ).json()
        file_path = info["result"]["file_path"]
        return _download_media_bytes(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        logger.warning("Telegram download failed for %s: %s", file_id, type(e).__name__)
        return None


def download_whatsapp_media(media_id):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        info = requests.get(
            f"https://graph.facebook.com/v21.0/{media_id}",
            headers=headers,
            timeout=MEDIA_DOWNLOAD_TIMEOUT,
        ).json()
        media_url = info.get("url")
        if not media_url:
            return None
        return _download_media_bytes(media_url, headers=headers)
    except (requests.RequestException, ValueError, AttributeError, TypeError) as e:
        logger.warning("WhatsApp download failed for %s: %s", media_id, type(e).__name__)
        return None


def get_file_extension(filename, mime_type=None):
    if filename and "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return {
        "image/jpeg": "jpg",
        "image/png":  "png",
        "image/gif":  "gif",
        "image/webp": "webp",
        "image/heic": "heic",
        "image/heif": "heif",
        "application/pdf": "pdf",
    }.get(mime_type, "jpg")


def _convert_heic_to_jpeg(file_content, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("heic", "heif") or not _HEIF_AVAILABLE:
        return file_content, filename
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(file_content)) as img:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=95)
            new_name = filename.rsplit(".", 1)[0] + ".jpg"
            return buf.getvalue(), new_name
    except (UnidentifiedImageError, OSError, ValueError) as e:
        logger.warning("HEIC→JPEG conversion failed for %s: %s", filename, type(e).__name__)
        return file_content, filename


MESSAGE_TEMPLATES = {
    "welcome": {
        "telegram": (
            "<b>Welcome to Research Paper Assistant</b>\n\n"
            "🔒 <b>First step:</b> link your account\n"
            "<code>/link your@email.com</code>\n\n"
            "Then send a photo of a research poster and I will:\n"
            "  • Extract title, authors and metadata\n"
            "  • Find the published paper link\n"
            "  • Search for the GitHub repository\n"
            "  • Generate a summary with GPT-4o\n"
            "  • Route it to your primary group\n\n"
            "📎 <b>Quick tip:</b> Add notes and tags in the caption:\n"
            "<code>note: interesting method  tag: mri, segmentation</code>\n\n"
            "Type /help for all commands and tips."
        ),
        "whatsapp": (
            "*Welcome to Research Paper Assistant*\n\n"
            "🔒 *First step:* link your account\n"
            "`link your@email.com`\n\n"
            "Then send a photo of a research poster and I will:\n"
            "  • Extract title, authors and metadata\n"
            "  • Find the published paper link\n"
            "  • Search for the GitHub repository\n"
            "  • Generate a summary\n"
            "  • Route it to your primary group\n\n"
            "📎 *Quick tip:* Add notes and tags in the caption:\n"
            "`note: interesting method  tag: mri, segmentation`\n\n"
            "Type *help* for all commands and tips."
        ),
    },
    "help": {
        "telegram": (
            "<b>📖 Research Paper Assistant</b>\n\n"

            "<b>How to upload:</b>\n"
            "Send a photo or document of a research poster.\n"
            "The AI will extract metadata, find the paper link, "
            "and search for a GitHub repo.\n\n"

            "<b>📷 Tips for best results:</b>\n"
            "  • Use high-res images with readable text\n"
            "  • Include the full poster (title + authors + abstract)\n"
            "  • Avoid blurry or rotated photos\n"
            "  • JPG/PNG work best\n\n"

            "<b>📎 Add info via caption:</b>\n"
            "<code>note: your text  tag: deep learning, mri</code>\n\n"

            "<b>🔍 Search your collection:</b>\n"
            "  /search &lt;keyword&gt;\n"
            "Searches title, authors, summary, tags, subfields and category.\n\n"
            "<b>Year filter:</b>\n"
            "  /search transformer year:2024\n"
            "  /search segmentation year:2020-2024\n\n"

            "<b>All commands:</b>\n"
            "/start — Welcome message\n"
            "/link &lt;email&gt; — Link your PosterHub account\n"
            "/help — This help page\n"
            "/dashboard — Open your collection\n"
            "/search &lt;keyword&gt; — Search papers"
        ),
        "whatsapp": (
            "*📖 Research Paper Assistant*\n\n"

            "*How to upload:*\n"
            "Send a photo or document of a research poster.\n"
            "The AI will extract metadata, find the paper link, "
            "and search for a GitHub repo.\n\n"

            "*📷 Tips for best results:*\n"
            "  • Use high-res images with readable text\n"
            "  • Include the full poster (title + authors + abstract)\n"
            "  • Avoid blurry or rotated photos\n"
            "  • JPG/PNG work best\n\n"

            "*📎 Add info via caption:*\n"
            "`note: your text  tag: deep learning, mri`\n\n"

            "*🔍 Search your collection:*\n"
            "  search <keyword>\n"
            "Searches title, authors, summary, tags, subfields and category.\n\n"
            "*Year filter:*\n"
            "  search transformer year:2024\n"
            "  search segmentation year:2020-2024\n\n"

            "*All commands:*\n"
            "*link <email>* — Link your PosterHub account\n"
            "*help* — This help page\n"
            "*dashboard* — Open your collection\n"
            "*search <keyword>* — Search papers"
        ),
    },
    "link_required": {
        "telegram": (
            "🔒 <b>Account not linked</b>\n\n"
            "Before uploading, link this chat to your PosterHub account:\n"
            "<code>/link your@email.com</code>\n\n"
            "Use the email registered on the website."
        ),
        "whatsapp": (
            "🔒 *Account not linked*\n\n"
            "Before uploading, link this chat to your PosterHub account:\n"
            "`link your@email.com`\n\n"
            "Use the email registered on the website."
        ),
    },
    "link_ok": {
        "telegram": (
            "✅ <b>Account linked.</b>\n"
            "Signed in as <i>{name}</i>.\n"
            "{group_line}"
            "You can now send posters."
        ),
        "whatsapp": (
            "✅ *Account linked.*\n"
            "Signed in as _{name}_.\n"
            "{group_line}"
            "You can now send posters."
        ),
    },
    "link_no_match": {
        "telegram": "❌ <b>Email not recognized.</b>\nMake sure it matches the address on your PosterHub account.",
        "whatsapp": "❌ *Email not recognized.* Make sure it matches the address on your PosterHub account.",
    },
    "link_bad_format": {
        "telegram": "⚠️ Invalid email. Usage: <code>/link your@email.com</code> (or <code>link your@email.com</code>)",
        "whatsapp": "⚠️ Invalid email. Usage: `link your@email.com` (or `/link your@email.com`)",
    },
    "link_no_group": {
        "telegram": "ℹ️ No primary group set — ask an admin to assign you to a group, or your uploads won't be routed to any group.",
        "whatsapp": "ℹ️ No primary group set — ask an admin to assign you to a group, or your uploads won't be routed to any group.",
    },
    "no_text": {
        "telegram": (
            "⚠️ <b>No research content detected</b>\n\n"
            "The image doesn't appear to contain a research poster.\n\n"
            "<b>Make sure:</b>\n"
            "  • The full poster is visible (title, authors, abstract)\n"
            "  • Text is sharp and readable\n"
            "  • The image is not rotated or cropped"
        ),
        "whatsapp": (
            "⚠️ *No research content detected*\n\n"
            "The image doesn't appear to contain a research poster.\n\n"
            "*Make sure:*\n"
            "  • The full poster is visible (title, authors, abstract)\n"
            "  • Text is sharp and readable\n"
            "  • The image is not rotated or cropped"
        ),
    },
    "analysis_failed": {
        "telegram": (
            "❌ <b>Analysis failed</b>. Check the internet connection.\n\n"
            "The image was saved to your collection.\n"
            "Press 🔄 Retry below when connectivity is restored."
        ),
        "whatsapp": (
            "❌ *Analysis failed*. Check the internet connection.\n\n"
            "The image was saved to your collection.\n"
            "Press 🔄 Retry below when connectivity is restored."
        ),
    },
    "fallback": {
        "telegram": (
            "Send me a photo or document of a research poster.\n"
            "Type /help for commands and upload tips."
        ),
        "whatsapp": (
            "Send me a photo or document of a research poster.\n"
            "Type *help* for commands and upload tips."
        ),
    },
    "download_error": {
        "telegram": "❌ Error downloading file.",
        "whatsapp": "❌ Error downloading file.",
    },
    "ratelimit": {
        "telegram": "⏳ <b>Analysis already in progress.</b>\nPlease wait for the current poster to finish before sending a new one.",
        "whatsapp": "⏳ *Analysis already in progress.*\nPlease wait for the current poster to finish before sending a new one.",
    },
    "analyzing": {
        "telegram": "⏳ <i>Analyzing your poster...</i> This may take a minute.",
        "whatsapp": "⏳ _Analyzing your poster..._ This may take a minute.",
    },
    "duplicate": {
        "telegram": (
            "⚠️ <b>Duplicate detected.</b>\n\n"
            "A paper titled <b>{title}</b> already exists in your collection.\n"
            "{paper_link_line}"
            "The uploaded file has been discarded."
        ),
        "whatsapp": (
            "⚠️ *Duplicate detected.*\n\n"
            "A paper titled *{title}* already exists in your collection.\n"
            "{paper_link_line}"
            "The uploaded file has been discarded."
        ),
    },
    "link_confirmation": {
        "telegram": (
            "🔗 <b>Retrieved paper link:</b>\n"
            "<a href=\"{link}\">{link}</a>\n\n"
            "Is this link correct?"
        ),
        "whatsapp": (
            "🔗 *Retrieved paper link:*\n"
            "{link}\n\n"
            "Is this link correct?"
        ),
    },
    "link_confirmed": {
        "telegram": "✅ <b>Link confirmed.</b> Paper saved to your collection.",
        "whatsapp": "✅ *Link confirmed.* Paper saved to your collection.",
    },
    "ask_url": {
        "telegram": "Please send the correct paper URL (starting with http...):",
        "whatsapp": "Please send the correct paper URL (starting with http...):",
    },
    "link_updated": {
        "telegram": "✅ <b>Link updated.</b>\n<i>{url}</i>\nPaper record has been saved.",
        "whatsapp": "✅ *Link updated.*\n_{url}_\nPaper record has been saved.",
    },
    "bad_url": {
        "telegram": "⚠️ Invalid URL format. Please send the full link starting with http:",
        "whatsapp": "⚠️ Invalid URL format. Please send the full link starting with http:",
    },
    "paper_not_found": {
        "telegram": "❌ <b>Paper not found.</b>",
        "whatsapp": "❌ *Paper not found.*",
    },
    "not_failed_state": {
        "telegram": "⚠️ <b>This paper is not in a failed state.</b>",
        "whatsapp": "⚠️ *This paper is not in a failed state.*",
    },
    "ask_add_info": {
        "telegram": "🖼️ <b>Image received.</b>\n\nWould you like to add <b>notes and tags</b> before analysis?",
        "whatsapp": "🖼️ *Image received.*\n\nWould you like to add *notes and tags* before analysis?",
    },
    "ask_notes_tags": {
        "telegram": (
            "📝 Please write your <b>notes and tags</b> in a single message.\n\n"
            "Recommended format:\n"
            "<code>note: your text  tag: deep learning, mri</code>\n\n"
            "Or write freely. The system will process the context."
        ),
        "whatsapp": (
            "📝 Please write your *notes and tags* in a single message.\n\n"
            "Recommended format:\n"
            "`note: your text  tag: deep learning, mri`\n\n"
            "Or write freely. The system will process the context."
        ),
    },
    "unrecognized_ask_info": {
        "telegram": "⚠️ Command not recognized. Would you like to add notes and tags?",
        "whatsapp": "⚠️ Command not recognized. Would you like to add notes and tags?",
    },
    "reply_1_or_2": {
        "telegram": "⚠️ Please reply <b>1</b> (yes) or <b>2</b> (no).",
        "whatsapp": "⚠️ Please reply *1* (yes) or *2* (no).",
    },
    "search_usage": {
        "telegram": (
            "🔍 <b>Search your collection</b>\n\n"
            "<b>Usage:</b> /search &lt;keyword&gt;\n\n"
            "Matches title, authors, summary, tags, subfields and category.\n\n"
            "<b>Examples:</b>\n"
            "  /search transformer\n"
            "  /search brain tumor\n"
            "  /search computer vision year:2024\n"
            "  /search segmentation year:2020-2024"
        ),
        "whatsapp": (
            "🔍 *Search your collection*\n\n"
            "*Usage:* search <keyword>\n\n"
            "Matches title, authors, summary, tags, subfields and category.\n\n"
            "*Examples:*\n"
            "  search transformer\n"
            "  search brain tumor\n"
            "  search computer vision year:2024\n"
            "  search segmentation year:2020-2024"
        ),
    },
    "search_no_results": {
        "telegram": "No papers found for '{query}'.",
        "whatsapp": "No papers found for '{query}'.",
    },
}


def _fmt(platform):
    if platform == "telegram":
        return (
            lambda t: f"<b>{t}</b>",
            lambda t: f"<i>{t}</i>",
            lambda text, url: f"<a href='{url}'>{text}</a>",
        )
    return (
        lambda t: f"*{t}*",
        lambda t: f"_{t}_",
        lambda text, url: url,
    )


def _truncate(text, max_len):
    return (text[:max_len] + "...") if len(text) > max_len else text


BOT_HTTP_TIMEOUT = (5, 10)


def _telegram_post(method, payload, description):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload,
            timeout=BOT_HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.warning("Telegram %s failed: %s", description, type(e).__name__)
        return False
    if not response.ok:
        logger.warning("Telegram %s returned HTTP %s", description, response.status_code)
    return response.ok


def _whatsapp_post(recipient, body, description):
    try:
        response = requests.post(
            f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_ID}/messages",
            headers={
                "Authorization": f"Bearer {WHATSAPP_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"messaging_product": "whatsapp", "to": recipient, **body},
            timeout=BOT_HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.warning("WhatsApp %s failed: %s", description, type(e).__name__)
        return False
    if not response.ok:
        logger.warning("WhatsApp %s returned HTTP %s", description, response.status_code)
    return response.ok


def send_message(platform, recipient, text):
    if platform == "telegram":
        return _telegram_post(
            "sendMessage",
            {"chat_id": recipient, "text": text, "parse_mode": "HTML"},
            "message",
        )
    if platform == "whatsapp":
        return _whatsapp_post(
            recipient,
            {"type": "text", "text": {"preview_url": True, "body": text}},
            "message",
        )
    return False


def send_buttons(platform, recipient, text, buttons, single_row=False):
    if platform == "telegram":
        keys = [{"text": b.get("text", b["title"]), "callback_data": b["id"]} for b in buttons]
        return _telegram_post(
            "sendMessage",
            {
                "chat_id": recipient,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [keys] if single_row else [[key] for key in keys]
                },
            },
            "buttons",
        )
    if platform == "whatsapp":
        return _whatsapp_post(
            recipient,
            {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": text},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                            for b in buttons
                        ]
                    },
                },
            },
            "buttons",
        )
    return False


CONFIRM_LINK_BUTTONS = (
    {"id": "link_yes", "title": "Yes, correct"},
    {"id": "link_no", "title": "No, wrong"},
)

ADD_INFO_BUTTONS = (
    {"id": "add_info_yes", "title": "Yes, add info"},
    {"id": "add_info_no", "title": "No, analyze now"},
)


def send_confirmation_buttons(platform, recipient, link):
    text = MESSAGE_TEMPLATES["link_confirmation"][platform].replace("{link}", link)
    send_buttons(platform, recipient, text, list(CONFIRM_LINK_BUTTONS), single_row=True)


def answer_telegram_callback(callback_query_id):
    _telegram_post("answerCallbackQuery", {"callback_query_id": callback_query_id}, "callback answer")


def _is_empty_analysis(enriched_data):
    if not enriched_data:
        return True
    if enriched_data.get("is_research_poster") is False:
        return True
    title       = (enriched_data.get("title") or "").strip().lower()
    description = (enriched_data.get("summary") or "").strip()
    authors     = (enriched_data.get("authors") or "").strip()
    if title not in {"", "untitled", "ai analysis failed", "analysis failed"}:
        return False
    return not description and not authors


def process_uploaded_poster(
    image_content,
    filename,
    source="web",
    existing_poster=None,
    user_notes=None,
    user_tags=None,
    activity_user=None,
    group_ids=None,
):
    poster = existing_poster or ResearchPoster()
    try:
        if image_content and not existing_poster:
            image_content, filename = _convert_heic_to_jpeg(image_content, filename)
            poster.image.save(filename, ContentFile(image_content))
            poster.generate_thumbnail(save=False)

        if activity_user and not poster.uploaded_by_id:
            poster.uploaded_by = activity_user

        user_group_ids = list(
            UserGroupMembership.objects
            .filter(user=activity_user)
            .values_list("group_id", flat=True)
        ) if activity_user else None

        try:
            poster.ensure_image_sha256(save=bool(poster.pk))
            if poster.image_sha256:
                hash_dup = ResearchPoster.find_existing_by_hash(
                    poster.image_sha256, exclude_id=poster.pk, group_ids=user_group_ids
                )
                if hash_dup:
                    logger.info(
                        "Duplicate detected by image hash: new=%s existing=%s",
                        poster.pk, hash_dup.pk,
                    )
                    if image_content and not existing_poster:
                        poster.image.delete(save=False)
                        if poster.pk:
                            poster.delete()
                    return hash_dup, None, "duplicate"
        except Exception as e:
            logger.warning("Image hash check failed (continuing): %s", e)

        overrides = {}
        if existing_poster:
            if existing_poster.paper_link \
                    and existing_poster.paper_link != (existing_poster.ai_paper_link or ""):
                overrides["paper_link"] = existing_poster.paper_link
            if existing_poster.github_link \
                    and existing_poster.github_link != (existing_poster.ai_github_link or ""):
                overrides["github_link"] = existing_poster.github_link

        enriched_data = analyze_and_enrich(poster.image.path, overrides=overrides)

        if enriched_data.get("_ai_error"):
            if poster.title in ("Pending analysis…", ""):
                poster.title = "Analysis Failed"
            poster.analysis_status = 'failed'
            if poster.pk:
                poster.save(update_fields=["title", "analysis_status", "uploaded_by", "updated_at"])
            else:
                poster.save()
            return poster, None, "analysis_failed"

        if _is_empty_analysis(enriched_data):
            if image_content and not existing_poster:
                poster.image.delete(save=False)
                if poster.pk:
                    poster.delete()
            return None, None, "no_text"

        extracted_title = enriched_data.get("title", "").strip()
        if extracted_title and extracted_title.lower() not in {"", "untitled"}:
            norm_title = " ".join(extracted_title.lower().split())
            exclude_pk = poster.pk if poster.pk else None
            dup_qs = ResearchPoster.objects.exclude(pk=exclude_pk)
            if user_group_ids is not None:
                dup_qs = dup_qs.filter(groups__id__in=user_group_ids)
            existing = dup_qs.filter(title__iexact=extracted_title).first()

            if not existing:
                words = [w for w in re.findall(r"[a-zA-Z]{4,}", norm_title)]
                if len(words) >= 3:
                    q = Q()
                    for w in words[:6]:
                        q |= Q(title__icontains=w)
                    candidates = dup_qs.filter(q)
                    for c in candidates:
                        c_norm  = " ".join(c.title.lower().split())
                        c_words = set(re.findall(r"[a-zA-Z]{4,}", c_norm))
                        t_words = set(words)
                        overlap = len(c_words & t_words)
                        total   = max(len(c_words), len(t_words), 1)
                        if overlap / total >= 0.8:
                            existing = c
                            break

            if existing:
                if image_content and not existing_poster:
                    poster.image.delete(save=False)
                    if poster.pk:
                        poster.delete()
                return existing, enriched_data, "duplicate"

        for link_field in ("paper_link", "github_link"):
            if existing_poster and not enriched_data.get(link_field):
                enriched_data[link_field] = getattr(existing_poster, link_field, "") or ""

        for field, (key, default) in {
            "title":       ("title", "Untitled"),
            "authors":     ("authors", ""),
            "summary":     ("summary", ""),
            "subfields":   ("subfields", ""),
            "paper_link":  ("paper_link", ""),
            "github_link": ("github_link", ""),
            "conference":  ("conference", ""),
        }.items():
            setattr(poster, field, enriched_data.get(key, default))

        if "paper_link" not in overrides:
            poster.ai_paper_link = enriched_data.get("paper_link", "")
        if "github_link" not in overrides:
            poster.ai_github_link = enriched_data.get("github_link", "")

        raw_year = enriched_data.get("publication_year", "")
        if raw_year:
            try:
                poster.publication_year = int(str(raw_year).strip()[:4])
            except (ValueError, TypeError):
                pass

        poster.derive_category_from_subfields()

        ai_tags = (enriched_data.get("tags", "") or "").strip()
        user_tag_input = (user_tags or "").strip()
        seen, combined = set(), []
        for tag_source in (user_tag_input, ai_tags):
            for t in tag_source.split(","):
                t = t.strip()
                key = t.lower()
                if t and key not in seen:
                    seen.add(key)
                    combined.append(t)
        poster.tags = ", ".join(combined)

        if user_notes and user_notes.strip():
            poster.notes = user_notes.strip()
        else:
            poster.notes = enriched_data.get("notes", f"Auto-extracted via {source}")

        analysis_group = None
        if activity_user is not None:
            primary_membership = (
                UserGroupMembership.objects
                .filter(user=activity_user, is_primary=True)
                .select_related("group")
                .first()
            )
            if primary_membership:
                analysis_group = primary_membership.group

        if analysis_group is None and group_ids:
            analysis_group = ResearchGroup.objects.filter(pk__in=group_ids).first()

        analysis_research_interests = (
            analysis_group.research_interests_text if analysis_group else ""
        )

        if analysis_research_interests and analysis_research_interests.strip():
            poster.why_useful = generate_why_useful(
                summary=poster.summary,
                user_notes=user_notes.strip() if user_notes and user_notes.strip() else "",
                user_tags=user_tags.strip() if user_tags and user_tags.strip() else "",
                research_interests=analysis_research_interests,
            )
        else:
            poster.why_useful = ""

        poster.validation_status = "pending"
        poster.analysis_status   = 'ok'
        poster.save()

        if group_ids:
            poster.groups.set(group_ids)

        if analysis_group and poster.why_useful:
            PosterGroupWhyUseful.objects.update_or_create(
                poster=poster,
                group=analysis_group,
                defaults={"why_useful": poster.why_useful},
            )

        ActivityLog.objects.get_or_create(
            poster=poster,
            action="created",
            defaults={
                "user": activity_user,
                "poster_title": poster.title,
                "details": f"Uploaded via {source} + AI analysis",
            },
        )
        return poster, enriched_data, None

    except Exception as e:
        logger.exception("process_uploaded_poster failed for poster %s", getattr(poster, "pk", None))
        return None, None, str(e)


def _build_links_section(poster, platform):
    bold, italic, link_fn = _fmt(platform)
    paper  = link_fn("View Paper", poster.paper_link) if poster.paper_link else italic("Not found")
    github = link_fn("View Code", poster.github_link) if poster.github_link else italic("Not found")
    return (
        f"{bold('Resources')}\n"
        f"   Link: {paper}\n"
        f"   GitHub: {github}"
    )


def _send_analysis_result(platform, recipient, poster):
    bold, italic, _ = _fmt(platform)
    title_max  = 80 if platform == "telegram" else 60
    author_max = 50 if platform == "telegram" else 40

    msg_parts = [
        f"{bold('✅ Analysis Complete')}\n",
        f"{bold('Title:')} {_truncate(poster.title, title_max)}",
        f"{bold('Authors:')} {_truncate(poster.authors, author_max)}",
        f"{bold('Category:')} {poster.get_category_display()}",
    ]

    if poster.why_useful and poster.why_useful.strip():
        msg_parts.append(f"\n💡 {italic(poster.why_useful.strip())}")

    msg_parts.append(f"\n{_build_links_section(poster, platform)}")
    msg_parts.append(italic("Successfully added to your primary collection"))
    send_message(platform, recipient, "\n".join(msg_parts))

def _save_image_only(image_content, filename, uploaded_by=None, source='web'):
    poster               = ResearchPoster()
    image_content, filename = _convert_heic_to_jpeg(image_content, filename)
    poster.image.save(filename, ContentFile(image_content))
    poster.title         = "Pending analysis…"
    poster.authors       = ""
    poster.summary       = ""
    poster.uploaded_by   = uploaded_by
    poster.upload_source = source
    poster.save()
    poster.generate_thumbnail(save=True)
    return poster


def _extract_inline_notes_and_tags(caption):
    if not caption:
        return "", ""
    text = caption.strip()
    tag_pattern  = re.compile(
        r'(?:tags?|tga|tgs|tg)\s*[:\-]\s*(.+?)(?=\s+(?:note|notes|nte|nota)\s*[:\-]|$)',
        re.IGNORECASE,
    )
    note_pattern = re.compile(
        r'(?:notes?|nte|nota)\s*[:\-]\s*(.+?)(?=\s+(?:tags?|tga|tgs|tg)\s*[:\-]|$)',
        re.IGNORECASE,
    )
    tag_match  = tag_pattern.search(text)
    note_match = note_pattern.search(text)
    tags  = tag_match.group(1).strip()  if tag_match  else ""
    notes = note_match.group(1).strip() if note_match else ""
    if not tag_match and not note_match:
        notes = text
        tags  = ""
    return notes[:500], tags[:200]


def _dispatch_bot_analysis(platform, recipient, poster_id, notes, tags, user_id=None):
    ResearchPoster.objects.filter(pk=poster_id).update(analysis_status='processing')
    _set_pending(platform, recipient, {"poster_id": poster_id, "state": "processing"})
    _set_ratelimit(platform, recipient)

    if notes or tags:
        bold, italic, _ = _fmt(platform)
        confirm_parts = [f"📋 {bold('Got it!')}"]
        if notes:
            confirm_parts.append(f"   Notes: {italic(_truncate(notes, 80))}")
        if tags:
            confirm_parts.append(f"   Tags: {italic(tags)}")
        send_message(platform, recipient, "\n".join(confirm_parts))

    send_message(platform, recipient, MESSAGE_TEMPLATES["analyzing"][platform])
    task = process_bot_poster_task.delay(
        platform=platform,
        recipient=recipient,
        poster_id=poster_id,
        notes=notes,
        tags=tags,
        user_id=user_id,
    )
    cache.set(f"bot:task:{poster_id}", task.id, timeout=BOT_STATE_TTL)
    cache.set(f"bot:context:{poster_id}", {"platform": platform, "recipient": recipient}, timeout=BOT_STATE_TTL)


def _handle_bot_retry(platform, recipient, poster_id):
    bot_user = _require_link_or_notify(platform, recipient)
    if bot_user is None:
        return
    poster = accessible_posters(bot_user).filter(pk=poster_id).first()
    if not poster:
        send_message(platform, recipient, MESSAGE_TEMPLATES["paper_not_found"][platform])
        return
    if poster.analysis_status != 'failed':
        send_message(platform, recipient, MESSAGE_TEMPLATES["not_failed_state"][platform])
        return
    if _check_ratelimit(platform, recipient):
        send_message(platform, recipient, MESSAGE_TEMPLATES["ratelimit"][platform])
        return
    _dispatch_bot_analysis(
        platform, recipient, poster_id,
        notes=poster.notes, tags=poster.tags,
        user_id=bot_user.pk,
    )


def _handle_media_upload(platform, recipient, media_content, filename, caption=None):
    if not media_content:
        send_message(platform, recipient, MESSAGE_TEMPLATES["download_error"][platform])
        return

    if _check_ratelimit(platform, recipient):
        send_message(platform, recipient, MESSAGE_TEMPLATES["ratelimit"][platform])
        return

    bot_user = _get_bot_user(platform, recipient)
    if bot_user is None:
        send_message(platform, recipient, MESSAGE_TEMPLATES["link_required"][platform])
        return

    poster = _save_image_only(media_content, filename, uploaded_by=bot_user, source=platform)

    if caption and caption.strip():
        notes, tags = _extract_inline_notes_and_tags(caption)
        _dispatch_bot_analysis(platform, recipient, poster.pk, notes, tags, user_id=bot_user.pk)
        return

    _set_pending(platform, recipient, {"poster_id": poster.pk, "state": "awaiting_add_info"})

    send_buttons(
        platform, recipient,
        MESSAGE_TEMPLATES["ask_add_info"][platform],
        list(ADD_INFO_BUTTONS),
    )


def _handle_pending_validation_text(platform, recipient, text):
    pending = _get_pending(platform, recipient)
    if not pending:
        return False

    state      = pending["state"]
    poster     = ResearchPoster.objects.filter(pk=pending["poster_id"]).first()
    normalized = text.strip().lower()

    if state == "processing":
        return True

    if state == "awaiting_add_info":
        YES = {"1", "si", "sì", "yes", "y", "ok", "sure", "yep"}
        NO  = {"2", "no", "n", "nope", "nah"}

        if normalized in YES:
            _set_pending(platform, recipient, {**pending, "state": "awaiting_notes_and_tags"})
            send_message(platform, recipient, MESSAGE_TEMPLATES["ask_notes_tags"][platform])
            return True

        elif normalized in NO:
            bot_user = _get_bot_user(platform, recipient)
            _dispatch_bot_analysis(platform, recipient, pending["poster_id"], notes=None, tags=None,
                                   user_id=bot_user.pk if bot_user else None)
            return True

        else:
            if platform == "telegram":
                send_buttons(
                    platform, recipient,
                    MESSAGE_TEMPLATES["unrecognized_ask_info"][platform],
                    list(ADD_INFO_BUTTONS),
                )
            else:
                send_message(platform, recipient, MESSAGE_TEMPLATES["reply_1_or_2"][platform])
            return True

    if state == "awaiting_notes_and_tags":
        notes, tags = _extract_inline_notes_and_tags(text)
        bot_user = _get_bot_user(platform, recipient)
        _dispatch_bot_analysis(platform, recipient, pending["poster_id"], notes=notes, tags=tags,
                               user_id=bot_user.pk if bot_user else None)
        return True

    if state == "awaiting_confirmation":
        YES = {"yes", "si", "sì", "ok", "correct", "y", "yep", "yeah", "sure", "1"}
        NO  = {"no", "n", "nope", "wrong", "incorrect", "bad", "nah", "2"}
        if normalized in YES:
            _del_pending(platform, recipient)
            send_message(platform, recipient, MESSAGE_TEMPLATES["link_confirmed"][platform])
        elif normalized in NO:
            _set_pending(platform, recipient, {**pending, "state": "awaiting_url"})
            send_message(platform, recipient, MESSAGE_TEMPLATES["ask_url"][platform])
        else:
            return False
        return True

    if state == "awaiting_url":
        url = text.strip()
        if url.startswith("http"):
            if poster:
                poster.paper_link = url
                poster.save(update_fields=["paper_link", "updated_at"])
                ActivityLog.objects.create(
                    user=None, poster=poster, action="updated",
                    poster_title=poster.title,
                    details="Paper link updated by user via bot validation",
                )
            _del_pending(platform, recipient)
            msg = MESSAGE_TEMPLATES["link_updated"][platform].replace("{url}", url)
            send_message(platform, recipient, msg)
        else:
            send_message(platform, recipient, MESSAGE_TEMPLATES["bad_url"][platform])
        return True

    return False


def _handle_pending_callback(platform, recipient, callback_data):
    if callback_data.startswith("retry_"):
        try:
            poster_id = int(callback_data.split("_", 1)[1])
            _handle_bot_retry(platform, recipient, poster_id)
        except (ValueError, IndexError):
            logger.warning("Invalid retry callback data: %s", callback_data)
        return

    pending = _get_pending(platform, recipient)
    if not pending:
        return

    state = pending.get("state")

    if state == "awaiting_add_info":
        if callback_data == "add_info_yes":
            _set_pending(platform, recipient, {**pending, "state": "awaiting_notes_and_tags"})
            send_message(platform, recipient, MESSAGE_TEMPLATES["ask_notes_tags"][platform])
        elif callback_data == "add_info_no":
            bot_user = _get_bot_user(platform, recipient)
            _dispatch_bot_analysis(platform, recipient, pending["poster_id"], notes=None, tags=None,
                                   user_id=bot_user.pk if bot_user else None)

    elif state == "awaiting_confirmation":
        if callback_data == "link_yes":
            _del_pending(platform, recipient)
            send_message(platform, recipient, MESSAGE_TEMPLATES["link_confirmed"][platform])
        elif callback_data == "link_no":
            _set_pending(platform, recipient, {**pending, "state": "awaiting_url"})
            send_message(platform, recipient, MESSAGE_TEMPLATES["ask_url"][platform])


def _bot_poster_scope(platform, recipient):
    user = _require_link_or_notify(platform, recipient)
    return accessible_posters(user) if user else None


def _handle_dashboard_command(platform, recipient):
    scope = _bot_poster_scope(platform, recipient)
    if scope is None:
        return

    bold, italic, link_fn = _fmt(platform)

    counts = scope.aggregate(
        total=Count("id", distinct=True),
        pending=Count("id", filter=Q(validation_status="pending"), distinct=True),
        approved=Count("id", filter=Q(validation_status="approved"), distinct=True),
        rejected=Count("id", filter=Q(validation_status="rejected"), distinct=True),
    )
    total, pending = counts["total"], counts["pending"]
    approved, rejected = counts["approved"], counts["rejected"]

    top_cats = (
        scope
        .exclude(category="other")
        .values("category")
        .annotate(n=Count("id", distinct=True))
        .order_by("-n")[:3]
    )
    cat_labels = dict(ResearchPoster.CATEGORY_CHOICES)
    cat_lines = ", ".join(
        f"{cat_labels.get(c['category'], c['category'])} ({c['n']})"
        for c in top_cats
    ) if top_cats else "—"

    recent = scope.order_by("-created_at")[:5]

    msg_parts = [
        f"{bold('📊 Research Collection')}\n",
        f"Total: {bold(str(total))}  •  ✅ {approved}  •  ⏳ {pending}  •  ❌ {rejected}",
        f"Top categories: {cat_lines}",
    ]

    if recent:
        title_max = 35 if platform == "telegram" else 30
        msg_parts.append(f"\n{bold('Recent Papers')}")
        for i, p in enumerate(recent, 1):
            status_icon = {"approved": "✅", "pending": "⏳", "rejected": "❌"}.get(p.validation_status, "•")
            paper  = link_fn("Paper", p.paper_link) if p.paper_link else "—"
            github = link_fn("Code", p.github_link)  if p.github_link else "—"
            msg_parts.append(f"{i}. {status_icon} {bold(_truncate(p.title, title_max))}")
            msg_parts.append(f"   {paper} • {github}")
    else:
        msg_parts.append(f"\n{italic('No papers in your collection yet.')}")

    send_message(platform, recipient, "\n".join(msg_parts))


def _handle_search_command(platform, recipient, query):
    if not query:
        send_message(platform, recipient, MESSAGE_TEMPLATES["search_usage"][platform])
        return

    scope = _bot_poster_scope(platform, recipient)
    if scope is None:
        return

    import html as html_mod

    year_from = None
    year_to = None
    year_match = re.search(r'\byear:(\d{4})(?:-(\d{4}))?\b', query)
    if year_match:
        year_from = int(year_match.group(1))
        year_to = int(year_match.group(2)) if year_match.group(2) else year_from
        query = query[:year_match.start()].strip() + " " + query[year_match.end():].strip()
        query = query.strip()

    if not query and not year_from:
        send_message(platform, recipient, MESSAGE_TEMPLATES["search_usage"][platform])
        return

    safe_query = html_mod.escape(query) if platform == "telegram" else query

    qs = scope.exclude(validation_status="rejected")
    if query:
        q_filter = (
            Q(title__icontains=query) | Q(authors__icontains=query) |
            Q(summary__icontains=query) | Q(tags__icontains=query) |
            Q(subfields__icontains=query) | Q(category__icontains=query)
        )


        query_lower = query.lower()
        query_slug = query_lower.replace(" ", "_").replace("-", "_")
        if query_slug != query_lower:
            q_filter |= Q(subfields__icontains=query_slug) | Q(category__icontains=query_slug)

        for cat_slug, cat_label in ResearchPoster.CATEGORY_CHOICES:
            if query_lower in cat_label.lower():
                q_filter |= Q(category=cat_slug)

        for sf_slug, sf_label in ResearchPoster.SUBFIELD_CHOICES:
            if query_lower in sf_label.lower():
                q_filter |= Q(subfields__icontains=sf_slug)

        qs = qs.filter(q_filter)
    if year_from:
        qs = qs.filter(publication_year__gte=year_from)
    if year_to:
        qs = qs.filter(publication_year__lte=year_to)

    results = qs.order_by("-created_at")[:5]

    if not results:
        msg = MESSAGE_TEMPLATES["search_no_results"][platform].replace("{query}", safe_query)
        send_message(platform, recipient, msg)
        return

    bold, italic, link_fn = _fmt(platform)
    title_max = 50 if platform == "telegram" else 40
    lines = [f"🔍 {bold('Results for:')} {italic(safe_query)}  ({len(results)} found)\n"]
    for i, p in enumerate(results, 1):
        paper  = link_fn("Paper", p.paper_link) if p.paper_link else "—"
        github = link_fn("Code", p.github_link)  if p.github_link else "—"
        subfields = ", ".join(p.subfields_display[:3]) if p.subfields_display else ""

        year_str = f" ({p.publication_year})" if p.publication_year else ""
        lines.append(f"{i}. {bold(_truncate(p.title, title_max))}{year_str}")
        if subfields:
            lines.append(f"   {p.get_category_display()} · {subfields}")
        else:
            lines.append(f"   {p.get_category_display()}")
        lines.append(f"   {paper} • {github}")

        if p.why_useful and p.why_useful.strip():
            lines.append(f"   💡 {italic(_truncate(p.why_useful.strip(), 80))}")
        lines.append("")

    send_message(platform, recipient, "\n".join(lines).strip())


def _subfield_q(slug):
    return (
        Q(subfields=slug)
        | Q(subfields__startswith=f"{slug},")
        | Q(subfields__endswith=f",{slug}")
        | Q(subfields__contains=f",{slug},")
    )


def _multi(params, key):
    values = params.getlist(key) if hasattr(params, "getlist") else []
    if not values:
        single = params.get(key, "")
        values = single.split(",") if single else []
    return [v.strip() for v in values if v and v.strip()]


def _int_or_none(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _apply_filters(queryset, params, favorite_user=None):
    search = (params.get("search") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(title__icontains=search) | Q(authors__icontains=search)
            | Q(summary__icontains=search) | Q(tags__icontains=search)
            | Q(subfields__icontains=search)
        )

    text_filters = (
        ("author",  "authors__icontains"),
        ("summary", "summary__icontains"),
        ("status",  "validation_status"),
        ("category", "category"),
    )
    for param, field in text_filters:
        val = (params.get(param) or "").strip()
        if val:
            queryset = queryset.filter(**{field: val})

    date_filters = (
        ("date_from", "created_at__date__gte"),
        ("date_to",   "created_at__date__lte"),
    )
    for param, field in date_filters:
        val = (params.get(param) or "").strip()
        if val:
            queryset = queryset.filter(**{field: val})

    year_filters = (
        ("year_from", "publication_year__gte"),
        ("year_to",   "publication_year__lte"),
    )
    for param, field in year_filters:
        val = _int_or_none((params.get(param) or "").strip())
        if val is not None:
            queryset = queryset.filter(**{field: val})

    for sf in _multi(params, "subfield"):
        queryset = queryset.filter(_subfield_q(sf))

    if params.get("has_github"):
        queryset = queryset.exclude(github_link="").exclude(github_link__isnull=True)
    if params.get("has_paper"):
        queryset = queryset.exclude(paper_link="").exclude(paper_link__isnull=True)

    user_scoped = (
        ("favorites", lambda qs, u: qs.filter(favorites__user=u).distinct()),
        ("mine",      lambda qs, u: qs.filter(uploaded_by=u)),
    )
    for param, apply_fn in user_scoped:
        if not params.get(param):
            continue
        if favorite_user and favorite_user.is_authenticated:
            queryset = apply_fn(queryset, favorite_user)
        else:
            return queryset.none()

    conferences = _multi(params, "conference")
    if conferences:
        q = Q()
        for c in conferences:
            q |= Q(conference__icontains=c)
        queryset = queryset.filter(q)

    group_id = _int_or_none((params.get("group") or "").strip())
    if group_id is not None:
        queryset = queryset.filter(groups__id=group_id).distinct()

    return queryset


def _get_stats(user, params=None):
    user_group_ids = list(
        UserGroupMembership.objects
        .filter(user=user)
        .values_list("group_id", flat=True)
    )
    if not user_group_ids:
        return {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "favorites": 0}

    qs = ResearchPoster.objects.filter(groups__id__in=user_group_ids).distinct()

    if params is not None:
        params_copy = params.copy() if hasattr(params, "copy") else dict(params)
        if hasattr(params_copy, "pop"):
            params_copy.pop("status", None)
        qs = _apply_filters(qs, params_copy, favorite_user=user)

    stats = qs.aggregate(
        total=Count("id", distinct=True),
        pending=Count("id", filter=Q(validation_status="pending"), distinct=True),
        approved=Count("id", filter=Q(validation_status="approved"), distinct=True),
        rejected=Count("id", filter=Q(validation_status="rejected"), distinct=True),
    )
    stats["favorites"] = (
        Favorite.objects
        .filter(user=user, poster__groups__id__in=user_group_ids)
        .values("poster_id")
        .distinct()
        .count()
    )
    return stats


def _serialize_activity(activity):
    from django.utils.timesince import timesince
    icons = {
        "created": "[Created]", "updated": "[Updated]", "deleted": "[Deleted]",
        "status_changed": "[Status]", "favorited": "[Starred]", "unfavorited": "[Unstarred]",
    }
    return {
        "icon":           icons.get(activity.action, ""),
        "action_display": activity.get_action_display(),
        "poster_title":   activity.poster_title or "Unknown",
        "time":           timesince(activity.timestamp) + " ago",
        "user":           activity.user.username if activity.user else "System",
    }


@login_required(login_url="login")
def tags_autocomplete(request):
    q = request.GET.get("q", "").strip().lower()
    if not q or len(q) < 2:
        return JsonResponse([], safe=False)

    all_tags = (
        accessible_posters(request.user)
        .exclude(tags__isnull=True)
        .exclude(tags="")
        .filter(tags__icontains=q)
        .values_list("tags", flat=True)
    )

    tag_counts = {}
    for tag_str in all_tags:
        for t in tag_str.split(","):
            t = t.strip()
            if t:
                key = t.lower()
                if q in key:
                    if key not in tag_counts:
                        tag_counts[key] = {"label": t, "count": 0}
                    tag_counts[key]["count"] += 1

    results = sorted(tag_counts.values(), key=lambda x: -x["count"])[:15]
    return JsonResponse([r["label"] for r in results], safe=False)


WEB_UPLOAD_COOLDOWN = 60


@login_required(login_url="login")
def conference_view(request):
    return render(request, "conference.html")


@_groups_required
def conference_search(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    paper_title = (body.get("paper_title") or "").strip()
    conference = (body.get("conference") or "").strip()
    year = (body.get("year") or "").strip()
    day = (body.get("day") or "").strip()
    tags = (body.get("tags") or "").strip()
    other_conference = (body.get("other_conference") or "").strip()

    if not paper_title or not conference or not year:
        return JsonResponse({"error": "Paper title, conference and year are required."}, status=400)

    from .utils_conference import search_conference

    try:
        result = search_conference(
            paper_title=paper_title,
            conference=conference,
            year=year,
            day=day,
            tags=tags,
            other_conference=other_conference,
        )
        return JsonResponse(result)
    except Exception as e:
        logger.exception("Conference search failed: %s", e)
        return JsonResponse(
            {"error": "Search failed — please try again later."},
            status=500,
        )


@login_required(login_url="login")
def upload_poster(request):
    is_ajax = _is_ajax(request)

    if not user_can_interact(request.user) and request.method == "POST":
        msg = "Your account is not in any research group yet. Uploads are disabled until an administrator adds you."
        if is_ajax:
            return JsonResponse({"error": "no_groups", "message": msg}, status=403)
        messages.warning(request, msg)
        return redirect("upload")

    if request.method == "POST":
        rate_key = f"web:upload_ratelimit:{request.user.pk}"
        if cache.get(rate_key):
            if is_ajax:
                return JsonResponse(
                    {"error": "rate_limit", "message": "You are uploading too quickly. Please wait a minute before uploading again."},
                    status=429,
                )
            messages.warning(
                request,
                "You are uploading too quickly. Please wait a minute before uploading again.",
            )
            return redirect("upload")

        files = request.FILES.getlist("image")
        user_notes = request.POST.get("notes", "") or ""
        user_tags = request.POST.get("tags", "") or ""
        is_multi = len(files) > 1

        user_group_ids = set(UserGroupMembership.objects.filter(user=request.user).values_list("group_id", flat=True))

        def _parse_group_ids(raw_list):
            return [int(gid) for gid in raw_list if str(gid).isdigit() and int(gid) in user_group_ids]

        shared_group_ids = _parse_group_ids(request.POST.getlist("group_ids"))
        per_file_groups = []
        if is_multi:
            for i in range(len(files)):
                indexed = _parse_group_ids(request.POST.getlist(f"group_ids_{i}"))
                per_file_groups.append(indexed or shared_group_ids)
        else:
            per_file_groups = [shared_group_ids] * len(files)

        if user_group_ids:
            for i, gids in enumerate(per_file_groups):
                if not gids:
                    fname = getattr(files[i], "name", f"file #{i + 1}") if i < len(files) else f"file #{i + 1}"
                    msg = (
                        f"Select at least one group for \"{fname}\"."
                        if is_multi else
                        "Select at least one group for this upload."
                    )
                    if is_ajax:
                        return JsonResponse({"error": "no_groups", "message": msg}, status=400)
                    messages.error(request, msg)
                    return redirect("upload")

        if not files:
            if is_ajax:
                return JsonResponse(
                    {"error": "no_files", "message": "Please select at least one image."},
                    status=400,
                )
            messages.error(request, "Please select at least one image.")
            return redirect("upload")

        if is_multi:
            user_notes = ""
            user_tags = ""

        allowed_mimes = PosterUploadForm.ALLOWED_MIME_TYPES
        allowed_exts = PosterUploadForm.ALLOWED_EXTENSIONS
        max_size = PosterUploadForm.MAX_UPLOAD_SIZE
        first_task_id = None

        for idx, f in enumerate(files):
            mime = getattr(f, "content_type", "")
            ext = f.name.rsplit(".", 1)[-1].lower() if "." in getattr(f, "name", "") else ""
            if mime not in allowed_mimes and ext not in allowed_exts:
                continue
            if f.size > max_size:
                continue

            file_group_ids = per_file_groups[idx] if idx < len(per_file_groups) else shared_group_ids

            poster_temp = ResearchPoster()
            raw = f.read()
            converted, conv_name = _convert_heic_to_jpeg(raw, f.name)
            poster_temp.image.save(conv_name, ContentFile(converted))
            poster_temp.title = "Pending analysis…"
            poster_temp.authors = ""
            poster_temp.summary = ""
            poster_temp.uploaded_by = request.user
            poster_temp.upload_source = "web"
            poster_temp.analysis_status = "processing"
            poster_temp.save()
            poster_temp.generate_thumbnail(save=True)

            if file_group_ids:
                poster_temp.groups.set(file_group_ids)

            task = process_poster_task.delay(
                poster_id=poster_temp.pk,
                user_notes=user_notes,
                user_tags=user_tags,
                source="web",
                user_id=request.user.pk,
                group_ids=file_group_ids,
            )
            cache.set(f"task:poster:{poster_temp.pk}", task.id, timeout=3600)
            if first_task_id is None:
                first_task_id = task.id

        if first_task_id is None:
            if is_ajax:
                return JsonResponse(
                    {"error": "no_valid_files", "message": "No valid images found. Check file type and size."},
                    status=400,
                )
            messages.error(request, "No valid images found. Check file type and size.")
            return redirect("upload")

        cache.set(rate_key, 1, timeout=WEB_UPLOAD_COOLDOWN)

        redirect_url = f"/dashboard/?task_id={first_task_id}"
        n = len(files)
        if is_ajax:
            return JsonResponse({"redirect": redirect_url})

        if n == 1:
            messages.info(request, "Poster uploaded. AI analysis is in progress.")
        else:
            messages.info(request, f"{n} posters uploaded. AI analysis is in progress for each one.")

        return redirect(redirect_url)
    else:
        form = PosterUploadForm()

    recent_uploads = (
        ResearchPoster.objects
        .filter(uploaded_by=request.user, upload_source="web")
        .order_by("-created_at")[:3]
    )
    user_posters = ResearchPoster.objects.filter(uploaded_by=request.user)
    upload_stats = {
        "total": user_posters.count(),
        "approved": user_posters.filter(validation_status="approved").count(),
        "pending": user_posters.filter(validation_status="pending").count(),
        "processing": user_posters.filter(analysis_status="processing").count(),
    }
    user_memberships = (
        UserGroupMembership.objects
        .filter(user=request.user)
        .select_related("group")
        .order_by("-is_primary", "group__name")
    )
    user_groups = [
        {"id": m.group.pk, "name": m.group.name, "is_primary": m.is_primary}
        for m in user_memberships
    ]

    return render(request, "upload.html", {
        "form": form,
        "recent_uploads": recent_uploads,
        "upload_stats": upload_stats,
        "user_groups": user_groups,
    })


@login_required(login_url="login")
def task_status(request, task_id):
    result = AsyncResult(task_id)
    data   = {"state": result.state}

    if result.state == "SUCCESS":
        task_result       = result.result or {}
        data["poster_id"] = task_result.get("poster_id")
        data["status"]    = task_result.get("status", "done")
        data["title"]     = task_result.get("title", "")
        if task_result.get("status") in ("duplicate", "rejected", "analysis_failed"):
            data["warning"] = task_result.get("reason") or task_result.get("status")

    elif result.state == "FAILURE":
        data["error"] = str(result.result)

    return JsonResponse(data)


@login_required(login_url="login")
def dashboard(request):
    user_memberships = (
        UserGroupMembership.objects
        .filter(user=request.user)
        .select_related("group")
        .order_by("-is_primary", "group__name")
    )
    user_groups = [{"id": m.group.pk, "name": m.group.name, "is_primary": m.is_primary} for m in user_memberships]
    base_qs = ResearchPoster.objects.select_related("uploaded_by").all()
    group_filter_id = request.GET.get("group", "").strip()
    group_ids = [g["id"] for g in user_groups]
    base_qs = base_qs.filter(groups__id__in=group_ids).distinct()

    posters = _apply_filters(base_qs, request.GET, favorite_user=request.user)
    stats = _get_stats(request.user, params=request.GET)

    SORT_FIELDS = {
        "title": "title", "category": "category",
        "status": "validation_status", "date": "created_at",
    }
    sort_by     = request.GET.get("sort", "date")
    sort_order  = request.GET.get("order", "desc")
    order_field = SORT_FIELDS.get(sort_by, "created_at")
    posters     = posters.order_by(order_field if sort_order == "asc" else f"-{order_field}")

    try:
        per_page = min(max(int(request.GET.get("per_page", 15)), 10), 100)
    except ValueError:
        per_page = 15

    paginator = Paginator(posters, per_page)
    page_obj  = paginator.get_page(request.GET.get("page", 1))

    poster_ids   = [p.id for p in page_obj.object_list]
    favorite_ids = set(
        Favorite.objects.filter(
            user=request.user,
            poster_id__in=poster_ids,
        ).values_list("poster_id", flat=True)
    )

    for poster in page_obj:
        poster.is_incomplete = not poster.paper_link or not poster.github_link
        poster.is_favorite   = poster.id in favorite_ids

    subfield_params = _multi(request.GET, "subfield")
    filter_keys = (
        "search", "status", "category", "author", "summary",
        "date_from", "date_to", "year_from", "year_to",
        "has_github", "has_paper", "favorites", "mine",
        "subfield", "group", "conference",
    )

    context = {
        "posters":            page_obj,
        "page_obj":           page_obj,
        "paginator":          paginator,
        "per_page":           per_page,
        "stats":              stats,
        "search_query":       request.GET.get("search", ""),
        "status_filter":      request.GET.get("status", ""),
        "category_filter":    request.GET.get("category", ""),
        "subfield_filter":    subfield_params,
        "has_github_filter":  request.GET.get("has_github", ""),
        "has_paper_filter":   request.GET.get("has_paper", ""),
        "favorites_filter":   request.GET.get("favorites", ""),
        "mine_filter":        request.GET.get("mine", ""),
        "categories":         ResearchPoster.CATEGORY_CHOICES,
        "subfields_grouped":  ResearchPoster.SUBFIELDS_GROUPED,
        "user_groups":        user_groups,
        "group_filter":       group_filter_id,
        "is_superuser":       request.user.is_superuser,
        "conference_filter":  ",".join(request.GET.getlist("conference")),
        "author_filter":      request.GET.get("author", ""),
        "summary_filter":     request.GET.get("summary", ""),
        "date_from_filter":   request.GET.get("date_from", ""),
        "date_to_filter":     request.GET.get("date_to", ""),
        "year_from_filter":   request.GET.get("year_from", ""),
        "year_to_filter":     request.GET.get("year_to", ""),
        "sort_by":            sort_by,
        "sort_order":         sort_order,
        "pending_task_id":    request.GET.get("task_id", ""),
        "has_active_filters": any(request.GET.get(k) for k in filter_keys),
    }

    if request.GET.get("_ajax"):
        table_html        = render_to_string("_dashboard_partial.html", context, request=request)
        pagination_html   = render_to_string("_pagination_ajax.html", context, request=request)
        cat               = context["category_filter"]
        subfields_for_cat = []
        if cat:
            for slug, label, items in ResearchPoster.SUBFIELDS_GROUPED:
                if slug == cat:
                    subfields_for_cat = [{"value": v, "label": l} for v, l in items]
                    break
        return JsonResponse({
            "table_html":        table_html,
            "pagination_html":   pagination_html,
            "stats":             stats,
            "category_filter":   cat,
            "subfields_for_cat": subfields_for_cat,
            "subfield_filter":   subfield_params,
            "total_count":       paginator.count,
        })

    return render(request, "dashboard.html", context)


@login_required(login_url="login")
def poster_detail(request, poster_id):
    poster      = get_accessible_poster_or_404(
        request.user, poster_id, ResearchPoster.objects.select_related("uploaded_by"),
    )
    is_favorite = Favorite.objects.filter(user=request.user, poster=poster).exists()

    user_group_ids_set = set(UserGroupMembership.objects.filter(user=request.user).values_list("group_id", flat=True))
    assigned_user_groups = list(poster.groups.filter(pk__in=user_group_ids_set).order_by("name"))
    poster_groups = [{"id": g.pk, "name": g.name} for g in assigned_user_groups]
    poster_group_ids = set(poster.groups.values_list("pk", flat=True))

    default_why_group_id = None
    if len(assigned_user_groups) == 1:
        default_why_group_id = assigned_user_groups[0].pk
    elif len(assigned_user_groups) > 1:
        primary = UserGroupMembership.objects.filter(user=request.user, is_primary=True).first()
        assigned_pks = {g.pk for g in assigned_user_groups}
        if primary and primary.group_id in assigned_pks:
            default_why_group_id = primary.group_id
        else:
            default_why_group_id = assigned_user_groups[0].pk

    user_memberships = (
        UserGroupMembership.objects
        .filter(user=request.user)
        .select_related("group")
        .order_by("-is_primary", "group__name")
    )
    user_groups_for_edit = [
        {"id": m.group.pk, "name": m.group.name, "is_primary": m.is_primary,
         "is_assigned": m.group.pk in poster_group_ids}
        for m in user_memberships
    ]
    can_edit_groups = (
        request.user.is_superuser
        or any(g["is_assigned"] for g in user_groups_for_edit)
    )

    return render(request, "poster_detail.html", {
        "poster":         poster,
        "is_favorite":    is_favorite,
        "tags_display":   poster.tags_list,
        "poster_groups":  poster_groups,
        "default_why_group_id": default_why_group_id,
        "user_groups_for_edit": user_groups_for_edit,
        "can_edit_groups":      can_edit_groups,
    })


@_groups_required
def edit_poster(request, poster_id):
    poster = get_accessible_poster_or_404(request.user, poster_id)
    next_url = (
        request.POST.get("next") or request.GET.get("next")
        or request.META.get("HTTP_REFERER", "")
    ).strip()
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        next_url = ""
    if request.method == "POST":
        form = PosterEditForm(request.POST, instance=poster)
        if form.is_valid():
            form.save()
            ActivityLog.objects.create(
                user=request.user, poster=poster, action="updated",
                poster_title=poster.title,
                details=f'Paper "{poster.title}" updated',
            )
            messages.success(request, "Paper updated successfully!")
            return redirect(next_url or "dashboard")
    else:
        form = PosterEditForm(instance=poster)
    return render(request, "edit_poster.html", {"form": form, "poster": poster, "next_url": next_url})


@_groups_required
def update_status(request, poster_id):
    if request.method == "POST":
        poster     = get_accessible_poster_or_404(request.user, poster_id)
        new_status = request.POST.get("status")
        if new_status in ("pending", "approved", "rejected"):
            old_status               = poster.validation_status
            poster.validation_status = new_status
            poster.save(update_fields=["validation_status", "updated_at"])
            activity = ActivityLog.objects.create(
                user=request.user, poster=poster, action="status_changed",
                poster_title=poster.title,
                details=f"Status: {old_status} → {new_status}",
            )
            if _is_ajax(request):
                return JsonResponse({
                    "success":    True,
                    "new_status": new_status,
                    "message":    f"Status updated to {new_status.capitalize()}!",
                    "stats":      _get_stats(request.user),
                    "activity":   _serialize_activity(activity),
                })
            messages.success(request, f"Status updated to {new_status.capitalize()}!")
    return redirect("dashboard")


@_groups_required
def delete_poster(request, poster_id):
    if request.method == "POST":
        poster = get_accessible_poster_or_404(request.user, poster_id)
        title  = poster.title

        _remove_poster_image(poster)

        activity = ActivityLog.objects.create(
            user=request.user, poster=None, action="deleted",
            poster_title=title,
            details=f'Paper "{title}" deleted',
        )
        poster.delete()

        if _is_ajax(request):
            return JsonResponse({
                "success":  True,
                "message":  f'Paper "{title}" deleted successfully!',
                "stats":    _get_stats(request.user),
                "activity": _serialize_activity(activity),
            })
        messages.success(request, f'Paper "{title}" deleted successfully!')
    return redirect("dashboard")


@_groups_required
def toggle_favorite(request, poster_id):
    if request.method == "POST":
        poster            = get_accessible_poster_or_404(request.user, poster_id)
        favorite, created = Favorite.objects.get_or_create(user=request.user, poster=poster)

        if created:
            is_favorite = True
            action      = "favorited"
        else:
            favorite.delete()
            is_favorite = False
            action      = "unfavorited"

        activity = ActivityLog.objects.create(
            user=request.user, poster=poster, action=action,
            poster_title=poster.title,
            details=f'Paper "{poster.title}" {action}',
        )
        return JsonResponse({
            "success":     True,
            "is_favorite": is_favorite,
            "stats":       _get_stats(request.user),
            "activity":    _serialize_activity(activity),
        })
    return JsonResponse({"success": False}, status=400)


@_groups_required
def update_notes(request, poster_id):
    if request.method == "POST":
        poster = get_accessible_poster_or_404(request.user, poster_id)
        try:
            notes = json.loads(request.body).get("notes", "").strip()
        except (json.JSONDecodeError, AttributeError):
            notes = request.POST.get("notes", "").strip()

        if len(notes) > 500:
            return JsonResponse({"success": False, "message": "Notes too long (max 500 characters)."}, status=400)
        poster.notes = notes
        poster.save(update_fields=["notes", "updated_at"])

        activity = ActivityLog.objects.create(
            user=request.user, poster=poster, action="updated",
            poster_title=poster.title,
            details="Notes updated",
        )
        if _is_ajax(request):
            return JsonResponse({
                "success":  True,
                "message":  "Notes saved!",
                "activity": _serialize_activity(activity),
            })
        messages.success(request, "Notes saved!")
        return redirect("dashboard")
    return JsonResponse({"success": False}, status=400)


@_groups_required
def update_tags(request, poster_id):
    if request.method != "POST":
        return JsonResponse({"success": False}, status=400)
    poster = get_accessible_poster_or_404(request.user, poster_id)
    try:
        tags = json.loads(request.body).get("tags", "").strip()
    except (json.JSONDecodeError, AttributeError):
        tags = request.POST.get("tags", "").strip()
    if len(tags) > 200:
        return JsonResponse({"success": False, "message": "Tags too long (max 200 characters)."}, status=400)

    poster.tags = tags
    poster.save(update_fields=["tags", "updated_at"])
    ActivityLog.objects.create(
        user=request.user, poster=poster, action="updated",
        poster_title=poster.title, details="Tags updated",
    )
    return JsonResponse({
        "success": True,
        "message": "Tags saved!",
        "tags_list": poster.tags_list,
    })


@_groups_required
def bulk_action(request):
    if request.method != "POST":
        return JsonResponse({"success": False}, status=400)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    ids    = data.get("ids", [])
    action = data.get("action", "")

    if not ids:
        return JsonResponse({"success": False, "error": "No papers selected"}, status=400)

    allowed_ids = list(
        accessible_posters(request.user)
        .filter(id__in=ids)
        .values_list("pk", flat=True)
    )
    posters = ResearchPoster.objects.filter(pk__in=allowed_ids)
    count   = len(allowed_ids)

    if action in ("pending", "approved", "rejected"):
        logs = [
            ActivityLog(
                user=request.user, poster=poster, action="status_changed",
                poster_title=poster.title,
                details=f"Bulk status: {poster.validation_status} → {action}",
            )
            for poster in posters
        ]
        posters.update(validation_status=action, updated_at=timezone.now())
        ActivityLog.objects.bulk_create(logs)
        message = f"{count} papers set to {action.capitalize()}"

    elif action == "delete":
        logs = []
        for poster in posters:
            _remove_poster_image(poster)
            logs.append(ActivityLog(
                user=request.user, poster=None, action="deleted",
                poster_title=poster.title,
                details=f'Paper "{poster.title}" deleted (bulk)',
            ))
        posters.delete()
        ActivityLog.objects.bulk_create(logs)
        message = f"{count} papers deleted"

    elif action == "favorite":
        already = set(
            Favorite.objects
            .filter(user=request.user, poster__in=posters)
            .values_list("poster_id", flat=True)
        )
        missing = [poster for poster in posters if poster.pk not in already]
        Favorite.objects.bulk_create(
            [Favorite(user=request.user, poster=poster) for poster in missing],
            ignore_conflicts=True,
        )
        ActivityLog.objects.bulk_create([
            ActivityLog(
                user=request.user, poster=poster, action="favorited",
                poster_title=poster.title,
                details=f'Paper "{poster.title}" favorited (bulk)',
            )
            for poster in missing
        ])
        message = f"{len(missing)} papers starred"

    elif action == "unfavorite":
        favorites    = Favorite.objects.filter(user=request.user, poster__in=posters)
        favorite_ids = set(favorites.values_list("poster_id", flat=True))
        favorites.delete()
        unstarred = [poster for poster in posters if poster.pk in favorite_ids]
        ActivityLog.objects.bulk_create([
            ActivityLog(
                user=request.user, poster=poster, action="unfavorited",
                poster_title=poster.title,
                details=f'Paper "{poster.title}" unfavorited (bulk)',
            )
            for poster in unstarred
        ])
        message = f"{len(unstarred)} papers unstarred"

    else:
        return JsonResponse({"success": False, "error": "Unknown action"}, status=400)

    return JsonResponse({
        "success": True,
        "message": message,
        "stats":   _get_stats(request.user),
        "action":  action,
    })


@_admin_required
def delete_all_activities(request):
    if request.method == "POST":
        count = ActivityLog.objects.count()
        ActivityLog.objects.all().delete()
        if _is_ajax(request):
            return JsonResponse({"success": True, "message": f"Successfully deleted {count} activity logs"})
        messages.success(request, f"Successfully deleted {count} activity logs")
        return redirect("dashboard")
    return JsonResponse({"success": False, "error": "Invalid request method"})


@_groups_required
def retry_analysis(request, poster_id):
    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)
    poster = get_accessible_poster_or_404(request.user, poster_id)
    if poster.analysis_status == 'processing':
        return JsonResponse({"success": False, "error": "Analysis is already running"}, status=400)
    poster.analysis_status = 'processing'
    poster.save(update_fields=["analysis_status", "updated_at"])
    task = process_poster_task.delay(
        poster_id=poster.pk,
        user_notes=poster.notes or "",
        user_tags=poster.tags or "",
        source="web",
        user_id=request.user.pk,
    )
    cache.set(f"task:poster:{poster.pk}", task.id, timeout=3600)
    return JsonResponse({"success": True, "task_id": task.id})


@_groups_required
def stop_analysis(request, poster_id):
    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)
    poster = get_accessible_poster_or_404(request.user, poster_id)

    task_id = cache.get(f"bot:task:{poster_id}") or cache.get(f"task:poster:{poster_id}")
    if task_id:
        try:
            celery_app.control.revoke(task_id, terminate=True)
        except Exception as e:
            logger.warning("Could not revoke task %s: %s", task_id, e)
        cache.delete(f"bot:task:{poster_id}")
        cache.delete(f"task:poster:{poster_id}")

    context = cache.get(f"bot:context:{poster_id}")
    if context:
        platform  = context["platform"]
        recipient = context["recipient"]
        _del_pending(platform, recipient)
        _clear_ratelimit(platform, recipient)
        cache.delete(f"bot:context:{poster_id}")
        send_message(platform, recipient, MESSAGE_TEMPLATES["analysis_failed"][platform])

    poster.analysis_status = 'failed'
    if poster.title in ("Pending analysis…", ""):
        poster.title = "Analysis Stopped"
    poster.save(update_fields=["title", "analysis_status", "updated_at"])

    return JsonResponse({"success": True, "message": "Analysis stopped"})


@login_required(login_url="login")
def dashboard_live_status(request):
    from django.db.models import Max
    user_group_ids = list(
        UserGroupMembership.objects
        .filter(user=request.user)
        .values_list("group_id", flat=True)
    )
    if not user_group_ids:
        return JsonResponse({'processing': 0, 'latest_id': 0})

    qs = ResearchPoster.objects.filter(groups__id__in=user_group_ids).distinct()
    processing = qs.filter(analysis_status='processing').count()
    latest_id = qs.aggregate(m=Max('id'))['m'] or 0
    return JsonResponse({'processing': processing, 'latest_id': latest_id})


EXPORT_FIELDS = [
    "ID", "Title", "Authors", "Year", "Category", "Tags",
    "Paper Link", "GitHub Link", "Summary", "Why Useful",
    "Status", "Source", "Uploaded By", "Created At",
]


def _get_export_queryset(request):
    return (
        _apply_filters(
            accessible_posters(request.user, ResearchPoster.objects.select_related("uploaded_by")),
            request.GET,
            favorite_user=request.user,
        )
        .filter(validation_status="approved")
        .order_by("-created_at")
    )


def _poster_to_row(poster):
    return [
        poster.id, poster.title, poster.authors,
        poster.publication_year or "",
        poster.get_category_display(), poster.tags or "",
        poster.paper_link or "", poster.github_link or "",
        poster.summary, poster.why_useful or "",
        poster.validation_status,
        poster.get_upload_source_display(),
        poster.uploaded_by.username if poster.uploaded_by else "",
        poster.created_at.strftime("%Y-%m-%d %H:%M"),
    ]


def _csv_safe_cell(value):
    if isinstance(value, str) and value:
        if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


@login_required(login_url="login")
def export_approved_csv(request):
    posters  = _get_export_queryset(request)
    filename = f"approved_posters_{timezone.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write(BOM_UTF8)
    writer = csv.writer(response, delimiter=";")
    writer.writerow(EXPORT_FIELDS)
    for p in posters:
        writer.writerow(_csv_safe_cell(value) for value in _poster_to_row(p))
    return response


@login_required(login_url="login")
def export_approved_json(request):
    posters  = _get_export_queryset(request)
    filename = f"approved_posters_{timezone.now().strftime('%Y%m%d_%H%M%S')}.json"
    payload  = {
        "exported_at": timezone.now().isoformat(),
        "count":       posters.count(),
        "items": [
            {
                "id":            p.id,
                "title":            p.title,
                "authors":          p.authors,
                "publication_year": p.publication_year,
                "category":         p.get_category_display(),
                "tags":          p.tags or "",
                "paper_link":    p.paper_link or "",
                "github_link":   p.github_link or "",
                "description":   p.summary,
                "why_useful":    p.why_useful or "",
                "status":        p.validation_status,
                "upload_source": p.get_upload_source_display(),
                "uploaded_by":   p.uploaded_by.username if p.uploaded_by else "",
                "created_at":    p.created_at.isoformat(),
                "updated_at":    p.updated_at.isoformat(),
            }
            for p in posters
        ],
    }
    response = HttpResponse(
        json.dumps(payload, ensure_ascii=False, indent=2),
        content_type="application/json",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response

@_admin_required
def group_list(request):
    search = (request.GET.get("q") or "").strip()
    groups_qs = ResearchGroup.objects.prefetch_related("memberships__user").all()
    if search:
        groups_qs = groups_qs.filter(
            Q(name__icontains=search)
            | Q(interests__text__icontains=search)
            | Q(memberships__user__username__icontains=search)
            | Q(memberships__user__first_name__icontains=search)
            | Q(memberships__user__last_name__icontains=search)
        ).distinct()

    try:
        per_page = int(request.GET.get("per_page", 25))
    except ValueError:
        per_page = 25
    per_page = max(5, min(per_page, 100))
    paginator = Paginator(groups_qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    User = get_user_model()
    all_users = User.objects.filter(is_active=True).order_by("username")
    pending_users = (
        User.objects
        .filter(is_active=True, group_memberships__isnull=True, pending_dismissal__isnull=True)
        .order_by("-date_joined", "username")
    )

    paginate_qs_base = f"q={search}" if search else ""
    return render(request, "groups/group_list.html", {
        "groups": page_obj.object_list,
        "all_users": all_users,
        "pending_users": pending_users,
        "search_query": search,
        "page_obj": page_obj,
        "per_page": per_page,
        "paginate_label": "groups",
        "paginate_qs_base": paginate_qs_base,
    })


def _split_interests(blob):
    if not blob:
        return []
    return [ln.strip() for ln in blob.splitlines() if ln.strip()]


@_admin_required
def group_create(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    name = (request.POST.get("name") or "").strip()
    interest_lines = _split_interests(request.POST.get("research_interests"))
    if not name:
        messages.error(request, "Group name is required.")
        return redirect("group_list")
    if ResearchGroup.objects.filter(name__iexact=name).exists():
        messages.error(request, f'Group "{name}" already exists.')
        return redirect("group_list")
    group = ResearchGroup.objects.create(name=name)
    for line in interest_lines:
        ResearchInterest.objects.create(group=group, text=line)
    messages.success(request, f'Group "{name}" created.')
    return redirect("group_list")


@_admin_required
def group_edit(request, group_id):
    group = get_object_or_404(ResearchGroup, pk=group_id)
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Group name is required.")
            return redirect("group_list")
        dup = ResearchGroup.objects.filter(name__iexact=name).exclude(pk=group.pk).exists()
        if dup:
            messages.error(request, f'Group "{name}" already exists.')
            return redirect("group_list")
        group.name = name
        group.save()
        messages.success(request, f'Group "{group.name}" updated.')
        return redirect("group_edit", group_id=group.pk)
    User = get_user_model()
    all_users = User.objects.filter(is_active=True).order_by("username")
    return render(request, "groups/group_edit.html", {
        "group": group,
        "all_users": all_users,
        "current_members": list(group.memberships.select_related("user").all()),
        "interests": list(group.interests.all()),
    })


@_admin_required
def interest_add(request, group_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    group = get_object_or_404(ResearchGroup, pk=group_id)
    lines = _split_interests(request.POST.get("text"))
    if not lines:
        messages.error(request, "Research interest text is required.")
        return redirect("group_edit", group_id=group.pk)
    for line in lines:
        ResearchInterest.objects.create(group=group, text=line)
    label = "interests" if len(lines) > 1 else "interest"
    messages.success(request, f"Added {len(lines)} research {label}.")
    return redirect("group_edit", group_id=group.pk)


@_admin_required
def interest_edit(request, interest_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    interest = get_object_or_404(ResearchInterest, pk=interest_id)
    text = (request.POST.get("text") or "").strip()
    if not text:
        messages.error(request, "Research interest cannot be empty.")
        return redirect("group_edit", group_id=interest.group_id)
    interest.text = text
    interest.save(update_fields=["text", "updated_at"])
    messages.success(request, "Research interest updated.")
    return redirect("group_edit", group_id=interest.group_id)


@_admin_required
def interest_delete(request, interest_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    interest = get_object_or_404(ResearchInterest, pk=interest_id)
    group_id = interest.group_id
    interest.delete()
    messages.success(request, "Research interest removed.")
    return redirect("group_edit", group_id=group_id)


@_admin_required
def group_delete(request, group_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    group = get_object_or_404(ResearchGroup, pk=group_id)
    name = group.name
    group.delete()
    messages.success(request, f'Group "{name}" deleted.')
    return redirect("group_list")


@_admin_required
def group_add_member(request, group_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    group = get_object_or_404(ResearchGroup, pk=group_id)
    User = get_user_model()

    user_ids = request.POST.getlist("user_ids") or [request.POST.get("user_id")]
    user_ids = [uid for uid in user_ids if uid]
    if not user_ids:
        messages.error(request, "Select at least one user.")
        return redirect("group_edit", group_id=group.pk)

    users = User.objects.filter(pk__in=user_ids)
    added, already = [], []
    for user in users:
        is_first_group = not UserGroupMembership.objects.filter(user=user).exists()
        _, created = UserGroupMembership.objects.get_or_create(
            user=user, group=group,
            defaults={"is_primary": is_first_group},
        )
        (added if created else already).append(user)

    PendingAssignmentDismissal.objects.filter(user__in=added).delete()

    if added:
        names = ", ".join((u.get_full_name().title() or u.username) for u in added)
        messages.success(request, f'Added to "{group.name}": {names}.')
    if already:
        names = ", ".join((u.get_full_name().title() or u.username) for u in already)
        messages.info(request, f'Already members of "{group.name}": {names}.')
    return redirect("group_edit", group_id=group.pk)


@_admin_required
def dismiss_pending_user(request, user_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    User = get_user_model()
    target = get_object_or_404(User, pk=user_id)
    PendingAssignmentDismissal.objects.get_or_create(
        user=target,
        defaults={"dismissed_by": request.user},
    )
    name = target.get_full_name().title() or target.username
    messages.success(request, f"{name} skipped — won't appear in the pending list.")
    return redirect("group_list")


@_admin_required
def group_remove_member(request, group_id, user_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    UserGroupMembership.objects.filter(group_id=group_id, user_id=user_id).delete()
    messages.success(request, "Member removed.")
    return redirect("group_edit", group_id=group_id)


@_admin_required
def group_set_primary(request, group_id, user_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    membership = get_object_or_404(UserGroupMembership, group_id=group_id, user_id=user_id)
    membership.is_primary = True
    membership.save()
    messages.success(request, "Primary group updated.")
    return redirect("group_edit", group_id=group_id)


@_groups_required
def set_my_primary_group(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    try:
        data = json.loads(request.body)
        group_id = data.get("group_id")
    except (json.JSONDecodeError, AttributeError):
        group_id = request.POST.get("group_id")
    membership = get_object_or_404(
        UserGroupMembership, user=request.user, group_id=group_id,
    )
    membership.is_primary = True
    membership.save()
    return JsonResponse({"success": True})

@_groups_required
def poster_why_useful_for_group(request, poster_id):

    poster = get_accessible_poster_or_404(request.user, poster_id)
    group_id = request.GET.get("group_id", "")

    if not group_id:
        return JsonResponse({"why_useful": poster.why_useful or ""})

    group = get_object_or_404(ResearchGroup, pk=group_id)

    if not can_access_group(request.user, group.pk):
        return JsonResponse({"error": "Forbidden"}, status=403)

    if not poster.groups.filter(pk=group.pk).exists():
        return JsonResponse({"error": "Group not assigned to this paper"}, status=403)

    research_interests = group.research_interests_text
    if not research_interests:
        PosterGroupWhyUseful.objects.filter(poster=poster, group=group).delete()
        return JsonResponse({"why_useful": "", "no_interests": True})

    cached = PosterGroupWhyUseful.objects.filter(poster=poster, group=group).first()
    if cached and cached.why_useful:
        return JsonResponse({"why_useful": cached.why_useful, "cached": True})

    why = generate_why_useful(
        summary=poster.summary,
        user_notes=poster.notes.strip() if poster.notes else "",
        user_tags="",
        research_interests=research_interests,
    )

    PosterGroupWhyUseful.objects.update_or_create(
        poster=poster, group=group,
        defaults={"why_useful": why},
    )

    return JsonResponse({"why_useful": why, "cached": False})


@_groups_required
def update_poster_groups(request, poster_id):

    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    poster = get_accessible_poster_or_404(request.user, poster_id)

    user_group_ids = set(
        UserGroupMembership.objects
        .filter(user=request.user)
        .values_list("group_id", flat=True)
    )
    current_group_ids = set(poster.groups.values_list("pk", flat=True))

    try:
        body = json.loads(request.body or "{}")
    except (json.JSONDecodeError, ValueError):
        body = {}
    raw_ids = body.get("group_ids", [])
    if not isinstance(raw_ids, list):
        raw_ids = []

    requested = set()
    for gid in raw_ids:
        try:
            requested.add(int(gid))
        except (ValueError, TypeError):
            continue

    allowed_new = requested & user_group_ids
    preserved = current_group_ids - user_group_ids
    if not allowed_new and not preserved:
        return JsonResponse({"success": False, "message": "Select at least one group."}, status=400)
    final_ids = sorted(allowed_new | preserved)

    poster.groups.set(final_ids)

    final_groups = list(
        ResearchGroup.objects
        .filter(pk__in=final_ids)
        .order_by("name")
        .values("pk", "name")
    )

    ActivityLog.objects.create(
        user=request.user, poster=poster, action="updated",
        poster_title=poster.title,
        details=f'Groups updated for "{poster.title}"',
    )

    return JsonResponse({
        "success": True,
        "groups": [{"id": g["pk"], "name": g["name"]} for g in final_groups],
        "user_groups": [{"id": g["pk"], "name": g["name"]} for g in final_groups if g["pk"] in user_group_ids],
    })


@login_required(login_url="login")
def my_groups(request):
    memberships = (
        UserGroupMembership.objects
        .filter(user=request.user)
        .select_related("group")
        .order_by("-is_primary", "group__name")
    )
    from urllib.parse import urlparse
    referer_path = urlparse(request.META.get("HTTP_REFERER", "")).path or ""
    back_target = "dashboard"
    if referer_path == "/" or referer_path.startswith("/?"):
        back_target = "upload"
    elif referer_path.startswith("/conference"):
        back_target = "conference"
    return render(request, "my_groups.html", {
        "memberships": memberships,
        "back_target": back_target,
    })


@_superuser_required
def user_admin_list(request):
    from .access import GROUP_MANAGER_ROLE
    User = get_user_model()
    search = (request.GET.get("q") or "").strip()
    users_qs = (
        User.objects
        .filter(is_active=True)
        .prefetch_related("group_memberships__group", "groups")
        .annotate(uploaded_count=Count("uploaded_posters", distinct=True))
        .order_by("-is_superuser", "-is_staff", "username")
    )
    if search:
        users_qs = users_qs.filter(
            Q(username__icontains=search)
            | Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(email__icontains=search)
            | Q(group_memberships__group__name__icontains=search)
        ).distinct()

    try:
        per_page = int(request.GET.get("per_page", 25))
    except ValueError:
        per_page = 25
    per_page = max(5, min(per_page, 100))
    paginator = Paginator(users_qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    users = list(page_obj.object_list)
    for u in users:
        u.group_names = [m.group.name for m in u.group_memberships.all()]
        u.is_group_manager_role = any(g.name == GROUP_MANAGER_ROLE for g in u.groups.all())

    paginate_qs_base = f"q={search}" if search else ""
    return render(request, "users/user_list.html", {
        "users": users,
        "search_query": search,
        "page_obj": page_obj,
        "per_page": per_page,
        "paginate_label": "users",
        "paginate_qs_base": paginate_qs_base,
    })


@_superuser_required
def user_toggle_superuser(request, user_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    User = get_user_model()
    target = get_object_or_404(User, pk=user_id)
    if target.pk == request.user.pk:
        messages.error(request, "You cannot change your own admin status.")
        return redirect("user_admin_list")
    name = target.get_full_name().title() or target.username
    if target.is_superuser:
        active_supers = User.objects.filter(is_active=True, is_superuser=True).count()
        if active_supers <= 1:
            messages.error(request, "Cannot demote the last remaining admin.")
            return redirect("user_admin_list")
        target.is_superuser = False
        target.is_staff = False
        target.save(update_fields=["is_superuser", "is_staff"])
        messages.success(request, f"{name} demoted from admin.")
    else:
        target.is_superuser = True
        target.is_staff = True
        target.save(update_fields=["is_superuser", "is_staff"])
        messages.success(request, f"{name} promoted to admin.")
    return redirect("user_admin_list")


@_superuser_required
def user_toggle_group_manager(request, user_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    from django.contrib.auth.models import Group
    from .access import GROUP_MANAGER_ROLE
    User = get_user_model()
    target = get_object_or_404(User, pk=user_id)
    if target.pk == request.user.pk:
        messages.error(request, "You cannot change your own group-manager status.")
        return redirect("user_admin_list")
    role_group, _ = Group.objects.get_or_create(name=GROUP_MANAGER_ROLE)
    name = target.get_full_name().title() or target.username
    if target.groups.filter(pk=role_group.pk).exists():
        target.groups.remove(role_group)
        messages.success(request, f"{name} removed from group managers.")
    else:
        target.groups.add(role_group)
        messages.success(request, f"{name} added to group managers.")
    return redirect("user_admin_list")


@_superuser_required
def user_delete(request, user_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    User = get_user_model()
    target = get_object_or_404(User, pk=user_id)
    if target.pk == request.user.pk:
        messages.error(request, "You cannot delete your own account.")
        return redirect("user_admin_list")
    if target.is_superuser:
        messages.error(request, "Cannot delete a superuser from this page.")
        return redirect("user_admin_list")
    name = target.get_full_name().title() or target.username
    target.delete()
    messages.success(request, f"{name} removed. Any papers they uploaded were preserved.")
    return redirect("user_admin_list")


@csrf_exempt
def telegram_webhook(request):
    if request.method != "POST":
        return HttpResponse("Telegram Bot Active", status=200)

    telegram_secret = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")
    if telegram_secret:
        presented = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(presented, telegram_secret):
            logger.warning("Rejected a Telegram webhook call with an invalid secret token")
            return HttpResponse("Forbidden", status=403)

    try:
        data = json.loads(request.body.decode("utf-8"))

        if "callback_query" in data:
            cb      = data["callback_query"]
            chat_id = cb["message"]["chat"]["id"]
            cb_data = cb.get("data", "")
            answer_telegram_callback(cb["id"])
            _handle_pending_callback("telegram", chat_id, cb_data)
            return HttpResponse("OK", status=200)

        if "message" not in data:
            return HttpResponse("OK")

        chat_id = data["message"]["chat"]["id"]
        message = data["message"]

        if "photo" in message:
            if _require_link_or_notify("telegram", chat_id) is None:
                return HttpResponse("OK", status=200)
            file_id = message["photo"][-1]["file_id"]
            caption = message.get("caption", "").strip()
            download_and_handle_media_task.delay(
                "telegram", chat_id,
                file_id,
                f"tg_{file_id}.jpg",
                caption=caption or None,
            )

        elif "document" in message:
            if _require_link_or_notify("telegram", chat_id) is None:
                return HttpResponse("OK", status=200)
            doc     = message["document"]
            ext     = get_file_extension(doc.get("file_name", ""), doc.get("mime_type"))
            caption = message.get("caption", "").strip()
            download_and_handle_media_task.delay(
                "telegram", chat_id,
                doc["file_id"],
                f"tg_doc_{doc['file_id']}.{ext}",
                caption=caption or None,
            )

        elif "text" in message:
            text = message["text"].strip()
            link_payload = _extract_link_payload(text, "telegram")
            if link_payload is not None:
                _handle_link_command("telegram", chat_id, link_payload)
            elif not _handle_pending_validation_text("telegram", chat_id, text):
                if text == "/start":
                    send_message("telegram", chat_id, MESSAGE_TEMPLATES["welcome"]["telegram"])
                    _set_welcomed("telegram", chat_id)
                elif text == "/help":
                    send_message("telegram", chat_id, MESSAGE_TEMPLATES["help"]["telegram"])
                elif text == "/dashboard":
                    _handle_dashboard_command("telegram", chat_id)
                elif text.startswith("/search"):
                    _handle_search_command("telegram", chat_id, text[7:].strip())
                elif not _is_welcomed("telegram", chat_id):
                    _set_welcomed("telegram", chat_id)
                    send_message("telegram", chat_id, MESSAGE_TEMPLATES["welcome"]["telegram"])
                else:
                    send_message("telegram", chat_id, MESSAGE_TEMPLATES["fallback"]["telegram"])

        else:
            if not _is_welcomed("telegram", chat_id):
                _set_welcomed("telegram", chat_id)
                send_message("telegram", chat_id, MESSAGE_TEMPLATES["welcome"]["telegram"])

    except Exception as e:
        logger.error("Telegram webhook error: %s", e)

    return HttpResponse("OK", status=200)


def _whatsapp_signature_is_valid(request):
    app_secret = getattr(settings, "WHATSAPP_APP_SECRET", "")
    if not app_secret:
        return True
    presented = request.headers.get("X-Hub-Signature-256", "")
    if not presented.startswith("sha256="):
        logger.warning("Rejected a WhatsApp webhook call with no payload signature")
        return False
    expected = hmac.new(app_secret.encode(), request.body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(presented[len("sha256="):], expected):
        logger.warning("Rejected a WhatsApp webhook call with an invalid payload signature")
        return False
    return True


@csrf_exempt
def whatsapp_webhook(request):
    if request.method == "GET":
        mode      = request.GET.get("hub.mode")
        token     = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")
        if mode == "subscribe" and WHATSAPP_VERIFY_TOKEN and hmac.compare_digest(token or "", WHATSAPP_VERIFY_TOKEN):
            return HttpResponse(challenge, status=200)
        return HttpResponse("Forbidden", status=403)

    if request.method != "POST":
        return HttpResponse("OK", status=200)

    if not _whatsapp_signature_is_valid(request):
        return HttpResponse("Forbidden", status=403)

    try:
        body    = json.loads(request.body.decode("utf-8"))
        changes = body.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})

        if "messages" not in changes:
            return HttpResponse("EVENT_RECEIVED", status=200)

        message  = changes["messages"][0]
        phone    = message["from"]
        msg_type = message["type"]

        if msg_type == "image":
            if _require_link_or_notify("whatsapp", phone) is None:
                return HttpResponse("EVENT_RECEIVED", status=200)
            image_id = message["image"]["id"]
            caption  = message["image"].get("caption", "").strip()
            download_and_handle_media_task.delay(
                "whatsapp", phone,
                image_id,
                f"wa_{image_id}.jpg",
                caption=caption or None,
            )

        elif msg_type == "document":
            if _require_link_or_notify("whatsapp", phone) is None:
                return HttpResponse("EVENT_RECEIVED", status=200)
            doc     = message["document"]
            ext     = get_file_extension(doc.get("filename", ""), doc.get("mime_type"))
            caption = doc.get("caption", "").strip()
            download_and_handle_media_task.delay(
                "whatsapp", phone,
                doc["id"],
                f"wa_doc_{doc['id']}.{ext}",
                caption=caption or None,
            )

        elif msg_type == "text":
            text_body = message["text"]["body"].strip()
            lower = text_body.lower()
            link_payload = _extract_link_payload(text_body, "whatsapp")
            if link_payload is not None:
                _handle_link_command("whatsapp", phone, link_payload)
            elif not _handle_pending_validation_text("whatsapp", phone, text_body):
                if lower in ("start", "hi", "hello", "ciao", "help", "aiuto", "aide"):
                    send_message("whatsapp", phone, MESSAGE_TEMPLATES["help"]["whatsapp"])
                elif lower in ("dashboard", "stats", "statistiche"):
                    _handle_dashboard_command("whatsapp", phone)
                elif lower.startswith("search"):
                    _handle_search_command("whatsapp", phone, text_body[6:].strip())
                else:
                    send_message("whatsapp", phone, MESSAGE_TEMPLATES["fallback"]["whatsapp"])

        elif msg_type == "interactive":
            btn_id = message.get("interactive", {}).get("button_reply", {}).get("id", "")
            _handle_pending_callback("whatsapp", phone, btn_id)

        return HttpResponse("EVENT_RECEIVED", status=200)

    except Exception as e:
        logger.error("WhatsApp webhook error: %s", e)
        return HttpResponse("ERROR", status=200)
