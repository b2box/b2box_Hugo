import { useEffect, useState } from "react";
import { getSettings, resetSetting, saveSetting } from "../api";
import { GROUP_LABELS } from "../sections";
import { IconCheck } from "../icons";
import type { Setting } from "../types";

// Vista de configuración runtime: sliders agrupados que se aplican en vivo.
export default function SettingsView() {
  const [settings, setSettings] = useState<Setting[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  // Valor local de cada slider mientras el usuario lo arrastra (antes de guardar).
  const [draft, setDraft] = useState<Record<string, number>>({});

  async function load() {
    setError(null);
    try {
      const items = await getSettings();
      setSettings(items);
      setDraft(Object.fromEntries(items.map((s) => [s.key, s.value])));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Error");
    }
  }

  useEffect(() => {
    load();
  }, []);

  function flashSaved() {
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  async function onSave(key: string) {
    try {
      await saveSetting(key, Number(draft[key]));
      flashSaved();
      load();
    } catch (err) {
      window.alert("No se pudo guardar: " + (err instanceof Error ? err.message : String(err)));
    }
  }

  async function onReset(key: string) {
    if (!window.confirm("¿Restablecer al default del .env?")) return;
    try {
      await resetSetting(key);
      flashSaved();
      load();
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
        <h2 className="text-lg font-semibold text-navy-900 flex items-center gap-2">
          <IconCheck className="w-5 h-5 text-navy-600" />
          Configuración
        </h2>
        {saved && (
          <p className="text-xs text-emerald-700 font-medium flex items-center gap-1">
            <IconCheck className="w-3.5 h-3.5" />
            Guardado
          </p>
        )}
      </div>
      <p className="text-sm text-navy-500">
        Estos valores se aplican en vivo (sin redeploy). Si cambiás algo y querés volver al default
        del .env, apretá "Restablecer".
      </p>

      {error ? (
        <div className="bg-rose-50 border border-rose-200 rounded-2xl p-6 text-rose-800 text-sm">
          Error: {error}
        </div>
      ) : settings === null ? (
        <div className="bg-white rounded-2xl border border-navy-200 p-8 text-center text-navy-400 text-sm">
          Cargando configuración…
        </div>
      ) : (
        <div className="space-y-3">
          {Object.entries(grouped).map(([group, items]) => (
            <div key={group} className="bg-white rounded-2xl border border-navy-200 p-5 shadow-card">
              <h3 className="text-sm font-semibold text-navy-700 uppercase tracking-wide mb-4">
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
            </div>
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
    <div className="border-b border-navy-100 last:border-0 pb-5 last:pb-0">
      <div className="flex items-center justify-between mb-1">
        <label className="text-sm font-medium text-navy-900" htmlFor={`set-${s.key}`}>
          {s.label}
          {s.modified && (
            <span className="ml-2 text-[10px] uppercase tracking-wide bg-amber-100 text-amber-800 px-1.5 py-0.5 rounded">
              modificado
            </span>
          )}
        </label>
        <span className="text-sm font-mono text-navy-700 num-tabular">{display(draftValue)}</span>
      </div>
      <p className="text-xs text-navy-500 mb-2">{s.description}</p>
      <div className="flex items-center gap-3">
        <input
          id={`set-${s.key}`}
          type="range"
          min={s.min}
          max={s.max}
          step={s.step}
          value={draftValue}
          onChange={(e) => onDraft(Number(e.target.value))}
          className="flex-1 accent-brand-600"
        />
        <button
          onClick={onSave}
          className="text-xs bg-brand-600 hover:bg-brand-700 text-white font-medium px-3 py-1.5 rounded-md transition"
        >
          Guardar
        </button>
        {s.modified && (
          <button onClick={onReset} className="text-xs text-navy-500 hover:text-navy-700 underline">
            Restablecer
          </button>
        )}
      </div>
      <p className="text-[11px] text-navy-400 mt-1">Default del .env: {display(s.default)}</p>
    </div>
  );
}
