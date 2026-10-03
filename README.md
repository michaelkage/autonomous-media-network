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
   - Engagement rate: `(likes + comments + shares) / views`. Rejects anything below threshold (default `2.0%`).
2. **Watermarking (`amn.watermark`)**: Pillow-driven. Places semi-transparent text with a soft drop shadow in the bottom-right corner, dynamically scaled to the image size. Preserves ICC profiles and EXIF metadata where present.
3. **Pipeline Orchestrator (`amn.pipeline`)**:
   - Atomic writes via temporary files.
   - Idempotent runs.
   - Moves vetted assets to `outbox/` and rejects to `rejected/`.
   - Generates SHA-256-verified `manifest.json`.

## CLI Usage

```bash
# Preview what would happen without touching files:
amn run --dry-run

# Run full pipeline with custom thresholds:
amn run \
  --inbox inbox \
  --outbox outbox \
  --rejected rejected \
  --watermark "@yourhandle" \
  --min-width 1080 \
  --min-height 1080 \
  --min-engagement 0.02
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

Missing fields default to zero. If `views <= 0`, engagement rate is 0.0% and rejected.

## GitHub Actions

- `.github/workflows/ci.yml`: Runs tests + linter on push and PR.
- `.github/workflows/pipeline.yml`: Scheduled run (every 6 hours) or manual `workflow_dispatch`. Watermarks assets, commits the outbox, and uploads the manifest as an artifact.
