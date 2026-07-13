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
  before: PriceSnapshot | null;
  after: PriceSnapshot | null;
  confidence: number | null;
  created_at: string | null;
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

export type AuditTarget =
  | "prices"
  | "duplicates"
  | "quality"
  | "pa_variants"
  | "bx_no_image"
  | "all";
