from pathlib import Path
import subprocess

import gremlins.triage_stability_benchmark as stability


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def test_triage_stability_benchmark_measures_repeats(monkeypatch, tmp_path: Path):
    repo = _fixture_repo(tmp_path)
    monkeypatch.setattr(stability, "health", lambda config: {
        "ok": True,
        "model_present": True,
        "models": [config.provider.model],
    })
    monkeypatch.setattr(stability, "load_triage_cases", lambda: [{
        "id": "triage-case",
        "category": "dependency",
        "task": "triage",
        "log": "ERROR ModuleNotFoundError: No module named 'httpx'\n",
        "expected_evidence": [["ModuleNotFoundError"], ["httpx"]],
        "expected_analysis": [["missing dependency"], ["install httpx"]],
    }])

    def fake_triage(text, task, config, **kwargs):
        return {
            "status": "complete",
            "summary": "httpx is a missing dependency; install httpx.",
            "failure_groups": [],
            "next_checks": ["install httpx"],
            "limitations": [],
            "evidence": ["ModuleNotFoundError: No module named 'httpx'"],
            "usage": {
                "local_model_called": True,
                "prompt_eval_count": 40,
                "eval_count": 10,
            },
        }

    monkeypatch.setattr(stability, "triage", fake_triage)

    report = stability.run_triage_stability_benchmark(
        str(repo),
        repeats=3,
        min_mean_quality_gain=0.10,
    )

    assert report["benchmark"] == "triage-stability-v1"
    assert report["corpus"]["cases"] == 1
    assert report["corpus"]["repeats"] == 3
    assert report["summary"]["model_calls"] == 3
    assert report["summary"]["prompt_eval_tokens"] == 120
    assert report["summary"]["eval_tokens"] == 30
    assert report["summary"]["stable_material_value"] is True
    assert report["summary"]["stable_case_fraction"] == 1.0
    assert len(report["cases"][0]["model"]["runs"]) == 3


def test_triage_stability_rejects_unbounded_repeats(monkeypatch, tmp_path: Path):
    repo = _fixture_repo(tmp_path)

    try:
        stability.run_triage_stability_benchmark(str(repo), repeats=6)
    except ValueError as exc:
        assert "between 2 and 5" in str(exc)
    else:
        raise AssertionError("unbounded repeats must be rejected")


def test_triage_stability_corpus_has_breadth():
    cases = stability.load_triage_cases()
    ids = [str(case.get("id")) for case in cases]
    categories = {str(case.get("category")) for case in cases}

    assert len(cases) >= 20
    assert len(ids) == len(set(ids))
    assert len(categories) >= 15
    for case in cases:
        assert case.get("log")
        assert len(case.get("expected_evidence", [])) >= 2
        assert len(case.get("expected_analysis", [])) >= 2


def test_triage_stability_cli_parses():
    from gremlins.cli import build_parser, benchmark_triage_stability_cmd

    args = build_parser().parse_args([
        "benchmark",
        "triage-stability",
        "--repository",
        ".",
        "--repeats",
        "3",
    ])

    assert args.func is benchmark_triage_stability_cmd
    assert args.repeats == 3
