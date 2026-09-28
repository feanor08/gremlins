from __future__ import annotations

import json
from pathlib import Path

from .metrics import metrics_path


def build_report(limit: int = 500) -> dict:
    path = metrics_path()
    if not path.exists():
        return {"jobs": 0, "by_worker": {}, "local_prompt_tokens": 0, "local_output_tokens": 0, "local_elapsed_seconds": 0.0}
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    by_worker: dict[str, int] = {}
    prompt = output = 0
    elapsed = 0.0
    statuses: dict[str, int] = {}
    for row in rows:
        worker = row.get("worker", "unknown")
        by_worker[worker] = by_worker.get(worker, 0) + 1
        status = row.get("status", "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        usage = row.get("usage") or {}
        prompt += int(usage.get("prompt_eval_count") or 0)
        output += int(usage.get("eval_count") or 0)
        elapsed += float(row.get("elapsed_seconds") or 0.0)
    return {
        "jobs": len(rows),
        "by_worker": by_worker,
        "statuses": statuses,
        "local_prompt_tokens": prompt,
        "local_output_tokens": output,
        "local_elapsed_seconds": round(elapsed, 3),
        "metrics_file": str(path),
    }
