//! Project-owned entry point for the upstream Codex App Server and agent core.
use codex_app_server::{
    AppServerRuntimeOptions, AppServerTransport, PluginStartupTasks,
    RemoteControlStartupMode, run_main_with_transport_options,
};
use codex_arg0::arg0_dispatch_or_else;
use codex_config::LoaderOverrides;
use codex_protocol::protocol::SessionSource;
use codex_utils_cli::CliConfigOverrides;
use codex_websocket_auth::WebsocketAuthSettings;

fn main() -> anyhow::Result<()> {
    arg0_dispatch_or_else(|paths| async move {
        run_main_with_transport_options(
            paths,
            CliConfigOverrides::default(),
            LoaderOverrides::default(),
            false,
            false,
            AppServerTransport::Stdio,
            SessionSource::VSCode,
            WebsocketAuthSettings::default(),
            AppServerRuntimeOptions {
                plugin_startup_tasks: PluginStartupTasks::Skip,
                remote_control_startup_mode: RemoteControlStartupMode::DisabledEphemeral,
                ..Default::default()
            },
        )
        .await
        .map(|_| ())
        .map_err(|error| anyhow::anyhow!("{error}"))
    })
}
