from pathlib import Path
import subprocess
import pytest

import gremlins.config as config_module
from gremlins.config import Config, Limits, ProviderConfig, SecurityConfig, load_config
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


def test_project_root_is_implicitly_allowed_outside_home(tmp_path, monkeypatch):
    root = tmp_path / "external-volume" / "gremlins"
    root.mkdir(parents=True)
    (root / "profiles").mkdir()
    (root / "gremlins.toml").write_text(
        """[provider]
kind = "ollama"
url = "http://127.0.0.1:11434"
model = "x"

[limits]
max_task_chars = 4000
max_evidence_chars = 48000
max_result_evidence_chars = 8000
max_file_chars = 16000
max_search_matches = 80
max_history_entries = 24
max_model_output_tokens = 1200
model_context_tokens = 16384
model_timeout_seconds = 90
command_timeout_seconds = 12

[security]
allowed_roots = ["~/only-home"]
denied_paths = []
require_git_repository = true
allow_network_to = ["127.0.0.1"]
""",
        encoding="utf-8",
    )
    (root / "profiles" / "mac-local.toml").write_text(
        'allowed_roots_extra = []\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    monkeypatch.setenv("GREMLINS_ROOT", str(root))
    config = load_config()

    assert root.resolve() in config.security.allowed_roots
    assert resolve_repository(root, config) == root.resolve()


def test_tilde_allowed_root_uses_account_home_not_process_home(tmp_path, monkeypatch):
    account_home = tmp_path / "account-home"
    isolated_home = tmp_path / "isolated-home"
    root = tmp_path / "gremlins-root"
    workspace = account_home / ".local" / "state" / "gremlins" / "benchmarks" / "workspace"

    account_home.mkdir()
    isolated_home.mkdir()
    root.mkdir()
    workspace.mkdir(parents=True)
    (root / "profiles").mkdir()

    (root / "gremlins.toml").write_text(
        """[provider]
kind = "ollama"
url = "http://127.0.0.1:11434"
model = "x"

[limits]
max_task_chars = 4000
max_evidence_chars = 48000
max_result_evidence_chars = 8000
max_file_chars = 16000
max_search_matches = 80
max_history_entries = 24
max_model_output_tokens = 1200
model_context_tokens = 16384
model_timeout_seconds = 90
command_timeout_seconds = 12

[security]
allowed_roots = ["~"]
denied_paths = []
require_git_repository = true
allow_network_to = ["127.0.0.1"]
""",
        encoding="utf-8",
    )
    (root / "profiles" / "mac-local.toml").write_text(
        "allowed_roots_extra = []\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)

    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("GREMLINS_ROOT", str(root))
    monkeypatch.setattr(config_module, "_account_home", lambda: account_home.resolve())

    config = load_config()

    assert account_home.resolve() in config.security.allowed_roots
    assert isolated_home.resolve() not in config.security.allowed_roots
    assert resolve_repository(workspace, config) == workspace.resolve()
