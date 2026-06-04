"""Shared library — used by every service. Never import a service from here."""

from polybot.config import settings
from polybot.logging import get_logger

__all__ = ["settings", "get_logger"]
__version__ = "0.1.0"
