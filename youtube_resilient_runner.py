"""Resilient YouTube workflow.

For every public YouTube URL the knowledge path and the MP4 path are independent:
Gemini analyzes the public URL directly while yt-dlp downloads the file. Analysis
is delivered before a potentially long multipart Telegram upload. If direct URL
analysis fails, the downloaded file is analyzed as a fallback before sending parts.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
import tempfile

import requests

import bot
import youtube_compat
from gemini_ai import format_analysis, is_configured
from youtube_direct import analyze_youtube_url


_ORIGINAL_HANDLE = bot.handle
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


def _clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _emit_analysis(chat_id, tmpdir, title, url, result):
    bot.send_long(chat_id, f"📝 {title}\n\n{format_analysis(result)}")
    note = bot.note_file(tmpdir, title, url, "YouTube", result)
    bot.send_doc(
        chat_id,
        note,
        f"📚 Заметка: выжимка + полная транскрипция • {result.get('engine', 'Gemini')}",
    )


def _send_local_file_route(chat_id):
    bot.send(
        chat_id,
        "📱 AI-разбор уже выполнен, но YouTube не отдал MP4 серверному IP Railway. "
        "Бесплатный резерв для самого файла на iPhone:\n\n"
        f"a-Shell mini: {ASHELL_MINI_URL}\n"
        f"SW-DLT: {SW_DLT_URL}\n\n"
        "После установки: YouTube → Поделиться → SW-DLT. Скачивание идёт с IP iPhone.",
    )


def _part_durations(parts):
    values = []
    for part in parts:
        try:
            values.append(bot.media_duration(part))
        except Exception:
            values.append(0.0)
    return values


def _send_parts(chat_id, status_id, source_video, parts, title):
    source_duration = bot.media_duration(source_video)
    durations = _part_durations(parts)
    measured_sum = sum(durations)
    delta = abs(measured_sum - source_duration)
    integrity_ok = bool(durations) and all(x > 0 for x in durations) and delta <= max(5.0, len(parts) * 0.75)

    bot.edit(
        chat_id,
        status_id,
        f"📦 YouTube: полное видео {_clock(source_duration)} подготовлено. "
        f"Telegram требует {len(parts)} частей. "
        + ("✅ Целостность проверена." if integrity_ok else "⚠️ Проверяю границы частей по длительности."),
    )

    cursor = 0.0
    for index, part in enumerate(parts, 1):
        duration = durations[index - 1] if index - 1 < len(durations) else 0.0
        start = cursor
        end = min(source_duration, cursor + duration) if duration else cursor
        cursor += duration
        bot.action(chat_id, "upload_video")
        bot.edit(
            chat_id,
            status_id,
            f"📤 YouTube: часть {index}/{len(parts)} • {_clock(start)}–{_clock(end)}",
        )
        bot.send_video(
            chat_id,
            part,
            f"{title}\nЧасть {index}/{len(parts)} • {_clock(start)}–{_clock(end)}",
        )

    return source_duration, measured_sum, integrity_ok


def _handle_youtube(message, url: str):
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return

    title = _youtube_title(url)
    status = bot.send(
        chat_id,
        "⏳ YouTube: одновременно запускаю полный AI-разбор и скачивание видео…",
    )
    status_id = status["message_id"]

    with tempfile.TemporaryDirectory(prefix="tg-youtube-") as tmpdir:
        analysis_result = None
        analysis_error = ""
        download_error = ""
        downloaded = None
        downloaded_title = ""

        # Network-heavy tasks run in parallel. We intentionally wait for the
        # semantic result before uploading many Telegram parts, so a 2+ hour
        # podcast can never finish with only 10-20 MP4 chunks and no analysis.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="youtube-job") as pool:
            analysis_future = pool.submit(analyze_youtube_url, url, title, "YouTube") if is_configured() else None
            download_future = pool.submit(bot.download, url, tmpdir)

            if analysis_future is not None:
                bot.edit(
                    chat_id,
                    status_id,
                    "🧠 YouTube: Gemini изучает весь ролик напрямую по ссылке; скачивание идёт параллельно…",
                )
                try:
                    # No short 240-second future timeout here. The Gemini HTTP layer
                    # has its own bounded retries/timeouts suitable for multi-hour video.
                    analysis_result = analysis_future.result()
                    _emit_analysis(chat_id, tmpdir, title, url, analysis_result)
                except Exception as exc:
                    analysis_error = bot.clean_error(exc)
                    print("YOUTUBE_DIRECT_ANALYSIS_FAIL:", analysis_error, flush=True)
            else:
                analysis_error = "GEMINI_API_KEY не настроен"

            try:
                downloaded, downloaded_title = download_future.result()
                title = downloaded_title or title
            except Exception as exc:
                download_error = bot.clean_error(exc)
                print("YOUTUBE_MP4_DOWNLOAD_FAIL:", download_error, flush=True)

        if downloaded is None:
            if analysis_result is not None:
                bot.edit(
                    chat_id,
                    status_id,
                    "✅ YouTube: выжимка, главные мысли, факты, таймкоды, полная транскрипция и .md готовы. "
                    "YouTube заблокировал только серверное получение MP4.",
                )
                _send_local_file_route(chat_id)
                return
            bot.edit(
                chat_id,
                status_id,
                "❌ YouTube: не удалось ни скачать MP4, ни завершить AI-разбор.\n\n"
                f"Анализ: {analysis_error[-700:]}\nСкачивание: {download_error[-700:]}",
            )
            return

        try:
            source_video = bot.normalize_mp4(downloaded, tmpdir)
        except Exception as exc:
            bot.edit(chat_id, status_id, f"❌ YouTube: MP4 скачан, но не подготовлен: {bot.clean_error(exc)}")
            return

        # Mandatory fallback: if direct URL analysis failed, analyze the downloaded
        # file BEFORE multipart upload. gemini_ai sends long video at low media
        # resolution, giving roughly a 3-hour context window on 1M-context models.
        if analysis_result is None:
            bot.edit(
                chat_id,
                status_id,
                "🧠 YouTube: прямой анализ не завершился. Видео уже скачано — "
                "запускаю резервный полный анализ файла до отправки частей…",
            )
            try:
                analysis_result = bot.analyze(chat_id, status_id, source_video, tmpdir, title, url, "YouTube")
            except Exception as exc:
                analysis_error = (analysis_error + " | " + bot.clean_error(exc)).strip(" |")
                print("YOUTUBE_FILE_ANALYSIS_FAIL:", analysis_error, flush=True)

        try:
            parts = bot.split_video(source_video, tmpdir)
            source_duration, measured_sum, integrity_ok = _send_parts(
                chat_id, status_id, source_video, parts, title
            )
        except Exception as exc:
            bot.edit(chat_id, status_id, f"❌ YouTube: ошибка подготовки/отправки частей: {bot.clean_error(exc)}")
            return

        if analysis_result is not None:
            bot.edit(
                chat_id,
                status_id,
                f"✅ YouTube: задача завершена. Видео {_clock(source_duration)} отправлено в {len(parts)} частях; "
                f"{'целостность подтверждена' if integrity_ok else 'длительности частей сохранены для проверки'}. "
                f"Выжимка, главные мысли, факты, таймкоды, полная транскрипция и .md готовы "
                f"({analysis_result.get('engine', 'AI')}).",
            )
        else:
            bot.edit(
                chat_id,
                status_id,
                f"⚠️ YouTube: видео {_clock(source_duration)} отправлено в {len(parts)} частях, "
                "но AI-разбор не завершился. Последняя причина: " + analysis_error[-650:],
            )


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
