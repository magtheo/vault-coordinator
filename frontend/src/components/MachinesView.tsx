import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { MachinesResponse, Machine } from "../types";
import { getMachines, refreshMachines, stopMachineJob } from "../api";

interface Props {
  onToast: (msg: string, ok: boolean) => void;
}

// Server refreshes its host-projection every `machines_poll_seconds` (30s);
// the client polls the *cached* endpoint — cheap — on the same cadence.
const POLL_MS = 30_000;

export function MachinesView({ onToast }: Props) {
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);

  const { data, error: qError, isPending } = useQuery({
    queryKey: ["machines"],
    queryFn: getMachines,
    refetchInterval: POLL_MS,
  });

  const error = qError instanceof Error ? qError.message : null;

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      await refreshMachines();
      await queryClient.invalidateQueries({ queryKey: ["machines"] });
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Refresh failed", false);
    } finally {
      setRefreshing(false);
    }
  };

  const handleStop = async (machine: string, job: string) => {
    try {
      await stopMachineJob(machine, job);
      onToast(`Stopped ${job} on ${machine}`, true);
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Stop failed", false);
    }
  };

  if (isPending && !data) {
    return (
      <div className="app-header">
        <h1>🖥️ Machines</h1>
      </div>
    );
  }

  return (
    <>
      <div className="app-header">
        <h1>🖥️ Machines</h1>
        <button className="btn-secondary" onClick={handleRefresh} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      {data?.age_seconds !== undefined && (
        <div className="machine-age">updated {Math.round(data.age_seconds)}s ago</div>
      )}
      {error && <div className="machine-error">{error}</div>}
      <div className="machine-list">
        {(data?.machines ?? []).map((m) => (
          <MachineCard key={m.name} machine={m} onStop={handleStop} />
        ))}
      </div>
    </>
  );
}

function MachineCard({
  machine,
  onStop,
}: {
  machine: Machine;
  onStop: (machine: string, job: string) => void;
}) {
  return (
    <div className={`machine-card ${machine.reachable ? "" : "unreachable"}`}>
      <div className="machine-head">
        <span className={`sync-dot ${machine.reachable ? "ok" : "stale"}`} />
        <span className="machine-name">{machine.name}</span>
        <span className="machine-host">{machine.host}</span>
      </div>

      {!machine.reachable && (
        <div className="machine-error">{machine.error ?? "unreachable"}</div>
      )}

      <div className="section-label">Jobs {machine.jobs.length > 0 && `(${machine.jobs.length})`}</div>
      {machine.jobs.length === 0 ? (
        <div className="machine-empty">no running jobs</div>
      ) : (
        machine.jobs.map((j) => (
          <div key={j.name} className="machine-job">
            <div className="task-content">
              <div className="task-title">
                {j.state === "active" ? "▶" : j.state === "failed" ? "✕" : "○"} {j.name}
              </div>
              <div className="task-meta">
                {j.since ? `${j.since} · ` : ""}{j.command.slice(0, 60)}
              </div>
            </div>
            <button className="btn-secondary" onClick={() => onStop(machine.name, j.name)}>
              Stop
            </button>
          </div>
        ))
      )}

      <div className="section-label">Sessions {machine.sessions.length > 0 && `(${machine.sessions.length})`}</div>
      <div className="machine-sessions">
        {machine.sessions.length === 0 ? (
          <span className="machine-empty">no tmux sessions</span>
        ) : (
          machine.sessions.map((s) => (
            <span key={s.name} className="machine-chip">
              {s.name} · {s.windows}w
            </span>
          ))
        )}
      </div>
    </div>
  );
}
