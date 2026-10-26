"""Private Responses endpoint serviced by model decisions returned over MCP."""

import asyncio
import contextlib
import hashlib
import hmac
import json
import secrets
import socket

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .responses import make_response, response_events


class EmbeddedServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        yield


class WebModelRelay:
    def __init__(self, store):
        self.store = store
        self.token = secrets.token_urlsafe(48)
        self.waiters: dict[str, asyncio.Future] = {}
        self.server = None
        self.task = None
        self.socket = None
        self.url = None
        self.app = Starlette(
            routes=[Route("/v1/responses", self.responses, methods=["POST"])]
        )

    async def start(self):
        if self.task and not self.task.done():
            return
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(32)
        self.socket.setblocking(False)
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}/v1"
        self.server = EmbeddedServer(
            uvicorn.Config(self.app, log_level="error", access_log=False, log_config=None)
        )
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        for _ in range(100):
            if self.server.started:
                return
            if self.task.done():
                await self.task
                raise RuntimeError("Model relay stopped during startup")
            await asyncio.sleep(0.01)
        raise RuntimeError("Model relay startup timed out")

    async def responses(self, request: Request):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(
            supplied.encode(), ("Bearer " + self.token).encode()
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4 * 1024 * 1024:
                return JSONResponse(
                    {"error": "model request exceeds 4 MiB"}, status_code=413
                )
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("input", []), list
            ):
                raise ValueError("Expected a Responses object with an input list")
            thread_id = (
                request.headers.get("thread-id")
                or request.headers.get("x-client-request-id")
                or request.headers.get("session-id")
            )
            if not thread_id:
                raise ValueError("Missing native Codex thread header")
            request_id = self.store.create_request(
                payload, thread_id, request.headers.get("x-codex-parent-thread-id")
            )
        except (ValueError, json.JSONDecodeError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        future = asyncio.get_running_loop().create_future()
        self.waiters[request_id] = future
        if not payload.get("stream", False):
            try:
                response = await asyncio.wait_for(asyncio.shield(future), 3600)
                self.store.set_status(request_id, "delivered")
                return JSONResponse(response)
            finally:
                self.waiters.pop(request_id, None)
                if not future.done():
                    future.cancel()
                    self.store.set_status(request_id, "cancelled")

        async def stream():
            delivered = False
            try:
                yield ": waiting for the webpage model\n\n"
                while not future.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(future), 5)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                response = future.result()
                for event in response_events(response):
                    yield event
                self.store.set_status(request_id, "delivered")
                delivered = True
            finally:
                self.waiters.pop(request_id, None)
                if not delivered:
                    self.store.set_status(request_id, "cancelled")
                    if not future.done():
                        future.cancel()

        return StreamingResponse(stream(), media_type="text/event-stream")

    def submit(self, request_id, output_text, tool_calls):
        record = self.store.request(request_id)
        response, fingerprint = make_response(
            record["payload"], output_text, tool_calls
        )
        future = self.waiters.get(request_id)
        if record["status"] == "pending" and (future is None or future.cancelled()):
            self.store.set_status(request_id, "cancelled")
            raise ValueError(
                "The native inference connection has closed; resume the thread instead of replaying this request"
            )
        response = self.store.answer(request_id, response, fingerprint)
        if future and not future.done():
            future.set_result(response)
        return {
            "request_id": request_id,
            "thread_id": record["thread_id"],
            "status": "answered",
            "usage_source": "estimated_from_native_payload",
            "estimated_usage": response["usage"],
        }

    def packet(self, request_id, known_request_id=None, max_chars=24000):
        record = self.store.request(request_id)
        payload = record["payload"]
        value = {
            "request_id": request_id,
            "thread_id": record["thread_id"],
            "status": record["status"],
            "model": payload.get("model"),
            "context_mode": "full",
            "input": payload.get("input", []),
            "instructions": payload.get("instructions", ""),
            "tools": payload.get("tools", []),
            "response_format": "submit_model_response(output_text and/or tool_calls). Native tools execute inside Codex.",
        }
        if known_request_id:
            previous = self.store.request(known_request_id)
            if known_request_id == request_id or previous["status"] not in (
                "answered",
                "delivered",
            ):
                raise ValueError("known_request_id must be an earlier answered request")
            if previous["thread_id"] != record["thread_id"]:
                raise ValueError("known_request_id belongs to a different thread")
            earlier = previous["payload"]
            prefix = 0
            for before, after in zip(
                earlier.get("input", []), payload.get("input", [])
            ):
                if before != after:
                    break
                prefix += 1
            if prefix:
                value["context_mode"] = "delta"
                value["base_request_id"] = known_request_id
                value["unchanged_input_items"] = prefix
                value["input"] = payload.get("input", [])[prefix:]
                for name in ("instructions", "tools"):
                    if earlier.get(name) == payload.get(name):
                        value.pop(name)
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded) > max_chars:
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest()
            value = {
                "request_id": request_id,
                "thread_id": record["thread_id"],
                "status": record["status"],
                "context_mode": "paged",
                "payload_sha256": digest,
                "sections": {
                    name: len(json.dumps(payload.get(name, ""), ensure_ascii=False))
                    for name in ("instructions", "input", "tools")
                },
                "next_action": "Use read_model_request for instructions, input and tools; read every page before replying.",
            }
        return value

    def read(self, request_id, section, offset=0, limit=16000):
        if (
            section not in ("instructions", "input", "tools")
            or offset < 0
            or not 1 <= limit <= 24000
        ):
            raise ValueError("Invalid section, offset or page limit")
        value = self.store.request(request_id)["payload"].get(section, "")
        text = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        end = min(offset + limit, len(text))
        return {
            "request_id": request_id,
            "section": section,
            "offset": offset,
            "text": text[offset:end],
            "next_offset": end if end < len(text) else None,
            "total_chars": len(text),
        }

    async def close(self):
        self.store.cancel_pending()
        for future in list(self.waiters.values()):
            if not future.done():
                future.cancel()
        self.waiters.clear()
        if self.server:
            self.server.should_exit = True
        if self.task:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self.task, 5)
            if not self.task.done():
                self.task.cancel()
        if self.socket:
            self.socket.close()
