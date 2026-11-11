# AMN Reddit Devvit Bridge

A server-side Devvit app that accepts authenticated AMN delivery requests and creates native Reddit image posts.

## Access requirement

Reddit currently marks external endpoints as a limited-access feature. Request access from Reddit and wait for approval before deploying this integration. A managed app token does not bypass the allowlist.

## Runtime flow

1. AMN sends a stable SHA-256 delivery ID, subreddit, title, and public HTTPS image URL to the Devvit global external endpoint.
2. The endpoint validates the request against the installation's configured subreddit and exact media-host allowlist.
3. Redis atomically reserves the delivery ID before external media upload or post submission.
4. Devvit uploads the image to Reddit media hosting and submits an image post as the app account.
5. The endpoint stores and returns the Reddit post ID and permalink.
6. Duplicate requests return the stored receipt. Ambiguous submissions are not retried automatically.

Each subreddit installation has its own Redis namespace. Configure AMN to call the endpoint URL for the installation that owns the target subreddit.

## Configure

- Install the app in the target subreddit as a moderator.
- Set the global app settings `allowedSubreddit` and `allowedMediaHosts`. The latter is a comma-separated list of exact hostnames, without scheme, port, or path.
- In Developer Settings, create a managed app token and store its secret in AMN's `AMN_REDDIT_DEVVIT_TOKEN` environment variable.
- Set `AMN_REDDIT_DEVVIT_URL` to the installation-specific global endpoint URL ending in `/external/amn/publish`.
- Keep the endpoint token out of configuration files, logs, issue reports, and source control.

## Build and playtest

Requires Node.js 24 and the Reddit Devvit CLI.

```bash
npm install
npx devvit login
npm run typecheck
npm run build
npx devvit playtest
```

Submit the app for Reddit review with `npx devvit publish` only after external endpoint access has been granted and the app has been reviewed against Reddit's current policies.

## Ambiguous native image submissions

Reddit may create native image posts asynchronously and fail to return a post ID in the initial submission response. The bridge treats any uncertain submit outcome as unresolved and retains its Redis reservation. An operator must independently inspect the subreddit and call the authenticated `/external/amn/reconcile` endpoint with a verified post receipt, or explicitly confirm that no post exists before resetting the reservation. Never clear an unresolved reservation merely to force a retry.

The bridge posts as the Devvit app account. It does not publish as the Reddit user who installed the app.

## Security boundaries

- HTTPS only, with no URL credentials, query strings, fragments, or non-standard ports for media.
- Exact hostname allowlist; no arbitrary media hostnames.
- One configured subreddit per installation.
- Managed token sent in the Authorization header.
- Atomic Redis reservation and fail-closed duplicate handling.
- No captions, access tokens, or remote request bodies are written to application logs.
- No automatic retry after a submission may have reached Reddit.
