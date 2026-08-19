import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

WHISPER_BIN = os.environ.get("WHISPER_CPP_BIN", "/usr/local/bin/whisper-cli")
WHISPER_MODEL = os.environ.get("WHISPER_CPP_MODEL", "/opt/models/ggml-small-q5_1.bin")
LLAMA_BIN = os.environ.get("LOCAL_LLM_BIN", "/usr/local/bin/llama-cli")
LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "/opt/models/qwen2.5-0.5b-instruct-q4_k_m.gguf")
THREADS = max(1, min(4, int(os.environ.get("LOCAL_AI_THREADS", "2"))))
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "ru").strip().lower() or "ru"

INITIAL_PROMPT = os.environ.get(
    "WHISPER_INITIAL_PROMPT",
    "Русская речь. Правильно распознавай имена, бренды и технические термины: "
    "ChatGPT, GPT-4, GPT-5, OpenAI, нейросеть, искусственный интеллект, "
    "YouTube, TikTok, Instagram, Telegram, Obsidian, Whisper, родственники, "
    "семейное древо, генеалогия. Не заменяй незнакомые слова случайными похожими словами.",
)

RU_STOP = {
    "это", "как", "что", "в", "и", "на", "с", "по", "для", "не", "а", "но", "к", "у", "из", "за",
    "то", "же", "бы", "мы", "вы", "я", "он", "она", "они", "его", "ее", "её", "их", "так", "там",
    "тут", "вот", "уже", "еще", "ещё", "если", "или", "при", "про", "от", "до", "быть", "есть",
    "был", "была", "были", "будет", "можно", "нужно", "который", "которая", "которые", "этот", "эта",
    "эти", "такой", "также", "очень", "просто", "себя", "меня", "тебя", "вам", "нас", "вас", "чтобы",
    "потом", "теперь", "тогда", "когда", "где", "здесь", "все", "всё", "свой", "свои", "свою", "более",
}

CORRECTIONS = [
    (r"\b(?:чат\s*джи\s*пи\s*ти|чатджипити|чат\s*гпт|чаджипити|чаджи\s*пити|джипити|chatgpt|chatgbt)\b", "ChatGPT"),
    (r"\b(?:опен\s*эй\s*ай|оупен\s*эй\s*ай|опенай)\b", "OpenAI"),
    (r"\b(?:джи\s*пи\s*ти)[ -]?(4|5)\b", r"GPT-\1"),
    (r"\b(?:обсидиан|обсидиум)\b", "Obsidian"),
    (r"\b(?:виспер|уиспер|вишпер)\b", "Whisper"),
]


def _cleanup(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    for pattern, replacement in CORRECTIONS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([.!?]){2,}", r"\1", text)
    text = re.sub(r"\b([^.!?]{16,100})\s+\1\b", r"\1", text, flags=re.IGNORECASE)
    return text.strip()


def _prepare_audio(video_path: Path, workdir: Path) -> Path:
    wav = workdir / "speech.wav"
    filters = "highpass=f=70,lowpass=f=7800,loudnorm=I=-16:TP=-1.5:LRA=11"
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
        "-af", filters, "-c:a", "pcm_s16le", str(wav),
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
    if result.returncode != 0 or not wav.exists() or wav.stat().st_size < 1024:
        err = result.stderr.decode("utf-8", errors="ignore")[-700:]
        raise RuntimeError("Не удалось подготовить аудио для распознавания: " + err)
    return wav


def transcribe(path: Path, hint: str = "") -> str:
    if not Path(WHISPER_BIN).exists():
        raise RuntimeError("whisper-cli не найден в контейнере")
    if not Path(WHISPER_MODEL).exists():
        raise RuntimeError("Локальная модель Whisper не найдена")

    with tempfile.TemporaryDirectory(prefix="local-asr-") as td:
        workdir = Path(td)
        audio = _prepare_audio(path, workdir)
        outbase = workdir / "transcript"
        prompt = INITIAL_PROMPT
        if hint:
            prompt += " Контекст/название видео: " + _cleanup(hint)[:350]

        cmd = [
            WHISPER_BIN,
            "-m", WHISPER_MODEL,
            "-f", str(audio),
            "-l", LANGUAGE,
            "-t", str(THREADS),
            "-bs", "5",
            "-bo", "5",
            "-tp", "0.0",
            "-tpi", "0.2",
            "-sns",
            "-nt",
            "-otxt",
            "-of", str(outbase),
            "--prompt", prompt,
            "--carry-initial-prompt",
            "-np",
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1800)
        txt_path = Path(str(outbase) + ".txt")
        if result.returncode != 0 or not txt_path.exists():
            err = (result.stderr or result.stdout or "")[-1200:]
            raise RuntimeError("Локальное распознавание речи завершилось ошибкой: " + err)
        text = _cleanup(txt_path.read_text(encoding="utf-8", errors="ignore"))

    if not text:
        raise RuntimeError("Не удалось распознать речь в видео")
    print(f"LOCAL_ASR engine=whisper.cpp model={Path(WHISPER_MODEL).name} lang={LANGUAGE}", flush=True)
    return text


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-zА-Яа-яЁё0-9-]{3,}", text)]


def _sentences(text: str) -> list[str]:
    items = []
    seen = set()
    for s in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = s.strip(" •\t")
        if len(s) < 25:
            continue
        key = re.sub(r"\W+", "", s.lower())
        if key and key not in seen:
            seen.add(key)
            items.append(s)
    return items


def _similarity(a: str, b: str) -> float:
    sa = set(_tokens(a)) - RU_STOP
    sb = set(_tokens(b)) - RU_STOP
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, min(len(sa), len(sb)))


def _compact_source(text: str, max_chars: int = 4500) -> str:
    text = _cleanup(text)
    if len(text) <= max_chars:
        return text
    sents = _sentences(text)
    if not sents:
        return text[:max_chars]

    words = [w for w in _tokens(text) if w not in RU_STOP and not w.isdigit()]
    freq = Counter(words)
    ranked = []
    for i, s in enumerate(sents):
        ws = [w for w in _tokens(s) if w not in RU_STOP]
        score = sum(freq.get(w, 0) for w in ws) / max(8, len(ws))
        if i < 3 or i >= len(sents) - 3:
            score *= 1.25
        ranked.append((score, i, s))

    chosen = []
    chars = 0
    for item in sorted(ranked, reverse=True):
        s = item[2]
        if any(_similarity(s, old[2]) > 0.72 for old in chosen):
            continue
        if chars + len(s) + 1 > max_chars:
            continue
        chosen.append(item)
        chars += len(s) + 1
    return " ".join(x[2] for x in sorted(chosen, key=lambda x: x[1]))


def _extract_json(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        raise RuntimeError("Локальная LLM вернула пустой ответ")
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("Локальная LLM не вернула JSON")
    try:
        obj = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("Локальная LLM вернула повреждённый JSON") from exc
    if not isinstance(obj, dict):
        raise RuntimeError("Локальная LLM вернула неожиданный формат")
    return obj


def _clean_list(value, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        item = _cleanup(str(item))
        if len(item) < 4:
            continue
        if any(_similarity(item, old) > 0.82 for old in out):
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _format_payload(obj: dict) -> str:
    short = _cleanup(str(obj.get("summary") or ""))
    points = _clean_list(obj.get("main_points"), 6)
    actions = _clean_list(obj.get("actions"), 4)
    tags_raw = obj.get("tags") if isinstance(obj.get("tags"), list) else []
    tags = []
    for tag in tags_raw:
        tag = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]", "", str(tag).strip().lstrip("#").lower())
        if len(tag) >= 3 and tag not in tags:
            tags.append("#" + tag)
        if len(tags) >= 7:
            break

    if len(short) < 40 or len(points) < 2:
        raise RuntimeError("Смысловая модель вернула слишком мало полезного содержания")
    if not actions:
        actions = ["Практических действий в ролике не заявлено; сохранить ключевые идеи как справочную заметку."]
    if not tags:
        tags = ["#видео", "#заметка"]

    return (
        f"🧠 КРАТКО\n{short}\n\n"
        "💡 ГЛАВНЫЕ МЫСЛИ\n" + "\n".join("• " + x for x in points) + "\n\n"
        "🎯 ЧТО СТОИТ ЗАПОМНИТЬ / СДЕЛАТЬ\n" + "\n".join("• " + x for x in actions) + "\n\n"
        "🏷 ТЕГИ\n" + " ".join(tags)
    )


def _run_llm(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Локальная LLM превысила лимит {timeout // 60} минут и была остановлена") from exc


def _semantic_summary(text: str, title: str = "") -> str:
    if not Path(LLAMA_BIN).exists():
        raise RuntimeError("llama-cli не найден в контейнере")
    if not Path(LLM_MODEL).exists():
        raise RuntimeError(f"Файл локальной LLM не найден: {LLM_MODEL}")

    source = _compact_source(text)
    system = (
        "Ты редактор русскоязычных заметок. Анализируй транскрипцию видео. "
        "Исправляй только очевидные ошибки распознавания по контексту. Не выдумывай факты. "
        "Не копируй длинные фразы дословно. Объединяй повторы и формулируй смысл своими словами."
    )
    user = f"""Название/контекст: {title or 'не указан'}

ТРАНСКРИПЦИЯ:
{source}

Верни JSON со следующими полями:
summary — 2–3 коротких предложения с главным смыслом ролика;
main_points — массив из 3–6 разных содержательных тезисов;
actions — массив из 1–4 полезных выводов или действий, только если они следуют из ролика;
tags — массив из 5–7 тематических тегов без символа #.
Все значения пиши на русском. Не повторяй один и тот же тезис в разных полях.
"""
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "main_points": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6},
            "actions": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 7},
        },
        "required": ["summary", "main_points", "actions", "tags"],
        "additionalProperties": False,
    }

    base = [
        LLAMA_BIN,
        "-m", LLM_MODEL,
        "-t", str(THREADS),
        "-c", "2048",
        "-n", "360",
        "--temp", "0.15",
        "--top-p", "0.9",
        "--repeat-penalty", "1.12",
        "--no-display-prompt",
        "--no-show-timings",
        "--no-warmup",
        "--jinja",
        "-st",
        "-j", json.dumps(schema, ensure_ascii=False),
        "-sys", system,
        "-p", user,
    ]

    print(f"LOCAL_LLM_START model={Path(LLM_MODEL).name} chars_in={len(source)} mode=json", flush=True)
    result = _run_llm(base, timeout=300)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "")[-1400:]
        raise RuntimeError(f"Локальная LLM завершилась с кодом {result.returncode}: {err}")

    obj = _extract_json(result.stdout)
    output = _format_payload(obj)
    print(f"LOCAL_SUMMARY model={Path(LLM_MODEL).name} chars_in={len(source)} chars_out={len(output)}", flush=True)
    return output


def _fallback_summary(text: str) -> str:
    text = _cleanup(text)
    sents = _sentences(text)
    if not sents:
        return "🧠 КРАТКО\n" + text[:1000] + "\n\n💡 ГЛАВНЫЕ МЫСЛИ\n• Недостаточно связного текста."
    words = [w for w in _tokens(text) if w not in RU_STOP]
    freq = Counter(words)
    scored = []
    for i, s in enumerate(sents):
        ws = [w for w in _tokens(s) if w not in RU_STOP]
        score = sum(freq.get(w, 0) for w in ws) / max(8, len(ws))
        scored.append((score, i, s))
    chosen = []
    for item in sorted(scored, reverse=True):
        if any(_similarity(item[2], x[2]) > 0.68 for x in chosen):
            continue
        chosen.append(item)
        if len(chosen) >= min(4, len(sents)):
            break
    chosen = sorted(chosen, key=lambda x: x[1])
    short = " ".join(x[2] for x in chosen[:2])[:800]
    bullets = "\n".join("• " + x[2] for x in chosen)
    tags = []
    for word, _ in freq.most_common(20):
        if len(word) >= 4 and word not in RU_STOP:
            tag = "#" + re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]", "", word)
            if tag not in tags:
                tags.append(tag)
        if len(tags) >= 6:
            break
    return (
        f"🧠 КРАТКО\n{short}\n\n💡 ГЛАВНЫЕ МЫСЛИ\n{bullets}\n\n"
        "🎯 ЧТО СТОИТ ЗАПОМНИТЬ / СДЕЛАТЬ\n• Смысловая модель временно не завершила обработку; сохранена полная транскрипция.\n\n"
        f"🏷 ТЕГИ\n{' '.join(tags)}"
    )


def summarize(text: str, title: str = "") -> str:
    try:
        return _semantic_summary(_cleanup(text), title=title)
    except Exception as exc:
        print("LOCAL_LLM_FALLBACK:", str(exc)[-1400:], flush=True)
        return _fallback_summary(text)
