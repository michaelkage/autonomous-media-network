"""Instagram Graph API image publisher using public, HTTPS-hosted JPEGs."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from ..publish import Publisher, _retry_after_from_response

__all__ = ["InstagramPublisher"]

_MAX_IMAGE_BYTES = 8 * 1024 * 1024


class InstagramPublisher(Publisher):
    """Publish a JPEG through Meta's container flow with durable local recovery.

    Meta fetches media from a public URL. When operation_db is configured, the
    container ID and lifecycle state are persisted before subsequent API calls.
    """

    def __init__(
        self,
        access_token: str,
        instagram_user_id: str,
        public_base_url: str,
        *,
        api_version: str = "v25.0",
        timeout: float = 30.0,
        poll_interval: float = 60.0,
        max_polls: int = 5,
        operation_db: Path | None = None,
    ) -> None:
        parsed = urlparse(public_base_url)
        if not access_token or not instagram_user_id:
            raise ValueError("Instagram access token and user ID are required")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Instagram media base URL must be public HTTPS")
        if isinstance(max_polls, bool) or not isinstance(max_polls, int) or max_polls < 1:
            raise ValueError("max_polls must be a positive integer")
        if poll_interval < 0 or timeout <= 0:
            raise ValueError("timeout must be positive and poll_interval cannot be negative")
        self.token, self.user_id = access_token, instagram_user_id
        self.public_base_url = public_base_url.rstrip("/")
        self.base = f"https://graph.facebook.com/{api_version}"
        self.timeout, self.poll_interval, self.max_polls = timeout, poll_interval, max_polls
        self.operation_db = Path(operation_db).resolve() if operation_db is not None else None
        self.session = requests.Session()
        self.retry_after: float | None = None

    def _operation_key(self, image_path: Path, media_url: str, caption: str) -> str:
        token_key = hashlib.sha256(self.token.encode("utf-8")).hexdigest()
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        material = "\0".join((token_key, self.user_id, digest, media_url, caption))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _ensure_operations_table(self, db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS instagram_publish_operations ("
            "operation_key TEXT PRIMARY KEY, container_id TEXT NOT NULL, "
            "status TEXT NOT NULL, updated_at REAL NOT NULL, "
            "delivery_id TEXT, output TEXT)"
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(instagram_publish_operations)")}
        if "delivery_id" not in columns:
            db.execute("ALTER TABLE instagram_publish_operations ADD COLUMN delivery_id TEXT")
        if "output" not in columns:
            db.execute("ALTER TABLE instagram_publish_operations ADD COLUMN output TEXT")

    def _load_operation(self, operation_key: str) -> tuple[str, str] | None:
        if self.operation_db is None or not self.operation_db.is_file():
            return None
        with closing(sqlite3.connect(self.operation_db, timeout=30)) as db:
            self._ensure_operations_table(db)
            row = db.execute(
                "SELECT container_id, status FROM instagram_publish_operations "
                "WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def _save_operation(self, operation_key: str, container_id: str, status: str) -> None:
        if self.operation_db is None:
            return
        self.operation_db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.operation_db, timeout=30)) as db:
            db.execute("PRAGMA busy_timeout = 30000")
            self._ensure_operations_table(db)
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO instagram_publish_operations "
                    "(operation_key, container_id, status, updated_at, delivery_id, output) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(operation_key) DO UPDATE SET "
                    "container_id = excluded.container_id, status = excluded.status, "
                    "updated_at = excluded.updated_at, "
                    "delivery_id = COALESCE(excluded.delivery_id, "
                    "instagram_publish_operations.delivery_id), "
                    "output = COALESCE(excluded.output, instagram_publish_operations.output)",
                    (
                        operation_key,
                        container_id,
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
        if self.operation_db is None:
            return
        with closing(sqlite3.connect(self.operation_db, timeout=30)) as db:
            db.execute(
                "DELETE FROM instagram_publish_operations WHERE operation_key = ?",
                (operation_key,),
            )
            db.commit()

    def _container_status(self, container_id: str) -> str:
        response = self.session.get(
            f"{self.base}/{container_id}",
            params={"fields": "status_code", "access_token": self.token},
            timeout=self.timeout,
        )
        response.raise_for_status()
        status = response.json().get("status_code")
        if not isinstance(status, str) or not status:
            raise RuntimeError("Instagram did not return a media container status")
        return status

    def publish(self, image_path: Path, caption: str) -> bool:
        self.retry_after = None
        image_path = Path(image_path)
        if image_path.suffix.lower() not in {".jpg", ".jpeg"}:
            print("Instagram requires JPEG images")
            return False
        try:
            if image_path.stat().st_size > _MAX_IMAGE_BYTES:
                raise ValueError("Instagram image uploads are limited to 8 MiB")
            media_url = f"{self.public_base_url}/{quote(image_path.name)}"
            operation_key = self._operation_key(image_path, media_url, caption)
            operation = self._load_operation(operation_key)

            if operation and operation[1] == "complete":
                return True
            if operation and operation[1] == "failed":
                self._delete_operation(operation_key)
                operation = None

            if operation is None:
                created = self.session.post(
                    f"{self.base}/{self.user_id}/media",
                    data={
                        "image_url": media_url,
                        "caption": caption[:2200],
                        "access_token": self.token,
                    },
                    timeout=self.timeout,
                )
                created.raise_for_status()
                container_id = created.json().get("id")
                if not container_id:
                    raise RuntimeError("Instagram did not return a media container ID")
                self._save_operation(operation_key, str(container_id), "processing")
                operation_status = "processing"
            else:
                container_id, operation_status = operation

            status = self._container_status(container_id)
            if status == "PUBLISHED":
                self._save_operation(operation_key, container_id, "complete")
                return True
            if status in {"ERROR", "EXPIRED"}:
                self._save_operation(operation_key, container_id, "failed")
                raise RuntimeError(f"Instagram container status: {status}")
            if status == "IN_PROGRESS":
                if operation_status != "publishing":
                    self._save_operation(operation_key, container_id, "processing")
                for _ in range(self.max_polls - 1):
                    time.sleep(self.poll_interval)
                    status = self._container_status(container_id)
                    if status != "IN_PROGRESS":
                        break
                if status == "IN_PROGRESS":
                    raise RuntimeError(
                        "Instagram media container is still processing; recovery will resume "
                        "with the existing container"
                    )
            if status not in {"FINISHED", "PUBLISHED"}:
                raise RuntimeError(f"Unexpected Instagram container status: {status}")
            if status == "PUBLISHED":
                self._save_operation(operation_key, container_id, "complete")
                return True

            # A prior publishing state means the POST outcome may have been lost.
            # Never repeat media_publish while Meta still reports FINISHED.
            if operation_status == "publishing":
                raise RuntimeError(
                    "Instagram publish outcome is ambiguous; container remains FINISHED. "
                    "Manual reconciliation is required before retrying publication"
                )

            self._save_operation(operation_key, container_id, "publishing")
            published = self.session.post(
                f"{self.base}/{self.user_id}/media_publish",
                data={"creation_id": container_id, "access_token": self.token},
                timeout=self.timeout,
            )
            published.raise_for_status()
            if not published.json().get("id"):
                raise RuntimeError("Instagram did not confirm media publication")
            self._save_operation(operation_key, container_id, "complete")
            return True
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            response = exc.response if isinstance(exc, requests.RequestException) else None
            self.retry_after = _retry_after_from_response(response)
            print(f"Instagram publish failed: {exc}")
            return False
