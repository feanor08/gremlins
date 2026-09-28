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


def _claude_tool_names(events: list[dict]) -> list[str]:
    names: list[str] = []
    for event in events:
        message = event.get("message")
        if isinstance(message, dict):
            names.extend(_tool_names_from_content(message.get("content")))
        names.extend(_tool_names_from_content(event.get("content")))
    return names


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

    tool_names = _claude_tool_names(events)
    subagent_calls = sum(name.lower() in {"agent", "task"} for name in tool_names)
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
            "subagent_calls": subagent_calls,
            "permission_denials": final.get("permission_denials") if final else None,
        },
    )


def _parse_codex_stream(stdout: str, returncode: int, elapsed_seconds: float) -> ClientRun:
    events: list[dict] = []
    response_parts: list[str] = []
    fatal = False
    usage_obj: dict = {}

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
        if event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    response_parts.append(text.strip())

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
        metadata={"events": len(events)},
    )


def _claude_command(
    prompt: str,
    model: str | None,
    allow_agents: bool = False,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    permission_mode: str = "plan",
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
    if model:
        args.extend(["--model", model])
    return args


def _codex_command(prompt: str, workspace: Path, model: str | None, allow_agents: bool = False) -> list[str]:
    args = ["codex"]
    if not allow_agents:
        args.extend(["-c", "agents.enabled=false"])
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
            "mcp__gremlins__repo_explorer",
            "mcp__gremlins__failure_triage",
        ]
        if arm == "B":
            disallowed_gremlins = gremlins_tools
        elif arm == "C":
            disallowed_gremlins = [
                name for name in gremlins_tools
                if name != "mcp__gremlins__repo_explorer"
            ]
        else:
            disallowed_gremlins = None

        command = _claude_command(
            prompt,
            model,
            allow_agents=(arm == "A"),
            allowed_tools=(["mcp__gremlins__repo_explorer"] if arm == "C" else None),
            disallowed_tools=disallowed_gremlins,
            permission_mode=("dontAsk" if arm in {"B", "C"} else "plan"),
        )
    else:
        command = _codex_command(prompt, workspace, model, allow_agents=(arm == "A"))

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
    redo = _redo_marker(parsed.response_text) if arm == "C" else None
    tag = prompt_info.get("measurement_tag")
    local = gremlins_stats_for_tag(str(tag)) if tag else None
    if arm == "C":
        calls = int((local or {}).get("calls") or 0)
        diagnostic = (
            f"tool_names={parsed.metadata.get('tool_names')}; "
            f"permission_denials={parsed.metadata.get('permission_denials')}; "
            f"tagged_gremlins_calls={calls}"
        )
        if raw:
            diagnostic += f"; raw_stdout={raw['stdout']}; raw_stderr={raw['stderr']}"
        if calls <= 0:
            raise RuntimeError(
                "Gremlins-assisted benchmark arm C completed without any tagged Gremlins calls; "
                "treating the run as invalid rather than recording frontier-only fallback data; "
                + diagnostic
            )
        if calls != 1:
            raise RuntimeError(
                "Gremlins-assisted benchmark arm C must contain exactly one tagged repo_explorer call; "
                "treating repeated treatment calls as invalid; "
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
        gremlins_calls=int(local["calls"]) if local else 0,
        gremlins_local_model_calls=int(local["local_model_calls"]) if local else 0,
        gremlins_result_chars=int(local["result_chars"]) if local else 0,
        notes=(
            f"automated hidden acceptance; missing_expected_paths={missing_paths}; "
            f"missing_expected_claims={missing_claims}; client_success={parsed.success}"
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
