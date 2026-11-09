import { useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  applyGateNow,
  getGateApplyStatus,
  getMeasures,
  getMeasuresSummary,
  runMeasuresCheck,
  type MeasuresVerdict,
} from "../api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const VERDICT_LABEL: Record<MeasuresVerdict, string> = {
  mal: "No tienen sentido",
  dudoso: "Dudosas",
  sin_medidas: "Sin medidas",
  error: "Sin chequear",
  ok: "OK",
};
const VERDICT_VARIANT: Record<MeasuresVerdict, "destructive" | "warning" | "secondary" | "success"> = {
  mal: "destructive",
  dudoso: "warning",
  sin_medidas: "secondary",
  error: "secondary",
  ok: "success",
};
const ORDER: MeasuresVerdict[] = ["mal", "dudoso", "sin_medidas", "error", "ok"];

function specsText(s: Record<string, number>, prefix: string): string {
  const dims = ["length", "width", "height"].map((k) => s[`${prefix}${k}`]).filter((v) => v);
  const kg = s[`${prefix}weight`];
  if (!dims.length && !kg) return "—";
  return `${dims.join(" × ") || "?"} cm${kg ? `, ${kg} kg` : ""}`;
}

async function detailOf(r: Response, fallback: string): Promise<string> {
  const body = (await r.json().catch(() => ({}))) as { detail?: unknown };
  return typeof body.detail === "string" ? body.detail : fallback;
}

/** Portón «Aplicar ahora» y revisión de medidas de los deshabilitados con la IA barata. */
export function MeasuresPanel() {
  const qc = useQueryClient();
  const [verdict, setVerdict] = useState<MeasuresVerdict | null>("mal");
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);
  const summaryQ = useQuery({ queryKey: ["measures-summary"], queryFn: getMeasuresSummary, refetchInterval: 30_000 });
  const listQ = useQuery({
    queryKey: ["measures", verdict],
    queryFn: () => getMeasures(verdict),
    placeholderData: keepPreviousData,
  });
  const gateQ = useQuery({ queryKey: ["gate-apply"], queryFn: getGateApplyStatus, refetchInterval: 15_000 });

  async function handleApply() {
    if (
      !window.confirm(
        "Con la última corrida: se ACTIVAN en Vendure los productos apagados en verde firme (si sus medidas tienen " +
          "sentido) y se APAGAN los rojos firmes publicados, sin esperar las 2 noches. Respeta los topes del día y todo " +
          "tiene Deshacer en la Bandeja. ¿Seguir?",
      )
    )
      return;
    try {
      const r = await applyGateNow();
      if (!r.ok) setFeedback({ ok: false, text: await detailOf(r, `No se pudo (${r.status}).`) });
      else {
        const body = (await r.json()) as { activations_daily_max: number; inactivations_daily_max: number };
        setFeedback({
          ok: true,
          text:
            `Portón disparado. Topes de hoy: ${body.activations_daily_max} activaciones y ` +
            `${body.inactivations_daily_max} apagados (se suben en Configuración).`,
        });
        [10, 60].forEach((s) => setTimeout(() => qc.invalidateQueries({ queryKey: ["gate-apply"] }), s * 1000));
      }
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : "Error" });
    }
  }

  async function handleCheck() {
    try {
      const r = await runMeasuresCheck();
      if (!r.ok) setFeedback({ ok: false, text: await detailOf(r, `No se pudo (${r.status}).`) });
      else {
        setFeedback({ ok: true, text: "Revisando las medidas de los deshabilitados. No toca Vendure." });
        [10, 60].forEach((s) =>
          setTimeout(() => {
            qc.invalidateQueries({ queryKey: ["measures-summary"] });
            qc.invalidateQueries({ queryKey: ["measures"] });
          }, s * 1000),
        );
      }
    } catch (err) {
      setFeedback({ ok: false, text: err instanceof Error ? err.message : "Error" });
    }
  }

  const sum = summaryQ.data;
  const last = gateQ.data?.last;
  return (
    <Card className="p-4 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-foreground">Portón y medidas de los deshabilitados</h3>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={handleCheck} disabled={sum?.running}>
            {sum?.running ? "Revisando…" : "Revisar medidas con IA"}
          </Button>
          <Button size="sm" onClick={handleApply} disabled={gateQ.data?.running}>
            {gateQ.data?.running ? "Aplicando…" : "Aplicar ahora: subir verdes / bajar rojos"}
          </Button>
        </div>
      </div>
      {feedback && (
        <p
          className={cn(
            "p-2 rounded-md text-xs border",
            feedback.ok
              ? "bg-success/10 border-success/40 text-success"
              : "bg-destructive/10 border-destructive/40 text-destructive",
          )}
        >
          {feedback.text}
        </p>
      )}
      {last && (
        <p className="text-xs text-muted-foreground">
          Último «Aplicar ahora»: corrida #{last.run_id}, {last.by},{" "}
          {Object.entries(last.counts)
            .map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`)
            .join(" · ") || "sin cambios"}
        </p>
      )}
      {sum && (
        <div className="flex flex-wrap gap-2 text-xs">
          {ORDER.map((v) => (
            <button
              key={v}
              onClick={() => setVerdict(verdict === v ? null : v)}
              className={cn(
                "px-2 py-1 rounded-md border transition-colors",
                verdict === v ? "border-foreground text-foreground" : "border-border text-muted-foreground",
              )}
            >
              {VERDICT_LABEL[v]}: {sum.by_verdict[v] ?? 0}
            </button>
          ))}
          <span className="text-muted-foreground self-center">
            IA {sum.ia_enabled ? "prendida" : "apagada (PM_LLM_*)"} · hoy {sum.used_today}/{sum.daily_max} · gastado
            USD {sum.cost_usd.toFixed(3)}
            {sum.guard_enabled ? "" : " · la guarda del portón está apagada"}
          </span>
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-muted-foreground text-left">
            <tr>
              <th className="py-1 pr-2">Producto</th>
              <th className="py-1 pr-2">Veredicto</th>
              <th className="py-1 pr-2">Producto</th>
              <th className="py-1 pr-2">Caja individual</th>
              <th className="py-1 pr-2">Caja master</th>
              <th className="py-1">Por qué</th>
            </tr>
          </thead>
          <tbody>
            {(listQ.data?.items ?? []).map((it) => (
              <tr key={it.product_id} className="border-t border-border align-top">
                <td className="py-1 pr-2">
                  <div className="text-foreground">{it.product_name || it.product_id}</div>
                  <div className="text-muted-foreground">{it.product_code}</div>
                </td>
                <td className="py-1 pr-2">
                  <Badge variant={VERDICT_VARIANT[it.verdict]}>{VERDICT_LABEL[it.verdict]}</Badge>
                </td>
                <td className="py-1 pr-2 whitespace-nowrap">{specsText(it.specs, "")}</td>
                <td className="py-1 pr-2 whitespace-nowrap">{specsText(it.specs, "box_")}</td>
                <td className="py-1 pr-2 whitespace-nowrap">
                  {specsText(it.specs, "master_")}
                  {it.specs.master_units ? ` · ${it.specs.master_units} u.` : ""}
                </td>
                <td className="py-1 text-muted-foreground">{it.motive || it.problems.join("; ") || "—"}</td>
              </tr>
            ))}
            {listQ.data && listQ.data.items.length === 0 && (
              <tr>
                <td colSpan={6} className="py-2 text-muted-foreground">
                  Nada en este filtro.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
