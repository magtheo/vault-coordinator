import { useState, useEffect, useCallback } from "react";
import type { TodayResponse, TasksResponse, Task } from "../types";
import { getToday, getTasks, createTask } from "../api";

interface Props {
  onSelectTask: (task: Task) => void;
  onToast: (msg: string, ok: boolean) => void;
  refreshKey: number;
}

export function TodaySchedule({ onSelectTask, onToast, refreshKey }: Props) {
  const [today, setToday] = useState<TodayResponse | null>(null);
  const [tasksData, setTasksData] = useState<TasksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCapture, setShowCapture] = useState(false);
  const [captureTitle, setCaptureTitle] = useState("");
  const [captureBusy, setCaptureBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setError(null);
      const [todayResp, tasksResp] = await Promise.all([getToday(), getTasks()]);
      setToday(todayResp);
      setTasksData(tasksResp);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const handleQuickCapture = async () => {
    if (!captureTitle.trim()) return;
    setCaptureBusy(true);
    try {
      await createTask({ title: captureTitle.trim() });
      onToast("Task created", true);
      setCaptureTitle("");
      setShowCapture(false);
      await load();
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Capture failed", false);
    } finally {
      setCaptureBusy(false);
    }
  };

  const events = today?.events ?? [];
  const dateLabel = today
    ? new Date(today.date + "T00:00:00").toLocaleDateString("en-US", {
        weekday: "long",
        month: "short",
        day: "numeric",
      })
    : "";

  // Unscheduled tasks: overdue/due-today Vikunja tasks + repo tasks in progress
  const allTasks = tasksData?.tasks ?? [];
  const unscheduled = allTasks.filter((t) => {
    if (t.scheduled) return false;
    if (t.source_status === "done") return false;
    // Vikunja: show overdue or due today
    if (t.kind === "vikunja_task") {
      if (!t.due_date || t.due_date.startsWith("0001-")) {
        // No due date — include it (it's in the inbox)
        return true;
      }
      const due = new Date(t.due_date);
      const today_end = new Date();
      today_end.setHours(23, 59, 59);
      return due <= today_end;
    }
    // Repo tasks: show only "in progress" (not done, has description or is active)
    // For simplicity, show first 5 unscheduled repo tasks
    return false;
  }).slice(0, 8);

  function formatTime(iso: string | null): string {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleTimeString("en-US", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
    } catch {
      return iso;
    }
  }

  return (
    <div>
      <div className="app-header">
        <h1>📅 {dateLabel}</h1>
        <button className="sync-btn" onClick={load}>↻</button>
      </div>

      {/* Quick capture */}
      {showCapture ? (
        <div className="capture-bar">
          <input
            type="text"
            placeholder="Task title…"
            value={captureTitle}
            onChange={(e) => setCaptureTitle(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleQuickCapture()}
            autoFocus
            className="capture-input"
          />
          <button className="btn-primary btn-sm" onClick={handleQuickCapture} disabled={captureBusy}>
            {captureBusy ? "…" : "Add"}
          </button>
          <button className="btn-secondary btn-sm" onClick={() => { setShowCapture(false); setCaptureTitle(""); }}>
            ✕
          </button>
        </div>
      ) : (
        <div className="quick-capture-trigger" onClick={() => setShowCapture(true)}>
          + Quick capture
        </div>
      )}

      {loading && <div className="loading">Loading…</div>}
      {error && <div className="error-text">{error}</div>}

      {!loading && !error && (
        <>
          {/* Today's time blocks */}
          {events.length > 0 && (
            <div className="today-section">
              <div className="section-label">Scheduled blocks</div>
              {events.map((event) => (
                <div key={event.uid} className="today-event">
                  <div className="event-time">
                    {formatTime(event.start)} – {formatTime(event.end)}
                  </div>
                  <div className="event-title">{event.summary}</div>
                  {event.linked_alias && (
                    <div className="event-link">
                      🔗 {event.linked_title || event.linked_alias}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Unscheduled tasks needing attention */}
          {unscheduled.length > 0 && (
            <div className="today-section">
              <div className="section-label">Needs scheduling</div>
              {unscheduled.map((task) => (
                <div
                  key={task.id}
                  className="task-item"
                  onClick={() => onSelectTask(task)}
                >
                  <div className="task-content">
                    <div className="task-title">
                      <span className={`task-source ${task.source}`}>
                        {task.source === "vikunja" ? "V" : "G"}
                      </span>
                      {task.title}
                    </div>
                    <div className="task-meta">
                      {task.due_date && !task.due_date.startsWith("0001-") && "⚠ overdue · "}
                      {task.project_ref?.replace("vikunja:project:", "#").replace("repo:", "")}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {events.length === 0 && unscheduled.length === 0 && (
            <div className="empty-state">
              Nothing scheduled today.
              <br />
              Go to Tasks to schedule work.
            </div>
          )}
        </>
      )}
    </div>
  );
}
