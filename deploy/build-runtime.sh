#!/usr/bin/env bash
# Build the project executables from the vendored Codex source.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "$ROOT/.build-tools/cargo/bin/cargo" ]]; then
  export CARGO_HOME="$ROOT/.build-tools/cargo"
  export RUSTUP_HOME="$ROOT/.build-tools/rustup"
  export PATH="$CARGO_HOME/bin:$PATH"
fi
if [[ -f "$ROOT/.build-tools/pkgconfig/libcap.pc" ]]; then
  export PKG_CONFIG_PATH="$ROOT/.build-tools/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
fi
command -v cargo >/dev/null || { echo 'Rust 1.99.1 is required to build the bundled Codex source.' >&2; exit 1; }
[[ -f "$ROOT/vendor/codex/UPSTREAM.json" ]] || { echo 'Missing vendored Codex source snapshot' >&2; exit 1; }
case "$(uname -s)" in
  Linux) pkg-config --exists libcap || { echo 'Install libcap development headers/pkg-config, or set PKG_CONFIG_PATH to a local libcap build.' >&2; exit 1; } ;;
  Darwin) ;;
  *) echo 'Only Linux and macOS builds are supported.' >&2; exit 1 ;;
esac
export CARGO_TARGET_DIR="${MCP_BRIDGE_BUILD_TARGET_DIR:-$ROOT/.build/target}"
cargo +1.99.1 build --locked --release --manifest-path "$ROOT/runtime/Cargo.toml" -j "${MCP_BRIDGE_BUILD_JOBS:-4}"
install -d "$ROOT/src/chatgpt_mcp_bridge/bin"
install -m 755 "$CARGO_TARGET_DIR/release/chatgpt-mcp-bridge-runtime" "$ROOT/src/chatgpt_mcp_bridge/bin/chatgpt-mcp-bridge-runtime"
if [[ "$(uname -s)" == Linux ]]; then
  ln -sfn chatgpt-mcp-bridge-runtime "$ROOT/src/chatgpt_mcp_bridge/bin/codex-linux-sandbox"
  CODEX_BWRAP_SOURCE_DIR="$ROOT/vendor/codex/codex-rs/vendor/bubblewrap" cargo +1.99.1 build --locked --release --manifest-path "$ROOT/runtime/bwrap/Cargo.toml"
  install -d "$ROOT/src/chatgpt_mcp_bridge/bin/codex-resources"
  install -m 755 "$CARGO_TARGET_DIR/release/bwrap" "$ROOT/src/chatgpt_mcp_bridge/bin/codex-resources/bwrap"
fi
"$ROOT/src/chatgpt_mcp_bridge/bin/chatgpt-mcp-bridge-runtime" --version

cargo +1.99.1 build --locked --release --manifest-path "$ROOT/runtime/agent/Cargo.toml" -j "${MCP_BRIDGE_BUILD_JOBS:-4}"
install -m 755 "$CARGO_TARGET_DIR/release/chatgpt-mcp-bridge" "$ROOT/src/chatgpt_mcp_bridge/bin/chatgpt-mcp-bridge"
