from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import platform
import resource
import subprocess
import tempfile
import time
from statistics import median
from typing import Callable, TypeVar

from .benchmark import load_pilot_cases
from .config import Config, load_config
from .retrieval import Evidence, git_history, literal_search, read_excerpt
from .security import PolicyError, resolve_repo_file, resolve_repository
from .workers import _extract_log_evidence, repo_explore


T = TypeVar("T")


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _source_state(source: Path) -> dict:
    head = _run_git(["rev-parse", "HEAD"], source)
    status = _run_git(["status", "--porcelain=v1"], source)
    if head.returncode != 0:
        raise ValueError(f"not a Git repository: {source}")
    return {
        "head": head.stdout.strip(),
        "dirty": bool(status.stdout.strip()),
    }


def _timed(call: Callable[[], T]) -> tuple[T, float]:
    started = time.perf_counter()
    result = call()
    return result, (time.perf_counter() - started) * 1000.0


def _latency(values: list[float]) -> dict:
    if not values:
        return {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)

    def percentile(q: float) -> float:
        index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
        return ordered[index]

    return {
        "samples": len(values),
        "p50_ms": round(median(ordered), 3),
        "p95_ms": round(percentile(0.95), 3),
        "max_ms": round(max(ordered), 3),
    }


def _ratio(passed: int, total: int) -> float | None:
    return round(passed / total, 4) if total else None


def _evidence_shape(item: Evidence, kind: str | None = None) -> bool:
    data = item.as_dict()
    required = {"id", "kind", "path", "start_line", "end_line", "text"}
    return required.issubset(data) and (kind is None or item.kind == kind)


def _isolated_config(config: Config, root: Path, denied: Path) -> Config:
    return replace(
        config,
        security=replace(
            config.security,
            allowed_roots=(root.resolve(),),
            denied_paths=(denied.resolve(),),
        ),
    )


def _expect_policy_error(call: Callable[[], object]) -> bool:
    try:
        call()
    except PolicyError:
        return True
    return False


def _resource_snapshot() -> dict:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss = float(usage.ru_maxrss)
    rss_bytes = int(rss if platform.system() == "Darwin" else rss * 1024)
    return {
        "user_cpu_seconds": float(usage.ru_utime),
        "system_cpu_seconds": float(usage.ru_stime),
        "process_peak_rss_bytes": rss_bytes,
    }


def run_capability_benchmark(repository: str = ".") -> dict:
    """Run the caller-independent Gremlins capability benchmark.

    The bundled corpus is specific to the Gremlins repository. Scoring data remains
    outside the searchable clone so deterministic retrieval cannot find its own
    expected paths in eval metadata.
    """

    source = Path(repository).expanduser().resolve()
    source_state = _source_state(source)
    if source_state["dirty"]:
        raise RuntimeError("capability benchmark requires a clean Git repository")

    cases = load_pilot_cases()
    if not cases:
        raise RuntimeError("capability benchmark has no cases")

    base_config = load_config()
    resource_before = _resource_snapshot()
    benchmark_started = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix=".gremlins-capabilities-", dir=str(source.parent)) as temp_dir:
        temp_root = Path(temp_dir)
        workspace = temp_root / "repo"
        denied = temp_root / "denied"

        clone = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(workspace)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if clone.returncode != 0:
            raise RuntimeError(f"failed to create capability benchmark workspace: {clone.stderr.strip()}")

        sparse = _run_git(["sparse-checkout", "set", "--no-cone", "/*", "!/evals/"], workspace)
        if sparse.returncode != 0:
            raise RuntimeError(f"failed to isolate capability answer data: {sparse.stderr.strip()}")

        denied.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=denied, check=True)
        config = _isolated_config(base_config, temp_root, denied)
        deterministic_budget = min(config.limits.max_result_evidence_chars, 3000)

        search_latencies: list[float] = []
        read_latencies: list[float] = []
        history_latencies: list[float] = []
        explorer_latencies: list[float] = []
        search_result_chars: list[int] = []
        read_result_chars: list[int] = []
        history_result_chars: list[int] = []
        explorer_result_chars: list[int] = []

        search_passed = 0
        search_total = 0
        read_passed = 0
        read_total = 0
        history_passed = 0
        history_total = 0
        explorer_passed = 0
        explorer_total = len(cases)
        local_model_calls = 0

        search_cases: list[dict] = []
        read_cases: list[dict] = []
        history_cases: list[dict] = []
        explorer_cases: list[dict] = []

        first_search: Evidence | None = None
        first_read: Evidence | None = None
        first_history: Evidence | None = None
        first_explorer: dict | None = None

        for case in cases:
            case_id = str(case.get("id"))
            terms = [
                str(value)
                for value in [*case.get("symbols", []), *case.get("terms", [])]
                if str(value).strip()
            ]
            expected_paths = [str(path) for path in case.get("expected_paths", [])]

            def run_search_case() -> list[Evidence]:
                hits: list[Evidence] = []
                seen: set[tuple[str, int | None, str]] = set()
                for term in terms:
                    for item in literal_search(workspace, term, config):
                        key = (item.path, item.start_line, item.text)
                        if key not in seen:
                            seen.add(key)
                            hits.append(item)
                return hits

            hits, elapsed = _timed(run_search_case)
            search_latencies.append(elapsed)
            if hits and first_search is None:
                first_search = hits[0]
            returned_paths = {item.path for item in hits}
            missing = [path for path in expected_paths if path not in returned_paths]
            search_total += len(expected_paths)
            search_passed += len(expected_paths) - len(missing)
            search_payload = [item.as_dict() for item in hits]
            search_result_chars.append(len(json.dumps(search_payload, ensure_ascii=False, separators=(",", ":"))))
            search_cases.append({
                "id": case_id,
                "ok": not missing,
                "expected_paths": expected_paths,
                "missing_expected_paths": missing,
                "hits": len(hits),
                "elapsed_ms": round(elapsed, 3),
            })

            for path in expected_paths:
                read_total += 1
                anchor = next((item for item in hits if item.path == path and item.start_line), None)
                if anchor is None:
                    read_cases.append({
                        "id": case_id,
                        "path": path,
                        "ok": False,
                        "reason": "no exact-search anchor in expected path",
                    })
                    continue
                start_line = max(1, int(anchor.start_line or 1) - 3)
                evidence, read_elapsed = _timed(
                    lambda path=path, start_line=start_line: read_excerpt(
                        workspace, path, config, start_line=start_line, line_count=10
                    )
                )
                read_latencies.append(read_elapsed)
                if first_read is None:
                    first_read = evidence
                needle = anchor.text.strip()[:120]
                read_ok = evidence.path == path and bool(needle) and needle in evidence.text
                if read_ok:
                    read_passed += 1
                read_result_chars.append(
                    len(json.dumps(evidence.as_dict(), ensure_ascii=False, separators=(",", ":")))
                )
                read_cases.append({
                    "id": case_id,
                    "path": path,
                    "ok": read_ok,
                    "anchor_line": anchor.start_line,
                    "elapsed_ms": round(read_elapsed, 3),
                })

        unique_expected_paths = sorted({
            str(path)
            for case in cases
            for path in case.get("expected_paths", [])
        })
        for path in unique_expected_paths:
            history_total += 1
            items, elapsed = _timed(lambda path=path: git_history(workspace, config, path=path))
            history_latencies.append(elapsed)
            if items and first_history is None:
                first_history = items[0]
            history_ok = bool(items) and len(items) <= config.limits.max_history_entries and all(
                _evidence_shape(item, "git") for item in items
            )
            if history_ok:
                history_passed += 1
            history_result_chars.append(
                len(json.dumps([item.as_dict() for item in items], ensure_ascii=False, separators=(",", ":")))
            )
            history_cases.append({
                "path": path,
                "ok": history_ok,
                "entries": len(items),
                "elapsed_ms": round(elapsed, 3),
            })

        for case in cases:
            case_id = str(case.get("id"))
            expected_paths = {str(path) for path in case.get("expected_paths", [])}
            result, elapsed = _timed(
                lambda case=case: repo_explore(
                    str(workspace),
                    str(case["task"]),
                    config,
                    terms=[str(value) for value in case.get("terms", [])],
                    symbols=[str(value) for value in case.get("symbols", [])],
                    mode="deterministic",
                )
            )
            explorer_latencies.append(elapsed)
            if first_explorer is None:
                first_explorer = result
            files = result.get("files") if isinstance(result.get("files"), list) else []
            returned_paths = {
                str(item.get("path"))
                for item in files
                if isinstance(item, dict)
            }
            missing = sorted(expected_paths - returned_paths)
            model_called = bool((result.get("usage") or {}).get("local_model_called"))
            local_model_calls += int(model_called)
            result_chars = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            explorer_result_chars.append(result_chars)
            explorer_ok = (
                not missing
                and not model_called
                and result_chars <= deterministic_budget
                and result.get("status") in {"complete", "partial"}
            )
            if explorer_ok:
                explorer_passed += 1
            explorer_cases.append({
                "id": case_id,
                "ok": explorer_ok,
                "status": result.get("status"),
                "missing_expected_paths": missing,
                "local_model_called": model_called,
                "result_chars": result_chars,
                "elapsed_ms": round(elapsed, 3),
            })

        triage_log = (
            "startup\n"
            "ERROR first failure\n"
            "detail line\n"
            "FAILED test_alpha\n"
            "Traceback (most recent call last):\n"
            "RuntimeError: boom\n"
        )
        triage_evidence, triage_elapsed = _timed(lambda: _extract_log_evidence(triage_log))
        triage_text = "\n".join(triage_evidence)
        triage_ok = (
            "ERROR first failure" in triage_text
            and "FAILED test_alpha" in triage_text
            and len(triage_text) <= 32000
        )

        policy_probes = {
            "read_path_escape_denied": _expect_policy_error(
                lambda: resolve_repo_file(workspace, "../outside")
            ),
            "search_scope_escape_denied": _expect_policy_error(
                lambda: literal_search(workspace, "x", config, scope="..")
            ),
            "git_history_path_escape_denied": _expect_policy_error(
                lambda: git_history(workspace, config, path="../outside")
            ),
            "denied_repository_rejected": _expect_policy_error(
                lambda: resolve_repository(denied, config)
            ),
        }
        policy_passed = sum(bool(value) for value in policy_probes.values())

        explorer_contract = bool(first_explorer) and {
            "status", "snapshot", "files", "coverage", "usage"
        }.issubset(first_explorer)
        contracts = {
            "repo_search_evidence": bool(first_search) and _evidence_shape(first_search, "search"),
            "code_read_evidence": bool(first_read) and _evidence_shape(first_read, "file"),
            "git_history_evidence": bool(first_history) and _evidence_shape(first_history, "git"),
            "repo_explore_result": explorer_contract,
            "triage_evidence": isinstance(triage_evidence, list) and all(
                isinstance(line, str) for line in triage_evidence
            ),
        }
        contracts_passed = sum(bool(value) for value in contracts.values())

        capabilities = {
            "repo-search": {
                "pass": search_passed == search_total and search_total > 0,
                "assertions": search_total,
                "passed": search_passed,
                "coverage": _ratio(search_passed, search_total),
                "latency": _latency(search_latencies),
                "max_result_chars": max(search_result_chars, default=0),
                "cases": search_cases,
            },
            "code-read": {
                "pass": read_passed == read_total and read_total > 0,
                "assertions": read_total,
                "passed": read_passed,
                "coverage": _ratio(read_passed, read_total),
                "latency": _latency(read_latencies),
                "max_result_chars": max(read_result_chars, default=0),
                "cases": read_cases,
            },
            "git-history": {
                "pass": history_passed == history_total and history_total > 0,
                "assertions": history_total,
                "passed": history_passed,
                "coverage": _ratio(history_passed, history_total),
                "latency": _latency(history_latencies),
                "max_result_chars": max(history_result_chars, default=0),
                "cases": history_cases,
            },
            "repo-explore": {
                "pass": explorer_passed == explorer_total and local_model_calls == 0,
                "cases_total": explorer_total,
                "cases_passed": explorer_passed,
                "case_success_rate": _ratio(explorer_passed, explorer_total),
                "local_model_calls": local_model_calls,
                "latency": _latency(explorer_latencies),
                "max_result_chars": max(explorer_result_chars, default=0),
                "result_budget_chars": deterministic_budget,
                "cases": explorer_cases,
            },
            "triage-evidence": {
                "pass": triage_ok,
                "cases_total": 1,
                "cases_passed": int(triage_ok),
                "latency": _latency([triage_elapsed]),
                "evidence_lines": len(triage_evidence),
                "result_chars": len(triage_text),
            },
            "security-policy": {
                "pass": policy_passed == len(policy_probes),
                "probes": len(policy_probes),
                "passed": policy_passed,
                "results": policy_probes,
            },
            "contracts": {
                "pass": contracts_passed == len(contracts),
                "probes": len(contracts),
                "passed": contracts_passed,
                "results": contracts,
            },
        }

    resource_after = _resource_snapshot()
    elapsed_seconds = time.perf_counter() - benchmark_started
    capabilities_passed = sum(bool(item.get("pass")) for item in capabilities.values())
    overall_pass = capabilities_passed == len(capabilities) and local_model_calls == 0

    return {
        "benchmark": "capabilities-v1",
        "repository": repository,
        "source_head": source_state["head"],
        "environment": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "pass": overall_pass,
        "summary": {
            "capabilities": len(capabilities),
            "passed": capabilities_passed,
            "failed": len(capabilities) - capabilities_passed,
            "local_model_calls": local_model_calls,
            "elapsed_seconds": round(elapsed_seconds, 3),
        },
        "capabilities": capabilities,
        "resources": {
            "user_cpu_seconds": round(
                resource_after["user_cpu_seconds"] - resource_before["user_cpu_seconds"], 3
            ),
            "system_cpu_seconds": round(
                resource_after["system_cpu_seconds"] - resource_before["system_cpu_seconds"], 3
            ),
            "process_peak_rss_bytes": resource_after["process_peak_rss_bytes"],
        },
        "requirements": {
            "ai_client_required": False,
            "local_model_required": False,
            "mcp_interface_required": False,
            "all_correctness_policy_contract_probes_must_pass": True,
            "local_model_calls_must_equal": 0,
            "repo_explore_result_budget_chars": deterministic_budget,
        },
        "note": (
            "Latency and resource measurements are reported for trend tracking; v1 gates correctness, "
            "coverage, policy behavior, contract shape, bounded repo-explore results, and zero model calls."
        ),
    }
