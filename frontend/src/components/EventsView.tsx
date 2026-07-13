import EventCard, { EventActions } from "./EventCard";
import { SECTION_ICON, IconLayers, IconRefresh } from "../icons";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
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
        <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
          <span className="w-5 h-5 inline-flex items-center justify-center text-muted-foreground">
            <Icon className="w-5 h-5" />
          </span>
          <span>{label}</span>
        </h2>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="num-tabular">{total} eventos</span>
          <button
            onClick={onRefresh}
            className="hover:text-foreground flex items-center gap-1 transition-colors"
          >
            <IconRefresh className="w-3 h-3" />
            Refrescar
          </button>
        </div>
      </div>
      <p className="text-sm text-muted-foreground mb-4">{meta.desc || "—"}</p>

      <div className="space-y-3">
        {error ? (
          <Card className="bg-destructive/10 border-destructive/40 p-6 text-destructive text-sm">
            No se pudieron cargar los eventos: {error}
          </Card>
        ) : loading ? (
          <Skeleton />
        ) : events.length === 0 ? (
          <EmptyState placeholder={!!meta.placeholder} />
        ) : (
          events.map((e) => <EventCard key={e.id} e={e} actions={actions} />)
        )}
      </div>

      <div className="flex items-center justify-center gap-2 mt-5">
        <Button variant="outline" size="sm" onClick={() => onChangePage(-1)} disabled={page === 0}>
          ← Anterior
        </Button>
        <span className="text-sm text-muted-foreground px-3 num-tabular">
          Página {page + 1} de {totalPages}
        </span>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onChangePage(1)}
          disabled={(page + 1) * PAGE_SIZE >= total}
        >
          Siguiente →
        </Button>
      </div>
    </section>
  );
}

function Skeleton() {
  return (
    <>
      {[0, 1, 2].map((i) => (
        <Card key={i} className="p-4 animate-fade-in flex gap-4 animate-pulse">
          <div className="w-16 h-16 rounded-lg bg-muted shrink-0"></div>
          <div className="flex-1 space-y-2">
            <div className="h-3 bg-muted rounded w-1/3"></div>
            <div className="h-3 bg-muted rounded w-2/3"></div>
            <div className="h-2 bg-muted rounded w-1/4"></div>
          </div>
        </Card>
      ))}
    </>
  );
}

function EmptyState({ placeholder }: { placeholder: boolean }) {
  if (placeholder) {
    return (
      <Card className="bg-warning/10 border-warning/40 p-8 text-center text-warning text-sm">
        <p className="font-medium mb-1">Sección pendiente</p>
        <p>
          Esta vista todavía no tiene datos porque falta definir qué son "Orders" en tu flujo.
          Avisanos para configurarla.
        </p>
      </Card>
    );
  }
  return (
    <Card className="p-12 text-center text-muted-foreground text-sm">
      Todavía no hay eventos en esta sección.
    </Card>
  );
}
