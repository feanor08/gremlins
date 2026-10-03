# Gremlins — Agent-Independent Local Capability Runtime

Gremlins is a local-first capability runtime for software-development automation.

Its job is simple:

> Expose bounded, secure, reusable operations on the machines where the work and evidence live, through stable contracts that any caller can invoke.

A caller may be a human, shell script, CI job, Python program, MCP client, coding agent, or future HTTP client. Gremlins does not require the caller to be an AI system.

The system is designed to start on one machine, then expand to other Macs, Linux machines and home servers, future GPU nodes, and Android devices without changing the meaning of its capabilities.

This document is the canonical architecture and implementation plan for the project.

---

# 1. Why Gremlins exists

The original motivation included reducing expensive coding-agent context, but the deeper product is broader: **local capabilities should be reusable independently of whoever calls them**.

Repository search, bounded file reads, Git history, log extraction, test parsing, device inspection, and similar work should not need to be reimplemented inside every agent, CI workflow, or automation script.

Gremlins therefore optimizes for:

- caller-independent capability contracts;
- deterministic execution before optional model reasoning;
- bounded local inference when inference adds value;
- small structured input/result contracts;
- direct CLI/process use;
- portable skills and workers;
- replaceable models and tool implementations;
- reproducible deployment;
- explicit security boundaries;
- no uncontrolled recursive delegation;
- easy migration from one Mac to Linux/home-server nodes and Android later;
- measurable correctness, latency, resource use, and downstream-client benefit.

Gremlins is **not an AI agent framework**.

AI coding agents can use Gremlins, but so can humans and ordinary software.

---

# 2. Non-negotiable design rules

These rules define the project.

1. **Gremlins is caller-agnostic.**
2. **CLI, MCP, and future APIs are adapters over the same core capabilities.**
3. **Deterministic execution happens before optional model inference.**
4. **Local model inference is optional and explicit.**
5. **A worker receives a task plus selected evidence, never an entire unrelated parent conversation.**
6. **Workers do not recursively create child workers by default.**
7. **Skills contain workflow knowledge, not security enforcement.**
8. **Security is enforced by the runtime and capability boundary.**
9. **Every operation has explicit input, evidence, output, time, permission, and optional-model budgets.**
10. **Missing evidence is a valid structured result.**
11. **Gremlins never silently falls back to a cloud model.**
12. **Repository work is read-only until a separate write-capable security architecture is deliberately enabled.**
13. **Model implementations are replaceable behind model capabilities.**
14. **Tool implementations are replaceable behind stable capabilities.**
15. **Client-specific configuration is generated adapter output, never canonical source of truth.**
16. **Deployment state is reproducible and auditable.**
17. **Optimizations must be measured at the capability layer; client-specific savings are integration measurements, not product identity.**

---

# 3. Final system architecture

```text
                        CALLERS
      human / shell / CI / program / coding agent
                           |
             +-------------+-------------+
             |             |             |
            CLI           MCP       future HTTP/API
             |             |             |
             +-------------+-------------+
                           |
                           v
                 +---------------------+
                 |   GREMLINS CORE     |
                 | capability runtime  |
                 +---------------------+
                           |
          +----------------+----------------+
          |                |                |
          v                v                v
     capability       policy/budget     job controller
      resolver           engine            + queue
          |                |                |
          +----------------+----------------+
                           |
                           v
                 DETERMINISTIC TOOLING
          search / reads / Git / parsers / devices
                           |
                       evidence
                           |
                +----------+----------+
                |                     |
          inference not needed   inference useful
                |                     |
                v                     v
        structured result        WORKER RUNTIME
                                      |
                               optional model class
                                      |
                               provider resolver
                                      |
                       local/private model provider
                                      |
                               structured result
                |                     |
                +----------+----------+
                           |
                    CONTRACT VALIDATION
                           |
                           v
                    structured result
                           |
                           v
                        CALLER
```

MCP is one adapter, not the architecture boundary. Claude Code and Codex are optional MCP clients. The CLI is equally valid, and future adapters must preserve the same capability semantics.

Android can participate as a device/tool capability provider, a caller, or an optional inference provider. It does not require a separate orchestration model.

---

# 4. The conceptual model

Gremlins uses the following concepts.

## 4.1 Capability

A capability is the stable behavior a consumer requests.

Examples:

```text
repo.search.literal
repo.retrieve.semantic
code.read
git.history
evidence.pack
log.extract.failures
test.parse
worker.repo-explorer
worker.triage
model.evidence-extraction
android.logcat
android.instrumentation
```

Consumers depend on capabilities, not concrete implementations.

Example:

```toml
[bindings]
"repo.search.literal" = "builtin-ripgrep"
"code.read" = "builtin-source-reader"
"git.history" = "builtin-git-history"
"evidence.pack" = "builtin-evidence-pack"
```

A future implementation can replace `builtin-ripgrep` without changing the workers that consume `repo.search.literal`.

Capabilities must represent **behavioral meaning**, not merely similar-looking APIs.

For example:

- literal search and semantic search are different capabilities;
- a ranked semantic retriever must never silently replace an exact-search capability.

### Evidence-pack boundary

`evidence.pack` composes deterministic retrieval capabilities into one bounded, provenance-preserving evidence bundle: ranked source excerpts, test/source relationships, and bounded Git/change history. It is retrieval orchestration, not a reasoning worker.

The measured frontier loop is intentionally iterative:

```text
caller hypothesis / question
        |
        v
evidence.pack
        |
        v
compact evidence
        |
        v
caller reasoning / next question
```

The caller owns causal conclusions, architecture, trade-offs, and patch design. Repeated evidence-pack calls during RCA are expected when each call answers a narrower evidence question.

The evidence loop has two explicit result profiles:

- **broad** — one cross-source discovery pass with the normal bounded result budget;
- **focused** — later path/term/symbol-constrained follow-ups with a substantially smaller result budget.

The caller should move from broad to focused rather than repeatedly paying for broad discovery. This is a context-control rule, not a reasoning rule.

---

## 4.2 Tool

A tool is executable functionality.

Examples:

- ripgrep;
- bounded source reader;
- Git-history reader;
- JUnit parser;
- log extractor;
- Android logcat collector;
- GitHub MCP;
- Forgejo MCP;
- ADB wrapper.

A tool declares:

- capability provided;
- input contract;
- output contract;
- side effects;
- platform support;
- permissions;
- time and output limits.

Tools do not contain broad orchestration logic.

---

## 4.3 Skill

A skill is reusable workflow knowledge.

Examples:

- repository search;
- Git-history investigation;
- failure triage;
- test analysis;
- Android debugging;
- Gradle debugging;
- CI investigation;
- code review.

Canonical skills use `SKILL.md`.

A skill explains **how** to approach a task.

It does not grant access.

A skill saying "read only" does not make a tool read only. The runtime enforces permissions separately.

Skills should not normally spawn workers.

---

## 4.4 Worker

A worker is a bounded execution policy.

A worker combines:

- a role;
- instructions;
- required capabilities;
- skills;
- model-class requirement;
- context budget;
- output budget;
- tool-call budget;
- time limit;
- permission profile;
- result contract.

Examples:

- repo explorer;
- triage;
- reviewer;
- test writer;
- CI investigator;
- Android specialist;
- Linux specialist;
- documentation worker;
- release verifier.

Workers are portable logical definitions.

Their model or execution node is resolved at deployment/runtime.

---

## 4.5 Model class

Workers do not request specific model names.

They request a class such as:

```text
evidence-extraction
local-coder
fast-classifier
review-reasoning
frontier-reasoning
long-context
```

A deployment profile maps that class to an actual provider/model.

Example:

```toml
[model_classes.evidence-extraction]
provider = "ollama-default"
placement = "local-only"
cloud_fallback = false
```

On one Mac this may resolve to Qwen through Ollama.

On another machine it may later resolve to a model running on a private Linux or GPU node.

The worker definition does not change.

---

## 4.6 Provider

A provider is the runtime/API that serves inference.

Examples:

- Ollama;
- MLX;
- llama.cpp;
- vLLM;
- remote private HTTP endpoint;
- Android native inference runtime.

Providers expose implementation details such as:

- endpoint;
- model name;
- context capability;
- structured-output support;
- hardware;
- concurrency;
- health;
- latency.

---

## 4.7 Node

A node is a machine/device capable of providing one or more capabilities.

Examples:

```text
macbook
linux-node
gpu-node
android-device
```

A node advertises:

- node ID;
- platform;
- architecture;
- available capability providers;
- model providers;
- execution slots;
- current health;
- policy labels;
- optional hardware metadata.

The worker does not care which node ultimately executes the model.

---

## 4.8 Interface adapter

An interface adapter exposes canonical Gremlins capabilities to a caller without redefining their meaning.

Current interfaces:

- CLI/direct process invocation;
- MCP over stdio.

Current optional MCP client integrations include Claude Code and Codex.

Future adapters may include:

- HTTP/JSON;
- a Python library API;
- Android bindings;
- other MCP clients.

Client-specific configuration is generated deployment output.

It is never the canonical source of truth, and Gremlins must remain usable when no AI client is installed.

---

## 4.9 Contract

Contracts define what goes into and comes out of tools and workers.

A useful result contract includes:

- status;
- repository/device snapshot identity;
- summary;
- findings;
- evidence references;
- limitations;
- truncation state;
- usage;
- next queries/checks where appropriate.

Possible job status values should be caller-neutral:

```text
complete
partial
needs-caller
busy
failed
cancelled
```

The current implementation still exposes the legacy name `needs-caller`. That is compatibility debt from the coding-agent-first prototype and should migrate to caller-neutral terminology without changing the underlying escalation semantics.

A structured schema alone is not enough.

Gremlins performs structural and referential validation, including checking that evidence IDs returned by a model actually exist. This does **not** prove that a cited item semantically supports a claim; claim-support quality is tested in evaluation and can still be checked by the frontier orchestrator when needed.

---

# 5. Control plane and data plane

Gremlins separates orchestration metadata from job execution.

## Control plane

The control plane owns:

- canonical configuration;
- capability bindings;
- provider definitions;
- node definitions;
- worker definitions;
- skills;
- permission profiles;
- deployment profiles;
- artifact/model identity;
- health state;
- activation and rollback;
- evaluation results.

Initially this is almost entirely files in the Git repository plus local generated state.

It should remain simple.

A database or dedicated distributed control service is unnecessary until deployment scale makes it useful.

## Data plane

The data plane handles actual jobs:

1. receive MCP call;
2. validate task;
3. resolve repository/device;
4. enforce policy;
5. perform deterministic retrieval;
6. build bounded evidence packet;
7. decide whether inference is useful;
8. resolve model class;
9. run one bounded worker/model interaction;
10. validate structured result;
11. validate evidence references;
12. record metrics;
13. return compact output.

---

# 6. Interfaces are adapters over one capability runtime

Gremlins capabilities must be directly callable without an AI client.

The CLI is a first-class interface for humans, shell scripts, CI, and programs. MCP is a first-class interoperability adapter for compatible clients. Future HTTP/library adapters may be added when justified.

No interface owns the underlying capability semantics.

Claude Code, Codex, and other coding agents may invoke Gremlins through MCP, but the system must not require them to operate.

An AI client should not need to create another frontier subagent merely to call a local Gremlins capability.

The MCP surface should stay small.

Current tools:

```text
gremlins_status
repo_search
code_read
git_history
repo_explorer
failure_triage
```

Future tools should be added by stable capability groups rather than exposing every internal implementation.

Internally, ordinary Python function calls are preferred.

There is no benefit in turning every internal function into another MCP hop.

---

# 7. Repository exploration pipeline

Repository exploration is a core workload and defines the Gremlins pattern.

```text
task
  |
  v
repository validation
  |
  v
path inventory
  |
  v
exact/literal search
  |
  v
bounded source reads
  |
  v
bounded Git history
  |
  v
evidence packet
  |
  +--> sufficient without model? --> return
  |
  v
one bounded local synthesis
  |
  v
validate evidence IDs
  |
  v
return compact result
```

The parent agent should receive selected evidence rather than re-reading the repository broadly.

The orchestrator may supply exact `terms` and `symbols` when it already knows the likely identifiers. Those explicit terms override Gremlins' fallback keyword guessing for repository-wide discovery. In a focused evidence request, if those exact terms miss inside a path already bounded by the request, Gremlins may perform bounded task-derived literal recovery within that path only. Bounded paths are either paths the orchestrator explicitly selected or tracked companion files that an already-relevant source file names literally (for example, `src/gremlins/config.py` naming `gremlins.toml`). Companion promotion and local recovery are deterministic structural relations; they never broaden repository-wide discovery terms. For simple location/reference tasks, `mode=auto` can return deterministic evidence without invoking the local model; `mode=deterministic` forces that behavior, while `mode=model` requests local synthesis.

The evidence used internally by the local model may be larger than the evidence returned to the frontier orchestrator. Returned evidence is separately bounded and prioritizes cited/selected items so Gremlins does not recreate the context problem it is intended to solve.

Later retrieval capabilities can include:

- language-aware symbol indexing;
- tree-sitter symbol extraction;
- code graph lookup;
- semantic retrieval.

These supplement exact search.

They do not replace it.

---

# 8. Failure and CI triage pipeline

The second core workload is failure analysis.

```text
log / test result / CI artifact
          |
          v
structured parser when available
          |
          v
failure line extraction
          |
          v
deduplication / grouping
          |
          v
causal vs cascading failure hints
          |
          v
bounded evidence
          |
          +--> obvious deterministic answer --> return
          |
          v
bounded local synthesis
          |
          v
root-cause confidence + next checks
```

Whenever possible Gremlins should consume:

- JUnit XML;
- test JSON;
- compiler diagnostics;
- structured CI metadata;

instead of asking a model to interpret an entire raw log.

---

# 9. Context architecture

Context control is one of the main reasons Gremlins exists.

A worker receives:

```json
{
  "task": "Find where retry exhaustion is classified and identify relevant tests",
  "repository": "/path/to/repo",
  "snapshot": "<git commit + dirty state>",
  "scope": ["src", "tests"],
  "evidence": ["bounded selected excerpts"]
}
```

It does **not** receive:

- the entire Claude/Codex conversation;
- unrelated files;
- previous subagent transcripts;
- arbitrary command history;
- all logs;
- the user's global shell/session context.

Starting budget policy:

- task text: small;
- evidence: bounded;
- local context: deliberately below advertised model maximum;
- output: compact;
- local model calls per worker: normally one;
- child workers: zero;
- concurrent local inference: bounded globally.

Budgets are enforced by the runtime, not merely described to the model.

---

# 10. Model execution architecture

The current implementation uses Ollama.

That is an implementation, not the architecture.

The final model layer is:

```text
worker
  |
  v
model class
  |
  v
provider resolver
  |
  +--> local Mac provider
  +--> remote/private provider
  +--> GPU provider
  +--> Android provider
```

Each provider adapter implements the same internal operations:

- health;
- model availability;
- structured completion;
- usage reporting;
- cancellation where supported;
- timeout;
- model identity;
- context/output limit validation.

Gremlins never silently converts a local-only request into cloud inference.

If no compatible local provider exists, the worker returns `needs-caller`.

Claude/Codex can then decide what to do.

---

# 11. Capability resolution

Capability-based dependency injection remains part of the final design.

However, the resolver must stay simple.

Initially:

```text
capability -> static configured implementation
```

Later:

```text
capability
   |
   +--> compatibility filter
   +--> permission filter
   +--> healthy provider filter
   +--> placement constraint
   +--> explicit preference
   |
   v
selected provider
```

The resolver should **not** become a general package SAT solver or Kubernetes-style scheduler.

A replacement must explicitly satisfy the requested capability contract.

---

# 12. Final repository structure

The repository should converge on this shape:

```text
gremlins/
├── README.md
├── architecture.md
├── gremlins.toml
├── pyproject.toml
├── uv.lock
├── stack.lock.template.json
│
├── skills/
│   ├── gremlins-delegation/
│   │   └── SKILL.md
│   ├── repo-search/
│   │   └── SKILL.md
│   ├── git-history/
│   │   └── SKILL.md
│   ├── log-analysis/
│   │   └── SKILL.md
│   ├── test-analysis/
│   │   └── SKILL.md
│   └── ...
│
├── contracts/
│   ├── jobs/
│   ├── tools/
│   └── workers/
│
├── workers/
│   ├── repo-explorer.toml
│   ├── triage.toml
│   ├── reviewer.toml
│   ├── ci-investigator.toml
│   └── ...
│
├── providers/
│   ├── ollama.toml
│   ├── mlx.toml
│   ├── llama-cpp.toml
│   └── ...
│
├── profiles/
│   ├── mac-local.toml
│   ├── mac-node.toml
│   ├── cluster-node.toml
│   ├── linux-node.toml
│   ├── android-termux.toml
│   └── android-native.toml
│
├── policies/
│   ├── repository-readonly.toml
│   ├── device-readonly.toml
│   └── ...
│
├── src/gremlins/
│   ├── cli.py
│   ├── server.py
│   ├── config.py
│   ├── contracts.py
│   ├── policy.py
│   ├── resolver.py
│   ├── registry.py
│   ├── jobs.py
│   ├── retrieval.py
│   ├── metrics.py
│   ├── deployment.py
│   ├── skills.py
│   ├── workers/
│   ├── tools/
│   ├── providers/
│   ├── nodes/
│   └── adapters/
│
├── adapters/
│   ├── claude/
│   ├── codex/
│   └── ...
│
├── deploy/
│   ├── launchd/
│   ├── systemd/
│   └── android/
│
├── evals/
│   ├── repo-explorer/
│   ├── triage/
│   ├── model-comparison/
│   └── security/
│
├── tests/
│   ├── contracts/
│   ├── security/
│   ├── tools/
│   ├── workers/
│   ├── adapters/
│   └── deployment/
│
└── scripts/
```

Not every directory needs to appear immediately.

The point is to keep canonical definitions distinct from runtime code and generated client configuration.

---

# 13. Canonical source of truth

Gremlins configuration must not be independently maintained in:

```text
~/.claude
~/.codex
~/.config
```

Those are deployment outputs.

Canonical state lives in the repository.

The deployment command:

```text
agentctl deploy --profile <profile>
```

resolves canonical definitions and writes only the client-specific pieces it owns.

Client configuration ownership must be narrow.

Gremlins must not replace an entire Claude or Codex configuration file if it only needs to manage one MCP registration or one skill directory.

---

# 14. Reproducibility and stack lock

Two deployments using the same Git commit should resolve to the same tested stack where practical.

Gremlins maintains two levels of locking.

## Python/application lock

`uv.lock` records Python dependency resolution.

## Gremlins stack lock

The generated stack lock records:

- Gremlins source commit;
- platform;
- Python version;
- package versions;
- canonical configuration digest;
- skill digests;
- worker definition digests;
- capability bindings;
- provider/runtime versions;
- model artifact identity;
- deployment profile;
- evaluation report digest.

A model tag alone is not enough.

Where available, record immutable model/artifact digests.

The lock captures reproducible inputs and deployment identity. `uv.lock` is committed and CI uses it as the Python dependency lock.

It does not promise mathematically identical model output across different hardware/runtime versions.

---

# 15. Deployment architecture

The deployment interface is:

```bash
agentctl deploy --profile mac-local
agentctl doctor
agentctl eval
agentctl report
```

A true generation-switching `agentctl rollback` is planned in the deployment-generation workstream. The current `rollback` command only prints the Git-checkout/reinstall procedure and must not be treated as an implemented atomic rollback mechanism.

Future profiles:

```bash
agentctl deploy --profile mac-node
agentctl deploy --profile cluster-node
agentctl deploy --profile linux-node
agentctl deploy --profile android-termux
agentctl deploy --profile android-native
```

Deployment performs:

1. platform detection;
2. dependency validation;
3. runtime installation/verification;
4. canonical configuration validation;
5. capability resolution;
6. model/runtime resolution;
7. model artifact installation;
8. adapter generation;
9. MCP registration;
10. service setup where required;
11. health checks;
12. contract tests;
13. smoke evaluations;
14. stack-lock generation;
15. activation.

A failed deployment must leave the last known-good generation usable.

---

# 16. Mac architecture

The Mac is both the first development environment and the first execution node.

Initial topology:

```text
Claude Code / Codex
        |
       MCP
        |
Gremlins on Mac
   |         |
 tools     Ollama
   |         |
 repo      local model
```

Preferred behavior:

- native execution;
- native Ollama/MLX/llama.cpp when useful;
- no mandatory Docker;
- stdio MCP for the simple local setup;
- one globally bounded inference slot initially;
- repository access constrained by policy;
- deterministic search via `rg` where available.

The local Mac remains useful in distributed deployments.

It can perform retrieval locally while delegating only bounded inference packets to another node.

---

# 17. Distributed architecture

Distributed expansion should not redesign the worker model.

The first distributed topology should be:

```text
Claude / Codex
     |
     v
Gremlins gateway on Mac
     |
     +---- local deterministic repository tooling
     |
     +---- model request ---------------------+
                                             |
                          +------------------+------------------+
                          |                  |                  |
                       Mac model         Linux model node          GPU node
                          |                  |                  |
                          +------------------+------------------+
                                             |
                                      structured result
```

Later, specific tool capabilities may also move to remote nodes.

## Node registration

A node should expose:

- node ID;
- platform;
- architecture;
- authenticated endpoint;
- model capabilities;
- tool capabilities;
- current health;
- available execution slots;
- policy labels;
- software/model identity.

## Scheduling

Do not introduce Kubernetes.

Use simple explicit placement and capability resolution.

Routing priority can consider:

1. policy;
2. compatibility;
3. health;
4. required model class;
5. placement preference;
6. available execution slot;
7. latency or queue depth.

A dedicated scheduler should only appear when this simple resolver is measurably insufficient.

---

# 18. Remote job semantics

Every remote job must have:

- job ID;
- request digest;
- snapshot ID;
- capability/worker identity;
- attempt number;
- deadline;
- cancellation state;
- side-effect classification;
- result digest.

For read-only jobs, a bounded retry can be safe.

For future write-capable jobs, a lost response does not prove that execution failed.

Writes therefore require idempotency or explicit reconciliation before retry.

---

# 19. Android architecture

Android joins without changing the concepts.

## Android as a device/tool node

This is the preferred first Android role.

Capabilities may include:

```text
android.logcat
android.instrumentation
android.apk.inspect
android.sensor.sample
android.camera.capture
android.microphone.capture
android.device-test
```

Some capabilities run from a host through ADB.

Others require a native Android app and Android permissions.

These must remain distinct implementations.

## Android as an inference node

Later, Android may provide:

```text
model.fast-classifier
model.evidence-extraction
```

through:

- llama.cpp;
- LiteRT-LM;
- another native runtime.

Inference admission should consider:

- available RAM;
- battery state;
- charging state;
- thermal state;
- foreground/background lifecycle;
- model size;
- latency.

Termux is useful for experimentation.

A native signed app is the long-term device-worker design.

---

# 20. Security architecture

The default Gremlins posture is deny-by-default.

## Repository permissions

Current safe profile:

```text
filesystem: repository read only
shell: none exposed to model
git: fixed read operations only
network: local/provider allowlist only
credentials: none
child workers: none
repository writes: none
```

## Path security

All repository paths must be resolved and normalized before use.

Gremlins protects against:

- `..` escapes;
- symlink escapes;
- credential-directory access;
- repository-root confusion;
- external-drive access not explicitly allowed.

## Git security

Never expose:

```text
run_git(arbitrary_arguments)
```

Expose bounded semantic functions such as:

```text
history_for_file(...)
search_commit_messages(...)
show_commit_metadata(...)
```

Git commands must use fixed argument structures.

## Network security

A worker cannot choose an arbitrary destination.

Model-provider endpoints are configured and allowlisted.

Remote provider endpoints must use authenticated private communication.

## Prompt injection

Repository text, logs, documentation, and third-party skill content are untrusted data.

Evidence supplied to a worker is data, not authority.

Tool and policy permissions do not change because a repository file asks them to.

---

# 21. Write-capable workers

Write capability is deliberately separate from the current architecture.

Before enabling repository writes, Gremlins must add:

- isolated worktree/workspace creation;
- execution sandbox;
- writable-path scope;
- command policy;
- test-execution policy;
- patch capture;
- explicit commit/push permissions;
- credential isolation;
- approval boundaries;
- write-job idempotency/reconciliation;
- stronger audit records.

A worktree alone is not a sandbox.

Running repository tests executes repository code and must be treated as code execution.

The future permission ladder should be explicit:

```text
read
branch-write
commit
push
merge
deploy
```

These permissions must never be implicitly bundled together.

---

# 22. External plugins and hot-swapping

The long-term system supports replacement of skills, tools, workers, providers, and model configurations.

The lifecycle is:

```text
fetch
  |
inspect
  |
verify identity/license/source
  |
validate manifest
  |
check platform + permissions
  |
run Gremlins-owned contract tests
  |
run security tests
  |
run task evaluations
  |
stage candidate
  |
health check
  |
activate for new jobs
  |
retain previous generation for rollback
```

Installing and activating are separate operations.

A fetched third-party plugin must remain inactive until it passes inspection and tests.

Plugin self-tests are useful but are not the authority.

Gremlins-owned tests determine compatibility.

---

# 23. Hot-swap boundaries

These components can be swapped independently when compatible:

- skills;
- tool implementations;
- workers;
- model configurations;
- model runtimes;
- inference providers;
- execution nodes;
- routing policy.

Active jobs remain pinned to the generation with which they started.

A new generation only affects new jobs.

If a candidate is incompatible:

- reject it;
- require an adapter; or
- define a new contract intentionally.

Never silently reinterpret incompatible behavior.

---

# 24. Plugin packaging

Do not build a marketplace first.

The basic package format can be a pinned Git source containing:

```text
plugin.toml
SKILL.md                 # when applicable
contracts/
tests/
resources/
LICENSE
```

Executable extensions require a stronger sandbox than instruction-only skills.

A checksum proves artifact identity.

It does not prove safety.

A signature proves publisher provenance.

It does not prove correctness.

---

# 25. Worker output design

Workers should return facts that a frontier model can use efficiently.

Repository worker example:

```json
{
  "status": "complete",
  "snapshot": {
    "head": "<commit>",
    "dirty": false
  },
  "summary": "Retry exhaustion is classified in the transport layer.",
  "findings": [
    {
      "claim": "The final retry maps to RetryExhaustedError.",
      "kind": "observed",
      "evidence_ids": ["e3", "e7"]
    }
  ],
  "evidence": [
    {
      "id": "e3",
      "path": "src/transport.py",
      "start_line": 120,
      "end_line": 147
    }
  ],
  "limitations": [],
  "truncated": false,
  "usage": {
    "local_model_calls": 1
  }
}
```

The parent gets:

- enough evidence to verify;
- enough interpretation to avoid repeating the exploration;
- not enough bulk to recreate the original context problem.

---

# 26. Metrics architecture

Gremlins must prove it is useful.

Per job, record:

- worker;
- status;
- evidence count;
- evidence size;
- deterministic tool calls;
- local model calls;
- local input tokens;
- local output tokens;
- queue time;
- model time;
- total elapsed time;
- escalation reason;
- provider/model identity;
- snapshot ID;
- result size.

Do not log source contents, full prompts, or raw logs by default.

At the workflow level compare:

- frontier uncached input tokens;
- frontier cache creation;
- frontier cache reads;
- frontier output tokens;
- number of frontier subagents;
- local worker calls;
- local inference tokens;
- task latency;
- developer waiting time;
- escalation rate;
- completion quality;
- local memory/CPU/GPU pressure.

The primary success metric is not "how many local calls happened."

It is:

> Did the same developer task finish correctly while consuming substantially less frontier context and frontier-agent work?

---

# 27. Evaluation architecture

Every worker and replacement model needs a task corpus.

Evaluation sets should include:

- easy positive cases;
- no-evidence cases;
- ambiguous cases;
- misleading evidence;
- dirty repositories;
- large repositories;
- malformed logs;
- duplicate failures;
- cascading test failures;
- timeouts;
- unavailable providers;
- prompt-injection text;
- symlink/path-escape attempts.

Evaluate three layers.

## Structural

- schema valid;
- evidence references valid;
- no missing mandatory fields;
- bounded output.

## Behavioral

- expected files found;
- expected failure signatures found;
- important evidence not omitted;
- no invented files/commits;
- appropriate `needs-caller` behavior.

## Operational/security

- timeout;
- cancellation;
- model unavailable;
- node unavailable;
- path escape;
- credential-path rejection;
- oversized input;
- malicious repository text.

---

# 28. Routing philosophy

Gremlins should use the cheapest reliable operation first.

Default routing order:

```text
deterministic parser/tool
        |
        v
small bounded local model
        |
        v
return needs-caller
        |
        v
frontier orchestrator decides
```

Gremlins itself does not escalate to the cloud.

This keeps billing, context, and high-risk decisions visible to the parent coding agent.

---

# 29. What Gremlins should not become

Gremlins should not turn into:

- another IDE;
- another full coding agent;
- a Kubernetes clone;
- a general distributed operating system;
- a universal package manager;
- an autonomous agent swarm;
- an unrestricted shell broker;
- a hidden cloud-routing proxy;
- a vector database project without a measured need;
- a giant permanent-memory system;
- a public plugin marketplace before local contracts are proven.

Complexity must be earned by a real workload.

---

# 30. Current implementation

The repository already contains a working local read-only base.

Implemented now:

- direct CLI capability invocation;
- MCP server adapter;
- optional Claude Code registration;
- optional Codex registration;
- canonical `gremlins.toml`;
- portable `SKILL.md` definitions;
- deterministic exact repository search;
- bounded source reads;
- bounded Git history;
- repo explorer;
- failure triage;
- Ollama provider;
- Qwen local-model default;
- structured Pydantic result validation;
- evidence-ID validation;
- repository/root/path security;
- denied credential paths;
- no arbitrary shell MCP tool;
- no Git writes;
- no child-worker spawning;
- host-level local-inference lock;
- local job metrics;
- stack-lock generation;
- doctor command;
- smoke evaluations;
- compact frontier-facing evidence results;
- orchestrator-supplied exact terms/symbols;
- deterministic-only repo exploration path;
- distinct `busy` and `needs-caller` statuses;
- machine-specific allowed roots in deployment profiles rather than canonical config;
- macOS and Linux CI;
- A/B/C benchmark data model;
- automated Claude/Codex frontier benchmark runner;
- fresh-clone isolation for each benchmark arm;
- repeated B/C pairing by case + iteration;
- counterbalanced B/C ordering;
- unique per-run Gremlins measurement tags;
- benchmark study provenance (source commit, Gremlins commit, client version);
- aggregate and median per-pair frontier-usage reporting;
- benchmark gate command.

Not yet implemented despite appearing in the end-state architecture: declarative worker manifests, a general capability registry/resolver, deployment generations with atomic rollback, multi-node routing, Android workers, external plugin activation, or write-capable workers.

This is the foundation, not a throwaway prototype.

The implementation should evolve in place toward the final architecture rather than creating separate "old" and "new" stacks.

---

# 31. Implementation plan

The implementation order is intentionally driven by proof of savings rather than by architectural completeness.

The key sequencing rule is:

> **Prove that Gremlins reduces frontier work before investing in abstractions whose value depends on that reduction.**

The current code should evolve in place. No parallel rewrite or "v2" stack is planned.

---

## Workstream A — harden the current Mac stack

### Goals

Make the current small system trustworthy enough to measure.

### Work

- keep returned worker evidence compact and independently bounded from local-model input;
- accept orchestrator-supplied exact terms/symbols;
- support a real deterministic-only return path;
- distinguish `busy` from `needs-caller`;
- pin dependencies with committed `uv.lock`;
- strengthen installer idempotency;
- improve `doctor` so it tests actual MCP invocation rather than only executable presence;
- keep core install, local-model provider setup, and client-adapter registration as separate explicit operations;
- add installer recovery if optional client registration fails;
- detect unsupported optional client CLI syntax cleanly;
- add model warm-up checks only for profiles that enable local inference;
- add explicit cancellation and timeout handling;
- keep the inference lock in Gremlins state and make its wait policy configurable;
- validate dirty-repository snapshot reporting;
- improve exact search handling for binary files, ignored files, submodules, and very large repositories;
- add direct MCP integration tests.

### Exit criteria

- core installation works without requiring an AI client;
- local-model setup is optional and separable from core installation;
- reinstall is safe;
- uninstall only removes Gremlins-owned configuration;
- direct CLI capability invocation works independently;
- optional MCP clients can invoke the same capabilities;
- tests pass on macOS;
- returned evidence is bounded tightly enough that a Gremlins call does not recreate a large frontier context;
- local worker failure never damages a repository;
- unavailable local inference returns useful evidence plus `needs-caller`;
- local inference contention returns `busy`, not a misleading escalation status.

---

## Caller-independent capability measurement

**Status:** Capability Benchmark v1 implemented and enforced in normal CI.

Before client-specific savings are considered, Gremlins now validates its own deterministic capability layer independently. The v1 gate covers exact search, bounded reads, Git history, deterministic repo exploration, log-evidence extraction, policy enforcement, contract shape, result-size bounds, and zero model calls. Latency and process resource use are recorded for trend analysis but are not hard CI thresholds yet.

This product-level baseline is separate from the frontier-client integration experiment below.

---

## Workstream G — measure whether Gremlins actually saves frontier work

**Implementation status:** measurement harness implemented. The product gate remains **pending** until real paired frontier runs are collected on the Mac and satisfy the criteria below. No later gated workstream should be treated as unlocked yet.

Implemented measurement infrastructure includes fresh-clone B/C isolation, counterbalanced ordering, repeated case/iteration pairing, unique Gremlins measurement tags, hidden path + answer-claim scoring, client/source/Gremlins provenance locking, Claude/Codex usage normalization, Claude subagent-call telemetry where exposed, compact local-result accounting, resumable suites, and an executable gate.

### Why this is second

This workstream decides whether the rest of the architecture is worth building.

One integration assumption is that a frontier coding agent can use a Gremlins result **without broadly repeating the same retrieval and reasoning work**.

That assumption must be measured, but it is not the definition of the product. Capability correctness, latency, result size, resource use, and contract stability must also be measurable without any frontier agent.

### Experiment arms

Use the same representative development tasks in three arms:

1. **A — current/frontier-heavy workflow**
2. **B — tuned frontier workflow without Gremlins**, using good direct search/context discipline
3. **C — Gremlins-assisted workflow**

The important comparison is **C versus B**. Otherwise deterministic retrieval improvements may be incorrectly credited to local inference.

### Record

- frontier uncached input;
- frontier cache creation;
- frontier cache reads;
- frontier output;
- API-equivalent cost using the actual model pricing/counter semantics;
- frontier subagent count;
- Gremlins calls;
- local model calls/tokens;
- frontier-facing Gremlins result size;
- whether the orchestrator repeats the same searches/reads after Gremlins;
- completion quality;
- false/unsupported worker conclusions;
- escalation rate;
- end-to-end latency;
- developer waiting time;
- Mac memory pressure.

Prompt-cache reads and ordinary input tokens must not be treated as economically identical. Report both raw counters and cost-weighted figures.

### Initial measurement gate

Before major architecture expansion, the representative corpus should show:

- no material completion-quality regression against tuned baseline B;
- a meaningful reduction in frontier context/subagent work;
- a meaningful reduction in cost-weighted frontier consumption on targeted tasks;
- bounded Gremlins result payloads;
- an acceptably low rate of frontier re-do/re-verification;
- no unacceptable latency or machine-pressure regression.

Initial numerical targets can be used for experiments, but they are not architectural truths. If C does not beat B, stop and improve or simplify Gremlins rather than building the registry, cluster, or plugin system.

### Gate

**Workstreams B, C, E and H–N do not proceed as product expansion until this gate passes.**

D and F may proceed only as focused experiments intended to improve the measured result.

---

## Workstream D — improve deterministic retrieval where measurement shows gaps

### Priority

The first retrieval improvement is **not** semantic indexing.

The orchestrating frontier model is already good at selecting search terms. Gremlins therefore accepts explicit `terms` and `symbols`; those override heuristic keyword extraction.

### Work

In measured order of need:

1. explicit orchestrator terms/symbols;
2. better exact-search/result ranking;
3. structured test/log parsers;
4. Git-diff-aware retrieval;
5. test-to-source relationship lookup;
6. language-aware symbol extraction/tree-sitter where it improves the corpus;
7. code graph lookup when measured;
8. optional semantic retrieval as a **separate** capability.

### Rules

- exact search remains available;
- semantic retrieval never masquerades as complete literal search;
- every retriever returns bounded, addressable evidence;
- retrieval changes must be evaluated on frontier savings as well as retrieval recall.

### Exit criteria

Retrieval finds the evidence needed for the measured task set with lower frontier redo rate and without materially increasing returned context.

---

## Workstream F — benchmark model value and provider alternatives

**Status:** Mac reference model-value measurement completed and the per-worker routing decision is frozen for the reference profile.

Current measured routing:
- repo exploration stays deterministic in normal operation; local synthesis did not meet the materiality threshold;
- bounded triage keeps deterministic evidence extraction and may use the local model for grouping/explanation/next checks;
- Claude-level ambiguous debugging, architecture, trade-offs, patch design, and subtle review are explicitly outside this local-worker optimization.

The broader triage stability study (20 categories × 3 repeats) confirmed the triage decision: +27.92 percentage points mean quality, 60/60 successful model calls, 20/20 stable cases, 19/20 cases above the quality floor, and zero regressions. This is the Mac reference behavior to reproduce on THG. It is still not a claim that one model/runtime is universally best.

### Goals

Determine when local inference adds value beyond deterministic retrieval.

### Work

- compare deterministic-only versus local-synthesis results;
- benchmark models per worker/task family rather than with generic chat benchmarks;
- measure whether local summaries reduce or increase frontier re-verification;
- keep the Ollama adapter;
- experiment with MLX on Apple Silicon only if it improves measured latency/resource use;
- experiment with llama.cpp or other runtimes only when useful;
- capture immutable model identity where practical;
- measure latency, memory, correctness, escalation and result size.

### Exit criteria

For each worker, there is evidence that either:

- local inference improves the end-to-end frontier workflow enough to keep it; or
- deterministic retrieval is better, in which case the model call is removed for that workload.

---

## Workstream B — make workers declarative

**Gate: Workstream G must pass first.**

### Goals

Make worker composition replaceable only after there is a proven worker product worth generalizing.

### Work

- introduce `workers/*.toml`;
- define worker schema;
- define permission profiles;
- move skill composition into worker definitions;
- move result-contract selection into worker definitions;
- move model-class selection into worker definitions;
- implement worker loader/validator;
- validate required capabilities before worker activation;
- retain Python implementations for execution logic.

### Exit criteria

A new read-only worker can be added primarily through a definition, skills, capabilities, result contract and tests rather than custom orchestration wiring.

---

## Workstream C — formalize the capability registry and resolver

**Gate: Workstream G must pass first, and B must demonstrate a real need for implementation substitution.**

### Goals

Make proven implementations replaceable without constructing a heavyweight plugin framework.

### Work

- registry of capabilities;
- explicit implementation bindings;
- platform compatibility constraints;
- permission compatibility constraints;
- health-aware provider selection;
- model-class resolution;
- `agentctl inspect`;
- `agentctl capabilities`.

### Exit criteria

Changing the configured implementation for a proven capability does not require editing its consumer.

---

## Workstream E — expand the worker catalog

**Gate: Workstream G must pass first.**

Add workers only for recurring measured workloads.

Likely order:

1. repo explorer;
2. triage;
3. CI investigator;
4. test analyzer;
5. documentation extractor;
6. mechanical reviewer;
7. simple test generator;
8. bounded refactor planner;
9. Android specialist;
10. Linux specialist;
11. release verifier.

A worker is not added merely because the abstraction can support it.

Write-capable workers remain disabled until the dedicated write-security workstream.

---

## Workstream H — robust deployment generations

**Gate: Workstream G must pass first.**

### Work

- stage deployment generations under Gremlins state;
- produce stack lock before activation;
- validate/evaluate a staged generation;
- atomically switch new jobs;
- retain previous generation;
- implement a real `agentctl rollback`;
- add `agentctl history`;
- preserve unrelated client configuration.

### Exit criteria

A deliberately broken candidate fails activation while the prior generation remains usable, and rollback is an actual generation switch rather than printed Git advice.

---

## Workstream I — second Mac / Linux node

**Gate: Workstream G must pass first.**

### Work

- systemd deployment;
- Linux ARM64/AMD64;
- configurable private provider endpoint;
- authenticated remote inference;
- provider health/timeout handling;
- keep repository retrieval local initially.

### Exit criteria

The same proven worker can move only its inference from the Mac to another node without changing its task/result contract.

---

## Workstream J — multi-node registry and routing

**Gate: Workstream G must pass first and there must be a measured reason to use multiple nodes.**

### Work

- node manifest;
- authenticated node endpoint;
- health heartbeat;
- capability advertisement;
- execution-slot reporting;
- model inventory;
- simple routing/placement;
- job IDs/deadlines/cancellation;
- read-only retry policy;
- node-disappearance behavior.

### Exit criteria

A proven worker requests a model capability and can execute on a compatible healthy node without knowing the physical node name.

No Kubernetes-style scheduler is introduced unless simple placement becomes measurably insufficient.

---

## Workstream K — external components and hot-swap

**Gate: Workstream G must pass first and at least one real replacement use case must exist.**

Add:

```text
agentctl install
agentctl inspect
agentctl test
agentctl activate
agentctl rollback
```

Installation and activation remain separate. Gremlins-owned tests, not plugin self-tests, determine compatibility.

---

## Workstream L — Android device worker

**Gate: Workstream G must pass first and a concrete Android capability workload must exist.**

Start with device-specific capabilities, not inference:

- logcat;
- instrumentation;
- APK inspection;
- sensor/camera test contracts;
- native app capabilities where Android permissions require them;
- authenticated job transport;
- user-visible execution state.

### Exit criteria

A frontier client can invoke a useful Android capability through the same Gremlins task/contract model.

---

## Workstream M — Android inference

**Gate: Workstream G must pass first, and Android device-worker value must already be proven.**

Benchmark on the real device:

- RAM;
- thermals;
- battery/charging;
- latency;
- quality;
- lifecycle stability;
- llama.cpp/LiteRT-LM or another appropriate runtime.

Expose an Android model capability only if it wins a real workload.

---

## Workstream N — write-capable workers

**Gate: Workstream G must pass first and read-only Gremlins must already be dependable.**

Required before enabling writes:

- isolated worktree/workspace;
- execution sandbox;
- command and network policy;
- scoped credentials;
- patch capture;
- test-execution sandbox;
- branch-only write default;
- separate commit/push/merge permissions;
- explicit approvals;
- idempotent/reconcilable job semantics;
- audit trail;
- recovery/revert flow.

### Exit criteria

A worker can produce a controlled patch without gaining accidental access to unrelated repositories, credentials, publication rights or deployment authority.

---

## Sequence summary

```text
A  harden the smallest working Mac path
|
G  measure C vs tuned frontier baseline B
|  PASS? ----------------------------------------------------+
|                                                            |
+--> no: simplify/fix/stop expansion                         |
|                                                            |
+--> yes                                                     |
     |                                                       |
     +--> D retrieval improvements backed by evidence        |
     +--> F model/provider experiments backed by evidence    |
     |                                                       |
     +--> B declarative workers, when needed                 |
          |
          +--> C capability registry, when needed
          +--> E additional proven workers
          +--> H deployment generations
          +--> I/J distributed execution only when useful
          +--> K external replacement ecosystem
          +--> L/M Android
          +--> N writes last
```

# 32. Operational state

Local state should live under:

```text
~/.local/state/gremlins/
```

Suggested layout:

```text
active-generation
generations/
jobs.jsonl
stack.lock.json
evals/
cache/
locks/
```

Secrets should not live in this directory unless protected by a dedicated secrets mechanism.

---

# 33. Job identity and snapshots

Every repository job should know what it inspected.

Snapshot identity should include:

- repository path/ID;
- HEAD commit;
- dirty state;
- relevant dirty-file information where needed.

For remote repository work, the node must operate on an explicit snapshot.

Two machines having directories with the same repository name does not mean they contain the same code.

---

# 34. Failure behavior

Gremlins should fail compactly and predictably.

Examples:

## Local model unavailable

Return deterministic evidence and the caller-neutral status:

```text
status: needs-caller
```

During the pre-1.0 compatibility window, model-unavailable results also include `legacy_status: needs-frontier` for consumers migrating from the prototype contract. New consumers must use `status`.

## Inference slot busy

Return:

```text
status: busy
```

Do not spawn another worker.

## Evidence truncated

Return:

```text
status: partial
truncated: true
```

## Node disappears

Return known evidence plus node/job status.

Retry only when the operation is safely retryable.

## Model returns invalid structure

Attempt at most a tightly bounded repair if explicitly enabled.

Otherwise return `needs-caller`.

No retry storms.

---

# 35. Concurrency

Concurrency must remain bounded at every level.

Initial defaults:

- one local inference job;
- small bounded job queue;
- deterministic tools can run concurrently where safe;
- worker child count zero.

Later node capacity is advertised as execution slots.

The resolver must never interpret "more nodes" as permission for uncontrolled fan-out.

---

# 36. Future reviewer/test/refactor workers

More capable workers should still follow the same evidence pattern.

Example reviewer:

```text
diff
  |
deterministic changed-file inventory
  |
relevant tests / ownership / static checks
  |
bounded local review
  |
structured findings with file/line evidence
```

Example test generator:

```text
target behavior
  |
existing test patterns
  |
relevant source API
  |
bounded local generation
  |
candidate patch
```

Generation does not imply permission to write or commit.

A candidate patch can be returned as data until write capability is explicitly enabled.

---

# 37. Definition of success

Gremlins succeeds when all of these are true:

1. Claude/Codex remain pleasant to use.
2. Mechanical exploration happens locally by default.
3. Frontier context is materially smaller on targeted workflows.
4. Frontier subagent use drops for mechanical tasks.
5. Local workers return enough evidence that the frontier model does not redo the same exploration.
6. Quality is preserved or improved.
7. Local model failures degrade gracefully.
8. The system remains understandable and maintainable.
9. Deploying to a new compatible machine is straightforward.
10. Replacing a model/tool/worker does not require redesigning consumers.
11. Linux nodes and Android can join through the same capability and worker contracts.
12. Security boundaries remain explicit as functionality grows.

---

# 38. Final architectural principle

The most important abstraction in Gremlins is not "agent."

It is a **bounded evidence job**:

> Given this task, scope, snapshot, permissions, and budget, use the cheapest compatible capabilities to return a compact, verifiable result.

That abstraction allows the implementation underneath to change:

- exact search can improve;
- a new skill can replace an old one;
- Qwen can become another model;
- Ollama can become MLX or llama.cpp;
- a local Mac model can move to Linux model node;
- a worker can later execute on Android;
- Claude can be replaced by another orchestrator.

The consumer still asks for the same useful capability.

That is the final Gremlins architecture.
