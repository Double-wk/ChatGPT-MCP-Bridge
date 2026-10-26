#!/usr/bin/env python3
"""Create a portable plugin ZIP for desktop stdio or an already hosted HTTPS MCP endpoint.

No auth secrets are bundled. For ChatGPT web, prefer "Add custom MCP server" when
bundled MCP declarations are treated as desktop-only by the host.
"""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugin"
PREFIX = "chatgpt-mcp-bridge/"
PATHS = (
    "plugin.json",
    ".codex-plugin/plugin.json",
)


def check_url(raw: str) -> str:
    url = urlsplit(raw)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.port == 0
            or url.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("--web-url requires a publicly reachable HTTPS MCP endpoint without credentials/query/fragment")
    return raw


def build(output: Path, web_url: str | None) -> None:
    if web_url:
        web_url = check_url(web_url)
        portable = {"type": "streamable-http", "url": web_url}
        legacy = {"type": "http", "url": web_url}
    else:
        portable = {"type": "stdio", "command": "chatgpt-mcp-bridge"}
        legacy = dict(portable)

    portable_mcp = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {"codex_local": portable},
    }
    legacy_mcp = {"mcpServers": {"codex_local": legacy}}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.resolve().is_relative_to(SOURCE.resolve()):
        raise ValueError("Output ZIP must be outside the plugin template")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        paths = list(PATHS)
        for path in sorted((SOURCE / "skills").rglob("*")):
            if path.is_file():
                if not path.resolve().is_relative_to(SOURCE.resolve()):
                    raise ValueError("Skill resources must remain inside the plugin template")
                paths.append(path.relative_to(SOURCE).as_posix())
        for relative in paths:
            archive.write(SOURCE / relative, PREFIX + relative)
        for relative, data in (("mcp.json", portable_mcp), (".mcp.json", legacy_mcp)):
            archive.writestr(PREFIX + relative, json.dumps(data, indent=2) + "\n")
    print(f"Wrote {output} ({'web HTTPS reference' if web_url else 'local stdio'})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-url", help="Already-deployed, authenticated HTTPS MCP endpoint")
    parser.add_argument("--output", type=Path, required=True, help="Destination ZIP filename")
    args = parser.parse_args()
    build(args.output, args.web_url)


if __name__ == "__main__":
    main()
