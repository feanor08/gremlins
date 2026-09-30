from dataclasses import replace
from pathlib import Path
import json
import subprocess

from gremlins.config import load_config
from gremlins.evidence_service import _focused_hit_context_excerpt, evidence_pack
from gremlins.retrieval import Evidence


def _config_for(repo: Path):
    config = load_config()
    return replace(
        config,
        security=replace(
            config.security,
            allowed_roots=(repo.resolve(),),
            denied_paths=(),
        ),
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "provider.py").write_text(
        "def provider_state():\n    local_model_called = False\n    return local_model_called\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_provider.py").write_text(
        "from src.provider import provider_state\ndef test_provider_state():\n    assert provider_state() is False\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add provider state and tests"], cwd=repo, check=True)
    (repo / "src" / "provider.py").write_text(
        "def provider_state():\n    local_model_called = True\n    return local_model_called\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/provider.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "record local model calls"], cwd=repo, check=True)
    return repo


def test_evidence_pack_fuzzy_discovery_relationships_and_history(tmp_path: Path):
    repo = _repo(tmp_path)
    result = evidence_pack(
        str(repo),
        "Find where the system records whether a local model was called.",
        _config_for(repo),
        max_files=6,
    )
    assert result["status"] == "complete"
    assert result["usage"]["local_model_called"] is False
    assert result["usage"]["reasoning_performed"] is False
    paths = {item["path"] for item in result["files"]}
    assert "src/provider.py" in paths
    assert any(
        relation["source"] == "src/provider.py"
        and relation["test"] == "tests/test_provider.py"
        for relation in result["relationships"]
    )
    assert any(item["path"] == "src/provider.py" for item in result["history"]["by_path"])
    assert len(json.dumps(result, separators=(",", ":"))) <= _config_for(repo).limits.max_result_evidence_chars


def test_evidence_pack_focus_path_is_preserved(tmp_path: Path):
    repo = _repo(tmp_path)
    result = evidence_pack(
        str(repo),
        "Show evidence around provider state.",
        _config_for(repo),
        paths=["tests/test_provider.py"],
        include_history=False,
        max_files=2,
    )
    assert result["request"]["focus_paths"] == ["tests/test_provider.py"]
    assert any(item["path"] == "tests/test_provider.py" for item in result["files"])


def test_evidence_pack_explicit_terms_do_not_broaden_fuzzy_variants(tmp_path: Path):
    repo = _repo(tmp_path)
    result = evidence_pack(
        str(repo),
        "Find provider information.",
        _config_for(repo),
        terms=["local_model_called"],
    )
    assert result["discovery"]["terms"][0] == "local_model_called"
    assert result["discovery"]["matched_fuzzy_variants"] == []


def test_evidence_pack_is_repeatable_and_model_free(tmp_path: Path):
    repo = _repo(tmp_path)
    first = evidence_pack(str(repo), "Find provider state.", _config_for(repo))
    second = evidence_pack(str(repo), "Find provider state.", _config_for(repo))
    assert first["usage"]["repeatable"] is True
    assert second["usage"]["repeatable"] is True
    assert first["usage"]["local_model_called"] is False
    assert second["usage"]["local_model_called"] is False
    assert [item["path"] for item in first["files"]] == [item["path"] for item in second["files"]]


def test_evidence_pack_focused_mode_is_compact(tmp_path: Path):
    repo = _repo(tmp_path)
    result = evidence_pack(
        str(repo),
        "Show the provider state implementation and its test.",
        _config_for(repo),
        detail="focused",
        paths=["src/provider.py"],
        terms=["local_model_called"],
        max_files=8,
    )
    encoded = json.dumps(result, separators=(",", ":"))
    assert result["request"]["detail"] == "focused"
    assert result["request"]["result_budget_chars"] == 3600
    assert result["request"]["max_files"] <= 4
    assert len(result["related_paths"]) <= 12
    assert len(result["relationships"]) <= 4
    assert len(encoded) <= 3600


def test_evidence_pack_broad_mode_keeps_full_budget(tmp_path: Path):
    repo = _repo(tmp_path)
    result = evidence_pack(
        str(repo),
        "Find provider state and related evidence.",
        _config_for(repo),
        detail="broad",
    )
    assert result["request"]["detail"] == "broad"
    assert result["request"]["result_budget_chars"] == _config_for(repo).limits.max_result_evidence_chars


def test_evidence_pack_focused_mode_requires_focus_input(tmp_path: Path):
    repo = _repo(tmp_path)
    try:
        evidence_pack(
            str(repo),
            "Find provider state.",
            _config_for(repo),
            detail="focused",
        )
    except ValueError as exc:
        assert "requires at least one path, term, or symbol" in str(exc)
    else:
        raise AssertionError("focused detail without a focus input must fail")


def test_evidence_pack_auto_focuses_exact_inputs(tmp_path: Path):
    repo = _repo(tmp_path)
    result = evidence_pack(
        str(repo),
        "Find provider information.",
        _config_for(repo),
        terms=["local_model_called"],
    )
    assert result["request"]["detail"] == "focused"
    assert result["request"]["result_budget_chars"] == 3600


def test_focused_python_pack_prefers_relevant_function_definition(tmp_path: Path):
    repo = _repo(tmp_path)
    retrieval = repo / "src" / "retrieval.py"
    retrieval.write_text(
        "\n".join([
            "def candidate_terms(task):",
            "    return ['fallback']",
            "",
            "def effective_terms(task: str, terms: list[str] | None = None, symbols: list[str] | None = None):",
            "    supplied = [*(symbols or []), *(terms or [])]",
            "    return supplied if supplied else candidate_terms(task)",
            "",
            "def build_repo_evidence(task: str, terms: list[str] | None = None, symbols: list[str] | None = None):",
            "    search_terms = effective_terms(task, terms=terms, symbols=symbols)",
            "    fallback_terms = ['fallback']",
            "    effective_search_terms = list(search_terms)",
            "    def excerpt_priority(hit):",
            "        return any(term in hit for term in fallback_terms)",
            "    return effective_search_terms",
        ]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/retrieval.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add focused retrieval fixture"], cwd=repo, check=True)

    result = evidence_pack(
        str(repo),
        (
            "Find the function definition that chooses between orchestrator-supplied "
            "terms/symbols parameters and fallback keyword extraction to build "
            "effective_search_terms."
        ),
        _config_for(repo),
        detail="focused",
        paths=["src/retrieval.py"],
        terms=["effective_search_terms", "def ", "terms: list", "symbols: list"],
        include_history=False,
    )

    entry = next(item for item in result["files"] if item["path"] == "src/retrieval.py")
    assert "focused symbol definition" in entry["reasons"]
    assert entry["excerpt"] is not None
    assert "def effective_terms" in entry["excerpt"]["text"]
    assert len(json.dumps(result, separators=(",", ":"))) <= 3600


def test_focused_handler_request_returns_multiple_exact_hit_regions(tmp_path: Path):
    repo = _repo(tmp_path)
    workers = repo / "src" / "workers.py"
    workers.write_text(
        "\n".join([
            "def repo_explore():",
            "    try:",
            "        run_provider()",
            "    except ProviderBusy as exc:",
            "        result = {'error': str(exc)}",
            "        status = \"busy\"",
            "    return status",
            *[f"padding_{index} = {index}" for index in range(30)],
            "def triage():",
            "    try:",
            "        run_provider()",
            "    except ProviderBusy as exc:",
            "        result = {'error': str(exc)}",
            "        status = \"busy\"",
            "    return status",
        ]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/workers.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add busy handlers"], cwd=repo, check=True)

    result = evidence_pack(
        str(repo),
        "Exact final verification: show context around both ProviderBusy handlers and status assignments.",
        _config_for(repo),
        detail="focused",
        paths=["src/workers.py"],
        terms=["except ProviderBusy as exc:", 'status = "busy"'],
        symbols=["ProviderBusy"],
        include_history=False,
        include_tests=False,
        max_files=1,
    )

    entry = next(item for item in result["files"] if item["path"] == "src/workers.py")
    excerpt = entry["excerpt"]["text"]
    assert "focused hit context" in entry["reasons"]
    assert "def repo_explore" in excerpt
    assert "def triage" in excerpt
    assert excerpt.count("except ProviderBusy as exc:") == 2
    assert excerpt.count('status = "busy"') == 2
    assert len(json.dumps(result, separators=(",", ":"))) <= 3600


def test_exact_verification_prefers_enclosing_function_around_literal_hit(tmp_path: Path):
    repo = _repo(tmp_path)
    service = repo / "src" / "evidence_service.py"
    service.write_text(
        "\n".join([
            "from retrieval import effective_terms",
            "",
            "def _discovery_terms(task, terms=None, symbols=None):",
            "    explicit = bool(terms or symbols)",
            "    base = effective_terms(task, terms=terms, symbols=symbols)",
            "    if explicit:",
            "        return base, []",
            "    fallback = [word for word in task.split() if len(word) > 3]",
            "    return base, fallback",
            "",
            *[f"padding_{index} = {index}" for index in range(30)],
            "",
            "def evidence_pack(repository, task, terms=None, symbols=None):",
            "    search_terms, fallback_terms = _discovery_terms(task, terms, symbols)",
            "    return search_terms + fallback_terms",
        ]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/evidence_service.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add discovery-term fixture"], cwd=repo, check=True)

    result = evidence_pack(
        str(repo),
        (
            "Exact final verification: show the evidence_pack selection implementation "
            "and the expressions terms or, symbols or, and fallback extraction."
        ),
        _config_for(repo),
        detail="focused",
        paths=["src/evidence_service.py"],
        terms=["terms or", "symbols or", "fallback extraction"],
        symbols=["evidence_pack"],
        include_history=False,
        include_tests=False,
        max_files=1,
    )

    entry = next(item for item in result["files"] if item["path"] == "src/evidence_service.py")
    excerpt = (entry.get("excerpt") or {}).get("text") or ""
    assert "focused hit context" in entry["reasons"]
    assert "def _discovery_terms" in excerpt
    assert "explicit = bool(terms or symbols)" in excerpt
    assert "base = effective_terms" in excerpt
    assert "if explicit:" in excerpt
    assert len(json.dumps(result, separators=(",", ":"))) <= 3600


def test_focused_compaction_never_evicts_caller_path(tmp_path: Path):
    repo = _repo(tmp_path)

    # Create several higher-scoring competitors so the caller-focused path is
    # last by raw evidence score. The tight budget then exercises compaction.
    for name in ("alpha", "beta", "gamma"):
        (repo / "src" / f"{name}.py").write_text(
            "\n".join([
                "effective_search_terms = ['x']",
                "effective_search_terms = ['y']",
                "effective_search_terms = ['z']",
            ]) + "\n",
            encoding="utf-8",
        )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add competing evidence"], cwd=repo, check=True)

    config = _config_for(repo)
    config = replace(
        config,
        limits=replace(config.limits, max_result_evidence_chars=1400),
    )

    result = evidence_pack(
        str(repo),
        "Find the exact caller-focused test path while competing search evidence exists.",
        config,
        detail="focused",
        paths=["tests/test_provider.py"],
        terms=["effective_search_terms"],
        include_history=False,
        max_files=4,
    )

    assert result["truncated"] is True
    assert result["files"]
    assert result["files"][0]["path"] == "tests/test_provider.py"
    assert "caller-focused path" in result["files"][0]["reasons"]
    assert any(item["path"] == "tests/test_provider.py" for item in result["files"])
    assert len(json.dumps(result, separators=(",", ":"))) <= 1400


def test_focused_compaction_preserves_exact_hit_context_over_no_hit_focus_excerpts(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    for name in ("alpha", "beta", "gamma"):
        (repo / "src" / f"{name}.py").write_text(
            "\n".join([
                f"def {name}_operation():",
                *[f"    value_{index} = {index}" for index in range(40)],
                "    return True",
            ]) + "\n",
            encoding="utf-8",
        )
    (repo / "tests" / "test_budget_terms.py").write_text(
        "\n".join([
            "def test_budget_terms():",
            '    task = "Find the frontier-facing evidence size budget."',
            '    terms = ["max_result_evidence_chars"]',
            "    assert terms",
        ]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add focused compaction fixture"], cwd=repo, check=True)

    result = evidence_pack(
        str(repo),
        (
            "Exact verification: show the definition and value associated with "
            "identifier size_budget and all literal frontier-facing occurrences."
        ),
        _config_for(repo),
        detail="focused",
        paths=["src/alpha.py", "src/beta.py", "src/gamma.py"],
        symbols=["size_budget"],
        terms=["size_budget", "frontier-facing"],
        include_history=False,
        include_tests=False,
        max_files=6,
    )

    by_path = {item["path"]: item for item in result["files"]}
    assert all(path in by_path for path in ("src/alpha.py", "src/beta.py", "src/gamma.py"))
    exact_hit = by_path["tests/test_budget_terms.py"]
    assert 'terms = ["max_result_evidence_chars"]' in exact_hit["excerpt"]["text"]
    assert len(json.dumps(result, separators=(",", ":"))) <= 3600


def test_exact_verification_keeps_preceding_setup_when_only_later_hit_is_returned(tmp_path: Path):
    repo = _repo(tmp_path)
    path = "tests/test_self_poison.py"
    lines = [
        "def test_budget_mapping():",
        '    task = "Find the frontier-facing evidence size budget."',
        '    terms = ["max_result_evidence_chars"]',
        "    assert terms",
        *[f"    padding_{index} = {index}" for index in range(10)],
        '    verification = "identifier size_budget and all literal frontier-facing occurrences"',
        "    assert verification",
    ]
    (repo / path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add self-poison fixture"], cwd=repo, check=True)

    later_line = lines.index(
        '    verification = "identifier size_budget and all literal frontier-facing occurrences"'
    ) + 1
    hit = Evidence(
        id="search:self-poison",
        kind="search",
        path=path,
        start_line=later_line,
        end_line=later_line,
        text=lines[later_line - 1],
    )

    excerpt = _focused_hit_context_excerpt(
        repo,
        path,
        [hit],
        (
            "Exact verification: show the definition and value associated with "
            "identifier size_budget and all literal frontier-facing occurrences."
        ),
        ["size_budget", "frontier-facing"],
        _config_for(repo),
    )

    assert excerpt is not None
    assert 'task = "Find the frontier-facing evidence size budget."' in excerpt.text
    assert 'terms = ["max_result_evidence_chars"]' in excerpt.text
    assert "verification =" in excerpt.text


def test_focused_python_definition_prefers_exact_parameter_hints_over_fallback_helper(tmp_path: Path):
    repo = _repo(tmp_path)
    retrieval = repo / "src" / "retrieval.py"
    retrieval.write_text(
        "\n".join([
            "def candidate_terms(task):",
            "    return ['fallback']",
            "",
            "def effective_terms(task: str, terms: list[str] | None = None, symbols: list[str] | None = None):",
            "    supplied = []",
            "    for value in [*(symbols or []), *(terms or [])]:",
            "        if value:",
            "            supplied.append(value)",
            "    return supplied if supplied else candidate_terms(task)",
            "",
            "def _fallback_terms_for_missed_phrases(task: str, search_terms, search_meta, limit: int = 2):",
            "    \"\"\"Recover fallback keyword terms when exact search terms miss.",
            "",
            "    Supplied terms remain authoritative and are searched first.",
            "    \"\"\"",
            "    fallback_terms = []",
            "    for term in search_terms:",
            "        if search_meta.get(term) == 0:",
            "            fallback_terms.append(term)",
            "    return fallback_terms[:limit]",
            "",
            "def build_repo_evidence(task: str, terms=None, symbols=None):",
            "    search_terms = effective_terms(task, terms=terms, symbols=symbols)",
            "    fallback_terms = _fallback_terms_for_missed_phrases(task, search_terms, {})",
            "    effective_search_terms = [*search_terms, *fallback_terms]",
            "    return effective_search_terms",
        ]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/retrieval.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add competing fallback helper"], cwd=repo, check=True)

    result = evidence_pack(
        str(repo),
        (
            "Find the function definition that chooses between orchestrator-supplied "
            "terms/symbols parameters and fallback keyword extraction to build "
            "effective_search_terms."
        ),
        _config_for(repo),
        detail="focused",
        paths=["src/retrieval.py"],
        terms=["effective_search_terms", "def ", "terms: list", "symbols: list"],
        include_history=False,
    )

    entry = next(item for item in result["files"] if item["path"] == "src/retrieval.py")
    excerpt = (entry.get("excerpt") or {}).get("text") or ""
    assert "focused symbol definition" in entry["reasons"]
    assert "def effective_terms" in excerpt
    assert "def _fallback_terms_for_missed_phrases" not in excerpt
    assert len(json.dumps(result, separators=(",", ":"))) <= 3600


def test_focused_definition_body_overlap_ignores_docstrings_and_string_literals(tmp_path: Path):
    repo = _repo(tmp_path)
    retrieval = repo / "src" / "retrieval.py"
    retrieval.write_text(
        "\n".join([
            "def candidate_terms(task):",
            "    return ['fallback']",
            "",
            "def effective_terms(task: str, terms=None, symbols=None):",
            "    supplied = [*(symbols or []), *(terms or [])]",
            "    return supplied if supplied else candidate_terms(task)",
            "",
            "def _fallback_terms_for_missed_phrases(task: str, search_terms, search_meta, limit=2):",
            "    \"\"\"Supplied terms remain authoritative; fallback search goes to effective terms.\"\"\"",
            "    noise = 'to supplied fallback search effective terms symbols'",
            "    fallback_terms = []",
            "    for term in search_terms:",
            "        if search_meta.get(term) == 0:",
            "            fallback_terms.append(term)",
            "    return fallback_terms[:limit]",
            "",
            "def build_repo_evidence(task: str, terms=None, symbols=None):",
            "    search_terms = effective_terms(task, terms=terms, symbols=symbols)",
            "    fallback_terms = _fallback_terms_for_missed_phrases(task, search_terms, {})",
            "    effective_search_terms = [*search_terms, *fallback_terms]",
            "    return effective_search_terms",
        ]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/retrieval.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add prose-heavy fallback decoy"], cwd=repo, check=True)

    result = evidence_pack(
        str(repo),
        (
            "Find the function definition that chooses between orchestrator-supplied "
            "terms/symbols parameters and fallback keyword extraction to build "
            "effective_search_terms."
        ),
        _config_for(repo),
        detail="focused",
        paths=["src/retrieval.py"],
        # Deliberately omit signature-like parameter hints so this regression
        # isolates AST code-body scoring from prose/string-literal noise.
        terms=["effective_search_terms", "def "],
        include_history=False,
    )

    entry = next(item for item in result["files"] if item["path"] == "src/retrieval.py")
    excerpt = (entry.get("excerpt") or {}).get("text") or ""
    assert "def effective_terms" in excerpt
    assert "def _fallback_terms_for_missed_phrases" not in excerpt


def test_focused_definition_exact_param_hints_beat_loose_param_tokens(tmp_path: Path):
    repo = _repo(tmp_path)
    retrieval = repo / "src" / "retrieval.py"
    retrieval.write_text(
        "\n".join([
            "def effective_terms(task: str, terms=None, symbols=None):",
            "    supplied = [*(symbols or []), *(terms or [])]",
            "    return supplied",
            "",
            "def fallback_terms(task: str, search_terms=None, search_symbols=None):",
            "    return [*(search_symbols or []), *(search_terms or [])]",
        ]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/retrieval.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add exact versus loose params"], cwd=repo, check=True)

    result = evidence_pack(
        str(repo),
        "Find the function implementation using the supplied terms/symbols parameters.",
        _config_for(repo),
        detail="focused",
        paths=["src/retrieval.py"],
        terms=["terms: list", "symbols: list"],
        include_history=False,
    )

    entry = next(item for item in result["files"] if item["path"] == "src/retrieval.py")
    excerpt = (entry.get("excerpt") or {}).get("text") or ""
    assert "def effective_terms" in excerpt
    assert "def fallback_terms" not in excerpt
