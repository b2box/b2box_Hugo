import { useState } from "react";
import Thumb from "./Thumb";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
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
    <Card className={cn("border-l-4 p-4 flex gap-4 shadow-sm", t.border)}>
      <Thumb imageUrl={p.image_url} alt={p.name} />
      <div className="flex-1 min-w-0">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className={cn("inline-block w-2 h-2 rounded-full", t.dot)}></span>
              <span className="font-semibold text-foreground">{e.title}</span>
              {p.code && (
                <span className={cn("text-xs font-mono px-2 py-0.5 rounded", t.badge)}>
                  BX {p.code}
                </span>
              )}
              {e.source && (
                <span className="text-[10px] uppercase tracking-wide bg-muted text-muted-foreground px-1.5 py-0.5 rounded">
                  {e.source}
                </span>
              )}
            </div>
            <p className="text-sm text-foreground mt-1 truncate" title={p.name || ""}>
              {p.name || `Producto #${p.id}`}
            </p>
            <p className="text-xs text-muted-foreground mt-0.5">
              ID: <span className="font-mono">{p.id}</span>
            </p>
            {p.source_url && (
              <div className="text-xs mt-1 flex items-center gap-1.5 text-muted-foreground">
                <IconExternalLink className="w-3 h-3" />
                <a
                  href={p.source_url}
                  target="_blank"
                  rel="noopener"
                  className="truncate text-primary hover:underline"
                  title={p.source_url}
                >
                  {shortUrl(p.source_url)}
                </a>
              </div>
            )}
          </div>
          <p className="text-xs text-muted-foreground shrink-0 whitespace-nowrap">
            {fmtTime(e.created_at)}
          </p>
        </div>

        {beforeStr && afterStr && (
          <div className="flex items-center gap-2 text-sm mt-2 num-tabular">
            <span className="text-muted-foreground">{beforeStr}</span>
            <IconArrowRight className="w-4 h-4 text-muted-foreground" />
            <strong className="text-foreground">{afterStr}</strong>
          </div>
        )}

        {e.detail && (
          <p className="text-xs text-muted-foreground mt-2 leading-relaxed">{e.detail}</p>
        )}

        {r && r.id && (
          <div className="text-xs text-muted-foreground mt-2 flex items-center gap-1.5">
            <span>relacionado:</span>
            {r.code && (
              <span className="font-mono bg-muted px-1.5 py-0.5 rounded">{r.code}</span>
            )}
            <span className="text-foreground">{r.name || `#${r.id}`}</span>
          </div>
        )}

        {(canRetry || canConfirmDuplicate || canConfirmDisableBx || canDismiss || canViewHistory) && (
          <div className="flex items-center gap-2 mt-3 pt-3 border-t border-border flex-wrap">
            {canConfirmDuplicate && (
              <Button
                variant="destructive"
                size="sm"
                onClick={() =>
                  run(
                    "dup",
                    "¿Confirmar este duplicado y deshabilitarlo en Vendure? Se puede revertir desde el historial.",
                    () => actions.onConfirmDuplicate(e.id),
                  )
                }
                disabled={busy === "dup"}
                title="Deshabilita el producto en Vendure (se puede revertir desde history)"
              >
                <IconCheckCircle className="w-3 h-3" />
                {busy === "dup" ? "Deshabilitando…" : "Confirmar duplicado"}
              </Button>
            )}
            {canConfirmDisableBx && (
              <Button
                variant="destructive"
                size="sm"
                onClick={() =>
                  run(
                    "bx",
                    '¿Deshabilitar este producto en Vendure? Su nombre empieza con "BX…" y no tiene imagen. Se puede revertir desde el historial.',
                    () => actions.onConfirmDisableBx(e.id),
                  )
                }
                disabled={busy === "bx"}
                title="Deshabilita este producto en Vendure por no tener imagen y nombre placeholder (BX…)"
              >
                <IconCheckCircle className="w-3 h-3" />
                {busy === "bx" ? "Deshabilitando…" : "Confirmar deshabilitar"}
              </Button>
            )}
            {canRetry && (
              <Button
                variant="default"
                size="sm"
                onClick={() => run("retry", null, () => actions.onRetryPaco(e.id))}
                disabled={busy === "retry"}
              >
                <IconRefresh className="w-3 h-3" />
                {busy === "retry" ? "Reintentando…" : "Reintentar Paco"}
              </Button>
            )}
            {canViewHistory && (
              <Button variant="secondary" size="sm" onClick={() => actions.onViewHistory(p.id as string)}>
                <IconClock className="w-3 h-3" />
                Historial
              </Button>
            )}
            {canDismiss && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() =>
                  run(
                    "dismiss",
                    "¿Descartar este evento? No se elimina nada en Vendure, solo desaparece de esta lista.",
                    () => actions.onDismiss(e.id),
                  )
                }
                disabled={busy === "dismiss"}
              >
                <IconX className="w-3 h-3" />
                Descartar
              </Button>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
