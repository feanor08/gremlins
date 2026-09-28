from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import os


def metrics_path() -> Path:
    base = Path(os.environ.get("GREMLINS_STATE_DIR", "~/.local/state/gremlins")).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return base / "jobs.jsonl"


def record(event: dict) -> None:
    payload = dict(event)
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    with metrics_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_events(limit: int | None = None) -> list[dict]:
    path = metrics_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if limit is not None:
        lines = lines[-max(0, int(limit)):]
    rows: list[dict] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def events_for_measurement_tag(tag: str) -> list[dict]:
    return [row for row in load_events() if row.get("measurement_tag") == tag]
