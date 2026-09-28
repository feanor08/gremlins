# Contributing to Gremlins

Thanks for helping improve Gremlins.

Gremlins is intentionally conservative: local-first, bounded, read-only by default, and measured against a tuned frontier-only baseline. Contributions should preserve those properties unless a proposal explicitly changes the architecture and includes a concrete security and measurement plan.

## Development setup

Current development support is strongest on macOS.

```bash
uv sync --locked
uv run pytest -q
uv run gremlins mcp-smoke
uv run gremlins eval
uv run gremlins benchmark pilot-local --repository .
```

If you change dependency metadata, regenerate and commit `uv.lock`.

## Pull requests

Keep changes focused. In the PR description, include:

- what problem the change solves;
- why the change belongs in Gremlins rather than the frontier orchestrator;
- security implications;
- evidence/result-size implications;
- tests added or updated;
- benchmark impact when the change affects retrieval, delegation, prompting, or local inference.

Do not weaken a measurement threshold simply because a change fails it. Diagnose the failure first.

## Design constraints

Please preserve these defaults:

- frontier model remains the orchestrator;
- deterministic retrieval before inference;
- no arbitrary shell exposed to the model;
- no recursive worker delegation;
- no cloud fallback inside Gremlins;
- read-only repository access unless a future write-capable subsystem introduces an explicit permission/sandbox boundary;
- bounded internal evidence and bounded frontier-facing results.

## Tests and benchmark fixtures

Benchmark expected paths, hidden search hints, and expected claims are scoring data. They must not leak into searchable benchmark workspaces or unrelated regression fixtures.

Prefer generic test fixtures over text copied from a benchmark answer.

## Reporting bugs

Open an issue with a minimal reproduction, platform details, Gremlins version/commit, and relevant non-sensitive logs.

For security issues, follow [SECURITY.md](SECURITY.md).
