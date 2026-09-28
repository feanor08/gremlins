from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import Config


class ProviderError(RuntimeError):
    pass


class ProviderBusy(ProviderError):
    pass


def _lock_path() -> Path:
    state = Path(os.environ.get("GREMLINS_STATE_DIR", "~/.local/state/gremlins")).expanduser()
    path = state / "locks" / "inference.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def global_inference_slot(timeout_seconds: float):
    path = _lock_path()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    with path.open("a+") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProviderBusy("local inference is busy")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _validate_endpoint(config: Config) -> None:
    parsed = urllib.parse.urlparse(config.provider.url)
    if parsed.scheme != "http":
        raise ProviderError("only http local/private provider endpoints are allowed")
    if not parsed.hostname or parsed.hostname not in config.security.allow_network_to:
        raise ProviderError(f"provider host is not allowlisted: {parsed.hostname}")


def _request(url: str, payload: dict | None, timeout: int) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
        raise ProviderError(str(exc)) from exc


def health(config: Config) -> dict:
    _validate_endpoint(config)
    url = f"{config.provider.url}/api/tags"
    data = _request(url, None, min(5, config.limits.model_timeout_seconds))
    names = [m.get("name") for m in data.get("models", [])]
    return {"ok": True, "model_present": config.provider.model in names, "models": names[:30]}


def chat_json(config: Config, system: str, user: str, schema: dict) -> tuple[dict, dict]:
    _validate_endpoint(config)
    if len(user) > config.limits.max_evidence_chars + config.limits.max_task_chars + 16000:
        raise ProviderError("model input exceeds Gremlins budget")
    payload = {
        "model": config.provider.model,
        "stream": False,
        "think": False,
        "format": schema,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {
            "temperature": 0.1,
            "num_ctx": config.limits.model_context_tokens,
            "num_predict": config.limits.max_model_output_tokens,
        },
    }
    started = time.monotonic()
    with global_inference_slot(config.provider.inference_lock_timeout_seconds):
        data = _request(f"{config.provider.url}/api/chat", payload, config.limits.model_timeout_seconds)
    elapsed = time.monotonic() - started
    content = (data.get("message") or {}).get("content", "")
    if not content:
        raise ProviderError("local model returned an empty response")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ProviderError("local model did not return valid JSON") from exc
    usage = {
        "local_model_called": True,
        "model": config.provider.model,
        "elapsed_seconds": round(elapsed, 3),
        "prompt_eval_count": data.get("prompt_eval_count"),
        "eval_count": data.get("eval_count"),
        "total_duration_ns": data.get("total_duration"),
    }
    return parsed, usage
