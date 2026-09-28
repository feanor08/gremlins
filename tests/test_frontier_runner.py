from pathlib import Path
import subprocess
from types import SimpleNamespace

import gremlins.benchmark as benchmark
import gremlins.frontier_runner as frontier_runner
from gremlins.frontier_runner import (
    _claude_benchmark_env,
    _claude_command,
    _codex_command,
    _ensure_clean_git_repository,
    _ensure_frontier_client_ready,
    _ensure_study_provenance,
    _parse_claude_stream,
    _parse_codex_stream,
    _redo_marker,
    _require_valid_frontier_run,
    _structural_acceptance,
)


def test_parse_claude_stream_usage_and_result():
    stdout = "\n".join([
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"Agent","id":"tool-1","input":{"prompt":"search"}}]}}',
        '{"type":"result","subtype":"success","is_error":false,"duration_ms":1234,"num_turns":3,"result":"Found src/gremlins/provider.py","total_cost_usd":0.0123,"usage":{"input_tokens":300,"cache_read_input_tokens":700,"cache_creation_input_tokens":100,"output_tokens":50}}',
    ])
    result = _parse_claude_stream(stdout, 0, 1.5)
    assert result.success is True
    assert result.response_text == "Found src/gremlins/provider.py"
    assert result.usage.uncached_input_tokens == 300
    assert result.usage.cache_read_tokens == 700
    assert result.usage.cache_write_tokens == 100
    assert result.usage.output_tokens == 50
    assert result.cost_usd == 0.0123
    assert result.metadata["subagent_calls"] == 1
    assert "Agent" in result.metadata["tool_names"]


def test_invalid_frontier_run_is_rejected():
    parsed = _parse_claude_stream("", 2, 0.1)
    try:
        _require_valid_frontier_run("claude", parsed, 2, "auth failed")
    except RuntimeError as exc:
        assert "benchmark invocation failed" in str(exc)
        assert "auth failed" in str(exc)
    else:
        raise AssertionError("failed client invocation must not become a benchmark record")


def test_zero_usage_frontier_run_is_rejected():
    parsed = _parse_claude_stream(
        '{"type":"result","subtype":"success","is_error":false,"result":"answer","usage":{"input_tokens":0,"output_tokens":0}}',
        0,
        0.1,
    )
    try:
        _require_valid_frontier_run("claude", parsed, 0, "")
    except RuntimeError as exc:
        assert "zero frontier usage tokens" in str(exc)
    else:
        raise AssertionError("zero-token frontier run must not become a benchmark record")


def test_parse_codex_stream_usage_and_result():
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"abc"}',
        '{"type":"item.completed","item":{"id":"1","type":"agent_message","text":"Found src/gremlins/provider.py"}}',
        '{"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":600,"output_tokens":120}}',
    ])
    result = _parse_codex_stream(stdout, 0, 2.0)
    assert result.success is True
    assert result.usage.uncached_input_tokens == 400
    assert result.usage.cache_read_tokens == 600
    assert result.usage.output_tokens == 120


def test_claude_benchmark_env_eager_loads_local_mcp(monkeypatch):
    monkeypatch.setenv("GREMLINS_TEST_SENTINEL", "present")
    env = _claude_benchmark_env()
    assert env["GREMLINS_TEST_SENTINEL"] == "present"
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert env["ENABLE_TOOL_SEARCH"] == "false"


def test_commands_are_noninteractive_and_read_only(tmp_path: Path):
    claude = _claude_command("task", "model-x")
    assert "-p" in claude
    assert claude[claude.index("-p") + 1] == "task"
    assert claude.index("task") < claude.index("--disallowedTools")
    assert "stream-json" in claude
    assert "plan" in claude
    assert "--max-turns" in claude
    assert "--no-session-persistence" in claude
    assert "--disallowedTools" in claude
    assert "Agent" in claude

    claude_a = _claude_command("task", "model-x", allow_agents=True)
    assert "--disallowedTools" not in claude_a

    claude_c = _claude_command(
        "task",
        "model-x",
        allowed_tools=["mcp__gremlins__repo_explorer"],
        disallowed_tools=["mcp__gremlins__repo_search", "mcp__gremlins__code_read"],
        permission_mode="dontAsk",
    )
    assert "--allowedTools" in claude_c
    assert "mcp__gremlins__repo_explorer" in claude_c
    assert "--disallowedTools" in claude_c
    assert "mcp__gremlins__repo_search" in claude_c
    assert "mcp__gremlins__code_read" in claude_c
    assert claude_c.index("task") < claude_c.index("--allowedTools")
    assert claude_c[claude_c.index("--permission-mode") + 1] == "dontAsk"

    codex = _codex_command("task", tmp_path, "model-y")
    assert codex[0] == "codex"
    assert "agents.enabled=false" in codex
    assert "exec" in codex
    assert "--json" in codex
    assert "--ephemeral" in codex
    assert "read-only" in codex
    assert str(tmp_path) in codex


def test_structural_acceptance_and_redo_marker():
    accepted, missing_paths, missing_claims = _structural_acceptance(
        "src/gremlins/provider.py and src/gremlins/workers.py define ProviderBusy and return status busy\nFRONTIER_REDO_SEARCH=false",
        ["src/gremlins/provider.py", "src/gremlins/workers.py"],
        [["ProviderBusy"], ["busy"]],
    )
    assert accepted is True
    assert missing_paths == []
    assert missing_claims == []
    assert _redo_marker("FRONTIER_REDO_SEARCH=false") is False
    assert _redo_marker("FRONTIER_REDO_SEARCH=true") is True
    assert _redo_marker("no marker") is None


def test_single_run_provenance_rejects_legacy_rows_without_metadata(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "load_records", lambda study: [{"case_id": "old"}])
    monkeypatch.setattr(frontier_runner, "load_study_metadata", lambda study: {})

    try:
        _ensure_study_provenance("legacy", tmp_path, "claude", None, False)
    except RuntimeError as exc:
        assert "existing records without immutable provenance" in str(exc)
        assert "fresh --study name" in str(exc)
    else:
        raise AssertionError("legacy rows without provenance must be rejected")


def test_single_run_provenance_written_and_checked(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "load_records", lambda study: [])
    monkeypatch.setattr(frontier_runner, "load_study_metadata", lambda study: {})

    def fake_run(args, **kwargs):
        if args[-1] == "HEAD":
            return SimpleNamespace(stdout="abc123\n", stderr="", returncode=0)
        if args[-1] == "--version":
            return SimpleNamespace(stdout="claude 9.9.9\n", stderr="", returncode=0)
        raise AssertionError(args)

    monkeypatch.setattr(frontier_runner.subprocess, "run", fake_run)
    captured = {}
    monkeypatch.setattr(frontier_runner, "assert_study_compatible", lambda study, payload: captured.setdefault("checked", payload))
    monkeypatch.setattr(frontier_runner, "write_study_metadata", lambda study, payload: captured.setdefault("written", payload))

    payload = _ensure_study_provenance("fresh", tmp_path, "claude", None, False)
    assert payload["source_head"] == "abc123"
    assert payload["client_version"] == "claude 9.9.9"
    assert payload["gremlins_head"] == "abc123"
    assert captured["checked"] == payload
    assert captured["written"] == payload


def test_claude_auth_preflight_rejects_logged_out_client(monkeypatch):
    monkeypatch.setattr(frontier_runner.shutil, "which", lambda client: "/usr/local/bin/claude")
    monkeypatch.setattr(
        frontier_runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout='{"loggedIn":false}',
            stderr="",
        ),
    )
    try:
        _ensure_frontier_client_ready("claude")
    except RuntimeError as exc:
        assert "authentication is not ready" in str(exc)
        assert "claude auth login" in str(exc)
    else:
        raise AssertionError("logged-out Claude client must fail benchmark preflight")


def test_claude_auth_preflight_accepts_logged_in_client(monkeypatch):
    monkeypatch.setattr(frontier_runner.shutil, "which", lambda client: "/usr/local/bin/claude")
    monkeypatch.setattr(
        frontier_runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"loggedIn":true}',
            stderr="",
        ),
    )
    _ensure_frontier_client_ready("claude")


def test_prepare_workspace_excludes_benchmark_answer_key(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    (source / "src").mkdir()
    (source / "src" / "app.py").write_text("TOKEN = 1\n", encoding="utf-8")
    (source / "evals" / "pilot").mkdir(parents=True)
    (source / "evals" / "pilot" / "cases.json").write_text('{"answer":"TOKEN"}\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=source, check=True)

    monkeypatch.setattr(frontier_runner, "benchmark_root", lambda: tmp_path / "bench")
    workspace = frontier_runner._prepare_workspace(source, "study", "case", "B", 1)

    assert (workspace / "src" / "app.py").is_file()
    assert not (workspace / "evals").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert status.stdout.strip() == ""


def test_benchmark_source_must_be_clean(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "a.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    assert _ensure_clean_git_repository(repo) == repo.resolve()

    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    try:
        _ensure_clean_git_repository(repo)
    except RuntimeError as exc:
        assert "must be clean" in str(exc)
    else:
        raise AssertionError("dirty repository should be rejected")


def test_codex_arm_a_can_keep_normal_agent_setting(tmp_path: Path):
    codex = _codex_command("task", tmp_path, None, allow_agents=True)
    assert "agents.enabled=false" not in codex


def test_c_arm_without_gremlins_call_is_invalid(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "_ensure_frontier_client_ready", lambda client: None)
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(frontier_runner, "_prepare_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(frontier_runner, "_ensure_study_provenance", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        frontier_runner,
        "_run_command",
        lambda *args, **kwargs: SimpleNamespace(
            stdout='{"type":"result","subtype":"success","is_error":false,"result":"src/gremlins/provider.py ProviderBusy busy FRONTIER_REDO_SEARCH=false","usage":{"input_tokens":10,"output_tokens":5}}',
            stderr="",
            returncode=0,
        ),
    )
    monkeypatch.setattr(frontier_runner, "gremlins_stats_for_tag", lambda tag: {
        "tag": tag,
        "calls": 0,
        "local_model_calls": 0,
        "result_chars": 0,
        "elapsed_seconds": 0,
        "statuses": {},
    })
    fake_case = {
        "id": "case-1",
        "task": "x",
        "expected_paths": ["src/gremlins/provider.py"],
        "expected_claims": [["ProviderBusy"], ["busy"]],
    }
    monkeypatch.setattr(frontier_runner, "get_pilot_case", lambda case_id: fake_case)
    monkeypatch.setattr(benchmark, "get_pilot_case", lambda case_id: fake_case)

    try:
        frontier_runner.run_frontier_case(
            study="invalid-c",
            repository=str(tmp_path),
            client="claude",
            case_id="case-1",
            arm="C",
        )
    except RuntimeError as exc:
        assert "without any tagged Gremlins calls" in str(exc)
    else:
        raise AssertionError("C arm without Gremlins calls must be rejected")


def test_c_arm_with_repeated_gremlins_calls_is_invalid(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "_ensure_frontier_client_ready", lambda client: None)
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(frontier_runner, "_prepare_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(frontier_runner, "_ensure_study_provenance", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        frontier_runner,
        "_run_command",
        lambda *args, **kwargs: SimpleNamespace(
            stdout='{"type":"result","subtype":"success","is_error":false,"result":"src/gremlins/provider.py ProviderBusy busy FRONTIER_REDO_SEARCH=false","usage":{"input_tokens":10,"output_tokens":5}}',
            stderr="",
            returncode=0,
        ),
    )
    monkeypatch.setattr(frontier_runner, "gremlins_stats_for_tag", lambda tag: {
        "tag": tag,
        "calls": 2,
        "local_model_calls": 0,
        "result_chars": 100,
        "elapsed_seconds": 0.1,
        "statuses": {"complete": 2},
    })
    fake_case = {
        "id": "case-1",
        "task": "x",
        "expected_paths": ["src/gremlins/provider.py"],
        "expected_claims": [["ProviderBusy"], ["busy"]],
    }
    monkeypatch.setattr(frontier_runner, "get_pilot_case", lambda case_id: fake_case)
    monkeypatch.setattr(benchmark, "get_pilot_case", lambda case_id: fake_case)

    try:
        frontier_runner.run_frontier_case(
            study="repeated-c",
            repository=str(tmp_path),
            client="claude",
            case_id="case-1",
            arm="C",
        )
    except RuntimeError as exc:
        assert "exactly one tagged repo_explorer call" in str(exc)
    else:
        raise AssertionError("C arm with repeated Gremlins calls must be rejected")


def test_suite_resumes_existing_case_arm(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(benchmark, "benchmark_root", lambda: tmp_path)
    monkeypatch.setattr(benchmark, "load_pilot_cases", lambda: [{"id": "case-1"}])
    monkeypatch.setattr(
        frontier_runner,
        "load_records",
        lambda study: [{"case_id": "case-1", "arm": "B", "iteration": 1}],
    )
    monkeypatch.setattr(
        frontier_runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="fake-version\n", stderr="", returncode=0),
    )

    calls = []

    def fake_run(**kwargs):
        calls.append((kwargs["case_id"], kwargs["arm"], kwargs["iteration"]))
        return {
            "accepted": True,
            "usage": {},
            "elapsed_seconds": 1.0,
            "gremlins": None,
        }

    monkeypatch.setattr(frontier_runner, "run_frontier_case", fake_run)

    result = frontier_runner.run_frontier_suite(
        study="resume",
        repository=str(tmp_path),
        client="claude",
        repeats=1,
    )
    assert result["runs_skipped_existing"] == 1
    assert calls == [("case-1", "C", 1)]
    assert result["runs_completed"] == 1


def test_hidden_claim_acceptance_rejects_filename_only_answer():
    accepted, missing_paths, missing_claims = _structural_acceptance(
        "See src/gremlins/provider.py and src/gremlins/workers.py.",
        ["src/gremlins/provider.py", "src/gremlins/workers.py"],
        [["ProviderBusy"], ["busy"]],
    )
    assert accepted is False
    assert missing_paths == []
    assert missing_claims == [["ProviderBusy"], ["busy"]]
