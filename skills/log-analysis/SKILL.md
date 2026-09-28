---
name: log-analysis
description: Extract and group failure evidence from logs while separating observed errors from inferred causes.
---

Extract failure-like lines and their immediate context first.
Group duplicate symptoms before reasoning about causes.
State whether a cause is directly proven or only plausible.
Return the smallest set of next checks that can disambiguate the cause.
