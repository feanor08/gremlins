from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any
import uuid

from .benchmark import (
    BenchmarkRecord,
    append_record,
    benchmark_root,
    build_arm_prompt,
    get_pilot_case,
    gremlins_stats_for_tag,
    next_missing_run,
    normalize_usage,
    write_study_metadata,
    assert_study_compatible,
    load_records,
    load_study_metadata,
)


@dataclass(frozen=True)
class ClientRun:
    success: bool
    response_text: str
    usage: Any
    elapsed_seconds: float
    cost_usd: float | None
    raw_stdout: str
    raw_stderr: str
    metadata: dict


def _run_command(
    args: list[str],
    cwd: Path,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
        env=env,
    )


def _claude_benchmark_env() -> dict[str, str]:
    """Use a small eager-loaded MCP surface for controlled B/C measurements."""
    env = os.environ.copy()
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    env["ENABLE_TOOL_SEARCH"] = "false"
    return env


def _ensure_frontier_client_ready(client: str) -> None:
    if shutil.which(client) is None:
        raise RuntimeError(f"{client} CLI is not installed or not on PATH")
    if client != "claude":
        return

    # CI and other headless environments commonly provide Claude credentials
    # through environment variables instead of an interactive login. The
    # workflow performs a real one-turn auth smoke before the benchmark; here
    # we only avoid rejecting a valid noninteractive credential prematurely.
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return

    try:
        proc = subprocess.run(
            ["claude", "auth", "status"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("claude auth status timed out; re-authenticate before benchmarking") from exc

    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or "not logged in")[-2000:]
        raise RuntimeError(
            "claude authentication is not ready for noninteractive benchmarking: "
            f"{detail}. Run 'claude auth login' and verify 'claude auth status --text'."
        )


def _ensure_clean_git_repository(repository: str | Path) -> Path:
    repo = Path(repository).expanduser().resolve()
    if not repo.is_dir():
        raise RuntimeError(f"repository does not exist: {repo}")
    top = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if top.returncode != 0:
        raise RuntimeError(f"not a Git repository: {repo}")
    repo = Path(top.stdout.strip()).resolve()
    dirty = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if dirty.returncode != 0:
        raise RuntimeError("failed to inspect repository status")
    if dirty.stdout.strip():
        raise RuntimeError(
            "benchmark source repository must be clean so B and C inspect the same reproducible snapshot"
        )
    return repo


def _prepare_workspace(repository: Path, study: str, case_id: str, arm: str, iteration: int = 1) -> Path:
    root = benchmark_root() / "workspaces" / study
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / f"{case_id}-{arm}-r{max(1, int(iteration))}"
    if workspace.exists():
        shutil.rmtree(workspace)
    proc = subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(repository), str(workspace)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to create benchmark workspace: {proc.stderr.strip()}")

    # The pilot corpus contains hidden expected paths/claims under evals/. Keep
    # those files available to the harness in the source checkout, but remove
    # them from each arm's worktree so neither frontier search nor Gremlins can
    # retrieve the answer key. Sparse checkout preserves Git history.
    sparse = subprocess.run(
        ["git", "-C", str(workspace), "sparse-checkout", "set", "--no-cone", "/*", "!/evals/"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if sparse.returncode != 0:
        raise RuntimeError(f"failed to isolate benchmark answer data: {sparse.stderr.strip()}")
    return workspace


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _tool_names_from_content(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    names: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"tool_use", "server_tool_use"}:
            name = item.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def _claude_tool_uses(events: list[dict]) -> list[dict]:
    uses: list[dict] = []
    seen: set[tuple[str, str, int]] = set()

    def collect(content: Any, event: dict, event_index: int) -> None:
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in {"tool_use", "server_tool_use"}:
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            tool_id = str(item.get("id") or "")
            key = (tool_id, name, event_index)
            if key in seen:
                continue
            seen.add(key)
            tool_input = item.get("input")
            parent_id = (
                item.get("parent_tool_use_id")
                or event.get("parent_tool_use_id")
            )
            uses.append({
                "name": name,
                "id": tool_id or None,
                "input": tool_input if isinstance(tool_input, dict) else {},
                "parent_tool_use_id": str(parent_id) if parent_id else None,
                "event_index": event_index,
            })

    for event_index, event in enumerate(events):
        message = event.get("message")
        if isinstance(message, dict):
            collect(message.get("content"), event, event_index)
        collect(event.get("content"), event, event_index)

    by_id = {
        str(use["id"]): use
        for use in uses
        if use.get("id")
    }
    for use in uses:
        parent = use.get("parent_tool_use_id")
        depth = 0
        seen_parents: set[str] = set()
        while parent and parent in by_id and parent not in seen_parents:
            seen_parents.add(parent)
            depth += 1
            parent = by_id[parent].get("parent_tool_use_id")
        use["observed_depth"] = depth if depth or not use.get("parent_tool_use_id") else None
    return uses


def _claude_tool_names(events: list[dict]) -> list[str]:
    return [str(use["name"]) for use in _claude_tool_uses(events)]


_FRONTIER_EVIDENCE_TOOL_NAMES = {
    "read",
    "grep",
    "glob",
    "search",
    "find",
    "toolsearch",
    "websearch",
    "webfetch",
    "ls",
}
_FRONTIER_READ_ONLY_BASH_RE = re.compile(
    r"(?:^|[;&|()]\s*)(?:rg|grep|find|ls|cat|sed|head|tail|wc|tree)\b"
    r"|(?:^|[;&|()]\s*)git\s+(?:log|show|blame|diff|status|grep|rev-parse)\b",
    re.IGNORECASE,
)


def _is_gremlins_tool_name(name: str) -> bool:
    low = name.lower()
    return low.startswith("mcp__gremlins__") or low.startswith("gremlins.")


def _is_frontier_evidence_use(use: dict) -> bool:
    name = str(use.get("name") or "").lower()
    if name in _FRONTIER_EVIDENCE_TOOL_NAMES:
        return True
    if name == "bash":
        payload = use.get("input") if isinstance(use.get("input"), dict) else {}
        command = str(payload.get("command") or "")
        return bool(_FRONTIER_READ_ONLY_BASH_RE.search(command))
    return False


def _claude_direct_tool_metrics(tool_uses: list[dict]) -> dict:
    root = [
        use for use in tool_uses
        if not use.get("parent_tool_use_id")
        and str(use.get("name") or "").lower() not in {"agent", "task"}
    ]
    direct = [
        use for use in root
        if not _is_gremlins_tool_name(str(use.get("name") or ""))
    ]
    evidence = [use for use in direct if _is_frontier_evidence_use(use)]
    gremlins = [
        use for use in root
        if _is_gremlins_tool_name(str(use.get("name") or ""))
    ]
    return {
        "frontier_direct_tool_calls": len(direct),
        "frontier_direct_evidence_calls": len(evidence),
        "frontier_gremlins_tool_calls": len(gremlins),
        "frontier_direct_tool_names": [str(use.get("name") or "") for use in direct],
        "frontier_direct_evidence_tool_names": [str(use.get("name") or "") for use in evidence],
        "frontier_gremlins_tool_names": [str(use.get("name") or "") for use in gremlins],
    }


def _parse_claude_stream(stdout: str, returncode: int, elapsed_seconds: float) -> ClientRun:
    events: list[dict] = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)

    final = next((item for item in reversed(events) if item.get("type") == "result"), {})
    response_text = str(final.get("result") or "").strip()

    if not response_text:
        assistant_text: list[str] = []
        for event in events:
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                text = _extract_text_from_content(message.get("content"))
                if text:
                    assistant_text.append(text)
            if event.get("type") == "assistant":
                text = _extract_text_from_content(event.get("content"))
                if text:
                    assistant_text.append(text)
        response_text = "\n".join(assistant_text).strip()

    usage_obj = final.get("usage") if isinstance(final.get("usage"), dict) else {}
    usage = normalize_usage(
        input_tokens=int(usage_obj.get("input_tokens") or 0),
        cached_input_tokens=int(usage_obj.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage_obj.get("cache_creation_input_tokens") or 0),
        output_tokens=int(usage_obj.get("output_tokens") or 0),
        input_includes_cached=False,
    )
    cost = final.get("total_cost_usd")
    try:
        cost_usd = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_usd = None

    tool_uses = _claude_tool_uses(events)
    tool_names = [str(use["name"]) for use in tool_uses]
    subagent_details: list[dict] = []
    for use in tool_uses:
        if str(use["name"]).lower() not in {"agent", "task"}:
            continue
        payload = use.get("input") if isinstance(use.get("input"), dict) else {}
        subagent_details.append({
            "tool_name": use["name"],
            "tool_use_id": use.get("id"),
            "parent_tool_use_id": use.get("parent_tool_use_id"),
            "observed_depth": use.get("observed_depth"),
            "subagent_type": payload.get("subagent_type") or payload.get("agent_type") or payload.get("type"),
            "description": payload.get("description"),
            "prompt": payload.get("prompt"),
            "model": payload.get("model"),
            "isolation": payload.get("isolation"),
            "run_in_background": payload.get("run_in_background"),
        })
    subagent_calls = len(subagent_details)
    direct_metrics = _claude_direct_tool_metrics(tool_uses)
    is_error = bool(final.get("is_error")) if final else False
    success = returncode == 0 and not is_error and bool(response_text)
    return ClientRun(
        success=success,
        response_text=response_text,
        usage=usage,
        elapsed_seconds=elapsed_seconds,
        cost_usd=cost_usd,
        raw_stdout=stdout,
        raw_stderr="",
        metadata={
            "result_subtype": final.get("subtype") if final else None,
            "num_turns": final.get("num_turns") if final else None,
            "session_id": final.get("session_id") if final else None,
            "tool_names": tool_names,
            "tool_calls": len(tool_names),
            "tool_uses": tool_uses,
            "subagent_calls": subagent_calls,
            "subagent_details": subagent_details,
            "permission_denials": final.get("permission_denials") if final else None,
            **direct_metrics,
        },
    )


def _parse_codex_stream(stdout: str, returncode: int, elapsed_seconds: float) -> ClientRun:
    events: list[dict] = []
    response_parts: list[str] = []
    fatal = False
    usage_obj: dict = {}
    direct_tool_calls = 0
    direct_evidence_calls = 0
    direct_tool_names: list[str] = []
    gremlins_tool_names: list[str] = []

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        event_type = event.get("type")
        if event_type in {"error", "turn.failed"}:
            fatal = True
        if event_type == "turn.completed" and isinstance(event.get("usage"), dict):
            usage_obj = event["usage"]
        if event_type != "item.completed":
            continue

        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")

        if item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                response_parts.append(text.strip())
            continue

        if item_type == "mcp_tool_call":
            server = str(item.get("server") or item.get("server_name") or "")
            tool = str(item.get("tool") or item.get("name") or "")
            rendered = f"{server}.{tool}".strip(".")
            if "gremlins" in server.lower() or "gremlins" in rendered.lower():
                gremlins_tool_names.append(rendered or tool or "mcp_tool_call")
            else:
                direct_tool_calls += 1
                direct_tool_names.append(rendered or tool or "mcp_tool_call")
            continue

        if item_type == "command_execution":
            command = str(item.get("command") or "")
            direct_tool_calls += 1
            direct_tool_names.append("command_execution")
            if _FRONTIER_READ_ONLY_BASH_RE.search(command):
                direct_evidence_calls += 1
            continue

        if item_type in {"file_read", "web_search", "web_fetch"}:
            direct_tool_calls += 1
            direct_evidence_calls += 1
            direct_tool_names.append(item_type)

    usage = normalize_usage(
        input_tokens=int(usage_obj.get("input_tokens") or 0),
        cached_input_tokens=int(usage_obj.get("cached_input_tokens") or 0),
        cache_write_tokens=int(usage_obj.get("cache_write_input_tokens") or 0),
        output_tokens=int(usage_obj.get("output_tokens") or 0),
        input_includes_cached=True,
    )
    response_text = "\n".join(response_parts).strip()
    return ClientRun(
        success=returncode == 0 and not fatal and bool(response_text),
        response_text=response_text,
        usage=usage,
        elapsed_seconds=elapsed_seconds,
        cost_usd=None,
        raw_stdout=stdout,
        raw_stderr="",
        metadata={
            "events": len(events),
            "frontier_direct_tool_calls": direct_tool_calls,
            "frontier_direct_evidence_calls": direct_evidence_calls,
            "frontier_gremlins_tool_calls": len(gremlins_tool_names),
            "frontier_direct_tool_names": direct_tool_names,
            "frontier_gremlins_tool_names": gremlins_tool_names,
        },
    )


def _claude_command(
    prompt: str,
    model: str | None,
    allow_agents: bool = False,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    permission_mode: str = "plan",
    setting_sources: str | None = None,
) -> list[str]:
    args = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        permission_mode,
        "--max-turns",
        "12",
        "--no-session-persistence",
    ]
    denied = list(disallowed_tools or [])
    if not allow_agents:
        denied.insert(0, "Agent")
    if denied:
        args.extend(["--disallowedTools", *dict.fromkeys(denied)])
    if allowed_tools:
        args.extend(["--allowedTools", *allowed_tools])
    if setting_sources:
        args.extend(["--setting-sources", setting_sources])
    if model:
        args.extend(["--model", model])
    return args


def _codex_command(
    prompt: str,
    workspace: Path,
    model: str | None,
    allow_agents: bool = False,
    gremlins_mode: str | None = None,
) -> list[str]:
    args = ["codex"]
    if not allow_agents:
        args.extend(["-c", "agents.enabled=false"])

    if gremlins_mode == "disabled":
        args.extend(["-c", "mcp_servers.gremlins.enabled=false"])
    elif gremlins_mode == "evidence-pack-only":
        args.extend([
            "-c",
            "mcp_servers.gremlins.enabled=true",
            "-c",
            'mcp_servers.gremlins.enabled_tools=["evidence_pack"]',
        ])
    elif gremlins_mode is not None:
        raise ValueError("gremlins_mode must be disabled, evidence-pack-only, or None")

    args.extend([
        "exec",
        "--json",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "-C",
        str(workspace),
    ])
    if model:
        args.extend(["--model", model])
    args.append(prompt)
    return args


def _structural_acceptance(
    response_text: str,
    expected_paths: list[str],
    expected_claims: list[list[str]] | None = None,
) -> tuple[bool, list[str], list[list[str]]]:
    text = response_text.lower()
    missing_paths = [path for path in expected_paths if path.lower() not in text]
    missing_claims: list[list[str]] = []
    for group in expected_claims or []:
        alternatives = [str(value) for value in group if str(value).strip()]
        if alternatives and not any(value.lower() in text for value in alternatives):
            missing_claims.append(alternatives)
    accepted = bool(response_text.strip()) and not missing_paths and not missing_claims
    return accepted, missing_paths, missing_claims


def _redo_marker(response_text: str) -> bool | None:
    match = re.search(r"FRONTIER_REDO_SEARCH\s*=\s*(true|false)", response_text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower() == "true"


def _require_valid_frontier_run(
    client: str,
    parsed: ClientRun,
    returncode: int,
    stderr: str,
) -> None:
    detail_source = stderr.strip() or parsed.raw_stdout.strip()
    detail = detail_source[-2000:] if detail_source else "no client output"
    if not parsed.success:
        raise RuntimeError(
            f"{client} benchmark invocation failed (returncode={returncode}): {detail}"
        )
    if parsed.usage.total_processed_tokens <= 0:
        raise RuntimeError(
            f"{client} benchmark invocation returned zero frontier usage tokens; "
            f"the client output/usage parser may be incompatible: {detail}"
        )


def _save_raw_run(study: str, case_id: str, arm: str, client: str, stdout: str, stderr: str) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = benchmark_root() / "raw" / study
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / f"{stamp}-{case_id}-{arm}-{client}"
    stdout_path = base.with_suffix(".stdout.jsonl")
    stderr_path = base.with_suffix(".stderr.txt")
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return {"stdout": str(stdout_path), "stderr": str(stderr_path)}


def _ensure_study_provenance(
    study: str,
    source: Path,
    client: str,
    model: str | None,
    include_a: bool,
) -> dict:
    existing_records = load_records(study)
    existing_metadata = load_study_metadata(study)
    if existing_records and not existing_metadata:
        raise RuntimeError(
            f"benchmark study {study!r} has existing records without immutable provenance; "
            "use a fresh --study name"
        )

    source_head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    ).stdout.strip()
    client_version = subprocess.run(
        [client, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    ).stdout.strip()
    project_root = Path(__file__).resolve().parents[2]
    gremlins_head = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    ).stdout.strip()
    payload = {
        "study": study,
        "client": client,
        "client_version": client_version,
        "requested_model": model,
        "source_repository": str(source),
        "source_head": source_head,
        "gremlins_head": gremlins_head,
        "include_a": include_a,
        "claude_tool_search": "disabled-preload" if client == "claude" else None,
        "claude_ai_mcp_servers": False if client == "claude" else None,
    }
    assert_study_compatible(study, payload)
    write_study_metadata(study, payload)
    return payload


def run_frontier_case(
    *,
    study: str,
    repository: str,
    client: str,
    case_id: str | None = None,
    arm: str | None = None,
    model: str | None = None,
    timeout_seconds: int = 900,
    include_a: bool = False,
    save_raw: bool = False,
    iteration: int = 1,
) -> dict:
    if client not in {"claude", "codex"}:
        raise ValueError("client must be claude or codex")
    _ensure_frontier_client_ready(client)

    if case_id is None or arm is None:
        next_run = next_missing_run(study, include_a=include_a)
        if next_run is None:
            raise RuntimeError("no missing benchmark runs remain")
        case_id = case_id or str(next_run["case"]["id"])
        arm = arm or str(next_run["arm"])
        iteration = int(next_run.get("iteration") or iteration)

    case = get_pilot_case(case_id)
    source = _ensure_clean_git_repository(repository)
    provenance = _ensure_study_provenance(study, source, client, model, include_a)
    iteration = max(1, int(iteration))
    workspace = _prepare_workspace(source, study, case_id, arm, iteration)
    unique_tag = None
    if arm == "C":
        unique_tag = f"benchmark:{study}:{case_id}:{arm}:r{iteration}:{uuid.uuid4().hex[:12]}"
    prompt_info = build_arm_prompt(
        case_id,
        arm,
        repository=str(workspace),
        iteration=iteration,
        measurement_tag=unique_tag,
    )
    prompt = str(prompt_info["prompt"])

    if client == "claude":
        gremlins_tools = [
            "mcp__gremlins__gremlins_status",
            "mcp__gremlins__repo_search",
            "mcp__gremlins__code_read",
            "mcp__gremlins__git_history",
            "mcp__gremlins__evidence_pack",
            "mcp__gremlins__repo_explorer",
            "mcp__gremlins__failure_triage",
        ]
        if arm == "B":
            disallowed_gremlins = gremlins_tools
        elif arm == "C":
            disallowed_gremlins = [
                name for name in gremlins_tools
                if name != "mcp__gremlins__evidence_pack"
            ]
        else:
            disallowed_gremlins = None

        command = _claude_command(
            prompt,
            model,
            allow_agents=(arm == "A"),
            allowed_tools=(["mcp__gremlins__evidence_pack"] if arm == "C" else None),
            disallowed_tools=disallowed_gremlins,
            permission_mode=("dontAsk" if arm in {"B", "C"} else "plan"),
        )
    else:
        codex_gremlins_mode = (
            "disabled" if arm == "B"
            else "evidence-pack-only" if arm == "C"
            else None
        )
        command = _codex_command(
            prompt,
            workspace,
            model,
            allow_agents=(arm == "A"),
            gremlins_mode=codex_gremlins_mode,
        )

    run_env = None
    if client == "claude" and arm in {"B", "C"}:
        run_env = _claude_benchmark_env()

    started = time.monotonic()
    proc = _run_command(command, workspace, timeout_seconds, env=run_env)
    elapsed = time.monotonic() - started

    if client == "claude":
        parsed = _parse_claude_stream(proc.stdout, proc.returncode, elapsed)
    else:
        parsed = _parse_codex_stream(proc.stdout, proc.returncode, elapsed)

    raw = _save_raw_run(study, case_id, arm, client, proc.stdout, proc.stderr) if save_raw else None
    try:
        _require_valid_frontier_run(client, parsed, proc.returncode, proc.stderr)
    except RuntimeError as exc:
        if raw:
            raise RuntimeError(
                f"{exc}; raw_stdout={raw['stdout']}; raw_stderr={raw['stderr']}"
            ) from exc
        raise

    accepted, missing_paths, missing_claims = _structural_acceptance(
        parsed.response_text,
        [str(path) for path in case.get("expected_paths", [])],
        [
            [str(value) for value in group]
            for group in case.get("expected_claims", [])
            if isinstance(group, list)
        ],
    )
    accepted = bool(parsed.success and accepted)
    reported_redo = _redo_marker(parsed.response_text) if arm == "C" else None
    direct_evidence_calls = parsed.metadata.get("frontier_direct_evidence_calls")
    observed_redo = (
        bool(int(direct_evidence_calls))
        if arm == "C" and direct_evidence_calls is not None
        else None
    )
    redo = observed_redo if observed_redo is not None else reported_redo
    redo_marker_matches_observed = (
        reported_redo == observed_redo
        if arm == "C" and reported_redo is not None and observed_redo is not None
        else None
    )
    tag = prompt_info.get("measurement_tag")
    local = gremlins_stats_for_tag(str(tag)) if tag else None
    if arm == "C":
        calls = int((local or {}).get("calls") or 0)
        workers = {
            str(name): int(count)
            for name, count in ((local or {}).get("workers") or {}).items()
        }
        local_model_calls = int((local or {}).get("local_model_calls") or 0)
        diagnostic = (
            f"tool_names={parsed.metadata.get('tool_names')}; "
            f"permission_denials={parsed.metadata.get('permission_denials')}; "
            f"tagged_gremlins_calls={calls}; workers={workers}; "
            f"local_model_calls={local_model_calls}"
        )
        if raw:
            diagnostic += f"; raw_stdout={raw['stdout']}; raw_stderr={raw['stderr']}"
        if calls <= 0:
            raise RuntimeError(
                "Gremlins-assisted benchmark arm C completed without any tagged evidence_pack calls; "
                "treating the run as invalid rather than recording frontier-only fallback data; "
                + diagnostic
            )
        if calls > 4:
            raise RuntimeError(
                "Gremlins-assisted benchmark arm C exceeded the four-call evidence-loop treatment bound; "
                + diagnostic
            )
        if workers and set(workers) != {"evidence-pack"}:
            raise RuntimeError(
                "Gremlins-assisted benchmark arm C used a Gremlins capability other than evidence_pack; "
                + diagnostic
            )
        if local_model_calls:
            raise RuntimeError(
                "Gremlins-assisted benchmark arm C evidence loop must remain model-free; "
                + diagnostic
            )

    record = BenchmarkRecord(
        case_id=case_id,
        arm=arm,
        client=client,
        model=model,
        accepted=accepted,
        elapsed_seconds=round(parsed.elapsed_seconds, 3),
        frontier_usage=parsed.usage,
        frontier_subagents=(
            int(parsed.metadata.get("subagent_calls"))
            if client == "claude" and parsed.metadata.get("subagent_calls") is not None
            else (0 if arm in {"B", "C"} else None)
        ),
        frontier_redid_search=redo,
        frontier_direct_tool_calls=(
            int(parsed.metadata["frontier_direct_tool_calls"])
            if parsed.metadata.get("frontier_direct_tool_calls") is not None
            else None
        ),
        frontier_direct_evidence_calls=(
            int(parsed.metadata["frontier_direct_evidence_calls"])
            if parsed.metadata.get("frontier_direct_evidence_calls") is not None
            else None
        ),
        gremlins_calls=int(local["calls"]) if local else 0,
        gremlins_local_model_calls=int(local["local_model_calls"]) if local else 0,
        gremlins_result_chars=int(local["result_chars"]) if local else 0,
        notes=(
            f"automated hidden acceptance; missing_expected_paths={missing_paths}; "
            f"missing_expected_claims={missing_claims}; client_success={parsed.success}; "
            f"reported_redo={reported_redo}; observed_redo={observed_redo}; "
            f"redo_marker_matches_observed={redo_marker_matches_observed}"
        ),
        cost_usd=parsed.cost_usd,
        iteration=iteration,
    )
    data_path = append_record(study, record)

    return {
        "study": study,
        "case_id": case_id,
        "arm": arm,
        "iteration": iteration,
        "client": client,
        "requested_model": model,
        "accepted": accepted,
        "missing_expected_paths": missing_paths,
        "missing_expected_claims": missing_claims,
        "frontier_redid_search": redo,
        "frontier_reported_redo_marker": reported_redo,
        "frontier_redo_marker_matches_observed": redo_marker_matches_observed,
        "frontier_direct_tool_calls": parsed.metadata.get("frontier_direct_tool_calls"),
        "frontier_direct_evidence_calls": parsed.metadata.get("frontier_direct_evidence_calls"),
        "frontier_gremlins_tool_calls": parsed.metadata.get("frontier_gremlins_tool_calls"),
        "usage": parsed.usage.__dict__,
        "cost_usd": parsed.cost_usd,
        "elapsed_seconds": round(parsed.elapsed_seconds, 3),
        "gremlins": local,
        "measurement_tag": tag,
        "workspace": str(workspace),
        "benchmark_file": str(data_path),
        "metadata_file": str(benchmark_root() / f"{study}.meta.json"),
        "provenance": provenance,
        "raw": raw,
        "response_excerpt": parsed.response_text[:3000],
        "metadata": parsed.metadata,
        "stderr_excerpt": proc.stderr[-2000:],
        "returncode": proc.returncode,
    }



def run_frontier_suite(
    *,
    study: str,
    repository: str,
    client: str,
    model: str | None = None,
    repeats: int = 1,
    include_a: bool = False,
    timeout_seconds: int = 900,
    save_raw: bool = False,
    force: bool = False,
) -> dict:
    """Run the controlled pilot as paired arms with counterbalanced B/C order.

    Each arm receives a fresh clone of the exact same clean source snapshot.
    B/C ordering alternates by case and repetition to reduce simple warm-cache/order bias.
    Individual task failures are recorded by run_frontier_case and do not stop the suite.
    Execution/setup exceptions are surfaced in the returned errors list.
    """
    from .benchmark import build_benchmark_report, load_pilot_cases

    source = _ensure_clean_git_repository(repository)
    repeats = max(1, int(repeats))
    cases = load_pilot_cases()

    source_head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    ).stdout.strip()
    client_version = subprocess.run(
        [client, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    ).stdout.strip()
    project_root = Path(__file__).resolve().parents[2]
    gremlins_head = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    ).stdout.strip()
    study_metadata = {
        "study": study,
        "client": client,
        "client_version": client_version,
        "requested_model": model,
        "source_repository": str(source),
        "source_head": source_head,
        "gremlins_head": gremlins_head,
        "repeats": repeats,
        "include_a": include_a,
        "ordering": "counterbalanced B/C by case index + iteration parity",
        "claude_tool_search": "disabled-preload" if client == "claude" else None,
        "claude_ai_mcp_servers": False if client == "claude" else None,
    }
    assert_study_compatible(study, study_metadata)
    metadata_path = write_study_metadata(study, study_metadata)
    arms_base = ["A", "B", "C"] if include_a else ["B", "C"]
    runs: list[dict] = []
    errors: list[dict] = []
    skipped: list[dict] = []
    existing = {
        (str(row.get("case_id")), str(row.get("arm")), int(row.get("iteration") or 1))
        for row in load_records(study)
    }

    for iteration in range(1, repeats + 1):
        for index, case in enumerate(cases):
            case_id = str(case["id"])
            if include_a:
                order = list(arms_base)
                if (index + iteration) % 2:
                    order = ["A", "C", "B"]
            else:
                order = ["B", "C"] if (index + iteration) % 2 == 0 else ["C", "B"]

            for arm in order:
                key = (case_id, arm, iteration)
                if not force and key in existing:
                    skipped.append({"case_id": case_id, "arm": arm, "iteration": iteration})
                    continue
                try:
                    result = run_frontier_case(
                        study=study,
                        repository=str(source),
                        client=client,
                        case_id=case_id,
                        arm=arm,
                        model=model,
                        timeout_seconds=timeout_seconds,
                        include_a=include_a,
                        save_raw=save_raw,
                        iteration=iteration,
                    )
                    runs.append({
                        "case_id": case_id,
                        "arm": arm,
                        "iteration": iteration,
                        "accepted": result["accepted"],
                        "usage": result["usage"],
                        "elapsed_seconds": result["elapsed_seconds"],
                        "gremlins": result.get("gremlins"),
                    })
                except Exception as exc:
                    errors.append({
                        "case_id": case_id,
                        "arm": arm,
                        "iteration": iteration,
                        "error": f"{type(exc).__name__}: {exc}",
                    })

    report = build_benchmark_report(study)
    return {
        "study": study,
        "client": client,
        "requested_model": model,
        "repeats": repeats,
        "include_a": include_a,
        "source_repository": str(source),
        "source_head": source_head,
        "client_version": client_version,
        "gremlins_head": gremlins_head,
        "metadata_file": str(metadata_path),
        "runs_attempted": len(cases) * len(arms_base) * repeats,
        "runs_completed": len(runs),
        "runs_skipped_existing": len(skipped),
        "skipped": skipped,
        "execution_errors": errors,
        "report": report,
        "runs": runs,
    }
