import os
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

WHISPER_BIN = os.environ.get("WHISPER_CPP_BIN", "/usr/local/bin/whisper-cli")
WHISPER_MODEL = os.environ.get("WHISPER_CPP_MODEL", "/opt/models/ggml-tiny.bin")
THREADS = max(1, min(2, int(os.environ.get("LOCAL_AI_THREADS", "1"))))
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "ru").strip().lower() or "ru"

INITIAL_PROMPT = os.environ.get(
    "WHISPER_INITIAL_PROMPT",
    "Русская речь. Правильно распознавай названия и термины: ChatGPT, OpenAI, GPT-4, GPT-5, "
    "YouTube, TikTok, Instagram, Telegram, Obsidian, Whisper, искусственный интеллект.",
)

STOP = {
    "это","как","что","в","и","на","с","по","для","не","а","но","к","у","из","за","то","же","бы","мы",
    "вы","я","он","она","они","его","ее","её","их","так","там","тут","вот","уже","еще","ещё","если","или",
    "при","про","от","до","быть","есть","был","была","были","будет","можно","нужно","который","которая",
    "которые","этот","эта","эти","такой","также","очень","просто","себя","меня","тебя","вам","нас","вас",
    "чтобы","потом","теперь","тогда","когда","где","здесь","все","всё","свой","свои","свою","более","мой",
    "моя","мои","ваш","ваша","ваши","один","два","три","этого","этой","этим"
}

CORRECTIONS = [
    (r"\b(?:чат\s*джи\s*пи\s*ти|чатджипити|чат\s*гпт|чаджипити|джипити|chatgpt|chatgbt)\b", "ChatGPT"),
    (r"\b(?:опен\s*эй\s*ай|оупен\s*эй\s*ай|опенай)\b", "OpenAI"),
    (r"\b(?:джи\s*пи\s*ти)[ -]?(4|5)\b", r"GPT-\1"),
    (r"\b(?:обсидиан|обсидиум)\b", "Obsidian"),
    (r"\b(?:виспер|уиспер|вишпер)\b", "Whisper"),
]


def _cleanup(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    for pattern, replacement in CORRECTIONS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([.!?]){2,}", r"\1", text)
    return text.strip()


def _prepare_audio(video_path, workdir):
    wav = workdir / "speech.wav"
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
        "-af", "highpass=f=70,lowpass=f=7800,loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:a", "pcm_s16le", str(wav),
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
    if result.returncode != 0 or not wav.exists() or wav.stat().st_size < 1024:
        err = result.stderr.decode("utf-8", errors="ignore")[-700:]
        raise RuntimeError("Не удалось подготовить аудио: " + err)
    return wav


def transcribe(path, hint=""):
    if not Path(WHISPER_BIN).is_file():
        raise RuntimeError("whisper-cli не найден")
    if not Path(WHISPER_MODEL).is_file():
        raise RuntimeError("локальная резервная модель Whisper не найдена")
    with tempfile.TemporaryDirectory(prefix="fallback-asr-") as td:
        workdir = Path(td)
        audio = _prepare_audio(Path(path), workdir)
        outbase = workdir / "transcript"
        prompt = INITIAL_PROMPT + ((" Контекст: " + _cleanup(hint)[:240]) if hint else "")
        cmd = [
            WHISPER_BIN, "-m", WHISPER_MODEL, "-f", str(audio), "-l", LANGUAGE,
            "-t", str(THREADS), "-bs", "3", "-bo", "3", "-nt", "-np",
            "-otxt", "-of", str(outbase), "--prompt", prompt,
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=900)
        txt = Path(str(outbase) + ".txt")
        if result.returncode != 0 or not txt.exists():
            err = (result.stderr or result.stdout or "")[-900:]
            raise RuntimeError("Whisper fallback завершился ошибкой: " + err)
        text = _cleanup(txt.read_text(encoding="utf-8", errors="ignore"))
    if not text:
        raise RuntimeError("Речь в видео не распознана")
    print(f"FALLBACK_ASR_OK model={Path(WHISPER_MODEL).name} chars={len(text)}", flush=True)
    return text


def _tokens(text):
    return [w.lower() for w in re.findall(r"[A-Za-zА-Яа-яЁё0-9-]{3,}", text)]


def _sentences(text):
    out, seen = [], set()
    for s in re.split(r"(?<=[.!?])\s+|\n+", _cleanup(text)):
        s = s.strip(" •\t-")
        if len(s) < 24:
            continue
        key = re.sub(r"\W+", "", s.lower())
        if key and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _similarity(a, b):
    sa, sb = set(_tokens(a)) - STOP, set(_tokens(b)) - STOP
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))


def _extractive(text, limit=6):
    sents = _sentences(text)
    if not sents:
        return []
    words = [w for w in _tokens(text) if w not in STOP]
    freq = Counter(words)
    scored = []
    for i, sent in enumerate(sents):
        content = [w for w in _tokens(sent) if w not in STOP]
        lexical = sum(freq.get(w, 0) for w in content) / max(6, len(content))
        central = sum(_similarity(sent, other) for j, other in enumerate(sents) if j != i)
        position = 0.18 if i < 2 else 0.0
        scored.append((lexical + central + position, i, sent))
    chosen = []
    for _, i, sent in sorted(scored, reverse=True):
        if any(_similarity(sent, old[2]) > 0.58 for old in chosen):
            continue
        chosen.append((0, i, sent))
        if len(chosen) >= limit:
            break
    return [x[2] for x in sorted(chosen, key=lambda x: x[1])]


def _actions(sentences):
    markers = ("сдел", "нужно", "можно", "попроб", "использ", "напиш", "попрос", "проверь", "созда", "добав", "сохрани")
    out = []
    for sent in sentences:
        low = sent.lower()
        if any(marker in low for marker in markers):
            out.append(sent)
        if len(out) >= 4:
            break
    return out


def _tags(text, limit=7):
    freq = Counter(w for w in _tokens(text) if w not in STOP and len(w) >= 4)
    bad = {"которых","которыми","потому","сейчас","затем","даже","будете","можете","будет","своего","своей"}
    out = []
    for word, _ in freq.most_common(40):
        if word in bad:
            continue
        tag = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]", "", word)
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= limit:
            break
    return out


def fallback_analyze(path, title=""):
    transcript = transcribe(Path(path), hint=title)
    points = _extractive(transcript, 6)
    summary = " ".join(points[:2]) if points else transcript[:700]
    actions = _actions(points + _sentences(transcript))
    return {
        "engine": f"Local fallback ({Path(WHISPER_MODEL).name})",
        "language": LANGUAGE,
        "summary": summary[:1100],
        "main_points": points or [transcript[:500]],
        "key_facts": [],
        "actions": actions,
        "visual_context": [],
        "claims_to_verify": [],
        "chapters": [],
        "tags": _tags(transcript),
        "transcript": transcript,
    }
