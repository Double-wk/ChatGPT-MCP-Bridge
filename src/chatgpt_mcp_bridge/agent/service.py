"""Thin native-thread orchestration. Planning, compaction and goals stay in Codex."""

import asyncio
import json
import os
from pathlib import Path

from ..config import getenv, state_dir
from ..model_bridge.relay import WebModelRelay
from ..model_bridge.store import RequestStore
from .client import AgentRuntime


class AgentService:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.home = (
            Path(os.environ.get("MCP_BRIDGE_AGENT_HOME", str(state_dir() / "agent")))
            .expanduser()
            .resolve()
        )
        self.store = RequestStore(self.home / "bridge.sqlite3")
        self.store.recover_dead_requests()
        self.relay = WebModelRelay(self.store)
        binary = Path(__file__).resolve().parents[1] / "bin/chatgpt-mcp-bridge"
        self.client = AgentRuntime(binary, self.home, self.workspace, self._event)
        self.loaded = set()
        self.lock = asyncio.Lock()

    def _event(self, method, params):
        if method == "thread/started":
            thread = params.get("thread", {})
            cwd = Path(thread.get("cwd") or str(self.workspace)).resolve()
            if thread.get("id") and cwd.is_relative_to(self.workspace):
                self.store.register_thread(thread["id"], str(cwd))
        if method.endswith("Delta") or method.endswith("/delta"):
            return
        self.store.event(method, params)

    async def start(self):
        async with self.lock:
            await self.relay.start()
            if (
                self.client.process
                and self.client.process.returncode is None
                and self.client.reader
                and not self.client.reader.done()
            ):
                return
            self.loaded.clear()
            self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            sandbox = "danger-full-access" if os.getenv("MCP_BRIDGE_FULL_ACCESS", "false").lower() == "true" else "workspace-write"
            config = f"""model = "gpt-web"
model_provider = "web_bridge"
approval_policy = "never"
sandbox_mode = "{sandbox}"
model_context_window = 128000
model_auto_compact_token_limit = 64000
model_catalog_json = {json.dumps(str(Path(__file__).resolve().parents[1] / "assets/models.json"))}
web_search = "disabled"

[tools.update_plan]
enabled = true

[features]
code_mode = false
code_mode_host = false

[sandbox_workspace_write]
network_access = {str(getenv("CODEX_MCP_NETWORK_ACCESS", "false").lower() == "true").lower()}
exclude_slash_tmp = true
exclude_tmpdir_env_var = true

[model_providers.web_bridge]
name = "ChatGPT MCP Bridge"
base_url = {json.dumps(self.relay.url)}
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 3600000
http_headers = {{ Authorization = {json.dumps("Bearer " + self.relay.token)} }}
"""
            path = self.home / "config.toml"
            temporary = path.with_suffix(".tmp")
            descriptor = os.open(
                temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600
            )
            with os.fdopen(descriptor, "w") as output:
                output.write(config)
            os.replace(temporary, path)
            try:
                await self.client.start()
            except BaseException:
                await self.relay.close()
                raise

    def directory(self, value):
        cwd = (self.workspace / (value or ".")).resolve(strict=True)
        if not cwd.is_relative_to(self.workspace) or not cwd.is_dir():
            raise ValueError("working_dir must be inside the configured workspace")
        return str(cwd)

    async def create(self, objective, working_dir=None, goal=False):
        if not objective.strip() or len(objective) > 16000:
            raise ValueError("objective must contain 1..16000 characters")
        if goal and len(objective) > 4000:
            raise ValueError("A native goal objective is limited to 4000 characters")
        cwd = self.directory(working_dir)
        result = await self.client.call(
            "thread/start",
            {
                "model": "gpt-web",
                "modelProvider": "web_bridge",
                "cwd": cwd,
                "approvalPolicy": "never",
                "sandbox": "danger-full-access" if os.getenv("MCP_BRIDGE_FULL_ACCESS", "false").lower() == "true" else "workspace-write",
            },
        )
        thread_id = result["thread"]["id"]
        self.store.register_thread(thread_id, cwd)
        self.loaded.add(thread_id)
        if goal:
            await self.client.call(
                "thread/goal/set",
                {
                    "threadId": thread_id,
                    "objective": objective,
                    "status": "active",
                    "origin": "user",
                },
            )
        await self.client.call(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": objective}]},
        )
        return {
            "thread_id": thread_id,
            "engine": "native_codex",
            "next_action": "next_model_request",
            "usage_source": "estimated_from_native_payload",
            "goal": goal,
        }

    async def ensure_loaded(self, thread_id):
        self.directory(self.store.thread(thread_id)["cwd"])
        if thread_id not in self.loaded:
            await self.client.call(
                "thread/resume",
                {
                    "threadId": thread_id,
                    "model": "gpt-web",
                    "modelProvider": "web_bridge",
                    "excludeTurns": True,
                },
            )
            self.loaded.add(thread_id)

    async def status(self, thread_id, after_event=0, limit=30):
        thread = self.store.thread(thread_id)
        events = self.store.events(thread_id, after_event, limit)
        pending = self.store.pending(thread_id)
        requests = [
            {
                "request_id": str(key),
                "method": value["method"],
                "params": value.get("params", {}),
            }
            for key, value in self.client.requests.items()
            if value.get("params", {}).get("threadId") == thread_id
        ]
        goal = None
        if thread_id in self.loaded:
            goal = (
                await self.client.call("thread/goal/get", {"threadId": thread_id})
            ).get("goal")
        return {
            "thread": thread,
            "events": events,
            "next_event": events[-1]["seq"] if events else after_event,
            "pending_model_requests": pending,
            "user_requests": requests,
            "goal": goal,
            "usage_source": "estimated_from_native_payload",
            "resumable": thread_id not in self.loaded,
            "next_action": "answer_agent_request"
            if requests
            else "next_model_request"
            if pending or thread["status"] == "active"
            else "inspect_result_or_resume",
        }

    async def next_request(self, thread_id=None, known_request_id=None, wait_ms=1000):
        if not 0 <= wait_ms <= 5000:
            raise ValueError("wait_ms must be between 0 and 5000")
        if thread_id:
            self.store.thread(thread_id)
        deadline = asyncio.get_running_loop().time() + wait_ms / 1000
        while True:
            pending = self.store.pending(thread_id)
            if pending:
                return self.relay.packet(pending[0]["id"], known_request_id)
            if asyncio.get_running_loop().time() >= deadline:
                return {
                    "status": "no_pending_request",
                    "thread_id": thread_id,
                    "next_action": "get_agent_status",
                }
            await asyncio.sleep(0.05)

    async def command(self, command, thread_id=None):
        verb, _, argument = command.strip().partition(" ")
        if (
            verb == "/goal"
            and argument
            and argument not in ("pause", "resume", "clear")
        ):
            if thread_id is None:
                return await self.create(argument, goal=True)
            await self.ensure_loaded(thread_id)
            result = await self.client.call(
                "thread/goal/set",
                {
                    "threadId": thread_id,
                    "objective": argument,
                    "status": "active",
                    "origin": "user",
                },
            )
            await self.client.call(
                "turn/start",
                {"threadId": thread_id, "input": [{"type": "text", "text": argument}]},
            )
            return result
        if not thread_id:
            raise ValueError(
                "This command requires thread_id; use list_agent_tasks to find it"
            )
        if verb == "/status":
            return await self.status(thread_id)
        await self.ensure_loaded(thread_id)
        if verb == "/compact":
            return await self.client.call(
                "thread/compact/start", {"threadId": thread_id}
            )
        if verb == "/goal" and not argument:
            return await self.client.call("thread/goal/get", {"threadId": thread_id})
        if verb == "/goal" and argument == "clear":
            return await self.client.call(
                "thread/goal/clear", {"threadId": thread_id, "origin": "user"}
            )
        if verb == "/pause" or (verb == "/goal" and argument == "pause"):
            state = self.store.thread(thread_id)
            if state["turn_id"]:
                await self.client.call(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": state["turn_id"]},
                )
            current = (
                await self.client.call("thread/goal/get", {"threadId": thread_id})
            ).get("goal")
            if current:
                await self.client.call(
                    "thread/goal/set",
                    {"threadId": thread_id, "status": "paused", "origin": "user"},
                )
            return {"thread_id": thread_id, "paused": True}
        if verb == "/resume" or (verb == "/goal" and argument == "resume"):
            current = (
                await self.client.call("thread/goal/get", {"threadId": thread_id})
            ).get("goal")
            if current:
                return await self.client.call(
                    "thread/goal/set",
                    {"threadId": thread_id, "status": "active", "origin": "user"},
                )
            return await self.client.call(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": argument
                            or "Continue the saved task using its current files and test results.",
                        }
                    ],
                },
            )
        if verb == "/fork":
            result = await self.client.call(
                "thread/fork", {"threadId": thread_id, "excludeTurns": True}
            )
            fork = result["thread"]
            self.store.register_thread(
                fork["id"], fork.get("cwd", self.store.thread(thread_id)["cwd"])
            )
            self.loaded.add(fork["id"])
            return {"thread_id": fork["id"], "forked_from": thread_id}
        raise ValueError(
            "Supported commands: /goal, /compact, /status, /pause, /resume, /fork"
        )

    async def close(self):
        await self.client.close()
        await self.relay.close()
        self.loaded.clear()
