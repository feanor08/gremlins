#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Installing it with the official installer..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv installation did not produce a usable uv binary." >&2
  exit 1
fi

if [ "$(uname -s)" = "Darwin" ] && ! command -v ollama >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing Ollama with Homebrew..."
    brew install ollama
  else
    echo "Ollama is missing and Homebrew is unavailable." >&2
    echo "Install Ollama, start it, then rerun ./install.sh" >&2
    exit 1
  fi
fi

if [ -f uv.lock ]; then
  uv sync --locked
else
  uv sync
fi

if command -v ollama >/dev/null 2>&1; then
  if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    if [ "$(uname -s)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
      brew services start ollama >/dev/null 2>&1 || true
      i=0
      while [ "$i" -lt 15 ]; do
        if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then break; fi
        sleep 1
        i=$((i + 1))
      done
    fi
  fi
fi

exec "$ROOT/.venv/bin/agentctl" deploy --profile mac-local --pull-model
