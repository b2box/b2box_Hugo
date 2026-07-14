import { useState } from "react";
import { SECTION_ICON, IconLayers } from "../icons";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { AuditTarget, SectionsResponse } from "../types";
import type { ButtonProps } from "@/components/ui/button";

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

const AUDIT_BUTTONS: { target: AuditTarget; label: string; variant: ButtonProps["variant"] }[] = [
  { target: "prices", label: "Verificar precios", variant: "default" },
  { target: "duplicates", label: "Buscar duplicados", variant: "secondary" },
  { target: "quality", label: "Revisar calidad", variant: "outline" },
  { target: "pa_variants", label: "Detectar variantes PA", variant: "outline" },
  { target: "bx_no_image", label: "Detectar BX sin imagen", variant: "outline" },
];

const FEEDBACK_CLASS: Record<AuditFeedback["kind"], string> = {
  ok: "bg-success/10 border border-success/40 text-success",
  busy: "bg-warning/10 border border-warning/40 text-warning",
  error: "bg-destructive/10 border border-destructive/40 text-destructive",
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
    <Card className="bg-sidebar p-3 h-fit shadow-sm sticky md:top-24">
      <nav className="space-y-0.5">
        {items.map(([key, info]) => {
          const Icon = SECTION_ICON[key] ?? IconLayers;
          const active = key === currentSection;
          return (
            <button
              key={key}
              onClick={() => onSelect(key)}
              className={cn(
                "w-full text-left px-3 py-2 rounded-md flex items-center justify-between gap-2 text-sm border-l-2 transition-colors",
                active
                  ? "bg-primary/10 text-primary border-l-primary font-semibold"
                  : "text-sidebar-foreground border-transparent hover:bg-muted",
              )}
            >
              <span className="flex items-center gap-2.5 truncate">
                <span
                  className={cn(
                    "w-4 h-4 shrink-0 inline-flex items-center justify-center",
                    active ? "text-primary" : "text-muted-foreground",
                  )}
                >
                  <Icon className="w-full h-full" />
                </span>
                <span className="font-medium">{info.label}</span>
              </span>
              {info.count != null && (
                <span className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded-full shrink-0 num-tabular">
                  {info.count}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      <hr className="my-4 border-border" />

      <div className="px-1.5 py-1 space-y-2">
        {AUDIT_BUTTONS.map((b) => (
          <Button
            key={b.target}
            variant={b.variant}
            onClick={() => handleAudit(b.target)}
            disabled={busyTarget === b.target}
            className="w-full"
          >
            {busyTarget === b.target ? "Disparando…" : b.label}
          </Button>
        ))}
        {feedback && (
          <p className={cn("mt-2 p-2 rounded-md text-xs animate-fade-in", FEEDBACK_CLASS[feedback.kind])}>
            {feedback.message}
          </p>
        )}
      </div>
    </Card>
  );
}
