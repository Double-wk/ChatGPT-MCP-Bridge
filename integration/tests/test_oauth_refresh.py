"""Offline end-to-end OAuth refresh and persistence tests."""
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from gateway import app as gateway
from gateway.token_store import TokenStore, digest


class OAuthRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TokenStore(Path(self.tmp.name) / "oauth.db")
        self.ctx = patch.object(gateway, "TOKEN_STORE", self.store)
        self.ctx.start()
        self.addCleanup(self.ctx.stop)
        gateway.codes.clear()

    def test_discovery_advertises_refresh_and_offline_access(self):
        with TestClient(gateway.app) as client:
            data = client.get("/.well-known/oauth-authorization-server").json()
        self.assertIn("refresh_token", data["grant_types_supported"])
        self.assertIn("offline_access", data["scopes_supported"])
        self.assertEqual(data["token_endpoint_auth_methods_supported"], ["none"])

    def test_authorization_code_refresh_replay_and_persistence(self):
        verifier = "test-pkce-verifier"
        code = "test-once-only-authorization-code"
        client_id = gateway.CHATGPT_CLIENT
        redirect = "https://chatgpt.com/connector/oauth/test"
        gateway.codes[code] = {
            "client_id": client_id, "redirect_uri": redirect,
            "challenge": gateway.challenge(verifier), "expires": 99999999999,
        }
        with TestClient(gateway.app) as browser:
            data = {"grant_type": "authorization_code",
                    "code": code, "code_verifier": verifier,
                    "redirect_uri": redirect, "client_id": client_id}
            first = browser.post("/oauth/token", data=data)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.headers["cache-control"], "no-store")
            initial = first.json()
            self.assertEqual(initial["expires_in"], 3600)
            self.assertEqual(initial["scope"], "mcp offline_access")
            self.assertTrue(self.store.authorized(initial["access_token"]))

            self.assertEqual(browser.post("/oauth/token", data=data).status_code, 400)
            forged = browser.post("/oauth/token", data={
                "grant_type": "refresh_token",
                "client_id": "other-client",
                "refresh_token": initial["refresh_token"],
            })
            self.assertEqual(forged.status_code, 401)

            rotated = browser.post("/oauth/token", data={
                "grant_type": "refresh_token", "client_id": client_id,
                "refresh_token": initial["refresh_token"],
            })
            self.assertEqual(rotated.status_code, 200, rotated.text)
            second = rotated.json()
            self.assertNotEqual(initial["access_token"], second["access_token"])
            self.assertNotEqual(initial["refresh_token"], second["refresh_token"])
            self.assertTrue(self.store.authorized(second["access_token"]))

            replay = browser.post("/oauth/token", data={
                "grant_type": "refresh_token", "client_id": client_id,
                "refresh_token": initial["refresh_token"],
            })
            self.assertEqual(replay.status_code, 400)
            self.assertEqual(replay.json()["error"], "invalid_grant")

        # The gateway process can restart while its token database survives.
        restored = TokenStore(self.store.path)
        self.assertTrue(restored.authorized(second["access_token"]))
        self.assertIsNotNone(restored.refresh(second["refresh_token"], client_id))
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        raw = self.store.path.read_bytes()
        for secret in [initial["access_token"], initial["refresh_token"],
                       second["access_token"], second["refresh_token"]]:
            self.assertNotIn(secret.encode(), raw)
        self.assertTrue(digest(second["access_token"]).encode() in raw)

    def test_client_redirect_and_pkce_checks_do_not_consume_code(self):
        verifier = "pkce-test"
        code = "token-validation-code"
        gateway.codes[code] = {
            "client_id": gateway.CHATGPT_CLIENT,
            "redirect_uri": gateway.CHATGPT_STABLE_REDIRECT,
            "challenge": gateway.challenge(verifier),
            "expires": 99999999999,
        }
        with TestClient(gateway.app) as browser:
            base = {
                "grant_type": "authorization_code", "code": code,
                "client_id": gateway.CHATGPT_CLIENT,
                "redirect_uri": gateway.CHATGPT_STABLE_REDIRECT,
                "code_verifier": verifier,
            }
            for wrong in [
                dict(base, code_verifier="bad"),
                dict(base, redirect_uri="https://attacker.example/"),
                dict(base, client_id="attacker-client"),
            ]:
                self.assertEqual(browser.post("/oauth/token", data=wrong).status_code, 400)
            self.assertEqual(browser.post("/oauth/token", data=base).status_code, 200)

    def test_expired_access_and_refresh(self):
        pair = self.store.issue(gateway.CHATGPT_CLIENT)
        now = __import__("time").time()
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE access_tokens SET expires = ?", (int(now)-1,))
            db.execute("UPDATE refresh_tokens SET expires = ?", (int(now)-1,))
        self.assertFalse(self.store.authorized(pair["access_token"]))
        self.assertIsNone(self.store.refresh(pair["refresh_token"], gateway.CHATGPT_CLIENT))

    def test_missing_or_wrong_grant_type(self):
        with TestClient(gateway.app) as browser:
            self.assertEqual(browser.post("/oauth/token", data={}).status_code, 400)
            self.assertEqual(browser.post("/oauth/token", data={
                "grant_type": "refresh_token", "client_id": gateway.CHATGPT_CLIENT,
                "refresh_token": "fake",
            }).json()["error"], "invalid_grant")


if __name__ == "__main__":
    unittest.main()
