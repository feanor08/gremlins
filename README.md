# Gremlins

**Gremlins is a local-first, read-only evidence layer for coding agents.**

It gives frontier coding assistants such as Claude Code and Codex a small set of bounded MCP tools for repository search, source reading, Git history, and failure triage. The goal is simple: spend expensive frontier context on reasoning, not on mechanical evidence gathering.

Gremlins is open source under the MIT License and is intended to stand on its own. It does not depend on a private platform or hosted service.

> **Status:** pre-1.0 and actively measured. The current implementation is macOS-first, read-only, and intentionally conservative. Linux, broader packaging, additional clients, and write-capable workflows belong to later phases and should not be treated as shipped today.

## Why Gremlins?

Coding agents are good at reasoning, but they often burn a lot of context on deterministic work:

- locating exact symbols and strings;
- reading small source excerpts;
- checking bounded Git history;
- extracting the important parts of failing logs;
- repeatedly rediscovering the same repository facts.

Gremlins moves that work into a local, bounded layer. It can return deterministic evidence directly, or make one explicitly requested local-model synthesis call when that is genuinely useful.

The frontier model remains the orchestrator.

## What is implemented

Current MCP tools:

- `gremlins_status` — runtime/model health;
- `repo_search` — exact read-only repository search;
- `code_read` — bounded source excerpts;
- `git_history` — bounded read-only Git history;
- `repo_explorer` — ranked multi-term repository evidence;
- `failure_triage` — bounded local log/test triage.

Important behavior:

- deterministic retrieval comes before inference;
- `repo_explorer mode=auto` is deterministic and does **not** call a model;
- `mode=model` explicitly requests local synthesis;
- local inference defaults to Ollama with `qwen3.5:4b`;
- MCP transport is stdio;
- repositories are read-only;
- no arbitrary shell tool is exposed;
- workers cannot spawn child agents;
- no cloud fallback happens inside Gremlins;
- frontier-facing evidence is capped separately from the internal evidence budget;
- local job metrics are stored under `~/.local/state/gremlins/`.

## Quick start

### Requirements

For the current supported path:

- macOS;
- Python 3.11+;
- Git;
- `uv` (the installer can bootstrap it);
- Ollama;
- Claude Code and/or Codex if you want automatic MCP registration.

Clone the public repository, then:

```bash
git clone https://github.com/feanor08/gremlins.git
cd gremlins
./install.sh
```

The installer creates a local virtual environment, prepares Ollama when possible, pulls the configured model, installs the Gremlins delegation skill, creates `~/.local/bin/gremlins-mcp`, and registers that MCP server with supported local clients.

Verify:

```bash
.venv/bin/gremlins doctor
claude mcp list   # if Claude Code is installed
codex mcp list    # if Codex is installed
```

## Direct use

```bash
# Deterministic repository exploration
.venv/bin/agentctl repo-explore \
  "Find references to RetryExhaustedError" \
  --repository /path/to/repo \
  --term RetryExhaustedError \
  --mode auto

# Log triage
cat failing.log | .venv/bin/agentctl triage

# Smoke evaluations
.venv/bin/agentctl eval
```

## Configuration

Canonical defaults live in `gremlins.toml`. Machine-specific additions live in `profiles/`.

By default, Gremlins allows read-only access to Git repositories under the current user's home directory and denies common credential locations such as SSH, AWS, GPG, cloud credentials, and macOS Keychains. Extra repository roots are opt-in: add them to the local profile you use.

The local-model input budget and the smaller frontier-facing result budget are intentionally separate.

## Security model

The security boundary is enforced in code, not only by prompts.

Gremlins currently exposes:

- no repository writes;
- no arbitrary shell execution through MCP;
- no model-selected network destinations;
- no child-agent spawning;
- path confinement to approved repository roots;
- fixed, read-only Git operations;
- local/private provider allowlisting;
- no internal cloud fallback.

See [docs/SECURITY.md](docs/SECURITY.md) and [SECURITY.md](SECURITY.md).

## Design rules

1. The frontier model remains the orchestrator.
2. Retrieval is deterministic before inference.
3. Workers receive a task plus bounded evidence, not the parent conversation.
4. Workers cannot recursively delegate.
5. Read-only behavior is enforced by the runtime.
6. Missing evidence is a valid result.
7. Local-model failure never silently escalates to a cloud model.
8. A Gremlins call should replace frontier work, not merely add another hop.

The detailed design and implementation plan lives in [architecture.md](architecture.md).

## Measurement first

Gremlins is deliberately gated on evidence that it actually reduces frontier work.

The repository includes a controlled B/C benchmark:

- **B** — tuned frontier-only workflow;
- **C** — Gremlins-assisted workflow.

The default gate requires, among other things:

- at least 10 paired B/C runs;
- at least 10 unique cases;
- at least 30% reduction in frontier processed tokens;
- no more than a 5 percentage-point acceptance drop;
- bounded redo/re-verification;
- no more than a 20% elapsed-time increase.

Run the deterministic corpus first:

```bash
.venv/bin/agentctl benchmark pilot-local --repository .
```

Then run a controlled frontier study:

```bash
.venv/bin/agentctl benchmark suite \
  --study pilot-claude \
  --repository . \
  --client claude \
  --repeats 2
```

Inspect it:

```bash
.venv/bin/agentctl benchmark report --study pilot-claude
.venv/bin/agentctl benchmark gate --study pilot-claude
```

See [docs/MEASUREMENT.md](docs/MEASUREMENT.md) for the protocol and fairness rules.

## Roadmap

The architecture is intentionally broader than the shipped implementation. Likely future work includes:

- stronger cross-platform installation;
- Linux/home-server profiles;
- a portable worker/skill registry;
- more local providers and model classes;
- richer observability and measurement;
- optional write-capable workers behind a separate permission and sandbox model;
- additional client integrations.

Those are roadmap items, not promises or current capabilities.

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

For most changes:

```bash
uv sync --locked
uv run pytest -q
uv run gremlins mcp-smoke
uv run gremlins eval
uv run gremlins benchmark pilot-local --repository .
```

Please preserve the project's core constraints: bounded evidence, read-only defaults, no hidden cloud escalation, and measurement before architectural expansion.

## License

MIT. See [LICENSE](LICENSE).
