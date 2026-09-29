#!/usr/bin/env bash
(
  set -euo pipefail

  REPEATS="${REPEATS:-2}"
  MODEL="${MODEL:-}"
  GREMLINS_STATE_DIR="${GREMLINS_STATE_DIR:-${RUNNER_TEMP:-/tmp}/gremlins-state}"
  export GREMLINS_STATE_DIR

  RUN_ID="${GITHUB_RUN_ID:-local}"
  RUN_ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"
  DIAG="public-diag-${RUN_ID}-${RUN_ATTEMPT}"
  STUDY="public-gate-${RUN_ID}-${RUN_ATTEMPT}"
  DIAG_DIR="${RUNNER_TEMP:-/tmp}/gremlins-diag"
  RESULT_DIR="$GREMLINS_STATE_DIR/results"

  mkdir -p "$DIAG_DIR" "$RESULT_DIR" "$GREMLINS_STATE_DIR"

  echo "===== SOURCE ====="
  git rev-parse HEAD
  git status --short --branch

  if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: benchmark source checkout must be clean"
    exit 1
  fi

  echo
  echo "===== LOCAL VALIDATION ====="
  uv run pytest -q
  uv run gremlins mcp-smoke
  uv run gremlins eval
  uv run gremlins benchmark evidence-service --repository .
  uv run gremlins benchmark pilot-local --repository .

  echo
  echo "===== CLAUDE AUTH ====="
  if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "ERROR: add repository secret ANTHROPIC_API_KEY (recommended) or CLAUDE_CODE_OAUTH_TOKEN"
    exit 1
  fi

  claude --version
  AUTH_OUTPUT="$(claude -p "Reply with exactly: AUTH_OK" --output-format text --max-turns 1)"
  if [ "$AUTH_OUTPUT" != "AUTH_OK" ]; then
    echo "ERROR: Claude authentication smoke failed"
    exit 1
  fi
  echo "CLAUDE_AUTH=PASS"

  echo
  echo "===== REGISTER GREMLINS MCP ====="
  mkdir -p "$HOME/.local/bin"
  cat > "$HOME/.local/bin/gremlins-mcp" <<EOF
#!/bin/sh
export GREMLINS_ROOT="$PWD"
exec "$PWD/.venv/bin/python" -m gremlins.server
EOF
  chmod 755 "$HOME/.local/bin/gremlins-mcp"

  claude mcp remove gremlins --scope user >/dev/null 2>&1 || true
  claude mcp add --scope user gremlins -- "$HOME/.local/bin/gremlins-mcp"
  claude mcp list

  run_diag_case() {
    local case_id="$1"
    local arm="$2"
    local rc=0
    local args=(
      .venv/bin/agentctl benchmark run
      --study "$DIAG"
      --repository .
      --client claude
      --case "$case_id"
      --arm "$arm"
      --iteration 1
    )

    if [ -n "$MODEL" ]; then
      args+=(--model "$MODEL")
    fi

    "${args[@]}" > "$DIAG_DIR/${case_id}-${arm}.json" || rc=$?

    # rc=1 means the structural scorer rejected the answer. Preserve that row
    # for diagnosis; rc>1 means the benchmark invocation itself failed.
    if [ "$rc" -gt 1 ]; then
      echo "ERROR: diagnostic execution failed for ${case_id}/${arm} rc=${rc}"
      exit "$rc"
    fi
  }

  echo
  echo "===== TREATMENT DIAGNOSTIC ====="
  run_diag_case repo-005 B
  run_diag_case repo-005 C
  run_diag_case repo-009 B
  run_diag_case repo-009 C

  python3 - "$DIAG_DIR" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
failures = []
summary = []

for case_id in ("repo-005", "repo-009"):
    row = json.loads((root / f"{case_id}-C.json").read_text())
    metadata = row.get("metadata") or {}
    gremlins = row.get("gremlins") or {}
    tool_names = metadata.get("tool_names") or []
    gremlins_tool_names = metadata.get("frontier_gremlins_tool_names") or []
    denials = metadata.get("permission_denials") or []
    calls = int(gremlins.get("calls") or 0)
    workers = gremlins.get("workers") or {}

    checks = {
        "accepted": bool(row.get("accepted")),
        "bounded_evidence_pack_calls": 1 <= calls <= 4,
        "no_local_model": int(gremlins.get("local_model_calls") or 0) == 0,
        "evidence_pack_worker_only": workers == {"evidence-pack": calls},
        "client_saw_same_gremlins_call_count": int(row.get("frontier_gremlins_tool_calls") or 0) == calls,
        "evidence_pack_tool_only": (
            len(gremlins_tool_names) == calls
            and all(name == "mcp__gremlins__evidence_pack" for name in gremlins_tool_names)
        ),
        "no_permission_denials": len(denials) == 0,
        "no_frontier_redo": row.get("frontier_redid_search") is False,
        "no_direct_frontier_evidence": int(row.get("frontier_direct_evidence_calls") or 0) == 0,
    }
    summary.append({"case_id": case_id, **checks})
    if not all(checks.values()):
        failures.append({"case_id": case_id, "checks": checks})

print(json.dumps(summary, indent=2))
if failures:
    print("Treatment diagnostic failed:", json.dumps(failures, indent=2), file=sys.stderr)
    raise SystemExit(1)

print("TREATMENT_DIAGNOSTIC=PASS")
PY

  echo
  echo "===== FULL 40-RUN GATE ====="
  suite_args=(
    .venv/bin/agentctl benchmark suite
    --study "$STUDY"
    --repository .
    --client claude
    --repeats "$REPEATS"
  )

  if [ -n "$MODEL" ]; then
    suite_args+=(--model "$MODEL")
  fi

  set +e
  "${suite_args[@]}" > "$RESULT_DIR/suite.json"
  SUITE_RC=$?
  set -e

  echo "SUITE_RC=$SUITE_RC"

  set +e
  .venv/bin/agentctl benchmark report --study "$STUDY" > "$RESULT_DIR/report.json"
  REPORT_RC=$?
  .venv/bin/agentctl benchmark gate --study "$STUDY" > "$RESULT_DIR/gate.json"
  GATE_RC=$?
  set -e

  echo "REPORT_RC=$REPORT_RC"
  echo "GATE_RC=$GATE_RC"

  python3 - "$RESULT_DIR/report.json" "$RESULT_DIR/gate.json" <<'PY'
import json
import pathlib
import sys

report_path = pathlib.Path(sys.argv[1])
gate_path = pathlib.Path(sys.argv[2])
report = json.loads(report_path.read_text()) if report_path.exists() and report_path.stat().st_size else {}
gate = json.loads(gate_path.read_text()) if gate_path.exists() and gate_path.stat().st_size else {}
comparison = report.get("comparison_B_to_C") or {}

lines = [
    "===== GATE SUMMARY =====",
    f"study={gate.get('study')}",
    f"pass={gate.get('pass')}",
    f"paired_cases={gate.get('paired_cases')}",
    f"unique_cases={gate.get('unique_cases')}",
    f"processed_token_change_pct={comparison.get('frontier_processed_tokens_change_pct')}",
    f"median_pair_token_change_pct={comparison.get('frontier_processed_tokens_median_pair_change_pct')}",
    f"direct_tool_calls_before={comparison.get('frontier_direct_tool_calls_before')}",
    f"direct_tool_calls_after={comparison.get('frontier_direct_tool_calls_after')}",
    f"direct_tool_calls_change_pct={comparison.get('frontier_direct_tool_calls_change_pct')}",
    f"direct_evidence_calls_before={comparison.get('frontier_direct_evidence_calls_before')}",
    f"direct_evidence_calls_after={comparison.get('frontier_direct_evidence_calls_after')}",
    f"direct_evidence_calls_change_pct={comparison.get('frontier_direct_evidence_calls_change_pct')}",
    f"acceptance_before={comparison.get('acceptance_rate_before')}",
    f"acceptance_after={comparison.get('acceptance_rate_after')}",
    f"redo_rate_after={comparison.get('redo_rate_after')}",
    f"elapsed_change_pct={comparison.get('elapsed_change_pct')}",
]
reasons = gate.get("reasons") or []
if reasons:
    lines.append("reasons=" + " | ".join(reasons))
else:
    lines.append("reasons=none")

print("\n".join(lines))

summary_path = pathlib.Path(__import__("os").environ.get("GITHUB_STEP_SUMMARY", ""))
if str(summary_path):
    summary_path.write_text(
        "# Gremlins frontier savings gate\n\n"
        + "\n".join(f"- {line}" for line in lines[1:])
        + "\n",
        encoding="utf-8",
    )
PY

  if [ "$SUITE_RC" -ne 0 ]; then
    echo "ERROR: benchmark suite had execution/setup errors"
    exit 1
  fi

  if [ "$REPORT_RC" -ne 0 ]; then
    echo "ERROR: benchmark report generation failed"
    exit 1
  fi

  if [ "$GATE_RC" -ne 0 ]; then
    echo "ERROR: measured B/C study did not pass the configured gate"
    exit 1
  fi

  echo "FRONTIER_GATE=PASS"
)
