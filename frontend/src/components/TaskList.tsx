import { useState, useEffect, useCallback } from "react";
import type { Task, TasksResponse } from "../types";
import { getTasks, triggerSync, createTask } from "../api";

interface Props {
  onSelectTask: (task: Task) => void;
  onToast: (msg: string, ok: boolean) => void;
  refreshKey: number;
  onRefreshed: () => void;
}

export function TaskList({ onSelectTask, onToast, refreshKey, onRefreshed }: Props) {
  const [data, setData] = useState<TasksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [showCapture, setShowCapture] = useState(false);
  const [captureTitle, setCaptureTitle] = useState("");
  const [captureBusy, setCaptureBusy] = useState(false);
  const [hideScheduled, setHideScheduled] = useState(false);

  const load = useCallback(async () => {
    try {
      setError(null);
      const resp = await getTasks();
      setData(resp);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load tasks");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Reload when parent triggers refresh
  useEffect(() => {
    if (refreshKey > 0) {
      load().then(onRefreshed);
    }
  }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSync = async () => {
    setLoading(true);
    try {
      await triggerSync();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Sync failed");
      setLoading(false);
    }
  };

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

  const tasks = data?.tasks ?? [];
  let filtered = query
    ? tasks.filter(
        (t) =>
          t.title.toLowerCase().includes(query.toLowerCase()) ||
          t.ref.toLowerCase().includes(query.toLowerCase()),
      )
    : tasks;

  if (hideScheduled) {
    filtered = filtered.filter((t) => !t.scheduled);
  }

  return (
    <div>
      {/* Sync status bar */}
      <div className="sync-bar">
        {data &&
          Object.entries(data.sync_status).map(([source, status]) => (
            <span key={source} className="sync-source">
              <span className={`sync-dot ${status.freshness === "fresh" ? "ok" : status.freshness === "stale" ? "stale" : "error"}`} />
              {source}
            </span>
          ))}
        <button className="sync-btn" onClick={handleSync}>↻ Sync</button>
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

      {/* Search + filter */}
      <div className="search-bar">
        <input
          type="text"
          placeholder="Search tasks…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button
          className={`filter-btn ${hideScheduled ? "active" : ""}`}
          onClick={() => setHideScheduled(!hideScheduled)}
        >
          {hideScheduled ? "Unscheduled only" : "All"}
        </button>
      </div>

      {loading && <div className="loading">Loading tasks…</div>}
      {error && <div className="error-text">{error}</div>}

      {!loading && !error && (
        <div className="task-list">
          {filtered.length === 0 && (
            <div className="empty-state">
              {query ? "No matching tasks" : "No tasks found. Try syncing."}
            </div>
          )}
          {filtered.map((task) => (
            <div
              key={task.id}
              className={`task-item ${task.scheduled ? "scheduled" : ""}`}
              onClick={() => onSelectTask(task)}
            >
              <div className="task-content">
                <div className="task-title">
                  <span className={`task-source ${task.source}`}>
                    {task.source === "vikunja" ? "V" : "G"}
                  </span>
                  {task.title}
                  {task.scheduled && <span className="check-icon"> ✓</span>}
                </div>
                <div className="task-meta">
                  {task.project_ref?.replace("vikunja:project:", "#").replace("repo:", "")}
                  {task.priority != null && task.priority > 0 && ` · P${task.priority}`}
                  {task.freshness !== "fresh" && (
                    <span className={`freshness-text ${task.freshness}`}> · {task.freshness}</span>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
