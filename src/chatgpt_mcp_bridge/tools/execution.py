"""Direct project execution and file tools."""

import json
import shutil
import sys

from ..app import mcp
from ..runtime.context import get_executor as _get_backend
from .common import mcp_tool as _mcp_tool


def _json(value):
    return json.dumps(value, ensure_ascii=False)


@mcp.tool()
@_mcp_tool
async def get_environment() -> str:
    """Show this host's workspace and embedded execution backend."""
    backend = _get_backend()
    await backend.start()
    return _json(
        {
            "backend": "embedded-codex-executor",
            "workspace": str(backend.workspace),
            "network_access": backend.network,
        }
    )


@mcp.tool()
@_mcp_tool
async def execute(
    command: str,
    working_dir: str | None = None,
    timeout: int = 30,
    read_only: bool = False,
) -> str:
    """Execute a local shell command through Codex. No server parameter.

    working_dir must be within the configured workspace. timeout is in seconds
    (1..3600). Output is capped at 64 KiB per stream. Use exec_command for
    interactive commands or long work that needs polling. read_only selects
    Codex's read-only sandbox; otherwise writes are confined to the workspace.
    """
    if not command.strip() or len(command.encode()) > 65536:
        raise ValueError("command must contain 1..65536 UTF-8 bytes")
    shell = shutil.which("bash") or "/bin/sh"
    return _json(
        await _get_backend().execute(
            [shell, "-c", command], working_dir, timeout, read_only
        )
    )


@mcp.tool()
@_mcp_tool
async def exec_command(
    command: list[str],
    working_dir: str | None = None,
    timeout: int = 600,
    yield_time_ms: int = 1000,
    tty: bool = False,
    read_only: bool = False,
) -> str:
    """Start a local argv command through Codex, optionally with a PTY.

    Returns session_id when still running. Poll or send input with write_stdin;
    stop with terminate_process. yield_time_ms is 0..10000, timeout is seconds.
    Shell syntax requires an explicit argv such as ["bash", "-c", "..."]
    """
    if not 0 <= yield_time_ms <= 10000:
        raise ValueError("yield_time_ms must be between 0 and 10000")
    if sum(len(arg.encode()) for arg in command) > 65536:
        raise ValueError("command exceeds 64 KiB")
    backend = _get_backend()
    session_id = await backend.launch(command, working_dir, timeout, tty, read_only)
    return _json(await backend.poll(session_id, yield_time_ms))


@mcp.tool()
@_mcp_tool
async def write_stdin(
    session_id: str,
    chars: str = "",
    close_stdin: bool = False,
    yield_time_ms: int = 1000,
) -> str:
    """Poll a command or send UTF-8 input. close_stdin sends EOF (without a PTY).

    Sessions are shared by authorized users of this single-user deployment,
    expire on restart and are removed after the final output is retrieved.
    """
    if not 0 <= yield_time_ms <= 10000:
        raise ValueError("yield_time_ms must be between 0 and 10000")
    backend = _get_backend()
    await backend.write(session_id, chars, close_stdin)
    return _json(await backend.poll(session_id, yield_time_ms))


@mcp.tool()
@_mcp_tool
async def terminate_process(session_id: str) -> str:
    """Stop a running Codex command and retrieve its remaining output."""
    backend = _get_backend()
    await backend.terminate(session_id)
    return _json(await backend.poll(session_id, 1000))


@mcp.tool()
@_mcp_tool
async def read_file(path: str, start_line: int = 1, max_lines: int = 200) -> str:
    """Read a UTF-8 file inside the workspace through the Codex read-only sandbox.

    Output includes line numbers, with at most 2000 lines and 48 KiB of text.
    """
    if start_line < 1 or not 1 <= max_lines <= 2000:
        raise ValueError("start_line must be positive; max_lines must be 1..2000")
    script = (
        FILE_HEADER
        + r"""
start, count = int(sys.argv[3]), int(sys.argv[4])
remaining = 48000
with p.open(encoding='utf-8') as f:
    for n in range(1, start + count):
        line = f.readline(48001)
        if not line: break
        if len(line) > 48000: raise ValueError('Line exceeds 48000 characters')
        if n >= start:
            if len(line.encode()) > remaining:
                print('[output truncated; request fewer lines]'); break
            print(str(n) + ': ' + line, end='' if line.endswith('\n') else '\n')
            remaining -= len(line.encode())
"""
    )
    backend = _get_backend()
    return _json(
        await backend.execute(
            [
                sys.executable,
                "-c",
                script,
                str(backend.workspace),
                path,
                str(start_line),
                str(max_lines),
            ],
            read_only=True,
        )
    )


FILE_HEADER = """import sys, pathlib, base64, os
root = pathlib.Path(sys.argv[1]).resolve()
p = (root / sys.argv[2]).resolve()
if not p.is_relative_to(root): raise ValueError('Path is outside workspace')
"""


@mcp.tool()
@_mcp_tool
async def write_file(path: str, content: str, overwrite: bool = False) -> str:
    """Write a UTF-8 file through Codex (64 KiB maximum). Parent must exist.

    Existing files require overwrite=True. Paths are relative to the workspace
    or absolute within it. Codex's sandbox enforces the write boundary.
    """
    import base64

    if len(content.encode()) > 65536:
        raise ValueError("content exceeds 64 KiB; use apply_patch for edits")
    script = (
        FILE_HEADER
        + """
data = base64.b64decode(sys.argv[3])
with p.open('wb' if sys.argv[4] == 'true' else 'xb') as f:
    f.write(data)
print('Wrote ' + str(len(data)) + ' bytes')
"""
    )
    backend = _get_backend()
    return _json(
        await backend.execute(
            [
                sys.executable,
                "-c",
                script,
                str(backend.workspace),
                path,
                base64.b64encode(content.encode()).decode(),
                str(overwrite).lower(),
            ]
        )
    )


@mcp.tool()
@_mcp_tool
async def apply_patch(patch: str, working_dir: str | None = None) -> str:
    """Apply Codex's *** Begin Patch format in the workspace (64 KiB maximum).

    Uses the installed Codex patch helper under the Codex workspace sandbox.
    Supports adding, updating and deleting files. No model or login is used.
    """
    if len(patch.encode()) > 65536 or not patch.startswith("*** Begin Patch\n"):
        raise ValueError("Expected Codex patch format, at most 64 KiB")
    backend = _get_backend()
    return _json(
        await backend.execute(
            [backend.binary, "--codex-run-as-apply-patch", patch], working_dir
        )
    )
