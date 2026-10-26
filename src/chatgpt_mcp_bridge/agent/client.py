"""JSON-RPC client for the project-built upstream Codex App Server."""

import asyncio
import contextlib
import json
import os
import signal
from pathlib import Path


class AgentRuntime:
    def __init__(self, binary, home, workspace, on_event):
        self.binary = Path(binary)
        self.home = Path(home)
        self.workspace = Path(workspace)
        self.on_event = on_event
        self.process = None
        self.reader = None
        self.pending = {}
        self.requests = {}
        self.next_id = 0
        self.log = None

    async def start(self):
        if (
            self.process
            and self.process.returncode is None
            and self.reader
            and not self.reader.done()
        ):
            return
        await self.close()
        if not self.binary.is_file():
            raise RuntimeError(
                "The native agent is not built; run deploy/build-runtime.sh"
            )
        allowed = {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "TERM",
            "TMPDIR",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env["CODEX_HOME"] = str(self.home)
        env["PATH"] = str(self.binary.parent) + os.pathsep + env.get("PATH", os.defpath)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log = (self.home / "agent.log").open("ab")
        self.process = await asyncio.create_subprocess_exec(
            str(self.binary),
            cwd=self.workspace,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.log,
            start_new_session=True,
            limit=8 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self._read())
        try:
            await self.call(
                "initialize",
                {
                    "clientInfo": {"name": "chatgpt_mcp_bridge", "version": "0.3.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self._send({"method": "initialized"})
        except BaseException:
            await self.close()
            raise

    async def _send(self, value):
        if not self.process or self.process.returncode is not None:
            raise RuntimeError("Native Codex agent connection is closed")
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()

    async def call(self, method, params, timeout=30):
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "id" in message and ("result" in message or "error" in message):
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(
                                RuntimeError(
                                    message["error"].get(
                                        "message", "Native request failed"
                                    )
                                )
                            )
                        else:
                            future.set_result(message["result"])
                elif "method" in message:
                    if "id" in message:
                        self.requests[str(message["id"])] = message
                    self.on_event(message["method"], message.get("params", {}))
        except asyncio.CancelledError:
            raise
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(
                        RuntimeError(
                            "Native Codex agent stopped; no operation was replayed"
                        )
                    )

    async def answer_request(self, request_id, result):
        request = self.requests.get(str(request_id))
        if not request:
            raise ValueError("Unknown or already answered native request")
        method = request["method"]
        if method.endswith("requestApproval"):
            if result.get("decision") not in ("accept", "decline", "cancel"):
                raise ValueError(
                    "An approval decision must be accept, decline or cancel"
                )
        elif method.endswith("requestUserInput"):
            if not isinstance(result.get("answers"), dict):
                raise ValueError("User input requires an answers object")
        else:
            raise ValueError(
                f"Native request {method} is not exposed through this adapter"
            )
        await self._send({"id": request["id"], "result": result})
        self.requests.pop(str(request_id))
        return {"answered": True, "request_id": str(request_id)}

    async def close(self):
        if self.process:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
            if self.process.stdin:
                self.process.stdin.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await self.process.stdin.wait_closed()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        self.process = None
        self.reader = None
        self.requests.clear()
        if self.log:
            self.log.close()
            self.log = None
