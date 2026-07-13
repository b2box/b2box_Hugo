import { useCallback, useEffect, useRef, useState } from "react";
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
import type { AuditEvent, AuditTarget, SectionsResponse, StatusResponse } from "../types";

export default function DashboardPage() {
  const [sections, setSections] = useState<SectionsResponse>({});
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);

  const [currentSection, setCurrentSection] = useState("inbox_luis");
  const [page, setPage] = useState(0);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [eventsLoading, setEventsLoading] = useState(true);
  const [eventsError, setEventsError] = useState<string | null>(null);

  const [historyProductId, setHistoryProductId] = useState<string | null>(null);

  // Refs para que el setInterval lea siempre el valor actual sin recrearse.
  const sectionRef = useRef(currentSection);
  const pageRef = useRef(page);
  sectionRef.current = currentSection;
  pageRef.current = page;

  const loadSections = useCallback(async () => {
    try {
      setSections(await getSections());
    } catch {
      /* el badge de salud ya refleja la desconexión */
    }
  }, []);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await getStatus());
      setConnected(true);
    } catch {
      setConnected(false);
    }
  }, []);

  const loadEvents = useCallback(async (section: string, pg: number) => {
    if (section === "settings") return;
    setEventsLoading(true);
    setEventsError(null);
    try {
      const d = await getEvents(section, pg * PAGE_SIZE, PAGE_SIZE);
      setEvents(d.items);
      setTotal(d.total);
    } catch (err) {
      setEventsError(err instanceof Error ? err.message : "Error");
    } finally {
      setEventsLoading(false);
    }
  }, []);

  // Init + polling cada 15s.
  useEffect(() => {
    loadSections();
    loadStatus();
    loadEvents("inbox_luis", 0);
    const id = setInterval(() => {
      loadStatus();
      loadSections();
      if (pageRef.current === 0) loadEvents(sectionRef.current, 0);
    }, 15000);
    return () => clearInterval(id);
  }, [loadSections, loadStatus, loadEvents]);

  function selectSection(key: string) {
    setCurrentSection(key);
    setPage(0);
    if (key !== "settings") loadEvents(key, 0);
  }

  function changePage(delta: number) {
    const next = page + delta;
    if (next < 0 || next * PAGE_SIZE >= total) return;
    setPage(next);
    loadEvents(currentSection, next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function handleRunAudit(target: AuditTarget): Promise<AuditFeedback> {
    const r = await apiRunAudit(target);
    if (r.status === 409) {
      const data = await r.json().catch(() => ({ detail: "Ya hay una en curso." }));
      return { kind: "busy", message: data.detail };
    }
    if (!r.ok) return { kind: "error", message: r.statusText };
    // Refrescos escalonados: la auditoría corre async en el backend.
    [3, 10, 30].forEach((s) =>
      setTimeout(() => {
        loadStatus();
        loadSections();
        loadEvents(sectionRef.current, pageRef.current);
      }, s * 1000),
    );
    return { kind: "ok", message: "Auditoría disparada" };
  }

  // Tras una acción que resuelve un evento: lo sacamos del listado y refrescamos
  // contadores/estado.
  const removeAndRefresh = useCallback(
    (id: number) => {
      setEvents((prev) => prev.filter((e) => e.id !== id));
      loadSections();
      loadStatus();
    },
    [loadSections, loadStatus],
  );

  const actions: EventActions = {
    onRetryPaco: async (id) => {
      await apiRetry(id);
      removeAndRefresh(id);
    },
    onConfirmDuplicate: async (id) => {
      const res = await apiConfirmDup(id);
      if (res.action === "no-op") {
        window.alert("El producto ya estaba deshabilitado en Vendure. Lo descartamos del listado.");
      }
      removeAndRefresh(id);
    },
    onConfirmDisableBx: async (id) => {
      const res = await apiConfirmBx(id);
      if (res.action === "no-op") {
        window.alert("El producto ya estaba deshabilitado en Vendure. Lo descartamos del listado.");
      }
      removeAndRefresh(id);
    },
    onDismiss: async (id) => {
      await apiDismiss(id);
      removeAndRefresh(id);
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
              loading={eventsLoading}
              error={eventsError}
              onRefresh={() => loadEvents(currentSection, page)}
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
