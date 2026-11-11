import { beforeEach, describe, expect, it, vi } from "vitest";

const mockState = vi.hoisted(() => {
  const hashes = new Map<string, Map<string, string>>();
  const bucket = (name: string) => {
    let value = hashes.get(name);
    if (!value) {
      value = new Map<string, string>();
      hashes.set(name, value);
    }
    return value;
  };

  return {
    hashes,
    media: { upload: vi.fn() },
    reddit: {
      submitPost: vi.fn(),
      getNewPosts: vi.fn(),
    },
    settings: {
      get: vi.fn(async (key: string) =>
        key === "allowedSubreddit" ? "earthporn" : "cdn.example.com",
      ),
    },
    redis: {
      hGet: vi.fn(async (hash: string, field: string) => bucket(hash).get(field) ?? null),
      hSetNX: vi.fn(async (hash: string, field: string, value: string) => {
        const target = bucket(hash);
        if (target.has(field)) return 0;
        target.set(field, value);
        return 1;
      }),
      hSet: vi.fn(async (hash: string, values: Record<string, string>) => {
        const target = bucket(hash);
        for (const [field, value] of Object.entries(values)) target.set(field, value);
        return Object.keys(values).length;
      }),
      hDel: vi.fn(async (hash: string, fields: string[]) => {
        const target = bucket(hash);
        let deleted = 0;
        for (const field of fields) {
          if (target.delete(field)) deleted += 1;
        }
        return deleted;
      }),
    },
  };
});

vi.mock("@devvit/web/server", () => ({
  media: mockState.media,
  reddit: mockState.reddit,
  redis: mockState.redis,
  settings: mockState.settings,
}));

import app from "./index";

const DELIVERY_ID = "a".repeat(64);
const DELIVERY_HASH = "amn:reddit:deliveries";
const CLAIMS_HASH = "amn:reddit:reconciliation_claims";
const AUDIT_HASH = "amn:reddit:reconciliation_audit";

function seedUnknownDelivery(): void {
  mockState.hashes.set(
    DELIVERY_HASH,
    new Map([
      [
        DELIVERY_ID,
        JSON.stringify({
          status: "unknown",
          updated_at: new Date(Date.now() - 180_000).toISOString(),
        }),
      ],
    ]),
  );
}

async function request(path: string, body: unknown): Promise<Response> {
  return await app.request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function publishedResolution(actor = "operator@example.com") {
  return {
    delivery_id: DELIVERY_ID,
    resolution: "published",
    post_id: "abc123",
    permalink: "/r/earthporn/comments/abc123/landscape/",
    actor,
    note: "Verified directly in the subreddit",
  };
}

function notPublishedResolution() {
  return {
    delivery_id: DELIVERY_ID,
    resolution: "not_published",
    actor: "operator@example.com",
    note: "Verified no matching post exists",
  };
}

describe("Reddit Devvit reconciliation", () => {
  beforeEach(() => {
    mockState.hashes.clear();
    vi.clearAllMocks();
    mockState.settings.get.mockImplementation(async (key: string) =>
      key === "allowedSubreddit" ? "earthporn" : "cdn.example.com",
    );
  });

  it("allows only one conflicting operator decision to claim a delivery", async () => {
    seedUnknownDelivery();

    const responses = await Promise.all([
      request("/external/amn/reconcile", publishedResolution()),
      request("/external/amn/reconcile", notPublishedResolution()),
    ]);

    expect(responses.map((response) => response.status).sort()).toEqual([200, 409]);
    expect(mockState.hashes.get(AUDIT_HASH)?.size).toBe(1);
    expect(mockState.hashes.get(CLAIMS_HASH)?.has(DELIVERY_ID)).toBe(
      mockState.hashes.get(DELIVERY_HASH)?.has(DELIVERY_ID) ?? false,
    );
  });

  it("resumes the same claimed decision after a partial Redis failure", async () => {
    seedUnknownDelivery();
    mockState.redis.hSet
      .mockRejectedValueOnce(new Error("temporary Redis failure"));

    const first = await request("/external/amn/reconcile", publishedResolution());
    expect(first.status).toBe(500);
    expect(mockState.hashes.get(CLAIMS_HASH)?.has(DELIVERY_ID)).toBe(true);

    const retry = await request("/external/amn/reconcile", publishedResolution());
    expect(retry.status).toBe(200);
    expect(await retry.json()).toMatchObject({
      status: "published",
      post_id: "abc123",
    });
    expect(mockState.hashes.get(AUDIT_HASH)?.size).toBe(1);
    expect(JSON.parse(mockState.hashes.get(DELIVERY_HASH)!.get(DELIVERY_ID)!)).toMatchObject({
      status: "published",
      post_id: "abc123",
    });
  });

  it("releases a negative claim only after resetting the delivery reservation", async () => {
    seedUnknownDelivery();

    const response = await request("/external/amn/reconcile", notPublishedResolution());

    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ status: "reset" });
    expect(mockState.hashes.get(DELIVERY_HASH)?.has(DELIVERY_ID)).toBe(false);
    expect(mockState.hashes.get(CLAIMS_HASH)?.has(DELIVERY_ID)).toBe(false);
    expect(mockState.hashes.get(AUDIT_HASH)?.size).toBe(1);
  });

  it("blocks a new publish while negative reconciliation finalization is pending", async () => {
    mockState.hashes.set(
      CLAIMS_HASH,
      new Map([
        [
          DELIVERY_ID,
          JSON.stringify({
            status: "not_published",
            resolution: "not_published",
            resolved_at: new Date().toISOString(),
            actor: "operator@example.com",
            note: "Verified no matching post exists",
          }),
        ],
      ]),
    );

    const response = await request("/external/amn/publish", {
      delivery_id: DELIVERY_ID,
      subreddit: "earthporn",
      title: "Landscape",
      image_url: "https://cdn.example.com/outbox/image.jpg",
    });

    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({
      status: "reconciliation_in_progress",
    });
    expect(mockState.media.upload).not.toHaveBeenCalled();
    expect(mockState.reddit.submitPost).not.toHaveBeenCalled();
  });
});
