import { useState, useEffect, useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { View, Task } from "./types";
import { TaskList } from "./components/TaskList";
import { TaskDetail } from "./components/TaskDetail";
import { TodaySchedule } from "./components/TodaySchedule";
import { MachinesView } from "./components/MachinesView";
import { ProjectsView } from "./components/ProjectsView";
import { CalendarView } from "./components/CalendarView";
import { getTasks, getToday, getMachines, getProjectsOverview, getLabels } from "./api";

export default function App() {
  const [view, setView] = useState<View>("today");
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null);
  const [showMachines, setShowMachines] = useState(false);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [toast]);

  // Prefetch every tab's data shortly after first paint → switching tabs
  // is instant even on the first visit (cache serves, background revalidates).
  useEffect(() => {
    const id = window.setTimeout(() => {
      queryClient.prefetchQuery({ queryKey: ["tasks"], queryFn: () => getTasks() });
      queryClient.prefetchQuery({ queryKey: ["today"], queryFn: getToday });
      queryClient.prefetchQuery({ queryKey: ["labels"], queryFn: getLabels });
      queryClient.prefetchQuery({ queryKey: ["machines"], queryFn: getMachines });
      queryClient.prefetchQuery({ queryKey: ["projects-overview"], queryFn: getProjectsOverview });
    }, 300);
    return () => window.clearTimeout(id);
  }, [queryClient]);

  const handleToast = useCallback((msg: string, ok: boolean) => {
    setToast({ msg, ok });
  }, []);

  const handleMutated = useCallback(
    (msg: string, ok: boolean) => {
      setToast({ msg, ok });
      if (ok) queryClient.invalidateQueries();
    },
    [queryClient],
  );

  // Fleet chip status (header, all views)
  const machinesQuery = useQuery({
    queryKey: ["machines"],
    queryFn: getMachines,
    refetchInterval: 60_000,
  });
  const fleetOk = (machinesQuery.data?.machines ?? []).every((m) => m.reachable);

  return (
    <div className="app">
      <header className="top-bar">
        <span className="top-bar-brand">vault</span>
        <button
          className={`fleet-chip ${fleetOk ? "ok" : "warn"}`}
          onClick={() => setShowMachines(true)}
          title="Machines"
        >
          <span className="fleet-dot" />
          fleet
        </button>
      </header>

      {view === "today" && (
        <TodaySchedule
          onSelectTask={setSelectedTask}
          onToast={handleToast}
        />
      )}

      {view === "tasks" && (
        <TaskList
          onSelectTask={setSelectedTask}
          onToast={handleToast}
        />
      )}

      {view === "calendar" && <CalendarView />}

      {view === "projects" && (
        <ProjectsView onSelectTask={setSelectedTask} onToast={handleToast} />
      )}

      {/* Bottom tab bar */}
      <nav className="tab-bar">
        <button
          className={`tab ${view === "today" ? "active" : ""}`}
          onClick={() => setView("today")}
        >
          <span className="tab-icon">📅</span>
          Today
        </button>
        <button
          className={`tab ${view === "tasks" ? "active" : ""}`}
          onClick={() => setView("tasks")}
        >
          <span className="tab-icon">📋</span>
          Tasks
        </button>
        <button
          className={`tab ${view === "calendar" ? "active" : ""}`}
          onClick={() => setView("calendar")}
        >
          <span className="tab-icon">🗓️</span>
          Calendar
        </button>
        <button
          className={`tab ${view === "projects" ? "active" : ""}`}
          onClick={() => setView("projects")}
        >
          <span className="tab-icon">📂</span>
          Projects
        </button>
      </nav>

      {/* Machines overlay (demoted from tab) */}
      {showMachines && (
        <MachinesOverlay onClose={() => setShowMachines(false)} onToast={handleToast} />
      )}

      {/* Task detail sheet */}
      {selectedTask && (
        <TaskDetail
          task={selectedTask}
          onClose={() => setSelectedTask(null)}
          onMutated={handleMutated}
        />
      )}

      {/* Toast notification */}
      {toast && (
        <div className={`toast ${toast.ok ? "success" : "error"}`}>
          {toast.msg}
        </div>
      )}
    </div>
  );
}

function MachinesOverlay({ onClose, onToast }: { onClose: () => void; onToast: (msg: string, ok: boolean) => void }) {
  return (
    <>
      <div className="sheet-overlay" onClick={onClose} />
      <div className="sheet machines-sheet">
        <div className="sheet-handle" />
        <MachinesView onToast={onToast} />
      </div>
    </>
  );
}
