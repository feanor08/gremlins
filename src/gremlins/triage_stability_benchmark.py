from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median, pstdev
import time

from .config import load_config
from .model_value_benchmark import (
    _deterministic_triage,
    _score_triage,
    _source_state,
    load_triage_cases,
)
from .provider import ProviderError, health
from .workers import triage


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * 0.95 + 0.999999)) - 1))
    return round(ordered[index], 4)


def _fraction(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def run_triage_stability_benchmark(
    repository: str = ".",
    *,
    repeats: int = 3,
    case_ids: list[str] | None = None,
    min_mean_quality_gain: float = 0.10,
    min_success_rate: float = 0.95,
    min_case_model_quality: float = 0.75,
    max_case_quality_span: float = 0.25,
    min_stable_case_fraction: float = 0.90,
    max_regressed_case_fraction: float = 0.10,
) -> dict:
    if repeats < 2 or repeats > 5:
        raise ValueError("repeats must be between 2 and 5 for the bounded stability benchmark")
    for name, value in {
        "min_mean_quality_gain": min_mean_quality_gain,
        "min_success_rate": min_success_rate,
        "min_case_model_quality": min_case_model_quality,
        "max_case_quality_span": max_case_quality_span,
        "min_stable_case_fraction": min_stable_case_fraction,
        "max_regressed_case_fraction": max_regressed_case_fraction,
    }.items():
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")

    source = Path(repository).expanduser().resolve()
    state = _source_state(source)
    if state["dirty"]:
        raise RuntimeError("triage stability benchmark requires a clean Git repository")

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

    cases = load_triage_cases()
    if case_ids:
        requested = set(case_ids)
        cases = [case for case in cases if str(case.get("id")) in requested]
        missing = requested - {str(case.get("id")) for case in cases}
        if missing:
            raise ValueError(f"unknown triage stability cases: {sorted(missing)}")
    if not cases:
        raise RuntimeError("triage stability benchmark has no selected cases")

    baselines: dict[str, dict] = {}
    per_case_runs: dict[str, list[dict]] = {str(case["id"]): [] for case in cases}

    for case in cases:
        case_id = str(case["id"])
        log_text = str(case.get("log") or "")
        started = time.perf_counter()
        deterministic = _deterministic_triage(
            log_text,
            config.limits.max_result_evidence_chars,
        )
        elapsed = time.perf_counter() - started
        score, detail = _score_triage(
            deterministic,
            case,
            elapsed,
            deterministic=True,
        )
        baselines[case_id] = {
            "score": score.as_dict(),
            "detail": detail,
        }

    execution_order: list[dict] = []
    all_model_runs: list[dict] = []

    for repeat_index in range(repeats):
        offset = repeat_index % len(cases)
        ordered_cases = cases[offset:] + cases[:offset]
        execution_order.append({
            "repeat": repeat_index + 1,
            "case_ids": [str(case["id"]) for case in ordered_cases],
        })

        for case in ordered_cases:
            case_id = str(case["id"])
            log_text = str(case.get("log") or "")
            task = str(
                case.get("task")
                or "Group failures, identify supported causes, and give next checks."
            )
            started = time.perf_counter()
            result = triage(
                log_text,
                task,
                config,
                measurement_tag=f"triage-stability:{case_id}:r{repeat_index + 1}",
            )
            elapsed = time.perf_counter() - started
            score, detail = _score_triage(
                result,
                case,
                elapsed,
                deterministic=False,
            )
            run = {
                "repeat": repeat_index + 1,
                **score.as_dict(),
                "detail": detail,
            }
            per_case_runs[case_id].append(run)
            all_model_runs.append({"id": case_id, **run})

    case_reports: list[dict] = []
    stable_cases = 0
    quality_floor_cases = 0
    regressed_cases = 0

    for case in cases:
        case_id = str(case["id"])
        runs = per_case_runs[case_id]
        qualities = [float(run["quality"]) for run in runs]
        successes = [
            run["status"] in {"complete", "partial"} and bool(run["model_called"])
            for run in runs
        ]
        baseline_quality = float(baselines[case_id]["score"]["quality"])
        mean_quality = mean(qualities)
        quality_span = max(qualities) - min(qualities)
        mean_gain = mean_quality - baseline_quality
        stable = all(successes) and quality_span <= max_case_quality_span
        meets_floor = mean_quality >= min_case_model_quality
        regressed = mean_quality < baseline_quality

        stable_cases += int(stable)
        quality_floor_cases += int(meets_floor)
        regressed_cases += int(regressed)

        case_reports.append({
            "id": case_id,
            "category": case.get("category"),
            "deterministic": baselines[case_id],
            "model": {
                "runs": runs,
                "mean_quality": round(mean_quality, 4),
                "min_quality": round(min(qualities), 4),
                "max_quality": round(max(qualities), 4),
                "quality_span": round(quality_span, 4),
                "quality_stddev": round(pstdev(qualities), 4),
                "mean_quality_gain": round(mean_gain, 4),
                "success_rate": round(sum(successes) / len(successes), 4),
                "stable": stable,
                "meets_quality_floor": meets_floor,
                "mean_regressed_vs_deterministic": regressed,
            },
        })

    deterministic_qualities = [
        float(baselines[str(case["id"])]["score"]["quality"])
        for case in cases
    ]
    model_qualities = [float(run["quality"]) for run in all_model_runs]
    model_latencies = [float(run["elapsed_seconds"]) for run in all_model_runs]
    successes = [
        run["status"] in {"complete", "partial"} and bool(run["model_called"])
        for run in all_model_runs
    ]
    model_calls = sum(int(bool(run["model_called"])) for run in all_model_runs)
    prompt_tokens = sum(int(run.get("prompt_eval_count") or 0) for run in all_model_runs)
    eval_tokens = sum(int(run.get("eval_count") or 0) for run in all_model_runs)

    deterministic_quality = mean(deterministic_qualities)
    model_quality = mean(model_qualities)
    mean_gain = model_quality - deterministic_quality
    success_rate = sum(successes) / len(successes)
    stable_case_fraction = stable_cases / len(cases)
    quality_floor_fraction = quality_floor_cases / len(cases)
    regressed_case_fraction = regressed_cases / len(cases)

    reasons: list[str] = []
    if mean_gain < min_mean_quality_gain:
        reasons.append(
            f"mean quality gain {mean_gain:.3f} is below {min_mean_quality_gain:.3f}"
        )
    if success_rate < min_success_rate:
        reasons.append(
            f"model success rate {success_rate:.1%} is below {min_success_rate:.1%}"
        )
    if stable_case_fraction < min_stable_case_fraction:
        reasons.append(
            f"stable case fraction {stable_case_fraction:.1%} is below "
            f"{min_stable_case_fraction:.1%}"
        )
    if quality_floor_fraction < min_stable_case_fraction:
        reasons.append(
            f"quality-floor case fraction {quality_floor_fraction:.1%} is below "
            f"{min_stable_case_fraction:.1%}"
        )
    if regressed_case_fraction > max_regressed_case_fraction:
        reasons.append(
            f"regressed case fraction {regressed_case_fraction:.1%} exceeds "
            f"{max_regressed_case_fraction:.1%}"
        )

    return {
        "benchmark": "triage-stability-v1",
        "source_head": state["head"],
        "provider": {
            "kind": config.provider.kind,
            "url": config.provider.url,
            "model": config.provider.model,
            "health": provider_state,
        },
        "corpus": {
            "cases": len(cases),
            "repeats": repeats,
            "expected_model_calls": len(cases) * repeats,
            "categories": sorted({
                str(case.get("category") or "uncategorized")
                for case in cases
            }),
        },
        "thresholds": {
            "min_mean_quality_gain": min_mean_quality_gain,
            "min_success_rate": min_success_rate,
            "min_case_model_quality": min_case_model_quality,
            "max_case_quality_span": max_case_quality_span,
            "min_stable_case_fraction": min_stable_case_fraction,
            "max_regressed_case_fraction": max_regressed_case_fraction,
        },
        "summary": {
            "deterministic_quality": round(deterministic_quality, 4),
            "model_quality": round(model_quality, 4),
            "mean_quality_gain": round(mean_gain, 4),
            "mean_quality_gain_percentage_points": round(mean_gain * 100.0, 2),
            "model_success_rate": round(success_rate, 4),
            "stable_cases": stable_cases,
            "stable_case_fraction": round(stable_case_fraction, 4),
            "quality_floor_cases": quality_floor_cases,
            "quality_floor_case_fraction": round(quality_floor_fraction, 4),
            "regressed_cases": regressed_cases,
            "regressed_case_fraction": round(regressed_case_fraction, 4),
            "model_quality_stddev_all_runs": round(pstdev(model_qualities), 4),
            "model_latency_seconds_median": round(median(model_latencies), 4),
            "model_latency_seconds_p95": _p95(model_latencies),
            "model_calls": model_calls,
            "prompt_eval_tokens": prompt_tokens,
            "eval_tokens": eval_tokens,
            "stable_material_value": not reasons,
            "reasons": reasons,
        },
        "execution_order": execution_order,
        "cases": case_reports,
        "interpretation": (
            "This benchmark measures whether bounded local triage synthesis remains useful "
            "across a broader corpus and repeated runs. It does not authorize Gremlins to "
            "perform Claude-level ambiguous debugging, architecture, trade-off analysis, "
            "patch design, or subtle review."
        ),
    }
