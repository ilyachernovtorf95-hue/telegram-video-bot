"""Direct YouTube analysis through Gemini.

This bypasses yt-dlp for the knowledge path. Public YouTube URLs are sent
straight to Gemini at low video resolution so long podcasts/interviews can fit
comfortably in the model context while preserving audio understanding.
"""

from __future__ import annotations

import json

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
        "- Обязательно охвати весь ролик, даже если он длится несколько часов.\n"
    )

    errors = []
    for model in gemini_ai.GEMINI_MODELS:
        payload = {
            "model": model,
            "input": [
                {"type": "video", "uri": url, "resolution": "low"},
                {"type": "text", "text": gemini_ai.PROMPT + context},
            ],
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": gemini_ai.ANALYSIS_SCHEMA,
            },
        }

        try:
            raw = gemini_ai._extract_output_text(
                gemini_ai._request_interaction(payload, f"Gemini YouTube model={model}")
            )
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
