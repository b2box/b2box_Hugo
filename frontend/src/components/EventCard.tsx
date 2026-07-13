import { useState } from "react";
import Thumb from "./Thumb";
import { TONES } from "../sections";
import { fmtPrice, fmtTime, shortUrl } from "../lib/format";
import {
  IconArrowRight,
  IconCheckCircle,
  IconClock,
  IconExternalLink,
  IconRefresh,
  IconX,
} from "../icons";
import type { AuditEvent } from "../types";

export interface EventActions {
  onRetryPaco: (id: number) => Promise<void>;
  onConfirmDuplicate: (id: number) => Promise<void>;
  onConfirmDisableBx: (id: number) => Promise<void>;
  onDismiss: (id: number) => Promise<void>;
  onViewHistory: (productId: string) => void;
}

type ActionKey = "retry" | "dup" | "bx" | "dismiss";

export default function EventCard({ e, actions }: { e: AuditEvent; actions: EventActions }) {
  const [busy, setBusy] = useState<ActionKey | null>(null);
  const t = TONES[e.tone] ?? TONES.muted;
  const p = e.product;
  const r = e.related_product;
  const beforeStr = fmtPrice(e.before);
  const afterStr = fmtPrice(e.after);

  const hasId = !!p.id && p.id !== "(nuevo)";
  const canRetry = (e.action === "paco_failed" || e.action === "verify_no_match") && !!p.image_url;
  const canConfirmDuplicate = e.action === "duplicate_flagged" && hasId;
  const canConfirmDisableBx = e.action === "bx_no_image_flagged" && hasId;
  const canDismiss = e.action !== "verify_passed_to_paco";
  const canViewHistory = hasId;

  async function run(key: ActionKey, confirmMsg: string | null, fn: () => Promise<void>) {
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    setBusy(key);
    try {
      await fn();
      // En éxito el padre remueve la tarjeta del listado (este componente se
      // desmonta), así que no hace falta re-habilitar el botón.
    } catch (err) {
      setBusy(null);
      window.alert(`Error: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  return (
    <div
      className={`audit-card bg-white rounded-2xl border border-navy-200 border-l-4 ${t.border} p-4 fade-in flex gap-4 shadow-card`}
    >
      <Thumb imageUrl={p.image_url} alt={p.name} />
      <div className="flex-1 min-w-0">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className={`inline-block w-2 h-2 rounded-full ${t.dot}`}></span>
              <span className="font-semibold text-navy-900">{e.title}</span>
              {p.code && (
                <span className={`text-xs font-mono ${t.badge} px-2 py-0.5 rounded`}>
                  BX {p.code}
                </span>
              )}
              {e.source && (
                <span className="text-[10px] uppercase tracking-wide bg-navy-100 text-navy-600 px-1.5 py-0.5 rounded">
                  {e.source}
                </span>
              )}
            </div>
            <p className="text-sm text-navy-700 mt-1 truncate" title={p.name || ""}>
              {p.name || `Producto #${p.id}`}
            </p>
            <p className="text-xs text-navy-400 mt-0.5">
              ID: <span className="font-mono">{p.id}</span>
            </p>
            {p.source_url && (
              <div className="text-xs mt-1 flex items-center gap-1.5 text-navy-500">
                <IconExternalLink className="w-3 h-3" />
                <a
                  href={p.source_url}
                  target="_blank"
                  rel="noopener"
                  className="truncate text-brand-600 hover:underline"
                  title={p.source_url}
                >
                  {shortUrl(p.source_url)}
                </a>
              </div>
            )}
          </div>
          <p className="text-xs text-navy-400 shrink-0 whitespace-nowrap">{fmtTime(e.created_at)}</p>
        </div>

        {beforeStr && afterStr && (
          <div className="flex items-center gap-2 text-sm mt-2 num-tabular">
            <span className="text-navy-500">{beforeStr}</span>
            <IconArrowRight className="w-4 h-4 text-navy-400" />
            <strong className="text-navy-900">{afterStr}</strong>
          </div>
        )}

        {e.detail && (
          <p className="text-xs text-navy-500 mt-2 leading-relaxed">{e.detail}</p>
        )}

        {r && r.id && (
          <div className="text-xs text-navy-500 mt-2 flex items-center gap-1.5">
            <span>relacionado:</span>
            {r.code && (
              <span className="font-mono bg-navy-100 px-1.5 py-0.5 rounded">{r.code}</span>
            )}
            <span className="text-navy-700">{r.name || `#${r.id}`}</span>
          </div>
        )}

        {(canRetry || canConfirmDuplicate || canConfirmDisableBx || canDismiss || canViewHistory) && (
          <div className="flex items-center gap-2 mt-3 pt-3 border-t border-navy-100 flex-wrap">
            {canConfirmDuplicate && (
              <button
                onClick={() =>
                  run(
                    "dup",
                    "¿Confirmar este duplicado y deshabilitarlo en Vendure? Se puede revertir desde el historial.",
                    () => actions.onConfirmDuplicate(e.id),
                  )
                }
                disabled={busy === "dup"}
                title="Deshabilita el producto en Vendure (se puede revertir desde history)"
                className="text-xs bg-rose-600 hover:bg-rose-700 text-white font-medium px-3 py-1.5 rounded-md flex items-center gap-1 transition disabled:opacity-50"
              >
                <IconCheckCircle className="w-3 h-3" />
                {busy === "dup" ? "Deshabilitando…" : "Confirmar duplicado"}
              </button>
            )}
            {canConfirmDisableBx && (
              <button
                onClick={() =>
                  run(
                    "bx",
                    '¿Deshabilitar este producto en Vendure? Su nombre empieza con "BX…" y no tiene imagen. Se puede revertir desde el historial.',
                    () => actions.onConfirmDisableBx(e.id),
                  )
                }
                disabled={busy === "bx"}
                title="Deshabilita este producto en Vendure por no tener imagen y nombre placeholder (BX…)"
                className="text-xs bg-rose-600 hover:bg-rose-700 text-white font-medium px-3 py-1.5 rounded-md flex items-center gap-1 transition disabled:opacity-50"
              >
                <IconCheckCircle className="w-3 h-3" />
                {busy === "bx" ? "Deshabilitando…" : "Confirmar deshabilitar"}
              </button>
            )}
            {canRetry && (
              <button
                onClick={() => run("retry", null, () => actions.onRetryPaco(e.id))}
                disabled={busy === "retry"}
                className="text-xs bg-brand-600 hover:bg-brand-700 text-white font-medium px-3 py-1.5 rounded-md flex items-center gap-1 transition disabled:opacity-50"
              >
                <IconRefresh className="w-3 h-3" />
                {busy === "retry" ? "Reintentando…" : "Reintentar Paco"}
              </button>
            )}
            {canViewHistory && (
              <button
                onClick={() => actions.onViewHistory(p.id as string)}
                className="text-xs bg-navy-100 hover:bg-navy-200 text-navy-700 font-medium px-3 py-1.5 rounded-md flex items-center gap-1 transition"
              >
                <IconClock className="w-3 h-3" />
                Historial
              </button>
            )}
            {canDismiss && (
              <button
                onClick={() =>
                  run(
                    "dismiss",
                    "¿Descartar este evento? No se elimina nada en Vendure, solo desaparece de esta lista.",
                    () => actions.onDismiss(e.id),
                  )
                }
                disabled={busy === "dismiss"}
                className="text-xs bg-navy-100 hover:bg-navy-200 text-navy-700 font-medium px-3 py-1.5 rounded-md flex items-center gap-1 transition disabled:opacity-50"
              >
                <IconX className="w-3 h-3" />
                Descartar
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
