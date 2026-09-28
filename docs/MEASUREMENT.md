# Gremlins measurement

Gremlins is caller-independent, so measurement has two layers.

## Capability-layer measurement

This is the product-level baseline and does not require an AI agent.

Measure:

- correctness and evidence coverage;
- deterministic recall;
- result size;
- latency;
- memory/CPU pressure;
- security/path-policy behavior;
- contract stability;
- whether optional local inference adds value over deterministic execution.

## Client-integration measurement

The existing A/B/C harness is an integration experiment for frontier coding agents.

Its question is:

> When a coding agent uses Gremlins, does the same developer task finish correctly with less frontier work than a well-tuned agent-only workflow?

Claude/Codex are benchmark clients here, not Gremlins dependencies.

Use three arms for the same case:

- **A** — current/frontier-heavy workflow.
- **B** — tuned frontier workflow without Gremlins.
- **C** — Gremlins-assisted workflow.

The primary comparison is **B versus C**.

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
