from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import tomllib

try:
    import pwd
except ImportError:  # pragma: no cover - non-POSIX fallback
    pwd = None


@dataclass(frozen=True)
class ProviderConfig:
    kind: str
    url: str
    model: str
    inference_lock_timeout_seconds: float = 3.0


@dataclass(frozen=True)
class Limits:
    max_task_chars: int
    max_evidence_chars: int
    max_result_evidence_chars: int
    max_file_chars: int
    max_search_matches: int
    max_history_entries: int
    max_model_output_tokens: int
    model_context_tokens: int
    model_timeout_seconds: int
    command_timeout_seconds: int


@dataclass(frozen=True)
class SecurityConfig:
    allowed_roots: tuple[Path, ...]
    denied_paths: tuple[Path, ...]
    require_git_repository: bool
    allow_network_to: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    root: Path
    provider: ProviderConfig
    limits: Limits
    security: SecurityConfig
    raw: dict = field(default_factory=dict)
    profile: str = "mac-local"


def _account_home() -> Path:
    """Return the OS account home, independent of a process-local HOME override."""
    if pwd is not None:
        try:
            return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        except (KeyError, OSError):
            pass
    return Path.home().resolve()


def _expand(path: str) -> Path:
    expanded = os.path.expandvars(path)
    if expanded == "~":
        return _account_home()
    if expanded.startswith("~/"):
        return (_account_home() / expanded[2:]).resolve()
    return Path(expanded).expanduser().resolve()


def project_root() -> Path:
    override = os.environ.get("GREMLINS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _load_profile(root: Path, name: str) -> dict:
    path = root / "profiles" / f"{name}.toml"
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_config(path: str | Path | None = None, profile: str | None = None) -> Config:
    root = project_root()
    config_path = Path(path).expanduser().resolve() if path else root / "gremlins.toml"
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    profile_name = profile or os.environ.get("GREMLINS_PROFILE", "mac-local")
    profile_data = _load_profile(root, profile_name)

    p = data["provider"]
    l = data["limits"]
    s = data["security"]

    allowed_roots: list[Path] = []
    for item in [
        *s["allowed_roots"],
        *profile_data.get("allowed_roots_extra", []),
        str(root),
    ]:
        expanded = _expand(item)
        if expanded not in allowed_roots:
            allowed_roots.append(expanded)

    return Config(
        root=root,
        provider=ProviderConfig(
            kind=p["kind"],
            url=p["url"].rstrip("/"),
            model=p["model"],
            inference_lock_timeout_seconds=float(p.get("inference_lock_timeout_seconds", 3.0)),
        ),
        limits=Limits(**l),
        security=SecurityConfig(
            allowed_roots=tuple(allowed_roots),
            denied_paths=tuple(_expand(x) for x in s["denied_paths"]),
            require_git_repository=bool(s.get("require_git_repository", True)),
            allow_network_to=tuple(s.get("allow_network_to", ["127.0.0.1", "localhost"])),
        ),
        raw={**data, "profile": profile_data},
        profile=profile_name,
    )
