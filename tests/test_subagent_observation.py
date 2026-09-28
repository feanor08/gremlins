from pathlib import Path
from types import SimpleNamespace
import subprocess

import gremlins.subagent_observation as observation
from gremlins.subagent_observation import classify_delegated_request


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def test_classifier_respects_known_agent_types():
    explore = classify_delegated_request({
        "subagent_type": "Explore",
        "prompt": "Investigate why this is designed this way",
    })
    plan = classify_delegated_request({
        "subagent_type": "Plan",
        "prompt": "Find relevant files",
    })
    assert explore["label"] == "evidence-acquisition"
    assert plan["label"] == "claude-level-reasoning"


def test_classifier_distinguishes_mixed_work():
    result = classify_delegated_request({
        "subagent_type": "general-purpose",
        "description": "Investigate root cause",
        "prompt": "Search the tests and git history, then explain the root cause hypothesis.",
    })
    assert result["label"] == "mixed-evidence-and-reasoning"
    assert "root cause" in result["reasoning_signals"]
    assert "search" in result["evidence_signals"]


def test_observation_corpus_has_required_families():
    cases = observation.load_observation_cases()
    families = {str(case.get("family")) for case in cases}
    assert len(cases) >= 10
    assert "root-cause-investigation" in families
    assert "architecture-planning" in families
    assert "broad-discovery" in families
    assert "test-to-source" in families


def test_observation_report_captures_agent_calls(monkeypatch, tmp_path: Path):
    repo = _fixture_repo(tmp_path)
    monkeypatch.setattr(observation, "_ensure_frontier_client_ready", lambda client: None)
    monkeypatch.setattr(observation, "_ensure_clean_git_repository", lambda repository: repo)
    monkeypatch.setattr(observation, "_prepare_observation_workspace", lambda *args, **kwargs: repo)
    monkeypatch.setattr(observation, "benchmark_root", lambda: tmp_path / "bench")
    monkeypatch.setattr(observation, "load_observation_cases", lambda: [{
        "id": "obs-1",
        "family": "broad-discovery",
        "task": "Find the implementation.",
    }])

    def fake_subprocess_run(args, **kwargs):
        if args[-1] == "HEAD":
            return SimpleNamespace(stdout="abc123\n", stderr="", returncode=0)
        if args[-1] == "--version":
            return SimpleNamespace(stdout="claude 9.9.9\n", stderr="", returncode=0)
        raise AssertionError(args)

    monkeypatch.setattr(observation.subprocess, "run", fake_subprocess_run)
    stdout = "\n".join([
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"tool-1","name":"Agent","input":{"subagent_type":"Explore","description":"Find provider code","prompt":"Search and locate provider state handling."}}]}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"done","usage":{"input_tokens":10,"output_tokens":5}}',
    ])
    monkeypatch.setattr(
        observation,
        "_run_command",
        lambda *args, **kwargs: SimpleNamespace(stdout=stdout, stderr="", returncode=0),
    )

    report = observation.run_claude_subagent_observation(
        str(repo),
        study="observe-test",
    )

    assert report["summary"]["total_subagent_calls"] == 1
    assert report["summary"]["subagent_types"] == {"Explore": 1}
    assert report["summary"]["delegated_request_classes"]["evidence-acquisition"] == 1
    assert report["runs"][0]["subagents"][0]["prompt"] == "Search and locate provider state handling."
