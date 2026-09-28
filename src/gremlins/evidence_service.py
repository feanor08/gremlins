from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Sequence

from .config import Config
from .metrics import record
from .retrieval import (
    Evidence,
    build_repo_evidence,
    candidate_terms,
    effective_terms,
    git_history,
    literal_search,
    path_inventory,
    read_excerpt,
    snapshot,
)
from .security import _safe_env, resolve_repo_file, resolve_repository, validate_task


_STOP_WORDS = {
    "the", "and", "for", "with", "from", "this", "that", "where", "what", "which",
    "find", "show", "please", "repository", "code", "into", "onto", "than", "then",
    "does", "did", "doing", "have", "has", "had", "will", "would", "could", "should",
    "system", "using", "used", "use", "relevant", "whether", "without", "through",
}
_TEST_PARTS = {"test", "tests", "spec", "specs"}


def _run_git(repo: Path, args: list[str], config: Config) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=config.limits.command_timeout_seconds,
        check=False,
        env=_safe_env(),
    )


def _is_test_path(path: str) -> bool:
    p = Path(path)
    name = p.name.lower()
    parts = {part.lower() for part in p.parts}
    return (
        bool(parts & _TEST_PARTS)
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".spec.ts")
        or name.endswith(".test.ts")
        or name.endswith(".spec.js")
        or name.endswith(".test.js")
    )


def _path_role(path: str) -> str:
    if _is_test_path(path):
        return "test"
    suffix = Path(path).suffix.lower()
    if suffix in {".md", ".mdx", ".rst", ".txt"}:
        return "documentation"
    if suffix in {".yml", ".yaml", ".toml", ".json", ".ini", ".cfg"}:
        return "configuration"
    return "source"


def _tracked_paths(repo: Path, config: Config) -> list[str]:
    proc = _run_git(repo, ["ls-files"], config)
    if proc.returncode != 0:
        return []
    # Respect sparse/isolated benchmark worktrees: an index entry whose file is
    # intentionally absent must not re-enter discovery merely through git ls-files.
    return [
        path for path in proc.stdout.splitlines()
        if (repo / path).is_file()
    ]


def _dedupe(values: Sequence[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _singular_word(word: str) -> str:
    low = word.lower()
    if len(low) > 4 and low.endswith("ies"):
        return low[:-3] + "y"
    if len(low) > 4 and low.endswith("ses"):
        return low[:-2]
    if len(low) > 3 and low.endswith("s") and not low.endswith("ss"):
        return low[:-1]
    return low


def _variant_candidates(task: str, base_terms: Sequence[str], limit: int = 24) -> list[str]:
    candidates: list[str] = []
    for term in base_terms:
        if " " in term:
            pieces = [piece for piece in re.findall(r"[A-Za-z0-9]+", term) if piece]
            if len(pieces) >= 2:
                candidates.extend(["_".join(pieces), "-".join(pieces), ".".join(pieces)])
        elif "_" in term:
            candidates.extend([term.replace("_", "-"), term.replace("_", ".")])
        elif "-" in term:
            candidates.extend([term.replace("-", "_"), term.replace("-", ".")])

    words = [
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", task)
        if word.lower() not in _STOP_WORDS
    ]
    normalized = [_singular_word(word) for word in words]
    for word, singular in zip(words, normalized):
        if singular != word:
            candidates.append(singular)

    for sequence in (normalized, words):
        for width in (2, 3):
            for index in range(max(0, len(sequence) - width + 1)):
                parts = sequence[index:index + width]
                if len(parts) != width:
                    continue
                candidates.extend(["_".join(parts), "-".join(parts), ".".join(parts)])
                if len(candidates) >= limit * 4:
                    break
    return _dedupe(candidates, limit)


def _discovery_terms(
    repo: Path,
    task: str,
    config: Config,
    scope: str,
    terms: Sequence[str] | None,
    symbols: Sequence[str] | None,
) -> tuple[list[str], list[str]]:
    explicit = bool(terms or symbols)
    base = effective_terms(task, terms=terms, symbols=symbols)
    if explicit:
        return base, []

    base = _dedupe([*base, *candidate_terms(task)], 12)
    matched_variants: list[str] = []
    existing = {term.lower() for term in base}
    for candidate in _variant_candidates(task, base):
        if candidate.lower() in existing:
            continue
        if literal_search(repo, candidate, config, scope=scope):
            matched_variants.append(candidate)
        if len(matched_variants) >= 4:
            break
    return _dedupe([*base, *matched_variants], 16), matched_variants


def _search_terms_for_path(path: str, hits: list[Evidence], search_terms: Sequence[str]) -> list[str]:
    texts = [path.lower(), *(item.text.lower() for item in hits)]
    return [
        term
        for term in search_terms
        if any(term.lower() in text for text in texts)
    ]


def _python_module_path(module: str, tracked: set[str]) -> str | None:
    module = module.strip(".")
    if module.startswith("gremlins."):
        module = module[len("gremlins."):]
    module = module.replace(".", "/")
    candidates = [
        f"src/gremlins/{module}.py",
        f"src/gremlins/{module}/__init__.py",
    ]
    return next((path for path in candidates if path in tracked), None)


def _python_neighbors(
    repo: Path,
    seed_paths: Sequence[str],
    config: Config,
    limit: int = 8,
) -> list[str]:
    tracked_list = _tracked_paths(repo, config)
    tracked = set(tracked_list)
    python_paths = [
        path for path in tracked_list
        if path.startswith("src/gremlins/") and path.endswith(".py")
    ]
    seed_modules = {
        Path(path).stem
        for path in seed_paths
        if path.startswith("src/gremlins/") and path.endswith(".py")
    }
    neighbors: list[str] = []

    # Forward local imports from the currently relevant files.
    for path in seed_paths:
        if path not in tracked or not path.endswith(".py"):
            continue
        try:
            text = resolve_repo_file(repo, path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(
            r"^\s*from\s+(?:gremlins\.)?([A-Za-z_][A-Za-z0-9_.]*)\s+import\s+",
            text,
            re.MULTILINE,
        ):
            target = _python_module_path(match.group(1), tracked)
            if target and target not in seed_paths and target not in neighbors:
                neighbors.append(target)
        for match in re.finditer(
            r"^\s*from\s+\.([A-Za-z_][A-Za-z0-9_.]*)\s+import\s+",
            text,
            re.MULTILINE,
        ):
            target = _python_module_path(match.group(1), tracked)
            if target and target not in seed_paths and target not in neighbors:
                neighbors.append(target)

    # Reverse local import edges: if a relevant module is used elsewhere, that
    # importer is useful evidence for flow/caller questions.
    for path in python_paths:
        if path in seed_paths:
            continue
        try:
            text = resolve_repo_file(repo, path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for module in seed_modules:
            patterns = (
                rf"^\s*from\s+\.{re.escape(module)}\s+import\s+",
                rf"^\s*from\s+gremlins\.{re.escape(module)}\s+import\s+",
                rf"^\s*import\s+gremlins\.{re.escape(module)}\b",
            )
            if any(re.search(pattern, text, re.MULTILINE) for pattern in patterns):
                if path not in neighbors:
                    neighbors.append(path)
                break

    return neighbors[:limit]


def _top_paths(
    repo: Path,
    evidence: list[Evidence],
    search_terms: Sequence[str],
    config: Config,
    max_files: int,
    focus_paths: Sequence[str] | None,
) -> list[str]:
    focused: list[str] = []
    for raw in focus_paths or []:
        file = resolve_repo_file(repo, raw)
        rel = str(file.relative_to(repo))
        if rel not in focused:
            focused.append(rel)

    hits_by_path: dict[str, list[Evidence]] = defaultdict(list)
    for item in evidence:
        if item.path and item.kind == "search":
            hits_by_path[item.path].append(item)

    inventory_paths = [
        item.path for item in path_inventory(repo, search_terms, config)
        if item.path
    ]
    candidates = [
        path for path in _dedupe(
            [*focused, *hits_by_path.keys(), *inventory_paths],
            120,
        )
        if (repo / path).is_file()
    ]

    def base_score(path: str) -> tuple[int, int, int, int]:
        hits = hits_by_path.get(path, [])
        matched = _search_terms_for_path(path, hits, search_terms)
        low = path.lower()
        name = Path(low).name
        path_term_score = sum(
            12 if term.lower() in name else 4
            for term in search_terms
            if term.lower() in low
        )
        return (
            1 if path in focused else 0,
            path_term_score,
            len({term.lower() for term in matched}),
            min(len(hits), 6),
        )

    prelim = sorted(
        candidates,
        key=lambda path: (
            -base_score(path)[0],
            -base_score(path)[1],
            -base_score(path)[2],
            -base_score(path)[3],
            len(path),
            path,
        ),
    )

    source_seeds = [path for path in prelim if _path_role(path) == "source"][:5]
    structural = set(_python_neighbors(repo, source_seeds, config, limit=10))
    for path in structural:
        if path not in candidates:
            candidates.append(path)

    def sort_key(path: str) -> tuple:
        focused_score, path_term_score, term_count, hit_count = base_score(path)
        return (
            -focused_score,
            -(1 if path in structural else 0),
            -path_term_score,
            -term_count,
            -hit_count,
            len(path),
            path,
        )

    ranked = sorted(candidates, key=sort_key)
    selected: list[str] = list(focused[:max_files])

    # First guarantee representation for each evidence role where candidates
    # exist. Then grow the useful roles. This avoids both source-only packs and
    # test/doc/config files crowding out the implementation.
    for role in ("source", "test", "documentation", "configuration"):
        if len(selected) >= max_files:
            break
        if any(_path_role(path) == role for path in selected):
            continue
        candidate = next(
            (path for path in ranked if _path_role(path) == role and path not in selected),
            None,
        )
        if candidate:
            selected.append(candidate)

    role_targets = {
        "source": 7,
        "test": 2,
        "documentation": 2,
        "configuration": 2,
    }
    for role, target in role_targets.items():
        already = sum(_path_role(path) == role for path in selected)
        for path in ranked:
            if len(selected) >= max_files or already >= target:
                break
            if path in selected or _path_role(path) != role:
                continue
            selected.append(path)
            already += 1

    for path in ranked:
        if len(selected) >= max_files:
            break
        if path not in selected:
            selected.append(path)
    return selected[:max_files]


def _related_path_index(
    repo: Path,
    evidence: list[Evidence],
    search_terms: Sequence[str],
    selected_paths: Sequence[str],
    config: Config,
    limit: int = 32,
) -> list[dict]:
    hits_by_path: dict[str, list[Evidence]] = defaultdict(list)
    for item in evidence:
        if item.kind == "search" and item.path and (repo / item.path).is_file():
            hits_by_path[item.path].append(item)

    structural = set(
        _python_neighbors(
            repo,
            [path for path in selected_paths if _path_role(path) == "source"][:6],
            config,
            limit=12,
        )
    )
    tracked = _tracked_paths(repo, config)

    # Test/source filename affinity is cheap, deterministic, and was a common
    # manual Claude lookup in the observation corpus. Include candidate tests
    # even when the detailed file slots are already full.
    affine_tests: list[str] = []
    source_basis = [
        path for path in [*selected_paths, *structural]
        if _path_role(path) == "source"
    ]
    for source in source_basis:
        stem = Path(source).stem.lower()
        if stem == "__init__":
            stem = Path(source).parent.name.lower()
        if len(stem) < 3:
            continue
        for path in tracked:
            if _is_test_path(path) and stem in Path(path).name.lower():
                if path not in affine_tests:
                    affine_tests.append(path)

    inventory = [
        item.path for item in path_inventory(repo, search_terms, config)
        if item.path and (repo / item.path).is_file()
    ]
    candidates = _dedupe(
        [*selected_paths, *structural, *affine_tests, *hits_by_path.keys(), *inventory],
        140,
    )
    selected_rank = {path: index for index, path in enumerate(selected_paths)}

    def score(path: str) -> tuple[int, int, int, int, int, int]:
        hits = hits_by_path.get(path, [])
        matched = _search_terms_for_path(path, hits, search_terms)
        low = path.lower()
        name = Path(low).name
        path_term_score = sum(
            12 if term.lower() in name else 4
            for term in search_terms
            if term.lower() in low
        )
        return (
            1 if path in selected_rank else 0,
            1 if path in structural else 0,
            1 if path in affine_tests else 0,
            path_term_score,
            len({term.lower() for term in matched}),
            min(len(hits), 6),
        )

    ranked = sorted(
        candidates,
        key=lambda path: (
            -(100 - selected_rank[path]) if path in selected_rank else 0,
            -score(path)[1],
            -score(path)[2],
            -score(path)[3],
            -score(path)[4],
            -score(path)[5],
            len(path),
            path,
        ),
    )

    chosen: list[str] = []

    def add(path: str) -> None:
        if path not in chosen and len(chosen) < limit:
            chosen.append(path)

    for path in selected_paths:
        add(path)
    for path in affine_tests:
        add(path)

    # Preserve breadth in the compact path-only index. These entries are cheap
    # and let the caller focus the next evidence request without repeating a
    # broad frontier search.
    role_targets = {
        "source": 16,
        "test": 6,
        "documentation": 5,
        "configuration": 4,
    }
    for role, target in role_targets.items():
        count = sum(_path_role(path) == role for path in chosen)
        for path in ranked:
            if len(chosen) >= limit or count >= target:
                break
            if path in chosen or _path_role(path) != role:
                continue
            add(path)
            count += 1

    for path in ranked:
        add(path)
        if len(chosen) >= limit:
            break

    return [
        {
            "path": path,
            "role": _path_role(path),
            "detailed": path in selected_rank,
        }
        for path in chosen
    ]


def _test_relationships(
    repo: Path,
    paths: Sequence[str],
    search_terms: Sequence[str],
    config: Config,
    limit: int = 10,
) -> list[dict]:
    tracked = _tracked_paths(repo, config)
    tests = [path for path in tracked if _is_test_path(path)]
    sources = [path for path in tracked if not _is_test_path(path)]

    term_test_hits: dict[str, list[Evidence]] = defaultdict(list)
    for term in search_terms[:4]:
        if len(term) < 3:
            continue
        for hit in literal_search(repo, term, config):
            if _is_test_path(hit.path):
                term_test_hits[term].append(hit)

    relationships: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(source: str, test: str, score: int, reason: str, hit: Evidence | None = None) -> None:
        key = (source, test)
        if key in seen or len(relationships) >= limit:
            return
        seen.add(key)
        item = {
            "type": "test-source",
            "source": source,
            "test": test,
            "score": score,
            "reason": reason,
        }
        if hit is not None:
            item["evidence"] = {
                "path": hit.path,
                "line": hit.start_line,
                "text": hit.text[:220],
            }
        relationships.append(item)

    for path in paths:
        if _is_test_path(path):
            stem = Path(path).stem.lower()
            stem = re.sub(r"^test_", "", stem)
            stem = re.sub(r"_test$", "", stem)
            if stem and stem not in {"test", "tests"}:
                for source in sources:
                    source_name = Path(source).stem.lower()
                    if stem in source_name or source_name in stem:
                        add(source, path, 8, "test/source filename affinity")
            continue

        stem = Path(path).stem.lower()
        if stem == "__init__":
            stem = Path(path).parent.name.lower()

        for test in tests:
            if stem and len(stem) >= 3 and stem in test.lower():
                add(path, test, 8, "source basename appears in test path")

        matched_terms = {
            term.lower()
            for term in search_terms
            if term.lower() in path.lower()
        }
        for term, hits in term_test_hits.items():
            if matched_terms and term.lower() not in matched_terms:
                continue
            for hit in hits[:4]:
                add(path, hit.path, 5, f"shared query term: {term}", hit)

    relationships.sort(key=lambda item: (-int(item["score"]), item["source"], item["test"]))
    return relationships[:limit]


def _file_entry(
    repo: Path,
    path: str,
    evidence: list[Evidence],
    search_terms: Sequence[str],
    config: Config,
    focused: bool,
) -> dict:
    hits = [item for item in evidence if item.kind == "search" and item.path == path][:3]
    excerpt = next(
        (item for item in evidence if item.kind == "file" and item.path == path),
        None,
    )
    if excerpt is None:
        start_line = max(1, int(hits[0].start_line or 1) - 3) if hits else 1
        try:
            excerpt = read_excerpt(repo, path, config, start_line=start_line, line_count=16)
        except (OSError, RuntimeError):
            excerpt = None

    matched = _search_terms_for_path(path, hits, search_terms)
    score = len(set(term.lower() for term in matched)) * 10 + len(hits) * 2 + (6 if focused else 0)
    return {
        "path": path,
        "role": _path_role(path),
        "score": score,
        "reasons": (
            (["caller-focused path"] if focused else [])
            + ([f"matched {len(set(matched))} query term(s)"] if matched else ["path/name relevance"])
        ),
        "matched_terms": matched[:8],
        "hits": [
            {"line": item.start_line, "text": item.text[:260]}
            for item in hits
        ],
        "excerpt": (
            {
                "start_line": excerpt.start_line,
                "end_line": excerpt.end_line,
                "text": excerpt.text[:700],
            }
            if excerpt is not None
            else None
        ),
    }


def _history_bundle(
    repo: Path,
    paths: Sequence[str],
    search_terms: Sequence[str],
    config: Config,
) -> dict:
    by_path: list[dict] = []
    for path in paths[:4]:
        entries = git_history(repo, config, path=path)[:2]
        if entries:
            by_path.append({
                "path": path,
                "entries": [entry.text[:320] for entry in entries],
            })

    topic: list[dict] = []
    for term in search_terms[:2]:
        entries = git_history(repo, config, query=term)[:2]
        if entries:
            topic.append({
                "query": term,
                "entries": [entry.text[:320] for entry in entries],
            })
    return {"by_path": by_path, "topic": topic}


def _compact_pack(result: dict, max_chars: int) -> dict:
    result["truncated"] = bool(result.get("truncated"))

    def size() -> int:
        return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))

    while size() > max_chars:
        changed = False
        history = result.get("history", {})
        topic = history.get("topic", [])
        by_path = history.get("by_path", [])

        # First reduce duplicate history breadth but preserve at least one
        # history group when history exists; history is first-class evidence,
        # not decoration to discard before source excerpts.
        if len(topic) > 1:
            topic.pop()
            changed = True
        elif len(by_path) > 1:
            by_path.pop()
            changed = True
        else:
            files = result.get("files", [])
            # Drop low-ranked excerpts before deleting an entire evidence
            # category. The top two file excerpts are retained longest.
            for item in reversed(files[2:]):
                if item.get("excerpt") is not None:
                    item["excerpt"] = None
                    changed = True
                    break
            if not changed:
                for item in reversed(files):
                    if len(item.get("hits") or []) > 1:
                        item["hits"].pop()
                        changed = True
                        break
            if not changed and len(result.get("relationships") or []) > 1:
                result["relationships"].pop()
                changed = True
            if not changed and len(files) > 1:
                files.pop()
                changed = True
            # Only as a last resort may the final history group be removed.
            if not changed and topic:
                topic.pop()
                changed = True
            if not changed and by_path:
                by_path.pop()
                changed = True

        if not changed:
            break
        result["truncated"] = True
    return result


def evidence_pack(
    repository: str,
    task: str,
    config: Config,
    *,
    scope: str = ".",
    terms: Sequence[str] | None = None,
    symbols: Sequence[str] | None = None,
    paths: Sequence[str] | None = None,
    include_tests: bool = True,
    include_history: bool = True,
    max_files: int = 6,
    measurement_tag: str | None = None,
) -> dict:
    """Build a deterministic provenance-preserving evidence bundle."""
    started = time.monotonic()
    task = validate_task(task, config.limits.max_task_chars)
    repo = resolve_repository(repository, config)
    max_files = max(1, min(int(max_files), 12))

    search_terms, matched_variants = _discovery_terms(
        repo, task, config, scope, terms, symbols
    )
    evidence, evidence_truncated, search_meta = build_repo_evidence(
        repo,
        task,
        config,
        scope=scope,
        terms=search_terms,
        symbols=None,
    )
    selected_paths = _top_paths(
        repo, evidence, search_terms, config, max_files, paths
    )
    focused = {
        str(resolve_repo_file(repo, path).relative_to(repo))
        for path in (paths or [])
    }

    files = [
        _file_entry(
            repo,
            path,
            evidence,
            search_terms,
            config,
            focused=path in focused,
        )
        for path in selected_paths
    ]
    files.sort(key=lambda item: (-int(item["score"]), item["path"]))

    related_paths = _related_path_index(
        repo,
        evidence,
        search_terms,
        [item["path"] for item in files],
        config,
    )
    relationships = (
        _test_relationships(
            repo,
            [item["path"] for item in files],
            search_terms,
            config,
        )
        if include_tests
        else []
    )
    history = (
        _history_bundle(
            repo,
            [item["path"] for item in files],
            search_terms,
            config,
        )
        if include_history
        else {"by_path": [], "topic": []}
    )

    snap = snapshot(repo, config)
    result = {
        "status": "complete" if files else "partial",
        "snapshot": {
            "head": snap.get("head"),
            "dirty": bool(snap.get("dirty")),
        },
        "request": {
            "scope": scope,
            "focus_paths": sorted(focused),
            "include_tests": bool(include_tests),
            "include_history": bool(include_history),
            "max_files": max_files,
        },
        "discovery": {
            "terms": search_terms,
            "matched_fuzzy_variants": matched_variants,
            "coverage": search_meta.get("terms", {}),
        },
        "files": files,
        "related_paths": related_paths,
        "relationships": relationships,
        "history": history,
        "truncated": bool(evidence_truncated or search_meta.get("truncated")),
        "usage": {
            "local_model_called": False,
            "mode": "deterministic-evidence",
            "reasoning_performed": False,
            "repeatable": True,
        },
    }
    result = _compact_pack(result, config.limits.max_result_evidence_chars)
    elapsed = round(time.monotonic() - started, 3)
    result_chars = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    record({
        "worker": "evidence-pack",
        "status": result["status"],
        "elapsed_seconds": elapsed,
        "evidence_count": len(evidence),
        "evidence_returned": len(result.get("files") or []),
        "relationships_returned": len(result.get("relationships") or []),
        "result_chars": result_chars,
        "measurement_tag": measurement_tag,
        "usage": result["usage"],
    })
    return result
