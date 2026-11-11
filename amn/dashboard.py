"""Loopback-only diagnostics dashboard with local configuration controls."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .distribution import load_config, save_config, validate_config

ROOT = Path(os.environ.get("AMN_ROOT", Path.cwd())).resolve()
_SECRET_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "password",
    "app_password",
    "secret",
    "client_secret",
    "api_key",
    "private_key",
    "bot_token",
}


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_secrets(item)
            for key, item in value.items()
            if key.lower() not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def create_app(root: Path = ROOT) -> Any:
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import HTMLResponse
    except ModuleNotFoundError as exc:
        raise RuntimeError('Install dashboard dependencies: pip install -e ".[dashboard]"') from exc

    root = Path(root).resolve()
    config_path = root / "config" / "amn.json"
    app = FastAPI(title="AMN Diagnostics", docs_url=None, redoc_url=None)
    pipeline_lock = threading.Lock()

    @app.middleware("http")
    async def enforce_local_origin(request: Request, call_next: Any) -> Any:
        host_header = request.headers.get("host", "")
        host = urlsplit(f"//{host_header}").hostname
        if host not in {"127.0.0.1", "localhost", "::1"}:
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "loopback host required"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            parsed_origin = urlsplit(origin)
            if (
                parsed_origin.scheme not in {"http", "https"}
                or parsed_origin.netloc.lower() != host_header.lower()
            ):
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name in ("inbox", "outbox", "rejected", "build"):
            folder = root / name
            files = (
                sorted(p.name for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []
            )
            result[name] = {"count": len(files), "files": files[:200]}
        manifest = root / "build" / "manifest.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                result["recent_rejections"] = data.get("rejected", [])[-100:]
            except (OSError, ValueError):
                result["recent_rejections"] = [{"error": "manifest is unreadable"}]
        else:
            result["recent_rejections"] = []
        jobs_database = root / "build" / "jobs.sqlite3"
        if jobs_database.is_file():
            from .jobs import JobQueue

            queue = JobQueue(jobs_database)
            result["jobs"] = {
                "counts": queue.counts(),
                "recent": [
                    {
                        "id": job["id"],
                        "kind": job["kind"],
                        "status": job["status"],
                        "attempts": job["attempts"],
                        "max_attempts": job["max_attempts"],
                        "last_error": job["last_error"],
                        "created_at": job["created_at"],
                        "updated_at": job["updated_at"],
                    }
                    for job in queue.list(limit=20)
                ],
            }
            schedules = queue.list_schedules()
            result["schedules"] = {
                "counts": {
                    "enabled": sum(schedule["enabled"] for schedule in schedules),
                    "disabled": sum(not schedule["enabled"] for schedule in schedules),
                },
                "recent": [
                    {
                        "id": schedule["id"],
                        "kind": schedule["kind"],
                        "enabled": schedule["enabled"],
                        "interval_seconds": schedule["interval_seconds"],
                        "next_run_at": schedule["next_run_at"],
                        "last_enqueued_job_id": schedule["last_enqueued_job_id"],
                        "created_at": schedule["created_at"],
                        "updated_at": schedule["updated_at"],
                    }
                    for schedule in schedules[:20]
                ],
            }
        else:
            result["jobs"] = {
                "counts": {"pending": 0, "running": 0, "succeeded": 0, "failed": 0},
                "recent": [],
            }
            result["schedules"] = {
                "counts": {"enabled": 0, "disabled": 0},
                "recent": [],
            }
        return result

    @app.get("/api/deliveries")
    def delivery_history(limit: int = 100, stale_after_seconds: int = 900) -> dict[str, Any]:
        from .delivery_events import delivery_event_snapshot

        try:
            return delivery_event_snapshot(
                root / "build" / "jobs.sqlite3",
                limit=limit,
                stale_after_seconds=stale_after_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/reddit-devvit/status")
    def reddit_devvit_status(limit: int = 25) -> dict[str, Any]:
        from .delivery_events import reddit_devvit_delivery_ids

        if isinstance(limit, bool) or not 1 <= limit <= 50:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 50")

        publish_url = os.environ.get("AMN_REDDIT_DEVVIT_URL", "")
        status_url = os.environ.get("AMN_REDDIT_DEVVIT_STATUS_URL", "")
        token = os.environ.get("AMN_REDDIT_DEVVIT_TOKEN", "")
        if not status_url and publish_url:
            parsed = urlsplit(publish_url)
            if parsed.path.endswith("/external/amn/publish"):
                status_url = parsed._replace(
                    path=parsed.path.removesuffix("/publish") + "/status",
                    query="",
                    fragment="",
                ).geturl()

        parsed_status = urlsplit(status_url)
        if (
            parsed_status.scheme != "https"
            or not parsed_status.hostname
            or parsed_status.username
            or parsed_status.password
            or parsed_status.query
            or parsed_status.fragment
            or not parsed_status.path.endswith("/external/amn/status")
            or not token.startswith("devvit_at_")
        ):
            return {
                "configured": False,
                "status": "not_configured",
                "deliveries": [],
            }

        delivery_ids = reddit_devvit_delivery_ids(
            root / "build" / "jobs.sqlite3",
            limit=limit,
        )
        if not delivery_ids:
            return {"configured": True, "status": "ok", "deliveries": []}

        import requests

        try:
            response = requests.post(
                status_url,
                headers={
                    "Authorization": f"bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"delivery_ids": delivery_ids},
                timeout=5,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise HTTPException(
                status_code=502,
                detail="Reddit Devvit provider status is temporarily unavailable",
            ) from exc

        if (
            not isinstance(payload, dict)
            or payload.get("status") != "ok"
            or not isinstance(payload.get("deliveries"), list)
        ):
            raise HTTPException(
                status_code=502,
                detail="Reddit Devvit provider returned an invalid status response",
            )

        allowed_ids = set(delivery_ids)
        deliveries = []
        for item in payload["deliveries"]:
            if not isinstance(item, dict):
                continue
            delivery_id = item.get("delivery_id")
            if not isinstance(delivery_id, str) or delivery_id not in allowed_ids:
                continue
            status = item.get("status")
            post_id = item.get("post_id")
            permalink = item.get("permalink")
            updated_at = item.get("updated_at")
            reconciliation = item.get("reconciliation")
            reconciliation_result = None
            if isinstance(reconciliation, dict):
                resolution = reconciliation.get("resolution")
                resolved_at = reconciliation.get("resolved_at")
                actor = reconciliation.get("actor")
                if resolution == "published" or resolution == "not_published":
                    reconciliation_result = {
                        "resolution": resolution,
                        "resolved_at": resolved_at if isinstance(resolved_at, str) else None,
                        "actor": actor if isinstance(actor, str) else None,
                    }
            deliveries.append(
                {
                    "delivery_id": delivery_id,
                    "status": status if isinstance(status, str) and status in {
                        "uploading",
                        "submitting",
                        "published",
                        "unknown",
                        "not_found",
                    } else "invalid",
                    "post_id": post_id if isinstance(post_id, str) else None,
                    "permalink": (
                        permalink
                        if isinstance(permalink, str) and permalink.startswith("/r/")
                        else None
                    ),
                    "updated_at": updated_at if isinstance(updated_at, str) else None,
                    "reconciliation": reconciliation_result,
                }
            )
        return {"configured": True, "status": "ok", "deliveries": deliveries}

    @app.get("/api/alerts")
    def operator_alerts(limit: int = 50) -> dict[str, Any]:
        from .operator_alerts import operator_alert_snapshot

        try:
            return operator_alert_snapshot(root / "build" / "jobs.sqlite3", limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return _redact_secrets(load_config(config_path))

    @app.put("/api/config")
    def put_config(payload: dict[str, Any]) -> dict[str, str]:
        try:
            validate_config(payload)
            save_config(config_path, payload)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "saved"}

    @app.post("/api/run")
    def run_pipeline() -> dict[str, Any]:
        if not pipeline_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="pipeline is already running")
        try:
            from .pipeline import run

            result = run(root, watermark_text=os.environ.get("AMN_WATERMARK_TEXT", ""))
            return {
                "passed": len(result.passed),
                "rejected": len(result.rejected),
                "errors": result.errors,
            }
        finally:
            pipeline_lock.release()

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local AMN diagnostics dashboard")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("dashboard binds to loopback only; use a separately secured reverse proxy")
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise SystemExit('Install dashboard dependencies: pip install -e ".[dashboard]"') from exc
    uvicorn.run(create_app(args.root), host=args.host, port=args.port)
