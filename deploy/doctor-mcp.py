#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
from pathlib import Path
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(execute=False, agent=False):
    path = os.getenv(
        "MCP_BRIDGE_HTTP_TOKEN_FILE",
        os.getenv(
            "SSH_MCP_HTTP_TOKEN_FILE", str(Path.home() / ".config/ssh-mcp/http-token")
        ),
    )
    token = Path(path).read_text().strip()
    port = int(
        os.getenv("MCP_BRIDGE_HTTP_PORT", os.getenv("SSH_MCP_HTTP_PORT", "8000"))
    )

    def data(result):
        if result.is_error:
            raise RuntimeError("MCP tool returned an error: " + str(result.content))
        value = getattr(result, "structured_content", None)
        if value is not None:
            if isinstance(value, dict) and isinstance(value.get("result"), str):
                return json.loads(value["result"])
            return value
        return json.loads("".join(x.text for x in result.content if x.type == "text"))

    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + token}, timeout=40
    ) as client:
        async with streamable_http_client(
            f"http://127.0.0.1:{port}/mcp", http_client=client
        ) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                expected = {
                    "get_environment",
                    "execute",
                    "exec_command",
                    "remote_ssh",
                    "write_stdin",
                    "terminate_process",
                    "read_file",
                    "write_file",
                    "apply_patch",
                    "start_agent",
                    "list_agent_tasks",
                    "get_agent_status",
                    "next_model_request",
                    "read_model_request",
                    "submit_model_response",
                    "agent_command",
                    "answer_agent_request",
                }
                assert names == expected, sorted(names)
                data(await session.call_tool("get_environment", arguments={}))
                print("Authenticated MCP discovery: 17 tools OK")
                if execute:
                    result = data(
                        await session.call_tool(
                            "execute",
                            arguments={"command": "printf mcp-ok", "read_only": True},
                        )
                    )
                    assert result["exit_code"] == 0 and result["stdout"] == "mcp-ok", (
                        result
                    )
                    print("Project execution OK")
                if agent:
                    created = data(
                        await session.call_tool(
                            "start_agent",
                            arguments={
                                "objective": "Reply mcp-native-ok. Do not inspect or change any files."
                            },
                        )
                    )
                    tid = created["thread_id"]
                    request = data(
                        await session.call_tool(
                            "next_model_request",
                            arguments={"thread_id": tid, "wait_ms": 5000},
                        )
                    )
                    assert "request_id" in request, request
                    data(
                        await session.call_tool(
                            "submit_model_response",
                            arguments={
                                "request_id": request["request_id"],
                                "output_text": "mcp-native-ok",
                            },
                        )
                    )
                    for _ in range(50):
                        result = data(
                            await session.call_tool(
                                "get_agent_status", arguments={"thread_id": tid}
                            )
                        )
                        if result["thread"]["status"] == "completed":
                            break
                        await asyncio.sleep(0.1)
                    else:
                        raise RuntimeError("Native response cycle did not finish")
                    print("Native Codex -> MCP model response -> completed turn OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe-execute", action="store_true")
    parser.add_argument("--probe-agent", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.probe_execute, args.probe_agent))
