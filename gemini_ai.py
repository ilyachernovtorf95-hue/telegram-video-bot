import json
import mimetypes
import os
import re
import time
from pathlib import Path

import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-3.7-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
GEMINI_FILE_TIMEOUT = max(30, min(900, int(os.environ.get("GEMINI_FILE_TIMEOUT", "300"))))
GEMINI_REQUEST_TIMEOUT = max(60, min(1200, int(os.environ.get("GEMINI_REQUEST_TIMEOUT", "600"))))
GEMINI_UPLOAD_TIMEOUT = max(60, min(1200, int(os.environ.get("GEMINI_UPLOAD_TIMEOUT", "600"))))
BASE_URL = "https://generativelanguage.googleapis.com"
API_BASE = f"{BASE_URL}/v1beta"
UPLOAD_URL = f"{BASE_URL}/upload/v1beta/files"

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        "summary": {"type": "string"},
        "main_points": {"type": "array", "items": {"type": "string"}},
        "key_facts": {"type": "array", "items": {"type": "string"}},
        "actions": {"type": "array", "items": {"type": "string"}},
        "visual_context": {"type": "array", "items": {"type": "string"}},
        "claims_to_verify": {"type": "array", "items": {"type": "string"}},
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"time": {"type": "string"}, "title": {"type": "string"}},
                "required": ["time", "title"],
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "transcript": {"type": "string"},
    },
    "required": ["language", "summary", "main_points", "key_facts", "actions", "visual_context", "claims_to_verify", "chapters", "tags", "transcript"],
}

PROMPT = """Ты — профессиональный аналитик видео, редактор конспектов и точный транскрибатор.

Проанализируй ВЕСЬ ролик от начала до конца: речь, аудио, текст в кадре, демонстрации, схемы и визуальный контекст. Особенно важно не ограничиваться первыми минутами длинного интервью или подкаста.

Правила:
1. Не выдумывай факты, имена, цифры или выводы, которых нет в ролике.
2. Отделяй факты из ролика от мнений/прогнозов спикеров. Рекламу не смешивай с главными мыслями.
3. transcript — максимально полная транскрипция всей разборчивой речи с нормальной пунктуацией. Не пересказывай вместо транскрипции. Исправляй очевидные ошибки распознавания, бренды и общеизвестные термины по контексту. Если уверенно различаешь спикеров, используй нейтральные метки «Ведущий:» / «Гость:»; не угадывай личности.
4. Для длинного видео в transcript можно ставить редкие временные ориентиры при смене крупных тем.
5. summary — 4–7 ясных предложений своими словами: о чём весь ролик, главные выводы и почему это важно.
6. main_points — 5–12 разных содержательных тезисов, охватывающих весь ролик, без повторов.
7. key_facts — конкретные цифры, названия, примеры, события и проверяемые утверждения, реально прозвучавшие в ролике. Если их нет — пустой список.
8. actions — практические действия, решения или идеи, которые зритель действительно может применить. Если их нет — пустой список.
9. visual_context — только значимые детали изображения: схемы, интерфейсы, графики, демонстрации и надписи, добавляющие смысл.
10. claims_to_verify — спорные/проверяемые утверждения спикеров, которые разумно проверить внешними источниками. Не объявляй их истинными или ложными.
11. chapters — ключевые смысловые разделы по всему ролику с приблизительными таймкодами MM:SS или HH:MM:SS. Для длинного ролика дай достаточно глав, чтобы покрыть весь материал.
12. tags — 4–8 коротких тематических тегов без #.
13. Все аналитические поля — на русском. transcript оставь на языке речи.
14. Если часть аудио неразборчива, кратко пометь это, а не придумывай слова.
15. Верни только данные по заданной JSON-схеме."""


class GeminiError(RuntimeError):
    pass


def is_configured():
    return bool(GEMINI_API_KEY)


def _headers():
    return {"x-goog-api-key": GEMINI_API_KEY}


def _raise_for_response(response, context):
    if response.ok:
        return
    body = (response.text or "")[-1800:]
    raise GeminiError(f"{context}: HTTP {response.status_code}: {body}")


def _request_interaction(payload, context):
    last_error = None
    for attempt in range(3):
        try:
            response = requests.post(
                f"{API_BASE}/interactions",
                headers={**_headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=(30, GEMINI_REQUEST_TIMEOUT),
            )
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                wait = 4 * (attempt + 1)
                print(f"{context}: transient HTTP {response.status_code}; retry in {wait}s", flush=True)
                time.sleep(wait)
                continue
            _raise_for_response(response, context)
            return response.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt >= 2:
                break
            wait = 4 * (attempt + 1)
            print(f"{context}: transient {type(exc).__name__}; retry in {wait}s", flush=True)
            time.sleep(wait)
        except Exception:
            raise
    raise GeminiError(f"{context}: network timeout after retries: {last_error}")


def _upload_file(path):
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY не настроен")
    path = Path(path)
    size = path.stat().st_size
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    headers = {
        **_headers(),
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(size),
        "X-Goog-Upload-Header-Content-Type": mime,
        "Content-Type": "application/json",
    }
    response = requests.post(UPLOAD_URL, headers=headers, json={"file": {"display_name": path.name[:120]}}, timeout=(20, 60))
    _raise_for_response(response, "Gemini: не удалось начать загрузку")
    upload_url = response.headers.get("x-goog-upload-url")
    if not upload_url:
        raise GeminiError("Gemini не вернул URL для загрузки файла")
    with path.open("rb") as fh:
        response = requests.post(
            upload_url,
            headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"},
            data=fh,
            timeout=(30, GEMINI_UPLOAD_TIMEOUT),
        )
    _raise_for_response(response, "Gemini: не удалось загрузить видео")
    payload = response.json()
    file_info = payload.get("file") if isinstance(payload, dict) else None
    if not isinstance(file_info, dict):
        raise GeminiError("Gemini вернул неожиданный ответ после загрузки файла")
    return file_info


def _wait_until_active(file_info):
    name = str(file_info.get("name") or "").strip()
    if not name:
        raise GeminiError("Gemini не вернул имя загруженного файла")
    deadline = time.monotonic() + GEMINI_FILE_TIMEOUT
    current = dict(file_info)
    while time.monotonic() < deadline:
        state = str(current.get("state") or "").upper()
        if state == "ACTIVE":
            return current
        if state == "FAILED":
            raise GeminiError("Gemini не смог обработать загруженное видео")
        response = requests.get(f"{API_BASE}/{name}", headers=_headers(), timeout=(15, 30))
        _raise_for_response(response, "Gemini: не удалось получить статус файла")
        current = response.json()
        time.sleep(3)
    raise GeminiError(f"Gemini обрабатывал файл дольше {GEMINI_FILE_TIMEOUT} секунд")


def _delete_file(name):
    if not name:
        return
    try:
        requests.delete(f"{API_BASE}/{name}", headers=_headers(), timeout=(10, 20))
    except Exception:
        pass


def _extract_output_text(payload):
    candidates = []
    for item in payload.get("outputs", []) if isinstance(payload, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            candidates.append(item["text"])
    for step in payload.get("steps", []) if isinstance(payload, dict) else []:
        if not isinstance(step, dict):
            continue
        for content in step.get("content", []) or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                candidates.append(content["text"])
    if not candidates:
        def walk(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "text" and isinstance(child, str):
                        candidates.append(child)
                    else:
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(payload)
    for text in candidates:
        text = (text or "").strip()
        if text.startswith("{") and text.endswith("}"):
            return text
    if candidates:
        return max(candidates, key=len).strip()
    raise GeminiError("Gemini не вернул текстовый результат")


def _clean_list(value, limit):
    out = []
    if not isinstance(value, list):
        return out
    seen = set()
    for item in value:
        text = re.sub(r"\s+", " ", str(item or "")).strip(" •-\t")
        key = text.lower()
        if len(text) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _validate_analysis(data, model):
    if not isinstance(data, dict):
        raise GeminiError("Gemini вернул не JSON-объект")
    summary = re.sub(r"\s+", " ", str(data.get("summary") or "")).strip()
    transcript = re.sub(r"[ \t]+", " ", str(data.get("transcript") or "")).strip()
    if len(summary) < 40:
        raise GeminiError("Gemini вернул слишком короткую выжимку")
    if len(transcript) < 10:
        transcript = "Разборчивой речи в ролике не обнаружено."
    chapters = []
    for item in data.get("chapters", []) if isinstance(data.get("chapters"), list) else []:
        if not isinstance(item, dict):
            continue
        t = re.sub(r"[^0-9:]", "", str(item.get("time") or ""))[:8]
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
        if title:
            chapters.append({"time": t or "00:00", "title": title})
        if len(chapters) >= 20:
            break
    tags = []
    for raw in data.get("tags", []) if isinstance(data.get("tags"), list) else []:
        tag = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]", "", str(raw).lstrip("#").lower())
        if len(tag) >= 2 and tag not in tags:
            tags.append(tag)
        if len(tags) >= 8:
            break
    result = {
        "engine": f"Gemini ({model})",
        "language": re.sub(r"\s+", " ", str(data.get("language") or "")).strip() or "не определён",
        "summary": summary,
        "main_points": _clean_list(data.get("main_points"), 12),
        "key_facts": _clean_list(data.get("key_facts"), 12),
        "actions": _clean_list(data.get("actions"), 10),
        "visual_context": _clean_list(data.get("visual_context"), 8),
        "claims_to_verify": _clean_list(data.get("claims_to_verify"), 10),
        "chapters": chapters,
        "tags": tags,
        "transcript": transcript,
    }
    if len(result["main_points"]) < 2:
        raise GeminiError("Gemini вернул недостаточно содержательных тезисов")
    return result


def _run_interaction(file_info, model, title, source_url, platform):
    uri = str(file_info.get("uri") or "").strip()
    mime = str(file_info.get("mimeType") or file_info.get("mime_type") or "video/mp4")
    if not uri:
        raise GeminiError("Gemini не вернул URI загруженного файла")
    context = f"\nКонтекст:\n- Платформа: {platform or 'неизвестно'}\n- Название: {title or 'не указано'}\n- Исходная ссылка: {source_url or 'не указана'}\n"
    payload = {
        "model": model,
        "input": [
            {"type": "video", "uri": uri, "mime_type": mime, "resolution": "low"},
            {"type": "text", "text": PROMPT + context},
        ],
        "response_format": {"type": "text", "mime_type": "application/json", "schema": ANALYSIS_SCHEMA},
    }
    raw = _extract_output_text(_request_interaction(payload, f"Gemini model={model}"))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        snippet = raw[:900].replace("\n", " ")
        raise GeminiError(f"Gemini вернул некорректный JSON: {snippet}") from exc
    return _validate_analysis(data, model)


def analyze_video(path, title="", source_url="", platform=""):
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY не настроен")
    if not GEMINI_MODELS:
        raise GeminiError("Не указана ни одна модель Gemini")
    file_name = ""
    try:
        file_info = _upload_file(Path(path))
        file_name = str(file_info.get("name") or "")
        file_info = _wait_until_active(file_info)
        errors = []
        for model in GEMINI_MODELS:
            try:
                result = _run_interaction(file_info, model, title, source_url, platform)
                print(f"GEMINI_ANALYSIS_OK model={model} transcript_chars={len(result['transcript'])}", flush=True)
                return result
            except Exception as exc:
                errors.append(f"{model}: {str(exc)[-900:]}")
                print(f"GEMINI_MODEL_FAIL model={model}: {str(exc)[-900:]}", flush=True)
        raise GeminiError(" | ".join(errors))
    finally:
        _delete_file(file_name)


def format_analysis(result):
    blocks = [f"🧠 КРАТКО\n{result.get('summary', '').strip()}"]
    points = result.get("main_points") or []
    if points:
        blocks.append("💡 ГЛАВНЫЕ МЫСЛИ\n" + "\n".join("• " + x for x in points))
    facts = result.get("key_facts") or []
    if facts:
        blocks.append("📌 ВАЖНЫЕ ФАКТЫ / ДЕТАЛИ\n" + "\n".join("• " + x for x in facts))
    actions = result.get("actions") or []
    if actions:
        blocks.append("🎯 ЧТО СТОИТ ЗАПОМНИТЬ / СДЕЛАТЬ\n" + "\n".join("• " + x for x in actions))
    visual = result.get("visual_context") or []
    if visual:
        blocks.append("🎬 ВАЖНОЕ ИЗ ВИЗУАЛА\n" + "\n".join("• " + x for x in visual))
    chapters = result.get("chapters") or []
    if chapters:
        blocks.append("⏱ КЛЮЧЕВЫЕ МОМЕНТЫ\n" + "\n".join(f"• {x.get('time', '00:00')} — {x.get('title', '')}" for x in chapters))
    verify = result.get("claims_to_verify") or []
    if verify:
        blocks.append("⚠️ ЧТО ТРЕБУЕТ ПРОВЕРКИ\n" + "\n".join("• " + x for x in verify))
    tags = result.get("tags") or []
    if tags:
        blocks.append("🏷 ТЕГИ\n" + " ".join("#" + t.lstrip("#") for t in tags))
    return "\n\n".join(blocks).strip()
