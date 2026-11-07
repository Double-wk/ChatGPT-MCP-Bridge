"""Private transport to the project's source-built Codex execution runtime."""

from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import json
import os
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import getenv, state_dir


@dataclass
class CommandSession:
    future: asyncio.Future
    output: dict[str, bytearray] = field(
        default_factory=lambda: {"stdout": bytearray(), "stderr": bytearray()}
    )
    truncated: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    decoders: dict = field(
        default_factory=lambda: {
            name: codecs.getincrementaldecoder("utf-8")("replace")
            for name in ("stdout", "stderr")
        }
    )
    received: dict = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    exit_code: int | None = None
    timed_out: bool = False
    timer: asyncio.Task | None = None


class ExecutionRuntime:
    """One private stdio connection, with bounded command sessions and output."""

    def __init__(self) -> None:
        self.workspace = (
            Path(getenv("CODEX_MCP_WORKSPACE", os.getcwd()))
            .expanduser()
            .resolve(strict=True)
        )
        if not self.workspace.is_dir():
            raise ValueError("CODEX_MCP_WORKSPACE must be a directory")
        self.binary = str(
            Path(__file__).resolve().parents[1] / "bin/chatgpt-mcp-bridge-runtime"
        )
        if not Path(self.binary).is_file() or not os.access(self.binary, os.X_OK):
            raise RuntimeError(
                "Project runtime is not built; run bash deploy/build-runtime.sh"
            )
        self.home = (
            Path(getenv("CODEX_MCP_HOME", str(state_dir() / "executor")))
            .expanduser()
            .resolve()
        )
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.sessions: dict[str, CommandSession] = {}
        self.start_lock = asyncio.Lock()
        self.next_id = 0
        self.output_cap = 65536
        self.network = getenv("CODEX_MCP_NETWORK_ACCESS", "false").lower() == "true"
        self.full_access = os.getenv("MCP_BRIDGE_FULL_ACCESS", "false").lower() == "true"
        if self.full_access:
            self.network = True
        self.sandboxes: dict[bool, dict] = {}

    def directory(self, path: str | None) -> str:
        target = (self.workspace / (path or ".")).resolve(strict=True)
        if not target.is_relative_to(self.workspace) or not target.is_dir():
            raise ValueError("Working directory must be inside CODEX_MCP_WORKSPACE")
        return str(target)

    async def start(self) -> None:
        async with self.start_lock:
            if (
                self.process
                and self.process.returncode is None
                and self.reader
                and not self.reader.done()
            ):
                return
            await self.close_all()
            self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Do not pass gateway credentials, API keys or the user's Codex login.
            names = {
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
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
            }
            env = {key: value for key, value in os.environ.items() if key in names}
            if self.network and "SSH_AUTH_SOCK" in os.environ:
                env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
            env["CODEX_HOME"] = str(self.home)
            env["PATH"] = (
                str(Path(self.binary).parent) + os.pathsep + env.get("PATH", os.defpath)
            )
            for read_only in (False, True):
                helper = await asyncio.create_subprocess_exec(
                    self.binary,
                    "--sandbox-context",
                    str(self.workspace),
                    str(read_only).lower(),
                    str(self.network).lower(),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await helper.communicate()
                if helper.returncode:
                    raise RuntimeError(
                        "Embedded Codex sandbox policy initialization failed"
                    )
                self.sandboxes[read_only] = json.loads(stdout)
                if self.full_access:
                    self.sandboxes[read_only]["permissions"] = {"type": "disabled"}
            self.process = await asyncio.create_subprocess_exec(
                self.binary,
                cwd=str(self.workspace),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=2 * 1024 * 1024,
                start_new_session=True,
            )
            self.reader = asyncio.create_task(self._read())
            try:
                await self._rpc("initialize", {"clientName": "chatgpt_mcp_bridge"})
                await self._send({"method": "initialized"})
            except BaseException:
                await self.close_all()
                raise

    async def _send(self, message: dict) -> None:
        if (
            not self.process
            or not self.process.stdin
            or self.process.returncode is not None
        ):
            raise RuntimeError("Codex connection is closed")
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def _request(self, method: str, params: dict) -> asyncio.Future:
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        future.add_done_callback(lambda _: self.pending.pop(request_id, None))
        try:
            await self._send({"id": request_id, "method": method, "params": params})
        except BaseException:
            future.cancel()
            raise
        return future

    async def _rpc(self, method: str, params: dict) -> Any:
        future = await self._request(method, params)
        try:
            return await asyncio.wait_for(future, 15)
        except asyncio.TimeoutError:
            raise RuntimeError(f"Codex {method} timed out") from None

    async def _read(self) -> None:
        try:
            assert self.process and self.process.stdout
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "id" in message and ("result" in message or "error" in message):
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(
                                RuntimeError(
                                    message["error"].get(
                                        "message", "Codex request failed"
                                    )
                                )
                            )
                        else:
                            future.set_result(message["result"])
                elif message.get("method") == "process/output":
                    params = message["params"]
                    session = self.sessions.get(params["processId"])
                    if session:
                        data = base64.b64decode(params["chunk"], validate=True)
                        stream = (
                            "stdout" if params["stream"] == "pty" else params["stream"]
                        )
                        buffer = session.output[stream]
                        available = max(0, self.output_cap - session.received[stream])
                        buffer.extend(data[:available])
                        session.received[stream] += len(data[:available])
                        session.truncated |= len(data) > available
                elif message.get("method") in ("process/exited", "process/closed"):
                    params = message["params"]
                    session = self.sessions.get(params["processId"])
                    if session:
                        if message["method"] == "process/exited":
                            session.exit_code = params["exitCode"]
                        elif not session.future.done():
                            session.future.set_result(
                                {
                                    "exitCode": 124
                                    if session.timed_out
                                    else session.exit_code
                                }
                            )
                            if session.timer:
                                session.timer.cancel()
                elif "id" in message:
                    # This adapter does not perform interactive model approvals.
                    await self._send(
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": "Unsupported server request",
                            },
                        }
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail outstanding calls; commands are never automatically retried.
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(
                        RuntimeError("Codex connection closed; command was not retried")
                    )
            for session in self.sessions.values():
                if not session.future.done():
                    session.future.set_exception(
                        RuntimeError("Codex connection closed; command was not retried")
                    )
                if session.timer:
                    session.timer.cancel()

    async def _expire(self, process_id: str, timeout: int) -> None:
        try:
            await asyncio.sleep(timeout)
            session = self.sessions.get(process_id)
            if session and not session.future.done():
                session.timed_out = True
                await self.terminate(process_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session = self.sessions.get(process_id)
            if session and not session.future.done():
                session.future.set_exception(exc)

    async def launch(
        self,
        argv: list[str],
        cwd: str | None,
        timeout: int | None,
        tty: bool = False,
        read_only: bool = False,
    ) -> str:
        if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise ValueError("command must be a non-empty argv array without NUL bytes")
        if timeout is not None and not 1 <= timeout <= 3600:
            raise ValueError("timeout must be between 1 and 3600 seconds")
        directory = self.directory(cwd)
        await self.start()
        # Completed sessions are evicted when the fixed capacity is reached.
        if len(self.sessions) >= 16:
            for key, session in list(self.sessions.items()):
                if session.future.done():
                    if not session.future.cancelled():
                        session.future.exception()
                    self.sessions.pop(key)
        if len(self.sessions) >= 16:
            raise ValueError(
                "16 commands are already running; terminate or finish one first"
            )
        process_id = uuid.uuid4().hex
        placeholder = asyncio.get_running_loop().create_future()
        session = CommandSession(placeholder)
        self.sessions[process_id] = session
        try:
            result = await self._rpc(
                "process/start",
                {
                    "argv": argv,
                    "cwd": Path(directory).as_uri(),
                    "processId": process_id,
                    "pipeStdin": True,
                    "env": {},
                    "envPolicy": {
                        "inherit": "all",
                        "ignoreDefaultExcludes": False,
                        "exclude": ["CODEX_*", "OPENAI_*", "GITHUB_*", "GATEWAY_*"],
                        "set": {},
                        "includeOnly": [],
                    },
                    "tty": tty,
                    "sandbox": self.sandboxes[read_only],
                },
            )
            if not self.full_access and result.get("sandboxType") in (None, "none"):
                await self.terminate(process_id)
                raise RuntimeError("Codex did not activate the requested sandbox")
            if not session.future.done() and timeout is not None:
                session.timer = asyncio.create_task(self._expire(process_id, timeout))
        except BaseException:
            self.sessions.pop(process_id, None)
            session.future.cancel()
            raise
        return process_id

    async def poll(self, process_id: str, yield_time_ms: int = 1000) -> dict:
        if not 0 <= yield_time_ms <= 10000:
            raise ValueError("yield_time_ms must be between 0 and 10000")
        session = self.sessions.get(process_id)
        if session is None:
            raise ValueError("Unknown or expired session_id")
        async with session.lock:
            if not session.future.done():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(session.future), yield_time_ms / 1000
                    )
            done = session.future.done()
            result = session.future.result() if done else {}
            response = {
                "session_id": None if done else process_id,
                "running": not done,
                "exit_code": result.get("exitCode"),
                "truncated": session.truncated,
            }
            for name, data in session.output.items():
                response[name] = session.decoders[name].decode(bytes(data), final=done)
                data.clear()
            if done:
                self.sessions.pop(process_id, None)
            return response

    async def write(self, process_id: str, chars: str, close_stdin: bool) -> None:
        session = self.sessions.get(process_id)
        if session is None:
            raise ValueError("Unknown or expired session_id")
        if len(chars.encode()) > 65536:
            raise ValueError("stdin is limited to 64 KiB per call")
        if not session.future.done() and (chars or close_stdin):
            if chars:
                result = await self._rpc(
                    "process/write",
                    {
                        "processId": process_id,
                        "chunk": base64.b64encode(chars.encode()).decode(),
                        "writeId": uuid.uuid4().hex,
                    },
                )
                if result["status"] != "accepted":
                    raise RuntimeError(
                        f"Codex stdin write rejected: {result['status']}"
                    )
            if close_stdin:
                await self._rpc("process/closeStdin", {"processId": process_id})

    async def terminate(self, process_id: str) -> None:
        session = self.sessions.get(process_id)
        if session is None:
            raise ValueError("Unknown or expired session_id")
        if not session.future.done():
            await self._rpc("process/terminate", {"processId": process_id})

    async def execute(
        self,
        argv: list[str],
        cwd: str | None = None,
        timeout: int = 30,
        read_only: bool = False,
    ) -> dict:
        process_id = await self.launch(argv, cwd, timeout, read_only=read_only)
        try:
            async with asyncio.timeout(timeout + 10):
                await asyncio.shield(self.sessions[process_id].future)
            return await self.poll(process_id, 0)
        except BaseException:
            with contextlib.suppress(Exception):
                await self.terminate(process_id)
            raise

    async def close_all(self) -> None:
        process, reader = self.process, self.reader
        if process:
            # npm's codex command is a Node wrapper. Kill its process group,
            # including the native App Server, even if the wrapper has exited.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            if process.stdin:
                process.stdin.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.wait_closed()
        if reader and reader is not asyncio.current_task():
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(reader), 2)
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        for session in self.sessions.values():
            if session.timer:
                session.timer.cancel()
            if session.future.done() and not session.future.cancelled():
                session.future.exception()
        self.sessions.clear()
        self.process = None
        self.reader = None
