import { useState, useEffect, useCallback } from "react";
import type { View, Task } from "./types";
import { TaskList } from "./components/TaskList";
import { TaskDetail } from "./components/TaskDetail";
import { TodaySchedule } from "./components/TodaySchedule";
import { MachinesView } from "./components/MachinesView";

export default function App() {
  const [view, setView] = useState<View>("today");
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [toast]);

  const handleToast = useCallback((msg: string, ok: boolean) => {
    setToast({ msg, ok });
  }, []);

  const handleMutated = useCallback((msg: string, ok: boolean) => {
    setToast({ msg, ok });
    if (ok) setRefreshKey((k) => k + 1);
  }, []);

  return (
    <div className="app">
      {view === "today" && (
        <TodaySchedule
          onSelectTask={setSelectedTask}
          onToast={handleToast}
          refreshKey={refreshKey}
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
            refreshKey={refreshKey}
            onRefreshed={() => {}}
          />
        </>
      )}

      {view === "machines" && (
        <MachinesView onToast={handleToast} refreshKey={refreshKey} />
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
