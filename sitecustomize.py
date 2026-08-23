"""Runtime compatibility hooks for Railway deployment.

Features:
- Optional TikTok cookies from TIKTOK_COOKIES_B64.
- Normalize vk.ru mirror links to vk.com before yt-dlp extraction.
- Resolve VK Channels message permalinks (/im/channels/...?...cmid=...)
  through the official VK API when VK_ACCESS_TOKEN is configured.

Secrets stay in Railway environment variables and are never logged.
"""
import base64
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse


VK_API_VERSION = os.environ.get("VK_API_VERSION", "5.199").strip() or "5.199"
VK_ACCESS_TOKEN = os.environ.get("VK_ACCESS_TOKEN", "").strip()
VK_CHANNEL_RE = re.compile(r"^/im/channels/(?P<peer>-?\d+)$", re.I)


def _prepare_cookiefile() -> str | None:
    raw = os.environ.get("TIKTOK_COOKIES_B64", "").strip()
    if not raw:
        return None
    try:
        data = base64.b64decode(raw, validate=True)
        text = data.decode("utf-8").replace("\r\n", "\n")
        if not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
            raise ValueError("cookies must be in Netscape format")
        path = Path("/tmp/tiktok-cookies.txt")
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(0o600)
        return str(path)
    except Exception as exc:
        print(f"TIKTOK_COOKIES_B64 ignored: {type(exc).__name__}: {exc}", flush=True)
        return None


def _normalize_vk_url(url: str) -> str:
    """Normalize VK mirror hosts without changing path/query semantics."""
    if not isinstance(url, str):
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    host = (parsed.hostname or "").lower()
    if host in {"vk.ru", "www.vk.ru", "m.vk.ru"}:
        netloc = "vk.com"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))
    return url


def _parse_vk_channel_permalink(url: str):
    """Return (peer_id, cmid) for VK Channels permalink, otherwise None."""
    try:
        parsed = urlparse(_normalize_vk_url(url))
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host not in {"vk.com", "www.vk.com", "m.vk.com"}:
        return None
    match = VK_CHANNEL_RE.match(parsed.path.rstrip("/"))
    if not match:
        return None
    cmid_raw = (parse_qs(parsed.query).get("cmid") or [""])[0]
    if not str(cmid_raw).isdigit():
        return None
    return int(match.group("peer")), int(cmid_raw)


def _vk_api(method: str, params: dict):
    """Call official VK API without putting the access token in the URL."""
    if not VK_ACCESS_TOKEN:
        raise RuntimeError("VK_ACCESS_TOKEN is not configured")
    import requests

    data = dict(params)
    data.update({"access_token": VK_ACCESS_TOKEN, "v": VK_API_VERSION})
    try:
        response = requests.post(
            f"https://api.vk.com/method/{method}",
            data=data,
            timeout=25,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"VK API network error: {type(exc).__name__}") from None

    if payload.get("error"):
        error = payload["error"] or {}
        code = error.get("error_code", "unknown")
        message = re.sub(r"\s+", " ", str(error.get("error_msg") or "VK API error")).strip()
        raise RuntimeError(f"VK API error {code}: {message}")
    return payload.get("response")


def _find_video_attachment(message: dict):
    """Find first video attachment, including common nested wall/reply structures."""
    if not isinstance(message, dict):
        return None

    for attachment in message.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        kind = attachment.get("type")
        if kind == "video" and isinstance(attachment.get("video"), dict):
            return attachment["video"]
        nested = attachment.get(kind) if kind else None
        if isinstance(nested, dict):
            found = _find_video_attachment(nested)
            if found:
                return found

    for key in ("reply_message", "fwd_messages", "copy_history"):
        nested = message.get(key)
        if isinstance(nested, dict):
            found = _find_video_attachment(nested)
            if found:
                return found
        elif isinstance(nested, list):
            for item in nested:
                found = _find_video_attachment(item)
                if found:
                    return found
    return None


def _resolve_vk_channel_video(url: str):
    """Resolve VK Channels message permalink to a direct video URL + title.

    VK Channels permalinks point to a message, not directly to media. The official
    messages.getByConversationMessageId method is therefore used to obtain the
    attachment, then video.get provides the playable file URLs.
    """
    parsed = _parse_vk_channel_permalink(url)
    if not parsed:
        return None
    if not VK_ACCESS_TOKEN:
        raise RuntimeError(
            "Это ссылка на сообщение VK-канала, а не прямая ссылка на видео. "
            "Для автоматического извлечения таких ссылок добавь бесплатную переменную "
            "VK_ACCESS_TOKEN в Railway."
        )

    peer_id, cmid = parsed
    response = _vk_api(
        "messages.getByConversationMessageId",
        {
            "peer_id": peer_id,
            "conversation_message_ids": str(cmid),
            "extended": 1,
        },
    )
    if isinstance(response, dict):
        items = response.get("items") or []
    elif isinstance(response, list):
        items = response
    else:
        items = []
    if not items:
        raise RuntimeError("VK API не вернул сообщение канала или у токена нет доступа к нему.")

    video = _find_video_attachment(items[0])
    if not video:
        raise RuntimeError("В этом сообщении VK-канала не найдено видео-вложение.")

    owner_id = video.get("owner_id")
    video_id = video.get("id")
    if owner_id is None or video_id is None:
        raise RuntimeError("VK API вернул видео без owner_id/id.")

    access_key = (video.get("access_key") or "").strip()
    video_ref = f"{owner_id}_{video_id}" + (f"_{access_key}" if access_key else "")
    details = _vk_api("video.get", {"videos": video_ref, "extended": 0})
    detail_items = (details or {}).get("items") if isinstance(details, dict) else None
    detail = (detail_items or [video])[0]
    files = detail.get("files") or {}

    media_url = ""
    for quality in ("1080", "720", "480", "360", "240", "144"):
        if files.get(f"mp4_{quality}"):
            media_url = files[f"mp4_{quality}"]
            break
    if not media_url:
        media_url = files.get("hls") or files.get("dash_sep") or files.get("dash_webm") or ""
    if not media_url:
        player = detail.get("player") or video.get("player")
        if player:
            return player, (detail.get("title") or video.get("title") or "VK video")
        raise RuntimeError("VK API не вернул доступный файл видео.")

    title = re.sub(r"\s+", " ", str(detail.get("title") or video.get("title") or "VK video")).strip()
    return media_url, title


_cookiefile = _prepare_cookiefile()

try:
    import yt_dlp

    _original_init = yt_dlp.YoutubeDL.__init__
    _original_extract_info = yt_dlp.YoutubeDL.extract_info

    def _patched_init(self, params=None, auto_init=True):
        params = dict(params or {})
        if _cookiefile:
            params.setdefault("cookiefile", _cookiefile)
        return _original_init(self, params, auto_init)

    def _patched_extract_info(self, url, *args, **kwargs):
        original_url = url
        if isinstance(url, str):
            url = _normalize_vk_url(url)

        channel_link = isinstance(url, str) and _parse_vk_channel_permalink(url)
        if channel_link:
            try:
                resolved = _resolve_vk_channel_video(url)
                if resolved:
                    url, title = resolved
                    extra_info = dict(kwargs.get("extra_info") or {})
                    extra_info.setdefault("title", title)
                    kwargs["extra_info"] = extra_info
                    kwargs.setdefault("force_generic_extractor", True)
                    print("VK channel permalink resolved through official VK API", flush=True)
            except Exception as resolver_exc:
                # First try yt-dlp itself in case support for this URL format was added.
                try:
                    return _original_extract_info(self, url, *args, **kwargs)
                except Exception:
                    message = str(resolver_exc).replace(VK_ACCESS_TOKEN, "***") if VK_ACCESS_TOKEN else str(resolver_exc)
                    raise yt_dlp.utils.DownloadError(message) from None

        return _original_extract_info(self, url, *args, **kwargs)

    yt_dlp.YoutubeDL.__init__ = _patched_init
    yt_dlp.YoutubeDL.extract_info = _patched_extract_info

    if _cookiefile:
        print("yt-dlp authenticated cookie fallback enabled", flush=True)
    print(
        "VK compatibility enabled: vk.ru mirror normalization; "
        f"channel resolver={'enabled' if VK_ACCESS_TOKEN else 'waiting-for-VK_ACCESS_TOKEN'}",
        flush=True,
    )
except Exception as exc:
    print(f"Could not enable runtime compatibility hooks: {type(exc).__name__}: {exc}", flush=True)
