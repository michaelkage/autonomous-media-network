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

Publishing resolves manifest paths against the project root and records per-platform delivery state in `build/publish-state.json`. Failed items remain retryable; successful items are skipped on subsequent runs.

The public media URL must serve the exact outbox filenames; AMN does not expose your local filesystem to Meta or TikTok. Instagram publishing uses Meta's two-step container flow and requires a professional account plus a publicly reachable JPEG URL. TikTok photo publishing uses the current PHOTO content-init endpoint, requires a verified public URL domain, creator privacy-level validation, and the `video.publish` scope. TikTok's API also restricts unaudited apps to private visibility. Its creator-info response must be reviewed and the configured privacy option must match the account's returned options. Configure these targets disabled until the app review, OAuth scopes, verified media host, and account-level tests are complete. Static images use TikTok's photo API; no fake looping video conversion is needed. See [Meta content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing) and [TikTok Direct Post](https://developers.tiktok.com/docs/en/content-posting-api-get-started).

## Diagnostics dashboard

```bash
AMN_ROOT="$PWD" AMN_WATERMARK_TEXT="@yourhandle" amn-dashboard --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. The optional FastAPI dashboard exposes workspace counts, a redacted configuration view, validated local config updates, and a pipeline trigger. It binds to loopback only. It never returns API secrets. Do not expose it to a network without adding authentication and TLS.

## Security and operational notes

- Use dedicated platform bot/app credentials with the minimum scopes.
- Never commit `config/amn.json`, bot tokens, app passwords, or OAuth refresh tokens.
- Telegram bots must be added to the destination channel with permission to post.
- Instagram/TikTok reposting must respect creator rights, platform policies, and required attribution.
- Reddit's public endpoint does not expose view counts; AMN does not fabricate an engagement denominator.
- Reddit subreddit ingestion is implemented. Arbitrary Instagram/TikTok competitor-profile scraping is not; use approved platform APIs and authorized accounts.
