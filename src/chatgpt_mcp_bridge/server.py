"""ChatGPT MCP Bridge entry point."""

from .logging_config import _configure_logging

_configure_logging()
from . import tools as _tools  # noqa: E402,F401
from .transport.http import main  # noqa: E402

if __name__ == "__main__":
    main()
