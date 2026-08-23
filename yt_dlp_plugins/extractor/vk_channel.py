import html
import os
import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError


class VKChannelPostIE(InfoExtractor):
    """Resolve VK Messenger/Channels post links to their attached VK video.

    VK's /im/channels/<peer>?cmid=<id> URLs are message permalinks, not video
    URLs, so yt-dlp's normal VK extractor cannot handle them directly.  This
    extractor first tries the official VK messages API when VK_ACCESS_TOKEN is
    configured and otherwise attempts a browser-like read of public channel
    pages.  Once an attachment is resolved, downloading is delegated back to
    yt-dlp's maintained VK extractor.
    """

    IE_NAME = "vk:channel-post"
    _VALID_URL = r"https?://(?:www\.)?vk\.(?:com|ru)/im/channels/(?P<peer>-?\d+)(?:\?(?P<query>[^#]*))?(?:#.*)?$"

    def _real_extract(self, url):
        match = self._match_valid_url(url)
        peer_id = int(match.group("peer"))
        query = parse_qs(match.group("query") or "")
        raw_cmid = (query.get("cmid") or [""])[0]
        if not str(raw_cmid).isdigit():
            raise ExtractorError("В ссылке VK Channels отсутствует корректный параметр cmid.", expected=True)
        cmid = int(raw_cmid)
        display_id = f"{peer_id}_{cmid}"

        attachment = self._resolve_with_vk_api(peer_id, cmid, display_id)
        if attachment is None:
            attachment = self._resolve_public_page(url, cmid)

        if attachment is None:
            if os.environ.get("VK_ACCESS_TOKEN", "").strip():
                detail = "VK API не вернул видео-вложение для этого сообщения."
            else:
                detail = (
                    "VK не отдал содержимое публичного сообщения без авторизации. "
                    "Для таких ссылок можно добавить бесплатный VK_ACCESS_TOKEN; "
                    "обычные прямые ссылки VK Video продолжат работать без него."
                )
            raise ExtractorError(detail, expected=True)

        kind, owner_id, video_id, access_key, title = attachment
        canonical = f"https://vk.com/{kind}{owner_id}_{video_id}"
        if access_key:
            canonical += f"?access_key={quote(access_key, safe='')}"

        return self.url_result(
            canonical,
            ie="VK",
            video_id=f"{owner_id}_{video_id}",
            video_title=title or None,
        )

    def _resolve_with_vk_api(self, peer_id, cmid, display_id):
        token = os.environ.get("VK_ACCESS_TOKEN", "").strip()
        if not token:
            return None

        version = os.environ.get("VK_API_VERSION", "5.199").strip() or "5.199"
        body = urlencode({
            "peer_id": peer_id,
            "conversation_message_ids": cmid,
            "extended": 1,
            "access_token": token,
            "v": version,
        }).encode()
        response = self._download_json(
            "https://api.vk.com/method/messages.getByConversationMessageId",
            display_id,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            fatal=False,
            note="Resolving VK channel message",
            errnote="VK channel message lookup failed",
        )
        if not isinstance(response, dict) or response.get("error"):
            return None

        payload = response.get("response") or {}
        items = payload.get("items") or []
        for item in items:
            found = self._find_video_attachment(item)
            if found:
                return found
        return None

    @classmethod
    def _find_video_attachment(cls, node):
        if isinstance(node, list):
            for child in node:
                found = cls._find_video_attachment(child)
                if found:
                    return found
            return None

        if not isinstance(node, dict):
            return None

        attachment_type = str(node.get("type") or "").lower()
        if attachment_type in {"video", "clip"}:
            video = node.get(attachment_type)
            if isinstance(video, dict):
                ref = cls._video_ref(video, attachment_type)
                if ref:
                    return ref

        # Some API representations expose the media object without the outer
        # {"type": "video", "video": {...}} wrapper.
        if "owner_id" in node and "id" in node and any(
            marker in node for marker in ("duration", "player", "files", "first_frame")
        ):
            ref = cls._video_ref(node, "clip" if node.get("type") == "short_video" else "video")
            if ref:
                return ref

        # Prefer the fields where VK nests message attachments, replies and
        # forwards, then fall back to remaining nested containers.
        preferred = ("attachments", "reply_message", "fwd_messages", "forwarded_messages")
        for key in preferred:
            if key in node:
                found = cls._find_video_attachment(node[key])
                if found:
                    return found
        for key, child in node.items():
            if key in preferred:
                continue
            if isinstance(child, (dict, list)):
                found = cls._find_video_attachment(child)
                if found:
                    return found
        return None

    @staticmethod
    def _video_ref(video, kind):
        try:
            owner_id = int(video.get("owner_id"))
            video_id = int(video.get("id"))
        except (TypeError, ValueError):
            return None
        access_key = str(video.get("access_key") or "").strip()
        title = str(video.get("title") or "").strip()
        return ("clip" if kind == "clip" else "video", owner_id, video_id, access_key, title)

    def _resolve_public_page(self, url, cmid):
        try:
            from curl_cffi import requests as browser_requests
        except Exception:
            return None

        candidates = []
        parsed = urlparse(url)
        for host in ("vk.com", "vk.ru"):
            candidates.append(urlunparse(parsed._replace(netloc=host)))

        seen = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                response = browser_requests.get(
                    candidate,
                    impersonate="chrome",
                    timeout=30,
                    allow_redirects=True,
                    headers={
                        "accept-language": "ru-RU,ru;q=0.9,en;q=0.7",
                        "referer": "https://vk.com/",
                    },
                )
            except Exception:
                continue
            if response.status_code >= 400 or "badbrowser.php" in str(response.url):
                continue

            page = html.unescape(response.text or "")
            page = page.replace("\\/", "/").replace("\\u0026", "&")
            found = self._extract_ref_from_page(page, cmid)
            if found:
                return found
        return None

    @classmethod
    def _extract_ref_from_page(cls, page, cmid):
        if not page:
            return None

        markers = [
            f'"conversation_message_id":{cmid}',
            f'"conversation_message_id": {cmid}',
            f'"cmid":{cmid}',
            f'"cmid": {cmid}',
            f"cmid={cmid}",
            f"cmid%3D{cmid}",
        ]
        windows = []
        for marker in markers:
            start = 0
            while True:
                index = page.find(marker, start)
                if index < 0:
                    break
                windows.append(page[max(0, index - 120000): index + 180000])
                start = index + len(marker)
                if len(windows) >= 8:
                    break
            if len(windows) >= 8:
                break

        # Search cmid-local windows first.  If the public page has only one
        # video reference in total, accepting that unique reference is safe.
        for window in windows:
            refs = cls._collect_refs(window)
            if refs:
                return refs[0]

        refs = cls._collect_refs(page[:4_000_000])
        unique = []
        keys = set()
        for ref in refs:
            key = ref[0], ref[1], ref[2]
            if key not in keys:
                keys.add(key)
                unique.append(ref)
        return unique[0] if len(unique) == 1 else None

    @classmethod
    def _collect_refs(cls, text):
        refs = []

        direct = re.compile(
            r"https?://(?:www\.)?(?:vk\.com|vk\.ru|vkvideo\.ru)/(video|clip)(-?\d+)_(\d+)"
            r"(?:[^\s\"'<>]{0,300})?",
            re.I,
        )
        bare = re.compile(r"(?<![A-Za-z0-9_])(video|clip)(-?\d+)_(\d+)(?!\d)", re.I)

        for regex in (direct, bare):
            for match in regex.finditer(text):
                kind = match.group(1).lower()
                owner_id = int(match.group(2))
                video_id = int(match.group(3))
                nearby = text[match.start(): min(len(text), match.end() + 500)]
                key_match = re.search(
                    r"(?:access_key(?:=|%3D|\\?\"?\s*:\s*\"?))([A-Za-z0-9_-]{6,128})",
                    nearby,
                    re.I,
                )
                access_key = unquote(key_match.group(1)) if key_match else ""
                refs.append((kind, owner_id, video_id, access_key, ""))
                if len(refs) >= 30:
                    return refs

        # Handle serialized message attachments where URL text is absent.
        for type_match in re.finditer(r'"type"\s*:\s*"(video|clip)"', text, re.I):
            chunk = text[max(0, type_match.start() - 1000): type_match.start() + 6000]
            owner_match = re.search(r'"owner_id"\s*:\s*(-?\d+)', chunk)
            id_match = re.search(r'"id"\s*:\s*(\d+)', chunk)
            if not owner_match or not id_match:
                continue
            key_match = re.search(r'"access_key"\s*:\s*"([^"\\]+)', chunk)
            title_match = re.search(r'"title"\s*:\s*"([^"\\]{1,300})', chunk)
            refs.append((
                type_match.group(1).lower(),
                int(owner_match.group(1)),
                int(id_match.group(1)),
                unquote(key_match.group(1)) if key_match else "",
                html.unescape(title_match.group(1)) if title_match else "",
            ))
            if len(refs) >= 30:
                break
        return refs
