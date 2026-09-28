---
name: gremlins-delegation
description: Use local Gremlins tools for cheap read-only repository exploration, Git history, log/test triage, and mechanical evidence gathering before spawning a frontier subagent.
---

Use Gremlins when it can replace multiple frontier retrieval hops, not merely add another hop.

- For a single obvious exact lookup that one direct client search can answer, work directly instead of paying a Gremlins round-trip.
- Use `repo_search` for exact strings or symbols when a Gremlins search itself replaces broader frontier discovery.
- Use `code_read` for bounded excerpts instead of reading whole files.
- Use `git_history` for a single bounded commit-history lookup.
- Use `evidence_pack` when the task would otherwise require several search/read/history hops, when test↔source relationships matter, or when Claude/Codex is doing RCA and needs evidence for a hypothesis. The pack is deterministic, model-free, provenance-preserving, and repeatable. Let the caller decide what the evidence means.
- Use `repo_explorer` for narrower multi-term repository location work when a full cross-source evidence pack is unnecessary. Supply a small exact term/symbol set and use `mode="auto"`; auto is deterministic and never calls the local model.
- Do not use `repo_explorer mode="model"` in normal routing. The Mac model-value benchmark found only +3.33 percentage points mean quality for ~39.5x median latency and +33.9% median result size. Keep model mode only for manual experiments/benchmarking until stronger evidence changes that result.
- Use `failure_triage` with `path` when logs are in a repository; do not paste a large log into the parent context first. Mac reference measurement supports local synthesis for bounded triage: the broader 20-category × 3-repeat study improved mean quality by +27.92 percentage points, completed 60/60 model calls successfully, kept 20/20 cases stable, and produced zero regressions. Treat its output as bounded triage assistance, not Claude-level root-cause reasoning.

Pass a short evidence question and exact targets when known, not the parent conversation. For iterative RCA, it is expected to call `evidence_pack` multiple times as Claude/Codex refines hypotheses. Gremlins gathers evidence; it does not decide the root cause. Gremlins workers cannot spawn children and never modify the repository.

Deterministic `repo_explorer` results are ranked exact hits with compact hit-centered context. If `coverage.terms[TERM].all_returned` is true, Gremlins returned every exact match it found for that term; do not repeat the same broad search unless the task genuinely needs more context. If a result is `partial` or `needs-caller`, continue from the returned evidence rather than restarting discovery from scratch.
