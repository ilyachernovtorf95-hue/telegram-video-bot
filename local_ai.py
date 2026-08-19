import os
import re
import signal
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
LLM_TIMEOUT = max(30, min(120, int(os.environ.get("LOCAL_LLM_TIMEOUT", "75"))))

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
    "мой", "моя", "мои", "ваш", "ваша", "ваши", "один", "два", "три", "этого", "этой", "этим",
}

CORRECTIONS = [
    (r"\b(?:чат\s*джи\s*пи\s*ти|чатджипити|чат\s*гпт|чаджипити|чаджи\s*пити|джипити|chatgpt|chatgbt|chatgbt)\b", "ChatGPT"),
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
    return text.strip()


def _prepare_audio(video_path: Path, workdir: Path) -> Path:
    wav = workdir / "speech.wav"
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
        "-af", "highpass=f=70,lowpass=f=7800,loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:a", "pcm_s16le", str(wav),
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
    if result.returncode != 0 or not wav.exists() or wav.stat().st_size < 1024:
        err = result.stderr.decode("utf-8", errors="ignore")[-900:]
        raise RuntimeError("Не удалось подготовить аудио: " + err)
    return wav


def transcribe(path: Path, hint: str = "") -> str:
    if not Path(WHISPER_BIN).is_file():
        raise RuntimeError("whisper-cli не найден в контейнере")
    if not Path(WHISPER_MODEL).is_file():
        raise RuntimeError("Модель Whisper не найдена")

    with tempfile.TemporaryDirectory(prefix="local-asr-") as td:
        workdir = Path(td)
        audio = _prepare_audio(path, workdir)
        outbase = workdir / "transcript"
        prompt = INITIAL_PROMPT
        if hint:
            prompt += " Контекст/название видео: " + _cleanup(hint)[:300]
        cmd = [
            WHISPER_BIN, "-m", WHISPER_MODEL, "-f", str(audio), "-l", LANGUAGE,
            "-t", str(THREADS), "-bs", "5", "-bo", "5", "-tp", "0.0", "-tpi", "0.2",
            "-sns", "-nt", "-otxt", "-of", str(outbase), "--prompt", prompt,
            "--carry-initial-prompt", "-np",
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1800)
        txt_path = Path(str(outbase) + ".txt")
        if result.returncode != 0 or not txt_path.exists():
            err = (result.stderr or result.stdout or "")[-1400:]
            raise RuntimeError("Whisper завершился ошибкой: " + err)
        text = _cleanup(txt_path.read_text(encoding="utf-8", errors="ignore"))

    if not text:
        raise RuntimeError("Речь в видео не распознана")
    print(f"LOCAL_ASR_OK model={Path(WHISPER_MODEL).name} chars={len(text)}", flush=True)
    return text


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-zА-Яа-яЁё0-9-]{3,}", text)]


def _sentences(text: str) -> list[str]:
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


def _similarity(a: str, b: str) -> float:
    sa = set(_tokens(a)) - RU_STOP
    sb = set(_tokens(b)) - RU_STOP
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))


def _compact_source(text: str, max_chars: int = 2200) -> str:
    text = _cleanup(text)
    if len(text) <= max_chars:
        return text
    sents = _sentences(text)
    if not sents:
        return text[:max_chars]
    # Preserve the opening and ending, then add central non-duplicate sentences.
    chosen = []
    for i in list(range(min(3, len(sents)))) + list(range(max(0, len(sents) - 2), len(sents))):
        if i not in [x[0] for x in chosen]:
            chosen.append((i, sents[i]))
    ranked = []
    for i, s in enumerate(sents):
        centrality = sum(_similarity(s, other) for j, other in enumerate(sents) if j != i)
        ranked.append((centrality, i, s))
    for _, i, s in sorted(ranked, reverse=True):
        if any(_similarity(s, old) > 0.55 for _, old in chosen):
            continue
        if sum(len(x[1]) + 1 for x in chosen) + len(s) > max_chars:
            continue
        chosen.append((i, s))
    return " ".join(s for _, s in sorted(chosen))[:max_chars]


def _resolve_llm_model() -> Path:
    configured = Path(LLM_MODEL)
    if configured.is_file():
        return configured
    candidates = sorted(Path("/opt/models").glob("*qwen2.5*0.5b*instruct*.gguf"))
    if candidates:
        return candidates[0]
    raise RuntimeError(f"Файл локальной LLM не найден: {LLM_MODEL}")


def _run_bounded(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()
        stdout, stderr = proc.communicate()
        raise RuntimeError(f"Локальная LLM превысила жёсткий лимит {timeout} секунд")
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _parse_marked(raw: str) -> dict:
    raw = (raw or "").replace("<|im_end|>", "").replace("<|im_start|>", "").strip()
    if not raw:
        raise RuntimeError("Локальная LLM вернула пустой ответ")

    def section(name: str, next_names: list[str]) -> str:
        tail = raw
        m = re.search(rf"(?im)^\s*{re.escape(name)}\s*:\s*", tail)
        if not m:
            return ""
        start = m.end()
        end = len(tail)
        for nxt in next_names:
            n = re.search(rf"(?im)^\s*{re.escape(nxt)}\s*:\s*", tail[start:])
            if n:
                end = min(end, start + n.start())
        return tail[start:end].strip()

    short = _cleanup(section("КРАТКО", ["ТЕЗИСЫ", "ДЕЙСТВИЯ", "ТЕГИ", "КОНЕЦ"]))
    points_raw = section("ТЕЗИСЫ", ["ДЕЙСТВИЯ", "ТЕГИ", "КОНЕЦ"])
    actions_raw = section("ДЕЙСТВИЯ", ["ТЕГИ", "КОНЕЦ"])
    tags_raw = section("ТЕГИ", ["КОНЕЦ"])

    def lines(value: str, limit: int) -> list[str]:
        items = []
        for line in value.splitlines():
            line = _cleanup(re.sub(r"^[\s•*\-\d.)]+", "", line))
            if len(line) < 8:
                continue
            if any(_similarity(line, old) > 0.68 for old in items):
                continue
            items.append(line)
            if len(items) >= limit:
                break
        return items

    points = lines(points_raw, 5)
    actions = lines(actions_raw, 3)
    tags = []
    for tag in re.split(r"[,\s#]+", tags_raw):
        tag = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]", "", tag.lower())
        if len(tag) >= 3 and tag not in tags:
            tags.append(tag)
        if len(tags) >= 6:
            break
    if len(short) < 35 or len(points) < 2:
        raise RuntimeError("LLM вернула неполный структурированный ответ")
    return {"summary": short, "points": points, "actions": actions, "tags": tags}


def _format(summary: str, points: list[str], actions: list[str], tags: list[str]) -> str:
    if not actions:
        actions = ["Сохранить ключевые идеи ролика и при необходимости проверить исходную транскрипцию."]
    tags = ["#" + re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]", "", t.lower().lstrip("#")) for t in tags]
    tags = [t for i, t in enumerate(tags) if len(t) > 1 and t not in tags[:i]][:6] or ["#видео", "#заметка"]
    return (
        f"🧠 КРАТКО\n{summary}\n\n"
        "💡 ГЛАВНЫЕ МЫСЛИ\n" + "\n".join("• " + x for x in points[:5]) + "\n\n"
        "🎯 ЧТО СТОИТ ЗАПОМНИТЬ / СДЕЛАТЬ\n" + "\n".join("• " + x for x in actions[:3]) + "\n\n"
        "🏷 ТЕГИ\n" + " ".join(tags)
    )


def _semantic_summary(text: str, title: str = "") -> str:
    if not Path(LLAMA_BIN).is_file():
        raise RuntimeError("llama-cli не найден")
    model = _resolve_llm_model()
    source = _compact_source(text, 2200)
    prompt = f"""Ты редактор русскоязычных заметок. Ниже автоматическая транскрипция видео.
Твоя задача: понять смысл, исправить только очевидные ошибки распознавания по контексту, не выдумывать факты и не копировать длинные фразы дословно.

Контекст/название: {title or 'не указан'}
Транскрипция: {source}

Ответь СТРОГО в таком коротком формате и закончи словом КОНЕЦ:
КРАТКО: 2 коротких предложения с главным смыслом.
ТЕЗИСЫ:
- 3–5 разных тезисов своими словами
ДЕЙСТВИЯ:
- 0–3 полезных вывода или действия, только если следуют из ролика
ТЕГИ: 5–6 тематических слов через запятую
КОНЕЦ
"""
    cmd = [
        LLAMA_BIN, "-m", str(model), "-t", str(THREADS),
        "-c", "1024", "-n", "180",
        "--temp", "0.10", "--top-p", "0.85", "--repeat-penalty", "1.12",
        "--no-display-prompt", "--no-show-timings", "--no-warmup", "-no-cnv",
        "-p", prompt,
    ]
    print(f"LOCAL_LLM_START model={model.name} chars_in={len(source)} timeout={LLM_TIMEOUT}", flush=True)
    result = _run_bounded(cmd, LLM_TIMEOUT)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "")[-1400:]
        raise RuntimeError(f"llama-cli code={result.returncode}: {err}")
    data = _parse_marked(result.stdout)
    output = _format(data["summary"], data["points"], data["actions"], data["tags"])
    print(f"LOCAL_LLM_OK model={model.name} chars_out={len(output)}", flush=True)
    return output


def _fallback_summary(text: str, title: str = "") -> str:
    sents = _sentences(text)
    if not sents:
        cleaned = _cleanup(text)
        return _format(cleaned[:500] or "В ролике недостаточно распознанной речи.", ["Недостаточно связного текста для анализа."], [], ["видео", "заметка"])

    # Lightweight LexRank/MMR-style extractive fallback. It always finishes quickly
    # and produces a useful note even if the local generative model cannot run.
    ranked = []
    for i, s in enumerate(sents):
        centrality = sum(_similarity(s, other) for j, other in enumerate(sents) if j != i)
        if i == 0:
            centrality += 0.20
        if i == len(sents) - 1:
            centrality += 0.10
        ranked.append((centrality, i, s))

    selected = []
    for score, i, s in sorted(ranked, reverse=True):
        if any(_similarity(s, old[2]) > 0.50 for old in selected):
            continue
        selected.append((score, i, s))
        if len(selected) >= min(5, len(sents)):
            break
    selected_by_score = sorted(selected, reverse=True)
    points = [x[2] for x in selected_by_score[:4]]
    summary = " ".join(x[2] for x in selected_by_score[:2])[:650]

    action_re = re.compile(r"\b(попрос|сдела|попроб|использ|сохрани|напиши|проверь|добав|созда|найди|узнай|можно|нужно|стоит)\w*", re.I)
    actions = []
    for s in sents:
        if action_re.search(s) and not any(_similarity(s, old) > 0.55 for old in actions):
            actions.append(s)
        if len(actions) >= 3:
            break

    words = [w for w in _tokens((title or "") + " " + text) if w not in RU_STOP and len(w) >= 4 and not w.isdigit()]
    freq = Counter(words)
    tags = []
    priority = ["chatgpt", "openai", "нейросеть", "генеалогия", "родственники", "семейное", "древо"]
    for p in priority:
        if p in freq and p not in tags:
            tags.append(p)
    for w, _ in freq.most_common(30):
        if w not in tags and not re.search(r"^(котор|потом|теперь|даже|очень|просто)", w):
            tags.append(w)
        if len(tags) >= 6:
            break

    print("LOCAL_SUMMARY_MODE=extractive_fallback", flush=True)
    return _format(summary, points, actions, tags)


def summarize(text: str, title: str = "") -> str:
    cleaned = _cleanup(text)
    try:
        return _semantic_summary(cleaned, title=title)
    except Exception as exc:
        print("LOCAL_LLM_FALLBACK:", str(exc)[-1400:], flush=True)
        return _fallback_summary(cleaned, title=title)
