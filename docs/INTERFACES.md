# Gremlins interfaces

Gremlins core owns capability semantics. Interfaces only expose them.

## Caller model

A caller can be:

- a human;
- a shell script;
- CI;
- another local program;
- an MCP client;
- a coding agent;
- a future HTTP/API client.

The runtime must not assume that the caller is an AI model.

## CLI

CLI/direct process invocation is a first-class interface.

Properties:

- works without Claude, Codex, or another agent;
- accepts explicit parameters;
- returns structured JSON;
- can be composed from shell, CI, and programs;
- uses the same capability implementations as other adapters.

Examples:

```bash
uv run gremlins repo-search RetryExhaustedError --repository /path/to/repo
uv run gremlins code-read src/example.py --repository /path/to/repo --start-line 1 --line-count 80
uv run gremlins git-history --repository /path/to/repo --path src/example.py

uv run gremlins repo-explore \
  "Find retry handling" \
  --repository /path/to/repo \
  --term RetryExhaustedError \
  --mode auto
```

## MCP

MCP is an optional interoperability adapter.

It maps MCP tool calls onto the same Gremlins capability contracts. Claude Code and Codex are currently supported clients, but they are not dependencies of Gremlins.

## Future adapters

Potential adapters include HTTP/JSON, a Python library API, and Android bindings.

A new adapter must not:

- change capability meaning;
- bypass policy/security;
- introduce hidden cloud fallback;
- require an AI model when the underlying capability is deterministic.

## Local models

Local inference is a provider behind capabilities that need it.

It is not an interface and not a mandatory runtime dependency.

The desired packaging split is:

```text
gremlins core
  + optional local-model provider
  + optional MCP adapter
  + optional client registration
```

The core installer now follows this split: provider setup, MCP installation, and client registration are explicit optional actions.
