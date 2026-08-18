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
from local_ai import summarize, transcribe

TOKEN = re.sub(r"\s+", "", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
if not TOKEN or ":" not in TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set or invalid")

API = f"https://api.telegram.org/bot{TOKEN}"
URL_RE = re.compile(r"https?://[^\s]+", re.I)
MAX_BYTES = int(os.environ.get("MAX_TELEGRAM_VIDEO_MB", "49")) * 1024 * 1024
SAFE_BYTES = min(MAX_BYTES - 3 * 1024 * 1024, 46 * 1024 * 1024)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def clean_error(exc):
    text = str(exc).replace(TOKEN, "***")
    return re.sub(r"\s+", " ", text).strip()[-700:]


def tg(method, *, data=None, files=None, params=None, timeout=90):
    r = requests.post(f"{API}/{method}", data=data, files=files, params=params, timeout=timeout)
    r.raise_for_status()
    p = r.json()
    if not p.get("ok"):
        raise RuntimeError(p)
    return p["result"]


def send(chat_id, text):
    return tg("sendMessage", data={"chat_id": chat_id, "text": text})


def send_long(chat_id, text, size=3800):
    text = (text or "").strip()
    while text:
        if len(text) <= size:
            send(chat_id, text); return
        cut = text.rfind("\n", 0, size)
        if cut < size // 2: cut = text.rfind(" ", 0, size)
        if cut < size // 2: cut = size
        send(chat_id, text[:cut].rstrip())
        text = text[cut:].lstrip()


def edit(chat_id, mid, text):
    return tg("editMessageText", data={"chat_id": chat_id, "message_id": mid, "text": text})


def action(chat_id, name):
    try: tg("sendChatAction", data={"chat_id": chat_id, "action": name}, timeout=20)
    except Exception: pass


def platform(url):
    h = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if h == "youtu.be" or "youtube.com" in h: return "YouTube"
    if "tiktok.com" in h: return "TikTok"
    if "instagram.com" in h: return "Instagram"
    if "threads.net" in h or "threads.com" in h: return "Threads"
    if h == "vk.com" or h.endswith(".vk.com") or "vkvideo.ru" in h: return "VK"
    return "сайт"


def opts(tmpdir, client=None):
    o = {
        "format": "best[ext=mp4][filesize<=46M]/best[filesize<=46M]/best[ext=mp4][height<=480]/best[height<=480]/best[height<=360]/best",
        "outtmpl": str(Path(tmpdir) / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True, "restrictfilenames": True, "merge_output_format": "mp4",
        "retries": 3, "fragment_retries": 3, "extractor_retries": 2, "socket_timeout": 30,
        "concurrent_fragment_downloads": 4, "http_chunk_size": 10 * 1024 * 1024,
        "http_headers": {"User-Agent": USER_AGENT},
        "js_runtimes": {"node": {"path": "/usr/local/bin/node"}},
    }
    if client:
        o["extractor_args"] = {"youtube": {"player_client": [client]}, "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]}}
    return o


def newest(tmpdir):
    files = sorted([p for p in Path(tmpdir).iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))], key=lambda p:p.stat().st_mtime, reverse=True)
    if not files: raise RuntimeError("Downloaded file was not found")
    mp4 = [p for p in files if p.suffix.lower() == ".mp4"]
    return mp4[0] if mp4 else files[0]


def direct_meta(url, tmpdir):
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, allow_redirects=True); r.raise_for_status()
    body = r.text
    def meta(prop):
        for pat in [rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)', rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']']:
            m = re.search(pat, body, re.I)
            if m: return html.unescape(m.group(1))
    media_url = meta("og:video") or meta("og:video:secure_url") or meta("og:image")
    if not media_url: raise RuntimeError("Public page does not expose downloadable media metadata")
    title = re.sub(r"\s+", " ", meta("og:title") or "Media").strip()
    m = requests.get(media_url, headers={"User-Agent":USER_AGENT,"Referer":url}, timeout=90, stream=True); m.raise_for_status()
    ct = (m.headers.get("content-type") or "").split(";",1)[0].lower()
    suffix = mimetypes.guess_extension(ct) or (".mp4" if "video" in ct else ".jpg")
    path = Path(tmpdir) / f"direct-media{suffix}"
    with path.open("wb") as f:
        for chunk in m.iter_content(1024*1024):
            if chunk: f.write(chunk)
    return path, title


def download(url, tmpdir):
    p = platform(url); errors=[]
    attempts = ["mweb","web_safari","android_vr"] if p == "YouTube" else [None]
    for client in attempts:
        try:
            with yt_dlp.YoutubeDL(opts(tmpdir, client)) as ydl:
                info = ydl.extract_info(url, download=True)
                title = re.sub(r"\s+", " ", info.get("title") or "Видео").strip()
            return newest(tmpdir), title
        except Exception as exc:
            errors.append(f"{p}/{client or 'generic'}: {type(exc).__name__}: {clean_error(exc)}")
            for f in Path(tmpdir).iterdir():
                try:
                    if f.is_file(): f.unlink()
                except OSError: pass
    if p in {"Instagram","Threads"}:
        try: return direct_meta(url,tmpdir)
        except Exception as exc: errors.append(f"{p}/og-meta: {clean_error(exc)}")
    raise RuntimeError(" | ".join(errors))


def probe(path):
    r = subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height:format=duration","-of","json",str(path)], capture_output=True,text=True,check=True,timeout=30)
    p=json.loads(r.stdout or "{}"); s=(p.get("streams") or [{}])[0]
    return int(s.get("width") or 0), int(s.get("height") or 0), max(1,int(round(float((p.get("format") or {}).get("duration") or 0))))


def send_video(chat_id,path,caption=""):
    w,h,d=probe(path); data={"chat_id":chat_id,"caption":caption[:1024],"supports_streaming":"true","duration":str(d)}
    if w and h: data.update(width=str(w),height=str(h))
    with path.open("rb") as f: return tg("sendVideo",data=data,files={"video":(path.name,f,"video/mp4")},timeout=600)


def send_doc(chat_id,path,caption=""):
    mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as f: return tg("sendDocument",data={"chat_id":chat_id,"caption":caption[:1024]},files={"document":(path.name,f,mime)},timeout=600)


def send_photo(chat_id,path,caption=""):
    mime=mimetypes.guess_type(path.name)[0] or "image/jpeg"
    with path.open("rb") as f: return tg("sendPhoto",data={"chat_id":chat_id,"caption":caption[:1024]},files={"photo":(path.name,f,mime)},timeout=300)


def duration(path):
    r=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(path)],capture_output=True,text=True,check=True,timeout=30)
    return float(r.stdout.strip())


def split_video(path,tmpdir):
    if path.stat().st_size <= MAX_BYTES: return [path]
    sec=max(30,int(duration(path)*SAFE_BYTES/path.stat().st_size*0.82)); pattern=str(Path(tmpdir)/"part-%03d.mp4")
    subprocess.run(["ffmpeg","-y","-fflags","+genpts","-i",str(path),"-map","0:v:0","-map","0:a:0?","-c","copy","-f","segment","-segment_time",str(sec),"-reset_timestamps","1",pattern],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=300)
    parts=sorted(Path(tmpdir).glob("part-*.mp4"))
    if not parts: raise RuntimeError("Video splitting produced no files")
    return parts


def safe_name(text):
    x=re.sub(r"[^\w\- .]+","",text or "",flags=re.UNICODE).strip().replace(" ","-")
    return (re.sub(r"-+","-",x).strip("-.")[:80] or "video-note")


def note_file(tmpdir,title,url,p,summary,transcript):
    path=Path(tmpdir)/f"{safe_name(title)}.md"
    path.write_text(f"# {title}\n\n- Источник: {p}\n- Ссылка: {url}\n- Создано: {time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime())}\n\n## Выжимка\n\n{summary}\n\n## Полная транскрипция\n\n{transcript}\n",encoding="utf-8")
    return path


def local_pipeline(chat_id,mid,path,tmpdir,title,url,p):
    edit(chat_id,mid,f"🧠 {p}: локально распознаю речь (без OpenAI API)…")
    transcript=transcribe(path)
    edit(chat_id,mid,f"✨ {p}: делаю локальную выжимку…")
    summary=summarize(transcript)
    send_long(chat_id,f"📝 {title}\n\n{summary}")
    note=note_file(tmpdir,title,url,p,summary,transcript)
    send_doc(chat_id,note,"📚 Obsidian-ready заметка: выжимка + полная транскрипция")


def handle(message):
    chat_id=(message.get("chat") or {}).get("id"); text=(message.get("text") or "").strip()
    if not chat_id: return
    if text.startswith(("/start","/help")):
        send(chat_id,"Пришли ссылку на видео. Скачаю медиа, локально распознаю речь через Whisper, сделаю выжимку и .md заметку для Obsidian. OpenAI API не используется."); return
    m=URL_RE.search(text)
    if not m: send(chat_id,"Пришли ссылку, начинающуюся с http:// или https://"); return
    url=m.group(0).rstrip(".,;!?)\"]}"); p=platform(url); status=send(chat_id,f"⏳ {p}: скачиваю медиа…"); mid=status["message_id"]
    try:
        with tempfile.TemporaryDirectory(prefix="tg-video-") as tmpdir:
            path,title=download(url,tmpdir); suffix=path.suffix.lower()
            if suffix in {".jpg",".jpeg",".png",".webp"}:
                edit(chat_id,mid,f"📤 {p}: отправляю фото…"); send_photo(chat_id,path,title)
            else:
                parts=split_video(path,tmpdir)
                for i,part in enumerate(parts,1):
                    action(chat_id,"upload_video"); edit(chat_id,mid,f"📤 {p}: отправляю видео" + (f" {i}/{len(parts)}…" if len(parts)>1 else "…"))
                    cap=title if len(parts)==1 else f"{title}\nЧасть {i}/{len(parts)}"
                    if part.suffix.lower()==".mp4": send_video(chat_id,part,cap)
                    else: send_doc(chat_id,part,cap)
                try: local_pipeline(chat_id,mid,path,tmpdir,title,url,p)
                except Exception as exc:
                    print("LOCAL_AI_ERROR:",clean_error(exc),flush=True); send(chat_id,"⚠️ Видео скачано, но локальный разбор не завершился.\n\nТехническая причина: "+clean_error(exc))
        try: tg("deleteMessage",data={"chat_id":chat_id,"message_id":mid})
        except Exception: pass
    except Exception as exc:
        print("DOWNLOAD_ERROR:",clean_error(exc),flush=True)
        try: edit(chat_id,mid,f"❌ {p}: не получилось обработать медиа.\n\nТехническая причина:\n{clean_error(exc)}")
        except Exception: send(chat_id,"❌ Ошибка при обработке ссылки.")


def main():
    me=tg("getMe",timeout=30); tg("deleteWebhook",data={"drop_pending_updates":"false"},timeout=30)
    print(f"Telegram video bot started as @{me.get('username','unknown')}",flush=True)
    print(f"yt-dlp: {yt_dlp.version.__version__}; local Whisper enabled",flush=True)
    offset=None
    while True:
        try:
            params={"timeout":50,"allowed_updates":json.dumps(["message"])}
            if offset is not None: params["offset"]=offset
            r=requests.get(f"{API}/getUpdates",params=params,timeout=60); r.raise_for_status(); payload=r.json()
            if not payload.get("ok"): raise RuntimeError(payload)
            for u in payload["result"]:
                offset=u["update_id"]+1
                if u.get("message"): handle(u["message"])
        except KeyboardInterrupt: break
        except Exception as exc: print("Polling error:",clean_error(exc),flush=True); time.sleep(3)

if __name__=="__main__": main()
