"""Translate explicit webpage decisions into the native Responses wire format."""

import hashlib
import json
import math
import time
import uuid

import jsonschema


def estimate_tokens(value):
    return max(1, math.ceil(len(json.dumps(value, ensure_ascii=False).encode()) / 3))


def make_response(request, output_text, tool_calls):
    if not output_text and not tool_calls:
        raise ValueError("Provide output_text, tool_calls, or both")
    if len(tool_calls) > 32:
        raise ValueError("At most 32 native tool calls are accepted per response")
    offered = {
        tool["name"]: tool for tool in request.get("tools", []) if "name" in tool
    }
    output = []
    if output_text:
        if len(output_text.encode()) > 262144:
            raise ValueError("output_text exceeds 256 KiB")
        output.append(
            {
                "type": "message",
                "id": "msg_" + uuid.uuid4().hex,
                "role": "assistant",
                "status": "completed",
                "phase": "commentary" if tool_calls else "final_answer",
                "content": [
                    {"type": "output_text", "text": output_text, "annotations": []}
                ],
            }
        )
    for call in tool_calls:
        name = call["name"]
        spec = offered.get(name)
        if spec is None:
            raise ValueError(
                f"Native tool {name!r} was not offered in this model request"
            )
        kind = call.get("kind", "function")
        call_id = "call_" + uuid.uuid4().hex
        if kind == "custom":
            if spec["type"] != "custom":
                raise ValueError(f"{name} is not a custom tool")
            value = call.get("input", "")
            if not isinstance(value, str) or not value:
                raise ValueError("A custom tool requires its text input")
            if len(value.encode()) > 262144:
                raise ValueError("Custom tool input exceeds 256 KiB")
            output.append(
                {
                    "type": "custom_tool_call",
                    "id": "ctc_" + uuid.uuid4().hex,
                    "call_id": call_id,
                    "name": name,
                    "input": value,
                    "status": "completed",
                }
            )
        elif kind == "function":
            if spec["type"] != "function":
                raise ValueError(f"{name} requires a custom tool call")
            arguments = call.get("arguments", "{}")
            if not isinstance(arguments, str):
                raise ValueError("arguments must be a JSON string")
            if len(arguments.encode()) > 65536:
                raise ValueError("Function arguments exceed 64 KiB")
            parsed = json.loads(arguments)
            jsonschema.validate(parsed, spec.get("parameters", {}))
            output.append(
                {
                    "type": "function_call",
                    "id": "fc_" + uuid.uuid4().hex,
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "status": "completed",
                }
            )
        else:
            raise ValueError("Tool kind must be function or custom")
    response = {
        "id": "resp_" + uuid.uuid4().hex,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": request.get("model", "gpt-web"),
        "output": output,
        "usage": {
            "input_tokens": estimate_tokens(request),
            "output_tokens": estimate_tokens(output),
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    response["usage"]["total_tokens"] = (
        response["usage"]["input_tokens"] + response["usage"]["output_tokens"]
    )
    submission_hash = hashlib.sha256(
        json.dumps({"text": output_text, "calls": tool_calls}, sort_keys=True).encode()
    ).hexdigest()
    return response, submission_hash


def response_events(response):
    def event(kind, **fields):
        return (
            "event: "
            + kind
            + "\ndata: "
            + json.dumps({"type": kind, **fields})
            + "\n\n"
        )

    yield event(
        "response.created", response={**response, "status": "in_progress", "output": []}
    )
    for index, item in enumerate(response["output"]):
        yield event("response.output_item.added", output_index=index, item=item)
        yield event("response.output_item.done", output_index=index, item=item)
    yield event("response.completed", response=response)
