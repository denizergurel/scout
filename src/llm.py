"""
LLM provider router.

Resolves per-stage LLM settings from config.yaml's `llm:` block, with the
LLM_PROVIDER env var as a back-compat fallback. Supported providers:

  claude   — local `claude` CLI (Claude Pro/Max subscription, no API key)
  openai   — OpenAI, OR any OpenAI-compatible endpoint via `api_base`
             (Ollama, LM Studio, vLLM, Together, Groq, DeepSeek, etc.)
  gemini   — Google Gemini via `GEMINI_API_KEY`

Public API:
  call_llm(system, user, max_tokens=4096, stage=None)
  call_llm_json(system, user, stage=None)
  resolve_llm_settings(stage=None) — returns the dict the router used

Per-stage routing:
  scout.py / editor.py call with stage="scout" / stage="editor".
  The Curator's draft-spotlight call uses stage="curator".
  Unknown stages fall back to llm.default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def resolve_llm_settings(stage: str | None = None) -> dict:
    """Resolve effective LLM settings for a given stage.

    Order of precedence:
      1. config.yaml -> llm.{stage}      (per-stage override)
      2. config.yaml -> llm.default      (default block)
      3. env var LLM_PROVIDER            (back-compat for older installs)
      4. provider='claude'               (hardcoded fallback)
    """
    cfg = _load_config()
    llm = cfg.get("llm") or {}
    default = llm.get("default") or {}
    stage_block = (llm.get(stage) or {}) if stage else {}

    # Merge: stage overrides default. Empty-string values count as "use default".
    merged = {**default, **{k: v for k, v in stage_block.items() if v not in (None, "")}}

    # If config is silent on provider, use the env var (or claude).
    if not merged.get("provider"):
        merged["provider"] = os.environ.get("LLM_PROVIDER", "claude").lower()
    else:
        merged["provider"] = str(merged["provider"]).lower()

    # Resolve api_key from `api_key_env` if set, else `api_key`, else provider-default env.
    if not merged.get("api_key"):
        env_name = merged.get("api_key_env")
        if env_name:
            merged["api_key"] = os.environ.get(env_name)

    return merged


def current_provider(stage: str | None = None) -> str:
    return resolve_llm_settings(stage).get("provider", "claude")


def call_llm(system_prompt: str, user_message: str, max_tokens: int = 4096, stage: str | None = None) -> str:
    """Send a system + user prompt and return the model's text response."""
    settings = resolve_llm_settings(stage)
    provider = settings["provider"]

    if provider == "claude":
        from claude_cli import call_claude
        return call_claude(system_prompt, user_message, max_tokens)

    if provider == "gemini":
        from gemini_client import call_gemini
        return call_gemini(system_prompt, user_message, max_tokens, model=settings.get("model") or None)

    if provider == "openai":
        from openai_client import call_openai
        return call_openai(
            system_prompt,
            user_message,
            max_tokens,
            model=settings.get("model") or None,
            api_base=settings.get("api_base") or None,
            api_key=settings.get("api_key") or None,
        )

    raise ValueError(
        f"Unknown LLM provider {provider!r} (stage={stage!r}). "
        f"Set llm.default.provider in config.yaml to one of: claude, openai, gemini."
    )


def call_llm_json(system_prompt: str, user_message: str, stage: str | None = None) -> dict | list:
    """Call the LLM and parse its response as JSON (handles markdown fences)."""
    return extract_json_from_text(call_llm(system_prompt, user_message, stage=stage))


def extract_json_from_text(text: str) -> dict | list:
    text = text.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()

    start_array = text.find("[")
    start_obj = text.find("{")

    if start_array == -1 and start_obj == -1:
        raise ValueError(f"No JSON found in response: {text[:200]}")

    if start_array != -1 and (start_obj == -1 or start_array < start_obj):
        end = text.rfind("]") + 1
        json_str = text[start_array:end]
    else:
        end = text.rfind("}") + 1
        json_str = text[start_obj:end]

    return json.loads(json_str)
