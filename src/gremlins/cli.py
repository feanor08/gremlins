from __future__ import annotations

import argparse
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
from .mcpcheck import check_python_module, check_wrapper
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


def _install_client_skill() -> dict:
    source = project_root() / "skills" / "gremlins-delegation" / "SKILL.md"
    destinations = [
        Path("~/.claude/skills/gremlins-delegation/SKILL.md").expanduser(),
        Path("~/.codex/skills/gremlins-delegation/SKILL.md").expanduser(),
    ]
    written = []
    for destination in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(str(destination))
    return {"installed": written}


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


def _ensure_model(pull: bool, profile: str = "mac-local") -> dict:
    config = load_config(profile=profile)
    if not shutil.which("ollama"):
        return {"ok": False, "reason": "ollama not found"}
    try:
        state = health(config)
    except ProviderError:
        state = {"ok": False, "model_present": False}
    if state.get("model_present"):
        return {"ok": True, "model": config.provider.model, "pulled": False}
    if not pull:
        return {"ok": False, "model": config.provider.model, "reason": "model missing"}
    proc = subprocess.run(["ollama", "pull", config.provider.model])
    return {"ok": proc.returncode == 0, "model": config.provider.model, "pulled": proc.returncode == 0}


def deploy(args: argparse.Namespace) -> int:
    if args.profile != "mac-local":
        print(json.dumps({"error": f"profile not implemented yet: {args.profile}"}, indent=2), file=sys.stderr)
        return 4
    wrapper = _write_wrapper(args.profile)
    model = _ensure_model(args.pull_model, args.profile)
    client_skill = _install_client_skill()
    claude = _configure_claude(wrapper) if not args.no_clients else {"skipped": True}
    codex = _configure_codex(wrapper) if not args.no_clients else {"skipped": True}
    result = {
        "wrapper": str(wrapper),
        "platform": f"{platform.system().lower()}-{platform.machine().lower()}",
        "model": model,
        "client_skill": client_skill,
        "claude": claude,
        "codex": codex,
    }
    if model.get("ok"):
        result["stack_lock"] = str(write_stack_lock(load_config(profile=args.profile)))
    print(json.dumps(result, indent=2))
    if not model.get("ok"):
        print("\nGremlins installed, but Ollama/model is not ready. Run: ollama serve   then: gremlins deploy --pull-model", file=sys.stderr)
        return 2
    if not args.no_clients and not any(x.get("configured") for x in (claude, codex)):
        print("\nGremlins is healthy but no coding client was configured automatically.", file=sys.stderr)
        return 3
    return 0


def doctor(_: argparse.Namespace) -> int:
    config = load_config()
    checks: dict[str, object] = {
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.machine()}",
        "git": shutil.which("git"),
        "rg": shutil.which("rg"),
        "ollama": shutil.which("ollama"),
        "claude": shutil.which("claude"),
        "codex": shutil.which("codex"),
        "config": str(project_root() / "gremlins.toml"),
        "wrapper": str(_wrapper_path()) if _wrapper_path().exists() else None,
        "stack_lock": str(stack_lock_path()) if stack_lock_path().exists() else None,
    }
    try:
        checks["provider"] = health(config)
    except ProviderError as exc:
        checks["provider"] = {"ok": False, "error": str(exc)}

    wrapper = _wrapper_path()
    checks["mcp"] = check_wrapper(wrapper) if wrapper.exists() else check_python_module(profile=config.profile)

    provider_ok = isinstance(checks["provider"], dict) and bool(checks["provider"].get("model_present"))
    mcp_ok = isinstance(checks["mcp"], dict) and bool(checks["mcp"].get("ok"))
    print(json.dumps(checks, indent=2))
    return 0 if provider_ok and mcp_ok else 2


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

    p = sub.add_parser("deploy", help="Configure the current local runtime and optional client adapters")
    p.add_argument("--profile", default="mac-local")
    p.add_argument("--pull-model", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--no-clients", action="store_true")
    p.set_defaults(func=deploy)

    p = sub.add_parser("doctor", help="Check local runtime, optional model provider, and adapters")
    p.set_defaults(func=doctor)

    p = sub.add_parser("serve", help="Run the Gremlins MCP server over stdio")
    p.set_defaults(func=serve)

    p = sub.add_parser("mcp-smoke", help="Initialize Gremlins as a real MCP subprocess and verify its tool surface")
    p.set_defaults(func=mcp_smoke_cmd)

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

    p = sub.add_parser("benchmark", help="Record and compare A/B/C frontier-savings measurements")
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
