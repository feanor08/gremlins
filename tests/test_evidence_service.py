from dataclasses import replace
from pathlib import Path
import json
import subprocess

from gremlins.config import load_config
from gremlins.evidence_service import evidence_pack


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
