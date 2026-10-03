#!/usr/bin/env python3
"""Deterministic Forgejo preflight for Gremlins frontier-treatment readiness.

This script performs no frontier or local-model calls. It validates the exact
repository evidence shapes that must pass before Mac Codex treatment is allowed.
"""
from __future__ import annotations

import json
from pathlib import Path

from gremlins.config import load_config
from gremlins.evidence_service import evidence_pack


def wire_chars(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def file_blob(payload: dict, path: str) -> str:
    for item in payload.get("files") or []:
        if item.get("path") == path:
            return json.dumps(item, ensure_ascii=False)
    raise AssertionError(f"missing evidence file: {path}")


def check_repo002() -> None:
    config = load_config()
    result = evidence_pack(
        ".",
        "Find the frontier-facing evidence size budget.",
        config,
        detail="focused",
        paths=["src/gremlins/config.py"],
        terms=["size_budget", "frontier-facing"],
        symbols=["size_budget"],
        include_history=False,
        include_tests=False,
        max_files=4,
    )

    size = wire_chars(result)
    assert size <= 3600, f"repo-002 focused pack exceeds budget: {size}"

    discovery = result.get("discovery") or {}
    assert discovery.get("terms") == [
        "size_budget",
        "frontier-facing",
    ], f"repo-002 discovery terms broadened: {discovery.get('terms')!r}"

    config_blob = file_blob(result, "src/gremlins/config.py")
    toml_blob = file_blob(result, "gremlins.toml")
    assert "max_result_evidence_chars" in config_blob, (
        "repo-002 config evidence omitted max_result_evidence_chars"
    )
    assert "max_result_evidence_chars = 8000" in toml_blob, (
        "repo-002 TOML evidence omitted configured 8000 value"
    )

    print(f"FORGEJO_PREFLIGHT_REPO002=PASS chars={size}")


def check_repo003() -> None:
    config = load_config()
    result = evidence_pack(
        ".",
        (
            "Exact final verification: show the evidence_pack selection "
            "implementation and the expressions terms or, symbols or, and "
            "fallback extraction."
        ),
        config,
        detail="focused",
        paths=["src/gremlins/evidence_service.py"],
        terms=["terms or", "symbols or", "fallback extraction"],
        symbols=["evidence_pack"],
        include_history=False,
        include_tests=False,
        max_files=1,
    )

    size = wire_chars(result)
    assert size <= 3600, f"repo-003 focused pack exceeds budget: {size}"

    blob = file_blob(result, "src/gremlins/evidence_service.py")
    for required in (
        "def _discovery_terms",
        "explicit = bool(terms or symbols)",
        "base = effective_terms",
        "if explicit:",
        "return base, []",
    ):
        assert required in blob, f"repo-003 evidence omitted {required!r}"

    print(f"FORGEJO_PREFLIGHT_REPO003=PASS chars={size}")


def main() -> int:
    root = Path.cwd()
    assert (root / "pyproject.toml").is_file(), "run from repository root"
    check_repo002()
    check_repo003()
    print("GREMLINS_FORGEJO_DETERMINISTIC_PREFLIGHT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
