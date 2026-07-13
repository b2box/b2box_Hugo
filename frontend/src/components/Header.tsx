import { IconLogout } from "../icons";
import { Button } from "@/components/ui/button";
import type { StatusResponse } from "../types";

interface HeaderProps {
  status: StatusResponse | null;
  connected: boolean | null; // null = conectando, true = ok, false = sin conexión
  onLogout: () => void;
}

// Header sticky con logo, indicador "Trabajando…", badge de salud y botón salir.
export default function Header({ status, connected, onLogout }: HeaderProps) {
  const inProgress = status?.metrics.audit_in_progress ?? {};
  const working: string[] = [];
  if (inProgress.prices) working.push("precios");
  if (inProgress.duplicates) working.push("duplicados");

  return (
    <header className="bg-card border-b border-border sticky top-0 z-20">
      <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-primary flex items-center justify-center text-primary-foreground font-bold shadow-sm tracking-tight">
            H
          </div>
          <div>
            <h1 className="text-base font-bold text-foreground leading-tight">Hugo</h1>
            <p className="text-xs text-muted-foreground">Control de calidad · catálogo B2Box</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {working.length > 0 && (
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-primary/10 text-sm border border-primary/20">
              <span className="w-2 h-2 rounded-full bg-primary animate-pulse"></span>
              <span className="text-primary font-medium">Verificando {working.join(" + ")}…</span>
            </div>
          )}
          <HealthBadge connected={connected} />
          <Button variant="secondary" size="sm" onClick={onLogout} title="Cerrar sesión">
            <IconLogout className="w-4 h-4" />
            Salir
          </Button>
        </div>
      </div>
    </header>
  );
}

function HealthBadge({ connected }: { connected: boolean | null }) {
  if (connected === true) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-success/10 text-sm">
        <span className="w-2 h-2 rounded-full bg-success"></span>
        <span className="text-success font-medium">Activo</span>
      </div>
    );
  }
  if (connected === false) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-destructive/10 text-sm">
        <span className="w-2 h-2 rounded-full bg-destructive"></span>
        <span className="text-destructive font-medium">Sin conexión</span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-muted text-sm">
      <span className="w-2 h-2 rounded-full bg-muted-foreground animate-pulse"></span>
      <span className="text-muted-foreground">Conectando…</span>
    </div>
  );
}
