import { useState, useEffect } from "react";
import type { View, Task } from "./types";
import { TaskList } from "./components/TaskList";
import { ScheduleSheet } from "./components/ScheduleSheet";
import { TodaySchedule } from "./components/TodaySchedule";

export default function App() {
  const [view, setView] = useState<View>("tasks");
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null);

  // Clear toast after 3 seconds
  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [toast]);

  const handleScheduled = (msg: string, ok: boolean) => {
    setToast({ msg, ok });
  };

  return (
    <div className="app">
      {view === "tasks" && (
        <>
          <div className="app-header">
            <h1>📋 Tasks</h1>
          </div>
          <TaskList onSchedule={(task) => setSelectedTask(task)} />
        </>
      )}

      {view === "today" && <TodaySchedule />}

      {/* Bottom tab bar */}
      <nav className="tab-bar">
        <button
          className={`tab ${view === "tasks" ? "active" : ""}`}
          onClick={() => setView("tasks")}
        >
          <span className="tab-icon">📋</span>
          Tasks
        </button>
        <button
          className={`tab ${view === "today" ? "active" : ""}`}
          onClick={() => setView("today")}
        >
          <span className="tab-icon">📅</span>
          Today
        </button>
      </nav>

      {/* Schedule bottom sheet */}
      {selectedTask && (
        <ScheduleSheet
          task={selectedTask}
          onClose={() => setSelectedTask(null)}
          onScheduled={handleScheduled}
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
