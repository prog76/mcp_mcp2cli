"""mcp2cli — command-line interface for calling MCP tools."""

from mcp2cli import client as client  # noqa: F401
from mcp2cli import cli as cli        # noqa: F401

__version__ = "0.1.0"

__all__ = ["client", "cli", "__version__"]
