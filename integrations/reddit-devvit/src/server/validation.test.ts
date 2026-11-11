import { describe, expect, it } from "vitest";

import {
  isAllowedImageUrl,
  isDeliveryId,
  isSubredditName,
  isSafeAuditText,
  normalizePostTitle,
  verifyPostReceipt,
} from "./validation";

describe("Devvit request validation", () => {
  it("accepts only stable lowercase SHA-256 delivery IDs", () => {
    expect(isDeliveryId("a".repeat(64))).toBe(true);
    expect(isDeliveryId("A".repeat(64))).toBe(false);
    expect(isDeliveryId("a".repeat(63))).toBe(false);
    expect(isDeliveryId(null)).toBe(false);
  });

  it("validates subreddit names without the r/ prefix", () => {
    expect(isSubredditName("earthporn")).toBe(true);
    expect(isSubredditName("Earth_Porn2")).toBe(true);
    expect(isSubredditName("r/earthporn")).toBe(false);
    expect(isSubredditName("a")).toBe(false);
    expect(isSubredditName("bad-name")).toBe(false);
  });

  it("rejects unsafe operator audit text and credential-like values", () => {
    expect(isSafeAuditText("operator@example.com", 100)).toBe(true);
    expect(isSafeAuditText("a".repeat(101), 100)).toBe(false);
    expect(isSafeAuditText("devvit_at_private", 100)).toBe(false);
    expect(isSafeAuditText("Bearer secret-token", 100)).toBe(false);
    expect(isSafeAuditText("operator\\nforged-entry", 100)).toBe(false);
    expect(isSafeAuditText("   ", 100)).toBe(false);
    expect(isSafeAuditText(null, 100)).toBe(false);
  });

  it("normalizes title whitespace and applies the Reddit title limit", () => {
    expect(normalizePostTitle("  a\n  landscape  ", "fallback")).toBe("a landscape");
    expect(normalizePostTitle("", "fallback.jpg")).toBe("fallback.jpg");
    expect(normalizePostTitle("x".repeat(350), "fallback")).toHaveLength(300);
  });

  it("allows only HTTPS image URLs on exact approved hosts", () => {
    const hosts = ["cdn.example.com"];
    expect(
      isAllowedImageUrl("https://cdn.example.com/outbox/image.jpg", hosts),
    ).toBe(true);
    expect(
      isAllowedImageUrl("https://cdn.example.com:443/outbox/image.webp", hosts),
    ).toBe(true);
    expect(
      isAllowedImageUrl("https://cdn.example.com.evil.test/image.jpg", hosts),
    ).toBe(false);
    expect(isAllowedImageUrl("http://cdn.example.com/image.jpg", hosts)).toBe(false);
    expect(
      isAllowedImageUrl("https://cdn.example.com:444/image.jpg", hosts),
    ).toBe(false);
    expect(
      isAllowedImageUrl("https://cdn.example.com/image.jpg?token=secret", hosts),
    ).toBe(false);
    expect(isAllowedImageUrl("https://cdn.example.com/image.gif", hosts)).toBe(false);
    expect(isAllowedImageUrl("not a URL", hosts)).toBe(false);
  });

  it("requires the post ID and subreddit to match the verified permalink", () => {
    expect(
      verifyPostReceipt(
        "t3_abc123",
        "/r/earthporn/comments/abc123/landscape/",
        "earthporn",
      ),
    ).toEqual({
      postId: "abc123",
      permalink: "/r/earthporn/comments/abc123/landscape/",
    });
    expect(
      verifyPostReceipt(
        "other123",
        "/r/earthporn/comments/abc123/landscape/",
        "earthporn",
      ),
    ).toBeNull();
    expect(
      verifyPostReceipt(
        "abc123",
        "/r/another/comments/abc123/landscape/",
        "earthporn",
      ),
    ).toBeNull();
    expect(
      verifyPostReceipt(
        "abc123",
        "/r/earthporn/comments/abc123/landscape/?context=1",
        "earthporn",
      ),
    ).toBeNull();
    expect(
      verifyPostReceipt("abc123", "https://reddit.com/r/earthporn/comments/abc123/", "earthporn"),
    ).toBeNull();
  });
});
