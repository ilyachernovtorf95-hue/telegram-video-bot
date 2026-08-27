"""Production runner with a resilient YouTube path.

YouTube is treated as two independent jobs:
1) knowledge extraction through Gemini's native public-YouTube-URL input;
2) best-effort MP4 download through yt-dlp.

They run in parallel. This keeps summaries/transcripts reliable even when
YouTube blocks Railway's datacenter IP, while still returning the MP4 whenever
server-side download succeeds.
"""

from __future__ import annotations

import concurrent.futures
import re
import tempfile

import requests

import bot
import youtube_compat
from gemini_ai import format_analysis, is_configured
from youtube_direct import analyze_youtube_url


_ORIGINAL_HANDLE = bot.handle

# Free local-on-device yt-dlp route for iOS. The download happens from the
# user's iPhone IP rather than Railway's datacenter IP.
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


def _emit_analysis(chat_id, tmpdir, title, url, result):
    summary = format_analysis(result)
    bot.send_long(chat_id, f"📝 {title}\n\n{summary}")
    note = bot.note_file(tmpdir, title, url, "YouTube", result)
    bot.send_doc(
        chat_id,
        note,
        f"📚 Заметка: выжимка + полная транскрипция • {result.get('engine', 'Gemini')}",
    )


def _send_local_file_route(chat_id):
    bot.send(
        chat_id,
        "📱 Сам MP4 YouTube серверу Railway не отдал. Бесплатный устойчивый путь для файла — "
        "скачать его локально на iPhone, где используется твой обычный IP:\n\n"
        f"1) a-Shell mini: {ASHELL_MINI_URL}\n"
        f"2) SW-DLT: {SW_DLT_URL}\n"
        "3) Затем: YouTube → Поделиться → SW-DLT.\n\n"
        "Это нужно только для физического MP4. Анализ, транскрипция, таймкоды и .md бот делает сам.",
    )


def _handle_youtube(message, url: str):
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return

    status = bot.send(
        chat_id,
        "⏳ YouTube: анализирую ролик напрямую через Gemini и одновременно пытаюсь получить MP4…",
    )
    status_id = status["message_id"]

    with tempfile.TemporaryDirectory(prefix="tg-youtube-") as tmpdir:
        title = _youtube_title(url)

        analysis_future = None
        executor = None
        if is_configured():
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gemini-youtube")
            analysis_future = executor.submit(analyze_youtube_url, url, title, "YouTube")

        downloaded = None
        download_error = ""
        try:
            downloaded, downloaded_title = bot.download(url, tmpdir)
            title = downloaded_title or title
        except Exception as exc:
            download_error = bot.clean_error(exc)
            print("YOUTUBE_MP4_DOWNLOAD_FAIL:", download_error, flush=True)

        result = None
        analysis_error = ""
        if analysis_future is not None:
            try:
                bot.edit(chat_id, status_id, "🧠 YouTube: завершаю смысловой анализ и транскрипцию…")
                result = analysis_future.result(timeout=240)
            except Exception as exc:
                analysis_error = bot.clean_error(exc)
                print("YOUTUBE_DIRECT_ANALYSIS_FAIL:", analysis_error, flush=True)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

        # If direct URL analysis failed but the MP4 did download, reuse the older
        # file-upload analysis as a fallback instead of losing the job.
        source_video = None
        if downloaded is not None:
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
            except Exception as exc:
                download_error = (download_error + " | " + bot.clean_error(exc)).strip(" |")
                source_video = None
                print("YOUTUBE_MP4_POSTPROCESS_FAIL:", download_error, flush=True)

        if result is None and source_video is not None:
            try:
                result = bot.analyze(chat_id, status_id, source_video, tmpdir, title, url, "YouTube")
            except Exception as exc:
                analysis_error = (analysis_error + " | " + bot.clean_error(exc)).strip(" |")
                print("YOUTUBE_FILE_ANALYSIS_FAIL:", analysis_error, flush=True)
        elif result is not None:
            _emit_analysis(chat_id, tmpdir, title, url, result)

        if result is None:
            bot.edit(
                chat_id,
                status_id,
                "❌ YouTube: не удалось завершить анализ.\n\n"
                f"Анализ: {analysis_error or 'неизвестная ошибка'}",
            )
            return

        if source_video is not None:
            bot.edit(
                chat_id,
                status_id,
                "✅ YouTube: готово. MP4 отправлен, анализ и транскрипция завершены "
                f"({result.get('engine', 'AI')}).",
            )
            return

        # The core knowledge task is successful even when YouTube rejects the
        # Railway IP. Present that as a successful partial result, not a giant
        # technical error, and offer the only robust free MP4 route for iPhone.
        bot.edit(
            chat_id,
            status_id,
            "✅ YouTube: анализ, транскрипция, таймкоды и .md готовы. "
            "YouTube заблокировал только серверное получение MP4 с IP Railway.",
        )
        _send_local_file_route(chat_id)
        print(f"YOUTUBE_DIRECT_ANALYSIS_OK_MP4_BLOCKED: {download_error}", flush=True)


def resilient_handle(message):
    text = (message.get("text") or "").strip()

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
    youtube_compat.install()
    bot.handle = resilient_handle

    import bot_runner

    bot_runner.main()


if __name__ == "__main__":
    main()
