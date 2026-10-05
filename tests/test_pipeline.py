"""End-to-end pipeline behaviour: routing, manifests, failure handling."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from amn.pipeline import RunResult, load_thresholds, main, run
from amn.vetting import Thresholds


def read_manifest(root: Path) -> dict:
    return json.loads((root / "build" / "manifest.json").read_text(encoding="utf-8"))


class TestPassing:
    def test_passing_asset_is_watermarked_into_the_outbox(self, project, add_asset):
        add_asset(size=(1200, 1200))

        result = run(project, watermark_text="@amn")

        assert [item["source"] for item in result.passed] == ["photo.jpg"]
        output = project / "outbox" / "photo.jpg"
        assert output.is_file()
        with Image.open(output) as image:
            assert image.format == "JPEG"
            assert image.size == (1200, 1200)

    def test_inbox_drains_so_reruns_are_idempotent(self, project, add_asset):
        add_asset()
        run(project, watermark_text="@amn")
        assert list((project / "inbox").iterdir()) == []

        second = run(project, watermark_text="@amn")
        assert second.passed == []
        assert read_manifest(project)["counts"] == {"passed": 0, "rejected": 0, "errors": 0}

    def test_manifest_records_provenance_and_digest(self, project, add_asset):
        add_asset(metrics={"views": 10_000, "likes": 500})

        run(project, watermark_text="@amn")

        entry = read_manifest(project)["passed"][0]
        assert entry["source"] == "photo.jpg"
        assert entry["output"] == "outbox/photo.jpg"
        assert entry["engagement_rate"] == 0.05
        assert len(entry["sha256"]) == 64

    def test_watermark_actually_changes_the_pixels(self, project, add_asset):
        source = add_asset(size=(1200, 1200), color=(10, 10, 10))
        with Image.open(source) as before:
            before_rgb = before.convert("RGB")

        run(project, watermark_text="@amn")

        with Image.open(project / "outbox" / "photo.jpg") as after:
            assert before_rgb.tobytes() != after.tobytes()


class TestRejecting:
    def test_low_resolution_is_hard_rejected(self, project, add_asset):
        add_asset(size=(400, 400))

        result = run(project, watermark_text="@amn")

        assert result.passed == []
        assert "resolution 400x400" in result.rejected[0]["reasons"][0]
        assert not (project / "outbox").exists()
        assert (project / "rejected" / "photo.jpg").is_file()

    def test_low_engagement_is_hard_rejected(self, project, add_asset):
        add_asset(metrics={"views": 10_000, "likes": 5})

        result = run(project, watermark_text="@amn")

        assert result.passed == []
        assert any("engagement rate" in reason for reason in result.rejected[0]["reasons"])

    def test_missing_metrics_are_hard_rejected(self, project, add_asset):
        add_asset(write_metrics=False)

        result = run(project, watermark_text="@amn")

        assert result.passed == []
        assert any("no engagement metrics" in reason for reason in result.rejected[0]["reasons"])

    def test_broken_metrics_manifest_is_rejected_not_crashed(self, project, add_asset):
        source = add_asset()
        source.with_suffix(".json").write_text("{ not json", encoding="utf-8")

        result = run(project, watermark_text="@amn")

        assert result.errors == []
        assert any("unreadable" in reason for reason in result.rejected[0]["reasons"])

    def test_rejected_asset_keeps_its_metrics_for_audit(self, project, add_asset):
        add_asset(size=(400, 400))

        run(project, watermark_text="@amn")

        assert (project / "rejected" / "photo.json").is_file()

    def test_mixed_batches_are_split_correctly(self, project, add_asset):
        add_asset(name="good.jpg", size=(1200, 1200))
        add_asset(name="small.jpg", size=(320, 320))

        result = run(project, watermark_text="@amn")

        assert [item["source"] for item in result.passed] == ["good.jpg"]
        assert [item["source"] for item in result.rejected] == ["small.jpg"]
        assert (project / "outbox" / "good.jpg").is_file()
        assert (project / "rejected" / "small.jpg").is_file()


class TestFailureHandling:
    def test_undecodable_file_is_reported_and_retired(self, project):
        (project / "inbox" / "corrupt.jpg").write_bytes(b"this is not an image")

        result = run(project, watermark_text="@amn")

        assert len(result.errors) == 1
        assert "corrupt.jpg" in result.errors[0]
        assert (project / "rejected" / "corrupt.jpg").is_file()
        assert list((project / "inbox").iterdir()) == []

    def test_one_corrupt_file_does_not_block_the_batch(self, project, add_asset):
        (project / "inbox" / "corrupt.jpg").write_bytes(b"nope")
        add_asset(name="good.jpg")

        result = run(project, watermark_text="@amn")

        assert [item["source"] for item in result.passed] == ["good.jpg"]
        assert len(result.errors) == 1

    def test_missing_inbox_is_an_error_not_a_crash(self, tmp_path):
        result = run(tmp_path)
        assert result.errors and "inbox" in result.errors[0]

    def test_non_image_files_in_the_inbox_are_ignored(self, project):
        (project / "inbox" / "notes.txt").write_text("ignore me", encoding="utf-8")

        result = run(project, watermark_text="@amn")

        assert result == RunResult()
        assert (project / "inbox" / "notes.txt").is_file()


class TestDryRun:
    def test_nothing_on_disk_changes(self, project, add_asset):
        add_asset(size=(1200, 1200))

        result = run(project, watermark_text="@amn", dry_run=True)

        assert [item["source"] for item in result.passed] == ["photo.jpg"]
        assert (project / "inbox" / "photo.jpg").is_file()
        assert (project / "inbox" / "photo.json").is_file()
        assert not (project / "outbox").exists()
        assert not (project / "build").exists()


class TestConfig:
    def test_thresholds_load_from_pyproject(self, project):
        (project / "pyproject.toml").write_text(
            "[tool.amn]\nmin_width = 200\nmin_height = 200\nmin_engagement_rate = 0.5\n",
            encoding="utf-8",
        )
        thresholds = load_thresholds(project)
        assert (thresholds.min_width, thresholds.min_engagement_rate) == (200, 0.5)

    def test_thresholds_default_when_project_is_absent(self, tmp_path):
        assert load_thresholds(tmp_path) == Thresholds()

    def test_configured_thresholds_are_enforced(self, project, add_asset):
        add_asset(size=(1200, 1200))  # comfortably above the built-in minimum

        result = run(project, thresholds=Thresholds(min_width=4_000, min_height=4_000))

        assert result.passed == []


class TestCli:
    def test_exits_zero_on_a_clean_run(self, project, add_asset, capsys):
        add_asset()
        assert main(["run", "--root", str(project), "--watermark", "@amn"]) == 0
        assert "1 passed" in capsys.readouterr().out

    def test_exits_nonzero_when_something_broke(self, project, capsys):
        (project / "inbox" / "corrupt.jpg").write_bytes(b"nope")
        assert main(["run", "--root", str(project)]) == 1
        assert "1 errors" in capsys.readouterr().out

    def test_rejections_alone_do_not_fail_the_run(self, project, add_asset, capsys):
        add_asset(size=(100, 100))
        assert main(["run", "--root", str(project)]) == 0
        assert "0 passed, 1 rejected" in capsys.readouterr().out

    def test_missing_watermark_text_warns_but_works(self, project, add_asset, capsys):
        add_asset()
        assert main(["run", "--root", str(project)]) == 0
        assert "no watermark text set" in capsys.readouterr().err

    def test_repo_pyproject_config_is_internally_valid(self):
        # Guards the config this repo actually ships.
        thresholds = load_thresholds(Path(__file__).resolve().parent.parent)
        assert thresholds.min_width >= 1080
        assert thresholds.min_height >= 1080
