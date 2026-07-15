// Metadata visual de las secciones/tabs. La lista canónica (labels + counts)
// viene del backend (/api/sections); acá guardamos descripción y flags de UI.
import type { Tone } from "./types";

export interface SectionMeta {
  desc: string;
  placeholder?: boolean;
}

export const SECTION_META: Record<string, SectionMeta> = {
  inbox_luis: {
    desc: "Productos que Luis nos pasó para verificar antes de mandarlos a Paco.",
  },
  inbox_orders: {
    desc: "Productos que llegan desde el flujo de pedidos. (Próximamente — pendiente de definición)",
    placeholder: true,
  },
  duplicates: {
    desc: "Productos detectados como duplicados (deshabilitados o marcados para revisión).",
  },
  price_changes: {
    desc: "Cambios de precio detectados en la fuente del proveedor (1688).",
  },
  sent_to_paco: {
    desc: "Productos que Hugo verificó como nuevos y reenvió a Paco para enriquecimiento.",
  },
  paco_errors: {
    desc: "Productos nuevos que Hugo intentó mandar a Paco pero falló (timeouts, auth, etc.).",
  },
  quality_issues: {
    desc: "Productos del catálogo con problemas detectados: precio en 0, sin imagen, nombre vacío, etc. Revisá y eliminá manualmente.",
  },
  quality_no_image: {
    desc: "Productos sin imagen destacada. Subí una imagen o eliminalos.",
  },
  quality_zero_price: {
    desc: "Productos con precio = 0 en su primera variante. Cargá un precio válido o deshabilitá.",
  },
  pa_variants: {
    desc: 'Productos con variantes cuyo nombre empieza por "PA". Revisalos a mano.',
  },
  bx_no_image: {
    desc: 'Productos con nombre "BX…" y sin imagen. Revisá cada uno y confirmá para deshabilitarlo en Vendure.',
  },
  all: {
    desc: "Todos los eventos de auditoría en orden cronológico.",
  },
  settings: {
    desc: "Ajustes runtime de Hugo (umbrales, intervalos). Se aplican sin redeploy.",
  },
  salud: {
    desc: "Estado del sistema: budget OTAPI, tasa de éxito con Paco, últimas auditorías y cache.",
  },
};

export const GROUP_LABELS: Record<string, string> = {
  dedup: "Deduplicación",
  pricing: "Precios",
  scheduler: "Scheduler",
  general: "General",
};

interface ToneStyle {
  border: string;
  dot: string;
  badge: string;
}

export const TONES: Record<Tone, ToneStyle> = {
  warning: { border: "tone-warning", dot: "bg-warning", badge: "bg-warning/10 text-warning" },
  danger: { border: "tone-danger", dot: "bg-destructive", badge: "bg-destructive/10 text-destructive" },
  info: { border: "tone-info", dot: "bg-primary", badge: "bg-primary/10 text-primary" },
  muted: { border: "tone-muted", dot: "bg-muted-foreground", badge: "bg-muted text-muted-foreground" },
};

export const PAGE_SIZE = 25;
