const DELIVERY_ID_PATTERN = /^[a-f0-9]{64}$/;
const SUBREDDIT_PATTERN = /^[A-Za-z0-9_]{2,21}$/;
const POST_ID_PATTERN = /^[A-Za-z0-9]+$/;
const IMAGE_EXTENSION_PATTERN = /\.(png|jpe?g|webp)$/i;

export type VerifiedPostReceipt = {
  postId: string;
  permalink: string;
};

export function isDeliveryId(value: unknown): value is string {
  return typeof value === "string" && DELIVERY_ID_PATTERN.test(value);
}

export function isSubredditName(value: unknown): value is string {
  return typeof value === "string" && SUBREDDIT_PATTERN.test(value);
}

export function isSafeAuditText(value: unknown, maxLength: number): value is string {
  if (typeof value !== "string") return false;
  const normalized = value.trim();
  return (
    normalized.length > 0 &&
    normalized.length <= maxLength &&
    !/[\\u0000-\\u001f\\u007f]/.test(normalized) &&
    !/devvit_at_|\\bbearer\\s/i.test(normalized)
  );
}

export function normalizePostTitle(value: string, fallback: string): string {
  const title = value.trim().replace(/\s+/g, " ").slice(0, 300);
  return title || fallback.slice(0, 300);
}

export function isAllowedImageUrl(value: string, allowedHosts: string[]): boolean {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      (url.port === "" || url.port === "443") &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      IMAGE_EXTENSION_PATTERN.test(url.pathname) &&
      allowedHosts.includes(url.hostname.toLowerCase())
    );
  } catch {
    return false;
  }
}

export function verifyPostReceipt(
  postId: unknown,
  permalink: unknown,
  allowedSubreddit: string,
): VerifiedPostReceipt | null {
  const normalizedPostId =
    typeof postId === "string" ? postId.replace(/^t3_/, "") : "";
  if (!POST_ID_PATTERN.test(normalizedPostId) || typeof permalink !== "string") {
    return null;
  }

  const match =
    /^\/r\/([A-Za-z0-9_]+)\/comments\/([A-Za-z0-9]+)(?:\/[^/?#]*)?\/?$/i.exec(
      permalink,
    );
  if (
    !match ||
    match[1].toLowerCase() !== allowedSubreddit.toLowerCase() ||
    match[2] !== normalizedPostId
  ) {
    return null;
  }

  return { postId: normalizedPostId, permalink };
}
