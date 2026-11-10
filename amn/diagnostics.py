"""Read-only preflight checks for configured publishing targets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .distribution import load_config, validate_config


def _check(target: str, name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"target": target, "check": name, "ok": ok, "detail": detail}


def run_diagnostics(root: Path, *, session: Any = None) -> list[dict[str, Any]]:
    """Check local config, provider access, and public media reachability.

    This function never publishes. Provider errors are sanitized because some APIs
    echo request URLs or credential-related details in exception messages.
    """
    root = Path(root).resolve()
    client = session or requests.Session()
    config_path = root / "config" / "amn.json"
    try:
        config = validate_config(load_config(config_path))
    except (OSError, ValueError) as exc:
        return [_check("configuration", "validation", False, str(exc))]

    results: list[dict[str, Any]] = []
    manifest_path = root / "build" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        passed = manifest.get("passed", [])
    except (OSError, ValueError, AttributeError):
        passed = []
        results.append(_check("workspace", "manifest", False, "manifest is unreadable"))

    for target in config.get("targets", []):
        target_id = target["id"]
        if not target.get("enabled", True):
            results.append(_check(target_id, "configuration", True, "target disabled; skipped"))
            continue

        platform = target["platform"].lower()
        if platform == "telegram":
            token = os.environ.get(target.get("token_env", "TELEGRAM_BOT_TOKEN"))
            chat_id = target.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
            if not token or not chat_id:
                results.append(
                    _check(
                        target_id,
                        "credentials",
                        False,
                        "bot token or chat ID missing",
                    )
                )
                continue
            try:
                base = f"https://api.telegram.org/bot{token}"
                me = client.get(f"{base}/getMe", timeout=15)
                me.raise_for_status()
                if not me.json().get("ok"):
                    raise ValueError("Telegram rejected bot authentication")
                chat = client.get(
                    f"{base}/getChat",
                    params={"chat_id": chat_id},
                    timeout=15,
                )
                chat.raise_for_status()
                if not chat.json().get("ok"):
                    raise ValueError("Telegram bot cannot access the configured chat")
                results.append(
                    _check(
                        target_id,
                        "account",
                        True,
                        "bot authenticated and chat accessible",
                    )
                )
            except (requests.RequestException, ValueError, TypeError, AttributeError):
                results.append(
                    _check(
                        target_id,
                        "account",
                        False,
                        "Telegram authentication or chat access failed",
                    )
                )

        elif platform == "instagram":
            token = os.environ.get(target.get("token_env", "INSTAGRAM_ACCESS_TOKEN"))
            user_id = target.get("instagram_user_id")
            if not token or not user_id:
                results.append(
                    _check(
                        target_id,
                        "credentials",
                        False,
                        "access token or account ID missing",
                    )
                )
                continue
            try:
                response = client.get(
                    f"https://graph.facebook.com/v25.0/{user_id}",
                    params={"fields": "id,username", "access_token": token},
                    timeout=15,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("error") or str(payload.get("id")) != str(user_id):
                    raise ValueError("Instagram account lookup failed")
                results.append(
                    _check(
                        target_id,
                        "account",
                        True,
                        "Instagram account token accepted",
                    )
                )
            except (requests.RequestException, ValueError, TypeError, AttributeError):
                results.append(
                    _check(
                        target_id,
                        "account",
                        False,
                        "Instagram token, account ID, or permissions rejected",
                    )
                )

        elif platform == "tiktok":
            token = os.environ.get(target.get("token_env", "TIKTOK_ACCESS_TOKEN"))
            if not token:
                results.append(
                    _check(
                        target_id,
                        "credentials",
                        False,
                        "user access token missing",
                    )
                )
                continue
            try:
                response = client.post(
                    "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={},
                    timeout=15,
                )
                response.raise_for_status()
                payload = response.json()
                error = payload.get("error", {})
                options = payload.get("data", {}).get("privacy_level_options", [])
                if error.get("code") not in {None, "", "ok"}:
                    raise ValueError("TikTok creator-info request rejected")
                privacy = target.get("privacy_level")
                if privacy not in options:
                    detail = f"privacy level unavailable; account options: {options}"
                    results.append(_check(target_id, "account", False, detail))
                else:
                    detail = f"creator access accepted; configured privacy level {privacy} is available"
                    results.append(_check(target_id, "account", True, detail))
            except (
                requests.RequestException,
                ValueError,
                TypeError,
                AttributeError,
            ):
                results.append(
                    _check(
                        target_id,
                        "account",
                        False,
                        "TikTok token, scope, or creator access rejected",
                    )
                )

        elif platform == "bluesky":
            handle = os.environ.get(target.get("handle_env", "BLUESKY_HANDLE"))
            password = os.environ.get(target.get("password_env", "BLUESKY_APP_PASSWORD"))
            if not handle or not password:
                results.append(
                    _check(
                        target_id,
                        "credentials",
                        False,
                        "handle or app password missing",
                    )
                )
                continue
            try:
                from atproto import Client

                bluesky = Client()
                bluesky.login(handle, password)
                results.append(_check(target_id, "account", True, "Bluesky login accepted"))
            except Exception:
                results.append(
                    _check(
                        target_id,
                        "account",
                        False,
                        "Bluesky login failed; verify optional dependency and app password",
                    )
                )

        if platform in {"instagram", "tiktok"}:
            media_url = target.get("public_base_url", "").rstrip("/")
            sample = next(
                (
                    item
                    for item in passed
                    if isinstance(item, dict)
                    and isinstance(item.get("output"), str)
                    and (root / item["output"]).is_file()
                ),
                None,
            )
            if not sample:
                results.append(
                    _check(
                        target_id,
                        "public_media",
                        True,
                        "skipped; no passed local asset available to probe",
                    )
                )
                continue
            media_name = Path(sample["output"]).name
            url = f"{media_url}/{quote(media_name)}"
            try:
                response = client.get(
                    url,
                    timeout=15,
                    stream=True,
                    allow_redirects=True,
                )
                response.raise_for_status()
                response.close()
                results.append(
                    _check(
                        target_id,
                        "public_media",
                        True,
                        "sample asset is reachable over HTTPS",
                    )
                )
            except (requests.RequestException, OSError):
                results.append(
                    _check(
                        target_id,
                        "public_media",
                        False,
                        "sample asset is not publicly reachable at the configured URL",
                    )
                )

    return results
