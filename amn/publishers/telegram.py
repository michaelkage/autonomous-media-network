"""Telegram Bot API publisher. Tokens are read from environment, never config files."""

from __future__ import annotations

from pathlib import Path

import requests

from ..publish import Publisher

__all__ = ["TelegramPublisher"]


class TelegramPublisher(Publisher):
    def __init__(self, bot_token: str, chat_id: str, *, timeout: float = 30.0) -> None:
        if not bot_token or not chat_id:
            raise ValueError("Telegram bot token and chat id are required")
        self.chat_id, self.timeout = chat_id, timeout
        self.endpoint = f"https://api.telegram.org/bot{bot_token}"
        self.session = requests.Session()

    def publish(self, image_path: Path, caption: str) -> bool:
        try:
            with image_path.open("rb") as image:
                response = self.session.post(
                    f"{self.endpoint}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption[:1024]},
                    files={"photo": (image_path.name, image, "application/octet-stream")},
                    timeout=self.timeout,
                )
            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 1)
                raise RuntimeError(f"Telegram rate limited; retry after {retry_after}s")
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(payload.get("description", "Telegram rejected the post"))
            return True
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            print(f"Telegram publish failed: {exc}")
            return False
