"""DeepSeek integration: HTTP client and structured classification output."""

from app.deepseek.client import DeepSeekClient, DeepSeekError
from app.deepseek.schemas import Classification

__all__ = [
    "Classification",
    "DeepSeekClient",
    "DeepSeekError",
]
