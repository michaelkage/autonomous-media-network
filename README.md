# Autonomous Media Network

Local-first media ingestion, deterministic vetting, watermarking, and routed social publishing.

## Install and test

```bash
pip install -e ".[dev,dashboard]"
pytest -q
ruff check .
ruff format --check .
```

## Pipeline

```bash
amn fetch --source reddit --query earthporn --limit 10
AMN_WATERMARK_TEXT="@yourhandle" amn run --root .
amn run --root . --dry-run
amn publish --platform mock --root .
amn publish --routed --root .
amn doctor --root .
```

Production runs fail closed when `AMN_WATERMARK_TEXT` is missing. When an output basename already exists, AMN appends a source digest instead of overwriting it. Writes are atomic, and duplicate-database corruption is surfaced rather than silently disabling protection.

## Distribution and targets

Copy `config/amn.example.json` to `config/amn.json`. If `--query` is omitted from `amn fetch`, AMN ingests every subreddit listed in `sources.reddit`. Routes map item categories/tags or the source subreddit in each image sidecar to enabled targets. Example sidecar metadata: `"categories": ["landscape"]`. The default route is empty, so content is never broadcast accidentally. Use `amn publish --routed --root .` to dispatch through those mappings. Credentials belong in environment variables, not JSON configuration.

Supported publisher adapters include Bluesky, Telegram, Instagram Graph API, and TikTok Content Posting API photo posts:

```bash
TELEGRAM_BOT_TOKEN="..." TELEGRAM_CHAT_ID="@your_channel" \
  amn publish --platform telegram --root .

# Instagram: professional account + public HTTPS media hosting required
INSTAGRAM_ACCESS_TOKEN="..." INSTAGRAM_USER_ID="..." AMN_PUBLIC_MEDIA_URL="https://media.example.com/outbox" \
  amn publish --platform instagram --root .

# TikTok photo post: verified media URL domain, video.publish scope and explicit privacy
TIKTOK_ACCESS_TOKEN="..." TIKTOK_PRIVACY_LEVEL="SELF_ONLY" TIKTOK_DIRECT_POST_ENABLED="true" AMN_PUBLIC_MEDIA_URL="https://media.example.com/outbox" \
  amn publish --platform tiktok --root .
```

Publishing resolves manifest paths against the project root and records per-platform delivery state in `build/publish-state.json`. Writes are atomic, and same-target deliveries are serialized across threads and local worker processes using a per-target SQLite lock file beside the state ledger. The lock covers the read/publish/write sequence, preventing workers from publishing the same delivery based on stale state. Keep the state and lock files on a local filesystem, not a network filesystem. Rate limiters are shared per target within a process; TikTok's per-token request slots are reserved transactionally in the local build/jobs.sqlite3 database, coordinating publisher instances across local worker processes and restarts. The token itself is never stored, only its SHA-256 key. All workers sharing a TikTok access token must use the same local database file; separate hosts or separate database files are not coordinated. TikTok publish IDs are journaled in that database immediately after initialization; interrupted operations resume by polling the existing ID, and completed operations are recognized on retry rather than submitted again. Adapters honor numeric `Retry-After` responses where the provider supplies them. Failed items remain retryable; successful items are skipped on subsequent runs. Delivery remains at-least-once: a crash after TikTok accepts initialization but before AMN persists the returned publish ID, or after another provider accepts a post but before local state persistence, can still result in a duplicate.\n\nEvery routed publish attempt is also recorded in an append-only `delivery_events` table in `build/jobs.sqlite3`. Events include attempt start, provider acceptance, persisted success, failure, and scheduled retry; captions, access tokens, and provider payloads are deliberately excluded. The loopback dashboard displays the latest 50 events, and `GET /api/deliveries?limit=100` returns a bounded sanitized history (1–500 events) with aggregate counts. This journal is diagnostic history, not a replacement for the delivery state ledger or provider operation journal. The same endpoint includes TikTok operation reconciliation: pending operations younger than the threshold remain pending, while operations with no status update for at least 15 minutes are reported as stalled for operator review. Override the window with `stale_after_seconds` (60–86400) on the endpoint. Stalled is an observation, not a provider failure; AMN does not rewrite the operation status or retry it automatically.\n\nTikTok Content Posting webhooks are supported separately from the loopback dashboard. Set `TIKTOK_CLIENT_SECRET` (and optionally `TIKTOK_CLIENT_KEY`), run `amn-webhooks --root .`, and configure `https://your-public-host/webhooks/tiktok` in the TikTok Developer Portal. Put the local receiver behind an HTTPS reverse proxy; do not expose the loopback dashboard. The receiver validates the `TikTok-Signature` HMAC-SHA256 header against the raw request body, enforces a five-minute timestamp window, optionally checks the client key, deduplicates exact webhook receipts, updates matching TikTok publish operations, and records the provider outcome in the delivery timeline. Supported events include `post.publish.failed`, `post.publish.complete`, `post.publish.publicly_available`, `post.publish.no_longer_publicaly_available`, and `post.publish.inbox_delivered`. TikTok may retry webhook delivery, so callbacks are handled idempotently. Status polling remains the fallback when no webhook arrives.

Run `amn doctor --root .` before enabling routed publishing. It performs read-only credential/account checks for enabled providers, checks S3 bucket access when storage is configured, and verifies that one passed media asset is publicly reachable for Instagram/TikTok. It never publishes content and reports sanitized failures. The public media URL must serve the exact outbox filenames; AMN does not expose your local filesystem to Meta or TikTok. For automatic uploads, install `pip install -e ".[s3]"` and configure the optional top-level `storage` block with `provider: "s3"` and your bucket name. AWS credentials use the standard `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optional `AWS_SESSION_TOKEN` environment variables; S3-compatible endpoints can be supplied through `AMN_S3_ENDPOINT_URL`. The URL path on each Instagram/TikTok target becomes the object-key prefix, so `https://cdn.example.com/outbox` uploads to `outbox/<filename>`. The bucket must be readable through that public CDN/custom domain. Cloudflare R2 works with its S3 endpoint and `region: "auto"`. Instagram publishing uses Meta's two-step container flow and requires a professional account plus a publicly reachable JPEG URL. TikTok photo publishing uses the current PHOTO content-init endpoint, requires a verified public URL domain, creator privacy-level validation, and the `video.publish` scope. TikTok's API also restricts unaudited apps to private visibility. Its creator-info response must be reviewed and the configured privacy option must match the account's returned options. Configure these targets disabled until the app review, OAuth scopes, verified media host, and account-level tests are complete. Static images use TikTok's photo API; no fake looping video conversion is needed. See [Meta content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing) and [TikTok Direct Post](https://developers.tiktok.com/docs/en/content-posting-api-get-started).

## Durable jobs

AMN can persist fetch, vetting-pipeline, and routed-publishing work in a local SQLite queue:

```bash
amn jobs enqueue --kind fetch --query earthporn --limit 10 --root .
amn jobs enqueue --kind pipeline --root .
amn jobs enqueue --kind publish --root .
amn jobs list --status pending --root .
amn jobs worker --once --root .
# Keep a worker running to process jobs as they arrive:
amn jobs worker --root .
```

The queue lives at `build/jobs.sqlite3`. Claims are transactional across local worker processes, each running job has a renewable lease, and crashed workers' expired jobs are reclaimed. Failed jobs retry with bounded exponential backoff until their attempt budget is exhausted. Pipeline jobs are serialized so two workers do not process the same inbox concurrently. The media pipeline also keeps an atomic per-asset checkpoint at `build/pipeline-checkpoint.json`: completed outcomes are journaled before inbox assets are retired, interrupted runs resume from that journal, and an identical already-rendered output is reused instead of duplicated. Optional dedupe keys prevent duplicate enqueue requests:

Recurring schedules are persisted in the same SQLite database. A scheduler tick enqueues each due schedule at most once, skips missed intervals rather than creating a catch-up storm, and can safely be invoked by cron or systemd timers:

```bash
amn jobs schedule add --id reddit-earthporn --kind fetch --query earthporn --every-seconds 3600 --root .
amn jobs schedule list --root .
amn jobs schedule tick --root .
amn jobs schedule disable --id reddit-earthporn --root .
amn jobs schedule enable --id reddit-earthporn --root .
```

Run `amn jobs schedule tick` from cron (for example, every minute) and keep `amn jobs worker` running separately. The scheduler does not need to stay resident.

```bash
amn jobs enqueue --kind fetch --query earthporn --dedupe-key "reddit:earthporn:2026-11-10" --root .
```

This is deliberately a single-host queue backed by a local SQLite database, not a distributed multi-host broker. Run one or more workers against the same local database; do not put the SQLite file on a network filesystem. The scheduler is a separate process concern: cron or a system scheduler should invoke `amn jobs schedule tick` periodically, independently of the dashboard.

## Diagnostics dashboard

```bash
AMN_ROOT="$PWD" AMN_WATERMARK_TEXT="@yourhandle" amn-dashboard --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. The optional FastAPI dashboard exposes workspace counts, recent rejection reasons, durable job counts and recent job states, recurring schedule status and next-run times, a redacted configuration view, validated local config updates, and a pipeline trigger. It binds to loopback only, rejects non-loopback Host headers and cross-origin requests, and prevents overlapping pipeline runs. API secrets stay in environment variables and are never returned by the config endpoint. Do not expose it to a network without adding authentication and TLS.

## Security and operational notes

- Use dedicated platform bot/app credentials with the minimum scopes.
- Never commit `config/amn.json`, bot tokens, app passwords, or OAuth refresh tokens.
- Telegram bots must be added to the destination channel with permission to post.
- Instagram/TikTok reposting must respect creator rights, platform policies, and required attribution.
- Reddit's public endpoint does not expose view counts; AMN does not fabricate an engagement denominator. Reddit downloads are streamed and capped at 25 MiB per asset.
- Dynamic config rejects unknown target fields, plaintext credential fields, invalid platform names, malformed routes, and non-integer threshold values.
- Reddit subreddit ingestion is implemented. Arbitrary Instagram/TikTok competitor-profile scraping is not; use approved platform APIs and authorized accounts.
