"""Exercise actual Codex planning, execution, compaction and goal continuation.

The scripted webpage responses make protocol behavior deterministic; they do
not measure the quality or speed of a real ChatGPT conversation.
"""

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from chatgpt_mcp_bridge.agent.service import AgentService


@pytest.fixture
async def agent(tmp_path, monkeypatch):
    binary = (
        Path(__file__).resolve().parents[2]
        / "src/chatgpt_mcp_bridge/bin/chatgpt-mcp-bridge"
    )
    if not binary.is_file():
        pytest.skip("Build the native agent first")
    if (
        binary.read_bytes()[:4] != b"\x7fELF"
        and os.getenv("MCP_BRIDGE_ALLOW_PROBE_AGENT") != "1"
    ):
        pytest.skip("Native artifact required")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("MCP_BRIDGE_AGENT_HOME", str(tmp_path / "agent-state"))
    service = AgentService(workspace)
    await service.start()
    try:
        yield service
    finally:
        await service.close()


async def next_request(agent, thread_id, previous=None):
    for _ in range(4):
        packet = await agent.next_request(thread_id, previous, wait_ms=5000)
        if packet.get("request_id"):
            return packet["request_id"]
    raise AssertionError(await agent.status(thread_id))


def function(agent, request_id, name, arguments, text=""):
    return agent.relay.submit(
        request_id, text, [{"name": name, "arguments": json.dumps(arguments)}]
    )


async def wait_finished(agent, thread_id):
    for _ in range(100):
        if agent.store.thread(thread_id)["status"] in (
            "completed",
            "failed",
            "interrupted",
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(await agent.status(thread_id))


async def test_native_plan_patch_execute_and_compact(agent):
    thread_id = (
        await agent.create("Create result.txt with READY, verify it, and finish.")
    )["thread_id"]
    request = await next_request(agent, thread_id)
    function(
        agent,
        request,
        "update_plan",
        {
            "plan": [
                {"step": "Implement result", "status": "in_progress"},
                {"step": "Verify contents", "status": "pending"},
            ]
        },
    )
    request = await next_request(agent, thread_id, request)
    agent.relay.submit(
        request,
        "",
        [
            {
                "name": "apply_patch",
                "kind": "custom",
                "input": "*** Begin Patch\n*** Add File: result.txt\n+READY\n*** End Patch",
            }
        ],
    )
    request = await next_request(agent, thread_id, request)
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "from pathlib import Path;assert Path('result.txt').read_text() == 'READY'+chr(10)",
        ]
    )
    function(
        agent,
        request,
        "exec_command",
        {"cmd": command, "workdir": str(agent.workspace), "max_output_tokens": 2000},
    )
    request = await next_request(agent, thread_id, request)
    function(
        agent,
        request,
        "update_plan",
        {
            "plan": [
                {"step": "Implement result", "status": "completed"},
                {"step": "Verify contents", "status": "completed"},
            ]
        },
    )
    request = await next_request(agent, thread_id, request)
    agent.relay.submit(request, "Created and verified result.txt.", [])
    await wait_finished(agent, thread_id)
    assert (agent.workspace / "result.txt").is_file(), json.dumps(
        agent.store.events(thread_id, limit=100), ensure_ascii=False
    )
    assert (agent.workspace / "result.txt").read_text() == "READY\n"
    assert any(
        event["method"] == "turn/plan/updated"
        for event in agent.store.events(thread_id, limit=100)
    )
    await agent.command("/compact", thread_id)
    request = await next_request(agent, thread_id)
    agent.relay.submit(
        request,
        "Task complete. result.txt contains READY and its Python verification passed. No remaining work.",
        [],
    )
    await wait_finished(agent, thread_id)
    events = agent.store.events(thread_id, limit=100)
    assert "contextCompaction" in json.dumps(events)


async def test_native_goal_continues_without_a_new_user_turn(agent):
    thread_id = (await agent.create("Report READY and complete this goal.", goal=True))[
        "thread_id"
    ]
    first = await next_request(agent, thread_id)
    agent.relay.submit(first, "I am checking the requested result.", [])
    second = await next_request(agent, thread_id, first)
    assert second != first
    function(agent, second, "update_goal", {"status": "complete"}, "READY")
    third = await next_request(agent, thread_id, second)
    agent.relay.submit(third, "READY. Goal complete.", [])
    await wait_finished(agent, thread_id)
    goal = (await agent.command("/goal", thread_id))["goal"]
    assert goal["status"] == "complete"
    assert goal["tokensUsed"] > 0
    turns = [
        event
        for event in agent.store.events(thread_id, limit=100)
        if event["method"] == "turn/started"
    ]
    assert len(turns) >= 2


async def test_native_goal_pause_and_resume(agent):
    thread_id = (
        await agent.create("Keep working until READY is reported.", goal=True)
    )["thread_id"]
    await next_request(agent, thread_id)
    await agent.command("/goal pause", thread_id)
    assert (await agent.command("/goal", thread_id))["goal"]["status"] == "paused"
    await agent.command("/goal resume", thread_id)
    request = await next_request(agent, thread_id)
    assert agent.store.request(request)["status"] == "pending"
    await agent.command("/pause", thread_id)


async def test_native_sandbox_rejects_outside_writes(agent):
    thread_id = (await agent.create("Check sandbox write restrictions."))["thread_id"]
    request = await next_request(agent, thread_id)
    outside = agent.workspace.parent / "outside.txt"
    command = shlex.join(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path;Path({str(outside)!r}).write_text('bad')",
        ]
    )
    function(
        agent,
        request,
        "exec_command",
        {"cmd": command, "workdir": str(agent.workspace), "max_output_tokens": 2000},
    )
    request = await next_request(agent, thread_id, request)
    assert not outside.exists()
    agent.relay.submit(request, "The sandbox rejected the outside write.", [])
    await wait_finished(agent, thread_id)


async def test_thread_recovers_after_agent_restart(agent):
    thread_id = (await agent.create("Remember that the task marker is READY."))[
        "thread_id"
    ]
    request = await next_request(agent, thread_id)
    agent.relay.submit(request, "Task marker: READY.", [])
    await wait_finished(agent, thread_id)
    await agent.close()
    await agent.start()
    await agent.command("/resume Continue the saved task.", thread_id)
    request = await next_request(agent, thread_id)
    payload = agent.store.request(request)["payload"]
    assert "READY" in json.dumps(payload["input"])
    agent.relay.submit(request, "Recovered READY from the saved thread.", [])
    await wait_finished(agent, thread_id)
