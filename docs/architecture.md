# Architecture

ChatGPT webpage → OAuth Gateway → MCP → native Codex App Server → execution tools.
The native engine sends model requests to a private loopback Responses relay.
The webpage reads requests and returns structured decisions over MCP.

## Repository layout

```text
src/chatgpt_mcp_bridge/  Python bridge, tools and OAuth Gateway
runtime/                Rust entry points and locked dependency graphs
  src/                  Standalone execution helper
  agent/                Native Codex App Server entry point
  bwrap/                Linux-only Bubblewrap build
vendor/                 Pinned upstream sources and their licenses
patches/                Recorded local changes to upstream sources
deploy/                 Installation, launchers, health checks and service templates
scripts/                Plugin ZIP builder
plugin/                 Plugin manifests and workflow instructions
tests/                  Unit tests and integration tests
docs/                   Architecture, route operation, deployment status and version policy
```

The three Rust targets serve different roles: the agent manages development
threads, the execution helper handles direct file/terminal calls, and Bubblewrap
provides the Linux sandbox. They share pinned upstream dependencies; macOS does
not build the Linux-only helper. `src/chatgpt_mcp_bridge/runtime/` is the Python
client for the native execution helper, not another copy of the Rust engine.

## Python modules

| Module | Responsibility |
| --- | --- |
| agent/ | Native thread APIs, commands, goals and resume/fork |
| model_bridge/ | Persistent request queue, Responses/SSE adapter, context paging/deltas |
| runtime/ | Direct execution and shared lifecycle |
| tools/ | MCP schemas and public operations |
| transport/ | HTTP, bearer validation and liveness |
| gateway/ | OAuth and durable access tokens |
| security/ | Credential redaction |

Planning, tool loops, history compaction and goal continuation use native Codex
implementations. The bridge adapts model transport. Native history and webpage
history are separate. MCP cannot wake a closed or completed webpage turn; the
webpage must service pending requests. Usage is estimated from payload bytes for
native accounting/compaction, not webpage token usage or billing.

## Deployment entry points

- `deploy/install.sh` installs the project into an activated Conda environment on
  macOS or Linux and builds the native programs.
- `deploy/run-http.sh`, `deploy/run-stdio.sh` and `deploy/start-gateway.sh` launch
  the installed package with the selected transport.
- `deploy/install-macos-launchagents.py` installs macOS login services.
- `deploy/start-nohup.sh` and `deploy/chatgpt-mcp-bridge.service` are optional Linux
  launch alternatives. Customize the systemd template's paths before use.
- `deploy/doctor-mcp.py` checks authenticated discovery, execution and the native
  model-response cycle.
- `deploy/run-cloudflare-tunnel.sh` optionally launches a temporary tunnel using
  an externally installed cloudflared. Tailscale Funnel is the primary Mac route.

Use [README](../README.md) for installation, [Funnel operations](macos-funnel-deployment.md)
for the public route, and [deployment status](DEPLOYMENT_STATUS.md) for this Mac.
One-time migration scripts and unused deployment plans are not maintained as
installation alternatives.

## Source versus generated files

`vendor/` is build input, not a cache. Its source and licenses are needed for
reproducible native builds. Do not prune upstream files based only on directory size.

`.build/`, `.build-tools/`, `src/chatgpt_mcp_bridge/bin/`, `dist/`, logs and Python
caches are local generated files and are ignored by Git. Build caches and local
Rust tools can be much larger than project source; they avoid repeated native
compilation and toolchain downloads. They are not needed by the installed Conda
service. Persistent task state defaults to `~/.local/state/chatgpt-mcp-bridge`;
private OAuth configuration and tokens live outside the repository.

## Runtime and CI

The deployment is single-user; authorized clients share the OS account. Sandbox
and network settings are explicit configuration. This Mac's current permissions
are recorded in deployment status.

CI builds, tests and packages on Ubuntu 24.04 and macOS 15. Only the hosted Linux
test VM enables unprivileged namespaces before probing Bubblewrap; production
scripts do not change host kernel settings. Linux native build outputs are cached
after compilation so later Python/test fixes can reuse dependencies.
