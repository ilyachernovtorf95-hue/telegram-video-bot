"""Runtime compatibility hooks for Railway deployment.

If TIKTOK_COOKIES_B64 is configured, expose the Netscape cookies file to yt-dlp.
The secret stays in Railway and is written only to the container's /tmp directory.
"""
import base64
import os
from pathlib import Path


def _prepare_cookiefile() -> str | None:
    raw = os.environ.get("TIKTOK_COOKIES_B64", "").strip()
    if not raw:
        return None
    try:
        data = base64.b64decode(raw, validate=True)
        text = data.decode("utf-8").replace("\r\n", "\n")
        if not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
            raise ValueError("cookies must be in Netscape format")
        path = Path("/tmp/tiktok-cookies.txt")
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(0o600)
        return str(path)
    except Exception as exc:
        print(f"TIKTOK_COOKIES_B64 ignored: {type(exc).__name__}: {exc}", flush=True)
        return None


_cookiefile = _prepare_cookiefile()

if _cookiefile:
    try:
        import yt_dlp

        _original_init = yt_dlp.YoutubeDL.__init__

        def _patched_init(self, params=None, auto_init=True):
            params = dict(params or {})
            params.setdefault("cookiefile", _cookiefile)
            return _original_init(self, params, auto_init)

        yt_dlp.YoutubeDL.__init__ = _patched_init
        print("yt-dlp authenticated cookie fallback enabled", flush=True)
    except Exception as exc:
        print(f"Could not enable yt-dlp cookie fallback: {type(exc).__name__}: {exc}", flush=True)
