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

from gemini_ai import analyze_video, format_analysis, is_configured
from local_ai import fallback_analyze

TOKEN = re.sub(r"\s+", "", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
if not TOKEN or ":" not in TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set or invalid")

API = f"https://api.telegram.org/bot{TOKEN}"
URL_RE = re.compile(r"https?://[^\s]+", re.I)
MAX_BYTES = int(os.environ.get("MAX_TELEGRAM_VIDEO_MB", "49")) * 1024 * 1024
SAFE_BYTES = min(MAX_BYTES - 2 * 1024 * 1024, 47 * 1024 * 1024)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
MAX_ANALYSIS_VIDEO_BYTES = int(os.environ.get("MAX_ANALYSIS_VIDEO_MB", "1900")) * 1024 * 1024


def clean_error(exc):
    text = str(exc).replace(TOKEN, "***")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        text = text.replace(key, "***")
    return re.sub(r"\s+", " ", text).strip()[-1000:]


def tg(method, *, data=None, files=None, params=None, timeout=90):
    response = requests.post(f"{API}/{method}", data=data, files=files, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return payload["result"]


def send(chat_id, text):
    return tg("sendMessage", data={"chat_id": chat_id, "text": text})


def send_long(chat_id, text, size=3800):
    text = (text or "").strip()
    while text:
        if len(text) <= size:
            send(chat_id, text)
            return
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = text.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        send(chat_id, text[:cut].rstrip())
        text = text[cut:].lstrip()


def edit(chat_id, message_id, text):
    try:
        return tg("editMessageText", data={"chat_id": chat_id, "message_id": message_id, "text": text})
    except Exception:
        return None


def action(chat_id, name):
    try:
        tg("sendChatAction", data={"chat_id": chat_id, "action": name}, timeout=20)
    except Exception:
        pass


def platform(url):
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if host == "youtu.be" or "youtube.com" in host:
        return "YouTube"
    if "tiktok.com" in host:
        return "TikTok"
    if "instagram.com" in host:
        return "Instagram"
    if "threads.net" in host or "threads.com" in host:
        return "Threads"
    if host == "vk.com" or host.endswith(".vk.com") or "vkvideo.ru" in host:
        return "VK"
    return "сайт"


def ytdlp_opts(tmpdir, client=None):
    result = {
        "format": (
            "bestvideo*[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo*[height<=1080]+bestaudio/"
            "best[height<=1080]/best"
        ),
        "outtmpl": str(Path(tmpdir) / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
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
        "quiet": True,
        "no_warnings": True,
    }
    if client:
        result["extractor_args"] = {
            "youtube": {"player_client": [client]},
            "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]},
        }
    return result


def newest_media(tmpdir):
    files = sorted(
        [
            path for path in Path(tmpdir).iterdir()
            if path.is_file() and not path.name.endswith((".part", ".ytdl", ".json"))
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise RuntimeError("Downloaded file was not found")
    videos = [p for p in files if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
    return videos[0] if videos else files[0]


def direct_meta(url, tmpdir):
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, allow_redirects=True)
    response.raise_for_status()
    body = response.text

    def meta(prop):
        patterns = [
            rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, body, re.I)
            if match:
                return html.unescape(match.group(1))
        return ""

    media_url = meta("og:video") or meta("og:video:secure_url") or meta("og:image")
    if not media_url:
        raise RuntimeError("Public page does not expose downloadable media metadata")
    title = re.sub(r"\s+", " ", meta("og:title") or "Media").strip()
    media = requests.get(
        media_url,
        headers={"User-Agent": USER_AGENT, "Referer": url},
        timeout=90,
        stream=True,
    )
    media.raise_for_status()
    content_type = (media.headers.get("content-type") or "").split(";", 1)[0].lower()
    suffix = mimetypes.guess_extension(content_type) or (".mp4" if "video" in content_type else ".jpg")
    path = Path(tmpdir) / f"direct-media{suffix}"
    with path.open("wb") as fh:
        for chunk in media.iter_content(1024 * 1024):
            if chunk:
                fh.write(chunk)
    return path, title


def download(url, tmpdir):
    source = platform(url)
    errors = []
    clients = ["mweb", "web_safari", "android_vr"] if source == "YouTube" else [None]
    for client in clients:
        try:
            with yt_dlp.YoutubeDL(ytdlp_opts(tmpdir, client)) as ydl:
                info = ydl.extract_info(url, download=True)
                title = re.sub(r"\s+", " ", info.get("title") or "Видео").strip()
            return newest_media(tmpdir), title
        except Exception as exc:
            errors.append(f"{source}/{client or 'generic'}: {type(exc).__name__}: {clean_error(exc)}")
            for file in Path(tmpdir).iterdir():
                try:
                    if file.is_file():
                        file.unlink()
                except OSError:
                    pass
    if source in {"Instagram", "Threads"}:
        try:
            return direct_meta(url, tmpdir)
        except Exception as exc:
            errors.append(f"{source}/og-meta: {clean_error(exc)}")
    raise RuntimeError(" | ".join(errors))


def probe(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True, timeout=30,
    )
    payload = json.loads(result.stdout or "{}")
    stream = (payload.get("streams") or [{}])[0]
    duration = max(1, int(round(float((payload.get("format") or {}).get("duration") or 0))))
    return int(stream.get("width") or 0), int(stream.get("height") or 0), duration


def media_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return max(1.0, float(result.stdout.strip()))


def normalize_mp4(path, tmpdir):
    if path.suffix.lower() == ".mp4":
        return path
    output = Path(tmpdir) / "source-normalized.mp4"
    copy = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", str(output)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600,
    )
    if copy.returncode == 0 and output.exists() and output.stat().st_size > 1024:
        return output
    transcode = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output),
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800,
    )
    if transcode.returncode != 0 or not output.exists():
        raise RuntimeError("Не удалось подготовить MP4 для Telegram")
    return output


def split_video(path, tmpdir):
    path = Path(path)
    if path.stat().st_size <= MAX_BYTES:
        return [path]

    total_duration = media_duration(path)
    segment_time = max(8, int(total_duration * SAFE_BYTES / path.stat().st_size * 0.80))

    for _ in range(6):
        for old in Path(tmpdir).glob("tg-part-*.mp4"):
            try:
                old.unlink()
            except OSError:
                pass
        pattern = str(Path(tmpdir) / "tg-part-%03d.mp4")
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-fflags", "+genpts", "-i", str(path),
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-f", "segment", "-segment_time", str(segment_time),
                "-reset_timestamps", "1", pattern,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900,
        )
        parts = sorted(Path(tmpdir).glob("tg-part-*.mp4"))
        if result.returncode == 0 and parts and max(p.stat().st_size for p in parts) <= MAX_BYTES:
            return parts
        segment_time = max(5, int(segment_time * 0.72))

    raise RuntimeError("Не удалось безопасно разделить видео без потери качества на части до лимита Telegram.")


def send_video(chat_id, path, caption=""):
    width, height, duration = probe(path)
    data = {
        "chat_id": chat_id,
        "caption": caption[:1024],
        "supports_streaming": "true",
        "duration": str(duration),
    }
    if width and height:
        data.update(width=str(width), height=str(height))
    with Path(path).open("rb") as fh:
        return tg(
            "sendVideo",
            data=data,
            files={"video": (Path(path).name, fh, "video/mp4")},
            timeout=600,
        )


def send_doc(chat_id, path, caption=""):
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        return tg(
            "sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"document": (path.name, fh, mime)},
            timeout=600,
        )


def send_photo(chat_id, path, caption=""):
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    with path.open("rb") as fh:
        return tg(
            "sendPhoto",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"photo": (path.name, fh, mime)},
            timeout=300,
        )


def safe_name(text):
    cleaned = re.sub(r"[^\w\- .]+", "", text or "", flags=re.UNICODE).strip().replace(" ", "-")
    return (re.sub(r"-+", "-", cleaned).strip("-.")[:80] or "video-note")


def note_file(tmpdir, title, url, source, result):
    path = Path(tmpdir) / f"{safe_name(title)}.md"
    analysis = format_analysis(result)
    transcript = (result.get("transcript") or "").strip()
    engine = result.get("engine") or "unknown"
    created = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    path.write_text(
        f"# {title}\n\n"
        f"- Источник: {source}\n"
        f"- Ссылка: {url}\n"
        f"- Движок анализа: {engine}\n"
        f"- Создано: {created}\n\n"
        f"## Смысловая выжимка\n\n{analysis}\n\n"
        f"## Полная транскрипция\n\n{transcript or 'Транскрипция отсутствует.'}\n",
        encoding="utf-8",
    )
    return path


def analyze(chat_id, status_id, path, tmpdir, title, url, source):
    result = None
    gemini_error = ""

    if is_configured() and Path(path).stat().st_size <= MAX_ANALYSIS_VIDEO_BYTES:
        edit(chat_id, status_id, f"🧠 {source}: изучаю видео целиком — речь, кадры и текст…")
        try:
            result = analyze_video(path, title=title, source_url=url, platform=source)
        except Exception as exc:
            gemini_error = clean_error(exc)
            print("GEMINI_ANALYSIS_FAIL:", gemini_error, flush=True)
    elif is_configured():
        gemini_error = "Видео превышает безопасный размер для бесплатного облачного анализа."
    else:
        gemini_error = "GEMINI_API_KEY не настроен."

    if result is None:
        edit(chat_id, status_id, f"🧠 {source}: облачный анализ недоступен, запускаю локальный резерв…")
        result = fallback_analyze(path, title=title)
        result["fallback_reason"] = gemini_error

    summary = format_analysis(result)
    send_long(chat_id, f"📝 {title}\n\n{summary}")
    note = note_file(tmpdir, title, url, source, result)
    send_doc(chat_id, note, f"📚 Заметка: выжимка + полная транскрипция • {result.get('engine', 'AI')}")
    return result


def handle(message):
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id:
        return

    if text.startswith(("/start", "/help")):
        send(
            chat_id,
            "Пришли ссылку на YouTube, TikTok, Instagram, Threads или VK.\n\n"
            "Я:\n"
            "1) скачаю и отправлю тебе видео;\n"
            "2) изучу речь, кадры и текст в ролике;\n"
            "3) сделаю краткую выжимку, главные мысли, факты, действия и теги;\n"
            "4) сохраню полную транскрипцию в .md для Obsidian.\n\n"
            "Основной анализ работает через бесплатный Gemini API; при его недоступности включается локальный резерв.",
        )
        return

    match = URL_RE.search(text)
    if not match:
        send(chat_id, "Пришли ссылку, начинающуюся с http:// или https://")
        return

    url = match.group(0).rstrip(".,;!?)\"]}")
    source = platform(url)
    status = send(chat_id, f"⏳ {source}: скачиваю видео…")
    status_id = status["message_id"]

    try:
        with tempfile.TemporaryDirectory(prefix="tg-video-") as tmpdir:
            downloaded, title = download(url, tmpdir)
            suffix = downloaded.suffix.lower()

            if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
                edit(chat_id, status_id, f"📤 {source}: отправляю изображение…")
                send_photo(chat_id, downloaded, title)
                edit(chat_id, status_id, "✅ Готово.")
                return

            source_video = normalize_mp4(downloaded, tmpdir)
            parts = split_video(source_video, tmpdir)
            for index, part in enumerate(parts, 1):
                action(chat_id, "upload_video")
                if len(parts) == 1:
                    edit(chat_id, status_id, f"📤 {source}: отправляю видео…")
                    caption = title
                else:
                    edit(chat_id, status_id, f"📤 {source}: отправляю видео {index}/{len(parts)}…")
                    caption = f"{title}\nЧасть {index}/{len(parts)}"
                send_video(chat_id, part, caption)

            result = analyze(chat_id, status_id, source_video, tmpdir, title, url, source)
            edit(
                chat_id,
                status_id,
                f"✅ {source}: готово. Видео отправлено, анализ и транскрипция завершены "
                f"({result.get('engine', 'AI')}).",
            )

    except Exception as exc:
        error = clean_error(exc)
        print("PROCESSING_ERROR:", error, flush=True)
        edit(chat_id, status_id, f"❌ {source}: обработка не завершилась.\n\nТехническая причина:\n{error}")


def acknowledge_update(update_id):
    params = {
        "offset": update_id + 1,
        "timeout": 0,
        "limit": 1,
        "allowed_updates": json.dumps(["message"]),
    }
    last_error = None
    for attempt in range(4):
        try:
            response = requests.get(f"{API}/getUpdates", params=params, timeout=15)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(payload)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.7 * (attempt + 1))
    raise RuntimeError("Не удалось подтвердить Telegram update: " + clean_error(last_error))


def main():
    me = tg("getMe", timeout=30)
    tg("deleteWebhook", data={"drop_pending_updates": "false"}, timeout=30)
    print(f"Telegram video bot started as @{me.get('username', 'unknown')}", flush=True)
    print(
        f"yt-dlp={yt_dlp.version.__version__}; Gemini={'configured' if is_configured() else 'not-configured'}; "
        "local Whisper fallback enabled",
        flush=True,
    )

    offset = None
    conflict_backoff = 3
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            response = requests.get(f"{API}/getUpdates", params=params, timeout=60)
            if response.status_code == 409:
                print("Telegram polling conflict (another instance is finishing); retrying.", flush=True)
                time.sleep(conflict_backoff)
                conflict_backoff = min(30, conflict_backoff + 3)
                continue
            response.raise_for_status()
            conflict_backoff = 3
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(payload)

            for update in payload["result"]:
                update_id = update["update_id"]
                acknowledge_update(update_id)
                offset = update_id + 1
                if update.get("message"):
                    handle(update["message"])

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("Polling error:", clean_error(exc), flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
