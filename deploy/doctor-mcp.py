#!/usr/bin/env python3
"""Authenticated local MCP discovery probe. Never prints bearer secrets.

Runs with the project's Conda interpreter. Verifies initialize, tools/list,
and read-only list_servers against 127.0.0.1. Use --probe-execute for a fixed,\nread-only hostname command on gpu-local.
"""
from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path
import os
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(probe_execute: bool = False) -> None:
    token_path = Path(
        os.environ.get(
            "SSH_MCP_HTTP_TOKEN_FILE",
            str(Path.home() / ".config/ssh-mcp/http-token"),
        )
    )
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("Bearer token file is empty")
    port = int(os.environ.get("SSH_MCP_HTTP_PORT", "8000"))
    url = f"http://127.0.0.1:{port}/mcp"
    # MCP SDK 2.3.0 takes a configured httpx2.AsyncClient (not headers=).
    # Its Streamable HTTP context manager yields TWO streams, not three.
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=30.0
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                response = await session.list_tools()
                tools = [tool.name for tool in response.tools]
                print(f"MCP initialize OK; discovered {len(tools)} tools: {', '.join(tools)}")
                if "list_servers" not in tools:
                    raise RuntimeError("Expected list_servers tool was not discovered")
                result = await session.call_tool("list_servers", arguments={})
                if result.is_error:
                    raise RuntimeError("list_servers returned an MCP tool error")
                print("list_servers OK (read-only; output not printed)")
                if not probe_execute:
                    print("SSH command execution has NOT been tested by this script.")
                    return
                if "execute" not in tools:
                    raise RuntimeError("Expected execute tool was not discovered")
                # Fixed, safe diagnostic; never accepts an arbitrary shell string.
                execution = await session.call_tool(
                    "execute",
                    arguments={
                        "server": "gpu-local",
                        "command": "hostname",
                        "timeout": 15,
                        "force": False,
                        "dry_run": False,
                    },
                )
                if execution.is_error:
                    raise RuntimeError("SSH execute MCP call failed; see server logs")
                output = " ".join(
                    getattr(item, "text", "")
                    for item in execution.content
                    if getattr(item, "type", "") == "text"
                )
                if not re.search(r"(?m)^Exit code: 0 [(]", output):
                    raise RuntimeError(
                        "SSH execute did not return exit code 0; " +
                        output[:700].replace(chr(10), " ")
                    )
                if "bms-51097659-001" not in output:
                    raise RuntimeError("SSH execute returned an unexpected hostname")
                print("MCP execute OK: gpu-local -> hostname -> bms-51097659-001 (exit 0)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="MCP discovery test, with optional safe read-only SSH execute probe."
    )
    parser.add_argument(
        "--probe-execute",
        action="store_true",
        help="Call execute on gpu-local using only the fixed read-only hostname command.",
    )
    args = parser.parse_args()
    asyncio.run(main(probe_execute=args.probe_execute))
