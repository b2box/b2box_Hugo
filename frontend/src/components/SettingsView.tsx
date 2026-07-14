import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getSettings, resetSetting, saveSetting } from "../api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { GROUP_LABELS } from "../sections";
import { IconCheck } from "../icons";
import type { Setting } from "../types";

// Vista de configuración runtime: sliders agrupados que se aplican en vivo.
export default function SettingsView() {
  const qc = useQueryClient();
  const settingsQ = useQuery({ queryKey: ["settings"], queryFn: getSettings });
  const settings = settingsQ.data ?? null;
  const error = settingsQ.error instanceof Error ? settingsQ.error.message : null;
  const [saved, setSaved] = useState(false);
  // Valor local de cada slider mientras el usuario lo arrastra (antes de guardar).
  const [draft, setDraft] = useState<Record<string, number>>({});

  // Sincronizamos el draft cuando llegan/cambian los settings del server.
  useEffect(() => {
    if (settings) setDraft(Object.fromEntries(settings.map((s) => [s.key, s.value])));
  }, [settings]);

  function flashSaved() {
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  async function onSave(key: string) {
    try {
      await saveSetting(key, Number(draft[key]));
      flashSaved();
      qc.invalidateQueries({ queryKey: ["settings"] });
    } catch (err) {
      window.alert("No se pudo guardar: " + (err instanceof Error ? err.message : String(err)));
    }
  }

  async function onReset(key: string) {
    if (!window.confirm("¿Restablecer al default del .env?")) return;
    try {
      await resetSetting(key);
      flashSaved();
      qc.invalidateQueries({ queryKey: ["settings"] });
    } catch (err) {
      window.alert("Error: " + (err instanceof Error ? err.message : String(err)));
    }
  }

  const grouped: Record<string, Setting[]> = {};
  (settings ?? []).forEach((s) => {
    (grouped[s.group] = grouped[s.group] || []).push(s);
  });

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
          <IconCheck className="w-5 h-5 text-muted-foreground" />
          Configuración
        </h2>
        {saved && (
          <p className="text-xs text-success font-medium flex items-center gap-1">
            <IconCheck className="w-3.5 h-3.5" />
            Guardado
          </p>
        )}
      </div>
      <p className="text-sm text-muted-foreground">
        Estos valores se aplican en vivo (sin redeploy). Si cambiás algo y querés volver al default
        del .env, apretá "Restablecer".
      </p>

      {error ? (
        <Card className="bg-destructive/10 border-destructive/40 p-6 text-destructive text-sm">
          Error: {error}
        </Card>
      ) : settings === null ? (
        <Card className="p-8 text-center text-muted-foreground text-sm">Cargando configuración…</Card>
      ) : (
        <div className="space-y-3">
          {Object.entries(grouped).map(([group, items]) => (
            <Card key={group} className="p-5 shadow-sm">
              <h3 className="text-sm font-semibold text-foreground uppercase tracking-wide mb-4">
                {GROUP_LABELS[group] || group}
              </h3>
              <div className="space-y-5">
                {items.map((s) => (
                  <SettingRow
                    key={s.key}
                    s={s}
                    draftValue={draft[s.key] ?? s.value}
                    onDraft={(v) => setDraft((d) => ({ ...d, [s.key]: v }))}
                    onSave={() => onSave(s.key)}
                    onReset={() => onReset(s.key)}
                  />
                ))}
              </div>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

function SettingRow({
  s,
  draftValue,
  onDraft,
  onSave,
  onReset,
}: {
  s: Setting;
  draftValue: number;
  onDraft: (v: number) => void;
  onSave: () => void;
  onReset: () => void;
}) {
  const isFloat = s.type === "float";
  const display = (v: number) => (isFloat ? Number(v).toFixed(2) : String(Number(v)));

  return (
    <div className="border-b border-border last:border-0 pb-5 last:pb-0">
      <div className="flex items-center justify-between mb-1">
        <label className="text-sm font-medium text-foreground flex items-center gap-2" htmlFor={`set-${s.key}`}>
          {s.label}
          {s.modified && <Badge variant="warning">modificado</Badge>}
        </label>
        <span className="text-sm font-mono text-foreground num-tabular">{display(draftValue)}</span>
      </div>
      <p className="text-xs text-muted-foreground mb-2">{s.description}</p>
      <div className="flex items-center gap-3">
        <input
          id={`set-${s.key}`}
          type="range"
          min={s.min}
          max={s.max}
          step={s.step}
          value={draftValue}
          onChange={(e) => onDraft(Number(e.target.value))}
          className="flex-1 accent-primary"
        />
        <Button size="sm" onClick={onSave}>
          Guardar
        </Button>
        {s.modified && (
          <Button variant="link" size="sm" onClick={onReset} className="px-1">
            Restablecer
          </Button>
        )}
      </div>
      <p className="text-[11px] text-muted-foreground mt-1">Default del .env: {display(s.default)}</p>
    </div>
  );
}
