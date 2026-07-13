import { IconBox, IconCopySquare, IconGrid, IconWarnFill } from "../icons";
import { Card } from "@/components/ui/card";
import { fmtTime, nfmt } from "../lib/format";
import type { StatusResponse } from "../types";

// Fila de 4 tarjetas de métricas + "última auditoría".
export default function MetricsRow({ status }: { status: StatusResponse | null }) {
  const m = status?.metrics;
  const val = (n: number | undefined) => (n == null ? "—" : nfmt(n));

  return (
    <section>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-[0.08em]">
          Resumen
        </h2>
        <p className="text-xs text-muted-foreground">
          Última auditoría:{" "}
          <span className="font-medium text-foreground">
            {status?.last_audit ? fmtTime(status.last_audit) : "nunca"}
          </span>
        </p>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Card className="p-4 shadow-sm">
          <div className="flex items-center gap-2 text-muted-foreground">
            <IconBox className="w-4 h-4" />
            <p className="text-[11px] uppercase tracking-wide">Productos vigilados</p>
          </div>
          <p className="text-2xl font-bold text-foreground mt-1.5 num-tabular">
            {val(m?.products_tracked)}
          </p>
        </Card>
        <Card className="p-4 shadow-sm">
          <div className="flex items-center gap-2 text-muted-foreground">
            <IconGrid className="w-4 h-4" />
            <p className="text-[11px] uppercase tracking-wide">Snapshots</p>
          </div>
          <p className="text-2xl font-bold text-foreground mt-1.5 num-tabular">
            {val(m?.snapshots_total)}
          </p>
        </Card>
        <Card className="p-4 shadow-sm border-warning/30">
          <div className="flex items-center gap-2 text-warning">
            <IconWarnFill className="w-4 h-4" />
            <p className="text-[11px] uppercase tracking-wide">Alertas (24h)</p>
          </div>
          <p className="text-2xl font-bold text-warning mt-1.5 num-tabular">
            {val(m?.alerts_last_24h)}
          </p>
        </Card>
        <Card className="p-4 shadow-sm border-destructive/30">
          <div className="flex items-center gap-2 text-destructive">
            <IconCopySquare className="w-4 h-4" />
            <p className="text-[11px] uppercase tracking-wide">Duplicados (7d)</p>
          </div>
          <p className="text-2xl font-bold text-destructive mt-1.5 num-tabular">
            {val(m?.duplicates_last_7d)}
          </p>
        </Card>
      </div>
    </section>
  );
}
