from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from .config import load_config, project_root
from .provider import ProviderError, health
from .deployment import stack_lock_path, write_stack_lock
from .workers import repo_explore, triage
from .evidence_service import evidence_pack
from .retrieval import git_history as _git_history, literal_search, read_excerpt, snapshot
from .security import resolve_repository
from .mcpcheck import check_python_module, check_wrapper
from .capability_benchmark import run_capability_benchmark
from .model_value_benchmark import run_model_value_benchmark
from .triage_stability_benchmark import run_triage_stability_benchmark
from .evidence_service_benchmark import run_evidence_service_benchmark
from .subagent_observation import (
    analyze_claude_observation_raw,
    run_claude_subagent_observation,
)
from .benchmark import (
    BenchmarkRecord,
    Prices,
    append_record,
    build_benchmark_report,
    normalize_usage,
    parse_usage_payload,
    evaluate_gate,
    gremlins_stats_for_tag,
    build_arm_prompt,
    next_missing_run,
    run_local_pilot,
    study_path,
)


def _run(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def _wrapper_path() -> Path:
    return Path("~/.local/bin/gremlins-mcp").expanduser()


def _write_wrapper(profile: str = "mac-local") -> Path:
    wrapper = _wrapper_path()
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    # Preserve the virtualenv interpreter path. Resolving this symlink can collapse
    # .venv/bin/python to the Homebrew/base interpreter and lose the venv site-packages.
    python = Path(sys.executable)
    root = project_root()
    wrapper.write_text(
        f"#!/bin/sh\nexport GREMLINS_ROOT='{root}'\nexport GREMLINS_PROFILE='{profile}'\nexec '{python}' -m gremlins.server\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _install_client_skill(client: str) -> dict:
    source = project_root() / "skills" / "gremlins-delegation" / "SKILL.md"
    destinations = {
        "claude": Path("~/.claude/skills/gremlins-delegation/SKILL.md").expanduser(),
        "codex": Path("~/.codex/skills/gremlins-delegation/SKILL.md").expanduser(),
    }
    if client not in destinations:
        raise ValueError(f"unsupported client: {client}")
    destination = destinations[client]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return {"client": client, "installed": [str(destination)]}


def _configure_claude(wrapper: Path) -> dict:
    if not shutil.which("claude"):
        return {"available": False, "configured": False, "reason": "claude CLI not found"}
    _run(["claude", "mcp", "remove", "gremlins", "--scope", "user"])
    proc = _run(["claude", "mcp", "add", "--scope", "user", "gremlins", "--", str(wrapper)])
    return {"available": True, "configured": proc.returncode == 0, "stderr": proc.stderr.strip()[-1000:]}


def _configure_codex(wrapper: Path) -> dict:
    if not shutil.which("codex"):
        return {"available": False, "configured": False, "reason": "codex CLI not found"}
    _run(["codex", "mcp", "remove", "gremlins"])
    proc = _run(["codex", "mcp", "add", "gremlins", "--", str(wrapper)])
    return {"available": True, "configured": proc.returncode == 0, "stderr": proc.stderr.strip()[-1000:]}


def _configure_client(client: str, wrapper: Path) -> dict:
    if client == "claude":
        return _configure_claude(wrapper)
    if client == "codex":
        return _configure_codex(wrapper)
    raise ValueError(f"unsupported client: {client}")


def _ensure_model(pull: bool, profile: str = "mac-local") -> dict:
    config = load_config(profile=profile)
    if not shutil.which("ollama"):
        return {"ok": False, "available": False, "reason": "ollama not found"}
    try:
        state = health(config)
    except ProviderError as exc:
        state = {"ok": False, "model_present": False, "error": str(exc)}
    if state.get("model_present"):
        return {"ok": True, "available": True, "model": config.provider.model, "pulled": False}
    if not pull:
        return {
            "ok": False,
            "available": True,
            "model": config.provider.model,
            "reason": "model missing or provider unavailable",
            "provider": state,
        }
    proc = subprocess.run(["ollama", "pull", config.provider.model], check=False)
    return {
        "ok": proc.returncode == 0,
        "available": True,
        "model": config.provider.model,
        "pulled": proc.returncode == 0,
    }


def _core_setup(profile: str) -> dict:
    config = load_config(profile=profile)
    git = shutil.which("git")
    if not git:
        return {
            "ok": False,
            "profile": profile,
            "reason": "git not found",
            "platform": f"{platform.system().lower()}-{platform.machine().lower()}",
        }
    lock = write_stack_lock(config)
    return {
        "ok": True,
        "profile": profile,
        "platform": f"{platform.system().lower()}-{platform.machine().lower()}",
        "python": sys.version.split()[0],
        "git": git,
        "ripgrep": shutil.which("rg"),
        "stack_lock": str(lock),
    }


def setup(args: argparse.Namespace) -> int:
    result = {"core": _core_setup(args.profile)}
    print(json.dumps(result, indent=2))
    return 0 if result["core"].get("ok") else 2


def provider_setup(args: argparse.Namespace) -> int:
    if args.provider != "ollama":
        print(json.dumps({"error": f"unsupported provider: {args.provider}"}, indent=2), file=sys.stderr)
        return 4
    result = {"provider": args.provider, **_ensure_model(args.pull, args.profile)}
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 2


def adapter_install(args: argparse.Namespace) -> int:
    if args.adapter != "mcp":
        print(json.dumps({"error": f"unsupported adapter: {args.adapter}"}, indent=2), file=sys.stderr)
        return 4
    wrapper = _write_wrapper(args.profile)
    print(json.dumps({
        "adapter": "mcp",
        "installed": True,
        "wrapper": str(wrapper),
        "profile": args.profile,
    }, indent=2))
    return 0


def adapter_configure(args: argparse.Namespace) -> int:
    wrapper = _wrapper_path()
    if not wrapper.exists():
        wrapper = _write_wrapper(args.profile)
    skill = _install_client_skill(args.client)
    state = _configure_client(args.client, wrapper)
    result = {
        "client": args.client,
        "adapter": "mcp",
        "wrapper": str(wrapper),
        "skill": skill,
        **state,
    }
    print(json.dumps(result, indent=2))
    return 0 if state.get("configured") else 2


def deploy(args: argparse.Namespace) -> int:
    """Compatibility umbrella. Optional integrations run only when explicitly requested."""
    result: dict[str, object] = {
        "deprecated": True,
        "message": "Use 'gremlins setup', 'gremlins provider setup ...', and 'gremlins adapter ...' for explicit setup.",
        "core": _core_setup(args.profile),
    }
    if not result["core"].get("ok"):
        print(json.dumps(result, indent=2))
        return 2

    failed = False
    if args.pull_model:
        model = _ensure_model(True, args.profile)
        result["provider"] = model
        failed = failed or not model.get("ok")

    clients: dict[str, object] = {}
    if not args.no_clients:
        for client in args.client:
            wrapper = _wrapper_path()
            if not wrapper.exists():
                wrapper = _write_wrapper(args.profile)
            skill = _install_client_skill(client)
            state = _configure_client(client, wrapper)
            clients[client] = {"skill": skill, **state}
            failed = failed or not state.get("configured")
    if clients:
        result["clients"] = clients

    print(json.dumps(result, indent=2))
    return 2 if failed else 0


def doctor(_: argparse.Namespace) -> int:
    try:
        config = load_config()
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "core": {"ok": False, "error": str(exc)},
            "providers": {},
            "interfaces": {},
            "clients": {},
        }, indent=2))
        return 2

    git = shutil.which("git")
    core_ok = bool(git)
    core = {
        "ok": core_ok,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.machine()}",
        "git": git,
        "ripgrep": {
            "available": bool(shutil.which("rg")),
            "required": False,
            "fallback": "git-backed portable literal search",
        },
        "config": str(project_root() / "gremlins.toml"),
        "profile": config.profile,
        "stack_lock": str(stack_lock_path()) if stack_lock_path().exists() else None,
    }

    provider_binary = shutil.which("ollama") if config.provider.kind == "ollama" else None
    provider: dict[str, object] = {
        "required": False,
        "kind": config.provider.kind,
        "available": bool(provider_binary),
        "model": config.provider.model,
    }
    if provider_binary:
        try:
            provider["health"] = health(config)
        except ProviderError as exc:
            provider["health"] = {"ok": False, "error": str(exc)}
    else:
        provider["health"] = {"ok": False, "status": "unavailable"}

    wrapper = _wrapper_path()
    mcp_available = importlib.util.find_spec("mcp") is not None
    mcp_state: dict[str, object] = {
        "required": False,
        "available": mcp_available,
        "configured": wrapper.exists(),
        "wrapper": str(wrapper) if wrapper.exists() else None,
    }
    if wrapper.exists():
        mcp_state["health"] = check_wrapper(wrapper)

    clients = {
        "claude": {"required": False, "available": bool(shutil.which("claude"))},
        "codex": {"required": False, "available": bool(shutil.which("codex"))},
    }
    capabilities = {
        "repo-explore-deterministic": {"available": core_ok, "requires_model": False},
        "repo-search-literal": {"available": core_ok, "requires_model": False},
        "code-read": {"available": core_ok, "requires_model": False},
        "git-history": {"available": core_ok, "requires_model": False},
        "evidence-pack": {"available": core_ok, "requires_model": False},
    }

    checks = {
        "ok": core_ok,
        "core": core,
        "capabilities": capabilities,
        "providers": {config.provider.kind: provider},
        "interfaces": {
            "cli": {"required": True, "available": True, "ok": True},
            "mcp": mcp_state,
        },
        "clients": clients,
    }
    print(json.dumps(checks, indent=2))
    return 0 if core_ok else 2


def run_repo_search(args: argparse.Namespace) -> int:
    config = load_config()
    repo = resolve_repository(args.repository, config)
    items = literal_search(repo, args.query, config, scope=args.scope)
    print(json.dumps({
        "snapshot": snapshot(repo, config),
        "matches": [item.as_dict() for item in items],
    }, indent=2))
    return 0


def run_code_read(args: argparse.Namespace) -> int:
    config = load_config()
    repo = resolve_repository(args.repository, config)
    evidence = read_excerpt(repo, args.path, config, args.start_line, args.line_count)
    print(json.dumps({
        "snapshot": snapshot(repo, config),
        "evidence": evidence.as_dict(),
    }, indent=2))
    return 0


def run_git_history(args: argparse.Namespace) -> int:
    config = load_config()
    repo = resolve_repository(args.repository, config)
    items = _git_history(repo, config, path=args.path, query=args.query)
    print(json.dumps({
        "snapshot": snapshot(repo, config),
        "history": [item.as_dict() for item in items],
    }, indent=2))
    return 0


def run_evidence_pack(args: argparse.Namespace) -> int:
    print(json.dumps(
        evidence_pack(
            args.repository,
            args.task,
            load_config(),
            scope=args.scope,
            terms=args.term,
            symbols=args.symbol,
            paths=args.path,
            include_tests=args.include_tests,
            include_history=args.include_history,
            max_files=args.max_files,
            measurement_tag=args.measurement_tag,
        ),
        indent=2,
    ))
    return 0


def run_repo(args: argparse.Namespace) -> int:
    print(json.dumps(
        repo_explore(
            args.repository,
            args.task,
            load_config(),
            scope=args.scope,
            terms=args.term,
            symbols=args.symbol,
            mode=args.mode,
            measurement_tag=args.measurement_tag,
        ),
        indent=2,
    ))
    return 0


def run_triage(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8", errors="replace") if args.file else sys.stdin.read()
    print(json.dumps(triage(text, args.task, load_config(), measurement_tag=args.measurement_tag), indent=2))
    return 0


def eval_cmd(_: argparse.Namespace) -> int:
    from .evals import run_smoke_evals
    report = run_smoke_evals()
    print(json.dumps(report, indent=2))
    return 0 if report["failed"] == 0 else 1



def report_cmd(args: argparse.Namespace) -> int:
    from .report import build_report
    print(json.dumps(build_report(args.limit), indent=2))
    return 0


def mcp_smoke_cmd(_: argparse.Namespace) -> int:
    result = check_python_module(profile=load_config().profile)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def benchmark_run_cmd(args: argparse.Namespace) -> int:
    from .frontier_runner import run_frontier_case

    result = run_frontier_case(
        study=args.study,
        repository=args.repository,
        client=args.client,
        case_id=args.case_id,
        arm=args.arm,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        include_a=args.include_a,
        save_raw=args.save_raw,
        iteration=args.iteration,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["accepted"] else 1


def benchmark_suite_cmd(args: argparse.Namespace) -> int:
    from .frontier_runner import run_frontier_suite

    result = run_frontier_suite(
        study=args.study,
        repository=args.repository,
        client=args.client,
        model=args.model,
        repeats=args.repeats,
        include_a=args.include_a,
        timeout_seconds=args.timeout_seconds,
        save_raw=args.save_raw,
        force=args.force,
    )
    print(json.dumps(result, indent=2))
    return 0 if not result["execution_errors"] else 2


def benchmark_record_cmd(args: argparse.Namespace) -> int:
    if args.usage_json:
        payload = json.loads(Path(args.usage_json).read_text(encoding="utf-8"))
        usage = parse_usage_payload(payload, args.usage_format)
    else:
        usage = normalize_usage(
            input_tokens=args.input_tokens,
            cached_input_tokens=args.cache_read_tokens,
            cache_write_tokens=args.cache_write_tokens,
            output_tokens=args.output_tokens,
            input_includes_cached=args.input_includes_cached,
        )
    prices = Prices(
        uncached_input_per_million=args.price_input,
        cache_read_per_million=args.price_cache_read,
        cache_write_per_million=args.price_cache_write,
        output_per_million=args.price_output,
    )
    tagged = gremlins_stats_for_tag(args.gremlins_tag) if args.gremlins_tag else None
    gremlins_calls = args.gremlins_calls if args.gremlins_calls is not None else (tagged["calls"] if tagged else 0)
    gremlins_local_model_calls = (
        args.gremlins_local_model_calls
        if args.gremlins_local_model_calls is not None
        else (tagged["local_model_calls"] if tagged else 0)
    )
    gremlins_result_chars = (
        args.gremlins_result_chars
        if args.gremlins_result_chars is not None
        else (tagged["result_chars"] if tagged else 0)
    )

    record = BenchmarkRecord(
        case_id=args.case_id,
        arm=args.arm,
        client=args.client,
        model=args.model,
        accepted=args.accepted,
        elapsed_seconds=args.elapsed,
        frontier_usage=usage,
        frontier_subagents=args.frontier_subagents,
        frontier_redid_search=args.frontier_redid_search,
        frontier_direct_tool_calls=args.frontier_direct_tool_calls,
        frontier_direct_evidence_calls=args.frontier_direct_evidence_calls,
        gremlins_calls=gremlins_calls,
        gremlins_local_model_calls=gremlins_local_model_calls,
        gremlins_result_chars=gremlins_result_chars,
        notes=((args.notes or "") + (f" [gremlins_tag={args.gremlins_tag}]" if args.gremlins_tag else "")).strip(),
        cost_usd=prices.cost_usd(usage),
        iteration=args.iteration,
    )
    path = append_record(args.study, record)
    print(json.dumps({"recorded": True, "study": args.study, "path": str(path), "gremlins": tagged, "record": record.as_dict()}, indent=2))
    return 0


def benchmark_report_cmd(args: argparse.Namespace) -> int:
    print(json.dumps(build_benchmark_report(args.study), indent=2))
    return 0

def benchmark_gate_cmd(args: argparse.Namespace) -> int:
    result = evaluate_gate(
        args.study,
        min_pairs=args.min_pairs,
        min_unique_cases=args.min_unique_cases,
        min_frontier_reduction_pct=args.min_frontier_reduction_pct,
        max_acceptance_drop=args.max_acceptance_drop,
        max_redo_rate=args.max_redo_rate,
        min_redo_coverage=args.min_redo_coverage,
        max_elapsed_increase_pct=args.max_elapsed_increase_pct,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["pass"] else 1




def benchmark_prompt_cmd(args: argparse.Namespace) -> int:
    result = build_arm_prompt(args.case_id, args.arm, repository=args.repository, iteration=args.iteration)
    print(json.dumps(result, indent=2))
    return 0


def benchmark_next_cmd(args: argparse.Namespace) -> int:
    result = next_missing_run(args.study, include_a=args.include_a, repeats=args.repeats)
    print(json.dumps({"study": args.study, "next": result}, indent=2))
    return 0 if result is not None else 1


def benchmark_cases_cmd(_: argparse.Namespace) -> int:
    path = project_root() / "evals" / "pilot" / "cases.json"
    print(path.read_text(encoding="utf-8"))
    return 0


def benchmark_pilot_local_cmd(args: argparse.Namespace) -> int:
    report = run_local_pilot(args.repository, case_id=args.case_id)
    print(json.dumps(report, indent=2))
    return 0 if report["failed"] == 0 else 1


def benchmark_capabilities_cmd(args: argparse.Namespace) -> int:
    report = run_capability_benchmark(args.repository)
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


def benchmark_evidence_service_cmd(args: argparse.Namespace) -> int:
    try:
        report = run_evidence_service_benchmark(args.repository)
    except RuntimeError as exc:
        print(json.dumps({"benchmark": "evidence-service-v1", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


def benchmark_model_value_cmd(args: argparse.Namespace) -> int:
    try:
        report = run_model_value_benchmark(
            args.repository,
            worker=args.worker,
            repo_case_ids=args.repo_case,
            triage_case_ids=args.triage_case,
            min_quality_gain=args.min_quality_gain,
        )
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"benchmark": "model-value-v1", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


def benchmark_triage_stability_cmd(args: argparse.Namespace) -> int:
    try:
        report = run_triage_stability_benchmark(
            args.repository,
            repeats=args.repeats,
            case_ids=args.case,
            min_mean_quality_gain=args.min_mean_quality_gain,
            min_success_rate=args.min_success_rate,
            min_case_model_quality=args.min_case_model_quality,
            max_case_quality_span=args.max_case_quality_span,
            min_stable_case_fraction=args.min_stable_case_fraction,
            max_regressed_case_fraction=args.max_regressed_case_fraction,
        )
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"benchmark": "triage-stability-v1", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


def benchmark_claude_subagents_cmd(args: argparse.Namespace) -> int:
    try:
        report = run_claude_subagent_observation(
            args.repository,
            study=args.study,
            repeats=args.repeats,
            case_ids=args.case,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
        )
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"benchmark": "claude-subagent-observation-v1", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


def benchmark_analyze_claude_observation_cmd(args: argparse.Namespace) -> int:
    try:
        report = analyze_claude_observation_raw(
            study=args.study,
            raw_dir=args.raw_dir,
        )
    except RuntimeError as exc:
        print(json.dumps({"analysis": "claude-observation-raw-v1", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


def benchmark_clear_cmd(args: argparse.Namespace) -> int:
    path = study_path(args.study)
    if path.exists():
        path.unlink()
    print(json.dumps({"cleared": True, "study": args.study, "path": str(path)}, indent=2))
    return 0


def serve(_: argparse.Namespace) -> int:
    from .server import main as serve_main
    serve_main()
    return 0


def rollback(_: argparse.Namespace) -> int:
    print("Gremlins does not overwrite source generations. To roll back, git checkout the desired commit and run ./install.sh again.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gremlins", description="Gremlins local capability runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="Bootstrap the deterministic Gremlins core only")
    p.add_argument("--profile", default="mac-local")
    p.set_defaults(func=setup)

    p = sub.add_parser("provider", help="Manage optional local-model providers")
    provider_sub = p.add_subparsers(dest="provider_command", required=True)
    pp = provider_sub.add_parser("setup", help="Validate or prepare an optional model provider")
    pp.add_argument("provider", choices=["ollama"])
    pp.add_argument("--profile", default="mac-local")
    pp.add_argument("--pull", action=argparse.BooleanOptionalAction, default=True)
    pp.set_defaults(func=provider_setup)

    p = sub.add_parser("adapter", help="Manage optional interfaces and client registrations")
    adapter_sub = p.add_subparsers(dest="adapter_command", required=True)
    ap = adapter_sub.add_parser("install", help="Install an interface adapter")
    ap.add_argument("adapter", choices=["mcp"])
    ap.add_argument("--profile", default="mac-local")
    ap.set_defaults(func=adapter_install)
    ap = adapter_sub.add_parser("configure", help="Configure an optional client to use Gremlins")
    ap.add_argument("client", choices=["claude", "codex"])
    ap.add_argument("--profile", default="mac-local")
    ap.set_defaults(func=adapter_configure)

    p = sub.add_parser("deploy", help="Deprecated compatibility umbrella; optional setup is opt-in")
    p.add_argument("--profile", default="mac-local")
    p.add_argument("--pull-model", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--client", action="append", choices=["claude", "codex"], default=[])
    p.add_argument("--no-clients", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=deploy)

    p = sub.add_parser("doctor", help="Report core health separately from optional providers, adapters, and clients")
    p.set_defaults(func=doctor)

    p = sub.add_parser("serve", help="Run the Gremlins MCP server over stdio")
    p.set_defaults(func=serve)

    p = sub.add_parser("mcp-smoke", help="Initialize Gremlins as a real MCP subprocess and verify its tool surface")
    p.set_defaults(func=mcp_smoke_cmd)

    p = sub.add_parser("repo-search", help="Run exact read-only repository search directly")
    p.add_argument("query")
    p.add_argument("--repository", default=".")
    p.add_argument("--scope", default=".")
    p.set_defaults(func=run_repo_search)

    p = sub.add_parser("code-read", help="Read a bounded source excerpt directly")
    p.add_argument("path")
    p.add_argument("--repository", default=".")
    p.add_argument("--start-line", type=int, default=1)
    p.add_argument("--line-count", type=int, default=120)
    p.set_defaults(func=run_code_read)

    p = sub.add_parser("git-history", help="Inspect bounded Git history directly")
    p.add_argument("--repository", default=".")
    p.add_argument("--path")
    p.add_argument("--query")
    p.set_defaults(func=run_git_history)

    p = sub.add_parser("evidence-pack", help="Build a deterministic cross-source evidence bundle")
    p.add_argument("task")
    p.add_argument("--repository", default=".")
    p.add_argument("--scope", default=".")
    p.add_argument("--term", action="append", default=None)
    p.add_argument("--symbol", action="append", default=None)
    p.add_argument("--path", action="append", default=None, help="Focus a known repository path; repeatable")
    p.add_argument("--include-tests", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--include-history", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--max-files", type=int, default=6)
    p.add_argument("--measurement-tag")
    p.set_defaults(func=run_evidence_pack)

    p = sub.add_parser("repo-explore", help="Run repo explorer directly")
    p.add_argument("task")
    p.add_argument("--repository", default=".")
    p.add_argument("--scope", default=".")
    p.add_argument("--term", action="append", default=None, help="Exact search term supplied by the caller; repeatable")
    p.add_argument("--symbol", action="append", default=None, help="Exact symbol supplied by the caller; repeatable")
    p.add_argument("--mode", choices=["auto", "deterministic", "model"], default="auto")
    p.add_argument("--measurement-tag")
    p.set_defaults(func=run_repo)

    p = sub.add_parser("triage", help="Run log/test triage directly")
    p.add_argument("--file")
    p.add_argument("--task", default="Group the failures, identify likely causes, and give the next checks.")
    p.add_argument("--measurement-tag")
    p.set_defaults(func=run_triage)

    p = sub.add_parser("eval", help="Run deterministic smoke evaluations")
    p.set_defaults(func=eval_cmd)

    p = sub.add_parser("report", help="Summarize local Gremlins job metrics")
    p.add_argument("--limit", type=int, default=500)
    p.set_defaults(func=report_cmd)

    p = sub.add_parser("benchmark", help="Run capability and optional client-integration measurements")
    bench = p.add_subparsers(dest="benchmark_command", required=True)

    bp = bench.add_parser("run", help="Run one controlled benchmark arm with installed Claude or Codex")
    bp.add_argument("--study", default="pilot")
    bp.add_argument("--repository", required=True)
    bp.add_argument("--client", choices=["claude", "codex"], required=True)
    bp.add_argument("--case", dest="case_id")
    bp.add_argument("--arm", choices=["A", "B", "C"])
    bp.add_argument("--model")
    bp.add_argument("--timeout-seconds", type=int, default=900)
    bp.add_argument("--include-a", action="store_true")
    bp.add_argument("--iteration", type=int, default=1)
    bp.add_argument("--save-raw", action="store_true", help="Opt in to saving raw client stdout/stderr locally")
    bp.set_defaults(func=benchmark_run_cmd)

    bp = bench.add_parser("suite", help="Run the full controlled B/C pilot with fresh workspaces and counterbalanced ordering")
    bp.add_argument("--study", default="pilot")
    bp.add_argument("--repository", required=True)
    bp.add_argument("--client", choices=["claude", "codex"], required=True)
    bp.add_argument("--model")
    bp.add_argument("--repeats", type=int, default=1)
    bp.add_argument("--include-a", action="store_true")
    bp.add_argument("--timeout-seconds", type=int, default=900)
    bp.add_argument("--save-raw", action="store_true", help="Opt in to saving raw client stdout/stderr locally")
    bp.add_argument("--force", action="store_true", help="Rerun arms already recorded for the same case/iteration")
    bp.set_defaults(func=benchmark_suite_cmd)

    bp = bench.add_parser("record", help="Record one completed benchmark arm")
    bp.add_argument("--study", default="pilot")
    bp.add_argument("--case", dest="case_id", required=True)
    bp.add_argument("--arm", choices=["A", "B", "C"], required=True)
    bp.add_argument("--client", required=True)
    bp.add_argument("--model")
    bp.add_argument("--accepted", action=argparse.BooleanOptionalAction, default=True)
    bp.add_argument("--elapsed", type=float, required=True)
    bp.add_argument("--usage-json", help="JSON file containing a provider usage object")
    bp.add_argument("--usage-format", choices=["openai", "anthropic", "normalized"], default="normalized")
    bp.add_argument("--input-tokens", type=int, default=0)
    bp.add_argument("--cache-read-tokens", type=int, default=0)
    bp.add_argument("--cache-write-tokens", type=int, default=0)
    bp.add_argument("--output-tokens", type=int, default=0)
    bp.add_argument("--input-includes-cached", action="store_true")
    bp.add_argument("--frontier-subagents", type=int)
    bp.add_argument("--frontier-redid-search", action=argparse.BooleanOptionalAction, default=None)
    bp.add_argument("--frontier-direct-tool-calls", type=int)
    bp.add_argument("--frontier-direct-evidence-calls", type=int)
    bp.add_argument("--gremlins-tag", help="Pull Gremlins call/model/result-size metrics from jobs.jsonl for this tag")
    bp.add_argument("--gremlins-calls", type=int)
    bp.add_argument("--gremlins-local-model-calls", type=int)
    bp.add_argument("--gremlins-result-chars", type=int)
    bp.add_argument("--price-input", type=float)
    bp.add_argument("--price-cache-read", type=float)
    bp.add_argument("--price-cache-write", type=float)
    bp.add_argument("--price-output", type=float)
    bp.add_argument("--notes")
    bp.add_argument("--iteration", type=int, default=1)
    bp.set_defaults(func=benchmark_record_cmd)

    bp = bench.add_parser("report", help="Compare benchmark arms, prioritizing paired B to C cases")
    bp.add_argument("--study", default="pilot")
    bp.set_defaults(func=benchmark_report_cmd)

    bp = bench.add_parser("gate", help="Evaluate whether paired B to C results justify architecture expansion")
    bp.add_argument("--study", default="pilot")
    bp.add_argument("--min-pairs", type=int, default=10)
    bp.add_argument("--min-unique-cases", type=int, default=10)
    bp.add_argument("--min-frontier-reduction-pct", type=float, default=30.0)
    bp.add_argument("--max-acceptance-drop", type=float, default=0.05)
    bp.add_argument("--max-redo-rate", type=float, default=0.20)
    bp.add_argument("--min-redo-coverage", type=float, default=0.80)
    bp.add_argument("--max-elapsed-increase-pct", type=float, default=20.0)
    bp.set_defaults(func=benchmark_gate_cmd)

    bp = bench.add_parser("prompt", help="Emit the controlled prompt for one A/B/C pilot run")
    bp.add_argument("--case", dest="case_id", required=True)
    bp.add_argument("--arm", choices=["A", "B", "C"], required=True)
    bp.add_argument("--repository", default=".")
    bp.add_argument("--iteration", type=int, default=1)
    bp.set_defaults(func=benchmark_prompt_cmd)

    bp = bench.add_parser("next", help="Show the next missing pilot case/arm")
    bp.add_argument("--study", default="pilot")
    bp.add_argument("--include-a", action="store_true")
    bp.add_argument("--repeats", type=int, default=1)
    bp.set_defaults(func=benchmark_next_cmd)

    bp = bench.add_parser("cases", help="Print the starter pilot case corpus")
    bp.set_defaults(func=benchmark_cases_cmd)

    bp = bench.add_parser("pilot-local", help="Run the deterministic pilot corpus without local inference")
    bp.add_argument("--repository", default=".")
    bp.add_argument("--case", dest="case_id", help="Run only one pilot case")
    bp.set_defaults(func=benchmark_pilot_local_cmd)

    bp = bench.add_parser("capabilities", help="Run the caller-independent capability benchmark")
    bp.add_argument("--repository", default=".")
    bp.set_defaults(func=benchmark_capabilities_cmd)

    bp = bench.add_parser("evidence-service", help="Run the model-free evidence-service benchmark")
    bp.add_argument("--repository", default=".")
    bp.set_defaults(func=benchmark_evidence_service_cmd)

    bp = bench.add_parser("model-value", help="Compare deterministic execution with optional local-model synthesis")
    bp.add_argument("--repository", default=".")
    bp.add_argument("--worker", choices=["all", "repo-explore", "triage"], default="all")
    bp.add_argument("--repo-case", action="append", help="Run only the selected repo case; repeatable")
    bp.add_argument("--triage-case", action="append", help="Run only the selected triage case; repeatable")
    bp.add_argument("--min-quality-gain", type=float, default=0.10)
    bp.set_defaults(func=benchmark_model_value_cmd)

    bp = bench.add_parser("triage-stability", help="Measure repeated local-model triage quality on the broader corpus")
    bp.add_argument("--repository", default=".")
    bp.add_argument("--repeats", type=int, default=3)
    bp.add_argument("--case", action="append", help="Run only selected triage case; repeatable")
    bp.add_argument("--min-mean-quality-gain", type=float, default=0.10)
    bp.add_argument("--min-success-rate", type=float, default=0.95)
    bp.add_argument("--min-case-model-quality", type=float, default=0.75)
    bp.add_argument("--max-case-quality-span", type=float, default=0.25)
    bp.add_argument("--min-stable-case-fraction", type=float, default=0.90)
    bp.add_argument("--max-regressed-case-fraction", type=float, default=0.10)
    bp.set_defaults(func=benchmark_triage_stability_cmd)

    bp = bench.add_parser("observe-claude-subagents", help="Observe normal Claude subagent delegation with Gremlins disabled")
    bp.add_argument("--repository", default=".")
    bp.add_argument("--study", default="mac-claude-subagents-v1")
    bp.add_argument("--repeats", type=int, default=1)
    bp.add_argument("--case", action="append", help="Run only selected observation case; repeatable")
    bp.add_argument("--model")
    bp.add_argument("--timeout-seconds", type=int, default=900)
    bp.set_defaults(func=benchmark_claude_subagents_cmd)

    bp = bench.add_parser("analyze-claude-observation", help="Analyze saved Claude observation streams without making new Claude calls")
    bp.add_argument("--study", default="mac-claude-subagents-v1")
    bp.add_argument("--raw-dir")
    bp.set_defaults(func=benchmark_analyze_claude_observation_cmd)

    bp = bench.add_parser("clear", help="Clear a local benchmark study")
    bp.add_argument("--study", default="pilot")
    bp.set_defaults(func=benchmark_clear_cmd)

    p = sub.add_parser("rollback", help="Show the rollback procedure")
    p.set_defaults(func=rollback)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
