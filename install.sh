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

if [ -f uv.lock ]; then
  uv sync --locked
else
  uv sync
fi

exec "$ROOT/.venv/bin/gremlins" setup --profile "${GREMLINS_PROFILE:-mac-local}"
