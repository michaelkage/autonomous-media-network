"""Pipeline orchestration and CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
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
INBOX_DIR, OUTBOX_DIR, REJECTED_DIR, BUILD_DIR = "inbox", "outbox", "rejected", "build"
WATERMARK_ENV = "AMN_WATERMARK_TEXT"
JPEG_QUALITY = 92


@dataclass
class RunResult:
    passed: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_thresholds(root: Path) -> Thresholds:
    pyproject = Path(root) / "pyproject.toml"
    if not pyproject.is_file():
        return Thresholds()
    with pyproject.open("rb") as handle:
        config = tomllib.load(handle)
    values = dict(config.get("tool", {}).get("amn", {}))
    dynamic = root / "config" / "amn.json"
    if dynamic.is_file():
        runtime = json.loads(dynamic.read_text(encoding="utf-8"))
        values.update(runtime.get("thresholds", {}))
    return Thresholds.from_mapping(values)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_engagement(source: Path) -> tuple[Engagement | None, str | None]:
    sidecar = source.with_suffix(".json")
    if not sidecar.is_file():
        return None, None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return Engagement.from_mapping(data), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return None, f"metrics manifest is invalid or unreadable: {exc}"


def _retire(source: Path, sidecar: Path, destination: Path) -> None:
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
    with Image.open(source) as image:
        image.load()
        if detector and detector.is_duplicate(image):
            return Verdict(False, ("duplicate image detected via dHash",), image.width, image.height), {}
        engagement, broken_metrics = _read_engagement(source)
        verdict = vet(image.format, image.size, engagement, thresholds)
        if broken_metrics:
            verdict = replace(verdict, passed=False, reasons=verdict.reasons + (broken_metrics,))
        if not verdict.passed:
            return verdict, {}

        source_digest = _sha256(source)
        output = outbox / f"{source.stem[:80]}.jpg"
        if output.exists():
            output = outbox / f"{source.stem[:80]}-{source_digest[:12]}.jpg"
        if output.exists():
            raise FileExistsError(f"refusing to overwrite existing output: {output}")
        if not dry_run:
            outbox.mkdir(parents=True, exist_ok=True)
            watermarked = apply_watermark(image, watermark_text)
            import io
            buffer = io.BytesIO()
            watermarked.convert("RGB").save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
            _atomic_write(output, buffer.getvalue())
        caption = Captioner.generate(image.copy(), {
            "likes": engagement.likes if engagement else 0,
            "comments": engagement.comments if engagement else 0,
            "shares": engagement.shares if engagement else 0,
            "views": engagement.views if engagement else 0,
        })

    record: dict[str, Any] = {
        "source": source.name,
        "source_sha256": source_digest,
        "output": f"{outbox.name}/{output.name}",
        "width": verdict.width,
        "height": verdict.height,
        "caption": caption,
    }
    try:
        sidecar_data = json.loads(source.with_suffix(".json").read_text(encoding="utf-8"))
        for key in ("categories", "tags"):
            values = sidecar_data.get(key, [])
            if isinstance(values, list):
                record[key] = [str(value) for value in values if isinstance(value, str)]
        for key in ("source", "subreddit", "source_id", "permalink"):
            if isinstance(sidecar_data.get(key), str):
                record[key] = sidecar_data[key]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    if engagement is not None:
        record["engagement_rate"] = round(engagement.rate, 6)
        record["metrics"] = {"views": engagement.views, "likes": engagement.likes,
                             "comments": engagement.comments, "shares": engagement.shares}
    if not dry_run:
        record["sha256"] = _sha256(output)
        if detector:
            detector.record_from_path(source)
    return verdict, record


def write_manifest(build_dir: Path, result: RunResult) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "counts": {"passed": len(result.passed), "rejected": len(result.rejected),
                   "errors": len(result.errors)},
        "passed": result.passed, "rejected": result.rejected, "errors": result.errors,
    }
    manifest = build_dir / "manifest.json"
    _atomic_write(manifest, (json.dumps(payload, indent=2) + "\n").encode())
    return manifest


def run(root: Path, *, thresholds: Thresholds | None = None,
        watermark_text: str = "", dry_run: bool = False) -> RunResult:
    root = Path(root).resolve()
    thresholds = thresholds or load_thresholds(root)
    inbox, outbox, rejected = (root / name for name in (INBOX_DIR, OUTBOX_DIR, REJECTED_DIR))
    result = RunResult()
    if not inbox.is_dir():
        result.errors.append(f"inbox directory not found: {inbox}")
        return result
    if not dry_run and not watermark_text.strip():
        result.errors.append(f"watermark required: set --watermark or {WATERMARK_ENV}")
        return result

    runtime_config = root / "config" / "amn.json"
    max_distance = 5
    if runtime_config.is_file():
        max_distance = int(
            json.loads(runtime_config.read_text(encoding="utf-8"))
            .get("thresholds", {}).get("max_distance", 5)
        )
    detector = DuplicateDetector(root / BUILD_DIR / "hashes.json", max_distance=max_distance)
    sources = sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    for source in sources:
        sidecar = source.with_suffix(".json")
        try:
            verdict, record = process_image(source, outbox, thresholds, watermark_text,
                                             dry_run, detector=detector)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            result.errors.append(f"{source.name}: {exc}")
            if not dry_run:
                _retire(source, sidecar, rejected)
            continue
        if verdict.passed:
            result.passed.append(record)
            if not dry_run:
                source.unlink(missing_ok=True)
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
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--root", type=Path, default=Path.cwd())
    run_parser.add_argument("--watermark", default="")
    run_parser.add_argument("--dry-run", action="store_true")
    pub = sub.add_parser("publish")
    pub.add_argument("--platform", choices=["mock", "bluesky", "telegram"])
    pub.add_argument("--routed", action="store_true", help="dispatch using config/amn.json routes")
    pub.add_argument("--root", type=Path, default=Path.cwd())
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--source", required=True, choices=["reddit"])
    fetch.add_argument("--query", help="one subreddit; omit to fetch configured Reddit sources")
    fetch.add_argument("--limit", type=int, default=10)
    fetch.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    if args.command == "run":
        watermark = args.watermark or os.environ.get(WATERMARK_ENV, "")
        result = run(args.root, watermark_text=watermark, dry_run=args.dry_run)
        print(f"amn: {len(result.passed)} passed, {len(result.rejected)} rejected, {len(result.errors)} errors")
        for item in result.rejected:
            print(f"  rejected {item['source']}: {'; '.join(item['reasons'])}")
        for error in result.errors:
            print(f"  error {error}", file=sys.stderr)
        return 1 if result.errors else 0
    if args.command == "fetch":
        from .fetchers.reddit import RedditFetcher
        fetcher = RedditFetcher(os.environ.get("REDDIT_USER_AGENT", "amn-fetcher-v1"))
        if args.query:
            sources = [args.query]
        else:
            from .distribution import load_config
            sources = load_config(args.root / "config" / "amn.json").get("sources", {}).get("reddit", [])
        if not sources:
            print("error: no Reddit sources configured; pass --query or set sources.reddit", file=sys.stderr)
            return 2
        total = sum(fetcher.fetch(source, args.limit, args.root / INBOX_DIR) for source in sources)
        print(f"successfully fetched {total} assets from {len(sources)} configured subreddit(s)")
        return 0
    if args.command == "publish":
        if args.routed:
            from .distribution import publish_routed_manifest
            totals = publish_routed_manifest(args.root)
            print(f"routed publishing complete: {totals}")
            return 0
        if not args.platform:
            print("error: --platform or --routed is required", file=sys.stderr)
            return 2
        from .publish import BlueskyPublisher, MockPublisher, publish_manifest
        from .publishers.telegram import TelegramPublisher
        if args.platform == "mock":
            publisher = MockPublisher()
        elif args.platform == "bluesky":
            handle, password = os.environ.get("BLUESKY_HANDLE"), os.environ.get("BLUESKY_APP_PASSWORD")
            if not handle or not password:
                print("error: BLUESKY_HANDLE and BLUESKY_APP_PASSWORD required", file=sys.stderr)
                return 1
            publisher = BlueskyPublisher(handle, password)
        else:
            token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
            if not token or not chat_id:
                print("error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required", file=sys.stderr)
                return 1
            publisher = TelegramPublisher(token, chat_id)
        count = publish_manifest(args.root / BUILD_DIR / "manifest.json", publisher, root=args.root)
        print(f"successfully published {count} assets to {args.platform}")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
