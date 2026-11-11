import { Hono } from "hono";
import { media, reddit, redis, settings } from "@devvit/web/server";

type PublishRequest = {
  delivery_id: string;
  subreddit: string;
  title: string;
  image_url: string;
};

type DeliveryRecord = {
  status: "processing" | "published" | "unknown";
  post_id?: string;
  permalink?: string;
  updated_at: string;
};

const app = new Hono();
const DELIVERY_HASH = "amn:reddit:deliveries";
const MAX_TITLE_LENGTH = 300;

function jsonRecord(value: string | null | undefined): DeliveryRecord | null {
  if (!value) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (typeof parsed !== "object" || parsed === null || !("status" in parsed)) return null;
    return parsed as DeliveryRecord;
  } catch {
    return null;
  }
}

function validImageUrl(value: string, allowedHosts: string[]): boolean {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      allowedHosts.includes(url.hostname.toLowerCase())
    );
  } catch {
    return false;
  }
}

app.post("/external/amn/publish", async (c) => {
  let input: PublishRequest;
  try {
    input = await c.req.json<PublishRequest>();
  } catch {
    return c.json({ status: "error", error: "invalid_json" }, 400);
  }

  if (
    !input ||
    typeof input.delivery_id !== "string" ||
    !/^[a-f0-9]{64}$/.test(input.delivery_id) ||
    typeof input.subreddit !== "string" ||
    !/^[A-Za-z0-9_]{2,21}$/.test(input.subreddit) ||
    typeof input.title !== "string" ||
    !input.title.trim() ||
    typeof input.image_url !== "string"
  ) {
    return c.json({ status: "error", error: "invalid_request" }, 400);
  }

  const [allowedSubreddit, allowedMediaHostsRaw] = await Promise.all([
    settings.get("allowedSubreddit"),
    settings.get("allowedMediaHosts"),
  ]);
  if (!allowedSubreddit || input.subreddit.toLowerCase() !== allowedSubreddit.toLowerCase()) {
    return c.json({ status: "error", error: "subreddit_not_allowed" }, 403);
  }

  const allowedHosts = (allowedMediaHostsRaw ?? "")
    .split(",")
    .map((host) => host.trim().toLowerCase())
    .filter(Boolean);
  if (!allowedHosts.length || !validImageUrl(input.image_url, allowedHosts)) {
    return c.json({ status: "error", error: "media_host_not_allowed" }, 403);
  }

  const title = input.title.trim().replace(/\s+/g, " ").slice(0, MAX_TITLE_LENGTH);
  const currentRaw = await redis.hGet(DELIVERY_HASH, input.delivery_id);
  const current = jsonRecord(currentRaw);
  if (current?.status === "published" && current.post_id) {
    return c.json({
      status: "published",
      post_id: current.post_id,
      permalink: current.permalink,
      duplicate: true,
    });
  }
  if (current) {
    // Existing failed or ambiguous records require an operator reset. This
    // intentionally favors duplicate prevention over automatic recovery.
    return c.json({ status: "reconciliation_required" }, 409);
  }

  const record: DeliveryRecord = {
    status: "processing",
    updated_at: new Date().toISOString(),
  };
  const reserved = await redis.hSetNX(
    DELIVERY_HASH,
    input.delivery_id,
    JSON.stringify(record),
  );
  if (!reserved) {
    const raced = jsonRecord(await redis.hGet(DELIVERY_HASH, input.delivery_id));
    if (raced?.status === "published" && raced.post_id) {
      return c.json({
        status: "published",
        post_id: raced.post_id,
        permalink: raced.permalink,
        duplicate: true,
      });
    }
    return c.json({ status: "reconciliation_required" }, 409);
  }

  let uploaded: { mediaId: string; mediaUrl: string };
  try {
    uploaded = await media.upload({ url: input.image_url, type: "image" });
    if (!uploaded.mediaUrl) throw new Error("Reddit media upload returned no URL");
  } catch {
    // No Reddit post has been submitted yet, so releasing the reservation is
    // safe and lets AMN retry a transient media-hosting failure.
    await redis.hDel(DELIVERY_HASH, [input.delivery_id]);
    return c.json({ status: "error", error: "media_upload_failed" }, 502);
  }

  try {
    const post = await reddit.submitPost({
      subredditName: allowedSubreddit,
      title,
      kind: "image",
      imageUrls: [uploaded.mediaUrl],
      sendreplies: true,
    });
    const postId = post.id.replace(/^t3_/, "");
    const permalink = post.permalink;
    if (!postId) throw new Error("Reddit returned no post ID");

    const published: DeliveryRecord = {
      status: "published",
      post_id: postId,
      permalink,
      updated_at: new Date().toISOString(),
    };
    await redis.hSet(DELIVERY_HASH, {
      [input.delivery_id]: JSON.stringify(published),
    });
    return c.json({
      status: "published",
      post_id: postId,
      permalink,
      media_id: uploaded.mediaId,
    });
  } catch {
    // Submission errors can be ambiguous: Reddit may have accepted the post
    // before the response was lost. Never retry this delivery automatically.
    await redis.hSet(DELIVERY_HASH, {
      [input.delivery_id]: JSON.stringify({
        status: "unknown",
        updated_at: new Date().toISOString(),
      } satisfies DeliveryRecord),
    });
    return c.json({ status: "reconciliation_required" }, 202);
  }
});


type ReconcileRequest = {
  delivery_id: string;
  resolution: "published" | "not_published";
  post_id?: string;
  permalink?: string;
};

app.post("/external/amn/reconcile", async (c) => {
  let input: ReconcileRequest;
  try {
    input = await c.req.json<ReconcileRequest>();
  } catch {
    return c.json({ status: "error", error: "invalid_json" }, 400);
  }
  if (
    !input ||
    typeof input.delivery_id !== "string" ||
    !/^[a-f0-9]{64}$/.test(input.delivery_id) ||
    !["published", "not_published"].includes(input.resolution)
  ) {
    return c.json({ status: "error", error: "invalid_request" }, 400);
  }

  const existing = jsonRecord(await redis.hGet(DELIVERY_HASH, input.delivery_id));
  if (!existing || existing.status === "published") {
    return c.json({ status: "error", error: "delivery_not_reconcilable" }, 409);
  }

  if (input.resolution === "not_published") {
    await redis.hDel(DELIVERY_HASH, [input.delivery_id]);
    return c.json({ status: "reset", delivery_id: input.delivery_id });
  }

  if (
    typeof input.post_id !== "string" ||
    !/^[A-Za-z0-9]+$/.test(input.post_id.replace(/^t3_/, "")) ||
    typeof input.permalink !== "string" ||
    !input.permalink.startsWith("/r/")
  ) {
    return c.json({ status: "error", error: "valid_post_receipt_required" }, 400);
  }
  const [allowedSubreddit] = await Promise.all([settings.get("allowedSubreddit")]);
  if (!allowedSubreddit || !input.permalink.startsWith(`/r/${allowedSubreddit}/comments/`)) {
    return c.json({ status: "error", error: "post_receipt_subreddit_mismatch" }, 400);
  }
  const resolved: DeliveryRecord = {
    status: "published",
    post_id: input.post_id.replace(/^t3_/, ""),
    permalink: input.permalink,
    updated_at: new Date().toISOString(),
  };
  await redis.hSet(DELIVERY_HASH, { [input.delivery_id]: JSON.stringify(resolved) });
  return c.json({ status: "published", post_id: resolved.post_id, permalink: resolved.permalink });
});

export default app;
