import sysconfig
from pathlib import Path
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        for name in ["chatgpt-mcp-bridge", "chatgpt-mcp-bridge-runtime"]:
            path = Path(self.root) / "src/chatgpt_mcp_bridge/bin" / name
            with path.open("rb") as stream:
                if stream.read(4) != bytes([127, 69, 76, 70]):
                    raise RuntimeError("Build the native program first: " + name)
        platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{platform}"
