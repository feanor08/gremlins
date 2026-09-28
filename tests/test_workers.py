from pathlib import Path
import subprocess

import pytest

from gremlins.config import load_config
from gremlins.provider import ProviderBusy
import gremlins.workers as workers
from gremlins.retrieval import Evidence
from gremlins.workers import (
    _compact_evidence,
    _deterministic_repo_result,
    _extract_log_evidence,
    _validate_evidence_ids,
)


def test_log_extraction():
    lines = _extract_log_evidence("start\nERROR first failure\ndetail\nok\nFAILED test_x\n")
    joined = "\n".join(lines)
    assert "ERROR first failure" in joined
    assert "FAILED test_x" in joined


def test_invalid_evidence_ids():
    result = {"findings": [{"evidence_ids": ["s1", "made-up"]}]}
    assert _validate_evidence_ids(result, {"s1"}) == ["made-up"]


def test_compact_evidence_obeys_result_budget():
    evidence = [
        Evidence(f"e{i}", "search", f"file{i}.py", i, i, "x" * 2000)
        for i in range(1, 20)
    ]
    compact = _compact_evidence(evidence, 4000, ["e5"])
    assert compact
    assert compact[0]["id"] == "e5"
    assert len(str(compact)) < 5000
    assert len(compact) < len(evidence)


def test_busy_provider_is_not_reported_as_needs_caller(monkeypatch):
    def busy(*args, **kwargs):
        raise ProviderBusy("local inference is busy")

    monkeypatch.setattr(workers, "chat_json", busy)
    result = workers.triage("ERROR test failed", "triage this", load_config())
    assert result["status"] == "busy"
    assert result["usage"]["local_model_called"] is False


def test_deterministic_result_final_metadata_stays_within_hard_cap():
    import json

    evidence = []
    for file_index in range(1, 8):
        path = f"src/file_{file_index}.py"
        for hit_index in range(1, 4):
            line = file_index * 10 + hit_index
            evidence.append(
                Evidence(
                    f"s{file_index}-{hit_index}",
                    "search",
                    path,
                    line,
                    line,
                    ("match " + "x" * 380),
                )
            )
        evidence.append(
            Evidence(
                f"f{file_index}",
                "file",
                path,
                file_index * 10,
                file_index * 10 + 12,
                "context " + "y" * 900,
            )
        )

    search_meta = {
        "terms": {
            "alpha": {"files": 7, "hits": 21, "all_returned": False},
            "beta": {"files": 7, "hits": 21, "all_returned": False},
            "gamma": {"files": 7, "hits": 21, "all_returned": False},
        }
    }
    result = _deterministic_repo_result(
        evidence,
        ["alpha", "beta", "gamma"],
        search_meta,
        {"head": "abc123", "dirty": False},
        False,
        8000,
    )
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= 3000
    assert result["hits_ranked"] == 21


def test_deterministic_result_keeps_top_section_breadcrumb_under_budget():
    import json

    evidence = [
        Evidence("s1", "search", "docs/design.md", 40, 40, "Before major rollout expansion, the representative corpus should show:"),
        Evidence("s2", "search", "docs/design.md", 41, 41, "The throughput gate remains pending."),
        Evidence(
            "f1",
            "file",
            "docs/design.md",
            37,
            52,
            "Section context:\n3: ## Parent Phase — prove whether the system improves throughput\n"
            "7: ### Local rollout gate\n"
            "Before major rollout expansion, the representative corpus should show:\n"
            + ("context " * 120),
        ),
    ]
    for i in range(2, 8):
        evidence.append(
            Evidence(
                f"s{i+1}",
                "search",
                f"src/noise_{i}.py",
                i,
                i,
                "noise " + ("x" * 300),
            )
        )

    result = _deterministic_repo_result(
        evidence,
        ["rollout expansion", "throughput"],
        {"terms": {}},
        {"head": "abc123", "dirty": False},
        True,
        3000,
    )

    assert result["files"][0]["path"] == "docs/design.md"
    assert result["files"][0]["section"] == [
        "## Parent Phase — prove whether the system improves throughput",
        "### Local rollout gate",
    ]
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= 3000


def test_repo_explorer_can_return_deterministic_result_without_model(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "retry.py").write_text(
        "class RetryExhaustedError(Exception):\n    pass\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "retry.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add retry error"], cwd=repo, check=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("local model must not be called")

    monkeypatch.setattr(workers, "resolve_repository", lambda *args, **kwargs: repo.resolve())
    monkeypatch.setattr(workers, "chat_json", fail_if_called)

    result = workers.repo_explore(
        str(repo),
        "Find references to RetryExhaustedError",
        load_config(),
        terms=["RetryExhaustedError"],
        mode="auto",
    )
    assert result["status"] == "complete"
    assert result["usage"]["local_model_called"] is False
    assert result["hits_returned"] > 0
    assert result["files"][0]["path"] == "retry.py"
    assert len(__import__("json").dumps(result, separators=(",", ":"))) <= 3000
    assert result["coverage"]["terms"]["RetryExhaustedError"]["all_returned"] is True



def test_repo_explorer_auto_never_calls_model_for_how_task(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo-how"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "retrieval.py").write_text(
        "def effective_terms(terms):\n    return terms or ['fallback']\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "retrieval.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("mode=auto must never call the local model")

    monkeypatch.setattr(workers, "resolve_repository", lambda *args, **kwargs: repo.resolve())
    monkeypatch.setattr(workers, "chat_json", fail_if_called)

    result = workers.repo_explore(
        str(repo),
        "Find how effective_terms uses supplied terms",
        load_config(),
        terms=["effective_terms", "terms"],
        mode="auto",
    )
    assert result["usage"]["local_model_called"] is False
    assert result["usage"]["mode"] == "deterministic"
    assert result["files"]


def test_repo_explorer_model_mode_is_explicit(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo-model"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "x.py").write_text("needle = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "x.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

    monkeypatch.setattr(workers, "resolve_repository", lambda *args, **kwargs: repo.resolve())
    monkeypatch.setattr(
        workers,
        "chat_json",
        lambda *args, **kwargs: (
            {
                "summary": "synthesized",
                "findings": [],
                "limitations": [],
                "next_queries": [],
            },
            {"local_model_called": True},
        ),
    )

    result = workers.repo_explore(
        str(repo),
        "Explain needle",
        load_config(),
        terms=["needle"],
        mode="model",
    )
    assert result["usage"]["local_model_called"] is True


def test_model_unavailable_uses_caller_neutral_status_with_legacy_alias(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo-unavailable"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "x.py").write_text("needle = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "x.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

    monkeypatch.setattr(workers, "resolve_repository", lambda *args, **kwargs: repo.resolve())

    def unavailable(*args, **kwargs):
        raise workers.ProviderError("provider unavailable")

    monkeypatch.setattr(workers, "chat_json", unavailable)

    result = workers.repo_explore(
        str(repo),
        "Explain needle",
        load_config(),
        terms=["needle"],
        mode="model",
    )

    assert result["status"] == "needs-caller"
    assert result["legacy_status"] == "needs-frontier"
    assert result["usage"]["local_model_called"] is False
