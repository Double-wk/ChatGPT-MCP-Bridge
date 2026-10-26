"""Shared tool lifecycle and error handling."""

import asyncio
import functools
import logging

from mcp.server.mcpserver.exceptions import ToolError
from opentelemetry import trace

from ..runtime import context
from ..security.redaction import _redact_secrets

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("chatgpt_mcp_bridge")


def mcp_tool(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            await context.initialize()
            with tracer.start_as_current_span(f"mcp.tool.{function.__name__}"):
                return await function(*args, **kwargs)
        except (ToolError, asyncio.CancelledError):
            raise
        except Exception as error:
            message = _redact_secrets(str(error))
            logger.error("%s failed: %s", function.__name__, message)
            raise ToolError(message) from error

    return wrapped
