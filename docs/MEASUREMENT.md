# Gremlins measurement

Gremlins is caller-independent, so measurement has two layers.

## Capability-layer measurement

This is the product-level baseline and does not require an AI agent.

The executable v1 gate is:

```bash
gremlins benchmark capabilities --repository .
```

It runs from a clean Gremlins checkout and evaluates the bundled hidden-answer corpus from an isolated clone where `evals/` is removed from the searchable worktree.

Capability Benchmark v1 measures:

- exact-search expected-path coverage;
- bounded source-read correctness around exact-search anchors;
- bounded Git-history behavior;
- deterministic repo-explorer expected-path coverage;
- deterministic repo-explorer result-size compliance;
- confirmation that deterministic repo exploration makes zero local-model calls;
- deterministic failure/log evidence extraction;
- path/scope/repository security-policy rejection;
- core result-contract shape;
- latency distributions;
- process CPU use and peak RSS.

### Capability Benchmark v1 gate

The v1 command exits nonzero unless all correctness, policy, and contract probes pass, every deterministic repo-explorer case remains within its result budget, and local-model calls remain exactly zero.

Latency, CPU time, and RSS are recorded but are **informational in v1**. They are intentionally not hard thresholds yet because GitHub-hosted runner variance would make those gates noisy. Once enough measurements exist, regression thresholds can be based on observed distributions rather than guesses.

This gate is now part of normal macOS and Linux CI.

### What v1 does not measure

The capability gate does not answer whether local inference is useful. That is a separate deterministic-vs-model experiment.

## Deterministic vs local-model value measurement

The first real model-value environment is the Apple-Silicon Mac reference deployment.

Bootstrap it explicitly:

```bash
./scripts/setup_mac_local_model.sh
```

Then run:

```bash
uv run gremlins benchmark model-value --repository . --worker all
```

The benchmark pairs the same tasks against:

- deterministic Gremlins evidence;
- deterministic evidence plus explicit local-model synthesis.

The repo-explorer arm uses the existing hidden expected paths and claims. The triage arm uses a separate hidden five-case log corpus. The primary signal is mean paired quality gain; result size, latency, prompt tokens, and output tokens are reported so any gain can be judged against its cost.

The configured reference model is currently `qwen3.5:4b` via the optional Ollama provider. That is an experiment implementation, not a Gremlins architectural dependency.

Normal CI remains model-free. The model-value benchmark is manual because it requires a real local model and machine-specific performance is part of what is being measured.

### First Mac result — routing consequence

The first real run used public main `7810291489f41ca566f83df95110d077d16cf4c7`, Ollama, and `qwen3.5:4b`.

Repo explorer, 10 paired cases:

- deterministic quality: 0.95;
- model quality: 0.9833;
- mean quality gain: +0.0333 (+3.33 percentage points), below the +0.10 materiality threshold;
- deterministic median latency: 0.2411 s;
- model median latency: 9.5233 s (~39.5x);
- median result-size change: +33.92%.

**Routing consequence:** normal repo exploration remains deterministic. Explicit model mode remains available for controlled experiments, not normal routing.

Triage, 5 paired cases:

- deterministic quality: 0.60;
- model quality: 0.95;
- mean quality gain: +0.35 (+35 percentage points);
- model success/call rate: 100%;
- model median latency: 4.9349 s;
- median result-size change: +83.55%.

The deterministic triage arm already preserved the relevant failure evidence; the measured gain came from grouping/explanation/next-check synthesis. Therefore Gremlins keeps deterministic evidence extraction as the first stage and permits bounded local-model synthesis for triage. This does not authorize Gremlins to replace Claude-level ambiguous debugging or architectural reasoning.

The first full run used 15 model calls, 21,153 prompt-eval tokens, and 2,659 eval/output tokens. That result was followed by the broader repeated stability study below.

### Triage Stability Benchmark v1

The follow-up benchmark expands triage to 20 directly-supported failure scenarios across dependency, service, storage, authentication, database, DNS, permissions, port conflict, TLS, configuration, filesystem, memory, assertion, schema, network, rate-limit, artifact-integrity, input-format, tooling, and timeout failures.

Run three rounds on the Mac reference environment:

```bash
uv run gremlins benchmark triage-stability --repository . --repeats 3
```

That is 60 local-model calls. The command rotates the case order between rounds and reports:

- deterministic baseline quality;
- mean/min/max local-model quality per case;
- per-case quality span and standard deviation;
- mean quality gain;
- model success rate;
- stable-case fraction;
- quality-floor fraction;
- regression-case fraction;
- median and p95 local-model latency;
- prompt-eval and output-token totals.

The default stability signal requires at least +10 percentage points mean quality gain, 95% model success, at least 90% stable cases, at least 90% of cases meeting a 0.75 mean model-quality floor, and no more than 10% of cases regressing versus their deterministic baseline. These are bounded engineering thresholds for this experiment, not universal model truths.


#### Mac reference result — PASS

On public main `43a3ac29bae189719395cd954e58b03fab847763` with Ollama + `qwen3.5:4b`, the 20-case × 3-repeat study produced:

- deterministic mean quality: 0.6375;
- local-model mean quality: 0.9167;
- mean quality gain: +0.2792 (+27.92 percentage points);
- model success: 60/60 calls (100%);
- stable cases: 20/20 (100%);
- cases at or above the 0.75 mean model-quality floor: 19/20 (95%);
- regressed cases versus deterministic: 0/20;
- median local-model latency: 5.2553 s;
- p95 local-model latency: 7.315 s;
- prompt-eval tokens: 14,955;
- eval/output tokens: 9,921.

The benchmark returned `stable_material_value=true` with no failed stability reasons. The only below-floor case was the local-service connection-refused case at 0.6667 mean model quality; it still improved over its 0.50 deterministic baseline and remained within the configured repeat-variation bound.

**Reference decision:** the Mac profile keeps deterministic failure-evidence extraction as stage one and uses local `qwen3.5:4b` synthesis for bounded failure grouping, directly-supported likely-cause explanation, and next-check suggestions. This does not extend Gremlins into ambiguous root-cause debugging or other Claude-level reasoning.

It also does not answer whether Claude, Codex, or another coding client saves frontier work by using Gremlins. That remains the client-integration B/C experiment below.

## Claude subagent observation before replacement benchmarking

Before deciding what additional Gremlins capabilities to build, observe Claude's normal delegation behavior with Gremlins hard-disabled:

```bash
uv run gremlins benchmark observe-claude-subagents \
  --repository . \
  --study mac-claude-subagents-v1 \
  --repeats 1
```

The v1 corpus contains ten read-only software-engineering task families, including broad/fuzzy discovery, history interpretation, test investigation, root-cause investigation, architecture planning, cross-file flow, security investigation, test-to-source mapping, fuzzy local-model-usage discovery, and CI investigation.

The observation harness:

- disables every Gremlins MCP tool for the run;
- invokes Claude with `--setting-sources project` so user-level skills/settings do not bias delegation behavior;
- allows Claude's Agent tool and does not instruct Claude to spawn agents;
- asks Claude to use its normal workflow;
- removes the benchmark corpus plus Gremlins delegation/measurement/reference README/architecture docs from each isolated observation workspace;
- records raw Claude stream JSON locally for audit;
- captures every visible `Agent`/`Task` tool call, declared subagent type, delegated description/prompt, and parent tool-use metadata when exposed;
- reports visible nested/sub-sub-agent calls when the Claude stream exposes parent relationships;
- classifies the **delegated request** as evidence acquisition, bounded analysis, mixed evidence+reasoning, Claude-level reasoning, utility/out-of-scope, or unknown.

The classifier is intentionally transparent and non-gating. It uses known agent semantics (`Explore` is retrieval, `Plan` is planning/reasoning) plus explicit keyword signals for catch-all/general-purpose prompts. The report includes the matched signals and raw delegated prompts so the classification can be reviewed manually.

Two different quantities matter:

1. **Subagent mix:** which agent types Claude actually spawns and how often.
2. **Delegated-work mix:** what fraction of observed calls are pure evidence acquisition, bounded analysis, mixed evidence+reasoning, or Claude-level reasoning.

The report does **not** claim to know exact token/time percentages inside hidden subagent execution. A mixed call is a candidate to split—Gremlins can gather evidence while Claude retains reasoning—not a candidate for wholesale replacement. Nested calls are only measurable when Claude surfaces them in the parent stream.

### Offline parent-retrieval analysis

The observation run always saves raw Claude streams. Analyze those existing files without making another Claude call:

```bash
uv run gremlins benchmark analyze-claude-observation \
  --study mac-claude-subagents-v1
```

This counts root/parent Claude tool activity separately from Agent calls and visible nested tool activity. It treats direct `Read`/`Grep`/`Glob`/search-style tools and read-only Git/shell inspection commands as evidence acquisition, reports the per-case tool-name mix, and includes terminal usage/cost even for max-turn results.

This is intentionally an **offline** analysis. Running it consumes no Claude quota. Its purpose is to distinguish two savings opportunities:

1. replacing spawned retrieval agents; and
2. reducing direct parent-Claude retrieval loops during reasoning-heavy tasks such as RCA.

### Evidence-service gate

The Claude observation study showed that the larger opportunity is direct parent-model retrieval, not only spawned subagents: **128 of 139 root non-Agent tool calls (92.1%) were evidence acquisition**, and eight of ten tasks gathered evidence without spawning any Agent.

The resulting product boundary is:

```text
caller/frontier model: hypotheses, causal reasoning, architecture, judgment
Gremlins: search, bounded reads, test/source links, Git/change evidence, compact evidence packs
```

The deterministic evidence-service gate is:

```bash
uv run gremlins benchmark evidence-service --repository .
```

It uses an isolated clone with `evals/` removed and checks observation-derived broad/fuzzy discovery, history evidence, test/source relationships, expected-path coverage, result-size bounds, and **zero local-model calls**. It is part of normal macOS/Linux CI.

The evidence service is intentionally repeatable. A frontier caller may use several `evidence_pack` calls while doing RCA; the caller still owns hypotheses and causal conclusions.

### Mac reference result — PASS

On public main `91696e130504aa01f819c0f70bcd8f8a8db49321`, the Apple-Silicon Mac reference run passed the deterministic evidence-service gate:

- cases: 9/9;
- expected-path assertions: 16/16;
- required test/source relationship: 1/1;
- required history evidence: 1/1;
- local-model calls: 0;
- maximum result size: 7,984 / 8,000 chars;
- elapsed time: 10.922 s.

This confirms the evidence-service behavior on the intended Mac reference deployment, not only GitHub-hosted CI.

## Claude/Codex integration gate before THG

THG deployment is intentionally paused until Gremlins is tested with both frontier clients on the Mac reference environment.

For each client, run a separate provenance-locked study with `--include-a`:

- **A — normal client workflow:** Gremlins is forbidden; the client's normal subagent/multi-agent behavior is allowed.
- **B — tuned frontier-only:** Gremlins is hard-disabled and subagents are disabled.
- **C — Gremlins evidence loop:** subagents are disabled; the client must use deterministic `evidence_pack` first and may make **1–4 tagged evidence-pack calls** as its reasoning develops.

The product gate remains **B vs C**. Arm A is diagnostic: it shows how much the client's normal agent/subagent-heavy workflow costs relative to a disciplined baseline.

Arm C now matches the measured architecture rather than the older one-shot repo-explorer experiment:

```text
frontier hypothesis/question
        |
        v
evidence_pack #1
        |
        v
frontier reasoning
        |
        +--> enough evidence -> answer
        |
        +--> narrower evidence question
                  |
                  v
             evidence_pack #2..#4
```

Every C-arm evidence-pack call must reuse the run's unique measurement tag. Other Gremlins capabilities and local-model calls make the treatment invalid. Direct frontier Read/Grep/Glob/Bash/Git retrieval remains available only as fallback so the benchmark can measure when Gremlins is insufficient; the response must report `FRONTIER_REDO_SEARCH=true` if that fallback occurs.

Claude enforcement uses the CLI tool allow/deny surface. Codex enforcement uses per-run config overrides: arm B sets `mcp_servers.gremlins.enabled=false`; arm C enables the Gremlins MCP server and restricts it to `evidence_pack`. Both B and C disable client subagents.

The runner now records, in addition to tokens/acceptance/elapsed time:

- direct parent/frontier non-Gremlins tool calls;
- direct parent/frontier evidence-acquisition calls;
- Gremlins evidence-pack call count;
- local-model call count;
- frontier redo-search marker.

This lets us test the core observation-derived hypothesis directly: **does the evidence loop remove parent-model retrieval operations, not merely reduce tokens?**

Before spending frontier quota, run the zero-token integration preflight:

```bash
uv run gremlins benchmark frontier-preflight \
  --repository . \
  --client claude

uv run gremlins benchmark frontier-preflight \
  --repository . \
  --client codex
```

The preflight verifies client readiness, clean benchmark source, the installed Gremlins MCP wrapper, wrapper/runtime-root freshness, the real MCP tool surface including `evidence_pack`, and client MCP registration. It makes **zero frontier-model calls**. A B/C suite now runs this preflight before any arm, so a broken C treatment cannot spend quota on B baselines first.

For a new treatment revision, use a fresh study name. A single-case `benchmark run` refuses to append a duplicate case/arm/iteration unless `--force` is explicit. `benchmark clear --study NAME` removes the study records, provenance metadata, and saved raw streams for that study.

After preflight passes, run one small paired diagnostic before the breadth pass. The final answer from each arm is explicitly required to name exact repository-relative paths supporting its concrete claims because structural acceptance checks those paths.

Then run one breadth-first pass with fresh v2 study names:

```bash
uv run gremlins benchmark suite \
  --study mac-claude-evidence-loop-v2 \
  --repository . \
  --client claude \
  --include-a \
  --repeats 1 \
  --save-raw

uv run gremlins benchmark suite \
  --study mac-codex-evidence-loop-v2 \
  --repository . \
  --client codex \
  --include-a \
  --repeats 1 \
  --save-raw
```

Then inspect:

```bash
uv run gremlins benchmark report --study mac-claude-evidence-loop-v2
uv run gremlins benchmark gate --study mac-claude-evidence-loop-v2

uv run gremlins benchmark report --study mac-codex-evidence-loop-v2
uv run gremlins benchmark gate --study mac-codex-evidence-loop-v2
```

If a client is close to the gate or results are noisy, repeat the same study with `--repeats 2`; the suite resumes the missing second iteration without discarding the first.

Do not compare Claude's absolute token counts directly with Codex's as if their accounting semantics were identical. The primary decision is within-client B→C reduction and quality preservation, with A providing client-specific normal-workflow context.

## Client-integration measurement

The existing A/B/C harness is an integration experiment for frontier coding agents.

Its question is:

> When a coding agent uses Gremlins, does the same developer task finish correctly with less frontier work than a well-tuned agent-only workflow?

Claude/Codex are benchmark clients here, not Gremlins dependencies.

Use three arms for the same case:

- **A** — current/frontier-heavy workflow.
- **B** — tuned frontier workflow without Gremlins.
- **C** — Gremlins-assisted workflow.

The primary comparison is **B versus C**. For the v2 evidence-loop treatment, inspect both frontier processed-token change and `frontier_direct_evidence_calls_change_pct`; the latter measures whether the mechanical retrieval burden actually moved out of the frontier loop.

## Record a run

```bash
agentctl benchmark record \
  --study pilot \
  --case repo-001 \
  --arm B \
  --client claude \
  --model <frontier-model> \
  --accepted \
  --elapsed 42.3 \
  --input-tokens 12000 \
  --cache-read-tokens 30000 \
  --output-tokens 1800 \
  --frontier-subagents 2
```

For providers where reported input tokens already include cached input, add:

```text
--input-includes-cached
```

Then record the matching C run. For C, also record Gremlins behavior:

```bash
agentctl benchmark record \
  --study pilot \
  --case repo-001 \
  --arm C \
  --client claude \
  --accepted \
  --elapsed 31.2 \
  --input-tokens 5000 \
  --cache-read-tokens 8000 \
  --output-tokens 1000 \
  --gremlins-calls 1 \
  --gremlins-local-model-calls 0 \
  --gremlins-result-chars 4200
```

Set `--frontier-redid-search` when the benchmarked frontier client repeats the same broad retrieval after Gremlins. That is a key integration failure mode.

## Cost-weighted comparison

Do not treat uncached input, cache reads, cache writes, and output as equally expensive.

If you want cost comparison, pass the actual model's per-million token prices when recording the run:

```text
--price-input ...
--price-cache-read ...
--price-cache-write ...
--price-output ...
```

Gremlins intentionally does not hard-code model pricing.

## Report

```bash
agentctl benchmark report --study pilot
```

The report shows raw A/B/C totals plus paired A→C and B→C changes. Repeated runs are paired by `case_id + iteration`, not overwritten. The report includes aggregate token change and median per-pair change so a single unusually large run is visible rather than silently dominating the result. The B→C comparison is the measurement gate described in `architecture.md`.

## Data

Benchmark records are stored at:

```text
~/.local/state/gremlins/benchmarks/<study>.jsonl
```

They contain usage counts and result-size measurements, not prompts or repository contents.


## Controlled pilot loop

The repository includes ten starter cases in `evals/pilot/cases.json`.

The expected paths, exact search hints, and expected answer claims in that file are **hidden scoring/evaluation data**. They are not injected into the frontier C-arm prompt. In arm C the frontier orchestrator must choose the Gremlins `terms`/`symbols` itself from the task, which is the behavior the product actually depends on.

First verify the deterministic path itself:

```bash
agentctl benchmark pilot-local --repository .
```

For the real frontier experiment, the preferred path is the one-command paired suite:

```bash
agentctl benchmark suite \
  --study pilot-claude \
  --repository . \
  --client claude \
  --repeats 2
```

Run this from a clean checkout of this Gremlins repository; the bundled starter cases are intentionally specific to this repository. Replace `claude` with `codex` for the Codex study. Real-project corpora should be added separately rather than reusing these hidden expectations against unrelated repositories.

Every arm runs in a fresh clone of the same clean Git snapshot. B/C order is counterbalanced by case and repetition to reduce simple warm-cache/order bias. Each automated C invocation gets a unique measurement tag so rerunning a case cannot accidentally count older Gremlins events. Study metadata records the source commit, Gremlins commit, client version, requested model and ordering policy. Those provenance fields are immutable for a study: if the source, Gremlins code, client version, model, or A-arm policy changes, the suite refuses to append and requires a new study name (or an explicit clear).

Raw Claude/Codex transcripts remain opt-in with `--save-raw`.

For manual/stepwise operation, ask Gremlins for the next missing B/C run:

```bash
agentctl benchmark next --study pilot --repeats 2
```

Generate the controlled prompt:

```bash
agentctl benchmark prompt --case repo-001 --arm B --repository /path/to/repo
agentctl benchmark prompt --case repo-001 --arm C --repository /path/to/repo
```

The C prompt includes a Gremlins `measurement_tag`. Gremlins records its own calls under that tag. When recording the frontier run, use:

```bash
agentctl benchmark record \
  --study pilot \
  --case repo-001 \
  --arm C \
  --client claude \
  --accepted \
  --elapsed 31.2 \
  --input-tokens 5000 \
  --cache-read-tokens 8000 \
  --output-tokens 1000 \
  --gremlins-tag pilot:repo-001:C
```

The recorder then fills Gremlins call count, local-model call count, and Gremlins result characters from local job metrics.

## Import provider usage JSON

If you have a usage object saved as JSON, Gremlins can normalize it instead of requiring manual token fields.

OpenAI-style usage:

```bash
agentctl benchmark record ... \
  --usage-json usage.json \
  --usage-format openai
```

Anthropic-style usage:

```bash
agentctl benchmark record ... \
  --usage-json usage.json \
  --usage-format anthropic
```

For OpenAI-style usage, cached input is treated as a subset of reported input and is subtracted before Gremlins records uncached input. For Anthropic-style usage, `input_tokens`, cache-read tokens, and cache-creation tokens are recorded as separate categories.

## Execute the gate

After enough paired B/C cases:

```bash
agentctl benchmark gate --study pilot
```

Defaults:

- at least 10 paired B/C runs;
- at least 10 unique paired cases (repetitions cannot substitute for breadth);
- at least 30% reduction in frontier processed tokens;
- no more than a 5 percentage-point drop in acceptance;
- C-arm frontier redo rate no higher than 20%;
- redo-marker telemetry present for at least 80% of C runs;
- elapsed-time increase no higher than 20%.

These defaults are starting engineering thresholds, not permanent truths. They are CLI flags and can be changed for a study.


## Experimental fairness rules

The frontier experiment follows these rules:

- B and C use the same source Git commit.
- B and C use the same requested frontier model when one is specified.
- Each arm gets a fresh repository clone.
- Tuned B and C disable frontier subagents where the client supports an enforceable switch; arm A may use its normal subagent behavior.
- C receives the task but not the benchmark's hidden search hints or expected paths.
- C must choose Gremlins search terms/symbols itself.
- Gremlins may return deterministic evidence without local inference when that is sufficient.
- Result correctness is scored from hidden expected paths **and hidden answer-claim groups**; merely naming the right file is not enough, and local-model success alone is not treated as task success.
- Raw prompt/cache/output counters remain separate. Cache-read tokens are not treated as economically identical to uncached input.
- Repeated runs use distinct Gremlins measurement tags.
- The benchmark is read-only; source repositories must be clean before an automated run.
