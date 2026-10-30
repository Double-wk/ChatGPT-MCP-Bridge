import platform
import struct
import sys
import sysconfig
from pathlib import Path
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        macho_cpu = None
        for name in ["chatgpt-mcp-bridge", "chatgpt-mcp-bridge-runtime"]:
            path = Path(self.root) / "src/chatgpt_mcp_bridge/bin" / name
            with path.open("rb") as stream:
                magic = stream.read(4)
                if sys.platform.startswith("linux"):
                    valid = magic == b"\x7fELF"
                elif sys.platform == "darwin":
                    valid = magic == b"\xcf\xfa\xed\xfe"
                    if valid:
                        cpu = struct.unpack("<I", stream.read(4))[0]
                        if macho_cpu is not None and cpu != macho_cpu:
                            raise RuntimeError("Native programs have different architectures")
                        macho_cpu = cpu
                else:
                    valid = False
                if not valid:
                    raise RuntimeError("Build the native program first: " + name)
        if sys.platform.startswith("linux"):
            for name in ["codex-linux-sandbox", "codex-resources/bwrap"]:
                path = "src/chatgpt_mcp_bridge/bin/" + name
                build_data["force_include"][str(Path(self.root) / path)] = (
                    "chatgpt_mcp_bridge/bin/" + name
                )
        if sys.platform == "darwin":
            arch = {0x0100000C: "arm64", 0x01000007: "x86_64"}.get(macho_cpu)
            if arch is None:
                raise RuntimeError("Unsupported macOS native architecture")
            major, minor, *_ = platform.mac_ver()[0].split(".")
            wheel_platform = f"macosx_{major}_{minor}_{arch}"
        else:
            wheel_platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{wheel_platform}"
