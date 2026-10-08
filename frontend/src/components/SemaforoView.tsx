import { Fragment, useEffect, useState, type ReactNode } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getPriceMonitorHistory,
  getPriceMonitorSnapshots,
  getPriceMonitorSummary,
  markNotTheSame,
  runPriceMonitor,
  undoNotTheSame,
  type EnabledFilter,
  type MatchFilter,
} from "../api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { IconExternalLink, IconRefresh, IconSearch, IconTrafficLight } from "../icons";
import { fmtArs, fmtPct, fmtTime, isUnreachableImage, nfmt } from "../lib/format";
import { SECTION_META } from "../sections";
import type {
  ListingSpecs,
  MatchCategory,
  MatchOrigin,
  MatchedListing,
  MlStatus,
  OurSpecs,
  PriceMonitorRun,
  PriceMonitorSnapshot,
  SemaforoColor,
  WebState,
} from "../types";

// Semáforo de precios contra Mercado Libre, en modo sombra: Hugo mide cada
// noche y guarda; esta vista solo lee. Nada de lo que se ve acá tocó Vendure.

const PAGE_SIZE = 25;
const COLOR_ORDER: SemaforoColor[] = ["verde", "amarillo", "rojo", "sin_dato"];

export const COLOR_META: Record<SemaforoColor, { label: string; dot: string; chip: string }> = {
  verde: { label: "Verde", dot: "bg-success", chip: "border-success/40 text-success" },
  amarillo: { label: "Amarillo", dot: "bg-warning", chip: "border-warning/40 text-warning" },
  rojo: { label: "Rojo", dot: "bg-destructive", chip: "border-destructive/40 text-destructive" },
  sin_dato: { label: "Sin dato", dot: "bg-muted-foreground", chip: "border-border text-muted-foreground" },
};

const STATUS_LABEL: Record<MlStatus, string> = {
  ok: "con dato",
  no_data: "no está en ML",
  failed: "ML falló",
  skipped: "no evaluado",
};

export const RUN_STATUS_LABEL: Record<PriceMonitorRun["status"], string> = {
  running: "corriendo",
  ok: "ok",
  degraded: "degradada",
  failed: "falló",
  skipped: "sin cupo de ML",
};

const MATCH_LABEL: Record<string, string> = {
  clip: "por foto",
  "clip+nombre": "foto + nombre",
  llm: "juez IA",
  specs: "medidas",
};

// De dónde salió la publicación y cómo se decidió.
const ORIGIN_LABEL: Record<MatchOrigin, string> = { api: "ficha API", web: "web" };
const SOURCE_LABEL: Record<string, string> = {
  clip: "foto",
  "clip+nombre": "foto + nombre",
  llm: "juez IA",
  specs: "medidas",
};
const DIFFERENCE_LABEL: Record<string, string> = {
  marca: "marca",
  modelo: "modelo",
  medida: "medidas",
  capacidad: "capacidad",
  cantidad: "cantidad (pack)",
  funcion: "función",
  accesorio: "accesorio",
  peso: "peso",
};
const WEB_STATE_LABEL: Record<WebState, string> = {
  ok: "ML web: buscó",
  empty: "ML web: sin publicaciones",
  blocked: "ML web: bloqueada",
  error: "ML web: falló",
  budget: "ML web: sin cupo",
  off: "ML web: cortada esta noche",
};

const ENABLED_OPTIONS: { value: EnabledFilter; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "enabled", label: "Habilitados" },
  { value: "disabled", label: "Deshabilitados" },
];
const MATCH_OPTIONS: { value: MatchFilter | null; label: string }[] = [
  { value: null, label: "Todos" },
  { value: "igual", label: "Con igual" },
  { value: "similar", label: "Con parecidos" },
  { value: "solo_similar", label: "Solo parecidos" },
];
const ORIGIN_OPTIONS: { value: MatchOrigin | null; label: string }[] = [
  { value: null, label: "Todos" },
  { value: "api", label: "Ficha API" },
  { value: "web", label: "Web" },
];

const pct = (v: number | null | undefined): string => (v == null ? "—" : `${Math.round(v * 100)} %`);

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_048_576) return `${(n / 1_048_576).toLocaleString("es-AR", { maximumFractionDigits: 1 })} MB`;
  return `${Math.round(n / 1024).toLocaleString("es-AR")} KB`;
}

const fmtDims = (d: number[]): string => `${d.map((n) => nfmt(n)).join(" × ")} cm`;
const fmtKg = (n: number): string => `${n.toLocaleString("es-AR", { maximumFractionDigits: 2 })} kg`;

function ourDimsText(o: OurSpecs | null | undefined): string {
  if (!o) return "";
  const prod = [o.length, o.width, o.height].filter((n): n is number => !!n);
  const box = [o.box_length, o.box_width, o.box_height].filter((n): n is number => !!n);
  const parts: string[] = [];
  if (prod.length || o.weight) {
    parts.push(`producto ${[prod.length ? fmtDims(prod) : "", o.weight ? fmtKg(o.weight) : ""].filter(Boolean).join(" · ")}`);
  }
  if (box.length || o.box_weight) {
    parts.push(`caja ${[box.length ? fmtDims(box) : "", o.box_weight ? fmtKg(o.box_weight) : ""].filter(Boolean).join(" · ")}`);
  }
  return parts.join(" · ");
}

function listingSpecsText(sp: ListingSpecs | undefined): string {
  if (!sp) return "";
  const parts: string[] = [];
  if (sp.dims_cm?.length) parts.push(sp.dims_cm.map(fmtDims).join(" / "));
  if (sp.weight_kg?.length) parts.push(sp.weight_kg.map(fmtKg).join(" / "));
  if (sp.capacity_ml?.length) parts.push(sp.capacity_ml.map((n) => (n >= 1000 ? `${n / 1000} L` : `${n} ml`)).join(" / "));
  if (sp.quantity) parts.push(`pack x${sp.quantity}`);
  return parts.join(" · ");
}

// Las fotos de ML son de *.mlstatic.com; el backend ya lo valida, esto es lo último
// antes del <img>.
function safeMlImage(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" && (u.hostname === "mlstatic.com" || u.hostname.endsWith(".mlstatic.com"))
      ? u.href
      : null;
  } catch {
    return null;
  }
}

export function ColorDot({ color }: { color: SemaforoColor }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium text-foreground">
      <span className={cn("w-2.5 h-2.5 rounded-full shrink-0", COLOR_META[color].dot)} />
      {COLOR_META[color].label}
    </span>
  );
}

function MiniThumb({ url, alt }: { url: string | null; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (!url || failed || isUnreachableImage(url)) {
    return <div className="w-10 h-10 rounded-md thumb-fallback shrink-0" />;
  }
  return (
    <img
      src={url}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
      className="w-10 h-10 rounded-md object-cover bg-muted shrink-0"
    />
  );
}

const ML_LINK_DOMAINS = ["mercadolibre.com.ar", "mercadolibre.com"];

// Link a una ficha de ML apto para un href. El backend ya lo sanea, pero esto
// es lo último antes del navegador (y cubre filas viejas): solo https y hosts
// de Mercado Libre, parseado con el mismo URL() que va a usar el navegador.
export function safeMlHref(permalink: string | null | undefined, mlId: string): string {
  const fallback = `https://www.mercadolibre.com.ar/p/${encodeURIComponent(mlId)}`;
  if (!permalink) return fallback;
  try {
    const u = new URL(permalink);
    const host = u.hostname.toLowerCase();
    const okHost = ML_LINK_DOMAINS.some((d) => host === d || host.endsWith(`.${d}`));
    if (u.protocol !== "https:" || !okHost || u.username || u.password || (u.port && u.port !== "443")) {
      return fallback;
    }
    return u.href;
  } catch {
    return fallback;
  }
}

function marginClass(color: SemaforoColor): string {
  if (color === "verde") return "text-success";
  if (color === "amarillo") return "text-warning";
  if (color === "rojo") return "text-destructive";
  return "text-muted-foreground";
}

export default function SemaforoView() {
  const qc = useQueryClient();
  const [color, setColor] = useState<SemaforoColor | null>(null);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);
  const [enabled, setEnabled] = useState<EnabledFilter>("all");
  const [match, setMatch] = useState<MatchFilter | null>(null);
  const [origin, setOrigin] = useState<MatchOrigin | null>(null);

  useEffect(() => {
    const t = setTimeout(() => {
      setDebounced(search);
      setPage(0);
    }, 350);
    return () => clearTimeout(t);
  }, [search]);

  const summaryQ = useQuery({
    queryKey: ["pm-summary"],
    queryFn: getPriceMonitorSummary,
    refetchInterval: 30_000,
  });
  const running = summaryQ.data?.running ?? false;

  const snapsQ = useQuery({
    queryKey: ["pm-snapshots", color, page, debounced, enabled, match, origin],
    queryFn: () =>
      getPriceMonitorSnapshots({ page, pageSize: PAGE_SIZE, color, q: debounced, enabled, match, origin }),
    placeholderData: keepPreviousData,
    // Mientras corre, la tabla de la corrida en curso va creciendo.
    refetchInterval: running ? 20_000 : false,
  });

  const data = snapsQ.data;
  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const colorCounts = data?.colors ?? {};
  const allCount = COLOR_ORDER.reduce((acc, c) => acc + (colorCounts[c] ?? 0), 0);
  const lastRun = summaryQ.data?.last_run ?? null;

  function pickColor(c: SemaforoColor | null) {
    setColor(c);
    setPage(0);
    setExpanded(null);
  }

  // Cualquier filtro vuelve a la primera página y cierra el panel abierto.
  function resetView() {
    setPage(0);
    setExpanded(null);
  }

  async function handleRun() {
    try {
      const r = await runPriceMonitor();
      if (r.status === 409 || r.status === 429) {
        // 409: ya corre. 429: sin cupo de ML o la última arrancó hace poco.
        const body = (await r.json().catch(() => ({}))) as { detail?: string };
        setFeedback({ ok: false, text: body.detail || "Ahora no se puede correr." });
      } else if (!r.ok) {
        setFeedback({ ok: false, text: `No se pudo disparar (${r.status}).` });
      } else {
        setFeedback({ ok: true, text: "Corrida disparada en modo sombra. Tarda: se recorre todo el catálogo." });
        [3, 15].forEach((s) =>
          setTimeout(() => {
            qc.invalidateQueries({ queryKey: ["pm-summary"] });
            qc.invalidateQueries({ queryKey: ["pm-snapshots"] });
          }, s * 1000),
        );
      }
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : "Error" });
    }
  }

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
          <IconTrafficLight className="w-5 h-5 text-muted-foreground" />
          Semáforo de precios
          <Badge variant="warning">modo sombra</Badge>
        </h2>
        <div className="flex items-center gap-2">
          <button
            onClick={() => {
              summaryQ.refetch();
              snapsQ.refetch();
            }}
            className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
          >
            <IconRefresh className="w-3.5 h-3.5" />
            Actualizar
          </button>
          <Button size="sm" onClick={handleRun} disabled={running}>
            {running ? "Corriendo…" : "Correr ahora"}
          </Button>
        </div>
      </div>

      <p className="text-sm text-muted-foreground">{SECTION_META.semaforo?.desc}</p>

      {feedback && (
        <p
          className={cn(
            "p-2 rounded-md text-xs border",
            feedback.ok
              ? "bg-success/10 border-success/40 text-success"
              : "bg-warning/10 border-warning/40 text-warning",
          )}
        >
          {feedback.text}
        </p>
      )}

      <RunLine run={lastRun} shownRunId={data?.run_id ?? null} />

      {summaryQ.data?.web?.off_reason && (
        <p className="p-2 rounded-md text-xs border bg-warning/10 border-warning/40 text-warning">
          La búsqueda en la web de Mercado Libre está apagada: {summaryQ.data.web.off_reason}. Los productos sin
          ficha de catálogo quedan sin dato.
        </p>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <FilterChip active={color === null} onClick={() => pickColor(null)}>
          Todos <span className="num-tabular">{nfmt(allCount)}</span>
        </FilterChip>
        {COLOR_ORDER.map((c) => (
          <FilterChip key={c} active={color === c} onClick={() => pickColor(c)} className={COLOR_META[c].chip}>
            <span className={cn("w-2 h-2 rounded-full", COLOR_META[c].dot)} />
            {COLOR_META[c].label} <span className="num-tabular">{nfmt(colorCounts[c] ?? 0)}</span>
          </FilterChip>
        ))}
        <div className="relative ml-auto w-full sm:w-64">
          <IconSearch className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Buscar por nombre o código"
            className="pl-9 h-9"
          />
        </div>
      </div>

      <div className="flex items-center gap-x-5 gap-y-2 flex-wrap">
        <FilterGroup
          label="Producto"
          value={enabled}
          options={ENABLED_OPTIONS}
          onPick={(v) => {
            setEnabled(v);
            resetView();
          }}
        />
        <FilterGroup
          label="Similitud"
          value={match}
          options={MATCH_OPTIONS}
          onPick={(v) => {
            setMatch(v);
            resetView();
          }}
        />
        <FilterGroup
          label="Origen"
          value={origin}
          options={ORIGIN_OPTIONS}
          onPick={(v) => {
            setOrigin(v);
            resetView();
          }}
        />
      </div>

      {snapsQ.error ? (
        <Card className="bg-destructive/10 border-destructive/40 p-6 text-destructive text-sm">
          Error: {snapsQ.error instanceof Error ? snapsQ.error.message : "Error"}
        </Card>
      ) : snapsQ.isPending ? (
        <Card className="p-8 text-center text-muted-foreground text-sm">Cargando…</Card>
      ) : data?.run_id == null ? (
        <Card className="p-8 text-center text-muted-foreground text-sm">
          Todavía no corrió ninguna vez. Corre solo a la noche ({summaryQ.data?.cron_utc ?? "06:00 UTC"}, UTC) o
          con "Correr ahora".
        </Card>
      ) : items.length === 0 ? (
        <Card className="p-8 text-center text-muted-foreground text-sm">No hay productos con ese filtro.</Card>
      ) : (
        <Card className="overflow-x-auto shadow-sm">
          <table className="w-full text-sm">
            <thead className="text-xs text-muted-foreground uppercase tracking-wide border-b border-border">
              <tr>
                <th className="text-left font-medium px-3 py-2">Producto</th>
                <th className="text-left font-medium px-3 py-2">Color</th>
                <th className="text-right font-medium px-3 py-2">Mediana ML</th>
                <th className="text-right font-medium px-3 py-2">Mínimo ML</th>
                <th className="text-right font-medium px-3 py-2">Publ.</th>
                <th className="text-right font-medium px-3 py-2">Nuestro</th>
                <th className="text-right font-medium px-3 py-2">Ganancia</th>
                <th className="text-left font-medium px-3 py-2">Mercado Libre</th>
                <th className="text-left font-medium px-3 py-2">Fecha</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <Fragment key={s.id}>
                  <SnapshotRow
                    s={s}
                    expanded={expanded === s.product.id}
                    onToggle={() => setExpanded(expanded === s.product.id ? null : s.product.id)}
                  />
                  {expanded === s.product.id && (
                    <tr className="bg-muted/40">
                      <td colSpan={9} className="px-3 py-3 space-y-4">
                        <ListingsPanel s={s} />
                        <ProductHistory productId={s.product.id} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {total > 0 && (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span className="num-tabular">
            {nfmt(total)} productos · página {page + 1} de {totalPages}
          </span>
          <div className="flex gap-2">
            <Button variant="secondary" size="sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
              Anterior
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={!data?.has_more}
              onClick={() => setPage(page + 1)}
            >
              Siguiente
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}

function FilterChip({
  active,
  onClick,
  className,
  children,
}: {
  active: boolean;
  onClick: () => void;
  className?: string;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 px-2.5 h-8 rounded-full border text-xs font-medium transition-colors",
        className ?? "border-border text-foreground",
        active ? "bg-primary/10 ring-1 ring-primary/40" : "hover:bg-muted",
      )}
    >
      {children}
    </button>
  );
}

function RunLine({ run, shownRunId }: { run: PriceMonitorRun | null; shownRunId: number | null }) {
  if (!run) return null;
  const when = run.status === "running" ? `arrancó ${fmtTime(run.started_at)}` : `terminó ${fmtTime(run.finished_at)}`;
  return (
    <p className="text-xs text-muted-foreground">
      Última corrida #{run.id} · {RUN_STATUS_LABEL[run.status]} · {when} ·{" "}
      <span className="num-tabular">
        {nfmt(run.processed)}/{nfmt(run.total_products)} productos · {nfmt(run.ml_requests_used)} requests ML
      </span>
      {run.web && (run.web.searches > 0 || run.web.status) && (
        <span className="block mt-0.5">
          ML web:{" "}
          <span className="num-tabular">
            {nfmt(run.web.searches)} búsquedas · {fmtBytes(run.web.bytes)}
            {run.web.bytes_per_search != null && ` (≈ ${fmtBytes(run.web.bytes_per_search)} c/u)`}
            {run.web.blocked > 0 && ` · ${nfmt(run.web.blocked)} bloqueos`} · {nfmt(run.web.n_ok)} con precio
          </span>
          {run.web.status && run.web.status !== "ok" && <span> · {run.web.status}</span>}
        </span>
      )}
      {shownRunId != null && shownRunId !== run.id && (
        <span> · la tabla muestra la corrida #{shownRunId} (la última terminada)</span>
      )}
    </p>
  );
}

function SnapshotRow({
  s,
  expanded,
  onToggle,
}: {
  s: PriceMonitorSnapshot;
  expanded: boolean;
  onToggle: () => void;
}) {
  const name = s.product.name || s.product.id;
  return (
    <tr className="border-b border-border last:border-0 align-top hover:bg-muted/30">
      <td className="px-3 py-2">
        <button onClick={onToggle} className="flex items-center gap-2.5 text-left min-w-[220px]" title="Ver historial">
          <MiniThumb url={s.product.image_url} alt={name} />
          <span className="min-w-0">
            <span className="block font-medium text-foreground truncate max-w-[260px]">{name}</span>
            <span className="block text-xs text-muted-foreground">
              {s.product.code || `#${s.product.id}`} · {expanded ? "ocultar detalle" : "ver detalle"}
              {s.product.enabled === false && (
                <Badge variant="outline" className="ml-1.5 align-middle" title="Deshabilitado en Vendure: se mide solo para mostrar">
                  deshabilitado
                </Badge>
              )}
            </span>
          </span>
        </button>
      </td>
      <td className="px-3 py-2 whitespace-nowrap">
        <ColorDot color={s.color} />
        {s.prev_color && s.prev_color !== s.color && (
          <span className="block text-[11px] text-muted-foreground mt-0.5">antes: {COLOR_META[s.prev_color].label}</span>
        )}
        <span className="block text-[11px] text-muted-foreground mt-0.5" title={s.ml_error ?? undefined}>
          {STATUS_LABEL[s.ml_status]}
        </span>
      </td>
      <td className="px-3 py-2 text-right num-tabular whitespace-nowrap">{fmtArs(s.ml_median_cents)}</td>
      <td className="px-3 py-2 text-right num-tabular whitespace-nowrap">{fmtArs(s.ml_min_cents)}</td>
      <td className="px-3 py-2 text-right num-tabular whitespace-nowrap">
        {s.ml_status === "ok" ? (
          <>
            {s.ml_listing_count}
            <span className="block text-[11px] text-muted-foreground">{s.ml_seller_count} vend.</span>
          </>
        ) : (
          "—"
        )}
      </td>
      <td className="px-3 py-2 text-right num-tabular whitespace-nowrap">
        {fmtArs(s.our_price_cents)}
        {s.tier_used && <span className="block text-[11px] text-muted-foreground">{s.tier_used}</span>}
      </td>
      <td className={cn("px-3 py-2 text-right num-tabular font-semibold whitespace-nowrap", marginClass(s.color))}>
        {fmtPct(s.est_margin_pct)}
      </td>
      <td className="px-3 py-2">
        {s.matched_listings.length === 0 ? (
          (s.similar_count ?? 0) === 0 && <span className="text-xs text-muted-foreground">—</span>
        ) : (
          <div className="space-y-0.5">
            {s.matched_listings.slice(0, 3).map((m) => (
              <a
                key={m.ml_id}
                href={safeMlHref(m.permalink, m.ml_id)}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 text-xs text-primary hover:underline max-w-[220px]"
                title={m.title}
              >
                <IconExternalLink className="w-3 h-3 shrink-0" />
                <span className="truncate">{m.title || m.ml_id}</span>
              </a>
            ))}
            {s.match_source && (
              <span className="block text-[11px] text-muted-foreground">
                igual · {s.match_origin ? `${ORIGIN_LABEL[s.match_origin]} · ` : ""}
                {MATCH_LABEL[s.match_source] ?? s.match_source}
                {s.match_confidence != null && ` (${Math.round(s.match_confidence * 100)}%)`}
              </span>
            )}
          </div>
        )}
        <SimilarChip s={s} />
        {s.web_state && s.web_state !== "ok" && s.ml_status !== "ok" && (
          <span className="block text-[11px] text-muted-foreground mt-0.5" title={s.ml_error ?? undefined}>
            {WEB_STATE_LABEL[s.web_state]}
          </span>
        )}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground whitespace-nowrap">{fmtTime(s.captured_at)}</td>
    </tr>
  );
}

function ProductHistory({ productId }: { productId: string }) {
  const q = useQuery({
    queryKey: ["pm-history", productId],
    queryFn: () => getPriceMonitorHistory(productId),
  });
  if (q.isPending) return <p className="text-xs text-muted-foreground">Cargando historial…</p>;
  if (q.error) return <p className="text-xs text-destructive">No se pudo cargar el historial.</p>;
  const rows = q.data?.items ?? [];
  if (rows.length === 0) return <p className="text-xs text-muted-foreground">Sin historial.</p>;
  return (
    <table className="text-xs w-full max-w-2xl">
      <thead className="text-muted-foreground">
        <tr>
          <th className="text-left font-medium pr-4 py-1">Fecha</th>
          <th className="text-left font-medium pr-4 py-1">Color</th>
          <th className="text-right font-medium pr-4 py-1">Mediana ML</th>
          <th className="text-right font-medium pr-4 py-1">Nuestro</th>
          <th className="text-right font-medium py-1">Ganancia</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((h) => (
          <tr key={h.id}>
            <td className="pr-4 py-0.5 whitespace-nowrap">{fmtTime(h.captured_at)}</td>
            <td className="pr-4 py-0.5">
              <ColorDot color={h.color} />
            </td>
            <td className="pr-4 py-0.5 text-right num-tabular">{fmtArs(h.ml_median_cents)}</td>
            <td className="pr-4 py-0.5 text-right num-tabular">{fmtArs(h.our_price_cents)}</td>
            <td className="py-0.5 text-right num-tabular">{fmtPct(h.est_margin_pct)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FilterGroup<T extends string | null>({
  label,
  value,
  options,
  onPick,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onPick: (v: T) => void;
}) {
  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
      {options.map((o) => (
        <FilterChip key={String(o.value)} active={value === o.value} onClick={() => onPick(o.value)}>
          {o.label}
        </FilterChip>
      ))}
    </div>
  );
}

// Chip gris con borde punteado: lo parecido NO usa verde/amarillo/rojo ni porcentaje de
// ganancia, y no cambia el color del producto.
function SimilarChip({ s }: { s: PriceMonitorSnapshot }) {
  const n = s.similar_count ?? 0;
  if (n === 0) return null;
  const ref = (s.similar_listings ?? []).map((m) => m.price_cents).filter((p): p is number => p != null);
  const lead = s.ml_status === "ok" ? "" : "Sin igual · ";
  return (
    <span
      className="inline-flex mt-1 px-2 py-0.5 rounded-full border border-dashed border-border text-[11px] text-muted-foreground"
      title="Publicaciones parecidas (otra marca conocida, pack, medidas o capacidad). Sirven de referencia y no cuentan para el color."
    >
      {lead}
      {n} {n === 1 ? "parecido" : "parecidos"}
      {ref.length > 0 && ` · ref. ${fmtArs(Math.min(...ref))}`}
    </span>
  );
}

function verdictText(m: MatchedListing, category: MatchCategory): string {
  const parts = [category === "igual" ? "Igual" : "Similar"];
  if (m.origin) parts.push(ORIGIN_LABEL[m.origin]);
  let via = SOURCE_LABEL[m.source ?? ""] ?? m.source ?? "";
  if (m.source === "llm") via = `juez IA${m.confidence != null ? ` ${pct(m.confidence)}` : ""}`;
  else if (m.confidence != null) via += ` · confirmado por juez IA ${pct(m.confidence)}`;
  if (via) parts.push(via);
  return parts.join(" · ");
}

// Qué encontramos en Mercado Libre: lo IGUAL (cuenta para el color) y lo PARECIDO
// (solo referencia), cada publicación con su % de foto y de nombre, de dónde salió y
// el botón "No es el mismo".
function ListingsPanel({ s }: { s: PriceMonitorSnapshot }) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [asking, setAsking] = useState<string | null>(null);
  const [removed, setRemoved] = useState<{ mlId: string; title: string; undone: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const igual = s.matched_listings;
  const similar = s.similar_listings ?? [];

  async function refresh() {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["pm-snapshots"] }),
      qc.invalidateQueries({ queryKey: ["pm-history", s.product.id] }),
      qc.invalidateQueries({ queryKey: ["pm-summary"] }),
    ]);
  }

  async function notSame(m: MatchedListing) {
    setBusy(m.ml_id);
    setError(null);
    try {
      await markNotTheSame(s.id, m.ml_id);
      setAsking(null);
      setRemoved({ mlId: m.ml_id, title: m.title || m.ml_id, undone: false });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "No se pudo guardar");
    } finally {
      setBusy(null);
    }
  }

  async function undo() {
    if (!removed) return;
    setBusy(removed.mlId);
    setError(null);
    try {
      await undoNotTheSame(s.product.id, removed.mlId);
      setRemoved({ ...removed, undone: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "No se pudo deshacer");
    } finally {
      setBusy(null);
    }
  }

  const specsLine = ourDimsText(s.our_specs);
  const webNote = s.web_state && s.web_state !== "ok" && s.ml_error ? s.ml_error : null;

  return (
    <div className="space-y-3">
      {specsLine && (
        <p className="text-xs text-muted-foreground">
          Nuestras medidas (Vendure): <span className="text-foreground">{specsLine}</span>
        </p>
      )}
      {webNote && <p className="text-xs text-muted-foreground">{webNote}</p>}
      {error && <p className="text-xs text-destructive">{error}</p>}
      {removed && (
        <p className="flex items-center gap-2 flex-wrap p-2 rounded-md border border-border bg-muted/40 text-xs">
          {removed.undone ? (
            <span>
              Listo: la próxima corrida vuelve a considerar «{removed.title}». Este detalle se actualiza entonces.
            </span>
          ) : (
            <>
              <span>
                Se sacó «{removed.title}» y no se va a usar para este producto en las próximas corridas.
              </span>
              <Button variant="secondary" size="sm" disabled={busy !== null} onClick={undo}>
                Deshacer
              </Button>
            </>
          )}
        </p>
      )}

      <div>
        <h4 className="text-xs font-semibold text-foreground mb-1.5">
          Iguales <span className="font-normal text-muted-foreground">(cuentan para el color)</span>
        </h4>
        {igual.length === 0 ? (
          <p className="text-xs text-muted-foreground">Ninguna publicación igual a lo nuestro.</p>
        ) : (
          <div className="space-y-1.5">
            {igual.map((m) => (
              <ListingCard
                key={m.ml_id}
                m={m}
                category="igual"
                busy={busy === m.ml_id}
                asking={asking === m.ml_id}
                onAsk={() => setAsking(m.ml_id)}
                onCancel={() => setAsking(null)}
                onConfirm={() => notSame(m)}
              />
            ))}
          </div>
        )}
      </div>

      {similar.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-muted-foreground mb-1.5">
            Parecidos <span className="font-normal">(no cuentan para el color; el precio es solo de referencia)</span>
          </h4>
          <div className="space-y-1.5">
            {similar.map((m) => (
              <ListingCard
                key={m.ml_id}
                m={m}
                category="similar"
                busy={busy === m.ml_id}
                asking={asking === m.ml_id}
                onAsk={() => setAsking(m.ml_id)}
                onCancel={() => setAsking(null)}
                onConfirm={() => notSame(m)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function ListingCard({
  m,
  category,
  busy,
  asking,
  onAsk,
  onCancel,
  onConfirm,
}: {
  m: MatchedListing;
  category: MatchCategory;
  busy: boolean;
  asking: boolean;
  onAsk: () => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const isSimilar = category === "similar";
  const img = safeMlImage(m.image_url);
  const theirs = listingSpecsText(m.specs);
  const price = isSimilar ? m.price_cents : m.median_cents;
  return (
    <div
      className={cn(
        "flex items-start gap-3 rounded-md border px-2.5 py-2",
        isSimilar ? "border-dashed border-border bg-muted/30 text-muted-foreground" : "border-border bg-background",
      )}
    >
      <MiniThumb url={img} alt={m.title} />
      <div className="min-w-0 flex-1 space-y-0.5">
        <a
          href={safeMlHref(m.permalink, m.ml_id)}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
          title={m.title}
        >
          <IconExternalLink className="w-3 h-3 shrink-0" />
          <span className="truncate">{m.title || m.ml_id}</span>
        </a>
        <p className="text-[11px]">
          <span className={cn("font-medium", !isSimilar && "text-foreground")}>{verdictText(m, category)}</span>
          {" · "}foto <span className="num-tabular">{pct(m.image_score)}</span> · nombre{" "}
          <span className="num-tabular">{pct(m.name_score)}</span>
        </p>
        {isSimilar && (m.differences?.length || m.reason) ? (
          <p className="text-[11px] flex flex-wrap items-center gap-1">
            {(m.differences ?? []).map((d) => (
              <Badge key={d} variant="outline">
                {DIFFERENCE_LABEL[d] ?? d}
              </Badge>
            ))}
            {m.reason && <span className="italic">{m.reason}</span>}
          </p>
        ) : null}
        {m.notes && m.notes.length > 0 && (
          <p className="text-[11px] text-warning">{m.notes.join(" · ")}</p>
        )}
        <p className="text-[11px]">
          {m.brand && <>marca {m.brand} · </>}
          {m.seller && <>vende {m.seller} · </>}
          {m.sold_quantity != null && <>{nfmt(m.sold_quantity)}+ vendidos · </>}
          {!isSimilar && m.listings > 1 && <>{m.listings} publicaciones · </>}
          {theirs && <>medidas de la publicación: {theirs}</>}
        </p>
      </div>
      <div className="text-right shrink-0 space-y-1">
        <p className="text-xs num-tabular font-semibold">{fmtArs(price)}</p>
        {asking ? (
          <div className="space-y-1 max-w-[210px]">
            <p className="text-[11px] text-foreground">
              ¿Seguro? Se saca de este producto y no se usa en las próximas corridas.
            </p>
            <div className="flex justify-end gap-1.5">
              <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
                Cancelar
              </Button>
              <Button size="sm" disabled={busy} onClick={onConfirm}>
                {busy ? "Guardando…" : "Sí, no es el mismo"}
              </Button>
            </div>
          </div>
        ) : (
          <Button variant="secondary" size="sm" disabled={busy} onClick={onAsk}>
            No es el mismo
          </Button>
        )}
      </div>
    </div>
  );
}
