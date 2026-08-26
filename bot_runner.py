import json
import queue
import threading
import time

import requests

import bot


MAX_QUEUED_JOBS = 6
JOBS = queue.Queue(maxsize=MAX_QUEUED_JOBS)
ACTIVE = set()
ACTIVE_LOCK = threading.Lock()


def job_key(message):
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    match = bot.URL_RE.search(text)
    if not chat_id or not match:
        return None
    url = match.group(0).rstrip(".,;!?)\"]}")
    return chat_id, url


def enqueue(message):
    key = job_key(message)
    if key is None:
        bot.handle(message)
        return

    chat_id, url = key
    source = bot.platform(url)

    with ACTIVE_LOCK:
        if key in ACTIVE:
            bot.send(chat_id, f"⏳ {source}: эта ссылка уже обрабатывается. Дождись текущего результата.")
            return
        ACTIVE.add(key)

    try:
        JOBS.put_nowait((key, message))
    except queue.Full:
        with ACTIVE_LOCK:
            ACTIVE.discard(key)
        bot.send(chat_id, "⚠️ Очередь обработки заполнена. Попробуй отправить ссылку немного позже.")
        return

    ahead = max(0, JOBS.qsize() - 1)
    if ahead:
        bot.send(
            chat_id,
            f"📥 {source}: ссылка принята. Перед ней в очереди: {ahead}. "
            "Бот продолжает отвечать на команды, пока видео обрабатывается.",
        )
    else:
        bot.send(
            chat_id,
            f"📥 {source}: ссылка принята. Начинаю обработку в фоне; "
            "бот продолжает отвечать на команды.",
        )


def worker():
    while True:
        key, message = JOBS.get()
        try:
            bot.handle(message)
        except Exception as exc:
            chat_id = (message.get("chat") or {}).get("id")
            error = bot.clean_error(exc)
            print("WORKER_FATAL_ERROR:", error, flush=True)
            if chat_id:
                try:
                    bot.send(chat_id, f"❌ Внутренняя ошибка фоновой обработки:\n{error}")
                except Exception:
                    pass
        finally:
            with ACTIVE_LOCK:
                ACTIVE.discard(key)
            JOBS.task_done()


def dispatch(message):
    text = (message.get("text") or "").strip()

    # Commands and ordinary text are handled immediately in the polling thread.
    # Heavy URL processing is queued so a long YouTube/TikTok job can never make
    # /start, /help, or future Telegram updates appear dead.
    if text.startswith(("/start", "/help")):
        bot.handle(message)
        return

    if bot.URL_RE.search(text):
        enqueue(message)
        return

    bot.handle(message)


def main():
    me = bot.tg("getMe", timeout=30)
    bot.tg("deleteWebhook", data={"drop_pending_updates": "false"}, timeout=30)

    threading.Thread(target=worker, name="media-worker", daemon=True).start()

    print(f"Responsive Telegram runner started as @{me.get('username', 'unknown')}", flush=True)
    print(
        "Polling is isolated from media processing; one background media worker is active.",
        flush=True,
    )

    offset = None
    conflict_backoff = 3

    while True:
        try:
            params = {
                "timeout": 50,
                "allowed_updates": json.dumps(["message"]),
            }
            if offset is not None:
                params["offset"] = offset

            response = requests.get(f"{bot.API}/getUpdates", params=params, timeout=60)
            if response.status_code == 409:
                print("Telegram polling conflict; retrying.", flush=True)
                time.sleep(conflict_backoff)
                conflict_backoff = min(30, conflict_backoff + 3)
                continue

            response.raise_for_status()
            conflict_backoff = 3
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(payload)

            for update in payload.get("result", []):
                # Advance the offset before dispatching. Dispatch is intentionally
                # non-blocking for heavy media jobs, so no second getUpdates call
                # is needed to acknowledge an update.
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    try:
                        dispatch(message)
                    except Exception as exc:
                        print("DISPATCH_ERROR:", bot.clean_error(exc), flush=True)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("Polling error:", bot.clean_error(exc), flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
