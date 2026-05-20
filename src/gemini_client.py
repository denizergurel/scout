"""
Gemini provider.

Calls Google Gemini via the google-generativeai SDK. Requires GEMINI_API_KEY.
Get a key at https://aistudio.google.com/apikey.

Model defaults to gemini-1.5-pro; override with GEMINI_MODEL.
"""

from __future__ import annotations

import os
import time


_GEMINI_DEFAULT_MODEL = "gemini-1.5-pro"


def call_gemini(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 4096,
    *,
    model: str | None = None,
) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get a key at https://aistudio.google.com/apikey"
        )

    try:
        import google.generativeai as genai
    except ImportError as e:
        raise RuntimeError(
            "google-generativeai not installed. Run: pip install google-generativeai"
        ) from e

    genai.configure(api_key=api_key)
    model_name = model or os.environ.get("GEMINI_MODEL") or _GEMINI_DEFAULT_MODEL
    model = genai.GenerativeModel(model_name, system_instruction=system_prompt)

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = model.generate_content(
                user_message,
                generation_config={"max_output_tokens": max_tokens},
            )
            return (response.text or "").strip()
        except Exception as e:
            last_error = e
            if attempt == 0:
                time.sleep(2)

    assert last_error is not None
    raise RuntimeError(f"Gemini call failed: {last_error}") from last_error
