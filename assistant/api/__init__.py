"""The assistant's HTTP and WebSocket surface."""

from .routes import build_assistant_app, router

__all__ = ["build_assistant_app", "router"]
