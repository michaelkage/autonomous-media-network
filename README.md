# autonomous-media-network

Hands-off content pipeline: drop images into `inbox/`, run the pipeline, vetted + watermarked assets land in `outbox/` alongside a distribution manifest.

```
inbox/                     amn run                       outbox/
  photo.jpg       ──────────────────────────►              photo.jpg (watermarked)
  photo.json                 │                             manifest.json
(sidecar metrics)            ▼
                         rejected/
                           bad_res.jpg
                           bad_res.json
```

## Quick Start

```bash
python -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -e ".[dev]"
pytest
ruff check
```

## How It Works

1. **Vetting Engine (`amn.vetting`)**: Pure logic. Assesses each asset against hard limits:
   - Minimum resolution: default `1080x1080` (rejects anything smaller, e.g. low-res crops, banners with sub-1080 short edges).
   - Sidecar requirement: every image `foo.ext` must have `foo.json` next to it.
   - Absolute engagement floor: `min_likes`, default `500`. Enforced for every source.
   - Engagement rate: `(likes + comments + shares) / views`, minimum `2.0%`, applied **only**
     when the source reports a real view count. Reddit's public API does not, so this rule
     is skipped for Reddit rather than fed an invented denominator.
2. **Watermarking (`amn.watermark`)**: Pillow-driven. Places semi-transparent text with a soft drop shadow in the bottom-right corner, dynamically scaled to the image size. Preserves ICC profiles and EXIF metadata where present.
3. **Pipeline Orchestrator (`amn.pipeline`)**:
   - Atomic writes via temporary files.
   - Idempotent runs.
   - Moves vetted assets to `outbox/` and rejects to `rejected/`.
   - Generates SHA-256-verified `manifest.json`.

## CLI Usage

Three subcommands. Thresholds live in `pyproject.toml` under `[tool.amn]`, not in flags.

```bash
# Pull new media from a source into inbox/
amn fetch --source reddit --query earthporn --limit 10   # no key needed

**Note:** The Pexels fetcher has been removed. It could not pass the engagement
gate because it provides no per-photo metrics. Reddit's public `.json` endpoint
requires no API approval, so it is the only supported source.

# Vet, watermark, and write build/manifest.json
amn run --watermark "@yourhandle"
amn run --dry-run        # report only, writes/moves/deletes nothing

# Publish the manifest
amn publish --platform mock
amn publish --platform bluesky --handle me.bsky.social   # needs the [bluesky] extra
```

## Sidecar Format

Place alongside the image (e.g. `inbox/post1.png` + `inbox/post1.json`):

```json
{
  "views": 50000,
  "likes": 2400,
  "comments": 150,
  "shares": 320
}
```

Every field defaults to zero, and `views` may be omitted entirely. Two rules apply:

- **`min_likes` (default 500)** — an absolute floor, enforced for every source. This is
  the rule that matters in practice, because it cannot be satisfied by inventing a
  denominator.
- **`min_engagement_rate` (default 0.02)** — only checked when the source reports a real
  `views` count. Reddit's public API never does, so for Reddit this rule is skipped
  rather than faked.

An asset with no sidecar is rejected outright.

## Duplicate Detection

`build/hashes.json` records a 64-bit Pillow dHash per accepted image. Re-crops,
resizes and re-compressions land within 5 bits of the original and are rejected; the
rule lives in `amn.intelligence` and the threshold is `MAX_DISTANCE`.

## GitHub Actions

- `.github/workflows/ci.yml`: tests + `ruff check` + `ruff format --check` on push and PR.
- `.github/workflows/pipeline.yml`: daily at 06:17 UTC, on `workflow_dispatch`, or when
  `inbox/` changes. Watermarks assets, commits the results, and uploads the manifest
  as an artifact.

### Requirements

| Variable/Secret | Purpose |
|-----------------|---------|
| `AMN_WATERMARK_TEXT` (repo variable) | Watermark text placed on vetting-passed images. Required for any run. |
| `AMN_PUBLISH` (repo variable, optional) | Set to `true` to enable the publish step. Off by default — publishing to Bluesky is irreversible. |
| `BLUESKY_HANDLE` (secret) | Required only when `AMN_PUBLISH=true`. |
| `BLUESKY_APP_PASSWORD` (secret) | Required only when `AMN_PUBLISH=true`. |
