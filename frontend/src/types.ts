// Tipos de las respuestas de la API de Hugo. Reflejan lo que arma
// `_humanize()` y los endpoints en backend/app/api/routes.py.

export type Tone = "warning" | "danger" | "info" | "muted";

export interface ProductRef {
  id: string | null;
  name: string | null;
  code: string | null;
  image_url?: string | null;
  source_url?: string | null;
}

export interface PriceSnapshot {
  price_cents: number | null;
  currency?: string | null;
}

// Qué hace "Confirmar duplicado" con este evento. Un flag de /verify no tiene
// nada que apagar (el candidato nunca entró a Vendure); uno de la auditoría de
// catálogo apaga el producto más nuevo y conserva el canónico.
export interface DuplicateSemantics {
  disable_target_id: string | null;
  canonical_product_id: string | null;
  vendure_action: "disable" | "none";
}

export interface AuditEvent {
  id: number;
  action: string;
  source: string | null;
  title: string;
  icon: string;
  tone: Tone;
  dismissed: boolean;
  product: ProductRef;
  related_product: ProductRef | null;
  detail: string | null;
  note: string | null;
  before: PriceSnapshot | null;
  after: PriceSnapshot | null;
  confidence: number | null;
  created_at: string | null;
  duplicate?: DuplicateSemantics | null;
}

export interface AuditLogResponse {
  items: AuditEvent[];
  total: number;
  skip: number;
  limit: number;
  has_more: boolean;
}

export interface SectionInfo {
  label: string;
  count: number | null;
}

export type SectionsResponse = Record<string, SectionInfo>;

export interface StatusResponse {
  agent: string;
  status: string;
  now: string;
  metrics: {
    products_tracked: number;
    snapshots_total: number;
    alerts_last_24h: number;
    duplicates_last_7d: number;
    audit_in_progress: { prices?: boolean; duplicates?: boolean };
  };
  last_audit: string | null;
  recent_events: AuditEvent[];
}

export interface Setting {
  key: string;
  group: string;
  label: string;
  description: string;
  type: "float" | "int" | string;
  value: number;
  default: number;
  min: number;
  max: number;
  step: number;
  modified: boolean;
}

export interface HistoryEvent extends AuditEvent {}

export interface HistoryResponse {
  product_id: string;
  current_state_in_vendure: {
    exists_in_vendure: boolean | null;
    enabled: boolean | null;
    error?: string;
  };
  total_events: number;
  events: HistoryEvent[];
}

export interface HealthMetrics {
  otapi_budget: { used: number; budget: number; remaining: number };
  price_monitor?: PriceMonitorSummary;
  paco: { passed: number; failed: number; success_rate: number | null };
  duplicates: { pending_flagged: number; disabled_total: number };
  quality_pending: number;
  errors_pending: number;
  image_hash_cache: { in_memory: number; persisted: number };
  last_price_snapshot: string | null;
  last_dedup_marker: string | null;
}

export interface BulkConfirmResult {
  would_disable?: number;
  would_confirm_only?: number;
  preview_ids?: string[];
  disabled?: number;
  confirmed_only?: number;
  skipped_already_disabled?: number;
  failed?: number;
}

export type AuditTarget =
  | "prices"
  | "duplicates"
  | "quality"
  | "pa_variants"
  | "bx_no_image"
  | "all";

// ─── Comparador de proveedores de visión ───────────────────────────
// CLIP arma la lista corta y cada proveedor decide sobre la MISMA lámina de
// candidatos. `answered:false` es "el proveedor no pudo responder", que es
// distinto de `found:false` ("miró y dijo que ninguno es").

export interface VisionVerdict {
  answered: boolean;
  found: boolean;
  product_id?: string | null;
  product_name?: string | null;
  product_code?: string | null;
  image_url?: string | null;
  confidence?: number;
  reason?: string;
  model?: string;
  elapsed_ms?: number;
}

export interface VisionCandidate {
  id: string;
  name: string;
  product_code: string | null;
  clip_score: number;
  image_url: string | null;
}

export interface VisionCompare {
  status: string;
  title?: string;
  marketplace?: string;
  canonical_url?: string;
  // El link estaba bloqueado y el producto se resolvió por nombre: la foto
  // puede ser la de otro producto parecido, no la que mandó el cliente.
  approximate?: boolean;
  all_images?: string[];
  query_images?: string[];
  vision_images?: string[];
  candidates?: VisionCandidate[];
  verdicts?: Record<string, VisionVerdict>;
  index?: Record<string, unknown>;
}

// ─── Semáforo de precios contra Mercado Libre (modo sombra) ────────
// Reflejan price_monitor.run_to_dict / snapshot_to_dict / summary.

export type SemaforoColor = "verde" | "amarillo" | "rojo" | "sin_dato";
export type MlStatus = "ok" | "no_data" | "failed" | "skipped";

export interface PriceMonitorRun {
  id: number;
  started_at: string | null;
  finished_at: string | null;
  status: "running" | "ok" | "degraded" | "failed";
  mode: number;
  trigger: string;
  total_products: number;
  processed: number;
  counts: Record<MlStatus, number>;
  colors: Record<SemaforoColor, number>;
  pct_no_data: number | null;
  pct_failed: number | null;
  ml_requests_used: number;
  llm: { calls: number; input_tokens: number; output_tokens: number; cost_usd: number };
  resumed_count: number;
  error: string | null;
}

export interface MatchedListing {
  ml_id: string;
  title: string;
  permalink: string;
  listings: number;
  min_cents: number;
  median_cents: number;
  source: string | null;
  image_score: number | null;
  name_score: number | null;
  confidence: number | null;
}

export interface PriceMonitorSnapshot {
  id: number;
  run_id: number;
  product: { id: string; name: string | null; code: string | null; image_url: string | null; slug: string | null };
  variant_id: string | null;
  captured_at: string | null;
  ml_status: MlStatus;
  ml_error: string | null;
  ml_median_cents: number | null;
  ml_min_cents: number | null;
  ml_listing_count: number;
  ml_seller_count: number;
  ml_currency: string | null;
  matched_listings: MatchedListing[];
  match_source: string | null;
  match_confidence: number | null;
  image_score_max: number | null;
  name_score_max: number | null;
  candidates_count: number;
  ambiguous_count: number;
  our_price_cents: number | null;
  tier_used: string | null;
  commission_pct: number | null;
  shipping_cents: number | null;
  est_margin_pct: number | null;
  color: SemaforoColor;
  prev_color: SemaforoColor | null;
}

export interface PriceMonitorSnapshotsResponse {
  run_id: number | null;
  items: PriceMonitorSnapshot[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
  colors: Partial<Record<SemaforoColor, number>>;
}

export interface PriceMonitorSummary {
  last_run: PriceMonitorRun | null;
  running: boolean;
  mode: number;
  ml_budget: { used: number; budget: number; remaining: number };
  judge_enabled: boolean;
  cron_utc: string;
}
