from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import os
import subprocess
import tempfile
from statistics import median
from typing import Iterable


ARMS = {"A", "B", "C"}


def benchmark_root() -> Path:
    base = Path(os.environ.get("GREMLINS_STATE_DIR", "~/.local/state/gremlins")).expanduser()
    path = base / "benchmarks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_study_name(study: str) -> str:
    safe = "".join(ch for ch in study if ch.isalnum() or ch in "-_.").strip(".")
    if not safe:
        raise ValueError("study name must contain letters, numbers, dash, underscore, or dot")
    return safe


def study_path(study: str) -> Path:
    return benchmark_root() / f"{_safe_study_name(study)}.jsonl"


def study_metadata_path(study: str) -> Path:
    return benchmark_root() / f"{_safe_study_name(study)}.meta.json"


def load_study_metadata(study: str) -> dict:
    path = study_metadata_path(study)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_study_metadata(study: str, payload: dict) -> Path:
    path = study_metadata_path(study)
    existing = load_study_metadata(study)
    merged = {**existing, **payload, "updated_at": datetime.now(timezone.utc).isoformat()}
    if "created_at" not in merged:
        merged["created_at"] = merged["updated_at"]
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def assert_study_compatible(study: str, payload: dict) -> None:
    existing = load_study_metadata(study)
    if not existing:
        return
    immutable = (
        "client",
        "client_version",
        "requested_model",
        "source_head",
        "gremlins_head",
        "include_a",
        "claude_tool_search",
        "claude_ai_mcp_servers",
    )
    mismatches = {
        key: {"existing": existing.get(key), "requested": payload.get(key)}
        for key in immutable
        if existing.get(key) != payload.get(key)
    }
    if mismatches:
        detail = "; ".join(
            f"{key}: {values['existing']!r} != {values['requested']!r}"
            for key, values in mismatches.items()
        )
        raise RuntimeError(
            f"benchmark study {study!r} cannot mix different provenance ({detail}); "
            "use a new --study name or clear the existing study"
        )


@dataclass(frozen=True)
class FrontierUsage:
    uncached_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_processed_tokens(self) -> int:
        return (
            self.uncached_input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )


@dataclass(frozen=True)
class Prices:
    uncached_input_per_million: float | None = None
    cache_read_per_million: float | None = None
    cache_write_per_million: float | None = None
    output_per_million: float | None = None

    def cost_usd(self, usage: FrontierUsage) -> float | None:
        values = (
            self.uncached_input_per_million,
            self.cache_read_per_million,
            self.cache_write_per_million,
            self.output_per_million,
        )
        if all(value is None for value in values):
            return None
        return (
            usage.uncached_input_tokens * (self.uncached_input_per_million or 0.0)
            + usage.cache_read_tokens * (self.cache_read_per_million or 0.0)
            + usage.cache_write_tokens * (self.cache_write_per_million or 0.0)
            + usage.output_tokens * (self.output_per_million or 0.0)
        ) / 1_000_000.0


@dataclass(frozen=True)
class BenchmarkRecord:
    case_id: str
    arm: str
    client: str
    model: str | None
    accepted: bool
    elapsed_seconds: float
    frontier_usage: FrontierUsage
    frontier_subagents: int | None = None
    frontier_redid_search: bool | None = None
    frontier_direct_tool_calls: int | None = None
    frontier_direct_evidence_calls: int | None = None
    gremlins_calls: int = 0
    gremlins_local_model_calls: int = 0
    gremlins_result_chars: int = 0
    notes: str = ""
    cost_usd: float | None = None
    iteration: int = 1

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
        return payload


def normalize_usage(
    *,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    input_includes_cached: bool = False,
) -> FrontierUsage:
    input_tokens = max(0, int(input_tokens))
    cached_input_tokens = max(0, int(cached_input_tokens))
    cache_write_tokens = max(0, int(cache_write_tokens))
    output_tokens = max(0, int(output_tokens))
    uncached = max(0, input_tokens - cached_input_tokens) if input_includes_cached else input_tokens
    return FrontierUsage(
        uncached_input_tokens=uncached,
        cache_read_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=output_tokens,
    )


def append_record(study: str, record: BenchmarkRecord) -> Path:
    if record.arm not in ARMS:
        raise ValueError(f"arm must be one of {sorted(ARMS)}")
    path = study_path(study)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.as_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def load_records(study: str) -> list[dict]:
    path = study_path(study)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _sum_usage(rows: Iterable[dict]) -> dict:
    totals = {
        "uncached_input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "processed_tokens": 0,
    }
    for row in rows:
        usage = row.get("frontier_usage") or {}
        for key in ("uncached_input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"):
            totals[key] += int(usage.get(key) or 0)
        totals["processed_tokens"] += sum(
            int(usage.get(key) or 0)
            for key in ("uncached_input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
        )
    return totals


def _safe_pct_change(before: float, after: float) -> float | None:
    if before == 0:
        return None
    return round(((after - before) / before) * 100.0, 2)


def _arm_summary(rows: list[dict]) -> dict:
    if not rows:
        return {
            "runs": 0,
            "accepted": 0,
            "acceptance_rate": None,
            "frontier_usage": _sum_usage([]),
            "frontier_subagents": 0,
            "frontier_redo_rate": None,
            "frontier_direct_tool_calls": None,
            "frontier_direct_evidence_calls": None,
            "frontier_direct_tool_calls_known_runs": 0,
            "frontier_direct_evidence_calls_known_runs": 0,
            "elapsed_seconds": 0.0,
            "cost_usd": None,
            "gremlins_calls": 0,
            "gremlins_local_model_calls": 0,
            "gremlins_result_chars": 0,
        }

    accepted = sum(bool(row.get("accepted")) for row in rows)
    redo_values = [bool(row["frontier_redid_search"]) for row in rows if row.get("frontier_redid_search") is not None]
    subagent_values = [int(row["frontier_subagents"]) for row in rows if row.get("frontier_subagents") is not None]
    direct_tool_values = [
        int(row["frontier_direct_tool_calls"])
        for row in rows
        if row.get("frontier_direct_tool_calls") is not None
    ]
    direct_evidence_values = [
        int(row["frontier_direct_evidence_calls"])
        for row in rows
        if row.get("frontier_direct_evidence_calls") is not None
    ]
    costs = [float(row["cost_usd"]) for row in rows if row.get("cost_usd") is not None]
    return {
        "runs": len(rows),
        "accepted": accepted,
        "acceptance_rate": round(accepted / len(rows), 4),
        "frontier_usage": _sum_usage(rows),
        "frontier_subagents": sum(subagent_values) if subagent_values else None,
        "frontier_subagents_known_runs": len(subagent_values),
        "frontier_redo_rate": round(sum(redo_values) / len(redo_values), 4) if redo_values else None,
        "frontier_redo_known_runs": len(redo_values),
        "frontier_direct_tool_calls": sum(direct_tool_values) if direct_tool_values else None,
        "frontier_direct_evidence_calls": sum(direct_evidence_values) if direct_evidence_values else None,
        "frontier_direct_tool_calls_known_runs": len(direct_tool_values),
        "frontier_direct_evidence_calls_known_runs": len(direct_evidence_values),
        "elapsed_seconds": round(sum(float(row.get("elapsed_seconds") or 0.0) for row in rows), 3),
        "cost_usd": round(sum(costs), 6) if costs else None,
        "gremlins_calls": sum(int(row.get("gremlins_calls") or 0) for row in rows),
        "gremlins_local_model_calls": sum(int(row.get("gremlins_local_model_calls") or 0) for row in rows),
        "gremlins_result_chars": sum(int(row.get("gremlins_result_chars") or 0) for row in rows),
    }


def _row_usage_total(row: dict) -> int:
    usage = row.get("frontier_usage") or {}
    return sum(
        int(usage.get(key) or 0)
        for key in ("uncached_input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
    )


def _paired(rows: list[dict], before_arm: str, after_arm: str) -> dict:
    by_run: dict[tuple[str, int], dict[str, dict]] = {}
    for row in rows:
        arm = row.get("arm")
        if arm not in {before_arm, after_arm}:
            continue
        key = (str(row.get("case_id")), int(row.get("iteration") or 1))
        by_run.setdefault(key, {})[arm] = row
    complete = [(key, pair) for key, pair in by_run.items() if before_arm in pair and after_arm in pair]

    before = [pair[before_arm] for _, pair in complete]
    after = [pair[after_arm] for _, pair in complete]
    before_summary = _arm_summary(before)
    after_summary = _arm_summary(after)

    before_usage = before_summary["frontier_usage"]
    after_usage = after_summary["frontier_usage"]
    pair_details: list[dict] = []
    token_changes: list[float] = []
    elapsed_changes: list[float] = []
    cost_changes: list[float] = []

    for (case_id, iteration), pair in complete:
        b = pair[before_arm]
        a = pair[after_arm]
        token_change = _safe_pct_change(float(_row_usage_total(b)), float(_row_usage_total(a)))
        elapsed_change = _safe_pct_change(
            float(b.get("elapsed_seconds") or 0.0),
            float(a.get("elapsed_seconds") or 0.0),
        )
        cost_change = None
        if b.get("cost_usd") is not None and a.get("cost_usd") is not None:
            cost_change = _safe_pct_change(float(b["cost_usd"]), float(a["cost_usd"]))
        if token_change is not None:
            token_changes.append(token_change)
        if elapsed_change is not None:
            elapsed_changes.append(elapsed_change)
        if cost_change is not None:
            cost_changes.append(cost_change)
        pair_details.append({
            "case_id": case_id,
            "iteration": iteration,
            "accepted_before": bool(b.get("accepted")),
            "accepted_after": bool(a.get("accepted")),
            "frontier_processed_tokens_before": _row_usage_total(b),
            "frontier_processed_tokens_after": _row_usage_total(a),
            "frontier_processed_tokens_change_pct": token_change,
            "elapsed_change_pct": elapsed_change,
            "frontier_cost_change_pct": cost_change,
            "frontier_redid_search_after": a.get("frontier_redid_search"),
            "frontier_direct_tool_calls_before": b.get("frontier_direct_tool_calls"),
            "frontier_direct_tool_calls_after": a.get("frontier_direct_tool_calls"),
            "frontier_direct_tool_calls_change_pct": (
                _safe_pct_change(
                    float(b["frontier_direct_tool_calls"]),
                    float(a["frontier_direct_tool_calls"]),
                )
                if b.get("frontier_direct_tool_calls") is not None
                and a.get("frontier_direct_tool_calls") is not None
                else None
            ),
            "frontier_direct_evidence_calls_before": b.get("frontier_direct_evidence_calls"),
            "frontier_direct_evidence_calls_after": a.get("frontier_direct_evidence_calls"),
            "frontier_direct_evidence_calls_change_pct": (
                _safe_pct_change(
                    float(b["frontier_direct_evidence_calls"]),
                    float(a["frontier_direct_evidence_calls"]),
                )
                if b.get("frontier_direct_evidence_calls") is not None
                and a.get("frontier_direct_evidence_calls") is not None
                else None
            ),
            "gremlins_calls_after": int(a.get("gremlins_calls") or 0),
            "gremlins_local_model_calls_after": int(a.get("gremlins_local_model_calls") or 0),
            "gremlins_result_chars_after": int(a.get("gremlins_result_chars") or 0),
        })

    comparison = {
        "paired_cases": len(complete),
        "unique_cases": len({case_id for (case_id, _), _pair in complete}),
        "before_arm": before_arm,
        "after_arm": after_arm,
        "frontier_processed_tokens_change_pct": _safe_pct_change(
            float(before_usage["processed_tokens"]), float(after_usage["processed_tokens"])
        ),
        "frontier_processed_tokens_median_pair_change_pct": round(median(token_changes), 2) if token_changes else None,
        "frontier_uncached_input_change_pct": _safe_pct_change(
            float(before_usage["uncached_input_tokens"]), float(after_usage["uncached_input_tokens"])
        ),
        "frontier_cache_read_change_pct": _safe_pct_change(
            float(before_usage["cache_read_tokens"]), float(after_usage["cache_read_tokens"])
        ),
        "frontier_output_change_pct": _safe_pct_change(
            float(before_usage["output_tokens"]), float(after_usage["output_tokens"])
        ),
        "frontier_subagents_change_pct": (
            _safe_pct_change(
                float(before_summary["frontier_subagents"]), float(after_summary["frontier_subagents"])
            )
            if before_summary["frontier_subagents"] is not None and after_summary["frontier_subagents"] is not None
            else None
        ),
        "frontier_direct_tool_calls_change_pct": (
            _safe_pct_change(
                float(before_summary["frontier_direct_tool_calls"]),
                float(after_summary["frontier_direct_tool_calls"]),
            )
            if before_summary["frontier_direct_tool_calls"] is not None
            and after_summary["frontier_direct_tool_calls"] is not None
            else None
        ),
        "frontier_direct_evidence_calls_change_pct": (
            _safe_pct_change(
                float(before_summary["frontier_direct_evidence_calls"]),
                float(after_summary["frontier_direct_evidence_calls"]),
            )
            if before_summary["frontier_direct_evidence_calls"] is not None
            and after_summary["frontier_direct_evidence_calls"] is not None
            else None
        ),
        "frontier_direct_tool_calls_before": before_summary["frontier_direct_tool_calls"],
        "frontier_direct_tool_calls_after": after_summary["frontier_direct_tool_calls"],
        "frontier_direct_evidence_calls_before": before_summary["frontier_direct_evidence_calls"],
        "frontier_direct_evidence_calls_after": after_summary["frontier_direct_evidence_calls"],
        "elapsed_change_pct": _safe_pct_change(
            float(before_summary["elapsed_seconds"]), float(after_summary["elapsed_seconds"])
        ),
        "elapsed_median_pair_change_pct": round(median(elapsed_changes), 2) if elapsed_changes else None,
        "acceptance_rate_before": before_summary["acceptance_rate"],
        "acceptance_rate_after": after_summary["acceptance_rate"],
        "redo_rate_after": after_summary["frontier_redo_rate"],
        "redo_known_fraction_after": (
            round(after_summary["frontier_redo_known_runs"] / len(after), 4)
            if after else None
        ),
        "pairs": pair_details,
    }
    if before_summary["cost_usd"] is not None and after_summary["cost_usd"] is not None:
        comparison["frontier_cost_change_pct"] = _safe_pct_change(
            float(before_summary["cost_usd"]), float(after_summary["cost_usd"])
        )
        comparison["frontier_cost_median_pair_change_pct"] = round(median(cost_changes), 2) if cost_changes else None
        comparison["frontier_cost_before_usd"] = before_summary["cost_usd"]
        comparison["frontier_cost_after_usd"] = after_summary["cost_usd"]
    else:
        comparison["frontier_cost_change_pct"] = None
        comparison["frontier_cost_median_pair_change_pct"] = None
        comparison["frontier_cost_before_usd"] = None
        comparison["frontier_cost_after_usd"] = None
    return comparison


def build_benchmark_report(study: str) -> dict:
    rows = load_records(study)
    by_arm = {arm: _arm_summary([row for row in rows if row.get("arm") == arm]) for arm in sorted(ARMS)}
    return {
        "study": study,
        "records": len(rows),
        "by_arm": by_arm,
        "comparison_B_to_C": _paired(rows, "B", "C"),
        "comparison_A_to_C": _paired(rows, "A", "C"),
        "data_file": str(study_path(study)),
        "metadata_file": str(study_metadata_path(study)),
        "interpretation": {
            "primary_comparison": "B_to_C",
            "reason": "B isolates frontier workflow improvements so local inference is not credited for deterministic retrieval alone.",
            "cost_note": "Cost is reported only when explicit per-million token prices were supplied with records.",
        },
    }


def pilot_cases_path() -> Path:
    from .config import project_root
    return project_root() / "evals" / "pilot" / "cases.json"


def load_pilot_cases() -> list[dict]:
    data = json.loads(pilot_cases_path().read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("pilot cases must be a JSON array")
    return [item for item in data if isinstance(item, dict)]


def run_local_pilot(repository: str = ".", case_id: str | None = None) -> dict:
    from .config import load_config
    from .workers import repo_explore

    config = load_config()
    cases = load_pilot_cases()
    if case_id is not None:
        cases = [case for case in cases if str(case.get("id")) == case_id]
        if not cases:
            raise ValueError(f"unknown pilot case: {case_id}")
    results: list[dict] = []
    passed = 0
    source = Path(repository).expanduser().resolve()

    # Run the deterministic smoke pilot against the same answer-isolated tree
    # used by frontier benchmarking. The case definitions stay in the source
    # checkout for scoring, but are absent from the searchable worktree.
    with tempfile.TemporaryDirectory(prefix=".gremlins-pilot-", dir=str(source.parent)) as temp_dir:
        workspace = Path(temp_dir) / "repo"
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(workspace)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if clone.returncode != 0:
            raise RuntimeError(f"failed to create local pilot workspace: {clone.stderr.strip()}")
        sparse = subprocess.run(
            ["git", "-C", str(workspace), "sparse-checkout", "set", "--no-cone", "/*", "!/evals/"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if sparse.returncode != 0:
            raise RuntimeError(f"failed to isolate local pilot answer data: {sparse.stderr.strip()}")

        for case in cases:
            result = repo_explore(
                str(workspace),
                str(case["task"]),
                config,
                terms=[str(value) for value in case.get("terms", [])],
                symbols=[str(value) for value in case.get("symbols", [])],
                mode="deterministic",
            )
            returned_items = (
                result.get("files")
                if isinstance(result.get("files"), list)
                else result.get("evidence", [])
            )
            returned_paths = {
                str(item.get("path"))
                for item in returned_items
                if isinstance(item, dict)
            }
            expected_paths = {str(path) for path in case.get("expected_paths", [])}
            missing = sorted(expected_paths - returned_paths)
            model_called = bool((result.get("usage") or {}).get("local_model_called"))
            result_chars = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            deterministic_budget = min(config.limits.max_result_evidence_chars, 3000)
            ok = not missing and not model_called and result_chars <= deterministic_budget
            if ok:
                passed += 1
            results.append({
                "id": case.get("id"),
                "ok": ok,
                "status": result.get("status"),
                "missing_expected_paths": missing,
                "returned_paths": sorted(returned_paths),
                "local_model_called": model_called,
                "result_chars": result_chars,
                "evidence_returned": result.get("hits_returned", result.get("evidence_returned")),
                "evidence_total": result.get("hits_ranked", result.get("evidence_total")),
            })

    return {
        "repository": repository,
        "cases": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "result_evidence_budget_chars": min(config.limits.max_result_evidence_chars, 3000),
        "results": results,
    }


def parse_usage_payload(payload: dict, usage_format: str) -> FrontierUsage:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
    if not isinstance(usage, dict):
        raise ValueError("usage payload must be a JSON object or contain a usage object")

    if usage_format == "openai":
        details = usage.get("input_tokens_details") or {}
        cached = int(details.get("cached_tokens") or usage.get("cached_input_tokens") or 0)
        return normalize_usage(
            input_tokens=int(usage.get("input_tokens") or 0),
            cached_input_tokens=cached,
            output_tokens=int(usage.get("output_tokens") or 0),
            input_includes_cached=True,
        )

    if usage_format == "anthropic":
        cache_write = int(
            usage.get("cache_creation_input_tokens")
            or usage.get("cache_write_input_tokens")
            or 0
        )
        return FrontierUsage(
            uncached_input_tokens=max(0, int(usage.get("input_tokens") or usage.get("uncached_input_tokens") or 0)),
            cache_read_tokens=max(0, int(usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0)),
            cache_write_tokens=max(0, cache_write),
            output_tokens=max(0, int(usage.get("output_tokens") or 0)),
        )

    if usage_format == "normalized":
        return FrontierUsage(
            uncached_input_tokens=max(0, int(usage.get("uncached_input_tokens") or 0)),
            cache_read_tokens=max(0, int(usage.get("cache_read_tokens") or 0)),
            cache_write_tokens=max(0, int(usage.get("cache_write_tokens") or 0)),
            output_tokens=max(0, int(usage.get("output_tokens") or 0)),
        )

    raise ValueError("usage format must be openai, anthropic, or normalized")


def evaluate_gate(
    study: str,
    *,
    min_pairs: int = 10,
    min_unique_cases: int = 10,
    min_frontier_reduction_pct: float = 30.0,
    max_acceptance_drop: float = 0.05,
    max_redo_rate: float = 0.20,
    min_redo_coverage: float = 0.80,
    max_elapsed_increase_pct: float = 20.0,
) -> dict:
    report = build_benchmark_report(study)
    comparison = report["comparison_B_to_C"]
    reasons: list[str] = []

    pairs = int(comparison.get("paired_cases") or 0)
    unique_cases = int(comparison.get("unique_cases") or 0)
    if pairs < min_pairs:
        reasons.append(f"need at least {min_pairs} paired B/C runs; have {pairs}")
    if unique_cases < min_unique_cases:
        reasons.append(f"need at least {min_unique_cases} unique paired cases; have {unique_cases}")

    token_change = comparison.get("frontier_processed_tokens_change_pct")
    if token_change is None or token_change > -abs(min_frontier_reduction_pct):
        reasons.append(
            f"frontier processed-token reduction must be at least {min_frontier_reduction_pct:.1f}%"
        )

    before_accept = comparison.get("acceptance_rate_before")
    after_accept = comparison.get("acceptance_rate_after")
    if before_accept is None or after_accept is None:
        reasons.append("acceptance rates are unavailable")
    elif float(after_accept) < float(before_accept) - max_acceptance_drop:
        reasons.append(
            f"acceptance-rate drop exceeds {max_acceptance_drop:.3f}"
        )

    redo_rate = comparison.get("redo_rate_after")
    redo_coverage = comparison.get("redo_known_fraction_after")
    if redo_coverage is None or float(redo_coverage) < min_redo_coverage:
        reasons.append(f"C-arm redo telemetry coverage is below {min_redo_coverage:.3f}")
    if redo_rate is None or float(redo_rate) > max_redo_rate:
        reasons.append(f"C-arm frontier redo rate exceeds {max_redo_rate:.3f}")

    elapsed_change = comparison.get("elapsed_change_pct")
    if elapsed_change is not None and float(elapsed_change) > max_elapsed_increase_pct:
        reasons.append(
            f"elapsed-time increase exceeds {max_elapsed_increase_pct:.1f}%"
        )

    cost_change = comparison.get("frontier_cost_change_pct")
    return {
        "study": study,
        "pass": not reasons,
        "reasons": reasons,
        "paired_cases": pairs,
        "unique_cases": unique_cases,
        "observed": {
            "frontier_processed_tokens_change_pct": token_change,
            "frontier_cost_change_pct": cost_change,
            "frontier_direct_tool_calls_change_pct": comparison.get("frontier_direct_tool_calls_change_pct"),
            "frontier_direct_evidence_calls_change_pct": comparison.get("frontier_direct_evidence_calls_change_pct"),
            "acceptance_rate_before": before_accept,
            "acceptance_rate_after": after_accept,
            "redo_rate_after": redo_rate,
            "redo_known_fraction_after": redo_coverage,
            "elapsed_change_pct": elapsed_change,
        },
        "thresholds": {
            "min_pairs": min_pairs,
            "min_unique_cases": min_unique_cases,
            "min_frontier_reduction_pct": min_frontier_reduction_pct,
            "max_acceptance_drop": max_acceptance_drop,
            "max_redo_rate": max_redo_rate,
            "min_redo_coverage": min_redo_coverage,
            "max_elapsed_increase_pct": max_elapsed_increase_pct,
        },
        "note": (
            "This gate uses B versus C. Cost is informative when explicit prices were recorded; "
            "processed tokens remain separately visible. Direct frontier tool/evidence-call reductions "
            "are reported for the evidence-loop treatment but are not yet pass criteria until the first paired study."
        ),
    }


def gremlins_stats_for_tag(tag: str) -> dict:
    from .metrics import events_for_measurement_tag

    rows = events_for_measurement_tag(tag)
    return {
        "tag": tag,
        "calls": len(rows),
        "local_model_calls": sum(
            bool((row.get("usage") or {}).get("local_model_called"))
            for row in rows
        ),
        "result_chars": sum(int(row.get("result_chars") or 0) for row in rows),
        "elapsed_seconds": round(sum(float(row.get("elapsed_seconds") or 0.0) for row in rows), 3),
        "statuses": {
            status: sum(1 for row in rows if row.get("status") == status)
            for status in sorted({str(row.get("status", "unknown")) for row in rows})
        },
        "workers": {
            worker: sum(1 for row in rows if str(row.get("worker", "unknown")) == worker)
            for worker in sorted({str(row.get("worker", "unknown")) for row in rows})
        },
        "evidence_pack_details": [
            str(row.get("detail"))
            for row in rows
            if str(row.get("worker", "")) == "evidence-pack" and row.get("detail")
        ],
        "evidence_pack_budgets": [
            int(row.get("result_budget_chars") or 0)
            for row in rows
            if str(row.get("worker", "")) == "evidence-pack"
        ],
    }


def get_pilot_case(case_id: str) -> dict:
    for case in load_pilot_cases():
        if str(case.get("id")) == case_id:
            return case
    raise ValueError(f"unknown pilot case: {case_id}")


def next_missing_run(study: str, include_a: bool = False, repeats: int = 1) -> dict | None:
    rows = load_records(study)
    seen = {
        (str(row.get("case_id")), str(row.get("arm")), int(row.get("iteration") or 1))
        for row in rows
    }
    arms = ["A", "B", "C"] if include_a else ["B", "C"]
    for iteration in range(1, max(1, int(repeats)) + 1):
        for case in load_pilot_cases():
            case_id = str(case.get("id"))
            for arm in arms:
                if (case_id, arm, iteration) not in seen:
                    return {"case": case, "arm": arm, "iteration": iteration}
    return None


def build_arm_prompt(
    case_id: str,
    arm: str,
    repository: str = ".",
    iteration: int = 1,
    measurement_tag: str | None = None,
) -> dict:
    if arm not in ARMS:
        raise ValueError(f"arm must be one of {sorted(ARMS)}")
    case = get_pilot_case(case_id)
    task = str(case["task"])
    terms = [str(value) for value in case.get("terms", [])]
    symbols = [str(value) for value in case.get("symbols", [])]
    tag = measurement_tag or f"pilot:{case_id}:{arm}:r{max(1, int(iteration))}"

    common = (
        f"Repository: {repository}\n"
        f"Task: {task}\n\n"
        "Finish the task and give a concise evidence-backed answer. "
        "In the final answer, name the exact repository-relative path(s) supporting each concrete claim or value; "
        "the benchmark scorer checks those paths explicitly. "
        "Do not modify the repository. Do not read unrelated files."
    )

    if arm == "A":
        instructions = (
            "This is benchmark arm A (existing/frontier-heavy workflow). "
            "Do not use Gremlins. Otherwise use your normal coding-agent workflow, including subagents if you normally would."
        )
    elif arm == "B":
        instructions = (
            "This is benchmark arm B (tuned frontier-only workflow). "
            "Do not use Gremlins and do not spawn subagents. "
            "Use only the native read-only repository tools Read, Grep, and Glob. "
            "Bash, web tools, write/edit tools, Gremlins, and subagents are unavailable in this arm. "
            "Keep retrieved context narrow and avoid repeating searches."
        )
    else:
        instructions = (
            "This is benchmark arm C (Gremlins evidence-loop workflow). "
            "Do not spawn subagents. Use Gremlins evidence_pack as your repository evidence interface before doing direct repository retrieval. "
            "In Claude Code this tool is named mcp__gremlins__evidence_pack; in other MCP clients use the equivalent evidence_pack tool exposed by the Gremlins server. "
            "You MAY call evidence_pack repeatedly as your reasoning develops, but make no more than four Gremlins calls in this arm. "
            "Use evidence_pack as the only Gremlins tool for this arm; do not call repo_explorer, repo_search, code_read, git_history, status, or failure_triage. "
            f"On every evidence_pack call pass repository='{repository}'. "
            f"The benchmark harness injects measurement_tag='{tag}' into the Gremlins MCP process, so do not invent or change the tag. "
            "The FIRST evidence_pack call must use detail='broad'. Then reason over the returned files, related_paths, relationships, and history. "
            "Every LATER evidence_pack call must use detail='focused' and must include at least one concrete paths, terms, or symbols value taken from the previous evidence. "
            "Do not make a second broad call. Focused follow-ups are intentionally compact. "
            "If a hypothesis or missing fact needs another lookup, ask a narrower question using those focused inputs. "
            "Prefer another focused evidence_pack over direct Read/Grep/Glob retrieval. "
            "Bash, web tools, and write/edit tools are unavailable in this arm. "
            "Use direct Read/Grep/Glob only if the evidence packs still lack evidence required to answer correctly. "
            "If you perform any direct repository evidence retrieval after using Gremlins, explicitly say FRONTIER_REDO_SEARCH=true at the end; otherwise say FRONTIER_REDO_SEARCH=false. "
            "The final answer must still be your own reasoning; Gremlins supplies evidence, not root-cause or architecture conclusions."
        )

    return {
        "case_id": case_id,
        "arm": arm,
        "iteration": max(1, int(iteration)),
        "measurement_tag": tag if arm == "C" else None,
        "expected_paths": case.get("expected_paths", []),
        "prompt": instructions + "\n\n" + common,
    }
