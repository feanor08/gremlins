#!/bin/sh
set -eu
claude mcp remove gremlins --scope user >/dev/null 2>&1 || true
codex mcp remove gremlins >/dev/null 2>&1 || true
rm -f "$HOME/.local/bin/gremlins-mcp"
rm -rf "$HOME/.claude/skills/gremlins-delegation" "$HOME/.codex/skills/gremlins-delegation"
echo "Gremlins client registrations and wrapper removed. Repository and Ollama model were left intact."
