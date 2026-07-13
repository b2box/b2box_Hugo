// Helpers de formato portados del index.html original.

import type { PriceSnapshot } from "../types";

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  const diffMin = Math.floor((now.getTime() - d.getTime()) / 60000);
  if (diffMin < 0) return d.toLocaleString("es-AR");
  if (diffMin < 1) return "hace instantes";
  if (diffMin < 60) return `hace ${diffMin} min`;
  if (diffMin < 1440) return `hace ${Math.floor(diffMin / 60)} h`;
  return d.toLocaleString("es-AR", { dateStyle: "short", timeStyle: "short" });
}

export function fmtPrice(obj: PriceSnapshot | null | undefined): string | null {
  if (!obj || obj.price_cents == null) return null;
  return `${(obj.price_cents / 100).toFixed(2)} ${obj.currency || ""}`.trim();
}

// Algunos productos seed/demo traen image_url=https://example.com/x.jpg → no
// tiene sentido intentar bajarla.
export function isUnreachableImage(url: string): boolean {
  return /^https?:\/\/(www\.)?example\.(com|org|net)/i.test(url);
}

export function shortUrl(url: string | null | undefined): string {
  if (!url) return "";
  try {
    const u = new URL(url);
    return (
      u.hostname.replace(/^www\./, "") +
      (u.pathname !== "/"
        ? u.pathname.slice(0, 30) + (u.pathname.length > 30 ? "…" : "")
        : "")
    );
  } catch {
    return url.slice(0, 50);
  }
}

export function nfmt(n: number): string {
  return n.toLocaleString("es-AR");
}
