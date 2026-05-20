#!/bin/bash
# Scout — Install
# Creates the virtual environment and installs dependencies.
# Run this once after cloning. Scheduling daily refresh happens later from
# the dashboard's Settings page.

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "Scout — Install"
echo "==============="
echo "Project: $PROJECT_DIR"
echo

# Python 3 check
if ! command -v python3 &> /dev/null; then
    echo "✗ python3 not found. Install Python 3.9 or later."
    exit 1
fi
PY_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "  ✓ Python $PY_VERSION"

# Create venv
if [ ! -f ".venv/bin/python" ]; then
    echo "  Creating .venv …"
    python3 -m venv .venv
    echo "  ✓ Virtual environment created"
else
    echo "  ✓ Virtual environment exists"
fi

# Install deps
echo "  Installing dependencies …"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "  ✓ Dependencies installed"

# LLM provider hint based on config
PROVIDER=$(.venv/bin/python -c "import yaml; cfg=yaml.safe_load(open('config.yaml')); print((cfg.get('llm', {}).get('default', {}) or {}).get('provider') or 'claude')" 2>/dev/null || echo "claude")

case "$PROVIDER" in
  claude)
    if command -v claude &> /dev/null; then
        echo "  ✓ Claude CLI: $(claude --version 2>&1 | head -1)"
    else
        echo "  ⚠ Claude CLI not found (your config uses provider=claude)."
        echo "    Install:  npm install -g @anthropic-ai/claude-code"
        echo "    Sign in:  claude login"
    fi
    ;;
  openai)
    echo "  ℹ LLM provider: openai. Set OPENAI_API_KEY in .env (or use api_base for a compatible endpoint)."
    ;;
  gemini)
    echo "  ℹ LLM provider: gemini. Set GEMINI_API_KEY in .env."
    ;;
  *)
    echo "  ℹ LLM provider: $PROVIDER"
    ;;
esac

echo
echo "Done. Next steps:"
echo
echo "  1. (Optional) Edit config.yaml — newsletter.name, slug, topics, sources"
echo "  2. Start the dashboard:"
echo "     .venv/bin/python -m uvicorn src.dashboard:app --reload"
echo "  3. Open http://localhost:8000"
echo "     - Click ✦ Refresh on Signals to pull a first batch"
echo "     - Enable daily automation in Settings (optional)"
echo
echo "Docs:"
echo "  README.md          — quick orientation"
echo "  SETUP.md           — full setup guide"
echo "  NEW_NEWSLETTER.md  — point Scout at a different beat"
