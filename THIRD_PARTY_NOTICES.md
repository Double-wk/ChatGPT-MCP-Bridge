# Third-party source

The bridge retains MPL-2.0 licensing for its original SSH MCP-derived transport
and new integration code. Vendored components retain their own licenses.

- `vendor/codex`: OpenAI Codex, Apache-2.0. Upstream tag `rust-v0.164.0`,
  commit `6aaac694803004a63c6f393d153c96d22521a6b9`. Original `LICENSE` and
  `NOTICE` are included. The local EOF extension is recorded separately in
  `patches/codex-close-stdin.patch`.
- `vendor/codex-deps/tokio-tungstenite`: the upstream fork/revision selected
  by Codex; original license retained.
- `vendor/codex-deps/tungstenite`: the upstream fork/revision selected by
  Codex; original license retained.
- `vendor/codex-deps/mxc`: Microsoft MXC SDK source needed for cross-platform
  Cargo dependency resolution; original licenses retained. It is not built
  into the Linux runtime.

The main project program directly links the upstream Codex App Server, core,
goals and compaction/history code. The standalone execution helper remains
available for explicit terminal/file operations. Additional pinned sources in
vendor/codex-deps/rmcp and vendor/codex-deps/nucleo retain their own licenses.
The local capability profile is derived from the upstream Codex model catalog.
See runtime/agent/Cargo.lock and vendor/codex/UPSTREAM.json for provenance.
