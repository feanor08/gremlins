from __future__ import annotations

from pathlib import Path
import tempfile
import subprocess

from .config import load_config
from .retrieval import literal_search, read_excerpt
from .security import resolve_repository
from .workers import _extract_log_evidence


def run_smoke_evals() -> dict:
    passed = 0
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as temp:
        repo = Path(temp) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "app.py").write_text("def retry():\n    raise RuntimeError('retry exhausted')\n", encoding="utf-8")
        cfg = load_config()
        # Temporary repo is outside normal policy; deterministic components are tested directly.
        hits = literal_search(repo, "retry exhausted", cfg)
        if hits and hits[0].path == "app.py":
            passed += 1
        else:
            failures.append("literal search")
        excerpt = read_excerpt(repo, "app.py", cfg)
        if "RuntimeError" in excerpt.text:
            passed += 1
        else:
            failures.append("bounded read")
        lines = _extract_log_evidence("ok\nERROR build failed\nTraceback: boom\n")
        if any("ERROR build failed" in line for line in lines):
            passed += 1
        else:
            failures.append("failure extraction")
    return {"passed": passed, "failed": len(failures), "failures": failures}
