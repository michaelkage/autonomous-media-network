"""Loopback-only diagnostics dashboard with local configuration controls."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from .distribution import load_config, save_config, validate_config

ROOT = Path(os.environ.get("AMN_ROOT", Path.cwd())).resolve()


def create_app(root: Path = ROOT) -> Any:
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse
    except ModuleNotFoundError as exc:
        raise RuntimeError('Install dashboard dependencies: pip install -e ".[dashboard]"') from exc

    root = Path(root).resolve()
    config_path = root / "config" / "amn.json"
    app = FastAPI(title="AMN Diagnostics", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name in ("inbox", "outbox", "rejected", "build"):
            folder = root / name
            files = (
                sorted(p.name for p in folder.iterdir() if p.is_file())
                if folder.is_dir()
                else []
            )
            result[name] = {"count": len(files), "files": files[:200]}
        return result

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        config = load_config(config_path)
        for target in config.get("targets", []):
            for key in list(target):
                if key.lower() in {
                    "token", "access_token", "refresh_token", "password", "secret", "client_secret"
                }:
                    target.pop(key, None)
        return config

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
        from .pipeline import run
        result = run(root, watermark_text=os.environ.get("AMN_WATERMARK_TEXT", ""))
        return {"passed": len(result.passed), "rejected": len(result.rejected),
                "errors": result.errors}

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
