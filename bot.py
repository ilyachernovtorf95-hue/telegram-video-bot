import html
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp

TOKEN = re.sub(r"\s+", "", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if ":" not in TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN has an invalid format")

API = f"https://api.telegram.org/bot{TOKEN}"
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
MAX_TELEGRAM_VIDEO_BYTES = int(os.environ.get("MAX_TELEGRAM_VIDEO_MB", "49")) * 1024 * 1024
SAFE_PART_BYTES = min(MAX_TELEGRAM_VIDEO_BYTES - 3 * 1024 * 1024, 46 * 1024 * 1024)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def redact_secret(text: str) -> str:
    text = str(text)
    if TOKEN:
        text = text.replace(TOKEN, "***")
    return re.sub(r"https://api\.telegram\.org/bot[^/\s]+", "https://api.telegram.org/bot***", text)


def tg(method: str, *, data=None, files=None, params=None, timeout=90):
    response = requests.post(f"{API}/{method}", data=data, files=files, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return payload["result"]


def send_message(chat_id: int, text: str):
    return tg("sendMessage", data={"chat_id": chat_id, "text": text})


def edit_message(chat_id: int, message_id: int, text: str):
    return tg("editMessageText", data={"chat_id": chat_id, "message_id": message_id, "text": text})


def send_chat_action(chat_id: int, action: str):
    try:
        tg("sendChatAction", data={"chat_id": chat_id, "action": action}, timeout=20)
    except Exception:
        pass


def send_photo(chat_id: int, path: Path, caption: str = ""):
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    with path.open("rb") as fh:
        return tg(
            "sendPhoto",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"photo": (path.name, fh, mime)},
            timeout=300,
        )


def probe_video_metadata(path: Path) -> tuple[int, int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or [{}]
    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    duration = max(1, int(round(float((payload.get("format") or {}).get("duration") or 0))))
    return width, height, duration


def send_video(chat_id: int, path: Path, caption: str = ""):
    width, height, duration = probe_video_metadata(path)
    data = {
        "chat_id": chat_id,
        "caption": caption[:1024],
        "supports_streaming": "true",
        "duration": str(duration),
    }
    if width > 0 and height > 0:
        data["width"] = str(width)
        data["height"] = str(height)

    with path.open("rb") as fh:
        return tg(
            "sendVideo",
            data=data,
            files={"video": (path.name, fh, "video/mp4")},
            timeout=600,
        )


def send_document(chat_id: int, path: Path, caption: str = ""):
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        return tg(
            "sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"document": (path.name, fh, mime)},
            timeout=600,
        )


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title or "Видео").strip()


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if host in {"youtu.be", "youtube.com", "m.youtube.com", "music.youtube.com"} or host.endswith(".youtube.com"):
        return "YouTube"
    if host == "vk.com" or host.endswith(".vk.com") or host == "vkvideo.ru" or host.endswith(".vkvideo.ru"):
        return "VK"
    if host == "tiktok.com" or host.endswith(".tiktok.com") or host == "vm.tiktok.com":
        return "TikTok"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "Instagram"
    if host == "threads.net" or host.endswith(".threads.net") or host == "threads.com" or host.endswith(".threads.com"):
        return "Threads"
    return "сайт"


def ytdlp_options(tmpdir: str, player_client: str | None = None) -> dict:
    options = {
        "format": (
            "best[ext=mp4][filesize<=46M]/"
            "best[filesize<=46M]/"
            "best[ext=mp4][height<=480]/"
            "best[height<=480]/"
            "best[ext=mp4][height<=360]/"
            "best[height<=360]/best"
        ),
        "outtmpl": str(Path(tmpdir) / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "verbose": True,
        "restrictfilenames": True,
        "merge_output_format": "mp4",
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10 * 1024 * 1024,
        "http_headers": {"User-Agent": USER_AGENT},
        "js_runtimes": {"node": {"path": "/usr/local/bin/node"}},
    }
    if player_client:
        options["extractor_args"] = {
            "youtube": {"player_client": [player_client]},
            "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]},
        }
    return options


def newest_download(tmpdir: str) -> Path:
    candidates = sorted(
        [
            p for p in Path(tmpdir).iterdir()
            if p.is_file() and not p.name.endswith((".part", ".ytdl"))
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("Downloaded file was not found")
    mp4s = [p for p in candidates if p.suffix.lower() == ".mp4"]
    return mp4s[0] if mp4s else candidates[0]


def download_direct_meta(url: str, tmpdir: str) -> tuple[Path, str]:
    """Fallback for public Instagram/Threads pages exposing og:video/og:image."""
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, allow_redirects=True)
    response.raise_for_status()
    body = response.text

    def find_meta(prop: str) -> str | None:
        patterns = [
            rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, body, re.IGNORECASE)
            if match:
                return html.unescape(match.group(1))
        return None

    media_url = find_meta("og:video") or find_meta("og:video:secure_url") or find_meta("og:image")
    if not media_url:
        raise RuntimeError("Public page does not expose downloadable media metadata")
    title = clean_title(find_meta("og:title") or "Media")
    media = requests.get(media_url, headers={"User-Agent": USER_AGENT, "Referer": url}, timeout=90, stream=True)
    media.raise_for_status()
    content_type = (media.headers.get("content-type") or "").split(";", 1)[0].lower()
    suffix = mimetypes.guess_extension(content_type) or (".mp4" if "video" in content_type else ".jpg")
    path = Path(tmpdir) / f"direct-media{suffix}"
    with path.open("wb") as fh:
        for chunk in media.iter_content(chunk_size=1024 * 1024):
            if chunk:
                fh.write(chunk)
    return path, title


def download_with_ytdlp(url: str, tmpdir: str) -> tuple[Path, str]:
    platform = detect_platform(url)
    errors = []

    if platform == "YouTube":
        attempts: list[str | None] = ["mweb", "web_safari", "android_vr"]
    else:
        attempts = [None]

    for player_client in attempts:
        try:
            label = player_client or "generic"
            print(f"DOWNLOAD_ATTEMPT platform={platform} client={label}", flush=True)
            with yt_dlp.YoutubeDL(ytdlp_options(tmpdir, player_client)) as ydl:
                info = ydl.extract_info(url, download=True)
                title = clean_title(info.get("title"))
            return newest_download(tmpdir), title
        except Exception as exc:
            label = player_client or "generic"
            message = f"{platform}/{label}: {type(exc).__name__}: {redact_secret(exc)}"
            errors.append(message)
            print("DOWNLOAD_ATTEMPT_FAILED:", message, flush=True)
            for p in Path(tmpdir).iterdir():
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass

    if platform in {"Instagram", "Threads"}:
        try:
            print(f"DOWNLOAD_FALLBACK platform={platform} method=og-meta", flush=True)
            return download_direct_meta(url, tmpdir)
        except Exception as exc:
            errors.append(f"{platform}/og-meta: {type(exc).__name__}: {redact_secret(exc)}")

    raise RuntimeError(" | ".join(errors))


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise RuntimeError("Could not determine video duration")
    return duration


def normalize_mp4_segment(raw_part: Path, index: int, tmpdir: str) -> Path:
    normalized = Path(tmpdir) / f"telegram-normalized-{index:03d}.mp4"
    cmd = [
        "ffmpeg", "-y", "-fflags", "+genpts", "-i", str(raw_part),
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
        str(normalized),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
    return normalized


def split_for_telegram(path: Path, tmpdir: str) -> list[Path]:
    size = path.stat().st_size
    if size <= MAX_TELEGRAM_VIDEO_BYTES:
        return [path]

    duration = probe_duration(path)
    segment_seconds = max(30, int(duration * SAFE_PART_BYTES / size * 0.82))
    pattern = str(Path(tmpdir) / "telegram-raw-part-%03d.mp4")
    cmd = [
        "ffmpeg", "-y", "-fflags", "+genpts", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-f", "segment", "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        "-segment_format", "mp4",
        "-segment_format_options", "movflags=+faststart",
        pattern,
    ]
    print(f"FAST_SPLIT size={size / 1024 / 1024:.1f}MB segment={segment_seconds}s", flush=True)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)

    raw_parts = sorted(Path(tmpdir).glob("telegram-raw-part-*.mp4"))
    if not raw_parts:
        raise RuntimeError("Video splitting produced no files")

    parts = [normalize_mp4_segment(part, index, tmpdir) for index, part in enumerate(raw_parts, 1)]
    oversized = [p for p in parts if p.stat().st_size > MAX_TELEGRAM_VIDEO_BYTES]
    if oversized:
        raise RuntimeError("A split part is still too large; retry with a lower source quality")

    metadata = []
    for p in parts:
        w, h, d = probe_video_metadata(p)
        metadata.append(f"{p.name}:{w}x{h}/{d}s/{p.stat().st_size/1024/1024:.1f}MB")
    print("FAST_SPLIT_RESULT " + ", ".join(metadata), flush=True)
    return parts


def safe_error_text(exc: Exception) -> str:
    text = re.sub(r"\s+", " ", redact_secret(exc)).strip()
    return text[-700:] if text else type(exc).__name__


def handle_message(message: dict):
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id:
        return

    if text.startswith("/start") or text.startswith("/help"):
        send_message(
            chat_id,
            "Пришли ссылку на видео или пост. Поддерживаю YouTube, VK, TikTok, Instagram, Threads и другие сайты через yt-dlp.",
        )
        return

    match = URL_RE.search(text)
    if not match:
        send_message(chat_id, "Пришли ссылку, начинающуюся с http:// или https://")
        return

    url = match.group(0).rstrip(".,;!?)\"]}")
    platform = detect_platform(url)
    status = send_message(chat_id, f"⏳ {platform}: скачиваю медиа…")
    status_id = status["message_id"]

    try:
        with tempfile.TemporaryDirectory(prefix="tg-video-") as tmpdir:
            path, title = download_with_ytdlp(url, tmpdir)
            suffix = path.suffix.lower()

            if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
                edit_message(chat_id, status_id, f"📤 {platform}: отправляю фото…")
                send_photo(chat_id, path, title)
            else:
                original_size = path.stat().st_size
                if original_size > MAX_TELEGRAM_VIDEO_BYTES:
                    edit_message(
                        chat_id,
                        status_id,
                        f"⚡ {platform}: файл {original_size / 1024 / 1024:.1f} МБ. Быстро делю на части без потери качества…",
                    )
                    parts = split_for_telegram(path, tmpdir)
                else:
                    parts = [path]

                total = len(parts)
                for index, part in enumerate(parts, 1):
                    send_chat_action(chat_id, "upload_video")
                    if total > 1:
                        edit_message(chat_id, status_id, f"📤 {platform}: отправляю часть {index}/{total}…")
                        caption = f"{title}\nЧасть {index}/{total}"
                    else:
                        edit_message(chat_id, status_id, f"📤 {platform}: отправляю видео…")
                        caption = title

                    if part.suffix.lower() == ".mp4":
                        send_video(chat_id, part, caption)
                    else:
                        send_document(chat_id, part, caption)

        try:
            tg("deleteMessage", data={"chat_id": chat_id, "message_id": status_id})
        except Exception:
            pass
    except Exception as exc:
        print("DOWNLOAD_ERROR:", safe_error_text(exc), flush=True)
        error_text = safe_error_text(exc)
        try:
            edit_message(
                chat_id,
                status_id,
                f"❌ {platform}: не получилось скачать или подготовить медиа.\n\nТехническая причина:\n{error_text}",
            )
        except Exception:
            send_message(chat_id, "❌ Ошибка при обработке ссылки.")


def main():
    me = tg("getMe", timeout=30)
    tg("deleteWebhook", data={"drop_pending_updates": "false"}, timeout=30)
    print(f"Telegram video bot started as @{me.get('username', 'unknown')}", flush=True)
    print(f"yt-dlp version: {yt_dlp.version.__version__}", flush=True)

    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            response = requests.get(f"{API}/getUpdates", params=params, timeout=60)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(payload)
            for update in payload["result"]:
                offset = update["update_id"] + 1
                if update.get("message"):
                    handle_message(update["message"])
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("Polling error:", safe_error_text(exc), flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
