import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Task, Label } from "../types";
import { getTasks } from "../api";
import { CaptureBox, lastUsedLabelIds } from "./CaptureBox";
import { useCompleteWithUndo, UndoBanner } from "./CompleteWithUndo";

interface Props {
  onSelectTask: (task: Task) => void;
  onToast: (msg: string, ok: boolean) => void;
}

const DAY = 36e5 * 24;

function dueGroup(t: Task, now: number): string {
  if (!t.due_date || t.due_date.startsWith("0001-")) return "No date";
  const due = new Date(t.due_date).getTime();
  if (due < now - DAY) return "Overdue";
  if (due <= now + DAY) return "Today";
  if (due <= now + DAY * 7) return "This week";
  return "Later";
}

const GROUP_ORDER = ["Overdue", "Today", "This week", "Later", "No date"];

export function TaskList({ onSelectTask, onToast }: Props) {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [filterLabel, setFilterLabel] = useState<number | null>(null);
  const { toggle, isChecked, latest, now: tickNow, undo } = useCompleteWithUndo(onToast);

  const { data, error: qError, isPending } = useQuery({
    queryKey: ["tasks"],
    queryFn: () => getTasks(),
  });

  const loading = isPending;
  const error = qError instanceof Error ? qError.message : null;

  // Vikunja tasks only — the authoritative task store (repo items live in Projects)
  const tasks = (data?.tasks ?? []).filter((t) => t.kind === "vikunja_task");

  const labelUsage = useMemo(() => {
    const counts = new Map<number, number>();
    for (const t of tasks) {
      for (const l of t.labels ?? []) {
        counts.set(l.id, (counts.get(l.id) ?? 0) + 1);
      }
    }
    return counts;
  }, [tasks]);

  const allLabels: Label[] = useMemo(() => {
    const seen = new Map<number, Label>();
    for (const t of tasks) {
      for (const l of t.labels ?? []) seen.set(l.id, l);
    }
    // Labels from the vocabulary endpoint arrive via CaptureBox query cache
    const cached = queryClient.getQueryData<Label[]>(["labels"]);
    for (const l of cached ?? []) seen.set(l.id, l);
    return [...seen.values()].sort(
      (a, b) => (labelUsage.get(b.id) ?? 0) - (labelUsage.get(a.id) ?? 0) || a.title.localeCompare(b.title),
    );
  }, [tasks, labelUsage, queryClient]);

  let filtered = tasks.filter((t) => t.source_status !== "done");

  if (query) {
    const q = query.toLowerCase();
    filtered = filtered.filter((t) => t.title.toLowerCase().includes(q));
  }
  if (filterLabel !== null) {
    filtered = filtered.filter((t) => (t.labels ?? []).some((l) => l.id === filterLabel));
  }

  const now = Date.now();
  const groups = useMemo(() => {
    const g = new Map<string, Task[]>();
    for (const t of filtered) {
      const key = dueGroup(t, now);
      const list = g.get(key) ?? [];
      list.push(t);
      g.set(key, list);
    }
    for (const list of g.values()) {
      list.sort((a, b) => {
        const ad = a.due_date && !a.due_date.startsWith("0001-") ? new Date(a.due_date).getTime() : Infinity;
        const bd = b.due_date && !b.due_date.startsWith("0001-") ? new Date(b.due_date).getTime() : Infinity;
        return ad - bd;
      });
    }
    return g;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtered]);

  return (
    <div>
      <div className="app-header">
        <h1>📋 Tasks</h1>
        <div className="task-count">{filtered.length}</div>
      </div>

      <CaptureBox onToast={onToast} defaultLabelIds={lastUsedLabelIds()} />

      {/* Filter chips */}
      <div className="filter-chip-row">
        <button
          className={`filter-chip ${filterLabel === null ? "active" : ""}`}
          onClick={() => setFilterLabel(null)}
        >
          All
        </button>
        {allLabels.map((l) => (
          <button
            key={l.id}
            className={`filter-chip ${filterLabel === l.id ? "active" : ""}`}
            onClick={() => setFilterLabel(filterLabel === l.id ? null : l.id)}
          >
            #{l.title}
          </button>
        ))}
      </div>

      {/* Search */}
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
              {query || filterLabel !== null ? "No matching tasks" : "No open tasks."}
            </div>
          )}
          {GROUP_ORDER.map((group) => {
            const list = groups.get(group);
            if (!list || list.length === 0) return null;
            return (
              <div key={group} className="task-group">
                <div className={`section-label ${group === "Overdue" ? "overdue-label" : ""}`}>
                  {group} ({list.length})
                </div>
                {list.map((task) => {
                  const checked = isChecked(task.ref);
                  return (
                  <div
                    key={task.id}
                    className={`task-item ${task.scheduled ? "scheduled" : ""} ${checked ? "completing" : ""}`}
                  >
                    <button
                      className="task-check"
                      onClick={() => toggle(task)}
                      title={checked ? "Undo" : "Complete"}
                    >
                      {checked ? "✓" : ""}
                    </button>
                    <div className="task-content" onClick={() => onSelectTask(task)}>
                      <div className="task-title">
                        {task.title}
                        {task.scheduled && <span className="check-icon"> ✓</span>}
                      </div>
                      <div className="task-meta">
                        {task.due_date && !task.due_date.startsWith("0001-") && (
                          <span>
                            {new Date(task.due_date).toLocaleDateString("en-US", {
                              month: "short",
                              day: "numeric",
                            })}{" "}
                          </span>
                        )}
                        {task.priority != null && task.priority > 0 && `P${task.priority} `}
                        {(task.labels ?? []).map((l) => (
                          <span key={l.id} className="label-chip static">#{l.title}</span>
                        ))}
                      </div>
                    </div>
                  </div>
                  );
                })}
              </div>
            );
          })}
        </div>
      )}

      <UndoBanner entry={latest} now={tickNow} onUndo={() => latest && undo(latest.task.ref)} />
    </div>
  );
}
