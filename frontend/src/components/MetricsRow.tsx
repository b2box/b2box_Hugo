import { IconBox, IconCopySquare, IconGrid, IconWarnFill } from "../icons";
import { fmtTime, nfmt } from "../lib/format";
import type { StatusResponse } from "../types";

// Fila de 4 tarjetas de métricas + "última auditoría".
export default function MetricsRow({ status }: { status: StatusResponse | null }) {
  const m = status?.metrics;
  const val = (n: number | undefined) => (n == null ? "—" : nfmt(n));

  return (
    <section>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-xs font-semibold text-navy-500 uppercase tracking-[0.08em]">Resumen</h2>
        <p className="text-xs text-navy-500">
          Última auditoría:{" "}
          <span className="font-medium text-navy-700">
            {status?.last_audit ? fmtTime(status.last_audit) : "nunca"}
          </span>
        </p>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="bg-white rounded-xl border border-navy-200 p-4 shadow-card">
          <div className="flex items-center gap-2 text-navy-500">
            <IconBox className="w-4 h-4" />
            <p className="text-[11px] uppercase tracking-wide">Productos vigilados</p>
          </div>
          <p className="text-2xl font-bold text-navy-900 mt-1.5 num-tabular">
            {val(m?.products_tracked)}
          </p>
        </div>
        <div className="bg-white rounded-xl border border-navy-200 p-4 shadow-card">
          <div className="flex items-center gap-2 text-navy-500">
            <IconGrid className="w-4 h-4" />
            <p className="text-[11px] uppercase tracking-wide">Snapshots</p>
          </div>
          <p className="text-2xl font-bold text-navy-900 mt-1.5 num-tabular">
            {val(m?.snapshots_total)}
          </p>
        </div>
        <div className="bg-white rounded-xl border border-amber-200 p-4 shadow-card">
          <div className="flex items-center gap-2 text-amber-700">
            <IconWarnFill className="w-4 h-4" />
            <p className="text-[11px] uppercase tracking-wide">Alertas (24h)</p>
          </div>
          <p className="text-2xl font-bold text-amber-700 mt-1.5 num-tabular">
            {val(m?.alerts_last_24h)}
          </p>
        </div>
        <div className="bg-white rounded-xl border border-rose-200 p-4 shadow-card">
          <div className="flex items-center gap-2 text-rose-700">
            <IconCopySquare className="w-4 h-4" />
            <p className="text-[11px] uppercase tracking-wide">Duplicados (7d)</p>
          </div>
          <p className="text-2xl font-bold text-rose-700 mt-1.5 num-tabular">
            {val(m?.duplicates_last_7d)}
          </p>
        </div>
      </div>
    </section>
  );
}
