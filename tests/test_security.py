from pathlib import Path
import subprocess
import pytest

from gremlins.config import Config, Limits, ProviderConfig, SecurityConfig
from gremlins.security import PolicyError, resolve_repository, resolve_repo_file


def cfg(root: Path) -> Config:
    return Config(
        root=root,
        provider=ProviderConfig("ollama", "http://127.0.0.1:11434", "x"),
        limits=Limits(4000,48000,8000,16000,80,24,1200,16384,90,12),
        security=SecurityConfig((root,), (root / "secret",), True, ("127.0.0.1",)),
    )


def test_repository_policy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert resolve_repository(repo, cfg(tmp_path)) == repo.resolve()


def test_denied_repository(tmp_path):
    denied = tmp_path / "secret"
    denied.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=denied, check=True)
    with pytest.raises(PolicyError):
        resolve_repository(denied, cfg(tmp_path))


def test_file_escape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.txt").write_text("ok")
    with pytest.raises(PolicyError):
        resolve_repo_file(repo, "../outside")


def test_search_scope_escape(tmp_path):
    from gremlins.retrieval import literal_search
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    with pytest.raises(PolicyError):
        literal_search(repo, "x", cfg(tmp_path), scope="..")


def test_git_history_path_escape(tmp_path):
    from gremlins.retrieval import git_history
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    with pytest.raises(PolicyError):
        git_history(repo, cfg(tmp_path), path="../outside")
