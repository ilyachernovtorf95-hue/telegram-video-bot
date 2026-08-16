import os
import re
import time
import json
import mimetypes
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

API = f"https://api.telegram.org/bot{TOKEN}"
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
MAX_TELEGRAM_VIDEO_BYTES = 49 * 1024 * 1024  # keep a little under Telegram's 50 MB sendVideo limit


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
            timeout=180,
        )


def send_document(chat_id: int, path: Path, caption: str = ""):
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        return tg(
            "sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"document": (path.name, fh, mime)},
            timeout=180,
        )


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title or "Видео").strip()


def download_with_ytdlp(url: str, tmpdir: str) -> tuple[Path, str]:
    outtmpl = str(Path(tmpdir) / "%(title).80s-%(id)s.%(ext)s")
    options = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "merge_output_format": "mp4",
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        title = clean_title(info.get("title"))
        prepared = Path(ydl.prepare_filename(info))

    candidates = sorted(
        [p for p in Path(tmpdir).iterdir() if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("Downloaded file was not found")

    # Prefer the final merged MP4 when present.
    mp4s = [p for p in candidates if p.suffix.lower() == ".mp4"]
    return (mp4s[0] if mp4s else candidates[0]), title


def handle_message(message: dict):
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    if not chat_id:
        return

    if text.startswith("/start") or text.startswith("/help"):
        send_message(
            chat_id,
            "Пришли ссылку на видео. Я попробую скачать ролик и отправить его обратно в Telegram.\n\n"
            "Работает не со всеми сайтами: некоторые требуют вход, cookies или блокируют скачивание.",
        )
        return

    match = URL_RE.search(text)
    if not match:
        send_message(chat_id, "Пришли обычную ссылку, начинающуюся с http:// или https://")
        return

    url = match.group(0)
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
                            "Обычный Telegram Bot API не отправляет video-файлы больше 50 МБ."
                        ),
                    },
                )
                return

            if path.suffix.lower() == ".mp4":
                send_video(chat_id, path, title)
            else:
                send_document(chat_id, path, title)

        tg("deleteMessage", data={"chat_id": chat_id, "message_id": status_id})
    except Exception as exc:
        print("ERROR:", repr(exc), flush=True)
        try:
            tg(
                "editMessageText",
                data={
                    "chat_id": chat_id,
                    "message_id": status_id,
                    "text": (
                        "❌ Не получилось скачать или отправить это видео. "
                        "Возможно, сайт требует авторизацию, блокирует скачивание "
                        "или ссылка не поддерживается."
                    ),
                },
            )
        except Exception:
            send_message(chat_id, "❌ Ошибка при обработке ссылки.")


def main():
    print("Telegram video bot started", flush=True)
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
            print("Polling error:", repr(exc), flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
