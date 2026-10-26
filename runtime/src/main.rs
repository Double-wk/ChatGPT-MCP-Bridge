//! Embedded Codex executor entry point. No CLI login, model agent or App Server.
//! The upstream execution, PTY, sandbox and patch implementations are linked
//! from the pinned source snapshot in vendor/codex.

use codex_arg0::arg0_dispatch_or_else;
use codex_exec_server::ExecServerRuntimeOptions;
use codex_file_system::FileSystemSandboxContext;
use codex_http_client::{HttpClientFactory, OutboundProxyPolicy};
use codex_protocol::protocol::SandboxPolicy;
use codex_utils_absolute_path::AbsolutePathBuf;

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--version") {
        println!("chatgpt-mcp-bridge-runtime 0.3.0 (Codex rust-v0.164.0)");
        return Ok(());
    }
    if args.get(1).map(String::as_str) == Some("--sandbox-context") {
        anyhow::ensure!(args.len() == 5, "expected workspace, read_only and network_access");
        let workspace = AbsolutePathBuf::from_absolute_path(&args[2])?;
        let read_only = args[3].parse::<bool>()?;
        let network_access = args[4].parse::<bool>()?;
        let policy: SandboxPolicy = serde_json::from_value(if read_only {
            serde_json::json!({"type": "read-only", "network_access": network_access})
        } else {
            serde_json::json!({"type": "workspace-write", "writable_roots": [&args[2]],
                "network_access": network_access, "exclude_slash_tmp": true,
                "exclude_tmpdir_env_var": true})
        })?;
        let context = FileSystemSandboxContext::from_legacy_sandbox_policy(policy, workspace.into())?;
        println!("{}", serde_json::to_string(&context)?);
        return Ok(());
    }
    // Includes upstream re-exec dispatch for PTY, sandbox and apply_patch.
    // Does not link the Codex CLI, login, core agent, or App Server crates.
    arg0_dispatch_or_else(|paths| async move {
        let runtime = ExecServerRuntimeOptions::from_optional_paths(
            paths.codex_self_exe,
            paths.codex_linux_sandbox_exe,
        )?;
        codex_exec_server::run_main(
            "stdio",
            runtime,
            HttpClientFactory::new(OutboundProxyPolicy::ReqwestDefault),
        )
        .await
        .map_err(|error| anyhow::anyhow!("{error}"))
    })
}
