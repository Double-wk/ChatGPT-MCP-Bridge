import logging
import sys
from typing import Any

import structlog

from .config import getenv


def _configure_logging() -> None:
    """Configure stderr logging with console or JSON output.

    Routes stdlib ``logging`` calls (from all modules) through structlog so
    any ``logger.info(...)`` call in ssh.py / config.py / server.py receives
    consistent ISO timestamps and structured rendering without changes to
    call sites.

    Controlled by ``SSH_MCP_LOG_FORMAT``:
      * unset / "console" (default): colorized human-readable output (dev)
      * "json": single-line JSON, one object per event (production log
        aggregators, structured search)

    Called unconditionally at module import so tool calls are instrumented
    even during lazy init. Idempotent — clears prior handlers before adding
    its own so repeated imports (e.g. under pytest) do not duplicate output.
    """
    fmt = getenv("SSH_MCP_LOG_FORMAT", "console").lower()

    # Processors applied to BOTH structlog-native loggers and stdlib foreign
    # loggers, so timestamps / levels / contextvars are consistent across
    # every log line no matter which logger produced it.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    renderer: Any
    if fmt == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Clear existing handlers so repeated configuration (e.g. under pytest
    # reloads) does not duplicate log lines.
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    logging.getLogger("mcp.server.mcpserver.server").setLevel(logging.WARNING)

    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
