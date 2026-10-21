"""Regression tests for streaming MCP proxy connections (no real secrets or SSH)."""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from starlette.requests import Request
from starlette.testclient import TestClient

from gateway import app as gateway_module


class FakeUpstream:
    def __init__(self, status=200, chunks=(b"data: first\n\n", b"data: second\n\n")):
        self.status_code = status
        self.headers = {
            "content-type": "text/event-stream",
            "mcp-session-id": "session-123",
            "cache-control": "no-cache",
        }
        self.chunks = chunks
        self.closed = False

    async def aiter_raw(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class FakeHTTPClient:
    def __init__(self, upstream=None, error=None):
        self.upstream = upstream if upstream is not None else FakeUpstream()
        self.error = error
        self.closed = False
        self.outgoing = None
        self.stream = None

    def build_request(self, method, url, **kwargs):
        return {"method": method, "url": url, **kwargs}

    async def send(self, outgoing, stream=False):
        self.outgoing = outgoing
        self.stream = stream
        if self.error is not None:
            raise self.error
        return self.upstream

    async def aclose(self):
        self.closed = True


class GatewayStreamingTests(unittest.TestCase):
    def _authenticated(self, fake):
        return (
            patch("gateway.app.valid_gateway_auth", return_value=True),
            patch("gateway.app.read", return_value="test-internal-token"),
            patch("gateway.app.httpx.AsyncClient", return_value=fake),
        )

    def test_sse_streaming_and_session_headers(self):
        fake = FakeHTTPClient()
        a, b, c = self._authenticated(fake)
        with a, b, c, TestClient(gateway_module.app) as browser:
            r = browser.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer externally-issued-secret",
                    "Accept": "text/event-stream",
                    "Mcp-Session-Id": "incoming-session",
                },
                json={"jsonrpc": "2.0", "method": "initialize"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"data: first\n\ndata: second\n\n")
        self.assertEqual(r.headers["mcp-session-id"], "session-123")
        self.assertEqual(fake.stream, True)
        self.assertEqual(fake.outgoing["method"], "POST")
        self.assertEqual(fake.outgoing["headers"]["authorization"], "Bearer test-internal-token")
        self.assertEqual(fake.outgoing["headers"]["mcp-session-id"], "incoming-session")
        self.assertTrue(fake.upstream.closed)
        self.assertTrue(fake.closed)

    def test_upstream_503_is_passed_through_and_closed(self):
        fake = FakeHTTPClient(upstream=FakeUpstream(status=503, chunks=(b"busy",)))
        a, b, c = self._authenticated(fake)
        with a, b, c, TestClient(gateway_module.app) as browser:
            r = browser.get("/mcp")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.content, b"busy")
        self.assertTrue(fake.upstream.closed)
        self.assertTrue(fake.closed)

    def test_many_requests_do_not_leave_open_clients(self):
        fake_clients = []
        for _ in range(30):
            fake = FakeHTTPClient()
            fake_clients.append(fake)
            a, b, c = self._authenticated(fake)
            with a, b, c, TestClient(gateway_module.app) as browser:
                self.assertEqual(browser.get("/mcp").status_code, 200)
        self.assertTrue(all(x.closed and x.upstream.closed for x in fake_clients))

    def test_connect_failure_returns_502_and_releases_client(self):
        fake = FakeHTTPClient(error=httpx.ConnectError("network unavailable"))
        a, b, c = self._authenticated(fake)
        with a, b, c, TestClient(gateway_module.app) as browser:
            r = browser.get("/mcp")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json(), {"error": "upstream_unavailable"})
        self.assertTrue(fake.closed)

    def test_timeout_returns_504_and_releases_client(self):
        fake = FakeHTTPClient(error=httpx.ReadTimeout("upstream timeout"))
        a, b, c = self._authenticated(fake)
        with a, b, c, TestClient(gateway_module.app) as browser:
            r = browser.get("/mcp")
        self.assertEqual(r.status_code, 504)
        self.assertEqual(r.json(), {"error": "upstream_timeout"})
        self.assertTrue(fake.closed)

    def test_asgi_downstream_disconnect_releases_resources(self):
        async def run():
            fake = FakeHTTPClient()
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1", "method": "GET", "path": "/mcp",
                "raw_path": b"/mcp", "query_string": b"", "scheme": "http",
                "headers": [(b"authorization", b"Bearer test")],
            }
            request = Request(scope, receive)
            a, b, c = self._authenticated(fake)
            with a, b, c:
                response = await gateway_module.mcp_proxy_fixed(request)
                async def send(message):
                    if message["type"] == "http.response.body":
                        raise OSError("client disconnected")
                with self.assertRaises(Exception):
                    await response(scope, receive, send)
            self.assertTrue(fake.upstream.closed)
            self.assertTrue(fake.closed)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
