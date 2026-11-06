import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { getLuisIntake, type LuisIntakeItem } from "../api";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { IconExternalLink } from "../icons";
import { fmtArs, fmtPct, fmtTime } from "../lib/format";
import { SECTION_META } from "../sections";
import { COLOR_META } from "./SemaforoView";

// Lo que mandó Luis: Google Lens al recibirlo y, cuando Paco lo sube a Vendure, nuestro precio contra
// lo que trajo Lens de Mercado Libre, con los colores del semáforo. Solo lee.

const LENS_LABEL: Record<string, string> = {
  pendiente: "Lens pendiente",
  ok: "Lens ok",
  sin_ml: "Lens sin ML",
  error: "Lens con error",
  apagado: "Lens apagado",
};

function ColorBadge({ item }: { item: LuisIntakeItem }) {
  if (!item.vendure_product_id) {
    return <Badge variant="outline" className="text-muted-foreground">Esperando subida</Badge>;
  }
  if (item.status === "sin_precio") {
    return <Badge variant="outline" className="text-muted-foreground">Sin precio en Vendure</Badge>;
  }
  const meta = COLOR_META[(item.color ?? "sin_dato") as keyof typeof COLOR_META] ?? COLOR_META.sin_dato;
  return (
    <Badge variant="outline" className={cn("gap-1.5", meta.chip)}>
      <span className={cn("size-2 rounded-full", meta.dot)} />
      {meta.label}
    </Badge>
  );
}

function Row({ item }: { item: LuisIntakeItem }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr className="border-t border-border align-top">
        <td className="p-2">
          {item.image_url ? (
            <img src={item.image_url} alt="" className="size-12 rounded object-cover" loading="lazy" />
          ) : null}
        </td>
        <td className="p-2 min-w-0">
          <div className="font-medium line-clamp-2">{item.name || "—"}</div>
          <div className="text-xs text-muted-foreground flex gap-2 flex-wrap">
            <a href={item.source_url} target="_blank" rel="noreferrer noopener" className="underline">
              1688 #{item.offer_id}
            </a>
            {item.vendure_product_id ? <span>Vendure #{item.vendure_product_id}</span> : null}
            <span>recibido {fmtTime(item.received_at)}</span>
          </div>
        </td>
        <td className="p-2 whitespace-nowrap"><ColorBadge item={item} /></td>
        <td className="p-2 text-right whitespace-nowrap">{fmtArs(item.our_price_cents)}</td>
        <td className="p-2 text-right whitespace-nowrap">{fmtArs(item.ml_median_cents)}</td>
        <td className="p-2 text-right whitespace-nowrap">{fmtPct(item.margin_pct)}</td>
        <td className="p-2 whitespace-nowrap">
          <Button variant="ghost" size="sm" onClick={() => setOpen(!open)} disabled={!item.ml_matches.length}>
            {item.ml_matches.length} en ML
          </Button>
          <div className="text-xs text-muted-foreground">{LENS_LABEL[item.lens_status] ?? item.lens_status}</div>
        </td>
      </tr>
      {open ? (
        <tr>
          <td />
          <td colSpan={6} className="p-2 pt-0">
            <ul className="text-sm space-y-1">
              {item.ml_matches.map((m, i) => (
                <li key={i} className="flex gap-2 items-baseline">
                  <span className="tabular-nums w-28 text-right">{fmtArs(m.price_cents)}</span>
                  {m.url ? (
                    <a href={m.url} target="_blank" rel="noreferrer noopener" className="underline inline-flex gap-1 items-center">
                      {m.title || "Publicación"} <IconExternalLink className="size-3" />
                    </a>
                  ) : (
                    <span>{m.title || "Publicación"}</span>
                  )}
                </li>
              ))}
            </ul>
          </td>
        </tr>
      ) : null}
    </>
  );
}

export default function SubidosView() {
  const [onlyUploaded, setOnlyUploaded] = useState(true);
  const q = useQuery({
    queryKey: ["luis-intake", onlyUploaded],
    queryFn: () => getLuisIntake(onlyUploaded),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  });
  const items = q.data?.items ?? [];
  return (
    <Card className="p-4 space-y-3">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-lg font-semibold">Subidos a Vendure</h2>
          <p className="text-sm text-muted-foreground max-w-3xl">{SECTION_META.subidos?.desc}</p>
        </div>
        <div className="flex gap-2">
          <Button variant={onlyUploaded ? "default" : "outline"} size="sm" onClick={() => setOnlyUploaded(true)}>
            Subidos
          </Button>
          <Button variant={!onlyUploaded ? "default" : "outline"} size="sm" onClick={() => setOnlyUploaded(false)}>
            Todo lo de Luis
          </Button>
        </div>
      </div>
      {q.isError ? <p className="text-sm text-destructive">No se pudo cargar la lista.</p> : null}
      {!q.isLoading && !items.length ? (
        <p className="text-sm text-muted-foreground">
          {onlyUploaded ? "Todavía no se subió a Vendure nada de lo que mandó Luis." : "Luis todavía no mandó productos nuevos."}
        </p>
      ) : null}
      {items.length ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted-foreground">
              <tr>
                <th className="p-2" />
                <th className="p-2">Producto</th>
                <th className="p-2">Color</th>
                <th className="p-2 text-right">Nuestro precio</th>
                <th className="p-2 text-right">Mediana ML</th>
                <th className="p-2 text-right">Margen reventa</th>
                <th className="p-2">Google Lens</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <Row key={item.id} item={item} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Card>
  );
}
