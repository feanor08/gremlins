from pathlib import Path
import subprocess
from types import SimpleNamespace

import gremlins.benchmark as benchmark
import gremlins.frontier_runner as frontier_runner
from gremlins.frontier_runner import (
    _claude_benchmark_env,
    _claude_benchmark_tool_policy,
    _claude_command,
    _codex_command,
    _ensure_clean_git_repository,
    _ensure_frontier_client_ready,
    _ensure_study_provenance,
    frontier_preflight,
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


def test_parse_claude_stream_tracks_direct_frontier_evidence_separately_from_gremlins():
    stdout = "\n".join([
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"Read","id":"r1","input":{"file_path":"src/a.py"}},{"type":"tool_use","name":"mcp__gremlins__evidence_pack","id":"g1","input":{"task":"find evidence"}},{"type":"tool_use","name":"Bash","id":"b1","input":{"command":"git status --short"}}]}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"done","usage":{"input_tokens":10,"output_tokens":5}}',
    ])
    result = _parse_claude_stream(stdout, 0, 1.0)
    assert result.metadata["frontier_direct_tool_calls"] == 2
    assert result.metadata["frontier_direct_evidence_calls"] == 2
    assert result.metadata["frontier_gremlins_tool_calls"] == 1


def test_parse_codex_stream_tracks_command_and_gremlins_calls():
    stdout = "\n".join([
        '{"type":"item.completed","item":{"type":"command_execution","command":"git log -n 3"}}',
        '{"type":"item.completed","item":{"type":"mcp_tool_call","server":"gremlins","tool":"evidence_pack"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":20}}',
    ])
    result = _parse_codex_stream(stdout, 0, 1.0)
    assert result.metadata["frontier_direct_tool_calls"] == 1
    assert result.metadata["frontier_direct_evidence_calls"] == 1
    assert result.metadata["frontier_gremlins_tool_calls"] == 1


def test_parse_codex_stream_classifies_wrapped_repo_search_but_not_skill_read():
    stdout = "\n".join([
        '{"type":"item.completed","item":{"type":"command_execution","command":"/bin/zsh -lc \\\"sed -n \'1,240p\' /Users/test/.codex/skills/gremlins-delegation/SKILL.md\\\""}}',
        '{"type":"item.completed","item":{"type":"mcp_tool_call","server":"gremlins","tool":"evidence_pack"}}',
        '{"type":"item.completed","item":{"type":"command_execution","command":"/bin/zsh -lc \\\"rg -n \'effective_terms\' src/gremlins/evidence_service.py\\\""}}',
        '{"type":"item.completed","item":{"type":"mcp_tool_call","server":"gremlins","tool":"evidence_pack","error":"MCP tool call requires approval, but approval policy is never"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":20}}',
    ])
    result = _parse_codex_stream(stdout, 0, 1.0)
    assert result.metadata["frontier_direct_tool_calls"] == 2
    assert result.metadata["frontier_direct_evidence_calls"] == 1
    assert result.metadata["frontier_gremlins_tool_calls"] == 2
    assert len(result.metadata["permission_denials"]) == 1


def test_invalid_frontier_run_is_rejected():
    parsed = _parse_claude_stream("", 2, 0.1)
    try:
        _require_valid_frontier_run("claude", parsed, 2, "auth failed")
    except RuntimeError as exc:
        assert "benchmark invocation failed" in str(exc)
        assert "auth failed" in str(exc)
    else:
        raise AssertionError("failed client invocation must not become a benchmark record")


def test_frontier_run_with_permission_denial_is_rejected(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "_ensure_frontier_client_ready", lambda client: None)
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(frontier_runner, "_prepare_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(frontier_runner, "_ensure_study_provenance", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        frontier_runner,
        "_run_command",
        lambda *args, **kwargs: SimpleNamespace(
            stdout='{"type":"result","subtype":"success","is_error":false,"result":"src/gremlins/provider.py inference.lock 3.0","usage":{"input_tokens":10,"output_tokens":5},"permission_denials":[{"tool_name":"Bash"}]}',
            stderr="",
            returncode=0,
        ),
    )
    fake_case = {
        "id": "case-1",
        "task": "x",
        "expected_paths": ["src/gremlins/provider.py"],
        "expected_claims": [["inference.lock"], ["3.0"]],
    }
    monkeypatch.setattr(frontier_runner, "get_pilot_case", lambda case_id: fake_case)
    monkeypatch.setattr(benchmark, "get_pilot_case", lambda case_id: fake_case)

    try:
        frontier_runner.run_frontier_case(
            study="permission-denial",
            repository=str(tmp_path),
            client="claude",
            case_id="case-1",
            arm="B",
        )
    except RuntimeError as exc:
        assert "permission denials" in str(exc)
    else:
        raise AssertionError("permission-denied benchmark run must be invalid")


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


def test_claude_benchmark_tool_policy_is_native_read_only():
    allowed_b, denied_b = _claude_benchmark_tool_policy("B")
    assert allowed_b == ["Read", "Grep", "Glob"]
    assert "Bash" in denied_b
    assert "WebSearch" in denied_b
    assert "Write" in denied_b
    assert "mcp__gremlins__evidence_pack" in denied_b

    allowed_c, denied_c = _claude_benchmark_tool_policy("C")
    assert allowed_c == ["Read", "Grep", "Glob", "mcp__gremlins__evidence_pack"]
    assert "Bash" in denied_c
    assert "WebSearch" in denied_c
    assert "Write" in denied_c
    assert "mcp__gremlins__evidence_pack" not in denied_c
    assert "mcp__gremlins__repo_explorer" in denied_c


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
        allowed_tools=["mcp__gremlins__evidence_pack"],
        disallowed_tools=["mcp__gremlins__repo_explorer", "mcp__gremlins__repo_search"],
        permission_mode="dontAsk",
    )
    assert "--allowedTools" in claude_c
    assert "mcp__gremlins__evidence_pack" in claude_c
    assert "--disallowedTools" in claude_c
    assert "mcp__gremlins__repo_explorer" in claude_c
    assert "mcp__gremlins__repo_search" in claude_c
    assert claude_c.index("task") < claude_c.index("--allowedTools")
    assert claude_c[claude_c.index("--permission-mode") + 1] == "dontAsk"

    codex = _codex_command("task", tmp_path, "model-y")
    assert codex[0] == "codex"
    assert 'approval_policy="never"' in codex
    assert "agents.enabled=false" in codex
    assert "exec" in codex
    assert "--json" in codex
    assert "--ephemeral" in codex
    assert "read-only" in codex
    assert codex[codex.index("--sandbox") + 1] == "read-only"
    assert str(tmp_path) in codex

    codex_b = _codex_command(
        "task",
        tmp_path,
        "model-y",
        gremlins_mode="disabled",
    )
    assert 'approval_policy="never"' in codex_b
    assert "mcp_servers.gremlins.enabled=false" in codex_b
    assert codex_b[codex_b.index("--sandbox") + 1] == "read-only"
    assert any(value.startswith("mcp_servers.gremlins.command=") for value in codex_b)
    assert not any(value.startswith("skills.config=") for value in codex_b)

    codex_c = _codex_command(
        "task",
        tmp_path,
        "model-y",
        gremlins_mode="evidence-pack-only",
    )
    assert 'approval_policy="never"' in codex_c
    assert "mcp_servers.gremlins.enabled=true" in codex_c
    assert codex_c[codex_c.index("--sandbox") + 1] == "read-only"
    assert any(value.startswith("mcp_servers.gremlins.command=") for value in codex_c)
    assert 'mcp_servers.gremlins.enabled_tools=["evidence_pack"]' in codex_c
    assert 'mcp_servers.gremlins.tools.evidence_pack.approval_mode="approve"' in codex_c
    assert 'mcp_servers.gremlins.env_vars=["GREMLINS_MEASUREMENT_TAG","GREMLINS_STATE_DIR"]' in codex_c
    assert not any(value.startswith("skills.config=") for value in codex_c)


def test_codex_benchmark_env_isolates_user_home_and_resets_skill_state(monkeypatch, tmp_path: Path):
    real_codex_home = tmp_path / "real-codex"
    real_codex_home.mkdir()
    (real_codex_home / "auth.json").write_text('{"token":"test"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    monkeypatch.setattr(frontier_runner, "benchmark_root", lambda: tmp_path / "benchmarks")

    env = frontier_runner._codex_benchmark_env()
    home = Path(env["HOME"])
    codex_home = Path(env["CODEX_HOME"])
    assert home == tmp_path / "benchmarks" / "codex-isolated-home"
    assert codex_home == home / ".codex"
    assert env["GREMLINS_STATE_DIR"] == str(tmp_path)
    assert (codex_home / "auth.json").read_text(encoding="utf-8") == '{"token":"test"}'
    assert not (home / ".agents").exists()
    assert not (codex_home / "skills").exists()

    # Any state created by one controlled child is removed before the next.
    poison = codex_home / "skills" / "gremlins-delegation"
    poison.mkdir(parents=True)
    (poison / "SKILL.md").write_text("poison", encoding="utf-8")
    env2 = frontier_runner._codex_benchmark_env()
    assert env2["HOME"] == str(home)
    assert not (Path(env2["CODEX_HOME"]) / "skills").exists()


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


def test_frontier_preflight_checks_wrapper_registration_and_tool_surface(monkeypatch, tmp_path: Path):
    wrapper = tmp_path / "gremlins-mcp"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    wrapper.write_text(
        f"#!/bin/sh\nexport GREMLINS_ROOT='{runtime}'\nexec python -m gremlins.server\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(frontier_runner.Path, "expanduser", lambda self: wrapper if str(self) == "~/.local/bin/gremlins-mcp" else self)
    monkeypatch.setattr(frontier_runner, "project_root", lambda: runtime)
    monkeypatch.setattr(frontier_runner, "_ensure_frontier_client_ready", lambda client: None)
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: repo)
    monkeypatch.setattr(frontier_runner, "check_wrapper", lambda path: {
        "ok": True,
        "tools": ["evidence_pack", "repo_search"],
        "missing_tools": [],
    })
    monkeypatch.setattr(frontier_runner, "check_python_module", lambda: {
        "ok": True,
        "tools": ["evidence_pack"],
        "missing_tools": [],
    })
    monkeypatch.setattr(frontier_runner, "_mcp_registration_state", lambda client: {"ok": True})
    monkeypatch.setattr(frontier_runner, "_codex_c_treatment_config_state", lambda: {"ok": True})

    result = frontier_preflight(repo, "claude")
    assert result["ok"] is True
    assert result["checks"]["evidence_pack_present"] is True
    assert result["frontier_model_calls"] == 0


def test_codex_preflight_validates_exact_c_treatment_config(monkeypatch):
    captured = []

    def fake_run(args, **kwargs):
        captured.append((args, kwargs.get("env")))
        if args[:3] == ["codex", "login", "status"]:
            return SimpleNamespace(returncode=0, stdout="Logged in\n", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="gremlins\n  enabled: true\n  enabled_tools: evidence_pack\n",
            stderr="",
        )

    isolated_env = {
        "HOME": "/tmp/gremlins-codex-home",
        "CODEX_HOME": "/tmp/gremlins-codex-home/.codex",
        "GREMLINS_STATE_DIR": "/tmp/real-gremlins-state",
    }
    monkeypatch.setattr(frontier_runner, "_codex_benchmark_env", lambda: isolated_env)
    monkeypatch.setattr(
        frontier_runner,
        "_codex_workspace_policy_state",
        lambda env: {
            "ok": True,
            "repository": "/tmp/real-gremlins-state/benchmarks/codex-policy-preflight",
            "metrics_path": "/tmp/real-gremlins-state/jobs.jsonl",
        },
    )
    monkeypatch.setattr(frontier_runner.subprocess, "run", fake_run)
    result = frontier_runner._codex_c_treatment_config_state()
    assert result["ok"] is True
    assert result["auth_returncode"] == 0
    assert result["auth_status"] == "Logged in"
    assert captured[0] == (["codex", "login", "status"], isolated_env)
    command, env = captured[1]
    assert env == isolated_env
    assert any(value.startswith("mcp_servers.gremlins.command=") for value in command)
    assert 'mcp_servers.gremlins.tools.evidence_pack.approval_mode="approve"' in command
    assert 'mcp_servers.gremlins.env_vars=["GREMLINS_MEASUREMENT_TAG","GREMLINS_STATE_DIR"]' in command
    assert not any(value.startswith("skills.config=") for value in command)
    assert command[-3:] == ["mcp", "get", "gremlins"]
    assert result["gremlins_state_dir"] == "/tmp/real-gremlins-state"
    assert result["workspace_policy"]["ok"] is True


def test_codex_treatment_preflight_fails_closed_on_workspace_policy(monkeypatch):
    isolated_env = {
        "HOME": "/tmp/gremlins-codex-home",
        "CODEX_HOME": "/tmp/gremlins-codex-home/.codex",
        "GREMLINS_STATE_DIR": "/tmp/real-gremlins-state",
    }

    def fake_run(args, **kwargs):
        if args[:3] == ["codex", "login", "status"]:
            return SimpleNamespace(returncode=0, stdout="Logged in\n", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="gremlins\n  enabled: true\n  enabled_tools: evidence_pack\n",
            stderr="",
        )

    monkeypatch.setattr(frontier_runner, "_codex_benchmark_env", lambda: isolated_env)
    monkeypatch.setattr(
        frontier_runner,
        "_codex_workspace_policy_state",
        lambda env: {
            "ok": False,
            "returncode": 1,
            "stderr": "repository is outside allowed roots",
        },
    )
    monkeypatch.setattr(frontier_runner.subprocess, "run", fake_run)

    result = frontier_runner._codex_c_treatment_config_state()
    assert result["ok"] is False
    assert result["workspace_policy"]["ok"] is False


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


def test_claude_auth_preflight_accepts_headless_env_credential(monkeypatch):
    monkeypatch.setattr(frontier_runner.shutil, "which", lambda client: "/usr/local/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("interactive auth status must not be required for CI credentials")

    monkeypatch.setattr(frontier_runner.subprocess, "run", fail_if_called)
    _ensure_frontier_client_ready("claude")


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
    assert "mcp_servers.gremlins.enabled=false" not in codex
    assert "mcp_servers.gremlins.enabled=true" not in codex


def test_codex_rejects_unknown_gremlins_mode(tmp_path: Path):
    try:
        _codex_command("task", tmp_path, None, gremlins_mode="unknown")
    except ValueError as exc:
        assert "gremlins_mode" in str(exc)
    else:
        raise AssertionError("unknown Gremlins treatment must fail closed")


def test_c_arm_without_gremlins_call_is_invalid(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "frontier_preflight", lambda repository, client: {"ok": True})
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
        "workers": {},
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
        assert "without any tagged evidence_pack calls" in str(exc)
    else:
        raise AssertionError("C arm without Gremlins calls must be rejected")


def test_c_arm_allows_bounded_repeated_evidence_pack_calls(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "frontier_preflight", lambda repository, client: {"ok": True})
    monkeypatch.setattr(frontier_runner, "_ensure_frontier_client_ready", lambda client: None)
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(frontier_runner, "_prepare_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(frontier_runner, "_ensure_study_provenance", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        frontier_runner,
        "_run_command",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="\n".join([
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"mcp__gremlins__evidence_pack","id":"g1","input":{"task":"first"}},{"type":"tool_use","name":"mcp__gremlins__evidence_pack","id":"g2","input":{"task":"second"}}]}}',
                '{"type":"result","subtype":"success","is_error":false,"result":"src/gremlins/provider.py ProviderBusy busy FRONTIER_REDO_SEARCH=false","usage":{"input_tokens":10,"output_tokens":5}}',
            ]),
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
        "workers": {"evidence-pack": 2},
        "evidence_pack_details": ["broad", "focused"],
        "evidence_pack_budgets": [8000, 3600],
        "evidence_pack_result_chars": [7900, 3400],
    })
    monkeypatch.setattr(frontier_runner, "append_record", lambda study, record: tmp_path / "study.jsonl")
    fake_case = {
        "id": "case-1",
        "task": "x",
        "expected_paths": ["src/gremlins/provider.py"],
        "expected_claims": [["ProviderBusy"], ["busy"]],
    }
    monkeypatch.setattr(frontier_runner, "get_pilot_case", lambda case_id: fake_case)
    monkeypatch.setattr(benchmark, "get_pilot_case", lambda case_id: fake_case)

    result = frontier_runner.run_frontier_case(
        study="repeated-c",
        repository=str(tmp_path),
        client="claude",
        case_id="case-1",
        arm="C",
    )
    assert result["gremlins"]["calls"] == 2
    assert result["frontier_gremlins_tool_calls"] == 2
    assert result["accepted"] is True


def test_c_arm_rejects_nonfocused_followup_telemetry(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "frontier_preflight", lambda repository, client: {"ok": True})
    monkeypatch.setattr(frontier_runner, "_ensure_frontier_client_ready", lambda client: None)
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(frontier_runner, "_prepare_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(frontier_runner, "_ensure_study_provenance", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        frontier_runner,
        "_run_command",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="\n".join([
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"mcp__gremlins__evidence_pack","id":"g1","input":{"task":"first"}},{"type":"tool_use","name":"mcp__gremlins__evidence_pack","id":"g2","input":{"task":"second"}}]}}',
                '{"type":"result","subtype":"success","is_error":false,"result":"src/gremlins/provider.py ProviderBusy busy FRONTIER_REDO_SEARCH=false","usage":{"input_tokens":10,"output_tokens":5}}',
            ]),
            stderr="",
            returncode=0,
        ),
    )
    monkeypatch.setattr(frontier_runner, "gremlins_stats_for_tag", lambda tag: {
        "tag": tag,
        "calls": 2,
        "local_model_calls": 0,
        "result_chars": 15000,
        "elapsed_seconds": 0.1,
        "statuses": {"complete": 2},
        "workers": {"evidence-pack": 2},
        "evidence_pack_details": ["broad", "broad"],
        "evidence_pack_budgets": [8000, 8000],
        "evidence_pack_result_chars": [7900, 7900],
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
            study="bad-detail-c",
            repository=str(tmp_path),
            client="claude",
            case_id="case-1",
            arm="C",
        )
    except RuntimeError as exc:
        assert "follow-up evidence_pack calls must be focused" in str(exc)
    else:
        raise AssertionError("C arm with repeated broad evidence packs must be rejected")


def test_c_arm_rejects_focused_pack_over_actual_size_budget(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "frontier_preflight", lambda repository, client: {"ok": True})
    monkeypatch.setattr(frontier_runner, "_ensure_frontier_client_ready", lambda client: None)
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(frontier_runner, "_prepare_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(frontier_runner, "_ensure_study_provenance", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        frontier_runner,
        "_run_command",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="\n".join([
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"mcp__gremlins__evidence_pack","id":"g1","input":{"task":"first"}},{"type":"tool_use","name":"mcp__gremlins__evidence_pack","id":"g2","input":{"task":"second"}}]}}',
                '{"type":"result","subtype":"success","is_error":false,"result":"src/gremlins/provider.py ProviderBusy busy FRONTIER_REDO_SEARCH=false","usage":{"input_tokens":10,"output_tokens":5}}',
            ]),
            stderr="",
            returncode=0,
        ),
    )
    monkeypatch.setattr(frontier_runner, "gremlins_stats_for_tag", lambda tag: {
        "tag": tag,
        "calls": 2,
        "local_model_calls": 0,
        "result_chars": 11800,
        "elapsed_seconds": 0.1,
        "statuses": {"complete": 2},
        "workers": {"evidence-pack": 2},
        "evidence_pack_details": ["broad", "focused"],
        "evidence_pack_budgets": [8000, 3600],
        "evidence_pack_result_chars": [7900, 3900],
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
            study="oversize-focused-c",
            repository=str(tmp_path),
            client="claude",
            case_id="case-1",
            arm="C",
        )
    except RuntimeError as exc:
        assert "exceeded its declared result budget" in str(exc)
    else:
        raise AssertionError("C arm with oversized focused pack must be rejected")


def test_c_arm_uses_observed_frontier_evidence_for_redo(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "frontier_preflight", lambda repository, client: {"ok": True})
    monkeypatch.setattr(frontier_runner, "_ensure_frontier_client_ready", lambda client: None)
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(frontier_runner, "_prepare_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(frontier_runner, "_ensure_study_provenance", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        frontier_runner,
        "_run_command",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="\n".join([
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"mcp__gremlins__evidence_pack","id":"g1","input":{"task":"first"}},{"type":"tool_use","name":"Read","id":"r1","input":{"file_path":"src/gremlins/provider.py"}}]}}',
                '{"type":"result","subtype":"success","is_error":false,"result":"src/gremlins/provider.py ProviderBusy busy FRONTIER_REDO_SEARCH=false","usage":{"input_tokens":10,"output_tokens":5}}',
            ]),
            stderr="",
            returncode=0,
        ),
    )
    monkeypatch.setattr(frontier_runner, "gremlins_stats_for_tag", lambda tag: {
        "tag": tag,
        "calls": 1,
        "local_model_calls": 0,
        "result_chars": 100,
        "elapsed_seconds": 0.1,
        "statuses": {"complete": 1},
        "workers": {"evidence-pack": 1},
        "evidence_pack_details": ["broad"],
        "evidence_pack_budgets": [8000],
        "evidence_pack_result_chars": [7900],
    })
    monkeypatch.setattr(frontier_runner, "append_record", lambda study, record: tmp_path / "study.jsonl")
    fake_case = {
        "id": "case-1",
        "task": "x",
        "expected_paths": ["src/gremlins/provider.py"],
        "expected_claims": [["ProviderBusy"], ["busy"]],
    }
    monkeypatch.setattr(frontier_runner, "get_pilot_case", lambda case_id: fake_case)
    monkeypatch.setattr(benchmark, "get_pilot_case", lambda case_id: fake_case)

    result = frontier_runner.run_frontier_case(
        study="observed-redo",
        repository=str(tmp_path),
        client="claude",
        case_id="case-1",
        arm="C",
    )
    assert result["frontier_direct_evidence_calls"] == 1
    assert result["frontier_redid_search"] is True
    assert result["frontier_reported_redo_marker"] is False
    assert result["frontier_redo_marker_matches_observed"] is False


def test_c_arm_rejects_more_than_four_evidence_pack_calls(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "frontier_preflight", lambda repository, client: {"ok": True})
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
        "calls": 5,
        "local_model_calls": 0,
        "result_chars": 100,
        "elapsed_seconds": 0.1,
        "statuses": {"complete": 5},
        "workers": {"evidence-pack": 5},
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
            study="too-many-c",
            repository=str(tmp_path),
            client="claude",
            case_id="case-1",
            arm="C",
        )
    except RuntimeError as exc:
        assert "exceeded the four-call evidence-loop treatment bound" in str(exc)
    else:
        raise AssertionError("C arm with more than four evidence_pack calls must be rejected")


def test_single_run_rejects_duplicate_case_arm_iteration_without_force(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "_ensure_frontier_client_ready", lambda client: None)
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(frontier_runner, "load_records", lambda study: [
        {"case_id": "case-1", "arm": "B", "iteration": 1}
    ])
    fake_case = {
        "id": "case-1",
        "task": "x",
        "expected_paths": [],
        "expected_claims": [],
    }
    monkeypatch.setattr(frontier_runner, "get_pilot_case", lambda case_id: fake_case)

    try:
        frontier_runner.run_frontier_case(
            study="dup",
            repository=str(tmp_path),
            client="claude",
            case_id="case-1",
            arm="B",
            iteration=1,
        )
    except RuntimeError as exc:
        assert "already contains case-1/B/r1" in str(exc)
    else:
        raise AssertionError("duplicate manual benchmark run must be rejected")


def test_suite_preflight_fails_before_any_frontier_run(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(benchmark, "load_pilot_cases", lambda: [{"id": "case-1"}])
    monkeypatch.setattr(frontier_runner, "frontier_preflight", lambda repository, client: {
        "ok": False,
        "frontier_model_calls": 0,
        "checks": {"client_registration_present": False},
    })

    calls = []
    monkeypatch.setattr(frontier_runner, "run_frontier_case", lambda **kwargs: calls.append(kwargs))

    try:
        frontier_runner.run_frontier_suite(
            study="preflight-fail",
            repository=str(tmp_path),
            client="claude",
            repeats=1,
        )
    except RuntimeError as exc:
        assert "before the suite could spend any benchmark model calls" in str(exc)
    else:
        raise AssertionError("suite must fail closed when treatment preflight fails")
    assert calls == []


def test_suite_resumes_existing_case_arm(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(frontier_runner, "frontier_preflight", lambda repository, client: {"ok": True})
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


def test_suite_stops_after_first_execution_error(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(frontier_runner, "frontier_preflight", lambda repository, client: {"ok": True})
    monkeypatch.setattr(benchmark, "benchmark_root", lambda: tmp_path)
    monkeypatch.setattr(
        benchmark,
        "load_pilot_cases",
        lambda: [{"id": "case-1"}, {"id": "case-2"}],
    )
    monkeypatch.setattr(frontier_runner, "load_records", lambda study: [])
    monkeypatch.setattr(
        frontier_runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="fake-version\n", stderr="", returncode=0),
    )

    calls = []

    def fake_run(**kwargs):
        calls.append((kwargs["case_id"], kwargs["arm"]))
        raise RuntimeError("client failed")

    monkeypatch.setattr(frontier_runner, "run_frontier_case", fake_run)

    result = frontier_runner.run_frontier_suite(
        study="fail-fast",
        repository=str(tmp_path),
        client="codex",
        repeats=1,
    )
    assert calls == [("case-1", "C")]
    assert result["runs_completed"] == 0
    assert result["stopped_early"] is True
    assert result["stop_reason"]["kind"] == "execution_error"
    assert len(result["execution_errors"]) == 1


def test_suite_stops_after_c_native_repository_retrieval(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(frontier_runner, "_ensure_clean_git_repository", lambda repository: tmp_path)
    monkeypatch.setattr(frontier_runner, "frontier_preflight", lambda repository, client: {"ok": True})
    monkeypatch.setattr(benchmark, "benchmark_root", lambda: tmp_path)
    monkeypatch.setattr(
        benchmark,
        "load_pilot_cases",
        lambda: [{"id": "case-1"}, {"id": "case-2"}],
    )
    monkeypatch.setattr(frontier_runner, "load_records", lambda study: [])
    monkeypatch.setattr(
        frontier_runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="fake-version\n", stderr="", returncode=0),
    )

    calls = []

    def fake_run(**kwargs):
        calls.append((kwargs["case_id"], kwargs["arm"]))
        return {
            "accepted": True,
            "usage": {},
            "elapsed_seconds": 1.0,
            "gremlins": {"calls": 2},
            "frontier_direct_evidence_calls": 1,
            "raw": {"stdout": "/tmp/case-1-C.stdout.jsonl"},
        }

    monkeypatch.setattr(frontier_runner, "run_frontier_case", fake_run)

    result = frontier_runner.run_frontier_suite(
        study="native-stop",
        repository=str(tmp_path),
        client="codex",
        repeats=1,
    )
    assert calls == [("case-1", "C")]
    assert result["runs_completed"] == 1
    assert result["stopped_early"] is True
    assert result["stop_reason"] == {
        "kind": "c_native_repository_retrieval",
        "case_id": "case-1",
        "arm": "C",
        "iteration": 1,
        "frontier_direct_evidence_calls": 1,
        "raw": {"stdout": "/tmp/case-1-C.stdout.jsonl"},
    }
    assert result["execution_errors"] == []


def test_hidden_claim_acceptance_rejects_filename_only_answer():
    accepted, missing_paths, missing_claims = _structural_acceptance(
        "See src/gremlins/provider.py and src/gremlins/workers.py.",
        ["src/gremlins/provider.py", "src/gremlins/workers.py"],
        [["ProviderBusy"], ["busy"]],
    )
    assert accepted is False
    assert missing_paths == []
    assert missing_claims == [["ProviderBusy"], ["busy"]]


def test_parse_claude_stream_captures_subagent_details_and_parent_metadata():
    stdout = "\n".join([
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"agent-1","name":"Agent","input":{"subagent_type":"general-purpose","description":"Investigate failure","prompt":"Search logs and explain root cause","model":"sonnet"}}]}}',
        '{"type":"assistant","parent_tool_use_id":"agent-1","message":{"role":"assistant","content":[{"type":"tool_use","id":"agent-2","name":"Agent","input":{"subagent_type":"Explore","description":"Find callers","prompt":"Locate all callers"}}]}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"done","usage":{"input_tokens":10,"output_tokens":5}}',
    ])
    result = _parse_claude_stream(stdout, 0, 1.0)
    assert result.metadata["subagent_calls"] == 2
    first, second = result.metadata["subagent_details"]
    assert first["subagent_type"] == "general-purpose"
    assert first["prompt"] == "Search logs and explain root cause"
    assert second["subagent_type"] == "Explore"
    assert second["parent_tool_use_id"] == "agent-1"
    assert second["observed_depth"] == 1


def test_claude_command_can_isolate_setting_sources():
    command = _claude_command(
        "task",
        None,
        allow_agents=True,
        setting_sources="project",
    )
    assert "--setting-sources" in command
    assert command[command.index("--setting-sources") + 1] == "project"
