"""Instagram Graph API image publisher using public, HTTPS-hosted JPEGs."""

from __future__ import annotations

import hashlib
import math
import re
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
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError("Instagram access token is required")
        if not isinstance(instagram_user_id, str) or re.fullmatch(r"[0-9]+", instagram_user_id) is None:
            raise ValueError("Instagram user ID must contain only decimal digits")
        if not isinstance(api_version, str) or re.fullmatch(r"v[0-9]+\.[0-9]+", api_version) is None:
            raise ValueError("api_version must use the format vMAJOR.MINOR")
        try:
            valid_port = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError:
            valid_port = False
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not valid_port
        ):
            raise ValueError("Instagram media base URL must be public HTTPS without credentials, query, or fragment")
        if isinstance(max_polls, bool) or not isinstance(max_polls, int) or max_polls < 1:
            raise ValueError("max_polls must be a positive integer")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
            or isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or not math.isfinite(poll_interval)
            or poll_interval < 0
        ):
            raise ValueError(
                "timeout must be positive and poll_interval must be non-negative; "
                "both must be finite numbers"
            )
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

    def _record_history(
        self,
        operation_key: str,
        *,
        event_kind: str,
        container_id: str | None = None,
        container_status: str | None = None,
        phase: str | None = None,
        http_status: int | None = None,
        error_type: str | None = None,
        error_code: int | str | None = None,
        error_subcode: int | str | None = None,
        fbtrace_id: str | None = None,
        message: str | None = None,
    ) -> None:
        if self.operation_db is None:
            return
        self.operation_db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.operation_db, timeout=30)) as db:
            db.execute("PRAGMA busy_timeout = 30000")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS instagram_publish_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_key TEXT NOT NULL,
                    container_id TEXT,
                    event_kind TEXT NOT NULL,
                    container_status TEXT,
                    phase TEXT,
                    http_status INTEGER,
                    error_type TEXT,
                    error_code TEXT,
                    error_subcode TEXT,
                    fbtrace_id TEXT,
                    message TEXT,
                    delivery_id TEXT,
                    output TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT INTO instagram_publish_history (
                    operation_key, container_id, event_kind, container_status,
                    phase, http_status, error_type, error_code, error_subcode,
                    fbtrace_id, message, delivery_id, output, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_key,
                    container_id,
                    event_kind,
                    container_status,
                    phase,
                    http_status,
                    str(error_type)[:120] if error_type is not None else None,
                    str(error_code)[:80] if error_code is not None else None,
                    str(error_subcode)[:80] if error_subcode is not None else None,
                    str(fbtrace_id)[:120] if fbtrace_id is not None else None,
                    message[:500] if message else None,
                    getattr(self, "_amn_delivery_id", None),
                    getattr(self, "_amn_delivery_output", None),
                    time.time(),
                ),
            )
            db.commit()

    def _record_provider_error(
        self, operation_key: str, container_id: str | None, phase: str, exc: Exception
    ) -> str:
        response = exc.response if isinstance(exc, requests.RequestException) else None
        payload: dict = {}
        if response is not None:
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("error"), dict):
                    payload = body["error"]
            except (ValueError, TypeError):
                pass
        message = str(payload.get("message") or type(exc).__name__)
        if self.token:
            message = message.replace(self.token, "[REDACTED]")
        message = message.replace("\\n", " ")[:500]
        error_type = payload.get("type")
        error_code = payload.get("code")
        error_subcode = payload.get("error_subcode")
        fbtrace_id = payload.get("fbtrace_id")
        http_status = getattr(response, "status_code", None)
        self._record_history(
            operation_key,
            event_kind="provider_error",
            container_id=container_id,
            phase=phase,
            http_status=http_status,
            error_type=str(error_type) if error_type is not None else None,
            error_code=error_code,
            error_subcode=error_subcode,
            fbtrace_id=str(fbtrace_id) if fbtrace_id is not None else None,
            message=message,
        )
        details = f"HTTP {http_status}" if http_status is not None else type(exc).__name__
        if error_code is not None:
            details += f", code={error_code}"
        if error_subcode is not None:
            details += f", subcode={error_subcode}"
        if error_type:
            details += f", type={error_type}"
        return f"Instagram {phase} failed ({details}): {message}"

    def _delete_operation(self, operation_key: str) -> None:
        if self.operation_db is None:
            return
        with closing(sqlite3.connect(self.operation_db, timeout=30)) as db:
            row = db.execute(
                "SELECT container_id, status FROM instagram_publish_operations "
                "WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row is not None:
                self._record_history(
                    operation_key,
                    event_kind="container_replaced",
                    container_id=str(row[0]),
                    container_status=str(row[1]),
                    message="Terminal operation archived before retry with a new container",
                )
            db.execute(
                "DELETE FROM instagram_publish_operations WHERE operation_key = ?",
                (operation_key,),
            )
            db.commit()

    @staticmethod
    def _response_object(response: requests.Response, phase: str) -> dict:
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Instagram returned an invalid {phase} response")
        return payload

    def _container_status(self, container_id: str) -> str:
        response = self.session.get(
            f"{self.base}/{container_id}",
            params={"fields": "status_code", "access_token": self.token},
            timeout=self.timeout,
        )
        response.raise_for_status()
        status = self._response_object(response, "container status").get("status_code")
        if not isinstance(status, str) or not status:
            raise RuntimeError("Instagram did not return a media container status")
        return status

    def publish(self, image_path: Path, caption: str) -> bool:
        self.retry_after = None
        image_path = Path(image_path)
        operation_key: str | None = None
        container_id: str | None = None
        phase = "media_container_create"
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
                phase = "media_container_create"
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
                container_id = self._response_object(created, "media container").get("id")
                if not isinstance(container_id, (str, int)) or isinstance(container_id, bool):
                    raise RuntimeError("Instagram did not return a valid media container ID")
                container_id = str(container_id)
                if not container_id:
                    raise RuntimeError("Instagram did not return a valid media container ID")
                self._save_operation(operation_key, container_id, "processing")
                operation_status = "processing"
            else:
                container_id, operation_status = operation

            phase = "container_status"
            status = self._container_status(container_id)
            if status == "PUBLISHED":
                self._save_operation(operation_key, container_id, "complete")
                return True
            if status in {"ERROR", "EXPIRED"}:
                self._record_history(
                    operation_key,
                    event_kind="container_terminal",
                    container_id=container_id,
                    container_status=status,
                    phase="container_status",
                    message=f"Instagram media container reached terminal status {status}",
                )
                self._save_operation(operation_key, container_id, "failed")
                raise RuntimeError(f"Instagram container status: {status}")
            if status == "IN_PROGRESS":
                if operation_status != "publishing":
                    self._save_operation(operation_key, container_id, "processing")
                for _ in range(self.max_polls - 1):
                    time.sleep(self.poll_interval)
                    phase = "container_status"
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
            phase = "media_publish"
            published = self.session.post(
                f"{self.base}/{self.user_id}/media_publish",
                data={"creation_id": container_id, "access_token": self.token},
                timeout=self.timeout,
            )
            published.raise_for_status()
            published_id = self._response_object(published, "media publication").get("id")
            if not isinstance(published_id, (str, int)) or isinstance(published_id, bool):
                raise RuntimeError("Instagram did not confirm media publication")
            self._save_operation(operation_key, container_id, "complete")
            return True
        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
            response = exc.response if isinstance(exc, requests.RequestException) else None
            self.retry_after = _retry_after_from_response(response)
            if operation_key is not None and isinstance(exc, requests.RequestException):
                try:
                    detail = self._record_provider_error(operation_key, container_id, phase, exc)
                except (OSError, sqlite3.Error):
                    detail = f"Instagram {phase} failed: {type(exc).__name__}"
            else:
                detail = str(exc).replace(self.token, "[REDACTED]") if self.token else str(exc)
            print(f"Instagram publish failed: {detail}")
            return False
