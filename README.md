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

Supported publisher adapters include Bluesky, Telegram, Facebook Pages, X, Reddit link posts, Instagram Graph API, and TikTok Content Posting API photo posts:

```bash
TELEGRAM_BOT_TOKEN="..." TELEGRAM_CHAT_ID="@your_channel" \
  amn publish --platform telegram --root .

# Reddit: registered OAuth access token, submit scope, and descriptive User-Agent
REDDIT_USER_ACCESS_TOKEN="..." REDDIT_SUBREDDIT="earthporn" \
AMN_REDDIT_USER_AGENT="linux:amn:v0.2.0 (by /u/your_username)" \
AMN_PUBLIC_MEDIA_URL="https://media.example.com/outbox" \
  amn publish --platform reddit --root .

# X: OAuth 2.0 user access token with tweet.write, media.write, and users.read
X_USER_ACCESS_TOKEN="..." amn publish --platform x --root .

# Facebook Page: Page access token with pages_manage_posts + page ID
FACEBOOK_PAGE_ACCESS_TOKEN="..." FACEBOOK_PAGE_ID="..." \
  amn publish --platform facebook --root .

# Instagram: professional account + public HTTPS media hosting required
INSTAGRAM_ACCESS_TOKEN="..." INSTAGRAM_USER_ID="..." AMN_PUBLIC_MEDIA_URL="https://media.example.com/outbox" \
  amn publish --platform instagram --root .

# TikTok photo post: verified media URL domain, video.publish scope and explicit privacy
TIKTOK_ACCESS_TOKEN="..." TIKTOK_PRIVACY_LEVEL="SELF_ONLY" TIKTOK_DIRECT_POST_ENABLED="true" AMN_PUBLIC_MEDIA_URL="https://media.example.com/outbox" \
  amn publish --platform tiktok --root .
```

Publishing resolves manifest paths against the project root and records per-platform delivery state in `build/publish-state.json`. Writes are atomic, and same-target deliveries are serialized across threads and local worker processes using a per-target SQLite lock file beside the state ledger. The lock covers the read/publish/write sequence, preventing workers from publishing the same delivery based on stale state. Keep the state and lock files on a local filesystem, not a network filesystem. Rate limiters are shared per target within a process; TikTok's per-token request slots are reserved transactionally in the local build/jobs.sqlite3 database, coordinating publisher instances across local worker processes and restarts. The token itself is never stored, only its SHA-256 key. All workers sharing a TikTok access token must use the same local database file; separate hosts or separate database files are not coordinated. TikTok publish IDs are journaled in that database immediately after initialization; interrupted operations resume by polling the existing ID, and completed operations are recognized on retry rather than submitted again. Instagram media container IDs are journaled in the same database before status polling; interrupted processing resumes against the existing container. A timed-out Instagram media_publish request is marked as requiring reconciliation: AMN checks the existing container status and will not blindly repeat the publish request while the provider still reports FINISHED. Containers reported as `ERROR` or `EXPIRED` are archived in `instagram_publish_history` before a retry creates a replacement container, preserving the failed container ID and terminal status. Structured Graph API errors retain the HTTP status, provider error type/code/subcode, trace ID, and a bounded sanitized message; the dashboard exposes the latest 50 container and provider-error history records. Access tokens and raw provider response payloads are never stored in this history. Inspect unresolved operations with `amn instagram-reconcile list --root .`. After independently checking the Instagram account, resolve one using `amn instagram-reconcile resolve --root . --operation-key <key> --resolution published --actor <operator> --note "Verified the live post"` or `--resolution not_published` when you have confirmed no post exists. `published` marks the operation complete; `not_published` makes it retryable so the next publish attempt creates a fresh container. Each resolution is transactional, can only be applied while the operation is ambiguous, and is retained in `instagram_operation_resolutions`, appended to the unified delivery-event journal with its original delivery correlation, and shown in the dashboard audit history. This command never calls Meta; the operator is responsible for verifying the outcome before choosing a resolution. Adapters honor numeric `Retry-After` responses where the provider supplies them. Failed items remain retryable; successful items are skipped on subsequent runs. Delivery remains at-least-once: a crash after TikTok accepts initialization but before AMN persists the returned publish ID, or after another provider accepts a post but before local state persistence, can still result in a duplicate.\n\nEvery routed publish attempt is also recorded in an append-only `delivery_events` table in `build/jobs.sqlite3`. Events include attempt start, provider acceptance, persisted success, failure, and scheduled retry; captions, access tokens, and provider payloads are deliberately excluded. The loopback dashboard displays the latest 50 events, and `GET /api/deliveries?limit=100` returns a bounded sanitized history (1–500 events) with aggregate counts. This journal is diagnostic history, not a replacement for the delivery state ledger or provider operation journal. The same endpoint includes TikTok operation reconciliation: pending operations younger than the threshold remain pending, while operations with no status update for at least 15 minutes are reported as stalled for operator review. Override the window with `stale_after_seconds` (60–86400) on the endpoint. Stalled is an observation, not a provider failure; AMN does not rewrite the operation status or retry it automatically. Instagram operations are exposed alongside TikTok: processing containers older than the threshold are marked stalled, while an interrupted media_publish outcome is surfaced as reconciliation_required for manual review. Manual resolutions, operator identity, and the required audit note are visible in the dashboard's Instagram reconciliation audit section. Telegram success continues to mean Bot API acceptance only; Telegram does not provide a general outgoing read receipt. Successful `sendPhoto` and `sendDocument` responses expose the Telegram `message_id`, and Facebook Page photo publishing exposes its returned post ID, as `provider_message_id` on the corresponding `provider_accepted` delivery event. These receipts let operators correlate accepted sends with provider-side records; they do not close the crash window before local persistence. Facebook Page photo uploads are capped locally at 10 MiB and use the Page Photos edge. Older delivery journals are migrated additively when the event writer next runs. This receipt improves auditability but does not close the crash window between Telegram accepting a send and AMN persisting its receipt.\n\nTikTok Content Posting webhooks are supported separately from the loopback dashboard. Set `TIKTOK_CLIENT_SECRET` (and optionally `TIKTOK_CLIENT_KEY`), run `amn-webhooks --root .`, and configure `https://your-public-host/webhooks/tiktok` in the TikTok Developer Portal. Put the local receiver behind an HTTPS reverse proxy; do not expose the loopback dashboard. The receiver validates the `TikTok-Signature` HMAC-SHA256 header against the raw request body, enforces a five-minute timestamp window, optionally checks the client key, deduplicates exact webhook receipts, updates matching TikTok publish operations, and records the provider outcome in the delivery timeline. Supported events include `post.publish.failed`, `post.publish.complete`, `post.publish.publicly_available`, `post.publish.no_longer_publicaly_available`, and `post.publish.inbox_delivered`. TikTok may retry webhook delivery, so callbacks are handled idempotently. Status polling remains the fallback when no webhook arrives.

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

## Telegram operator alerts

The Telegram publisher can also send operational alerts to a separate private operator chat. Keep this destination separate from the public publishing channel: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` in the process environment, then run:

```bash
amn alerts check --root .
```

The checker detects terminally failed queue jobs, repeated delivery failures (at least three failed attempts in 30 minutes), stalled TikTok operations, stalled Instagram containers, and Instagram outcomes requiring manual reconciliation. Incidents are persisted in `operator_alerts` inside `build/jobs.sqlite3`, deduplicated by stable incident identity, automatically marked resolved when the condition clears, and re-notified if the same incident reopens. Active incidents have a six-hour notification cooldown; failed notification attempts are retried no more than once every five minutes. Alert delivery is best-effort and never runs inside a publishing operation. Messages contain bounded operational identifiers and status only, not captions, tokens, raw provider errors, or source content.

For unattended checks, add a durable alert schedule and keep the existing worker running:

```bash
amn jobs schedule add --id operator-alerts --kind alerts --every-seconds 300 --root .
amn jobs schedule tick --root .
amn jobs worker --root .
```

Invoke `amn jobs schedule tick --root .` periodically from cron/systemd (for example, once per minute). The alert job runs in the same durable queue as fetch, pipeline, and publish work. The dashboard's Operator alerts section and `GET /api/alerts` expose sanitized active/resolved history. Telegram credentials are not read from the AMN JSON config.

GitHub Actions Telegram publishing is separately opt-in. Add repository secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, then set repository variable `AMN_TELEGRAM_PUBLISH=true`. The workflow maps those secrets into the publishing step explicitly and fails that step if publishing is enabled but either secret is missing. Keep this publishing chat distinct from `TELEGRAM_ALERT_CHAT_ID`.

## Diagnostics dashboard

```bash
AMN_ROOT="$PWD" AMN_WATERMARK_TEXT="@yourhandle" amn-dashboard --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. The optional FastAPI dashboard exposes workspace counts, recent rejection reasons, durable job counts and recent job states, recurring schedule status and next-run times, a redacted configuration view, validated local config updates, and a pipeline trigger. It binds to loopback only, rejects non-loopback Host headers and cross-origin requests, and prevents overlapping pipeline runs. API secrets stay in environment variables and are never returned by the config endpoint. Do not expose it to a network without adding authentication and TLS.

## Security and operational notes

- Use dedicated platform bot/app credentials with the minimum scopes.
- Never commit `config/amn.json`, bot tokens, app passwords, or OAuth refresh tokens.
- Telegram bots must be added to the destination channel with permission to post.
- Reddit publishing uses an authenticated Reddit Data API user token with the `submit` scope and a truthful, descriptive `AMN_REDDIT_USER_AGENT` in Reddit's documented format. The subreddit must allow the account to submit. AMN currently creates a **link post** pointing at the publicly hosted image; this is not a native Reddit image upload. Configure a public HTTPS media URL, and optionally S3 storage, so Reddit can fetch the image. Reddit API access and commercial use remain subject to Reddit approval and its current developer terms. AMN does not automate OAuth consent or refresh tokens; supply a valid access token and rotate it outside AMN.
- Facebook publishing requires a Page access token for a Page where the authorizing person has the CREATE_CONTENT task, plus the pages_manage_posts, pages_read_engagement, and pages_show_list permissions. Keep the Page token in FACEBOOK_PAGE_ACCESS_TOKEN; it is never stored in AMN config.
- X publishing requires an OAuth 2.0 user access token with `tweet.write`, `media.write`, and `users.read` scopes. Store it in `X_USER_ACCESS_TOKEN`. AMN uploads a JPEG through X API v2's simple image-upload endpoint (5 MiB limit), then creates the Post with the returned media ID. A successful media upload followed by a failed Post request can leave an unused media object; a crash after Post acceptance but before local state persistence can still produce an at-least-once duplicate.
- Instagram/TikTok reposting must respect creator rights, platform policies, and required attribution.
- Reddit's public endpoint does not expose view counts; AMN does not fabricate an engagement denominator. Reddit downloads are streamed and capped at 25 MiB per asset.
- Dynamic config rejects unknown target fields, plaintext credential fields, invalid platform names, malformed routes, and non-integer threshold values.
- Reddit subreddit ingestion is implemented. Arbitrary Instagram/TikTok competitor-profile scraping is not; use approved platform APIs and authorized accounts.
