import { useState, useEffect, useCallback } from "react";
import type { MachinesResponse, Machine } from "../types";
import { getMachines, stopMachineJob } from "../api";

interface Props {
  onToast: (msg: string, ok: boolean) => void;
  refreshKey: number;
}

const POLL_MS = 30_000;

export function MachinesView({ onToast, refreshKey }: Props) {
  const [data, setData] = useState<MachinesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      const resp = await getMachines();
      setData(resp);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load machines");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (refreshKey > 0) load();
  }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  const handleStop = async (machine: string, job: string) => {
    try {
      await stopMachineJob(machine, job);
      onToast(`Stopped ${job} on ${machine}`, true);
      await load();
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Stop failed", false);
    }
  };

  if (loading && !data) return <div className="app-header"><h1>🖥️ Machines</h1></div>;

  return (
    <>
      <div className="app-header">
        <h1>🖥️ Machines</h1>
        <button className="btn-secondary" onClick={() => { setLoading(true); load(); }}>
          Refresh
        </button>
      </div>
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
