"""Exercise the real Codex executor with an isolated home and workspace."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from chatgpt_mcp_bridge.runtime.executor import ExecutionRuntime as CodexBackend


@pytest.fixture
async def backend(tmp_path, monkeypatch):
    if not (
        Path(__file__).resolve().parents[1]
        / "src/chatgpt_mcp_bridge/bin/chatgpt-mcp-bridge-runtime"
    ).is_file():
        pytest.skip("Build the embedded Codex runtime before execution tests")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_MCP_WORKSPACE", str(workspace))
    monkeypatch.setenv("CODEX_MCP_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_MCP_NETWORK_ACCESS", "false")
    # These must not be forwarded to Codex or its children.
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-do-not-forward")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "test-secret-do-not-forward")
    instance = CodexBackend()
    try:
        yield instance
    finally:
        await instance.close_all()


async def test_no_login_and_no_gateway_secrets(backend):
    script = "import os,json; print(json.dumps({k:os.getenv(k) for k in ['OPENAI_API_KEY','GITHUB_CLIENT_SECRET','CODEX_HOME']}))"
    result = await backend.execute([sys.executable, "-c", script])
    assert result["exit_code"] == 0, json.dumps(result, ensure_ascii=False)
    env = json.loads(result["stdout"])
    assert env["OPENAI_API_KEY"] is None
    assert env["GITHUB_CLIENT_SECRET"] is None
    # Codex's child environment policy also strips its private state directory.
    assert env["CODEX_HOME"] is None
    assert not (backend.home / "auth.json").exists()


async def test_interactive_input_and_polling(backend):
    session = await backend.launch(
        [
            sys.executable,
            "-u",
            "-c",
            "print('ready',flush=True); print(input(),flush=True)",
        ],
        None,
        15,
    )
    first = await backend.poll(session, 500)
    assert first["running"]
    await backend.write(session, "hello-codex\n", True)
    output = first["stdout"]
    for _ in range(15):
        result = await backend.poll(session, 1000)
        output += result["stdout"]
        if not result["running"]:
            break
    assert result["exit_code"] == 0, json.dumps(result, ensure_ascii=False)
    assert "ready" in output and "hello-codex" in output


async def test_concurrent_commands_are_correlated(backend):
    results = await asyncio.gather(
        *(backend.execute(["/bin/echo", str(n)]) for n in range(4))
    )
    assert [r["stdout"].strip() for r in results] == ["0", "1", "2", "3"]


async def test_pty_session(backend):
    session = await backend.launch(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys;print(sys.stdin.isatty());print(input())",
        ],
        None,
        15,
        tty=True,
    )
    first = await backend.poll(session, 500)
    assert first["running"]
    await backend.write(session, "pty-input\n", False)
    result = await backend.poll(session, 5000)
    assert result["exit_code"] == 0, json.dumps(result, ensure_ascii=False)
    assert "True" in first["stdout"] + result["stdout"]
    assert "pty-input" in result["stdout"]


async def test_workspace_symlink_cannot_grant_outside_write(backend, tmp_path):
    (backend.workspace / "escape").symlink_to(tmp_path, target_is_directory=True)
    result = await backend.execute(
        [
            sys.executable,
            "-c",
            "from pathlib import Path;Path('escape/outside').write_text('bad')",
        ]
    )
    assert result["exit_code"] != 0
    assert not (tmp_path / "outside").exists()


async def test_sandbox_blocks_outside_and_readonly_writes(backend, tmp_path):
    outside = tmp_path / "outside.txt"
    result = await backend.execute(
        [
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('bad')",
            str(outside),
        ]
    )
    assert result["exit_code"] != 0
    assert not outside.exists()
    inside = backend.workspace / "readonly.txt"
    result = await backend.execute(
        [
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('bad')",
            str(inside),
        ],
        read_only=True,
    )
    assert result["exit_code"] != 0
    assert not inside.exists()


async def test_output_cap_and_nonzero_exit(backend):
    result = await backend.execute(
        [
            sys.executable,
            "-c",
            "import sys; print('x'*100000);sys.stderr.write('failure');sys.exit(7)",
        ]
    )
    assert result["exit_code"] == 7
    assert len(result["stdout"].encode()) <= backend.output_cap
    assert result["stderr"] == "failure"
    assert result["truncated"]


async def test_terminate_releases_process(backend):
    session = await backend.launch(
        [sys.executable, "-c", "import time; time.sleep(120)"], None, 180
    )
    await backend.poll(session, 500)
    await backend.terminate(session)
    result = await backend.poll(session, 5000)
    assert not result["running"]


async def test_timeout_does_not_leave_running_job(backend):
    result = await backend.execute(
        [sys.executable, "-c", "import time;time.sleep(120)"], timeout=1
    )
    assert result["exit_code"] == 124
    assert not result["running"]


async def test_closed_connection_fails_pending_without_replay(backend):
    session = await backend.launch(
        [sys.executable, "-c", "import time;time.sleep(120)"], None, 180
    )
    await backend.poll(session, 300)
    os.killpg(backend.process.pid, signal.SIGKILL)
    with pytest.raises(RuntimeError, match="connection closed"):
        for _ in range(10):
            await backend.poll(session, 1000)
    # Starting again creates a fresh connection and does not replay the job.
    result = await backend.execute(["/bin/echo", "recovered"])
    assert result["stdout"] == "recovered\n"


def test_reject_workspace_escape(backend):
    with pytest.raises(ValueError, match="inside"):
        backend.directory("..")


async def test_reject_invalid_requests_before_execution(backend):
    with pytest.raises(ValueError):
        await backend.launch([], None, 30)
    with pytest.raises(ValueError):
        await backend.launch(["echo"], None, 0)
    with pytest.raises(ValueError):
        await backend.poll("unknown", 0)
    assert backend.process is None


async def test_file_tools_and_codex_patch(backend, monkeypatch):
    import chatgpt_mcp_bridge.tools.execution as server
    from chatgpt_mcp_bridge.runtime import context

    monkeypatch.setattr(context, "executor", backend)
    result = json.loads(await server.write_file("hello.txt", "hello\n中文\n"))
    assert result["exit_code"] == 0, json.dumps(result, ensure_ascii=False)
    result = json.loads(await server.write_file("hello.txt", "wrong"))
    assert result["exit_code"] != 0
    result = json.loads(await server.read_file("hello.txt", start_line=2))
    assert "2: 中文" in result["stdout"]
    patch = "*** Begin Patch\n*** Update File: hello.txt\n@@\n-hello\n+patched\n*** End Patch"
    result = json.loads(await server.apply_patch(patch))
    assert result["exit_code"] == 0, json.dumps(result, ensure_ascii=False)
    assert (backend.workspace / "hello.txt").read_text() == "patched\n中文\n"
    escape = json.loads(await server.write_file("../escaped.txt", "bad"))
    assert escape["exit_code"] != 0
    assert not (backend.workspace.parent / "escaped.txt").exists()


async def test_mcp_tool_catalog():
    import chatgpt_mcp_bridge.tools.execution as server

    tools = await server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert names >= {
        "get_environment",
        "execute",
        "exec_command",
        "remote_ssh",
        "write_stdin",
        "terminate_process",
        "read_file",
        "write_file",
        "apply_patch",
    }
    for tool in tools:
        assert "server" not in tool.input_schema.get("properties", {})


async def test_remote_ssh_persistent_session(backend, tmp_path, monkeypatch):
    import chatgpt_mcp_bridge.tools.execution as server

    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text("#!/bin/sh\nprintf 'connected %s\\n' \"$*\"\nwhile IFS= read -r line; do printf 'reply:%s\\n' \"$line\"; done\n")
    fake_ssh.chmod(0o755)
    backend.full_access = True
    backend.network = True
    monkeypatch.setattr(server, "_get_backend", lambda: backend)
    monkeypatch.setattr(server.shutil, "which", lambda name: str(fake_ssh))
    result = json.loads(await server.remote_ssh.__wrapped__("user@test-host", 2222, "password", 500))
    session_id = result["session_id"]
    assert result["running"] and session_id
    output = result["stdout"]
    for _ in range(5):
        if "-p 2222 user@test-host" in output:
            break
        output += (await backend.poll(session_id, 500))["stdout"]
    assert "-p 2222 user@test-host" in output
    assert "BatchMode=no" in output
    assert backend.sessions[session_id].timer is None
    await backend.write(session_id, "first\n", False)
    first = await backend.poll(session_id, 500)
    assert "reply:first" in first["stdout"]
    await backend.write(session_id, "second\n", False)
    second = await backend.poll(session_id, 500)
    assert "reply:second" in second["stdout"]
    await backend.terminate(session_id)
    result = await backend.poll(session_id, 5000)
    assert not result["running"]


async def test_remote_ssh_rejects_options_and_requires_network(backend, monkeypatch):
    import chatgpt_mcp_bridge.tools.execution as server

    monkeypatch.setattr(server, "_get_backend", lambda: backend)
    with pytest.raises(ValueError, match="SSH alias"):
        await server.remote_ssh.__wrapped__("-oProxyCommand=bad")
    with pytest.raises(ValueError, match="port"):
        await server.remote_ssh.__wrapped__("test-host", 65536)
    with pytest.raises(ValueError, match="authentication"):
        await server.remote_ssh.__wrapped__("test-host", authentication="invalid")
    with pytest.raises(ValueError, match="requires"):
        await server.remote_ssh.__wrapped__("test-host")


async def test_remote_ssh_password_prompt(backend, tmp_path, monkeypatch):
    import chatgpt_mcp_bridge.tools.execution as server

    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        "#!/bin/sh\nstty -echo\nprintf 'Password: '\nIFS= read -r password\n"
        "stty echo\n[ \"$password\" = 'test-only' ] || exit 1\n"
        "printf 'authenticated\\n'\nread -r command\nprintf 'done:%s\\n' \"$command\"\n"
    )
    fake_ssh.chmod(0o755)
    backend.full_access = True
    backend.network = True
    monkeypatch.setattr(server, "_get_backend", lambda: backend)
    monkeypatch.setattr(server.shutil, "which", lambda name: str(fake_ssh))
    result = json.loads(await server.remote_ssh.__wrapped__("user@test-host", authentication="password", yield_time_ms=500))
    session_id = result["session_id"]
    output = result["stdout"]
    for _ in range(5):
        if "Password:" in output:
            break
        output += (await backend.poll(session_id, 500))["stdout"]
    assert "Password:" in output
    await backend.write(session_id, "test-only\n", False)
    output = (await backend.poll(session_id, 500))["stdout"]
    assert "authenticated" in output and "test-only" not in output
    await backend.write(session_id, "hello\n", False)
    output = await backend.poll(session_id, 5000)
    assert "done:hello" in output["stdout"]
