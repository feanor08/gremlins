from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import subprocess
import tempfile
import time
from statistics import median

from .benchmark import load_pilot_cases
from .config import load_config, project_root
from .provider import ProviderError, health
from .workers import _compact_lines, _extract_log_evidence, repo_explore, triage


@dataclass(frozen=True)
class ArmScore:
    quality: float
    evidence_coverage: float
    analysis_coverage: float
    result_chars: int
    elapsed_seconds: float
    model_called: bool
    status: str
    prompt_eval_count: int | None = None
    eval_count: int | None = None

    def as_dict(self) -> dict:
        return {
            "quality": round(self.quality, 4),
            "evidence_coverage": round(self.evidence_coverage, 4),
            "analysis_coverage": round(self.analysis_coverage, 4),
            "result_chars": self.result_chars,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "model_called": self.model_called,
            "status": self.status,
            "prompt_eval_count": self.prompt_eval_count,
            "eval_count": self.eval_count,
        }


def triage_cases_path() -> Path:
    return project_root() / "evals" / "model-value" / "triage_cases.json"


def load_triage_cases() -> list[dict]:
    data = json.loads(triage_cases_path().read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("triage model-value cases must be a JSON array")
    return [item for item in data if isinstance(item, dict)]


def _source_state(source: Path) -> dict:
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    status = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain=v1"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if head.returncode != 0:
        raise ValueError(f"not a Git repository: {source}")
    return {"head": head.stdout.strip(), "dirty": bool(status.stdout.strip())}


def _coverage(text: str, groups: list[list[str]]) -> tuple[float, list[list[str]]]:
    if not groups:
        return 1.0, []
    lowered = text.lower()
    missing: list[list[str]] = []
    for group in groups:
        alternatives = [str(value).strip() for value in group if str(value).strip()]
        if not alternatives:
            continue
        if not any(value.lower() in lowered for value in alternatives):
            missing.append(alternatives)
    total = len([group for group in groups if any(str(value).strip() for value in group)])
    if total == 0:
        return 1.0, []
    return (total - len(missing)) / total, missing


def _result_text(result: object) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _repo_paths(result: dict) -> set[str]:
    items = result.get("files")
    if not isinstance(items, list):
        items = result.get("evidence") if isinstance(result.get("evidence"), list) else []
    return {
        str(item.get("path"))
        for item in items
        if isinstance(item, dict) and item.get("path") is not None
    }


def _score_repo(result: dict, case: dict, elapsed: float) -> tuple[ArmScore, dict]:
    expected_paths = [str(value) for value in case.get("expected_paths", [])]
    returned_paths = _repo_paths(result)
    missing_paths = [path for path in expected_paths if path not in returned_paths]
    path_coverage = (
        (len(expected_paths) - len(missing_paths)) / len(expected_paths)
        if expected_paths else 1.0
    )
    claim_groups = [
        [str(value) for value in group]
        for group in case.get("expected_claims", [])
        if isinstance(group, list)
    ]
    claim_coverage, missing_claims = _coverage(_result_text(result), claim_groups)
    quality = (path_coverage + claim_coverage) / 2.0
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    score = ArmScore(
        quality=quality,
        evidence_coverage=path_coverage,
        analysis_coverage=claim_coverage,
        result_chars=len(_result_text(result)),
        elapsed_seconds=elapsed,
        model_called=bool(usage.get("local_model_called")),
        status=str(result.get("status") or "unknown"),
        prompt_eval_count=(
            int(usage["prompt_eval_count"])
            if usage.get("prompt_eval_count") is not None else None
        ),
        eval_count=int(usage["eval_count"]) if usage.get("eval_count") is not None else None,
    )
    detail = {
        "missing_expected_paths": missing_paths,
        "missing_expected_claim_groups": missing_claims,
        "returned_paths": sorted(returned_paths),
    }
    return score, detail


def _score_triage(
    result: dict,
    case: dict,
    elapsed: float,
    *,
    deterministic: bool,
) -> tuple[ArmScore, dict]:
    evidence_groups = [
        [str(value) for value in group]
        for group in case.get("expected_evidence", [])
        if isinstance(group, list)
    ]
    analysis_groups = [
        [str(value) for value in group]
        for group in case.get("expected_analysis", [])
        if isinstance(group, list)
    ]
    payload = _result_text(result)
    evidence_coverage, missing_evidence = _coverage(payload, evidence_groups)
    analysis_coverage, missing_analysis = _coverage(payload, analysis_groups)
    quality = (evidence_coverage + analysis_coverage) / 2.0
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    score = ArmScore(
        quality=quality,
        evidence_coverage=evidence_coverage,
        analysis_coverage=analysis_coverage,
        result_chars=len(payload),
        elapsed_seconds=elapsed,
        model_called=False if deterministic else bool(usage.get("local_model_called")),
        status=str(result.get("status") or "unknown"),
        prompt_eval_count=(
            int(usage["prompt_eval_count"])
            if usage.get("prompt_eval_count") is not None else None
        ),
        eval_count=int(usage["eval_count"]) if usage.get("eval_count") is not None else None,
    )
    return score, {
        "missing_expected_evidence_groups": missing_evidence,
        "missing_expected_analysis_groups": missing_analysis,
    }


def _deterministic_triage(text: str, max_chars: int) -> dict:
    evidence = _extract_log_evidence(text, max_chars=max_chars)
    compact = _compact_lines(evidence, max_chars)
    return {
        "status": "complete",
        "summary": "Deterministic failure evidence only; no local synthesis was requested.",
        "failure_groups": [],
        "next_checks": [],
        "limitations": ["No model synthesis in deterministic arm."],
        "evidence": compact,
        "evidence_lines_total": len(evidence),
        "evidence_lines_returned": len(compact),
        "usage": {"local_model_called": False, "mode": "deterministic"},
    }


def _pct_change(before: float, after: float) -> float | None:
    if before == 0:
        return None
    return round(((after - before) / before) * 100.0, 2)


def _aggregate(pairs: list[dict], min_quality_gain: float) -> dict:
    if not pairs:
        return {
            "cases": 0,
            "deterministic_quality": None,
            "model_quality": None,
            "quality_gain": None,
            "model_success_rate": None,
            "model_call_rate": None,
            "value_signal": "no-cases",
            "reasons": ["no cases were selected"],
        }

    det_scores = [float(item["deterministic"]["quality"]) for item in pairs]
    model_scores = [float(item["model"]["quality"]) for item in pairs]
    det_sizes = [int(item["deterministic"]["result_chars"]) for item in pairs]
    model_sizes = [int(item["model"]["result_chars"]) for item in pairs]
    det_elapsed = [float(item["deterministic"]["elapsed_seconds"]) for item in pairs]
    model_elapsed = [float(item["model"]["elapsed_seconds"]) for item in pairs]
    model_success = [
        item["model"]["status"] in {"complete", "partial"}
        and bool(item["model"]["model_called"])
        for item in pairs
    ]
    model_called = [bool(item["model"]["model_called"]) for item in pairs]

    det_quality = sum(det_scores) / len(det_scores)
    model_quality = sum(model_scores) / len(model_scores)
    quality_gain = model_quality - det_quality
    success_rate = sum(model_success) / len(model_success)
    call_rate = sum(model_called) / len(model_called)
    reasons: list[str] = []

    if success_rate < 1.0:
        reasons.append(f"local-model arm completed successfully for only {success_rate:.0%} of cases")
    if quality_gain < min_quality_gain:
        reasons.append(
            f"mean quality gain {quality_gain:.3f} is below the configured materiality threshold {min_quality_gain:.3f}"
        )

    value_signal = "measured-quality-gain" if not reasons else "no-material-value-yet"
    return {
        "cases": len(pairs),
        "deterministic_quality": round(det_quality, 4),
        "model_quality": round(model_quality, 4),
        "quality_gain": round(quality_gain, 4),
        "quality_gain_percentage_points": round(quality_gain * 100.0, 2),
        "model_success_rate": round(success_rate, 4),
        "model_call_rate": round(call_rate, 4),
        "deterministic_result_chars_median": int(median(det_sizes)),
        "model_result_chars_median": int(median(model_sizes)),
        "result_chars_change_pct": _pct_change(float(median(det_sizes)), float(median(model_sizes))),
        "deterministic_elapsed_seconds_median": round(median(det_elapsed), 4),
        "model_elapsed_seconds_median": round(median(model_elapsed), 4),
        "latency_multiplier": (
            round(median(model_elapsed) / median(det_elapsed), 2)
            if median(det_elapsed) > 0 else None
        ),
        "value_signal": value_signal,
        "reasons": reasons,
    }


def run_model_value_benchmark(
    repository: str = ".",
    *,
    worker: str = "all",
    repo_case_ids: list[str] | None = None,
    triage_case_ids: list[str] | None = None,
    min_quality_gain: float = 0.10,
) -> dict:
    if worker not in {"all", "repo-explore", "triage"}:
        raise ValueError("worker must be one of: all, repo-explore, triage")
    if not 0.0 <= min_quality_gain <= 1.0:
        raise ValueError("min_quality_gain must be between 0 and 1")

    source = Path(repository).expanduser().resolve()
    state = _source_state(source)
    if state["dirty"]:
        raise RuntimeError("model-value benchmark requires a clean Git repository")

    config = load_config()
    try:
        provider_state = health(config)
    except ProviderError as exc:
        raise RuntimeError(f"local model provider is unavailable: {exc}") from exc
    if not provider_state.get("model_present"):
        raise RuntimeError(
            f"configured local model is not present: {config.provider.model}; "
            f"available={provider_state.get('models', [])}"
        )

    repo_pairs: list[dict] = []
    triage_pairs: list[dict] = []

    if worker in {"all", "repo-explore"}:
        cases = load_pilot_cases()
        if repo_case_ids:
            selected = set(repo_case_ids)
            cases = [case for case in cases if str(case.get("id")) in selected]
            missing = selected - {str(case.get("id")) for case in cases}
            if missing:
                raise ValueError(f"unknown repo model-value cases: {sorted(missing)}")

        with tempfile.TemporaryDirectory(prefix=".gremlins-model-value-", dir=str(source.parent)) as temp_dir:
            temp_root = Path(temp_dir).resolve()
            workspace = temp_root / "repo"
            config = replace(
                config,
                security=replace(
                    config.security,
                    allowed_roots=(*config.security.allowed_roots, temp_root),
                ),
            )
            clone = subprocess.run(
                ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(workspace)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if clone.returncode != 0:
                raise RuntimeError(f"failed to create model-value workspace: {clone.stderr.strip()}")
            sparse = subprocess.run(
                ["git", "-C", str(workspace), "sparse-checkout", "set", "--no-cone", "/*", "!/evals/"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if sparse.returncode != 0:
                raise RuntimeError(f"failed to isolate model-value answer data: {sparse.stderr.strip()}")

            for case in cases:
                case_id = str(case.get("id"))
                kwargs = {
                    "terms": [str(value) for value in case.get("terms", [])],
                    "symbols": [str(value) for value in case.get("symbols", [])],
                }

                started = time.perf_counter()
                deterministic = repo_explore(
                    str(workspace),
                    str(case["task"]),
                    config,
                    mode="deterministic",
                    measurement_tag=f"model-value:{case_id}:deterministic",
                    **kwargs,
                )
                det_elapsed = time.perf_counter() - started
                det_score, det_detail = _score_repo(deterministic, case, det_elapsed)

                started = time.perf_counter()
                model = repo_explore(
                    str(workspace),
                    str(case["task"]),
                    config,
                    mode="model",
                    measurement_tag=f"model-value:{case_id}:model",
                    **kwargs,
                )
                model_elapsed = time.perf_counter() - started
                model_score, model_detail = _score_repo(model, case, model_elapsed)

                repo_pairs.append({
                    "id": case_id,
                    "task": str(case.get("task") or ""),
                    "deterministic": det_score.as_dict(),
                    "model": model_score.as_dict(),
                    "quality_gain": round(model_score.quality - det_score.quality, 4),
                    "deterministic_detail": det_detail,
                    "model_detail": model_detail,
                })

    if worker in {"all", "triage"}:
        cases = load_triage_cases()
        if triage_case_ids:
            selected = set(triage_case_ids)
            cases = [case for case in cases if str(case.get("id")) in selected]
            missing = selected - {str(case.get("id")) for case in cases}
            if missing:
                raise ValueError(f"unknown triage model-value cases: {sorted(missing)}")

        for case in cases:
            case_id = str(case.get("id"))
            log_text = str(case.get("log") or "")
            task = str(case.get("task") or "Group failures, identify supported causes, and give next checks.")

            started = time.perf_counter()
            deterministic = _deterministic_triage(log_text, config.limits.max_result_evidence_chars)
            det_elapsed = time.perf_counter() - started
            det_score, det_detail = _score_triage(
                deterministic, case, det_elapsed, deterministic=True
            )

            started = time.perf_counter()
            model = triage(
                log_text,
                task,
                config,
                measurement_tag=f"model-value:{case_id}:model",
            )
            model_elapsed = time.perf_counter() - started
            model_score, model_detail = _score_triage(
                model, case, model_elapsed, deterministic=False
            )

            triage_pairs.append({
                "id": case_id,
                "task": task,
                "deterministic": det_score.as_dict(),
                "model": model_score.as_dict(),
                "quality_gain": round(model_score.quality - det_score.quality, 4),
                "deterministic_detail": det_detail,
                "model_detail": model_detail,
            })

    repo_summary = _aggregate(repo_pairs, min_quality_gain)
    triage_summary = _aggregate(triage_pairs, min_quality_gain)

    selected_summaries = []
    if worker in {"all", "repo-explore"}:
        selected_summaries.append(repo_summary)
    if worker in {"all", "triage"}:
        selected_summaries.append(triage_summary)

    positive = [
        summary.get("value_signal") == "measured-quality-gain"
        for summary in selected_summaries
    ]

    prompt_tokens = sum(
        int(pair["model"].get("prompt_eval_count") or 0)
        for pair in [*repo_pairs, *triage_pairs]
    )
    output_tokens = sum(
        int(pair["model"].get("eval_count") or 0)
        for pair in [*repo_pairs, *triage_pairs]
    )

    return {
        "benchmark": "model-value-v1",
        "source_head": state["head"],
        "provider": {
            "kind": config.provider.kind,
            "url": config.provider.url,
            "model": config.provider.model,
            "health": provider_state,
        },
        "worker": worker,
        "thresholds": {
            "min_mean_quality_gain": min_quality_gain,
            "latency_is_informational": True,
            "result_size_is_informational": True,
        },
        "repo_explore": {
            "summary": repo_summary,
            "pairs": repo_pairs,
        },
        "triage": {
            "summary": triage_summary,
            "pairs": triage_pairs,
        },
        "model_usage": {
            "prompt_eval_tokens": prompt_tokens,
            "eval_tokens": output_tokens,
            "calls": sum(
                int(bool(pair["model"].get("model_called")))
                for pair in [*repo_pairs, *triage_pairs]
            ),
        },
        "overall": {
            "workers_measured": len(selected_summaries),
            "workers_with_measured_quality_gain": sum(positive),
            "all_measured_workers_show_material_gain": bool(positive) and all(positive),
        },
        "interpretation": (
            "The primary signal is paired mean quality gain on the same inputs and evidence budget. "
            "Latency and result-size changes are reported but not used to manufacture a value verdict. "
            "A worker should keep local synthesis only when its measured quality gain is material enough "
            "for the workload."
        ),
    }
