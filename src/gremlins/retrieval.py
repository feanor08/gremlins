from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from pathlib import Path
import json
import re
import shutil
import subprocess
from typing import Iterable, Sequence

from .config import Config
from .security import PolicyError, resolve_repo_file, _safe_env


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: str
    path: str
    start_line: int | None
    end_line: int | None
    text: str

    def as_dict(self) -> dict:
        return asdict(self)


def _run(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env=_safe_env(),
    )


def snapshot(repo: Path, config: Config) -> dict:
    repo = repo.resolve()
    head = _run(["git", "rev-parse", "HEAD"], repo, config.limits.command_timeout_seconds)
    status = _run(["git", "status", "--porcelain=v1"], repo, config.limits.command_timeout_seconds)
    return {
        "repository": str(repo),
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()),
        "status": status.stdout.splitlines()[:40],
    }


def _scope_arg(repo: Path, scope: str) -> str:
    repo = repo.resolve()
    candidate = Path(scope).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (repo / candidate).resolve()
    try:
        relative = resolved.relative_to(repo)
    except ValueError as exc:
        raise PolicyError("scope escapes repository") from exc
    if not resolved.exists():
        raise PolicyError(f"scope does not exist: {scope}")
    return "." if str(relative) == "." else str(relative)


def candidate_terms(task: str) -> list[str]:
    quoted = re.findall(r"[\`\"']([^\`\"']{2,80})[\`\"']", task)
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.:/-]{2,}", task)
    stop = {
        "the", "and", "for", "with", "from", "this", "that", "where", "what", "which",
        "find", "show", "please", "repository", "code",
    }
    terms: list[str] = []
    for term in quoted + tokens:
        if term.lower() in stop:
            continue
        if term not in terms:
            terms.append(term)
    return sorted(terms[:12], key=len, reverse=True)


def effective_terms(
    task: str,
    terms: Sequence[str] | None = None,
    symbols: Sequence[str] | None = None,
) -> list[str]:
    supplied: list[str] = []
    for value in [*(symbols or []), *(terms or [])]:
        cleaned = value.strip()
        if not cleaned or "\n" in cleaned or "\r" in cleaned:
            continue
        cleaned = cleaned[:160]
        if cleaned not in supplied:
            supplied.append(cleaned)
        if len(supplied) >= 16:
            break
    return supplied if supplied else candidate_terms(task)


def path_inventory(repo: Path, search_terms: Sequence[str], config: Config) -> list[Evidence]:
    proc = _run(["git", "ls-files"], repo, config.limits.command_timeout_seconds)
    if proc.returncode != 0:
        return []
    terms = [t.lower() for t in search_terms]
    scored: list[tuple[int, str]] = []
    for path in proc.stdout.splitlines():
        low = path.lower()
        score = sum(3 if term in Path(low).name else 1 for term in terms if term in low)
        if score:
            scored.append((score, path))
    scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    return [Evidence(f"p{i}", "path", path, None, None, path) for i, (_, path) in enumerate(scored[:30], 1)]


def literal_search(repo: Path, query: str, config: Config, scope: str = ".") -> list[Evidence]:
    repo = repo.resolve()
    query = query.strip()
    if not query:
        return []
    max_matches = config.limits.max_search_matches
    items: list[Evidence] = []
    safe_scope = _scope_arg(repo, scope)
    if shutil.which("rg"):
        proc = _run(
            [
                "rg",
                "--line-number",
                "--with-filename",
                "--no-heading",
                "--color",
                "never",
                "--fixed-strings",
                "--hidden",
                "--glob",
                "!.git/**",
                "--",
                query,
                safe_scope,
            ],
            repo,
            config.limits.command_timeout_seconds,
        )
        lines = proc.stdout.splitlines()[:max_matches]
        for idx, line in enumerate(lines, 1):
            match = re.match(r"^(.*?):(\d+):(.*)$", line)
            if not match:
                continue
            path, line_no, text = match.groups()
            if path.startswith("./"):
                path = path[2:]
            items.append(Evidence(f"s{idx}", "search", path, int(line_no), int(line_no), text[:1000]))
        return items

    # Portable fallback: ask Git for tracked plus untracked/non-ignored files
    # instead of recursively walking build environments such as .venv or
    # __pycache__. This keeps fallback semantics close to ripgrep's normal
    # repository-aware ignore behavior.
    proc = _run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", safe_scope],
        repo,
        config.limits.command_timeout_seconds,
    )
    if proc.returncode != 0:
        return []

    for rel in proc.stdout.splitlines():
        if len(items) >= max_matches:
            break
        try:
            file = resolve_repo_file(repo, rel)
            with file.open("rb") as raw:
                if b"\x00" in raw.read(4096):
                    continue
            with file.open("r", encoding="utf-8", errors="ignore") as fh:
                for number, line in enumerate(fh, 1):
                    if query in line:
                        items.append(Evidence(f"s{len(items)+1}", "search", rel, number, number, line.rstrip()[:1000]))
                        if len(items) >= max_matches:
                            break
        except (OSError, RuntimeError):
            continue
    return items


def read_excerpt(repo: Path, path: str, config: Config, start_line: int = 1, line_count: int = 120) -> Evidence:
    repo = repo.resolve()
    file = resolve_repo_file(repo, path)
    start_line = max(1, int(start_line))
    line_count = min(max(1, int(line_count)), 240)
    text_lines: list[str] = []
    end_line = start_line - 1
    with file.open("r", encoding="utf-8", errors="replace") as fh:
        for number, line in enumerate(fh, 1):
            if number < start_line:
                continue
            if number >= start_line + line_count:
                break
            text_lines.append(f"{number}: {line.rstrip()}\n")
            end_line = number
            if sum(len(x) for x in text_lines) >= config.limits.max_file_chars:
                break
    rel = str(file.relative_to(repo))
    return Evidence("file", "file", rel, start_line, end_line, "".join(text_lines))


def contextual_excerpt(
    repo: Path,
    path: str,
    config: Config,
    anchor_line: int,
    line_count: int = 16,
) -> Evidence:
    """Return tight hit context plus active Markdown heading breadcrumbs."""
    start = max(1, int(anchor_line) - 3)
    excerpt = read_excerpt(repo, path, config, start_line=start, line_count=line_count)

    suffix = Path(path).suffix.lower()
    if suffix not in {".md", ".mdx", ".markdown"}:
        return excerpt

    file = resolve_repo_file(repo.resolve(), path)
    active: dict[int, tuple[int, str]] = {}
    try:
        with file.open("r", encoding="utf-8", errors="replace") as fh:
            for number, line in enumerate(fh, 1):
                if number > anchor_line:
                    break
                match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line.rstrip())
                if not match:
                    continue
                level = len(match.group(1))
                active[level] = (number, line.rstrip())
                for deeper in [key for key in active if key > level]:
                    active.pop(deeper, None)
    except OSError:
        return excerpt

    if not active:
        return excerpt

    breadcrumbs = [
        f"{number}: {heading}"
        for _, (number, heading) in sorted(active.items())
    ]
    prefix = "Section context:\n" + "\n".join(breadcrumbs) + "\n"
    text = (prefix + excerpt.text)[: config.limits.max_file_chars]
    return Evidence(
        excerpt.id,
        excerpt.kind,
        excerpt.path,
        excerpt.start_line,
        excerpt.end_line,
        text,
    )


def git_history(repo: Path, config: Config, path: str | None = None, query: str | None = None) -> list[Evidence]:
    args = ["git", "log", f"-n{config.limits.max_history_entries}", "--date=iso-strict", "--pretty=format:%H%x09%ad%x09%an%x09%s"]
    if query:
        args.extend(["--grep", query, "--regexp-ignore-case"])
    if path:
        safe_path = _scope_arg(repo, path)
        args.extend(["--", safe_path])
    proc = _run(args, repo, config.limits.command_timeout_seconds)
    if proc.returncode != 0:
        return []
    out: list[Evidence] = []
    for idx, line in enumerate(proc.stdout.splitlines(), 1):
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        sha, date, author, subject = parts
        out.append(Evidence(f"g{idx}", "git", path or "", None, None, f"{sha} {date} {author}: {subject}"))
    return out


def _reindex(items: list[Evidence]) -> list[Evidence]:
    return [replace(item, id=f"e{index}") for index, item in enumerate(items, 1)]


def _whole_word_hit(term: str, text: str) -> bool:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,}", term):
        return False
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])"
    return re.search(pattern, text) is not None


def _separator_variants(search_terms: Sequence[str], limit: int = 2) -> list[str]:
    """Generate a tiny set of literal resource-name variants for compound identifiers.

    This helps a supplied identifier such as foo_bar locate a nearby literal
    resource/file token such as foo.bar without broadening into semantic search.
    """
    existing = {term.lower() for term in search_terms}
    out: list[str] = []
    for term in search_terms:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,}", term) or "_" not in term:
            continue
        for variant in (term.replace("_", "."), term.replace("_", "-")):
            low = variant.lower()
            if low in existing or variant in out:
                continue
            out.append(variant)
            if len(out) >= limit:
                return out
    return out


def _fallback_terms_for_missed_phrases(
    task: str,
    search_terms: Sequence[str],
    search_meta: dict,
    limit: int = 2,
) -> list[str]:
    """Recover a few literal atomic terms when exact phrases miss entirely.

    Supplied terms remain authoritative and are searched first. This only
    decomposes multiword phrases that produced zero exact hits, avoiding a
    broad fallback search for already-successful exact terms.
    """
    stop = {
        "the", "and", "for", "with", "from", "this", "that", "where", "what", "which",
        "find", "show", "please", "repository", "code", "is", "are", "was", "were",
        "be", "been", "being", "on", "in", "at", "to", "of", "by", "as", "it",
    }
    existing = {term.lower() for term in search_terms}
    coverage = search_meta.get("terms", {})
    candidates: list[str] = []

    missed_phrases = [
        term for term in search_terms
        if " " in term and int((coverage.get(term) or {}).get("hits") or 0) == 0
    ]
    for phrase in missed_phrases:
        # Only decompose clean natural-language phrases. Structured literals
        # such as 'status = "busy"' or code snippets should remain exact and
        # must not explode into broad fallback tokens.
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\s+[A-Za-z0-9_-]+)+", phrase):
            continue
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", phrase):
            if token.lower() in stop or token.lower() in existing:
                continue
            if token not in candidates:
                candidates.append(token)
            if len(candidates) >= limit:
                return candidates
    return candidates


def _ranked_literal_hits(
    repo: Path,
    search_terms: Sequence[str],
    config: Config,
    scope: str = ".",
) -> tuple[list[Evidence], dict]:
    """Search every supplied term, then rank files before applying result caps.

    Ranking favors files that cover more distinct terms, then files with more
    exact hits. A per-file cap prevents tests/docs or generated files from
    crowding every other source file out of the result.
    """
    by_term: dict[str, list[Evidence]] = {}
    by_path: dict[str, list[tuple[str, Evidence]]] = {}

    for term in search_terms:
        hits = literal_search(repo, term, config, scope=scope)
        # Prefer an actual token hit over incidental substring matches such as
        # "gated" inside "delegated"; retain substring matches as fallback.
        hits = sorted(
            hits,
            key=lambda hit: (
                0 if _whole_word_hit(term, hit.text) else 1,
                len(hit.text),
                hit.start_line or 0,
                hit.path,
            ),
        )
        by_term[term] = hits
        for hit in hits:
            by_path.setdefault(hit.path, []).append((term, hit))

    ranked_paths = sorted(
        by_path,
        key=lambda path: (
            -len({term for term, _ in by_path[path]}),
            -len({(hit.start_line, hit.text) for _, hit in by_path[path]}),
            len(path),
            path,
        ),
    )

    selected: list[Evidence] = []
    selected_keys: set[tuple[str, int | None, str]] = set()
    per_file_cap = 3

    for path in ranked_paths:
        candidates = by_path[path]
        chosen: list[tuple[str, Evidence]] = []
        covered_terms: set[str] = set()
        seen_hits: set[tuple[int | None, str]] = set()

        # First take one hit for as many distinct terms as possible.
        for term, hit in candidates:
            key = (hit.start_line, hit.text)
            if term in covered_terms or key in seen_hits:
                continue
            chosen.append((term, hit))
            covered_terms.add(term)
            seen_hits.add(key)
            if len(chosen) >= per_file_cap:
                break

        # Then fill any remaining slots with other unique hits.
        if len(chosen) < per_file_cap:
            for term, hit in candidates:
                key = (hit.start_line, hit.text)
                if key in seen_hits:
                    continue
                chosen.append((term, hit))
                seen_hits.add(key)
                if len(chosen) >= per_file_cap:
                    break

        for _, hit in chosen:
            key = (hit.path, hit.start_line, hit.text)
            if key in selected_keys:
                continue
            selected.append(hit)
            selected_keys.add(key)
            if len(selected) >= config.limits.max_search_matches:
                break
        if len(selected) >= config.limits.max_search_matches:
            break

    coverage_terms: dict[str, dict] = {}
    search_truncated = False
    for term in search_terms:
        raw_hits = by_term.get(term, [])
        raw_keys = {(hit.path, hit.start_line, hit.text) for hit in raw_hits}
        returned_keys = {
            (hit.path, hit.start_line, hit.text)
            for hit in selected
            if term in hit.text
        }
        if len(raw_hits) >= config.limits.max_search_matches:
            search_truncated = True
        all_returned = bool(raw_keys) and raw_keys.issubset(returned_keys) and len(raw_hits) < config.limits.max_search_matches
        coverage_terms[term] = {
            "hits": len(raw_keys),
            "returned": len(raw_keys & returned_keys),
            "all_returned": all_returned,
        }

    return selected, {
        "terms": coverage_terms,
        "truncated": search_truncated or any(
            info["returned"] < info["hits"] for info in coverage_terms.values()
        ),
    }


def build_repo_evidence(
    repo: Path,
    task: str,
    config: Config,
    scope: str = ".",
    terms: Sequence[str] | None = None,
    symbols: Sequence[str] | None = None,
) -> tuple[list[Evidence], bool, dict]:
    search_terms = effective_terms(task, terms=terms, symbols=symbols)
    search_hits, search_meta = _ranked_literal_hits(
        repo,
        search_terms,
        config,
        scope=scope,
    )
    fallback_terms = _fallback_terms_for_missed_phrases(task, search_terms, search_meta)
    separator_variants = _separator_variants(search_terms)
    candidate_extra_terms = [*fallback_terms, *separator_variants]

    # Probe the tiny variant set and retain only variants that actually match.
    matched_variants: list[str] = []
    for term in candidate_extra_terms:
        if literal_search(repo, term, config, scope=scope):
            matched_variants.append(term)

    if matched_variants:
        effective_search_terms = [*search_terms, *matched_variants]
        search_hits, search_meta = _ranked_literal_hits(
            repo,
            effective_search_terms,
            config,
            scope=scope,
        )
    else:
        effective_search_terms = list(search_terms)
    search_meta["fallback_terms"] = [term for term in matched_variants if term in fallback_terms]
    search_meta["separator_variants"] = [term for term in matched_variants if term in separator_variants]
    search_meta["effective_terms"] = effective_search_terms

    # Search hits are the highest-value evidence. Keep them first so exact
    # matches cannot be crowded out by path inventory or Git history.
    enriched: list[Evidence] = list(search_hits)

    # If a bounded fallback term was needed to recover a missed phrase, prefer
    # its nearby context for that file. The original exact hits remain in the
    # result; this only decides which one bounded excerpt accompanies them.
    def excerpt_priority(hit: Evidence) -> tuple[int, int, int]:
        recovery_terms = [
            *(search_meta.get("fallback_terms") or []),
            *(search_meta.get("separator_variants") or []),
        ]
        is_recovery = any(term in hit.text for term in recovery_terms)
        matching_terms = [
            term for term in effective_search_terms
            if term in hit.text
        ]
        specificity = max((len(term) for term in matching_terms), default=0)
        return (
            0 if is_recovery else 1,
            -specificity,
            hit.start_line or 0,
        )

    seen_paths: set[str] = set()
    for hit in sorted(search_hits, key=excerpt_priority):
        if hit.path in seen_paths or len(seen_paths) >= 6:
            continue
        seen_paths.add(hit.path)
        try:
            # Center context tightly around the selected hit. Markdown gets
            # active parent/local headings prepended as a compact breadcrumb,
            # so section identity survives result compaction.
            enriched.append(
                contextual_excerpt(
                    repo,
                    hit.path,
                    config,
                    anchor_line=(hit.start_line or 1),
                    line_count=16,
                )
            )
        except (OSError, RuntimeError):
            pass

    # These are useful primarily for explicit model synthesis. They come after
    # exact hits/excerpts so they cannot consume the deterministic answer slots.
    enriched.extend(path_inventory(repo, effective_search_terms, config)[:12])
    for term in effective_search_terms[:3]:
        enriched.extend(git_history(repo, config, query=term)[:5])

    total = 0
    bounded: list[Evidence] = []
    truncated = bool(search_meta.get("truncated"))
    for item in enriched:
        cost = len(item.text) + len(item.path) + 80
        if total + cost > config.limits.max_evidence_chars:
            truncated = True
            break
        bounded.append(item)
        total += cost
    return _reindex(bounded), truncated, search_meta


def evidence_json(items: list[Evidence]) -> str:
    return json.dumps([x.as_dict() for x in items], ensure_ascii=False, separators=(",", ":"))
