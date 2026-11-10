"""Local-first target and category routing configuration."""

from __future__ import annotations

import json
import os
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
    likes, distance = int(thresholds.get("min_likes", 500)), int(thresholds.get("max_distance", 5))
    if likes < 0 or not 0 <= distance <= 64:
        raise ValueError("min_likes must be >= 0 and max_distance must be between 0 and 64")
    for target in data.get("targets", []):
        if not isinstance(target, dict) or not target.get("id") or not target.get("platform"):
            raise ValueError("each target requires id and platform")
        if target.get("token_env") and not str(target["token_env"]).isidentifier():
            raise ValueError("token_env must be an environment variable name")
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
    """Resolve explicit categories/tags to configured targets; never broadcast implicitly."""
    categories = set(item.get("categories", [])) | set(item.get("tags", []))
    routes = config.get("routes", {})
    target_ids: set[str] = set(routes.get("default", []))
    for category in categories:
        target_ids.update(routes.get(category, []))
    return [target for target in config.get("targets", [])
            if target.get("enabled", True) and target.get("id") in target_ids]
