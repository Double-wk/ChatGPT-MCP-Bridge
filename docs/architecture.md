# Architecture

ChatGPT webpage -> MCP -> native Codex App Server -> execution tools.
The native engine sends model requests to a private loopback Responses relay.
The webpage reads requests and returns structured decisions over MCP.

| Module | Responsibility |
| --- | --- |
| agent/ | Native thread APIs, commands, goals and resume/fork |
| model_bridge/ | Persistent request queue, Responses/SSE adapter, context paging/deltas |
| runtime/ | Direct execution and shared lifecycle |
| tools/ | MCP schemas and public operations |
| transport/ | HTTP, bearer validation and liveness |
| gateway/ | OAuth and durable access tokens |
| security/ | Credential redaction |
| runtime/agent (repository root) | Native Codex App Server entry point |
| plugin/ | Webpage workflow instructions and manifests |
| vendor/ | Pinned upstream sources and licenses |

Planning, tool loops, history compaction and goal continuation are native Codex
implementations. The bridge adapts model transport rather than replacing the
planner. Native history and webpage history are separate. MCP cannot wake a
closed or completed webpage turn; the webpage must service pending requests.
Usage is estimated from payload bytes for native accounting/compaction and is not
webpage token usage or billing. No model-quality or throughput parity is asserted.

The old SSH package, inventory and tests are retired. Root gateway and integration
tests move into the package and tests/integration. Build caches, binaries and logs
are ignored; persistent state defaults to ~/.local/state/chatgpt-mcp-bridge.
The deployment is single-user, and authorized clients share the OS account.

CI runs on Ubuntu 24.04 and enables unprivileged namespaces only inside the
hosted test VM before probing Bubblewrap. Native workspace/network sandboxing
remains enabled. Production setup scripts do not change the host kernel settings.
Build outputs are cached after compilation so later Python/test fixes can reuse
native dependencies. Execution assertions include full test-result diagnostics.
