from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any

from .benchmark import benchmark_root
from .frontier_runner import (
    _claude_command,
    _claude_tool_uses,
    _ensure_clean_git_repository,
    _ensure_frontier_client_ready,
    _parse_claude_stream,
    _require_valid_frontier_run,
    _run_command,
)


EVIDENCE_SIGNALS = (
    "find", "search", "locate", "identify", "inspect", "read", "trace", "gather",
    "collect", "list", "references", "usages", "history", "git", "tests", "logs",
    "files", "symbols", "where", "evidence", "map",
)
BOUNDED_ANALYSIS_SIGNALS = (
    "summarize", "explain", "compare", "verify", "correlate", "classify",
    "distill", "relationship", "flow", "reconcile",
)
REASONING_SIGNALS = (
    "root cause", "why", "design", "architecture", "architectural", "plan",
    "trade-off", "tradeoff", "recommend", "fix", "implementation", "race",
    "correctness", "review", "hypothesis", "causal", "cause",
)


def observation_cases_path() -> Path:
    return Path(__file__).resolve().parents[2] / "evals" / "subagent-observation" / "cases.json"


def load_observation_cases() -> list[dict]:
    data = json.loads(observation_cases_path().read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("subagent observation corpus must be a JSON list")
    return [row for row in data if isinstance(row, dict)]


def classify_delegated_request(call: dict) -> dict:
    subagent_type = str(call.get("subagent_type") or "").strip()
    text = " ".join(
        str(call.get(key) or "")
        for key in ("description", "prompt")
    ).lower()

    evidence = sorted({signal for signal in EVIDENCE_SIGNALS if signal in text})
    bounded = sorted({signal for signal in BOUNDED_ANALYSIS_SIGNALS if signal in text})
    reasoning = sorted({signal for signal in REASONING_SIGNALS if signal in text})

    lowered_type = subagent_type.lower()
    if lowered_type == "explore":
        label = "evidence-acquisition"
    elif lowered_type == "plan":
        label = "claude-level-reasoning"
    elif lowered_type == "statusline-setup":
        label = "utility-or-out-of-scope"
    elif reasoning and evidence:
        label = "mixed-evidence-and-reasoning"
    elif reasoning:
        label = "claude-level-reasoning"
    elif evidence and bounded:
        label = "bounded-analysis"
    elif evidence:
        label = "evidence-acquisition"
    elif bounded:
        label = "bounded-analysis"
    else:
        label = "unknown"

    return {
        "label": label,
        "evidence_signals": evidence,
        "bounded_analysis_signals": bounded,
        "reasoning_signals": reasoning,
        "method": "auditable-keyword-heuristic-plus-known-agent-type",
        "note": (
            "This classifies the delegated request, not the hidden token/time split inside "
            "the spawned subagent. Review captured prompts before making product decisions."
        ),
    }


def _prepare_observation_workspace(
    source: Path,
    study: str,
    case_id: str,
    iteration: int,
) -> Path:
    root = benchmark_root() / "workspaces" / study
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / f"{case_id}-observe-r{iteration}"
    if workspace.exists():
        shutil.rmtree(workspace)

    proc = subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(workspace)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to create observation workspace: {proc.stderr.strip()}")

    sparse = subprocess.run(
        [
            "git", "-C", str(workspace), "sparse-checkout", "set", "--no-cone",
            "/*",
            "!/evals/",
            "!/skills/gremlins-delegation/",
            "!/docs/MEASUREMENT.md",
            "!/docs/MAC_REFERENCE.md",
            "!/README.md",
            "!/architecture.md",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if sparse.returncode != 0:
        raise RuntimeError(f"failed to isolate observation workspace: {sparse.stderr.strip()}")
    return workspace


def _observation_prompt(case: dict, workspace: Path) -> str:
    return (
        f"Repository: {workspace}\n"
        f"Task: {case['task']}\n\n"
        "Complete this read-only software-engineering task using your normal workflow. "
        "Use Claude subagents only if you would normally use them; do not spawn agents "
        "merely because this is an observation. "
        "Do not modify files. Give a concise evidence-backed final answer."
    )


def _save_raw(study: str, case_id: str, iteration: int, stdout: str, stderr: str) -> dict:
    directory = benchmark_root() / "raw" / study
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / f"{case_id}-observe-r{iteration}"
    stdout_path = base.with_suffix(".stdout.jsonl")
    stderr_path = base.with_suffix(".stderr.txt")
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return {"stdout": str(stdout_path), "stderr": str(stderr_path)}


def run_claude_subagent_observation(
    repository: str = ".",
    *,
    study: str = "mac-claude-subagents-v1",
    repeats: int = 1,
    case_ids: list[str] | None = None,
    model: str | None = None,
    timeout_seconds: int = 900,
) -> dict:
    if repeats < 1 or repeats > 3:
        raise ValueError("repeats must be between 1 and 3")

    _ensure_frontier_client_ready("claude")
    source = _ensure_clean_git_repository(repository)
    source_head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    client_version = subprocess.run(
        ["claude", "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    ).stdout.strip()

    cases = load_observation_cases()
    if case_ids:
        requested = set(case_ids)
        cases = [case for case in cases if str(case.get("id")) in requested]
        missing = requested - {str(case.get("id")) for case in cases}
        if missing:
            raise ValueError(f"unknown observation cases: {sorted(missing)}")
    if not cases:
        raise RuntimeError("no observation cases selected")

    gremlins_tools = [
        "mcp__gremlins__gremlins_status",
        "mcp__gremlins__repo_search",
        "mcp__gremlins__code_read",
        "mcp__gremlins__git_history",
        "mcp__gremlins__repo_explorer",
        "mcp__gremlins__failure_triage",
    ]

    runs: list[dict] = []
    all_calls: list[dict] = []
    execution_errors: list[dict] = []

    for iteration in range(1, repeats + 1):
        for case in cases:
            case_id = str(case["id"])
            workspace = _prepare_observation_workspace(source, study, case_id, iteration)
            prompt = _observation_prompt(case, workspace)
            command = _claude_command(
                prompt,
                model,
                allow_agents=True,
                disallowed_tools=gremlins_tools,
                permission_mode="plan",
                setting_sources="project",
            )
            started = time.monotonic()
            try:
                proc = _run_command(command, workspace, timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                execution_errors.append({
                    "case_id": case_id,
                    "iteration": iteration,
                    "error": f"TimeoutExpired: {exc}",
                })
                continue
            elapsed = time.monotonic() - started
            raw = _save_raw(study, case_id, iteration, proc.stdout, proc.stderr)
            parsed = _parse_claude_stream(proc.stdout, proc.returncode, elapsed)
            try:
                _require_valid_frontier_run("claude", parsed, proc.returncode, proc.stderr)
            except RuntimeError as exc:
                execution_errors.append({
                    "case_id": case_id,
                    "iteration": iteration,
                    "error": str(exc),
                    "raw": raw,
                })
                continue

            calls: list[dict] = []
            for index, call in enumerate(parsed.metadata.get("subagent_details") or [], start=1):
                enriched = {
                    "case_id": case_id,
                    "case_family": case.get("family"),
                    "iteration": iteration,
                    "call_index": index,
                    **call,
                    "classification": classify_delegated_request(call),
                }
                calls.append(enriched)
                all_calls.append(enriched)

            runs.append({
                "case_id": case_id,
                "family": case.get("family"),
                "iteration": iteration,
                "elapsed_seconds": round(elapsed, 3),
                "usage": parsed.usage.__dict__,
                "total_tool_calls": parsed.metadata.get("tool_calls"),
                "subagent_calls": len(calls),
                "subagents": calls,
                "response_excerpt": parsed.response_text[:1200],
                "raw": raw,
            })

    labels = Counter(
        str(call["classification"]["label"])
        for call in all_calls
    )
    types = Counter(
        str(call.get("subagent_type") or call.get("tool_name") or "unknown")
        for call in all_calls
    )
    families = Counter(str(run.get("family") or "unknown") for run in runs if run["subagent_calls"])
    total_calls = len(all_calls)
    evidence_only = labels.get("evidence-acquisition", 0)
    bounded = labels.get("bounded-analysis", 0)
    mixed = labels.get("mixed-evidence-and-reasoning", 0)
    reasoning = labels.get("claude-level-reasoning", 0)

    visible_nested = sum(
        1
        for call in all_calls
        if call.get("parent_tool_use_id") or (call.get("observed_depth") or 0) > 0
    )

    report = {
        "benchmark": "claude-subagent-observation-v1",
        "study": study,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_repository": str(source),
        "source_head": source_head,
        "client": "claude",
        "client_version": client_version,
        "requested_model": model,
        "corpus": {
            "cases": len(cases),
            "repeats": repeats,
            "runs_expected": len(cases) * repeats,
            "families": [str(case.get("family")) for case in cases],
        },
        "summary": {
            "runs_completed": len(runs),
            "execution_errors": len(execution_errors),
            "runs_with_subagents": sum(run["subagent_calls"] > 0 for run in runs),
            "total_subagent_calls": total_calls,
            "subagent_types": dict(types),
            "delegated_request_classes": dict(labels),
            "pure_evidence_call_fraction": round(evidence_only / total_calls, 4) if total_calls else None,
            "evidence_or_bounded_call_fraction": round((evidence_only + bounded) / total_calls, 4) if total_calls else None,
            "calls_with_evidence_component_fraction": round((evidence_only + bounded + mixed) / total_calls, 4) if total_calls else None,
            "claude_reasoning_call_fraction": round(reasoning / total_calls, 4) if total_calls else None,
            "mixed_call_fraction": round(mixed / total_calls, 4) if total_calls else None,
            "visible_nested_subagent_calls": visible_nested,
            "families_with_subagents": dict(families),
        },
        "runs": runs,
        "execution_errors": execution_errors,
        "interpretation": {
            "what_is_measured": (
                "Observed parent Claude stream Agent/Task calls, their declared subagent type, "
                "delegated prompt, and a transparent classification of the requested work. "
                "Claude runs with project-only settings so user-level Gremlins delegation guidance "
                "is not part of the observation treatment."
            ),
            "what_is_not_measured": (
                "The report does not claim exact token/time shares inside each spawned subagent. "
                "Nested subagent calls are counted only when Claude's stream exposes them."
            ),
            "product_use": (
                "Use evidence-acquisition and bounded-analysis calls as Gremlins opportunity "
                "candidates. Keep Claude-level reasoning with Claude; mixed calls are candidates "
                "for splitting evidence acquisition from reasoning rather than replacing the call wholesale."
            ),
        },
    }

    output_path = benchmark_root() / f"{study}.subagents.json"
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["report_file"] = str(output_path)
    return report


READ_ONLY_TOOL_NAMES = {
    "read",
    "grep",
    "glob",
    "search",
    "find",
    "toolsearch",
    "websearch",
    "webfetch",
    "ls",
}
READ_ONLY_BASH_RE = re.compile(
    r"(?:^|[;&|()]\s*)(?:rg|grep|find|ls|cat|sed|head|tail|wc|tree)\b"
    r"|(?:^|[;&|()]\s*)git\s+(?:log|show|blame|diff|status|grep|rev-parse)\b",
    re.IGNORECASE,
)


def _load_raw_events(path: Path) -> list[dict]:
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _root_tool_category(use: dict) -> str:
    name = str(use.get("name") or "").lower()
    if name in {"agent", "task"}:
        return "subagent"
    if use.get("parent_tool_use_id"):
        return "nested"
    if name in READ_ONLY_TOOL_NAMES:
        return "direct-evidence"
    if name == "bash":
        payload = use.get("input") if isinstance(use.get("input"), dict) else {}
        command = str(payload.get("command") or "")
        if READ_ONLY_BASH_RE.search(command):
            return "direct-evidence"
    return "other"


def _tool_preview(use: dict) -> str | None:
    payload = use.get("input") if isinstance(use.get("input"), dict) else {}
    for key in ("command", "pattern", "query", "file_path", "path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            compact = " ".join(value.strip().split())
            return compact[:300]
    return None


def analyze_claude_observation_raw(
    study: str = "mac-claude-subagents-v1",
    *,
    raw_dir: str | None = None,
) -> dict:
    directory = (
        Path(raw_dir).expanduser().resolve()
        if raw_dir
        else benchmark_root() / "raw" / study
    )
    if not directory.is_dir():
        raise RuntimeError(f"raw observation directory does not exist: {directory}")

    case_family = {
        str(case.get("id")): str(case.get("family") or "unknown")
        for case in load_observation_cases()
    }
    files = sorted(directory.glob("*.stdout.jsonl"))
    if not files:
        raise RuntimeError(f"no Claude raw stdout JSONL files found in {directory}")

    cases: list[dict] = []
    root_names: Counter[str] = Counter()
    evidence_names: Counter[str] = Counter()
    nested_names: Counter[str] = Counter()
    total_usage = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
    }
    total_cost = 0.0
    cost_known = 0
    total_root_tools = 0
    total_root_nonagent = 0
    total_direct_evidence = 0
    total_agent_calls = 0
    total_nested = 0
    reported_spawned = 0
    max_turn_results = 0
    successful_results = 0

    filename_re = re.compile(r"^(obs-\d+)-observe-r(\d+)\.stdout\.jsonl$")

    for path in files:
        match = filename_re.match(path.name)
        case_id = match.group(1) if match else path.stem
        iteration = int(match.group(2)) if match else 1
        events = _load_raw_events(path)
        final = next(
            (event for event in reversed(events) if event.get("type") == "result"),
            {},
        )
        uses = _claude_tool_uses(events)

        root_uses = [use for use in uses if not use.get("parent_tool_use_id")]
        nested_uses = [use for use in uses if use.get("parent_tool_use_id")]
        agent_uses = [
            use for use in uses
            if str(use.get("name") or "").lower() in {"agent", "task"}
        ]
        root_nonagent = [
            use for use in root_uses
            if str(use.get("name") or "").lower() not in {"agent", "task"}
        ]
        direct_evidence = [
            use for use in root_nonagent
            if _root_tool_category(use) == "direct-evidence"
        ]

        root_names.update(str(use.get("name") or "unknown") for use in root_uses)
        evidence_names.update(str(use.get("name") or "unknown") for use in direct_evidence)
        nested_names.update(str(use.get("name") or "unknown") for use in nested_uses)

        usage = final.get("usage") if isinstance(final.get("usage"), dict) else {}
        thinking = (
            usage.get("output_tokens_details")
            if isinstance(usage.get("output_tokens_details"), dict)
            else {}
        )
        normalized_usage = {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "thinking_tokens": int(thinking.get("thinking_tokens") or 0),
        }
        for key, value in normalized_usage.items():
            total_usage[key] += value

        cost = final.get("total_cost_usd")
        try:
            cost_value = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost_value = None
        if cost_value is not None:
            total_cost += cost_value
            cost_known += 1

        subtype = final.get("subtype")
        terminal_reason = final.get("terminal_reason")
        is_error = bool(final.get("is_error"))
        if subtype == "error_max_turns" or terminal_reason == "max_turns":
            max_turn_results += 1
        elif final and not is_error:
            successful_results += 1

        subagent_stats = (
            final.get("subagent_stats")
            if isinstance(final.get("subagent_stats"), dict)
            else {}
        )
        reported_spawned += int(subagent_stats.get("spawned") or 0)

        total_root_tools += len(root_uses)
        total_root_nonagent += len(root_nonagent)
        total_direct_evidence += len(direct_evidence)
        total_agent_calls += len(agent_uses)
        total_nested += len(nested_uses)

        cases.append({
            "case_id": case_id,
            "family": case_family.get(case_id, "unknown"),
            "iteration": iteration,
            "result": {
                "subtype": subtype,
                "terminal_reason": terminal_reason,
                "is_error": is_error,
                "num_turns": final.get("num_turns"),
                "cost_usd": cost_value,
                "usage": normalized_usage,
                "reported_subagent_stats": subagent_stats,
            },
            "tool_activity": {
                "root_tool_calls": len(root_uses),
                "root_nonagent_tool_calls": len(root_nonagent),
                "direct_evidence_tool_calls": len(direct_evidence),
                "direct_evidence_fraction_of_root_nonagent": (
                    round(len(direct_evidence) / len(root_nonagent), 4)
                    if root_nonagent else None
                ),
                "agent_calls": len(agent_uses),
                "nested_tool_calls": len(nested_uses),
                "root_tool_names": dict(Counter(
                    str(use.get("name") or "unknown") for use in root_uses
                )),
                "direct_evidence_tool_names": dict(Counter(
                    str(use.get("name") or "unknown") for use in direct_evidence
                )),
                "nested_tool_names": dict(Counter(
                    str(use.get("name") or "unknown") for use in nested_uses
                )),
                "direct_evidence_previews": [
                    {
                        "name": use.get("name"),
                        "preview": _tool_preview(use),
                    }
                    for use in direct_evidence
                ],
            },
            "raw_file": str(path),
        })

    runs_with_agents = sum(case["tool_activity"]["agent_calls"] > 0 for case in cases)
    direct_evidence_no_agent = sum(
        case["tool_activity"]["direct_evidence_tool_calls"] > 0
        and case["tool_activity"]["agent_calls"] == 0
        for case in cases
    )

    report = {
        "analysis": "claude-observation-raw-v1",
        "study": study,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "raw_directory": str(directory),
        "summary": {
            "runs_analyzed": len(cases),
            "successful_terminal_results": successful_results,
            "max_turn_results": max_turn_results,
            "runs_with_agents": runs_with_agents,
            "agent_calls_observed": total_agent_calls,
            "subagents_reported_spawned": reported_spawned,
            "nested_tool_calls_visible": total_nested,
            "root_tool_calls": total_root_tools,
            "root_nonagent_tool_calls": total_root_nonagent,
            "direct_evidence_tool_calls": total_direct_evidence,
            "direct_evidence_fraction_of_root_nonagent": (
                round(total_direct_evidence / total_root_nonagent, 4)
                if total_root_nonagent else None
            ),
            "runs_with_direct_evidence_and_no_agent": direct_evidence_no_agent,
            "total_cost_usd": round(total_cost, 6) if cost_known else None,
            "cost_known_runs": cost_known,
            "usage": total_usage,
            "root_tool_names": dict(root_names),
            "direct_evidence_tool_names": dict(evidence_names),
            "nested_tool_names": dict(nested_names),
        },
        "cases": cases,
        "interpretation": {
            "direct_evidence": (
                "Root-level Read/Grep/Glob/Search/Find/ToolSearch/WebSearch/WebFetch/ls "
                "plus read-only shell evidence commands such as rg/grep/find/ls/cat/sed/head/"
                "tail/wc/tree and git log/show/blame/diff/status/grep/rev-parse."
            ),
            "boundary": (
                "This analysis measures visible frontier tool activity from already-recorded "
                "Claude streams. It does not infer that all non-tool tokens are reasoning, and "
                "it does not claim hidden nested work that Claude did not emit."
            ),
            "product_use": (
                "High direct-evidence activity without Agent calls supports using Gremlins as "
                "an evidence service inside the parent Claude reasoning loop, not only as a "
                "replacement for spawned Explore agents."
            ),
        },
    }

    output_path = benchmark_root() / f"{study}.retrieval-analysis.json"
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["report_file"] = str(output_path)
    return report
