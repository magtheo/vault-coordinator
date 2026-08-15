import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ProjectOverview, ProjectTask } from "../types";
import { getProjectsOverview, stopMachineJob } from "../api";
import type { Task } from "../types";

interface Props {
  onSelectTask: (task: Task) => void;
  onToast: (msg: string, ok: boolean) => void;
}

export function ProjectsView({ onSelectTask, onToast }: Props) {
  const [open, setOpen] = useState<ProjectOverview | null>(null);
  const { data, error: qError, isPending } = useQuery({
    queryKey: ["projects-overview"],
    queryFn: getProjectsOverview,
  });

  const error = qError instanceof Error ? qError.message : null;
  const projects = data?.projects ?? [];

  return (
    <>
      <div className="app-header">
        <h1>📂 Projects</h1>
      </div>
      {isPending && <div className="loading">Loading…</div>}
      {error && <div className="error-text">{error}</div>}
      <div className="machine-list">
        {projects.map((p) => (
          <div
            key={`${p.source}:${p.name}`}
            className="machine-card project-card"
            onClick={() => setOpen(p)}
          >
            <div className="machine-head">
              <span className="machine-name">{p.name}</span>
              <span className="machine-host">
                {p.host ? `${p.host} · ` : ""}{p.source}
              </span>
            </div>
            <div className="machine-sessions">
              {p.open_tasks > 0 && (
                <span className="machine-chip">📋 {p.open_tasks} open</span>
              )}
              {p.done_tasks > 0 && (
                <span className="machine-chip">✓ {p.done_tasks} done</span>
              )}
              {p.sessions.map((s) => (
                <span key={s.host + s.name} className="machine-chip">
                  ▶ {s.name} · {s.host}
                </span>
              ))}
              {p.jobs.map((j) => (
                <span key={s_key(j)} className="machine-chip">
                  ⚙ {j.name} · {j.state}
                </span>
              ))}
              {p.open_tasks === 0 && p.sessions.length === 0 && p.jobs.length === 0 && (
                <span className="machine-empty">nothing active</span>
              )}
            </div>
          </div>
        ))}
      </div>

      {open && (
        <ProjectSheet
          project={open}
          onClose={() => setOpen(null)}
          onSelectTask={onSelectTask}
          onToast={onToast}
        />
      )}
    </>
  );
}

function s_key(j: { host: string; name: string }) {
  return j.host + ":" + j.name;
}

function ProjectSheet({
  project,
  onClose,
  onSelectTask,
  onToast,
}: {
  project: ProjectOverview;
  onClose: () => void;
  onSelectTask: (task: Task) => void;
  onToast: (msg: string, ok: boolean) => void;
}) {
  const toTask = (t: ProjectTask): Task => ({
    id: t.alias,
    ref: t.alias,
    kind: t.kind,
    title: t.title,
    source: t.kind === "vikunja_task" ? "vikunja" : "git",
    source_status: null,
    project_ref: null,
    freshness: "fresh",
    scheduled: false,
    capabilities: {
      edit: t.kind === "vikunja_task",
      complete: t.kind === "vikunja_task",
      reopen: t.kind === "vikunja_task",
      schedule: true,
      set_deadline: t.kind === "vikunja_task",
      set_priority: t.kind === "vikunja_task",
      set_project: t.kind === "vikunja_task",
      open_source: t.kind !== "vikunja_task",
    },
    priority: null,
    due_date: null,
    is_favorite: null,
    description: null,
    provenance: null,
    last_synced: "",
  });

  const handleStop = async (host: string, job: string) => {
    try {
      await stopMachineJob(host, job);
      onToast(`Stopped ${job}`, true);
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Stop failed", false);
    }
  };

  return (
    <>
      <div className="sheet-overlay" onClick={onClose} />
      <div className="sheet project-sheet">
        <div className="sheet-handle" onClick={onClose} />
        <div className="sheet-title">{project.name}</div>
        <div className="sheet-subtitle">
          {project.host ? `${project.host} · ` : ""}{project.source}
        </div>

        {project.jobs.length > 0 && (
          <>
            <div className="section-label">Jobs</div>
            {project.jobs.map((j) => (
              <div key={s_key(j)} className="machine-job">
                <div className="task-content">
                  <div className="task-title">
                    {j.state === "active" ? "▶" : j.state === "failed" ? "✕" : "○"} {j.name} · {j.host}
                  </div>
                  <div className="task-meta">{j.command.slice(0, 50)}</div>
                </div>
                <button className="btn-secondary" onClick={() => handleStop(j.host, j.name)}>
                  Stop
                </button>
              </div>
            ))}
          </>
        )}

        {project.sessions.length > 0 && (
          <>
            <div className="section-label">Sessions</div>
            <div className="machine-sessions">
              {project.sessions.map((s) => (
                <span key={s.host + s.name} className="machine-chip">
                  ▶ {s.name} · {s.windows}w · {s.host}
                </span>
              ))}
            </div>
          </>
        )}

        <div className="section-label">
          Tasks · {project.open_tasks} open, {project.done_tasks} done
        </div>
        <div className="task-list">
          {project.tasks.length === 0 && (
            <div className="machine-empty">no open tasks</div>
          )}
          {project.tasks.map((t) => (
            <div
              key={t.alias}
              className="task-item"
              onClick={() => {
                onClose();
                onSelectTask(toTask(t));
              }}
            >
              <div className="task-content">
                <div className="task-title">
                  <span className={`task-source ${t.kind === "vikunja_task" ? "vikunja" : "git"}`}>
                    {t.kind === "vikunja_task" ? "V" : "G"}
                  </span>
                  {t.title}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}
