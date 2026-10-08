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
  // Consulta dedup_only: Hugo no mandó a Paco (lo hace quien consultó); sin "Reintentar".
  dedup_only?: boolean;
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
  status: "running" | "ok" | "degraded" | "failed" | "skipped";
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
  // Fuente "ML web" (búsqueda en listado.mercadolibre.com.ar). Corridas viejas: sin campo.
  web?: {
    status: string | null;
    searches: number;
    bytes: number;
    blocked: number;
    n_ok: number;
    bytes_per_search: number | null;
  };
  n_con_similares?: number;
  // Color ESTIMADO por similares (aparte del real) y productos con solo diferentes.
  estimated?: { verde: number; amarillo: number; rojo: number };
  solo_diferentes?: number;
  // Por fuente (Mercado Libre y cada tienda): productos con idéntico / similar / solo diferentes / nada.
  sources?: Record<string, SourceStats>;
}

// De dónde salió una publicación y cómo se decidió que es (o se parece a) lo nuestro.
export type MatchOrigin = "api" | "web";
export type MatchCategory = "igual" | "similar" | "diferente";
// Qué devolvió ML para un producto: idéntico (con o sin precio que cuente), solo
// similares, solo diferentes, o nada ("sin dato" de verdad).
export type MatchState = "igual" | "igual_sin_precio" | "similar" | "diferente" | "ninguno";
export type WebState = "ok" | "empty" | "blocked" | "error" | "budget" | "off";

export interface ListingSpecs {
  quantity?: number;
  capacity_ml?: number[];
  dims_cm?: number[][];
  weight_kg?: number[];
}

// Una publicación de ML: IGUAL (cuenta para el color) o SIMILAR (solo se muestra).
// Los campos nuevos faltan en las corridas anteriores.
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
  origin?: MatchOrigin;
  category?: MatchCategory;
  reason?: string;
  differences?: string[];
  // Avisos que no cambian el veredicto (p. ej. "medida dudosa en Vendure").
  notes?: string[];
  // Similares: ¿cuenta para el color estimado? Solo los confirmados (juez, medidas o una
  // persona) sin diferencia de cantidad ni de capacidad y con precio en pesos.
  in_estimate?: boolean;
  brand?: string | null;
  image_url?: string | null;
  seller?: string | null;
  sold_quantity?: number | null;
  price_cents?: number | null;
  specs?: ListingSpecs;
}

// Medidas nuestras de Vendure (cm y kg).
export interface OurSpecs {
  length?: number;
  width?: number;
  height?: number;
  weight?: number;
  box_length?: number;
  box_width?: number;
  box_height?: number;
  box_weight?: number;
}

export interface PriceMonitorSnapshot {
  id: number;
  run_id: number;
  product: {
    id: string;
    name: string | null;
    code: string | null;
    image_url: string | null;
    slug: string | null;
    enabled?: boolean;
  };
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
  // Idénticos sin un precio que cuente (se muestran con los idénticos, no suman a la mediana).
  unpriced_listings?: MatchedListing[];
  similar_count?: number;
  similar_listings?: MatchedListing[];
  // Publicaciones DIFERENTES: se guardan y se muestran con su precio, no cuentan para nada.
  other_count?: number;
  other_listings?: MatchedListing[];
  match_state?: MatchState | null;
  // Color ESTIMADO por la mediana de los similares (solo si no hay idéntico). No es el color real.
  estimated_color?: SemaforoColor | null;
  estimated_margin_pct?: number | null;
  estimated_median_cents?: number | null;
  estimated_listing_count?: number;
  estimated_from?: "similar" | null;
  match_origin?: MatchOrigin | null;
  web_state?: WebState | null;
  web_searches?: number;
  web_bytes?: number;
  our_specs?: OurSpecs | null;
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
  // Tiendas (Gadnic, Casa Perfecta…): la mejor coincidencia por fuente, el detalle por
  // tienda y el precio idéntico más bajo de afuera. Faltan en servidores viejos.
  cells?: Record<string, SourceCell>;
  stores?: Record<string, { label: string; matches: StoreMatchRow[] }>;
  cheapest_outside?: CheapestOutside | null;
  // De qué precios sale el color: ml | ml+tiendas | tiendas.
  price_basis?: "ml" | "ml+tiendas" | "tiendas";
}

export interface PriceMonitorSnapshotsResponse {
  run_id: number | null;
  items: PriceMonitorSnapshot[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
  colors: Partial<Record<SemaforoColor, number>>;
  // Aparte del color real: productos con color estimado y cuántos hay en cada estado.
  estimated_colors?: Partial<Record<"verde" | "amarillo" | "rojo", number>>;
  states?: Partial<Record<"igual" | "solo_similar" | "solo_diferente" | "ninguno", number>>;
  // Las columnas de fuente de la tabla: Mercado Libre y las tiendas activas.
  sources?: SourceMeta[];
  // ¿Las tiendas cuentan para el color? (ajuste «Tiendas cuentan para el color»)
  stores_affect_color?: boolean;
}

export interface PriceMonitorSummary {
  last_run: PriceMonitorRun | null;
  running: boolean;
  mode: number;
  ml_budget: { used: number; budget: number; remaining: number };
  judge_enabled: boolean;
  cron_utc: string;
  include_disabled?: boolean;
  // Cupo del día de la búsqueda web y, si hoy no corre, por qué (p. ej. falta BROWSER_PROXY).
  web?: { used: number; budget: number; remaining: number; off_reason: string | null };
  // Índice de cada tienda (cuánto hay leído, cuánto muerto, cupo de hoy) y si las tiendas cuentan para el color.
  stores?: StoreIndexStatus[];
  stores_affect_color?: boolean;
}

// ─── Tiendas como fuentes de comparación ───────────────────────────

export type StoreCategory = "igual" | "similar" | "diferente";

export interface SourceMeta {
  key: string; // "ml" | "store:<id>"
  label: string;
}

// El mejor resultado de una fuente para un producto: el idéntico; si no hay, el similar;
// si no, el más parecido (diferente).
export interface SourceCell {
  key: string;
  label: string;
  category: StoreCategory | null;
  counts: Record<StoreCategory, number>;
  price_cents?: number | null;
  price_doubtful?: boolean;
  price_note?: string | null;
  title?: string | null;
  url?: string | null;
  image_url?: string | null;
  match_id?: number;
  human_label?: "es" | "no_es" | null;
  // 0 = agotado (se muestra con «sin stock» y no cuenta para el color ni para «más barato afuera»).
  stock?: number | null;
}

export interface CheapestOutside {
  key: string;
  label: string;
  price_cents: number;
  title: string | null;
  url: string | null;
  // Lo único idéntico que hay afuera está agotado: se muestra como dato, no como «más barato».
  out_of_stock?: boolean;
}

export interface StoreMatchRow {
  id: number;
  store_id: number;
  store: string;
  rank: number;
  category: StoreCategory;
  auto_category: StoreCategory;
  source: string | null;
  title: string;
  url: string | null;
  image_url: string | null;
  brand: string | null;
  price_cents: number | null;
  price_doubtful: boolean;
  price_note: string | null;
  stock: number | null;
  image_score: number | null;
  name_score: number | null;
  confidence: number | null;
  differences: string[];
  reason: string | null;
  notes: string | null;
  human_label: "es" | "no_es" | null;
  // ¿Este similar entra al color ESTIMADO (cuando las tiendas cuentan)? Confirmado, sin diferencia de
  // cantidad ni capacidad y con precio creíble: el mismo criterio que en Mercado Libre.
  in_estimate?: boolean;
}

export interface SourceStats {
  label: string;
  total: number;
  igual: number;
  similar: number;
  diferente: number;
  nada: number;
}

export interface StoreIndexStatus {
  id: number;
  name: string;
  enabled: boolean;
  urls: number;
  indexed: number;
  dead: number;
  never_read: number;
  doubtful_price: number;
  // Fichas que vienen fallando (todavía no se dan por muertas) y cuántas dieron 5xx: una tienda caída aparece acá.
  failing?: number;
  errors_5xx?: number;
  // ok | degradada (la mitad o más dio 5xx) | caida (todas, ninguna bien: se corta la pasada)
  health?: StoreHealth | null;
  pages_today: number;
  max_pages_per_day: number;
  last_indexed_at: string | null;
  last_index_status: string | null;
}

export type StorePlatform = "tiendanube" | "jsonld_sitemap";
export type StoreHealth = "ok" | "degradada" | "caida";

export interface MarketStore {
  id: number;
  name: string;
  base_url: string;
  platform: StorePlatform;
  enabled: boolean;
  refresh_days: number;
  max_pages_per_day: number;
  sitemap_url: string | null;
  image_hosts: string | null;
  house_brand: string | null;
  notes: string | null;
  last_indexed_at: string | null;
  last_index_status: string | null;
  health?: StoreHealth | null;
  index?: StoreIndexStatus | null;
}

export type MarketStoreInput = Partial<
  Pick<
    MarketStore,
    "name" | "base_url" | "platform" | "enabled" | "refresh_days" | "max_pages_per_day" | "sitemap_url" | "image_hosts" | "house_brand" | "notes"
  >
>;
