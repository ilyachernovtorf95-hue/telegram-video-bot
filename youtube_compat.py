"""YouTube reliability layer for Railway.

Goals:
- keep Telegram polling lightweight and responsive;
- try several YouTube clients instead of relying on one fragile path;
- force IPv4 on cloud hosts;
- start the bgutil PO-token helper only while a YouTube job is running;
- optionally use user-supplied YouTube cookies and/or a proxy without storing secrets in git.

Optional Railway variables:
- YOUTUBE_COOKIES_B64: base64 of a Netscape-format cookies.txt file
- YOUTUBE_COOKIES: raw Netscape-format cookies.txt text (multiline)
- YOUTUBE_USER_AGENT: browser User-Agent matching the cookie session
- YOUTUBE_PROXY: optional http/https/socks proxy URL
"""

from __future__ import annotations

import base64
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

import yt_dlp

import bot


PO_HOST = "127.0.0.1"
PO_PORT = 4416
PO_SERVER = "/opt/bgutil-ytdlp-pot-provider/server/build/main.js"
YOUTUBE_COOKIE_PATH = Path("/tmp/youtube-cookies.txt")
YOUTUBE_USER_AGENT = os.environ.get("YOUTUBE_USER_AGENT", "").strip()
YOUTUBE_PROXY = os.environ.get("YOUTUBE_PROXY", "").strip()

_ORIGINAL_DOWNLOAD = bot.download
_ORIGINAL_YTDLP_OPTS = bot.ytdlp_opts


def _prepare_youtube_cookies() -> Optional[str]:
    raw_b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    raw_text = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not raw_b64 and not raw_text:
        return None

    try:
        if raw_b64:
            text = base64.b64decode(raw_b64, validate=True).decode("utf-8")
        else:
            text = raw_text
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
            raise ValueError("cookies.txt must be in Netscape format")
        if "youtube.com" not in text and "google.com" not in text:
            raise ValueError("cookie file does not contain YouTube/Google cookies")
        YOUTUBE_COOKIE_PATH.write_text(text.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
        YOUTUBE_COOKIE_PATH.chmod(0o600)
        print("YouTube authenticated cookie fallback enabled", flush=True)
        return str(YOUTUBE_COOKIE_PATH)
    except Exception as exc:
        print(f"YOUTUBE_COOKIES ignored: {type(exc).__name__}: {exc}", flush=True)
        return None


YOUTUBE_COOKIEFILE = _prepare_youtube_cookies()


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _start_po_provider():
    """Start PO-token helper for the duration of a YouTube job.

    Returns (process, owned). If an existing helper already listens on the port,
    it is reused and will not be terminated by this job.
    """
    if _port_open(PO_HOST, PO_PORT):
        return None, False
    if not Path(PO_SERVER).exists():
        print("YouTube PO-token server not present; continuing without it", flush=True)
        return None, False

    proc = subprocess.Popen(
        ["/usr/local/bin/node", PO_SERVER],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 8
    while time.time() < deadline:
        if proc.poll() is not None:
            print("YouTube PO-token server exited during startup", flush=True)
            return None, False
        if _port_open(PO_HOST, PO_PORT):
            print("YouTube PO-token helper started on demand", flush=True)
            return proc, True
        time.sleep(0.25)
    try:
        proc.terminate()
    except Exception:
        pass
    print("YouTube PO-token helper did not become ready; continuing", flush=True)
    return None, False


def _stop_po_provider(proc, owned: bool):
    if not proc or not owned:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _clear_tmp(tmpdir):
    for file in Path(tmpdir).iterdir():
        try:
            if file.is_file():
                file.unlink()
        except OSError:
            pass


def _youtube_opts(tmpdir, client: str, *, authenticated: bool):
    opts = _ORIGINAL_YTDLP_OPTS(tmpdir, client)

    # Railway egress may expose IPv6 and cloud-host paths that YouTube rejects.
    # Binding to 0.0.0.0 is yt-dlp's programmatic equivalent of --force-ipv4.
    opts["source_address"] = "0.0.0.0"
    opts["sleep_interval_requests"] = 1.0
    opts["retries"] = 5
    opts["fragment_retries"] = 5
    opts["extractor_retries"] = 3
    opts["socket_timeout"] = 35

    headers = dict(opts.get("http_headers") or {})
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    if authenticated and YOUTUBE_USER_AGENT:
        headers["User-Agent"] = YOUTUBE_USER_AGENT
    opts["http_headers"] = headers

    if authenticated and YOUTUBE_COOKIEFILE:
        opts["cookiefile"] = YOUTUBE_COOKIEFILE

    if YOUTUBE_PROXY:
        opts["proxy"] = YOUTUBE_PROXY

    extractor_args = dict(opts.get("extractor_args") or {})
    youtube_args = dict(extractor_args.get("youtube") or {})
    youtube_args["player_client"] = [client]
    extractor_args["youtube"] = youtube_args
    extractor_args.setdefault(
        "youtubepot-bgutilhttp",
        {"base_url": [f"http://{PO_HOST}:{PO_PORT}"]},
    )
    opts["extractor_args"] = extractor_args
    return opts


def _is_auth_or_ip_block(errors: list[str]) -> bool:
    text = " ".join(errors).lower()
    markers = (
        "sign in to confirm you're not a bot",
        "sign in to confirm you’re not a bot",
        "login_required",
        "http error 403",
        "403: forbidden",
    )
    return any(marker in text for marker in markers)


def _download_youtube(url, tmpdir):
    errors: list[str] = []
    po_proc, po_owned = _start_po_provider()

    # Public/anonymous routes first. web_embedded often avoids the standard web
    # gate for embeddable public videos; android_vr/web_safari are useful fallbacks.
    attempts = [
        ("web_embedded", False),
        ("android_vr", False),
        ("web_safari", False),
        ("mweb", False),
        ("tv", False),
    ]

    # If the user supplied a dedicated YouTube session, retry only clients that
    # support account cookies. This is the reliable path for cloud-IP bot checks.
    if YOUTUBE_COOKIEFILE:
        attempts.extend([
            ("web_safari", True),
            ("web", True),
            ("mweb", True),
            ("tv", True),
        ])

    try:
        for client, authenticated in attempts:
            label = f"{client}{'+cookies' if authenticated else ''}"
            try:
                with yt_dlp.YoutubeDL(_youtube_opts(tmpdir, client, authenticated=authenticated)) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = re.sub(r"\s+", " ", info.get("title") or "Видео").strip()
                print(f"YouTube download succeeded via {label}", flush=True)
                return bot.newest_media(tmpdir), title
            except Exception as exc:
                clean = bot.clean_error(exc)
                errors.append(f"YouTube/{label}: {type(exc).__name__}: {clean}")
                print(f"YouTube attempt failed via {label}: {clean}", flush=True)
                _clear_tmp(tmpdir)
    finally:
        _stop_po_provider(po_proc, po_owned)

    if _is_auth_or_ip_block(errors):
        if not YOUTUBE_COOKIEFILE and not YOUTUBE_PROXY:
            raise RuntimeError(
                "YouTube заблокировал запросы с IP Railway (проверка «не бот»/HTTP 403). "
                "Бот уже попробовал несколько клиентов, IPv4 и PO-token fallback. "
                "Для стабильной загрузки нужен один бесплатный шаг: добавить в Railway "
                "YOUTUBE_COOKIES_B64 (cookies.txt из отдельной YouTube-сессии). "
                "Код уже готов принять эти cookies; присылать их в чат не нужно."
            )
        if YOUTUBE_COOKIEFILE and not YOUTUBE_PROXY:
            raise RuntimeError(
                "YouTube продолжил блокировать Railway даже с cookies. Это ограничение IP дата-центра. "
                "Следующий резерв — YOUTUBE_PROXY через доверенный residential/ISP IP. "
                "Остальные платформы и сам Telegram-бот продолжают работать."
            )

    raise RuntimeError(" | ".join(errors[-5:]))


def patched_download(url, tmpdir):
    if bot.platform(url) == "YouTube":
        return _download_youtube(url, tmpdir)
    return _ORIGINAL_DOWNLOAD(url, tmpdir)


def install():
    bot.download = patched_download
    print(
        "YouTube reliability layer enabled: multi-client, IPv4, on-demand PO-token, "
        f"cookies={'yes' if YOUTUBE_COOKIEFILE else 'no'}, proxy={'yes' if YOUTUBE_PROXY else 'no'}",
        flush=True,
    )


def main():
    install()
    import bot_runner
    bot_runner.main()


if __name__ == "__main__":
    main()
