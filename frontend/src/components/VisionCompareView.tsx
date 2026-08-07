import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { compareVision } from "../api";
import { Card } from "@/components/ui/card";
import { IconLayers } from "../icons";
import type { VisionVerdict } from "../types";

// Banco de pruebas: pegás un link y los dos proveedores contestan sobre la MISMA
// lista corta de CLIP. Sirve para decidir cuál dejamos en producción con casos
// reales en vez de con opiniones.

const PROVIDER_LABEL: Record<string, string> = {
  anthropic: "Anthropic (Claude)",
  openai: "OpenAI (GPT)",
};

function VerdictCard({ provider, v }: { provider: string; v: VisionVerdict }) {
  // Tres estados distintos, y la diferencia importa: encontró / miró y dijo que
  // ninguno / no pudo contestar.
  const tone = !v.answered
    ? "border-muted"
    : v.found
      ? "border-primary/50"
      : "border-warning/50";

  return (
    <Card className={`p-5 shadow-sm border ${tone}`}>
      <h3 className="text-sm font-semibold uppercase tracking-wide mb-3">
        {PROVIDER_LABEL[provider] ?? provider}
      </h3>

      {!v.answered ? (
        <p className="text-sm text-muted-foreground">
          No contestó (sin API key, timeout o respuesta inválida). Distinto de
          decir que ninguno matchea.
        </p>
      ) : v.found ? (
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            {v.image_url ? (
              <img
                src={v.image_url}
                alt={v.product_name ?? ""}
                className="w-20 h-20 object-contain rounded bg-white shrink-0"
              />
            ) : null}
            <div className="min-w-0">
              <p className="font-medium text-foreground break-words">{v.product_name}</p>
              <p className="text-xs text-muted-foreground">{v.product_code}</p>
            </div>
          </div>
          <p className="text-sm">
            Confianza:{" "}
            <span className="num-tabular font-semibold">
              {((v.confidence ?? 0) * 100).toFixed(0)}%
            </span>
          </p>
          <p className="text-sm text-muted-foreground italic">{v.reason}</p>
        </div>
      ) : (
        <div className="space-y-2">
          <p className="font-medium text-foreground">Ninguno es el mismo producto</p>
          <p className="text-sm text-muted-foreground italic">{v.reason}</p>
        </div>
      )}

      <p className="text-xs text-muted-foreground mt-4">
        {v.model} · {v.elapsed_ms} ms
      </p>
    </Card>
  );
}

export default function VisionCompareView() {
  const [url, setUrl] = useState("");
  const m = useMutation({ mutationFn: compareVision });
  const data = m.data;

  return (
    <section className="space-y-4">
      <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
        <IconLayers className="w-5 h-5 text-primary" />
        Comparar visión
      </h2>

      <Card className="p-5 shadow-sm space-y-3">
        <p className="text-sm text-muted-foreground">
          Pegá un link de producto. CLIP arma la lista corta y cada proveedor
          decide sobre las mismas fotos: la única variable es el modelo.
        </p>
        <form
          className="flex flex-col sm:flex-row gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (url.trim()) m.mutate(url.trim());
          }}
        >
          <input
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://articulo.mercadolibre.com.ar/…"
            className="flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={m.isPending || !url.trim()}
            className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium disabled:opacity-50"
          >
            {m.isPending ? "Comparando…" : "Comparar"}
          </button>
        </form>
        {m.isPending ? (
          <p className="text-xs text-muted-foreground">
            Puede tardar: baja las fotos, arma la lámina y espera a los dos modelos.
          </p>
        ) : null}
      </Card>

      {m.error ? (
        <Card className="bg-destructive/10 border-destructive/40 p-6 text-destructive text-sm">
          Error: {m.error instanceof Error ? m.error.message : "Error"}
        </Card>
      ) : null}

      {data?.status === "indexing" ? (
        <Card className="p-6 text-sm text-muted-foreground">
          El índice de imágenes se está construyendo. Reintentá en un rato.
        </Card>
      ) : null}

      {data?.status === "no_candidates" ? (
        <Card className="p-6 text-sm text-muted-foreground">
          CLIP no devolvió ningún candidato para esas fotos.
        </Card>
      ) : null}

      {data?.status === "ok" && data.verdicts ? (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {Object.entries(data.verdicts).map(([provider, v]) => (
              <VerdictCard key={provider} provider={provider} v={v} />
            ))}
          </div>

          <Card className="p-5 shadow-sm">
            <h3 className="text-sm font-semibold uppercase tracking-wide mb-1">
              Lista corta de CLIP
            </h3>
            <p className="text-xs text-muted-foreground mb-3">
              Los candidatos que vieron los dos modelos, en el orden en que los
              numeró la lámina.
            </p>
            <ol className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3">
              {(data.candidates ?? []).map((c, i) => (
                <li key={c.id} className="text-xs space-y-1">
                  {c.image_url ? (
                    <img
                      src={c.image_url}
                      alt={c.name}
                      className="w-full aspect-square object-contain rounded bg-white"
                    />
                  ) : null}
                  <p className="font-medium">
                    {i + 1}. <span className="num-tabular">{c.clip_score.toFixed(3)}</span>
                  </p>
                  <p className="text-muted-foreground line-clamp-2">{c.name}</p>
                </li>
              ))}
            </ol>
          </Card>
        </>
      ) : null}
    </section>
  );
}
