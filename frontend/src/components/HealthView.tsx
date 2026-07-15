import { useQuery } from "@tanstack/react-query";
import { getHealthMetrics } from "../api";
import { Card } from "@/components/ui/card";
import { IconActivity } from "../icons";
import { fmtTime, nfmt } from "../lib/format";

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
