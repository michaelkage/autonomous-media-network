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
```

Production runs fail closed when `AMN_WATERMARK_TEXT` is missing. Accepted output names include a content digest, preventing collisions. Writes are atomic, and duplicate-database corruption is surfaced rather than silently disabling protection.

## Distribution and targets

Copy `config/amn.example.json` to `config/amn.json`. Routes map item categories to enabled targets. The default route is empty, so content is never broadcast accidentally. Credentials belong in environment variables, not JSON configuration.

Supported publisher adapters currently include Bluesky and Telegram:

```bash
TELEGRAM_BOT_TOKEN="..." TELEGRAM_CHAT_ID="@your_channel" \
  amn publish --platform telegram --root .
```

Publishing resolves manifest paths against the project root and records per-platform delivery state in `build/publish-state.json`. Failed items remain retryable; successful items are skipped on subsequent runs.

Instagram Graph API and TikTok Content Posting API require separate account eligibility, OAuth scopes, media hosting/container workflows, and platform review. They are deliberately not represented as working adapters until those credentials and endpoint flows can be exercised against real test accounts. The publisher interface is the extension boundary; vetting remains platform-agnostic.

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
