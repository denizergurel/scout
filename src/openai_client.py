"""
OpenAI (and OpenAI-compatible) provider.

Works with:
  - OpenAI itself (default — needs OPENAI_API_KEY)
  - Any OpenAI-compatible endpoint via `api_base` override:
      Ollama         — http://localhost:11434/v1
      LM Studio      — http://localhost:1234/v1
      Groq           — https://api.groq.com/openai/v1
      Together       — https://api.together.xyz/v1
      DeepSeek       — https://api.deepseek.com/v1
      vLLM / any OpenAI-compatible server

Pass `model`, `api_base`, and `api_key` from config.yaml's `llm:` block.
Falls back to env vars (OPENAI_MODEL, OPENAI_API_KEY) when args are omitted.
"""

from __future__ import annotations

import os
import time


_OPENAI_DEFAULT_MODEL = "gpt-4o"


def call_openai(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 4096,
    *,
    model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
) -> str:
    # api_key: explicit > env. Some local servers (Ollama) don't validate the
    # key, but the OpenAI SDK still requires a non-empty string.
    resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not resolved_key:
        # Allow local endpoints (Ollama, LM Studio) without an explicit key.
        if api_base and ("localhost" in api_base or "127.0.0.1" in api_base):
            resolved_key = "local"
        else:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Either set the env var, "
                "or put `api_key` / `api_key_env` in your config.yaml llm block."
            )

    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("openai SDK not installed. Run: pip install openai") from e

    client_kwargs: dict = {"api_key": resolved_key}
    if api_base:
        client_kwargs["base_url"] = api_base
    client = OpenAI(**client_kwargs)

    model_name = model or os.environ.get("OPENAI_MODEL") or _OPENAI_DEFAULT_MODEL

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            # Bound the per-request wait. The SDK default is many minutes
            # plus its own retries — long enough that a hung endpoint can
            # wedge the whole Scout/Editor stage. 120s comfortably covers
            # normal long-completion latency while keeping failures bounded.
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=max_tokens,
                timeout=120,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            last_error = e
            if attempt == 0:
                time.sleep(2)

    assert last_error is not None
    raise RuntimeError(f"OpenAI-compatible call failed: {last_error}") from last_error
