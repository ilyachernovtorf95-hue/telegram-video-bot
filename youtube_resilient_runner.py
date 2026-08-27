"""Production runner with a non-failing YouTube path.

For YouTube we still try to download the file through the existing reliability
layer. If YouTube blocks Railway's datacenter IP, we do not fail the whole job:
Gemini analyzes the public YouTube URL directly and the bot still returns the
summary, chapters, visual context, transcript and Obsidian note.

The actual YouTube MP4 remains a separate concern because YouTube can block
server-side downloads by IP reputation. A free iPhone-local download route is
shown only when that happens.
"""

from __future__ import annotations

import re
import tempfile

import requests

import bot
import youtube_compat
from gemini_ai import format_analysis, is_configured
from youtube_direct import analyze_youtube_url


_ORIGINAL_HANDLE = bot.handle

# Maintained, free, local-on-device yt-dlp route for iOS. It avoids Railway's
# datacenter IP because the download happens on the user's iPhone connection.
ASHELL_MINI_URL = "https://apps.apple.com/app/a-shell-mini/id1543537943"
SW_DLT_URL = "https://www.icloud.com/shortcuts/695f53b649c947d998ae058d03efcc43"


def _youtube_title(url: str) -> str:
    try:
        response = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=(10, 20),
        )
        response.raise_for_status()
        title = re.sub(r"\s+", " ", str(response.json().get("title") or "")).strip()
        if title:
            return title[:180]
    except Exception:
        pass
    return "YouTube видео"


def _send_direct_analysis(chat_id, status_id, tmpdir, title, url, download_error):
    if not is_configured():
        raise RuntimeError(
            "YouTube не дал скачать файл с IP Railway, а GEMINI_API_KEY не настроен "
            "для прямого анализа YouTube URL."
        )

    bot.edit(
        chat_id,
        status_id,
        "🧠 YouTube: серверная загрузка файла заблокирована, но анализ продолжается "
        "напрямую через Gemini — без скачивания видео Railway.",
    )

    result = analyze_youtube_url(url, title=title, platform="YouTube")
    summary = format_analysis(result)
    bot.send_long(chat_id, f"📝 {title}\n\n{summary}")
    note = bot.note_file(tmpdir, title, url, "YouTube", result)
    bot.send_doc(
        chat_id,
        note,
        f"📚 Заметка: выжимка + полная транскрипция • {result.get('engine', 'Gemini')}",
    )

    bot.edit(
        chat_id,
        status_id,
        "✅ YouTube: анализ и транскрипция завершены напрямую через Gemini. "
        "Сам MP4 YouTube не отдаёт серверному IP Railway.",
    )

    # Do not dump a giant yt-dlp error into Telegram anymore. Give one concise,
    # practical file-download fallback that uses the user's own iPhone IP.
    bot.send(
        chat_id,
        "📱 Нужен сам файл YouTube без оплаты? Используй локальный путь на iPhone: "
        "a-Shell mini + SW-DLT. Скачивание идёт с твоего IP, поэтому блокировка Railway "
        "не участвует. После установки: YouTube → Поделиться → SW-DLT.\n\n"
        f"a-Shell mini: {ASHELL_MINI_URL}\n"
        f"SW-DLT: {SW_DLT_URL}\n\n"
        "Это требуется только для самого MP4; анализ, транскрипция и .md уже выполнены ботом автоматически.",
    )
    print(f"YOUTUBE_DOWNLOAD_BLOCKED_DIRECT_ANALYSIS_OK: {download_error}", flush=True)
    return result


def _handle_youtube(message, url: str):
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return

    status = bot.send(chat_id, "⏳ YouTube: пытаюсь получить видео и параллельно готовлю резервный анализ…")
    status_id = status["message_id"]

    with tempfile.TemporaryDirectory(prefix="tg-youtube-") as tmpdir:
        title = _youtube_title(url)
        try:
            downloaded, downloaded_title = bot.download(url, tmpdir)
            title = downloaded_title or title
        except Exception as exc:
            error = bot.clean_error(exc)
            print("YOUTUBE_DOWNLOAD_FAIL_FALLBACK_TO_GEMINI_URL:", error, flush=True)
            try:
                _send_direct_analysis(chat_id, status_id, tmpdir, title, url, error)
            except Exception as analysis_exc:
                analysis_error = bot.clean_error(analysis_exc)
                bot.edit(
                    chat_id,
                    status_id,
                    "❌ YouTube: не удалось ни скачать файл, ни завершить прямой Gemini-анализ.\n\n"
                    f"Причина анализа: {analysis_error}",
                )
            return

        try:
            source_video = bot.normalize_mp4(downloaded, tmpdir)
            parts = bot.split_video(source_video, tmpdir)
            for index, part in enumerate(parts, 1):
                bot.action(chat_id, "upload_video")
                if len(parts) == 1:
                    bot.edit(chat_id, status_id, "📤 YouTube: отправляю видео…")
                    caption = title
                else:
                    bot.edit(chat_id, status_id, f"📤 YouTube: отправляю видео {index}/{len(parts)}…")
                    caption = f"{title}\nЧасть {index}/{len(parts)}"
                bot.send_video(chat_id, part, caption)

            result = bot.analyze(chat_id, status_id, source_video, tmpdir, title, url, "YouTube")
            bot.edit(
                chat_id,
                status_id,
                "✅ YouTube: готово. Видео отправлено, анализ и транскрипция завершены "
                f"({result.get('engine', 'AI')}).",
            )
        except Exception as exc:
            error = bot.clean_error(exc)
            print("YOUTUBE_POST_DOWNLOAD_PROCESSING_ERROR:", error, flush=True)
            bot.edit(chat_id, status_id, f"❌ YouTube: ошибка после загрузки видео.\n\n{error}")


def resilient_handle(message):
    text = (message.get("text") or "").strip()

    # Keep all commands and all non-YouTube behavior exactly as before.
    if text.startswith(("/start", "/help")):
        return _ORIGINAL_HANDLE(message)

    match = bot.URL_RE.search(text)
    if not match:
        return _ORIGINAL_HANDLE(message)

    url = match.group(0).rstrip(".,;!?)\"]}")
    if bot.platform(url) != "YouTube":
        return _ORIGINAL_HANDLE(message)

    return _handle_youtube(message, url)


def main():
    # Reuse all current yt-dlp/PO-token/cookie/proxy attempts first. Only after
    # they fail do we switch the knowledge path to Gemini's native YouTube URL.
    youtube_compat.install()
    bot.handle = resilient_handle

    import bot_runner

    bot_runner.main()


if __name__ == "__main__":
    main()
