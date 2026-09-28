from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import time

from .config import load_config
from .evidence_service import evidence_pack


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _cases() -> list[dict]:
    path = Path(__file__).resolve().parents[2] / "evals" / "evidence-service" / "cases.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [item for item in data if isinstance(item, dict)]


def run_evidence_service_benchmark(repository: str = ".") -> dict:
    source = Path(repository).expanduser().resolve()
    head = _git(["rev-parse", "HEAD"], source)
    status = _git(["status", "--porcelain=v1"], source)
    if head.returncode != 0:
        raise RuntimeError(f"not a Git repository: {source}")
    if status.stdout.strip():
        raise RuntimeError("evidence-service benchmark requires a clean Git repository")

    cases = _cases()
    if not cases:
        raise RuntimeError("evidence-service benchmark has no cases")

    base_config = load_config()
    started = time.perf_counter()
    reports: list[dict] = []
    path_assertions = path_passed = 0
    relation_required = relation_passed = 0
    history_required = history_passed = 0
    local_model_calls = 0
    max_result_chars = 0

    with tempfile.TemporaryDirectory(prefix=".gremlins-evidence-", dir=str(source.parent)) as temp_dir:
        root = Path(temp_dir)
        workspace = root / "repo"
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(workspace)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if clone.returncode != 0:
            raise RuntimeError(f"failed to create evidence benchmark workspace: {clone.stderr.strip()}")

        sparse = _git(["sparse-checkout", "set", "--no-cone", "/*", "!/evals/"], workspace)
        if sparse.returncode != 0:
            raise RuntimeError(f"failed to isolate evidence benchmark answers: {sparse.stderr.strip()}")

        config = replace(
            base_config,
            security=replace(
                base_config.security,
                allowed_roots=(root.resolve(),),
                denied_paths=(),
            ),
        )

        for case in cases:
            case_started = time.perf_counter()
            result = evidence_pack(str(workspace), str(case["task"]), config, max_files=12)
            elapsed_ms = (time.perf_counter() - case_started) * 1000.0

            returned_paths = {
                str(item.get("path"))
                for item in result.get("files", [])
                if isinstance(item, dict)
            }
            returned_paths.update(
                str(item.get("path"))
                for item in result.get("related_paths", [])
                if isinstance(item, dict) and item.get("path")
            )
            for relation in result.get("relationships", []):
                if isinstance(relation, dict):
                    returned_paths.add(str(relation.get("source")))
                    returned_paths.add(str(relation.get("test")))

            expected = [str(path) for path in case.get("expected_paths", [])]
            missing = [path for path in expected if path not in returned_paths]
            path_assertions += len(expected)
            path_passed += len(expected) - len(missing)

            relation_ok = True
            if case.get("expect_relationship"):
                relation_required += 1
                relation_ok = bool(result.get("relationships"))
                relation_passed += int(relation_ok)

            history_ok = True
            if case.get("expect_history"):
                history_required += 1
                history = result.get("history") or {}
                history_ok = bool(history.get("by_path") or history.get("topic"))
                history_passed += int(history_ok)

            model_called = bool((result.get("usage") or {}).get("local_model_called"))
            local_model_calls += int(model_called)
            result_chars = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            max_result_chars = max(max_result_chars, result_chars)
            bounded = result_chars <= config.limits.max_result_evidence_chars

            reports.append({
                "id": case.get("id"),
                "family": case.get("family"),
                "ok": not missing and relation_ok and history_ok and not model_called and bounded,
                "expected_paths": expected,
                "returned_paths": sorted(returned_paths),
                "missing_expected_paths": missing,
                "discovery_terms": list((result.get("discovery") or {}).get("terms") or []),
                "matched_fuzzy_variants": list((result.get("discovery") or {}).get("matched_fuzzy_variants") or []),
                "relationship_ok": relation_ok,
                "history_ok": history_ok,
                "local_model_called": model_called,
                "result_chars": result_chars,
                "elapsed_ms": round(elapsed_ms, 3),
            })

    passed_cases = sum(bool(case["ok"]) for case in reports)
    overall = (
        passed_cases == len(reports)
        and path_passed == path_assertions
        and relation_passed == relation_required
        and history_passed == history_required
        and local_model_calls == 0
        and max_result_chars <= base_config.limits.max_result_evidence_chars
    )
    return {
        "benchmark": "evidence-service-v1",
        "repository": repository,
        "source_head": head.stdout.strip(),
        "pass": overall,
        "summary": {
            "cases": len(reports),
            "passed_cases": passed_cases,
            "path_assertions": path_assertions,
            "path_passed": path_passed,
            "relationship_assertions": relation_required,
            "relationship_passed": relation_passed,
            "history_assertions": history_required,
            "history_passed": history_passed,
            "local_model_calls": local_model_calls,
            "max_result_chars": max_result_chars,
            "result_budget_chars": base_config.limits.max_result_evidence_chars,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        },
        "cases": reports,
        "requirements": {
            "local_model_calls_must_equal": 0,
            "all_expected_paths_must_be_covered": True,
            "required_test_source_relationships_must_exist": True,
            "required_history_must_exist": True,
            "result_must_fit_budget": True,
        },
    }
