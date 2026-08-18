import { useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Task, Project } from "../types";
import { editTask, completeTask, reopenTask, getProjects, getLabels, attachLabel, detachLabel } from "../api";
import { ScheduleSheet } from "./ScheduleSheet";

interface Props {
  task: Task;
  onClose: () => void;
  onMutated: (msg: string, ok: boolean) => void;
}

export function TaskDetail({ task, onClose, onMutated }: Props) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [editing, setEditing] = useState(false);
  const [showSchedule, setShowSchedule] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showLabelPicker, setShowLabelPicker] = useState(false);

  const labelsQuery = useQuery({
    queryKey: ["labels"],
    queryFn: getLabels,
    staleTime: 60_000,
    enabled: task.kind === "vikunja_task",
  });

  // Editable fields
  const [title, setTitle] = useState(task.title);
  const [priority, setPriority] = useState(task.priority ?? 0);
  const [dueDate, setDueDate] = useState(
    task.due_date && !task.due_date.startsWith("0001-")
      ? task.due_date.slice(0, 16)
      : "",
  );
  const [projectId, setProjectId] = useState<number | null>(() => {
    const raw = task.raw as Record<string, unknown> | null | undefined;
    return raw && typeof raw.project_id === "number" ? raw.project_id : null;
  });

  const caps = task.capabilities;

  useEffect(() => {
    if (caps.set_project || caps.edit) {
      getProjects().then((r) => setProjects(r.projects)).catch(() => {});
    }
  }, [caps.set_project, caps.edit]);

  async function handleSave() {
    setBusy(true);
    try {
      const fields: Record<string, unknown> = {};
      if (title !== task.title) fields.title = title;
      if (priority !== (task.priority ?? 0)) fields.priority = priority;
      if (dueDate !== (task.due_date?.slice(0, 16) ?? "")) {
        fields.due_date = dueDate ? dueDate + ":00Z" : "0001-01-01T00:00:00Z";
      }
      if (projectId !== null) fields.project_id = projectId;

      if (Object.keys(fields).length === 0) {
        setEditing(false);
        return;
      }

      await editTask(task.ref, fields);
      onMutated("Task updated", true);
      setEditing(false);
    } catch (e) {
      onMutated(e instanceof Error ? e.message : "Update failed", false);
    } finally {
      setBusy(false);
    }
  }

  async function handleComplete() {
    setBusy(true);
    try {
      await completeTask(task.ref);
      onMutated("Task completed", true);
      onClose();
    } catch (e) {
      onMutated(e instanceof Error ? e.message : "Failed", false);
    } finally {
      setBusy(false);
    }
  }

  async function handleReopen() {
    setBusy(true);
    try {
      await reopenTask(task.ref);
      onMutated("Task reopened", true);
      onClose();
    } catch (e) {
      onMutated(e instanceof Error ? e.message : "Failed", false);
    } finally {
      setBusy(false);
    }
  }

  async function handleAttach(labelId: number) {
    setBusy(true);
    try {
      await attachLabel(task.ref, labelId);
      onMutated("Label added", true);
    } catch (e) {
      onMutated(e instanceof Error ? e.message : "Failed", false);
    } finally {
      setBusy(false);
    }
  }

  async function handleDetach(labelId: number) {
    setBusy(true);
    try {
      await detachLabel(task.ref, labelId);
      onMutated("Label removed", true);
    } catch (e) {
      onMutated(e instanceof Error ? e.message : "Failed", false);
    } finally {
      setBusy(false);
    }
  }

  const vikunjaProjects = projects.filter((p) => p.source === "vikunja");

  return (
    <>
      <div className="sheet-overlay" onClick={onClose} />
      <div className="sheet">
        <div className="sheet-handle" />

        {/* Header */}
        <div className="detail-header">
          <span className={`task-source ${task.source}`}>
            {task.source === "vikunja" ? "V" : "G"}
          </span>
          {editing ? (
            <input
              className="detail-title-input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          ) : (
            <div className="sheet-title">{task.title}</div>
          )}
        </div>
        <div className="sheet-subtitle">
          {task.ref}
          {task.freshness !== "fresh" && (
            <span className={`freshness-badge ${task.freshness}`}> {task.freshness}</span>
          )}
          {task.scheduled && <span className="scheduled-badge"> scheduled</span>}
        </div>

        {/* Provenance for repo tasks */}
        {task.provenance && (
          <div className="provenance-info">
            {task.provenance.repo_name} · {task.provenance.branch?.slice(0, 20)}
            {task.provenance.dirty && <span className="dirty-badge"> dirty</span>}
          </div>
        )}

        {/* Edit fields */}
        {editing && (
          <>
            {caps.set_priority && (
              <>
                <div className="section-label">Priority</div>
                <div className="option-grid">
                  {[0, 1, 2, 3, 4, 5].map((p) => (
                    <button
                      key={p}
                      className={`option-btn ${priority === p ? "selected" : ""}`}
                      onClick={() => setPriority(p)}
                    >
                      {p}
                    </button>
                  ))}
                </div>
              </>
            )}

            {caps.set_deadline && (
              <>
                <div className="section-label">Due date</div>
                <input
                  type="datetime-local"
                  className="detail-input"
                  value={dueDate}
                  onChange={(e) => setDueDate(e.target.value)}
                />
              </>
            )}

            {caps.set_project && vikunjaProjects.length > 0 && (
              <>
                <div className="section-label">Project</div>
                <select
                  className="detail-input"
                  value={projectId ?? 1}
                  onChange={(e) => setProjectId(Number(e.target.value))}
                >
                  {vikunjaProjects.map((p) => (
                    <option key={p.ref} value={p.project_id ?? 1}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </>
            )}
          </>
        )}

        {/* Read-only info when not editing */}
        {!editing && task.kind === "vikunja_task" && (
          <div className="detail-info">
            {task.project_ref && (
              <div className="info-row">
                <span className="info-label">Project</span>
                <span className="info-value">
                  {task.project_ref.replace("vikunja:project:", "#").replace("repo:", "")}
                </span>
              </div>
            )}
            {task.priority != null && task.priority > 0 && (
              <div className="info-row">
                <span className="info-label">Priority</span>
                <span className="info-value">{task.priority}</span>
              </div>
            )}
            {task.due_date && !task.due_date.startsWith("0001-") && (
              <div className="info-row">
                <span className="info-label">Due</span>
                <span className="info-value">
                  {new Date(task.due_date).toLocaleDateString("en-US", {
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </span>
              </div>
            )}
          </div>
        )}

        {!editing && task.kind === "repo_task" && (
          <div className="detail-info">
            {task.project_ref && (
              <div className="info-row">
                <span className="info-label">Project</span>
                <span className="info-value">
                  {task.project_ref.replace("repo:", "")}
                </span>
              </div>
            )}
          </div>
        )}

        {/* Labels (Vikunja tasks) */}
        {task.kind === "vikunja_task" && !showSchedule && (
          <div className="detail-labels">
            <div className="section-label">
              Labels
              <button
                className="btn-secondary btn-xs"
                onClick={() => setShowLabelPicker((s) => !s)}
                disabled={busy}
              >
                {showLabelPicker ? "done" : "+"}
              </button>
            </div>
            <div className="label-chip-row">
              {(task.labels ?? []).length === 0 && !showLabelPicker && (
                <span className="task-meta">none</span>
              )}
              {(task.labels ?? []).map((l) => (
                <button
                  key={l.id}
                  className="label-chip static removable"
                  onClick={() => handleDetach(l.id)}
                  disabled={busy}
                  title="Remove label"
                >
                  #{l.title} ✕
                </button>
              ))}
            </div>
            {showLabelPicker && (
              <div className="label-picker">
                {(labelsQuery.data ?? [])
                  .filter((l) => !(task.labels ?? []).some((t) => t.id === l.id))
                  .map((l) => (
                    <button
                      key={l.id}
                      className="label-chip"
                      onClick={() => handleAttach(l.id)}
                      disabled={busy}
                    >
                      + #{l.title}
                    </button>
                  ))}
              </div>
            )}
          </div>
        )}

        {/* Actions */}
        <div className="detail-actions">
          {/* Schedule — always available for schedulable tasks */}
          {caps.schedule && !showSchedule && (
            <button
              className="btn-primary"
              onClick={() => setShowSchedule(true)}
              disabled={busy}
            >
              Schedule
            </button>
          )}

          {/* Edit / Save / Cancel */}
          {caps.edit && !editing && !showSchedule && (
            <button className="btn-secondary" onClick={() => setEditing(true)} disabled={busy}>
              Edit
            </button>
          )}
          {editing && (
            <>
              <button className="btn-primary" onClick={handleSave} disabled={busy}>
                {busy ? "Saving…" : "Save"}
              </button>
              <button className="btn-secondary" onClick={() => setEditing(false)} disabled={busy}>
                Cancel
              </button>
            </>
          )}

          {/* Complete / Reopen */}
          {caps.complete && task.source_status === "open" && !editing && !showSchedule && (
            <button className="btn-complete" onClick={handleComplete} disabled={busy}>
              Complete
            </button>
          )}
          {caps.reopen && task.source_status === "done" && !editing && !showSchedule && (
            <button className="btn-secondary" onClick={handleReopen} disabled={busy}>
              Reopen
            </button>
          )}

          {/* Close */}
          {!editing && !showSchedule && (
            <button className="btn-secondary" onClick={onClose} disabled={busy}>
              Close
            </button>
          )}
        </div>
      </div>

      {/* Schedule sheet nested inside detail */}
      {showSchedule && (
        <ScheduleSheet
          task={task}
          onClose={() => setShowSchedule(false)}
          onScheduled={(msg, ok) => {
            onMutated(msg, ok);
            if (ok) {
              setShowSchedule(false);
              onClose();
            }
          }}
        />
      )}
    </>
  );
}
