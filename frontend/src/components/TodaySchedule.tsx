import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Task } from "../types";
import { getToday, getTasks } from "../api";
import { CaptureBox, lastUsedLabelIds } from "./CaptureBox";
import { useCompleteWithUndo, UndoBanner } from "./CompleteWithUndo";

interface Props {
  onSelectTask: (task: Task) => void;
  onToast: (msg: string, ok: boolean) => void;
}

export function TodaySchedule({ onSelectTask, onToast }: Props) {
  const queryClient = useQueryClient();
  const { toggle, isChecked, latest, now, undo } = useCompleteWithUndo(onToast);
  const defaults = lastUsedLabelIds();

  const todayQuery = useQuery({ queryKey: ["today"], queryFn: getToday });
  const tasksQuery = useQuery({ queryKey: ["tasks"], queryFn: () => getTasks() });

  const today = todayQuery.data;
  const loading = todayQuery.isPending || tasksQuery.isPending;
  const error =
    todayQuery.error instanceof Error
      ? todayQuery.error.message
      : tasksQuery.error instanceof Error
        ? tasksQuery.error.message
        : null;

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["today"] });
    queryClient.invalidateQueries({ queryKey: ["tasks"] });
  };

  const events = (today?.events ?? []).filter((e) => e.start);
  const dateLabel = today
    ? new Date(today.date + "T00:00:00").toLocaleDateString("en-US", {
        weekday: "long",
        month: "short",
        day: "numeric",
      })
    : "";

  // Now / next event
  const nowMs = Date.now();
  const nextEvent = events.find((e) => new Date(e.end ?? e.start!).getTime() > nowMs);
  const currentEvent = events.find(
    (e) =>
      new Date(e.start!).getTime() <= nowMs &&
      new Date(e.end ?? e.start!).getTime() > nowMs,
  );

  // Strictly due/overdue Vikunja tasks (in-grace rows stay for undo)
  const dueTasks = (tasksQuery.data?.tasks ?? []).filter((t) => {
    if (t.kind !== "vikunja_task") return false;
    if (t.source_status === "done" && !isChecked(t.ref)) return false;
    if (!t.due_date || t.due_date.startsWith("0001-")) return false;
    return new Date(t.due_date).getTime() <= nowMs + 36e5 * 24; // due today or overdue
  });

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
        <button className="sync-btn" onClick={refresh}>↻</button>
      </div>

      <CaptureBox onToast={onToast} defaultLabelIds={defaults} />

      {loading && <div className="loading">Loading…</div>}
      {error && <div className="error-text">{error}</div>}

      {!loading && !error && (
        <>
          {(currentEvent || nextEvent) && (
            <div className="today-section">
              <div className="section-label">{currentEvent ? "Now" : "Next"}</div>
              <div className="next-event-card">
                <div className="event-time">
                  {formatTime((currentEvent ?? nextEvent)!.start)} –{" "}
                  {formatTime((currentEvent ?? nextEvent)!.end)}
                </div>
                <div className="event-title">{(currentEvent ?? nextEvent)!.summary}</div>
                {(currentEvent ?? nextEvent)!.linked_alias && (
                  <div className="event-link">
                    🔗 {(currentEvent ?? nextEvent)!.linked_title || (currentEvent ?? nextEvent)!.linked_alias}
                  </div>
                )}
              </div>
            </div>
          )}

          {events.length > 0 && (
            <div className="today-section">
              <div className="section-label">Schedule</div>
              {events.map((event) => (
                <div key={event.uid} className="today-event">
                  <div className="event-time">
                    {formatTime(event.start)} – {formatTime(event.end)}
                  </div>
                  <div className="event-title">{event.summary}</div>
                  {event.linked_alias && (
                    <div className="event-link">🔗 {event.linked_title || event.linked_alias}</div>
                  )}
                </div>
              ))}
            </div>
          )}

          <div className="today-section">
            <div className="section-label">
              Tasks due {dueTasks.length > 0 && `(${dueTasks.length})`}
            </div>
            {dueTasks.length === 0 && (
              <div className="empty-state">Nothing due. Enjoy.</div>
            )}
            {dueTasks.map((task) => {
              const overdue =
                task.due_date && new Date(task.due_date).getTime() < nowMs - 36e5 * 24;
              const checked = isChecked(task.ref);
              return (
                <div key={task.id} className={`task-item ${checked ? "completing" : ""}`}>
                  <button
                    className="task-check"
                    onClick={() => toggle(task)}
                    title={checked ? "Undo" : "Complete"}
                  >
                    {checked ? "✓" : ""}
                  </button>
                  <div className="task-content" onClick={() => onSelectTask(task)}>
                    <div className="task-title">{task.title}</div>
                    <div className="task-meta">
                      {overdue && <span className="overdue-chip">overdue</span>}
                      {task.due_date && (
                        <span>
                          {" "}
                          {new Date(task.due_date).toLocaleDateString("en-US", {
                            month: "short",
                            day: "numeric",
                          })}
                        </span>
                      )}
                      {(task.labels ?? []).map((l) => (
                        <span key={l.id} className="label-chip static">#{l.title}</span>
                      ))}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}

      <UndoBanner entry={latest} now={now} onUndo={() => latest && undo(latest.task.ref)} />
    </div>
  );
}
