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


def _is_gremlins_use(use: dict) -> bool:
    return str(use.get("name") or "").startswith("mcp__gremlins__")


def _is_direct_evidence_use(use: dict) -> bool:
    return str(use.get("name") or "") in {"Read", "Grep", "Glob", "Search", "Find"}


def _safe_json_object(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _result_paths(result_text: str) -> list[str]:
    parsed = _safe_json_object(result_text)
    if parsed is None:
        return sorted(_paths_from_result(result_text))

    paths: list[str] = []
    for key in ("files", "related_paths"):
        values = parsed.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
    for item in parsed.get("relationships") or []:
        if not isinstance(item, dict):
            continue
        for key in ("source", "test", "source_path", "test_path"):
            path = item.get(key)
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
    return paths


def _gremlins_call_detail(use: dict, result_text: str, call_number: int) -> dict:
    tool_input = dict(use.get("input") or {})
    parsed = _safe_json_object(result_text)
    request = parsed.get("request") if isinstance(parsed, dict) and isinstance(parsed.get("request"), dict) else {}
    return {
        "call_number": call_number,
        "name": use.get("name"),
        "event_index": use.get("event_index"),
        "task": tool_input.get("task"),
        "detail": tool_input.get("detail") or request.get("detail"),
        "paths": list(tool_input.get("paths") or []),
        "terms": list(tool_input.get("terms") or []),
        "symbols": list(tool_input.get("symbols") or []),
        "max_files": tool_input.get("max_files"),
        "result_chars": len(result_text),
        "result_budget_chars": request.get("result_budget_chars"),
        "returned_paths": _result_paths(result_text),
        "result_excerpt": result_text[:3000],
    }


def _direct_use_detail(use: dict, phase: str) -> dict:
    return {
        "name": use.get("name"),
        "event_index": use.get("event_index"),
        "phase": phase,
        "input": dict(use.get("input") or {}),
    }


def analyze_trace(path: Path) -> dict:
    events = _json_lines(path)
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    uses = _tool_uses(events)
    results = _tool_results(events)

    gremlins_positions = [
        index for index, use in enumerate(uses)
        if _is_gremlins_use(use)
    ]
    evidence_pack_positions = [
        index for index, use in enumerate(uses)
        if use.get("name") == "mcp__gremlins__evidence_pack"
    ]
    first_gremlins = gremlins_positions[0] if gremlins_positions else None
    last_gremlins = gremlins_positions[-1] if gremlins_positions else None

    gremlins_calls: list[dict] = []
    all_returned_paths: set[str] = set()
    for call_number, use_index in enumerate(gremlins_positions, 1):
        use = uses[use_index]
        tool_id = use.get("id")
        result_text = results.get(tool_id, "") if isinstance(tool_id, str) else ""
        detail = _gremlins_call_detail(use, result_text, call_number)
        gremlins_calls.append(detail)
        all_returned_paths.update(detail["returned_paths"])

    direct_evidence: list[dict] = []
    for index, use in enumerate(uses):
        if not _is_direct_evidence_use(use):
            continue
        if first_gremlins is None or index < first_gremlins:
            phase = "before-gremlins"
        elif last_gremlins is not None and index > last_gremlins:
            phase = "after-final-gremlins"
        else:
            phase = "between-gremlins"
        direct_evidence.append(_direct_use_detail(use, phase))

    post_first = [
        item for item in direct_evidence
        if item["phase"] in {"between-gremlins", "after-final-gremlins"}
    ]
    after_final = [
        item for item in direct_evidence
        if item["phase"] == "after-final-gremlins"
    ]

    # Identify whether direct fallback re-read paths already surfaced by
    # Gremlins. This distinguishes evidence insufficiency from redundant
    # frontier verification.
    redundant_fallback: list[dict] = []
    for item in post_first:
        payload = _input_text(item.get("input") or {})
        touched = sorted(
            path for path in all_returned_paths
            if path and path in payload
        )
        if touched:
            redundant_fallback.append({
                **item,
                "gremlins_returned_paths_touched": touched,
            })

    denials = final.get("permission_denials")
    if not isinstance(denials, list):
        denials = []

    usage = final.get("usage") if isinstance(final.get("usage"), dict) else {}
    total_gremlins_result_chars = sum(
        int(item.get("result_chars") or 0) for item in gremlins_calls
    )
    evidence_pack_calls = [
        item for item in gremlins_calls
        if item.get("name") == "mcp__gremlins__evidence_pack"
    ]

    return {
        "raw_file": str(path),
        "num_turns": final.get("num_turns"),
        "terminal_subtype": final.get("subtype"),
        "terminal_reason": final.get("terminal_reason"),
        "tool_sequence": [use.get("name") for use in uses],
        "tool_calls": len(uses),
        "toolsearch_calls": sum(use.get("name") == "ToolSearch" for use in uses),
        "gremlins_calls": len(gremlins_positions),
        "evidence_pack_calls": len(evidence_pack_positions),
        "gremlins_client_result_chars": total_gremlins_result_chars,
        "gremlins_call_details": gremlins_calls,
        "evidence_pack_details": [
            item.get("detail") for item in evidence_pack_calls
        ],
        "evidence_pack_result_chars": [
            int(item.get("result_chars") or 0) for item in evidence_pack_calls
        ],
        "returned_paths": sorted(all_returned_paths),
        "direct_evidence_calls": len(direct_evidence),
        "direct_evidence_uses": direct_evidence,
        "direct_evidence_after_first_gremlins": len(post_first),
        "direct_evidence_after_final_gremlins": len(after_final),
        "after_final_gremlins_uses": after_final,
        "redundant_post_gremlins_calls": len(redundant_fallback),
        "redundant_post_gremlins_uses": redundant_fallback,
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
                int(row.get("direct_evidence_after_first_gremlins") or 0) for row in arm_rows
            ),
            "direct_evidence_calls": sum(
                int(row.get("direct_evidence_calls") or 0) for row in arm_rows
            ),
            "direct_evidence_after_final_gremlins": sum(
                int(row.get("direct_evidence_after_final_gremlins") or 0) for row in arm_rows
            ),
            "redundant_post_gremlins_calls": sum(
                int(row.get("redundant_post_gremlins_calls") or 0) for row in arm_rows
            ),
            "evidence_pack_calls": sum(
                int(row.get("evidence_pack_calls") or 0) for row in arm_rows
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
        "direct_evidence_calls": 0,
        "direct_evidence_after_final_gremlins": 0,
        "redundant_post_gremlins_calls": 0,
        "evidence_pack_calls": 0,
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
