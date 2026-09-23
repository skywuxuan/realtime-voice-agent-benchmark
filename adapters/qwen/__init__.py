"""Qwen Audio 3.0 Realtime adapter (optional WebSocket dependency)."""

from adapters.qwen.adapter import QwenRealtimeAdapter
from adapters.qwen.config import QwenSettings

__all__ = ["QwenRealtimeAdapter", "QwenSettings"]
