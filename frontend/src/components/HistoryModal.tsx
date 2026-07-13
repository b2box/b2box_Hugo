import { useEffect, useState } from "react";
import { getHistory } from "../api";
import { TONES } from "../sections";
import { fmtTime } from "../lib/format";
import { IconX } from "../icons";
import type { HistoryResponse } from "../types";

// Modal overlay con la timeline del producto. Se abre desde el botón "Historial"
// de una tarjeta de evento.
export default function HistoryModal({
  productId,
  onClose,
}: {
  productId: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getHistory(productId)
      .then((d) => alive && setData(d))
      .catch((err) => alive && setError(err instanceof Error ? err.message : "Error"));
    return () => {
      alive = false;
    };
  }, [productId]);

  return (
    <div
      className="fixed inset-0 bg-navy-950/50 z-50 flex items-center justify-center p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-white rounded-2xl max-w-3xl w-full max-h-[80vh] overflow-hidden flex flex-col shadow-card-hover">
        <div className="px-6 py-4 border-b border-navy-200 flex items-center justify-between">
          <h3 className="font-semibold text-navy-900">
            Historial del producto <span className="font-mono text-sm">{productId}</span>
          </h3>
          <button
            onClick={onClose}
            aria-label="Cerrar"
            className="w-7 h-7 rounded-md text-navy-400 hover:text-navy-700 hover:bg-navy-100 flex items-center justify-center transition"
          >
            <IconX className="w-4 h-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-6 text-sm text-navy-500">
          {error ? (
            <p className="text-rose-700">Error: {error}</p>
          ) : !data ? (
            "Cargando…"
          ) : (
            <HistoryBody data={data} />
          )}
        </div>
      </div>
    </div>
  );
}

function HistoryBody({ data }: { data: HistoryResponse }) {
  const enabled = data.current_state_in_vendure.enabled;
  return (
    <>
      <div className="mb-4 p-3 rounded-lg bg-navy-50 flex items-center justify-between">
        <span className="text-navy-700">Estado actual:</span>
        {enabled === true ? (
          <span className="text-emerald-700 bg-emerald-100 px-2 py-0.5 rounded">
            Enabled actualmente
          </span>
        ) : enabled === false ? (
          <span className="text-rose-700 bg-rose-100 px-2 py-0.5 rounded">
            Disabled actualmente
          </span>
        ) : (
          <span className="text-navy-600 bg-navy-100 px-2 py-0.5 rounded">No existe en Vendure</span>
        )}
      </div>
      <p className="text-xs text-navy-400 mb-3">
        {data.total_events} eventos registrados, en orden cronológico.
      </p>
      {data.events.length === 0 ? (
        <p className="text-center text-navy-400 py-8">Sin eventos registrados.</p>
      ) : (
        data.events.map((ev) => {
          const t = TONES[ev.tone] ?? TONES.muted;
          return (
            <div
              key={ev.id}
              className="flex gap-3 pb-4 border-b border-navy-100 last:border-0 mb-4"
            >
              <div className={`w-2 h-2 rounded-full ${t.dot} mt-2 shrink-0`}></div>
              <div className="flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-navy-900">{ev.title}</span>
                  <span className="text-xs text-navy-400">{fmtTime(ev.created_at)}</span>
                </div>
                <p className="text-xs text-navy-600 mt-1">{ev.detail || ""}</p>
                <div className="flex gap-2 mt-1">
                  {ev.source && (
                    <span className="text-[10px] uppercase tracking-wide bg-navy-100 text-navy-600 px-1.5 py-0.5 rounded">
                      {ev.source}
                    </span>
                  )}
                  {ev.dismissed && (
                    <span className="text-[10px] uppercase tracking-wide bg-amber-100 text-amber-800 px-1.5 py-0.5 rounded">
                      descartado
                    </span>
                  )}
                </div>
              </div>
            </div>
          );
        })
      )}
    </>
  );
}
