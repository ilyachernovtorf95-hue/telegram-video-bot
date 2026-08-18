import os
import re
from collections import Counter
from pathlib import Path

from faster_whisper import WhisperModel

_MODEL = None
MODEL_NAME = os.environ.get("WHISPER_MODEL", "tiny")

RU_STOP = {
    "это","как","что","в","и","на","с","по","для","не","а","но","к","у","из","за","то","же","бы","мы","вы","я","он","она","они","его","ее","их","так","там","тут","вот","уже","еще","если","или","при","про","от","до","быть","есть","был","была","были","будет","можно","нужно","который","которая","которые","этот","эта","эти","такой","также","очень","просто"
}


def _model():
    global _MODEL
    if _MODEL is None:
        _MODEL = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
    return _MODEL


def transcribe(path: Path) -> str:
    segments, _ = _model().transcribe(
        str(path), beam_size=1, vad_filter=True, condition_on_previous_text=False
    )
    text = " ".join((segment.text or "").strip() for segment in segments).strip()
    if not text:
        raise RuntimeError("Не удалось распознать речь в видео")
    return re.sub(r"\s+", " ", text)


def _sentences(text: str):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 25]


def summarize(text: str) -> str:
    sentences = _sentences(text)
    if not sentences:
        return "🧠 КРАТКО\n" + text[:1200] + "\n\n💡 ГЛАВНЫЕ МЫСЛИ\n• Недостаточно связного текста для автоматической выжимки."
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9-]{3,}", text.lower())
    freq = Counter(w for w in words if w not in RU_STOP)
    scored = []
    for i, sentence in enumerate(sentences):
        sw = re.findall(r"[A-Za-zА-Яа-яЁё0-9-]{3,}", sentence.lower())
        score = sum(freq[w] for w in sw if w in freq) / max(8, len(sw))
        scored.append((score, i, sentence))
    top = sorted(scored, reverse=True)[: min(6, len(scored))]
    chosen = [x[2] for x in sorted(top, key=lambda x: x[1])]
    short = " ".join(chosen[:2])[:1200]
    bullets = "\n".join("• " + s[:500] for s in chosen[:6])
    tags = " ".join("#" + re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]", "", w) for w, _ in freq.most_common(7))
    return f"🧠 КРАТКО\n{short}\n\n💡 ГЛАВНЫЕ МЫСЛИ\n{bullets}\n\n🎯 ЧТО СТОИТ ЗАПОМНИТЬ\n• Ключевые тезисы сохранены выше; проверь полную транскрипцию для контекста.\n\n🏷 ТЕГИ\n{tags}"
