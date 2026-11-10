from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from amn.diagnostics import run_diagnostics


def _response(payload: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _workspace(tmp_path: Path, config: dict) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "outbox").mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "outbox" / "asset.jpg").write_bytes(b"image")
    (tmp_path / "config" / "amn.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "build" / "manifest.json").write_text(
        json.dumps({"passed": [{"output": "outbox/asset.jpg"}]}), encoding="utf-8"
    )


def test_diagnostics_checks_instagram_account_and_public_media(tmp_path: Path, monkeypatch) -> None:
    _workspace(
        tmp_path,
        {
            "targets": [
                {
                    "id": "ig",
                    "platform": "instagram",
                    "enabled": True,
                    "instagram_user_id": "12345",
                    "public_base_url": "https://media.example.com/outbox",
                }
            ],
            "routes": {},
        },
    )
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "secret")
    session = MagicMock()
    session.get.side_effect = [
        _response({"id": "12345", "username": "amn"}),
        _response({"ok": True}),
    ]

    results = run_diagnostics(tmp_path, session=session)

    assert [item["check"] for item in results] == ["account", "public_media"]
    assert all(item["ok"] for item in results)
    assert session.get.call_args_list[0].kwargs["params"]["access_token"] == "secret"
    assert session.get.call_args_list[1].args[0] == "https://media.example.com/outbox/asset.jpg"


def test_diagnostics_checks_tiktok_scope_and_account_privacy(tmp_path: Path, monkeypatch) -> None:
    _workspace(
        tmp_path,
        {
            "targets": [
                {
                    "id": "tt",
                    "platform": "tiktok",
                    "enabled": True,
                    "public_base_url": "https://media.example.com/outbox",
                    "privacy_level": "SELF_ONLY",
                    "direct_post_enabled": True,
                }
            ],
            "routes": {},
        },
    )
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "secret")
    session = MagicMock()
    session.post.return_value = _response(
        {
            "data": {"privacy_level_options": ["SELF_ONLY"]},
            "error": {"code": "ok"},
        }
    )
    session.get.return_value = _response({"ok": True})

    results = run_diagnostics(tmp_path, session=session)

    assert [item["check"] for item in results] == ["account", "public_media"]
    assert all(item["ok"] for item in results)
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"


def test_diagnostics_reports_missing_telegram_credentials(tmp_path: Path, monkeypatch) -> None:
    _workspace(
        tmp_path,
        {
            "targets": [
                {
                    "id": "tg",
                    "platform": "telegram",
                    "enabled": True,
                    "chat_id": "@channel",
                }
            ],
            "routes": {},
        },
    )
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    results = run_diagnostics(tmp_path, session=MagicMock())

    assert results == [
        {
            "target": "tg",
            "check": "credentials",
            "ok": False,
            "detail": "bot token or chat ID missing",
        }
    ]


def test_diagnostics_skips_disabled_targets_without_network_calls(tmp_path: Path) -> None:
    _workspace(
        tmp_path,
        {"targets": [{"id": "ig", "platform": "instagram", "enabled": False}], "routes": {}},
    )
    session = MagicMock()

    results = run_diagnostics(tmp_path, session=session)

    assert results[0]["ok"] is True
    session.get.assert_not_called()
    session.post.assert_not_called()
