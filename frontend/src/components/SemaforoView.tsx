import { Fragment, useEffect, useState, type ReactNode } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getPriceMonitorHistory,
  getPriceMonitorSnapshots,
  getPriceMonitorSummary,
  runPriceMonitor,
} from "../api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { IconExternalLink, IconRefresh, IconSearch, IconTrafficLight } from "../icons";
import { fmtArs, fmtPct, fmtTime, isUnreachableImage, nfmt } from "../lib/format";
import { SECTION_META } from "../sections";
import type { MlStatus, PriceMonitorRun, PriceMonitorSnapshot, SemaforoColor } from "../types";

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
};

const MATCH_LABEL: Record<string, string> = {
  clip: "por foto",
  "clip+nombre": "foto + nombre",
  llm: "juez IA",
};

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
    queryKey: ["pm-snapshots", color, page, debounced],
    queryFn: () => getPriceMonitorSnapshots({ page, pageSize: PAGE_SIZE, color, q: debounced }),
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

  async function handleRun() {
    try {
      const r = await runPriceMonitor();
      if (r.status === 409) {
        setFeedback({ ok: false, text: "Ya hay una corrida en curso." });
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
                      <td colSpan={9} className="px-3 py-3">
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
              {s.product.code || `#${s.product.id}`} · {expanded ? "ocultar historial" : "historial"}
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
          <span className="text-xs text-muted-foreground">—</span>
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
                match {MATCH_LABEL[s.match_source] ?? s.match_source}
                {s.match_confidence != null && ` (${Math.round(s.match_confidence * 100)}%)`}
              </span>
            )}
          </div>
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
