"""Sandboxed code execution: run_python in a throwaway Docker container.

No network, capped memory/CPU, 30s timeout, container removed afterwards.
The tool name matches the `run_` approval pattern, so every execution is
approval-gated on top of the sandbox. Degrades to a clear error if Docker
is not installed.
"""
from __future__ import annotations

import asyncio

from langchain_core.tools import tool

TIMEOUT_S = 30
IMAGE = "python:3.12-slim"


@tool
async def run_python(code: str) -> str:
    """Execute a Python snippet in an isolated Docker sandbox (no network, no host access)
    and return stdout/stderr. Use for calculations, parsing, or verifying code output."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "--rm", "--network=none", "-m", "512m", "--cpus", "1",
            "-i", IMAGE, "python", "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return "ERROR: Docker is not installed/running — sandboxed execution unavailable."

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return f"ERROR: execution exceeded {TIMEOUT_S}s and was killed."

    text = (out.decode(errors="replace") + ("\n--- stderr ---\n" + err.decode(errors="replace") if err else "")).strip()
    return text[:6000] if text else "(no output)"


def sandbox_tools() -> list:
    return [run_python]
