"""
Shared logger for the daily pipeline (collector → scout → editor → daily).

Writes a timestamped line to stdout AND appends to output/daily/agent.log,
so per-batch warnings from scout.py / editor.py end up in the same file
daily.py logs to — even when daily.py is launched by launchd and stdout
is discarded. Before this, batch errors printed to stdout were silently
swallowed, hiding the catastrophic "every batch failed" mode behind a
clean-looking agent.log.

Safe to call standalone: if the log file's directory can't be written,
the function still prints to stdout and returns.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent.parent / "output" / "daily" / "agent.log"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
