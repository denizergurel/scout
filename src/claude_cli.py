"""
Claude CLI provider.

Calls Claude via the `claude` CLI (npm: @anthropic-ai/claude-code), authed
against your Claude Pro/Max subscription — no API key needed; billing goes
against the subscription.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path


def _find_claude_bin() -> str:
    """Locate the Claude CLI: env override, then PATH, then known Homebrew prefixes."""
    override = os.environ.get("CLAUDE_BIN")
    if override and Path(override).exists():
        return override

    found = shutil.which("claude")
    if found:
        return found

    for candidate in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if Path(candidate).exists():
            return candidate

    return "claude"


CLAUDE_BIN = _find_claude_bin()


def call_claude(system_prompt: str, user_message: str, max_tokens: int = 4096) -> str:
    """Call Claude via the CLI with a system prompt and user message.

    Uses `claude -p` (print mode), which takes a prompt from stdin and returns
    the response to stdout. Retries once on transient failures.
    """
    full_prompt = f"""<system>
{system_prompt}
</system>

{user_message}"""

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            result = subprocess.run(
                [CLAUDE_BIN, "-p", full_prompt, "--output-format", "text"],
                capture_output=True,
                text=True,
                timeout=180,
            )

            if result.returncode != 0:
                raise RuntimeError(
                    f"Claude CLI error (exit {result.returncode}): {result.stderr.strip()}"
                )

            return result.stdout.strip()

        except subprocess.TimeoutExpired:
            last_error = RuntimeError("Claude CLI timed out after 180 seconds")
        except FileNotFoundError:
            raise RuntimeError(
                f"Claude CLI not found at {CLAUDE_BIN}. "
                "Install via: npm install -g @anthropic-ai/claude-code"
            )
        except RuntimeError as e:
            last_error = e

        if attempt == 0:
            time.sleep(2)

    assert last_error is not None
    raise last_error
