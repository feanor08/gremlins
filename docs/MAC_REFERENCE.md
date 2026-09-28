# Mac reference deployment

This document freezes the first measured Gremlins local-model reference profile.

## Reference stack

- platform: Apple Silicon macOS;
- Gremlins source used for the stability decision: `43a3ac29bae189719395cd954e58b03fab847763`;
- provider: Ollama on `http://127.0.0.1:11434`;
- model: `qwen3.5:4b`;
- profile: `mac-local`.

Ollama and the local model remain optional to Gremlins core.

## Routing

Normal reference routing is:

- `repo-search`: deterministic;
- `code-read`: deterministic;
- `git-history`: deterministic;
- normal `repo-explorer`: deterministic;
- `repo-explorer mode=model`: experiment/benchmark only;
- triage evidence extraction: deterministic;
- bounded triage grouping / directly-supported likely-cause explanation / next checks: local `qwen3.5:4b`;
- ambiguous debugging, architecture, trade-offs, patch design, subtle review: caller/frontier model.

There is no cloud fallback inside Gremlins.

## Evidence

The repo-explorer model-value study did not justify local synthesis for normal routing:

- deterministic quality 0.95;
- model quality 0.9833;
- +3.33 percentage points;
- ~39.5x median latency;
- +33.9% median result size.

The triage stability study justified local synthesis:

- 20 failure categories;
- 3 repeats each;
- 60/60 successful local-model calls;
- deterministic mean quality 0.6375;
- local-model mean quality 0.9167;
- +27.92 percentage points mean quality gain;
- 20/20 stable cases;
- 19/20 cases at or above 0.75 mean model quality;
- 0/20 regressed cases;
- median local-model latency 5.2553 s;
- p95 latency 7.315 s;
- 14,955 prompt-eval tokens;
- 9,921 eval/output tokens.

## THG reproduction rule

THG should reproduce the same capability/task/result contracts and routing behavior before any provider optimization.

Provider/runtime changes are allowed only as measured substitutions. A THG node may use a different runtime or model later, but it must re-run the same triage stability benchmark and preserve or improve the reference behavior before activation.

Do not move repository retrieval remote merely because inference moves to another node. Keep deterministic repository evidence gathering local to the caller/repository unless a separate measured use case justifies changing that boundary.
