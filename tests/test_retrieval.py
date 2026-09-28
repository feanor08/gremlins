from pathlib import Path
import subprocess

from gremlins.config import load_config
from gremlins.retrieval import (
    build_repo_evidence,
    candidate_terms,
    contextual_excerpt,
    effective_terms,
    literal_search,
    read_excerpt,
    git_history,
)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "app.py").write_text("def run():\n    raise RuntimeError('retry exhausted')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "handle retry exhaustion"], cwd=repo, check=True)
    return repo


def test_literal_search_and_read(tmp_path):
    repo = make_repo(tmp_path)
    cfg = load_config()
    hits = literal_search(repo, "retry exhausted", cfg)
    assert hits and hits[0].path == "app.py"
    excerpt = read_excerpt(repo, "app.py", cfg)
    assert "RuntimeError" in excerpt.text


def test_history(tmp_path):
    repo = make_repo(tmp_path)
    cfg = load_config()
    history = git_history(repo, cfg, query="retry")
    assert history
    assert "handle retry exhaustion" in history[0].text


def test_candidate_terms_prefers_identifiers():
    terms = candidate_terms("Find where `RetryExhaustedError` is converted in transport_retry.py")
    assert "RetryExhaustedError" in terms


def test_explicit_terms_override_keyword_guessing():
    terms = effective_terms(
        "Find where retry exhaustion is classified and identify relevant tests",
        terms=["RetryExhaustedError", "retry_exhausted"],
        symbols=["classify_retry"],
    )
    assert terms == ["classify_retry", "RetryExhaustedError", "retry_exhausted"]
    assert "relevant" not in terms



def test_literal_search_includes_hidden_tracked_paths(tmp_path):
    repo = make_repo(tmp_path)
    workflow = repo / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text("run: uv sync --locked\n", encoding="utf-8")
    subprocess.run(["git", "add", ".github/workflows/ci.yml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add ci"], cwd=repo, check=True)

    hits = literal_search(repo, "uv sync --locked", load_config())
    assert any(hit.path == ".github/workflows/ci.yml" for hit in hits)


def test_repo_evidence_ranks_multi_term_files_and_centers_excerpt(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "core.py").write_text(
        "\n".join([
            "header = 1",
            "needle_one = True",
            "context = 2",
            "ANSWER_LINE = 'keep me visible'",
            "needle_two = True",
            "tail = 3",
        ]) + "\n",
        encoding="utf-8",
    )
    (repo / "tests" / "noise.py").write_text(
        "\n".join([f"needle_one = {i}" for i in range(20)]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/core.py", "tests/noise.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add search fixture"], cwd=repo, check=True)

    evidence, truncated, meta = build_repo_evidence(
        repo,
        "Find both needles",
        load_config(),
        terms=["needle_one", "needle_two"],
    )
    search_hits = [item for item in evidence if item.kind == "search"]
    assert search_hits
    assert search_hits[0].path == "src/core.py"
    assert {item.path for item in search_hits[:2]} == {"src/core.py"}
    centered = [item for item in evidence if item.kind == "file" and item.path == "src/core.py"]
    assert centered
    assert "ANSWER_LINE" in centered[0].text
    assert meta["terms"]["needle_two"]["hits"] == 1


def test_missed_phrase_adds_bounded_atomic_fallback_and_prefers_word_hits(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "architecture.md").write_text(
        "\n".join([
            "A delegated helper can search the repository.",
            "The product remains gated until evidence is collected.",
            "## Phase Delta — prove whether the system improves throughput",
            "The throughput harness is implemented and the rollout gate is pending.",
            "Before major rollout expansion, the representative corpus should show:",
        ]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "architecture.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add measurement gate fixture"], cwd=repo, check=True)

    evidence, truncated, meta = build_repo_evidence(
        repo,
        "Find the statement that rollout expansion is gated on throughput.",
        load_config(),
        terms=["rollout expansion", "gated on throughput", "gated"],
    )

    assert "throughput" in meta["fallback_terms"]
    assert meta["terms"]["throughput"]["hits"] >= 1

    architecture_evidence = [item for item in evidence if item.path == "architecture.md"]
    assert any("Phase Delta" in item.text for item in architecture_evidence)

    search_hits = [item for item in evidence if item.kind == "search" and item.path == "architecture.md"]
    gated_hits = [item for item in search_hits if "gated" in item.text.lower()]
    assert gated_hits
    assert "delegated" not in gated_hits[0].text.lower()


def test_structured_or_single_exact_terms_do_not_expand_fallbacks(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "worker.py").write_text(
        'status = "busy"\nmax_result_evidence_chars = 3000\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "worker.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add exact fixture"], cwd=repo, check=True)

    _, _, structured_meta = build_repo_evidence(
        repo,
        "Find where status becomes busy",
        load_config(),
        terms=['status = "missing"'],
    )
    assert structured_meta["fallback_terms"] == []

    _, _, single_meta = build_repo_evidence(
        repo,
        "Find the frontier-facing evidence size budget.",
        load_config(),
        terms=["max_result_evidence_chars"],
    )
    assert single_meta["fallback_terms"] == []


def test_compound_identifier_can_recover_matching_resource_literal(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "provider.py").write_text(
        'path = state / "locks" / "inference.lock"\n'
        'with global_inference_slot(config.provider.inference_lock_timeout_seconds):\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "provider.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add lock fixture"], cwd=repo, check=True)

    evidence, _, meta = build_repo_evidence(
        repo,
        "Find the local inference lock and its configured timeout.",
        load_config(),
        terms=["inference_lock", "inference_lock_timeout_seconds"],
    )

    assert "inference.lock" in meta["separator_variants"]
    assert any("inference.lock" in item.text for item in evidence if item.path == "provider.py")


def test_markdown_context_excerpt_includes_parent_and_local_headings(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "architecture.md").write_text(
        "\n".join([
            "# Architecture",
            "",
            "## Parent Phase — prove whether the system improves throughput",
            "",
            "Some setup.",
            "",
            "### Local rollout gate",
            "",
            "Before major rollout expansion, the representative corpus should show:",
        ]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "architecture.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add nested markdown headings"], cwd=repo, check=True)

    excerpt = contextual_excerpt(
        repo,
        "architecture.md",
        load_config(),
        anchor_line=9,
        line_count=8,
    )

    assert "## Parent Phase" in excerpt.text
    assert "### Local rollout gate" in excerpt.text
    assert "Before major rollout expansion" in excerpt.text


def test_specific_exact_phrase_drives_markdown_excerpt_anchor(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "architecture.md").write_text(
        "\n".join([
            "# Architecture",
            "throughput appears in unrelated introduction",
            "",
            "## Parent Phase — prove whether the system improves throughput",
            "",
            "### Local rollout gate",
            "",
            "Before major rollout expansion, the representative corpus should show:",
        ]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "architecture.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add gate fixture"], cwd=repo, check=True)

    evidence, _, _ = build_repo_evidence(
        repo,
        "Find the statement that rollout expansion is gated on throughput.",
        load_config(),
        terms=["rollout expansion", "throughput"],
    )

    excerpts = [
        item for item in evidence
        if item.kind == "file" and item.path == "architecture.md"
    ]
    assert excerpts
    assert "## Parent Phase" in excerpts[0].text
    assert "Before major rollout expansion" in excerpts[0].text
