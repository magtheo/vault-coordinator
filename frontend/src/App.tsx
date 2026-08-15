import { useState, useEffect, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { View, Task } from "./types";
import { TaskList } from "./components/TaskList";
import { TaskDetail } from "./components/TaskDetail";
import { TodaySchedule } from "./components/TodaySchedule";
import { MachinesView } from "./components/MachinesView";
import { ProjectsView } from "./components/ProjectsView";
import { getTasks, getToday, getMachines, getProjectsOverview } from "./api";

export default function App() {
  const [view, setView] = useState<View>("today");
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null);
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
      queryClient.prefetchQuery({ queryKey: ["tasks"], queryFn: getTasks });
      queryClient.prefetchQuery({ queryKey: ["today"], queryFn: getToday });
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

  return (
    <div className="app">
      {view === "today" && (
        <TodaySchedule
          onSelectTask={setSelectedTask}
          onToast={handleToast}
        />
      )}

      {view === "tasks" && (
        <>
          <div className="app-header">
            <h1>📋 Tasks</h1>
          </div>
          <TaskList
            onSelectTask={setSelectedTask}
            onToast={handleToast}
          />
        </>
      )}

      {view === "projects" && (
        <ProjectsView onSelectTask={setSelectedTask} onToast={handleToast} />
      )}

      {view === "machines" && <MachinesView onToast={handleToast} />}

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
          className={`tab ${view === "projects" ? "active" : ""}`}
          onClick={() => setView("projects")}
        >
          <span className="tab-icon">📂</span>
          Projects
        </button>
        <button
          className={`tab ${view === "machines" ? "active" : ""}`}
          onClick={() => setView("machines")}
        >
          <span className="tab-icon">🖥️</span>
          Machines
        </button>
      </nav>

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
