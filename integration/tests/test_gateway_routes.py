import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from gateway.app import app


class GatewayRoutingTests(unittest.TestCase):
    def test_health_and_discovery(self):
        with TestClient(app) as client:
            for path in (
                "/healthz",
                "/.well-known/oauth-authorization-server",
                "/.well-known/oauth-protected-resource/mcp",
                "/mcp/.well-known/oauth-protected-resource",
                "/.well-known/openid-configuration",
            ):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, path)

    def test_unauthenticated_mcp_fails_closed_without_token_file(self):
        with patch("gateway.app.EXT") as token_path:
            token_path.is_file.return_value = False
            with TestClient(app) as client:
                response = client.get("/mcp")
                self.assertEqual(response.status_code, 401)

    def test_incomplete_auth_request_is_rejected(self):
        with TestClient(app) as client:
            response = client.get("/oauth/authorize", params={
                "client_id": "chatgpt-mcp-bridge",
                "response_type": "code",
            })
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
