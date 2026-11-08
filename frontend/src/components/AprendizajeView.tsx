import { useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  adjustmentAction,
  getAdjustments,
  getCorrections,
  runAdjustments,
  setCorrectionMotive,
  type CorrectionMotive,
  type SemaforoAdjustment,
  type SemaforoCorrection,
} from "../api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { fmtArs, fmtPct, fmtTime, nfmt } from "../lib/format";
import { safeHttpsUrl } from "./SourceCells";

// La persona manda y el semáforo aprende. Arriba, los ajustes por segmento que Hugo propone (o aplicó) a partir de las
// correcciones, con su historial y Deshacer. Abajo, las correcciones: alguien prendió un producto que Hugo veía rojo o
// apagó uno que veía verde; Hugo no lo revierte y no lo toca solo por un tiempo. Todo lo que viene de ML y lo que
// escriben las personas se muestra como texto.

const STATUS_VARIANT: Record<string, BadgeProps["variant"]> = {
  propuesto: "warning",
  aplicado: "success",
  rechazado: "outline",
  deshecho: "outline",
  reemplazado: "outline",
};

function errText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Error";
}

function fmtValue(param: string, v: number): string {
  if (param === "yellow_min" || param === "green_min") return fmtPct(v);
  if (param === "max_price_ratio") return `${v.toLocaleString("es-AR")}×`;
  return nfmt(v);
}

export default function AprendizajeView() {
  const qc = useQueryClient();
  const [page, setPage] = useState(0);
  const adjQ = useQuery({ queryKey: ["sem-adjustments"], queryFn: getAdjustments, refetchInterval: 60_000 });
  const corrQ = useQuery({
    queryKey: ["sem-corrections", page],
    queryFn: () => getCorrections(page),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  });
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  function refresh() {
    qc.invalidateQueries({ queryKey: ["sem-adjustments"] });
    qc.invalidateQueries({ queryKey: ["sem-corrections"] });
  }

  async function run() {
    setBusy(true);
    setNotice(null);
    try {
      const r = await runAdjustments();
      setNotice({
        ok: true,
        text:
          r.modo === 0
            ? "El aprendizaje está apagado (pm_learn_mode = 0)."
            : `Listo: ${nfmt(r.propuestos ?? 0)} ajustes nuevos, ${nfmt(r.aplicados ?? 0)} aplicados solos.`,
      });
      refresh();
    } catch (e) {
      setNotice({ ok: false, text: errText(e) });
    } finally {
      setBusy(false);
    }
  }

  const adj = adjQ.data;
  const corr = corrQ.data;
  const pending = (adj?.items ?? []).filter((a) => a.status === "propuesto");
  const history = (adj?.items ?? []).filter((a) => a.status !== "propuesto");

  return (
    <div className="space-y-4">
      <Card className="p-4 space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <h3 className="font-semibold text-foreground flex items-center gap-2">
            Ajustes del semáforo por segmento
            {adj && <Badge variant={adj.mode === 0 ? "outline" : "warning"}>aprendizaje: {adj.mode_label}</Badge>}
          </h3>
          {adj?.can_decide && (
            <Button variant="secondary" size="sm" disabled={busy} onClick={run}>
              Buscar ajustes ahora
            </Button>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          Una vez por semana Hugo mira las correcciones de los últimos {adj ? nfmt(adj.window_days) : "…"} días y busca si
          se concentran en un tipo de caso (al menos {adj ? nfmt(adj.min_corrections) : "…"} y la mitad o más). Un ajuste
          solo cambia lo que Hugo hace solo: lo que cae en el segmento y no pasa el corte ajustado va a la bandeja. Cada
          corte se mueve como mucho {adj ? fmtPct(adj.max_shift_pts) : "…"} puntos.
        </p>
        {notice && (
          <p className={notice.ok ? "text-xs text-success" : "text-xs text-destructive"}>{notice.text}</p>
        )}
        {adjQ.error ? (
          <p className="text-sm text-destructive">Error: {errText(adjQ.error)}</p>
        ) : pending.length === 0 ? (
          <p className="text-sm text-muted-foreground">No hay ajustes propuestos.</p>
        ) : (
          <div className="space-y-2">
            {pending.map((a) => (
              <AdjustmentRow key={a.id} a={a} canDecide={!!adj?.can_decide} onChanged={refresh} />
            ))}
          </div>
        )}
        {history.length > 0 && (
          <details className="text-sm">
            <summary className="cursor-pointer text-xs text-muted-foreground">
              Historial ({nfmt(history.length)})
            </summary>
            <div className="space-y-2 mt-2">
              {history.map((a) => (
                <AdjustmentRow key={a.id} a={a} canDecide={!!adj?.can_decide} onChanged={refresh} />
              ))}
            </div>
          </details>
        )}
      </Card>

      <Card className="p-4 space-y-3">
        <h3 className="font-semibold text-foreground">Correcciones de las personas</h3>
        <p className="text-xs text-muted-foreground">
          Alguien prendió un producto que Hugo veía rojo o apagó uno que veía verde. Hugo no lo revierte y no lo toca
          solo por {corr ? nfmt(corr.hold_days) : "…"} días, salvo que cambie su precio. Contar por qué ayuda a que
          aprenda; «los de Mercado Libre no eran el mismo producto» además saca esas publicaciones de la próxima medición.
        </p>
        {corrQ.error ? (
          <p className="text-sm text-destructive">Error: {errText(corrQ.error)}</p>
        ) : corrQ.isPending ? (
          <p className="text-sm text-muted-foreground">Cargando…</p>
        ) : (corr?.items.length ?? 0) === 0 ? (
          <p className="text-sm text-muted-foreground">Todavía no hay correcciones.</p>
        ) : (
          <div className="space-y-2">
            {corr!.items.map((c) => (
              <CorrectionRow
                key={c.id}
                c={c}
                motives={corr!.motives}
                canDecide={!!adj?.can_decide}
                onChanged={refresh}
              />
            ))}
          </div>
        )}
        {corr && corr.total > corr.page_size && (
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span className="num-tabular">
              {nfmt(corr.total)} correcciones · página {page + 1}
            </span>
            <div className="flex gap-2">
              <Button variant="secondary" size="sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
                Anterior
              </Button>
              <Button variant="secondary" size="sm" disabled={!corr.has_more} onClick={() => setPage(page + 1)}>
                Siguiente
              </Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

function AdjustmentRow({ a, canDecide, onChanged }: { a: SemaforoAdjustment; canDecide: boolean; onChanged: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function act(action: "approve" | "reject" | "undo") {
    setBusy(true);
    setError(null);
    try {
      await adjustmentAction(a.id, action);
      onChanged();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border border-border rounded-md p-3 space-y-1">
      <div className="flex items-center gap-2 flex-wrap text-sm">
        <Badge variant={STATUS_VARIANT[a.status] ?? "outline"}>{a.status_label}</Badge>
        <span className="font-medium text-foreground">{a.segment_label}</span>
        <span className="text-muted-foreground">· {a.direction_label}</span>
        <span className="num-tabular ml-auto text-foreground">
          {a.param_label}: {fmtValue(a.param, a.old_value)} → {fmtValue(a.param, a.new_value)}
        </span>
      </div>
      <p className="text-xs text-muted-foreground">{a.reason}</p>
      <p className="text-[11px] text-muted-foreground">
        Propuesto {fmtTime(a.created_at)} con {nfmt(a.n_corrections)} correcciones
        {a.decided_by && ` · ${a.status === "rechazado" ? "rechazó" : "aplicó"} ${a.decided_by} ${fmtTime(a.decided_at)}`}
        {a.undone_by && ` · deshizo ${a.undone_by} ${fmtTime(a.undone_at)}`}
        {a.replaces_id && ` · reemplaza al ajuste #${a.replaces_id}`}
      </p>
      {canDecide && (a.status === "propuesto" || a.status === "aplicado") && (
        <div className="flex gap-2 pt-1">
          {a.status === "propuesto" && (
            <>
              <Button size="sm" disabled={busy} onClick={() => act("approve")}>
                Aplicar
              </Button>
              <Button size="sm" variant="secondary" disabled={busy} onClick={() => act("reject")}>
                Rechazar
              </Button>
            </>
          )}
          {a.status === "aplicado" && (
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => act("undo")}>
              Deshacer
            </Button>
          )}
        </div>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

function CorrectionRow({
  c,
  motives,
  canDecide,
  onChanged,
}: {
  c: SemaforoCorrection;
  motives: { code: CorrectionMotive; label: string }[];
  canDecide: boolean;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [motive, setMotive] = useState<CorrectionMotive | "">(c.motive ?? "");
  const [note, setNote] = useState(c.note ?? "");
  const [bad, setBad] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    if (!motive) return;
    setBusy(true);
    setError(null);
    try {
      await setCorrectionMotive(c.id, { motive, note: note.trim() || null, mlIds: motive === "match_malo" ? bad : [] });
      setOpen(false);
      onChanged();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border border-border rounded-md p-3 space-y-1 text-sm">
      <div className="flex items-center gap-2 flex-wrap">
        <Badge variant={c.hugo_color === "rojo" ? "destructive" : "success"}>
          Hugo: {c.hugo_color}
          {c.hugo_firm ? " firme" : ""}
        </Badge>
        <span className="text-foreground">{c.human_decision_label}</span>
        <span className="font-medium text-foreground truncate max-w-[22rem]">{c.product_name ?? c.product_id}</span>
        {c.holding && <Badge variant="warning">Hugo no lo toca hasta {fmtTime(c.hold_until)}</Badge>}
        <span className="ml-auto text-xs text-muted-foreground">
          {c.actor} · {c.origin === "bandeja" ? "desde la bandeja" : "en Vendure"} · {fmtTime(c.at)}
        </span>
      </div>
      <p className="text-xs text-muted-foreground num-tabular">
        Nuestro precio {fmtArs(c.our_price_cents)} · mediana ML {fmtArs(c.ml_median_cents)} · ganancia{" "}
        {fmtPct(c.margin_pct)} · {c.firm_listings ?? "—"} idénticos firmes
        {c.price_ratio != null && ` · ${c.price_ratio.toLocaleString("es-AR")}× la mediana`}
        {c.tier_used && ` · ${c.tier_used}`}
        {c.category && ` · ${c.category}`}
      </p>
      {c.motive_label && (
        <p className="text-xs text-foreground">
          Por qué: {c.motive_label}
          {c.note && ` — ${c.note}`}
          {c.bad_ml_ids.length > 0 && ` (no eran el mismo: ${c.bad_ml_ids.join(", ")})`}
        </p>
      )}
      {canDecide && !open && (
        <button className="text-xs text-muted-foreground hover:text-foreground underline" onClick={() => setOpen(true)}>
          {c.motive ? "Cambiar el motivo" : "Contar por qué"}
        </button>
      )}
      {open && (
        <div className="space-y-2 pt-1">
          <div className="flex flex-wrap gap-2">
            {motives.map((m) => (
              <label key={m.code} className="text-xs flex items-center gap-1">
                <input type="radio" checked={motive === m.code} onChange={() => setMotive(m.code)} />
                {m.label}
              </label>
            ))}
          </div>
          {motive === "match_malo" && (
            <div className="space-y-1">
              <p className="text-xs text-muted-foreground">Marcá las publicaciones que NO eran el mismo producto:</p>
              {c.listings.length === 0 && <p className="text-xs text-muted-foreground">La medición no tiene idénticos.</p>}
              {c.listings.map((l) => {
                const href = safeHttpsUrl(l.permalink);
                return (
                  <label key={l.ml_id} className="text-xs flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={bad.includes(l.ml_id)}
                      onChange={(e) =>
                        setBad(e.target.checked ? [...bad, l.ml_id] : bad.filter((x) => x !== l.ml_id))
                      }
                    />
                    <span className="num-tabular">{fmtArs(l.median_cents)}</span>
                    {href ? (
                      <a href={href} target="_blank" rel="noopener noreferrer" className="underline truncate max-w-[28rem]">
                        {l.title || l.ml_id}
                      </a>
                    ) : (
                      <span className="truncate max-w-[28rem]">{l.title || l.ml_id}</span>
                    )}
                  </label>
                );
              })}
            </div>
          )}
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={500}
            rows={2}
            placeholder={motive === "otro" ? "Contá el motivo" : "Nota (opcional)"}
            className="w-full rounded-md border border-border bg-transparent p-2 text-xs"
          />
          <div className="flex gap-2">
            <Button size="sm" disabled={busy || !motive} onClick={save}>
              Guardar
            </Button>
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => setOpen(false)}>
              Cancelar
            </Button>
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>
      )}
    </div>
  );
}
