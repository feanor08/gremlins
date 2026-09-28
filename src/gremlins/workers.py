from __future__ import annotations

import json
import re
import time
from typing import Sequence

from pydantic import ValidationError

from .config import Config
from .contracts import RepoSynthesis, TriageSynthesis
from .metrics import record
from .provider import ProviderBusy, ProviderError, chat_json
from .retrieval import Evidence, build_repo_evidence, effective_terms, evidence_json, snapshot
from .security import PolicyError, resolve_repository, validate_task
from .skills import worker_instructions


FINDING_SCHEMA = RepoSynthesis.model_json_schema()
TRIAGE_SCHEMA = TriageSynthesis.model_json_schema()


def _validate_evidence_ids(result: dict, valid: set[str]) -> list[str]:
    invalid: list[str] = []
    for finding in result.get("findings", []):
        for evidence_id in finding.get("evidence_ids", []):
            if evidence_id not in valid:
                invalid.append(evidence_id)
    return invalid


def _compact_evidence(
    evidence: list[Evidence],
    max_chars: int,
    preferred_ids: Sequence[str] | None = None,
) -> list[dict]:
    preferred = list(dict.fromkeys(preferred_ids or []))[:12]
    by_id = {item.id: item for item in evidence}
    preferred_items = [by_id[evidence_id] for evidence_id in preferred if evidence_id in by_id]
    ordered: list[Evidence] = list(preferred_items)

    for kind in ("search", "file", "path", "git"):
        for item in evidence:
            if item.kind == kind and item not in ordered:
                ordered.append(item)

    returned: list[dict] = []
    used = 0
    preferred_budget = max(240, min(1200, max_chars // max(1, len(preferred_items)))) if preferred_items else 1200

    for item in ordered:
        remaining = max_chars - used
        if remaining <= 160:
            break
        data = item.as_dict()
        target_budget = preferred_budget if item in preferred_items else 1000
        text_budget = min(target_budget, max(80, remaining - len(item.path) - 120))
        if len(data["text"]) > text_budget:
            data["text"] = data["text"][:text_budget] + "…"
        cost = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        if used + cost > max_chars:
            continue
        returned.append(data)
        used += cost
        if len(returned) >= 12:
            break
    return returned



def _section_breadcrumbs(excerpt: Evidence | None) -> list[str]:
    if not excerpt or not excerpt.text.startswith("Section context:\n"):
        return []
    rows: list[str] = []
    for line in excerpt.text.splitlines()[1:]:
        match = re.match(r"^\d+:\s+(#{1,6}\s+.+)$", line)
        if not match:
            break
        rows.append(match.group(1)[:180])
        if len(rows) >= 3:
            break
    return rows


def _deterministic_repo_result(
    evidence: list[Evidence],
    search_terms: Sequence[str],
    search_meta: dict,
    snap: dict,
    truncated: bool,
    max_chars: int,
) -> dict:
    """Build a small answer-shaped exact-search result for the frontier."""
    search_hits = [item for item in evidence if item.kind == "search"]
    excerpts: dict[str, Evidence] = {}
    for item in evidence:
        if item.kind == "file":
            excerpts.setdefault(item.path, item)

    by_path: dict[str, list[Evidence]] = {}
    path_order: list[str] = []
    for hit in search_hits:
        if hit.path not in by_path:
            by_path[hit.path] = []
            path_order.append(hit.path)
        by_path[hit.path].append(hit)

    compact_snapshot = {
        "head": snap.get("head"),
        "dirty": bool(snap.get("dirty")),
    }
    coverage = search_meta.get("terms", {})
    hard_cap = max(800, min(int(max_chars), 3000))
    files: list[dict] = []

    # Budget using the final result shape, not a smaller intermediate shape.
    # Use the longest boolean spelling (false) and the maximum possible hit
    # count so later finalization cannot grow the serialized payload.
    base = {
        "status": "complete",
        "snapshot": compact_snapshot,
        "files": files,
        "coverage": {"terms": coverage},
        "hits_returned": len(search_hits),
        "hits_ranked": len(search_hits),
        "truncated": False,
        "usage": {
            "local_model_called": False,
            "search_terms": list(search_terms),
            "fallback_terms": list(search_meta.get("fallback_terms") or []),
            "separator_variants": list(search_meta.get("separator_variants") or []),
            "effective_terms": list(search_meta.get("effective_terms") or search_terms),
            "mode": "deterministic",
        },
    }

    # First spend the budget on ranked exact hits so an early context excerpt
    # can never crowd a later relevant file out of the frontier result.
    for path in path_order:
        candidate = {
            "path": path,
            "hits": [
                {"line": hit.start_line, "text": hit.text[:320]}
                for hit in by_path[path][:3]
            ],
        }
        # Preserve a tiny section identity for the top-ranked file even when
        # there is not enough room for the full excerpt later.
        if not files:
            section = _section_breadcrumbs(excerpts.get(path))
            if section:
                candidate["section"] = section
        trial = {**base, "files": [*files, candidate]}
        if len(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) > hard_cap:
            break
        files.append(candidate)

    # Then add small hit-centered context only while spare budget remains.
    for index, item in enumerate(files[:3]):
        excerpt = excerpts.get(item["path"])
        if not excerpt:
            continue
        context = {
            "start_line": excerpt.start_line,
            "end_line": excerpt.end_line,
            "text": excerpt.text[:500],
        }
        enriched_files = [dict(value) for value in files]
        enriched_files[index]["excerpt"] = context
        trial = {**base, "files": enriched_files}
        if len(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) <= hard_cap:
            files = enriched_files

    returned_hits = sum(len(item["hits"]) for item in files)
    omitted_ranked_hits = max(0, len(search_hits) - returned_hits)
    base["files"] = files
    base["hits_returned"] = returned_hits
    base["hits_ranked"] = len(search_hits)
    base["truncated"] = bool(truncated or omitted_ranked_hits)
    if not files:
        base["status"] = "partial"
    return base

def _compact_lines(lines: list[str], max_chars: int) -> list[str]:
    result: list[str] = []
    used = 0
    for line in lines:
        clipped = line[:1200]
        if used + len(clipped) + 1 > max_chars:
            break
        result.append(clipped)
        used += len(clipped) + 1
    return result


def repo_explore(
    repository: str,
    task: str,
    config: Config,
    scope: str = ".",
    terms: Sequence[str] | None = None,
    symbols: Sequence[str] | None = None,
    mode: str = "auto",
    measurement_tag: str | None = None,
) -> dict:
    started = time.monotonic()
    task = validate_task(task, config.limits.max_task_chars)
    if mode not in {"auto", "deterministic", "model"}:
        raise PolicyError("mode must be one of: auto, deterministic, model")

    repo = resolve_repository(repository, config)
    search_terms = effective_terms(task, terms=terms, symbols=symbols)
    evidence, truncated, search_meta = build_repo_evidence(
        repo,
        task,
        config,
        scope=scope,
        terms=terms,
        symbols=symbols,
    )
    snap = snapshot(repo, config)

    deterministic_only = mode in {"auto", "deterministic"}
    if deterministic_only:
        result = _deterministic_repo_result(
            evidence,
            search_terms,
            search_meta,
            snap,
            truncated,
            config.limits.max_result_evidence_chars,
        )
        record({
            "worker": "repo-explorer",
            "status": result["status"],
            "elapsed_seconds": round(time.monotonic()-started,3),
            "evidence_count": len(evidence),
            "evidence_returned": result.get("hits_returned", 0),
            "result_chars": len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))),
            "measurement_tag": measurement_tag,
            "usage": result["usage"],
        })
        return result

    if not evidence:
        result = {
            "status": "partial",
            "snapshot": {"head": snap.get("head"), "dirty": bool(snap.get("dirty"))},
            "summary": "No repository evidence was found for model synthesis.",
            "findings": [],
            "evidence": [],
            "evidence_total": 0,
            "evidence_returned": 0,
            "limitations": ["No matching repository evidence was found."],
            "next_queries": search_terms,
            "truncated": False,
            "usage": {"local_model_called": False, "search_terms": search_terms, "mode": "model"},
        }
        record({
            "worker": "repo-explorer",
            "status": result["status"],
            "elapsed_seconds": round(time.monotonic()-started,3),
            "evidence_count": 0,
            "result_chars": len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))),
            "measurement_tag": measurement_tag,
            "usage": result["usage"],
        })
        return result

    skill_text = worker_instructions(config, "repo-explorer")
    system = (
        "You are Gremlins repo-explorer, a read-only local worker. Use ONLY the supplied evidence. "
        "Never invent files, line numbers, commits, APIs, or behavior. Distinguish observations from inference. "
        "Keep the result compact. Cite evidence only by its exact id. If evidence is insufficient, say so.\n\n"
        + skill_text
    )
    user = f"TASK:\n{task}\n\nREPOSITORY SNAPSHOT:\n{json.dumps(snap)}\n\nEVIDENCE:\n{evidence_json(evidence)}"
    try:
        raw_result, usage = chat_json(config, system, user, FINDING_SCHEMA)
        model_result = RepoSynthesis.model_validate(raw_result).model_dump()
        invalid = _validate_evidence_ids(model_result, {e.id for e in evidence})
        if invalid:
            model_result.setdefault("limitations", []).append(
                f"Model returned invalid evidence ids that were removed: {sorted(set(invalid))}"
            )
            for finding in model_result.get("findings", []):
                finding["evidence_ids"] = [x for x in finding.get("evidence_ids", []) if x not in invalid]
        status = "partial" if truncated else "complete"
    except ProviderBusy as exc:
        model_result = {
            "summary": "Repository evidence was collected, but the local inference slot is busy.",
            "findings": [],
            "limitations": [str(exc)],
            "next_queries": [],
        }
        usage = {"local_model_called": False, "provider_error": str(exc), "search_terms": search_terms}
        status = "busy"
    except (ProviderError, ValidationError) as exc:
        model_result = {
            "summary": "Repository evidence was collected, but local inference was unavailable.",
            "findings": [],
            "limitations": [str(exc)],
            "next_queries": [],
        }
        usage = {"local_model_called": False, "provider_error": str(exc), "search_terms": search_terms}
        status = "needs-frontier"

    preferred_ids = [
        evidence_id
        for finding in model_result.get("findings", [])
        for evidence_id in finding.get("evidence_ids", [])
    ]
    compact = _compact_evidence(evidence, config.limits.max_result_evidence_chars, preferred_ids)
    returned_ids = {item["id"] for item in compact}
    omitted_citations = 0
    for finding in model_result.get("findings", []):
        before = list(finding.get("evidence_ids", []))
        finding["evidence_ids"] = [evidence_id for evidence_id in before if evidence_id in returned_ids]
        omitted_citations += len(before) - len(finding["evidence_ids"])
    if omitted_citations:
        model_result.setdefault("limitations", []).append(
            f"{omitted_citations} cited evidence references were omitted by the frontier result-size budget."
        )
    result = {
        "status": status,
        "snapshot": snap,
        **model_result,
        "evidence": compact,
        "evidence_total": len(evidence),
        "evidence_returned": len(compact),
        "truncated": truncated,
        "usage": usage,
    }
    record({
        "worker": "repo-explorer",
        "status": status,
        "elapsed_seconds": round(time.monotonic()-started,3),
        "evidence_count": len(evidence),
        "evidence_returned": len(compact),
        "result_chars": len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))),
        "measurement_tag": measurement_tag,
        "usage": usage,
    })
    return result


def _extract_log_evidence(text: str, max_chars: int = 32000) -> list[str]:
    lines = text.splitlines()
    patterns = re.compile(r"(?i)(error|failed|failure|exception|traceback|panic|fatal|assert|timeout|segfault|exit code|non-zero)")
    indexes = [i for i, line in enumerate(lines) if patterns.search(line)]
    if not indexes:
        indexes = list(range(min(len(lines), 80)))
    selected: list[str] = []
    seen: set[int] = set()
    for idx in indexes[:80]:
        for pos in range(max(0, idx-2), min(len(lines), idx+4)):
            if pos in seen:
                continue
            seen.add(pos)
            selected.append(f"L{pos+1}: {lines[pos]}")
            if sum(len(x)+1 for x in selected) >= max_chars:
                return selected
    return selected


def triage(text: str, task: str, config: Config, measurement_tag: str | None = None) -> dict:
    started = time.monotonic()
    task = validate_task(task, config.limits.max_task_chars)
    text = text[-max(config.limits.max_evidence_chars * 4, 64000):]
    evidence = _extract_log_evidence(text, max_chars=config.limits.max_evidence_chars)
    skill_text = worker_instructions(config, "triage")
    system = (
        "You are Gremlins triage, a read-only local worker. Analyze ONLY the provided log excerpts. "
        "Group duplicate failures. Separate direct evidence from possible causes. Do not claim a root cause is proven "
        "unless the logs directly prove it. Keep the output compact and actionable.\n\n"
        + skill_text
    )
    user = f"TASK:\n{task}\n\nLOG EVIDENCE:\n" + "\n".join(evidence)
    try:
        raw_result, usage = chat_json(config, system, user, TRIAGE_SCHEMA)
        model_result = TriageSynthesis.model_validate(raw_result).model_dump()
        status = "complete"
    except ProviderBusy as exc:
        model_result = {
            "summary": "Failure-like log lines were extracted, but the local inference slot is busy.",
            "failure_groups": [],
            "next_checks": [],
            "limitations": [str(exc)],
        }
        usage = {"local_model_called": False, "provider_error": str(exc)}
        status = "busy"
    except (ProviderError, ValidationError) as exc:
        model_result = {
            "summary": "Failure-like log lines were extracted, but local inference was unavailable.",
            "failure_groups": [],
            "next_checks": [],
            "limitations": [str(exc)],
        }
        usage = {"local_model_called": False, "provider_error": str(exc)}
        status = "needs-frontier"

    compact = _compact_lines(evidence, config.limits.max_result_evidence_chars)
    result = {
        "status": status,
        **model_result,
        "evidence": compact,
        "evidence_lines_total": len(evidence),
        "evidence_lines_returned": len(compact),
        "usage": usage,
    }
    record({
        "worker": "triage",
        "status": status,
        "elapsed_seconds": round(time.monotonic()-started,3),
        "evidence_lines": len(evidence),
        "evidence_lines_returned": len(compact),
        "result_chars": len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))),
        "measurement_tag": measurement_tag,
        "usage": usage,
    })
    return result
