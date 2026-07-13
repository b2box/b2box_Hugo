import { useState } from "react";
import { SECTION_ICON, IconLayers } from "../icons";
import type { AuditTarget, SectionsResponse } from "../types";

export interface AuditFeedback {
  kind: "ok" | "busy" | "error";
  message: string;
}

interface SidebarProps {
  sections: SectionsResponse;
  currentSection: string;
  onSelect: (key: string) => void;
  onRunAudit: (target: AuditTarget) => Promise<AuditFeedback>;
}

const AUDIT_BUTTONS: { target: AuditTarget; label: string; className: string }[] = [
  {
    target: "prices",
    label: "Verificar precios",
    className: "bg-brand-600 hover:bg-brand-700 text-white",
  },
  {
    target: "duplicates",
    label: "Buscar duplicados",
    className: "bg-navy-900 hover:bg-navy-800 text-white",
  },
  {
    target: "quality",
    label: "Revisar calidad",
    className: "bg-white hover:bg-navy-50 text-navy-700 border border-navy-200",
  },
  {
    target: "pa_variants",
    label: "Detectar variantes PA",
    className: "bg-white hover:bg-navy-50 text-navy-700 border border-navy-200",
  },
  {
    target: "bx_no_image",
    label: "Detectar BX sin imagen",
    className: "bg-white hover:bg-navy-50 text-navy-700 border border-navy-200",
  },
];

const FEEDBACK_CLASS: Record<AuditFeedback["kind"], string> = {
  ok: "bg-emerald-50 border border-emerald-200 text-emerald-800",
  busy: "bg-amber-50 border border-amber-200 text-amber-900",
  error: "bg-rose-50 border border-rose-200 text-rose-800",
};

export default function Sidebar({
  sections,
  currentSection,
  onSelect,
  onRunAudit,
}: SidebarProps) {
  const [busyTarget, setBusyTarget] = useState<AuditTarget | null>(null);
  const [feedback, setFeedback] = useState<AuditFeedback | null>(null);

  async function handleAudit(target: AuditTarget) {
    setBusyTarget(target);
    try {
      const fb = await onRunAudit(target);
      setFeedback(fb);
    } catch (err) {
      setFeedback({ kind: "error", message: err instanceof Error ? err.message : "Error" });
    } finally {
      setTimeout(() => setBusyTarget(null), 3000);
    }
  }

  // La lista de secciones viene del backend; sumamos "settings" como ítem extra.
  const items: [string, { label: string; count: number | null }][] = [
    ...Object.entries(sections),
    ["settings", { label: "Configuración", count: null }],
  ];

  return (
    <aside className="bg-white rounded-2xl border border-navy-200 p-3 h-fit shadow-card sticky md:top-24">
      <nav className="space-y-0.5">
        {items.map(([key, info]) => {
          const Icon = SECTION_ICON[key] ?? IconLayers;
          const active = key === currentSection;
          return (
            <button
              key={key}
              onClick={() => onSelect(key)}
              className={`nav-item w-full text-left px-3 py-2 rounded-lg flex items-center justify-between gap-2 text-sm border-l-2 border-transparent hover:bg-navy-50 ${
                active ? "active" : ""
              }`}
            >
              <span className="flex items-center gap-2.5 truncate">
                <span className="nav-icon w-4 h-4 shrink-0 text-navy-400 inline-flex items-center justify-center">
                  <Icon className="w-full h-full" />
                </span>
                <span className="font-medium">{info.label}</span>
              </span>
              {info.count != null && (
                <span className="text-xs bg-navy-100 text-navy-600 px-2 py-0.5 rounded-full shrink-0 num-tabular">
                  {info.count}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      <hr className="my-4 border-navy-200" />

      <div className="px-1.5 py-1 space-y-2">
        {AUDIT_BUTTONS.map((b) => (
          <button
            key={b.target}
            onClick={() => handleAudit(b.target)}
            disabled={busyTarget === b.target}
            className={`w-full text-sm font-medium py-2 px-3 rounded-lg transition disabled:opacity-50 ${b.className}`}
          >
            {busyTarget === b.target ? "Disparando…" : b.label}
          </button>
        ))}
        {feedback && (
          <p className={`mt-2 p-2 rounded text-xs fade-in ${FEEDBACK_CLASS[feedback.kind]}`}>
            {feedback.message}
          </p>
        )}
      </div>
    </aside>
  );
}
