import { useEffect, useState, type ReactNode } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, approveReview, decideReview, getReviewItem, getReviewItems, getReviewSummary, undoReview } from "../api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { IconExternalLink, IconInbox, IconRefresh, IconSearch, IconX } from "../icons";
import { fmtPct, fmtTime, isUnreachableImage, nfmt } from "../lib/format";
import { SECTION_META } from "../sections";
import { StoresPanel, safeHttpsUrl } from "./SourceCells";
import { COLOR_META, FilterChip, ListingsPanel, REVIEW_REASON } from "./SemaforoView";
import type {
  DiagRule,
  MatchOrigin,
  PriceReviewDetail,
  PriceReviewItemRow,
  ReviewAction,
  ReviewBreakdown,
  ReviewCause,
  ReviewEvent,
  ReviewFix,
  ReviewTab,
} from "../types";

// Bandeja de revisión de precios (Pao). Hugo propone los rojos firmes con su diagnóstico; inactiva o
// reactiva en Vendure solo lo que decide una persona de PRICE_REVIEW_DECIDERS. Los botones de decisión
// salen de `actions` (los arma el backend según el estado, la sesión y el modo). Todo lo que viene de
// Mercado Libre, y los textos que escribe Pao, se muestran como texto: nada se renderiza como HTML.

const PAGE_SIZE = 25;

const TABS: { value: ReviewTab; label: string }[] = [
  { value: "para_decidir", label: "Para decidir" },
  { value: "en_proceso", label: "En proceso" },
  { value: "mantenidos", label: "Mantenidos" },
  { value: "cerrados", label: "Cerrados" },
];

const CAUSE_OPTIONS: { value: ReviewCause; label: string }[] = [
  { value: "a", label: "(a) Caja" },
  { value: "b", label: "(b) Tramos" },
  { value: "c", label: "(c) Certificado" },
  { value: "d", label: "(d) Valor por m³" },
  { value: "e", label: "(e) ML bajo la fábrica" },
  { value: "ninguna", label: "Sin causa clara" },
  { value: "sin_dato", label: "Incompleto" },
];

const CAUSE_SHORT: Record<string, string> = {
  a: "(a) Caja",
  b: "(b) Tramos",
  c: "(c) Certificado",
  d: "(d) Valor por m³",
  e: "(e) ML bajo la fábrica",
  ninguna: "Sin causa clara",
  sin_dato: "Diagnóstico incompleto",
};

const ORIGIN_TEXT: Record<MatchOrigin, string> = {
  api: "ficha de catálogo (API de ML)",
  web: "búsqueda web de ML",
  oficina: "búsqueda web de ML desde la oficina",
};

const EVENT_LABEL: Record<string, string> = {
  propuesto: "Hugo lo propuso",
  cerrado_auto: "Se cerró sola",
  aviso_sla: "Aviso por demora",
  aprobado: "Inactivar y revisar",
  decision_corregir: "Decidió corregir",
  decision_mantener: "Decidió mantenerlo activo",
  decision_despublicar: "Decidió despublicar",
  decision_reactivar: "Lo corrigió y pidió reactivar",
  deshacer: "Deshizo la última acción",
  intent: "Pedido a Vendure",
  inactivado: "Inactivado en Vendure",
  reactivado: "Reactivado en Vendure",
  ya_activo: "No hizo falta reactivar",
  vendure_error: "Vendure no aceptó el cambio",
  reactivar_a_mano: "Hay que reactivarlo a mano",
  intent_descartado: "Cambio a medias descartado",
  reabierto: "Se reabrió",
};

// Una frase fija por código de error: el detalle técnico no se muestra.
const VENDURE_ERROR_TEXT: Record<string, string> = {
  lectura: "no se pudo leer el producto",
  escritura: "no aceptó la escritura",
  valor_no_coincide: "respondió otro valor",
  no_existe: "el producto no existe",
};

const STATE_BADGE: Record<string, BadgeProps["variant"]> = {
  propuesto: "warning",
  en_revision: "warning",
  corrigiendo: "default",
  buscando_prov: "default",
  mantenido: "outline",
  despublicado: "destructive",
  corregido: "success",
  cerrado_auto: "success",
};

// Los 10 componentes del precio, siempre con el mismo color.
const PART_COLORS = [
  "#2563eb", "#0ea5e9", "#14b8a6", "#22c55e", "#84cc16", "#eab308", "#f97316", "#ef4444", "#a855f7", "#64748b",
];

// "1 unidad" / "25 unidades".
const units = (n: number): string => `${nfmt(n)} ${n === 1 ? "unidad" : "unidades"}`;
const usd = (v: number | null | undefined): string =>
  v == null ? "—" : v.toLocaleString("es-AR", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const ars = (v: number | null | undefined): string =>
  v == null ? "—" : v.toLocaleString("es-AR", { style: "currency", currency: "ARS", maximumFractionDigits: 0 });

function fmtDays(d: number | null | undefined): string {
  if (d == null) return "—";
  if (d < 1) return "hoy";
  return `hace ${Math.floor(d)} ${Math.floor(d) === 1 ? "día" : "días"}`;
}

function MiniThumb({ url, alt, size = "w-10 h-10" }: { url: string | null; alt: string; size?: string }) {
  const [failed, setFailed] = useState(false);
  const safe = safeHttpsUrl(url);                      // último control antes del <img>: https, sin usuario ni puerto
  if (!safe || failed || isUnreachableImage(safe)) {
    return <div className={cn(size, "rounded-md thumb-fallback shrink-0")} />;
  }
  return (
    <img
      src={safe}
      alt={alt}
      loading="lazy"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
      className={cn(size, "rounded-md object-cover bg-muted shrink-0")}
    />
  );
}

// ─── La pantalla ────────────────────────────────────────────────

export default function BandejaView({ onOpenSemaforo }: { onOpenSemaforo: () => void }) {
  const qc = useQueryClient();
  const [tab, setTab] = useState<ReviewTab>("para_decidir");
  const [cause, setCause] = useState<ReviewCause | null>(null);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    const t = setTimeout(() => {
      setDebounced(search);
      setPage(0);
    }, 350);
    return () => clearTimeout(t);
  }, [search]);

  const summaryQ = useQuery({ queryKey: ["pr-summary"], queryFn: getReviewSummary, refetchInterval: 30_000 });
  const itemsQ = useQuery({
    queryKey: ["pr-items", tab, cause, debounced, page],
    queryFn: () => getReviewItems({ state: tab, cause, q: debounced, page, pageSize: PAGE_SIZE }),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  });

  const summary = summaryQ.data;
  const data = itemsQ.data;
  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const lp = summary?.last_pass ?? null;

  function refresh() {
    qc.invalidateQueries({ queryKey: ["pr-summary"] });
    qc.invalidateQueries({ queryKey: ["pr-items"] });
    qc.invalidateQueries({ queryKey: ["pr-item"] });
  }

  if (selected != null) {
    return (
      <section className="space-y-4">
        <ItemDetail id={selected} onBack={() => setSelected(null)} onChanged={refresh} />
      </section>
    );
  }

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
          <IconInbox className="w-5 h-5 text-muted-foreground" />
          Bandeja de precios
          {summary && (
            <Badge variant={summary.mode === 0 ? "outline" : "warning"}>modo {summary.mode_label}</Badge>
          )}
          {summary?.read_only && <Badge variant="outline">solo lectura</Badge>}
        </h2>
        <button
          onClick={refresh}
          className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
        >
          <IconRefresh className="w-3.5 h-3.5" />
          Actualizar
        </button>
      </div>

      <p className="text-sm text-muted-foreground">{SECTION_META.bandeja?.desc}</p>

      {summary?.mode === 0 && (
        <p className="p-2 rounded-md text-xs border bg-warning/10 border-warning/40 text-warning">
          El modo está en sombra: Hugo marca los rojos pero no llena la bandeja ni manda el resumen diario, y las
          decisiones están frenadas (Deshacer sigue). Para que empiece a proponer, pasá «Modo de la bandeja» a 1 en
          Configuración.
        </p>
      )}
      {summary && summary.mode === 2 && (
        <p className="p-2 rounded-md text-xs border bg-warning/10 border-warning/40 text-warning">
          El modo automático todavía no existe: Hugo se comporta como en propuesta y no inactiva nada.
        </p>
      )}
      {summary?.last_pass_error && (
        <p className="p-2 rounded-md text-xs border bg-destructive/10 border-destructive/40 text-destructive">
          La última pasada de la bandeja falló: {summary.last_pass_error}. La corrida del semáforo quedó bien.
        </p>
      )}

      <p className="text-xs text-muted-foreground">
        {lp ? (
          <>
            Última pasada {fmtTime(lp.at)} · <span className="num-tabular">{nfmt(lp.new)}</span> nuevos ·{" "}
            <span className="num-tabular">{nfmt(lp.closed)}</span> cerradas solas ·{" "}
            <span className="num-tabular">{nfmt(lp.firm)}</span> rojos firmes ·{" "}
            <span className="num-tabular">{nfmt(lp.doubtful)}</span> dudosos
            {lp.read_cap_reached && " · tope de consultas a Vendure alcanzado: algunos diagnósticos quedan para mañana"}
          </>
        ) : (
          "Todavía no corrió ninguna pasada. Corre sola después de la corrida nocturna del semáforo."
        )}
        {summary?.oldest_pending_days != null && summary.pending > 0 && (
          <> · la más vieja espera {fmtDays(summary.oldest_pending_days).replace("hace ", "")}</>
        )}
      </p>

      <div className="flex items-center gap-2 flex-wrap">
        {TABS.map((t) => (
          <FilterChip
            key={t.value}
            active={tab === t.value}
            onClick={() => {
              setTab(t.value);
              setPage(0);
            }}
          >
            {t.label} <span className="num-tabular">{nfmt(summary?.groups[t.value] ?? 0)}</span>
          </FilterChip>
        ))}
        <FilterChip active={false} onClick={onOpenSemaforo} className="border-dashed border-border text-muted-foreground">
          Rojos dudosos → Semáforo <span className="num-tabular">{nfmt(lp?.doubtful ?? 0)}</span>
        </FilterChip>
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

      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Causa probable</span>
        <FilterChip
          active={cause === null}
          onClick={() => {
            setCause(null);
            setPage(0);
          }}
        >
          Todas
        </FilterChip>
        {CAUSE_OPTIONS.map((c) => (
          <FilterChip
            key={c.value}
            active={cause === c.value}
            onClick={() => {
              setCause(cause === c.value ? null : c.value);
              setPage(0);
            }}
          >
            {c.label} <span className="num-tabular">{nfmt(data?.causes[c.value] ?? 0)}</span>
          </FilterChip>
        ))}
      </div>

      {itemsQ.error ? (
        <Card className="bg-destructive/10 border-destructive/40 p-6 text-destructive text-sm">
          Error: {itemsQ.error instanceof Error ? itemsQ.error.message : "Error"}
        </Card>
      ) : itemsQ.isPending ? (
        <Card className="p-8 text-center text-muted-foreground text-sm">Cargando…</Card>
      ) : items.length === 0 ? (
        <Card className="p-8 text-center text-muted-foreground text-sm">
          {tab === "para_decidir" && !cause && !debounced
            ? "No hay productos para decidir. Un rojo entra cuando lleva dos noches seguidas con dato firme."
            : "No hay fichas con ese filtro."}
        </Card>
      ) : (
        <Card className="overflow-x-auto shadow-sm">
          <table className="w-full text-sm">
            <thead className="text-xs text-muted-foreground uppercase tracking-wide border-b border-border">
              <tr>
                <th className="text-left font-medium px-3 py-2">Producto</th>
                <th className="text-right font-medium px-3 py-2">Ganancia</th>
                <th className="text-left font-medium px-3 py-2">Causa probable</th>
                <th className="text-left font-medium px-3 py-2">Estado</th>
                <th className="text-left font-medium px-3 py-2">Espera</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <ItemRow key={it.id} it={it} onOpen={() => setSelected(it.id)} />
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {total > 0 && (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span className="num-tabular">
            {nfmt(total)} fichas · página {page + 1} de {totalPages}
          </span>
          <div className="flex gap-2">
            <Button variant="secondary" size="sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
              Anterior
            </Button>
            <Button variant="secondary" size="sm" disabled={!data?.has_more} onClick={() => setPage(page + 1)}>
              Siguiente
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}

export function ItemRow({ it, onOpen }: { it: PriceReviewItemRow; onOpen: () => void }) {
  const name = it.product.name || it.product.id;
  return (
    <tr className="border-b border-border last:border-0 align-top hover:bg-muted/30">
      <td className="px-3 py-2">
        <button onClick={onOpen} className="flex items-center gap-2.5 text-left min-w-[220px]">
          <MiniThumb url={it.product.image_url} alt={name} />
          <span className="min-w-0">
            <span className="block font-medium text-foreground truncate max-w-[300px]">{name}</span>
            <span className="block text-xs text-muted-foreground">{it.product.code || `#${it.product.id}`}</span>
          </span>
        </button>
      </td>
      <td className="px-3 py-2 text-right num-tabular whitespace-nowrap font-semibold text-destructive">
        {fmtPct(it.last_margin_pct)}
        <span className="block text-[11px] font-normal text-muted-foreground">
          {ars((it.our_price_cents ?? 0) / 100)} vs {ars((it.ml_median_cents ?? 0) / 100)}
        </span>
      </td>
      <td className="px-3 py-2 max-w-[360px]">
        <span className="font-medium text-foreground">{CAUSE_SHORT[it.probable_cause ?? ""] ?? "Diagnóstico pendiente"}</span>
        {it.cause_summary && (
          <span className="block text-xs text-muted-foreground line-clamp-2" title={it.cause_summary}>
            {it.cause_summary}
          </span>
        )}
        {it.diag_status && it.diag_status !== "completo" && (
          <Badge variant="warning" className="mt-1" title="Todavía no se pudo leer el desglose del precio en Vendure">
            diagnóstico {it.diag_status === "pendiente" ? "pendiente" : "parcial"}
          </Badge>
        )}
      </td>
      <td className="px-3 py-2 whitespace-nowrap">
        <Badge variant={STATE_BADGE[it.state] ?? "default"}>{it.state_label}</Badge>
        {it.flags.includes("oficina") && (
          <span className="block text-[11px] text-muted-foreground mt-0.5">dato de la oficina</span>
        )}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground whitespace-nowrap">{fmtDays(it.waiting_days)}</td>
      <td className="px-3 py-2 text-right">
        <Button variant="secondary" size="sm" onClick={onOpen}>
          Ver
        </Button>
      </td>
    </tr>
  );
}

// ─── El detalle de una ficha ────────────────────────────────────

function ItemDetail({ id, onBack, onChanged }: { id: number; onBack: () => void; onChanged: () => void }) {
  const q = useQuery({ queryKey: ["pr-item", id], queryFn: () => getReviewItem(id), refetchInterval: 60_000 });
  const d = q.data;
  return (
    <div className="space-y-4">
      <button onClick={onBack} className="text-xs text-muted-foreground hover:text-foreground transition-colors">
        ← Volver a la bandeja
      </button>
      {q.error ? (
        <Card className="bg-destructive/10 border-destructive/40 p-6 text-destructive text-sm">
          {q.error instanceof Error ? q.error.message : "No se pudo cargar la ficha"}
        </Card>
      ) : !d ? (
        <Card className="p-8 text-center text-muted-foreground text-sm">Cargando…</Card>
      ) : (
        <DetailBody d={d} onChanged={() => { q.refetch(); onChanged(); }} />
      )}
    </div>
  );
}

function SectionTitle({ children }: { children: ReactNode }) {
  return <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2">{children}</h3>;
}

export function DetailBody({ d, onChanged }: { d: PriceReviewDetail; onChanged: () => void }) {
  const name = d.product.name || d.product.id;
  const snap = d.snapshot;
  const diag = d.diagnosis;
  const closed = d.state === "cerrado_auto" || d.state === "corregido" || d.state === "despublicado";
  const closedWhy =
    d.state === "cerrado_auto"
      ? d.events.find((e) => e.kind === "cerrado_auto")?.motive_text ?? "dejó de ser rojo"
      : d.state === "despublicado"
        ? "se decidió despublicar el producto"
        : "se corrigió y se reactivó";
  return (
    <>
      <div className="flex items-center gap-2 flex-wrap">
        <h2 className="text-lg font-semibold text-foreground">{name}</h2>
        <Badge variant={STATE_BADGE[d.state] ?? "default"}>{d.state_label}</Badge>
        <span className="text-xs text-muted-foreground">
          {d.product.code || `#${d.product.id}`} · {d.state === "propuesto" ? `propuesto ${fmtTime(d.opened_at)}` : `desde ${fmtTime(d.state_since)}`}
          {d.streak_nights > 0 && ` · ${d.streak_nights} noches seguidas en rojo`}
        </span>
      </div>

      {!d.costs_visible && (
        <p className="p-2 rounded-md text-xs border bg-muted/40 border-border text-muted-foreground">
          El costo de fábrica y el fee de B2BOX son solo para responsables: acá no se muestran.
        </p>
      )}

      {closed && (
        <p className="p-2 rounded-md text-xs border bg-success/10 border-success/40 text-success">
          Esta ficha ya está cerrada{d.closed_at ? ` (${fmtTime(d.closed_at)})` : ""}: {closedWhy}
        </p>
      )}
      {d.doubtful_today && (
        <p className="p-2 rounded-md text-xs border bg-warning/10 border-warning/40 text-warning">
          Hoy el rojo es dudoso: {d.doubtful_today}. La ficha sigue abierta, pero conviene revisar las publicaciones.
        </p>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="p-4 space-y-3">
          <SectionTitle>Nuestro producto</SectionTitle>
          <div className="flex gap-3">
            <MiniThumb url={d.product.image_url} alt={name} size="w-20 h-20" />
            <div className="min-w-0 space-y-1 text-sm">
              <p className="font-medium text-foreground">{name}</p>
              <p className="text-xs text-muted-foreground">{d.product.code || `#${d.product.id}`}</p>
              {snap?.tier_used && <p className="text-xs text-muted-foreground">Tramo usado: {snap.tier_used.replace("tier:min_qty=", "desde ") + (snap.tier_used.startsWith("tier:") ? " unidades" : "")}</p>}
            </div>
          </div>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
            <dt className="text-muted-foreground">Nuestro precio (con IVA)</dt>
            <dd className="text-right num-tabular font-semibold">{ars((d.our_price_cents ?? 0) / 100)}</dd>
            <dt className="text-muted-foreground">Mediana en ML</dt>
            <dd className="text-right num-tabular">{ars((d.ml_median_cents ?? 0) / 100)}</dd>
            <dt className="text-muted-foreground">Ganancia del revendedor</dt>
            <dd className={cn("text-right num-tabular font-semibold", d.last_color ? COLOR_META[d.last_color].chip.split(" ")[1] : "")}>
              {fmtPct(d.last_margin_pct)}
            </dd>
            {diag && (
              <>
                <dt className="text-muted-foreground" title="El precio más alto con el que el revendedor llegaría al corte del amarillo">
                  Precio máximo para no ser rojo
                </dt>
                <dd className="text-right num-tabular">{ars(diag.p_max_cents / 100)}</dd>
                <dt className="text-muted-foreground">Habría que bajar</dt>
                <dd className="text-right num-tabular">{diag.gap == null ? "ni a precio cero alcanza" : fmtPct(diag.gap * 100)}</dd>
              </>
            )}
          </dl>
          <div className="flex gap-3 text-xs flex-wrap">
            {d.links.storefront && <ExternalLink href={d.links.storefront}>Ficha en la web</ExternalLink>}
            {d.links.vendure_admin && <ExternalLink href={d.links.vendure_admin}>Abrir en Vendure</ExternalLink>}
            {!d.links.storefront && !d.links.vendure_admin && (
              <span className="text-muted-foreground">Sin links configurados al sitio ni a Vendure.</span>
            )}
          </div>
        </Card>

        <Card className="p-4 space-y-3 lg:col-span-2">
          <SectionTitle>Precio: de dónde sale</SectionTitle>
          {d.breakdown && d.breakdown.usd_available ? (
            <BreakdownBlock b={d.breakdown} />
          ) : (
            <p className="text-sm text-muted-foreground">
              {d.diag_status === "completo"
                ? "Este tramo no tiene desglose en dólares (abastecimiento local o sin precio de fábrica)."
                : "Todavía no se pudo leer el desglose del precio en Vendure; Hugo vuelve a intentarlo esta noche."}
            </p>
          )}
          {d.tiers.length > 0 && <TiersTable d={d} />}
        </Card>
      </div>

      {diag && <DiagnosisBlock d={d} />}

      <Card className="p-4 space-y-3">
        <SectionTitle>En Mercado Libre</SectionTitle>
        {snap ? (
          <>
            <dl className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1 text-xs">
              <div>
                <dt className="text-muted-foreground">Mediana de los idénticos</dt>
                <dd className="num-tabular font-semibold">{ars((snap.ml_median_cents ?? 0) / 100)}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Mínimo</dt>
                <dd className="num-tabular">{ars((snap.ml_min_cents ?? 0) / 100)}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Publicaciones firmes / vendedores</dt>
                <dd className="num-tabular">
                  {d.firm_listing_count ?? "—"} / {d.firm_seller_count ?? "—"}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Dato de</dt>
                <dd>
                  {fmtTime(d.data?.at)}
                  {d.data?.origin ? ` · ${ORIGIN_TEXT[d.data.origin]}` : ""}
                </dd>
              </div>
            </dl>
            <ListingsPanel s={snap} onChanged={onChanged} />
            <StoresPanel s={snap} onChanged={onChanged} />
          </>
        ) : (
          <p className="text-sm text-muted-foreground">No quedó guardada la medición de este producto.</p>
        )}
      </Card>

      <DecisionBlock d={d} onChanged={onChanged} />

      <Card className="p-4 space-y-2">
        <SectionTitle>Historia</SectionTitle>
        {d.events.length === 0 ? (
          <p className="text-xs text-muted-foreground">Sin eventos.</p>
        ) : (
          <ul className="space-y-1 text-xs">
            {d.events.map((e) => (
              <EventLine key={e.id} e={e} />
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  // El servidor solo manda links https armados desde plantillas del entorno; igual se chequea acá.
  let ok = false;
  try {
    ok = new URL(href).protocol === "https:";
  } catch {
    ok = false;
  }
  if (!ok) return null;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
      <IconExternalLink className="w-3 h-3" />
      {children}
    </a>
  );
}

function EventLine({ e }: { e: ReviewEvent }) {
  const payload = e.payload ?? {};
  const extra =
    e.kind === "propuesto" && typeof payload.racha === "number"
      ? ` (${payload.racha} noches seguidas en rojo)`
      : e.kind === "vendure_error"
        ? `: ${VENDURE_ERROR_TEXT[e.motive_code ?? ""] ?? "falló"}`
        : e.kind.startsWith("decision_") && e.motive_code
          ? ` (${e.motive_code.replace(/_/g, " ")})`
          : typeof payload.sin_linea_base === "string"
            ? ` (${payload.sin_linea_base})`
            : "";
  return (
    <li className="flex gap-2">
      <span className="text-muted-foreground whitespace-nowrap">{fmtTime(e.at)}</span>
      <span className="text-muted-foreground">{e.actor}</span>
      <span className="text-foreground">
        {EVENT_LABEL[e.kind] ?? e.kind}
        {extra}
        {e.motive_text ? `: ${e.motive_text}` : ""}
      </span>
    </li>
  );
}

// ─── Decisión ───────────────────────────────────────────────────

const VENDURE_EFFECT: Record<"apagar" | "reactivar" | "nada", string> = {
  apagar:
    "Hugo va a INACTIVAR el producto en Vendure: deja de verse en la tienda. Si ya estaba inactivo por otra razón, no lo toca y la ficha se cierra.",
  reactivar:
    "Hugo va a REACTIVAR el producto en Vendure, solo si lo había apagado Hugo y nadie lo apagó después. Si el producto tiene contacto del proveedor, Vendure lo vuelve a publicar en los otros canales; si eso falla, no se reactiva.",
  nada: "No cambia nada en Vendure.",
};

function DecisionBlock({ d, onChanged }: { d: PriceReviewDetail; onChanged: () => void }) {
  const [open, setOpen] = useState<ReviewAction | null>(null);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const main = d.actions.filter((a) => a.code !== "deshacer");
  const undo = d.actions.find((a) => a.code === "deshacer");
  const reasons = Array.from(new Set(d.actions.filter((a) => !a.enabled && a.reason).map((a) => a.reason)));

  function finished(ok: boolean, text: string) {
    setOpen(null);
    setNotice({ ok, text });
    onChanged();
  }

  return (
    <Card className="p-4 space-y-2">
      <SectionTitle>Decisión</SectionTitle>
      {d.decision && (
        <p className="text-xs text-muted-foreground">
          Última decisión: {EVENT_LABEL[`decision_${d.decision.code}`] ?? d.decision.code}
          {d.decision.by ? ` · ${d.decision.by}` : ""}
          {d.decision.at ? ` · ${fmtTime(d.decision.at)}` : ""}
          {d.decision.motive_text ? `: ${d.decision.motive_text}` : ""}
        </p>
      )}
      {main.length === 0 && !undo ? (
        <p className="text-xs text-muted-foreground">Esta ficha no espera ninguna decisión.</p>
      ) : (
        <div className="flex gap-2 flex-wrap items-center">
          {main.map((a) => (
            <Button
              key={a.code}
              variant={a.code === "despublicar" ? "destructive" : "secondary"}
              size="sm"
              disabled={!a.enabled}
              title={a.reason ?? undefined}
              onClick={() => {
                setNotice(null);
                setOpen(a);
              }}
            >
              {a.label}
            </Button>
          ))}
          {undo && (
            <>
              <Button
                variant="outline"
                size="sm"
                disabled={!undo.enabled}
                title={undo.reason ?? undefined}
                onClick={() => {
                  setNotice(null);
                  setOpen(undo);
                }}
              >
                Deshacer
              </Button>
              {undo.deadline && (
                <span className="text-[11px] text-muted-foreground">
                  la última acción, hasta el {fmtTime(undo.deadline)}
                </span>
              )}
            </>
          )}
        </div>
      )}
      {reasons.length > 0 && <p className="text-xs text-muted-foreground">{reasons.join(" · ")}.</p>}
      {notice && (
        <p
          className={cn(
            "p-2 rounded-md text-xs border",
            notice.ok
              ? "bg-success/10 border-success/40 text-success"
              : "bg-warning/10 border-warning/40 text-warning",
          )}
        >
          {notice.text}
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        Si una publicación de Mercado Libre no es el mismo producto, marcala arriba con «No es el mismo»: si el producto
        deja de ser rojo, esta ficha se cierra sola.
      </p>
      {open && <ActionModal d={d} action={open} onClose={() => setOpen(null)} onFinished={finished} />}
    </Card>
  );
}

function ActionModal({
  d,
  action,
  onClose,
  onFinished,
}: {
  d: PriceReviewDetail;
  action: ReviewAction;
  onClose: () => void;
  onFinished: (ok: boolean, text: string) => void;
}) {
  const [motive, setMotive] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const len = text.trim().length;
  const needsText = action.text === "siempre" || (action.text === "otro" && motive === "otro");
  const valid = (action.motives.length === 0 || motive !== "") && (!needsText || len >= 10) && len <= 500;
  const isUndo = action.code === "deshacer";

  async function submit() {
    setBusy(true);
    setError(null);
    const input = { expectedState: d.state, motiveCode: motive || null, motiveText: text.trim() || null };
    try {
      const r =
        action.code === "inactivar"
          ? await approveReview(d.id, input)
          : isUndo
            ? await undoReview(d.id, input)
            : await decideReview(d.id, action.code as "corregir" | "mantener" | "despublicar" | "reactivar", input);
      const extra =
        r.note === "ya_inactivo"
          ? " El producto ya estaba inactivo por otra razón: Hugo no lo tocó y la ficha se cerró."
          : r.note === "ya_activo"
            ? " El producto ya estaba activo: Hugo no escribió nada."
            : "";
      onFinished(true, `Listo: la ficha está en «${r.state_label}».${extra}`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "No se pudo hacer.";
      // 409: la ficha cambió (u otra regla lo frena): se recarga y se avisa. 400, 429, 502: la frase del backend.
      if (e instanceof ApiError && e.status === 409) onFinished(false, msg);
      else setError(msg);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 bg-foreground/40 z-50 flex items-center justify-center p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div className="bg-card rounded-lg border border-border max-w-lg w-full shadow-lg" role="dialog" aria-modal="true">
        <div className="px-5 py-3 border-b border-border flex items-center justify-between">
          <h3 className="font-semibold text-foreground">
            {action.label} · {d.product.code || `#${d.product.id}`}
          </h3>
          <button
            onClick={onClose}
            disabled={busy}
            aria-label="Cerrar"
            className="w-7 h-7 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted flex items-center justify-center transition-colors"
          >
            <IconX className="w-4 h-4" />
          </button>
        </div>
        <div className="p-5 space-y-3 text-sm">
          {action.motives.length > 0 && (
            <label className="block space-y-1">
              <span className="text-xs text-muted-foreground">Motivo</span>
              <select
                value={motive}
                onChange={(e) => setMotive(e.target.value)}
                className="w-full h-9 rounded-md border border-input bg-background px-2 text-sm"
              >
                <option value="">Elegí un motivo…</option>
                {action.motives.map((m) => (
                  <option key={m.code} value={m.code}>
                    {m.label}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="block space-y-1">
            <span className="text-xs text-muted-foreground">
              {needsText ? "Contá el motivo (obligatorio, al menos 10 caracteres)" : "Nota (opcional)"}
            </span>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              maxLength={500}
              rows={3}
              className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm"
            />
            <span className={cn("block text-right text-[11px]", needsText && len < 10 ? "text-warning" : "text-muted-foreground")}>
              {len}/500
            </span>
          </label>
          <div className="p-2 rounded-md text-xs border bg-muted/40 border-border text-foreground space-y-1">
            <p>
              {isUndo ? "Vuelve" : "La ficha pasa"} a «{action.to_state_label ?? action.to_state}».
            </p>
            <p>{VENDURE_EFFECT[action.vendure ?? "nada"]}</p>
          </div>
          {error && (
            <p className="p-2 rounded-md text-xs border bg-destructive/10 border-destructive/40 text-destructive">{error}</p>
          )}
        </div>
        <div className="px-5 py-3 border-t border-border flex justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={onClose} disabled={busy}>
            Cancelar
          </Button>
          <Button
            variant={action.code === "despublicar" ? "destructive" : "default"}
            size="sm"
            onClick={submit}
            disabled={!valid || busy}
          >
            {busy ? "Guardando…" : `Confirmar: ${action.label}`}
          </Button>
        </div>
      </div>
    </div>
  );
}

// ─── El precio ──────────────────────────────────────────────────

function BreakdownBlock({ b }: { b: ReviewBreakdown }) {
  const comps = b.components;
  const restricted = b.restricted === true;
  const total = comps.reduce((acc, c) => acc + Math.max(c.usd, 0), 0) || 1;
  return (
    <div className="space-y-2">
      {restricted ? (
        <p className="text-xs text-muted-foreground">
          Tramo de {units(b.quantity)}: precio de {ars(b.factura_unit_cents / 100)} por unidad con IVA. Se muestran las partes
          que no son la fábrica; la fábrica y el fee son solo para responsables. Lo arma Vendure; Hugo no recalcula.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          Tramo de {units(b.quantity)}: factura de <span className="num-tabular text-foreground">{usd(b.factura_usd)}</span>{" "}
          (<span className="num-tabular">{ars(b.factura_unit_cents / 100)}</span> por unidad con IVA). Fábrica a ¥
          {(b.cny_unit ?? 0).toLocaleString("es-AR", { maximumFractionDigits: 2 })} por unidad. Lo arma Vendure; Hugo no recalcula.
        </p>
      )}
      {!restricted && (
        <div className="flex h-3 w-full overflow-hidden rounded-full border border-border" role="img" aria-label="Partes del precio">
          {comps.map((c, i) =>
            c.usd > 0 ? (
              <div
                key={c.key}
                title={`${c.label}: ${usd(c.usd)} (${fmtPct(c.pct)})`}
                style={{ width: `${(c.usd / total) * 100}%`, background: PART_COLORS[i % PART_COLORS.length] }}
              />
            ) : null,
          )}
        </div>
      )}
      <table className="w-full text-xs">
        <thead className="text-muted-foreground uppercase tracking-wide">
          <tr>
            <th className="text-left font-medium py-1">Parte</th>
            <th className="text-right font-medium py-1">USD</th>
            {!restricted && <th className="text-right font-medium py-1">$ por unidad</th>}
            {!restricted && <th className="text-right font-medium py-1">% del precio</th>}
          </tr>
        </thead>
        <tbody>
          {comps.map((c, i) => (
            <tr key={c.key} className="border-t border-border align-top">
              <td className="py-1">
                <span className="inline-block w-2 h-2 rounded-full mr-1.5" style={{ background: PART_COLORS[i % PART_COLORS.length] }} />
                {c.label}
                {c.detail.length > 0 && (
                  <span className="block text-[11px] text-muted-foreground pl-3.5">
                    {c.detail.filter((x) => x.usd > 0).map((x) => `${x.code.replace(/_/g, " ")} ${usd(x.usd)}`).join(" · ")}
                  </span>
                )}
              </td>
              <td className="py-1 text-right num-tabular">{usd(c.usd)}</td>
              {!restricted && <td className="py-1 text-right num-tabular">{ars(c.ars_unit)}</td>}
              {!restricted && <td className="py-1 text-right num-tabular">{fmtPct(c.pct)}</td>}
            </tr>
          ))}
          {restricted && (
            <tr className="border-t border-border text-muted-foreground">
              <td className="py-1">Fábrica y fee</td>
              <td className="py-1 text-right" colSpan={1}>solo para responsables</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function TiersTable({ d }: { d: PriceReviewDetail }) {
  return (
    <div className="space-y-1">
      <SectionTitle>Tramos de cantidad</SectionTitle>
      <table className="w-full text-xs">
        <thead className="text-muted-foreground uppercase tracking-wide">
          <tr>
            <th className="text-left font-medium py-1">Unidades</th>
            <th className="text-right font-medium py-1">Fábrica (¥)</th>
            <th className="text-right font-medium py-1">Neto</th>
            <th className="text-right font-medium py-1">Precio con IVA</th>
            <th className="text-right font-medium py-1">Ganancia contra ML</th>
          </tr>
        </thead>
        <tbody>
          {d.tiers.map((t, i) => (
            <tr key={i} className="border-t border-border">
              <td className="py-1">
                {t.min_quantity ?? "?"}
                {t.max_quantity != null && t.max_quantity !== t.min_quantity ? ` a ${t.max_quantity}` : t.max_quantity != null ? "" : " o más"}
              </td>
              <td className="py-1 text-right num-tabular">
                {t.cny_unit === undefined ? (
                  <span className="text-muted-foreground" title="El costo de fábrica es solo para responsables">solo responsables</span>
                ) : (
                  t.cny_unit.toLocaleString("es-AR", { maximumFractionDigits: 2 })
                )}
              </td>
              <td className="py-1 text-right num-tabular">{ars(t.neto_cents / 100)}</td>
              <td className="py-1 text-right num-tabular">{ars(t.price_cents / 100)}</td>
              <td
                className={cn(
                  "py-1 text-right num-tabular font-medium",
                  t.margin_pct == null ? "text-muted-foreground" : t.margin_pct < 10 ? "text-destructive" : t.margin_pct < 30 ? "text-warning" : "text-success",
                )}
              >
                {fmtPct(t.margin_pct)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── El diagnóstico a-e ─────────────────────────────────────────

const STATUS_BADGE: Record<DiagRule["status"], { label: string; variant: BadgeProps["variant"] }> = {
  dispara: { label: "SÍ", variant: "destructive" },
  no: { label: "NO", variant: "outline" },
  sin_dato: { label: "SIN DATO", variant: "warning" },
  no_aplica: { label: "NO APLICA", variant: "default" },
};

const FIX_TEXT: Record<ReviewFix, string> = {
  si: "Arreglarla saca el producto del rojo: sí",
  no: "Arreglarla saca el producto del rojo: no",
  aprox: "Arreglarla saca el producto del rojo: no se puede asegurar",
};

function DiagnosisBlock({ d }: { d: PriceReviewDetail }) {
  const diag = d.diagnosis!;
  const cause = diag.probable_cause;
  return (
    <Card className="p-4 space-y-3">
      <SectionTitle>Diagnóstico</SectionTitle>
      <p className="text-sm">
        <span className="text-muted-foreground">Causa probable: </span>
        <span className="font-semibold text-foreground">{CAUSE_SHORT[cause] ?? cause}</span>
        {d.diag_status && d.diag_status !== "completo" && (
          <Badge variant="warning" className="ml-2">
            {d.diag_status === "pendiente" ? "se completa esta noche" : "incompleto"}
          </Badge>
        )}
      </p>
      {diag.fix_not_enough && (
        <p className="p-2 rounded-md text-xs border bg-warning/10 border-warning/40 text-warning">
          {diag.fixable === "aprox"
            ? "Corregir lo nuestro quizá no alcance para sacarlo del rojo."
            : "Corregir lo nuestro no alcanza para sacarlo del rojo: pasa por el proveedor, el producto o el mercado."}
        </p>
      )}
      <ul className="space-y-2">
        {diag.rules.map((r) => (
          <li key={r.code} className={cn("rounded-md border px-3 py-2", r.code === cause ? "border-destructive/50 bg-destructive/5" : "border-border")}>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-medium text-foreground">
                ({r.code}) {r.title}
              </span>
              <Badge variant={STATUS_BADGE[r.status].variant}>{STATUS_BADGE[r.status].label}</Badge>
              {r.status === "dispara" && r.fixes && <span className="text-[11px] text-muted-foreground">{FIX_TEXT[r.fixes]}</span>}
              {r.status === "dispara" && r.estimated && <Badge variant="outline">estimado</Badge>}
            </div>
            <p className="text-xs text-muted-foreground mt-1">{r.summary}</p>
            {r.status === "dispara" && r.action && <p className="text-xs text-foreground mt-1">Qué hacer: {r.action}</p>}
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">Pista: {diag.suggestion}.</p>
      {diag.missing.length > 0 && (
        <p className="text-xs text-muted-foreground">No se pudo leer: {diag.missing.join(", ")}.</p>
      )}
      {d.flags.some((f) => f.startsWith("dudoso:")) && (
        <p className="text-xs text-muted-foreground">
          {d.flags
            .filter((f) => f.startsWith("dudoso:"))
            .map((f) => REVIEW_REASON[f.slice(7)] ?? f.slice(7))
            .join(", ")}
        </p>
      )}
    </Card>
  );
}
