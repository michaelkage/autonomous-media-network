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

    def _send_file(self, method: str, field: str, path: Path, caption: str) -> bool:
        try:
            with path.open("rb") as handle:
                response = self.session.post(
                    f"{self.endpoint}/{method}",
                    data={"chat_id": self.chat_id, "caption": caption[:1024]},
                    files={field: (path.name, handle, "application/octet-stream")},
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
            print(f"Telegram {method} failed: {exc}")
            return False

    def publish(self, image_path: Path, caption: str) -> bool:
        return self._send_file("sendPhoto", "photo", image_path, caption)

    def publish_document(self, document_path: Path, caption: str = "AMN delivery manifest") -> bool:
        """Send the selected delivery manifest as a Telegram document."""
        return self._send_file("sendDocument", "document", document_path, caption)
