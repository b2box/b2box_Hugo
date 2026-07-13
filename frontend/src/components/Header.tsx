import { IconLogout } from "../icons";
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
    <header className="bg-white border-b border-navy-200 sticky top-0 z-10">
      <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-navy-900 flex items-center justify-center text-white font-bold shadow-card tracking-tight">
            H
          </div>
          <div>
            <h1 className="text-base font-bold text-navy-900 leading-tight">Hugo</h1>
            <p className="text-xs text-navy-500">Control de calidad · catálogo B2Box</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {working.length > 0 && (
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-brand-50 text-sm border border-brand-100">
              <span className="w-2 h-2 rounded-full bg-brand-600 pulse-dot"></span>
              <span className="text-brand-700 font-medium">Verificando {working.join(" + ")}…</span>
            </div>
          )}
          <HealthBadge connected={connected} />
          <button
            onClick={onLogout}
            title="Cerrar sesión"
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-navy-100 hover:bg-navy-200 text-navy-700 text-sm font-medium transition"
          >
            <IconLogout className="w-4 h-4" />
            Salir
          </button>
        </div>
      </div>
    </header>
  );
}

function HealthBadge({ connected }: { connected: boolean | null }) {
  if (connected === true) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-50 text-sm">
        <span className="w-2 h-2 rounded-full bg-emerald-500"></span>
        <span className="text-emerald-700 font-medium">Activo</span>
      </div>
    );
  }
  if (connected === false) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-rose-50 text-sm">
        <span className="w-2 h-2 rounded-full bg-rose-500"></span>
        <span className="text-rose-700 font-medium">Sin conexión</span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-navy-100 text-sm">
      <span className="w-2 h-2 rounded-full bg-navy-400 pulse-dot"></span>
      <span className="text-navy-600">Conectando…</span>
    </div>
  );
}
