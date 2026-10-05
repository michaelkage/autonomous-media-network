"""Pipeline orchestration and CLI.

inbox/<name>.jpg  +  inbox/<name>.json
    -> vet
    -> passed:  watermark, write outbox/<name>.jpg, delete source
    -> failed:  move to rejected/ with reasons in the manifest
    -> build/manifest.json   (the hand-off boundary for any future publisher)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .intelligence import Captioner, DuplicateDetector
from .vetting import Engagement, Thresholds, Verdict, vet
from .watermark import apply_watermark

__all__ = ["RunResult", "load_thresholds", "run", "write_manifest", "main"]

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
INBOX_DIR = "inbox"
OUTBOX_DIR = "outbox"
REJECTED_DIR = "rejected"
BUILD_DIR = "build"
WATERMARK_ENV = "AMN_WATERMARK_TEXT"
JPEG_QUALITY = 92


@dataclass
class RunResult:
    """Everything one run did. Serialised to build/manifest.json."""

    passed: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def load_thresholds(root: Path) -> Thresholds:
    """Read [tool.amn] from the project's pyproject.toml, or use defaults."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return Thresholds()
    with pyproject.open("rb") as handle:
        config = tomllib.load(handle)
    return Thresholds.from_mapping(config.get("tool", {}).get("amn", {}))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_engagement(source: Path) -> tuple[Engagement | None, str | None]:
    """Load the sidecar metrics manifest. Returns (engagement, reason_if_broken)."""
    sidecar = source.with_suffix(".json")
    if not sidecar.is_file():
        return None, None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"metrics manifest is unreadable: {exc}"
    try:
        return Engagement.from_mapping(data), None
    except ValueError as exc:
        return None, f"metrics manifest is invalid: {exc}"


def _retire(source: Path, sidecar: Path, destination: Path) -> None:
    """Move an asset out of the inbox so the queue always drains."""
    destination.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination / source.name))
    if sidecar.exists():
        shutil.move(str(sidecar), str(destination / sidecar.name))


def process_image(
    source: Path,
    outbox: Path,
    thresholds: Thresholds,
    watermark_text: str = "",
    dry_run: bool = False,
    detector: DuplicateDetector | None = None,
) -> tuple[Verdict, dict[str, Any]]:
    """Vet one asset and, if it passes, write the watermarked copy.

    Raises:
        UnidentifiedImageError: the file is not a decodable image.
        OSError: the file could not be read or written.
    """
    with Image.open(source) as image:
        image.load()

        # 1. Duplicate Check
        if detector and detector.is_duplicate(image):
            return Verdict(
                passed=False,
                reasons=("duplicate image detected via dHash",),
                width=image.width,
                height=image.height,
            ), {}

        engagement, broken_metrics = _read_engagement(source)
        verdict = vet(image.format, image.size, engagement, thresholds)
        if broken_metrics and not verdict.passed:
            verdict = replace(verdict, reasons=verdict.reasons + (broken_metrics,))
        if not verdict.passed:
            return verdict, {}

        output = outbox / f"{source.stem}.jpg"
        if not dry_run:
            outbox.mkdir(parents=True, exist_ok=True)
            watermarked = apply_watermark(image, watermark_text)
            watermarked.convert("RGB").save(output, "JPEG", quality=JPEG_QUALITY, optimize=True)

    # 2. Captioning
    metrics_data = {}
    if verdict.engagement:
        metrics_data = {
            "views": verdict.engagement.views,
            "likes": verdict.engagement.likes,
            "comments": verdict.engagement.comments,
            "shares": verdict.engagement.shares,
        }
    caption = Captioner.generate(image, metrics_data)

    record: dict[str, Any] = {
        "source": source.name,
        "output": f"{outbox.name}/{output.name}",
        "width": verdict.width,
        "height": verdict.height,
        "caption": caption,
    }
    if verdict.engagement is not None:
        record["engagement_rate"] = round(verdict.engagement.rate, 6)
    if not dry_run:
        record["sha256"] = _sha256(output)
        if detector:
            detector.record(image)
    return verdict, record


def write_manifest(build_dir: Path, result: RunResult) -> Path:
    """Write the run manifest: the contract any future publisher consumes."""
    build_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "counts": {
            "passed": len(result.passed),
            "rejected": len(result.rejected),
            "errors": len(result.errors),
        },
        "passed": result.passed,
        "rejected": result.rejected,
        "errors": result.errors,
    }
    manifest = build_dir / "manifest.json"
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return manifest


def run(
    root: Path,
    *,
    thresholds: Thresholds | None = None,
    watermark_text: str = "",
    dry_run: bool = False,
) -> RunResult:
    """Process every asset in the inbox. See module docstring for the flow."""
    root = Path(root)
    thresholds = thresholds or load_thresholds(root)
    inbox, outbox, rejected = root / INBOX_DIR, root / OUTBOX_DIR, root / REJECTED_DIR
    result = RunResult()
    detector = DuplicateDetector(root / BUILD_DIR / "hashes.json")

    if not inbox.is_dir():
        result.errors.append(f"inbox directory not found: {inbox}")
        return result

    sources = sorted(
        path for path in inbox.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    for source in sources:
        sidecar = source.with_suffix(".json")
        try:
            verdict, record = process_image(
                source, outbox, thresholds, watermark_text, dry_run, detector=detector
            )
        except (UnidentifiedImageError, OSError) as exc:
            # Retired rather than retried: a file that cannot be decoded now
            # never will, and leaving it would re-break every future run.
            result.errors.append(f"{source.name}: {exc}")
            if not dry_run:
                _retire(source, sidecar, rejected)
            continue

        if verdict.passed:
            result.passed.append(record)
            if not dry_run:
                source.unlink()
                sidecar.unlink(missing_ok=True)
        else:
            result.rejected.append({"source": source.name, "reasons": list(verdict.reasons)})
            if not dry_run:
                _retire(source, sidecar, rejected)

    if not dry_run:
        write_manifest(root / BUILD_DIR, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amn", description="Vet and watermark queued media.")
    subparsers = parser.add_subparsers(dest="command")

    # Run command
    run_parser = subparsers.add_parser("run", help="Vet and watermark queued media.")
    run_parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="project root containing inbox/"
    )
    run_parser.add_argument(
        "--watermark", default="", help=f"watermark text (defaults to ${WATERMARK_ENV})"
    )
    run_parser.add_argument(
        "--dry-run", action="store_true", help="vet only; write, move and delete nothing"
    )

    # Publish command
    pub_parser = subparsers.add_parser("publish", help="Publish manifest assets to social media.")
    pub_parser.add_argument(
        "--platform", required=True, choices=["mock", "bluesky"], help="Platform to publish to"
    )
    pub_parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="project root containing build/"
    )

    # Fetch command
    fetch_parser = subparsers.add_parser("fetch", help="Fetch new media from sources.")
    fetch_parser.add_argument(
        "--source", required=True, choices=["reddit"], help="Source to fetch from"
    )
    fetch_parser.add_argument(
        "--query",
        default="earthporn",
        help="Subreddit name for reddit, search term for pexels",
    )
    fetch_parser.add_argument("--limit", type=int, default=10, help="Number of assets to fetch")
    fetch_parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="project root containing inbox/"
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        watermark_text = args.watermark or os.environ.get(WATERMARK_ENV, "")
        result = run(args.root, watermark_text=watermark_text, dry_run=args.dry_run)

        print(
            f"amn: {len(result.passed)} passed, "
            f"{len(result.rejected)} rejected, {len(result.errors)} errors"
        )
        if not watermark_text and result.passed:
            print(f"amn: no watermark text set (--watermark or ${WATERMARK_ENV})", file=sys.stderr)
        for item in result.rejected:
            print(f"  rejected {item['source']}: {'; '.join(item['reasons'])}")
        for error in result.errors:
            print(f"  error {error}", file=sys.stderr)
        return 1 if result.errors else 0

    elif args.command == "fetch":
        # Only Reddit is offered: it is the sole source reporting real
        # per-post engagement, which is what min_likes measures. Pexels was
        # removed because it publishes no engagement at all, so every asset it
        # produced was guaranteed to be rejected by the vetting gate.
        from .fetchers.reddit import RedditFetcher

        user_agent = os.environ.get("REDDIT_USER_AGENT", "amn-fetcher-v1")
        fetcher = RedditFetcher(user_agent)
        count = fetcher.fetch(
            subreddit_name=args.query,
            limit=args.limit,
            inbox_dir=args.root / INBOX_DIR,
        )
        print(f"successfully fetched {count} assets from r/{args.query}")
        return 0

    elif args.command == "publish":
        from .publish import BlueskyPublisher, MockPublisher, publish_manifest

        if args.platform == "mock":
            publisher = MockPublisher()
        elif args.platform == "bluesky":
            handle = os.environ.get("BLUESKY_HANDLE")
            password = os.environ.get("BLUESKY_APP_PASSWORD")
            if not handle or not password:
                print("error: BLUESKY_HANDLE and BLUESKY_APP_PASSWORD required", file=sys.stderr)
                return 1
            publisher = BlueskyPublisher(handle, password)
        else:
            return 1

        count = publish_manifest(args.root / BUILD_DIR / "manifest.json", publisher)
        print(f"successfully published {count} assets to {args.platform}")
        return 0

    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
