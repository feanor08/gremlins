from pathlib import Path
import subprocess

import gremlins.model_value_benchmark as model_value


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "sample.py").write_text(
        "class NeedleError(Exception):\n    pass\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def test_model_value_benchmark_measures_paired_gain(monkeypatch, tmp_path: Path):
    repo = _fixture_repo(tmp_path)
    monkeypatch.setattr(model_value, "health", lambda config: {
        "ok": True,
        "model_present": True,
        "models": [config.provider.model],
    })
    monkeypatch.setattr(model_value, "load_pilot_cases", lambda: [{
        "id": "repo-case",
        "task": "Explain NeedleError",
        "terms": ["NeedleError"],
        "expected_paths": ["sample.py"],
        "expected_claims": [["exception class"], ["NeedleError"]],
    }])
    monkeypatch.setattr(model_value, "load_triage_cases", lambda: [{
        "id": "triage-case",
        "task": "triage",
        "log": "ERROR ModuleNotFoundError: No module named 'httpx'\n",
        "expected_evidence": [["ModuleNotFoundError"], ["httpx"]],
        "expected_analysis": [["missing dependency"], ["install httpx"]],
    }])

    def fake_repo_explore(repository, task, config, mode, **kwargs):
        if mode == "deterministic":
            return {
                "status": "complete",
                "files": [{"path": "sample.py", "hits": [{"line": 1, "text": "NeedleError"}]}],
                "usage": {"local_model_called": False},
            }
        return {
            "status": "complete",
            "summary": "NeedleError is an exception class.",
            "findings": [],
            "evidence": [{"id": "s1", "kind": "search", "path": "sample.py", "text": "NeedleError"}],
            "usage": {
                "local_model_called": True,
                "prompt_eval_count": 100,
                "eval_count": 20,
            },
        }

    def fake_triage(text, task, config, **kwargs):
        return {
            "status": "complete",
            "summary": "The httpx package is a missing dependency; install httpx.",
            "failure_groups": [],
            "next_checks": ["install httpx"],
            "limitations": [],
            "evidence": ["ModuleNotFoundError: No module named 'httpx'"],
            "usage": {
                "local_model_called": True,
                "prompt_eval_count": 50,
                "eval_count": 15,
            },
        }

    monkeypatch.setattr(model_value, "repo_explore", fake_repo_explore)
    monkeypatch.setattr(model_value, "triage", fake_triage)

    report = model_value.run_model_value_benchmark(
        str(repo),
        min_quality_gain=0.10,
    )

    assert report["benchmark"] == "model-value-v1"
    assert report["repo_explore"]["summary"]["value_signal"] == "measured-quality-gain"
    assert report["triage"]["summary"]["value_signal"] == "measured-quality-gain"
    assert report["overall"]["all_measured_workers_show_material_gain"] is True
    assert report["model_usage"]["calls"] == 2
    assert report["model_usage"]["prompt_eval_tokens"] == 150
    assert report["model_usage"]["eval_tokens"] == 35


def test_model_value_benchmark_rejects_missing_provider(monkeypatch, tmp_path: Path):
    repo = _fixture_repo(tmp_path)
    monkeypatch.setattr(model_value, "health", lambda config: {
        "ok": True,
        "model_present": False,
        "models": [],
    })

    try:
        model_value.run_model_value_benchmark(str(repo), worker="repo-explore")
    except RuntimeError as exc:
        assert "configured local model is not present" in str(exc)
    else:
        raise AssertionError("missing configured model must fail explicitly")


def test_model_value_cli_parses():
    from gremlins.cli import build_parser, benchmark_model_value_cmd

    args = build_parser().parse_args([
        "benchmark",
        "model-value",
        "--repository",
        ".",
        "--worker",
        "triage",
        "--min-quality-gain",
        "0.2",
    ])

    assert args.func is benchmark_model_value_cmd
    assert args.worker == "triage"
    assert args.min_quality_gain == 0.2


def test_model_value_benchmark_allows_only_its_created_workspace(monkeypatch, tmp_path: Path):
    repo = _fixture_repo(tmp_path)
    monkeypatch.setattr(model_value, "health", lambda config: {
        "ok": True,
        "model_present": True,
        "models": [config.provider.model],
    })
    monkeypatch.setattr(model_value, "load_pilot_cases", lambda: [{
        "id": "repo-case",
        "task": "Find NeedleError",
        "terms": ["NeedleError"],
        "expected_paths": ["sample.py"],
        "expected_claims": [["NeedleError"]],
    }])

    seen = {}

    def fake_repo_explore(repository, task, config, mode, **kwargs):
        workspace = Path(repository).resolve()
        seen["workspace"] = workspace
        seen["allowed_roots"] = tuple(config.security.allowed_roots)
        assert any(
            workspace == root or workspace.is_relative_to(root)
            for root in config.security.allowed_roots
        )
        return {
            "status": "complete",
            "files": [{"path": "sample.py", "hits": [{"line": 1, "text": "NeedleError"}]}],
            "evidence": [{"id": "s1", "kind": "search", "path": "sample.py", "text": "NeedleError"}],
            "usage": {
                "local_model_called": mode == "model",
                "prompt_eval_count": 10 if mode == "model" else None,
                "eval_count": 5 if mode == "model" else None,
            },
        }

    monkeypatch.setattr(model_value, "repo_explore", fake_repo_explore)

    report = model_value.run_model_value_benchmark(
        str(repo),
        worker="repo-explore",
        min_quality_gain=0.0,
    )

    assert report["repo_explore"]["summary"]["cases"] == 1
    workspace = seen["workspace"]
    assert workspace.name == "repo"
    assert any(workspace.is_relative_to(root) for root in seen["allowed_roots"])
    assert repo.parent.resolve() not in seen["allowed_roots"]
