import { useEffect, useState } from "react";
import { getHistory } from "../api";
import { TONES } from "../sections";
import { fmtTime } from "../lib/format";
import { IconX } from "../icons";
import { cn } from "@/lib/utils";
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
      className="fixed inset-0 bg-foreground/40 z-50 flex items-center justify-center p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-card rounded-lg border border-border max-w-3xl w-full max-h-[80vh] overflow-hidden flex flex-col shadow-lg">
        <div className="px-6 py-4 border-b border-border flex items-center justify-between">
          <h3 className="font-semibold text-foreground">
            Historial del producto <span className="font-mono text-sm">{productId}</span>
          </h3>
          <button
            onClick={onClose}
            aria-label="Cerrar"
            className="w-7 h-7 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted flex items-center justify-center transition-colors"
          >
            <IconX className="w-4 h-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto scrollbar-thin p-6 text-sm text-muted-foreground">
          {error ? (
            <p className="text-destructive">Error: {error}</p>
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
      <div className="mb-4 p-3 rounded-md bg-muted flex items-center justify-between">
        <span className="text-foreground">Estado actual:</span>
        {enabled === true ? (
          <span className="text-success bg-success/10 px-2 py-0.5 rounded">Enabled actualmente</span>
        ) : enabled === false ? (
          <span className="text-destructive bg-destructive/10 px-2 py-0.5 rounded">
            Disabled actualmente
          </span>
        ) : (
          <span className="text-muted-foreground bg-muted px-2 py-0.5 rounded">
            No existe en Vendure
          </span>
        )}
      </div>
      <p className="text-xs text-muted-foreground mb-3">
        {data.total_events} eventos registrados, en orden cronológico.
      </p>
      {data.events.length === 0 ? (
        <p className="text-center text-muted-foreground py-8">Sin eventos registrados.</p>
      ) : (
        data.events.map((ev) => {
          const t = TONES[ev.tone] ?? TONES.muted;
          return (
            <div key={ev.id} className="flex gap-3 pb-4 border-b border-border last:border-0 mb-4">
              <div className={cn("w-2 h-2 rounded-full mt-2 shrink-0", t.dot)}></div>
              <div className="flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-foreground">{ev.title}</span>
                  <span className="text-xs text-muted-foreground">{fmtTime(ev.created_at)}</span>
                </div>
                <p className="text-xs text-muted-foreground mt-1">{ev.detail || ""}</p>
                <div className="flex gap-2 mt-1">
                  {ev.source && (
                    <span className="text-[10px] uppercase tracking-wide bg-muted text-muted-foreground px-1.5 py-0.5 rounded">
                      {ev.source}
                    </span>
                  )}
                  {ev.dismissed && (
                    <span className="text-[10px] uppercase tracking-wide bg-warning/10 text-warning px-1.5 py-0.5 rounded">
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
