"""The Publishing Layer.

Reads the run manifest and pushes approved assets to social media platforms.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

__all__ = ["Publisher", "MockPublisher", "BlueskyPublisher", "publish_manifest"]


class Publisher(ABC):
    """Abstract base for all social media publishers."""

    @abstractmethod
    def publish(self, image_path: Path, caption: str) -> bool:
        """Post the image with a caption. Returns True if successful."""
        ...


class MockPublisher(Publisher):
    """A publisher that just logs to the console. Use for testing."""

    def publish(self, image_path: Path, caption: str) -> bool:
        print(f"[MOCK PUBLISH] {image_path.name} | Caption: {caption}")
        return True


class BlueskyPublisher(Publisher):
    """Publishes assets to the AT Protocol (Bluesky).

    Requires the optional `bluesky` extra: pip install -e ".[bluesky]".
    """

    def __init__(self, handle: str, app_password: str) -> None:
        try:
            from atproto import Client
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                'Bluesky publishing needs atproto. Install it with: pip install -e ".[bluesky]"'
            ) from exc
        self.client = Client()
        self.client.login(handle, app_password)

    def publish(self, image_path: Path, caption: str) -> bool:
        try:
            # Upload image
            with open(image_path, "rb") as f:
                img_ref = self.client.upload_blob(f.read())

            # Create post
            self.client.send_post(
                text=caption,
                embed=self.client.create_embed(
                    image=img_ref, alt="Generated image from Autonomous Media Network"
                ),
            )
            return True
        except Exception as exc:
            print(f"Bluesky publish failed: {exc}")
            return False


def publish_manifest(manifest_path: Path, publisher: Publisher) -> int:
    """Process a run manifest and publish all passing assets.

    Returns the count of successfully published images.
    """
    if not manifest_path.is_file():
        print(f"manifest not found: {manifest_path}")
        return 0

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    passed = data.get("passed", [])

    successes = 0
    for item in passed:
        # Manifest output paths are relative to root
        image_path = Path(item["output"])
        caption = item.get("caption", "No caption provided.")

        if publisher.publish(image_path, caption):
            successes += 1

    return successes
