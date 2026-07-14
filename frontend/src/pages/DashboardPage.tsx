import { useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import Header from "../components/Header";
import Sidebar, { AuditFeedback } from "../components/Sidebar";
import MetricsRow from "../components/MetricsRow";
import EventsView from "../components/EventsView";
import SettingsView from "../components/SettingsView";
import HistoryModal from "../components/HistoryModal";
import { EventActions } from "../components/EventCard";
import {
  confirmDisableBx as apiConfirmBx,
  confirmDuplicate as apiConfirmDup,
  dismissEvent as apiDismiss,
  getEvents,
  getSections,
  getStatus,
  logout as apiLogout,
  retryPaco as apiRetry,
  runAudit as apiRunAudit,
} from "../api";
import { PAGE_SIZE } from "../sections";
import type { AuditTarget } from "../types";

// Auto-refresh en background: React Query trae la data nueva y la MERGEA en el
// lugar (los cards con la misma id no se re-montan), sin skeleton ni flash.
const POLL_MS = 20_000;

export default function DashboardPage() {
  const qc = useQueryClient();
  const [currentSection, setCurrentSection] = useState("inbox_luis");
  const [page, setPage] = useState(0);
  const [historyProductId, setHistoryProductId] = useState<string | null>(null);

  // ─── Queries (con polling que pausa al ocultar la pestaña) ───────
  const statusQ = useQuery({
    queryKey: ["status"],
    queryFn: getStatus,
    refetchInterval: POLL_MS,
  });
  const sectionsQ = useQuery({
    queryKey: ["sections"],
    queryFn: getSections,
    refetchInterval: POLL_MS,
  });
  const eventsQ = useQuery({
    queryKey: ["events", currentSection, page],
    queryFn: () => getEvents(currentSection, page * PAGE_SIZE, PAGE_SIZE),
    enabled: currentSection !== "settings",
    placeholderData: keepPreviousData, // paginación sin flicker
    refetchInterval: POLL_MS, // auto-refresh silencioso en background
  });

  const status = statusQ.data ?? null;
  const connected = statusQ.isError ? false : statusQ.isSuccess ? true : null;
  const sections = sectionsQ.data ?? {};
  const events = eventsQ.data?.items ?? [];
  const total = eventsQ.data?.total ?? 0;

  const invalidateAll = () => {
    qc.invalidateQueries({ queryKey: ["events"] });
    qc.invalidateQueries({ queryKey: ["sections"] });
    qc.invalidateQueries({ queryKey: ["status"] });
  };

  function selectSection(key: string) {
    setCurrentSection(key);
    setPage(0);
  }

  function changePage(delta: number) {
    const next = page + delta;
    if (next < 0 || next * PAGE_SIZE >= total) return;
    setPage(next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function handleRunAudit(target: AuditTarget): Promise<AuditFeedback> {
    const r = await apiRunAudit(target);
    if (r.status === 409) {
      const data = await r.json().catch(() => ({ detail: "Ya hay una en curso." }));
      return { kind: "busy", message: data.detail };
    }
    if (!r.ok) return { kind: "error", message: r.statusText };
    // La auditoría corre async en el backend → refrescamos escalonado.
    [3, 10, 30].forEach((s) => setTimeout(invalidateAll, s * 1000));
    return { kind: "ok", message: "Auditoría disparada" };
  }

  const actions: EventActions = {
    onRetryPaco: async (id) => {
      await apiRetry(id);
      invalidateAll();
    },
    onConfirmDuplicate: async (id) => {
      const res = await apiConfirmDup(id);
      if (res.action === "no-op") {
        window.alert("El producto ya estaba deshabilitado en Vendure. Lo descartamos del listado.");
      }
      invalidateAll();
    },
    onConfirmDisableBx: async (id) => {
      const res = await apiConfirmBx(id);
      if (res.action === "no-op") {
        window.alert("El producto ya estaba deshabilitado en Vendure. Lo descartamos del listado.");
      }
      invalidateAll();
    },
    onDismiss: async (id) => {
      await apiDismiss(id);
      invalidateAll();
    },
    onViewHistory: (productId) => setHistoryProductId(productId),
  };

  async function handleLogout() {
    await apiLogout();
    window.location.href = "/login";
  }

  const label =
    currentSection === "settings"
      ? "Configuración"
      : sections[currentSection]?.label ?? currentSection;

  return (
    <>
      <Header status={status} connected={connected} onLogout={handleLogout} />

      <div className="max-w-7xl mx-auto px-6 py-6 grid grid-cols-1 md:grid-cols-[260px_1fr] gap-6">
        <Sidebar
          sections={sections}
          currentSection={currentSection}
          onSelect={selectSection}
          onRunAudit={handleRunAudit}
        />

        <main className="space-y-6 min-w-0">
          <MetricsRow status={status} />

          {currentSection === "settings" ? (
            <SettingsView />
          ) : (
            <EventsView
              section={currentSection}
              label={label}
              events={events}
              total={total}
              page={page}
              loading={eventsQ.isPending || (eventsQ.isFetching && eventsQ.isPlaceholderData)}
              error={eventsQ.error instanceof Error ? eventsQ.error.message : null}
              onRefresh={() => eventsQ.refetch()}
              onChangePage={changePage}
              actions={actions}
            />
          )}
        </main>
      </div>

      <footer className="max-w-7xl mx-auto px-6 py-6 text-xs text-muted-foreground text-center">
        Hugo · agente B2Box ·{" "}
        <a href="/docs" className="text-primary underline hover:no-underline transition-colors">
          API docs
        </a>
      </footer>

      {historyProductId && (
        <HistoryModal productId={historyProductId} onClose={() => setHistoryProductId(null)} />
      )}
    </>
  );
}
