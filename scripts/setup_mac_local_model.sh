#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

MODEL="qwen3.5:4b"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This bootstrap is for macOS only." >&2
  exit 2
fi

if [ "$(uname -m)" != "arm64" ]; then
  echo "This reference bootstrap expects Apple Silicon (arm64)." >&2
  exit 2
fi

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required for the Mac reference setup." >&2
  echo "Install Homebrew first, then rerun this script." >&2
  exit 2
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "Installing Ollama with Homebrew..."
  brew install ollama
fi

echo "Starting Ollama as a Homebrew service..."
brew services start ollama >/dev/null

echo "Waiting for Ollama API..."
i=0
while [ "$i" -lt 30 ]; do
  if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 1
done

if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "Ollama did not become ready on http://127.0.0.1:11434." >&2
  exit 2
fi

echo "Preparing Gremlins core..."
./install.sh

echo "Pulling configured reference model: $MODEL"
ollama pull "$MODEL"

echo "Verifying Gremlins optional provider..."
uv run gremlins provider setup ollama --profile mac-local --no-pull

echo "Running Gremlins doctor..."
uv run gremlins doctor

cat <<'EOF'

Mac reference setup is ready.

Next run:
  uv run gremlins benchmark model-value --repository . --worker all

The benchmark is intentionally manual because it performs real local inference.
EOF
