import logging
import os
import uuid
from contextlib import contextmanager

import redis
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, close_old_connections

logger = logging.getLogger(__name__)

LOCK_TIMEOUT = int(os.getenv("POSTER_PROCESSING_LOCK_TIMEOUT", "3600"))
STALE_PROCESSING_SECONDS = 600
BOT_MESSAGE_TTL = 86400
BOT_CONTEXT_TTL = 86400
BOT_STATE_TTL = 7200
LOCK_BUSY_RETRY_SECONDS = 30
STALE_LOCK_RETRY_SECONDS = 5

_UNLOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

_redis_client = None
_redis_client_pid = None


def _get_redis_client():
    global _redis_client, _redis_client_pid
    pid = os.getpid()
    if _redis_client is None or _redis_client_pid != pid:
        redis_url = os.environ.get("REDIS_CACHE_URL") or getattr(
            settings, "CELERY_BROKER_URL", "redis://redis:6379/0"
        )
        _redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
        _redis_client_pid = pid
    return _redis_client


def _lock_key(poster_id):
    return f"lock:poster:{poster_id}"


def _acquire_poster_lock(poster_id):
    client = _get_redis_client()
    key = _lock_key(poster_id)
    token = uuid.uuid4().hex
    acquired = bool(client.set(key, token, ex=LOCK_TIMEOUT, nx=True))
    return client, key, token, acquired


def _release_poster_lock(client, key, token):
    try:
        client.eval(_UNLOCK_SCRIPT, 1, key, token)
    except redis.RedisError as e:
        logger.warning("Could not release lock %s: %s", key, type(e).__name__)


def _get_poster(poster_id):
    from bot_engine.models import ResearchPoster

    return ResearchPoster.objects.filter(pk=poster_id).first()


def _resolve_poster(new_poster, poster_id):
    resolved = new_poster or _get_poster(poster_id)
    return resolved, (resolved.pk if resolved else poster_id)


def _is_stale_processing(poster_id):
    from django.utils import timezone

    try:
        poster = _get_poster(poster_id)
    except DatabaseError as e:
        logger.warning("Stale-processing check failed for poster %s: %s", poster_id, type(e).__name__)
        return False
    if not poster or poster.analysis_status != "processing":
        return False
    return (timezone.now() - poster.updated_at).total_seconds() > STALE_PROCESSING_SECONDS


def _force_break_lock(poster_id):
    try:
        _get_redis_client().delete(_lock_key(poster_id))
        logger.info("Force-broke stale lock for poster_id=%s", poster_id)
    except redis.RedisError as e:
        logger.warning("Could not force-break lock for poster_id=%s: %s", poster_id, type(e).__name__)


def _mark_poster_processing(poster_id):
    try:
        poster = _get_poster(poster_id)
        if not poster:
            return None
        if poster.analysis_status != "processing":
            poster.analysis_status = "processing"
            poster.save(update_fields=["analysis_status", "updated_at"])
        return poster
    except DatabaseError as e:
        logger.warning("Could not mark poster %s as processing: %s", poster_id, type(e).__name__)
        return None


def _mark_poster_failed(poster_id, title_fallback="Analysis Failed"):
    try:
        poster = _get_poster(poster_id)
        if not poster:
            return
        poster.analysis_status = "failed"
        if poster.title in ("Pending analysis…", "", None):
            poster.title = title_fallback
        poster.save(update_fields=["title", "analysis_status", "updated_at"])
    except DatabaseError as e:
        logger.warning("Could not mark poster %s as failed: %s", poster_id, type(e).__name__)


def _delete_temp_poster(poster_id, log_prefix):
    try:
        poster = _get_poster(poster_id)
    except DatabaseError as e:
        logger.warning("[%s] Could not load poster %s: %s", log_prefix, poster_id, type(e).__name__)
        return
    if not poster:
        return
    if poster.image:
        try:
            poster.image.delete(save=False)
        except (OSError, ValueError) as e:
            logger.warning("[%s] Could not delete image for poster %s: %s",
                           log_prefix, poster_id, type(e).__name__)
    try:
        poster.delete()
    except DatabaseError as e:
        logger.warning("[%s] Could not delete poster %s: %s", log_prefix, poster_id, type(e).__name__)


def _send_failed_with_retry(platform, recipient, poster_id):
    from bot_engine.views import MESSAGE_TEMPLATES, send_buttons, send_message

    text = MESSAGE_TEMPLATES["analysis_failed"][platform]
    sent = send_buttons(
        platform,
        recipient,
        text,
        [{"id": f"retry_{poster_id}", "title": "🔄 Retry", "text": "🔄 Retry Analysis"}],
    )
    if not sent and platform == "whatsapp":
        send_message(platform, recipient, text)


def _send_failed_with_retry_once(platform, recipient, poster_id):
    once_key = f"bot:failed-message:{platform}:{recipient}:{poster_id}"
    if not cache.add(once_key, "1", timeout=BOT_MESSAGE_TTL):
        logger.info("[Task:bot] failure message already sent for %s/%s/%s", platform, recipient, poster_id)
        return
    _send_failed_with_retry(platform, recipient, poster_id)


def _send_success_once(platform, recipient, poster):
    from bot_engine.views import _send_analysis_result, send_confirmation_buttons

    once_key = f"bot:success-message:{platform}:{recipient}:{poster.pk}"
    if not cache.add(once_key, "1", timeout=BOT_MESSAGE_TTL):
        logger.info("[Task:bot] success message already sent for %s/%s/%s", platform, recipient, poster.pk)
        return
    _send_analysis_result(platform, recipient, poster)
    send_confirmation_buttons(platform, recipient, poster.paper_link or "Not found")


def _clear_bot_ratelimit(platform, recipient):
    from bot_engine.views import _clear_ratelimit

    try:
        _clear_ratelimit(platform, recipient)
    except redis.RedisError as e:
        logger.warning("Could not clear rate limit for %s/%s: %s", platform, recipient, type(e).__name__)


@contextmanager
def _poster_lock(poster_id, log_prefix):
    try:
        client, key, token, acquired = _acquire_poster_lock(poster_id)
    except redis.RedisError:
        logger.exception("[%s] Could not acquire Redis lock for poster_id=%s", log_prefix, poster_id)
        raise
    try:
        yield client, key, token, acquired
    finally:
        if acquired:
            _release_poster_lock(client, key, token)
        close_old_connections()


def _handle_busy_lock(self, poster_id, log_prefix, on_exhausted):
    if _is_stale_processing(poster_id):
        logger.warning("[%s] poster_id=%s stale processing detected, breaking lock", log_prefix, poster_id)
        _force_break_lock(poster_id)
        close_old_connections()
        raise self.retry(countdown=STALE_LOCK_RETRY_SECONDS)
    if self.request.retries >= self.max_retries:
        logger.error("[%s] poster_id=%s lock retries exhausted, marking failed", log_prefix, poster_id)
        _mark_poster_failed(poster_id)
        on_exhausted()
        close_old_connections()
        return {"poster_id": poster_id, "status": "analysis_failed"}
    logger.info("[%s] poster_id=%s lock held, retrying later (attempt %d/%d)",
                log_prefix, poster_id, self.request.retries, self.max_retries)
    close_old_connections()
    raise self.retry(countdown=LOCK_BUSY_RETRY_SECONDS)


def _lookup_user(user_id):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=user_id).first() if user_id else None


@shared_task(bind=True, max_retries=5, default_retry_delay=15, acks_late=True, reject_on_worker_lost=True)
def process_poster_task(self, poster_id, user_notes=None, user_tags=None, source="web", user_id=None, group_ids=None):
    close_old_connections()

    with _poster_lock(poster_id, "Task:web") as (client, key, token, acquired):
        if not acquired:
            return _handle_busy_lock(self, poster_id, "Task:web", on_exhausted=lambda: None)

        try:
            from bot_engine.views import process_uploaded_poster

            activity_user = _lookup_user(user_id)

            logger.info("[Task:web] Starting analysis poster_id=%d source=%s", poster_id, source)

            poster = _mark_poster_processing(poster_id)
            if not poster:
                logger.error("[Task:web] Poster %d not found in DB", poster_id)
                return {"error": "poster not found"}

            new_poster, _, error = process_uploaded_poster(
                image_content=None,
                filename=None,
                source=source,
                existing_poster=poster,
                user_notes=user_notes,
                user_tags=user_tags,
                activity_user=activity_user,
                group_ids=group_ids,
            )

            if error == "no_text":
                _delete_temp_poster(poster_id, "Task:web")
                logger.info("[Task:web] poster_id=%d deleted (no_text)", poster_id)
                return {"poster_id": poster_id, "status": "rejected", "reason": "no_text"}

            if error == "duplicate":
                dup_id = new_poster.pk if new_poster else None
                if not new_poster or poster_id != new_poster.pk:
                    _delete_temp_poster(poster_id, "Task:web")
                logger.info("[Task:web] poster_id=%d -> duplicate of %s", poster_id, dup_id)
                return {"poster_id": poster_id, "status": "duplicate", "duplicate_id": dup_id}

            if error == "analysis_failed":
                _, resolved_id = _resolve_poster(new_poster, poster_id)
                _mark_poster_failed(resolved_id)
                logger.warning("[Task:web] poster_id=%d -> analysis_failed, kept in DB", poster_id)
                return {"poster_id": resolved_id, "status": "analysis_failed"}

            if error:
                _delete_temp_poster(poster_id, "Task:web")
                logger.warning("[Task:web] poster_id=%d -> AI failed, deleted: %s", poster_id, error)
                return {"poster_id": poster_id, "status": "error"}

            resolved, resolved_id = _resolve_poster(new_poster, poster_id)
            resolved_title = resolved.title if resolved else ""
            logger.info("[Task:web] Completed poster_id=%d title='%s'", resolved_id, resolved_title)
            return {"poster_id": resolved_id, "title": resolved_title, "status": "done"}

        except Exception as exc:
            logger.error("[Task:web] Error on poster_id=%d: %s", poster_id, exc)
            if self.request.retries >= self.max_retries:
                _mark_poster_failed(poster_id)
                return {"poster_id": poster_id, "status": "analysis_failed"}
            raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=5)
def download_and_handle_media_task(self, platform, recipient, media_id, filename, caption=None):
    close_old_connections()
    try:
        from bot_engine.views import (
            download_whatsapp_media,
            download_telegram_file,
            _handle_media_upload,
        )

        if platform == "whatsapp":
            media_content = download_whatsapp_media(media_id)
        else:
            media_content = download_telegram_file(media_id)

        close_old_connections()
        _handle_media_upload(platform, recipient, media_content, filename, caption=caption)

    except Exception as exc:
        logger.error("[Task:media] %s %s/%s: %s", platform, recipient, media_id, exc)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        from bot_engine.views import send_message
        send_message(platform, recipient, "❌ Error processing your image. Please try again.")
    finally:
        close_old_connections()


@shared_task(bind=True, max_retries=5, default_retry_delay=15, acks_late=True, reject_on_worker_lost=True)
def process_bot_poster_task(self, platform, recipient, poster_id, notes=None, tags=None, user_id=None):
    close_old_connections()

    cache_key = f"bot:{platform}:{recipient}"

    def _bot_cleanup():
        cache.delete(cache_key)
        _clear_bot_ratelimit(platform, recipient)

    with _poster_lock(poster_id, "Task:bot") as (client, key, token, acquired):
        if not acquired:
            return _handle_busy_lock(self, poster_id, "Task:bot", on_exhausted=_bot_cleanup)

        try:
            from bot_engine.models import UserGroupMembership
            from bot_engine.views import (
                process_uploaded_poster,
                send_message,
                MESSAGE_TEMPLATES,
            )

            logger.info("[Task:bot] Starting analysis poster_id=%d platform=%s", poster_id, platform)

            poster = _mark_poster_processing(poster_id)
            if not poster:
                _bot_cleanup()
                logger.error("[Task:bot] Poster %d not found in DB", poster_id)
                return {"error": "poster not found"}

            activity_user = _lookup_user(user_id)
            group_ids = []
            if activity_user:
                primary_group_id = (
                    UserGroupMembership.objects
                    .filter(user=activity_user, is_primary=True)
                    .values_list("group_id", flat=True)
                    .first()
                )
                if primary_group_id:
                    group_ids = [primary_group_id]

            new_poster, _, error = process_uploaded_poster(
                image_content=None,
                filename=None,
                source=platform,
                existing_poster=poster,
                user_notes=notes,
                user_tags=tags or None,
                activity_user=activity_user,
                group_ids=group_ids or None,
            )

            if error == "no_text":
                _delete_temp_poster(poster_id, "Task:bot")
                _bot_cleanup()
                send_message(platform, recipient, MESSAGE_TEMPLATES["no_text"][platform])
                logger.info("[Task:bot] poster_id=%d -> no_text", poster_id)
                return {"status": "no_text", "poster_id": poster_id}

            if error == "duplicate":
                dup_poster = new_poster
                if not dup_poster or poster_id != dup_poster.pk:
                    _delete_temp_poster(poster_id, "Task:bot")
                _bot_cleanup()

                dup_title = dup_poster.title if dup_poster else "Unknown"
                paper_link = getattr(dup_poster, "paper_link", "") or ""
                if paper_link and platform == "telegram":
                    link_line = f'🔗 <a href="{paper_link}">View paper</a>\n'
                elif paper_link:
                    link_line = f"🔗 {paper_link}\n"
                else:
                    link_line = ""
                msg = (
                    MESSAGE_TEMPLATES["duplicate"][platform]
                    .replace("{title}", dup_title)
                    .replace("{paper_link_line}", link_line)
                )
                send_message(platform, recipient, msg)
                logger.info("[Task:bot] poster_id=%d -> duplicate '%s'", poster_id, dup_title)
                return {"status": "duplicate", "poster_id": poster_id}

            if error == "analysis_failed":
                _, resolved_id = _resolve_poster(new_poster, poster_id)

                _mark_poster_failed(resolved_id)
                _bot_cleanup()
                cache.set(
                    f"bot:context:{resolved_id}",
                    {"platform": platform, "recipient": recipient},
                    timeout=BOT_CONTEXT_TTL,
                )
                _send_failed_with_retry_once(platform, recipient, resolved_id)
                logger.warning("[Task:bot] poster_id=%d -> analysis_failed, retry button sent", poster_id)
                return {"status": "analysis_failed", "poster_id": resolved_id}

            if error:
                _delete_temp_poster(poster_id, "Task:bot")
                _bot_cleanup()
                send_message(platform, recipient, "❌ Analysis could not be completed. Please try sending the image again.")
                logger.warning("[Task:bot] poster_id=%d -> error: %s", poster_id, error)
                return {"status": "error", "poster_id": poster_id}

            resolved, _ = _resolve_poster(new_poster, poster_id)
            if not resolved:
                _bot_cleanup()
                logger.error("[Task:bot] Resolved poster missing after processing poster_id=%d", poster_id)
                return {"status": "error", "poster_id": poster_id}

            logger.info("[Task:bot] Completed poster_id=%d title='%s'", resolved.pk, resolved.title)

            _send_success_once(platform, recipient, resolved)
            cache.set(
                cache_key,
                {"poster_id": resolved.pk, "state": "awaiting_confirmation"},
                timeout=BOT_STATE_TTL,
            )
            _clear_bot_ratelimit(platform, recipient)
            return {"status": "done", "poster_id": resolved.pk, "title": resolved.title}

        except Exception as exc:
            logger.error("[Task:bot] Error on poster_id=%d: %s", poster_id, exc)
            _clear_bot_ratelimit(platform, recipient)

            if self.request.retries >= self.max_retries:
                _mark_poster_failed(poster_id)
                cache.delete(cache_key)
                _send_failed_with_retry_once(platform, recipient, poster_id)
                return {"status": "analysis_failed", "poster_id": poster_id}

            raise self.retry(exc=exc)
