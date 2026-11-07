"""OAuth account linking, DCR persistence and resource isolation."""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
from starlette.testclient import TestClient

from chatgpt_mcp_bridge.gateway import app as gateway
from chatgpt_mcp_bridge.gateway.token_store import TokenStore, digest


class OAuthResourceRegistrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = TokenStore(Path(tmp.name) / "oauth.db")
        context = patch.object(gateway, "TOKEN_STORE", self.store)
        context.start()
        self.addCleanup(context.stop)
        env = patch.dict(
            gateway.os.environ,
            {
                "GATEWAY_PUBLIC_BASE": "https://mcp.example.com",
                "GITHUB_ALLOWED_IDS": "42",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        gateway.codes.clear()
        gateway.oauth_states.clear()
        self.resource = "https://mcp.example.com/mcp"
        self.redirect = "https://chatgpt.com/connector/oauth/test_callback"
        self.verifier = "v" * 43

    def register(self, browser):
        response = browser.post("/register", json={"redirect_uris": [self.redirect]})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["client_id"]

    def authorization(self, client_id):
        return {
            "client_id": client_id,
            "redirect_uri": self.redirect,
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": gateway.challenge(self.verifier),
            "state": "chatgpt-state",
            "resource": self.resource,
        }

    def test_real_route_sequence_with_mocked_github_identity(self):
        async_client = httpx.AsyncClient

        def github(request):
            if (
                request.url.host == "github.com"
                and request.url.path == "/login/oauth/access_token"
            ):
                return httpx.Response(200, json={"access_token": "fake-github-token"})
            if request.url.host == "api.github.com" and request.url.path == "/user":
                return httpx.Response(200, json={"id": 42})
            raise AssertionError(str(request.url))

        def github_client(**kwargs):
            return async_client(transport=httpx.MockTransport(github), **kwargs)

        with TestClient(gateway.app) as browser:
            client_id = self.register(browser)
            restored = TokenStore(self.store.path)
            self.assertTrue(restored.known_client(client_id))
            self.assertTrue(restored.client_redirect_allowed(client_id, self.redirect))
            start = browser.get(
                "/oauth/authorize",
                params=self.authorization(client_id),
                follow_redirects=False,
            )
            self.assertEqual(start.status_code, 302, start.text)
            upstream = urlparse(start.headers["location"])
            self.assertEqual(upstream.hostname, "github.com")
            state = parse_qs(upstream.query)["state"][0]
            self.assertEqual(gateway.oauth_states[state]["resource"], self.resource)
            with patch.object(gateway.httpx, "AsyncClient", github_client):
                callback = browser.get(
                    "/oauth/github/callback",
                    params={"code": "github-code", "state": state},
                    follow_redirects=False,
                )
            self.assertEqual(callback.status_code, 302, callback.text)
            result = parse_qs(urlparse(callback.headers["location"]).query)
            self.assertEqual(result["state"], ["chatgpt-state"])
            code = result["code"][0]
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "redirect_uri": self.redirect,
                "code_verifier": self.verifier,
                "resource": self.resource,
            }
            wrong = browser.post(
                "/oauth/token", data=dict(data, resource="https://other.example/mcp")
            )
            self.assertEqual(wrong.json()["error"], "invalid_target")
            self.assertIn(code, gateway.codes)
            exchanged = browser.post("/oauth/token", data=data)
            self.assertEqual(exchanged.status_code, 200, exchanged.text)
            pair = exchanged.json()
            self.assertTrue(self.store.authorized(pair["access_token"], self.resource))
            self.assertFalse(
                self.store.authorized(pair["access_token"], "https://other.example/mcp")
            )
            self.assertEqual(browser.post("/oauth/token", data=data).status_code, 400)
            refresh = {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": pair["refresh_token"],
                "resource": self.resource,
            }
            self.assertEqual(
                browser.post(
                    "/oauth/token",
                    data=dict(refresh, resource="https://other.example/mcp"),
                ).status_code,
                400,
            )
            rotated = browser.post("/oauth/token", data=refresh)
            self.assertEqual(rotated.status_code, 200, rotated.text)
            self.assertTrue(
                restored.authorized(rotated.json()["access_token"], self.resource)
            )
            self.assertEqual(
                browser.post("/oauth/token", data=refresh).status_code, 400
            )

    def test_registration_rejects_malformed_and_untrusted_metadata(self):
        with TestClient(gateway.app) as browser:
            for data in [
                [],
                {},
                {"redirect_uris": []},
                {"redirect_uris": [None]},
                {"redirect_uris": ["https://attacker.example/callback"]},
                {
                    "redirect_uris": [self.redirect],
                    "grant_types": [["authorization_code"]],
                },
                {
                    "redirect_uris": [self.redirect],
                    "token_endpoint_auth_method": "client_secret_post",
                },
            ]:
                self.assertEqual(browser.post("/register", json=data).status_code, 400)
            self.assertEqual(
                browser.post("/register", content=b"not-json").status_code, 400
            )

    def test_registration_limits_each_client_to_its_exact_redirect(self):
        with TestClient(gateway.app) as browser:
            client_id = self.register(browser)
            params = self.authorization(client_id)
            params["redirect_uri"] = gateway.CHATGPT_STABLE_REDIRECT
            self.assertEqual(
                browser.get("/oauth/authorize", params=params).status_code, 400
            )
            params = self.authorization("unregistered-client")
            self.assertEqual(
                browser.get("/oauth/authorize", params=params).status_code, 400
            )

    def test_authorization_requires_one_correct_resource_and_pkce_s256(self):
        with TestClient(gateway.app) as browser:
            params = self.authorization(gateway.CHATGPT_CLIENT)
            for invalid in [
                dict(params, resource="https://other.example/mcp"),
                dict(params, code_challenge_method="plain"),
                dict(params, code_challenge="invalid"),
            ]:
                self.assertEqual(
                    browser.get("/oauth/authorize", params=invalid).status_code, 400
                )
            params.pop("resource")
            self.assertEqual(
                browser.get("/oauth/authorize", params=params).status_code, 400
            )

    def test_proxy_rejects_other_resource_tokens_and_advertises_discovery(self):
        pair = self.store.issue(gateway.CHATGPT_CLIENT, "https://other.example/mcp")
        with TestClient(gateway.app) as browser:
            response = browser.get(
                "/mcp", headers={"Authorization": "Bearer " + pair["access_token"]}
            )
            self.assertEqual(response.status_code, 401)
            self.assertIn(
                'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"',
                response.headers["www-authenticate"],
            )
            self.assertEqual(
                browser.get("/.well-known/oauth-authorization-server").json()[
                    "registration_endpoint"
                ],
                "https://mcp.example.com/register",
            )

    def test_legacy_database_migration_preserves_records_without_accepting_wrong_audience(
        self,
    ):
        path = self.store.path.parent / "legacy.db"
        with sqlite3.connect(path) as db:
            for table in ["access_tokens", "refresh_tokens"]:
                db.execute(
                    f"CREATE TABLE {table} (digest TEXT PRIMARY KEY, client_id TEXT NOT NULL, expires INTEGER NOT NULL)"
                )
            db.execute(
                "INSERT INTO access_tokens VALUES (?, ?, ?)",
                (digest("legacy-token"), gateway.CHATGPT_CLIENT, int(time.time()) + 60),
            )
        path.chmod(0o600)
        restored = TokenStore(path)
        self.assertTrue(restored.authorized("legacy-token"))
        self.assertFalse(restored.authorized("legacy-token", self.resource))
        pair = restored.issue(gateway.CHATGPT_CLIENT, self.resource)
        self.assertTrue(restored.authorized(pair["access_token"], self.resource))


if __name__ == "__main__":
    unittest.main()
