"""Pipeline orchestration and CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image

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
    values: dict[str, Any] = {}
    if pyproject.is_file():
        with pyproject.open("rb") as handle:
            config = tomllib.load(handle)
        values.update(config.get("tool", {}).get("amn", {}))
    dynamic = root / "config" / "amn.json"
    if dynamic.is_file():
        from .distribution import validate_config

        runtime = validate_config(json.loads(dynamic.read_text(encoding="utf-8")))
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
    except (OSError, ValueError) as exc:
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
        if detector:
            distance = detector.distance_to_known(image)
            if distance is not None and distance <= detector.max_distance:
                reason = (
                    "duplicate image detected via dHash "
                    f"(distance {distance}, threshold {detector.max_distance})"
                )
                return Verdict(False, (reason,), image.width, image.height), {}
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
        rendered: bytes | None = None
        if not dry_run:
            outbox.mkdir(parents=True, exist_ok=True)
            watermarked = apply_watermark(image, watermark_text)
            import io

            buffer = io.BytesIO()
            watermarked.convert("RGB").save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
            rendered = buffer.getvalue()
            if output.exists():
                if output.read_bytes() != rendered:
                    raise FileExistsError(f"refusing to overwrite existing output: {output}")
            else:
                _atomic_write(output, rendered)
        caption = Captioner.generate(
            image.copy(),
            {
                "likes": engagement.likes if engagement else 0,
                "comments": engagement.comments if engagement else 0,
                "shares": engagement.shares if engagement else 0,
                "views": engagement.views if engagement else 0,
            },
        )

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
        if isinstance(sidecar_data.get("source"), str):
            record["source_platform"] = sidecar_data["source"]
        for key in ("subreddit", "source_id", "permalink"):
            if isinstance(sidecar_data.get(key), str):
                record[key] = sidecar_data[key]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    if engagement is not None:
        record["engagement_rate"] = round(engagement.rate, 6)
        record["metrics"] = {
            "views": engagement.views,
            "likes": engagement.likes,
            "comments": engagement.comments,
            "shares": engagement.shares,
        }
    if not dry_run:
        record["sha256"] = _sha256(output)
    return verdict, record


def write_manifest(build_dir: Path, result: RunResult) -> Path:
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
    _atomic_write(manifest, (json.dumps(payload, indent=2) + "\n").encode())
    return manifest


def _load_checkpoint(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("pipeline checkpoint must contain a list")
    entries: list[dict[str, Any]] = []
    for entry in payload:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("source_name"), str)
            or not isinstance(entry.get("source_sha256"), str)
            or entry.get("outcome") not in {"passed", "rejected", "error"}
        ):
            raise ValueError("pipeline checkpoint contains an invalid entry")
        entries.append(entry)
    return entries


def _save_checkpoint(path: Path, entries: list[dict[str, Any]]) -> None:
    _atomic_write(path, (json.dumps(entries, indent=2) + "\n").encode())


def _restore_checkpoint_entry(result: RunResult, entry: dict[str, Any]) -> None:
    outcome = entry["outcome"]
    if outcome == "passed":
        result.passed.append(entry["record"])
    elif outcome == "rejected":
        result.rejected.append(entry["record"])
    else:
        result.errors.append(entry["record"])


def run(
    root: Path,
    *,
    thresholds: Thresholds | None = None,
    watermark_text: str = "",
    dry_run: bool = False,
) -> RunResult:
    root = Path(root).resolve()
    thresholds = thresholds or load_thresholds(root)
    inbox, outbox, rejected = (root / name for name in (INBOX_DIR, OUTBOX_DIR, REJECTED_DIR))
    result = RunResult()
    if not inbox.is_dir():
        result.errors.append(f"inbox directory not found: {inbox}")
        return result
    checkpoint_path = root / BUILD_DIR / "pipeline-checkpoint.json"
    try:
        checkpoint = [] if dry_run else _load_checkpoint(checkpoint_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result.errors.append(f"pipeline checkpoint is invalid or unreadable: {exc}")
        return result
    completed = {(entry["source_name"], entry["source_sha256"]): entry for entry in checkpoint}
    for entry in checkpoint:
        _restore_checkpoint_entry(result, entry)
    runtime_config = root / "config" / "amn.json"
    max_distance = 5
    if runtime_config.is_file():
        from .distribution import validate_config

        runtime = validate_config(json.loads(runtime_config.read_text(encoding="utf-8")))
        max_distance = int(runtime.get("thresholds", {}).get("max_distance", 5))
    detector = DuplicateDetector(root / BUILD_DIR / "hashes.json", max_distance=max_distance)
    sources = sorted(
        p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    if sources and not dry_run and not watermark_text.strip():
        result.errors.append(f"watermark required: set --watermark or {WATERMARK_ENV}")
        return result
    for source in sources:
        sidecar = source.with_suffix(".json")
        source_digest = _sha256(source)
        source_key = (source.name, source_digest)
        previous = completed.get(source_key)
        if previous is not None:
            if previous["outcome"] == "passed":
                detector.record_from_path(source)
                source.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
            else:
                _retire(source, sidecar, rejected)
            continue
        try:
            verdict, record = process_image(
                source, outbox, thresholds, watermark_text, dry_run, detector=detector
            )
            if verdict.passed:
                entry = {
                    "source_name": source.name,
                    "source_sha256": source_digest,
                    "outcome": "passed",
                    "record": record,
                }
                result.passed.append(record)
            else:
                rejected_record = {
                    "source": source.name,
                    "reasons": list(verdict.reasons),
                }
                entry = {
                    "source_name": source.name,
                    "source_sha256": source_digest,
                    "outcome": "rejected",
                    "record": rejected_record,
                }
                result.rejected.append(rejected_record)
        except (OSError, ValueError) as exc:
            error = f"{source.name}: {exc}"
            entry = {
                "source_name": source.name,
                "source_sha256": source_digest,
                "outcome": "error",
                "record": error,
            }
            result.errors.append(error)
        if not dry_run:
            checkpoint.append(entry)
            completed[source_key] = entry
            _save_checkpoint(checkpoint_path, checkpoint)
            if entry["outcome"] == "passed":
                detector.record_from_path(source)
                source.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
            else:
                _retire(source, sidecar, rejected)
    if not dry_run:
        write_manifest(root / BUILD_DIR, result)
        checkpoint_path.unlink(missing_ok=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amn", description="Vet and watermark queued media.")
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--root", type=Path, default=Path.cwd())
    run_parser.add_argument("--watermark", default="")
    run_parser.add_argument("--dry-run", action="store_true")
    pub = sub.add_parser("publish")
    pub.add_argument("--platform", choices=["mock", "bluesky", "telegram", "instagram", "tiktok"])
    pub.add_argument("--routed", action="store_true", help="dispatch using config/amn.json routes")
    pub.add_argument("--root", type=Path, default=Path.cwd())
    doctor = sub.add_parser(
        "doctor", help="run read-only publishing and media-host preflight checks"
    )
    doctor.add_argument("--root", type=Path, default=Path.cwd())
    jobs = sub.add_parser("jobs", help="enqueue, inspect, and run durable jobs")
    job_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    enqueue = job_commands.add_parser(
        "enqueue", help="enqueue a pipeline, fetch, or routed publish job"
    )
    enqueue.add_argument("--kind", choices=["pipeline", "fetch", "publish"], required=True)
    enqueue.add_argument("--query", help="one subreddit for fetch jobs")
    enqueue.add_argument("--limit", type=int, default=10)
    enqueue.add_argument("--dedupe-key")
    enqueue.add_argument("--root", type=Path, default=Path.cwd())
    worker = job_commands.add_parser("worker", help="run queued jobs")
    worker.add_argument("--once", action="store_true", help="process at most one job then exit")
    worker.add_argument("--poll-interval", type=float, default=2.0)
    worker.add_argument("--lease-seconds", type=float, default=3600.0)
    worker.add_argument("--root", type=Path, default=Path.cwd())
    job_list = job_commands.add_parser("list", help="show recent durable jobs")
    job_list.add_argument("--status", choices=["pending", "running", "succeeded", "failed"])
    job_list.add_argument("--limit", type=int, default=50)
    job_list.add_argument("--root", type=Path, default=Path.cwd())

    fetch = sub.add_parser("fetch")
    fetch.add_argument("--source", required=True, choices=["reddit"])
    fetch.add_argument("--query", help="one subreddit; omit to fetch configured Reddit sources")
    fetch.add_argument("--limit", type=int, default=10)
    fetch.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    if args.command == "jobs":
        from .jobs import JobQueue

        queue = JobQueue(args.root / BUILD_DIR / "jobs.sqlite3")
        if args.jobs_command == "enqueue":
            if args.limit < 1 or args.limit > 100:
                parser.error("--limit for jobs enqueue must be between 1 and 100")
            if args.kind != "fetch" and args.query:
                parser.error("--query is only valid for fetch jobs")
            payload: dict[str, Any] = {"root": str(args.root.resolve())}
            if args.kind == "fetch":
                payload.update({"query": args.query, "limit": args.limit})
            job_id = queue.enqueue(args.kind, payload, dedupe_key=args.dedupe_key)
            print(f"enqueued {args.kind} job {job_id}")
            return 0
        if args.jobs_command == "list":
            for job in queue.list(status=args.status, limit=args.limit):
                print(
                    f"{job['id']} {job['status']} {job['kind']} "
                    f"attempt={job['attempts']}/{job['max_attempts']} "
                    f"error={job['last_error'] or '-'}"
                )
            print(f"job counts: {queue.counts()}")
            return 0
        if args.poll_interval <= 0 or args.lease_seconds < 3:
            parser.error("--poll-interval must be positive and --lease-seconds at least 3")
        worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

        def execute_job(job: dict[str, Any]) -> None:
            payload = job["payload"]
            job_root = Path(payload.get("root", args.root)).resolve()
            if job["kind"] == "pipeline":
                result = run(job_root, watermark_text=os.environ.get(WATERMARK_ENV, ""))
                if result.errors:
                    raise RuntimeError("; ".join(result.errors))
                print(f"pipeline job: {len(result.passed)} passed, {len(result.rejected)} rejected")
            elif job["kind"] == "fetch":
                from .fetchers.reddit import RedditFetcher

                query = payload.get("query")
                if query:
                    sources = [query]
                else:
                    from .distribution import load_config, validate_config

                    config = validate_config(load_config(job_root / "config" / "amn.json"))
                    sources = config.get("sources", {}).get("reddit", [])
                if not sources:
                    raise RuntimeError("no Reddit sources configured for fetch job")
                fetcher = RedditFetcher(os.environ.get("REDDIT_USER_AGENT", "amn-fetcher-v1"))
                total = sum(
                    fetcher.fetch(source, int(payload.get("limit", 10)), job_root / INBOX_DIR)
                    for source in sources
                )
                print(f"fetch job: downloaded {total} assets from {len(sources)} subreddit(s)")
            elif job["kind"] == "publish":
                from .distribution import publish_routed_manifest

                print(f"publish job: {publish_routed_manifest(job_root)}")
            else:
                raise ValueError(f"unsupported job kind: {job['kind']}")

        def process_one() -> bool:
            job = queue.claim(worker_id, lease_seconds=args.lease_seconds)
            if job is None:
                return False
            stop_heartbeat = threading.Event()

            def keep_lease_alive() -> None:
                interval = max(1.0, args.lease_seconds / 3)
                while not stop_heartbeat.wait(interval):
                    if not queue.heartbeat(job["id"], worker_id, lease_seconds=args.lease_seconds):
                        return

            heartbeat = threading.Thread(target=keep_lease_alive, daemon=True)
            heartbeat.start()
            try:
                execute_job(job)
                if not queue.complete(job["id"], worker_id):
                    print(f"job {job['id']} lost its lease before completion", file=sys.stderr)
            except Exception as exc:
                queue.fail(job["id"], worker_id, str(exc))
                print(f"job {job['id']} failed: {exc}", file=sys.stderr)
            finally:
                stop_heartbeat.set()
                heartbeat.join(timeout=2)
            return True

        if args.once:
            process_one()
            return 0
        print(f"AMN worker {worker_id} polling {queue.database}")
        try:
            while True:
                if not process_one():
                    time.sleep(args.poll_interval)
        except KeyboardInterrupt:
            print("worker stopped")
            return 0

    if args.command == "doctor":
        from .diagnostics import run_diagnostics

        checks = run_diagnostics(args.root)
        failed = 0
        for check in checks:
            status = "OK" if check["ok"] else "FAIL"
            print(f"[{status}] {check['target']} / {check['check']}: {check['detail']}")
            failed += not check["ok"]
        print(f"diagnostics: {len(checks) - failed} passed, {failed} failed")
        return 1 if failed else 0
    if args.command == "run":
        watermark = args.watermark or os.environ.get(WATERMARK_ENV, "")
        result = run(args.root, watermark_text=watermark, dry_run=args.dry_run)
        print(
            f"amn: {len(result.passed)} passed, "
            f"{len(result.rejected)} rejected, {len(result.errors)} errors"
        )
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
            from .distribution import load_config, validate_config

            config = validate_config(load_config(args.root / "config" / "amn.json"))
            sources = config.get("sources", {}).get("reddit", [])
        if not sources:
            print(
                "error: no Reddit sources configured; pass --query or set sources.reddit",
                file=sys.stderr,
            )
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
        from .publishers.instagram import InstagramPublisher
        from .publishers.telegram import TelegramPublisher
        from .publishers.tiktok import TikTokPublisher

        if args.platform == "mock":
            publisher = MockPublisher()
        elif args.platform == "bluesky":
            handle = os.environ.get("BLUESKY_HANDLE")
            password = os.environ.get("BLUESKY_APP_PASSWORD")
            if not handle or not password:
                print("error: BLUESKY_HANDLE and BLUESKY_APP_PASSWORD required", file=sys.stderr)
                return 1
            publisher = BlueskyPublisher(handle, password)
        elif args.platform == "telegram":
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            if not token or not chat_id:
                print("error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required", file=sys.stderr)
                return 1
            publisher = TelegramPublisher(token, chat_id)
        elif args.platform == "instagram":
            token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
            user_id = os.environ.get("INSTAGRAM_USER_ID")
            media_url = os.environ.get("AMN_PUBLIC_MEDIA_URL")
            if not token or not user_id or not media_url:
                print(
                    "error: INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_USER_ID and "
                    "AMN_PUBLIC_MEDIA_URL required",
                    file=sys.stderr,
                )
                return 1
            publisher = InstagramPublisher(token, user_id, media_url)
        else:
            if os.environ.get("TIKTOK_DIRECT_POST_ENABLED") != "true":
                print(
                    "error: set TIKTOK_DIRECT_POST_ENABLED=true to enable TikTok direct posting",
                    file=sys.stderr,
                )
                return 1
            token = os.environ.get("TIKTOK_ACCESS_TOKEN")
            media_url = os.environ.get("AMN_PUBLIC_MEDIA_URL")
            privacy = os.environ.get("TIKTOK_PRIVACY_LEVEL")
            if not token or not media_url or not privacy:
                print(
                    "error: TIKTOK_ACCESS_TOKEN, AMN_PUBLIC_MEDIA_URL and "
                    "TIKTOK_PRIVACY_LEVEL required",
                    file=sys.stderr,
                )
                return 1
            publisher = TikTokPublisher(
                token,
                media_url,
                privacy,
                direct_post_enabled=os.environ.get("TIKTOK_DIRECT_POST_ENABLED") == "true",
            )
        manifest_path = args.root / BUILD_DIR / "manifest.json"
        count = publish_manifest(manifest_path, publisher, root=args.root)
        if args.platform == "telegram" and count:
            publisher.publish_document(manifest_path)
        print(f"successfully published {count} assets to {args.platform}")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
