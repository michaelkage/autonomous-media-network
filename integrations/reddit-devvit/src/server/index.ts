import { Hono } from "hono";
import { media, reddit, redis, settings } from "@devvit/web/server";
import {
  isAllowedImageUrl,
  isDeliveryId,
  isSafeAuditText,
  isSubredditName,
  normalizePostTitle,
  verifyPostReceipt,
} from "./validation";

type PublishRequest = {
  delivery_id: string;
  subreddit: string;
  title: string;
  image_url: string;
};

type DeliveryRecord = {
  status: "uploading" | "submitting" | "published" | "unknown";
  post_id?: string;
  permalink?: string;
  updated_at: string;
  resolution?: "published" | "not_published";
  resolved_at?: string;
  resolution_actor?: string;
  resolution_note?: string;
};

type ReconciliationRecord = {
  status: "published" | "not_published";
  resolution: "published" | "not_published";
  resolved_at: string;
  actor: string;
  note: string;
};

const app = new Hono();

function settingText(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

const DELIVERY_HASH = "amn:reddit:deliveries";
const RECONCILIATION_HASH = "amn:reddit:reconciliations";
const RECONCILIATION_AUDIT_HASH = "amn:reddit:reconciliation_audit";

function jsonRecord<T extends { status: string }>(
  value: string | null | undefined,
): T | null {
  if (!value) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (typeof parsed !== "object" || parsed === null || !("status" in parsed)) return null;
    return parsed as T;
  } catch {
    return null;
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
    !isDeliveryId(input.delivery_id) ||
    !isSubredditName(input.subreddit) ||
    typeof input.title !== "string" ||
    !input.title.trim() ||
    typeof input.image_url !== "string"
  ) {
    return c.json({ status: "error", error: "invalid_request" }, 400);
  }

  const [allowedSubredditSetting, allowedMediaHostsSetting] = await Promise.all([
    settings.get("allowedSubreddit"),
    settings.get("allowedMediaHosts"),
  ]);
  const allowedSubreddit = settingText(allowedSubredditSetting);
  const allowedMediaHosts = settingText(allowedMediaHostsSetting);
  if (!allowedSubreddit || input.subreddit.toLowerCase() !== allowedSubreddit.toLowerCase()) {
    return c.json({ status: "error", error: "subreddit_not_allowed" }, 403);
  }

  const allowedHosts = (allowedMediaHosts ?? "")
    .split(",")
    .map((host: string) => host.trim().toLowerCase())
    .filter(Boolean);
  if (!allowedHosts.length || !isAllowedImageUrl(input.image_url, allowedHosts)) {
    return c.json({ status: "error", error: "media_host_not_allowed" }, 403);
  }

  const title = normalizePostTitle(input.title, "");
  const currentRaw = await redis.hGet(DELIVERY_HASH, input.delivery_id);
  const current = jsonRecord<DeliveryRecord>(currentRaw);
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
    status: "uploading",
    updated_at: new Date().toISOString(),
  };
  const reserved = await redis.hSetNX(
    DELIVERY_HASH,
    input.delivery_id,
    JSON.stringify(record),
  );
  if (!reserved) {
    const raced = jsonRecord<DeliveryRecord>(
      await redis.hGet(DELIVERY_HASH, input.delivery_id),
    );
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

  await redis.hSet(DELIVERY_HASH, {
    [input.delivery_id]: JSON.stringify({
      status: "submitting",
      updated_at: new Date().toISOString(),
    } satisfies DeliveryRecord),
  });

  try {
    const post = await reddit.submitPost({
      subredditName: allowedSubreddit,
      title,
      kind: "image",
      imageUrls: [uploaded.mediaUrl],
      runAs: "APP",
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
    // Native image posts can be created asynchronously without returning a
    // post ID. Search a bounded recent window for this unique Reddit-hosted
    // media URL before declaring the result ambiguous.
    try {
      const recentPosts = await reddit.getNewPosts({
        subredditName: allowedSubreddit,
        limit: 50,
      }).all();
      const matchingPost = recentPosts.find(
        (candidate) =>
          candidate.subredditName.toLowerCase() === allowedSubreddit.toLowerCase() &&
          candidate.url === uploaded.mediaUrl,
      );
      if (matchingPost) {
        const recovered: DeliveryRecord = {
          status: "published",
          post_id: matchingPost.id.replace(/^t3_/, ""),
          permalink: matchingPost.permalink,
          updated_at: new Date().toISOString(),
        };
        await redis.hSet(DELIVERY_HASH, {
          [input.delivery_id]: JSON.stringify(recovered),
        });
        return c.json({
          status: "published",
          post_id: recovered.post_id,
          permalink: recovered.permalink,
          recovered: true,
        });
      }
    } catch {
      // The provider lookup is best-effort. Keep the reservation unresolved.
    }

    // Reddit may have accepted the post even if its response and subsequent
    // lookup were unavailable. Never automatically resubmit this delivery.
    await redis.hSet(DELIVERY_HASH, {
      [input.delivery_id]: JSON.stringify({
        status: "unknown",
        updated_at: new Date().toISOString(),
      } satisfies DeliveryRecord),
    });
    return c.json({ status: "reconciliation_required" }, 202);
  }
});



type StatusRequest = {
  delivery_ids: string[];
};

app.post("/external/amn/status", async (c) => {
  let input: StatusRequest;
  try {
    input = await c.req.json<StatusRequest>();
  } catch {
    return c.json({ status: "error", error: "invalid_json" }, 400);
  }
  if (
    !input ||
    !Array.isArray(input.delivery_ids) ||
    input.delivery_ids.length > 50 ||
    input.delivery_ids.some(
      (deliveryId) =>
        typeof deliveryId !== "string" || !/^[a-f0-9]{64}$/.test(deliveryId),
    )
  ) {
    return c.json({ status: "error", error: "invalid_request" }, 400);
  }

  const uniqueIds = [...new Set(input.delivery_ids)];
  const records = await Promise.all(
    uniqueIds.map(async (deliveryId) => ({
      delivery_id: deliveryId,
      record: jsonRecord<DeliveryRecord>(await redis.hGet(DELIVERY_HASH, deliveryId)),
      reconciliation: jsonRecord<ReconciliationRecord>(
        await redis.hGet(RECONCILIATION_HASH, deliveryId),
      ),
    })),
  );
  return c.json({
    status: "ok",
    deliveries: records.map(({ delivery_id, record, reconciliation }) => ({
      delivery_id,
      status: record?.status ?? "not_found",
      post_id: record?.post_id ?? null,
      permalink: record?.permalink ?? null,
      updated_at: record?.updated_at ?? null,
      reconciliation: reconciliation?.resolution
        ? {
            resolution: reconciliation.resolution,
            resolved_at: reconciliation.resolved_at ?? null,
            actor: reconciliation.actor,
          }
        : null,
    })),
  });
});

type ReconcileRequest = {
  delivery_id: string;
  resolution: "published" | "not_published";
  post_id?: string;
  permalink?: string;
  actor: string;
  note: string;
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
    !isDeliveryId(input.delivery_id) ||
    !["published", "not_published"].includes(input.resolution) ||
    !isSafeAuditText(input.actor, 100) ||
    !isSafeAuditText(input.note, 500)
  ) {
    return c.json({ status: "error", error: "invalid_request" }, 400);
  }

  const existing = jsonRecord<DeliveryRecord>(
    await redis.hGet(DELIVERY_HASH, input.delivery_id),
  );
  if (!existing || existing.status === "published") {
    return c.json({ status: "error", error: "delivery_not_reconcilable" }, 409);
  }
  const updatedAt = Date.parse(existing.updated_at);
  if (!Number.isFinite(updatedAt) || Date.now() - updatedAt < 120_000) {
    return c.json({ status: "error", error: "delivery_still_active" }, 409);
  }

  const receipt =
    input.resolution === "published"
      ? verifyPostReceipt(
          input.post_id,
          input.permalink,
          settingText(await settings.get("allowedSubreddit")) ?? "",
        )
      : null;
  if (input.resolution === "published" && !receipt) {
    return c.json({ status: "error", error: "valid_post_receipt_required" }, 400);
  }

  const resolvedAt = new Date().toISOString();
  const resolutionAudit: ReconciliationRecord = {
    status: input.resolution,
    resolution: input.resolution,
    resolved_at: resolvedAt,
    actor: input.actor.trim(),
    note: input.note.trim(),
  };
  const auditEntryId = [
    input.delivery_id,
    resolvedAt,
    crypto.randomUUID(),
  ].join(":");
  const auditRecorded = await redis.hSetNX(
    RECONCILIATION_AUDIT_HASH,
    auditEntryId,
    JSON.stringify(resolutionAudit),
  );
  if (!auditRecorded) {
    return c.json({ status: "error", error: "audit_record_conflict" }, 503);
  }

  if (input.resolution === "not_published") {
    await redis.hSet(RECONCILIATION_HASH, {
      [input.delivery_id]: JSON.stringify(resolutionAudit),
    });
    await redis.hDel(DELIVERY_HASH, [input.delivery_id]);
    return c.json({ status: "reset", delivery_id: input.delivery_id });
  }

  await redis.hSet(RECONCILIATION_HASH, {
    [input.delivery_id]: JSON.stringify(resolutionAudit),
  });
  const resolved: DeliveryRecord = {
    status: "published",
    post_id: receipt.postId,
    permalink: receipt.permalink,
    updated_at: resolvedAt,
    resolution: "published",
    resolved_at: resolvedAt,
    resolution_actor: input.actor.trim(),
    resolution_note: input.note.trim(),
  };
  await redis.hSet(DELIVERY_HASH, { [input.delivery_id]: JSON.stringify(resolved) });
  return c.json({ status: "published", post_id: resolved.post_id, permalink: resolved.permalink });
});

export default app;
