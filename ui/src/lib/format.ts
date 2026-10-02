import type { Desk } from "./types";

export function ago(iso: string, now = Date.now()): string {
  const s = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function stamp(d: Date | string): string {
  const x = typeof d === "string" ? new Date(d) : d;
  return x.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export const TIER_LABEL: Record<string, string> = {
  A: "Primary / wire",
  B: "Independent",
  C: "Partisan / low reliability",
  S: "State media",
  U: "Unrated outlet",
};

export const EXCLUDED_LABEL: Record<string, string> = {
  no_desk_match: "No desk match",
  sports: "Sports",
  celebrity_entertainment: "Celebrity and entertainment",
  lifestyle: "Lifestyle",
  learned_dislike: "Learned from your feedback",
  you_dismissed: "You dismissed this",
};

export const FALLBACK_COLOR = "#7b8794";

export function deskColor(desks: Map<string, Desk>, key: string | null | undefined): string {
  return (key && desks.get(key)?.color) || FALLBACK_COLOR;
}

export function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

const LANG = new Intl.DisplayNames(["en"], { type: "language" });
export function languageName(code: string | null): string {
  if (!code) return "unknown";
  try {
    return LANG.of(code) ?? code;
  } catch {
    return code;
  }
}
