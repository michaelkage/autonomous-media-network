"""Optional S3-compatible public media storage for social publishing."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .publish import Publisher


class S3MediaStorage:
    """Upload media to S3-compatible storage under the public URL path prefix."""

    def __init__(
        self,
        bucket: str,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        session_token: str | None = None,
        client: Any = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3 bucket name is required")
        self.bucket = bucket
        self._uploaded: dict[tuple[str, int, int], str] = {}
        if client is not None:
            self.client = client
            return
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError('Install S3 storage support: pip install -e ".[s3]"') from exc
        if bool(access_key) != bool(secret_key):
            raise ValueError("S3 access key and secret key must be configured together")
        if session_token and not access_key:
            raise ValueError("S3 session token requires explicit access and secret keys")
        options: dict[str, Any] = {
            "service_name": "s3",
            "region_name": region,
            "endpoint_url": endpoint_url,
        }
        if access_key:
            options.update(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                aws_session_token=session_token,
            )
        self.client = boto3.client(**options)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> S3MediaStorage:
        def env(name: str, default: str) -> str | None:
            return os.environ.get(config.get(name, default))

        return cls(
            str(config["bucket"]),
            region=config.get("region") or os.environ.get("AWS_REGION"),
            endpoint_url=env("endpoint_url_env", "AMN_S3_ENDPOINT_URL"),
            access_key=env("access_key_env", "AWS_ACCESS_KEY_ID"),
            secret_key=env("secret_key_env", "AWS_SECRET_ACCESS_KEY"),
            session_token=env("session_token_env", "AWS_SESSION_TOKEN"),
        )

    @staticmethod
    def object_key(image_path: Path, public_base_url: str) -> str:
        parsed = urlparse(public_base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("public media URL must be HTTPS without query or fragment")
        prefix = unquote(parsed.path).strip("/")
        parts = prefix.split("/") if prefix else []
        if any(part in {".", ".."} for part in parts):
            raise ValueError("public media URL contains an unsafe object prefix")
        return "/".join([*parts, image_path.name])

    def upload(self, image_path: Path, public_base_url: str) -> str:
        image_path = Path(image_path).resolve()
        if not image_path.is_file():
            raise FileNotFoundError(f"media asset not found: {image_path}")
        key = self.object_key(image_path, public_base_url)
        stat = image_path.stat()
        cache_key = (str(image_path), stat.st_mtime_ns, stat.st_size)
        if self._uploaded.get(cache_key) == key:
            return key
        content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        self.client.upload_file(
            str(image_path),
            self.bucket,
            key,
            ExtraArgs={
                "ContentType": content_type,
                "CacheControl": "public, max-age=300",
            },
        )
        self._uploaded[cache_key] = key
        return key

    def check_access(self) -> None:
        """Raise if the configured bucket cannot be accessed with these credentials."""
        self.client.head_bucket(Bucket=self.bucket)


class StorageBackedPublisher(Publisher):
    """Upload each local asset before delegating to a URL-based publisher."""

    def __init__(
        self, publisher: Publisher, storage: S3MediaStorage, public_base_url: str
    ) -> None:
        self.publisher = publisher
        self.storage = storage
        self.public_base_url = public_base_url
        self.retry_after = None

    def publish(self, image_path: Path, caption: str) -> bool:
        try:
            self.storage.upload(image_path, self.public_base_url)
        except Exception:
            self.retry_after = None
            print("S3 media upload failed; verify bucket access and public URL mapping")
            return False
        result = self.publisher.publish(image_path, caption)
        self.retry_after = getattr(self.publisher, "retry_after", None)
        return result
