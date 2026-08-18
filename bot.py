import json
import mimetypes
import os
import re
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
POT_SCRIPT = "/opt/bgutil-ytdlp-pot-provider/server/build/generate_once.js"
CHROME_PATH = os.environ.get("CHROME_PATH", "/usr/bin/chromium")


def redact_secret(text: str) -> str:
    text = str(text)
    if TOKEN:
        text = text.replace(TOKEN, "***")
    text = re.sub(r"https://api\.telegram\.org/bot[^/\s]+", "https://api.telegram.org/bot***", text)
    return text


def tg(method: str, *, data=None, files=None, params=None, timeout=90):
    response = requests.post(
        f"{API}/{method}",
        data=data,
        files=files,
        params=params,
        timeout=timeout,
    )
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
            data={
                "chat_id": chat_id,
                "caption": caption[:1024],
                "supports_streaming": "true",
            },
            files={"video": (path.name, fh, "video/mp4")},
            timeout=240,
        )


def send_document(chat_id: int, path: Path, caption: str = ""):
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        return tg(
            "sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"document": (path.name, fh, mime)},
            timeout=240,
        )


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title or "Видео").strip()


def ytdlp_options(tmpdir: str, player_client: str) -> dict:
    return {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
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
            "youtubepot-bgutilscript": {"script_path": [POT_SCRIPT]},
            "youtubepot-wpc": {"browser_path": [CHROME_PATH]},
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


def safe_error_text(exc: Exception) -> str:
    text = redact_secret(exc)
    text = re.sub(r"\s+", " ", text).strip()
    return text[-700:] if text else type(exc).__name__


def handle_message(message: dict):
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
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
            size = path.stat().st_size

            if size > MAX_TELEGRAM_VIDEO_BYTES:
                tg(
                    "editMessageText",
                    data={
                        "chat_id": chat_id,
                        "message_id": status_id,
                        "text": (
                            f"Видео скачалось, но весит {size / 1024 / 1024:.1f} МБ. "
                            "Нужна версия меньшего размера для отправки через этого бота."
                        ),
                    },
                )
                return

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
                    "text": f"❌ Не получилось скачать видео.\n\nТехническая причина:\n{error_text}",
                },
            )
        except Exception:
            send_message(chat_id, "❌ Ошибка при обработке ссылки.")


def main():
    me = tg("getMe", timeout=30)
    tg("deleteWebhook", data={"drop_pending_updates": "false"}, timeout=30)
    print(f"Telegram video bot started as @{me.get('username', 'unknown')}", flush=True)
    print(f"yt-dlp version: {yt_dlp.version.__version__}", flush=True)
    print(f"PO token script exists: {Path(POT_SCRIPT).exists()}", flush=True)
    print(f"Chromium exists: {Path(CHROME_PATH).exists()} ({CHROME_PATH})", flush=True)

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
