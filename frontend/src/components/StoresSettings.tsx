import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createStore, deleteStore, getStores, indexStoreNow, updateStore } from "../api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { fmtTime, nfmt } from "../lib/format";
import type { MarketStore, MarketStoreInput, StorePlatform } from "../types";

// Tiendas que se usan como fuente de comparación del semáforo (Casa Perfecta, Gadnic…). Agregar otra
// tienda es completar este formulario: no hace falta código ni deploy. Hugo lee el sitemap de la
// tienda (respetando su robots.txt, a un ritmo suave y con un tope de páginas por día) y compara
// contra lo que quedó indexado. Son referencia: no cambian el color salvo el ajuste "Tiendas
// cuentan para el color" (grupo Semáforo de precios, arriba).

const PLATFORM_LABEL: Record<StorePlatform, string> = {
  tiendanube: "Tiendanube",
  jsonld_sitemap: "Sitio propio (sitemap + JSON-LD)",
};

const FIELD = "h-9 w-full rounded-md border border-input bg-background px-2 text-sm text-foreground";

type Draft = {
  name: string;
  base_url: string;
  platform: StorePlatform;
  enabled: boolean;
  refresh_days: string;
  max_pages_per_day: string;
  sitemap_url: string;
  image_hosts: string;
  house_brand: string;
  notes: string;
};

const EMPTY: Draft = {
  name: "",
  base_url: "https://",
  platform: "tiendanube",
  enabled: true,
  refresh_days: "7",
  max_pages_per_day: "1000",
  sitemap_url: "",
  image_hosts: "",
  house_brand: "",
  notes: "",
};

function toDraft(s: MarketStore): Draft {
  return {
    name: s.name,
    base_url: s.base_url,
    platform: s.platform,
    enabled: s.enabled,
    refresh_days: String(s.refresh_days),
    max_pages_per_day: String(s.max_pages_per_day),
    sitemap_url: s.sitemap_url ?? "",
    image_hosts: s.image_hosts ?? "",
    house_brand: s.house_brand ?? "",
    notes: s.notes ?? "",
  };
}

function toInput(d: Draft): MarketStoreInput {
  return {
    name: d.name.trim(),
    base_url: d.base_url.trim(),
    platform: d.platform,
    enabled: d.enabled,
    refresh_days: Number(d.refresh_days),
    max_pages_per_day: Number(d.max_pages_per_day),
    sitemap_url: d.sitemap_url.trim(),
    image_hosts: d.image_hosts.trim(),
    house_brand: d.house_brand.trim(),
    notes: d.notes.trim(),
  };
}

export default function StoresSettings() {
  const qc = useQueryClient();
  const storesQ = useQuery({ queryKey: ["stores"], queryFn: getStores, refetchInterval: 30_000 });
  const [adding, setAdding] = useState(false);
  const stores = storesQ.data?.items ?? [];

  async function refresh() {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["stores"] }),
      qc.invalidateQueries({ queryKey: ["pm-snapshots"] }),
      qc.invalidateQueries({ queryKey: ["pm-summary"] }),
    ]);
  }

  return (
    <Card className="p-5 shadow-sm space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h3 className="text-sm font-semibold text-foreground uppercase tracking-wide">
          Tiendas de comparación
        </h3>
        {!adding && (
          <Button size="sm" onClick={() => setAdding(true)}>
            Agregar tienda
          </Button>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        Hugo compara cada producto contra Mercado Libre y contra estas tiendas, en la misma corrida. Lee el sitemap de
        cada tienda de madrugada (sin usar su buscador, respetando su robots.txt, una página cada 2-3 segundos y hasta
        el tope diario) y compara contra lo indexado. Son referencia: no cambian el color. Para sumar otra tienda
        Tiendanube, cargala acá con su dirección y elegí «Tiendanube».
      </p>

      {adding && (
        <StoreForm
          title="Tienda nueva"
          initial={EMPTY}
          submitLabel="Agregar"
          onCancel={() => setAdding(false)}
          onSubmit={async (d) => {
            await createStore(toInput(d));
            setAdding(false);
            await refresh();
          }}
        />
      )}

      {storesQ.error ? (
        <p className="text-sm text-destructive">
          No se pudieron leer las tiendas: {storesQ.error instanceof Error ? storesQ.error.message : "Error"}
        </p>
      ) : storesQ.isPending ? (
        <p className="text-sm text-muted-foreground">Cargando tiendas…</p>
      ) : stores.length === 0 ? (
        <p className="text-sm text-muted-foreground">No hay tiendas cargadas.</p>
      ) : (
        <div className="space-y-3">
          {stores.map((s) => (
            <StoreRow key={s.id} store={s} onChanged={refresh} />
          ))}
        </div>
      )}
    </Card>
  );
}

function StoreRow({ store, onChanged }: { store: MarketStore; onChanged: () => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const ix = store.index;

  async function run(action: () => Promise<unknown>, okText: string) {
    setBusy(true);
    setMessage(null);
    try {
      await action();
      setMessage({ ok: true, text: okText });
      await onChanged();
    } catch (err) {
      setMessage({ ok: false, text: err instanceof Error ? err.message : "Error" });
    } finally {
      setBusy(false);
    }
  }

  if (editing) {
    return (
      <StoreForm
        title={`Editar ${store.name}`}
        initial={toDraft(store)}
        submitLabel="Guardar"
        onCancel={() => setEditing(false)}
        onSubmit={async (d) => {
          await updateStore(store.id, toInput(d));
          setEditing(false);
          await onChanged();
        }}
      />
    );
  }

  return (
    <div className="rounded-md border border-border p-3 space-y-2">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium text-foreground">{store.name}</span>
          <Badge variant="outline">{PLATFORM_LABEL[store.platform] ?? store.platform}</Badge>
          {!store.enabled && <Badge variant="warning">apagada</Badge>}
          {store.house_brand && (
            <Badge variant="outline" title="Su marca propia se trata como genérica (marca genérica = idéntico)">
              marca propia: {store.house_brand}
            </Badge>
          )}
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <Button
            variant="secondary"
            size="sm"
            disabled={busy || !store.enabled}
            onClick={() =>
              run(() => indexStoreNow(store.id), "Indexado en curso: tarda (una página cada 2-3 segundos).")
            }
          >
            Indexar ahora
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() =>
              run(
                () => updateStore(store.id, { enabled: !store.enabled }),
                store.enabled ? "Tienda apagada: deja de compararse." : "Tienda prendida.",
              )
            }
          >
            {store.enabled ? "Apagar" : "Prender"}
          </Button>
          <Button variant="secondary" size="sm" disabled={busy} onClick={() => setEditing(true)}>
            Editar
          </Button>
          <Button
            variant="destructive"
            size="sm"
            disabled={busy}
            onClick={() => {
              if (
                window.confirm(
                  `¿Borrar ${store.name}? Se borra también todo lo que se leyó de la tienda y sus coincidencias.`,
                )
              ) {
                void run(() => deleteStore(store.id), "Tienda borrada.");
              }
            }}
          >
            Borrar
          </Button>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        {store.base_url} · se vuelve a leer cada {store.refresh_days} días · hasta {nfmt(store.max_pages_per_day)} páginas por día
      </p>
      {ix && (
        <p className="text-xs text-muted-foreground">
          <span className="num-tabular">{nfmt(ix.indexed)}</span> productos leídos de{" "}
          <span className="num-tabular">{nfmt(ix.urls)}</span> URLs · <span className="num-tabular">{nfmt(ix.dead)}</span>{" "}
          muertas · <span className="num-tabular">{nfmt(ix.never_read)}</span> sin leer todavía · hoy{" "}
          <span className="num-tabular">{nfmt(ix.pages_today)}</span>/<span className="num-tabular">{nfmt(ix.max_pages_per_day)}</span>{" "}
          páginas
          {ix.doubtful_price > 0 && <> · {nfmt(ix.doubtful_price)} con precio dudoso</>}
        </p>
      )}
      {store.last_index_status && (
        <p className="text-xs text-muted-foreground">
          Última pasada {fmtTime(store.last_indexed_at)}: {store.last_index_status}
        </p>
      )}
      {store.notes && <p className="text-xs text-muted-foreground italic">{store.notes}</p>}
      {message && <p className={message.ok ? "text-xs text-success" : "text-xs text-destructive"}>{message.text}</p>}
    </div>
  );
}

function StoreForm({
  title,
  initial,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  title: string;
  initial: Draft;
  submitLabel: string;
  onSubmit: (d: Draft) => Promise<void>;
  onCancel: () => void;
}) {
  const [d, setD] = useState<Draft>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((cur) => ({ ...cur, [k]: v }));

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await onSubmit(d);
    } catch (err) {
      setError(err instanceof Error ? err.message : "No se pudo guardar");
    } finally {
      setBusy(false);
    }
  }

  const label = "block text-xs font-medium text-foreground mb-1";
  const hint = "block text-[11px] text-muted-foreground mt-1";
  return (
    <div className="rounded-md border border-primary/40 bg-muted/30 p-4 space-y-3">
      <h4 className="text-sm font-semibold text-foreground">{title}</h4>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label className={label}>Nombre</label>
          <Input value={d.name} maxLength={60} onChange={(e) => set("name", e.target.value)} placeholder="Casa Perfecta" />
        </div>
        <div>
          <label className={label}>Dirección de la tienda</label>
          <Input value={d.base_url} onChange={(e) => set("base_url", e.target.value)} placeholder="https://www.mitienda.com.ar" />
          <span className={hint}>Solo https, sin usuario ni puerto.</span>
        </div>
        <div>
          <label className={label}>Plataforma</label>
          <select className={FIELD} value={d.platform} onChange={(e) => set("platform", e.target.value as StorePlatform)}>
            {(Object.keys(PLATFORM_LABEL) as StorePlatform[]).map((p) => (
              <option key={p} value={p}>
                {PLATFORM_LABEL[p]}
              </option>
            ))}
          </select>
          <span className={hint}>Tiendanube sirve para cualquier tienda hecha con Tiendanube.</span>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={label}>Releer cada (días)</label>
            <Input type="number" min={1} max={90} value={d.refresh_days} onChange={(e) => set("refresh_days", e.target.value)} />
          </div>
          <div>
            <label className={label}>Páginas por día</label>
            <Input
              type="number"
              min={1}
              max={20000}
              value={d.max_pages_per_day}
              onChange={(e) => set("max_pages_per_day", e.target.value)}
            />
          </div>
        </div>
        <div>
          <label className={label}>Sitemap (opcional)</label>
          <Input value={d.sitemap_url} onChange={(e) => set("sitemap_url", e.target.value)} placeholder="https://…/sitemap.xml" />
          <span className={hint}>Vacío = /sitemap.xml de la tienda.</span>
        </div>
        <div>
          <label className={label}>Dominios de las fotos (opcional)</label>
          <Input value={d.image_hosts} onChange={(e) => set("image_hosts", e.target.value)} placeholder="mitienda.com.ar, cdn.mitienda.com" />
          <span className={hint}>Vacío = la tienda (y mitiendanube.com si es Tiendanube). Solo se aceptan fotos de acá.</span>
        </div>
        <div>
          <label className={label}>Marca propia (opcional)</label>
          <Input value={d.house_brand} onChange={(e) => set("house_brand", e.target.value)} placeholder="Gadnic" />
          <span className={hint}>Se trata como marca genérica: no baja un idéntico a similar.</span>
        </div>
        <div>
          <label className={label}>Notas</label>
          <Input value={d.notes} onChange={(e) => set("notes", e.target.value)} />
        </div>
      </div>
      <label className="flex items-center gap-2 text-xs text-foreground">
        <input type="checkbox" checked={d.enabled} onChange={(e) => set("enabled", e.target.checked)} />
        Prendida (se indexa y se compara)
      </label>
      {error && <p className="text-xs text-destructive">{error}</p>}
      <div className="flex items-center gap-2">
        <Button size="sm" disabled={busy} onClick={submit}>
          {busy ? "Guardando…" : submitLabel}
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancelar
        </Button>
      </div>
    </div>
  );
}
