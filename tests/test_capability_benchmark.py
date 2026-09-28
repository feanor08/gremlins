from pathlib import Path
import subprocess

import gremlins.capability_benchmark as capability_benchmark
import gremlins.workers as workers


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "sample.py").write_text(
        "class NeedleError(Exception):\n    pass\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add sample"], cwd=repo, check=True)
    return repo


def test_capability_benchmark_passes_without_model(monkeypatch, tmp_path: Path):
    repo = _fixture_repo(tmp_path)
    monkeypatch.setattr(
        capability_benchmark,
        "load_pilot_cases",
        lambda: [{
            "id": "case-1",
            "task": "Find NeedleError",
            "terms": ["NeedleError"],
            "expected_paths": ["src/sample.py"],
        }],
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("capability benchmark must not call the local model")

    monkeypatch.setattr(workers, "chat_json", forbidden)

    report = capability_benchmark.run_capability_benchmark(str(repo))

    assert report["pass"] is True
    assert report["benchmark"] == "capabilities-v1"
    assert report["summary"]["local_model_calls"] == 0
    assert report["requirements"]["ai_client_required"] is False
    assert report["requirements"]["local_model_required"] is False
    assert report["requirements"]["mcp_interface_required"] is False

    for name in (
        "repo-search",
        "code-read",
        "git-history",
        "repo-explore",
        "triage-evidence",
        "security-policy",
        "contracts",
    ):
        assert report["capabilities"][name]["pass"] is True


def test_capability_benchmark_requires_clean_repository(monkeypatch, tmp_path: Path):
    repo = _fixture_repo(tmp_path)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.setattr(
        capability_benchmark,
        "load_pilot_cases",
        lambda: [{
            "id": "case-1",
            "task": "Find NeedleError",
            "terms": ["NeedleError"],
            "expected_paths": ["src/sample.py"],
        }],
    )

    try:
        capability_benchmark.run_capability_benchmark(str(repo))
    except RuntimeError as exc:
        assert "clean Git repository" in str(exc)
    else:
        raise AssertionError("dirty repository must be rejected")


def test_capability_benchmark_cli_parses():
    from gremlins.cli import build_parser, benchmark_capabilities_cmd

    args = build_parser().parse_args(["benchmark", "capabilities", "--repository", "."])

    assert args.func is benchmark_capabilities_cmd
    assert args.repository == "."
