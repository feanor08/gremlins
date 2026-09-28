from __future__ import annotations

import asyncio
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
            return {
                "ok": not missing and not bool(getattr(status_result, "isError", False)),
                "tools": tool_names,
                "missing_tools": missing,
                "status_call_error": bool(getattr(status_result, "isError", False)),
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
