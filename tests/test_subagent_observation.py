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


def test_raw_analyzer_counts_direct_parent_evidence(monkeypatch, tmp_path: Path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    stream = "\n".join([
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"r1","name":"Read","input":{"file_path":"src/a.py"}},{"type":"tool_use","id":"a1","name":"Agent","input":{"subagent_type":"Explore","description":"find x","prompt":"search for x"}}]}}',
        '{"type":"assistant","parent_tool_use_id":"a1","message":{"role":"assistant","content":[{"type":"tool_use","id":"g1","name":"Grep","input":{"pattern":"x"}}]}}',
        '{"type":"result","subtype":"success","is_error":false,"num_turns":3,"total_cost_usd":0.12,"usage":{"input_tokens":2,"cache_creation_input_tokens":10,"cache_read_input_tokens":20,"output_tokens":5,"output_tokens_details":{"thinking_tokens":1}},"subagent_stats":{"spawned":1}}',
    ])
    (raw_dir / "obs-001-observe-r1.stdout.jsonl").write_text(stream, encoding="utf-8")
    monkeypatch.setattr(observation, "load_observation_cases", lambda: [{
        "id": "obs-001",
        "family": "broad-discovery",
    }])

    report = observation.analyze_claude_observation_raw(
        "study",
        raw_dir=str(raw_dir),
    )

    assert report["summary"]["runs_analyzed"] == 1
    assert report["summary"]["agent_calls_observed"] == 1
    assert report["summary"]["subagents_reported_spawned"] == 1
    assert report["summary"]["root_tool_calls"] == 2
    assert report["summary"]["root_nonagent_tool_calls"] == 1
    assert report["summary"]["direct_evidence_tool_calls"] == 1
    assert report["summary"]["nested_tool_calls_visible"] == 1
    assert report["summary"]["total_cost_usd"] == 0.12
    assert report["cases"][0]["tool_activity"]["direct_evidence_tool_names"] == {"Read": 1}


def test_raw_analyzer_includes_max_turn_results(monkeypatch, tmp_path: Path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    stream = "\n".join([
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"b1","name":"Bash","input":{"command":"git log --oneline -5"}}]}}',
        '{"type":"result","subtype":"error_max_turns","terminal_reason":"max_turns","is_error":true,"num_turns":13,"total_cost_usd":0.5,"usage":{"input_tokens":3,"cache_creation_input_tokens":30,"cache_read_input_tokens":300,"output_tokens":20},"subagent_stats":{"spawned":0}}',
    ])
    (raw_dir / "obs-004-observe-r1.stdout.jsonl").write_text(stream, encoding="utf-8")
    monkeypatch.setattr(observation, "load_observation_cases", lambda: [{
        "id": "obs-004",
        "family": "root-cause-investigation",
    }])

    report = observation.analyze_claude_observation_raw(
        "study",
        raw_dir=str(raw_dir),
    )

    assert report["summary"]["max_turn_results"] == 1
    assert report["summary"]["successful_terminal_results"] == 0
    assert report["summary"]["direct_evidence_tool_calls"] == 1
    assert report["summary"]["runs_with_direct_evidence_and_no_agent"] == 1
