"""Symphony control API: `/v1` (bearer `sym_` tokens), SSE, MCP and webhooks."""

from .app import build_app, build_services
from .control import Services

__all__ = ["Services", "build_app", "build_services"]
