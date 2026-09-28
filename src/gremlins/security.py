from __future__ import annotations

from pathlib import Path
import os
import subprocess

from .config import Config


class PolicyError(RuntimeError):
    pass


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_repository(value: str | Path, config: Config) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_dir():
        raise PolicyError(f"repository is not a directory: {path}")

    if not any(_is_within(path, root) or path == root for root in config.security.allowed_roots):
        raise PolicyError(f"repository is outside allowed roots: {path}")
    if any(_is_within(path, denied) or path == denied for denied in config.security.denied_paths):
        raise PolicyError(f"repository is inside a denied path: {path}")

    if config.security.require_git_repository:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=config.limits.command_timeout_seconds,
            check=False,
            env=_safe_env(),
        )
        if proc.returncode != 0:
            raise PolicyError(f"not a git repository: {path}")
        path = Path(proc.stdout.strip()).resolve()
        if not any(_is_within(path, root) or path == root for root in config.security.allowed_roots):
            raise PolicyError("git toplevel is outside allowed roots")
        if any(_is_within(path, denied) or path == denied for denied in config.security.denied_paths):
            raise PolicyError("git toplevel is inside a denied path")
    return path


def resolve_repo_file(repo: Path, relative: str) -> Path:
    repo = repo.resolve()
    candidate = (repo / relative).resolve()
    if not _is_within(candidate, repo):
        raise PolicyError("path escapes repository")
    if candidate.is_symlink():
        resolved = candidate.resolve()
        if not _is_within(resolved, repo):
            raise PolicyError("symlink escapes repository")
    if not candidate.is_file():
        raise PolicyError(f"not a file: {relative}")
    return candidate


def validate_task(task: str, max_chars: int) -> str:
    task = task.strip()
    if not task:
        raise PolicyError("task must not be empty")
    if len(task) > max_chars:
        raise PolicyError(f"task exceeds {max_chars} characters")
    return task


def _safe_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
    return env
