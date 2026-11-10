from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from amn.dashboard import create_app


def test_dashboard_serves_ui_and_workspace_status(tmp_path: Path) -> None:
    for folder in ("inbox", "outbox", "rejected", "build"):
        (tmp_path / folder).mkdir()
    (tmp_path / "inbox" / "queued.jpg").write_bytes(b"asset")

    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    page = client.get("/")
    status = client.get("/api/status")

    assert page.status_code == 200
    assert "Autonomous Media Network" in page.text
    assert status.json()["inbox"]["count"] == 1


def test_dashboard_blocks_cross_origin_mutations(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    response = client.put(
        "/api/config",
        headers={"Origin": "https://attacker.example"},
        json={"targets": [], "routes": {}, "thresholds": {"min_likes": 500, "max_distance": 5}},
    )
    assert response.status_code == 403
    assert not (tmp_path / "config" / "amn.json").exists()


def test_dashboard_rejects_non_loopback_host(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), base_url="http://attacker.example")
    assert client.get("/api/status").status_code == 403


def test_dashboard_saves_only_valid_configuration(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    invalid = client.put(
        "/api/config",
        json={
            "targets": [{"id": "telegram", "platform": "telegram", "token": "plaintext-secret"}],
            "routes": {},
            "thresholds": {"min_likes": 500, "max_distance": 5},
        },
    )
    valid = client.put(
        "/api/config",
        json={
            "sources": {"reddit": ["earthporn"]},
            "targets": [],
            "routes": {"default": []},
            "thresholds": {"min_likes": 500, "max_distance": 5},
        },
    )

    assert invalid.status_code == 400
    assert valid.status_code == 200
    saved = json.loads((tmp_path / "config" / "amn.json").read_text(encoding="utf-8"))
    assert saved["sources"]["reddit"] == ["earthporn"]
