#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
from statistics import median
from typing import Any


def _json_lines(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _content_blocks(event: dict) -> list[dict]:
    blocks: list[dict] = []
    message = event.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        blocks.extend(x for x in message["content"] if isinstance(x, dict))
    if isinstance(event.get("content"), list):
        blocks.extend(x for x in event["content"] if isinstance(x, dict))
    return blocks


def _tool_uses(events: list[dict]) -> list[dict]:
    uses: list[dict] = []
    for event_index, event in enumerate(events):
        for block in _content_blocks(event):
            if block.get("type") not in {"tool_use", "server_tool_use"}:
                continue
            uses.append({
                "event_index": event_index,
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input") if isinstance(block.get("input"), dict) else {},
            })
    return uses


def _tool_results(events: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for event in events:
        for block in _content_blocks(event):
            if block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str):
                continue
            content = block.get("content")
            if isinstance(content, str):
                text = content
            else:
                text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            out[tool_use_id] = text
    return out


def _paths_from_result(text: str) -> set[str]:
    paths = set(re.findall(r'"path"\s*:\s*"([^"]+)"', text))
    paths.update(re.findall(r"'path'\s*:\s*'([^']+)'", text))
    return paths


def _input_text(tool_input: dict) -> str:
    return json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))


def analyze_trace(path: Path) -> dict:
    events = _json_lines(path)
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    uses = _tool_uses(events)
    results = _tool_results(events)

    gremlins_indexes = [
        index for index, use in enumerate(uses)
        if use.get("name") == "mcp__gremlins__repo_explorer"
    ]
    first_gremlins = gremlins_indexes[0] if gremlins_indexes else None
    gremlins_result_chars = 0
    gremlins_input: dict = {}
    gremlins_result_excerpt = ""
    returned_paths: set[str] = set()
    if first_gremlins is not None:
        use = uses[first_gremlins]
        gremlins_input = dict(use.get("input") or {})
        tool_id = use.get("id")
        if isinstance(tool_id, str):
            result_text = results.get(tool_id, "")
            gremlins_result_chars = len(result_text)
            gremlins_result_excerpt = result_text[:8000]
            returned_paths = _paths_from_result(result_text)

    post_gremlins = uses[first_gremlins + 1 :] if first_gremlins is not None else []
    verification_uses: list[dict] = []
    for use in post_gremlins:
        if use.get("name") not in {"Bash", "Read", "Grep"}:
            continue
        payload = _input_text(use.get("input") or {})
        touched = sorted(path for path in returned_paths if path and path in payload)
        if touched:
            verification_uses.append({
                "name": use.get("name"),
                "paths": touched,
                "input": use.get("input"),
            })

    denials = final.get("permission_denials")
    if not isinstance(denials, list):
        denials = []

    usage = final.get("usage") if isinstance(final.get("usage"), dict) else {}
    return {
        "raw_file": str(path),
        "num_turns": final.get("num_turns"),
        "tool_sequence": [use.get("name") for use in uses],
        "tool_calls": len(uses),
        "toolsearch_calls": sum(use.get("name") == "ToolSearch" for use in uses),
        "gremlins_calls": len(gremlins_indexes),
        "gremlins_client_result_chars": gremlins_result_chars,
        "gremlins_input": gremlins_input,
        "gremlins_terms": list(gremlins_input.get("terms") or []),
        "gremlins_symbols": list(gremlins_input.get("symbols") or []),
        "gremlins_result_excerpt": gremlins_result_excerpt,
        "returned_paths": sorted(returned_paths),
        "post_gremlins_tool_calls": len(post_gremlins),
        "post_gremlins_verification_calls": len(verification_uses),
        "verification_uses": verification_uses,
        "permission_denials": denials,
        "usage": usage,
    }


def load_records(path: Path) -> list[dict]:
    return _json_lines(path)


def pair_records_with_traces(records: list[dict], raw_dir: Path) -> list[dict]:
    grouped_records: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        grouped_records[(str(record.get("case_id")), str(record.get("arm")))].append(record)
    for values in grouped_records.values():
        values.sort(key=lambda row: str(row.get("recorded_at") or ""))

    grouped_raw: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for path in sorted(raw_dir.glob("*-claude.stdout.jsonl")):
        match = re.match(r"^\d{8}T\d{6}Z-(.+)-([ABC])-claude\.stdout\.jsonl$", path.name)
        if not match:
            continue
        grouped_raw[(match.group(1), match.group(2))].append(path)

    rows: list[dict] = []
    for key, recs in grouped_records.items():
        traces = sorted(grouped_raw.get(key, []))
        for index, record in enumerate(recs):
            row = {
                "case_id": key[0],
                "arm": key[1],
                "iteration": int(record.get("iteration") or index + 1),
                "accepted": bool(record.get("accepted")),
                "elapsed_seconds": record.get("elapsed_seconds"),
                "frontier_usage": record.get("frontier_usage") or {},
                "cost_usd": record.get("cost_usd"),
            }
            if index < len(traces):
                row.update(analyze_trace(traces[index]))
            else:
                row["trace_missing"] = True
            rows.append(row)
    rows.sort(key=lambda row: (row["iteration"], row["case_id"], row["arm"]))
    return rows


def summarize(rows: list[dict]) -> dict:
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_arm[row["arm"]].append(row)

    summary: dict[str, Any] = {}
    for arm, arm_rows in sorted(by_arm.items()):
        turns = [int(row["num_turns"]) for row in arm_rows if isinstance(row.get("num_turns"), int)]
        summary[arm] = {
            "runs": len(arm_rows),
            "median_turns": median(turns) if turns else None,
            "toolsearch_runs": sum(bool(row.get("toolsearch_calls")) for row in arm_rows),
            "toolsearch_calls": sum(int(row.get("toolsearch_calls") or 0) for row in arm_rows),
            "permission_denials": sum(len(row.get("permission_denials") or []) for row in arm_rows),
            "post_gremlins_verification_calls": sum(
                int(row.get("post_gremlins_verification_calls") or 0) for row in arm_rows
            ),
            "tool_sequence_counts": dict(Counter(
                " -> ".join(str(x) for x in row.get("tool_sequence") or [])
                for row in arm_rows
            )),
        }

    c_rows = by_arm.get("C", [])
    c_summary = summary.setdefault("C", {
        "runs": 0,
        "median_turns": None,
        "toolsearch_runs": 0,
        "toolsearch_calls": 0,
        "permission_denials": 0,
        "post_gremlins_verification_calls": 0,
        "tool_sequence_counts": {},
    })
    c_summary["gremlins_client_result_chars_total"] = sum(
        int(row.get("gremlins_client_result_chars") or 0) for row in c_rows
    ) if c_rows else 0
    c_summary["runs_with_post_gremlins_verification"] = sum(
        bool(row.get("post_gremlins_verification_calls")) for row in c_rows
    ) if c_rows else 0
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze saved Claude benchmark stream-json traces.")
    parser.add_argument("--study", default="pilot-claude-gate-v1")
    parser.add_argument(
        "--state-dir",
        default="~/.local/state/gremlins",
        help="Gremlins state directory (default: ~/.local/state/gremlins)",
    )
    parser.add_argument("--output", help="Optional JSON output file")
    args = parser.parse_args()

    state = Path(args.state_dir).expanduser()
    records_path = state / "benchmarks" / f"{args.study}.jsonl"
    raw_dir = state / "benchmarks" / "raw" / args.study
    if not records_path.is_file():
        raise SystemExit(f"benchmark records not found: {records_path}")
    if not raw_dir.is_dir():
        raise SystemExit(f"raw trace directory not found: {raw_dir}")

    rows = pair_records_with_traces(load_records(records_path), raw_dir)
    payload = {
        "study": args.study,
        "records": len(rows),
        "summary": summarize(rows),
        "runs": rows,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).expanduser().write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
