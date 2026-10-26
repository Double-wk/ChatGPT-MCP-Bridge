import asyncio
import json

import httpx
import jsonschema
import pytest

from chatgpt_mcp_bridge.model_bridge.relay import WebModelRelay
from chatgpt_mcp_bridge.model_bridge.responses import make_response, response_events
from chatgpt_mcp_bridge.model_bridge.store import RequestStore


@pytest.fixture
def payload():
    return {
        "model": "gpt-web",
        "input": [{"role": "user", "content": "Inspect the project"}],
        "instructions": "Project agent context",
        "tools": [
            {
                "type": "function",
                "name": "exec_command",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                    "additionalProperties": False,
                },
            }
        ],
    }


def test_requests_survive_reopening_and_identical_answers_are_idempotent(
    tmp_path, payload
):
    path = tmp_path / "state.sqlite3"
    store = RequestStore(path)
    request_id = store.create_request(payload, "thread-1")
    reopened = RequestStore(path)
    assert reopened.request(request_id)["payload"] == payload
    first, fingerprint = make_response(payload, "Inspected", [])
    assert reopened.answer(request_id, first, fingerprint)["id"] == first["id"]
    second, same = make_response(payload, "Inspected", [])
    assert reopened.answer(request_id, second, same)["id"] == first["id"]
    different, changed = make_response(payload, "Different", [])
    with pytest.raises(ValueError, match="different response"):
        reopened.answer(request_id, different, changed)


def test_invalid_or_unoffered_calls_cannot_reach_native_execution(payload):
    with pytest.raises(ValueError, match="not offered"):
        make_response(payload, "", [{"name": "account_login", "arguments": "{}"}])
    with pytest.raises(jsonschema.ValidationError):
        make_response(payload, "", [{"name": "exec_command", "arguments": '{"cmd":9}'}])
    with pytest.raises(ValueError, match="custom tool"):
        make_response(
            payload, "", [{"kind": "custom", "name": "exec_command", "input": "whoami"}]
        )


def test_response_wire_preserves_tool_arguments_and_declares_estimated_usage(payload):
    args = json.dumps({"cmd": "printf 'hello world'"})
    response, _ = make_response(
        payload, "Checking files", [{"name": "exec_command", "arguments": args}]
    )
    assert response["output"][0]["phase"] == "commentary"
    assert response["output"][1]["arguments"] == args
    assert response["usage"]["total_tokens"] > 0
    wire = "".join(response_events(response))
    assert "response.output_item.done" in wire and "response.completed" in wire


def test_context_delta_and_full_reset_after_native_compaction(tmp_path, payload):
    relay = WebModelRelay(RequestStore(tmp_path / "bridge.sqlite3"))
    first = relay.store.create_request(payload, "thread-1")
    response, fingerprint = make_response(payload, "Read", [])
    relay.store.answer(first, response, fingerprint)
    next_payload = {
        **payload,
        "input": payload["input"]
        + [{"type": "function_call_output", "output": "files"}],
    }
    second = relay.store.create_request(next_payload, "thread-1")
    delta = relay.packet(second, first)
    assert delta["context_mode"] == "delta"
    assert delta["input"] == next_payload["input"][1:]
    assert "tools" not in delta and "instructions" not in delta
    compacted = relay.store.create_request(
        {**payload, "input": [{"role": "user", "content": "Saved compacted summary"}]},
        "thread-1",
    )
    assert relay.packet(compacted, first)["context_mode"] == "full"
    with pytest.raises(ValueError):
        relay.packet(second, second)


def test_context_paging_is_lossless(tmp_path, payload):
    relay = WebModelRelay(RequestStore(tmp_path / "bridge.sqlite3"))
    payload["instructions"] = "上下文" * 12000
    request_id = relay.store.create_request(payload, "thread-1")
    assert relay.packet(request_id)["context_mode"] == "paged"
    text, offset = "", 0
    while True:
        page = relay.read(request_id, "instructions", offset, 5000)
        text += page["text"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert text == payload["instructions"]


def test_native_events_and_thread_history_are_persistent_and_paginated(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    store = RequestStore(path)
    store.register_thread("thread-1", str(tmp_path))
    store.event("turn/started", {"threadId": "thread-1", "turn": {"id": "turn-1"}})
    store.event(
        "turn/plan/updated",
        {"threadId": "thread-1", "plan": [{"step": "Verify", "status": "inProgress"}]},
    )
    assert store.thread("thread-1")["status"] == "active"
    store.event(
        "turn/completed",
        {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
    )
    first = store.events("thread-1", limit=2)
    assert len(first) == 2
    assert (
        RequestStore(path).events("thread-1", first[-1]["seq"])[0]["method"]
        == "turn/completed"
    )
    assert RequestStore(path).thread("thread-1")["status"] == "completed"


async def test_private_http_relay_accepts_only_authorized_model_requests(
    tmp_path, payload
):
    relay = WebModelRelay(RequestStore(tmp_path / "bridge.sqlite3"))
    await relay.start()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            denied = await client.post(relay.url + "/responses", json=payload)
            assert denied.status_code == 401
            task = asyncio.create_task(
                client.post(
                    relay.url + "/responses",
                    json=payload,
                    headers={
                        "Authorization": "Bearer " + relay.token,
                        "thread-id": "thread-1",
                    },
                )
            )
            for _ in range(100):
                pending = relay.store.pending()
                if pending:
                    break
                await asyncio.sleep(0.01)
            assert len(pending) == 1
            result = relay.submit(pending[0]["id"], "Ready", [])
            assert result["usage_source"] == "estimated_from_native_payload"
            response = await task
            assert response.status_code == 200
            assert response.json()["output"][0]["content"][0]["text"] == "Ready"
            assert relay.store.request(pending[0]["id"])["status"] == "delivered"
    finally:
        await relay.close()


def test_cancelled_requests_cannot_be_replayed(tmp_path, payload):
    relay = WebModelRelay(RequestStore(tmp_path / "bridge.sqlite3"))
    request_id = relay.store.create_request(payload, "thread-1")
    relay.store.cancel_pending()
    with pytest.raises(ValueError, match="cancelled"):
        relay.submit(request_id, "Late reply", [])
