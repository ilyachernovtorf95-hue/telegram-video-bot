import math
import os
import re
from collections import Counter
from pathlib import Path

from faster_whisper import WhisperModel

_MODEL = None
MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "auto").strip().lower()
BEAM_SIZE = max(1, min(8, int(os.environ.get("WHISPER_BEAM_SIZE", "5"))))

# Hint common terms that small Whisper models often mangle in Russian speech.
INITIAL_PROMPT = os.environ.get(
    "WHISPER_INITIAL_PROMPT",
    "Русская речь. ChatGPT, GPT, GPT-4, GPT-5, OpenAI, нейросеть, искусственный интеллект, "
    "YouTube, TikTok, Instagram, Telegram, Obsidian, Whisper. Имена и названия писать аккуратно.",
)

RU_STOP = {
    "это", "как", "что", "в", "и", "на", "с", "по", "для", "не", "а", "но", "к", "у", "из", "за",
    "то", "же", "бы", "мы", "вы", "я", "он", "она", "они", "его", "ее", "её", "их", "так", "там",
    "тут", "вот", "уже", "еще", "ещё", "если", "или", "при", "про", "от", "до", "быть", "есть",
    "был", "была", "были", "будет", "можно", "нужно", "который", "которая", "которые", "этот", "эта",
    "эти", "такой", "также", "очень", "просто", "себя", "меня", "тебя", "вам", "нас", "вас", "чтобы",
    "потом", "теперь", "тогда", "когда", "где", "здесь", "все", "всё", "свой", "свои", "свою", "более",
}

# Conservative post-corrections: only distinctive ASR distortions, not generic words.
CORRECTIONS = [
    (r"\b(?:чат\s*джи\s*пи\s*ти|чатджипити|чат\s*гпт|чаджипити|чаджи\s*пити|джипити)\b", "ChatGPT"),
    (r"\b(?:опен\s*эй\s*ай|оупен\s*эй\s*ай|опенай)\b", "OpenAI"),
    (r"\b(?:джи\s*пи\s*ти)[ -]?(4|5)\b", r"GPT-\1"),
    (r"\b(?:обсидиан|обсидиум)\b", "Obsidian"),
    (r"\b(?:виспер|уиспер|вишпер)\b", "Whisper"),
]


def _model():
    global _MODEL
    if _MODEL is None:
        # CPU threads are intentionally bounded for Railway shared CPUs.
        cpu_threads = max(1, min(4, int(os.environ.get("WHISPER_CPU_THREADS", "2"))))
        _MODEL = WhisperModel(
            MODEL_NAME,
            device="cpu",
            compute_type=COMPUTE_TYPE,
            cpu_threads=cpu_threads,
            num_workers=1,
        )
    return _MODEL


def _cleanup_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    for pattern, replacement in CORRECTIONS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # Remove accidental repeated neighboring phrases/sentences.
    text = re.sub(r"\b([^.!?]{12,80})\s+\1\b", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([.!?]){2,}", r"\1", text)
    if text and text[0].isalpha():
        text = text[0].upper() + text[1:]
    return text


def transcribe(path: Path) -> str:
    kwargs = dict(
        beam_size=BEAM_SIZE,
        best_of=BEAM_SIZE,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 350, "speech_pad_ms": 250},
        condition_on_previous_text=True,
        initial_prompt=INITIAL_PROMPT,
        temperature=0.0,
        repetition_penalty=1.08,
        no_repeat_ngram_size=3,
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,
    )
    if LANGUAGE and LANGUAGE != "auto":
        kwargs["language"] = LANGUAGE

    segments, info = _model().transcribe(str(path), **kwargs)
    pieces = []
    previous = ""
    for segment in segments:
        part = _cleanup_text((segment.text or "").strip())
        if not part:
            continue
        # Suppress exact/near duplicate segment emitted around VAD boundaries.
        key = re.sub(r"\W+", "", part.lower())
        prev_key = re.sub(r"\W+", "", previous.lower())
        if key and (key == prev_key or (len(key) > 30 and key in prev_key)):
            continue
        pieces.append(part)
        previous = part

    text = _cleanup_text(" ".join(pieces))
    if not text:
        raise RuntimeError("Не удалось распознать речь в видео")
    detected = getattr(info, "language", None)
    probability = getattr(info, "language_probability", None)
    if detected:
        print(f"WHISPER language={detected} probability={probability} model={MODEL_NAME} beam={BEAM_SIZE}", flush=True)
    return text


def _sentences(text: str) -> list[str]:
    raw = re.split(r"(?<=[.!?])\s+|\n+", text)
    result = []
    seen = set()
    for sentence in raw:
        sentence = sentence.strip(" •\t")
        if len(sentence) < 28:
            continue
        key = re.sub(r"\W+", "", sentence.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(sentence)
    return result


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-zА-Яа-яЁё0-9-]{3,}", text)]


def _keywords(text: str) -> Counter:
    tokens = [w for w in _tokens(text) if w not in RU_STOP and not w.isdigit()]
    # Mildly prefer specific/longer words over conversational filler.
    counts = Counter(tokens)
    weighted = Counter()
    for word, count in counts.items():
        weighted[word] = count * (1.0 + min(len(word), 12) / 20.0)
    return weighted


def _sentence_score(sentence: str, freq: Counter, position: int, total: int) -> float:
    words = [w for w in _tokens(sentence) if w not in RU_STOP]
    if not words:
        return 0.0
    lexical = sum(float(freq.get(w, 0)) for w in words) / math.sqrt(max(6, len(words)))
    # Intro/conclusion sentences frequently contain the point of short social videos.
    edge_bonus = 1.10 if position < max(1, total // 6) or position >= max(0, total - max(1, total // 6)) else 1.0
    # Penalize transcript fragments that are probably malformed.
    weird = sum(1 for w in words if len(w) > 22)
    quality = 1.0 / (1.0 + weird * 0.35)
    return lexical * edge_bonus * quality


def _similarity(a: str, b: str) -> float:
    sa = set(_tokens(a)) - RU_STOP
    sb = set(_tokens(b)) - RU_STOP
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, min(len(sa), len(sb)))


def _pick_diverse(scored: list[tuple[float, int, str]], limit: int) -> list[tuple[float, int, str]]:
    chosen = []
    for item in sorted(scored, reverse=True):
        sentence = item[2]
        if any(_similarity(sentence, old[2]) > 0.72 for old in chosen):
            continue
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen


def summarize(text: str) -> str:
    text = _cleanup_text(text)
    sentences = _sentences(text)
    if not sentences:
        return (
            "🧠 КРАТКО\n" + text[:1200] +
            "\n\n💡 ГЛАВНЫЕ МЫСЛИ\n• Речь слишком короткая или фрагментарная для надёжной автоматической выжимки.\n\n"
            "🎯 ЧТО СТОИТ ЗАПОМНИТЬ\n• Проверь полную транскрипцию."
        )

    freq = _keywords(text)
    scored = [(_sentence_score(s, freq, i, len(sentences)), i, s) for i, s in enumerate(sentences)]
    selected = _pick_diverse(scored, min(6, len(sentences)))
    selected_in_order = sorted(selected, key=lambda x: x[1])

    # The short summary uses the strongest two non-duplicate sentences, not simply the first two.
    short_items = sorted(selected, reverse=True)[:2]
    short_items = sorted(short_items, key=lambda x: x[1])
    short = " ".join(x[2] for x in short_items)[:1100]
    bullets = "\n".join("• " + x[2][:520] for x in selected_in_order)

    tags = []
    for word, _ in freq.most_common(20):
        normalized = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]", "", word)
        if len(normalized) < 4 or normalized in RU_STOP:
            continue
        if normalized.lower() in {t.lower().lstrip("#") for t in tags}:
            continue
        tags.append("#" + normalized)
        if len(tags) == 7:
            break

    # Avoid pretending this extractive local algorithm invented actions not present in the source.
    action_candidates = [s for s in sentences if re.search(r"\b(нужно|стоит|можно|попроб|сделай|делай|проверь|используй|запомни|важно)\w*\b", s, re.I)]
    if action_candidates:
        action_scored = [(_sentence_score(s, freq, sentences.index(s), len(sentences)), sentences.index(s), s) for s in action_candidates]
        actions = _pick_diverse(action_scored, 3)
        action_text = "\n".join("• " + x[2][:500] for x in sorted(actions, key=lambda x: x[1]))
    else:
        action_text = "• В видео нет явного практического действия; основные тезисы сохранены выше."

    return (
        f"🧠 КРАТКО\n{short}\n\n"
        f"💡 ГЛАВНЫЕ МЫСЛИ\n{bullets}\n\n"
        f"🎯 ЧТО СТОИТ ЗАПОМНИТЬ / СДЕЛАТЬ\n{action_text}\n\n"
        f"🏷 ТЕГИ\n{' '.join(tags)}"
    )
