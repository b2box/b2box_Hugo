import { useQuery } from "@tanstack/react-query";
import { getHealthMetrics } from "../api";
import { Card } from "@/components/ui/card";
import { IconActivity } from "../icons";
import { fmtPct, fmtTime, nfmt } from "../lib/format";
import { COLOR_META, RUN_STATUS_LABEL } from "./SemaforoView";
import type { PriceMonitorSummary, SemaforoColor } from "../types";

// Página de salud del sistema: budget OTAPI, tasa Paco, últimas auditorías, cache.
export default function HealthView() {
  const q = useQuery({
    queryKey: ["health-metrics"],
    queryFn: getHealthMetrics,
    refetchInterval: 30_000,
  });
  const m = q.data;

  return (
    <section className="space-y-4">
      <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
        <IconActivity className="w-5 h-5 text-primary" />
        Salud del sistema
      </h2>

      {q.error ? (
        <Card className="bg-destructive/10 border-destructive/40 p-6 text-destructive text-sm">
          Error: {q.error instanceof Error ? q.error.message : "Error"}
        </Card>
      ) : !m ? (
        <Card className="p-8 text-center text-muted-foreground text-sm">Cargando…</Card>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {/* OTAPI budget */}
          <Card className="p-5 shadow-sm">
            <h3 className="text-sm font-semibold text-foreground uppercase tracking-wide mb-3">
              Budget OTAPI (hoy)
            </h3>
            <div className="flex items-end gap-2">
              <span className="text-3xl font-bold text-foreground num-tabular">
                {nfmt(m.otapi_budget.used)}
              </span>
              <span className="text-sm text-muted-foreground mb-1">/ {nfmt(m.otapi_budget.budget)} calls</span>
            </div>
            <div className="mt-3 h-2 rounded-full bg-muted overflow-hidden">
              <div
                className="h-full bg-primary transition-all"
                style={{
                  width: `${Math.min(100, m.otapi_budget.budget ? (m.otapi_budget.used / m.otapi_budget.budget) * 100 : 0)}%`,
                }}
              />
            </div>
            <p className="text-xs text-muted-foreground mt-2">
              Quedan {nfmt(m.otapi_budget.remaining)} calls hoy.
            </p>
          </Card>

          {/* Paco success rate */}
          <Card className="p-5 shadow-sm">
            <h3 className="text-sm font-semibold text-foreground uppercase tracking-wide mb-3">
              Éxito con Paco
            </h3>
            <div className="flex items-end gap-2">
              <span className="text-3xl font-bold text-success num-tabular">
                {m.paco.success_rate == null ? "—" : `${(m.paco.success_rate * 100).toFixed(0)}%`}
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-2">
              {nfmt(m.paco.passed)} enviados OK · {nfmt(m.paco.failed)} fallidos
            </p>
          </Card>

          {/* Duplicados */}
          <Card className="p-5 shadow-sm">
            <h3 className="text-sm font-semibold text-foreground uppercase tracking-wide mb-3">
              Duplicados
            </h3>
            <div className="flex items-center gap-6">
              <div>
                <p className="text-2xl font-bold text-warning num-tabular">
                  {nfmt(m.duplicates.pending_flagged)}
                </p>
                <p className="text-xs text-muted-foreground">pendientes</p>
              </div>
              <div>
                <p className="text-2xl font-bold text-foreground num-tabular">
                  {nfmt(m.duplicates.disabled_total)}
                </p>
                <p className="text-xs text-muted-foreground">deshabilitados</p>
              </div>
            </div>
          </Card>

          {/* Cache de imágenes */}
          <Card className="p-5 shadow-sm">
            <h3 className="text-sm font-semibold text-foreground uppercase tracking-wide mb-3">
              Cache de imágenes (pHash)
            </h3>
            <div className="flex items-center gap-6">
              <div>
                <p className="text-2xl font-bold text-foreground num-tabular">
                  {nfmt(m.image_hash_cache.persisted)}
                </p>
                <p className="text-xs text-muted-foreground">en DB</p>
              </div>
              <div>
                <p className="text-2xl font-bold text-foreground num-tabular">
                  {nfmt(m.image_hash_cache.in_memory)}
                </p>
                <p className="text-xs text-muted-foreground">en memoria</p>
              </div>
            </div>
          </Card>

          {/* Semáforo de precios contra ML */}
          {m.price_monitor && <PriceMonitorCard pm={m.price_monitor} />}

          {/* Pendientes + últimas auditorías */}
          <Card className="p-5 shadow-sm sm:col-span-2">
            <h3 className="text-sm font-semibold text-foreground uppercase tracking-wide mb-3">
              Pendientes y últimas corridas
            </h3>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
              <Stat label="Calidad pendiente" value={nfmt(m.quality_pending)} />
              <Stat label="Errores pendientes" value={nfmt(m.errors_pending)} />
              <Stat
                label="Último precio"
                value={m.last_price_snapshot ? fmtTime(m.last_price_snapshot) : "—"}
              />
              <Stat
                label="Último dedup"
                value={m.last_dedup_marker ? fmtTime(m.last_dedup_marker) : "nunca"}
              />
            </div>
          </Card>
        </div>
      )}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-lg font-bold text-foreground num-tabular">{value}</p>
      <p className="text-xs text-muted-foreground">{label}</p>
    </div>
  );
}

const RUN_STATUS_CLASS: Record<string, string> = {
  ok: "text-success",
  running: "text-primary",
  degraded: "text-warning",
  failed: "text-destructive",
  skipped: "text-warning",
};

// Card de Salud del semáforo: si la corrida de anoche anduvo, cuánto de ML
// usó y cuánto quedó sin dato o falló. En sombra, lo que importa medir.
function PriceMonitorCard({ pm }: { pm: PriceMonitorSummary }) {
  const run = pm.last_run;
  return (
    <Card className="p-5 shadow-sm sm:col-span-2">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-foreground uppercase tracking-wide">
          Semáforo de precios (ML){pm.mode === 0 ? " · sombra" : ""}
        </h3>
        <span className="text-xs text-muted-foreground">cron {pm.cron_utc} UTC</span>
      </div>
      {!run ? (
        <p className="text-sm text-muted-foreground">Todavía no corrió ninguna vez.</p>
      ) : (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
            <div>
              <p className={`text-lg font-bold ${RUN_STATUS_CLASS[run.status] ?? "text-foreground"}`}>
                {RUN_STATUS_LABEL[run.status] ?? run.status}
              </p>
              <p className="text-xs text-muted-foreground">
                #{run.id} · {fmtTime(run.finished_at ?? run.started_at)}
              </p>
            </div>
            <Stat
              label={`requests ML (hoy ${nfmt(pm.ml_budget.used)}/${nfmt(pm.ml_budget.budget)})`}
              value={nfmt(run.ml_requests_used)}
            />
            <Stat label="sin dato" value={fmtPct(run.pct_no_data)} />
            <Stat label="ML falló" value={fmtPct(run.pct_failed)} />
            <Stat
              label={`productos (${nfmt(run.counts.skipped)} no evaluados)`}
              value={`${nfmt(run.processed)}/${nfmt(run.total_products)}`}
            />
            <Stat
              label={pm.judge_enabled ? "llamadas juez IA" : "juez IA apagado"}
              value={nfmt(run.llm.calls)}
            />
            <Stat label="costo IA (USD)" value={run.llm.cost_usd.toFixed(4)} />
            <div>
              <div className="flex items-center gap-2 flex-wrap text-xs">
                {(Object.keys(COLOR_META) as SemaforoColor[]).map((c) => (
                  <span key={c} className="inline-flex items-center gap-1 num-tabular">
                    <span className={`w-2 h-2 rounded-full ${COLOR_META[c].dot}`} />
                    {nfmt(run.colors[c] ?? 0)}
                  </span>
                ))}
              </div>
              <p className="text-xs text-muted-foreground mt-1">por color</p>
            </div>
          </div>
          {run.error && <p className="text-xs text-destructive mt-3">Error: {run.error}</p>}
        </>
      )}
    </Card>
  );
}
