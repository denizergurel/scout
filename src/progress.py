"""
Lightweight progress reporter for the pipeline.

Each stage (collector, scout, editor) calls ``update(...)`` at meaningful
checkpoints — per feed, per batch — and the dashboard polls
``output/daily/refresh_status.json`` to render a live progress pill.

Designed to be a no-op when there's no active refresh:
- If the status file doesn't exist, writes are skipped.
- If the file's state is not ``"running"``, writes are skipped — so a
  standalone ``python src/scout.py`` invocation can't accidentally
  overwrite a ``"done"`` or ``"error"`` state.

Writes are also wrapped defensively: a transient OSError or partial JSON
must never break the pipeline. Progress is UX, not a correctness signal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

STATUS_FILE = (
    Path(__file__).resolve().parent.parent / "output" / "daily" / "refresh_status.json"
)


def _read() -> dict:
    if not STATUS_FILE.exists():
        return {}
    try:
        with open(STATUS_FILE, "r") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write(payload: dict) -> None:
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATUS_FILE, "w") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass


def update(
    stage: str,
    *,
    stage_label: str | None = None,
    current: int | None = None,
    total: int | None = None,
    last_done: str | None = None,
    message: str | None = None,
) -> None:
    """Patch the running refresh status with the given fields.

    Only writes when an active refresh is in flight (state == "running").
    Otherwise this is a no-op, so a standalone stage run won't clobber
    the dashboard's terminal state.
    """
    status = _read()
    if status.get("state") != "running":
        return

    status["stage"] = stage
    if stage_label is not None:
        status["stage_label"] = stage_label
    if current is not None:
        status["current"] = current
    if total is not None:
        status["total"] = total
    if last_done is not None:
        status["last_done"] = last_done
    if message is not None:
        status["message"] = message
    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write(status)
