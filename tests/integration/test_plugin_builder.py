import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_plugin", ROOT / "scripts/build_plugin.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class PluginBuilderTests(unittest.TestCase):
    def test_desktop_zip(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "desktop.zip"
            module.build(dest, None)
            with zipfile.ZipFile(dest) as z:
                names = z.namelist()
                self.assertTrue(all(n.startswith("chatgpt-mcp-bridge/") for n in names))
                conf = json.loads(z.read("chatgpt-mcp-bridge/mcp.json"))
                self.assertEqual(conf["mcpServers"]["codex_local"]["type"], "stdio")
                self.assertIn(
                    "chatgpt-mcp-bridge/skills/chatgpt-mcp-bridge/SKILL.md", names
                )
                self.assertIn(
                    "chatgpt-mcp-bridge/skills/chatgpt-mcp-bridge/references/responses.md",
                    names,
                )

    def test_web_zip(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "web.zip"
            module.build(dest, "https://mcp.example.org/mcp")
            with zipfile.ZipFile(dest) as z:
                portable = json.loads(z.read("chatgpt-mcp-bridge/mcp.json"))
                compat = json.loads(z.read("chatgpt-mcp-bridge/.mcp.json"))
                self.assertEqual(
                    portable["mcpServers"]["codex_local"]["type"], "streamable-http"
                )
                self.assertEqual(
                    portable["mcpServers"]["codex_local"]["url"],
                    "https://mcp.example.org/mcp",
                )
                self.assertEqual(compat["mcpServers"]["codex_local"]["type"], "http")
                self.assertEqual(
                    portable["$schema"],
                    "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                )

    def test_reject_insecure_url(self):
        for url in (
            "http://mcp.example.org/mcp",
            "https://user:secret@mcp.example.org/mcp",
            "https://mcp.example.org/mcp?token=secret",
            "https://mcp.example.org/mcp#fragment",
            "https://localhost/mcp",
        ):
            with self.subTest(url=url):
                with tempfile.TemporaryDirectory() as td, self.assertRaises(ValueError):
                    module.build(Path(td) / "bad.zip", url)


if __name__ == "__main__":
    unittest.main()
