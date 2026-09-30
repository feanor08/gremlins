from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import project_root


REQUIRED_TOOLS = {
    "gremlins_status",
    "repo_search",
    "code_read",
    "git_history",
    "evidence_pack",
    "repo_explorer",
    "failure_triage",
}


async def _check(command: str, args: list[str], env: dict[str, str] | None = None) -> dict:
    params = StdioServerParameters(command=command, args=args, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            response = await session.list_tools()
            tool_names = sorted(tool.name for tool in response.tools)
            missing = sorted(REQUIRED_TOOLS - set(tool_names))
            status_result = await session.call_tool("gremlins_status", arguments={})
            evidence_result = await session.call_tool(
                "evidence_pack",
                arguments={
                    "task": "Locate the evidence_pack MCP adapter implementation.",
                    "repository": str(project_root()),
                    "detail": "focused",
                    "paths": ["src/gremlins/server.py"],
                    "symbols": ["evidence_pack"],
                    "include_history": False,
                },
            )
            evidence_texts = [
                part.text
                for part in evidence_result.content
                if getattr(part, "type", None) == "text" and isinstance(getattr(part, "text", None), str)
            ]
            evidence_text = "\n".join(evidence_texts)
            try:
                evidence_payload = json.loads(evidence_text)
            except json.JSONDecodeError:
                evidence_payload = {}
            evidence_budget = int(
                ((evidence_payload.get("request") or {}).get("result_budget_chars") or 0)
            )
            evidence_wire_chars = len(evidence_text)
            structured = getattr(evidence_result, "structured_content", None)
            evidence_wire_budget_ok = bool(
                evidence_budget
                and evidence_wire_chars <= evidence_budget
                and structured == evidence_payload
            )
            status_error = bool(getattr(status_result, "isError", False))
            evidence_error = bool(getattr(evidence_result, "isError", False))
            return {
                "ok": (
                    not missing
                    and not status_error
                    and not evidence_error
                    and evidence_wire_budget_ok
                ),
                "tools": tool_names,
                "missing_tools": missing,
                "status_call_error": status_error,
                "evidence_pack_call_error": evidence_error,
                "evidence_pack_wire_chars": evidence_wire_chars,
                "evidence_pack_wire_budget_chars": evidence_budget,
                "evidence_pack_wire_budget_ok": evidence_wire_budget_ok,
            }


def check_python_module(timeout_seconds: float = 12.0, profile: str = "mac-local") -> dict:
    env = dict(os.environ)
    env["GREMLINS_ROOT"] = str(project_root())
    env["GREMLINS_PROFILE"] = profile

    async def runner() -> dict:
        return await asyncio.wait_for(
            _check(sys.executable, ["-m", "gremlins.server"], env=env),
            timeout=timeout_seconds,
        )

    try:
        return asyncio.run(runner())
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def check_wrapper(wrapper: Path, timeout_seconds: float = 12.0) -> dict:
    async def runner() -> dict:
        return await asyncio.wait_for(
            _check(str(wrapper), [], env=None),
            timeout=timeout_seconds,
        )

    try:
        return asyncio.run(runner())
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
