import json
import mimetypes
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

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
TARGET_VIDEO_BYTES = min(MAX_TELEGRAM_VIDEO_BYTES - 2 * 1024 * 1024, 47 * 1024 * 1024)


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


def send_chat_action(chat_id: int, action: str):
    try:
        tg("sendChatAction", data={"chat_id": chat_id, "action": action}, timeout=20)
    except Exception:
        pass


def send_video(chat_id: int, path: Path, caption: str = ""):
    with path.open("rb") as fh:
        return tg(
            "sendVideo",
            data={"chat_id": chat_id, "caption": caption[:1024], "supports_streaming": "true"},
            files={"video": (path.name, fh, "video/mp4")},
            timeout=300,
        )


def send_document(chat_id: int, path: Path, caption: str = ""):
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        return tg(
            "sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"document": (path.name, fh, mime)},
            timeout=300,
        )


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title or "Видео").strip()


def ytdlp_options(tmpdir: str, player_client: str) -> dict:
    return {
        "format": "bv*[height<=720]+ba/b[height<=720]/best",
        "outtmpl": str(Path(tmpdir) / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "verbose": True,
        "restrictfilenames": True,
        "merge_output_format": "mp4",
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
        "js_runtimes": {"node": {"path": "/usr/local/bin/node"}},
        "extractor_args": {
            "youtube": {"player_client": [player_client]},
            "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]},
        },
    }


def download_with_ytdlp(url: str, tmpdir: str) -> tuple[Path, str]:
    errors = []
    for player_client in ("mweb", "web_safari", "android_vr"):
        try:
            print(f"DOWNLOAD_ATTEMPT player_client={player_client}", flush=True)
            with yt_dlp.YoutubeDL(ytdlp_options(tmpdir, player_client)) as ydl:
                info = ydl.extract_info(url, download=True)
                title = clean_title(info.get("title"))

            candidates = sorted(
                [p for p in Path(tmpdir).iterdir() if p.is_file() and not p.name.endswith(".part")],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                raise RuntimeError("Downloaded file was not found")
            mp4s = [p for p in candidates if p.suffix.lower() == ".mp4"]
            return (mp4s[0] if mp4s else candidates[0]), title
        except Exception as exc:
            message = f"{player_client}: {type(exc).__name__}: {redact_secret(exc)}"
            errors.append(message)
            print("DOWNLOAD_ATTEMPT_FAILED:", message, flush=True)
            for p in Path(tmpdir).iterdir():
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass

    raise RuntimeError(" | ".join(errors))


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path)
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise RuntimeError("Could not determine video duration")
    return duration


def compress_for_telegram(path: Path, tmpdir: str) -> Path:
    if path.stat().st_size <= MAX_TELEGRAM_VIDEO_BYTES:
        return path

    duration = probe_duration(path)
    target_bytes = TARGET_VIDEO_BYTES
    audio_bps = 96_000 if duration < 3600 else 64_000
    total_bps = max(300_000, int((target_bytes * 8 / duration) * 0.92))
    video_bps = max(180_000, total_bps - audio_bps)

    if video_bps >= 1_000_000:
        max_height = 720
    elif video_bps >= 550_000:
        max_height = 480
    else:
        max_height = 360

    output = Path(tmpdir) / "telegram-compressed.mp4"

    for attempt in range(3):
        if output.exists():
            output.unlink()

        maxrate = int(video_bps * 1.15)
        bufsize = int(video_bps * 2)
        print(
            f"COMPRESS_ATTEMPT={attempt + 1} duration={duration:.1f}s "
            f"video_bps={video_bps} audio_bps={audio_bps} height={max_height}",
            flush=True,
        )

        cmd = [
            "ffmpeg", "-y", "-i", str(path),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", f"scale=-2:'min({max_height},ih)'",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", str(video_bps),
            "-maxrate", str(maxrate),
            "-bufsize", str(bufsize),
            "-c:a", "aac", "-b:a", str(audio_bps),
            "-movflags", "+faststart",
            str(output),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)

        size = output.stat().st_size
        print(f"COMPRESS_RESULT size={size / 1024 / 1024:.1f}MB", flush=True)
        if size <= MAX_TELEGRAM_VIDEO_BYTES:
            return output

        ratio = target_bytes / size
        video_bps = max(150_000, int(video_bps * ratio * 0.90))
        if video_bps < 500_000:
            max_height = min(max_height, 360)

    raise RuntimeError(
        f"Could not compress video below {MAX_TELEGRAM_VIDEO_BYTES / 1024 / 1024:.0f} MB"
    )


def safe_error_text(exc: Exception) -> str:
    text = re.sub(r"\s+", " ", redact_secret(exc)).strip()
    return text[-700:] if text else type(exc).__name__


def handle_message(message: dict):
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id:
        return

    if text.startswith("/start") or text.startswith("/help"):
        send_message(chat_id, "Пришли ссылку на видео. Я попробую скачать ролик и отправить его обратно в Telegram.")
        return

    match = URL_RE.search(text)
    if not match:
        send_message(chat_id, "Пришли ссылку, начинающуюся с http:// или https://")
        return

    url = match.group(0).rstrip(".,;!?)\"]}")
    status = send_message(chat_id, "⏳ Скачиваю видео…")
    status_id = status["message_id"]

    try:
        send_chat_action(chat_id, "upload_video")
        with tempfile.TemporaryDirectory(prefix="tg-video-") as tmpdir:
            path, title = download_with_ytdlp(url, tmpdir)
            original_size = path.stat().st_size

            if original_size > MAX_TELEGRAM_VIDEO_BYTES:
                tg(
                    "editMessageText",
                    data={
                        "chat_id": chat_id,
                        "message_id": status_id,
                        "text": (
                            f"⏳ Видео скачалось ({original_size / 1024 / 1024:.1f} МБ). "
                            "Сжимаю до размера для Telegram…"
                        ),
                    },
                )
                path = compress_for_telegram(path, tmpdir)

            send_chat_action(chat_id, "upload_video")
            if path.suffix.lower() == ".mp4":
                send_video(chat_id, path, title)
            else:
                send_document(chat_id, path, title)

        try:
            tg("deleteMessage", data={"chat_id": chat_id, "message_id": status_id})
        except Exception:
            pass
    except Exception as exc:
        print("DOWNLOAD_ERROR:", safe_error_text(exc), flush=True)
        error_text = safe_error_text(exc)
        try:
            tg(
                "editMessageText",
                data={
                    "chat_id": chat_id,
                    "message_id": status_id,
                    "text": f"❌ Не получилось скачать или подготовить видео.\n\nТехническая причина:\n{error_text}",
                },
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
