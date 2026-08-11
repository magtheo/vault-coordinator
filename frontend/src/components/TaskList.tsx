import { useState, useEffect, useCallback } from "react";
import type { Task, TasksResponse } from "../types";
import { getTasks, triggerSync } from "../api";

interface Props {
  onSchedule: (task: Task) => void;
}

export function TaskList({ onSchedule }: Props) {
  const [data, setData] = useState<TasksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

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

  const tasks = data?.tasks ?? [];
  const filtered = query
    ? tasks.filter(
        (t) =>
          t.title.toLowerCase().includes(query.toLowerCase()) ||
          t.alias.toLowerCase().includes(query.toLowerCase()),
      )
    : tasks;

  return (
    <div>
      <div className="sync-bar">
        {data &&
          Object.entries(data.sync_status).map(([source, status]) => (
            <span key={source}>
              <span
                className={`sync-dot ${status.consecutive_failures > 0 ? "stale" : "ok"}`}
              />
              {source}
            </span>
          ))}
        <button className="sync-btn" onClick={handleSync} style={{ marginLeft: "auto" }}>
          ↻ Sync
        </button>
      </div>

      <div className="search-bar">
        <input
          type="text"
          placeholder="Search tasks…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
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
              className="task-item"
              onClick={() => onSchedule(task)}
            >
              <div className="task-content">
                <div className="task-title">
                  <span className={`task-source ${task.source}`}>
                    {task.source === "vikunja" ? "V" : "G"}
                  </span>
                  {task.title}
                </div>
                <div className="task-meta">{task.alias}</div>
              </div>
              <span className="schedule-icon">⏰</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
