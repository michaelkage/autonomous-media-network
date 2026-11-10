"""Local-first target and category routing configuration."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "sources": {"reddit": []},
    "targets": [],
    "routes": {"default": []},
    "thresholds": {"min_likes": 500, "max_distance": 5},
}


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("AMN config must be a JSON object")
    return {**DEFAULT_CONFIG, **data}


def validate_config(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data.get("sources", {}), dict):
        raise ValueError("sources must be an object")
    if not isinstance(data.get("targets", []), list):
        raise ValueError("targets must be an array")
    if not isinstance(data.get("routes", {}), dict):
        raise ValueError("routes must be an object")
    thresholds = data.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ValueError("thresholds must be an object")
    likes, distance = int(thresholds.get("min_likes", 500)), int(thresholds.get("max_distance", 5))
    if likes < 0 or not 0 <= distance <= 64:
        raise ValueError("min_likes must be >= 0 and max_distance must be between 0 and 64")
    ids: set[str] = set()
    for target in data.get("targets", []):
        if not isinstance(target, dict) or not target.get("id") or not target.get("platform"):
            raise ValueError("each target requires id and platform")
        target_id = str(target["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", target_id):
            raise ValueError(
                "target id must contain only letters, numbers, underscores, and hyphens"
            )
        if target_id in ids:
            raise ValueError(f"duplicate target id: {target_id}")
        ids.add(target_id)
        for field in target:
            normalized = field.lower()
            if normalized in {
                "token", "access_token", "refresh_token", "password", "secret", "client_secret"
            }:
                raise ValueError(
                    f"store credentials in environment variables, not config ({field})"
                )
        if target.get("token_env") and not str(target["token_env"]).isidentifier():
            raise ValueError("token_env must be an environment variable name")
    for route, target_ids in data.get("routes", {}).items():
        if not isinstance(target_ids, list) or any(
            target_id not in ids for target_id in target_ids
        ):
            raise ValueError(f"route {route!r} references an unknown target")
    return data


def save_config(path: Path, data: dict[str, Any]) -> None:
    validate_config(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".amn-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def targets_for(item: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve explicit categories/tags to configured targets; no implicit broadcast."""
    categories = set(item.get("categories", [])) | set(item.get("tags", []))
    if item.get("subreddit"):
        categories.add(item["subreddit"])
    routes = config.get("routes", {})
    target_ids: set[str] = set(routes.get("default", []))
    for category in categories:
        target_ids.update(routes.get(category, []))
    return [target for target in config.get("targets", [])
            if target.get("enabled", True) and target.get("id") in target_ids]


def publish_routed_manifest(root: Path) -> dict[str, int]:
    """Dispatch each manifest item only to targets selected by its route."""
    from .publish import BlueskyPublisher, publish_manifest
    from .publishers.instagram import InstagramPublisher
    from .publishers.telegram import TelegramPublisher
    from .publishers.tiktok import TikTokPublisher

    root = Path(root).resolve()
    manifest = root / "build" / "manifest.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest}")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    config = validate_config(load_config(root / "config" / "amn.json"))
    totals: dict[str, int] = {}
    for target in config.get("targets", []):
        if not target.get("enabled", True):
            continue
        platform = target["platform"].lower()
        if platform == "telegram":
            token = os.environ.get(target.get("token_env", "TELEGRAM_BOT_TOKEN"))
            chat_id = target.get("chat_id")
            if not token or not chat_id:
                print(f"skipping {target['id']}: Telegram token or chat_id missing")
                continue
            publisher = TelegramPublisher(token, str(chat_id))
        elif platform == "bluesky":
            handle = os.environ.get(target.get("handle_env", "BLUESKY_HANDLE"))
            password = os.environ.get(target.get("password_env", "BLUESKY_APP_PASSWORD"))
            if not handle or not password:
                print(f"skipping {target['id']}: Bluesky credentials missing")
                continue
            publisher = BlueskyPublisher(handle, password)
        elif platform == "instagram":
            token = os.environ.get(target.get("token_env", "INSTAGRAM_ACCESS_TOKEN"))
            user_id = target.get("instagram_user_id")
            media_url = target.get("public_base_url")
            if not token or not user_id or not media_url:
                print(
                    f"skipping {target['id']}: Instagram token, user ID or public_base_url missing"
                )
                continue
            publisher = InstagramPublisher(token, str(user_id), str(media_url))
        elif platform == "tiktok":
            token = os.environ.get(target.get("token_env", "TIKTOK_ACCESS_TOKEN"))
            media_url = target.get("public_base_url")
            privacy = target.get("privacy_level")
            if not token or not media_url or not privacy:
                print(
                    f"skipping {target['id']}: TikTok token, public_base_url "
                    "or privacy_level missing"
                )
                continue
            publisher = TikTokPublisher(
                token, str(media_url), str(privacy),
                direct_post_enabled=target.get("direct_post_enabled", False))
        else:
            print(f"skipping {target['id']}: unsupported platform {platform}")
            continue

        selected = [item for item in data.get("passed", []) if target in targets_for(item, config)]
        if not selected:
            totals[target["id"]] = 0
            continue
        temporary_manifest = root / "build" / f".routed-{target['id']}.json"
        temporary_manifest.write_text(json.dumps({"passed": selected}), encoding="utf-8")
        try:
            totals[target["id"]] = publish_manifest(
                temporary_manifest, publisher, root=root, platform=target["id"])
            if platform == "telegram" and totals[target["id"]]:
                publisher.publish_document(temporary_manifest)
        finally:
            temporary_manifest.unlink(missing_ok=True)
    return totals
