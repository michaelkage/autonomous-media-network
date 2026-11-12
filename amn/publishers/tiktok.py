"""TikTok Content Posting API photo publisher."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from ..publish import Publisher, RateLimiter, _retry_after_from_response

__all__ = ["TikTokPublisher"]

_TOKEN_LIMITERS_LOCK = threading.Lock()
_TOKEN_LIMITERS: dict[str, RateLimiter] = {}


def _limiter_for_token(access_token: str) -> RateLimiter:
    """Share TikTok's per-token request spacing across publisher instances."""
    token_key = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    with _TOKEN_LIMITERS_LOCK:
        return _TOKEN_LIMITERS.setdefault(token_key, RateLimiter(min_interval=10.0))


class SQLiteTokenRateLimiter:
    """Reserve request slots transactionally across local worker processes."""

    def __init__(self, database: Path, token_key: str, min_interval: float = 10.0) -> None:
        if min_interval <= 0:
            raise ValueError("min_interval must be positive")
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.token_key = token_key
        self.min_interval = min_interval

    def wait(self) -> None:
        with closing(sqlite3.connect(self.database, timeout=30, isolation_level=None)) as db:
            db.execute("PRAGMA busy_timeout = 30000")
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_rate_limits ("
                "token_key TEXT PRIMARY KEY, next_allowed_at REAL NOT NULL)"
            )
            db.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                row = db.execute(
                    "SELECT next_allowed_at FROM provider_rate_limits WHERE token_key = ?",
                    (self.token_key,),
                ).fetchone()
                slot = max(now, row[0] if row else now)
                db.execute(
                    "INSERT INTO provider_rate_limits(token_key, next_allowed_at) "
                    "VALUES (?, ?) ON CONFLICT(token_key) "
                    "DO UPDATE SET next_allowed_at = excluded.next_allowed_at",
                    (self.token_key, slot + self.min_interval),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


def _truncate_utf16(value: str, max_units: int) -> str:
    result: list[str] = []
    used = 0
    for character in value:
        units = 2 if ord(character) > 0xFFFF else 1
        if used + units > max_units:
            break
        result.append(character)
        used += units
    return "".join(result)


class TikTokPublisher(Publisher):
    """Publish a photo post from a verified, public HTTPS URL.

    TikTok's photo API uses PULL_FROM_URL, so the configured domain/prefix must
    be verified in the TikTok developer app. Direct posting requires video.publish.
    Request spacing is shared by access token across local worker processes when
    rate_limit_db is supplied. The default in-memory limiter is process-local.
    """

    API = "https://open.tiktokapis.com/v2"

    def __init__(
        self,
        access_token: str,
        public_base_url: str,
        privacy_level: str,
        *,
        direct_post_enabled: bool = False,
        timeout: float = 30.0,
        poll_interval: float = 5.0,
        max_polls: int = 24,
        rate_limit_db: Path | None = None,
    ) -> None:
        parsed = urlparse(public_base_url)
        if not access_token:
            raise ValueError("TikTok user access token is required")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("TikTok media base URL must be public HTTPS")
        if not privacy_level:
            raise ValueError("TikTok privacy_level must be explicitly configured")
        if not direct_post_enabled:
            raise ValueError("TikTok direct posting requires explicit direct_post_enabled=true")
        self.token, self.public_base_url = access_token, public_base_url.rstrip("/")
        self.privacy_level = privacy_level
        self.timeout, self.poll_interval, self.max_polls = timeout, poll_interval, max_polls
        self.session = requests.Session()
        token_key = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
        self.rate_limit_db = Path(rate_limit_db).resolve() if rate_limit_db else None
        self.rate_limiter = (
            SQLiteTokenRateLimiter(rate_limit_db, token_key)
            if rate_limit_db is not None
            else _limiter_for_token(access_token)
        )
        self.retry_after: float | None = None
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    def _request(self, endpoint: str, payload: dict) -> dict:
        self.rate_limiter.wait()
        response = self.session.post(
            f"{self.API}/{endpoint}/", headers=self.headers, json=payload, timeout=self.timeout
        )
        response.raise_for_status()
        result = response.json()
        error = result.get("error", {})
        if error.get("code") not in {None, "", "ok"}:
            raise RuntimeError(f"TikTok API {error.get('code')}: {error.get('message', '')}")
        return result

    def _operation_key(self, image_path: Path, photo_url: str, caption: str) -> str:
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        material = "\0".join(
            (self.headers["Authorization"], digest, photo_url, caption, self.privacy_level)
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _ensure_operations_table(self, db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS tiktok_publish_operations ("
            "operation_key TEXT PRIMARY KEY, publish_id TEXT NOT NULL, "
            "status TEXT NOT NULL, updated_at REAL NOT NULL, "
            "delivery_id TEXT, output TEXT)"
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(tiktok_publish_operations)")}
        if "delivery_id" not in columns:
            db.execute("ALTER TABLE tiktok_publish_operations ADD COLUMN delivery_id TEXT")
        if "output" not in columns:
            db.execute("ALTER TABLE tiktok_publish_operations ADD COLUMN output TEXT")

    def _load_operation(self, operation_key: str) -> tuple[str, str] | None:
        if self.rate_limit_db is None:
            return None
        with closing(sqlite3.connect(self.rate_limit_db, timeout=30)) as db:
            self._ensure_operations_table(db)
            row = db.execute(
                "SELECT publish_id, status FROM tiktok_publish_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def _save_operation(self, operation_key: str, publish_id: str, status: str) -> None:
        if self.rate_limit_db is None:
            return
        with closing(sqlite3.connect(self.rate_limit_db, timeout=30)) as db:
            db.execute("PRAGMA busy_timeout = 30000")
            self._ensure_operations_table(db)
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO tiktok_publish_operations "
                    "(operation_key, publish_id, status, updated_at, delivery_id, output) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(operation_key) DO UPDATE SET "
                    "publish_id = excluded.publish_id, status = excluded.status, "
                    "updated_at = excluded.updated_at, "
                    "delivery_id = COALESCE(excluded.delivery_id, "
                    "tiktok_publish_operations.delivery_id), "
                    "output = COALESCE(excluded.output, tiktok_publish_operations.output)",
                    (
                        operation_key,
                        publish_id,
                        status,
                        time.time(),
                        getattr(self, "_amn_delivery_id", None),
                        getattr(self, "_amn_delivery_output", None),
                    ),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _delete_operation(self, operation_key: str) -> None:
        if self.rate_limit_db is None:
            return
        with closing(sqlite3.connect(self.rate_limit_db, timeout=30)) as db:
            db.execute(
                "DELETE FROM tiktok_publish_operations WHERE operation_key = ?",
                (operation_key,),
            )
            db.commit()

    def publish(self, image_path: Path, caption: str) -> bool:
        self.retry_after = None
        if image_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            print("TikTok photo posts require a supported image")
            return False
        photo_url = f"{self.public_base_url}/{quote(image_path.name)}"
        operation_key = self._operation_key(image_path, photo_url, caption)
        try:
            operation = self._load_operation(operation_key)
            if operation and operation[1] == "complete":
                return True
            if operation and operation[1] == "failed":
                self._delete_operation(operation_key)
                operation = None

            if operation:
                publish_id = operation[0]
            else:
                creator = self._request("post/publish/creator_info/query", {})
                options = creator.get("data", {}).get("privacy_level_options", [])
                if self.privacy_level not in options:
                    raise RuntimeError(
                        f"configured privacy level {self.privacy_level!r} is not allowed: {options}"
                    )

                description = _truncate_utf16(caption, 4000)
                initialized = self._request(
                    "post/publish/content/init",
                    {
                        "post_info": {
                            "title": _truncate_utf16(description, 90),
                            "description": description,
                            "privacy_level": self.privacy_level,
                        },
                        "source_info": {
                            "source": "PULL_FROM_URL",
                            "photo_cover_index": 0,
                            "photo_images": [photo_url],
                        },
                        "post_mode": "DIRECT_POST",
                        "media_type": "PHOTO",
                    },
                )
                publish_id = initialized.get("data", {}).get("publish_id")
                if not publish_id:
                    raise RuntimeError("TikTok did not return a publish ID")
                # Persist immediately after TikTok accepts init. A restarted
                # worker can poll this operation instead of re-posting.
                self._save_operation(operation_key, str(publish_id), "pending")

            for attempt in range(self.max_polls):
                status = self._request("post/publish/status/fetch", {"publish_id": publish_id})
                state = status.get("data", {}).get("status")
                if state == "PUBLISH_COMPLETE":
                    self._save_operation(operation_key, publish_id, "complete")
                    return True
                if state == "FAILED":
                    self._delete_operation(operation_key)
                    raise RuntimeError(status.get("data", {}).get("fail_reason", "post failed"))
                if attempt + 1 < self.max_polls:
                    time.sleep(self.poll_interval)
            # Keep publish_id pending. Retries and later workers resume polling.
            raise RuntimeError("TikTok post is still processing; recovery will resume polling")
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            response = exc.response if isinstance(exc, requests.RequestException) else None
            self.retry_after = _retry_after_from_response(response)
            message = str(exc).replace(self.token, "[REDACTED]")[:300]
            print(f"TikTok publish failed: {message}")
            return False
