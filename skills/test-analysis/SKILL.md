---
name: test-analysis
description: Analyze test failures, deduplicate related failures, and identify actionable next checks from supplied test output.
---

Prefer structured test output when available.
Separate the first causal failure from cascaded failures.
Do not claim code correctness from a single passing test.
Return failed tests, shared signatures, likely causes, and next checks.
