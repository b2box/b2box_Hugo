import { useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { labelStoreMatch, unlabelStoreMatch } from "../api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { IconExternalLink } from "../icons";
import { fmtArs, fmtTime, isUnreachableImage, nfmt } from "../lib/format";
import type {
  CheapestOutside,
  PriceMonitorRun,
  PriceMonitorSnapshot,
  SourceCell as SourceCellData,
  SourceMeta,
  StoreCategory,
  StoreIndexStatus,
  StoreMatchRow,
} from "../types";

// Las fuentes de comparación que no son Mercado Libre: Gadnic, Casa Perfecta y las tiendas que se
// carguen desde Configuración. Son REFERENCIA: no cambian el color del semáforo (que sale de ML)
// salvo que "Tiendas cuentan para el color" esté prendido. Una columna por fuente en la tabla, un
// bloque por tienda en el detalle, filtros por fuente y contadores en Salud.

const CATEGORY_LABEL: Record<StoreCategory, string> = {
  igual: "Idéntico",
  similar: "Similar",
  diferente: "Diferente",
};
const CATEGORY_PLURAL: Record<StoreCategory, string> = {
  igual: "idénticos",
  similar: "similares",
  diferente: "diferentes",
};
const CATEGORY_SINGULAR: Record<StoreCategory, string> = {
  igual: "idéntico",
  similar: "similar",
  diferente: "diferente",
};
// Idéntico: borde liso; similar: punteado largo; diferente: punteado corto. No se usan los colores
// del semáforo (verde/amarillo/rojo) para no confundirlos con el color real.
const CATEGORY_BADGE: Record<StoreCategory, string> = {
  igual: "border-foreground/50 text-foreground",
  similar: "border-dashed text-muted-foreground",
  diferente: "border-dotted text-muted-foreground opacity-80",
};
const CATEGORIES: StoreCategory[] = ["igual", "similar", "diferente"];

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
const SOURCE_LABEL: Record<string, string> = {
  clip: "foto",
  "clip+nombre": "foto + nombre",
  llm: "juez IA",
  specs: "medidas",
  veto: "descartado por foto o nombre",
  none: "sin confirmar",
  manual: "marcado por una persona",
  ambiguo: "parecido, sin confirmar",
};

const pct = (v: number | null | undefined): string => (v == null ? "—" : `${Math.round(v * 100)} %`);

// Último control antes del navegador. El backend ya sanea (https al host de la tienda, foto a su
// CDN); acá solo se vuelve a exigir https, sin usuario ni puerto, parseado con el URL() que usa el
// navegador.
export function safeHttpsUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    if (u.protocol !== "https:" || u.username || u.password || (u.port && u.port !== "443")) return null;
    return u.href;
  } catch {
    return null;
  }
}

function Thumb({ url, alt }: { url: string | null | undefined; alt: string }) {
  const [failed, setFailed] = useState(false);
  const safe = safeHttpsUrl(url);
  if (!safe || failed || isUnreachableImage(safe)) {
    return <div className="w-10 h-10 rounded-md thumb-fallback shrink-0" />;
  }
  return (
    <img
      src={safe}
      alt={alt}
      loading="lazy"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
      className="w-10 h-10 rounded-md object-cover bg-muted shrink-0"
    />
  );
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 px-2.5 h-8 rounded-full border border-border text-xs font-medium text-foreground transition-colors",
        active ? "bg-primary/10 ring-1 ring-primary/40" : "hover:bg-muted",
      )}
    >
      {children}
    </button>
  );
}

function countsText(counts: Record<StoreCategory, number> | undefined): string {
  if (!counts) return "";
  return CATEGORIES.filter((c) => (counts[c] ?? 0) > 0)
    .map((c) => `${counts[c]} ${counts[c] === 1 ? CATEGORY_SINGULAR[c] : CATEGORY_PLURAL[c]}`)
    .join(" · ");
}

// ─── La tabla: una celda por fuente ─────────────────────────────────

export function SourceHeads({ sources }: { sources: SourceMeta[] }) {
  return (
    <>
      {sources.map((s) => (
        <th key={s.key} className="text-left font-medium px-3 py-2">
          {s.label}
        </th>
      ))}
    </>
  );
}

// El mejor resultado de la fuente: el precio del idéntico; si no hay, el del similar (marcado
// "similar"); si no, el más parecido (marcado "diferente"). Con el veredicto en una insignia, el
// link y, si el precio no es creíble, el aviso.
export function SourceCell({ cell, hideCounts = false }: { cell: SourceCellData | undefined; hideCounts?: boolean }) {
  if (!cell || !cell.category) return <span className="text-xs text-muted-foreground">—</span>;
  const href = safeHttpsUrl(cell.url);
  const counts = hideCounts ? "" : countsText(cell.counts);
  return (
    <div className="space-y-0.5 min-w-[150px] max-w-[210px]">
      <div className="flex items-center gap-1.5 flex-wrap">
        <span
          className={cn(
            "text-xs num-tabular",
            cell.category === "igual" ? "font-semibold text-foreground" : "text-muted-foreground",
          )}
        >
          {cell.price_cents != null ? fmtArs(cell.price_cents) : "sin precio"}
        </span>
        <Badge variant="outline" className={CATEGORY_BADGE[cell.category]}>
          {CATEGORY_LABEL[cell.category]}
        </Badge>
        {cell.price_doubtful && (
          <Badge variant="warning" title={cell.price_note ?? "El precio de la tienda no es creíble"}>
            precio dudoso
          </Badge>
        )}
        {cell.stock === 0 && (
          <Badge variant="outline" title="Agotado: se ve, pero no cuenta para el color ni para «más barato afuera»">
            sin stock
          </Badge>
        )}
      </div>
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1 text-xs text-primary hover:underline"
          title={cell.title ?? undefined}
        >
          <IconExternalLink className="w-3 h-3 shrink-0" />
          <span className="truncate">{cell.title || cell.label}</span>
        </a>
      ) : (
        cell.title && <span className="block text-xs text-muted-foreground truncate">{cell.title}</span>
      )}
      {counts && <span className="block text-[11px] text-muted-foreground">{counts}</span>}
    </div>
  );
}

// "Más barato afuera": la fuente con el precio más bajo entre los idénticos de todas (sin precios dudosos).
export function CheapestCell({ c }: { c: CheapestOutside | null | undefined }) {
  if (!c) return <span className="text-xs text-muted-foreground">—</span>;
  const href = safeHttpsUrl(c.url);
  const body = (
    <>
      <span
        className={cn(
          "block text-xs num-tabular",
          c.out_of_stock ? "text-muted-foreground" : "font-semibold text-foreground",
        )}
      >
        {fmtArs(c.price_cents)}
      </span>
      <span className="block text-[11px] text-muted-foreground">
        {c.label}
        {c.out_of_stock && " · sin stock"}
      </span>
    </>
  );
  return href ? (
    <a href={href} target="_blank" rel="noopener noreferrer" className="block hover:underline" title={c.title ?? undefined}>
      {body}
    </a>
  ) : (
    <div title={c.title ?? undefined}>{body}</div>
  );
}

// ─── Filtros por fuente ─────────────────────────────────────────────

export function SourceFilters({
  sources,
  source,
  onSource,
  igualIn,
  onToggleIgual,
}: {
  sources: SourceMeta[];
  source: string | null;
  onSource: (v: string | null) => void;
  igualIn: string[];
  onToggleIgual: (key: string) => void;
}) {
  if (sources.length <= 1) return null;
  const label = "text-[11px] uppercase tracking-wide text-muted-foreground";
  return (
    <>
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className={label} title="Productos que tienen algo de esa fuente (idéntico, similar o diferente)">
          Fuente
        </span>
        <Chip active={source === null} onClick={() => onSource(null)}>
          Todas
        </Chip>
        {sources.map((s) => (
          <Chip key={s.key} active={source === s.key} onClick={() => onSource(source === s.key ? null : s.key)}>
            {s.label}
          </Chip>
        ))}
      </div>
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className={label} title="Productos con un idéntico en alguna de las fuentes elegidas">
          Tiene idéntico en
        </span>
        {sources.map((s) => (
          <Chip key={s.key} active={igualIn.includes(s.key)} onClick={() => onToggleIgual(s.key)}>
            {s.label}
          </Chip>
        ))}
      </div>
    </>
  );
}

// ─── El detalle de un producto: un bloque por tienda ────────────────

export function StoresPanel({ s, affectColor = false }: { s: PriceMonitorSnapshot; affectColor?: boolean }) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState<number | null>(null);
  const [asking, setAsking] = useState<number | null>(null);
  const [done, setDone] = useState<{
    id: number;
    title: string;
    label: "es" | "no_es";
    undone: boolean;
    restored?: "es" | "no_es" | null;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const stores = Object.entries(s.stores ?? {});
  if (stores.length === 0) return null;

  async function refresh() {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["pm-snapshots"] }),
      qc.invalidateQueries({ queryKey: ["pm-summary"] }),
    ]);
  }

  async function correct(m: StoreMatchRow, label: "es" | "no_es") {
    setBusy(m.id);
    setError(null);
    try {
      await labelStoreMatch(m.id, label);
      setAsking(null);
      setDone({ id: m.id, title: m.title || m.store, label, undone: false });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "No se pudo guardar");
    } finally {
      setBusy(null);
    }
  }

  async function undo() {
    if (!done) return;
    setBusy(done.id);
    setError(null);
    try {
      const res = await unlabelStoreMatch(done.id);
      // Si había cambiado de opinión, «Deshacer» vuelve a su marca anterior.
      setDone({ ...done, undone: true, restored: res.match?.human_label ?? null });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "No se pudo deshacer");
    } finally {
      setBusy(null);
    }
  }

  const counted = s.price_basis && s.price_basis !== "ml";
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        Tiendas:{" "}
        {counted
          ? "los idénticos de las tiendas cuentan para el color de este producto."
          : affectColor
            ? "un idéntico de tienda cuenta para el color si tiene stock, un precio creíble y lo confirman la foto + el nombre, las medidas o una persona (el juez IA solo no alcanza)."
            : "solo referencia, no cambian el color (que sale de Mercado Libre)."}
      </p>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {done && (
        <p className="flex items-center gap-2 flex-wrap p-2 rounded-md border border-border bg-muted/40 text-xs">
          {done.undone ? (
            <span>
              Listo: «{done.title}»{" "}
              {done.restored === "es"
                ? "vuelve a idéntico (tu marca anterior)."
                : done.restored === "no_es"
                  ? "vuelve a diferente (tu marca anterior)."
                  : "vuelve a lo que decidió Hugo."}
            </span>
          ) : (
            <>
              <span>
                {done.label === "es"
                  ? `«${done.title}» pasó a idéntico; la próxima corrida lo respeta.`
                  : `«${done.title}» pasó a diferente y no se vuelve a proponer para este producto.`}
              </span>
              <Button variant="secondary" size="sm" disabled={busy !== null} onClick={undo}>
                Deshacer
              </Button>
            </>
          )}
        </p>
      )}
      {stores.map(([id, store]) => (
        <div key={id} className="space-y-2">
          <h4 className="text-xs font-semibold text-foreground">
            {store.label}{" "}
            <span className="font-normal text-muted-foreground">
              {store.matches.length === 0
                ? "· todavía no hay productos de esta tienda para comparar"
                : `· ${CATEGORIES.map((c) => `${store.matches.filter((m) => m.category === c).length} ${CATEGORY_PLURAL[c]}`).join(" · ")}`}
            </span>
          </h4>
          {CATEGORIES.map((cat) => {
            const list = store.matches.filter((m) => m.category === cat);
            if (list.length === 0) return null;
            return (
              <div key={cat} className="space-y-1.5">
                {list.map((m) => (
                  <StoreMatchCard
                    key={m.id}
                    m={m}
                    affectColor={affectColor}
                    busy={busy === m.id}
                    asking={asking === m.id}
                    onAsk={() => setAsking(m.id)}
                    onCancel={() => setAsking(null)}
                    onConfirm={() => correct(m, m.category === "igual" ? "no_es" : "es")}
                  />
                ))}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function StoreMatchCard({
  m,
  affectColor,
  busy,
  asking,
  onAsk,
  onCancel,
  onConfirm,
}: {
  m: StoreMatchRow;
  affectColor: boolean;
  busy: boolean;
  asking: boolean;
  onAsk: () => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const isIgual = m.category === "igual";
  const href = safeHttpsUrl(m.url);
  const via = m.human_label ? "marcado por una persona" : (SOURCE_LABEL[m.source ?? ""] ?? m.source ?? "");
  const action = isIgual ? "No es el mismo" : "Es el mismo";
  return (
    <div
      className={cn(
        "flex items-start gap-3 rounded-md border px-2.5 py-2",
        m.category === "igual" && "border-border bg-background",
        m.category === "similar" && "border-dashed border-border bg-muted/30 text-muted-foreground",
        m.category === "diferente" && "border-dotted border-border bg-muted/20 text-muted-foreground",
      )}
    >
      <Thumb url={m.image_url} alt={m.title} />
      <div className="min-w-0 flex-1 space-y-0.5">
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
            title={m.title}
          >
            <IconExternalLink className="w-3 h-3 shrink-0" />
            <span className="truncate">{m.title || m.store}</span>
          </a>
        ) : (
          <span className="block text-xs font-medium truncate" title={m.title}>
            {m.title || m.store}
          </span>
        )}
        <p className="text-[11px]">
          <span className={cn("font-medium", isIgual && "text-foreground")}>
            {CATEGORY_LABEL[m.category]}
            {via && ` · ${via}`}
          </span>
          {" · "}foto <span className="num-tabular">{pct(m.image_score)}</span> · nombre{" "}
          <span className="num-tabular">{pct(m.name_score)}</span>
        </p>
        {(m.differences.length > 0 || m.reason) && (
          <p className="text-[11px] flex flex-wrap items-center gap-1">
            {m.differences.map((d) => (
              <Badge key={d} variant="outline">
                {DIFFERENCE_LABEL[d] ?? d}
              </Badge>
            ))}
            {m.reason && <span className="italic">{m.reason}</span>}
          </p>
        )}
        {m.notes && <p className="text-[11px] text-warning">{m.notes}</p>}
        <p className="text-[11px]">
          {m.brand && <>marca {m.brand} · </>}
          {m.human_label && <>corregido a mano · </>}
        </p>
      </div>
      <div className="text-right shrink-0 space-y-1">
        <p className="text-xs num-tabular font-semibold">
          {m.price_cents != null ? fmtArs(m.price_cents) : "sin precio"}
        </p>
        {m.price_doubtful && (
          <Badge variant="warning" title={m.price_note ?? "El precio no es creíble: no cuenta para nada"}>
            precio dudoso
          </Badge>
        )}
        {m.stock === 0 && (
          <Badge variant="outline" title="Agotado: no cuenta para el color ni para «más barato afuera»">
            sin stock
          </Badge>
        )}
        {affectColor && m.in_estimate && (
          <Badge variant="outline" title="Similar confirmado, sin diferencia de pack ni capacidad: suma al color estimado">
            entra al estimado
          </Badge>
        )}
        {asking ? (
          <div className="space-y-1 max-w-[210px]">
            <p className="text-[11px] text-foreground">
              {isIgual
                ? "¿Seguro? Pasa a diferente y no se vuelve a proponer para este producto."
                : "¿Seguro? Pasa a idéntico y la próxima corrida lo respeta."}
            </p>
            <div className="flex justify-end gap-1.5">
              <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
                Cancelar
              </Button>
              <Button size="sm" disabled={busy} onClick={onConfirm}>
                {busy ? "Guardando…" : `Sí, ${action.toLowerCase()}`}
              </Button>
            </div>
          </div>
        ) : (
          <Button variant="secondary" size="sm" disabled={busy} onClick={onAsk}>
            {action}
          </Button>
        )}
      </div>
    </div>
  );
}

// ─── Card de Salud: contadores por fuente y estado del índice ───────

export function SourcesStats({ run, stores }: { run: PriceMonitorRun; stores?: StoreIndexStatus[] }) {
  const entries = Object.entries(run.sources ?? {});
  if (entries.length === 0 && !(stores && stores.length > 0)) return null;
  return (
    <div className="mt-4 pt-4 border-t border-border space-y-3">
      {entries.length > 0 && (
        <div>
          <p className="text-xs font-medium text-foreground mb-1.5">
            Productos por fuente <span className="font-normal text-muted-foreground">(corrida #{run.id})</span>
          </p>
          <table className="text-xs w-full max-w-xl">
            <thead className="text-muted-foreground">
              <tr>
                <th className="text-left font-medium pr-4 py-1">Fuente</th>
                <th className="text-right font-medium pr-4 py-1">Idéntico</th>
                <th className="text-right font-medium pr-4 py-1">Similar</th>
                <th className="text-right font-medium pr-4 py-1">Solo diferentes</th>
                <th className="text-right font-medium py-1">Nada</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([key, st]) => (
                <tr key={key}>
                  <td className="pr-4 py-0.5 text-foreground">
                    {st.label}
                    {st.skipped && <span className="block text-[11px] text-warning">{st.skipped}</span>}
                  </td>
                  <td className="pr-4 py-0.5 text-right num-tabular font-semibold">{nfmt(st.igual)}</td>
                  <td className="pr-4 py-0.5 text-right num-tabular">{nfmt(st.similar)}</td>
                  <td className="pr-4 py-0.5 text-right num-tabular">{nfmt(st.diferente)}</td>
                  <td className="py-0.5 text-right num-tabular text-muted-foreground">{nfmt(st.nada)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {stores && stores.length > 0 && (
        <div>
          <p className="text-xs font-medium text-foreground mb-1.5">Índice de las tiendas</p>
          <ul className="space-y-1 text-xs text-muted-foreground">
            {stores.map((st) => (
              <li key={st.id}>
                <span className="text-foreground font-medium">{st.name}</span>
                {!st.enabled && " (apagada)"}: <span className="num-tabular">{nfmt(st.indexed)}</span> leídos de{" "}
                <span className="num-tabular">{nfmt(st.urls)}</span> · <span className="num-tabular">{nfmt(st.dead)}</span>{" "}
                muertos · hoy <span className="num-tabular">{nfmt(st.pages_today)}</span>/
                <span className="num-tabular">{nfmt(st.max_pages_per_day)}</span> páginas
                {st.doubtful_price > 0 && <> · {nfmt(st.doubtful_price)} con precio dudoso</>}
                {(st.failing ?? 0) > 0 && (
                  <>
                    {" "}
                    · {nfmt(st.failing ?? 0)} fichas fallando
                    {(st.errors_5xx ?? 0) > 0 && <> ({nfmt(st.errors_5xx ?? 0)} con error 5xx)</>}
                  </>
                )}
                {st.health === "caida" && (
                  <>
                    {" "}
                    · <span className="text-destructive font-medium">caída: contesta 5xx en todo, la pasada se cortó</span>
                  </>
                )}
                {st.health === "degradada" && (
                  <>
                    {" "}
                    · <span className="text-warning font-medium">degradada: la mitad de las fichas da 5xx</span>
                  </>
                )}
                {st.last_indexed_at && <> · última pasada {fmtTime(st.last_indexed_at)}</>}
                {st.last_index_status && <span className="block">{st.last_index_status}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
