---
name: gremlins-delegation
description: Use local Gremlins tools for cheap read-only repository exploration, Git history, log/test triage, and mechanical evidence gathering before spawning a frontier subagent.
---

Use Gremlins when it can replace multiple frontier retrieval hops, not merely add another hop.

- For a single obvious exact lookup that one direct client search can answer, work directly instead of paying a Gremlins round-trip.
- Use `repo_search` for exact strings or symbols when a Gremlins search itself replaces broader frontier discovery.
- Use `code_read` for bounded excerpts instead of reading whole files.
- Use `git_history` for commit-history evidence.
- Use `repo_explorer` for multi-term or multi-file repository location work that would otherwise require several search/read steps. Supply a small exact term/symbol set and use `mode="auto"`; auto is deterministic and never calls the local model.
- Use `mode="model"` only when local synthesis is explicitly useful. Do not trigger local inference just because a task says "how", "explain", or similar.
- Use `failure_triage` with `path` when logs are in a repository; do not paste a large log into the parent context first.

Pass a short task and exact targets, not the parent conversation. Gremlins workers cannot spawn children and never modify the repository.

Deterministic `repo_explorer` results are ranked exact hits with compact hit-centered context. If `coverage.terms[TERM].all_returned` is true, Gremlins returned every exact match it found for that term; do not repeat the same broad search unless the task genuinely needs more context. If a result is `partial` or `needs-caller`, continue from the returned evidence rather than restarting discovery from scratch.
