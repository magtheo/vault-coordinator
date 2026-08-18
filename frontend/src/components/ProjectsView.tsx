import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Markdown from "react-markdown";
import type { ProjectOverview, ProjectTask, JobLogResponse } from "../types";
import type { Task } from "../types";
import {
  getProjectsOverview,
  getAiSummary,
  stopMachineJob,
  getJobLog,
  completeTask,
  runProjectCommand,
  getTasks,
} from "../api";

// Slug rule mirrors src/adapters/vault.py (incl. override)
const SLUG_OVERRIDES: Record<string, string> = { "hermes dual-bot": "hermes" };
function slugify(name: string): string {
  const key = name.trim().toLowerCase();
  return SLUG_OVERRIDES[key] ?? key.replace(/\s+/g, "-");
}

interface Props {
  onSelectTask: (task: Task) => void;
  onToast: (msg: string, ok: boolean) => void;
}

export function ProjectsView({ onSelectTask, onToast }: Props) {
  const queryClient = useQueryClient();
  // All Vikunja tasks — filtered per project by tag in the detail sheet
  const allTasksQuery = useQuery({ queryKey: ["tasks"], queryFn: () => getTasks() });
  const [open, setOpen] = useState<ProjectOverview | null>(null);
  const [logJob, setLogJob] = useState<{ host: string; name: string } | null>(null);

  const { data, error: qError, isPending } = useQuery({
    queryKey: ["projects-overview"],
    queryFn: getProjectsOverview,
    refetchInterval: 30_000,
  });

  const ai = useQuery({
    queryKey: ["ai-summary"],
    queryFn: () => getAiSummary(false),
    staleTime: 5 * 60_000,
  });

  const error = qError instanceof Error ? qError.message : null;
  const projects = data?.projects ?? [];

  const refreshAll = async () => {
    await getAiSummary(true).catch(() => {});
    queryClient.invalidateQueries({ queryKey: ["ai-summary"] });
    queryClient.invalidateQueries({ queryKey: ["projects-overview"] });
  };

  const handleStop = async (host: string, job: string) => {
    try {
      await stopMachineJob(host, job);
      onToast(`Stopped ${job}`, true);
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Stop failed", false);
    }
  };

  const handleRun = async (host: string, project: string, key: string) => {
    try {
      await runProjectCommand(host, project, key);
      onToast(`Started ${project}-${key} on ${host}`, true);
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Run failed", false);
    }
  };

  const handleComplete = async (t: ProjectTask) => {
    try {
      await completeTask(t.alias);
      onToast("Task completed", true);
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Complete failed", false);
    }
  };

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
    labels: null,
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
    due_date: t.due_date ?? null,
    is_favorite: null,
    description: null,
    provenance: null,
    last_synced: "",
  });

  const fmtAge = (min: number | null | undefined) => {
    if (min == null) return "";
    if (min < 60) return `${min}m`;
    if (min < 48 * 60) return `${Math.round(min / 60)}h`;
    return `${Math.round(min / (24 * 60))}d`;
  };

  return (
    <>
      <div className="app-header">
        <h1>📂 Projects</h1>
        <button className="btn-secondary" onClick={refreshAll}>↻</button>
      </div>

      {/* AI insights — advisory only */}
      <div className="ai-card">
        <div className="ai-head">
          <span>🤖 Insights</span>
          <span className="machine-host">
            {ai.data ? (ai.data.cached ? "cached" : "fresh") : "…"}
          </span>
        </div>
        {ai.isPending && <div className="machine-empty">Generating summary…</div>}
        {ai.error && (
          <div className="machine-error">
            {ai.error instanceof Error ? ai.error.message : "summary unavailable"}
          </div>
        )}
        {ai.data && (
          <div className="ai-body">
            <Markdown>{ai.data.summary}</Markdown>
          </div>
        )}
      </div>

      {isPending && <div className="loading">Loading…</div>}
      {error && <div className="error-text">{error}</div>}

      <div className="machine-list">
        {projects.map((p) => {
          const a = p.attention;
          const hasAttention = a.score > 0;
          return (
            <div
              key={`${p.source}:${p.name}`}
              className={`machine-card project-card ${hasAttention ? "needs-attention" : ""}`}
              onClick={() => setOpen(p)}
            >
              <div className="machine-head">
                <span className="machine-name">{p.name}</span>
                <span className="machine-host">
                  {p.host ? `${p.host} · ` : ""}{p.source}
                </span>
              </div>

              {hasAttention && (
                <div className="attention-row">
                  {a.host_down && <span className="attention-chip">⚠ host unreachable</span>}
                  {a.failed_jobs > 0 && (
                    <span className="attention-chip">✕ {a.failed_jobs} failed job{a.failed_jobs > 1 ? "s" : ""}</span>
                  )}
                  {a.overdue_tasks > 0 && (
                    <span className="attention-chip">⚠ {a.overdue_tasks} overdue</span>
                  )}
                </div>
              )}

              <div className="machine-sessions">
                {p.git?.branch && (
                  <span className="machine-chip">
                    ⎇ {p.git.branch}
                    {p.git.dirty ? " •" : ""}
                    {p.git.unpushed ? ` ↑${p.git.unpushed}` : ""}
                    {p.git.commit_age_min != null ? ` · ${fmtAge(p.git.commit_age_min)}` : ""}
                  </span>
                )}
                {p.open_tasks > 0 && (
                  <span className="machine-chip">📋 {p.open_tasks} open</span>
                )}
                {p.sessions.map((s) => (
                  <span key={s.host + s.name} className="machine-chip">
                    ▶ {s.name} · {s.windows}w · {s.host}
                  </span>
                ))}
                {p.jobs.map((j) => (
                  <span key={j.host + j.name} className="machine-chip">
                    {j.state === "failed" ? "✕" : "⚙"} {j.name} · {j.host}
                  </span>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      {/* Project detail sheet */}
      {open && (
        <div className="sheet-overlay" onClick={() => setOpen(null)} />
      )}
      {open && (
        <div className="sheet project-sheet">
          <div className="sheet-handle" onClick={() => setOpen(null)} />
          <div className="sheet-title">{open.name}</div>
          <div className="sheet-subtitle">
            {open.host ? `${open.host} · ` : ""}{open.source}
            {open.git?.branch && ` · ⎇ ${open.git.branch}${open.git.dirty ? " (dirty)" : ""}`}
          </div>
          {open.git?.last_subject && (
            <div className="machine-empty">
              last: {open.git.last_subject} ({fmtAge(open.git.commit_age_min)} ago)
            </div>
          )}

          {open.commands.length > 0 && (
            <>
              <div className="section-label">Commands</div>
              <div className="machine-sessions" style={{ marginBottom: 8 }}>
                {open.commands.map((c) => (
                  <button
                    key={c.host + c.key}
                    className="btn-secondary btn-sm"
                    onClick={() => handleRun(c.host, open.name, c.key)}
                  >
                    ▶ {c.key} · {c.host}
                  </button>
                ))}
              </div>
            </>
          )}

          {open.jobs.length > 0 && (
            <>
              <div className="section-label">Jobs</div>
              {open.jobs.map((j) => (
                <div key={j.host + j.name} className="machine-job">
                  <div className="task-content">
                    <div className="task-title">
                      {j.state === "failed" ? "✕" : j.state === "active" ? "▶" : "○"} {j.name} · {j.host}
                      {j.exit_code != null && j.exit_code !== 0 ? ` (exit ${j.exit_code})` : ""}
                    </div>
                    <div className="task-meta">{j.command.slice(0, 50)}</div>
                  </div>
                  <button className="btn-secondary" onClick={() => setLogJob({ host: j.host, name: j.name })}>
                    Log
                  </button>
                  <button className="btn-secondary" onClick={() => handleStop(j.host, j.name)}>
                    Stop
                  </button>
                </div>
              ))}
            </>
          )}

          {open.sessions.length > 0 && (
            <>
              <div className="section-label">Sessions</div>
              {open.sessions.map((s) => (
                <div key={s.host + s.name} className="session-block">
                  <div className="task-title">▶ {s.name} · {s.windows} windows · {s.host}</div>
                  {s.panes?.map((pn, i) => (
                    <div key={i} className="pane-row">
                      <span className="pane-win">{pn.window_name}</span>
                      <span className="machine-host">{pn.command}</span>
                    </div>
                  ))}
                </div>
              ))}
            </>
          )}

          {/* Tasks tagged with this project (Vikunja labels) */}
          {(() => {
            const slug = slugify(open.name);
            const tagged = (allTasksQuery.data?.tasks ?? []).filter(
              (t) =>
                t.kind === "vikunja_task" &&
                t.source_status !== "done" &&
                (t.labels ?? []).some((l) => l.title === slug),
            );
            if (tagged.length === 0) return null;
            return (
              <>
                <div className="section-label">Tagged #{slug} ({tagged.length})</div>
                <div className="task-list" style={{ marginBottom: 12 }}>
                  {tagged.map((t) => (
                    <div key={t.ref} className="machine-job">
                      <div
                        className="task-content"
                        onClick={() => {
                          setOpen(null);
                          onSelectTask(t);
                        }}
                      >
                        <div className="task-title">{t.title}</div>
                        {t.due_date && !t.due_date.startsWith("0001-") && (
                          <div className="task-meta">
                            due {new Date(t.due_date).toLocaleDateString()}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </>
            );
          })()}

          <div className="section-label">
            Tasks · {open.open_tasks} open{open.overdue_tasks > 0 ? ` (${open.overdue_tasks} overdue)` : ""}, {open.done_tasks} done
          </div>
          <div className="task-list">
            {open.tasks.length === 0 && (
              <div className="machine-empty">no open tasks</div>
            )}
            {open.tasks.map((t) => (
              <div key={t.alias} className="machine-job">
                <div
                  className="task-content"
                  onClick={() => {
                    setOpen(null);
                    onSelectTask(toTask(t));
                  }}
                >
                  <div className="task-title">
                    <span className={`task-source ${t.kind === "vikunja_task" ? "vikunja" : "git"}`}>
                      {t.kind === "vikunja_task" ? "V" : "G"}
                    </span>
                    {t.title}
                    {t.overdue && <span className="attention-chip"> ⚠ overdue</span>}
                  </div>
                  {t.due_date && !t.due_date.startsWith("0001-") && (
                    <div className="task-meta">due {new Date(t.due_date).toLocaleDateString()}</div>
                  )}
                </div>
                {t.kind === "vikunja_task" && (
                  <button className="btn-secondary" onClick={() => handleComplete(t)}>
                    ✓
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

          {/* Log modal */}
      {logJob && (
        <LogModal
          host={logJob.host}
          job={logJob.name}
          onClose={() => setLogJob(null)}
        />
      )}
    </>
  );
}

function LogModal({
  host,
  job,
  onClose,
}: {
  host: string;
  job: string;
  onClose: () => void;
}) {
  const [lines, setLines] = useState(50);
  const { data, isPending, error, refetch } = useQuery<JobLogResponse>({
    queryKey: ["job-log", host, job, lines],
    queryFn: () => getJobLog(host, job, lines),
    staleTime: 0,
  });

  return (
    <>
      <div className="sheet-overlay" onClick={onClose} />
      <div className="sheet log-sheet">
        <div className="sheet-handle" onClick={onClose} />
        <div className="sheet-title">⚙ {job} · {host}</div>
        <div className="log-actions">
          <button className="btn-secondary" onClick={() => refetch()}>↻ Refresh</button>
          <button
            className="btn-secondary"
            onClick={() => setLines(lines === 50 ? 150 : 50)}
          >
            {lines === 50 ? "Show 150" : "Show 50"}
          </button>
        </div>
        {isPending && <div className="machine-empty">loading…</div>}
        {error && (
          <div className="machine-error">
            {error instanceof Error ? error.message : "log unavailable"}
          </div>
        )}
        {data && <pre className="log-pre">{data.log}</pre>}
      </div>
    </>
  );
}
