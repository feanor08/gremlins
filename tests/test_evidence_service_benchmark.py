from pathlib import Path
import subprocess

import gremlins.evidence_service_benchmark as benchmark


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "sample.py").write_text("NeedleEvidence = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add evidence"], cwd=repo, check=True)
    return repo


def test_evidence_service_benchmark_passes(monkeypatch, tmp_path: Path):
    repo = _repo(tmp_path)
    monkeypatch.setattr(benchmark, "_cases", lambda: [{
        "id": "e1",
        "family": "fixture",
        "task": "Find NeedleEvidence",
        "expected_paths": ["src/sample.py"],
    }])
    report = benchmark.run_evidence_service_benchmark(str(repo))
    assert report["pass"] is True
    assert report["summary"]["local_model_calls"] == 0
    assert report["summary"]["path_passed"] == 1


def test_evidence_service_benchmark_cli_parses():
    from gremlins.cli import build_parser, benchmark_evidence_service_cmd

    args = build_parser().parse_args([
        "benchmark",
        "evidence-service",
        "--repository",
        ".",
    ])
    assert args.func is benchmark_evidence_service_cmd
    assert args.repository == "."
