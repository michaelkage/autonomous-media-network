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
        return """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AMN Diagnostics</title>
<style>
body{font:16px system-ui;max-width:900px;margin:40px auto;padding:0 18px;background:#10151d;color:#e8edf4}
section{background:#1b2532;padding:18px;border-radius:12px;margin:14px 0}
label{display:block;margin:14px 0}input{width:100%}button{padding:10px 16px;border:0;border-radius:8px;cursor:pointer}
pre{white-space:pre-wrap}
</style></head><body><h1>Autonomous Media Network</h1>
<section><h2>Workspace</h2><pre id="status">Loading…</pre></section>
<section><h2>Curation controls</h2>
<label>Minimum likes: <b id="likesValue">500</b><input id="likes" type="range" min="0" max="10000" step="50"></label>
<label>Maximum dHash distance: <b id="distanceValue">5</b><input id="distance" type="range" min="0" max="64"></label>
<h3>Sources, routes and target accounts</h3>
<p>Configure target IDs, platform names, handles/chat IDs, environment-variable credential references, source lists and category routes. Secret values stay in the process environment.</p>
<textarea id="configEditor" rows="18" style="width:100%;font:13px monospace;background:#0b1017;color:#e8edf4"></textarea>
<button onclick="saveConfig()">Save configuration</button> <button onclick="runPipeline()">Run pipeline</button>
<pre id="message"></pre></section>
<script>
let config;
async function load(){const s=await fetch("/api/status");document.querySelector("#status").textContent=JSON.stringify(await s.json(),null,2);
config=await(await fetch("/api/config")).json();configEditor.value=JSON.stringify(config,null,2);likes.value=config.thresholds.min_likes??500;distance.value=config.thresholds.max_distance??5;likesValue.textContent=likes.value;distanceValue.textContent=distance.value}
likes.oninput=()=>likesValue.textContent=likes.value;distance.oninput=()=>distanceValue.textContent=distance.value;
async function saveConfig(){try{config=JSON.parse(configEditor.value);config.thresholds={...config.thresholds,min_likes:+likes.value,max_distance:+distance.value};
const r=await fetch("/api/config",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(config)});message.textContent=await r.text();if(r.ok)configEditor.value=JSON.stringify(config,null,2)}catch(e){message.textContent="Invalid JSON: "+e.message}}
async function runPipeline(){const r=await fetch("/api/run",{method:"POST"});message.textContent=await r.text();load()}load();
</script></body></html>"""

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name in ("inbox", "outbox", "rejected", "build"):
            folder = root / name
            files = sorted(p.name for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []
            result[name] = {"count": len(files), "files": files[:200]}
        return result

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        config = load_config(config_path)
        for target in config.get("targets", []):
            for key in list(target):
                if key.lower() in {"token", "access_token", "refresh_token", "password", "secret", "client_secret"}:
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
