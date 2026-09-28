from __future__ import annotations

from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import subprocess

from .config import Config
from .metrics import metrics_path


def _digest(path: Path) -> str:
    h = sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _git_head(root: Path) -> str | None:
    proc = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _ollama_model_identity(model: str) -> dict:
    proc = subprocess.run(["ollama", "list"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return {"name": model, "id": None}
    lines = proc.stdout.splitlines()
    for line in lines[1:]:
        parts = line.split()
        if parts and parts[0] == model:
            return {"name": model, "id": parts[1] if len(parts) > 1 else None}
    return {"name": model, "id": None}


def stack_lock_path() -> Path:
    return metrics_path().parent / "stack.lock.json"


def write_stack_lock(config: Config) -> Path:
    skills: dict[str, str] = {}
    for path in sorted((config.root / "skills").glob("*/SKILL.md")):
        skills[path.parent.name] = _digest(path)
    payload = {
        "source_commit": _git_head(config.root),
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "python": platform.python_version(),
        "packages": {
            "gremlins": _package_version("gremlins"),
            "mcp": _package_version("mcp"),
            "pydantic": _package_version("pydantic"),
        },
        "config": {"path": str(config.root / "gremlins.toml"), "digest": _digest(config.root / "gremlins.toml")},
        "model": _ollama_model_identity(config.provider.model),
        "skills": skills,
        "bindings": config.raw.get("bindings", {}),
    }
    path = stack_lock_path()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
