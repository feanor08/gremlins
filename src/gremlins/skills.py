from __future__ import annotations

from pathlib import Path
import re

from .config import Config


class SkillError(RuntimeError):
    pass


def load_skill(config: Config, name: str) -> str:
    path = config.root / "skills" / name / "SKILL.md"
    if not path.is_file():
        raise SkillError(f"skill not found: {name}")
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5 :]
    return text.strip()


def worker_skill_names(config: Config, worker: str) -> list[str]:
    workers = config.raw.get("workers", {})
    spec = workers.get(worker, {})
    names = spec.get("skills", [])
    if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
        raise SkillError(f"invalid skill list for worker: {worker}")
    return list(names)


def worker_instructions(config: Config, worker: str) -> str:
    chunks = []
    for name in worker_skill_names(config, worker):
        chunks.append(f"SKILL {name}:\n{load_skill(config, name)}")
    return "\n\n".join(chunks)
