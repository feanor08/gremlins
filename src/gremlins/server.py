from __future__ import annotations

from mcp.server import MCPServer

from .config import load_config
from .provider import ProviderError, health
from .retrieval import git_history as _git_history, literal_search, read_excerpt, snapshot
from .evidence_service import evidence_pack as _evidence_pack
from .security import PolicyError, resolve_repository, resolve_repo_file
from .workers import repo_explore, triage


mcp = MCPServer(
    "Gremlins",
    instructions=(
        "Use Gremlins as a cheap read-only evidence engine for repository exploration, exact search, bounded reads, Git history, test/source relationships, and failure triage. "
        "Prefer evidence_pack when the caller would otherwise perform several search/read/history hops, including during root-cause analysis. "
        "Gremlins never modifies repositories and its local workers cannot spawn child workers."
    ),
)


@mcp.tool()
def gremlins_status() -> dict:
    """Check Gremlins runtime status and optional local-model availability."""
    config = load_config()
    try:
        provider = health(config)
    except ProviderError as exc:
        provider = {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "provider": provider,
        "model": config.provider.model,
        "limits": config.limits.__dict__,
        "bindings": config.raw.get("bindings", {}),
    }


@mcp.tool()
def repo_search(query: str, repository: str = ".", scope: str = ".") -> dict:
    """Exact read-only repository search. Use this before semantic reasoning when locating files/symbols/text."""
    config = load_config()
    repo = resolve_repository(repository, config)
    items = literal_search(repo, query, config, scope=scope)
    return {"snapshot": snapshot(repo, config), "matches": [x.as_dict() for x in items]}


@mcp.tool()
def code_read(path: str, repository: str = ".", start_line: int = 1, line_count: int = 120) -> dict:
    """Read a bounded source excerpt from a file inside a Git repository. Read-only and path-confined."""
    config = load_config()
    repo = resolve_repository(repository, config)
    return {"snapshot": snapshot(repo, config), "evidence": read_excerpt(repo, path, config, start_line, line_count).as_dict()}


@mcp.tool()
def git_history(repository: str = ".", path: str | None = None, query: str | None = None) -> dict:
    """Inspect bounded Git commit history without allowing arbitrary Git command execution."""
    config = load_config()
    repo = resolve_repository(repository, config)
    items = _git_history(repo, config, path=path, query=query)
    return {"snapshot": snapshot(repo, config), "history": [x.as_dict() for x in items]}


@mcp.tool()
def evidence_pack(
    task: str,
    repository: str = ".",
    scope: str = ".",
    terms: list[str] | None = None,
    symbols: list[str] | None = None,
    paths: list[str] | None = None,
    include_tests: bool = True,
    include_history: bool = True,
    max_files: int = 6,
    measurement_tag: str | None = None,
) -> dict:
    """Deterministic evidence bundle for multi-hop investigation. Call repeatedly during RCA; Gremlins gathers evidence while the caller keeps causal reasoning and judgment."""
    return _evidence_pack(
        repository,
        task,
        load_config(),
        scope=scope,
        terms=terms,
        symbols=symbols,
        paths=paths,
        include_tests=include_tests,
        include_history=include_history,
        max_files=max_files,
        measurement_tag=measurement_tag,
    )


@mcp.tool()
def repo_explorer(
    task: str,
    repository: str = ".",
    scope: str = ".",
    terms: list[str] | None = None,
    symbols: list[str] | None = None,
    mode: str = "auto",
    measurement_tag: str | None = None,
) -> dict:
    """Bounded read-only repo worker. Supply terms/symbols when the caller knows exact targets. mode=auto is deterministic and never calls a model; mode=model explicitly requests local synthesis. Deterministic results are ranked exact hits with compact hit-centered context. When coverage says all_returned=true for a term, Gremlins returned every exact match it found for that term, so do not repeat the same broad search unless more context is genuinely needed."""
    return repo_explore(
        repository,
        task,
        load_config(),
        scope=scope,
        terms=terms,
        symbols=symbols,
        mode=mode,
        measurement_tag=measurement_tag,
    )


@mcp.tool()
def failure_triage(
    task: str = "Group the failures, identify likely causes, and give the next checks.",
    text: str | None = None,
    path: str | None = None,
    repository: str = ".",
    measurement_tag: str | None = None,
) -> dict:
    """Bounded local log/test triage. Prefer path=... so the caller sends a filename, not the full log. Read-only."""
    config = load_config()
    if path:
        repo = resolve_repository(repository, config)
        file = resolve_repo_file(repo, path)
        # Bound file ingestion before the worker performs its own evidence extraction.
        with file.open("r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(config.limits.max_evidence_chars * 4 + 1)
        if len(content) > config.limits.max_evidence_chars * 4:
            content = content[-config.limits.max_evidence_chars * 4 :]
    elif text is not None:
        content = text
    else:
        raise PolicyError("provide either path or text")
    return triage(content, task, config, measurement_tag=measurement_tag)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
