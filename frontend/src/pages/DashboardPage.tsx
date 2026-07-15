import { useEffect, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import Header from "../components/Header";
import Sidebar, { AuditFeedback } from "../components/Sidebar";
import MetricsRow from "../components/MetricsRow";
import EventsView from "../components/EventsView";
import SettingsView from "../components/SettingsView";
import HealthView from "../components/HealthView";
import HistoryModal from "../components/HistoryModal";
import { EventActions } from "../components/EventCard";
import {
  bulkConfirmDuplicates as apiBulkConfirm,
  confirmDisableBx as apiConfirmBx,
  confirmDuplicate as apiConfirmDup,
  dismissEvent as apiDismiss,
  dismissSection as apiDismissSection,
  getEvents,
  getSections,
  getStatus,
  logout as apiLogout,
  retryPaco as apiRetry,
  runAudit as apiRunAudit,
  setComment as apiSetComment,
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
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [historyProductId, setHistoryProductId] = useState<string | null>(null);

  // Debounce del buscador: no dispara un request por tecla.
  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedSearch(search);
      setPage(0);
    }, 350);
    return () => clearTimeout(t);
  }, [search]);

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
  const isListSection = currentSection !== "settings" && currentSection !== "salud";
  const eventsQ = useQuery({
    queryKey: ["events", currentSection, page, debouncedSearch],
    queryFn: () => getEvents(currentSection, page * PAGE_SIZE, PAGE_SIZE, debouncedSearch),
    enabled: isListSection,
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
    setSearch("");
    setDebouncedSearch("");
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
    onComment: async (id, note) => {
      await apiSetComment(id, note);
      // Refresca la lista para mostrar el comentario (no saca la card).
      qc.invalidateQueries({ queryKey: ["events"] });
    },
    onViewHistory: (productId) => setHistoryProductId(productId),
  };

  async function handleArchiveAll() {
    if (
      !window.confirm(
        `¿Archivar todos los eventos de "${label}"? No borra nada en Vendure, solo los saca del listado.`,
      )
    )
      return;
    try {
      await apiDismissSection(currentSection);
      invalidateAll();
    } catch (err) {
      window.alert("No se pudo archivar: " + (err instanceof Error ? err.message : String(err)));
    }
  }

  async function handleBulkConfirmDuplicates() {
    try {
      const dry = await apiBulkConfirm(0.99, false);
      const n = dry.would_disable ?? 0;
      if (n === 0) {
        window.alert("No hay duplicados con confianza ≥99% para confirmar.");
        return;
      }
      if (
        !window.confirm(
          `¿Deshabilitar en Vendure ${n} duplicados con confianza ≥99%? Se puede revertir desde el historial.`,
        )
      )
        return;
      const res = await apiBulkConfirm(0.99, true);
      window.alert(
        `Deshabilitados: ${res.disabled ?? 0} · ya estaban off: ${res.skipped_already_disabled ?? 0} · fallidos: ${res.failed ?? 0}`,
      );
      invalidateAll();
    } catch (err) {
      window.alert("Error: " + (err instanceof Error ? err.message : String(err)));
    }
  }

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
          ) : currentSection === "salud" ? (
            <HealthView />
          ) : (
            <EventsView
              section={currentSection}
              label={label}
              events={events}
              total={total}
              page={page}
              loading={eventsQ.isPending}
              error={eventsQ.error instanceof Error ? eventsQ.error.message : null}
              search={search}
              onSearchChange={setSearch}
              onRefresh={() => eventsQ.refetch()}
              onChangePage={changePage}
              onArchiveAll={handleArchiveAll}
              onBulkConfirmDuplicates={
                currentSection === "duplicates" ? handleBulkConfirmDuplicates : undefined
              }
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
