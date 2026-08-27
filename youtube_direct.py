"""Direct YouTube analysis through Gemini.

This bypasses yt-dlp entirely for the knowledge-extraction path. Gemini's
Interactions API can consume a public YouTube URL directly, so Railway's
YouTube download/IP restrictions do not prevent summaries, chapters, visual
analysis, or transcription.
"""

from __future__ import annotations

import json

import requests

import gemini_ai


def analyze_youtube_url(url: str, title: str = "", platform: str = "YouTube"):
    if not gemini_ai.GEMINI_API_KEY:
        raise gemini_ai.GeminiError("GEMINI_API_KEY не настроен")
    if not gemini_ai.GEMINI_MODELS:
        raise gemini_ai.GeminiError("Не указана ни одна модель Gemini")

    context = (
        "\nКонтекст:\n"
        f"- Платформа: {platform or 'YouTube'}\n"
        f"- Название: {title or 'не указано'}\n"
        f"- Исходная ссылка: {url}\n"
        "- Видео передано напрямую как публичный YouTube URL.\n"
    )

    errors = []
    for model in gemini_ai.GEMINI_MODELS:
        payload = {
            "model": model,
            "input": [
                {"type": "video", "uri": url},
                {"type": "text", "text": gemini_ai.PROMPT + context},
            ],
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": gemini_ai.ANALYSIS_SCHEMA,
            },
        }

        try:
            response = requests.post(
                f"{gemini_ai.API_BASE}/interactions",
                headers={**gemini_ai._headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=(30, gemini_ai.GEMINI_REQUEST_TIMEOUT),
            )
            gemini_ai._raise_for_response(response, f"Gemini YouTube model={model}")
            raw = gemini_ai._extract_output_text(response.json())
            data = json.loads(raw)
            result = gemini_ai._validate_analysis(data, model)
            result["engine"] = f"Gemini YouTube URL ({model})"
            print(
                f"GEMINI_YOUTUBE_URL_OK model={model} transcript_chars={len(result['transcript'])}",
                flush=True,
            )
            return result
        except Exception as exc:
            errors.append(f"{model}: {str(exc)[-900:]}")
            print(f"GEMINI_YOUTUBE_URL_FAIL model={model}: {str(exc)[-900:]}", flush=True)

    raise gemini_ai.GeminiError(" | ".join(errors))
