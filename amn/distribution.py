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
    if not isinstance(data, dict):
        raise ValueError("AMN config must be a JSON object")

    sources = data.get("sources", {})
    if not isinstance(sources, dict):
        raise ValueError("sources must be an object")
    reddit_sources = sources.get("reddit", [])
    if (
        not isinstance(reddit_sources, list)
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,21}", name)
            for name in reddit_sources
        )
        or len(set(reddit_sources)) != len(reddit_sources)
    ):
        raise ValueError("sources.reddit must contain unique valid subreddit names")

    targets = data.get("targets", [])
    if not isinstance(targets, list):
        raise ValueError("targets must be an array")
    routes = data.get("routes", {})
    if not isinstance(routes, dict):
        raise ValueError("routes must be an object")
    thresholds = data.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ValueError("thresholds must be an object")

    likes = thresholds.get("min_likes", 500)
    distance = thresholds.get("max_distance", 5)
    if (
        isinstance(likes, bool)
        or not isinstance(likes, int)
        or isinstance(distance, bool)
        or not isinstance(distance, int)
        or likes < 0
        or not 0 <= distance <= 64
    ):
        raise ValueError(
            "min_likes must be a non-negative integer and max_distance an integer from 0 to 64"
        )

    allowed_fields = {
        "id", "platform", "enabled", "token_env", "handle_env", "password_env",
        "chat_id", "instagram_user_id", "public_base_url", "privacy_level",
        "direct_post_enabled",
    }
    supported_platforms = {"telegram", "bluesky", "instagram", "tiktok"}
    ids: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("each target must be an object")
        if set(target) - allowed_fields:
            raise ValueError(f"unsupported target fields: {sorted(set(target) - allowed_fields)}")
        target_id, platform = target.get("id"), target.get("platform")
        if not isinstance(target_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", target_id
        ):
            raise ValueError(
                "target id must contain only letters, numbers, underscores, and hyphens"
            )
        if not isinstance(platform, str) or platform.lower() not in supported_platforms:
            raise ValueError(f"target {target_id!r} has an unsupported platform")
        if target_id in ids:
            raise ValueError(f"duplicate target id: {target_id}")
        ids.add(target_id)

        if "enabled" in target and not isinstance(target["enabled"], bool):
            raise ValueError(f"target {target_id!r}: enabled must be boolean")
        if "direct_post_enabled" in target and not isinstance(target["direct_post_enabled"], bool):
            raise ValueError(f"target {target_id!r}: direct_post_enabled must be boolean")
        for env_key in ("token_env", "handle_env", "password_env"):
            value = target.get(env_key)
            if value is not None and (
                not isinstance(value, str) or not value.isidentifier()
            ):
                raise ValueError(
                    f"target {target_id!r}: {env_key} must be an environment variable name"
                )
        for string_key in (
            "chat_id", "instagram_user_id", "public_base_url", "privacy_level"
        ):
            value = target.get(string_key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"target {target_id!r}: {string_key} must be a non-empty string")

        if platform.lower() in {"instagram", "tiktok"}:
            from urllib.parse import urlparse

            media_url = target.get("public_base_url")
            if not isinstance(media_url, str):
                raise ValueError(f"target {target_id!r}: public_base_url must use HTTPS")
            parsed = urlparse(media_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError(f"target {target_id!r}: public_base_url must use HTTPS")
        if platform.lower() == "instagram" and target.get("enabled", True):
            if not re.fullmatch(r"[0-9]+", target.get("instagram_user_id", "")):
                raise ValueError(f"target {target_id!r}: instagram_user_id must be numeric")
        if platform.lower() == "tiktok" and target.get("enabled", True):
            if not target.get("privacy_level"):
                raise ValueError(f"target {target_id!r}: privacy_level is required")
        if platform.lower() == "tiktok" and target.get("enabled", True):
            if not target.get("direct_post_enabled", False):
                raise ValueError(
                    f"target {target_id!r}: enable direct_post_enabled for active TikTok targets"
                )

    for route, target_ids in routes.items():
        if not isinstance(route, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", route
        ):
            raise ValueError(
                "route names must contain only letters, numbers, underscores, and hyphens"
            )
        if not isinstance(target_ids, list) or any(
            not isinstance(target_id, str) or target_id not in ids for target_id in target_ids
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
    return [
        target
        for target in config.get("targets", [])
        if target.get("enabled", True) and target.get("id") in target_ids
    ]


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
            if not target.get("direct_post_enabled", False):
                print(f"skipping {target['id']}: TikTok direct posting is not enabled")
                continue
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
                token,
                str(media_url),
                str(privacy),
                direct_post_enabled=target.get("direct_post_enabled", False),
            )
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
                temporary_manifest, publisher, root=root, platform=target["id"]
            )
            if platform == "telegram" and totals[target["id"]]:
                publisher.publish_document(temporary_manifest)
        finally:
            temporary_manifest.unlink(missing_ok=True)
    return totals
