"""Authenticated MCP HTTP transport and process entry-point dispatch."""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import sys
from collections.abc import AsyncGenerator
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings

from .. import __version__
from ..app import mcp
from ..config import getenv
from ..runtime import context

logger = logging.getLogger(__name__)

_MIN_TOKEN_LENGTH = 16


def _build_http_app(
    token: str | None,
    *,
    stateless: bool,
    transport_security: TransportSecuritySettings,
) -> Any:
    """Return the Starlette ASGI app for streamable HTTP transport."""
    from starlette.applications import Starlette
    from starlette.routing import Mount

    inner_app = mcp.streamable_http_app(
        stateless_http=stateless,
        transport_security=transport_security,
    )

    try:
        session_manager = mcp.session_manager
    except RuntimeError as exc:
        raise RuntimeError(
            "MCPServer's session manager was not initialized by "
            "streamable_http_app() — this used to also populate "
            "Starlette's private router.lifespan_context, which "
            "ssh-mcp no longer depends on. The MCP SDK may have changed "
            "its internal wiring (as of mcp 2.2.0 it lives in "
            "mcp/server/lowlevel/server.py); update the lifespan wiring "
            "in server.py._build_http_app or pin a known-good mcp."
        ) from exc

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with session_manager.run():
            try:
                yield
            finally:
                if context.executor is not None:
                    logger.info("Draining Codex connection on HTTP shutdown")
                    try:
                        await context.shutdown()
                        logger.info("Codex connection drained cleanly")
                    except Exception as e:
                        logger.warning(
                            "Error draining Codex connection: %s",
                            e,
                            exc_info=True,
                        )

    app = Starlette(
        routes=[Mount("/", app=inner_app)],
        lifespan=_lifespan,
    )

    if token is not None:
        _assert_valid_bearer_token(token)
        _BearerAuth = _make_bearer_auth_middleware()
        app.add_middleware(_BearerAuth, expected=token)

    return app


def _assert_valid_bearer_token(token: str) -> None:
    """Raise ValueError if ``token`` cannot work as an HTTP bearer credential."""
    if not token or len(token) < _MIN_TOKEN_LENGTH:
        raise ValueError(
            f"bearer token must be at least {_MIN_TOKEN_LENGTH} characters "
            f"(got {len(token)}) — a short or empty token is a security risk"
        )
    if not token.isascii() or not token.isprintable() or any(map(str.isspace, token)):
        raise ValueError(
            "bearer token must be printable ASCII with no whitespace — an "
            "HTTP header value cannot carry control characters, interior "
            "spaces or non-ASCII text, so such a token would return 401 to "
            "every client. Check for a stray newline, tab or non-ASCII "
            "character in SSH_MCP_HTTP_TOKEN / SSH_MCP_HTTP_TOKEN_FILE."
        )


def _make_bearer_auth_middleware() -> Any:
    """Return the ``_BearerAuth`` pure ASGI middleware class."""
    import hmac
    import json

    async def _send_401(
        send: Any,
        body: dict[str, str],
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        encoded = json.dumps(body).encode()
        response_headers = list(headers or [(b"content-type", b"application/json")])
        response_headers.append((b"content-length", str(len(encoded)).encode()))
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": response_headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": encoded,
            }
        )

    class _BearerAuth:
        def __init__(self, app: Any, expected: str) -> None:
            self.app = app
            self._expected = expected

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            headers = dict(scope.get("headers", []))
            raw_auth = headers.get(b"authorization", b"")
            parts = raw_auth.split(maxsplit=1)
            if len(parts) != 2 or parts[0].decode("latin-1").lower() != "bearer":
                await _send_401(
                    send,
                    {"error": "missing bearer token"},
                    headers=[
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="ssh-mcp"'),
                    ],
                )
                return
            supplied = parts[1]
            expected = self._expected.encode("utf-8")
            if not hmac.compare_digest(supplied, expected):
                await _send_401(
                    send,
                    {"error": "invalid bearer token"},
                    headers=[
                        (b"content-type", b"application/json"),
                        (
                            b"www-authenticate",
                            b'Bearer realm="ssh-mcp", error="invalid_token"',
                        ),
                    ],
                )
                return
            await self.app(scope, receive, send)

    return _BearerAuth


def _build_transport_security(
    raw_allowed_hosts: str | None, host: str
) -> TransportSecuritySettings:
    """Build DNS-rebinding-protection settings, refusing wildcard hosts."""
    allowed_hosts_env = (raw_allowed_hosts or "").strip()

    base_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    extra_hosts: list[str] = []
    if raw_allowed_hosts and not allowed_hosts_env:
        raise RuntimeError(
            "SSH_MCP_HTTP_ALLOWED_HOSTS is set but contains only whitespace. "
            "Unset the variable to use the localhost-only default, or "
            "provide a concrete hostname (e.g. 'ssh-mcp.internal:*')."
        )
    if allowed_hosts_env:
        extra_hosts = [h.strip() for h in allowed_hosts_env.split(",") if h.strip()]
        for entry in extra_hosts:
            remainder = entry[:-2] if entry.endswith(":*") else entry
            if not remainder or "*" in remainder or not remainder.strip("."):
                raise RuntimeError(
                    f"SSH_MCP_HTTP_ALLOWED_HOSTS entry {entry!r} contains a "
                    "wildcard the MCP SDK does not implement, so it would "
                    "match no request at all. Only a trailing ':*' port "
                    "wildcard is supported. List each concrete hostname "
                    "instead (e.g. 'api.internal.example.com:*')."
                )

    if host not in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:  # nosec B104
        base_hosts.append(f"{host}:*")

    default_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*base_hosts, *extra_hosts],
        allowed_origins=default_origins,
    )


_MAX_TOKEN_FILE_BYTES = 65536


def _read_token_file(token_file: str) -> str:
    """Read the bearer token from a file, validating what we actually read."""
    try:
        fd = os.open(token_file, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError as e:
        raise RuntimeError(
            f"SSH_MCP_HTTP_TOKEN_FILE={token_file!r} could not be read: {e}"
        ) from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(
                f"SSH_MCP_HTTP_TOKEN_FILE={token_file!r} is not a regular file "
                f"(mode {stat.filemode(st.st_mode)}). Point it at a file "
                "containing the token."
            )
        if st.st_size > _MAX_TOKEN_FILE_BYTES:
            raise RuntimeError(
                f"SSH_MCP_HTTP_TOKEN_FILE={token_file!r} is "
                f"{st.st_size} bytes, over the {_MAX_TOKEN_FILE_BYTES}-byte "
                "limit. A bearer token is tens of bytes — this is almost "
                "certainly the wrong path."
            )
        posix_perms = os.name == "posix"
        if posix_perms and st.st_mode & 0o022:
            raise RuntimeError(
                f"SSH_MCP_HTTP_TOKEN_FILE={token_file!r} is writable beyond "
                f"its owner (mode {oct(stat.S_IMODE(st.st_mode))}). Anyone "
                "who can write this file chooses the token that authorises "
                "remote command execution. Run: chmod go-w "
                f"{token_file!r}"
            )
        if posix_perms and st.st_mode & 0o044:
            logger.warning(
                "SSH_MCP_HTTP_TOKEN_FILE %r is readable beyond its owner "
                "(mode %s). This token authenticates remote command "
                "execution; prefer 0600. Container secret mounts are "
                "commonly 0444 (Docker) or 0644 (Kubernetes), which is "
                "usually acceptable inside an isolated image but not on a "
                "shared host.",
                token_file,
                oct(stat.S_IMODE(st.st_mode)),
            )
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None and st.st_uid not in (geteuid(), 0):
            logger.warning(
                "SSH_MCP_HTTP_TOKEN_FILE %r is owned by uid %d, which is "
                "neither this process (uid %d) nor root. That user can "
                "replace the token at any time.",
                token_file,
                st.st_uid,
                geteuid(),
            )
        with os.fdopen(fd, "rb") as fh:
            fd = -1  # fdopen owns it now; do not double-close in `finally`
            raw = fh.read(_MAX_TOKEN_FILE_BYTES + 1)
        if len(raw) > _MAX_TOKEN_FILE_BYTES:
            raise RuntimeError(
                f"SSH_MCP_HTTP_TOKEN_FILE={token_file!r} exceeds the "
                f"{_MAX_TOKEN_FILE_BYTES}-byte limit while being read. "
                "A bearer token is tens of bytes — this is almost "
                "certainly the wrong path."
            )
        return raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError as e:
        raise RuntimeError(
            f"SSH_MCP_HTTP_TOKEN_FILE={token_file!r} is not valid UTF-8 "
            f"({e.reason} at byte {e.start}). A bearer token must be text; "
            "this looks like a binary file."
        ) from e
    except OSError as e:
        raise RuntimeError(
            f"SSH_MCP_HTTP_TOKEN_FILE={token_file!r} could not be read: {e}"
        ) from e
    finally:
        if fd != -1:
            os.close(fd)


def _run_http() -> None:
    """Run ssh-mcp over MCP streamable HTTP transport."""
    import uvicorn

    host = getenv("SSH_MCP_HTTP_HOST", "127.0.0.1")
    raw_port = getenv("SSH_MCP_HTTP_PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError(
            f"SSH_MCP_HTTP_PORT={raw_port!r} is not a valid integer"
        ) from exc
    if not (1 <= port <= 65535):
        raise RuntimeError(
            f"SSH_MCP_HTTP_PORT={port} is out of range (must be 1-65535)"
        )
    env_token_raw = getenv("SSH_MCP_HTTP_TOKEN", "")
    raw_token = env_token_raw.strip()
    token_file = getenv("SSH_MCP_HTTP_TOKEN_FILE", "").strip()
    if not raw_token and token_file:
        raw_token = _read_token_file(token_file)
        if not raw_token:
            raise RuntimeError(
                f"SSH_MCP_HTTP_TOKEN_FILE={token_file!r} is empty or contains "
                "only whitespace. A configured token source that yields no "
                "token would silently disable authentication. Write a token "
                "to the file, unset the variable, or set "
                "SSH_MCP_HTTP_AUTH=none if unauthenticated is deliberate."
            )
    elif not raw_token and env_token_raw and not token_file:
        raise RuntimeError(
            "SSH_MCP_HTTP_TOKEN is set but contains only whitespace. A "
            "configured token that strips to nothing would silently disable "
            "authentication. Set a real token, unset the variable, or set "
            "SSH_MCP_HTTP_AUTH=none if unauthenticated is deliberate."
        )
    token = raw_token or None
    stateless = getenv("SSH_MCP_HTTP_STATELESS", "false").strip().lower() == "true"
    _allowed_hosts_raw = getenv("SSH_MCP_HTTP_ALLOWED_HOSTS")

    auth_mode = getenv("SSH_MCP_HTTP_AUTH", "bearer").strip().lower()
    if auth_mode not in {"bearer", "none"}:
        raise RuntimeError(
            f"Unknown SSH_MCP_HTTP_AUTH={auth_mode!r}. "
            "Valid values: 'bearer' (default), 'none'."
        )

    import ipaddress

    try:
        address = ipaddress.ip_address(host)
        mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
        is_localhost = mapped.is_loopback if mapped is not None else address.is_loopback
    except ValueError:
        is_localhost = host.lower() in {"localhost"}

    if auth_mode == "bearer":
        if not is_localhost and token is None:
            raise RuntimeError(
                f"SSH_MCP_HTTP_TOKEN must be set when binding to {host!r}. "
                "Refusing to expose SSH command execution over the network "
                "without bearer-token authentication."
            )
    else:  # auth_mode == "none"
        token = None
        if not is_localhost:
            ack = getenv("SSH_MCP_HTTP_NETWORK_NO_AUTH", "")
            if ack != "I_ACCEPT_RCE_RISK":
                raise RuntimeError(
                    f"SSH_MCP_HTTP_AUTH=none with host={host!r} is refused. "
                    "Binding an unauthenticated SSH command executor to a "
                    "non-localhost address is equivalent to granting a "
                    "remote root shell to anyone who can reach the port. "
                    "If you understand the risk and handle authentication "
                    "at a reverse proxy, set environment variable "
                    "SSH_MCP_HTTP_NETWORK_NO_AUTH=I_ACCEPT_RCE_RISK to "
                    "proceed."
                )

    transport_security = _build_transport_security(_allowed_hosts_raw, host)

    effective_auth = "bearer" if token else "none"
    logger.info(
        "Starting ChatGPT MCP Bridge v%s (streamable-http) on %s:%s stateless=%s auth=%s",
        __version__,
        host,
        port,
        stateless,
        effective_auth,
    )
    if token is None:
        if is_localhost:
            logger.warning(
                "HTTP transport is running WITHOUT authentication on %s. "
                "Do NOT forward this port beyond the loopback interface.",
                host,
            )
        else:
            logger.warning(
                "⚠️  ChatGPT MCP Bridge is serving UNAUTHENTICATED HTTP on %s:%s. "
                "You accepted SSH_MCP_HTTP_NETWORK_NO_AUTH=I_ACCEPT_RCE_RISK. "
                "Every request that reaches /mcp can execute shell commands "
                "on the configured remote servers. Terminate auth at your "
                "reverse proxy and NEVER expose this port to an untrusted "
                "network.",
                host,
                port,
            )

    app = _build_http_app(
        token, stateless=stateless, transport_security=transport_security
    )

    keepalive, concurrency, backlog = _parse_http_tuning()
    logger.info(
        "HTTP tuning: timeout_keep_alive=%ss limit_concurrency=%s backlog=%s",
        keepalive,
        concurrency,
        backlog,
    )

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,  # use the root logger we configured above
        timeout_keep_alive=keepalive,
        limit_concurrency=concurrency,
        backlog=backlog,
    )


def _parse_http_tuning() -> tuple[int, int, int]:
    """Parse the uvicorn tuning env vars with validation."""

    def _parse_int(name: str, default: int, *, min_value: int) -> int:
        raw = getenv(name)
        if raw is None or raw.strip() == "":
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{name}={raw!r} is not a valid integer") from exc
        if value < min_value:
            raise RuntimeError(
                f"{name}={value} is below the minimum allowed value {min_value}"
            )
        return value

    keepalive = _parse_int("SSH_MCP_HTTP_KEEPALIVE_TIMEOUT", default=2, min_value=0)
    concurrency = _parse_int("SSH_MCP_HTTP_LIMIT_CONCURRENCY", default=256, min_value=1)
    backlog = _parse_int("SSH_MCP_HTTP_BACKLOG", default=128, min_value=1)
    return keepalive, concurrency, backlog


def main() -> None:
    """Entry point for console script (uvx ssh-mcp)."""
    if len(sys.argv) >= 2 and sys.argv[1] == "healthcheck":
        from .healthcheck import run as run_healthcheck

        run_healthcheck()  # exits 0 or 1
        return  # unreachable but keeps mypy happy

    transport = getenv("SSH_MCP_TRANSPORT", "stdio").strip().lower()

    if transport in ("http", "streamable-http"):
        _run_http()
        return
    if transport != "stdio":
        raise ValueError(f"Unknown SSH_MCP_TRANSPORT={transport!r}; use stdio or http")
    logger.info("Starting local Codex MCP bridge v%s", __version__)
    mcp.run(transport="stdio")
