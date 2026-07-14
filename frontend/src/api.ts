// Cliente HTTP de Hugo. Un único wrapper `apiFetch` que:
//  - manda las cookies de sesión (credentials: 'include' es redundante en same-origin
//    pero explícito no molesta),
//  - ante 401 (sesión expirada) redirige al login, igual que hacía el fetch
//    interceptado del index.html original.

import type {
  AuditLogResponse,
  AuditTarget,
  HistoryResponse,
  SectionsResponse,
  Setting,
  StatusResponse,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  const resp = await fetch(input, { credentials: "same-origin", ...init });
  if (resp.status === 401) {
    // Sesión expirada → al login. Frenamos la cadena para que el caller no
    // procese el 401 como si fuera data válida.
    window.location.href = "/login";
    throw new ApiError("No autenticado — redirigiendo al login", 401);
  }
  return resp;
}

async function asJson<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}) as { detail?: string });
    throw new ApiError(body.detail || resp.statusText, resp.status);
  }
  return resp.json() as Promise<T>;
}

// ─── Auth ──────────────────────────────────────────────────────────

export async function login(
  username: string,
  password: string,
): Promise<{ ok: boolean; detail?: string }> {
  const r = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ username, password }),
  });
  const data = await r.json().catch(() => ({}) as { ok?: boolean; detail?: string });
  if (r.ok && data.ok) return { ok: true };
  return { ok: false, detail: data.detail || "No se pudo iniciar sesión" };
}

export async function logout(): Promise<void> {
  try {
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  } catch {
    /* ignore */
  }
}

// ─── Dashboard ─────────────────────────────────────────────────────

export async function getStatus(): Promise<StatusResponse> {
  return asJson<StatusResponse>(await apiFetch("/api/status"));
}

export async function getSections(): Promise<SectionsResponse> {
  return asJson<SectionsResponse>(await apiFetch("/api/sections"));
}

export async function getEvents(
  section: string,
  skip: number,
  limit: number,
): Promise<AuditLogResponse> {
  const params = new URLSearchParams({
    skip: String(skip),
    limit: String(limit),
    section,
  });
  return asJson<AuditLogResponse>(await apiFetch("/audit-log?" + params));
}

export async function runAudit(target: AuditTarget): Promise<Response> {
  return apiFetch(`/audit?target=${target}`, { method: "POST" });
}

// ─── Acciones sobre eventos ────────────────────────────────────────

export async function retryPaco(eventId: number): Promise<void> {
  await asJson(await apiFetch(`/api/audit-log/${eventId}/retry-paco`, { method: "POST" }));
}

export async function confirmDuplicate(
  eventId: number,
): Promise<{ ok: boolean; action: string }> {
  return asJson(await apiFetch(`/api/audit-log/${eventId}/confirm-duplicate`, { method: "POST" }));
}

export async function confirmDisableBx(
  eventId: number,
): Promise<{ ok: boolean; action: string }> {
  return asJson(await apiFetch(`/api/audit-log/${eventId}/confirm-disable-bx`, { method: "POST" }));
}

export async function dismissEvent(eventId: number): Promise<void> {
  await asJson(await apiFetch(`/api/audit-log/${eventId}/dismiss`, { method: "POST" }));
}

export async function dismissSection(section: string): Promise<{ dismissed: number }> {
  const params = new URLSearchParams({ section });
  return asJson(await apiFetch("/api/audit-log/dismiss-section?" + params, { method: "POST" }));
}

export async function getHistory(productId: string): Promise<HistoryResponse> {
  return asJson<HistoryResponse>(
    await apiFetch(`/api/products/${encodeURIComponent(productId)}/history`),
  );
}

// ─── Settings runtime ──────────────────────────────────────────────

export async function getSettings(): Promise<Setting[]> {
  return asJson<Setting[]>(await apiFetch("/api/settings"));
}

export async function saveSetting(key: string, value: number): Promise<void> {
  await asJson(
    await apiFetch(`/api/settings/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    }),
  );
}

export async function resetSetting(key: string): Promise<void> {
  await asJson(await apiFetch(`/api/settings/${key}`, { method: "DELETE" }));
}
