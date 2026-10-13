// Cliente HTTP de Hugo. Un único wrapper `apiFetch` que:
//  - manda las cookies de sesión (credentials: 'include' es redundante en same-origin
//    pero explícito no molesta),
//  - ante 401 (sesión expirada) redirige al login, igual que hacía el fetch
//    interceptado del index.html original.

import type {
  AuditLogResponse,
  AuditTarget,
  BulkConfirmResult,
  HealthMetrics,
  HistoryResponse,
  MarketStore,
  MarketStoreInput,
  MatchOrigin,
  PriceMonitorSnapshot,
  PriceMonitorSnapshotsResponse,
  PriceMonitorSummary,
  SectionsResponse,
  SemaforoColor,
  SeoItemsResponse,
  SeoList,
  SeoListName,
  SeoListsResponse,
  SeoSummary,
  Setting,
  StatusResponse,
  StoreMatchRow,
  VisionCompare,
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

// El `detail` de FastAPI es un texto, o una lista de errores de validación (422):
// [{loc: ["body", "items", 0], msg: "…"}]. Sin esto un 422 se mostraba como «[object Object]».
export function detailText(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map((d) => {
        if (typeof d === "string") return d;
        if (d && typeof d === "object" && "msg" in d) {
          const loc = Array.isArray((d as { loc?: unknown }).loc)
            ? ((d as { loc: unknown[] }).loc as unknown[]).filter((x) => x !== "body").join(".")
            : "";
          return `${loc ? loc + ": " : ""}${String((d as { msg: unknown }).msg)}`;
        }
        return JSON.stringify(d);
      })
      .join(" · ");
  }
  return fallback;
}

async function asJson<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}) as { detail?: unknown });
    throw new ApiError(detailText(body.detail, resp.statusText), resp.status);
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
  q?: string,
): Promise<AuditLogResponse> {
  const params = new URLSearchParams({
    skip: String(skip),
    limit: String(limit),
    section,
  });
  if (q && q.trim()) params.set("q", q.trim());
  return asJson<AuditLogResponse>(await apiFetch("/audit-log?" + params));
}

export async function getHealthMetrics(): Promise<HealthMetrics> {
  return asJson<HealthMetrics>(await apiFetch("/api/health-metrics"));
}

export async function bulkConfirmDuplicates(
  minConfidence: number,
  confirm: boolean,
): Promise<BulkConfirmResult> {
  const params = new URLSearchParams({
    min_confidence: String(minConfidence),
    confirm: String(confirm),
  });
  return asJson<BulkConfirmResult>(
    await apiFetch("/api/duplicates/bulk-confirm?" + params, { method: "POST" }),
  );
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

export async function setComment(eventId: number, note: string): Promise<void> {
  await asJson(
    await apiFetch(`/api/audit-log/${eventId}/comment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    }),
  );
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

// ─── Comparador de proveedores de visión ───────────────────────────

export async function compareVision(url: string): Promise<VisionCompare> {
  return asJson<VisionCompare>(
    await apiFetch("/api/vision-compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }),
  );
}

// ─── Semáforo de precios contra Mercado Libre ──────────────────────

export interface SnapshotQuery {
  page: number;
  pageSize: number;
  color?: SemaforoColor | null;
  q?: string;
  runId?: number | null;
  // Productos de Vendure: habilitados / deshabilitados / todos.
  enabled?: EnabledFilter;
  // Qué devolvió ML: idéntico, solo similares, solo diferentes, sin resultados.
  match?: MatchFilter | null;
  origin?: MatchOrigin | null;
  // Color estimado por similares.
  estimated?: EstimatedFilter | null;
  // Por fuente: "ml" o "store:<id>" (productos con algo de esa fuente), y las fuentes donde
  // tiene que haber un idéntico (cualquiera de ellas).
  source?: string | null;
  igualIn?: string[];
}

export type EnabledFilter = "all" | "enabled" | "disabled";
export type MatchFilter = "igual" | "similar" | "solo_similar" | "diferente" | "solo_diferente" | "ninguno";
export type EstimatedFilter = "verde" | "amarillo" | "rojo" | "any";

export async function getPriceMonitorSnapshots(query: SnapshotQuery): Promise<PriceMonitorSnapshotsResponse> {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) });
  if (query.color) params.set("color", query.color);
  if (query.q && query.q.trim()) params.set("q", query.q.trim());
  if (query.runId) params.set("run_id", String(query.runId));
  if (query.enabled && query.enabled !== "all") params.set("enabled", query.enabled);
  if (query.match) params.set("match", query.match);
  if (query.origin) params.set("origin", query.origin);
  if (query.estimated) params.set("estimated", query.estimated);
  if (query.source) params.set("source", query.source);
  if (query.igualIn && query.igualIn.length > 0) params.set("igual_in", query.igualIn.join(","));
  return asJson<PriceMonitorSnapshotsResponse>(await apiFetch("/api/price-monitor/snapshots?" + params));
}

export async function getPriceMonitorHistory(
  productId: string,
): Promise<{ product_id: string; items: PriceMonitorSnapshotsResponse["items"] }> {
  return asJson(await apiFetch(`/api/price-monitor/products/${encodeURIComponent(productId)}/history?limit=30`));
}

// Devuelve la Response cruda: el 409 ("ya hay una corrida") no es un error
// para la UI, es un aviso.
export async function runPriceMonitor(): Promise<Response> {
  return apiFetch("/api/price-monitor/run", { method: "POST" });
}

// "No es el mismo": saca la publicación del snapshot (que se recalcula) y la excluye
// para ese producto en las próximas corridas. Devuelve el snapshot actualizado.
export async function markNotTheSame(snapshotId: number, mlId: string): Promise<PriceMonitorSnapshot> {
  const r = await apiFetch(`/api/price-monitor/snapshots/${snapshotId}/not-same`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ml_id: mlId }),
  });
  return (await asJson<{ snapshot: PriceMonitorSnapshot }>(r)).snapshot;
}

// "Es el mismo": una publicación SIMILAR o DIFERENTE pasa a idéntica (recalcula el color
// real) y la próxima corrida la respeta. Devuelve el snapshot actualizado.
export async function markTheSame(snapshotId: number, mlId: string): Promise<PriceMonitorSnapshot> {
  const r = await apiFetch(`/api/price-monitor/snapshots/${snapshotId}/same`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ml_id: mlId }),
  });
  return (await asJson<{ snapshot: PriceMonitorSnapshot }>(r)).snapshot;
}

// Deshacer "No es el mismo" o "Es el mismo". Si la persona había cambiado de opinión
// vuelve a la marca anterior (`restored`: 0 = "No es el mismo", 1 = "Es el mismo"); si
// no, borra la marca y la próxima corrida vuelve a juzgar la publicación sola. El detalle
// de hoy no se reconstruye.
export interface UndoResult {
  removed: boolean;
  restored?: 0 | 1;
}

export async function undoFeedback(productId: string, mlId: string): Promise<UndoResult> {
  return asJson<UndoResult>(
    await apiFetch(
      `/api/price-monitor/products/${encodeURIComponent(productId)}/feedback/${encodeURIComponent(mlId)}`,
      { method: "DELETE" },
    ),
  );
}

export async function getPriceMonitorSummary(): Promise<PriceMonitorSummary> {
  return asJson<PriceMonitorSummary>(await apiFetch("/api/price-monitor/summary"));
}


// ─── Tiendas (Casa Perfecta, Gadnic…) ──────────────────────────────

// "Es el mismo" (es) / "No es el mismo" (no_es) sobre un candidato de una tienda. Recalcula las
// cuentas y, si las tiendas cuentan, el color; la próxima corrida lo respeta.
export async function labelStoreMatch(matchId: number, label: "es" | "no_es"): Promise<{ match: StoreMatchRow }> {
  return asJson(
    await apiFetch(`/api/price-monitor/store-matches/${matchId}/label`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    }),
  );
}

export async function unlabelStoreMatch(matchId: number): Promise<{ match: StoreMatchRow }> {
  return asJson(await apiFetch(`/api/price-monitor/store-matches/${matchId}/label`, { method: "DELETE" }));
}

export async function getStores(): Promise<{ items: MarketStore[]; platforms: string[]; affect_color: boolean }> {
  return asJson(await apiFetch("/api/stores"));
}

export async function createStore(input: MarketStoreInput): Promise<MarketStore> {
  return asJson(
    await apiFetch("/api/stores", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function updateStore(id: number, input: MarketStoreInput): Promise<MarketStore> {
  return asJson(
    await apiFetch(`/api/stores/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function deleteStore(id: number): Promise<void> {
  await asJson(await apiFetch(`/api/stores/${id}`, { method: "DELETE" }));
}

export async function indexStoreNow(id: number): Promise<void> {
  await asJson(await apiFetch(`/api/stores/${id}/index`, { method: "POST" }));
}

// ─── Auditoría de textos del catálogo (SEO, solo lectura) ──────────

export type SeoEnabled = "all" | "enabled" | "disabled";
export type SeoChannel = "all" | "ar" | "solo_default";

export interface SeoQuery {
  rule?: string | null;
  enabled: SeoEnabled;
  lang?: string | null;
  channel: SeoChannel;
  q?: string;
  onlyIssues: boolean;
  page?: number;
  pageSize?: number;
}

function seoParams(query: SeoQuery, paged: boolean): URLSearchParams {
  const params = new URLSearchParams({ only_issues: String(query.onlyIssues) });
  if (query.rule) params.set("rule", query.rule);
  if (query.enabled !== "all") params.set("enabled", query.enabled);
  if (query.lang) params.set("lang", query.lang);
  if (query.channel !== "all") params.set("channel", query.channel);
  if (query.q && query.q.trim()) params.set("q", query.q.trim());
  if (paged) {
    params.set("page", String(query.page ?? 0));
    params.set("page_size", String(query.pageSize ?? 25));
  }
  return params;
}

export async function getSeoSummary(): Promise<SeoSummary> {
  return asJson<SeoSummary>(await apiFetch("/api/seo/text-audit/summary"));
}

export async function getSeoItems(query: SeoQuery): Promise<SeoItemsResponse> {
  return asJson<SeoItemsResponse>(await apiFetch("/api/seo/text-audit/items?" + seoParams(query, true)));
}

// Devuelve la Response cruda: el 409 ("ya hay una") y el 429 son avisos, no errores.
export async function runSeoAudit(): Promise<Response> {
  return apiFetch("/api/seo/text-audit/run", { method: "POST" });
}

// Baja el CSV con los mismos filtros. Va por fetch (no por un link) para que una
// sesión vencida redirija al login en vez de bajar un JSON de error.
export interface SeoCsvResult {
  total: number;
  truncated: boolean;
  maxRows: number;
}

export async function downloadSeoCsv(query: SeoQuery): Promise<SeoCsvResult> {
  const r = await apiFetch("/api/seo/text-audit/export.csv?" + seoParams(query, false));
  if (!r.ok) {
    const body = await r.json().catch(() => ({}) as { detail?: unknown });
    throw new ApiError(detailText(body.detail, r.statusText), r.status);
  }
  const blob = await r.blob();
  const name = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") ?? "")?.[1] ?? "auditoria-textos.csv";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return {
    total: Number(r.headers.get("X-Export-Total") ?? 0),
    truncated: r.headers.get("X-Export-Truncated") === "1",
    maxRows: Number(r.headers.get("X-Export-Max-Rows") ?? 0),
  };
}

export async function getSeoLists(): Promise<SeoListsResponse> {
  return asJson<SeoListsResponse>(await apiFetch("/api/seo/text-audit/lists"));
}

// `allowEmpty`: dejar la lista vacía apaga la regla; el servidor lo rechaza (422) si no se pide a propósito.
export async function saveSeoList(name: SeoListName, items: string[], allowEmpty = false): Promise<SeoList> {
  const r = await apiFetch(`/api/seo/text-audit/lists/${name}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items, allow_empty: allowEmpty }),
  });
  return asJson<SeoList>(r);
}

export async function resetSeoList(name: SeoListName): Promise<SeoList> {
  return asJson<SeoList>(await apiFetch(`/api/seo/text-audit/lists/${name}`, { method: "DELETE" }));
}
