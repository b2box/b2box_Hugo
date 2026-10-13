import { useEffect, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  downloadSeoCsv,
  getSeoItems,
  getSeoLists,
  getSeoSummary,
  resetSeoList,
  runSeoAudit,
  saveSeoList,
  type SeoChannel,
  type SeoEnabled,
  type SeoQuery,
} from "../api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { IconFileText, IconRefresh, IconSearch } from "../icons";
import { fmtTime, nfmt } from "../lib/format";
import { SECTION_META } from "../sections";
import { FilterChip, FilterGroup } from "./SemaforoView";
import type { SeoItem, SeoListName, SeoRule, SeoRun } from "../types";

// Auditoría de textos del catálogo: Hugo lee Vendure (solo lectura), aplica reglas
// fijas sin IA y guarda el resultado. Esta vista solo muestra. Los campos de
// proveedor nunca llegan acá: la regla FAB dice únicamente que hubo coincidencia.

const PAGE_SIZE = 25;

const ENABLED_OPTIONS: { value: SeoEnabled; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "enabled", label: "Habilitados" },
  { value: "disabled", label: "Deshabilitados" },
];
const CHANNEL_OPTIONS: { value: SeoChannel; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "ar", label: "Argentina" },
  { value: "solo_default", label: "Solo canal por defecto" },
];

const RUN_STATUS_LABEL: Record<SeoRun["status"], string> = {
  running: "corriendo",
  ok: "ok",
  degraded: "incompleta",
  failed: "falló",
};

// Color del chip de cada regla, por grupo.
const GROUP_VARIANT: Record<string, BadgeProps["variant"]> = {
  titulo: "warning",
  marcas: "destructive",
  duplicados: "default",
  descripcion: "outline",
  traduccion: "destructive",
};

const LIST_META: { name: SeoListName; label: string; help: string }[] = [
  {
    name: "marcas",
    label: "Marcas y personajes de terceros",
    help: "Regla MAR. Una por línea. Se compara sin tildes ni mayúsculas y por palabra entera.",
  },
  {
    name: "relleno",
    label: "Relleno de marketing",
    help: "Regla RELLENO. Frases o palabras que no aportan al título. Una por línea.",
  },
  {
    name: "tecnicos",
    label: "Datos técnicos permitidos en mayúsculas",
    help: "Regla COD. Siglas como LED o USB que no son códigos de modelo. Una por línea.",
  },
];

function channelText(i: SeoItem): string {
  const parts: string[] = [];
  if (i.in_ar === true) parts.push("Argentina");
  else if (i.in_ar === null) parts.push("Argentina ?");
  if (i.in_default === true) parts.push("por defecto");
  else if (i.in_default === null) parts.push("por defecto ?");
  return parts.join(" + ") || "—";
}

export default function SeoTextosView() {
  const qc = useQueryClient();
  const [rule, setRule] = useState<string | null>(null);
  const [enabled, setEnabled] = useState<SeoEnabled>("all");
  const [channel, setChannel] = useState<SeoChannel>("all");
  const [lang, setLang] = useState<string | null>(null);
  const [onlyIssues, setOnlyIssues] = useState(true);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => {
      setDebounced(search);
      setPage(0);
    }, 350);
    return () => clearTimeout(t);
  }, [search]);

  const summaryQ = useQuery({ queryKey: ["seo-summary"], queryFn: getSeoSummary, refetchInterval: 30_000 });
  const running = summaryQ.data?.running ?? false;
  const rules: SeoRule[] = summaryQ.data?.rules ?? [];

  const query: SeoQuery = { rule, enabled, lang, channel, q: debounced, onlyIssues, page, pageSize: PAGE_SIZE };
  const itemsQ = useQuery({
    queryKey: ["seo-items", rule, enabled, lang, channel, debounced, onlyIssues, page],
    queryFn: () => getSeoItems(query),
    placeholderData: keepPreviousData,
    refetchInterval: running ? 10_000 : false,
  });

  const data = itemsQ.data;
  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const counts = data?.counts ?? {};
  const run = summaryQ.data?.run ?? null;

  function resetView() {
    setPage(0);
  }

  function refresh() {
    qc.invalidateQueries({ queryKey: ["seo-summary"] });
    qc.invalidateQueries({ queryKey: ["seo-items"] });
  }

  async function handleRun() {
    try {
      const r = await runSeoAudit();
      if (r.status === 409 || r.status === 429) {
        const body = (await r.json().catch(() => ({}))) as { detail?: string };
        setFeedback({ ok: false, text: body.detail || "Ahora no se puede correr." });
      } else if (!r.ok) {
        setFeedback({ ok: false, text: `No se pudo disparar (${r.status}).` });
      } else {
        setFeedback({ ok: true, text: "Auditoría disparada. Solo lee Vendure; tarda unos segundos." });
        [3, 10, 30].forEach((s) => setTimeout(refresh, s * 1000));
      }
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : "Error" });
    }
  }

  async function handleExport() {
    setExporting(true);
    try {
      const res = await downloadSeoCsv(query);
      setFeedback(
        res.truncated
          ? {
              ok: false,
              text: `El CSV trae solo las primeras ${nfmt(res.maxRows)} filas de ${nfmt(res.total)}. Filtrá más (regla, idioma, canal) y exportá de nuevo.`,
            }
          : null,
      );
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : "No se pudo exportar." });
    } finally {
      setExporting(false);
    }
  }

  const languageOptions = [
    { value: null as string | null, label: "Todos" },
    ...(data?.languages ?? []).map((l) => ({ value: l as string | null, label: l === "-" ? "sin idioma" : l })),
  ];

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
          <IconFileText className="w-5 h-5 text-muted-foreground" />
          Textos del catálogo (SEO)
          <Badge variant="success">solo lectura</Badge>
        </h2>
        <div className="flex items-center gap-2">
          <button
            onClick={refresh}
            className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
          >
            <IconRefresh className="w-3.5 h-3.5" />
            Actualizar
          </button>
          <Button
            size="sm"
            variant="secondary"
            onClick={handleExport}
            disabled={exporting || !run}
            title="CSV con «;» como separador (se abre directo en Excel en español) y los mismos filtros"
          >
            {exporting ? "Exportando…" : "Exportar CSV"}
          </Button>
          <Button size="sm" onClick={handleRun} disabled={running}>
            {running ? "Corriendo…" : "Auditar ahora"}
          </Button>
        </div>
      </div>

      <p className="text-sm text-muted-foreground">{SECTION_META.seo_textos?.desc}</p>

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

      <RunLine run={run} cron={summaryQ.data?.cron_utc ?? null} />

      <div className="flex items-center gap-2 flex-wrap">
        <FilterChip
          active={rule === null}
          onClick={() => {
            setRule(null);
            resetView();
          }}
        >
          Todas las reglas <span className="num-tabular">{nfmt(data?.facet_products ?? 0)}</span>
        </FilterChip>
        {rules.map((r) => (
          <FilterChip
            key={r.id}
            active={rule === r.id}
            onClick={() => {
              setRule(rule === r.id ? null : r.id);
              resetView();
            }}
            className={cn(
              "border-border",
              (counts[r.id] ?? 0) === 0 && rule !== r.id ? "text-muted-foreground" : "text-foreground",
            )}
          >
            <span title={`${r.id}: ${r.help}`}>{r.label}</span>{" "}
            <span className="num-tabular">{nfmt(counts[r.id] ?? 0)}</span>
          </FilterChip>
        ))}
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
          label="Canal"
          value={channel}
          options={CHANNEL_OPTIONS}
          onPick={(v) => {
            setChannel(v);
            resetView();
          }}
        />
        <FilterGroup
          label="Idioma"
          value={lang}
          options={languageOptions}
          onPick={(v) => {
            setLang(v);
            resetView();
          }}
        />
        <label className="inline-flex items-center gap-1.5 text-xs text-foreground cursor-pointer">
          <input
            type="checkbox"
            checked={onlyIssues}
            onChange={(e) => {
              setOnlyIssues(e.target.checked);
              resetView();
            }}
          />
          Solo con problemas
        </label>
        <div className="relative ml-auto w-full sm:w-64">
          <IconSearch className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Buscar por nombre, URL o código"
            className="pl-9 h-9"
          />
        </div>
      </div>

      {itemsQ.error ? (
        <Card className="bg-destructive/10 border-destructive/40 p-6 text-destructive text-sm">
          Error: {itemsQ.error instanceof Error ? itemsQ.error.message : "Error"}
        </Card>
      ) : itemsQ.isPending ? (
        <Card className="p-8 text-center text-muted-foreground text-sm">Cargando…</Card>
      ) : data?.run_id == null ? (
        <Card className="p-8 text-center text-muted-foreground text-sm">
          Todavía no corrió ninguna vez.{" "}
          {summaryQ.data?.cron_utc
            ? `Corre sola (cron UTC «${summaryQ.data.cron_utc}») o con «Auditar ahora».`
            : "Corré «Auditar ahora»."}
        </Card>
      ) : items.length === 0 ? (
        <Card className="p-8 text-center text-muted-foreground text-sm">No hay productos con ese filtro.</Card>
      ) : (
        <Card className="overflow-x-auto shadow-sm">
          <table className="w-full text-sm">
            <thead className="text-xs text-muted-foreground uppercase tracking-wide border-b border-border">
              <tr>
                <th className="text-left font-medium px-3 py-2">Producto</th>
                <th className="text-left font-medium px-3 py-2">Estado</th>
                <th className="text-left font-medium px-3 py-2">Idioma</th>
                <th className="text-right font-medium px-3 py-2">Largo</th>
                <th className="text-left font-medium px-3 py-2">Problemas</th>
              </tr>
            </thead>
            <tbody>
              {items.map((i) => (
                <ItemRow key={`${i.product_id}|${i.language}`} item={i} rules={rules} titleMax={summaryQ.data?.limits.title_max ?? 60} />
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {total > 0 && (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span className="num-tabular">
            {nfmt(total)} filas · página {page + 1} de {totalPages}
          </span>
          <div className="flex gap-2">
            <Button variant="secondary" size="sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
              Anterior
            </Button>
            <Button variant="secondary" size="sm" disabled={page + 1 >= totalPages} onClick={() => setPage(page + 1)}>
              Siguiente
            </Button>
          </div>
        </div>
      )}

      <ListsPanel />
    </section>
  );
}

function ItemRow({ item, rules, titleMax }: { item: SeoItem; rules: SeoRule[]; titleMax: number }) {
  const byId = new Map(rules.map((r) => [r.id, r]));
  return (
    <tr className="border-b border-border last:border-0 align-top">
      <td className="px-3 py-2 min-w-[16rem] max-w-md">
        <div className="font-medium text-foreground break-words">
          {item.name.trim() ? item.name : <span className="text-muted-foreground">(sin nombre)</span>}
        </div>
        <div className="text-xs text-muted-foreground break-all">
          #{item.product_id}
          {item.product_code ? ` · ${item.product_code}` : ""}
          {item.slug ? ` · /${item.slug}` : ""}
        </div>
      </td>
      <td className="px-3 py-2 whitespace-nowrap">
        <Badge variant={item.enabled ? "success" : "secondary"}>{item.enabled ? "habilitado" : "deshabilitado"}</Badge>
        <div className="text-xs text-muted-foreground mt-1">{channelText(item)}</div>
      </td>
      <td className="px-3 py-2 whitespace-nowrap text-xs">{item.language || "—"}</td>
      <td
        className={cn(
          "px-3 py-2 text-right num-tabular whitespace-nowrap",
          item.name_len > titleMax && "text-warning font-medium",
        )}
      >
        {item.name_len}
      </td>
      <td className="px-3 py-2 min-w-[18rem]">
        {item.issues.length === 0 ? (
          <span className="text-xs text-muted-foreground">sin problemas</span>
        ) : (
          <ul className="space-y-1">
            {item.issues.map((is) => {
              const meta = byId.get(is.rule);
              return (
                <li key={is.rule} className="flex items-start gap-1.5 text-xs">
                  <Badge variant={GROUP_VARIANT[meta?.group ?? ""] ?? "default"} title={meta?.help} className="shrink-0 mt-0.5">
                    {is.rule}
                  </Badge>
                  <span className="text-muted-foreground break-words">{is.detail}</span>
                </li>
              );
            })}
          </ul>
        )}
      </td>
    </tr>
  );
}

function RunLine({ run, cron }: { run: SeoRun | null; cron: string | null }) {
  if (!run) return null;
  const when = run.status === "running" ? `arrancó ${fmtTime(run.started_at)}` : `terminó ${fmtTime(run.finished_at)}`;
  const failed = Object.entries(run.channels_failed);
  return (
    <div className="text-xs text-muted-foreground space-y-0.5">
      <p>
        Última corrida #{run.id} · {RUN_STATUS_LABEL[run.status]} · {when}
        {run.duration_s != null && ` · ${run.duration_s} s`} ·{" "}
        <span className="num-tabular">
          {nfmt(run.products_total)} productos ({nfmt(run.products_enabled)} habilitados) ·{" "}
          {nfmt(run.products_with_issues)} con problemas
        </span>
        {run.channels_ok.length > 0 && ` · canales leídos: ${run.channels_ok.join(", ")}`}
        {cron && ` · automática: cron UTC «${cron}»`}
      </p>
      {failed.length > 0 && (
        <p className="p-2 rounded-md border bg-warning/10 border-warning/40 text-warning">
          No se pudo leer el canal {failed.map(([c, why]) => `${c} (${why})`).join("; ")}. Los demás se auditaron igual.
        </p>
      )}
      {run.notes && <p>{run.notes}</p>}
      {run.status === "failed" && run.error && (
        <p className="p-2 rounded-md border bg-destructive/10 border-destructive/40 text-destructive">{run.error}</p>
      )}
    </div>
  );
}

function ListsPanel() {
  const qc = useQueryClient();
  const listsQ = useQuery({ queryKey: ["seo-lists"], queryFn: getSeoLists });
  const data = listsQ.data;
  return (
    <details className="group">
      <summary className="cursor-pointer text-sm font-medium text-foreground select-none">
        Listas editables (marcas, relleno, datos técnicos)
      </summary>
      <p className="text-xs text-muted-foreground mt-2 mb-3">
        Lo que guardes pisa la lista de fábrica entera y vale desde la próxima corrida; «Restablecer» vuelve a la de
        fábrica.
      </p>
      {listsQ.isPending ? (
        <p className="text-xs text-muted-foreground">Cargando…</p>
      ) : !data ? (
        <p className="text-xs text-destructive">No se pudieron leer las listas.</p>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {LIST_META.map((m) => (
            <ListEditor
              key={m.name}
              name={m.name}
              label={m.label}
              help={m.help}
              items={data.lists[m.name].items}
              modified={data.lists[m.name].modified}
              maxItems={data.max_items}
              onChanged={() => qc.invalidateQueries({ queryKey: ["seo-lists"] })}
            />
          ))}
        </div>
      )}
    </details>
  );
}

function ListEditor({
  name,
  label,
  help,
  items,
  modified,
  maxItems,
  onChanged,
}: {
  name: SeoListName;
  label: string;
  help: string;
  items: string[];
  modified: boolean;
  maxItems: number;
  onChanged: () => void;
}) {
  const saved = items.join("\n");
  const [text, setText] = useState(saved);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => setText(saved), [saved]);
  const dirty = text !== saved;
  const count = text.split("\n").filter((l) => l.trim()).length;

  async function save() {
    const empty = count === 0;
    if (
      empty &&
      !window.confirm(`Dejar «${label}» vacía apaga esa regla: no va a marcar nada. ¿Seguro?`)
    )
      return;
    setBusy(true);
    try {
      await saveSeoList(name, text.split("\n"), empty);
      setMsg({ ok: true, text: "Guardada. Vale desde la próxima corrida." });
      onChanged();
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "No se pudo guardar." });
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    if (!window.confirm(`¿Volver «${label}» a la lista de fábrica? Se pierden tus cambios.`)) return;
    setBusy(true);
    try {
      await resetSeoList(name);
      setMsg({ ok: true, text: "Restablecida." });
      onChanged();
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "No se pudo restablecer." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-medium text-foreground">{label}</h3>
        {modified && <Badge variant="warning">modificada</Badge>}
      </div>
      <p className="text-xs text-muted-foreground">{help}</p>
      <textarea
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setMsg(null);
        }}
        rows={10}
        spellCheck={false}
        className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring resize-y font-mono"
      />
      <div className="flex items-center gap-2 flex-wrap">
        <Button size="sm" onClick={save} disabled={busy || !dirty}>
          {busy ? "…" : "Guardar"}
        </Button>
        <Button size="sm" variant="ghost" onClick={reset} disabled={busy || (!modified && !dirty)}>
          Restablecer
        </Button>
        <span className={cn("text-xs ml-auto num-tabular", count > maxItems ? "text-destructive" : "text-muted-foreground")}>
          {count} / {maxItems}
        </span>
      </div>
      {msg && <p className={cn("text-xs", msg.ok ? "text-success" : "text-destructive")}>{msg.text}</p>}
    </Card>
  );
}
