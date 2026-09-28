# Gremlins

**Gremlins is a local capability runtime for software-development automation.**

It runs on your machines and exposes bounded, structured capabilities such as repository search, source reading, Git history, failure triage, and optional local-model synthesis.

Gremlins does **not** require an AI coding agent. A human, shell script, CI job, Python program, MCP client, coding agent, or future HTTP client can all call the same capabilities.

AI agents are clients of Gremlins, not dependencies of Gremlins.

> **Status:** pre-1.0. The deterministic core installs and runs independently. Local-model providers, MCP, and Claude/Codex registration are explicit optional setup steps.

## The model

```text
                  any caller
        human / shell / CI / program / agent
                       |
          +------------+------------+
          |            |            |
         CLI          MCP      future HTTP/API
          |            |            |
          +------------+------------+
                       |
                 Gremlins Core
                       |
       +---------------+---------------+
       |               |               |
   retrieval        workers         policy
       |               |               |
       +---------------+---------------+
                       |
              structured result
                       |
              optional local model
```

The caller decides what to do with the result. Gremlins does not need to know whether that caller is Claude Code, Codex, a human, CI, or another program.

## What Gremlins does

Current capabilities include:

- exact read-only repository search (`repo-search`);
- bounded source excerpts (`code-read`);
- bounded Git history (`git-history`);
- ranked multi-term repository exploration;
- failure/log triage;
- path and repository security checks;
- structured evidence/results;
- local job metrics;
- optional local-model synthesis.

The local model is **optional to the architecture**. Deterministic capabilities should work without inference whenever inference is unnecessary.

### Measured model-routing policy

The Apple-Silicon Mac reference measurements with Ollama + `qwen3.5:4b` establish the current routing boundary:

- repository search/read/history and normal `repo_explorer`: deterministic;
- `repo_explorer mode=model`: benchmark/manual experiment only, not normal routing;
- failure evidence extraction: deterministic first stage;
- bounded failure grouping, likely-cause synthesis, and next checks: local-model triage when available;
- ambiguous debugging, architecture, trade-offs, patch design, and subtle review remain caller/frontier-model work.

Repo-explorer local synthesis improved mean quality only from 0.95 to 0.9833 (+3.33 pp) while increasing median latency from 0.241 s to 9.523 s (~39.5x) and median result size by 33.9%, so normal repo exploration stays deterministic. The follow-up triage stability study covered 20 failure categories × 3 repeats (60 local-model calls): deterministic mean quality 0.6375, local-model mean quality 0.9167 (+27.92 pp), 60/60 successful model calls, 20/20 stable cases, 19/20 cases above the 0.75 quality floor, and 0 regressions. Median local-model triage latency was 5.255 s and p95 was 7.315 s. This freezes bounded local triage synthesis as part of the Mac reference profile; routing changes require new evidence.

## Interfaces

### CLI — first-class interface

Gremlins capabilities are directly callable from the terminal or another process:

```bash
uv run gremlins repo-explore \
  "Find references to RetryExhaustedError" \
  --repository /path/to/repo \
  --term RetryExhaustedError \
  --mode auto
```

Log triage can also be called directly:

```bash
cat failing.log | uv run gremlins triage
```

CLI output is structured JSON, so shell scripts, CI, and programs can consume it.

### MCP — optional adapter

Gremlins also exposes the same local capabilities over MCP for compatible clients.

Claude Code and Codex are currently supported integrations, but they are not the Gremlins runtime and are not required for direct CLI use.

### Future adapters

The same capability contracts should be exposable through additional adapters, for example:

- HTTP/JSON;
- Python library/API;
- Android bindings;
- other MCP clients.

Adapters must not redefine the underlying capability semantics.

## Core design rules

1. **Gremlins is caller-agnostic.**
2. **CLI, MCP, and future APIs are adapters over the same core capabilities.**
3. **Deterministic execution happens before optional model inference.**
4. **Local inference is optional and explicitly requested where useful.**
5. **No cloud model fallback occurs inside Gremlins.**
6. **Repository access is read-only by default.**
7. **No arbitrary shell execution is exposed as a Gremlins capability.**
8. **Every operation has bounded inputs, evidence, output, time, and permissions.**
9. **Missing evidence is a valid structured result.**
10. **Client-specific integrations never become the source of truth for the core runtime.**

## Quick start

### Core development setup

Requirements for the current codebase:

- macOS or Linux;
- Python 3.11+;
- Git;
- `uv`.

```bash
git clone https://github.com/feanor08/gremlins.git
cd gremlins
uv sync --locked
```

Run deterministic validation:

```bash
uv run pytest -q
uv run gremlins eval
uv run gremlins benchmark capabilities --repository .
uv run gremlins benchmark pilot-local --repository .
```

`benchmark capabilities` is the caller-independent product gate. It checks exact search, bounded reads, Git history, deterministic repo exploration, log-evidence extraction, path/security policy, stable result shapes, bounded result size, and zero local-model calls. It also reports latency, CPU time, and process peak RSS for trend tracking without making those machine-sensitive measurements hard pass/fail thresholds in v1.

Run capabilities directly:

```bash
uv run gremlins repo-search RetryExhaustedError --repository .
uv run gremlins code-read src/gremlins/workers.py --repository . --start-line 1 --line-count 80
uv run gremlins git-history --repository . --path src/gremlins/workers.py

uv run gremlins repo-explore \
  "Find the provider configuration" \
  --repository . \
  --term ProviderConfig \
  --mode auto
```

### Mac reference setup

For the first real local-model deployment, Gremlins uses an Apple-Silicon Mac as the reference environment:

```bash
./scripts/setup_mac_local_model.sh
```

The script keeps the model path explicit and optional: it installs Ollama through Homebrew when needed, starts the local service, prepares the caller-independent Gremlins core, pulls the configured `qwen3.5:4b` model, and verifies provider health. It does **not** make the local model a core dependency.

After setup:

```bash
uv run gremlins benchmark model-value --repository . --worker all
```

This Mac result is the reference measurement before reproducing the same Gremlins contracts/configuration shape on THG nodes.

### Core installer and optional integrations

`./install.sh` installs the Python environment and bootstraps the deterministic core only. It does not install or require Ollama, Claude Code, or Codex.

Optional setup is explicit:

```bash
# Core only
./install.sh
uv run gremlins doctor

# Optional local-model provider
uv run gremlins provider setup ollama

# Optional MCP adapter
uv run gremlins adapter install mcp

# Optional client registration
uv run gremlins adapter configure claude
uv run gremlins adapter configure codex
```

Direct deterministic CLI commands remain usable without any of those optional integrations.

## Configuration

Canonical defaults live in `gremlins.toml`. Machine-specific additions live in `profiles/`.

By default, Gremlins confines repository access to approved roots and denies common credential locations such as SSH, AWS, GPG, cloud credentials, and macOS Keychains.

Extra roots are explicit deployment/profile configuration.

## Security model

The security boundary is enforced by the runtime and capability contracts, not by caller prompts.

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

## Architecture

The canonical architecture is [architecture.md](architecture.md).

The central abstraction is:

```text
caller
  -> adapter
  -> capability contract
  -> Gremlins runtime
  -> deterministic implementation / optional local worker
  -> structured result
```

See [docs/INTERFACES.md](docs/INTERFACES.md) for the caller/interface boundary.

## Measurement

Gremlins should be useful independently of any particular AI agent.

Agent-based frontier benchmarks are therefore **integration experiments**, not runtime requirements. They answer questions such as:

> When a coding agent uses Gremlins, does it perform the same task with less expensive frontier work?

The existing B/C benchmark harness supports Claude/Codex studies for that purpose.

The product itself should also be measured at the capability layer:

- correctness;
- recall/coverage;
- result size;
- latency;
- local resource use;
- deterministic-vs-model value;
- client-independent contract stability.

See [docs/MEASUREMENT.md](docs/MEASUREMENT.md).

## Optional AI integrations

Current integrations can register Gremlins with Claude Code and Codex through MCP.

Those integrations are convenience adapters only:

```text
Claude Code ----\
Codex -----------+--> MCP adapter --> Gremlins Core
other MCP client/
```

Gremlins must remain usable when none of them is installed.

## Roadmap

Near-term architectural work:

- decouple core installation from Ollama and client registration;
- make direct capability execution the primary product surface;
- formalize stable input/output contracts;
- make local-model providers optional plugins/adapters;
- separate client integrations from core runtime packages;
- remove the legacy `needs-frontier` compatibility alias after the pre-1.0 migration window;
- add stronger Linux packaging;
- add HTTP/library adapters only when there is a concrete use case;
- continue measurement at both capability and client-integration layers.

Longer-term work may include multi-node capability routing, Android providers, richer worker registries, and separately secured write-capable operations.

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

For most core changes:

```bash
uv sync --locked
uv run pytest -q
uv run gremlins eval
uv run gremlins benchmark pilot-local --repository .
```

Please preserve caller independence, bounded execution, read-only defaults, and explicit separation between core runtime and optional integrations.

## License

MIT. See [LICENSE](LICENSE).
