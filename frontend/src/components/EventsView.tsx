import EventCard, { EventActions } from "./EventCard";
import { SECTION_ICON, IconLayers, IconRefresh } from "../icons";
import { PAGE_SIZE, SECTION_META } from "../sections";
import type { AuditEvent } from "../types";

interface EventsViewProps {
  section: string;
  label: string;
  events: AuditEvent[];
  total: number;
  page: number;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onChangePage: (delta: number) => void;
  actions: EventActions;
}

export default function EventsView({
  section,
  label,
  events,
  total,
  page,
  loading,
  error,
  onRefresh,
  onChangePage,
  actions,
}: EventsViewProps) {
  const meta = SECTION_META[section] ?? { desc: "" };
  const Icon = SECTION_ICON[section] ?? IconLayers;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <section>
      <div className="flex items-center justify-between mb-3 flex-wrap gap-3">
        <h2 className="text-lg font-semibold text-navy-900 flex items-center gap-2">
          <span className="w-5 h-5 inline-flex items-center justify-center text-navy-600">
            <Icon className="w-5 h-5" />
          </span>
          <span>{label}</span>
        </h2>
        <div className="flex items-center gap-3 text-xs text-navy-500">
          <span className="num-tabular">{total} eventos</span>
          <button
            onClick={onRefresh}
            className="hover:text-navy-700 flex items-center gap-1 transition"
          >
            <IconRefresh className="w-3 h-3" />
            Refrescar
          </button>
        </div>
      </div>
      <p className="text-sm text-navy-500 mb-4">{meta.desc || "—"}</p>

      <div className="space-y-3">
        {error ? (
          <div className="bg-rose-50 border border-rose-200 rounded-2xl p-6 text-rose-800 text-sm">
            No se pudieron cargar los eventos: {error}
          </div>
        ) : loading ? (
          <Skeleton />
        ) : events.length === 0 ? (
          <EmptyState placeholder={!!meta.placeholder} />
        ) : (
          events.map((e) => <EventCard key={e.id} e={e} actions={actions} />)
        )}
      </div>

      <div className="flex items-center justify-center gap-2 mt-5">
        <button
          onClick={() => onChangePage(-1)}
          disabled={page === 0}
          className="px-3 py-1.5 text-sm rounded-md border border-navy-300 bg-white text-navy-700 hover:bg-navy-100 disabled:opacity-40 disabled:cursor-not-allowed transition"
        >
          ← Anterior
        </button>
        <span className="text-sm text-navy-500 px-3 num-tabular">
          Página {page + 1} de {totalPages}
        </span>
        <button
          onClick={() => onChangePage(1)}
          disabled={(page + 1) * PAGE_SIZE >= total}
          className="px-3 py-1.5 text-sm rounded-md border border-navy-300 bg-white text-navy-700 hover:bg-navy-100 disabled:opacity-40 disabled:cursor-not-allowed transition"
        >
          Siguiente →
        </button>
      </div>
    </section>
  );
}

function Skeleton() {
  return (
    <>
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="bg-white rounded-2xl border border-navy-200 p-4 fade-in flex gap-4 animate-pulse"
        >
          <div className="w-16 h-16 rounded-lg bg-navy-200 shrink-0"></div>
          <div className="flex-1 space-y-2">
            <div className="h-3 bg-navy-200 rounded w-1/3"></div>
            <div className="h-3 bg-navy-200 rounded w-2/3"></div>
            <div className="h-2 bg-navy-200 rounded w-1/4"></div>
          </div>
        </div>
      ))}
    </>
  );
}

function EmptyState({ placeholder }: { placeholder: boolean }) {
  if (placeholder) {
    return (
      <div className="bg-amber-50 border border-amber-200 rounded-2xl p-8 text-center text-amber-900 text-sm">
        <p className="font-medium mb-1">Sección pendiente</p>
        <p>
          Esta vista todavía no tiene datos porque falta definir qué son "Orders" en tu flujo.
          Avisanos para configurarla.
        </p>
      </div>
    );
  }
  return (
    <div className="bg-white rounded-2xl border border-navy-200 p-12 text-center text-navy-400 text-sm">
      Todavía no hay eventos en esta sección.
    </div>
  );
}
